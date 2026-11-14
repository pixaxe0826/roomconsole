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


def _agent_run_private(store, db, row: dict) -> tuple[bool, bool]:
    """Return `(is_private_write, is_unshared_read)` for an agent run in `hub_agent_runs`."""
    preview = _object(row.get('preview_json'))
    for mut in preview.get('mutations', []):
        if mut.get('domain') != 'memo':
            continue
        req = mut.get('widget_request') if isinstance(mut.get('widget_request'), dict) else {}
        target = req.get('target') if isinstance(req.get('target'), dict) else None
        args = req.get('args') if isinstance(req.get('args'), dict) else {}
        if target and target.get('value'):
            cur = db.execute('SELECT shared FROM hub_notes WHERE id=?', (target['value'],)).fetchone()
            if not cur or not cur['shared'] or args.get('shared') is False:
                return True, False
        elif not bool(args.get('shared')):
            return True, False

    for rd in preview.get('reads_executed', []):
        if rd.get('domain') != 'memo':
            continue
        data = rd.get('data') if isinstance(rd.get('data'), dict) else {}
        if rd.get('action') == 'list':
            return False, True
        if not bool(data.get('shared')):
            return False, True
    return False, False


def enabled(store) -> bool:
    layout = store.get('layout') or {}
    return any(w.get('type') == WIDGET_TYPE for w in layout.get('widgets', []))


def index(store) -> dict:
    if not enabled(store):
        return {'enabled': False, 'items': [], 'total': 0, 'count': 0, 'latest_id': None}
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
                """SELECT id, status, created_at, updated_at, tier_used, model_used
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
                        'source': 'personal_agent',
                        'route': r['tier_used'],
                        'model': r['model_used'],
                    }
                )

    items.sort(key=lambda x: (str(x.get('sent_at') or ''), str(x.get('id') or '')))
    latest_id = items[-1]['id'] if items else None
    return {
        'enabled': True,
        'items': items,
        'total': len(items),
        'count': len(items),
        'latest_id': latest_id,
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
            agent = _object(agent_row[0]) if agent_row else {}
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
            return {
                'id': row['id'],
                'status': row['status'],
                'sent_at': row['started_at'],
                'created_at': row['started_at'],
                'updated_at': row['updated_at'],
                'finished_at': row['finished_at'],
                'model': cfg.get('model') if isinstance(cfg.get('model'), str) else '',
                'input': input_text,
                'output': output,
                'input_text': input_text or '',
                'output_text': output or '',
                'refusal': refusal,
                'output_kind': kind,
                'truncated': result.get('finish_reason') == 'length',
            }

        if _table_exists(db, 'hub_agent_runs'):
            arow_raw = db.execute('SELECT * FROM hub_agent_runs WHERE id=?', (rid,)).fetchone()
            if arow_raw is not None:
                arow = dict(arow_raw)
                priv_write, priv_read = _agent_run_private(store, db, arow)
                mapped_status = _map_agent_status(arow['status'])
                input_text = str(arow.get('source_text') or '')
                output_text = str(arow.get('summary_text') or '')
                if arow['status'] == 'awaiting_confirmation' and not (priv_write or priv_read):
                    if '[관리자 승인 대기' not in output_text:
                        output_text = f"{output_text}\n[관리자 승인 대기: 관리자 화면에서 확인 후 반영됩니다.]"
                if priv_write:
                    input_text = '메모 변경 요청'
                    output_text = '이 메모 변경 요청과 결과는 관리자 기록에서 확인해 주세요.'
                elif priv_read:
                    input_text = '메모 조회'
                    output_text = '이 메모 조회 결과는 관리자 기록에서 확인해 주세요. 현재 공유되지 않은 메모 내용은 표시하지 않습니다.'
                timestamps = _object(arow.get('timestamps_json'))
                cost_micros = int(arow.get('cost_microdollars') or 0)
                model_label = str(arow.get('model_used') or '')
                tier_label = str(arow.get('tier_used') or '')
                if tier_label and model_label and tier_label not in model_label:
                    display_model = f'{model_label} · {tier_label}'
                else:
                    display_model = model_label or tier_label
                return {
                    'id': arow['id'],
                    'source': 'personal_agent',
                    'status': mapped_status,
                    'agent_status': arow['status'],
                    'sent_at': arow['created_at'],
                    'created_at': arow['created_at'],
                    'updated_at': arow['updated_at'],
                    'finished_at': timestamps.get('executed_at') or timestamps.get('planned_at') or arow['updated_at'],
                    'model': display_model,
                    'route': tier_label or str(arow.get('mode') or ''),
                    'cost_microdollars': cost_micros,
                    'cost_usd': round(cost_micros / 1_000_000, 6),
                    'input': input_text,
                    'output': output_text if output_text else None,
                    'input_text': input_text,
                    'output_text': output_text,
                    'response_text': output_text,
                    'redacted': bool(priv_write or priv_read),
                    'refusal': None,
                    'output_kind': 'text' if output_text else 'empty',
                    'truncated': False,
                }

    raise HTTPException(404, '전송된 LLM 기록이 없거나 삭제되었습니다.')
