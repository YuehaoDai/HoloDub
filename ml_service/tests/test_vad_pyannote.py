from __future__ import annotations

import sys
from types import ModuleType

from app.adapters.vad import VADAdapter, _spans_from_scores
from app.config import Settings
from app.services.model_registry import ModelRegistry


def test_pyannote_loader_uses_current_token_kwarg(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeTurn:
        start = 1.25
        end = 2.5

    class FakeDiarization:
        def itertracks(self, yield_label: bool = False):
            assert yield_label is True
            yield FakeTurn(), None, "SPEAKER_00"

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, name: str, **kwargs):
            calls["name"] = name
            calls["kwargs"] = kwargs
            return cls()

        def __call__(self, audio_path: str):
            calls["audio_path"] = audio_path
            return FakeDiarization()

    pyannote_module = ModuleType("pyannote")
    audio_module = ModuleType("pyannote.audio")
    audio_module.Pipeline = FakePipeline
    pyannote_module.audio = audio_module
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_module)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_module)

    audio_path = tmp_path / "clip.wav"
    settings = Settings(
        data_root=tmp_path,
        ml_vad_backend="pyannote",
        pyannote_pipeline="pyannote/test-pipeline",
        pyannote_auth_token="hf_test_token",
    )

    spans, diagnostics = VADAdapter(settings, ModelRegistry()).analyze(audio_path)

    assert calls["name"] == "pyannote/test-pipeline"
    assert calls["kwargs"] == {"token": "hf_test_token"}
    assert calls["audio_path"] == str(audio_path)
    assert spans[0].start_ms == 1250
    assert spans[0].end_ms == 2500
    assert spans[0].speaker_label == "SPEAKER_00"
    assert diagnostics == ["vad backend=pyannote"]


def test_pyannote_pipeline_failure_uses_cached_segmentation_fallback(tmp_path, monkeypatch) -> None:
    calls = {"pipeline": 0, "segmentation": 0}

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, name: str, **kwargs):
            calls["pipeline"] += 1
            raise RuntimeError("403 gated pipeline")

    pyannote_module = ModuleType("pyannote")
    audio_module = ModuleType("pyannote.audio")
    audio_module.Pipeline = FakePipeline
    pyannote_module.audio = audio_module
    monkeypatch.setitem(sys.modules, "pyannote", pyannote_module)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_module)

    settings = Settings(
        data_root=tmp_path,
        ml_vad_backend="pyannote",
        pyannote_pipeline="pyannote/gated-pipeline",
        pyannote_auth_token="hf_test_token",
    )
    adapter = VADAdapter(settings, ModelRegistry())

    def fake_segmentation(audio_path):
        calls["segmentation"] += 1
        return [], ["vad backend=pyannote mode=segmentation"]

    monkeypatch.setattr(adapter, "_run_pyannote_segmentation", fake_segmentation)

    first_spans, first_diagnostics = adapter.analyze(tmp_path / "a.wav")
    second_spans, second_diagnostics = adapter.analyze(tmp_path / "b.wav")

    assert first_spans == []
    assert second_spans == []
    assert calls == {"pipeline": 1, "segmentation": 2}
    assert "pipeline_fallback=segmentation" in first_diagnostics[0]
    assert "cached=true" in second_diagnostics[0]


def test_spans_from_pyannote_scores_uses_hysteresis_and_gap_merge() -> None:
    class Window:
        def __init__(self, start: float, end: float) -> None:
            self.start = start
            self.end = end

    windows = [Window(i * 0.1, i * 0.1 + 0.1) for i in range(12)]
    spans = _spans_from_scores(
        scores=[0.0, 0.6, 0.7, 0.4, 0.0, 0.65, 0.7, 0.3, 0.0, 0.8, 0.2, 0.0],
        windows=windows,
        onset=0.5,
        offset=0.45,
        min_speech_ms=150,
        min_silence_ms=250,
    )

    assert [(span.start_ms, span.end_ms, span.speaker_label) for span in spans] == [
        (100, 1100, "SPK_01"),
    ]
