from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services.model_registry import ModelRegistry


@dataclass
class SpeechSpan:
    start_ms: int
    end_ms: int
    speaker_label: str = "SPK_01"


class VADAdapter:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry

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
            return Pipeline.from_pretrained(
                self.settings.pyannote_pipeline,
                use_auth_token=self.settings.pyannote_auth_token,
            )

        pipeline = self.registry.get_or_load("pyannote_pipeline", loader)
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
