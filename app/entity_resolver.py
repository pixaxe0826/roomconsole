"""Single-turn selection over SERVER-READ rows; no fuzzy matching or conversation state.

A partial query cannot establish uniqueness. Matching never removes candidates by
completion state to turn an ambiguous title into a convenient mutation target.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


def normalize_title(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value)).strip()


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    match_policy: str
    matches: tuple[dict, ...]

    def evidence(self) -> dict:
        return {'status': self.status, 'match_policy': self.match_policy,
                'candidate_count': len(self.matches),
                'candidates': [{k: v.get(k) for k in ('id', 'title', 'date', 'time', 'version')}
                               for v in self.matches[:5]],
                'candidates_truncated': len(self.matches) > 5}


def resolve_target(items: list[dict], target_text: str, *, truncated: bool = False) -> Resolution:
    if truncated:
        return Resolution('incomplete', 'refuse_truncated_query', ())
    needle = normalize_title(target_text)
    if not needle:
        return Resolution('missing', 'no_target_text', ())
    if any(not isinstance(r.get('id'), str) or not isinstance(r.get('title'), str)
           or not isinstance(r.get('version'), int) or isinstance(r.get('version'), bool)
           for r in items):
        raise ValueError('Invalid server target rows')
    exact = tuple(r for r in items if normalize_title(r['title']) == needle)
    if exact:
        return Resolution('resolved' if len(exact) == 1 else 'ambiguous', 'exact_normalized', exact)
    # Do not broaden one-character references or guess/edit the transcript.
    if len(re.sub(r'\s+', '', needle)) < 2:
        return Resolution('missing', 'short_substring_refused', ())
    partial = tuple(r for r in items if needle in normalize_title(r['title']))
    return Resolution('resolved' if len(partial) == 1 else 'ambiguous' if partial else 'missing',
                      'unique_literal_substring', partial)
