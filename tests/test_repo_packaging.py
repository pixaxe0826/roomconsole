"""Repository hygiene and deterministic source archives; never use user data."""
from __future__ import annotations
import hashlib
import json
import subprocess
import shutil
import zipfile
from pathlib import Path

import pytest
from scripts.repo_policy import source_path_allowed, public_files, secret_findings
from scripts.package_release import build_archive
from scripts.check_repo import markdown_links, check_repository


@pytest.mark.parametrize('name', [
    'data/admin-token.txt', 'data/room-hub.sqlite3', '.env', '.env.production',
    'docs/backup.db', 'web/example.pem', 'web/font.woff2', 'widgets/demo/real-token.txt',
    '.git/config', '.venv/lib/a.py', 'artifacts/test-results/backend.xml',
    'docs/backend-test-results.xml', 'dist/archive.zip', '../private.md',
    '/tmp/private.py', 'web/session.sqlite3-wal', 'widgets/test/recording.wav',
])
def test_sensitive_or_generated_path_excluded(name):
    assert not source_path_allowed(name)


@pytest.mark.parametrize('name', ['README.md', '.env.example', '.github/workflows/ci.yml', 'app/main.py', 'web/client.js', 'widgets/todos/manifest.json', 'docs/assets/client.png', 'previews/client_preview.html'])
def test_source_paths_allowed(name):
    assert source_path_allowed(name)


def create_tree(root: Path):
    for name, text in [('VERSION', '0.1.1\n'), ('README.md', '# Example\n'), ('app/main.py', 'x = 1\n'), ('data/admin-token.txt', 'not-public'), ('.env', 'HUB_ADMIN_TOKEN=not-public'), ('docs/log-results.xml', '<data/>')]:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')


def test_archive_has_only_sources_and_verifiable_hashes(tmp_path):
    source, out = tmp_path / 'source', tmp_path / 'out'
    create_tree(source)
    result = build_archive(source, out, validate=False)
    archive = Path(result['archive'])
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(n.startswith('room-hub/') for n in z.namelist())
        assert not any('/data/' in n or n.endswith('/.env') or n.endswith('.xml') for n in z.namelist())
        manifest = json.loads(z.read('room-hub/SOURCE_MANIFEST.json'))
        for item in manifest['files']:
            content = z.read('room-hub/' + item['path'])
            assert hashlib.sha256(content).hexdigest() == item['sha256']
            assert len(content) == item['bytes']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == result['sha256']
    assert Path(result['sha256_file']).read_text().startswith(result['sha256'])


def test_reproducible_and_refuses_accidental_overwrite(tmp_path):
    source = tmp_path / 'source'; create_tree(source)
    a = build_archive(source, tmp_path / 'one', validate=False)
    b = build_archive(source, tmp_path / 'two', validate=False)
    assert a['sha256'] == b['sha256']
    with pytest.raises(FileExistsError):
        build_archive(source, tmp_path / 'one', validate=False)
    c = build_archive(source, tmp_path / 'one', force=True, validate=False)
    assert c['sha256'] == a['sha256']


def test_symlink_not_followed(tmp_path):
    root = tmp_path / 'source'; create_tree(root)
    target = tmp_path / 'private.txt'; target.write_text('private')
    try:
        (root / 'app' / 'leak.txt').symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('Creating symlinks is not permitted in this test environment')
    with pytest.raises(ValueError, match='Symlink'):
        list(public_files(root))


def test_filled_example_key_is_rejected_without_disclosing_value():
    sample = 'only-an-example-not-a-real-key'
    issues = secret_findings('.env.example', ('HUB_ADMIN_TOKEN=' + sample).encode())
    assert issues and sample not in str(issues)
    assert not secret_findings('.env.example', b'HUB_ADMIN_TOKEN=\nHUB_INGEST_TOKEN=\n')


def test_known_token_detection_without_printing_secret():
    sample = 'gh' + 'p_' + ('A' * 36)
    issues = secret_findings('app/example.py', ('KEY="' + sample + '"').encode())
    assert issues and sample not in str(issues)


def test_links_ignore_fenced_examples():
    text = '[Guide](docs/SETUP.md)\n```\n[x](missing.md)\n```\n![image](docs/assets/client.png)'
    assert markdown_links(text) == ['docs/SETUP.md', 'docs/assets/client.png']


def test_staged_checker_finds_private_path(tmp_path):
    if not shutil.which('git'):
        pytest.skip('Git is not installed')
    create_tree(tmp_path)
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', 'data/admin-token.txt'], cwd=tmp_path, check=True)
    result = check_repository(tmp_path, staged=True)
    assert any('Staged file is not a publishable source path: data/admin-token.txt' in e for e in result['errors'])
