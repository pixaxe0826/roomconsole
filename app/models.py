from datetime import date as Date
from typing import Literal, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
class RepeatRule(StrictModel):
    frequency: Literal['none','daily','weekdays','weekly','monthly']='none'
    interval: int=Field(default=1,ge=1,le=52)
    until: Date|None=None
    weekdays: list[int]=Field(default_factory=list,max_length=7)
    @field_validator('weekdays')
    @classmethod
    def days(cls,v):
        if len(set(v))!=len(v) or any(x<0 or x>6 for x in v): raise ValueError('요일은 월=0…일=6, 중복 없이 입력하세요.')
        return sorted(v)
class TaskCreate(StrictModel):
    title: str=Field(min_length=1,max_length=240)
    date: Date
    time: str|None=Field(default=None,pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    category: str=Field(default='personal',pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    priority: Literal['normal','high']='normal'
    notes: str=Field(default='',max_length=4000)
    repeat: RepeatRule=Field(default_factory=RepeatRule)
    @model_validator(mode='after')
    def dates_valid(self):
        if self.repeat.frequency!='none':
            if self.repeat.until is None: raise ValueError('반복 종료일이 필요합니다.')
            if self.repeat.until<self.date: raise ValueError('종료일은 시작일 이후여야 합니다.')
            if (self.repeat.until-self.date).days>1827: raise ValueError('반복 범위는 최대 약 5년입니다.')
        return self
class TaskPatch(StrictModel):
    version: int=Field(ge=1)
    title: str|None=Field(default=None,min_length=1,max_length=240)
    date: Date|None=None
    time: str|None=Field(default=None,pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    category: str|None=Field(default=None,pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    priority: Literal['normal','high']|None=None
    notes: str|None=Field(default=None,max_length=4000)
    completed: bool|None=None
class TaskCompletion(StrictModel):
    """The sole write operation permitted to a paired display; no field widening."""
    version: int=Field(ge=1, strict=True)
    completed: bool=Field(strict=True)

class WidgetInstance(StrictModel):
    id: str=Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    type: str=Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    title: str=Field(default='',max_length=80)
    x: int=Field(ge=0,le=15)
    y: int=Field(ge=0,le=15)
    w: int=Field(ge=1,le=16)
    h: int=Field(ge=1,le=16)
    config: dict[str,Any]=Field(default_factory=dict)
class Layout(StrictModel):
    columns: int=Field(default=8,ge=4,le=16)
    rows: int=Field(default=6,ge=4,le=16)
    widgets: list[WidgetInstance]=Field(min_length=1,max_length=40)
    version: int=Field(default=1,ge=1)
    @model_validator(mode='after')
    def positions(self):
        cells=set();ids=set()
        for w in self.widgets:
            if w.id in ids: raise ValueError('위젯 ID 중복')
            ids.add(w.id)
            if w.x+w.w>self.columns or w.y+w.h>self.rows: raise ValueError('위젯이 격자를 벗어납니다.')
            for x in range(w.x,w.x+w.w):
                for y in range(w.y,w.y+w.h):
                    if (x,y) in cells: raise ValueError('위젯끼리 겹칩니다.')
                    cells.add((x,y))
        return self
class HubSettings(StrictModel):
    title: str=Field(default='My room',min_length=1,max_length=50)
    timezone: str='Asia/Seoul'
    location_name: str=Field(default='',max_length=60)
    latitude: float|None=Field(default=None,ge=-90,le=90)
    longitude: float|None=Field(default=None,ge=-180,le=180)
    weather_interval_minutes: int=Field(default=15,ge=5,le=120)
    theme: Literal['light','dark']='light'
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):
        try: ZoneInfo(v)
        except (ZoneInfoNotFoundError,ValueError): raise ValueError('유효한 IANA 시간대가 아닙니다.')
        return v
    @model_validator(mode='after')
    def coords(self):
        if (self.latitude is None)!=(self.longitude is None): raise ValueError('위도와 경도를 함께 입력하세요.')
        return self
class VoiceText(StrictModel):
    schema_version: Literal['1']='1'
    request_id: str=Field(min_length=1,max_length=128,pattern=r'^[\w.:-]+$')
    source: str=Field(default='manual',min_length=1,max_length=80,pattern=r'^[\w.:-]+$')
    text: str=Field(min_length=1,max_length=16000)
    locale: str=Field(default='ko-KR',max_length=30)
    metadata: dict[str,Any]=Field(default_factory=dict)
class RemoteCommand(StrictModel):
    action: Literal['home','expand','select_date','reload']
    widget_id: str|None=Field(default=None,max_length=64)
    date: Date|None=None
    device_id: str|None=Field(default=None,max_length=64)
class Login(StrictModel): token:str=Field(min_length=1,max_length=256)
class PairCreate(StrictModel): name:str=Field(default='Room iPad',min_length=1,max_length=80)
class PairAccept(StrictModel): code:str=Field(min_length=10,max_length=200)
class VoiceReview(StrictModel):
    status: Literal['pending_review','reviewed','dismissed']
    text: str|None=Field(default=None,max_length=16000)
class WidgetData(StrictModel): data:dict
