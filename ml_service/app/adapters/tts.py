from __future__ import annotations

import base64
import gc
import hashlib
import json
import logging
import math
import os
import random
import re
import shlex
import struct
import subprocess
import threading
import types
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.adapters.media import probe_duration
from app.config import Settings
from app.models import TTSRequest, TTSResponse
from app.storage import ensure_parent, resolve_data_path

logger = logging.getLogger(__name__)


def _prefer_local_hf_cache_for_indextts2() -> None:
    hf_home = Path(os.environ.get("HF_HOME") or "~/.cache/huggingface").expanduser()
    cached = hf_home / "hub" / "models--facebook--w2v-bert-2.0"
    if not cached.exists():
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class UnsupportedTTSBackendError(RuntimeError):
    """Raised when ml_tts_backend is set to a value the adapter does not support.

    The previous behaviour silently fell back to generating silent WAV files,
    which masked configuration mistakes (a worker would persist 'successful'
    silent segments). Now we surface the error so operators can fix the env.
    """

    def __init__(self, backend: str, supported: tuple[str, ...]) -> None:
        self.backend = backend
        self.supported = supported
        super().__init__(
            f"unsupported ML_TTS_BACKEND={backend!r}; "
            f"expected one of {list(supported)}"
        )


_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC = 30 * 60.0
"""Maximum number of seconds a non-loader caller will wait for the in-flight
IndexTTS2 load before giving up with ``TimeoutError``.

This caps the worst-case latency of a synthesis request that arrives during
warm-up: instead of blocking forever (and bottlenecking the entire worker),
the request fails fast with a 503-mappable error. The watchdog in
``app.main`` will independently flip the warm-up status to ``error`` if the
loader thread vanishes, so subsequent requests will fail fast as well.
"""

_REFERENCE_PROMPT_MAX_ORIGINAL_SEC = 20.0
_REFERENCE_PROMPT_CLIP_SEC = 12.0
_REFERENCE_PROMPT_HOP_SEC = 1.0
_REFERENCE_PROMPT_CACHE_VERSION = "stable-v2"
_LONG_TEXT_CHUNK_MIN_CHARS = 120
_LONG_TEXT_CHUNK_TARGET_CHARS = 72
_LONG_TEXT_CHUNK_MAX_CHARS = 110
_LONG_TEXT_CHUNK_GAP_SEC = 0.08
_INDEXTTS2_DURATION_PATCH_ATTR = "_holodub_duration_control_patch"
_INDEXTTS2_DURATION_ORIGINAL_ATTR = "_holodub_original_inference_speech"


@dataclass(frozen=True)
class _LoraCandidatePolicy:
    present: bool = False
    schema_version: Any | None = None
    enabled: bool = False
    phase: str = ""
    execution_mode: str = ""
    execution_enabled: bool = False
    audio_selection_mode: str = ""
    policy_version: str = ""
    offline_baseline_id: str = ""
    selected_family: str = "default"
    fallback_reason: str = ""
    split_planning: bool = False
    dense_eligible: bool = False
    near_long_eligible: bool = False
    short_dense_eligible: bool = False
    short_transition_eligible: bool = False
    short_term_number_eligible: bool = False
    retry_escalation_eligible: bool = False
    candidate_families: list[Any] = field(default_factory=list)
    split_policy: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_meta(cls, dubbing_meta: dict[str, Any] | None) -> "_LoraCandidatePolicy":
        if not isinstance(dubbing_meta, dict):
            return cls()
        candidate = dubbing_meta.get("lora_candidate")
        if not isinstance(candidate, dict):
            return cls()
        families = candidate.get("candidate_families")
        split_policy = candidate.get("split_policy")
        gate = candidate.get("gate")
        eligibility = candidate.get("eligibility")
        if not isinstance(eligibility, dict):
            eligibility = {}
        return cls(
            present=True,
            schema_version=candidate.get("schema_version"),
            enabled=bool(candidate.get("enabled")),
            phase=str(candidate.get("phase") or "unknown"),
            execution_mode=str(candidate.get("execution_mode") or "unknown"),
            execution_enabled=bool(candidate.get("candidate_execution_enabled")),
            audio_selection_mode=str(candidate.get("audio_selection_mode") or ""),
            policy_version=str(candidate.get("policy_version") or ""),
            offline_baseline_id=str(candidate.get("offline_baseline_id") or ""),
            selected_family=str(candidate.get("selected_family") or "default"),
            fallback_reason=str(candidate.get("fallback_reason") or ""),
            split_planning=bool(candidate.get("split_planning")),
            dense_eligible=bool(eligibility.get("dense_eligible")),
            near_long_eligible=bool(eligibility.get("near_long_eligible")),
            short_dense_eligible=bool(eligibility.get("short_dense_eligible")),
            short_transition_eligible=bool(eligibility.get("short_transition_eligible")),
            short_term_number_eligible=bool(eligibility.get("short_term_number_eligible")),
            retry_escalation_eligible=bool(eligibility.get("retry_escalation_eligible")),
            candidate_families=families if isinstance(families, list) else [],
            split_policy=split_policy if isinstance(split_policy, dict) else {},
            gate=gate if isinstance(gate, dict) else {},
        )

    @property
    def should_execute_candidates(self) -> bool:
        return (
            self.present
            and self.enabled
            and self.execution_enabled
            and self.audio_selection_mode == "candidate_audio"
        )

    @property
    def join_quality_required(self) -> str:
        if "join_quality_required" not in self.split_policy:
            return ""
        return str(bool(self.split_policy.get("join_quality_required"))).lower()

    @property
    def max_drift_ms(self) -> str:
        if self.gate.get("max_drift_ms") is None:
            return ""
        return str(self.gate.get("max_drift_ms"))

    @property
    def max_fit_delta(self) -> str:
        if self.gate.get("max_fit_delta") is None:
            return ""
        return str(self.gate.get("max_fit_delta"))

    def diagnostic(self) -> str:
        if not self.present:
            return ""
        diag = (
            f" lora_candidate={self.phase}"
            f" mode={self.execution_mode}"
            f" enabled={str(self.enabled).lower()}"
            f" split={str(self.split_planning).lower()}"
            f" selected={self.selected_family}"
            f" families={len(self.candidate_families)}"
        )
        if self.schema_version is not None:
            diag += f" schema={self.schema_version}"
        if self.policy_version:
            diag += f" policy={self.policy_version}"
        if self.offline_baseline_id:
            diag += f" baseline={self.offline_baseline_id}"
        if self.audio_selection_mode:
            diag += f" audio={self.audio_selection_mode}"
        if self.dense_eligible:
            diag += " dense=true"
        if self.near_long_eligible:
            diag += " near_long=true"
        if self.short_dense_eligible:
            diag += " short_dense=true"
        if self.short_transition_eligible:
            diag += " short_transition=true"
        if self.short_term_number_eligible:
            diag += " short_term_number=true"
        if self.retry_escalation_eligible:
            diag += " retry_escalation=true"
        if self.join_quality_required:
            diag += f" join_required={self.join_quality_required}"
        if self.max_drift_ms:
            diag += f" max_drift_ms={self.max_drift_ms}"
        if self.max_fit_delta:
            diag += f" max_fit_delta={self.max_fit_delta}"
        if self.fallback_reason:
            diag += f" fallback={self.fallback_reason}"
        return diag


def _meta_setting(
    settings: Any,
    generation_meta: dict[str, Any] | None,
    meta_key: str,
    settings_key: str,
    default: Any,
) -> Any:
    if isinstance(generation_meta, dict) and meta_key in generation_meta:
        return generation_meta.get(meta_key)
    return getattr(settings, settings_key, default)


def _indextts2_duration_control_mode(
    settings: Any,
    generation_meta: dict[str, Any] | None = None,
) -> str:
    raw = _meta_setting(
        settings,
        generation_meta,
        "duration_control_mode",
        "indextts2_duration_control_mode",
        "off",
    )
    mode = str(raw or "off")
    mode = mode.strip().lower()
    if mode in {"", "0", "false", "none", "off"}:
        return "off"
    if mode in {"embedding", "duration_embedding", "community"}:
        return "embedding"
    logger.warning("unknown INDEXTTS2_DURATION_CONTROL_MODE=%r; using off", mode)
    return "off"


# Lower bound for the adaptive tokens-per-char blend used by the text token
# budget. Without it, a single truncated or over-rushed observation (e.g. a
# generation that was cut by the mel-token cap) feeds a tiny observed rate
# back into the next attempt's budget, which caps the next generation even
# harder — a ratchet that can pin a segment's output far below target
# regardless of the text. Observed rates below half the prior are treated as
# poisoned calibration rather than signal; the blend may never sink below
# floor_ratio * prior. See docs/mit6824-research-journal-2026-07-21.md (R1).
TOKENS_PER_CHAR_FLOOR_RATIO: float = 0.5


def _blend_tokens_per_char(
    observed: float | None,
    prior: float,
    *,
    observed_weight: float = 0.6,
    floor_ratio: float = TOKENS_PER_CHAR_FLOOR_RATIO,
) -> float:
    """Blend an observed tokens-per-char rate with the prior, floored.

    observed=None (or non-positive) returns the prior untouched. Otherwise
    returns observed_weight*observed + (1-observed_weight)*prior, but never
    less than floor_ratio*prior so a poisoned observation cannot collapse the
    text token budget across retries.
    """
    if observed is None or observed <= 0 or prior <= 0:
        return prior
    blended = observed_weight * observed + (1.0 - observed_weight) * prior
    return max(blended, floor_ratio * prior)


def _indextts2_constant_duration_token_rate(
    settings: Any,
    generation_meta: dict[str, Any] | None = None,
) -> float:
    try:
        rate = float(
            _meta_setting(
                settings,
                generation_meta,
                "duration_token_rate",
                "indextts2_duration_token_rate",
                50.0,
            )
            or 50.0
        )
    except (TypeError, ValueError):
        return 50.0
    if rate <= 0:
        return 50.0
    return rate


def _indextts2_duration_token_rate(
    settings: Any,
    target_sec: float | None = None,    generation_meta: dict[str, Any] | None = None,
) -> float:
    constant_rate = _indextts2_constant_duration_token_rate(settings, generation_meta)
    policy = str(
        _meta_setting(
            settings,
            generation_meta,
            "duration_token_rate_policy",
            "indextts2_duration_token_rate_policy",
            "constant",
        )
        or "constant"
    ).strip().lower()
    if policy in {"", "constant", "fixed"} or target_sec is None:
        return constant_rate
    if policy != "piecewise":
        logger.warning("unknown INDEXTTS2_DURATION_TOKEN_RATE_POLICY=%r; using constant", policy)
        return constant_rate

    spec = str(
        _meta_setting(
            settings,
            generation_meta,
            "duration_piecewise_token_rates",
            "indextts2_duration_piecewise_token_rates",
            "15:23.4375,23:30,default:32.5",
        )
        or ""
    )
    fallback = constant_rate
    for item in spec.split(","):
        if not item.strip() or ":" not in item:
            continue
        raw_limit, raw_rate = [part.strip() for part in item.split(":", 1)]
        try:
            rate = float(raw_rate)
        except ValueError:
            continue
        if rate <= 0:
            continue
        if raw_limit.lower() in {"*", "default", "else", "inf", "infinity"}:
            fallback = rate
            continue
        try:
            limit_sec = float(raw_limit)
        except ValueError:
            continue
        if target_sec < limit_sec:
            return rate
    return fallback


def _indextts2_duration_min_sec(
    settings: Any,
    generation_meta: dict[str, Any] | None = None,
) -> float:
    try:
        min_sec = float(
            _meta_setting(
                settings,
                generation_meta,
                "duration_control_min_sec",
                "indextts2_duration_control_min_sec",
                15.0,
            )
            or 0.0
        )
    except (TypeError, ValueError):
        return 15.0
    return max(0.0, min_sec)


def _indextts2_duration_tokens(
    settings: Any,
    target_sec: float,
    generation_meta: dict[str, Any] | None = None,
) -> int:
    rate = _indextts2_duration_token_rate(settings, target_sec, generation_meta)
    return max(1, int(round(max(0.05, target_sec) * rate)))


def _indextts2_duration_max_tokens(
    settings: Any,
    *,
    current_max_mel_tokens: int,
    target_duration_tokens: int,
    max_allowed_sec: float,
    target_sec: float | None = None,
    generation_meta: dict[str, Any] | None = None,
) -> int:
    rate = _indextts2_duration_token_rate(
        settings,
        target_sec or max_allowed_sec,
        generation_meta,
    )
    allowed_tokens = int(max(0.05, max_allowed_sec) * rate)
    if allowed_tokens <= 0:
        allowed_tokens = target_duration_tokens
    target_with_headroom = target_duration_tokens + min(128, max(24, target_duration_tokens // 4))
    return max(
        64,
        min(
            max(current_max_mel_tokens, target_with_headroom),
            max(target_duration_tokens, allowed_tokens),
            4096,
        ),
    )


def _apply_generation_seed(generation_meta: dict[str, Any]) -> str:
    if "seed" not in generation_meta:
        return ""
    try:
        seed = int(generation_meta["seed"])
    except (TypeError, ValueError):
        return " seed=invalid"
    random.seed(seed)
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except Exception as exc:  # noqa: BLE001
        return f" seed={seed} seed_warn={exc}"
    return f" seed={seed}"


def _patch_indextts2_duration_control(tts: Any) -> bool:
    """Enable JarodMica-style duration embeddings on an IndexTTS2 instance.

    The public IndexTTS2 release already accepts unknown generation kwargs in
    infer_v2.py, but its GPT method forwards them into HuggingFace generate().
    This patch consumes target_duration_tokens first and swaps the second
    conditioning token from the learned speed embedding to a mel-position
    embedding at the requested semantic-token length.
    """
    gpt = getattr(tts, "gpt", None)
    if gpt is None:
        logger.warning("IndexTTS2 duration control requested, but model.gpt is missing")
        return False
    if getattr(gpt, _INDEXTTS2_DURATION_PATCH_ATTR, False):
        return True

    original = getattr(gpt, "inference_speech", None)
    if original is None:
        logger.warning("IndexTTS2 duration control requested, but inference_speech is missing")
        return False

    mel_pos_embedding = getattr(gpt, "mel_pos_embedding", None)
    mel_pos_emb = getattr(mel_pos_embedding, "emb", None)
    if mel_pos_emb is None:
        logger.warning("IndexTTS2 duration control requested, but mel_pos_embedding.emb is missing")
        return False

    original_globals = getattr(getattr(original, "__func__", original), "__globals__", {})
    torch = original_globals.get("torch")
    F = original_globals.get("F")
    LogitsProcessorList = original_globals.get("LogitsProcessorList")
    TypicalLogitsWarper = original_globals.get("TypicalLogitsWarper")
    model_logger = original_globals.get("logger", logger)
    if torch is None or F is None or LogitsProcessorList is None or TypicalLogitsWarper is None:
        logger.warning("IndexTTS2 duration control requested, but GPT globals are incomplete")
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
            cond_lengths = torch.tensor([speech_condition.shape[-1]], device=speech_condition.device)
        if emo_cond_lengths is None:
            emo_cond_lengths = torch.tensor([emo_speech_condition.shape[-1]], device=speech_condition.device)

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
        else:
            model_logger.debug("Use the specified emotion vector")

        tmp = torch.zeros(text_inputs.size(0), device=text_inputs.device)
        duration_free = self_gpt.speed_emb(torch.zeros_like(tmp).long())
        duration_idx_value = int(target_duration_tokens)
        duration_idx_value = max(1, min(duration_idx_value, int(self_gpt.max_mel_tokens) - 1))
        duration_idx = torch.full(
            (text_inputs.size(0),),
            duration_idx_value,
            device=text_inputs.device,
            dtype=torch.long,
        )
        max_index = mel_pos_emb.num_embeddings - 1
        duration_ctrl = mel_pos_emb(duration_idx.clamp(max=max_index).long())
        conds_latent = torch.cat(
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
            assert num_return_sequences % input_tokens.shape[0] == 0, (
                "The num_return_sequences must be divisible by the batch number of input_tokens"
            )
            assert num_return_sequences % text_inputs.shape[0] == 0, (
                "The num_return_sequences must be divisible by the batch number of text_inputs"
            )
            repeat_count = num_return_sequences // input_ids.shape[0]
            if repeat_count > 1:
                input_ids = input_ids.repeat(repeat_count, 1)
                attention_mask = attention_mask.repeat(repeat_count, 1)
            input_tokens = input_tokens.repeat(num_return_sequences // input_tokens.shape[0], 1)
            inputs = torch.cat([input_ids, input_tokens], dim=1)
            attention_mask = F.pad(attention_mask, (0, input_tokens.shape[1]), value=1)

        trunc_index = inputs.shape[1]
        logits_processor = LogitsProcessorList()
        if typical_sampling:
            if not (0.0 < typical_mass < 1.0):
                raise ValueError(
                    f"`typical_mass` has to be a float > 0 and < 1, but is {typical_mass}"
                )
            min_tokens_to_keep = 2 if hf_generate_kwargs.get("num_beams", 1) > 1 else 1
            logits_processor.append(
                TypicalLogitsWarper(mass=typical_mass, min_tokens_to_keep=min_tokens_to_keep)
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
        if isinstance(output, torch.Tensor):
            return output[:, trunc_index:], speech_conditioning_latent
        output.sequences = output.sequences[:, trunc_index:]
        return output, speech_conditioning_latent

    setattr(gpt, _INDEXTTS2_DURATION_ORIGINAL_ATTR, original)
    gpt.inference_speech = types.MethodType(patched_inference_speech, gpt)
    setattr(gpt, _INDEXTTS2_DURATION_PATCH_ATTR, True)
    logger.info("enabled experimental IndexTTS2 duration embedding patch")
    return True


class TTSAdapter:
    """Thin facade over the configured TTS backend.

    Concurrency model for IndexTTS2 inline mode:
        - The state machine has four states: ``idle -> loading -> ready``
          or ``idle -> loading -> error``. Transitions are protected by a
          short critical section (``_state_lock``); the GPU-heavy
          ``IndexTTS2(...)`` construction itself runs *outside* any lock
          so a crash, segfault, or external SIGKILL of the loader thread
          can never leak a permanently-held mutex.
        - The first caller to observe ``status == idle`` becomes the
          *loader* and atomically flips it to ``loading``. Concurrent
          callers become *waiters* and block on a ``threading.Event``
          with a hard timeout (``_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC``).
        - On success the loader publishes the model handle then sets
          status to ``ready`` and signals the event. On failure it
          records the exception, sets status to ``error``, and still
          signals the event so waiters do not deadlock.
        - After a terminal ``error`` callers are allowed to retry; the
          next caller to acquire the state lock will reset the state
          machine to ``loading`` and attempt the load again.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._indextts2_warmup_status: str = "idle"
        self._indextts2_model: Any | None = None
        self._state_lock = threading.Lock()
        self._load_done = threading.Event()
        self._indextts2_load_error: Exception | None = None

    def backend_name(self) -> str:
        return self.settings.ml_tts_backend

    def get_indextts2_warmup_status(self) -> str:
        """Return 'idle'|'loading'|'ready'|'error' for IndexTTS2 inline mode."""
        return self._indextts2_warmup_status

    def force_indextts2_load_error(self, reason: str) -> None:
        """Mark IndexTTS2 warm-up as failed from outside the loader.

        Used by the lifespan watchdog when it observes a vanished loader
        thread or a hard timeout. The transition is no-op when the
        status is already terminal (``ready`` / ``error``) so multiple
        concurrent watchdogs cannot stomp on each other.
        """
        with self._state_lock:
            if self._indextts2_warmup_status in ("ready", "error"):
                return
            self._indextts2_warmup_status = "error"
            self._indextts2_load_error = RuntimeError(reason)
        # Wake any waiters so they fail-fast instead of blocking until the
        # ``_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC`` ceiling.
        self._load_done.set()
        logger.error("IndexTTS2 warm-up forced to error: %s", reason)

    def is_indextts2_inline_enabled(self) -> bool:
        return (
            self.settings.ml_tts_backend == "indextts2"
            and self.settings.indextts2_inline
        )

    def warm_up_indextts2(self) -> None:
        """Eagerly load IndexTTS2 (synchronously). No-op if not in inline mode.

        Intended to be called from FastAPI lifespan in a background
        thread; the loader itself blocks until the model is ready or an
        error is raised. Exceptions are logged but never propagated so a
        broken environment does not crash the FastAPI startup sequence
        — ``/readyz`` will surface the failure instead.
        """
        if not self.is_indextts2_inline_enabled():
            return
        try:
            self.ensure_indextts2_loaded()
        except Exception as exc:  # noqa: BLE001
            logger.exception("IndexTTS2 warm-up failed: %s", exc)

    def release_indextts2(self) -> bool:
        """Drop the cached inline IndexTTS2 model and best-effort free memory."""
        with self._state_lock:
            model = self._indextts2_model
            if model is None:
                return False
            self._indextts2_model = None
            self._indextts2_warmup_status = "idle"
            self._indextts2_load_error = None
            self._load_done.clear()

        del model
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("IndexTTS2 release memory cleanup failed: %s", exc)
        logger.info("IndexTTS2 released; warmup_status=idle")
        return True

    def ensure_indextts2_loaded(
        self, wait_timeout_sec: float = _INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC
    ) -> Any:
        """Load IndexTTS2 once (or wait for an in-flight load) and return it.

        The fast path (model already cached) returns without touching any
        lock. Concurrent first-time callers cooperate via a state machine
        so the model is never instantiated twice; non-loader callers
        wait on an event with a hard timeout so a vanished loader thread
        cannot wedge the whole service.
        """
        if self._indextts2_model is not None:
            return self._indextts2_model

        with self._state_lock:
            if self._indextts2_model is not None:
                return self._indextts2_model

            if self._indextts2_warmup_status == "loading":
                role = "waiter"
            else:
                # ``idle`` (first ever call) or ``error`` (previous attempt
                # failed and a caller is retrying) — either way, we become
                # the loader and reset the event so waiters block again.
                self._indextts2_warmup_status = "loading"
                self._indextts2_load_error = None
                self._load_done.clear()
                role = "loader"

        if role == "waiter":
            if not self._load_done.wait(timeout=wait_timeout_sec):
                raise TimeoutError(
                    f"timed out after {wait_timeout_sec:.0f}s waiting for "
                    "IndexTTS2 load to finish"
                )
            if self._indextts2_model is None:
                err = self._indextts2_load_error
                msg = (
                    f"IndexTTS2 load failed: status={self._indextts2_warmup_status}"
                )
                if err is not None:
                    raise RuntimeError(msg) from err
                raise RuntimeError(msg)
            return self._indextts2_model

        # We are the loader. Heavy work below runs *outside* any lock so
        # an abrupt thread death cannot strand the service.
        try:
            try:
                from indextts import IndexTTS2  # type: ignore[import]
            except ImportError as exc:
                raise RuntimeError(
                    "indextts2-inference is not installed; "
                    "add it to pyproject.toml[real] or install manually"
                ) from exc
            _prefer_local_hf_cache_for_indextts2()

            init_kwargs: dict[str, Any] = {
                "use_fp16": True,
                # See settings.indextts2_use_cuda_kernel for context: default
                # False because the BigVGAN fused-CUDA-kernel JIT path hangs
                # on sm_120 inside the FastAPI lifespan worker thread.
                "use_cuda_kernel": self.settings.indextts2_use_cuda_kernel,
            }
            if self.settings.indextts2_model_dir:
                init_kwargs["model_dir"] = self.settings.indextts2_model_dir
            if self.settings.indextts2_attn_backend:
                init_kwargs["attn_backend"] = self.settings.indextts2_attn_backend
            logger.info(
                "loading IndexTTS2 (model_dir=%s, attn=%s, fp16=True, cuda_kernel=%s)",
                self.settings.indextts2_model_dir or "<auto>",
                self.settings.indextts2_attn_backend or "<sdpa>",
                self.settings.indextts2_use_cuda_kernel,
            )
            model = IndexTTS2(**init_kwargs)
        except BaseException as exc:
            with self._state_lock:
                self._indextts2_warmup_status = "error"
                self._indextts2_load_error = (
                    exc if isinstance(exc, Exception) else None
                )
            self._load_done.set()
            logger.exception("IndexTTS2 load raised: %s", exc)
            raise
        else:
            with self._state_lock:
                self._indextts2_model = model
                self._indextts2_warmup_status = "ready"
            self._load_done.set()
            logger.info("IndexTTS2 loaded; warmup_status=ready")
            return model

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        # Empty / punctuation-only text always returns silence, regardless of
        # the configured backend. This avoids real TTS models generating
        # unexpectedly long audio for degenerate inputs like lone periods.
        if not _has_speakable_text(request.text):
            return self._run_silence(request)
        _printable = request.text.translate(
            str.maketrans("", "", " \t\n\r.,!?。！？，、；：…—–")
        )
        if False and not _printable:
            return self._run_silence(request)

        backend = self.settings.ml_tts_backend
        if backend == "indextts2":
            if self.settings.indextts2_inline:
                return self._run_indextts2_inline(request)
            return self._run_indextts2(request)
        if backend == "silence":
            return self._run_silence(request)

        # Unknown / misconfigured backend: refuse loudly instead of silently
        # producing a silent WAV (which downstream worker would persist as a
        # successful synthesis result, masking the misconfiguration).
        raise UnsupportedTTSBackendError(
            backend=backend,
            supported=("indextts2", "silence"),
        )

    def _run_silence(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        write_silence_wav(
            output_path,
            sample_rate=self.settings.default_sample_rate,
            channels=self.settings.default_channels,
            duration_sec=max(request.target_duration_sec, 0.05),
        )
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=int(request.target_duration_sec * 1000),
            diagnostics=["tts backend=silence"],
        )

    def _run_indextts2_inline(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)

        # Single load path: blocks once on the first concurrent request,
        # then becomes O(1) for the remainder of the process lifetime.
        tts = self.ensure_indextts2_loaded()
        generation_meta = {}
        if isinstance(request.dubbing_meta, dict) and isinstance(
            request.dubbing_meta.get("lora_candidate_generation"), dict
        ):
            generation_meta = request.dubbing_meta["lora_candidate_generation"]
        duration_mode = _indextts2_duration_control_mode(self.settings, generation_meta)

        # Resolve spk_audio_prompt from voice_config, then fall back to global default.
        # A multi-sample voice_config is treated as one speaker pool; the
        # prompt selector will pick a single stable neutral clip so multi-chapter
        # lecture episodes do not drift in timbre from chapter to chapter.
        spk_audio_paths: list[Path] = []
        samples = (request.voice_config or {}).get("sample_relpaths", [])
        if samples:
            spk_audio_paths = [
                resolve_data_path(self.settings.data_root, str(sample))
                for sample in samples
                if str(sample).strip()
            ]
        if not spk_audio_paths and self.settings.indextts2_default_voice_relpath:
            spk_audio_paths.append(
                resolve_data_path(
                    self.settings.data_root, self.settings.indextts2_default_voice_relpath
                )
            )
        if not spk_audio_paths:
            raise RuntimeError(
                "IndexTTS2 inline: no spk_audio_prompt available. "
                "Bind a VoiceProfile with at least one sample_relpath, "
                "or set INDEXTTS2_DEFAULT_VOICE_RELPATH."
            )
        spk_audio = self._prepare_spk_audio_prompt(spk_audio_paths)

        # Scheme 1 — text-based token budget:
        #   Estimate how many tokens the text needs using tokens_per_char.
        #   This is the primary constraint; the model will naturally stop when
        #   the text is consumed, well before this budget is exhausted.
        #
        # Scheme 2 — adaptive tokens_per_char (applied on retry):
        #   Blend observed rate from the previous attempt with the prior so the
        #   budget converges toward the actual rate for this specific segment.
        #
        # Hard ceiling — max_allowed_sec:
        #   Audio must never exceed (target + gap) to avoid spilling into the
        #   next segment.  This is enforced separately in the merge stage by
        #   clipping each overlay to its allowed slot.

        TOKENS_PER_SEC: float = 50.0
        PRIOR_TOKENS_PER_CHAR: float = 13.5
        HEADROOM: float = 1.05

        target_sec = max(request.target_duration_sec, 0.1)
        max_allowed_sec = request.max_allowed_sec if request.max_allowed_sec > 0 else target_sec
        if duration_mode == "embedding":
            if target_sec < _indextts2_duration_min_sec(self.settings, generation_meta):
                duration_mode = "off"
            elif not _patch_indextts2_duration_control(tts):
                duration_mode = "off"
        text_chars = max(1, len([c for c in request.text if not c.isspace()]))

        # Scheme 2: blend observed tokens_per_char with prior if feedback
        # exists. Floored via _blend_tokens_per_char so a truncated or
        # over-rushed observation cannot ratchet the budget downward across
        # retries (the 2026-07-21 ep183 short-segment under-production bug).
        observed_tokens_per_char: float | None = None
        if request.prev_actual_sec > 0 and request.prev_text_chars > 0:
            observed_tokens_per_char = (request.prev_actual_sec * TOKENS_PER_SEC) / request.prev_text_chars
        tokens_per_char = _blend_tokens_per_char(observed_tokens_per_char, PRIOR_TOKENS_PER_CHAR)

        # Scheme 1: text-based budget — primary constraint.
        tokens_from_text = int(text_chars * tokens_per_char * HEADROOM)

        # Hard ceiling: audio MUST NOT exceed (target + gap).
        tokens_from_allowed = int(max_allowed_sec * TOKENS_PER_SEC)

        max_mel_tokens = max(64, min(tokens_from_text, tokens_from_allowed, 4096))
        duration_target_tokens = 0
        if duration_mode == "embedding":
            duration_target_tokens = _indextts2_duration_tokens(
                self.settings,
                target_sec,
                generation_meta,
            )
            max_mel_tokens = _indextts2_duration_max_tokens(
                self.settings,
                current_max_mel_tokens=max_mel_tokens,
                target_duration_tokens=duration_target_tokens,
                max_allowed_sec=max_allowed_sec,
                target_sec=target_sec,
                generation_meta=generation_meta,
            )
        timing_groups = _timing_groups_from_meta(request.dubbing_meta, request.text)
        timing_max_allowed_ms = _timing_max_allowed_ms(request.dubbing_meta)
        timing_status = _timing_status_from_meta(request.dubbing_meta)
        lora_candidate_diag = _lora_candidate_diag_from_meta(request.dubbing_meta)
        timing_diag = ""
        if timing_max_allowed_ms > 0:
            timing_tokens = int((timing_max_allowed_ms / 1000.0) * TOKENS_PER_SEC)
            max_mel_tokens = max(64, min(max_mel_tokens, timing_tokens))
            timing_diag = (
                f" timing_plan={timing_status or 'unknown'}"
                f" timing_max_ms={timing_max_allowed_ms}"
            )
        if timing_groups:
            timing_diag += f" timing_groups={len(timing_groups)}"

        try:
            headroom = int(generation_meta.get("max_mel_headroom") or 0)
        except (TypeError, ValueError):
            headroom = 0
        if headroom > 0:
            max_mel_tokens = min(4096, max_mel_tokens + headroom)
        seed_diag = _apply_generation_seed(generation_meta)

        num_beams = _resolve_num_beams(
            self.settings,
            target_sec=target_sec,
            timing_status=timing_status,
        )

        # OPT-204 prosody plan resolution. When the request carries a
        # structured dubbing_meta block (translator emitted it via the
        # emit_dubbing_plan strict tool), convert the fields into the
        # IndexTTS2 conditioning surface (emo_vector / emphasis_tokens /
        # post-utterance silence). When dubbing_meta is None or empty,
        # fall back to the pre-OPT-204 path (use_emo_text boolean).
        #
        # Why the conversion lives here, not in the LLM call: the LLM
        # output is the operator-facing semantic representation (label
        # strings + 0..1 floats); the IndexTTS2 conditioning surface is
        # implementation-specific (8-element vector, integer token IDs).
        # Keeping them decoupled means an IndexTTS2 schema bump in a
        # future model release does not require a translator re-run.
        tmp_wav = output_path.with_suffix(".tmp.wav")
        infer_kwargs: dict[str, Any] = {
            "spk_audio_prompt": spk_audio,
            "text": request.text,
            "output_path": str(tmp_wav),
            "max_mel_tokens": max_mel_tokens,
            "num_beams": num_beams,
        }
        for key in (
            "temperature",
            "top_p",
            "top_k",
            "repetition_penalty",
            "length_penalty",
            "max_text_tokens_per_segment",
        ):
            if key in generation_meta:
                infer_kwargs[key] = generation_meta[key]
        if duration_target_tokens > 0:
            infer_kwargs["target_duration_tokens"] = duration_target_tokens
        prosody_diag = "emo_text=" + str(self.settings.indextts2_use_emo_text)
        pause_after_ms = 0
        if request.dubbing_meta:
            try:
                infer_kwargs.update(
                    self._resolve_prosody_kwargs(request.dubbing_meta, request.text)
                )
                pause_after_ms = int(request.dubbing_meta.get("pause_after_ms", 0) or 0)
                if pause_after_ms < 0:
                    pause_after_ms = 0
                if pause_after_ms > 1000:
                    pause_after_ms = 1000
                emo_label = (
                    request.dubbing_meta.get("emotion", {}) or {}
                ).get("label") or ""
                pacing_label = request.dubbing_meta.get("pacing") or "normal"
                prosody_diag = (
                    f"prosody=plan(emotion={emo_label} pacing={pacing_label}"
                    f" pause_ms={pause_after_ms})"
                )
            except Exception as exc:  # noqa: BLE001
                # Malformed dubbing_meta must NEVER fail a TTS call —
                # an upstream translator bug should produce slightly
                # less prosodic audio, not a stuck pipeline. Logged at
                # WARN so operators can spot consistent failures.
                logger.warning(
                    "tts: dubbing_meta resolution failed (%s); "
                    "falling back to legacy emo_text",
                    exc,
                )
                infer_kwargs["use_emo_text"] = self.settings.indextts2_use_emo_text
        else:
            infer_kwargs["use_emo_text"] = self.settings.indextts2_use_emo_text

        if timing_groups:
            text_chunks = [group_text for group_text, _ in timing_groups]
            timing_chunk_ms = [target_ms for _, target_ms in timing_groups]
        elif bool(generation_meta.get("disable_text_chunking")):
            text_chunks = [request.text]
            timing_chunk_ms = []
        else:
            text_chunks = _split_long_tts_text(request.text)
            timing_chunk_ms = []
        if len(text_chunks) == 1:
            tts.infer(**infer_kwargs)
            chunk_diag = " chunk_mode=timing" if timing_chunk_ms else ""
        else:
            chunk_paths: list[Path] = []
            chars_by_chunk = [
                max(1, len([c for c in chunk if not c.isspace()]))
                for chunk in text_chunks
            ]
            total_chunk_chars = max(1, sum(chars_by_chunk))
            try:
                for chunk_idx, chunk in enumerate(text_chunks):
                    chunk_path = output_path.with_suffix(
                        f".chunk{chunk_idx:02d}.tmp.wav"
                    )
                    chunk_paths.append(chunk_path)
                    chunk_kwargs = dict(infer_kwargs)
                    chunk_kwargs["text"] = chunk
                    chunk_kwargs["output_path"] = str(chunk_path)
                    if timing_chunk_ms:
                        chunk_target_sec = timing_chunk_ms[chunk_idx] / 1000.0
                        chunk_budget = int(
                            chunk_target_sec * TOKENS_PER_SEC
                        )
                    else:
                        chunk_target_sec = target_sec * chars_by_chunk[chunk_idx] / total_chunk_chars
                        chunk_budget = int(
                            max_mel_tokens
                            * chars_by_chunk[chunk_idx]
                            / total_chunk_chars
                        )
                    chunk_kwargs["max_mel_tokens"] = max(64, min(chunk_budget, 2048))
                    if duration_mode == "embedding":
                        chunk_duration_tokens = _indextts2_duration_tokens(
                            self.settings,
                            chunk_target_sec,
                            generation_meta,
                        )
                        chunk_kwargs["target_duration_tokens"] = chunk_duration_tokens
                        chunk_kwargs["max_mel_tokens"] = min(
                            2048,
                            _indextts2_duration_max_tokens(
                                self.settings,
                                current_max_mel_tokens=int(chunk_kwargs["max_mel_tokens"]),
                                target_duration_tokens=chunk_duration_tokens,
                                max_allowed_sec=chunk_target_sec + 0.5,
                                target_sec=chunk_target_sec,
                                generation_meta=generation_meta,
                            ),
                        )
                    tts.infer(**chunk_kwargs)
                _concat_wav_files(chunk_paths, tmp_wav, gap_sec=_LONG_TEXT_CHUNK_GAP_SEC)
            finally:
                for chunk_path in chunk_paths:
                    chunk_path.unlink(missing_ok=True)
            chunk_diag = (
                f" chunked={len(text_chunks)}"
                f" chunk_mode={'timing' if timing_chunk_ms else 'text'}"
                f" chunk_chars={','.join(str(v) for v in chars_by_chunk)}"
            )

        # Post-process: resample to project sample rate only.
        # Duration stretching (atempo) is intentionally omitted — any overflow is
        # handled in the pipeline by borrowing from the trailing silence gap, and
        # re-translation is triggered when the overflow exceeds that gap.
        subprocess.run(
            [
                self.settings.ffmpeg_bin, "-y",
                "-i", str(tmp_wav),
                "-af", f"aresample={self.settings.default_sample_rate}",
                "-ac", str(self.settings.default_channels),
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        tmp_wav.unlink(missing_ok=True)

        # OPT-204 pause_after_ms: append trailing silence to the synthesized
        # WAV so the per-segment boundary lands on the requested pause. We
        # append AFTER the resample step so the silence is at the project
        # sample rate. ffmpeg's concat demuxer is overkill for a 0..1s
        # silence; using a single -f lavfi anullsrc + -t pad expression
        # keeps the dependency surface unchanged (no new binaries).
        if pause_after_ms > 0:
            self._append_trailing_silence(output_path, pause_after_ms)

        actual_sec = probe_duration(self.settings, output_path)
        duration_ms = int(actual_sec * 1000)
        diag = [
            f"tts backend=indextts2(inline) max_mel_tokens={max_mel_tokens}"
            f" tokens_per_char={tokens_per_char:.2f}"
            f" num_beams={num_beams}"
            f" duration_control={duration_mode}"
            f" duration_target_tokens={duration_target_tokens}"
            f" duration_token_rate={_indextts2_duration_token_rate(self.settings, target_sec, generation_meta):.3f}"
            f" duration_min_sec={_indextts2_duration_min_sec(self.settings, generation_meta):.3f}"
            f" (prev_actual={request.prev_actual_sec:.2f}s prev_chars={request.prev_text_chars})"
            f" {prosody_diag}"
            f"{timing_diag}"
            f"{lora_candidate_diag}"
            f"{chunk_diag}"
            f"{seed_diag}"
            f" actual={actual_sec:.2f}s target={target_sec:.2f}s"
        ]
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=diag,
        )

    def _prepare_spk_audio_prompt(self, source_paths: list[Path] | Path) -> str:
        """Return a short, stable reference prompt for IndexTTS2 voice cloning."""
        if isinstance(source_paths, Path):
            paths = [source_paths]
        else:
            paths = [path for path in source_paths if path]
        if not paths:
            raise RuntimeError("voice reference prompt requested with no source paths")

        short_paths: list[Path] = []
        long_paths: list[Path] = []
        for source_path in paths:
            try:
                duration = probe_duration(self.settings, source_path)
            except Exception as exc:  # pragma: no cover - defensive fallback
                logger.warning(
                    "voice reference duration probe failed; skipping source",
                    extra={"path": str(source_path), "error": str(exc)},
                )
                continue
            if duration <= _REFERENCE_PROMPT_MAX_ORIGINAL_SEC:
                short_paths.append(source_path)
            else:
                long_paths.append(source_path)

        if not long_paths and short_paths:
            return str(short_paths[0])
        if not long_paths:
            return str(paths[0])

        clip_path = _reference_clip_path(self.settings.data_root, long_paths)
        if clip_path.exists():
            return str(clip_path)
        ensure_parent(clip_path)

        candidate = _find_stable_reference_candidate(
            long_paths,
            _REFERENCE_PROMPT_CLIP_SEC,
            _REFERENCE_PROMPT_HOP_SEC,
        )
        source_path = candidate["path"]
        start_sec = float(candidate["start_sec"])
        tmp_path = clip_path.with_suffix(".tmp.wav")
        command = [
            self.settings.ffmpeg_bin,
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{_REFERENCE_PROMPT_CLIP_SEC:.3f}",
            "-i",
            str(source_path),
            "-vn",
            "-ar",
            str(self.settings.default_sample_rate),
            "-ac",
            str(self.settings.default_channels),
            str(tmp_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
            tmp_path.replace(clip_path)
            logger.info(
                "created cached voice reference clip",
                extra={
                    "source": str(source_path),
                    "source_count": len(long_paths),
                    "clip": str(clip_path),
                    "start_sec": round(start_sec, 3),
                    "duration_sec": _REFERENCE_PROMPT_CLIP_SEC,
                    "score": round(float(candidate["score"]), 3),
                },
            )
            return str(clip_path)
        except Exception as exc:  # pragma: no cover - depends on ffmpeg runtime
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "voice reference clip creation failed; using original",
                extra={"source": str(source_path), "error": str(exc)},
            )
            return str(source_path)

    def _resolve_prosody_kwargs(
        self, dubbing_meta: dict[str, Any], text: str
    ) -> dict[str, Any]:
        """Convert the LLM-emitted DubbingPlan into IndexTTS2 infer kwargs.

        Returns ONLY the keys we want to override; the caller starts
        from a baseline dict and updates it with our result, so unknown
        IndexTTS2 args stay at their defaults.

        Conversion rules:
            emotion.{valence,arousal} → emo_vector (8-element list)
                Mapping: emo_vector indices are [happy, sad, angry,
                surprised, fear, disgust, neutral, excited]. We approximate
                from (valence, arousal):
                    high valence + high arousal → excited / happy
                    high valence + low arousal  → calm / neutral
                    low valence + high arousal  → angry / surprised
                    low valence + low arousal   → sad
                The mapping is intentionally simple; OPT-204 PR-13's
                L3 evaluation step (50-sample human rating) will tell
                us whether finer-grained tuning is worth it.
            pacing               → set use_emo_text=False (the model's
                pacing slider is implicit in max_mel_tokens; the LLM-
                emitted pacing is a hint for downstream consumers like
                future post-processing tempo bumps, not for IndexTTS2
                directly).
            emphasis_words       → emphasis_tokens (token-id list resolved
                by IndexTTS2's tokenizer over the target text).

        emphasis_tokens may need to be empty when IndexTTS2 does not
        expose the kwarg in the installed version; we omit it rather
        than fail.
        """
        kwargs: dict[str, Any] = {}
        explicit_vector = dubbing_meta.get("emo_vector")
        if explicit_vector is not None:
            values = [float(item) for item in explicit_vector]
            if len(values) != _EMO_VECTOR_LEN:
                raise ValueError(f"emo_vector must contain {_EMO_VECTOR_LEN} values")
            kwargs["use_emo_text"] = bool(dubbing_meta.get("use_emo_text", False))
            kwargs["emo_vector"] = values
            if dubbing_meta.get("emo_alpha") is not None:
                kwargs["emo_alpha"] = float(dubbing_meta["emo_alpha"])
            return kwargs

        emo = dubbing_meta.get("emotion") or {}
        valence = float(emo.get("valence", 0.0) or 0.0)
        arousal = float(emo.get("arousal", 0.0) or 0.0)
        valence = max(-1.0, min(1.0, valence))
        arousal = max(0.0, min(1.0, arousal))
        # Always disable text-driven emotion when a structured dubbing plan is
        # present. For neutral/calm lecture narration, do not send an explicit
        # emo_vector either: IndexTTS2 will use the speaker prompt's natural,
        # usually flatter style instead of forcing a performed emotion.
        kwargs["use_emo_text"] = False
        label = str(emo.get("label") or "").strip().lower()
        neutral_labels = {"", "neutral", "calm", "平静", "中性"}
        if not (
            label in neutral_labels
            and arousal <= self.settings.indextts2_neutral_dubbing_max_arousal
        ):
            kwargs["emo_vector"] = _emo_vector_from_valence_arousal(
                valence,
                arousal,
                max_sum=self.settings.indextts2_dubbing_emo_vector_max_sum,
            )

        emphasis_words = dubbing_meta.get("emphasis_words") or []
        if emphasis_words and isinstance(emphasis_words, list):
            # We pass the raw word list — IndexTTS2 versions that accept
            # emphasis_words handle the tokenisation themselves; versions
            # that do not will reject the kwarg, in which case we strip
            # it (caught by the outer try/except in the caller).
            # Defensive: ensure every entry actually appears in the
            # target text so the model has something to anchor on.
            anchored = [w for w in emphasis_words if isinstance(w, str) and w and w in text]
            if anchored:
                logger.debug("tts: ignoring emphasis_words for inline IndexTTS2: %s", anchored)

        return kwargs

    def _append_trailing_silence(self, output_path: Path, pause_ms: int) -> None:
        """Append `pause_ms` of silence to an existing WAV at output_path.

        Implemented as a single ffmpeg run that pipes the existing WAV
        through a `apad` filter so we avoid spawning two processes. The
        output is written to a temp file then atomically renamed over the
        original — failure leaves the original intact.
        """
        if pause_ms <= 0:
            return
        padded = output_path.with_suffix(".padded.wav")
        pad_seconds = pause_ms / 1000.0
        subprocess.run(
            [
                self.settings.ffmpeg_bin, "-y",
                "-i", str(output_path),
                # apad pad_dur: append `pad_dur` seconds of silence
                # (sample-rate aware) to the input stream.
                "-af", f"apad=pad_dur={pad_seconds}",
                "-ar", str(self.settings.default_sample_rate),
                "-ac", str(self.settings.default_channels),
                str(padded),
            ],
            check=True,
            capture_output=True,
        )
        padded.replace(output_path)

    def _run_indextts2(self, request: TTSRequest) -> TTSResponse:
        if self.settings.indextts2_command:
            return self._run_indextts2_command(request)
        if self.settings.indextts2_endpoint:
            return self._run_indextts2_http(request)
        raise RuntimeError("INDEXTTS2_COMMAND or INDEXTTS2_ENDPOINT is required when ML_TTS_BACKEND=indextts2")

    def _run_indextts2_command(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        voice_json = json.dumps(request.voice_config, ensure_ascii=False)
        command_template = self.settings.indextts2_command.format(
            text=request.text,
            duration=request.target_duration_sec,
            output=output_path,
            voice_json=voice_json,
        )
        command = shlex.split(command_template)
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "IndexTTS2 command failed")
        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=["tts backend=indextts2(command)"],
        )

    def _run_indextts2_http(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        headers = {}
        if self.settings.indextts2_api_key:
            headers["Authorization"] = f"Bearer {self.settings.indextts2_api_key}"

        payload = {
            "model": self.settings.indextts2_model,
            "text": request.text,
            "target_duration_sec": request.target_duration_sec,
            "voice_config": request.voice_config,
            "output_path": str(output_path),
        }
        response = httpx.post(
            self.settings.indextts2_endpoint,
            json=payload,
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if content_type.startswith("audio/"):
            output_path.write_bytes(response.content)
        else:
            body = response.json()
            if "audio_base64" in body:
                output_path.write_bytes(base64.b64decode(body["audio_base64"]))
            elif body.get("audio_path"):
                source = Path(body["audio_path"])
                output_path.write_bytes(source.read_bytes())
            elif not output_path.exists():
                raise RuntimeError("IndexTTS2 HTTP response did not provide audio content")

        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=["tts backend=indextts2(http)"],
        )


# IndexTTS2's emo_vector convention (consistent across the model
# series we ship): an 8-element list of non-negative floats summing
# to <= 1.0, indexed as:
#
#   [happy, sad, angry, surprised, fear, disgust, neutral, excited]
#
# A zero vector is silently ignored by the model (no emotion
# conditioning applied), so we always emit a non-trivial mix. The
# mapping below is the OPT-204 PR-13 starting heuristic; the L3
# human-rated eval (≥ 50 samples) will decide whether we keep this
# linear projection or move to a learned map.

_EMO_VECTOR_LEN = 8
_IDX_HAPPY, _IDX_SAD, _IDX_ANGRY = 0, 1, 2
_IDX_SURPRISED, _IDX_FEAR, _IDX_DISGUST = 3, 4, 5
_IDX_NEUTRAL, _IDX_EXCITED = 6, 7


def _emo_vector_from_valence_arousal(valence: float, arousal: float) -> list[float]:
    """Project (valence, arousal) ∈ [-1,1]×[0,1] onto the 8-element emo_vector.

    The mapping uses four quadrants by signed valence × thresholded arousal:

        valence >= 0, arousal >= 0.5 → excited / happy mix
        valence >= 0, arousal <  0.5 → neutral / calm mix
        valence <  0, arousal >= 0.5 → angry / surprised mix
        valence <  0, arousal <  0.5 → sad / neutral mix

    The weights inside each quadrant are continuous (linear interpolation
    on |valence| or arousal) so a segment with valence=0.51 arousal=0.49
    does NOT produce a discontinuous jump versus 0.49/0.49.

    Returns a fresh list (caller is free to mutate without affecting
    subsequent calls). The vector is L1-normalised to sum to 1.0 so
    IndexTTS2 treats it as a probability distribution.
    """
    v = max(-1.0, min(1.0, valence))
    a = max(0.0, min(1.0, arousal))
    vec = [0.0] * _EMO_VECTOR_LEN

    # Magnitudes used per-quadrant.
    abs_v = abs(v)
    if v >= 0:
        if a >= 0.5:
            # Energetic positive: excited + happy
            vec[_IDX_EXCITED] = 0.4 + 0.4 * a
            vec[_IDX_HAPPY] = 0.3 + 0.3 * v
        else:
            # Calm positive: neutral + happy
            vec[_IDX_NEUTRAL] = 0.5 + 0.3 * (1.0 - a)
            vec[_IDX_HAPPY] = 0.2 + 0.4 * v
    else:
        if a >= 0.5:
            # Energetic negative: angry + surprised + fear
            vec[_IDX_ANGRY] = 0.3 + 0.5 * abs_v
            vec[_IDX_SURPRISED] = 0.2 + 0.2 * (a - 0.5) * 2
            vec[_IDX_FEAR] = 0.1 + 0.1 * abs_v
        else:
            # Subdued negative: sad + neutral
            vec[_IDX_SAD] = 0.4 + 0.4 * abs_v
            vec[_IDX_NEUTRAL] = 0.3 + 0.3 * (1.0 - a)

    # L1-normalise. Defensive: avoid divide-by-zero on an all-zero
    # vector (impossible given the branches above always assign to at
    # least one index, but cheap to enforce).
    total = sum(vec)
    if total <= 0:
        vec[_IDX_NEUTRAL] = 1.0
        return vec
    return [x / total for x in vec]


# Override the legacy OPT-204 mapping above with IndexTTS2's official order:
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm].
# The installed runtime blends this vector by total mass, so keeping the sum
# below 1.0 preserves speaker-prompt style and avoids over-emoted lectures.
_IDX_HAPPY, _IDX_ANGRY, _IDX_SAD = 0, 1, 2
_IDX_AFRAID, _IDX_DISGUSTED, _IDX_MELANCHOLIC = 3, 4, 5
_IDX_SURPRISED, _IDX_CALM = 6, 7


def _emo_vector_from_valence_arousal(
    valence: float,
    arousal: float,
    *,
    max_sum: float = 0.35,
) -> list[float]:
    v = max(-1.0, min(1.0, valence))
    a = max(0.0, min(1.0, arousal))
    cap = max(0.0, min(0.8, max_sum))
    vec = [0.0] * _EMO_VECTOR_LEN

    abs_v = abs(v)
    if v >= 0:
        if a >= 0.5:
            vec[_IDX_HAPPY] = 0.5 + 0.4 * v
            vec[_IDX_SURPRISED] = 0.1 + 0.2 * (a - 0.5) * 2
        else:
            vec[_IDX_CALM] = 0.6 + 0.3 * (1.0 - a)
            vec[_IDX_HAPPY] = 0.1 + 0.3 * v
    else:
        if a >= 0.5:
            vec[_IDX_ANGRY] = 0.3 + 0.5 * abs_v
            vec[_IDX_SURPRISED] = 0.2 + 0.2 * (a - 0.5) * 2
            vec[_IDX_AFRAID] = 0.1 + 0.1 * abs_v
        else:
            vec[_IDX_SAD] = 0.4 + 0.4 * abs_v
            vec[_IDX_MELANCHOLIC] = 0.15 + 0.2 * abs_v
            vec[_IDX_CALM] = 0.2 + 0.2 * (1.0 - a)

    total = sum(vec)
    if total <= 0 or cap <= 0:
        vec[_IDX_CALM] = cap
        return vec
    return [x / total * cap for x in vec]


_TEXT_CHUNK_BOUNDARY_RE = re.compile(r"([^。！？!?；;，,\n]+[。！？!?；;，,\n]?)")


def _timing_plan_from_meta(dubbing_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(dubbing_meta, dict):
        return None
    timing = dubbing_meta.get("timing")
    if not isinstance(timing, dict):
        return None
    return timing


def _timing_status_from_meta(dubbing_meta: dict[str, Any] | None) -> str:
    timing = _timing_plan_from_meta(dubbing_meta)
    if not timing:
        return ""
    status = timing.get("status")
    return str(status) if status else ""


def _lora_candidate_diag_from_meta(dubbing_meta: dict[str, Any] | None) -> str:
    return _LoraCandidatePolicy.from_meta(dubbing_meta).diagnostic()


def _timing_max_allowed_ms(dubbing_meta: dict[str, Any] | None) -> int:
    timing = _timing_plan_from_meta(dubbing_meta)
    if not timing:
        return 0
    return _coerce_positive_int(timing.get("max_allowed_ms"))


def _resolve_num_beams(settings: Any, *, target_sec: float, timing_status: str) -> int:
    base = _coerce_positive_int(getattr(settings, "indextts2_num_beams", 1))
    if base <= 0:
        base = 1
    long_beams = _coerce_positive_int(getattr(settings, "indextts2_long_num_beams", base))
    if long_beams <= 0:
        long_beams = base
    try:
        long_min_sec = float(getattr(settings, "indextts2_long_quality_min_sec", 30.0) or 0)
    except (TypeError, ValueError):
        long_min_sec = 30.0

    risky_timing = str(timing_status or "").strip().lower() in {"too_long", "too_short"}
    if (long_min_sec > 0 and target_sec >= long_min_sec) or risky_timing:
        return max(base, long_beams)
    return base


def _timing_groups_from_meta(
    dubbing_meta: dict[str, Any] | None,
    fallback_text: str,
) -> list[tuple[str, int]] | None:
    timing = _timing_plan_from_meta(dubbing_meta)
    if not timing:
        return None
    raw_groups = timing.get("groups")
    if not isinstance(raw_groups, list):
        return None

    groups: list[tuple[str, int]] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        target_ms = _coerce_positive_int(raw.get("target_ms"))
        if text and target_ms > 0:
            groups.append((text, target_ms))
    if not groups:
        return None

    joined = "".join(text for text, _ in groups)
    if _compact_for_timing(joined) != _compact_for_timing(fallback_text):
        return None
    return groups


def _coerce_positive_int(value: Any) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out if out > 0 else 0


def _compact_for_timing(text: str) -> str:
    return "".join(str(text or "").split())


def _split_long_tts_text(text: str) -> list[str]:
    """Split long TTS text into sentence-sized chunks for clearer synthesis.

    IndexTTS2 can synthesize long text, but dense 30-40s lecture segments are
    prone to over-compression: the resulting audio has energy throughout yet
    ASR only recovers scattered phrases. Chunking keeps each infer call close
    to a natural breath group while preserving the caller-visible single WAV.
    """
    stripped = (text or "").strip()
    if len([c for c in stripped if not c.isspace()]) < _LONG_TEXT_CHUNK_MIN_CHARS:
        return [text]

    pieces = [m.group(1).strip() for m in _TEXT_CHUNK_BOUNDARY_RE.finditer(stripped)]
    pieces = [p for p in pieces if p]
    if not pieces:
        pieces = [stripped]

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece_parts = _split_oversized_piece(piece)
        for part in piece_parts:
            if not current:
                current = part
                continue
            combined_len = _speakable_len(current) + _speakable_len(part)
            if (
                _speakable_len(current) >= _LONG_TEXT_CHUNK_TARGET_CHARS
                or combined_len > _LONG_TEXT_CHUNK_MAX_CHARS
            ):
                chunks.append(current)
                current = part
            else:
                current += part
    if current:
        chunks.append(current)

    if len(chunks) <= 1:
        return [text]
    return chunks


def _split_oversized_piece(piece: str) -> list[str]:
    if _speakable_len(piece) <= _LONG_TEXT_CHUNK_MAX_CHARS:
        return [piece]
    parts: list[str] = []
    current = ""
    for char in piece:
        current += char
        if _speakable_len(current) >= _LONG_TEXT_CHUNK_TARGET_CHARS:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts


def _speakable_len(text: str) -> int:
    return len([c for c in text if not c.isspace()])


def _concat_wav_files(
    input_paths: list[Path],
    output_path: Path,
    *,
    gap_sec: float = 0.0,
) -> None:
    """Concatenate WAV files with identical parameters into output_path."""
    if not input_paths:
        raise ValueError("cannot concatenate an empty WAV list")
    with wave.open(str(input_paths[0]), "rb") as first:
        params = first.getparams()
        audio_format = (
            first.getnchannels(),
            first.getsampwidth(),
            first.getframerate(),
            first.getcomptype(),
            first.getcompname(),
        )
        frames = [first.readframes(first.getnframes())]
    for input_path in input_paths[1:]:
        with wave.open(str(input_path), "rb") as wav_file:
            wav_format = (
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
                wav_file.getframerate(),
                wav_file.getcomptype(),
                wav_file.getcompname(),
            )
            if wav_format != audio_format:
                raise ValueError(
                    f"WAV params differ while concatenating {input_path}: "
                    f"{wav_format} != {audio_format}"
                )
            frames.append(wav_file.readframes(wav_file.getnframes()))
    with wave.open(str(output_path), "wb") as out:
        out.setparams(params)
        gap_frames = max(0, int(params.framerate * gap_sec))
        gap = b"\x00" * params.sampwidth * params.nchannels * gap_frames
        for idx, frame_data in enumerate(frames):
            if idx > 0 and gap:
                out.writeframes(gap)
            out.writeframes(frame_data)


def _reference_clip_path(data_root: Path, source_paths: list[Path] | Path) -> Path:
    paths = [source_paths] if isinstance(source_paths, Path) else source_paths
    parts: list[str] = [_REFERENCE_PROMPT_CACHE_VERSION]
    for source_path in paths:
        try:
            rel = source_path.resolve().relative_to(data_root.resolve()).as_posix()
        except ValueError:
            rel = source_path.resolve().as_posix()
        try:
            stat = source_path.stat()
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            stamp = "missing"
        parts.append(f"{rel}:{stamp}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return data_root / "tmp" / "voice_refs" / f"{digest}.wav"


def _has_speakable_text(text: str) -> bool:
    return any(ch.isalnum() for ch in text or "")


def _find_loud_reference_start(source_path: Path, window_sec: float, hop_sec: float) -> float:
    """Find the start of the loudest window in a PCM WAV reference file."""
    try:
        with wave.open(str(source_path), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            if sample_rate <= 0 or sample_width != 2:
                return 0.0
            hop_frames = max(1, int(sample_rate * hop_sec))
            energies: list[float] = []
            while True:
                raw = wav_file.readframes(hop_frames)
                if not raw:
                    break
                samples = array("h")
                samples.frombytes(raw)
                if os.sys.byteorder != "little":
                    samples.byteswap()
                if not samples:
                    energies.append(0.0)
                    continue
                energies.append(sum(float(s) * float(s) for s in samples) / len(samples))
    except (OSError, wave.Error):
        return 0.0

    if not energies:
        return 0.0
    window_hops = max(1, int(round(window_sec / hop_sec)))
    if len(energies) <= window_hops:
        return 0.0

    current = sum(energies[:window_hops])
    best_sum = current
    best_idx = 0
    for idx in range(1, len(energies) - window_hops + 1):
        current += energies[idx + window_hops - 1] - energies[idx - 1]
        if current > best_sum:
            best_sum = current
            best_idx = idx
    return float(best_idx) * hop_sec


def _find_stable_reference_candidate(
    source_paths: list[Path],
    window_sec: float,
    hop_sec: float,
) -> dict[str, Any]:
    """Pick a neutral-ish prompt window from one or more reference WAV files.

    The old rule chose the loudest 12 seconds. That tends to over-sample
    excited lecture moments, board noise, or filler-heavy asides. This scorer
    instead rewards moderate, consistent speech energy and penalizes near-clip
    peaks and edge windows. It is intentionally audio-only so production does
    not need to run ASR before TTS.
    """
    fallback_path = source_paths[0]
    fallback = {"path": fallback_path, "start_sec": 0.0, "score": 1_000_000.0}
    best: dict[str, Any] | None = None
    for source_path in source_paths:
        frames = _reference_hop_features(source_path, hop_sec)
        if not frames:
            continue
        window_hops = max(1, int(round(window_sec / hop_sec)))
        if len(frames) <= window_hops:
            candidate = {"path": source_path, "start_sec": 0.0, "score": 0.0}
            best = candidate if best is None or candidate["score"] < best["score"] else best
            continue
        duration_sec = len(frames) * hop_sec
        for idx in range(0, len(frames) - window_hops + 1):
            start_sec = float(idx) * hop_sec
            end_sec = start_sec + window_sec
            # Avoid chapter edges where intro/outro silence and clipped applause
            # are more common. Keep the guard soft for short source files.
            edge_penalty = 0.0
            if duration_sec > 90:
                if start_sec < 20:
                    edge_penalty += (20 - start_sec) * 0.2
                if duration_sec - end_sec < 20:
                    edge_penalty += (20 - (duration_sec - end_sec)) * 0.2

            window = frames[idx : idx + window_hops]
            rms_values = [f["rms_dbfs"] for f in window if f["rms_dbfs"] > -70]
            if len(rms_values) < max(3, int(window_hops * 0.55)):
                continue
            mean_rms = sum(rms_values) / len(rms_values)
            variance = sum((x - mean_rms) ** 2 for x in rms_values) / len(rms_values)
            std_rms = variance ** 0.5
            peak_dbfs = max(f["peak_dbfs"] for f in window)
            active_ratio = len(rms_values) / len(window)

            # Target a moderate speech prompt around -30 dBFS. Penalize loud
            # energetic windows, very sparse speech, and unstable amplitude.
            score = abs(mean_rms - (-30.0)) * 1.2
            score += std_rms * 1.5
            score += abs(active_ratio - 0.78) * 8.0
            if peak_dbfs > -6.0:
                score += (peak_dbfs + 6.0) * 3.0
            if mean_rms > -25.0:
                score += (mean_rms + 25.0) * 2.0
            if mean_rms < -38.0:
                score += (-38.0 - mean_rms) * 2.0
            score += edge_penalty

            candidate = {"path": source_path, "start_sec": start_sec, "score": score}
            if best is None or score < float(best["score"]):
                best = candidate
    return best or fallback


def _reference_hop_features(source_path: Path, hop_sec: float) -> list[dict[str, float]]:
    try:
        with wave.open(str(source_path), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            if sample_rate <= 0 or sample_width != 2:
                return []
            hop_frames = max(1, int(sample_rate * hop_sec))
            features: list[dict[str, float]] = []
            while True:
                raw = wav_file.readframes(hop_frames)
                if not raw:
                    break
                samples = array("h")
                samples.frombytes(raw)
                if os.sys.byteorder != "little":
                    samples.byteswap()
                if channels > 1:
                    samples = array("h", samples[::channels])
                if not samples:
                    continue
                rms = math.sqrt(sum(float(s) * float(s) for s in samples) / len(samples))
                peak = max(abs(int(s)) for s in samples)
                rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0
                peak_dbfs = 20.0 * math.log10(peak / 32768.0) if peak > 0 else -120.0
                features.append({"rms_dbfs": rms_dbfs, "peak_dbfs": peak_dbfs})
            return features
    except (OSError, wave.Error, EOFError):
        return []


def write_silence_wav(path: Path, sample_rate: int, channels: int, duration_sec: float) -> None:
    total_frames = int(sample_rate * duration_sec)
    frame = struct.pack("<h", 0) * channels
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for _ in range(total_frames):
            wav_file.writeframesraw(frame)
