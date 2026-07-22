# Smoke real del broker MO2/USVFS (Windows) — PR #350

Checklist manual para verificar en una instalación Windows real lo que CI
**no puede** probar: que la inyección USVFS funciona de verdad. CI solo
valida contratos y lifecycle con fakes y sockets loopback (ver ADR
[`0007-mo2-broker-usvfs.md`](../adr/0007-mo2-broker-usvfs.md)).

No ejecutar en Linux/WSL: el paso de instalación está bloqueado a propósito
fuera de `win32` (ver "Fixes aplicados" al final). Todo este runbook corre
en el Windows donde vive MO2.

## 0. Precondiciones

- [ ] Windows con Python 3.11/3.12 y el venv del repo (`uv sync --extra dev`),
      **o** el `SkyClaw.exe` congelado (PyInstaller).
- [ ] MO2 instalado, versión anotada acá: `_______________`
- [ ] `ModOrganizer.exe` presente en la raíz de la instancia MO2.
- [ ] Instalación de Skyrim SE/AE con carpeta `Data`.
- [ ] Un perfil MO2 (ej. `Default`) con **al menos un mod activo que aporte
      un plugin** (`.esp`/`.esm`/`.esl`) — es el canario que la attestation
      necesita para tener algo que fingerprintear.
- [ ] LOOT instalado (`loot.exe` accesible o configurado en `config.toml` /
      `--loot-exe`).

## 1. Instalar el bridge

```powershell
python -m sky_claw --mode install-vfs-bridge --mo2-root "C:\ruta\a\MO2"
```

**Esperado:** termina sin excepción, log `Bridge MO2/USVFS instalado en ...`
en `logs/sky_claw.log` (JSON, en el cwd desde donde corriste el comando).

**Verificar en disco:**
- [ ] `<MO2Root>/plugins/skyclaw_bridge/` existe con `plugin.py`, `runtime.py`,
      `protocol.py`, `bridge_config.json`.
- [ ] `bridge_config.json` tiene `worker_executable` apuntando a un **binario
      de Windows** (`python.exe` o el `.exe` congelado) — si ves una ruta
      tipo `/usr/bin/python3` acá, el fix P2 falló o corriste esto desde WSL.

**Fallo esperado (negativo, confirma el guard):** si por error corrés este
paso en WSL/Linux, tiene que **rechazar** con un mensaje que menciona
"Windows" — no debe instalar nada. Si instala, es una regresión del fix P2.

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## 2. Arrancar MO2 y confirmar que el plugin cargó

- [ ] Abrir MO2 normalmente (no headless).
- [ ] En el visor de logs/consola de plugins de MO2 (Tools → mostrar log),
      buscar la línea `Sky-Claw VFS Bridge iniciado`.
- [ ] Si no aparece, o aparece `Sky-Claw VFS Bridge no pudo cargar
      bridge_config.json: ...` — anotar el mensaje completo acá:
      `_______________`

**Nota:** estos mensajes (`qInfo`/`qWarning`) van al log interno de MO2, **no**
a `logs/sky_claw.log`.

- [ ] Resultado: PASA / FALLA

## 3. Probe VFS aislado (`vfs-health`)

Con MO2 **abierto** (el plugin necesita estar cargado para aceptar la
conexión del broker):

```powershell
python -m sky_claw --mode vfs-health --mo2-root "C:\ruta\a\MO2" --skyrim-path "C:\ruta\a\Skyrim Special Edition" --vfs-profile Default
```

**Esperado:** log final `Probe VFS correcto para perfil Default; worker y
nieto validaron el canary` en `logs/sky_claw.log`. Exit code 0.

Este único comando ya cubre dos de los casos críticos:

### 3a. Canary visible en worker Y en el proceso nieto
- [ ] El log confirma "worker y nieto validaron el canary" (no solo el
      worker). Si solo el worker ve la VFS pero el nieto no, es exactamente
      el escenario que la auditoría original (`U-01`) señaló como falso-verde.
- [ ] Resultado: PASA / FALLA — notas: `_______________`

### 3b. Rechazo de perfil equivocado
Repetir el comando con `--vfs-profile` apuntando a un perfil que **no**
tiene el mod/plugin canario (o un nombre de perfil inexistente):

```powershell
python -m sky_claw --mode vfs-health --mo2-root "C:\ruta\a\MO2" --skyrim-path "C:\ruta\a\Skyrim Special Edition" --vfs-profile PerfilSinCanary
```

**Esperado:** falla con `VfsBrokerError` (fingerprint/canary no coincide),
**no** un falso positivo.

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## 4. Ejecución representativa de LOOT a través del broker

Arrancar el daemon completo (GUI, que usa `require_vfs=True` — el path
productivo real):

```powershell
python -m sky_claw --mode gui --mo2-root "C:\ruta\a\MO2"
```

- [ ] Disparar un sort de LOOT desde la GUI sobre el perfil de prueba.
- [ ] Aprobar el HITL cuando lo pida.
- [ ] Confirmar que `loadorder.txt`/`plugins.txt` del perfil se actualizaron
      de verdad (timestamp/contenido).
- [ ] Confirmar en `logs/sky_claw.log` que la respuesta trae
      `vfs_attestation` (lo agrega `loot_service.py` cuando el runner es el
      brokered).

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## 5. Cancelación / timeout — el árbol completo debe morir

Mientras un sort esté corriendo (paso 4, o forzando un timeout bajo con
`--vfs-timeout` en `vfs-health`):

- [ ] Cancelar desde la GUI (o dejar que expire el timeout).
- [ ] Abrir el Administrador de Tareas de Windows **antes** de que termine
      la ventana de gracia y confirmar que el proceso worker (y cualquier
      hijo/nieto que hubiera lanzado, ej. `loot.exe`) **no queda huérfano**
      — el Job Object (`kill-on-close`) debe matar el árbol completo.
- [ ] Confirmar en el log de MO2 (paso 2) que aparece el evento
      `worker_exit` y que el broker no quedó colgado (el proceso Sky-Claw
      no se queda esperando indefinidamente — este es el caso exacto que
      el fix P1 de este PR corrige).

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## 6. Cierre de MO2 con un job activo (regresión del fix P1)

Este caso reproduce específicamente el hallazgo P1 del review de Codex.

- [ ] Disparar un sort de LOOT (paso 4).
- [ ] Mientras el worker sigue corriendo, cerrar MO2 directamente (botón X).
- [ ] Confirmar que el proceso Sky-Claw (broker) **no se cuelga
      indefinidamente** esperando el fence terminal — debe recibir el
      `worker_exit` (o el error de desconexión del bridge) y liberar el lock
      de LOOT en un tiempo acotado.
- [ ] Confirmar que no quedó ningún proceso worker/hijo huérfano en el
      Administrador de Tareas.

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## 7. Rollback contra el target físico real

- [ ] Provocar un fallo a mitad del sort (ej. hacer `plugins.txt` de solo
      lectura antes de correr, o matar `loot.exe` a mano desde el
      Administrador de Tareas durante la ejecución).
- [ ] Confirmar que `loadorder.txt`/`plugins.txt` quedan en el estado
      **previo** al intento fallido (rollback real), no corruptos ni a
      medio escribir.

- [ ] Resultado: PASA / FALLA — notas: `_______________`

## Registro de la corrida

| Campo | Valor |
|---|---|
| Fecha | |
| Versión de MO2 | |
| Versión de USVFS (si se puede ver) | |
| Commit/rama de Sky-Claw probado | |
| Resultado global | PASA / FALLA / PARCIAL |
| Bloqueadores encontrados | |

## Fixes ya aplicados (contexto, no repetir)

Este runbook nace del review de Codex sobre el PR #350. Los tres hallazgos
que motivó ya están corregidos en el código (commit `c2f254a`) y cubiertos
por tests unitarios — este runbook prueba el comportamiento **end-to-end**
en hardware real, no reemplaza esos tests:

- **P1** — teardown del bridge: `wait_for_monitors` ahora espera los threads
  reales (no el dict que el monitor vacía antes de emitir el evento) y
  `client.stop()` drena `outgoing` con timeout antes de cortar el socket.
  Ver casos 5 y 6 arriba.
- **P2** — `install-vfs-bridge` rechaza fuera de `win32` (ver caso 1,
  "fallo esperado").
- **P3** — un fallo de `prepare_for_approval` (attestation sin canary, MO2
  movido) vuelve como error dict (`LootSortingExecutionFailed`), no como
  excepción cruda. No requiere hardware Windows para probarse — ya cubierto
  por `tests/test_hitl_vfs_prepare.py` y
  `tests/test_supervisor_dispatch_tool.py`.
