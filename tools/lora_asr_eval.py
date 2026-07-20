from __future__ import annotations

import argparse
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*|\d+(?:\.\d+)?")
CHINESE_DOMAIN_TERMS = (
    "\u65e5\u5fd7\u6761\u76ee",  # 日志条目
    "\u8ffd\u52a0\u6761\u76ee",  # 追加条目
    "\u7ebf\u6027\u4e00\u81f4\u6027",  # 线性一致性
    "\u6267\u884c\u5386\u53f2",  # 执行历史
    "\u6301\u4e45\u5316",  # 持久化
    "\u6613\u5931\u6027",  # 易失性
    "\u5168\u5e8f",  # 全序
    "\u65e5\u5fd7",  # 日志
    "\u8ddf\u968f\u8005",  # 跟随者
    "\u9886\u5bfc\u8005",  # 领导者
    "\u5feb\u7167",  # 快照
    "\u673a\u68b0\u786c\u76d8",  # 机械硬盘
    "\u4ecb\u8d28",  # 介质
)
TERM_ALIASES: dict[str, tuple[str, ...]] = {
    "日志条目": ("日制条目", "日置条目", "日之条目", "日日条目"),
    "日志": ("日制", "日置", "日之", "日日", "日治"),
    "易失性": ("意识性",),
    "全序": ("权序",),
    "a": ("a", "诶", "欸"),
    "b": ("b", "比"),
    "x": ("x", "叉", "艾克斯", "埃克斯"),
    "raft": ("raft", "拉夫特"),
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
    "1": ("1", "一"),
    "2": ("2", "二", "两"),
    "10": ("10", "十"),
}

CHINESE_DIGITS = "零一二三四五六七八九"

SECTION_NUMBER_RE = re.compile(r"第(\d{1,2})\.(\d{1,2})\.(\d{1,2})节")
DIGIT_RE = re.compile(r"\d")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
FILLER_RE = re.compile(r"[嗯哦呃啊]+")

ASR_HOMOPHONE_NORMALIZATIONS: tuple[tuple[str, str], ...] = (
    ("日制条目", "日志条目"),
    ("日置条目", "日志条目"),
    ("日之条目", "日志条目"),
    ("日日条目", "日志条目"),
    ("日制", "日志"),
    ("日置", "日志"),
    ("日之", "日志"),
    ("日日", "日志"),
    ("日治", "日志"),
    ("意识性", "易失性"),
    ("权序", "全序"),
    ("曹魏", "槽位"),
    ("曹卫", "槽位"),
    ("日至", "日志"),
    ("所引", "索引"),
    ("当机", "宕机"),
    ("临近", "邻近"),
    ("直接了当", "直截了当"),
    ("图示三", "图十三"),
)


ASR_HOMOPHONE_NORMALIZATIONS = ASR_HOMOPHONE_NORMALIZATIONS + (
    ("他", "它"),
    ("她", "它"),
)

PUNCT_RE = re.compile(r"[\s,.\!?;:，。！？、；：（）《》“”\"'`~\-\u2013\u2014]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASR-check LoRA inference samples.")
    parser.add_argument("--features", required=True)
    parser.add_argument("--inference-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=os.environ.get("FASTER_WHISPER_MODEL", "large-v3"))
    parser.add_argument("--language", default="zh")
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="auto")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def normalize_section_number(match: re.Match[str]) -> str:
    return (
        "第"
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(1))
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(2))
        + "点"
        + "".join(CHINESE_DIGITS[int(char)] for char in match.group(3))
        + "节"
    )


def chinese_number(value: int) -> str:
    if value < 0 or value > 99:
        return ""
    if value < 10:
        return CHINESE_DIGITS[value]
    tens, ones = divmod(value, 10)
    if value == 10:
        return "十"
    if tens == 1:
        return "十" + (CHINESE_DIGITS[ones] if ones else "")
    return CHINESE_DIGITS[tens] + "十" + (CHINESE_DIGITS[ones] if ones else "")


def normalize_number_token(match: re.Match[str]) -> str:
    raw = match.group(0)
    if "." in raw:
        return "点".join(
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


def normalize(text: str) -> str:
    normalized = text or ""
    normalized = SECTION_NUMBER_RE.sub(normalize_section_number, normalized)
    for source, target in ASR_HOMOPHONE_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    normalized = normalize_digits(normalized)
    normalized = FILLER_RE.sub("", normalized)
    return PUNCT_RE.sub("", normalized).lower()


def similarity(expected: str, actual: str) -> float:
    a = normalize(expected)
    b = normalize(actual)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


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
        aliases.append("".join("两" if char == "2" else CHINESE_DIGITS[int(char)] for char in term))
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
        if len(normalized) == 1 and not (normalized.isdigit() or normalized in {"a", "b", "x", "y"}):
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
    total = len(hard_terms)
    latin_total = len(latin_terms)
    return {
        "all_detected_terms": terms,
        "critical_terms": hard_terms,
        "critical_terms_matched": matched,
        "critical_terms_missing": [term for term in hard_terms if term not in matched],
        "critical_term_recall": (len(matched) / total) if total else None,
        "soft_latin_terms": soft_latin_terms,
        "soft_latin_terms_matched": soft_latin_matched,
        "soft_latin_terms_missing": [term for term in soft_latin_terms if term not in soft_latin_matched],
        "latin_surface_terms_matched": latin_surface,
        "latin_surface_terms_missing": [term for term in latin_terms if term not in latin_surface],
        "latin_surface_recall": (len(latin_surface) / latin_total) if latin_total else None,
    }


def resolve_audio_path(raw_path: str) -> Path:
    text = str(raw_path).replace("\\", "/")
    if text.startswith("/data/"):
        return Path("data") / text[len("/data/") :]
    if text.startswith("data/"):
        return Path(text)

    path = Path(raw_path)
    if path.exists() or path.is_absolute():
        return path
    return Path(text)


def main() -> int:
    args = parse_args()
    from faster_whisper import WhisperModel

    feature_rows = read_jsonl(Path(args.features))
    expected_by_id = {row["sample_id"]: row["text"] for row in feature_rows}
    report = json.loads(Path(args.inference_report).read_text(encoding="utf-8"))

    print(f"[asr] loading {args.model}")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    print("[asr] model loaded")

    rows: list[dict[str, Any]] = []
    for row in report["rows"]:
        path = resolve_audio_path(str(row["path"]))
        expected = expected_by_id[row["sample_id"]]
        print(f"[asr] {row['sample_id']} {row['variant']}")
        segments, info = model.transcribe(
            str(path),
            language=args.language,
            beam_size=args.beam_size,
            vad_filter=False,
        )
        asr_text = "".join(segment.text for segment in segments).strip()
        sim = similarity(expected, asr_text)
        term_metrics = critical_term_metrics(expected, asr_text)
        rows.append(
            {
                **row,
                "asr_text": asr_text,
                "expected_text": expected,
                "similarity": sim,
                **term_metrics,
                "language_probability": getattr(info, "language_probability", None),
            }
        )

    by_variant: dict[str, list[float]] = {}
    term_by_variant: dict[str, list[float]] = {}
    latin_by_variant: dict[str, list[float]] = {}
    for row in rows:
        by_variant.setdefault(row["variant"], []).append(float(row["similarity"]))
        if row.get("critical_term_recall") is not None:
            term_by_variant.setdefault(row["variant"], []).append(float(row["critical_term_recall"]))
        if row.get("latin_surface_recall") is not None:
            latin_by_variant.setdefault(row["variant"], []).append(float(row["latin_surface_recall"]))
    summary = {
        variant: {
            "count": len(values),
            "mean_similarity": sum(values) / max(1, len(values)),
            "min_similarity": min(values) if values else None,
            "mean_critical_term_recall": (
                sum(term_by_variant.get(variant, [])) / len(term_by_variant[variant])
                if term_by_variant.get(variant)
                else None
            ),
            "min_critical_term_recall": min(term_by_variant[variant]) if term_by_variant.get(variant) else None,
            "mean_latin_surface_recall": (
                sum(latin_by_variant.get(variant, [])) / len(latin_by_variant[variant])
                if latin_by_variant.get(variant)
                else None
            ),
            "min_latin_surface_recall": min(latin_by_variant[variant]) if latin_by_variant.get(variant) else None,
        }
        for variant, values in by_variant.items()
    }
    payload = {
        "features": args.features,
        "inference_report": args.inference_report,
        "model": args.model,
        "rows": rows,
        "summary": summary,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
