from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml"]
DEFAULT_SAMPLE_IDS = ["seg10511", "seg10518", "seg10686"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a real HoloDub job, seed LoRA canary segments, run tts_duration, and summarize candidate provenance."
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument(
        "--feature-jsonl",
        default="data/training/lora_features/pilot-episode146-code1500/train.features.jsonl",
    )
    parser.add_argument(
        "--sample-ids",
        default=",".join(DEFAULT_SAMPLE_IDS),
        help="Comma-separated feature sample ids to run. Defaults cover short + long candidate paths.",
    )
    parser.add_argument("--output", default="data/training/lora_runs/main_flow_candidate_smoke/go_pipeline_smoke_summary.json")
    parser.add_argument("--job-name", default="")
    parser.add_argument("--input-relpath", default="training/lora_runs/main_flow_candidate_smoke/smoke_input.mp4")
    parser.add_argument("--source-language", default="en")
    parser.add_argument("--target-language", default="zh-CN")
    parser.add_argument("--tenant-key", default="default")
    parser.add_argument("--gap-ms", type=int, default=500)
    parser.add_argument("--min-target-sec", type=float, default=18.0)
    parser.add_argument("--min-text-chars", type=int, default=180)
    parser.add_argument("--max-candidate-drift-ms", type=float, default=500.0)
    parser.add_argument("--timeout-sec", type=float, default=1800.0)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--expect-split-mix", default="seg10686")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Docker compose file. Repeatable. Defaults to docker-compose.yml + docker-compose.ml-app.yml.",
    )
    return parser.parse_args()


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def read_feature_rows(path: Path, sample_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(sample_ids)
    rows_by_id: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            sample_id = str(row.get("sample_id") or "")
            if sample_id in wanted and sample_id not in rows_by_id:
                rows_by_id[sample_id] = row
    missing = [sample_id for sample_id in sample_ids if sample_id not in rows_by_id]
    if missing:
        raise ValueError(f"feature samples not found in {path}: {', '.join(missing)}")
    return [rows_by_id[sample_id] for sample_id in sample_ids]


def request_json(
    method: str,
    api_base: str,
    path: str,
    payload: dict[str, Any] | None = None,
    api_key: str = "",
    timeout: float = 60.0,
) -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if not raw:
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON")
    return decoded


def create_job(args: argparse.Namespace) -> dict[str, Any]:
    name = args.job_name or f"LoRA candidate Go pipeline smoke {int(time.time())}"
    payload = {
        "name": name,
        "tenant_key": args.tenant_key,
        "input_relpath": args.input_relpath,
        "source_language": args.source_language,
        "target_language": args.target_language,
        "max_retries": 0,
        "auto_start": False,
        "config": {
            "lora_candidate_smoke": True,
            "feature_jsonl": args.feature_jsonl,
            "sample_ids": split_csv(args.sample_ids),
        },
    }
    return request_json("POST", args.api_base, "/jobs", payload, args.api_key)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def jsonb_literal(value: dict[str, Any]) -> str:
    return sql_literal(json.dumps(value, ensure_ascii=False, sort_keys=True)) + "::jsonb"


def build_insert_segment_sql(
    row: dict[str, Any],
    *,
    job_id: int,
    ordinal: int,
    start_ms: int,
    target_ms: int,
) -> str:
    sample_id = str(row.get("sample_id") or f"sample_{ordinal}")
    source_text = str(row.get("source_text") or "")
    target_text = str(row.get("text") or row.get("target_text") or "")
    meta = {
        "smoke_sample_id": sample_id,
        "smoke_feature_segment_id": row.get("segment_id"),
        "smoke_feature_job_id": row.get("job_id"),
        "smoke_feature_audio_relpath": row.get("audio_relpath"),
    }
    end_ms = start_ms + target_ms
    return (
        "INSERT INTO segments "
        "(job_id, speaker_label, ordinal, start_ms, end_ms, original_duration_ms, "
        "source_text, target_text, status, meta, created_at, updated_at) VALUES "
        f"({job_id}, 'SPEAKER_00', {ordinal}, {start_ms}, {end_ms}, {target_ms}, "
        f"{sql_literal(source_text)}, {sql_literal(target_text)}, 'translated', {jsonb_literal(meta)}, NOW(), NOW()) "
        "RETURNING id;"
    )


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    command = ["docker", "compose"]
    for file in files:
        command.extend(["-f", file])
    command.extend(extra)
    return command


def psql_scalar(args: argparse.Namespace, sql: str) -> str:
    command = compose_command(
        args,
        [
            "exec",
            "-T",
            args.postgres_service,
            "psql",
            "-U",
            args.postgres_user,
            "-d",
            args.postgres_db,
            "-X",
            "-A",
            "-t",
            "-c",
            sql,
        ],
    )
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


def parse_returning_id(psql_output: str) -> int:
    for line in psql_output.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    raise ValueError(f"could not parse RETURNING id from psql output: {psql_output!r}")


def insert_segments(args: argparse.Namespace, job_id: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    cursor_ms = 0
    for idx, row in enumerate(rows, start=1):
        target_ms = int(row.get("target_ms") or row.get("audio_ms") or 0)
        if target_ms <= 0:
            raise ValueError(f"sample {row.get('sample_id')!r} has no positive target_ms/audio_ms")
        sql = build_insert_segment_sql(row, job_id=job_id, ordinal=idx, start_ms=cursor_ms, target_ms=target_ms)
        segment_id = parse_returning_id(psql_scalar(args, sql))
        inserted.append(
            {
                "sample_id": row.get("sample_id"),
                "segment_id": segment_id,
                "ordinal": idx,
                "target_ms": target_ms,
                "text": row.get("text"),
                "source_text": row.get("source_text"),
            }
        )
        cursor_ms += target_ms + max(0, int(args.gap_ms))
    return inserted


def enqueue_tts(args: argparse.Namespace, job_id: int, segment_ids: list[int]) -> dict[str, Any]:
    return request_json(
        "POST",
        args.api_base,
        f"/jobs/{job_id}/retry",
        {"stage": "tts_duration", "segment_ids": segment_ids},
        args.api_key,
    )


def fetch_segments(args: argparse.Namespace, job_id: int) -> list[dict[str, Any]]:
    payload = request_json("GET", args.api_base, f"/jobs/{job_id}/segments", api_key=args.api_key)
    rows = payload.get("segments") or []
    return [row for row in rows if isinstance(row, dict)]


def wait_for_segments(args: argparse.Namespace, job_id: int, segment_ids: set[int]) -> list[dict[str, Any]]:
    deadline = time.time() + args.timeout_sec
    last_rows: list[dict[str, Any]] = []
    while time.time() < deadline:
        rows = [row for row in fetch_segments(args, job_id) if int(row.get("id") or 0) in segment_ids]
        last_rows = rows
        if rows and all(str(row.get("status") or "").lower() in {"synthesized", "skipped"} for row in rows):
            return rows
        time.sleep(args.poll_sec)
    statuses = {row.get("id"): row.get("status") for row in last_rows}
    raise TimeoutError(f"segments did not finish before timeout: {statuses}")


def count_non_ws(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def candidate_meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    if not isinstance(meta, dict):
        return {}
    raw = meta.get("tts_lora_candidate")
    return raw if isinstance(raw, dict) else {}


def candidate_runtime(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    if not isinstance(meta, dict):
        return {}
    candidate = candidate_meta(row)
    runtime = candidate.get("runtime")
    if isinstance(runtime, dict):
        return runtime
    raw = meta.get("tts_lora_candidate_runtime")
    return raw if isinstance(raw, dict) else {}


def segment_summary(
    row: dict[str, Any],
    inserted: dict[str, Any],
    *,
    api_base: str,
    min_target_sec: float,
    min_text_chars: int,
) -> dict[str, Any]:
    target_ms = int(row.get("original_duration_ms") or inserted.get("target_ms") or 0)
    duration_ms = int(row.get("tts_duration_ms") or 0)
    text = str(row.get("tgt_text") or row.get("target_text") or inserted.get("text") or "")
    candidate = candidate_meta(row)
    runtime = candidate_runtime(row)
    audio_relpath = str(row.get("tts_audio_path") or row.get("tts_audio_rel_path") or "")
    eligible = (target_ms / 1000.0) >= min_target_sec or count_non_ws(text) >= min_text_chars
    return {
        "sample_id": inserted.get("sample_id"),
        "segment_id": row.get("id"),
        "ordinal": row.get("ordinal"),
        "status": row.get("status"),
        "eligible": eligible,
        "target_ms": target_ms,
        "tts_duration_ms": duration_ms,
        "drift_ms": duration_ms - target_ms if duration_ms else None,
        "text_chars": count_non_ws(text),
        "tts_audio_relpath": audio_relpath,
        "artifact_url": api_base.rstrip("/") + "/data-artifacts/" + audio_relpath if audio_relpath else "",
        "candidate_plan": {
            "enabled": candidate.get("enabled"),
            "phase": candidate.get("phase"),
            "audio_selection_mode": candidate.get("audio_selection_mode"),
            "selected_family": candidate.get("selected_family"),
            "fallback_reason": candidate.get("fallback_reason"),
            "candidate_execution_enabled": candidate.get("candidate_execution_enabled"),
        },
        "runtime": {
            "audio_selection_mode": runtime.get("audio_selection_mode"),
            "selected_family": runtime.get("selected_family"),
            "selected_split_strategy": runtime.get("selected_split_strategy"),
            "fallback_reason": runtime.get("fallback_reason"),
            "candidate_count": len(runtime.get("candidates") or []),
            "split_parts": len(((runtime.get("split_plan") or {}) if isinstance(runtime.get("split_plan"), dict) else {}).get("parts") or []),
            "gate_decision": runtime.get("gate_decision"),
        },
    }


def validate_segments(
    segments: list[dict[str, Any]],
    *,
    allow_fallback: bool,
    expect_split_mix: set[str],
    max_candidate_drift_ms: float,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for segment in segments:
        sample_id = str(segment.get("sample_id") or segment.get("segment_id"))

        def issue(reason: str, value: Any, expected: Any = None) -> None:
            issues.append({"sample_id": sample_id, "reason": reason, "value": value, "expected": expected})

        if segment.get("status") != "synthesized":
            issue("segment_not_synthesized", segment.get("status"), "synthesized")
        if not segment.get("tts_audio_relpath"):
            issue("missing_tts_audio_relpath", segment.get("tts_audio_relpath"), "non-empty audio path")

        runtime = segment.get("runtime") or {}
        plan = segment.get("candidate_plan") or {}
        if segment.get("eligible"):
            if plan.get("enabled") is not True:
                issue("eligible_candidate_plan_not_enabled", plan.get("enabled"), True)
            if runtime.get("audio_selection_mode") != "candidate_audio":
                issue("eligible_runtime_not_candidate_audio", runtime.get("audio_selection_mode"), "candidate_audio")
            if runtime.get("fallback_reason") and not allow_fallback:
                issue("candidate_fallback", runtime.get("fallback_reason"), "")
            if abs(float(segment.get("drift_ms") or 0.0)) > max_candidate_drift_ms:
                issue("candidate_drift_exceeds_limit", segment.get("drift_ms"), f"<= {max_candidate_drift_ms} ms")
        else:
            if plan.get("enabled") is not False:
                issue("ineligible_candidate_plan_not_disabled", plan.get("enabled"), False)
            if plan.get("audio_selection_mode") != "default_audio":
                issue("ineligible_audio_selection_not_default", plan.get("audio_selection_mode"), "default_audio")

        if sample_id in expect_split_mix and runtime.get("selected_family") != "split_mix":
            issue("expected_split_mix_not_selected", runtime.get("selected_family"), "split_mix")
    return issues


def build_summary(
    *,
    args: argparse.Namespace,
    job: dict[str, Any],
    inserted: list[dict[str, Any]],
    finished_rows: list[dict[str, Any]],
    elapsed_sec: float,
) -> dict[str, Any]:
    inserted_by_id = {int(item["segment_id"]): item for item in inserted}
    summaries = [
        segment_summary(
            row,
            inserted_by_id[int(row.get("id") or 0)],
            api_base=args.api_base,
            min_target_sec=args.min_target_sec,
            min_text_chars=args.min_text_chars,
        )
        for row in finished_rows
        if int(row.get("id") or 0) in inserted_by_id
    ]
    issues = validate_segments(
        summaries,
        allow_fallback=args.allow_fallback,
        expect_split_mix=set(split_csv(args.expect_split_mix)),
        max_candidate_drift_ms=args.max_candidate_drift_ms,
    )
    return {
        "schema_version": "holodub.main_flow_lora_candidate_smoke.v1",
        "ok": not issues,
        "elapsed_sec": round(elapsed_sec, 1),
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "sample_ids": split_csv(args.sample_ids),
        "segments": summaries,
        "validation_issues": issues,
        "command_config": {
            "api_base": args.api_base,
            "feature_jsonl": args.feature_jsonl,
            "sample_ids": split_csv(args.sample_ids),
            "min_target_sec": args.min_target_sec,
            "min_text_chars": args.min_text_chars,
            "max_candidate_drift_ms": args.max_candidate_drift_ms,
            "allow_fallback": args.allow_fallback,
            "expect_split_mix": split_csv(args.expect_split_mix),
            "compose_files": args.compose_file or DEFAULT_COMPOSE_FILES,
        },
    }


def main() -> int:
    args = parse_args()
    start = time.time()
    rows = read_feature_rows(Path(args.feature_jsonl), split_csv(args.sample_ids))
    job = create_job(args)
    job_id = int(job["id"])
    inserted = insert_segments(args, job_id, rows)
    enqueue_tts(args, job_id, [int(item["segment_id"]) for item in inserted])
    finished = wait_for_segments(args, job_id, {int(item["segment_id"]) for item in inserted})
    summary = build_summary(args=args, job=job, inserted=inserted, finished_rows=finished, elapsed_sec=time.time() - start)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
