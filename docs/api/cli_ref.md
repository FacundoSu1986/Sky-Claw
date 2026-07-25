# Referencia de CLI

> **Audiencia:** usuarios técnicos y desarrolladores.
>
> **Estado:** Implementado.
>
> **Fuente canónica:** `sky_claw/__main__.py::_parse_args`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Sintaxis

```text
python -m sky_claw [opciones] [command]
```

`command` es opcional y posicional. El modo por defecto desde fuente es `cli`;
el ejecutable congelado cambia el default a `gui`.

## Opciones

| Opción | Valores o default | Uso |
|---|---|---|
| `--mode` | `cli`, `telegram`, `oneshot`, `gui`, `security`, `install-vfs-bridge`, `vfs-health` | Selecciona el modo |
| `--provider` | `anthropic`, `deepseek`, `openai`, `ollama` | Proveedor LLM |
| `--mo2-root` | config o `C:\MO2Portable` | Instancia portable |
| `--skyrim-path` | config o vacío | Instalación; obligatoria para `vfs-health` |
| `--vfs-profile` | `Default` | Perfil de la sonda |
| `--vfs-timeout` | `30.0` segundos | Timeout del worker VFS |
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

## Precondiciones especiales

`install-vfs-bridge` falla fuera de Windows. En producción exige un ejecutable
congelado; `SKYCLAW_ALLOW_DEV_WORKER=1` sólo habilita la vía de desarrollo.

`vfs-health` exige `ModOrganizer.exe`, un `Data` válido, perfil explícito y un
bridge disponible. El éxito de la sonda debe incluir la atestación del canary.
