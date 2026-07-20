from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lora_split_plan import planned_parts, target_sec


SCHEMA_VERSION = "holodub.indextts2_lora_split_part_manifest.v1"
ANGLE_TOKEN_RE = re.compile(r"<([^<>]+)>")
SPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a LoRA training manifest with cleaned base rows and split-part augmentations."
    )
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--audio-output-relroot", default="training/lora_aug/split_parts")
    parser.add_argument("--splits", default="train,val,test")
    parser.add_argument("--include-base", action="store_true")
    parser.add_argument("--min-target-sec", type=float, default=18.0)
    parser.add_argument("--min-text-chars", type=int, default=180)
    parser.add_argument("--max-part-sec", type=float, default=16.0)
    parser.add_argument("--max-part-chars", type=int, default=170)
    parser.add_argument("--min-part-chars", type=int, default=24)
    parser.add_argument("--max-parts", type=int, default=4)
    parser.add_argument("--pause-sec", type=float, default=0.18)
    parser.add_argument("--duration-token-rate-policy", choices=("constant", "piecewise"), default="piecewise")
    parser.add_argument("--duration-token-rate", type=float, default=23.4375)
    parser.add_argument("--max-parent-rows", type=int, default=0)
    parser.add_argument("--max-augmented-parts", type=int, default=0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
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


def count_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def sanitize_text(text: str) -> tuple[str, list[str]]:
    cleaned = text or ""
    rewrites: list[str] = []
    replacements = {
        ord('"'): "",
        ord("'"): "",
        0x2018: "",
        0x2019: "",
        0x201c: "",
        0x201d: "",
        0x300a: "",
        0x300b: "",
    }
    translated = cleaned.translate(replacements)
    if translated != cleaned:
        rewrites.append("quotes_removed")
        cleaned = translated

    def angle_repl(match: re.Match[str]) -> str:
        inner = match.group(1).replace("_", " ").strip()
        return f" {inner} " if inner else ""

    translated = ANGLE_TOKEN_RE.sub(angle_repl, cleaned)
    if translated != cleaned:
        rewrites.append("angle_tokens_spoken")
        cleaned = translated

    translated = cleaned.replace("\u2014", "\uff0c").replace("\u2013", "\uff0c")
    translated = translated.replace("--", "\uff0c")
    if translated != cleaned:
        rewrites.append("dash_to_comma")
        cleaned = translated

    translated = cleaned.replace("\ufffd", "")
    if translated != cleaned:
        rewrites.append("replacement_char_removed")
        cleaned = translated

    translated = SPACE_RE.sub(" ", cleaned).strip()
    if translated != cleaned:
        rewrites.append("space_collapsed")
        cleaned = translated
    return cleaned, list(dict.fromkeys(rewrites))


def resolve_audio_path(data_root: Path, row: dict[str, Any]) -> Path:
    raw_path = str(row.get("audio_path") or "").strip()
    if raw_path:
        path = Path(raw_path)
        if path.exists():
            return path
    relpath = str(row.get("audio_relpath") or "").strip().replace("\\", "/")
    path = Path(relpath)
    if path.is_absolute() and path.exists():
        return path
    return data_root / relpath


def data_relpath(data_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(data_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def run_ffmpeg_cut(ffmpeg: str, source: Path, output: Path, start_sec: float, duration_sec: float, overwrite: bool) -> None:
    if output.exists() and not overwrite:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    clipped_duration_sec = max(0.05, duration_sec)
    fade_out_start_sec = max(0.0, clipped_duration_sec - 0.015)
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, start_sec):.6f}",
        "-i",
        str(source),
        "-t",
        f"{clipped_duration_sec:.6f}",
        "-af",
        f"afade=t=in:st=0:d=0.01,afade=t=out:st={fade_out_start_sec:.6f}:d=0.015",
        "-ar",
        "22050",
        "-ac",
        "1",
        str(output),
    ]
    subprocess.run(command, check=True)


def split_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        min_target_sec=args.min_target_sec,
        min_text_chars=args.min_text_chars,
        max_part_sec=args.max_part_sec,
        max_part_chars=args.max_part_chars,
        min_part_chars=args.min_part_chars,
        max_parts=args.max_parts,
        pause_sec=args.pause_sec,
        force_break_after_regex=[],
        duration_token_rate_policy=args.duration_token_rate_policy,
        duration_token_rate=args.duration_token_rate,
    )


def should_augment(row: dict[str, Any], args: argparse.Namespace) -> bool:
    return target_sec(row) >= args.min_target_sec or count_chars(str(row.get("text") or "")) >= args.min_text_chars


def cleaned_base_row(row: dict[str, Any]) -> dict[str, Any]:
    text, rewrites = sanitize_text(str(row.get("text") or ""))
    out = {**row, "text": text}
    if rewrites:
        out["text_cleaning_rewrites"] = rewrites
        out["text_source"] = f"{row.get('text_source') or 'manifest'}+cleaned"
    return out


def build_augmented_rows(
    row: dict[str, Any],
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_audio_root: Path,
) -> list[dict[str, Any]]:
    source_audio = resolve_audio_path(data_root, row)
    if not source_audio.exists():
        return []

    cleaned = cleaned_base_row(row)
    parts = planned_parts(cleaned, split_args(args))
    if len(parts) < 2:
        return []

    parent_id = str(row.get("sample_id") or row.get("segment_id"))
    audio_ms = int(float(row.get("audio_ms") or row.get("target_ms") or target_sec(row) * 1000.0))
    planned_total_ms = sum(int(part.get("target_ms") or 0) for part in parts)
    if planned_total_ms <= 0:
        return []

    rows: list[dict[str, Any]] = []
    cursor_ms = 0
    for part in parts:
        part_index = int(part["split_part_index"])
        part_ms = int(part.get("target_ms") or 0)
        if part_ms <= 0:
            continue
        start_ms = min(cursor_ms, max(0, audio_ms - 50))
        duration_ms = min(part_ms, max(50, audio_ms - start_ms))
        output_audio = output_audio_root / f"{parent_id}_p{part_index:02d}.wav"
        run_ffmpeg_cut(
            args.ffmpeg,
            source_audio,
            output_audio,
            start_ms / 1000.0,
            duration_ms / 1000.0,
            args.overwrite,
        )
        cursor_ms += part_ms

        text = str(part.get("text") or "").strip()
        part_row = {
            **cleaned,
            "schema_version": SCHEMA_VERSION,
            "sample_id": f"{parent_id}_sp{part_index}",
            "source_sample_id": parent_id,
            "source_segment_id": row.get("segment_id"),
            "segment_id": row.get("segment_id"),
            "audio_path": str(output_audio),
            "audio_relpath": data_relpath(data_root, output_audio),
            "audio_ms": duration_ms,
            "target_ms": part_ms,
            "text": text,
            "chars": count_chars(text),
            "tier": f"{row.get('tier') or 'unknown'}_split_part",
            "source_tier": row.get("tier"),
            "augmentation": {
                "type": "split_part_audio_cut",
                "source_sample_id": parent_id,
                "part_index": part_index,
                "part_count": int(part["split_part_count"]),
                "source_audio_relpath": row.get("audio_relpath"),
                "source_audio_start_ms": start_ms,
                "source_audio_duration_ms": duration_ms,
                "planned_part_target_ms": part_ms,
                "split_pause_sec": args.pause_sec,
                "duration_token_rate_policy": args.duration_token_rate_policy,
            },
            "training_policy": {
                "use": "train",
                "risk": "synthetic_split_part_alignment",
            },
        }
        rows.append(part_row)
    return rows


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    data_root = Path(args.data_root)
    source_dir = Path(args.manifest_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_audio_root = data_root / args.audio_output_relroot / output_dir.name

    split_names = [item.strip() for item in args.splits.split(",") if item.strip()]
    out_by_split: dict[str, list[dict[str, Any]]] = {split: [] for split in split_names}
    parent_count = 0
    parent_attempt_count = 0
    augmented_count = 0
    skipped: list[dict[str, Any]] = []

    for split in split_names:
        for row in read_jsonl(source_dir / f"{split}.jsonl"):
            base = cleaned_base_row(row)
            if args.include_base:
                out_by_split[split].append(base)
            if not should_augment(base, args):
                continue
            if args.max_parent_rows > 0 and parent_attempt_count >= args.max_parent_rows:
                continue
            if args.max_augmented_parts > 0 and augmented_count >= args.max_augmented_parts:
                continue
            parent_attempt_count += 1
            try:
                augmented = build_augmented_rows(
                    base,
                    args=args,
                    data_root=data_root,
                    output_audio_root=output_audio_root / split,
                )
            except Exception as exc:  # noqa: BLE001 - keep manifest generation moving and report failures.
                skipped.append(
                    {
                        "sample_id": row.get("sample_id"),
                        "split": split,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if not augmented:
                skipped.append({"sample_id": row.get("sample_id"), "split": split, "reason": "no_augmented_parts"})
                continue
            parent_count += 1
            if args.max_augmented_parts > 0:
                remaining = max(0, args.max_augmented_parts - augmented_count)
                augmented = augmented[:remaining]
            augmented_count += len(augmented)
            out_by_split[split].extend(augmented)

    all_rows: list[dict[str, Any]] = []
    for split in split_names:
        for row in out_by_split[split]:
            row["split"] = split
        all_rows.extend(out_by_split[split])
        write_jsonl(output_dir / f"{split}.jsonl", out_by_split[split])
    write_jsonl(output_dir / "all.jsonl", all_rows)

    stats = {
        "accepted": len(all_rows),
        "base_rows": sum(1 for row in all_rows if not row.get("augmentation")),
        "split_part_rows": sum(1 for row in all_rows if row.get("augmentation")),
        "augmented_parent_rows": parent_count,
        "augmented_parent_attempts": parent_attempt_count,
        "train": len(out_by_split.get("train", [])),
        "val": len(out_by_split.get("val", [])),
        "test": len(out_by_split.get("test", [])),
        "total_audio_hours": round(sum(int(row.get("audio_ms") or 0) for row in all_rows) / 3_600_000, 4),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_manifest_dir": str(source_dir),
        "stats": stats,
        "files": {"all": "all.jsonl", **{split: f"{split}.jsonl" for split in split_names}},
        "policy": {
            "include_base": args.include_base,
            "min_target_sec": args.min_target_sec,
            "min_text_chars": args.min_text_chars,
            "max_part_sec": args.max_part_sec,
            "max_part_chars": args.max_part_chars,
            "max_parts": args.max_parts,
            "pause_sec": args.pause_sec,
            "text_cleaning": "quotes_removed; angle_tokens_spoken; dash_to_comma; replacement_char_removed",
            "augmentation": "split text with lora_split_plan and cut source audio by planned part duration",
        },
        "skipped": skipped,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "README.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"output_dir": str(output_dir), **stats, "skipped": len(skipped)}


def main() -> int:
    result = build_manifest(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
