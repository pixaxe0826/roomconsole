"""Static, synthetic hint-contract tests; not an audio accuracy benchmark."""
import builtins
import sqlite3
import unicodedata

import pytest

from app.transcription_hints import (
    HINT_VERSION,
    MAX_KEYWORDS,
    MAX_KEYWORD_CHARACTERS,
    MAX_PROMPT_CHARACTERS,
    ROOM_HUB_DOMAIN_KEYWORDS,
    ROOM_HUB_KEYWORDS,
    ROOM_HUB_PROMPT,
    room_hub_transcription_hints,
)
from app.widget_protocol.system_adapters import (
    ClockAdapter,
    FullAlarmAdapter,
    FullCalendarAdapter,
    FullMemoAdapter,
    FullTodoAdapter,
    IndoorSensorAdapter,
    LLMDisplayAdapter,
    LayoutAdapter,
    SettingsAdapter,
    SystemAdapter,
    WeatherAdapter,
)
from app.widget_protocol.timers import TimerAdapter


def test_vocabulary_groups_match_existing_adapter_names():
    adapters = (
        FullTodoAdapter, FullCalendarAdapter, FullMemoAdapter,
        FullAlarmAdapter, TimerAdapter, ClockAdapter, WeatherAdapter,
        IndoorSensorAdapter, LLMDisplayAdapter, LayoutAdapter,
        SettingsAdapter, SystemAdapter,
    )
    assert set(ROOM_HUB_DOMAIN_KEYWORDS) == {adapter.name for adapter in adapters}
    assert all(terms for terms in ROOM_HUB_DOMAIN_KEYWORDS.values())


def test_literal_keywords_are_small_unique_single_line_values():
    assert 0 < len(ROOM_HUB_KEYWORDS) <= MAX_KEYWORDS
    assert len(ROOM_HUB_KEYWORDS) == len({term.casefold() for term in ROOM_HUB_KEYWORDS})
    for term in ROOM_HUB_KEYWORDS:
        assert isinstance(term, str)
        assert 0 < len(term) <= MAX_KEYWORD_CHARACTERS
        assert term == term.strip() == unicodedata.normalize('NFC', term)
        assert not any(ch in term for ch in '<>\r\n')
        assert not any(unicodedata.category(ch).startswith('C') for ch in term)


def test_context_describes_recording_without_operation_instructions():
    assert 0 < len(ROOM_HUB_PROMPT) <= MAX_PROMPT_CHARACTERS
    assert '한국어' in ROOM_HUB_PROMPT and '녹음' in ROOM_HUB_PROMPT
    for instruction in ('실행하세요', '추가하세요', '출력하세요', '반드시', '정답', '보정하세요'):
        assert instruction not in ROOM_HUB_PROMPT
    assert {'할 일', '완료 취소', '실내 온도', '습도', '기압', '먼지 센서', '타이머', '위젯'} <= set(ROOM_HUB_KEYWORDS)
    # A vocabulary hint must not misrepresent raw dust readings as a calibrated
    # concentration or air-quality grade supported by the indoor adapter.
    assert 'PM2.5' not in ROOM_HUB_KEYWORDS and 'AQI' not in ROOM_HUB_KEYWORDS


def test_returned_lists_cannot_mutate_static_or_subsequent_hints():
    first = room_hub_transcription_hints()
    first['keywords'].append('synthetic untrusted content')
    first['prompt'] = 'different context'
    second = room_hub_transcription_hints()
    assert second == {'version': HINT_VERSION, 'prompt': ROOM_HUB_PROMPT, 'keywords': list(ROOM_HUB_KEYWORDS)}
    assert 'synthetic untrusted content' not in second['keywords']
    with pytest.raises(TypeError):
        ROOM_HUB_DOMAIN_KEYWORDS['unknown'] = ('synthetic term',)


def test_hint_generation_does_not_access_stored_or_environment_data(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Static hint generation must not access files or the DB')

    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setenv('HUB_NOTE_TITLE', 'synthetic private note title')
    monkeypatch.setenv('HUB_TASK_TITLE', 'synthetic task title')
    result = room_hub_transcription_hints()
    assert 'synthetic' not in repr(result)
    assert set(result) == {'version', 'prompt', 'keywords'}
