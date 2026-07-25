# Uso de la CLI

> **Audiencia:** usuarios técnicos y operadores.
>
> **Estado:** Implementado.
>
> **Fuente canónica:** `sky_claw/__main__.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Modos principales

```powershell
python -m sky_claw --mode cli
python -m sky_claw --mode oneshot "comando"
python -m sky_claw --mode gui
python -m sky_claw --mode telegram
python -m sky_claw --mode security "comando"
```

En `oneshot` y `security`, `command` es posicional; no existe `--command`.
`-v` o `--verbose` activa logging de depuración.

## Proveedor LLM

`--provider` acepta `anthropic`, `deepseek`, `openai` u `ollama`. El modelo se
resuelve desde la configuración específica del proveedor; no existe una flag
`--model`.

## Operación VFS

`install-vfs-bridge` instala el plugin y `vfs-health` ejecuta la sonda
worker+nieto. Ambos requieren parámetros y precondiciones adicionales
documentados en [CLI de referencia](../api/cli_ref.md) y
[validación real](../operations/real_rig_validation.md).

## Salida y diagnóstico

La CLI configura logging antes de ejecutar el modo. Usar `Ctrl+C` para que
`AppContext.stop()` tenga oportunidad de cerrar recursos. Si una ejecución
falla, conservar el `correlation_id` y consultar
[troubleshooting](troubleshooting.md).
