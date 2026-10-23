#!/usr/bin/env python3
"""媒体下载与转写。

转写顺序（如实记录用了哪个，不静默降级）：
1. 平台自带字幕（yt-dlp 抓原生/自动字幕）→ source = "platform"
2. 火山引擎大模型录音文件极速版 → "volcengine"
3. 本地 faster-whisper（仅在旧系统已初始化时可用）→ "local"
4. 都没有 → "none" + warnings
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from . import store
from .config import BROWSER_UA, aliyun_api_key, load_config, volcengine_api_key
from .tikhub import download as download_url

# 本机转写用的脚本放这里
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

VOLC_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
VOLC_RESOURCE_ID = "volc.bigasr.auc_turbo"
MAX_INLINE_AUDIO_BYTES = 70 * 1024 * 1024

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

ProgressFn = Callable[[str, int, str], None]


class NoSpeechError(RuntimeError):
    """音频里没有有效语音——不算失败，整条照常入库，字幕留空。

    这是内容问题不是接口故障（SPEC §7.1）。收到它就直接 source: "none" 收工，
    **不许**再去跑本机兜底：为一条没人说话的视频等几分钟转写毫无意义（踩过）。
    """


# _explain_aliyun 判定"没说话"时返回的哨兵，用来和普通错误说明区分开。
NO_SPEECH = "NO_SPEECH"
# 「没说话」统一文案。SPEC §7.1：禁止写「转写失败」——那会让人以为出故障去重抓。
NO_SPEECH_NOTE = "这条视频没有说话（可能只有背景音乐）"


# 火山这条线到底通不通：把最近一次真实调用的结果记下来。
# 不在界面上猜，而是拿真实结果说话（状态接口会读它）。
_火山状态: dict[str, Any] = {"ok": None, "note": ""}


def volcengine_state() -> dict[str, Any]:
    return dict(_火山状态)


def _记火山(ok: bool, note: str = "") -> None:
    _火山状态["ok"] = ok
    _火山状态["note"] = note


# ---- 外部工具 ----


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(command: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float | None:
    if not have("ffprobe"):
        return None
    try:
        result = _run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], timeout=60)
        return round(float(result.stdout.strip()), 3)
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def probe_video_codec(path: Path) -> str:
    """读视频流的真实编码。读不出来返回空串。

    为什么要探：抖音等平台的高码率档位普遍是 H.265，而 Chrome 解不了 H.265——
    表现是"有声音、画面不动"。选流时已经优先 H.264，这里再兜一道，
    真下到 HEVC 就如实告诉用户，别让他以为能播。
    """
    if not have("ffprobe"):
        return ""
    try:
        result = _run([
            "ffprobe", "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path),
        ], timeout=60)
        return (result.stdout or "").strip().splitlines()[0].strip().lower() if result.stdout.strip() else ""
    except (subprocess.SubprocessError, OSError, IndexError):
        return ""


# 浏览器（Chrome/Safari）能直接播的编码
BROWSER_OK_CODECS = frozenset({"h264", "avc1", "vp8", "vp9", "av1"})


def has_video_stream(path: Path) -> bool:
    """这个文件里到底有没有视频轨。

    抖音有些作品的 play_addr 给的是**纯音频**地址（实测下到 59KB 的 mp3，
    或者只有 aac 音轨的 mp4）。不校验的话，抓回来的"视频"点开只有声音，
    而且用户完全不知道为什么。选流时靠这个把纯音频的剔掉。
    """
    if not have("ffprobe"):
        return path.exists() and path.stat().st_size > 0
    try:
        result = _run([
            "ffprobe", "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", str(path),
        ], timeout=60)
        return bool((result.stdout or "").strip())
    except (subprocess.SubprocessError, OSError):
        return False


def browser_playable(path: Path) -> bool:
    codec = probe_video_codec(path)
    return codec in BROWSER_OK_CODECS


def has_audio_stream(path: Path) -> bool:
    if not have("ffprobe"):
        return False
    try:
        result = _run([
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        ], timeout=60)
        return result.returncode == 0 and "audio" in result.stdout
    except (subprocess.SubprocessError, OSError):
        return False


def guess_suffix(url: str, content_type: str = "") -> str:
    lowered = content_type.lower()
    if "video/mp4" in lowered:
        return ".mp4"
    if "image/jpeg" in lowered:
        return ".jpg"
    if "image/png" in lowered:
        return ".png"
    if "image/webp" in lowered:
        return ".webp"
    if "audio/mpeg" in lowered:
        return ".mp3"
    path = urllib.parse.urlparse(url).path if hasattr(urllib, "parse") else url.split("?")[0]
    suffix = Path(path).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    return ".bin"


def sniff_suffix(data: bytes, fallback: str = ".bin") -> str:
    """按文件头判断真实格式。

    必须做这一步：小红书图片地址不带扩展名（格式藏在 query 的 format/webp 里），
    按 URL 猜会一律存成 .jpg，但字节其实是 WebP。文件名和内容不一致时，
    服务端会按扩展名发 Content-Type，浏览器就渲染不出来（实测过：白块）。
    """
    if len(data) < 12:
        return fallback
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return ".heic"
        if brand in (b"isom", b"mp42", b"avc1", b"iso2"):
            return ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    return fallback


# 浏览器原生能渲染的图片格式。HEIC/BMP/TIFF 不在里面——Chrome 一律白块。
WEB_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 需要转码的图片格式
NEEDS_CONVERT_SUFFIXES = {".heic", ".heif", ".bmp", ".tiff", ".tif"}


def convert_image_for_web(data: bytes, suffix: str, work_dir: Path) -> tuple[bytes, str, bool]:
    """把浏览器渲染不了的图（HEIC/BMP/TIFF）转成 JPG。返回 (字节, 扩展名, 是否转换过)。

    小红书图文大量返回 HEIC，Chrome 没有 HEVC 解码器，不转就是一片白块
    （踩过：5 张图一张都看不了）。用 macOS 自带的 `sips`，不引第三方依赖
    （SPEC §0：只用标准库 + ffmpeg/yt-dlp）。

    转不动就**原样返回**，由调用方写 warning——那时图真的看不了，该让人知道。
    """
    if suffix in WEB_IMAGE_SUFFIXES or suffix not in NEEDS_CONVERT_SUFFIXES:
        return data, suffix, False
    source = work_dir / f"convert-src{suffix}"
    target = work_dir / "convert-out.jpg"
    source.write_bytes(data)
    try:
        proc = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(source), "--out", str(target)],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return data, suffix, False
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        return data, suffix, False
    return target.read_bytes(), ".jpg", True


# ---- 媒体落盘 ----


def save_remote_media(
    item_id: str,
    url: str,
    filename_stem: str,
    *,
    role: str = "content",
    media_type: str = "image",
    referer: str = "",
    keep_remote: bool = True,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """下载一个远程媒体到 items/<id>/media/，返回 media 条目。

    下载失败不抛异常：返回一条只有远程 url 的条目（path 为空），
    由调用方决定要不要写 warning——宁可留着链接，也不要整条失败。
    """
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        suffix = ".mp4" if media_type == "video" else ".jpg"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / f"payload{suffix}"
        try:
            download_url(url, tmp_path, referer=referer)
        except Exception as exc:  # noqa: BLE001 - 单张图失败不该拖垮整条
            return {
                "role": role, "type": media_type, "path": "",
                "url": url if keep_remote else "", "width": 0, "height": 0, "bytes": 0,
                "error": f"{type(exc).__name__}: {exc}"[:160],
            }
        data = tmp_path.read_bytes()
        # 用真实字节决定扩展名，不信 URL（小红书等平台地址里根本没有扩展名）
        real_suffix = sniff_suffix(data, suffix)
        # 图片还要保证浏览器**渲染得出来**：小红书的 HEIC 在 Chrome 里是白块（踩过）
        source_format = real_suffix
        converted = False
        if media_type == "image":
            data, real_suffix, converted = convert_image_for_web(data, real_suffix, Path(tmpdir))
            if not converted and real_suffix in NEEDS_CONVERT_SUFFIXES and on_warn:
                on_warn(f"这张图是 {source_format.lstrip('.').upper()} 格式，"
                        f"浏览器显示不出来，而且没能转成 JPG")
        filename = f"{store.safe_filename(filename_stem, 'media', 50)}{real_suffix}"
        relative = store.save_media(item_id, filename, data)
    entry: dict[str, Any] = {
        "role": role, "type": media_type, "path": relative, "url": url,
        "width": 0, "height": 0, "bytes": len(data),
    }
    # 转换过就把原格式留在条目里，可追溯；但不写 warnings——
    # 转成功是好事，不该每张图刷一条黄条把真正的警告淹了。
    if converted:
        entry["source_format"] = source_format
    return entry


def save_video_file(item_id: str, source: Path, stem: str = "video") -> dict[str, Any]:
    suffix = source.suffix if source.suffix in VIDEO_SUFFIXES else ".mp4"
    filename = f"{store.safe_filename(stem, 'video', 40)}{suffix}"
    relative = store.save_media(item_id, filename, source.read_bytes())
    return {
        "role": "video", "type": "video", "path": relative, "url": "",
        "width": 0, "height": 0, "bytes": source.stat().st_size,
        "duration": probe_duration(source),
    }


# ---- 平台字幕（yt-dlp） ----


def fetch_subtitles(url: str, work_dir: Path) -> tuple[list[dict], str]:
    """用 yt-dlp 抓平台原生/自动字幕。返回 (segments, 说明)。失败返回 ([], 原因)。"""
    if not have("yt-dlp"):
        return [], "未安装 yt-dlp"
    out_tmpl = str(work_dir / "sub")
    command = [
        "yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
        "--sub-langs", "zh-Hans,zh-CN,zh,zh-Hant,en", "--sub-format", "vtt/srt/best",
        "--convert-subs", "srt", "-o", out_tmpl, url,
    ]
    try:
        _run(command, timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        return [], f"yt-dlp 抓字幕异常：{type(exc).__name__}"
    candidates = sorted(work_dir.glob("sub*.srt")) + sorted(work_dir.glob("sub*.vtt"))
    if not candidates:
        return [], "平台没有提供字幕"
    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    segments = parse_srt(text) if candidates[0].suffix == ".srt" else parse_vtt(text)
    return (segments, f"平台字幕（{candidates[0].name}）") if segments else ([], "字幕文件为空")


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _stamp_to_seconds(value: str) -> float:
    match = _TIME_RE.search(value)
    if not match:
        return 0.0
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def _segments_from_blocks(blocks: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for start_raw, end_raw, body in blocks:
        lines = [line.strip() for line in body.splitlines()]
        # 去掉 VTT 的 inline 标签与重复行
        cleaned: list[str] = []
        for line in lines:
            line = re.sub(r"<[^>]+>", "", line).strip()
            if line and (not cleaned or cleaned[-1] != line):
                cleaned.append(line)
        text = " ".join(cleaned).strip()
        if not text:
            continue
        start = _stamp_to_seconds(start_raw)
        end = _stamp_to_seconds(end_raw)
        if segments and start - float(segments[-1]["end"]) < 0.4 and text == segments[-1]["text"]:
            continue
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return segments


def parse_srt(text: str) -> list[dict[str, Any]]:
    blocks: list[tuple[str, str, str]] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in chunk.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing_index = 1 if "-->" in lines[1] else (0 if "-->" in lines[0] else -1)
        if timing_index < 0:
            continue
        start_raw, _, end_raw = lines[timing_index].partition("-->")
        blocks.append((start_raw, end_raw, "\n".join(lines[timing_index + 1:])))
    return _segments_from_blocks(blocks)


def parse_vtt(text: str) -> list[dict[str, Any]]:
    blocks: list[tuple[str, str, str]] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines or "-->" not in "".join(lines[:2]):
            continue
        timing_index = next((i for i, line in enumerate(lines[:3]) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_raw, _, end_raw = lines[timing_index].partition("-->")
        blocks.append((start_raw, end_raw.split(" ")[0], "\n".join(lines[timing_index + 1:])))
    return _segments_from_blocks(blocks)


# ---- 阿里云百炼转写 ----
#
# 走的是官方"上传本地文件拿临时 URL → 异步转写"这条路（不是同步 base64）：
#   ① GET  /api/v1/uploads?action=getPolicy&model=<模型>   拿上传凭证（免费临时存储）
#   ② POST <upload_host>（multipart）                       得到 oss:// 临时 URL
#   ③ POST /api/v1/services/audio/asr/transcription         提交转写任务
#        必须带 X-DashScope-OssResourceResolve: enable，否则解析不了 oss://
#   ④ GET  /api/v1/tasks/<task_id>                          轮询
#   ⑤ 下载 transcription_url 的 JSON                        sentences[] 里有毫秒级时间戳
#
# 为什么不用同步接口：同步那类（base64 直传）只吃 5 分钟以内的音频，
# 而且 qwen3-asr-flash 的同步口根本不返回时间戳。异步这条能到 12 小时，
# 时间戳固定开启，本地文件走临时上传即可，**不需要把音频切段**。
#
# 硬约束（文档明确写了的）：
#   · 上传时指定的模型必须和调用时完全一致，否则文件用不了
#   · 上传与调用必须同一个阿里云主账号的 Key
#   · 临时 URL 48 小时有效；文件上传后不可查、不可改、不可下载，只能当参数用
#   · 上传凭证接口 100 QPS 限流

ALIYUN_API_ROOT = "https://dashscope.aliyuncs.com"
ALIYUN_UPLOAD_POLICY_PATH = "/api/v1/uploads"
ALIYUN_TRANSCRIBE_PATH = "/api/v1/services/audio/asr/transcription"
ALIYUN_TASKS_PATH = "/api/v1/tasks"
ALIYUN_DEFAULT_FILETRANS_MODEL = "qwen-audio-3.1-asr-flash-filetrans"
ALIYUN_POLL_INTERVAL = 3
ALIYUN_POLL_TIMEOUT = 1800        # 30 分钟，够 12 小时音频里比较长的了
ALIYUN_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024   # 官方上限 1GB

# 阿里云这条线到底通不通，记最近一次真实结果
_阿里云状态: dict[str, Any] = {"ok": None, "note": ""}


def aliyun_state() -> dict[str, Any]:
    return dict(_阿里云状态)


def _记阿里云(ok: bool, note: str = "") -> None:
    _阿里云状态["ok"] = ok
    _阿里云状态["note"] = note


def _explain_aliyun(code: Any, message: str) -> str:
    """把阿里云的错误翻成能直接照做的说明。"""
    text = str(message or "")
    low = text.lower()
    if "invalidapikey" in low or "invalid api-key" in low or code in ("InvalidApiKey", 401):
        return "阿里云百炼 Key 无效或没配好（检查 DASHSCOPE_API_KEY）"
    if "arrearage" in low or "overdue" in low or "欠费" in text:
        return "阿里云账号欠费了，充值后即可继续"
    if "throttling" in low or "rate limit" in low or code == "Throttling":
        return "阿里云限流了（请求太频繁），等一会儿再试"
    if "model" in low and ("not exist" in low or "not found" in low or "invalid" in low):
        return f"阿里云没有这个模型（{text[:80]}）。上传和调用必须用同一个模型名"
    if "ossresource" in low or "oss://" in text or "resolve" in low:
        return ("阿里云解析不了 oss:// 临时链接。调用时必须带请求头 "
                "X-DashScope-OssResourceResolve: enable")
    if "download" in low and "fail" in low:
        return f"阿里云下载不到这个文件（{text[:80]}），可能是临时链接过期了"
    if "have_no_words" in low or "no words" in low:
        # 这不是故障——这条音频里真的没人说话（纯音乐/无口播）。
        # 当成故障的话会白白再跑一遍本机模型，几分钟白等。
        return NO_SPEECH
    if "size" in low or "too large" in low:
        return f"文件太大了（{text[:80]}）"
    return f"阿里云识别失败（{code}）：{text[:160] or '无说明'}"


def _阿里云请求(path: str, api_key: str, *, 方法: str = "GET", 负载: Any = None,
               额外头: dict[str, str] | None = None, 超时: int = 120) -> dict[str, Any]:
    头 = {"Authorization": f"Bearer {api_key}"}
    if 负载 is not None:
        头["Content-Type"] = "application/json"
    if 额外头:
        头.update(额外头)
    数据 = json.dumps(负载, ensure_ascii=False).encode("utf-8") if 负载 is not None else None
    请求 = urllib.request.Request(ALIYUN_API_ROOT + path, data=数据, headers=头, method=方法)
    try:
        with urllib.request.urlopen(请求, timeout=超时) as 响应:
            return json.loads(响应.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        原文 = exc.read().decode("utf-8", "replace")
        try:
            体 = json.loads(原文)
            code = 体.get("code") or (体.get("error") or {}).get("code") or exc.code
            msg = (体.get("message") or (体.get("error") or {}).get("message")
                   or 体.get("msg") or 原文[:200])
        except json.JSONDecodeError:
            code, msg = exc.code, 原文[:200]
        说明 = _explain_aliyun(code, msg)
        # 「没说话」是内容问题不是故障（SPEC §7.1）：必须抛 NoSpeechError。
        # 抛 RuntimeError 会被 transcribe_video 当成故障去跑本机兜底，几分钟白等。
        if 说明 == NO_SPEECH:
            raise NoSpeechError(NO_SPEECH_NOTE) from exc
        raise RuntimeError(说明) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"阿里云请求失败：{type(exc).__name__}: {exc}") from exc


def _阿里云上传(api_key: str, 模型: str, 音频: Path) -> str:
    """把本地音频传到阿里云临时存储，返回 oss:// 开头的临时 URL。"""
    大小 = 音频.stat().st_size
    if 大小 > ALIYUN_UPLOAD_MAX_BYTES:
        raise RuntimeError(f"音频 {大小 // 1024 // 1024}MB 超过阿里云 1GB 上传上限")

    策略 = _阿里云请求(
        f"{ALIYUN_UPLOAD_POLICY_PATH}?action=getPolicy&model={urllib.parse.quote(模型, safe='')}",
        api_key,
    ).get("data") or {}
    必需 = ("upload_dir", "oss_access_key_id", "signature", "policy", "upload_host")
    缺 = [k for k in 必需 if not 策略.get(k)]
    if 缺:
        raise RuntimeError(f"阿里云没返回完整的上传凭证（缺 {', '.join(缺)}）")

    文件名 = re.sub(r"[^0-9A-Za-z._\-]+", "_", 音频.name) or "audio.mp3"
    key = f"{策略['upload_dir']}/{文件名}"
    边界 = "----glean" + uuid.uuid4().hex
    # 顺序有讲究：key 必须在 file 之前，OSS 才认
    字段 = [
        ("OSSAccessKeyId", 策略["oss_access_key_id"]),
        ("Signature", 策略["signature"]),
        ("policy", 策略["policy"]),
        ("x-oss-object-acl", 策略.get("x_oss_object_acl", "")),
        ("x-oss-forbid-overwrite", 策略.get("x_oss_forbid_overwrite", "")),
        ("key", key),
        ("success_action_status", "200"),
    ]
    块: list[bytes] = []
    for 名, 值 in 字段:
        块.append(
            f"--{边界}\r\nContent-Disposition: form-data; name=\"{名}\"\r\n\r\n{值}\r\n".encode("utf-8")
        )
    块.append(
        f"--{边界}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{文件名}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    块.append(音频.read_bytes())
    块.append(f"\r\n--{边界}--\r\n".encode("utf-8"))
    体 = b"".join(块)

    请求 = urllib.request.Request(
        策略["upload_host"], data=体, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={边界}",
                 "Content-Length": str(len(体))},
    )
    try:
        with urllib.request.urlopen(请求, timeout=1800) as 响应:
            if 响应.status not in (200, 203, 204):
                raise RuntimeError(f"上传返回 HTTP {响应.status}")
    except urllib.error.HTTPError as exc:
        详情 = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"上传到阿里云临时存储失败（HTTP {exc.code}）：{详情}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"上传到阿里云临时存储失败：{type(exc).__name__}: {exc}") from exc
    return f"oss://{key}"


def transcribe_with_aliyun(audio: Path, work_dir: Path, model: str = "",
                           on_progress: "ProgressFn | None" = None) -> list[dict[str, Any]]:
    """阿里云百炼异步转写。本地文件经临时上传后提交，支持超长音频，句级时间戳。"""
    api_key = aliyun_api_key()
    if not api_key:
        raise RuntimeError(
            "没有配置阿里云百炼 Key。设置里可以填，或设置环境变量：\n"
            '在 .env 里填写 DASHSCOPE_API_KEY 后重启服务'
        )
    配置 = load_config()
    模型 = model or str(配置.get("aliyun_asr_model") or ALIYUN_DEFAULT_FILETRANS_MODEL)

    def 报(标签: str, 百分比: int, 说明: str = "") -> None:
        if on_progress:
            on_progress(标签, 百分比, 说明)

    报("正在把音频传到阿里云", 52, f"模型 {模型}")
    临时链接 = _阿里云上传(api_key, 模型, audio)

    报("正在提交转写任务", 58)
    提交 = _阿里云请求(
        ALIYUN_TRANSCRIBE_PATH, api_key, 方法="POST",
        额外头={
            "X-DashScope-Async": "enable",
            # 少了这个头，阿里云解析不了 oss:// 链接，会直接报错
            "X-DashScope-OssResourceResolve": "enable",
        },
        负载={
            "model": 模型,
            "input": {"file_urls": [临时链接]},
            "parameters": {"channel_id": [0]},
        },
    )
    任务号 = str(((提交.get("output") or {}).get("task_id")) or "")
    if not 任务号:
        raise RuntimeError(f"阿里云没返回任务号：{json.dumps(提交, ensure_ascii=False)[:200]}")

    报("正在等阿里云转写", 62, "长音频要等一会儿")
    开始 = time.time()
    结果: dict[str, Any] = {}
    while time.time() - 开始 < ALIYUN_POLL_TIMEOUT:
        time.sleep(ALIYUN_POLL_INTERVAL)
        查询 = _阿里云请求(f"{ALIYUN_TASKS_PATH}/{任务号}", api_key)
        输出 = 查询.get("output") or {}
        状态 = str(输出.get("task_status") or "").upper()
        if 状态 in ("SUCCEEDED", "SUCCESS"):
            结果 = 输出
            break
        if 状态 in ("FAILED", "UNKNOWN"):
            说明 = _explain_aliyun(输出.get("code") or 状态,
                                  输出.get("message") or "任务失败")
            if 说明 == "NO_SPEECH":
                _记阿里云(True, "")
                raise NoSpeechError(NO_SPEECH_NOTE)
            raise RuntimeError(说明)
        已等 = int(time.time() - 开始)
        报("正在等阿里云转写", min(88, 62 + 已等 // 4), f"已等 {已等} 秒")
    else:
        raise RuntimeError(f"阿里云转写超过 {ALIYUN_POLL_TIMEOUT // 60} 分钟还没完成")

    段落: list[dict[str, Any]] = []
    for 子任务 in 结果.get("results") or []:
        链接 = str(子任务.get("transcription_url") or "")
        if not 链接:
            continue
        详情 = _阿里云下载结果(链接)
        for 轨 in 详情.get("transcripts") or []:
            for 句 in 轨.get("sentences") or []:
                文本 = str(句.get("text") or "").strip()
                if not 文本:
                    continue
                起 = 句.get("begin_time")
                止 = 句.get("end_time")
                段落.append({
                    "start": round(float(起) / 1000, 3) if isinstance(起, (int, float)) else 0.0,
                    "end": round(float(止) / 1000, 3) if isinstance(止, (int, float)) else 0.0,
                    "text": 文本,
                })
    if not 段落:
        _记阿里云(True, "")
        raise NoSpeechError(NO_SPEECH_NOTE)
    段落.sort(key=lambda x: x["start"])
    _记阿里云(True, "")
    return 段落


def _阿里云下载结果(链接: str) -> dict[str, Any]:
    """下载 transcription_url 指向的结果 JSON。"""
    请求 = urllib.request.Request(链接, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(请求, timeout=120) as 响应:
            return json.loads(响应.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"下载阿里云转写结果失败：{type(exc).__name__}") from exc


# ---- 火山转写 ----


def segments_to_text(segments: list[dict[str, Any]], with_time: bool = False) -> str:
    if not with_time:
        return "\n".join(str(segment.get("text") or "").strip() for segment in segments if segment.get("text"))
    lines = []
    for segment in segments:
        start = float(segment.get("start") or 0)
        minutes, seconds = divmod(int(start), 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {segment.get('text')}")
    return "\n".join(lines)


def transcribe_with_volcengine(audio: Path) -> list[dict[str, Any]]:
    """火山引擎大模型录音文件极速版。返回 segments（start/end 秒 + text）。"""
    api_key = volcengine_api_key()
    if not api_key:
        raise RuntimeError(
            "未配置火山引擎转写 Key（请设置环境变量 VOLCENGINE_ASR_API_KEY）"
        )
    if audio.stat().st_size > MAX_INLINE_AUDIO_BYTES:
        raise RuntimeError("音频超过 70MB，火山极速版不支持内嵌方式，已跳过转写")
    body = {
        "user": {"uid": api_key},
        "audio": {"data": base64.b64encode(audio.read_bytes()).decode("ascii")},
        "request": {"model_name": "bigmodel"},
    }
    request = urllib.request.Request(
        VOLC_ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": VOLC_RESOURCE_ID,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            status = response.headers.get("X-Api-Status-Code", "")
            message = response.headers.get("X-Api-Message", "")
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.headers.get("X-Api-Status-Code", str(exc.code)) if exc.headers else str(exc.code)
        raise RuntimeError(f"火山转写请求失败（状态 {status}）") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"火山转写请求失败：{type(exc).__name__}") from exc
    if status and status != "20000000":
        detail = re.sub(r"\s+", " ", message).strip()[:200]
        if status == "20000003":
            # 没语音不算资源问题，是内容问题
            _记火山(True, "")
            raise NoSpeechError(detail or NO_SPEECH_NOTE)
        if "not granted" in detail or status == "45000030":
            _记火山(False, "账号没有开通「大模型录音文件极速版」这个资源")
        else:
            _记火山(False, f"识别失败（状态 {status}）")
        raise RuntimeError(f"火山转写识别失败（状态 {status}{'：' + detail if detail else ''}）")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("火山转写返回了无法解析的结果") from exc
    if isinstance(payload, dict) and payload.get("code") not in {None, 0, "0"}:
        raise RuntimeError(f"火山转写识别失败（业务码 {payload.get('code')}）")

    result = payload.get("result") or {}
    segments: list[dict[str, Any]] = []
    for utterance in result.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        text = str(utterance.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(utterance.get("start_time") or 0) / 1000
            end = float(utterance.get("end_time") or 0) / 1000
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    if not segments:
        whole = str(result.get("text") or "").strip()
        if whole:
            segments = [{"start": 0.0, "end": 0.0, "text": whole}]
        else:
            _记火山(True, "")
            raise NoSpeechError("火山转写成功，但没有可用内容")
    # 走到这里说明火山这条线是通的，记下来给界面用
    _记火山(True, "")
    return segments


def extract_audio(video: Path, work_dir: Path) -> Path:
    if not have("ffmpeg"):
        raise RuntimeError("未安装 ffmpeg。请先执行：brew install ffmpeg")
    audio = work_dir / "audio.mp3"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio),
    ])
    if not audio.exists() or audio.stat().st_size == 0:
        raise RuntimeError("未能从视频中提取可用音频")
    return audio


def _local_whisper_python() -> Path | None:
    """找 faster-whisper 的 python 解释器。

    优先环境变量；其次复用旧自媒体系统已经初始化好的那套虚拟环境
    （用户机器上已装好且带模型，没必要再下一份 98MB）。
    """
    candidates = [
        os.environ.get("GLEAN_WHISPER_PYTHON", ""),
        str(Path.home() / ".glean/faster-whisper/bin/python"),
    ]
    for raw in candidates:
        if raw and Path(raw).is_file() and os.access(raw, os.X_OK):
            return Path(raw)
    return None


def local_whisper_available() -> bool:
    return _local_whisper_python() is not None


def transcribe_with_local(audio: Path, work_dir: Path, model: str = "") -> list[dict[str, Any]]:
    """本机 faster-whisper 转写（CPU）。慢、但不花钱、不依赖外部服务。"""
    python = _local_whisper_python()
    if not python:
        raise RuntimeError(
            "没有可用的本机转写环境。可用环境变量 GLEAN_WHISPER_PYTHON "
            "指定一个装了 faster-whisper 的 python。"
        )
    script = VENDOR_DIR / "transcribe_local.py"
    if not script.is_file():
        raise RuntimeError(f"本机转写脚本缺失：{script}")
    out = work_dir / "local-asr"
    out.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [str(python), str(script), str(audio), "--output-dir", str(out),
             "--language", "zh", "--model", model or os.environ.get("GLEAN_WHISPER_MODEL") or "small"],
            capture_output=True, text=True, timeout=3600,
        )
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"本机转写执行失败：{type(exc).__name__}") from exc
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        raise RuntimeError(f"本机转写失败：{lines[-1][:200] if lines else '未知错误'}")
    srt = out / "subtitle.srt"
    if not srt.is_file():
        raise RuntimeError("本机转写没有生成字幕文件")
    segments = parse_srt(srt.read_text(encoding="utf-8", errors="replace"))
    if not segments:
        raise NoSpeechError(NO_SPEECH_NOTE)
    return segments


def _volc_hint(message: str) -> str:
    """火山报错时给一句能直接照做的说明。"""
    if "45000030" in message or "not granted" in message:
        return ("火山账号没有开通「大模型录音文件极速版」这个资源（接口返回 not granted）。"
                "请到火山控制台开通或续费资源包；"
                "也可以现在就到设置里把「视频里说的话」换成「用本机模型」")
    return message


def transcribe_video(
    video: Path,
    work_dir: Path,
    *,
    engine: str = "volcengine",
    on_progress: ProgressFn | None = None,
    fallback_local: bool | None = None,
) -> dict[str, Any]:
    """转写一个视频文件，返回 SPEC 规定的 transcript 对象。

    engine: volcengine | local | off
    fallback_local: 火山不可用时是否自动改用本机模型。
    这不是静默降级——结果里会写明"这次用的是本机模型"，界面上也看得到。
    """
    def report(label: str, percent: int, detail: str = "") -> None:
        if on_progress:
            on_progress(label, percent, detail)

    if fallback_local is None:
        fallback_local = bool(load_config().get("transcribe_fallback_local", True))
    if not has_audio_stream(video):
        return {"source": "none", "text": "", "segments": [], "note": "视频没有音轨，没做转写"}
    if engine == "off":
        # 用户主动关的，不是降级——不给 note，否则每条都挂一句「已关闭转写」的警告，
        # 界面上一片黄条，反而把真正的警告淹了（SPEC：warnings 只记降级路径）。
        return {"source": "none", "text": "", "segments": [], "note": ""}

    report("正在提取音频", 30)
    try:
        audio = extract_audio(video, work_dir)
    except Exception as exc:  # noqa: BLE001
        return {"source": "none", "text": "", "segments": [], "note": f"音频提取失败：{exc}"}

    if engine == "aliyun":
        report("正在用阿里云转写", 50, "音频长的话会自动分段")
        try:
            segments = transcribe_with_aliyun(audio, work_dir)
        except NoSpeechError as exc:
            return {"source": "none", "text": "", "segments": [], "note": str(exc)}
        except Exception as exc:  # noqa: BLE001
            note = f"阿里云转写失败：{exc}"
            if fallback_local and local_whisper_available():
                report("改用本机模型转写", 60, "阿里云不可用，自动换成本机")
                try:
                    segments = transcribe_with_local(audio, work_dir)
                    return {"source": "local", "text": segments_to_text(segments),
                            "segments": segments,
                            "note": f"阿里云不可用，这次用的是本机模型（{note}）"}
                except NoSpeechError:
                    # 云端是真故障，但本机跑完发现其实没人说话——按「没说话」记，
                    # 不要写「也失败」，否则用户以为本机也坏了。
                    return {"source": "none", "text": "", "segments": [], "note": NO_SPEECH_NOTE}
                except Exception as local_exc:  # noqa: BLE001
                    return {"source": "none", "text": "", "segments": [],
                            "note": f"{note}；改用本机模型也失败：{local_exc}"}
            return {"source": "none", "text": "", "segments": [], "note": note}
        report("转写完成", 80, f"{len(segments)} 段")
        return {"source": "aliyun", "text": segments_to_text(segments), "segments": segments}

    if engine == "local":
        report("正在用本机模型转写", 50, "慢一些，但不用联网")
        try:
            segments = transcribe_with_local(audio, work_dir)
        except NoSpeechError as exc:
            return {"source": "none", "text": "", "segments": [], "note": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"source": "none", "text": "", "segments": [], "note": f"本机转写失败：{exc}"}
        report("转写完成", 80, f"{len(segments)} 段")
        return {"source": "local", "text": segments_to_text(segments), "segments": segments}

    report("正在转写语音", 55, "引擎：火山引擎")
    try:
        segments = transcribe_with_volcengine(audio)
    except NoSpeechError:
        # SPEC §7.1：不写「失败」，免得用户以为出故障了去重抓（重抓还是没语音）。
        return {"source": "none", "text": "", "segments": [],
                "note": NO_SPEECH_NOTE}
    except Exception as exc:  # noqa: BLE001
        note = _volc_hint(f"转写失败：{exc}")
        if fallback_local and local_whisper_available():
            report("改用本机模型转写", 60, "火山不可用，自动换成本机")
            try:
                segments = transcribe_with_local(audio, work_dir)
                return {
                    "source": "local",
                    "text": segments_to_text(segments),
                    "segments": segments,
                    "note": f"火山不可用，这次用的是本机模型（{note}）",
                }
            except NoSpeechError:
                # 同上：本机跑完发现没人说话，按「没说话」记，不写「也失败」。
                return {"source": "none", "text": "", "segments": [], "note": NO_SPEECH_NOTE}
            except Exception as local_exc:  # noqa: BLE001
                return {"source": "none", "text": "", "segments": [],
                        "note": f"{note}；改用本机模型也失败：{local_exc}"}
        return {"source": "none", "text": "", "segments": [], "note": note}

    report("转写完成", 80, f"{len(segments)} 段")
    return {"source": "volcengine", "text": segments_to_text(segments), "segments": segments}
