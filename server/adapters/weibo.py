#!/usr/bin/env python3
"""微博适配器：单条微博（文字 / 图片 / 视频，含转发）+ 博主主页。

坑（实测，字段路径全部来自真实响应，不是文档推测）：
- `data.text` 是 HTML（`<br />`、`<a>`、表情 `<img>`），纯文本正文在 `data.text_raw`。
- 长文（`isLongText=true`）时 `text_raw` 仍被截断（实测 textLength=348 只回 181 字），
  全文只在 `data.longText.content`，且必须带 `is_get_long_text=true` 才会出现该对象。
- `created_at` 形如 `Sun Sep 20 17:16:54 +0800 2026`（北京时间），
  格式与 X 相同但时区是 +0800，不能假定 +0000。
- 转发微博的原文在 `retweeted_status`，结构与主贴一致（自带 pic_infos / page_info）。
- 图片 URL 不在 `pic_ids` 里：拿 pic_id 去 `pic_infos` 取 `largest` / `mw2000`。
- 博主主页有两种入口：`/u/<uid>`（数字 uid）与 `/n/<name>`（域名）。
  后者要传 `custom`（实测 `custom=rmrbty` → 人民日报体育），传 uid 查不出来。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    first_url,
    make_item_id,
    strip_html,
)

DETAIL_PATH = "/api/v1/weibo/web_v2/fetch_post_detail"
USER_INFO_PATH = "/api/v1/weibo/web_v2/fetch_user_info"
USER_POSTS_PATH = "/api/v1/weibo/web_v2/fetch_user_posts"

# 微博时间格式与 X 同款，但偏移是 +0800
WEIBO_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"

# 列表接口会把长文截断，翻页上限防止 --limit 0 时无限翻
MAX_PAGES = 12

# 视频直链优先级：实测 h265/hevc/inch_* 全是空串，只有这三个有值
VIDEO_KEYS = ("mp4_720p_mp4", "mp4_hd_url", "stream_url_hd", "stream_url", "mp4_sd_url")


def parse_weibo_time(value: Any) -> str:
    """微博时间 'Sun Sep 20 17:16:54 +0800 2026' → ISO8601（本机时区）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, WEIBO_TIME_FORMAT).astimezone().isoformat(timespec="seconds")
    except ValueError:
        # 认不出来就原样返回：宁可显示原始字符串，也不猜一个时间
        return text


def _text(mblog: dict[str, Any]) -> str:
    """正文优先 longText.content（长文全文）→ text_raw（纯文本）→ strip_html(text)。"""
    long_text = mblog.get("longText")
    if isinstance(long_text, dict):
        content = str(long_text.get("content") or "").strip()
        if content:
            return content
    elif isinstance(long_text, str) and long_text.strip():
        return long_text.strip()
    raw = str(mblog.get("text_raw") or "").strip()
    if raw:
        return raw
    return strip_html(mblog.get("text"))


def _pics(mblog: dict[str, Any]) -> list[dict[str, Any]]:
    """图片：pic_ids 定顺序，pic_infos[pic_id] 里取最大尺寸那档。"""
    infos = mblog.get("pic_infos")
    if not isinstance(infos, dict) or not infos:
        return []
    ids = [str(pic_id) for pic_id in (mblog.get("pic_ids") or [])]
    rows: list[dict[str, Any]] = []
    for pic_id in ids or list(infos.keys()):
        info = infos.get(pic_id)
        if not isinstance(info, dict):
            continue
        for key in ("largest", "mw2000", "large", "original", "bmiddle"):
            variant = info.get(key)
            if not isinstance(variant, dict):
                continue
            url = str(variant.get("url") or "")
            if url.startswith("http"):
                rows.append({"url": url, "width": as_int(variant.get("width")),
                             "height": as_int(variant.get("height"))})
                break
    return rows


def _video_streams(mblog: dict[str, Any]) -> list[str]:
    media = ((mblog.get("page_info") or {}).get("media_info") or {})
    rows: list[str] = []
    for key in VIDEO_KEYS:
        url = str(media.get(key) or "")
        if url.startswith("http") and url not in rows:
            rows.append(url)
    return rows


def _video_duration(mblog: dict[str, Any]) -> float | None:
    media = ((mblog.get("page_info") or {}).get("media_info") or {})
    raw = media.get("duration")
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return round(value, 3) if value > 0 else None


def _cover_url(mblog: dict[str, Any]) -> str:
    """封面：page_info.page_pic（视频/头条图都有），没有就用第一张图。"""
    page = mblog.get("page_info") or {}
    url = str(page.get("page_pic") or "")
    if url.startswith("http"):
        return url
    pics = _pics(mblog)
    return pics[0]["url"] if pics else ""


def _is_video(mblog: dict[str, Any]) -> bool:
    if _video_streams(mblog):
        return True
    return str((mblog.get("page_info") or {}).get("object_type") or "") == "video"


def _stats(mblog: dict[str, Any]) -> dict[str, int]:
    return {
        "like": as_int(mblog.get("attitudes_count")),
        "comment": as_int(mblog.get("comments_count")),
        "share": as_int(mblog.get("reposts_count")),
        "collect": 0,  # 微博没有收藏数，不编
        "play": 0,
    }


def _author(user: dict[str, Any], uid: str = "") -> dict[str, Any]:
    user = user or {}
    user_id = str(user.get("idstr") or user.get("id") or uid or "")
    domain = str(user.get("domain") or "")
    return {
        "name": str(user.get("screen_name") or ""),
        "handle": domain,  # 实测多为空串，空就留空
        "id": user_id,
        "avatar": first_url(user.get("avatar_hd"), user.get("avatar_large"),
                            user.get("profile_image_url")),
        "url": f"https://weibo.com/u/{user_id}" if user_id else "",
        "followers": as_int(user.get("followers_count")),
    }


def _kind_of(mblog: dict[str, Any]) -> str:
    if _is_video(mblog):
        return "video"
    if _pics(mblog) or as_int(mblog.get("pic_num")):
        return "images"
    return "text"


def _visual_source(mblog: dict[str, Any], retweeted: dict[str, Any]) -> dict[str, Any]:
    """主贴自己没图没视频时（典型是转发），媒体取被转发的原文。"""
    if _pics(mblog) or _video_streams(mblog):
        return mblog
    if retweeted and (_pics(retweeted) or _video_streams(retweeted)):
        return retweeted
    return mblog


def _transcribe_entry(entry: dict[str, Any] | None, ctx: FetchContext) -> dict[str, Any]:
    """按设置转写，并如实返回 transcript 对象（与 douyin 同一套约定）。"""
    from .. import store as store_module
    from ..media import transcribe_video

    if not entry or not entry.get("path"):
        return {"source": "none", "text": "", "segments": [], "note": "没有可转写的视频文件"}
    if not ctx.want_transcribe:
        return {"source": "none", "text": "", "segments": [], "note": "已关闭转写"}

    ctx.report("正在转写语音", 62)
    path = store_module.media_abs_path(ctx.item_id, entry["path"])
    if not path:
        return {"source": "none", "text": "", "segments": [], "note": "视频文件缺失"}
    engine = str(ctx.options.get("transcribe_engine") or "volcengine")
    result = transcribe_video(path, ctx.work_dir, engine=engine)
    if result.get("source") == "none" and result.get("note"):
        ctx.warn(str(result["note"]))
    return result


def fetch_post(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条微博：文字 / 图片 / 视频，转发帖会把原文一并附上。"""
    post_id = str(resolved.ids.get("post_id") or "")
    if not post_id:
        raise TikHubError("链接里没有识别到微博 ID")
    ctx.begin(make_item_id("weibo", "unknown", post_id))  # 占位：定下类型后再改

    ctx.report("正在调用微博接口", 12, f"微博 {post_id}")
    payload = get(DETAIL_PATH, {"id": post_id, "is_get_long_text": "true"})
    ctx.note_api(DETAIL_PATH)
    mblog = data_of(payload) or {}
    if not isinstance(mblog, dict) or not (_text(mblog) or mblog.get("pic_num")
                                           or mblog.get("page_info")):
        raise TikHubError("微博没有返回正文（帖子可能已删除、仅粉丝可见或需要登录）")

    retweeted = mblog.get("retweeted_status")
    if not isinstance(retweeted, dict):
        retweeted = {}

    # 转发帖自己的正文往往没图没视频，类型与媒体都按"用户实际看到的那条"判定
    source = _visual_source(mblog, retweeted)
    kind = _kind_of(source)
    item_id = ctx.begin(make_item_id("weibo", kind, post_id))
    item = empty_item("weibo", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("weibo_fetch_post_detail", payload)

    text = _text(mblog)
    if retweeted:
        rt_name = str((retweeted.get("user") or {}).get("screen_name") or "")
        rt_text = _text(retweeted)
        if rt_text:
            label = f"转发自 @{rt_name}" if rt_name else "转发内容"
            text = f"{text}\n\n--- {label} ---\n{rt_text}" if text else f"--- {label} ---\n{rt_text}"
            ctx.warn(f"这是一条转发微博，正文里已附上被转发原文（{label}）")

    user = mblog.get("user") or {}
    uid = str(user.get("idstr") or user.get("id") or "")
    mblogid = str(mblog.get("mblogid") or "")
    item.update({
        "title": text.split("\n")[0][:80] if text else f"微博 {post_id}",
        "text": text,
        "author": _author(user),
        "created_at": parse_weibo_time(mblog.get("created_at")),
        "duration": _video_duration(source) if kind == "video" else None,
        "stats": _stats(mblog),
        "canonical_url": (f"https://weibo.com/{uid}/{mblogid}" if uid and mblogid
                          else f"https://weibo.com/detail/{post_id}"),
        "profile": {"source": str(mblog.get("source") or ""),
                    "region": str(mblog.get("region_name") or "")},
    })

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = []
    cover = _cover_url(source)
    if cover and ctx.want_media:
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image",
                                    referer="https://weibo.com/"))

    pics = _pics(source)
    for index, pic in enumerate(pics, start=1):
        if ctx.want_media:
            media.append(ctx.save_media(pic["url"], f"{index:03d}", role="content",
                                        media_type="image", referer="https://weibo.com/"))
        else:
            media.append({"role": "content", "type": "image", "path": "", "url": pic["url"],
                          "width": pic["width"], "height": pic["height"], "bytes": 0})

    streams = _video_streams(source)
    video_entry: dict[str, Any] | None = None
    if streams and ctx.want_media:
        from .. import store as store_module
        from ..media import probe_duration
        from ..tikhub import download as download_url

        target = ctx.work_dir / "video.mp4"
        errors: list[str] = []
        for index, url in enumerate(streams, start=1):
            try:
                size = download_url(url, target, referer="https://weibo.com/")
            except Exception as exc:  # noqa: BLE001 - 逐条线路试
                errors.append(f"线路{index}:{type(exc).__name__}")
                continue
            relative = store_module.save_media(item_id, "video.mp4", target.read_bytes())
            video_entry = {"role": "video", "type": "video", "path": relative, "url": "",
                           "width": 0, "height": 0, "bytes": size,
                           "duration": probe_duration(target)}
            media.append(video_entry)
            break
        if video_entry is None:
            ctx.warn(f"视频下载失败（{len(streams)} 条线路都不可用：{'，'.join(errors[:3])}）")
    elif streams:
        media.append({"role": "video", "type": "video", "path": "", "url": streams[0],
                      "width": 0, "height": 0, "bytes": 0})

    item["media"] = media

    if kind == "video" and video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
    elif kind == "video":
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if kind == "images" and not pics:
        ctx.warn("接口显示这是带图微博，但没有取到图片地址")
    if not text and not media:
        ctx.warn("这条微博既没有正文也没有媒体（可能是纯转发且原文已被删除）")
    return item


def _child_entry(mblog: dict[str, Any]) -> dict[str, Any]:
    """列表里的一条 → children 条目。长文标题只有截断版，展开时会用详情接口取全文。"""
    post_id = str(mblog.get("idstr") or mblog.get("id") or "")
    text = _text(mblog)
    kind = _kind_of(mblog)
    note = ""
    if isinstance(mblog.get("retweeted_status"), dict):
        note = "转发"
    elif mblog.get("isLongText"):
        # 列表接口对长文只给截断正文（实测 348 字回 181 字），如实标注
        note = "长文（列表正文被截断）"
    return {
        "id": make_item_id("weibo", kind, post_id),
        "title": text.split("\n")[0][:80] if text else f"微博 {post_id}",
        "kind": kind,
        "duration": _video_duration(mblog) if kind == "video" else None,
        "stats": _stats(mblog),
        "created_at": parse_weibo_time(mblog.get("created_at")),
        "url": f"https://weibo.com/detail/{post_id}",
        "cover": _cover_url(mblog),
        "kind_note": note,
        "local": False,
    }


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """微博博主主页：资料 + 微博列表。"""
    uid = str(resolved.ids.get("uid") or "")
    screen_name = str(resolved.ids.get("screen_name") or "")
    if not uid and not screen_name:
        raise TikHubError("链接里没有识别到微博博主（缺少 uid 或昵称）")
    item_id = ctx.begin(make_item_id("weibo", "profile", uid or screen_name))
    item = empty_item("weibo", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容

    author: dict[str, Any] = {"name": "", "handle": "", "id": uid, "avatar": "",
                              "url": resolved.url, "followers": 0}
    profile: dict[str, Any] = {}
    signature = ""
    if uid or screen_name:
        ctx.report("正在读取博主资料", 10)
        # /u/<uid> 用 uid；/n/<name> 只能用 custom（实测 uid 传域名查不出用户）
        params = {"uid": uid} if uid else {"custom": screen_name}
        try:
            payload = get(USER_INFO_PATH, params)
            ctx.note_api(USER_INFO_PATH)
            user = (data_of(payload) or {}).get("user") or {}
            if user:
                ctx.save_raw("weibo_fetch_user_info", payload)
                author.update(_author(user, uid))
                uid = author["id"] or uid
                signature = str(user.get("description") or "")
                profile = {
                    "signature": signature,
                    "statuses_count": as_int(user.get("statuses_count")),
                    "following_count": as_int(user.get("friends_count")),
                    "followers_count_text": str(user.get("followers_count_str") or ""),
                    "verified": str(user.get("verified_reason") or ""),
                    "location": str(user.get("location") or ""),
                    "gender": str(user.get("gender") or ""),
                }
            else:
                ctx.warn(f"微博没有返回 {uid or screen_name} 的资料，只展示微博列表")
        except TikHubError as exc:
            ctx.warn(f"博主资料没取到（{exc}），以下只展示微博列表")

    item["author"] = author
    item["profile"] = profile
    item["title"] = f"{author['name']}的微博" if author["name"] else "微博博主主页"
    if uid:
        item["canonical_url"] = f"https://weibo.com/u/{uid}"

    ctx.report("正在拉取微博列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    truncated_long = 0
    seen: set[str] = set()
    page = 0
    while uid and page < MAX_PAGES:
        page += 1
        payload = get(USER_POSTS_PATH, {"uid": uid, "page": page})
        ctx.note_api(USER_POSTS_PATH)
        if page == 1:
            ctx.save_raw("weibo_fetch_user_posts", payload)
        # 微博列表是双层包装：data.data.list[]
        rows = ((data_of(payload) or {}).get("data") or {}).get("list") or []
        fresh = [row for row in rows if isinstance(row, dict)
                 and str(row.get("idstr") or row.get("id") or "") not in seen]
        if not fresh:
            break
        for mblog in fresh:
            seen.add(str(mblog.get("idstr") or mblog.get("id") or ""))
            if mblog.get("isLongText"):
                truncated_long += 1
            children.append(_child_entry(mblog))
            if ctx.limit and len(children) >= ctx.limit:
                break
        ctx.report("正在拉取微博列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if ctx.limit and len(children) >= ctx.limit:
            break

    item["children"] = children
    if truncated_long:
        ctx.warn(f"{truncated_long} 条长微博在列表接口里正文被截断，标题只用了可见部分"
                 f"（展开单条时会用详情接口取全文）")
    if not children:
        ctx.warn("没有取到任何微博（主页可能为空、被保护或接口受限）")
    elif ctx.limit and len(children) >= ctx.limit:
        ctx.warn(f"已达到抓取上限 {ctx.limit} 条，博主还有更多微博未拉取")
    return item
