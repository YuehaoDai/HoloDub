from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build lightweight lora_infer feature rows from episode_segments.json.")
    parser.add_argument("--episode-segments", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--code-token-rate", type=float, default=50.0)
    return parser.parse_args()


def segment_sample_id(row: dict[str, Any]) -> str:
    raw = row.get("sample_id")
    if raw:
        return str(raw)
    segment_id = row.get("segment_id")
    if segment_id not in (None, ""):
        return f"seg{segment_id}"
    return ""


def build_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = json.loads(Path(args.episode_segments).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("episode segments must be a JSON list")
    selected = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sample_id = segment_sample_id(row)
        if selected and sample_id not in selected:
            continue
        target_ms = int(float(row.get("original_duration_ms") or row.get("target_ms") or row.get("tts_duration_ms") or 0))
        text = str(row.get("target_text") or row.get("text") or "").strip()
        if not sample_id or not text or target_ms <= 0:
            continue
        out.append(
            {
                "schema_version": "holodub.lora_infer_lightweight_features.v1",
                "sample_id": sample_id,
                "segment_id": int(row.get("segment_id") or 0),
                "episode_id": int(row.get("episode_id") or 0) if row.get("episode_id") else None,
                "target_ms": target_ms,
                "audio_ms": int(float(row.get("tts_duration_ms") or 0)),
                "code_len": max(64, int(round(target_ms / 1000.0 * float(args.code_token_rate)))),
                "text": text,
                "source_text": str(row.get("source_text") or ""),
            }
        )
        if args.max_samples > 0 and len(out) >= args.max_samples:
            break
    return out


def main() -> int:
    args = parse_args()
    out = build_rows(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in out:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "rows": len(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
