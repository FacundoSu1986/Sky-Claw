# Referencia de CLI

> **Audiencia:** usuarios técnicos y desarrolladores.
>
> **Estado:** Implementado.
>
> **Fuente canónica:** `sky_claw/__main__.py::_parse_args`.
>
> **Última verificación:** 2026-10-10 sobre `origin/main` `647d246` más este cambio.

## Sintaxis

```text
python -m sky_claw [opciones] [command]
```

`command` es opcional y posicional. El modo por defecto desde fuente es `cli`;
el ejecutable congelado cambia el default a `gui`. Los `(default: X)` que imprime
`--help` se derivan del default efectivo de cada contexto (`%(default)s`), así que
el congelado declara `gui` y `--provider` declara el proveedor configurado; lo
ancla `tests/test_cli_help_defaults.py`.

## Opciones

| Opción | Valores o default | Uso |
|---|---|---|
| `--mode` | `cli`, `telegram`, `oneshot`, `gui`, `security`, `install-vfs-bridge`, `vfs-health`, `vfs-quarantine`, `bodyslide-recovery` | Selecciona el modo |
| `--provider` | `anthropic`, `deepseek`, `openai`, `ollama`; default: config o `deepseek` | Proveedor LLM |
| `--mo2-root` | config o `C:\MO2Portable` | Instancia portable |
| `--skyrim-path` | config o vacío | Instalación; obligatoria para `vfs-health` |
| `--profile` | `MO2_PROFILE` o `Default` | Perfil MO2 de **toda** la sesión |
| `--vfs-profile` | `Default` | Perfil de la sonda `vfs-health` únicamente |
| `--vfs-timeout` | `30.0` segundos | Timeout del worker VFS |
| `--vfs-quarantine-action` | `status` (default) o `release` | Solo `vfs-quarantine`: informar o liberar la cuarentena |
| `--vfs-evidence` | vacío | Solo `vfs-quarantine release`: qué verificaste; es obligatoria y queda en el log como `CRITICAL` |
| `--bodyslide-recovery-action` | `status` (default), `release-quarantine` o `confirm-terminal` | Solo `bodyslide-recovery`: ver el estado, liberar la cuarentena del recurso o confirmar que una corrida lanzada terminó |
| `--bodyslide-evidence` | vacío | Solo `bodyslide-recovery release-quarantine` y `confirm-terminal`: qué verificaste; es obligatoria y queda en el log como `CRITICAL` |
| `--bodyslide-exe` | config (`bodyslide_exe`) o vacío | Solo `bodyslide-recovery`: el ejecutable de la instalación de BodySlide |
| `--db-path` | `mod_registry.db` | Registry principal |
| `--loot-exe` | config o `loot.exe` | LOOT CLI |
| `--webhook-host` | `127.0.0.1` | Parámetro conservado para modo Telegram |
| `--webhook-port` | `8080` | Parámetro conservado para modo Telegram |
| `--operator-chat-id` | config o vacío | Operador HITL autorizado |
| `--staging-dir` | `<base>\MO2Portable\downloads` | Staging de descargas |
| `--xedit-exe` | config o vacío | SSEEdit |
| `--install-dir` | `<base>\Modding` | Instalación automática de tools |
| `-v`, `--verbose` | desactivado | Logging DEBUG |

La descripción de un argumento no garantiza que todos los modos lo consuman.
Seguir la rama correspondiente de `_async_main()` para confirmar impacto.

`--profile` y `--vfs-profile` no son intercambiables: el primero fija el perfil MO2
con el que trabaja la sesión entera —tools del agente, rituales de la GUI, LOOT,
DynDOLOD, Pandora, Wrye Bash, Synthesis y la evaluación de `fileDependency` de
FOMOD—; el segundo solo elige el perfil que sondea `--mode vfs-health`.

El perfil queda **fijo durante la sesión**: no se puede cambiar desde el chat ni
desde la GUI, y el agente no lo acepta como argumento de sus herramientas. Cambiarlo
requiere reiniciar Sky-Claw con otro `--profile` (o con otro `MO2_PROFILE`). Un
nombre con separadores de ruta o metacaracteres aborta el arranque antes de tocar el
filesystem.

## Precondiciones especiales

`install-vfs-bridge` falla fuera de Windows. En producción exige un ejecutable
congelado; `SKYCLAW_ALLOW_DEV_WORKER=1` sólo habilita la vía de desarrollo.

`vfs-health` exige `ModOrganizer.exe`, un `Data` válido, perfil explícito y un
bridge disponible. El éxito de la sonda debe incluir la atestación del canary.

`vfs-quarantine` no arranca el daemon (arrancarlo tomaría el lock de la instancia que este modo necesita ver
libre). La instancia sale de `--mo2-root`, igual que `install-vfs-bridge`; `status` además lista las
instancias en cuarentena que encuentre bajo `vfs_bridge`. `release` exige `--vfs-evidence`, se niega si hay un
broker vivo dueño del lock (hay que cerrar Sky-Claw antes) y limpia las tres formas persistidas de la
cuarentena —marcador, lock envenenado y lock huérfano—, porque borrar solo el marcador haría que el próximo
arranque reclame el lock huérfano y vuelva a aislar la instancia. Un rechazo (sin evidencia, dueño vivo o error de disco) no es un fallo del proceso: sale con código `1` y el motivo en el log, sin traceback, y la instancia sigue aislada. Ancla: `tests/test_vfs_quarantine.py`.

`bodyslide-recovery` tampoco arranca el daemon: arrancarlo tomaría el recurso `bodyslide-meshes` que el operador
está liberando. Cerrá Sky-Claw antes. Tres acciones:

- `status` muestra los journals de BodySlide y si el recurso está en cuarentena, sin escribir nada.
- `release-quarantine` retira la cuarentena del recurso (la escribe una siembra que se colgó).
- `confirm-terminal` lleva el journal `LANZADO` de la instalación de `--bodyslide-exe` a `TERMINAL`: es la
  salida cuando el broker no pudo probar que BodySlide terminó. Exige además la misma máquina, la misma
  carpeta física y que el dueño de la corrida no esté vivo.

Las dos últimas exigen `--bodyslide-evidence`. Ninguna restaura la configuración de BodySlide: la restaura
el próximo arranque, si los archivos siguen siendo los que dejó la corrida. Un rechazo sale con código `1`
y el motivo en el log, sin cambiar nada. Anclas: `tests/test_bodyslide_operador.py` y
`tests/test_bodyslide_terminalidad_operador.py`.
