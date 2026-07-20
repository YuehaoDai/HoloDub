from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.models import SeparateRequest, SeparateResponse
from app.storage import ensure_parent, resolve_data_path

logger = logging.getLogger(__name__)


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

        # demucs 4.x declares --segment as int, while htdemucs' transformer
        # model refuses values above 7.8s. Clamp to 7s even if an operator sets
        # a larger value in the environment.
        segment_sec = max(1, min(7, int(self.settings.ml_demucs_segment_sec)))
        with tempfile.TemporaryDirectory() as temp_dir:
            chunk_sec = max(0.0, float(getattr(self.settings, "ml_demucs_chunk_sec", 0.0)))
            duration = probe_duration(self.settings, input_path) if chunk_sec > 0 else 0.0
            if chunk_sec > 0 and duration > chunk_sec:
                self._run_demucs_chunked(input_path, vocals_path, bgm_path, Path(temp_dir), duration, chunk_sec, segment_sec)
            else:
                vocals_source, bgm_source = self._run_demucs_command(input_path, Path(temp_dir), segment_sec)
                shutil.copy2(vocals_source, vocals_path)
                shutil.copy2(bgm_source, bgm_path)

        return SeparateResponse(
            vocals_relpath=request.vocals_output_relpath,
            bgm_relpath=request.bgm_output_relpath,
            diagnostics=["separator backend=demucs"],
        )

    def _run_demucs_command(
        self, input_path: Path, out_dir: Path, segment_sec: int
    ) -> tuple[Path, Path]:
        device = str(getattr(self.settings, "ml_demucs_device", "auto")).strip().lower()
        model_name = str(getattr(self.settings, "ml_demucs_model", "htdemucs")).strip()
        command = [
            "python",
            "-m",
            "demucs",
            "-n",
            model_name or "htdemucs",
            "--two-stems",
            "vocals",
            "--segment",
            str(segment_sec),
            "--overlap",
            str(self.settings.ml_demucs_overlap),
            "--jobs",
            str(self.settings.ml_demucs_jobs),
            "--out",
            str(out_dir),
            str(input_path),
        ]
        if device and device != "auto":
            command[7:7] = ["--device", device]
        threads = max(1, int(getattr(self.settings, "ml_demucs_threads", 1)))
        env = os.environ.copy()
        env.update(
            {
                "OMP_NUM_THREADS": str(threads),
                "MKL_NUM_THREADS": str(threads),
                "OPENBLAS_NUM_THREADS": str(threads),
                "NUMEXPR_NUM_THREADS": str(threads),
                "VECLIB_MAXIMUM_THREADS": str(threads),
                "TORCH_NUM_THREADS": str(threads),
            }
        )
        timeout_sec = max(0.0, float(getattr(self.settings, "ml_demucs_timeout_sec", 0.0)))
        run_kwargs = {
            "check": True,
            "capture_output": True,
            "text": True,
            "env": env,
        }
        if timeout_sec > 0:
            run_kwargs["timeout"] = timeout_sec
        try:
            subprocess.run(command, **run_kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError("demucs backend requested but demucs is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            vocals_source, bgm_source = self._find_demucs_outputs(out_dir)
            if vocals_source is not None and bgm_source is not None:
                logger.warning(
                    "demucs timed out after writing stems; accepting completed outputs",
                    extra={
                        "timeout_sec": timeout_sec,
                        "input_path": str(input_path),
                        "out_dir": str(out_dir),
                    },
                )
                return vocals_source, bgm_source
            stderr = _decode_subprocess_output(exc.stderr)
            raise RuntimeError(
                stderr or f"demucs separation timed out after {timeout_sec:.1f}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr.strip() or "demucs separation failed") from exc

        vocals_source, bgm_source = self._find_demucs_outputs(out_dir)
        if vocals_source is None or bgm_source is None:
            raise RuntimeError("demucs output layout was not recognized")
        return vocals_source, bgm_source

    def _find_demucs_outputs(self, out_dir: Path) -> tuple[Path | None, Path | None]:
        vocals_source = next(out_dir.rglob("vocals.wav"), None)
        bgm_source = next(out_dir.rglob("no_vocals.wav"), None)
        if vocals_source is not None and vocals_source.stat().st_size <= 0:
            vocals_source = None
        if bgm_source is not None and bgm_source.stat().st_size <= 0:
            bgm_source = None
        return vocals_source, bgm_source

    def _run_demucs_chunked(
        self,
        input_path: Path,
        vocals_path: Path,
        bgm_path: Path,
        temp_root: Path,
        duration: float,
        chunk_sec: float,
        segment_sec: int,
    ) -> None:
        vocals_parts: list[Path] = []
        bgm_parts: list[Path] = []
        start = 0.0
        index = 0
        chunk_total = max(1, math.ceil(duration / chunk_sec))
        while start < duration:
            length = min(chunk_sec, duration - start)
            chunk_input = temp_root / f"chunk_{index:04d}.wav"
            progress_pct = round((index / chunk_total) * 100.0, 1)
            logger.info(
                "demucs chunk starting",
                extra={
                    "chunk_index": index,
                    "chunk_total": chunk_total,
                    "progress_pct": progress_pct,
                    "start_sec": start,
                    "duration_sec": length,
                },
            )
            subprocess.run(
                [
                    self.settings.ffmpeg_bin,
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{length:.3f}",
                    "-i",
                    str(input_path),
                    "-vn",
                    "-ar",
                    str(self.settings.default_sample_rate),
                    "-ac",
                    str(self.settings.default_channels),
                    str(chunk_input),
                ],
                check=True,
                capture_output=True,
            )
            chunk_out = temp_root / f"demucs_{index:04d}"
            vocals_source, bgm_source = self._run_demucs_command(
                chunk_input, chunk_out, segment_sec
            )
            vocals_parts.append(vocals_source)
            bgm_parts.append(bgm_source)
            logger.info(
                "demucs chunk completed",
                extra={
                    "chunk_index": index,
                    "chunk_total": chunk_total,
                    "progress_pct": round(((index + 1) / chunk_total) * 100.0, 1),
                },
            )
            start += length
            index += 1

        logger.info("demucs chunk concat starting", extra={"chunk_count": len(vocals_parts)})
        self._concat_audio_parts(vocals_parts, vocals_path, temp_root / "vocals_concat.txt")
        self._concat_audio_parts(bgm_parts, bgm_path, temp_root / "bgm_concat.txt")
        logger.info("demucs chunk concat completed", extra={"chunk_count": len(vocals_parts)})

    def _concat_audio_parts(self, parts: list[Path], output_path: Path, list_path: Path) -> None:
        if not parts:
            raise RuntimeError("demucs chunking produced no parts")
        list_body = "".join(f"file '{_ffmpeg_concat_path(part)}'\n" for part in parts)
        list_path.write_text(list_body, encoding="utf-8")
        subprocess.run(
            [
                self.settings.ffmpeg_bin,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ],
            check=True,
            capture_output=True,
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


def _ffmpeg_concat_path(path: Path) -> str:
    return path.as_posix().replace("'", r"'\''")


def _decode_subprocess_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace").strip()
    return output.strip()
