"""Static vocabulary for Korean Room Hub recordings, not execution authority.

The terms describe the current trusted adapters in widget_protocol and the
existing assistant/time vocabulary. They are not example commands, evaluation
answers, or a request to insert words absent from the recording. This module
never reads task/memo titles, bodies, settings, environment variables, or a DB.
The caller must independently enforce audio consent and the budget policy.
"""
from __future__ import annotations

from types import MappingProxyType


HINT_VERSION = 'room-hub-ko-v2'
MAX_KEYWORDS = 48
MAX_KEYWORD_CHARACTERS = 40
MAX_PROMPT_CHARACTERS = 400

# Linguistic metadata only. These groups must never be used as an operation
# registry or interpreted as proof that an adapter is enabled on an instance.
ROOM_HUB_DOMAIN_KEYWORDS = MappingProxyType({
    'todo': ('할 일', '완료 처리', '완료 취소', '우선순위'),
    'calendar': ('일정', '달력', '종일', '반복 일정'),
    'memo': ('메모',),
    'food': ('소비기한', '식품', '개수'),
    'alarm': ('알람', '미루기'),
    'timer': ('타이머', '카운트다운'),
    'clock': ('현재 시각', '시간대'),
    'weather': ('날씨', '예보'),
    'indoor': ('실내 온도', '습도', '기압', '공기질', '먼지 센서', 'ADC'),
    'llm-response': ('에이전트', '응답 이력'),
    'layout': ('위젯', '대시보드', '레이아웃', '배치'),
    'settings': ('설정', '다크 모드', '라이트 모드'),
    'system': ('시스템 상태', '연결된 기기'),
})

# Generic product/date terms, not personal names or stored entity contents.
_COMMON_KEYWORDS = ('Room Hub', '룸 허브', '브리핑', '오늘', '내일', '모레', '오전', '오후')
ROOM_HUB_KEYWORDS = tuple(dict.fromkeys(
    (*_COMMON_KEYWORDS, *(term for terms in ROOM_HUB_DOMAIN_KEYWORDS.values() for term in terms))
))
ROOM_HUB_PROMPT = (
    '한국어로 말하는 Room Hub 개인 비서 요청에 관한 녹음. '
    '주제는 할 일, 일정, 메모, 식품 소비기한과 개수, 알람, 타이머, 시각, 날씨, 실내 센서, '
    '위젯 배치, 허브 설정 및 시스템 상태.'
)


def room_hub_transcription_hints() -> dict:
    """Return fresh, bounded context/keyword values without accessing user data.

    GPT-Transcribe can receive ``prompt`` and the literal ``keywords`` array.
    Older providers may use supported prompt context instead; the caller owns
    that model-specific serialization. Hints do not establish ASR accuracy.
    """
    return {
        'version': HINT_VERSION,
        'prompt': ROOM_HUB_PROMPT,
        'keywords': list(ROOM_HUB_KEYWORDS),
    }
