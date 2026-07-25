# Instalación

> **Audiencia:** usuarios y desarrolladores que preparan Sky-Claw en Windows.
>
> **Estado:** Implementado para ejecución desde fuente; Parcial para entrega
> empaquetada.
>
> **Fuentes canónicas:** `pyproject.toml`, `build.bat`, `sky_claw.spec` y
> `.github/workflows/ci.yml`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Requisitos

- Windows 10/11 para el flujo completo MO2/USVFS.
- Python 3.11 o 3.12, las versiones ejecutadas por CI.
- Mod Organizer 2 y Skyrim Special Edition/Anniversary Edition.
- Las herramientas que vaya a usar el ritual: LOOT, SSEEdit u otras.

## Entorno desde fuente

Desde la raíz del repositorio:

```powershell
uv sync --locked --extra dev
.\.venv\Scripts\python.exe -m sky_claw --mode gui
```

`build.bat` también crea y usa `.venv`, instala dependencias, ejecuta pytest y
construye `dist\SkyClawApp.exe`. No es un instalador de release firmado.

## Configuración inicial

El asistente comprobado está en:

```powershell
.\.venv\Scripts\python.exe local_scripts\scripts\first_run.py
```

Revisar la [configuración](configuration.md) antes del primer arranque.

## Bridge MO2/USVFS

La instalación productiva del bridge exige Windows y el ejecutable congelado:

```powershell
.\dist\SkyClawApp.exe --mode install-vfs-bridge --mo2-root "D:\MO2Portable"
```

El modo de desarrollo con `SKYCLAW_ALLOW_DEV_WORKER=1` es una vía de prueba; un
`python.exe` de venv puede ser rechazado por USVFS. Reiniciar MO2 después de
instalar un plugin nuevo y ejecutar el
[smoke del rig real](../operations/real_rig_validation.md).

## Verificación mínima

- La GUI o CLI arranca sin exponer secretos.
- `~/.sky_claw/config.toml` existe y no contiene claves sensibles.
- `ModOrganizer.exe` y el directorio `Data` existen en las rutas elegidas.
- La instalación del bridge y el canary VFS se validan antes de ejecutar LOOT.
