from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


FIGURE_DIGIT_RE = re.compile(r"(?<![A-Za-z0-9])([\u56fe\u8868])(\d{1,2})(?![A-Za-z0-9])")
TERM_ORDINAL_DIGIT_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(\u53f7(?=\u4efb\u671f))")
ITEM_ORDINAL_DIGIT_RE = re.compile(r"(?:(?<=\u7b2c)|(?<=[\u3001\uff0c\u548c\u53ca\u4e0e\u6216]))(\d{1,2})(?=[\u3001\uff0c\u548c\u53ca\u4e0e\u6216\u9879])")
RENQI_HAO_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2}|[\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})\u53f7\u4efb\u671f")
SECTION_NUMBER_RE = re.compile(r"\u7b2c?(\d+(?:\.\d+)+)\u8282")
NUMBER_LIST_BEFORE_NODE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})\u3001(\d{1,2})\u3002(?=[\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\d]{1,3}\u53f7\u8282\u70b9)")
FIGURE_DASH_RE = re.compile(r"([\u56fe\u8868][\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\d]{1,3})[\u2014\u2013-]+")
PREVIOUS_LOG_FIELD_RE = re.compile(r"\u524d\u4e00\u65e5\u5fd7(\u7d22\u5f15|\u4efb\u671f)\u5b57\u6bb5")
DURATION_OPERATION_LONGER_RE = re.compile(r"(\u64cd\u4f5c)\u90fd\u8981\u957f\u5f97\u591a")
TECH_LATIN_CJK_SUFFIX_RE = re.compile(r"(?<![A-Za-z])(Raft|RAFT|raft|Append Entries RPC|Append Entries|RPC)(?=[\u4e00-\u9fff])")


def chinese_number(value: int) -> str:
    digits = "\u96f6\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d"
    if value < 0 or value > 99:
        raise ValueError(f"value out of range: {value}")
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    if value == 10:
        return "\u5341"
    if tens == 1:
        return "\u5341" + (digits[ones] if ones else "")
    return digits[tens] + "\u5341" + (digits[ones] if ones else "")


def chinese_number_token(token: str) -> str:
    if token.isdigit():
        return chinese_number(int(token))
    return token


def normalize_spoken_text(text: str, policy: str = "figure-digits") -> tuple[str, list[dict[str, Any]]]:
    if policy in {"", "none"}:
        return text, []
    if policy not in {"figure-digits", "technical-v2", "technical-v3"}:
        raise ValueError(f"unknown spoken text policy: {policy}")

    rewrites: list[dict[str, Any]] = []

    def record_rewrite(rule: str, original: str, replacement: str, match: re.Match[str]) -> None:
        if replacement == original:
            return
        rewrites.append(
            {
                "rule": rule,
                "original": original,
                "replacement": replacement,
                "start_rune": match.start(),
                "end_rune": match.end(),
            }
        )

    def replace_figure(match: re.Match[str]) -> str:
        original = match.group(0)
        value = int(match.group(2))
        replacement = match.group(1) + chinese_number(value)
        record_rewrite("figure_digit_to_chinese_numeral", original, replacement, match)
        return replacement

    def replace_term_ordinal(match: re.Match[str]) -> str:
        original = match.group(0)
        value = int(match.group(1))
        replacement = chinese_number(value) + " " + match.group(2)
        record_rewrite("ordinal_digit_hao_to_spaced_hao", original, replacement, match)
        return replacement

    def replace_item_ordinal(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = chinese_number(int(match.group(1)))
        record_rewrite("item_ordinal_digit_to_chinese_numeral", original, replacement, match)
        return replacement

    def replace_renqi(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = chinese_number_token(match.group(1)) + " \u53f7\u4efb\u671f"
        record_rewrite("renqi_hao_to_spaced_hao_term", original, replacement, match)
        return replacement

    def replace_section(match: re.Match[str]) -> str:
        original = match.group(0)
        parts = [chinese_number(int(part)) for part in match.group(1).split(".")]
        replacement = "\u7b2c" + "\u70b9".join(parts) + "\u8282"
        record_rewrite("section_number_to_spoken_points", original, replacement, match)
        return replacement

    def replace_number_list_before_node(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = chinese_number(int(match.group(1))) + "\u548c" + chinese_number(int(match.group(2))) + "\u3002\u63a5\u7740\uff0c"
        record_rewrite("number_list_sentence_pause_before_node", original, replacement, match)
        return replacement

    def replace_number_list_before_node_v3(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = chinese_number(int(match.group(1))) + "\u548c" + chinese_number(int(match.group(2))) + "\u3002\u968f\u540e\uff0c"
        record_rewrite("number_list_sentence_pause_before_node_v3", original, replacement, match)
        return replacement

    def replace_figure_dash(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = match.group(1) + "\uff0c"
        record_rewrite("figure_dash_to_comma_pause", original, replacement, match)
        return replacement

    def replace_previous_log_field_v3(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = "\u524d\u4e00\u6761\u65e5\u5fd7\u7684" + match.group(1) + "\u5b57\u6bb5"
        record_rewrite("previous_log_field_to_clause_v3", original, replacement, match)
        return replacement

    def replace_duration_operation_longer_v3(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = match.group(1) + "\u8017\u65f6\u90fd\u8981\u66f4\u4e45"
        record_rewrite("duration_polyphone_chang_to_haoshi_gengjiu_v3", original, replacement, match)
        return replacement

    def replace_technical_latin_cjk_suffix(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = original + " "
        record_rewrite("technical_latin_cjk_suffix_spacing_v3", original, replacement, match)
        return replacement

    spoken = FIGURE_DIGIT_RE.sub(replace_figure, text or "")
    if policy == "figure-digits":
        spoken = TERM_ORDINAL_DIGIT_RE.sub(replace_term_ordinal, spoken)
        spoken = ITEM_ORDINAL_DIGIT_RE.sub(replace_item_ordinal, spoken)
        return spoken, rewrites

    spoken = RENQI_HAO_RE.sub(replace_renqi, spoken)
    spoken = SECTION_NUMBER_RE.sub(replace_section, spoken)
    if policy == "technical-v3":
        spoken = NUMBER_LIST_BEFORE_NODE_RE.sub(replace_number_list_before_node_v3, spoken)
    else:
        spoken = NUMBER_LIST_BEFORE_NODE_RE.sub(replace_number_list_before_node, spoken)
    spoken = FIGURE_DASH_RE.sub(replace_figure_dash, spoken)
    if policy == "technical-v3":
        spoken = PREVIOUS_LOG_FIELD_RE.sub(replace_previous_log_field_v3, spoken)
        spoken = DURATION_OPERATION_LONGER_RE.sub(replace_duration_operation_longer_v3, spoken)
        spoken = TECH_LATIN_CJK_SUFFIX_RE.sub(replace_technical_latin_cjk_suffix, spoken)
        return spoken, rewrites

    spoken = spoken.replace("\u957f\u5f97\u591a", "\u4e45\u5f97\u591a")
    if "\u4e45\u5f97\u591a" in spoken and "\u957f\u5f97\u591a" in (text or ""):
        rewrites.append(
            {
                "rule": "polyphone_chang_de_duo_to_jiu_de_duo",
                "original": "\u957f\u5f97\u591a",
                "replacement": "\u4e45\u5f97\u591a",
                "start_rune": (text or "").find("\u957f\u5f97\u591a"),
                "end_rune": (text or "").find("\u957f\u5f97\u591a") + len("\u957f\u5f97\u591a"),
            }
        )
    spoken = spoken.replace("\u524d\u4e00\u65e5\u5fd7", "\u524d\u4e00\u4e2a\u65e5\u5fd7")
    if "\u524d\u4e00\u4e2a\u65e5\u5fd7" in spoken and "\u524d\u4e00\u65e5\u5fd7" in (text or ""):
        rewrites.append(
            {
                "rule": "awkward_log_compound_insert_ge",
                "original": "\u524d\u4e00\u65e5\u5fd7",
                "replacement": "\u524d\u4e00\u4e2a\u65e5\u5fd7",
                "start_rune": (text or "").find("\u524d\u4e00\u65e5\u5fd7"),
                "end_rune": (text or "").find("\u524d\u4e00\u65e5\u5fd7") + len("\u524d\u4e00\u65e5\u5fd7"),
            }
        )
    return spoken, rewrites


def apply_spoken_text_rows(
    rows: list[dict[str, Any]],
    policy: str = "figure-digits",
    target_sample_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        sample_id = str(row.get("sample_id") or "")
        if target_sample_ids is not None and sample_id not in target_sample_ids:
            output.append(row)
            continue
        text = str(row.get("text") or "")
        spoken, rewrites = normalize_spoken_text(text, policy)
        if rewrites:
            row = {
                **row,
                "display_text": row.get("display_text") or text,
                "text": spoken,
                "spoken_text_policy": policy,
                "spoken_text_rewrites": rewrites,
            }
        output.append(row)
    return output


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply deterministic spoken-text rewrites to LoRA feature JSONL.")
    parser.add_argument("--features", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", choices=("none", "figure-digits", "technical-v2", "technical-v3"), default="figure-digits")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = apply_spoken_text_rows(read_jsonl(Path(args.features)), args.policy)
    write_jsonl(Path(args.output), rows)
    rewrite_count = sum(len(row.get("spoken_text_rewrites") or []) for row in rows)
    sample_count = sum(1 for row in rows if row.get("spoken_text_rewrites"))
    print(
        json.dumps(
            {
                "output": args.output,
                "policy": args.policy,
                "rows": len(rows),
                "rewrite_count": rewrite_count,
                "rewritten_samples": sample_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
