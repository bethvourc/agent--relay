from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return path


def write_json_atomic(path: Path, data: dict[str, Any]) -> Path:
    return write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
