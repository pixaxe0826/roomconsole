"""Per-capability schemas, reusing existing write models where possible."""
from datetime import date as Date
from typing import Literal
from pydantic import Field, field_validator, model_validator

from .models import Contract
from ..models import TaskCreate, TaskPatch  # Unchanged UI/API models, not copies.
from ..life import AlarmInput


class Empty(Contract):
    pass


class VersionArgs(Contract):
    version: int = Field(ge=1)


class MemoWrite(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, max_length=8000)
    pinned: bool | None = None
    shared: bool | None = None
    version: int | None = Field(default=None, ge=1)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        if value is not None and not value.strip():
            raise ValueError('Empty title')
        return value


class MemoAppend(VersionArgs):
    text: str = Field(min_length=1, max_length=8000)


class MemoData(Contract):
    id: str | None = None
    widget_id: str | None = None
    title: str
    body: str
    version: int | None = None
    pinned: bool | None = None
    shared: bool
    created_at: str | None = None
    updated_at: str | None = None
    selection_policy: str
    active_card_known: bool = False


class MemoMutation(Contract):
    id: str
    version: int | None = None
    duplicate: bool = False


class TaskQuery(Contract):
    date: Date | None = None
    start: Date | None = None
    end: Date | None = None
    status: Literal['all', 'pending', 'completed'] = 'all'
    period: Literal['morning', 'afternoon'] | None = None
    limit: int = Field(default=50, ge=1, le=10000)

    @model_validator(mode='after')
    def valid_range(self):
        if self.date is not None and (self.start is not None or self.end is not None):
            raise ValueError('Use date OR start/end')
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError('Inverted date range')
        return self


class TaskData(Contract):
    id: str
    title: str
    date: str
    time: str | None
    category: str
    priority: str
    notes: str
    completed: bool
    series_id: str | None
    version: int
    created_at: str
    updated_at: str


class TaskList(Contract):
    ok: bool
    source: str
    count: int
    items: list[TaskData]
    range: list[str]
    status: str
    as_of: str
    revision: int
    limit: int
    returned: int
    truncated: bool
    period: Literal['morning', 'afternoon'] | None = None
    untimed_count: int = 0


class TasksCreated(Contract):
    ids: list[str]
    count: int
    series_id: str | None


class TasksDeleted(Contract):
    deleted: int


class AlarmData(AlarmInput):
    id: str
    version: int
    created_at: str
    updated_at: str
    next_fire_at: str | None


class AlarmsList(Contract):
    items: list[AlarmData]
    delivery: Literal['foreground_browser_only'] = 'foreground_browser_only'
    sound_confirmed: Literal[False] = False
