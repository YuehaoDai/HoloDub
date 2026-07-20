from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACCEPT_REPORT_SCHEMA = "holodub.mit6824.acceptance_dry_run_report.v1"
START_REPORT_SCHEMA = "holodub.mit6824.start_dry_run_report.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether a MIT 6.824 lecture acceptance gate has current dry-run evidence."
    )
    parser.add_argument("--lecture", type=int, default=0, help="Lecture to check. Defaults to first awaiting acceptance.")
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_acceptance_gate_check.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_acceptance_gate_check.md")
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


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"_missing": True, "_path": str(path) if path else ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"_invalid_json": True, "_path": str(path), "_error": str(exc)}
    return payload if isinstance(payload, dict) else {"_invalid_json": True, "_path": str(path), "_error": "not an object"}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def lecture_rows(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for row in payload.get("lectures") or []:
        if not isinstance(row, dict):
            continue
        try:
            lecture = int(row.get("lecture") or 0)
        except (TypeError, ValueError):
            continue
        if lecture:
            rows[lecture] = row
    return rows


def first_awaiting_acceptance(status: dict[str, Any]) -> int | None:
    for row in status.get("lectures") or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "awaiting_human_acceptance":
            continue
        try:
            return int(row.get("lecture") or 0)
        except (TypeError, ValueError):
            return None
    return None


def normalize_path(raw: Any) -> str:
    return str(raw or "").replace("\\", "/")


def issue(issues: list[dict[str, Any]], severity: str, code: str, detail: str = "") -> None:
    issues.append({"severity": severity, "code": code, "detail": detail})


def bool_is_false(value: Any) -> bool:
    return value is False


def side_effects_are_false(report: dict[str, Any], expected_keys: list[str], issues: list[dict[str, Any]]) -> None:
    contract = report.get("side_effect_contract") if isinstance(report.get("side_effect_contract"), dict) else {}
    for key in expected_keys:
        if not bool_is_false(contract.get(key)):
            issue(issues, "hard", f"side_effect_not_false:{key}", str(contract.get(key)))


def build_gate(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    acceptance_status_path = resolve(root, args.acceptance_status)
    tracker_path = resolve(root, args.tracker)
    acceptance_root = resolve(root, args.acceptance_root) or root / "data" / "validation" / "mit6824_acceptance"
    acceptance_status = read_json(acceptance_status_path)
    tracker = read_json(tracker_path)
    selected_lecture = args.lecture or first_awaiting_acceptance(acceptance_status)

    if not selected_lecture:
        issues: list[dict[str, Any]] = []
        for label, payload in (("acceptance_status", acceptance_status), ("tracker", tracker)):
            if payload.get("_missing"):
                issue(issues, "hard", f"missing:{label}", str(payload.get("_path") or ""))
            if payload.get("_invalid_json"):
                issue(issues, "hard", f"invalid_json:{label}", str(payload.get("_error") or ""))
        hard_count = sum(1 for row in issues if row.get("severity") == "hard")
        return {
            "schema_version": "holodub.mit6824.acceptance_gate_check.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "fail" if hard_count else "no_pending_acceptance",
            "pass": hard_count == 0,
            "lecture": None,
            "issues": issues,
            "summary": {
                "hard_issue_count": hard_count,
                "warning_count": sum(1 for row in issues if row.get("severity") == "warning"),
                "issue_codes": [str(row.get("code")) for row in issues],
            },
        }

    lecture = int(selected_lecture)
    next_lecture = lecture + 1
    lecture_dir = acceptance_root / f"lec-{lecture:02d}"
    acceptance_manifest_path = lecture_dir / "acceptance.json"
    accept_report_path = lecture_dir / "acceptance_dry_run_report.json"
    acceptance_manifest = read_json(acceptance_manifest_path)
    accept_report = read_json(accept_report_path)
    acceptance_rows = lecture_rows(acceptance_status)
    tracker_rows = lecture_rows(tracker)
    current_acceptance = acceptance_rows.get(lecture, {})
    next_tracker = tracker_rows.get(next_lecture, {})
    issues: list[dict[str, Any]] = []

    for label, payload in (
        ("acceptance_status", acceptance_status),
        ("tracker", tracker),
        ("acceptance_manifest", acceptance_manifest),
        ("acceptance_dry_run_report", accept_report),
    ):
        if payload.get("_missing"):
            issue(issues, "hard", f"missing:{label}", str(payload.get("_path") or ""))
        if payload.get("_invalid_json"):
            issue(issues, "hard", f"invalid_json:{label}", str(payload.get("_error") or ""))

    if current_acceptance.get("status") != "awaiting_human_acceptance":
        issue(issues, "hard", "lecture_not_awaiting_human_acceptance", str(current_acceptance.get("status") or ""))
    readiness = current_acceptance.get("acceptance_readiness")
    if isinstance(readiness, dict) and readiness.get("ready_for_human_review") is not True:
        issue(issues, "hard", "acceptance_readiness_not_ready", str(readiness.get("content_errors") or ""))
    if acceptance_manifest.get("human_accepted") is not False:
        issue(issues, "hard", "acceptance_manifest_not_unaccepted", str(acceptance_manifest.get("human_accepted")))
    if "human_acceptance" in acceptance_manifest:
        issue(issues, "hard", "acceptance_manifest_has_human_acceptance", "")

    if accept_report.get("schema_version") != ACCEPT_REPORT_SCHEMA:
        issue(issues, "hard", "acceptance_dry_run_schema_mismatch", str(accept_report.get("schema_version") or ""))
    if accept_report.get("not_acceptance_state") is not True:
        issue(issues, "hard", "acceptance_dry_run_not_marked_non_state", "")
    side_effects_are_false(
        accept_report,
        [
            "acceptance_manifest_written",
            "course_advance_executed",
            "finalizer_spawned",
            "human_accepted_changed",
            "next_lecture_started",
        ],
        issues,
    )
    accept_result = accept_report.get("result") if isinstance(accept_report.get("result"), dict) else {}
    if accept_result.get("status") != "dry_run_acceptance_ready":
        issue(issues, "hard", "acceptance_dry_run_not_ready", str(accept_result.get("status") or ""))
    if normalize_path(accept_result.get("acceptance_manifest")) and normalize_path(
        accept_result.get("acceptance_manifest")
    ) != normalize_path(rel_to_root(acceptance_manifest_path, root)):
        issue(issues, "warning", "acceptance_manifest_path_differs", str(accept_result.get("acceptance_manifest")))

    post = (
        accept_result.get("auto_advance_next", {}).get("post_acceptance_next_action")
        if isinstance(accept_result.get("auto_advance_next"), dict)
        else {}
    )
    post = post if isinstance(post, dict) else {}
    if post.get("status") != "ready_to_start":
        issue(issues, "hard", "post_acceptance_not_ready_to_start", str(post.get("status") or ""))
    if post.get("lecture") != next_lecture:
        issue(issues, "hard", "post_acceptance_next_lecture_mismatch", str(post.get("lecture") or ""))
    source_preflight = post.get("source_preflight") if isinstance(post.get("source_preflight"), dict) else {}
    if source_preflight.get("ready") is not True:
        issue(issues, "hard", "post_acceptance_source_preflight_not_ready", str(source_preflight.get("errors") or ""))
    start_report_rel = str(post.get("start_dry_run_report") or "")
    if not start_report_rel:
        issue(issues, "hard", "missing_start_dry_run_report_reference", "")
        start_report_path = acceptance_root / f"lec-{next_lecture:02d}" / "start_dry_run_report.json"
    else:
        start_report_path = resolve(root, start_report_rel) or (acceptance_root / f"lec-{next_lecture:02d}" / "start_dry_run_report.json")
    start_report = read_json(start_report_path)

    if start_report.get("_missing"):
        issue(issues, "hard", "missing:start_dry_run_report", str(start_report.get("_path") or ""))
    if start_report.get("_invalid_json"):
        issue(issues, "hard", "invalid_json:start_dry_run_report", str(start_report.get("_error") or ""))
    if start_report.get("schema_version") != START_REPORT_SCHEMA:
        issue(issues, "hard", "start_dry_run_schema_mismatch", str(start_report.get("schema_version") or ""))
    if start_report.get("not_start_state") is not True:
        issue(issues, "hard", "start_dry_run_not_marked_non_state", "")
    side_effects_are_false(start_report, ["job_posted", "episode_created", "tracker_written", "finalizer_spawned"], issues)
    start_result = start_report.get("result") if isinstance(start_report.get("result"), dict) else {}
    if start_result.get("status") != "dry_run":
        issue(issues, "hard", "start_dry_run_status_not_dry_run", str(start_result.get("status") or ""))
    if start_result.get("lecture") != next_lecture:
        issue(issues, "hard", "start_dry_run_lecture_mismatch", str(start_result.get("lecture") or ""))
    start_source = start_result.get("source") if isinstance(start_result.get("source"), dict) else {}
    if start_source.get("source_file_present") is not True:
        issue(issues, "hard", "start_source_file_not_present", "")
    payload = start_result.get("payload") if isinstance(start_result.get("payload"), dict) else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    if payload.get("target_language") != "zh-CN":
        issue(issues, "hard", "start_payload_target_language_not_zh_cn", str(payload.get("target_language") or ""))
    if config.get("course") != "mit6824-2020":
        issue(issues, "hard", "start_payload_course_mismatch", str(config.get("course") or ""))
    if config.get("lecture") != next_lecture:
        issue(issues, "hard", "start_payload_lecture_mismatch", str(config.get("lecture") or ""))
    if config.get("lora_candidate_opt_in") is not True:
        issue(issues, "hard", "start_payload_lora_opt_in_not_true", str(config.get("lora_candidate_opt_in")))
    if next_tracker.get("status") != "not_started":
        issue(issues, "hard", "next_lecture_tracker_not_not_started", str(next_tracker.get("status") or ""))

    hard_count = sum(1 for row in issues if row.get("severity") == "hard")
    warning_count = sum(1 for row in issues if row.get("severity") == "warning")
    return {
        "schema_version": "holodub.mit6824.acceptance_gate_check.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if hard_count == 0 else "fail",
        "pass": hard_count == 0,
        "lecture": lecture,
        "next_lecture": next_lecture,
        "artifacts": {
            "acceptance_manifest": rel_to_root(acceptance_manifest_path, root),
            "acceptance_dry_run_report": rel_to_root(accept_report_path, root),
            "start_dry_run_report": rel_to_root(start_report_path, root),
            "acceptance_status": rel_to_root(acceptance_status_path, root),
            "tracker": rel_to_root(tracker_path, root),
        },
        "evidence": {
            "acceptance_status": current_acceptance.get("status"),
            "acceptance_readiness_ready": readiness.get("ready_for_human_review") if isinstance(readiness, dict) else None,
            "human_accepted": acceptance_manifest.get("human_accepted"),
            "post_acceptance_status": post.get("status"),
            "post_acceptance_source_ready": source_preflight.get("ready"),
            "start_payload_name": payload.get("name"),
            "start_payload_target_language": payload.get("target_language"),
            "start_payload_lora_opt_in": config.get("lora_candidate_opt_in"),
            "next_tracker_status": next_tracker.get("status"),
        },
        "issues": issues,
        "summary": {
            "hard_issue_count": hard_count,
            "warning_count": warning_count,
            "issue_codes": [str(row.get("code")) for row in issues],
        },
    }


def write_markdown(path: Path, gate: dict[str, Any]) -> None:
    lines = [
        "# MIT 6.824 Acceptance Gate Check",
        "",
        f"Generated: `{gate.get('generated_at')}`",
        f"Status: `{gate.get('status')}`",
        f"Pass: `{gate.get('pass')}`",
        f"Lecture: `{gate.get('lecture')}`",
        f"Next lecture: `{gate.get('next_lecture')}`",
        "",
        "## Artifacts",
        "",
    ]
    for key, value in (gate.get("artifacts") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Evidence", ""])
    for key, value in (gate.get("evidence") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Issues", ""])
    issues = gate.get("issues") if isinstance(gate.get("issues"), list) else []
    if not issues:
        lines.append("- None")
    else:
        for row in issues:
            lines.append(f"- `{row.get('severity')}` `{row.get('code')}` {row.get('detail') or ''}".rstrip())
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    gate = build_gate(args, root)
    write_json(output_json, gate)
    write_markdown(output_md, gate)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "summary": gate.get("summary"),
                "status": gate.get("status"),
                "pass": gate.get("pass"),
                "lecture": gate.get("lecture"),
                "next_lecture": gate.get("next_lecture"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if gate.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
