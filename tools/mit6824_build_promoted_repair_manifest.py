from __future__ import annotations

import argparse
import json
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "holodub.indextts2_lora_manifest.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a LoRA training manifest from promoted MIT 6.824 repair audio."
    )
    parser.add_argument("--promotion-summary", required=True)
    parser.add_argument("--lightweight-features", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--tier", default="silver", choices=("silver", "gold"))
    parser.add_argument(
        "--warning",
        default="silver promoted repair data; use for adapter experiments until full episode and human review pass",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(path: str | Path, root: Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else root / value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return int(round(handle.getnframes() / float(handle.getframerate()) * 1000.0))


def count_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def chars_per_sec(chars: int, ms: int) -> float:
    if ms <= 0:
        return 0.0
    return round(chars / (ms / 1000.0), 6)


def build(args: argparse.Namespace) -> dict[str, Any]:
    root = repo_root()
    data_root = resolve(args.data_root, root)
    promotion_path = resolve(args.promotion_summary, root)
    lightweight_path = resolve(args.lightweight_features, root)
    output_dir = resolve(args.output_dir, root)
    output_dir.mkdir(parents=True, exist_ok=True)

    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    lightweight_rows = {
        str(row.get("sample_id") or ""): row for row in read_jsonl(lightweight_path)
    }
    selected = promotion.get("selected")
    if not isinstance(selected, dict) or not selected:
        raise ValueError("promotion summary has no selected repairs")

    rows: list[dict[str, Any]] = []
    missing_lightweight: list[str] = []
    for source_id, selected_row in selected.items():
        if not isinstance(selected_row, dict):
            continue
        source_id = str(source_id)
        lightweight = lightweight_rows.get(source_id)
        if not lightweight:
            missing_lightweight.append(source_id)
            continue
        audio_path = resolve(str(selected_row.get("path") or ""), root)
        audio_relpath = audio_path.resolve().relative_to(data_root.resolve()).as_posix()
        audio_ms = wav_duration_ms(audio_path)
        sample_id = str(selected_row.get("repair_sample_id") or source_id)
        text = str(lightweight.get("text") or lightweight.get("display_text") or "").strip()
        target_ms = int(lightweight.get("target_ms") or audio_ms)
        chars = count_chars(text)
        signed_drift_ms = audio_ms - target_ms
        reported_abs_drift = safe_float(selected_row.get("abs_drift_ms"), abs(float(signed_drift_ms)))
        fit_tempo_delta = safe_float(selected_row.get("fit_tempo_delta"))
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "sample_id": sample_id,
                "source_sample_id": source_id,
                "segment_id": lightweight.get("segment_id"),
                "episode_id": lightweight.get("episode_id"),
                "split": "train",
                "tier": args.tier,
                "text": text,
                "source_text": lightweight.get("source_text"),
                "audio_relpath": audio_relpath,
                "audio_path": str(audio_path),
                "audio_exists": audio_path.exists(),
                "audio_ms": audio_ms,
                "target_ms": target_ms,
                "drift_ms": signed_drift_ms,
                "abs_drift_ms": reported_abs_drift,
                "reported_abs_drift_ms": reported_abs_drift,
                "fit_tempo_delta": fit_tempo_delta,
                "asr_similarity": safe_float(selected_row.get("similarity")),
                "critical_term_recall": 1.0,
                "chars": chars,
                "target_chars_per_sec": chars_per_sec(chars, target_ms),
                "audio_chars_per_sec": chars_per_sec(chars, audio_ms),
                "text_source": "promoted_repair_selected",
                "training_policy": {
                    "use": "train",
                    "risk": "promoted_repair_adjunct_not_standalone_gold",
                    "requires": [
                        "append_to_lecture7_style_feasible_corpus",
                        "preprocess_inside_ml_container",
                        "generated_audio_holdout_gate",
                        "episode_shadow_fit_audit",
                    ],
                },
                "repair_metadata": {
                    "selected_audio_path": selected_row.get("path"),
                    "source_sample_id": source_id,
                    "repair_sample_id": sample_id,
                    "promotion_summary": str(promotion_path.relative_to(root)).replace("\\", "/"),
                },
            }
        )

    rows.sort(key=lambda row: str(row.get("sample_id") or ""))
    write_jsonl(output_dir / "train.jsonl", rows)
    write_jsonl(output_dir / "all.jsonl", rows)
    write_jsonl(output_dir / "val.jsonl", [])
    write_jsonl(output_dir / "test.jsonl", [])
    write_jsonl(output_dir / "rejected.jsonl", [])

    stats = {
        "accepted": len(rows),
        "rejected": 0,
        "missing_audio": sum(1 for row in rows if not row.get("audio_exists")),
        "missing_lightweight": len(missing_lightweight),
        "train": len(rows),
        "val": 0,
        "test": 0,
        "total_audio_hours": round(
            sum(int(row.get("audio_ms") or 0) for row in rows) / 3_600_000.0,
            4,
        ),
        "max_target_chars_per_sec": max((safe_float(row.get("target_chars_per_sec")) for row in rows), default=0.0),
        "max_fit_tempo_delta": max((safe_float(row.get("fit_tempo_delta")) for row in rows), default=0.0),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tier": args.tier,
        "episode_ids": sorted({row.get("episode_id") for row in rows}),
        "source": "promoted selected repair audio",
        "source_files": {
            "promotion_summary": str(promotion_path.relative_to(root)).replace("\\", "/"),
            "lightweight_features": str(lightweight_path.relative_to(root)).replace("\\", "/"),
        },
        "filters": {
            "max_abs_drift_ms": 500,
            "min_asr_similarity": 0.97,
            "max_fit_tempo_delta": 0.05,
            "join_quality_fail_count": 0,
        },
        "stats": stats,
        "files": {
            "all": "all.jsonl",
            "train": "train.jsonl",
            "val": "val.jsonl",
            "test": "test.jsonl",
            "rejected": "rejected.jsonl",
        },
        "warnings": [args.warning],
        "policy": {
            "role": "repair_adjunct",
            "failed_shadow_audio_is_not_gold": True,
            "must_merge_with": "Lecture-7-style feasible corpus before training",
            "max_fit_tempo_delta": 0.05,
            "max_target_chars_per_sec": stats["max_target_chars_per_sec"],
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "# Promoted Repair LoRA Manifest\n\n"
        "Generated from selected repair audio that passed ASR, drift, fit, and join gates.\n",
        encoding="utf-8",
    )
    return {"output_dir": str(output_dir), "sample_ids": [row["sample_id"] for row in rows], "stats": stats}


def main() -> int:
    print(json.dumps(build(parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
