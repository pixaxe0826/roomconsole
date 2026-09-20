"""Trusted server capability registry; no Python imports from widget folders.

All exposed names resolve to a server-owned handler. No model-selected SQL,
URL or class name is ever evaluated. Notes/alarms are NOT registered services.
"""
from dataclasses import dataclass, asdict

@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    access: str
    handler: str
    input_fields: tuple[str, ...]
    confirmation: bool = False
    event: str | None = None

    def public(self):
        return asdict(self)

_CAPS = (
    Capability('todo.list','등록된 할 일 조회·브리핑','read','tasks.query',('date_ref','status')),
    Capability('calendar.query','Room Hub 일정 조회; 외부 캘린더 아님','read','tasks.query',('date_ref','status')),
    Capability('time.query','서버 현재 시각','read','clock.now',()),
    Capability('weather.query','서버에 저장된 날씨·예보','read','weather.cache',('date_ref',)),
    Capability('todo.create','한 날짜의 할 일 추가','write','tasks.confirmed_write',('date_ref','title','time'),True,'tasks.changed'),
    Capability('todo.complete','서버에서 찾은 할 일 완료','write','tasks.confirmed_write',('date_ref','title','scope','status'),True,'tasks.changed'),
    Capability('todo.uncomplete','완료 취소','write','tasks.confirmed_write',('date_ref','title','scope','status'),True,'tasks.changed'),
    Capability('todo.delete','할 일 삭제','write','tasks.confirmed_write',('date_ref','title','scope','status'),True,'tasks.changed'),
)
REGISTRY={c.name:c for c in _CAPS}
READS=frozenset(c.name for c in _CAPS if c.access=='read')
WRITES=frozenset(c.name for c in _CAPS if c.access=='write')
INTENTS=sorted(set(REGISTRY)|{'unknown','chat.general'})


def require(name):
    if name not in REGISTRY: raise ValueError('서버에 등록된 기능이 아닙니다. 실행하지 않았습니다.')
    return REGISTRY[name]


def manifest():
    return {'version':1,'capabilities':[c.public() for c in _CAPS],
            'unavailable':['notes','alarms'],
            'policy':'읽기는 서버 데이터, 모든 쓰기는 관리자 확인. 위젯 코드와 실행 권한은 별개.'}


def parser_description():
    return '; '.join(c.name+'='+c.description for c in _CAPS)
