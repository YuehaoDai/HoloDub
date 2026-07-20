from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize per-lecture MIT 6.824 dubbing acceptance artifacts."
    )
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument("--output-json", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_acceptance_status.md")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def rel_to_root(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        payload = load_json(path)
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": rel_to_root(path, repo_root())}
    return payload if isinstance(payload, dict) else {"_invalid_json": True, "_path": rel_to_root(path, repo_root())}


def latest_episode(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not episodes:
        return None
    return sorted(episodes, key=lambda row: int(row.get("id") or 0), reverse=True)[0]


def latest_completed_episode(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = [row for row in episodes if row.get("status") == "completed" and row.get("output_rel_path")]
    return latest_episode(completed)


def file_status(path: Path | None, root: Path) -> dict[str, Any]:
    if path is None:
        return {"present": False, "path": "", "size_bytes": 0}
    if not path.exists():
        return {"present": False, "path": rel_to_root(path, root), "size_bytes": 0}
    return {
        "present": path.is_file() or path.is_dir(),
        "path": rel_to_root(path, root),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


def first_existing(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return candidates[0] if candidates else None


def acceptance_manifest(lecture_dir: Path) -> dict[str, Any]:
    path = lecture_dir / "acceptance.json"
    if not path.exists():
        return {}
    try:
        data = load_json(path)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {"_invalid_acceptance_json": True}


def artifact_path_from_manifest(manifest: dict[str, Any], key: str, root: Path) -> Path | None:
    value = manifest.get(key)
    if isinstance(value, str) and value:
        return resolve(root, value)
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        value = artifacts.get(key)
        if isinstance(value, str) and value:
            return resolve(root, value)
    return None


def human_review_decision(manifest: dict[str, Any]) -> dict[str, Any]:
    review = manifest.get("human_review")
    if isinstance(review, dict):
        return review
    review = manifest.get("human_review_decision")
    if isinstance(review, dict):
        return review
    return {}


def acceptance_actions(manifest: dict[str, Any]) -> dict[str, Any]:
    actions = manifest.get("acceptance_actions")
    return actions if isinstance(actions, dict) else {}


def acceptance_content_readiness(
    *,
    root: Path,
    manifest: dict[str, Any],
    artifact_paths: dict[str, Path | None],
    artifacts: dict[str, dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}

    if manifest.get("_invalid_acceptance_json"):
        errors.append("invalid_acceptance_manifest_json")
    if missing:
        errors.extend(f"missing_required_artifact:{key}" for key in missing)

    actions = acceptance_actions(manifest)
    if not actions.get("accept_command"):
        errors.append("missing_acceptance_action:accept_command")
    if not actions.get("blocker_command"):
        errors.append("missing_acceptance_action:blocker_command")

    asr_path = artifact_paths.get("final_mix_asr_report")
    asr_report = load_json_if_exists(asr_path)
    if artifacts["final_mix_asr_report"]["present"]:
        if not asr_report or asr_report.get("_invalid_json"):
            errors.append("invalid_final_mix_asr_report_json")
        else:
            summary = asr_report.get("summary")
            gate = asr_report.get("gate")
            if not isinstance(summary, dict):
                errors.append("final_mix_asr_report_missing_summary")
            else:
                metrics["asr"] = {
                    "mean_similarity": summary.get("mean_similarity"),
                    "weighted_similarity": summary.get("weighted_similarity"),
                    "min_similarity": summary.get("min_similarity"),
                    "low_similarity_count": summary.get("low_similarity_count"),
                    "very_low_similarity_count": summary.get("very_low_similarity_count"),
                }
            if not isinstance(gate, dict) or "pass" not in gate:
                errors.append("final_mix_asr_report_missing_gate")
            elif gate.get("pass") is not True:
                warnings.append("final_mix_asr_gate_not_passed")

    fit_audit_path = artifact_paths.get("fit_audit")
    fit_audit = load_json_if_exists(fit_audit_path)
    if artifacts["fit_audit"]["present"]:
        if not fit_audit or fit_audit.get("_invalid_json"):
            errors.append("invalid_fit_audit_json")
        else:
            fit_summary = fit_audit.get("summary")
            if not isinstance(fit_summary, dict):
                errors.append("fit_audit_missing_summary")
            else:
                metrics["fit_audit"] = {
                    "status": fit_audit.get("status"),
                    "recommendation": fit_audit.get("recommendation"),
                    "selected_fit": fit_summary.get("selected_fit"),
                    "candidate_fit": fit_summary.get("candidate_fit"),
                }
            if fit_audit.get("status") != "pass":
                errors.append(f"fit_audit_not_pass:{fit_audit.get('status') or 'unknown'}")

    risk_path = artifact_paths.get("risk_queue")
    risk_queue = load_json_if_exists(risk_path)
    if artifacts["risk_queue"]["present"]:
        if not risk_queue or risk_queue.get("_invalid_json"):
            errors.append("invalid_risk_queue_json")
        else:
            entries = risk_queue.get("entries")
            summary = risk_queue.get("summary")
            if not isinstance(entries, list):
                errors.append("risk_queue_missing_entries")
            if not isinstance(summary, dict):
                errors.append("risk_queue_missing_summary")
            else:
                metrics["risk_queue"] = {
                    "total_entries": summary.get("total_entries"),
                    "by_priority": summary.get("by_priority"),
                    "by_source": summary.get("by_source"),
                }

    review_pack_path = artifact_paths.get("human_review_pack")
    review_manifest_path = review_pack_path.parent / "review_manifest.json" if review_pack_path else None
    review_manifest = load_json_if_exists(review_manifest_path)
    if artifacts["human_review_pack"]["present"]:
        if not review_manifest or review_manifest.get("_invalid_json"):
            errors.append("missing_or_invalid_review_manifest")
        else:
            rows = review_manifest.get("review_rows")
            if not isinstance(rows, list):
                errors.append("review_manifest_missing_review_rows")
            else:
                metrics["review_pack"] = {"review_rows": len(rows)}
                if not rows:
                    warnings.append("review_pack_has_no_focused_rows")
            review_actions = review_manifest.get("acceptance_actions")
            if not isinstance(review_actions, dict) or not review_actions.get("accept_command"):
                errors.append("review_manifest_missing_acceptance_actions")

    provenance_path = artifact_paths.get("adapter_profile_provenance")
    provenance = load_json_if_exists(provenance_path)
    if artifacts["adapter_profile_provenance"]["present"]:
        if not provenance or provenance.get("_invalid_json"):
            errors.append("invalid_adapter_profile_provenance_json")
        else:
            runtime = provenance.get("runtime")
            if not isinstance(runtime, dict):
                errors.append("adapter_profile_provenance_missing_runtime")
            else:
                runtime_summary = runtime.get("summary")
                if isinstance(runtime_summary, dict):
                    metrics["adapter_profile"] = {
                        "candidate_audio_segments": runtime_summary.get("candidate_audio_segments"),
                        "fallback_segments": runtime_summary.get("fallback_segments"),
                        "selected_family_counts": runtime_summary.get("selected_family_counts"),
                    }
                else:
                    warnings.append("adapter_profile_provenance_missing_runtime_summary")

    return {
        "schema_version": "holodub.mit6824.acceptance_readiness.v1",
        "required_files_present": not missing,
        "ready_for_human_review": not errors,
        "content_errors": errors,
        "content_warnings": warnings,
        "metrics": metrics,
    }


def lecture_acceptance(row: dict[str, Any], *, root: Path, acceptance_root: Path) -> dict[str, Any]:
    lecture = int(row["lecture"])
    lecture_dir = acceptance_root / f"lec-{lecture:02d}"
    episodes = row.get("episodes") or []
    latest = latest_episode(episodes)
    completed = latest_completed_episode(episodes)
    latest_incomplete = bool(latest and latest.get("status") != "completed")
    manifest = {} if latest_incomplete else acceptance_manifest(lecture_dir)

    final_video_path = artifact_path_from_manifest(manifest, "final_video", root)
    if final_video_path is None and completed and not latest_incomplete:
        final_video_path = resolve(root / "data", completed.get("output_rel_path"))

    final_audio_path = artifact_path_from_manifest(manifest, "final_audio", root)
    if final_audio_path is None and not latest_incomplete:
        final_audio_path = first_existing(
            [
                lecture_dir / "final_mix_audio.wav",
                lecture_dir / "final_audio.wav",
            ]
        )

    asr_report_path = artifact_path_from_manifest(manifest, "final_mix_asr_report", root)
    if asr_report_path is None and not latest_incomplete:
        asr_report_path = first_existing(
            [
                lecture_dir / "final_mix_asr_report.json",
                lecture_dir / "final_mix_asr_audit" / "final_mix_asr_report.json",
            ]
        )

    risk_queue_path = artifact_path_from_manifest(manifest, "risk_queue", root)
    if risk_queue_path is None and not latest_incomplete:
        risk_queue_path = first_existing([lecture_dir / "risk_queue.json"])

    fit_audit_path = artifact_path_from_manifest(manifest, "fit_audit", root)
    if fit_audit_path is None and not latest_incomplete:
        fit_audit_path = first_existing([lecture_dir / "fit_audit.json"])

    review_pack_path = artifact_path_from_manifest(manifest, "human_review_pack", root)
    if review_pack_path is None and not latest_incomplete:
        review_pack_path = first_existing(
            [
                lecture_dir / "human_review_pack" / "index.html",
                lecture_dir / "review_pack" / "index.html",
            ]
        )

    provenance_path = artifact_path_from_manifest(manifest, "adapter_profile_provenance", root)
    if provenance_path is None and not latest_incomplete:
        provenance_path = first_existing([lecture_dir / "adapter_profile_provenance.json", lecture_dir / "provenance.json"])

    artifacts = {
        "final_video": file_status(final_video_path, root),
        "final_audio": file_status(final_audio_path, root),
        "final_mix_asr_report": file_status(asr_report_path, root),
        "fit_audit": file_status(fit_audit_path, root),
        "risk_queue": file_status(risk_queue_path, root),
        "human_review_pack": file_status(review_pack_path, root),
        "adapter_profile_provenance": file_status(provenance_path, root),
    }
    artifact_paths = {
        "final_video": final_video_path,
        "final_audio": final_audio_path,
        "final_mix_asr_report": asr_report_path,
        "fit_audit": fit_audit_path,
        "risk_queue": risk_queue_path,
        "human_review_pack": review_pack_path,
        "adapter_profile_provenance": provenance_path,
    }
    required = list(artifacts)
    missing = [key for key in required if not artifacts[key]["present"]]
    readiness = acceptance_content_readiness(
        root=root,
        manifest=manifest,
        artifact_paths=artifact_paths,
        artifacts=artifacts,
        missing=missing,
    )
    human_accepted = bool(manifest.get("human_accepted"))
    accepted_at = manifest.get("accepted_at") if isinstance(manifest.get("accepted_at"), str) else ""
    review_decision = human_review_decision(manifest)
    review_status = str(review_decision.get("status") or "")

    if row.get("status") == "not_started":
        status = "not_started"
    elif latest and latest.get("status") not in {"completed"}:
        status = "in_progress"
    elif missing:
        status = "missing_acceptance_artifacts"
    elif review_status in {"blocked", "needs_repair", "rejected"} and not human_accepted:
        status = "review_blocked"
    elif not human_accepted:
        status = "awaiting_human_acceptance"
    else:
        status = "accepted"

    return {
        "lecture": lecture,
        "title": row.get("title") or "",
        "status": status,
        "course_tracker_status": row.get("status"),
        "latest_episode": latest,
        "completed_episode": completed,
        "acceptance_dir": rel_to_root(lecture_dir, root),
        "acceptance_manifest": file_status(lecture_dir / "acceptance.json", root),
        "human_accepted": human_accepted,
        "accepted_at": accepted_at,
        "human_review": review_decision,
        "acceptance_actions": acceptance_actions(manifest),
        "acceptance_readiness": readiness,
        "missing": missing,
        "artifacts": artifacts,
    }


def build_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    tracker_path = resolve(root, args.tracker)
    if tracker_path is None:
        raise ValueError("tracker path is required")
    tracker = load_json(tracker_path)
    acceptance_root = resolve(root, args.acceptance_root)
    if acceptance_root is None:
        raise ValueError("acceptance root is required")
    lectures = [
        lecture_acceptance(row, root=root, acceptance_root=acceptance_root)
        for row in tracker.get("lectures", [])
    ]
    summary: dict[str, Any] = {"total": len(lectures), "by_status": {}}
    for row in lectures:
        status = row["status"]
        summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
    summary["accepted"] = [row["lecture"] for row in lectures if row["status"] == "accepted"]
    summary["ready_for_review"] = [row["lecture"] for row in lectures if row["status"] == "awaiting_human_acceptance"]
    summary["review_blocked"] = [row["lecture"] for row in lectures if row["status"] == "review_blocked"]
    summary["not_started"] = [row["lecture"] for row in lectures if row["status"] == "not_started"]
    summary["content_ready_for_review"] = [
        row["lecture"]
        for row in lectures
        if (row.get("acceptance_readiness") or {}).get("ready_for_human_review") is True
    ]
    summary["content_readiness_blocked"] = [
        row["lecture"]
        for row in lectures
        if row.get("status") not in {"not_started", "in_progress"}
        and (row.get("acceptance_readiness") or {}).get("ready_for_human_review") is False
    ]
    return {
        "schema_version": "holodub.mit6824_acceptance_status.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tracker": rel_to_root(tracker_path, root),
        "acceptance_root": rel_to_root(acceptance_root, root),
        "summary": summary,
        "lectures": lectures,
    }


def write_markdown(path: Path, status: dict[str, Any]) -> None:
    lines = [
        "# MIT 6.824 Acceptance Status",
        "",
        f"Generated: `{status['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Status counts: `{json.dumps(status['summary']['by_status'], ensure_ascii=False, sort_keys=True)}`",
        f"- Accepted: `{status['summary']['accepted']}`",
        f"- Ready for review: `{status['summary']['ready_for_review']}`",
        f"- Content ready for review: `{status['summary'].get('content_ready_for_review', [])}`",
        f"- Content readiness blocked: `{status['summary'].get('content_readiness_blocked', [])}`",
        f"- Review blocked: `{status['summary']['review_blocked']}`",
        f"- Not started: `{status['summary']['not_started']}`",
        "",
        "## Lectures",
        "",
        "| Lecture | Title | Status | Readiness | Missing | Final Video | Review Pack |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in status["lectures"]:
        artifacts = row["artifacts"]
        readiness = row.get("acceptance_readiness") if isinstance(row.get("acceptance_readiness"), dict) else {}
        if row.get("status") in {"not_started", "in_progress"}:
            readiness_label = "n/a"
        else:
            readiness_label = "ready" if readiness.get("ready_for_human_review") is True else "blocked"
        lines.append(
            "| {lecture} | {title} | {status} | {readiness} | {missing} | {final_video} | {review_pack} |".format(
                lecture=row["lecture"],
                title=(row.get("title") or "").replace("|", "\\|"),
                status=row["status"],
                readiness=readiness_label,
                missing=", ".join(row["missing"]),
                final_video=artifacts["final_video"]["path"],
                review_pack=artifacts["human_review_pack"]["path"],
            )
        )
    readiness_rows = [
        row
        for row in status["lectures"]
        if isinstance(row.get("acceptance_readiness"), dict)
        and (
            row["acceptance_readiness"].get("content_errors")
            or row["acceptance_readiness"].get("content_warnings")
        )
    ]
    if readiness_rows:
        lines.extend(["", "## Content Readiness Issues", ""])
        for row in readiness_rows:
            readiness = row["acceptance_readiness"]
            lines.append(f"### Lecture {row['lecture']}: {row.get('title') or ''}")
            errors = readiness.get("content_errors") or []
            warnings = readiness.get("content_warnings") or []
            lines.append(f"- Errors: `{json.dumps(errors, ensure_ascii=False)}`")
            lines.append(f"- Warnings: `{json.dumps(warnings, ensure_ascii=False)}`")
    ready_actions = [row for row in status["lectures"] if row.get("acceptance_actions")]
    if ready_actions:
        lines.extend(["", "## Acceptance Actions", ""])
        for row in ready_actions:
            actions = row.get("acceptance_actions") or {}
            if not isinstance(actions, dict):
                continue
            lines.extend(
                [
                    f"### Lecture {row['lecture']}: {row.get('title') or ''}",
                    "",
                    f"Dry-run report: `{actions.get('accept_dry_run_report') or ''}`",
                    "",
                    "Dry-run accept rehearsal:",
                    "",
                    "```powershell",
                    str(actions.get("accept_dry_run_command") or ""),
                    "```",
                    "",
                    "Accept and advance:",
                    "",
                    "```powershell",
                    str(actions.get("accept_command") or ""),
                    "```",
                    "",
                    "Record blocker:",
                    "",
                    "```powershell",
                    str(actions.get("blocker_command") or ""),
                    "```",
                    "",
                ]
            )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    status = build_status(args, root)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths are required")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(output_md, status)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "summary": status["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
