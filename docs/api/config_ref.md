# Referencia de configuración

> **Audiencia:** desarrolladores y operadores.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `sky_claw/config.py`,
> `sky_claw/local/local_config.py` y
> `sky_claw/antigravity/core/path_resolver.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

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
