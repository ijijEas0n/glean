#!/usr/bin/env python3
"""通用兜底适配器：用 yt-dlp 处理认不出平台的链接。

能拿多少拿多少（标题/作者/时长/封面/字幕），拿不到就如实说，不编。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..media import parse_srt, parse_vtt
from ..resolver import Resolved
from ..tikhub import TikHubError
from .base import FetchContext, as_int, empty_item, iso_from_unix, make_item_id


def _yt_dlp_json(url: str) -> dict[str, Any]:
    command = ["yt-dlp", "--dump-single-json", "--no-warnings", "--skip-download", url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise TikHubError("未安装 yt-dlp。请先执行：brew install yt-dlp") from exc
    except subprocess.SubprocessError as exc:
        raise TikHubError(f"yt-dlp 执行失败：{type(exc).__name__}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        message = (result.stderr or "").strip().splitlines()
        detail = message[-1][:200] if message else "没有输出"
        raise TikHubError(f"yt-dlp 未能解析这个链接：{detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TikHubError("yt-dlp 返回的不是合法 JSON") from exc


def fetch(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    url = resolved.url
    item = empty_item("generic", "video", resolved)
    ctx.begin(make_item_id("generic", "video", url.rsplit("/", 1)[-1][:40]))

    ctx.report("正在用 yt-dlp 解析链接", 15)
    info = _yt_dlp_json(url)
    ctx.note_api("yt-dlp")

    video_id = str(info.get("id") or "")
    kind = "video" if (info.get("duration") or info.get("formats")) else "text"
    item_id = ctx.begin(make_item_id("generic", kind, video_id or url[-24:]))
    item = empty_item("generic", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("yt_dlp_info", {k: v for k, v in info.items() if k != "formats"})

    thumbnails = info.get("thumbnails") or []
    cover = str(info.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else "") or "")
    upload_date = str(info.get("upload_date") or "")
    created_at = ""
    if len(upload_date) == 8 and upload_date.isdigit():
        from datetime import datetime

        created_at = datetime.strptime(upload_date, "%Y%m%d").astimezone().isoformat(timespec="seconds")
    elif info.get("timestamp"):
        created_at = iso_from_unix(info.get("timestamp"))

    item.update({
        "title": str(info.get("title") or 无文案),
        "text": str(info.get("description") or "").strip(),
        "author": {
            "name": str(info.get("uploader") or info.get("channel") or ""),
            "handle": str(info.get("uploader_id") or ""),
            "id": str(info.get("channel_id") or ""),
            "avatar": "",
            "url": str(info.get("channel_url") or info.get("uploader_url") or ""),
            "followers": as_int(info.get("channel_follower_count")),
        },
        "created_at": created_at,
        "duration": as_int(info.get("duration")) or None,
        "stats": {
            "like": as_int(info.get("like_count")),
            "comment": as_int(info.get("comment_count")),
            "share": 0,
            "collect": 0,
            "play": as_int(info.get("view_count")),
        },
        "canonical_url": str(info.get("webpage_url") or url),
    })

    ctx.report("正在下载媒体", 40)
    media: list[dict[str, Any]] = []
    if cover and ctx.want_media:
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image"))

    transcript: dict[str, Any] = {"source": "none", "text": "", "segments": []}
    if kind == "video" and ctx.want_media:
        ctx.report("正在下载视频", 50)
        target_tmpl = str(ctx.work_dir / "video.%(ext)s")
        command = ["yt-dlp", "--no-warnings", "-f", "mp4/bestvideo*+bestaudio/best",
                   "-o", target_tmpl, url]
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=1800)
        except (subprocess.SubprocessError, OSError):
            pass
        downloaded = sorted(ctx.work_dir.glob("video.*"))
        if downloaded:
            from .. import store
            from ..media import probe_duration

            source = downloaded[0]
            relative = store.save_media(item_id, f"video{source.suffix}", source.read_bytes())
            entry = {"role": "video", "type": "video", "path": relative, "url": "",
                     "width": as_int(info.get("width")), "height": as_int(info.get("height")),
                     "bytes": source.stat().st_size, "duration": probe_duration(source)}
            media.append(entry)
            if ctx.want_transcribe:
                from ..media import transcribe_video

                ctx.report("正在转写语音", 62)
                engine = str(ctx.options.get("transcribe_engine") or "volcengine")
                transcript = transcribe_video(source, ctx.work_dir, engine=engine)
                if transcript.get("source") == "none" and transcript.get("note"):
                    ctx.warn(str(transcript["note"]))
        else:
            ctx.warn("视频下载失败（yt-dlp 未能下载到本地文件）")

    # 平台自带字幕优先于转写
    if transcript.get("source") == "none":
        from ..media import fetch_subtitles

        ctx.report("正在尝试平台字幕", 70)
        segments, note = fetch_subtitles(url, ctx.work_dir)
        if segments:
            transcript = {"source": "platform", "text": "\n".join(s["text"] for s in segments),
                          "segments": segments}
        elif note and kind == "video":
            ctx.warn(f"平台字幕不可用：{note}")

    item["media"] = media
    item["transcript"] = transcript
    ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
    if resolved.platform == "generic":
        ctx.warn("这个平台没有专门的适配器，内容由 yt-dlp 通用抓取，字段可能不全")
    return item

# 接口里确实没有文案时的占位。不要用业务 ID（"抖音作品 7683191553704120241" 这种）
# 堆在标题位上——数字又长又难读，排版很难看。没有就说没有。
无文案 = "（这条没有文案）"
