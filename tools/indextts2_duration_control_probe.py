#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import temporal_plan_experiment as exp


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "validation" / "indextts2_duration_probe"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe community-style IndexTTS2 duration conditioning."
    )
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument(
        "--segment-ids",
        default="10514,10537,10543",
        help="Comma-separated segment ids to synthesize.",
    )
    parser.add_argument(
        "--variants",
        default="old,compact",
        help="Comma-separated text variants from temporal_plan_experiment.",
    )
    parser.add_argument(
        "--modes",
        default="off,embedding",
        help="Comma-separated duration-control modes.",
    )
    parser.add_argument("--duration-token-rate", type=float, default=50.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cps-mode", choices=("default", "episode"), default="episode")
    parser.add_argument("--llm-compact", action="store_true")
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    samples = exp.load_episode_segments(args.pg_container, args.episode_id)
    by_id = {s.segment_id: s for s in samples}
    wanted = [int(x.strip()) for x in args.segment_ids.split(",") if x.strip()]
    missing = [sid for sid in wanted if sid not in by_id]
    if missing:
        raise SystemExit(f"missing segment ids: {missing}")

    cps = 4.0
    if args.cps_mode == "episode":
        cps = exp.estimate_episode_chars_per_sec(samples) or cps

    run_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    compact_cache: dict[str, dict[str, Any]] = {}

    requests: list[dict[str, Any]] = []
    manifest_samples: list[dict[str, Any]] = []
    for sid in wanted:
        sample = by_id[sid]
        manifest_samples.append(exp.sample_summary(sample, cps))
        for variant in variants:
            text = exp.text_for_variant(
                sample,
                variant,
                cps,
                llm_compact=args.llm_compact,
                compact_cache=compact_cache,
            )
            for repeat in range(max(1, args.repeats)):
                for mode in modes:
                    payload = exp.build_tts_payload(
                        sample,
                        f"{variant}-{mode}",
                        repeat,
                        run_id,
                        cps,
                        temporal_min_target_sec=999999.0,
                        text_override=text,
                    )
                    payload["output_relpath"] = (
                        f"experiments/indextts2_duration_probe/{run_id}/"
                        f"seg{sample.segment_id}-{variant}-{mode}-r{repeat}.wav"
                    )
                    requests.append(
                        {
                            "segment_id": sample.segment_id,
                            "variant": variant,
                            "repeat": repeat,
                            "duration_mode": mode,
                            "duration_token_rate": args.duration_token_rate,
                            "target_ms": sample.target_ms,
                            "baseline_tts_ms": sample.baseline_tts_ms,
                            "expected_text": text,
                            "payload": payload,
                        }
                    )

    manifest = {
        "episode_id": args.episode_id,
        "segment_ids": wanted,
        "variants": variants,
        "modes": modes,
        "duration_token_rate": args.duration_token_rate,
        "repeats": args.repeats,
        "chars_per_sec": cps,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "compact_cache": compact_cache,
        "samples": manifest_samples,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    requests_path = run_dir / "requests.json"
    results_path = run_dir / "results.jsonl"
    requests_path.write_text(
        json.dumps(requests, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"wrote manifest: {run_dir / 'manifest.json'}")
    print(f"synth requests: {len(requests)}")
    container_requests = "/data/" + requests_path.relative_to(REPO_ROOT / "data").as_posix()
    container_results = "/data/" + results_path.relative_to(REPO_ROOT / "data").as_posix()
    synthesize_in_container(args.ml_container, container_requests, container_results)
    summarize_results(results_path)
    return 0


def synthesize_in_container(container: str, requests_path: str, results_path: str) -> None:
    script = r"""
import json
import time
from pathlib import Path

from app.adapters.tts import TTSAdapter
from app.config import Settings
from app.models import TTSRequest

requests_path = Path(__import__("sys").argv[1])
results_path = Path(__import__("sys").argv[2])
items = json.loads(requests_path.read_text(encoding="utf-8"))
settings = Settings()
settings.indextts2_duration_token_rate = 50.0
adapter = TTSAdapter(settings)
results_path.parent.mkdir(parents=True, exist_ok=True)
with results_path.open("w", encoding="utf-8") as fh:
    for item in items:
        settings.indextts2_duration_control_mode = item["duration_mode"]
        settings.indextts2_duration_token_rate = float(item.get("duration_token_rate") or 50.0)
        started = time.time()
        error = ""
        response = {}
        try:
            response = adapter.synthesize(TTSRequest(**item["payload"])).model_dump()
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
        elapsed = round(time.time() - started, 3)
        actual_ms = int(response.get("actual_duration_ms") or 0)
        row = {
            **{k: v for k, v in item.items() if k != "payload"},
            "elapsed_sec": elapsed,
            "actual_ms": actual_ms,
            "drift_ms": actual_ms - int(item.get("target_ms") or 0) if actual_ms else None,
            "abs_drift_ms": abs(actual_ms - int(item.get("target_ms") or 0)) if actual_ms else None,
            "audio_relpath": response.get("audio_relpath", item["payload"].get("output_relpath", "")),
            "diagnostics": response.get("diagnostics", []),
            "error": error,
        }
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)
"""
    command = ["docker", "exec", "-i", container, "python", "-", requests_path, results_path]
    subprocess.run(command, input=script, text=True, check=True)


def summarize_results(results_path: Path) -> None:
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print("\nsummary:")
    for row in rows:
        print(
            "seg={segment_id} {variant:<7} {duration_mode:<9} "
            "target={target_ms:>5} actual={actual_ms:>5} "
            "drift={drift_ms!s:>6} err={error}".format(**row)
        )


if __name__ == "__main__":
    sys.exit(main())
