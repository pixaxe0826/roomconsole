"""Opt-in read-only projection for the LLM reply widget.

Adding llm-response to the shared layout explicitly shares USER text and FINAL
output with all paired displays. It never shares prompts, raw responses, reasoning,
source-device metadata, endpoints, usage, keys, or manager action permissions.
No model calls or database mutations occur on either read path.
"""
from __future__ import annotations
import json
import sqlite3
from .life import memo_result_is_public
from fastapi import HTTPException

WIDGET_TYPE = 'llm-response'


def enabled(store) -> bool:
    layout = store.get('layout') or {}
    return any(w.get('type') == WIDGET_TYPE for w in layout.get('widgets', []))


def index(store) -> dict:
    if not enabled(store):
        return {'enabled': False, 'items': [], 'total': 0}
    # Legacy records need an HTTP attempt. New assistant records can also expose
    # processed local results without falsely claiming an LLM call. Unprocessed
    # prepared/queued records remain manager-only. No approval tokens are exposed.
    with store.connect() as db:
        rows = db.execute("""SELECT id, status, started_at AS sent_at, updated_at
            FROM llm_requests WHERE started_at IS NOT NULL AND (dispatch_attempted=1 OR (id IN (SELECT request_id FROM assistant_runs) AND status NOT IN ('prepared','queued')))
            ORDER BY started_at ASC, id ASC""").fetchall()
    items = [dict(r) for r in rows]
    return {'enabled': True, 'items': items, 'total': len(items)}


def _object(raw):
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def entry(store, rid: str) -> dict:
    if not enabled(store):
        raise HTTPException(403, '관리자가 화면 배치에 LLM 응답 위젯을 추가한 뒤 조회할 수 있습니다.')
    with store.connect() as db:
        row = db.execute("""SELECT id, source_text, status, started_at, updated_at,
            finished_at, config_json, response_json FROM llm_requests
            WHERE id=? AND started_at IS NOT NULL AND (dispatch_attempted=1 OR (id IN (SELECT request_id FROM assistant_runs) AND status NOT IN ('prepared','queued')))""", (rid,)).fetchone()
    if row is None:
        raise HTTPException(404, '전송된 LLM 기록이 없거나 삭제되었습니다.')
    with store.connect() as db:
        agent = db.execute('SELECT record_json FROM assistant_runs WHERE request_id=?', (rid,)).fetchone()
    agent = _object(agent[0]) if agent else {}
    result = _object(row['response_json'])
    cfg = _object(row['config_json'])
    output = result.get('output')
    refusal = result.get('refusal')
    # Never stringify an object that could contain extra/raw provider fields.
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
        'id': row['id'], 'status': row['status'], 'sent_at': row['started_at'],
        'updated_at': row['updated_at'], 'finished_at': row['finished_at'],
        'model': cfg.get('model') if isinstance(cfg.get('model'), str) else '',
        'input': input_text, 'output': output, 'refusal': refusal,
        'output_kind': kind, 'truncated': result.get('finish_reason') == 'length',
    }
