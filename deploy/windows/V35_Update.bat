@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
title LG V35 Room Hub Update

rem Values copied from the user's V35ssh.bat. No password is stored here.
set "HOST=192.168.0.14"
set "PORT=8022"
set "USER=u0_a306"
set "RH_SELF=%~f0"
set "RH_PAYLOAD=%TEMP%\room-hub-update-%RANDOM%-%RANDOM%.sh"

where ssh.exe >nul 2>&1
if errorlevel 1 goto :no_ssh
where powershell.exe >nul 2>&1
if errorlevel 1 goto :no_powershell

echo ========================================
echo       LG V35 Room Hub Git Update
echo ========================================
echo Host: %HOST%  Port: %PORT%  User: %USER%
echo Run this only after reviewing and merging the PR and confirming main CI is green.
echo This tool does NOT query GitHub Actions; it deploys the current origin/main.
echo SSH may ask for your existing password or a first-use host key check.
echo Never accept a changed host key without verifying the V35.
echo.

powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $c=New-Object Net.Sockets.TcpClient; try { $a=$c.ConnectAsync('%HOST%',%PORT%); if (-not $a.Wait(5000)) { exit 1 }; if (-not $c.Connected) { exit 1 } } catch { exit 1 } finally { $c.Dispose() }"
if errorlevel 1 goto :no_connection

rem Extract the embedded script as UTF-8 WITHOUT BOM and with LF (not CRLF).
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $s=[IO.File]::ReadAllText($env:RH_SELF); $m=':__ROOM_HUB_REMOTE__'; $p=$s.Substring($s.LastIndexOf($m)+$m.Length).TrimStart([char]13,[char]10).Replace([string][char]13,''); [IO.File]::WriteAllText($env:RH_PAYLOAD,$p,(New-Object Text.UTF8Encoding($false)))"
if errorlevel 1 goto :extract_failed

echo Connecting and updating current origin/main. Do not close this window...
ssh.exe -T -p %PORT% -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=8 %USER%@%HOST% "bash -s" < "%RH_PAYLOAD%"
set "RH_RC=%ERRORLEVEL%"
del /q "%RH_PAYLOAD%" >nul 2>&1
if not "%RH_RC%"=="0" goto :update_failed

echo.
echo [SUCCESS] Git update, exact source sync, health and stable runit RUN verified.
echo SSH has closed. Closing this window in 5 seconds...
timeout /t 5 /nobreak >nul 2>&1
exit /b 0

:no_ssh
echo [ERROR] Windows OpenSSH Client ssh.exe is missing.
goto :failure
:no_powershell
echo [ERROR] Windows PowerShell powershell.exe is missing.
goto :failure
:no_connection
echo [ERROR] Cannot connect to %HOST%:%PORT%. Check Wi-Fi and Termux sshd.
goto :failure
:extract_failed
echo [ERROR] Cannot prepare the local temporary SSH script.
del /q "%RH_PAYLOAD%" >nul 2>&1
goto :failure
:update_failed
echo.
echo [ERROR] SSH/update/verification failed. Exit code: %RH_RC%
echo Success has NOT been confirmed. Review the output above.
echo A network disconnect does not prove that a remote update stopped.
echo Check the V35 before retrying. Do not force-reset the repository.
:failure
echo.
pause
exit /b 1

:__ROOM_HUB_REMOTE__
set -Eeuo pipefail
umask 077
export PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PATH="$PREFIX/bin:$PATH"
ROOT="$HOME/room-hub"
command -v git >/dev/null
[ -x "$PREFIX/bin/python" ]
[ "$(git -C "$ROOT" branch --show-current)" = main ] || { echo 'STOP: deployment branch is not main' >&2; exit 1; }
[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ] || { echo 'STOP: local changes; no overwrite' >&2; exit 1; }
ORIGIN="$(git -C "$ROOT" remote get-url origin)"
case "$ORIGIN" in
  https://github.com/pixaxe0826/roomconsole|https://github.com/pixaxe0826/roomconsole.git|git@github.com:pixaxe0826/roomconsole.git) ;;
  *) echo 'STOP: unexpected origin; no code executed' >&2; exit 1 ;;
esac
export GIT_TERMINAL_PROMPT=0
git -C "$ROOT" fetch --prune origin main
TARGET="$(git -C "$ROOT" rev-parse origin/main)"
echo "[TARGET] origin/main $TARGET"
TEMP_RUN="$(mktemp -d "$HOME/.room-hub-ssh-update.XXXXXX")"
trap 'rm -rf -- "$TEMP_RUN"' EXIT
# Git objects only: never patch operational tracked files to repair an updater.
git -C "$ROOT" show "$TARGET:deploy/termux/update_from_git.py" > "$TEMP_RUN/update_from_git.py"
git -C "$ROOT" show "$TARGET:deploy/termux/remote_update.py" > "$TEMP_RUN/remote_update.py"
"$PREFIX/bin/python" "$TEMP_RUN/remote_update.py" --root "$ROOT" --target "$TARGET"
