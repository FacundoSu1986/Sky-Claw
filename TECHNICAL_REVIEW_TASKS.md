# Backlog atómico — Ejecución del plan de TECHNICAL_REVIEW.md

**Fecha:** 2026-07-06
**Metodología:** OODA (Observar/Orientar = `TECHNICAL_REVIEW.md`; este documento = Decidir; cada PR = Actuar) + TDD estricto (rojo → verde → refactor).

**Estado de ejecución (2026-07-06):**
**PR #238 (MERGEADO):** Sprint 1 completo (T-01/02/05/06/07/08/09) + T-03 (ADR 0001) +
T-04 (delegación a Bashed Patch) + fixes de reviews Codex/Copilot + fix del flake Windows
de watcher_daemon_io.
**Rama actual (reiniciada desde main):** ✅ T-10 BLE001 en `local/tools/` (`d6972de`) ·
✅ T-13 `VfsHealthChecker` (`56dedbe`) · ✅ T-14 versión LOOT + advisory (`69be4c1`).
Siguiente: T-15 (agregador de preflight, compone T-09/T-13/T-14 + límites de plugins) →
T-16 (panel GUI); en paralelo T-11 (BLE001 en `local/xedit/`).
**Reglas de atomicidad:** una tarea = una rama = un PR = una preocupación. Cada tarea nombra su test rojo y su criterio de aceptación. Tamaños: **S** (<½ día), **M** (½–1 día), **L** (1–3 días).

## Grafo de dependencias (resumen)

```
Oleada 0 (P0):        T-01 ──> T-02 ──> T-03(ADR) ──> T-04
Oleada 1 (paralela):  T-05 ──> T-06        T-07, T-08, T-09   (independientes entre sí y de T-0x)
Oleada 2 (paralela):  T-10, T-11 (secuencial entre sí)        T-12 (plantilla repetible)
Oleada 3:             T-13, T-14 ──> T-15 ──> T-16
Oleada 4:             T-17 ──> T-18 ──> T-20        T-19a ──> T-19b        T-21 (tras T-15)
Oleada 5 (paralela):  T-22, T-23, T-24
Oleada 6 (final):     T-25 (humano + rig real; requiere Oleadas 0–4)
```

Optimización: dentro de cada oleada las tareas sin flecha entre sí son **paralelizables** (distintos contribuyentes o sesiones de agente, sin conflictos de archivos). Las oleadas 1 y 2 pueden arrancar en paralelo con la 0 salvo T-04.

---

## Oleada 0 — Contención del P0 (leveled list merge)

### T-01 · Deshabilitar la estrategia `CreateMergedPatch` (S)
- **Archivos:** `sky_claw/local/xedit/patch_orchestrator.py` (registro de estrategias).
- **Test rojo:** dado un lote de conflictos LVLI/LVLN/LVSP, el orquestador NO selecciona `CreateMergedPatch` (hoy la selecciona como fallback prioridad 10).
- **Aceptación:** la estrategia queda fuera del registro (o tras un feature-flag apagado por defecto) con comentario apuntando al P0; suite verde; ningún ritual puede invocar `apply_leveled_list_merge.pas`.
- **Dependencias:** ninguna. **Primera tarea a mergear.**

### T-02 · Hotfix del script: forward del ganador (M)
- **Archivos:** `sky_claw/local/xedit/scripts/apply_leveled_list_merge.pas`.
- **Cambio:** en `Process(e)`, saltar todo record que no sea `WinningOverride(e)`; eliminar la lógica de "skip por FormID duplicado" como mecanismo de selección (queda solo como guard de re-proceso).
- **Test rojo:** el runner de scripts no es ejecutable en CI (requiere xEdit) → el test verifica el *contenido generado/estático* del script: presencia de `WinningOverride` en la ruta de copia y ausencia del patrón "primera versión gana". Documentar procedimiento de smoke manual en el docstring.
- **Aceptación:** el script copia únicamente la versión ganadora por load order; smoke manual descrito paso a paso.
- **Dependencias:** T-01 (el script queda inofensivo pero sigue deshabilitado hasta T-04).

### T-03 · ADR: estrategia definitiva para leveled lists (S)
- **Archivos:** `docs/adr/0001-leveled-lists.md` (nuevo).
- **Contenido:** decidir entre (a) merge real propio con semántica Relev/Delev, (b) delegar a Bashed Patch de Wrye Bash (ya integrado en `wrye_bash_runner.py`), (c) esperar integración de Mator Smash. Registrar la decisión, el porqué y el criterio de reversión.
- **Aceptación:** ADR mergeado; T-04 se especifica según la opción elegida.
- **Dependencias:** T-02 (conocer el costo real del fix informa la decisión).

### T-04 · Implementar la decisión del ADR (L)
- **Opción (a):** merge real de entradas LVLI/LVLN/LVSP (unión de entradas de todos los overrides). Test rojo: fixture con dos overrides del mismo LVLI → el plan/script generado contiene las entradas de ambos.
- **Opción (b):** `CreateMergedPatch` se reemplaza por una delegación explícita a Wrye Bash con explicación al usuario ("leveled lists → Bashed Patch"). Test rojo: conflictos LVLI producen un plan que invoca `wrye_bash_runner`.
- **Aceptación:** re-habilitar la ruta de leveled lists con la nueva semántica; suite verde.
- **Dependencias:** T-03.

---

## Oleada 1 — Seguridad operacional

### T-05 · `LoadOrderSnapshotService`: resolver archivos del profile MO2 (M)
- **Archivos:** nuevo servicio en `sky_claw/local/mo2/` (reutilizar el parsing existente de `modlist.txt` con BOM preservado).
- **Test rojo:** dado un fixture de instalación MO2 (profile con `plugins.txt`/`loadorder.txt`), el servicio devuelve las rutas absolutas correctas; error tipado si el profile no existe.
- **Aceptación:** API `resolve_load_order_files(profile) -> list[Path]` con tests de BOM/encoding.
- **Dependencias:** ninguna.

### T-06 · Snapshot real en `loot_service` (S)
- **Archivos:** `sky_claw/local/tools/loot_service.py:142-147`.
- **Cambio:** pasar los archivos de T-05 como `target_files` del `SnapshotTransactionLock`; borrar el docstring del deferral.
- **Test rojo:** si LOOT (mock) falla a mitad de ejecución tras mutar `plugins.txt`, el contenido se restaura al estado previo.
- **Aceptación:** LOOT queda al mismo nivel de rollback que xEdit/Synthesis/QuickAutoClean.
- **Dependencias:** T-05.

### T-07 · SCPT → SCEN en el orquestador (S)
- **Archivos:** `sky_claw/local/xedit/patch_orchestrator.py:342,471`.
- **Test rojo:** un conflicto con record `SCEN` se clasifica como alto riesgo; `SCPT` ya no aparece en el set (alineado con SCA-001 de `conflict_analyzer.py:34`).
- **Aceptación:** cero referencias a `SCPT` en código Python; suite verde.
- **Dependencias:** ninguna. Paralelizable con T-05/T-08/T-09.

### T-08 · SCPT → SCEN en `list_all_conflicts.pas` (S)
- **Archivos:** `sky_claw/local/xedit/scripts/list_all_conflicts.pas:24`.
- **Test rojo:** mismo patrón que T-02 — verificar el contenido del script (set de firmas sincronizado con `ConflictAnalyzer`); idealmente extraer la lista de firmas críticas a UNA fuente (constante Python que genera/valida el .pas).
- **Aceptación:** una sola fuente de verdad para las firmas críticas.
- **Dependencias:** ninguna (mergear después de T-07 para reusar la constante).

### T-09 · Chequeo de `mteFunctions.pas` en discovery de xEdit (S)
- **Archivos:** `sky_claw/local/discovery/` + `sky_claw/local/xedit/runner.py`.
- **Test rojo:** con una instalación xEdit sin `Edit Scripts/mteFunctions.pas`, el discovery reporta el faltante con mensaje accionable (link de descarga) en vez de fallar al compilar el script.
- **Aceptación:** el error aparece en preflight/discovery, nunca a mitad de un Ritual.
- **Dependencias:** ninguna.

---

## Oleada 2 — Robustez incremental (plantillas repetibles)

### T-10 · BLE001 en `sky_claw/local/tools/` (M)
- **Cambio:** activar `BLE001` para la carpeta en `pyproject.toml` (per-file-ignores inverso) y convertir cada `except Exception` en excepciones tipadas mapeadas al contrato `success`+`message` (`tool_result.py`).
- **Test rojo:** los tests de contrato existentes (`tests/test_tool_result_contract.py`) siguen verdes + test nuevo por runner: una excepción inesperada del subproceso NO se traga silenciosamente (se propaga o se reporta con `success=False` y mensaje fiel).
- **Aceptación:** `ruff check` verde con BLE001 activo en la carpeta.
- **Dependencias:** ninguna. **Nota:** `pyproject.toml:110` documenta 31 violaciones en runners de `local/` — es el lote más valioso.

### T-11 · BLE001 en `sky_claw/local/xedit/` (M)
- Mismo patrón que T-10. **Dependencias:** T-10 (reusar el patrón de excepciones tipadas).

### T-12 · Plantilla: mypy estricto módulo a módulo (S por módulo, repetible)
- **Cambio:** quitar un módulo de `ignore_errors = true` (`pyproject.toml:192`), anotar, arreglar.
- **Orden sugerido:** empezar por los módulos que tocan Oleadas 1 y 4 (`loot_service.py`, `mo2/`, `conflict_analyzer.py`) para que el trabajo nuevo ya nazca tipado.
- **Aceptación por iteración:** un módulo migrado por PR; contador del TODO en `pyproject.toml` actualizado.
- **Dependencias:** ninguna; repetir hasta agotar (~30 módulos — no bloquea GA, es mantenimiento continuo).

---

## Oleada 3 — Preflight de Modlist

### T-13 · `VfsHealthChecker` (M)
- **Archivos:** nuevo módulo en `sky_claw/local/validators/`.
- **Test rojo:** en un árbol temporal con symlink/junction en la ruta simulada de juego/MO2/`mods/`/`profiles/`/`overwrite/`, el checker reporta cada uno con severidad y explicación (libloot <0.29 se sale del VFS). Distinguir del sandboxing de `path_validator.py` (propósito inverso: proteger al usuario, no al proceso).
- **Aceptación:** reporte estructurado `[(ruta, tipo, severidad, remediación)]`.
- **Dependencias:** ninguna.

### T-14 · Detección de versión de LOOT (S)
- **Archivos:** `sky_claw/local/loot/cli.py` o discovery.
- **Test rojo:** dado un output mockeado de `--version`, advierte si <0.29 con la explicación del bug de symlinks.
- **Aceptación:** versión detectada y expuesta al preflight.
- **Dependencias:** ninguna; paralelizable con T-13.

### T-15 · Agregador de preflight (semáforo) (M)
- **Archivos:** nuevo `preflight.py` que compone T-13, T-14, T-09, límites de plugins (`conflict_analyzer`), masters faltantes.
- **Test rojo:** combinaciones de checks → verde/amarillo/rojo; rojo bloquea rituales mutantes (con override HITL explícito).
- **Aceptación:** todo Ritual mutante ejecuta preflight primero; resultado persistido en el journal.
- **Dependencias:** T-13, T-14 (T-09 deseable).

### T-16 · Panel de preflight en GUI (M)
- **Archivos:** `sky_claw/antigravity/gui/views/` (nueva sección).
- **Test rojo:** test de UI (patrón de tests de GUI existente) — el semáforo y la lista de checks se renderizan desde el resultado del agregador.
- **Aceptación:** visible antes de lanzar cualquier Ritual.
- **Dependencias:** T-15.

---

## Oleada 4 — Semántica de modding

### T-17 · `PluginHeaderInspector` (M)
- **Archivos:** nuevo módulo en `sky_claw/local/validators/` o `local/xedit/`.
- **Test rojo:** fixtures binarios sintéticos de header TES4 → detecta ESL-flag real, versión de header 43 vs 44, y form version; error tipado ante archivo truncado.
- **Aceptación:** API pura sin dependencia de xEdit (lectura binaria directa del header).
- **Dependencias:** ninguna (paralelizable con Oleada 3).

### T-18 · Reemplazar heurística `.esl` por el inspector (S)
- **Archivos:** `sky_claw/local/xedit/conflict_analyzer.py:207` (y límites en :89,:198).
- **Test rojo:** un `.esp` con ESL-flag cuenta como light plugin; un `.esl` corrupto/sin flag se reporta.
- **Aceptación:** límites full/light calculados con flags reales.
- **Dependencias:** T-17.

### T-19a · Export de flags SPEL desde xEdit (M)
- **Archivos:** `sky_claw/local/xedit/scripts/` (extender el export de conflictos) + parser en `conflict_analyzer.py`.
- **Test rojo:** el parser entiende líneas de conflicto SPEL con el estado del flag `Manual Cost Calc` por override.
- **Aceptación:** el reporte de conflictos incluye flags críticos por versión del record.
- **Dependencias:** T-08 (fuente única de firmas), idealmente T-04 cerrada.

### T-19b · Regla `Manual Cost Calc` + alerta explicada (S)
- **Test rojo:** conflicto SPEL donde un override define coste manual y el ganador no lo preserva → alerta crítica con texto explicativo ("sin este flag el motor recalcula por duración → coste astronómico en mods de magia sostenida").
- **Aceptación:** primera regla del motor declarativo de flags; diseño extensible a PERK/MGEF/Relev/Delev.
- **Dependencias:** T-19a.

### T-20 · Asistente de estrategia de parcheo (M)
- **Archivos:** capa de recomendación sobre `patch_orchestrator.py` + GUI.
- **Test rojo:** conflictos LVLI → recomienda Bashed Patch con justificación; patcher conocido → Synthesis; crítico (QUST/SCEN/NPC_) → xEdit manual. Nunca recomienda el merged patch propio salvo T-04(a) completada.
- **Aceptación:** cada recomendación lleva su porqué (trazabilidad).
- **Dependencias:** T-04, T-18.

### T-21 · Post-run validator v1 (M)
- **Archivos:** nuevo validador compuesto, invocado al final de cada Ritual mutante.
- **Test rojo:** tras un run simulado que deja un master faltante / header 43 / overwrite sucio, el validador lo reporta.
- **Aceptación:** resultado persistido en el journal y visible en GUI; reusa T-15/T-17.
- **Dependencias:** T-15, T-17.

---

## Oleada 5 — GUI / accesibilidad (paralelizable entre sí y con todo lo demás)

### T-22 · Transiciones específicas + `prefers-reduced-motion` (S)
- **Archivos:** los 7 usos de `transition: all` en `sky_claw/antigravity/gui/`.
- **Aceptación:** transiciones por propiedad; media query global de reduced motion.

### T-23 · Virtualización de listas de mods (M)
- **Test rojo:** con >200 mods en el fixture, la lista renderiza en modo virtual (NiceGUI soporta scroll virtual vía Quasar `QVirtualScroll`).
- **Aceptación:** sin regresión funcional en selección/filtrado.

### T-24 · Labels reales y focus visible (S)
- **Aceptación:** inputs con label (no placeholder-como-label); anillo de focus en controles interactivos.

---

## Oleada 6 — Validación E2E (bloqueante GA; requiere humano + rig real)

### T-25 · Matriz de smoke real documentada y ejecutada (L, manual)
- **Contenido:** perfiles MO2 descartables — (a) vanilla+USSEP, (b) overhauls de magia (Mysticism/Vokrii/Sustained Magic — valida T-19b en vivo), (c) lista grande con DynDOLOD. Incluye el smoke pendiente de QuickAutoClean (CLAUDE.md).
- **Criterio de aceptación (= criterio de GA):** run completo sin mutaciones no aprobadas; rollback probado en vivo (matar LOOT a mitad de run → T-06 restaura); diff de `plugins.txt`/`modlist.txt`/`overwrite` explicable; resultados registrados en `docs/validation/`.
- **Dependencias:** Oleadas 0, 1, 3 y 4 (mínimo T-04, T-06, T-15, T-19b).

---

## Orden de ejecución recomendado (óptimo con 1–3 ejecutores)

| Sprint | Ejecutor A | Ejecutor B | Ejecutor C |
|---|---|---|---|
| 1 | T-01 → T-02 | T-05 → T-06 | T-07 → T-08 → T-09 |
| 2 | T-03 → T-04 | T-10 → T-11 | T-13 ∥ T-14 → T-15 |
| 3 | T-17 → T-18 | T-19a → T-19b | T-16 ∥ T-22/T-23/T-24 |
| 4 | T-20 | T-21 | T-12 (continuo) |
| 5 | T-25 (humano, con acompañamiento) | | |

Con un solo ejecutor: seguir la numeración por oleadas; nunca adelantar Oleada 4 antes de cerrar T-01 (el P0 contenido es prerrequisito de confianza para todo lo demás).
