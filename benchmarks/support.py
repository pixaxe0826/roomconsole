"""Read-only production contract inspection, isolated from the live application.

Operation availability is not proof that every linguistic form/slot is supported.
No dataset, expected answer, live Store, application lifespan or model is opened.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .dataset import CAPABILITY, fingerprint, stable

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_VERSION = '1.0'
SUPPORT_DEFINITION = 'assistant_exposed_operation_v1'


def production_hash(root: Path = ROOT) -> str:
    """Byte-for-byte compatible with the M1 runner's production_source_hash."""
    digest = hashlib.sha256()
    for path in sorted((root / 'app').rglob('*.py')):
        if '__pycache__' not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _inspect() -> dict:
    # Executed only in a dedicated short-lived process, not the server or runner.
    import socket
    import sqlite3
    from unittest.mock import patch

    def denied(*args, **kwargs):
        raise RuntimeError('Support inspection cannot access SQLite or network')

    class MetadataOnly:
        def __getattr__(self, name):
            raise RuntimeError('Support inspection cannot call a service: ' + name)

    before = production_hash()
    with patch.object(sqlite3, 'connect', denied), \
         patch.object(socket.socket, 'connect', denied), \
         patch.object(socket.socket, 'connect_ex', denied), \
         patch.object(socket, 'create_connection', denied):
        from app.capabilities import REGISTRY
        from app.widget_protocol.adapters import TaskServices, build_registry
        from app.widget_bridge import ACTION_WORDS, DOMAIN_WORDS, BRIDGE_VERSION

        inert = MetadataOnly()
        registry = build_registry(inert, inert, denied,
                                  TaskServices(denied, denied, denied, denied), timers=inert)
        catalog = registry.manifest()
        operations = []
        for widget in catalog['widgets']:
            domain = widget['name']
            for cap in widget['capabilities']:
                native = domain + '.' + cap['action']
                exposed = domain in DOMAIN_WORDS and cap['action'] in ACTION_WORDS
                operations.append({
                    'native': native, 'source': 'WidgetRegistry',
                    'availability': cap['availability'], 'exposed': exposed,
                    'read_only': cap['read_only'],
                    'requires_confirmation': cap['requires_confirmation'],
                    'permission': cap['permission_level'], 'target_types': cap['target_types'],
                    'target_required': cap['target_required'],
                    'assistant_capability': cap['assistant_capability'],
                    'input_schema': cap['input_schema'], 'output_schema': cap['output_schema'],
                })
        # This older registry is additive. Its stale manifest.unavailable list must
        # not override the newer Bridge's registered note/alarm operations.
        for name, cap in sorted(REGISTRY.items()):
            operations.append({
                'native': name, 'source': 'AssistantRegistry',
                'availability': 'implemented', 'exposed': True,
                'read_only': cap.access == 'read', 'requires_confirmation': cap.confirmation,
                'permission': cap.access, 'input_fields': list(cap.input_fields),
                'handler': cap.handler,
            })
        if 'app.main' in sys.modules:
            raise RuntimeError('Support probe unexpectedly imported the live app factory')
    if production_hash() != before:
        raise ValueError('Production source changed during support inspection')
    body = {
        'snapshot_version': SNAPSHOT_VERSION, 'definition': SUPPORT_DEFINITION,
        'production_source_hash': before, 'bridge_version': BRIDGE_VERSION,
        'operations': sorted(operations, key=lambda c: (c['native'], c['source'])),
        'widget_catalog_hash': fingerprint(catalog),
        'scope': 'Declared available Assistant operations, not UI/DB feature or utterance coverage.',
        'io': {'network_calls': 0, 'sqlite_connections': 0, 'app_main_imported': False},
    }
    return dict(body, snapshot_hash=fingerprint(body))


def validate_snapshot(snapshot: dict, expected_production_hash: str) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError('Invalid support snapshot')
    body = {k: v for k, v in snapshot.items() if k != 'snapshot_hash'}
    if (snapshot.get('snapshot_version') != SNAPSHOT_VERSION or
            snapshot.get('definition') != SUPPORT_DEFINITION or
            snapshot.get('snapshot_hash') != fingerprint(body)):
        raise ValueError('Invalid support snapshot version/hash')
    if snapshot.get('production_source_hash') != expected_production_hash:
        raise ValueError('Support snapshot production source mismatch')
    ops = snapshot.get('operations')
    if not isinstance(ops, list) or not ops:
        raise ValueError('Support snapshot has no operation evidence')
    keys = set()
    for op in ops:
        if (not isinstance(op, dict) or not isinstance(op.get('native'), str) or
                not CAPABILITY.fullmatch(op['native']) or type(op.get('exposed')) is not bool or
                op.get('availability') not in {'implemented', 'unavailable'}):
            raise ValueError('Invalid support operation metadata')
        key = (op['native'], op.get('source'))
        if key in keys:
            raise ValueError('Duplicate support operation')
        keys.add(key)
    return snapshot


def capture_snapshot() -> dict:
    before = production_hash()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('HUB_', 'ROOM_HUB_BENCHMARK'))
           and k not in {'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONINSPECT'}}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    try:
        result = subprocess.run([sys.executable, '-m', 'benchmarks.support'], cwd=ROOT,
                                env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Production support inspection timed out; no analysis was published') from exc
    if result.returncode:
        raise ValueError('Production support inspection failed: ' + result.stderr[-2000:])
    snapshot = json.loads(result.stdout)
    validate_snapshot(snapshot, before)
    if production_hash() != before:
        raise ValueError('Production source changed during support inspection')
    return snapshot


def projection_receipt(projection: dict) -> dict:
    """Names only; never cases, gold, fixtures or text into a support catalog."""
    body = {key: deepcopy(projection.get(key, {}))
            for key in ('capability_aliases', 'slot_aliases', 'target_slots')}
    return dict(body, receipt_hash=fingerprint(body))


class SupportIndex:
    """Production declarations plus recorded representation aliases, not an NLU."""
    def __init__(self, snapshot: dict, rows=(), projection: dict | None = None):
        self.snapshot = snapshot
        self.entries: dict[str, list[dict]] = {}
        for op in snapshot['operations']:
            self.entries.setdefault(op['native'], []).append(op)
        self.aliases: dict[str, set[str]] = {}
        self.alias_evidence: list[dict] = []
        for op in snapshot['operations']:
            alias = op.get('assistant_capability')
            # Calendar reuses Todo Operation objects. Their todo.* metadata must
            # NEVER turn a calendar operation into a todo alias.
            if alias and alias.split('.')[0] == op['native'].split('.')[0]:
                self._alias(op['native'], alias, 'production_assistant_capability')
        if projection is not None:
            if not isinstance(projection, dict) or not isinstance(projection.get('capability_aliases'), dict):
                raise ValueError('Invalid recorded projection receipt')
            body = {k: v for k, v in projection.items() if k != 'receipt_hash'}
            if projection.get('receipt_hash') != fingerprint(body):
                raise ValueError('Recorded projection receipt mismatch')
            for native, canonical in projection.get('capability_aliases', {}).items():
                self._alias(native, canonical, 'recorded_projection')
        else:
            # M1 omitted its projection config. An OBSERVED alias is representation
            # evidence only; do not infer aliases from expected/gold or input text.
            observed = {}
            for row in rows:
                actual = row.get('actual') or {}
                native, canonical = actual.get('native_capability'), actual.get('capability')
                if native and canonical and native != canonical:
                    if native in observed and observed[native] != canonical:
                        raise ValueError('Conflicting recorded capability aliases')
                    observed[native] = canonical
            for native, canonical in sorted(observed.items()):
                self._alias(native, canonical, 'observed_m1_projection')

    def _alias(self, native, canonical, source):
        if not all(isinstance(v, str) and CAPABILITY.fullmatch(v) for v in (native, canonical)):
            raise ValueError('Invalid recorded capability alias')
        if native == canonical:
            return
        self.aliases.setdefault(canonical, set()).add(native)
        self.aliases.setdefault(native, set()).add(canonical)
        item = {'native': native, 'canonical': canonical, 'source': source}
        if item not in self.alias_evidence:
            self.alias_evidence.append(item)

    def equivalents(self, name):
        if not isinstance(name, str):
            return set()
        seen, queue = set(), [name]
        while queue:
            value = queue.pop()
            if value not in seen:
                seen.add(value)
                queue.extend(self.aliases.get(value, ()))
        return seen

    def equivalent(self, left, right):
        return left is not None and right is not None and bool(self.equivalents(left) & self.equivalents(right))

    def lookup(self, capability):
        candidates = [op for name in sorted(self.equivalents(capability)) for op in self.entries.get(name, [])]
        implemented = [op for op in candidates if op['exposed'] and op['availability'] == 'implemented']
        if implemented:
            state, reason = 'SUPPORTED', 'declared_assistant_operation'
        elif any(op['exposed'] for op in candidates):
            state, reason = 'UNSUPPORTED', 'declared_but_unavailable'
        elif candidates:
            state, reason = 'UNSUPPORTED', 'not_exposed_to_assistant'
        else:
            state, reason = 'UNSUPPORTED', 'undeclared_as_named_or_recorded_alias'
        return {'status': state, 'reason': reason, 'expected_capability': capability,
                'native_candidates': sorted({op['native'] for op in candidates}),
                'sources': sorted({op['source'] for op in candidates}),
                'scope': 'operation_only; not all parameter, target, language or UI semantics'}


if __name__ == '__main__':
    print(stable(_inspect()))
