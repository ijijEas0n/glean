#!/usr/bin/env python3
"""快手适配器：单条作品 + 博主主页。

坑（实测，字段路径全部来自真实响应）：
- **时间与时长都是毫秒**：`timestamp` / `duration` 要自己除 1000（和抖音的秒不一样）。
- 同一份数据两套命名：`web/fetch_one_video_v2` 回 `photo`（camelCase：`likeCount`、
  `mainMvUrls`），`app/fetch_one_video` 与作品列表回 feed（snake_case：`like_count`、
  `main_mv_urls`）。所以统一先归一化再翻译，不能一套字段名走到底。
- `photo.user` 是 null，作者信息平铺在 `photo.userName/.userId/.userEid/.headUrl`；
  粉丝数只在同一响应的 `data.counts.fanCount`。
- `photo_id` 有两种写法：数字 ID（`5226990476821709485`，列表接口给这个）和
  eid（`3xv7cxgi2qn8u62`，分享链接给这个）。`fetch_one_video_v2` 两种都收（实测），
  规范链接要用 `share_info` 里的 eid。
- `web/fetch_user_info` **只认 eid**，传数字 ID 回 `result=21 参数格式错误`（实测）；
  数字 ID 要走 `app/fetch_one_user_v2`（两种都收）。
- 作品列表 `app/fetch_user_post_v2` **只认数字 ID**，并用 `pcursor` 翻页
  （第一页空，之后回传上一页的 pcursor；返回 "no_more" 表示到底）。
- `manifest.adaptationSet[].representation[].codecs` 是 null，判断编码只能看 `videoCodec`
  （实测有 avc / hevc / venus7-cape4-v1 几种），转写要挑 avc。
"""

from __future__ import annotations

from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    iso_from_unix,
    make_item_id,
)

# 接口里确实没有文案时的占位。不要用业务 ID（"抖音作品 7683191553704120241" 这种）
# 堆在标题位上——数字又长又难读，排版很难看。没有就说没有。
无文案 = "（这条没有文案）"

VIDEO_PATH = "/api/v1/kuaishou/web/fetch_one_video_v2"
VIDEO_FALLBACK_PATH = "/api/v1/kuaishou/app/fetch_one_video"
VIDEO_BY_URL_PATH = "/api/v1/kuaishou/app/fetch_one_video_by_url"
USER_INFO_WEB_PATH = "/api/v1/kuaishou/web/fetch_user_info"
USER_INFO_APP_PATH = "/api/v1/kuaishou/app/fetch_one_user_v2"
USER_POSTS_PATH = "/api/v1/kuaishou/app/fetch_user_post_v2"

REFERER = "https://www.kuaishou.com/"
MAX_PAGES = 12


# ---- 归一化：两种响应结构 → 一套内部字段 ----


def _cdn_url(*groups: Any) -> str:
    """快手的地址是 `[{"cdn":..., "url":...}]`，base.first_url 不认这种结构。"""
    for group in groups:
        for row in group if isinstance(group, list) else []:
            if isinstance(row, dict):
                url = str(row.get("url") or "")
            elif isinstance(row, str):
                url = row
            else:
                continue
            if url.startswith("http"):
                return url
    return ""


def _share_ids(share_info: Any) -> tuple[str, str]:
    """`share_info` 形如 "userId=3xq7vb3qdp27buk&photoId=3xv7cxgi2qn8u62"，取两个 eid。"""
    user_eid = ""
    photo_eid = ""
    for part in str(share_info or "").split("&"):
        key, _, value = part.partition("=")
        if key == "userId":
            user_eid = value
        elif key == "photoId":
            photo_eid = value
    return user_eid, photo_eid


def _representations(manifest: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in (manifest or {}).get("adaptationSet") or []:
        if isinstance(group, dict):
            rows.extend(row for row in (group.get("representation") or []) if isinstance(row, dict))
    return rows


def _video_urls(representations: list[dict[str, Any]], main_urls: Any) -> list[str]:
    """H.264 优先（转写要用 ffmpeg 读音频），再按码率从高到低；最后回落无签名的直链。"""
    ordered = sorted(
        representations,
        key=lambda row: (str(row.get("videoCodec") or "") == "avc",
                          not row.get("mute"), as_int(row.get("avgBitrate"))),
        reverse=True,
    )
    rows: list[str] = []
    for representation in ordered:
        for candidate in [representation.get("url"), *(representation.get("backupUrl") or [])]:
            if isinstance(candidate, str) and candidate.startswith("http") and candidate not in rows:
                rows.append(candidate)
    for group in (main_urls if isinstance(main_urls, list) else []):
        url = group.get("url") if isinstance(group, dict) else group
        if isinstance(url, str) and url.startswith("http") and url not in rows:
            rows.append(url)
    return rows


def _normalize_web(photo: dict[str, Any], counts: dict[str, Any]) -> dict[str, Any]:
    """`web/fetch_one_video_v2` 的 data.photo（camelCase）+ data.counts。"""
    share_user_eid, share_photo_eid = _share_ids(photo.get("share_info"))
    return {
        "photo_id": str(photo.get("photoId") or ""),
        "photo_eid": share_photo_eid,
        "caption": str(photo.get("caption") or ""),
        "duration_ms": as_int(photo.get("duration")),
        "timestamp_ms": as_int(photo.get("timestamp")),
        "like": as_int(photo.get("likeCount")),
        "comment": as_int(photo.get("commentCount")),
        "share": as_int(photo.get("shareCount")),
        "collect": 0,  # 单条接口没有收藏数（只有列表 feed 有），不编
        "play": as_int(photo.get("viewCount")),
        "cover": _cdn_url(photo.get("coverUrls"), photo.get("webpCoverUrls")),
        "video_urls": _video_urls(_representations(photo.get("manifest")), photo.get("mainMvUrls")),
        "author_name": str(photo.get("userName") or ""),
        "author_id": str(photo.get("userId") or ""),
        "author_eid": str(photo.get("userEid") or share_user_eid),
        "avatar": str(photo.get("headUrl") or _cdn_url(photo.get("headUrls"))),
        "followers": as_int((counts or {}).get("fanCount")),
        "is_video": str(photo.get("photoType") or "") == "VIDEO" or as_int(photo.get("type")) == 1,
    }


def _normalize_feed(feed: dict[str, Any]) -> dict[str, Any]:
    """列表/兼容接口的 feed（snake_case）。"""
    share_user_eid, share_photo_eid = _share_ids(feed.get("share_info"))
    return {
        "photo_id": str(feed.get("photo_id") or ""),
        "photo_eid": share_photo_eid,
        "caption": str(feed.get("caption") or feed.get("captionToComment") or ""),
        "duration_ms": as_int(feed.get("duration")),
        "timestamp_ms": as_int(feed.get("timestamp")),
        "like": as_int(feed.get("like_count")),
        "comment": as_int(feed.get("comment_count")),
        "share": as_int(feed.get("share_count")),
        "collect": as_int(feed.get("collect_count")),
        "play": as_int(feed.get("view_count")),
        "cover": _cdn_url(feed.get("cover_thumbnail_urls"), feed.get("ff_cover_thumbnail_urls")),
        "video_urls": _video_urls(_representations(feed.get("streamManifest")),
                                  feed.get("main_mv_urls")),
        "author_name": str(feed.get("user_name") or ""),
        "author_id": str(feed.get("user_id") or ""),
        "author_eid": share_user_eid,
        "avatar": _cdn_url(feed.get("headurls")),
        "followers": 0,  # 列表 feed 不带粉丝数
        "is_video": as_int(feed.get("type")) == 1,
    }


def _stats(row: dict[str, Any]) -> dict[str, int]:
    return {"like": as_int(row.get("like")), "comment": as_int(row.get("comment")),
            "share": as_int(row.get("share")), "collect": as_int(row.get("collect")),
            "play": as_int(row.get("play"))}


def _canonical_url(row: dict[str, Any]) -> str:
    """分享链接用的是 eid（share_info 里的 photoId=3x…），没有才退回数字 ID。"""
    key = str(row.get("photo_eid") or row.get("photo_id") or "")
    return f"https://www.kuaishou.com/short-video/{key}" if key else ""


def _profile_url(row: dict[str, Any]) -> str:
    key = str(row.get("author_eid") or row.get("author_id") or "")
    return f"https://www.kuaishou.com/profile/{key}" if key else ""


def _title(row: dict[str, Any], fallback: str) -> str:
    caption = str(row.get("caption") or "").strip()
    return caption.split("\n")[0][:80] if caption else fallback


# ---- 取数 ----


def _load_photo(photo_id: str, share_text: str,
                ctx: FetchContext) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """三级取数：web v2 → app → 按分享链接。返回 (接口标签, 原始响应, 归一化作品)。"""
    errors: list[str] = []
    if photo_id:
        try:
            payload = get(VIDEO_PATH, {"photo_id": photo_id})
            ctx.note_api(VIDEO_PATH)
            data = data_of(payload) or {}
            photo = data.get("photo") if isinstance(data, dict) else None
            if isinstance(photo, dict) and photo:
                counts = data.get("counts") or {}
                return "kuaishou_fetch_one_video_v2", payload, _normalize_web(photo, counts)
            errors.append(f"web 接口没返回作品（result={data.get('result') if isinstance(data, dict) else '?'}）")
        except TikHubError as exc:
            errors.append(f"web 接口：{exc}")

        try:
            payload = get(VIDEO_FALLBACK_PATH, {"photo_id": photo_id})
            ctx.note_api(VIDEO_FALLBACK_PATH)
            data = data_of(payload) or {}
            photos = data.get("photos") if isinstance(data, dict) else None
            feed = photos[0] if photos and isinstance(photos[0], dict) else {}
            if feed:
                ctx.warn("主接口没返回作品，已切换到 app 兼容接口")
                return "kuaishou_fetch_one_video", payload, _normalize_feed(feed)
            errors.append("app 兼容接口也没返回作品")
        except TikHubError as exc:
            errors.append(f"app 接口：{exc}")

    if share_text:
        try:
            payload = get(VIDEO_BY_URL_PATH, {"share_text": share_text})
            ctx.note_api(VIDEO_BY_URL_PATH)
            data = data_of(payload) or {}
            photos = data.get("photos") if isinstance(data, dict) else None
            feed = photos[0] if photos and isinstance(photos[0], dict) else {}
            if feed:
                ctx.warn("作品 ID 不可用，已改用分享链接取作品")
                return "kuaishou_fetch_one_video_by_url", payload, _normalize_feed(feed)
            errors.append("分享链接接口也没返回作品")
        except TikHubError as exc:
            errors.append(f"分享链接接口：{exc}")

    detail = "；".join(errors) or "缺少作品 ID"
    raise TikHubError(f"快手没有返回作品数据（{detail}）")


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


def fetch_video(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条快手作品（video / unknown 都走这里）。"""
    photo_id = str(resolved.ids.get("photo_id") or "")
    share_text = str(resolved.ids.get("share_text") or resolved.url or "")
    ctx.begin(make_item_id("kuaishou", "video", photo_id))  # 占位：拿到真实 ID 后再改

    ctx.report("正在调用快手接口", 12, f"作品 {photo_id or share_text[:40]}")
    raw_name, payload, row = _load_photo(photo_id, share_text, ctx)

    real_id = str(row.get("photo_id") or photo_id or "unknown")
    item_id = ctx.begin(make_item_id("kuaishou", "video", real_id))
    item = empty_item("kuaishou", "video", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw(raw_name, payload)

    item.update({
        "title": _title(row, 无文案),
        "text": str(row.get("caption") or "").strip(),
        "author": {
            "name": str(row.get("author_name") or ""),
            "handle": str(row.get("author_eid") or ""),
            "id": str(row.get("author_id") or row.get("author_eid") or ""),
            "avatar": str(row.get("avatar") or ""),
            "url": _profile_url(row),
            "followers": as_int(row.get("followers")),
        },
        "created_at": iso_from_unix(row.get("timestamp_ms"), unit="ms"),
        "duration": (round(as_int(row.get("duration_ms")) / 1000, 3)
                     if as_int(row.get("duration_ms")) else None),
        "stats": _stats(row),
        "canonical_url": _canonical_url(row) or resolved.url,
    })

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = []
    cover = str(row.get("cover") or "")
    if cover and ctx.want_media:
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image",
                                    referer=REFERER))

    candidates = [url for url in (row.get("video_urls") or []) if isinstance(url, str)]
    video_entry: dict[str, Any] | None = None
    if candidates and ctx.want_media:
        from .. import store as store_module
        from ..media import probe_duration
        from ..tikhub import download as download_url

        target = ctx.work_dir / "video.mp4"
        errors: list[str] = []
        for index, url in enumerate(candidates, start=1):
            try:
                size = download_url(url, target, referer=REFERER)
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
            ctx.warn(f"视频下载失败（{len(candidates)} 条线路都不可用：{'，'.join(errors[:3])}）")
    elif candidates:
        media.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                      "width": 0, "height": 0, "bytes": 0})

    item["media"] = media

    if video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
    else:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if not row.get("is_video"):
        ctx.warn("这条作品的类型不是视频（可能是图集），只入库了封面与文字")
    if not candidates:
        ctx.warn("没有取到任何播放地址（作品可能已下架或仅作者可见）")
    return item


def _user_from_web(row: dict[str, Any]) -> dict[str, Any]:
    """`web/fetch_user_info` 的 data.userProfile（实测只认 eid，能带回数字 uid）。"""
    profile = row.get("profile") or {}
    counts = row.get("ownerCount") or {}
    return {
        "eid": str(profile.get("user_id") or ""),
        "uid": str(row.get("userDefineId") or ""),
        "name": str(profile.get("user_name") or ""),
        "avatar": str(profile.get("headurl") or _cdn_url(profile.get("headurls"))),
        "signature": str(profile.get("user_text") or ""),
        "followers": as_int(counts.get("fan")),
        "following": as_int(counts.get("follow")),
        "photo_count": as_int(counts.get("photo_public")),
        "total_like": as_int(counts.get("like")),
        "gender": str(row.get("gender") or "").upper(),
        "ip_location": "",
        "verified": "",
    }


def _user_from_app(row: dict[str, Any]) -> dict[str, Any]:
    """`app/fetch_one_user_v2` 的 data.userProfile（数字 ID / eid 都收）。"""
    profile = row.get("profile") or {}
    counts = row.get("ownerCount") or {}
    return {
        "eid": "",
        "uid": str(profile.get("user_id") or ""),
        "name": str(profile.get("user_name") or ""),
        "avatar": str(profile.get("headurl") or _cdn_url(profile.get("headurls"))),
        "signature": str(profile.get("user_text") or ""),
        "followers": as_int(counts.get("fan")),
        "following": as_int(counts.get("follow")),
        "photo_count": as_int(counts.get("photo")),
        "total_like": as_int(counts.get("total_photo_like")),
        "gender": str(profile.get("user_sex") or "").upper(),
        "ip_location": str((row.get("ipLocation") or {}).get("ipLocationName") or ""),
        "verified": "认证用户" if row.get("verified") else "",
    }


def _load_user(path: str, params: dict[str, Any], mapper: Any, raw_name: str,
               ctx: FetchContext) -> dict[str, Any]:
    """取博主资料。接口不通或没数据都返回 {}（由调用方决定要不要换接口）。"""
    try:
        payload = get(path, params)
    except TikHubError as exc:
        ctx.warn(f"快手用户接口 {path.rsplit('/', 1)[-1]} 不可用（{exc}）")
        return {}
    ctx.note_api(path)
    data = data_of(payload) or {}
    row = data.get("userProfile") if isinstance(data, dict) else None
    if not isinstance(row, dict) or not row:
        message = str(data.get("error_msg") or "") if isinstance(data, dict) else ""
        ctx.warn(f"快手用户接口 {path.rsplit('/', 1)[-1]} 没返回资料"
                 f"{'（' + message + '）' if message else ''}")
        return {}
    ctx.save_raw(raw_name, payload)
    return mapper(row)


def _child_entry(feed: dict[str, Any]) -> dict[str, Any]:
    """列表里的一条 feed → children 条目。"""
    row = _normalize_feed(feed)
    photo_id = str(row.get("photo_id") or "")
    kind = "video" if row.get("is_video") else "images"
    return {
        "id": make_item_id("kuaishou", kind, photo_id),
        "title": _title(row, 无文案),
        "kind": kind,
        "duration": (round(as_int(row.get("duration_ms")) / 1000, 3)
                     if as_int(row.get("duration_ms")) else None),
        "stats": _stats(row),
        "created_at": iso_from_unix(row.get("timestamp_ms"), unit="ms"),
        "url": _canonical_url(row) or f"https://www.kuaishou.com/short-video/{photo_id}",
        "cover": str(row.get("cover") or ""),
        "local": False,
    }


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """快手博主主页：资料 + 作品列表（pcursor 翻页）。"""
    user_id = str(resolved.ids.get("user_id") or "")
    if not user_id:
        raise TikHubError("链接里没有识别到快手用户 ID")
    item_id = ctx.begin(make_item_id("kuaishou", "profile", user_id))
    item = empty_item("kuaishou", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容

    ctx.report("正在读取博主资料", 10, user_id)
    is_eid = not user_id.isdigit()
    if is_eid:
        # eid（3x…）走 web 接口，它专门支持 eid，还能带回作品列表要用的数字 uid
        user = _load_user(USER_INFO_WEB_PATH, {"user_id": user_id}, _user_from_web,
                          "kuaishou_fetch_user_info", ctx)
        if not user:
            user = _load_user(USER_INFO_APP_PATH, {"user_id": user_id}, _user_from_app,
                              "kuaishou_fetch_one_user_v2", ctx)
    else:
        # 实测 web 接口传数字 ID 会回 result=21「参数格式错误」，数字 ID 只能走 app
        user = _load_user(USER_INFO_APP_PATH, {"user_id": user_id}, _user_from_app,
                          "kuaishou_fetch_one_user_v2", ctx)
        if not user:
            user = _load_user(USER_INFO_WEB_PATH, {"user_id": user_id}, _user_from_web,
                              "kuaishou_fetch_user_info", ctx)
    if not user:
        ctx.warn("博主资料没取到，以下只展示作品列表")

    eid = str(user.get("eid") or (user_id if is_eid else ""))
    author = {
        "name": str(user.get("name") or ""),
        "handle": eid,
        "id": str(user.get("uid") or user_id),
        "avatar": str(user.get("avatar") or ""),
        "url": f"https://www.kuaishou.com/profile/{eid or user_id}",
        "followers": as_int(user.get("followers")),
    }
    item.update({
        "title": f"{author['name']}的快手主页" if author["name"] else "快手博主主页",
        "text": str(user.get("signature") or ""),
        "author": author,
        "canonical_url": author["url"],
        "profile": {
            "signature": str(user.get("signature") or ""),
            "photo_count": as_int(user.get("photo_count")),
            "following_count": as_int(user.get("following")),
            "total_like": as_int(user.get("total_like")),
            "ip_location": str(user.get("ip_location") or ""),
            "gender": str(user.get("gender") or ""),
            "verified": str(user.get("verified") or ""),
        },
    })

    uid_for_posts = str(user.get("uid") or (user_id if user_id.isdigit() else ""))
    if not uid_for_posts:
        ctx.warn("只有 eid 没有数字用户 ID，而作品列表接口只认数字 ID，本次未拉作品列表")

    ctx.report("正在拉取作品列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    page = 0
    while uid_for_posts and page < MAX_PAGES:
        page += 1
        query: dict[str, Any] = {"user_id": uid_for_posts}
        if cursor:
            query["pcursor"] = cursor
        payload = get(USER_POSTS_PATH, query)
        ctx.note_api(USER_POSTS_PATH)
        if page == 1:
            ctx.save_raw("kuaishou_fetch_user_post_v2", payload)
        data = data_of(payload) or {}
        feeds = data.get("feeds") or []
        if not feeds:
            break
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            photo_id = str(feed.get("photo_id") or "")
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            children.append(_child_entry(feed))
            if ctx.limit and len(children) >= ctx.limit:
                break
        ctx.report("正在拉取作品列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if ctx.limit and len(children) >= ctx.limit:
            break
        next_cursor = str(data.get("pcursor") or "")
        if not next_cursor or next_cursor == cursor or next_cursor == "no_more":
            break
        cursor = next_cursor

    item["children"] = children
    if not children:
        ctx.warn("没有取到任何作品（主页可能为空、仅自己可见或接口受限）")
    elif ctx.limit and len(children) >= ctx.limit:
        ctx.warn(f"已达到抓取上限 {ctx.limit} 条，博主还有更多作品未拉取")
    return item
