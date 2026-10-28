"""低粉爆款候选筛选（非预测）。

抖音互动仅点赞，小红书互动为点赞+收藏。倍数=互动/max(粉丝,1)。
评分纯函数不访问网络；拉源、补粉、查本地存在性单独处理。
未知粉丝一律丢弃；缺失发布时间默认跳过（可配置放行），不从作品 ID 猜时间。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import os
import re
from typing import Any

from . import store, tikhub
from .config import load_config, tikhub_api_key
from .resolver import resolve
from urllib.parse import urlsplit, urlencode

PLATFORMS = ("douyin", "xiaohongshu")
DEFAULTS = {
    "enabled": True,
    "max_calls": 24,
    "douyin": {
        "max_fans": 50000, "min_interactions": 10000, "min_multiplier": 3,
        "max_age_days": 14, "allow_missing_date": False, "keywords": ["AI"],
        "source": "lowfan",
        "path": "/api/v1/douyin/billboard/fetch_hot_total_low_fan_list",
        "user_path": "/api/v1/douyin/app/v3/handler_user_profile",
    },
    "xiaohongshu": {
        "max_fans": 10000, "min_interactions": 1000, "min_multiplier": 1,
        "max_age_days": 14, "allow_missing_date": False, "keywords": ["AI"],
        "path": "/api/v1/xiaohongshu/app_v2/search_notes",
        "user_path": "/api/v1/xiaohongshu/app_v2/get_user_info",
    },
}
THRESHOLDS = ("max_fans", "min_interactions", "min_multiplier", "max_age_days")


def count(value: Any) -> int | None:
    """展示数字兼容万/千；缺失、非法、负值保持未知，不能伪装成零粉。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").rstrip("+")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([万亿千wWkK]?)", text)
    if not match:
        return None
    number = float(match[1]) * {"万": 10000, "亿": 100000000, "千": 1000,
                              "w": 10000, "W": 10000, "k": 1000, "K": 1000}.get(match[2], 1)
    return int(number) if math.isfinite(number) else None


def published(value: Any) -> datetime | None:
    try:
        if not value:
            return None
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            stamp = float(value)
            return datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc)
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def score_item(item: dict, rules: dict | None = None, *, now: datetime | None = None) -> dict:
    """返回新对象；抖点赞、红赞+藏；比较未经四舍五入的倍数与天数。"""
    platform = item.get("platform")
    if platform not in PLATFORMS:
        raise ValueError("低粉爆款仅支持抖音和小红书")
    rules = {**DEFAULTS[platform], **(rules or {})}
    fans, likes = count(item.get("fans")), count(item.get("likes"))
    collects = count(item.get("collects"))
    interactions = likes if platform == "douyin" else (
        likes + collects if likes is not None and collects is not None else None)
    multiplier = interactions / max(fans, 1) if fans is not None and interactions is not None else None
    stamp = published(item.get("published_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - stamp).total_seconds() / 86400 if stamp else None
    passed = (fans is not None and interactions is not None and multiplier is not None
              and fans <= rules["max_fans"] and interactions >= rules["min_interactions"]
              and multiplier >= rules["min_multiplier"]
              and ((age is None and rules["allow_missing_date"])
                   or (age is not None and 0 <= age <= rules["max_age_days"])))
    return {**item, "fans": fans, "interactions": interactions, "multiplier": multiplier,
            "published_at": stamp.isoformat() if stamp else None, "age_days": age, "passed": bool(passed)}


def passes_threshold(item: dict, rules: dict | None = None, *, now: datetime | None = None) -> bool:
    return score_item(item, rules, now=now)["passed"]


def prepare(payload: dict) -> dict:
    """在创建异步任务前验证全部参数，拒绝无界请求和跨域 path。"""
    config = load_config().get("viral", {})
    if not isinstance(config, dict):
        raise ValueError("低粉爆款配置必须是对象")
    result = deepcopy(DEFAULTS)
    result.update({k: v for k, v in config.items() if k not in PLATFORMS})
    enabled_env = os.environ.get("GLEAN_VIRAL_ENABLED", "").lower()
    if not result["enabled"] or enabled_env in ("0", "false", "off"):
        raise ValueError("低粉爆款功能已关闭")
    platforms = payload.get("platforms", list(PLATFORMS))
    if not isinstance(platforms, list) or not platforms or any(p not in PLATFORMS for p in platforms):
        raise ValueError("platforms 请选择 douyin / xiaohongshu")
    result["platforms"] = list(dict.fromkeys(platforms))
    limit = payload.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit 必须是 1–100 的整数")
    result["limit"] = limit
    cap = result["max_calls"]
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 50:
        raise ValueError("viral.max_calls 必须是 1–50 的整数")
    overrides = payload.get("thresholds", {})
    if not isinstance(overrides, dict) or any(k not in PLATFORMS for k in overrides):
        raise ValueError("thresholds 必须按平台提供")
    for p in PLATFORMS:
        saved, patch = config.get(p, {}), overrides.get(p, {})
        if not isinstance(saved, dict) or not isinstance(patch, dict):
            raise ValueError("平台配置与门槛必须是对象")
        if any(k not in (*THRESHOLDS, "allow_missing_date") for k in patch):
            raise ValueError("未知门槛字段")
        rule = {**DEFAULTS[p], **saved, **patch}
        for k in THRESHOLDS:
            v = rule[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{p}.{k} 必须是非负有限数字")
        if not isinstance(rule["allow_missing_date"], bool):
            raise ValueError("allow_missing_date 必须是布尔值")
        keywords = payload.get("keywords", rule["keywords"])
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5 or any(
            not isinstance(k, str) or not k.strip() or len(k) > 80 for k in keywords
        ):
            raise ValueError("keywords 需为 1–5 个非空关键词，每个最多 80 字")
        rule["keywords"] = list(dict.fromkeys(k.strip() for k in keywords))
        for k in ("path", "user_path"):
            if not isinstance(rule[k], str) or not re.fullmatch(rf"/api/v1/{p}/[A-Za-z0-9_/]+", rule[k]):
                raise ValueError(f"{p}.{k} 必须是该平台的 TikHub API path")
        if p == "douyin" and rule["source"] not in ("lowfan", "search"):
            raise ValueError("douyin.source 必须是 lowfan / search")
        result[p] = rule
    if not tikhub_api_key():
        raise tikhub.TikHubError("未配置 TikHub API Key，请前往设置填写；不要发送到聊天")
    return result


def pick(obj: dict, *keys: str) -> Any:
    return next((obj[k] for k in keys if obj.get(k) is not None), None)


def rows_from(payload: dict) -> list[dict]:
    """只遍历已知列表包装，不递归搜任意 id，以免把作者/广告算成作品。

    端点有公开文档；具体响应包装与字段兼容仍是假设/待实测。
    未识别的结构显式报错，不能伪装成“没有候选”。
    """
    data = tikhub.data_of(payload)
    for _ in range(5):
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            break
        key = next((k for k in ("items", "objs", "notes", "aweme_list", "business_data", "list", "data")
                    if isinstance(data.get(k), (dict, list))), None)
        if key is None:
            break
        data = data[key]
    raise tikhub.TikHubError("低粉爆款数据结构未识别，请核对端点与响应格式（待实测）")


def normalize(platform: str, obj: dict) -> dict | None:
    obj = obj.get("aweme_info") or obj.get("note_card") or obj.get("note") or obj
    if not isinstance(obj, dict):
        return None
    biz = str(pick(obj, "aweme_id", "note_id", "item_id", "id") or "")
    pattern = r"\d+" if platform == "douyin" else r"[a-fA-F0-9]{24}"
    if not re.fullmatch(pattern, biz):
        return None
    if platform == "xiaohongshu":
        biz = biz.lower()
    author = obj.get("author") or obj.get("user") or {}
    if not isinstance(author, dict):
        author = {}
    stats = obj.get("statistics") or obj.get("interact_info") or obj
    if not isinstance(stats, dict):
        stats = {}
    url = (f"https://www.douyin.com/video/{biz}" if platform == "douyin"
           else f"https://www.xiaohongshu.com/explore/{biz}")
    if platform == "xiaohongshu" and obj.get("xsec_token"):
        url += "?" + urlencode({"xsec_token": str(obj["xsec_token"]), "xsec_source": "pc_search"})
    return {"platform": platform, "id": biz, "key": f"{platform}:{biz}",
            "url": url,
            "title": str(pick(obj, "item_title", "title", "display_title", "desc") or "")[:500],
            "author": str(pick(author, "nickname", "nick_name", "name") or obj.get("nick_name") or ""),
            "author_id": str(pick(author, "sec_uid", "userid", "user_id", "uid", "id") or ""),
            "fans": count(pick(obj, "fans_cnt", "fans")) if platform == "douyin" and any(k in obj for k in ("fans_cnt", "fans"))
                    else count(pick(author, "follower_count", "fans", "fans_count", "followers")),
            "likes": count(pick(stats, "digg_count", "like_cnt", "liked_count", "likes")),
            "collects": count(pick(stats, "collected_count", "collected", "collect_count", "collects")),
            "published_at": pick(obj, "publish_time", "create_time", "time", "timestamp", "published_at"),
            "raw": {"id": biz, "type": str(obj.get("type") or ""),
                    "metrics_source": "search_or_lowfan", "fans_source": "search",
                    "likes": count(pick(stats, "digg_count", "like_cnt", "liked_count", "likes")),
                    "collects": count(pick(stats, "collected_count", "collected", "collect_count", "collects"))}}


def profile_fans(payload: dict) -> int | None:
    data = tikhub.data_of(payload)
    for _ in range(5):
        if not isinstance(data, dict):
            return None
        value = pick(data, "follower_count", "fans", "fans_count", "followers")
        if value is not None:
            return count(value)
        interactions = data.get("interactions", [])
        if isinstance(interactions, dict):
            interactions = interactions.get("list", [])
        if isinstance(interactions, list):
            for row in interactions:
                if isinstance(row, dict) and row.get("type") == "fans":
                    return count(row.get("count"))
        data = next((data[k] for k in ("data", "user", "user_info") if isinstance(data.get(k), dict)), None)
    return None


def preview(settings: dict, progress=None) -> dict:
    """每平台每关键词仅首屏，最多扫描 200 条；补粉缓存仅本次任务有效。"""
    calls, scanned = 0, 0
    warnings, candidates, seen, fans_cache = [], [], set(), {}

    def call(path, params, *, post=False):
        nonlocal calls
        if calls >= settings["max_calls"]:
            raise ValueError("已达本次 TikHub 调用上限，结果可能不完整")
        calls += 1
        return tikhub.post(path, params) if post else tikhub.get(path, params)

    for platform in settings["platforms"]:
        rule = settings[platform]
        for keyword in rule["keywords"]:
            if calls >= settings["max_calls"] or scanned >= 200:
                warnings.append("已达调用或扫描上限，结果可能不完整")
                break
            if progress:
                progress("正在筛选候选", min(90, 10 + calls), f"{platform} · {keyword}")
            if platform == "douyin":
                query = ({"page": 1, "page_size": 50, "date_window": 168, "keyword": keyword}
                         if rule["source"] == "lowfan" else {"keyword": keyword, "cursor": 0})
            else:
                query = {"keyword": keyword, "page": 1, "sort_type": "popularity_descending"}
            try:
                rows = rows_from(call(rule["path"], query, post=platform == "douyin"))
            except tikhub.TikHubError as exc:
                # 鉴权/余额/网络问题要成为任务失败，避免用户误认成“零候选”。
                raise tikhub.TikHubError(f"{platform} 拉源失败：{exc}") from exc
            for obj in rows[:200 - scanned]:
                scanned += 1
                item = normalize(platform, obj)
                if not item or item["key"] in seen:
                    continue
                seen.add(item["key"])
                # 先按互动和时效排除，省下无意义的补粉调用。
                probe = score_item({**item, "fans": 0}, {**rule, "min_multiplier": 0})
                if probe["interactions"] is None or probe["interactions"] < rule["min_interactions"]:
                    continue
                if not probe["passed"]:
                    continue
                if platform == "xiaohongshu" or item["fans"] is None:
                    author_id = item["author_id"]
                    if not author_id:
                        warnings.append(f"{item['key']} 缺作者 ID，已跳过")
                        continue
                    cache_key = (platform, author_id)
                    if cache_key not in fans_cache:
                        try:
                            param = "user_id" if platform == "xiaohongshu" else "sec_user_id"
                            fans_cache[cache_key] = profile_fans(call(rule["user_path"], {param: author_id}))
                        except (tikhub.TikHubError, ValueError) as exc:
                            warnings.append(f"{platform} 补粉失败，跳过该作者：{exc}")
                            fans_cache[cache_key] = None
                    item["fans"] = fans_cache[cache_key]
                    item["raw"]["fans_source"] = "profile"
                if item["fans"] is None:
                    warnings.append(f"{item['key']} 粉丝数未知，已跳过")
                    continue
                scored = score_item(item, rule)
                if scored["passed"]:
                    local_id = store.find_same_work(platform, item["id"])
                    candidates.append({**scored, "local": bool(local_id), "item_id": local_id})
    candidates.sort(key=lambda x: x["multiplier"], reverse=True)
    return {"candidates": candidates[:settings["limit"]], "warnings": list(dict.fromkeys(warnings)),
            "scanned": scanned, "calls": calls, "limit": settings["limit"],
            "thresholds": {p: {k: settings[p][k] for k in (*THRESHOLDS, "allow_missing_date")}
                           for p in settings["platforms"]}}


def selected_items(payload: dict) -> tuple[list[dict], list[dict]]:
    """仅接受明确选中的作品；全部校验后才创建任务，不支持主页/短链。"""
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("请勾选 1–100 条作品后入库")
    clean, existing, seen = [], [], set()
    for row in items:
        if not isinstance(row, dict):
            raise ValueError("所选作品必须是对象")
        p, biz = row.get("platform"), str(row.get("id") or "")
        url = row.get("url")
        if not url:
            if p not in PLATFORMS:
                raise ValueError("作品缺少平台或链接")
            url = f"https://www.douyin.com/video/{biz}" if p == "douyin" else f"https://www.xiaohongshu.com/explore/{biz}"
        if not isinstance(url, str):
            raise ValueError("作品链接必须是字符串")
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme not in ("http", "https") or not any(
            host == domain or host.endswith("." + domain)
            for domain in ("douyin.com", "xiaohongshu.com", "iesdouyin.com")
        ):
            raise ValueError("请使用抖音/小红书作品长链接")
        resolved = resolve(url, expand=False)
        if resolved.platform not in PLATFORMS or resolved.kind not in ("video", "images", "text", "work"):
            raise ValueError("仅支持抖音/小红书作品长链接")
        biz = str(resolved.ids.get("aweme_id") or resolved.ids.get("note_id") or "")
        pattern = r"\d+" if resolved.platform == "douyin" else r"[a-fA-F0-9]{24}"
        if not re.fullmatch(pattern, biz):
            raise ValueError("作品 ID 无效，请使用作品长链接")
        key = f"{resolved.platform}:{biz}"
        if key in seen:
            continue
        seen.add(key)
        local_id = store.find_same_work(resolved.platform, biz)
        entry = {"id": key, "url": resolved.url}
        if local_id:
            existing.append({**entry, "item_id": local_id})
        else:
            clean.append(entry)
    if clean and not tikhub_api_key():
        raise tikhub.TikHubError("未配置 TikHub API Key，请前往设置填写；不要发送到聊天")
    return clean, existing
