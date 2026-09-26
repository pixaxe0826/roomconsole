"""Synthetic request-local metadata catalogs; no real suite, body data or model."""
from dataclasses import FrozenInstanceError
import json
import unicodedata

import pytest

from app.entity_catalog import EntityCatalog, CATALOG_VERSION
from app.entity_resolver import resolve_target


def row(identity='one', title='합성 분류', **kw):
    return dict(id=identity, title=title, version=1, date='2028-03-01', time=None,
                completed=False, **kw)


def catalog(rows, domain='todo', **kw):
    source = 'room_hub_sqlite.hub_notes' if domain == 'memo' else 'room_hub_sqlite.tasks'
    return EntityCatalog.from_rows(domain, rows, source=source, as_of='2028-03-01T09:00:00+09:00',
                                   scope={'date': None, 'completion_filter': 'all'}, **kw)


@pytest.mark.parametrize('domain', ['todo', 'calendar', 'memo'])
@pytest.mark.parametrize('text', ['합성 분류', '합성  분류', '분류', '합성', '합', '합성분류', '다른 이름'])
def test_catalog_delegates_exactly_to_existing_resolver(domain, text):
    rows = [row('one'), row('two', '합성 분류 보관')]
    old = resolve_target(rows, text)
    new = catalog(rows, domain).resolve(text)
    assert (old.status, old.match_policy, [r['id'] for r in old.matches]) == (
        new.status, new.match_policy, [r['id'] for r in new.matches])


def test_snapshot_is_frozen_and_results_are_detached():
    source = row()
    c = catalog([source])
    source['title'] = '원본 참조를 통한 변조'
    with pytest.raises(FrozenInstanceError):
        c.entries[0].title = '변조'
    result = c.resolve('합성 분류')
    result.matches[0]['title'] = '반환값 변조'
    assert c.resolve('합성 분류').matches[0]['title'] == '합성 분류'


def test_bodies_permissions_notes_and_prompts_do_not_enter_catalog():
    c = catalog([row(body='BODY_SENTINEL', notes='NOTES_SENTINEL', permission='admin', confirmed_digest='x')])
    dumped = str(c) + json.dumps(c.evidence()) + str(c.resolve('합성 분류'))
    assert 'BODY_SENTINEL' not in dumped and 'NOTES_SENTINEL' not in dumped
    assert 'confirmed_digest' not in dumped and 'permission' not in dumped
    assert c.evidence()['sent_to_model'] is False and c.evidence()['body_included'] is False


def test_hash_ignores_order_and_time_but_captures_metadata_and_scope():
    rows = [row('a'), row('b', '다른 제목')]
    c = catalog(rows)
    assert c.evidence()['content_sha256'] == catalog(list(reversed(rows))).evidence()['content_sha256']
    changed = [dict(rows[0], version=2), rows[1]]
    assert c.evidence()['content_sha256'] != catalog(changed).evidence()['content_sha256']
    d = EntityCatalog.from_rows('todo', rows, source=c.source, as_of='later', scope={'date':'2028-03-01'})
    assert c.evidence()['content_sha256'] != d.evidence()['content_sha256']


def test_exact_normalized_duplicates_include_private_and_completed():
    rows = [row('public', shared=True), dict(row('private', unicodedata.normalize('NFD','합성 분류')),
                                         shared=False, completed=True)]
    assert catalog(rows, 'memo').resolve('합성 분류').status == 'ambiguous'


@pytest.mark.parametrize('rows', [
    [{'id':'a', 'title':'x'}], [dict(row(), version=True)], [dict(row(), version=0)],
    [dict(row(), id='')], [dict(row(), id=5)], [dict(row(), title=None)],
    [dict(row(), completed='yes')], [row('same'), row('same','다른 제목')],
])
def test_bad_server_shapes_fail_closed(rows):
    with pytest.raises(ValueError): catalog(rows)


def test_refuse_truncated_even_with_exact_match():
    assert catalog([row()], truncated=True).resolve('합성 분류').status == 'incomplete'
    many = [row(str(i), '합성 분류') for i in range(201)]
    c = catalog(many, 'memo')
    assert len(c.entries) == 200 and c.truncated
    assert c.resolve('합성 분류').status == 'incomplete'


def test_unsupported_domain_and_source_not_accepted():
    with pytest.raises(ValueError):catalog([row()], 'shell')
    with pytest.raises(ValueError):
        EntityCatalog.from_rows('memo', [row()], source='model', as_of='now', scope={})
