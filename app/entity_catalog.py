"""Request-local, metadata-only catalog over server-read rows.

Not a new registry, database, cache, or model prompt. Selection delegates to the
existing exact-normalized / unique-literal-substring Resolver. No fuzzy aliases.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .entity_resolver import Resolution, resolve_target

CATALOG_VERSION = '1.0.0'
SOURCES = {'todo': 'room_hub_sqlite.tasks', 'calendar': 'room_hub_sqlite.tasks',
           'memo': 'room_hub_sqlite.hub_notes'}
LIMITS = {'todo': 10000, 'calendar': 10000, 'memo': 200}


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    title: str
    version: int
    date: str | None = None
    time: str | None = None
    completed: bool | None = None
    shared: bool | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    domain: str
    source: str
    as_of: str
    entries: tuple[CatalogEntry, ...]
    truncated: bool
    scope: tuple[tuple[str, str | bool | None], ...]

    @classmethod
    def from_rows(cls, domain: str, rows: list[dict], *, source: str, as_of: str,
                  scope: dict, truncated: bool = False):
        if domain not in SOURCES or source != SOURCES[domain]:
            raise ValueError('Invalid catalog source')
        if not isinstance(as_of, str) or not as_of or type(truncated) is not bool:
            raise ValueError('Invalid catalog snapshot metadata')
        if any(not isinstance(k, str) or (v is not None and type(v) not in (str, bool))
               for k, v in scope.items()):
            raise ValueError('Invalid catalog scope')
        entries = []
        for row in rows[:LIMITS[domain]]:
            if (not isinstance(row.get('id'), str) or not row['id']
                    or not isinstance(row.get('title'), str)
                    or type(row.get('version')) is not int or row['version'] < 1):
                raise ValueError('Invalid server catalog row')
            optional = {k: row.get(k) for k in ('date', 'time', 'completed', 'shared', 'updated_at')}
            if any(v is not None and type(v) is not (bool if k in {'completed', 'shared'} else str)
                   for k, v in optional.items()):
                raise ValueError('Invalid server catalog metadata')
            entries.append(CatalogEntry(row['id'], row['title'], row['version'], **optional))
        if len({e.id for e in entries}) != len(entries):
            raise ValueError('Duplicate server catalog identities')
        return cls(domain, source, as_of, tuple(entries), truncated or len(rows) > LIMITS[domain],
                   tuple(sorted(scope.items())))

    def resolve(self, target_text: str) -> Resolution:
        # New dicts are returned, so mutating evidence cannot mutate the snapshot.
        return resolve_target([asdict(e) for e in self.entries], target_text, truncated=self.truncated)

    def evidence(self) -> dict:
        digest = content_hash({'version': CATALOG_VERSION, 'domain': self.domain,
            'source': self.source, 'scope': dict(self.scope), 'truncated': self.truncated,
            'entries': [asdict(e) for e in sorted(self.entries, key=lambda e: e.id)]})
        return {'version': CATALOG_VERSION, 'domain': self.domain, 'source': self.source,
                'as_of': self.as_of, 'scope': dict(self.scope), 'entry_count': len(self.entries),
                'truncated': self.truncated, 'content_sha256': digest,
                'lifetime': 'this_grounding_only_no_cross_request_cache',
                'selection': 'existing_exact_then_unique_literal_substring',
                'body_included': False, 'sent_to_model': False}
