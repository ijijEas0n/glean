#!/usr/bin/env python3
"""Instagram 适配器：单条帖子（图片 / 视频 / 图集）+ 博主主页。

字段路径全部来自实测响应（.fixtures/instagram__*.json，图集 / 分页 / v1 兜底另做活体复核）。

坑（实测，不是文档推测）：
- 帖子详情在 `data.items[0]`；`media_type` 1=图片，2=视频，8=图集。
  图集的原图**不在** `edge_sidecar_to_children`（那是另一套 GraphQL 字段名），
  这套响应里是 `carousel_media[]`，每个子项自己带 media_type / image_versions2 / video_versions
  —— 图集可以图文视频混排（实测 carousel 第一格就是视频）。
- `taken_at` 是 unix 秒；`video_duration` 是**秒**（实测 10.47），不要再除 1000。
- `image_versions2.candidates` 是同一张图的多档分辨率、从大到小排（实测 1080x1920 → 360x640），
  取 candidates[0] 才是原图；candidates[1] 不是第二张图。
- 主页作品列表（v3 get_user_posts）返回的是精简节点：`taken_at` 实测恒为 null，
  所以主页清单里的 created_at 只能是空串，必须如实说明，不能瞎填时间。
- 列表翻页参数实测是 `after`（值取上一页的 `next_max_id`）；`max_id` / `next_max_id` 都无效。
- v1 的 fetch_user_posts **必须**给数字 user_id（给 username 直接 422），所以它只能当兜底，
  并且要先用主页资料里的 pk 换出数字 id。
- v1 fetch_user_info_by_username 偶发返回 `{status, errorMessage}` 而不是 user，要当失败处理。
"""

from __future__ import annotations

from typing import Any

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
    short_id,
)

POST_PATH = "/api/v1/instagram/v3/get_post_info_by_code"
PROFILE_PATH = "/api/v1/instagram/v3/get_user_profile"
POSTS_PATH = "/api/v1/instagram/v3/get_user_posts"
PROFILE_FALLBACK_PATH = "/api/v1/instagram/v1/fetch_user_info_by_username"
POSTS_FALLBACK_PATH = "/api/v1/instagram/v1/fetch_user_posts"

REFERER = "https://www.instagram.com/"
PAGE_SIZE = 20
MAX_PAGES = 12  # 主页翻页上限，防止 --limit 0 时无限翻
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_CAROUSEL = 8


def _caption_text(post: dict[str, Any]) -> str:
    """caption 在详情接口里是对象，在 v1 列表里也是对象；取不到就是空串。"""
    caption = post.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "").strip()
    if isinstance(caption, str):
        return caption.strip()
    return ""


def _title_from_caption(caption: str, shortcode: str) -> str:
    """Instagram 没有标题字段：用正文第一行当标题，没有正文才用短码兜底。"""
    text = (caption or "").strip()
    if not text:
        return f"Instagram 帖子 {shortcode}"
    first_line = text.split("\n")[0].strip()
    return (first_line or text)[:80]


def _owner(post: dict[str, Any]) -> dict[str, Any]:
    """作者对象实测有两个同构键：user 与 owner，谁在就用谁。"""
    for key in ("user", "owner"):
        value = post.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _author(user: dict[str, Any]) -> dict[str, Any]:
    user = user or {}
    username = str(user.get("username") or "")
    return {
        "name": str(user.get("full_name") or username),
        "handle": f"@{username}" if username else "",
        "id": str(user.get("pk") or user.get("id") or ""),
        "avatar": first_url(user.get("hd_profile_pic_url_info"), user.get("profile_pic_url_hd"),
                            user.get("profile_pic_url")),
        "url": f"https://www.instagram.com/{username}/" if username else "",
        "followers": as_int(user.get("follower_count")),
    }


def _image_candidates(post: dict[str, Any]) -> list[dict[str, Any]]:
    """image_versions2.candidates 是同一张图的多档分辨率，按宽度从大到小排。"""
    versions = post.get("image_versions2") or {}
    rows = [c for c in (versions.get("candidates") or [])
            if isinstance(c, dict) and str(c.get("url") or "").startswith("http")]
    return sorted(rows, key=lambda c: as_int(c.get("width")), reverse=True)


def _image_url(post: dict[str, Any]) -> str:
    rows = _image_candidates(post)
    return str(rows[0].get("url") or "") if rows else ""


def _video_versions(post: dict[str, Any]) -> list[dict[str, Any]]:
    """视频多档码率：按分辨率从大到小排，取第一个当主播放地址。"""
    rows = [v for v in (post.get("video_versions") or [])
            if isinstance(v, dict) and str(v.get("url") or "").startswith("http")]
    return sorted(rows, key=lambda v: as_int(v.get("width")) * as_int(v.get("height")), reverse=True)


def _is_video(post: dict[str, Any]) -> bool:
    return (as_int(post.get("media_type")) == MEDIA_TYPE_VIDEO
            or bool(post.get("is_video"))
            or bool(_video_versions(post)))


def _carousel(post: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (post.get("carousel_media") or []) if isinstance(c, dict) and c]


def _stats(post: dict[str, Any]) -> dict[str, int]:
    post = post or {}
    return {
        "like": as_int(post.get("like_count")),
        "comment": as_int(post.get("comment_count")),
        # share_count 实测这个键存在但常常是 null，取不到就留 0，不拿转发数顶替
        "share": as_int(post.get("share_count")),
        "collect": 0,  # Instagram 没有收藏数，留 0 不猜
        "play": as_int(post.get("play_count") or post.get("ig_play_count") or post.get("view_count")),
    }


def _post_url(shortcode: str, kind: str) -> str:
    """视频帖用 /reel/，其余用 /p/；两种都是 Instagram 真实可打开的短码地址。"""
    if not shortcode:
        return ""
    return f"https://www.instagram.com/{'reel' if kind == 'video' else 'p'}/{shortcode}/"


def _unwrap_user(payload: dict[str, Any]) -> dict[str, Any]:
    """v3 的 user 直接挂在 data 上；v1 藏在 data.data.user（实测两套都遇到过）。"""
    data = data_of(payload) or {}
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("user"), dict):
        return inner["user"]
    if isinstance(data.get("user"), dict):
        return data["user"]
    return data if (data.get("username") or data.get("pk")) else {}


def _failure_hint(payload: dict[str, Any]) -> str:
    data = data_of(payload) or {}
    for key in ("errorMessage", "message", "status"):
        value = data.get(key)
        if value:
            return str(value)[:120]
    return "没有 user 数据"


def _download_video(candidates: list[str], ctx: FetchContext, stem: str,
                    post: dict[str, Any]) -> dict[str, Any] | None:
    """下载视频到 media/。全部地址失败返回 None（不抛，让上层记 warning）。"""
    if not candidates:
        return None
    from ..tikhub import download as download_url

    target = ctx.work_dir / f"{stem}.mp4"
    errors: list[str] = []
    for index, url in enumerate(candidates, start=1):
        try:
            size = download_url(url, target, referer=REFERER)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"线路{index}:{type(exc).__name__}")
            continue
        from .. import store as store_module

        relative = store_module.save_media(ctx.item_id, f"{stem}.mp4", target.read_bytes())
        from ..media import probe_duration

        return {
            "role": "video", "type": "video", "path": relative, "url": "",
            "width": as_int(post.get("original_width")), "height": as_int(post.get("original_height")),
            "bytes": size, "duration": probe_duration(target),
        }
    ctx.warn(f"视频下载失败（{len(candidates)} 条线路都不可用：{'，'.join(errors[:3])}）")
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


def _image_entries(post: dict[str, Any], ctx: FetchContext, stem: str = "001") -> list[dict[str, Any]]:
    url = _image_url(post)
    if not url:
        return []
    if ctx.want_media:
        return [ctx.save_media(url, stem, role="content", media_type="image", referer=REFERER)]
    return [{"role": "content", "type": "image", "path": "", "url": url,
             "width": as_int(post.get("original_width")), "height": as_int(post.get("original_height")),
             "bytes": 0}]


def _carousel_entries(post: dict[str, Any], ctx: FetchContext) -> tuple[list[dict[str, Any]], int, bool]:
    """图集：封面取第一格（视频格也有 image_versions2 当首帧），再逐格落盘。

    返回 (媒体条目, 拿不到地址的格数, 是否含视频格)。
    """
    entries: list[dict[str, Any]] = []
    children = _carousel(post)
    cover = _image_url(children[0]) if children else ""
    if cover:
        if ctx.want_media:
            entries.append(ctx.save_media(cover, "cover", role="cover", media_type="image", referer=REFERER))
        else:
            entries.append({"role": "cover", "type": "image", "path": "", "url": cover,
                            "width": 0, "height": 0, "bytes": 0})

    missing = 0
    has_video = False
    for index, child in enumerate(children, start=1):
        stem = f"{index:03d}"
        if _is_video(child):
            has_video = True
            candidates = [str(v.get("url")) for v in _video_versions(child)]
            entry = _download_video(candidates, ctx, stem, child) if ctx.want_media else None
            if entry:
                entries.append(entry)
            elif candidates:
                entries.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                                "width": as_int(child.get("original_width")),
                                "height": as_int(child.get("original_height")), "bytes": 0})
            else:
                missing += 1
            continue
        url = _image_url(child)
        if not url:
            missing += 1
            continue
        if ctx.want_media:
            entries.append(ctx.save_media(url, stem, role="content", media_type="image", referer=REFERER))
        else:
            entries.append({"role": "content", "type": "image", "path": "", "url": url,
                            "width": as_int(child.get("original_width")),
                            "height": as_int(child.get("original_height")), "bytes": 0})
    return entries, missing, has_video


def fetch_post(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条 Instagram 帖子（图片 / 视频 / 图集）。"""
    shortcode = str(resolved.ids.get("shortcode") or "")
    if not shortcode:
        raise TikHubError("链接里没有识别到 Instagram 帖子短码")
    ctx.begin(make_item_id("instagram", "unknown", shortcode))  # 占位：定类型后再改

    ctx.report("正在调用 Instagram 接口", 12, f"帖子 {shortcode}")
    payload = get(POST_PATH, {"code": shortcode})
    ctx.note_api(POST_PATH)
    rows = (data_of(payload) or {}).get("items") or []
    post = rows[0] if rows and isinstance(rows[0], dict) else {}
    if not post:
        raise TikHubError(f"Instagram 没有返回帖子数据（{shortcode} 可能已删除、私密或需要登录）")

    carousel = _carousel(post)
    if as_int(post.get("media_type")) == MEDIA_TYPE_CAROUSEL or carousel:
        kind = "images"
    elif _is_video(post):
        kind = "video"
    else:
        kind = "images" if _image_url(post) else "text"

    # 类型定下来之后再落 item_id：raw/ 与 media/ 都要进同一个目录
    item_id = ctx.begin(make_item_id("instagram", kind, shortcode))
    item = empty_item("instagram", kind, resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    ctx.save_raw("instagram_get_post_info_by_code", payload)

    caption = _caption_text(post)
    duration = as_float(post.get("video_duration"))
    item.update({
        "title": _title_from_caption(caption, shortcode),
        "text": caption,
        "author": _author(_owner(post)),
        "created_at": iso_from_unix(post.get("taken_at")),
        "duration": round(duration, 3) if (kind == "video" and duration) else None,
        "stats": _stats(post),
        "canonical_url": _post_url(shortcode, kind),
    })

    ctx.report("正在下载媒体", 35)
    media: list[dict[str, Any]] = []
    video_entry: dict[str, Any] | None = None
    if carousel:
        entries, missing, has_video = _carousel_entries(post, ctx)
        media.extend(entries)
        if missing:
            ctx.warn(f"图集里有 {missing} 格没有取到媒体地址，已跳过")
        if has_video and ctx.want_transcribe:
            ctx.warn("这条是图文视频混排的图集，未对图集内的视频做转写")
    elif kind == "video":
        cover = _image_url(post)
        if cover:
            if ctx.want_media:
                media.append(ctx.save_media(cover, "cover", role="cover", media_type="image", referer=REFERER))
            else:
                media.append({"role": "cover", "type": "image", "path": "", "url": cover,
                              "width": as_int(post.get("original_width")),
                              "height": as_int(post.get("original_height")), "bytes": 0})
        candidates = [str(v.get("url")) for v in _video_versions(post)]
        if ctx.want_media:
            video_entry = _download_video(candidates, ctx, "video", post)
        if video_entry:
            media.append(video_entry)
        elif candidates:  # 下载失败也把远程地址留在 item 里，别让链接白白丢掉
            media.append({"role": "video", "type": "video", "path": "", "url": candidates[0],
                          "width": as_int(post.get("original_width")),
                          "height": as_int(post.get("original_height")), "bytes": 0})
        else:
            ctx.warn("这条帖子是视频，但接口没有给出可播放地址")
    else:
        media.extend(_image_entries(post, ctx))

    item["media"] = media

    if kind == "video" and video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
        ctx.publish()  # 字幕是 report 之后才填的，手动推一次让前端立刻能看到
    elif kind == "video":
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}

    if not media:
        ctx.warn("这条帖子没有取到任何媒体（可能是纯文字帖或接口降级）")
    return item


def _fetch_profile(username: str, ctx: FetchContext) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """博主资料：v3 为主，v1 兜底（v1 偶发只回 status/errorMessage）。"""
    errors: list[str] = []
    for path in (PROFILE_PATH, PROFILE_FALLBACK_PATH):
        try:
            payload = get(path, {"username": username})
        except TikHubError as exc:
            errors.append(f"{path.rsplit('/', 1)[-1]}：{exc}")
            continue
        ctx.note_api(path)
        user = _unwrap_user(payload)
        if user:
            return user, payload
        errors.append(f"{path.rsplit('/', 1)[-1]}：{_failure_hint(payload)}")
    ctx.warn("Instagram 博主资料没取到（" + "；".join(errors) + "），下面只展示帖子清单")
    return {}, None


def _child_from_v3(row: dict[str, Any]) -> dict[str, Any]:
    """v3 列表节点（实测字段）：taken_at 恒为 null，所以 created_at 只能是空串。"""
    code = str(row.get("code") or row.get("shortcode") or "")
    is_video = bool(row.get("is_video")) or as_int(row.get("media_type")) == MEDIA_TYPE_VIDEO
    kind = "video" if is_video else "images"
    caption = str(row.get("caption_text") or "").strip()
    return {
        "id": make_item_id("instagram", kind, code),
        "title": _title_from_caption(caption, code),
        "kind": kind,
        "duration": None,  # 列表节点没有时长字段，不猜
        "stats": _stats(row),
        "created_at": iso_from_unix(row.get("taken_at")),
        "url": _post_url(code, kind),
        "cover": str(row.get("thumbnail_url") or ""),
        "local": False,
    }


def _child_from_v1(row: dict[str, Any]) -> dict[str, Any]:
    """v1 列表条目是完整对象：有时长、有发布时间，但没有 is_video 标记。"""
    code = str(row.get("code") or "")
    carousel = _carousel(row)
    if as_int(row.get("media_type")) == MEDIA_TYPE_CAROUSEL or carousel:
        kind = "images"
    elif _is_video(row):
        kind = "video"
    else:
        kind = "images"
    caption = _caption_text(row)
    duration = as_float(row.get("video_duration"))
    cover = _image_url(row) or (_image_url(carousel[0]) if carousel else "")
    return {
        "id": make_item_id("instagram", kind, code),
        "title": _title_from_caption(caption, code),
        "kind": kind,
        "duration": round(duration, 3) if (kind == "video" and duration) else None,
        "stats": _stats(row),
        "created_at": iso_from_unix(row.get("taken_at")),
        "url": _post_url(code, kind),
        "cover": cover,
        "local": False,
    }


def _fetch_children(username: str, user_id: str, ctx: FetchContext) -> tuple[list[dict[str, Any]], bool]:
    """拉作品清单。返回 (children, 列表接口是否给过发布时间)。"""
    limit = ctx.limit
    children: list[dict[str, Any]] = []
    has_time = False

    ctx.report("正在拉取帖子列表", 30, f"最多 {limit or '全部'} 条")
    page = 0
    cursor = ""
    try:
        while page < MAX_PAGES:
            page += 1
            query: dict[str, Any] = {"username": username, "first": PAGE_SIZE}
            if cursor:
                # 实测：这个接口的翻页参数是 after，值取上一页的 next_max_id
                query["after"] = cursor
            payload = get(POSTS_PATH, query)
            ctx.note_api(POSTS_PATH)
            if page == 1:
                ctx.save_raw("instagram_get_user_posts", payload)
            data = data_of(payload) or {}
            rows = [r for r in (data.get("items") or []) if isinstance(r, dict)]
            if not rows:
                break
            for row in rows:
                child = _child_from_v3(row)
                if child["created_at"]:
                    has_time = True
                children.append(child)
                if limit and len(children) >= limit:
                    break
            ctx.report("正在拉取帖子列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
            if limit and len(children) >= limit:
                break
            next_cursor = str(data.get("next_max_id") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    except TikHubError as exc:
        if children:
            ctx.warn(f"帖子列表翻页中断，只取到 {len(children)} 条（{exc}）")

    if not children:
        if not user_id:
            ctx.warn("v3 帖子列表没有返回数据，而兜底接口需要数字 user_id，这次没拿到，清单只能为空")
            return [], has_time
        ctx.warn("v3 帖子列表没有返回数据，改用 v1 接口（需要数字 user_id）重试")
        try:
            payload = get(POSTS_FALLBACK_PATH, {"user_id": user_id})
            ctx.note_api(POSTS_FALLBACK_PATH)
            ctx.save_raw("instagram_fetch_user_posts", payload)
            data = data_of(payload) or {}
            for row in data.get("items") or []:
                if not isinstance(row, dict):
                    continue
                child = _child_from_v1(row)
                if child["created_at"]:
                    has_time = True
                children.append(child)
                if limit and len(children) >= limit:
                    break
        except TikHubError as exc:
            ctx.warn(f"v1 兜底接口也没取到帖子列表（{exc}）")

    return children, has_time


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """Instagram 博主主页：资料 + 帖子清单。"""
    username = str(resolved.ids.get("username") or "").lstrip("@")
    if not username:
        raise TikHubError("链接里没有识别到 Instagram 用户名")

    ctx.report("正在读取博主资料", 10)
    user, profile_payload = _fetch_profile(username, ctx)

    item_id = ctx.begin(make_item_id("instagram", "profile", username))
    item = empty_item("instagram", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    if profile_payload is not None:
        ctx.save_raw("instagram_get_user_profile", profile_payload)

    author = _author(user)
    if not author["handle"]:  # 资料没取到时，至少把 URL 里的用户名如实带上
        author["handle"] = f"@{username}"
    if not author["url"]:
        author["url"] = f"https://www.instagram.com/{username}/"

    item.update({
        "title": f"{author['name']}的 Instagram 主页" if author["name"] else "Instagram 博主主页",
        "author": author,
        "profile": {
            "signature": str(user.get("biography") or ""),
            "media_count": as_int(user.get("media_count")),
            "following_count": as_int(user.get("following_count")),
            "verified": "官方认证" if user.get("is_verified") else "",
            "private": bool(user.get("is_private")),
            "category": str(user.get("category") or ""),
            "external_url": str(user.get("external_url") or ""),
            "user_id": str(user.get("pk") or user.get("id") or ""),
        },
    })

    user_id = str(user.get("pk") or user.get("id") or "")
    children, has_time = _fetch_children(username, user_id, ctx)
    item["children"] = children
    if not children:
        ctx.warn("没有取到任何帖子（主页可能为空、私密或接口受限）")
    else:
        if not has_time:
            # 实测 v3 列表节点的 taken_at 恒为 null：不瞎填时间，只说明白
            ctx.warn("Instagram 列表接口没有返回发布时间，清单里的时间为空，可点进单条帖子查看")
        if ctx.limit and len(children) >= ctx.limit:
            ctx.warn(f"已达到抓取上限 {ctx.limit} 条，博主还有更多帖子未拉取")
    return item
