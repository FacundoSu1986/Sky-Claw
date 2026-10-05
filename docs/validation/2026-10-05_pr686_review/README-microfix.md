# Micro-fix para PR #686 — F1 + F2 (listo para aplicar)

**Patch:** `pr686-f1-f2-microfix.patch` (379 líneas, 2 archivos)
**Base:** HEAD del PR #686 = `48caef8` · **verificado con `git apply --check` sobre checkout limpio**
**Revisión publicada en GitHub:** https://github.com/FacundoSu1986/Sky-Claw/pull/686#pullrequestreview-5419073866

> Este directorio vive en la rama de sesión `arena/01a10d41-sky-claw`, que **no** es la rama del PR
> (`arena/01a10bb8-sky-claw`). Por eso el fix se entrega como patch: esta sesión no puede pushear a la
> rama del PR. Aplicalo desde la otra sesión (o a mano) con los comandos de abajo.

## Qué contiene el patch

### F1 — retry de inspecciones transitorias (`sky_claw/app/security/links.py`, +44/−5)

* `exigir_arbol_copiable_sin_reparse` pasa a usar `link_kind_and_identity_or_raise_with_retry` en las
  **cuatro** inspecciones: raíz, revalidación tras `scandir`, revalidación tras enumerar y cada descendiente.
* Nuevo helper `_scandir_con_reintento`: mismo presupuesto que el resto del módulo (5 intentos, backoff
  lineal 0.1 s) para `os.scandir`, que no tiene variante con retry en la stdlib.
  `FileNotFoundError` **no** se reintenta: la desaparición no es transitoria.
* Docstring del guard actualizado: retry para bloqueos transitorios, fail-closed para fallos persistentes.

### F2 — tests directos de la primitiva (`tests/test_links.py`, +266)

Clase nueva `TestExigirArbolCopiableSinReparse`, 14 tests:

| Test | Corre en |
|---|---|
| `test_arbol_limpio_pasa` | Linux + Windows |
| `test_raiz_inexistente_falla_con_file_not_found` | idem |
| `test_raiz_archivo_regular_no_es_directorio` | idem |
| `test_raiz_symlink_se_rechaza` | idem (skip en Windows sin privilegios) |
| `test_raiz_junction_se_rechaza` | **sólo Windows** (`junction_guard`) |
| `test_descendiente_symlink_se_rechaza` | Linux + Windows |
| `test_descendiente_junction_se_rechaza` | **sólo Windows** |
| `test_reparse_no_clasificado_falla_cerrado` (tag `0x9000001A`) | Linux + Windows |
| `test_entrada_de_tipo_especial_se_rechaza` (FIFO simulado, portable) | idem |
| `test_entrada_que_desaparece_falla_cerrado` | idem |
| `test_directorio_que_cambia_al_abrirlo_falla_cerrado` | idem |
| `test_directorio_que_cambia_al_enumerar_falla_cerrado` | idem |
| `test_bloqueo_transitorio_se_reintenta_y_pasa` **(ancla F1)** | idem |
| `test_bloqueo_persistente_falla_cerrado` | idem |

## Evidencia local del patch (Linux, py3.11)

```
tests/test_links.py                     → 39 passed,  7 skipped   (antes)
                                        → 51 passed,  9 skipped   (después: +12 passed; +2 son los junction de Windows)
tests/test_runner_defects_p1_p2.py      → 14 passed,  2 skipped   (sin cambios)
tests/test_estado_ooda.py               → 19 passed               (sin cambios)
familias packaging/dyndolod/rollback/workspace/handoff/links/grass/mo2/snapshot → 736 passed, 31 skipped
ruff check sky_claw/ tests/ + ruff format --check → limpio
mypy sky_claw/app/security/links.py     → sin errores
```

**Red check de F1 reproducido por mí:** revirtiendo *sólo* las cuatro inspecciones dentro del guard a la
variante sin retry (y conservando los tests nuevos), `test_bloqueo_transitorio_se_reintenta_y_pasa` falla con
`PermissionError: [Errno 13] Access is denied` que escapa del guard; con el retry, pasa. El caso persistente
falla cerrado en ambos escenarios (es la contracara, no el ancla).

## Cómo aplicarlo

```bash
git checkout arena/01a10bb8-sky-claw
git pull
git apply docs/validation/2026-10-05_pr686_review/pr686-f1-f2-microfix.patch   # o la ruta donde lo tengas
git add sky_claw/app/security/links.py tests/test_links.py
git commit -m "fix(dyndolod): reintentar inspecciones transitorias en el pre-scan reparse + tests directos"
git push
```

## Pendiente (F3) — lo hace quien aplica, después de correr

El body de #686 sigue con los conteos viejos. Con este patch, los valores correctos son:

* `tests/test_links.py`: **51 passed, 9 skipped** (era 39/7)
* R2 focused (`test_runner_defects_p1_p2.py -k r2`): **5 passed, 2 skipped** (el body dice 4)
* `test_runner_defects_p1_p2.py` completo: **14 passed, 2 skipped** (el body dice 13)

Y en la fila OODA (`docs/pending_ooda_status.md:338`) la cita a `tests/test_links.py` **ya queda respaldada**
por este patch (antes era una cita sin cobertura).

## Fuera de este patch (acordado como non-blocking)

F4 (mensaje de remediación / política cloud), F5 (TOCTOU pre-scan → `copytree`), F6 (overhead ~12 %),
F7 (superficies hermanas con `copytree`).
