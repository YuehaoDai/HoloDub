from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import Settings
from app.models import (
    LoraCanaryRunRequest,
    LoraCanaryRunResponse,
    LoraRegressionRunRequest,
    LoraRegressionRunResponse,
    LoraTrainingRunRequest,
    LoraTrainingRunResponse,
)

SubprocessRun = Callable[..., subprocess.CompletedProcess[str]]


class LoraTrainingRunner:
    def __init__(
        self,
        settings: Settings,
        script_path: Path | None = None,
        subprocess_run: SubprocessRun | None = None,
    ) -> None:
        self.settings = settings
        self.script_path = script_path or self._find_training_script()
        self.tools_root = self._find_tools_root()
        self.subprocess_run = subprocess_run or subprocess.run

    def run_smoke(self, request: LoraTrainingRunRequest) -> LoraTrainingRunResponse:
        if not self.settings.ml_lora_training_smoke_enabled:
            raise LoraTrainingDisabledError("ml_lora_training_smoke_disabled")
        max_steps = min(
            max(1, request.max_steps),
            max(1, self.settings.ml_lora_training_max_smoke_steps),
        )
        return self._run_training(request, max_steps=max_steps, smoke_run=True)

    def run_full(self, request: LoraTrainingRunRequest) -> LoraTrainingRunResponse:
        if not self.settings.ml_lora_training_full_enabled:
            raise LoraTrainingDisabledError("ml_lora_training_full_disabled")
        max_steps = max(1, request.max_steps)
        return self._run_training(request, max_steps=max_steps, smoke_run=False)

    def run_canary(self, request: LoraCanaryRunRequest) -> LoraCanaryRunResponse:
        if not self.settings.ml_lora_training_validation_enabled:
            raise LoraTrainingDisabledError("ml_lora_training_validation_disabled")
        features_relpath = _clean_relpath(request.features_relpath, "features_relpath")
        run_dir_relpath = _clean_relpath(request.run_dir_relpath, "run_dir_relpath")
        adapter_relpath = _clean_relpath(request.adapter_relpath, "adapter_relpath")
        existing_run_root_relpath = (
            _clean_relpath(request.existing_run_root_relpath, "existing_run_root_relpath")
            if request.existing_run_root_relpath
            else ""
        )
        voice_prompt_relpath = _clean_relpath(request.voice_prompt_relpath, "voice_prompt_relpath")
        run_dir = self.settings.data_root / run_dir_relpath
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_log_relpath = _join_relpath(run_dir_relpath, "canary.out.log")
        stderr_log_relpath = _join_relpath(run_dir_relpath, "canary.err.log")

        command = [
            sys.executable or "python",
            str(self.tools_root / "lora_canary_candidate_loop.py"),
            "--features",
            str(self.settings.data_root / features_relpath),
            "--run-dir",
            str(self.settings.data_root / run_dir_relpath),
            "--adapter",
            str(self.settings.data_root / adapter_relpath),
            "--data-root",
            str(self.settings.data_root),
            "--host-data-root",
            str(self.settings.data_root),
            "--voice-prompt-relpath",
            voice_prompt_relpath,
            "--candidate-families",
            request.candidate_families,
            "--spoken-text-policy",
            request.spoken_text_policy,
            "--duration-token-rate-policy",
            request.duration_token_rate_policy,
            "--max-fit-delta",
            str(request.max_fit_delta),
            "--max-drift-ms",
            str(request.max_drift_ms),
            "--min-similarity",
            str(request.min_similarity),
            "--target-similarity",
            str(request.target_similarity),
            "--min-f0-range",
            str(request.min_f0_range),
            "--target-f0-range",
            str(request.target_f0_range),
            "--no-docker",
        ]
        if request.skip_inference:
            command.append("--skip-inference")
            if existing_run_root_relpath:
                command.extend(["--existing-run-root", str(self.settings.data_root / existing_run_root_relpath)])
        if request.resume:
            command.append("--resume")
        if request.sample_ids.strip():
            command.extend(["--sample-ids", request.sample_ids.strip()])

        exit_code, diagnostics = self._run_subprocess(
            command,
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            mode="canary",
        )
        status = "completed" if exit_code == 0 else "failed"
        return LoraCanaryRunResponse(
            status=status,
            exit_code=exit_code,
            run_dir_relpath=run_dir_relpath,
            loop_summary_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "loop_summary.json"),
            candidate_gate_report_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "candidate_gate_report.json"),
            listening_pack_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "listening_pack/index.html"),
            quality_baseline_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "quality_baseline.json"),
            human_review_report_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "human_review_report.json"),
            risk_queue_relpath=_existing_relpath(self.settings.data_root, run_dir_relpath, "risk_queue.json"),
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            command=command,
            diagnostics=diagnostics,
            metrics=_load_canary_metrics(self.settings.data_root, run_dir_relpath),
        )

    def run_regression(self, request: LoraRegressionRunRequest) -> LoraRegressionRunResponse:
        if not self.settings.ml_lora_training_validation_enabled:
            raise LoraTrainingDisabledError("ml_lora_training_validation_disabled")
        summary_relpath = _clean_relpath(request.summary_relpath, "summary_relpath")
        gate_report_relpath = _clean_relpath(request.gate_report_relpath, "gate_report_relpath")
        report_relpath = _clean_relpath(request.report_relpath, "report_relpath")
        risk_queue_relpath = (
            _clean_relpath(request.risk_queue_relpath, "risk_queue_relpath")
            if request.risk_queue_relpath
            else ""
        )
        report_path = self.settings.data_root / report_relpath
        report_path.parent.mkdir(parents=True, exist_ok=True)
        log_dir_relpath = str(PurePosixPath(report_relpath).parent)
        stdout_log_relpath = _join_relpath(log_dir_relpath, "regression.out.log")
        stderr_log_relpath = _join_relpath(log_dir_relpath, "regression.err.log")
        command = [
            sys.executable or "python",
            str(self.tools_root / "lora_canary_regression_check.py"),
            "--summary",
            str(self.settings.data_root / summary_relpath),
            "--gate-report",
            str(self.settings.data_root / gate_report_relpath),
            "--min-count",
            str(request.min_count),
            "--max-mean-drift-ms",
            str(request.max_mean_drift_ms),
            "--max-max-drift-ms",
            str(request.max_max_drift_ms),
            "--min-mean-similarity",
            str(request.min_mean_similarity),
            "--min-min-similarity",
            str(request.min_min_similarity),
            "--max-join-quality-failures",
            str(request.max_join_quality_failures),
        ]
        if risk_queue_relpath:
            command.extend(["--risk-queue", str(self.settings.data_root / risk_queue_relpath)])
        for value in request.expect_chain:
            command.extend(["--expect-chain", value])
        for value in request.forbid_chain:
            command.extend(["--forbid-chain", value])
        for value in request.expect_risk_sample:
            command.extend(["--expect-risk-sample", value])

        exit_code, diagnostics = self._run_subprocess(
            command,
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            mode="regression",
        )
        status = "completed" if exit_code == 0 else "failed"
        metrics = _load_summary_metrics(self.settings.data_root, summary_relpath)
        report_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "exit_code": exit_code,
                    "command": command,
                    "diagnostics": diagnostics,
                    "metrics": metrics,
                    "stdout_log_relpath": stdout_log_relpath,
                    "stderr_log_relpath": stderr_log_relpath,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return LoraRegressionRunResponse(
            status=status,
            exit_code=exit_code,
            report_relpath=report_relpath,
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            command=command,
            diagnostics=diagnostics,
            metrics=metrics,
        )

    def _run_training(
        self,
        request: LoraTrainingRunRequest,
        *,
        max_steps: int,
        smoke_run: bool,
    ) -> LoraTrainingRunResponse:
        output_dir_relpath = _clean_relpath(request.output_dir_relpath, "output_dir_relpath")
        train_features_relpath = _clean_relpath(
            request.train_features_relpath, "train_features_relpath"
        )
        val_features_relpath = _clean_relpath(request.val_features_relpath, "val_features_relpath")

        output_dir = self.settings.data_root / output_dir_relpath
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_log_relpath = _join_relpath(output_dir_relpath, "train.out.log")
        stderr_log_relpath = _join_relpath(output_dir_relpath, "train.err.log")
        stdout_log = self.settings.data_root / stdout_log_relpath
        stderr_log = self.settings.data_root / stderr_log_relpath

        command = self._build_command(
            request,
            train_features_relpath=train_features_relpath,
            val_features_relpath=val_features_relpath,
            output_dir_relpath=output_dir_relpath,
            max_steps=max_steps,
        )
        diagnostics = [
            "execution_mode=ml_service_subprocess",
            f"smoke_run={str(smoke_run).lower()}",
            f"full_run={str(not smoke_run).lower()}",
            f"max_steps={max_steps}",
        ]

        with stdout_log.open("w", encoding="utf-8", newline="\n") as stdout_handle, stderr_log.open(
            "w", encoding="utf-8", newline="\n"
        ) as stderr_handle:
            try:
                completed = self.subprocess_run(
                    command,
                    cwd=str(self.script_path.parents[1]),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    check=False,
                )
                exit_code = int(completed.returncode)
            except Exception as exc:
                stderr_handle.write(f"\n[runner] failed to launch training: {exc}\n")
                return self._response(
                    status="failed",
                    exit_code=None,
                    output_dir_relpath=output_dir_relpath,
                    stdout_log_relpath=stdout_log_relpath,
                    stderr_log_relpath=stderr_log_relpath,
                    command=command,
                    diagnostics=[*diagnostics, f"launch_error={exc}"],
                )

        status = "completed" if exit_code == 0 else "failed"
        adapter_relpath = _join_relpath(output_dir_relpath, "adapter_latest.pt")
        summary_relpath = _join_relpath(output_dir_relpath, "summary.json")
        metrics_relpath = _join_relpath(output_dir_relpath, "metrics.jsonl")
        if status == "completed":
            diagnostics.append("adapter_written=true")
        else:
            diagnostics.append(f"process_exit_code={exit_code}")
        return self._response(
            status=status,
            exit_code=exit_code,
            output_dir_relpath=output_dir_relpath,
            adapter_relpath=adapter_relpath if (self.settings.data_root / adapter_relpath).exists() else None,
            summary_relpath=summary_relpath if (self.settings.data_root / summary_relpath).exists() else None,
            metrics_relpath=metrics_relpath if (self.settings.data_root / metrics_relpath).exists() else None,
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            command=command,
            diagnostics=diagnostics,
        )

    def _build_command(
        self,
        request: LoraTrainingRunRequest,
        *,
        train_features_relpath: str,
        val_features_relpath: str,
        output_dir_relpath: str,
        max_steps: int,
    ) -> list[str]:
        if request.duration_source not in ("audio", "target"):
            raise ValueError("duration_source must be audio or target")
        if request.precision not in ("fp16", "fp32"):
            raise ValueError("precision must be fp16 or fp32")
        command = [
            sys.executable or "python",
            str(self.script_path),
            "--train-features",
            str(self.settings.data_root / train_features_relpath),
            "--val-features",
            str(self.settings.data_root / val_features_relpath),
            "--output-dir",
            str(self.settings.data_root / output_dir_relpath),
            "--rank",
            str(request.rank),
            "--alpha",
            str(request.alpha),
            "--dropout",
            str(request.dropout),
            "--lr",
            str(request.learning_rate),
            "--max-steps",
            str(max_steps),
            "--save-every",
            str(min(max(1, request.save_every), max_steps)),
            "--eval-interval",
            str(min(max(1, request.eval_interval), max_steps)),
            "--text-loss-weight",
            str(request.text_loss_weight),
            "--mel-loss-weight",
            str(request.mel_loss_weight),
            "--duration-source",
            request.duration_source,
            "--duration-dropout",
            str(request.duration_dropout),
            "--mel-stop-loss-weight",
            str(request.mel_stop_loss_weight),
            "--mel-end-loss-weight",
            str(request.mel_end_loss_weight),
            "--mel-end-window",
            str(request.mel_end_window),
            "--duration-stop-bce-weight",
            str(request.duration_stop_bce_weight),
            "--duration-position-loss-weight",
            str(request.duration_position_loss_weight),
            "--duration-position-loss-window",
            str(request.duration_position_loss_window),
        ]
        if request.duration_control:
            command.append("--duration-control")
        if request.gradient_checkpointing:
            command.append("--gradient-checkpointing")
        if request.precision == "fp32":
            command.append("--no-fp16")
        return command

    def _response(
        self,
        *,
        status: str,
        exit_code: int | None,
        output_dir_relpath: str,
        stdout_log_relpath: str,
        stderr_log_relpath: str,
        command: list[str],
        diagnostics: list[str],
        adapter_relpath: str | None = None,
        summary_relpath: str | None = None,
        metrics_relpath: str | None = None,
    ) -> LoraTrainingRunResponse:
        return LoraTrainingRunResponse(
            status=status,
            exit_code=exit_code,
            output_dir_relpath=output_dir_relpath,
            adapter_relpath=adapter_relpath,
            summary_relpath=summary_relpath,
            metrics_relpath=metrics_relpath,
            stdout_log_relpath=stdout_log_relpath,
            stderr_log_relpath=stderr_log_relpath,
            command=command,
            diagnostics=diagnostics,
        )

    def _run_subprocess(
        self,
        command: list[str],
        *,
        stdout_log_relpath: str,
        stderr_log_relpath: str,
        mode: str,
    ) -> tuple[int | None, list[str]]:
        stdout_log = self.settings.data_root / stdout_log_relpath
        stderr_log = self.settings.data_root / stderr_log_relpath
        stdout_log.parent.mkdir(parents=True, exist_ok=True)
        diagnostics = ["execution_mode=ml_service_subprocess", f"runner_mode={mode}"]
        with stdout_log.open("w", encoding="utf-8", newline="\n") as stdout_handle, stderr_log.open(
            "w", encoding="utf-8", newline="\n"
        ) as stderr_handle:
            try:
                completed = self.subprocess_run(
                    command,
                    cwd=str(self.tools_root.parent),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    check=False,
                )
                exit_code = int(completed.returncode)
            except Exception as exc:
                stderr_handle.write(f"\n[runner] failed to launch {mode}: {exc}\n")
                return None, [*diagnostics, f"launch_error={exc}"]
        if exit_code != 0:
            diagnostics.append(f"process_exit_code={exit_code}")
        return exit_code, diagnostics

    def _find_training_script(self) -> Path:
        candidates = [
            Path("/app/tools/lora_train_indextts2_smoke.py"),
            Path(__file__).resolve().parents[3] / "tools" / "lora_train_indextts2_smoke.py",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError("lora_train_indextts2_smoke.py not found")

    def _find_tools_root(self) -> Path:
        candidates = [
            Path("/app/tools"),
            Path(__file__).resolve().parents[3] / "tools",
        ]
        for candidate in candidates:
            if (candidate / "lora_canary_candidate_loop.py").exists():
                return candidate
        raise FileNotFoundError("LoRA tools directory not found")


class LoraTrainingDisabledError(RuntimeError):
    pass


def _clean_relpath(value: str, field: str) -> str:
    cleaned = str(value or "").replace("\\", "/").strip()
    cleaned = cleaned.removeprefix("/data/").removeprefix("data/").strip("/")
    if not cleaned:
        raise ValueError(f"{field} is required")
    path = PurePosixPath(cleaned)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"{field} must be a safe DATA_ROOT-relative path")
    return str(path)


def _join_relpath(*parts: str) -> str:
    return str(PurePosixPath(*[part.strip("/") for part in parts if part]))


def _existing_relpath(data_root: Path, *parts: str) -> str | None:
    relpath = _join_relpath(*parts)
    return relpath if (data_root / relpath).exists() else None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _summary_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    selected = payload.get("selected_metrics")
    if isinstance(selected, dict):
        return dict(selected)
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("selected"), dict):
        return dict(summary["selected"])
    return {}


def _count_list(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _load_canary_metrics(data_root: Path, run_dir_relpath: str) -> dict[str, Any]:
    summary = _read_json(data_root / run_dir_relpath / "loop_summary.json")
    metrics = _summary_metrics(summary)
    if not metrics:
        gate = _read_json(data_root / run_dir_relpath / "candidate_gate_report.json")
        metrics = _summary_metrics(gate)
    if summary:
        metrics.update(
            {
                "fallback_samples_count": _count_list(summary, "fallback_samples"),
                "low_asr_samples_count": _count_list(summary, "low_asr_samples"),
                "low_critical_term_samples_count": _count_list(summary, "low_critical_term_samples"),
                "join_quality_failures_count": _count_list(summary, "join_quality_failures"),
            }
        )
    return metrics


def _load_summary_metrics(data_root: Path, summary_relpath: str) -> dict[str, Any]:
    return _summary_metrics(_read_json(data_root / summary_relpath))
