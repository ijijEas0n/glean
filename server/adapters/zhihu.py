#!/usr/bin/env python3
"""知乎适配器：回答 / 专栏文章 / 想法（pin）三种内容。

一个函数三种形态，因为 resolver 把三者都归到同一个入口（kind 分别是 article / text）。

坑（实测）：

- **正文都是 HTML**：回答和文章的 `content`、想法的 `content_html` 都带标签，
  必须先 strip_html 再进 item.text。
- **同一平台两种字段名和两种数据水平**：
  * 回答**没有标题**，标题只能取 `question.title`；回答**也没有点赞/评论总数**，
    顶层完全没有 `voteup_count`/`comment_count`（只有逐段的 seg_info 计数）。
    本适配器补一次 `fetch_question_answers`（用回答自带的 question.id），
    从列表里的同一条 answer 上读真实总数；补不到就留 0 + warning，绝不编数。
  * 文章有自己的 `title`，也**有** `voteup_count`/`comment_count`/`favlists_count`，
    不需要补调用。
  * 时间字段名还不一样：回答是 `created_time`，文章是 `created`，想法是 `created`。
- **想法的正文块是混合的**：`content[]` 里可能是 text / image / video / link_card，
  单独的 text 块里也可能再嵌 HTML。所以统一走 `content_html` 做正文，
  再按块类型收集图片和视频，不要只读 text 块。
- 想法图片有 `original_url`（原图）和 `url`（720w 压缩图），优先原图。
- 想法的视频是 `playlist[]`（多档码率 mp4），地址带 auth_key 会过期，只能当次用。
- 作者粉丝数不在内容里，要单独问 `fetch_user_info?user_url_token=`（单层 `data.follower_count`）。
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    iso_from_unix,
    make_item_id,
    strip_html,
)

ANSWER_PATH = "/api/v1/zhihu/web/fetch_answer_detail"
ARTICLE_PATH = "/api/v1/zhihu/web/fetch_column_article_detail"
PIN_PATH = "/api/v1/zhihu/web/fetch_pin_detail"
USER_INFO_PATH = "/api/v1/zhihu/web/fetch_user_info"
QUESTION_ANSWERS_PATH = "/api/v1/zhihu/web/fetch_question_answers"

ANSWER_SCAN_LIMIT = 20  # 补查回答互动数据时扫多少条；再多就不值当了

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


# ---- 公共取值 ----


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """多字段名兼容：同一个平台不同端点字段名不一致。"""
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _plain(markup: Any) -> str:
    """HTML → 纯文本。

    base.strip_html 只认有限的几个实体，实测知乎正文里还有 `&#34;` 这种**数字实体**
    （不处理的话正文里会原样留着 &#34;），所以剥完标签再统一反转义一次。
    """
    return html.unescape(strip_html(markup))


def _html_images(markup: str) -> list[str]:
    """从正文 HTML 抠原图。`data-original` 是原图，`src` 常常是占位或压缩图。"""
    rows: list[str] = []
    for tag in _IMG_TAG_RE.findall(markup or ""):
        attrs = {name.lower(): (double or single) for name, double, single in _ATTR_RE.findall(tag)}
        for key in ("data-original", "data-actualsrc", "src"):
            url = str(attrs.get(key) or "").strip()
            if not url.startswith("http"):
                continue  # 懒加载占位是 data:image/svg+xml，直接跳过
            if url not in rows:
                rows.append(url)
            break
    return rows


def _author(row: dict[str, Any]) -> dict[str, Any]:
    """只回 SPEC §3 规定的 6 个键；headline 这类平台特有字段放 item.profile。"""
    row = row or {}
    token = str(row.get("url_token") or "")
    return {
        "name": str(row.get("name") or ""),
        "handle": token,
        "id": str(row.get("id") or ""),  # 注意这是 hash，不是 url_token
        "avatar": str(_pick(row, "avatar_url", "avatar_url_template", default="") or ""),
        "url": f"https://www.zhihu.com/people/{token}" if token else "",
        "followers": 0,
    }


def _headline(data: dict[str, Any]) -> str:
    """签名（headline）是作者级字段，放进 profile，不塞进 author 破坏冻结结构。"""
    row = data.get("author") if isinstance(data.get("author"), dict) else {}
    return str(row.get("headline") or "")


def _with_followers(author: dict[str, Any], ctx: FetchContext) -> dict[str, Any]:
    """粉丝数不在内容里，单独查一次；查不到就留 0，别影响主流程。"""
    token = str(author.get("handle") or "")
    if not token:
        ctx.warn("作者没有 url_token，粉丝数无法查询")
        return author
    try:
        payload = get(USER_INFO_PATH, {"user_url_token": token})
        ctx.note_api(USER_INFO_PATH)
        ctx.save_raw("zhihu_fetch_user_info", payload)
        # 单层：粉丝数直接在 data.follower_count
        author["followers"] = as_int((data_of(payload) or {}).get("follower_count"))
    except TikHubError as exc:
        ctx.warn(f"作者粉丝数没取到（{exc}）")
    return author


def _start(kind: str, key: str, resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """类型定下来之后再落 item_id，并挂到上下文做渐进展示。"""
    item_id = ctx.begin(make_item_id("zhihu", kind, key))
    item = empty_item("zhihu", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)
    return item


def _transcribe_entry(entry: dict[str, Any] | None, ctx: FetchContext) -> dict[str, Any]:
    from .. import store as store_module
    from ..media import transcribe_video

    if not entry or not entry.get("path"):
        return {"source": "none", "text": "", "segments": [], "note": "没有可转写的视频文件"}
    if not ctx.want_transcribe:
        return {"source": "none", "text": "", "segments": [], "note": "已关闭转写"}

    ctx.report("正在转写语音", 62)
    path = store_module.media_abs_path(ctx.item_id, entry["path"])
    if not path:
        return {"source": "none", "text": "", "segments": [], "note": "视频文件缺失"}
    engine = str(ctx.options.get("transcribe_engine") or "volcengine")
    result = transcribe_video(path, ctx.work_dir, engine=engine)
    if result.get("note") and result.get("source") == "none":
        ctx.warn(str(result["note"]))
    return result


def _media_entries(cover: str, images: list[str], ctx: FetchContext) -> list[dict[str, Any]]:
    """封面 + 正文图统一落盘；不下载时也把远程地址留下。"""
    entries: list[dict[str, Any]] = []
    if cover:
        if ctx.want_media:
            entries.append(ctx.save_media(cover, "cover", role="cover", media_type="image",
                                          referer="https://www.zhihu.com/"))
        else:
            entries.append({"role": "cover", "type": "image", "path": "", "url": cover,
                            "width": 0, "height": 0, "bytes": 0})
    for index, url in enumerate(images, start=1):
        if ctx.want_media:
            entries.append(ctx.save_media(url, f"{index:03d}", role="content", media_type="image",
                                          referer="https://www.zhihu.com/"))
        else:
            entries.append({"role": "content", "type": "image", "path": "", "url": url,
                            "width": 0, "height": 0, "bytes": 0})
    return entries


# ---- 回答 ----


def _answer_stats(answer_id: str, question_id: str, ctx: FetchContext) -> dict[str, int]:
    """回答详情接口不返回点赞/评论总数，只能从问题下的回答列表里捞同一条。

    实测 `fetch_question_answers` 的 `target` 上同时有 voteup_count / comment_count /
    favlists_count，且与页面上的「876 赞同 · 58 收藏 · 97 评论」完全对得上。
    """
    empty = {"like": 0, "comment": 0, "share": 0, "collect": 0, "play": 0}
    if not question_id:
        ctx.warn("回答里没有 question.id，点赞/评论数无法补查，互动数据留空")
        return empty
    ctx.report("正在拉取互动数据", 18)
    try:
        payload = get(QUESTION_ANSWERS_PATH, {"question_id": question_id, "limit": ANSWER_SCAN_LIMIT})
        ctx.note_api(QUESTION_ANSWERS_PATH)
        ctx.save_raw("zhihu_fetch_question_answers", payload)
    except TikHubError as exc:
        ctx.warn(f"回答的点赞/评论数没取到（{exc}），互动数据留空")
        return empty

    rows = (data_of(payload) or {}).get("data") or []
    for row in rows if isinstance(rows, list) else []:
        target = (row or {}).get("target") if isinstance(row, dict) else None
        if not isinstance(target, dict) or str(target.get("id") or "") != answer_id:
            continue
        return {
            "like": as_int(target.get("voteup_count")),      # 赞同
            "comment": as_int(target.get("comment_count")),  # 评论
            "share": 0,                                      # 接口不给转发数
            "collect": as_int(target.get("favlists_count")), # 收藏
            "play": 0,                                       # 接口不给阅读数
        }
    ctx.warn(f"回答了扫前 {ANSWER_SCAN_LIMIT} 条同问题回答也没找到这条，点赞/评论数留空")
    return empty


def _fetch_answer(answer_id: str, resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    ctx.begin(make_item_id("zhihu", "article", answer_id))  # 占位：确定 kind 后重设
    ctx.report("正在调用知乎接口", 12, f"回答 {answer_id}")
    payload = get(ANSWER_PATH, {"answer_id": answer_id})
    ctx.note_api(ANSWER_PATH)
    data = data_of(payload)
    if not isinstance(data, dict) or not data.get("content"):
        raise TikHubError("知乎没有返回回答内容（回答可能已删除或需要登录）")

    item = _start("article", answer_id, resolved, ctx)
    ctx.save_raw("zhihu_fetch_answer_detail", payload)

    question = data.get("question") if isinstance(data.get("question"), dict) else {}
    html_body = str(data.get("content") or "")
    excerpt = _plain(data.get("excerpt"))
    title = str(question.get("title") or "").strip() or excerpt[:80]
    if not title:
        title = f"知乎回答 {answer_id}"
        ctx.warn("回答既没有所属问题标题也没有摘要，标题用 ID 兜底")

    item.update({
        "title": title,
        "text": _plain(html_body),
        "author": _with_followers(_author(data.get("author") or {}), ctx),
        "created_at": iso_from_unix(data.get("created_time")),
        "duration": None,
        "canonical_url": f"https://www.zhihu.com/answer/{answer_id}",
        "profile": {
            "question_id": str(question.get("id") or ""),
            "question_url": f"https://www.zhihu.com/question/{question.get('id')}" if question.get("id") else "",
            "answer_type": str(data.get("answer_type") or ""),
            "updated_at": iso_from_unix(data.get("updated_time")),
            "headline": _headline(data),
        },
        "stats": _answer_stats(answer_id, str(question.get("id") or ""), ctx),
    })

    images = _html_images(html_body)
    ctx.report("正在下载媒体", 35)
    item["media"] = _media_entries(images[0] if images else "", images, ctx)
    if not images:
        ctx.warn("回答正文里没有解析到图片（可能是纯文字回答）")
    item["transcript"] = {"source": "none", "text": "", "segments": []}
    return item


# ---- 专栏文章 ----


def _fetch_article(article_id: str, resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    ctx.begin(make_item_id("zhihu", "article", article_id))
    ctx.report("正在调用知乎接口", 12, f"文章 {article_id}")
    payload = get(ARTICLE_PATH, {"article_id": article_id})
    ctx.note_api(ARTICLE_PATH)
    data = data_of(payload)
    if not isinstance(data, dict) or not data.get("content"):
        raise TikHubError("知乎没有返回文章内容（文章可能已删除或需要登录）")

    item = _start("article", article_id, resolved, ctx)
    ctx.save_raw("zhihu_fetch_column_article_detail", payload)

    html_body = str(data.get("content") or "")
    reaction = data.get("reaction") if isinstance(data.get("reaction"), dict) else {}
    statistics = reaction.get("statistics") if isinstance(reaction.get("statistics"), dict) else {}

    item.update({
        "title": str(data.get("title") or "").strip() or f"知乎文章 {article_id}",
        "text": _plain(html_body),
        "author": _with_followers(_author(data.get("author") or {}), ctx),
        "created_at": iso_from_unix(data.get("created")),  # 文章的键是 created，不是 created_time
        "duration": None,
        "canonical_url": f"https://zhuanlan.zhihu.com/p/{article_id}",
        "profile": {
            "ip_location": str(data.get("ip_info") or ""),
            "article_type": str(data.get("article_type") or ""),
            "updated_at": iso_from_unix(data.get("updated")),
            "headline": _headline(data),
        },
        # 文章详情自带完整互动数据，不需要补查（这点和回答相反）
        "stats": {
            "like": as_int(data.get("voteup_count")),      # 赞同
            "comment": as_int(data.get("comment_count")),
            "share": as_int(statistics.get("share_count")),
            "collect": as_int(data.get("favlists_count")),
            "play": as_int(statistics.get("pv_count")),
        },
    })

    images = _html_images(html_body)
    cover = str(_pick(data, "title_image", "image_url", default="") or "")
    if not cover and images:
        cover = images[0]
    ctx.report("正在下载媒体", 35)
    item["media"] = _media_entries(cover, images, ctx)
    if not images:
        ctx.warn("文章正文里没有解析到图片（可能是纯文字文章）")
    item["transcript"] = {"source": "none", "text": "", "segments": []}
    return item


# ---- 想法（pin） ----


def _pin_blocks(data: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    """拆想法正文块：返回 (纯文本, 图片地址, 视频块)。"""
    texts: list[str] = []
    images: list[str] = []
    videos: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            text = _plain(block.get("content") or block.get("own_text"))
            if text:
                texts.append(text)
        elif kind == "image":
            # original_url 是原图，url 是 720w 压缩图；原图优先
            url = str(_pick(block, "original_url", "watermark_url", "url", default="") or "")
            if url.startswith("http") and url not in images:
                images.append(url)
        elif kind == "video":
            videos.append(block)
        elif kind == "link_card":
            # 想法里转的内容卡片也是作者发的一部分，留下链接，但不伪造标题
            url = str(block.get("url") or "")
            if url.startswith("http"):
                texts.append(url)
    return "\n".join(texts), images, videos


def _seconds(value: Any) -> float | None:
    """想法的视频时长是秒（实测 25~60），直接当秒用，不做毫秒猜测。"""
    try:
        if value in (None, ""):
            return None
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _best_pin_video(block: dict[str, Any]) -> tuple[str, int, int, float | None]:
    """挑一路 mp4：码率最高的那档。地址带 auth_key，过期就不能用了。"""
    playlist = [row for row in (block.get("playlist") or []) if isinstance(row, dict)]
    if not playlist:
        return "", as_int(block.get("width")), as_int(block.get("height")), None
    best = max(playlist, key=lambda row: as_int(row.get("bitrate")))
    duration = _seconds(best.get("duration") or block.get("duration"))
    return (str(best.get("url") or ""), as_int(best.get("width")) or as_int(block.get("width")),
            as_int(best.get("height")) or as_int(block.get("height")), duration)


def _fetch_pin(pin_id: str, resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    ctx.begin(make_item_id("zhihu", "text", pin_id))
    ctx.report("正在调用知乎接口", 12, f"想法 {pin_id}")
    payload = get(PIN_PATH, {"pin_id": pin_id})
    ctx.note_api(PIN_PATH)
    data = data_of(payload)
    if not isinstance(data, dict) or not data.get("content"):
        raise TikHubError("知乎没有返回想法内容（想法可能已删除或需要登录）")

    blocks_text, images, videos = _pin_blocks(data)
    # 有视频就是视频，其次图集，都没有才是纯文字
    kind = "video" if videos else ("images" if images else "text")
    item = _start(kind, pin_id, resolved, ctx)
    ctx.save_raw("zhihu_fetch_pin_detail", payload)

    # 想法的 content_html 才是完整正文：text 块里可能又嵌了一层 HTML，只有它能兜住
    body = _plain(data.get("content_html")) or blocks_text
    # excerpt_title 实测也是**带标签的 HTML**（有的想法整段正文都塞在里面），
    # 只取第一行再截断，否则标题会变成一大坨 <p>。
    title = _plain(data.get("excerpt_title")).strip()
    title = title.split("\n")[0].strip()[:80] if title else ""
    if not title:
        title = body.split("\n")[0][:80] if body else f"知乎想法 {pin_id}"

    item.update({
        "title": title,
        "text": body,
        "author": _with_followers(_author(data.get("author") or {}), ctx),
        "created_at": iso_from_unix(data.get("created")),
        "duration": None,
        "canonical_url": f"https://www.zhihu.com/pin/{pin_id}",
        "profile": {
            "pin_type": str(data.get("pin_type") or ""),
            "updated_at": iso_from_unix(data.get("updated")),
            "repin_count": as_int(data.get("repin_count")),
            "headline": _headline(data),
        },
        "stats": {
            "like": as_int(data.get("like_count")),          # 鼓掌/赞同
            "comment": as_int(data.get("comment_count")),
            "share": as_int(data.get("repin_count")),        # 转发
            "collect": as_int(data.get("favorite_count")),   # 收藏
            "play": as_int(data.get("page_view_count")),     # 实测常为 null → 0
        },
    })

    ctx.report("正在下载媒体", 35)
    media = _media_entries(images[0] if images else "", images, ctx)

    video_entry: dict[str, Any] | None = None
    if videos:
        url, width, height, duration = _best_pin_video(videos[0])
        if duration is not None:
            item["duration"] = duration
        if url:
            if ctx.want_media:
                entry = ctx.save_media(url, "video", role="video", media_type="video")
                entry["width"], entry["height"] = width, height
                media.append(entry)
                if entry.get("path"):
                    video_entry = entry
                else:
                    ctx.warn(f"想法视频下载失败（{entry.get('error') or '未知原因'}）")
            else:
                media.append({"role": "video", "type": "video", "path": "", "url": url,
                              "width": width, "height": height, "bytes": 0})
        else:
            ctx.warn("想法里标记了视频但没有可用的播放地址")
    item["media"] = media

    if video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
    elif videos:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}
    else:
        item["transcript"] = {"source": "none", "text": "", "segments": []}
    return item


# ---- 入口 ----


def fetch_post(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """按 ID 类型分发：回答 / 专栏文章 / 想法。"""
    ids = resolved.ids
    if ids.get("answer_id"):
        return _fetch_answer(str(ids["answer_id"]), resolved, ctx)
    if ids.get("article_id"):
        return _fetch_article(str(ids["article_id"]), resolved, ctx)
    if ids.get("pin_id"):
        return _fetch_pin(str(ids["pin_id"]), resolved, ctx)
    raise TikHubError("链接里没有识别到知乎内容 ID（支持回答 / 专栏文章 / 想法）")
