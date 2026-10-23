#!/usr/bin/env python3
"""链接识别：把用户粘进来的一坨东西 → 平台 + 类型 + 业务 ID。

豆瓣式的短链（v.douyin.com / xhslink.com / v.kuaishou.com）先跟随 302 展开再判定。
用户从 App 复制的分享文案（带一堆中文和表情）会先抽出其中的 URL。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import BROWSER_UA
from .tikhub import _build_opener, current_proxy

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？、；：\"'）】]+", re.I)
MAX_REDIRECTS = 4

PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "twitter": "X",
    "bilibili": "哔哩哔哩",
    "weibo": "微博",
    "zhihu": "知乎",
    "youtube": "YouTube",
    "kuaishou": "快手",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "wechat_mp": "微信公众号",
    "generic": "通用链接",
}

KIND_LABELS = {
    "video": "单条视频",
    "images": "图文笔记",
    "text": "文字帖",
    "article": "文章",
    "profile": "博主主页",
    "work": "单条作品",      # 视频还是图文，抓取后才能判定，先如实说"作品"
    "unknown": "未知类型",
}


@dataclass
class Resolved:
    """识别结果。"""

    input_url: str
    url: str
    platform: str
    kind: str
    ids: dict[str, str] = field(default_factory=dict)
    hint: str = ""
    needs_expand: bool = False
    expanded_from: str = ""

    @property
    def platform_label(self) -> str:
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_url": self.input_url,
            "resolved_url": self.url,
            "platform": self.platform,
            "platform_label": self.platform_label,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "ids": self.ids,
            "needs_expand": self.needs_expand,
            "expanded_from": self.expanded_from,
            "hint": self.hint or f"{self.platform_label} · {self.kind_label}",
        }


def extract_url(text: str) -> str:
    """从分享文案里抽出第一个 URL；本身就是 URL 就原样返回。"""
    text = (text or "").strip()
    if not text:
        return ""
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip("，。、）】\"'")
    # 没带协议头的情况：www.douyin.com/video/xxx
    loose = re.search(r"(?:www\.)?[a-z0-9-]+\.(?:com|cn|io|me|be|tv|co)/[^\s\u4e00-\u9fff]*", text, re.I)
    if loose:
        return "https://" + loose.group(0).rstrip("，。、）】\"'")
    return ""


def _resolve_redirect(url: str, max_hops: int = MAX_REDIRECTS) -> tuple[str, bool]:
    """跟随 302 展开短链。返回 (最终URL, 是否发生过跳转)。任何失败都原样返回。"""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
            return None

    current = url
    changed = False
    for _ in range(max_hops):
        request = urllib.request.Request(
            current,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": current_proxy(), "https": current_proxy()} if current_proxy() else {}
            ),
            NoRedirect,
        )
        try:
            with opener.open(request, timeout=15) as response:
                location = response.headers.get("Location") or response.geturl()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if not location:
                break
        except Exception:  # noqa: BLE001 - 展开失败就用原链，由后续步骤兜底
            break
        if not location or location == current:
            break
        current = urllib.parse.urljoin(current, location) if not location.startswith("http") else location
        changed = True
        # 落地到目标平台就停，不必继续跟
        if _looks_like_target(current):
            break
    return current, changed


def _looks_like_target(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "iesdouyin.com" in host or "douyin.com" in host:
        return "video" in url or "user" in url or "share" in url
    if "xiaohongshu.com" in host:
        return True
    return False


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def resolve(raw_input: str, *, expand: bool = True) -> Resolved:
    """识别链接。识别不出来时 platform=generic，交给 yt-dlp 兜底。"""
    original = (raw_input or "").strip()
    url = extract_url(original)
    if not url:
        raise ValueError("没有识别到链接。请粘贴一个 http(s) 开头的地址。")

    expanded_from = ""
    needs_expand = False
    parsed = urlparse(url)
    host = _host(url)
    path = parsed.path
    query = parse_qs(parsed.query)

    short_hosts = ("v.douyin.com", "xhslink.com", "xhslink.cn", "v.kuaishou.com",
                   "vm.tiktok.com", "b23.tv", "youtu.be", "weibo.cn")
    if expand and (any(h in host for h in short_hosts) or host in ("t.co",)):
        final, changed = _resolve_redirect(url)
        if changed:
            expanded_from = url
            needs_expand = True
            url = final
            parsed = urlparse(url)
            host = _host(url)
            path = parsed.path
            query = parse_qs(parsed.query)

    result = _match(url, host, path, query)

    # 极短链没展开成功（被风控挡了）：把原链当主链，但如实标注
    if result.platform == "generic" and needs_expand:
        result.hint = f"短链未能展开，将按通用链接处理（原始：{expanded_from}）"
    if needs_expand:
        result.needs_expand = True
        result.expanded_from = expanded_from
    return result


def _match(url: str, host: str, path: str, query: dict[str, list[str]]) -> Resolved:
    """按平台判定。每个分支都先判主页，再判单条。"""

    def make(platform: str, kind: str, ids: dict[str, str], hint: str = "") -> Resolved:
        return Resolved(input_url=url, url=url, platform=platform, kind=kind, ids=ids, hint=hint)

    # ---------- 抖音 ----------
    if "douyin.com" in host or "iesdouyin.com" in host:
        # 顺序很重要：`/user/<sec_uid>?modal_id=<aweme_id>` 是"在主页里打开某一条作品"，
        # 属于单条作品而不是主页。modal_id 必须先于 /user/ 判断，否则会整页当主页抓。
        modal = ""
        if query.get("modal_id"):
            modal = str(query["modal_id"][0]).strip()
        if modal and modal.isdigit():
            return make("douyin", "work", 
                        {"aweme_id": modal, "share_url": url,
                         "from_profile": path.split("/user/")[-1].strip("/")},
                        "抖音单条作品（抓取后判定视频或图文）")
        if "/article/" in path:
            article_id = path.split("/article/")[-1].strip("/").split("?")[0]
            if article_id:
                return make("douyin", "article", {"article_id": article_id, "share_url": url}, "抖音文章")
        if "/user/" in path:
            sec = path.split("/user/")[-1].strip("/")
            if sec:
                return make("douyin", "profile", {"sec_user_id": sec}, "抖音博主主页")
        if "/share/user/" in path:
            sec = path.split("/share/user/")[-1].strip("/")
            if sec:
                return make("douyin", "profile", {"sec_user_id": sec}, "抖音博主主页（分享链）")
        aweme = None
        match = re.search(r"/video/(\d+)", path) or re.search(r"/note/(\d+)", path)
        if match:
            aweme = match.group(1)
        elif "/share/video/" in path:
            aweme = path.split("/share/video/")[-1].strip("/")
        if aweme:
            return make("douyin", "video", {"aweme_id": aweme, "share_url": url},
                        "抖音单条作品（抓取后判定视频或图文）")
        if len(path.strip("/")) == 0:
            return make("douyin", "unknown", {}, "抖音链接缺少作品或用户标识")

    # ---------- 小红书 ----------
    if "xiaohongshu.com" in host or "xhslink" in host:
        if "/user/profile/" in path:
            user_id = path.split("/user/profile/")[-1].strip("/").split("?")[0]
            if user_id:
                return make("xiaohongshu", "profile", {"user_id": user_id}, "小红书博主主页")
        match = re.search(r"/(?:explore|discovery/item|item)/([0-9a-fA-F]{16,32})", path)
        if match:
            note_id = match.group(1).lower()
            ids = {"note_id": note_id}
            if query.get("xsec_token"):
                ids["xsec_token"] = query["xsec_token"][0]
            return make("xiaohongshu", "work", ids, "小红书笔记（抓取后判定图文或视频）")
        if "/user/profile" in path:
            return make("xiaohongshu", "profile", {}, "小红书主页链接缺少 user_id")

    # ---------- X / Twitter ----------
    if host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com"):
        match = re.search(r"/status(?:es)?/(\d+)", path)
        if match:
            handle = ""
            parts = [p for p in path.split("/") if p and p not in ("status", "statuses", match.group(1))]
            if parts:
                handle = parts[0].lstrip("@")
            ids = {"tweet_id": match.group(1)}
            if handle:
                ids["screen_name"] = handle
            # 不预判成"文字帖"：一条推文可能是视频/图文/文章，抓之前根本不知道。
            # 预判成文字帖会让界面把视频推文显示成「X · 文字帖」（实测踩过）。
            return make("twitter", "work", ids, "X 帖子（抓取后判定视频或图文）")
        parts = [p for p in path.split("/") if p and p not in ("i", "home", "with_replies", "media", "likes")]
        if parts and not path.startswith(("/i/", "/home", "/search", "/explore", "/settings")):
            handle = parts[0].lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                return make("twitter", "profile", {"screen_name": handle}, "X 博主主页")


    # ---------- B 站 ----------
    if "bilibili.com" in host or host == "b23.tv":
        match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", path)
        if match:
            token = match.group(1)
            ids = {"bv_id": token} if token.lower().startswith("bv") else {"aid": token[2:]}
            return make("bilibili", "video", ids, "哔哩哔哩单条视频")
        match = re.search(r"/space\.bilibili\.com|space\.bilibili\.com", url) or re.search(r"/(\d{4,})", path)
        space = re.search(r"space\.bilibili\.com/(\d+)", url) or re.search(r"/space/(\d+)", path)
        if space:
            return make("bilibili", "profile", {"uid": space.group(1)}, "哔哩哔哩 UP 主主页")
        if query.get("bvid"):
            return make("bilibili", "video", {"bv_id": query["bvid"][0]}, "哔哩哔哩单条视频")

    # ---------- 微博 ----------
    if "weibo.com" in host or "weibo.cn" in host:
        match = re.search(r"/(?:detail|status)/(\w+)", path)
        if match:
            return make("weibo", "text", {"post_id": match.group(1)}, "微博帖子")
        match = re.search(r"/(?:u|profile)/(\d+)", path)
        if match:
            return make("weibo", "profile", {"uid": match.group(1)}, "微博博主主页")
        match = re.search(r"/n/([A-Za-z0-9_\-]+)", path)
        if match:
            return make("weibo", "profile", {"screen_name": match.group(1)}, "微博博主主页（昵称）")
        if query.get("uid"):
            return make("weibo", "profile", {"uid": query["uid"][0]}, "微博博主主页")

    # ---------- 知乎 ----------
    if "zhihu.com" in host:
        match = re.search(r"/answer/(\d+)", path)
        if match:
            return make("zhihu", "article", {"answer_id": match.group(1)}, "知乎回答")
        match = re.search(r"/p/(\d+)", path)
        if match:
            return make("zhihu", "article", {"article_id": match.group(1)}, "知乎文章")
        match = re.search(r"/pin/(\d+)", path)
        if match:
            return make("zhihu", "text", {"pin_id": match.group(1)}, "知乎想法")
        match = re.search(r"/people/([A-Za-z0-9_\-]+)", path)
        if match:
            return make("zhihu", "profile", {"user_url_token": match.group(1)}, "知乎用户主页")

    # ---------- YouTube ----------
    if "youtube.com" in host or host == "youtu.be":
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
            if vid:
                return make("youtube", "video", {"video_id": vid}, "YouTube 视频")
        if query.get("v"):
            return make("youtube", "video", {"video_id": query["v"][0]}, "YouTube 视频")
        match = re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_\-]{6,})", path)
        if match:
            return make("youtube", "video", {"video_id": match.group(1)}, "YouTube 视频")
        match = re.search(r"/(@[A-Za-z0-9_.\-]+|channel/[A-Za-z0-9_\-]+|c/[A-Za-z0-9_.\-]+|user/[A-Za-z0-9_.\-]+)", path)
        if match:
            return make("youtube", "profile", {"channel": match.group(1)}, "YouTube 频道")

    # ---------- 快手 ----------
    if "kuaishou.com" in host:
        if "/profile/" in path:
            uid = path.split("/profile/")[-1].strip("/").split("?")[0]
            if uid:
                return make("kuaishou", "profile", {"user_id": uid}, "快手博主主页")
        match = re.search(r"/(?:short-video|fw/photo)/([A-Za-z0-9_\-]+)", path)
        if match:
            return make("kuaishou", "video", {"photo_id": match.group(1), "share_text": url}, "快手视频")
        if query.get("photoId"):
            return make("kuaishou", "video", {"photo_id": query["photoId"][0]}, "快手视频")

    # ---------- TikTok ----------
    if "tiktok.com" in host:
        match = re.search(r"/video/(\d+)", path)
        if match:
            return make("tiktok", "video", {"aweme_id": match.group(1), "share_url": url}, "TikTok 视频")
        match = re.search(r"/@([A-Za-z0-9_.]+)", path)
        if match:
            return make("tiktok", "profile", {"unique_id": match.group(1)}, "TikTok 博主主页")

    # ---------- Instagram ----------
    if "instagram.com" in host:
        match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)", path)
        if match:
            return make("instagram", "unknown", {"shortcode": match.group(1)}, "Instagram 帖子")
        user = path.strip("/").split("/")[0]
        if user and user not in ("explore", "reels", "stories", "accounts"):
            return make("instagram", "profile", {"username": user}, "Instagram 主页")

    # ---------- 微信公众号 ----------
    if "mp.weixin.qq.com" in host:
        return make("wechat_mp", "article", {"article_url": url}, "微信公众号文章")

    # ---------- 兜底 ----------
    return Resolved(
        input_url=url, url=url, platform="generic", kind="unknown", ids={"url": url},
        hint=f"未识别的平台（{host or '无域名'}），将尝试用 yt-dlp 通用抓取",
    )


def describe(resolved: Resolved) -> str:
    """给日志/界面用的一句话。"""
    return f"{resolved.platform_label} · {resolved.kind_label}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
