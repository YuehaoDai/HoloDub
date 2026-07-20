#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks for the IndexTTS2 LoRA pilot.")
    parser.add_argument("--manifest-dir", type=Path, default=None)
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "python": check_container_python(args.ml_container),
        "gpu": check_gpu(),
        "manifest": check_manifest(args.manifest_dir) if args.manifest_dir else None,
        "recommendations": [],
    }
    recommendations = report["recommendations"]
    py = report["python"]
    manifest = report.get("manifest") or {}

    if not py.get("peft_installed"):
        recommendations.append("install the training extra before running LoRA: peft is missing")
    if not py.get("trainer_modules_present"):
        recommendations.append(
            "current indextts2-inference package has no trainer; use the HoloDub trainer scaffold or vendor a reviewed training_v2 script"
        )
    if manifest and manifest.get("tier") == "silver":
        recommendations.append(
            "manifest is silver synthetic data; use it only for smoke tests/duration calibration"
        )
    if manifest and manifest.get("accepted_audio_hours", 0) < 0.5:
        recommendations.append("accepted audio is below 30 minutes; gather/label more data before real LoRA")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def check_container_python(container: str) -> dict[str, Any]:
    script = r"""
import importlib.util, inspect, json, pkgutil
out = {}
try:
    import indextts
    out["indextts_path"] = indextts.__file__
    mods = [m.name for m in pkgutil.walk_packages(indextts.__path__, indextts.__name__ + ".")]
    out["trainer_modules_present"] = any("train" in m.lower() or "trainer" in m.lower() for m in mods)
    from indextts.gpt.model_v2 import UnifiedVoice
    out["unified_voice_init"] = str(inspect.signature(UnifiedVoice.__init__))
except Exception as exc:
    out["indextts_error"] = repr(exc)
out["peft_installed"] = importlib.util.find_spec("peft") is not None
out["accelerate_installed"] = importlib.util.find_spec("accelerate") is not None
out["omegaconf_installed"] = importlib.util.find_spec("omegaconf") is not None
print(json.dumps(out, ensure_ascii=False))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=script,
        text=True,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(proc.stdout)


def check_gpu() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"available": False, "error": repr(exc)}
    line = proc.stdout.strip().splitlines()[0]
    name, total, used, driver = [part.strip() for part in line.split(",")]
    return {
        "available": True,
        "name": name,
        "memory_total_mib": int(total),
        "memory_used_mib": int(used),
        "driver_version": driver,
    }


def check_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    stats = payload.get("stats", {})
    return {
        "path": str(manifest_path),
        "tier": payload.get("tier"),
        "accepted": int(stats.get("accepted") or 0),
        "train": int(stats.get("train") or 0),
        "val": int(stats.get("val") or 0),
        "test": int(stats.get("test") or 0),
        "accepted_audio_hours": float(stats.get("total_audio_hours") or 0),
        "warnings": payload.get("warnings", []),
    }


if __name__ == "__main__":
    sys.exit(main())
