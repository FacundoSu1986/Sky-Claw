# Referencia de configuración

> **Audiencia:** desarrolladores y operadores.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `sky_claw/config.py`,
> `sky_claw/local/local_config.py` y
> `sky_claw/app/core/path_resolver.py`.
>
> **Última verificación:** 2026-08-08 sobre `fix/dyndolod-cli-contrato-asistida`
> `e8427063`.

## `Config`

`Config` mantiene la configuración global en `~/.sky_claw/config.toml`. Sus
defaults verificados son:

| Categoría | Claves |
|---|---|
| Rutas | `mo2_root`, `install_dir`, `loot_exe`, `xedit_exe`, `pandora_exe`, `bodyslide_exe`, `skyrim_path` |
| Proveedor | `llm_provider`, `llm_model`, `{provider}_model` |
| Secretos | `llm_api_key`, `{provider}_api_key`, `nexus_api_key`, `search_api_key`, `telegram_bot_token` |
| Telegram | `telegram_chat_id` |
| Setup | `first_run` |

`as_dict` devuelve una copia. `save()` extrae secretos y los persiste en el
keyring `sky_claw`; no vuelve a texto plano si falla el backend.

### Claves administradas, fuera de los defaults

`Config.save()` relee el TOML y aplica sólo las claves reasignadas o borradas desde el
baseline del objeto. Por eso una clave fuera de `_load_defaults()` se persiste cuando un
instalador la escribe explícitamente en `_data`; no se serializa indiscriminadamente todo
el estado en memoria. Los instaladores que producen mods sin ejecutable propio guardan
así la lista de nombres que el orquestador necesita para activarlos:

| Clave | Escrita por |
|---|---|
| `ngio_mods` | `ensure_ngio` |
| `community_shaders_mods` | `ensure_community_shaders`, desde ambas superficies |

No aparecen en la tabla de defaults porque no existen hasta que la operación termina. En
`LocalConfig` sí son campos declarados de la dataclass, con la misma semántica.

### Escribir y persistir

`sky_claw/local/local_config.py` expone la frontera que abstrae las dos clases:
`escribir_campo` para setear y `guardar_config` / `persistir_campo` para persistir, más
sus variantes bloqueantes que serializan las escrituras concurrentes sobre el mismo
archivo. `Config.__getattr__` sólo lee de `_data`: un `setattr` crudo crea un atributo que
ningún `save` mira. No usar `load` / `save` de ese módulo directamente — son JSON-only y
reescriben el archivo entero.

## `SystemPaths`

Resuelve el drive base entre Windows y WSL, convierte rutas Windows y define
`modding_root()`. No implica que el pipeline completo sea soportado bajo WSL.

## `LocalConfig`

`sky_claw/local/local_config.py` representa configuración local de MO2 y tools.
Debe contrastarse con ese modelo antes de agregar una clave: no asumir que una
clave global de `Config` existe también en `LocalConfig`.

## `PathResolver`

Es la frontera para resolver y validar rutas externas. Lee las variables
enumeradas en [configuración de usuario](../user/configuration.md) y aplica
validación antes de devolver paths. Los callers no deben inventar aliases.

## Otras variables operativas

- `SKY_CLAW_DEV_NO_AUTH`: bypass sólo en modo de desarrollo del servidor web.
- `SKYCLAW_ALLOW_DEV_WORKER`: habilita worker VFS no congelado para pruebas.
- `SKYCLAW_METRICS_HOST` y `SKYCLAW_METRICS_PORT`: endpoint de métricas.
- `OTEL_EXPORTER_OTLP_ENDPOINT`: collector de tracing.

No tratar estas variables como equivalentes a campos TOML.
