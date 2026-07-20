from __future__ import annotations

import subprocess
import logging
from pathlib import Path

from app.adapters.media import MediaSeparatorAdapter
from app.models import SeparateRequest


class _Settings:
    data_root: Path
    ffmpeg_bin = "ffmpeg"
    ml_separator_backend = "demucs"
    ml_demucs_segment_sec = 8.0
    ml_demucs_overlap = 0.1
    ml_demucs_jobs = 1
    ml_demucs_device = "cpu"
    ml_demucs_model = "mdx_q"
    ml_demucs_threads = 1
    ml_demucs_timeout_sec = 0.0
    default_sample_rate = 24000
    default_channels = 1

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root


def test_demucs_segment_arg_is_integer(tmp_path, monkeypatch):
    (tmp_path / "in.mkv").write_bytes(b"not really media")
    seen: dict[str, list[str]] = {}

    def fake_run(command, check, capture_output, text, env):  # noqa: ANN001
        seen["command"] = command
        seen["env"] = env
        out_dir = Path(command[command.index("--out") + 1])
        demucs_dir = out_dir / "htdemucs" / "in"
        demucs_dir.mkdir(parents=True)
        (demucs_dir / "vocals.wav").write_bytes(b"vocals")
        (demucs_dir / "no_vocals.wav").write_bytes(b"bgm")

    monkeypatch.setattr("subprocess.run", fake_run)

    response = MediaSeparatorAdapter(_Settings(tmp_path)).separate(
        SeparateRequest(
            input_relpath="in.mkv",
            vocals_output_relpath="out/vocals.wav",
            bgm_output_relpath="out/bgm.wav",
        )
    )

    command = seen["command"]
    assert command[command.index("-n") + 1] == "mdx_q"
    assert command[command.index("--device") + 1] == "cpu"
    assert command[command.index("--segment") + 1] == "7"
    assert command[command.index("--jobs") + 1] == "1"
    assert seen["env"]["OMP_NUM_THREADS"] == "1"
    assert seen["env"]["TORCH_NUM_THREADS"] == "1"
    assert response.vocals_relpath == "out/vocals.wav"
    assert (tmp_path / "out" / "vocals.wav").read_bytes() == b"vocals"


def test_demucs_timeout_accepts_completed_stems(tmp_path, monkeypatch):
    (tmp_path / "in.wav").write_bytes(b"not really media")

    class TimeoutSettings(_Settings):
        ml_demucs_timeout_sec = 1.0

    def fake_run(command, **kwargs):  # noqa: ANN001
        assert kwargs["timeout"] == 1.0
        out_dir = Path(command[command.index("--out") + 1])
        demucs_dir = out_dir / "mdx_q" / "in"
        demucs_dir.mkdir(parents=True)
        (demucs_dir / "vocals.wav").write_bytes(b"vocals")
        (demucs_dir / "no_vocals.wav").write_bytes(b"bgm")
        raise subprocess.TimeoutExpired(command, timeout=1.0, stderr=b"stuck")

    monkeypatch.setattr("subprocess.run", fake_run)

    response = MediaSeparatorAdapter(TimeoutSettings(tmp_path)).separate(
        SeparateRequest(
            input_relpath="in.wav",
            vocals_output_relpath="out/vocals.wav",
            bgm_output_relpath="out/bgm.wav",
        )
    )

    assert response.vocals_relpath == "out/vocals.wav"
    assert (tmp_path / "out" / "vocals.wav").read_bytes() == b"vocals"
    assert (tmp_path / "out" / "bgm.wav").read_bytes() == b"bgm"


def test_demucs_chunk_logs_total_progress(tmp_path, monkeypatch, caplog):
    adapter = MediaSeparatorAdapter(_Settings(tmp_path))
    input_path = tmp_path / "in.wav"
    input_path.write_bytes(b"input")
    vocals_path = tmp_path / "out" / "vocals.wav"
    bgm_path = tmp_path / "out" / "bgm.wav"
    temp_root = tmp_path / "chunks"
    temp_root.mkdir()

    def fake_run(command, **kwargs):  # noqa: ANN001
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"chunk")

    def fake_demucs_command(chunk_input, chunk_out, segment_sec):  # noqa: ANN001
        chunk_out.mkdir(parents=True, exist_ok=True)
        vocals = chunk_out / "vocals.wav"
        bgm = chunk_out / "no_vocals.wav"
        vocals.write_bytes(b"vocals")
        bgm.write_bytes(b"bgm")
        return vocals, bgm

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(adapter, "_run_demucs_command", fake_demucs_command)

    with caplog.at_level(logging.INFO, logger="app.adapters.media"):
        adapter._run_demucs_chunked(
            input_path,
            vocals_path,
            bgm_path,
            temp_root,
            duration=31.0,
            chunk_sec=10.0,
            segment_sec=7,
        )

    start_records = [record for record in caplog.records if record.message == "demucs chunk starting"]
    completed_records = [record for record in caplog.records if record.message == "demucs chunk completed"]
    assert [record.chunk_total for record in start_records] == [4, 4, 4, 4]
    assert completed_records[-1].progress_pct == 100.0
