"""Bounded Whisper decoding policy. Not an LLM correction or action router.

New settings are separate from speech-config.json so all existing model/thread
management tools remain compatible. With no opt-in file, legacy behavior wins.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PATCH_ID = 'room-hub-0.1.5-stt-accuracy1'
PROFILES = {'legacy': 1, 'hint': 1, 'balanced': 3, 'careful': 5}
# A vocabulary hint, NOT an instruction to replace the recording with a command.
VOCABULARY = '할 일, 오늘, 내일, 남은 할 일, 추가해 줘, 완료 처리해, 완료 취소해, 보여줘.'
# Whisper uses byte-level BPE. This conservative byte bound also bounds the
# ordinary-text token count below the 224-token initial prompt window for base.
MAX_HINT_BYTES = 220
MAX_REPORT_BYTES = 256 * 1024


@dataclass(frozen=True)
class AccuracyConfig:
    schema: int = 1
    profile: str = 'legacy'
    include_task_titles: bool = True
    max_task_titles: int = 4

    @property
    def beam(self) -> int:
        return PROFILES[self.profile]

    @classmethod
    def from_dict(cls, raw) -> 'AccuracyConfig':
        if not isinstance(raw, dict) or set(raw) - {'schema', 'profile', 'include_task_titles', 'max_task_titles'}:
            raise ValueError('Unknown STT accuracy settings')
        c = cls(**raw)
        if type(c.schema) is not int or c.schema != 1 or not isinstance(c.profile, str) or c.profile not in PROFILES:
            raise ValueError('Invalid STT accuracy schema/profile')
        if type(c.include_task_titles) is not bool:
            raise ValueError('include_task_titles must be boolean')
        if type(c.max_task_titles) is not int or not 0 <= c.max_task_titles <= 4:
            raise ValueError('max_task_titles must be 0..4')
        return c

    @classmethod
    def read(cls, path: Path) -> 'AccuracyConfig':
        if not path.exists():
            return cls()
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError('Invalid STT accuracy file')
        return cls.from_dict(json.loads(path.read_text('utf-8')))

    def public(self) -> dict:
        return {**asdict(self), 'beam_size': self.beam, 'prompt_enabled': self.profile != 'legacy',
                'patch_id': PATCH_ID, 'automatic_second_pass': False}


def clean_title(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize('NFC', value)
    if any(unicodedata.category(x).startswith('C') for x in value):
        return None
    value = re.sub(r'\s+', ' ', value).strip()
    # Reject markup / special tokens / multi-line text; never truncate a name
    # into a different name. Titles are data, never shell commands.
    if not 1 <= len(value) <= 40 or not re.fullmatch(r'[\w .+/#()&·:\-]+', value):
        return None
    return value


def make_hint(policy: AccuracyConfig, titles=()) -> tuple[str, list[str]]:
    if policy.profile == 'legacy':
        return '', []
    text, selected = VOCABULARY, []
    if policy.include_task_titles and policy.max_task_titles:
        for raw in titles:
            title = clean_title(raw)
            if not title or title in selected:
                continue
            proposed = text + (' 참고 어휘: ' if not selected else ', ') + title
            if len(proposed.encode('utf-8')) > MAX_HINT_BYTES:
                continue
            text, selected = proposed, [*selected, title]
            if len(selected) >= policy.max_task_titles:
                break
    assert len(text.encode('utf-8')) <= MAX_HINT_BYTES
    return text, selected


def task_hint(store, policy: AccuracyConfig, now: datetime | None = None) -> dict:
    """Only uncompleted titles for server-local today/tomorrow, bounded at source."""
    titles, warning = [], None
    if policy.profile != 'legacy' and policy.include_task_titles and policy.max_task_titles:
        try:
            settings = store.get('settings') or {}
            zone = ZoneInfo(settings.get('timezone', 'Asia/Seoul'))
            today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
            with store.connect() as db:
                titles = [r['title'] for r in db.execute(
                    '''SELECT title FROM tasks WHERE completed=0 AND date IN (?,?)
                       ORDER BY CASE WHEN priority='high' THEN 0 ELSE 1 END,
                       date, time IS NULL, time, updated_at DESC, id LIMIT 32''',
                    (str(today), str(today + timedelta(days=1))))]
        except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
            warning = 'task_hint_unavailable'
    text, selected = make_hint(policy, titles)
    return {'text': text, 'titles': selected, 'warning': warning,
            'utf8_bytes': len(text.encode('utf-8')), 'byte_limit': MAX_HINT_BYTES}


def score_summary(path: Path) -> dict:
    """Parse v1.8.3 -ls output. Token p is NOT calibrated ASR correctness.

    Read bytes: BPE tokens may contain partial UTF-8 sequences. Parsing token
    probabilities must not corrupt the actual UTF-8 .txt transcription.
    """
    result = {'available': False, 'token_count': 0, 'mean_token_p': None,
              'mean_token_log_p': None, 'low_p_fraction': None,
              'meaning': 'decoder scores, NOT word accuracy / calibrated confidence'}
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        result['reason'] = 'missing_or_oversize_score_file'
        return result
    ps = []
    for line in path.read_bytes().splitlines():
        try:
            raw, p = line.rsplit(b'\t', 1)
            if not raw.strip() or raw.strip().startswith(b'[_') or raw.strip().startswith(b'<|'):
                continue
            p = float(p)
            if math.isfinite(p) and 0 <= p <= 1:
                ps.append(p)
        except (ValueError, TypeError):
            continue
    if not ps:
        result['reason'] = 'no_usable_scores'
        return result
    return {**result, 'available': True, 'token_count': len(ps),
            'mean_token_p': sum(ps) / len(ps),
            'mean_token_log_p': sum(math.log(max(p, 1e-9)) for p in ps) / len(ps),
            'low_p_fraction': sum(p < .33 for p in ps) / len(ps)}


def diagnostic_segments(path: Path) -> dict:
    """Allowlist the plain -oj result. Omit model paths and raw system metadata."""
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        return {'available': False, 'reason': 'missing_or_oversize_json'}
    try:
        data = json.loads(path.read_text('utf-8'))
        source = data.get('transcription')
        if not isinstance(source, list):
            raise ValueError()
        segments = []
        for row in source[:128]:
            if not isinstance(row, dict) or not isinstance(row.get('text'), str):
                continue
            item = {'text': row['text'][:16000]}
            offsets = row.get('offsets', {})
            if isinstance(offsets, dict):
                item['offsets_ms'] = {k: offsets[k] for k in ('from', 'to')
                                      if type(offsets.get(k)) in (int, float) and math.isfinite(offsets[k])}
            segments.append(item)
        return {'available': True, 'language': data.get('result', {}).get('language'), 'segments': segments}
    except (ValueError, TypeError, AttributeError, UnicodeError):
        # Some CLI revisions serialize unusual text imperfectly; the .txt result
        # remains canonical and must not become an empty/error transcript.
        return {'available': False, 'reason': 'unreadable_diagnostic_json'}


def serialize_report(report: dict) -> str:
    text = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if len(text.encode('utf-8')) > MAX_REPORT_BYTES:
        report = {**report, 'segments': {'available': False, 'reason': 'report_size_limit'}}
        text = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if len(text.encode('utf-8')) > MAX_REPORT_BYTES:
        raise ValueError('STT report too large')
    return text
