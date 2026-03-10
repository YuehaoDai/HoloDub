from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ModelManifest:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "path": str(self.path),
                "error": "invalid_json",
            }
