#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "holodub.mit6824_lora_training_pack_audit.v1"
DEFAULT_HOLDOUT_IDS = [
    "seg15916",
    "seg15964",
    "seg15965",
    "seg15976",
    "seg15986",
    "seg15990",
    "seg15991",
    "seg15992",
    "seg16008",
    "seg16030",
    "seg16038",
    "seg16040",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a MIT 6.824 LoRA training pack before starting adapter training."
    )
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--features-dir", default="")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", default="")
    parser.add_argument("--holdout-sample-id", action="append", default=[])
    parser.add_argument("--holdout-sample-ids-file", default="")
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    parser.add_argument("--min-train-rows", type=int, default=80)
    parser.add_argument("--min-val-rows", type=int, default=6)
    parser.add_argument("--min-test-rows", type=int, default=7)
    parser.add_argument("--min-total-hours", type=float, default=0.5)
    parser.add_argument("--warn-cps", type=float, default=5.6)
    parser.add_argument("--style", default="lecture7_goldmatch")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(root: Path, raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_holdout_ids(args: argparse.Namespace, root: Path) -> set[str]:
    ids = set(DEFAULT_HOLDOUT_IDS)
    ids.update(item.strip() for item in args.holdout_sample_id if item and item.strip())
    if args.holdout_sample_ids_file:
        path = resolve(root, args.holdout_sample_ids_file)
        if path.exists():
            for raw in path.read_text(encoding="utf-8").replace(",", "\n").splitlines():
                raw = raw.strip()
                if raw:
                    ids.add(raw)
    return {normalize_sample_id(item) for item in ids if item}


def normalize_sample_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.isdigit():
        return f"seg{value}"
    match = re.match(r"^(seg\d+)", value)
    if match:
        return match.group(1)
    return value


def sample_ids_for_row(row: dict[str, Any]) -> set[str]:
    ids = {
        normalize_sample_id(row.get("sample_id")),
        normalize_sample_id(row.get("source_sample_id")),
        normalize_sample_id(row.get("source_segment_id")),
        normalize_sample_id(row.get("segment_id")),
    }
    augmentation = row.get("augmentation")
    if isinstance(augmentation, dict):
        ids.add(normalize_sample_id(augmentation.get("source_sample_id")))
        ids.add(normalize_sample_id(augmentation.get("source_segment_id")))
    repair = row.get("repair_metadata")
    if isinstance(repair, dict):
        ids.add(normalize_sample_id(repair.get("source_sample_id")))
        ids.add(normalize_sample_id(repair.get("repair_sample_id")))
    return {item for item in ids if item}


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def split_rows(manifest_dir: Path) -> dict[str, list[dict[str, Any]]]:
    return {split: read_jsonl(manifest_dir / f"{split}.jsonl") for split in ("train", "val", "test")}


def feature_rows(features_dir: Path | None) -> dict[str, list[dict[str, Any]]]:
    if not features_dir:
        return {}
    return {split: read_jsonl(features_dir / f"{split}.features.jsonl") for split in ("train", "val", "test")}


def row_hours(rows: list[dict[str, Any]]) -> float:
    return sum(as_float(row.get("audio_ms") or row.get("target_ms")) for row in rows) / 3_600_000.0


def row_fit(row: dict[str, Any]) -> float:
    return as_float(row.get("fit_tempo_delta"), 0.0)


def row_cps(row: dict[str, Any]) -> float:
    return max(
        as_float(row.get("target_chars_per_sec"), 0.0),
        as_float(row.get("audio_chars_per_sec"), 0.0),
    )


def audit(args: argparse.Namespace) -> dict[str, Any]:
    root = repo_root()
    manifest_dir = resolve(root, args.manifest_dir)
    features_dir = resolve(root, args.features_dir) if args.features_dir else None
    manifest = read_json(manifest_dir / "manifest.json")
    rows_by_split = split_rows(manifest_dir)
    features_by_split = feature_rows(features_dir)
    holdout_ids = load_holdout_ids(args, root)

    all_rows: list[dict[str, Any]] = []
    for split, rows in rows_by_split.items():
        for row in rows:
            row = dict(row)
            row["_split"] = split
            all_rows.append(row)

    leakage: list[dict[str, Any]] = []
    high_fit_rows: list[dict[str, Any]] = []
    high_cps_rows: list[dict[str, Any]] = []
    source_risks: list[dict[str, Any]] = []
    for row in all_rows:
        row_ids = sample_ids_for_row(row)
        leaked = sorted(row_ids & holdout_ids)
        if leaked:
            leakage.append(
                {
                    "split": row.get("_split"),
                    "sample_id": row.get("sample_id"),
                    "source_ids": sorted(row_ids),
                    "matched_holdout_ids": leaked,
                    "tier": row.get("tier"),
                }
            )
        fit = row_fit(row)
        if fit > args.max_fit_delta:
            high_fit_rows.append(
                {
                    "split": row.get("_split"),
                    "sample_id": row.get("sample_id"),
                    "fit_tempo_delta": fit,
                    "tier": row.get("tier"),
                }
            )
        cps = row_cps(row)
        if cps > args.warn_cps:
            high_cps_rows.append(
                {
                    "split": row.get("_split"),
                    "sample_id": row.get("sample_id"),
                    "chars_per_sec": round(cps, 6),
                    "tier": row.get("tier"),
                }
            )
        policy = row.get("training_policy")
        risk = str(policy.get("risk") if isinstance(policy, dict) else row.get("risk") or "")
        source = " ".join(
            str(row.get(key) or "")
            for key in ("text_source", "source", "audio_relpath", "audio_path")
        )
        if "failed" in risk.lower() or "failed_shadow" in source.lower():
            source_risks.append(
                {
                    "split": row.get("_split"),
                    "sample_id": row.get("sample_id"),
                    "risk": risk,
                    "tier": row.get("tier"),
                }
            )

    split_counts = {split: len(rows) for split, rows in rows_by_split.items()}
    feature_counts = {split: len(rows) for split, rows in features_by_split.items()} if features_by_split else {}
    stats = {
        "manifest_rows": split_counts,
        "feature_rows": feature_counts,
        "total_manifest_rows": len(all_rows),
        "total_feature_rows": sum(feature_counts.values()) if feature_counts else None,
        "total_audio_hours": round(row_hours(all_rows), 4),
        "by_tier": dict(sorted(Counter(str(row.get("tier") or "unknown") for row in all_rows).items())),
        "max_fit_tempo_delta": max((row_fit(row) for row in all_rows), default=0.0),
        "max_chars_per_sec": round(max((row_cps(row) for row in all_rows), default=0.0), 6),
        "high_cps_warning_count": len(high_cps_rows),
    }

    blockers: list[str] = []
    warnings: list[str] = []
    if split_counts.get("train", 0) < args.min_train_rows:
        blockers.append(f"train_rows:{split_counts.get('train', 0)}<{args.min_train_rows}")
    if split_counts.get("val", 0) < args.min_val_rows:
        blockers.append(f"val_rows:{split_counts.get('val', 0)}<{args.min_val_rows}")
    if split_counts.get("test", 0) < args.min_test_rows:
        blockers.append(f"test_rows:{split_counts.get('test', 0)}<{args.min_test_rows}")
    if stats["total_audio_hours"] < args.min_total_hours:
        blockers.append(f"total_audio_hours:{stats['total_audio_hours']}<{args.min_total_hours}")
    if leakage:
        blockers.append(f"holdout_leakage:{len(leakage)}")
    if high_fit_rows:
        blockers.append(f"fit_delta_above_{args.max_fit_delta}:{len(high_fit_rows)}")
    if source_risks:
        blockers.append(f"failed_shadow_or_bad_source_rows:{len(source_risks)}")
    if high_cps_rows:
        warnings.append(f"chars_per_sec_above_{args.warn_cps}:{len(high_cps_rows)}")
    if features_by_split:
        for split in ("train", "val", "test"):
            if feature_counts.get(split, 0) == 0 and split_counts.get(split, 0) > 0:
                blockers.append(f"{split}_features_missing")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "style": args.style,
        "manifest_dir": rel(manifest_dir, root),
        "features_dir": rel(features_dir, root) if features_dir else "",
        "manifest_schema_version": manifest.get("schema_version"),
        "manifest_tier": manifest.get("tier"),
        "manifest_policy": manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {},
        "thresholds": {
            "max_fit_delta": args.max_fit_delta,
            "min_train_rows": args.min_train_rows,
            "min_val_rows": args.min_val_rows,
            "min_test_rows": args.min_test_rows,
            "min_total_hours": args.min_total_hours,
            "warn_cps": args.warn_cps,
        },
        "holdout_ids": sorted(holdout_ids),
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "warnings": warnings,
        "stats": stats,
        "holdout_leakage": leakage[:200],
        "high_fit_rows": high_fit_rows[:200],
        "high_cps_rows": high_cps_rows[:200],
        "source_risks": source_risks[:200],
        "training_decision": {
            "allowed_to_start_training": not blockers,
            "reason": "training_pack_passed_preflight" if not blockers else "training_pack_failed_preflight",
            "required_before_promotion": [
                "generated_audio_holdout_beats_current_goldmatch",
                "selected_fit_delta_max<=0.05",
                "asr_join_tail_hard_gates_pass",
                "episode_shadow_fit_audit_pass",
                "human_review_pack_pass",
            ],
        },
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# LoRA Training Pack Audit",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        f"Manifest: `{payload['manifest_dir']}`",
        f"Features: `{payload.get('features_dir') or '-'}`",
        "",
        "## Decision",
        "",
        f"- Allowed to start training: `{payload['training_decision']['allowed_to_start_training']}`",
        f"- Reason: `{payload['training_decision']['reason']}`",
        f"- Blockers: `{payload['blockers']}`",
        f"- Warnings: `{payload['warnings']}`",
        "",
        "## Stats",
        "",
        f"- Manifest rows: `{payload['stats']['manifest_rows']}`",
        f"- Feature rows: `{payload['stats']['feature_rows']}`",
        f"- Total audio hours: `{payload['stats']['total_audio_hours']}`",
        f"- By tier: `{payload['stats']['by_tier']}`",
        f"- Max fit tempo delta: `{payload['stats']['max_fit_tempo_delta']}`",
        f"- Max chars/sec: `{payload['stats']['max_chars_per_sec']}`",
        "",
        "## Leakage",
        "",
        f"- Holdout leakage rows: `{len(payload['holdout_leakage'])}`",
        "",
    ]
    if payload["holdout_leakage"]:
        lines.extend(["| Split | Sample | Matched Holdout | Tier |", "| --- | --- | --- | --- |"])
        for row in payload["holdout_leakage"][:50]:
            lines.append(
                f"| {row.get('split')} | {row.get('sample_id')} | {row.get('matched_holdout_ids')} | {row.get('tier')} |"
            )
        lines.append("")
    if payload["high_fit_rows"]:
        lines.extend(["## High Fit Rows", "", "| Split | Sample | Fit | Tier |", "| --- | --- | ---: | --- |"])
        for row in payload["high_fit_rows"][:50]:
            lines.append(
                f"| {row.get('split')} | {row.get('sample_id')} | {row.get('fit_tempo_delta')} | {row.get('tier')} |"
            )
        lines.append("")
    if payload["high_cps_rows"]:
        lines.extend(["## CPS Warnings", "", "| Split | Sample | CPS | Tier |", "| --- | --- | ---: | --- |"])
        for row in payload["high_cps_rows"][:50]:
            lines.append(
                f"| {row.get('split')} | {row.get('sample_id')} | {row.get('chars_per_sec')} | {row.get('tier')} |"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    payload = audit(args)
    output_json = resolve(root, args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_md = resolve(root, args.output_md) if args.output_md else output_json.with_suffix(".md")
    write_markdown(output_md, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "output_json": rel(output_json, root),
                "output_md": rel(output_md, root),
                "blockers": payload["blockers"],
                "warnings": payload["warnings"],
                "stats": payload["stats"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
