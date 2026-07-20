from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


@dataclass
class SpeechSpan:
    start_ms: int
    end_ms: int
    speaker_label: str = "SPK_01"


class VADAdapter:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self._pipeline_fallback_reasons: dict[str, str] = {}

    def backend_name(self) -> str:
        return self.settings.ml_vad_backend

    def analyze(self, audio_path: Path) -> tuple[list[SpeechSpan], list[str]]:
        if self.settings.ml_vad_backend == "pyannote":
            return self._run_pyannote(audio_path)
        return [], [f"vad backend={self.settings.ml_vad_backend}"]

    def _run_pyannote(self, audio_path: Path) -> tuple[list[SpeechSpan], list[str]]:
        if not self.settings.pyannote_auth_token:
            raise RuntimeError("PYANNOTE_AUTH_TOKEN is required when ML_VAD_BACKEND=pyannote")

        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise RuntimeError("pyannote.audio is not installed") from exc

        def loader():
            pipeline = Pipeline.from_pretrained(
                self.settings.pyannote_pipeline,
                token=self.settings.pyannote_auth_token,
            )
            device = _pyannote_device(self.settings.pyannote_device)
            if device is not None and hasattr(pipeline, "to"):
                pipeline.to(device)
            return pipeline

        pipeline_key = f"pyannote_pipeline:{self.settings.pyannote_pipeline}"
        if pipeline_key in self._pipeline_fallback_reasons:
            spans, diagnostics = self._run_pyannote_segmentation(audio_path)
            return spans, [
                "vad backend=pyannote pipeline_fallback=segmentation cached=true",
                f"pyannote pipeline_error={self._pipeline_fallback_reasons[pipeline_key]}",
                *diagnostics,
            ]

        try:
            pipeline = self.registry.get_or_load(pipeline_key, loader)
        except Exception as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            self._pipeline_fallback_reasons[pipeline_key] = reason
            logger.warning(
                "pyannote pipeline failed; falling back to segmentation VAD",
                extra={"pipeline": self.settings.pyannote_pipeline, "reason": reason},
            )
            spans, diagnostics = self._run_pyannote_segmentation(audio_path)
            return spans, [
                "vad backend=pyannote pipeline_fallback=segmentation",
                f"pyannote pipeline_error={reason}",
                *diagnostics,
            ]

        diarization = pipeline(str(audio_path))
        spans: list[SpeechSpan] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            spans.append(
                SpeechSpan(
                    start_ms=int(turn.start * 1000),
                    end_ms=int(turn.end * 1000),
                    speaker_label=str(speaker),
                )
            )
        return spans, ["vad backend=pyannote"]

    def _run_pyannote_segmentation(self, audio_path: Path) -> tuple[list[SpeechSpan], list[str]]:
        try:
            import numpy as np
            from pyannote.audio import Inference, Model
        except ImportError as exc:
            raise RuntimeError("pyannote.audio segmentation dependencies are not installed") from exc

        def loader():
            model = Model.from_pretrained(
                self.settings.pyannote_segmentation_model,
                token=self.settings.pyannote_auth_token,
            )
            device = _pyannote_device(self.settings.pyannote_device)
            if device is not None and hasattr(model, "to"):
                model.to(device)
            return model

        model = self.registry.get_or_load(
            f"pyannote_segmentation:{self.settings.pyannote_segmentation_model}",
            loader,
        )

        def speech_probability(scores):
            return np.max(scores, axis=-1, keepdims=True)

        inference = Inference(
            model,
            pre_aggregation_hook=speech_probability,
            batch_size=max(1, self.settings.pyannote_inference_batch_size),
        )
        output = inference(str(audio_path))
        scores = [float(feature[0]) for feature, _window in output.iterfeatures(window=True)]
        windows = [window for _feature, window in output.iterfeatures(window=True)]
        spans = _spans_from_scores(
            scores=scores,
            windows=windows,
            onset=self.settings.pyannote_segmentation_onset,
            offset=self.settings.pyannote_segmentation_offset,
            min_speech_ms=self.settings.pyannote_min_speech_ms,
            min_silence_ms=self.settings.pyannote_min_silence_ms,
        )
        return spans, [
            "vad backend=pyannote mode=segmentation",
            f"pyannote segmentation_model={self.settings.pyannote_segmentation_model}",
        ]


def _pyannote_device(device_name: str):
    normalized = (device_name or "auto").strip().lower()
    if normalized in {"", "auto"}:
        try:
            import torch
        except ImportError:
            return None
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    try:
        import torch
    except ImportError:
        return None
    return torch.device(normalized)


def _spans_from_scores(
    *,
    scores,
    windows,
    onset: float,
    offset: float,
    min_speech_ms: int,
    min_silence_ms: int,
) -> list[SpeechSpan]:
    raw: list[SpeechSpan] = []
    active = False
    start_sec = 0.0
    last_end_sec = 0.0
    for score, window in zip(scores, windows):
        if not active and score >= onset:
            start_sec = float(window.start)
            active = True
        if active:
            last_end_sec = float(window.end)
            if score < offset:
                raw.append(
                    SpeechSpan(
                        start_ms=max(0, int(start_sec * 1000)),
                        end_ms=max(0, int(last_end_sec * 1000)),
                    )
                )
                active = False
    if active:
        raw.append(
            SpeechSpan(
                start_ms=max(0, int(start_sec * 1000)),
                end_ms=max(0, int(last_end_sec * 1000)),
            )
        )

    merged: list[SpeechSpan] = []
    for span in raw:
        if span.end_ms - span.start_ms < min_speech_ms:
            continue
        if merged and span.start_ms - merged[-1].end_ms <= min_silence_ms:
            merged[-1].end_ms = max(merged[-1].end_ms, span.end_ms)
            continue
        merged.append(span)
    return merged
