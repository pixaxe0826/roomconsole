"""Synthetic literal target selection; server metadata is not model authority."""
import unicodedata

import pytest
from app.entity_resolver import resolve_target


def row(id, title, version=1):
    return dict(id=id, title=title, version=version, date='2028-02-29', time=None)


def test_exact_then_literal_substring_not_fuzzy():
    items = [row('server-a', '도서 포장'), row('server-b', '도서 포장 준비')]
    result = resolve_target(items, '도서  포장')
    assert result.status == 'resolved' and result.matches[0]['id'] == 'server-a'
    assert result.match_policy == 'exact_normalized'
    assert resolve_target(items, '도서').status == 'ambiguous'
    assert resolve_target(items, '도').status == 'missing'
    assert resolve_target(items, '도서포장').status == 'missing'  # no ASR/fuzzy rewrite
    assert resolve_target(items, '택배 포장').status == 'missing'
    assert resolve_target([items[1]], '포장 준비').status == 'resolved'


def test_unicode_whitespace_same_title_duplicates_are_ambiguous():
    items = [row('server-a', '도서 포장'), row('server-b', unicodedata.normalize('NFD', '도서 포장'))]
    assert resolve_target(items, '도서 포장').status == 'ambiguous'
    items[1]['date'] = '2028-03-03'
    items[1]['completed'] = True
    assert resolve_target(items, '도서 포장').status == 'ambiguous'


def test_truncation_cannot_prove_unique_even_if_exact():
    result = resolve_target([row('server-a', '도서 포장')], '도서 포장', truncated=True)
    assert result.status == 'incomplete' and not result.matches


@pytest.mark.parametrize('items', [[{'id': 'a', 'title': 'test'}], [row('a', 'test', True)], [row(1, 'test')], [row('a', None)]])
def test_bad_server_rows_fail_closed(items):
    with pytest.raises(ValueError):
        resolve_target(items, 'test')


def test_candidate_output_is_bounded():
    result = resolve_target([row(str(i), '도서 포장') for i in range(10)], '도서 포장')
    evidence = result.evidence()
    assert len(evidence['candidates']) == 5 and evidence['candidates_truncated']
    assert evidence['candidate_count'] == 10
