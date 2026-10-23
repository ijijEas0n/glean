#!/usr/bin/env python3
"""适配器注册表。

`get_adapter(resolved)` 返回一个可调用对象：`adapter(resolved, ctx) -> item dict`。
没写适配器的平台一律走 generic（yt-dlp 兜底），绝不静默失败。
"""

from __future__ import annotations

from typing import Any, Callable

from ..resolver import Resolved
from .base import FetchContext, empty_item, make_item_id  # noqa: F401 - 供适配器共用

AdapterFn = Callable[[Resolved, FetchContext], dict[str, Any]]

# (platform, kind) → (模块名, 函数名)。kind 用 "*" 表示该平台的所有类型。
_REGISTRY: dict[tuple[str, str], tuple[str, str]] = {
    ("douyin", "video"): ("douyin", "fetch_one"),
    ("douyin", "images"): ("douyin", "fetch_one"),
    ("douyin", "unknown"): ("douyin", "fetch_one"),
    ("douyin", "work"): ("douyin", "fetch_one"),
    ("douyin", "article"): ("douyin", "fetch_article"),
    ("douyin", "profile"): ("douyin", "fetch_profile"),

    ("xiaohongshu", "video"): ("xiaohongshu", "fetch_note"),
    ("xiaohongshu", "images"): ("xiaohongshu", "fetch_note"),
    ("xiaohongshu", "text"): ("xiaohongshu", "fetch_note"),
    ("xiaohongshu", "unknown"): ("xiaohongshu", "fetch_note"),
    ("xiaohongshu", "work"): ("xiaohongshu", "fetch_note"),
    ("xiaohongshu", "profile"): ("xiaohongshu", "fetch_profile"),

    ("twitter", "text"): ("twitter", "fetch_post"),
    ("twitter", "article"): ("twitter", "fetch_post"),
    ("twitter", "images"): ("twitter", "fetch_post"),
    ("twitter", "video"): ("twitter", "fetch_post"),
    ("twitter", "work"): ("twitter", "fetch_post"),
    ("twitter", "unknown"): ("twitter", "fetch_post"),
    ("twitter", "profile"): ("twitter", "fetch_profile"),


    ("wechat_mp", "article"): ("wechat_mp", "fetch_article"),
    ("wechat_mp", "unknown"): ("wechat_mp", "fetch_article"),

    ("bilibili", "video"): ("bilibili", "fetch_video"),
    ("bilibili", "unknown"): ("bilibili", "fetch_video"),
    ("bilibili", "profile"): ("bilibili", "fetch_profile"),

    ("weibo", "text"): ("weibo", "fetch_post"),
    ("weibo", "unknown"): ("weibo", "fetch_post"),
    ("weibo", "profile"): ("weibo", "fetch_profile"),

    ("zhihu", "article"): ("zhihu", "fetch_post"),
    ("zhihu", "text"): ("zhihu", "fetch_post"),
    ("zhihu", "unknown"): ("zhihu", "fetch_post"),

    ("youtube", "video"): ("youtube", "fetch_video"),
    ("youtube", "unknown"): ("youtube", "fetch_video"),
    ("youtube", "profile"): ("youtube", "fetch_profile"),

    ("kuaishou", "video"): ("kuaishou", "fetch_video"),
    ("kuaishou", "unknown"): ("kuaishou", "fetch_video"),
    ("kuaishou", "profile"): ("kuaishou", "fetch_profile"),

    ("tiktok", "video"): ("tiktok", "fetch_video"),
    ("tiktok", "unknown"): ("tiktok", "fetch_video"),
    ("tiktok", "profile"): ("tiktok", "fetch_profile"),

    ("instagram", "video"): ("instagram", "fetch_post"),
    ("instagram", "images"): ("instagram", "fetch_post"),
    ("instagram", "text"): ("instagram", "fetch_post"),
    ("instagram", "unknown"): ("instagram", "fetch_post"),
    ("instagram", "profile"): ("instagram", "fetch_profile"),
}


def _load(module_name: str, function_name: str) -> AdapterFn:
    from importlib import import_module

    module = import_module(f".{module_name}", package=__name__)
    function = getattr(module, function_name, None)
    if function is None:
        raise RuntimeError(f"适配器 {module_name}.{function_name} 不存在")
    return function  # type: ignore[return-value]


def resolve_target(resolved: Resolved) -> tuple[str, str, bool]:
    """返回 (模块名, 函数名, 是否是兜底)。"""
    key = (resolved.platform, resolved.kind)
    if key in _REGISTRY:
        module_name, function_name = _REGISTRY[key]
        return module_name, function_name, False
    fallback_key = (resolved.platform, "*")
    if fallback_key in _REGISTRY:
        module_name, function_name = _REGISTRY[fallback_key]
        return module_name, function_name, False
    return "generic", "fetch", True


def get_adapter(resolved: Resolved) -> AdapterFn:
    module_name, function_name, _ = resolve_target(resolved)
    try:
        return _load(module_name, function_name)
    except (ImportError, AttributeError, RuntimeError):
        # 该平台适配器还没写好：退回通用抓取，而不是让整条任务失败
        if module_name != "generic":
            return _load("generic", "fetch")
        raise


def supported_platforms() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for platform, kind in _REGISTRY:
        rows.setdefault(platform, [])
        if kind not in rows[platform]:
            rows[platform].append(kind)
    return rows
