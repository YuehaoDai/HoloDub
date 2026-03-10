from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.models import SeparateRequest, SeparateResponse
from app.storage import ensure_parent, resolve_data_path


class MediaSeparatorAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def backend_name(self) -> str:
        return self.settings.ml_separator_backend

    def separate(self, request: SeparateRequest) -> SeparateResponse:
        if self.settings.ml_separator_backend == "demucs":
            return self._run_demucs(request)
        return self._run_ffmpeg_stub(request)

    def _run_ffmpeg_stub(self, request: SeparateRequest) -> SeparateResponse:
        input_path = resolve_data_path(self.settings.data_root, request.input_relpath)
        vocals_path = resolve_data_path(self.settings.data_root, request.vocals_output_relpath)
        bgm_path = resolve_data_path(self.settings.data_root, request.bgm_output_relpath)
        ensure_parent(vocals_path)
        ensure_parent(bgm_path)

        subprocess.run(
            [
                self.settings.ffmpeg_bin,
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-ar",
                str(self.settings.default_sample_rate),
                "-ac",
                str(self.settings.default_channels),
                str(vocals_path),
            ],
            check=True,
            capture_output=True,
        )

        duration = probe_duration(self.settings, input_path)
        subprocess.run(
            [
                self.settings.ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                f"anullsrc=r={self.settings.default_sample_rate}:cl=mono",
                str(bgm_path),
            ],
            check=True,
            capture_output=True,
        )

        return SeparateResponse(
            vocals_relpath=request.vocals_output_relpath,
            bgm_relpath=request.bgm_output_relpath,
            diagnostics=["separator backend=ffmpeg_stub"],
        )

    def _run_demucs(self, request: SeparateRequest) -> SeparateResponse:
        input_path = resolve_data_path(self.settings.data_root, request.input_relpath)
        vocals_path = resolve_data_path(self.settings.data_root, request.vocals_output_relpath)
        bgm_path = resolve_data_path(self.settings.data_root, request.bgm_output_relpath)
        ensure_parent(vocals_path)
        ensure_parent(bgm_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            command = [
                "python",
                "-m",
                "demucs",
                "--two-stems",
                "vocals",
                "--out",
                temp_dir,
                str(input_path),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except FileNotFoundError as exc:
                raise RuntimeError("demucs backend requested but demucs is not installed") from exc
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr.strip() or "demucs separation failed") from exc

            temp_root = Path(temp_dir)
            vocals_source = next(temp_root.rglob("vocals.wav"), None)
            bgm_source = next(temp_root.rglob("no_vocals.wav"), None)
            if vocals_source is None or bgm_source is None:
                raise RuntimeError("demucs output layout was not recognized")

            shutil.copy2(vocals_source, vocals_path)
            shutil.copy2(bgm_source, bgm_path)

        return SeparateResponse(
            vocals_relpath=request.vocals_output_relpath,
            bgm_relpath=request.bgm_output_relpath,
            diagnostics=["separator backend=demucs"],
        )


def probe_duration(settings: Settings, input_path: Path) -> float:
    result = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return max(float(result.stdout.strip()), 0.001)
