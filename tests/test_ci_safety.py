"""Regression coverage for the Windows test-identity failure; no test skips."""
from conftest import pytest_make_parametrize_id


def test_large_binary_id_is_bounded_stable_and_distinct():
    value = b'x' * (10 * 1024 * 1024 + 1)
    first = pytest_make_parametrize_id(None, value, 'blob')
    assert first.startswith('blob-bytes-10485761-') and len(first) < 80
    assert first == pytest_make_parametrize_id(None, value, 'blob')
    assert first != pytest_make_parametrize_id(None, b'y' * len(value), 'blob')
    assert len(value) == 10485761


def test_long_unicode_id_is_bounded():
    assert len(pytest_make_parametrize_id(None, '가' * 5000, 'text')) < 80


def test_small_values_keep_default_pytest_ids():
    for value in ('short', b'RIFF', 413, None, {'blob': 'synthetic'}):
        assert pytest_make_parametrize_id(None, value, 'value') is None


def test_collected_ids_are_bounded_and_oversize_test_remains(request):
    items = request.session.items
    assert all(len(item.nodeid) <= 4096 for item in items)
    cases = [item for item in items if item.originalname == 'test_file_rejections']
    if cases:
        oversized = [item for item in cases if len(item.callspec.params['blob']) > 10 * 1024 * 1024]
        assert len(oversized) == 1
        assert len(oversized[0].callspec.params['blob']) == 10485761
        assert oversized[0].callspec.params['status'] == 413
