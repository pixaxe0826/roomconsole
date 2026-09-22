"""Bounded countdowns. SQLite deadlines, not browser intervals, are authoritative.

Only timer controls are immediate. Paired displays gain no other write authority.
No subprocesses, OS alarm integration, model calls, or operational config changes.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import uid

MAX_ACTIVE = 32
MAX_HISTORY = 256
MAX_RECEIPTS = 10000
LOG = logging.getLogger(__name__)


def stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class TimerInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    duration_seconds: int = Field(ge=1, le=600)
    label: str = Field(default='타이머', min_length=1, max_length=80)

    @field_validator('label')
    @classmethod
    def clean_label(cls, value):
        if not value.strip():
            raise ValueError('타이머 이름을 입력하세요.')
        return value.strip()


class TimerStart(TimerInput):
    # UI instance identity, deliberately absent from the model's TimerInput schema.
    widget_id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')


class TimerStop(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    widget_id: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    version: int | None = Field(default=None, ge=1)


class TimerService:
    def __init__(self, store, changed, *, clock=time.time):
        self.store, self.changed, self.clock = store, changed, clock
        self.scheduler_error = False
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_timer_meta(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO hub_timer_meta VALUES(1,0);
                CREATE TABLE IF NOT EXISTS hub_timers(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    label TEXT NOT NULL, duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 1 AND 600),
                    state TEXT NOT NULL CHECK(state IN ('running','expired','stopped')),
                    started_at REAL NOT NULL, deadline_at REAL NOT NULL,
                    ended_at REAL, version INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS hub_timers_due ON hub_timers(state,deadline_at);
                CREATE TABLE IF NOT EXISTS hub_timer_requests(
                    request_key TEXT PRIMARY KEY, signature TEXT NOT NULL, response TEXT NOT NULL);
            ''')
            # Additive migration: preserve IDs, deadlines and durable receipts.
            db.execute('BEGIN IMMEDIATE')
            if 'widget_id' not in {r['name'] for r in db.execute('PRAGMA table_info(hub_timers)')}:
                db.execute('ALTER TABLE hub_timers ADD COLUMN widget_id TEXT')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS hub_timer_widget_running ON hub_timers(widget_id) WHERE state='running' AND widget_id IS NOT NULL")
        self.reconcile_widgets()

    @staticmethod
    def _widgets(db):
        row = db.execute("SELECT value FROM kv WHERE key='layout'").fetchone()
        layout = json.loads(row['value']) if row else {'widgets': []}
        return sorted((w for w in layout['widgets'] if w.get('type') == 'timers'),
                      key=lambda w: (w.get('y', 0), w.get('x', 0), w['id']))

    def reconcile_widgets(self):
        """On startup/admin layout write, bind legacy active runs once to empty slots.

        Never move a bound timer or change its deadline. Overflow/removed widgets
        remain visible as recovery rows rather than being silently stopped/lost.
        """
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            widgets = self._widgets(db)
            if not widgets:
                return 0
            self._expire(db, self.clock())
            busy = {r[0] for r in db.execute("SELECT widget_id FROM hub_timers WHERE state='running' AND widget_id IS NOT NULL")}
            free = [w for w in widgets if w['id'] not in busy]
            legacy = db.execute("SELECT id FROM hub_timers WHERE state='running' AND widget_id IS NULL ORDER BY sequence").fetchall()
            assigned = 0
            for widget, row in zip(free, legacy):
                db.execute('UPDATE hub_timers SET widget_id=? WHERE id=?', (widget['id'], row['id']))
                assigned += 1
            if assigned:
                self._bump(db)
            return assigned

    @contextmanager
    def db(self):
        with closing(self.store.connect()) as db:
            with db:
                yield db

    @staticmethod
    def _bump(db):
        db.execute('UPDATE hub_timer_meta SET revision=revision+1 WHERE id=1')

    def _expire(self, db, at):
        rows = db.execute("SELECT id FROM hub_timers WHERE state='running' AND deadline_at<=?", (at,)).fetchall()
        if rows:
            db.execute("UPDATE hub_timers SET state='expired',ended_at=deadline_at,version=version+1 WHERE state='running' AND deadline_at<=?", (at,))
            self._bump(db)
        return [row['id'] for row in rows]

    def tick(self):
        at = self.clock()
        with self.db() as db:
            # Idle countdowns never acquire a write lock or update each second.
            if db.execute("SELECT 1 FROM hub_timers WHERE state='running' AND deadline_at<=? LIMIT 1", (at,)).fetchone() is None:
                return []
            db.execute('BEGIN IMMEDIATE')
            return self._expire(db, at)

    @staticmethod
    def _data(row, at):
        running = row['state'] == 'running' and row['deadline_at'] > at
        state = row['state'] if row['state'] != 'running' or running else 'expired'
        ended = row['ended_at'] if row['ended_at'] is not None else (row['deadline_at'] if state == 'expired' else None)
        return {'id': row['id'], 'widget_id': row['widget_id'], 'label': row['label'], 'duration_seconds': row['duration_seconds'],
                'state': state, 'started_at': stamp(row['started_at']), 'deadline_at': stamp(row['deadline_at']),
                'ended_at': stamp(ended) if ended is not None else None, 'version': row['version'],
                'remaining_seconds': max(0, min(row['duration_seconds'], math.ceil(row['deadline_at']-at))) if running else 0}

    def snapshot(self):
        at = self.clock()
        with self.db() as db:
            # One consistent read transaction; never hide a row with a LIMIT.
            db.execute('BEGIN')
            rows = db.execute("SELECT * FROM hub_timers ORDER BY CASE WHEN state='running' AND deadline_at>? THEN 0 ELSE 1 END,sequence DESC", (at,)).fetchall()
            revision = db.execute('SELECT revision FROM hub_timer_meta WHERE id=1').fetchone()[0]
        items = [self._data(r, at) for r in rows]
        active = [x for x in items if x['state'] == 'running']
        return {'revision': revision, 'server_time': stamp(at), 'items': items,
                'current_id': active[0]['id'] if active else None, 'active_count': len(active),
                'max_active': MAX_ACTIVE, 'scheduler_error': self.scheduler_error,
                'delivery': 'foreground_browser_only'}

    def get(self, identity):
        with self.db() as db:
            row = db.execute('SELECT * FROM hub_timers WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, '타이머가 없습니다.')
        return self._data(row, self.clock())

    @staticmethod
    def _key(principal, request_id):
        return hashlib.sha256((principal + '\0' + request_id).encode()).hexdigest()

    def _replay(self, db, key, signature, at):
        old = db.execute('SELECT * FROM hub_timer_requests WHERE request_key=?', (key,)).fetchone()
        if old:
            if old['signature'] != signature:
                raise HTTPException(409, '같은 요청 키의 내용이 다릅니다.')
            result = json.loads(old['response'])
            row = db.execute('SELECT * FROM hub_timers WHERE id=?', (result['id'],)).fetchone()
            if row:
                result.update(self._data(row, at))
            return result | {'duplicate': True, 'changed': False}
        if db.execute('SELECT count(*) FROM hub_timer_requests').fetchone()[0] >= MAX_RECEIPTS:
            raise HTTPException(503, '타이머 요청 기록 용량을 확인하세요. 자동으로 재시도하지 않습니다.')
        return None

    @staticmethod
    def _save(db, key, signature, result):
        db.execute('INSERT INTO hub_timer_requests VALUES(?,?,?)', (key, signature, json.dumps(result, ensure_ascii=False)))

    def start(self, body: TimerStart, principal: str):
        key = self._key(principal, body.request_id)
        # Keep the old unscoped signature compatible with pre-migration receipts.
        sig = ['start', body.duration_seconds, body.label]
        if body.widget_id is not None:
            sig.append(body.widget_id)
        signature = json.dumps(sig, ensure_ascii=False)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            at = self.clock()
            old = self._replay(db, key, signature, at)
            if old is not None:
                return old
            self._expire(db, at)
            if db.execute("SELECT count(*) FROM hub_timers WHERE state='running'").fetchone()[0] >= MAX_ACTIVE:
                raise HTTPException(429, f'동시에 실행할 수 있는 타이머는 {MAX_ACTIVE}개입니다.')
            widgets = self._widgets(db)
            widget_id = body.widget_id
            busy = {r[0] for r in db.execute("SELECT widget_id FROM hub_timers WHERE state='running'")}
            if widget_id is not None:
                if widget_id not in {w['id'] for w in widgets}:
                    raise HTTPException(409, '해당 타이머 위젯이 배치에서 제거되었습니다. 화면을 새로 확인하세요.')
                if widget_id in busy:
                    raise HTTPException(409, '이 위젯의 타이머가 이미 실행 중입니다. 종료한 뒤 다시 시작하세요.')
            elif widgets:
                # Existing voice/Protocol requests use the first idle placed widget;
                # never overwrite another countdown or create a hidden extra run.
                widget_id = next((w['id'] for w in widgets if w['id'] not in busy), None)
                if widget_id is None:
                    raise HTTPException(409, '모든 타이머 위젯이 실행 중입니다. 빈 위젯을 추가하거나 타이머 하나를 종료하세요.')
            # No layout: retain trusted legacy service/Protocol compatibility.
            label = body.label
            if widget_id is not None and label == '타이머':
                index, widget = next((i, w) for i, w in enumerate(widgets, 1) if w['id'] == widget_id)
                label = widget.get('title') or f'타이머 {index}'
            identity = uid()
            db.execute('INSERT INTO hub_timers(id,label,duration_seconds,state,started_at,deadline_at,ended_at,version,widget_id) VALUES(?,?,?,?,?,?,NULL,1,?)',
                       (identity, label, body.duration_seconds, 'running', at, at+body.duration_seconds, widget_id))
            # Keep recent history plus the last run of each currently placed widget.
            # That run restores its duration after reload. Removed slots add no unbounded retention.
            ids = [w['id'] for w in widgets]
            marks = ','.join('?' for _ in ids) or 'NULL'
            db.execute("DELETE FROM hub_timers WHERE state!='running' AND sequence NOT IN (SELECT sequence FROM hub_timers ORDER BY sequence DESC LIMIT ?) "
                       + f"AND sequence NOT IN (SELECT MAX(sequence) FROM hub_timers WHERE widget_id IN ({marks}) GROUP BY widget_id)", (MAX_HISTORY, *ids))
            self._bump(db)
            result = self._data(db.execute('SELECT * FROM hub_timers WHERE id=?', (identity,)).fetchone(), at) | {'changed': True, 'duplicate': False}
            self._save(db, key, signature, result)
        return result

    def stop(self, target: str, body: TimerStop, principal: str):
        """Resolve current inside the write transaction AFTER replay lookup.

        A retry of 'current' must never stop the next running timer.
        """
        key = self._key(principal, body.request_id)
        sig = ['stop', target, body.version]
        if body.widget_id is not None:
            sig.append(body.widget_id)
        signature = json.dumps(sig)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            at = self.clock()
            old = self._replay(db, key, signature, at)
            if old is not None:
                return old
            self._expire(db, at)
            if target == 'current':
                if body.widget_id is not None:
                    row = db.execute("SELECT * FROM hub_timers WHERE state='running' AND widget_id=?", (body.widget_id,)).fetchone()
                else:
                    row = db.execute("SELECT * FROM hub_timers WHERE state='running' ORDER BY sequence DESC LIMIT 1").fetchone()
            else:
                row = db.execute('SELECT * FROM hub_timers WHERE id=?', (target,)).fetchone()
            if row is None:
                raise HTTPException(404, '실행 중인 현재 타이머가 없습니다.' if target == 'current' else '타이머가 없습니다.')
            if body.widget_id is not None and row['widget_id'] != body.widget_id:
                raise HTTPException(409, '다른 위젯의 타이머는 변경하지 않았습니다.')
            if body.version is not None and row['version'] != body.version and row['state'] == 'running':
                raise HTTPException(409, '타이머 상태가 바뀌었습니다. 다시 확인하세요.')
            changed = row['state'] == 'running'
            if changed:
                db.execute("UPDATE hub_timers SET state='stopped',ended_at=?,version=version+1 WHERE id=?", (at, row['id']))
                self._bump(db)
            result = self._data(db.execute('SELECT * FROM hub_timers WHERE id=?', (row['id'],)).fetchone(), at) | {'changed': changed, 'duplicate': False}
            self._save(db, key, signature, result)
        return result

    async def announce(self, event, identity):
        # Notification failure cannot roll back committed state or invite a duplicate start.
        try:
            await self.changed(event, identity)
        except Exception:
            LOG.warning('Timer notification failed; refresh the timer state.')

    async def run(self):
        while True:
            try:
                ids = await asyncio.to_thread(self.tick)
                self.scheduler_error = False
                if ids:
                    await self.announce('timer.expired', ','.join(ids))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.scheduler_error = True
                LOG.warning('Timer scheduler failed; state remains persisted.')
            await asyncio.sleep(0.25)

    def visible(self):
        return any(w.get('type') == 'timers' for w in self.store.get('layout')['widgets'])

    def display(self):
        if self.visible():
            return self.snapshot()
        return {'items': [], 'current_id': None, 'active_count': 0, 'revision': 0,
                'server_time': stamp(self.clock()), 'max_active': MAX_ACTIVE,
                'scheduler_error': False, 'delivery': 'foreground_browser_only'}


def register_routes(app, timers: TimerService, viewer):
    def operator(session=Depends(viewer)):
        if session['role'] != 'admin' and not timers.visible():
            raise HTTPException(403, '타이머 위젯을 먼저 배치해 주세요.')
        return 'admin' if session['role'] == 'admin' else 'display:' + session['device_id']

    @app.get('/api/timers')
    async def listing(_=Depends(operator)):
        return await asyncio.to_thread(timers.snapshot)

    @app.post('/api/timers/start')
    async def start(body: TimerStart, principal=Depends(operator)):
        result = await asyncio.to_thread(timers.start, body, principal)
        if result['changed']:
            await timers.announce('timer.started', result['id'])
        return result | {'snapshot': await asyncio.to_thread(timers.snapshot)}

    @app.post('/api/timers/{target}/stop')
    async def stop(target: str, body: TimerStop, principal=Depends(operator)):
        if len(target) > 128:
            raise HTTPException(422, '타이머 ID를 확인하세요.')
        result = await asyncio.to_thread(timers.stop, target, body, principal)
        if result['changed']:
            await timers.announce('timer.stopped', result['id'])
        return result | {'snapshot': await asyncio.to_thread(timers.snapshot)}
