"""Data-only transfer inventories; never interpret an uploaded path as code."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .paths import NAME, owned_child, relative_file, no_links

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 512
RECEIPT = 'TRANSFER.json'
ALLOWED_SUFFIXES = {'.json', '.jsonl', '.md', '.txt', '.sha256'}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def describe(root: Path, names: list[str]) -> list[dict]:
    if not names or len(names) > MAX_FILES or len({n.casefold() for n in names}) != len(names):
        raise ValueError('Empty, oversized or case-colliding file inventory')
    result, total = [], 0
    for name in sorted(names):
        relative_file(name)
        if name == RECEIPT or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError('Only data/documentation files may be transferred')
        path = owned_child(root, name)
        size = path.stat().st_size
        total += size
        if size > MAX_FILE or total > MAX_TOTAL:
            raise ValueError('Dataset exceeds transfer size limits')
        result.append({'path': name, 'bytes': size, 'sha256': sha256_file(path)})
    return result


def verify_inventory(root: Path, receipt: dict, *, exact: bool = True) -> None:
    if not isinstance(receipt, dict) or receipt.get('format') != 'room-hub-transfer-v1':
        raise ValueError('Invalid transfer receipt')
    if not isinstance(receipt.get('suite'), str) or not NAME.fullmatch(receipt['suite']):
        raise ValueError('Invalid receipt suite')
    files = receipt.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('Missing inventory')
    if any(not isinstance(f, dict) or set(f) != {'path', 'bytes', 'sha256'} for f in files):
        raise ValueError('Invalid file inventory')
    if any(not isinstance(f['path'], str) or type(f['bytes']) is not int
           or f['bytes'] < 0 or not isinstance(f['sha256'], str) for f in files):
        raise ValueError('Invalid inventory path/size/hash types')
    if describe(root, [f['path'] for f in files]) != files:
        raise ValueError('Dataset checksum mismatch')
    if exact:
        found = set()
        for path in root.rglob('*'):
            no_links(path)
            if path.is_file() and path != root / RECEIPT:
                found.add(path.relative_to(root).as_posix())
        if found != {f['path'] for f in files}:
            raise ValueError('Dataset contains missing or unlisted files')
