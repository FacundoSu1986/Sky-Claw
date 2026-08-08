# AGENTS.md — guía para agentes en este repo

Fuente canónica de instrucciones para cualquier agente (Claude Code, Codex, Copilot…).
`CLAUDE.md` solo importa este archivo. El detalle de invariantes y patrones de código está
en [.github/coding_conventions.md](.github/coding_conventions.md).

Para descubrir documentación por audiencia y conocer qué fuente domina ante
drift, leer [docs/README.md](docs/README.md) y
[docs/documentation/source_of_truth.md](docs/documentation/source_of_truth.md).
Los `AGENTS.md` de subárbol son punteros de alcance y no reemplazan esta guía.

## Dominio

Gestión de mods de Skyrim SE/AE vía Mod Organizer 2 (LOOT, xEdit, DynDOLOD…).

## Convenciones

- **Tests y comentarios de código en español** (convención del repo).
- **TDD**: test que falla (rojo) → implementación → verde.
- Entorno: venv del repo vía `uv sync --extra dev`. Correr tests:
  - Windows: `.venv/Scripts/python -m pytest`
  - POSIX: `.venv/bin/python -m pytest`

  (`asyncio_mode=auto`: los tests `async def` no necesitan decorador.)
- Lint/format/types — el gate "Lint" de CI exige **ambos** comandos de ruff:
  `ruff check sky_claw/ tests/` **y** `ruff format --check sky_claw/ tests/`.
  `mypy sky_claw/` corre bloqueante **pero no cubre todo**: `pyproject.toml`
  tiene `ignore_errors = true` para ~30 módulos, incluidos `sky_claw.app.gui.*`,
  `sky_claw.app.web.*` y `sky_claw.local.tools.*`. Lo mismo `BLE001` de ruff,
  exento en todo `sky_claw/app/**`. Antes de confiar en que un gate te cubre,
  verificá que tu archivo no esté en la lista de exentos — si lo está, ese
  código sale a producción sin type-check.
- Una rama + un PR por cambio; no commitear directo a `main`. El revisor
  automático (`qodo-merge-adversarial.yml`) corre **solo** en `pull_request`:
  un push directo saltea el único control que empíricamente ataja defectos acá.
- **Al cerrar una tarea del backlog** (`TECHNICAL_REVIEW_TASKS.md`, T-XX):
  actualizar `docs/pending_ooda_status.md` en el mismo PR (o dejar constancia
  explícita si el cierre es parcial/cubre solo un runner). El título del PR
  declarando "cerrado" **no alcanza**.

## La regla que más se viola: arreglar un hermano y no al otro

Es **la** clase de defecto dominante del repo: 13 de 21 follow-ups auditados son
un fix que aterrizó en un camino y dejó intacto a su gemelo. No es descuido de
gente distraída — pasa leyendo este archivo, y el que lo ataja es siempre un bot.

Aplica **a todo cambio**, no solo al cierre de una T-XX.

Las dos formas concretas que toma acá:

1. **Dos superficies, un recurso.** La misma operación mutante se alcanza desde
   la GUI (`SupervisorAgent` → `tool_dispatcher`) *y* desde el agente LLM
   (`AsyncToolRegistry` → `LLMRouter` → Telegram / `/api/chat`). 9 de los 13
   episodios son exactamente esto: lock, journal, preflight o sandbox cableados
   en un path y ausentes en el otro (#166→#167, #171→#172, #213→#215→#217,
   #243→#247).
2. **Hermano en el mismo archivo.** #373: `run_ritual` recibió el scoping del
   dueño de la aprobación HITL y `run_ritual_install` —90 líneas más abajo,
   misma estructura, y la aprobación *más* sensible porque es egress de red—
   quedó sin él.

**Enunciala como propiedad del mecanismo, no como recordatorio de proceso.** Es
la diferencia medible entre los episodios que fallaron y los que salieron bien de
entrada: *"el lock cross-process solo protege si TODOS los mutadores participan"*
(#316, #324) cubrió ambos paths sin que lo pidiera un revisor. *"verificar el
árbol de callers"* no.

**Y anclala con un test que enumere, no que muestree.** Un caso escrito a mano
para el hermano que te faltó no ataja al tercero. El repo ya tiene el
instrumento y funciona:

- `tests/test_ritual_dispatch.py` → `assert RITUAL_TOOL_MAP == {...}` (igualdad
  literal del dict: agregar un ritual sin cablearlo rompe el test).
- `tests/test_hitl_client_scoping.py` → la familia de lanzadores se detecta por
  introspección y se congela; un lanzador nuevo rompe el ancla hasta que se le
  escribe su receta, y la receta lo mete en los tests de comportamiento.
- `tests/test_db_connection_invariant.py` → el conjunto de módulos que abren
  conexiones SQLite por su cuenta se detecta por AST y se congela.

Escribir el racional de por qué excluís una rama **no cuenta como verificarla**:
en #318 y #373 el autor enumeró, escribió el párrafo justificando el recorte, y
el revisor lo revirtió igual.

> Contexto de por qué esta sección existe y es tan explícita: ~94% de los ítems
> normativos de este corpus no tienen gate que los haga fallar. Las reglas que el
> repo sí cumple (ruff, mypy, contrato `success`/`message`) comparten que nombran
> un comando o rompen un test. Si agregás una regla acá, traé con qué se verifica
> — o va a envejecer como las demás.

## Antes de tocar el pipeline de modding

Leer [`sky_claw/local/AGENTS.md`](sky_claw/local/AGENTS.md) — **SOP del pipeline de
modding de Skyrim** (orden de stages, reglas por tool, failure modes) — antes de
modificar `sky_claw/local/tools/`, `sky_claw/local/xedit/` o
`sky_claw/app/orchestrator/tool_strategies/`.

## Contratos vigentes

**Resultado de tools.** Todo tool nuevo emite `success: bool` + `message: str` (canónico,
vacío en éxito) además de sus campos estructurados. `normalize_tool_result`
(`sky_claw/local/tools/tool_result.py`) es la única pieza que conoce las claves legacy
(`details`/`error`/`logs`/`stderr`/`errors`/`reason`); "error desconocido" solo puede
originarse en su fallback. Tests ancla: `tests/test_tool_result.py` (shapes legacy reales)
y `tests/test_tool_result_contract.py` (retorno de error por servicio). *Historia: cada
servicio reportaba errores bajo claves distintas y el summarizer adivinaba — parcheado dos
veces (#214, #216) antes del fix de raíz.*

**Capa del agente LLM**: política base lock-only, **sin middleware HITL
general** (decisión documentada en #217). Los handlers que reciben un
`HITLGuard` explícito, como `download_mod`, conservan su aprobación específica.

**Persistencia de config.** Hay **dos** clases de config y no son
intercambiables: `Config` (`sky_claw/config.py`, TOML, estado en `_data`, la que
inyecta `AppContext` y a la que apunta `AppContext.config_path`) y `LocalConfig`
(dataclass, JSON, formato legacy). Escribí campos con `escribir_campo` y
persistí con `guardar_config`/`persistir_campo`
(`sky_claw/local/local_config.py`) — nunca con `load`/`save` crudos, que son
JSON-only. `Config.__getattr__` solo lee de `_data`, así que un
`local_cfg.campo = valor` crudo crea un atributo que funciona en la sesión y que
ningún `save` mira: se pierde al reiniciar, sin error. Anclas (todas en
`tests/test_local_config_persistencia.py`, por AST y por igualdad literal):
quién puede importar `load`/`save` crudos —hoy solo `app_context.py`, y solo
`load`, para la migración legacy—, cuántas asignaciones crudas sobre
`local_cfg` hay por módulo —hoy cero—, y la frontera exacta de lo que
serializa la sección de carrera GUI↔agente (ver debajo). *Historia: el mismo
defecto en las dos superficies del PR #442 (la GUI reescribía `config.toml`
con JSON de defaults y el agente escribía un atributo muerto sobre `Config`)
más cuatro hermanos en `app_context.py` que hacían que la autodetección
zero-config nunca persistiera; los tests pasaban porque los fixtures usaban
`LocalConfig` y un `.json` de conveniencia.*

Ambos `save` (TOML y JSON) escriben **atómicamente** (temporal + `os.replace`
en el mismo directorio que el destino): un fallo a mitad de la serialización
ya no puede truncar la config existente a 0 bytes. `persistir_campo_bloqueante`
(GUI) y `guardar_config_bloqueante` (agente) además serializan con un lock
compartido de proceso (`sky_claw/local/local_config.py`,
`_obtener_lock_de_escritura`) — usalos en vez de las variantes sync + `to_thread`
crudo desde cualquier código nuevo que persista sobre el mismo `config.toml`.
El lock evita que dos escrituras concurrentes intercalen sus `os.replace`
(archivo corrupto). Desde **F1**, el contenido tampoco se pierde:
`Config.save()` hace merge-on-save — relee el disco bajo un threading.Lock por
path y aplica solo las generaciones que cambiaron contra su baseline (snapshot
sincronizado al construir / tras cada save), así que un objeto `Config` de vida
larga ya no puede pisar un campo que otro camino escribió con lectura fresca.
Para valores mutables, la intención se expresa reasignando la clave completa
(`cfg._data[campo] = valor` / `escribir_campo`): mutar una lista o dict *in-place*
no avanza su generación y no se persiste. Los dos
locks no son redundantes: el threading.Lock por path dentro de `Config.save()`
es el coordinador del ARCHIVO — toda mutación del TOML pasa por ahí con
lectura fresca, sea `persistir_campo` o un `.save()` directo que se saltea el
lock asyncio — y el lock asyncio además serializa los ciclos de los wrappers
(ambos locks se anclan con el interleaving "GUI lee → agente escribe → GUI
reemplaza" en los tests de carrera). Los borrados
(secretos, secciones legacy) van DESPUÉS del merge para no resucitar. Anclas:
tests de la sección "Carrera GUI ↔ agente LLM" en
`tests/test_local_config_persistencia.py` (cierre paramétrico sobre campos que
SÍ vienen de `_load_defaults()` + ancla AST de serializadores TOML) y
`tests/test_config_secretos_sin_keyring.py`. Todo escritor nuevo de config
participa por construcción porque el merge vive en `Config.save()`, no en un
wrapper — si agregás un serializador que escriba el TOML por fuera, el ancla
AST se rompe a propósito: decidí si participa del merge o es una exención.

**Secretos en `Config.save()`.** Fail-closed en las **dos** direcciones: no baja
plaintext nuevo al TOML si el keyring falla, y tampoco borra el secreto que ya
estaba en el archivo cuando no lo pudo mover (perder la key es tan malo como
filtrarla, y no hay backup ni escritura atómica). Sin una escritura explícita,
el valor vivo del keyring prevalece sobre memoria o plaintext obsoletos; una
escritura rechazada queda pendiente para reintento. Ancla:
`tests/test_config_secretos_sin_keyring.py`.

## Pendientes conocidos

Ver [`docs/pending_ooda_status.md`](docs/pending_ooda_status.md) para el inventario
completo verificado contra el código (reemplaza mantener la lista acá, que se
desactualiza igual que cualquier otro doc estático). El pendiente de QuickAutoClean
—transversal a toda tarea que toque `local/tools/xedit_service.py`— vive en
[`sky_claw/local/tools/AGENTS.md`](sky_claw/local/tools/AGENTS.md), que carga al
trabajar ahí.
