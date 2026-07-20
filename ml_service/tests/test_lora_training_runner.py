from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import LoraCanaryRunRequest, LoraRegressionRunRequest, LoraTrainingRunRequest
from app.services.lora_training import LoraTrainingRunner
from app.runtime import LazyLoraTrainingRunner


def _settings(data_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        data_root=data_root,
        ml_lora_training_smoke_enabled=True,
        ml_lora_training_max_smoke_steps=1,
        ml_lora_training_full_enabled=True,
        ml_lora_training_validation_enabled=True,
    )


def _request() -> LoraTrainingRunRequest:
    return LoraTrainingRunRequest(
        train_features_relpath="training/features/train.jsonl",
        val_features_relpath="training/features/val.jsonl",
        output_dir_relpath="training/lora_runs/run-a/_smoke_step1",
        max_steps=120,
        save_every=20,
        eval_interval=10,
        duration_control=True,
        duration_source="target",
        duration_dropout=0.1,
        mel_stop_loss_weight=2.0,
        mel_end_loss_weight=1.5,
        mel_end_window=64,
        duration_position_loss_weight=0.2,
        duration_position_loss_window=32,
    )


def test_lora_training_smoke_caps_steps_and_records_artifacts(tmp_path: Path):
    script = tmp_path / "tools" / "lora_train_indextts2_smoke.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fake')\n", encoding="utf-8")
    data_root = tmp_path / "data"

    def fake_run(command, cwd, stdout, stderr, text, check):  # noqa: ANN001
        assert "--max-steps" in command
        assert command[command.index("--max-steps") + 1] == "1"
        assert command[command.index("--save-every") + 1] == "1"
        assert command[command.index("--eval-interval") + 1] == "1"
        assert command[command.index("--duration-source") + 1] == "target"
        assert command[command.index("--duration-dropout") + 1] == "0.1"
        assert command[command.index("--mel-stop-loss-weight") + 1] == "2.0"
        assert command[command.index("--mel-end-loss-weight") + 1] == "1.5"
        assert command[command.index("--mel-end-window") + 1] == "64"
        assert command[command.index("--duration-position-loss-weight") + 1] == "0.2"
        assert command[command.index("--duration-position-loss-window") + 1] == "32"
        stdout.write("[fake] ok\n")
        output_dir = data_root / "training/lora_runs/run-a/_smoke_step1"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "adapter_latest.pt").write_bytes(b"adapter")
        (output_dir / "summary.json").write_text("{}", encoding="utf-8")
        (output_dir / "metrics.jsonl").write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    runner = LoraTrainingRunner(
        _settings(data_root), script_path=script, subprocess_run=fake_run
    )

    response = runner.run_smoke(_request())

    assert response.status == "completed"
    assert response.exit_code == 0
    assert response.adapter_relpath == "training/lora_runs/run-a/_smoke_step1/adapter_latest.pt"
    assert response.summary_relpath == "training/lora_runs/run-a/_smoke_step1/summary.json"
    assert response.metrics_relpath == "training/lora_runs/run-a/_smoke_step1/metrics.jsonl"
    assert response.stdout_log_relpath == "training/lora_runs/run-a/_smoke_step1/train.out.log"
    assert response.stderr_log_relpath == "training/lora_runs/run-a/_smoke_step1/train.err.log"


def test_lora_training_full_keeps_requested_cadence_and_official_output(tmp_path: Path):
    script = tmp_path / "tools" / "lora_train_indextts2_smoke.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fake')\n", encoding="utf-8")
    data_root = tmp_path / "data"
    request = _request()
    request.output_dir_relpath = "training/lora_runs/run-a"

    def fake_run(command, cwd, stdout, stderr, text, check):  # noqa: ANN001
        assert command[command.index("--max-steps") + 1] == "120"
        assert command[command.index("--save-every") + 1] == "20"
        assert command[command.index("--eval-interval") + 1] == "10"
        output_dir = data_root / "training/lora_runs/run-a"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "adapter_latest.pt").write_bytes(b"adapter")
        return subprocess.CompletedProcess(command, 0)

    runner = LoraTrainingRunner(
        _settings(data_root), script_path=script, subprocess_run=fake_run
    )

    response = runner.run_full(request)

    assert response.status == "completed"
    assert response.output_dir_relpath == "training/lora_runs/run-a"
    assert response.adapter_relpath == "training/lora_runs/run-a/adapter_latest.pt"
    assert "full_run=true" in response.diagnostics


def test_lora_training_smoke_rejects_unsafe_paths(tmp_path: Path):
    script = tmp_path / "tools" / "lora_train_indextts2_smoke.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fake')\n", encoding="utf-8")
    request = _request()
    request.output_dir_relpath = "../bad"
    runner = LoraTrainingRunner(_settings(tmp_path / "data"), script_path=script)

    with pytest.raises(ValueError, match="safe DATA_ROOT-relative"):
        runner.run_smoke(request)


def test_lazy_lora_training_runner_defers_missing_tools_until_use(tmp_path: Path, monkeypatch):
    class MissingRunner:
        def __init__(self, settings):  # noqa: ANN001
            raise FileNotFoundError("LoRA tools directory not found")

    monkeypatch.setattr("app.runtime.LoraTrainingRunner", MissingRunner)
    runner = LazyLoraTrainingRunner(_settings(tmp_path / "data"))

    with pytest.raises(FileNotFoundError, match="LoRA tools directory not found"):
        runner.run_smoke(_request())


def test_lora_canary_runner_uses_no_docker_and_returns_review_artifacts(tmp_path: Path):
    script = tmp_path / "tools" / "lora_train_indextts2_smoke.py"
    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script.write_text("print('fake')\n", encoding="utf-8")
    (tools / "lora_canary_candidate_loop.py").write_text("print('fake')\n", encoding="utf-8")
    (tools / "lora_canary_regression_check.py").write_text("print('fake')\n", encoding="utf-8")
    data_root = tmp_path / "data"

    def fake_run(command, cwd, stdout, stderr, text, check):  # noqa: ANN001
        assert "lora_canary_candidate_loop.py" in command[1]
        assert "--no-docker" in command
        assert command[command.index("--candidate-families") + 1] == "flat,split_light_vector,split_mix"
        assert "--skip-inference" in command
        existing_root = command[command.index("--existing-run-root") + 1].replace("\\", "/")
        assert existing_root.endswith(
            "training/lora_runs/run-a/canary84_full84_regen_v1"
        )
        run_dir = data_root / "training/lora_runs/run-a/canary84_full84_regen_v1"
        (run_dir / "listening_pack").mkdir(parents=True)
        (run_dir / "loop_summary.json").write_text(
            '{"selected_metrics":{"count":84,"mean_similarity":0.993,"join_quality_fail_count":0},"low_asr_samples":[{"sample_id":"seg1"}]}',
            encoding="utf-8",
        )
        (run_dir / "candidate_gate_report.json").write_text("{}", encoding="utf-8")
        (run_dir / "listening_pack/index.html").write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    runner = LoraTrainingRunner(
        _settings(data_root), script_path=script, subprocess_run=fake_run
    )
    runner.tools_root = tools

    response = runner.run_canary(
        LoraCanaryRunRequest(
            features_relpath="training/features/canary.jsonl",
            run_dir_relpath="training/lora_runs/run-a/canary84_full84_regen_v1",
            adapter_relpath="training/lora_runs/run-a/adapter_latest.pt",
            existing_run_root_relpath="training/lora_runs/run-a/canary84_full84_regen_v1",
            skip_inference=True,
        )
    )

    assert response.status == "completed"
    assert response.loop_summary_relpath == "training/lora_runs/run-a/canary84_full84_regen_v1/loop_summary.json"
    assert response.candidate_gate_report_relpath == "training/lora_runs/run-a/canary84_full84_regen_v1/candidate_gate_report.json"
    assert response.listening_pack_relpath == "training/lora_runs/run-a/canary84_full84_regen_v1/listening_pack/index.html"
    assert response.metrics["count"] == 84
    assert response.metrics["mean_similarity"] == 0.993
    assert response.metrics["low_asr_samples_count"] == 1


def test_lora_regression_runner_writes_report(tmp_path: Path):
    script = tmp_path / "tools" / "lora_train_indextts2_smoke.py"
    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script.write_text("print('fake')\n", encoding="utf-8")
    (tools / "lora_canary_candidate_loop.py").write_text("print('fake')\n", encoding="utf-8")
    (tools / "lora_canary_regression_check.py").write_text("print('fake')\n", encoding="utf-8")
    data_root = tmp_path / "data"

    def fake_run(command, cwd, stdout, stderr, text, check):  # noqa: ANN001
        assert "lora_canary_regression_check.py" in command[1]
        assert command[command.index("--min-count") + 1] == "84"
        stdout.write('{"ok": true}\n')
        return subprocess.CompletedProcess(command, 0)

    runner = LoraTrainingRunner(
        _settings(data_root), script_path=script, subprocess_run=fake_run
    )
    runner.tools_root = tools
    summary = data_root / "training/lora_runs/run-a/canary84_full84_regen_v1/loop_summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(
        '{"selected_metrics":{"count":84,"mean_abs_drift_ms":39.1,"min_similarity":0.9625}}',
        encoding="utf-8",
    )

    response = runner.run_regression(
        LoraRegressionRunRequest(
            summary_relpath="training/lora_runs/run-a/canary84_full84_regen_v1/loop_summary.json",
            gate_report_relpath="training/lora_runs/run-a/canary84_full84_regen_v1/candidate_gate_report.json",
            risk_queue_relpath="training/lora_runs/run-a/canary84_full84_regen_v1/risk_queue.json",
            report_relpath="training/lora_runs/run-a/canary84_full84_regen_v1/regression_report.json",
        )
    )

    assert response.status == "completed"
    assert response.metrics["mean_abs_drift_ms"] == 39.1
    report = data_root / "training/lora_runs/run-a/canary84_full84_regen_v1/regression_report.json"
    assert report.exists()
    report_text = report.read_text(encoding="utf-8")
    assert '"status": "completed"' in report_text
    assert '"min_similarity": 0.9625' in report_text
