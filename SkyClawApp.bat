@echo off
setlocal enabledelayedexpansion
title Sky-Claw - Skyrim Mod Manager

echo.
echo ============================
echo  Sky-Claw - Iniciando
echo ============================
echo.

cd /d "%~dp0"

:: 1. Avisar si los puertos de la GUI ya estan en uso: 8080 (NiceGUI, la UI) y
::    8765 (API /api/chat). Antes se miraba 8888, que no lo bindea nadie: el
::    conflicto real (WinError 10048 al reabrir) pasaba sin aviso.
set "GUI_PORTS=8080 8765"
for %%P in (%GUI_PORTS%) do (
    netstat -ano | findstr /C:":%%P " >nul
    if !errorlevel! equ 0 (
        echo [AVISO] El puerto %%P ya parece estar en uso.
        echo Intentando continuar igual, pero podria haber conflictos.
        echo.
    )
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
        echo [ERROR] La aplicacion se cerro con errores.
    )
) else (
    :: 3. Fallback to Python
    echo [+] No se encontro .exe compilado. Buscando Python
    
    :: Check if venv exists: el entorno del repo es .venv (lo crea build.bat/uv).
    if exist ".venv\Scripts\python.exe" (
        echo [i] Usando entorno virtual [.venv]
        set "PY_CMD=.venv\Scripts\python.exe"
    ) else (
        echo [i] Usando Python del sistema
        set "PY_CMD=python"
    )
    
    echo [+] Iniciando con !PY_CMD!
    !PY_CMD! -m sky_claw --mode gui
    if errorlevel 1 (
        echo.
        echo [ERROR] Error al iniciar con Python. 
        echo Asegurate de haber instalado las dependencias con build.bat
    )
)

echo.
echo Presione cualquier tecla para salir.
pause >nul
