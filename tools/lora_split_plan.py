from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


HARD_BOUNDARY_RE = re.compile(r"([.!?;:\n\u3002\uff01\uff1f\uff1b\uff1a])")
SOFT_BOUNDARY_RE = re.compile(r"([,\u3001\uff0c])")
SPLIT_PROTECTED_TERMS = (
    "\u65e5\u5fd7\u6761\u76ee",
    "\u8ffd\u52a0\u6761\u76ee",
    "\u7ebf\u6027\u4e00\u81f4\u6027",
    "\u6267\u884c\u5386\u53f2",
    "\u6301\u4e45\u5316",
    "\u6613\u5931\u6027",
    "\u5168\u5e8f",
    "\u8ddf\u968f\u8005",
    "\u9886\u5bfc\u8005",
    "\u673a\u68b0\u786c\u76d8",
    "\u65e5\u5fd7",
    "append entries",
    "appendentries",
    "raft",
    "rpc",
    "fsync",
)
PREFERRED_CUT_AFTER = set(",;:\u3001\uff0c\uff1b\uff1a\u3002\uff01\uff1f.!?")
PREFERRED_CUT_BEFORE = (
    "\u56e0\u6b64",
    "\u56e0\u4e3a",
    "\u6240\u4ee5",
    "\u4f46\u662f",
    "\u4e0d\u8fc7",
    "\u7136\u540e",
    "\u968f\u540e",
    "\u63a5\u7740",
    "\u90a3\u4e48",
)
WEAK_CUT_AFTER = set("\u7684\u5730\u5f97\u7b2c\u524d\u540e")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create split-synthesis feature rows for high-risk LoRA TTS samples."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--output-features", required=True)
    parser.add_argument("--output-plan", required=True)
    parser.add_argument(
        "--sample-ids",
        default="",
        help="Comma-separated sample IDs. When set, matching samples are forced through split planning.",
    )
    parser.add_argument("--min-target-sec", type=float, default=18.0)
    parser.add_argument("--min-text-chars", type=int, default=180)
    parser.add_argument("--max-part-sec", type=float, default=16.0)
    parser.add_argument("--max-part-chars", type=int, default=150)
    parser.add_argument("--min-part-chars", type=int, default=24)
    parser.add_argument("--max-parts", type=int, default=6)
    parser.add_argument("--pause-sec", type=float, default=0.18)
    parser.add_argument(
        "--min-chars-per-sec-to-split",
        type=float,
        default=0.0,
        help=(
            "Also split samples whose target Chinese-character rate meets this "
            "threshold, even when they are below the duration/text-length trigger. "
            "0 disables the density trigger."
        ),
    )
    parser.add_argument(
        "--max-part-chars-per-sec",
        type=float,
        default=5.6,
        help="Flag split parts whose required Chinese-character rate is above this threshold.",
    )
    parser.add_argument(
        "--force-break-after-regex",
        action="append",
        default=[],
        help=(
            "Regex for a breath group that must end a split part. Use this for "
            "known pause-intent boundaries, for example a number list followed "
            "by a new sentence."
        ),
    )
    parser.add_argument("--duration-token-rate-policy", choices=("constant", "piecewise"), default="piecewise")
    parser.add_argument("--duration-token-rate", type=float, default=23.4375)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def target_sec(row: dict[str, Any]) -> float:
    value = float(row.get("target_ms") or 0.0) / 1000.0
    if value <= 0:
        value = float(row.get("audio_ms") or 0.0) / 1000.0
    return value


def count_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def meaningful_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def split_with_delimiters(text: str, pattern: re.Pattern[str]) -> list[str]:
    parts = pattern.split(text)
    groups: list[str] = []
    current = ""
    for item in parts:
        if not item:
            continue
        current += item
        if pattern.fullmatch(item):
            stripped = current.strip()
            if stripped:
                groups.append(stripped)
            current = ""
    stripped = current.strip()
    if stripped:
        groups.append(stripped)
    return groups


def split_breath_groups(text: str, max_group_chars: int) -> list[str]:
    groups = split_with_delimiters(text.strip(), HARD_BOUNDARY_RE)
    if not groups:
        groups = [text.strip()] if text.strip() else []

    expanded: list[str] = []
    for group in groups:
        if count_chars(group) <= max_group_chars:
            expanded.append(group)
            continue
        soft = split_with_delimiters(group, SOFT_BOUNDARY_RE)
        if len(soft) <= 1:
            expanded.extend(split_long_run(group, max_group_chars))
        else:
            for item in soft:
                if count_chars(item) <= max_group_chars:
                    expanded.append(item)
                else:
                    expanded.extend(split_long_run(item, max_group_chars))
    return [group for group in expanded if group.strip()]


def split_long_run(text: str, max_chars: int) -> list[str]:
    stripped = text.strip()
    if max_chars <= 0 or len(stripped) <= max_chars:
        return [stripped] if stripped else []
    chunks: list[str] = []
    remaining = stripped
    while len(remaining) > max_chars:
        cut = choose_safe_split_index(remaining, max_chars)
        left = remaining[:cut].strip()
        right = remaining[cut:].strip()
        if not left or not right:
            break
        chunks.append(left)
        remaining = right
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks or ([stripped] if stripped else [])


def protected_spans(text: str) -> list[tuple[int, int]]:
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for term in SPLIT_PROTECTED_TERMS:
        needle = term.lower()
        start = 0
        while needle:
            index = lowered.find(needle, start)
            if index < 0:
                break
            spans.append((index, index + len(needle)))
            start = index + 1
    return spans


def is_safe_split_index(text: str, cut: int) -> bool:
    if cut <= 0 or cut >= len(text):
        return False
    return not any(start < cut < end for start, end in protected_spans(text))


def split_index_score(text: str, cut: int, desired: int) -> float:
    left = text[:cut]
    right = text[cut:]
    score = float(abs(cut - desired))
    if left and left[-1] in PREFERRED_CUT_AFTER:
        score -= 10.0
    if any(right.startswith(prefix) for prefix in PREFERRED_CUT_BEFORE):
        score -= 6.0
    if left and left[-1] in WEAK_CUT_AFTER:
        score += 4.0
    if right and right[0] in PREFERRED_CUT_AFTER:
        score += 8.0
    return score


def choose_safe_split_index(text: str, desired: int) -> int:
    if len(text) <= 1:
        return len(text)
    desired = max(1, min(desired, len(text) - 1))
    window = max(8, min(30, math.ceil(len(text) * 0.25)))
    min_side = max(1, min(8, len(text) // 5))
    lower = max(min_side, desired - window)
    upper = min(len(text) - min_side, desired + window)
    candidates = [
        cut
        for cut in range(lower, upper + 1)
        if is_safe_split_index(text, cut)
    ]
    if not candidates:
        candidates = [
            cut
            for cut in range(1, len(text))
            if is_safe_split_index(text, cut)
        ]
    if not candidates:
        return desired
    return min(candidates, key=lambda cut: split_index_score(text, cut, desired))


def pack_short_groups(groups: list[str], min_chars: int) -> list[str]:
    if len(groups) <= 1 or min_chars <= 0:
        return groups
    packed: list[str] = []
    current: list[str] = []
    current_chars = 0
    for group in groups:
        current.append(group)
        current_chars += count_chars(group)
        if current_chars >= min_chars:
            packed.append("".join(current).strip())
            current = []
            current_chars = 0
    if current:
        if packed and current_chars < min_chars:
            packed[-1] = (packed[-1] + "".join(current)).strip()
        else:
            packed.append("".join(current).strip())
    return packed


def choose_part_count(chars: int, sec: float, args: argparse.Namespace) -> int:
    by_duration = math.ceil(sec / max(args.max_part_sec, 0.1))
    by_chars = math.ceil(chars / max(args.max_part_chars, 1))
    return max(2, min(max(args.max_parts, 1), max(by_duration, by_chars)))


def pack_to_part_count(groups: list[str], part_count: int) -> list[str]:
    if len(groups) <= part_count:
        return groups
    total = sum(count_chars(group) for group in groups)
    target = max(1, math.ceil(total / part_count))
    packed: list[str] = []
    current: list[str] = []
    current_chars = 0
    for group in groups:
        if current and current_chars >= target and len(packed) < part_count - 1:
            packed.append("".join(current).strip())
            current = []
            current_chars = 0
        current.append(group)
        current_chars += count_chars(group)
    if current:
        packed.append("".join(current).strip())
    return packed


def force_break_patterns(args: argparse.Namespace) -> list[re.Pattern[str]]:
    return [re.compile(pattern) for pattern in getattr(args, "force_break_after_regex", []) or [] if pattern]


def ends_forced_block(group: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(group) for pattern in patterns)


def force_break_blocks(groups: list[str], patterns: list[re.Pattern[str]]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        current.append(group)
        if ends_forced_block(group, patterns):
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return [block for block in blocks if block]


def pack_with_forced_boundaries(groups: list[str], args: argparse.Namespace) -> list[str]:
    patterns = force_break_patterns(args)
    if not patterns:
        groups = pack_short_groups(groups, args.min_part_chars)
        return pack_to_part_count(groups, choose_part_count(sum(count_chars(group) for group in groups), 0.0, args))

    parts: list[str] = []
    blocks = force_break_blocks(groups, patterns)
    for block in blocks:
        packed = pack_short_groups(block, args.min_part_chars)
        block_chars = sum(count_chars(group) for group in packed)
        block_part_count = max(1, math.ceil(block_chars / max(args.max_part_chars, 1)))
        parts.extend(pack_to_part_count(packed, block_part_count))
    return parts


def estimated_part_seconds(parts: list[str], sec: float, pause_sec: float) -> list[float]:
    total_chars = sum(count_chars(part) for part in parts)
    if not parts or total_chars <= 0:
        return []
    pause_total = max(0.0, pause_sec) * max(0, len(parts) - 1)
    speech_sec = max(0.05 * len(parts), sec - pause_total)
    return [speech_sec * count_chars(part) / max(total_chars, 1) for part in parts]


def split_part_once(text: str, args: argparse.Namespace) -> list[str]:
    chars = count_chars(text)
    if chars <= 1:
        return [text]
    groups = split_breath_groups(text, args.max_part_chars)
    groups = pack_short_groups(groups, args.min_part_chars)
    if len(groups) > 1:
        split = pack_to_part_count(groups, 2)
        if len(split) > 1:
            return split
    return split_long_run(text, max(1, math.ceil(chars / 2)))


def merge_tiny_islands(parts: list[str]) -> list[str]:
    merged = [part for part in parts if part.strip()]
    index = 0
    while len(merged) > 1 and index < len(merged):
        part = merged[index]
        if meaningful_chars(part) > 0 and count_chars(part) > 3:
            index += 1
            continue
        if index > 0:
            merged[index - 1] = (merged[index - 1] + part).strip()
            del merged[index]
        else:
            merged[1] = (part + merged[1]).strip()
            del merged[0]
    return merged


def enforce_max_part_seconds(parts: list[str], sec: float, args: argparse.Namespace) -> list[str]:
    if len(parts) < 2 or args.max_part_sec <= 0:
        return parts
    max_parts = max(1, int(args.max_parts))
    while len(parts) < max_parts:
        seconds = estimated_part_seconds(parts, sec, args.pause_sec)
        if not seconds:
            break
        worst_sec = max(seconds)
        if worst_sec <= args.max_part_sec + 1e-6:
            break
        worst_index = seconds.index(worst_sec)
        split = split_part_once(parts[worst_index], args)
        if len(split) < 2:
            break
        available_new_parts = max_parts - len(parts)
        if len(split) - 1 > available_new_parts:
            split = split[: available_new_parts + 1]
        parts = parts[:worst_index] + split + parts[worst_index + 1 :]
    return merge_tiny_islands(parts)


def duration_token_rate(target: float, args: argparse.Namespace) -> float:
    if args.duration_token_rate_policy == "piecewise":
        if target < 15.0:
            return 23.4375
        if target < 23.0:
            return 30.0
        return 32.5
    return float(args.duration_token_rate)


def max_part_chars_per_sec(args: argparse.Namespace) -> float:
    return float(getattr(args, "max_part_chars_per_sec", 5.6))


def min_chars_per_sec_to_split(args: argparse.Namespace) -> float:
    return max(0.0, float(getattr(args, "min_chars_per_sec_to_split", 0.0) or 0.0))


def planned_parts(row: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    text = str(row.get("text") or "").strip()
    sec = target_sec(row)
    chars = count_chars(text)
    groups = split_breath_groups(text, args.max_part_chars)
    if force_break_patterns(args):
        parts = pack_with_forced_boundaries(groups, args)
    else:
        groups = pack_short_groups(groups, args.min_part_chars)
        part_count = choose_part_count(chars, sec, args)
        parts = pack_to_part_count(groups, part_count)
    if len(parts) < 2 and text:
        parts = split_long_run(text, max(1, math.ceil(chars / 2)))
    parts = enforce_max_part_seconds(parts, sec, args)
    parts = merge_tiny_islands(parts)

    total_part_chars = sum(count_chars(part) for part in parts)
    pause_total = max(0.0, args.pause_sec) * max(0, len(parts) - 1)
    speech_sec = max(0.05 * len(parts), sec - pause_total)
    max_safe_cps = max_part_chars_per_sec(args)

    out: list[dict[str, Any]] = []
    assigned_ms = 0
    for index, part in enumerate(parts, start=1):
        part_chars = count_chars(part)
        if index == len(parts):
            part_ms = max(50, int(round(speech_sec * 1000.0)) - assigned_ms)
        else:
            part_ms = max(50, int(round(speech_sec * 1000.0 * part_chars / max(total_part_chars, 1))))
            assigned_ms += part_ms
        part_sec = part_ms / 1000.0
        required_chars_per_sec = (part_chars / part_sec) if part_sec > 0 else 0.0
        speech_rate_risk = required_chars_per_sec > max_safe_cps
        rate = duration_token_rate(part_sec, args)
        tokens = max(1, int(round(part_sec * rate)))
        sample_id = f"{row['sample_id']}_p{index}"
        planned = {
            **row,
            "sample_id": sample_id,
            "source_sample_id": row["sample_id"],
            "split_part_index": index,
            "split_part_count": len(parts),
            "split_pause_sec": args.pause_sec,
            "split_strategy": "automatic_clause_duration_plan",
            "text": part,
            "text_len": len(part),
            "target_ms": part_ms,
            "audio_ms": part_ms,
            "required_chars_per_sec": round(required_chars_per_sec, 6),
            "max_part_chars_per_sec": max_safe_cps,
            "speech_rate_risk": speech_rate_risk,
            "speech_rate_risk_reason": "required_chars_per_sec_above_split_safe_limit" if speech_rate_risk else "",
            "duration_token_rate": rate,
            "duration_tokens_from_target": tokens,
            "duration_tokens_from_audio": tokens,
            "code_len": tokens,
        }
        out.append(planned)
    return out


def should_plan(row: dict[str, Any], forced_ids: set[str], args: argparse.Namespace) -> bool:
    if row.get("sample_id") in forced_ids:
        return True
    sec = target_sec(row)
    chars = count_chars(str(row.get("text") or ""))
    min_cps = min_chars_per_sec_to_split(args)
    density_eligible = min_cps > 0 and sec > 0 and (chars / sec) >= min_cps
    return sec >= args.min_target_sec or chars >= args.min_text_chars or density_eligible


def main() -> int:
    args = parse_args()
    rows = read_jsonl(Path(args.features))
    forced_ids = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
    if forced_ids:
        rows = [row for row in rows if row.get("sample_id") in forced_ids]

    feature_rows: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if not should_plan(row, forced_ids, args):
            skipped.append({"sample_id": row.get("sample_id"), "reason": "below_split_threshold"})
            continue
        parts = planned_parts(row, args)
        if len(parts) < 2:
            skipped.append({"sample_id": row.get("sample_id"), "reason": "could_not_split"})
            continue
        feature_rows.extend(parts)
        planned_summary = {
            "speech_rate_risk_parts": [
                part["sample_id"]
                for part in parts
                if bool(part.get("speech_rate_risk"))
            ],
            "max_required_chars_per_sec": max(
                (float(part.get("required_chars_per_sec") or 0.0) for part in parts),
                default=0.0,
            ),
        }
        plan_rows.append(
            {
                **planned_summary,
                "sample_id": row["sample_id"],
                "target_sec": target_sec(row),
                "text": row.get("text"),
                "part_count": len(parts),
                "pause_sec": args.pause_sec,
                "needs_text_compression": bool(planned_summary["speech_rate_risk_parts"]),
                "parts": [
                    {
                        "sample_id": part["sample_id"],
                        "index": part["split_part_index"],
                        "target_sec": float(part["target_ms"]) / 1000.0,
                        "chars": count_chars(str(part.get("text") or "")),
                        "required_chars_per_sec": part.get("required_chars_per_sec"),
                        "speech_rate_risk": part.get("speech_rate_risk"),
                        "speech_rate_risk_reason": part.get("speech_rate_risk_reason"),
                        "text": part.get("text"),
                    }
                    for part in parts
                ],
            }
        )

    write_jsonl(Path(args.output_features), feature_rows)
    payload = {
        "features": args.features,
        "output_features": args.output_features,
        "policy": {
            "min_target_sec": args.min_target_sec,
            "min_text_chars": args.min_text_chars,
            "max_part_sec": args.max_part_sec,
            "max_part_chars": args.max_part_chars,
            "min_part_chars": args.min_part_chars,
            "max_parts": args.max_parts,
            "pause_sec": args.pause_sec,
            "max_part_chars_per_sec": max_part_chars_per_sec(args),
            "force_break_after_regex": args.force_break_after_regex,
            "duration_token_rate_policy": args.duration_token_rate_policy,
            "duration_token_rate": args.duration_token_rate,
        },
        "planned": plan_rows,
        "skipped": skipped,
    }
    output_plan = Path(args.output_plan)
    output_plan.parent.mkdir(parents=True, exist_ok=True)
    output_plan.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"planned": len(plan_rows), "parts": len(feature_rows), "skipped": len(skipped)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
