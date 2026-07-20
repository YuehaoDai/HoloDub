from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "holodub.indextts2_lora_merged_manifest.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge one or more LoRA dataset manifest directories.")
    parser.add_argument("--manifest-dir", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-name", default="")
    parser.add_argument("--seed", default="holodub-lora-merged-v1")
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


def split_key(row: dict[str, Any]) -> tuple[int, int, str]:
    tier_rank = 0 if row.get("tier") == "gold" else 1
    return (tier_rank, int(row.get("episode_id") or 0), str(row.get("sample_id") or ""))


def merge(args: argparse.Namespace) -> dict[str, Any]:
    output_root = Path(args.output_dir)
    run_name = args.run_name or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    merged_by_split = {"train": [], "val": [], "test": [], "all": []}
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_dir in args.manifest_dir:
        manifest_dir = Path(raw_dir)
        manifest_path = manifest_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        source_rows = read_jsonl(manifest_dir / "all.jsonl")
        accepted = 0
        for row in source_rows:
            sample_id = str(row.get("sample_id") or "")
            key = f"{row.get('episode_id')}:{sample_id}"
            if not sample_id or key in seen:
                continue
            seen.add(key)
            accepted += 1
            split = str(row.get("split") or "train")
            if split not in {"train", "val", "test"}:
                split = "train"
            merged_by_split[split].append(row)
            merged_by_split["all"].append(row)
        sources.append(
            {
                "manifest_dir": str(manifest_dir),
                "tier": manifest.get("tier"),
                "accepted": accepted,
                "stats": manifest.get("stats") if isinstance(manifest.get("stats"), dict) else {},
            }
        )

    for split in merged_by_split:
        merged_by_split[split].sort(key=split_key)
        write_jsonl(run_dir / f"{split}.jsonl", merged_by_split[split])

    stats = {
        "accepted": len(merged_by_split["all"]),
        "train": len(merged_by_split["train"]),
        "val": len(merged_by_split["val"]),
        "test": len(merged_by_split["test"]),
        "total_audio_hours": round(
            sum(int(row.get("audio_ms") or 0) for row in merged_by_split["all"]) / 3_600_000,
            4,
        ),
        "by_tier": {},
        "by_episode": {},
    }
    for row in merged_by_split["all"]:
        tier = str(row.get("tier") or "unknown")
        episode = str(row.get("episode_id") or "unknown")
        stats["by_tier"][tier] = stats["by_tier"].get(tier, 0) + 1
        stats["by_episode"][episode] = stats["by_episode"].get(episode, 0) + 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "sources": sources,
        "stats": stats,
        "files": {"all": "all.jsonl", "train": "train.jsonl", "val": "val.jsonl", "test": "test.jsonl"},
        "policy": {
            "gold_source": "human-approved or lecture-7 regression-accepted low-fit audio",
            "silver_source": "lecture-local low-fit high-ASR candidate audio; not promotable without human validation",
            "excluded": "high fit tempo, low ASR, high drift, low judge, too long, or too many chars",
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(run_dir / "manifest.json", run_dir / "README.json")
    return {"run_dir": str(run_dir), **stats}


def main() -> int:
    result = merge(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
