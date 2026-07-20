from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
    parser = argparse.ArgumentParser(description="Record MIT 6.824 lecture review blockers without accepting the lecture.")
    parser.add_argument("--lecture", type=int, required=True)
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument("--reviewer", default="user")
    parser.add_argument("--notes", default="")
    parser.add_argument("--blocker", action="append", default=[], help="A review blocker note. Repeatable.")
    parser.add_argument("--refresh-status", action="store_true")
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--acceptance-status-md", default="data/validation/mit6824_acceptance_status.md")
    parser.add_argument("--course-advance-status", default="data/validation/mit6824_course_advance_status.json")
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def blocker_rows(blockers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for index, raw in enumerate(blockers, start=1):
        text = str(raw or "").strip()
        if not text:
            continue
        rows.append({"id": index, "description": text})
    return rows


def record_blockers(
    *,
    root: Path,
    acceptance_root: Path,
    lecture: int,
    reviewer: str,
    notes: str,
    blockers: list[str],
) -> dict[str, Any]:
    lecture_dir = acceptance_root / f"lec-{lecture:02d}"
    manifest_path = lecture_dir / "acceptance.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"acceptance manifest not found: {manifest_path}")
    rows = blocker_rows(blockers)
    if not rows and not notes.strip():
        raise ValueError("at least one --blocker or --notes value is required")
    manifest = read_json(manifest_path)
    statuses = artifact_status(root, manifest)
    paths = artifact_paths(root, manifest)
    missing = [key for key, status in statuses.items() if not status["present"]]
    readiness = acceptance_content_readiness(
        root=root,
        manifest=manifest,
        artifact_paths=paths,
        artifacts=statuses,
        missing=missing,
    )
    recorded_at = datetime.now(timezone.utc).isoformat()
    manifest["human_accepted"] = False
    manifest["accepted_at"] = ""
    manifest["human_review"] = {
        "schema_version": "holodub.mit6824.human_review_decision.v1",
        "status": "blocked",
        "recorded_at": recorded_at,
        "reviewer": reviewer,
        "notes": notes,
        "blockers": rows,
        "artifact_snapshot": statuses,
        "risk_queue_summary": load_risk_summary(root, manifest),
        "acceptance_readiness_snapshot": readiness,
    }
    write_json(manifest_path, manifest)
    return {
        "status": "review_blocked",
        "lecture": lecture,
        "acceptance_manifest": rel_to_root(manifest_path, root),
        "recorded_at": recorded_at,
        "blocker_count": len(rows),
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


def main() -> int:
    args = parse_args()
    root = repo_root()
    acceptance_root = resolve(root, args.acceptance_root)
    if acceptance_root is None:
        raise ValueError("acceptance root required")
    result = record_blockers(
        root=root,
        acceptance_root=acceptance_root,
        lecture=args.lecture,
        reviewer=args.reviewer,
        notes=args.notes,
        blockers=args.blocker or [],
    )
    if args.refresh_status:
        result["refresh_status"] = refresh_status(args, root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    refresh = result.get("refresh_status") if isinstance(result.get("refresh_status"), dict) else {}
    if refresh and refresh.get("returncode") != 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
