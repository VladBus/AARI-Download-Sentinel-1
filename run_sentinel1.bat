@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo ERROR: Virtual environment Python was not found:
    echo        %PYTHON_EXE%
    echo Create it first with: py -3.14 -m venv .venv
    exit /b 1
)

"%PYTHON_EXE%" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"
exit /b %EXIT_CODE%
