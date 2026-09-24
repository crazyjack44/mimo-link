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
echo Starting MiMo Link...
"%PYTHON%" "%~dp0server.py" --open
pause
