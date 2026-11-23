# NGIO / Grass Cache — reconciliación adversarial de Fase 1 y P0 SAFE CACHE (Fase 2)

> **Fecha:** 2026-11-23 · **Base:** `origin/main` `9632a465f8520010dfbe89da4a29dd29f24875da`
> **Rama:** `arena/9a1eb8ae-sky-claw` · **Autor:** agente (Arena), a pedido del propietario.
> **Estado:** `CODE_READY_FOR_HUMAN_SMOKE` para el paquete P0. **No** es `MVP_CERTIFIED`.

Este documento reconcilia contra el código de la HEAD los hallazgos F-01…F-12 de la
auditoría consolidada del 23-11-2026 y registra la remediación del único `P0`.
No repite la auditoría: cada fila cita el código o la fuente que la sostiene.

## 1. Verificación de la fuente primaria (F-01)

**No se tomó el mecanismo de ningún informe.** Se leyó el código de NGIO-NG en el tag
`1.0.13` (API de contenidos de GitHub, 2026-11-23):

`src/GidFileCache.cpp::GidFileGenerationTask::Init()`

```cpp
auto fs = std::ifstream(Util::getProgressFilePath());
std::string l;
while (std::getline(fs, l)) {
    l = Util::StringHelpers::trim(l);
    if (l.empty()) continue;
    ProgressDone->insert(l);
}
IsResuming = !ProgressDone->empty();
...
if (ProgressDone->empty()) {
    auto dir = std::filesystem::path("Data/Grass");
    for (const auto& entry : std::filesystem::directory_iterator(dir)) {
        if (entry.path().extension() == ".dgid" || entry.path().extension() == ".cgid"
            || entry.path().extension() == ".fail") {
            remove(entry.path());
        }
    }
}
```

Y `GidFileGenerationTask::End()` borra el archivo de progreso al terminar — de ahí que
su ausencia sea la señal de "terminó" que usaba el runner.

**Consecuencias que el código de Sky-Claw violaba:**

1. El disparador del borrado es el flag **vacío**, no sólo ausente. `run()` escribía
   `""` en `PrecacheGrass.txt` en *todas* las invocaciones.
2. Cualquier línea no blanca preserva el caché y hace que NGIO reanude. La definición
   de "progreso" del contrato nuevo (contenido no blanco) coincide **exactamente** con
   la del binario (`trim` + descarte de líneas vacías).
3. `releases/latest` del repo de NGIO-NG devuelve `1.0.13` aunque existen tags
   `1.0.17`/`1.0.17b3`: el instalador queda anclado a una release que no es la última
   (F-06 confirmado, ver §3).

**NO VERIFICADO (sigue abierto):** el binario de Nexus #42161 2.2.0 no fue inspeccionado
(no hay acceso al ZIP ni rig Windows). El contrato nuevo es fail-closed respecto de las
dos versiones: si 2.2.0 conserva la semántica, Sky-Claw ya no destruye; si la cambió,
Sky-Claw sigue sin destruir. Lo que **no** se puede afirmar es compatibilidad de
instalación con 2.2.0.

## 2. Reconciliación F-01…F-12 contra la HEAD

| # | Veredicto propio | Evidencia en la HEAD | Acción en este paquete |
|---|---|---|---|
| F-01 | **CONFIRMADO** | `grass_cache_runner.py:249` escribía el flag vacío sin condición; fuente primaria §1 | **Corregido** (P0) |
| F-02 | **CONFIRMADO** | `_salida_exito` sólo exige `flag ausente + count>0 + bytes>0`; no mira `.fail`, ni procedencia, ni frescura; `etapas_previas_lods.py:77` sólo busca "algún `.cgid` no vacío" | Fuera de alcance (P1) |
| F-03 | **CONFIRMADO** | `xedit/runner.py:830,904,1238` pasan `-D:<game>/Data` y spawnean directo (`run_capture`), sin USVFS (`output_targets.py` lo documenta: ningún runner hereda la VFS) | Fuera de alcance (P2) |
| F-04 | **CONFIRMADO** | `GrassCacheService.generate()` nunca llama a `analyze_prerequisites`; no hay testigo vinculado al perfil | Fuera de alcance (P2) |
| F-05 | **CONFIRMADO** | `_vigilar()` sólo detecta cancelación/deadline/disco/muerte; la racha de stall se calcula post-crash ⇒ un freeze con PID vivo consume hasta `max_runtime_s` (12 h) | Fuera de alcance (P1) |
| F-06 | **CONFIRMADO** | `tools_installer.py:266` (`releases/latest` de NGIO-NG) + selector por nombre `ngio-ae` (L1215); `releases/latest` = 1.0.13 mientras hay tags 1.0.17 | Fuera de alcance (P3) |
| F-07 | **CONFIRMADO en escritura / P-RIESGO en efecto** | `grass_profile.py:85-92` escribe sólo 3 claves de `[GrassConfig]`; MO2 superpone el archivo entero | Fuera de alcance (P3) |
| F-08 | **CONFIRMADO** | `vfs.py:589` usa `moshortcut://SKSE` sin atestar el nombre efectivo de la entrada; `ensure_ngio` no tiene gate de SKSE | Fuera de alcance (P3) |
| F-09 | **CONFIRMADO** | `output_targets.py` no conoce ningún destino de grass (grep sin resultados); no hay productor de rollback | Fuera de alcance (P4) |
| F-10 | **CONFIRMADO** | Las strategies se registran en `tool_dispatcher.py:331,338`, pero `app/agent/tools/__init__.py` y `discovery/registry.py` no mencionan grass: no hay entrada en el registro del agente ni botón de GUI | Fuera de alcance (P5) |
| F-11 | **REFUTADO para symlinks; P-RIESGO residual en junctions** | `grass_profile.py` usa `link_kind_and_identity_or_raise_with_retry` y `_reject_link_or_reparse` antes de copiar (L132-223) | Sin cambios |
| F-12 | **CONFIRMADO** | `pipeline.grass_cache.*` no tiene **ningún** suscriptor en el árbol (grep vacío); los `.fail` y los logs de NGIO no se inspeccionan | Parcial (el resultado ahora expone `mode` y `backup_dir`) |

**Correcciones a la auditoría consolidada que este trabajo mantiene:**

- Grass Cache Helper NG (SKSE) ≠ plugin "Precache Grass" de MO2. No se retiró nada.
- La desaparición del INI temporal no prueba nada sobre el consumo del caché: no se tocó
  ese punto y sigue **NO VERIFICADO**.
- `count(.cgid) == count(celdas)` no se usa como criterio en ninguna parte.

## 3. P0 — SAFE CACHE: qué cambió

Módulo nuevo `sky_claw/local/tools/grass_cache_safety.py` (mypy estricto) que contesta
**una sola vez** "¿puedo arrancar?" y es la única pieza que escribe el flag:

| Modo | Precondición | Acción sobre el flag |
|---|---|---|
| `new` (default) | sin `.cgid/.dgid/.fail` **y** sin progreso | escribe vacío |
| `resume` | flag con progreso **o** progreso archivado con huella coincidente | **no lo toca** (o lo restaura byte a byte) |
| `rebuild` | `REBUILD` **y** respaldo verificado por sha256 releído del destino | archiva el progreso y escribe vacío |

Propiedades que quedan ancladas por tests:

- Un run `new` con caché previo **no lanza**: `cache_existente_sin_decision`.
- Un flag con progreso **nunca se trunca** sin decisión; `resume` lo conserva byte a byte.
- `rebuild` sin aprobación, sin raíz de estado o sin respaldo que cubra el inventario
  actual **no arranca**.
- El respaldo se verifica **releyendo el destino** (hash + tamaño), no con el hash en
  memoria; un manifiesto tamperizado no se reusa como verificado.
- Los enlaces (symlink/junction) en el caché o en el flag son `HOLD` fail-closed.
- Al salir, el flag se **neutraliza** (el juego del usuario no puede quedar en modo
  precache) pero su contenido se archiva atado a la huella del caché, y `resume` sólo lo
  restaura si la huella coincide.
- El servicio corre el mismo `preparar_arranque` **antes de clonar el perfil**, y el
  runner lo repite sobre el inventario fresco. Un rechazo no crea transacción de journal.

## 4. Límites declarados (no son verdes)

1. **Nada de esto se probó en Windows con MO2, USVFS, SKSE ni NGIO real.** No hay rig
   en este entorno. Por eso el veredicto es `CODE_READY_FOR_HUMAN_SMOKE`.
2. **NGIO 2.2.0 (Nexus) no fue inspeccionado.** El contrato no depende de su semántica.
3. **Reanudación tras muerte dura (SIGKILL/corte de luz):** el `finally` no corre, así que
   no hay archivo de progreso archivado y el barrido de arranque (U-03) borra el flag
   huérfano. En ese escenario `resume` se niega y el camino es `rebuild`, que **respalda
   antes de borrar** — no se pierde nada, pero se regenera. Cerrarlo exige que el
   barrido archive antes de borrar (P1).
4. **El respaldo copia, no mueve**, y vive en `SystemPaths.runtime_state_dir()`. No se
   verifica que esté en otro volumen: "independiente" es aquí lógico (fuera del árbol
   MO2), no físico.
5. **Los `.fail` se respaldan y se inventarían, pero no se interpretan** (F-02).
6. Un `rebuild` sobre un caché de varios GB duplica ese espacio mientras dura el run.

## 5. Gates ejecutados (2026-11-23, Linux, Python 3.11.2)

| Gate | Comando | Resultado |
|---|---|---|
| ROJO previo | `pytest tests/test_grass_cache_runner.py -k cache_existente_sin_decision` | `TimeoutError`: el runner **arrancaba** con caché previo |
| VERDE focalizado | `pytest tests/test_grass_cache_{runner,service,safety,strategy}.py` | **128 passed** |
| Ruff | `ruff check` + `ruff format --check` sobre el write-set | limpio |
| mypy | `mypy` sobre los módulos (nuevo módulo agregado a la lista estricta; verificado con una sonda de tipos que el gate realmente lo mira) | sin errores |
| Suite completa | `pytest` | 20434 passed, 626 skipped, **136 failed preexistentes** |
| Baseline | los mismos 136 node IDs sobre el árbol con `git stash` | **idénticos** (diff vacío ⇒ 0 regresiones) |

Los 136 fallos preexistentes son de `runtime_vault*`, `frozen_runtime*`, `journal`,
`telegram_sender` y `process_helper` (módulos Windows-only o dependientes de timing,
ejecutados en Linux). El módulo `tests/test_runtime_vault_operator_verifier.py` no
colecciona en POSIX (`_SECURITY_ATTRIBUTES`) y se excluyó por eso, también en el baseline.

## 6. Orden de los paquetes siguientes

`P1` veredicto + watchdog de no-progreso (depende de P0) → `P2` USVFS/Fase A → `P3`
instalador + INI → `P4` publicación en mod dedicado + rollback → `P5` superficies.
Antes de cada uno: `git fetch`, PRs abiertos (#1003, #1006, #1007 al cierre de este
documento) y write-sets reales — no se asume que sigan abiertos.
