"""Bounded declarative memo sentence families; no service or permission code.

Design references (not vendored code): HassIL sentence data/raw spans/sample,
OVOS ordered pipelines, Rhasspy raw-value separation. See NLU_REFERENCE_REVIEW.
This is NOT a general HassIL/YAML implementation or an independent learned NLU.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

GRAMMAR_VERSION = '1.0.0'
MAX_SOURCE_LENGTH = 1500
NOUN = r'(?:메모(?!리)|노트)'
PLEASE = r'(?:\s*(?:줘|주세요|줘요))?(?:요)?'
READ_VERB = (r'(?:읽어\s*(?:줘|주세요|줘요)|보여\s*(?:줘|주세요|줘요)|'
             r'알려\s*(?:줘|주세요|줘요)|확인해\s*(?:줘|주세요|줘요))(?:요)?')
READ_TAIL = r'(?:의)?(?:\s*(?:내용|본문))?(?:을|를)?\s*(?:좀\s*)?(?P<verb>' + READ_VERB + r')'
# Only a complete selector phrase; "최근 연구" can still be a literal name.
RECENT = (r'(?:(?:가장|제일)\s+최근(?:에)?\s*|최근(?:에)?\s*|마지막으로\s*)'
          r'(?:수정한|손댄|건드린|편집한|저장한|작성한)')
RECENT_READ = r'(?P<selector>' + RECENT + r')\s*' + NOUN + READ_TAIL
PREFIX = r'(?P<title>.+?)\s+' + NOUN
WRAPPED = r'(?P<title>.+?)(?P<naming>(?:이)?라고\s*(?:해\s*둔|이름\s*붙인)|(?:이)?라는)\s+' + NOUN
CONTENT_FIRST = (r'(?P<text>.+?)(?P<quotative>(?:이)?라고)\s+'
                 r'(?P<selector>현재|지금)\s*' + NOUN + r'에\s*(?:한\s*줄\s*)?'
                 r'(?P<verb>덧붙여|추가해)' + PLEASE)
SEARCH = (r'(?P<query>.+?)\s*(?:들어간|포함된|관련된|관련)\s*' + NOUN +
          r'(?:을|를)?\s*(?P<verb>' + READ_VERB + r'|(?:찾아|검색해)' + PLEASE + r')')
PRONOUN = r'(?P<reference>그|이|저|그거|이거|저거)\s+' + NOUN + READ_TAIL


@dataclass(frozen=True, slots=True)
class SentenceFamily:
    name: str
    stage: int
    action: str
    disposition: Literal['named', 'delegate', 'block']
    pattern: str
    selector: str | None = None
    issue: str | None = None


FAMILIES = (
    SentenceFamily('unsupported.search', 0, 'read', 'block', SEARCH, issue='MEMO_SEARCH_UNSUPPORTED'),
    SentenceFamily('unsupported.reference', 0, 'read', 'block', PRONOUN, issue='CONTEXT_REFERENCE_UNSUPPORTED'),
    SentenceFamily('selector.latest', 10, 'read', 'delegate', RECENT_READ, selector='last_modified'),
    # Explicit current selector must not be swallowed by a greedy title prefix.
    # Delegate to the unchanged model/source validator; no new write executor.
    SentenceFamily('selector.content_first_append', 10, 'append', 'delegate', CONTENT_FIRST, selector='current'),
    *(SentenceFamily('named.wrapper.' + action, 20, action, 'named', WRAPPED + tail) for action, tail in (
        ('read', READ_TAIL),
        ('clear', r'(?:의)?(?:\s*(?:내용|본문))?(?:을|를)?\s*(?P<verb>비워)' + PLEASE),
        ('append', r'(?:의\s*본문)?에\s+(?P<text>.+?)\s*(?P<verb>덧붙여|추가해)' + PLEASE),
        ('write', r'(?:의)?(?:\s*(?:내용|본문))?(?:을|를)\s+(?P<body>.+?)(?:으로|로)\s*(?P<verb>바꿔|교체해)' + PLEASE),
    )),
    *(SentenceFamily('named.literal.' + action, 30, action, 'named', PREFIX + tail) for action, tail in (
        ('read', READ_TAIL),
        ('clear', r'(?:의)?(?:\s*(?:내용|본문))?(?:을|를)?\s*(?P<verb>비워)' + PLEASE),
        ('append', r'(?:의\s*본문)?에\s+(?P<text>.+?)\s*(?P<verb>덧붙여|추가해)' + PLEASE),
        ('write', r'(?:의)?(?:\s*(?:내용|본문))?(?:을|를)\s+(?P<body>.+?)(?:으로|로)\s*(?P<verb>바꿔|교체해)' + PLEASE),
    )),
)


def validate_families(families=FAMILIES):
    names = set()
    for family in families:
        if family.name in names or family.action not in {'read', 'clear', 'append', 'write'}:
            raise ValueError('Invalid or duplicate linguistic family')
        names.add(family.name)
        if family.disposition not in {'named', 'delegate', 'block'} or family.stage < 0:
            raise ValueError('Invalid linguistic disposition')
        groups = re.compile(family.pattern).groupindex
        needed = {'title'} if family.disposition == 'named' else set()
        if family.disposition == 'named' and family.action in {'append', 'write'}:
            needed.add('text' if family.action == 'append' else 'body')
        if not needed <= groups.keys() or (family.disposition == 'block' and not family.issue):
            raise ValueError('Missing typed source field')


validate_families()
COMPILED = tuple((f, re.compile(f.pattern)) for f in FAMILIES)


def grammar_hash() -> str:
    from dataclasses import asdict
    manifest = {'version': GRAMMAR_VERSION, 'families': [asdict(f) for f in FAMILIES]}
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LanguageMatch:
    family: SentenceFamily
    # Captures are raw offsets. Normalizing an entity name does not rewrite body.
    captures: tuple[tuple[str, int, int, str], ...]
    shadowed: tuple[str, ...] = ()

    def value(self, name: str) -> str | None:
        return next((value for key, _, _, value in self.captures if key == name), None)

    def audit(self) -> dict:
        return {'version': GRAMMAR_VERSION, 'hash': grammar_hash(), 'family': self.family.name,
                'stage': self.family.stage, 'disposition': self.family.disposition,
                'selector': self.family.selector, 'issue': self.family.issue,
                'shadowed_families': list(self.shadowed), 'policy': 'first_stage_unique_semantics',
                'authority': 'none_linguistic_candidate_only'}


def recognize(raw: str, *, compiled=COMPILED) -> LanguageMatch | None:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_SOURCE_LENGTH:
        return None
    # Trailing presentation punctuation only; never strip/replace inside literals.
    text = re.sub(r'[?？!！.。]+$', '', raw).rstrip()
    matches = []
    for family, pattern in compiled:
        match = pattern.fullmatch(text)
        if match:
            spans = tuple((key, match.start(key), match.end(key), match[key])
                          for key in pattern.groupindex if match[key] is not None)
            matches.append(LanguageMatch(family, spans))
    if not matches:
        return None
    first = min(m.family.stage for m in matches)
    eligible = [m for m in matches if m.family.stage == first]
    def meaning(m):
        return (m.family.action, m.family.disposition, m.family.selector, m.family.issue,
                tuple((key, m.value(key)) for key in ('title', 'text', 'body')))
    if len({meaning(m) for m in eligible}) != 1:
        block = SentenceFamily('ambiguous.same_stage', first, 'read', 'block', '',
                               issue='MEMO_GRAMMAR_AMBIGUOUS')
        return LanguageMatch(block, (), tuple(sorted(m.family.name for m in matches)))
    chosen = sorted(eligible, key=lambda m: m.family.name)[0]
    return LanguageMatch(chosen.family, chosen.captures,
                         tuple(sorted(m.family.name for m in matches if m is not chosen)))
