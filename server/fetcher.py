#!/usr/bin/env python3
"""抓取编排：识别结果 → 适配器 → 落盘。

适配器只管把平台数据翻译成统一结构；排队、进度、落盘、主页批量展开都在这里。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import store
from .adapters import get_adapter
from .adapters.base import FetchContext
from .resolver import Resolved
from .textfmt import split_tags
from .tikhub import TikHubError

ProgressFn = Callable[[str, int, str], None]

# 主页批量展开时，单条作品的进度区间（百分比）
CHILD_PROGRESS_START = 42
CHILD_PROGRESS_END = 92


def _build_context(
    options: dict[str, Any],
    work_dir: Path,
    progress: ProgressFn | None,
    on_partial: ProgressFn | None = None,
) -> FetchContext:
    """建上下文，并把 raw/media 的落盘回调绑到 ctx.item_id 上。

    回调是闭包，读的是 ctx.item_id 的**当前值**——所以适配器什么时候调用
    ctx.begin(item_id)，之后的写入就自动进哪个目录，不需要把 id 传进来。
    """
    ctx = FetchContext(options=options, work_dir=work_dir, progress=progress,
                       publisher=on_partial)

    def raw_saver(endpoint: str, payload: Any) -> str:
        if not ctx.item_id:
            return ""
        return store.save_raw(ctx.item_id, endpoint, payload)

    def media_saver(url: str, stem: str, *, role: str = "content",
                    media_type: str = "image", referer: str = "") -> dict[str, Any]:
        if not ctx.item_id:
            return {"role": role, "type": media_type, "path": "", "url": url,
                    "width": 0, "height": 0, "bytes": 0}
        from .media import save_remote_media

        return save_remote_media(ctx.item_id, url, stem, role=role,
                                 media_type=media_type, referer=referer,
                                 on_warn=ctx.warn)

    ctx.raw_saver = raw_saver
    ctx.media_saver = media_saver
    return ctx


def _split_text_and_tags(item: dict[str, Any]) -> dict[str, Any]:
    """把正文和话题标签拆开，界面/文档里才不至于糊成一团。

    原文一个字都不丢：拆过的话，原串原样存进 text_raw，text 放拆干净的正文。
    长文（文章类）不拆——那里面的 # 可能真有别的意思。
    """
    if str(item.get("kind") or "") == "article":
        return item
    raw = str(item.get("text") or "")
    if not raw or "#" not in raw:
        return item
    body, tags = split_tags(raw)
    if not tags:
        return item
    if body != raw:
        item["text_raw"] = raw
        item["text"] = body
    item["tags"] = tags
    return item


# 旧版写进标题的占位/编造值。新规则是「取不到就留空」（SPEC §3），
# 所以回填时要识别并抹掉——它们不含 #，光靠去标签抓不到（踩过）。
_LEGACY_TITLE_JUNK = {"（这条没有文案）", "（无标题）", "（没有标题）", "无标题", "没有标题"}
_LEGACY_TITLE_JUNK_RE = re.compile(r"^(?:作品|抖音作品|抖音文章|小红书笔记)\s*\d+$")


def _clean_title(raw: str) -> str:
    """标题去标签 + 抹掉旧占位。只动含 # 的，且不做截断（真标题可能是长句）。

    之前只拆正文不拆标题，列表页被 `#AI#智能体#openclaw` 糊住，认不出是哪条（踩过）。
    拆完空了就**留空**，不许填「（这条没有文案）」占位——SPEC §3「取不到就留空」。

    没有独立标题时，接口会拿整段正文当标题；那种情况取**第一个非空行**，
    别把四五百字的正文糊成一行塞进列表（回填旧数据时踩到）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text in _LEGACY_TITLE_JUNK or _LEGACY_TITLE_JUNK_RE.fullmatch(text):
        return ""
    if "#" not in text:
        return text
    body, _ = split_tags(text)
    for line in body.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def _clean_titles(item: dict[str, Any]) -> dict[str, Any]:
    """正文去标签之外，标题也要去（含主页清单里每条作品的标题）。"""
    item["title"] = _clean_title(item.get("title"))
    for child in item.get("children") or []:
        if isinstance(child, dict):
            child["title"] = _clean_title(child.get("title"))
    for row in item.get("children_preview") or []:
        if isinstance(row, dict):
            row["title"] = _clean_title(row.get("title"))
    return item


def _has_transcript(item: dict[str, Any]) -> bool:
    transcript = item.get("transcript") or {}
    return bool(str(transcript.get("text") or "").strip()) or any(
        isinstance(segment, dict) and str(segment.get("text") or "").strip()
        for segment in transcript.get("segments") or []
    )


def _child_result(child: dict[str, Any], saved: dict[str, Any]) -> list[str]:
    """子条入库状态和字幕状态分开；把带作品 ID 的原因汇总到父任务。"""
    child["item_id"] = saved["id"]
    child["has_transcript"] = _has_transcript(saved)
    child["warnings"] = list(saved.get("warnings") or [])
    return [f"{saved['id']}：{warning}" for warning in child["warnings"]]


def _finalize(item: dict[str, Any], ctx: FetchContext) -> dict[str, Any]:
    _split_text_and_tags(item)
    _clean_titles(item)
    # 来源：单条、批量、子条目都从这里过，统一打一次。只认白名单，别让任意字符串进库。
    wanted = str((ctx.options or {}).get("source") or "").strip()
    item["source"] = wanted if wanted in store.SOURCES else "library"
    # 收藏夹分栏：只有收藏同步会传；顺手做清洗（去空、去重、限个数）
    raw_cols = (ctx.options or {}).get("collections")
    item["collections"] = store.item_collections({"collections": raw_cols or []})[:20]
    item["fetched_at"] = store.now_iso()
    item["source_api"] = list(ctx.source_api)
    item["warnings"] = list(ctx.warnings)
    item.setdefault("author", {})
    item.setdefault("stats", {})
    item.setdefault("profile", {})
    item.setdefault("media", [])
    item.setdefault("children", [])
    if not item.get("transcript"):
        item["transcript"] = {"source": "none", "text": "", "segments": []}
    transcript = item["transcript"]
    有文字 = _has_transcript(item)
    # 适配器可能返回空稿但忘了 warn；在所有落盘路径统一兜底。
    has_video_track = item.get("kind") == "video" or any(
        isinstance(media, dict) and media.get("type") == "video"
        for media in item.get("media") or []
    )
    if has_video_track and ctx.want_transcribe and not 有文字:
        note = str(transcript.get("note") or "").strip()
        if not note:
            note = ("转写引擎已关闭（off），本次请求未生成文字稿"
                    if ctx.options.get("transcribe_engine") == "off"
                    else "已请求转写，但未生成文字稿（转写未返回有效文字）")
        transcript["note"] = note
        ctx.warn(note)
        item["warnings"] = list(ctx.warnings)
    if not ctx.want_media:
        for media in item.get("media") or []:
            if (isinstance(media, dict) and media.get("type") == "video"
                    and not media.get("path") and not media.get("error") and not media.get("removed")):
                media["download_skipped"] = True
    if ctx.options.get("keep_video", True) is False and 有文字:
        for media in item.get("media") or []:
            if not isinstance(media, dict) or media.get("role") != "video":
                continue
            relative = str(media.get("path") or "").strip()
            if not relative:
                continue
            path = store.media_abs_path(str(item.get("id") or ctx.item_id), relative)
            try:
                if path:
                    path.unlink(missing_ok=True)
                media["path"] = ""
                media["removed"] = True
            except OSError as exc:
                ctx.warn(f"转写完成，但视频文件未能清理（{exc}）")
                item.setdefault("warnings", []).append(ctx.warnings[-1])
    return item


def fetch_one_child(
    child: dict[str, Any],
    options: dict[str, Any],
    progress: ProgressFn | None,
    *,
    index: int = 0,
    total: int = 0,
) -> dict[str, Any] | None:
    """抓主页清单里的单条作品，落成独立 item。失败原因写回子条（不中断整批）。"""
    from .resolver import resolve

    url = str(child.get("url") or "")
    if not url:
        child["batch_state"] = "failed"
        child["error"] = "子条没有可抓取的链接"
        return None
    try:
        resolved = resolve(url, expand=False)
        adapter = get_adapter(resolved)
        with tempfile.TemporaryDirectory(prefix="glean-child-") as tmp:
            ctx = _build_context(options, Path(tmp), progress)
            item = adapter(resolved, ctx)
            item = _finalize(item, ctx)
            store.save_item(item)
            return item
    except Exception as exc:  # noqa: BLE001 - 单个子条失败不应中断整个主页
        child["batch_state"] = "failed"
        child["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return None


def run_preview(
    resolved: Resolved,
    options: dict[str, Any] | None = None,
    progress: ProgressFn | None = None,
    on_partial: ProgressFn | None = None,
) -> dict[str, Any]:
    """只拉"主页 + 作品清单"，不下载媒体、不转写、不展开子条目。

    博主主页先出预览让用户挑，是产品的主流程：先看清有哪些作品（封面/标题/时长/数据），
    再决定抓哪几条。所以这一步必须便宜——只花一次列表接口的钱。
    """
    options = dict(options or {})
    preview_options = {
        **options,
        "download_media": False,     # 预览不下媒体
        "transcribe": False,         # 预览不转写
        "expand_children": False,    # 预览不逐条抓
    }
    adapter = get_adapter(resolved)
    with tempfile.TemporaryDirectory(prefix="glean-preview-") as tmp:
        ctx = _build_context(preview_options, Path(tmp), progress, on_partial)
        if progress:
            progress("正在读取作品清单", 10, f"{resolved.platform_label} · {resolved.kind_label}")
        item = adapter(resolved, ctx)
        # 顺序很重要：必须先 _finalize（它才把 warnings / source_api 挂上去）
        # 再推最终快照，否则界面上永远看不到适配器的降级说明（踩过：
        # 小红书资料接口挂了，用户只看到"名字是空的"，没有任何解释）。
        item = _finalize(item, ctx)
        ctx.attach(item)
        item["preview"] = True
        item_id = store.save_item(item)
        # save_item 会按磁盘事实修正 children.local；推最终快照让预览也立刻标出已存作品。
        ctx.publish()
        store.rebuild_index()
    return {"item_id": item_id, "item": item, "warnings": item.get("warnings") or [],
            "children_ok": 0, "children_failed": 0}


def run_batch(
    items: list[dict[str, Any]],
    options: dict[str, Any] | None = None,
    progress: ProgressFn | None = None,
    on_partial: ProgressFn | None = None,
    profile_id: str = "",
) -> dict[str, Any]:
    """批量抓用户从预览里挑中的作品。

    每条作品单独解析、单独落成 item；单条失败不中断整批，最后汇总成功/失败数。
    on_partial 会推一个"批次清单"，让界面能逐条显示进度。
    """
    options = dict(options or {})
    total = len(items)
    if not total:
        raise ValueError("没有选中任何作品")

    # 批次自身的进度清单（前端按这个逐条打勾）
    batch_item: dict[str, Any] = {
        "id": profile_id or f"batch-{store.now_iso()}",
        "platform": "",
        "platform_label": "",
        "kind": "profile",
        "title": f"已选 {total} 条作品",
        "url": "",
        "canonical_url": "",
        "text": "",
        "author": {},
        "created_at": "",
        "duration": None,
        "stats": {},
        "transcript": {"source": "none", "text": "", "segments": []},
        "media": [],
        "profile": {},
        "children": [],
        "fetched_at": store.now_iso(),
        "source_api": [],
        "warnings": [],
    }

    def publish_batch() -> None:
        if on_partial:
            on_partial(batch_item)

    from .resolver import resolve

    done_ids: list[str] = []
    # 收集 (item_id, aweme_id)，收尾时一次批量补播放量
    待补播放: list[tuple[str, str]] = []
    failed = 0
    for index, entry in enumerate(items, start=1):
        url = str(entry.get("url") or "")
        child_row = {
            "id": str(entry.get("id") or ""),
            "title": str(entry.get("title") or url),
            "kind": str(entry.get("kind") or "video"),
            "duration": entry.get("duration"),
            "stats": entry.get("stats") or {},
            "created_at": entry.get("created_at") or "",
            "url": url,
            "cover": str(entry.get("cover") or ""),
            "local": False,
            "batch_state": "running",
            "transcribe": bool(entry.get("transcribe", options.get("transcribe", True))),
        }
        batch_item["children"].append(child_row)
        publish_batch()

        # 每条占进度条的一段：第 i 条落在 [基, 基+段长]。
        # 光在换条时跳一下不够——一条内部要下十几张图，中间几十秒百分比不动，
        # 看着就像卡死了。所以把每条自己的进度映射进它那一段。
        段长 = 90.0 / max(1, total)
        基 = 5.0 + 段长 * (index - 1)
        标题短 = str(entry.get("title") or "")[:20]

        def 子进度(标签: str, 百分比: int, 说明: str = "") -> None:
            if not progress:
                return
            整体 = int(基 + 段长 * max(0, min(100, 百分比)) / 100)
            # 说明优先用子步骤的（"正在下载图片和视频"比标题更有信息量）
            细节 = f"第 {index}/{total} 条"
            if 说明:
                细节 += f"：{说明[:24]}"
            elif 标题短:
                细节 += f"：{标题短}"
            progress("正在抓取选中的作品", 整体, 细节)

        if progress:
            progress("正在抓取选中的作品", int(基), f"第 {index}/{total} 条：{标题短}")

        try:
            child_resolved = resolve(url, expand=False)
        except ValueError as exc:
            child_row["batch_state"] = "failed"
            child_row["error"] = str(exc)[:120]
            failed += 1
            publish_batch()
            continue

        if options.get("skip_existing"):
            biz = child_resolved.ids.get("aweme_id") or child_resolved.ids.get("note_id") or ""
            local_id = store.find_same_work(child_resolved.platform, biz)
            if local_id:
                child_row.update({"local": True, "item_id": local_id, "batch_state": "done"})
                done_ids.append(local_id)
                publish_batch()
                continue

        try:
            adapter = get_adapter(child_resolved)
            # 逐条控制转写：用户在清单里勾了"这条要转文字"就转，没勾就不转。
            # 转写（尤其本机模型）很慢，不能因为批量拿就无差别全转。
            这条选项 = dict(options)
            if "transcribe" in entry:
                这条选项["transcribe"] = bool(entry.get("transcribe"))
            # 播放量由 run_batch 收尾**一次**批量补。逐条补的话 N 条就是 N 次接口，白花钱。
            这条选项["补播放量"] = False
            with tempfile.TemporaryDirectory(prefix="glean-batch-") as tmp:
                ctx = _build_context(这条选项, Path(tmp), 子进度, None)
                child_item = adapter(child_resolved, ctx)
                child_item = _finalize(child_item, ctx)
                saved_id = store.save_item(child_item)
            child_row["id"] = saved_id
            child_row["item_id"] = saved_id
            child_row["title"] = child_item.get("title") or child_row["title"]
            child_row["kind"] = child_item.get("kind") or child_row["kind"]
            child_row["cover"] = next(
                (m.get("path") and f"/api/media/{saved_id}/{Path(m['path']).name}"
                 for m in (child_item.get("media") or []) if m.get("role") == "cover"),
                child_row["cover"],
            ) or child_row["cover"]
            child_row["local"] = True
            child_row["batch_state"] = "partial" if child_item.get("warnings") else "done"
            batch_item["warnings"].extend(_child_result(child_row, child_item))
            done_ids.append(saved_id)
            待补播放.append((saved_id, str(child_resolved.ids.get("aweme_id") or "")))
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            child_row["batch_state"] = "failed"
            child_row["error"] = f"{type(exc).__name__}: {exc}"[:160]
            failed += 1
        publish_batch()

    # 播放量：抓的时候拿不到（列表/详情接口的 play_count 恒 0），收尾拿一批 ID
    # 去批量接口一次补回来。逐条补会是 N 次调用，白花钱。
    # 只有抖音有这个批量接口，别的平台静默跳过（播放量是加分项，不该影响抓取）。
    if 待补播放:
        try:
            from .adapters.douyin import fetch_play_counts
        except Exception:  # noqa: BLE001
            fetch_play_counts = None
        if fetch_play_counts:
            if progress:
                progress("正在补播放量", 94, f"{len(待补播放)} 条")
            播放 = fetch_play_counts([aid for _, aid in 待补播放 if aid])
            for saved_id, aid in 待补播放:
                if aid in 播放:
                    store.patch_item_stats(saved_id, {"play": 播放[aid]})

    if profile_id and done_ids:
        store.mark_children_local(profile_id, done_ids)
    store.rebuild_index()
    if progress:
        progress("正在写入本地库", 96, f"成功 {len(done_ids)} 条，失败 {failed} 条")

    if failed:
        summary = f"{len(done_ids)} 条入库，{failed} 条失败"
        reasons = [str(row.get("error") or "") for row in batch_item["children"] if row.get("error")]
        if reasons:
            summary += "：" + "；".join(reasons[:3])
        batch_item["warnings"].append(summary)
    publish_batch()
    return {
        "item_id": profile_id or "",
        "item": batch_item,
        "warnings": batch_item["warnings"],
        "children_ok": len(done_ids),
        "children_failed": failed,
    }


def run_fetch(
    resolved: Resolved,
    options: dict[str, Any] | None = None,
    progress: ProgressFn | None = None,
    on_partial: ProgressFn | None = None,
) -> dict[str, Any]:
    """执行抓取。返回 {item_id, item, warnings, children_ok, children_failed}。

    on_partial 会在抓取过程中被反复调用，收到的是"当前已经拿到的内容"，
    用于前端渐进展示（不必等整条抓完）。
    """
    options = dict(options or {})
    adapter = get_adapter(resolved)

    with tempfile.TemporaryDirectory(prefix="glean-") as tmp:
        work_dir = Path(tmp)
        ctx = _build_context(options, work_dir, progress, on_partial)
        if progress:
            progress("正在调用平台接口", 8, f"{resolved.platform_label} · {resolved.kind_label}")

        item = adapter(resolved, ctx)
        # 适配器返回后再推一次完整快照：转写结果等是在最后一次 report 之后才填进去的，
        # 不补这一次，前端就要等到全部结束才能看到字幕。
        # 同样必须先 _finalize 把 warnings 挂上，再推。
        item = _finalize(item, ctx)
        ctx.attach(item)

        # 主页：把清单里的作品也落成独立 item，方便 agent 逐条读
        children_ok = 0
        children_failed = 0
        if item.get("kind") == "profile" and options.get("expand_children", True):
            children = item.get("children") or []
            total = len(children)
            if total:
                # 批量展开时降低单条成本：媒体按设置、转写默认关（用户可显式开）
                child_options = dict(options)
                child_options["transcribe"] = bool(options.get("transcribe_children", False))
                for index, child in enumerate(children, start=1):
                    percent = CHILD_PROGRESS_START + int(
                        (CHILD_PROGRESS_END - CHILD_PROGRESS_START) * index / max(1, total)
                    )
                    if progress:
                        progress(
                            "正在抓取作品内容",
                            percent,
                            f"第 {index}/{total} 条：{str(child.get('title') or '')[:24]}",
                        )
                    saved = fetch_one_child(child, child_options, None, index=index, total=total)
                    if saved:
                        child["local"] = True
                        child["batch_state"] = "partial" if saved.get("warnings") else "done"
                        item["warnings"].extend(_child_result(child, saved))
                        children_ok += 1
                    else:
                        child["batch_state"] = "failed"
                        children_failed += 1
                item["children"] = children

        if children_failed:
            failed_children = [child for child in item.get("children") or []
                               if child.get("batch_state") == "failed"]
            reasons = [f"{child.get('id') or child.get('url') or '子条'}：{child.get('error')}"
                       for child in failed_children if child.get("error")]
            summary = f"{children_ok} 条入库，{children_failed} 条失败"
            if reasons:
                summary += "：" + "；".join(reasons[:3])
            item["warnings"].append(summary)
        ctx.publish()
        if progress:
            progress("正在写入本地库", 95)
        item_id = store.save_item(item)
        store.rebuild_index()

    return {
        "item_id": item_id,
        "item": item,
        "warnings": item.get("warnings") or [],
        "children_ok": children_ok,
        "children_failed": children_failed,
    }
