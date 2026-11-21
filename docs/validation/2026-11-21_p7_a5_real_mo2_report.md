# CERTIFICACIÓN EXPERIMENTAL P7-A5-REAL-MO2 — MOD ORGANIZER 2.5.2 AUTÉNTICO FRENTE A `FILE_SHARE_NONE`

**Fecha:** 2026-11-21  
**Misión:** `P7-A5-REAL-MO2`  
**Estado experimental emitido:** **`P7_A5_REAL_MO2_GO`** (condicionado; **NO** autoriza transición productiva de P7-A5)  
**Rama / Base:** `test/p7-a5-real-mo2-compat` sobre `origin/main` = `dae85a25f5adcde005a3ce9a4e8b38ca6fbc0e85` (sincronizada posteriormente con `origin/main` `959a61114be7d8ec2d1b6c66f5669d195b739bb7`)  
**`EMISION_PRODUCTIVA_HABILITADA`:** `False` (sin cambios)

---

## 1. Resumen ejecutivo

Se ejecutó la certificación experimental de `ModOrganizer.exe` **2.5.2** auténtico (modo `ACTIVE_GLOBAL`) frente al mecanismo real de exclusión de Sky-Claw (`sky_claw/local/frozen_runtime/mo2_ini_exclusion.py`, `CreateFileW` con `dwShareMode = 0` / `FILE_SHARE_NONE`) dentro de una máquina virtual VMware Workstation 17.6.2 completamente aislada (`P7A5-RealMO2`), sin red, sin carpetas compartidas y sin acceso a discos o instancias productivas del host.

- **Preservación del `ModOrganizer.ini` ya persistido en disco vs. pérdida de cambios pendientes en memoria:**
  - **En disco:** el `ModOrganizer.ini` persistido previamente quedó **100 % preservado byte-a-byte** frente a bloqueos en todos los escenarios (`A`, `B`, `C`, `D1`, `D2`, `E/F`), sin truncamiento, sin corrupción estructural y manteniendo intactas las 6 entradas de `customExecutables` (`SKSE`, `SSEEdit`, `Pandora` + 3 defaults), `gamePath`, `selected_profile` y la clave desconocida `[CustomLabSentinel] sentinel_key=preserve_me_v1`.
  - **En memoria (cambios pendientes aún no volcados):** la preservación del archivo en disco **no equivale** a preservar ediciones pendientes de una sesión viva. En **D2b**, cuando MO2 cerró mientras `FILE_SHARE_NONE` seguía retenido, `QSettings::sync()` falló con `SHARING VIOLATION` y el INI en disco permaneció intacto, **pero el cambio pendiente en memoria (`MainWindow_tabWidget_index` cambiado de `3` a `2`) se perdió silenciosamente al salir el proceso** sin aviso bloqueante al usuario ni reintento tras la liberación.
- **Dos hallazgos operativos reales en MO2 2.5.2 (no visibles con `QSettings` sintético):**
  1. **Escenario B (arranque con INI bloqueado):** MO2 muestra el diálogo modal `"Error"` (`"Cannot open instance 'Skyrim Special Edition', failed to read INI file ..."`). Al pulsar `OK`, **deselecciona la instancia activa** y abre `"Instance manager"`; el reinicio posterior también abre `"Instance manager"` y exige seleccionar `"Skyrim Special Edition"` → `"Switch to this instance"`, tras lo cual recupera la instancia completa sin daño al INI en disco.
  2. **Escenarios C y D2b (MO2 vivo en reposo / cierre bajo bloqueo):** en reposo, MO2 **no retiene ningún handle Win32 abierto** sobre `ModOrganizer.ini` (`handle64.exe`: `No matching handles found.`), por lo que `adquirir_exclusion_ini()` logra `EXCLUSION_ESTABLISHED` aun con MO2 abierto. Si MO2 se cierra mientras `FILE_SHARE_NONE` está activo (`D2b`), el archivo en disco no se corrompe, pero la edición en memoria se pierde silenciosamente.
- **Alcance y límite sobre quiescencia:** combinar la observación de procesos vivos (`default_runtime_probes().processes()`) antes de adquirir `FILE_SHARE_NONE` acota el solapamiento con MO2 abierto en reposo (Escenario C), pero **no garantiza quiescencia durante toda la transición**: existe una ventana no atómica entre observar los procesos y adquirir el bloqueo sobre `ModOrganizer.ini`, además de no impedir que MO2 pierda cambios en memoria si estuviera abierto al producirse un cierre bajo lock o que caiga en `"Instance manager"` si el usuario lo inicia mientras el lock está retenido.

---

## 2. Regla epistemológica aplicada (`HECHO OBSERVADO` / `RESULTADO DE TEST` / `INFERENCIA` / `NO VERIFICADO`)

- **HECHO OBSERVADO:** Evidencia directa capturada en disco/memoria/UI/ProcMon/Handle64 dentro de la VM aislada o en hashes del host.
- **RESULTADO DE TEST:** Aserciones verificadas por `harness_validacion.py`, el runner experimental del laboratorio y comparación criptográfica SHA-256.
- **INFERENCIA:** Deducción técnica basada en hechos observados o lectura de código fuente sin observación directa de cada instrucción en runtime.
- **NO VERIFICADO:** Aspectos fuera del alcance de esta corrida experimental.

---

## 3. Alcance y límites de seguridad

- **HECHO OBSERVADO:** El baseline criptográfico del host (`host_baseline_pre.json` vs `host_baseline_post.json`) arrojó **`diffs = 0`**, contador de entradas de nivel superior del clon de referencia intacto (`31`), `Total running VMs: 0` al finalizar y `0` procesos prohibidos en ejecución en el host (`ModOrganizer`, `SkyrimSE`, `skse64_loader`, `SSEEdit`, `Pandora`).
- No se modificaron instalaciones productivas de Skyrim SE/Steam, `FrozenRuntime`, clones del runtime, el MO2 productivo, `%LOCALAPPDATA%\ModOrganizer` del usuario anfitrión, `config.toml`, `appmanifest_489830.acf` ni las VMs históricas existentes.

---

## 4. Fuentes canónicas inspeccionadas

- [`AGENTS.md`](../../AGENTS.md), [`docs/agents/completion_policy.md`](../agents/completion_policy.md), [`.github/coding_conventions.md`](../../.github/coding_conventions.md)
- [`docs/adr/0017-mo2-exclusion-gate.md`](../adr/0017-mo2-exclusion-gate.md)
- [`docs/validation/2026-11-01_p5c2_mo2_exclusion_gate.md`](2026-11-01_p5c2_mo2_exclusion_gate.md)
- [`docs/validation/2026-11-17_p7_a5_compat_report.md`](2026-11-17_p7_a5_compat_report.md) y [`docs/validation/2026-11-17_p7_a5_compat_mo2/README.md`](2026-11-17_p7_a5_compat_mo2/README.md)
- [`docs/operations/p7_golden_master_runbook.md`](../operations/p7_golden_master_runbook.md)
- [`sky_claw/local/frozen_runtime/mo2_ini_exclusion.py`](../../sky_claw/local/frozen_runtime/mo2_ini_exclusion.py), [`sky_claw/local/frozen_runtime/runtime_clone_binder.py`](../../sky_claw/local/frozen_runtime/runtime_clone_binder.py), `effective_runtime/{oracle.py,models.py}`
- [`tests/test_p7_a5_compat_probe_harness.py`](../../tests/test_p7_a5_compat_probe_harness.py)

---

## 5. Fase 0 — Infraestructura y aislamiento

- **HECHO OBSERVADO:** Hipervisor real disponible en el host: **VMware Workstation 17.6.2 build-24409262** (`Monitor Mode: ULM`, `VMAuthdService = Running`), a diferencia de lo registrado en el informe previo del 2026-11-17 donde figuró ausente/no operativo.
- **HECHO OBSERVADO:** Se aprovisionó una VM descartable nueva desde cero (`P7A5-RealMO2`, Windows 10 Enterprise LTSC 2021 Eval x64 desde ISO oficial SHA-256 `E4AB2E3535BE5748252A8D5D57539A6E59BE8D6726345EE10E7AFD2CB89FEFB5`, disco virtual independiente de 80 GB, 2 vCPU, 4096 MB RAM, EFI Secure Boot).
- **RESULTADO DE TEST:** Auditoría del `.vmx` (`passed: true`) y verificación dentro del guest (`guest_env_check.json`):
  - `ethernet0.present = "FALSE"` (`ipconfig_has_ipv4 = false`)
  - `sharedFolder.maxNum = "0"` (`shared_folders_accessible = false`, `WinError 1232`)
  - `isolation.tools.hgfs/copy/paste/dnd.disable = "TRUE"`, `tools.syncTime = "FALSE"`, `usb.present = "FALSE"`, `RemoteDisplay.vnc.enabled = "FALSE"`
  - Usuario exclusivo del guest de laboratorio: `p7a5-mo2-lab\labmo2`.

---

## 6. Fase 1 — Binarios y verificación criptográfica en staging

| Artefacto | Versión / Origen | SHA-256 | Verificación |
|---|---|---|---|
| `ModOrganizer.exe` | 2.5.2 (`Mod.Organizer-2.5.2.7z`) | `442B354A8F34754DA0048654C44D27F51628FEBA54CE46C3187CF58D6C43E622` | Coincide byte-a-byte con reporte #965 §5; `Qt6Core.dll` `6.7.1.0`, `usvfs_x64.dll` `0.5.6.1`, `portable.txt` ausente (`ACTIVE_GLOBAL`) |
| `Mod.Organizer-2.5.2.7z` | GitHub `ModOrganizer2/modorganizer` v2.5.2 | `E6376EFD87FD5DDD95AEE959405E8F067AFA526EA6C2C0C5AA03C5108BF4A815` | Verificado en staging |
| `python-3.11.9-amd64.exe` | Python Software Foundation | `5EE42C4EEE1E6B4464BB23722F90B45303F79442DF63083F05322F1785F5FDDE` | Firma Authenticode válida |
| `vc_redist.x64.exe` | Microsoft VC++ 2015-2022 x64 | `CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B` | Firma Authenticode válida |
| `Procmon64.exe` | Sysinternals | `AA0B10E936FEEA464348695F8A85A2DCAFD2CB3DABD1D47D8A57E7594783B27B` | Firma Microsoft válida |
| `handle64.exe` | Sysinternals | `24BAFCC570CC9BBB6B6E6652A57A519E0464E3996891AABA6F55299CCE20B04F` | Firma Microsoft válida |

---

## 7. Fase 2 — Baseline limpio de MO2 2.5.2 (`ACTIVE_GLOBAL`)

- **HECHO OBSERVADO:** MO2 2.5.2 abrió sin errores sobre un directorio de juego sintético dentro del guest (`C:\LabGame\Skyrim Special Edition\`, cabecera TES4 mínima de 48 bytes en `Data\Skyrim.esm`) con título `"Skyrim Special Edition – Mod Organizer v2.5.2"`.
- **HECHO OBSERVADO (`baseline_ini.json`):**
  - Ruta en guest: `C:\Users\labmo2\AppData\Local\ModOrganizer\Skyrim Special Edition\ModOrganizer.ini`
  - Tamaño: `5742` bytes | SHA-256: **`7C2BA86003FD3961CCC4F18D1DC0391924D6FCA05AB88EFC1861FFA3CF9E9985`**
  - `gameName = Skyrim Special Edition` | `gamePath = @ByteArray(C:\\LabGame\\Skyrim Special Edition)` | `selected_profile = @ByteArray(LabGlobalProfile)`
  - `customExecutables\size = 6`: entradas `1..3` (`SKSE`, `SSEEdit`, `Pandora` con `title`, `binary`, `arguments`, `workingDirectory` verificados) + entradas `4..6` por defecto de MO2.
  - Clave desconocida de preservación: `[CustomLabSentinel] sentinel_key = preserve_me_v1`.
- **Snapshots restaurables creados en la VM:** `01-win10-tools-clean`, `02-mo2-baseline-global` y `03-mo2-baseline-ready`.

---

## 8. Escenario A — MO2 cerrado bajo `FILE_SHARE_NONE`

- **RESULTADO DE TEST (`scenario_A/result_A.json`):**
  - Pre SHA-256 = Post SHA-256 = `7C2BA86003FD3961CCC4F18D1DC0391924D6FCA05AB88EFC1861FFA3CF9E9985`.
  - `holder.py` (PID `2396`) adquirió `EXCLUSION_ESTABLISHED` (`autoriza=True`, `intentos=1`, `identidad="dab4aa6c:00040000000191af"`).
  - Sonda Win32 externa (`CreateFileW(GENERIC_WRITE, share=7)`) bloqueada con `BLOQUEADO:32` (`ERROR_SHARING_VIOLATION`).
  - Tras liberar, `handle64.exe` confirmó `has_open_handles = False` (`No matching handles found.`).
- **NO VERIFICADO en A:** en esa primera corrida de `A`, `Procmon64.exe` no produjo `trace.pml` dentro de los 20 s al haberse invocado desde sesión no elevada; las operaciones `CreateFile` exactas de `ModOrganizer.exe` quedaron capturadas con `Procmon64.exe` elevado en los escenarios `B`, `C` y `D2`.

---

## 9. Escenario B — Arranque de MO2 con `ModOrganizer.ini` bloqueado

- **HECHO OBSERVADO (`scenario_B/result_B.json` y `procmon_ini_events.json`, 808 eventos sobre `ModOrganizer.ini`):**
  1. `holder.py` (PID `5636`) adquirió `EXCLUSION_ESTABLISHED` sobre `ModOrganizer.ini`.
  2. Al lanzar `ModOrganizer.exe` (PID `388`), `Procmon64.exe` registró las llamadas Win32 exactas sobre `ModOrganizer.ini`:
     - `CreateFile` (`Desired Access: Generic Read`, `Disposition: Open`, `ShareMode: Read, Write`) → **`SHARING VIOLATION`**
     - `CreateFile` (`Desired Access: Generic Read/Write`, `Disposition: OpenIf`, `ShareMode: Read, Write`) → **`SHARING VIOLATION`**
  3. MO2 mostró el diálogo modal `#32770` `"Error"`: `"Cannot open instance 'Skyrim Special Edition', failed to read INI file C:/Users/labmo2/AppData/Local/ModOrganizer/Skyrim Special Edition/ModOrganizer.ini."`.
  4. No creó archivos alternos (`ModOrganizer.ini.tmp`, `.bak`, etc.), no lanzó el asistente de primera vez y mantuvo el INI en disco byte-idéntico (`7C2BA860...`).
  5. Al liberar el lock y pulsar `"OK"` en `"Error"`, MO2 abrió la ventana `"Instance manager"` (deseleccionando la instancia global activa).
  6. Al cerrar MO2 y reiniciarlo (`PID 5408`), volvió a abrir `"Instance manager"` hasta seleccionar `"Skyrim Special Edition"` → `"Switch to this instance"`, abriendo entonces `"Skyrim Special Edition – Mod Organizer v2.5.2"` con `LabGlobalProfile`, los 6 `customExecutables`, `gamePath` y `sentinel_key` intactos (`all_sections_present = True`, `customExecutables_exact_equal = True`).

---

## 10. Escenario C — Adquisición de `FILE_SHARE_NONE` con MO2 abierto e inactivo

- **HECHO OBSERVADO (`scenario_C/result_C.json` y `procmon_ini_events.json`, 595 eventos):**
  - Con `ModOrganizer.exe` (PID `3500`) abierto en su ventana principal:
    - `handle64.exe` reportó **`has_open_handles = False`** (`No matching handles found.`): `QSettings` abre, lee/escribe y cierra el handle de `ModOrganizer.ini` de forma transitoria; **no retiene un handle abierto en reposo**.
    - En consecuencia, `acquirer.py` (`adquirir_exclusion_ini(intentos=1)`) obtuvo **`EXCLUSION_ESTABLISHED`** (`autoriza = True`) aun con MO2 abierto.
    - La sonda de procesos de Sky-Claw (`oracle.default_runtime_probes().processes()`) sí detectó **`kind="mo2"`, `pid=3500`, `exe="C:\\LabMO2\\ModOrganizer.exe"`, `indeterminate=False`**.
  - Al cerrar MO2 normalmente tras liberar el lock, el INI previo persistido y su esquema completo (`customExecutables` 6/6, `gamePath`, `selected_profile` y `sentinel_key`) quedaron 100 % preservados (`handles_after = False`).

---

## 11. Escenario D — Escritura de preferencias bajo contención (`D1` control + `D2a`/`D2b`)

- **RESULTADO DE TEST D1 (control sin bloqueo, `scenario_D1/result_D1.json`):**
  - Clic UIA en pestaña `"Saves"` (`MainWindow_tabWidget_index`: `0` → `3`), cierre limpio (`WM_CLOSE`) y reinicio: valor `3` persistido en disco y tras reinicio; `customExecutables` y claves preservados al 100 %.
- **RESULTADO DE TEST D2a (lock adquirido con MO2 abierto, cambio en UI, lock liberado ANTES de cerrar MO2, `scenario_D2/result_D2.json`):**
  - `holder_d2a` (`PID 5648`) adquirió `EXCLUSION_ESTABLISHED`; clic UIA en `"Saves"`; liberación antes del cierre; `WM_CLOSE`.
  - Resultado: `tab_actual = "3"` (`persisted = True`), 100 % de preservación de `customExecutables` y sentinela en el INI en disco.
- **RESULTADO DE TEST D2b (cambio en UI a `"Data"`=`2`, lock adquirido, cierre de MO2 MIENTRAS el lock sigue activo; `procmon_ini_events.json` 1520 eventos):**
  - `holder_d2b` (`PID 2960`) sostuvo `FILE_SHARE_NONE` durante `WM_CLOSE` de MO2 (`PID 1892`).
  - `Procmon64.exe` registró `CreateFile` (`Desired Access: Generic Read/Write`, `Disposition: OpenIf`, `ShareMode: Read, Write`) → **`SHARING VIOLATION`**.
  - **Preservación en disco:** `ModOrganizer.ini` permaneció **byte-idéntico** al snapshot previo al cierre (`BEC2DEDA8B462C259731DCE82E4CE0DF5AF82181BC0527E220411F8077FBAFA1`, `tab_on_disk_after_locked_close = "3"`), sin corrupción ni truncamiento.
  - **Pérdida en memoria (hallazgo crítico):** `ModOrganizer.exe` salió normalmente sin dejar proceso colgado (`remaining_pids = []`) y **sin avisar ni conservar en un borrador la modificación pendiente en memoria (`MainWindow_tabWidget_index` = `2`)**, por lo que ese cambio en vuelo **se perdió silenciosamente** (`silent_loss_of_in_flight_edit_on_locked_exit = True`). Al reiniciar tras liberar el lock, MO2 arrancó limpio con el estado previo en disco (`customExecutables` 6/6 y todas las claves intactas).

---

## 12. Escenarios E y F — Recuperación ante fallos y verificación cruzada de contratos

- **RESULTADO DE TEST (`scenario_EF/result_EF.json`):**
  - **F1 (archivo temporalmente bloqueado por tercero):** bloqueado (`EXCLUSION_NOT_ESTABLISHED`, `WinError 32`, `3` intentos en `0.031 s`); tras liberar el tercero, recuperado en `1` intento (`EXCLUSION_ESTABLISHED`, SHA-256 intacto).
  - **F2 (archivo `FILE_ATTRIBUTE_READONLY`):** rechazado (`EXCLUSION_NOT_ESTABLISHED`, `WinError 5`); tras restaurar permiso de escritura, recuperado (`EXCLUSION_ESTABLISHED`, SHA-256 intacto).
  - **F3 (muerte abrupta del holder con `os._exit(9)`):** el kernel cerró el handle automáticamente; la readquisición inmediata tuvo éxito (`EXCLUSION_ESTABLISHED`, SHA-256 intacto).
  - **F4 (timeout del holder `timeout_s=1.5`):** cierre fail-closed (`rc=3`, sin archivo `.released`), `harness_rejected_timeout=True`, readquisición limpia y `0` handles abiertos.

---

## 13. Veredicto experimental contra criterios de P7-A5

- **Veredicto:** **`P7_A5_REAL_MO2_GO`** (condicionado; **NO** autoriza transición productiva).
- **Justificación:**
  - `FILE_SHARE_NONE` protege la integridad física y estructural de `ModOrganizer.ini` en disco en todos los escenarios reales probados con `ModOrganizer.exe` 2.5.2 (`A`–`F`), preservando byte-a-byte `customExecutables`, `gamePath`, `selected_profile` y claves desconocidas, y falla cerrado ante errores, permisos read-only o muertes abruptas.
  - Al mismo tiempo, la certificación deja establecidas dos limitaciones conductuales reales que condicionan el diseño operativo de P7-A5:
    1. Si el usuario inicia MO2 durante la retención de `FILE_SHARE_NONE` (Escenario B), MO2 falla la carga de instancia y cae a `"Instance manager"`, requiriendo re-selección manual de instancia al reiniciar (aunque el INI en disco no sufre daño).
    2. Si MO2 estuviera abierto y se cerrara mientras `FILE_SHARE_NONE` sigue retenido (Escenarios C y D2b), el INI previo en disco se preserva intacto, **pero los cambios pendientes en memoria de esa sesión de MO2 se pierden silenciosamente**. Además, observar los procesos vivos antes de adquirir `FILE_SHARE_NONE` **no garantiza quiescencia continua durante toda la transición** porque existe una ventana no atómica entre la observación de procesos y la adquisición del bloqueo (y frente a eventos externos durante la transición).

---

## 14. Invariantes y limitaciones operativas registradas

1. **Pre-chequeo de proceso vivo + `FILE_SHARE_NONE` (insuficiencia del lock solo):** como MO2 en reposo no sostiene un handle abierto sobre `ModOrganizer.ini` (Escenario C), `FILE_SHARE_NONE` por sí solo no detecta una instancia abierta; se mantiene necesario consultar `oracle.default_runtime_probes().processes()` antes de adquirir el lock, asumiendo explícitamente que **entre la observación y la adquisición existe una ventana TOCTOU y no hay garantía de quiescencia completa durante toda la transición**.
2. **Distinción disco vs. memoria:** en contención al cierre (Escenario D2b), el gate protege el INI persistido en disco pero **no** evita la pérdida silenciosa de cambios en memoria aún no guardados por MO2.
3. **Ventana acotada:** mantener el uso de `FILE_SHARE_NONE` restringido a la ventana mínima necesaria mientras MO2 esté cerrado para reducir colisiones con arranques manuales (Escenario B).
4. **No activación productiva:** mantener `EMISION_PRODUCTIVA_HABILITADA = False`. No se introducen ni implementan mecanismos nuevos fuera de este alcance experimental.

---

## 15. Matriz de trazabilidad de artefactos

- Artefactos crudos completos del laboratorio (fuera del repositorio, conservados intactos):
  - Baselines host: `host_baseline_pre.json`, `host_baseline_post.json` (`diffs = 0`)
  - Staging y auditoría VMX: `staging_manifest.json`, `vmx_isolation_audit.json`, `guest_env_check.json`, `baseline_ini.json`
  - Corridas experimentales: `scenario_{A,B,C,D1,D2,EF}/result_*.json`, `procmon_ini_events.json` (`B`, `C`, `D2`), `uia_*.json`, `screen_*_end.png`, `mo2_logs*`
- Índice sanitizado en el repositorio: [`docs/validation/2026-11-21_p7_a5_real_mo2/README.md`](2026-11-21_p7_a5_real_mo2/README.md).

---

## 16. Riesgos residuales y no verificados (`NO VERIFICADO`)

- **NO VERIFICADO:** Comportamiento si un usuario abre *manualmente* el diálogo `Settings` o `Modify Executables` dentro de MO2 y pulsa `OK` durante la ventana de bloqueo sin cerrar MO2 (en D2 se ejerció cambio de estado de pestaña en `MainWindow` + `WM_CLOSE`, aunque ambos flujos pasan por `Settings::sync()` / `QSettings`).
- **NO VERIFICADO:** Interacción durante el intervalo exacto de carrera entre `oracle.default_runtime_probes().processes()` y `CreateFileW(..., dwShareMode=0)` si `ModOrganizer.exe` arranca simultáneamente en esa ventana.
- **NO VERIFICADO:** Plugins externos de MO2 que escriban sus propios `.ini` fuera de `ModOrganizer.ini`.
- **NO VERIFICADO:** Traza elevada de `Procmon` en el Escenario A (cubierta en `B`, `C` y `D2`).

---

## 17. Integridad del código del repositorio

- No se modificó código de producción ni scripts productivos del repositorio (`sky_claw/` intacto). Este PR publica exclusivamente el informe sanitizado y su índice de evidencias.

---

## 18. Gates de calidad y sanitización

- Verificados sobre el árbol final del PR: `ruff check`, `ruff format --check`, `mypy`, `pytest` en los tests de contrato aplicables, `git diff --check`, ausencia de BOM UTF-8 (`0xEF 0xBB 0xBF`) y ausencia de rutas de cuenta del host o credenciales.

---

## 19. Rollback / Limpieza del laboratorio

- La VM `P7A5-RealMO2` quedó apagada (`Total running VMs: 0`) con sus snapshots limpios e intactos y resultados crudos preservados fuera del repositorio. Ningún archivo productivo del host fue modificado.

---

## 20. Próximos pasos recomendados

1. Conservar este cierre en Draft PR para revisión del propietario.
2. Ejecutar en la siguiente fase el **preflight P7-A5 estrictamente read-only**, sin habilitar transición ni emisión productiva (`EMISION_PRODUCTIVA_HABILITADA = False`).
