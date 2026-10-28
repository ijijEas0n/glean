#!/usr/bin/env python3
"""本地库读写。

设计原则：
- `items/<id>/` 是唯一真源；`index.json` / `index.md` 只是派生产物，随时可重建，不会漂移。
- 所有写入原子（先写 .tmp 再 os.replace），断电/中断不会留下半个文件。
- 目录结构以"agent 直接读"为第一优先级：content.md 是给人/agent 看的正文，
  item.json 是给程序看的结构化数据，raw/ 保留原始出处。
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import LIBRARY_ROOT

ITEMS_DIR = LIBRARY_ROOT / "items"
INDEX_JSON = LIBRARY_ROOT / "index.json"
INDEX_MD = LIBRARY_ROOT / "index.md"

_lock = threading.RLock()
_list_cache: dict[str, Any] = {"at": 0.0, "rows": []}
_LIST_TTL = 2.0

# 按 item_id 加锁：`save_item` / `mark_children_local` 都是「读-改-写」，
# 光靠单次写原子是不够的——两个 worker 同时处理同一条会**丢更新**。
# 用 RLock 是因为 mark_children_local 会调 save_item，非重入锁会死锁。
_item_locks: dict[str, threading.RLock] = {}
_item_locks_guard = threading.Lock()


def item_lock(item_id: str) -> threading.RLock:
    """取这条 item 的锁。同一个 item 的读-改-写在锁内完成。"""
    key = str(item_id or "")
    with _item_locks_guard:
        lock = _item_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _item_locks[key] = lock
        return lock

LIBRARY_AGENTS_MD = """# 这个库是什么

这是 **Glean（拾取）** 的本地内容库。每一条从互联网抓下来的内容占一个目录，
结构固定，可以直接按下面的规则读取，不需要先跑程序。

## 目录

```
library/
├── index.json          # 全库索引（轻量，列表用）
├── index.md            # 人可读的目录表
└── items/<item_id>/
    ├── item.json       # 结构化完整记录（元数据 + 正文 + 字幕分段 + 媒体清单）
    ├── content.md      # ★ 正文 / 字幕原文，agent 优先读这个
    ├── raw/            # 原始 API 返回（同一平台可能多个端点，文件名即端点名）
    └── media/          # 封面、图片、视频等实体文件
```

`item_id` 形如 `douyin-video-7587425737197145390`、`xiaohongshu-images-6a8ede3a...`。
同一链接重复抓取会覆盖同一目录，不会产生重复条目。

## 怎么读

- **想知道库里有什么**：读 `index.json`，每条含 id / platform / kind / title / author / fetched_at。
- **想读某条内容**：直接读 `items/<id>/content.md`，正文和字幕原文都在里面，带 YAML frontmatter。
- **想要结构化数据**（时间轴、互动数据、媒体清单）：读 `items/<id>/item.json`。
- **想追溯数据来源**：看 `items/<id>/raw/`，是 TikHub 接口的原样返回。

## 约定

- 抓下来的内容**原文不改写、不纠错**。字幕是机器转写的，可能有错别字。
- 取不到的字段留空（`""` / `null` / `[]`），**不会用猜测值填充**。
- `item.json` 的 `warnings` 字段记录降级情况（例如"无音轨，未转写"），
  `transcript.source` 说明字幕来自哪里：`platform`（平台自带）、`volcengine`（火山转写）、
  `local`（本地模型）、`none`（没有）。
- 视频文件默认在转写完成后按设置删除；`item.json.media` 里 `path` 为空表示文件已清理，
  但远程 `url` 仍然保留。
"""


# ---- 工具 ----


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_tmp_counter = itertools.count()


def atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写 .tmp 再 os.replace。

    tmp 名必须**唯一**。以前用固定的 `path.name + ".tmp"`，两个线程同时写同一个文件
    就会共用同一个 tmp、互相覆盖，`os.replace` 出来是坏文件（踩过）。
    这里加 pid + 线程 id + 自增序号三重保险。
    失败时清掉半截 tmp，别在库里留垃圾。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{next(_tmp_counter)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def safe_filename(value: str, fallback: str = "file", limit: int = 60) -> str:
    value = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", (value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:limit]


def item_dir(item_id: str) -> Path:
    return ITEMS_DIR / safe_filename(item_id, "item", 120)


def ensure_library() -> None:
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    agents = LIBRARY_ROOT / "AGENTS.md"
    if not agents.exists():
        atomic_write_text(agents, LIBRARY_AGENTS_MD)


# ---- 写入 ----


def save_raw(item_id: str, endpoint: str, payload: Any) -> str:
    """把原始接口返回存进 raw/。文件名 = 端点名。返回相对路径。"""
    name = safe_filename(endpoint.strip("/").replace("/", "_"), "response", 80) + ".json"
    target = item_dir(item_id) / "raw" / name
    atomic_write_json(target, payload)
    return f"raw/{name}"


def save_media(item_id: str, filename: str, content: bytes) -> str:
    name = safe_filename(filename, "file", 100)
    target = item_dir(item_id) / "media" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, target)
    return f"media/{name}"


def save_media_file(item_id: str, filename: str, source: Path) -> str:
    """把一个已经落在磁盘上的文件搬进媒体目录（流式，不整文件读进内存）。

    大视频必须走这里：save_media 收的是 bytes，调用方一旦写成 source.read_bytes()，
    几个 GB 的视频会直接进内存——实测 X 的长访谈能到 3.58GB，必爆。
    """
    name = safe_filename(filename, "file", 100)
    target = item_dir(item_id) / "media" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    # 用 copyfile 而不是 move：调用方后面还要对原文件跑 ffprobe 量时长
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return f"media/{name}"


def media_abs_path(item_id: str, relative: str) -> Path | None:
    """把 media[].path 解析成绝对路径，并防止路径穿越。"""
    base = item_dir(item_id).resolve()
    candidate = (base / relative).resolve()
    if not str(candidate).startswith(str(base)):
        return None
    return candidate if candidate.is_file() else None


# ---- content.md ----


def _yaml_scalar(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text.strip() != text:
        return '"' + text.replace('"', '\\"').replace("\n", " ") + '"'
    return text


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_content_md(item: dict[str, Any]) -> str:
    """按 SPEC §2.1 冻结格式渲染 content.md。"""
    transcript = item.get("transcript") or {}
    media = item.get("media") or []
    stats = item.get("stats") or {}
    author = item.get("author") or {}
    lines: list[str] = ["---"]
    lines.append(f"id: {_yaml_scalar(item.get('id'))}")
    lines.append(f"platform: {_yaml_scalar(item.get('platform'))}")
    lines.append(f"platform_label: {_yaml_scalar(item.get('platform_label'))}")
    lines.append(f"kind: {_yaml_scalar(item.get('kind'))}")
    lines.append(f"title: {_yaml_scalar(item.get('title'))}")
    lines.append(f"author: {_yaml_scalar(author.get('name'))}")
    lines.append(f"author_id: {_yaml_scalar(author.get('id'))}")
    lines.append(f"url: {_yaml_scalar(item.get('canonical_url') or item.get('url'))}")
    lines.append(f"created_at: {_yaml_scalar(item.get('created_at'))}")
    lines.append(f"fetched_at: {_yaml_scalar(item.get('fetched_at'))}")
    if item.get("duration") is not None:
        lines.append(f"duration: {_yaml_scalar(item.get('duration'))}")
    stat_pairs = ", ".join(f"{k}: {v}" for k, v in stats.items() if isinstance(v, (int, float)) and v)
    lines.append(f"stats: {{{stat_pairs}}}")
    lines.append(f"transcript_source: {_yaml_scalar(transcript.get('source') or 'none')}")
    lines.append(f"media_count: {len(media)}")
    if item.get("tags"):
        lines.append("tags: [" + ", ".join(_yaml_scalar(t) for t in item["tags"]) + "]")
    lines.append("---")
    lines.append("")
    # 标题取不到就**不写这一行**（SPEC §3「取不到就留空，绝不猜测」）。
    # 以前填「（无标题）」——agent 读到的就是一句废话，还会被当成真标题。
    # frontmatter 里的 title: 留空才是事实，下面的作者/时间/链接行够定位这条。
    title = str(item.get("title") or "").strip()
    if title:
        lines.append(f"# {title}")
        lines.append("")

    meta_bits = [f"作者：{author.get('name') or '未知'}"]
    if item.get("created_at"):
        meta_bits.append(str(item["created_at"]))
    if item.get("duration"):
        meta_bits.append(f"时长 {_clock(item['duration'])}")
    lines.append("> " + " · ".join(meta_bits))
    lines.append(f"> 原链接：{item.get('canonical_url') or item.get('url') or ''}")
    lines.append("")

    if item.get("kind") == "profile":
        profile = item.get("profile") or {}
        if profile.get("signature"):
            lines.append("## 博主简介")
            lines.append("")
            lines.append(str(profile["signature"]).strip())
            lines.append("")
        children = item.get("children") or []
        lines.append(f"## 作品清单（{len(children)} 条）")
        lines.append("")
        lines.append("| # | 标题 | 类型 | 时长 | 点赞 | 发布 | 本地 |")
        lines.append("|---|------|------|------|------|------|------|")
        for index, child in enumerate(children, start=1):
            child_title = str(child.get("title") or "").replace("|", "／")[:60]
            stats_child = child.get("stats") or {}
            duration = _clock(child["duration"]) if child.get("duration") else "-"
            published = str(child.get("created_at") or "")[:10] or "-"
            local = "✅" if child.get("local") else "—"
            link = f"[{child_title}](items/{child['id']}/content.md)" if child.get("local") else child_title
            lines.append(
                f"| {index} | {link} | {child.get('kind') or ''} | {duration} | "
                f"{stats_child.get('like') or '-'} | {published} | {local} |"
            )
        lines.append("")
    else:
        text = str(item.get("text") or "").strip()
        if text:
            lines.append("## 正文")
            lines.append("")
            lines.append(text)
            lines.append("")
        # 标签单独成节，别和正文糊在一起（原串在 item.json 的 text_raw 里，一字未改）
        tags = item.get("tags") or []
        if tags:
            lines.append("## 标签")
            lines.append("")
            lines.append(" ".join(f"#{t}" for t in tags))
            lines.append("")

        segments = [segment for segment in transcript.get("segments") or []
                    if isinstance(segment, dict) and str(segment.get("text") or "").strip()]
        transcript_text = str(transcript.get("text") or "").strip()
        if segments:
            lines.append("## 字幕原文")
            lines.append("")
            for segment in segments:
                stamp = _clock(segment.get("start") or 0)
                content = str(segment.get("text") or "").strip()
                if content:
                    lines.append(f"[{stamp}] {content}")
            lines.append("")
        elif transcript_text:
            lines.append("## 字幕原文")
            lines.append("")
            lines.append(transcript_text)
            lines.append("")

    if media:
        lines.append("## 媒体")
        lines.append("")
        for entry in media:
            path = entry.get("path") or ""
            role = entry.get("role") or ""
            note = f"（{role}）" if role else ""
            if path:
                lines.append(f"- {path}{note}")
            elif entry.get("url"):
                lines.append(f"- {entry['url']}{note}（未下载）")
        lines.append("")

    warnings = item.get("warnings") or []
    if warnings:
        lines.append("## 抓取说明")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def preserve_transcript(item: dict[str, Any]) -> dict[str, Any]:
    """重抓时别把已经转好的字幕弄丢。

    踩过的坑：同一条视频重新抓一次，正好碰上火山转写临时限流（状态 45000030），
    新结果是空的，直接把上次转好的 53 段字幕覆盖没了。
    转写是"花了钱、花了时间"的成果，临时故障不该销毁它。
    所以：这次没转出来，但磁盘上已有字幕 → 保留旧的，并如实写明这次为什么没转。
    """
    item_id = str(item.get("id") or "")
    if not item_id:
        return item
    new_transcript = item.get("transcript") or {}
    if new_transcript.get("segments") or new_transcript.get("text"):
        return item  # 这次转出来了，用新的
    existing = load_item(item_id)
    if not existing:
        return item
    old = existing.get("transcript") or {}
    if not (old.get("segments") or old.get("text")):
        return item  # 上次也没有，没什么可保的
    note = str(new_transcript.get("note") or "").strip()
    if "已关闭转写" in note:
        原因 = "这次设置了不转写"
    elif "跳过转写" in note:
        原因 = "这次没下载视频，没法转写"
    elif note:
        原因 = f"这次没转成功（{note}）"
    else:
        原因 = "这次没转出文字"
    item["transcript"] = {
        **old,
        "note": f"{原因}，显示的是上一次转好的文字",
    }
    warnings = list(item.get("warnings") or [])
    warnings.append(f"{原因}，已保留上一次转好的文字稿")
    item["warnings"] = warnings
    return item


def preserve_media(item: dict[str, Any]) -> dict[str, Any]:
    """重抓时别把已经下载好的媒体路径弄丢。

    踩过的坑：带 `download_media: false` 重抓一次（只想刷新元数据），
    新结果的 `media[].path` 全是空的，直接覆盖上去——可 7MB 的视频文件
    好好地躺在磁盘上。于是**记录开始说谎**：界面显示没视频，
    agent 读 item.json 也以为没有，实际文件就在那儿。

    规则：文件还在磁盘上，路径就要照实记着。这跟 preserve_transcript 不同，
    不写 warning——记录符合事实是本分，不是降级，没必要每次都弹一条黄条
    （否则用户一开「不下媒体」就满屏警告，把真正的警告淹了）。
    """
    item_id = str(item.get("id") or "")
    if not item_id:
        return item
    new_media = [m for m in (item.get("media") or []) if isinstance(m, dict)]
    if all(str(m.get("path") or "").strip() for m in new_media):
        return item          # 这次都下到了，用新的
    existing = load_item(item_id)
    if not existing:
        return item
    旧: dict[tuple[str, str], dict[str, Any]] = {}
    for m in existing.get("media") or []:
        if isinstance(m, dict) and str(m.get("path") or "").strip():
            旧[(str(m.get("role") or ""), str(m.get("type") or ""))] = m
    if not 旧:
        return item
    directory = item_dir(item_id)

    def 磁盘上还在(m: dict[str, Any]) -> str:
        老路径 = str(m.get("path") or "")
        return 老路径 if 老路径 and (directory / 老路径).is_file() else ""

    # 情况一：新记录有这条媒体，但这次没下到（path 空）→ 把旧路径接回来
    for m in new_media:
        if m.get("removed"):
            continue
        if str(m.get("path") or "").strip():
            continue
        old = 旧.get((str(m.get("role") or ""), str(m.get("type") or "")))
        if not old or not 磁盘上还在(old):
            continue
        for key in ("path", "width", "height", "bytes", "duration", "source_format"):
            if old.get(key) not in (None, ""):
                m[key] = old[key]

    # 情况二：新记录连这条媒体都没有（比如 download_media:false 时封面压根没生成条目），
    # 但文件还在磁盘上 → 补回来。否则封面会凭空消失，记录和磁盘对不上（踩过）。
    现有 = {(str(m.get("role") or ""), str(m.get("type") or "")) for m in new_media}
    for key, old in 旧.items():
        if key in 现有 or not 磁盘上还在(old):
            continue
        补 = {k: v for k, v in old.items() if v not in (None, "")}
        item.setdefault("media", []).append(补)
    return item


def _same_source_id(item: dict[str, Any]) -> str:
    """库里已经有同一条链接的话，返回它现有的 id，别新建一条。

    这是去重保险：item_id 是拿业务 ID 拼出来的，一旦生成规则变过
    （比如截断长度调整），同一条内容就会落到两个目录里，用户会看到两份。
    与其要求生成规则永远不变，不如在落盘前按 canonical_url 认一下。
    """
    url = str(item.get("canonical_url") or item.get("url") or "").strip()
    if not url:
        return ""
    platform = str(item.get("platform") or "")
    for directory in ITEMS_DIR.iterdir() if ITEMS_DIR.is_dir() else []:
        if not directory.is_dir():
            continue
        if directory.name == str(item.get("id") or ""):
            return ""      # 就是它自己
        try:
            other = json.loads((directory / "item.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(other, dict):
            continue
        if str(other.get("platform") or "") != platform:
            continue
        if str(other.get("canonical_url") or other.get("url") or "").strip() == url:
            return directory.name
    return ""


def find_same_work(platform: str, business_id: str, urls: Iterable[str] = ()) -> str:
    """按**业务 ID** 找同一条作品已经落在哪个目录；找不到返回 ""。

    判重必须比业务 ID，不能比链接字符串（踩过）：同一条作品用户可能从
    `?modal_id=<id>`、`/video/<id>`、短链、App 分享文案四种形态粘进来，
    存下来的 `url` 是"用户粘的那条"、`canonical_url` 是规范链，两者对不上，
    字符串全等就会判成"没拿过"然后重复抓一遍——白花钱还多等几分钟。

    `business_id` = aweme_id / note_id 这类平台内稳定 ID，item_id 尾段就是它。
    """
    biz = str(business_id or "").strip()
    if not biz:
        return ""
    platform = str(platform or "").strip()
    wanted = {str(u).strip() for u in urls if u and str(u).strip()}
    for directory in ITEMS_DIR.iterdir() if ITEMS_DIR.is_dir() else []:
        if not directory.is_dir():
            continue
        name = directory.name
        if platform and not name.startswith(f"{platform}-"):
            continue
        # item_id 尾段就是业务 ID（item_id = <platform>-<kind>-<业务ID>）
        if name == biz or name.endswith(f"-{biz}"):
            return name
        if not wanted:
            continue
        try:
            other = json.loads((directory / "item.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(other, dict):
            continue
        for key in ("canonical_url", "url"):
            if str(other.get(key) or "").strip() in wanted:
                return name
    return ""


def _srt_time(seconds: Any) -> str:
    """SRT 的时间格式：00:00:01,234"""
    try:
        total_ms = max(0, int(round(float(seconds or 0) * 1000)))
    except (TypeError, ValueError):
        total_ms = 0
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    sec, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def render_srt(item: dict[str, Any]) -> str:
    """把字幕分段渲染成标准 SRT。

    为什么要单独出这个文件：`content.md` 是给人/agent 读的，但很多工具
    （剪辑软件、播放器、其他 agent 流程）认的是 SRT。多写一份几乎不花成本，
    引用起来却方便很多。
    """
    segments = (item.get("transcript") or {}).get("segments") or []
    if not segments:
        return ""
    blocks: list[str] = []
    for index, seg in enumerate(segments, start=1):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = _srt_time(seg.get("start"))
        end = _srt_time(seg.get("end")) if seg.get("end") else _srt_time(
            (float(seg.get("start") or 0) + 3.0))
        blocks.append(f"{index}\n{start} --> {end}\n{text}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def save_item(item: dict[str, Any]) -> str:
    """落盘一条 item（item.json + content.md）并刷新索引。返回 item_id。

    整个「读-改-写」必须在 item 锁内完成：单次写原子还不够，
    两个 worker 同时抓同一条时（重复粘同一链接就会发生）会丢更新。
    """
    with item_lock(str(item.get("id") or "")):
        return _save_item(item)


def _save_item(item: dict[str, Any]) -> str:
    ensure_library()
    已有 = _same_source_id(item)
    if 已有:
        item["id"] = 已有
    # 同一作品重抓时合并归属：收藏分栏按旧后新保序去重；favorites 只升级，
    # 普通 library 抓取不能把已有收藏关系降级掉。
    旧条目 = load_item(str(item.get("id") or ""))
    if 旧条目:
        旧来源 = item_source(旧条目)
        新来源 = item_source(item)
        item["source"] = "favorites" if 旧来源 == "favorites" and 新来源 == "library" else 新来源
        item["collections"] = list(dict.fromkeys(
            item_collections(旧条目) + item_collections(item)
        ))[:20]
    # children 的 local/has_transcript **一律按磁盘算**，不从上一份条目继承。
    # 继承那套有个洞：只抓过这个博主的某几条、但从没抓过他的主页时，
    # 新预览会把这几条全标成"没拿过"，用户就重复抓（踩过，正好是
    # 「从视频点进博主主页再挑几条」这条链路的关键一步）。
    _sync_child_locals(item)
    preserve_transcript(item)
    preserve_media(item)
    item_id = str(item["id"])
    directory = item_dir(item_id)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(directory / "item.json", item)
    atomic_write_text(directory / "content.md", render_content_md(item))
    # 有字幕就顺带出一份标准 SRT；没有就把旧的清掉（避免留下过期的）
    srt_path = directory / "transcript.srt"
    srt = render_srt(item)
    if srt:
        atomic_write_text(srt_path, srt)
    else:
        try:
            srt_path.unlink(missing_ok=True)
        except OSError:
            pass
    invalidate_list_cache()
    return item_id


# ---- 读取 ----


def child_item_id(child: dict[str, Any], platform: str = "") -> str:
    """按**业务 ID** 找这条作品实际落在哪个目录名；找不到就回 child 自己的 id。

    为什么不能直接拿 `child.id` 当目录名：item_id 里含「类型」这种**派生信息**——
    列表接口说"不是 video 就算 images"，详情接口才认得出 text。于是小红书的纯文字
    笔记在主页清单里是 `xiaohongshu-images-<id>`，落盘却是 `xiaohongshu-text-<id>`，
    永远对不上（踩过）：界面上明明抓过还显示"没拿"，用户就重复抓一遍。

    所以判"在不在本地"一律按 `biz_id` 匹配，不比 id 字符串。
    """
    cid = str(child.get("item_id") or child.get("id") or "")
    biz = str(child.get("biz_id") or "").strip()
    if not biz and cid.count("-") >= 2:
        biz = cid.split("-", 2)[-1]        # 兜底：item_id 尾段就是业务 ID
    if not biz:
        return cid
    plat = platform or (cid.split("-", 1)[0] if "-" in cid else "")
    if not plat:
        return cid
    前缀 = f"{plat}-"
    尾段 = f"-{biz}"
    for directory in ITEMS_DIR.iterdir() if ITEMS_DIR.is_dir() else []:
        if not directory.is_dir():
            continue
        name = directory.name
        if name.startswith(前缀) and name.endswith(尾段) and (directory / "item.json").is_file():
            return name
    return cid


def _sync_child_locals(item: dict[str, Any]) -> dict[str, Any]:
    """主页清单里的"这条在不在本地"，**以磁盘为准**，不看存下来的标记。

    存下来的 local 会失真：条目被删了标记还在、从别处单独抓的又不会补上标记。
    磁盘上有没有这个目录是唯一的事实，按它算，界面就永远不会说谎。
    """
    children = item.get("children")
    if not children:
        return item
    platform = str(item.get("platform") or "")
    for child in children:
        if not isinstance(child, dict):
            continue
        # 按业务 ID 解析真实目录，别信 child.id（类型判定两边可能不一致）
        child_id = child_item_id(child, platform)
        目录 = item_dir(child_id) if child_id else None
        在 = bool(目录) and (目录 / "item.json").is_file()
        child["local"] = 在
        # 有没有字幕也按磁盘算。界面要拿它决定"复制字幕路径"能不能点——
        # 只看 local 的话，纯音乐视频（拿了但没字幕）会被算成有字幕，
        # 复制出来就是一条指向不存在文件的路径。
        child["has_transcript"] = bool(在 and 目录 and (目录 / "transcript.srt").is_file())
        if 在:
            child["item_id"] = child_id   # 记下真实目录名，界面可直接用
        else:
            child.pop("item_id", None)
    return item


def load_item(item_id: str) -> dict[str, Any] | None:
    path = item_dir(item_id) / "item.json"
    if not path.is_file():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _sync_child_locals(item) if isinstance(item, dict) else None


def load_content(item_id: str) -> str | None:
    path = item_dir(item_id) / "content.md"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def invalidate_list_cache() -> None:
    with _lock:
        _list_cache["at"] = 0.0


def _cover_for(item_id: str, item: dict[str, Any]) -> str:
    for entry in item.get("media") or []:
        if entry.get("role") == "cover" and entry.get("path"):
            return f"/api/media/{item_id}/{Path(entry['path']).name}"
    for entry in item.get("media") or []:
        if entry.get("path") and entry.get("type") == "image":
            return f"/api/media/{item_id}/{Path(entry['path']).name}"
    return ""


SOURCES = ("library", "favorites")
# 「未分类」是分栏里的兜底分组名，不是真实收藏夹名（真实夹名不许占用它）
未分类 = "未分类"


def item_source(item: dict[str, Any]) -> str:
    """条目来源。老条目没有这个字段，一律当作「我拿过的」，不需要迁移。"""
    value = str(item.get("source") or "").strip()
    return value if value in SOURCES else "library"


def item_collections(item: dict[str, Any]) -> list[str]:
    """所属收藏夹（分栏）。老条目没有这个字段，一律视为未分类。"""
    value = item.get("collections")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for raw in value:
        name = str(raw or "").strip()
        if name and name != 未分类 and name not in out:
            out.append(name)
    return out


def _summary(item_id: str, item: dict[str, Any]) -> dict[str, Any]:
    transcript = item.get("transcript") or {}
    text = str(item.get("text") or "").replace("\n", " ").strip()
    author = item.get("author") or {}
    return {
        "id": item_id,
        "platform": item.get("platform"),
        "platform_label": item.get("platform_label"),
        "kind": item.get("kind"),
        "source": item_source(item),
        "collections": item_collections(item),
        # 标题空着交给界面兜底（平台 · 作者 · 时长），这里不填占位——
        # 「（无标题）」在列表里比空白更难认（SPEC §3「取不到就留空」）。
        "title": str(item.get("title") or ""),
        "author": author.get("name") or "",
        "author_id": str(author.get("id") or ""),
        "author_avatar": author.get("avatar") or "",
        "duration": item.get("duration"),
        "created_at": item.get("created_at") or "",
        "fetched_at": item.get("fetched_at") or "",
        "cover": _cover_for(item_id, item),
        "stats": item.get("stats") or {},
        "has_transcript": bool((transcript.get("segments") or transcript.get("text"))),
        "transcript_source": transcript.get("source") or "none",
        "child_count": len(item.get("children") or []),
        "media_count": len(item.get("media") or []),
        "excerpt": text[:120],
        "url": item.get("canonical_url") or item.get("url") or "",
        # 全文检索用的拼接串。以前只搜 excerpt（正文前 120 字），
        # 正文第 121 字之后就搜不到了（踩过：1200 字的文章只搜得到开头）。
        # 这个字段只在内存里做匹配，返回给前端前会被 pop 掉。
        "_search": " ".join(filter(None, [
            str(item.get("title") or ""),
            str(author.get("name") or ""),
            str(author.get("handle") or ""),
            text,
            " ".join(str(t) for t in (item.get("tags") or [])),
            str((transcript.get("text") or "")),
            " ".join(str(c.get("title") or "") for c in (item.get("children") or [])
                     if isinstance(c, dict)),
        ])).lower(),
    }


def _scan_rows() -> list[dict[str, Any]]:
    """扫 items/ 目录得到列表行。带 2 秒缓存，避免每次请求都全盘扫描。"""
    with _lock:
        if _list_cache["at"] and time.time() - _list_cache["at"] < _LIST_TTL:
            return list(_list_cache["rows"])
    rows: list[dict[str, Any]] = []
    if ITEMS_DIR.is_dir():
        for directory in ITEMS_DIR.iterdir():
            if not directory.is_dir():
                continue
            item = load_item(directory.name)
            if item:
                rows.append(_summary(directory.name, item))
    rows.sort(key=lambda row: str(row.get("fetched_at") or ""), reverse=True)
    with _lock:
        _list_cache["at"] = time.time()
        _list_cache["rows"] = rows
    return list(rows)


def list_items(
    *,
    platform: str = "",
    kind: str = "",
    exclude_kind: str = "",
    source: str = "",
    collection: str = "",
    query: str = "",
    author: str = "",
    author_id: str = "",
    missing_transcript: bool = False,
    limit: int = 50,
    offset: int = 0,
    order: str = "fetched_at_desc",
) -> tuple[int, list[dict[str, Any]]]:
    rows = _scan_rows()
    if platform:
        rows = [row for row in rows if row["platform"] == platform]
    if kind:
        rows = [row for row in rows if row["kind"] == kind]
    if exclude_kind:
        rows = [row for row in rows if row.get("kind") != exclude_kind]
    if source:
        rows = [row for row in rows if (row.get("source") or "library") == source]
    if collection:
        if collection == 未分类:
            rows = [row for row in rows if not (row.get("collections") or [])]
        else:
            rows = [row for row in rows if collection in (row.get("collections") or [])]
    if author_id:
        rows = [row for row in rows if row.get("kind") != "profile"
                and str(row.get("author_id") or "") == author_id]
    elif author:
        rows = [row for row in rows if str(row.get("author") or "") == author]
    if missing_transcript:
        rows = [row for row in rows if row.get("kind") == "video" and not row.get("has_transcript")]
    if query:
        # 走全文检索串（标题/作者/正文全文/标签/字幕/子条目标题），
        # 不再只搜正文前 120 字。
        needle = query.lower()
        rows = [row for row in rows if needle in str(row.get("_search") or "")]
    if order == "created_at_desc":
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    elif order == "title":
        rows.sort(key=lambda row: str(row.get("title") or ""))
    total = len(rows)
    page = rows[offset: offset + max(1, limit)]
    # `_search` 是内部检索串（含正文全文，可能很大），不给前端；
    # 一定要复制一份再剥——`_scan_rows` 给的是缓存里字典的引用，直接 pop 会改坏缓存。
    return total, [{k: v for k, v in row.items() if k != "_search"} for row in page]


def platform_counts(source: str = "") -> dict[str, int]:
    """按平台计数。传 source 时只数那个栏目（两个栏目各自的平台筛选都靠它）。"""
    counts: dict[str, int] = {}
    for row in _scan_rows():
        if source and (row.get("source") or "library") != source:
            continue
        key = str(row.get("platform") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def source_counts() -> dict[str, int]:
    """两个栏目各多少条。不受任何筛选影响，给栏目标签上的数字用。"""
    counts = {source: 0 for source in SOURCES}
    for row in _scan_rows():
        counts[row.get("source") or "library"] = counts.get(row.get("source") or "library", 0) + 1
    return counts


def author_names(source: str = "") -> list[str]:
    """作者筛选选项使用全量索引，避免只显示当前分页的作者。"""
    names = {
        str(row.get("author") or "").strip()
        for row in _scan_rows()
        if row.get("kind") != "profile"
        and (not source or (row.get("source") or "library") == source)
        and str(row.get("author") or "").strip()
    }
    return sorted(names, key=str.casefold)


def author_counts(source: str = "") -> list[dict[str, Any]]:
    """按稳定作者 ID 统计非主页内容，供内容库快捷筛选。"""
    counts: dict[str, dict[str, Any]] = {}
    for row in _scan_rows():
        author_id = str(row.get("author_id") or "").strip()
        name = str(row.get("author") or "").strip()
        if row.get("kind") == "profile" or not author_id or not name:
            continue
        # 来源为空时与“全部”页签一致；favorites 则只统计收藏夹中的条目。
        if source and (row.get("source") or "library") != source:
            continue
        entry = counts.setdefault(author_id, {"id": author_id, "name": name, "count": 0})
        entry["count"] += 1
    return sorted(counts.values(), key=lambda x: (-x["count"], str(x["name"]).casefold(), x["id"]))


def content_count(source: str = "") -> int:
    return sum(1 for row in _scan_rows()
               if row.get("kind") != "profile"
               and (not source or (row.get("source") or "library") == source))


def collection_counts(source: str = "favorites") -> dict[str, int]:
    """收藏夹分栏计数：{夹名: 条数, "未分类": 条数}。

    一条内容可以同时属于多个夹，所以各夹之和可能大于总数——这是对的，如实呈现。
    """
    counts: dict[str, int] = {}
    for row in _scan_rows():
        if source and (row.get("source") or "library") != source:
            continue
        names = row.get("collections") or []
        if not names:
            counts[未分类] = counts.get(未分类, 0) + 1
            continue
        for name in names:
            counts[str(name)] = counts.get(str(name), 0) + 1
    return counts


def delete_item(item_id: str) -> bool:
    directory = item_dir(item_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory)
    invalidate_list_cache()
    return True


def mark_children_local(profile_id: str, item_ids: Iterable[str]) -> int:
    """把主页 item 里对应作品的 local 标记为 true。

    用户在预览里挑了几条、这几条抓完之后，主页清单要如实显示"哪些已经在本地了"。
    返回被标记的条数。

    实际的"在不在本地"一律由 `_sync_child_locals` **按磁盘重算**，
    这里不看传进来的 id 有没有对上 child.id——类型判定在不同接口可能不一致，
    比字符串会漏（踩过）。这里只负责触发一次重算并落盘。
    """
    # 读-改-写要整体加锁，否则两个 worker 同时回写同一份主页会丢更新
    with item_lock(str(profile_id)):
        return _mark_children_local(profile_id, item_ids)


def _mark_children_local(profile_id: str, item_ids: Iterable[str]) -> int:
    item = load_item(profile_id)
    if not item:
        return 0
    wanted = {str(value) for value in item_ids if value}
    if not wanted:
        return 0
    _sync_child_locals(item)
    marked = 0
    for child in item.get("children") or []:
        if not isinstance(child, dict) or not child.get("local"):
            continue
        if str(child.get("id") or "") in wanted or str(child.get("item_id") or "") in wanted:
            marked += 1
    if marked:
        save_item(item)
    return marked


def patch_item_stats(item_id: str, stats_patch: dict[str, Any]) -> bool:
    """就地补一条 item 的 stats（比如播放量），不动别的字段。

    批量抓完后一次补播放量会用到：抓的时候拿不到播放量（列表/详情接口不给），
    只能事后拿一批 ID 去批量接口补回来。整条重写风险大，所以只动 stats。
    """
    if not item_id or not stats_patch:
        return False
    with item_lock(str(item_id)):
        item = load_item(item_id)
        if not item:
            return False
        stats = dict(item.get("stats") or {})
        for key, value in stats_patch.items():
            if value is not None:
                stats[key] = value
        if stats == (item.get("stats") or {}):
            return True
        item["stats"] = stats
        _save_item(item)
        return True


def children_of(profile_id: str) -> list[dict[str, Any]]:
    item = load_item(profile_id)
    if not item:
        return []
    return [child for child in (item.get("children") or []) if isinstance(child, dict)]


def rebuild_index() -> dict[str, Any]:
    """重建 index.json / index.md（派生产物，可随时重建）。"""
    rows = _scan_rows()
    payload = {
        "generated_at": now_iso(),
        "total": len(rows),
        "by_platform": platform_counts(),
        "items": rows,
    }
    atomic_write_json(INDEX_JSON, payload)

    lines = [
        "# Glean 本地库目录",
        "",
        f"共 {len(rows)} 条 · 生成于 {payload['generated_at']}",
        "",
        "| 平台 | 类型 | 标题 | 作者 | 抓取时间 | 目录 |",
        "|------|------|------|------|----------|------|",
    ]
    for row in rows:
        title = str(row.get("title") or "").replace("|", "／")[:56]
        lines.append(
            f"| {row.get('platform_label') or ''} | {row.get('kind') or ''} | {title} | "
            f"{str(row.get('author') or '').replace('|', '／')[:20]} | "
            f"{str(row.get('fetched_at') or '')[:16].replace('T', ' ')} | "
            f"[{row['id']}](items/{row['id']}/content.md) |"
        )
    lines.append("")
    atomic_write_text(INDEX_MD, "\n".join(lines))
    return payload
