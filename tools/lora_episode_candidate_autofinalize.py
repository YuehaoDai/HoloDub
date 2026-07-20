from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll lora_episode_candidate_finalize.py until an episode shadow run is complete."
    )
    parser.add_argument("--source-episode-id", type=int, required=True)
    parser.add_argument("--shadow-episode-id", type=int, required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--poll-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    parser.add_argument("--asr-timeout-sec", type=int, default=10800)
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-review-pack", action="store_true")
    parser.add_argument("--skip-clips", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def finalize_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        args.python,
        "tools/lora_episode_candidate_finalize.py",
        "--source-episode-id",
        str(args.source_episode_id),
        "--shadow-episode-id",
        str(args.shadow_episode_id),
        "--run-dir",
        args.run_dir,
        "--api-base",
        args.api_base,
        "--data-root",
        args.data_root,
        "--asr-timeout-sec",
        str(args.asr_timeout_sec),
    ]
    if args.skip_asr:
        cmd.append("--skip-asr")
    if args.skip_review_pack:
        cmd.append("--skip-review-pack")
    if args.skip_clips:
        cmd.append("--skip-clips")
    return cmd


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "auto_finalize_status.json"
    history_path = run_dir / "auto_finalize_history.jsonl"
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    cmd = finalize_command(args)
    attempt = 0
    while True:
        attempt += 1
        started = time.time()
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        row = {
            "attempt": attempt,
            "returncode": completed.returncode,
            "elapsed_sec": round(time.time() - started, 1),
            "updated_at_epoch": time.time(),
            "command": cmd,
            "output_tail": completed.stdout[-4000:],
        }
        if completed.returncode == 0:
            row["status"] = "finalized"
            write_json(status_path, row)
            append_jsonl(history_path, row)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            return 0
        if completed.returncode != 2:
            row["status"] = "finalize_failed"
            write_json(status_path, row)
            append_jsonl(history_path, row)
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            return completed.returncode
        row["status"] = "waiting_for_episode"
        write_json(status_path, row)
        append_jsonl(history_path, row)
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        if deadline and time.time() >= deadline:
            row["status"] = "timeout"
            write_json(status_path, row)
            append_jsonl(history_path, row)
            return 124
        time.sleep(max(1.0, args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
