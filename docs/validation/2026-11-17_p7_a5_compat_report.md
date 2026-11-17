# P7-A5-COMPAT — Compatibilidad real de MO2 2.5.2 con `FILE_SHARE_NONE`

> **Informe técnico `P7_A5_COMPAT_REPORT`.** El archivo versionado usa la
> convención de fecha del repo; este es el informe que el encargo pide con ese
> nombre.
>
> **Sesión:** `P7-A5-COMPAT-MO2`
> **Tracker:** #672 (gate de exclusión) — esta fase NO cierra P7-A5 productivo.
> **Dictamen:** **`P7_A5_COMPAT_INCONCLUSIVE`**
> **Fecha:** 2026-11-17
> **Base:** `origin/main` = `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` (merge de
> #947). La rama parte de ese SHA sin commits previos; el SHA final es el commit
> que introduce este archivo (no auto-referenciable dentro del propio archivo) y
> se identifica en el PR.
> **Alcance:** `SYNTHETIC_ONLY` + sonda de la capa de configuración + preflight
> read-only. **MO2 2.5.2 real NO se ejecutó.**

---

## 0. Resumen ejecutivo

| Pregunta del encargo | Respuesta | Estado |
|---|---|---|
| ¿El mecanismo de exclusión cumple su contrato Win32 en este host? | Sí, medido con procesos externos reales | **VERIFIED (sintético)** |
| ¿Es operativamente compatible con MO2 2.5.2 real? | **No medido.** No hay aislamiento disponible | **NOT_VERIFIED** |
| ¿Qué hace `QSettings` 6.7 —la capa que MO2 2.5.2 **importa**— cuando el INI está retenido? | No lee (`AccessError`, 0 claves); no escribe (INI intacto); al liberar sincroniza y **conserva** las claves previas | **VERIFIED (librería)**; el uso real que MO2 hace de ella **no** está probado (§7) |
| ¿MO2 pierde configuración en silencio? | **No determinado.** Depende de la lógica de la aplicación | **NOT_VERIFIED** |
| ¿Se puede declarar GO? | No: falta el criterio de MO2 real | **NO (INCONCLUSIVE)** |

**Tokens vigentes que este informe NO levanta:**
`MODORGANIZER_EXE_COMPAT = NOT_MEASURED_ON_LIVE_MO2` (el informe acota qué falta,
no lo cierra) y `QT_QSETTINGS_EQUIVALENCE = NOT_CLAIMED` (se midió el
comportamiento de Qt, **no** el modo de compartición que Qt pide contra el INI
retenido en el escenario real).

La conclusión central: **el rig sintético no habilita el GO**. El encargo lo
prohíbe, y la medición de la capa Qt —aun siendo favorable— **no es MO2**.

---

## 1. SHA base y final

- Base: `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` — `Merge pull request #947`,
  2026-11-15 16:55:49 -0300.
- `origin/main` al iniciar: `db85bd581537f906f166ec0cacafeedf9ee17d02`, 13
  commits por delante de la base. No se sincronizó: el encargo fija la base en
  #947.
- SHA final: el commit que agrega este informe (ver PR).

## 2. Rama y worktree

- Rama: `test/p7-a5-compat-mo2` (no existía en `origin`).
- Worktree: `E:/Skyclaw_Main_Sync/.worktrees/p7-a5-compat-mo2`.
- El worktree **ya existía** al iniciar la sesión, limpio, exactamente en
  `28ce8345`. No se creó otro, no se usó el de #947 y no se tocó el principal
  (`E:/Skyclaw_Main_Sync`, que sigue en `main` con su árbol previo sin commitear).

## 3. Versiones del laboratorio

| Componente | Valor | Cómo se obtuvo |
|---|---|---|
| SO | Windows 10 Pro 10.0.19045, x64 | `Win32_OperatingSystem` |
| Host | MSI MS-7E24, `HypervisorPresent=True` | `Win32_ComputerSystem` |
| Python (repo) | 3.11.9 | `.venv` del worktree (`uv sync --extra dev`) |
| Python (lab Qt) | 3.11.9 + PyQt6 6.7.1 (`PyQt6-Qt6` 6.7.3) | wheel de PyPI |
| Qt del lab | runtime `qVersion()` **6.7.3**; build `QT_VERSION_STR` **6.7.1** | sonda |
| Qt del lab (DLL) | `PyQt6/Qt6/bin/Qt6Core.dll` **6.7.3.0** | `VersionInfo` |
| Suite de exclusión | 78 passed, 1 skipped (ver §16) | `pytest` |

## 4. Modelo de aislamiento disponible: **ninguno**

| Mecanismo | Estado medido |
|---|---|
| Windows Sandbox | `Containers-DisposableClientVM` = **2 (deshabilitada)**; `WindowsSandbox.exe` **ausente** |
| Hyper-V | `Microsoft-Hyper-V-All` = **2 (deshabilitada)**; sin módulo `Get-VM` |
| Contenedores Windows | `Containers` = **2**; demonio Docker **caído**; un contenedor no hospeda una GUI Qt interactiva |
| VirtualBox / VMware | **ausentes** |
| WSL | `VirtualMachinePlatform`=1, **sin distribuciones**, y es Linux |

**Conclusión:** no hay ninguna VM Windows desechable en este host. Ejecutar
`ModOrganizer.exe` contra las superficies del usuario habría compartido
`%LOCALAPPDATA%\ModOrganizer`, HKCU y `Documents\My Games`, que el §4 prohíbe
modificar. Por eso **no se ejecutó** y no se declara equivalencia.

## 5. Hash de MO2 y bibliotecas relevantes

| Artefacto | Versión | Tamaño | SHA-256 |
|---|---|---|---|
| `ModOrganizer.exe` | 2.5.2 | 5.028.352 B | `442B354A8F34754DA0048654C44D27F51628FEBA54CE46C3187CF58D6C43E622` |
| `Qt6Core.dll` (en `dlls/`) | 6.7.1.0 | — | no hasheado |
| `QtWebEngineProcess.exe` | 6.7.1.0 | — | no hasheado |
| `usvfs_x64.dll` | 0.5.6.1 | 1.854.976 B | no hasheado |

La versión de Qt de MO2 se obtuvo de los DLL/EXE del propio instalador.

## 6. Evidencia sintética previa

`tests/test_frozen_runtime_p5c2_mo2_exclusion.py` (P5-C2) se ejecutó en este host
**antes** de tocar nada: **77 passed, 1 skipped**.

El skip **no** es el caso no-Windows (ese corre y pasa en Windows): es
`test_un_reparse_point_se_rechaza`
(`tests/test_frozen_runtime_p5c2_mo2_exclusion.py:146`, «crear symlinks requiere
privilegio en esta máquina», WinError 1314). Consecuencia declarada: la rama de
rechazo de reparse points **no se ejerció en este host** y sigue `INDETERMINATE`
(ADR 0017 §7), sin autorizar mutación.

Tras agregar el test de §16, la suite queda en **78 passed, 1 skipped**.

## 7. Evidencia obtenida del MO2 auténtico

**Ninguna ejecución.** No se invocó `ModOrganizer.exe`, ni el binder productivo,
ni una transición A5.

Lo único observado del binario real es **estático**: su tabla de importaciones
contiene `?status@QSettings@@…`, `?sync@QSettings@@…`, `?value@QSettings@@…`,
`?setValue@QSettings@@…`, `?beginReadArray@QSettings@@…`,
`?setArrayIndex@QSettings@@…`, `?fileName@QSettings@@…`, y **no**
`?isWritable@QSettings@@…`. Que `status` esté importado prueba que MO2 *puede*
consultarlo; **no** prueba que lo consulte ni qué hace con el resultado. Esa
distinción es la que impide el GO.

## 8. Matriz de comportamiento `FILE_SHARE_NONE`

Medida en este host (build 19045). El tenedor usa el módulo real
(`adquirir_exclusion_ini`); cada agresor corre en un **proceso separado**. En
todas las filas **BLOQUEADAS** el `sha256` del INI quedó igual al previo (la fila
de control lo cambia a propósito).

| Experimento | Resultado | Código Win32 | Ancla en el repo |
|---|---|---|---|
| Escritura externa CRT (`open("r+b")`) | BLOQUEADO | `errno 13` (CRT, **no** Win32) | `test_escritura_externa_bloqueada…` |
| Escritura `CreateFileW(GENERIC_WRITE, share=7)` | BLOQUEADO | **32** `SHARING_VIOLATION` | `test_escritura_externa_con_comparticion_plena…` (nuevo) |
| Truncado externo (`truncate(0)`) | BLOQUEADO | `errno 13` (CRT) | **no separable**: el `open` se deniega antes del truncado; lo cubre la fila de escritura |
| `DeleteFile` externo | BLOQUEADO | **32** | `test_borrado_externo_bloqueado…` |
| `MoveFileExW` externo (`os.replace`) | BLOQUEADO | **5** `ACCESS_DENIED` | `test_reemplazo_externo_bloqueado…` |
| `ReplaceFileW` externo | BLOQUEADO | **32** | `NOT_ANCHORED_IN_REPO` (medido en el rig) |
| Lectura externa CRT | BLOQUEADO | `errno 13` | `test_ningun_lector_externo_abre…` |
| Lectura `CreateFileW(GENERIC_READ, share=7)` | BLOQUEADO | **32** | `test_ningun_lector_externo_abre[compatible]` |
| Apertura con sólo `DELETE` | BLOQUEADO | **32** | `NOT_ANCHORED_IN_REPO` |
| Escritura por alias hardlink del objeto | BLOQUEADO | `errno 13` | `NOT_ANCHORED_IN_REPO` |
| Tras cerrar el handle | **EXITO** (mutó el archivo) | — | `test_liberacion_tras_cerrar…` |
| Muerte abrupta del tenedor (`os._exit(9)`) | readquisición `ESTABLISHED` | — | `test_liberacion_tras_muerte_abrupta…` |
| Segundo intento con el handle retenido | `NOT_ESTABLISHED`, determinista | — | `test_el_reintento_acotado…` |

**Distinción exigida:** los `13` son `EACCES` del CRT y **no** demuestran por sí
solos una violación de compartición. La demostración está en las filas con
`CreateFileW` (32) y `MoveFileExW` (5). Se reportan las dos capas separadas.

## 9. Lectura y escritura concurrentes (capa Qt 6.7)

Sonda versionada en
[`2026-11-17_p7_a5_compat_mo2/probe/probe_qsettings.py`](2026-11-17_p7_a5_compat_mo2/probe/probe_qsettings.py)
(comandos y requisitos en el README del paquete). Cada escenario en **proceso
nuevo**: Qt cachea `QConfFile` por proceso, así que medir desde el proceso que
autoró el archivo mide el caché y no el disco.

INI de laboratorio **autorado con `QSettings`**: 34 claves, `gamePath` como
`@ByteArray(G:\\Lab_Compat\\Skyrim_Runtime_1.6.1170)`, perfil
`@ByteArray(Golden Master)`, `size=3` y 3 ejecutables (SKSE con
`-p "Golden Master"`, SSEEdit con `-quickautoclean`, Pandora), UNC sintética
`\\NAS-SINTETICO\share\Data`.

| Escenario | Medición |
|---|---|
| **A** — cliente arranca con el lock retenido | `status` tras ctor `AccessError`; **0 claves**; `gamePath` nulo; **0 ejecutables**; INI intacto |
| **B** — escritura con el lock retenido | 0 claves leídas; `setValue`+`sync` → `AccessError`; **INI intacto** |
| **C** — `sync` diferido | 1.er `sync` `AccessError`; tras liberar escribe y una lectura fresca ve **36 claves** (34+2) con los **3 ejecutables y `gamePath` intactos** |
| **D** — destructor tras liberar | escribió; 35 claves; **3 ejecutables y `gamePath` preservados** |
| **E** — objeto `QSettings` vivo, sin lock | adquisición `ESTABLISHED` ⇒ **`QSettings` no retiene el archivo** |

Hallazgo con consecuencia: **`status()` es pegajoso.** En C el segundo `sync`
escribió de verdad y el estado siguió diciendo `AccessError`. Un componente que
decida «falló» sólo por `status` se equivoca. Medido una vez, en este build.

## 10. Comportamiento de MO2 tras error de compartición

**NOT_VERIFIED.** No se ejecutó MO2: no se observó si muestra error, reintenta,
usa caché, crea configuración alternativa, omite datos o falla.

Lo único afirmable es lo que habilita §9: un cliente tipo MO2 **vería un store
vacío** con `AccessError` y **no podría escribir** mientras el lock esté
retenido. El riesgo residual —que MO2 escriba defaults, abra el asistente de
primera ejecución o cree otra configuración— depende de su lógica y no se midió.

## 11. Integridad de `modorganizer.ini`

- En todas las filas bloqueadas de §8 el `sha256` quedó idéntico.
- En C y D, tras liberar, el archivo cambió **solo** por lo que el cliente
  escribió; las claves preexistentes se conservaron, incluidos los 3 ejecutables.
- `POWER_LOSS_GUARANTEE = NOT_CLAIMED` (sin cambios; no se hizo corte eléctrico).

## 12. Reintentos y timeouts

- Con el handle retenido y 3 intentos, el segundo intento termina en
  `NOT_ESTABLISHED` de forma determinista y sin espera apreciable (medido).
- El valor por defecto de reintentos y espera lo posee el módulo
  (`adquirir_exclusion_ini`), no este informe.

## 13. Recuperación y rollback

- **No se ejerció recuperación contra MO2** ni una transición A5.
- Muerte abrupta del tenedor: el kernel libera el handle y la readquisición da
  `ESTABLISHED` (medido). Evita dejar el INI inaccesible tras un crash.
- `POWER_LOSS_GUARANTEE = NOT_CLAIMED`.

## 14. Diferencias Portable vs ACTIVE_GLOBAL

- La instancia real vive en `%LOCALAPPDATA%\ModOrganizer\Skyrim Special Edition\`,
  lo que **corrobora** la identidad `ACTIVE_GLOBAL` del preflight anterior.
- **No se ejecutó MO2 Portable ni Global.** No se declara equivalencia, ni se usa
  el portable como sustituto: el encargo lo prohíbe y la sonda Qt no lo cambia.

## 15. Riesgos residuales

| # | Riesgo | Severidad | Estado |
|---|---|---|---|
| R1 | MO2 2.5.2 real nunca se ejecutó bajo el lock | **Alta** | `NOT_VERIFIED` — bloquea el GO |
| R2 | MO2 podría tratar el store vacío como «instancia sin configurar» y escribir defaults | Alta | No medido |
| R3 | `QSettings::status` pegajoso: decidir por `status` puede concluir mal | Media | Medido (§9-C) |
| R4 | Sin demostración de equivalencia con `ACTIVE_GLOBAL` | Alta | Bloquea el GO |
| R5 | Lectores durante el lock reciben `ERROR_SHARING_VIOLATION` | Media | Medido y documentado en el módulo |
| R6 | Escritura in-place no atómica; sin garantía ante corte eléctrico | Media | `POWER_LOSS_GUARANTEE = NOT_CLAIMED` |
| R7 | Preflight productivo `BLOCKED` (§19) | Alta | Observado, **no corregido** |

## 16. Tests ejecutados

Comandos en la forma que exige `AGENTS.md`, desde el worktree:

| Comando | Resultado |
|---|---|
| `.venv\Scripts\python.exe -m pytest -q -rs tests/test_frozen_runtime_p5c2_mo2_exclusion.py` | **78 passed, 1 skipped** (4,90 s) |
| `.venv\Scripts\python.exe -m pytest -q tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py` | **192 passed** (45,69 s) |
| `.venv\Scripts\python.exe -m ruff check sky_claw/ scripts/ tests/` | All checks passed |
| `.venv\Scripts\python.exe -m ruff format --check sky_claw/ scripts/ tests/` | 1120 files already formatted |
| `.venv\Scripts\python.exe -m mypy sky_claw/ scripts/` | Success: 457 source files |
| `git diff --check` | sin errores |

**Test agregado (uno solo), con su mutante medido.** Se agregó
`test_escritura_externa_con_comparticion_plena_queda_bloqueada`: un escritor
`CreateFileW(GENERIC_WRITE, share=READ|WRITE|DELETE)` con el lock retenido, más
un control positivo que exige `ESCRIBIO` después de `cerrar()`.

| Constante mutada | Tests que mueren |
|---|---|
| `COMPARTICION_EXCLUSIVA = 2` (`FILE_SHARE_WRITE`) | **sólo** `test_la_comparticion_propia_es_ninguna` y el test nuevo; toda la suite de comportamiento sigue verde |
| `COMPARTICION_EXCLUSIVA = 4` (`FILE_SHARE_DELETE`) | borrado externo y `ReplaceFileW` |
| `ACCESO_EXCLUSIVO` sin `DELETE` | **sólo** `test_la_comparticion_propia_es_ninguna` |

Es la razón del test: los escritores previos abren por el CRT, cuyo
`dwShareMode` es `READ|WRITE`, y quedan bloqueados **aunque** la exclusión
concediera `WRITE`, porque el acceso propio incluye `DELETE` y el CRT no lo
comparte. Sin este caso, una regresión de `dwShareMode` que admita escritores
reales deja la suite entera en verde. Se descartaron tres tests que se habían
escrito antes en esta fase (truncado, `ReplaceFileW` y alias) porque **ninguno
mata un mutante propio**: el truncado no se alcanza (el `open` se deniega
primero), y los otros dos mueren exactamente donde ya morían los existentes.

## 17. Estado de CI

Pendiente al redactar: se ejecuta al abrir el PR (Draft). Los gates locales son
§16.

## 18. Colisiones con otros PRs

- No había PR abierto para `test/p7-a5-compat-mo2` (`gh pr list` vacío).
- La rama está 13 commits detrás de `origin/main`; no se hizo merge para no mover
  la base fijada por el encargo.
- No se tocó el worktree ni la rama de #947.

## 19. Preflight productivo (read-only)

Se ejecutó el inspector oficial, sin escribir nada:

```powershell
.venv\Scripts\python.exe scripts\p7_readonly_inspection.py `
  --storage-root G:\Modding\FrozenRuntime `
  --mo2-instance "%LOCALAPPDATA%\ModOrganizer\Skyrim Special Edition" `
  --config <config.toml> --clone-id clone-p7-01 --mostrar-rutas
```

**Veredicto: `BLOCKED` · `listo_para_p7: False` · exit code 1.**

| Hallazgo del inspector | Valor medido |
|---|---|
| Desired runtime | `clone=None generation=None root=None`: el Desired **no se resuelve a una raíz** |
| Clone objetivo | `clone-p7-01`, `PROVISIONED`, generación `1.6.1170.0__7949c58cfd27`, `G:\Modding\FrozenClones\clone-p7-01` |
| Versión del Clone | `SkyrimSE.exe` **1.6.1170.0**; `skse64_loader.exe` presente |
| `gamePath` del INI | `G:\Modding\Skyrim_Runtime_1.6.1170` · ¿es el Clone? **no** |
| `skyrim_path` de `config.toml` | `G:\SteamLibrary\steamapps\common\Skyrim Special Edition` · ¿es el Clone? **no** |
| Divergencia de config | `config_divergence_detected`: `config_skyrim_path` apunta fuera de los runtimes admitidos para `strict_provider_idle` |
| Managed Source | `managed_source_not_found` |
| Estado del journal | `ABSENT`; sin recuperaciones pendientes |
| Procesos que invalidan | `live_mo2_running: mo2:12452` |
| Config del Clone | ningún candidato existe en el Clone (3 rutas comprobadas) |

**Incidente observado (Anexo D):** durante el preflight había un
`ModOrganizer.exe` vivo, `pid 12452`, iniciado 18:35:57, ruta
`E:\SkyClaw_R0_Rig\mo2\ModOrganizer.exe` (instalación de **rig**, no la
productiva). No lo lanzó ningún comando de esta sesión principal; los revisores
en vuelo ejecutaron sondas y su origen no se pudo atribuir. **No se cerró** (no
se intervienen procesos ajenos). El veredicto `BLOCKED` es parcialmente
atribuible a esa condición.

**Integridad verificada tras el incidente:** `modorganizer.ini` conserva
`sha256 87C6B486…F11A24` y `mtime 2026-11-14T18:32:11`, y `config.toml` conserva
`sha256 3E66978A…E40B7`. Ninguna superficie productiva cambió.

## 20. Próximas acciones que requieren aprobación del propietario

Las acciones que hacen pasar el dictamen son las P7-A…P7-G que el propio
inspector enumera (`scripts/p7_readonly_inspection.py`), y el criterio de
aceptación durable es **ADR 0017 §8**. Hoy `listo_para_p7 = False`.

| ID | Operación | Escribe | Aprobación |
|---|---|---|---|
| A1 | Aprovisionar VM Windows desechable con snapshot | nada productivo | **Propietario** (sin esto no hay GO posible) |
| A2 | En la VM: MO2 2.5.2, instancia **GLOBAL** con usuario Windows independiente, `gamePath`/perfil/ejecutables sintéticos | superficie de la VM | Propietario |
| C1 | MO2 cerrado: adquirir y liberar el lock sobre el INI sintético | INI de la VM | Propietario |
| C2 | MO2 arrancando con el lock retenido: registrar su reacción | INI de la VM | Propietario |
| C3 | MO2 abierto antes: intentar la adquisición y observar | INI de la VM | Propietario |
| C4 | Cambiar una preferencia inocua desde la GUI de la VM, con y sin lock | INI de la VM | Propietario |
| F1 | Corregir `skyrim_path` divergente en `config.toml` | `%USERPROFILE%\.sky_claw\config.toml` | Propietario; **prohibido** en esta fase |
| F2 | Corregir entrada SKSE #6, SSEEdit #7, Pandora #8 y Explore++ #3 | **`modorganizer.ini` productivo** — la superficie cuya escritura este informe no pudo probar contra MO2 | Propietario; **prohibido** en esta fase y no autorizable antes de A1–C4 |
| F3 | Resolver el Desired runtime (raíz no resuelta) | estado de Frozen Runtime | Propietario |

Nada de lo anterior se ejecutó. `EMISION_PRODUCTIVA_HABILITADA` no se consultó
ni se cambió.

---

## Anexo A — Qué se ejecutó y qué no

**Ejecutado (superficies desechables o lectura):** suite P5-C2 y el test nuevo;
matriz Win32 con procesos externos; sonda Qt en `E:\Skyclaw_Rig_MO2_Compat`;
inspector read-only; hashing del binario MO2; preflight del INI real.

**No ejecutado:** `ModOrganizer.exe` (por esta sesión); binder productivo;
transición A5; Skyrim, Steam, SKSE; escrituras en `G:\…`, en el MO2 operativo,
en `%LOCALAPPDATA%\ModOrganizer` o en `.sky_claw\config.toml`.

## Anexo B — Nota metodológica (confundidores corregidos)

1. **Caché de `QConfFile` por proceso.** La primera sonda medía la lectura desde
   el proceso que había autorado el INI y reportaba `NoError` con 34 claves:
   leía el caché. Se corrigió con un cliente por escenario.
2. **Colisión de archivos de coordinación.** La primera versión liberaba el lock
   antes de tiempo porque tenedor y cliente compartían `ready`/`release`.
3. **`errno` del CRT vs código Win32.** `open()` informa `13`; el código de
   compartición se ve con `CreateFileW` (`32`). El informe separa las capas.
4. **INI sintético no canónico.** Un INI escrito a mano con `1\title=` no es lo
   que Qt interpreta (la raíz de Qt *es* `[General]`). Se pasó a autorarlo con la
   propia API de Qt.
5. **Tres tests descartados.** Se escribieron truncado, `ReplaceFileW` y alias;
   el análisis de mutantes mostró que ninguno mata un mutante propio, y sus
   docstrings afirmaban más de lo que probaban. Se retiraron (ver §16).

## Anexo C — Datos que NO se publican

- La copia del INI real **no** se versiona: contiene el perfil y las rutas del
  operador. El preflight se cita con los campos necesarios, sin transcribir el
  archivo.
- No se transcriben SID, `telegram_chat_id`, tokens ni credenciales. Las rutas de
  cuenta se expresan como `%LOCALAPPDATA%`.
- Las sondas sí se versionan (precedente del repo: `docs/validation/**/probe/`);
  sus salidas crudas contienen rutas del laboratorio, no datos del operador.

## Anexo D — Incidente: proceso MO2 de rig durante el preflight

- Hecho: `ModOrganizer.exe` pid 12452 vivo, inicio 18:35:57, ruta
  `E:\SkyClaw_R0_Rig\mo2\ModOrganizer.exe`.
- Efecto: el inspector lo reporta como `live_mo2_running` y el preflight queda
  `BLOCKED`.
- Atribución: **no** lo lanzó ningún comando de esta sesión principal. Los
  revisores en vuelo ejecutaron sondas; no se pudo atribuir con certeza.
- Acción: **ninguna** sobre el proceso (no se cierran procesos ajenos).
- Verificación de daño: las dos superficies productivas conservan hash y mtime
  (§19). Sin cambios.
- Recomendación: antes de repetir el preflight, el propietario debe confirmar que
  no hay MO2 vivo; y ninguna fase futura debe lanzar MO2 fuera de la VM de A1.
