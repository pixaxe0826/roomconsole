"""Opt-in read-only projection for the LLM reply widget (`llm-response`).

Adding `llm-response` to the shared layout explicitly shares USER text and FINAL
output with paired displays from both `llm_requests` and `hub_agent_runs`.
It never shares prompts, raw responses, reasoning, preview_sha256, source-device
metadata, endpoints, keys, or manager action permissions.
No model calls or business database mutations occur on either read path.
"""
from __future__ import annotations

import json
import sqlite3
from fastapi import HTTPException

from .life import memo_result_is_public

WIDGET_TYPE = 'llm-response'


def _table_exists(db, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _object(raw):
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _map_agent_status(status: str) -> str:
    if status == 'completed':
        return 'succeeded'
    if status in {'awaiting_confirmation', 'needs_clarification', 'running', 'failed', 'cancelled', 'interrupted'}:
        return status
    return 'succeeded'


def _saved_object(row: dict, raw_key: str, decoded_key: str) -> dict | None:
    """Accept DB rows and the manager DTO, without treating broken proof as empty."""
    value = row.get(decoded_key) if decoded_key in row else row.get(raw_key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _memo_read_is_public(store, data: dict) -> bool:
    # MemoAdapter uses `id`, whereas the original memo visibility contract uses
    # `note_id`/`count`/`source`. Keep the same live version and placement checks.
    if data.get('shared') is not True:
        return False
    note_id = data.get('id') or data.get('note_id')
    snapshot = dict(data, note_id=note_id, count=1,
                    source='room_hub_sqlite.hub_notes' if note_id else 'room_hub_sqlite.kv.note')
    try:
        return memo_result_is_public(store, snapshot)
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return False


def _legacy_private(store, agent: dict) -> bool:
    if agent.get('protocol_private'):
        return True
    if agent.get('memo_read'):
        try:
            return not memo_result_is_public(store, agent.get('tool_result') or {})
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            return True
    return False


def _referenced_result_private(store, db, identity: str, context: dict) -> bool:
    """Re-evaluate ancestry, never trust a saved public flag on derived text."""
    if not enabled(store) or not isinstance(identity, str) or not identity:
        return True
    if identity in context['visiting'] or len(context['visiting']) >= 32:
        return True
    if identity in context['cache']:
        return context['cache'][identity]
    if len(context['cache']) >= 256:
        return True
    context['visiting'].add(identity)
    private = True
    try:
        row = db.execute('SELECT * FROM hub_agent_runs WHERE id=?', (identity,)).fetchone()
        if row is not None:
            private = any(_agent_run_private(store, db, dict(row), context))
        elif _table_exists(db, 'llm_requests'):
            legacy = db.execute(
                'SELECT started_at,dispatch_attempted,status FROM llm_requests WHERE id=?', (identity,)
            ).fetchone()
            saved = (db.execute('SELECT record_json FROM assistant_runs WHERE request_id=?', (identity,)).fetchone()
                     if _table_exists(db, 'assistant_runs') else None)
            if legacy and legacy['started_at'] is not None and (
                legacy['dispatch_attempted'] or (saved and legacy['status'] not in {'prepared', 'queued'})
            ):
                proof = _saved_object({'raw': saved[0]}, 'raw', 'parsed') if saved else {}
                private = proof is None or _legacy_private(store, proof)
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        private = True
    finally:
        context['visiting'].discard(identity)
    context['cache'][identity] = private
    return private


def _agent_run_private(store, db, row: dict, context: dict | None = None) -> tuple[bool, bool]:
    """Fail closed on missing proof; recheck memo visibility and derived replies now."""
    if context is None:
        context = {'visiting': {str(row.get('id') or '')}, 'cache': {}}
    preview = _saved_object(row, 'preview_json', 'preview')
    if preview is None:
        return False, True
    reads = preview.get('reads_executed')
    mutations = preview.get('mutations')
    if not isinstance(reads, list) or not isinstance(mutations, list):
        return False, True
    for mut in mutations:
        if not isinstance(mut, dict):
            return False, True
        if mut.get('domain') == 'memo':
            # The source/preview can contain an old private body, even after a
            # write sets shared=True. No exact post-write proof is saved here.
            return True, False
    for rd in reads:
        if not isinstance(rd, dict):
            return False, True
        domain = rd.get('domain')
        if domain == 'system':
            # Overview includes private-note counts; device/audit data are
            # manager diagnostics, not shared-display information.
            return False, True
        if domain not in {'memo', 'llm-response'}:
            continue
        data = rd.get('data')
        if not isinstance(data, dict):
            return False, True
        if domain == 'memo':
            if rd.get('action') != 'read' or not _memo_read_is_public(store, data):
                return False, True
        elif rd.get('action') == 'list':
            refs = data.get('recent_agent_runs')
            if not isinstance(refs, list):
                return False, True
            for ref in refs:
                if not isinstance(ref, dict) or ref.get('redacted') or _referenced_result_private(
                    store, db, ref.get('id'), context
                ):
                    return False, True
        elif rd.get('action') in {'read', 'get', 'latest'}:
            if data.get('id') == 'none' and data.get('status') == 'empty':
                continue
            if data.get('redacted') or _referenced_result_private(store, db, data.get('id'), context):
                return False, True
        else:
            return False, True
    return False, False


def project_agent_run(store, row: dict) -> dict:
    """Shared allowlist for widgets, adapters and an authenticated owner's status.

    The caller enforces authentication/ownership. Only `entry` requires the
    llm-response widget, so an owner's status remains available without it.
    Manager history is never modified by this read-only projection.
    """
    with store.connect() as db:
        priv_write, priv_read = _agent_run_private(store, db, row)
    status = str(row.get('status') or 'interrupted')
    input_text = str(row.get('source_text') or '')
    output_text = str(row.get('summary_text') or '')
    if priv_write:
        input_text = '메모 변경 요청'
        output_text = '이 메모 변경 요청과 결과는 관리자 기록에서 확인해 주세요.'
    elif priv_read:
        input_text = '개인 비서 응답'
        output_text = '현재 공유할 수 없는 요청과 결과는 관리자 기록에서 확인해 주세요.'
        if status == 'running':
            output_text = '요청을 처리 중입니다. 검증된 결과가 준비되면 표시합니다.'
        elif status == 'awaiting_confirmation':
            output_text = '관리자 확인을 기다리고 있습니다. 관리자 화면에서 내용을 확인해 주세요.'
    elif status == 'awaiting_confirmation' and '[관리자 승인 대기' not in output_text:
        output_text += '\n[관리자 승인 대기: 관리자 화면에서 확인 후 반영됩니다.]'
    timestamps = _saved_object(row, 'timestamps_json', 'timestamps') or {}
    cost = row.get('cost_microdollars')
    cost_micros = cost if row.get('cost_known', True) and type(cost) is int and cost >= 0 else None
    model_label = str(row.get('model_used') or '')
    tier_label = str(row.get('tier_used') or '')
    display_model = (f'{model_label} · {tier_label}'
                     if tier_label and model_label and tier_label not in model_label else model_label or tier_label)
    return {
        'id': row['id'], 'source': 'personal_agent',
        'status': _map_agent_status(status), 'agent_status': status,
        'sent_at': row.get('created_at'), 'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
        'finished_at': timestamps.get('executed_at') or timestamps.get('planned_at') or row.get('updated_at'),
        'model': display_model, 'route': tier_label or str(row.get('mode') or ''),
        'cost_microdollars': cost_micros,
        'cost_usd': round(cost_micros / 1_000_000, 6) if cost_micros is not None else None,
        'input': input_text, 'output': output_text or None,
        'input_text': input_text, 'output_text': output_text, 'response_text': output_text,
        'redacted': bool(priv_write or priv_read), 'refusal': None,
        'output_kind': 'text' if output_text else 'empty', 'truncated': False,
    }


def enabled(store) -> bool:
    layout = store.get('layout') or {}
    return any(w.get('type') == WIDGET_TYPE for w in layout.get('widgets', []))


def index(store) -> dict:
    if not enabled(store):
        return {'enabled': False, 'items': [], 'total': 0}
    items: list[dict] = []
    with store.connect() as db:
        if _table_exists(db, 'llm_requests'):
            has_assistant_runs = _table_exists(db, 'assistant_runs')
            if has_assistant_runs:
                rows = db.execute(
                    """SELECT id, status, started_at AS sent_at, updated_at
                    FROM llm_requests
                    WHERE started_at IS NOT NULL AND (
                        dispatch_attempted=1 OR (
                            id IN (SELECT request_id FROM assistant_runs)
                            AND status NOT IN ('prepared','queued')
                        )
                    )
                    ORDER BY started_at ASC, id ASC"""
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT id, status, started_at AS sent_at, updated_at
                    FROM llm_requests
                    WHERE started_at IS NOT NULL AND dispatch_attempted=1
                    ORDER BY started_at ASC, id ASC"""
                ).fetchall()
            items.extend(dict(r) for r in rows)

        if _table_exists(db, 'hub_agent_runs'):
            arows = db.execute(
                """SELECT id, status, created_at, updated_at
                FROM hub_agent_runs
                ORDER BY created_at ASC, id ASC"""
            ).fetchall()
            for r in arows:
                items.append(
                    {
                        'id': r['id'],
                        'status': _map_agent_status(r['status']),
                        'sent_at': r['created_at'],
                        'updated_at': r['updated_at'],
                    }
                )

    items.sort(key=lambda x: (str(x.get('sent_at') or ''), str(x.get('id') or '')))
    return {
        'enabled': True,
        'items': items,
        'total': len(items),
    }


def entry(store, rid: str) -> dict:
    if not enabled(store):
        raise HTTPException(403, '관리자가 화면 배치에 LLM 응답 위젯을 추가한 뒤 조회할 수 있습니다.')
    with store.connect() as db:
        row = None
        if _table_exists(db, 'llm_requests'):
            has_assistant_runs = _table_exists(db, 'assistant_runs')
            if has_assistant_runs:
                row = db.execute(
                    """SELECT id, source_text, status, started_at, updated_at,
                        finished_at, config_json, response_json FROM llm_requests
                        WHERE id=? AND started_at IS NOT NULL AND (
                            dispatch_attempted=1 OR (
                                id IN (SELECT request_id FROM assistant_runs)
                                AND status NOT IN ('prepared','queued')
                            )
                        )""",
                    (rid,),
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT id, source_text, status, started_at, updated_at,
                        finished_at, config_json, response_json FROM llm_requests
                        WHERE id=? AND started_at IS NOT NULL AND dispatch_attempted=1""",
                    (rid,),
                ).fetchone()

        if row is not None:
            agent_row = (
                db.execute('SELECT record_json FROM assistant_runs WHERE request_id=?', (rid,)).fetchone()
                if _table_exists(db, 'assistant_runs')
                else None
            )
            agent = (_saved_object({'raw': agent_row[0]}, 'raw', 'parsed')
                     if agent_row else {})
            if agent is None:
                agent = {'protocol_private': True}
            result = _object(row['response_json'])
            cfg = _object(row['config_json'])
            output = result.get('output')
            refusal = result.get('refusal')
            output = output if isinstance(output, str) else None
            refusal = refusal if isinstance(refusal, str) else None
            input_text = row['source_text']
            if agent.get('protocol_private'):
                input_text = '메모 변경 요청'
                output = '이 메모 변경 요청과 결과는 관리자 기록에서 확인해 주세요.'
                refusal = None
            elif agent.get('memo_read'):
                try:
                    visible = memo_result_is_public(store, agent.get('tool_result') or {})
                except (ValueError, TypeError, sqlite3.Error):
                    visible = False
                if not visible:
                    input_text = '메모 조회'
                    output = '이 메모 조회 결과는 관리자 기록에서 확인해 주세요. 현재 공유되지 않은 메모 내용은 표시하지 않습니다.'
                    refusal = None
            if output:
                kind = 'text'
            elif refusal:
                kind = 'refusal'
            elif result.get('tool_calls'):
                kind = 'tool_only'
            elif result.get('reasoning'):
                kind = 'no_final_output'
            else:
                kind = 'empty'
            projected = {
                'id': row['id'],
                'status': row['status'],
                'sent_at': row['started_at'],
                'updated_at': row['updated_at'],
                'finished_at': row['finished_at'],
                'model': cfg.get('model') if isinstance(cfg.get('model'), str) else '',
                'input': input_text,
                'output': output,
                'refusal': refusal,
                'output_kind': kind,
                'truncated': result.get('finish_reason') == 'length',
            }
            # Keep the historical DTO allowlist unchanged for ordinary records;
            # adapters need an explicit marker when a private reply was masked.
            if _legacy_private(store, agent):
                projected['redacted'] = True
            return projected

        if _table_exists(db, 'hub_agent_runs'):
            arow_raw = db.execute('SELECT * FROM hub_agent_runs WHERE id=?', (rid,)).fetchone()
            if arow_raw is not None:
                return project_agent_run(store, dict(arow_raw))

    raise HTTPException(404, '전송된 LLM 기록이 없거나 삭제되었습니다.')
