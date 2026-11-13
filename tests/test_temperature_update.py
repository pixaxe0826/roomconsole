"""Synthetic source/package tests; never connect to Room Hub or read production data."""
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import zipfile

import pytest

from scripts import apply_temperature_update as updater
from scripts import build_temperature_package as builder


BASE_MAIN = b"""from . import llm_display
from .timers import TimerService, register_routes as register_timer_routes
def create_app(data_dir=None,weather_enabled=True,*,speech_config=None,speech_runner=None,llm_backend=None):
 data=data_dir
 store=object()
 app=object()
 timers=object()
 # A local remote-Whisper wrapper setting must survive byte-for-byte.
 local_wrapper='keep this local change'
 def admin(request:Request):return authenticate(request,'admin')
 def viewer(request:Request):return authenticate(request)
 register_life_routes(app,life,admin,viewer,changed)
 register_timer_routes(app,timers,viewer)
 def new_session(role,device_id,days):
  return None
 return app
"""
# M3.4-0 adds runtime setup but preserves the same reviewed integration boundary.
PR29_SHAPE = BASE_MAIN.replace(
    b'from . import llm_display\n',
    b'from . import llm_display\nfrom . import runtime_diagnostics\n',
).replace(b' timers=object()\n', b' timers=object()\n runtime=object()\n')


def make_package(root):
    package = root / 'package'
    payload = package / 'payload'
    files = []
    for relative in sorted(updater.PAYLOAD_PATHS):
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith('manifest.json'):
            blob = json.dumps({'id': path.parent.name, 'apiVersion': 1}).encode()
        elif relative.endswith('.py'):
            blob = b'# Synthetic source only.\nVALUE = 1\n'
        else:
            blob = b'/* Synthetic widget source. */\n'
        path.write_bytes(blob)
        files.append({'path': relative, 'sha256': updater.digest(blob)})
    manifest = {'schema_version': 1, 'patch_id': updater.PATCH_ID,
                'main_patch': updater.MAIN_PATCH, 'files': files}
    (package / 'PATCH_MANIFEST.json').write_text(json.dumps(manifest))
    return package


def make_target(root, main=BASE_MAIN):
    target = root / 'installation'
    (target / 'app').mkdir(parents=True)
    (target / 'widgets').mkdir()
    (target / 'scripts').mkdir()
    (target / 'app' / 'main.py').write_bytes(main)
    (target / 'app' / 'store.py').write_bytes(b'# Synthetic local FD hotfix, do not read or modify.\n')
    return target


def make_synthetic_prior(target, monkeypatch, version='v1'):
    """Small generated source substitutes exercise the same pinned-hash gate.

    Released v1/v2 bytes are verified separately from the preserved source ZIPs;
    CI needs no external release archive or duplicated historical source tree.
    """
    originals = {}
    known_name = 'KNOWN_' + version.upper() + '_SHA256'
    for relative in sorted(getattr(updater, known_name)):
        blob = ('# Synthetic ' + version + ' source: ' + relative + '\n').encode()
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        originals[relative] = blob
    monkeypatch.setattr(updater, known_name, {path: updater.digest(blob) for path, blob in originals.items()})
    (target / 'app/main.py').write_bytes(updater.patch_main((target / 'app/main.py').read_bytes()))
    return originals


@pytest.fixture
def stopped(monkeypatch):
    def refused(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(updater.socket, 'create_connection', refused)


@pytest.mark.parametrize('baseline', [BASE_MAIN, PR29_SHAPE])
@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_exact_insertions_preserve_local_main_and_pr29(baseline, newline):
    original = baseline.replace(b'\n', newline)
    result = updater.patch_main(original)
    expected = original
    for anchor, addition in ((updater.IMPORT_ANCHOR, updater.IMPORT_INSERT),
                             (updater.ROUTE_ANCHOR, updater.ROUTE_INSERT)):
        old = anchor.encode().replace(b'\n', newline)
        expected = expected.replace(old, old + addition.encode().replace(b'\n', newline))
    assert result == expected
    assert updater.patch_main(result) == result


def test_utf8_bom_is_preserved():
    original = b'\xef\xbb\xbf' + BASE_MAIN
    assert updater.patch_main(original).startswith(b'\xef\xbb\xbf')


@pytest.mark.parametrize('mutate', [
    lambda b: b.replace(b'weather_enabled=True', b'weather_enabled=False'),
    lambda b: b.replace(b'return authenticate(request)', b'return authenticate(request,"admin")'),
    lambda b: b.replace(b' register_life_routes(app,life,admin,viewer,changed)\n', b''),
    lambda b: b.replace(b' def new_session(', b' x=1\n def new_session('),
    lambda b: b.replace(b'from . import llm_display\n', b'from . import llm_display\n' * 2),
    lambda b: b.replace(b' data=data_dir\n', b''),
    lambda b: b.replace(b' register_timer_routes(app,timers,viewer)', b' if True:\n  register_timer_routes(app,timers,viewer)'),
    lambda b: b + updater.IMPORT_INSERT.encode(),
    lambda b: b.replace(b'\n', b'\r\n', 1),
])
def test_unknown_integration_structure_stops(mutate):
    with pytest.raises(ValueError):
        updater.patch_main(mutate(BASE_MAIN))


def test_text_anchor_inside_string_is_not_used():
    fake = BASE_MAIN.replace(b'from . import llm_display\n', b'from . import (llm_display)\n')
    fake += b'EXAMPLE="""\nfrom . import llm_display\n"""\n'
    with pytest.raises(ValueError, match='text anchor'):
        updater.patch_main(fake)


def test_check_is_read_only_and_does_not_read_store_or_runtime(tmp_path, monkeypatch):
    package, target = make_package(tmp_path), make_target(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    reader = updater.read_source
    def restricted(path):
        assert Path(path).name != 'store.py'
        assert 'data' not in Path(path).parts
        return reader(path)
    monkeypatch.setattr(updater, 'read_source', restricted)
    assert len(updater.plan(package, target)) == 9
    assert updater.main(['--package', str(package), '--target', str(target)]) == 0
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before == after


def test_apply_rollback_idempotency_and_permissions(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path, PR29_SHAPE)
    main = target / 'app/main.py'
    main.chmod(0o640)
    old_stat = main.stat()
    backup = updater.apply(package, target, stopped=True)
    assert main.read_bytes() == updater.patch_main(PR29_SHAPE)
    if os.name != 'nt':
        assert stat.S_IMODE(main.stat().st_mode) == 0o640
    assert main.stat().st_uid == old_stat.st_uid
    assert main.stat().st_gid == old_stat.st_gid
    assert (target / 'app/store.py').read_bytes().startswith(b'# Synthetic local FD hotfix')
    if os.name != 'nt':
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        assert stat.S_IMODE((backup / 'receipt.json').stat().st_mode) == 0o600
    assert updater.apply(package, target, stopped=True) is None
    assert not updater.plan(package, target)
    updater.rollback(target, backup, stopped=True)
    assert main.read_bytes() == PR29_SHAPE
    for relative in updater.PAYLOAD_PATHS:
        assert not (target / relative).exists()
    assert not (target / 'widgets/indoor-temperature').exists()
    assert not (target / 'widgets/indoor-air-quality').exists()
    updater.rollback(target, backup, stopped=True)


def test_preexisting_identical_new_file_is_not_deleted_by_rollback(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path)
    relative = 'app/indoor_temperature.py'
    original = (package / 'payload' / relative).read_bytes()
    (target / relative).write_bytes(original)
    backup = updater.apply(package, target, stopped=True)
    updater.rollback(target, backup, stopped=True)
    assert (target / relative).read_bytes() == original


@pytest.mark.parametrize('baseline', [BASE_MAIN, PR29_SHAPE])
@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_prior_upgrade_and_rollback_restore_exact_prior_source(tmp_path, stopped, monkeypatch, baseline, version):
    package, target = make_package(tmp_path), make_target(tmp_path, baseline)
    originals = make_synthetic_prior(target, monkeypatch, version)
    original_main = (target / 'app/main.py').read_bytes()
    old_modes = {}
    for index, relative in enumerate(sorted(originals)):
        path = target / relative
        path.chmod(0o640 if index == 0 else 0o644)
        old_modes[relative] = updater.metadata(path)
    changes = updater.plan(package, target)
    assert {row['path'] for row in changes} == updater.PAYLOAD_PATHS
    backup = updater.apply(package, target, stopped=True)
    receipt = json.loads((backup / 'receipt.json').read_text())
    assert receipt['patch_id'] == 'room-hub-indoor-sensors-v3'
    assert {row['path'] for row in receipt['files'] if row['before_sha256'] is not None} == set(originals)
    assert (target / 'app/main.py').read_bytes() == original_main
    for relative in originals:
        assert (target / relative).read_bytes() == (package / 'payload' / relative).read_bytes()
        assert updater.metadata(target / relative) == old_modes[relative]
    for relative in updater.PAYLOAD_PATHS - originals.keys():
        assert (target / relative).read_bytes() == (package / 'payload' / relative).read_bytes()
    assert updater.apply(package, target, stopped=True) is None
    updater.rollback(target, backup, stopped=True)
    assert (target / 'app/main.py').read_bytes() == original_main
    for relative, original in originals.items():
        assert (target / relative).read_bytes() == original
        assert updater.metadata(target / relative) == old_modes[relative]
    assert (target / 'widgets/indoor-temperature').is_dir()
    assert not (target / 'widgets/indoor-air-quality').exists()


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_modified_prior_file_stops_upgrade_before_any_change(tmp_path, stopped, monkeypatch, version):
    package, target = make_package(tmp_path), make_target(tmp_path)
    originals = make_synthetic_prior(target, monkeypatch, version)
    relative = 'widgets/indoor-temperature/widget.js'
    (target / relative).write_bytes(originals[relative] + b'// Local change\n')
    before = {path: (target / path).read_bytes() if (target / path).exists() else None for path in updater.ALLOWED}
    with pytest.raises(ValueError, match='reviewed v1/v2'):
        updater.apply(package, target, stopped=True)
    assert {path: (target / path).read_bytes() if (target / path).exists() else None for path in updater.ALLOWED} == before
    assert not (target.parent / 'room-hub-update-backups').exists()


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_injected_upgrade_failure_restores_all_prior_files(tmp_path, stopped, monkeypatch, version):
    package, target = make_package(tmp_path), make_target(tmp_path)
    originals = make_synthetic_prior(target, monkeypatch, version)
    writer = updater.atomic_write
    failed = False
    def fail_one(path, blob, **kwargs):
        nonlocal failed
        if path == target / 'widgets/indoor-temperature/style.css' and not failed:
            failed = True
            raise OSError('Synthetic upgrade disk failure')
        return writer(path, blob, **kwargs)
    monkeypatch.setattr(updater, 'atomic_write', fail_one)
    with pytest.raises(OSError, match='Synthetic upgrade disk failure'):
        updater.apply(package, target, stopped=True)
    for relative, original in originals.items():
        assert (target / relative).read_bytes() == original
    assert not (target / 'widgets/indoor-air-quality').exists()
    assert (target / 'app/main.py').read_bytes() == updater.patch_main(BASE_MAIN)


def test_corrupt_v3_payload_blocks_valid_prior_upgrade(tmp_path, stopped, monkeypatch):
    package, target = make_package(tmp_path), make_target(tmp_path)
    originals = make_synthetic_prior(target, monkeypatch)
    (package / 'payload/app/indoor_temperature.py').write_bytes(b'CORRUPTED = True\n')
    with pytest.raises(ValueError, match='hash mismatch'):
        updater.apply(package, target, stopped=True)
    for relative, original in originals.items():
        assert (target / relative).read_bytes() == original


@pytest.mark.parametrize('patch_id', ['room-hub-indoor-temperature-v1', 'room-hub-indoor-temperature-v2-pull'])
def test_prior_receipt_requires_original_installer(tmp_path, stopped, patch_id):
    target = make_target(tmp_path)
    backup = tmp_path / 'prior-backup'
    backup.mkdir()
    (backup / 'receipt.json').write_text(json.dumps({'patch_id': patch_id, 'target': str(target), 'state': 'applied'}))
    with pytest.raises(ValueError, match='matching original installer'):
        updater.rollback(target, backup, stopped=True)


def test_existing_different_new_file_blocks_every_change(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path)
    (target / 'app/indoor_temperature.py').write_text('CUSTOM = True\n')
    with pytest.raises(ValueError, match='differs'):
        updater.apply(package, target, stopped=True)
    assert (target / 'app/main.py').read_bytes() == BASE_MAIN
    assert not (target.parent / 'room-hub-update-backups').exists()


def test_apply_requires_stopped_and_closed_port(tmp_path, stopped, monkeypatch):
    package, target = make_package(tmp_path), make_target(tmp_path)
    with pytest.raises(ValueError, match='server-stopped'):
        updater.apply(package, target)
    monkeypatch.setattr(updater.socket, 'create_connection', lambda *a, **k: contextlib.nullcontext())
    with pytest.raises(ValueError, match='still open'):
        updater.apply(package, target, stopped=True)
    def timeout(*args, **kwargs):
        raise socket.timeout()
    monkeypatch.setattr(updater.socket, 'create_connection', timeout)
    with pytest.raises(ValueError, match='Could not verify'):
        updater.apply(package, target, stopped=True)
    assert (target / 'app/main.py').read_bytes() == BASE_MAIN


@pytest.mark.parametrize('location', ['target_file', 'target_parent', 'package_payload', 'manifest'])
@pytest.mark.skipif(os.name == 'nt', reason='Windows symlink creation requires separate OS privileges')
def test_symlinks_are_refused(tmp_path, location):
    package, target = make_package(tmp_path), make_target(tmp_path)
    if location == 'target_file':
        original = target / 'app/main.py'
        moved = target / 'saved-main.py'
        original.rename(moved)
        original.symlink_to(moved)
    elif location == 'target_parent':
        (target / 'widgets').rmdir()
        (tmp_path / 'external').mkdir()
        (target / 'widgets').symlink_to(tmp_path / 'external', target_is_directory=True)
    elif location == 'package_payload':
        (package / 'payload').rename(package / 'elsewhere')
        (package / 'payload').symlink_to(package / 'elsewhere', target_is_directory=True)
    else:
        (package / 'PATCH_MANIFEST.json').rename(package / 'manifest-real.json')
        (package / 'PATCH_MANIFEST.json').symlink_to(package / 'manifest-real.json')
    with pytest.raises(ValueError, match='Symlink'):
        updater.plan(package, target)


def test_damaged_payload_or_out_of_scope_manifest_stops(tmp_path):
    package, target = make_package(tmp_path), make_target(tmp_path)
    (package / 'payload/app/indoor_temperature.py').write_text('CHANGED = True\n')
    with pytest.raises(ValueError, match='hash mismatch'):
        updater.plan(package, target)
    manifest = json.loads((package / 'PATCH_MANIFEST.json').read_text())
    manifest['files'][0]['path'] = 'app/store.py'
    (package / 'PATCH_MANIFEST.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='non-allowlisted'):
        updater.plan(package, target)


@pytest.mark.parametrize('widget_id', ['indoor-temperature', 'indoor-air-quality'])
def test_widget_manifest_must_match_its_installation_directory(tmp_path, widget_id):
    package, target = make_package(tmp_path), make_target(tmp_path)
    relative = 'widgets/' + widget_id + '/manifest.json'
    blob = json.dumps({'id': 'wrong-widget', 'apiVersion': 1}).encode()
    (package / 'payload' / relative).write_bytes(blob)
    manifest_path = package / 'PATCH_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    next(row for row in manifest['files'] if row['path'] == relative)['sha256'] = updater.digest(blob)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Invalid widget manifest'):
        updater.plan(package, target)
    assert (target / 'app/main.py').read_bytes() == BASE_MAIN


def test_air_widget_local_file_is_not_mistaken_for_prior_temperature_patch(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path)
    air = target / 'widgets/indoor-air-quality/widget.js'
    air.parent.mkdir()
    air.write_bytes(b'/* Existing independently customized air widget. */\n')
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='differs'):
        updater.apply(package, target, stopped=True)
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()} == before
    assert not (target.parent / 'room-hub-update-backups').exists()


def test_injected_write_failure_restores_all_source(tmp_path, stopped, monkeypatch):
    package, target = make_package(tmp_path), make_target(tmp_path)
    writer = updater.atomic_write
    failed = False
    def fail_one(path, blob, **kwargs):
        nonlocal failed
        if path == target / 'widgets/indoor-temperature/style.css' and not failed:
            failed = True
            raise OSError('Synthetic disk failure')
        return writer(path, blob, **kwargs)
    monkeypatch.setattr(updater, 'atomic_write', fail_one)
    with pytest.raises(OSError, match='Synthetic disk failure'):
        updater.apply(package, target, stopped=True)
    assert (target / 'app/main.py').read_bytes() == BASE_MAIN
    assert all(not (target / path).exists() for path in updater.PAYLOAD_PATHS)
    receipts = list((target.parent / 'room-hub-update-backups').glob('*/receipt.json'))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())['state'] == 'rolled_back'


def test_concurrent_source_edit_is_preserved_and_recovery_is_blocked(tmp_path, stopped, monkeypatch):
    package, target = make_package(tmp_path), make_target(tmp_path)
    writer = updater.atomic_write
    edited = False
    def edit_during_write(path, blob, **kwargs):
        nonlocal edited
        result = writer(path, blob, **kwargs)
        if path == target / 'app/indoor_temperature.py' and not edited:
            edited = True
            (target / 'app/main.py').write_bytes(BASE_MAIN + b'# Concurrent edit\n')
        return result
    monkeypatch.setattr(updater, 'atomic_write', edit_during_write)
    with pytest.raises(ValueError, match='recovery blocked'):
        updater.apply(package, target, stopped=True)
    assert (target / 'app/main.py').read_bytes().endswith(b'# Concurrent edit\n')
    receipt_path = next((target.parent / 'room-hub-update-backups').glob('*/receipt.json'))
    assert json.loads(receipt_path.read_text())['state'] == 'recovery_required'


def test_rollback_refuses_later_edit_without_partial_restore(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path)
    backup = updater.apply(package, target, stopped=True)
    original_main = (target / 'app/main.py').read_bytes()
    (target / 'widgets/indoor-temperature/widget.js').write_bytes(b'/* Edited after installation. */\n')
    with pytest.raises(ValueError, match='File changed'):
        updater.rollback(target, backup, stopped=True)
    assert (target / 'app/main.py').read_bytes() == original_main
    assert (target / 'app/indoor_temperature.py').exists()


def test_corrupt_original_backup_prevents_rollback(tmp_path, stopped):
    package, target = make_package(tmp_path), make_target(tmp_path)
    backup = updater.apply(package, target, stopped=True)
    (backup / 'originals/app/main.py').write_bytes(b'# Broken backup\n')
    with pytest.raises(ValueError, match='Corrupt original'):
        updater.rollback(target, backup, stopped=True)
    assert (target / 'app/main.py').read_bytes() == updater.patch_main(BASE_MAIN)


def test_reproducible_source_only_zip(tmp_path):
    package = make_package(tmp_path)
    source = package / 'payload'
    (source / 'scripts/apply_temperature_update.py').write_bytes(Path(updater.__file__).read_bytes())
    (source / 'app/store.py').write_bytes(b'# Must never be packaged.\n')
    (source / 'data').mkdir()
    (source / 'data/private.txt').write_bytes(b'Synthetic exclusion marker only.')
    first, first_sha = builder.build(source, tmp_path / 'first.zip')
    second, second_sha = builder.build(source, tmp_path / 'second.zip')
    assert first_sha == second_sha and first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        assert names == {'PATCH_MANIFEST.json', 'README.txt', 'apply_update.py'} | {'payload/' + path for path in updater.PAYLOAD_PATHS}
        assert not any('store.py' in name or name.startswith('data/') for name in names)
        extracted = tmp_path / 'extracted'
        archive.extractall(extracted)
    assert set(updater.load_package(extracted)) == updater.PAYLOAD_PATHS
    with pytest.raises(ValueError, match='already exists'):
        builder.build(source, first)
