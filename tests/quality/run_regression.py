from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from pathlib import Path


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def ffprobe_duration_ms(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(float(result.stdout.strip()) * 1000)


def safe_ratio(a: int, b: int) -> float:
    if b <= 0:
        return 0.0
    return a / b


def evaluate_job(api_base_url: str, data_root: Path, sample: dict) -> dict:
    job_id = sample["job_id"]
    job = fetch_json(f"{api_base_url.rstrip('/')}/jobs/{job_id}")

    output_path = data_root / job.get("output_relpath", "")
    input_path = data_root / job.get("input_relpath", "")

    output_exists = output_path.exists()
    output_duration_ms = ffprobe_duration_ms(output_path) if output_exists else 0
    input_duration_ms = ffprobe_duration_ms(input_path) if input_path.exists() else 0
    output_delta_ms = abs(output_duration_ms - input_duration_ms)

    segment_reports = []
    max_segment_delta = 0
    max_translation_ratio = 0.0
    all_segments_synthesized = True
    for segment in job.get("segments", []):
        original_duration = int(segment.get("original_duration_ms") or 0)
        synthesized_duration = int(segment.get("tts_duration_ms") or 0)
        segment_delta = abs(synthesized_duration - original_duration)
        max_segment_delta = max(max_segment_delta, segment_delta)

        source_len = len((segment.get("src_text") or "").strip())
        target_len = len((segment.get("tgt_text") or "").strip())
        translation_ratio = safe_ratio(target_len, source_len)
        max_translation_ratio = max(max_translation_ratio, translation_ratio)

        synthesized = bool(segment.get("tts_audio_path"))
        if not synthesized:
            all_segments_synthesized = False

        segment_reports.append(
            {
                "segment_id": segment.get("id"),
                "original_duration_ms": original_duration,
                "tts_duration_ms": synthesized_duration,
                "duration_delta_ms": segment_delta,
                "translation_ratio": round(translation_ratio, 3),
                "synthesized": synthesized,
            }
        )

    report = {
        "sample": sample.get("name", f"job-{job_id}"),
        "job_id": job_id,
        "status": job.get("status"),
        "output_exists": output_exists,
        "input_duration_ms": input_duration_ms,
        "output_duration_ms": output_duration_ms,
        "output_duration_delta_ms": output_delta_ms,
        "all_segments_synthesized": all_segments_synthesized,
        "max_segment_duration_delta_ms": max_segment_delta,
        "max_translation_length_ratio": round(max_translation_ratio, 3),
        "thresholds": {
            "max_output_duration_delta_ms": sample.get("max_output_duration_delta_ms", 1000),
            "max_segment_duration_delta_ms": sample.get("max_segment_duration_delta_ms", 500),
            "max_translation_length_ratio": sample.get("max_translation_length_ratio", 1.8),
        },
        "pass": (
            job.get("status") == "completed"
            and output_exists
            and output_delta_ms <= sample.get("max_output_duration_delta_ms", 1000)
            and all_segments_synthesized
            and max_segment_delta <= sample.get("max_segment_duration_delta_ms", 500)
            and max_translation_ratio <= sample.get("max_translation_length_ratio", 1.8)
        ),
        "segments": segment_reports,
    }
    return report


def load_samples(args: argparse.Namespace) -> list[dict]:
    if args.manifest:
        payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        return payload.get("samples", [])
    if args.job_id is None:
        raise SystemExit("either --job-id or --manifest is required")
    return [
        {
            "name": f"job-{args.job_id}",
            "job_id": args.job_id,
            "max_output_duration_delta_ms": args.max_output_duration_delta_ms,
            "max_segment_duration_delta_ms": args.max_segment_duration_delta_ms,
            "max_translation_length_ratio": args.max_translation_length_ratio,
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run HoloDub regression checks against completed jobs.")
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--output")
    parser.add_argument("--max-output-duration-delta-ms", type=int, default=1000)
    parser.add_argument("--max-segment-duration-delta-ms", type=int, default=500)
    parser.add_argument("--max-translation-length-ratio", type=float, default=1.8)
    args = parser.parse_args()

    samples = load_samples(args)
    reports = [evaluate_job(args.api_base_url, Path(args.data_root), sample) for sample in samples]
    payload = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "reports": reports}

    output = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    print(output)

    return 0 if all(report["pass"] for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
