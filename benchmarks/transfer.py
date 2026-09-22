"""Explicit local packaging and staged V35 import. No network or model calls.

PowerShell owns ssh/scp; this shared stdlib-only validator owns the bytes. A
validated ZIP is never extracted with extractall and never executes dataset code.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile

from .dataset import json_read, load_directory, load_suite, referenced_files, stable
from .integrity import MAX_FILE, MAX_FILES, MAX_TOTAL, RECEIPT, describe, sha256_file, verify_inventory
from .paths import NAME, external_directory, no_links, relative_file

TOKEN = re.compile(r'^[0-9a-f]{32}$')
HASH = re.compile(r'^[0-9a-f]{64}$')


def discover_source(value: str | Path, suite: str | None = None) -> Path:
    source = external_directory(value)
    if suite is not None and not NAME.fullmatch(suite):
        raise ValueError('Invalid suite name')
    if (source / 'manifest.json').is_file():
        return source
    candidates = [p for p in source.iterdir() if p.is_dir() and (p / 'manifest.json').is_file()]
    if suite is not None and len(candidates) != 1:
        candidates = [p for p in candidates if p.name == suite]
    if len(candidates) != 1:
        raise ValueError('Source must contain one suite; select its manifest directory explicitly')
    return external_directory(candidates[0])


def prepare(source: str | Path, output: str | Path, suite_name: str | None = None) -> dict:
    source = discover_source(source, suite_name)
    suite = load_directory(source, name=suite_name)
    out = external_directory(output, exists=False)
    if out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError('Package output must be outside the source dataset')
    # A dedicated, nonexistent output avoids overwriting any local data.
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        names = referenced_files(suite.manifest, source)
        for optional in ('README.md', 'SOURCES.md', 'provenance.json'):
            if (source / optional).is_file() and optional not in names:
                names.append(optional)
        inventory = describe(source, names)
        descriptor = {'format': 'room-hub-transfer-v1', 'suite': suite.name,
                      'suite_version': suite.manifest['version'], 'case_count': len(suite.cases),
                      'dataset_hash': suite.digest, 'files': inventory}
        archive = out / 'bundle.zip'
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(RECEIPT, stable(descriptor))
            for item in inventory:
                # Read once, check, then write those very bytes (source cannot race the hash).
                data = (source / item['path']).read_bytes()
                import hashlib
                if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('Source changed during packaging; retry from a stable dataset')
                z.writestr('files/' + item['path'], data)
        # Revalidate the packaged snapshot, not only the live source directory.
        with tempfile.TemporaryDirectory(prefix='room-hub-package-check-') as tmp:
            unpack(archive, Path(tmp), sha256_file(archive))
        metadata = {k: descriptor[k] for k in ('suite', 'suite_version', 'case_count', 'dataset_hash')}
        metadata.update(archive=str(archive), archive_sha256=sha256_file(archive),
                        file_count=len(inventory), warnings=suite.warnings)
        (out / 'prepared.json').write_text(stable(metadata) + '\n', encoding='utf-8')
        return metadata
    except BaseException:
        shutil.rmtree(out)
        raise


def unpack(archive: Path, destination: Path, expected_sha256: str) -> dict:
    if not HASH.fullmatch(expected_sha256):
        raise ValueError('Invalid expected archive SHA256')
    no_links(archive)
    if not archive.is_file() or archive.stat().st_size > MAX_TOTAL + 4 * 1024 * 1024:
        raise ValueError('Missing or oversized transfer archive')
    if sha256_file(archive) != expected_sha256:
        raise ValueError('Archive SHA256 mismatch; refusing partial/corrupt transfer')
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > MAX_FILES + 1 or sum(e.file_size for e in entries) > MAX_TOTAL + MAX_FILE:
            raise ValueError('Archive exceeds extraction limits')
        names = [e.filename for e in entries]
        if len(set(n.casefold() for n in names)) != len(names):
            raise ValueError('Duplicate/case-colliding ZIP entries')
        for e in entries:
            relative_file(e.filename)
            kind = stat.S_IFMT(e.external_attr >> 16)
            if e.is_dir() or kind not in (0, stat.S_IFREG) or e.flag_bits & 1 or e.file_size > MAX_FILE:
                raise ValueError('Only unencrypted regular files are allowed in a package')
        if RECEIPT not in names:
            raise ValueError('Missing transfer descriptor')
        if z.getinfo(RECEIPT).file_size > 1024 * 1024:
            raise ValueError('Transfer descriptor too large')
        from .dataset import reject_duplicate_keys
        receipt = json.loads(z.read(RECEIPT), object_pairs_hook=reject_duplicate_keys)
        if not isinstance(receipt, dict) or not isinstance(receipt.get('files'), list):
            raise ValueError('Invalid transfer descriptor')
        files = receipt['files']
        if not files or len(files) > MAX_FILES:
            raise ValueError('Invalid inventory length')
        wanted = {RECEIPT}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ValueError('Invalid inventory entry')
            wanted.add('files/' + relative_file(item['path']))
        if set(names) != wanted:
            raise ValueError('Unlisted or missing archive entries')
        for item in files:
            path = destination / relative_file(item['path'])
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open('xb') as out:
                out.write(z.read('files/' + item['path']))
        (destination / RECEIPT).write_text(stable(receipt), encoding='utf-8')
    verify_inventory(destination, receipt)
    suite = load_directory(destination, name=receipt.get('suite'))
    if receipt.get('dataset_hash') != suite.digest or receipt.get('case_count') != len(suite.cases):
        raise ValueError('Packaged dataset digest/count mismatch')
    return receipt


def receive_init(root: Path, token: str) -> Path:
    root = external_directory(root)
    if not TOKEN.fullmatch(token):
        raise ValueError('Invalid transfer token')
    incoming = root / '.incoming'
    no_links(incoming)
    incoming.mkdir(mode=0o700, exist_ok=True)
    path = incoming / token
    path.mkdir(mode=0o700, exist_ok=False)
    return path


@contextmanager
def import_lock(root: Path):
    lock = root / '.import-lock'
    no_links(lock)
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError('Another import is active (or left a lock); inspect before retrying') from exc
    try:
        yield
    finally:
        lock.rmdir()


def install(root: Path, token: str, archive_sha256: str, *, force: bool = False) -> dict:
    root = external_directory(root)
    if not TOKEN.fullmatch(token):
        raise ValueError('Invalid transfer token')
    incoming = root / '.incoming' / token
    no_links(incoming)
    if not incoming.is_dir():
        raise ValueError('Unknown staging token; prepare the remote transfer first')
    if set(p.name for p in incoming.iterdir()) != {'bundle.zip'}:
        raise ValueError('Staging directory must contain exactly bundle.zip')
    with import_lock(root):
        # Validation failure leaves only quarantined input, never a visible suite.
        with tempfile.TemporaryDirectory(prefix='.validated-', dir=root) as tmp:
            stage = Path(tmp)
            receipt = unpack(incoming / 'bundle.zip', stage, archive_sha256)
            suite_name = receipt['suite']
            final = root / suite_name
            no_links(final)
            backup = None
            if final.exists():
                if not force:
                    raise FileExistsError('Suite already exists; explicit --force is required')
                if not final.is_dir():
                    raise ValueError('Existing suite is not a regular directory')
                # Verify old ownership before moving it; do not rename unrelated user files.
                old_receipt = json_read(final / RECEIPT) if (final / RECEIPT).is_file() else None
                if not old_receipt or old_receipt.get('suite') != suite_name:
                    raise ValueError('Existing suite was not installed by this helper; archive it manually')
                verify_inventory(final, old_receipt)
                backups = root / '.backups'
                no_links(backups)
                backups.mkdir(mode=0o700, exist_ok=True)
                backup = backups / (suite_name + '-' + uuid.uuid4().hex)
                final.rename(backup)
            try:
                # Same-filesystem directory rename: no partially copied suite is promoted.
                stage.rename(final)
            except BaseException:
                if backup is not None and not final.exists():
                    backup.rename(final)
                raise
            result = {'suite': suite_name, 'dataset_hash': receipt['dataset_hash'],
                      'case_count': receipt['case_count'], 'archive_sha256': archive_sha256,
                      'installed_at': datetime.now(timezone.utc).isoformat(),
                      'path': str(final), 'backup': str(backup) if backup else None}
        # Only this completed transfer's staging files are removed.
        shutil.rmtree(incoming)
        return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--source', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    prep.add_argument('--suite')
    init = sub.add_parser('receive-init')
    init.add_argument('--root', type=Path, required=True)
    init.add_argument('--token', required=True)
    imp = sub.add_parser('install')
    imp.add_argument('--root', type=Path, required=True)
    imp.add_argument('--token', required=True)
    imp.add_argument('--sha256', required=True)
    imp.add_argument('--force', action='store_true')
    verify = sub.add_parser('verify')
    verify.add_argument('--root', type=Path, required=True)
    verify.add_argument('--suite', required=True)
    args = p.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.source, args.output, args.suite)
        elif args.command == 'receive-init':
            result = {'staging': str(receive_init(args.root, args.token))}
        elif args.command == 'install':
            result = install(args.root, args.token, args.sha256, force=args.force)
        else:
            s = load_suite(args.suite, args.root)
            if not (s.path / RECEIPT).is_file():
                raise ValueError('No transfer receipt: use validate for manually managed suites')
            result = {'valid': True, 'suite': s.name, 'dataset_hash': s.digest, 'case_count': len(s.cases)}
        print(stable(result))
        return 0
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(f'Benchmark transfer error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
