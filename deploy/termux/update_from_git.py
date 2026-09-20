"""Safe GitHub-main updater for the V35 deployment clone.

Run from the outer Termux shell through update-from-git.sh. Runtime/private
state stays outside Git. Updates are fast-forward only and roll back tracked
source plus SQLite when a post-fetch deployment step fails.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request


class UpdateError(RuntimeError):
    pass


def run(cmd, *, cwd=None, capture=False, check=True):
    kwargs = {"cwd": cwd, "check": check, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, **kwargs)


def out(cmd, *, cwd=None) -> str:
    return run(cmd, cwd=cwd, capture=True).stdout.strip()


def git(root: Path, *args: str, capture: bool = False, check: bool = True):
    return run(["git", "-C", str(root), *args], capture=capture, check=check)


def git_out(root: Path, *args: str) -> str:
    return git(root, *args, capture=True).stdout.strip()


def stop(msg: str) -> "NoReturn":
    raise UpdateError(msg)


def http_health() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8088/healthz", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def stop_room_hub(root: Path, service: Path) -> None:
    stopper = root / "deploy/termux/stop-room-hub.py"
    if stopper.is_file():
        run([sys.executable, str(stopper), "--root", str(root)])
    else:
        run(["sv", "down", str(service)], check=False)


def start_room_hub(service: Path) -> None:
    run(["sv", "up", str(service)])


def sqlite_backup(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UpdateError("SQLite backup integrity check failed")
    return True


def restore_sqlite(src: Path, dst: Path) -> None:
    if not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(dst) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, dst)


def sync_v35_deps(root: Path, prefix: Path) -> None:
    venv_python = root / ".venv-v35/bin/python"
    if not (root / ".venv-v35/pyvenv.cfg").is_file() or not venv_python.is_file():
        raise UpdateError("existing V35 virtual environment is missing")
    proot = prefix / "bin/proot-distro"
    if not proot.is_file():
        raise UpdateError("proot-distro is missing")
    common = [
        str(proot), "login", "--bind", f"{root}:/opt/room-hub", "roomhub", "--",
        "/opt/room-hub/.venv-v35/bin/python", "-m", "pip",
    ]
    run(common + [
        "install", "--only-binary=:all:",
        "-r", "/opt/room-hub/deploy/termux/requirements-v35.txt",
    ])
    run(common + ["check"])


def status(root: Path, remote_name: str, remote_branch: str) -> dict:
    branch = git_out(root, "branch", "--show-current")
    if branch != "main":
        stop(f"V35 deployments must stay on local main; found: {branch}")
    try:
        git_out(root, "remote", "get-url", remote_name)
    except subprocess.CalledProcessError as exc:
        raise UpdateError(f"Git remote is missing: {remote_name}") from exc

    print(f"Fetching {remote_name}/{remote_branch}...", flush=True)
    git(root, "fetch", "--prune", remote_name, remote_branch)

    remote_ref = f"{remote_name}/{remote_branch}"
    local = git_out(root, "rev-parse", "HEAD")
    remote = git_out(root, "rev-parse", remote_ref)
    local_tree = git_out(root, "rev-parse", "HEAD^{tree}")
    remote_tree = git_out(root, "rev-parse", f"{remote_ref}^{{tree}}")
    counts = git_out(root, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}")
    dirty = git_out(root, "status", "--porcelain", "--untracked-files=normal")
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    print("\n=== Room Hub Git status ===")
    print(f"branch       : {branch}")
    print(f"local HEAD   : {local}")
    print(f"remote HEAD  : {remote}")
    print(f"local tree   : {local_tree}")
    print(f"remote tree  : {remote_tree}")
    print(f"ahead/behind : {counts}")
    print(f"version      : {version}")
    if dirty:
        print("working tree : DIRTY")
        print(dirty)
    else:
        print("working tree : clean (ignored runtime/private files excluded)")

    return {
        "branch": branch,
        "local": local,
        "remote": remote,
        "local_tree": local_tree,
        "remote_tree": remote_tree,
        "counts": counts,
        "dirty": dirty,
        "version": version,
        "remote_ref": remote_ref,
    }


def deploy(root: Path, prefix: Path, home: Path, remote_name: str, remote_branch: str) -> int:
    info = status(root, remote_name, remote_branch)
    if info["dirty"]:
        stop("Tracked/unignored local changes exist. Do not overwrite them automatically.")
    if info["local"] == info["remote"]:
        print("ALREADY CURRENT: no Git update is needed.")
        print(json.dumps(http_health(), ensure_ascii=False))
        return 0

    ancestor = git(
        root, "merge-base", "--is-ancestor", info["local"], info["remote"],
        check=False,
    ).returncode
    if ancestor != 0:
        stop("Local history diverged from origin/main. Refusing a reset/force update.")

    short_local = info["local"][:12]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = home / "room-hub-git-backups" / f"{stamp}-{short_local}"
    backup.mkdir(parents=True, exist_ok=False)
    (home / "room-hub-git-backups").chmod(0o700)
    backup.chmod(0o700)
    (backup / "previous-head.txt").write_text(info["local"] + "\n", encoding="utf-8")
    (backup / "target-head.txt").write_text(info["remote"] + "\n", encoding="utf-8")
    previous_requirements = git_out(
        root, "show", f"{info['local']}:deploy/termux/requirements-v35.txt"
    )
    (backup / "requirements-v35.before.txt").write_text(
        previous_requirements + "\n", encoding="utf-8"
    )

    req_changed = (
        git(
            root, "diff", "--quiet", info["local"], info["remote"], "--",
            "deploy/termux/requirements-v35.txt", check=False,
        ).returncode
        != 0
    )

    data_env = os.environ.get("ROOM_HUB_DATA_DIR") or os.environ.get("HUB_DATA_DIR")
    data_dir = Path(data_env).expanduser().resolve() if data_env else (root / "data").resolve()
    db = data_dir / "room-hub.sqlite3"
    db_copy = backup / "room-hub.sqlite3"
    service = prefix / "var/service/room-hub"
    db_backed_up = False
    switched = False

    print("\nStopping Room Hub and creating a rollback point...", flush=True)
    stop_room_hub(root, service)
    db_backed_up = sqlite_backup(db, db_copy)

    try:
        print("Fast-forwarding local main to fetched origin/main...", flush=True)
        git(root, "merge", "--ff-only", info["remote"])
        switched = True
        run([sys.executable, str(root / "scripts/check_repo.py")])

        if req_changed:
            print(
                "V35 dependency pins changed; synchronizing only "
                "deploy/termux/requirements-v35.txt...",
                flush=True,
            )
            sync_v35_deps(root, prefix)
        else:
            print(
                "V35 dependency pins unchanged; keeping the existing validated V35 environment.",
                flush=True,
            )

        if git_out(root, "status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError("Unexpected working tree changes after update.")

        start_room_hub(service)
        expected = (root / "VERSION").read_text(encoding="utf-8").strip()
        health = None
        for _ in range(30):
            try:
                health = http_health()
            except Exception:
                health = None
            if health and str(health.get("version")) == expected:
                print("\nUPDATED SUCCESSFULLY")
                print(f"  HEAD    : {git_out(root, 'rev-parse', 'HEAD')}")
                print(f"  tree    : {git_out(root, 'rev-parse', 'HEAD^{tree}')}")
                print(f"  version : {expected}")
                print(f"  backup  : {backup}")
                print(f"  health  : {json.dumps(health, ensure_ascii=False)}")
                return 0
            time.sleep(1)
        raise UpdateError("Health/version verification failed after update.")
    except BaseException:
        print("\nUPDATE FAILED. Rolling back code and SQLite DB...", file=sys.stderr)
        try:
            stop_room_hub(root, service)
        except Exception:
            pass
        if switched:
            git(root, "reset", "--hard", info["local"], check=False)
        if req_changed:
            try:
                sync_v35_deps(root, prefix)
            except Exception:
                print(
                    "WARNING: old V35 dependencies could not be fully restored.",
                    file=sys.stderr,
                )
        if db_backed_up:
            try:
                restore_sqlite(db_copy, db)
            except Exception:
                print(
                    f"WARNING: SQLite backup could not be restored automatically: {db_copy}",
                    file=sys.stderr,
                )
        run(["sv", "up", str(service)], check=False)
        print(f"Rollback target HEAD: {info['local']}", file=sys.stderr)
        print(f"Backup directory: {backup}", file=sys.stderr)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare local tracked source with origin/main")
    args = parser.parse_args(argv)

    prefix_value = os.environ.get("PREFIX")
    if not prefix_value:
        print("STOP: Run this in the outer Termux shell (PREFIX is missing).", file=sys.stderr)
        return 1
    prefix = Path(prefix_value)
    home = Path.home()
    root = Path(os.environ.get("ROOM_HUB_ROOT", Path(__file__).resolve().parents[2])).resolve()
    remote_name = os.environ.get("ROOM_HUB_GIT_REMOTE", "origin")
    remote_branch = os.environ.get("ROOM_HUB_GIT_BRANCH", "main")

    if not (root / ".git").is_dir():
        print(
            "STOP: This Room Hub install is not Git-managed. Run adopt-git.sh once first.",
            file=sys.stderr,
        )
        return 1
    if not (prefix / "var/service/room-hub").is_dir():
        print("STOP: room-hub service not found.", file=sys.stderr)
        return 1

    try:
        if args.check:
            info = status(root, remote_name, remote_branch)
            if info["dirty"]:
                return 2
            if info["local"] != info["remote"]:
                return 3
            print("SYNCED: local tracked source is exactly origin/main.")
            return 0
        return deploy(root, prefix, home, remote_name, remote_branch)
    except (UpdateError, subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
