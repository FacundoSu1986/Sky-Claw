# Continuidad — PR #669 (GP2-S4E) — corte del 2026-10-03 01:5x

Estado guardado y verificado antes de pausar.

## Punto seguro

```text
branch:  feat/runtime-vault-s4e-production-wiring
HEAD:    c430d72f011c9fcef754f8fbb32b458079407cb8   (pusheado)
main:    0103ee4f6de15207032d25c254ede5cf2c01bff9   (sin drift)
merge-b: 0103ee4f6de15207032d25c254ede5cf2c01bff9
behind:  0     ahead: 6
working tree: limpio salvo untracked del usuario (intactos)
```

Comandos de verificacion:

```powershell
cd E:\Skyclaw_Main_Sync
git fetch --all --prune
git log --oneline --decorate -n 8
gh pr checks 669
```

Suite local en el punto de corte:

```text
pytest -k runtime_vault -q   -> 1680 passed, 21 skipped, 1 PREEXISTING
ruff check sky_claw/ tests/  -> All checks passed
ruff format --check          -> 792 files already formatted
mypy sky_claw/               -> Success: 345
mypy --platform win32 .../runtime_vault/ -> Success: 41
```

El `PREEXISTING` (`test_apply_real_con_fallo_dispara_rollback_exacto`) fue
RE-verificado en un worktree pristino del `origin/main` ACTUAL (0103ee4f),
misma plataforma y mismo comando.

## Hecho en esta sesion

**P2.5 — contrato de replay del candidate manifest** (`5a4eb7ae`)

- El arbitro de create-once paso de `if dest.exists()` (check-then-act) a
  `os.link` — falla DENTRO de la syscall de publicacion, sin ventana.
- Replay con los MISMOS bytes -> exito idempotente. Replay con bytes
  DIFERENTES -> conflicto fail-closed, sin sustituir.
- Se elimino la sobre-afirmacion "FlushFileBuffers verified": el flush es
  `os.fsync`, contrato mas debil y declarado como tal.
- NO se toco el anchor de `os.replace` (sigue en `{clone.py,
  trusted_namespace.py}`).
- LIMITACION declarada: un swap CONCURRENTE de reparse entre el check y el
  write no esta cerrado causalmente. No importa para seguridad: staging no es
  autoridad y S4-A revalida digest y bindings.

**Anchor M-CM3 de reparse (`c430d72f`)** — lo que Rompio CI en `c144aecd`

El test construia el symlink en `<programdata>/runtime_vault/staging/<op>`, pero
la ruta derivada lleva `Sky-Claw/` mas. El enlace caia donde la primitive nunca
mira. Localmente no se notaba porque crear symlink en Windows exige privilegio
y el test se saltaba ANTES de validar nada: **el anchor no se estaba
ejercitando**. En CI el symlink si se crea y fallo con "DID NOT RAISE".
Ahora el test deriva el padre con `derive_candidate_manifest_path`.

CI de `c144aecd`: `1 failed, 9657 passed, 19 skipped` (solo ese test).
CI de `c430d72f`: recien empujado, **aun no inspeccionado**.

## Pendiente (orden de la closure)

| # | Bloque | Estado |
|---|---|---|
| P1 | handle cleanup causal (`handle.closed`) | CERRADO (`bd2575b9`) |
| P2 | candidate manifest publicado en fresh path | CERRADO (`c144aecd`) |
| P2.5 | contrato de replay + arbitro atomico | CERRADO (`5a4eb7ae`, `c430d72f`) |
| P3 | pre-plan / binding-only recovery + E16 | **ABIERTO** |
| P4 | startup privileged + outcomes visibles | **ABIERTO** |
| P5 | session lifetime + terminal mappings + lock outcome | **ABIERTO** |
| P6 | E02 rollback estricto | **ABIERTO** |
| P7 | E08 COMMITTED + orphan lock real | **ABIERTO** |
| P8 | E04-E07 (crash en cada gate de S4-D) | **ABIERTO** |
| P9 | temp path guard + anchors causales (§32, §33) | **ABIERTO** |
| P10 | probes NTFS + ctime, R2 recalculado | **ABIERTO** |
| P11 | ADR 0012 + review threads | **ABIERTO** |
| P12 | validacion completa + CI Windows | **ABIERTO** |

## Primer paso manana

1. `gh pr checks 669` y leer los LOGS de `Tests / windows-latest` py3.11 y
   py3.12 de `c430d72f`. Confirmar que `test_runtime_vault_s4e_candidate_manifest.py`
   aparece en verde y que `test_rechaza_reparse_en_la_ruta_de_staging` **no**
   aparece como skip en CI (alli el symlink si se puede crear; si sale skipped
   con WinError 1314, el runner no tiene privilegio y ese anchor no se probo).
2. Despues, P3: distinguir `ProtectionJournalClassification.ABSENT` de
   `INDETERMINATE`, rutear `plan NOT_DURABLE + journal ABSENT + binding DURABLE`
   a S4-C, y agregar E16.

## Nota de alcance

No mergear. No empezar GP3, GUI ni LLM/tool wiring. No mezclar DynDOLOD, MO2 ni
Parallax.
