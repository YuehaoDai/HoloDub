from __future__ import annotations

import json
import re
from pathlib import Path

from app.adapters.media import probe_duration
from app.adapters.vad import SpeechSpan
from app.config import Settings
from app.models import Segment, WordToken
from app.services.model_registry import ModelRegistry

PUNCTUATION_ENDINGS = (".", "!", "?", "。", "！", "？", ",", "，", ";", "；", ":", "：")


class ASRAdapter:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry

    def backend_name(self) -> str:
        return self.settings.ml_asr_backend

    def transcribe_words(self, audio_path: Path, source_language: str) -> tuple[list[WordToken], list[str]]:
        if self.settings.ml_asr_backend == "faster_whisper":
            return self._run_faster_whisper(audio_path, source_language)
        return self._run_mock(audio_path)

    def smart_split(
        self,
        audio_path: Path,
        source_language: str,
        min_segment_sec: float,
        max_segment_sec: float,
        speech_spans: list[SpeechSpan] | None = None,
    ) -> tuple[list[Segment], list[str]]:
        words, diagnostics = self.transcribe_words(audio_path, source_language)
        words = assign_speakers(words, speech_spans or [])
        segments = segment_words(words, min_segment_sec, max_segment_sec)
        return segments, diagnostics

    def _run_mock(self, audio_path: Path) -> tuple[list[WordToken], list[str]]:
        candidates = [
            audio_path.with_suffix(audio_path.suffix + ".words.json"),
            audio_path.with_suffix(".words.json"),
            audio_path.with_suffix(".txt"),
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.suffix == ".json":
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    payload = payload.get("words", [])
                words = [WordToken.model_validate(item) for item in payload]
                return words, [f"asr backend=mock sidecar={candidate.name}"]

            text = candidate.read_text(encoding="utf-8").strip()
            tokens = text.split()
            duration_ms = int(probe_duration(self.settings, audio_path) * 1000)
            if not tokens:
                tokens = [audio_path.stem]
            step_ms = max(duration_ms // len(tokens), 1)
            words = []
            for index, token in enumerate(tokens):
                words.append(
                    WordToken(
                        word=token,
                        start_ms=index * step_ms,
                        end_ms=min(duration_ms, (index + 1) * step_ms),
                    )
                )
            return words, [f"asr backend=mock sidecar={candidate.name}"]

        return [
            WordToken(
                word=audio_path.stem,
                start_ms=0,
                end_ms=1000,
            )
        ], ["asr backend=mock generated"]

    def _run_faster_whisper(self, audio_path: Path, source_language: str) -> tuple[list[WordToken], list[str]]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed") from exc

        def loader():
            return WhisperModel(self.settings.faster_whisper_model, device="auto", compute_type="auto")

        model = self.registry.get_or_load(f"faster_whisper:{self.settings.faster_whisper_model}", loader)
        segments, _ = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=source_language or None,
        )
        words: list[WordToken] = []
        for segment in segments:
            segment_words = getattr(segment, "words", None) or []
            if segment_words:
                for word in segment_words:
                    token = (word.word or "").strip()
                    if not token:
                        continue
                    words.append(
                        WordToken(
                            word=token,
                            start_ms=int((word.start or 0.0) * 1000),
                            end_ms=int((word.end or 0.0) * 1000),
                        )
                    )
                continue
            text = (segment.text or "").strip()
            if text:
                words.append(
                    WordToken(
                        word=text,
                        start_ms=int(segment.start * 1000),
                        end_ms=int(segment.end * 1000),
                    )
                )
        if not words:
            raise RuntimeError("faster-whisper returned no words")
        return words, ["asr backend=faster_whisper"]


def assign_speakers(words: list[WordToken], spans: list[SpeechSpan]) -> list[WordToken]:
    if not spans:
        return words
    assigned: list[WordToken] = []
    for word in words:
        midpoint = (word.start_ms + word.end_ms) // 2
        label = word.speaker_label
        for span in spans:
            if span.start_ms <= midpoint <= span.end_ms:
                label = span.speaker_label
                break
        assigned.append(word.model_copy(update={"speaker_label": label}))
    return assigned


def segment_words(words: list[WordToken], min_segment_sec: float, max_segment_sec: float) -> list[Segment]:
    if not words:
        return []

    min_ms = int(min_segment_sec * 1000)
    max_ms = int(max_segment_sec * 1000)

    current: list[WordToken] = []
    segments: list[Segment] = []
    for index, word in enumerate(words):
        current.append(word)
        current_duration = current[-1].end_ms - current[0].start_ms
        next_word = words[index + 1] if index + 1 < len(words) else None
        split_reason = ""

        if next_word is None:
            split_reason = "end"
        else:
            gap_ms = max(next_word.start_ms - word.end_ms, 0)
            if next_word.speaker_label != current[0].speaker_label and current_duration >= min_ms:
                split_reason = "speaker_change"
            elif current_duration >= max_ms:
                split_reason = "max_duration"
            elif current_duration >= min_ms and word.word.endswith(PUNCTUATION_ENDINGS):
                split_reason = "punctuation"
            elif current_duration >= min_ms and gap_ms >= 500:
                split_reason = "silence_gap"

        if split_reason:
            segments.append(
                Segment(
                    start_ms=current[0].start_ms,
                    end_ms=current[-1].end_ms,
                    text=render_text(current),
                    speaker_label=current[0].speaker_label,
                    split_reason=split_reason,
                )
            )
            current = []

    return segments


def render_text(words: list[WordToken]) -> str:
    text = " ".join(word.word.strip() for word in words if word.word.strip())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s+([，。！？；：])", r"\1", text)
    return text.strip()
