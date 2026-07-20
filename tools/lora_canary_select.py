from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any


ASCII_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{1,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a balanced LoRA evaluation canary from feature JSONL files.")
    parser.add_argument("--features-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--splits", default="test,val,train")
    parser.add_argument("--seed", type=int, default=20260617)
    parser.add_argument("--short-sec", type=float, default=10.0)
    parser.add_argument("--long-sec", type=float, default=20.0)
    parser.add_argument("--short-share", type=float, default=0.30)
    parser.add_argument("--medium-share", type=float, default=0.35)
    parser.add_argument("--long-share", type=float, default=0.35)
    parser.add_argument("--prefer-high-drift", action="store_true", default=True)
    parser.add_argument("--sample-ids-output", default="")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def target_sec(row: dict[str, Any]) -> float:
    target_ms = float(row.get("target_ms") or 0)
    if target_ms <= 0:
        target_ms = float(row.get("audio_ms") or 0)
    return target_ms / 1000.0


def bucket(row: dict[str, Any], short_sec: float, long_sec: float) -> str:
    sec = target_sec(row)
    if sec < short_sec:
        return "short"
    if sec < long_sec:
        return "medium"
    return "long"


def has_code_switch(row: dict[str, Any]) -> bool:
    return bool(ASCII_TERM_RE.search(str(row.get("source_text") or "") + " " + str(row.get("text") or "")))


def risk_score(row: dict[str, Any]) -> float:
    drift = abs(float(row.get("drift_ms") or 0.0))
    text_len = float(row.get("text_len") or 0.0)
    code_len = float(row.get("code_len") or 0.0)
    code_switch_bonus = 500.0 if has_code_switch(row) else 0.0
    return drift + 0.8 * text_len + 0.15 * code_len + code_switch_bonus


def quotas(count: int, short_share: float, medium_share: float, long_share: float) -> dict[str, int]:
    raw = {
        "short": count * short_share,
        "medium": count * medium_share,
        "long": count * long_share,
    }
    out = {key: int(value) for key, value in raw.items()}
    while sum(out.values()) < count:
        key = max(raw, key=lambda item: raw[item] - out[item])
        out[key] += 1
    while sum(out.values()) > count:
        key = max(out, key=lambda item: out[item])
        out[key] -= 1
    return out


def split_priority(split: str) -> int:
    return {"test": 0, "val": 1, "train": 2}.get(split, 9)


def select_bucket(rows: list[dict[str, Any]], want: int, rng: random.Random) -> list[dict[str, Any]]:
    if want <= 0 or not rows:
        return []
    shuffled = list(rows)
    rng.shuffle(shuffled)
    shuffled.sort(key=lambda row: (split_priority(str(row.get("split") or "")), -risk_score(row)))
    return shuffled[:want]


def rebalance(
    selected: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    count: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    if len(selected) >= count:
        return selected[:count]
    selected_ids = {row["sample_id"] for row in selected}
    candidates = [row for row in remaining if row["sample_id"] not in selected_ids]
    rng.shuffle(candidates)
    candidates.sort(key=lambda row: (split_priority(str(row.get("split") or "")), -risk_score(row)))
    return selected + candidates[: count - len(selected)]


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    features_dir = Path(args.features_dir)
    split_names = [item.strip() for item in args.splits.split(",") if item.strip()]

    rows: list[dict[str, Any]] = []
    for split in split_names:
        for row in read_jsonl(features_dir / f"{split}.features.jsonl"):
            rows.append(
                {
                    **row,
                    "split": row.get("split") or split,
                    "canary_bucket": bucket(row, args.short_sec, args.long_sec),
                    "canary_target_sec": target_sec(row),
                    "canary_has_code_switch": has_code_switch(row),
                    "canary_risk_score": risk_score(row),
                }
            )
    if not rows:
        raise ValueError(f"no feature rows found in {features_dir}")

    by_bucket: dict[str, list[dict[str, Any]]] = {"short": [], "medium": [], "long": []}
    for row in rows:
        by_bucket[row["canary_bucket"]].append(row)

    bucket_quotas = quotas(args.count, args.short_share, args.medium_share, args.long_share)
    selected: list[dict[str, Any]] = []
    for name, want in bucket_quotas.items():
        selected.extend(select_bucket(by_bucket[name], want, rng))
    selected = rebalance(selected, rows, args.count, rng)
    selected.sort(key=lambda row: (row["canary_bucket"], split_priority(str(row.get("split") or "")), row["sample_id"]))

    output = Path(args.output)
    write_jsonl(output, selected)
    ids = ",".join(str(row["sample_id"]) for row in selected)
    if args.sample_ids_output:
        Path(args.sample_ids_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.sample_ids_output).write_text(ids + "\n", encoding="utf-8")

    summary = {
        "output": str(output),
        "count": len(selected),
        "sample_ids": ids,
        "by_bucket": {key: sum(1 for row in selected if row["canary_bucket"] == key) for key in by_bucket},
        "by_split": {split: sum(1 for row in selected if row.get("split") == split) for split in split_names},
        "code_switch_count": sum(1 for row in selected if row["canary_has_code_switch"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
