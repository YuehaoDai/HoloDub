from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from lora_build_split_part_manifest import count_chars, sanitize_text


SCHEMA_VERSION = "holodub.indextts2_lora_weighted_manifest.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a LoRA manifest that keeps real utterances and up-samples long clean rows."
    )
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--splits", default="train,val,test")
    parser.add_argument("--long-target-sec", type=float, default=18.0)
    parser.add_argument("--very-long-target-sec", type=float, default=30.0)
    parser.add_argument("--long-repeat", type=int, default=1)
    parser.add_argument("--very-long-repeat", type=int, default=2)
    parser.add_argument("--max-train-rows", type=int, default=320)
    parser.add_argument("--exclude-sample-ids", default="")
    parser.add_argument("--exclude-sample-ids-file", default="")
    parser.add_argument("--seed", type=int, default=20260624)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_excluded(args: argparse.Namespace) -> set[str]:
    excluded = {item.strip() for item in args.exclude_sample_ids.split(",") if item.strip()}
    if args.exclude_sample_ids_file:
        path = Path(args.exclude_sample_ids_file)
        if path.exists():
            for raw in path.read_text(encoding="utf-8").replace(",", "\n").splitlines():
                item = raw.strip()
                if item:
                    excluded.add(item)
    return excluded


def target_sec(row: dict[str, Any]) -> float:
    raw = row.get("target_ms") or row.get("audio_ms") or 0
    try:
        return float(raw) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def cleaned_row(row: dict[str, Any]) -> dict[str, Any]:
    text, rewrites = sanitize_text(str(row.get("text") or ""))
    out = {**row, "text": text, "chars": count_chars(text), "schema_version": SCHEMA_VERSION}
    if rewrites:
        out["text_cleaning_rewrites"] = rewrites
        out["text_source"] = f"{row.get('text_source') or 'manifest'}+cleaned"
    return out


def repeat_count(row: dict[str, Any], args: argparse.Namespace) -> int:
    seconds = target_sec(row)
    if seconds >= args.very_long_target_sec:
        return max(0, args.very_long_repeat)
    if seconds >= args.long_target_sec:
        return max(0, args.long_repeat)
    return 0


def duplicate_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    sample_id = str(row.get("sample_id") or row.get("segment_id"))
    seconds = target_sec(row)
    return {
        **row,
        "sample_id": f"{sample_id}_wrep{index:02d}",
        "source_sample_id": sample_id,
        "source_segment_id": row.get("segment_id"),
        "tier": f"{row.get('tier') or 'unknown'}_long_weighted",
        "source_tier": row.get("tier"),
        "augmentation": {
            "type": "weighted_duplicate",
            "source_sample_id": sample_id,
            "target_sec": round(seconds, 3),
            "policy": "real_audio_long_upsample",
        },
        "training_policy": {
            "use": "train",
            "risk": "real_audio_duplicate",
        },
    }


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    source_dir = Path(args.manifest_dir)
    output_dir = Path(args.output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"output dir already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    excluded = load_excluded(args)
    rng = random.Random(args.seed)
    split_names = [item.strip() for item in args.splits.split(",") if item.strip()]
    out_by_split: dict[str, list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []

    for split in split_names:
        base_rows: list[dict[str, Any]] = []
        duplicate_rows: list[dict[str, Any]] = []
        for row in read_jsonl(source_dir / f"{split}.jsonl"):
            source_id = str(row.get("sample_id") or row.get("segment_id"))
            if source_id in excluded:
                skipped.append({"sample_id": source_id, "split": split, "reason": "excluded_sample_id"})
                continue
            base = cleaned_row(row)
            base_rows.append(base)
            if split != "train":
                continue
            for index in range(1, repeat_count(base, args) + 1):
                duplicate_rows.append(duplicate_row(base, index))

        if split == "train" and args.max_train_rows > 0:
            room = max(0, args.max_train_rows - len(base_rows))
            rng.shuffle(duplicate_rows)
            duplicate_rows = duplicate_rows[:room]
        out_by_split[split] = base_rows + duplicate_rows

    all_rows: list[dict[str, Any]] = []
    for split in split_names:
        for row in out_by_split.get(split, []):
            row["split"] = split
        all_rows.extend(out_by_split.get(split, []))
        write_jsonl(output_dir / f"{split}.jsonl", out_by_split.get(split, []))
    write_jsonl(output_dir / "all.jsonl", all_rows)

    stats = {
        "accepted": len(all_rows),
        "base_rows": sum(1 for row in all_rows if not row.get("augmentation")),
        "weighted_rows": sum(1 for row in all_rows if row.get("augmentation")),
        "train": len(out_by_split.get("train", [])),
        "val": len(out_by_split.get("val", [])),
        "test": len(out_by_split.get("test", [])),
        "train_long_rows": sum(1 for row in out_by_split.get("train", []) if target_sec(row) >= args.long_target_sec),
        "train_very_long_rows": sum(
            1 for row in out_by_split.get("train", []) if target_sec(row) >= args.very_long_target_sec
        ),
        "total_audio_hours_logical": round(
            sum(int(row.get("audio_ms") or row.get("target_ms") or 0) for row in all_rows) / 3_600_000,
            4,
        ),
        "by_tier": dict(Counter(str(row.get("tier") or "unknown") for row in all_rows)),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_manifest_dir": str(source_dir),
        "stats": stats,
        "files": {"all": "all.jsonl", **{split: f"{split}.jsonl" for split in split_names}},
        "policy": {
            "base": "real accepted utterances only",
            "augmentation": "duplicate real long utterances; no synthetic audio cuts",
            "long_target_sec": args.long_target_sec,
            "very_long_target_sec": args.very_long_target_sec,
            "long_repeat": args.long_repeat,
            "very_long_repeat": args.very_long_repeat,
            "max_train_rows": args.max_train_rows,
            "excluded_sample_ids": sorted(excluded),
        },
        "skipped": skipped,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "README.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"output_dir": str(output_dir), **stats, "skipped": len(skipped)}


def main() -> int:
    print(json.dumps(build_manifest(parse_args()), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
