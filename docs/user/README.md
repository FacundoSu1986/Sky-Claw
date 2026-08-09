# Guías de usuario

> **Audiencia:** usuarios de Sky-Claw y operadores de una instalación local.
>
> **Estado:** Parcial; el flujo base está implementado y las ejecuciones reales
> dependen de la instalación local de Skyrim, MO2 y sus herramientas.
>
> **Fuentes canónicas:** `sky_claw/__main__.py`,
> `sky_claw/app/gui/` y `sky_claw/local/AGENTS.md`.
>
> **Última verificación:** 2026-08-08 sobre `fix/dyndolod-cli-contrato-asistida`
> `e8427063`.

## Ruta recomendada

1. [Instalación](installation.md).
2. [Configuración](configuration.md).
3. [Interfaz gráfica](gui.md) o [CLI](cli.md).
4. [Community Shaders](community_shaders.md), capacidad opt-in de preparación del
   entorno que no hace falta para el flujo base.
5. [Flujo seguro](safe_workflows.md) antes de ejecutar un ritual.
6. [Diagnóstico](troubleshooting.md) si el preflight o una operación falla.

El modo [Telegram](telegram.md) es opcional. La documentación de usuario no
reemplaza el [SOP técnico del pipeline](../pipeline/skyrim_sop.md), que define
el orden canónico de herramientas.

## Límites

- Sky-Claw es un plano de control, no un agente autónomo irrestricto. La
  aprobación humana depende de la operación y de la ruta concreta; consultar
  el [flujo seguro](safe_workflows.md) antes de ejecutar una mutación.
- Una ejecución desde fuente o un test con subprocess mockeado no demuestra
  visibilidad USVFS.
- El primer uso de herramientas externas debe hacerse sobre un perfil
  descartable y con evidencia recuperable.
