#!/usr/bin/env python3
"""Glean 本地服务：静态前端 + JSON API + SSE 进度 + 媒体 Range 传输。

只用标准库（http.server），零依赖，双击脚本就能跑。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):  # 允许 `python3 server/main.py` 直接启动
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from server import media, store, update_check, viral, viral_r  # type: ignore
    from server.accounts import (add_account, cached_works, get_account,
                                 remove_account, summarize_all, update_account)
    from server.config import (BROWSER_UA, LIBRARY_ROOT, WEB_ROOT, aliyun_api_key,
                               clear_key_cache, load_config, save_config,
                               tikhub_api_key, tikhub_key_source, volcengine_api_key)
    from server.jobs import JobManager
    from server.media import (aliyun_state, have, local_whisper_available,
                             volcengine_state)
    from server.resolver import resolve
    from server.tikhub import (TikHubError, account_state, detect_proxy, proxy_state)
    from server.tikhub import _build_opener as tikhub_opener
else:
    from . import media, store, update_check, viral, viral_r
    from .accounts import (add_account, cached_works, get_account,
                           remove_account, summarize_all, update_account)
    from .config import (BROWSER_UA, LIBRARY_ROOT, WEB_ROOT, aliyun_api_key,
                         clear_key_cache, load_config, save_config, tikhub_api_key,
                         tikhub_key_source, volcengine_api_key)
    from .jobs import JobManager
    from .media import (aliyun_state, have, local_whisper_available,
                        volcengine_state)
    from .resolver import resolve
    from .tikhub import (TikHubError, account_state, detect_proxy, proxy_state)
    from .tikhub import _build_opener as tikhub_opener

BIND_HOST = "127.0.0.1"
BIND_PORT = 8899
MAX_BODY = 2 * 1024 * 1024
VERSION = update_check.VERSION

JOBS = JobManager(max_workers=2)
_AVATAR_CACHE: dict[str, tuple[bytes, str]] = {}
_AVATAR_CACHE_LOCK = threading.Lock()


def library_size_bytes() -> int:
    """本地库占用的磁盘空间。

    视频很占地方（一条 4 分钟的小红书视频就有 70MB），把体积显示出来，
    用户才知道什么时候该关掉"保留视频"。
    """
    total = 0
    for path in LIBRARY_ROOT.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


# 各平台"作品业务 ID"在 resolved.ids 里的字段名。判重靠它，不靠链接字符串。
_BUSINESS_ID_KEYS = (
    "aweme_id",    # 抖音 / TikTok
    "note_id",     # 小红书
    "tweet_id",    # X
    "video_id",    # YouTube
    "article_id",  # 抖音文章 / 知乎文章
    "answer_id",   # 知乎回答
    "pin_id",      # 知乎想法
    "post_id",     # 微博
    "bv_id", "aid",  # B 站
    "photo_id",    # 快手
)


def _already_in_library(resolved: Any) -> Optional[dict[str, Any]]:
    """这条链接是不是已经抓过，给前端提示用。按**业务 ID**判，不比链接字符串。

    同一条作品用户可能从 `?modal_id=<id>`、`/video/<id>`、短链、App 分享文案
    四种形态粘进来；存下来的 `url` 是"用户粘的那条"、`canonical_url` 是规范链，
    字符串比对必然漏判 → 重复抓一遍，白花钱还多等几分钟（踩过）。
    """
    ids = dict(getattr(resolved, "ids", None) or {})
    business_id = ""
    for key in _BUSINESS_ID_KEYS:
        value = str(ids.get(key) or "").strip()
        if value:
            business_id = value
            break
    urls = [str(getattr(resolved, "url", "") or ""),
            str(getattr(resolved, "input_url", "") or "")]
    found = store.find_same_work(str(getattr(resolved, "platform", "") or ""),
                                 business_id, urls)
    if not found:
        return None
    item = store.load_item(found)
    if not item:
        return None
    return {"id": found,
            "fetched_at": str(item.get("fetched_at") or ""),
            "title": str(item.get("title") or "")}


def _分栏清洗(value: Any) -> list[str]:
    """收藏夹分栏名清洗：只认列表，去空、去重、限 20 个。

    传字符串会被逐字拆成假夹名（"健身" → "健"/"身"），所以先判类型。
    「未分类」是界面兜底分组，不是真夹名，直接剔掉。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for raw in value:
        name = str(raw or "").strip()
        if name and name != "未分类" and name not in out:
            out.append(name)
        if len(out) >= 20:
            break
    return out


def _收藏夹同步路径() -> tuple[Path, Path]:
    """返回 (同步脚本路径, 进度文件路径)。进度文件由同步脚本自己写。"""
    raw = str(load_config().get("favorites_sync_script") or "").strip()
    script = Path(os.path.expanduser(raw)) if raw else Path("")
    return script, (script.parent / "上次同步.json" if str(script) else Path(""))


def _同步在跑(progress: dict[str, Any]) -> bool:
    """进度文件说 running 还不够——脚本崩了会留下 running=true 的假象，要验进程。"""
    if not progress.get("running"):
        return False
    try:
        pid = int(progress.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = f"Glean/{VERSION}"
    protocol_version = "HTTP/1.1"
    # ---- 基础工具 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        # 默认日志太吵，只留错误
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write(f"[glean] {fmt % args}\n")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        return payload if isinstance(payload, dict) else {}

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path.startswith("/api/"):
            self._route_api_get(path, query)
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(str(exc))
            return
        match = re.fullmatch(r"/api/accounts/([^/]+)/sync", path)
        if match:
            self._api_account_sync(match.group(1), payload)
            return
        if path == "/api/accounts":
            self._api_account_add(payload)
        elif path == "/api/resolve":
            self._api_resolve(payload)
        elif path == "/api/reveal":
            self._api_reveal(payload)
        elif path == "/api/viral/preview":
            self._api_viral_preview(payload)
        elif path in ("/api/viral/r/estimate", "/api/viral/r"):
            try:
                result = viral_r.estimate(payload) if path.endswith('/estimate') else viral_r.execute(payload)
                self._json({"ok": True, **result})
            except (ValueError, OSError, TikHubError) as exc:
                self._error(str(exc))
        elif path == "/api/viral/feedback":
            self._api_viral_feedback(payload)
        elif path == "/api/viral/save":
            self._api_viral_save(payload)
        elif path == "/api/preview":
            self._api_preview(payload)
        elif path == "/api/jobs":
            self._api_create_job(payload)
        elif path == "/api/config":
            self._api_save_config(payload)
        elif path == "/api/recheck":
            self._api_recheck()
        elif path == "/api/favorites/sync":
            self._api_favorites_sync()
        else:
            self._error("没有这个接口", 404)

    def do_PATCH(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(str(exc))
            return
        match = re.fullmatch(r"/api/accounts/([^/]+)", path)
        if match:
            try:
                self._json({"ok": True, "account": update_account(match.group(1), payload)})
            except ValueError as exc:
                self._error(str(exc))
            return
        self._error("没有这个接口", 404)

    def do_DELETE(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        match = re.fullmatch(r"/api/accounts/([^/]+)", path)
        if match:
            if remove_account(match.group(1)):
                self._json({"ok": True})
            else:
                self._error("找不到这个账号", 404)
            return
        match = re.fullmatch(r"/api/items/([^/]+)", path)
        if not match:
            self._error("没有这个接口", 404)
            return
        item_id = match.group(1)
        if store.delete_item(item_id):
            store.rebuild_index()
            self._json({"ok": True, "deleted": item_id})
        else:
            self._error("这个条目不存在", 404)

    # ---- API ----

    def _route_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/update":
            self._json(update_check.check_update(VERSION, refresh=query.get("refresh") == ["1"]))
            return
        if path == "/api/status":
            self._api_status()
            return
        if path == "/api/favorites/status":
            self._api_favorites_status()
            return
        if path == "/api/accounts":
            self._json({"ok": True, "accounts": summarize_all(),
                        "supported": [{"platform": "douyin", "label": "抖音"},
                                      {"platform": "xiaohongshu", "label": "小红书"}]})
            return
        match = re.fullmatch(r"/api/accounts/([^/]+)/works", path)
        if match:
            result = cached_works(match.group(1))
            if result is None:
                self._error("找不到这个账号", 404)
            else:
                self._json({"ok": True, **result})
            return
        if path == "/api/viral/feedback":
            self._api_viral_feedback()
            return
        if path == "/api/config":
            config = load_config()
            # key 明文一个都不许出这个门（SPEC §6）。只告诉前端"配没配"，
            # 让它知道该显示"已配 Key / 没配 Key"，但拿不到值。
            for secret in ("tikhub_api_key", "volcengine_api_key", "aliyun_api_key"):
                config.pop(secret, None)
            config["tikhub_api_key_set"] = bool(tikhub_api_key())
            config["volcengine_api_key_set"] = bool(volcengine_api_key())
            config["aliyun_api_key_set"] = bool(aliyun_api_key())
            self._json({"ok": True, "config": config})
            return
        if path == "/api/items":
            self._api_items(query)
            return
        if path == "/api/jobs":
            self._json({"ok": True, "jobs": JOBS.recent()})
            return

        match = re.fullmatch(r"/api/jobs/([^/]+)/events", path)
        if match:
            self._api_job_events(match.group(1))
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if match:
            job = JOBS.get(match.group(1))
            if not job:
                self._error("任务不存在", 404)
                return
            self._json({"ok": True, "job": job.to_dict()})
            return
        match = re.fullmatch(r"/api/items/([^/]+)/content", path)
        if match:
            content = store.load_content(match.group(1))
            if content is None:
                self._error("条目不存在", 404)
                return
            self._json({"ok": True, "content": content})
            return
        match = re.fullmatch(r"/api/items/([A-Za-z0-9_-]{1,160})/raw", path)
        if match:
            item_id = match.group(1)
            items_root = (LIBRARY_ROOT / "items").resolve()
            item_dir = (items_root / item_id).resolve()
            if not str(item_dir).startswith(str(items_root) + os.sep):
                self._error("条目不存在", 404)
                return
            raw_dir = item_dir / "raw"
            if not raw_dir.is_dir():
                self._error("这条内容没有接口原始记录", 404)
                return
            files = []
            for raw_path in sorted(raw_dir.glob("*.json")):
                try:
                    data = json.loads(raw_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                files.append({"name": raw_path.name, "data": data})
            self._json({"ok": True, "files": files})
            return
        match = re.fullmatch(r"/api/items/([^/]+)", path)
        if match:
            item = store.load_item(match.group(1))
            if not item:
                self._error("条目不存在", 404)
                return
            self._json({"ok": True, "item": item})
            return
        match = re.fullmatch(r"/api/media/([^/]+)/(.+)", path)
        if match:
            self._serve_media(match.group(1), match.group(2))
            return
        if path == "/api/thumb":
            self._api_thumb(query)
            return
        if path == "/api/avatar":
            self._api_avatar(query)
            return
        self._error("没有这个接口", 404)

    def _api_status(self) -> None:
        key = tikhub_api_key()
        state = proxy_state()
        if not state.get("checked_at"):
            state = detect_proxy()
        counts = store.platform_counts()
        self._json({
            "ok": True,
            "version": VERSION,
            "library_root": str(LIBRARY_ROOT),
            "key_present": bool(key),
            "key_source": tikhub_key_source(),
            "volcengine_key": bool(volcengine_api_key()),
            "tikhub_ok": account_state().get("ok"),             # None=还没试过
            "tikhub_note": account_state().get("note") or "",
            "volcengine_ok": volcengine_state().get("ok"),      # None=还没试过
            "volcengine_note": volcengine_state().get("note") or "",
            "aliyun_key": bool(aliyun_api_key()),
            "aliyun_ok": aliyun_state().get("ok"),
            "aliyun_note": aliyun_state().get("note") or "",
            "aliyun_model": load_config().get("aliyun_asr_model") or "",
            "local_transcribe": local_whisper_available(),
            "proxy": {
                "url": state.get("url") or "",
                "ok": bool(state.get("ok")),
                "latency_ms": state.get("latency_ms") or 0,
                "message": state.get("message") or "",
                "checked_at": store.now_iso() if state.get("checked_at") else "",
            },
            "network": {
                "tikhub_ok": bool(state.get("ok")),
                "direct_ok": not bool(state.get("url")),
                "message": state.get("message") or "尚未探测",
            },
            "counts": {"items": sum(counts.values()), "by_platform": counts},
            "library_bytes": library_size_bytes(),
            "ffmpeg": have("ffmpeg"),
            "ffprobe": have("ffprobe"),
            "ytdlp": have("yt-dlp"),
            "node": have("node"),
        })

    def _api_recheck(self) -> None:
        state = detect_proxy(force=True)
        self._json({"ok": bool(state.get("ok")), "proxy": state,
                    "error": None if state.get("ok") else state.get("message")})

    def _api_account_add(self, payload: dict[str, Any]) -> None:
        try:
            row = add_account(str(payload.get("url") or ""), str(payload.get("name") or ""))
        except ValueError as exc:
            self._error(str(exc))
            return
        self._json({"ok": True, "account": row}, status=201)

    def _api_account_sync(self, account_id: str, payload: dict[str, Any]) -> None:
        """点「更新作品」：先只拉作品清单（预览），让用户挑，再决定抓哪些。

        这么设计是因为"更新"通常只要新增的那几条，不该无差别全抓——费钱又费时。
        """
        account = get_account(account_id)
        if not account:
            self._error("找不到这个账号", 404)
            return
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        merged = self._merge_options(options)
        # 主页清单要比默认多拉一些，方便看出哪些是新的
        merged["limit"] = min(200, max(int(merged.get("limit") or 20), 50))
        merged["account_id"] = account_id
        job = JOBS.create_preview(str(account["url"]), merged)
        self._json({"ok": True, "job_id": job.id, "mode": "preview",
                    "account": account}, status=202)

    def _api_resolve(self, payload: dict[str, Any]) -> None:
        url = str(payload.get("url") or "").strip()
        if not url:
            self._error("请提供 url")
            return
        try:
            resolved = resolve(url)
        except ValueError as exc:
            self._error(str(exc))
            return
        result = resolved.to_dict()
        # 顺手告诉前端这条是不是已经抓过。
        # 按**业务 ID**判，不比链接字符串——同一条作品可能从 ?modal_id=、/video/、
        # 短链、App 分享文案四种形态粘进来，比字符串会漏判然后重复抓（踩过）。
        result["already_in_library"] = _already_in_library(resolved)
        self._json({"ok": True, **result})

    def _merge_options(self, options: dict[str, Any]) -> dict[str, Any]:
        config = load_config()
        return {
            "download_media": options.get("download_media", config.get("download_media", True)),
            "transcribe": options.get("transcribe", True),
            "transcribe_engine": config.get("transcribe_engine", "volcengine"),
            "limit": options.get("limit", config.get("default_limit", 20)),
            "expand_children": options.get("expand_children", False),
            "transcribe_children": options.get("transcribe_children", False),
            "keep_video": options.get("keep_video", config.get("keep_video", True)),
            # 来源（SPEC 4.6）：不带就是「我拿过的」；收藏同步传 favorites
            "source": options.get("source", "library"),
            # 收藏夹分栏（SPEC 4.6）：收藏同步按夹名传，最多 20 个。
            # 只认列表——传字符串会被逐字拆开，变成一堆假夹名。
            "collections": [str(x).strip() for x in _分栏清洗(options.get("collections"))],
        }

    def _api_reveal(self, payload: dict[str, Any]) -> None:
        """在访达里真的打开这条内容的文件夹。

        之前只把路径复制到剪贴板，用户点完什么都没发生——那不叫"显示"。
        这里直接调 macOS 的 open，并且只允许打开 library/items/ 下面真实存在的目录。
        """
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            self._error("缺少 item_id")
            return
        directory = store.item_dir(item_id)
        # 防路径穿越：解析后的路径必须仍在 items/ 目录里，且真的存在
        items_root = (LIBRARY_ROOT / "items").resolve()
        try:
            resolved = directory.resolve()
        except OSError:
            self._error("这条内容不存在", 404)
            return
        if not str(resolved).startswith(str(items_root)) or not resolved.is_dir():
            self._error("这条内容不在本地库里（可能还没抓下来）", 404)
            return
        if sys.platform != "darwin":
            self._json({"ok": True, "path": str(resolved), "opened": False,
                        "message": "不是 macOS，已返回路径"})
            return
        try:
            subprocess.Popen(["open", str(resolved)])
        except OSError as exc:
            self._error(f"打不开访达：{exc}", 500)
            return
        self._json({"ok": True, "path": str(resolved), "opened": True})

    def _api_viral_feedback(self, payload=None) -> None:
        try:
            value = viral.load_feedback() if payload is None else viral.save_feedback(payload)
            self._json({"ok": True, "feedback": value})
        except (ValueError, OSError) as exc:
            self._error(str(exc))

    def _api_viral_preview(self, payload: dict[str, Any]) -> None:
        try:
            settings = viral.prepare(payload)
        except (ValueError, TikHubError) as exc:
            self._error(str(exc))
            return
        job = JOBS.create("viral:preview", settings, mode="viral_preview")
        self._json({"ok": True, "job_id": job.id, "mode": "viral_preview"}, status=202)

    def _api_viral_save(self, payload: dict[str, Any]) -> None:
        try:
            items, existing = viral.selected_items(payload)
            options = payload.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("options 必须是对象")
            for key in ("download_media", "transcribe"):
                if key in options and not isinstance(options[key], bool):
                    raise ValueError(f"{key} 必须是布尔值")
        except (ValueError, TikHubError) as exc:
            self._error(str(exc))
            return
        if not items:
            self._json({"ok": True, "job_id": None, "count": 0, "existing": existing})
            return
        # 保存走原有批次管线；默认只下载，转写必须由用户显式开启。
        merged = self._merge_options({**options, "transcribe": options.get("transcribe", False)})
        merged["skip_existing"] = True
        job = JOBS.create_batch(items, merged)
        self._json({"ok": True, "job_id": job.id, "mode": "batch",
                    "count": len(items), "existing": existing}, status=202)

    def _api_preview(self, payload: dict[str, Any]) -> None:
        """博主主页第一步：只拉作品清单（封面/标题/时长/数据），不下载不转写。"""
        url = str(payload.get("url") or "").strip()
        if not url:
            self._error("请提供 url")
            return
        try:
            resolved = resolve(url)
        except ValueError as exc:
            self._error(str(exc))
            return
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        merged = self._merge_options(options)
        # 预览阶段想多看几条：允许请求里直接指定 limit（"再看 20 条"用）
        if isinstance(options.get("limit"), int) and options["limit"] > 0:
            merged["limit"] = min(200, options["limit"])
        job = JOBS.create_preview(url, merged)
        self._json({"ok": True, "job_id": job.id, "mode": "preview",
                    "resolve": resolved.to_dict()}, status=202)

    def _api_create_job(self, payload: dict[str, Any]) -> None:
        # 批量模式：抓用户在预览里挑中的作品
        items = payload.get("items")
        if isinstance(items, list) and items:
            clean = [row for row in items if isinstance(row, dict) and row.get("url")]
            if not clean:
                self._error("选中的作品里没有可用链接")
                return
            options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
            job = JOBS.create_batch(
                clean,
                self._merge_options(options),
                profile_id=str(payload.get("profile_id") or ""),
                url=str(payload.get("url") or ""),
            )
            self._json({"ok": True, "job_id": job.id, "mode": "batch",
                        "count": len(clean)}, status=202)
            return

        url = str(payload.get("url") or "").strip()
        if not url:
            self._error("请提供 url")
            return
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        try:
            resolved = resolve(url)
        except ValueError as exc:
            self._error(str(exc))
            return
        job = JOBS.create(url, self._merge_options(options))
        job.resolved = resolved
        self._json({"ok": True, "job_id": job.id, "mode": "single",
                    "resolve": resolved.to_dict()}, status=202)

    def _api_thumb(self, query: dict[str, list[str]]) -> None:
        """预览用的远程封面代理。

        预览阶段刻意不把封面下载到本地（要快要便宜），但平台 CDN 多半校验 Referer，
        浏览器直连会 403。这里由服务端带上正确的 Referer 取回来转发。

        只允许 http/https，且拒绝内网地址——本地服务也不该成为 SSRF 跳板。
        """
        raw = (query.get("url") or [""])[0]
        # parse_qs 会把空值丢掉，所以 ref 缺失或为空时这里是 []，直接 [0] 会 IndexError
        # 并让连接被静默关闭（curl 看到 Empty reply / HTTP 000）。前端已做防御，
        # 后端也必须扛得住，否则任何少传 ref 的调用都会断连。
        referer = (query.get("ref") or [""])[0]
        # 预览网格只要缩略图。原图动辄 800KB，20 张就是 16MB，网格会迟迟不出图。
        # 小红书这类 CDN 地址里带 imageView2/2/w/<宽>，把宽度改小即可，不必下原图。
        try:
            want_width = int((query.get("w") or ["0"])[0] or 0)
        except ValueError:
            want_width = 0
        if want_width and re.search(r"imageView2/2/w/\d+", raw):
            raw = re.sub(r"imageView2/2/w/\d+", f"imageView2/2/w/{max(80, min(2000, want_width))}", raw)
        if not raw:
            self._error("缺少 url")
            return
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https"):
            self._error("只支持 http/https")
            return
        host = (parsed.hostname or "").lower()
        blocked = (
            host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
            or host.endswith(".local")
            or host.startswith(("10.", "192.168.", "169.254."))
            or (host.startswith("172.") and 16 <= int((host.split(".")[1] or "0") or 0) <= 31)
        )
        if blocked:
            self._error("不允许访问内网地址")
            return
        headers = {"User-Agent": BROWSER_UA, "Accept": "image/*,*/*"}
        if referer:
            headers["Referer"] = referer
        try:
            request = urllib.request.Request(raw, headers=headers)
            # _build_opener 的代理参数是必填的，漏传会 TypeError（踩过：所有封面 502）。
            # 必须传当前探测到的代理——平台 CDN 常常也要走代理才通。
            state = proxy_state()
            with tikhub_opener(state.get("url") or "").open(request, timeout=30) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type") or "image/jpeg"
        except Exception as exc:  # noqa: BLE001 - 单张封面失败不该报错刷屏
            self._error(f"封面取不回来：{type(exc).__name__}", 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _api_avatar(self, query: dict[str, list[str]]) -> None:
        """只代理抖音/小红书头像，避免浏览器防盗链并限制可访问域名。"""
        raw = (query.get("url") or [""])[0]
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = ("douyinpic.com", "xhscdn.com")
        if parsed.scheme != "https" or not any(host == d or host.endswith("." + d) for d in allowed):
            self._error("头像地址不在允许范围")
            return
        with _AVATAR_CACHE_LOCK:
            cached = _AVATAR_CACHE.get(raw)
        if cached:
            body, content_type = cached
        else:
            referer = "https://www.douyin.com/" if host.endswith("douyinpic.com") else "https://www.xiaohongshu.com/"
            fetch_url = raw
            if host.endswith("douyinpic.com"):
                # 抖音头像默认给 .heic，Chrome 解不了；同一地址换成 .jpeg 就能直接拿到 JPEG
                fetch_url = re.sub(r"\.heic(?=\?|$)", ".jpeg", raw)
            request = urllib.request.Request(fetch_url, headers={
                "User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*",
                "Referer": referer,
            })
            try:
                proxy = proxy_state().get("url") or ""
                with tikhub_opener(proxy).open(request, timeout=15) as response:
                    body = response.read(4 * 1024 * 1024 + 1)
                    content_type = response.headers.get("Content-Type") or "image/jpeg"
                if len(body) > 4 * 1024 * 1024 or not content_type.lower().startswith("image/"):
                    self._error("头像文件无效", 502)
                    return
            except Exception:
                self._error("头像暂时取不回来", 502)
                return
            with _AVATAR_CACHE_LOCK:
                if len(_AVATAR_CACHE) >= 256:
                    _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
                _AVATAR_CACHE[raw] = (body, content_type)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _api_job_events(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self._error("任务不存在", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        channel = job.subscribe()
        try:
            self._sse_send("progress", job.to_dict())
            if job.state in ("done", "partial", "failed"):
                self._sse_send("done" if job.state != "failed" else "error", job.to_dict())
                return
            deadline = time.time() + 1800
            while time.time() < deadline:
                try:
                    event, data = channel.get(timeout=15)
                except Exception:  # noqa: BLE001 - 超时就发心跳，防止代理断流
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_send(event, data)
                if event in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            job.unsubscribe(channel)

    def _sse_send(self, event: str, data: Any) -> None:
        body = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _api_items(self, query: dict[str, list[str]]) -> None:
        def value(key: str, default: str = "") -> str:
            return (query.get(key) or [default])[0]

        limit = max(1, min(200, int(value("limit", "50") or 50)))
        offset = max(0, int(value("offset", "0") or 0))
        source = value("source")
        if source and source not in store.SOURCES:
            self._error("source 只能是 library 或 favorites")
            return
        total, rows = store.list_items(
            platform=value("platform"),
            kind=value("kind"),
            exclude_kind=value("exclude_kind"),
            source=source,
            collection=value("collection"),
            query=value("q"),
            author=value("author"),
            author_id=value("author_id"),
            missing_transcript=value("missing_transcript") == "1",
            limit=limit,
            offset=offset,
            order=value("order", "fetched_at_desc") or "fetched_at_desc",
        )
        # by_platform 跟着当前栏目走（切到收藏夹，平台筛选只数收藏）；
        # by_source 永远是全量，两个栏目顶上的数字靠它；
        # by_collection 是分栏计数（只在收藏夹栏目里有意义）
        self._json({"ok": True, "total": total, "items": rows,
                    "authors": store.author_names(source),
                    "author_counts": store.author_counts(source),
                    "author_total": store.content_count(source),
                    "by_platform": store.platform_counts(source),
                    "by_source": store.source_counts(),
                    "by_collection": store.collection_counts(source or "favorites")
                    if (source or "favorites") == "favorites" else {}})

    def _api_favorites_status(self) -> None:
        """「收藏夹」栏目的更新进度：读同步脚本写的进度文件，并确认进程真的还活着。"""
        script, progress_file = _收藏夹同步路径()
        info: dict[str, Any] = {"running": False, "message": "", "new_count": 0,
                                "started_at": "", "finished_at": "",
                                "configured": bool(str(script) and script.is_file())}
        try:
            if str(progress_file) and progress_file.is_file():
                info.update(json.loads(progress_file.read_text(encoding="utf-8")))
        except Exception:
            pass
        info["running"] = _同步在跑(info)
        if not info["message"]:
            info["message"] = "正在更新…" if info["running"] else ""
        self._json({"ok": True, **info})

    def _api_favorites_sync(self) -> None:
        """手动更新按钮：起一次收藏夹同步脚本（后台跑，进度靠 status 接口查）。"""
        script, progress_file = _收藏夹同步路径()
        if not str(script) or not script.is_file():
            self._error("找不到收藏夹同步脚本，检查设置里的路径", 404)
            return
        # 已经在跑就别再起一个：两个浏览器抢同一个登录 profile 会互相打架
        try:
            if str(progress_file) and progress_file.is_file():
                if _同步在跑(json.loads(progress_file.read_text(encoding="utf-8"))):
                    self._json({"ok": False, "running": True,
                                "message": "已经在更新了，稍等一下"}, status=200)
                    return
        except Exception:
            pass
        try:
            log_path = script.parent / "手动更新.log"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n===== 手动更新 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                fh.flush()
                subprocess.Popen([sys.executable, str(script)], cwd=str(script.parent),
                                 stdout=fh, stderr=fh, start_new_session=True)
        except Exception as exc:
            self._error(f"起不来：{exc}", 500)
            return
        self._json({"ok": True, "started": True,
                    "message": "已开始更新，新内容会自动出现"}, status=202)

    def _api_save_config(self, payload: dict[str, Any]) -> None:
        patch = {key: value for key, value in payload.items() if key in {
            "proxy", "transcribe_engine", "transcribe_fallback_local", "keep_video",
            "video_max_height",
            "download_media", "default_limit", "tikhub_api_key", "volcengine_api_key",
            "aliyun_api_key", "aliyun_asr_model", "aliyun_chunk_seconds",
        }}
        # Key 允许通过设置面板写入本地配置（600 权限），不写日志
        saved = save_config(patch)
        if any(key in patch for key in ("tikhub_api_key", "volcengine_api_key",
                                        "aliyun_api_key", "proxy")):
            clear_key_cache()
            if "proxy" in patch or "tikhub_api_key" in patch:
                detect_proxy(force=True)
        saved.pop("tikhub_api_key", None)
        # 任何接口都不许回显 key 明文（SPEC §6）——三个都要抹掉，
        # 之前只抹了 tikhub，另外两个会被原样吐回浏览器。
        saved.pop("volcengine_api_key", None)
        saved.pop("aliyun_api_key", None)
        self._json({"ok": True, "config": saved})

    # ---- 静态与媒体 ----

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            self._error("文件不存在", 404)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # 前端是本地开发，改动必须立刻可见。no-cache 还不够——
        # 浏览器手上那份旧副本会在没有校验器时继续用，用户就会看到"我明明改了它却没变"
        # （踩过：界面还显示着早就删掉的旧文案）。no-store 直接禁掉缓存。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, item_id: str, filename: str) -> None:
        """媒体文件，支持 Range（视频拖动进度条必须）。"""
        target = store.media_abs_path(item_id, f"media/{filename}")
        if not target:
            self._error("媒体文件不存在", 404)
            return
        size = target.stat().st_size
        content_type, _ = mimetypes.guess_type(str(target))
        content_type = content_type or "application/octet-stream"
        # 不信扩展名，按文件头判真实类型：历史文件里存在 .jpg 实际是 WebP 的情况，
        # Content-Type 报错浏览器就会渲染成白块。
        try:
            with open(target, "rb") as probe:
                head = probe.read(16)
            sniffed = media.sniff_suffix(head, "")
            if sniffed:
                content_type = mimetypes.guess_type(f"x{sniffed}")[0] or content_type
        except OSError:
            pass
        range_header = self.headers.get("Range") or ""
        start, end = 0, size - 1
        status = 200
        if range_header.startswith("bytes="):
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), size - 1)
                elif not raw_start:
                    start = max(0, size - int(raw_end or 0))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(target, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def build_server(host: str = BIND_HOST, port: int = BIND_PORT) -> ThreadingHTTPServer:
    store.ensure_library()
    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Glean 拾取 · 本地服务")
    parser.add_argument("--host", default=BIND_HOST)
    parser.add_argument("--port", type=int, default=BIND_PORT)
    parser.add_argument("--check", action="store_true", help="只做自检并退出")
    args = parser.parse_args()

    store.ensure_library()
    store.rebuild_index()

    if args.check:
        print("library:", LIBRARY_ROOT)
        print("web:", WEB_ROOT, "index:", (WEB_ROOT / "index.html").is_file())
        print("tikhub key:", bool(tikhub_api_key()), "source:", tikhub_key_source())
        print("volcengine key:", bool(volcengine_api_key()))
        print("ffmpeg:", have("ffmpeg"), "ffprobe:", have("ffprobe"),
              "yt-dlp:", have("yt-dlp"), "node:", have("node"))
        state = detect_proxy()
        print("proxy:", state.get("url") or "direct", "ok:", state.get("ok"), state.get("message"))
        return 0 if state.get("ok") else 1

    httpd = build_server(args.host, args.port)
    banner = f"拾取 Glean 已启动 → http://{args.host}:{args.port}"
    print(banner)
    print(f"本地库：{LIBRARY_ROOT}")
    if not tikhub_api_key():
        print("⚠️  未配置 TikHub Key，抓取会失败。请看 README 的配置章节。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        JOBS.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
