#!/usr/bin/env python3
"""命令行试抓：直接跑一次适配器，打印结果，不落盘。

用法：
    python3 tools/try_fetch.py <链接> [--limit 5] [--no-media] [--transcribe]
    python3 tools/try_fetch.py <链接> --dry    # 只解析，不抓取
    python3 tools/try_fetch.py <链接> --json   # 打印完整 item.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.adapters import get_adapter, resolve_target  # noqa: E402
from server.adapters.base import FetchContext  # noqa: E402
from server.config import tikhub_api_key  # noqa: E402
from server.resolver import resolve  # noqa: E402
from server.tikhub import detect_proxy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="试抓一个链接")
    parser.add_argument("url")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--no-media", action="store_true", help="不下载媒体")
    parser.add_argument("--transcribe", action="store_true", help="开启转写")
    parser.add_argument("--dry", action="store_true", help="只识别链接")
    parser.add_argument("--json", action="store_true", help="打印完整 item")
    parser.add_argument("--keep", default="", help="把工作目录保留到指定路径，便于看下载了什么")
    args = parser.parse_args()

    if not tikhub_api_key():
        print("✗ 没有 TikHub Key", file=sys.stderr)
        return 2

    resolved = resolve(args.url)
    module_name, function_name, is_fallback = resolve_target(resolved)
    print(f"链接识别：{resolved.platform_label} · {resolved.kind_label}")
    print(f"  展开后：{resolved.url}")
    print(f"  ID    ：{resolved.ids}")
    print(f"  适配器：{module_name}.{function_name}" + ("（兜底）" if is_fallback else ""))
    if resolved.hint:
        print(f"  说明  ：{resolved.hint}")
    if args.dry:
        return 0

    state = detect_proxy()
    print(f"网络  ：{state.get('message')} ok={state.get('ok')}")

    adapter = get_adapter(resolved)
    work_dir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="glean-try-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"工作目录：{work_dir}")

    def progress(label: str, percent: int, detail: str = "") -> None:
        print(f"  [{percent:3d}%] {label} {detail}".rstrip())

    ctx = FetchContext(
        options={
            "download_media": not args.no_media,
            "transcribe": args.transcribe,
            "transcribe_engine": "volcengine",
            "limit": args.limit,
            "expand_children": False,
        },
        work_dir=work_dir,
        progress=progress,
    )
    # 不落盘：raw/media 都留在内存里
    ctx.raw_saver = lambda endpoint, payload: ""
    ctx.media_saver = lambda url, stem, role="content", media_type="image", referer="": {
        "role": role, "type": media_type, "path": "", "url": url,
        "width": 0, "height": 0, "bytes": 0,
    }

    item = adapter(resolved, ctx)
    item["source_api"] = ctx.source_api
    item["warnings"] = ctx.warnings

    if args.json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0

    print("\n--- 抓取结果 ---")
    print("id      :", item.get("id"))
    print("kind    :", item.get("kind"))
    print("title   :", str(item.get("title"))[:90])
    print("author  :", (item.get("author") or {}).get("name"), "|", (item.get("author") or {}).get("handle"))
    print("created :", item.get("created_at"))
    print("duration:", item.get("duration"))
    print("stats   :", item.get("stats"))
    text = str(item.get("text") or "")
    print("text    :", (text[:160] + "…") if len(text) > 160 else text)
    media = item.get("media") or []
    print(f"media   : {len(media)} 条")
    for entry in media[:6]:
        print(f"   - {entry.get('role')}/{entry.get('type')}  {str(entry.get('url'))[:80]}")
    children = item.get("children") or []
    if children:
        print(f"children: {len(children)} 条")
        for child in children[:5]:
            print(f"   - [{child.get('kind')}] {str(child.get('title'))[:60]} | {child.get('url')}")
    transcript = item.get("transcript") or {}
    print("transcript source:", transcript.get("source"), "| segments:",
          len(transcript.get("segments") or []))
    for segment in (transcript.get("segments") or [])[:3]:
        print(f"   [{segment.get('start')}] {str(segment.get('text'))[:60]}")
    if item.get("profile"):
        print("profile :", json.dumps(item["profile"], ensure_ascii=False)[:200])
    if item.get("warnings"):
        print("warnings:")
        for warning in item["warnings"]:
            print("   ⚠", warning)
    print("source_api:", item.get("source_api"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
