#!/usr/bin/env python3
"""适配器公共设施。

每个平台一个模块，统一出口：

    def fetch(resolved: Resolved, ctx: FetchContext) -> dict
        # 返回 SPEC §3 的 item 字典；id / fetched_at / source_api 由调用方补齐

适配器只负责"把平台数据翻译成统一结构"，不负责排队、落盘、进度展示。
所有降级都要走 ctx.warn(...)，不允许静默吞掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..resolver import PLATFORM_LABELS, Resolved

ProgressFn = Callable[[str, int, str], None]


@dataclass
class FetchContext:
    """一次抓取的上下文。"""

    options: dict[str, Any] = field(default_factory=dict)
    work_dir: Path = field(default_factory=lambda: Path("/tmp"))
    progress: ProgressFn | None = None
    raw_saver: Callable[[str, Any], str] | None = None
    media_saver: Callable[..., dict[str, Any]] | None = None

    # 记录
    warnings: list[str] = field(default_factory=list)
    source_api: list[str] = field(default_factory=list)
    # 适配器第一步必须 begin(item_id)：后续 raw/media 落盘都按这个目录走
    item_id: str = ""
    # 正在构建的 item。attach() 之后，每次 report() 都会把它快照推给前端，
    # 这样界面上内容才能"随进度一段段长出来"，而不是等全部结束才有东西看。
    item: dict[str, Any] | None = None
    publisher: Callable[[dict[str, Any]], None] | None = None

    def begin(self, item_id: str) -> str:
        self.item_id = item_id
        return item_id

    def attach(self, item: dict[str, Any]) -> dict[str, Any]:
        """把正在构建的 item 挂到上下文上（适配器建完 item 后立刻调用）。"""
        self.item = item
        self._publish()
        return item

    def publish(self) -> None:
        """手动推一次当前内容快照。

        适配器在"最后一次 report 之后"又填了内容时必须自己调一次，
        否则那段内容只会在整条抓完之后才出现。典型场景：转写结果是在
        report("正在转写语音") 之后才赋给 item 的。
        """
        self._publish()

    def _publish(self) -> None:
        if self.publisher and self.item is not None:
            try:
                self.publisher(self.item)
            except Exception:  # noqa: BLE001 - 推送失败绝不能影响抓取
                pass

    def report(self, label: str, percent: int, detail: str = "") -> None:
        if self.progress:
            self.progress(label, percent, detail)
        # 进度每次推进都顺带推一次当前内容快照，前端据此做渐进展示
        self._publish()

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def note_api(self, path: str) -> None:
        if path not in self.source_api:
            self.source_api.append(path)

    def save_raw(self, endpoint: str, payload: Any) -> None:
        if self.raw_saver:
            self.raw_saver(endpoint, payload)

    def save_media(self, url: str, stem: str, *, role: str = "content",
                   media_type: str = "image", referer: str = "") -> dict[str, Any]:
        if not self.media_saver:
            return {"role": role, "type": media_type, "path": "", "url": url,
                    "width": 0, "height": 0, "bytes": 0}
        return self.media_saver(url, stem, role=role, media_type=media_type, referer=referer)

    @property
    def want_media(self) -> bool:
        return bool(self.options.get("download_media", True))

    @property
    def want_transcribe(self) -> bool:
        return bool(self.options.get("transcribe", True))

    @property
    def limit(self) -> int:
        try:
            return int(self.options.get("limit") or 0)
        except (TypeError, ValueError):
            return 0


# ---- 时间与数值 ----


def iso_from_unix(value: Any, *, unit: str = "s") -> str:
    """新浪/抖音式时间戳 → ISO8601（带本机时区）。认不出来返回空串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if unit == "ms" or number > 1e11:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first_url(*addresses: Any) -> str:
    """从 TikHub 常见的 {url_list:[...]} / [url] / "url" 结构里取第一个可用地址。"""
    for address in addresses:
        if not address:
            continue
        if isinstance(address, str):
            if address.startswith("http"):
                return address
            continue
        if isinstance(address, dict):
            urls = address.get("url_list") or address.get("urls") or []
        elif isinstance(address, list):
            urls = address
        else:
            continue
        for url in urls:
            if isinstance(url, str) and url.startswith("http"):
                return url
    return ""


def all_urls(*addresses: Any) -> list[str]:
    rows: list[str] = []
    for address in addresses:
        if not address:
            continue
        if isinstance(address, str):
            if address.startswith("http") and address not in rows:
                rows.append(address)
            continue
        if isinstance(address, dict):
            urls = address.get("url_list") or address.get("urls") or []
        elif isinstance(address, list):
            urls = address
        else:
            continue
        for url in urls:
            if isinstance(url, str) and url.startswith("http") and url not in rows:
                rows.append(url)
    return rows


def strip_html(value: Any) -> str:
    """把 HTML 正文压成纯文本，保留段落。绝不用它做"改写"，只做标签剥离。"""
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|blockquote)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&mdash;", "—"), ("&hellip;", "…")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def short_id(value: str, keep: int = 72) -> str:
    """业务 ID 截断。

    上限放到 72：抖音 sec_uid 本身就约 55 字符，48 会把它截掉一截，
    虽然目录仍能用，但两个 sec_uid 前 48 位相同时会撞目录。
    """
    cleaned = re.sub(r"[^0-9A-Za-z_\-]+", "", value or "")
    return cleaned[:keep] if len(cleaned) > keep else cleaned


def make_item_id(platform: str, kind: str, key: str) -> str:
    """SPEC §2：<platform>-<kind>-<稳定业务ID>。"""
    return f"{platform}-{kind}-{short_id(key) or 'unknown'}"


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def empty_item(platform: str, kind: str, resolved: Resolved) -> dict[str, Any]:
    """给所有适配器一个字段齐全的起点，避免下游 KeyError。"""
    return {
        "id": "",
        "platform": platform,
        "platform_label": platform_label(platform),
        "kind": kind,
        "url": resolved.input_url,
        "canonical_url": resolved.url,
        "title": "",
        "text": "",
        "author": {"name": "", "handle": "", "id": "", "avatar": "", "url": "", "followers": 0},
        "created_at": "",
        "duration": None,
        "stats": {"like": 0, "comment": 0, "share": 0, "collect": 0, "play": 0},
        "transcript": {"source": "none", "text": "", "segments": []},
        "media": [],
        "children": [],
        "profile": {},
        "fetched_at": "",
        "source_api": [],
        "warnings": [],
    }


def normalized_media_candidates(candidates: list[str]) -> list[str]:
    """去掉重复与明显的占位地址，保持顺序。"""
    rows: list[str] = []
    for url in candidates:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        if url in rows:
            continue
        rows.append(url)
    return rows
