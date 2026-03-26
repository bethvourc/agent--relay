from __future__ import annotations

from dataclasses import dataclass

EVIDENCE_DEPTHS = {"minimal", "standard", "full"}


@dataclass(frozen=True)
class ResumeRenderOptions:
    evidence_depth: str = "standard"

    def __post_init__(self) -> None:
        if self.evidence_depth not in EVIDENCE_DEPTHS:
            allowed = ", ".join(sorted(EVIDENCE_DEPTHS))
            raise ValueError(f"evidence_depth must be one of: {allowed}")
