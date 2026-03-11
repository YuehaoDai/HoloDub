"""
Pre-compile BigVGAN's anti-alias activation CUDA kernel during Docker build.

This runs as part of the ml.Dockerfile build so the compiled .so is baked
into the image layer.  Every container start then loads the cached extension
directly, eliminating the ~2-minute JIT compilation that otherwise happens
on the first TTS inference call.

Set TORCH_CUDA_ARCH_LIST before running, e.g.:
    TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9+PTX;12.0" python3 precompile_bigvgan.py
"""

import glob
import os
import sys
import time

os.environ.setdefault("TORCH_CUDA_ARCH_LIST", "7.5;8.0;8.6;8.9+PTX;12.0")

try:
    import indextts
except ImportError:
    print("indextts not installed — skipping BigVGAN pre-compilation")
    sys.exit(0)

base = os.path.join(
    os.path.dirname(indextts.__file__),
    "s2mel", "modules", "bigvgan", "alias_free_activation", "cuda",
)

cpp_sources = sorted(glob.glob(os.path.join(base, "*.cpp")))
cu_sources  = sorted(glob.glob(os.path.join(base, "*.cu")))

if not cu_sources:
    print("No CUDA sources found under", base, "— skipping")
    sys.exit(0)

print(f"Compiling BigVGAN CUDA kernel ({len(cpp_sources)} .cpp, {len(cu_sources)} .cu) …")
print(f"Architectures: {os.environ.get('TORCH_CUDA_ARCH_LIST')}")

t0 = time.time()
try:
    from torch.utils.cpp_extension import load
    ext = load(
        name="anti_alias_activation_cuda",
        sources=cpp_sources + cu_sources,
        extra_cuda_cflags=["-O3", "--use_fast_math"],
        verbose=True,
    )
    elapsed = time.time() - t0
    print(f"BigVGAN CUDA kernel compiled successfully in {elapsed:.1f}s")
    print(f"Extension: {ext}")
except Exception as exc:
    print(f"Pre-compilation failed ({exc}); kernel will be compiled at first runtime use")
    sys.exit(1)
