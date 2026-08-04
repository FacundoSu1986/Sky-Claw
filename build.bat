@echo off
setlocal enabledelayedexpansion
title Sky-Claw Build

echo.
echo  ============================
echo   Sky-Claw - Building .exe
echo  ============================
echo.

cd /d "%~dp0"

:: Strip spaces from SKIP_TESTS so the common "set SKIP_TESTS=1 && build.bat"
:: idiom (which leaves a trailing space in the value) still matches the flag.
set "SKIP_TESTS=%SKIP_TESTS: =%"


:: ---------------------------------------------------------------------------
:: [1/4] Toolchain check: uv is the only supported environment manager.
:: uv creates/reuses .venv on demand and is idempotent: if the environment
:: already matches uv.lock, the sync step below is a fast no-op.
:: ---------------------------------------------------------------------------
echo [1/4] Checking toolchain (uv)...
where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo  ERROR: uv not found in PATH.
    echo  Install it first:  pip install uv   (or https://docs.astral.sh/uv/^)
    if not defined CI pause
    exit /b 1
)

if not exist ".venv\" (
    echo  .venv not found - uv sync will create it.
) else (
    echo  Reusing existing .venv ^(no recreation if already in sync^).
)

:: ---------------------------------------------------------------------------
:: [2/4] Dependencies: uv sync replaces venv+pip. No pip is required and an
:: up-to-date environment is left untouched (idempotent).
:: ---------------------------------------------------------------------------
echo [2/4] Syncing dependencies (uv sync --extra dev)...
uv sync --extra dev
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency sync failed ^(uv sync --extra dev^).
    if not defined CI pause
    exit /b 1
)
echo.

:: ---------------------------------------------------------------------------
:: [3/4] Tests: fail-fast by default, but a developer can set SKIP_TESTS=1
:: (also true/yes/on) to omit the suite on machines without local DB config
:: or Skyrim paths.
:: ---------------------------------------------------------------------------
echo [3/4] Running tests...
if /i "%SKIP_TESTS%"=="1" goto :tests_skipped
if /i "%SKIP_TESTS%"=="true" goto :tests_skipped
if /i "%SKIP_TESTS%"=="yes" goto :tests_skipped
if /i "%SKIP_TESTS%"=="on" goto :tests_skipped

uv run pytest tests/ -q --tb=short
if errorlevel 1 (
    echo.
    echo  ERROR: Tests failed. Fix failing tests before building,
    echo  or re-run with SKIP_TESTS=1 to bypass this check.
    if not defined CI pause
    exit /b 1
)
goto :tests_done

:tests_skipped
echo  SKIP_TESTS is set - test suite omitted.

:tests_done
echo.

:: ---------------------------------------------------------------------------
:: [4/4] PyInstaller build. pyinstaller is not part of the [dev] extras, so
:: it is injected ephemerally via "uv run --with" (no lockfile changes).
:: ---------------------------------------------------------------------------
echo [4/4] Building SkyClawApp.exe...
uv run --with pyinstaller pyinstaller sky_claw.spec --clean
if errorlevel 1 (
    echo.
    echo  ERROR: Build failed. Check the output above.
    if not defined CI pause
    exit /b 1
)

if exist "dist\SkyClawApp.exe" (
    echo  ============================
    echo   Build complete!
    echo   dist\SkyClawApp.exe
    echo  ============================
    echo.
    if not defined CI pause
    exit /b 0
) else (
    echo  ERROR: SkyClawApp.exe not found in dist/.
    echo.
    if not defined CI pause
    exit /b 1
)
