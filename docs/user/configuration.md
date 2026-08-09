# Configuración

> **Audiencia:** usuarios y operadores.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `sky_claw/config.py`,
> `sky_claw/app/core/path_resolver.py` y
> `local_scripts/scripts/first_run.py`.
>
> **Última verificación:** 2026-08-09 sobre `origin/main` `272d4953`.

## Archivo y precedencia

`Config` carga valores por este orden:

1. defaults del código;
2. `~/.sky_claw/config.toml`;
3. secretos recuperados del keyring del sistema.

El TOML admite claves planas y compatibilidad para las secciones `[telegram]`,
`[nexus]` y `[paths]`. Las claves planas explícitas prevalecen sobre el valor
extraído de esas secciones.

## Campos no secretos

| Grupo | Claves |
|---|---|
| Instalaciones | `mo2_root`, `skyrim_path`, `install_dir` |
| Herramientas | `loot_exe`, `xedit_exe`, `pandora_exe`, `bodyslide_exe` |
| LLM | `llm_provider`, `anthropic_model`, `deepseek_model`, `openai_model`, `ollama_model` |
| Telegram | `telegram_chat_id` |
| Estado | `first_run` |

`llm_model` es una clave legacy: se copia en memoria al campo del proveedor
activo cuando éste está vacío; el archivo no se reescribe por esa migración.

## Estado administrado

Estas claves las escribe Sky-Claw al terminar una instalación y no deben editarse a
mano. No son defaults del código: aparecen en el archivo recién cuando la operación que
las produce se completa.

| Clave | Significado |
|---|---|
| `ngio_mods` | Nombres de los mods MO2 instalados para el precache de grass |
| `community_shaders_mods` | Nombres de los mods MO2 instalados por la operación de [Community Shaders](community_shaders.md) |

Ambas existen porque esos componentes no tienen ejecutable propio: el escaneo no puede
deducir qué instaló Sky-Claw, así que se deja constancia del nombre exacto de cada
directorio bajo `mods/`.

Son **un registro, no un disparador**. En el árbol actual ninguna ruta de producción
lee estas claves: la activación en `modlist.txt` la sigue haciendo el operador desde
MO2. Los comentarios del código las describen como insumo de una activación
automática; eso es intención de diseño, no comportamiento vigente. Escribirlas a mano
no activa nada, y que falten no impide activar los mods manualmente.

## Secretos

`Config` usa el servicio `sky_claw` del keyring. Incluye claves LLM, Nexus,
búsqueda, Telegram y autenticación WebSocket. Si detecta un secreto en TOML,
intenta migrarlo al keyring y guardar el archivo sin el valor. Si el keyring
falla, no debe usarse un TOML como fallback de texto plano.

No publicar capturas, logs o ejemplos con tokens reales.

## Variables de rutas

`PathResolver` centraliza `SKYRIM_PATH`, `MO2_PATH`, `MO2_MODS_PATH`,
`MO2_PROFILE`, `XEDIT_PATH`, `LOOT_EXE`, `DYNDLOD_EXE`, `TEXGEN_EXE`,
`SYNTHESIS_EXE`, `WRYE_BASH_PATH` y `PANDORA_EXE`. La GUI puede completar
variables no secretas después de su escaneo.

Las flags de CLI son otra entrada de configuración. Consultar
[la referencia de CLI](../api/cli_ref.md).

## Criterio de parada

No ejecutar un ritual si una ruta no existe, apunta a otra instancia, el
perfil activo no coincide o el keyring no puede entregar la credencial
necesaria.
