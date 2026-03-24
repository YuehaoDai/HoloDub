from __future__ import annotations

import json
import re
from pathlib import Path

from app.adapters.media import probe_duration
from app.adapters.vad import SpeechSpan
from app.config import Settings
from app.models import Segment, WordToken
from app.services.model_registry import ModelRegistry

# Only hard sentence boundaries trigger a split — commas/semicolons/colons are
# clause separators, not sentence ends, and splitting there creates tiny fragments.
SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？")


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
        hard_max_segment_sec: float = 45.0,
        close_gap_ms: int = 800,
        speech_spans: list[SpeechSpan] | None = None,
    ) -> tuple[list[Segment], list[str]]:
        words, diagnostics = self.transcribe_words(audio_path, source_language)
        words = assign_speakers(words, speech_spans or [])
        segments = segment_words(words, min_segment_sec, max_segment_sec, hard_max_segment_sec, close_gap_ms)
        return segments, diagnostics

    def align_sentences(self, audio_path: Path, text: str, language: str = "zh") -> None:
        raise NotImplementedError("align_sentences removed in rollback")

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
            vad_filter=self.settings.faster_whisper_vad_filter,
            beam_size=self.settings.faster_whisper_beam_size,
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


def segment_words(
    words: list[WordToken],
    min_segment_sec: float,
    max_segment_sec: float,
    hard_max_segment_sec: float = 45.0,
    close_gap_ms: int = 800,
) -> list[Segment]:
    if not words:
        return []

    min_ms = int(min_segment_sec * 1000)
    max_ms = int(max_segment_sec * 1000)
    hard_max_ms = int(hard_max_segment_sec * 1000)
    # Require at least this many words before allowing a punctuation/silence split.
    # This prevents single-word or two-word micro-segments that TTS handles poorly.
    min_word_count = 5
    # Base silence gap required to count as a phrase boundary.
    # When a segment grows beyond the "medium" threshold below, this requirement
    # is relaxed progressively so that long segments break on shorter pauses.
    base_silence_threshold_ms = 800
    # Once a segment exceeds this duration, start lowering the silence threshold.
    progressive_threshold_ms = 25_000

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
            word_count = len(current)

            # Progressive silence threshold: relax as the segment gets longer so
            # that segments beyond 25 s are more willing to split on shorter pauses.
            # At 25 s the threshold stays at 800 ms; by 45 s it drops to ~300 ms.
            if current_duration <= progressive_threshold_ms:
                silence_threshold_ms = base_silence_threshold_ms
            else:
                excess_ratio = min((current_duration - progressive_threshold_ms) /
                                   (hard_max_ms - progressive_threshold_ms), 1.0)
                silence_threshold_ms = int(base_silence_threshold_ms - excess_ratio * 500)
                silence_threshold_ms = max(silence_threshold_ms, 300)

            if next_word.speaker_label != current[0].speaker_label and current_duration >= min_ms:
                split_reason = "speaker_change"
            elif current_duration >= max_ms:
                split_reason = "max_duration"
            elif (current_duration >= min_ms
                  and word_count >= min_word_count
                  and word.word.rstrip().endswith(SENTENCE_ENDINGS)):
                split_reason = "punctuation"
            elif (current_duration >= min_ms
                  and word_count >= min_word_count
                  and gap_ms >= silence_threshold_ms):
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

    # Post-pass 1: merge any segment that is too short (< min_word_count words),
    # but never let the merged result exceed hard_max_ms.
    segments = _merge_short_segments(segments, min_word_count, hard_max_ms)

    # Post-pass 2: merge consecutive segments whose gap is dangerously tight.
    # Segments separated by less than close_gap_ms have very little breathing room
    # between them; merging prevents TTS spillover.  close_gap_ms defaults to 800 ms
    # (down from the old 1500 ms) to avoid over-aggressively chaining segments.
    # hard_max_ms acts as a safety ceiling so no merged segment can exceed it.
    segments = _merge_close_gap_segments(segments, close_gap_ms=close_gap_ms, hard_max_ms=hard_max_ms)

    return segments


def _merge_short_segments(segments: list[Segment], min_word_count: int, hard_max_ms: int = 0) -> list[Segment]:
    """Merge segments that contain fewer than min_word_count words into a neighbour.

    If hard_max_ms > 0, a merge is skipped when the combined duration would exceed
    the ceiling, leaving the short segment in place rather than bloating its neighbour.
    """
    if len(segments) <= 1:
        return segments

    def word_count(seg: Segment) -> int:
        return len(seg.text.split())

    def duration_ms(seg: Segment) -> int:
        return seg.end_ms - seg.start_ms

    def _merge(a: Segment, b: Segment) -> Segment:
        return Segment(
            start_ms=a.start_ms,
            end_ms=b.end_ms,
            text=a.text + " " + b.text,
            speaker_label=a.speaker_label,
            split_reason=a.split_reason,
        )

    merged = True
    while merged:
        merged = False
        result: list[Segment] = []
        i = 0
        while i < len(segments):
            seg = segments[i]
            if word_count(seg) < min_word_count and len(segments) > 1:
                # Merge: prefer merging with the previous segment if it exists,
                # but only if the combined duration stays within hard_max_ms.
                if result and (hard_max_ms <= 0 or duration_ms(_merge(result[-1], seg)) <= hard_max_ms):
                    result[-1] = _merge(result[-1], seg)
                    merged = True
                elif i + 1 < len(segments) and (hard_max_ms <= 0 or duration_ms(_merge(seg, segments[i + 1])) <= hard_max_ms):
                    segments[i + 1] = _merge(seg, segments[i + 1])
                    i += 1
                    merged = True
                    continue
                else:
                    result.append(seg)
            else:
                result.append(seg)
            i += 1
        segments = result

    return segments


def _merge_close_gap_segments(segments: list[Segment], close_gap_ms: int, hard_max_ms: int = 0) -> list[Segment]:
    """Merge consecutive segments whose inter-segment gap is shorter than close_gap_ms.

    A tight gap means the TTS audio for the first segment has very little room
    before the next sentence starts.  Even modest over-runs will cause spillover
    clipping.  Merging the two sentences into one gives the TTS model a longer,
    more natural utterance and removes the problematic boundary altogether.

    If hard_max_ms > 0, a merge is rejected when the combined segment would exceed
    the hard ceiling.  This prevents the greedy forward chain from snowballing into
    very long segments when many consecutive inter-segment gaps are all below the
    threshold (e.g., continuous narration with sub-second pauses throughout).

    We iterate forward and keep merging as long as the gap to the *next* segment
    is below the threshold.  The combined segment inherits the start of the first
    and the end of the last.
    """
    if len(segments) <= 1:
        return segments

    result: list[Segment] = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        # Greedily merge forward while the gap to the next segment is too tight,
        # stopping if the merged result would exceed hard_max_ms.
        while i + 1 < len(segments):
            nxt = segments[i + 1]
            gap_ms = nxt.start_ms - seg.end_ms
            if gap_ms >= close_gap_ms:
                break
            merged_end = nxt.end_ms
            if hard_max_ms > 0 and merged_end - seg.start_ms > hard_max_ms:
                break
            seg = Segment(
                start_ms=seg.start_ms,
                end_ms=nxt.end_ms,
                text=seg.text + " " + nxt.text,
                speaker_label=seg.speaker_label,
                split_reason=seg.split_reason,
            )
            i += 1
        result.append(seg)
        i += 1
    return result


def render_text(words: list[WordToken]) -> str:
    text = " ".join(word.word.strip() for word in words if word.word.strip())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s+([，。！？；：])", r"\1", text)
    return text.strip()
