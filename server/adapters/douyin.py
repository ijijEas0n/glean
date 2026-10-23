#!/usr/bin/env python3
"""抖音适配器：单条作品（视频 / 图集）+ 博主主页。

字段路径全部来自实测响应（.fixtures / 探针原始 JSON），不是文档推测。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    first_url,
    iso_from_unix,
    make_item_id,
    normalized_media_candidates,
)

# 标题取不到就**留空**（SPEC §3「取不到就留空，绝不猜测」）。
# 两件事都踩过：① 用业务 ID 当标题（"抖音作品 7683…"）数字又长又难读；
# ② 用「（这条没有文案）」占位，把列表页塞满废话，反而认不出是哪条。
# 空标题由界面兜底显示 平台 · 作者 · 时长，不在数据里编东西。

DETAIL_PATH = "/api/v1/douyin/app/v3/fetch_one_video"
DETAIL_FALLBACK_PATH = "/api/v1/douyin/app/v3/fetch_one_video_v3"
PROFILE_PATH = "/api/v1/douyin/app/v3/handler_user_profile"
POSTS_PATH = "/api/v1/douyin/app/v3/fetch_user_post_videos"
# 播放量有**专门的批量接口**。作品详情接口的 statistics.play_count 恒为 0
# （实测 49 份原始返回全是 0，是接口不给，不是没取），必须走这条才拿得到。
STATS_PATH = "/api/v1/douyin/app/v3/fetch_multi_video_statistics"

# media_type: 2=图集, 4=视频（实测：video 那条=4 且 images 为 null；图集那条=2 且 images 有值）
MEDIA_TYPE_IMAGES = 2
MEDIA_TYPE_ARTICLE = 43      # 实测：文章是 43，description 空的"作品"很多就是它

MAX_PAGES = 12  # 主页翻页上限，防止 --limit 0 时无限翻


def _cover_url(video: dict[str, Any]) -> str:
    """封面优先 dynamic_cover（实测分辨率远高于 cover），但要排掉 vvic 动态图容器。"""
    for key in ("dynamic_cover", "cover", "origin_cover", "cover_original_scale"):
        address = video.get(key) or {}
        urls = [u for u in (address.get("url_list") or []) if isinstance(u, str)]
        if not urls:
            continue
        if key == "dynamic_cover":
            clean = next((u for u in urls if "vvic" not in u and "_offtrans_" not in u), "")
            if not clean:
                continue
            return clean
        return urls[0]
    return ""


def _video_candidates(video: dict[str, Any]) -> list[str]:
    """播放地址优先级：**先保证浏览器能播，再谈码率**。

    踩过的坑：原先是"按码率从高到低挑"，而抖音码率最高的那几档几乎都是 H.265
    （实测 is_h265=2 / is_bytevc1=2）。H.265 在 Chrome 里解不了——
    表现就是"有声音、画面不动"，整条视频等于看不了。
    实测库里 11 条抖音视频全是 HEVC，无一例外。

    所以顺序改成：
      ① play_addr_h264 —— 接口明确标了 H.264 的那路（实测 720x1280 h264，能播）
      ② download_no_watermark_addr —— 无水印，通常也是 H.264
      ③ bit_rate 里没标 H.265 的（按码率从高到低）
      ④ 剩下的（H.265）—— 兜底，能下到总比没有好，但会记一条警告
      ⑤ play_addr
    代价：H.264 压缩率低，同样内容文件大 2~3 倍（实测 28MB vs 9.3MB）。
    能播比省空间重要。
    """
    rows: list[str] = []

    def add(address: object) -> None:
        if isinstance(address, dict):
            rows.extend(u for u in (address.get("url_list") or []) if isinstance(u, str))

    def 是HEVC(item: dict[str, Any]) -> bool:
        # 实测这两个字段是 1/2 表示 H.265，0/缺失 才是 H.264
        return as_int(item.get("is_h265")) > 0 or as_int(item.get("is_bytevc1")) > 0

    档位 = [x for x in (video.get("bit_rate") or []) if isinstance(x, dict)]
    档位.sort(key=lambda x: as_int(x.get("bit_rate")), reverse=True)

    add(video.get("play_addr_h264"))                    # ①
    add(video.get("download_no_watermark_addr"))        # ②
    for item in 档位:                                    # ③
        if not 是HEVC(item):
            add(item.get("play_addr"))
    for item in 档位:                                    # ④
        if 是HEVC(item):
            add(item.get("play_addr"))
    add(video.get("play_addr"))                         # ⑤
    return normalized_media_candidates(rows)


def _stats(statistics: dict[str, Any]) -> dict[str, int]:
    statistics = statistics or {}
    return {
        "like": as_int(statistics.get("digg_count")),
        "comment": as_int(statistics.get("comment_count")),
        "share": as_int(statistics.get("share_count")),
        "collect": as_int(statistics.get("collect_count")),
        "play": as_int(statistics.get("play_count")),
    }


def fetch_play_counts(aweme_ids: Iterable[str], 每批: int = 50) -> dict[str, int]:
    """批量取播放量，返回 {aweme_id: play_count}。

    播放量是判断内容质量最关键的指标——它能把"粉丝多所以赞多"和"内容真的好"分开。
    作品详情接口不给这个数（statistics.play_count 恒 0），只能走批量统计接口，
    一次能传多个 ID，所以很便宜：一个主页几十条作品也就一两次调用。

    取不到就返回部分结果/空字典，**不抛异常**——播放量是加分项，不该拖垮整次抓取。
    """
    ids = [str(x).strip() for x in aweme_ids if str(x).strip()]
    结果: dict[str, int] = {}
    for i in range(0, len(ids), 每批):
        chunk = ids[i:i + 每批]
        try:
            payload = get(STATS_PATH, {"aweme_ids": ",".join(chunk)})
        except Exception:  # noqa: BLE001 - 播放量取不到不该让整条任务失败
            continue
        rows = ((payload or {}).get("data") or {}).get("statistics_list") or []
        for row in rows:
            if isinstance(row, dict):
                aid = str(row.get("aweme_id") or "")
                if aid:
                    结果[aid] = as_int(row.get("play_count"))
    return 结果


def 补播放量(ctx: Any, item: dict[str, Any], aweme_id: str) -> None:
    """给单条 item 补上播放量。

    批量抓取时会跳过（`补播放量` 选项为 False），由 run_batch 收尾**一次**补完——
    逐条调的话 N 条就是 N 次接口，白花钱。
    取不到就不动，播放量是加分项，不该影响抓取成败。
    """
    if not aweme_id or not ctx.options.get("补播放量", True):
        return
    播放 = fetch_play_counts([aweme_id])
    if aweme_id in 播放:
        item.setdefault("stats", {})["play"] = 播放[aweme_id]
        ctx.note_api(STATS_PATH)


def _author(author: dict[str, Any]) -> dict[str, Any]:
    author = author or {}
    sec_uid = str(author.get("sec_uid") or "")
    return {
        "name": str(author.get("nickname") or ""),
        "handle": str(author.get("unique_id") or author.get("short_id") or ""),
        "id": sec_uid,
        "avatar": first_url(author.get("avatar_larger"), author.get("avatar_thumb"), author.get("avatar_medium")),
        "url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else "",
        "followers": as_int(author.get("follower_count")),
    }


def _media_entries(aweme: dict[str, Any], ctx: FetchContext) -> list[dict[str, Any]]:
    """封面 + 图集原图落盘。图集一律全收，不因为能下视频就丢图。"""
    entries: list[dict[str, Any]] = []
    video = aweme.get("video") or {}
    cover = _cover_url(video)
    if cover and ctx.want_media:
        entries.append(ctx.save_media(cover, "cover", role="cover", media_type="image"))

    images = aweme.get("images") or []
    for index, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            continue
        url = first_url(
            [u for u in (image.get("url_list") or []) if "vvic" not in u and "_offtrans_" not in u],
            image.get("download_url_list"),
        )
        if not url:
            continue
        if ctx.want_media:
            entries.append(ctx.save_media(url, f"{index:03d}", role="content", media_type="image"))
        else:
            entries.append({"role": "content", "type": "image", "path": "", "url": url,
                            "width": as_int(image.get("width")), "height": as_int(image.get("height")),
                            "bytes": 0})
    return entries


def _download_video(aweme: dict[str, Any], ctx: FetchContext) -> dict[str, Any] | None:
    """下载视频到 media/。全部地址失败返回 None（不抛，让上层记 warning）。"""
    video = aweme.get("video") or {}
    candidates = _video_candidates(video)
    if not candidates:
        return None
    from ..tikhub import download as download_url

    target = ctx.work_dir / "video.mp4"
    errors: list[str] = []
    兜底: tuple[Any, ...] | None = None      # 能用但不理想的（比如 HEVC）

    for index, url in enumerate(candidates, start=1):
        try:
            size = download_url(url, target, referer="https://www.douyin.com/")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"线路{index}:{type(exc).__name__}")
            continue

        from .. import store as store_module
        from ..media import (BROWSER_OK_CODECS, has_video_stream, probe_duration,
                             probe_video_codec)

        # ① 得真有视频轨。抖音有些地址给的是**纯音频**（实测下到 59KB 的 mp3、
        #    或只有 aac 音轨的 mp4），不校验的话"视频"点开只有声音。
        if not has_video_stream(target):
            errors.append(f"线路{index}:只有音频没有画面")
            continue

        # ② 编码得是浏览器能播的。能播的优先，HEVC 先留着当兜底继续试后面的。
        编码 = probe_video_codec(target)
        if 编码 and 编码 not in BROWSER_OK_CODECS:
            if 兜底 is None:
                兜底 = (size, 编码, target.read_bytes())
            errors.append(f"线路{index}:{编码}(浏览器放不了)")
            continue

        relative = store_module.save_media(ctx.item_id, "video.mp4", target.read_bytes())
        return {
            "role": "video", "type": "video", "path": relative, "url": "",
            "width": as_int(video.get("width")), "height": as_int(video.get("height")),
            "bytes": size, "duration": probe_duration(target),
        }

    # 所有 H.264 线路都不行，但有能播画面的 HEVC —— 留着总比没有好，如实警告
    if 兜底:
        size, 编码, 数据 = 兜底
        from .. import store as store_module

        relative = store_module.save_media(ctx.item_id, "video.mp4", 数据)
        ctx.warn(f"这条只有 {编码.upper()} 版本，浏览器多半放不出画面（只有声音）。"
                 f"转写不受影响。")
        from ..media import probe_duration

        return {
            "role": "video", "type": "video", "path": relative, "url": "",
            "width": as_int(video.get("width")), "height": as_int(video.get("height")),
            "bytes": size, "duration": probe_duration(target),
        }

    ctx.warn(f"视频下载失败（{len(candidates)} 条线路都不可用：{'，'.join(errors[:4])}）")
    return None


def _transcribe_entry(entry: dict[str, Any] | None, ctx: FetchContext) -> dict[str, Any]:
    """按设置转写，并如实返回 transcript 对象。"""
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


def fetch_one(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条抖音作品（视频或图集）。"""
    aweme_id = resolved.ids.get("aweme_id") or ""
    ctx.begin(make_item_id("douyin", "video", aweme_id))  # 占位：确定类型后会改成正确的 id
    item = empty_item("douyin", "video", resolved)

    ctx.report("正在调用抖音接口", 12, f"作品 {aweme_id}")
    try:
        payload = get(DETAIL_PATH, {"aweme_id": aweme_id})
        ctx.note_api(DETAIL_PATH)
    except TikHubError as first_error:
        payload = get(DETAIL_FALLBACK_PATH, {"aweme_id": aweme_id})
        ctx.note_api(DETAIL_FALLBACK_PATH)
        ctx.warn(f"主接口不可用，已自动切换兼容接口（{first_error}）")

    aweme = (data_of(payload) or {}).get("aweme_detail") or {}
    if not aweme:
        raise TikHubError("抖音没有返回作品数据（作品可能已删除或仅自己可见）")

    media_type = as_int(aweme.get("media_type"))
    # media_type 43 其实是**文章**。以前这里只判了 2/4，43 就落到"视频"分支，
    # 结果把文章当视频抓：play_addr 给的是静态图，下回来只有音频、没有画面。
    # 认出来就改走文章那条路（fetch_article）。
    if media_type == MEDIA_TYPE_ARTICLE:
        # payload 已经在手上了，一起带过去，别让文章这条路再调一次接口
        return fetch_article(resolved, ctx, payload)
    images = aweme.get("images") or []
    has_images = bool(images) and any(isinstance(x, dict) and x.get("url_list") for x in images)
    # media_type: 2=图集, 4=视频（实测）
    kind = "images" if (has_images or media_type == MEDIA_TYPE_IMAGES) else "video"

    # 类型定下来之后再落 item_id：raw/ 与 media/ 都要进同一个目录
    item_id = ctx.begin(make_item_id("douyin", kind, aweme_id))
    item = empty_item("douyin", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("douyin_fetch_one_video", payload)

    video = aweme.get("video") or {}
    duration_ms = as_int(video.get("duration"))
    # item_title 是干净标题（"AI零代码开发 10分钟搞定小程序"），desc 里塞满话题标签，
    # 只适合当正文；标题优先 item_title。
    clean_title = str(aweme.get("item_title") or "").strip()
    item.update({
        "title": clean_title or str(aweme.get("desc") or "").strip(),
        "text": str(aweme.get("desc") or "").strip(),
        "author": _author(aweme.get("author") or {}),
        "created_at": iso_from_unix(aweme.get("create_time")),
        "duration": round(duration_ms / 1000, 3) if duration_ms else None,
        "stats": _stats(aweme.get("statistics") or {}),
        "canonical_url": f"https://www.douyin.com/video/{aweme_id}",
    })
    补播放量(ctx, item, aweme_id)

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = _media_entries(aweme, ctx)
    video_entry: dict[str, Any] | None = None
    if kind == "video" and ctx.want_media:
        video_entry = _download_video(aweme, ctx)
        if video_entry:
            media.append(video_entry)
    elif kind == "video" and not ctx.want_media:
        candidates = _video_candidates(video)
        if candidates:
            media.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                          "width": 0, "height": 0, "bytes": 0})
    item["media"] = media

    if kind == "video" and video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
        ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
    elif kind == "video":
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if kind == "images" and not has_images:
        ctx.warn("接口标记为图集但没有取到图片地址")
    return item


def fetch_article(
    resolved: Resolved,
    ctx: FetchContext,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """抖音文章。

    实测：文章没有独立端点，用作品接口传 aweme_id 就能取到，
    返回的 `aweme_detail.media_type == 43`，正文在
    `article_info.article_content`（一个 JSON 字符串，里面的 `markdown` 才是正文）。

    `payload` 是 fetch_one 已经调过接口拿到的数据。能复用就别再调一次——
    文章是先调作品接口认出 media_type=43 才转过来的，那份数据本来就在手上。
    """
    import json as json_module

    # 文章的 ID 和作品的 aweme_id 是同一串数字。只认 article_id 的话，
    # 从主页清单过来的链接是 `/video/<id>`，解析出的是 aweme_id，article_id 为空 →
    # 拿空字符串去调接口，TikHub 回 422「aweme_id Field required」，文章 100% 抓不下来（踩过）。
    article_id = (resolved.ids.get("article_id") or resolved.ids.get("aweme_id") or "")
    item = empty_item("douyin", "article", resolved)
    ctx.begin(make_item_id("douyin", "article", article_id))

    if payload is None:
        ctx.report("正在调用抖音接口", 12, f"文章 {article_id}")
        try:
            payload = get(DETAIL_PATH, {"aweme_id": article_id})
            ctx.note_api(DETAIL_PATH)
        except TikHubError as first_error:
            payload = get(DETAIL_FALLBACK_PATH, {"aweme_id": article_id})
            ctx.note_api(DETAIL_FALLBACK_PATH)
            ctx.warn(f"主接口不可用，已自动切换兼容接口（{first_error}）")

    aweme = (data_of(payload) or {}).get("aweme_detail") or {}
    if not aweme:
        raise TikHubError("抖音没有返回文章数据（文章可能已删除或仅自己可见）")

    item_id = ctx.begin(make_item_id("douyin", "article", article_id))
    item = empty_item("douyin", "article", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw("douyin_fetch_one_video_article", payload)

    info = aweme.get("article_info") or {}
    title = str(info.get("article_title") or "").strip()
    body = ""
    raw_content = info.get("article_content")
    if isinstance(raw_content, str) and raw_content.strip():
        try:
            parsed = json_module.loads(raw_content)
            if isinstance(parsed, dict):
                # markdown 是正文；abstract 是它的截断版，只在没有 markdown 时兜底
                body = str(parsed.get("markdown") or parsed.get("long_article_abstract") or "").strip()
        except json_module.JSONDecodeError:
            # 不是 JSON 就直接当正文用，不丢内容
            body = raw_content.strip()
    elif isinstance(raw_content, dict):
        body = str(raw_content.get("markdown") or raw_content.get("long_article_abstract") or "").strip()

    media_type = as_int(aweme.get("media_type"))
    item.update({
        "title": title or str(aweme.get("desc") or "").strip(),
        "text": body,
        "author": _author(aweme.get("author") or {}),
        "created_at": iso_from_unix(aweme.get("create_time")),
        "duration": None,
        "stats": _stats(aweme.get("statistics") or {}),
        "canonical_url": f"https://www.douyin.com/article/{article_id}",
        "profile": {
            "read_time": as_int(info.get("read_time")),
            "article_type": as_int(info.get("article_type")),
        },
    })
    补播放量(ctx, item, article_id)
    if media_type != 43:
        ctx.warn(f"这条链接的 media_type 是 {media_type}（不是文章 43），内容可能不完整")

    ctx.report("正在下载媒体", 40)
    item["media"] = _media_entries(aweme, ctx)
    if not body:
        ctx.warn("接口没有返回文章正文（可能需要在 App 内查看）")
    return item


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """抖音博主主页：资料 + 作品列表（可选逐条也落成独立 item）。"""
    sec_uid = resolved.ids.get("sec_user_id") or ""
    item_id = ctx.begin(make_item_id("douyin", "profile", sec_uid))
    item = empty_item("douyin", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照

    ctx.report("正在读取博主资料", 10)
    profile_payload = get(PROFILE_PATH, {"sec_user_id": sec_uid})
    ctx.note_api(PROFILE_PATH)
    ctx.save_raw("douyin_handler_user_profile", profile_payload)
    user = (data_of(profile_payload) or {}).get("user") or {}
    if not user:
        raise TikHubError("抖音没有返回博主资料（可能 sec_uid 不正确或账号已注销）")

    author = {
        "name": str(user.get("nickname") or ""),
        "handle": str(user.get("unique_id") or user.get("short_id") or ""),
        "id": sec_uid,
        "avatar": first_url(user.get("avatar_thumb"), user.get("avatar_medium"), user.get("avatar_larger")),
        "url": f"https://www.douyin.com/user/{sec_uid}",
        "followers": as_int(user.get("follower_count")),
    }
    limit = ctx.limit
    item.update({
        "title": f"{author['name']}的抖音主页" if author["name"] else "抖音博主主页",
        "author": author,
        "profile": {
            "signature": str(user.get("signature") or ""),
            "aweme_count": as_int(user.get("aweme_count")),
            "following_count": as_int(user.get("following_count")),
            "total_favorited": as_int(user.get("total_favorited")),
            "verified": str(user.get("custom_verify") or user.get("enterprise_verify_reason") or ""),
            "ip_location": str(user.get("ip_location") or ""),
        },
        "stats": {"like": as_int(user.get("total_favorited")), "comment": 0, "share": 0,
                  "collect": 0, "play": as_int(user.get("follower_count"))},
        # 主页总数给界面用（判断"还有没有更多"），不当警告
        # aweme_count 已在上面 profile 里
    })

    ctx.report("正在拉取作品列表", 30, f"最多 {limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    cursor = ""
    page = 0
    while page < MAX_PAGES:
        page += 1
        query: dict[str, Any] = {"sec_user_id": sec_uid, "count": 20}
        if cursor:
            query["max_cursor"] = cursor
        payload = get(POSTS_PATH, query)
        ctx.note_api(POSTS_PATH)
        if page == 1:
            ctx.save_raw("douyin_fetch_user_post_videos", payload)
        data = data_of(payload) or {}
        rows = data.get("aweme_list") or []
        if not rows:
            break
        for aweme in rows:
            if not isinstance(aweme, dict):
                continue
            aweme_id = str(aweme.get("aweme_id") or "")
            images = aweme.get("images") or []
            媒体类型 = as_int(aweme.get("media_type"))
            is_images = 媒体类型 == MEDIA_TYPE_IMAGES or bool(images)
            # 43 是文章，别塞进"视频"里（塞了就会去下视频，拿回来只有音频）
            子类型 = "article" if 媒体类型 == MEDIA_TYPE_ARTICLE else ("images" if is_images else "video")
            video = aweme.get("video") or {}
            duration_ms = as_int(video.get("duration"))
            children.append({
                "id": make_item_id("douyin", 子类型, aweme_id),
                # 业务 ID 显式存一份：item_id 含"类型"这种派生信息，
                # 不同接口判定可能不一致，判"在不在本地"一律按 biz_id 匹配。
                "biz_id": aweme_id,
                "title": str(aweme.get("desc") or "").strip(),
                "kind": 子类型,
                "duration": round(duration_ms / 1000, 3) if duration_ms else None,
                "stats": _stats(aweme.get("statistics") or {}),
                "created_at": iso_from_unix(aweme.get("create_time")),
                "url": f"https://www.douyin.com/video/{aweme_id}",
                "cover": _cover_url(video),
                "local": False,
            })
            if limit and len(children) >= limit:
                break
        item["children"] = children
        ctx.report("正在拉取作品列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if limit and len(children) >= limit:
            break
        if not as_int(data.get("has_more")):
            break
        next_cursor = str(data.get("max_cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    item["children"] = children
    # 播放量单独**批量**补：列表/详情接口的 play_count 恒为 0（是接口不给），
    # 只有批量统计接口有。一次调用拿一整页，很便宜；取不到就保持 0，不拖垮抓取。
    if children:
        ctx.report("正在补播放量", 72, f"{len(children)} 条")
        播放 = fetch_play_counts([str(c.get("biz_id") or "") for c in children])
        for c in children:
            aid = str(c.get("biz_id") or "")
            if aid in 播放:
                c.setdefault("stats", {})["play"] = 播放[aid]
        if 播放:
            ctx.note_api(STATS_PATH)
    if not children:
        ctx.warn("没有取到任何作品（主页可能为空或接口受限）")
    # 注意：这里**不要**因为"拉满了 limit"就发警告。那是预期行为——
    # 界面有「再看 20 条」可以继续拉。之前发了黄条，看着像出错，用户会困惑。
    # 总数写进 profile，界面自己会显示"他一共 N 条"。
    return item
