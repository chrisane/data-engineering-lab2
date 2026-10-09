@echo off
rem ---------------------------------------------------------
rem Opens a terminal window showing the pipeline status board.
rem Double-click this file, or run it from any terminal.
rem Arguments are passed through, e.g.:  run_pipeline.bat --regenerate
rem ---------------------------------------------------------

setlocal
rem Drop the trailing backslash so quoted paths stay valid.
set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Virtual environment not found at "%PYTHON%".
    echo Create it first:  python -m venv .venv  then  .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 2
)

start "Retail Data Platform v2 - Pipeline" /D "%PROJECT_DIR%" cmd /k ""%PYTHON%" scripts\run_pipeline.py %* & echo. & echo Press any key to close this window... & pause >nul & exit"
