from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import torch


SCHEMA_VERSION = "holodub.indextts2_lora_features.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encode HoloDub LoRA manifest rows into IndexTTS2 GPT training features."
    )
    parser.add_argument("--manifest-dir", required=True, help="Directory containing train/val/test JSONL manifests.")
    parser.add_argument("--output-dir", required=True, help="Directory for feature JSONL and .npy arrays.")
    parser.add_argument("--data-root", default="/data", help="Container data root used by audio_relpath values.")
    parser.add_argument("--voice-prompt-relpath", default="voices/test_ref.wav")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cfg-path", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--no-fp16", action="store_true", help="Disable fp16 model loading.")
    parser.add_argument("--splits", default="train,val,test", help="Comma-separated split names to process.")
    parser.add_argument("--max-train-samples", type=int, default=48)
    parser.add_argument("--max-val-samples", type=int, default=8)
    parser.add_argument("--max-test-samples", type=int, default=8)
    parser.add_argument("--max-text-tokens", type=int, default=600)
    parser.add_argument("--max-code-tokens", type=int, default=1200)
    parser.add_argument("--max-audio-sec", type=float, default=60.0)
    parser.add_argument("--duration-token-rate", type=float, default=23.4375)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def resolve_data_path(data_root: Path, relpath: str) -> Path:
    path = Path(relpath)
    if path.is_absolute():
        return path
    return data_root / relpath.replace("\\", "/")


def torch_device_type(device: Any) -> str:
    return torch.device(device).type


def limit_for_split(args: argparse.Namespace, split: str) -> int:
    if split == "train":
        return args.max_train_samples
    if split == "val":
        return args.max_val_samples
    if split == "test":
        return args.max_test_samples
    return 0


def tokenize(tts: Any, text: str) -> list[int]:
    tokens = tts.tokenizer.tokenize(text)
    ids = tts.tokenizer.convert_tokens_to_ids(tokens)
    return [int(item) for item in ids]


def encode_audio_codes(tts: Any, audio_path: Path, max_audio_sec: float) -> np.ndarray:
    audio, _ = tts._load_and_cut_audio(str(audio_path), max_audio_sec, sr=16000)
    inputs = tts.extract_features(audio, sampling_rate=16000, return_tensors="pt")
    input_features = inputs["input_features"].to(tts.device)
    attention_mask = inputs["attention_mask"].to(tts.device)
    with torch.no_grad():
        with torch.amp.autocast(
            torch_device_type(tts.device),
            enabled=tts.dtype is not None,
            dtype=tts.dtype,
        ):
            features = tts.get_emb(input_features, attention_mask)
        codes = tts.semantic_codec.quantize(features)[0]
    return codes[0].detach().cpu().numpy().astype(np.int64)


def build_shared_conditioning(tts: Any, voice_prompt: Path, output_dir: Path) -> tuple[Path, Path]:
    shared_dir = output_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    condition_path = shared_dir / "condition.npy"
    emo_vec_path = shared_dir / "emo_vec.npy"

    spk_cond_emb, _, _, _ = tts._get_speaker_conditioning(str(voice_prompt))
    emo_cond_emb = tts._get_emotion_conditioning(str(voice_prompt))
    cond_lengths = torch.tensor([spk_cond_emb.shape[-1]], device=tts.device)
    emo_lengths = torch.tensor([emo_cond_emb.shape[-1]], device=tts.device)

    with torch.no_grad():
        with torch.amp.autocast(
            torch_device_type(tts.device),
            enabled=tts.dtype is not None,
            dtype=tts.dtype,
        ):
            condition = tts.gpt.get_conditioning(spk_cond_emb.transpose(1, 2), cond_lengths)
            emo_vec = tts.gpt.merge_emovec(
                spk_cond_emb,
                emo_cond_emb,
                cond_lengths,
                emo_lengths,
                alpha=1.0,
            )

    np.save(condition_path, condition.squeeze(0).detach().float().cpu().numpy())
    np.save(emo_vec_path, emo_vec.squeeze(0).detach().float().cpu().numpy())
    return condition_path, emo_vec_path


def main() -> int:
    args = parse_args()

    # Import inside main so py_compile on the host does not require IndexTTS2.
    from indextts.infer_v2 import IndexTTS2

    data_root = Path(args.data_root)
    manifest_dir = Path(args.manifest_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    arrays_dir = output_dir / "arrays"
    arrays_dir.mkdir(parents=True, exist_ok=True)

    voice_prompt = resolve_data_path(data_root, args.voice_prompt_relpath)
    if not voice_prompt.exists():
        raise FileNotFoundError(f"voice prompt not found: {voice_prompt}")

    print("[preprocess] loading IndexTTS2")
    tts = IndexTTS2(
        model_dir=args.model_dir,
        cfg_path=args.cfg_path,
        use_fp16=not args.no_fp16,
        device=args.device,
        use_cuda_kernel=False,
    )
    print("[preprocess] model loaded")

    condition_path, emo_vec_path = build_shared_conditioning(tts, voice_prompt, output_dir)
    max_mel_tokens = int(getattr(tts.gpt, "max_mel_tokens", args.max_code_tokens))
    effective_max_code_tokens = min(args.max_code_tokens, max_mel_tokens - 4)

    split_names = [part.strip() for part in args.splits.split(",") if part.strip()]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_dir": str(manifest_dir),
        "output_dir": str(output_dir),
        "voice_prompt": str(voice_prompt),
        "condition_path": str(condition_path),
        "emo_vec_path": str(emo_vec_path),
        "max_text_tokens": args.max_text_tokens,
        "max_code_tokens": effective_max_code_tokens,
        "duration_token_rate": args.duration_token_rate,
        "splits": {},
        "rejections": [],
    }

    for split in split_names:
        source_path = manifest_dir / f"{split}.jsonl"
        if not source_path.exists():
            print(f"[preprocess] skip missing split: {source_path}")
            continue
        rows = read_jsonl(source_path)
        accepted: list[dict[str, Any]] = []
        rejected = 0
        split_limit = limit_for_split(args, split)
        split_arrays = arrays_dir / split
        split_arrays.mkdir(parents=True, exist_ok=True)

        for row in rows:
            if split_limit > 0 and len(accepted) >= split_limit:
                break

            sample_id = str(row.get("sample_id") or f"seg{row.get('segment_id')}")
            text = str(row.get("text") or "").strip()
            audio_relpath = str(row.get("audio_relpath") or "").strip()
            audio_path = resolve_data_path(data_root, audio_relpath)
            reason = ""
            if not text:
                reason = "empty_text"
            elif not audio_relpath or not audio_path.exists():
                reason = "missing_audio"
            else:
                text_ids = tokenize(tts, text)
                if len(text_ids) == 0:
                    reason = "empty_text_tokens"
                elif len(text_ids) > args.max_text_tokens:
                    reason = f"text_tokens>{args.max_text_tokens}"
                else:
                    estimated_codes = math.ceil(float(row.get("audio_ms") or 0) / 1000.0 * 50.0)
                    if estimated_codes > effective_max_code_tokens * 1.15:
                        reason = f"estimated_code_tokens>{effective_max_code_tokens}"
                    else:
                        codes = encode_audio_codes(tts, audio_path, args.max_audio_sec)
                        if len(codes) > effective_max_code_tokens:
                            reason = f"code_tokens>{effective_max_code_tokens}"
                        else:
                            text_path = split_arrays / f"{sample_id}.text_ids.npy"
                            codes_path = split_arrays / f"{sample_id}.codes.npy"
                            if args.overwrite or not text_path.exists():
                                np.save(text_path, np.asarray(text_ids, dtype=np.int64))
                            if args.overwrite or not codes_path.exists():
                                np.save(codes_path, codes)
                            audio_sec = float(row.get("audio_ms") or 0) / 1000.0
                            target_sec = float(row.get("target_ms") or row.get("audio_ms") or 0) / 1000.0
                            record = {
                                "schema_version": SCHEMA_VERSION,
                                "sample_id": sample_id,
                                "segment_id": row.get("segment_id"),
                                "episode_id": row.get("episode_id"),
                                "job_id": row.get("job_id"),
                                "split": split,
                                "tier": row.get("tier"),
                                "text": text,
                                "source_text": row.get("source_text"),
                                "audio_relpath": audio_relpath,
                                "text_ids_path": str(text_path),
                                "codes_path": str(codes_path),
                                "condition_path": str(condition_path),
                                "emo_vec_path": str(emo_vec_path),
                                "text_len": len(text_ids),
                                "code_len": int(len(codes)),
                                "audio_ms": row.get("audio_ms"),
                                "target_ms": row.get("target_ms"),
                                "drift_ms": row.get("drift_ms"),
                                "duration_tokens_from_audio": max(
                                    1, int(round(audio_sec * args.duration_token_rate))
                                ),
                                "duration_tokens_from_target": max(
                                    1, int(round(target_sec * args.duration_token_rate))
                                ),
                                "duration_token_rate": args.duration_token_rate,
                            }
                            accepted.append(record)
                            print(
                                "[preprocess]",
                                split,
                                sample_id,
                                f"text={len(text_ids)}",
                                f"codes={len(codes)}",
                            )
            if reason:
                rejected += 1
                summary["rejections"].append(
                    {
                        "split": split,
                        "sample_id": sample_id,
                        "reason": reason,
                    }
                )

        feature_path = output_dir / f"{split}.features.jsonl"
        write_jsonl(feature_path, accepted)
        summary["splits"][split] = {
            "source_rows": len(rows),
            "accepted": len(accepted),
            "rejected_before_limit": rejected,
            "feature_path": str(feature_path),
        }

    (output_dir / "features_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary["splits"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
