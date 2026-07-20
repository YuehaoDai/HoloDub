from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RISK_REGRESSION_ANCHORS = {"seg10569", "seg10686", "seg10634"}
RISK_REASON_PRIORITY = {
    "listening_override_applied": 0,
    "manual_listening_issue": 0,
    "fit_tempo_delta_gt_0.08": 1,
    "fallback_tier_not_strict": 2,
    "f0_lt_4.8": 3,
    "similarity_lt_0.985": 4,
    "latin_surface_recall_lt_1.0": 5,
    "regression_anchor": 6,
    "tail_truncation": 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine existing LoRA canary candidate reports, then run prosody, gate, listening pack, and summary."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--candidate", action="append", default=[], help="Candidate report as label=path.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--voice-prompt-relpath", default="voices/test_ref.wav")
    parser.add_argument("--title", default="HoloDub LoRA Combined Gate")
    parser.add_argument("--review-notes", action="append", default=[], help="Listening review notes JSON. May be repeated.")
    parser.add_argument(
        "--listening-overrides",
        action="append",
        default=[],
        help="Human listening override JSON. May be repeated.",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--docker-image", default="holodub-ml-service:latest")
    parser.add_argument("--no-docker", action="store_true")

    parser.add_argument("--max-drift-ms", type=float, default=500.0)
    parser.add_argument("--target-mean-drift-ms", type=float, default=300.0)
    parser.add_argument("--min-similarity", type=float, default=0.97)
    parser.add_argument("--target-similarity", type=float, default=0.985)
    parser.add_argument("--min-f0-range", type=float, default=4.5)
    parser.add_argument("--target-f0-range", type=float, default=5.4)
    parser.add_argument("--min-critical-term-recall", type=float, default=0.8)
    parser.add_argument("--target-critical-term-recall", type=float, default=0.95)
    parser.add_argument("--min-tail-recall", type=float, default=0.0)
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    parser.add_argument("--allow-failed-join-quality", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_review_notes(paths: list[str]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for value in paths:
        if not value:
            continue
        payload = read_json(Path(value))
        current = payload.get("notes", []) if isinstance(payload, dict) else payload
        if not isinstance(current, list):
            raise ValueError(f"review notes must be a list or {{'notes': [...]}}: {value}")
        for note in current:
            if isinstance(note, dict) and note.get("sample_id") is not None:
                notes.append(note)
    return notes


def parse_candidate(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"--candidate must be label=path, got {value!r}")
    label, raw_path = value.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"--candidate label is empty: {value!r}")
    path = Path(raw_path.strip())
    if not path.exists():
        raise FileNotFoundError(path)
    return label, path


def rel_to_repo(path: Path) -> str:
    path = path.resolve()
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        rel = path
    return str(rel).replace("\\", "/")


def path_str(path: Path) -> str:
    return str(path)


def docker_python_command(args: argparse.Namespace, tool_args: list[str]) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "-v",
        f"{REPO_ROOT}:/workspace",
        "-v",
        f"{REPO_ROOT / 'data'}:/data",
        "-v",
        f"{REPO_ROOT / 'hf-cache'}:/root/.cache/huggingface",
        "-v",
        f"{REPO_ROOT / 'torch-cache'}:/root/.cache/torch",
        "-w",
        "/workspace",
        args.docker_image,
        "python3",
        *tool_args,
    ]


def python_tool_command(args: argparse.Namespace, tool_args: list[str], *, docker: bool = False) -> list[str]:
    if docker and not args.no_docker:
        return docker_python_command(args, tool_args)
    return [args.python, *tool_args]


def run_command(cmd: list[str], *, commands: list[dict[str, Any]], label: str) -> None:
    commands.append({"label": label, "cmd": cmd})
    print(f"[combine] {label}: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


def metric_rows(report: Path) -> list[dict[str, Any]]:
    return list(read_json(report).get("rows", []))


def summarize_gate(gate_report: Path) -> dict[str, Any]:
    gate = read_json(gate_report)
    rows = gate.get("rows", [])
    low_asr = [
        {
            "sample_id": row.get("sample_id"),
            "candidate_label": row.get("candidate_label"),
            "similarity": row.get("similarity"),
            "abs_drift_ms": row.get("abs_drift_ms"),
        }
        for row in rows
        if row.get("similarity") is not None and float(row.get("similarity") or 0.0) < 0.97
    ]
    low_terms = [
        {
            "sample_id": row.get("sample_id"),
            "candidate_label": row.get("candidate_label"),
            "critical_term_recall": row.get("critical_term_recall"),
            "critical_terms_missing": row.get("critical_terms_missing"),
        }
        for row in rows
        if row.get("critical_term_recall") is not None and float(row["critical_term_recall"]) < 0.8
    ]
    fallback_samples = [
        {
            "sample_id": item.get("sample_id"),
            "choice": item.get("choice"),
            "chosen_similarity": item.get("chosen_similarity"),
            "chosen_abs_drift_ms": item.get("chosen_abs_drift_ms"),
            "no_selection": bool(item.get("no_selection")),
            "no_selection_reason": item.get("no_selection_reason"),
        }
        for item in gate.get("decisions", [])
        if item.get("fallback_used")
    ]
    no_selection_samples = [
        {
            "sample_id": item.get("sample_id"),
            "reason": item.get("no_selection_reason"),
            "candidate_rejections": [
                {
                    "label": candidate.get("label"),
                    "reject_reasons": candidate.get("reject_reasons", []),
                    "abs_drift_ms": candidate.get("abs_drift_ms"),
                    "similarity": candidate.get("similarity"),
                    "join_quality_pass": candidate.get("join_quality_pass"),
                    "join_quality_reasons": candidate.get("join_quality_reasons", []),
                }
                for candidate in item.get("candidates", [])
            ],
        }
        for item in gate.get("decisions", [])
        if item.get("no_selection")
    ]
    return {
        "selected_metrics": gate.get("summary", {}).get("selected", {}),
        "per_candidate_metrics": gate.get("summary", {}).get("candidates", {}),
        "fallback_samples": fallback_samples,
        "no_selection_samples": no_selection_samples,
        "low_asr_samples": low_asr,
        "low_critical_term_samples": low_terms,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_labels(row: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for value in (
        row.get("candidate_label"),
        row.get("upstream_candidate_label"),
        row.get("label"),
        *(row.get("candidate_label_chain") or []),
    ):
        if value is not None:
            labels.add(str(value))
    return labels


def _decision_by_sample(gate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(decision.get("sample_id")): decision
        for decision in gate.get("decisions", [])
        if decision.get("sample_id") is not None
    }


def _selected_by_sample(gate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("sample_id")): row
        for row in gate.get("rows", [])
        if row.get("sample_id") is not None
    }


def _runner_up(decision: dict[str, Any]) -> dict[str, Any] | None:
    choice = str(decision.get("choice") or "")
    for candidate in decision.get("candidates", []):
        labels = _candidate_labels(candidate)
        if choice and choice in labels:
            continue
        return candidate
    return None


def review_notes_by_sample(review_notes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_sample: dict[str, list[dict[str, Any]]] = {}
    for note in review_notes:
        sample_id = str(note.get("sample_id") or "").strip()
        if not sample_id:
            continue
        by_sample.setdefault(sample_id, []).append(note)
    return by_sample


def note_forces_risk(note: dict[str, Any]) -> bool:
    if bool(note.get("force_risk")):
        return True
    severity = str(note.get("severity") or "").strip().lower()
    return severity in {"blocker", "critical", "high"}


def build_risk_queue(gate: dict[str, Any], review_notes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    decisions = _decision_by_sample(gate)
    notes_by_sample = review_notes_by_sample(review_notes or [])
    entries: list[dict[str, Any]] = []
    for row in gate.get("rows", []):
        sample_id = str(row.get("sample_id"))
        if not sample_id or sample_id == "None":
            continue
        decision = decisions.get(sample_id, {})
        reasons: list[str] = []
        if row.get("listening_override_applied") or decision.get("listening_override_applied"):
            reasons.append("listening_override_applied")
        fit_delta = _float_or_none(row.get("fit_tempo_delta"))
        if fit_delta is not None and fit_delta > 0.08:
            reasons.append("fit_tempo_delta_gt_0.08")
        fallback_tier = str(decision.get("fallback_tier") or "strict")
        if fallback_tier != "strict":
            reasons.append("fallback_tier_not_strict")
        f0_range = _float_or_none(row.get("f0_range_semitones_10_90"))
        if f0_range is not None and f0_range < 4.8:
            reasons.append("f0_lt_4.8")
        similarity = _float_or_none(row.get("similarity"))
        if similarity is not None and similarity < 0.985:
            reasons.append("similarity_lt_0.985")
        latin = _float_or_none(row.get("latin_surface_recall"))
        if latin is not None and latin < 1.0:
            reasons.append("latin_surface_recall_lt_1.0")
        if "tail_truncation" in (row.get("reject_reasons") or []):
            reasons.append("tail_truncation")
        forced_notes = [note for note in notes_by_sample.get(sample_id, []) if note_forces_risk(note)]
        if forced_notes:
            reasons.append("manual_listening_issue")
        if sample_id in RISK_REGRESSION_ANCHORS:
            reasons.append("regression_anchor")
        if not reasons:
            continue
        runner = _runner_up(decision)
        entries.append(
            {
                "sample_id": sample_id,
                "reasons": reasons,
                "priority": min(RISK_REASON_PRIORITY[reason] for reason in reasons),
                "selected_candidate_label": row.get("candidate_label"),
                "selected_upstream_candidate_label": row.get("upstream_candidate_label"),
                "selected_candidate_label_chain": row.get("candidate_label_chain") or [],
                "fallback_tier": fallback_tier,
                "abs_drift_ms": row.get("abs_drift_ms"),
                "similarity": row.get("similarity"),
                "critical_term_recall": row.get("critical_term_recall"),
                "latin_surface_recall": row.get("latin_surface_recall"),
                "tail_recall": row.get("tail_recall"),
                "tail_expected": row.get("tail_expected"),
                "tail_observed_window": row.get("tail_observed_window"),
                "f0_range_semitones_10_90": row.get("f0_range_semitones_10_90"),
                "fit_tempo_delta": row.get("fit_tempo_delta"),
                "listening_override_applied": bool(
                    row.get("listening_override_applied") or decision.get("listening_override_applied")
                ),
                "review_notes": forced_notes,
                "runner_up": runner,
            }
        )
    entries.sort(key=lambda item: (item["priority"], item["sample_id"]))
    return {
        "count": len(entries),
        "sample_ids": [entry["sample_id"] for entry in entries],
        "entries": entries,
    }


def build_human_review_report(gate: dict[str, Any], risk_queue: dict[str, Any]) -> dict[str, Any]:
    selected = _selected_by_sample(gate)
    override_items: list[dict[str, Any]] = []
    for decision in gate.get("decisions", []):
        if not decision.get("listening_override_applied"):
            continue
        sample_id = str(decision.get("sample_id"))
        row = selected.get(sample_id, {})
        override_items.append(
            {
                "sample_id": sample_id,
                "choice": decision.get("choice"),
                "listening_override": decision.get("listening_override"),
                "covered_reject_reasons": decision.get("chosen_reject_reasons") or [],
                "metrics": {
                    "abs_drift_ms": decision.get("chosen_abs_drift_ms"),
                    "similarity": decision.get("chosen_similarity"),
                    "critical_term_recall": decision.get("chosen_critical_term_recall"),
                    "f0_range_semitones_10_90": decision.get("chosen_f0_range_semitones_10_90"),
                    "fit_tempo_delta": decision.get("chosen_fit_tempo_delta"),
                    "candidate_label_chain": row.get("candidate_label_chain") or [],
                },
                "runner_up": _runner_up(decision),
            }
        )

    rows = list(gate.get("rows", []))
    manual_issue_entries = [
        entry for entry in risk_queue.get("entries", []) if "manual_listening_issue" in (entry.get("reasons") or [])
    ]
    tempo_over_8pct = [
        row for row in rows if (_float_or_none(row.get("fit_tempo_delta")) or 0.0) > 0.08
    ]
    low_f0 = [
        row for row in rows if (value := _float_or_none(row.get("f0_range_semitones_10_90"))) is not None and value < 4.8
    ]
    low_asr = [
        row for row in rows if (value := _float_or_none(row.get("similarity"))) is not None and value < 0.985
    ]
    low_critical_term = [
        row
        for row in rows
        if (value := _float_or_none(row.get("critical_term_recall"))) is not None and value < 0.95
    ]
    return {
        "summary": {
            "listening_override_applied_count": len(override_items),
            "manual_listening_issue_count": len(manual_issue_entries),
            "pending_review_count": len(risk_queue.get("entries", [])),
            "tempo_over_8pct_count": len(tempo_over_8pct),
            "low_f0_count": len(low_f0),
            "low_asr_count": len(low_asr),
            "low_critical_term_count": len(low_critical_term),
        },
        "listening_overrides": override_items,
        "manual_listening_issues": manual_issue_entries,
        "needs_review": risk_queue.get("entries", []),
        "tempo_over_8pct": tempo_over_8pct,
        "low_f0": low_f0,
        "low_asr": low_asr,
        "low_critical_term": low_critical_term,
    }


def build_quality_baseline(run_dir: Path, gate: dict[str, Any], risk_queue: dict[str, Any] | None = None) -> dict[str, Any]:
    selected_metrics = gate.get("summary", {}).get("selected", {})
    selected_count = int(selected_metrics.get("count") or 0)
    risk_entries = (risk_queue or {}).get("entries", [])
    manual_issue_count = sum(1 for entry in risk_entries if "manual_listening_issue" in (entry.get("reasons") or []))
    return {
        "baseline_id": run_dir.name,
        "candidate_gate_report": str(run_dir / "candidate_gate_report.json"),
        "known_human_issue_count": manual_issue_count,
        "selected_metrics": selected_metrics,
        "acceptance_thresholds": {
            "count": selected_count,
            "mean_abs_drift_ms_lt": 50.0,
            "max_abs_drift_ms_lt": 300.0,
            "mean_similarity_gte": 0.99,
            "min_similarity_gte": 0.96,
            "join_quality_fail_count_eq": 0,
        },
        "regression_anchors": {
            "seg10569": {
                "expected_label_chain_contains": "shard01_selected",
                "forbidden_selected_label_chain_contains": "split_light_vector_10",
            },
            "seg10686": {
                "expected_label_chain_contains": "split_mix",
            },
            "seg10634": {
                "must_be_present_in_risk_queue": True,
            },
        },
    }


def build_risk_candidate_report(gate: dict[str, Any], risk_queue: dict[str, Any]) -> dict[str, Any]:
    decisions = _decision_by_sample(gate)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sample_id in risk_queue.get("sample_ids", []):
        decision = decisions.get(str(sample_id), {})
        candidates: list[dict[str, Any]] = []
        runner = _runner_up(decision)
        if runner:
            candidates.append(runner)
        for candidate in decision.get("candidates", []):
            labels = _candidate_labels(candidate)
            if any("10" in label or "rescue" in label for label in labels):
                candidates.append(candidate)
        for candidate in candidates:
            path = candidate.get("path") or candidate.get("selected_path") or candidate.get("selected_source_path")
            label = str(candidate.get("label") or "candidate")
            if not path:
                continue
            key = (str(sample_id), label, str(path))
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "sample_id": sample_id,
                    "candidate_label": label,
                    "upstream_candidate_label": candidate.get("upstream_candidate_label"),
                    "candidate_label_chain": candidate.get("candidate_label_chain") or [],
                    "path": path,
                    "duration_sec": candidate.get("duration_sec"),
                    "target_sec": candidate.get("target_sec"),
                    "similarity": candidate.get("similarity"),
                    "critical_term_recall": candidate.get("critical_term_recall"),
                    "critical_terms_missing": candidate.get("critical_terms_missing") or [],
                    "latin_surface_recall": candidate.get("latin_surface_recall"),
                    "variant": candidate.get("variant"),
                    "fit_tempo_delta": candidate.get("fit_tempo_delta"),
                    "fit_applied": candidate.get("fit_applied"),
                    "join_quality": {
                        "pass": candidate.get("join_quality_pass"),
                        "reasons": candidate.get("join_quality_reasons") or [],
                    },
                }
            )
    return {"rows": rows}


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    selected_dir = run_dir / "selected"
    if selected_dir.exists():
        shutil.rmtree(selected_dir)
    selected_dir.mkdir(parents=True, exist_ok=True)

    candidate_reports = dict(parse_candidate(value) for value in args.candidate)
    if not candidate_reports:
        raise SystemExit("At least one --candidate is required.")

    commands: list[dict[str, Any]] = []
    prosody_report = run_dir / "prosody_report.json"
    gate_report = run_dir / "candidate_gate_report.json"
    listening_dir = run_dir / "listening_pack"
    risk_listening_dir = run_dir / "risk_listening_pack"
    risk_queue_path = run_dir / "risk_queue.json"
    human_review_report = run_dir / "human_review_report.json"
    quality_baseline = run_dir / "quality_baseline.json"
    risk_candidate_report = run_dir / "risk_candidates_report.json"
    risk_sample_ids = run_dir / "risk_queue.sample_ids.txt"

    reports_for_docker = ",".join(rel_to_repo(path) for path in candidate_reports.values())
    run_command(
        python_tool_command(
            args,
            [
                "tools/lora_prosody_eval.py",
                "--features",
                rel_to_repo(Path(args.features)),
                "--data-root",
                args.data_root,
                "--reference-wav",
                args.voice_prompt_relpath,
                "--include-teacher",
                "--output",
                rel_to_repo(prosody_report),
                "--reports",
                reports_for_docker,
            ],
            docker=True,
        ),
        commands=commands,
        label="prosody",
    )

    gate_args = [
        "tools/lora_candidate_gate.py",
        "--output",
        path_str(gate_report),
        "--selected-dir",
        path_str(selected_dir),
        "--prosody-report",
        path_str(prosody_report),
        "--max-drift-ms",
        str(args.max_drift_ms),
        "--target-mean-drift-ms",
        str(args.target_mean_drift_ms),
        "--min-similarity",
        str(args.min_similarity),
        "--target-similarity",
        str(args.target_similarity),
        "--min-f0-range",
        str(args.min_f0_range),
        "--target-f0-range",
        str(args.target_f0_range),
        "--min-critical-term-recall",
        str(args.min_critical_term_recall),
        "--target-critical-term-recall",
        str(args.target_critical_term_recall),
        "--min-tail-recall",
        str(args.min_tail_recall),
        "--max-fit-delta",
        str(args.max_fit_delta),
    ]
    if args.allow_failed_join_quality:
        gate_args.append("--allow-failed-join-quality")
    for override_path in args.listening_overrides:
        gate_args.extend(["--listening-overrides", override_path])
    for label, report in sorted(candidate_reports.items()):
        gate_args.extend(["--candidate", f"{label}={report}"])
    run_command(python_tool_command(args, gate_args), commands=commands, label="candidate_gate")

    gate_payload = read_json(gate_report)
    review_notes = load_review_notes(args.review_notes)
    risk_queue = build_risk_queue(gate_payload, review_notes)
    write_json(risk_queue_path, risk_queue)
    risk_sample_ids.write_text(
        "\n".join(risk_queue.get("sample_ids", [])) + ("\n" if risk_queue.get("sample_ids") else ""),
        encoding="utf-8",
    )
    write_json(human_review_report, build_human_review_report(gate_payload, risk_queue))
    write_json(quality_baseline, build_quality_baseline(run_dir, gate_payload, risk_queue))
    write_json(risk_candidate_report, build_risk_candidate_report(gate_payload, risk_queue))

    pack_args = [
        "tools/lora_build_listening_pack.py",
        "--output-dir",
        path_str(listening_dir),
        "--features",
        path_str(Path(args.features)),
        "--data-root",
        args.data_root,
        "--include-teacher",
        "--only-report-samples",
        "--reference-wav",
        args.voice_prompt_relpath,
        "--title",
        args.title,
        "--report",
        f"selected={gate_report}",
    ]
    for label, report in sorted(candidate_reports.items()):
        pack_args.extend(["--report", f"{label}={report}"])
    for review_note in args.review_notes:
        pack_args.extend(["--review-notes", review_note])
    run_command(python_tool_command(args, pack_args), commands=commands, label="listening_pack")

    risk_pack_args = [
        "tools/lora_build_listening_pack.py",
        "--output-dir",
        path_str(risk_listening_dir),
        "--features",
        path_str(Path(args.features)),
        "--data-root",
        args.data_root,
        "--include-teacher",
        "--only-report-samples",
        "--sample-ids-file",
        path_str(risk_sample_ids),
        "--reference-wav",
        args.voice_prompt_relpath,
        "--title",
        f"{args.title} risk review",
        "--report",
        f"selected={gate_report}",
        "--report",
        f"runner_up={risk_candidate_report}",
    ]
    for review_note in args.review_notes:
        risk_pack_args.extend(["--review-notes", review_note])
    run_command(python_tool_command(args, risk_pack_args), commands=commands, label="risk_listening_pack")

    summary = {
        "features": args.features,
        "run_dir": str(run_dir),
        "candidate_reports": {label: str(path) for label, path in sorted(candidate_reports.items())},
        "review_notes": list(args.review_notes),
        "listening_overrides": list(args.listening_overrides),
        "prosody_report": str(prosody_report),
        "candidate_gate_report": str(gate_report),
        "listening_pack": str(listening_dir / "index.html"),
        "risk_listening_pack": str(risk_listening_dir / "index.html"),
        "risk_queue": str(risk_queue_path),
        "human_review_report": str(human_review_report),
        "quality_baseline": str(quality_baseline),
        "commands": commands,
        "config": {
            "max_drift_ms": args.max_drift_ms,
            "target_mean_drift_ms": args.target_mean_drift_ms,
            "min_similarity": args.min_similarity,
            "target_similarity": args.target_similarity,
            "min_f0_range": args.min_f0_range,
            "target_f0_range": args.target_f0_range,
            "min_critical_term_recall": args.min_critical_term_recall,
            "target_critical_term_recall": args.target_critical_term_recall,
            "min_tail_recall": args.min_tail_recall,
            "max_fit_delta": args.max_fit_delta,
            "allow_failed_join_quality": bool(args.allow_failed_join_quality),
        },
        **summarize_gate(gate_report),
    }
    write_json(run_dir / "combined_summary.json", summary)
    print(json.dumps({"summary": str(run_dir / "combined_summary.json"), "listening_pack": str(listening_dir / "index.html")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
