"""M3 source-only proposals. No database IDs, versions or authority live here."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

SEMANTIC_VERSION = '1.0.0'
Scalar = str | int | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """Offsets refer to the stored NFC/whitespace-normalized transcript, not audio."""
    field: str
    start: int
    end: int
    text: str
    policy: str = 'source_span'


@dataclass(frozen=True, slots=True)
class Temporal:
    start: str | None = None
    end: str | None = None
    date_ref: str | None = None
    time: str | None = None
    period: str | None = None
    relation: str | None = None
    relative_minutes: int | None = None
    exact_minute: bool = True
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticFrame:
    widget: str
    action: str
    arguments: tuple[tuple[str, Scalar], ...]
    temporal: Temporal
    target_text: str | None = None
    evidence: tuple[Evidence, ...] = ()
    confidence: Literal['EXACT', 'MISSING', 'UNSUPPORTED'] = 'EXACT'
    issue: str | None = None
    field: str | None = None
    message: str | None = None
    version: str = SEMANTIC_VERSION

    def proposal(self) -> dict:
        # The server resolver supplies a target only AFTER source validation.
        return {'widget': self.widget, 'action': self.action,
                'target': None, 'args': dict(self.arguments)}

    def data(self) -> dict:
        value = asdict(self)
        value['arguments'] = dict(self.arguments)
        # Plain JSON-compatible lists make stored proof round-trips exact.
        value['evidence'] = [asdict(e) for e in self.evidence]
        value['temporal']['evidence'] = [asdict(e) for e in self.temporal.evidence]
        return value
