"""Declarative linguistic metadata, NOT an operation/permission registry.

M3.3-A observes existing parsers in shadow. Merely declaring an utterance family
never enables a route or grants authority. Slot decoders reuse production date,
clock and literal rules; there is no new model, database access or learned NLU.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .semantic_parser import parse_semantic
from .semantic_types import SEMANTIC_VERSION, SemanticFrame

INTERACTION_VERSION = '1.0.0'


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SlotSpec:
    name: str
    kind: str
    required: bool = False
    destination: str | None = None
    default: str | None = None


@dataclass(frozen=True, slots=True)
class IntentSpec:
    name: str
    widget: str
    action: str
    families: tuple[str, ...]
    slots: tuple[SlotSpec, ...] = ()
    dialog: bool = False


DATE = SlotSpec('date', 'date', True, 'date')
TIME = SlotSpec('time', 'exact_time', True, 'time')
TITLE = SlotSpec('title', 'source_literal', True, 'title')
OPTIONAL_TIME = SlotSpec('time', 'exact_time', False, 'time')
TARGET = SlotSpec('target_text', 'source_literal', True)
OPTIONAL_DATE = SlotSpec('date', 'date', False, 'date')
READ_SLOTS = (SlotSpec('start', 'date', destination='start', default='today'),
              SlotSpec('end', 'date', destination='end', default='today'),
              SlotSpec('status', 'todo_status', destination='status', default='pending'),
              SlotSpec('period', 'period', destination='period'))
SPECS = (
    IntentSpec('todo.list', 'todo', 'list', ('existing_scoped_read', 'bounded_collection_read'), READ_SLOTS),
    IntentSpec('todo.add', 'todo', 'add', ('explicit_date_literal_add',), (DATE, TITLE, OPTIONAL_TIME), True),
    *(IntentSpec('todo.' + action, 'todo', action, ('single_literal_' + action,),
                 (TARGET, OPTIONAL_DATE), True) for action in ('complete', 'reopen', 'delete')),
    IntentSpec('calendar.list', 'calendar', 'list', ('existing_scoped_read', 'bounded_collection_read'),
               (*READ_SLOTS[:2], SlotSpec('status', 'todo_status', destination='status', default='all'), READ_SLOTS[3])),
    # Time remains OPTIONAL in the current product contract. Never infer all_day.
    IntentSpec('calendar.add', 'calendar', 'add', ('explicit_date_literal_add',), (DATE, TITLE, OPTIONAL_TIME), True),
    IntentSpec('memo.read', 'memo', 'read', ('existing_memo_selector',), (SlotSpec('selector', 'memo_selector'),)),
    IntentSpec('alarm.list', 'alarm', 'list', ('existing_alarm_list',)),
    IntentSpec('alarm.set', 'alarm', 'set', ('explicit_alarm_date_clock',), (DATE, TIME), True),
    IntentSpec('alarm.cancel', 'alarm', 'cancel', ('existing_explicit_alarm_id',), (SlotSpec('target', 'server_target'),)),
)


def manifest() -> dict:
    return {'version': INTERACTION_VERSION, 'semantic_version': SEMANTIC_VERSION,
            'intents': [asdict(s) for s in SPECS], 'authority': 'linguistic_metadata_only',
            'activation': 'shadow_initial_request; explicit_bound_missing_slot_followup_only',
            'delegated': ['timer', 'legacy_atomic_writes', 'existing_read_and_memo_paths']}


def model_hash() -> str:
    return fingerprint(manifest())


def spec_for(widget: str, action: str) -> IntentSpec | None:
    return next((s for s in SPECS if (s.widget, s.action) == (widget, action)), None)


def validate_registry(registry, specs=SPECS) -> None:
    """Fail startup on linguistic/production drift; never register an operation."""
    names = set()
    for spec in specs:
        if spec.name in names:
            raise ValueError('Duplicate interaction intent')
        names.add(spec.name)
        adapter = registry.get(spec.widget)
        op = adapter.operations.get(spec.action) if adapter else None
        if op is None:
            raise ValueError('Interaction intent has no registered operation: ' + spec.name)
        if len({s.name for s in spec.slots}) != len(spec.slots):
            raise ValueError('Duplicate interaction slot')
        for slot in spec.slots:
            if slot.kind not in {'date', 'exact_time', 'source_literal', 'todo_status', 'period', 'memo_selector', 'server_target'}:
                raise ValueError('Unknown interaction slot kind')
            if slot.destination and slot.destination not in op.inputs.model_fields:
                # Optional date qualifies SERVER entity selection; it is not sent
                # as an argument to complete/reopen/delete.
                if slot == OPTIONAL_DATE and op.target_required:
                    continue
                raise ValueError('Interaction slot not representable: ' + spec.name + '.' + slot.name)
        if spec.dialog and op.read_only:
            raise ValueError('M3.3-A dialog is limited to existing write previews')


def known_slots(frame: SemanticFrame, spec: IntentSpec) -> dict:
    args = dict(frame.arguments)
    values = {'date': frame.temporal.start, 'time': frame.temporal.time,
              'target_text': frame.target_text, **args}
    return {s.name: values.get(s.name) for s in spec.slots}


def missing_slots(frame: SemanticFrame, spec: IntentSpec) -> tuple[str, ...]:
    values = known_slots(frame, spec)
    return tuple(s.name for s in spec.slots if s.required and values[s.name] is None)


def candidate(text: str, at: str, timezone: str, *, raw: str) -> SemanticFrame | None:
    # Reuse the source-only production parser; don't duplicate a Router or NLU.
    return parse_semantic(text, at, timezone, raw=raw)


def shadow(record: dict) -> dict:
    """Read-only observation. No claims about gold accuracy or route activation."""
    from .semantic_types import semantic_decision
    frame = candidate(record['normalized'], record['reference_at'], record['timezone'], raw=record['raw'])
    spec = spec_for(frame.widget, frame.action) if frame else None
    return {'version': INTERACTION_VERSION, 'hash': model_hash(), 'activated': False,
            'scope': 'shadow_existing_source_parser_not_an_independent_prediction',
            'candidate': spec.name if spec else None,
            'slots': known_slots(frame, spec) if spec else {},
            'missing_slots': list(missing_slots(frame, spec)) if spec else [],
            'outcome': semantic_decision(frame)['outcome'],
            'evidence': frame.data()['evidence'] if frame else [],
            'authoritative_route': record.get('routing', {}).get('route')}
