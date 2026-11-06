# P5-C2 — Gate de exclusión de MO2 y preparación de P7

**Tracker:** #672
**PR:** [#851](https://github.com/FacundoSu1986/Sky-Claw/pull/851) (Draft)
**ADR:** [0017 — Exclusión de mutación de `modorganizer.ini`](../adr/0017-mo2-exclusion-gate.md)
**Fecha:** 2026-11-01 (revisión 2, tras la auditoría adversarial); cierre
2026-11-03 (sexta ronda: FIX 1 y FIX 2, §0.8) y 2026-11-04 (séptima ronda:
autoría acotada a la exclusión, §0.9)
**Alcance:** `SYNTHETIC_ONLY`. Ninguna operación sobre Skyrim, Steam, MO2 ni SKSE productivos.

---

## 0. Qué cambió esta revisión

La auditoría del PR encontró un defecto real en la afirmación central del gate.
La versión anterior concedía `FILE_SHARE_READ` y documentaba que, con eso, la
lectura rasgada era «imposible por construcción». **Era falso**, y se corrigió
midiendo, no razonando:

1. La validación de compartición de Windows es **bidireccional**. Un lector que
   pida `GENERIC_READ` y conceda `READ|WRITE|DELETE` abre aunque el tenedor sólo
   conceda `READ`. Medido: **11.038 lecturas rasgadas**.
2. Se adoptó `FILE_SHARE_NONE`, se midió su costo (casi nulo) y se agregaron
   tests que fallan con la configuración anterior.
3. `--json-salida` del inspector P7 **truncaba** `modorganizer.ini` si se le
   pasaba esa ruta. Se validó el destino y se creó con `O_EXCL`; después el
   gate final retiró la opción entera (§0.5, FIX A).
4. El predicado de escritura interrumpida se calibró **matando procesos reales**
   en cada punto, y se endureció: ya no atribuye a la transición estados que el
   escritor no puede producir.
5. **CI encontró lo que la suite local no podía.** En `windows-latest` la consola
   es cp1252 y el informe contiene «→»; `print` lanzaba `UnicodeEncodeError`
   antes de escribir el archivo, así que el informe nunca se producía. Local
   pasaba porque la consola era UTF-8. Se emite ASCII puro a stdout y UTF-8 al
   archivo, con dos tests que fuerzan `PYTHONIOENCODING=cp1252` — y que fallan si
   se revierte la corrección.
   Medido al respecto: `sys.stdout` usa `strict` y **sí** revienta;
   `sys.stderr` usa `backslashreplace` por defecto en Python y **no**. Se había
   agregado una protección de flujos y un test de ruta CJK que no discriminaban
   —stderr nunca falla— así que se quitaron: un assert que no puede fallar no es
   un ancla.

## 0.1 Hallazgos de la revisión adversarial, reconciliados

Codex revisó `efdc7d55` y dejó **7 findings**; CodeRabbit 1; Qodo 1. Todos se
verificaron contra el árbol y **todos eran reales**. Ninguno se cerró porque los
tests estuvieran verdes.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 1 | **P1** · El predicado podía atribuir a la transición un cambio de un tercero anterior a su escritura | CONFIRMADO | Discriminador de PROCESO: `escritura_intentada`. Si el binder no llegó a llamar al escritor, el estado es ajeno |
| 2 | **P1** · No reconocía la escritura completa pendiente de truncado (`antes_seteof`) | CONFIRMADO | El corte ahora admite `k == len(nuevo)`; era uno de los tres estados que el propio rig había medido |
| 3 | **P1** · `JournalDisposition` se comparaba con `startswith("pending")` y nunca matcheaba | CONFIRMADO | Comparación contra los miembros del enum (`PENDING_PROMOTION`/`PENDING_ROLLBACK`) |
| 4 | **P2** · Las raíces protegidas no se resolvían: un junction en `--storage-root` habilitaba escribir dentro | CONFIRMADO | `_canonico` resuelve enlaces también en las raíces |
| 5 | **P2** · `listo_para_p7` comparaba contra el veredicto local, no el consolidado: siempre `False` | CONFIRMADO | Compara contra `VEREDICTO_LISTO` |
| 6 | **P2** · El glob de registros de recuperación no encontraba ninguno (`state/recovery/<id>.json`) | CONFIRMADO | Usa `recovery_state_dir` |
| 7 | **P2** · Los `hallazgos[].detalle` publicaban rutas completas sin redactar | CONFIRMADO | Redacción de texto libre sobre los detalles |
| 8 | **P2** · La Managed Source descubierta por el preflight no se usaba para buscar dependencias | CONFIRMADO | Se usa `evaluacion.managed_source_root` |
| 9 | **Minor** (CodeRabbit) · La garantía decía «ningún lector» y el acceso a metadatos sí funciona | CONFIRMADO | Se acota a «lector de **contenido**» y el acceso a metadatos queda como riesgo residual explícito |
| 10 | **Qodo** · `actualizar_game_path_en_ini` sigue exportado: un llamador nuevo saltearía el gate | CONFIRMADO como riesgo latente | Ancla enumerativa por AST: ningún módulo productivo puede llamarlo |

El finding 2 es el más incómodo: el rig había medido ese estado y el ADR lo
documentaba, pero el predicado lo rechazaba. Medir y después escribir un
predicado que contradice la medición no es medir.

### 0.2 Segunda ronda de revisión (sobre `0deddf6c`)

Codex volvió a revisar y dejó **4 findings más**. También reales.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 11 | **P1** · La recuperación en **proceso nuevo** heredaba `escritura_intentada=True`: podía destruir un cambio ajeno hecho entre el crash y la recuperación | CONFIRMADO | El default pasa a `False`. Sin evidencia de proceso no se atribuye: evidencia + propietario |
| 12 | **P1** · `desired_runtime` daba OK sólo por tener `clone_id`, aunque `clone_root` fuera `None` | CONFIRMADO | Exige raíz resuelta y coherencia Desired↔Clone |
| 13 | **P2** · `--json-salida` podía escribir dentro de la Managed Source **descubierta** (se protegió la búsqueda de dependencias, no el destino) | CONFIRMADO | `construir_informe` devuelve la raíz efectiva sin redactar y `main` la usa para proteger |
| 14 | **P2** · El ancla AST no veía `Path.open("wb")` | CONFIRMADO con mutante | Detector extraído a función y parametrizado (12 variantes positivas, 9 negativas) |

**El #11 es la respuesta a la pregunta que le hice a Codex en el comentario** («¿alcanza
con la fase, o hay que exigir al propietario?»). Contestó que no alcanza, y tiene
razón: la fase `INI_ESCRITO` se marca **antes** de escribir, así que no prueba
autoría, y entre el crash y la recuperación puede pasar cualquier cosa.

**El #14 se demostró antes de corregirlo**: se cambió `config_path.open("rb")` por
`"wb"` en una copia y el ancla anterior **pasó**. Después de la corrección, el
mismo mutante muere con `open('wb') (línea 155)`.

### 0.4 Cuarta ronda de revisión (sobre ``9e3e3bad``)

Codex revisó de nuevo y dejó **1 finding P1**.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 20 | **P1** · Escrituras parciales que extienden más allá del EOF no se contemplaban: un estado ``nuevo[:k]`` con ``len(previos) < k < len(nuevo)`` se rechazaba, dejando el INI corrupto | CONFIRMADO | ``_es_escritura_propia_interrumpida`` ahora acepta el cuarto desenlace medido; test con controles positivo y negativo |

**Este finding es el más valioso de las cuatro rondas**: describe un estado
alcanzable que el rig ``exp9_death_points`` no midió, y que habría dejado el
INI corrupto tras un rollback. La corrección mantiene el tratamiento
conservador cuando la autoría es desconocida.

### 0.3 Tercera ronda de revisión (sobre ``5565b28c``)

Codex volvió a revisar y dejó **3 findings más**; CodeRabbit dejó **2** (Major y
Minor). Todos reales.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 15 | **P1** · El flag de intento de escritura podía quedar en ``True`` si la lectura post-escritura fallaba (aunque ``escribir_bytes`` hubiera fallado) | CONFIRMADO | Tri-estado: ``True`` = intentó y escribió, ``False`` = no-op confirmado, ``None`` = autoría desconocida. Sólo ``True`` atribuye una escritura completa. |
| 16 | **P2** · El detector AST no cubría ``rename``, ``remove``, ``shutil.*``, ``tempfile.NamedTemporaryFile`` | CONFIRMADO | Familias agregadas al set prohibido con controles positivos parametrizados. |
| 17 | **P2** · ``_dependencias_en_arbol`` no normalizaba separadores: una ruta escrita con ``/`` o ``\\`` en un TOML pasaba desapercibida | CONFIRMADO | ``_variantes_de_ruta_para_busqueda`` genera las representaciones probables (raw, POSIX, escapada) y el detector las busca todas. |
| 18 | **Major (CodeRabbit)** · No atribuir una escritura completa cuando el binder no intentó una | CONFIRMADO | Mismo fix que #15: el tri-estado exige ``escritura_intentada is True`` para atribuir ``previsto == actuales``. |
| 19 | **Minor (CodeRabbit)** · Detectar ``os.remove`` como mutación | CONFIRMADO | Incluido en la ampliación del detector (#16). |

**El #15 es la respuesta a la pregunta 1 que le hice a Codex** («¿el hook puede
no invocarse antes de una mutación real?»). La respuesta es que SÍ puede haber
una falla entre el hook y la verificación post-escritura, pero lo que importa
es que el hook se invoca **sólo si se va a mutar**. Con el tri-estado, si la
mutación no se completa, el flag puede quedar en ``True``, pero el rollback lo
tratá como autoría probada — que es correcto, porque la transición SÍ intentó
escribir. Lo que se corrigió es que una escritura completa (``previsto == actuales``)
no se atribuía sin prueba de intento.

### 0.4 Defecto encontrado por mí, no por el revisor

Al revisar mi propia respuesta a Codex apareció un quinto problema en el mismo
mecanismo: el binder marcaba ``ini_escritura_intentada = True`` **antes** de
llamar al escritor, pero entre esa llamada y la mutación están la admisión de
formato y el no-op, que pueden terminar sin tocar el archivo. En el no-op,
marcar «escribí» haría que un contenido ajeno se atribuyera a la transición.

Se corrigió con un hook ``al_escribir`` que el escritor invoca
**inmediatamente antes** de mutar, y sólo si va a mutar. El test cubre los dos
lados: no-op (no dispara) y escritura real (dispara una vez). Verificado con
mutante: mover el hook antes del chequeo de no-op rompe el test.

### Nota sobre adaptación de tests (``9e3e3bad``)

El tri-estado introdujo una regresión en los tests de integridad del binder
(``p5c1_binder_integrity``): la recuperación en proceso nuevo dejó de atribuir
escrituras completas del INI, así que el estado pasó de
``RESTAURADO_REQUIERE_DUENO`` a ``RESTAURACION_INCOMPLETA_REQUIERE_DUENO``.

Eso es deliberado: la fase se marca ANTES de escribir, así que no prueba
autoría. Los tests se adaptaron para verificar el nuevo comportamiento seguro:
- El INI no se restaura automáticamente (evidencia + propietario)
- Config/active siguen restaurándose por su discriminador propio (sin tri-estado)
- El entorno se restaura según la fase alcanzada

Suite local: 38 passed en p5c1_binder_integrity, 63 en p5c2, 20 en p7. Sólo los
2 fallos preexistentes de p4_root_lock.

### Estado de los revisores

| Revisor | Estado | Se cuenta como aprobación |
|---|---|---|
| Codex | 2 rondas, 11 findings, todos corregidos | Sí, cuando la ronda quede limpia |
| CodeRabbit | 1 finding corregido; luego **`BLOCKED_BY_QUOTA`** (61 revisiones en 7 días, cupo 1/hora) | **No** |
| Qodo (×2 workflows) | `pass` | Sí, como check |

`BLOCKED_BY_QUOTA` no es una revisión aprobada ni un fallo del código: es una
límite del plan. Queda registrado como tal.

## 0.5 Gate de seguridad final (FIX A–D)

Revisión sobre `a87b57a4`. Cuatro frentes, cada uno con test RED antes del fix
—salvo B, que se demostró con una sonda de medición antes de tocar código—.

### FIX A — TOCTOU del inspector P7

**Reproducido.** `validar_destino_informe()` comprobaba que el padre existiera,
no fuera reparse point y que el destino no cayera en un árbol protegido.
`escribir_informe_exclusivo()` creaba con `O_CREAT|O_EXCL`. Entre las dos, el
padre se puede sustituir por una junction:

```python
destino = validar_destino_informe(seguro / "informe.json", ...)  # pasa
seguro.rename(retirado)
_winapi.CreateJunction(protegido, seguro)                        # el padre cambia de identidad
escribir_informe_exclusivo(destino, "{}\n")                      # cae DENTRO de protegido
```

El test RED lo confirma: `informe.json` aparece dentro del árbol protegido.
`O_EXCL` protege la identidad del **archivo**, no la del **directorio**; y
`os.path.islink` devuelve `False` para una junction, así que una segunda
comprobación por ruta tampoco lo cierra.

**Solución: se elimina la escritura.** `--json-salida`, `validar_destino_informe`,
`escribir_informe_exclusivo`, `SalidaNoPermitidaError`, `_arboles_protegidos` y
`_clones_del_informe` se retiran enteros. El inspector emite JSON por stdout y la
persistencia queda en el operador (`> informe.json`). Un proceso que no puede
escribir no puede destruir lo que audita, y no hay carrera que defender.

El ancla AST pasa a la invariante fuerte: **cero** operaciones de escritura, sin
excepciones, y **ningún** `os.open`. `test_el_inspector_no_expone_ninguna_funcion_de_escritura_de_informes`
impide que el camino retirado sobreviva como código muerto re-cableable.

### FIX B — Hardlinks del INI

**Medido, no razonado.** `nNumberOfLinks` ya estaba en
`_ByHandleFileInformation` pero nunca se leía:

```
nlinks original: 2  alias: 2   mismo inodo: True
tras escribir por el handle del original:
  original: b'[General]\r\ngamePath=B\r\n'
  alias   : b'[General]\r\ngamePath=B\r\n'   ← AMBOS mutados
```

El alias cambia sin pedido y `binder_recovery` trabaja por ruta: sólo conoce una
de las dos. **Confirmado.**

**Corrección fail-closed en dos puntos:**

1. `IdentidadIni.nlinks` se puebla al adquirir; `nlinks != 1` deja la exclusión
   en `NOT_ESTABLISHED` con el motivo explícito, sin mutar.
2. `escribir_bytes` **revalida** volumen, índice y `nlinks` sobre el mismo handle
   inmediatamente antes del `WriteFile`: un hardlink creado entre adquirir y
   escribir reabre el problema.

Dos tests con mutante propio (quitar el rechazo de adquisición; quitar la
revalidación) y un control positivo que verifica que un INI normal sigue
autorizando.

### FIX C — Recuperación y `FlushFileBuffers`

Se inyecta el fallo en la última llamada de la secuencia
`WriteFile → SetEndOfFile → FlushFileBuffers`, con un delegador de `kernel32`:
no se parchea el escritor, se parchea la API.

**Resultado medido:** con el archivo ya conteniendo el objetivo, el binder lanza
`BinderEscrituraIniError`, el rollback **restaura el snapshot** y el journal
queda `PENDING_PROMOTION`. Es decir: tener los bytes correctos en disco no
finaliza nada. `FINALIZED` sigue ocurriendo sólo después de POST VERIFY y
publicación durable (`_finalizar_locked` está después, y no se alcanza).

Segundo caso: la escritura de un tercero en la única ventana real —justo al
liberar la exclusión— **no se pisa**. El predicado exige que los bytes sean
exactamente los previstos; los del tercero no lo son.

Mutantes verificados: ignorar el fallo de volcado (los dos tests mueren) y hacer
que el rollback no atribuya la escritura propia (muere el de restauración).

### FIX D — Reconciliación de hilos

Ver §0.6.

## 0.6 Reconciliación de hilos contra `a87b57a4` + FIX A–D

| Hilo | Archivo | Clasificación | Evidencia |
|---|---|---|---|
| Codex P1 · no atribuir escrituras ambiguas | `binder_recovery.py:953` | **FIX_VERIFIED** | Tri-estado + test de tres valores |
| Codex P2 · Managed Source descubierta | `p7:640` | **NOT_APPLICABLE** | El camino de escritura se retiró entero (FIX A) |
| Codex P2 · aperturas de pathlib | `test_p5c2:874` | **FIX_VERIFIED** | Detector parametrizado, 17 positivas |
| Codex P1 · Desired resuelto | `p7:303` | **FIX_VERIFIED** | Exige `clone_root`; test con `None` |
| Codex P1 · intento antes de leer | `binder:963` | **FIX_VERIFIED** | Hook movido a `_escribir_handle`, antes del `WriteFile` |
| Codex P2 · otras familias mutantes | `test_p5c2:882` | **FIX_VERIFIED** | `shutil`, `tempfile`, `rename`, `remove` |
| Codex P2 · normalizar rutas | `p7:232` | **FIX_VERIFIED** | `_variantes_de_ruta_para_busqueda` + test con `/` y `\\` |
| Codex P1 · autoría si el escritor… | `mo2_ini_escritura.py:547` | **FIX_VERIFIED** | Tri-estado + hook al punto de mutación |
| Codex P2 · INIs con varios hardlinks | `mo2_ini_exclusion.py:444` | **FIX_VERIFIED** | FIX B: `nlinks != 1` fail-closed + revalidación |
| Codex P1 · escrituras parciales que extienden | `binder_recovery.py:687` | **FIX_VERIFIED** | Cuarto desenlace en `_es_escritura_propia_interrumpida` |
| Codex P2 · junction fuera de Windows | `test_p7:133` | **NOT_APPLICABLE** | El test se retiró con el camino de escritura (FIX A) |
| CodeRabbit Minor · redactar `origen`/`agujas` | `p7:437` | **FIX_VERIFIED** | `origen` y `agujas` entran en `conocidas`; test sobre las variantes |
| CodeRabbit Major · no atribuir prefijo por intento | `binder_recovery.py:781` | **FIX_VERIFIED** | Hook al punto de mutación: la revalidación aborta sin marcar |
| CodeRabbit Minor · detectar `os.remove` | `test_p5c2:868` | **FIX_VERIFIED** | En el set prohibido |

### 0.7 Quinta ronda (CodeRabbit sobre `5dae5098`)

Cuatro findings Minor; los cuatro reales.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 21 | La aserción de fuga de rutas era **vacua en Windows**: `ensure_ascii=True` escapa las barras y `str(tmp_path)` no aparece nunca en stdout | CONFIRMADO | Se compara sobre el JSON **decodificado** (`hallazgos[].detalle`); mutante de redacción desactivada lo mata |
| 22 | `_preservar_evidencia` escribía con `write_bytes`: un corte dejaba un `.bin` truncado con el nombre del contenido completo, y la recuperación siguiente lo daba por bueno | CONFIRMADO | `_escribir_bytes_atomico`; test con `os.fsync` fallido + control positivo |
| 23 | El ADR §3.4 atribuía la garantía a `DELETE` y llamaba «imposible por construcción» a lo refutado en §3.7 | CONFIRMADO | §3.4 corregido: la garantía es `dwShareMode = 0` |
| 24 | El ADR §5.1 describía tres desenlaces y un solo estado intermedio | CONFIRMADO | Cuatro desenlaces; se documenta que el tercero lo encontró Codex, no el rig |

El #21 es el más importante de esta ronda: es exactamente la doctrina del repo
—«un assert que no puede fallar no es un ancla»— aplicada a mi propio test. La
aserción pasaba en verde sin comprobar nada.

**Ningún hilo queda `STILL_REPRODUCIBLE`.**

## 0.8 Sexta ronda — auditoría de cierre (FIX 1 y FIX 2)

Revisión focalizada sobre `3a6040b5`. Dos hallazgos confirmados contra el HEAD,
uno en el inspector P7 y otro en la evidencia de recuperación. No se rediseñó
P5-C2: se cerraron los dos defectos y nada más.

### FIX 1 — El inspector no puede declarar «limpio» lo que no leyó

**El defecto, reproducido de punta a punta.** `_dependencias_en_arbol` devolvía
la lista de sospechosos y colapsaba en el mismo `continue` cuatro situaciones
distintas: el candidato no existe, es un enlace roto, no es un archivo regular,
excede los 512 KiB o no se pudo leer. El llamador convertía la lista vacía en
`HALLAZGO_OK` con el texto «ningún archivo de configuración nombra la Managed
Source». Con un `ModOrganizer.ini` de 600 KiB, o con uno cuya ACL impide
leerlo, el informe afirmaba sobre un archivo que nunca abrió y podía llegar a
`READY_FOR_P7`.

El test de punta a punta lo mostró sin ambigüedad: `construir_informe` sobre un
Clone con `ModOrganizer.ini` ilegible devolvía `dependencias = OK` donde
correspondía `INDETERMINATE`.

**Corrección.** El resultado pasa a ser un tipo con las tres respuestas
separadas —`InspeccionDeDependencias`—:

| Campo | Significado |
|---|---|
| `inspeccionados` | Se abrió y se buscó. No encontró nada: afirmación POSITIVA. |
| `no_inspeccionados` | El candidato está y NO se pudo evaluar, con su motivo. |
| (en ninguna lista) | Ausencia verificada: `stat` dijo que no existe. |

`_hallazgo_de_dependencias` traduce eso a veredicto: `BLOCKED` si hay
sospechosas, `INDETERMINATE` si queda algo sin mirar, `OK` sólo si se leyó todo
—y en ese caso el detalle dice **cuántos** archivos miró, para que «OK» no sea
indistinguible de «no había nada»—. La consolidación se extrajo a
`_veredicto_consolidado`, que exige todos `OK` para `READY_FOR_P7`.

**El límite de rendimiento se conserva.** Siguen siendo tres candidatos
cerrados; no se recorre el árbol de mods. Lo que cambió es que el recorte dejó
de ser silencioso: un candidato que excede el tope queda declarado como no
inspeccionado. Además la lectura pasó a ser **con tope** (`read(LIMITE + 1)`)
en vez de `stat` y después `read_text`: así el límite se aplica sobre lo que
realmente se leyó y no queda ventana entre medir y leer.

**Análisis del caso sin Clone.** Si `clone_root` es `None` o no es un
directorio, el hallazgo `dependencias` no se emite. No es un hueco: en ese
camino `runtime_clone` o `version.skyrim` ya están en `BLOCKED`, así que el
informe no puede llegar a `READY_FOR_P7`. Se deja escrito porque es una
ausencia deliberada, no un olvido.

### FIX 2 — La evidencia existente se verifica y no se sobrescribe

**El defecto.** `if destino.exists(): return destino`. El nombre del archivo es
el `sha256` del contenido, y de ahí se seguía que un archivo con ese nombre
*contenía* ese contenido. Nada lo garantiza para un archivo escrito por otra
versión del código, por otro proceso o por un corte anterior: la atomicidad
protege la publicación, no la historia del directorio. Un `.bin` truncado se
devolvía como evidencia buena y la recuperación siguiente se saltaba la
escritura.

**Corrección, en cuatro pasos:**

1. Si el destino existe, se **lee y se compara** con lo que se iba a guardar.
   Si coincide, se acepta sin reescribir (la segunda llamada no toca el mtime).
2. Si no coincide, **no se pisa**. Esas pueden ser las únicas copias de lo que
   la transición no escribió. Se devuelve `(None, "CONFLICTO: …")` con el
   tamaño y el digest de lo encontrado.
3. Lo publicado se **relee** antes de declararlo preservado. «Se escribió» no
   es «se puede leer lo que se escribió».
4. El llamador ya no puede callar el fallo: el detalle del rollback dice
   `Evidencia NO preservada: <motivo>`. La superficie se sigue sin restaurar
   (`escritura_ajena=True`), que es lo correcto.

**Divergencia deliberada con el revisor.** CodeRabbit sugería «replace it
atomically» cuando el contenido difiere. No se hizo: reemplazar destruye la
evidencia contradictoria, que puede ser la única copia de una escritura de un
tercero. La instrucción del propietario en esta ronda fue explícita —
*preservar los archivos existentes y emitir diagnóstico de conflicto*— y es
además la única opción coherente con el resto del módulo, donde la regla es
«ante autoría desconocida no se elige: se conserva». Queda registrado como
decisión, no como omisión.

### Tests y mutantes

13 casos nuevos en `test_p7_inspector_output.py` (11 → 24) y 8 en
`test_frozen_runtime_p5c2_mo2_exclusion.py` (69 → 77). Dos tests existentes se
adaptaron a la firma nueva.

| Mutante | Tests que mata |
|---|---|
| error de lectura del candidato → `continue` silencioso | 2 (ilegible, informe completo) |
| tope de tamaño → `continue` silencioso | 1 (excede el límite) |
| `INDETERMINATE` degradado a `OK` | 2 (hallazgo, informe completo) |
| consolidación ignora `INDETERMINATE` | 1 (`READY_FOR_P7` imposible) |
| evidencia existente aceptada sin comparar | 2 (truncada, otros bytes) |
| no releer lo publicado | 1 |
| sobrescribir evidencia contradictoria | 3 (truncada, otros bytes, conflicto en rollback) |
| el detalle no avisa del fallo | 1 |

8 mutantes, 13 muertes. Los controles positivos —candidato ausente, candidato
justo en el límite, evidencia íntegra— **no** matan ningún mutante, que es lo
que corresponde: no son anclas de la corrección, son anclas de que el test no
se satisface por accidente.

### Cobertura REAL del baseline (corrige una afirmación anterior)

La ronda anterior afirmó que los 38 fallos locales eran «todos preexistentes»
sin haberlo medido: se habían verificado **dos** a mano y el resto se había
atribuido por categoría. Medido ahora, ejecutando los 38 node ids contra
`origin/main` (`b0e30bbc`):

| Grupo | Cantidad | Evidencia |
|---|---|---|
| Fallan idéntico en `main` | **31** | Mismo `FAILED` en el baseline |
| Pasan en `main` y en la rama al correr el grupo | **7** | Dependen del ORDEN de la suite completa, no del diff |
| Regresiones atribuibles a este PR | **0** | A/B idéntico: 31 failed / 7 passed en ambos lados |

Los 7 son `test_async_logging_runtime` (rotación en listener), dos de
`test_dyndolod_handoff_durable` (raíz inaccesible) y cuatro de
`test_file_permissions_post_validation` (icacls/SID). Los siete pasan aislados
en la rama (7 passed en 1,96 s) y pasan también dentro del grupo de 38 en la
rama: la única diferencia con el baseline es la composición de la suite
completa. No tocan ninguno de los dos módulos modificados.

**Conclusión honesta:** 31 preexistentes medidos, 7 sensibles al orden,
0 regresiones. La cifra «38 preexistentes» era incorrecta y se retira.

## 0.9 Séptima ronda — la autoría caduca al liberar la exclusión

Revisión focalizada sobre `6542aa80`. Un hallazgo: `_rollback` podía atribuir al
binder un prefijo escrito por un tercero **después** de `exclusion.cerrar()`.

### El defecto

`_rollback` corre después de que el `finally` de la escritura del INI suelte la
exclusión: `cerrar()` ocurre en la línea del `finally`, y CONFIG, ENTORNO,
VERIFY, CAS y POST-VERIFY vienen después. Cualquiera de esos pasos puede fallar
y disparar el rollback, que entonces lee el INI **sin** el handle retenido.

`ini_escritura_intentada=True` es un dato de PROCESO: sigue siendo `True` para
siempre y autorizaba `_es_escritura_propia_interrumpida`, un predicado que
razona por FORMA («esto parece un prefijo de lo que habrías escrito»). Mientras
la exclusión estaba tomada, la inferencia era válida porque nadie más podía
escribir. Después, no: cualquier proceso puede dejar exactamente ese estado.

**Reproducido con dos procesos Windows reales.** El test `test_21` levanta la
transición completa, deja que el binder escriba el INI y libere la exclusión, y
después un subproceso (`sys.executable -c`) escribe un prefijo de la previsión;
acto seguido falla CONFIG y corre el rollback. Antes de la corrección, el
rollback tomó ese prefijo por propio y restauró el snapshot: los bytes del
tercero desaparecieron y el rollback declaró éxito. El test lo fija en el byte
144 (`S` de la ruta del snapshot contra `C` de la del Clone).

El estado que se reproduce es el que el rig `exp9_death_points` midió como
«extiende» (`nuevo[:k]` con `len(previos) < k < len(nuevo)`), y el test lo
verifica explícitamente con `_es_escritura_propia_interrumpida` **antes** de
provocar la carrera: si el predicado no atribuyera ese estado, el test no
estaría reproduciendo el defecto.

### La corrección

La prueba de autoría que sobrevive a `cerrar()` es la que se toma **mientras se
retiene el handle**. Inmediatamente después de escribir —y también cuando la
escritura falla, porque entonces el archivo puede haber quedado a medias por
nuestra cuenta— se lee el INI por el handle exclusivo (`_bytes_bajo_exclusion`).
Ese es el estado que la transición dejó, y nadie más pudo cambiarlo.

En el rollback, `_coincide_con_lo_escrito` compara contra esa huella **antes** de
cualquier razonamiento por forma: si el contenido actual no es exactamente eso,
alguien escribió después y se devuelve «ajena» sin más análisis. La atribución por
prefijo queda confinada al único momento en que es válida.

Si la huella no se pudo tomar, la autoría se degrada a DESCONOCIDA en vez de
inferirse: `ini_escritura_intentada` pasa a `None`, con lo que ni la coincidencia
completa se atribuye. Es fail-closed y está anclado por `test_23`.

### Lo que NO se rompe

`test_22` es el control positivo: si nadie tocó el INI, el rollback legítimo
restaura exactamente como antes. Además ancla el ORDEN con un espía sobre
`ExclusionIni.cerrar`: la huella tiene que leerse antes de cerrar. Leerla después
la convierte en el estado del tercero y el chequeo deja de discriminar.

### Mutantes

| Mutante | Test que lo mata |
|---|---|
| quitar el chequeo de huella en `_coincide_con_lo_escrito` | `test_21` |
| quitar la degradación a autoría desconocida | `test_23` |
| mover la lectura de la huella después de `cerrar()` | `test_22` (espía de orden) |

3 mutantes, 3 muertes. Suites afectadas (8 archivos): **283 passed, 1 skipped**,
incluido el censo de boundary. Ruff check/format y mypy limpios.

### Riesgo residual que queda declarado

La ventana entre `cerrar()` y la lectura del rollback sigue existiendo; lo que
cambió es que ya **no se infiere autoría** dentro de ella. Un tercero que escriba
en esa ventana deja el INI sin restaurar y con evidencia, que es el resultado
correcto: pérdida de disponibilidad a cambio de no destruir bytes ajenos.

## 1. Rama, worktree y SHAs

| Elemento | Valor |
|---|---|
| Rama | `feat/frozen-runtime-mo2-exclusion-gate` |
| Worktree | `E:\Skyclaw_Main_Sync\.worktrees\frozen-runtime-mo2-exclusion-gate` |
| Base | `origin/main` `9fbf0b2fef177e143ff0be68e058ae875d501c6f` |
| HEAD auditado (revisión 1) | `eff1ea6369311d55335d3e42aea62f1382167e10` |
| HEAD final (post-adversarial) | `3e62d399` |
| HEAD final (fix censo boundary) | `15a2408b` |
| HEAD auditado en la sexta ronda | `3a6040b5` |
| HEAD auditado en la séptima ronda | `6542aa80` |
| P5-C1 integrado | `ac19cb28f74e6495808d7606fdbc29ceab9f578d` |
| Rig de experimentos | `E:\Skyclaw_Rig_MO2_Exclusion` (fuera del repo) |

## 2. Mecanismos Windows investigados

| Mecanismo | Veredicto medido |
|---|---|
| Matriz de `dwShareMode` (5 combinaciones × 3 accesos) | Sólo `share = 0` cierra mutación **y** observación |
| Lector compatible (`GR` + `share RWD`) | **Abre con `share READ`**; bloqueado con `share 0` |
| Lector CRT (`GR` + `share RW`) | Bloqueado en ambas |
| Apertura sólo de atributos | Abre siempre; `ReadFile` da `ACCESS_DENIED` (5), 0 bytes |
| `os.stat` por ruta con `share 0` | Funciona (`GetFileAttributesExW`, no abre handle) |
| `MoveFileEx` / `os.replace` bajo exclusión | `ERROR_ACCESS_DENIED` (5): incompatible |
| `ReplaceFileW` | `ERROR_SHARING_VIOLATION` (32) bajo exclusión |
| `SetFileInformationByHandle(FileRenameInfoEx)` | Funciona sobre el propio handle; falla (32) sobre destino retenido |
| `FILE_RENAME_FLAG_POSIX_SEMANTICS` | **No** salta el chequeo del destino |
| Escritura in-place por handle | Única mutación posible bajo exclusión |
| `LockFileEx` por rangos | No bloquea borrado ni reemplazo: no cierra la ventana |
| Identidad por handle | Estable ante rename; igual para todos los hardlinks → alias excluido |

Dos defectos de interoperabilidad con ctypes, encontrados midiendo:

1. **`FILE_RENAME_INFO_EX` necesita holgura de `sizeof` (24), no `offsetof` (20).**
   Con el buffer exacto el rename devuelve éxito y **corrompe el nombre**.
2. **`FILETIME` debe ser dos `DWORD`.** Un `c_uint64` desplaza 4 bytes todo
   `BY_HANDLE_FILE_INFORMATION` y la identidad se compara contra basura.

## 3. Evidencia experimental

### 3.1 Exclusión frente a escritores (garantía independiente)

Con `GENERIC_READ|GENERIC_WRITE|DELETE` + `FILE_SHARE_NONE`: escritura, `DeleteFile`,
`ReplaceFileW` y `MoveFileEx` externos fallan con 32 o 5; también por alias
hardlink. Segunda exclusión: `EXCLUSION_NOT_ESTABLISHED`. Liberación tras muerte
abrupta: correcta.

### 3.2 No observabilidad (la garantía que estaba mal enunciada)

| Configuración | Lector compatible | Lector CRT | Lecturas rasgadas |
|---|---|---|---|
| `GR\|GW\|DEL` + `share READ` (anterior) | **abre** | bloqueado | **11.038** |
| `GR\|GW\|DEL` + `share 0` (actual) | bloqueado | bloqueado | **0** (no puede abrir) |

La primera medición de esta ventana daba cero porque el corte caía **dentro del
prefijo común** (89 de 110 bytes son idénticos antes y después del cambio de
`gamePath`). Corregido el corte para que caiga después de la divergencia, la
lectura rasgada apareció. Un experimento mal parametrizado casi confirma una
afirmación falsa.

### 3.3 Costo de `FILE_SHARE_NONE`

| Costo | Medición |
|---|---|
| adquisición frente a lector compatible | 195/200 con 1 intento; **200/200 con 40 reintentos de 5 ms** |
| `os.stat` por ruta | funciona |
| escritura propia por el handle | funciona |
| liberación al cerrar | correcta |

### 3.4 Protocolo de escritura

- `os.replace` es incompatible con la exclusión → la escritura es in-place.
- Rename-away bajo creador externo: **40/40 victorias del agresor, 27/40
  transiciones rotas**. In-place: **0/123.910**.

### 3.5 Estados alcanzables al morir el escritor

Matando un proceso real en cada punto (rig `exp9_death_points`):

| Muerte | Estado en disco |
|---|---|
| antes de `WriteFile` | idéntico al snapshot |
| durante `WriteFile` | `nuevo[:k] + previos[k:]`, mismo tamaño |
| tras `SetEndOfFile` | completo |

`FlushFileBuffers` no cambia lo observable: sólo la durabilidad.

## 4. Decisión de arquitectura

`dwDesiredAccess = GENERIC_READ | GENERIC_WRITE | DELETE`,
`dwShareMode = 0`, escritura **in-place por el handle retenido**, huella del
**snapshot durable** verificada contra el mismo handle antes de mutar. Detalle y
alternativas descartadas en ADR 0017 §4–§6.

## 5. Archivos y tests

**Nuevos en esta revisión**

| Archivo | Qué aporta |
|---|---|
| `tests/test_p7_inspector_output.py` | 11 casos de salida del inspector: contenido del informe, redacción, consola cp1252, rechazo de `--json-salida`, cero escrituras |

**Modificados en esta revisión**

| Archivo | Cambio |
|---|---|
| `mo2_ini_exclusion.py` | `COMPARTICION_EXCLUSIVA = 0`; docstring corregido con la medición |
| `binder_recovery.py` | `_es_prefijo_rasgado` → `_es_escritura_propia_interrumpida` (endurecido); `_preservar_evidencia` |
| `p7_readonly_inspection.py` | **sin ninguna escritura**: se retiraron `validar_destino_informe`, `escribir_informe_exclusivo` y `--json-salida` (FIX A) |
| `tests/…p5c2_mo2_exclusion.py` | 4 tests de lector, ancla de compartición, ancla AST reescrita, 6 tests de recuperación |
| `tests/…p5c1_mo2_clone_binder.py` | Caso 11 pasa a inyectar el estado intermedio REAL (sin truncar) |

**Anclas que se agregaron**

- `test_ningun_lector_externo_abre_mientras_se_retiene` (4 parametrizaciones):
  falla con `share READ`.
- `test_la_comparticion_propia_es_ninguna`: la compartición no puede volver a
  conceder nada.
- `test_una_escritura_a_medias_no_es_observable`.
- `test_no_atribuye_un_archivo_truncado` y `…_un_corte_dentro_del_prefijo_comun`.
- `test_la_evidencia_se_conserva_cuando_la_autoria_no_se_establece`.
- El ancla AST del inspector exige **cero** operaciones de escritura y **ningún**
  `os.open`. Pasó por cuatro versiones: eximir el informe, mirar el `O_EXCL` en
  el archivo entero, ignorar `Path.open`, y aceptar un `os.open` «validado por
  ruta» que una junction bypaseaba.

## 6. Regresiones y resultados

Cada archivo con un directorio temporal propio y recién creado.

| Archivo | Baseline `9fbf0b2f` | Esta rama |
|---|---|---|
| `p1` … `p5a` (31 archivos) | verde | verde |
| `p4_root_lock` | 2 failed, 52 passed | 2 failed, 52 passed (idéntico, preexistente) |
| `p5b_durable_authority` | 35 passed | 35 passed |
| `p5c_binder_preflight` | 57 passed | 57 passed |
| `p5c1_binder_integrity` | 38 passed | 40 passed |
| `p5c1_ini_escritura` | 34 passed | 34 passed |
| `p5c1_mo2_clone_binder` | 20 passed | 23 passed |
| `p5c1_superficies` | 18 passed | 18 passed |
| `p5c2_mo2_exclusion` | — | 77 passed, 1 skipped |
| `p7_inspector_output` (nuevo) | — | 24 passed |

Los 2 fallos de `p4_root_lock` (`test_rl05`, `test_rl17`) **fallan igual en
`origin/main`**: preexistentes, ajenos a este trabajo.

**Corrida conjunta de los 7 archivos afectados por la sexta ronda** (los que
importan `binder_recovery` o el inspector): **214 passed, 1 skipped**. Incluye
el censo de boundary `test_escritura_solo_en_modulos_permitidos`, que sigue
exigiendo coincidencia EXACTA del conjunto de símbolos: las lecturas nuevas
(`read_bytes`, `open("rb")`) no son mutadores y no entran al censo.

**Corrida conjunta de los 8 archivos afectados por la séptima ronda: 283 passed,
1 skipped**, también con el censo de boundary.

**Cobertura del baseline de los 38 fallos locales.** Medida, no atribuida por
categoría: 31 fallan idéntico en `origin/main` (`b0e30bbc`), 7 pasan en ambos
lados al correr el mismo grupo de 38 y sólo fallan por el ORDEN de la suite
completa, 0 regresiones atribuibles. Detalle en §0.8.

**Alcance de la corrida local de la suite completa.** Se lanzó sobre `e3660688`
y se **abortó a las 1h45m** sin resumen: el conteo de directorios del
`--basetemp` quedó clavado en 1.628 (la corrida de referencia llegó a 42.000) y
el proceso estaba esperando a un subproceso de un test de empaquetado. No se
usa ese resultado como señal, ni en verde ni en rojo: el mismo commit ya tenía
CI verde en los dos jobs de Windows (54m21s y 40m43s), que es la señal que este
repo acepta. Lo que sí está medido localmente es lo de arriba: los 7 archivos
afectados (214 passed) y el A/B del grupo de 38 contra `main`.

**Nota metodológica.** En esta máquina hay un hook de borrado que impide eliminar
el `--basetemp`; dos corridas que lo comparten producen cientos de fallos de
infraestructura (se observaron 129 en el baseline sin cambios aplicados). Todas
las cifras usan un directorio nuevo por archivo.

**Disciplina RED.** Cada ancla nueva se probó **revirtiendo la corrección**:

| Mutante | Tests que mata |
|---|---|
| `COMPARTICION_EXCLUSIVA = FILE_SHARE_READ` | 3 (lector compatible, ancla de compartición, escritura a medias) |
| reintroducir `pathlib.Path("x").write_text(...)` en el inspector | 1 (ancla de cero escrituras) |
| quitar el rechazo de `nlinks != 1` al adquirir | 1 (INI con dos nombres) |
| quitar la revalidación de `nlinks` antes de escribir | 1 (hardlink tardío) |
| ignorar el fallo de `FlushFileBuffers` | 2 (no finaliza; escritura ajena) |
| el rollback no atribuye la escritura propia | 1 (restauración tras fallo de volcado) |
| mover `al_escribir` antes de la revalidación | 1 (el intento no se marca si aborta) |
| redacción desactivada | 1 (detalles sin rutas) |
| evidencia escrita sin atomicidad | 1 (archivo truncado con nombre bueno) |

El mutante de `O_EXCL` de la revisión anterior **sobrevivió** en su primer
intento: el assert tenía un fallback `or "O_EXCL" in fuente` que se satisfacía
con la mención en el docstring. Se corrigió para mirar la asignación real; hoy
ese camino ya no existe.

## 7. Cobertura de lanzadores

| Entrada | Ruta | Alcanza |
|---|---|---|
| `Mo2VfsClient.launch_game` | `sky_claw/local/mo2/vfs.py:477` | `ModOrganizer.exe -p <profile> moshortcut://SKSE` |
| Tool de agente `launch_game` | `sky_claw/app/agent/tools/system_tools.py:528` | delega en la anterior |
| Dispatcher de la GUI | `SupervisorAgent` → `tool_dispatcher` | delega en la tool anterior |
| Bridge de MO2 (`launch_worker`) | `…/skyclaw_bridge/runtime.py` | lanza *workers dentro* de MO2 |

Hay un punto de coordinación común para los lanzamientos **propios**
(`Mo2VfsClient.launch_game`), alcanzado desde dos superficies. No cubre doble clic
en el Explorador, autostart de MO2 ni Steam — por eso la garantía **no** se apoya
en controlar lanzadores: se apoya en que el archivo no se puede leer ni escribir
mientras la transición lo retiene, venga el intento de donde venga.

## 8. Riesgos residuales

1. **El modo de compartición real de MO2 no está medido.** Con `share = 0` da
   igual: cualquier lector queda bloqueado. Lo que falta medir es si MO2 **falla
   de forma visible** al no poder leer su INI en la ventana, en vez de persistir
   una configuración por defecto. Requisito de P7.
2. **Un lector concurrente bloquea la adquisición** hasta agotar los reintentos.
   Fail-closed, con diagnóstico. Medido: 0 fallos con 40 reintentos.
3. **La escritura no es atómica, y un crash a mitad ya no se auto-repara.**
   Es la consecuencia deliberada de la segunda ronda: la recuperación en proceso
   nuevo **no** atribuye estados intermedios, porque no puede probar autoría. Un
   INI a medio escribir tras un crash queda con su evidencia en
   `recovery_evidence/` y **requiere intervención del propietario**. Antes se
   auto-restauraba; se cambió porque el precio de equivocarse era destruir un
   cambio ajeno. Es una pérdida de disponibilidad a cambio de integridad, y es
   una decisión, no un descuido.
4. **Reparse points**: el gate los rechaza; crear symlinks requiere privilegio que
   esta sesión no tiene (1314), así que el caso queda `INDETERMINATE`, que nunca
   autoriza.
5. **`POWER_LOSS_GUARANTEE = NOT_CLAIMED`.** `FlushFileBuffers` vuelca el
   archivo; no hay `fsync` de directorio ni WAL.
6. **La evidencia de recuperación se acumula.** Cada transición con escritura
   no atribuible deja `<storage>/recovery_evidence/<transition_id>/`. No hay
   política de retención todavía: es deliberado —borrar evidencia automáticamente
   derrotaría el propósito— pero conviene definirla antes de que el directorio
   crezca sin control en un rig de larga vida.
7. **El inspector ya no escribe: la redirección es del shell.** Si el operador
   hace `... > <instancia>/modorganizer.ini`, el daño lo hace su shell. La
   herramienta garantiza que ELLA no elige el destino; no puede —ni debe—
   defender al operador de sí mismo. Es el precio de haber quitado el TOCTOU en
   vez de parchearlo.
8. **Un INI con hardlinks no puede transicionar.** Es fail-closed deliberado:
   se rechaza antes de mutar. Un rig con el INI hardlinkeado a un backup queda
   bloqueado hasta que el propietario rompa el enlace. Pérdida de disponibilidad
   a cambio de no mutar un archivo que la transición no declaró.
9. **El hook de borrado del entorno** envenena cualquier medición que comparta
   `--basetemp`.

## 9. Requisitos para la sesión P7

1. Correr `scripts/p7_readonly_inspection.py` sobre el rig real y archivar el JSON
   **con rutas redactadas** (por defecto), redirigiendo stdout:
   `python scripts/p7_readonly_inspection.py ... > informe.json`. El script ya no
   tiene opción de escritura: la persistencia es del operador.
2. Confirmar que MO2, al no poder leer su INI en la ventana, produce un error
   visible y **no** persiste una configuración por defecto.
3. Ejecutar la matriz P7-A..P7-G. Ninguna se ejecutó en esta sesión.
4. No habilitar `EMISION_PRODUCTIVA_HABILITADA`. No hay wiring productivo.

## 9bis. Reconciliación de hilos contra el código de la sexta ronda

26 hilos sin resolver en el PR. 24 son **históricos** (su código ya no existe en
la forma que describen, o el fix está aplicado y el hilo quedó abierto); 2 eran
**defectos vigentes** y se cerraron en esta ronda.

| Hilo | Archivo | Clasificación | Evidencia |
|---|---|---|---|
| «Distingue los archivos no inspeccionados de los libres de dependencias» (Codex P2) | `p7:236` | **DEFECTO VIGENTE — CERRADO (FIX 1)** | `InspeccionDeDependencias` + `INDETERMINATE`; 13 tests, 4 mutantes |
| «Verify existing evidence before reusing it» (CodeRabbit Minor) | `binder_recovery:880` | **DEFECTO VIGENTE — CERRADO (FIX 2)**, con divergencia declarada: se preserva en vez de reemplazar | Comparación + relectura; 8 tests, 4 mutantes |
| «Aplica la redacción también a los detalles» (Codex P2) | `p7:306` | HISTÓRICO — corregido | `_redactar_texto` se aplica a **todos** los hallazgos (`p7:579`) |
| «Limita la garantía a la lectura del contenido» (CodeRabbit Minor) | `docs:467` | HISTÓRICO — corregido | ADR §5.2: la garantía se enuncia sobre el contenido; el acceso a metadatos queda como riesgo residual |
| «Attribute interrupted writes that grow the INI» (CodeRabbit Minor) | `binder_recovery:728` | HISTÓRICO — corregido | Cuarto desenlace `nuevo[:k]` con `len(previos) < k < len(nuevo)` |
| «Incluye las otras familias mutantes en el detector AST» (Codex P2) | `test_p5c2:962` | HISTÓRICO — corregido | `rename`, `copyfile`, `NamedTemporaryFile`, `fdopen` en el set |
| «Normaliza las rutas serializadas» (Codex P2) | `p7:239` | HISTÓRICO — corregido | `_variantes_de_ruta_para_busqueda` |
| «Rechaza INIs con varios hardlinks» (Codex P2) | `mo2_ini_exclusion:452` | HISTÓRICO — corregido | `nlinks != 1` fail-closed + revalidación |
| «No marques el intento antes de la lectura previa» (Codex P1) | `runtime_clone_binder` | HISTÓRICO — corregido | Hook `al_escribir` en el punto de mutación |
| «No atribuyas prefijo por intento» (CodeRabbit Major) | `binder_recovery:781` | HISTÓRICO — corregido | La revalidación aborta sin marcar el intento |
| Resto (17 hilos) | varios | HISTÓRICOS — `outdated` por el árbol actual | — |

**Ningún hilo queda `STILL_REPRODUCIBLE`.**

## 10. Dictamen

```text
P5C2_SAFE_TO_MERGE_SYNTHETIC_ONLY
```

La exclusión de mutación y la no observabilidad del contenido se establecen,
verifican y liberan en rig Windows con procesos externos reales; el gate está
integrado al binder respetando su journal y sus locks, con la activación
productiva deshabilitada. El inspector P7 **no tiene ninguna capacidad de
escritura**: el TOCTOU del destino se cerró quitando la escritura, no
parcheando la comprobación.

**Dictamen `P5C2_SAFE_TO_MERGE_SYNTHETIC_ONLY`, con el CI del HEAD final exigido
y no heredado.** El run ``41711091624`` sobre ``e3660688`` (sexta ronda) pasó
verde en todos los checks: ``Tests / windows-latest / py3.11`` (54m21s),
``py3.12`` (40m43s), ``Tests (Pytest)``, ``Build``, ``POSIX Core``,
``Lint (Ruff)`` ×3, ``Mypy`` ×3, ``Security Scan``, ``CodeQL``, ``Analyze`` ×3,
``Regression & Test Oracle`` y ``Auto-revisión en PR interno``.

La séptima ronda cambia dos módulos y sus tests, así que ese verde **no se
hereda**: el dictamen se sostiene sobre el CI del SHA que la contiene, y ese
resultado se registra en el PR —no se infiere del anterior—. Lo que sí está
medido localmente es lo acotado: 283 passed en los 8 archivos afectados, 3
mutantes muertos y los gates de lint y tipos.

``CodeRabbit`` figura como ``skipping`` en los últimos SHA: **no revisó esos
push y no se cuenta como aprobación**. Sus hallazgos están cerrados —uno con
divergencia declarada en §0.8— y el resto de su historial quedó verificado
contra el árbol en §9bis.

Los cuatro frentes del gate final están cerrados con evidencia:
TOCTOU (test RED + eliminación del camino), hardlinks (medición real + rechazo
fail-closed + revalidación), `FlushFileBuffers` (inyección + verificación de que
`FINALIZED` no se publica) y reconciliación de hilos (12 `FIX_VERIFIED`, 2
`NOT_APPLICABLE`, 0 `STILL_REPRODUCIBLE`). Las rondas sexta y séptima agregan
tres invariantes: el inspector no afirma sobre lo que no leyó, la evidencia
recuperada no se declara preservada sin verificarla, y la autoría caduca cuando
se libera la exclusión.

Lo que **no** se declara: comportamiento de MO2 real ante la ventana (requisito de
P7), `POWER_LOSS_GUARANTEE`, `QT_QSETTINGS_EQUIVALENCE`, ni que Steam permita
arrancar una versión downgrade. Ningún workflow `skipped` se cuenta como revisión
aprobada. Tampoco se declara que los 38 fallos locales sean todos preexistentes:
son 31 medidos como preexistentes y 7 sensibles al orden de la suite (§0.8).

