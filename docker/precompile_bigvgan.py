"""Pre-compile BigVGAN's anti-alias activation CUDA kernel during ml.Dockerfile build.

Why this script must align with IndexTTS' runtime loader
--------------------------------------------------------
IndexTTS' upstream loader at
``indextts/s2mel/modules/bigvgan/alias_free_activation/cuda/load.py``
hard-codes ``build_directory = srcpath / 'build'`` (under site-packages),
NOT the PyTorch default ``~/.cache/torch_extensions/``. Any precompile
that targets the latter is silently wasted because the runtime loader
never reads from there.

In addition, IndexTTS' loader computes ``cc_flags`` from the runtime
GPU's compute capability:

    arch=compute_<MAJOR><MINOR>,code=sm_<MAJOR><MINOR>

We therefore must emit IDENTICAL ``cc_flags`` here so that PyTorch's
``cpp_extension.load`` cache validity check (which hashes ``build.ninja``)
matches at runtime and skips the JIT compile entirely.

Strategy
--------
1. Monkey-patch ``torch.cuda`` so that, even though no GPU is visible
   inside the docker build sandbox, ``torch.cuda.is_available()`` claims
   True and ``torch.cuda.get_device_capability(0)`` returns the target
   compute capability (default sm_120, override via ``BIGVGAN_TARGET_SM``).
2. Call IndexTTS' own ``load.load()`` so build_directory and every
   other compile flag is contributed by IndexTTS itself.
3. Result: the .so is dropped into
   ``<site-packages>/indextts/.../cuda/build/anti_alias_activation_cuda.so``
   and the matching ``build.ninja`` exactly mirrors what runtime would
   recompute, so runtime PyTorch reports "ninja: no work to do" and
   ``dlopen``s in <5 s.

If the precompile fails (no nvcc, etc.) we exit non-zero so the docker
build line can `|| echo "..."` it as non-fatal but visible in the log.
"""

from __future__ import annotations

import os
import sys
import time

TARGET_SM = os.environ.get("BIGVGAN_TARGET_SM", "120")

if not (TARGET_SM.isdigit() and len(TARGET_SM) in (2, 3)):
    print(f"BIGVGAN_TARGET_SM={TARGET_SM!r} is not a valid compute capability "
          "(expected e.g. '70', '80', '86', '89', '120'). Skipping.")
    sys.exit(0)

if len(TARGET_SM) == 2:
    target_major, target_minor = int(TARGET_SM[0]), int(TARGET_SM[1])
else:
    target_major, target_minor = int(TARGET_SM[:-1]), int(TARGET_SM[-1])

print(f"[precompile_bigvgan] target compute capability sm_{TARGET_SM} "
      f"({target_major}.{target_minor})")

try:
    import torch
except ImportError:
    print("[precompile_bigvgan] torch not installed -- skipping")
    sys.exit(0)

try:
    import indextts  # noqa: F401
except ImportError:
    print("[precompile_bigvgan] indextts not installed -- skipping")
    sys.exit(0)

# Monkey-patch torch.cuda so IndexTTS' load.load() generates cc_flags
# for the deployment GPU even though the build container has no GPU.
torch.cuda.is_available = lambda: True  # type: ignore[assignment]
torch.cuda.device_count = lambda: 1  # type: ignore[assignment]
torch.cuda.get_device_capability = (  # type: ignore[assignment]
    lambda _idx=0: (target_major, target_minor)
)

# torch.utils.cpp_extension also probes ``torch.version.cuda`` and
# ``CUDA_HOME``; the devel base image already provides a real CUDA
# toolkit so those work as-is.

# Trigger IndexTTS' own loader. This compiles to the IDENTICAL build
# directory and with the IDENTICAL flags that runtime will compute.
print("[precompile_bigvgan] invoking IndexTTS' own load.load() ...")
t0 = time.time()
try:
    from indextts.s2mel.modules.bigvgan.alias_free_activation.cuda.load import (
        load as bigvgan_load,
    )
    ext = bigvgan_load()
    elapsed = time.time() - t0
    print(f"[precompile_bigvgan] OK in {elapsed:.1f}s; extension={ext}")
except Exception as exc:  # noqa: BLE001
    elapsed = time.time() - t0
    print(f"[precompile_bigvgan] FAILED after {elapsed:.1f}s: {exc!r}")
    print("[precompile_bigvgan] falling back: kernel will be JIT-compiled at "
          "first runtime use (set INDEXTTS2_USE_CUDA_KERNEL=false to disable).")
    sys.exit(1)
