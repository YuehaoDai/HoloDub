from __future__ import annotations

import copy
import logging
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from app.adapters.indextts2_lora import LoraAdapterController, ensure_lora_adapter
from app.adapters.media import probe_duration
from app.adapters.tts import TTSAdapter, _LoraCandidatePolicy
from app.adapters.asr import ASRAdapter
from app.config import Settings
from app.models import TTSRequest, TTSResponse
from app.services.lora_candidate_quality import (
    asr_similarity,
    audio_prosody_metrics,
    concat_audio,
    critical_term_metrics,
    fit_audio_duration,
    join_quality,
)
from app.services.lora_candidate_split import SplitPlanOptions, build_split_plan, should_split
from app.storage import ensure_parent, resolve_data_path


logger = logging.getLogger(__name__)

LIGHT_VECTOR = [0.12, 0.0, 0.0, 0.0, 0.0, 0.0, 0.08, 0.15]
GENERATION_DEFAULTS: dict[str, Any] = {
    "seed": 1234,
    "temperature": 0.8,
    "top_p": 0.8,
    "top_k": 30,
    "repetition_penalty": 10.0,
    "length_penalty": 0.0,
    "duration_control_mode": "embedding",
    "duration_control_min_sec": 0.05,
    "duration_token_rate_policy": "piecewise",
    "duration_token_rate": 23.4375,
    "duration_piecewise_token_rates": "15:23.4375,23:30,default:32.5",
    "max_text_tokens_per_segment": 600,
    "disable_text_chunking": True,
}
GATE_DEFAULTS: dict[str, float] = {
    "max_drift_ms": 500.0,
    "min_similarity": 0.97,
    "target_similarity": 0.985,
    "min_f0_range": 4.5,
    "target_f0_range": 5.4,
    "min_critical_term_recall": 0.8,
    "target_critical_term_recall": 0.95,
    "max_fit_delta": 0.05,
}
TEXT_COMPRESSION_MIN_SPLIT_VARIANTS = 4
TEXT_COMPRESSION_MIN_STRATEGIES = 2
TEXT_COMPRESSION_FIT_MARGIN = 0.015
JOIN_RESCUE_DEFAULTS: dict[str, float] = {
    "min_similarity": 0.995,
    "max_drift_ms": 50.0,
    "max_fit_delta": 0.05,
    "max_edge_rms_jump_db": 20.0,
    "max_pause_sec": 0.02,
    "max_prev_tail_dbfs": -40.0,
    "max_next_head_dbfs": -50.0,
}


class LoraCandidateService:
    def __init__(self, settings: Settings, tts: TTSAdapter, asr: ASRAdapter) -> None:
        self.settings = settings
        self.tts = tts
        self.asr = asr

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
        if not policy.should_execute_candidates:
            return self.tts.synthesize(request)

        if not bool(getattr(self.settings, "ml_lora_candidate_execution_enabled", False)):
            return self._fallback_response(request, policy, "ml_lora_candidate_execution_disabled")

        adapter_path = self._resolve_adapter_path()
        if not adapter_path:
            return self._fallback_response(request, policy, "indextts2_lora_adapter_path_missing")
        if not adapter_path.exists():
            return self._fallback_response(request, policy, "indextts2_lora_adapter_not_found")

        if self.settings.ml_tts_backend != "indextts2" or not self.settings.indextts2_inline:
            return self._fallback_response(request, policy, "candidate_execution_requires_indextts2_inline")

        try:
            tts_model = self.tts.ensure_indextts2_loaded()
            controller = ensure_lora_adapter(tts_model, adapter_path)
            return self._run_candidates(request, policy, controller)
        except Exception as exc:  # noqa: BLE001
            logger.exception("LoRA candidate execution failed; falling back to default TTS")
            return self._fallback_response(
                request,
                policy,
                "candidate_execution_error",
                error=str(exc),
            )

    def _resolve_adapter_path(self) -> Path | None:
        raw = str(getattr(self.settings, "indextts2_lora_adapter_path", "") or "").strip()
        if not raw:
            return None
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/data/"):
            return Path(self.settings.data_root) / normalized.removeprefix("/data/")
        if normalized.startswith("data/"):
            return Path(self.settings.data_root) / normalized.removeprefix("data/")
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        if path.exists():
            return path
        return resolve_data_path(self.settings.data_root, raw)

    def _adapter_runtime_provenance(self, controller: LoraAdapterController) -> dict[str, Any]:
        path = getattr(controller, "adapter_path", None)
        path_obj = Path(path) if path else None
        data_relpath = ""
        if path_obj is not None:
            try:
                data_relpath = path_obj.resolve().relative_to(Path(self.settings.data_root).resolve()).as_posix()
            except ValueError:
                data_relpath = ""
        metadata = getattr(controller, "metadata", None)
        return {
            "path": str(path_obj) if path_obj is not None else "",
            "data_relpath": data_relpath,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    def _fallback_response(
        self,
        request: TTSRequest,
        policy: _LoraCandidatePolicy,
        reason: str,
        *,
        error: str = "",
    ) -> TTSResponse:
        response = self.tts.synthesize(request)
        runtime = self._empty_runtime(policy, reason, error=error)
        runtime["audio_relpath"] = response.audio_relpath
        runtime["actual_duration_ms"] = response.actual_duration_ms
        response.candidate_result = runtime
        response.diagnostics.append(
            f"lora_candidate_runtime selected=default fallback={reason}"
        )
        return response

    def _empty_runtime(
        self,
        policy: _LoraCandidatePolicy,
        reason: str,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        runtime: dict[str, Any] = {
            "schema_version": 1,
            "execution_requested": True,
            "execution_mode": policy.execution_mode,
            "audio_selection_mode": "default_audio",
            "selected_family": "default",
            "selection_reason": reason,
            "fallback_reason": reason,
            "candidate_families": list(policy.candidate_families),
            "candidates": [],
            "split_plan": None,
            "gate_decision": {"status": "skipped", "reason": reason},
        }
        if error:
            runtime["error"] = error
        return runtime

    def _run_candidates(
        self,
        request: TTSRequest,
        policy: _LoraCandidatePolicy,
        controller: LoraAdapterController,
    ) -> TTSResponse:
        families = [str(item) for item in policy.candidate_families if item]
        if not families:
            families = ["default"]
        candidates: list[dict[str, Any]] = []
        part_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
        candidate_generation_stop_reason = ""

        default_candidate = self._synthesize_single_candidate(
            request,
            family="default",
            controller=controller,
            lora_enabled=False,
        )
        candidates.append(default_candidate)
        if self._is_strong_gate_pass(default_candidate):
            candidate_generation_stop_reason = "default_strong_gate_pass"

        split_plan: dict[str, Any] | None = None
        split_plan_alternates: list[dict[str, Any]] = []
        split_text_compression: dict[str, Any] | None = None
        split_opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)
        if (
            not candidate_generation_stop_reason
            and policy.split_planning
            and self._should_split_candidate(request, policy, split_opts)
        ):
            split_plan = build_split_plan(request.text, request.target_duration_sec, split_opts)
            split_text_compression = self._split_plan_text_compression_recommendation(split_plan)
            if split_text_compression:
                candidate_generation_stop_reason = "needs_text_compression_speech_rate"
            if not candidate_generation_stop_reason and "split_light_vector" in families:
                split_light = self._synthesize_split_light_vector(
                    request,
                    split_plan,
                    controller,
                    part_cache,
                )
                candidates.append(split_light)
                if self._is_strong_gate_pass(split_light):
                    candidate_generation_stop_reason = "split_light_vector_strong_gate_pass"
            if not candidate_generation_stop_reason and "split_mix" in families:
                split_mix = self._synthesize_split_mix(
                    request,
                    split_plan,
                    controller,
                    part_cache,
                )
                candidates.append(split_mix)
                if self._is_strong_gate_pass(split_mix):
                    candidate_generation_stop_reason = "split_mix_strong_gate_pass"

            if self._rescue_strategies_enabled(policy):
                for split_strategy, rescue_opts, concat_pause_override in self._rescue_specs():
                    if candidate_generation_stop_reason or any(row.get("gate_pass") for row in candidates):
                        break
                    rescue_plan = build_split_plan(
                        request.text,
                        request.target_duration_sec,
                        rescue_opts,
                    )
                    rescue_plan["strategy"] = split_strategy
                    if concat_pause_override is not None:
                        rescue_plan["planning_pause_sec"] = rescue_plan.get("pause_sec")
                        rescue_plan["pause_sec"] = concat_pause_override
                    if not self._is_distinct_split_plan(split_plan, rescue_plan):
                        continue
                    if any(not self._is_distinct_split_plan(existing, rescue_plan) for existing in split_plan_alternates):
                        continue
                    split_plan_alternates.append(rescue_plan)
                    if "split_light_vector" in families:
                        split_light = self._synthesize_split_light_vector(
                            request,
                            rescue_plan,
                            controller,
                            part_cache,
                            split_strategy=split_strategy,
                        )
                        candidates.append(split_light)
                        if self._is_strong_gate_pass(split_light):
                            candidate_generation_stop_reason = (
                                f"{split_strategy}_split_light_vector_strong_gate_pass"
                            )
                            break
                    if not candidate_generation_stop_reason and "split_mix" in families:
                        split_mix = self._synthesize_split_mix(
                            request,
                            rescue_plan,
                            controller,
                            part_cache,
                            split_strategy=split_strategy,
                        )
                        candidates.append(split_mix)
                        if self._is_strong_gate_pass(split_mix):
                            candidate_generation_stop_reason = f"{split_strategy}_split_mix_strong_gate_pass"
                            break
                    text_compression = self._text_compression_recommendation(candidates)
                    if text_compression:
                        candidate_generation_stop_reason = "needs_text_compression_high_fit_delta"
                        break

        selected = self._select_candidate(candidates)
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        selected_path = resolve_data_path(self.settings.data_root, str(selected["audio_relpath"]))
        if selected_path != output_path:
            shutil.copyfile(selected_path, output_path)
        actual_duration_ms = int(probe_duration(self.settings, output_path) * 1000)

        fallback_reason = ""
        audio_selection_mode = "candidate_audio"
        if not selected.get("gate_pass"):
            fallback_reason = "all_candidates_rejected"
            audio_selection_mode = "default_audio"
            selected = default_candidate
            selected_path = resolve_data_path(self.settings.data_root, str(selected["audio_relpath"]))
            if selected_path != output_path:
                shutil.copyfile(selected_path, output_path)
            actual_duration_ms = int(probe_duration(self.settings, output_path) * 1000)

        text_compression = split_text_compression or self._text_compression_recommendation(candidates)
        runtime = {
            "schema_version": 1,
            "execution_requested": True,
            "execution_mode": policy.execution_mode,
            "audio_selection_mode": audio_selection_mode,
            "selected_family": selected["family"],
            "selection_reason": "highest_gate_score" if not fallback_reason else fallback_reason,
            "fallback_reason": fallback_reason,
            "candidate_families": families,
            "split_plan": self._runtime_split_plan(split_plan, split_plan_alternates, selected),
            "split_plan_alternates": split_plan_alternates,
            "selected_split_strategy": selected.get("split_strategy"),
            "candidate_generation_stop_reason": candidate_generation_stop_reason,
            "rescue_strategies_enabled": self._rescue_strategies_enabled(policy),
            "text_compression_recommendation": text_compression,
            "candidates": candidates,
            "selected_candidate": dict(selected),
            "adapter": self._adapter_runtime_provenance(controller),
            "gate_decision": {
                "status": "selected" if not fallback_reason else "fallback",
                "selected_family": selected["family"],
                "score": selected.get("score"),
                "reject_reasons": selected.get("reject_reasons", []),
                "needs_text_compression": bool(text_compression),
            },
            "audio_relpath": request.output_relpath,
            "actual_duration_ms": actual_duration_ms,
        }
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=actual_duration_ms,
            diagnostics=[
                "lora_candidate_runtime "
                f"selected={selected['family']} fallback={fallback_reason or '<none>'}"
            ],
            candidate_result=runtime,
        )

    def _rescue_specs(self) -> list[tuple[str, SplitPlanOptions, float | None]]:
        return [
            (
                "balanced_four_part",
                SplitPlanOptions(max_parts=4, max_part_sec=10.5, pause_sec=0.18),
                None,
            ),
            (
                "micro_clause_six_part",
                SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
                None,
            ),
            (
                "micro_clause_tight_concat",
                SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
                0.04,
            ),
            (
                "micro_clause_tight_pause",
                SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.04),
                None,
            ),
            (
                "micro_clause_zero_concat",
                SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
                0.0,
            ),
            (
                "micro_clause_zero_pause",
                SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.0),
                None,
            ),
            (
                "semantic_two_part",
                SplitPlanOptions(max_parts=2, max_part_sec=999.0, pause_sec=0.18),
                None,
            ),
        ]

    @staticmethod
    def _rescue_strategies_enabled(policy: _LoraCandidatePolicy) -> bool:
        split_policy = getattr(policy, "split_policy", {}) or {}
        if not isinstance(split_policy, dict):
            return False
        return bool(split_policy.get("enable_rescue_strategies"))

    def _synthesize_single_candidate(
        self,
        request: TTSRequest,
        *,
        family: str,
        controller: LoraAdapterController,
        lora_enabled: bool,
        text: str | None = None,
        target_sec: float | None = None,
        output_relpath: str | None = None,
        part_index: int | None = None,
    ) -> dict[str, Any]:
        relpath = output_relpath or self._candidate_relpath(request.output_relpath, family)
        meta = self._candidate_meta(request.dubbing_meta, family=family)
        child = request.model_copy(
            update={
                "text": text if text is not None else request.text,
                "target_duration_sec": target_sec if target_sec is not None else request.target_duration_sec,
                "max_allowed_sec": (target_sec + 0.5) if target_sec is not None else request.max_allowed_sec,
                "output_relpath": relpath,
                "prev_actual_sec": 0.0,
                "prev_text_chars": 0,
                "dubbing_meta": meta,
            }
        )
        if lora_enabled:
            with controller.enabled():
                response = self.tts.synthesize(child)
        else:
            controller.set_enabled(False)
            response = self.tts.synthesize(child)
        path = resolve_data_path(self.settings.data_root, relpath)
        fit = fit_audio_duration(
            self.settings,
            path,
            child.target_duration_sec,
            max_delta=GATE_DEFAULTS["max_fit_delta"],
        )
        return self._evaluate_candidate(
            family=family,
            audio_relpath=response.audio_relpath,
            expected_text=child.text,
            target_sec=child.target_duration_sec,
            fit=fit,
            language=request.target_language,
            part_index=part_index,
        )

    def _synthesize_split_light_vector(
        self,
        request: TTSRequest,
        split_plan: dict[str, Any],
        controller: LoraAdapterController,
        part_cache: dict[tuple[str, str, int], dict[str, Any]],
        *,
        split_strategy: str = "automatic",
    ) -> dict[str, Any]:
        part_paths: list[Path] = []
        part_rows: list[dict[str, Any]] = []
        for part in split_plan["parts"]:
            index = int(part["index"])
            part_row = self._cached_part(
                request,
                split_plan,
                part,
                family="split_light_vector",
                controller=controller,
                part_cache=part_cache,
                split_strategy=split_strategy,
            )
            part_rows.append(part_row)
            part_paths.append(resolve_data_path(self.settings.data_root, str(part_row["audio_relpath"])))
        output_name = self._split_output_name("split_light_vector", split_strategy)
        relpath = self._candidate_relpath(request.output_relpath, output_name)
        output_path = resolve_data_path(self.settings.data_root, relpath)
        joined_paths, boundary_repair = concat_audio(
            self.settings,
            part_paths,
            output_path,
            pause_sec=float(split_plan["pause_sec"]),
            repair_loud_boundary_tails_enabled=True,
        )
        jq = join_quality(joined_paths, pause_sec=float(split_plan["pause_sec"]))
        fit = fit_audio_duration(
            self.settings,
            output_path,
            request.target_duration_sec,
            max_delta=GATE_DEFAULTS["max_fit_delta"],
        )
        return self._evaluate_candidate(
            family="split_light_vector",
            audio_relpath=relpath,
            expected_text=request.text,
            target_sec=request.target_duration_sec,
            fit=fit,
            language=request.target_language,
            join_quality_report=jq,
            source_parts=[str(row["audio_relpath"]) for row in part_rows],
            boundary_repair=boundary_repair,
            split_strategy=split_strategy,
            split_part_count=int(split_plan.get("part_count") or len(split_plan.get("parts") or [])),
        )

    def _synthesize_split_mix(
        self,
        request: TTSRequest,
        split_plan: dict[str, Any],
        controller: LoraAdapterController,
        part_cache: dict[tuple[str, str, int], dict[str, Any]],
        *,
        split_strategy: str = "automatic",
    ) -> dict[str, Any]:
        selected_parts: list[dict[str, Any]] = []
        for part in split_plan["parts"]:
            light = self._cached_part(
                request,
                split_plan,
                part,
                family="split_light_vector",
                controller=controller,
                part_cache=part_cache,
                split_strategy=split_strategy,
            )
            emotext = self._cached_part(
                request,
                split_plan,
                part,
                family="split_emotext",
                controller=controller,
                part_cache=part_cache,
                split_strategy=split_strategy,
            )
            selected_parts.append(max([light, emotext], key=self._part_quality_key))

        part_paths = [
            resolve_data_path(self.settings.data_root, str(part["audio_relpath"]))
            for part in selected_parts
        ]
        output_name = self._split_output_name("split_mix", split_strategy)
        relpath = self._candidate_relpath(request.output_relpath, output_name)
        output_path = resolve_data_path(self.settings.data_root, relpath)
        joined_paths, boundary_repair = concat_audio(
            self.settings,
            part_paths,
            output_path,
            pause_sec=float(split_plan["pause_sec"]),
            repair_loud_boundary_tails_enabled=True,
        )
        jq = join_quality(joined_paths, pause_sec=float(split_plan["pause_sec"]))
        fit = fit_audio_duration(
            self.settings,
            output_path,
            request.target_duration_sec,
            max_delta=GATE_DEFAULTS["max_fit_delta"],
        )
        return self._evaluate_candidate(
            family="split_mix",
            audio_relpath=relpath,
            expected_text=request.text,
            target_sec=request.target_duration_sec,
            fit=fit,
            language=request.target_language,
            join_quality_report=jq,
            source_parts=[str(part["audio_relpath"]) for part in selected_parts],
            selected_part_families=[str(part["family"]) for part in selected_parts],
            boundary_repair=boundary_repair,
            split_strategy=split_strategy,
            split_part_count=int(split_plan.get("part_count") or len(split_plan.get("parts") or [])),
        )

    def _cached_part(
        self,
        request: TTSRequest,
        split_plan: dict[str, Any],
        part: dict[str, Any],
        *,
        family: str,
        controller: LoraAdapterController,
        part_cache: dict[tuple[str, str, int], dict[str, Any]],
        split_strategy: str = "automatic",
    ) -> dict[str, Any]:
        index = int(part["index"])
        key = (split_strategy, family, index)
        if key in part_cache:
            return part_cache[key]
        part_name = self._split_output_name(f"{family}_p{index:02d}", split_strategy)
        relpath = self._candidate_relpath(request.output_relpath, part_name)
        row = self._synthesize_single_candidate(
            request,
            family=family,
            controller=controller,
            lora_enabled=True,
            text=str(part["text"]),
            target_sec=float(part["target_sec"]),
            output_relpath=relpath,
            part_index=index,
        )
        row["split_parent_part_count"] = split_plan["part_count"]
        row["split_strategy"] = split_strategy
        part_cache[key] = row
        return row

    def _evaluate_candidate(
        self,
        *,
        family: str,
        audio_relpath: str,
        expected_text: str,
        target_sec: float,
        fit: dict[str, Any],
        language: str,
        part_index: int | None = None,
        join_quality_report: dict[str, Any] | None = None,
        source_parts: list[str] | None = None,
        selected_part_families: list[str] | None = None,
        boundary_repair: dict[str, Any] | None = None,
        split_strategy: str | None = None,
        split_part_count: int | None = None,
    ) -> dict[str, Any]:
        path = resolve_data_path(self.settings.data_root, audio_relpath)
        duration_sec = probe_duration(self.settings, path)
        duration_ms = int(duration_sec * 1000)
        drift_ms = (duration_sec - target_sec) * 1000.0
        asr_text = ""
        asr_diagnostics: list[str] = []
        try:
            asr_text, asr_diagnostics = self.asr.transcribe_window(
                path,
                _asr_language(language),
                0,
                duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            asr_diagnostics = [f"asr_failed={exc}"]
        sim = asr_similarity(expected_text, asr_text)
        term = critical_term_metrics(expected_text, asr_text)
        prosody, prosody_diag = audio_prosody_metrics(path)
        row: dict[str, Any] = {
            "family": family,
            "audio_relpath": audio_relpath,
            "duration_ms": duration_ms,
            "duration_sec": duration_sec,
            "target_sec": target_sec,
            "drift_ms": drift_ms,
            "abs_drift_ms": abs(drift_ms),
            "asr_text": asr_text,
            "similarity": sim,
            "diagnostics": asr_diagnostics + prosody_diag,
            "join_quality": join_quality_report,
            "boundary_repair": boundary_repair or {"enabled": False, "repaired_part_indices": []},
            "source_parts": source_parts or [],
            "selected_part_families": selected_part_families or [],
            **term,
            **prosody,
            **fit,
        }
        if part_index is not None:
            row["part_index"] = part_index
        if split_strategy:
            row["split_strategy"] = split_strategy
        if split_part_count is not None:
            row["split_part_count"] = split_part_count
        join_rescue = self._join_quality_rescue(row)
        if join_rescue is not None:
            row["join_quality_rescue"] = join_rescue
        row["reject_reasons"] = self._reject_reasons(row)
        row["risk_reasons"] = self._risk_reasons(row)
        row["gate_pass"] = not row["reject_reasons"]
        row["score"] = self._candidate_score(row)
        row["strong_gate_pass"] = self._is_strong_gate_pass(row)
        return row

    def _reject_reasons(self, row: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        if float(row.get("similarity") or 0.0) < GATE_DEFAULTS["min_similarity"]:
            reasons.append("low_asr")
        if float(row.get("abs_drift_ms") or 0.0) > GATE_DEFAULTS["max_drift_ms"]:
            reasons.append("high_drift")
        if float(row.get("fit_tempo_delta") or 0.0) > GATE_DEFAULTS["max_fit_delta"]:
            reasons.append("high_fit_delta")
        if row.get("fit_reject_reason") == "high_fit_delta":
            reasons.append("high_fit_delta")
        term_recall = row.get("critical_term_recall")
        if term_recall is not None and float(term_recall) < GATE_DEFAULTS["min_critical_term_recall"]:
            reasons.append("low_term_recall")
        jq = row.get("join_quality")
        if jq is not None and not bool(jq.get("pass")):
            join_rescue = row.get("join_quality_rescue")
            if not (
                isinstance(join_rescue, dict)
                and bool(join_rescue.get("applied"))
            ):
                join_rescue = self._join_quality_rescue(row)
                if join_rescue is not None:
                    row["join_quality_rescue"] = join_rescue
            if not join_rescue:
                reasons.append("bad_join_quality")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _float_or_default(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _join_quality_rescue(self, row: dict[str, Any]) -> dict[str, Any] | None:
        jq = row.get("join_quality")
        if not isinstance(jq, dict) or bool(jq.get("pass")):
            return None

        reasons = [str(item) for item in (jq.get("reasons") or []) if str(item)]
        if not reasons or set(reasons) != {"large_edge_rms_jump"}:
            return None

        if (
            self._float_or_default(row.get("similarity"), 0.0)
            < JOIN_RESCUE_DEFAULTS["min_similarity"]
        ):
            return None
        if (
            self._float_or_default(row.get("abs_drift_ms"), 999999.0)
            > JOIN_RESCUE_DEFAULTS["max_drift_ms"]
        ):
            return None
        if (
            self._float_or_default(row.get("fit_tempo_delta"), 999999.0)
            > JOIN_RESCUE_DEFAULTS["max_fit_delta"]
        ):
            return None
        if row.get("fit_reject_reason") == "high_fit_delta":
            return None

        term_recall = row.get("critical_term_recall")
        if (
            term_recall is not None
            and self._float_or_default(term_recall, 0.0)
            < GATE_DEFAULTS["target_critical_term_recall"]
        ):
            return None

        joins = jq.get("joins")
        if not isinstance(joins, list) or not joins:
            return None

        max_pause = 0.0
        max_prev_tail = -120.0
        max_next_head = -120.0
        max_edge = self._float_or_default(jq.get("max_edge_rms_jump_db"), 0.0)
        for join in joins:
            if not isinstance(join, dict):
                return None
            pause = self._float_or_default(join.get("pause_sec"), 999999.0)
            prev_tail = self._float_or_default(join.get("prev_tail_dbfs"), 0.0)
            next_head = self._float_or_default(join.get("next_head_dbfs"), 0.0)
            edge = self._float_or_default(join.get("edge_rms_jump_db"), 0.0)
            max_pause = max(max_pause, pause)
            max_prev_tail = max(max_prev_tail, prev_tail)
            max_next_head = max(max_next_head, next_head)
            max_edge = max(max_edge, edge)
            if pause > JOIN_RESCUE_DEFAULTS["max_pause_sec"]:
                return None
            if prev_tail > JOIN_RESCUE_DEFAULTS["max_prev_tail_dbfs"]:
                return None
            if next_head > JOIN_RESCUE_DEFAULTS["max_next_head_dbfs"]:
                return None

        if max_edge > JOIN_RESCUE_DEFAULTS["max_edge_rms_jump_db"]:
            return None

        return {
            "applied": True,
            "rule": "quiet_zero_pause_large_edge_rms_jump",
            "original_reasons": reasons,
            "max_edge_rms_jump_db": round(max_edge, 3),
            "max_pause_sec": round(max_pause, 6),
            "max_prev_tail_dbfs": round(max_prev_tail, 3),
            "max_next_head_dbfs": round(max_next_head, 3),
        }

    def _risk_reasons(self, row: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        f0_range = row.get("f0_range_semitones_10_90")
        if f0_range is not None and float(f0_range) < GATE_DEFAULTS["min_f0_range"]:
            reasons.append("flat_f0")
        latin = row.get("latin_surface_recall")
        if latin is not None and float(latin) < 1.0:
            reasons.append("latin_surface_recall_below_1")
        join_rescue = row.get("join_quality_rescue")
        if isinstance(join_rescue, dict) and bool(join_rescue.get("applied")):
            reasons.append("join_quality_rescue")
        boundary_repair = row.get("boundary_repair")
        if (
            isinstance(boundary_repair, dict)
            and boundary_repair.get("repaired_part_indices")
        ):
            reasons.append("boundary_tail_repair")
        return reasons

    def _candidate_score(self, row: dict[str, Any]) -> float:
        similarity = float(row.get("similarity") or 0.0)
        drift = float(row.get("abs_drift_ms") or 0.0)
        f0_range = float(row.get("f0_range_semitones_10_90") or 0.0)
        fit_delta = float(row.get("fit_tempo_delta") or 0.0)
        term_recall = row.get("critical_term_recall")
        drift_score = max(0.0, 1.0 - drift / max(GATE_DEFAULTS["max_drift_ms"], 1.0))
        asr_score = min(1.05, similarity / max(GATE_DEFAULTS["target_similarity"], 1e-6))
        f0_score = (
            min(1.15, f0_range / max(GATE_DEFAULTS["target_f0_range"], 1e-6))
            if f0_range > 0
            else 0.6
        )
        term_score = (
            min(1.05, float(term_recall) / max(GATE_DEFAULTS["target_critical_term_recall"], 1e-6))
            if term_recall is not None
            else 1.0
        )
        fit_penalty = min(0.4, fit_delta * 3.0)
        return 0.28 * drift_score + 0.42 * asr_score + 0.15 * f0_score + 0.12 * term_score - 0.03 * fit_penalty

    def _is_strong_gate_pass(self, row: dict[str, Any]) -> bool:
        if not row.get("gate_pass"):
            return False
        if float(row.get("similarity") or 0.0) < GATE_DEFAULTS["target_similarity"]:
            return False
        if float(row.get("abs_drift_ms") or 0.0) > 300.0:
            return False
        if float(row.get("fit_tempo_delta") or 0.0) > GATE_DEFAULTS["max_fit_delta"]:
            return False
        if row.get("fit_reject_reason") == "high_fit_delta":
            return False
        term_recall = row.get("critical_term_recall")
        if term_recall is not None and float(term_recall) < GATE_DEFAULTS["target_critical_term_recall"]:
            return False
        latin_recall = row.get("latin_surface_recall")
        if latin_recall is not None and float(latin_recall) < 1.0:
            return False
        f0_range = row.get("f0_range_semitones_10_90")
        if f0_range is not None and float(f0_range) < GATE_DEFAULTS["min_f0_range"]:
            return False
        join_rescue = row.get("join_quality_rescue")
        if isinstance(join_rescue, dict) and bool(join_rescue.get("applied")):
            return False
        jq = row.get("join_quality")
        if jq is not None and not bool(jq.get("pass")):
            return False
        return True

    def _select_candidate(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        passed = [row for row in candidates if row.get("gate_pass")]
        if passed:
            best = max(passed, key=lambda row: float(row.get("score") or 0.0))
            mix = self._preferred_split_mix(passed)
            if mix is not None and float(best.get("score") or 0.0) - float(mix.get("score") or 0.0) <= 0.05:
                return mix
            return best
        return max(candidates, key=self._part_quality_key)

    def _preferred_split_mix(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        eligible = []
        for row in candidates:
            if row.get("family") != "split_mix":
                continue
            if float(row.get("similarity") or 0.0) < GATE_DEFAULTS["target_similarity"]:
                continue
            if float(row.get("abs_drift_ms") or 0.0) > 300.0:
                continue
            term_recall = row.get("critical_term_recall")
            if term_recall is not None and float(term_recall) < GATE_DEFAULTS["target_critical_term_recall"]:
                continue
            eligible.append(row)
        if not eligible:
            return None
        return max(eligible, key=lambda row: float(row.get("score") or 0.0))

    def _part_quality_key(self, row: dict[str, Any]) -> tuple[float, float, float, float, float]:
        sim = float(row.get("similarity") or 0.0)
        term = row.get("critical_term_recall")
        term_score = float(term) if term is not None else 1.0
        return (
            1.0 if row.get("gate_pass") else 0.0,
            sim,
            term_score,
            -float(row.get("abs_drift_ms") or 0.0),
            float(row.get("score") or 0.0),
        )

    def _text_compression_recommendation(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        over_slow_rows: list[dict[str, Any]] = []
        strategies: set[str] = set()
        for row in candidates:
            family = str(row.get("family") or "")
            if not family.startswith("split_"):
                continue
            reject_reasons = {str(item) for item in row.get("reject_reasons", [])}
            if "high_fit_delta" not in reject_reasons or "high_drift" not in reject_reasons:
                continue
            if self._float_or_default(row.get("similarity"), 0.0) < GATE_DEFAULTS["target_similarity"]:
                continue
            term_recall = row.get("critical_term_recall")
            if (
                term_recall is not None
                and self._float_or_default(term_recall, 0.0) < GATE_DEFAULTS["target_critical_term_recall"]
            ):
                continue
            jq = row.get("join_quality")
            if isinstance(jq, dict) and not bool(jq.get("pass")):
                continue
            if self._float_or_default(row.get("drift_ms"), 0.0) <= 0.0:
                continue
            fit_delta = self._float_or_default(row.get("fit_tempo_delta"), 0.0)
            if fit_delta < GATE_DEFAULTS["max_fit_delta"] + TEXT_COMPRESSION_FIT_MARGIN:
                continue
            over_slow_rows.append(row)
            strategies.add(str(row.get("split_strategy") or "automatic"))

        if (
            len(over_slow_rows) < TEXT_COMPRESSION_MIN_SPLIT_VARIANTS
            or len(strategies) < TEXT_COMPRESSION_MIN_STRATEGIES
        ):
            return None

        best = min(
            over_slow_rows,
            key=lambda row: (
                self._float_or_default(row.get("fit_tempo_delta"), 999999.0),
                self._float_or_default(row.get("abs_drift_ms"), 999999.0),
            ),
        )
        return {
            "needed": True,
            "reason": "complete_high_similarity_candidates_remain_too_slow",
            "best_family": best.get("family"),
            "best_split_strategy": best.get("split_strategy") or "automatic",
            "best_fit_tempo_delta": round(self._float_or_default(best.get("fit_tempo_delta"), 0.0), 8),
            "best_drift_ms": round(self._float_or_default(best.get("drift_ms"), 0.0), 3),
            "observed_split_variants": len(over_slow_rows),
            "observed_split_strategies": sorted(strategies),
            "max_allowed_fit_delta": GATE_DEFAULTS["max_fit_delta"],
            "action": "shorten_or_rephrase_target_text_before_next_candidate_attempt",
        }

    @staticmethod
    def _split_plan_text_compression_recommendation(split_plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not split_plan or not bool(split_plan.get("needs_text_compression")):
            return None
        risk_parts = split_plan.get("speech_rate_risk_parts") or []
        return {
            "needed": True,
            "reason": "split_part_required_speech_rate_above_safe_limit",
            "max_required_chars_per_sec": split_plan.get("max_required_chars_per_sec"),
            "max_allowed_chars_per_sec": split_plan.get("max_part_chars_per_sec"),
            "risk_part_indexes": risk_parts,
            "action": "shorten_or_rephrase_target_text_or_borrow_source_window_before_candidate_attempt",
        }

    def _candidate_relpath(self, output_relpath: str, name: str) -> str:
        base = PurePosixPath(str(output_relpath).replace("\\", "/"))
        directory = base.parent / f"{base.stem}.lora_candidates"
        return str(directory / f"{name}.wav")

    def _should_split_candidate(
        self,
        request: TTSRequest,
        policy: _LoraCandidatePolicy,
        split_opts: SplitPlanOptions,
    ) -> bool:
        if should_split(request.text, request.target_duration_sec, split_opts):
            return True
        return bool(
            getattr(policy, "dense_eligible", False)
            or getattr(policy, "near_long_eligible", False)
            or getattr(policy, "short_dense_eligible", False)
            or getattr(policy, "short_transition_eligible", False)
            or getattr(policy, "short_term_number_eligible", False)
            or getattr(policy, "retry_escalation_eligible", False)
        )

    def _split_output_name(self, name: str, split_strategy: str) -> str:
        if split_strategy in {"", "automatic"}:
            return name
        return f"{name}_{split_strategy}"

    def _is_distinct_split_plan(self, left: dict[str, Any] | None, right: dict[str, Any]) -> bool:
        if not left:
            return True
        left_parts = [str(part.get("text") or "") for part in left.get("parts", [])]
        right_parts = [str(part.get("text") or "") for part in right.get("parts", [])]
        if left_parts != right_parts:
            return True
        if abs(float(left.get("pause_sec") or 0.0) - float(right.get("pause_sec") or 0.0)) > 1e-6:
            return True
        if (
            abs(float(left.get("planning_pause_sec") or left.get("pause_sec") or 0.0) - float(right.get("planning_pause_sec") or right.get("pause_sec") or 0.0))
            > 1e-6
        ):
            return True
        left_targets = [round(float(part.get("target_sec") or 0.0), 6) for part in left.get("parts", [])]
        right_targets = [round(float(part.get("target_sec") or 0.0), 6) for part in right.get("parts", [])]
        return left_targets != right_targets

    def _runtime_split_plan(
        self,
        primary: dict[str, Any] | None,
        alternates: list[dict[str, Any]],
        selected: dict[str, Any],
    ) -> dict[str, Any] | None:
        selected_strategy = str(selected.get("split_strategy") or "")
        if selected_strategy and selected_strategy != "automatic":
            for plan in alternates:
                if str(plan.get("strategy") or "") == selected_strategy:
                    return plan
        return primary

    def _candidate_meta(self, meta: dict[str, Any] | None, *, family: str) -> dict[str, Any] | None:
        out = copy.deepcopy(meta) if isinstance(meta, dict) else {}
        out["lora_candidate_generation"] = {
            **GENERATION_DEFAULTS,
            "max_mel_headroom": 512 if family.startswith("split_") else 0,
        }
        if family in {"split_light_vector", "light_vector"}:
            out["emo_vector"] = list(LIGHT_VECTOR)
            out["use_emo_text"] = False
        return out


def _asr_language(language: str) -> str:
    value = (language or "").strip().lower()
    if value.startswith("zh"):
        return "zh"
    if value.startswith("en"):
        return "en"
    if value.startswith("ja"):
        return "ja"
    if value.startswith("ko"):
        return "ko"
    return value
