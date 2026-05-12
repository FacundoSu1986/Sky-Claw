@echo off
setlocal enabledelayedexpansion
title Sky-Claw Build

echo.
echo  ============================
echo   Sky-Claw - Building .exe
echo  ============================
echo.

cd /d "%~dp0"

:: 1. Check for Virtual Environment
if not exist "venv\" (
    echo [1/3] Virtual environment not found. Creating it...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo  ERROR: Failed to create virtual environment. 
        echo  Ensure python is in your PATH.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Using existing virtual environment.
)

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo  ERROR: venv\Scripts\activate.bat not found.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

echo [2/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install pyinstaller
python -m pip install -e ".[dev]"
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency installation failed.
    pause
    exit /b 1
)
echo.

echo [3/3] Running tests...
python -m pytest tests/ -q --tb=short
if errorlevel 1 (
    echo.
    echo  ERROR: Tests failed. Fix failing tests before building.
    pause
    exit /b 1
)
echo.

echo [4/4] Building SkyClawApp.exe...
pyinstaller sky_claw.spec --clean
if errorlevel 1 (
    echo.
    echo  ERROR: Build failed. Check the output above.
    pause
    exit /b 1
)

if exist "dist\SkyClawApp.exe" (
    echo  ============================
    echo   Build complete!
    echo   dist\SkyClawApp.exe
    echo  ============================
) else (
    echo  ERROR: SkyClawApp.exe not found in dist/.
)
echo.
pause
