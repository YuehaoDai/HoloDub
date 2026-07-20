from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lora_spoken_text import apply_spoken_text_rows


REPO_ROOT = Path(__file__).resolve().parents[1]

LIGHT_VECTOR = "0.12,0,0,0,0,0,0.08,0.15"


@dataclass(frozen=True)
class CandidateSpec:
    label: str
    raw_label: str
    use_emo_text: bool = False
    emo_alpha: float = 1.0
    emo_vector: str = ""
    fit_suffix: str = "gatefit"
    max_fit_delta: float = 0.05
    min_fit_drift_ms: float = 300.0
    duration_token_rate_policy: str | None = None
    duration_token_rate: float | None = None


BASE_CANDIDATES = (
    CandidateSpec(label="flat", raw_label="flat"),
    CandidateSpec(label="light_vector", raw_label="light_vector", emo_vector=LIGHT_VECTOR),
    CandidateSpec(label="emotext_a05", raw_label="emotext_a05", use_emo_text=True, emo_alpha=0.5),
    CandidateSpec(
        label="rescue_r23_emotext",
        raw_label="rescue_r23_emotext",
        use_emo_text=True,
        emo_alpha=0.5,
        duration_token_rate_policy="constant",
        duration_token_rate=23.4375,
    ),
)

COMPARISON_BASE_CANDIDATES = (
    CandidateSpec(
        label="emotext_a05_08",
        raw_label="emotext_a05",
        use_emo_text=True,
        emo_alpha=0.5,
        fit_suffix="gatefit08",
        max_fit_delta=0.08,
    ),
    CandidateSpec(
        label="emotext_a05_10",
        raw_label="emotext_a05",
        use_emo_text=True,
        emo_alpha=0.5,
        fit_suffix="gatefit10",
        max_fit_delta=0.10,
    ),
)

SPLIT_CANDIDATES = ("split_emotext", "split_light_vector", "split_mix")
COMPARISON_SPLIT_CANDIDATES = ("split_light_vector_10",)
ALL_CANDIDATE_FAMILIES = tuple(spec.label for spec in BASE_CANDIDATES) + SPLIT_CANDIDATES
KNOWN_CANDIDATE_FAMILIES = (
    ALL_CANDIDATE_FAMILIES
    + tuple(spec.label for spec in COMPARISON_BASE_CANDIDATES)
    + COMPARISON_SPLIT_CANDIDATES
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the HoloDub LoRA canary candidate loop: synth, fit, ASR, prosody, gate, pack, summarize."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--host-data-root", default="data")
    parser.add_argument("--voice-prompt-relpath", default="voices/test_ref.wav")
    parser.add_argument("--sample-ids", default="")
    parser.add_argument(
        "--sample-ids-file",
        default="",
        help="Text file containing comma-separated sample ids or one sample id per line.",
    )
    parser.add_argument(
        "--candidate-families",
        default="all",
        help="Comma-separated candidate families to run. Use 'all' for the default full set.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing raw/fit/ASR/prosody/split reports in --run-dir when present.",
    )
    parser.add_argument(
        "--skip-inference",
        action="store_true",
        help="Reuse existing canary reports and only run planning/gate/pack/summary steps.",
    )
    parser.add_argument(
        "--existing-run-root",
        default="",
        help="Existing artifact root used by --skip-inference. Defaults to the feature file parent.",
    )
    parser.add_argument("--docker-image", default="holodub-ml-service:latest")
    parser.add_argument("--no-docker", action="store_true", help="Run Python tools directly instead of docker.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--repetition-penalty", type=float, default=10.0)
    parser.add_argument("--length-penalty", type=float, default=0.0)
    parser.add_argument("--duration-token-rate-policy", choices=("constant", "piecewise"), default="piecewise")
    parser.add_argument("--duration-token-rate", type=float, default=23.4375)
    parser.add_argument(
        "--spoken-text-policy",
        choices=("none", "figure-digits", "technical-v2", "technical-v3"),
        default="none",
        help="Apply deterministic spoken-form rewrites before TTS, preserving display_text in features.",
    )
    parser.add_argument("--split-min-target-sec", type=float, default=18.0)
    parser.add_argument("--split-min-text-chars", type=int, default=180)
    parser.add_argument(
        "--split-min-chars-per-sec",
        type=float,
        default=0.0,
        help="Forwarded to lora_split_plan.py --min-chars-per-sec-to-split; 0 disables the density trigger.",
    )
    parser.add_argument("--split-max-part-sec", type=float, default=16.0)
    parser.add_argument("--split-max-part-chars", type=int, default=170)
    parser.add_argument("--split-max-part-chars-per-sec", type=float, default=5.6)
    parser.add_argument("--split-max-parts", type=int, default=4)
    parser.add_argument("--split-pause-sec", type=float, default=0.18)
    parser.add_argument(
        "--split-force-break-after-regex",
        action="append",
        default=[],
        help="Regex forwarded to lora_split_plan.py --force-break-after-regex.",
    )
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    parser.add_argument("--min-similarity", type=float, default=0.97)
    parser.add_argument("--target-similarity", type=float, default=0.985)
    parser.add_argument("--min-critical-term-recall", type=float, default=0.8)
    parser.add_argument("--target-critical-term-recall", type=float, default=0.95)
    parser.add_argument("--min-tail-recall", type=float, default=0.0)
    parser.add_argument("--min-f0-range", type=float, default=4.5)
    parser.add_argument("--target-f0-range", type=float, default=5.4)
    parser.add_argument("--max-drift-ms", type=float, default=500.0)
    parser.add_argument("--target-mean-drift-ms", type=float, default=300.0)
    parser.add_argument("--asr-model", default="large-v3")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def path_str(path: Path | str) -> str:
    return str(path)


def rel_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def docker_path(path: Path | str) -> str:
    raw = Path(path)
    if not raw.is_absolute():
        raw = (REPO_ROOT / raw).resolve()
    data_root = (REPO_ROOT / "data").resolve()
    try:
        rel = raw.relative_to(data_root).as_posix()
        return f"/data/{rel}"
    except ValueError:
        pass
    try:
        rel = raw.relative_to(REPO_ROOT).as_posix()
        return f"/workspace/{rel}"
    except ValueError:
        return raw.as_posix()


def sample_id_set(raw: str) -> set[str]:
    normalized = raw.replace("\r", "\n").replace(",", "\n")
    return {item.strip() for item in normalized.splitlines() if item.strip()}


def requested_sample_ids(args: argparse.Namespace) -> set[str]:
    ids = sample_id_set(args.sample_ids)
    if args.sample_ids_file:
        ids.update(sample_id_set(Path(args.sample_ids_file).read_text(encoding="utf-8")))
    return ids


def candidate_family_set(args: argparse.Namespace) -> set[str]:
    raw = str(args.candidate_families or "all").strip()
    if not raw or raw.lower() == "all":
        return set(ALL_CANDIDATE_FAMILIES)
    values = sample_id_set(raw)
    unknown = sorted(values - set(KNOWN_CANDIDATE_FAMILIES))
    if unknown:
        raise ValueError(f"unknown candidate families: {', '.join(unknown)}")
    if not values:
        raise ValueError("--candidate-families resolved to an empty set")
    return values


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run_command(
    command: list[str],
    *,
    commands: list[dict[str, Any]],
    label: str,
    cwd: Path = REPO_ROOT,
) -> None:
    print(f"[loop] {label}: {command_text(command)}")
    subprocess.run(command, cwd=str(cwd), check=True)
    commands.append({"label": label, "command": command})


def reuse_existing(
    args: argparse.Namespace,
    path: Path,
    commands: list[dict[str, Any]],
    label: str,
    *,
    expected_sample_ids: set[str] | None = None,
    expected_paths: set[str] | None = None,
) -> bool:
    if not args.resume or not path.exists():
        return False
    if expected_sample_ids is not None:
        available = report_sample_ids(path)
        missing = sorted(expected_sample_ids - available)
        if missing:
            print(f"[loop] {label}: not reusing {path}; missing sample ids: {','.join(missing[:12])}")
            return False
    if expected_paths is not None:
        available_paths = report_audio_paths(path)
        expected_norm = {normalize_path(item) for item in expected_paths}
        missing_paths = expected_norm - available_paths
        if missing_paths:
            preview = ",".join(sorted(missing_paths)[:4])
            print(f"[loop] {label}: not reusing {path}; missing audio paths: {preview}")
            return False
    print(f"[loop] {label}: reuse {path}")
    commands.append({"label": label, "reused": str(path)})
    return True


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def prepare_spoken_features(
    args: argparse.Namespace,
    dirs: dict[str, Path],
    commands: list[dict[str, Any]],
) -> None:
    policy = str(getattr(args, "spoken_text_policy", "none") or "none")
    args.original_features = str(args.features)
    args.spoken_text_features = ""
    args.spoken_text_rewrite_count = 0
    args.spoken_text_rewritten_samples = 0
    if policy == "none":
        return

    rows = read_jsonl_rows(Path(args.features))
    requested = requested_sample_ids(args)
    target_sample_ids = requested if requested else None
    rewritten = apply_spoken_text_rows(rows, policy, target_sample_ids=target_sample_ids)
    output = dirs["plan"] / "spoken.features.jsonl"
    write_jsonl(output, rewritten)

    rewrite_count = sum(len(row.get("spoken_text_rewrites") or []) for row in rewritten)
    rewritten_sample_ids = sorted(
        str(row.get("sample_id"))
        for row in rewritten
        if row.get("sample_id") and row.get("spoken_text_rewrites")
    )
    sample_count = len(rewritten_sample_ids)
    args.features = str(output)
    args.spoken_text_features = str(output)
    args.spoken_text_rewrite_count = rewrite_count
    args.spoken_text_rewritten_samples = sample_count
    commands.append(
        {
            "label": "spoken_text",
            "policy": policy,
            "source": str(args.original_features),
            "output": str(output),
            "target_sample_ids": sorted(target_sample_ids) if target_sample_ids is not None else [],
            "rewrite_count": rewrite_count,
            "rewritten_samples": sample_count,
            "rewritten_sample_ids": rewritten_sample_ids,
        }
    )
    print(
        json.dumps(
            {
                "spoken_text_policy": policy,
                "source": str(args.original_features),
                "output": str(output),
                "target_sample_ids": sorted(target_sample_ids) if target_sample_ids is not None else [],
                "rewrite_count": rewrite_count,
                "rewritten_samples": sample_count,
                "rewritten_sample_ids": rewritten_sample_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def feature_target_sec(row: dict[str, Any]) -> float:
    target = float(row.get("target_ms") or 0.0) / 1000.0
    if target <= 0:
        target = float(row.get("audio_ms") or 0.0) / 1000.0
    return target


def loop_duration_token_rate(target_sec: float, args: argparse.Namespace) -> float:
    if args.duration_token_rate_policy == "piecewise":
        if target_sec < 15.0:
            return 23.4375
        if target_sec < 23.0:
            return 30.0
        return 32.5
    return float(args.duration_token_rate)


def add_full_duration_fields(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        target = max(0.05, feature_target_sec(row))
        rate = loop_duration_token_rate(target, args)
        tokens = max(1, int(round(target * rate)))
        output.append(
            {
                **row,
                "duration_token_rate": rate,
                "duration_tokens_from_target": tokens,
                "duration_tokens_from_audio": tokens,
                "code_len": tokens,
            }
        )
    return output


def prepare_full_duration_features(
    args: argparse.Namespace,
    dirs: dict[str, Path],
    commands: list[dict[str, Any]],
) -> None:
    rows = read_jsonl_rows(Path(args.features))
    output = dirs["plan"] / "full.features.jsonl"
    write_jsonl(output, add_full_duration_fields(rows, args))
    args.full_duration_features = str(output)
    args.features = str(output)
    commands.append(
        {
            "label": "full_duration_plan",
            "source": str(args.spoken_text_features or getattr(args, "original_features", args.features)),
            "output": str(output),
            "duration_token_rate_policy": args.duration_token_rate_policy,
            "duration_token_rate": args.duration_token_rate,
        }
    )
    print(
        json.dumps(
            {
                "full_duration_features": str(output),
                "rows": len(rows),
                "duration_token_rate_policy": args.duration_token_rate_policy,
                "duration_token_rate": args.duration_token_rate,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def jsonl_sample_ids(path: Path) -> set[str]:
    return {str(row["sample_id"]) for row in read_jsonl_rows(path) if row.get("sample_id")}


def report_sample_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(row["sample_id"]) for row in read_json(path).get("rows", []) if row.get("sample_id")}


def report_audio_paths(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {normalize_path(str(row["path"])) for row in read_json(path).get("rows", []) if row.get("path")}


def split_plan_sample_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = read_json(path)
    ids = {str(row["sample_id"]) for row in payload.get("planned", []) if row.get("sample_id")}
    ids.update(str(row["sample_id"]) for row in payload.get("skipped", []) if row.get("sample_id"))
    return ids


def docker_python_command(args: argparse.Namespace, tool_args: list[str]) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "-v",
        f"{REPO_ROOT}:/workspace",
        "-v",
        f"{REPO_ROOT / 'data'}:/data",
        "-v",
        f"{REPO_ROOT / 'hf-cache'}:/root/.cache/huggingface",
        "-v",
        f"{REPO_ROOT / 'torch-cache'}:/root/.cache/torch",
        "-w",
        "/workspace",
        args.docker_image,
        "python3",
        *tool_args,
    ]


def python_tool_command(args: argparse.Namespace, tool_args: list[str], *, docker: bool = False) -> list[str]:
    if docker and not args.no_docker:
        return docker_python_command(args, tool_args)
    return [args.python, *tool_args]


def selected_sample_ids(args: argparse.Namespace) -> str:
    return ",".join(sorted(requested_sample_ids(args)))


def filter_report(source: Path, output: Path, ids: set[str]) -> Path:
    payload = read_json(source)
    if ids:
        payload["rows"] = [row for row in payload.get("rows", []) if str(row.get("sample_id")) in ids]
        if "summary" in payload:
            payload["summary"] = recompute_asr_summary(payload["rows"])
    write_json(output, payload)
    return output


def recompute_asr_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_variant.setdefault(str(row.get("variant")), []).append(row)

    summary: dict[str, Any] = {}
    for variant, values in by_variant.items():
        sims = [float(row["similarity"]) for row in values if row.get("similarity") is not None]
        terms = [
            float(row["critical_term_recall"])
            for row in values
            if row.get("critical_term_recall") is not None
        ]
        latin = [
            float(row["latin_surface_recall"])
            for row in values
            if row.get("latin_surface_recall") is not None
        ]
        summary[variant] = {
            "count": len(values),
            "mean_similarity": mean(sims),
            "min_similarity": min(sims) if sims else None,
            "mean_critical_term_recall": mean(terms),
            "min_critical_term_recall": min(terms) if terms else None,
            "mean_latin_surface_recall": mean(latin),
            "min_latin_surface_recall": min(latin) if latin else None,
        }
    return summary


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def legacy_report_paths(existing_root: Path) -> dict[str, Path]:
    return {
        "flat": existing_root / "canary24_flat_gatefit" / "asr_report.json",
        "light_vector": existing_root / "canary24_light_vector_gatefit" / "asr_report.json",
        "emotext_a05": existing_root / "canary24_emotext_a05_gatefit" / "asr_report.json",
        "emotext_a05_08": existing_root / "canary24_emotext_a05_gatefit08" / "asr_report.json",
        "rescue_r23_emotext": existing_root / "canary24_rescue_r23_emotext_a05_gatefit" / "asr_report.json",
        "split_emotext": existing_root / "canary24_rescue_split_seg10686_gatefit" / "asr_report.json",
        "split_mix": existing_root / "canary24_rescue_split_seg10686_mix_gatefit" / "asr_report.json",
    }


def ensure_dirs(run_dir: Path) -> dict[str, Path]:
    dirs = {
        "plan": run_dir / "plan",
        "raw": run_dir / "raw",
        "gatefit": run_dir / "gatefit",
        "split": run_dir / "split",
        "asr": run_dir / "asr",
        "selected": run_dir / "selected",
        "listening_pack": run_dir / "listening_pack",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def copy_if_different(source: Path, target: Path) -> None:
    if not source.exists():
        return
    try:
        if source.resolve() == target.resolve():
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copytree_if_different(source: Path, target: Path) -> None:
    if not source.exists() or not source.is_dir():
        return
    try:
        if source.resolve() == target.resolve():
            return
    except OSError:
        pass
    shutil.copytree(source, target, dirs_exist_ok=True)


def existing_combined_gate(existing_root: Path) -> Path | None:
    gate = existing_root / "candidate_gate_report.json"
    if gate.exists():
        return gate
    return None


def reuse_combined_gate_artifacts(
    existing_root: Path,
    run_dir: Path,
    dirs: dict[str, Path],
    commands: list[dict[str, Any]],
) -> Path | None:
    gate = existing_combined_gate(existing_root)
    if gate is None:
        return None
    output = run_dir / "candidate_gate_report.json"
    copy_if_different(gate, output)
    copy_if_different(existing_root / "prosody_report.json", run_dir / "prosody_report.json")
    copy_if_different(existing_root / "quality_baseline.json", run_dir / "quality_baseline.json")
    copy_if_different(existing_root / "human_review_report.json", run_dir / "human_review_report.json")
    copy_if_different(existing_root / "risk_queue.json", run_dir / "risk_queue.json")
    copytree_if_different(existing_root / "selected", dirs["selected"])
    commands.append({"label": "combined_gate_reuse", "source": str(gate), "output": str(output)})
    return output


def run_split_plan(args: argparse.Namespace, dirs: dict[str, Path], commands: list[dict[str, Any]]) -> tuple[Path, Path]:
    split_features = dirs["plan"] / "split.features.jsonl"
    split_plan = dirs["plan"] / "split.plan.json"
    requested = requested_sample_ids(args)
    plan_covers_request = not requested or requested <= split_plan_sample_ids(split_plan)
    if args.resume and split_features.exists() and split_plan.exists() and plan_covers_request:
        print(f"[loop] split_plan: reuse {split_plan}")
        commands.append({"label": "split_plan", "reused": str(split_plan)})
        return split_features, split_plan
    if args.resume and split_features.exists() and split_plan.exists() and not plan_covers_request:
        missing = sorted(requested - split_plan_sample_ids(split_plan))
        print(f"[loop] split_plan: not reusing {split_plan}; missing sample ids: {','.join(missing[:12])}")
    cmd = python_tool_command(
        args,
        [
            "tools/lora_split_plan.py",
            "--features",
            path_str(Path(args.features)),
            "--output-features",
            path_str(split_features),
            "--output-plan",
            path_str(split_plan),
            "--min-target-sec",
            str(args.split_min_target_sec),
            "--min-text-chars",
            str(args.split_min_text_chars),
            "--min-chars-per-sec-to-split",
            str(args.split_min_chars_per_sec),
            "--max-part-sec",
            str(args.split_max_part_sec),
            "--max-part-chars",
            str(args.split_max_part_chars),
            "--max-part-chars-per-sec",
            str(args.split_max_part_chars_per_sec),
            "--max-parts",
            str(args.split_max_parts),
            "--pause-sec",
            str(args.split_pause_sec),
            *sum(
                (["--force-break-after-regex", pattern] for pattern in args.split_force_break_after_regex),
                [],
            ),
            "--duration-token-rate-policy",
            args.duration_token_rate_policy,
            "--duration-token-rate",
            str(args.duration_token_rate),
            *(
                ["--sample-ids", selected_sample_ids(args)]
                if selected_sample_ids(args)
                else []
            ),
        ],
    )
    run_command(cmd, commands=commands, label="split_plan")
    return split_features, split_plan


def infer_candidate(
    args: argparse.Namespace,
    features: Path,
    output_dir: Path,
    spec: CandidateSpec,
    commands: list[dict[str, Any]],
    *,
    split_parts: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "inference_report.json"
    expected_ids = jsonl_sample_ids(features)
    requested = requested_sample_ids(args)
    if requested and not split_parts:
        expected_ids &= requested
    if reuse_existing(args, report, commands, f"infer_{spec.label}", expected_sample_ids=expected_ids):
        return report
    tool_args = [
        "tools/lora_infer_indextts2.py",
        "--features",
        docker_path(features),
        "--adapter",
        docker_path(args.adapter),
        "--output-dir",
        docker_path(output_dir),
        "--data-root",
        args.data_root,
        "--voice-prompt-relpath",
        args.voice_prompt_relpath,
        "--skip-baseline",
        "--max-samples",
        "100000",
        "--seed",
        str(args.seed),
        "--num-beams",
        "1",
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--top-k",
        str(args.top_k),
        "--repetition-penalty",
        str(args.repetition_penalty),
        "--length-penalty",
        str(args.length_penalty),
        "--duration-control",
        "--duration-token-rate-policy",
        spec.duration_token_rate_policy or args.duration_token_rate_policy,
        "--duration-token-rate",
        str(spec.duration_token_rate if spec.duration_token_rate is not None else args.duration_token_rate),
        "--max-mel-headroom",
        "512" if split_parts else "128",
    ]
    if selected_sample_ids(args) and not split_parts:
        tool_args.extend(["--sample-ids", selected_sample_ids(args)])
    if spec.use_emo_text:
        tool_args.extend(["--use-emo-text", "--emo-alpha", str(spec.emo_alpha)])
    if spec.emo_vector:
        tool_args.extend(["--emo-vector", spec.emo_vector])
    cmd = python_tool_command(args, tool_args, docker=True)
    run_command(cmd, commands=commands, label=f"infer_{spec.label}")
    return report


def fit_report(
    args: argparse.Namespace,
    source_report: Path,
    output_dir: Path,
    output_report: Path,
    spec: CandidateSpec,
    commands: list[dict[str, Any]],
) -> Path:
    if reuse_existing(
        args,
        output_report,
        commands,
        f"fit_{spec.label}",
        expected_sample_ids=report_sample_ids(source_report),
    ):
        return output_report
    cmd = python_tool_command(
        args,
        [
            "tools/lora_duration_fit.py",
            "--inference-report",
            path_str(source_report),
            "--output-dir",
            path_str(output_dir),
            "--output-report",
            path_str(output_report),
            "--variants",
            "lora",
            "--variant-suffix",
            spec.fit_suffix,
            "--max-tempo-delta",
            str(spec.max_fit_delta),
            "--min-fit-drift-ms",
            str(spec.min_fit_drift_ms),
            "--filter",
            "atempo",
        ],
    )
    run_command(cmd, commands=commands, label=f"fit_{spec.label}")
    return output_report


def run_asr(
    args: argparse.Namespace,
    report: Path,
    output: Path,
    commands: list[dict[str, Any]],
    *,
    label: str,
) -> Path:
    if reuse_existing(args, output, commands, f"asr_{label}", expected_sample_ids=report_sample_ids(report)):
        return output
    cmd = python_tool_command(
        args,
        [
            "tools/lora_asr_eval.py",
            "--features",
            rel_to_repo(Path(args.features)),
            "--inference-report",
            rel_to_repo(report),
            "--output",
            rel_to_repo(output),
            "--model",
            args.asr_model,
        ],
        docker=True,
    )
    run_command(cmd, commands=commands, label=f"asr_{label}")
    return output


def rescore_reports(args: argparse.Namespace, reports: list[Path], commands: list[dict[str, Any]]) -> None:
    if not reports:
        return
    tool_args = ["tools/lora_asr_rescore_terms.py"]
    for report in reports:
        tool_args.extend(["--report", path_str(report)])
    run_command(python_tool_command(args, tool_args), commands=commands, label="term_rescore")


def run_prosody(
    args: argparse.Namespace,
    reports: list[Path],
    output: Path,
    commands: list[dict[str, Any]],
) -> Path:
    expected_paths: set[str] = set()
    for report in reports:
        expected_paths.update(report_audio_paths(report))
    if reuse_existing(args, output, commands, "prosody", expected_paths=expected_paths):
        return output
    joined = ",".join(rel_to_repo(report) for report in reports)
    cmd = python_tool_command(
        args,
        [
            "tools/lora_prosody_eval.py",
            "--features",
            rel_to_repo(Path(args.features)),
            "--data-root",
            args.data_root,
            "--reference-wav",
            args.voice_prompt_relpath,
            "--include-teacher",
            "--output",
            rel_to_repo(output),
            "--reports",
            joined,
        ],
        docker=True,
    )
    run_command(cmd, commands=commands, label="prosody")
    return output


def run_gate(
    args: argparse.Namespace,
    candidate_reports: dict[str, Path],
    prosody_report: Path,
    run_dir: Path,
    dirs: dict[str, Path],
    commands: list[dict[str, Any]],
) -> Path:
    output = run_dir / "candidate_gate_report.json"
    tool_args = [
        "tools/lora_candidate_gate.py",
        "--output",
        path_str(output),
        "--selected-dir",
        path_str(dirs["selected"]),
        "--prosody-report",
        path_str(prosody_report),
        "--max-drift-ms",
        str(args.max_drift_ms),
        "--target-mean-drift-ms",
        str(args.target_mean_drift_ms),
        "--min-similarity",
        str(args.min_similarity),
        "--target-similarity",
        str(args.target_similarity),
        "--min-f0-range",
        str(args.min_f0_range),
        "--target-f0-range",
        str(args.target_f0_range),
        "--min-critical-term-recall",
        str(args.min_critical_term_recall),
        "--target-critical-term-recall",
        str(args.target_critical_term_recall),
        "--min-tail-recall",
        str(args.min_tail_recall),
        "--max-fit-delta",
        str(args.max_fit_delta),
    ]
    for label, report in sorted(candidate_reports.items()):
        tool_args.extend(["--candidate", f"{label}={report}"])
    run_command(python_tool_command(args, tool_args), commands=commands, label="candidate_gate")
    return output


def run_listening_pack(
    args: argparse.Namespace,
    selected_report: Path,
    candidate_reports: dict[str, Path],
    dirs: dict[str, Path],
    commands: list[dict[str, Any]],
) -> Path:
    tool_args = [
        "tools/lora_build_listening_pack.py",
        "--output-dir",
        path_str(dirs["listening_pack"]),
        "--features",
        path_str(Path(args.features)),
        "--data-root",
        args.host_data_root,
        "--include-teacher",
        "--only-report-samples",
        "--reference-wav",
        args.voice_prompt_relpath,
        "--title",
        "HoloDub LoRA Canary Candidate Loop",
        "--report",
        f"selected={selected_report}",
    ]
    for label, report in sorted(candidate_reports.items()):
        tool_args.extend(["--report", f"{label}={report}"])
    run_command(python_tool_command(args, tool_args), commands=commands, label="listening_pack")
    return dirs["listening_pack"] / "index.html"


def copy_existing_reports(
    args: argparse.Namespace,
    run_dir: Path,
    dirs: dict[str, Path],
    existing_root: Path,
    ids: set[str],
) -> tuple[dict[str, Path], list[str]]:
    copied: dict[str, Path] = {}
    missing: list[str] = []
    enabled = candidate_family_set(args)
    for label, source in legacy_report_paths(existing_root).items():
        if label not in enabled:
            continue
        if not source.exists():
            missing.append(f"{label}:{source}")
            continue
        output = dirs["asr"] / f"{label}.json"
        copied[label] = filter_report(source, output, ids)
    return copied, missing


def available_samples_for_parts(plan_path: Path, parts: dict[str, dict[str, Any]]) -> list[str]:
    plan = read_json(plan_path)
    available: list[str] = []
    for row in plan.get("planned", []):
        part_ids = [str(part["sample_id"]) for part in row.get("parts", [])]
        if part_ids and all(part_id in parts for part_id in part_ids):
            available.append(str(row["sample_id"]))
    return available


def part_rows(report_path: Path) -> dict[str, dict[str, Any]]:
    if not report_path.exists():
        return {}
    return {str(row["sample_id"]): row for row in read_json(report_path).get("rows", []) if row.get("sample_id")}


def write_mixed_part_report(
    plan_path: Path,
    emotext_report: Path,
    light_report: Path,
    output_report: Path,
) -> tuple[Path, list[str]]:
    plan = read_json(plan_path)
    emotext = part_rows(emotext_report)
    light = part_rows(light_report)
    rows: list[dict[str, Any]] = []
    complete_samples: list[str] = []
    for sample in plan.get("planned", []):
        chosen: list[dict[str, Any]] = []
        for part in sorted(sample.get("parts", []), key=lambda item: int(item.get("index") or 0)):
            part_id = str(part["sample_id"])
            index = int(part.get("index") or 0)
            source = emotext.get(part_id) if index == 1 else light.get(part_id) or emotext.get(part_id)
            if source is None:
                chosen = []
                break
            chosen.append({**source, "split_mix_source": "emotext" if source is emotext.get(part_id) else "light_vector"})
        if chosen:
            complete_samples.append(str(sample["sample_id"]))
            rows.extend(chosen)
    payload = {
        "source_reports": [str(emotext_report), str(light_report)],
        "mix_policy": "part1_emotext_remaining_light_vector",
        "rows": rows,
    }
    write_json(output_report, payload)
    return output_report, complete_samples


def assemble_split_candidate(
    args: argparse.Namespace,
    split_plan: Path,
    part_reports: list[Path],
    output_dir: Path,
    output_report: Path,
    variant: str,
    sample_ids: list[str],
    commands: list[dict[str, Any]],
) -> Path | None:
    if not sample_ids:
        return None
    if reuse_existing(args, output_report, commands, f"assemble_{variant}", expected_sample_ids=set(sample_ids)):
        return output_report
    tool_args = [
        "tools/lora_split_assemble.py",
        "--plan",
        path_str(split_plan),
        "--output-dir",
        path_str(output_dir),
        "--output-report",
        path_str(output_report),
        "--variant",
        variant,
        "--data-root",
        args.data_root,
        "--sample-ids",
        ",".join(sample_ids),
    ]
    for report in part_reports:
        tool_args.extend(["--parts-report", path_str(report)])
    run_command(python_tool_command(args, tool_args), commands=commands, label=f"assemble_{variant}")
    return output_report


def overlay_asr_report(assembled_report: Path, asr_source: Path, output_report: Path) -> Path:
    assembled = read_json(assembled_report)
    asr = read_json(asr_source)
    asr_by_id = {str(row["sample_id"]): row for row in asr.get("rows", [])}
    rows: list[dict[str, Any]] = []
    for row in assembled.get("rows", []):
        sample_id = str(row["sample_id"])
        if sample_id not in asr_by_id:
            continue
        rows.append({**asr_by_id[sample_id], **row})
    payload = {
        "features": asr.get("features"),
        "inference_report": str(assembled_report),
        "model": asr.get("model"),
        "rows": rows,
        "summary": recompute_asr_summary(rows),
        "asr_source_report": str(asr_source),
    }
    write_json(output_report, payload)
    return output_report


def duplicate_prosody_aliases(existing_report: Path, output_report: Path, aliases: dict[str, str]) -> Path:
    if not existing_report.exists():
        write_json(output_report, {"rows": [], "summary": {}, "reports": []})
        return output_report
    payload = read_json(existing_report)
    rows = list(payload.get("rows", []))
    by_path = {normalize_path(str(row.get("path") or "")): row for row in rows}
    for new_path, source_path in aliases.items():
        source = by_path.get(normalize_path(source_path))
        if not source:
            continue
        rows.append({**source, "path": new_path})
    payload["rows"] = rows
    write_json(output_report, payload)
    return output_report


def normalize_path(path: str) -> str:
    return str(Path(path.replace("/data/", "data/"))).replace("\\", "/").lower()


def build_skip_inference_split_reports(
    args: argparse.Namespace,
    existing_root: Path,
    split_plan: Path,
    dirs: dict[str, Path],
    candidate_reports: dict[str, Path],
    commands: list[dict[str, Any]],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    enabled = candidate_family_set(args)
    emotext_parts = existing_root / "canary24_rescue_split_seg10686_parts_gatefit" / "inference_report.json"
    light_parts = existing_root / "canary24_rescue_split_seg10686_p2_light_gatefit" / "inference_report.json"
    if not emotext_parts.exists():
        return aliases

    if "split_emotext" in enabled:
        emotext_available = available_samples_for_parts(split_plan, part_rows(emotext_parts))
        split_emotext_report = assemble_split_candidate(
            args,
            split_plan,
            [emotext_parts],
            dirs["split"] / "split_emotext",
            dirs["split"] / "split_emotext" / "inference_report.json",
            "split_auto_emotext_gatefit",
            emotext_available,
            commands,
        )
        source_asr = existing_root / "canary24_rescue_split_seg10686_gatefit" / "asr_report.json"
        if split_emotext_report and source_asr.exists():
            output = dirs["asr"] / "split_emotext.json"
            candidate_reports["split_emotext"] = overlay_asr_report(split_emotext_report, source_asr, output)
            rows = read_json(candidate_reports["split_emotext"]).get("rows", [])
            source_rows = read_json(source_asr).get("rows", [])
            if rows and source_rows:
                aliases[str(rows[0]["path"])] = str(source_rows[0]["path"])

    if "split_mix" in enabled and light_parts.exists():
        mix_parts_report, mix_samples = write_mixed_part_report(
            split_plan,
            emotext_parts,
            light_parts,
            dirs["split"] / "split_mix_parts_report.json",
        )
        split_mix_report = assemble_split_candidate(
            args,
            split_plan,
            [mix_parts_report],
            dirs["split"] / "split_mix",
            dirs["split"] / "split_mix" / "inference_report.json",
            "split_auto_mix_gatefit",
            mix_samples,
            commands,
        )
        mix_asr = existing_root / "canary24_rescue_split_seg10686_mix_gatefit" / "asr_report.json"
        if split_mix_report and mix_asr.exists():
            output = dirs["asr"] / "split_mix.json"
            candidate_reports["split_mix"] = overlay_asr_report(split_mix_report, mix_asr, output)
            rows = read_json(candidate_reports["split_mix"]).get("rows", [])
            source_rows = read_json(mix_asr).get("rows", [])
            if rows and source_rows:
                aliases[str(rows[0]["path"])] = str(source_rows[0]["path"])
    return aliases


def generate_full_candidates(
    args: argparse.Namespace,
    dirs: dict[str, Path],
    split_features: Path,
    split_plan: Path,
    commands: list[dict[str, Any]],
) -> dict[str, Path]:
    candidate_reports: dict[str, Path] = {}
    raw_reports: dict[str, Path] = {}
    enabled = candidate_family_set(args)
    full_features = Path(getattr(args, "full_duration_features", "") or args.features)

    for spec in (*BASE_CANDIDATES, *COMPARISON_BASE_CANDIDATES):
        if spec.label not in enabled:
            continue
        if spec.raw_label not in raw_reports:
            raw_reports[spec.raw_label] = infer_candidate(
                args,
                full_features,
                dirs["raw"] / spec.raw_label,
                spec,
                commands,
            )
        fit_output = dirs["gatefit"] / spec.label / "inference_report.json"
        fit_report_path = fit_report(
            args,
            raw_reports[spec.raw_label],
            dirs["gatefit"] / spec.label,
            fit_output,
            spec,
            commands,
        )
        candidate_reports[spec.label] = run_asr(
            args,
            fit_report_path,
            dirs["asr"] / f"{spec.label}.json",
            commands,
            label=spec.label,
        )

    if split_features.exists() and split_features.read_text(encoding="utf-8").strip():
        need_split_emotext_parts = bool({"split_emotext", "split_mix"} & enabled)
        need_split_light_parts = bool({"split_light_vector", "split_light_vector_10", "split_mix"} & enabled)
        if not (need_split_emotext_parts or need_split_light_parts):
            return candidate_reports
        split_emotext_spec = CandidateSpec(
            label="split_emotext",
            raw_label="split_emotext",
            use_emo_text=True,
            emo_alpha=0.5,
            max_fit_delta=args.max_fit_delta,
            min_fit_drift_ms=40.0,
        )
        split_light_spec = CandidateSpec(
            label="split_light_vector",
            raw_label="split_light_vector",
            emo_vector=LIGHT_VECTOR,
            max_fit_delta=args.max_fit_delta,
            min_fit_drift_ms=40.0,
        )
        split_light10_spec = CandidateSpec(
            label="split_light_vector_10",
            raw_label="split_light_vector",
            emo_vector=LIGHT_VECTOR,
            fit_suffix="gatefit10",
            max_fit_delta=0.10,
            min_fit_drift_ms=40.0,
        )
        split_part_reports: dict[str, Path] = {}
        split_specs = []
        if need_split_emotext_parts:
            split_specs.append(split_emotext_spec)
        if need_split_light_parts:
            split_specs.append(split_light_spec)
        if "split_light_vector_10" in enabled:
            split_specs.append(split_light10_spec)
        for spec in split_specs:
            raw = infer_candidate(args, split_features, dirs["raw"] / spec.label, spec, commands, split_parts=True)
            split_part_reports[spec.label] = fit_report(
                args,
                raw,
                dirs["gatefit"] / f"{spec.label}_parts",
                dirs["gatefit"] / f"{spec.label}_parts" / "inference_report.json",
                spec,
                commands,
            )

        plan = read_json(split_plan)
        all_split_samples = [str(row["sample_id"]) for row in plan.get("planned", [])]
        assembled_reports: dict[str, Path | None] = {}
        if "split_emotext" in enabled and "split_emotext" in split_part_reports:
            assembled_reports["split_emotext"] = assemble_split_candidate(
                args,
                split_plan,
                [split_part_reports["split_emotext"]],
                dirs["split"] / "split_emotext",
                dirs["split"] / "split_emotext" / "inference_report.json",
                "split_auto_emotext_gatefit",
                all_split_samples,
                commands,
            )
        if "split_light_vector" in enabled and "split_light_vector" in split_part_reports:
            assembled_reports["split_light_vector"] = assemble_split_candidate(
                args,
                split_plan,
                [split_part_reports["split_light_vector"]],
                dirs["split"] / "split_light_vector",
                dirs["split"] / "split_light_vector" / "inference_report.json",
                "split_auto_light_gatefit",
                all_split_samples,
                commands,
            )
        if "split_light_vector_10" in enabled and "split_light_vector_10" in split_part_reports:
            assembled_reports["split_light_vector_10"] = assemble_split_candidate(
                args,
                split_plan,
                [split_part_reports["split_light_vector_10"]],
                dirs["split"] / "split_light_vector_10",
                dirs["split"] / "split_light_vector_10" / "inference_report.json",
                "split_auto_light_gatefit10",
                all_split_samples,
                commands,
            )
        if "split_mix" in enabled and {"split_emotext", "split_light_vector"} <= set(split_part_reports):
            mix_parts_report, mix_samples = write_mixed_part_report(
                split_plan,
                split_part_reports["split_emotext"],
                split_part_reports["split_light_vector"],
                dirs["split"] / "split_mix_parts_report.json",
            )
            assembled_reports["split_mix"] = assemble_split_candidate(
                args,
                split_plan,
                [mix_parts_report],
                dirs["split"] / "split_mix",
                dirs["split"] / "split_mix" / "inference_report.json",
                "split_auto_mix_gatefit",
                mix_samples,
                commands,
            )
        for label, report in assembled_reports.items():
            if report:
                candidate_reports[label] = run_asr(args, report, dirs["asr"] / f"{label}.json", commands, label=label)
    return candidate_reports


def build_summary(
    args: argparse.Namespace,
    run_dir: Path,
    candidate_reports: dict[str, Path],
    gate_report: Path,
    listening_pack: Path,
    commands: list[dict[str, Any]],
    missing_candidates: list[str],
) -> dict[str, Any]:
    gate = read_json(gate_report)
    rows = gate.get("rows", [])
    policy = gate.get("policy", {})
    min_asr = float(policy.get("min_similarity") or args.min_similarity)
    min_terms = float(policy.get("min_critical_term_recall") or args.min_critical_term_recall)
    low_asr = [
        {
            "sample_id": row.get("sample_id"),
            "candidate_label": row.get("candidate_label"),
            "similarity": row.get("similarity"),
            "abs_drift_ms": row.get("abs_drift_ms"),
        }
        for row in rows
        if float(row.get("similarity") or 0.0) < min_asr
    ]
    low_terms = [
        {
            "sample_id": row.get("sample_id"),
            "candidate_label": row.get("candidate_label"),
            "critical_term_recall": row.get("critical_term_recall"),
            "critical_terms_missing": row.get("critical_terms_missing"),
        }
        for row in rows
        if row.get("critical_term_recall") is not None and float(row["critical_term_recall"]) < min_terms
    ]
    join_failures: list[dict[str, Any]] = []
    for report in candidate_reports.values():
        payload = read_json(report)
        for row in payload.get("rows", []):
            jq = row.get("join_quality")
            if jq is not None and not bool(jq.get("pass")):
                join_failures.append(
                    {
                        "sample_id": row.get("sample_id"),
                        "variant": row.get("variant"),
                        "reasons": jq.get("reasons", []),
                        "path": row.get("path"),
                    }
                )
    fallback_samples = [
        {
            "sample_id": item.get("sample_id"),
            "choice": item.get("choice"),
            "chosen_similarity": item.get("chosen_similarity"),
            "chosen_abs_drift_ms": item.get("chosen_abs_drift_ms"),
            "no_selection": bool(item.get("no_selection")),
            "no_selection_reason": item.get("no_selection_reason"),
        }
        for item in gate.get("decisions", [])
        if item.get("fallback_used")
    ]
    no_selection_samples = [
        {
            "sample_id": item.get("sample_id"),
            "reason": item.get("no_selection_reason"),
            "candidate_rejections": [
                {
                    "label": candidate.get("label"),
                    "reject_reasons": candidate.get("reject_reasons", []),
                    "abs_drift_ms": candidate.get("abs_drift_ms"),
                    "similarity": candidate.get("similarity"),
                    "join_quality_pass": candidate.get("join_quality_pass"),
                    "join_quality_reasons": candidate.get("join_quality_reasons", []),
                }
                for candidate in item.get("candidates", [])
            ],
        }
        for item in gate.get("decisions", [])
        if item.get("no_selection")
    ]
    summary = {
        "features": args.features,
        "source_features": getattr(args, "original_features", args.features),
        "run_dir": str(run_dir),
        "skip_inference": bool(args.skip_inference),
        "candidate_reports": {label: str(path) for label, path in sorted(candidate_reports.items())},
        "selected_metrics": gate.get("summary", {}).get("selected", {}),
        "per_candidate_metrics": gate.get("summary", {}).get("candidates", {}),
        "fallback_samples": fallback_samples,
        "no_selection_samples": no_selection_samples,
        "low_asr_samples": low_asr,
        "low_critical_term_samples": low_terms,
        "join_quality_failures": join_failures,
        "missing_candidates": missing_candidates,
        "listening_pack": str(listening_pack),
        "commands": commands,
        "config": {
            "candidate_families": sorted(candidate_family_set(args)),
            "sample_ids": selected_sample_ids(args),
            "sample_ids_file": args.sample_ids_file,
            "resume": bool(args.resume),
            "spoken_text": {
                "policy": getattr(args, "spoken_text_policy", "none"),
                "features": getattr(args, "spoken_text_features", ""),
                "rewrite_count": getattr(args, "spoken_text_rewrite_count", 0),
                "rewritten_samples": getattr(args, "spoken_text_rewritten_samples", 0),
            },
            "generation": {
                "seed": args.seed,
                "temperature": args.temperature,
                "top_p": args.top_p,
                "top_k": args.top_k,
                "repetition_penalty": args.repetition_penalty,
                "length_penalty": args.length_penalty,
                "duration_token_rate_policy": args.duration_token_rate_policy,
            },
            "split": {
                "min_target_sec": args.split_min_target_sec,
                "min_text_chars": args.split_min_text_chars,
                "min_chars_per_sec": args.split_min_chars_per_sec,
                "max_part_sec": args.split_max_part_sec,
                "max_part_chars": args.split_max_part_chars,
                "max_part_chars_per_sec": args.split_max_part_chars_per_sec,
                "max_parts": args.split_max_parts,
                "pause_sec": args.split_pause_sec,
                "force_break_after_regex": args.split_force_break_after_regex,
            },
        },
    }
    write_json(run_dir / "loop_summary.json", summary)
    return summary


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def maybe_add_regression_hash(summary: dict[str, Any], run_dir: Path) -> None:
    row = None
    report = run_dir / "asr" / "split_mix.json"
    if report.exists():
        rows = read_json(report).get("rows", [])
        row = next((item for item in rows if item.get("sample_id") == "seg10686"), None)
    if not row or not row.get("path"):
        return
    path = Path(str(row["path"]))
    if not path.exists():
        return
    summary["seg10686_split_mix_regression"] = {
        "path": str(path),
        "sha256": sha256(path),
        "duration_sec": row.get("duration_sec"),
        "similarity": row.get("similarity"),
        "critical_term_recall": row.get("critical_term_recall"),
        "join_quality": row.get("join_quality"),
    }
    write_json(run_dir / "loop_summary.json", summary)


def main() -> int:
    args = parse_args()
    candidate_family_set(args)
    original_feature_path = Path(args.features)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    dirs = ensure_dirs(run_dir)
    commands: list[dict[str, Any]] = []
    ids = requested_sample_ids(args)

    prepare_spoken_features(args, dirs, commands)
    prepare_full_duration_features(args, dirs, commands)
    split_features, split_plan = run_split_plan(args, dirs, commands)

    missing_candidates: list[str] = []
    if args.skip_inference:
        existing_root = Path(args.existing_run_root) if args.existing_run_root else original_feature_path.parent
        combined_gate = reuse_combined_gate_artifacts(existing_root, run_dir, dirs, commands)
        if combined_gate is not None:
            candidate_reports: dict[str, Path] = {}
            listening_pack = run_listening_pack(args, combined_gate, candidate_reports, dirs, commands)
            summary = build_summary(
                args,
                run_dir,
                candidate_reports,
                combined_gate,
                listening_pack,
                commands,
                missing_candidates,
            )
            maybe_add_regression_hash(summary, run_dir)
            print(json.dumps({"summary": str(run_dir / "loop_summary.json"), "listening_pack": str(listening_pack)}, ensure_ascii=False))
            return 0
        candidate_reports, missing_candidates = copy_existing_reports(args, run_dir, dirs, existing_root, ids)
        aliases = build_skip_inference_split_reports(
            args,
            existing_root,
            split_plan,
            dirs,
            candidate_reports,
            commands,
        )
        rescore_reports(args, list(candidate_reports.values()), commands)
        prosody_source = existing_root / "canary24_prosody_report.json"
        prosody_report = duplicate_prosody_aliases(prosody_source, run_dir / "prosody_report.json", aliases)
    else:
        candidate_reports = generate_full_candidates(args, dirs, split_features, split_plan, commands)
        rescore_reports(args, list(candidate_reports.values()), commands)
        prosody_report = run_prosody(args, list(candidate_reports.values()), run_dir / "prosody_report.json", commands)

    gate_report = run_gate(args, candidate_reports, prosody_report, run_dir, dirs, commands)
    listening_pack = run_listening_pack(args, gate_report, candidate_reports, dirs, commands)
    summary = build_summary(
        args,
        run_dir,
        candidate_reports,
        gate_report,
        listening_pack,
        commands,
        missing_candidates,
    )
    maybe_add_regression_hash(summary, run_dir)
    print(json.dumps({"summary": str(run_dir / "loop_summary.json"), "listening_pack": str(listening_pack)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
