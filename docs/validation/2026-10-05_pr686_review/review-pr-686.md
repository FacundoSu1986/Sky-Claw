# Revisión adversarial — PR #686

**Título:** `fix(dyndolod): reject reparse descendants during packaging`
**Autor:** FacundoSu1986 · **Base:** `main` · **Rama del PR:** `arena/01a10bb8-sky-claw`
**HEAD auditado:** `48caef8` · **Base de comparación:** `main @ 32e4c1d` (el PR está 1 commit atrás: #685)
**Alcance:** `sky_claw/app/security/links.py` (+75), `sky_claw/local/tools/dyndolod_runner.py` (+21/−7),
`tests/test_runner_defects_p1_p2.py`, `tests/test_estado_ooda.py`, 2 docs.
**Publicada en GitHub:** https://github.com/FacundoSu1986/Sky-Claw/pull/686#pullrequestreview-5419073866

```text
R2_REPARSE_POLICY          = PASS
R2_SCAN_CANCEL_LIFETIME    = PASS
R2_TRANSIENT_FS_RETRY      = NEEDS_FIX      (F1 — BLOCKING)
R2_PRIMITIVE_DIRECT_TESTS  = NEEDS_FIX      (F2 — BLOCKING)
PR_686_READY_TO_MERGE      = NO
```

| # | Severidad | Clasificación | Título |
|---|---|---|---|
| F1 | [MEDIA] | **BLOCKING** | El pre-scan usa la clasificación **sin** retry: un lock transitorio de AV/indexer aborta el packaging |
| F2 | [MEDIA] | **BLOCKING** | El primitivo central nuevo (~75 líneas) no tiene tests directos; la fila OODA lo "verifica" citando un archivo que no lo menciona |
| F3 | [MEDIA] | **REQUIRED CLEANUP** | Los conteos de tests del body son del commit anterior: no reproducen contra HEAD |
| F4 | [BAJA] | NON-BLOCKING | Fail-closed rechaza también enlaces internos/tags cloud y el mensaje no ofrece remediación |
| F5 | [BAJA] | NON-BLOCKING / follow-up | TOCTOU pre-scan → `copytree` (declarado): el invariante vale para el instante del scan |
| F6 | [BAJA] | NON-BLOCKING | Coste: recorrido completo extra (~12 % de la copia, medido) e inventario duplicado |
| F7 | [BAJA] | NON-BLOCKING / follow-up | Superficies hermanas con `copytree` sin gate (fuera del scope R2) |

## Verificación (reproducible sobre el HEAD del PR)

```
tests/test_runner_defects_p1_p2.py                  → 14 passed, 2 skipped
tests/test_runner_defects_p1_p2.py -k r2            → 5 passed, 2 skipped     (el body dice 4)
tests/test_links.py tests/test_estado_ooda.py       → 58 passed, 7 skipped
familias relacionadas (packaging/output/dyndolod/rollback/workspace/handoff/links) → 1397 passed, 27 skipped
ruff check + ruff format --check                    → limpio
```

**Red check reproducido** (reemplazando `links.py` + `dyndolod_runner.py` por los de `main` y conservando los
tests del PR): `test_r2_symlink_descendiente_falla_cerrado_sin_seguir_destino` y
`test_r2_reparse_no_clasificado_falla_cerrado_antes_de_copytree` fallan con `DID NOT RAISE`; el test de
lifecycle falla con `AttributeError` (símbolo inexistente en main, esperado).

**Sondeos propios del primitivo** — todos conformes a lo documentado: raíz inexistente (`FileNotFoundError`),
raíz archivo, raíz symlink, symlink anidado a árbol externo, symlink interno, symlink roto, FIFO, socket unix
→ rechazados; árbol limpio, vacío y con archivos read-only → admitidos.

## F1 — [MEDIA, BLOCKING] Sin retry en inspecciones transitorias

`links.py:620` (raíz), `:647` y `:655` (revalidaciones), `:666` (descendientes) + `os.scandir` de `:646`: todas
usan la variante sin retry. Con un único `PermissionError` transitorio inyectado:

```text
[guard]     ABORTA con un fallo transitorio: PermissionError: [Errno 13] Access is denied
[retry]     link_kind_and_identity_or_raise_with_retry SOBREVIVE: tipo=None
[packaging] FALLA: No se puede empaquetar la fuente '.../src' para 'TestMod': [Errno 13] Access is denied
```

Superficies hermanas con retry en el mismo patrón: `local/mo2/grass_profile.py:132,145,202,209,223`,
`local/tools/pandora_service.py:297,308`, y el propio `_package_output_as_mod:2544`
(`link_kind_or_raise_with_retry`). **Fix:** patch adjunto.

## F2 — [MEDIA, BLOCKING] Sin tests directos del primitivo

Ramas sin cobertura: raíz inexistente/archivo/enlace, tipos no regulares, entrada desaparecida, y las dos
revalidaciones de identidad (`links.py:653,661`). Contraste: `exigir_contencion_fisica`, primitivo hermano,
tiene ~15 tests (`tests/test_links.py:657-851`). **Fix:** patch adjunto (14 tests).

## F3 — [MEDIA, REQUIRED CLEANUP] Body desactualizado

Declara `4 passed, 2 skipped` (R2) y `13 passed, 2 skipped`; contra `48caef8` son **5** y **14** — los números
del commit anterior (`0849caf`). También lista `tests/test_links.py` entre las suites tocadas, cuando no fue
modificado. Valores correctos tras aplicar el patch: `51/9`, `5/2`, `14/2`.

## F4–F7 — non-blocking

* **F4:** mensajes sin remediación (el hermano de `mod_path` sí la da) y política cloud no documentada.
* **F5:** TOCTOU declarado; follow-up de copia por entrada con revalidación (`grass_profile._read_file_bytes_link_safe`).
* **F6:** guard 128 ms vs `copytree` 1112 ms en 20 000 archivos (~12 %).
* **F7:** `tools_installer.py:1180`, `fomod/installer.py:495,542`, `mo2/bridge_installer.py:80`.

## Falsos positivos descartados

`exists()` "redundante" (defensa en profundidad) · "el helper se traga la cancelación" (falso: `raise intencion`
incondicional) · "scan solapado con el worker mutante" (falso: el handoff espera `worker.done()`) ·
docstring coverage (`.pr_agent.toml` prohíbe sugerir docstrings) · error de colección de
`test_runtime_vault_operator_verifier.py` (preexistente, símbolo Windows-only) · BOM/línea en blanco (inocuos).

## Lo que está bien

Gate antes de toda mutación destructiva (rmtree/mkdir/copytree/copy2/meta.ini en cero, `previous.txt`
byte-exacto) · revalidación de identidad por directorio siguiendo `_borrar_recursivo` · reutiliza la
clasificación central sin duplicar detección · extiende el handoff terminal de R1 al pre-scan · orden causal
congelado por eventos (no sleeps) · documentación honesta del límite TOCTOU.

## Estado de la rama

1 commit atrás de `main` (#685), sin conflictos. El job `Auto-revisión en PR interno` (Qodo PR-Agent) falla
también en ramas no relacionadas → infra, no el PR.
