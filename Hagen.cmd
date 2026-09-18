@echo off
rem ============================================================
rem  Hagen - local meeting recorder.
rem  NOTE: this file is intentionally ASCII-only and CRLF. cmd.exe cannot
rem  parse a batch file that contains UTF-8 Cyrillic. All Russian text
rem  is printed by run.py, and the window title is set from Python.
rem
rem  Runs the portable python\python.exe of this folder: it sees the
rem  libraries of .venv through python\Lib\site-packages\hagen-venv.pth,
rem  so the folder can be moved anywhere (see hagen\portable.py).
rem ============================================================
setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "OMP_NUM_THREADS=7"
set "KMP_DUPLICATE_LIB_OK=TRUE"
set "HF_HUB_DISABLE_TELEMETRY=1"

set "PY=python\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo   ERROR: Python not found in %cd%
  echo   Run Ustanovka.cmd first.
  echo.
  pause
  exit /b 1
)

"%PY%" run.py --app %*
set CODE=%ERRORLEVEL%

if not "%CODE%"=="0" (
  echo.
  echo   Hagen exited with code %CODE%.
  echo   See logs\hagen.log for details.
  echo.
  pause
)
exit /b %CODE%
