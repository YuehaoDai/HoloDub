from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "holodub.indextts2_lora_features_merged.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge preprocessed IndexTTS2 LoRA feature JSONL directories."
    )
    parser.add_argument("--source-dir", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--splits", default="train,val,test")
    parser.add_argument(
        "--duplicate-policy",
        choices=("keep-first", "keep-last", "error"),
        default="keep-last",
    )
    parser.add_argument("--description", default="")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sample_key(row: dict[str, Any]) -> str:
    sample_id = str(row.get("sample_id") or "").strip()
    if sample_id:
        return sample_id
    return f"segment:{row.get('segment_id')}:{row.get('split')}"


def merge_split(
    split: str,
    source_dirs: list[Path],
    duplicate_policy: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    duplicates: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []

    for source_index, source_dir in enumerate(source_dirs):
        path = source_dir / f"{split}.features.jsonl"
        rows = read_jsonl(path)
        source_summaries.append(
            {
                "source_dir": str(source_dir),
                "split": split,
                "path": str(path),
                "rows": len(rows),
            }
        )
        for row in rows:
            row = dict(row)
            row["split"] = split
            key = sample_key(row)
            if key in rows_by_key:
                duplicate = {
                    "sample_id": key,
                    "split": split,
                    "kept_policy": duplicate_policy,
                    "previous_source": rows_by_key[key].get("_merge_source_dir"),
                    "new_source": str(source_dir),
                }
                duplicates.append(duplicate)
                if duplicate_policy == "error":
                    raise ValueError(f"duplicate feature sample {key} in split {split}: {duplicate}")
                if duplicate_policy == "keep-first":
                    continue
            else:
                order.append(key)
            row["_merge_source_dir"] = str(source_dir)
            row["_merge_source_index"] = source_index
            rows_by_key[key] = row

    merged = [rows_by_key[key] for key in order if key in rows_by_key]
    for row in merged:
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.pop("_merge_source_index", None)
    return merged, duplicates, source_summaries


def copy_shared(source_dirs: list[Path], output_dir: Path) -> list[str]:
    copied: list[str] = []
    for source_dir in source_dirs:
        shared = source_dir / "shared"
        if not shared.exists():
            continue
        target = output_dir / "shared"
        target.mkdir(parents=True, exist_ok=True)
        for item in shared.iterdir():
            if item.is_file():
                dest = target / item.name
                if not dest.exists():
                    shutil.copyfile(item, dest)
                    copied.append(str(dest))
        if copied:
            break
    return copied


def main() -> int:
    args = parse_args()
    source_dirs = [Path(raw) for raw in args.source_dir]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    splits = [item.strip() for item in args.splits.split(",") if item.strip()]

    all_duplicates: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    split_stats: dict[str, dict[str, Any]] = {}
    tier_counts: Counter[str] = Counter()

    for split in splits:
        rows, duplicates, summaries = merge_split(split, source_dirs, args.duplicate_policy)
        all_duplicates.extend(duplicates)
        source_summaries.extend(summaries)
        write_jsonl(output_dir / f"{split}.features.jsonl", rows)
        split_stats[split] = {"rows": len(rows), "duplicates": len(duplicates)}
        tier_counts.update(str(row.get("tier") or "unknown") for row in rows)

    copied_shared = copy_shared(source_dirs, output_dir)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "description": args.description,
        "source_dirs": [str(path) for path in source_dirs],
        "duplicate_policy": args.duplicate_policy,
        "splits": split_stats,
        "source_summaries": source_summaries,
        "duplicates": all_duplicates,
        "copied_shared": copied_shared,
        "by_tier": dict(sorted(tier_counts.items())),
        "policy": {
            "purpose": "append promoted repair features to the validated feasible feature cache",
            "failed_shadow_audio_is_not_gold": True,
            "requires_generated_audio_holdout_before_adapter_use": True,
        },
    }
    (output_dir / "features_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), **split_stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
