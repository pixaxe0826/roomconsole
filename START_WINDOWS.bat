@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'"
if errorlevel 1 goto error
if not exist .venv\Scripts\python.exe (
 py -3 -m venv .venv
 if errorlevel 1 goto error
 .venv\Scripts\python.exe -m pip install -r requirements.txt
 if errorlevel 1 goto error
)
.venv\Scripts\python.exe run.py
pause
exit /b 0
:error
echo Setup failed. Install Python 3.11 or newer and check your network.
pause
exit /b 1
