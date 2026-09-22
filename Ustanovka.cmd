@echo off
rem ============================================================
rem  Hagen installer. ASCII-only and CRLF on purpose:
rem  cmd.exe cannot parse a batch file with UTF-8 Cyrillic or
rem  with Unix line endings. All Russian text is printed by install.py.
rem
rem  Which Python runs install.py:
rem   1) python\python.exe inside this folder (portable copy);
rem   2) an installed Python 3.12 via the "py" launcher;
rem   3) otherwise portable Python 3.12 is downloaded from nuget.org
rem      into python\ (no admin rights, nothing is registered).
rem  Exactly 3.12: the libraries in lock.json are built for it.
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist "python\python.exe" (
  "python\python.exe" install.py %*
  goto done
)

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.12 install.py %*
  goto done
)

echo.
echo   Python not found. Downloading portable Python 3.12 (about 15 MB)...
curl.exe -L --fail -o "_python.zip" "https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg"
if errorlevel 1 goto nopython
if exist "_python_tmp" rmdir /s /q "_python_tmp"
mkdir "_python_tmp"
tar -xf "_python.zip" -C "_python_tmp"
if errorlevel 1 goto nopython
move "_python_tmp\tools" "python" >nul
rmdir /s /q "_python_tmp"
del "_python.zip"
"python\python.exe" install.py %*
goto done

:nopython
echo.
echo   Could not get Python automatically.
echo   Install Python 3.12 from https://www.python.org and run this file again.

:done
echo.
pause
