# Sky-Claw — Deployment & Operations Runbook

Operational guide for deploying, running and recovering Sky-Claw. For the
quick-start install flow see [QUICKSTART.md](QUICKSTART.md); this document
covers the production/operations gap: configuration, secrets, observability,
failure handling and the pre-flight checklist for a real end-to-end run.

> **Estado:** release-candidate, no GA. Los cimientos (locks, rollback, redacción,
> SSRF, HITL) son de grado producción; lo que sigue abierto está en
> [Limitaciones conocidas](#9-limitaciones-conocidas).

---

## 1. Requisitos

| Componente | Versión / nota |
|---|---|
| Python | **3.11 – 3.12** (lo que valida CI; `pyproject.toml` exige `>=3.11`). El entorno de desarrollo corre 3.11.15. ⚠️ `QUICKSTART.md` dice "3.14+" — **es inexacto**, usar 3.11/3.12. |
| OS | Windows 10/11 (target primario; `file_permissions.py` usa DACL de Windows). Linux/WSL2 corre el core async pero no es la plataforma de entrega. |
| MO2 | Mod Organizer 2 instalado y configurado para Skyrim Special Edition. |
| Node.js | Solo para el modo Telegram (gateway en `sky_claw/antigravity/comms/telegram_gateway_node/`). |
| Red | Salida a Nexus / proveedor LLM. El egress está restringido por allowlist (`config.py:ALLOWED_HOSTS`). |

---

## 2. Instalación y empaquetado

### Ejecución desde fuente (desarrollo / operador)
```bat
build.bat              :: crea venv\ e instala dependencias
```
Lockfiles reproducibles: `requirements.lock` (pip) y `uv.lock` (uv). Para un
entorno bloqueado: `uv sync --locked --extra dev`.

### Binario empaquetado (.exe)
`build.bat` construye el ejecutable vía PyInstaller (`sky_claw.spec`). El binario
arranca en **modo GUI por defecto** (`__main__.py` fija `mode=gui` cuando
`sys.frozen`).

> ⚠️ **Pendiente de release:** no hay tag de versión (CHANGELOG está en
> `[Unreleased]`) ni binario firmado/validado. Ver [Limitaciones](#9-limitaciones-conocidas).

---

## 3. Configuración

La config se carga desde **`~/.sky_claw/config.toml`** (`config.py:58-59`,
`DEFAULT_CONFIG_DIR = Path.home() / ".sky_claw"`). Se permite override por
variables de entorno.

> El asistente interactivo existe en **`local_scripts/scripts/first_run.py`**
> (no en `scripts/first_run.py`, que es la ruta equivocada que indica
> `QUICKSTART.md`). Corré `python local_scripts/scripts/first_run.py`, o editá
> `~/.sky_claw/config.toml` a mano / usá las env vars de abajo.

### Paths de herramientas (excepción Zero-Trust documentada)
`path_resolver.py` es el **único** punto que lee estas variables de entorno
(ver `ZERO_TRUST_TODO.md`); ningún otro módulo debe leerlas directo. Nombres
reales según `sky_claw/antigravity/core/path_resolver.py`:

| Variable de entorno | Uso |
|---|---|
| `SKYRIM_PATH` | Raíz de Skyrim SE (**requerida** para el chain de herramientas) |
| `MO2_PATH` | Instalación de Mod Organizer 2 |
| `MO2_MODS_PATH` / `MO2_PROFILE` | Carpeta de mods / perfil activo de MO2 |
| `XEDIT_PATH` | Ejecutable de xEdit/SSEEdit (**requerida** para dry-run/preview) |
| `LOOT_EXE` | Ejecutable de LOOT |
| `DYNDLOD_EXE` / `TEXGEN_EXE` | DynDOLOD / TexGen |
| `SYNTHESIS_EXE` / `WRYE_BASH_PATH` | Synthesis / Wrye Bash |

Algunos paths también se pueden pasar por **flags de CLI** (distintos de las env
vars, resueltos por argparse, no por `path_resolver`): `--xedit-exe`,
`--install-dir`, `--mo2-root`, `--staging-dir`. Todos los paths pasan por
`PathValidator` (sandbox anti-traversal) antes de cualquier subprocess.

---

## 4. Secretos

**Nunca** en código ni en `config.toml` plano (si aparecen ahí, `Config` los
migra a keyring y los borra del TOML — `config.py:96-102`).

### Secretos de runtime (LLM / Nexus / Telegram) → OS keyring
El path de arranque real: `Config._load_from_keyring()` (`config.py:67-81`) lee
del **keyring del SO** bajo el servicio **`sky_claw`**. `AppContext` construye el
`LLMRouter` con estas claves; **`CredentialVault` NO interviene en este flujo.**

| Clave keyring (`service="sky_claw"`) | Uso |
|---|---|
| `llm_api_key` | API key del proveedor LLM (genérica) |
| `<provider>_api_key` (`anthropic_api_key`, `deepseek_api_key`) | API key específica por proveedor (tiene precedencia) |
| `nexus_api_key` | API de Nexus Mods |
| `telegram_bot_token` | Bot de Telegram |

Cargá cada uno con el wizard (`first_run.py`) o `keyring.set_password("sky_claw", "<clave>", "<valor>")`.

**Proveedores LLM soportados** (`agent/providers.py:create_provider`,
`--provider`, el wizard y el ops-hub web): **`anthropic`, `deepseek`, `openai`,
`ollama`**. El default de `OpenAIProvider` es `gpt-5`; se configura con el campo
`llm_model` (wizard / `config.toml`), que `create_provider` inyecta al provider.
No existe un flag `--model` en la CLI. Si el modelo no está disponible en tu
cuenta, la API devuelve un 4xx claro (se loguea) y elegís otro.

### Token WS — dos flujos distintos
1. **Gateway Node ↔ bridge Python**: el server Node lee `WS_AUTH_TOKEN` de
   `process.env` (`telegram_gateway_node/server.js:98`); el bridge Python lo lee
   de `WS_AUTH_TOKEN` y, si no está, cae a keyring (`frontend_bridge.py:301`).
2. **WS Daemon ↔ NiceGUI (interno)**: `AuthTokenManager` lee/escribe un
   **token-file rotativo** en `~/.sky_claw/tokens/` (`read_token_file(token_dir)`),
   con TTL/rotación. La rotación es del archivo, no del keyring.

### CredentialVault (almacén cifrado, separado)
`sky_claw/antigravity/security/credential_vault.py` es un almacén **cifrado con
Fernet** (clave derivada por PBKDF2 desde un salt por máquina en
`~/.sky_claw/vault_salt.bin` + backup), ciphertext en SQLite. API
`get_secret(name)` / `set_secret(name, value)`. Es una facilidad aparte — **no es
el mecanismo que alimenta LLM/Nexus/Telegram al arranque** (eso es keyring, arriba).

---

## 5. Modos de ejecución

```bash
python -m sky_claw --mode gui                 # interfaz NiceGUI (default del binario)
python -m sky_claw --mode telegram            # bot HITL desde el celular
python -m sky_claw --mode cli                 # terminal interactiva
python -m sky_claw --mode oneshot "<comando>" # ejecución única (command es POSICIONAL)
python -m sky_claw --mode security "<comando>"# utilidades de seguridad
python -m sky_claw --mode cli -v              # -v / --verbose → logging DEBUG
python -m sky_claw --provider anthropic       # anthropic | deepseek | openai | ollama
```
`command` es un argumento **posicional** (`__main__.py:58-62`, `nargs="?"`) — **no
existe `--command`**. El modo Telegram requiere el gateway Node corriendo
(`telegram_gateway_node/`, `npm install` + arranque del server).

---

## 6. Observabilidad — ¿dónde queda registrado si algo falla?

`setup_logging()` (`logging_config.py:208`) se invoca en todos los modos al
arranque. Produce **logs JSON rotativos (10 MB × 5 backups)** en `logs/`, con
`correlation_id` por línea y **redacción de secretos** (API keys, tokens, Bearer,
PII, query-strings) ya aplicada en disco:

| Archivo | Contenido |
|---|---|
| `logs/sky_claw.log` | App principal — **todos los niveles, incluido ERROR**. Es el log a mirar primero. |
| `logs/watcher.log` | Subsistema watcher (`SkyClaw.Watcher`, `propagate=False`). |
| `logs/watcher_security.log` | Eventos de seguridad. |

> Nota: `CorrelationFilter` calcula un `trace_id` de OTEL en el record, pero el
> formatter JSON actual solo emite `correlation_id` (no `trace_id`). Para
> correlacionar con un trace de OTEL hay que agregar `trace_id` al formatter.

Garantías relevantes para un run real:
- **Excepciones no manejadas del event loop (modos no-GUI)**:
  `_install_loop_exception_handler()` (`__main__.py:129`) se instala en la rama
  `asyncio.run` de `_main()` — es decir **cli / oneshot / telegram / security**.
  Enruta a `logger.error(..., exc_info=exc)`; pasar la **instancia** de excepción
  sí adjunta el traceback (logging la convierte a `(type, exc, exc.__traceback__)`),
  así que la tarea fire-and-forget que falle queda en `logs/sky_claw.log` con
  stack y `correlation_id`. **En modo GUI este handler NO se instala** — NiceGUI/
  uvicorn manejan su propio loop; ahí confiá en el log + el handler de uvicorn.
- **Audit trail en SQLite** (aparte de los logs de texto), en
  `sky_claw/antigravity/db/journal.py`: la tabla `journal_entries` registra cada
  operación con estado `started/completed/failed/rolled_back`; la tabla
  `transactions` agrupa con estado `pending/committed/rolled_back`. El registry
  además anota tareas vía `log_tasks_batch`.
- **Métricas/tracing** (opcional): Prometheus (`prometheus-client`) y OpenTelemetry
  (`OTEL_EXPORTER_OTLP_ENDPOINT`) si se configura un collector.

### Diagnóstico rápido de un fallo
```bash
# correr con DEBUG
python -m sky_claw --mode cli -v
# filtrar errores del log JSON
grep '"levelname": "ERROR"' logs/sky_claw.log            # bash/WSL
Select-String '"levelname": "ERROR"' logs\sky_claw.log   # PowerShell
# seguir el hilo de una operación por su correlation_id
grep '<correlation_id>' logs/sky_claw.log
```

---

## 7. Manejo de fallos y recuperación

| Mecanismo | Comportamiento |
|---|---|
| **Rollback automático** | Si una operación falla bajo transacción, `sync_engine` invoca `rollback_manager.undo_last_operation()` y restaura los archivos desde snapshot. No requiere acción manual. |
| **Snapshots de archivos** | `SnapshotTransactionLock` crea snapshots **antes** de mutar el VFS; el rollback restaura en orden inverso. Recuperación manual: `snapshot_manager.restore_snapshot()` (API async, no CLI). |
| **Journal de transacciones** | Filas PENDING huérfanas de una sesión previa se barren al arrancar (`journal.sweep_stale_pending()`); `rollback_transaction()` marca ROLLED_BACK. |
| **Locks distribuidos** | TTL con heartbeat (renovación a TTL/3). Si se pierde el lease mid-operación, `LockLeaseLostError` aborta en salida limpia en vez de competir con otro escritor. |
| **Procesos externos huérfanos** | Los runners (xEdit/DynDOLOD/BodySlide/Pandora/Wrye Bash) hacen `kill()` + reap en timeout — no quedan procesos reteniendo handles del VFS/Data. |
| **Shutdown graceful** | `SIGTERM` se traduce a `KeyboardInterrupt` (`__main__.py:178`) → `AppContext.stop()` corre el cleanup y cancela runners. No matar con `kill -9` salvo último recurso. |
| **Gate HITL** | Operaciones destructivas y descargas desde hosts externos requieren aprobación humana (fail-closed). |

---

## 8. Pre-flight checklist — primer run end-to-end real

Antes de soltar el agente sobre un Skyrim+MO2 real (idealmente en VM o perfil de
MO2 descartable la primera vez):

- [ ] `~/.sky_claw/config.toml` creado; `SKYRIM_PATH` y `XEDIT_PATH` válidos (los exige el chequeo de paths en runtime), `MO2_PATH` dentro del sandbox.
- [ ] Secretos en **keyring** (`service="sky_claw"`): `llm_api_key` o `<provider>_api_key`; `nexus_api_key`; `telegram_bot_token` si usás Telegram. (Cargar en `CredentialVault` NO los expone al arranque.)
- [ ] Proveedor LLM elegido entre los soportados: `anthropic` / `deepseek` / `openai` / `ollama`.
- [ ] Suite local en verde: `pytest -q`.
- [ ] Gates: `ruff check sky_claw/ tests/` y `python -m mypy sky_claw/ --ignore-missing-imports`.
- [ ] `logs/` escribible; correr con `-v` la primera vez.
- [ ] Perfil de MO2 respaldado (el rollback cubre operaciones del agente, pero un backup externo es barato).
- [ ] Validar el chain de preview/dry-run (LOOT→xEdit→DynDOLOD con HITL) **antes** de un run con mutaciones reales.
- [ ] Tras el run: revisar `logs/sky_claw.log` por `ERROR`; en el journal, `transactions` con estado `pending`/`rolled_back` (transacción no confirmada) y `journal_entries` con estado `failed` (operación caída).

---

## 9. Limitaciones conocidas

Honestidad operativa — esto sigue abierto y conviene saberlo antes de producción:

- **Sin validación end-to-end en rig real documentada** — la cobertura es unit/integration (~2050 tests, ~65%).
- **Sin tag de release ni binario firmado/validado** (CHANGELOG `[Unreleased]`).
- **Frontera de tipos parcial** — el override de mypy con `ignore_errors=true` cubre **prácticamente todo `sky_claw.*` / `sky_claw.antigravity.*`**, con re-habilitación puntual de checks en un subconjunto de `core.*` y en `orchestrator.sync_engine`. El grueso del código no está type-checked aún.
- **Loop-exception handler solo en modos no-GUI** — en GUI la captura de excepciones del loop depende de NiceGUI/uvicorn, no del handler de `__main__`.
- **Features incompletas** (roadmap PR-6..PR-10): respond del ops-hub web es stub, wiring de `event_bus` en GUI incompleto, masterlist del scraper stub, `path_resolver` aún lee `os.environ` (excepción documentada, pendiente migrar a `config.toml`).
- **Docs**: `QUICKSTART.md` apunta a `scripts/first_run.py` (la ruta real es `local_scripts/scripts/first_run.py`) y dice Python "3.14+" (real: 3.11/3.12) — pendientes de corregir.
- **`os._exit(3)` del fail-fast de tests** (PR #179): vigilar las primeras corridas de CI por un posible hard-kill ante un hilo non-daemon lento de dependencia.
