#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "validation" / "temporal_plan_experiments"


@dataclass
class SegmentSample:
    segment_id: int
    job_id: int
    ordinal: int
    target_ms: int
    baseline_tts_ms: int
    gap_after_ms: int
    source_text: str
    target_text: str
    meta: dict[str, Any]

    @property
    def baseline_abs_drift_ms(self) -> int:
        return abs(self.baseline_tts_ms - self.target_ms)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare old TTS requests against pre-TTS temporal planning."
    )
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument("--per-bucket", type=int, default=3)
    parser.add_argument("--top-drift", type=int, default=6)
    parser.add_argument("--max-samples", type=int, default=12)
    parser.add_argument(
        "--segment-ids",
        default="",
        help="Comma-separated segment ids. When set, bypasses automatic sampling.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--run-tts", action="store_true")
    parser.add_argument("--temporal-min-target-sec", type=float, default=15.0)
    parser.add_argument(
        "--variants",
        default="old,temporal",
        help=(
            "Comma-separated variants: old, temporal, old_fit, temporal_fit, "
            "compact, compact_fit, compact_temporal, compact_temporal_fit."
        ),
    )
    parser.add_argument(
        "--llm-compact",
        action="store_true",
        help="Use the configured OpenAI-compatible LLM to pre-compress compact variants.",
    )
    parser.add_argument("--fit-min-tempo", type=float, default=0.85)
    parser.add_argument("--fit-max-tempo", type=float, default=1.15)
    parser.add_argument(
        "--fit-filter",
        choices=("rubberband", "atempo"),
        default="rubberband",
    )
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--cps-mode",
        choices=("default", "episode"),
        default="default",
        help="Use language default chars/sec or estimate from episode baseline.",
    )
    args = parser.parse_args()

    segments = load_episode_segments(args.pg_container, args.episode_id)
    if not segments:
        print(f"no synthesized segments found for episode {args.episode_id}", file=sys.stderr)
        return 1

    cps = 4.0
    if args.cps_mode == "episode":
        cps = estimate_episode_chars_per_sec(segments) or cps

    if args.segment_ids:
        wanted = {int(x.strip()) for x in args.segment_ids.split(",") if x.strip()}
        by_id = {s.segment_id: s for s in segments}
        missing = sorted(wanted - set(by_id))
        if missing:
            print(f"missing segment ids: {missing}", file=sys.stderr)
            return 1
        samples = [by_id[sid] for sid in sorted(wanted)]
    else:
        samples = choose_samples(
            segments,
            per_bucket=args.per_bucket,
            top_drift=args.top_drift,
            max_samples=args.max_samples,
        )
    run_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "episode_id": args.episode_id,
        "sample_count": len(samples),
        "repeats": args.repeats,
        "run_tts": args.run_tts,
        "variants": [v.strip() for v in args.variants.split(",") if v.strip()],
        "temporal_min_target_sec": args.temporal_min_target_sec,
        "fit_min_tempo": args.fit_min_tempo,
        "fit_max_tempo": args.fit_max_tempo,
        "fit_filter": args.fit_filter,
        "llm_compact": args.llm_compact,
        "cps_mode": args.cps_mode,
        "chars_per_sec": cps,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "samples": [sample_summary(s, cps) for s in samples],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"wrote manifest: {run_dir / 'manifest.json'}")
    print_sample_table(samples, cps)

    if not args.run_tts:
        print("dry run only; pass --run-tts to synthesize old vs temporal variants")
        return 0

    results_path = run_dir / "results.jsonl"
    with results_path.open("w", encoding="utf-8") as fh:
        variants = [v.strip() for v in args.variants.split(",") if v.strip()]
        compact_cache: dict[str, dict[str, Any]] = {}
        for sample in samples:
            for repeat in range(args.repeats):
                base_cache: dict[str, dict[str, Any]] = {}
                for variant in variants:
                    base_variant = variant.replace("_fit", "")
                    started = time.time()
                    if variant.endswith("_fit") and base_variant in base_cache:
                        cached = base_cache[base_variant]
                        response = dict(cached.get("response") or {})
                        error = str(cached.get("error") or "")
                        payload = dict(cached.get("payload") or {})
                    else:
                        payload = build_tts_payload(
                            sample,
                            base_variant,
                            repeat,
                            run_dir.name,
                            cps,
                            temporal_min_target_sec=args.temporal_min_target_sec,
                            text_override=text_for_variant(
                                sample,
                                base_variant,
                                cps,
                                llm_compact=args.llm_compact,
                                compact_cache=compact_cache,
                            ),
                        )
                        try:
                            response = call_tts(args.ml_container, payload)
                            error = ""
                        except Exception as exc:  # noqa: BLE001
                            response = {}
                            error = repr(exc)
                        if not error:
                            base_cache[base_variant] = {
                                "response": dict(response),
                                "error": error,
                                "payload": dict(payload),
                            }
                    elapsed = time.time() - started
                    actual_ms = int(response.get("actual_duration_ms") or 0)
                    fit_info: dict[str, Any] = {}
                    if not error and variant.endswith("_fit"):
                        fit_info = apply_duration_fit(
                            args.ml_container,
                            response.get("audio_relpath", ""),
                            sample.target_ms,
                            actual_ms,
                            min_tempo=args.fit_min_tempo,
                            max_tempo=args.fit_max_tempo,
                            fit_filter=args.fit_filter,
                        )
                        if fit_info.get("fit_error"):
                            error = str(fit_info["fit_error"])
                        elif fit_info.get("fit_applied"):
                            actual_ms = int(fit_info.get("fit_actual_ms") or actual_ms)
                        fit_info["fit_source_variant"] = base_variant
                    row = {
                        "segment_id": sample.segment_id,
                        "job_id": sample.job_id,
                        "ordinal": sample.ordinal,
                        "variant": variant,
                        "repeat": repeat,
                        "target_ms": sample.target_ms,
                        "baseline_tts_ms": sample.baseline_tts_ms,
                        "baseline_drift_ms": sample.baseline_tts_ms - sample.target_ms,
                        "actual_ms": actual_ms,
                        "drift_ms": actual_ms - sample.target_ms if actual_ms else None,
                        "abs_drift_ms": abs(actual_ms - sample.target_ms) if actual_ms else None,
                        "elapsed_sec": round(elapsed, 3),
                        "audio_relpath": response.get("audio_relpath", ""),
                        "expected_text": payload.get("text", ""),
                        "expected_chars": speakable_len(payload.get("text", "")),
                        "text_source": text_source_for_variant(sample, base_variant),
                        **fit_info,
                        "diagnostics": response.get("diagnostics", []),
                        "error": error,
                    }
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    print_result(row)

    if compact_cache:
        manifest["compact_cache"] = compact_cache
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    summarize_results(results_path)
    return 0


def load_episode_segments(pg_container: str, episode_id: int) -> list[SegmentSample]:
    query = f"""
with ranked as (
  select
    s.id,
    s.job_id,
    s.ordinal,
    s.end_ms - s.start_ms as target_ms,
    coalesce(s.tts_duration_ms, 0) as baseline_tts_ms,
    greatest(
      coalesce(
        lead(s.start_ms) over (partition by s.job_id order by s.ordinal) - s.end_ms,
        30000
      ),
      0
    ) as gap_after_ms,
    coalesce(s.source_text, '') as source_text,
    coalesce(s.target_text, '') as target_text,
    coalesce(s.meta::text, '{{}}') as meta
  from segments s
  join jobs j on j.id = s.job_id
  where j.episode_id = {int(episode_id)}
    and s.status = 'synthesized'
    and coalesce(s.target_text, '') <> ''
)
select * from ranked order by job_id, ordinal;
"""
    cmd = [
        "docker",
        "exec",
        pg_container,
        "psql",
        "-U",
        "holodub",
        "-d",
        "holodub",
        "--csv",
        "-c",
        query,
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
    rows = csv.DictReader(proc.stdout.splitlines())
    samples: list[SegmentSample] = []
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        samples.append(
            SegmentSample(
                segment_id=int(row["id"]),
                job_id=int(row["job_id"]),
                ordinal=int(row["ordinal"]),
                target_ms=int(row["target_ms"]),
                baseline_tts_ms=int(row["baseline_tts_ms"]),
                gap_after_ms=int(row["gap_after_ms"]),
                source_text=row["source_text"],
                target_text=row["target_text"],
                meta=meta,
            )
        )
    return samples


def choose_samples(
    segments: list[SegmentSample],
    *,
    per_bucket: int,
    top_drift: int,
    max_samples: int,
) -> list[SegmentSample]:
    buckets = [
        ("short", lambda s: s.target_ms <= 8_000),
        ("medium", lambda s: 15_000 <= s.target_ms <= 25_000),
        ("long", lambda s: s.target_ms >= 30_000),
    ]
    chosen: dict[int, SegmentSample] = {}
    for _, predicate in buckets:
        bucket = [s for s in segments if predicate(s)]
        bucket.sort(key=lambda s: (-s.baseline_abs_drift_ms, s.job_id, s.ordinal))
        for sample in bucket[:per_bucket]:
            chosen[sample.segment_id] = sample

    by_drift = sorted(segments, key=lambda s: (-s.baseline_abs_drift_ms, s.job_id, s.ordinal))
    for sample in by_drift[:top_drift]:
        chosen[sample.segment_id] = sample

    out = list(chosen.values())
    out.sort(key=lambda s: (duration_bucket_order(s), -s.baseline_abs_drift_ms, s.job_id, s.ordinal))
    return out[:max_samples]


def duration_bucket_order(sample: SegmentSample) -> int:
    if sample.target_ms <= 8_000:
        return 0
    if sample.target_ms <= 25_000:
        return 1
    return 2


def estimate_episode_chars_per_sec(segments: list[SegmentSample]) -> float:
    rates = [
        speakable_len(sample_tts_text(s)) / (s.baseline_tts_ms / 1000.0)
        for s in segments
        if s.baseline_tts_ms > 0 and speakable_len(sample_tts_text(s)) > 0
    ]
    if not rates:
        return 0
    return statistics.median(rates)


def sample_tts_text(sample: SegmentSample) -> str:
    spoken = str(sample.meta.get("tts_spoken_text") or "").strip()
    return repair_spoken_clarity(spoken or sample.target_text)


def repair_spoken_clarity(text: str) -> str:
    repairs = [
        ("AppendEntries RPC", "追加条目RPC"),
        ("Append Entries RPC", "追加条目RPC"),
    ]
    out = text
    for old, new in repairs:
        out = out.replace(old, new)
    return out


def uses_temporal_plan(base_variant: str) -> bool:
    return base_variant == "temporal" or base_variant.endswith("_temporal")


def uses_compact_text(base_variant: str) -> bool:
    return base_variant.startswith("compact")


def text_source_for_variant(sample: SegmentSample, base_variant: str) -> str:
    if uses_compact_text(base_variant):
        return "llm_compact"
    return "tts_spoken_text" if sample_tts_text(sample) != sample.target_text else "target_text"


def text_for_variant(
    sample: SegmentSample,
    base_variant: str,
    cps: float,
    *,
    llm_compact: bool,
    compact_cache: dict[str, dict[str, Any]],
) -> str:
    if not uses_compact_text(base_variant):
        return sample_tts_text(sample)
    cache_key = f"{sample.segment_id}:{base_variant}"
    cached = compact_cache.get(cache_key)
    if cached:
        return str(cached.get("text") or sample_tts_text(sample))
    original = sample_tts_text(sample)
    original_chars = speakable_len(original)
    max_chars = compact_char_target(sample, original, cps, compact_char_bias(base_variant))
    min_chars = max(compact_min_chars(max_chars), int(math.floor(original_chars * 0.80)))
    if min_chars > max_chars:
        max_chars = min(original_chars, min_chars)
    used_min_chars = min_chars
    used_max_chars = max_chars
    method = "llm" if llm_compact else "heuristic"
    if llm_compact:
        compact = llm_compact_text(sample, original, min_chars, max_chars)
        if looks_incomplete_compact(compact) and max_chars < speakable_len(original):
            relaxed_budgets = [
                max_chars + max(18, int(math.ceil(max_chars * 0.15))),
                max_chars + max(28, int(math.ceil(max_chars * 0.25))),
            ]
            for relaxed_max in relaxed_budgets:
                relaxed_max = min(speakable_len(original), relaxed_max)
                relaxed = llm_compact_text(sample, original, min_chars, relaxed_max)
                relaxed_chars = speakable_len(relaxed)
                if (
                    min_chars <= relaxed_chars <= relaxed_max
                    and not looks_incomplete_compact(relaxed)
                ):
                    compact = relaxed
                    used_max_chars = relaxed_max
                    method = "llm_relaxed"
                    break
    else:
        compact = heuristic_compact_text(original, max_chars)
    compact = repair_compact_clarity(compact)
    compact = maybe_append_terminal_punctuation(compact, used_max_chars)
    if speakable_len(compact) > used_max_chars:
        used_max_chars = speakable_len(compact)
        if llm_compact:
            method = f"{method}_clarity_repair"
    compact_cache[cache_key] = {
        "segment_id": sample.segment_id,
        "variant": base_variant,
        "text": compact,
        "min_chars": used_min_chars,
        "max_chars": used_max_chars,
        "strict_min_chars": min_chars,
        "strict_max_chars": max_chars,
        "original_chars": speakable_len(original),
        "compact_chars": speakable_len(compact),
        "complete_sentence": not looks_incomplete_compact(compact),
        "method": method,
    }
    return compact


def repair_compact_clarity(text: str) -> str:
    repairs = [
        ("Append Entries RPC\u8bf7\u6c42", "\u8ffd\u52a0\u6761\u76eeRPC\u8bf7\u6c42"),
        ("Append Entries RPC", "\u8ffd\u52a0\u6761\u76eeRPC"),
        ("\u8fd9\u6b21\u8ffd\u52a0\u6761\u76eeRPC\u8bf7\u6c42", "\u8fd9\u6b21RPC\u8bf7\u6c42"),
        ("\u8fd9\u6b21\u8ffd\u52a0\u6761\u76eeRPC", "\u8fd9\u6b21RPC"),
        ("\u540d\u4e3a\"\u4e0b\u4e00\u4e2a\u7d22\u5f15\"\u6838\u5fc3\u5b57\u6bb5", "\u540d\u4e3a\"\u4e0b\u4e00\u4e2a\u7d22\u5f15\"\u7684\u6838\u5fc3\u5b57\u6bb5"),
        ("\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c", "\u670d\u52a1\u5668\u4e8c\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c"),
        ("\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e00\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c", "\u670d\u52a1\u5668\u4e00\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c"),
        ("\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c", "\u670d\u52a1\u5668\u4e8c\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c"),
        ("\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e00\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c", "\u670d\u52a1\u5668\u4e00\u7684\u4e0b\u4e00\u4e2a\u7d22\u5f15\u503c"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u4efb\u671f", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u4efb\u671f"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u7d22\u5f15\u503c", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u503c"),
        ("\u524d\u4e00\u65e5\u5fd7\u7d22\u5f15\u4f4d\u7f6e", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u4f4d\u7f6e"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u7d22\u5f15\u662f", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u662f"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u503c\u4e0d\u540c", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7f16\u53f7\u4e0d\u540c"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u662f", "\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7f16\u53f7\u662f"),
        ("\u4ece\u65b0\u6307\u5b9a\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7d22\u5f15\u4f4d\u7f6e\u4e4b\u540e", "\u4ece\u65b0\u6307\u5b9a\u4f4d\u7f6e\u4e4b\u540e"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u4efb\u671f", "\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7684\u4efb\u671f"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7684\u7f16\u53f7", "\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7684\u7f16\u53f7"),
        ("\u524d\u4e00\u4e2a\u65e5\u5fd7\u6761\u76ee\u7f16\u53f7", "\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7f16\u53f7"),
        ("\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7684\u4efb\u671f", "\u524d\u4e00\u4e2a\u6761\u76ee\u7684\u4efb\u671f"),
        ("\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7684\u7f16\u53f7", "\u524d\u4e00\u4e2a\u6761\u76ee\u7684\u7f16\u53f7"),
        ("\u524d\u4e00\u4e2a\u65e5 \u5fd7\u6761\u76ee\u7f16\u53f7", "\u524d\u4e00\u4e2a\u6761\u76ee\u7f16\u53f7"),
        ("\u8fd9\u4e00\u5173\u952e\u4e8b\u5b9e\u56e0\u6b64", "\u8fd9\u4e00\u5173\u952e\u4e8b\u5b9e\u3002\u56e0\u6b64"),
        ("\u6761\u76ee\u800c\u662f", "\u6761\u76ee\uff0c\u800c\u662f"),
        ("RPC\u8bf7\u6c42\u9886\u5bfc\u8005", "RPC\u8bf7\u6c42\u3002\u9886\u5bfc\u8005"),
        ("\u62d2\u7edd\u54cd\u5e94\u4e0e\u6b64\u540c\u65f6", "\u62d2\u7edd\u54cd\u5e94\u3002\u4e0e\u6b64\u540c\u65f6"),
        ("\u6838\u5fc3\u5b57\u6bb5\u4e3a\u6bcf\u4e2a", "\u6838\u5fc3\u5b57\u6bb5\uff0c\u4e3a\u6bcf\u4e2a"),
        ("\u4fdd\u5b58\u4e00\u4efd\u5177\u4f53\u6765\u8bf4", "\u4fdd\u5b58\u4e00\u4efd\u3002\u5177\u4f53\u6765\u8bf4"),
        ("\u552f\u4ed6\u80fd\u5f53\u9009\uff0c\u56e0\u4efb\u671f\u66f4\u9ad8", "\u552f\u4ed6\u80fd\u5f53\u9009\uff0c\u56e0\u4e3a\u4efb\u671f\u66f4\u9ad8"),
        ("\u552f\u4ed6\u80fd\u5f53\u9009 \u56e0\u4efb\u671f\u66f4\u9ad8", "\u552f\u4ed6\u80fd\u5f53\u9009\uff0c\u56e0\u4e3a\u4efb\u671f\u66f4\u9ad8"),
        ("\u56e0\u4efb\u671f\u66f4\u9ad8", "\u56e0\u4e3a\u4efb\u671f\u66f4\u9ad8"),
        ("\u56e0\u5176\u4efb\u671f\u66f4\u9ad8", "\u56e0\u4e3a\u4efb\u671f\u66f4\u9ad8"),
        ("\u670d\u52a1\u5668\u4e8c", "\u4e8c\u53f7\u8282\u70b9"),
        ("\u670d\u52a1\u5668\u4e00", "\u4e00\u53f7\u8282\u70b9"),
    ]
    out = text
    for old, new in repairs:
        out = out.replace(old, new)
    return out


def compact_char_bias(base_variant: str) -> int:
    if "tight" in base_variant:
        return -8
    if "loose" in base_variant:
        return 6
    return 0


def compact_char_target(sample: SegmentSample, text: str, cps: float, bias: int = 0) -> int:
    chars = speakable_len(text)
    if chars <= 0:
        return 0
    if sample.baseline_tts_ms > sample.target_ms:
        target = int(math.floor(chars * sample.target_ms / sample.baseline_tts_ms))
    else:
        target = int(math.floor((sample.target_ms / 1000.0) * max(cps, 1.0)))
    return max(1, min(chars, target - 2 + bias))


def compact_min_chars(max_chars: int) -> int:
    return max(1, min(max_chars, max(int(math.floor(max_chars * 0.94)), max_chars - 12)))


def heuristic_compact_text(text: str, max_chars: int) -> str:
    out = text
    replacements = [
        ("\u5e76\u4e14", ""),
        ("\u8fd9\u6b21\u65b0\u7684", "\u8fd9\u6b21"),
        ("\u6709\u6240\u4e0d\u540c", "\u4e0d\u540c"),
        ("\u4f46\u5173\u952e\u662f", "\u5173\u952e\u662f"),
        ("\u5c06\u4f1a", "\u4f1a"),
        ("\u5177\u4f53\u5185\u5bb9", "\u5185\u5bb9"),
        ("\u5bf9\u4e8e", ""),
        ("\u800c\u8a00", ""),
        ("\u56e0\u6b64\uff0c\u4e8c\u53f7\u8282\u70b9\uff0c", "\u56e0\u6b64\uff0c\u4e8c\u53f7\u8282\u70b9\u7684"),
        ("\u8be5\u4f4d\u7f6e\u5904\u7684", "\u8be5\u4f4d\u7f6e\u7684"),
        ("\u5b8c\u5168\u4e00\u81f4", "\u4e00\u81f4"),
        ("\u5c31\u5fc5\u987b", "\u5fc5\u987b"),
        ("实际上", ""),
        ("正式", ""),
        ("当前的", ""),
        ("本次", "这次"),
        ("完整包含", "包含"),
        ("所有既有内容", "所有内容"),
        ("严格规则", "规则"),
        ("必须立即", "必须"),
    ]
    for old, new in replacements:
        if speakable_len(out) <= max_chars:
            break
        out = out.replace(old, new)
    out = out.replace(
        "\u56e0\u6b64\uff0c\u4e8c\u53f7\u8282\u70b9\uff0c",
        "\u56e0\u6b64\uff0c\u4e8c\u53f7\u8282\u70b9\u7684",
    )
    return out


def fit_text_to_budget(text: str, min_chars: int, max_chars: int) -> str:
    out = heuristic_compact_text(text, max_chars)
    if min_chars <= speakable_len(out) <= max_chars:
        return trim_to_complete_clause(out, min_chars)
    if speakable_len(out) <= max_chars:
        return trim_to_complete_clause(out, min_chars)

    # Last-resort cleanup for provider outputs that ignore the hard interval:
    # remove low-semantic punctuation/filler before falling back to a hard cut.
    for token in [
        "\uff0c",
        "\u3002",
        "\uff1b",
        "\uff1a",
        "\u2014\u2014",
        "\u7684",
    ]:
        if speakable_len(out) <= max_chars:
            break
        out = out.replace(token, "")
    if speakable_len(out) <= max_chars:
        return out

    kept: list[str] = []
    count = 0
    for ch in out:
        if not ch.isspace():
            count += 1
        if count > max_chars:
            break
        kept.append(ch)
    return trim_to_complete_clause("".join(kept).strip(), min_chars)


def looks_incomplete_tail(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.count('"') % 2 == 1 or stripped.count("\u201c") != stripped.count("\u201d"):
        return True
    dangling_terms = (
        "\u4e0b\u4e00",
        "\u4e0a\u4e00",
        "\u524d\u4e00",
        "\u8fd9\u4e2a",
        "\u8fd9\u4e9b",
        "\u90a3\u4e2a",
        "\u540d\u4e3a",
        "\u79f0\u4e3a",
        "\u5c5e\u4e8e",
        "\u4e13\u5c5e\u4e8e",
        "\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c",
        "\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c",
        "\u4f4d\u4e8e",
        "\u4e5f",
        "\u4e5f\u7ef4",
        "\u7ef4",
        "\u7ef4\u62a4",
        "\u5c06",
        "\u628a",
        "\u4ece",
        "\u5728",
    )
    return any(stripped.endswith(term) for term in dangling_terms)


def has_terminal_punctuation(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return ord(stripped[-1]) in {33, 46, 63, 0x3002, 0xFF01, 0xFF1F}


def looks_incomplete_compact(text: str) -> bool:
    if looks_incomplete_tail(text):
        return True
    return speakable_len(text) >= 40 and not has_terminal_punctuation(text)


def maybe_append_terminal_punctuation(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if has_terminal_punctuation(stripped) or looks_incomplete_tail(stripped):
        return stripped
    if speakable_len(stripped) + 1 <= max_chars:
        return stripped + "\u3002"
    return stripped


def trim_to_complete_clause(text: str, min_chars: int) -> str:
    stripped = text.strip()
    if not looks_incomplete_tail(stripped):
        return stripped
    if stripped.count('"') % 2 == 1:
        quote = stripped.rfind('"')
        if quote > 0 and speakable_len(stripped[:quote]) >= min_chars:
            stripped = stripped[:quote].strip()
    separators = ".!?;:,，、。！？；："
    best = ""
    for idx, ch in enumerate(stripped):
        if ch not in separators:
            continue
        prefix = stripped[: idx + 1].strip()
        if speakable_len(prefix) >= min_chars:
            best = prefix
    if best and not looks_incomplete_tail(best):
        return best
    for term in (
        "\u4e0b\u4e00",
        "\u4e0a\u4e00",
        "\u524d\u4e00",
        "\u8fd9\u4e2a",
        "\u8fd9\u4e9b",
        "\u90a3\u4e2a",
        "\u540d\u4e3a",
        "\u79f0\u4e3a",
        "\u5c5e\u4e8e",
        "\u4e13\u5c5e\u4e8e",
        "\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c",
        "\u4e13\u5c5e\u4e8e\u670d\u52a1\u5668\u4e8c",
        "\u4f4d\u4e8e",
        "\u4e5f",
        "\u4e5f\u7ef4",
        "\u7ef4",
        "\u7ef4\u62a4",
        "\u5c06",
        "\u628a",
        "\u4ece",
        "\u5728",
    ):
        if stripped.endswith(term) and speakable_len(stripped[: -len(term)]) >= min_chars:
            return stripped[: -len(term)].strip()
    return stripped


def llm_compact_text(sample: SegmentSample, text: str, min_chars: int, max_chars: int) -> str:
    load_dotenv()
    base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("RETRANSLATION_MODEL") or os.getenv("OPENAI_MODEL", "")
    if not base_url or not api_key or not model:
        return heuristic_compact_text(text, max_chars)

    best = ""
    for attempt in range(3):
        extra = ""
        if best:
            issue = "outside the target interval"
            if looks_incomplete_compact(best):
                issue = "did not end as a complete punctuated sentence"
            extra = (
                f"\nPrevious attempt was {speakable_len(best)} chars and {issue}. "
                "Revise that previous attempt to stay inside the interval without dropping facts.\n"
                f"Previous attempt text:\n{best}"
            )
        payload = {
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You rewrite Chinese dubbing spoken_text before TTS. "
                        "Return JSON only: {\"text\":\"...\"}. Preserve every factual claim, "
                        "number, protocol term, and causal relation. Do not add commentary. "
                        "The text must sound natural when read aloud. Do not truncate. "
                        "End at a complete clause or sentence, with no dangling word. "
                        "Use natural Chinese punctuation every 40-70 characters when possible."
                        "For Chinese output, it must end with a Chinese period, question mark, or exclamation mark."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Target duration: {sample.target_ms / 1000.0:.2f}s\n"
                        f"Required non-space Chinese character interval: {min_chars}-{max_chars}\n"
                        f"Source English:\n{sample.source_text}\n\n"
                        f"Current Chinese spoken_text ({speakable_len(text)} chars):\n{text}\n\n"
                        "Rewrite the Chinese spoken_text to fit the interval. "
                        "Keep it concise, faithful, and suitable for TTS."
                        "\nDo not cut off the ending to satisfy length. Rewrite instead. "
                        "The final character must be Chinese punctuation such as \u3002, \uff1f, or \uff01."
                        f"{extra}"
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            compact = str(parsed.get("text") or "").strip()
        except Exception:  # noqa: BLE001
            return heuristic_compact_text(text, max_chars)
        if not compact:
            return heuristic_compact_text(text, max_chars)
        compact = maybe_append_terminal_punctuation(compact, max_chars)
        best = compact
        chars = speakable_len(compact)
        if min_chars <= chars <= max_chars and not looks_incomplete_compact(compact):
            return compact
    if best and speakable_len(best) > max_chars:
        shrunk = fit_text_to_budget(best, min_chars, max_chars)
        shrunk = maybe_append_terminal_punctuation(shrunk, max_chars)
        if min_chars <= speakable_len(shrunk) <= max_chars and not looks_incomplete_compact(shrunk):
            return shrunk
    if best:
        best = maybe_append_terminal_punctuation(best, max_chars)
    if best and min_chars <= speakable_len(best) <= max_chars and not looks_incomplete_compact(best):
        return best
    fallback = fit_text_to_budget(text, min_chars, max_chars)
    fallback = maybe_append_terminal_punctuation(fallback, max_chars)
    if min_chars <= speakable_len(fallback) <= max_chars:
        return fallback
    return fallback


def load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_tts_payload(
    sample: SegmentSample,
    variant: str,
    repeat: int,
    run_id: str,
    cps: float,
    *,
    temporal_min_target_sec: float,
    text_override: str | None = None,
) -> dict[str, Any]:
    base_variant = variant.replace("_fit", "")
    text = text_override if text_override is not None else sample_tts_text(sample)
    meta = dict(sample.meta.get("dubbing") or {})
    meta.pop("timing", None)
    if uses_temporal_plan(base_variant) and sample.target_ms / 1000.0 >= temporal_min_target_sec:
        meta["timing"] = build_timing_plan(
            text,
            sample.target_ms,
            cps,
            pacing=sample_pacing(sample),
            arousal=sample_arousal(sample),
        )

    return {
        "text": text,
        "target_duration_sec": sample.target_ms / 1000.0,
        "max_allowed_sec": (sample.target_ms + sample.gap_after_ms) / 1000.0,
        "voice_config": {},
        "output_relpath": (
            f"experiments/temporal_plan/{run_id}/"
            f"seg{sample.segment_id}-{variant}-r{repeat}.wav"
        ),
        "dubbing_meta": meta or None,
    }


def build_timing_plan(
    text: str,
    target_ms: int,
    cps: float,
    *,
    pacing: str = "normal",
    arousal: float | None = None,
) -> dict[str, Any]:
    target_sec = target_ms / 1000.0
    min_allowed_ms = max(0, int(round((target_sec - 0.7) * 1000)))
    max_allowed_ms = int(round((target_sec + 0.3) * 1000))
    chars = speakable_len(text)
    predicted_ms = int(round(chars / cps * 1000)) if cps > 0 else 0
    required_cps = chars / target_sec if target_sec > 0 else 0.0
    min_natural_cps, max_natural_cps = natural_rate_window(cps, pacing=pacing, arousal=arousal)
    rate_status = "natural"
    rate_risk = ""
    if required_cps > max_natural_cps:
        rate_status = "too_fast"
        rate_risk = "required_speech_rate_above_natural_window"
    elif required_cps > 0 and required_cps < min_natural_cps:
        rate_status = "too_slow"
        rate_risk = "required_speech_rate_below_natural_window"
    min_chars = max(0, int(math.ceil((min_allowed_ms / 1000.0) * cps)))
    max_chars = max(1, int(math.floor((max_allowed_ms / 1000.0) * cps)))
    status = "fits"
    risk = ""
    if predicted_ms > max_allowed_ms:
        status = "too_long"
        risk = "text_exceeds_pre_tts_budget"
    elif predicted_ms < min_allowed_ms:
        status = "too_short"
        risk = "text_under_fills_slot"
    return {
        "target_ms": target_ms,
        "predicted_ms": predicted_ms,
        "min_allowed_ms": min_allowed_ms,
        "max_allowed_ms": max_allowed_ms,
        "chars": chars,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "chars_per_sec": cps,
        "pacing": pacing,
        "arousal": arousal,
        "required_chars_per_sec": round(required_cps, 3),
        "rate_ratio": round(required_cps / cps, 3) if cps > 0 else 0,
        "min_natural_chars_per_sec": round(min_natural_cps, 3),
        "max_natural_chars_per_sec": round(max_natural_cps, 3),
        "rate_status": rate_status,
        "rate_risk": rate_risk,
        "status": status,
        "risk": risk,
        "groups": build_timing_groups(text, target_ms),
    }


def natural_rate_window(cps: float, pacing: str = "normal", arousal: float | None = None) -> tuple[float, float]:
    if cps <= 0:
        cps = 10.0
    min_mult, max_mult = 0.82, 1.18
    pacing = (pacing or "normal").strip().lower()
    if pacing == "slow":
        min_mult, max_mult = 0.70, 1.05
    elif pacing == "fast":
        min_mult, max_mult = 0.95, 1.35
    if arousal is not None:
        arousal = max(0.0, min(1.0, float(arousal)))
        delta = arousal - 0.5
        min_mult *= 1 + 0.08 * delta
        max_mult *= 1 + 0.16 * delta
    return max(0.3, cps * min_mult), max(0.3, cps * max_mult)


def build_timing_groups(text: str, target_ms: int, max_groups: int = 8) -> list[dict[str, Any]]:
    groups = split_breath_groups(text)
    groups = pack_short_groups(groups, min_chars=32)
    if len(groups) > max_groups:
        groups = pack_groups(groups, max_groups)
    total_chars = sum(speakable_len(g) for g in groups)
    if not groups or total_chars <= 0:
        return []
    out = []
    assigned = 0
    for idx, group in enumerate(groups):
        chars = speakable_len(group)
        group_ms = round(target_ms * chars / total_chars)
        if idx == len(groups) - 1:
            group_ms = target_ms - assigned
        assigned += int(group_ms)
        out.append({"text": group, "chars": chars, "target_ms": int(group_ms)})
    return out


def split_breath_groups(text: str) -> list[str]:
    boundaries = set(".!?;:\n。！？；：")
    out: list[str] = []
    current: list[str] = []
    for ch in text.strip():
        current.append(ch)
        if ch in boundaries:
            piece = "".join(current).strip()
            if piece:
                out.append(piece)
            current = []
    piece = "".join(current).strip()
    if piece:
        out.append(piece)
    return out


def pack_groups(groups: list[str], max_groups: int) -> list[str]:
    total = sum(speakable_len(g) for g in groups)
    target = max(1, math.ceil(total / max_groups))
    packed: list[str] = []
    current = ""
    current_chars = 0
    for group in groups:
        if current and current_chars >= target and len(packed) < max_groups - 1:
            packed.append(current)
            current = ""
            current_chars = 0
        current += group
        current_chars += speakable_len(group)
    if current:
        packed.append(current)
    return packed


def pack_short_groups(groups: list[str], min_chars: int) -> list[str]:
    if min_chars <= 0 or len(groups) <= 1:
        return groups
    packed: list[str] = []
    current = ""
    current_chars = 0
    for group in groups:
        current += group
        current_chars += speakable_len(group)
        if current_chars >= min_chars:
            packed.append(current)
            current = ""
            current_chars = 0
    if current:
        packed.append(current)
    return packed


def call_tts(container: str, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    code = (
        "import base64,json,sys,urllib.request;"
        "payload=json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'));"
        "data=json.dumps(payload,ensure_ascii=False).encode('utf-8');"
        "req=urllib.request.Request('http://127.0.0.1:8000/tts/run',data=data,"
        "headers={'Content-Type':'application/json'},method='POST');"
        "print(urllib.request.urlopen(req,timeout=1800).read().decode('utf-8'))"
    )
    proc = subprocess.run(
        ["docker", "exec", container, "python", "-c", code, encoded],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1900,
    )
    return json.loads(proc.stdout)


def apply_duration_fit(
    container: str,
    audio_relpath: str,
    target_ms: int,
    actual_ms: int,
    *,
    min_tempo: float,
    max_tempo: float,
    fit_filter: str,
) -> dict[str, Any]:
    if not audio_relpath or target_ms <= 0 or actual_ms <= 0:
        return {"fit_applied": False, "fit_error": "missing audio or duration"}
    tempo = actual_ms / target_ms
    if tempo < min_tempo or tempo > max_tempo:
        return {
            "fit_applied": False,
            "fit_skipped": "tempo_out_of_range",
            "fit_tempo": round(tempo, 6),
        }

    in_abs = "/data/" + audio_relpath.replace("\\", "/").lstrip("/")
    if audio_relpath.endswith(".wav"):
        out_relpath = audio_relpath[:-4] + f".{fit_filter}-fit.wav"
    else:
        out_relpath = audio_relpath + f".{fit_filter}-fit.wav"
    out_abs = "/data/" + out_relpath.replace("\\", "/").lstrip("/")
    if fit_filter == "rubberband":
        audio_filter = f"rubberband=tempo={tempo:.8f}:pitch=1.0"
    else:
        audio_filter = f"atempo={tempo:.8f}"
    script = (
        "set -e;"
        f"mkdir -p \"$(dirname '{out_abs}')\";"
        f"ffmpeg -hide_banner -loglevel error -y -i '{in_abs}' "
        f"-filter:a '{audio_filter}' '{out_abs}';"
        f"ffprobe -v error -show_entries format=duration "
        f"-of default=noprint_wrappers=1:nokey=1 '{out_abs}'"
    )
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if proc.returncode != 0:
        return {
            "fit_applied": False,
            "fit_tempo": round(tempo, 6),
            "fit_error": proc.stderr.strip() or proc.stdout.strip(),
        }
    try:
        fit_sec = float(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "fit_applied": False,
            "fit_tempo": round(tempo, 6),
            "fit_error": f"could not parse ffprobe output: {proc.stdout!r}",
        }
    return {
        "fit_applied": True,
        "fit_filter": fit_filter,
        "fit_tempo": round(tempo, 6),
        "fit_audio_relpath": out_relpath,
        "fit_actual_ms": int(round(fit_sec * 1000)),
    }


def probe_audio_ms(container: str, audio_relpath: str) -> int:
    if not audio_relpath:
        return 0
    audio_abs = "/data/" + audio_relpath.replace("\\", "/").lstrip("/")
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_abs,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    try:
        return int(round(float(proc.stdout.strip().splitlines()[-1]) * 1000))
    except (ValueError, IndexError):
        return 0


def print_sample_table(samples: list[SegmentSample], cps: float) -> None:
    print("selected samples:")
    print("segment job ord target_s baseline_s drift_s chars pred_s status req_cps rate rate_status pacing")
    for sample in samples:
        plan = build_timing_plan(
            sample.target_text,
            sample.target_ms,
            cps,
            pacing=sample_pacing(sample),
            arousal=sample_arousal(sample),
        )
        print(
            f"{sample.segment_id:>7} {sample.job_id:>3} {sample.ordinal:>3} "
            f"{sample.target_ms/1000:>8.2f} {sample.baseline_tts_ms/1000:>10.2f} "
            f"{(sample.baseline_tts_ms-sample.target_ms)/1000:>7.2f} "
            f"{plan['chars']:>5} {plan['predicted_ms']/1000:>6.2f} {plan['status']} "
            f"{plan['required_chars_per_sec']:>7.2f} {plan['rate_ratio']:>4.2f} "
            f"{plan['rate_status']} {plan['pacing']}"
        )


def print_result(row: dict[str, Any]) -> None:
    if row["error"]:
        print(f"ERROR seg={row['segment_id']} {row['variant']} r{row['repeat']}: {row['error']}")
        return
    print(
        f"seg={row['segment_id']} {row['variant']} r{row['repeat']} "
        f"actual={row['actual_ms']/1000:.2f}s drift={row['drift_ms']/1000:.2f}s "
        f"elapsed={row['elapsed_sec']:.1f}s"
    )


def summarize_results(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"wrote results: {path}")
    variants = sorted({r["variant"] for r in rows})
    for variant in variants:
        vals = [r["abs_drift_ms"] for r in rows if r["variant"] == variant and r["abs_drift_ms"] is not None]
        if not vals:
            continue
        vals.sort()
        p50 = percentile(vals, 50) / 1000.0
        p95 = percentile(vals, 95) / 1000.0
        print(
            f"{variant}: n={len(vals)} mean={statistics.mean(vals)/1000:.3f}s "
            f"p50={p50:.3f}s p95={p95:.3f}s max={max(vals)/1000:.3f}s"
        )


def percentile(vals: list[int], pct: float) -> float:
    if not vals:
        return 0
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(vals[lo])
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def sample_summary(sample: SegmentSample, cps: float) -> dict[str, Any]:
    text = sample_tts_text(sample)
    plan = build_timing_plan(
        text,
        sample.target_ms,
        cps,
        pacing=sample_pacing(sample),
        arousal=sample_arousal(sample),
    )
    return {
        "segment_id": sample.segment_id,
        "job_id": sample.job_id,
        "ordinal": sample.ordinal,
        "target_ms": sample.target_ms,
        "baseline_tts_ms": sample.baseline_tts_ms,
        "baseline_drift_ms": sample.baseline_tts_ms - sample.target_ms,
        "chars": plan["chars"],
        "predicted_ms": plan["predicted_ms"],
        "status": plan["status"],
        "risk": plan["risk"],
        "required_chars_per_sec": plan["required_chars_per_sec"],
        "rate_ratio": plan["rate_ratio"],
        "rate_status": plan["rate_status"],
        "rate_risk": plan["rate_risk"],
        "pacing": plan["pacing"],
        "arousal": plan["arousal"],
        "source_preview": sample.source_text[:120],
        "target_preview": text[:120],
        "text_source": "tts_spoken_text" if text != sample.target_text else "target_text",
    }


def speakable_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def sample_pacing(sample: SegmentSample) -> str:
    dubbing = sample.meta.get("dubbing")
    if isinstance(dubbing, dict):
        pacing = dubbing.get("pacing")
        if isinstance(pacing, str) and pacing:
            return pacing
    return "normal"


def sample_arousal(sample: SegmentSample) -> float | None:
    dubbing = sample.meta.get("dubbing")
    if not isinstance(dubbing, dict):
        return None
    emotion = dubbing.get("emotion")
    if not isinstance(emotion, dict):
        return None
    arousal = emotion.get("arousal")
    try:
        return float(arousal)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
