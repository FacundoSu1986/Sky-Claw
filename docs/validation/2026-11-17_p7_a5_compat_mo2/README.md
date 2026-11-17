# Sondas de P7-A5-COMPAT (2026-11-17)

> **No las ejecuta CI.** Requieren PyQt6 fuera del venv del repo y un `run_dir`
> desechable. Se versionan para que la evidencia del informe
> [P7-A5-COMPAT](../2026-11-17_p7_a5_compat_report.md) sea reproducible.

## Requisitos

- Windows (la exclusión de archivos compartidos no existe fuera de Windows).
- `probe_qsettings.py`, `qsettings_child.py` y `dump_ini.py`: un venv aparte con
  `PyQt6==6.7.1` (arrastra `PyQt6-Qt6` 6.7.3; `qVersion()` informa 6.7.3 y
  `QT_VERSION_STR` 6.7.1).
- `holder.py`, `acquirer.py` y `matrix_win32.py`: el venv del repo
  (`.venv/Scripts/python.exe`), porque importan
  `sky_claw.local.frozen_runtime.mo2_ini_exclusion`.

## Matriz Win32 (códigos medidos)

```powershell
.venv\Scripts\python.exe probe\matrix_win32.py <run_dir>
```

Escribe `<run_dir>/matrix.json`. Cada agresor corre en un proceso separado; el
tenedor usa el módulo real. Valores esperados en Windows 10 19045 (ver §8 del
informe): `DeleteFile` 32, `MoveFileExW` 5, `ReplaceFileW` 32,
`CreateFileW(GENERIC_READ|GENERIC_WRITE, share=7)` 32, escritores del CRT
`errno 13`.

## Sonda de QSettings bajo el lock

```powershell
<venv-pyqt>\python.exe probe\probe_qsettings.py `
  <repo>\.venv\Scripts\python.exe `
  <run_dir>
```

Escribe `<run_dir>/qsettings.json`. Escenarios A–E (§9 del informe). Cada cliente
corre en **proceso nuevo**: Qt cachea `QConfFile` por proceso y medir desde el
proceso que autoró el INI mide el caché, no el disco.

Resultado esperado en Qt 6.7:

| Escenario | Esperado |
|---|---|
| A: lectura con el lock retenido | `AccessError`, 0 claves, INI intacto |
| B: escritura con el lock retenido | `AccessError`, INI intacto |
| C: `sync` diferido | 1.er `sync` `AccessError`; tras liberar escribe y **conserva** las claves previas |
| D: destructor tras liberar | escribe; conserva las claves previas |
| E: objeto `QSettings` vivo | la adquisición sigue dando `EXCLUSION_ESTABLISHED` |

## Salida cruda usada por el informe

`raw/qsettings.json` y `raw/matrix.json` son las corridas citadas. Contienen
rutas del laboratorio (`E:\Skyclaw_Rig_MO2_Compat`), no datos del operador.
