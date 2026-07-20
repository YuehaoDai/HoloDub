from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize external runtime blockers for MIT 6.824 delivery.")
    parser.add_argument("--status-refresh", default="data/validation/mit6824_status_refresh.json")
    parser.add_argument("--watchdog-status", default="data/validation/mit6824_watchdog_status.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_runtime_blockers.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_runtime_blockers.md")
    parser.add_argument(
        "--include-system-probes",
        action="store_true",
        help="Capture WSL and Docker service diagnostics for manual repair handoff. Not intended for frequent polling.",
    )
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
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def docker_summary(status_refresh: dict[str, Any], watchdog: dict[str, Any]) -> dict[str, Any]:
    refresh_summary = status_refresh.get("summary") if isinstance(status_refresh.get("summary"), dict) else {}
    refresh_runtime = refresh_summary.get("runtime") if isinstance(refresh_summary.get("runtime"), dict) else {}
    refresh_docker = refresh_runtime.get("docker") if isinstance(refresh_runtime.get("docker"), dict) else {}
    watchdog_runtime = watchdog.get("runtime") if isinstance(watchdog.get("runtime"), dict) else {}
    watchdog_docker = watchdog_runtime.get("docker") if isinstance(watchdog_runtime.get("docker"), dict) else {}
    return watchdog_docker or refresh_docker or {"status": "not_checked", "reason": "not_checked"}


def decode_probe_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    if raw.count(b"\x00") > max(4, len(raw) // 10):
        try:
            return raw.decode("utf-16le", errors="replace").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding, errors="replace").replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def run_probe(command: list[str], *, root: Path, timeout_sec: float = 20.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = decode_probe_output(exc.stdout if isinstance(exc.stdout, bytes) else None)
        return {
            "command": command,
            "status": "timeout",
            "timeout_sec": timeout_sec,
            "stdout_tail": stdout[-2000:],
        }
    stdout = decode_probe_output(proc.stdout)
    return {
        "command": command,
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "stdout_tail": stdout[-2000:],
    }


def collect_system_probes(root: Path) -> dict[str, Any]:
    return {
        "wsl_list": run_probe(["wsl", "-l", "-v"], root=root, timeout_sec=20.0),
        "docker_service": run_probe(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Service -Name com.docker.service -ErrorAction SilentlyContinue | "
                "Select-Object Name,Status,StartType | ConvertTo-Json -Compress",
            ],
            root=root,
            timeout_sec=20.0,
        ),
        "docker_desktop_wsl_system_probe": run_probe(
            ["wsl", "--system", "-d", "docker-desktop", "true"],
            root=root,
            timeout_sec=20.0,
        ),
    }


def build_report(
    *,
    status_refresh: dict[str, Any],
    watchdog: dict[str, Any],
    root: Path,
    system_probes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    watchdog_summary = watchdog.get("summary") if isinstance(watchdog.get("summary"), dict) else {}
    missing_reasons = watchdog_summary.get("missing_reasons")
    if not isinstance(missing_reasons, list):
        missing_reasons = []
    docker = docker_summary(status_refresh, watchdog)
    docker_status = str(docker.get("status") or watchdog_summary.get("docker_status") or "not_checked")
    blockers: list[dict[str, Any]] = []
    if docker_status not in {"available", "not_checked"} or "docker_engine_unavailable" in missing_reasons:
        blockers.append(
            {
                "id": "docker_engine_unavailable",
                "severity": "external_runtime_blocker",
                "title": "Docker Desktop / WSL engine is unavailable",
                "blocks": [
                    "lecture8_finalizer",
                    "docker_compose_services",
                    "ml_service_candidate_execution",
                    "episode_generation_pipeline",
                ],
                "evidence": {
                    "docker": docker,
                    "watchdog_summary": watchdog_summary,
                    "system_probes": system_probes or {},
                },
                "safe_recovery_commands": [
                    "Start Docker Desktop or Docker Desktop Service with sufficient permissions",
                    "docker version",
                    "wsl -l -v",
                    "python tools\\mit6824_watchdog.py --allow-missing",
                    "python tools\\mit6824_watchdog.py --restart-status-watch --allow-missing",
                    "python tools\\mit6824_finalize_lecture.py --lecture 8 --refresh-tracker --watch --execute --poll-sec 300 --timeout-sec 172800",
                    "python tools\\mit6824_status_refresh.py",
                ],
                "manual_checks_if_still_failing": [
                    "Run Docker Desktop troubleshooting diagnostics",
                    "Check whether docker-desktop WSL can mount its VHDX",
                    "Confirm com.docker.service can be started from an elevated shell",
                ],
                "unsafe_actions_not_taken": [
                    "Do not unregister docker-desktop WSL distro automatically",
                    "Do not delete Docker VHDX files automatically",
                    "Do not factory-reset Docker Desktop without explicit user approval and backup awareness",
                ],
            }
        )
    return {
        "schema_version": "holodub.mit6824_runtime_blockers.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "needs_external_repair" if blockers else "ok",
        "paths": {
            "status_refresh": rel_to_root(resolve(root, "data/validation/mit6824_status_refresh.json"), root),
            "watchdog_status": rel_to_root(resolve(root, "data/validation/mit6824_watchdog_status.json"), root),
        },
        "summary": {
            "blocker_count": len(blockers),
            "blocker_ids": [blocker["id"] for blocker in blockers],
            "docker_status": docker_status,
            "system_probe_count": len(system_probes or {}),
        },
        "system_probes": system_probes or {},
        "blockers": blockers,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# MIT 6.824 Runtime Blockers",
        "",
        f"Generated: `{report.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Status: `{report.get('status', '')}`",
        f"- Blocker count: `{summary.get('blocker_count', 0)}`",
        f"- Blocker IDs: `{summary.get('blocker_ids', [])}`",
        f"- Docker status: `{summary.get('docker_status', '')}`",
        "",
    ]
    for blocker in report.get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        evidence = blocker.get("evidence") if isinstance(blocker.get("evidence"), dict) else {}
        docker = evidence.get("docker") if isinstance(evidence.get("docker"), dict) else {}
        probes = evidence.get("system_probes") if isinstance(evidence.get("system_probes"), dict) else {}
        lines.extend(
            [
                f"## {blocker.get('id', '')}",
                "",
                f"- Severity: `{blocker.get('severity', '')}`",
                f"- Title: {blocker.get('title', '')}",
                f"- Blocks: `{blocker.get('blocks', [])}`",
                f"- Docker reason: `{docker.get('reason', '')}`",
                f"- Docker command: `{docker.get('command', '')}`",
                f"- System probes captured: `{list(probes.keys())}`",
                "",
                "### Safe Recovery Commands",
                "",
            ]
        )
        commands = blocker.get("safe_recovery_commands") if isinstance(blocker.get("safe_recovery_commands"), list) else []
        lines.extend(f"- `{command}`" for command in commands)
        lines.extend(["", "### Unsafe Actions Not Taken", ""])
        unsafe = blocker.get("unsafe_actions_not_taken") if isinstance(blocker.get("unsafe_actions_not_taken"), list) else []
        lines.extend(f"- {item}" for item in unsafe)
        if probes:
            lines.extend(["", "### System Probe Tails", ""])
            for name, probe in probes.items():
                if not isinstance(probe, dict):
                    continue
                tail = str(probe.get("stdout_tail") or "").replace("`", "'")
                lines.extend(
                    [
                        f"- `{name}` status: `{probe.get('status', '')}` returncode: `{probe.get('returncode', '')}`",
                        f"  - `{tail[:500]}`",
                    ]
                )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    status_refresh_path = resolve(root, args.status_refresh)
    watchdog_path = resolve(root, args.watchdog_status)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    previous = read_json(output_json)
    previous_probes = previous.get("system_probes") if isinstance(previous.get("system_probes"), dict) else {}
    system_probes = collect_system_probes(root) if args.include_system_probes else previous_probes
    report = build_report(
        status_refresh=read_json(status_refresh_path),
        watchdog=read_json(watchdog_path),
        root=root,
        system_probes=system_probes,
    )
    write_json(output_json, report)
    write_markdown(output_md, report)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "status": report.get("status"),
                "summary": report.get("summary"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
