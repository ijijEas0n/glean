#!/usr/bin/env python3
"""TikTok 适配器：单条作品（视频 / 图集）+ 博主主页。

字段路径全部来自实测响应（.fixtures/tiktok__*.json，图集 / 翻页 / 资料参数另做活体复核）。

坑（实测，不是文档推测）：
- 作品本体在 `data.aweme_detail`；`create_time` 是 unix 秒，`video.duration` 是毫秒（与抖音同款）。
- 图集判定**不能看 media_type**：TikTok 详情里压根没有这个键，列表里它恒为 null。
  实测图集是 `aweme_type=150` / `content_type="multi_photo"` / `image_post_info.images` 有值，
  而且图集的 `video.duration=0`、`image_infos` 也是 null（两处都会误导）。
- 图片 url_list 里同一张图有 .heic 和 .jpeg 两种后缀（同集群不同 host），
  只有 Safari 认 heic，所以优先挑 jpeg，挑不到才用第一条。
- `handler_user_profile` 用 `sec_user_id` 经常回 status_code=3002334 "Please try again later"，
  用 `unique_id` 稳定可用；两个参数互为兜底，都失败就退回作品列表里的作者信息。
- `fetch_user_post_videos` 偶尔空手而归（data.status_msg="No more videos"），要如实报出来；
  翻页靠 `data.max_cursor` + `data.has_more`（活体验证第二页确实不同）。
- 视频 CDN 需要 `Referer: https://www.tiktok.com/`，缺了会 403。
"""

from __future__ import annotations

from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    first_url,
    iso_from_unix,
    make_item_id,
    normalized_media_candidates,
    short_id,
)

# 接口里确实没有文案时的占位。不要用业务 ID（"抖音作品 7683191553704120241" 这种）
# 堆在标题位上——数字又长又难读，排版很难看。没有就说没有。
无文案 = "（这条没有文案）"

DETAIL_PATH = "/api/v1/tiktok/app/v3/fetch_one_video"
DETAIL_FALLBACK_PATH = "/api/v1/tiktok/app/v3/fetch_one_video_v2"
DETAIL_SHARE_PATH = "/api/v1/tiktok/app/v3/fetch_one_video_by_share_url"
PROFILE_PATH = "/api/v1/tiktok/app/v3/handler_user_profile"
USER_ID_PATH = "/api/v1/tiktok/app/v3/get_user_id_and_sec_user_id_by_username"
POSTS_PATH = "/api/v1/tiktok/app/v3/fetch_user_post_videos"

REFERER = "https://www.tiktok.com/"
PAGE_SIZE = 20
MAX_PAGES = 12  # 主页翻页上限，防止 --limit 0 时无限翻
PHOTO_AWEME_TYPE = 150  # 实测：aweme_type=150 且 content_type=multi_photo 就是图集
BROWSER_IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png", ".webp")


def _best_image_url(*addresses: Any) -> str:
    """从若干 {url_list:[...]} 里挑一条能直接在浏览器里显示的图。

    TikTok 同一张图会给 heic / jpeg 多个 host，heic 只有 Safari 认，所以优先 jpeg。
    """
    for address in addresses:
        if not isinstance(address, dict):
            continue
        urls = [u for u in (address.get("url_list") or []) if isinstance(u, str) and u.startswith("http")]
        if not urls:
            continue
        for url in urls:
            if url.split("?")[0].lower().endswith(BROWSER_IMAGE_SUFFIXES):
                return url
        return urls[0]
    return ""


def _cover_url(video: dict[str, Any]) -> str:
    """封面优先 dynamic_cover，退回 cover / origin_cover（都是实测存在的键）。"""
    return _best_image_url(video.get("dynamic_cover"), video.get("cover"), video.get("origin_cover"))


def _gallery_urls(aweme: dict[str, Any]) -> list[str]:
    """图集原图：image_post_info.images[].display_image（实测路径）。

    注意不是 image_infos —— 图集作品的 image_infos 同样是 null，用它会把图集当视频。
    """
    info = aweme.get("image_post_info") or {}
    rows: list[str] = []
    for image in info.get("images") or []:
        if not isinstance(image, dict):
            continue
        url = _best_image_url(image.get("display_image"))
        if url and url not in rows:
            rows.append(url)
    return rows


def _gallery_title(aweme: dict[str, Any]) -> str:
    """图集自带 title（实测 image_post_info.title），比长 desc 更适合当标题；视频没有这个键。"""
    info = aweme.get("image_post_info") or {}
    return str(info.get("title") or "").strip()


def _is_gallery(aweme: dict[str, Any]) -> bool:
    """图集判定：类型标记三选一（实测都出现在同一个图集响应里）。"""
    return (
        as_int(aweme.get("aweme_type")) == PHOTO_AWEME_TYPE
        or str(aweme.get("content_type") or "") == "multi_photo"
        or bool(_gallery_urls(aweme))
    )


def _video_candidates(video: dict[str, Any]) -> list[str]:
    """播放地址优先级（FIELD_MAP 实测）：无水印 H.264 → H.264 → 码率梯子里非 HEVC 的 → 默认。

    转写要靠 ffmpeg 读音轨，HEVC 容易读不出来，所以 `play_addr_bytevc1`（TikTok 的 HEVC 键名，
    抖音叫 play_addr_265）和带水印的 `download_addr` 都排在最后。
    """
    rows: list[str] = []
    add = lambda address: rows.extend(  # noqa: E731
        u for u in (address.get("url_list") or []) if isinstance(u, str)
    ) if isinstance(address, dict) else None

    add(video.get("download_no_watermark_addr"))  # 实测既无水印又是 H.264，最好用的一条
    add(video.get("play_addr_h264"))
    for item in sorted(
        (x for x in (video.get("bit_rate") or []) if isinstance(x, dict) and not as_int(x.get("is_bytevc1"))),
        key=lambda x: as_int(x.get("bit_rate")),
        reverse=True,
    ):
        add(item.get("play_addr"))
    add(video.get("play_addr"))
    add(video.get("play_addr_bytevc1"))
    return normalized_media_candidates(rows)


def _stats(statistics: dict[str, Any]) -> dict[str, int]:
    statistics = statistics or {}
    return {
        "like": as_int(statistics.get("digg_count")),
        "comment": as_int(statistics.get("comment_count")),
        "share": as_int(statistics.get("share_count")),
        "collect": as_int(statistics.get("collect_count")),
        "play": as_int(statistics.get("play_count")),
    }


def _profile_url(unique_id: str) -> str:
    """只有拿到 @handle 才拼主页链接；拼不出来就留空，不编造 sec_uid 形式的地址。"""
    return f"https://www.tiktok.com/@{unique_id}" if unique_id else ""


def _video_url(aweme_id: str, unique_id: str) -> str:
    return f"https://www.tiktok.com/@{unique_id}/video/{aweme_id}" if (aweme_id and unique_id) else ""


def _author(author: dict[str, Any]) -> dict[str, Any]:
    """作品详情里的 author.follower_count 实测恒为 0（FIELD_MAP），只能照实存 0，不能当真实粉丝数。

    真粉丝数只在「主页资料 / 作品列表里的 author」里有值，所以主页抓取那条路径是准的。
    """
    author = author or {}
    unique_id = str(author.get("unique_id") or "")
    return {
        "name": str(author.get("nickname") or ""),
        "handle": f"@{unique_id}" if unique_id else "",
        "id": str(author.get("sec_uid") or author.get("uid") or ""),
        "avatar": _best_image_url(author.get("avatar_thumb"), author.get("avatar_medium"),
                                  author.get("avatar_larger")),
        "url": _profile_url(unique_id),
        "followers": as_int(author.get("follower_count")),
    }


def _entry_from_aweme(aweme: dict[str, Any]) -> dict[str, Any]:
    """主页清单里的一条作品（列表接口的 aweme 与详情同构）。"""
    aweme_id = str(aweme.get("aweme_id") or "")
    video = aweme.get("video") or {}
    gallery = _is_gallery(aweme)
    duration_ms = as_int(video.get("duration"))
    unique_id = str((aweme.get("author") or {}).get("unique_id") or "")
    desc = str(aweme.get("desc") or "").strip()
    return {
        "id": make_item_id("tiktok", "images" if gallery else "video", aweme_id),
        "title": _gallery_title(aweme) or desc or 无文案,
        "kind": "images" if gallery else "video",
        "duration": None if gallery else (round(duration_ms / 1000, 3) if duration_ms else None),
        "stats": _stats(aweme.get("statistics") or {}),
        "created_at": iso_from_unix(aweme.get("create_time")),
        "url": _video_url(aweme_id, unique_id) or str(aweme.get("share_url") or "").split("?")[0],
        "cover": _cover_url(video),
        "local": False,
    }


def _labels(endpoint: str) -> str:
    """把端点路径的最后一段当 raw 文件名：主接口 / 兼容接口 / 分享链接口不会互相覆盖。"""
    return endpoint.rstrip("/").rsplit("/", 1)[-1]


def _fetch_detail(aweme_id: str, share_url: str, ctx: FetchContext) -> tuple[dict[str, Any], str]:
    """取作品详情：主接口 → 兼容接口 v2 → 分享链接口。返回 (payload, 实际用的端点)。"""
    errors: list[str] = []
    if aweme_id:
        try:
            payload = get(DETAIL_PATH, {"aweme_id": aweme_id})
            ctx.note_api(DETAIL_PATH)
            return payload, DETAIL_PATH
        except TikHubError as exc:
            errors.append(f"fetch_one_video:{exc}")
            ctx.warn(f"TikTok 主接口不可用，改用兼容接口 fetch_one_video_v2（{exc}）")
        try:
            payload = get(DETAIL_FALLBACK_PATH, {"aweme_id": aweme_id})
            ctx.note_api(DETAIL_FALLBACK_PATH)
            return payload, DETAIL_FALLBACK_PATH
        except TikHubError as exc:
            errors.append(f"fetch_one_video_v2:{exc}")
            ctx.warn(f"TikTok 兼容接口也不可用，改用分享链接口（{exc}）")
    if share_url:
        payload = get(DETAIL_SHARE_PATH, {"share_url": share_url})
        ctx.note_api(DETAIL_SHARE_PATH)
        return payload, DETAIL_SHARE_PATH
    raise TikHubError("TikTok 作品详情没取到（" + "；".join(errors) + "）")


def _media_entries(aweme: dict[str, Any], gallery: list[str], ctx: FetchContext) -> list[dict[str, Any]]:
    """封面 + 图集原图落盘。图集一律全收，不因为能下视频就丢图。

    不下载媒体时也留一条只有远程地址的条目——地址是接口给的原始数据，不该白丢。
    """
    entries: list[dict[str, Any]] = []
    cover = _cover_url(aweme.get("video") or {})
    if cover:
        if ctx.want_media:
            entries.append(ctx.save_media(cover, "cover", role="cover", media_type="image", referer=REFERER))
        else:
            entries.append({"role": "cover", "type": "image", "path": "", "url": cover,
                            "width": 0, "height": 0, "bytes": 0})

    for index, url in enumerate(gallery, start=1):
        if ctx.want_media:
            entries.append(ctx.save_media(url, f"{index:03d}", role="content", media_type="image",
                                          referer=REFERER))
        else:
            entries.append({"role": "content", "type": "image", "path": "", "url": url,
                            "width": 0, "height": 0, "bytes": 0})
    return entries


def _download_video(aweme: dict[str, Any], ctx: FetchContext) -> dict[str, Any] | None:
    """下载视频到 media/。全部地址失败返回 None（不抛，让上层记 warning）。"""
    video = aweme.get("video") or {}
    candidates = _video_candidates(video)
    if not candidates:
        return None
    from ..tikhub import download as download_url

    target = ctx.work_dir / "video.mp4"
    errors: list[str] = []
    for index, url in enumerate(candidates, start=1):
        try:
            size = download_url(url, target, referer=REFERER)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"线路{index}:{type(exc).__name__}")
            continue
        from .. import store as store_module

        relative = store_module.save_media(ctx.item_id, "video.mp4", target.read_bytes())
        from ..media import probe_duration

        return {
            "role": "video", "type": "video", "path": relative, "url": "",
            "width": as_int(video.get("width")), "height": as_int(video.get("height")),
            "bytes": size, "duration": probe_duration(target),
        }
    ctx.warn(f"视频下载失败（{len(candidates)} 条线路都不可用：{'，'.join(errors[:3])}）")
    return None


def _transcribe_entry(entry: dict[str, Any] | None, ctx: FetchContext) -> dict[str, Any]:
    """按设置转写，并如实返回 transcript 对象。"""
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
    if result.get("note") and result.get("source") == "none":
        ctx.warn(str(result["note"]))
    return result


def fetch_video(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条 TikTok 作品（视频或图集）。"""
    aweme_id = str(resolved.ids.get("aweme_id") or "")
    share_url = str(resolved.ids.get("share_url") or resolved.url or "")
    if not aweme_id and not share_url:
        raise TikHubError("链接里没有识别到 TikTok 作品 ID")
    ctx.begin(make_item_id("tiktok", "video", aweme_id or short_id(share_url)))  # 占位：定类型后再改

    ctx.report("正在调用 TikTok 接口", 12, f"作品 {aweme_id}")
    payload, endpoint = _fetch_detail(aweme_id, share_url, ctx)

    data = data_of(payload) or {}
    aweme = data.get("aweme_detail") or {}
    if not aweme:
        reason = str(data.get("status_msg") or "").strip()
        raise TikHubError(
            f"TikTok 没有返回作品数据{'（' + reason + '）' if reason else ''}，作品可能已删除或仅自己可见"
        )

    gallery = _gallery_urls(aweme)
    kind = "images" if _is_gallery(aweme) else "video"

    # 类型定下来之后再落 item_id：raw/ 与 media/ 都要进同一个目录
    item_id = ctx.begin(make_item_id("tiktok", kind, aweme_id or short_id(share_url)))
    item = empty_item("tiktok", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    ctx.save_raw(f"tiktok_{_labels(endpoint)}", payload)

    video = aweme.get("video") or {}
    duration_ms = as_int(video.get("duration"))
    author = _author(aweme.get("author") or {})
    unique_id = author["handle"].lstrip("@")
    desc = str(aweme.get("desc") or "").strip()
    item.update({
        "title": (_gallery_title(aweme) if kind == "images" else "") or desc or 无文案,
        "text": desc,
        "author": author,
        "created_at": iso_from_unix(aweme.get("create_time")),
        "duration": round(duration_ms / 1000, 3) if (kind == "video" and duration_ms) else None,
        "stats": _stats(aweme.get("statistics") or {}),
        "canonical_url": (_video_url(aweme_id, unique_id)
                          or str(aweme.get("share_url") or "").split("?")[0]
                          or resolved.url),
    })

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = _media_entries(aweme, gallery, ctx)
    video_entry: dict[str, Any] | None = None
    if kind == "video" and ctx.want_media:
        video_entry = _download_video(aweme, ctx)
        if video_entry:
            media.append(video_entry)
        else:
            candidates = _video_candidates(video)
            if candidates:  # 下载失败也把远程地址留在 item 里，别让链接白白丢掉
                media.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                              "width": as_int(video.get("width")), "height": as_int(video.get("height")),
                              "bytes": 0})
    elif kind == "video":
        candidates = _video_candidates(video)
        if candidates:
            media.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                          "width": as_int(video.get("width")), "height": as_int(video.get("height")),
                          "bytes": 0})
    item["media"] = media

    if kind == "video" and video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
        ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
    elif kind == "video":
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if kind == "images" and not gallery:
        ctx.warn("接口标记为图集但没有取到图片地址")
    return item


def _profile_user(unique_id: str, sec_uid: str, ctx: FetchContext) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """博主资料：unique_id 与 sec_user_id 两个参数互为兜底（实测后者常回 3002334）。"""
    attempts: list[tuple[str, dict[str, Any], str]] = []
    if unique_id:
        attempts.append((PROFILE_PATH, {"unique_id": unique_id}, "unique_id"))
    if sec_uid:
        attempts.append((PROFILE_PATH, {"sec_user_id": sec_uid}, "sec_user_id"))
    errors: list[str] = []
    for path, params, label in attempts:
        try:
            payload = get(path, params)
        except TikHubError as exc:
            errors.append(f"{label}：{exc}")
            continue
        ctx.note_api(path)
        data = data_of(payload) or {}
        user = data.get("user") or {}
        if user:
            return user, payload
        # HTTP 200 但没有 user：TikTok 把失败原因放在 status_msg 里（实测 "Please try again later"）
        errors.append(f"{label}：{str(data.get('status_msg') or '没有 user 数据')}")
    if errors:
        ctx.warn("TikTok 博主资料没取到（" + "；".join(errors) + "），下面用作品列表里的作者信息兜底")
    return {}, None


def _resolve_sec_uid(unique_id: str, ctx: FetchContext) -> str:
    """按 @handle 换 sec_user_id（拉作品列表要用它，实测这个接口稳定）。"""
    if not unique_id:
        return ""
    try:
        payload = get(USER_ID_PATH, {"username": unique_id})
    except TikHubError as exc:
        ctx.warn(f"TikTok 没能把 @{unique_id} 换成 sec_user_id（{exc}），作品列表可能拉不到")
        return ""
    ctx.note_api(USER_ID_PATH)
    return str((data_of(payload) or {}).get("sec_user_id") or "")


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """TikTok 博主主页：资料 + 作品列表。"""
    unique_id = str(resolved.ids.get("unique_id") or "").lstrip("@")
    sec_uid = str(resolved.ids.get("sec_user_id") or "")
    if not unique_id and not sec_uid:
        raise TikHubError("链接里没有识别到 TikTok 博主标识")

    ctx.report("正在读取博主资料", 10)
    user, profile_payload = _profile_user(unique_id, sec_uid, ctx)

    # 拉作品列表必须有 sec_user_id：资料里没给就按用户名换一次
    posts_sec_uid = sec_uid or str(user.get("sec_uid") or "")
    if not posts_sec_uid:
        posts_sec_uid = _resolve_sec_uid(unique_id, ctx)

    item_id = ctx.begin(make_item_id("tiktok", "profile", posts_sec_uid or unique_id))
    item = empty_item("tiktok", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    if profile_payload is not None:
        ctx.save_raw("tiktok_handler_user_profile", profile_payload)

    ctx.report("正在拉取作品列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    fallback_author: dict[str, Any] = {}
    page = 0
    cursor = ""
    last_message = ""
    if posts_sec_uid:
        while page < MAX_PAGES:
            page += 1
            query: dict[str, Any] = {"sec_user_id": posts_sec_uid, "count": PAGE_SIZE}
            if cursor:
                query["max_cursor"] = cursor
            payload = get(POSTS_PATH, query)
            ctx.note_api(POSTS_PATH)
            if page == 1:
                ctx.save_raw("tiktok_fetch_user_post_videos", payload)
            data = data_of(payload) or {}
            rows = data.get("aweme_list") or []
            last_message = str(data.get("status_msg") or "")
            if not rows:
                break
            for aweme in rows:
                if not isinstance(aweme, dict):
                    continue
                if not fallback_author:
                    fallback_author = aweme.get("author") or {}
                children.append(_entry_from_aweme(aweme))
                if ctx.limit and len(children) >= ctx.limit:
                    break
            ctx.report("正在拉取作品列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
            if ctx.limit and len(children) >= ctx.limit:
                break
            if not as_int(data.get("has_more")):
                break
            next_cursor = str(data.get("max_cursor") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

    # 资料接口挂了也别整条失败：作品列表里的 author 字段是完整的（实测含昵称、简介、粉丝数）
    if not user and fallback_author:
        user = fallback_author

    author = _author(user)
    if posts_sec_uid and not author["id"]:
        author["id"] = posts_sec_uid
    if unique_id and not author["url"]:
        author["url"] = _profile_url(unique_id)

    item.update({
        "title": f"{author['name']}的 TikTok 主页" if author["name"] else "TikTok 博主主页",
        "author": author,
        "profile": {
            "signature": str(user.get("signature") or ""),
            "aweme_count": as_int(user.get("aweme_count")),
            "following_count": as_int(user.get("following_count")),
            "total_favorited": as_int(user.get("total_favorited")),
            "verified": str(user.get("custom_verify") or user.get("enterprise_verify_reason") or ""),
            "region": str(user.get("region") or ""),
            "ip_location": str(user.get("ip_location") or ""),
        },
        "stats": {"like": as_int(user.get("total_favorited")), "comment": 0, "share": 0,
                  "collect": 0, "play": as_int(user.get("follower_count"))},
        "children": children,
    })
    if not children:
        detail = f"（接口说明：{last_message}）" if last_message else ""
        ctx.warn(f"没有取到任何作品{detail}（主页可能为空、接口暂时受限或账号停更）")
    elif ctx.limit and len(children) >= ctx.limit:
        ctx.warn(f"已达到抓取上限 {ctx.limit} 条，博主还有更多作品未拉取")
    return item
