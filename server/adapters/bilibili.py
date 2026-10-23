#!/usr/bin/env python3
"""哔哩哔哩适配器：单条视频 + UP 主主页。

坑（全部是实测踩出来的，不是文档推测）：

- `duration` 单位是**秒**，别的平台大多给毫秒，所以这里绝不能再除 1000。
- **包装层数不统一**：`fetch_one_video` / `fetch_video_playurl` / `fetch_user_profile` /
  `fetch_user_post_videos` 都是双层 `data.data.*`，唯独 `fetch_user_relation_stat` 是单层
  `data.follower`。写一个通用 unwrap 必然有一处取错对象，所以按端点各写各的。
- `fetch_one_video` **不返回任何播放地址**，必须拿 `cid` 再调一次 `fetch_video_playurl`。
- playurl 只给 DASH，`durl` 键是**完全缺失**（不是 null），没有合流好的 mp4：
  视频流和音频流是两个 `.m4s`。转写要读音轨，所以必须 ffmpeg 合流后再落盘，
  否则存下来的是无声视频，转写会直接判定「没有音轨」。
- DASH 的 `baseUrl` 经常落在 `upos-sz-mirrorcosov.bilivideo.com`，本机实测直接连不通
  （curl 25 秒零字节），而 `backupUrl` 的 akamaized 节点秒下；所以两条线路都要当候选逐个试。
- 封面和播放地址都要带 `Referer: https://www.bilibili.com/`，否则 403。
- 三种入参三条取值路径：`fetch_one_video(bv_id)` → `data.data`；
  `fetch_video_detail(aid)` → `data.data.View`（老 web 结构，首字母大写还多包一层）；
  `fetch_one_video_v3(url)` → 直接就是视频对象本身。
- 播放地址是一次性签名链（带 deadline），存进 item.json 只能当溯源，不能当长期可播地址。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..resolver import Resolved
from ..tikhub import TikHubError, data_of, get
from .base import (
    FetchContext,
    as_int,
    empty_item,
    iso_from_unix,
    make_item_id,
    normalized_media_candidates,
)

DETAIL_BV_PATH = "/api/v1/bilibili/web/fetch_one_video"
DETAIL_AID_PATH = "/api/v1/bilibili/web/fetch_video_detail"
DETAIL_URL_PATH = "/api/v1/bilibili/web/fetch_one_video_v3"
PLAYURL_PATH = "/api/v1/bilibili/web/fetch_video_playurl"
PROFILE_PATH = "/api/v1/bilibili/web/fetch_user_profile"
RELATION_PATH = "/api/v1/bilibili/web/fetch_user_relation_stat"
POSTS_PATH = "/api/v1/bilibili/web/fetch_user_post_videos"

REFERER = "https://www.bilibili.com/"
PAGE_SIZE = 20
MAX_PAGES = 12  # 主页翻页上限，防止 --limit 0 时无限翻
STREAM_TIMEOUT = 60  # 单条线路的超时；线路有好几条，不能每条都等 120 秒
# 实测（本机）这个 cosov 节点连不通：能建连但零字节，一直挂到超时；akamaized 节点秒下。
# 每个候选地址都要等「代理 + 直连」两次超时，所以把已知不通的节点排到最后，
# 避免下载一个好端端的视频先白等几分钟。它仍然是候选，只是优先级最低。
SLOW_HOSTS = ("upos-sz-mirrorcosov.bilivideo.com",)


# ---- 取值 ----


def _video_node(payload: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """按入参类型取视频对象——三个端点三种包装，不能共用。"""
    inner = data_of(payload)
    if mode == "aid":
        # data.data.View：老 web 结构，首字母大写
        node = (inner or {}).get("View") if isinstance(inner, dict) else None
    elif mode == "url":
        # v3 直接把视频对象放在 data 上，不再套一层
        node = inner
    else:
        node = (inner or {}).get("data") if isinstance(inner, dict) else None
    return node if isinstance(node, dict) else {}


def _stats(stat: dict[str, Any]) -> dict[str, int]:
    """bilibili 的字段名自成一套：reply=评论、favorite=收藏、view=播放。"""
    stat = stat or {}
    return {
        "like": as_int(stat.get("like")),
        "comment": as_int(stat.get("reply")),
        "share": as_int(stat.get("share")),
        "collect": as_int(stat.get("favorite")),
        "play": as_int(stat.get("view")),
    }


def _author(owner: dict[str, Any], *, followers: int = 0) -> dict[str, Any]:
    """bilibili 没有 handle（@名），用 mid 兜底，别硬造一个。"""
    owner = owner or {}
    mid = str(owner.get("mid") or "")
    return {
        "name": str(owner.get("name") or ""),
        "handle": mid,
        "id": mid,
        "avatar": str(owner.get("face") or ""),
        "url": f"https://space.bilibili.com/{mid}" if mid else "",
        "followers": followers,
    }


def _length_seconds(value: Any) -> float | None:
    """列表接口的时长是 "mm:ss" / "hh:mm:ss" 字符串，不是数字。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)


# ---- 播放地址 ----


def _stream_urls(stream: dict[str, Any]) -> list[str]:
    """一路流的候选地址，按「可用性优先」排序。

    baseUrl 和 backupUrl 都要试（实测有 baseUrl 不通、backupUrl 能下的情况），
    但已知连不通的 CDN 节点排到最后，别让一次下载先白等好几分钟。
    """
    urls = normalized_media_candidates(
        [stream.get("baseUrl"), stream.get("base_url"), *(stream.get("backupUrl") or [])]
    )
    return sorted(urls, key=lambda url: any(host in url for host in SLOW_HOSTS))


def _pick_video_stream(dash: dict[str, Any]) -> dict[str, Any] | None:
    """优先 H.264：ffmpeg 合流和转写引擎对它最稳，HEVC 偶尔解不开音视频同步。"""
    rows = [row for row in (dash.get("video") or []) if isinstance(row, dict)]
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            1 if str(row.get("codecs") or "").startswith("avc1") else 0,
            as_int(row.get("width")) * as_int(row.get("height")),
            as_int(row.get("bandwidth")),
        ),
    )


def _pick_audio_stream(dash: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in (dash.get("audio") or []) if isinstance(row, dict)]
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            1 if str(row.get("codecs") or "").startswith("mp4a") else 0,
            as_int(row.get("bandwidth")),
        ),
    )


def _download_stream(stream: dict[str, Any], target: Path) -> tuple[bool, str]:
    """逐个候选线路下载一路流。返回 (是否成功, 失败摘要)。"""
    from ..tikhub import download as download_url

    urls = _stream_urls(stream)
    if not urls:
        return False, "没有候选地址"
    errors: list[str] = []
    for index, url in enumerate(urls, start=1):
        try:
            download_url(url, target, referer=REFERER, timeout=STREAM_TIMEOUT)
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 换下一条线路，不在这里失败
            errors.append(f"线路{index}:{type(exc).__name__}")
    return False, "，".join(errors[:3])


def _mux(video: Path, audio: Path, target: Path) -> bool:
    """把 DASH 的视频流和音频流合成一个 mp4。`-c copy` 不重编码，快且不掉画质。"""
    from ..media import have

    if not have("ffmpeg"):
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(video), "-i", str(audio), "-c", "copy", str(target)],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


def _assemble_video(dash: dict[str, Any], ctx: FetchContext) -> Path | None:
    """拼一个可播放的本地视频文件。失败返回 None，由调用方记 warning。"""
    video_stream = _pick_video_stream(dash)
    if video_stream is None:
        ctx.warn("播放地址里没有视频流（多为会员专享或已下架）")
        return None

    ctx.report("正在下载视频流", 38, f"{len(_stream_urls(video_stream))} 条线路")
    video_file = ctx.work_dir / "video.m4s"
    ok, detail = _download_stream(video_stream, video_file)
    if not ok:
        ctx.warn(f"视频流下载失败（{detail}）")
        return None

    final = ctx.work_dir / "video.mp4"
    audio_stream = _pick_audio_stream(dash)
    if audio_stream is None:
        # 没有音轨就照实说明，不伪装成"下载成功"
        ctx.warn("播放地址里没有音频流，本地文件是无声视频")
    else:
        ctx.report("正在下载音频流", 44, f"{len(_stream_urls(audio_stream))} 条线路")
        audio_file = ctx.work_dir / "audio.m4s"
        ok, detail = _download_stream(audio_stream, audio_file)
        if not ok:
            ctx.warn(f"音频流下载失败（{detail}），本地文件是无声视频")
        elif _mux(video_file, audio_file, final):
            return final
        else:
            ctx.warn("视频流与音频流合流失败（ffmpeg 不可用或格式不支持），本地文件是无声视频")

    # 合流不成：退回把纯视频流改名保存，至少画面还在
    final.write_bytes(video_file.read_bytes())
    return final


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


# ---- 单条视频 ----


def fetch_video(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """单条 B 站视频：元数据 → 播放地址 → 合流下载 → 转写。"""
    bv_id = resolved.ids.get("bv_id") or ""
    aid = resolved.ids.get("aid") or ""
    if not bv_id and not aid:
        raise TikHubError("链接里没有识别到 bilibili 视频 ID")
    ctx.begin(make_item_id("bilibili", "video", bv_id or aid))  # 占位：拿到 bvid 后会改成正确的 id

    ctx.report("正在调用哔哩哔哩接口", 12, f"{bv_id or 'av' + aid}")
    mode = "bv" if bv_id else "aid"
    if bv_id:
        payload = get(DETAIL_BV_PATH, {"bv_id": bv_id})
        ctx.note_api(DETAIL_BV_PATH)
    else:
        payload = get(DETAIL_AID_PATH, {"aid": aid})
        ctx.note_api(DETAIL_AID_PATH)
    node = _video_node(payload, mode=mode)
    if not node:
        # 主接口拿不到（稿件失效/风控）时退回按完整链接的 v3 接口
        ctx.report("正在调用哔哩哔哩接口", 18, "改用兼容接口重试")
        payload = get(DETAIL_URL_PATH, {"url": resolved.url})
        ctx.note_api(DETAIL_URL_PATH)
        node = _video_node(payload, mode="url")
        ctx.warn("主接口没有返回视频数据，已自动切换兼容接口")
    if not node:
        raise TikHubError("bilibili 没有返回视频数据（稿件可能已删除、被锁定或仅 UP 主可见）")

    final_bv = str(node.get("bvid") or bv_id)
    item_id = ctx.begin(make_item_id("bilibili", "video", final_bv or aid))
    item = empty_item("bilibili", "video", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照
    ctx.save_raw("bilibili_fetch_one_video", payload)

    owner = node.get("owner") or {}
    dimension = node.get("dimension") or {}
    duration = as_int(node.get("duration"))  # 秒，不要除 1000
    item.update({
        "title": str(node.get("title") or "").strip() or f"哔哩哔哩视频 {final_bv}",
        "text": str(node.get("desc") or "").strip(),
        "author": _author(owner),
        "created_at": iso_from_unix(node.get("pubdate") or node.get("ctime")),
        "duration": float(duration) if duration else None,
        "stats": _stats(node.get("stat") or {}),
        "canonical_url": f"https://www.bilibili.com/video/{final_bv}" if final_bv else resolved.url,
        "profile": {
            "aid": str(node.get("aid") or ""),
            "cid": str(node.get("cid") or ""),
            "danmaku": as_int((node.get("stat") or {}).get("danmaku")),
            "coin": as_int((node.get("stat") or {}).get("coin")),
        },
    })
    if not item["text"]:
        ctx.warn("这条视频没有简介（接口返回空 desc）")

    media: list[dict[str, Any]] = []
    cover = str(node.get("pic") or "")
    if cover and ctx.want_media:
        # hdslb 的图要带 Referer，否则会被判定为盗链
        media.append(ctx.save_media(cover, "cover", role="cover", media_type="image", referer=REFERER))
    elif cover:
        media.append({"role": "cover", "type": "image", "path": "", "url": cover,
                      "width": as_int(dimension.get("width")), "height": as_int(dimension.get("height")),
                      "bytes": 0})

    pages = node.get("pages") or []
    if as_int(node.get("videos")) > 1:
        ctx.warn(f"这是 {as_int(node.get('videos'))} 分 P 的稿件，本次只抓了第 1 个分 P")

    # 取播放地址：即使不下载也要拿，item.json 里要留下可溯源的远程地址
    ctx.report("正在拉取播放地址", 18, f"{final_bv} / cid {node.get('cid')}")
    dash: dict[str, Any] = {}
    cid = as_int(node.get("cid")) or as_int((pages[0] if pages else {}).get("cid"))
    if final_bv and cid:
        try:
            play_payload = get(PLAYURL_PATH, {"bv_id": final_bv, "cid": cid})
            ctx.note_api(PLAYURL_PATH)
            ctx.save_raw("bilibili_fetch_video_playurl", play_payload)
            dash = ((data_of(play_payload) or {}).get("data") or {}).get("dash") or {}
        except TikHubError as exc:
            ctx.warn(f"播放地址接口失败（{exc}），视频未下载")
    else:
        ctx.warn("视频缺少 bvid/cid，无法取播放地址")

    video_entry: dict[str, Any] | None = None
    remote_video = _stream_urls(_pick_video_stream(dash) or {})
    if dash and ctx.want_media:
        assembled = _assemble_video(dash, ctx)
        if assembled is not None:
            from .. import store as store_module
            from ..media import probe_duration

            relative = store_module.save_media(ctx.item_id, "video.mp4", assembled.read_bytes())
            stream = _pick_video_stream(dash) or {}
            video_entry = {
                "role": "video", "type": "video", "path": relative, "url": "",
                "width": as_int(stream.get("width")) or as_int(dimension.get("width")),
                "height": as_int(stream.get("height")) or as_int(dimension.get("height")),
                "bytes": assembled.stat().st_size, "duration": probe_duration(assembled),
            }
            media.append(video_entry)
    elif remote_video:
        media.append({"role": "video", "type": "video", "path": "", "url": remote_video[0],
                      "width": as_int(dimension.get("width")), "height": as_int(dimension.get("height")),
                      "bytes": 0})

    item["media"] = media

    if video_entry:
        item["transcript"] = _transcribe_entry(video_entry, ctx)
    else:
        item["transcript"] = {"source": "none", "text": "", "segments": [],
                              "note": "视频未下载到本地，跳过转写"}
    return item


# ---- UP 主主页 ----


def fetch_profile(resolved: Resolved, ctx: FetchContext) -> dict[str, Any]:
    """UP 主主页：资料 + 粉丝数 + 投稿列表。"""
    uid = resolved.ids.get("uid") or ""
    if not uid:
        raise TikHubError("链接里没有识别到 bilibili 用户 UID")
    item_id = ctx.begin(make_item_id("bilibili", "profile", uid))
    item = empty_item("bilibili", "profile", resolved)
    item["id"] = item_id
    ctx.attach(item)  # 挂到上下文：之后每次进度推送都会带上当前内容快照

    ctx.report("正在读取 UP 主资料", 12)
    profile_payload = get(PROFILE_PATH, {"uid": uid})
    ctx.note_api(PROFILE_PATH)
    ctx.save_raw("bilibili_fetch_user_profile", profile_payload)
    user = (data_of(profile_payload) or {}).get("data") or {}
    if not user:
        raise TikHubError("bilibili 没有返回用户资料（UID 可能不正确或账号已注销）")

    official = user.get("official") or {}
    school = user.get("school") or {}
    author = _author({"mid": user.get("mid") or uid, "name": user.get("name"), "face": user.get("face")})
    author["followers"] = _fetch_followers(uid, ctx)

    item.update({
        "title": f"{author['name']}的哔哩哔哩主页" if author["name"] else "哔哩哔哩 UP 主主页",
        "author": author,
        "profile": {
            "signature": str(user.get("sign") or ""),
            "level": as_int(user.get("level")),
            "sex": str(user.get("sex") or ""),
            "birthday": str(user.get("birthday") or ""),
            "school": str(school.get("name") or ""),
            "verified": str(official.get("title") or official.get("desc") or ""),
        },
        # 主页没有"点赞/播放"这种单条指标，把粉丝数放进 play 位与 douyin 适配器保持一致
        "stats": {"like": 0, "comment": 0, "share": 0, "collect": 0, "play": author["followers"]},
    })

    children = _fetch_children(uid, ctx)
    item["children"] = children
    return item


def _fetch_followers(uid: str, ctx: FetchContext) -> int:
    """粉丝数不在 fetch_user_profile 里，要单独问 relation_stat（注意它是单层包装）。"""
    try:
        payload = get(RELATION_PATH, {"uid": uid})
        ctx.note_api(RELATION_PATH)
        ctx.save_raw("bilibili_fetch_user_relation_stat", payload)
        # 单层：粉丝数直接在 data.follower，不在 data.data.follower
        return as_int((data_of(payload) or {}).get("follower"))
    except TikHubError as exc:
        ctx.warn(f"粉丝数没取到（{exc}），主页只展示资料与投稿")
        return 0


def _fetch_children(uid: str, ctx: FetchContext) -> list[dict[str, Any]]:
    """翻页拉投稿列表。ps 是每页条数，总数在 data.data.page.count。"""
    limit = ctx.limit
    ctx.report("正在拉取投稿列表", 30, f"最多 {limit or '全部'} 条")
    children: list[dict[str, Any]] = []
    page = 0
    total = 0
    while page < MAX_PAGES:
        page += 1
        payload = get(POSTS_PATH, {"uid": uid, "pn": page, "ps": PAGE_SIZE})
        ctx.note_api(POSTS_PATH)
        if page == 1:
            ctx.save_raw("bilibili_fetch_user_post_videos", payload)
        data = (data_of(payload) or {}).get("data") or {}
        listing = data.get("list") or {}
        rows = listing.get("vlist") or []
        total = as_int((data.get("page") or {}).get("count"))
        if not rows:
            break
        for video in rows:
            if not isinstance(video, dict):
                continue
            bvid = str(video.get("bvid") or "")
            children.append({
                "id": make_item_id("bilibili", "video", bvid),
                "title": str(video.get("title") or "").strip() or f"投稿 {bvid}",
                "kind": "video",
                "duration": _length_seconds(video.get("length")),
                # 列表接口只有播放/评论/弹幕，点赞收藏转发要逐条进详情才有，这里如实留 0
                "stats": {"like": 0, "comment": as_int(video.get("comment")), "share": 0,
                          "collect": 0, "play": as_int(video.get("play"))},
                "created_at": iso_from_unix(video.get("created")),
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "cover": str(video.get("pic") or ""),
                "local": False,
            })
            if limit and len(children) >= limit:
                break
        ctx.report("正在拉取投稿列表", min(70, 30 + page * 4), f"已取 {len(children)} 条")
        if limit and len(children) >= limit:
            break
        if total and page * PAGE_SIZE >= total:
            break

    if not children:
        ctx.warn("没有取到任何投稿（主页可能为空或接口受限）")
    elif limit and len(children) >= limit and (not total or total > len(children)):
        ctx.warn(f"已达到抓取上限 {limit} 条，UP 主还有更多投稿未拉取")
    return children
