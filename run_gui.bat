@echo off
REM Safe File Sorter - double-click launcher for Windows.
REM Prefers pythonw.exe so no console window stays open; falls back to python.exe
REM and finally to the py launcher.

setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw.exe "run.py" %*
    exit /b 0
)

where python.exe >nul 2>nul
if %ERRORLEVEL%==0 (
    python.exe "run.py" %*
    exit /b %ERRORLEVEL%
)

where py.exe >nul 2>nul
if %ERRORLEVEL%==0 (
    py.exe "run.py" %*
    exit /b %ERRORLEVEL%
)

echo Python 3 was not found on PATH.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
exit /b 1
