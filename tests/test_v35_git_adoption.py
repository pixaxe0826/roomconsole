from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/termux/adopt-git.sh"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_case(tmp_path: Path, *, health_ok: bool):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source"
    ignore = shutil.ignore_patterns(
        ".git", ".venv", ".venv-v35", "__pycache__", ".pytest_cache",
        "artifacts", "dist", "build", "data", "runtime", "backups", "secrets",
    )
    shutil.copytree(ROOT, source, ignore=ignore)
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "synthetic source"], cwd=source, check=True, capture_output=True)

    install = home / "room-hub"
    shutil.copytree(source, install, ignore=shutil.ignore_patterns(".git"))
    assert not (install / ".env").exists()
    data = install / "data"
    data.mkdir()
    (data / "sentinel.txt").write_text("keep-private-runtime", encoding="utf-8")

    prefix = tmp_path / "prefix"
    service = prefix / "var/service/room-hub"
    service.mkdir(parents=True)
    write_executable(service / "run", "#!/bin/sh\nexit 0\n")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    write_executable(fakebin / "sv", "#!/bin/sh\nexit 0\n")
    if health_ok:
        write_executable(fakebin / "curl", "#!/bin/sh\nprintf '%s\\n' '{\"version\":\"0.1.7\"}'\n")
    else:
        write_executable(fakebin / "curl", "#!/bin/sh\nexit 7\n")

    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PREFIX=str(prefix),
        PATH=f"{fakebin}:{env['PATH']}",
        ROOM_HUB_ROOT=str(install),
        ROOM_HUB_REPO_URL=str(source),
        ROOM_HUB_BRANCH="main",
        ROOM_HUB_ADOPT_HEALTH_ATTEMPTS="1",
        ROOM_HUB_ADOPT_HEALTH_SLEEP="0",
    )
    return home, install, env


def test_adoption_succeeds_without_private_env_file(tmp_path: Path):
    home, install, env = make_case(tmp_path, health_ok=True)
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Git adoption complete." in result.stdout
    assert (install / ".git").is_dir()
    assert (install / "data/sentinel.txt").read_text(encoding="utf-8") == "keep-private-runtime"
    assert subprocess.run(
        ["git", "-C", str(install), "status", "--porcelain"],
        text=True, capture_output=True, check=True,
    ).stdout == ""
    assert subprocess.run(
        ["git", "-C", str(install), "config", "--get", "pull.ff"],
        text=True, capture_output=True, check=True,
    ).stdout.strip() == "only"
    assert subprocess.run(
        ["git", "-C", str(install), "remote", "get-url", "--push", "origin"],
        text=True, capture_output=True, check=True,
    ).stdout.strip().startswith("disabled-v35://")
    assert list(home.glob("room-hub-pre-git-*"))


def test_adoption_failure_restores_old_install_and_never_reports_success(tmp_path: Path):
    home, install, env = make_case(tmp_path, health_ok=False)
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Git adoption complete." not in combined
    assert "Previous installation restored." in combined
    assert not (install / ".git").exists()
    assert (install / "data/sentinel.txt").read_text(encoding="utf-8") == "keep-private-runtime"
    assert list(home.glob("room-hub-failed-git-*"))
