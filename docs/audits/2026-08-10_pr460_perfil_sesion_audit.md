# Auditoría independiente del PR #460 — perfil MO2 de sesión

**Objeto:** `FacundoSu1986/Sky-Claw#460` en `bd1b475`.
**Postura:** revisor independiente. Nada se dio por verificado porque el autor lo
declarara; cada afirmación de abajo se reprodujo ejecutando el código.

---

## 1. Las anclas de `tests/test_perfil_sesion_invariante.py`, rotas a mano

El archivo declara 13 funciones de test (20 casos con la parametrización). Se rompió
**cada mecanismo** de forma independiente y se confirmó que se pone rojo por la razón
que el docstring declara — no por un `ImportError` ni por un error de colección.

| # | Ancla | Mutación aplicada | |
|---|---|---|---|
| 1 | `ningun_params_model_expone_profile_al_llm` | reexponer `profile` en `ToggleModParams` | 🔴 |
| 2 | `perfil_de_sesion_se_valida_al_construir_el_registry` | sacar `assert_safe_component` del `__init__` | 🔴 |
| 3 | `la_familia_del_perfil_esta_congelada` | agregar `rebuild_bashed_patch(mo2, *, profile)` a `system_tools` | 🔴 |
| 4 | `toda_la_familia_tiene_receta` | quitar la receta de `launch_game` | 🔴 |
| 5 | `el_tool_usa_el_perfil_de_sesion` — **los 8 casos** | cada lambda del registry vuelve al literal `"Default"`, uno por uno | 🔴×8 |
| 6 | `el_wiring_no_pasa_perfiles_literales` | `sync_engine.run(profile="Default")` en `app_context` | 🔴 |
| 7 | `ninguna_linea_de_comando_fija_el_perfil` | Synthesis vuelve a `["--profile", "Default"]` | 🔴 |
| 8 | `todo_caller_de_la_familia_pasa_el_perfil` | `launch_game(self._mo2)` sin el kwarg | 🔴 |
| 9 | `toda_construccion_del_resolver_declara_su_perfil` | sacar `profile_name` del `PathResolutionService` del supervisor | 🔴 |
| 10 | `ancla_api_publica_de_la_deteccion_de_runner_por_perfil` | `es_runner_por_perfil` deja de ser el helper | 🔴 |
| 11 | `las_dos_resoluciones_de_perfil_coinciden…` | volver a la precedencia invertida (`MO2_PROFILE` gana) | 🔴 |
| 12 | `toda_construccion_del_supervisor_declara_su_perfil` | sacar `profile_name` del bootloader de la GUI | 🔴 |
| 13 | `ninguna_fuente_del_perfil_evade_la_validacion` | `resolver_perfil_activo` devuelve el perfil sin `assert_safe_component` | 🔴 |

**Resultado: 20/20 casos verificados en rojo.** Ninguna ancla pasa en verde con su
invariante roto. La tabla del autor en el hilo del PR cubre 11 mecanismos y **un solo
caso** de los 8 de `el_tool_usa_el_perfil_de_sesion` (`toggle_mod`); esta auditoría
cubre además los otros 7 y `toda_la_familia_tiene_receta`, que no figura en esa tabla.

### Una mutación que el archivo NO ataja (y está bien)

Revertir `if profile_name:` a `if profile_name is not None:` en `resolver_perfil_activo`
—el fix del commit `9c683fd`— **no** rompe ninguna ancla de
`test_perfil_sesion_invariante.py`: `test_las_dos_resoluciones_de_perfil_coinciden…`
solo ejercita el perfil inyectado y `None`, nunca `""`. Sí lo ataja
`tests/test_path_resolution_service.py::TestGetActiveProfile::test_el_perfil_vacio_cuenta_como_ausencia`,
verificado en rojo contra la suite entera. No es un hueco de cobertura; es una nota
sobre dónde vive el guard.

### Estado de la suite en `bd1b475`

`4510 passed, 34 skipped`, exit code 0.

---

## 2. ¿Quedan cerrados H-01 / H-02 / H-03?

| | Hallazgo | Veredicto |
|---|---|---|
| **H-01** | de los 8 tools del registry que operan sobre un perfil, solo `install_mod_from_archive` recibía el de la sesión | **Cerrado.** Los 8 lo reciben del registry, ninguno lo expone al LLM, y las 8 mutaciones lo confirman. |
| **H-02** | `_bootloader.py` construía el `SupervisorAgent` sin `profile_name` | **Cerrado.** `profile_name=ctx.mo2_profile`, y el orden es correcto: `ctx = await start_full(args)` corre antes, y `_start_full_inner` publica `self.mo2_profile = active_profile`. |
| **H-03** | `app_context` hardcodeaba `"Default"` en `build_vfs_loot_runner` y en la sincronización inicial | **Parcial.** La mitad de `sync_engine.run` está cerrada. La del runner de LOOT **no**: ver §3. |

H-04 (taxonomía de errores del parser FOMOD) queda deliberadamente fuera del PR, con
su nombre y su razón. El recorte es correcto: es otro contrato y no hay fail-open.

---

## 3. El hermano que falta: el Ritual de LOOT de la GUI

El propio PR describe H-03 como *"el runner que **muta** `plugins.txt`/`loadorder.txt`,
**compartido por agente y GUI**"*. El PR arregla el perfil con el que ese runner se
**construye**. La superficie GUI lo **reasigna a `"Default"` al consumirlo**, así que
sobre esa superficie el fix queda inerte.

### Camino, sin una sola pieza mockeada del código bajo prueba

```
ritual_runner.dispatch_ritual   ->  supervisor.dispatch_tool("execute_loot_sorting", {})
ExecuteLootSortingStrategy      ->  LootExecutionParams(**{})
app/core/models.py:51           ->  profile_name = "Default"    (default de pydantic)
LootSortingService              ->  _ensure_loot_runner("Default")
BrokeredLootRunner.for_profile  ->  runner NUEVO atado a "Default"
```

`for_profile` devuelve `self` **solo cuando el perfil coincide**
(`brokered_loot.py:74`); con `"Default"` construye otro runner y descarta el de sesión.
Nada en el medio enriquece el payload: `dispatch_tool` delega verbatim y ningún
middleware lo toca.

### Reproducción, con el perfil de sesión en `Requiem`

```
perfil de sesión (--profile / MO2_PROFILE) : 'Requiem'
perfil que resuelve el PathResolver        : 'Requiem'

modal HITL que aprueba el operador         : profile_name='Default', update_masterlist=True
perfil al que se rebindeó el runner de LOOT: ['Default']
targets del snapshot/rollback              : .../profiles/Default/plugins.txt
```

Dos consecuencias:

1. la GUI ordena el load order de `Default` mientras el agente LLM ordena el de la
   sesión — **sobre el mismo recurso**, que es exactamente la clase de defecto que
   este PR cierra en la otra dirección;
2. el modal HITL le nombra al operador un perfil que no es el que se va a ejecutar:
   la aprobación se da sobre otra operación.

**El desplazamiento es coherente, no parcial.** Una versión anterior de este documento
afirmaba una tercera consecuencia —que el rollback protegía los archivos del perfil de
sesión mientras LOOT reescribía los de `Default`, dejando el archivo mutado sin red—
y **era incorrecta**. `_resolve_load_order_for_runner` saca los targets del runner
**ya rebindeado** vía `mutation_targets` (`loot_service.py:548-560`,
`brokered_loot.py:89`), y solo cae al `_ensure_load_order_resolver()` del
`PathResolver` para runners legacy que no declaran ese método. El snapshot sigue al
sort por construcción. El error venía de un doble de test sin `mutation_targets`, que
tomaba la rama legacy: un doble no representativo produciendo un hallazgo que el
código real no tiene. Corregido acá y en el doble de
`tests/test_perfil_sesion_ritual_gui.py`.

Lo que sí queda, y es el hallazgo: el par entero —lo que se ordena y lo que se
snapshotea— se desplaza junto al perfil equivocado.

### Por qué ninguna ancla del PR lo ve

- `test_el_wiring_no_pasa_perfiles_literales` barre **kwargs con valor constante en
  call sites**. Acá el literal es un `Field("Default")` en `models.py:51` — una
  declaración, no una llamada.
- `test_todo_caller_de_la_familia_pasa_el_perfil` enumera los 8 handlers de
  `system_tools` por nombre pelado. `ExecuteLootSortingStrategy` no es de esa familia.
- `test_el_tool_usa_el_perfil_de_sesion` ejercita `AsyncToolRegistry`, o sea la
  superficie del agente. La GUI no pasa por ahí.

### Los dos hermanos de la misma carpeta ya lo hacen bien

En `sky_claw/app/orchestrator/tool_strategies/`, las tres estrategias que tocan un
perfil se dividen así:

| Estrategia | De dónde sale el perfil con payload vacío | |
|---|---|---|
| `ValidatePluginLimitStrategy` | `default_profile_getter=lambda: supervisor.profile_name` | ✅ |
| `GenerateBashedPatchStrategy` | `profile or self._path_resolver.get_active_profile()` | ✅ |
| `ExecuteLootSortingStrategy` | default de pydantic → `"Default"` | ❌ |

Es la forma «hermano en el mismo archivo» de AGENTS.md §*La regla que más se viola*,
sobre la operación más destructiva de las tres.

### Barrido del resto del árbol

Se enumeraron todos los defaults `"Default"` de firma del paquete y se verificó el
call site de producción de cada uno. `AssetConflictDetector`, `ProfileSandbox`,
`MO2PluginStateProvider`, `grass_profile`, `sync_engine.run` y `PathResolutionService`
reciben el perfil activo explícitamente. **`ExecuteLootSortingStrategy` es el único
camino de producción al perfil MO2 que queda en `"Default"`.**

---

## 4. Fix propuesto (incluido en esta rama)

El mismo mecanismo que ya usa `ValidatePluginLimitStrategy`: la estrategia recibe un
`default_profile_getter` y lo aplica cuando el payload no trae `profile_name`.

- `sky_claw/app/orchestrator/tool_strategies/execute_loot_sorting.py`
- `sky_claw/app/orchestrator/tool_dispatcher.py`
- `tests/test_perfil_sesion_ritual_gui.py` — 3 tests, verificados **en rojo** contra el
  código de producción de `bd1b475` (revirtiendo la estrategia, no el test) y en verde
  con el fix. Afirman sobre a qué perfil se rebindeó el runner
  (lo que LOOT va a ordenar), no sobre el texto del wiring.

Verificación del fix: `4513 passed, 34 skipped`, exit code 0;
`ruff check` + `ruff format --check` limpios; `mypy --platform win32` →
`Success: no issues found in 263 source files`.

### Ancla más fuerte, para considerar

Los 3 tests de arriba son de comportamiento y cubren el caso. El ancla que atajaría la
**clase** es enumerativa sobre `RITUAL_TOOL_MAP` (ya congelado por igualdad literal en
`tests/test_ritual_dispatch.py`): despachar cada ritual con el payload vacío que manda
la GUI y afirmar que el perfil que llega al servicio es el de la sesión. Así un ritual
nuevo entra solo y rompe hasta que se decida su perfil — mismo instrumento que
`test_perfil_sesion_invariante.py` aplica a la superficie del agente, aplicado a la
superficie que le falta.
