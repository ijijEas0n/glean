#!/usr/bin/env python3
"""X（Twitter）适配器：帖子（含自回复线程）+ 博主主页。

坑（实测）：
- 列表接口 `fetch_user_post_tweet` 的时间线里，每条的 author 字段是空的
  （只有 rest_id），博主名要取 `data.user.name`。
- 详情接口 `fetch_tweet_detail` 的正文优先用 display_text（t.co 已展开）。
- created_at 是 Twitter 自己的字符串格式（"Sun Sep 20 03:37:17 +0000 2026"），
  不是 ISO，要单独解析。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..config import load_config
from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import FetchContext, as_int, empty_item, first_url, make_item_id

DETAIL_PATH = "/api/v1/twitter/web/fetch_tweet_detail"
COMMENTS_PATH = "/api/v1/twitter/web/fetch_post_comments"
PROFILE_PATH = "/api/v1/twitter/web/fetch_user_profile"
USER_POSTS_PATH = "/api/v1/twitter/web/fetch_user_post_tweet"

X_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"

# 视频下载的分辨率上限（配置项 video_max_height 没写时的默认值）
DEFAULT_VIDEO_MAX_HEIGHT = 720


def parse_x_time(value: Any) -> str:
    """X 的时间是 'Sun Sep 20 03:37:17 +0000 2026'，转成 ISO8601。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, X_TIME_FORMAT).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return text


def _photos(media: Any) -> list[str]:
    """详情接口 media.photos / photo / all，列表接口 media[] 直接是数组。"""
    if isinstance(media, list):
        rows: list[str] = []
        for item in media:
            if isinstance(item, dict):
                url = str(item.get("media_url_https") or item.get("url") or item.get("media_url") or "")
                if url.startswith("http") and url not in rows:
                    rows.append(url)
            elif isinstance(item, str) and item.startswith("http"):
                rows.append(item)
        return rows
    if isinstance(media, dict):
        rows = []
        for group in (media.get("photos"), media.get("photo"), media.get("all")):
            for item in group if isinstance(group, list) else []:
                if isinstance(item, dict):
                    url = str(item.get("media_url_https") or item.get("url") or "")
                    if url.startswith("http") and url not in rows:
                        rows.append(url)
        return rows
    return []


def _resolution_pair(url: str) -> tuple[int, int]:
    """从 variant 地址里抠分辨率：.../vid/avc1/1280x720/xxx.mp4 → (1280, 720)。

    抠不到返回 (0, 0)——不猜，挑档那边会退化成按码率判断。
    """
    match = re.search(r"/(\d{2,5})x(\d{2,5})/", url)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _resolution_of(url: str) -> int:
    """只要画面高度，用于挑档。"""
    return _resolution_pair(url)[1]


def _video_max_height() -> int:
    """下载分辨率上限，来自本机配置。0 = 不封顶（要原画时改配置）。"""
    return as_int(load_config().get("video_max_height", DEFAULT_VIDEO_MAX_HEIGHT),
                  DEFAULT_VIDEO_MAX_HEIGHT)


def _pick_variant(candidates: list[tuple[int, int, str]], max_height: int) -> str:
    """按分辨率封顶挑档。candidates = [(高度, 码率, 地址)]。

    为什么不直接取最高码率：X 的最高档常常就是原始上传档。实测一条 4h23m 的访谈，
    四个档是 155MB / 372MB / 957MB / 3.58GB——取最高码率等于硬下 3.58GB，
    既慢又会被整文件读进内存撑爆。
    """
    if not candidates:
        return ""
    known = [row for row in candidates if row[0] > 0]
    if not known:
        # 地址里没有分辨率信息：只能按码率取最小的，起码不会再踩到原始档
        return min(candidates, key=lambda row: row[1])[2]
    if max_height > 0:
        within = [row for row in known if row[0] <= max_height]
        if within:
            return max(within, key=lambda row: (row[0], row[1]))[2]
        # 所有档都超上限：退而取最小的那档，别硬下原画
        return min(known, key=lambda row: (row[0], row[1]))[2]
    return max(known, key=lambda row: (row[0], row[1]))[2]


def _videos(media: Any, max_height: int | None = None) -> list[str]:
    """挑视频直链：默认取不超过配置上限的最高档（默认 720p）。"""
    if not isinstance(media, dict):
        return []
    limit = _video_max_height() if max_height is None else max_height
    rows: list[str] = []
    for group in (media.get("videos"), media.get("video")):
        for item in group if isinstance(group, list) else []:
            if not isinstance(item, dict):
                continue
            variants = item.get("variants") or item.get("video_urls") or []
            candidates: list[tuple[int, int, str]] = []
            for variant in variants if isinstance(variants, list) else []:
                if not isinstance(variant, dict):
                    continue
                url = str(variant.get("url") or "")
                if not url.startswith("http"):
                    continue
                if "mpegurl" in str(variant.get("content_type") or ""):
                    continue
                candidates.append((_resolution_of(url), as_int(variant.get("bitrate")), url))
            best = _pick_variant(candidates, limit)
            if not best:
                best = first_url(item.get("media_url_https"), item.get("url"), item.get("video_url"))
            if best and best not in rows:
                rows.append(best)
    return rows


def _video_poster(media: Any) -> str:
    """视频推文的缩略图地址。X 把它放在 media.video[].media_url_https。

    纯视频推文没有 photos，不取这张图的话条目在库里就是没封面的空白格子。
    """
    if not isinstance(media, dict):
        return ""
    for group in (media.get("videos"), media.get("video")):
        for item in group if isinstance(group, list) else []:
            if isinstance(item, dict):
                url = str(item.get("media_url_https") or "")
                if url.startswith("http"):
                    return url
    return ""


def _thread_text(tweet_id: str, handle: str, root_text: str, own_text: str) -> tuple[str, list[str]]:
    """把作者自己的回复楼拼成整条。返回 (正文, 警告)。"""
    warnings: list[str] = []
    try:
        payload = get(COMMENTS_PATH, {"tweet_id": tweet_id, "cursor": ""})
    except TikHubError as exc:
        return own_text, [f"线程拼接失败，只保留当前这条（{exc}）"]

    thread = (data_of(payload) or {}).get("thread") or []
    members: list[tuple[str, str]] = []
    for item in thread:
        if not isinstance(item, dict):
            continue
        author = (item.get("author") or {}).get("screen_name") or ""
        if handle and str(author).lower() != handle.lower():
            continue
        text = str(item.get("display_text") or item.get("text") or "").strip()
        if text:
            members.append((str(item.get("id") or ""), text))
    members.sort(key=lambda pair: pair[0])

    ordered: list[str] = []
    for text in [root_text, *(t for _, t in members), own_text]:
        if text and text not in ordered:
            ordered.append(text)
    if len(ordered) > 1:
        warnings.append(f"已自动拼接同作者线程（共 {len(ordered)} 段）")
    return "\n\n".join(ordered), warnings


def fetch_post(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    tweet_id = resolved.ids.get("tweet_id") or ""
    handle = resolved.ids.get("screen_name") or ""
    item = empty_item("twitter", "text", resolved)
    ctx.begin(make_item_id("twitter", "text", tweet_id))

    ctx.report("正在调用 X 接口", 12, f"帖子 {tweet_id}")
    payload = get(DETAIL_PATH, {"tweet_id": tweet_id})
    ctx.note_api(DETAIL_PATH)
    data = data_of(payload) or {}
    if not isinstance(data, dict) or not (data.get("text") or data.get("display_text")):
        raise TikHubError("X 没有返回帖子内容（帖子可能已删除、受保护或需要登录）")

    author = data.get("author") or {}
    media = data.get("media") or {}
    article = data.get("article") if isinstance(data.get("article"), dict) else None
    kind = "article" if article else ("images" if _photos(media) else ("video" if _videos(media) else "text"))

    item_id = ctx.begin(make_item_id("twitter", kind, tweet_id))
    item = empty_item("twitter", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("twitter_fetch_tweet_detail", payload)

    text = str((article or {}).get("full_text") or data.get("display_text") or data.get("text") or "").strip()
    warnings: list[str] = []
    if not article and not str(data.get("in_reply_to_status_id_str") or ""):
        # 非回复帖才拼线程，回复帖拼起来会把别人的对话也带进来
        text, warnings = _thread_text(tweet_id, str(author.get("screen_name") or handle), "", text)
    for warning in warnings:
        ctx.warn(warning)

    screen_name = str(author.get("screen_name") or handle or "")
    item.update({
        "title": str((article or {}).get("title") or "").strip() or text.split("\n")[0][:80] or 无文案,
        "text": text,
        "author": {
            "name": str(author.get("name") or screen_name),
            "handle": f"@{screen_name}" if screen_name else "",
            "id": str(author.get("rest_id") or author.get("id") or ""),
            "avatar": str(author.get("avatar") or author.get("profile_image_url_https") or ""),
            "url": f"https://x.com/{screen_name}" if screen_name else "",
            "followers": as_int(author.get("followers_count")),
        },
        "created_at": parse_x_time(data.get("created_at")),
        "stats": {
            "like": as_int(data.get("favorite_count") or data.get("favorites")),
            "comment": as_int(data.get("reply_count") or data.get("replies")),
            "share": as_int(data.get("retweet_count") or data.get("retweets")),
            "collect": as_int(data.get("bookmark_count") or data.get("bookmarks")),
            "play": as_int(data.get("views_count") or data.get("views")),
        },
        "canonical_url": f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name else resolved.url,
    })

    ctx.report("正在下载媒体", 40)
    media_entries: list[dict[str, Any]] = []
    photos = _photos(media)
    for index, url in enumerate(photos, start=1):
        if ctx.want_media:
            media_entries.append(ctx.save_media(url, f"{index:03d}",
                                                role="cover" if index == 1 else "content",
                                                media_type="image", referer="https://x.com/"))
        else:
            media_entries.append({"role": "cover" if index == 1 else "content", "type": "image",
                                  "path": "", "url": url, "width": 0, "height": 0, "bytes": 0})

    videos = _videos(media)
    if videos and not photos:
        # 纯视频推文：用视频缩略图当封面。不取的话库里这条就是没封面的空白格子
        # （索引的 cover 字段只认 role="cover" 的媒体项）。
        poster = _video_poster(media)
        if poster:
            if ctx.want_media:
                media_entries.append(ctx.save_media(poster, "cover", role="cover",
                                                    media_type="image", referer="https://x.com/"))
            else:
                media_entries.append({"role": "cover", "type": "image", "path": "",
                                      "url": poster, "width": 0, "height": 0, "bytes": 0})
    if videos and ctx.want_media:
        from .. import store
        from ..media import probe_duration
        from ..tikhub import download as download_url

        target = ctx.work_dir / "video.mp4"
        width, height = _resolution_pair(videos[0])
        ctx.report("正在下载视频", 40, f"选了 {height}p（上限 {_video_max_height()}p）" if height
                   else "已选定视频直链")
        for url in videos:
            try:
                size = download_url(url, target, referer="https://x.com/")
            except Exception as exc:  # noqa: BLE001 - 换下一档再试，但要说出来
                ctx.warn(f"视频下载失败：{exc}")
                continue
            # 走流式落盘：大视频 read_bytes() 会把整个文件读进内存，几个 GB 必爆
            relative = store.save_media_file(item_id, "video.mp4", target)
            entry = {"role": "video", "type": "video", "path": relative, "url": "",
                     "width": width, "height": height, "bytes": size, "duration": probe_duration(target)}
            media_entries.append(entry)
            # 时长要提到 item 级：索引/界面读的是 item["duration"]，
            # 只留在 media[] 里的话列表上这条永远显示没有时长。
            item["duration"] = entry.get("duration")
            if ctx.want_transcribe:
                from .. import store as store_module
                from ..media import transcribe_video

                ctx.report("正在转写语音", 62)
                path = store_module.media_abs_path(item_id, relative)
                if path:
                    engine = str(ctx.options.get("transcribe_engine") or "volcengine")
                    result = transcribe_video(path, ctx.work_dir, engine=engine)
                    if result.get("source") == "none" and result.get("note"):
                        ctx.warn(str(result["note"]))
                    item["transcript"] = result
                    ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
            break
    elif videos:
        media_entries.append({"role": "video", "type": "video", "path": "", "url": videos[0],
                              "width": 0, "height": 0, "bytes": 0})

    item["media"] = media_entries
    return item


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    handle = resolved.ids.get("screen_name") or ""
    item = empty_item("twitter", "profile", resolved)
    ctx.begin(make_item_id("twitter", "profile", handle))

    ctx.report("正在读取 X 博主资料", 10)
    payload = get(PROFILE_PATH, {"screen_name": handle})
    ctx.note_api(PROFILE_PATH)
    user = (data_of(payload) or {}).get("user") or {}
    if not user:
        raise TikHubError(f"X 没有返回 @{handle} 的资料（账号可能不存在或已改名）")

    item_id = ctx.begin(make_item_id("twitter", "profile", handle))
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("twitter_fetch_user_profile", payload)

    name = str(user.get("name") or handle)
    username = str(user.get("profile") or handle)
    item.update({
        "title": f"@{username} 的 X 主页",
        "author": {
            "name": name, "handle": f"@{username}", "id": str(user.get("rest_id") or user.get("id") or ""),
            "avatar": str(user.get("avatar") or ""), "url": f"https://x.com/{username}",
            "followers": as_int(user.get("sub_count")),
        },
        "profile": {
            "signature": str(user.get("desc") or ""),
            "statuses_count": as_int(user.get("statuses_count")),
            "media_count": as_int(user.get("media_count")),
            "following_count": as_int(user.get("friends")),
            "verified": "认证账号" if user.get("blue_verified") else "",
            "location": str(user.get("location") or ""),
            "website": str(user.get("website") or ""),
        },
        "created_at": parse_x_time(user.get("created_at")),
    })

    ctx.report("正在拉取帖子列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    cursor = ""
    page = 0
    while page < 12:
        page += 1
        query: dict[str, Any] = {"screen_name": username}
        if cursor:
            query["cursor"] = cursor
        payload = get(USER_POSTS_PATH, query)
        ctx.note_api(USER_POSTS_PATH)
        if page == 1:
            ctx.save_raw("twitter_fetch_user_post_tweet", payload)
        data = data_of(payload) or {}
        timeline = data.get("timeline") or []
        if not timeline:
            break
        for tweet in timeline:
            if not isinstance(tweet, dict):
                continue
            tweet_id = str(tweet.get("tweet_id") or "")
            tweet_text = str(tweet.get("text") or "").strip()
            photos = _photos(tweet.get("media"))
            videos = _videos(tweet.get("media"))
            is_retweet = tweet_text.startswith("RT @")
            children.append({
                "id": make_item_id("twitter", "video" if videos else ("images" if photos else "text"), tweet_id),
                "title": tweet_text.split("\n")[0][:80] or f"帖子 {tweet_id}",
                "kind": "video" if videos else ("images" if photos else "text"),
                "duration": None,
                "stats": {
                    "like": as_int(tweet.get("favorites")),
                    "comment": as_int(tweet.get("replies")),
                    "share": as_int(tweet.get("retweets")),
                    "collect": as_int(tweet.get("bookmarks")),
                    "play": as_int(tweet.get("views")),
                },
                "created_at": parse_x_time(tweet.get("created_at")),
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "cover": photos[0] if photos else "",
                "kind_note": "转推" if is_retweet else "",
                "local": False,
            })
            if ctx.limit and len(children) >= ctx.limit:
                break
        ctx.report("正在拉取帖子列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if ctx.limit and len(children) >= ctx.limit:
            break
        next_cursor = str(data.get("next_cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    item["children"] = children
    if not children:
        ctx.warn("没有取到任何帖子（账号可能没有公开推文）")
    return item

# 接口里确实没有文案时的占位。不要用业务 ID（"抖音作品 7683191553704120241" 这种）
# 堆在标题位上——数字又长又难读，排版很难看。没有就说没有。
无文案 = "（这条没有文案）"
