"""Create a source-only ZIP with hashes. No credentials, DBs, histories or logs.

This command does not run tests, claim test success, upload or publish anything.
Use check_repo.py and the documented test commands before building a release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

try:
    from .check_repo import check_repository
    from .repo_policy import ROOT, public_files, secret_findings
except ImportError:
    from check_repo import check_repository
    from repo_policy import ROOT, public_files, secret_findings


def build_archive(root: Path, output: Path, *, force: bool = False, validate: bool = True) -> dict:
    root, output = root.resolve(), output.resolve()
    if validate:
        result = check_repository(root)
        if not result['ok']:
            raise ValueError('Repository check failed:\n' + '\n'.join(result['errors']))
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid VERSION')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'room_hub_github_v{version}.zip'
    checksum_path = archive.with_suffix('.zip.sha256')
    if not force and (archive.exists() or checksum_path.exists()):
        raise FileExistsError(f'{archive.name} already exists; review before using --force')
    entries: list[tuple[str, bytes, int]] = []
    for path in sorted(public_files(root)):
        rel = path.relative_to(root).as_posix()
        content = path.read_bytes()
        issues = secret_findings(rel, content)
        if issues:
            raise ValueError(rel + ': ' + '; '.join(issues))
        entries.append((rel, content, 0o755 if rel.endswith('.sh') else 0o644))
    manifest = {
        'project': 'Room Hub', 'version': version, 'package_type': 'github-source',
        'runtime_data_included': False,
        'note': 'Hashes cover all source entries, excluding SOURCE_MANIFEST.json itself. Not a test result or signature.',
        'files': [{'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()} for name, content, _ in entries],
    }
    entries.append(('SOURCE_MANIFEST.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'), 0o644))
    temporary = archive.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
            for name, content, mode in entries:
                # Fixed metadata makes identical source trees produce identical ZIP bytes.
                info = zipfile.ZipInfo('room-hub/' + name, date_time=(2026, 9, 17, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zipped.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(temporary) as zipped:
            bad = zipped.testzip()
            if bad:
                raise ValueError(f'ZIP integrity failure: {bad}')
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_path.write_text(f'{sha}  {archive.name}\n', encoding='utf-8', newline='\n')
    return {'archive': str(archive), 'sha256_file': str(checksum_path), 'sha256': sha, 'bytes': archive.stat().st_size, 'source_files': len(entries) - 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    parser.add_argument('--force', action='store_true', help='Replace an existing archive and checksum')
    args = parser.parse_args()
    try:
        result = build_archive(ROOT, args.output_dir, force=args.force)
    except (ValueError, OSError) as exc:
        print(f'Package aborted: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
