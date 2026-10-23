#!/usr/bin/env python3
"""TikHub 客户端。

三件必须做对的事（都是实测踩出来的）：
1. UA 必须是浏览器样式，否则 Cloudflare 返回 `403 error code: 1010`。
2. api.tikhub.io 在本机直连被 reset，必须走本机代理；代理端口自动探测并缓存。
3. 业务错误藏在 HTTP 200 里（响应体 code != 200），必须查 body 里的 code。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from .config import BROWSER_UA, PROXY_CANDIDATES, load_config, save_config, tikhub_api_key

API_ROOT = "https://api.tikhub.io"
MAX_ATTEMPTS = 3
RETRY_DELAYS = (2, 6)
RETRYABLE_STATUS = frozenset({400, 408, 425, 429, 500, 501, 502, 503, 504})
TIMEOUT = 60

_lock = threading.RLock()
_proxy_state: dict[str, Any] = {"url": "", "ok": False, "checked_at": 0.0, "latency_ms": 0, "message": ""}

# TikHub 账号状态：记最近一次真实调用的结果，界面据此提前提醒（别等用户粘完链接才报错）
_account_state: dict[str, Any] = {"ok": None, "kind": "", "note": ""}


def account_state() -> dict[str, Any]:
    return dict(_account_state)


def _remember_account(ok: bool, kind: str = "", note: str = "") -> None:
    _account_state["ok"] = ok
    _account_state["kind"] = kind
    _account_state["note"] = note


class TikHubError(RuntimeError):
    """带人话说明的 TikHub 调用失败。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0, body: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.body = body


# ---- 代理探测 ----


def _port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _build_opener(proxy: str) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})  # 显式直连，忽略系统代理设置
    return urllib.request.build_opener(handler)


def _probe_tikhub(proxy: str, key: str, timeout: int = 20) -> tuple[bool, int, str]:
    """用一次真实请求判断这个代理能不能打通 TikHub。返回 (可用, 耗时ms, 说明)。"""
    url = f"{API_ROOT}/api/v1/tikhub/user/get_user_info"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "User-Agent": BROWSER_UA, "Accept": "application/json"}
    )
    started = time.time()
    try:
        with _build_opener(proxy).open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:200].decode("utf-8", "replace")
        if exc.code == 403 and "1010" in raw:
            return False, 0, "被 Cloudflare 拦截（UA 问题）"
        return False, 0, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 探测阶段任何异常都只是"这条路不通"
        return False, 0, f"{type(exc).__name__}: {exc}"
    latency = int((time.time() - started) * 1000)
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return False, latency, f"接口返回 code={payload.get('code') if isinstance(payload, dict) else '?'}"
    return True, latency, "ok"


def detect_proxy(force: bool = False, key: str | None = None) -> dict[str, Any]:
    """探测可用代理并缓存。

    顺序：显式配置（"direct" = 强制直连）→ 环境变量 → 上次成功的 → 常见端口。
    探测结果写入 ~/.glean/config.json 的 last_proxy_ok，下次启动先试它。
    """
    key = key or tikhub_api_key() or ""
    with _lock:
        if not force and _proxy_state["ok"] and time.time() - _proxy_state["checked_at"] < 300:
            return dict(_proxy_state)

    configured = str(load_config().get("proxy") or "").strip()
    cached = str(load_config().get("last_proxy_ok") or "").strip()

    candidates: list[str] = []
    if configured == "direct":
        candidates = [""]
    elif configured:
        candidates = [configured]
    else:
        for value in (cached,):
            if value and value not in candidates:
                candidates.append(value)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = __import__("os").environ.get(name, "").strip()
            if value and value not in candidates:
                candidates.append(value)
        candidates.append("")  # 最后试直连
        for url in PROXY_CANDIDATES:
            port = int(url.rsplit(":", 1)[1])
            if _port_open("127.0.0.1", port) and url not in candidates:
                candidates.append(url)

    if not key:
        with _lock:
            _proxy_state.update(
                {"url": "", "ok": False, "checked_at": time.time(), "latency_ms": 0,
                 "message": "未配置 TikHub Key"}
            )
            return dict(_proxy_state)

    last_message = "没有可用的网络路径"
    for proxy in candidates:
        ok, latency, message = _probe_tikhub(proxy, key)
        if ok:
            with _lock:
                _proxy_state.update(
                    {"url": proxy, "ok": True, "checked_at": time.time(),
                     "latency_ms": latency, "message": "直连可用" if not proxy else f"经代理 {proxy}"}
                )
            if proxy != cached:
                save_config({"last_proxy_ok": proxy})
            return dict(_proxy_state)
        last_message = f"{proxy or '直连'} → {message}"

    with _lock:
        _proxy_state.update(
            {"url": "", "ok": False, "checked_at": time.time(), "latency_ms": 0, "message": last_message}
        )
        return dict(_proxy_state)


def proxy_state() -> dict[str, Any]:
    with _lock:
        return dict(_proxy_state)


def current_proxy() -> str:
    """取当前应使用的代理；未探测过则先探测。"""
    with _lock:
        if _proxy_state["checked_at"] and time.time() - _proxy_state["checked_at"] < 300:
            return str(_proxy_state["url"])
    return str(detect_proxy().get("url") or "")


def opener_for_download() -> urllib.request.OpenerDirector:
    """下载媒体用：先按当前代理，失败由调用方回落直连。"""
    return _build_opener(current_proxy())


# ---- 请求 ----


def _explain_http(code: int, body: str) -> str:
    """把 TikHub 的 HTTP 错误翻成人话 + 能直接照做的下一步。"""
    low = body.lower()
    if code == 402 or "insufficient balance" in low:
        return ("TikHub 账号余额不足（接口返回 402）。"
                "到 tikhub.io 充值后即可继续。"
                "注意：部分接口只扣付费余额，免费额度不能抵扣。")
    if code == 401 or code == 403 and "1010" not in low:
        return ("TikHub 拒绝了这次请求（鉴权失败）。"
                "检查 Key 是否有效：echo $TIKHUB_API_KEY")
    if code == 429:
        return "TikHub 限流了（请求太频繁），等一会儿再试。"
    if code >= 500:
        return f"TikHub 服务端出错（HTTP {code}），通常等一下就好。"
    return f"TikHub 请求失败（HTTP {code}）：{body[:180] or '无响应内容'}"


def _compact_error(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message_zh", "message", "detail", "msg", "error"):
            value = payload.get(key)
            if value:
                return str(value)[:200]
        data = payload.get("data")
        if isinstance(data, dict):
            return str(data)[:200]
    return str(payload)[:200]


def request(
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    *,
    method: str | None = None,
    timeout: int = TIMEOUT,
) -> dict[str, Any]:
    """调用 TikHub。成功返回完整响应体（含 data）；失败抛 TikHubError。"""
    key = tikhub_api_key()
    if not key:
        raise TikHubError(
            "未配置 TikHub Key。可在设置里填写，或执行：\n"
            '在仓库根目录 .env 里填写 TIKHUB_API_KEY=<你的key> 后重启服务'
        )

    verb = (method or ("POST" if body is not None else "GET")).upper()
    url = f"{API_ROOT}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url = f"{url}?{urlencode(clean)}"

    payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last_error = "未知错误"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        proxy = current_proxy()
        headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
        }
        if payload_bytes is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=verb)
        try:
            with _build_opener(proxy).open(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code == 403 and "1010" in raw:
                # UA 被 Cloudflare 拦：换代理重试通常无用，直接报清楚
                raise TikHubError(
                    "TikHub 被 Cloudflare 拦截（error code 1010）。通常是请求头被改写或网络中间层注入，"
                    "请检查代理设置。",
                    status=403, body=raw[:300],
                ) from exc
            if exc.code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                last_error = f"HTTP {exc.code}"
                time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            if exc.code == 402 or "insufficient balance" in raw.lower():
                _remember_account(False, "balance", "TikHub 余额不足")
            elif exc.code in (401, 403):
                _remember_account(False, "auth", "TikHub Key 鉴权失败")
            raise TikHubError(_explain_http(exc.code, raw), status=exc.code, body=raw[:300]) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            # 网络路径可能变了（代理挂了 / 直连通了），强制重探一次
            if attempt < MAX_ATTEMPTS:
                last_error = f"{type(exc).__name__}"
                detect_proxy(force=True, key=key)
                time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            raise TikHubError(
                f"TikHub 连不上（{type(exc).__name__}）。请检查代理是否开启，"
                f"或在设置里点「重新探测网络」。最后一次错误：{exc}"
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TikHubError(f"TikHub 返回的不是 JSON：{raw[:200]}") from exc
        if not isinstance(payload, dict):
            raise TikHubError("TikHub 返回格式不正确（不是对象）")

        code = payload.get("code")
        if code == 200:
            _remember_account(True)
            return payload
        business = str(code or "")
        if business.isdigit() and int(business) in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
            last_error = f"业务码 {business}"
            time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
            continue
        raise TikHubError(
            f"TikHub 未返回数据（代码 {business or '未知'}）：{_compact_error(payload)}"
        )

    raise TikHubError(f"TikHub 请求重试耗尽：{last_error}")


def get(path: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return request(path, params=params, **kwargs)


def post(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return request(path, body=body or {}, **kwargs)


def data_of(payload: dict[str, Any]) -> Any:
    """取 data 段；缺失时抛人话错误。"""
    data = payload.get("data")
    if data is None:
        raise TikHubError(f"TikHub 响应里没有 data 段：{_compact_error(payload)}")
    return data


# ---- 媒体下载（不走 TikHub，但要走同一条网络路径） ----


def download(url: str, target, *, timeout: int = 120, referer: str = "") -> int:
    """下载一个媒体地址到 target（Path）。失败抛异常。返回字节数。

    先按当前代理下载，失败再回落直连——视频 CDN 常常直连比代理快。
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    errors: list[str] = []
    for proxy in (current_proxy(), ""):
        request_obj = urllib.request.Request(url, headers=headers)
        try:
            with _build_opener(proxy).open(request_obj, timeout=timeout) as response:
                total = 0
                with open(target, "wb") as output:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        total += len(chunk)
            if total == 0:
                raise RuntimeError("响应为空")
            return total
        except Exception as exc:  # noqa: BLE001 - 逐个路径试
            errors.append(f"{proxy or '直连'}:{type(exc).__name__}")
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
    raise RuntimeError("媒体下载失败（" + "，".join(errors) + "）")
