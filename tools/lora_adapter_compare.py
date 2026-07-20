from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two or more LoRA candidate loop runs and emit a registry evidence report."
    )
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        help="Run label and directory as label=path. Provide at least two.",
    )
    parser.add_argument(
        "--adapter",
        action="append",
        default=[],
        help="Adapter label and checkpoint path as label=path. Labels should match --run labels.",
    )
    parser.add_argument("--output", required=True, help="Output adapter_compare_report.json path.")
    parser.add_argument("--runtime-label", default="", help="Label currently configured for runtime use.")
    parser.add_argument("--canonical-label", default="", help="Label currently treated as registry canonical.")
    parser.add_argument("--min-promotion-samples", type=int, default=84)
    return parser.parse_args()


def parse_label_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"expected label=path, got {value!r}")
    label, raw_path = value.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"missing label in {value!r}")
    return label, Path(raw_path.strip())


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def maybe_read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        return read_json(path), ""
    except Exception as exc:  # noqa: BLE001 - report generation must survive older malformed artifacts.
        return None, f"{type(exc).__name__}: {exc}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def file_info(path: Path) -> dict[str, Any]:
    resolved = path
    exists = resolved.exists()
    info: dict[str, Any] = {
        "path": str(path),
        "exists": exists,
    }
    if exists:
        stat = resolved.stat()
        info.update(
            {
                "size_bytes": stat.st_size,
                "sha256": sha256_file(resolved),
                "sha256_16": sha256_file(resolved)[:16],
            }
        )
    return info


def selected_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("selected_metrics")
    if isinstance(metrics, dict):
        return metrics
    nested = summary.get("summary")
    if isinstance(nested, dict) and isinstance(nested.get("selected"), dict):
        return nested["selected"]
    return {}


def sample_ids(summary: dict[str, Any]) -> list[str]:
    config = summary.get("config") if isinstance(summary.get("config"), dict) else {}
    raw = config.get("sample_ids")
    if isinstance(raw, str) and raw.strip():
        return [item.strip() for item in raw.split(",") if item.strip()]
    features = summary.get("features")
    if isinstance(features, list):
        return [str(item.get("sample_id")) for item in features if isinstance(item, dict) and item.get("sample_id")]
    return []


def compact_no_selection(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("no_selection_samples") or []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "sample_id": row.get("sample_id"),
                "reason": row.get("reason") or row.get("no_selection_reason"),
                "candidate_rejections": [
                    {
                        "label": item.get("label"),
                        "similarity": item.get("similarity"),
                        "abs_drift_ms": item.get("abs_drift_ms"),
                        "join_quality_pass": item.get("join_quality_pass"),
                        "reject_reasons": item.get("reject_reasons") or [],
                    }
                    for item in (row.get("candidate_rejections") or [])
                    if isinstance(item, dict)
                ],
            }
        )
    return out


def selected_audio_hashes(run_dir: Path) -> dict[str, dict[str, Any]]:
    selected_dir = run_dir / "selected"
    if not selected_dir.exists():
        return {}
    hashes: dict[str, dict[str, Any]] = {}
    for path in sorted(selected_dir.glob("*.wav")):
        hashes[path.name] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    return hashes


def gate_selected_rows(gate: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not gate:
        return {}
    rows = gate.get("rows")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("sample_id"):
            continue
        out[str(row["sample_id"])] = {
            "candidate_label": row.get("candidate_label"),
            "similarity": row.get("similarity"),
            "abs_drift_ms": row.get("abs_drift_ms"),
            "fit_tempo_delta": row.get("fit_tempo_delta"),
            "join_quality_pass": (row.get("join_quality") or {}).get("pass")
            if isinstance(row.get("join_quality"), dict)
            else row.get("join_quality_pass"),
            "selected_path": row.get("selected_path") or row.get("path"),
        }
    return out


def summarize_run(label: str, run_dir: Path, adapter_path: Path | None) -> dict[str, Any]:
    summary = read_json(run_dir / "loop_summary.json")
    gate, gate_error = maybe_read_json(run_dir / "candidate_gate_report.json")
    metrics = selected_metrics(summary)
    ids = sample_ids(summary)
    no_selection = compact_no_selection(summary)
    audio_hashes = selected_audio_hashes(run_dir)
    listening_pack = summary.get("listening_pack")
    if not listening_pack and (run_dir / "listening_pack" / "index.html").exists():
        listening_pack = str(run_dir / "listening_pack" / "index.html")
    return {
        "label": label,
        "run_dir": str(run_dir),
        "adapter": file_info(adapter_path) if adapter_path else None,
        "sample_ids": ids,
        "sample_count": len(ids),
        "selected_metrics": metrics,
        "selected_count": int(metrics.get("count") or len(audio_hashes)),
        "fallback_count": len(summary.get("fallback_samples") or []),
        "no_selection_count": len(no_selection),
        "no_selection_samples": no_selection,
        "selected_audio_hashes": audio_hashes,
        "selected_rows": gate_selected_rows(gate),
        "gate_report_error": gate_error,
        "listening_pack": listening_pack,
        "seg10686_split_mix_regression": summary.get("seg10686_split_mix_regression"),
    }


def metric_number(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = metrics.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def rank_tuple(run: dict[str, Any]) -> tuple[float, float, float, float, float]:
    metrics = run.get("selected_metrics") or {}
    return (
        float(run.get("selected_count") or 0),
        -float(run.get("no_selection_count") or 0),
        -metric_number(metrics, "join_quality_fail_count", 999.0),
        metric_number(metrics, "mean_similarity"),
        -metric_number(metrics, "mean_abs_drift_ms", 999999.0),
    )


def choose_winner(runs: dict[str, dict[str, Any]]) -> str:
    ranked = sorted(runs.values(), key=rank_tuple, reverse=True)
    if len(ranked) < 2:
        return ranked[0]["label"] if ranked else ""
    if rank_tuple(ranked[0]) == rank_tuple(ranked[1]):
        return "tie"
    return str(ranked[0]["label"])


def compare_audio_hashes(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    labels = sorted(runs)
    if len(labels) < 2:
        return {"identical": False, "reason": "need_at_least_two_runs"}
    first = runs[labels[0]].get("selected_audio_hashes") or {}
    comparisons: dict[str, Any] = {}
    all_identical = True
    for label in labels[1:]:
        current = runs[label].get("selected_audio_hashes") or {}
        common = sorted(set(first).intersection(current))
        only_first = sorted(set(first).difference(current))
        only_current = sorted(set(current).difference(first))
        mismatched = [
            name
            for name in common
            if first[name].get("sha256") != current[name].get("sha256")
        ]
        identical = not only_first and not only_current and not mismatched
        all_identical = all_identical and identical
        comparisons[f"{labels[0]}__vs__{label}"] = {
            "identical": identical,
            "common_files": common,
            "only_left": only_first,
            "only_right": only_current,
            "mismatched": mismatched,
        }
    return {"identical": all_identical, "comparisons": comparisons}


def compare_core_metrics(runs: dict[str, dict[str, Any]]) -> bool:
    values = [
        {
            "selected_metrics": run.get("selected_metrics"),
            "selected_count": run.get("selected_count"),
            "no_selection_count": run.get("no_selection_count"),
        }
        for run in runs.values()
    ]
    return all(value == values[0] for value in values[1:]) if values else False


def adapter_files_identical(runs: dict[str, dict[str, Any]]) -> bool:
    shas = {
        str((run.get("adapter") or {}).get("sha256"))
        for run in runs.values()
        if run.get("adapter")
    }
    return len(shas) == 1 and "" not in shas


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    run_paths = dict(parse_label_path(value) for value in args.run)
    adapter_paths = dict(parse_label_path(value) for value in args.adapter)
    if len(run_paths) < 2:
        raise ValueError("at least two --run values are required")

    runs = {
        label: summarize_run(label, run_dir, adapter_paths.get(label))
        for label, run_dir in run_paths.items()
    }
    winner = choose_winner(runs)
    audio = compare_audio_hashes(runs)
    core_identical = compare_core_metrics(runs)
    files_identical = adapter_files_identical(runs)
    sample_count = min((int(run.get("sample_count") or 0) for run in runs.values()), default=0)
    selected_counts = [int(run.get("selected_count") or 0) for run in runs.values()]
    min_selected_count = min(selected_counts, default=0)

    findings: list[str] = []
    if sample_count < args.min_promotion_samples:
        findings.append("smoke_only_not_promotion")
    if min_selected_count < sample_count:
        findings.append("partial_selection_no_promotion")
    if args.runtime_label and args.canonical_label and args.runtime_label != args.canonical_label:
        findings.append("runtime_differs_from_canonical")
    if audio.get("identical"):
        findings.append("selected_audio_identical")
    if not files_identical:
        findings.append("adapter_files_differ")

    if winner == "tie":
        if args.runtime_label:
            decision = "tie_keep_runtime_until_full_canary"
        else:
            decision = "tie_requires_full_canary"
    elif winner == args.canonical_label:
        decision = "canonical_preferred_by_smoke_but_requires_full_canary"
    elif winner == args.runtime_label:
        decision = "runtime_preferred_by_smoke_but_requires_full_canary"
    else:
        decision = "winner_requires_full_canary"

    promotion_allowed = (
        sample_count >= args.min_promotion_samples
        and min_selected_count == sample_count
        and winner != ""
        and "partial_selection_no_promotion" not in findings
    )

    return {
        "schema_version": "holodub.lora_adapter_compare.v1",
        "status": "smoke_complete_needs_full_canary" if not promotion_allowed else "comparison_pass",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "winner": winner,
        "decision": decision,
        "runtime_label": args.runtime_label,
        "canonical_label": args.canonical_label,
        "sample_count": sample_count,
        "promotion_allowed": promotion_allowed,
        "hard_findings": findings,
        "comparison": {
            "selected_audio_identical": audio.get("identical"),
            "selected_audio": audio,
            "core_metrics_identical": core_identical,
            "adapter_files_identical": files_identical,
        },
        "runs": runs,
    }


def main() -> int:
    args = parse_args()
    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "status": report["status"],
                "winner": report["winner"],
                "decision": report["decision"],
                "promotion_allowed": report["promotion_allowed"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
