from __future__ import annotations

import asyncio
import base64
import json
import shlex
import struct
import subprocess
import wave
from pathlib import Path

import httpx

from app.adapters.media import probe_duration
from app.config import Settings
from app.models import TTSRequest, TTSResponse
from app.storage import ensure_parent, resolve_data_path


class TTSAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def backend_name(self) -> str:
        return self.settings.ml_tts_backend

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        if self.settings.ml_tts_backend == "indextts2":
            if self.settings.indextts2_inline:
                return self._run_indextts2_inline(request)
            return self._run_indextts2(request)
        if self.settings.ml_tts_backend == "edge_tts":
            return self._run_edge_tts(request)
        return self._run_silence(request)

    def _run_edge_tts(self, request: TTSRequest) -> TTSResponse:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError("edge-tts is not installed; add it to pyproject.toml[edge_tts] or install manually") from exc

        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)

        # Resolve voice: use voice_config["edge_tts_voice"] if set, else fall back to locale default
        voice = (request.voice_config or {}).get("edge_tts_voice") or self.settings.edge_tts_voice

        tmp_mp3 = output_path.with_suffix(".mp3")

        async def _synthesize() -> None:
            communicate = edge_tts.Communicate(request.text, voice)
            await communicate.save(str(tmp_mp3))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _synthesize())
                future.result(timeout=60)
        else:
            asyncio.run(_synthesize())

        # Convert mp3 → wav at the project sample rate, then apply atempo to hit target duration
        actual_raw_sec = probe_duration(self.settings, tmp_mp3)
        target_sec = max(request.target_duration_sec, 0.05)
        # clamp atempo to [0.5, 2.0] – ffmpeg hard limits
        if actual_raw_sec > 0:
            ratio = actual_raw_sec / target_sec
            ratio = max(0.5, min(2.0, ratio))
            atempo_filter = f"atempo={ratio:.6f}"
        else:
            atempo_filter = "atempo=1.0"

        subprocess.run(
            [
                self.settings.ffmpeg_bin, "-y",
                "-i", str(tmp_mp3),
                "-af", atempo_filter,
                "-ar", str(self.settings.default_sample_rate),
                "-ac", str(self.settings.default_channels),
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        tmp_mp3.unlink(missing_ok=True)

        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=[f"tts backend=edge_tts voice={voice} atempo={atempo_filter}"],
        )

    def _run_silence(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        write_silence_wav(
            output_path,
            sample_rate=self.settings.default_sample_rate,
            channels=self.settings.default_channels,
            duration_sec=max(request.target_duration_sec, 0.05),
        )
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=int(request.target_duration_sec * 1000),
            diagnostics=["tts backend=silence"],
        )

    def _run_indextts2_inline(self, request: TTSRequest) -> TTSResponse:
        try:
            from indextts import IndexTTS2  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "indextts2-inference is not installed; add it to pyproject.toml[real] or install manually"
            ) from exc

        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)

        # Lazy-load the model as a singleton on this adapter instance so it is only
        # initialised once per worker process (model weights stay in GPU VRAM).
        if not hasattr(self, "_indextts2_model"):
            init_kwargs: dict = {}
            if self.settings.indextts2_model_dir:
                init_kwargs["model_dir"] = self.settings.indextts2_model_dir
            if self.settings.indextts2_attn_backend:
                init_kwargs["attn_backend"] = self.settings.indextts2_attn_backend
            self._indextts2_model = IndexTTS2(**init_kwargs)  # type: ignore[assignment]

        tts = self._indextts2_model

        # Resolve spk_audio_prompt from voice_config, then fall back to global default.
        spk_audio: str | None = None
        samples = (request.voice_config or {}).get("sample_relpaths", [])
        if samples:
            spk_audio = str(resolve_data_path(self.settings.data_root, samples[0]))
        if spk_audio is None and self.settings.indextts2_default_voice_relpath:
            spk_audio = str(
                resolve_data_path(
                    self.settings.data_root, self.settings.indextts2_default_voice_relpath
                )
            )
        if spk_audio is None:
            raise RuntimeError(
                "IndexTTS2 inline: no spk_audio_prompt available. "
                "Bind a VoiceProfile with at least one sample_relpath, "
                "or set INDEXTTS2_DEFAULT_VOICE_RELPATH."
            )

        # Estimate max_mel_tokens from target duration.
        # Empirical rate: ~86 AR tokens/sec at the model's 22 050 Hz output.
        # A 1.3× headroom lets the model breathe; atempo corrects residual drift.
        target_sec = max(request.target_duration_sec, 0.1)
        estimated_tokens = int(target_sec * 86 * 1.3)
        max_mel_tokens = max(256, min(4096, estimated_tokens))

        tmp_wav = output_path.with_suffix(".tmp.wav")
        tts.infer(
            spk_audio_prompt=spk_audio,
            text=request.text,
            output_path=str(tmp_wav),
            max_mel_tokens=max_mel_tokens,
            use_emo_text=self.settings.indextts2_use_emo_text,
            # num_beams=1 is ~3x faster than the default of 3; quality difference
            # is minor for dubbing use-cases where duration accuracy matters more.
            num_beams=1,
        )

        # Post-process: resample to project sample rate and apply atempo if the
        # generated duration drifts more than 5 % from the target.
        actual_sec = probe_duration(self.settings, tmp_wav)
        atempo_filter: str | None = None
        if actual_sec > 0 and target_sec > 0:
            ratio = actual_sec / target_sec
            ratio = max(0.5, min(2.0, ratio))
            if abs(ratio - 1.0) > 0.05:
                atempo_filter = f"atempo={ratio:.6f}"

        af_args = [atempo_filter, f"aresample={self.settings.default_sample_rate}"]
        af_chain = ",".join(f for f in af_args if f)
        subprocess.run(
            [
                self.settings.ffmpeg_bin, "-y",
                "-i", str(tmp_wav),
                "-af", af_chain,
                "-ac", str(self.settings.default_channels),
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        tmp_wav.unlink(missing_ok=True)

        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        diag = [
            f"tts backend=indextts2(inline) max_mel_tokens={max_mel_tokens}"
            f" emo_text={self.settings.indextts2_use_emo_text}"
        ]
        if atempo_filter:
            diag.append(f"atempo={atempo_filter} (fallback, actual={actual_sec:.2f}s target={target_sec:.2f}s)")
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=diag,
        )

    def _run_indextts2(self, request: TTSRequest) -> TTSResponse:
        if self.settings.indextts2_command:
            return self._run_indextts2_command(request)
        if self.settings.indextts2_endpoint:
            return self._run_indextts2_http(request)
        raise RuntimeError("INDEXTTS2_COMMAND or INDEXTTS2_ENDPOINT is required when ML_TTS_BACKEND=indextts2")

    def _run_indextts2_command(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        voice_json = json.dumps(request.voice_config, ensure_ascii=False)
        command_template = self.settings.indextts2_command.format(
            text=request.text,
            duration=request.target_duration_sec,
            output=output_path,
            voice_json=voice_json,
        )
        command = shlex.split(command_template)
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "IndexTTS2 command failed")
        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=["tts backend=indextts2(command)"],
        )

    def _run_indextts2_http(self, request: TTSRequest) -> TTSResponse:
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)
        headers = {}
        if self.settings.indextts2_api_key:
            headers["Authorization"] = f"Bearer {self.settings.indextts2_api_key}"

        payload = {
            "model": self.settings.indextts2_model,
            "text": request.text,
            "target_duration_sec": request.target_duration_sec,
            "voice_config": request.voice_config,
            "output_path": str(output_path),
        }
        response = httpx.post(
            self.settings.indextts2_endpoint,
            json=payload,
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if content_type.startswith("audio/"):
            output_path.write_bytes(response.content)
        else:
            body = response.json()
            if "audio_base64" in body:
                output_path.write_bytes(base64.b64decode(body["audio_base64"]))
            elif body.get("audio_path"):
                source = Path(body["audio_path"])
                output_path.write_bytes(source.read_bytes())
            elif not output_path.exists():
                raise RuntimeError("IndexTTS2 HTTP response did not provide audio content")

        duration_ms = int(probe_duration(self.settings, output_path) * 1000)
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=duration_ms,
            diagnostics=["tts backend=indextts2(http)"],
        )


def write_silence_wav(path: Path, sample_rate: int, channels: int, duration_sec: float) -> None:
    total_frames = int(sample_rate * duration_sec)
    frame = struct.pack("<h", 0) * channels
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for _ in range(total_frames):
            wav_file.writeframesraw(frame)
