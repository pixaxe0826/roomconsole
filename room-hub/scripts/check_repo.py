"""Check source hygiene, local documentation links and version consistency.

Run before git add, and again with --staged before the first commit.
Does not connect to GitHub, open private data or execute the application.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from .repo_policy import ROOT, public_files, secret_findings, source_path_allowed
except ImportError:
    from repo_policy import ROOT, public_files, secret_findings, source_path_allowed

REQUIRED = (
    'README.md', 'VERSION', '.gitignore', '.env.example', 'requirements.txt',
    'run.py', 'app/main.py', 'web/client.html', 'web/manager.html',
    'web/client.js', 'web/manager.js', 'web/manager.css', 'Dockerfile', 'compose.yaml',
    'previews/client_preview.html', 'previews/manager_preview.html',
    'docs/SETUP.md', 'docs/GITHUB_PUBLISH.md', 'docs/TEST_REPORT.md',
    '.github/workflows/ci.yml', '.github/workflows/source-archive.yml',
)


def markdown_links(text: str) -> list[str]:
    # Ignore fenced examples. Validate paths, not GitHub-generated heading anchors.
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    return re.findall(r'\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text)


def check_repository(root: Path = ROOT, *, staged: bool = False) -> dict:
    root = root.resolve()
    errors: list[str] = []
    link_count = 0
    for name in REQUIRED:
        if not (root / name).is_file():
            errors.append(f'Missing file: {name}')
    try:
        paths = list(public_files(root))
    except ValueError as exc:
        return {'ok': False, 'errors': [str(exc)], 'files_checked': 0, 'links_checked': 0}
    version = (root / 'VERSION').read_text(encoding='utf-8').strip() if (root / 'VERSION').exists() else ''
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        errors.append('VERSION must contain x.y.z')
    for path in paths:
        rel = path.relative_to(root).as_posix()
        content = path.read_bytes()
        errors.extend(f'{rel}: {reason}' for reason in secret_findings(rel, content))
        if path.suffix == '.py':
            try:
                ast.parse(content.decode('utf-8'), filename=rel)
            except (SyntaxError, UnicodeError) as exc:
                errors.append(f'{rel}: invalid Python source ({exc})')
        if path.suffix in {'.json', '.webmanifest'}:
            try:
                json.loads(content)
            except (ValueError, UnicodeError) as exc:
                errors.append(f'{rel}: invalid JSON ({exc})')
        if path.suffix == '.md':
            for link in markdown_links(content.decode('utf-8')):
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (root if parsed.path.startswith('/') else path.parent) / unquote(parsed.path.lstrip('/'))
                link_count += 1
                if not target.exists():
                    errors.append(f'{rel}: missing local link {link}')
                elif not target.resolve().is_relative_to(root):
                    errors.append(f'{rel}: link escapes repository {link}')
    # Values must agree without importing the server (which creates a data directory).
    for name in ('app/main.py', 'run.py', 'compose.yaml', 'deploy/truenas.example.yaml'):
        if (root / name).exists() and version and version not in (root / name).read_text(encoding='utf-8'):
            errors.append(f'{name}: VERSION value is missing')
    if staged:
        try:
            proc = subprocess.run(
                ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'],
                cwd=root, check=True, capture_output=True,
            )
            for raw in proc.stdout.split(b'\0'):
                if not raw:
                    continue
                rel = raw.decode('utf-8')
                if not source_path_allowed(rel):
                    errors.append(f'Staged file is not a publishable source path: {rel}')
                    continue
                blob = subprocess.run(['git', 'show', f':{rel}'], cwd=root, check=True, capture_output=True).stdout
                errors.extend(f'Staged {rel}: {reason}' for reason in secret_findings(rel, blob))
        except (subprocess.CalledProcessError, FileNotFoundError, UnicodeError):
            errors.append('--staged requires Git and an initialized repository')
    return {'ok': not errors, 'version': version, 'files_checked': len(paths), 'links_checked': link_count, 'errors': errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='Inspect the actual Git index, not only the working files')
    parser.add_argument('--json', type=Path, help='Optional result path, e.g. artifacts/test-results/repository.json')
    args = parser.parse_args()
    result = check_repository(staged=args.staged)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
