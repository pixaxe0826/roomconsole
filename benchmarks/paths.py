"""Filesystem boundaries for external, data-only suites (no application imports)."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat

CODE_ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$')


def no_links(path: Path) -> None:
    """Reject symlinks and Windows junctions/reparse points, including ancestors."""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlinks/reparse points are not allowed in dataset paths')


def external_directory(value: str | Path | None, *, exists: bool = True) -> Path:
    if value is None or not str(value).strip():
        raise ValueError('Supply --suite-root or ROOM_HUB_BENCHMARK_DATA; no dataset is bundled')
    path = Path(value).expanduser().absolute()
    no_links(path)
    path = path.resolve()
    if path.is_relative_to(CODE_ROOT) or CODE_ROOT.is_relative_to(path):
        raise ValueError('Dataset must be outside the Room Hub repository and its ancestors')
    if any((p / '.git').exists() for p in (path, *path.parents)):
        raise ValueError('Dataset inside a Git working tree is not allowed; use an external folder')
    if exists and not path.is_dir():
        raise ValueError('External dataset directory does not exist')
    return path


def relative_file(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ValueError('Invalid suite filename')
    if '\\' in name or ':' in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('Unsafe suite filename')
    parts = name.split('/')
    if any(p in {'', '.', '..'} or p.startswith('.') or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Unsafe suite filename')
    if any(re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p) for p in parts):
        raise ValueError('Reserved Windows filename')
    if any(any(c in p for c in '<>"|?*') for p in parts):
        raise ValueError('Invalid Windows filename')
    return PurePosixPath(name).as_posix()


def owned_child(root: Path, name: str) -> Path:
    name = relative_file(name)
    path = root / name
    no_links(path)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Missing or unsafe suite file: ' + name)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError('Suite inputs must be regular, non-hardlinked files')
    return path


def result_directory(value: str | Path, suite_path: Path | None = None) -> Path:
    path = Path(value).expanduser().absolute()
    no_links(path)
    path = path.resolve()
    if path.is_relative_to(CODE_ROOT) and not path.is_relative_to(CODE_ROOT / 'artifacts' / 'benchmarks'):
        raise ValueError('In-repository results are allowed only under artifacts/benchmarks')
    if CODE_ROOT.is_relative_to(path):
        raise ValueError('Results root cannot contain the source repository')
    if suite_path is not None and (path.is_relative_to(suite_path) or suite_path.is_relative_to(path)):
        raise ValueError('Results and dataset directories must be separate')
    if any(p.name.lower() in {'data', '.git'} for p in (path, *path.parents)):
        raise ValueError('Results cannot be written to production data or Git metadata')
    return path
