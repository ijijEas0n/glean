#!/usr/bin/env python3
"""Glean 配置与凭证读取。

优先级（Key）：环境变量 → ~/.glean/config.json。
配置项（代理、转写引擎等）写在 ~/.glean/config.json，权限 600。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

# ---- 路径 ----

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"
# 库放哪。支持环境变量覆盖——**跑测试/试抓时必须指向临时目录**，
# 否则测试数据会混进用户的真实库（已经出过一次事故）。
LIBRARY_ROOT = Path(os.environ.get("GLEAN_LIBRARY_DIR") or (PROJECT_ROOT / "library"))
FIXTURES_ROOT = PROJECT_ROOT / ".fixtures"

# 配置目录。和库一样支持环境变量覆盖——**跑测试时必须一起隔离**，
# 否则临时实例会把用户的真实设置改掉（已经发生过一次：测试实例把转写引擎改了）。
CONFIG_DIR = Path(os.environ.get("GLEAN_CONFIG_DIR") or (Path.home() / ".glean"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# 密钥只从环境变量（或本机 ~/.glean/config.json 的设置面板）读取，仓库里不存任何密钥。

# TikHub 在 Cloudflare 后面：非浏览器 UA 会被 403 error code 1010 拦掉。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 本机代理探测顺序：Clash Verge 默认 7897 排第一（实测可用）。
PROXY_CANDIDATES = (
    "http://127.0.0.1:7897",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:1087",
    "http://127.0.0.1:7891",
    "http://127.0.0.1:6152",
    "http://127.0.0.1:10808",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "proxy": "",              # 空 = 自动探测；填 "direct" 强制直连
    "transcribe_engine": "volcengine",   # volcengine | aliyun | local | off
    # 阿里云用的模型。**必须是异步 filetrans 那类**——同步模型（qwen-audio-3.1-asr-flash）
    # 提交到异步端点会报 "url error"。filetrans 支持 12 小时、时间戳固定开启。
    "aliyun_asr_model": "qwen-audio-3.1-asr-flash-filetrans",
    "transcribe_fallback_local": True,   # 火山不可用时自动改用本机模型（会写明，不是静默降级）
    "keep_video": True,       # 转写完成后是否保留视频文件
    # 视频下载的分辨率上限。X 的 variants 里最高那档常常就是「原始上传档」：
    # 实测一条 4h23m 的长访谈，1080p 档是 3.58GB，下载要一小时、下完还会被
    # read_bytes 读进内存撑爆。所以按分辨率封顶挑档，不再无脑取最高码率。
    # 720 = 取不超过 720p 的最高档。填 0 表示不封顶（要原画时再改）。
    "video_max_height": 720,
    "download_media": True,
    "default_limit": 20,      # 主页类默认抓取条数，0 = 全部
    "last_proxy_ok": "",      # 上次探测到的可用代理（缓存，写回文件）
    # 三个 Key 由设置面板写入（POST /api/config）。必须出现在这里，
    # 否则 save_config 的「只接受已知键」会把它们静默丢掉——
    # 用户填完 key、界面说"设置好了"、实际一个字节都没存（踩过）。
    # 留空 = 这个 key 不设置，视为未设置，不是清空。
    "tikhub_api_key": "",
    "volcengine_api_key": "",
    "aliyun_api_key": "",
    # 「收藏夹」栏目的同步脚本（手动更新按钮会跑它）。留空 = 用这个默认位置。
    "favorites_sync_script": "~/Documents/自媒体系统/抖音收藏夹同步/同步.py",
}

_lock = threading.RLock()
_config_cache: dict[str, Any] | None = None
_key_cache: dict[str, str | None] = {}


def _resolve_key(env_var: str, config_key: str) -> tuple[str, str]:
    """取一个 Key，返回 (值, 来源)。

    顺序固定为 **环境变量 → 配置文件**。
    留空视为"没设置"，自动回落下一档。
    """
    key = os.environ.get(env_var, "").strip()
    if key:
        return key, "env"
    key = str(load_config().get(config_key) or "").strip()
    if key:
        return key, "config"
    return "", ""


def tikhub_api_key() -> str | None:
    if "tikhub" not in _key_cache:
        key, source = _resolve_key(
            "TIKHUB_API_KEY", "tikhub_api_key")
        _key_cache["tikhub"] = key or None
        _key_cache["tikhub_source"] = source
    return _key_cache["tikhub"]


def tikhub_key_source() -> str:
    tikhub_api_key()
    return str(_key_cache.get("tikhub_source") or "")


def aliyun_api_key() -> str | None:
    """阿里云百炼 DashScope Key。"""
    if "aliyun" not in _key_cache:
        key, _ = _resolve_key("DASHSCOPE_API_KEY", "aliyun_api_key")
        _key_cache["aliyun"] = key or None
    return _key_cache["aliyun"]


def volcengine_api_key() -> str | None:
    """火山转写 Key。"""
    if "volc" not in _key_cache:
        key, _ = _resolve_key("VOLCENGINE_ASR_API_KEY", "volcengine_api_key")
        _key_cache["volc"] = key or None
    # 必须从缓存取：key 只在 if 块里绑定，缓存命中时取它会 UnboundLocalError（踩过）
    return _key_cache["volc"]


def clear_key_cache() -> None:
    with _lock:
        _key_cache.clear()


# ---- 配置文件 ----


def load_config() -> dict[str, Any]:
    global _config_cache
    with _lock:
        if _config_cache is not None:
            return dict(_config_cache)
        data: dict[str, Any] = {}
        try:
            if CONFIG_PATH.exists():
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
        merged = {**DEFAULT_CONFIG, **data}
        _config_cache = merged
        return dict(merged)


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入配置；只接受已知键，避免脏数据。"""
    global _config_cache
    with _lock:
        current = load_config()
        for key, value in patch.items():
            if key in DEFAULT_CONFIG:
                current[key] = value
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
        _config_cache = current
        # Key 可能被改（或清空），立刻让下次取 key 走新值，
        # 不靠调用方记得来清缓存——漏一次就是"填了没生效"。
        _key_cache.clear()
        return dict(current)
