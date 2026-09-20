"""Private Room Hub data transfer. This is NOT a GitHub/source archive.

Uses only Python's standard library (Windows or Termux). Stop ALL writers first:
SQLite uses the backup API, but audio files and widget code are copied separately.
Never upload the resulting ZIP: it contains authentication keys and personal data.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile

TABLES = {'kv', 'tasks', 'series', 'devices', 'sessions', 'pairs', 'voice', 'audit'}
MAX_BYTES = 2 * 1024**3


def check_database(path: Path) -> int:
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed.')
        tables = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES <= tables:
            raise ValueError('Not a Room Hub database.')
        return db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]


def copy_tree(source: Path, dest: Path, excluded: set[str] | None = None) -> None:
    excluded = excluded or set()
    if source.is_symlink():
        raise ValueError('Symbolic links are not accepted in migration data.')
    for base, dirs, files in os.walk(source, followlinks=False):
        base = Path(base)
        if any((base / d).is_symlink() for d in dirs):
            raise ValueError('Directory symbolic link found in migration data.')
        for name in files:
            p = base / name
            if p.is_symlink():
                raise ValueError('File symbolic link found in migration data.')
            rel = p.relative_to(source)
            if rel.as_posix() in excluded:
                continue
            q = dest / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)


def export_data(source: Path, output: Path, data_dir: Path | None = None) -> dict:
    source = source.resolve()
    data = (data_dir or source / 'data').resolve()
    output = output.resolve()
    if not (source / 'app/main.py').is_file():
        raise ValueError('Source must be the RUNNING project root with app/main.py.')
    if output.exists():
        raise FileExistsError('Output exists. Choose a new output filename.')
    if output.is_relative_to(data) or output.is_relative_to(source / 'widgets'):
        raise ValueError('Write the backup outside data/ and widgets/.')
    if not (source / 'widgets').is_dir():
        raise ValueError('Source widgets/ is missing.')
    db_path = data / 'room-hub.sqlite3'
    tasks = check_database(db_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='roomhub-private-') as tmp:
        stage = Path(tmp)
        (stage / 'data').mkdir()
        excluded = {'room-hub.sqlite3', 'room-hub.sqlite3-wal', 'room-hub.sqlite3-shm', '.v35-server.lock'}
        copy_tree(data, stage / 'data', excluded)
        dest_db = stage / 'data/room-hub.sqlite3'
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(dest_db) as dst:
            src.backup(dst)
        # Only the COPY is modified. Authentication must be re-established on V35.
        with sqlite3.connect(dest_db) as db:
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM pairs')
            db.execute('UPDATE devices SET revoked=1')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        for suffix in ('-wal', '-shm'):
            Path(str(dest_db) + suffix).unlink(missing_ok=True)
        check_database(dest_db)
        # Preserve explicitly configured token values when the caller inherited them.
        for env, filename in [('HUB_ADMIN_TOKEN', 'admin-token.txt'), ('HUB_INGEST_TOKEN', 'ingest-token.txt')]:
            value = os.environ.get(env)
            if value:
                if len(value) < 24:
                    raise ValueError(f'{env} is shorter than the server minimum.')
                (stage / 'data' / filename).write_text(value + '\n', encoding='utf-8')
        copy_tree(source / 'widgets', stage / 'widgets')
        files = sorted(p for p in stage.rglob('*') if p.is_file())
        info = {'kind': 'room-hub-private-migration', 'schema': 1, 'task_count': tasks,
                'sessions_cleared': True,
                'files': [{'path': p.relative_to(stage).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in files]}
        with output.open('xb') as raw, zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, p.relative_to(stage).as_posix())
            z.writestr('migration-manifest.json', json.dumps(info, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    return {'archive': str(output), 'task_count': tasks, 'contains_private_keys_and_data': True,
            'message': 'Source unchanged. The imported copy will require login and iPad pairing.'}


def import_data(archive: Path, target: Path) -> dict:
    target = target.resolve()
    if not (target / 'app/main.py').is_file():
        raise ValueError('Target must be the NEW Room Hub project root.')
    if (target / 'data').exists() or (target / 'data').is_symlink():
        raise FileExistsError('Target data/ already exists. Nothing overwritten. Inspect/backup it before proceeding.')
    if (target / 'widgets').is_symlink():
        raise ValueError('Target widgets must not be a symbolic link.')
    with tempfile.TemporaryDirectory(prefix='.roomhub-import-', dir=target.parent) as tmp:
        stage = Path(tmp)
        with zipfile.ZipFile(archive) as z:
            entries = [i for i in z.infolist() if not i.is_dir()]
            if len(entries) > 10000 or sum(i.file_size for i in entries) > MAX_BYTES:
                raise ValueError('Archive exceeds transfer safety limits (10,000 files / 2 GiB).')
            names = set()
            for i in entries:
                p = PurePosixPath(i.filename)
                if '\\' in i.filename or ':' in i.filename or p.is_absolute() or '..' in p.parts:
                    raise ValueError('Unsafe archive member path.')
                if not p.parts or (p.parts[0] not in {'data', 'widgets'} and p.as_posix() != 'migration-manifest.json'):
                    raise ValueError('Unexpected archive content.')
                if stat.S_ISLNK(i.external_attr >> 16) or i.filename.casefold() in names:
                    raise ValueError('Symlink or duplicate path in archive.')
                names.add(i.filename.casefold())
                q = stage / p.as_posix()
                q.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, q.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
            manifest = json.loads((stage / 'migration-manifest.json').read_text(encoding='utf-8'))
            if manifest.get('kind') != 'room-hub-private-migration' or manifest.get('schema') != 1:
                raise ValueError('Unrecognized transfer format.')
            expected = {f['path']: f['sha256'] for f in manifest['files']}
            actual = {i.filename for i in entries} - {'migration-manifest.json'}
            if set(expected) != actual:
                raise ValueError('Manifest/file list mismatch.')
            for name, sha in expected.items():
                if hashlib.sha256((stage / name).read_bytes()).hexdigest() != sha:
                    raise ValueError('Transfer checksum mismatch.')
        tasks = check_database(stage / 'data/room-hub.sqlite3')
        merged = stage / 'merged-widgets'
        merged.mkdir()
        if (target / 'widgets').exists():
            copy_tree(target / 'widgets', merged)
        if (stage / 'widgets').exists():
            copy_tree(stage / 'widgets', merged)
        # Private data on Android must be inside Termux HOME, not shared storage.
        for p in (stage / 'data').rglob('*'):
            p.chmod(0o700 if p.is_dir() else 0o600)
        (stage / 'data').chmod(0o700)
        backup = target / 'backups' / f'widgets-before-import-{time.time_ns()}'
        backup.parent.mkdir(parents=True, exist_ok=True)
        moved_old = False
        try:
            if (target / 'widgets').exists():
                (target / 'widgets').rename(backup)
                moved_old = True
            merged.rename(target / 'widgets')
            (stage / 'data').rename(target / 'data')
        except Exception:
            if (target / 'widgets').exists():
                shutil.rmtree(target / 'widgets')
            if moved_old:
                backup.rename(target / 'widgets')
            raise
    return {'target': str(target), 'task_count': tasks, 'login_and_pair_again': True,
            'message': 'Imported data and widgets. Private transfer ZIP can be removed after verification.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    exp = sub.add_parser('export')
    exp.add_argument('--source', type=Path, required=True)
    exp.add_argument('--output', type=Path, required=True)
    exp.add_argument('--data-dir', type=Path, help='Use when HUB_DATA_DIR was customized on the old server.')
    exp.add_argument('--server-stopped', action='store_true', required=True,
                     help='Confirm ALL writers are stopped; the flag cannot stop/detect them for you.')
    imp = sub.add_parser('import')
    imp.add_argument('archive', type=Path)
    imp.add_argument('--target', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        os.umask(0o077)
        out = export_data(args.source, args.output, args.data_dir) if args.command == 'export' else import_data(args.archive, args.target)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, KeyError) as exc:
        parser.exit(1, f'Migration stopped: {exc}\n')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
