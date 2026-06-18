@echo off
echo ============================================================
echo  Running conciliation pipeline for all organizations
echo ============================================================

echo.
echo [ORG 1] Starting...
.venv\Scripts\python.exe main.py --live --org 1 --year 2026
if %ERRORLEVEL% NEQ 0 (
    echo [ORG 1] FAILED with exit code %ERRORLEVEL%
) else (
    echo [ORG 1] Finished OK
)

echo.
echo [ORG 2] Starting...
.venv\Scripts\python.exe main.py --live --org 2 --year 2026
if %ERRORLEVEL% NEQ 0 (
    echo [ORG 2] FAILED with exit code %ERRORLEVEL%
) else (
    echo [ORG 2] Finished OK
)

echo.
echo ============================================================
echo  All organizations processed
echo ============================================================
