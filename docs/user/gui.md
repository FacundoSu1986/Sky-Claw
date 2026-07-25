# Interfaz gráfica

> **Audiencia:** usuarios de la aplicación NiceGUI.
>
> **Estado:** Implementado con algunas capacidades dependientes del entorno.
>
> **Fuentes canónicas:** `sky_claw/antigravity/gui/sky_claw_gui.py`,
> `sky_claw/antigravity/gui/views/forge_dashboard.py` y
> `sky_claw/antigravity/gui/controllers/ritual_runner.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Arranque

```powershell
.\.venv\Scripts\python.exe -m sky_claw --mode gui
```

El binario congelado selecciona GUI por defecto. El bootloader escanea el
entorno, inicia servicios y publica el resultado en el store reactivo.

## Secciones

| Sección | Propósito |
|---|---|
| Panel | Estado, telemetría, chat y rituales disponibles |
| Mods | Inventario y selección |
| Conflictos | Conflictos detectados y resoluciones |
| Descargas | Historial, acciones y aprobaciones relacionadas |
| Ajustes | Configuración visible de la aplicación |

La disponibilidad de un ritual deriva del escaneo real; no se debe interpretar
una tarjeta visible como prueba de que el ejecutable o el perfil sean válidos.

## Flujo de ritual

1. Seleccionar una acción.
2. Revisar el preflight y la tool detectada.
3. Examinar el preview cuando exista.
4. Si la ruta incluye un gate HITL, aprobar o denegar la solicitud. Los cinco
   rituales actualmente expuestos por el Panel están cableados a ese gate; si
   el gate no está disponible, fallan cerrados.
5. Esperar el resultado normalizado y revisar la evidencia.

`ritual_runner.py` consume `normalize_tool_result()` para presentar una vista
común. El resultado visual no sustituye la revisión del journal, logs o
artefactos cuando la operación cambia archivos.

La ruta LLM general es `lock-only` y no muestra este gate por herencia; sólo los
handlers que inyectan `HITLGuard`, como ciertas descargas e instalaciones,
solicitan aprobación propia.

## Seguridad operativa

No activar autoaprobación en un perfil de uso real sin haber completado la
[validación del rig](../operations/real_rig_validation.md). Ante un estado
ambiguo, detener nuevas ejecuciones y seguir
[troubleshooting](troubleshooting.md).
