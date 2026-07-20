from __future__ import annotations

import argparse
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_PUNCTUATION = "。？！.!?"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build repaired LoRA feature rows from a repair queue, mainly text-compression-before-TTS anchors."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--repair-queue", required=True)
    parser.add_argument("--output-features", required=True)
    parser.add_argument("--output-report", required=True)
    parser.add_argument(
        "--sample-ids",
        default="",
        help="Comma-separated source sample ids to repair. Defaults to all text_compression_before_tts entries.",
    )
    parser.add_argument(
        "--variants",
        default="cps52,cps54",
        help="Comma-separated repair variants. Supported forms: cps52, cps54, cps555, heuristic52.",
    )
    parser.add_argument("--min-char-ratio", type=float, default=0.82)
    parser.add_argument("--llm", action="store_true", help="Use configured OpenAI-compatible chat model when available.")
    return parser.parse_args()


def load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def selected_ids(raw: str) -> set[str]:
    return {item.strip() for item in raw.replace("\r", "\n").replace(",", "\n").splitlines() if item.strip()}


def speakable_len(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def variant_cps(variant: str) -> float:
    raw = variant.strip().lower()
    match = re.search(r"cps(\d+)$", raw)
    if match:
        digits = match.group(1)
        if len(digits) == 2:
            return float(digits) / 10.0
        if len(digits) == 3:
            return float(digits) / 100.0
    match = re.search(r"(\d+(?:\.\d+)?)", raw)
    if match:
        value = float(match.group(1))
        return value / 10.0 if value > 10 else value
    raise ValueError(f"cannot infer cps from variant: {variant}")


def variant_method(variant: str, llm_enabled: bool) -> str:
    raw = variant.strip().lower()
    if raw.startswith("heuristic"):
        return "heuristic"
    return "llm" if llm_enabled else "heuristic"


def has_terminal_punctuation(text: str) -> bool:
    return bool(text.strip()) and text.strip()[-1] in TERMINAL_PUNCTUATION


def normalize_tts_punctuation(text: str) -> str:
    out = text.strip()
    out = re.sub(r'"([^"\n]+)"', r"“\1”", out)
    out = out.replace("——", "，")
    out = re.sub(r"，{2,}", "，", out)
    out = re.sub(r"。{2,}", "。", out)
    out = out.replace("，。", "。")
    out = out.replace("。。", "。")
    return out


def ensure_terminal_punctuation(text: str, max_chars: int) -> str:
    stripped = normalize_tts_punctuation(text)
    if not stripped or has_terminal_punctuation(stripped):
        return stripped
    if speakable_len(stripped) + 1 <= max_chars:
        return stripped + "。"
    return stripped


def split_sentences(text: str) -> list[str]:
    pieces = re.split(r"([。？！!?；;])", text.strip())
    rows: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        current += piece
        if re.fullmatch(r"[。？！!?；;]", piece):
            if current.strip():
                rows.append(current.strip())
            current = ""
    if current.strip():
        rows.append(current.strip())
    return rows or ([text.strip()] if text.strip() else [])


def heuristic_rewrite(text: str, max_chars: int, min_chars: int) -> str:
    out = normalize_tts_punctuation(text)
    replacements = [
        ("你知道，", ""),
        ("你知道", ""),
        ("事实上，", "其实，"),
        ("现在。事实是。", "其实，"),
        ("它可能。是应用程序。希望。更倾向于看到。", "应用程序可能更希望看到，"),
        ("当它收到响应时。它更希望看到。一个时间。接近于它实际。收到响应的时间。", "收到响应时的时间戳接近实际响应时间。"),
        ("而不是一个时间。在很久以前。当它最初。发送请求时。", "而不是最初发送请求时的旧时间。"),
        ("如果你。你知道。如果你使用。这样的系统。", "如果使用这种系统，"),
        ("你必须编写应用程序。具有容错性。", "应用程序必须能容忍这种情况。"),
        ("我确实是把“线性一致性”和“强一致性”当作同义词来使用的。", "我把“线性一致性”和“强一致性”当同义词用。"),
        ("对绝大多数分布式系统研究者来说，", "多数分布式系统研究者会认为，"),
        ("这两个术语确实可以互换。", "这两个术语可互换。"),
        ("尽管今天这篇论文或许持不同看法。", "不过今天的论文可能不同意。"),
        ("客户端在发送读取请求时，必须说明，", "客户端发读请求时要说明，"),
        ("我上次发送给主节点的写入请求的ZXID是某个值。", "我上次给主节点的写请求 ZXID 是某个值。"),
        ("而这个副本必须等待，直到它从主节点看到该信息。", "副本要等到从主节点看到该信息。"),
    ]
    for old, new in replacements:
        if speakable_len(out) <= max_chars:
            break
        out = out.replace(old, new)
    clarity_candidates: list[str] = []
    if "应用程序" in out and "实际响应时间" in out and "容错性" in out:
        clarity_candidates.append(
            "也就是说，应用程序收到响应时，可能更希望看到一个接近实际响应时间的时间戳，"
            "而不是最初发送请求时的旧时间。事实是，如果你使用这样的分布式系统，"
            "应用程序就必须对这种时间差具有一定容错性。"
        )
    if clarity_candidates:
        in_budget = [
            candidate
            for candidate in clarity_candidates
            if min_chars <= speakable_len(candidate) <= max_chars
        ]
        if in_budget:
            out = max(in_budget, key=speakable_len)
    if speakable_len(out) <= max_chars:
        return ensure_terminal_punctuation(out, max_chars)

    sentences = split_sentences(out)
    kept: list[str] = []
    for sentence in sentences:
        candidate = "".join(kept + [sentence])
        if speakable_len(candidate) <= max_chars:
            kept.append(sentence)
            continue
        break
    joined = "".join(kept).strip()
    if speakable_len(joined) >= min_chars and has_terminal_punctuation(joined):
        return joined

    chars: list[str] = []
    count = 0
    for char in out:
        if not char.isspace():
            count += 1
        if count > max_chars:
            break
        chars.append(char)
    trimmed = "".join(chars).strip()
    for index in range(len(trimmed) - 1, -1, -1):
        if trimmed[index] in TERMINAL_PUNCTUATION and speakable_len(trimmed[: index + 1]) >= min_chars:
            return trimmed[: index + 1].strip()
    return ensure_terminal_punctuation(trimmed, max_chars)


def budget_distance(chars: int, min_chars: int, max_chars: int) -> int:
    if chars < min_chars:
        return min_chars - chars
    if chars > max_chars:
        return chars - max_chars
    return 0


def choose_repaired_text(
    *,
    llm_text: str | None,
    heuristic_text: str,
    min_chars: int,
    max_chars: int,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if llm_text:
        candidates.append(("llm", ensure_terminal_punctuation(llm_text, max_chars)))
    candidates.append(("heuristic", ensure_terminal_punctuation(heuristic_text, max_chars)))
    for method, text in candidates:
        chars = speakable_len(text)
        if min_chars <= chars <= max_chars:
            return text, method
    best_method, best_text = min(
        candidates,
        key=lambda item: (
            budget_distance(speakable_len(item[1]), min_chars, max_chars),
            1 if speakable_len(item[1]) < min_chars else 0,
        ),
    )
    return best_text, best_method


def llm_rewrite(
    *,
    source_text: str,
    current_text: str,
    target_sec: float,
    min_chars: int,
    max_chars: int,
) -> str | None:
    load_dotenv()
    base_url = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("RETRANSLATION_MODEL") or os.getenv("OPENAI_MODEL", "")
    if not base_url or not api_key or not model:
        return None
    best = ""
    for attempt in range(3):
        retry_hint = ""
        if best:
            retry_hint = (
                f"\n上一版为 {speakable_len(best)} 字，不在 {min_chars}-{max_chars} 字目标内，"
                "或句子不完整。请在不丢事实的前提下重写。\n上一版：\n"
                f"{best}"
            )
        payload = {
            "model": model,
            "temperature": 0.15,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是中文配音译文编辑。只返回 JSON: {\"text\":\"...\"}。"
                        "目标是压缩 TTS 前的中文 spoken_text，让它能在给定时长内自然说完。"
                        "必须保留英文原文中的事实、数字、协议术语、因果关系和结论。"
                        "可以删掉口头填充词和重复表达，可以更凝练地转述。"
                        "不要截断句尾，不要为了字数牺牲关键信息。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"目标时长：{target_sec:.2f} 秒\n"
                        f"中文非空白字符目标：{min_chars}-{max_chars} 字\n"
                        f"英文原文：\n{source_text}\n\n"
                        f"当前中文 spoken_text（{speakable_len(current_text)} 字）：\n{current_text}\n\n"
                        "请输出更短、更自然、适合配音的中文。句末必须完整。"
                        f"{retry_hint}"
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            text = str(parsed.get("text") or "").strip()
        except Exception:
            return None
        if not text:
            continue
        text = ensure_terminal_punctuation(text, max_chars)
        best = text
        if min_chars <= speakable_len(text) <= max_chars and has_terminal_punctuation(text):
            return text
    return best or None


def entries_to_repair(queue: dict[str, Any], wanted: set[str]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in queue.get("entries", [])
        if isinstance(row, dict) and row.get("action") == "text_compression_before_tts"
    ]
    if wanted:
        rows = [row for row in rows if str(row.get("sample_id")) in wanted]
    return rows


def char_budget(entry: dict[str, Any], original_chars: int, cps: float, min_ratio: float) -> tuple[int, int]:
    max_required = float((entry.get("speech_rate") or {}).get("max_required_chars_per_sec") or 0.0)
    if max_required > 0:
        max_chars = max(1, int(math.floor(original_chars * cps / max_required)))
    else:
        target_sec = float(entry.get("target_sec") or 0.0)
        max_chars = max(1, int(math.floor(target_sec * cps)))
    max_chars = min(max_chars, max(1, original_chars - 1))
    min_chars = max(1, min(max_chars, int(math.floor(max_chars * min_ratio))))
    return min_chars, max_chars


def main() -> int:
    args = parse_args()
    features_path = Path(args.features)
    queue_path = Path(args.repair_queue)
    features = {str(row.get("sample_id")): row for row in read_jsonl(features_path) if row.get("sample_id") is not None}
    queue = read_json(queue_path)
    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    if not variants:
        raise ValueError("--variants resolved to empty list")

    repaired_rows: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    for entry in entries_to_repair(queue, selected_ids(args.sample_ids)):
        sample_id = str(entry.get("sample_id"))
        source = features.get(sample_id)
        if source is None:
            raise ValueError(f"repair queue sample not found in features: {sample_id}")
        original = str(source.get("text") or "")
        original_chars = speakable_len(original)
        for variant in variants:
            cps = variant_cps(variant)
            min_chars, max_chars = char_budget(entry, original_chars, cps, float(args.min_char_ratio))
            method = variant_method(variant, bool(args.llm))
            llm_text = None
            if method == "llm":
                llm_text = llm_rewrite(
                    source_text=str(source.get("source_text") or entry.get("source_text") or ""),
                    current_text=original,
                    target_sec=float(entry.get("target_sec") or 0.0),
                    min_chars=min_chars,
                    max_chars=max_chars,
                )
            heuristic_text = heuristic_rewrite(original, max_chars, min_chars)
            repaired_text, method = choose_repaired_text(
                llm_text=llm_text,
                heuristic_text=heuristic_text,
                min_chars=min_chars,
                max_chars=max_chars,
            )
            repaired_chars = speakable_len(repaired_text)
            variant_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", variant.strip().lower()).strip("._")
            new_sample_id = f"{sample_id}_repair_{variant_id}"
            row = {
                **source,
                "sample_id": new_sample_id,
                "source_sample_id": sample_id,
                "display_text": source.get("display_text") or original,
                "text": repaired_text,
                "text_repair": {
                    "schema_version": "holodub.lora_text_repair.v1",
                    "source_sample_id": sample_id,
                    "variant": variant_id,
                    "method": method,
                    "reason": entry.get("reason"),
                    "target_cps": cps,
                    "min_chars": min_chars,
                    "max_chars": max_chars,
                    "original_chars": original_chars,
                    "repaired_chars": repaired_chars,
                    "within_budget": min_chars <= repaired_chars <= max_chars,
                },
            }
            repaired_rows.append(row)
            report_rows.append(
                {
                    "sample_id": new_sample_id,
                    "source_sample_id": sample_id,
                    "variant": variant_id,
                    "method": method,
                    "target_cps": cps,
                    "min_chars": min_chars,
                    "max_chars": max_chars,
                    "original_chars": original_chars,
                    "repaired_chars": repaired_chars,
                    "within_budget": min_chars <= repaired_chars <= max_chars,
                    "source_text": source.get("source_text"),
                    "original_text": original,
                    "repaired_text": repaired_text,
                }
            )

    write_jsonl(Path(args.output_features), repaired_rows)
    report = {
        "schema_version": "holodub.lora_text_repair_features.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": str(features_path),
        "repair_queue": str(queue_path),
        "output_features": args.output_features,
        "policy": {
            "variants": variants,
            "min_char_ratio": args.min_char_ratio,
            "llm_requested": bool(args.llm),
        },
        "summary": {
            "source_samples": len({row["source_sample_id"] for row in report_rows}),
            "repaired_rows": len(report_rows),
            "within_budget": sum(1 for row in report_rows if row["within_budget"]),
        },
        "rows": report_rows,
    }
    write_json(Path(args.output_report), report)
    print(json.dumps({"output_features": args.output_features, "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
