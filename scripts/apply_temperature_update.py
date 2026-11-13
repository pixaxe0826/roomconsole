#!/usr/bin/env python3
"""Source-only indoor-sensors installer; never imports Room Hub or reads its data.

Only two reviewed statements are inserted into the existing app/main.py. All its
other bytes, including local runtime changes, remain intact. Unknown integration
structure is a conflict, never a reason to overwrite main.py with a bundled copy.
This complete patch installs both widgets without any earlier temperature patch.
Existing feature files may also be upgraded from the exact released v1/v2 bytes.
Unknown local edits are never accepted as a prior version.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import sys
import tempfile

PATCH_ID = 'room-hub-indoor-sensors-v3'
MAIN_PATCH = 'insert-temperature-routes-v1'
PAYLOAD_PATHS = frozenset({
    'app/indoor_temperature.py',
    'scripts/configure_temperature_sensor.py',
    'widgets/indoor-temperature/manifest.json',
    'widgets/indoor-temperature/widget.js',
    'widgets/indoor-temperature/style.css',
    'widgets/indoor-air-quality/manifest.json',
    'widgets/indoor-air-quality/widget.js',
    'widgets/indoor-air-quality/style.css',
})
# These hashes were verified against the preserved, originally delivered v1
# temperature-runtime-update.zip (SHA256 below), not mutable working-tree files.
# They deliberately identify exact bytes; local edits/line-ending changes stop.
V1_ARCHIVE_SHA256 = '0d8c3d6078d3cd981321e6c869d1568390557d840ce87298bce7951ea41f3d3e'
KNOWN_V1_SHA256 = {
    'app/indoor_temperature.py': '932d65d432dc1ff8d59a2209da81ba90ee7070be389bb6aefedec3dda708c224',
    'scripts/configure_temperature_sensor.py': 'a2ff7b0b97d76110f59a344d0051e1a3552f3cc738340db5ea11f94e7e67525e',
    'widgets/indoor-temperature/manifest.json': '574d3d2b1a0f5120a514d9174f13fa3c4103429e47b0cda2508ec9e77c130f3f',
    'widgets/indoor-temperature/style.css': '04f4a38145e41de446c2e1d1cc1b0beda22ab8af847d3898e753ba3a55e97798',
    'widgets/indoor-temperature/widget.js': '645049fcba644276a0649a994fd9e85f8c1b86bae7990331e16e3ab1469dff13',
}
# The v2 archive is immutable evidence from the previous delivered update.
# Hashes are never learned from the target installation or mutable source tree.
V2_ARCHIVE_SHA256 = '71d4165657041d57627c52259d535481c21b93b2e3eda9f30349ac6179265f82'
KNOWN_V2_SHA256 = {
    'app/indoor_temperature.py': 'a86caf48866a0ca6f4de4333d05898fa1f567abb059a1341df6d525bc06fee7f',
    'scripts/configure_temperature_sensor.py': 'd32bf29db584f1eaf7584946c6eb9bd3821f0c61752f16ece20cd23fcf606faf',
    'widgets/indoor-temperature/manifest.json': 'e4766207b21bbf082c44c238f2623922db22feea4372cbb424324138bbc9e548',
    'widgets/indoor-temperature/style.css': '49f18bd8f57eeb01dbcdfbd797e7407f3d071847d86671d7ec69344e1307dc09',
    'widgets/indoor-temperature/widget.js': 'b94c94badf51afdd0cec5a6024727c08ac5b888a77b48a87d48a52bbf37ead64',
}
MAIN = 'app/main.py'
ALLOWED = PAYLOAD_PATHS | {MAIN}
MAX_SOURCE_BYTES = 2 * 1024 * 1024
IMPORT_ANCHOR = 'from . import llm_display\n'
IMPORT_INSERT = 'from .indoor_temperature import register_routes as register_temperature_routes\n'
ROUTE_ANCHOR = ' register_timer_routes(app,timers,viewer)\n'
ROUTE_INSERT = ' register_temperature_routes(app,data,store,admin,viewer)\n'


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def checked_root(value, *, must_exist=True):
    root = Path(value).expanduser().absolute()
    if '..' in root.parts:
        raise ValueError('Use an absolute path without ..')
    for item in (root, *root.parents):
        if item.is_symlink():
            raise ValueError('Symlink path refused: ' + str(item))
        if item.exists() and not item.is_dir():
            raise ValueError('Non-directory path: ' + str(item))
    if must_exist and not root.is_dir():
        raise ValueError('Directory does not exist: ' + str(root))
    return root


def safe_path(root, relative):
    p = PurePosixPath(relative)
    if relative not in ALLOWED or p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('Non-allowlisted source path: ' + str(relative))
    checked_root(root)
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink source path refused: ' + relative)
        if current != root.joinpath(*p.parts) and current.exists() and not current.is_dir():
            raise ValueError('Non-directory source parent: ' + relative)
    if current.exists() and not current.is_file():
        raise ValueError('Non-file source target: ' + relative)
    return current


def read_source(path):
    if path.is_symlink():
        raise ValueError('Symlink file refused: ' + str(path))
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SOURCE_BYTES:
            raise ValueError('Unexpected source file type, links or size: ' + str(path))
        blob = stream.read(MAX_SOURCE_BYTES + 1)
        if len(blob) > MAX_SOURCE_BYTES:
            raise ValueError('Source file is too large: ' + str(path))
    return blob


def _ast_equal(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _call(node, name, args):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == name
            and not node.value.keywords
            and [x.id if isinstance(x, ast.Name) else None for x in node.value.args] == args)


def patch_main(blob):
    """Validate scope/auth anchors, then insert exactly two lines preserving bytes."""
    try:
        text = blob.decode('utf-8-sig')
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError('main.py is not valid UTF-8 Python') from exc
    newline = '\r\n' if '\r\n' in text else '\n'
    if '\r' in text.replace('\r\n', '') or (newline == '\r\n' and '\n' in text.replace('\r\n', '')):
        raise ValueError('Mixed or unsupported main.py line endings')
    normalized = text.replace('\r\n', '\n')
    for anchor in (IMPORT_ANCHOR, ROUTE_ANCHOR):
        if normalized.count(anchor) != 1:
            raise ValueError('Expected one exact integration anchor; inspect main.py locally')
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'create_app']
    if len(functions) != 1:
        raise ValueError('Unknown create_app structure')
    factory = functions[0]
    expected_args = ast.parse('def create_app(data_dir=None,weather_enabled=True,*,speech_config=None,speech_runner=None,llm_backend=None): pass').body[0].args
    if not _ast_equal(factory.args, expected_args) or factory.decorator_list:
        raise ValueError('Unknown create_app signature; no source changes made')
    import_nodes = [n for n in tree.body if _ast_equal(n, ast.parse(IMPORT_ANCHOR).body[0])]
    timer_import = ast.parse('from .timers import TimerService, register_routes as register_timer_routes').body[0]
    if len(import_nodes) != 1 or sum(_ast_equal(n, timer_import) for n in tree.body) != 1:
        raise ValueError('Unknown module import boundary')
    lines = normalized.splitlines(keepends=True)
    if lines[import_nodes[0].lineno - 1] != IMPORT_ANCHOR:
        raise ValueError('Import text anchor does not match its Python statement')
    calls = [i for i, n in enumerate(factory.body) if _call(n, 'register_timer_routes', ['app', 'timers', 'viewer'])]
    if len(calls) != 1:
        raise ValueError('Timer route anchor is not in the reviewed factory scope')
    index = calls[0]
    if lines[factory.body[index].lineno - 1] != ROUTE_ANCHOR:
        raise ValueError('Route text anchor does not match its Python statement')
    if not index or not _call(factory.body[index - 1], 'register_life_routes', ['app', 'life', 'admin', 'viewer', 'changed']):
        raise ValueError('Unknown route registration surroundings')
    for name, source in (
        ('admin', "def admin(request:Request):return authenticate(request,'admin')"),
        ('viewer', 'def viewer(request:Request):return authenticate(request)'),
    ):
        matches = [n for n in factory.body[:index] if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(matches) != 1 or not _ast_equal(matches[0], ast.parse(source).body[0]):
            raise ValueError('Unknown ' + name + ' authentication boundary')
    bound = {n.id for item in factory.body[:index] if isinstance(item, (ast.Assign, ast.AnnAssign))
             for n in ast.walk(item) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if not {'app', 'data', 'store', 'timers'}.issubset(bound):
        raise ValueError('Required factory dependencies are not local assignments')
    imported = normalized.count(IMPORT_INSERT)
    registered = normalized.count(ROUTE_INSERT)
    if (imported, registered) not in {(0, 0), (1, 1)}:
        raise ValueError('Partial or duplicate temperature integration; inspect source and backup')
    if imported:
        if normalized.count('register_temperature_routes') != 2:
            raise ValueError('Unexpected additional temperature route binding')
        if IMPORT_ANCHOR + IMPORT_INSERT not in normalized or ROUTE_ANCHOR + ROUTE_INSERT not in normalized:
            raise ValueError('Temperature integration is not adjacent to reviewed anchors')
        item = factory.body[index + 1] if len(factory.body) > index + 1 else None
        if not _call(item, 'register_temperature_routes', ['app', 'data', 'store', 'admin', 'viewer']):
            raise ValueError('Unknown temperature registration scope')
    elif 'register_temperature_routes' in normalized or 'from .indoor_temperature' in normalized:
        raise ValueError('Unrecognized pre-existing temperature integration')
    next_index = index + 1 + imported
    if next_index >= len(factory.body):
        raise ValueError('Missing reviewed next factory statement')
    following = factory.body[next_index]
    if not isinstance(following, ast.FunctionDef) or following.name != 'new_session' or [a.arg for a in following.args.args] != ['role', 'device_id', 'days']:
        raise ValueError('Unknown statement following the route registration anchor')
    if imported:
        return blob
    result = blob
    for anchor, addition in ((IMPORT_ANCHOR, IMPORT_INSERT), (ROUTE_ANCHOR, ROUTE_INSERT)):
        old = anchor.replace('\n', newline).encode('utf-8')
        result = result.replace(old, old + addition.replace('\n', newline).encode('utf-8'), 1)
    ast.parse(result.decode('utf-8-sig'))
    return result


def load_package(package):
    package = checked_root(package)
    payload = checked_root(package / 'payload')
    manifest = json.loads(read_source(package / 'PATCH_MANIFEST.json'))
    if (manifest.get('schema_version'), manifest.get('patch_id'), manifest.get('main_patch')) != (1, PATCH_ID, MAIN_PATCH):
        raise ValueError('Wrong indoor-sensors update manifest')
    rows = manifest.get('files')
    if not isinstance(rows, list) or len(rows) != len(PAYLOAD_PATHS):
        raise ValueError('Incomplete payload manifest')
    paths = [row.get('path') for row in rows if isinstance(row, dict)]
    if len(paths) != len(rows) or len(set(paths)) != len(paths) or set(paths) != PAYLOAD_PATHS:
        raise ValueError('Duplicate, missing or non-allowlisted payload path')
    result = {}
    for row in rows:
        relative = row['path']
        if not isinstance(row.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
            raise ValueError('Invalid payload digest')
        blob = read_source(safe_path(payload, relative))
        if digest(blob) != row['sha256']:
            raise ValueError('Payload hash mismatch: ' + relative)
        if relative.endswith('.py'):
            try:
                ast.parse(blob.decode('utf-8-sig'))
            except (SyntaxError, UnicodeError) as exc:
                raise ValueError('Invalid Python payload: ' + relative) from exc
        if relative.endswith('manifest.json'):
            widget = json.loads(blob)
            expected_id = PurePosixPath(relative).parent.name
            if widget.get('id') != expected_id or widget.get('apiVersion') != 1:
                raise ValueError('Invalid widget manifest: ' + relative)
        result[relative] = blob
    return result


def metadata(path):
    info = path.stat()
    return {'mode': stat.S_IMODE(info.st_mode), 'uid': getattr(info, 'st_uid', None), 'gid': getattr(info, 'st_gid', None)}


def plan(package, target):
    target = checked_root(target)
    payload = load_package(package)
    main = safe_path(target, MAIN)
    original = read_source(main)
    payload[MAIN] = patch_main(original)
    changes = []
    for relative in sorted(payload, key=lambda x: (x == MAIN, x)):
        path = safe_path(target, relative)
        before = read_source(path) if path.exists() else None
        after = payload[relative]
        if before == after:
            continue
        prior_hashes = {KNOWN_V1_SHA256.get(relative), KNOWN_V2_SHA256.get(relative)}
        if relative != MAIN and before is not None and digest(before) not in prior_hashes:
            raise ValueError('Existing feature file differs from this payload and reviewed v1/v2; nothing written: ' + relative)
        changes.append({'path': relative, 'before': before, 'after': after,
                        **(metadata(path) if before is not None else {'mode': 0o644, 'uid': None, 'gid': None})})
    return changes


def check_stopped(stopped, port):
    if not stopped:
        raise ValueError('Stop Room Hub, then pass --server-stopped; do not stop SSH/nginx')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid Room Hub port')
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass
    except ConnectionRefusedError:
        return
    except OSError as exc:
        raise ValueError('Could not verify the Room Hub port is closed') from exc
    raise ValueError('Room Hub port is still open: ' + str(port))


def atomic_write(path, blob, *, mode=0o644, uid=None, gid=None):
    checked_root(path.parent, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Symlink replacement refused')
    fd, name = tempfile.mkstemp(prefix='.roomhub-temperature-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, 'fchown') and uid is not None and gid is not None:
                os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode) if hasattr(os, 'fchmod') else os.chmod(name, mode)
        os.replace(name, path)
        if hasattr(os, 'O_DIRECTORY'):
            with contextlib.suppress(OSError):
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def write_receipt(backup, receipt):
    atomic_write(backup / 'receipt.json', (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode(), mode=0o600)


def current_hash(target, relative):
    path = safe_path(target, relative)
    return digest(read_source(path)) if path.exists() else None


def _restore(target, backup, receipt):
    # Check the complete rollback before changing any file. A concurrent edit is
    # preserved for operator review, including when automatic recovery is needed.
    allowed_parents = {str(PurePosixPath(path).parent) for path in ALLOWED}
    for relative in receipt.get('created_dirs', []):
        if relative not in allowed_parents:
            raise ValueError('Unexpected directory in rollback receipt')
        checked_root(target / relative, must_exist=False)
    for row in receipt['files']:
        allowed = {row['after_sha256']}
        if receipt['state'] in {'installing', 'rolling_back', 'recovery_required'}:
            allowed.add(row['before_sha256'])
        if current_hash(target, row['path']) not in allowed:
            raise ValueError('File changed after planning/install; backup preserved: ' + row['path'])
        if row['before_sha256'] is not None:
            blob = read_source(safe_path(backup / 'originals', row['path']))
            if digest(blob) != row['before_sha256']:
                raise ValueError('Corrupt original backup: ' + row['path'])
    receipt['state'] = 'rolling_back'
    write_receipt(backup, receipt)
    for row in reversed(receipt['files']):
        current = current_hash(target, row['path'])
        if current == row['before_sha256']:
            continue
        if current != row['after_sha256']:
            raise ValueError('Concurrent source edit during rollback: ' + row['path'])
        path = safe_path(target, row['path'])
        if row['before_sha256'] is None:
            path.unlink()
        else:
            atomic_write(path, read_source(safe_path(backup / 'originals', row['path'])),
                         **{key: row[key] for key in ('mode', 'uid', 'gid')})
    for relative in sorted(receipt.get('created_dirs', []), key=lambda x: len(PurePosixPath(x).parts), reverse=True):
        directory = checked_root(target / relative, must_exist=False)
        with contextlib.suppress(FileNotFoundError, OSError):
            directory.rmdir()  # Remove only our empty directories; never recurse.
    receipt['state'] = 'rolled_back'
    write_receipt(backup, receipt)


def apply(package, target, *, stopped=False, port=8088):
    target = checked_root(target)
    changes = plan(package, target)
    if not changes:
        return None
    check_stopped(stopped, port)
    parent = checked_root(target.parent / 'room-hub-update-backups', must_exist=False)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=PATCH_ID + '-', dir=parent))
    (backup / 'originals').mkdir(mode=0o700)
    receipt = {'patch_id': PATCH_ID, 'target': str(target), 'state': 'installing', 'files': [], 'created_dirs': []}
    for change in changes:
        path = safe_path(target, change['path'])
        if not path.parent.exists():
            receipt['created_dirs'].append(str(path.parent.relative_to(target)))
        row = {key: change[key] for key in ('path', 'mode', 'uid', 'gid')}
        row.update(before_sha256=digest(change['before']) if change['before'] is not None else None,
                   after_sha256=digest(change['after']))
        receipt['files'].append(row)
        if change['before'] is not None:
            dest = safe_path(backup / 'originals', change['path'])
            atomic_write(dest, change['before'], mode=0o600)
            if digest(read_source(dest)) != row['before_sha256']:
                raise ValueError('Backup verification failed')
    receipt['created_dirs'] = sorted(set(receipt['created_dirs']))
    write_receipt(backup, receipt)
    print('BACKUP: ' + str(backup), flush=True)
    try:
        for change, row in zip(changes, receipt['files']):
            path = safe_path(target, change['path'])
            if current_hash(target, change['path']) != row['before_sha256']:
                raise ValueError('Source changed during installation: ' + change['path'])
            if change['before'] is not None and metadata(path) != {key: change[key] for key in ('mode', 'uid', 'gid')}:
                raise ValueError('Source metadata changed during installation: ' + change['path'])
            atomic_write(path, change['after'], **{key: change[key] for key in ('mode', 'uid', 'gid')})
        receipt['state'] = 'applied'
        write_receipt(backup, receipt)
    except BaseException as original_error:
        receipt['state'] = 'installing'
        try:
            _restore(target, backup, receipt)
        except BaseException as recovery_error:
            receipt['state'] = 'recovery_required'
            with contextlib.suppress(Exception):
                write_receipt(backup, receipt)
            raise ValueError('Installation stopped; preserve backup ' + str(backup) + '; recovery blocked: ' + str(recovery_error)) from original_error
        raise
    return backup


def rollback(target, backup, *, stopped=False, port=8088):
    target, backup = checked_root(target), checked_root(backup)
    check_stopped(stopped, port)
    receipt = json.loads(read_source(backup / 'receipt.json'))
    if receipt.get('patch_id') != PATCH_ID or receipt.get('target') != str(target):
        raise ValueError('Backup belongs to another target/update; use the matching original installer for a v1/v2 backup')
    if receipt.get('state') == 'rolled_back':
        return
    if receipt.get('state') not in {'applied', 'installing', 'rolling_back', 'recovery_required'}:
        raise ValueError('Unknown backup state')
    rows = receipt.get('files', [])
    if not rows or len(rows) > len(ALLOWED) or len({row['path'] for row in rows}) != len(rows):
        raise ValueError('Invalid rollback file list')
    for row in rows:
        safe_path(target, row['path'])
        if not re.fullmatch('[0-9a-f]{64}', row.get('after_sha256', '')):
            raise ValueError('Invalid rollback digest')
        if row.get('before_sha256') is not None and not re.fullmatch('[0-9a-f]{64}', row['before_sha256']):
            raise ValueError('Invalid original digest')
        if type(row.get('mode')) is not int or not 0 <= row['mode'] <= 0o777:
            raise ValueError('Invalid original file mode')
        for key in ('uid', 'gid'):
            if row.get(key) is not None and (type(row[key]) is not int or row[key] < 0):
                raise ValueError('Invalid original ownership')
    _restore(target, backup, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--package', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--port', type=int, default=8088)
    parser.add_argument('--server-stopped', action='store_true')
    options = parser.add_mutually_exclusive_group()
    options.add_argument('--check', action='store_true')
    options.add_argument('--apply', action='store_true')
    options.add_argument('--rollback', metavar='BACKUP_DIRECTORY')
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            rollback(args.target, args.rollback, stopped=args.server_stopped, port=args.port)
            print('ROLLED BACK: source only; sensor settings and measurements retained')
        elif args.apply:
            result = apply(args.package, args.target, stopped=args.server_stopped, port=args.port)
            print('APPLIED: ' + PATCH_ID if result else 'ALREADY APPLIED: ' + PATCH_ID)
        else:
            changes = plan(args.package, args.target)
            print('READY: ' + str(len(changes)) + ' source files; no files changed' if changes else 'ALREADY APPLIED: ' + PATCH_ID)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
