"""Durable, principal-scoped audio ingress; never re-dispatch an uncertain STT call."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json

from fastapi import HTTPException

from .store import utcnow


class AudioIngress:
    def __init__(self, store):
        self.store = store
        self.ready = False
        self.inflight: dict[str, asyncio.Event] = {}

    def _ensure_schema(self):
        if self.ready:
            return
        with closing(self.store.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS hub_agent_audio (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(principal, request_id))''')
            # A new service instance represents restart, not permission to resend.
            db.execute("UPDATE hub_agent_audio SET status='interrupted',updated_at=? WHERE status='running'", (utcnow(),))
        self.ready = True

    def _result(self, identity):
        with closing(self.store.connect()) as db:
            row = db.execute('SELECT status,result_json FROM hub_agent_audio WHERE id=?', (identity,)).fetchone()
        if row and row['status'] == 'succeeded' and row['result_json']:
            return json.loads(row['result_json'])
        raise HTTPException(409, '이 음성 요청은 처리 중이거나 중단되어 재전송하지 않습니다. 기존 기록을 확인하고 필요하면 새 요청 ID를 사용하세요.')

    async def transcribe_once(self, *, principal, request_id, audio_bytes, options, transcribe, authorize):
        """Reserve before conversion/upload; callback receives a unique provider key."""
        authorize()
        self._ensure_schema()
        identity = hashlib.sha256(json.dumps([principal, request_id], separators=(',', ':')).encode()).hexdigest()
        fingerprint = hashlib.sha256(audio_bytes + b'\0' + json.dumps(options, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM hub_agent_audio WHERE id=?', (identity,)).fetchone()
            if old and old['fingerprint'] != fingerprint:
                raise HTTPException(409, '동일한 음성 요청 ID의 녹음 또는 언어/실행 모드가 다릅니다.')
            if not old:
                now = utcnow()
                db.execute("INSERT INTO hub_agent_audio VALUES(?,?,?,?,'running',NULL,?,?)",
                           (identity, principal, request_id, fingerprint, now, now))
        if old:
            pending = self.inflight.get(identity)
            if pending:
                await pending.wait()
            authorize()
            return self._result(identity), True

        done = self.inflight[identity] = asyncio.Event()
        try:
            authorize()
            result = await transcribe('stt:' + identity)
            authorize()
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            with closing(self.store.connect()) as db, db:
                db.execute("UPDATE hub_agent_audio SET status='succeeded',result_json=?,updated_at=? WHERE id=?",
                           (encoded, utcnow(), identity))
            return result, False
        except BaseException:
            with closing(self.store.connect()) as db, db:
                db.execute("UPDATE hub_agent_audio SET status='interrupted',updated_at=? WHERE id=?", (utcnow(), identity))
            raise
        finally:
            done.set()
            self.inflight.pop(identity, None)
