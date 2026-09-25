"""M3 source-only proposals. No database IDs, versions or authority live here."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

SEMANTIC_VERSION = '1.1.0'
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


def semantic_decision(frame: SemanticFrame | None) -> dict:
    """Diagnostic arbitration, not permission and not a model confidence score.

    MISSING remains the stored frame confidence for compatibility. The richer
    outcome distinguishes absent user arguments from parse/temporal ambiguity.
    EXACT means a source plan, not a resolved entity or authorization to execute.
    """
    if frame is None:
        return {'outcome': 'NO_MATCH', 'reason_code': 'PARSE_UNCERTAIN',
                'next_step': 'existing_guarded_route', 'known_arguments': {}}
    if frame.confidence == 'EXACT':
        outcome, reason, step = 'EXACT', 'SOURCE_PLAN_COMPLETE', 'existing_validation_and_policy'
    elif frame.confidence == 'UNSUPPORTED':
        outcome, reason, step = 'UNSUPPORTED', 'UNSUPPORTED_CONDITION', 'clarify'
    elif frame.issue in {'TEMPORAL_NOT_EXACT', 'STATUS_CONFLICT', 'NONCONTIGUOUS_TITLE'}:
        outcome, reason, step = 'AMBIGUOUS', 'PARSE_AMBIGUITY', 'clarify'
    else:
        outcome, reason, step = 'PARTIAL', 'MISSING_REQUIRED_ARGUMENT', 'clarify'
    return {'outcome': outcome, 'reason_code': reason, 'source_issue': frame.issue,
            'field': frame.field, 'next_step': step, 'known_arguments': dict(frame.arguments),
            'source_temporal': {'start': frame.temporal.start, 'end': frame.temporal.end,
                                'time': frame.temporal.time},
            'scope': 'source_plan_only_not_entity_resolution_or_execution_authority'}
