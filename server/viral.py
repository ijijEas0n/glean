"""低粉爆款候选筛选（非预测）。

两平台互动均为点赞+收藏；抖音收藏缺失按 0 并注明。倍数=互动/max(粉丝,1)。
评分纯函数不访问网络；拉源、补粉、查本地存在性单独处理。
未知粉丝、缺失日期留在参考池并标待核，不从作品 ID 猜时间。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from threading import RLock
import os
import re
from typing import Any

from . import store, tikhub
from .config import load_config, tikhub_api_key
from .resolver import resolve
from urllib.parse import urlsplit, urlencode

PLATFORMS = ("douyin", "xiaohongshu")
DEFAULTS = {
    "tier": 3,
    "enabled": True,
    "max_calls": 24,
    "source_window_hours": 168,
    "publish_time": 7,
    "douyin_sources": ["search", "lowfan"],
    "max_pages": 2,
    "douyin": {
        "max_fans": 50000, "min_interactions": 1200, "min_multiplier": 0.3,
        "max_age_days": 14, "allow_missing_date": False, "keywords": ["AI"],
        "source": "search",
        "path": "/api/v1/douyin/search/fetch_video_search_v2",
        "user_path": "/api/v1/douyin/app/v3/handler_user_profile",
    },
    "xiaohongshu": {
        "max_fans": 10000, "min_interactions": 300, "min_multiplier": 0.1,
        "max_age_days": 14, "allow_missing_date": False, "keywords": ["AI"],
        "path": "/api/v1/xiaohongshu/app_v2/search_notes",
        "user_path": "/api/v1/xiaohongshu/app_v2/get_user_info",
    },
}
THRESHOLDS = ("max_fans", "min_interactions", "min_multiplier", "max_age_days")

TIER_NAMES = ("最严", "较严", "适中", "宽松", "最松")
PRESETS = {
    "douyin": ((30000, 24000, 5, 7), (50000, 12000, 3, 14),
               (50000, 3600, 1, 14), (50000, 1200, 0.3, 14), (100000, 360, 0.1, 30)),
    "xiaohongshu": ((5000, 2000, 2, 7), (10000, 1000, 1, 14),
                    (10000, 500, 0.5, 14), (10000, 300, 0.1, 14), (30000, 100, 0.05, 30)),
}


# 只保存审阅反馈，不含平台响应或凭证。路径固定在仓库内。
FEEDBACK_PATH = Path(__file__).resolve().parents[1] / "data" / "viral-feedback.json"
_FEEDBACK_LOCK = RLock()


def load_feedback() -> dict:
    with _FEEDBACK_LOCK:
        if not FEEDBACK_PATH.exists():
            return {"items": {}, "authors": {}}
        value = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(not isinstance(value.get(k), dict) for k in ("items", "authors")):
            raise ValueError("低粉候选反馈文件格式错误，请检查 data/viral-feedback.json")
        return value


def save_feedback(payload: dict) -> dict:
    platform, biz = payload.get("platform"), payload.get("id")
    kind, mark = payload.get("kind", "item"), payload.get("mark", "")
    if platform not in PLATFORMS or not isinstance(biz, str) or not biz.strip() or len(biz) > 256:
        raise ValueError("反馈的平台或 ID 无效")
    if kind not in ("item", "author") or mark not in (("", "不相关", "值得跟") if kind == "item" else ("", "屏蔽")):
        raise ValueError("反馈类型或标记无效")
    key = f"{platform}:{biz}"
    with _FEEDBACK_LOCK:
        value = load_feedback()
        group = value["items" if kind == "item" else "authors"]
        if mark:
            group[key] = mark
        else:
            group.pop(key, None)
        FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = FEEDBACK_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(FEEDBACK_PATH)
        return value


def local_fields(obj: dict) -> dict:
    """只提取当前作品响应中已有的文本和标识，绝不补详情。"""
    def text(value):
        if isinstance(value, str):
            return value[:12000]
        if isinstance(value, list):
            return " ".join(text(x) for x in value[:100])[:12000]
        if isinstance(value, dict):
            return " ".join(text(value.get(k)) for k in ("name", "title", "cha_name", "hashtag_name", "text"))[:12000]
        return ""
    # 非空对象/数组才算商品证据；false、0、空结构和字符串 0 不命中。
    flags = [k for k in ("is_ads", "is_ad", "is_commerce", "ad_info", "promotion", "promotions", "product_info", "goods_info", "shopping_cart")
             if obj.get(k) not in (None, False, 0, "", "0", "false", [], {})]
    return {"description": " ".join(text(obj.get(k)) for k in ("desc", "description", "content"))[:12000],
            "topics": " ".join(text(obj.get(k)) for k in ("text_extra", "cha_list", "hashtags", "tag_list", "topics"))[:12000],
            "commerce_fields": flags}


def preset(platform: str, tier: int) -> dict:
    return dict(zip(THRESHOLDS, PRESETS[platform][tier]))


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


def interactions(item: dict) -> int | None:
    """同一口径供四门槛与 R 使用；仅抖音允许缺收藏按零。"""
    likes, collects = count(item.get("likes")), count(item.get("collects"))
    if likes is None or (item.get("platform") == "xiaohongshu" and collects is None):
        return None
    return likes + (collects or 0)


def metric_notes(item: dict) -> list[str]:
    return ["收藏缺失"] if item.get("platform") == "douyin" and count(item.get("collects")) is None else []


def score_item(item: dict, rules: dict | None = None, *, now: datetime | None = None) -> dict:
    """返回新对象；两平台赞+藏；比较未经四舍五入的倍数与天数。"""
    platform = item.get("platform")
    if platform not in PLATFORMS:
        raise ValueError("低粉爆款仅支持抖音和小红书")
    rules = {**DEFAULTS[platform], **(rules or {})}
    fans = count(item.get("fans"))
    interaction_count = interactions(item)
    multiplier = interaction_count / max(fans, 1) if fans is not None and interaction_count is not None else None
    stamp = published(item.get("published_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - stamp).total_seconds() / 86400 if stamp else None
    reasons = []
    if fans is None:
        reasons.append("粉丝待核")
    elif fans > rules["max_fans"]:
        reasons.append("粉丝超上限")
    if interaction_count is None:
        reasons.append("互动待核")
    elif interaction_count < rules["min_interactions"]:
        reasons.append("互动不足")
    if multiplier is not None and multiplier < rules["min_multiplier"]:
        reasons.append("倍数不足")
    if age is None:
        reasons.append("日期待核")
    elif age < 0:
        reasons.append("日期异常")
    elif age > rules["max_age_days"]:
        reasons.append("超出本地天数")
    return {**item, "fans": fans, "interactions": interaction_count, "metric_notes": metric_notes(item), "multiplier": multiplier,
            "published_at": stamp.isoformat() if stamp else None, "age_days": age,
            "passed": not reasons, "fail_reasons": reasons,
            "pending": fans is None or interaction_count is None or age is None}


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
    for name, low, high in (("source_window_hours", 24, 168), ("max_pages", 1, 3)):
        value = payload.get(name, result[name])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} 必须是 {low}–{high} 的整数")
        result[name] = value
    days = payload.get("publish_time", result["publish_time"])
    if isinstance(days, bool) or not isinstance(days, int) or days not in (0, 7, 180):
        raise ValueError("publish_time 必须是 7 / 180 / 0（不限）")
    result["publish_time"] = days
    sources = payload.get("douyin_sources", result["douyin_sources"])
    # 兼容旧配置显式选择单一来源；新请求可用来源勾选覆盖。
    if ("douyin_sources" not in payload and "douyin_sources" not in config
            and isinstance(config.get("douyin"), dict) and "source" in config["douyin"]):
        sources = [config["douyin"]["source"]]
    if not isinstance(sources, list) or not sources or any(x not in ("search", "lowfan") for x in sources):
        raise ValueError("douyin_sources 请选择 search / lowfan")
    result["douyin_sources"] = [x for x in ("search", "lowfan") if x in sources]
    tier = payload.get("tier", result["tier"])
    if isinstance(tier, bool) or not isinstance(tier, int) or not 0 <= tier <= 4:
        raise ValueError("tier 必须是 0–4 的整数")
    result["tier"] = tier
    overrides = payload.get("thresholds", {})
    if not isinstance(overrides, dict) or any(k not in PLATFORMS for k in overrides):
        raise ValueError("thresholds 必须按平台提供")
    for p in PLATFORMS:
        saved, patch = config.get(p, {}), overrides.get(p, {})
        if not isinstance(saved, dict) or not isinstance(patch, dict):
            raise ValueError("平台配置与门槛必须是对象")
        if any(k not in (*THRESHOLDS, "allow_missing_date") for k in patch):
            raise ValueError("未知门槛字段")
        # 显式选档覆盖历史数字；未选档时仍兼容已保存的自定义门槛。
        rule = {**DEFAULTS[p], **preset(p, tier), **saved}
        if "tier" in payload:
            rule.update(preset(p, tier))
        rule.update(patch)
        for k in THRESHOLDS:
            v = rule[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{p}.{k} 必须是非负有限数字")
        if not isinstance(rule["allow_missing_date"], bool):
            raise ValueError("allow_missing_date 必须是布尔值")
        keywords = payload.get("keywords", rule["keywords"])
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20 or any(
            not isinstance(k, str) or not k.strip() or len(k.strip()) > 80 for k in keywords
        ):
            raise ValueError("keywords 需为 1–20 个非空关键词，每个最多 80 字")
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


def is_balance_error(value) -> bool:
    return getattr(value, "status", None) == 402 or any(
        text in str(value).lower() for text in ("402", "insufficient balance", "余额不足"))


def check_balance(payload):
    data = payload
    for _ in range(5):
        if not isinstance(data, dict):
            break
        messages = " ".join(str(data.get(k, "")) for k in ("message", "message_zh", "error"))
        if str(data.get("code")) == "402" or any(x in messages.lower() for x in ("insufficient balance", "余额不足")):
            raise tikhub.TikHubError("TikHub 余额不足，请充值", status=402)
        data = data.get("detail") if isinstance(data.get("detail"), dict) else data.get("data")


def pagination(payload: dict, platform: str) -> tuple[bool | None, dict]:
    """只读取已知响应包装，含实测 business_config.next_page。"""
    data = tikhub.data_of(payload)
    layers = []
    for _ in range(5):
        if not isinstance(data, dict):
            break
        layers.append(data)
        if isinstance(data.get("business_config"), dict):
            layers.append(data["business_config"])
        data = data.get("data")
    more, params = None, {}
    keys = ("cursor", "search_id", "backtrace") if platform == "douyin" else ("search_id", "search_session_id")
    for layer in layers:
        if "has_more" in layer:
            more = str(layer["has_more"]).lower() not in ("0", "false", "none")
        for container in (layer, layer.get("next_page", {}), layer.get("log_pb", {})):
            if isinstance(container, dict):
                params.update({k: container[k] for k in keys if container.get(k) is not None})
    return more, params


def rows_from(payload: dict) -> list[dict]:
    """只遍历已知列表包装，不递归搜任意 id，以免把作者/广告算成作品。

    搜索包装按 2026-10-29 真实响应 fixture 回归。
    未识别的结构显式报错，不能伪装成“没有候选”。
    """
    check_balance(payload)
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


def cover_of(platform: str, obj: dict) -> str:
    """从抖音/小红书搜索条目抠封面 URL；取不到留空，不编造。

    抖音低粉榜是扁平字段 `item_cover_url`，不是详情里的 video.cover。
    """
    from server.adapters.base import first_url
    from server.adapters.douyin import _cover_url
    from server.adapters.xiaohongshu import _image_urls, _video_cover

    url = ""
    for key in ("item_cover_url", "cover_url"):
        value = obj.get(key)
        if isinstance(value, str) and value.startswith("http"):
            url = value
            break
    if not url and platform == "douyin":
        video = obj.get("video") if isinstance(obj.get("video"), dict) else {}
        if video:
            url = _cover_url(video)
    if not url and platform == "xiaohongshu":
        url = _video_cover(obj) or next(iter(_image_urls(obj)), "")
    if not url:
        for key in ("cover", "thumbnail", "thumb_url", "image", "origin_cover"):
            value = obj.get(key)
            if isinstance(value, str) and value.startswith("http"):
                url = value
                break
            if isinstance(value, dict):
                url = first_url(value)
                if url:
                    break
    return url[:2000] if url else ""


def normalize(platform: str, obj: dict) -> dict | None:
    if platform == "douyin" and isinstance(obj.get("data"), dict):
        obj = obj["data"]
        if not isinstance(obj.get("aweme_info"), dict):
            return None
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
    return {**local_fields(obj), "platform": platform, "id": biz, "key": f"{platform}:{biz}",
            "url": url,
            "cover": cover_of(platform, obj),
            "title": str(next((obj[k] for k in ("item_title", "title", "display_title", "desc") if obj.get(k)), ""))[:500],
            "author": str(pick(author, "nickname", "nick_name", "name") or obj.get("nick_name") or ""),
            "author_id": str(pick(author, "sec_uid", "userid", "user_id", "uid", "id") or obj.get("sec_uid") or ""),
            "fans": count(pick(obj, "fans_cnt", "fans")) if platform == "douyin" and any(k in obj for k in ("fans_cnt", "fans"))
                    else count(pick(author, "follower_count", "fans", "fans_count", "followers")),
            "likes": count(pick(stats, "digg_count", "like_cnt", "liked_count", "likes")),
            "collects": count(pick(stats, "collect_count", "collect_cnt", "collected_count", "collected", "collects")),
            "published_at": pick(obj, "publish_time", "create_time", "time", "timestamp", "published_at"),
            "raw": {"id": biz, "type": str(obj.get("type") or ""),
                    "metrics_source": "search_or_lowfan", "fans_source": "search",
                    "likes": count(pick(stats, "digg_count", "like_cnt", "liked_count", "likes")),
                    "collects": count(pick(stats, "collect_count", "collect_cnt", "collected_count", "collected", "collects"))}}


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
    """按页轮询各平台关键词，最多扫描 300 条；补粉缓存仅本次任务有效。"""
    calls, scanned = 0, 0
    warnings, pool, fans_cache, fans_notes = [], [], {}, {}
    now = datetime.now(timezone.utc)
    incomplete = False
    authors_checked = 0

    def call(path, params, *, post=False):
        nonlocal calls, incomplete
        if calls >= settings["max_calls"]:
            incomplete = True
            raise ValueError("已达本次 TikHub 调用上限，结果未完整核对")
        calls += 1
        try:
            response = tikhub.post(path, params) if post else tikhub.get(path, params)
        except tikhub.TikHubError as exc:
            if is_balance_error(exc):
                raise tikhub.TikHubError("TikHub 余额不足，请充值", status=402) from exc
            raise
        check_balance(response)
        return response

    source_calls = {p: 0 for p in settings["platforms"]}
    pages_by_keyword = {p: {k: 0 for k in settings[p]["keywords"]} for p in settings["platforms"]}
    pages_by_source = {}
    streams = [(p, source, k) for p in settings["platforms"]
               for source in (settings["douyin_sources"] if p == "douyin" else ["search"])
               for k in settings[p]["keywords"]]
    continuation, exhausted, originals = {}, set(), {}
    # 所有来源/关键词先首屏，再深翻；低粉榜只补一页，最后补粉。
    for page in range(1, settings["max_pages"] + 1):
        for platform, source, keyword in streams:
            stream = (platform, source, keyword)
            if stream in exhausted or (source == "lowfan" and page > 1):
                continue
            if calls >= settings["max_calls"] or scanned >= 300:
                incomplete = True
                warnings.append("已达调用或扫描上限，结果未完整核对")
                continue
            rule = settings[platform]
            if progress:
                progress("正在查询本批参考", min(90, 10 + calls), f"{platform} · {source} · {keyword} · 第{page}页")
            if platform == "douyin" and source == "lowfan":
                path = "/api/v1/douyin/billboard/fetch_hot_total_low_fan_list"
                query = {"page": 1, "page_size": 50, "date_window": 168, "keyword": keyword}
            elif platform == "douyin":
                path = (DEFAULTS["douyin"]["path"] if rule["path"].endswith("fetch_hot_total_low_fan_list") else rule["path"])
                query = {"keyword": keyword, "cursor": 0, "sort_type": "1",
                         "publish_time": str(settings["publish_time"]), "search_id": "", "backtrace": ""}
            else:
                path = rule["path"]
                query = {"keyword": keyword, "page": page, "sort_type": "popularity_descending",
                         "time_filter": {7: "一周内", 180: "半年内", 0: "不限"}[settings["publish_time"]]}
            query.update(continuation.get(stream, {}))
            try:
                response = call(path, query, post=platform == "douyin")
                rows = rows_from(response)
            except tikhub.TikHubError as exc:
                raise tikhub.TikHubError(f"{platform} 拉源失败：{exc}") from exc
            fetched_at = datetime.now(timezone.utc).isoformat()
            source_calls[platform] += 1
            pages_by_keyword[platform][keyword] = max(page, pages_by_keyword[platform][keyword])
            pages_by_source.setdefault(platform + ":" + source, {})[keyword] = page
            more, next_params = pagination(response, platform)
            if not rows or more is False:
                exhausted.add(stream)
            elif source == "search":
                if platform == "douyin" and ("cursor" not in next_params or next_params["cursor"] == query["cursor"]):
                    exhausted.add(stream)
                    if page < settings["max_pages"]:
                        incomplete = True
                        warnings.append("抖音搜索缺少有效翻页游标，未继续翻页")
                else:
                    continuation[stream] = {**continuation.get(stream, {}), **next_params}
            if len(rows) > 300 - scanned:
                incomplete = True
                warnings.append("已达扫描上限，结果未完整核对")
            for obj in rows[:300 - scanned]:
                scanned += 1
                item = normalize(platform, obj)
                if not item:
                    continue
                source_fans = item["fans"]
                if source == "search" and item["fans"] == 0:
                    item["fans"] = None
                item.update(source=platform + ":" + source, metrics_source=path, fetched_at=fetched_at)
                item["raw"].update(fans_source=source, metrics_source=path, fetched_at=fetched_at, keyword=keyword,
                                   fans=source_fans, published_at=item["published_at"])
                if item["key"] in originals:
                    original = originals[item["key"]]
                    original["raw"].setdefault("alternatives", []).append(deepcopy(item["raw"]))
                    for field in ("description", "topics"):
                        if item[field] and item[field] not in original[field]:
                            original[field] = (original[field] + " " + item[field])[:24000]
                    original["commerce_fields"] = list(dict.fromkeys(original["commerce_fields"] + item["commerce_fields"]))
                else:
                    originals[item["key"]] = item
                    pool.append(item)

    def score_in_range(item, rule):
        if settings["publish_time"]:
            rule = {**rule, "max_age_days": min(rule["max_age_days"], settings["publish_time"])}
        return score_item(item, rule, now=now)

    # 互动高的优先；仅当前档互动/日期有希望通过的作者才主动补粉。
    pool.sort(key=lambda x: score_in_range(x, settings[x["platform"]])["interactions"] or 0, reverse=True)
    for item in pool:
        platform = item["platform"]
        rule = settings[platform]
        probe = score_in_range(item, rule)
        eligible = (probe["interactions"] is not None and probe["interactions"] >= rule["min_interactions"]
                    and probe["age_days"] is not None and 0 <= probe["age_days"] <= min(
                        rule["max_age_days"], settings["publish_time"] or rule["max_age_days"]))
        author_id = item["author_id"]
        cache_key = (platform, author_id)
        item["fans_note"] = ""
        if platform == "xiaohongshu" or item["fans"] is None:
            if author_id and cache_key in fans_cache:
                item["fans"] = fans_cache[cache_key]
                item["raw"]["fans_source"] = "profile"
            elif eligible and author_id:
                try:
                    param = "user_id" if platform == "xiaohongshu" else "sec_user_id"
                    if calls >= settings["max_calls"]:
                        fans_notes[cache_key] = "调用上限，未补查"
                    else:
                        authors_checked += 1
                    fans_cache[cache_key] = profile_fans(call(rule["user_path"], {param: author_id}))
                except (tikhub.TikHubError, ValueError) as exc:
                    if is_balance_error(exc):
                        raise tikhub.TikHubError("TikHub 余额不足，请充值", status=402) from exc
                    warnings.append(f"{platform} 补粉失败，粉丝待核：{exc}")
                    fans_cache[cache_key] = None
                item["fans"] = fans_cache[cache_key]
                item["raw"]["fans_source"] = "profile"
            elif platform == "xiaohongshu":
                # 保持旧版红书必须以作者接口核粉的口径。
                item["fans"] = None
            if item["fans"] is None:
                item["fans_note"] = fans_notes.get(cache_key) or (
                    "补查无结果或失败" if cache_key in fans_cache else
                    "缺作者 ID" if not author_id else "本次未补查")
        local_id = store.find_same_work(platform, item["id"])
        item.update(local=bool(local_id), item_id=local_id)

    if calls >= settings["max_calls"] or scanned >= 300:
        incomplete = True
        warnings.append("已达调用或扫描上限，结果未完整核对")
    scored_pool = []
    for item in pool:
        scored = score_in_range(item, settings[item["platform"]])
        strict = score_in_range(item, preset(item["platform"], 1))["passed"]
        scored.update(strict_pass=strict, pass_level=("pending" if scored["pending"] else
                      "strict" if strict else "tier" if scored["passed"] else "pool"))
        scored_pool.append(scored)
    candidates = sorted((x for x in scored_pool if x["passed"]), key=lambda x: x["multiplier"], reverse=True)
    thresholds = {p: {k: settings[p][k] for k in (*THRESHOLDS, "allow_missing_date")}
                  for p in settings["platforms"]}
    custom = any(any(settings[p][k] != preset(p, settings["tier"])[k] for k in THRESHOLDS)
                 for p in settings["platforms"])
    return {"candidates": candidates[:settings["limit"]], "pool": scored_pool,
            "tier": settings["tier"], "preset": {"id": settings["tier"], "name": "自定义" if custom else TIER_NAMES[settings["tier"]]},
            "thresholds": thresholds, "thresholds_applied": thresholds,
            "presets": {p: [preset(p, t) for t in range(5)] for p in PLATFORMS},
            "evaluated_at": now.isoformat(),
            "stats": {"scanned": scanned, "recognized": len(scored_pool), "pool": len(scored_pool),
                      "strict_pass": sum(x["strict_pass"] for x in scored_pool), "tier_pass": len(candidates),
                      "pending": sum(x["pending"] for x in scored_pool),
                      "pending_fans": sum(x["fans"] is None for x in scored_pool),
                      "pending_date": sum(x["age_days"] is None for x in scored_pool),
                      "pending_interactions": sum(x["interactions"] is None for x in scored_pool)},
            "coverage": {"incomplete": incomplete, "authors_checked": authors_checked,
                         "keywords": {p: settings[p]["keywords"] for p in settings["platforms"]},
                         "max_pages": settings["max_pages"], "pages_by_keyword": pages_by_keyword,
                         "pages_by_source": pages_by_source, "publish_time": settings["publish_time"],
                         "douyin_sources": settings["douyin_sources"],
                         "source_calls": source_calls, "scan_limit": 300,
                         "source_window_hours": {p: 168 if p == "douyin" and "lowfan" in settings["douyin_sources"] else None
                                                 for p in settings["platforms"]}},
            "warnings": list(dict.fromkeys(warnings)), "scanned": scanned, "calls": calls, "limit": settings["limit"]}


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
