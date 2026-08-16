# Sky-Claw — Deployment & Operations Runbook

> **Audiencia:** operadores y responsables de release.
> **Fuente canónica:** runtime, CI y packaging del árbol actual.
> **Última verificación:** 2026-07-26 sobre
> `codex/crash-logging-async-safe` `74bdb8f`.

Operational guide for deploying, running and recovering Sky-Claw. For the
quick-start install flow see [QUICKSTART.md](QUICKSTART.md); this document
covers the production/operations gap: configuration, secrets, observability,
failure handling and the pre-flight checklist for a real end-to-end run.

> **Estado:** existen releases publicadas hasta `v0.2.4`; los cambios del árbol
> actual siguen bajo `[Unreleased]`. Los cimientos (locks, rollback, redacción,
> SSRF, HITL) son de grado producción; lo que sigue abierto está en
> [Limitaciones conocidas](#9-limitaciones-conocidas).

---

## 1. Requisitos

| Componente | Versión / nota |
|---|---|
| Python | **3.11 – 3.12** (versiones validadas por CI; `pyproject.toml` exige `>=3.11`). |
| OS | Windows 10/11 (target primario; `file_permissions.py` usa DACL de Windows). Linux/WSL2 corre el core async pero no es la plataforma de entrega. |
| MO2 | Mod Organizer 2 instalado y configurado para Skyrim Special Edition. |
| Node.js | Sólo para desplegar el gateway Node opcional en `sky_claw/app/comms/telegram_gateway_node/`; el modo `telegram` de Python usa polling. |
| Red | Salida a Nexus / proveedor LLM. El egress está restringido por allowlist (`config.py:ALLOWED_HOSTS`). |

---

## 2. Instalación y empaquetado

### Ejecución desde fuente (desarrollo / operador)
```bat
build.bat              :: crea .venv\ e instala dependencias
```

### Bridge MO2/USVFS (obligatorio para LOOT productivo)

Instala o actualiza el plugin antes del primer run. El comando no arranca el
daemon ni ejecuta herramientas:

```bat
python -m sky_claw --mode install-vfs-bridge --mo2-root "D:\MO2Portable"
```

Con el build empaquetado:

```bat
SkyClawApp.exe --mode install-vfs-bridge --mo2-root "D:\MO2Portable"
```

El instalador hace staging + swap con rollback y deja el bundle en
`<MO2>\plugins\skyclaw_bridge`. No guarda el token IPC en el plugin: solo fija
el executable del worker, su prefijo y la ruta del descriptor efimero bajo
`~\.sky_claw\vfs_bridge\<instance-id>`. Reinicia MO2 para que cargue un plugin
nuevo. Si el executable de Sky-Claw cambia de ubicacion, reinstala el bridge.

Al iniciar Sky-Claw, el broker publica una sesion owner-only en loopback. El
plugin reconecta cuando el descriptor aparece. Cada ejecucion usa un perfil
explicito y falla cerrada si el bridge no conecta, si el perfil cambia o si no
hay un canary elegible: al menos un archivo de un mod habilitado que no exista
en el `Data` fisico.

Smoke obligatorio en la instalacion real (primero un perfil descartable):

1. Abrir MO2 y confirmar en su log que `Sky-Claw VFS Bridge` cargo.
2. Con MO2 abierto y el daemon Sky-Claw detenido, ejecutar
   `python -m sky_claw --mode vfs-health --mo2-root "D:\MO2Portable"`
   (agregar `--skyrim-path` y `--vfs-profile` si no estan en config). Worker y
   nieto deben ver el mismo canary/hash.
3. Cambiar al perfil incorrecto y comprobar que no se lanza la tool.
4. Ejecutar LOOT con un snapshot conocido, comprobar stdout/stderr, exit code y
   que el load order real corresponde al perfil.
5. Forzar timeout/cancelacion y verificar que `worker_exit` llega antes del
   rollback y que no quedan worker ni nietos.
6. Inyectar un fallo despues de escritura y comparar byte a byte el target
   restaurado.

Este smoke no se sustituye por pytest/PyInstaller: depende de la version real
de MO2 y USVFS. La decision completa esta en
[`docs/adr/0007-mo2-broker-usvfs.md`](docs/adr/0007-mo2-broker-usvfs.md).

Lockfiles reproducibles: `requirements.lock` (pip) y `uv.lock` (uv). Para un
entorno bloqueado: `uv sync --locked --extra dev`.

### Binario empaquetado (.exe)
`build.bat` construye el ejecutable vía PyInstaller (`sky_claw.spec`). El binario
arranca en **modo GUI por defecto** (`__main__.py` fija `mode=gui` cuando
`sys.frozen`).

> La release `v0.2.4` publicó `SkyClawApp.exe`, su SBOM y el bundle de firma
> Cosign `SkyClawApp.exe.bundle.json`. Los cambios del árbol actual permanecen
> en `[Unreleased]`; no forman parte de esa release.

---

## 3. Configuración

La config se carga desde **`~/.sky_claw/config.toml`**
(`Config.DEFAULT_CONFIG_FILE`). `Config` combina defaults, TOML y keyring; no
implementa un override genérico `SKY_CLAW_*`. Las variables de rutas que se
enumeran abajo pertenecen a `PathResolver`.

> El asistente interactivo existe en **`local_scripts/scripts/first_run.py`**.
> Corré `python local_scripts/scripts/first_run.py`, editá
> `~/.sky_claw/config.toml` a mano / usá las env vars de abajo.

### Paths de herramientas (excepción Zero-Trust documentada)
`path_resolver.py` es el **único** punto que lee estas variables de entorno
(ver `docs/pending_ooda_status.md` §2.3 — migración a `config.toml` pendiente,
sin fecha); ningún otro módulo debe leerlas directo. Nombres
reales según `sky_claw/app/core/path_resolver.py`:

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
`ollama`**. El modelo es **provider-scoped**: cada provider tiene su campo
`{provider}_model` en `config.toml` (`anthropic_model`, `deepseek_model`,
`openai_model`, `ollama_model`), seteado por el wizard CLi; vacío → el
`DEFAULT_MODEL` del provider (p.ej. `gpt-5` para OpenAI). Así cambiar de
provider nunca arrastra un modelo incompatible. El `llm_model` global es legacy
(se migra al `{provider}_model` activo al cargar). No existe flag `--model` en
la CLI. Si el modelo no está disponible en tu cuenta, la API devuelve un 4xx
claro (se loguea) y elegís otro.

### Token WS — dos flujos distintos
1. **Gateway Node ↔ bridge Python**: el server Node lee `WS_AUTH_TOKEN` de
   `process.env` (`telegram_gateway_node/server.js:98`); el bridge Python lo lee
   de `WS_AUTH_TOKEN` y, si no está, cae a keyring (`frontend_bridge.py:301`).
2. **WS Daemon ↔ NiceGUI (interno)**: `AuthTokenManager` lee/escribe un
   **token-file rotativo** en `~/.sky_claw/tokens/` (`read_token_file(token_dir)`),
   con TTL/rotación. La rotación es del archivo, no del keyring.

### CredentialVault (almacén cifrado, separado)
`sky_claw/app/security/credential_vault.py` es un almacén **cifrado con
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
`command` es un argumento **posicional** (`__main__.py::_parse_args`,
`nargs="?"`) — **no existe `--command`**. El modo Telegram vigente construye
`TelegramPolling`; el gateway Node es una superficie separada y no es
precondición de `_run_telegram()`.

---

## 6. Observabilidad — ¿dónde queda registrado si algo falla?

`setup_logging()` se invoca antes de crear el event loop en todos los modos.
El productor sólo redacta, enriquece y usa `put_nowait()` sobre una cola
acotada; un listener dedicado realiza la apertura, escritura, flush y rotación.
Produce **logs JSON rotativos (10 MB × 5 backups)** bajo
`~/.sky_claw/logs/`, con `correlation_id`, `trace_id`, PID, hilo, rol de proceso
y task por línea. La **redacción de secretos** (API keys, tokens, Bearer, PII y
query-strings) ocurre antes de cruzar al listener.

| Archivo | Contenido |
|---|---|
| `~/.sky_claw/logs/sky_claw.log` | App principal: todos los niveles, incluido `ERROR`. Es el log a mirar primero. |
| `~/.sky_claw/logs/crash.log` | Todos los eventos `ERROR+`, incluidos watcher y seguridad. |
| `~/.sky_claw/logs/watcher.log` | Subsistema `SkyClaw.Watcher`. |
| `~/.sky_claw/logs/watcher_security.log` | Eventos de `SkyClaw.Security`. |
| `~/.sky_claw/logs/workers/vfs-worker-<job_id>.log` | Evidencia y traceback del worker VFS aislado por job. |

La cola principal admite 8192 eventos. Si se satura, los productores nunca
esperan: los eventos `WARNING` y menores pueden perderse y la pérdida se
contabiliza. Un deque de emergencia conserva como máximo 256 eventos
`ERROR/CRITICAL` para reinyección; si también se llena, expulsa el evento
`ERROR/CRITICAL` más antiguo. Un fallo de permisos, disco lleno, apertura o
rotación marca el runtime de logging como degradado y aplica reintentos
espaciados; no se escribe un fallback síncrono desde asyncio ni se propaga el
fallo a la aplicación. Cualquier mecanismo futuro de health-check o
recuperación debe mantener el I/O bloqueante fuera del event loop.
Si Windows bloquea el renombrado de una rotación (`WinError 32/5`), el rollover
se difiere y el listener continúa escribiendo en el archivo base hasta el
siguiente intento.

Garantías relevantes para un run real:
- **Excepciones no manejadas del event loop (incluida GUI)**:
  `install_loop_exception_handler()` se instala en `_main()` y también en el
  bootstrap del loop de NiceGUI. Las tasks creadas por `AppContext._track_task()`
  consumen inmediatamente su excepción y registran nombre, contexto y traceback;
  las tasks huérfanas caen al handler del loop. La cancelación normal no se
  registra como crash.
- **Subprocesos y VFS**: LOOT y xEdit registran los non-zero como `ERROR` con
  `exit_code` y `stderr`. Hasta 64 KiB se conserva completo; para salidas mayores
  se guarda el tail, tamaño original, SHA-256 y `stderr_truncated=true`. Un
  `worker_exit` sin `job_result` queda atribuido al job y tool en el broker.
- **GUI empaquetada**: si `stdout`/`stderr` son `None`, se conectan a adaptadores
  de texto del pipeline con `write()`, `flush()`, `isatty()`, `writable()` y
  `fileno()` compatibles con `TextIOBase`. Ya no se crea el archivo lateral
  `sky_claw_startup.log`. El watchdog registra `gui_watchdog_shutdown` antes de
  solicitar `app.shutdown`; el `finally` exterior drena y cierra el listener
  después de los hooks de NiceGUI y `AppContext.stop()`. El stdout del probe VFS
  sigue reservado exclusivamente para su hash de protocolo.
- **Audit trail en SQLite** (aparte de los logs de texto), en
  `sky_claw/app/db/journal.py`: la tabla `journal_entries` registra cada
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
grep '"level": "ERROR"' ~/.sky_claw/logs/sky_claw.log
Select-String '"level": "ERROR"' "$HOME\.sky_claw\logs\sky_claw.log"
# seguir el hilo de una operación por su correlation_id
grep '<correlation_id>' ~/.sky_claw/logs/sky_claw.log
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
| **Gate HITL** | Los handlers y middleware que reciben `HITLGuard` requieren aprobación y fallan cerrados; no es una garantía implícita de toda ruta. |

---

## 8. Pre-flight checklist — primer run end-to-end real

Antes de soltar el agente sobre un Skyrim+MO2 real (idealmente en VM o perfil de
MO2 descartable la primera vez):

- [ ] `~/.sky_claw/config.toml` creado; `SKYRIM_PATH` y `XEDIT_PATH` válidos (los exige el chequeo de paths en runtime), `MO2_PATH` dentro del sandbox.
- [ ] Secretos en **keyring** (`service="sky_claw"`): `llm_api_key` o `<provider>_api_key`; `nexus_api_key`; `telegram_bot_token` si usás Telegram. (Cargar en `CredentialVault` NO los expone al arranque.)
- [ ] Proveedor LLM elegido entre los soportados: `anthropic` / `deepseek` / `openai` / `ollama`.
- [ ] Suite local en verde: `pytest -q`.
- [ ] Gates: `ruff check sky_claw/ tests/`, `ruff format --check sky_claw/ tests/` y `mypy sky_claw/`.
- [ ] `~/.sky_claw/logs/` escribible; correr con `-v` la primera vez. Si no lo
  está, Sky-Claw debe continuar y el logging queda degradado.
- [ ] Perfil de MO2 respaldado (el rollback cubre operaciones del agente, pero un backup externo es barato).
- [ ] Validar preview y dry-run siguiendo el orden canónico de `sky_claw/local/AGENTS.md` **antes** de un run con mutaciones reales.
- [ ] Tras el run: revisar `~/.sky_claw/logs/crash.log`; en el journal,
  `transactions` con estado `pending`/`rolled_back` (transacción no confirmada)
  y `journal_entries` con estado `failed` (operación caída).

---

## 9. Limitaciones conocidas

Honestidad operativa — esto sigue abierto y conviene saberlo antes de producción:

- **Validación de rig real parcial** — existe evidencia histórica del canary
  brokerizado, pero no cubre todos los runners ni todos los escenarios; ver
  `docs/operations/real_rig_validation.md`.
- **Evidencia de publisher de Windows pendiente** — Cosign no equivale a
  Authenticode. El workflow incluye `Cosign sign-blob`, pero no un paso de
  Authenticode ni `signtool`; esta tarea no inspeccionó la firma PE histórica.
  Los cambios actuales siguen en `[Unreleased]`.
- **Frontera de tipos parcial** — el override de mypy con `ignore_errors=true` cubre **prácticamente todo `sky_claw.*` / `sky_claw.app.*`**, con re-habilitación puntual de checks en un subconjunto de `core.*` y en `orchestrator.sync_engine`. El grueso del código no está type-checked aún.
- **Estado vivo** — consultar `docs/pending_ooda_status.md` y reverificar cada
  ítem contra el árbol actual; un roadmap o auditoría fechada no sustituye esa
  comprobación.

---

## 10. Proceso de Release y Empaquetado Final

Workflow estándar para publicar una nueva versión de Sky-Claw. El empaquetado se realiza con PyInstaller usando el spec `sky_claw.spec` (que autoderiva el `VERSIONINFO` de la versión del paquete en `pyproject.toml`).

> **Estado actual:** GitHub contiene releases hasta `v0.2.4`. La publicación de
> nuevos releases está **pausada temporalmente** mientras continúan los refactors
> estructurales. `.github/workflows/release.yml` conserva la receta de build,
> SBOM, firma y publicación, pero no se dispara por tags y el job `release` está
> bloqueado intencionalmente. Los cambios del árbol actual siguen en
> `[Unreleased]`.
>
> **Evidencia histórica de `v0.2.4`:** esta actualización documental
> no verificó criptográficamente el bundle, no hizo cold boot de
> `SkyClawApp.exe` ni inspeccionó la firma PE del artefacto histórico.

> **Reactivación:** restaurar en `.github/workflows/release.yml` el trigger
> `push.tags` para `v*` y eliminar el guard `if: ${{ false }}` del job `release`.
> Solo después de mergear esa reactivación debe crearse y pushearse un nuevo tag
> de versión.

### 10.1 Checklist de Release

Antes de invocar el empaquetado, **todos** los gates de CI deben estar en verde y la pre-flight checklist (§8) validada en una instalación real:

1.  **Verificar versión del paquete:** Bump de `version` en `pyproject.toml` (semver: `MAJOR.MINOR.PATCH`).
2.  **Actualizar CHANGELOG:** Mover los cambios de `[Unreleased]` a la nueva sección de versión con fecha.
3.  **Gates locales:**
    - `ruff check sky_claw/ tests/` (sin errores).
    - `ruff format --check sky_claw/ tests/`.
    - `mypy sky_claw/` (bloqueante).
    - `pytest -q` (cobertura ≥ 60%).
4.  **Smoke del VFS Bridge:** Ejecutar el flujo de la sección §2 (Bridge MO2/USVFS) en un perfil descartable para confirmar que el bridge funciona con el binario empaquetado.

### 10.2 Empaquetado con PyInstaller

El script `build.bat` orquesta la construcción del entorno y el binario. Para invocar PyInstaller directamente (debug):

```bash
# Asegurar venv activo y dependencias instaladas
pyinstaller sky_claw.spec --noconfirm --clean
```

El binario resultante se deposita en `dist/SkyClawApp/` (o equivalente según el spec). El `.spec` maneja:
- Inclusión de assets (plantillas, recursos estáticos de NiceGUI).
- Autoderivación del `VERSIONINFO` de Windows desde `pyproject.toml`.
- Hidden imports de dependencias dinámicas (ej. plugins de Pydantic).

### 10.3 Post-Build y Validación

Tras generar el `.exe`:

1.  **Arranque en modo GUI:** Ejecutar `SkyClawApp.exe` en una máquina limpia (sin Python instalado). Debe arrancar en modo GUI por defecto (`sys.frozen`).
2.  **Arranque en modo CLI:** Probar `SkyClawApp.exe --mode cli -v` para verificar logs y que el handler de excepciones del loop funciona.
3.  **Validación de Bridge:** Correr `SkyClawApp.exe --mode install-vfs-bridge --mo2-root "D:\MO2Portable"` y validar el smoke de §2.
4.  **Artifact Tagging:** mientras la publicación esté pausada, **no crear ni
    pushear tags de release**. Tras reactivar el workflow según el bloque
    anterior, sustituir `<version>` por la versión SemVer declarada, crear el tag
    (`git tag -a "v<version>" -m "Release v<version>"`) y pushearlo
    (`git push origin "v<version>"`). En ese estado reactivado, el push dispara
    `.github/workflows/release.yml`, que publica el ejecutable, el bundle Cosign
    y el SBOM en GitHub Releases. Los comandos con `<version>` son plantillas,
    no deben ejecutarse literalmente.

> **Firmas distintas:** Cosign no equivale a Authenticode. El workflow firma el
> blob y publica el material en `SkyClawApp.exe.bundle.json`, pero no incluye un
> paso de Authenticode ni `signtool`. Esta tarea no inspeccionó la firma PE del
> artefacto histórico; la identidad de publisher y SmartScreen permanecen sin
> verificación independiente.

La separación entre operación diaria, observabilidad, recuperación, release y
smoke real se mantiene en [docs/operations](docs/operations/README.md).
