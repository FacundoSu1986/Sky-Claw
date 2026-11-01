# P5-C2 — Gate de exclusión de MO2 y preparación de P7

**Tracker:** #672
**ADR:** [0017 — Exclusión de mutación de `modorganizer.ini`](../adr/0017-mo2-exclusion-gate.md)
**Fecha:** 2026-11-01
**Alcance:** `SYNTHETIC_ONLY`. Ninguna operación sobre Skyrim, Steam, MO2 ni SKSE productivos.

---

## 1. Rama, worktree y SHAs

| Elemento | Valor |
|---|---|
| PR | [#851](https://github.com/FacundoSu1986/Sky-Claw/pull/851) (Draft) |
| Rama | `feat/frozen-runtime-mo2-exclusion-gate` |
| Commit | `e6c58cd06c756234a17da25ff9f79cedcd51385a` |
| Worktree | `E:\Skyclaw_Main_Sync\.worktrees\frozen-runtime-mo2-exclusion-gate` |
| Base | `origin/main` `9fbf0b2fef177e143ff0be68e058ae875d501c6f` |
| P5-C1 integrado | `ac19cb28f74e6495808d7606fdbc29ceab9f578d` (ancestro de `origin/main`, verificado con `merge-base --is-ancestor`) |
| CI post-merge de P5-C1 | run `41249009598`, `success`, 12/12 |
| CI de `origin/main` al iniciar | run `41296992561`, `success` |
| Worktree de baseline para comparar | `E:\Skyclaw_Main_Sync\.worktrees\p5c2-baseline` (`--detach 9fbf0b2f`) |
| Rig de experimentos | `E:\Skyclaw_Rig_MO2_Exclusion` (fuera del repo, no se commitea) |

`main` no se tocó. El worktree principal estaba 10 commits atrás y **no** contiene
el código de P5-C1; por eso el trabajo se hizo en un worktree propio desde
`origin/main`.

## 2. Mecanismos Windows investigados

Se probaron empíricamente, con procesos externos reales, no por documentación:

| Mecanismo | Veredicto medido |
|---|---|
| `CreateFileW` + matriz de `dwShareMode` (5 combinaciones × 3 accesos) | Una sola combinación excluye mutación sin romper lectores útiles |
| `MoveFileExW(REPLACE_EXISTING)` / `os.replace` bajo exclusión | `ERROR_ACCESS_DENIED` (5): incompatible |
| `ReplaceFileW` | `ERROR_SHARING_VIOLATION` (32) bajo exclusión |
| `SetFileInformationByHandle(FileRenameInfoEx)` | Funciona **sobre el propio handle**; falla (32) sobre un destino retenido ajeno |
| `FILE_RENAME_FLAG_POSIX_SEMANTICS` | **No** salta el chequeo de compartición del destino |
| Escritura in-place por handle (`WriteFile` + `SetEndOfFile`) | Única mutación posible bajo exclusión |
| `LockFileEx` por rangos | Bloquea escritura in-place pero **no** borrado ni reemplazo: no cierra la ventana |
| Identidad de archivo por handle (`GetFileInformationByHandle`) | Estable ante rename; **igual para todos los hardlinks** → el alias también queda excluido |
| Reparse points | Detección por atributos; el gate los rechaza |

Dos defectos de interoperabilidad con ctypes se encontraron y corrigieron por
medición, no por lectura:

1. **`FILE_RENAME_INFO_EX` necesita holgura de `sizeof` (24), no `offsetof` (20).**
   Con un buffer de exactamente `20 + len(nombre)` el rename **devuelve éxito y
   corrompe el nombre** (lee 4 bytes de más). Reproducido de forma determinista
   con nombres de 64 caracteres; desaparece con 4 bytes de holgura.
2. **`FILETIME` debe declararse como dos `DWORD`.** Un `c_uint64` alinea a 8 y
   desplaza todos los campos siguientes de `BY_HANDLE_FILE_INFORMATION` cuatro
   bytes: la identidad se compara contra basura y el gate rechaza archivos sanos.

## 3. Evidencia experimental

Ver ADR 0017 §3 para las tablas completas. Resumen de lo que discrimina:

- **La exclusión se establece y es total contra mutación**: escritura, `DeleteFile`,
  `ReplaceFileW` y `MoveFileEx` externos fallan; también por alias hardlink.
- **`os.replace` es incompatible con la exclusión**: obliga a cambiar el protocolo
  de escritura, no es una preferencia.
- **El rename-away es peor que la ventana que cierra**: bajo un creador externo,
  40/40 victorias del agresor y 27/40 transiciones rotas.
- **La escritura in-place sin `DELETE` se observa rasgada**: 72 lecturas truncadas
  sobre 109.270. Con `DELETE` en el acceso propio, 0 sobre 74.689 — imposible por
  construcción, no improbable.
- **La adquisición puede fallar y el reintento lo resuelve**: 291/300 fallos sin
  reintento, 0/300 con 40 reintentos de 5 ms.

## 4. Decisión de arquitectura

`dwDesiredAccess = GENERIC_READ | GENERIC_WRITE | DELETE`,
`dwShareMode = FILE_SHARE_READ`, escritura **in-place por el handle retenido**, con
la huella del **snapshot durable** verificada contra el mismo handle antes de
mutar. Detalle y alternativas descartadas en ADR 0017 §4–§6.

La consecuencia que hubo que pagar: la escritura deja de ser atómica, y eso
**rompió el discriminador de escritura ajena** de `binder_recovery`, que estaba
calibrado para un reemplazo atómico. Un archivo a medio escribir no es ni el
snapshot ni la previsión, así que se clasificaba como ajeno y el rollback **se
negaba a restaurarlo**. Se corrigió reconociendo las dos formas de una escritura
propia a medias. Lo detectó el test de escritura parcial.

## 5. Archivos y tests

**Nuevos**

| Archivo | Qué aporta |
|---|---|
| `sky_claw/local/frozen_runtime/mo2_ini_exclusion.py` | El gate: tres veredictos, identidad de grado handle, rechazo de reparse points, reintento acotado |
| `tests/test_frozen_runtime_p5c2_mo2_exclusion.py` | 24 casos: concurrencia interproceso real, liberación, integridad, equivalencia de hermanos, ancla AST de read-only |
| `scripts/p7_readonly_inspection.py` | Inspección READ-ONLY de P7 con redacción de rutas y matriz P7-A..G |
| `docs/adr/0017-mo2-exclusion-gate.md` | La decisión y su evidencia |

**Modificados**

| Archivo | Cambio |
|---|---|
| `mo2_ini_escritura.py` | `actualizar_game_path_por_handle` (hermano del escritor por ruta) + `Protocol IniRetenido` |
| `runtime_clone_binder.py` | Adquiere el gate tras el snapshot, verifica la huella del snapshot por el handle, escribe por el handle; `BinderExclusionError`; `exclusion` en `BinderResult` |
| `binder_recovery.py` | `_es_prefijo_rasgado`: reconoce una escritura propia a medias |
| `surface_census.py` | `ANCLAS_DE_SUPERFICIE["mo2_ini"]` incluye el escritor por handle (lo exigió el ancla AST) |
| `tests/…p5c1_mo2_clone_binder.py` | Casos 10, 11 y 15 re-anclados al escritor por handle; el 11 pasa a probar escritura **parcial** real |
| `tests/…p5c1_binder_integrity.py` | Tres casos re-anclados; el de escritura ajena mueve la inyección a la ventana que ahora existe (post-liberación) |

**Anclas que se agregaron** (para que la clase de defecto no vuelva):

- `test_los_dos_escritores_producen_los_mismos_bytes`: los dos caminos de escritura
  se comparan contra `prever_bytes` en cinco casos. Un tercer escritor que no la
  use rompe el test.
- `test_los_escritores_del_ini_estan_enumerados`: igualdad literal del conjunto de
  escritores.
- `test_el_binder_no_escribe_el_ini_por_ruta`: el binder no puede volver al camino
  sin exclusión.
- `test_la_inspeccion_de_p7_no_contiene_operaciones_mutantes`: ancla AST.
- `ANCLAS_DE_SUPERFICIE` con el escritor nuevo: el censo por AST vuelve a ver al
  binder.

## 6. Regresiones y resultados

Comparación **contra el baseline `9fbf0b2f`**, cada archivo con un directorio
temporal propio y recién creado:

| Archivo | Baseline | Esta rama |
|---|---|---|
| `p1` … `p5a` (31 archivos) | verde | verde |
| `p4_root_lock` | **2 failed**, 52 passed | **2 failed**, 52 passed (idéntico) |
| `p5b_durable_authority` | 35 passed | 35 passed |
| `p5c_binder_preflight` | 57 passed | 57 passed |
| `p5c1_binder_integrity` | 38 passed | 38 passed |
| `p5c1_ini_escritura` | 34 passed | 34 passed |
| `p5c1_mo2_clone_binder` | 20 passed | 20 passed |
| `p5c1_superficies` | 18 passed | 18 passed |
| `p5c2_mo2_exclusion` (nuevo) | — | 23 passed, 1 skipped |

Los 2 fallos de `p4_root_lock` (`test_rl05_lockfile_no_vive_en_temp`,
`test_rl17_canonical_root_representaciones_equivalentes`) **fallan igual en
`origin/main` sin ningún cambio aplicado**: son preexistentes y ajenos a este
trabajo. No se tocan acá; quedan registrados para no confundirlos con una
regresión de P5-C2.

Ruff y Mypy limpios sobre los archivos tocados. `mypy` **sí** cubre el módulo
nuevo: `sky_claw.local.frozen_runtime.*` está en la lista de opt-in estricto, no
en la de `ignore_errors`.

**CI del PR #851 sobre el commit de código `e6c58cd0`** (run `41306633742`):
15 jobs `pass`, 0 `fail`. Incluye Lint Ruff py3.11/3.12, Mypy py3.11/3.12,
Security Scan, CodeQL, **POSIX Core py3.11** (el módulo importa y sus tests no
dependientes de Win32 corren fuera de Windows) y **Tests / windows-latest
py3.11 y py3.12**, que son los que ejercitan la exclusión real.

**Nota metodológica, porque afectó la lectura de los resultados.** En esta máquina
hay un hook de borrado que impide eliminar el directorio de `--basetemp`. Cuando
dos corridas comparten `--basetemp`, la segunda no puede recrearlo y **cientos de
tests fallan por infraestructura**, no por código: se observaron 129 fallos en el
baseline sin ningún cambio aplicado. Todas las cifras de la tabla usan un
directorio nuevo por archivo. Cualquier comparación futura tiene que hacer lo
mismo o va a medir el hook.

**Un caso queda `skip`**: la creación de symlinks requiere privilegio que esta
sesión no tiene (`ERROR_PRIVILEGE_NOT_HELD`, 1314). El gate **rechaza** reparse
points por atributos y el test lo verifica cuando puede crearlos; el
comportamiento de un INI enlazado queda `INDETERMINATE`, que nunca autoriza.

## 7. Cobertura de lanzadores

Censo de las entradas de Sky-Claw capaces de iniciar MO2 (y por lo tanto SKSE):

| Entrada | Ruta | Alcanza |
|---|---|---|
| `Mo2VfsClient.launch_game` | `sky_claw/local/mo2/vfs.py:477` | `ModOrganizer.exe -p <profile> moshortcut://SKSE` |
| Tool de agente `launch_game` | `sky_claw/app/agent/tools/system_tools.py:528` | delega en la anterior |
| Dispatcher de la GUI | `SupervisorAgent` → `tool_dispatcher` | delega en la tool anterior |
| Bridge de MO2 (`launch_worker`) | `sky_claw/local/mo2/plugin_bundle/…/runtime.py` | lanza *workers dentro* de MO2, no MO2 |

**Hay un punto de coordinación común** para los lanzamientos iniciados por
Sky-Claw: `Mo2VfsClient.launch_game`, alcanzado desde dos superficies (GUI y
agente LLM). Un gate ahí cubriría el 100 % de los lanzamientos propios.

**Y ese punto no alcanza**, que es la distinción que el brief pedía no confundir:
un doble clic en el Explorador, el autostart de MO2 o Steam no pasan por ahí. Por
eso la garantía **no** se apoya en controlar lanzadores: se apoya en que el
archivo no se puede mutar mientras la transición lo retiene, venga el intento de
donde venga. La cobertura de lanzadores queda como defensa en profundidad y como
material de P7, no como el mecanismo.

## 8. Riesgos abiertos

1. **El modo de compartición real de MO2 no está medido.** Lo medido es el patrón
   `share = READ|WRITE` (el del CRT de Python y el documentado para
   `QFSFileEngine`). Si MO2 abriera su INI con `FILE_SHARE_DELETE`, un lector
   podría observar el archivo durante la transición. Requisito de P7.
2. **Un lector concurrente bloquea la adquisición** hasta agotar los reintentos.
   Fail-closed, pero es una negativa, no un éxito.
3. **La escritura no es atómica.** La recuperación depende de `binder_recovery`;
   si el registro durable se corrompe, un INI a medio escribir no tiene vuelta.
   Además, el predicado de escritura rasgada cubre las dos ventanas de crash
   realistas pero no una escritura parcial que hubiera extendido el archivo
   dejando cola sin inicializar (ADR 0017 §5.1, «límite conocido»).
4. **Reparse points**: `INDETERMINATE` por falta de privilegio para crear el caso.
5. **`POWER_LOSS_GUARANTEE` sigue en `NOT_CLAIMED`.** `FlushFileBuffers` vuelca el
   archivo; no hay `fsync` de directorio ni WAL.
6. **El hook de borrado del entorno** hace que `--basetemp` compartido produzca
   falsos rojos masivos. No es del repo, pero envenena cualquier medición.

## 9. Requisitos para la sesión P7

1. Correr `scripts/p7_readonly_inspection.py` sobre el rig real y archivar el JSON
   **con rutas redactadas** (por defecto). Sin `--mostrar-rutas` en artefactos
   remotos.
2. Medir el `dwShareMode` con el que MO2 abre `modorganizer.ini` (sonda de sólo
   lectura mientras MO2 está abierto) y confirmar que la transición no le impide
   arrancar cuando la ventana está cerrada.
3. Confirmar que un lector bloqueado durante la ventana produce un error visible
   de MO2 y **no** una configuración por defecto persistida.
4. Ejecutar la matriz P7-A..P7-G. Ninguna se ejecutó en esta sesión.
5. No habilitar `EMISION_PRODUCTIVA_HABILITADA`. No hay wiring productivo.

## 10. Dictamen

```text
MO2_EXCLUSION_SYNTHETIC_READY
P7_READONLY_PREPARED
```

`MO2_EXCLUSION_SYNTHETIC_READY`: la exclusión de mutación se establece, se
verifica y se libera correctamente en rig Windows con procesos externos reales;
cierra la ventana TOCTOU entre comprobar y mutar porque ambas operaciones usan el
mismo handle; y está integrada al binder respetando su journal y sus locks, con la
activación productiva deshabilitada.

`P7_READONLY_PREPARED`: existe una herramienta de inspección read-only, anclada
por AST, con redacción de rutas y con la matriz P7-A..P7-G emitida como contrato
de la sesión siguiente.

Lo que **no** se declara: exclusión demostrada contra MO2 real (sólo contra el
patrón de compartición que MO2 usa por tecnología), `POWER_LOSS_GUARANTEE`,
`QT_QSETTINGS_EQUIVALENCE`, ni que Steam permita arrancar una versión downgrade.
