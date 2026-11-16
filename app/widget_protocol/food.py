"""Explicit typed binding to food inventory; no display or model write authority."""
from __future__ import annotations

import asyncio
import hashlib
from types import MappingProxyType

from pydantic import Field, field_validator, model_validator

from ..food import FoodCreate, FoodInput, FoodService, clean_food_name, valid_expiry_date
from .adapters import StoredAdapter
from .core import AdapterResult, Operation, missing
from .models import Contract
from .schemas import Empty, VersionArgs


class FoodItem(FoodInput):
    id: str
    version: int
    created_at: str
    updated_at: str


class FoodList(Contract):
    items: list[FoodItem]
    count: int


class FoodMutation(FoodItem):
    duplicate: bool = False


class FoodDeleted(Contract):
    id: str
    deleted: bool
    duplicate: bool = False


class FoodPatch(VersionArgs):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    quantity: int | None = Field(default=None, ge=1, le=9999)
    expiry_date: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        return clean_food_name(value) if value is not None else value

    @field_validator('expiry_date')
    @classmethod
    def valid_date(cls, value):
        return valid_expiry_date(value) if value is not None else value

    @model_validator(mode='after')
    def explicit_fields(self):
        for name in ('name', 'quantity', 'expiry_date'):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError('Food fields cannot be cleared.')
        return self


class FoodAdapter(StoredAdapter):
    name = 'food'
    source_of_truth = 'room_hub_sqlite.hub_food_items'
    health_table = 'hub_food_items'
    operations = MappingProxyType({
        'list': Operation(Empty, FoodList, '소비기한 순으로 등록된 모든 식품·개수·소비기한 조회'),
        'get': Operation(Empty, FoodItem, '확정한 식품 ID의 등록 내용 조회',
                         target_types=('item_id',), target_required=True),
        'add': Operation(FoodInput, FoodMutation, '식품·개수·소비기한을 새 항목으로 등록', False, 'write'),
        'update': Operation(FoodPatch, FoodMutation, '확정한 식품 ID와 버전의 등록 내용 수정',
                            False, 'write', ('item_id',), True),
        'delete': Operation(VersionArgs, FoodDeleted, '확정한 식품 ID와 버전의 항목만 삭제',
                            False, 'write', ('item_id',), True),
    })

    def __init__(self, store, food: FoodService, changed):
        super().__init__(store)
        self.food, self.changed = food, changed

    def validate_slots(self, request, args):
        if request.action == 'update' and not (args.model_fields_set - {'version'}):
            missing('args.name')

    async def _execute(self, request, args, authority):
        if request.action == 'list':
            items = await asyncio.to_thread(self.food.items)
            return AdapterResult({'items': items, 'count': len(items)})
        if request.action == 'get':
            return AdapterResult(await asyncio.to_thread(self.food.get, request.target.value))
        key = 'wp_food_' + hashlib.sha256(
            (authority.principal + ':' + (request.idempotency_key or request.request_id)).encode()
        ).hexdigest()
        if request.action == 'add':
            result = await asyncio.to_thread(self.food.create, FoodCreate(**args.model_dump(), request_id=key))
            event = 'food.created'
        elif request.action == 'update':
            values = args.model_dump(exclude={'version'}, exclude_unset=True)
            result = await asyncio.to_thread(self.food.update_fields, request.target.value,
                                             values, args.version, request_id=key)
            event = 'food.updated'
        else:
            result = await asyncio.to_thread(self.food.delete, request.target.value, args.version, request_id=key)
            event = 'food.deleted'
        changed = not result['duplicate']
        if changed:
            await self.changed(event, result['id'])
        return AdapterResult(result, changed=changed, event=event, entity_id=result['id'])
