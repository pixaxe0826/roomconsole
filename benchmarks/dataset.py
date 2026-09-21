"""Data-only suites. Answers stay here and in scoring, never in the runtime caller."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_ROOT = Path(__file__).resolve().parent / 'suites'
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$')
CAPABILITY = re.compile(r'^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$')


def reject_duplicate_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def json_read(path: Path) -> Any:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f'File exceeds 32 MiB: {path.name}')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))


def stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(stable(value).encode()).hexdigest()


def child(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f'Missing or unsafe suite file: {name}')
    return path


def aware(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('reference_datetime must include a timezone')
    return stamp


def validate_value(value: Any, spec: dict, path: str) -> None:
    """Small documented slot-schema dialect, not an implementation of JSON Schema."""
    types = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
             'object': dict, 'array': list, 'null': type(None)}
    requested = spec.get('type')
    if requested:
        choices = requested if isinstance(requested, list) else [requested]
        if any(t not in types for t in choices):
            raise ValueError(f'{path}: unknown slot schema type')
        if not any(isinstance(value, types[t]) and not (isinstance(value, bool) and t in {'integer', 'number'}) for t in choices):
            raise ValueError(f'{path}: expected {requested}')
    if 'enum' in spec and value not in spec['enum']:
        raise ValueError(f'{path}: value is not in enum')
    if value is not None and spec.get('format') == 'date':
        date.fromisoformat(value)
    if value is not None and spec.get('format') == 'time':
        time.fromisoformat(value)
    if isinstance(value, dict):
        for key in spec.get('required', []):
            if key not in value:
                raise ValueError(f'{path}.{key}: required slot missing')
        props = spec.get('properties', {})
        if spec.get('additionalProperties') is False and set(value) - set(props):
            raise ValueError(f'{path}: unknown slot')
        for key, sub in props.items():
            if key in value:
                validate_value(value[key], sub, f'{path}.{key}')
    if isinstance(value, list) and 'items' in spec:
        for i, item in enumerate(value):
            validate_value(item, spec['items'], f'{path}[{i}]')


@dataclass
class Suite:
    name: str
    path: Path
    manifest: dict
    schema: dict
    fixtures: dict
    cases: list[dict]
    projection: dict
    digest: str
    warnings: list[str]

    def select(self, *, categories=(), tags=(), ids=(), source_type=None, split=None):
        return [c for c in self.cases
                if (not ids or c['id'] in ids)
                and (not tags or set(tags) <= set(c.get('tags', [])))
                and (not categories or category(c) in categories)
                and (not source_type or c['source_type'] == source_type)
                and (not split or c.get('split', 'development') == split)]


def category(case: dict) -> str:
    return case.get('category') or case.get('variant_style') or 'uncategorized'


def list_suites(root=DEFAULT_ROOT):
    root = Path(root)
    return sorted(p.parent.name for p in root.glob('*/manifest.json'))


def load_suite(name: str, root=DEFAULT_ROOT) -> Suite:
    if not NAME.fullmatch(name):
        raise ValueError('Invalid suite name')
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Suite escapes root')
    manifest = json_read(child(path, 'manifest.json'))
    if not isinstance(manifest.get('version'), str) or not manifest['version'].strip():
        raise ValueError('Suite version is required')
    aware(manifest['reference_datetime'])
    ZoneInfo(manifest.get('timezone', 'UTC'))
    schema = json_read(child(path, manifest.get('schema_file', 'schema.json')))
    fixtures = json_read(child(path, manifest.get('fixtures_file', 'fixtures.json')))
    projection = json_read(child(path, manifest['projection_file'])) if manifest.get('projection_file') else {}
    files = manifest.get('case_files', ['cases.jsonl'])
    if not files or not isinstance(files, list):
        raise ValueError('case_files must be a nonempty list')
    rows = []
    for filename in files:
        p = child(path, filename)
        if p.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Case file exceeds 32 MiB')
        for n, line in enumerate(p.read_text(encoding='utf-8-sig').splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=reject_duplicate_keys, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                if not isinstance(row, dict):
                    raise ValueError('case must be an object')
                rows.append(row)
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{filename}:{n}: {exc}') from exc
    if len(rows) > 100000:
        raise ValueError('Suite exceeds 100000 cases')
    by_id, inputs, counts = {}, set(), {}
    required = set(schema.get('required_fields', [])) | {'id', 'input_text', 'expected', 'source_type'}
    fixture_ids = set()
    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str):
                fixture_ids.add(value['id'])
            for v in value.values(): collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(fixtures)
    sessions = {}
    for case in rows:
        missing = required - set(case)
        if missing:
            raise ValueError(f'Missing fields {sorted(missing)}')
        identity = case['id']
        if not isinstance(identity, str) or not identity or identity in by_id:
            raise ValueError(f'Duplicate/invalid case ID: {identity}')
        if not isinstance(case['input_text'], str) or not case['input_text'].strip():
            raise ValueError(f'{identity}: empty input')
        norm = unicodedata.normalize('NFC', ' '.join(case['input_text'].split()))
        if norm in inputs:
            raise ValueError(f'{identity}: duplicate input')
        inputs.add(norm); by_id[identity] = case
        if case['source_type'] not in {'unique', 'paraphrase'}:
            raise ValueError(f'{identity}: invalid source_type')
        counts[case['source_type']] = counts.get(case['source_type'], 0) + 1
        if not isinstance(case.get('tags', []), list) or not all(isinstance(t, str) for t in case.get('tags', [])):
            raise ValueError(f'{identity}: invalid tags')
        aware(case.get('reference_datetime', manifest['reference_datetime']))
        expected = case['expected']
        if not isinstance(expected, dict) or not CAPABILITY.fullmatch(expected.get('capability', '')):
            raise ValueError(f'{identity}: invalid capability')
        for k in ('domain', 'policy', 'status'):
            if not isinstance(expected.get(k), str):
                raise ValueError(f'{identity}: expected.{k} required')
        if expected['policy'] not in {'EXECUTE', 'CONFIRM', 'CLARIFY', 'CANCEL'}:
            raise ValueError(f'{identity}: invalid policy')
        if type(expected.get('adapter_should_execute')) is not bool or not isinstance(expected.get('slots'), dict):
            raise ValueError(f'{identity}: invalid slots/execution annotation')
        if type(expected.get('route_strict', False)) is not bool:
            raise ValueError(f'{identity}: invalid route_strict')
        if expected['status'] not in schema.get('statuses', ['success', 'needs_confirmation', 'needs_clarification', 'cancelled', 'unsupported', 'error', 'unavailable', 'not_found']):
            raise ValueError(f'{identity}: invalid expected status')
        if not isinstance(expected.get('actions', []), list):
            raise ValueError(f'{identity}: expected actions must be a list')
        for action in expected.get('actions', []):
            if not isinstance(action, dict) or not CAPABILITY.fullmatch(action.get('capability', '')) or not isinstance(action.get('slots'), dict):
                raise ValueError(f'{identity}: malformed expected action')
        stable(expected)
        slot_spec = schema.get('slot_schemas', {}).get(expected['capability'], {'type': 'object'})
        validate_value(expected['slots'], slot_spec, identity + '.slots')
        for slot in schema.get('fixture_slot_references', []):
            values = expected['slots'].get(slot)
            for v in values if isinstance(values, list) else [values]:
                if v is not None and v not in fixture_ids:
                    raise ValueError(f'{identity}: missing fixture ID for {slot}: {v}')
        if 'fixture_id' in case and case['fixture_id'] not in fixtures.get('snapshots', {}):
            raise ValueError(f'{identity}: missing fixture snapshot')
        if 'session_id' in case:
            sid, turn = case['session_id'], case.get('turn')
            if not isinstance(sid, str) or not sid or type(turn) is not int or turn < 1:
                raise ValueError(f'{identity}: session requires a string ID and positive turn')
            if turn in sessions.setdefault(sid, set()):
                raise ValueError(f'{identity}: duplicate session turn')
            sessions[sid].add(turn)
    for case in rows:
        parent = case.get('parent_id')
        if case['source_type'] == 'paraphrase':
            if parent not in by_id or by_id[parent]['source_type'] != 'unique':
                raise ValueError(f'{case["id"]}: invalid parent_id')
        elif parent is not None:
            raise ValueError(f'{case["id"]}: unique case cannot have parent_id')
    for field, actual in [('case_count', len(rows)), ('unique_instruction_count', counts.get('unique', 0)), ('paraphrase_count', counts.get('paraphrase', 0))]:
        if field in manifest and manifest[field] != actual:
            raise ValueError(f'{field}: manifest {manifest[field]}, actual {actual}')
    warnings = []
    if not schema.get('slot_schemas'):
        warnings.append('No per-capability slot schemas: only structural slot validation is available.')
    registry = None
    registry_file = manifest.get('registry_file')
    if registry_file:
        registry = json_read(child(path, registry_file))
        observed = registry.get('counts', {})
        for key, value in [('registered_unique_tasks', counts.get('unique', 0)),
                           ('linked_paraphrases', counts.get('paraphrase', 0)),
                           ('unique_capabilities', len({c['expected']['capability'] for c in rows}))]:
            if observed.get(key) != value:
                raise ValueError(f'Registry count mismatch: {key}')
    digest = fingerprint({'manifest': manifest, 'schema': schema, 'fixtures': fixtures,
                          'cases': rows, 'projection': projection, 'registry': registry})
    return Suite(name, path, manifest, schema, fixtures, rows, projection, digest, warnings)
