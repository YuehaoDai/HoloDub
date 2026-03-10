from pathlib import Path


def resolve_data_path(data_root: Path, relpath: str) -> Path:
    return data_root / relpath


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
