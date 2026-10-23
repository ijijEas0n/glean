#!/usr/bin/env python3
"""Local faster-whisper transcription used by the Vault ingestion wrapper."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


def srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe audio with local faster-whisper")
    parser.add_argument("audio")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--model", default="small")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        args.audio,
        language=args.language,
        vad_filter=True,
        beam_size=5,
    )

    rendered = []
    for index, segment in enumerate(segments, start=1):
        text = segment.text.strip()
        if not text:
            continue
        rendered.append((index, segment.start, segment.end, text))

    srt = "\n\n".join(
        f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}"
        for index, start, end, text in rendered
    )
    (output / "subtitle.srt").write_text(srt + "\n", encoding="utf-8")
    (output / "text.txt").write_text("\n".join(item[3] for item in rendered) + "\n", encoding="utf-8")
    print(json.dumps({
        "segment_count": len(rendered),
        "language": info.language,
        "language_probability": info.language_probability,
        "model": args.model,
        "srt_path": str(output / "subtitle.srt"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
