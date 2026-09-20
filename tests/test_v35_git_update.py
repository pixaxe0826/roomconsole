from __future__ import annotations

import os
from pathlib import Path
import shutil
import importlib.util
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/termux/update_from_git.py"
WRAPPER = ROOT / "deploy/termux/update-from-git.sh"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def make_git_pair(tmp_path: Path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.name", "CI Test")
    git(remote, "config", "user.email", "ci@example.invalid")
    (remote / "VERSION").write_text("0.1.7\n", encoding="utf-8")
    (remote / "payload.txt").write_text("one\n", encoding="utf-8")
    (remote / "deploy/termux").mkdir(parents=True)
    (remote / "deploy/termux/requirements-v35.txt").write_text("example==1\n", encoding="utf-8")
    (remote / "scripts").mkdir()
    (remote / "scripts/check_repo.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    git(remote, "add", "-A")
    git(remote, "commit", "-m", "initial")

    local = tmp_path / "room-hub"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(local)],
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    prefix = tmp_path / "prefix"
    (prefix / "var/service/room-hub").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        PREFIX=str(prefix),
        ROOM_HUB_ROOT=str(local),
        HOME=str(tmp_path / "home"),
    )
    Path(env["HOME"]).mkdir()
    return remote, local, env


@pytest.mark.skipif(os.name == "nt", reason="Bash wrapper is a Termux/Linux entry point")
def test_termux_wrapper_is_minimal_and_parses():
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)
    text = WRAPPER.read_text(encoding="utf-8")
    assert "update_from_git.py" in text
    assert text.count("$(") <= 2


@pytest.mark.skipif(os.name == "nt", reason="Git/Termux check-path regression")
def test_check_reports_exact_sync(tmp_path: Path):
    _, local, env = make_git_pair(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SYNCED: local tracked source is exactly origin/main." in result.stdout
    local_head = git(local, "rev-parse", "HEAD")
    assert f"local HEAD   : {local_head}" in result.stdout
    assert f"remote HEAD  : {local_head}" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Git/Termux check-path regression")
def test_check_fetches_new_main_and_returns_behind_without_mutating_local(tmp_path: Path):
    remote, local, env = make_git_pair(tmp_path)
    before = git(local, "rev-parse", "HEAD")

    (remote / "payload.txt").write_text("two\n", encoding="utf-8")
    git(remote, "add", "payload.txt")
    git(remote, "commit", "-m", "next")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert git(local, "rev-parse", "HEAD") == before
    assert git(local, "rev-parse", "origin/main") != before
    assert "working tree : clean" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Git/Termux check-path regression")
def test_check_rejects_dirty_tracked_source(tmp_path: Path):
    _, local, env = make_git_pair(tmp_path)
    (local / "payload.txt").write_text("local change\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30,
    )
    assert result.returncode == 2
    assert "working tree : DIRTY" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Git/Termux deploy-path regression")
def test_successful_update_reports_tree_without_rolling_back(tmp_path: Path, monkeypatch, capsys):
    remote, local, env = make_git_pair(tmp_path)
    before = git(local, "rev-parse", "HEAD")

    (remote / "payload.txt").write_text("two\n", encoding="utf-8")
    git(remote, "add", "payload.txt")
    git(remote, "commit", "-m", "next")
    target = git(remote, "rev-parse", "HEAD")

    spec = importlib.util.spec_from_file_location("room_hub_update_from_git", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "stop_room_hub", lambda root, service: None)
    monkeypatch.setattr(module, "start_room_hub", lambda service: None)
    monkeypatch.setattr(module, "http_health", lambda: {"status": "ok", "version": "0.1.7"})

    rc = module.deploy(
        local,
        Path(env["PREFIX"]),
        Path(env["HOME"]),
        "origin",
        "main",
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.out + captured.err
    assert git(local, "rev-parse", "HEAD") == target
    assert git(local, "rev-parse", "HEAD") != before
    tree = git(local, "rev-parse", "HEAD^{tree}")
    assert "UPDATED SUCCESSFULLY" in captured.out
    assert f"tree    : {tree}" in captured.out
    assert "UPDATE FAILED" not in captured.err
