#!/usr/bin/env python3
"""任务队列。

一个任务 = 一次抓取。串行执行（TikHub 有并发限制，抢额度没意义），
但支持多个订阅者通过 SSE 实时看进度。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import store
from .fetcher import run_fetch
from .resolver import Resolved, describe, resolve
from .tikhub import TikHubError

STAGE_DEFINITION = [
    ("resolve", "识别链接"),
    ("fetch", "调用接口"),
    ("media", "下载媒体"),
    ("transcribe", "转写语音"),
    ("store", "写入本地库"),
]

# 把适配器报的中文进度文案映射到阶段。顺序即优先级。
STAGE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("store", ("写入", "入库", "保存", "已完成")),
    ("transcribe", ("转写", "字幕", "音频", "语音")),
    ("media", ("下载", "解密", "媒体", "封面")),
    ("fetch", ("接口", "资料", "列表", "解析", "读取", "拉取", "识别")),
]

TERMINAL_STATES = {"done", "partial", "failed"}


def _stage_for_label(label: str) -> str:
    for stage, keywords in STAGE_KEYWORDS:
        if any(keyword in label for keyword in keywords):
            return stage
    return "fetch"


class Job:
    """一个抓取任务。线程安全的进度更新 + 多订阅者广播。"""

    def __init__(
        self,
        url: str,
        options: dict[str, Any],
        resolved: Resolved | None = None,
        *,
        mode: str = "single",
        items: list[dict[str, Any]] | None = None,
        profile_id: str = "",
    ):
        self.id = f"j_{uuid.uuid4().hex[:12]}"
        self.url = url
        self.options = options
        self.resolved = resolved
        self.mode = mode  # single=单条抓取 / preview=只出主页预览 / batch=抓选中的作品
        self.items = items or []
        self.profile_id = profile_id
        self.state = "queued"
        self.title = ""
        self.item_id = ""
        self.error: str | None = None
        self.warnings: list[str] = []
        self.created_at = store.now_iso()
        self.started_at = ""
        self.finished_at = ""
        self.progress: dict[str, Any] = {"stage": "resolve", "label": "排队中", "percent": 0, "detail": ""}
        self.stages: list[dict[str, str]] = [
            {"key": key, "label": label, "state": "pending"} for key, label in STAGE_DEFINITION
        ]
        self.children_ok = 0
        self.children_failed = 0
        # 抓取过程中不断更新的内容快照，供前端"随进度慢慢跳出来"
        self.partial: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue] = []

    # ---- 渐进内容 ----

    def publish_partial(self, item: dict[str, Any]) -> None:
        """适配器每次拿到新东西都会调这里；只保留界面要用的字段，别把原始响应塞进 SSE。"""
        if not isinstance(item, dict):
            return
        children = item.get("children") or []
        media = item.get("media") or []
        transcript = item.get("transcript") or {}
        # 批次任务（用户从预览里挑的那几条）的 children 每条带 batch_state，
        # 界面要靠它逐条打勾。投影时必须原样保留，不能只留 children_preview 的摘要。
        is_batch = any(isinstance(row, dict) and row.get("batch_state") for row in children)
        with self._lock:
            self.partial = {
                "id": item.get("id") or "",
                "platform": item.get("platform") or "",
                "platform_label": item.get("platform_label") or "",
                "kind": item.get("kind") or "",
                "title": item.get("title") or "",
                "text": item.get("text") or "",
                "author": item.get("author") or {},
                "created_at": item.get("created_at") or "",
                "duration": item.get("duration"),
                "stats": item.get("stats") or {},
                "profile": item.get("profile") or {},
                "media": media[:60],
                "media_count": len(media),
                "transcript": {
                    "source": transcript.get("source") or "none",
                    "note": transcript.get("note") or "",
                    "segments": (transcript.get("segments") or [])[:400],
                    "text": transcript.get("text") or "",
                },
                "children_count": len(children),
                # 预览网格按请求的 limit 展示（由 API 限制在 200 以内），不能在快照再截断。
                "children_preview": children,
                # 批次模式额外原样带上逐条状态（含 batch_state / error / item_id）
                **({"children": children} if is_batch else {}),
                "warnings": list(self.warnings) + list(item.get("warnings") or []),
            }
        self._broadcast("progress")

    # ---- 订阅 ----

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _broadcast(self, event: str) -> None:
        payload = (event, self.to_dict())
        with self._lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                pass

    # ---- 进度 ----

    def set_stage(self, stage: str, state: str) -> None:
        with self._lock:
            for row in self.stages:
                if row["key"] == stage:
                    row["state"] = state
                elif state == "running" and row["state"] == "pending" and _index(row["key"]) < _index(stage):
                    # 进到后面的阶段说明前面的已经过了
                    row["state"] = "done"
            self._broadcast("progress")

    def report(self, label: str, percent: int, detail: str = "") -> None:
        stage = _stage_for_label(label)
        with self._lock:
            self.progress = {"stage": stage, "label": label, "percent": max(0, min(100, percent)), "detail": detail}
        self.set_stage(stage, "running")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "state": self.state,
                "mode": self.mode,
                "profile_id": self.profile_id,
                "platform": self.resolved.platform if self.resolved else "",
                "platform_label": self.resolved.platform_label if self.resolved else "",
                "kind": self.resolved.kind if self.resolved else "",
                "kind_label": self.resolved.kind_label if self.resolved else "",
                "title": self.title,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress": dict(self.progress),
                "stages": [dict(row) for row in self.stages],
                "item_id": self.item_id,
                "warnings": list(self.warnings),
                "error": self.error,
                "children_ok": self.children_ok,
                "children_failed": self.children_failed,
                "partial": dict(self.partial),
            }


def _index(stage_key: str) -> int:
    for position, (key, _) in enumerate(STAGE_DEFINITION):
        if key == stage_key:
            return position
    return 0


class JobManager:
    def __init__(self, max_workers: int = 2):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="glean")

    def _trim_history(self) -> None:
        """超过 50 条时只淘汰已结束任务；排队和运行中的任务必须保留。"""
        while len(self._order) > 50:
            stale = next((job_id for job_id in self._order
                          if self._jobs.get(job_id) is None
                          or self._jobs[job_id].state in TERMINAL_STATES), None)
            if stale is None:
                break
            self._order.remove(stale)
            self._jobs.pop(stale, None)

    def create(
        self,
        url: str,
        options: dict[str, Any] | None = None,
        *,
        mode: str = "single",
        items: list[dict[str, Any]] | None = None,
        profile_id: str = "",
    ) -> Job:
        options = dict(options or {})
        resolved: Resolved | None = None
        if mode == "single":
            try:
                resolved = resolve(url)
            except ValueError:
                resolved = None
        job = Job(url, options, resolved, mode=mode, items=items, profile_id=profile_id)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_history()
        self._pool.submit(self._run, job)
        return job

    def create_preview(self, url: str, options: dict[str, Any] | None = None) -> Job:
        """只出主页预览，不抓内容。"""
        options = dict(options or {})
        try:
            limit = int(options.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        options["limit"] = max(1, min(200, limit))
        resolved: Resolved | None = None
        try:
            resolved = resolve(url)
        except ValueError:
            resolved = None
        job = Job(url, options, resolved, mode="preview")
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_history()
        self._pool.submit(self._run, job)
        return job

    def create_batch(
        self,
        items: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        profile_id: str = "",
        url: str = "",
    ) -> Job:
        """抓用户从预览里挑中的作品。"""
        job = Job(url or f"batch:{len(items)}", dict(options or {}), None,
                  mode="batch", items=items, profile_id=profile_id)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_history()
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[job_id].to_dict() for job_id in ids if job_id in self._jobs]

    def _run(self, job: Job) -> None:
        job.state = "running"
        job.started_at = store.now_iso()
        with job._lock:
            job.stages[0]["state"] = "done"
        job.set_stage("fetch", "running")
        job._broadcast("progress")

        try:
            if job.mode == "preview":
                from .fetcher import run_preview

                resolved = job.resolved or resolve(job.url)
                job.resolved = resolved
                job.report("已识别链接", 6, describe(resolved))
                result = run_preview(resolved, job.options, job.report, job.publish_partial)
            elif job.mode == "batch":
                from .fetcher import run_batch

                job.report("开始抓取选中的作品", 4, f"共 {len(job.items)} 条")
                result = run_batch(job.items, job.options, job.report,
                                   job.publish_partial, job.profile_id)
            else:
                resolved = job.resolved or resolve(job.url)
                job.resolved = resolved
                job.report("已识别链接", 6, describe(resolved))
                result = run_fetch(resolved, job.options, job.report, job.publish_partial)

            job.item_id = result["item_id"]
            job.title = str(result["item"].get("title") or "")
            job.warnings = list(result.get("warnings") or [])
            job.children_ok = result.get("children_ok") or 0
            job.children_failed = result.get("children_failed") or 0

            for row in job.stages:
                row["state"] = "done"
            job.progress = {"stage": "store", "label": "已完成", "percent": 100,
                            "detail": f"已存入 {job.item_id}"}
            job.state = "partial" if job.warnings else "done"
            if job.mode == "preview" and job.options.get("account_id"):
                from .accounts import mark_synced

                mark_synced(str(job.options["account_id"]))
            job._broadcast("progress")
        except TikHubError as exc:
            self._fail(job, str(exc))
        except ValueError as exc:
            self._fail(job, f"链接无法识别：{exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成人话写进任务
            self._fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            job.finished_at = store.now_iso()
            job._broadcast("done" if job.state in ("done", "partial") else "error")

    def _fail(self, job: Job, message: str) -> None:
        job.state = "failed"
        job.error = message
        with job._lock:
            current = job.progress.get("stage") or "fetch"
            for row in job.stages:
                if row["key"] == current:
                    row["state"] = "failed"
        job.progress = {**job.progress, "label": "抓取失败", "detail": message[:160]}
        job._broadcast("progress")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def wait_for(job: Job, timeout: float = 600) -> None:
    """同步等待任务结束（脚本/测试用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in ("done", "partial", "failed"):
            return
        time.sleep(0.2)
