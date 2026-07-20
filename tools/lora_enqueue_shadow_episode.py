from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enqueue tts_duration tasks for a cloned LoRA shadow episode.")
    parser.add_argument("--clone-report", required=True)
    parser.add_argument("--queue-key", default="holodub:tasks")
    parser.add_argument("--requested-by", default="lora_shadow_clone")
    parser.add_argument("--reason", default="lora_shadow_episode_tts")
    parser.add_argument("--redis-service", default="redis")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, timeout: int = 30) -> str:
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


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def enqueue_jobs(args: argparse.Namespace, root: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for chapter in report.get("chapters") or []:
        payload = {
            "job_id": int(chapter["shadow_job_id"]),
            "stage": "tts_duration",
            "attempt": 0,
            "requested_by": args.requested_by,
            "reason": args.reason,
        }
        cmd = compose_command(
            args,
            [
                "exec",
                "-T",
                args.redis_service,
                "redis-cli",
                "RPUSH",
                args.queue_key,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ],
        )
        run(cmd, cwd=root)
        payloads.append(payload)
    return payloads


def main() -> int:
    args = parse_args()
    root = repo_root()
    report_path = Path(args.clone_report)
    if not report_path.is_absolute():
        report_path = root / report_path
    report = load_report(report_path)
    payloads = enqueue_jobs(args, root, report)
    report["enqueued"] = True
    report["queue_key"] = args.queue_key
    report["requested_by"] = args.requested_by
    report["reason"] = args.reason
    report["enqueued_payloads"] = payloads
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"clone_report": str(report_path), "enqueued_payloads": payloads}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
