"""Persistent notes and foreground-browser alarms (no native push or ASR changes).

All scheduling decisions belong to SQLite/server time. A ringing row means an
alarm is due, NOT that an iPad has played sound. Writes are manager-only except
for versioned acknowledgement/snooze of an existing occurrence on a paired display.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
import json
import logging
import re
import time
from typing import Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .store import uid

UTC = timezone.utc
RING_SECONDS = 120
LATE_GRACE_SECONDS = 300
SNOOZE_SECONDS = 300
MAX_SNOOZES = 3
MAX_NOTES = 200
MAX_ALARMS = 64
MAX_RECEIPTS = 10000
LOG = logging.getLogger(__name__)


def stamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class NoteInput(StrictInput):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default='', max_length=8000)
    pinned: bool = False
    shared: bool = False

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        if not value.strip():
            raise ValueError('메모 제목을 입력하세요.')
        return value.strip()


class NoteCreate(NoteInput):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class NoteUpdate(NoteInput):
    version: int = Field(ge=1)


class AlarmInput(StrictInput):
    label: str = Field(min_length=1, max_length=120)
    time: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    timezone: str = Field(default='Asia/Seoul', min_length=1, max_length=80)
    repeat: Literal['once', 'weekly'] = 'once'
    date: str | None = None
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    enabled: bool = True

    @field_validator('label')
    @classmethod
    def clean_label(cls, value):
        if not value.strip():
            raise ValueError('알람 이름을 입력하세요.')
        return value.strip()

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('유효한 IANA 시간대가 필요합니다.') from None
        return value

    @model_validator(mode='after')
    def schedule_fields(self):
        if any(type(x) is not int or x not in range(7) for x in self.weekdays):
            raise ValueError('요일은 월요일 0부터 일요일 6까지입니다.')
        self.weekdays = sorted(set(self.weekdays))
        if self.repeat == 'once':
            if not self.date or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.date):
                raise ValueError('한 번 알람은 날짜가 필요합니다.')
            if not 1970 <= date.fromisoformat(self.date).year <= 2100:
                raise ValueError('날짜는 1970년~2100년 범위로 지정하세요.')
            if self.weekdays:
                raise ValueError('한 번 알람에는 반복 요일을 지정하지 않습니다.')
        elif self.date is not None or not self.weekdays:
            raise ValueError('요일 반복은 날짜 없이 하나 이상의 요일을 선택하세요.')
        return self


class AlarmCreate(AlarmInput):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class AlarmUpdate(AlarmInput):
    version: int = Field(ge=1)


class Version(StrictInput):
    version: int = Field(ge=1)


class EventAction(Version):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


def wall_instants(day: date, clock: str, zone: str) -> list[float]:
    """Round-trip both folds: reject nonexistent local times; deduplicate normal times."""
    naive = datetime.combine(day, day_time.fromisoformat(clock))
    tz = ZoneInfo(zone)
    values = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
            values.add(aware.timestamp())
    return sorted(values)


def next_fire(spec: dict, after: float) -> float | None:
    if not spec['enabled']:
        return None
    if spec['repeat'] == 'once':
        values = wall_instants(date.fromisoformat(spec['date']), spec['time'], spec['timezone'])
        if len(values) != 1:
            raise HTTPException(422, '서머타임으로 없거나 두 번 있는 시각입니다. 다른 시각을 선택하세요.')
        return values[0] if values[0] > after else None
    local_day = datetime.fromtimestamp(after, ZoneInfo(spec['timezone'])).date()
    # A weekly time in a spring-forward gap is skipped for that local date.
    # In a repeated autumn hour, ring only at its first occurrence.
    for offset in range(15):
        d = local_day + timedelta(days=offset)
        if d.weekday() not in spec['weekdays']:
            continue
        values = wall_instants(d, spec['time'], spec['timezone'])
        if values and values[0] > after:
            return values[0]
    raise HTTPException(422, '다음 반복 시각을 계산하지 못했습니다.')


class LifeService:
    def __init__(self, store, changed, *, clock: Callable[[], float] = time.time):
        self.store, self.changed, self.clock = store, changed, clock
        self.last_tick_at = None
        self.scheduler_error = False
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS hub_life_meta(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO hub_life_meta VALUES(1,0);
                CREATE TABLE IF NOT EXISTS hub_notes(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    pinned INTEGER NOT NULL, shared INTEGER NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_alarms(
                    id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, next_fire REAL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS hub_alarm_due ON hub_alarms(next_fire);
                CREATE TABLE IF NOT EXISTS hub_alarm_events(
                    id TEXT PRIMARY KEY, alarm_id TEXT NOT NULL REFERENCES hub_alarms(id) ON DELETE CASCADE,
                    label TEXT NOT NULL, timezone TEXT NOT NULL, scheduled_at REAL NOT NULL,
                    due_at REAL NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    snoozes INTEGER NOT NULL DEFAULT 0, ring_until REAL,
                    updated_at TEXT NOT NULL, UNIQUE(alarm_id, scheduled_at));
                CREATE TABLE IF NOT EXISTS hub_life_receipts(
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self, *, write=False):
        # sqlite3.Connection.__exit__ commits/rolls back but does not close.
        db = self.store.connect()
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def note(row):
        item = dict(row)
        item['pinned'], item['shared'] = bool(item['pinned']), bool(item['shared'])
        return item

    @staticmethod
    def alarm(row):
        item = dict(row)
        spec = json.loads(item.pop('spec_json'))
        at = item.pop('next_fire')
        return {**item, **spec, 'next_fire_at': stamp(at) if at is not None else None}

    @staticmethod
    def event(row):
        item = dict(row)
        for name in ('scheduled_at', 'due_at', 'ring_until'):
            item[name] = stamp(item[name]) if item[name] is not None else None
        return item

    def _recorded(self, db, request_id, operation, values):
        digest = hashlib.sha256(json.dumps([operation, values], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        old = db.execute('SELECT * FROM hub_life_receipts WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['digest'] != digest:
                raise HTTPException(409, '동일 요청 ID의 내용이 달라졌습니다. 새 작업으로 다시 확인하세요.')
            return digest, json.loads(old['result_json']) | {'duplicate': True}
        if db.execute('SELECT count(*) FROM hub_life_receipts').fetchone()[0] >= MAX_RECEIPTS:
            raise HTTPException(409, '중복 방지 기록 한도에 도달했습니다. 관리자 점검이 필요합니다.')
        return digest, None

    @staticmethod
    def bump(db):
        db.execute('UPDATE hub_life_meta SET revision=revision+1 WHERE id=1')

    def _receipt(self, db, request_id, digest, result):
        self.bump(db)
        db.execute('INSERT INTO hub_life_receipts VALUES(?,?,?,?)',
                   (request_id, digest, json.dumps(result, ensure_ascii=False), stamp(self.clock())))
        return result

    @staticmethod
    def _current(db, table, identity, version):
        # table is selected only by trusted methods below, never by an HTTP/model parameter.
        row = db.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, '삭제되었거나 존재하지 않는 항목입니다.')
        if row['version'] != version:
            raise HTTPException(409, '다른 화면에서 변경되었습니다. 목록을 새로 불러온 뒤 다시 확인하세요.')
        return row

    def notes(self):
        with self.db() as db:
            return [self.note(r) for r in db.execute('SELECT * FROM hub_notes ORDER BY pinned DESC, updated_at DESC, id')]

    def create_note(self, body: NoteCreate):
        values = body.model_dump(exclude={'request_id'})
        with self.db(write=True) as db:
            digest, old = self._recorded(db, body.request_id, 'note.create', values)
            if old:
                return old
            if db.execute('SELECT count(*) FROM hub_notes').fetchone()[0] >= MAX_NOTES:
                raise HTTPException(422, f'메모는 최대 {MAX_NOTES}개입니다.')
            nid, now = uid(), stamp(self.clock())
            db.execute('INSERT INTO hub_notes VALUES(?,?,?,?,?,1,?,?)',
                       (nid, body.title, body.body, int(body.pinned), int(body.shared), now, now))
            return self._receipt(db, body.request_id, digest, {'id': nid, 'duplicate': False})

    def update_note(self, nid, body: NoteUpdate):
        with self.db(write=True) as db:
            self._current(db, 'hub_notes', nid, body.version)
            db.execute('UPDATE hub_notes SET title=?,body=?,pinned=?,shared=?,version=version+1,updated_at=? WHERE id=?',
                       (body.title, body.body, int(body.pinned), int(body.shared), stamp(self.clock()), nid))
            self.bump(db)
        return {'id': nid, 'version': body.version + 1}

    def delete_note(self, nid, version):
        with self.db(write=True) as db:
            self._current(db, 'hub_notes', nid, version)
            db.execute('DELETE FROM hub_notes WHERE id=?', (nid,))
            self.bump(db)
        return {'deleted': True}

    def alarms(self):
        with self.db() as db:
            return [self.alarm(r) for r in db.execute('SELECT * FROM hub_alarms ORDER BY next_fire IS NULL,next_fire,id')]

    def events(self, limit=100):
        with self.db() as db:
            return [self.event(r) for r in db.execute(
                'SELECT * FROM hub_alarm_events ORDER BY scheduled_at DESC,id DESC LIMIT ?', (limit,))]

    def _validated_alarm(self, body):
        spec = body.model_dump(exclude={'request_id', 'version'})
        at = next_fire(spec, self.clock())
        if spec['enabled'] and at is None:
            raise HTTPException(422, '지난 시각의 알람은 켤 수 없습니다. 미래 날짜와 시각으로 변경하세요.')
        return spec, at

    def create_alarm(self, body: AlarmCreate):
        values = body.model_dump(exclude={'request_id'})
        with self.db(write=True) as db:
            digest, old = self._recorded(db, body.request_id, 'alarm.create', values)
            if old:
                return old
            spec, at = self._validated_alarm(body)
            if db.execute('SELECT count(*) FROM hub_alarms').fetchone()[0] >= MAX_ALARMS:
                raise HTTPException(422, f'알람은 최대 {MAX_ALARMS}개입니다.')
            aid, now = uid(), stamp(self.clock())
            db.execute('INSERT INTO hub_alarms VALUES(?,?,?,1,?,?)',
                       (aid, json.dumps(spec, ensure_ascii=False), at, now, now))
            return self._receipt(db, body.request_id, digest, {'id': aid, 'duplicate': False})

    def update_alarm(self, aid, body: AlarmUpdate):
        spec, at = self._validated_alarm(body)
        with self.db(write=True) as db:
            self._current(db, 'hub_alarms', aid, body.version)
            now = stamp(self.clock())
            db.execute('UPDATE hub_alarms SET spec_json=?,next_fire=?,version=version+1,updated_at=? WHERE id=?',
                       (json.dumps(spec, ensure_ascii=False), at, now, aid))
            # A manager edit replaces the schedule and cancels old active occurrences.
            db.execute("UPDATE hub_alarm_events SET state='cancelled',ring_until=NULL,version=version+1,updated_at=? "
                       "WHERE alarm_id=? AND state IN ('ringing','snoozed','missed')", (now, aid))
            self.bump(db)
        return {'id': aid, 'version': body.version + 1}

    def delete_alarm(self, aid, version):
        with self.db(write=True) as db:
            self._current(db, 'hub_alarms', aid, version)
            db.execute('DELETE FROM hub_alarms WHERE id=?', (aid,))
            self.bump(db)
        return {'deleted': True}

    def tick(self):
        now = self.clock()
        updated = 0
        with self.db(write=True) as db:
            updated += db.execute("UPDATE hub_alarm_events SET state='missed',ring_until=NULL,version=version+1,updated_at=? "
                                  "WHERE state='ringing' AND ring_until<=?", (stamp(now), now)).rowcount
            for row in db.execute("SELECT * FROM hub_alarm_events WHERE state='snoozed' AND due_at<=?", (now,)).fetchall():
                state = 'ringing' if now - row['due_at'] <= LATE_GRACE_SECONDS else 'missed'
                db.execute('UPDATE hub_alarm_events SET state=?,version=version+1,ring_until=?,updated_at=? WHERE id=?',
                           (state, now + RING_SECONDS if state == 'ringing' else None, stamp(now), row['id']))
                updated += 1
            for row in db.execute('SELECT * FROM hub_alarms WHERE next_fire<=?', (now,)).fetchall():
                due, spec = row['next_fire'], json.loads(row['spec_json'])
                state = 'ringing' if now - due <= LATE_GRACE_SECONDS else 'missed'
                db.execute('INSERT OR IGNORE INTO hub_alarm_events VALUES(?,?,?,?,?,?,?,1,0,?,?)',
                           (uid(), row['id'], spec['label'], spec['timezone'], due, due, state,
                            now + RING_SECONDS if state == 'ringing' else None, stamp(now)))
                if spec['repeat'] == 'once':
                    spec['enabled'] = False
                at = next_fire(spec, now)
                db.execute('UPDATE hub_alarms SET spec_json=?,next_fire=?,version=version+1,updated_at=? WHERE id=?',
                           (json.dumps(spec, ensure_ascii=False), at, stamp(now), row['id']))
                updated += 1
            # Bounded recent history. Do not discard ringing or snoozed events.
            updated += db.execute("DELETE FROM hub_alarm_events WHERE state NOT IN ('ringing','snoozed') AND id NOT IN "
                       "(SELECT id FROM hub_alarm_events ORDER BY scheduled_at DESC,id DESC LIMIT 100)").rowcount
            if updated:self.bump(db)
        self.last_tick_at, self.scheduler_error = stamp(now), False
        return updated

    async def run(self):
        while True:
            try:
                # Real file operations on a thread, keeping ASGI/STT/UI loops responsive.
                if await asyncio.to_thread(self.tick):
                    await self.changed('alarm.occurrence_changed')
            except asyncio.CancelledError:
                raise
            except Exception:
                self.scheduler_error = True
                LOG.exception('Alarm scheduler tick failed; no audio delivery claim')
            await asyncio.sleep(1)

    def act(self, eid, action: Literal['ack', 'snooze'], body: EventAction, *, display_only=False):
        now = self.clock()
        with self.db(write=True) as db:
            if display_only:
                layout=json.loads(db.execute("SELECT value FROM kv WHERE key='layout'").fetchone()[0])
                if not any(w['type']=='alarms' for w in layout['widgets']):
                    raise HTTPException(403, '알람 위젯이 배치되어 있지 않습니다.')
            digest, old = self._recorded(db, body.request_id, 'event.' + action, {'id': eid, 'version': body.version})
            if old:
                return old
            row = self._current(db, 'hub_alarm_events', eid, body.version)
            if action == 'ack':
                if row['state'] not in {'ringing', 'snoozed', 'missed'}:
                    raise HTTPException(409, '이미 종료된 알람 회차입니다.')
                db.execute("UPDATE hub_alarm_events SET state='acknowledged',ring_until=NULL,version=version+1,updated_at=? WHERE id=?",
                           (stamp(now), eid))
            else:
                if row['state'] != 'ringing' or row['ring_until'] <= now:
                    raise HTTPException(409, '지금 울림 대기 중인 알람만 미룰 수 있습니다.')
                if row['snoozes'] >= MAX_SNOOZES:
                    raise HTTPException(409, '한 회차는 최대 3번 미룰 수 있습니다.')
                db.execute("UPDATE hub_alarm_events SET state='snoozed',due_at=?,ring_until=NULL,snoozes=snoozes+1,"
                           'version=version+1,updated_at=? WHERE id=?', (now + SNOOZE_SECONDS, stamp(now), eid))
            return self._receipt(db, body.request_id, digest, {'id': eid, 'action': action, 'duplicate': False})

    def display(self):
        # Widget placement is explicit opt-in. No private-note counts or text in this DTO.
        with self.db() as db:
            db.execute('BEGIN')
            layout = json.loads(db.execute("SELECT value FROM kv WHERE key='layout'").fetchone()[0])
            kinds = {w['type'] for w in layout['widgets']}
            revision = db.execute('SELECT revision FROM hub_life_meta WHERE id=1').fetchone()[0]
            notes = [self.note(r) for r in db.execute('SELECT * FROM hub_notes WHERE shared=1 ORDER BY pinned DESC,updated_at DESC,id')] if 'note' in kinds else []
            alarms = [self.alarm(r) for r in db.execute('SELECT * FROM hub_alarms ORDER BY next_fire IS NULL,next_fire,id')] if 'alarms' in kinds else []
            events = [self.event(r) for r in db.execute(
                "SELECT * FROM hub_alarm_events WHERE state IN ('ringing','snoozed','missed') ORDER BY state='missed',scheduled_at DESC,id DESC")] if 'alarms' in kinds else []
        return {'version': 1, 'revision': revision, 'as_of': stamp(self.clock()), 'notes': {'enabled': 'note' in kinds, 'items': notes},
                'alarms': {'enabled': 'alarms' in kinds, 'items': alarms, 'events': events},
                'scheduler': {'last_tick_at': self.last_tick_at, 'error': self.scheduler_error,
                              'delivery': 'foreground_browser_only', 'sound_confirmed': False}}


def register_routes(app, service: LifeService, admin, viewer, changed):
    @app.get('/api/life/display')
    async def display(_=Depends(viewer)):
        return service.display()

    @app.get('/api/life/notes')
    async def notes(_=Depends(admin)):
        return {'items': service.notes(), 'limit': MAX_NOTES}

    @app.post('/api/life/notes', status_code=201)
    async def add_note(body: NoteCreate, _=Depends(admin)):
        result = service.create_note(body)
        if not result['duplicate']:
            await changed('note.created', result['id'])
        return result

    @app.put('/api/life/notes/{nid}')
    async def edit_note(nid: str, body: NoteUpdate, _=Depends(admin)):
        result = service.update_note(nid, body)
        await changed('note.updated', nid)
        return result

    @app.delete('/api/life/notes/{nid}')
    async def remove_note(nid: str, body: Version, _=Depends(admin)):
        result = service.delete_note(nid, body.version)
        await changed('note.deleted', nid)
        return result

    @app.get('/api/life/alarms')
    async def alarms(_=Depends(admin)):
        return {'items': service.alarms(), 'events': service.events(), 'limit': MAX_ALARMS,
                'history_limit': 100, 'server_time': stamp(service.clock()),
                'scheduler': {'last_tick_at': service.last_tick_at, 'error': service.scheduler_error}}

    @app.post('/api/life/alarms', status_code=201)
    async def add_alarm(body: AlarmCreate, _=Depends(admin)):
        result = service.create_alarm(body)
        if not result['duplicate']:
            await changed('alarm.created', result['id'])
        return result

    @app.put('/api/life/alarms/{aid}')
    async def edit_alarm(aid: str, body: AlarmUpdate, _=Depends(admin)):
        result = service.update_alarm(aid, body)
        await changed('alarm.updated', aid)
        return result

    @app.delete('/api/life/alarms/{aid}')
    async def remove_alarm(aid: str, body: Version, _=Depends(admin)):
        result = service.delete_alarm(aid, body.version)
        await changed('alarm.deleted', aid)
        return result

    def permit_display(session):
        if session['role'] != 'admin' and not service.display()['alarms']['enabled']:
            raise HTTPException(403, '알람 위젯이 배치된 표시 기기에서만 확인·미루기가 가능합니다.')

    @app.post('/api/life/events/{eid}/ack')
    async def acknowledge(eid: str, body: EventAction, session=Depends(viewer)):
        permit_display(session)
        result = service.act(eid, 'ack', body, display_only=session['role']!='admin')
        if not result['duplicate']:
            await changed('alarm.acknowledged', eid)
        return result

    @app.post('/api/life/events/{eid}/snooze')
    async def snooze(eid: str, body: EventAction, session=Depends(viewer)):
        permit_display(session)
        result = service.act(eid, 'snooze', body, display_only=session['role']!='admin')
        if not result['duplicate']:
            await changed('alarm.snoozed', eid)
        return result


def memo_snapshot(store, selector='current', *, widget_id=None):
    """Read-only assistant view of the existing note storage, in one snapshot.

    Current means the first saved note-widget's DEFAULT card, not a fabricated
    browser selection. Latest uses updated_at independently of pinned order.
    Private notes may be read by the manager; llm_display must recheck sharing.
    """
    if selector not in {'current', 'last_modified'}:
        raise ValueError('메모 조회 기준을 확인해 주세요.')
    with store.connect() as db:
        db.execute('BEGIN')
        def kv(key, default):
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default
        layout = kv('layout', {})
        widgets = [w for w in layout.get('widgets', []) if w.get('type') == 'note']
        widget = widgets[0] if widgets else None
        if widget_id is not None:
            widget = next((w for w in widgets if w['id'] == widget_id), None)
            if widget is None:
                raise LookupError('배치된 메모 위젯이 없습니다.')
            if selector != 'current':
                raise ValueError('명시한 메모 위젯의 현재 카드만 조회할 수 있습니다.')
        revision = db.execute('SELECT revision FROM hub_life_meta WHERE id=1').fetchone()[0]
        result = {'ok': True, 'source': 'room_hub_sqlite.hub_notes', 'selector': selector,
                  'selection_policy': 'default_widget_card', 'widget_id': widget['id'] if widget else None,
                  'note_id': None, 'version': None, 'updated_at': None, 'shared': False,
                  'count': 0, 'body': '', 'title': '', 'revision': revision, 'as_of': stamp(time.time())}
        if selector == 'last_modified':
            rows = db.execute('SELECT * FROM hub_notes ORDER BY updated_at DESC,id LIMIT 2').fetchall()
            if len(rows) == 2 and rows[0]['updated_at'] == rows[1]['updated_at']:
                raise ValueError('같은 시각에 수정된 메모가 여러 개라 방금 메모를 특정할 수 없습니다. 관리자 메모 목록에서 확인해 주세요.')
            if rows:
                note = LifeService.note(rows[0])
                return result | {'selection_policy': 'updated_at', 'note_id': note['id'],
                    'version': note['version'], 'updated_at': note['updated_at'], 'shared': note['shared'],
                    'count': 1, 'body': note['body'], 'title': note['title']}
        if not widget:
            raise ValueError('배치된 메모 위젯이 없어 현재 메모를 특정할 수 없습니다. 관리자에서 메모 위젯을 추가해 주세요.')
        cfg = widget.get('config') or {}
        data = kv('widget_data:note', {}) or {}
        legacy = data.get('text') if data.get('text') is not None else cfg.get('text', '')
        if legacy is None:
            legacy = ''
        if not isinstance(legacy, str):
            raise ValueError('기존 메모의 저장 형식을 확인해 주세요. 내용을 임의로 변환하지 않았습니다.')
        if legacy:
            title = cfg.get('caption') or '기존 고정 메모'
            if not isinstance(title, str):
                title = '기존 고정 메모'
            return result | {'source': 'room_hub_sqlite.kv.note', 'count': 1,
                             'body': legacy, 'title': title, 'shared': True}
        row = db.execute('SELECT * FROM hub_notes WHERE shared=1 ORDER BY pinned DESC,updated_at DESC,id LIMIT 1').fetchone()
        if row:
            note = LifeService.note(row)
            return result | {'note_id': note['id'], 'version': note['version'], 'updated_at': note['updated_at'],
                             'shared': True, 'count': 1, 'body': note['body'], 'title': note['title']}
        return result


def memo_result_is_public(store, snapshot):
    """Recheck visibility before projecting a saved memo reply to any display."""
    if not snapshot.get('shared') or not snapshot.get('count'):
        return False
    with store.connect() as db:
        db.execute('BEGIN')
        row = db.execute("SELECT value FROM kv WHERE key='layout'").fetchone()
        widgets = [w for w in json.loads(row[0]).get('widgets', []) if w.get('type') == 'note'] if row else []
        if not widgets:
            return False
        if snapshot.get('note_id'):
            note = db.execute('SELECT shared,version FROM hub_notes WHERE id=?', (snapshot['note_id'],)).fetchone()
            return bool(note and note['shared'] and note['version'] == snapshot.get('version'))
        widget = next((w for w in widgets if w['id'] == snapshot.get('widget_id')), None)
        if not widget or snapshot.get('source') != 'room_hub_sqlite.kv.note':
            return False
        data = db.execute("SELECT value FROM kv WHERE key='widget_data:note'").fetchone()
        data = (json.loads(data[0]) if data else {}) or {}
        cfg = widget.get('config') or {}
        body = data.get('text') if data.get('text') is not None else cfg.get('text', '')
        return isinstance(body, str) and body == snapshot.get('body')


def note_catalog_snapshot(store):
    """Server-internal metadata read; NOT a new API, search action or permission.

    Include private titles in the manager resolver to avoid false uniqueness.
    Never read bodies until an actual unique note is selected via MemoAdapter.
    Detect externally oversized stores instead of silently treating a prefix as
    the complete catalog. Neither pin order nor sharing chooses a named target.
    """
    with store.connect() as db:
        db.execute('BEGIN')
        rows = db.execute('SELECT id,title,version,shared,updated_at FROM hub_notes ORDER BY id LIMIT ?',
                          (MAX_NOTES + 1,)).fetchall()
        revision = db.execute('SELECT revision FROM hub_life_meta WHERE id=1').fetchone()[0]
        return {'items': [dict(r) | {'shared': bool(r['shared'])} for r in rows[:MAX_NOTES]],
                'truncated': len(rows) > MAX_NOTES, 'revision': revision,
                'source': 'room_hub_sqlite.hub_notes', 'as_of': stamp(time.time())}
