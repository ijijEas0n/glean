#!/usr/bin/env python3
"""「我的账号」：把自己在抖音/小红书的号登记下来，方便一键更新作品。

存哪：`~/.glean/accounts.json`（跟其他设置一起，权限 600）。
不写死在代码里——用户以后加号、换号都不用改程序。

每条账号只记最少的信息（平台、主页链接、显示名、上次更新时间）。
作品清单和内容本身都在本地库里（`library/items/`），这里不重复存。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from typing import Any

from .config import CONFIG_DIR
from .resolver import PLATFORM_LABELS, resolve

ACCOUNTS_PATH = CONFIG_DIR / "accounts.json"
_lock = threading.RLock()

# 支持的平台：目前只做抖音和小红书（用户明确的范围）
SUPPORTED = ("douyin", "xiaohongshu")


def _load() -> list[dict[str, Any]]:
    try:
        if ACCOUNTS_PATH.exists():
            data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
            rows = data.get("accounts") if isinstance(data, dict) else data
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _save(rows: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"accounts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCOUNTS_PATH)


def 猜名字(url: str, platform: str) -> str:
    """从链接里猜一个能认出来的名字，用户之后可以改。"""
    if platform == "xiaohongshu":
        m = re.search(r"/user/profile/([0-9a-fA-F]+)", url)
        return f"小红书号 {m.group(1)[:8]}" if m else "我的小红书"
    m = re.search(r"/user/([A-Za-z0-9_\-]+)", url)
    return f"抖音号 {m.group(1)[-6:]}" if m else "我的抖音"


def list_accounts() -> list[dict[str, Any]]:
    with _lock:
        return _load()


def get_account(account_id: str) -> dict[str, Any] | None:
    with _lock:
        return next((a for a in _load() if a.get("id") == account_id), None)


def add_account(url: str, name: str = "") -> dict[str, Any]:
    """登记一个账号。平台从链接自动认，认不出或不是主页就报错。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("请提供主页链接")
    resolved = resolve(url)
    if resolved.kind != "profile":
        raise ValueError(
            f"这条链接不是主页（认出来是「{resolved.kind_label}」）。"
            f"请粘博主主页的链接，形如 douyin.com/user/... 或 xiaohongshu.com/user/profile/..."
        )
    if resolved.platform not in SUPPORTED:
        raise ValueError(f"目前只支持抖音和小红书，这条认出来是「{resolved.platform_label}」")

    with _lock:
        rows = _load()
        # 同一个主页别登记两次
        已有 = next((a for a in rows if a.get("url") == resolved.url), None)
        if 已有:
            raise ValueError("这个主页已经登记过了")

        row = {
            "id": f"acc_{uuid.uuid4().hex[:10]}",
            "platform": resolved.platform,
            "platform_label": PLATFORM_LABELS.get(resolved.platform, resolved.platform),
            "name": (name or "").strip() or 猜名字(resolved.url, resolved.platform),
            "url": resolved.url,
            "ids": resolved.ids,          # 主页 ID，更新时直接用
            "added_at": _now(),
            "last_sync_at": "",
        }
        rows.append(row)
        _save(rows)
        return row


def update_account(account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        rows = _load()
        row = next((a for a in rows if a.get("id") == account_id), None)
        if not row:
            raise ValueError("找不到这个账号")
        if "name" in patch:
            row["name"] = str(patch["name"] or "").strip() or row["name"]
        if "url" in patch and str(patch["url"]).strip():
            resolved = resolve(str(patch["url"]).strip())
            if resolved.kind != "profile":
                raise ValueError("新链接不是主页")
            row["url"] = resolved.url
            row["platform"] = resolved.platform
            row["platform_label"] = PLATFORM_LABELS.get(resolved.platform, resolved.platform)
            row["ids"] = resolved.ids
        _save(rows)
        return row


def mark_synced(account_id: str) -> None:
    with _lock:
        rows = _load()
        row = next((a for a in rows if a.get("id") == account_id), None)
        if row:
            row["last_sync_at"] = _now()
            _save(rows)


def remove_account(account_id: str) -> bool:
    with _lock:
        rows = _load()
        剩 = [a for a in rows if a.get("id") != account_id]
        if len(剩) == len(rows):
            return False
        _save(剩)
        return True


def _now() -> str:
    from .store import now_iso

    return now_iso()


def summarize(account: dict[str, Any]) -> dict[str, Any]:
    """给看板用：把账号和本地库里的实际情况拼起来。

    「这个号有多少作品 / 本地已经有多少条」都来自主页条目（本地库是唯一真源），
    账号记录里不重复存这些——不然两边会不一致。
    """
    from . import store

    row = dict(account)
    row.setdefault("platform_label", PLATFORM_LABELS.get(str(account.get("platform")), ""))
    row["profile"] = None
    try:
        resolved = resolve(str(account.get("url") or ""))
        if resolved.kind == "profile":
            from .adapters.base import make_item_id

            # 主页条目的 id 规则和适配器保持一致，这里从 ids 反推
            key = (resolved.ids.get("sec_user_id") or resolved.ids.get("user_id") or "")
            item_id = make_item_id(resolved.platform, "profile", key)
            item = store.load_item(item_id)
            if item:
                作品 = item.get("children") or []
                已拿 = sum(1 for c in 作品 if c.get("local"))
                row["profile"] = {
                    "item_id": item_id,
                    "title": item.get("title") or "",
                    "author": (item.get("author") or {}).get("name") or "",
                    "avatar": (item.get("author") or {}).get("avatar") or "",
                    "works": len(作品),
                    "local": 已拿,
                    "total": (item.get("profile") or {}).get("aweme_count")
                             or (item.get("profile") or {}).get("note_count") or 0,
                    "fetched_at": item.get("fetched_at") or "",
                }
    except Exception:  # noqa: BLE001 - 看板不该因为一条算不出来就整体失败
        pass

    # ── 我发的内容，本地一共有多少条 ──
    # 靠 item 上的 author.id 认（比主页清单更可靠：单独抓的作品也算得进来）。
    # 注意：sec_uid 在不同来源长度不一样——主页链接里可能被截断（实测 72 字符），
    # 而作品接口返回的是完整的（76 字符）。所以按"前缀关系"比，不能要求全等。
    def 同一个人(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        # 短的那个必须是长的那个的前缀，且够长（避免误判）
        短, 长 = (a, b) if len(a) <= len(b) else (b, a)
        return len(短) >= 40 and 长.startswith(短)

    作者id = ""
    try:
        ids = resolve(str(account.get("url") or "")).ids
        作者id = str(ids.get("sec_user_id") or ids.get("user_id") or "")
    except Exception:  # noqa: BLE001
        pass
    row["local_items"] = []
    if 作者id:
        for d in _iter_item_ids():
            item = store.load_item(d)
            if not item or item.get("kind") == "profile":
                continue
            if 同一个人(str((item.get("author") or {}).get("id") or ""), 作者id):
                row["local_items"].append({
                    "id": d,
                    "title": item.get("title") or "",
                    "kind": item.get("kind") or "",
                    "duration": item.get("duration"),
                    "fetched_at": item.get("fetched_at") or "",
                    "stats": item.get("stats") or {},
                })
        row["local_items"].sort(key=lambda x: str(x.get("fetched_at") or ""), reverse=True)
    row["local_count"] = len(row["local_items"])
    return row


def cached_works(account_id: str) -> dict[str, Any] | None:
    """只从本地主页条目和作品条目拼出博主作品列表，不访问平台。"""
    from . import store

    account = get_account(account_id)
    if not account:
        return None
    try:
        resolved = resolve(str(account.get("url") or ""))
    except ValueError:
        return {"account": account, "works": [], "fetched_at": ""}
    author_id = str(resolved.ids.get("sec_user_id") or resolved.ids.get("user_id") or "")

    def same_person(candidate: str) -> bool:
        if not candidate or not author_id:
            return False
        if candidate == author_id:
            return True
        short, long = (candidate, author_id) if len(candidate) <= len(author_id) else (author_id, candidate)
        return len(short) >= 40 and long.startswith(short)

    from .adapters.base import make_item_id
    profile_id = make_item_id(resolved.platform, "profile", author_id)
    profile = store.load_item(profile_id) or {}
    works: dict[str, dict[str, Any]] = {}
    for child in profile.get("children") or []:
        if not isinstance(child, dict) or not child.get("id"):
            continue
        row = dict(child)
        local_id = store.child_item_id(row, resolved.platform)
        local_dir = store.item_dir(local_id) if local_id else None
        row["local"] = bool(local_dir and (local_dir / "item.json").is_file())
        if row["local"]:
            row["item_id"] = local_id
        works[str(row["id"])] = row

    for item_dir in store.ITEMS_DIR.iterdir() if store.ITEMS_DIR.is_dir() else []:
        if not item_dir.is_dir():
            continue
        item = store.load_item(item_dir.name)
        if not item or item.get("kind") == "profile":
            continue
        if not same_person(str((item.get("author") or {}).get("id") or "")):
            continue
        media = item.get("media") or []
        cover = next((m for m in media if m.get("role") == "cover" and m.get("path")), None)
        if cover:
            filename = str(cover["path"]).split("/")[-1]
            cover_url = f"/api/media/{item_dir.name}/{filename}"
        else:
            cover_url = ""
        existing_key = next((key for key, child in works.items()
                             if key == item_dir.name or
                             (str(child.get("biz_id") or "") and item_dir.name.endswith("-" + str(child.get("biz_id"))))),
                            item_dir.name)
        existing = works.get(existing_key, {})
        works[item_dir.name] = {
            **existing,
            "id": item_dir.name,
            "title": item.get("title") or existing.get("title") or "",
            "kind": item.get("kind") or existing.get("kind") or "",
            "duration": item.get("duration"),
            "stats": item.get("stats") or existing.get("stats") or {},
            "created_at": item.get("created_at") or existing.get("created_at") or "",
            "url": item.get("url") or existing.get("url") or "",
            "cover": cover_url or existing.get("cover") or "",
            "local": True,
        }
        if existing_key != item_dir.name:
            works.pop(existing_key, None)
    result = list(works.values())
    result.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return {"account": account, "works": result, "profile_id": profile_id,
            "fetched_at": str(profile.get("fetched_at") or "")}


def _iter_item_ids() -> list[str]:
    from . import store

    try:
        return [p.name for p in store.ITEMS_DIR.iterdir() if p.is_dir()]
    except OSError:
        return []


def summarize_all() -> list[dict[str, Any]]:
    return [summarize(a) for a in list_accounts()]
