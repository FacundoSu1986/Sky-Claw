# Evidencia experimental P7-A5-REAL-MO2 (Mod Organizer 2.5.2 auténtico)

Este directorio indexa la certificación experimental ejecutada el `2026-11-21` contra `ModOrganizer.exe` **2.5.2** auténtico (`SHA-256 = 442B354A8F34754DA0048654C44D27F51628FEBA54CE46C3187CF58D6C43E622`, modo `ACTIVE_GLOBAL`) frente a `sky_claw/local/frozen_runtime/mo2_ini_exclusion.py` (`FILE_SHARE_NONE`) dentro de una VM Windows 10 LTSC 2021 aislada sin red ni carpetas compartidas.

Ver el informe completo en [2026-11-21_p7_a5_real_mo2_report.md](../2026-11-21_p7_a5_real_mo2_report.md). Los resultados crudos, capturas UIA, trazas `Procmon64.exe` y snapshots de la VM se conservan intactos en el laboratorio local fuera del repositorio.

## Resumen de resultados por escenario

| Escenario | Comportamiento observado | INI persistido en disco vs. estado en memoria | Evidencia registrada en laboratorio |
|---|---|---|---|
| **A** (MO2 cerrado) | `EXCLUSION_ESTABLISHED`, sonda externa `WinError 32`, liberación limpia (`0` handles) | INI en disco 100 % byte-idéntico (`7C2BA860...`) | `result_A.json`, `handle64` |
| **B** (MO2 inicia bloqueado) | ProcMon: `SHARING VIOLATION` en `Generic Read` y `Generic Read/Write`; modal `"Error"`; al pulsar `OK` deselecciona instancia y abre `"Instance manager"` (requiere `"Switch to this instance"` al reiniciar) | INI en disco 100 % byte-idéntico durante el lock; `customExecutables` (6/6) y sentinela intactos tras switch | `result_B.json`, `procmon_ini_events.json` (808 eventos) |
| **C** (MO2 abierto inactivo) | `handle64.exe` = `0` handles abiertos en reposo; `adquirir_exclusion_ini` logra `EXCLUSION_ESTABLISHED`; `default_runtime_probes().processes()` detecta `kind="mo2"` | INI en disco 100 % preservado tras cierre; confirma que el lock solo no detecta MO2 abierto en reposo | `result_C.json`, `procmon_ini_events.json` (595 eventos) |
| **D1 / D2a / D2b** (escritura bajo contención) | `D1`/`D2a` persisten `MainWindow_tabWidget_index = 3`; `D2b` (cierre bajo lock): `SHARING VIOLATION`, salida limpia sin hang | INI en disco 100 % byte-idéntico (`BEC2DEDA...`, `customExecutables` 6/6 y sentinela intactos), **pero la edición pendiente en memoria (`"Data"` = `2`) se pierde silenciosamente al salir** | `result_D1.json`, `result_D2.json`, `procmon_ini_events.json` (1520 eventos) |
| **E / F1–F4** (fallos y recuperación) | `F1` (`WinError 32`), `F2` (`WinError 5`), `F3` (`os._exit(9)`), `F4` (`timeout rc=3`): todos rechazados/fallan cerrado y se recuperan limpiamente | INI 100 % intacto (`sha_intact = True`) | `result_EF.json` |

## Veredicto y límites de alcance

**`P7_A5_REAL_MO2_GO`** condicionado (sin autorizar transición ni emisión productiva):
1. El pre-chequeo `oracle.default_runtime_probes().processes()` + `FILE_SHARE_NONE` es necesario porque MO2 en reposo no retiene handle abierto sobre el INI (C), **pero no garantiza quiescencia durante toda la transición**: existe una ventana no atómica entre observar los procesos vivos y adquirir el bloqueo.
2. `FILE_SHARE_NONE` protege el archivo ya persistido en disco, no los cambios pendientes en memoria si MO2 se cierra bajo bloqueo (D2b).
3. Acotar la exclusión a ventanas breves con MO2 cerrado (B) y mantener `EMISION_PRODUCTIVA_HABILITADA = False`.
