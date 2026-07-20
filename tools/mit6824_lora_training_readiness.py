#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "holodub.mit6824_lora_training_readiness.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decide whether a MIT 6.824 LoRA corpus is ready for another duration/prosody training run."
    )
    parser.add_argument("--candidate-manifest-dir", required=True)
    parser.add_argument("--reference-manifest-dir", default="")
    parser.add_argument("--audit-json", required=True)
    parser.add_argument("--previous-decision-json", action="append", default=[])
    parser.add_argument("--episode-monitor-json", action="append", default=[])
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", default="")
    parser.add_argument("--min-new-source-ids", type=int, default=40)
    parser.add_argument("--min-total-hours", type=float, default=0.8)
    parser.add_argument("--allow-audit-fail", action="store_true")
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
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


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


def source_ids_for_row(row: dict[str, Any]) -> set[str]:
    ids = {
        normalize_sample_id(row.get("sample_id")),
        normalize_sample_id(row.get("segment_id")),
        normalize_sample_id(row.get("source_sample_id")),
        normalize_sample_id(row.get("source_segment_id")),
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


def corpus_summary(manifest_dir: Path, root: Path) -> dict[str, Any]:
    manifest = read_json_if_exists(manifest_dir / "manifest.json")
    all_rows = read_jsonl(manifest_dir / "all.jsonl")
    split_counts = {split: len(read_jsonl(manifest_dir / f"{split}.jsonl")) for split in ("train", "val", "test")}
    source_ids: set[str] = set()
    for row in all_rows:
        source_ids.update(source_ids_for_row(row))
    stats = manifest.get("stats") if isinstance(manifest.get("stats"), dict) else {}
    return {
        "manifest_dir": rel(manifest_dir, root),
        "manifest_present": bool(manifest),
        "row_count": len(all_rows),
        "source_id_count": len(source_ids),
        "source_ids": sorted(source_ids),
        "split_counts": split_counts,
        "total_audio_hours": float(stats.get("total_audio_hours") or 0.0),
        "tier": manifest.get("tier") or "",
        "warnings": manifest.get("warnings") or [],
    }


def decision_status(args: argparse.Namespace, payload: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    blockers: list[str] = []
    recommendations: list[str] = []
    audit = payload["audit"]
    candidate = payload["candidate"]
    comparison = payload.get("reference_comparison") or {}

    if audit.get("status") != "pass" and not args.allow_audit_fail:
        blockers.append(f"training_pack_audit_not_pass:{audit.get('status') or 'unknown'}")
    if float(candidate.get("total_audio_hours") or 0.0) < args.min_total_hours:
        blockers.append(
            f"candidate_audio_hours:{candidate.get('total_audio_hours')}<{args.min_total_hours}"
        )
    new_count = int(comparison.get("new_source_id_count") or 0)
    if payload.get("reference") and new_count < args.min_new_source_ids:
        blockers.append(f"new_source_ids:{new_count}<{args.min_new_source_ids}")

    for item in payload.get("previous_decisions") or []:
        decision = str(item.get("decision") or item.get("status") or "")
        if "rejected" in decision or "regressed" in decision:
            recommendations.append(
                "previous_adapter_regressed_on_generated_holdout; require new clean data before retraining"
            )
            break

    monitor_blockers = []
    for monitor in payload.get("episode_monitors") or []:
        agent = monitor.get("agent") if isinstance(monitor.get("agent"), dict) else {}
        failure_count = int(agent.get("failure_count") or 0)
        high_fit_count = int(agent.get("failure_fit_delta_count") or 0)
        if failure_count or high_fit_count:
            monitor_blockers.append(
                {
                    "path": monitor.get("_path"),
                    "failure_count": failure_count,
                    "failure_fit_delta_count": high_fit_count,
                }
            )
    if monitor_blockers:
        recommendations.append("episode_monitor_has_repair_queue; repair/regenerate before using those rows as training data")

    if blockers:
        recommendations.append("do_not_start_new_training_from_this_pack")
        if payload.get("reference") and new_count == 0:
            recommendations.append("collect_new_non_holdout_ASR_pass_fit005_human_approved_segments")
        return "not_ready_to_train", blockers, sorted(set(recommendations))
    return "ready_to_train", blockers, sorted(set(recommendations))


def render_markdown(payload: dict[str, Any]) -> str:
    candidate = payload["candidate"]
    audit = payload["audit"]
    comparison = payload.get("reference_comparison") or {}
    blockers = payload.get("blockers") or []
    recommendations = payload.get("recommendations") or []
    lines = [
        "# LoRA Training Readiness",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Candidate Pack",
        "",
        f"- Manifest: `{candidate['manifest_dir']}`",
        f"- Rows: `{candidate['row_count']}`",
        f"- Source ids: `{candidate['source_id_count']}`",
        f"- Audio hours: `{candidate['total_audio_hours']}`",
        f"- Splits: train `{candidate['split_counts']['train']}`, val `{candidate['split_counts']['val']}`, test `{candidate['split_counts']['test']}`",
        f"- Audit status: `{audit.get('status')}`",
        "",
    ]
    if comparison:
        lines.extend(
            [
                "## Reference Comparison",
                "",
                f"- Reference: `{payload['reference']['manifest_dir']}`",
                f"- Candidate sources already in reference: `{comparison['overlap_source_id_count']}`",
                f"- New candidate sources: `{comparison['new_source_id_count']}`",
                "",
            ]
        )
    if blockers:
        lines.extend(["## Blockers", ""])
        lines.extend(f"- `{item}`" for item in blockers)
        lines.append("")
    if recommendations:
        lines.extend(["## Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = repo_root()
    candidate_dir = resolve(root, args.candidate_manifest_dir)
    audit_path = resolve(root, args.audit_json)
    candidate = corpus_summary(candidate_dir, root)
    audit = read_json(audit_path)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate": candidate,
        "audit": {
            "path": rel(audit_path, root),
            "status": audit.get("status"),
            "blockers": audit.get("blockers") or [],
            "warnings": audit.get("warnings") or [],
            "stats": audit.get("stats") or {},
        },
        "reference": None,
        "reference_comparison": None,
        "previous_decisions": [],
        "episode_monitors": [],
        "thresholds": {
            "min_new_source_ids": args.min_new_source_ids,
            "min_total_hours": args.min_total_hours,
            "allow_audit_fail": args.allow_audit_fail,
        },
    }

    if args.reference_manifest_dir:
        reference = corpus_summary(resolve(root, args.reference_manifest_dir), root)
        candidate_ids = set(candidate["source_ids"])
        reference_ids = set(reference["source_ids"])
        payload["reference"] = {k: v for k, v in reference.items() if k != "source_ids"}
        payload["reference_comparison"] = {
            "overlap_source_id_count": len(candidate_ids & reference_ids),
            "new_source_id_count": len(candidate_ids - reference_ids),
            "new_source_ids": sorted(candidate_ids - reference_ids),
        }

    for raw_path in args.previous_decision_json:
        path = resolve(root, raw_path)
        item = read_json_if_exists(path)
        if item:
            item["_path"] = rel(path, root)
            payload["previous_decisions"].append(item)

    for raw_path in args.episode_monitor_json:
        path = resolve(root, raw_path)
        item = read_json_if_exists(path)
        if item:
            item["_path"] = rel(path, root)
            payload["episode_monitors"].append(item)

    status, blockers, recommendations = decision_status(args, payload)
    payload["status"] = status
    payload["blockers"] = blockers
    payload["recommendations"] = recommendations

    output_json = resolve(root, args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.output_md:
        output_md = resolve(root, args.output_md)
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(render_markdown(payload), encoding="utf-8")

    print(json.dumps({"status": status, "blockers": blockers}, ensure_ascii=False, indent=2))
    return 0 if status == "ready_to_train" else 2


if __name__ == "__main__":
    raise SystemExit(main())
