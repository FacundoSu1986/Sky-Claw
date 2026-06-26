# Diseño: Búsqueda de mods en Nexus por lenguaje natural

**Fecha:** 2026-06-25
**Estado:** Aprobado (brainstorming) — pendiente plan de implementación

## Context

Hoy la IA de Sky-Claw solo puede traer un mod de Nexus si el usuario le pasa el **Nexus ID numérico** a mano (`download_mod(nexus_id, file_id)`). Eso no le ahorra trabajo: el usuario tiene que ir a la web, buscar el mod, copiar el número. El objetivo es que pueda pedir en lenguaje natural —*"un mod de armaduras con más de 500 descargas"*— y que la IA lo encuentre y se lo proponga.

**Constraint duro:** todo acceso a Nexus va por la **API oficial** (`scraper/scraper_agent.py::_api_request`); el scraping fue descartado permanentemente por ToS (`scraper/nexus.py` es un stub). La API oficial **no tiene búsqueda full-text del catálogo** — solo browse (trending/latest/updated) y fetch-by-ID. Por eso el descubrimiento real necesita un **buscador web** que apunte a `nexusmods.com` y después enriquecer cada candidato con la API oficial (esto respeta ToS: no se scrapea Nexus, se lo consulta por su API).

Decisión del usuario (brainstorming): sumar un buscador web con API key, priorizando **la solución más simple que más trabajo le ahorre**.

## Goal / Non-goals

**Goal:** una tool `search_nexus` que, dado texto libre, devuelva una lista corta de mods reales de Nexus (con nombre, descargas, categoría, ID y URL) lista para que el usuario elija.

**Non-goals (YAGNI):**
- No descarga nada por sí sola — eso sigue siendo `download_mod` (con HITL obligatorio).
- No parsea el lenguaje natural con regex propios — eso lo hace el LLM (que ya es bueno en eso).
- No soporta otros juegos en esta fase (scope Skyrim SE, igual que el `_api_request` actual).
- No construye un índice local del catálogo de Nexus.

## Approach

**LLM-orquestado.** El LLM traduce la intención del usuario a parámetros estructurados y llama a la tool; la tool hace el trabajo determinístico (buscar, extraer IDs, enriquecer, filtrar) y devuelve JSON. El LLM narra el resultado.

## Componente: tool `search_nexus`

Nueva función en `sky_claw/antigravity/agent/tools/nexus_tools.py`, registrada en `AsyncToolRegistry._register_builtins()` como las demás (`ToolDescriptor` + `params_model` Pydantic), con schema `SearchNexusParams` en `tools/schemas.py`.

**Parámetros (mínimos a propósito):**
- `query: str` (requerido) — texto de búsqueda, ej. `"armor mod"`.
- `min_downloads: int | None` (opcional) — filtro por descargas, ej. `500`.
- `limit: int = 5` (opcional, cap a 10) — cuántos resultados devolver.

**Comportamiento:**
1. **Atajo URL/ID:** si `query` contiene una URL de Nexus o un ID, extrae el ID directo y salta el buscador web (resuelve nombre/URL→metadata sin gastar una búsqueda).
2. Si no, llama al **buscador web** (Brave Search API) con `site:nexusmods.com/skyrimspecialedition <query>`.
3. Extrae los `mod_id` de las URLs de resultado (`.../skyrimspecialedition/mods/<id>`).
4. Para los primeros N candidatos, hace fetch de metadata vía la **API oficial de Nexus** (reusa el path `_api_request`/downloader ya existente): nombre, `mod_downloads`, `category`, `summary`, `endorsement_count`.
5. Filtra por `min_downloads` si vino, ordena por descargas desc, corta a `limit`.
6. **Sanitiza** todo el texto que cruza al LLM con `sanitize_for_prompt` (títulos/summaries de Nexus + snippets de Brave).
7. Devuelve JSON: `[{nexus_id, name, downloads, category, summary, url}]`.

## Seguridad (patrones ya existentes en el repo)

- **Egress solo vía `NetworkGateway`**: el request a Brave usa `GatewayTCPConnector` y la allow-list incluye `api.search.brave.com`. Mismo Zero-Trust que `download_mod`/`setup_tools` (gateway=None → abort).
- **Sanitización anti prompt-injection** obligatoria de todo texto externo (un mod author puede meter `[INST]ignore previous[/INST]` en el título; ya se hace en `download_mod`).
- **Read-only**: la tool no toca filesystem ni descarga; no requiere HITL. La descarga posterior sí pasa por el HITL de `download_mod`.

## Manejo de errores / degradación

(Simple a propósito — sin endpoints nuevos: el enriquecimiento reusa el `query_nexus`/`_api_request` ya existente; lo único externo nuevo es Brave.)

- **Sin `search_api_key`** configurada → devuelve un JSON con un mensaje claro guiando a configurarla (no crashea, no rompe el chat). El atajo URL/ID sigue funcionando sin key.
- **Brave caído / rate-limit / 0 resultados** → devuelve un mensaje claro ("no pude buscar ahora / sin resultados, probá otros términos o pasame la URL"). No se implementan endpoints de browse en esta fase (queda como mejora futura).
- **ID no resoluble** (404 en la API oficial) → se omite ese candidato y se sigue con los demás.

## Configuración

- Nuevo secreto opcional `search_api_key` en el keyring (`keyring.set_password("sky_claw", "search_api_key", ...)`), agregado a la lista de `sensitive_keys` de `config.py` y como campo opcional en el setup wizard.
- Brave Search API: tier gratuito (~2000 consultas/mes), key gratis. Si el usuario no la configura, la feature degrada con el mensaje guía.

## Testing

- `search_nexus` con **Brave API y Nexus API mockeadas**:
  - extracción de `mod_id` desde URLs de resultados,
  - filtrado por `min_downloads` y orden por descargas,
  - sanitización de un título malicioso (`[INST]...`),
  - atajo URL/ID (no llama al buscador),
  - degradación sin `search_api_key` y con Brave caído (mensaje claro, sin crash),
  - egress rechazado si `gateway=None`.
- Registro/ACL: la tool aparece en `tool_schemas()` y respeta el allowlist `allowed_tools`.

## Out of scope / futuro

- Multi-juego (más allá de SE).
- Otros providers de búsqueda (Google CSE, Bing) — Brave es el default; abstraerlo si hace falta.
- Auto-descarga del top result (se mantiene la elección humana + HITL).
- Fallback a endpoints de browse de la API (trending/latest) cuando Brave no está disponible — requiere implementar esos endpoints en `scraper_agent`; se difiere.
