from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"sha256:{digest}"


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
