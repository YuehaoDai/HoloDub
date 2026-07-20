from __future__ import annotations

import argparse
import json
import logging
import re
import types
import wave
from pathlib import Path
from typing import Any

import torch

from lora_train_indextts2_smoke import apply_lora, iter_lora_modules


logger = logging.getLogger(__name__)
_DURATION_PATCH_ATTR = "_holodub_duration_control_patch"
_DURATION_ORIGINAL_ATTR = "_holodub_original_inference_speech"
_EMOTION_CLAMP_PATCH_ATTR = "_holodub_emotion_clamp_patch"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate baseline/LoRA IndexTTS2 comparison samples.")
    parser.add_argument("--features", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--voice-prompt-relpath", default="voices/test_ref.wav")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cfg-path", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument("--max-samples", type=int, default=3)
    parser.add_argument("--sample-ids", default="", help="Comma-separated sample ids to synthesize.")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-lora", action="store_true")
    parser.add_argument("--num-beams", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--repetition-penalty", type=float, default=10.0)
    parser.add_argument("--length-penalty", type=float, default=0.0)
    parser.add_argument("--use-emo-text", action="store_true")
    parser.add_argument("--emo-text", default=None)
    parser.add_argument("--emo-vector", default="", help="Comma-separated 8-value IndexTTS2 emotion vector.")
    parser.add_argument("--emo-alpha", type=float, default=1.0)
    parser.add_argument("--max-text-tokens-per-segment", type=int, default=600)
    parser.add_argument("--max-mel-headroom", type=int, default=128)
    parser.add_argument(
        "--duration-control",
        action="store_true",
        help="Pass target_duration_tokens into IndexTTS2 GPT generation.",
    )
    parser.add_argument(
        "--duration-token-rate",
        type=float,
        default=23.4375,
        help="Token-rate used to convert target seconds to target_duration_tokens.",
    )
    parser.add_argument(
        "--duration-token-rate-policy",
        choices=("constant", "piecewise"),
        default="constant",
        help="Use a constant token-rate or the current pilot piecewise duration-rate calibration.",
    )
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def resolve_data_path(data_root: Path, relpath: str) -> Path:
    path = Path(relpath)
    if path.is_absolute():
        return path
    return data_root / relpath.replace("\\", "/")


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return frames / float(rate)


def target_duration_sec(row: dict[str, Any]) -> float:
    target_sec = float(row.get("target_ms") or 0) / 1000.0
    if target_sec <= 0:
        target_sec = float(row.get("audio_ms") or 0) / 1000.0
    return target_sec


def duration_token_rate_for_target(target_sec: float, args: argparse.Namespace) -> float:
    if args.duration_token_rate_policy == "piecewise":
        if target_sec < 15.0:
            return 23.4375
        if target_sec < 23.0:
            return 30.0
        return 32.5
    return float(args.duration_token_rate)


def parse_emo_vector(raw: str) -> list[float] | None:
    if not raw.strip():
        return None
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if len(values) != 8:
        raise ValueError(f"--emo-vector must contain 8 values, got {len(values)}")
    return values


def coerce_emotion_score(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        is_percent = raw.endswith("%")
        stripped = raw.strip("%")
        try:
            number = float(stripped)
            return number / 100.0 if is_percent else number
        except ValueError:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", stripped)
            if match:
                number = float(match.group(0))
                return number / 100.0 if is_percent else number
    return 0.0


def patch_emotion_clamp(tts: Any) -> None:
    qwen_emo = getattr(tts, "qwen_emo", None)
    if qwen_emo is None or getattr(qwen_emo, _EMOTION_CLAMP_PATCH_ATTR, False):
        return
    original = getattr(qwen_emo, "clamp_score", None)
    if original is None:
        return

    def patched_clamp_score(value: Any) -> Any:
        return original(coerce_emotion_score(value))

    qwen_emo.clamp_score = patched_clamp_score
    setattr(qwen_emo, _EMOTION_CLAMP_PATCH_ATTR, True)


def synthesize(
    tts: Any,
    voice_prompt: Path,
    row: dict[str, Any],
    output_path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    max_mel_tokens = max(64, int(row["code_len"]) + args.max_mel_headroom)
    generation_kwargs: dict[str, Any] = {}
    generation_info: dict[str, Any] = {
        "target_duration_tokens": None,
        "duration_token_rate": None,
        "max_mel_tokens": max_mel_tokens,
        "use_emo_text": args.use_emo_text,
        "emo_text": args.emo_text,
        "emo_vector": parse_emo_vector(args.emo_vector),
        "emo_alpha": args.emo_alpha,
    }
    if args.duration_control:
        target_sec = target_duration_sec(row)
        token_rate = duration_token_rate_for_target(target_sec, args)
        target_duration_tokens = max(1, int(round(max(0.05, target_sec) * token_rate)))
        generation_kwargs["target_duration_tokens"] = target_duration_tokens
        max_mel_tokens = max(max_mel_tokens, target_duration_tokens + args.max_mel_headroom)
        generation_info.update(
            {
                "target_duration_tokens": target_duration_tokens,
                "duration_token_rate": token_rate,
                "max_mel_tokens": max_mel_tokens,
            }
        )
    tts.infer(
        str(voice_prompt),
        row["text"],
        str(output_path),
        use_emo_text=args.use_emo_text,
        emo_text=args.emo_text,
        emo_vector=generation_info["emo_vector"],
        emo_alpha=args.emo_alpha,
        verbose=False,
        max_text_tokens_per_segment=args.max_text_tokens_per_segment,
        num_beams=args.num_beams,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        repetition_penalty=args.repetition_penalty,
        length_penalty=args.length_penalty,
        max_mel_tokens=max_mel_tokens,
        **generation_kwargs,
    )
    return generation_info


def enable_duration_control(tts: Any) -> None:
    if not patch_duration_control(tts):
        raise RuntimeError("failed to patch IndexTTS2 duration control")


def patch_duration_control(tts: Any) -> bool:
    gpt = getattr(tts, "gpt", None)
    if gpt is None:
        return False
    if getattr(gpt, _DURATION_PATCH_ATTR, False):
        return True

    original = getattr(gpt, "inference_speech", None)
    mel_pos_embedding = getattr(gpt, "mel_pos_embedding", None)
    mel_pos_emb = getattr(mel_pos_embedding, "emb", None)
    if original is None or mel_pos_emb is None:
        return False

    original_globals = getattr(getattr(original, "__func__", original), "__globals__", {})
    torch_mod = original_globals.get("torch")
    f_mod = original_globals.get("F")
    logits_processor_list = original_globals.get("LogitsProcessorList")
    typical_logits_warper = original_globals.get("TypicalLogitsWarper")
    model_logger = original_globals.get("logger", logger)
    if None in (torch_mod, f_mod, logits_processor_list, typical_logits_warper):
        return False

    def patched_inference_speech(
        self_gpt: Any,
        speech_condition: Any,
        text_inputs: Any,
        emo_speech_condition: Any = None,
        cond_lengths: Any = None,
        emo_cond_lengths: Any = None,
        emo_vec: Any = None,
        use_speed: Any = False,
        input_tokens: Any = None,
        num_return_sequences: int = 1,
        max_generate_length: Any = None,
        typical_sampling: bool = False,
        typical_mass: float = 0.9,
        **hf_generate_kwargs: Any,
    ) -> Any:
        target_duration_tokens = hf_generate_kwargs.pop("target_duration_tokens", None)
        if target_duration_tokens is None:
            return original(
                speech_condition,
                text_inputs,
                emo_speech_condition=emo_speech_condition,
                cond_lengths=cond_lengths,
                emo_cond_lengths=emo_cond_lengths,
                emo_vec=emo_vec,
                use_speed=use_speed,
                input_tokens=input_tokens,
                num_return_sequences=num_return_sequences,
                max_generate_length=max_generate_length,
                typical_sampling=typical_sampling,
                typical_mass=typical_mass,
                **hf_generate_kwargs,
            )

        if speech_condition.ndim == 2:
            speech_condition = speech_condition.unsqueeze(0)
        if emo_speech_condition is None:
            emo_speech_condition = speech_condition
        if cond_lengths is None:
            cond_lengths = torch_mod.tensor([speech_condition.shape[-1]], device=speech_condition.device)
        if emo_cond_lengths is None:
            emo_cond_lengths = torch_mod.tensor(
                [emo_speech_condition.shape[-1]], device=emo_speech_condition.device
            )

        speech_conditioning_latent = self_gpt.get_conditioning(
            speech_condition.transpose(1, 2), cond_lengths
        )
        if emo_vec is None:
            model_logger.debug("compute emo vec")
            emo_vec = self_gpt.get_emo_conditioning(
                emo_speech_condition.transpose(1, 2), emo_cond_lengths
            )
            emo_vec = self_gpt.emovec_layer(emo_vec)
            emo_vec = self_gpt.emo_layer(emo_vec)

        tmp = torch_mod.zeros(text_inputs.size(0), device=text_inputs.device)
        duration_free = self_gpt.speed_emb(torch_mod.zeros_like(tmp).long())
        duration_idx_value = max(1, min(int(target_duration_tokens), int(self_gpt.max_mel_tokens) - 1))
        duration_idx = torch_mod.full(
            (text_inputs.size(0),),
            duration_idx_value,
            device=text_inputs.device,
            dtype=torch_mod.long,
        )
        max_index = mel_pos_emb.num_embeddings - 1
        duration_ctrl = mel_pos_emb(duration_idx.clamp(max=max_index).long())
        conds_latent = torch_mod.cat(
            (
                speech_conditioning_latent + emo_vec.unsqueeze(1),
                duration_ctrl.unsqueeze(1),
                duration_free.unsqueeze(1),
            ),
            1,
        )
        input_ids, inputs_embeds, attention_mask = self_gpt.prepare_gpt_inputs(
            conds_latent, text_inputs
        )
        self_gpt.inference_model.store_mel_emb(inputs_embeds)
        if input_tokens is None:
            inputs = input_ids
        else:
            if input_tokens.ndim == 1:
                input_tokens = input_tokens.unsqueeze(0)
            repeat_count = num_return_sequences // input_ids.shape[0]
            if repeat_count > 1:
                input_ids = input_ids.repeat(repeat_count, 1)
                attention_mask = attention_mask.repeat(repeat_count, 1)
            input_tokens = input_tokens.repeat(num_return_sequences // input_tokens.shape[0], 1)
            inputs = torch_mod.cat([input_ids, input_tokens], dim=1)
            attention_mask = f_mod.pad(attention_mask, (0, input_tokens.shape[1]), value=1)

        trunc_index = inputs.shape[1]
        logits_processor = logits_processor_list()
        if typical_sampling:
            if not (0.0 < typical_mass < 1.0):
                raise ValueError(f"`typical_mass` must be > 0 and < 1, got {typical_mass}")
            min_tokens_to_keep = 2 if hf_generate_kwargs.get("num_beams", 1) > 1 else 1
            logits_processor.append(
                typical_logits_warper(mass=typical_mass, min_tokens_to_keep=min_tokens_to_keep)
            )

        max_length = (
            (trunc_index + self_gpt.max_mel_tokens - 1)
            if max_generate_length is None
            else trunc_index + max_generate_length
        )
        if self_gpt.accel_engine is not None and num_return_sequences == 1:
            output = self_gpt.accel_engine.generate(
                inputs,
                max_new_tokens=max_length - trunc_index,
                attention_mask=attention_mask,
                temperature=hf_generate_kwargs.get("temperature", 1),
                stop_tokens=[self_gpt.stop_mel_token],
                tts_embeddings=inputs_embeds,
                tts_mel_embedding=self_gpt.inference_model.embeddings,
                tts_text_pos_embedding=self_gpt.inference_model.text_pos_embedding,
            )
        else:
            output = self_gpt.inference_model.generate(
                inputs,
                bos_token_id=self_gpt.start_mel_token,
                pad_token_id=self_gpt.stop_mel_token,
                eos_token_id=self_gpt.stop_mel_token,
                attention_mask=attention_mask,
                max_length=max_length,
                logits_processor=logits_processor,
                num_return_sequences=num_return_sequences,
                **hf_generate_kwargs,
            )
        if isinstance(output, torch_mod.Tensor):
            return output[:, trunc_index:], speech_conditioning_latent
        output.sequences = output.sequences[:, trunc_index:]
        return output, speech_conditioning_latent

    setattr(gpt, _DURATION_ORIGINAL_ATTR, original)
    gpt.inference_speech = types.MethodType(patched_inference_speech, gpt)
    setattr(gpt, _DURATION_PATCH_ATTR, True)
    return True


def load_adapter(tts: Any, adapter_path: Path) -> dict[str, Any]:
    payload = torch.load(adapter_path, map_location="cpu", weights_only=False)
    metadata = payload.get("metadata", {})
    target_regex = metadata.get("target_regex")
    rank = int(metadata.get("rank", 4))
    alpha = float(metadata.get("alpha", 16.0))
    target_names = apply_lora(tts.gpt, target_regex, rank=rank, alpha=alpha, dropout=0.0)
    module_payloads = payload["modules"]
    module_map = dict(iter_lora_modules(tts.gpt))
    for name in target_names:
        module = module_map[name]
        saved = module_payloads[name]
        module.lora_A.data.copy_(saved["lora_A"].to(module.lora_A.device, dtype=module.lora_A.dtype))
        module.lora_B.data.copy_(saved["lora_B"].to(module.lora_B.device, dtype=module.lora_B.dtype))
    tts.gpt.to(tts.device)
    if getattr(tts, "use_fp16", False):
        tts.gpt.half()
    if hasattr(tts.gpt, "post_init_gpt2_config"):
        tts.gpt.post_init_gpt2_config(use_deepspeed=False, kv_cache=True, half=getattr(tts, "use_fp16", False))
    tts.gpt.eval()
    return metadata


def main() -> int:
    args = parse_args()
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    from indextts.infer_v2 import IndexTTS2

    features = read_jsonl(Path(args.features))
    selected_ids = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
    if selected_ids:
        rows = [row for row in features if row["sample_id"] in selected_ids]
    else:
        rows = features[: args.max_samples]
    if not rows:
        raise ValueError("no rows selected for inference")
    if args.skip_baseline and args.skip_lora:
        raise ValueError("cannot skip both baseline and LoRA")

    data_root = Path(args.data_root)
    voice_prompt = resolve_data_path(data_root, args.voice_prompt_relpath)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("[infer] loading IndexTTS2")
    tts = IndexTTS2(
        model_dir=args.model_dir,
        cfg_path=args.cfg_path,
        use_fp16=not args.no_fp16,
        device=args.device,
        use_cuda_kernel=False,
    )
    if args.duration_control:
        enable_duration_control(tts)
    patch_emotion_clamp(tts)
    print("[infer] model loaded")

    report: list[dict[str, Any]] = []
    if not args.skip_baseline:
        for row in rows:
            sample_id = row["sample_id"]
            baseline_path = output_dir / f"{sample_id}-baseline.wav"
            print(f"[infer] baseline {sample_id}")
            generation_info = synthesize(tts, voice_prompt, row, baseline_path, args)
            report.append(
                {
                    "sample_id": sample_id,
                    "variant": "baseline",
                    "path": str(baseline_path),
                    "duration_sec": wav_duration_sec(baseline_path),
                    "teacher_sec": float(row.get("audio_ms") or 0) / 1000.0,
                    "target_sec": float(row.get("target_ms") or 0) / 1000.0,
                    "code_len": row.get("code_len"),
                    "text_len": row.get("text_len"),
                    **generation_info,
                }
            )

    metadata: dict[str, Any] = {}
    if not args.skip_lora:
        metadata = load_adapter(tts, Path(args.adapter))
        if args.duration_control:
            enable_duration_control(tts)
        for row in rows:
            sample_id = row["sample_id"]
            lora_path = output_dir / f"{sample_id}-lora.wav"
            print(f"[infer] lora {sample_id}")
            generation_info = synthesize(tts, voice_prompt, row, lora_path, args)
            report.append(
                {
                    "sample_id": sample_id,
                    "variant": "lora",
                    "path": str(lora_path),
                    "duration_sec": wav_duration_sec(lora_path),
                    "teacher_sec": float(row.get("audio_ms") or 0) / 1000.0,
                    "target_sec": float(row.get("target_ms") or 0) / 1000.0,
                    "code_len": row.get("code_len"),
                    "text_len": row.get("text_len"),
                    **generation_info,
                }
            )

    payload = {
        "features": args.features,
        "adapter": args.adapter,
        "adapter_metadata": metadata,
        "duration_control": args.duration_control,
        "duration_token_rate": args.duration_token_rate,
        "duration_token_rate_policy": args.duration_token_rate_policy,
        "seed": args.seed,
        "generation": {
            "num_beams": args.num_beams,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "repetition_penalty": args.repetition_penalty,
            "length_penalty": args.length_penalty,
            "use_emo_text": args.use_emo_text,
            "emo_text": args.emo_text,
            "emo_vector": parse_emo_vector(args.emo_vector),
            "emo_alpha": args.emo_alpha,
        },
        "rows": report,
    }
    (output_dir / "inference_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
