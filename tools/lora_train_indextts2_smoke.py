from __future__ import annotations

import argparse
import gc
import json
import math
import random
import re
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.optim import AdamW
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, Dataset


SCHEMA_VERSION = "holodub.indextts2_lora_adapter.v1"
DEFAULT_TARGET_REGEX = r"^gpt\.h\.\d+\.(attn\.c_attn|attn\.c_proj|mlp\.c_fc|mlp\.c_proj)$"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a small GPT-only LoRA adapter for IndexTTS2 from precomputed features."
    )
    parser.add_argument("--train-features", required=True)
    parser.add_argument("--val-features", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--cfg-path", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument("--rank", type=int, default=4)
    parser.add_argument("--alpha", type=float, default=16.0)
    parser.add_argument("--dropout", type=float, default=0.05)
    parser.add_argument("--target-regex", default=DEFAULT_TARGET_REGEX)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accumulation", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--text-loss-weight", type=float, default=0.2)
    parser.add_argument("--mel-loss-weight", type=float, default=0.8)
    parser.add_argument("--duration-control", action="store_true")
    parser.add_argument(
        "--duration-source",
        choices=("audio", "target"),
        default="audio",
        help="Use audio-derived or slot-target duration tokens from the feature cache.",
    )
    parser.add_argument("--duration-dropout", type=float, default=0.15)
    parser.add_argument(
        "--mel-stop-loss-weight",
        type=float,
        default=1.0,
        help="Extra loss weight for the stop mel token; values >1 discourage early/late stop drift.",
    )
    parser.add_argument(
        "--mel-end-loss-weight",
        type=float,
        default=1.0,
        help="Extra loss weight for the last mel tokens before stop.",
    )
    parser.add_argument(
        "--mel-end-window",
        type=int,
        default=0,
        help="Number of final non-stop mel tokens to reweight with --mel-end-loss-weight.",
    )
    parser.add_argument(
        "--duration-stop-bce-weight",
        type=float,
        default=0.0,
        help="Optional BCE loss weight for predicting the stop mel token only at the target stop position.",
    )
    parser.add_argument(
        "--duration-position-loss-weight",
        type=float,
        default=0.0,
        help="Optional loss weight that pulls the stop-logit probability center toward the target duration.",
    )
    parser.add_argument(
        "--duration-position-loss-window",
        type=int,
        default=16,
        help="Stop-logit positions earlier than target-window are treated as early-stop mass.",
    )
    parser.add_argument("--grad-clip", type=float, default=1.0)
    parser.add_argument("--log-interval", type=int, default=1)
    parser.add_argument("--eval-interval", type=int, default=10)
    parser.add_argument("--save-every", type=int, default=10)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--gradient-checkpointing", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


class FeatureDataset(Dataset):
    def __init__(self, path: Path, duration_source: str) -> None:
        self.path = path
        self.rows = read_jsonl(path)
        if not self.rows:
            raise ValueError(f"no feature rows in {path}")
        self.duration_source = duration_source

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        text_ids = np.load(row["text_ids_path"]).astype(np.int64)
        codes = np.load(row["codes_path"]).astype(np.int64)
        condition = np.load(row["condition_path"]).astype(np.float32)
        emo_vec = np.load(row["emo_vec_path"]).astype(np.float32)
        duration_key = (
            "duration_tokens_from_target"
            if self.duration_source == "target"
            else "duration_tokens_from_audio"
        )
        return {
            "sample_id": row["sample_id"],
            "text_ids": torch.from_numpy(text_ids).long(),
            "codes": torch.from_numpy(codes).long(),
            "condition": torch.from_numpy(condition).float(),
            "emo_vec": torch.from_numpy(emo_vec).float(),
            "duration_tokens": torch.tensor(int(row[duration_key]), dtype=torch.long),
            "text_length": torch.tensor(len(text_ids), dtype=torch.long),
            "code_length": torch.tensor(len(codes), dtype=torch.long),
        }


def collate_features(batch: list[dict[str, Any]]) -> dict[str, Any]:
    text_ids = pad_sequence([item["text_ids"] for item in batch], batch_first=True, padding_value=0)
    codes = pad_sequence([item["codes"] for item in batch], batch_first=True, padding_value=0)
    return {
        "sample_ids": [item["sample_id"] for item in batch],
        "text_ids": text_ids.long(),
        "codes": codes.long(),
        "condition": torch.stack([item["condition"] for item in batch], dim=0),
        "emo_vec": torch.stack([item["emo_vec"] for item in batch], dim=0),
        "duration_tokens": torch.stack([item["duration_tokens"] for item in batch], dim=0),
        "text_lengths": torch.stack([item["text_length"] for item in batch], dim=0),
        "code_lengths": torch.stack([item["code_length"] for item in batch], dim=0),
    }


class LoraConv1D(nn.Module):
    def __init__(self, base: nn.Module, rank: int, alpha: float, dropout: float) -> None:
        super().__init__()
        if not hasattr(base, "weight"):
            raise TypeError(f"LoRA target has no weight: {type(base)!r}")
        if base.weight.ndim != 2:
            raise ValueError(f"LoRA target weight must be 2D, got {tuple(base.weight.shape)}")
        self.base = base
        for param in self.base.parameters():
            param.requires_grad_(False)
        in_features = int(base.weight.shape[0])
        out_features = int(base.weight.shape[1])
        self.rank = rank
        self.alpha = float(alpha)
        self.scaling = self.alpha / float(rank)
        self.dropout = nn.Dropout(dropout)
        self.lora_A = nn.Parameter(torch.empty(in_features, rank, dtype=torch.float32))
        self.lora_B = nn.Parameter(torch.zeros(rank, out_features, dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        result = self.base(x)
        lora_x = self.dropout(x).to(self.lora_A.dtype)
        delta = torch.matmul(torch.matmul(lora_x, self.lora_A), self.lora_B)
        return result + (delta * self.scaling).to(result.dtype)


def get_child(parent: nn.Module, name: str) -> nn.Module:
    return parent._modules[name]


def replace_child(parent: nn.Module, name: str, module: nn.Module) -> None:
    parent._modules[name] = module


def find_parent(root: nn.Module, module_name: str) -> tuple[nn.Module, str]:
    parts = module_name.split(".")
    parent = root
    for part in parts[:-1]:
        parent = get_child(parent, part)
    return parent, parts[-1]


def apply_lora(model: nn.Module, target_regex: str, rank: int, alpha: float, dropout: float) -> list[str]:
    pattern = re.compile(target_regex)
    for param in model.parameters():
        param.requires_grad_(False)
    target_names = [
        name
        for name, module in model.named_modules()
        if pattern.match(name) and module.__class__.__name__ == "Conv1D"
    ]
    if not target_names:
        raise RuntimeError(f"no Conv1D modules matched target regex: {target_regex}")
    for name in target_names:
        parent, child_name = find_parent(model, name)
        base = get_child(parent, child_name)
        replace_child(parent, child_name, LoraConv1D(base, rank=rank, alpha=alpha, dropout=dropout))
    return target_names


def iter_lora_modules(model: nn.Module) -> list[tuple[str, LoraConv1D]]:
    return [(name, module) for name, module in model.named_modules() if isinstance(module, LoraConv1D)]


def save_adapter(path: Path, model: nn.Module, metadata: dict[str, Any]) -> None:
    modules: dict[str, dict[str, torch.Tensor]] = {}
    for name, module in iter_lora_modules(model):
        modules[name] = {
            "lora_A": module.lora_A.detach().cpu(),
            "lora_B": module.lora_B.detach().cpu(),
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "modules": modules,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(payload, path)


def release_non_gpt_modules(tts: Any) -> None:
    for attr in (
        "qwen_emo",
        "extract_features",
        "semantic_model",
        "semantic_mean",
        "semantic_std",
        "semantic_codec",
        "s2mel",
        "campplus_model",
        "bigvgan",
    ):
        if hasattr(tts, attr):
            setattr(tts, attr, None)
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def duration_control_embedding(
    model: nn.Module,
    duration_tokens: torch.Tensor,
    fallback: torch.Tensor,
    dropout: float,
) -> torch.Tensor:
    max_index = model.mel_pos_embedding.emb.num_embeddings - 1
    clamped = duration_tokens.clamp(min=1, max=max_index).long()
    duration_ctrl = model.mel_pos_embedding.emb(clamped)
    if dropout > 0:
        drop_mask = torch.rand(clamped.size(0), device=clamped.device) < dropout
        if drop_mask.any():
            duration_ctrl = torch.where(drop_mask.unsqueeze(1), fallback, duration_ctrl)
    return duration_ctrl


def compute_losses(
    model: nn.Module,
    batch: dict[str, torch.Tensor],
    device: torch.device,
    *,
    duration_control: bool,
    duration_dropout: float,
    mel_stop_loss_weight: float,
    mel_end_loss_weight: float,
    mel_end_window: int,
    duration_stop_bce_weight: float,
    duration_position_loss_weight: float,
    duration_position_loss_window: int,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, dict[str, float]]:
    model_dtype = model.text_embedding.weight.dtype
    condition = batch["condition"].to(device=device, dtype=model_dtype)
    text_ids = batch["text_ids"].to(device=device)
    codes = batch["codes"].to(device=device)
    emo_vec = batch["emo_vec"].to(device=device, dtype=model_dtype)
    text_lengths = batch["text_lengths"].to(device=device)
    code_lengths = batch["code_lengths"].to(device=device)
    duration_tokens = batch["duration_tokens"].to(device=device)

    batch_size = text_ids.size(0)
    use_speed = torch.zeros(batch_size, dtype=torch.long, device=device)

    text_inputs = model.set_text_padding(text_ids.clone(), text_lengths)
    text_inputs = F.pad(text_inputs, (0, 1), value=model.stop_text_token)
    text_inputs, text_targets = model.build_aligned_inputs_and_targets(
        text_inputs, model.start_text_token, model.stop_text_token
    )

    mel_inputs = model.set_mel_padding(codes.clone(), code_lengths)
    mel_inputs = F.pad(mel_inputs, (0, 1), value=model.stop_mel_token)
    mel_inputs, mel_targets = model.build_aligned_inputs_and_targets(
        mel_inputs, model.start_mel_token, model.stop_mel_token
    )

    duration_free = model.speed_emb(torch.zeros_like(use_speed))
    if duration_control:
        duration_ctrl = duration_control_embedding(
            model,
            duration_tokens,
            fallback=duration_free,
            dropout=duration_dropout if model.training else 0.0,
        )
    else:
        duration_ctrl = model.speed_emb(torch.ones_like(use_speed))
    conds = torch.cat(
        (condition + emo_vec.unsqueeze(1), duration_ctrl.unsqueeze(1), duration_free.unsqueeze(1)),
        dim=1,
    )

    text_emb = model.text_embedding(text_inputs) + model.text_pos_embedding(text_inputs)
    mel_emb = model.mel_embedding(mel_inputs) + model.mel_pos_embedding(mel_inputs)
    text_logits, mel_logits = model.get_logits(conds, text_emb, model.text_head, mel_emb, model.mel_head)

    text_mask = (
        torch.arange(text_targets.size(1), device=device).unsqueeze(0)
        < (text_lengths + 1).unsqueeze(1)
    )
    mel_mask = (
        torch.arange(mel_targets.size(1), device=device).unsqueeze(0)
        < (code_lengths + 1).unsqueeze(1)
    )

    text_ce = F.cross_entropy(text_logits.float(), text_targets, reduction="none")
    mel_ce = F.cross_entropy(mel_logits.float(), mel_targets, reduction="none")
    text_loss = (text_ce * text_mask).sum() / text_mask.sum().clamp_min(1)
    mel_weights = mel_mask.float()
    if mel_stop_loss_weight != 1.0 or (mel_end_window > 0 and mel_end_loss_weight != 1.0):
        for item_index, length in enumerate(code_lengths.tolist()):
            stop_index = int(length)
            if mel_end_window > 0 and mel_end_loss_weight != 1.0:
                end_start = max(0, stop_index - int(mel_end_window))
                mel_weights[item_index, end_start:stop_index] *= float(mel_end_loss_weight)
            if stop_index < mel_weights.size(1):
                mel_weights[item_index, stop_index] *= float(mel_stop_loss_weight)
    mel_loss = (mel_ce * mel_weights).sum() / mel_weights.sum().clamp_min(1.0)
    stop_bce_loss = mel_loss.new_zeros(())
    duration_position_loss = mel_loss.new_zeros(())
    stop_logits = None
    if duration_stop_bce_weight > 0 or duration_position_loss_weight > 0:
        stop_logits = mel_logits.float()[:, int(model.stop_mel_token), :]
    if duration_stop_bce_weight > 0 and stop_logits is not None:
        stop_targets = torch.zeros_like(stop_logits)
        for item_index, length in enumerate(code_lengths.tolist()):
            stop_index = int(length)
            if stop_index < stop_targets.size(1):
                stop_targets[item_index, stop_index] = 1.0
        stop_bce = F.binary_cross_entropy_with_logits(stop_logits, stop_targets, reduction="none")
        stop_bce_loss = (stop_bce * mel_mask.float()).sum() / mel_mask.sum().clamp_min(1.0)
    if duration_position_loss_weight > 0 and stop_logits is not None:
        positions = torch.arange(stop_logits.size(1), device=device).float().unsqueeze(0)
        valid_mask = mel_mask.bool()
        masked_logits = stop_logits.masked_fill(~valid_mask, -1.0e4)
        stop_probs = torch.softmax(masked_logits, dim=1)
        target_pos = code_lengths.float().clamp_min(1.0)
        expected_pos = (stop_probs * positions).sum(dim=1)
        relative_position_loss = F.smooth_l1_loss(
            expected_pos / target_pos,
            torch.ones_like(target_pos),
            reduction="mean",
        )
        early_cutoff = (code_lengths - int(duration_position_loss_window)).clamp_min(0)
        early_mask = positions < early_cutoff.float().unsqueeze(1)
        early_mass = (stop_probs * early_mask.float()).sum(dim=1).mean()
        duration_position_loss = relative_position_loss + early_mass

    with torch.no_grad():
        flat_logits = mel_logits.permute(0, 2, 1).reshape(-1, mel_logits.size(1))
        flat_targets = mel_targets.reshape(-1)
        flat_mask = mel_mask.reshape(-1)
        if flat_mask.any():
            valid_logits = flat_logits[flat_mask]
            valid_targets = flat_targets[flat_mask]
            mel_top1 = (valid_logits.argmax(dim=-1) == valid_targets).float().mean().item()
        else:
            mel_top1 = 0.0
    return (
        text_loss,
        mel_loss,
        stop_bce_loss,
        duration_position_loss,
        {
            "mel_top1": mel_top1,
            "stop_bce_loss": stop_bce_loss.item(),
            "duration_position_loss": duration_position_loss.item(),
        },
    )


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    *,
    duration_control: bool,
    duration_dropout: float,
    mel_stop_loss_weight: float,
    mel_end_loss_weight: float,
    mel_end_window: int,
    duration_stop_bce_weight: float,
    duration_position_loss_weight: float,
    duration_position_loss_window: int,
    use_amp: bool,
) -> dict[str, float]:
    model.eval()
    totals = {
        "text_loss": 0.0,
        "mel_loss": 0.0,
        "stop_bce_loss": 0.0,
        "duration_position_loss": 0.0,
        "mel_top1": 0.0,
        "count": 0,
    }
    for batch in loader:
        with torch.amp.autocast(device.type, enabled=use_amp, dtype=torch.float16):
            text_loss, mel_loss, stop_bce_loss, duration_position_loss, metrics = compute_losses(
                model,
                batch,
                device,
                duration_control=duration_control,
                duration_dropout=duration_dropout,
                mel_stop_loss_weight=mel_stop_loss_weight,
                mel_end_loss_weight=mel_end_loss_weight,
                mel_end_window=mel_end_window,
                duration_stop_bce_weight=duration_stop_bce_weight,
                duration_position_loss_weight=duration_position_loss_weight,
                duration_position_loss_window=duration_position_loss_window,
            )
        bsz = int(batch["text_ids"].size(0))
        totals["text_loss"] += text_loss.item() * bsz
        totals["mel_loss"] += mel_loss.item() * bsz
        totals["stop_bce_loss"] += stop_bce_loss.item() * bsz
        totals["duration_position_loss"] += duration_position_loss.item() * bsz
        totals["mel_top1"] += metrics["mel_top1"] * bsz
        totals["count"] += bsz
    count = max(1, int(totals["count"]))
    model.train()
    return {
        "text_loss": totals["text_loss"] / count,
        "mel_loss": totals["mel_loss"] / count,
        "stop_bce_loss": totals["stop_bce_loss"] / count,
        "duration_position_loss": totals["duration_position_loss"] / count,
        "mel_top1": totals["mel_top1"] / count,
    }


def append_metric(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    set_seed(args.seed)
    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True

    from indextts.infer_v2 import IndexTTS2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "metrics.jsonl"
    if metrics_path.exists():
        metrics_path.unlink()

    train_dataset = FeatureDataset(Path(args.train_features), args.duration_source)
    val_dataset = FeatureDataset(Path(args.val_features), args.duration_source)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        collate_fn=collate_features,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=collate_features,
    )

    print("[train] loading IndexTTS2")
    tts = IndexTTS2(
        model_dir=args.model_dir,
        cfg_path=args.cfg_path,
        use_fp16=not args.no_fp16,
        device=args.device,
        use_cuda_kernel=False,
    )
    model = tts.gpt
    release_non_gpt_modules(tts)
    device = torch.device(args.device or ("cuda" if torch.cuda.is_available() else "cpu"))
    model.to(device)
    if (not args.no_fp16) and device.type == "cuda":
        model.half()
    model.train()
    if hasattr(model.gpt, "config"):
        model.gpt.config.use_cache = False
    if args.gradient_checkpointing and hasattr(model.gpt, "gradient_checkpointing_enable"):
        model.gpt.gradient_checkpointing_enable()

    target_names = apply_lora(
        model,
        target_regex=args.target_regex,
        rank=args.rank,
        alpha=args.alpha,
        dropout=args.dropout,
    )
    model.to(device)
    for _, module in iter_lora_modules(model):
        module.lora_A.data = module.lora_A.data.to(device=device, dtype=torch.float32)
        module.lora_B.data = module.lora_B.data.to(device=device, dtype=torch.float32)
    lora_params = [param for param in model.parameters() if param.requires_grad]
    trainable_params = sum(param.numel() for param in lora_params)
    total_params = sum(param.numel() for param in model.parameters())
    print(
        f"[train] lora_modules={len(target_names)} trainable={trainable_params} total={total_params}"
    )

    optimizer = AdamW(lora_params, lr=args.lr, weight_decay=args.weight_decay)
    optimizer.zero_grad(set_to_none=True)
    use_amp = (not args.no_fp16) and device.type == "cuda"

    base_val = evaluate(
        model,
        val_loader,
        device,
        duration_control=args.duration_control,
        duration_dropout=args.duration_dropout,
        mel_stop_loss_weight=args.mel_stop_loss_weight,
        mel_end_loss_weight=args.mel_end_loss_weight,
        mel_end_window=args.mel_end_window,
        duration_stop_bce_weight=args.duration_stop_bce_weight,
        duration_position_loss_weight=args.duration_position_loss_weight,
        duration_position_loss_window=args.duration_position_loss_window,
        use_amp=use_amp,
    )
    print(
        "[val] step=0 "
        f"text_loss={base_val['text_loss']:.4f} mel_loss={base_val['mel_loss']:.4f} "
        f"duration_pos={base_val['duration_position_loss']:.4f} "
        f"mel_top1={base_val['mel_top1']:.4f}"
    )
    append_metric(metrics_path, {"phase": "val", "step": 0, **base_val})

    metadata: dict[str, Any] = {
        "rank": args.rank,
        "alpha": args.alpha,
        "dropout": args.dropout,
        "target_regex": args.target_regex,
        "target_names": target_names,
        "train_features": args.train_features,
        "val_features": args.val_features,
        "duration_control": args.duration_control,
        "duration_source": args.duration_source,
        "duration_dropout": args.duration_dropout,
        "mel_stop_loss_weight": args.mel_stop_loss_weight,
        "mel_end_loss_weight": args.mel_end_loss_weight,
        "mel_end_window": args.mel_end_window,
        "duration_stop_bce_weight": args.duration_stop_bce_weight,
        "duration_position_loss_weight": args.duration_position_loss_weight,
        "duration_position_loss_window": args.duration_position_loss_window,
        "trainable_params": trainable_params,
        "total_params": total_params,
    }

    global_step = 0
    last_loss = None
    while global_step < args.max_steps:
        for batch_index, batch in enumerate(train_loader):
            with torch.amp.autocast(device.type, enabled=use_amp, dtype=torch.float16):
                text_loss, mel_loss, stop_bce_loss, duration_position_loss, metrics = compute_losses(
                    model,
                    batch,
                    device,
                    duration_control=args.duration_control,
                    duration_dropout=args.duration_dropout,
                    mel_stop_loss_weight=args.mel_stop_loss_weight,
                    mel_end_loss_weight=args.mel_end_loss_weight,
                    mel_end_window=args.mel_end_window,
                    duration_stop_bce_weight=args.duration_stop_bce_weight,
                    duration_position_loss_weight=args.duration_position_loss_weight,
                    duration_position_loss_window=args.duration_position_loss_window,
                )
                loss = (
                    args.text_loss_weight * text_loss
                    + args.mel_loss_weight * mel_loss
                    + args.duration_stop_bce_weight * stop_bce_loss
                    + args.duration_position_loss_weight * duration_position_loss
                )
                loss = loss / max(1, args.grad_accumulation)

            loss.backward()
            if (batch_index + 1) % args.grad_accumulation == 0:
                if args.grad_clip > 0:
                    torch.nn.utils.clip_grad_norm_(lora_params, args.grad_clip)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                global_step += 1
                last_loss = {
                    "phase": "train",
                    "step": global_step,
                    "text_loss": text_loss.item(),
                    "mel_loss": mel_loss.item(),
                    "stop_bce_loss": stop_bce_loss.item(),
                    "duration_position_loss": duration_position_loss.item(),
                    "loss": loss.item() * max(1, args.grad_accumulation),
                    "mel_top1": metrics["mel_top1"],
                    "sample_ids": batch["sample_ids"],
                }
                append_metric(metrics_path, last_loss)
                if global_step % args.log_interval == 0:
                    print(
                        f"[train] step={global_step} "
                        f"text_loss={text_loss.item():.4f} mel_loss={mel_loss.item():.4f} "
                        f"stop_bce={stop_bce_loss.item():.4f} "
                        f"duration_pos={duration_position_loss.item():.4f} "
                        f"mel_top1={metrics['mel_top1']:.4f}"
                    )
                if args.eval_interval > 0 and global_step % args.eval_interval == 0:
                    val_metrics = evaluate(
                        model,
                        val_loader,
                        device,
                        duration_control=args.duration_control,
                        duration_dropout=args.duration_dropout,
                        mel_stop_loss_weight=args.mel_stop_loss_weight,
                        mel_end_loss_weight=args.mel_end_loss_weight,
                        mel_end_window=args.mel_end_window,
                        duration_stop_bce_weight=args.duration_stop_bce_weight,
                        duration_position_loss_weight=args.duration_position_loss_weight,
                        duration_position_loss_window=args.duration_position_loss_window,
                        use_amp=use_amp,
                    )
                    print(
                        f"[val] step={global_step} "
                        f"text_loss={val_metrics['text_loss']:.4f} "
                        f"mel_loss={val_metrics['mel_loss']:.4f} "
                        f"duration_pos={val_metrics['duration_position_loss']:.4f} "
                        f"mel_top1={val_metrics['mel_top1']:.4f}"
                    )
                    append_metric(metrics_path, {"phase": "val", "step": global_step, **val_metrics})
                if args.save_every > 0 and global_step % args.save_every == 0:
                    save_adapter(output_dir / f"adapter_step{global_step}.pt", model, metadata | {"step": global_step})
                    save_adapter(output_dir / "adapter_latest.pt", model, metadata | {"step": global_step})
                if global_step >= args.max_steps:
                    break
        if len(train_loader) == 0:
            raise RuntimeError("empty train loader")

    final_val = evaluate(
        model,
        val_loader,
        device,
        duration_control=args.duration_control,
        duration_dropout=args.duration_dropout,
        mel_stop_loss_weight=args.mel_stop_loss_weight,
        mel_end_loss_weight=args.mel_end_loss_weight,
        mel_end_window=args.mel_end_window,
        duration_stop_bce_weight=args.duration_stop_bce_weight,
        duration_position_loss_weight=args.duration_position_loss_weight,
        duration_position_loss_window=args.duration_position_loss_window,
        use_amp=use_amp,
    )
    print(
        f"[val] step={global_step} text_loss={final_val['text_loss']:.4f} "
        f"mel_loss={final_val['mel_loss']:.4f} "
        f"duration_pos={final_val['duration_position_loss']:.4f} "
        f"mel_top1={final_val['mel_top1']:.4f}"
    )
    append_metric(metrics_path, {"phase": "val", "step": global_step, **final_val})
    save_adapter(output_dir / "adapter_latest.pt", model, metadata | {"step": global_step})
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "steps": global_step,
                "baseline_val": base_val,
                "final_val": final_val,
                "last_train": last_loss,
                "adapter": str(output_dir / "adapter_latest.pt"),
                "metrics": str(metrics_path),
                "metadata": metadata,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
