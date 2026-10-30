"""用户按需查询账号历史 R；独立预算，无自动补详情、重试或 TikHub 代理探测。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from statistics import median
from threading import RLock
import json
import math
import time
import uuid

from . import tikhub, viral
from .adapters.douyin import POSTS_PATH
from .adapters.xiaohongshu import USER_POSTS_PATH

CACHE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'viral-r.json'
TTL = 24 * 3600
WINDOW = 20
MIN_SAMPLES = 5
BUDGET = 20
R_HIGH, R_MEDIUM = 10, 3
_LOCK = RLock()
_PLANS = {}


def interactions(row):
    likes = viral.count(row.get('likes'))
    collects = viral.count(row.get('collects'))
    if likes is None or (row['platform'] == 'xiaohongshu' and collects is None):
        return None
    return likes + (collects if row['platform'] == 'xiaohongshu' else 0)


def score(row, history):
    # 按时间降序整理过的历史，排除本条、去重；缺指标不冒充 0。
    seen, values = set(), []
    for other in history:
        biz = other['id']
        if biz == row['id'] or biz in seen:
            continue
        seen.add(biz)
        value = other.get('interactions')
        if value is not None:
            values.append(value)
        if len(values) == WINDOW:
            break
    base = median(values) if values else None
    result = {'value': None, 'samples': len(values), 'median': base,
              'distribution': f'互动范围 {min(values):g}–{max(values):g}，零互动 {values.count(0)} 条' if values else '无可用近作',
              'grade': '样本不足', 'note': '至少需要 5 条其它作品的完整互动指标'}
    current = interactions(row)
    if len(values) < MIN_SAMPLES:
        return result
    if base == 0:
        return {**result, 'grade': '基线为零', 'note': '历史中位数为 0，无法稳健计算倍数，暂不评级（未用 1 替代）'}
    if current is None:
        return {**result, 'grade': '互动待核', 'note': '本条互动指标缺失，无法计算 R'}
    ratio = current / base
    return {**result, 'value': round(ratio, 2), 'grade': '明显爆' if ratio >= R_HIGH else '偏爆' if ratio >= R_MEDIUM else '平常',
            'note': '抖音按赞，小红书按赞+藏；本条使用预览时互动快照'}


def _cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or any(not isinstance(v, dict) or
                type(v.get('fetched_at')) not in (int, float) or not math.isfinite(v['fetched_at']) or
                not isinstance(v.get('note'), str) or not isinstance(v.get('works'), list) or any(
                    not isinstance(w, dict) or not isinstance(w.get('id'), str) or
                    (w.get('interactions') is not None and (type(w['interactions']) not in (int, float) or
                     not math.isfinite(w['interactions']) or w['interactions'] < 0))
                    for w in v['works']) for v in data.values()):
            raise ValueError()
        return data
    except (ValueError, TypeError) as exc:
        raise ValueError('R 缓存格式错误，请检查 data/viral-r.json；未覆盖原文件') from exc


def _fresh(entry):
    return bool(entry and 0 <= time.time() - entry['fetched_at'] < TTL)


def _save(data):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(CACHE_PATH)


def estimate(payload):
    items = payload.get('items')
    pages, budget = payload.get('pages', 2), payload.get('budget', BUDGET)
    if not isinstance(items, list) or len(items) > 300 or any(not isinstance(x, dict) for x in items):
        raise ValueError('请提供当前预览的作品列表（最多 300 条）')
    if type(pages) is not int or not 1 <= pages <= 3 or type(budget) is not int or not 1 <= budget <= BUDGET:
        raise ValueError('R 每作者最多 1–3 页，独立预算须为 1–20 次')
    if not isinstance(payload.get('worth_only', False), bool):
        raise ValueError('worth_only 必须为布尔值')
    for row in items:
        if row.get('platform') not in viral.PLATFORMS or not isinstance(row.get('id'), str) or not row['id'] or len(row['id']) > 256:
            raise ValueError('R 作品的平台或 ID 无效')
        if row.get('author_id') is not None and (not isinstance(row['author_id'], str) or len(row['author_id']) > 256):
            raise ValueError('R 作者 ID 无效')
    by_key = {f"{x['platform']}:{x['id']}": x for x in items}
    keys = [k for k, mark in viral.load_feedback()['items'].items() if mark == '值得跟'] if payload.get('worth_only') else list(by_key)
    rows, missing = [], []
    for key in keys:
        row = by_key.get(key)
        if not row or not str(row.get('author_id') or '').strip():
            missing.append(f'{key}：当前预览无作者 ID，不能计算；请恢复对应预览或重新查询')
        elif row['platform'] == 'douyin' and row['author_id'].isdigit():
            missing.append(f'{key}：只有数字 UID，主页作品接口需要 sec_uid；不额外付费换取 ID')
        else:
            rows.append({k: row.get(k) for k in ('platform', 'id', 'author_id', 'likes', 'collects')})
    with _LOCK:
        cache = _cache()
        authors = {f"{r['platform']}:{r['author_id']}" for r in rows}
        cached = sum(_fresh(cache.get(a)) for a in authors)
        calls = (len(authors) - cached) * pages
        now = time.time()
        for key in list(_PLANS):
            if _PLANS[key]['expires'] <= now:
                del _PLANS[key]
        if len(_PLANS) >= 100:
            del _PLANS[next(iter(_PLANS))]
        token = uuid.uuid4().hex
        _PLANS[token] = {'rows': deepcopy(rows), 'pages': pages, 'budget': budget, 'expires': now + 600, 'estimated': calls}
        return {'plan_id': token, 'authors': len(authors), 'cached_authors': cached, 'uncached_authors': len(authors)-cached,
                'pages': pages, 'estimated_calls': calls, 'budget': budget, 'over_budget': calls > budget, 'missing': missing}


def _page(platform, payload):
    viral.check_balance(payload)
    if str(payload.get('code', 200)) == '429':
        raise tikhub.TikHubError('TikHub 限流了，请稍后手动重试', status=429)
    data = tikhub.data_of(payload)
    if platform == 'xiaohongshu' and isinstance(data, dict) and isinstance(data.get('data'), dict):
        data = data['data']
    field = 'aweme_list' if platform == 'douyin' else 'notes'
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        raise ValueError('作者近作数据结构未识别，未计算 R；请核对主页作品端点')
    rows = data[field]
    works = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError('作者近作条目结构未识别，未计算 R')
        # 小红书主页字段与搜索字段不完全一致。
        raw = dict(raw)
        if platform == 'xiaohongshu':
            raw.setdefault('liked_count', raw.get('like_count'))
        row = viral.normalize(platform, raw)
        if row is None:
            raise ValueError('作者近作作品 ID 结构未识别，未计算 R')
        works.append({'id': row['id'], 'interactions': interactions(row),
                      'published_at': row.get('published_at')})
    more = str(data.get('has_more', False)).lower() not in ('0', 'false', 'none', '')
    cursor = data.get('max_cursor') if platform == 'douyin' else data.get('cursor') or (rows[-1].get('cursor') or rows[-1].get('id') if rows else '')
    return works, more, cursor


def execute(payload):
    # 全程串行锁：多个标签页并发查询同作者也只付一次；计划只能执行一次。
    with _LOCK:
        plan = _PLANS.pop(str(payload.get('plan_id', '')), None)
        if not plan or plan['expires'] <= time.time():
            raise ValueError('R 预计调用已过期或已执行，请重新点击算 R')
        cache = _cache()
        authors = {f"{r['platform']}:{r['author_id']}": r for r in plan['rows']}
        needed = sum(not _fresh(cache.get(a)) for a in authors) * plan['pages']
        if needed > plan['budget'] or needed > plan['estimated']:
            raise ValueError('R 查询超过独立预算或缓存已过期，请减少作者/页数后重新估算')
        calls, results, errors = 0, {}, {}
        for author, row in authors.items():
            entry = cache.get(author)
            hit = _fresh(entry)
            if not hit:
                platform = row['platform']
                works, cursor, seen = [], '', set()
                try:
                    for _ in range(plan['pages']):
                        if calls >= plan['budget']:
                            raise ValueError('已达 R 独立预算上限，未完成的作者不计算')
                        params = {'sec_user_id': row['author_id'], 'count': 20} if platform == 'douyin' else {'user_id': row['author_id']}
                        if cursor:
                            params['max_cursor' if platform == 'douyin' else 'cursor'] = cursor
                        calls += 1
                        response = tikhub.get(POSTS_PATH if platform == 'douyin' else USER_POSTS_PATH, params,
                                              max_attempts=1, probe_proxy=False)
                        batch, more, next_cursor = _page(platform, response)
                        for work in batch:
                            if work['id'] not in seen:
                                seen.add(work['id']); works.append(work)
                        if len([w for w in works if w['interactions'] is not None]) >= WINDOW + 1 or not more:
                            break
                        if not next_cursor or next_cursor == cursor:
                            raise ValueError('作者近作分页结构未识别，未计算 R')
                        cursor = next_cursor
                    # 置顶作品按发布时间重排；缺时间放末尾，同时间保留接口顺序并提示。
                    works.sort(key=lambda w: (viral.published(w.get('published_at')).timestamp() if viral.published(w.get('published_at')) else 0), reverse=True)
                    entry = {'fetched_at': time.time(), 'works': works,
                             'pages_limit': plan['pages'],
                             'note': '只按本次返回近作计算，最多 20 条其它作品；非全量账号统计。' +
                             (' 部分作品缺日期，近作顺序待核。' if any(not viral.published(w.get('published_at')) for w in works) else '')}
                    cache[author] = entry
                    _save(cache)
                except (tikhub.TikHubError, ValueError, OSError) as exc:
                    error = 'TikHub 余额不足，请充值（402）' if viral.is_balance_error(exc) else 'TikHub 限流了，请稍后手动重试（429）' if getattr(exc, 'status', 0) == 429 else str(exc)
                    errors[author] = error
                    # 失败立即停止本批，避免余额/限流/未知结构继续花额度。
                    break
            for target in plan['rows']:
                if f"{target['platform']}:{target['author_id']}" == author:
                    results[f"{target['platform']}:{target['id']}"] = {**score(target, entry['works']),
                        'fetched_at': entry['fetched_at'], 'cache_hit': hit, 'history_note': entry['note']}
        return {'results': results, 'errors': errors, 'calls': calls,
                'uncomputed': [f"{r['platform']}:{r['id']}" for r in plan['rows'] if f"{r['platform']}:{r['id']}" not in results]}
