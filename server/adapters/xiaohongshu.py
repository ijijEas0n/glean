#!/usr/bin/env python3
"""小红书适配器：单条笔记（视频 / 图文）+ 博主主页。

坑（实测）：
- 笔记数据藏在 `data.data[].note_list[]`，不是直接在 data 下面。
- 同一个平台两套字段名：详情接口用 liked_count / time，列表接口用 likes / create_time。
  所以统一走 `_pick()` 做多字段名兼容，而不是假定只有一种。
- `origin_img` 是个 {width,height} 字典，不是 URL；图片地址取 url_size_large。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_float,
    as_int,
    empty_item,
    first_url,
    iso_from_unix,
    make_item_id,
    normalized_media_candidates,
)

# 标题取不到就**留空**（SPEC §3「取不到就留空，绝不猜测」）。
# 踩过：用「（这条没有文案）」占位会把列表页塞满废话，反而认不出是哪条。
# 空标题由界面兜底显示 平台 · 作者 · 时长，不在数据里编东西。

VIDEO_DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_video_note_detail"
IMAGE_DETAIL_PATH = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
USER_POSTS_PATH = "/api/v1/xiaohongshu/app_v2/get_user_posted_notes"
USER_INFO_PATH = "/api/v1/xiaohongshu/web_v3/fetch_user_info"

# 已确认这两家视频 CDN 同 host 的 443 可用。部分 fake-ip/代理环境下
# TikHub 给的 http://…:80 会一直卡到下载超时（默认 120s，再乘代理+直连）。
# 只用「相等或点分后缀」匹配，避免 notxhscdn.com / xhscdn.com.example.org 被误升级。
_XHS_HTTPS_CDN_DOMAINS = ("rednotecdn.com", "xhscdn.com")


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """多字段名兼容：同一份数据在不同端点上字段名不一致。"""
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _note_from_detail(payload: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    """详情接口：data.data[].note_list[]，取 id 匹配的那条。"""
    rows = (data_of(payload) or {}).get("data") or []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        for note in item.get("note_list") or []:
            if isinstance(note, dict) and str(note.get("id") or "").lower() == note_id.lower():
                return note
        # 少数返回没有 note_list，note 直接挂在 item 上
        if str(item.get("id") or "").lower() == note_id.lower():
            return item
    return None


def _author(user: dict[str, Any]) -> dict[str, Any]:
    user = user or {}
    user_id = str(_pick(user, "userid", "user_id", "id", default="") or "")
    avatar = str(_pick(user, "images", "image", "avatar", default="") or "")
    return {
        "name": str(_pick(user, "nickname", "nick_name", "name", default="") or ""),
        "handle": str(user.get("red_id") or ""),
        "id": user_id,
        "avatar": avatar,
        "url": f"https://www.xiaohongshu.com/user/profile/{user_id}" if user_id else "",
        "followers": as_int(_pick(user, "fans", "fans_count", "follower_count", default=0)),
    }


def _stats(note: dict[str, Any]) -> dict[str, int]:
    return {
        "like": as_int(_pick(note, "liked_count", "likes", "like_count", default=0)),
        "comment": as_int(_pick(note, "comments_count", "comment_count", default=0)),
        "share": as_int(_pick(note, "shared_count", "share_count", default=0)),
        "collect": as_int(_pick(note, "collected_count", "collect_count", default=0)),
        "play": as_int(_pick(note, "view_count", "views", default=0)),
    }


def _image_urls(note: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for image in note.get("images_list") or []:
        if not isinstance(image, dict):
            continue
        url = first_url(
            image.get("url_size_large"),
            image.get("url"),
            (image.get("url_multi_level") or {}).get("origin") if isinstance(image.get("url_multi_level"), dict) else "",
            (image.get("url_multi_level") or {}).get("low") if isinstance(image.get("url_multi_level"), dict) else "",
        )
        if url and url not in rows:
            rows.append(url)
    return rows


def _is_xhs_https_cdn(host: str) -> bool:
    """host 是否为 rednotecdn.com / xhscdn.com 或其子域名。"""
    return any(host == domain or host.endswith("." + domain) for domain in _XHS_HTTPS_CDN_DOMAINS)


def _netloc_for_https(netloc: str, port: int | None) -> str | None:
    """换成 https 时用的 netloc。显式 80 端口去掉；没法安全改就返回 None。"""
    if "@" in netloc or netloc.startswith("["):
        return None
    if port is None:
        # `host:` 这种空端口不是默认 80，原样留下，避免改成一个坏地址。
        if ":" in netloc:
            return None
        return netloc
    host, sep, port_text = netloc.rpartition(":")
    if not sep or not host or not port_text:
        return None
    try:
        if int(port_text) != 80:
            return None
    except ValueError:
        return None
    return host


def normalize_xhs_video_url(url: str) -> str:
    """把小红书视频 CDN 的 http 升级为 https。

    只处理 rednotecdn.com / xhscdn.com 及其子域名。已是 https 的地址原样返回；
    path、query、片段和签名逐字保留，不解码再编码。显式 :80（含 :0080）会去掉，
    其他端口、用户信息、非法端口一律不动。

    不把原 HTTP 留在候选里：下载再退回这条 HTTP 会重新卡在 80 端口的长超时上。
    其他候选（含升级后的备用 HTTPS）仍按原顺序继续试。不关闭 TLS 校验。
    """
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme != "http":
        return url
    if parts.username is not None or parts.password is not None:
        return url
    host = (parts.hostname or "").rstrip(".").lower()
    if not _is_xhs_https_cdn(host):
        return url
    try:
        port = parts.port
    except ValueError:
        return url
    if port not in (None, 80):
        return url
    netloc = _netloc_for_https(parts.netloc, port)
    if netloc is None:
        return url
    return parts._replace(scheme="https", netloc=netloc).geturl()


def _video_candidates(note: dict[str, Any]) -> list[str]:
    """H.264 优先：转写要用 ffmpeg 读音频，高压缩编码容易失败。

    收集完再升级协议、再去重。同一条资源经常同时给 HTTP 主地址和 HTTPS
    备用地址，升级后才能并成一条，且不会把原 HTTP 留进下载循环。
    """
    media = ((note.get("video_info_v2") or {}).get("media") or {})
    streams = media.get("stream") or {}
    if not isinstance(streams, dict):
        return []
    rows: list[str] = []
    for codec in ("h264", "h265", "h266", "av1"):
        variants = streams.get(codec) or []
        ordered = sorted(
            (x for x in variants if isinstance(x, dict)),
            key=lambda x: (
                as_int(x.get("default_stream")),
                as_int(x.get("width")) * as_int(x.get("height")),
                as_int(x.get("weight")),
            ),
            reverse=True,
        )
        for variant in ordered:
            for candidate in [variant.get("master_url"), *(variant.get("backup_urls") or [])]:
                if isinstance(candidate, str) and candidate.startswith("http") and candidate not in rows:
                    rows.append(candidate)
    return normalized_media_candidates([normalize_xhs_video_url(url) for url in rows])


def _duration_seconds(note: dict[str, Any]) -> float | None:
    """时长。

    实测两个来源单位不同：`video_info_v2.capa.duration` 是**秒**（如 52），
    `media.stream.h264[0].duration` 是**毫秒**（52200）。优先用 capa，
    拿不到再按毫秒量级判断后换算。
    """
    info = note.get("video_info_v2") or {}
    capa = info.get("capa") or {}
    raw = _pick(capa, "duration", "video_duration", default=None)
    if raw is not None:
        value = as_float(raw)
        if value:
            return round(value, 3)
    media = (info.get("media") or {}).get("video") or {}
    raw = _pick(media, "duration", default=None)
    if raw is None:
        streams = (info.get("media") or {}).get("stream") or {}
        for codec in ("h264", "h265", "h266", "av1"):
            variants = streams.get(codec) or []
            if variants and isinstance(variants[0], dict):
                raw = variants[0].get("duration")
                break
    if raw is None:
        return None
    value = as_float(raw)
    if not value:
        return None
    # 流级别是毫秒；只转换明显是毫秒的量级，保住真实的亚秒值
    return round(value / 1000, 3) if value >= 1000 else round(value, 3)


def _video_cover(note: dict[str, Any]) -> str:
    """视频笔记的封面。

    视频笔记的 images_list 里没有可用大图（url_size_large 为空），
    封面只能从 `video_info_v2.image.thumbnail / first_frame` 拿。
    """
    image = (note.get("video_info_v2") or {}).get("image") or {}
    return first_url(image.get("first_frame"), image.get("thumbnail"))


def parse_display_count(value: Any) -> int:
    """把"1千+ / 1.2万 / 3.4亿 / 1234"这类展示用数字解析成整数。

    小红书粉丝数只给展示字符串（interactions.list[].count），没有原始数字，
    所以只能解析这个字符串；解析不出来就返回 0（宁可留空也不编）。
    """
    text = str(value or "").strip().replace(",", "").replace("+", "")
    if not text:
        return 0
    unit = 1
    for suffix, factor in (("亿", 100_000_000), ("万", 10_000), ("千", 1_000), ("w", 10_000), ("k", 1_000), ("K", 1_000)):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            unit = factor
            break
    try:
        return int(float(text) * unit)
    except (TypeError, ValueError):
        return 0


def _profile_interactions(rows: Any) -> dict[str, int]:
    """interactions.list[] → {fans: N, follows: N}。"""
    result: dict[str, int] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "")
        if kind in ("fans", "follows", "interaction"):
            result[kind] = parse_display_count(row.get("count"))
    return result


def _title(note: dict[str, Any], note_id: str) -> str:
    title = str(_pick(note, "title", "display_title", default="") or "").strip()
    if title:
        return title
    desc = str(note.get("desc") or "").strip()
    if desc:
        # 只取第一行当标题；话题标签由 fetcher._clean_titles 统一拆掉
        return desc.split("\n")[0][:80]
    return ""


def fetch_note(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条小红书笔记。先按视频问，拿不到再按图文问。"""
    note_id = resolved.ids.get("note_id") or ""
    if not note_id:
        raise TikHubError("链接里没有识别到小红书笔记 ID")
    ctx.begin(make_item_id("xiaohongshu", "unknown", note_id))

    ctx.report("正在调用小红书接口", 12, f"笔记 {note_id}")
    note: dict[str, Any] | None = None
    note_type = ""
    video_error = ""
    # 图文和视频是两个端点，而链接本身不带类型信息，只能试。
    # 实测两种失败形态都有：视频接口对图文帖可能"什么都不返回"，也可能返回 note
    # 但里面没有 video_info_v2。两种都要能落到图文端点，所以统一判"拿不到播放地址就换"。
    # 代价：图文帖会多花一次接口调用（视频帖一次就够）。
    try:
        payload = get(VIDEO_DETAIL_PATH, {"note_id": note_id})
        ctx.note_api(VIDEO_DETAIL_PATH)
        note = _note_from_detail(payload, note_id)
        if note and not _video_candidates(note):
            # 返回了但不是视频：换图文端点拿完整图集（这里不能直接用，图集会缺）
            note = None
            note_type = ""
    except TikHubError as exc:
        video_error = str(exc)

    if note is None:
        ctx.report("正在调用小红书接口", 20, "按图文笔记重试")
        payload = get(IMAGE_DETAIL_PATH, {"note_id": note_id})
        ctx.note_api(IMAGE_DETAIL_PATH)
        note = _note_from_detail(payload, note_id)
        note_type = str((note or {}).get("type") or "")

    if not note:
        detail = f"（视频接口：{video_error}）" if video_error else ""
        raise TikHubError(f"小红书没有返回笔记数据{detail}，笔记可能已删除或需要登录")

    has_video = bool(_video_candidates(note)) or note_type == "video"
    kind = "video" if has_video else ("images" if _image_urls(note) else "text")
    item_id = ctx.begin(make_item_id("xiaohongshu", kind, note_id))
    item = empty_item("xiaohongshu", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    ctx.save_raw(f"xiaohongshu_{'video' if has_video else 'image'}_note_detail", payload)

    item.update({
        "title": _title(note, note_id),
        "text": str(note.get("desc") or "").strip(),
        "author": _author(note.get("user") or {}),
        "created_at": iso_from_unix(_pick(note, "time", "create_time", default=0)),
        "duration": _duration_seconds(note) if has_video else None,
        "stats": _stats(note),
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "profile": {"ip_location": str(note.get("ip_location") or "")},
    })

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = []
    image_urls = _image_urls(note)
    video_candidates = _video_candidates(note)

    if has_video:
        # 视频笔记的 images_list 没有可用大图，封面单独从 video_info_v2.image 取，
        # 否则视频条目会完全没有封面（踩过）。
        cover = _video_cover(note)
        if cover:
            media.append(ctx.save_media(cover, "cover", role="cover", media_type="image",
                                        referer="https://www.xiaohongshu.com/")
                         if ctx.want_media else
                         {"role": "cover", "type": "image", "path": "", "url": cover,
                          "width": 0, "height": 0, "bytes": 0})
    elif image_urls:
        if ctx.want_media:
            media.append(ctx.save_media(image_urls[0], "cover", role="cover", media_type="image",
                                       referer="https://www.xiaohongshu.com/"))
        else:
            media.append({"role": "cover", "type": "image", "path": "", "url": image_urls[0],
                          "width": 0, "height": 0, "bytes": 0})

    if image_urls and not has_video:
        if ctx.want_media:
            for index, url in enumerate(image_urls, start=1):
                media.append(ctx.save_media(url, f"{index:03d}", role="content", media_type="image",
                                            referer="https://www.xiaohongshu.com/"))
        else:
            for index, url in enumerate(image_urls, start=1):
                media.append({"role": "content", "type": "image",
                              "path": "", "url": url, "width": 0, "height": 0, "bytes": 0})

    video_entry: dict[str, Any] | None = None
    if has_video and video_candidates and ctx.want_media:
        from .. import store
        from ..media import probe_duration
        from ..tikhub import download as download_url

        target = ctx.work_dir / "video.mp4"
        for index, url in enumerate(video_candidates, start=1):
            try:
                size = download_url(url, target, referer="https://www.xiaohongshu.com/")
            except Exception:  # noqa: BLE001 - 换下一条线路
                continue
            relative = store.save_media(item_id, "video.mp4", target.read_bytes())
            video_entry = {"role": "video", "type": "video", "path": relative, "url": "",
                           "width": 0, "height": 0, "bytes": size, "duration": probe_duration(target)}
            media.append(video_entry)
            break
        if video_entry is None:
            ctx.warn(f"视频下载失败（{len(video_candidates)} 条线路都不可用）")
    elif has_video and video_candidates:
        media.append({"role": "video", "type": "video", "path": "", "url": video_candidates[0],
                      "width": 0, "height": 0, "bytes": 0})

    item["media"] = media

    if video_entry and ctx.want_transcribe:
        from .. import store as store_module
        from ..media import transcribe_video

        ctx.report("正在转写语音", 62)
        path = store_module.media_abs_path(item_id, video_entry["path"])
        if path:
            engine = str(ctx.options.get("transcribe_engine") or "volcengine")
            result = transcribe_video(path, ctx.work_dir, engine=engine)
            if result.get("source") == "none" and result.get("note"):
                ctx.warn(str(result["note"]))
            item["transcript"] = result
            ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
    elif has_video:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if not image_urls and not video_candidates:
        ctx.warn("这条笔记既没有图片也没有视频（可能是纯文字帖）")
    return item


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """小红书博主主页：资料 + 笔记清单。"""
    user_id = resolved.ids.get("user_id") or ""
    item = empty_item("xiaohongshu", "profile", resolved)
    ctx.begin(make_item_id("xiaohongshu", "profile", user_id))

    author: dict[str, Any] = {"name": "", "handle": "", "id": user_id, "avatar": "",
                              "url": resolved.url, "followers": 0}
    signature = ""
    ip_location = ""
    note_count = 0
    following_count = 0
    if user_id:
        ctx.report("正在读取博主资料", 10)
        try:
            payload = get(USER_INFO_PATH, {"user_id": user_id})
            ctx.note_api(USER_INFO_PATH)
            # 这个端点也是双层包装：真资料在 data.data.basic_info；
            # 粉丝/关注在 interactions 里，而且只给展示字符串（"1千+"）。
            # 注意：interactions 的形态不稳定——有时是 {list: [...]}，有时直接就是 [...]，
            # 实测两次请求拿到过两种，所以两种都要兼容。
            outer = data_of(payload) or {}
            if not isinstance(outer, dict):
                raise TikHubError("小红书用户资料返回了非预期的结构")
            inner = outer.get("data") if isinstance(outer.get("data"), dict) else outer
            basic = inner.get("basic_info") if isinstance(inner.get("basic_info"), dict) else inner
            if isinstance(basic, dict):
                author.update({
                    "name": str(_pick(basic, "nickname", "nick_name", "name", default="") or ""),
                    "handle": str(basic.get("red_id") or ""),
                    "id": user_id,
                    "avatar": str(_pick(basic, "images", "image", "imageb", default="") or ""),
                    "url": f"https://www.xiaohongshu.com/user/profile/{user_id}",
                })
                signature = str(_pick(basic, "desc", "signature", "introduction", default="") or "")
                ip_location = str(basic.get("ip_location") or "")
            raw_interactions = inner.get("interactions")
            if isinstance(raw_interactions, dict):
                interaction_rows = raw_interactions.get("list")
            elif isinstance(raw_interactions, list):
                interaction_rows = raw_interactions
            else:
                interaction_rows = []
            counts = _profile_interactions(interaction_rows)
            author["followers"] = counts.get("fans", 0)
            following_count = counts.get("follows", 0)
            note_count = as_int(inner.get("posted"))
            ctx.save_raw("xiaohongshu_fetch_user_info", payload)
        except TikHubError as exc:
            ctx.warn(f"这次没读到博主资料（{exc}），下面的作品清单是准的；"
                     f"博主名和头像改用作品清单里带的")

    item_id = ctx.begin(make_item_id("xiaohongshu", "profile", user_id))
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容
    item["author"] = author
    item["title"] = f"{author['name']}的小红书主页" if author["name"] else "小红书博主主页"
    item["profile"] = {
        "signature": signature,
        "ip_location": ip_location,
        "note_count": note_count,
        "following_count": following_count,
    }

    ctx.report("正在拉取笔记列表", 30, f"最多 {ctx.limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    cursor = ""
    page = 0
    while page < 12:
        page += 1
        query: dict[str, Any] = {"user_id": user_id}
        if cursor:
            query["cursor"] = cursor
        payload = get(USER_POSTS_PATH, query)
        ctx.note_api(USER_POSTS_PATH)
        if page == 1:
            ctx.save_raw("xiaohongshu_get_user_posted_notes", payload)
        inner = (data_of(payload) or {}).get("data") or {}
        notes = inner.get("notes") or []
        if not notes:
            break
        for note in notes:
            if not isinstance(note, dict):
                continue
            note_id = str(note.get("id") or "")
            is_video = str(note.get("type") or "") == "video"
            images = _image_urls(note)
            # 类型必须和 kind 用**同一个判定**。以前 id 用 `video/images` 二分、
            # kind 用三分（含 text），纯文字笔记两边永远对不上：
            # 清单里是 `xiaohongshu-images-<id>`，落盘是 `xiaohongshu-text-<id>`，
            # 于是界面上明明抓过还显示"没拿"，会重复抓（踩过）。
            子类型 = "video" if is_video else ("images" if images else "text")
            children.append({
                "id": make_item_id("xiaohongshu", 子类型, note_id),
                # 业务 ID 显式存一份。item_id 里含"类型"这种派生信息，
                # 列表接口和详情接口判定可能不一致，所以判"在不在本地"
                # 一律按 biz_id 匹配，不比 id 字符串。
                "biz_id": note_id,
                "title": _title(note, note_id),
                "kind": 子类型,
                "duration": None,
                "stats": _stats(note),
                "created_at": iso_from_unix(_pick(note, "create_time", "time", default=0)),
                "url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "cover": images[0] if images else "",
                "local": False,
                # 清单里每条都带一份精简的 user（昵称+头像），资料接口挂了时拿它兜底
                "author_hint": {
                    "name": str(_pick(note.get("user") or {}, "nickname", "nick_name", "name", default="") or ""),
                    "avatar": str(_pick(note.get("user") or {}, "images", "image", "avatar", default="") or ""),
                },
            })
            if ctx.limit and len(children) >= ctx.limit:
                break
        item["children"] = children
        ctx.report("正在拉取笔记列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if ctx.limit and len(children) >= ctx.limit:
            break
        if not inner.get("has_more"):
            break
        next_cursor = str(notes[-1].get("cursor") or notes[-1].get("id") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    item["children"] = children
    # 资料接口不稳定时，用清单里第一条笔记自带的 user 兜底填名字和头像，
    # 别让界面上出现一个空白作者卡。
    if not author.get("name"):
        兜底 = next((c.get("author_hint") for c in children if c.get("author_hint")), None)
        if 兜底:
            author["name"] = 兜底.get("name") or ""
            author["avatar"] = 兜底.get("avatar") or ""
            item["title"] = f"{author['name']}的小红书主页" if author["name"] else "小红书主页"
    if not children:
        ctx.warn("没有取到任何笔记（主页可能为空或接口受限）")
    return item
