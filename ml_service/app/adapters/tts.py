from __future__ import annotations

import base64
import json
import logging
import shlex
import struct
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any

import httpx

from app.adapters.media import probe_duration
from app.config import Settings
from app.models import TTSRequest, TTSResponse
from app.storage import ensure_parent, resolve_data_path

logger = logging.getLogger(__name__)


class UnsupportedTTSBackendError(RuntimeError):
    """Raised when ml_tts_backend is set to a value the adapter does not support.

    The previous behaviour silently fell back to generating silent WAV files,
    which masked configuration mistakes (a worker would persist 'successful'
    silent segments). Now we surface the error so operators can fix the env.
    """

    def __init__(self, backend: str, supported: tuple[str, ...]) -> None:
        self.backend = backend
        self.supported = supported
        super().__init__(
            f"unsupported ML_TTS_BACKEND={backend!r}; "
            f"expected one of {list(supported)}"
        )


_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC = 30 * 60.0
"""Maximum number of seconds a non-loader caller will wait for the in-flight
IndexTTS2 load before giving up with ``TimeoutError``.

This caps the worst-case latency of a synthesis request that arrives during
warm-up: instead of blocking forever (and bottlenecking the entire worker),
the request fails fast with a 503-mappable error. The watchdog in
``app.main`` will independently flip the warm-up status to ``error`` if the
loader thread vanishes, so subsequent requests will fail fast as well.
"""


class TTSAdapter:
    """Thin facade over the configured TTS backend.

    Concurrency model for IndexTTS2 inline mode:
        - The state machine has four states: ``idle -> loading -> ready``
          or ``idle -> loading -> error``. Transitions are protected by a
          short critical section (``_state_lock``); the GPU-heavy
          ``IndexTTS2(...)`` construction itself runs *outside* any lock
          so a crash, segfault, or external SIGKILL of the loader thread
          can never leak a permanently-held mutex.
        - The first caller to observe ``status == idle`` becomes the
          *loader* and atomically flips it to ``loading``. Concurrent
          callers become *waiters* and block on a ``threading.Event``
          with a hard timeout (``_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC``).
        - On success the loader publishes the model handle then sets
          status to ``ready`` and signals the event. On failure it
          records the exception, sets status to ``error``, and still
          signals the event so waiters do not deadlock.
        - After a terminal ``error`` callers are allowed to retry; the
          next caller to acquire the state lock will reset the state
          machine to ``loading`` and attempt the load again.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._indextts2_warmup_status: str = "idle"
        self._indextts2_model: Any | None = None
        self._state_lock = threading.Lock()
        self._load_done = threading.Event()
        self._indextts2_load_error: Exception | None = None

    def backend_name(self) -> str:
        return self.settings.ml_tts_backend

    def get_indextts2_warmup_status(self) -> str:
        """Return 'idle'|'loading'|'ready'|'error' for IndexTTS2 inline mode."""
        return self._indextts2_warmup_status

    def force_indextts2_load_error(self, reason: str) -> None:
        """Mark IndexTTS2 warm-up as failed from outside the loader.

        Used by the lifespan watchdog when it observes a vanished loader
        thread or a hard timeout. The transition is no-op when the
        status is already terminal (``ready`` / ``error``) so multiple
        concurrent watchdogs cannot stomp on each other.
        """
        with self._state_lock:
            if self._indextts2_warmup_status in ("ready", "error"):
                return
            self._indextts2_warmup_status = "error"
            self._indextts2_load_error = RuntimeError(reason)
        # Wake any waiters so they fail-fast instead of blocking until the
        # ``_INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC`` ceiling.
        self._load_done.set()
        logger.error("IndexTTS2 warm-up forced to error: %s", reason)

    def is_indextts2_inline_enabled(self) -> bool:
        return (
            self.settings.ml_tts_backend == "indextts2"
            and self.settings.indextts2_inline
        )

    def warm_up_indextts2(self) -> None:
        """Eagerly load IndexTTS2 (synchronously). No-op if not in inline mode.

        Intended to be called from FastAPI lifespan in a background
        thread; the loader itself blocks until the model is ready or an
        error is raised. Exceptions are logged but never propagated so a
        broken environment does not crash the FastAPI startup sequence
        — ``/readyz`` will surface the failure instead.
        """
        if not self.is_indextts2_inline_enabled():
            return
        try:
            self.ensure_indextts2_loaded()
        except Exception as exc:  # noqa: BLE001
            logger.exception("IndexTTS2 warm-up failed: %s", exc)

    def ensure_indextts2_loaded(
        self, wait_timeout_sec: float = _INDEXTTS2_LOAD_WAIT_TIMEOUT_SEC
    ) -> Any:
        """Load IndexTTS2 once (or wait for an in-flight load) and return it.

        The fast path (model already cached) returns without touching any
        lock. Concurrent first-time callers cooperate via a state machine
        so the model is never instantiated twice; non-loader callers
        wait on an event with a hard timeout so a vanished loader thread
        cannot wedge the whole service.
        """
        if self._indextts2_model is not None:
            return self._indextts2_model

        with self._state_lock:
            if self._indextts2_model is not None:
                return self._indextts2_model

            if self._indextts2_warmup_status == "loading":
                role = "waiter"
            else:
                # ``idle`` (first ever call) or ``error`` (previous attempt
                # failed and a caller is retrying) — either way, we become
                # the loader and reset the event so waiters block again.
                self._indextts2_warmup_status = "loading"
                self._indextts2_load_error = None
                self._load_done.clear()
                role = "loader"

        if role == "waiter":
            if not self._load_done.wait(timeout=wait_timeout_sec):
                raise TimeoutError(
                    f"timed out after {wait_timeout_sec:.0f}s waiting for "
                    "IndexTTS2 load to finish"
                )
            if self._indextts2_model is None:
                err = self._indextts2_load_error
                msg = (
                    f"IndexTTS2 load failed: status={self._indextts2_warmup_status}"
                )
                if err is not None:
                    raise RuntimeError(msg) from err
                raise RuntimeError(msg)
            return self._indextts2_model

        # We are the loader. Heavy work below runs *outside* any lock so
        # an abrupt thread death cannot strand the service.
        try:
            try:
                from indextts import IndexTTS2  # type: ignore[import]
            except ImportError as exc:
                raise RuntimeError(
                    "indextts2-inference is not installed; "
                    "add it to pyproject.toml[real] or install manually"
                ) from exc

            init_kwargs: dict[str, Any] = {
                "use_fp16": True,
                # See settings.indextts2_use_cuda_kernel for context: default
                # False because the BigVGAN fused-CUDA-kernel JIT path hangs
                # on sm_120 inside the FastAPI lifespan worker thread.
                "use_cuda_kernel": self.settings.indextts2_use_cuda_kernel,
            }
            if self.settings.indextts2_model_dir:
                init_kwargs["model_dir"] = self.settings.indextts2_model_dir
            if self.settings.indextts2_attn_backend:
                init_kwargs["attn_backend"] = self.settings.indextts2_attn_backend
            logger.info(
                "loading IndexTTS2 (model_dir=%s, attn=%s, fp16=True, cuda_kernel=%s)",
                self.settings.indextts2_model_dir or "<auto>",
                self.settings.indextts2_attn_backend or "<sdpa>",
                self.settings.indextts2_use_cuda_kernel,
            )
            model = IndexTTS2(**init_kwargs)
        except BaseException as exc:
            with self._state_lock:
                self._indextts2_warmup_status = "error"
                self._indextts2_load_error = (
                    exc if isinstance(exc, Exception) else None
                )
            self._load_done.set()
            logger.exception("IndexTTS2 load raised: %s", exc)
            raise
        else:
            with self._state_lock:
                self._indextts2_model = model
                self._indextts2_warmup_status = "ready"
            self._load_done.set()
            logger.info("IndexTTS2 loaded; warmup_status=ready")
            return model

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        # Empty / punctuation-only text always returns silence, regardless of
        # the configured backend. This avoids real TTS models generating
        # unexpectedly long audio for degenerate inputs like lone periods.
        _printable = request.text.translate(
            str.maketrans("", "", " \t\n\r.,!?。！？，、；：…—–")
        )
        if not _printable:
            return self._run_silence(request)

        backend = self.settings.ml_tts_backend
        if backend == "indextts2":
            if self.settings.indextts2_inline:
                return self._run_indextts2_inline(request)
            return self._run_indextts2(request)
        if backend == "silence":
            return self._run_silence(request)

        # Unknown / misconfigured backend: refuse loudly instead of silently
        # producing a silent WAV (which downstream worker would persist as a
        # successful synthesis result, masking the misconfiguration).
        raise UnsupportedTTSBackendError(
            backend=backend,
            supported=("indextts2", "silence"),
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
        output_path = resolve_data_path(self.settings.data_root, request.output_relpath)
        ensure_parent(output_path)

        # Single load path: blocks once on the first concurrent request,
        # then becomes O(1) for the remainder of the process lifetime.
        tts = self.ensure_indextts2_loaded()

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

        # Scheme 1 — text-based token budget:
        #   Estimate how many tokens the text needs using tokens_per_char.
        #   This is the primary constraint; the model will naturally stop when
        #   the text is consumed, well before this budget is exhausted.
        #
        # Scheme 2 — adaptive tokens_per_char (applied on retry):
        #   Blend observed rate from the previous attempt with the prior so the
        #   budget converges toward the actual rate for this specific segment.
        #
        # Hard ceiling — max_allowed_sec:
        #   Audio must never exceed (target + gap) to avoid spilling into the
        #   next segment.  This is enforced separately in the merge stage by
        #   clipping each overlay to its allowed slot.

        TOKENS_PER_SEC: float = 50.0
        PRIOR_TOKENS_PER_CHAR: float = 13.5
        HEADROOM: float = 1.05

        target_sec = max(request.target_duration_sec, 0.1)
        max_allowed_sec = request.max_allowed_sec if request.max_allowed_sec > 0 else target_sec
        text_chars = max(1, len([c for c in request.text if not c.isspace()]))

        # Scheme 2: blend observed tokens_per_char with prior if feedback exists.
        tokens_per_char = PRIOR_TOKENS_PER_CHAR
        if request.prev_actual_sec > 0 and request.prev_text_chars > 0:
            observed = (request.prev_actual_sec * TOKENS_PER_SEC) / request.prev_text_chars
            tokens_per_char = 0.6 * observed + 0.4 * PRIOR_TOKENS_PER_CHAR

        # Scheme 1: text-based budget — primary constraint.
        tokens_from_text = int(text_chars * tokens_per_char * HEADROOM)

        # Hard ceiling: audio MUST NOT exceed (target + gap).
        tokens_from_allowed = int(max_allowed_sec * TOKENS_PER_SEC)

        max_mel_tokens = max(64, min(tokens_from_text, tokens_from_allowed, 4096))

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

        # Post-process: resample to project sample rate only.
        # Duration stretching (atempo) is intentionally omitted — any overflow is
        # handled in the pipeline by borrowing from the trailing silence gap, and
        # re-translation is triggered when the overflow exceeds that gap.
        subprocess.run(
            [
                self.settings.ffmpeg_bin, "-y",
                "-i", str(tmp_wav),
                "-af", f"aresample={self.settings.default_sample_rate}",
                "-ac", str(self.settings.default_channels),
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        tmp_wav.unlink(missing_ok=True)

        actual_sec = probe_duration(self.settings, output_path)
        duration_ms = int(actual_sec * 1000)
        diag = [
            f"tts backend=indextts2(inline) max_mel_tokens={max_mel_tokens}"
            f" tokens_per_char={tokens_per_char:.2f}"
            f" (prev_actual={request.prev_actual_sec:.2f}s prev_chars={request.prev_text_chars})"
            f" emo_text={self.settings.indextts2_use_emo_text}"
            f" actual={actual_sec:.2f}s target={target_sec:.2f}s"
        ]
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
