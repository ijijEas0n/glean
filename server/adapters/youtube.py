#!/usr/bin/env python3
"""YouTube 适配器：单条视频 + 频道主页。

坑（实测，字段路径全部来自真实响应）：
- 视频详情是 `web_v2/get_video_info`；`web_v2/get_post_detail` 只返回社区帖，不是视频。
- `length_seconds` / `view_count` / `like_count` / `comment_count` 全是字符串，要转数字；
  订阅数只有文本（`"2.08M subscribers"`），只能按它自己给的单位换算。
- 详情接口**不含任何播放地址**，必须再调一次 `get_video_streams`（实测要 11 秒，慢）。
- `get_video_streams` 的 `has_audio` / `has_video` / `codecs` 全是 null，
  判断有没有音轨只能看 `mime_type`：只有 `audio/…` 的 adaptive 流才有声音。
- 只有 `formats[]`（实测 itag 18）是音视频合一的单 URL；转写必须用它，
  否则 ffmpeg 读不到音轨。没有合流时退而用 H.264 视频流 + mp4 音频流现合并。
- 频道列表接口只给相对时间（`"17小时前"`）和缩写播放量（`"2612万次观看"`），
  所以 children 的 created_at 只能留空，相对时间放进 kind_note。
- `get_channel_id` 实测只认 `@handle` 和 `channel/UC…`；老式 `c/name` 直接 400。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_float,
    empty_item,
    make_item_id,
)

VIDEO_INFO_PATH = "/api/v1/youtube/web_v2/get_video_info"
VIDEO_STREAMS_PATH = "/api/v1/youtube/web_v2/get_video_streams"
CHANNEL_ID_PATH = "/api/v1/youtube/web_v2/get_channel_id"
CHANNEL_VIDEOS_PATH = "/api/v1/youtube/web_v2/get_channel_videos"

# 合流优先：itag 22=720p、18=360p，都是 H.264+AAC 的单文件，ffmpeg 直接能读音频
MUXED_ITAGS = (22, 18)
MAX_PAGES = 12
REFERER = "https://www.youtube.com/"

# "2.08M subscribers" / "2612万次观看" 这类平台自己的缩写文本
COUNT_UNITS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "万": 10_000, "亿": 100_000_000}
COUNT_RE = re.compile(r"([\d.]+)\s*([KkMmBb万亿]?)")


def _count(value: Any) -> int:
    """YouTube 的计数有纯数字串也有缩写文本，统一换算成整数。

    "43327" → 43327；"43,327 views" → 43327；"2.08M subscribers" → 2080000；
    "2612万次观看" → 26120000。平台本身就是四舍五入展示的，这里只能照它给的算。
    """
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    match = COUNT_RE.search(text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    return int(number * COUNT_UNITS.get(match.group(2).lower(), 1))


def _duration_seconds(value: Any) -> float | None:
    """详情的 length_seconds 是纯秒；频道列表的 duration 是 "19:03" / "1:02:03"。"""
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        try:
            numbers = [int(part) for part in text.split(":")]
        except ValueError:
            return None
        seconds = 0
        for number in numbers:
            seconds = seconds * 60 + number
        return float(seconds)
    try:
        return float(text)
    except ValueError:
        return None


def _iso(value: Any) -> str:
    """publish_date 已经是带偏移的 ISO8601，校验一下再用，避免把脏字符串写进库。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _best_thumbnail(thumbnails: Any) -> str:
    """缩略图列表按宽度取最大的一张（实测升序排列，最后一档最清晰）。"""
    best_url = ""
    best_width = -1
    for row in thumbnails if isinstance(thumbnails, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if not url.startswith("http"):
            continue
        width = int(row.get("width") or 0)
        if width > best_width:
            best_url, best_width = url, width
    return best_url


def _mime_of(fmt: dict[str, Any]) -> str:
    return str(fmt.get("mime_type") or "")


def _muxed_urls(streams: dict[str, Any]) -> list[str]:
    """音视频合一的单 URL：优先 itag 22/18，其余 mp4 合流按码率兜底。"""
    formats = [fmt for fmt in (streams.get("formats") or []) if isinstance(fmt, dict)]
    preferred = sorted(
        formats,
        key=lambda fmt: (fmt.get("itag") in MUXED_ITAGS, -int(fmt.get("itag") or 0)),
        reverse=True,
    )
    rows: list[str] = []
    for fmt in preferred:
        url = str(fmt.get("url") or "")
        if url.startswith("http") and url not in rows:
            rows.append(url)
    return rows


def _adaptive_urls(streams: dict[str, Any], kind: str) -> list[str]:
    """adaptive 流里挑真能用的：视频优先 H.264（ffmpeg 兼容），音频优先 audio/mp4。"""
    rows: list[tuple[tuple[int, int, int], str]] = []
    for fmt in streams.get("adaptive_formats") or []:
        if not isinstance(fmt, dict):
            continue
        mime = _mime_of(fmt)
        url = str(fmt.get("url") or "")
        if not url.startswith("http"):
            continue
        if kind == "video":
            if not mime.startswith("video/"):
                continue
            # avc1=H.264 最稳；vp9/av01 次之（转写要经 ffmpeg 解音频）
            score = (1 if "avc1" in mime else 0, int(fmt.get("height") or 0),
                     int(fmt.get("bitrate") or 0))
        else:
            if not mime.startswith("audio/"):
                continue
            # mp4a 容器比 webm/opus 更容易被 ffmpeg 直接读取
            score = (1 if "mp4a" in mime else 0, int(fmt.get("bitrate") or 0), 0)
        rows.append((score, url))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [url for _, url in rows]


def _mux(video: Path, audio: Path, work_dir: Path) -> Path | None:
    """用 ffmpeg 把分离的视频流与音频流合并（-c copy，不重编码）。失败返回 None。"""
    from ..media import have

    if not have("ffmpeg"):
        return None
    output = work_dir / "muxed.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-i", str(audio), "-c", "copy", "-movflags", "+faststart", str(output)],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        return None
    return output


def _download_first(urls: list[str], target: Path, *, errors: list[str], label: str) -> bool:
    """按顺序试每条线路，成功一条就返回。全部失败返回 False（不抛，让上层记警告）。"""
    from ..tikhub import download as download_url

    for index, url in enumerate(urls, start=1):
        try:
            download_url(url, target, referer=REFERER)
            return True
        except Exception as exc:  # noqa: BLE001 - 换下一条线路
            errors.append(f"{label}{index}:{type(exc).__name__}")
    return False


def _video_entry(relative: str, source: Path) -> dict[str, Any]:
    from ..media import probe_duration

    return {"role": "video", "type": "video", "path": relative, "url": "",
            "width": 0, "height": 0, "bytes": source.stat().st_size,
            "duration": probe_duration(source)}


def _download_video(streams: dict[str, Any], ctx: FetchContext,
                    item_id: str) -> tuple[dict[str, Any] | None, str]:
    """下载视频。返回 (media 条目, 降级说明)。拿不到就返回 (None, "") 并写警告。"""
    from .. import store as store_module

    errors: list[str] = []
    target = ctx.work_dir / "video.mp4"

    # 1) 合流单文件（有音轨，最省事）
    if _download_first(_muxed_urls(streams), target, errors=errors, label="合流线路"):
        relative = store_module.save_media(item_id, "video.mp4", target.read_bytes())
        return _video_entry(relative, target), ""

    # 2) 分离流：H.264 视频 + mp4 音频，现合并
    video_path = ctx.work_dir / "video_only.mp4"
    audio_path = ctx.work_dir / "audio_only.m4a"
    got_video = _download_first(_adaptive_urls(streams, "video"), video_path,
                                errors=errors, label="视频线路")
    got_audio = got_video and _download_first(_adaptive_urls(streams, "audio"), audio_path,
                                              errors=errors, label="音频线路")
    note = ""
    if got_video and got_audio:
        muxed = _mux(video_path, audio_path, ctx.work_dir)
        if muxed:
            relative = store_module.save_media(item_id, "video.mp4", muxed.read_bytes())
            return _video_entry(relative, muxed), ""
        note = "YouTube 只给了分离流且音视频合并失败，这条视频没有音轨"
    elif got_video:
        note = "YouTube 只给了分离流且音频流没下下来，这条视频没有音轨"
    if got_video:
        relative = store_module.save_media(item_id, "video.mp4", video_path.read_bytes())
        return _video_entry(relative, video_path), note

    if not errors:
        errors.append("接口没返回任何可用格式")
    ctx.warn(f"视频下载失败（{len(errors)} 条线路都不可用：{'，'.join(errors[:3])}）")
    return None, ""


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
    """单条 YouTube 视频。"""
    video_id = str(resolved.ids.get("video_id") or "")
    if not video_id:
        raise TikHubError("链接里没有识别到 YouTube 视频 ID")
    ctx.begin(make_item_id("youtube", "video", video_id))

    ctx.report("正在调用 YouTube 接口", 12, f"视频 {video_id}")
    payload = get(VIDEO_INFO_PATH, {"video_id": video_id, "language_code": "en",
                                    "need_format": "true"})
    ctx.note_api(VIDEO_INFO_PATH)
    info = data_of(payload) or {}
    if not isinstance(info, dict) or not str(info.get("title") or "").strip():
        raise TikHubError("YouTube 没有返回视频信息（视频可能已删除、私有或地区受限）")

    item_id = ctx.begin(make_item_id("youtube", "video", video_id))
    item = empty_item("youtube", "video", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("youtube_get_video_info", payload)

    followers = _count(info.get("subscriber_count_text"))
    if info.get("subscriber_count_text") and not followers:
        ctx.warn(f"订阅数只有文本（{info.get('subscriber_count_text')}），换算不出来，留空")
    item.update({
        "title": str(info.get("title") or "").strip(),
        "text": str(info.get("description") or "").strip(),
        "author": {
            "name": str(info.get("author") or ""),
            "handle": str(info.get("channel_handle") or ""),
            "id": str(info.get("channel_id") or ""),
            "avatar": _best_thumbnail(info.get("channel_avatar_thumbnails")),
            "url": str(info.get("channel_url") or info.get("owner_profile_url") or ""),
            "followers": followers,
        },
        "created_at": _iso(info.get("publish_date") or info.get("upload_date")),
        "duration": _duration_seconds(info.get("length_seconds")),
        "stats": {
            "like": _count(info.get("like_count")),
            "comment": _count(info.get("comment_count")),
            "share": 0,
            "collect": 0,
            "play": _count(info.get("view_count")),
        },
        "canonical_url": str(info.get("video_url") or f"https://www.youtube.com/watch?v={video_id}"),
        "profile": {
            "category": str(info.get("category") or ""),
            "keywords": info.get("keywords") or [],
            "is_shorts": bool(info.get("is_shorts_eligible")),
            "playability": str(info.get("playability_status") or ""),
        },
    })

    # 流地址是第二个接口，实测 11 秒；进度文案要让用户知道在等什么
    ctx.report("正在拉取视频流地址", 20, "流接口较慢，约 10 秒")
    streams: dict[str, Any] = {}
    try:
        stream_payload = get(VIDEO_STREAMS_PATH, {"video_id": video_id})
        ctx.note_api(VIDEO_STREAMS_PATH)
        data = data_of(stream_payload) or {}
        if isinstance(data, dict):
            streams = data
        ctx.save_raw("youtube_get_video_streams", stream_payload)
    except TikHubError as exc:
        ctx.warn(f"没有取到视频流地址（{exc}），只入库封面与文字信息")

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = []
    cover = _best_thumbnail(info.get("thumbnails"))
    if cover and ctx.want_media:
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image",
                                    referer=REFERER))
    elif cover:
        media.append({"role": "cover", "type": "image", "path": "", "url": cover,
                      "width": 0, "height": 0, "bytes": 0})

    video_entry: dict[str, Any] | None = None
    if streams and ctx.want_media:
        video_entry, note = _download_video(streams, ctx, item_id)
        if note:
            ctx.warn(note)
        if video_entry:
            media.append(video_entry)
    elif streams:
        urls = _muxed_urls(streams)
        if urls:
            media.append({"role": "video", "type": "video", "path": "", "url": urls[0],
                          "width": 0, "height": 0, "bytes": 0})
    if not streams:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "没有取到视频流，跳过转写"}
    item["media"] = media

    if video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
    elif streams:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}
    if info.get("is_live_content"):
        ctx.warn("这条是直播内容，接口给的流地址可能不完整")
    return item


def _resolve_channel_id(channel: str, ctx: FetchContext) -> str:
    """@handle / channel/UC… / c/name 三种写法统一成 channel_id。

    `channel/UC…` 本身就是 ID，直接用（省一次调用）；`c/name` 实测 TikHub 只回 400
    （YouTube 已把老式自定义链接 302 到 @name），所以回落成 @name 再查一次，
    并明确警告这个回落假设——不能悄悄换一个频道。
    """
    raw = channel.strip().strip("/")
    if raw.startswith("channel/"):
        candidate = raw.split("/", 1)[1]
        if candidate.startswith("UC"):
            return candidate

    handle = ""
    if raw.startswith("@"):
        handle = raw
    elif raw.startswith(("c/", "user/")):
        handle = "@" + raw.split("/", 1)[1]

    try:
        payload = get(CHANNEL_ID_PATH, {"channel_url": f"https://youtube.com/{raw}"})
        ctx.note_api(CHANNEL_ID_PATH)
        found = str((data_of(payload) or {}).get("channel_id") or "")
        if found:
            return found
        ctx.warn(f"频道地址 {raw} 没解析出 channel_id，改用 @ 形式重试")
    except TikHubError as exc:
        if not handle:
            raise TikHubError(f"YouTube 频道地址解析失败（{raw}）：{exc}") from exc
        ctx.warn(f"老式频道链接 {raw} 解析失败（{exc}），已改按 {handle} 解析，"
                 f"请核对是不是同一个频道")

    payload = get(CHANNEL_ID_PATH, {"channel_url": f"https://youtube.com/{handle}"})
    ctx.note_api(CHANNEL_ID_PATH)
    found = str((data_of(payload) or {}).get("channel_id") or "")
    if not found:
        raise TikHubError(f"YouTube 没有解析出 {channel} 的 channel_id（账号可能已改名）")
    return found


def _child_entry(video: dict[str, Any]) -> dict[str, Any]:
    """频道列表里的一条 → children 条目。"""
    video_id = str(video.get("video_id") or "")
    return {
        "id": make_item_id("youtube", "video", video_id),
        "title": str(video.get("title") or "") or f"视频 {video_id}",
        "kind": "video",
        "duration": _duration_seconds(video.get("duration")),
        "stats": {"like": 0, "comment": 0, "share": 0, "collect": 0,
                  "play": _count(video.get("view_count"))},
        # 列表接口只给相对时间（"17小时前"），算不出绝对时间：created_at 留空，原文放 kind_note
        "created_at": "",
        "url": str(video.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
        "cover": _best_thumbnail(video.get("thumbnails")) or str(video.get("thumbnail") or ""),
        "kind_note": str(video.get("published_time") or ""),
        "local": False,
    }


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """YouTube 频道主页：频道资料 + 视频清单（按 continuation_token 翻页）。"""
    channel = str(resolved.ids.get("channel") or "")
    if not channel:
        raise TikHubError("链接里没有识别到 YouTube 频道")
    item_id = ctx.begin(make_item_id("youtube", "profile", channel))
    item = empty_item("youtube", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容

    ctx.report("正在解析频道地址", 10, channel)
    channel_id = _resolve_channel_id(channel, ctx)
    item_id = ctx.begin(make_item_id("youtube", "profile", channel_id))
    item["id"] = item_id

    ctx.report("正在读取频道资料", 14, channel_id)
    payload = get(CHANNEL_VIDEOS_PATH, {"channel_id": channel_id})
    ctx.note_api(CHANNEL_VIDEOS_PATH)
    data = data_of(payload) or {}
    if not isinstance(data, dict):
        raise TikHubError("YouTube 没有返回频道数据（频道可能不存在或已改名）")
    info = data.get("channel") or {}
    if not info and not (data.get("videos") or []):
        raise TikHubError("YouTube 没有返回频道数据（频道可能不存在或已改名）")
    ctx.save_raw("youtube_get_channel_videos", payload)

    handle_url = str(info.get("handle_url") or "")
    match = re.search(r"@[A-Za-z0-9_.\-]+", handle_url)
    author = {
        "name": str(info.get("name") or channel),
        "handle": match.group(0) if match else (channel if channel.startswith("@") else ""),
        "id": str(info.get("id") or channel_id),
        "avatar": str(info.get("avatar") or ""),
        "url": str(info.get("url") or f"https://www.youtube.com/channel/{channel_id}"),
        "followers": 0,  # 频道接口不带订阅数（只有单条视频接口给文本），留 0 不猜
    }
    item.update({
        "title": f"{author['name']}的 YouTube 频道" if author["name"] else "YouTube 频道",
        "text": str(info.get("description") or ""),
        "author": author,
        "canonical_url": author["url"],
        "profile": {
            "signature": str(info.get("description") or ""),
            "verified": "认证频道" if info.get("is_verified") else "",
            "rss_url": str(info.get("rss_url") or ""),
            "keywords": str(info.get("keywords") or ""),
        },
    })

    ctx.report("正在拉取作品列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    token = ""
    page = 0
    while True:
        for video in data.get("videos") or []:
            if not isinstance(video, dict):
                continue
            video_id = str(video.get("video_id") or "")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            children.append(_child_entry(video))
            if ctx.limit and len(children) >= ctx.limit:
                break
        ctx.report("正在拉取作品列表", min(70, 30 + (page + 1) * 4), f"已取 {len(children)} 条")
        if ctx.limit and len(children) >= ctx.limit:
            break
        next_token = str(data.get("continuation_token") or "")
        page += 1
        if not next_token or next_token == token or page >= MAX_PAGES:
            break
        token = next_token
        payload = get(CHANNEL_VIDEOS_PATH, {"channel_id": channel_id,
                                            "continuation_token": token})
        ctx.note_api(CHANNEL_VIDEOS_PATH)
        data = data_of(payload) or {}
        if not isinstance(data, dict):
            break

    item["children"] = children
    if not children:
        ctx.warn("这个频道没有取到长视频（可能只发 Shorts、直播或社区帖）")
    elif ctx.limit and len(children) >= ctx.limit:
        ctx.warn(f"已达到抓取上限 {ctx.limit} 条，频道还有更多视频未拉取")
    return item
