from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_CONTROLLER_ATTR = "_holodub_lora_controller"
_IS_LORA_MODULE_ATTR = "_holodub_lora_conv1d"


def _torch_nn() -> tuple[Any, Any]:
    try:
        import torch
        from torch import nn
    except ImportError as exc:  # pragma: no cover - depends on real ML extras
        raise RuntimeError(
            "torch is required for IndexTTS2 LoRA candidate execution"
        ) from exc
    return torch, nn


def _get_child(parent: Any, name: str) -> Any:
    return parent._modules[name]


def _replace_child(parent: Any, name: str, module: Any) -> None:
    parent._modules[name] = module


def _find_parent(root: Any, module_name: str) -> tuple[Any, str]:
    parent = root
    parts = module_name.split(".")
    for part in parts[:-1]:
        parent = _get_child(parent, part)
    return parent, parts[-1]


def _new_lora_conv1d(base: Any, rank: int, alpha: float, dropout: float) -> Any:
    torch, nn = _torch_nn()
    if not hasattr(base, "weight"):
        raise TypeError(f"LoRA target has no weight: {type(base)!r}")
    if base.weight.ndim != 2:
        raise ValueError(f"LoRA target weight must be 2D, got {tuple(base.weight.shape)}")

    class LoraConv1D(nn.Module):
        def __init__(self) -> None:
            super().__init__()
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
            self.lora_enabled = False
            setattr(self, _IS_LORA_MODULE_ATTR, True)

        def forward(self, x: Any) -> Any:
            result = self.base(x)
            if not self.lora_enabled:
                return result
            lora_x = self.dropout(x).to(self.lora_A.dtype)
            delta = lora_x.matmul(self.lora_A).matmul(self.lora_B)
            return result + (delta * self.scaling).to(result.dtype)

    return LoraConv1D()


def _iter_lora_modules(model: Any) -> list[tuple[str, Any]]:
    return [
        (name, module)
        for name, module in model.named_modules()
        if getattr(module, _IS_LORA_MODULE_ATTR, False)
    ]


def _apply_lora(model: Any, target_regex: str, rank: int, alpha: float, dropout: float) -> list[str]:
    pattern = re.compile(target_regex)
    for param in model.parameters():
        param.requires_grad_(False)
    target_names = [
        name
        for name, module in model.named_modules()
        if pattern.match(name)
        and (
            module.__class__.__name__ == "Conv1D"
            or getattr(module, _IS_LORA_MODULE_ATTR, False)
        )
    ]
    if not target_names:
        raise RuntimeError(f"no Conv1D modules matched LoRA target regex: {target_regex}")
    for name in target_names:
        parent, child_name = _find_parent(model, name)
        base = _get_child(parent, child_name)
        if getattr(base, _IS_LORA_MODULE_ATTR, False):
            continue
        _replace_child(
            parent,
            child_name,
            _new_lora_conv1d(base, rank=rank, alpha=alpha, dropout=dropout),
        )
    return target_names


class LoraAdapterController:
    def __init__(self, *, adapter_path: Path, metadata: dict[str, Any], modules: list[Any]) -> None:
        self.adapter_path = adapter_path
        self.metadata = metadata
        self.modules = modules

    def set_enabled(self, enabled: bool) -> None:
        for module in self.modules:
            module.lora_enabled = enabled

    @contextlib.contextmanager
    def enabled(self) -> Iterator[None]:
        previous = [bool(getattr(module, "lora_enabled", False)) for module in self.modules]
        try:
            self.set_enabled(True)
            yield
        finally:
            for module, state in zip(self.modules, previous, strict=True):
                module.lora_enabled = state


def ensure_lora_adapter(tts: Any, adapter_path: str | Path) -> LoraAdapterController:
    torch, _ = _torch_nn()
    path = Path(adapter_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"IndexTTS2 LoRA adapter not found: {path}")

    gpt = getattr(tts, "gpt", None)
    if gpt is None:
        raise RuntimeError("IndexTTS2 model has no gpt module for LoRA")

    existing = getattr(gpt, _CONTROLLER_ATTR, None)
    if isinstance(existing, LoraAdapterController):
        if existing.adapter_path != path:
            raise RuntimeError(
                f"a different LoRA adapter is already loaded: {existing.adapter_path}"
            )
        existing.set_enabled(False)
        return existing

    payload = torch.load(path, map_location="cpu", weights_only=False)
    metadata = payload.get("metadata", {})
    target_regex = str(metadata.get("target_regex") or "")
    if not target_regex:
        raise ValueError(f"LoRA adapter metadata missing target_regex: {path}")
    rank = int(metadata.get("rank", 4))
    alpha = float(metadata.get("alpha", 16.0))
    target_names = _apply_lora(gpt, target_regex, rank=rank, alpha=alpha, dropout=0.0)
    module_payloads = payload["modules"]
    module_map = dict(_iter_lora_modules(gpt))
    for name in target_names:
        module = module_map[name]
        saved = module_payloads[name]
        module.lora_A.data.copy_(saved["lora_A"].to(module.lora_A.device, dtype=module.lora_A.dtype))
        module.lora_B.data.copy_(saved["lora_B"].to(module.lora_B.device, dtype=module.lora_B.dtype))

    device = getattr(tts, "device", None)
    if device is not None:
        gpt.to(device)
    if getattr(tts, "use_fp16", False):
        gpt.half()
    if hasattr(gpt, "post_init_gpt2_config"):
        gpt.post_init_gpt2_config(
            use_deepspeed=False,
            kv_cache=True,
            half=getattr(tts, "use_fp16", False),
        )
    gpt.eval()
    controller = LoraAdapterController(
        adapter_path=path,
        metadata=dict(metadata),
        modules=[module for _, module in _iter_lora_modules(gpt)],
    )
    controller.set_enabled(False)
    setattr(gpt, _CONTROLLER_ATTR, controller)
    return controller
