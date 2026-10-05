## Contexto

Revisión de la toolchain completa de DynDOLOD (etapa 9) sobre
`main` @ `c34cd3ddc54dd7b9befc190cb043d27b316c6137` (2026-09-15). No es una
auditoría de prosa: los hallazgos marcados **CONFIRMADO** se reprodujeron con una
sonda ejecutada, los **MEDIDO** salen de correr los gates del repo, y los
**LECTURA** son de inspección y lo declaran.

**La toolchain funciona.** Evidencia corrida en esta revisión:

| verificación | resultado |
|---|---|
| `pytest tests/test_dyndolod_{service,workspace,handoff_durable,taxonomia_log,uia_preflight}.py` | 737 passed, 10 skipped |
| `pytest tests/test_{output_targets,contrato_argumentos_cli,resume_hardening,dispatcher_dependencies,ritual_dispatch,dir_rollback,borrado_recursivo,links}.py` | 287 passed, 19 skipped |
| `ruff check` + `ruff format --check` (4 módulos + strategy) | limpio |
| real rig commiteado `docs/validation/2026-09-13_pr580_real_rig/final-report.md` | T5-v2 **10/10 PASS** sobre TexGenx64/DynDOLODx64 Alpha-209 reales |

Ninguno de los nueve hallazgos contradice ese veredicto. Regla de este issue,
tomada de #576: **no implementar un fix sin reproducción o invariante rota
concreta**; lo que ya está reproducido se marca como tal.

Archivos en alcance: `tool_strategies/generate_lods.py`,
`tool_dispatcher.py`, `dyndolod_service.py`, `dyndolod_runner.py`,
`dyndolod_workspace.py`, `dyndolod_uia_preflight.py`,
`validators/preflight.py`, `validators/texgen_visibility.py`.

---

## 1. `preservado_para_deployment` es un parámetro muerto — CONFIRMADO (P1, señal/contrato)

`_cerrar_tx_tras_rollback` (`dyndolod_service.py:867`, parámetro en `:876`) tiene
una rama entera —exclusión del universo medido (`:895`) y un `logger.warning`
redactado a mano (`:903-918`)— que **nunca se ejecuta**: ningún caller
productivo ni test pasa el parámetro.

`execute()` descarta el valor de retorno de `_preservar_mod_de_texgen`
(`:1559-1564`) y los cuatro `_cerrar_tx_tras_rollback` (`:1847`, `:1964`,
`:2044`, `:2067`) lo omiten.

Consecuencia hoy (camino F-02, certificación bajo lease fallida: el mod se
preserva y se relanza `DynDOLODExecutionError` → handler de `:1847`): un
move-aside **confirmado a propósito** se cuenta como rollback no resuelto
(`commit()` ⇒ `_enabled=False` ⇒ `rollback_completed=False`) y la corrida cierra
con `CRITICAL "rollback INCOMPLETO … revisar backups move-aside"`, que es
exactamente lo que el docstring de `_preservar_mod_de_texgen` dice que no hay que
afirmar.

Sonda ejecutada (misma forma que `tests/test_dyndolod_service.py:170`):

```
--- SIN preservado_para_deployment (como llama execute() hoy) ---
CRITICAL: DynDOLOD (stage 9): rollback INCOMPLETO tras error de dominio (TX 42):
          la TX queda PENDIENTE; revisar backups move-aside y targets no cubiertos manualmente.
--- CON preservado_para_deployment (parámetro hoy muerto) ---
WARNING:  DynDOLOD (stage 9): TX 42 queda PENDIENTE con una mutación PRESERVADA a propósito
          tras error de dominio: '/mods/TexGen Output' contiene la salida de TexGen de esta
          corrida y espera que el operador la materialice en el Data. NO es un rollback incompleto.
```

**Segunda mitad, y es la que decide el fix.** El docstring de
`_cerrar_tx_tras_rollback` afirma *"La TX queda PENDIENTE y `rolled_back` en
False, que es la verdad: hay una mutación viva en disco"*, pero el código de esa
rama **no devuelve False**: devuelve `rolled_back`, calculado antes. Cablear el
parámetro sin tocar el retorno cambia el log y deja la contradicción intacta —con
cobertura completa se llegaría a `mark_transaction_rolled_back(tx_id)` sobre una
mutación preservada.

### Aceptación

- [ ] Capturar el path que devuelve `_preservar_mod_de_texgen` y pasarlo en los 4 call sites.
- [ ] Decidir explícitamente el retorno de la rama preservada (False si se le cree al docstring) y alinear docstring/código.
- [ ] Test que ancle **las dos** mitades: el nivel/mensaje del registro y el valor de `rolled_back`.
- [ ] Verificar que el camino `_CertificacionPreservadaError` (que no llama a `_cerrar_tx_tras_rollback`) no necesite el mismo tratamiento.

## 2. El packaging borra el mod anterior ANTES de medir el espacio — LECTURA (P2, pérdida acotada)

`_empaquetar_sincrono` (`dyndolod_runner.py:1444-1486`) ejecuta, en este orden:
`rmtree_link_aware(mod_path)` (`:1449`) → `_bytes_del_arbol` +
`_espacio_libre_en` (`:1458-1460`) → `copytree` (`:1479`).

El comentario declara *"Se mide DESPUÉS de liberar el mod previo, que es espacio
realmente recuperable"* y el mensaje de error promete *"El staging raw y sus
backups quedan intactos"*. Cierto para el staging; **no para el mod previo**:
cuando el chequeo ENOSPC dispara, `mods/DynDOLOD Output` ya fue borrado. Ídem
ante cualquier fallo de `copytree` a mitad (permiso, AV, path largo): destino
parcial y estado anterior inexistente.

Hoy lo sostiene el `DirectoryRollback` del servicio, pero sólo con
`create_snapshot=True` y sólo si la lease sobrevive
(`should_rollback=_conserva_las_leases`). Con `create_snapshot=False` —que el
docstring de `execute` describe como renuncia válida del operador— el mod previo
se pierde sin red.

### Aceptación

- [ ] Reproducir con un test: destino con espacio insuficiente + mod previo poblado + `create_snapshot=False`; afirmar qué sobrevive.
- [ ] Medir antes de borrar (descontando lo recuperable) o copiar a staging hermano y renombrar al final.
- [ ] Corregir el comentario/mensaje si la decisión es otra.

## 3. El packaging mide link-aware y copia link-following — CONFIRMADO (P2)

Mismo bloque: el presupuesto usa `_bytes_del_arbol` → `iter_archivos_propios`,
que **no atraviesa** enlaces (política documentada en `app/security/links.py`),
mientras la copia usa `shutil.copytree(src, dst)` pelado (`:1479`), que **sí** los
atraviesa. En Windows el caso relevante es el junction: `copytree` decide con
`os.path.islink`, que no reconoce `IO_REPARSE_TAG_MOUNT_POINT` —la ceguera que
`links.py` declara cerrada para el borrado (#404/#405) y para la medición, y que
acá sigue abierta para la copia.

Sonda ejecutada:

```
_bytes_del_arbol(staging) = 100 bytes (1 archivos propios)
copytree copió            = 5100 bytes (2 archivos)
```

Consecuencias: presupuesto ENOSPC sub-medido (disco lleno a mitad de copia, justo
el caso que el chequeo existe para evitar), contenido ajeno empaquetado dentro del
mod, y ciclo si el enlace apunta a un ancestro.

Cobertura existente: T1–T5 de `tests/test_dyndolod_service.py:6679-6860` ponen el
junction en la **cadena de ancestros** entre el root y la fuente. No hay ningún
caso con un enlace **dentro** del árbol que se copia. `_exigir_fuente_del_subroot`
y `exigir_contencion_fisica` validan la cadena root→leaf, no los descendientes de
la fuente.

Hermanos que sí se protegen (defecto hermano del repo, mismo paquete):
`profile_sandbox._materialize` corre `_reject_symlinks` antes de cada `copytree`
(`profile_sandbox.py:314-320`); `grass_profile` usa `copy_function=shutil.copy2`.

### Aceptación

- [x] Test con junction/symlink real **dentro** del staging (usar `tests/_symlink_guard.crear_junction`) que hoy falle.
- [x] Escaneo fail-closed de la fuente con la familia link-aware existente, o copia link-aware propia.
- [x] Que el presupuesto ENOSPC y la copia midan/copien el mismo conjunto.

> **RESUELTO por PR #686** — merge commit `7684c92a4205a2d53aa47972be126282e093cecf` (PR HEAD `7374d609`).
> Qué lo cerró:
>
> - pre-scan fail-closed de descendientes (`exigir_arbol_copiable_sin_reparse` en `app/security/links.py`) **antes** de `rmtree`/`mkdir`/`copytree`;
> - rechazo de symlink, junction y reparse tag no clasificado: un enlace descendiente ya no llega a la copia;
> - `inventory == copyable` sobre árboles admitidos (el presupuesto link-aware y la copia ya operan sobre el mismo conjunto);
> - lifecycle del scan resistente a cancelación: cancel #1/#2 esperan scan terminal antes de propagar, y el worker mutante no arranca;
> - retry acotado de inspecciones transitorias (`_scandir_con_reintento` + `link_kind_and_identity_or_raise_with_retry`, locks AV/indexer) y tests directos del primitivo (`TestExigirArbolCopiableSinReparse`, `tests/test_links.py`).
>
> **Límite que sigue vigente:** el pre-scan **no se declara race-proof** frente a un actor externo que reemplace una entrada entre el scan y `copytree`; la copia con validación por entrada/handles queda como follow-up independiente.
> Este sub-hallazgo queda cerrado; **#592 permanece abierto** por los demás hallazgos (1, 2, 4–9; el finding 2 —borrado previo a ENOSPC— sigue abierto).

## 4. El fallo más probable del rig diagnostica genérico — LECTURA (P2, UX)

`docs/pending_ooda_status.md` §"TexGen: preset persistido puede desviar
`OutputPath`" sigue **OPEN** y medido dos veces (rig 2026-08-11 y T5-v2
2026-09-10): el preset rancio hace que la GUI escriba fuera del staging
administrado. La contención existe y es correcta (born-empty + `_tiene_artefacto`
⇒ falla cerrada), pero el operador recibe *"no se encontró el artefacto"* tras
45 s (TexGen) o 4 min (DynDOLOD) sin puntero a la causa real.

Mejora aditiva, no cambia ningún gate: cuando `_tiene_artefacto` falla, leer el
`OutputPath=` del preset persistido
(`<exe>/Edit Scripts/DynDOLOD/Presets/DynDOLOD_SSE_{TexGen,Default}.ini`) y
nombrarlo en el error. Es el paso intermedio mientras #528 (UIA output gate) siga
abierto; `dyndolod_uia_preflight.py` ya modela el veredicto en tres valores
(`MATCH`/`MISMATCH`/`UNKNOWN`) para no afirmar lo que no se puede.

### Aceptación

- [ ] Diagnóstico que nombre el `OutputPath=` del preset cuando el artefacto no aparece en el subroot administrado.
- [ ] No convertir ese diagnóstico en veredicto: el gate sigue siendo el artefacto.

## 5. T5 del argv quedó respondido por el rig y el código sigue diciendo "pendiente" — MEDIDO (P2, docs + ancla faltante)

`_switch_de_ruta` (`dyndolod_runner.py:2136-2159`) y
`tests/test_contrato_argumentos_cli.py:995-1030` declaran **Pendiente de T5** la
divergencia CRT/Delphi: con rutas con espacios `list2cmdline` duplica el backslash
final y no se sabe si el binario lo tolera. Tres opciones candidatas escritas,
ninguna elegida (*"No lo damos por resuelto"*).

El rig commiteado del 2026-09-13 la responde. `dyndolod-command.txt` (línea real
enviada):

```
"-o:E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD\\" "-d:C:\Modding\DynDOLOD RigTest\_rig_test\data\\" …
```

`dyndolod-log.txt:12` / `texgen-log.txt:11` (lo que el binario parseó):

```
Using Output Path: E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD\
Using Skyrim Special Edition Data Path: C:\Modding\DynDOLOD RigTest\_rig_test\data\
```

Backslash duplicado **tolerado y normalizado** en los dos binarios, con espacios,
en `-o:`, `-d:`, `-m:` y `-t:`: es la opción A. La evidencia está en el árbol y es
auditable; lo que falta es el cierre del ítem.

Segunda mitad: el binario **ecoa** el `-o:`/`-d:` efectivos en su log y
`_parse_log` hoy sólo los loguea como evidencia (`:2864-2866`) sin afirmar nada.
Comparar esas líneas contra el root administrado y el `data_dir` de la config
(sobre la ventana ya atribuible a la corrida) cierra el falso verde U-01 —"generó
LODs del juego base y reportó éxito"— con la autoridad del propio binario.

### Aceptación

- [ ] Actualizar docstring de `_switch_de_ruta`, docstring del test de divergencia y el inventario citando `validation/2026-09-13_pr580_real_rig/`.
- [ ] Decidir si el test de divergencia se mantiene como ancla de la serialización o pasa a afirmar el vector verificado.
- [ ] Evaluar el ancla `Using Output Path:`/`Using … Data Path:` en el post-check (fail-closed o warning: decidir y justificar).

## 6. `generate_lods` sin `ErrorWrappingMiddleware`/`DictResultGuard` + tramo del service desprotegido — LECTURA (P3, estructura)

`tool_dispatcher.py:258-261` registra la tool con `middleware=[gate]`. Sus cuatro
hermanos de pipeline (LOOT, xEdit, Synthesis, Grass Cache) llevan los tres.

`DynDOLODPipelineService.execute` cubre casi todo con `except Exception`
(`:2060`), pero **no** el tramo anterior al `try` principal: `preflight.run()`
(que a su vez no envuelve sus sensores, `preflight.py:234-310`), la resolución de
`_consultar_resume`/`reconciliar_orphan_de_artifact` (SQLite real) y
`_publish_started`. Una excepción ahí sale del dispatcher sin dict estructural y
la GUI la aterriza con el catch-all de `run_ritual`
(`ritual_runner.py:838-849`) como *"El ritual «dyndolod» falló: `OSError`"*, sin
`preflight`, sin `reason`, sin eventos.

### Aceptación

- [ ] Registrar `generate_lods` como sus hermanos (el ancla `test_gate_hitl_envuelve_exactamente_las_siete_tools_destructivas` no se rompe: enumera membresía del gate, no la lista completa).
- [ ] Cerrar el tramo desprotegido del service. Las dos mitades, o ninguna.

## 7. El sensor de VFS del preflight corre en el hilo del event loop — LECTURA (P3)

`preflight.py:244` llama `self._vfs_checker.check()` **síncrono**; los otros seis
sensores del mismo método pasan por `asyncio.to_thread` con su comentario
explicativo. DynDOLOD es justo el ritual que lo configura con
`scan_mods_dir=True` (`dyndolod_service.py:412-417`, que documenta que el `False`
hardcodeado anterior lo dejaba ciego), así que recorre el primer nivel de `mods/`
más los ancestros de game y MO2 en el hilo del loop, con la UI de NiceGUI colgada
de ese loop. Convención P2 de `.github/coding_conventions.md` incumplida en el
sensor más barato de arreglar.

### Aceptación

- [ ] `await asyncio.to_thread(self._vfs_checker.check)` + test de que ningún sensor del `run()` bloquea el loop.

## 8. mypy no cubre estos módulos y hay 7 errores latentes — MEDIDO (P3)

`pyproject.toml:225` exime `sky_claw.local.tools.*` con `ignore_errors = true`:
el gate de CI no type-checkea ninguno de los cuatro archivos de esta toolchain (el
`AGENTS.md` raíz pide verificar si el archivo está exento antes de confiar en el
gate). Corrido con un config sin la exención:

```
dyndolod_runner.py:1214  arg-type             StreamReader | None → StreamReader (proc.stdout)
dyndolod_runner.py:1215  arg-type             idem (proc.stderr)
dyndolod_runner.py:1289  assignment           results = [] sobre tuple[BaseException | None, …]
dyndolod_runner.py:2219  return-value         _firma_del_log devuelve _SIN_LOG: tuple[float | int, …]
dyndolod_runner.py:2396  assignment           de_esta_corrida: str ← str | None
dyndolod_service.py:530  func-returns-value   `p in seen or seen.add(p)` (idioma de dedupe)
dyndolod_service.py:2001 no-redef             `payload` definido dos veces (dos handlers hermanos)
```

Ninguno es bug de runtime hoy. El `:2219` es el más sustantivo: la firma del log es
la evidencia de atribución de la corrida y su tipo declarado
(`tuple[int | str, ...] | None`) no describe el centinela que realmente devuelve.
El `:1289` es la rama de drain-timeout.

### Aceptación

- [ ] Corregir los 7 y, si el volumen lo permite, sacar `sky_claw.local.tools.dyndolod_*` de la exención (o abrir el sub-issue del sprint de type-coverage).

## 9. Deuda estructural y nitpicks — LECTURA (P4)

- `execute()` mide **1149 líneas** (`dyndolod_service.py:954-2102`) con closures anidadas, cuatro handlers y tres caminos de cierre durable. Los fail-fast pre-lock (preflight, runner init, coherencia de layout, config paths, gate de perfil, consulta de resume) son ~250 líneas de la misma forma —medir, loguear con etapa, publicar completed, devolver dict— y se extraen sin cambiar semántica. La certificación bajo lease (F-02 / `_CertificacionPreservadaError`) es la otra candidata: hoy sólo se puede probar a través de `execute()`.
- Docstring stale: `_ensure_runner` (`:268-274`) documenta *"Variables de entorno requeridas: DYNDLOD_EXE, SKYRIM_PATH, MO2_PATH…"* y el cuerpo no lee env: resuelve todo por `self._path_resolver`.
- `preset` viaja como metadato (eventos, journal, preview) y **nunca se valida** contra el `Literal["Low","Medium","High"]` que su propio docstring declara.
- `_find_dyndolod_output` (`:2941-2955`) hace `any(candidato.iterdir())` sin captura de `OSError`, a diferencia de cada otra sonda del archivo; hoy sólo lo consumen tests.
- `execution_cwd = pathlib.Path.cwd()` (`:900`, `:1022`) hace que el `-t:` y los relativos del binario dependan del cwd del daemon. Los cinco switches son absolutos, así que no es un defecto demostrado: es una dependencia implícita no declarada en ningún contrato.

---

## Prioridades

| # | hallazgo | severidad | estado |
|---|---|---|---|
| 1 | `preservado_para_deployment` muerto + retorno que contradice su docstring | P1 | CONFIRMADO (sonda) |
| 2 | borrado del mod previo antes del chequeo ENOSPC | P2 | LECTURA |
| 3 | `copytree` atraviesa enlaces que la medición no cuenta | P2 | CONFIRMADO (sonda) |
| 4 | rojo genérico ante el fallo más probable (preset `OutputPath=`) | P2 | LECTURA (ítem OPEN en el inventario) |
| 5 | T5 del argv respondido por el rig, sin cerrar + ancla `Using Output Path:` ausente | P2 | MEDIDO (evidencia commiteada) |
| 6 | `generate_lods` sin ErrorWrapping/DictResultGuard + tramo del service desprotegido | P3 | LECTURA |
| 7 | sensor VFS del preflight bloquea el event loop | P3 | LECTURA |
| 8 | mypy exento, 7 errores latentes | P3 | MEDIDO |
| 9 | `execute()` de 1149 líneas, docstrings stale, `preset` sin validar | P4 | LECTURA |

## Criterio de cierre

El mismo de #576, por sub-hallazgo:

1. **REPRODUCIDO** → PR específico con test rojo y severidad por impacto.
2. **NO REPRODUCIDO / propiedad demostrada** → documentar la evidencia y cerrar sin cambio productivo.
3. **NO APLICA al camino productivo** → documentar por qué y cerrar.

## Relacionados

#556 (backlog post-DynDOLOD de runners) · #575 · #576 (metodología de
reproducción) · #528 (UIA output gate,downstream del hallazgo 4) ·
`docs/pending_ooda_status.md` (fila "TexGen: preset persistido puede desviar
`OutputPath`", OPEN) · `docs/validation/2026-09-13_pr580_real_rig/final-report.md`
(evidencia del hallazgo 5).

