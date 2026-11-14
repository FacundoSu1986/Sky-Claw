# Uso de la CLI

> **Audiencia:** usuarios técnicos y operadores.
>
> **Estado:** Implementado.
>
> **Fuente canónica:** `sky_claw/__main__.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e` más este cambio.

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

Si el broker perdió la certeza de que un proceso terminó, deja la instancia en
**cuarentena** y la mantiene entre reinicios. `vfs-quarantine` muestra por qué
(`--vfs-quarantine-action status`, el valor por defecto) y la levanta con
`--vfs-quarantine-action release --vfs-evidence "<qué verificaste>"`, solo con
Sky-Claw cerrado. Detalle en [CLI de referencia](../api/cli_ref.md).

Cerrar MO2 a mitad de una corrida también puede dejar la instancia en cuarentena: el bridge avisa de la salida del
worker durante el cierre, pero si el aviso no llega Sky-Claw no puede probar que el proceso terminó. Es lo esperado y se
libera con el mismo comando, una vez que verificaste que no quedan procesos de la herramienta.

## Salida y diagnóstico

En el REPL (`--mode cli`), escribir `exit`, `quit` o `salir` cierra la sesión;
`Ctrl+C` y `Ctrl+D` también terminan el loop. Los comandos se comparan por
igualdad exacta sobre el texto recortado (con `/` inicial opcional y sin
distinguir mayúsculas), así que una frase como `salir del juego` se envía al
agente en vez de cortar la sesión.

La CLI configura logging antes de ejecutar el modo. Usar `Ctrl+C` para que
`AppContext.stop()` tenga oportunidad de cerrar recursos. Si una ejecución
falla, conservar el `correlation_id` y consultar
[troubleshooting](troubleshooting.md).
