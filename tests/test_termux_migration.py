"""Host-only tests. These do not emulate Termux, PRoot or Android."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v35migration', ROOT / 'deploy/termux/migration.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture_source(tmp):
    root = tmp / 'old'
    (root / 'app').mkdir(parents=True)
    (root / 'app/main.py').write_text('# fixture only')
    (root / 'widgets/custom').mkdir(parents=True)
    (root / 'widgets/custom/widget.js').write_text('// custom widget')
    store = Store(root / 'data/room-hub.sqlite3')
    with store.connect() as db:
        db.execute("INSERT INTO tasks VALUES ('id','테스트','2026-09-17',NULL,'personal','normal','',1,NULL,2,'now','now')")
        db.execute("INSERT INTO devices(id,name,created_at) VALUES ('d','test','now')")
        db.execute("INSERT INTO sessions VALUES ('testhash','display','d',99999999999)")
    (root / 'data/admin-token.txt').write_text('test-only-token-never-production-12345\n')
    (root / 'data/audio').mkdir()
    (root / 'data/audio/example.txt').write_text('fixture audio placeholder')
    return root


def target(tmp):
    root = tmp / 'new'
    (root / 'app').mkdir(parents=True)
    (root / 'app/main.py').write_text('# fixture')
    (root / 'widgets/clock').mkdir(parents=True)
    (root / 'widgets/clock/widget.js').write_text('// bundled clock')
    return root


def test_round_trip_preserves_tasks_keys_audio_widgets(tmp_path):
    source = fixture_source(tmp_path)
    out = tmp_path / 'private.zip'
    assert m.export_data(source, out)['task_count'] == 1
    dst = target(tmp_path)
    assert m.import_data(out, dst)['task_count'] == 1
    with sqlite3.connect(dst / 'data/room-hub.sqlite3') as db:
        assert db.execute('SELECT title,completed,version FROM tasks').fetchone() == ('테스트',1,2)
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        assert db.execute('SELECT revoked FROM devices').fetchone()[0] == 1
    assert (dst / 'data/admin-token.txt').read_text() == (source / 'data/admin-token.txt').read_text()
    assert (dst / 'data/audio/example.txt').read_text() == 'fixture audio placeholder'
    assert (dst / 'widgets/custom/widget.js').is_file()
    assert (dst / 'widgets/clock/widget.js').is_file()
    with sqlite3.connect(source / 'data/room-hub.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        assert db.execute('SELECT revoked FROM devices').fetchone()[0] == 0


def test_refuses_target_with_existing_data(tmp_path):
    src = fixture_source(tmp_path); out = tmp_path / 'private.zip'
    m.export_data(src,out); dst = target(tmp_path)
    (dst / 'data').mkdir()
    with pytest.raises(FileExistsError): m.import_data(out,dst)


def test_refuses_duplicate_export(tmp_path):
    src = fixture_source(tmp_path); out = tmp_path / 'private.zip'
    m.export_data(src,out)
    with pytest.raises(FileExistsError): m.export_data(src,out)


def test_refuses_symlinks(tmp_path):
    src = fixture_source(tmp_path)
    (src / 'data/link').symlink_to(src / 'app/main.py')
    with pytest.raises(ValueError): m.export_data(src,tmp_path/'private.zip')


def test_refuses_traversal_archive(tmp_path):
    out=tmp_path/'bad.zip'
    with zipfile.ZipFile(out,'w') as z: z.writestr('../escape.txt','bad')
    dst=target(tmp_path)
    with pytest.raises(ValueError): m.import_data(out,dst)
    assert not (tmp_path/'escape.txt').exists()


def test_checksum_mismatch_has_no_effect(tmp_path):
    src=fixture_source(tmp_path); out=tmp_path/'private.zip'; m.export_data(src,out)
    changed=tmp_path/'tampered.zip'
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(changed,'w') as new:
        for name in z.namelist(): new.writestr(name,b'changed' if name.endswith('admin-token.txt') else z.read(name))
    dst=target(tmp_path)
    with pytest.raises(ValueError): m.import_data(changed,dst)
    assert not (dst/'data').exists()
    assert (dst/'widgets/clock/widget.js').read_text() == '// bundled clock'


def test_wal_snapshot(tmp_path):
    src=fixture_source(tmp_path)
    # Hold a reader connection open, ensuring WAL-backed data can be backed up.
    with sqlite3.connect(src/'data/room-hub.sqlite3') as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE tasks SET notes='from WAL'"); db.commit()
        out=tmp_path/'private.zip'; m.export_data(src,out)
        dst=target(tmp_path);m.import_data(out,dst)
        with sqlite3.connect(dst/'data/room-hub.sqlite3') as copied:
            assert copied.execute('SELECT notes FROM tasks').fetchone()[0]=='from WAL'


def test_explicit_data_directory(tmp_path):
    src=fixture_source(tmp_path)
    alternate=tmp_path/'custom-data'; (src/'data').rename(alternate)
    out=tmp_path/'private.zip';m.export_data(src,out,alternate)
    dst=target(tmp_path); assert m.import_data(out,dst)['task_count']==1
