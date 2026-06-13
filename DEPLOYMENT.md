# Sky-Claw — Deployment & Operations Runbook

Operational guide for deploying, running and recovering Sky-Claw. For the
quick-start install flow see [QUICKSTART.md](QUICKSTART.md); this document
covers the production/operations gap: configuration, secrets, observability,
failure handling and the pre-flight checklist for a real end-to-end run.

> **Estado:** release-candidate, no GA. Los cimientos (locks, rollback, redacción,
> SSRF, HITL) son de grado producción; lo que sigue abierto está en
> [Limitaciones conocidas](#limitaciones-conocidas).

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
> `[Unreleased]`) ni binario firmado/validado. Ver [Limitaciones](#limitaciones-conocidas).

---

## 3. Configuración

La config se carga desde **`~/.sky_claw/config.toml`** (`config.py:58-59`,
`DEFAULT_CONFIG_DIR = Path.home() / ".sky_claw"`). Se permite override por
variables de entorno.

> ⚠️ `QUICKSTART.md` indica correr `python scripts/first_run.py` (asistente
> interactivo) — **ese archivo no existe en el repo**. Hasta que se reponga,
> configurar `~/.sky_claw/config.toml` a mano (o por las env vars de abajo).

### Paths de herramientas (excepción Zero-Trust documentada)
`path_resolver.py` es el **único** punto que lee estas variables de entorno
(ver `ZERO_TRUST_TODO.md`); ningún otro módulo debe leerlas directo:

| Variable | Uso |
|---|---|
| `SKYRIM_PATH` | Raíz de Skyrim SE |
| `MO2_PATH` | Instalación de Mod Organizer 2 |
| `DYNDLOD_EXE`, `--xedit-exe`, `--install-dir` | Ejecutables de herramientas (también por CLI) |

Estos paths pasan por `PathValidator` (sandbox anti-traversal) antes de cualquier
subprocess.

---

## 4. Secretos

**Nunca** en `config.toml` ni en código. Sky-Claw usa dos mecanismos:

- **`CredentialVault`** (`security/credential_vault.py`): secretos cifrados en
  SQLite vía `get_secret(name)` / `set_secret(name, value)`. Backend keyring del
  SO (Windows Credential Manager).
- **Keyring directo** para el token WS del puente NiceGUI↔Daemon
  (`auth_token_manager`, servicio `sky_claw` / clave `ws_auth_token`), con
  rotación a media-vida (TTL 3600 s).

Secretos requeridos según modo: API key del proveedor LLM (Claude/GPT/DeepSeek/
Ollama), API key de Nexus, y —para Telegram— `telegram_bot_token` + el
`ws_auth_token` del gateway.

---

## 5. Modos de ejecución

```bash
python -m sky_claw --mode gui        # interfaz NiceGUI (default del binario)
python -m sky_claw --mode telegram   # bot HITL desde el celular
python -m sky_claw --mode cli        # terminal interactiva
python -m sky_claw --mode oneshot --command "<cmd>"   # ejecución única
python -m sky_claw --mode security --command "<cmd>"  # utilidades de seguridad
python -m sky_claw --mode cli -v     # -v / --verbose → logging DEBUG
```
El modo Telegram requiere el gateway Node corriendo
(`telegram_gateway_node/`, `npm install` + arranque del server).

---

## 6. Observabilidad — ¿dónde queda registrado si algo falla?

`setup_logging()` (`logging_config.py:208`) se invoca en todos los modos al
arranque. Produce **logs JSON rotativos (10 MB × 5 backups)** en `logs/`, con
`correlation_id` + `trace_id` por línea y **redacción de secretos** (API keys,
tokens, Bearer, PII, query-strings) ya aplicada en disco:

| Archivo | Contenido |
|---|---|
| `logs/sky_claw.log` | App principal — **todos los niveles, incluido ERROR**. Es el log a mirar primero. |
| `logs/watcher.log` | Subsistema watcher (`SkyClaw.Watcher`, `propagate=False`). |
| `logs/watcher_security.log` | Eventos de seguridad. |

Garantías relevantes para un run real:
- **Las excepciones no manejadas del event loop no se pierden**:
  `_install_loop_exception_handler()` (`__main__.py:129`) las enruta a
  `logger.error` con `exc_info`, así que las tareas fire-and-forget que fallen
  quedan en `logs/sky_claw.log` con stack y `correlation_id`.
- **Audit trail en SQLite** (aparte de los logs de texto): tabla `transactions`
  del journal (`db/journal.py`) + `log_tasks_batch` del registry registran
  cada operación con su estado (started/completed/failed/rolled_back).
- **Métricas/tracing** (opcional): Prometheus (`prometheus-client`) y OpenTelemetry
  (`OTEL_EXPORTER_OTLP_ENDPOINT`) si se configura un collector.

### Diagnóstico rápido de un fallo
```bash
# correr con DEBUG
python -m sky_claw --mode cli -v
# filtrar errores del log JSON
grep '"levelname": "ERROR"' logs/sky_claw.log        # bash/WSL
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

- [ ] `~/.sky_claw/config.toml` creado; `SKYRIM_PATH` / `MO2_PATH` válidos y dentro del sandbox.
- [ ] Secretos cargados en el vault (LLM key; Nexus key; Telegram token si aplica).
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
- **Frontera de tipos parcial** — mypy `ignore_errors=true` en gui/comms/agent/web/security/db (~1.7k errores suprimidos); el core fundacional y `sync_engine` sí están chequeados.
- **Features incompletas** (roadmap PR-6..PR-10): respond del ops-hub web es stub, wiring de `event_bus` en GUI incompleto, masterlist del scraper stub, `path_resolver` aún lee `os.environ` (excepción documentada, pendiente migrar a `config.toml`).
- **Docs**: `QUICKSTART.md` referencia `scripts/first_run.py` (ausente) y versión Python "3.14+" (real: 3.11/3.12) — pendientes de corregir.
- **`os._exit(3)` del fail-fast de tests** (PR #179): vigilar las primeras corridas de CI por un posible hard-kill ante un hilo non-daemon lento de dependencia.
