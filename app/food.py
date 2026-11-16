"""Use-by-date inventory, not a food-safety assessment or a background alarm.

Manual CRUD is manager-only. The display projection is opt-in through a placed
``food-expiry`` widget. All writes use the existing Store connection/UoW so an
approved multi-step agent request can commit or roll back as one transaction.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import hashlib
import json
import time
from typing import Callable

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock_service import clock_context
from .store import uid

MAX_FOOD_ITEMS = 1000
MAX_FOOD_RECEIPTS = 10000
DUE_SOON_DAYS = 3


def clean_food_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(ord(char) < 32 for char in cleaned):
        raise ValueError('식품 이름을 한 줄로 입력하세요.')
    return cleaned


def valid_expiry_date(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('소비기한은 YYYY-MM-DD 날짜로 입력하세요.')
    return value


class FoodInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(ge=1, le=9999)
    expiry_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')

    _name = field_validator('name')(clean_food_name)
    _expiry_date = field_validator('expiry_date')(valid_expiry_date)


class FoodCreate(FoodInput):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{8,128}$')


class FoodUpdate(FoodInput):
    version: int = Field(ge=1)


class FoodService:
    def __init__(self, store, changed=None, *, clock: Callable[[], float] = time.time):
        self.store, self.changed, self.clock = store, changed, clock
        # Avoid executescript: it implicitly commits an active outer UoW.
        with self.store.connect() as db:
            for statement in (
                '''CREATE TABLE IF NOT EXISTS hub_food_items(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 9999),
                    expiry_date TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS hub_food_expiry ON hub_food_items(expiry_date, name, id)',
                '''CREATE TABLE IF NOT EXISTS hub_food_receipts(
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
            ):
                db.execute(statement)

    def _stamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    @staticmethod
    def _items(db):
        return [dict(row) for row in db.execute(
            'SELECT * FROM hub_food_items ORDER BY expiry_date, name, id')]

    def items(self):
        """All rows, including passed dates; no implicit removal or hidden limit."""
        with self.store.connect() as db:
            return self._items(db)

    @staticmethod
    def _current(db, identity, version=None):
        row = db.execute('SELECT * FROM hub_food_items WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, '삭제되었거나 존재하지 않는 식품입니다.')
        if version is not None and row['version'] != version:
            raise HTTPException(409, '다른 화면에서 변경되었습니다. 목록을 새로 불러온 뒤 확인하세요.')
        return dict(row)

    def get(self, identity):
        with self.store.connect() as db:
            return self._current(db, identity)

    @staticmethod
    def _recorded(db, request_id, operation, values):
        if request_id is None:
            return None, None
        digest = hashlib.sha256(json.dumps(
            [operation, values], sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()).hexdigest()
        old = db.execute('SELECT * FROM hub_food_receipts WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['digest'] != digest:
                raise HTTPException(409, '동일 요청 ID의 내용이 달라졌습니다. 새 작업으로 다시 확인하세요.')
            return digest, json.loads(old['result_json']) | {'duplicate': True}
        if db.execute('SELECT count(*) FROM hub_food_receipts').fetchone()[0] >= MAX_FOOD_RECEIPTS:
            raise HTTPException(409, '중복 방지 기록 한도에 도달했습니다. 관리자 점검이 필요합니다.')
        return digest, None

    def _receipt(self, db, request_id, digest, result):
        if request_id is not None:
            db.execute('INSERT INTO hub_food_receipts VALUES(?,?,?,?)',
                       (request_id, digest, json.dumps(result, ensure_ascii=False), self._stamp()))
        return result

    def create(self, body: FoodCreate):
        values = body.model_dump(exclude={'request_id'})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            digest, old = self._recorded(db, body.request_id, 'food.add', values)
            if old is not None:
                return old
            if db.execute('SELECT count(*) FROM hub_food_items').fetchone()[0] >= MAX_FOOD_ITEMS:
                raise HTTPException(422, f'식품은 최대 {MAX_FOOD_ITEMS}개 항목까지 등록할 수 있습니다.')
            identity, now = uid(), self._stamp()
            db.execute('INSERT INTO hub_food_items VALUES(?,?,?,?,1,?,?)',
                       (identity, body.name, body.quantity, body.expiry_date, now, now))
            result = self._current(db, identity) | {'duplicate': False}
            return self._receipt(db, body.request_id, digest, result)

    def update(self, identity, body: FoodUpdate, *, request_id=None):
        return self.update_fields(identity, body.model_dump(exclude={'version'}),
                                  body.version, request_id=request_id)

    def update_fields(self, identity, values, version, *, request_id=None):
        """Replay the original partial update before reading/merging the current row."""
        if type(version) is not int or version < 1:
            raise HTTPException(422, '유효한 식품 버전이 필요합니다.')
        if not values or set(values) - {'name', 'quantity', 'expiry_date'}:
            raise HTTPException(422, '변경할 식품·개수·소비기한 값을 명시하세요.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            digest, old = self._recorded(db, request_id, 'food.update',
                                         {'id': identity, 'version': version, **values})
            if old is not None:
                return old
            current = self._current(db, identity, version)
            body = FoodInput(**({name: current[name] for name in FoodInput.model_fields} | values))
            db.execute('''UPDATE hub_food_items SET name=?,quantity=?,expiry_date=?,
                          version=version+1,updated_at=? WHERE id=?''',
                       (body.name, body.quantity, body.expiry_date, self._stamp(), identity))
            result = self._current(db, identity) | {'duplicate': False}
            return self._receipt(db, request_id, digest, result)

    def delete(self, identity, version, *, request_id=None):
        # bool must not become version 1 in internal callers either.
        if type(version) is not int or version < 1:
            raise HTTPException(422, '유효한 식품 버전이 필요합니다.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            digest, old = self._recorded(db, request_id, 'food.delete', {'id': identity, 'version': version})
            if old is not None:
                return old
            self._current(db, identity, version)
            db.execute('DELETE FROM hub_food_items WHERE id=?', (identity,))
            return self._receipt(db, request_id, digest,
                                 {'id': identity, 'deleted': True, 'duplicate': False})

    def snapshot(self, *, shared_only=False):
        """Read layout, timezone and inventory within one consistent SQLite snapshot."""
        with self.store.connect() as db:
            db.execute('BEGIN')
            values = {row['key']: json.loads(row['value']) for row in db.execute(
                "SELECT key,value FROM kv WHERE key IN ('layout','settings')")}
            enabled = any(widget.get('type') == 'food-expiry'
                          for widget in values['layout'].get('widgets', []))
            zone = values['settings']['timezone']
            today = clock_context(self._stamp(), zone)['today']
            items = self._items(db) if enabled or not shared_only else []
        day = date.fromisoformat(today)
        for item in items:
            remaining = (date.fromisoformat(item['expiry_date']) - day).days
            item['days_remaining'] = remaining
            item['expiry_status'] = ('expired' if remaining < 0 else 'today' if remaining == 0
                                     else 'soon' if remaining <= DUE_SOON_DAYS else 'later')
        return {'enabled': enabled, 'items': items, 'today': today, 'timezone': zone,
                'due_soon_days': DUE_SOON_DAYS}

    def display(self):
        return self.snapshot(shared_only=True)


def register_routes(app, service: FoodService, admin, viewer, changed):
    @app.get('/api/food/items')
    async def list_items(_=Depends(admin)):
        return await asyncio.to_thread(service.snapshot)

    @app.get('/api/food/display')
    async def display_items(_=Depends(viewer)):
        return await asyncio.to_thread(service.display)

    @app.post('/api/food/items', status_code=201)
    async def add_item(body: FoodCreate, _=Depends(admin)):
        result = await asyncio.to_thread(service.create, body)
        if not result['duplicate']:
            await changed('food.created', result['id'])
        return result

    @app.put('/api/food/items/{identity}')
    async def update_item(identity: str, body: FoodUpdate, _=Depends(admin)):
        result = await asyncio.to_thread(service.update, identity, body)
        await changed('food.updated', identity)
        return result

    @app.delete('/api/food/items/{identity}')
    async def delete_item(identity: str, version: int = Query(ge=1), _=Depends(admin)):
        result = await asyncio.to_thread(service.delete, identity, version)
        await changed('food.deleted', identity)
        return result
