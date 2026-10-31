from __future__ import annotations

import math
import re
import subprocess
import wave
from array import array
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.adapters.media import probe_duration

LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*|\d+(?:\.\d+)?")
CHINESE_DOMAIN_TERMS = (
    "\u65e5\u5fd7\u6761\u76ee",
    "\u8ffd\u52a0\u6761\u76ee",
    "\u7ebf\u6027\u4e00\u81f4\u6027",
    "\u6267\u884c\u5386\u53f2",
    "\u6301\u4e45\u5316",
    "\u6613\u5931\u6027",
    "\u5168\u5e8f",
    "\u65e5\u5fd7",
    "\u8ddf\u968f\u8005",
    "\u9886\u5bfc\u8005",
    "\u5feb\u7167",
    "\u673a\u68b0\u786c\u76d8",
    "\u4ecb\u8d28",
)
TERM_ALIASES: dict[str, tuple[str, ...]] = {
    "\u65e5\u5fd7\u6761\u76ee": (
        "\u65e5\u5236\u6761\u76ee",
        "\u65e5\u7f6e\u6761\u76ee",
        "\u65e5\u4e4b\u6761\u76ee",
        "\u65e5\u65e5\u6761\u76ee",
    ),
    "\u65e5\u5fd7": (
        "\u65e5\u5236",
        "\u65e5\u7f6e",
        "\u65e5\u4e4b",
        "\u65e5\u65e5",
        "\u65e5\u6cbb",
    ),
    "\u6613\u5931\u6027": ("\u610f\u8bc6\u6027",),
    "\u5168\u5e8f": ("\u6743\u5e8f",),
    "a": ("a", "\u8bf6", "\u6b38"),
    "b": ("b", "\u6bd4"),
    "x": ("x", "\u53c9", "\u827e\u514b\u65af", "\u57c3\u514b\u65af"),
    "raft": ("raft", "\u62c9\u592b\u7279"),
    "rpc": ("rpc", "r p c"),
    "fsync": ("fsync", "f sync"),
    "appendentries": (
        "appendentries",
        "append entries",
        "append entry",
        "appendantries",
        "appendant trees",
        "appendant trace",
        "appendant entries",
    ),
    "xterm": ("xterm", "x term"),
    "rap": ("rap",),
    "1": ("1", "\u4e00"),
    "2": ("2", "\u4e8c", "\u4e24"),
    "10": ("10", "\u5341"),
}

CHINESE_DIGITS = "\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d"
SECTION_NUMBER_RE = re.compile(r"\u7b2c?(\d{1,2})\.(\d{1,2})\.(\d{1,2})\u8282?")
DIGIT_RE = re.compile(r"\d")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
FILLER_RE = re.compile(r"[\u55ef\u54e6\u5463\u554a]+")
PUNCT_RE = re.compile(r"[\s,.\!?;:\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a\uff08\uff09\u300a\u300b\u201c\u201d\"'`~\-\u2013\u2014]+")
ASR_HOMOPHONE_NORMALIZATIONS: tuple[tuple[str, str], ...] = (
    ("\u65e5\u5236\u6761\u76ee", "\u65e5\u5fd7\u6761\u76ee"),
    ("\u65e5\u7f6e\u6761\u76ee", "\u65e5\u5fd7\u6761\u76ee"),
    ("\u65e5\u4e4b\u6761\u76ee", "\u65e5\u5fd7\u6761\u76ee"),
    ("\u65e5\u65e5\u6761\u76ee", "\u65e5\u5fd7\u6761\u76ee"),
    ("\u65e5\u5236", "\u65e5\u5fd7"),
    ("\u65e5\u7f6e", "\u65e5\u5fd7"),
    ("\u65e5\u4e4b", "\u65e5\u5fd7"),
    ("\u65e5\u65e5", "\u65e5\u5fd7"),
    ("\u65e5\u6cbb", "\u65e5\u5fd7"),
    ("\u610f\u8bc6\u6027", "\u6613\u5931\u6027"),
    ("\u6743\u5e8f", "\u5168\u5e8f"),
    ("\u66f9\u9b4f", "\u69fd\u4f4d"),
    ("\u66f9\u536b", "\u69fd\u4f4d"),
    ("\u5f53\u673a", "\u5b95\u673a"),
    ("\u4ed6", "\u5b83"),
    ("\u5979", "\u5b83"),
)


def normalize_section_number(match: re.Match[str]) -> str:
    return (
        "\u7b2c"
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(1))
        + "\u70b9"
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(2))
        + "\u70b9"
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(3))
        + "\u8282"
    )


def chinese_number(value: int) -> str:
    if value < 0 or value > 99:
        return ""
    if value < 10:
        return CHINESE_DIGITS[value]
    tens, ones = divmod(value, 10)
    if value == 10:
        return "\u5341"
    if tens == 1:
        return "\u5341" + (CHINESE_DIGITS[ones] if ones else "")
    return CHINESE_DIGITS[tens] + "\u5341" + (CHINESE_DIGITS[ones] if ones else "")


def normalize_number_token(match: re.Match[str]) -> str:
    raw = match.group(0)
    if "." in raw:
        return "\u70b9".join(
            "".join(CHINESE_DIGITS[int(char)] for char in part)
            for part in raw.split(".")
        )
    if len(raw) <= 2 and (len(raw) == 1 or not raw.startswith("0")):
        spoken = chinese_number(int(raw))
        if spoken:
            return spoken
    return "".join(CHINESE_DIGITS[int(char)] for char in raw)


def normalize_digits(text: str) -> str:
    return NUMBER_RE.sub(normalize_number_token, text)


def normalize_asr_text(text: str) -> str:
    normalized = text or ""
    normalized = SECTION_NUMBER_RE.sub(normalize_section_number, normalized)
    for source, target in ASR_HOMOPHONE_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    normalized = normalize_digits(normalized)
    normalized = FILLER_RE.sub("", normalized)
    return PUNCT_RE.sub("", normalized).lower()


def asr_similarity(expected: str, actual: str) -> float:
    left = normalize_asr_text(expected)
    right = normalize_asr_text(actual)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def normalize_term(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def split_camel(term: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", term)


def digit_term_aliases(term: str) -> list[str]:
    if not term.isdigit():
        return []
    aliases = ["".join(CHINESE_DIGITS[int(char)] for char in term)]
    numeric = chinese_number(int(term)) if len(term) <= 2 else ""
    if numeric:
        aliases.append(numeric)
    if "2" in term:
        aliases.append("".join("\u4e24" if char == "2" else CHINESE_DIGITS[int(char)] for char in term))
    return aliases


def extract_critical_terms(text: str) -> list[str]:
    terms: list[str] = []
    for term in CHINESE_DOMAIN_TERMS:
        if term in (text or "") and term not in terms:
            terms.append(term)
    for match in LATIN_TERM_RE.finditer(text or ""):
        raw = match.group(0).strip("._+-")
        normalized = normalize_term(raw)
        if not normalized:
            continue
        if len(normalized) == 1 and not (
            normalized.isdigit() or normalized in {"a", "b", "x", "y"}
        ):
            continue
        if normalized not in terms:
            terms.append(normalized)
    return terms


def term_aliases(term: str) -> list[str]:
    aliases = [term]
    aliases.extend(TERM_ALIASES.get(term, ()))
    aliases.extend(digit_term_aliases(normalize_term(term)))
    split = split_camel(term)
    if split != term:
        aliases.append(split)
    return list(dict.fromkeys(aliases))


def term_present(term: str, actual_text: str) -> bool:
    actual_normalized = normalize_term(actual_text)
    actual_lower = (actual_text or "").lower()
    for alias in term_aliases(term):
        alias_normalized = normalize_term(alias)
        if alias_normalized and alias_normalized in actual_normalized:
            return True
        if alias.lower() in actual_lower:
            return True
    return False


def latin_surface_present(term: str, actual_text: str) -> bool:
    normalized = normalize_term(term)
    return bool(normalized) and normalized in normalize_term(actual_text)


def is_soft_latin_technical_term(term: str) -> bool:
    normalized = normalize_term(term)
    if normalized in {"a", "b", "x", "y"}:
        return False
    return any("a" <= char <= "z" for char in normalized)


def critical_term_metrics(expected: str, actual: str) -> dict[str, Any]:
    terms = extract_critical_terms(expected)
    hard_terms = [term for term in terms if not is_soft_latin_technical_term(term)]
    matched = [term for term in hard_terms if term_present(term, actual)]
    latin_terms = [term for term in terms if normalize_term(term)]
    latin_surface = [term for term in latin_terms if latin_surface_present(term, actual)]
    soft_latin_terms = [term for term in terms if is_soft_latin_technical_term(term)]
    soft_latin_matched = [term for term in soft_latin_terms if term_present(term, actual)]
    return {
        "all_detected_terms": terms,
        "critical_terms": hard_terms,
        "critical_terms_matched": matched,
        "critical_terms_missing": [term for term in hard_terms if term not in matched],
        "critical_term_recall": len(matched) / len(hard_terms) if hard_terms else None,
        "soft_latin_terms": soft_latin_terms,
        "soft_latin_terms_matched": soft_latin_matched,
        "soft_latin_terms_missing": [
            term for term in soft_latin_terms if term not in soft_latin_matched
        ],
        "latin_surface_terms_matched": latin_surface,
        "latin_surface_terms_missing": [
            term for term in latin_terms if term not in latin_surface
        ],
        "latin_surface_recall": len(latin_surface) / len(latin_terms) if latin_terms else None,
    }


def _quantile(values: Any, q: float) -> float:
    if values.numel() == 0:
        return 0.0
    import torch

    return float(torch.quantile(values.float(), q).item())


def _semitone_range(low_hz: float, high_hz: float) -> float:
    if low_hz <= 0 or high_hz <= 0:
        return 0.0
    return 12.0 * math.log2(high_hz / low_hz)


def audio_prosody_metrics(path: Path, *, sample_rate: int = 16000) -> tuple[dict[str, Any], list[str]]:
    try:
        import torch
        import torchaudio
        from torchaudio.functional import detect_pitch_frequency
    except ImportError as exc:  # pragma: no cover - real extras only
        return {}, [f"prosody unavailable={exc.__class__.__name__}"]

    wav, native_rate = torchaudio.load(str(path))
    if wav.ndim == 2 and wav.size(0) > 1:
        wav = wav.mean(dim=0, keepdim=True)
    pitch_wav = wav
    if int(native_rate) != sample_rate:
        pitch_wav = torchaudio.functional.resample(wav, int(native_rate), sample_rate)
    f0 = detect_pitch_frequency(
        pitch_wav,
        sample_rate,
        frame_time=0.02,
        win_length=30,
        freq_low=60,
        freq_high=500,
    )[0]
    voiced = f0[(f0 >= 60) & (f0 <= 500)].float()
    metrics: dict[str, Any] = {"voiced_frame_count": int(voiced.numel())}
    if voiced.numel() > 5:
        median = float(voiced.median().item())
        p10 = _quantile(voiced, 0.10)
        p90 = _quantile(voiced, 0.90)
        semitones = 12.0 * torch.log2(voiced / max(median, 1e-6))
        metrics.update(
            {
                "f0_hz_median": median,
                "f0_hz_p10": p10,
                "f0_hz_p90": p90,
                "f0_range_semitones_10_90": _semitone_range(p10, p90),
                "f0_std_semitones": float(semitones.std(unbiased=False).item()),
            }
        )
    return metrics, []


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def read_wav_samples(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as handle:
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM wav for RMS check, got sample width {width}: {path}")
    samples = array("h")
    samples.frombytes(frames)
    return samples, rate


def fade_tail(path: Path, output_path: Path, fade_ms: float) -> Path:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(handle.getnframes())
    if params.sampwidth != 2:
        raise ValueError(f"expected 16-bit PCM wav for tail fade, got sample width {params.sampwidth}: {path}")
    samples = array("h")
    samples.frombytes(frames)
    if not samples:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as handle:
            handle.setparams(params)
            handle.writeframes(frames)
        return output_path

    channels = max(1, int(params.nchannels))
    fade_frames = max(1, int(params.framerate * max(1.0, fade_ms) / 1000.0))
    fade_samples = min(len(samples), fade_frames * channels)
    start = len(samples) - fade_samples
    frame_count = max(1, fade_samples // channels)
    for offset in range(fade_samples):
        frame = offset // channels
        gain = max(0.0, 1.0 - (frame + 1) / frame_count)
        samples[start + offset] = int(samples[start + offset] * gain)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as handle:
        handle.setparams(params)
        handle.writeframes(samples.tobytes())
    return output_path


def rms_dbfs(samples: array) -> float:
    if not samples:
        return -120.0
    total = sum(float(sample) * float(sample) for sample in samples)
    rms = math.sqrt(total / len(samples))
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms / 32768.0))


def edge_rms(path: Path, window_ms: float) -> dict[str, float]:
    samples, rate = read_wav_samples(path)
    window = max(1, int(rate * max(window_ms, 1.0) / 1000.0))
    return {
        "head_dbfs": round(rms_dbfs(samples[:window]), 3),
        "tail_dbfs": round(rms_dbfs(samples[-window:]), 3),
    }


def join_quality(
    part_paths: list[Path],
    *,
    pause_sec: float,
    join_window_ms: float = 120.0,
    max_edge_rms_jump_db: float = 16.0,
    max_boundary_tail_dbfs: float = -24.0,
) -> dict[str, Any]:
    edges = [edge_rms(path, join_window_ms) for path in part_paths]
    joins: list[dict[str, Any]] = []
    for index in range(len(edges) - 1):
        jump = abs(float(edges[index]["tail_dbfs"]) - float(edges[index + 1]["head_dbfs"]))
        joins.append(
            {
                "index": index + 1,
                "prev_tail_dbfs": edges[index]["tail_dbfs"],
                "next_head_dbfs": edges[index + 1]["head_dbfs"],
                "edge_rms_jump_db": round(jump, 3),
                "pause_sec": pause_sec,
            }
        )
    reasons: list[str] = []
    max_jump = max((float(join["edge_rms_jump_db"]) for join in joins), default=0.0)
    if pause_sec <= 0.02 and max_jump > max_edge_rms_jump_db:
        reasons.append("large_edge_rms_jump")
    if any(float(join["prev_tail_dbfs"]) > max_boundary_tail_dbfs for join in joins):
        reasons.append("loud_pre_join_tail")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "max_edge_rms_jump_db": round(max_jump, 3),
        "max_boundary_tail_dbfs": max_boundary_tail_dbfs,
        "joins": joins,
    }


def repair_loud_boundary_tails(
    part_paths: list[Path],
    output_dir: Path,
    boundary_repair_id: str,
    *,
    enabled: bool,
    fade_ms: float = 120.0,
    join_window_ms: float = 120.0,
    max_boundary_tail_dbfs: float = -24.0,
) -> tuple[list[Path], dict[str, Any]]:
    if not enabled or len(part_paths) < 2 or fade_ms <= 0:
        return part_paths, {"enabled": False, "repaired_part_indices": []}

    repaired_paths = list(part_paths)
    repairs: list[dict[str, Any]] = []
    for index, path in enumerate(part_paths[:-1]):
        edge = edge_rms(path, join_window_ms)
        tail_dbfs = float(edge["tail_dbfs"])
        if tail_dbfs <= float(max_boundary_tail_dbfs):
            continue
        repaired_path = output_dir / "_boundary_repair" / f"{boundary_repair_id}_p{index + 1}_tailfade.wav"
        repaired_paths[index] = fade_tail(path, repaired_path, fade_ms)
        repaired_edge = edge_rms(repaired_paths[index], join_window_ms)
        repairs.append(
            {
                "part_index": index + 1,
                "source_path": str(path),
                "repaired_path": str(repaired_paths[index]),
                "fade_ms": fade_ms,
                "tail_dbfs_before": edge["tail_dbfs"],
                "tail_dbfs_after": repaired_edge["tail_dbfs"],
            }
        )

    return repaired_paths, {
        "enabled": True,
        "fade_ms": fade_ms,
        "repaired_part_indices": [item["part_index"] for item in repairs],
        "repairs": repairs,
    }


def concat_audio(
    settings: Any,
    part_paths: list[Path],
    output_path: Path,
    *,
    pause_sec: float,
    repair_loud_boundary_tails_enabled: bool = False,
    boundary_tail_fade_ms: float = 120.0,
    join_window_ms: float = 120.0,
    max_boundary_tail_dbfs: float = -24.0,
) -> tuple[list[Path], dict[str, Any]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_paths, boundary_repair = repair_loud_boundary_tails(
        part_paths,
        output_path.parent,
        output_path.stem,
        enabled=repair_loud_boundary_tails_enabled,
        fade_ms=boundary_tail_fade_ms,
        join_window_ms=join_window_ms,
        max_boundary_tail_dbfs=max_boundary_tail_dbfs,
    )
    command = [settings.ffmpeg_bin, "-y", "-v", "error"]
    filter_inputs: list[str] = []
    input_index = 0
    for index, path in enumerate(part_paths):
        command.extend(["-i", str(path)])
        filter_inputs.append(f"[{input_index}:a]")
        input_index += 1
        if index < len(part_paths) - 1 and pause_sec > 0:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-t",
                    f"{pause_sec:.6f}",
                    "-i",
                    f"anullsrc=r={settings.default_sample_rate}:cl=mono",
                ]
            )
            filter_inputs.append(f"[{input_index}:a]")
            input_index += 1
    command.extend(
        [
            "-filter_complex",
            "".join(filter_inputs) + f"concat=n={len(filter_inputs)}:v=0:a=1",
            "-ar",
            str(settings.default_sample_rate),
            "-ac",
            str(settings.default_channels),
            str(output_path),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)
    return part_paths, boundary_repair


def fit_audio_duration(
    settings: Any,
    path: Path,
    target_sec: float,
    *,
    max_delta: float = 0.05,
    min_abs_drift_ms: float = 40.0,
) -> dict[str, Any]:
    actual_sec = probe_duration(settings, path)
    drift_ms = (actual_sec - target_sec) * 1000.0
    result: dict[str, Any] = {
        "fit_filter": "atempo",
        "fit_applied": False,
        "fit_tempo_delta": 0.0,
        "fit_reject_reason": "",
        "pre_fit_duration_sec": actual_sec,
        "pre_fit_drift_ms": drift_ms,
    }
    if target_sec <= 0 or abs(drift_ms) < min_abs_drift_ms:
        return result
    tempo = actual_sec / target_sec
    delta = abs(tempo - 1.0)
    result["fit_tempo_delta"] = round(delta, 8)
    if delta > max_delta:
        result["fit_reject_reason"] = "high_fit_delta"
        return result
    fitted = path.with_suffix(".fit.tmp.wav")
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-i",
            str(path),
            "-filter:a",
            f"atempo={tempo:.8f}",
            "-ar",
            str(settings.default_sample_rate),
            "-ac",
            str(settings.default_channels),
            str(fitted),
        ],
        check=True,
        capture_output=True,
    )
    fitted.replace(path)
    result["fit_applied"] = True
    result["post_fit_duration_sec"] = probe_duration(settings, path)
    result["post_fit_drift_ms"] = (float(result["post_fit_duration_sec"]) - target_sec) * 1000.0
    return result
