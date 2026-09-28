@echo off
setlocal
cd /d "%~dp0"
set PYTHON=
if defined MIMO_PYTHON set PYTHON=%MIMO_PYTHON%
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not defined PYTHON for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYTHON set PYTHON=%%i
if not defined PYTHON (
  echo Python not found.
  pause
  exit /b 1
)

rem Free leftover listener on 8765 (orphaned after closing the console window).
echo Checking port 8765...
for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /R /C:":8765 .*LISTENING"') do (
  echo Killing leftover process PID %%p on port 8765
  taskkill /PID %%p /T /F >nul 2>&1
)

echo Starting MiMo Link...
"%PYTHON%" "%~dp0server.py" --open
echo.
echo MiMo Link exited. Press any key to close this window.
pause >nul
