"""Source-only packaging rules. No application import and no data-directory access.

This is a conservative publishing guard, not a comprehensive secret scanner.
Keep rules synchronized with .gitignore and tests/test_repo_packaging.py.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {'.github', 'app', 'web', 'widgets', 'docs', 'deploy', 'examples', 'scripts', 'tests', 'previews', 'benchmarks'}
ROOT_FILES = {
    '.gitignore', '.gitattributes', '.editorconfig', '.dockerignore', '.env.example',
    'README.md', 'README_KO.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'AGENTS.md', 'THIRD_PARTY_NOTICES.md', 'VERSION', 'Dockerfile', 'compose.yaml',
    'START_HERE.html', 'START_WINDOWS.bat', 'start.sh', 'run.py',
    'requirements.txt', 'requirements-dev.txt', 'pytest.ini', 'LICENSE', 'LICENSE.md',
}
EXCLUDED_DIRS = {
    '.git', '.venv', 'venv', 'env', '__pycache__', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', 'benchmark-data', 'benchmark-results', 'room-hub-benchmark-data', 'room-hub-benchmark-results', 'data', 'backups', 'secrets', 'artifacts', 'dist', 'build',
    'node_modules', '.vscode', '.idea',
}
BINARY_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.ico'}
SOURCE_SUFFIXES = BINARY_SUFFIXES | {
    '.py', '.js', '.css', '.html', '.md', '.json', '.yaml', '.yml', '.txt',
    '.svg', '.webmanifest', '.sh', '.ps1', '.bat', '.ini', '.toml',
}
FORBIDDEN_SUFFIXES = {
    '.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.pfx', '.crt', '.cer',
    '.ttf', '.otf', '.woff', '.woff2', '.eot', '.zip', '.pyc', '.pyo', '.log',
    '.bak', '.tmp', '.wav', '.mp3', '.m4a', '.flac', '.ogg', '.webm', '.bin',
}
SECRET_PATTERNS = (
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{20,}'),
)


def source_path_allowed(path: str | PurePosixPath) -> bool:
    p = PurePosixPath(str(path).replace('\\', '/'))
    if p.is_absolute() or not p.parts or '..' in p.parts:
        return False
    if any(part.lower() in EXCLUDED_DIRS for part in p.parts):
        return False
    if p.parts[0] == 'benchmarks' and (p.suffix.lower() not in {'.py', '.md'} or any(x in {'suites', 'results', 'datasets'} for x in p.parts)):
        return False
    name = p.name.lower()
    if re.fullmatch(r'all_.*\.jsonl|unique_tasks_registry.*\.json', name):
        return False
    if name.startswith('.env') and p.as_posix() != '.env.example':
        return False
    if name in {'source_manifest.json', '.ds_store', 'thumbs.db', 'desktop.ini'}:
        return False
    if name.endswith('-token.txt') or '.sqlite' in name or re.search(r'\.db(?:-|$)', name):
        return False
    if p.suffix.lower() in FORBIDDEN_SUFFIXES:
        return False
    if p.parts[0] == 'docs' and (name.endswith('-results.json') or name.endswith('-results.xml') or name == 'preview-before-fix-samples.json'):
        return False
    if len(p.parts) == 1:
        return p.name in ROOT_FILES
    return p.parts[0] in SOURCE_DIRS and p.suffix.lower() in SOURCE_SUFFIXES


def public_files(root: Path = ROOT) -> Iterator[Path]:
    """Walk only known source roots; never recurse into private/runtime directories."""
    root = root.resolve()
    for base, dirs, files in os.walk(root, followlinks=False):
        relative = Path(base).relative_to(root)
        if relative == Path('.'):
            dirs[:] = sorted(d for d in dirs if d in SOURCE_DIRS and not (Path(base) / d).is_symlink())
        else:
            dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if not source_path_allowed(path.relative_to(root).as_posix()):
                continue
            if path.is_symlink():
                raise ValueError(f'Symlink is not publishable: {path.relative_to(root)}')
            yield path


def secret_findings(relative: str, content: bytes) -> list[str]:
    """Return reasons, never the matching credential values."""
    if PurePosixPath(relative).suffix.lower() in BINARY_SUFFIXES:
        return []
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return ['Source text is not UTF-8']
    issues = []
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        issues.append('Possible embedded credential/private key; review manually')
    if relative == '.env.example':
        for line in text.splitlines():
            if re.match(r'^\s*HUB_(?:ADMIN|INGEST)_TOKEN\s*=\s*\S', line):
                issues.append('Example token must remain empty')
    if text.lstrip().startswith('SQLite format 3'):
        issues.append('Database content disguised as source')
    return issues
