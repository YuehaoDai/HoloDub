from __future__ import annotations

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start one MIT 6.824 lecture from mit6824_course_tracker.json with safety checks."
    )
    parser.add_argument("--lecture", type=int, required=True)
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--execute", action="store_true", help="Actually POST /jobs. Without this flag the command is dry-run.")
    parser.add_argument(
        "--dry-run-output",
        default="",
        help="When not using --execute, write a machine-readable dry-run audit report to this path.",
    )
    parser.add_argument("--allow-non-not-started", action="store_true")
    parser.add_argument(
        "--block-active-episode-name-fragment",
        action="append",
        default=["lora-candidate-episode147-v25"],
        help="Refuse --execute while an active episode name contains this fragment. Repeatable.",
    )
    parser.add_argument("--skip-active-run-check", action="store_true")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def rel_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def resolve(root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, timeout: int = 60) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def active_episode_rows(args: argparse.Namespace, root: Path) -> list[dict[str, str]]:
    fragments = [fragment for fragment in args.block_active_episode_name_fragment or [] if fragment]
    if args.skip_active_run_check or not fragments:
        return []
    where = " or ".join(f"lower(name) like lower('%' || {sql_literal(fragment)} || '%')" for fragment in fragments)
    sql = f"""
select id,
       coalesce(name,'') as name,
       coalesce(status,'') as status,
       coalesce(updated_at::text,'') as updated_at
from episodes
where ({where})
  and coalesce(status,'') not in ('completed','cancelled','failed');
"""
    cmd = compose_command(
        args,
        [
            "exec",
            "-T",
            args.postgres_service,
            "psql",
            "-U",
            args.postgres_user,
            "-d",
            args.postgres_db,
            "-P",
            "pager=off",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    output = run(cmd, cwd=root, timeout=60)
    lines = [line for line in output.splitlines() if line and not line.startswith("(")]
    if len(lines) <= 1:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append({headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))})
    return rows


def load_tracker(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_lecture(tracker: dict[str, Any], lecture: int) -> dict[str, Any]:
    for row in tracker.get("lectures") or []:
        if int(row.get("lecture") or 0) == lecture:
            return row
    raise ValueError(f"lecture {lecture} not found in tracker")


def request_json(
    method: str,
    api_base: str,
    path: str,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
    timeout: float = 60.0,
) -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if not raw:
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON")
    return decoded


def validate_row(row: dict[str, Any], *, allow_non_not_started: bool) -> dict[str, Any]:
    status = row.get("status")
    if status != "not_started" and not allow_non_not_started:
        raise ValueError(f"lecture {row.get('lecture')} status is {status!r}; refusing to start without --allow-non-not-started")
    source = row.get("source") or {}
    if not source.get("source_file_present"):
        raise ValueError(f"lecture {row.get('lecture')} source file is missing")
    payload = row.get("start_payload")
    if not isinstance(payload, dict) or not payload.get("input_relpath"):
        raise ValueError(f"lecture {row.get('lecture')} has no valid start_payload")
    return payload


def source_evidence(row: dict[str, Any], root: Path) -> dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    raw_path = str(source.get("source_video_abspath") or source.get("source_video_repo_relpath") or "")
    path = Path(raw_path) if raw_path else None
    if path is not None and not path.is_absolute():
        path = root / path
    present = bool(path and path.exists())
    return {
        "source_video": source.get("source_video_repo_relpath") or source.get("source_video_relpath") or "",
        "source_video_abspath": str(path) if path else "",
        "source_file_present": present,
        "source_bytes": path.stat().st_size if present and path else source.get("source_bytes"),
        "duration_sec": source.get("duration_sec"),
        "title": source.get("title") or row.get("title") or "",
    }


def maybe_write_dry_run_output(args: argparse.Namespace, root: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    raw = str(getattr(args, "dry_run_output", "") or "")
    if not raw:
        return None
    path = resolve(root, raw)
    if path is None:
        return None
    report = {
        "schema_version": "holodub.mit6824.start_dry_run_report.v1",
        "report_role": "MIT 6.824 lecture start dry-run audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "not_start_state": True,
        "command_config": {
            "lecture": args.lecture,
            "tracker": args.tracker,
            "api_base": args.api_base,
            "execute": bool(args.execute),
            "dry_run_output": raw,
        },
        "side_effect_contract": {
            "job_posted": False,
            "episode_created": False,
            "tracker_written": False,
            "finalizer_spawned": False,
        },
        "result": result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "schema_version": report["schema_version"],
        "path": rel_to_root(path, root),
        "persisted": True,
        "not_start_state": True,
    }


def main() -> int:
    args = parse_args()
    root = repo_root()
    if args.execute and args.dry_run_output:
        raise ValueError("--dry-run-output is only valid without --execute")
    tracker_path = Path(args.tracker)
    if not tracker_path.is_absolute():
        tracker_path = root / tracker_path
    tracker = load_tracker(tracker_path)
    row = find_lecture(tracker, args.lecture)
    payload = validate_row(row, allow_non_not_started=args.allow_non_not_started)
    blockers = active_episode_rows(args, root) if args.execute else []
    if blockers:
        print(
            json.dumps(
                {
                    "status": "blocked_active_run",
                    "lecture": args.lecture,
                    "tracker": rel_to_root(tracker_path, root),
                    "blockers": blockers,
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if not args.execute:
        result = {
            "status": "dry_run",
            "lecture": args.lecture,
            "tracker": rel_to_root(tracker_path, root),
            "execute_with": f"python tools/mit6824_start_lecture.py --lecture {args.lecture} --execute",
            "source": source_evidence(row, root),
            "payload": payload,
        }
        dry_run_output = maybe_write_dry_run_output(args, root, result)
        if dry_run_output is not None:
            result["dry_run_output"] = dry_run_output
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    response = request_json("POST", args.api_base, "/jobs", payload, args.api_key)
    print(
        json.dumps(
            {
                "status": "started",
                "lecture": args.lecture,
                "tracker": rel_to_root(tracker_path, root),
                "payload": payload,
                "response": response,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
