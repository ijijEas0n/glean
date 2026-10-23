#!/usr/bin/env python3
"""微信公众号适配器：单篇文章。

坑（实测）：

- 正文是 HTML，字段名不直观：`content_noencode` 是**带标签的 HTML 全文**，
  `content_text` 是**纯文本全文**。要落进 item.text 就得先 strip_html，
  不能直接把 `content_noencode` 塞进去，也不能只留 `content_text`（会丢掉图片位置）。
- 正文图片是**懒加载**：`<img>` 上通常只有 `data-src`，没有 `src`；
  少数带 `src` 的又是 `data:image/svg+xml` 占位。所以取值顺序是
  `data-src` → `data-original` → `src`，并且要排掉 `data:` 和 `res.wx.qq.com`（那是内联表情）。
- 属性值是 HTML 转义过的（`&amp;`），取出来必须先反转义，否则图片地址带 `&amp;` 会 404。
- `link` 也是转义过的，同样是反转义后再当 canonical_url。
- `fetch_article_detail` **完全没有互动数据**（没有阅读/点赞/在看/评论/分享），
  这些只在 `fetch_article_stats` 里。本适配器会补一次 stats 调用；
  补不到就如实留 0 并记 warning，绝不编数。
- 公众号没有稳定的"主页链接"字段，用接口给出的 `bizuin` 拼公众号主页地址
  （`bizuin` 实测等于 `link` 里的 `__biz`，是真实值，不是猜的）。
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, post
from .base import (
    FetchContext,
    as_int,
    empty_item,
    iso_from_unix,
    make_item_id,
    strip_html,
)

DETAIL_PATH = "/api/v1/wechat_mp/v2/fetch_article_detail"
STATS_PATH = "/api/v1/wechat_mp/v2/fetch_article_stats"

REFERER = "https://mp.weixin.qq.com/"
# 正文里的内联表情走这个域名，不是文章配图，下载下来没意义
EMOJI_HOST = "res.wx.qq.com"

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _article_key(url: str) -> str:
    """拿不到 sn 之前的占位 id：短链的 /s/<token> 或长链的 sn 都在 path/query 里。"""
    parsed = urlparse(url)
    if parsed.query:
        for pair in parsed.query.split("&"):
            name, _, value = pair.partition("=")
            if name == "sn" and value:
                return value
    return parsed.path.rstrip("/").split("/")[-1] or url


def _body_images(markup: str) -> list[str]:
    """从正文 HTML 里抠出配图地址：data-src 优先（懒加载），再退 data-original / src。"""
    rows: list[str] = []
    for tag in _IMG_TAG_RE.findall(markup or ""):
        attrs = {name.lower(): (double or single) for name, double, single in _ATTR_RE.findall(tag)}
        for key in ("data-src", "data-original", "src"):
            url = html.unescape(str(attrs.get(key) or "")).strip()
            if not url.startswith("http") or EMOJI_HOST in url:
                continue
            if url not in rows:
                rows.append(url)
            break
    return rows


def _author(content: dict[str, Any]) -> dict[str, Any]:
    user_name = str(content.get("user_name") or "")
    bizuin = str(content.get("bizuin") or "")
    # 公众号没有 handle 字段；自定义微信号 alias 经常是空的（实测这篇就是空串）
    handle = str(content.get("alias") or "") or user_name
    return {
        "name": str(content.get("nick_name") or ""),
        "handle": handle,
        "id": user_name,
        "avatar": str(content.get("round_head_img") or content.get("hd_head_img") or ""),
        "url": (
            f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={bizuin}&scene=124#wechat_redirect"
            if bizuin else ""
        ),
        "followers": 0,  # 文章详情里没有粉丝数，公开接口也拿不到
    }


def _stats(url: str, ctx: FetchContext) -> dict[str, int]:
    """互动数据在另一个端点。拿不到就全 0 + warning，不猜。"""
    ctx.report("正在拉取互动数据", 18)
    try:
        payload = post(STATS_PATH, {"url": url})
        ctx.note_api(STATS_PATH)
        ctx.save_raw("wechat_mp_fetch_article_stats", payload)
        row = data_of(payload) or {}
    except TikHubError as exc:
        ctx.warn(f"文章的阅读/点赞数据没取到（{exc}），互动数据留空")
        return {"like": 0, "comment": 0, "share": 0, "collect": 0, "play": 0}

    if not isinstance(row, dict):
        ctx.warn("互动数据接口返回的结构不认识，互动数据留空")
        return {"like": 0, "comment": 0, "share": 0, "collect": 0, "play": 0}
    return {
        "like": as_int(row.get("like_count")),        # 赞
        "comment": as_int(row.get("comment_count")),  # 评论
        "share": as_int(row.get("share_count")),      # 分享
        "collect": as_int(row.get("collect_count")),  # 收藏
        "play": as_int(row.get("read_num")),          # 阅读
    }


def fetch_article(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单篇公众号文章：正文 + 配图 + 互动数据。"""
    url = resolved.ids.get("article_url") or resolved.url
    if not url:
        raise TikHubError("链接里没有识别到公众号文章地址")
    ctx.begin(make_item_id("wechat_mp", "article", _article_key(url)))  # 占位：拿到 sn 后改成正确 id

    ctx.report("正在调用公众号接口", 12, _article_key(url))
    payload = post(DETAIL_PATH, {"url": url})
    ctx.note_api(DETAIL_PATH)

    data = data_of(payload) or {}
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict) or not content:
        raise TikHubError("公众号没有返回文章内容（链接可能已过期、被删或需要分享票据）")

    # sn 是公众号文章的稳定业务 id（短链 token 会变，sn 不会）
    item_id = ctx.begin(make_item_id("wechat_mp", "article", str(content.get("sn") or _article_key(url))))
    item = empty_item("wechat_mp", "article", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    ctx.save_raw("wechat_mp_fetch_article_detail", payload)

    html_body = str(content.get("content_noencode") or "")
    plain_body = str(content.get("content_text") or "")
    # strip_html 只认有限的几个实体，公众号正文里还有 &#34; 这种数字实体，剥完标签再反转义一次
    body = html.unescape(strip_html(html_body)) or html.unescape(strip_html(plain_body))
    if not body:
        ctx.warn("正文为空（接口没有返回 content_noencode / content_text）")
    elif not html_body:
        ctx.warn("接口没给 HTML 正文，正文由纯文本字段降级得到（图片位置会丢失）")

    canonical = html.unescape(str(content.get("link") or "")) or url
    ip_wording = content.get("ip_wording") if isinstance(content.get("ip_wording"), dict) else {}
    album = content.get("appmsgalbuminfo") if isinstance(content.get("appmsgalbuminfo"), dict) else {}

    item.update({
        "title": str(content.get("title") or "").strip() or f"公众号文章 {_article_key(url)}",
        "text": body,
        "author": _author(content),
        "created_at": iso_from_unix(
            content.get("create_timestamp") or content.get("ori_create_time")
        ),
        "duration": None,  # 文章不是视频
        "canonical_url": canonical,
        "profile": {
            "digest": str(content.get("desc") or ""),
            "byline": str(content.get("author") or ""),   # 文内署名，常与公众号名不同
            "signature": str(content.get("signature") or ""),
            "ip_location": str(ip_wording.get("province_name") or ""),
            "account_id": str(content.get("user_name") or ""),
            "alias": str(content.get("alias") or ""),
            "album": str(album.get("title") or ""),
            "msg_id": str(content.get("mid") or ""),
            "idx": as_int(content.get("idx")),
        },
        "stats": _stats(url, ctx),
    })

    ctx.report("正在下载配图", 35)
    media: list[dict[str, Any]] = []
    cover = str(content.get("cdn_url") or content.get("cdn_url_1_1") or content.get("cdn_url_235_1") or "")
    if cover and ctx.want_media:
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image", referer=REFERER))
    elif cover:
        media.append({"role": "cover", "type": "image", "path": "", "url": cover,
                      "width": 0, "height": 0, "bytes": 0})

    images = _body_images(html_body)
    for index, image_url in enumerate(images, start=1):
        if ctx.want_media:
            media.append(ctx.save_media(image_url, f"{index:03d}", role="content",
                                        media_type="image", referer=REFERER))
        else:
            media.append({"role": "content", "type": "image", "path": "", "url": image_url,
                          "width": 0, "height": 0, "bytes": 0})
    item["media"] = media
    if not images:
        ctx.warn("正文里没有解析到配图（可能是纯文字文章，或图片用的是接口没给的字段）")

    # 文章没有音视频，转写不适用；保留 SPEC 要求的完整 transcript 结构
    item["transcript"] = {"source": "none", "text": "", "segments": []}
    return item
