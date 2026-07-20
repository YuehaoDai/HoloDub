from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mit6824_acceptance_status import acceptance_content_readiness


REQUIRED_ARTIFACTS = [
    "final_video",
    "final_audio",
    "final_mix_asr_report",
    "fit_audit",
    "risk_queue",
    "human_review_pack",
    "adapter_profile_provenance",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark one MIT 6.824 lecture acceptance manifest as human accepted.")
    parser.add_argument("--lecture", type=int, required=True)
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument("--reviewer", default="user")
    parser.add_argument("--notes", default="")
    parser.add_argument("--risk-anchor", action="append", default=[], help="Accepted risk anchor note. Repeatable.")
    parser.add_argument("--allow-missing-artifacts", action="store_true")
    parser.add_argument(
        "--allow-content-errors",
        action="store_true",
        help="Accept even when artifact content readiness checks fail. Intended only for explicit manual override.",
    )
    parser.add_argument("--force", action="store_true", help="Update accepted_at even if already accepted.")
    parser.add_argument("--dry-run", action="store_true", help="Validate acceptance and preview next action without writing.")
    parser.add_argument(
        "--dry-run-output",
        default="",
        help="Optional JSON report path for --dry-run. This writes an audit report only, not acceptance state.",
    )
    parser.add_argument("--refresh-status", action="store_true")
    parser.add_argument(
        "--auto-advance-next",
        action="store_true",
        help="After acceptance, run the safe course advance action to start the next lecture and spawn its finalizer.",
    )
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--tracker-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--acceptance-status-md", default="data/validation/mit6824_acceptance_status.md")
    parser.add_argument("--course-advance-status", default="data/validation/mit6824_course_advance_status.json")
    parser.add_argument("--course-advance-history", default="data/validation/mit6824_course_advance_history.jsonl")
    parser.add_argument("--python", default=sys.executable)
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


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def read_json_if_exists(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return read_json(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_json_output(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                decoded = json.loads(raw[start : end + 1])
                return decoded if isinstance(decoded, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def artifact_value(manifest: dict[str, Any], key: str) -> str:
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
        return artifacts[key]
    value = manifest.get(key)
    return value if isinstance(value, str) else ""


def artifact_status(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for key in REQUIRED_ARTIFACTS:
        raw = artifact_value(manifest, key)
        path = resolve(root, raw)
        present = bool(path and path.exists())
        statuses[key] = {
            "path": rel_to_root(path, root) if path else raw,
            "present": present,
            "size_bytes": path.stat().st_size if path and path.is_file() else 0,
        }
    return statuses


def artifact_paths(root: Path, manifest: dict[str, Any]) -> dict[str, Path | None]:
    return {key: resolve(root, artifact_value(manifest, key)) for key in REQUIRED_ARTIFACTS}


def load_risk_summary(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    risk_path = resolve(root, artifact_value(manifest, "risk_queue"))
    if risk_path is None or not risk_path.exists():
        return {}
    try:
        payload = read_json(risk_path)
    except (json.JSONDecodeError, ValueError):
        return {"invalid_risk_queue": rel_to_root(risk_path, root)}
    summary = payload.get("summary")
    if isinstance(summary, dict):
        return summary
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    return {"total_entries": len(entries)}


def accept_manifest(
    *,
    root: Path,
    acceptance_root: Path,
    lecture: int,
    reviewer: str,
    notes: str,
    risk_anchors: list[str],
    allow_missing_artifacts: bool,
    allow_content_errors: bool = False,
    force: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    lecture_dir = acceptance_root / f"lec-{lecture:02d}"
    manifest_path = lecture_dir / "acceptance.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"acceptance manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    statuses = artifact_status(root, manifest)
    paths = artifact_paths(root, manifest)
    missing = [key for key, status in statuses.items() if not status["present"]]
    if missing and not allow_missing_artifacts:
        return {
            "status": "missing_artifacts",
            "lecture": lecture,
            "acceptance_manifest": rel_to_root(manifest_path, root),
            "missing": missing,
            "artifacts": statuses,
        }
    readiness = acceptance_content_readiness(
        root=root,
        manifest=manifest,
        artifact_paths=paths,
        artifacts=statuses,
        missing=missing,
    )
    already_accepted = bool(manifest.get("human_accepted"))
    if already_accepted and not force:
        return {
            "status": "already_accepted",
            "lecture": lecture,
            "acceptance_manifest": rel_to_root(manifest_path, root),
            "accepted_at": manifest.get("accepted_at", ""),
            "artifacts": statuses,
            "acceptance_readiness": readiness,
        }
    if not readiness.get("ready_for_human_review") and not allow_content_errors:
        return {
            "status": "content_readiness_failed",
            "lecture": lecture,
            "acceptance_manifest": rel_to_root(manifest_path, root),
            "missing": missing,
            "artifacts": statuses,
            "acceptance_readiness": readiness,
        }
    accepted_at = datetime.now(timezone.utc).isoformat()
    if dry_run:
        return {
            "status": "dry_run_acceptance_ready",
            "lecture": lecture,
            "acceptance_manifest": rel_to_root(manifest_path, root),
            "would_accept_at": accepted_at,
            "would_write_manifest": True,
            "missing": missing,
            "artifacts": statuses,
            "risk_queue_summary": load_risk_summary(root, manifest),
            "acceptance_readiness": readiness,
        }
    manifest["human_accepted"] = True
    manifest["accepted_at"] = accepted_at
    manifest["human_acceptance"] = {
        "schema_version": "holodub.mit6824.human_acceptance.v1",
        "accepted_at": accepted_at,
        "reviewer": reviewer,
        "notes": notes,
        "accepted_risk_anchors": risk_anchors,
        "artifact_snapshot": statuses,
        "risk_queue_summary": load_risk_summary(root, manifest),
        "allow_missing_artifacts": allow_missing_artifacts,
        "allow_content_errors": allow_content_errors,
        "acceptance_readiness_snapshot": readiness,
    }
    write_json(manifest_path, manifest)
    return {
        "status": "accepted",
        "lecture": lecture,
        "acceptance_manifest": rel_to_root(manifest_path, root),
        "accepted_at": accepted_at,
        "missing": missing,
        "artifacts": statuses,
        "acceptance_readiness": readiness,
    }


def refresh_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
        args.python,
        "tools/mit6824_status_refresh.py",
        "--tracker",
        args.tracker,
        "--acceptance-status",
        args.acceptance_status,
        "--acceptance-status-md",
        args.acceptance_status_md,
        "--course-advance-status",
        args.course_advance_status,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    return {"command": cmd, "returncode": proc.returncode, "stdout_tail": proc.stdout[-2000:]}


def run_course_advance(args: argparse.Namespace, root: Path, *, execute: bool = True, refresh: bool = True) -> dict[str, Any]:
    cmd = [
        args.python,
        "tools/mit6824_course_advance.py",
        "--tracker",
        args.tracker,
        "--tracker-md",
        args.tracker_md,
        "--acceptance-root",
        args.acceptance_root,
        "--acceptance-status",
        args.acceptance_status,
        "--acceptance-status-md",
        args.acceptance_status_md,
        "--output",
        args.course_advance_status,
        "--history-jsonl",
        args.course_advance_history,
    ]
    if refresh:
        cmd.insert(2, "--refresh")
    if execute:
        cmd.extend(["--execute", "--spawn-finalizer"])
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "json": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-4000:],
    }


def simulate_acceptance_status_for_preview(
    *,
    args: argparse.Namespace,
    root: Path,
    output_path: Path,
    acceptance_result: dict[str, Any],
) -> dict[str, Any]:
    source_path = resolve(root, args.acceptance_status)
    status = copy.deepcopy(read_json_if_exists(source_path))
    if not isinstance(status.get("lectures"), list):
        return {
            "created": False,
            "reason": "acceptance_status_missing_or_invalid",
            "source": rel_to_root(source_path, root),
        }
    lecture = int(acceptance_result.get("lecture") or 0)
    simulated_at = str(acceptance_result.get("would_accept_at") or "")
    updated = False
    for row in status["lectures"]:
        if not isinstance(row, dict) or int(row.get("lecture") or 0) != lecture:
            continue
        row["status"] = "accepted"
        row["human_accepted"] = True
        row["accepted_at"] = simulated_at
        row["human_review"] = {}
        row["human_acceptance_preview"] = {
            "schema_version": "holodub.mit6824.human_acceptance_preview.v1",
            "status": "simulated_accepted_for_dry_run",
            "accepted_at": simulated_at,
            "source_acceptance_manifest": acceptance_result.get("acceptance_manifest", ""),
            "acceptance_readiness_snapshot": acceptance_result.get("acceptance_readiness", {}),
        }
        updated = True
        break
    if not updated:
        return {
            "created": False,
            "reason": f"lecture_{lecture}_not_found",
            "source": rel_to_root(source_path, root),
        }
    lectures = [row for row in status.get("lectures") or [] if isinstance(row, dict)]
    by_status: dict[str, int] = {}
    for row in lectures:
        row_status = str(row.get("status") or "unknown")
        by_status[row_status] = by_status.get(row_status, 0) + 1
    status["summary"] = {
        "total": len(lectures),
        "by_status": by_status,
        "accepted": [int(row.get("lecture") or 0) for row in lectures if row.get("status") == "accepted"],
        "ready_for_review": [int(row.get("lecture") or 0) for row in lectures if row.get("status") == "awaiting_human_acceptance"],
        "review_blocked": [int(row.get("lecture") or 0) for row in lectures if row.get("status") == "review_blocked"],
        "not_started": [int(row.get("lecture") or 0) for row in lectures if row.get("status") == "not_started"],
        "content_ready_for_review": [
            int(row.get("lecture") or 0)
            for row in lectures
            if (row.get("acceptance_readiness") or {}).get("ready_for_human_review") is True
        ],
        "content_readiness_blocked": [
            int(row.get("lecture") or 0)
            for row in lectures
            if row.get("status") not in {"not_started", "in_progress"}
            and (row.get("acceptance_readiness") or {}).get("ready_for_human_review") is False
        ],
    }
    status["dry_run_preview"] = {
        "schema_version": "holodub.mit6824.acceptance_status_preview.v1",
        "simulated_accepted_lecture": lecture,
        "source_acceptance_status": rel_to_root(source_path, root),
    }
    write_json(output_path, status)
    return {
        "created": True,
        "source": rel_to_root(source_path, root),
        "path": str(output_path),
        "simulated_accepted_lecture": lecture,
    }


def run_course_advance_preview(args: argparse.Namespace, root: Path, acceptance_result: dict[str, Any] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mit6824_accept_preview_") as tmp_raw:
        tmp = Path(tmp_raw)
        preview_args = argparse.Namespace(**vars(args))
        simulated_status = tmp / "acceptance_status_preview.json"
        simulation: dict[str, Any] = {}
        if acceptance_result is not None:
            simulation = simulate_acceptance_status_for_preview(
                args=args,
                root=root,
                output_path=simulated_status,
                acceptance_result=acceptance_result,
            )
            if simulation.get("created"):
                preview_args.acceptance_status = str(simulated_status)
        preview_args.course_advance_status = str(tmp / "course_advance_preview.json")
        preview_args.course_advance_history = str(tmp / "course_advance_preview.jsonl")
        result = run_course_advance(preview_args, root, execute=False, refresh=False)
        output_path = Path(preview_args.course_advance_status)
        if output_path.exists():
            try:
                result["preview"] = read_json(output_path)
            except (json.JSONDecodeError, ValueError):
                result["preview"] = {"_invalid_json": str(output_path)}
        preview = result.get("preview") if isinstance(result.get("preview"), dict) else {}
        next_action = preview.get("next_action") if isinstance(preview.get("next_action"), dict) else {}
        if next_action:
            result["post_acceptance_next_action"] = next_action
        if isinstance(next_action.get("after_acceptance"), dict):
            result["after_acceptance_preview"] = next_action["after_acceptance"]
        elif next_action:
            result["after_acceptance_preview"] = next_action
        if simulation:
            result["acceptance_status_simulation"] = {
                key: value for key, value in simulation.items() if key != "path"
            }
        result["preview_storage"] = {
            "ephemeral": True,
            "persisted": False,
            "reason": "dry-run preview is returned inline and temporary files are deleted after command completion",
        }
        return result


def dry_run_report(args: argparse.Namespace, root: Path, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "holodub.mit6824.acceptance_dry_run_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_role": "acceptance_dry_run_audit_report",
        "not_acceptance_state": True,
        "side_effect_contract": {
            "acceptance_manifest_written": False,
            "human_accepted_changed": False,
            "course_advance_executed": False,
            "next_lecture_started": False,
            "finalizer_spawned": False,
        },
        "command_config": {
            "lecture": args.lecture,
            "acceptance_root": args.acceptance_root,
            "reviewer": args.reviewer,
            "notes": args.notes,
            "risk_anchor_count": len(args.risk_anchor or []),
            "allow_missing_artifacts": bool(args.allow_missing_artifacts),
            "allow_content_errors": bool(args.allow_content_errors),
            "auto_advance_next": bool(args.auto_advance_next),
            "tracker": args.tracker,
            "acceptance_status": args.acceptance_status,
            "course_advance_status": args.course_advance_status,
        },
        "result": result,
        "repo_root": str(root),
    }


def maybe_write_dry_run_output(args: argparse.Namespace, root: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    raw = str(getattr(args, "dry_run_output", "") or "")
    if not raw:
        return None
    output = resolve(root, raw)
    if output is None:
        raise ValueError("--dry-run-output path is invalid")
    report = dry_run_report(args, root, result)
    write_json(output, report)
    return {
        "path": rel_to_root(output, root),
        "schema_version": report["schema_version"],
        "not_acceptance_state": True,
        "persisted": True,
    }


def maybe_auto_advance_next(args: argparse.Namespace, root: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    if not getattr(args, "auto_advance_next", False):
        return None
    if getattr(args, "dry_run", False):
        if result.get("status") != "dry_run_acceptance_ready":
            return {"skipped": True, "reason": f"dry_run_acceptance_status:{result.get('status')}"}
        return run_course_advance_preview(args, root, result)
    if result.get("status") not in {"accepted", "already_accepted"}:
        return {"skipped": True, "reason": f"acceptance_status:{result.get('status')}"}
    return run_course_advance(args, root, execute=True, refresh=True)


def main() -> int:
    args = parse_args()
    root = repo_root()
    acceptance_root = resolve(root, args.acceptance_root)
    if acceptance_root is None:
        raise ValueError("acceptance root required")
    if getattr(args, "dry_run_output", "") and not getattr(args, "dry_run", False):
        raise ValueError("--dry-run-output requires --dry-run")
    result = accept_manifest(
        root=root,
        acceptance_root=acceptance_root,
        lecture=args.lecture,
        reviewer=args.reviewer,
        notes=args.notes,
        risk_anchors=args.risk_anchor or [],
        allow_missing_artifacts=args.allow_missing_artifacts,
        allow_content_errors=args.allow_content_errors,
        force=args.force,
        dry_run=getattr(args, "dry_run", False),
    )
    auto_advance = maybe_auto_advance_next(args, root, result)
    if auto_advance is not None:
        result["auto_advance_next"] = auto_advance
    if args.refresh_status and result.get("status") in {"accepted", "already_accepted"} and not getattr(args, "dry_run", False):
        result["refresh_status"] = refresh_status(args, root)
    dry_run_output = maybe_write_dry_run_output(args, root, result) if getattr(args, "dry_run", False) else None
    if dry_run_output is not None:
        result["dry_run_output"] = dry_run_output
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") not in {"accepted", "already_accepted", "dry_run_acceptance_ready"}:
        return 2
    if auto_advance is not None and auto_advance.get("returncode") not in {None, 0}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
