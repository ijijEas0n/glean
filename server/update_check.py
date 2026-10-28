"""只读更新检查：标准库、短超时、进程内缓存，不执行升级。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REPO_URL = "https://github.com/ijijEas0n/glean"
API_URL = "https://api.github.com/repos/ijijEas0n/glean"
RAW_URL = "https://raw.githubusercontent.com/ijijEas0n/glean/main/VERSION"
CACHE_SECONDS = 6 * 60 * 60
CHECK_TIMEOUT = 4.0
MAX_RESPONSE = 256 * 1024
_lock = threading.Lock()
_cache: dict = {}
_cache_until = 0.0


def version_parts(value: str) -> tuple[int, ...]:
    """仅接受数字段版本；忽略 v/V 前缀及末尾的零。"""
    value = value.strip()
    if not re.fullmatch(r"[vV]?[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError("版本号不是有效的数字段格式")
    parts = [int(part) for part in value.lstrip("vV").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def read_version() -> str:
    try:
        value = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
        version_parts(value)
        return value
    except (OSError, UnicodeError, ValueError):
        return "0.0.0"  # 无法确定本机版本时使用保守兜底，不影响服务启动。


VERSION = read_version()


def _fetch(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"glean-update-check/{VERSION}",
        "Accept": "application/vnd.github+json" if url.startswith(API_URL) else "text/plain",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("更新响应过大")
    return data.decode("utf-8")


def _error_reason(exc: Exception) -> str:
    # 不回显外部错误全文（可能包含用户的代理地址或凭证）。
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "请求超时"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return "网络不可用或请求超时"
    return "响应无效或没有可用版本"


def check_update(current: str = VERSION, *, refresh: bool = False) -> dict:
    global _cache, _cache_until
    with _lock:
        if not refresh and _cache.get("current") == current and time.monotonic() < _cache_until:
            return {**_cache, "source": "cache"}
        result = {
            "ok": False, "current": current, "latest": None,
            "update_available": False, "release_url": REPO_URL, "html_url": REPO_URL,
            "notes": "", "checked_at": None, "source": None, "error": None,
        }
        errors = []
        deadline = time.monotonic() + CHECK_TIMEOUT
        for source, url in (
            ("release", API_URL + "/releases/latest"),
            ("tag", API_URL + "/tags?per_page=1"),
            ("raw_version", RAW_URL),
        ):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                raw = _fetch(url, remaining)
                notes = ""
                release_url = REPO_URL
                if source == "release":
                    release = json.loads(raw)
                    latest = release["tag_name"].strip()
                    release_url = REPO_URL + "/releases/tag/" + quote(latest, safe="")
                    notes = (release.get("body") or "")[:2000]
                elif source == "tag":
                    latest = json.loads(raw)[0]["name"].strip()
                    release_url = REPO_URL + "/releases/tag/" + quote(latest, safe="")
                else:
                    latest = raw.strip()
                available = version_parts(latest) > version_parts(current)
                result.update(ok=True, latest=latest, update_available=available,
                              release_url=release_url, html_url=release_url,
                              notes=notes, source=source)
                break
            except Exception as exc:
                # 更新检查失败不能影响服务的其他功能。
                errors.append(f"{source}: {_error_reason(exc)}")
        if not result["ok"]:
            result["error"] = "暂时查不到更新；" + "；".join(errors)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        _cache = dict(result)
        # 失败短暂缓存，避免无网时每次会话都等待，且恢复网络后可及时重查。
        _cache_until = time.monotonic() + (CACHE_SECONDS if result["ok"] else 60)
        return result
