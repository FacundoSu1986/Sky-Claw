@echo off
setlocal enabledelayedexpansion
title Sky-Claw - Skyrim Mod Manager

echo.
echo ============================
echo  Sky-Claw - Iniciando
echo ============================
echo.

cd /d "%~dp0"

:: 1. Check if something is already listening on port 8888
netstat -ano | findstr :8888 >nul
if %errorlevel% equ 0 (
    echo [!] ALERTA: El puerto 8888 ya parece estar en uso.
    echo Intentando continuar igual, pero podria haber conflictos.
    echo.
)

:: 2. Try to run .exe version
set "EXE_PATH="
if exist "dist\SkyClawApp.exe" (
    set "EXE_PATH=dist\SkyClawApp.exe"
) else (
    if exist "SkyClawApp.exe" (
        set "EXE_PATH=SkyClawApp.exe"
    )
)

if defined EXE_PATH (
    echo [+] Iniciando version compilada [!EXE_PATH!]
    "!EXE_PATH!"
    if errorlevel 1 (
        echo.
        echo [!] La aplicacion se cerro con errores.
    )
) else (
    :: 3. Fallback to Python
    echo [+] No se encontro .exe compilado. Buscando Python
    
    :: Check if venv exists
    if exist "venv\Scripts\python.exe" (
        echo [i] Usando entorno virtual [venv]
        set "PY_CMD=venv\Scripts\python.exe"
    ) else (
        echo [i] Usando Python del sistema
        set "PY_CMD=python"
    )
    
    echo [+] Iniciando con !PY_CMD!
    !PY_CMD! -m sky_claw --mode gui
    if errorlevel 1 (
        echo.
        echo [!] Error al iniciar con Python. 
        echo Asegurate de haber instalado las dependencias con build.bat
    )
)

echo.
echo Presione cualquier tecla para salir.
pause >nul
