from __future__ import annotations

import math
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SplitPlanOptions:
    min_target_sec: float = 18.0
    min_text_chars: int = 180
    min_chars_per_sec_to_split: float = 0.0
    max_part_sec: float = 16.0
    max_part_chars: int = 170
    min_part_chars: int = 24
    max_parts: int = 4
    pause_sec: float = 0.18
    max_part_chars_per_sec: float = 5.6


def count_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def meaningful_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def should_split(text: str, target_sec: float, options: SplitPlanOptions | None = None) -> bool:
    opts = options or SplitPlanOptions()
    chars = count_chars(text)
    density_eligible = (
        opts.min_chars_per_sec_to_split > 0
        and target_sec > 0
        and (chars / target_sec) >= opts.min_chars_per_sec_to_split
    )
    return target_sec >= opts.min_target_sec or chars >= opts.min_text_chars or density_eligible


def split_with_delimiters(text: str, pattern: re.Pattern[str]) -> list[str]:
    groups: list[str] = []
    current = ""
    for item in pattern.split(text):
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


def split_breath_groups(text: str, max_group_chars: int) -> list[str]:
    groups = split_with_delimiters(text.strip(), HARD_BOUNDARY_RE)
    if not groups and text.strip():
        groups = [text.strip()]
    expanded: list[str] = []
    for group in groups:
        if count_chars(group) <= max_group_chars:
            expanded.append(group)
            continue
        soft = split_with_delimiters(group, SOFT_BOUNDARY_RE)
        if len(soft) <= 1:
            expanded.extend(split_long_run(group, max_group_chars))
            continue
        for item in soft:
            if count_chars(item) <= max_group_chars:
                expanded.append(item)
            else:
                expanded.extend(split_long_run(item, max_group_chars))
    return [group for group in expanded if group.strip()]


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


def choose_part_count(chars: int, sec: float, opts: SplitPlanOptions) -> int:
    by_duration = math.ceil(sec / max(opts.max_part_sec, 0.1))
    by_chars = math.ceil(chars / max(opts.max_part_chars, 1))
    return max(2, min(max(opts.max_parts, 1), max(by_duration, by_chars)))


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


def split_part_once(text: str, opts: SplitPlanOptions) -> list[str]:
    chars = count_chars(text)
    if chars <= 1:
        return [text]
    groups = pack_short_groups(split_breath_groups(text, opts.max_part_chars), opts.min_part_chars)
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


def estimated_part_seconds(parts: list[str], sec: float, opts: SplitPlanOptions) -> list[float]:
    total_chars = sum(count_chars(part) for part in parts)
    if not parts or total_chars <= 0:
        return []
    pause_total = max(0.0, opts.pause_sec) * max(0, len(parts) - 1)
    speech_sec = max(0.05 * len(parts), sec - pause_total)
    return [speech_sec * count_chars(part) / max(total_chars, 1) for part in parts]


def enforce_max_part_seconds(parts: list[str], sec: float, opts: SplitPlanOptions) -> list[str]:
    if len(parts) < 2 or opts.max_part_sec <= 0:
        return parts
    while len(parts) < max(1, opts.max_parts):
        seconds = estimated_part_seconds(parts, sec, opts)
        if not seconds:
            break
        worst_sec = max(seconds)
        if worst_sec <= opts.max_part_sec + 1e-6:
            break
        worst_index = seconds.index(worst_sec)
        split = split_part_once(parts[worst_index], opts)
        if len(split) < 2:
            break
        available_new_parts = max(1, opts.max_parts) - len(parts)
        if len(split) - 1 > available_new_parts:
            kept_count = available_new_parts + 1
            split = [*split[:available_new_parts], "".join(split[available_new_parts:]).strip()]
            if len(split) != kept_count:
                break
        parts = parts[:worst_index] + split + parts[worst_index + 1 :]
    return merge_tiny_islands(parts)


def build_split_plan(text: str, target_sec: float, options: SplitPlanOptions | None = None) -> dict[str, Any]:
    opts = options or SplitPlanOptions()
    chars = count_chars(text)
    groups = split_breath_groups(text, opts.max_part_chars)
    groups = pack_short_groups(groups, opts.min_part_chars)
    part_count = choose_part_count(chars, target_sec, opts)
    parts = pack_to_part_count(groups, part_count)
    if len(parts) < 2 and text.strip():
        parts = split_long_run(text, max(1, math.ceil(chars / 2)))
    parts = enforce_max_part_seconds(parts, target_sec, opts)
    parts = merge_tiny_islands(parts)

    total_chars = sum(count_chars(part) for part in parts)
    pause_total = max(0.0, opts.pause_sec) * max(0, len(parts) - 1)
    speech_sec = max(0.05 * len(parts), target_sec - pause_total)
    assigned_ms = 0
    planned_parts: list[dict[str, Any]] = []
    for index, part in enumerate(parts, start=1):
        part_chars = count_chars(part)
        if index == len(parts):
            part_ms = max(50, round(speech_sec * 1000.0) - assigned_ms)
        else:
            part_ms = max(
                50,
                round(speech_sec * 1000.0 * part_chars / max(total_chars, 1)),
            )
            assigned_ms += part_ms
        part_sec = part_ms / 1000.0
        required_chars_per_sec = (part_chars / part_sec) if part_sec > 0 else 0.0
        speech_rate_risk = required_chars_per_sec > opts.max_part_chars_per_sec
        planned_parts.append(
            {
                "index": index,
                "target_sec": part_sec,
                "target_ms": part_ms,
                "chars": part_chars,
                "required_chars_per_sec": round(required_chars_per_sec, 6),
                "max_part_chars_per_sec": opts.max_part_chars_per_sec,
                "speech_rate_risk": speech_rate_risk,
                "speech_rate_risk_reason": (
                    "required_chars_per_sec_above_split_safe_limit" if speech_rate_risk else ""
                ),
                "text": part,
            }
        )
    speech_rate_risk_parts = [
        part["index"]
        for part in planned_parts
        if bool(part.get("speech_rate_risk"))
    ]
    return {
        "target_sec": target_sec,
        "text_chars": chars,
        "part_count": len(planned_parts),
        "pause_sec": opts.pause_sec,
        "min_chars_per_sec_to_split": opts.min_chars_per_sec_to_split,
        "max_required_chars_per_sec": max(
            (float(part.get("required_chars_per_sec") or 0.0) for part in planned_parts),
            default=0.0,
        ),
        "max_part_chars_per_sec": opts.max_part_chars_per_sec,
        "speech_rate_risk_parts": speech_rate_risk_parts,
        "needs_text_compression": bool(speech_rate_risk_parts),
        "strategy": "automatic_clause_duration_plan",
        "parts": planned_parts,
    }
