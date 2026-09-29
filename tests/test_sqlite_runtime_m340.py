"""Synthetic lifetime/isolation regressions; never uses deployment data."""
import gc
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.sqlite_connection import ClosingConnection
from app.store import Store
from benchmarks.storage import ClosingConnection as BenchmarkConnection, owned_sqlite


def test_shared_factory_identity_and_transaction_settings(tmp_path):
    assert ClosingConnection is BenchmarkConnection
    store = Store(tmp_path / 'sample.sqlite3')
    db = store.connect()
    with db:
        assert type(db) is ClosingConnection
        assert db.row_factory is sqlite3.Row
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 15000
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute('SELECT 1')


@pytest.mark.parametrize('fail', [False, True])
def test_commit_rollback_and_deterministic_close(tmp_path, fail):
    store = Store(tmp_path / 'sample.sqlite3')
    db = store.connect()
    try:
        with db:
            db.execute("INSERT INTO kv VALUES('synthetic-transaction','17')")
            if fail:
                raise LookupError('synthetic exception retained by test')
    except LookupError:
        pass
    assert store.get('synthetic-transaction') == (None if fail else 17)
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute('SELECT 1')


def test_failed_commit_closes_and_rolls_back(tmp_path):
    store = Store(tmp_path / 'sample.sqlite3')
    with store.connect() as db:
        db.executescript('CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(pid REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);')
    db = store.connect()
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute('INSERT INTO c VALUES(999)')
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute('SELECT 1')
    with store.connect() as fresh:
        assert fresh.execute('SELECT COUNT(*) FROM c').fetchone()[0] == 0


def test_pragma_failure_does_not_leak(monkeypatch, tmp_path):
    store = Store(tmp_path / 'sample.sqlite3')
    class Broken:
        closed = False
        def execute(self, *args):
            raise sqlite3.OperationalError('synthetic pragma failure')
        def close(self):
            self.closed = True
    db = Broken()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: db)
    with pytest.raises(sqlite3.OperationalError):
        store.connect()
    assert db.closed


def test_benchmark_accepts_only_exact_shared_factory_and_owned_paths(tmp_path):
    class NotReviewed(ClosingConnection):
        def __init__(self, *a, **k):
            pytest.fail('unreviewed factory must not be constructed')
    inside = tmp_path / 'owned'; inside.mkdir()
    with owned_sqlite(inside):
        store = Store(inside / 'sample.sqlite3')
        assert store.get('revision') == 0
        for factory in (NotReviewed, sqlite3.Connection, lambda *a, **k: None):
            with pytest.raises(RuntimeError, match='unreviewed'):
                sqlite3.connect(inside / 'other.sqlite3', factory=factory)
        with pytest.raises(RuntimeError, match='outside'):
            sqlite3.connect(tmp_path / 'outside.sqlite3', factory=ClosingConnection)
        with pytest.raises(RuntimeError, match='unreviewed'):
            sqlite3.connect(inside / 'other.sqlite3', 5, 0, '', True, ClosingConnection)
    assert not (tmp_path / 'outside.sqlite3').exists()


def _db_fds(root):
    count = 0
    for fd in Path('/proc/self/fd').iterdir():
        try:
            count += str(root) in str(fd.readlink())
        except OSError:
            pass
    return count


@pytest.mark.skipif(not Path('/proc/self/fd').exists(), reason='Linux FD observation only')
def test_fd_stable_without_gc_and_with_retained_connections(tmp_path):
    store = Store(tmp_path / 'sample.sqlite3')
    before = _db_fds(tmp_path)
    retained = []
    was_enabled = gc.isenabled(); gc.disable()
    try:
        for i in range(300):
            db = store.connect(); retained.append(db)
            with db:
                db.execute("INSERT OR REPLACE INTO kv VALUES('fd-check', ?)", (str(i),))
            assert store.get('fd-check') == i
        assert _db_fds(tmp_path) == before
    finally:
        if was_enabled:
            gc.enable()


def test_repeated_manager_pair_revoke_uses_closed_connections(tmp_path):
    from app.main import create_app
    app = create_app(tmp_path, weather_enabled=False)
    with TestClient(app) as client:
        client.headers.update({'Authorization': 'Bearer ' + app.state.admin_token})
        for i in range(30):
            pair = client.post('/api/devices/pair', json={'name': 'synthetic-screen'}).json()
            assert client.get('/api/admin/overview').status_code == 200
            assert client.delete('/api/devices/' + pair['device_id']).status_code == 200
        assert client.get('/healthz').status_code == 200
        with app.state.store.connect() as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_backup_destination_and_source_both_close(tmp_path, monkeypatch):
    from app.main import create_app
    app=create_app(tmp_path,weather_enabled=False)
    original=sqlite3.connect
    retained=[]
    def track(*args,**kwargs):
        db=original(*args,**kwargs);retained.append(db);return db
    with TestClient(app) as client:
        client.headers.update({'Authorization':'Bearer '+app.state.admin_token})
        monkeypatch.setattr(sqlite3,'connect',track)
        for _ in range(5):
            result=client.get('/api/admin/backup')
            assert result.status_code==200 and result.content.startswith(b'SQLite format 3')
        assert retained and all(type(db) is ClosingConnection for db in retained)
        for db in retained:
            with pytest.raises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
