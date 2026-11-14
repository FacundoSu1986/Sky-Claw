# Registro de worktrees (higiene de git)

> **Generado:** 2026-10-04
> **Base `main`:** `ee4a67ec`
> **Repo:** https://github.com/FacundoSu1986/Sky-Claw.git

Registra la poda de **worktrees** de git: qué se removió, qué se conserva y por
qué. Complementa `archive/git-branches/README.md` (archivo de ramas obsoletas).

**No se borró ninguna rama**: solo se removieron worktrees (copias de trabajo); las
ramas siguen existiendo y están archivadas en el bundle.

## Convención recomendada en el snapshot del 2026-10-04

Esta sección conserva el contexto de la poda. La política vigente de aislamiento,
las raíces administradas por herramientas y la transición de checkouts existentes
se definen en [`AGENTS.md`](../AGENTS.md), no en este inventario histórico.

Un único lugar para worktrees, co-locado y ya en `.gitignore`:

```
<repo>/.worktrees/<nombre-corto>
```

Evitar: `%TEMP%` (el SO lo limpia y deja el registro colgado), raíces de unidad
sueltas (`E:\<nombre>`) o múltiples roots (`C:\Worktrees`, `E:\Sectores\...`).

```bash
git worktree add .worktrees/<nombre> <rama>
git worktree remove .worktrees/<nombre>
git worktree prune        # limpia entradas cuyo directorio ya no existe
git worktree list         # auditar
```

## Estado

| Métrica | Antes | Después |
|---|---:|---:|
| Worktrees registrados | 25 | **3** |
| `detached HEAD` | 11 | 0 |
| Fuera del repo (TEMP / unidades sueltas) | 21 | 0 |

Registro ↔ disco quedó **consistente**: `git worktree prune --dry-run` no reporta
entradas colgadas.

## Conservados (3)

| Path | Rama | Motivo |
|---|---|---|
| `E:/Skyclaw_Main_Sync` | `research/native-parallax-exp-m6-…` | Worktree principal |
| `C:/Worktrees/Sky-Claw-p0-alpha209-uia` | `research/dyndolod-p0-alpha209-uia` | PR #670 abierto |
| `E:/Skyclaw_Steam_Frozen_Runtime` | `feat/steam-frozen-runtime` | PR #673 abierto + cambios sin commitear |

## Removidos (22)

Todos sobre ramas obsoletas (ya archivadas en el bundle) o `detached HEAD` viejo.

| Path | Rama / estado |
|---|---|
| `%TEMP%/kilo/m5-final` | detached |
| `%TEMP%/kilo/m5-full` | detached |
| `%TEMP%/kilo/m5-provfix` | `research/native-parallax-exp-m5-frequency-coherence` |
| `C:/Worktrees/pristine-main` | detached |
| `C:/Worktrees/Sky-Claw-exp-m3-corpus` | `research/native-parallax-exp-m3-clean-authored-trust` |
| `C:/Worktrees/Sky-Claw-parallaxr-a0` | `feat/parallaxr-assisted-external-mode` |
| `C:/Worktrees/Sky-Claw-pr2` | `feat/dyndolod-pr2-external-staging` |
| `C:/Worktrees/Sky-Claw-pr3` | `refactor/dyndolod-remove-output-freshness` |
| `C:/Worktrees/Sky-Claw-pre-lod-p2` | `feat/pre-lod-p2-tool-registry` |
| `C:/Worktrees/Sky-Claw-pre-lod-p3` | `feat/pre-lod-p3-tool-version-detection` |
| `C:/Worktrees/Sky-Claw-s4d` | `feat/runtime-vault-s4d-finalization` |
| `C:/Worktrees/Sky-Claw-skse-pr2` | `feat/skse-nexus-acquisition` |
| `C:/Worktrees/Sky-Claw-uia-output-gate-v2` | `feat/dyndolod-uia-output-gate-v2` |
| `C:/Worktrees/Sky-Claw-586c` | `feat/dyndolod-usvfs-real-rig-586c` (evidencia untracked → cuarentena) |
| `E:/Sectores/…/Sky-Claw Worktrees/t5v2-docs` | `docs/dyndolod-t5v2-rig-evidence` |
| `E:/Sectores/…/Sky-Claw-pr590-p2` | detached |
| `E:/Sectores/…/Sky-Claw-pr590-selector-binding` | detached |
| `E:/Skyclaw_586C` | detached (evidencia untracked → cuarentena) |
| `E:/Skyclaw_H1_Modlist_Order` | `fix/mo2-modlist-priority-h1` |
| `E:/Skyclaw_Main_Sync/.claude/worktrees/sky-claw-pr-526-review-f8c32e` | detached |
| `E:/Skyclaw_Main_Sync/.claude/worktrees/texgen-dyndolod-lifecycle-218ad6` | detached |
| `E:/Skyclaw_Main_Sync/.kilo/worktrees/zigzag-blouse` | detached (`uv.lock` modificado → cuarentena) |

### Directorios huérfanos también removidos (no registrados)

`C:/Worktrees/Sky-Claw-uia-output-gate-v2`, `.claude/worktrees/stoic-swartz-98a4bc`
y las carpetas vacías `C:/Worktrees` (queda solo el activo), `.kilo/worktrees`,
`E:/Sectores/…/Sky-Claw Worktrees`, `.claude/worktrees`.

## Cuarentena de artefactos untracked

Algunos worktrees tenían archivos **sin trackear** (evidencia de validación,
~10 MB) o modificaciones. Se preservaron en `archive/worktree-quarantine/20261004/`
(gitignored):

- `Sky-Claw-586c/docs/validation/…` — evidencia USVFS/texgen de PR #586c.
- `Skyclaw_586C/docs/validation/…`
- `zigzag-blouse/uv.lock.diff` — diff del `uv.lock` modificado (detached).

## Huérfanos bloqueados (requieren elevación)

Cinco directorios **vacíos** bajo `.worktrees/` (`.task3-focused-…`,
`.task3-green-…`, `.task3-red-…`, `.task3-review-config-…`,
`.task3-review-focused-…`) tienen una ACL que deniega el acceso; no se pudieron
borrar sin permisos de administrador. **No** estaban registrados como worktrees
en el snapshot. Antes de limpiar uno, verificar otra vez el registro y elegir su
nombre completo; no usar comodines para una operación recursiva. Ejemplo en
PowerShell **como Administrador**, sustituyendo el nombre exacto del directorio:

```powershell
$raiz = (Resolve-Path -LiteralPath .worktrees).Path
$objetivo = [IO.Path]::GetFullPath((Join-Path $raiz 'NOMBRE-EXACTO-VERIFICADO'))
if ([IO.Path]::GetDirectoryName($objetivo) -ne $raiz) { throw 'Objetivo fuera de .worktrees' }
# Revisar que el objetivo no aparezca en este registro antes de continuar.
git worktree list --porcelain
takeown /f "$objetivo" /r
icacls "$objetivo" /grant "${env:USERNAME}:(F)" /t
Remove-Item -LiteralPath $objetivo -Recurse -Force
```

## Recrear un worktree

```bash
git worktree add .worktrees/<nombre> <rama>
# Si la rama ya no existe, restaurarla primero desde el bundle:
# Obtener el asset del release branch-archive-20261004 (todavía en borrador).
# Ver git-branches/README.md para acceso e integridad; un clon no trae el bundle.
# Sin -Bundle toma el .bundle más nuevo de la carpeta; elegir el que contiene la rama
# (obsolete-branches-20261004.bundle u obsolete-branches-20261110.bundle):
pwsh -File archive/git-branches/restore.ps1 -Bundle <ruta-del-bundle> -Branch '<rama-o-ref-completo>'
# Un nombre corto ambiguo requiere refs/heads/<rama> o refs/remotes/origin/<rama>.
```

## Poda del 2026-11-10

> **Base `main`:** `2dab73ad` · **Worktrees registrados:** 78 → 35 · **Stashes:** 8, sin tocar.
> Las ramas se archivaron y borraron por otro camino: ver `git-branches/README.md`, sección
> "Segundo archivo (2026-11-10)".

**Criterio.** Un worktree se retiró solo si, en el momento de hacerlo: (1) `git status` estaba limpio,
(2) su rama estaba integrada en `main` (o era un detached cuyo HEAD ya es ancestro de `main`),
(3) su reflog de HEAD no tenía commits ni checkouts en las últimas 96 h y (4) no era el principal, la sesión
actual, una sesión de la app (`.claude/worktrees/*`), de Codex ni el binding legado de `AGENTS.md`.
Siempre con `git worktree remove` **sin** `--force`. Además, `git worktree prune` quitó la entrada colgada
`parallax-mod-comparison-test-034e0a` (su `gitdir` ya no existía).

### Removidos (43)

| Path | Rama / estado |
|---|---|
| `.worktrees/592-enospc-packaging` | `fix/dyndolod-packaging-enospc-preserve-previous` |
| `.worktrees/706-postmerge-hardening` | `fix/telegram-706-postmerge-hardening` |
| `.worktrees/baseline-9fe88cb4-symcheck` | detached `9fe88cb4` |
| `.worktrees/ci-windows-test-timeout` | `ci-windows-test-timeout` |
| `.worktrees/db-741-lease-cancel` | `fix/db-741-cancelled-acquire-lease` |
| `.worktrees/dyndolod-592-dispatcher-errors` | `fix/dyndolod-592-dispatcher-errors` |
| `.worktrees/dyndolod-592-f4-f5-output-evidence` | `fix/dyndolod-592-f4-f5-output-evidence` |
| `.worktrees/dyndolod-661-unattended-windows-rig` | `validation/dyndolod-661-unattended-windows-rig` |
| `.worktrees/e3-r2-d1-worker-argv` | `fix/mo2-e3-r2-d1-worker-argv` |
| `.worktrees/f1-taxonomia-modal-stitched` | `fix/dyndolod-f1-taxonomia-modal` |
| `.worktrees/f2-uia-modal-reobservation` | `fix/dyndolod-f2-uia-modal-reobservation` |
| `.worktrees/f2b-output-reobservation` | `fix/dyndolod-f2b-output-reobservation` |
| `.worktrees/fix-qodo-models` | `ci/qodo-model-resilience` |
| `.worktrees/frozen-runtime-p04` | `design/frozen-runtime-reference-clone-p04` |
| `.worktrees/frozen-runtime-p4-contracts` | `design/frozen-runtime-p4-contracts` |
| `.worktrees/frozen-runtime-p4-s1-root-lock` | `feat/frozen-runtime-p4-s1-root-lock` |
| `.worktrees/frozen-runtime-p4-s2-participant-wiring` | `frozen-runtime-p4-s2-participant-wiring` |
| `.worktrees/frozen-runtime-p4-s3-durable-journal` | `frozen-runtime-p4-s3-durable-journal` |
| `.worktrees/frozen-runtime-p4-s4-approval-authority` | `feat/frozen-runtime-p4-s4-approval-authority` |
| `.worktrees/frozen-runtime-p4-s5-scope-binding` | `feat/frozen-runtime-p4-s5-scope-binding` |
| `.worktrees/frozen-runtime-p4-s7-witness-binding` | `feat/frozen-runtime-p4-s7-witness-request-binding` |
| `.worktrees/frozen-runtime-p5b-durable-authority` | `feat/frozen-runtime-p5b-durable-authority` |
| `.worktrees/frozen-runtime-p5c-binder-preflight` | `feat/frozen-runtime-p5c-binder-preflight` |
| `.worktrees/frozen-runtime-p5c1-mvp-binder` | `feat/frozen-runtime-p5c1-mvp-binder` |
| `.worktrees/h4-r7-bound-review` | `h4-r7-bound-review` |
| `.worktrees/h4-resize-normal-fix` | `h4-resize-normal-fix` |
| `.worktrees/h4-resize-normal-gate` | `h4-resize-normal-gate` |
| `.worktrees/h4-resize-normal-preflight` | `h4-resize-normal-preflight` |
| `.worktrees/merge-conflict-standards` | `merge-conflict-standards` |
| `.worktrees/np-a0c-diagtexconv` | `np-a0c-diagtexconv` |
| `.worktrees/np-m6-rev73` | detached `268fa84a` |
| `.worktrees/p4s2-io-errors` | `p4s2-io-errors` |
| `.worktrees/p4s6-reconcile-778` | `reconcile-778-fix` |
| `.worktrees/p5a-baseline-efc1bebc` | detached `efc1bebc` |
| `.worktrees/p5a-effective-runtime-oracle` | `p5a-effective-runtime-oracle` |
| `.worktrees/parallax-unattended` | `parallax-unattended-core` |
| `.worktrees/pr-857-qodo-049` | `dependabot/github_actions/github-actions-minor-patch-421cb9cc85` |
| `.worktrees/pr697-review-remediation` | `pr697-review-remediation` |
| `.worktrees/privacy-redact-identifiers` | `privacy/redact-operator-identifiers` |
| `.worktrees/r1c-9fbf0b2fef17-20261101T134003Z` | detached `9fbf0b2f` |
| `.worktrees/rev72-m6-final-validation` | `research/native-parallax-m6-rev5` |
| `.worktrees/rig-item3` | `rig/item3` |
| `.worktrees/vfs-bridge-robusto` | `feat/vfs-bridge-robusto` |

### Incidentes y lecciones

- **`git worktree remove` también borra los archivos ignorados.** Los `scratch/` (notas de trabajo que git no
  versiona: 399 archivos en 7 worktrees) se copiaron a la cuarentena y se verificó la copia antes de retirar.
  Los `.pytest-tmp-*`, cachés, `.venv` y las bases vacías de `.skyclaw_backups/` se dejaron ir.
- **4 worktrees quedaron a medias** (`592-enospc-packaging`, `baseline-9fe88cb4-symcheck`,
  `dyndolod-592-f4-f5-output-evidence`, `h4-r7-bound-review`): git los dio de baja pero no pudo vaciar el
  directorio ("Directory not empty", por las carpetas `.pytest-tmp-*`). Se comprobó que lo restante era
  descartable y se borraron por ruta exacta. Antes de borrar un worktree con muchas carpetas `.pytest-tmp-*`,
  conviene limpiarlas primero.
- **Las ramas `claude/<nombre>` de sesiones de la app con carpeta viva no se borran**, aunque su tip esté en
  `main`: la app puede buscarlas por nombre.
- Aparecieron sesiones nuevas durante la poda y se fusionaron PRs en GitHub (`origin/main` avanzó 2 commits).
  Por eso cada paso destructivo revalida el estado en el momento, no el de la foto inicial.

### Conservados (35)

Cada worktree figura una sola vez, bajo el primer motivo que lo excluyó.

| Cant. | Qué | Por qué |
|---:|---|---|
| 1 | Principal (`main`) | Integración; tiene carpetas temporales sin trackear que no se tocaron |
| 1 | Esta sesión | En uso |
| 5 | `.claude/worktrees/*` | Sesiones de la app: se cierran archivando la sesión |
| 1 | Codex (`$CODEX_HOME/worktrees/…`) | Sesión activa con cambios sin commitear |
| 1 | `E:/Sectores/…/Skyclaw_Worktree` | Binding legado de `AGENTS.md` (retirarlo requiere un PR a `AGENTS.md` y a `LEGADOS` del test) |
| 6 | Con cambios sin commitear o archivos sin trackear | Trabajo posiblemente vivo |
| 8 | Actividad en las últimas 96 h | Se retiran en una pasada futura |
| 10 | Rama con PR abierto o con trabajo sin respaldo | Decisión del dueño |
| 2 | Detached con HEAD fuera de `main` (`np-v0d`, `p5a-integration`) | Sus commits solo son alcanzables desde ese worktree |

### Cuarentena

`archive/worktree-quarantine/20261110/` (gitignored, en el checkout principal):

- `foto-previa.txt`: todas las ramas, refs remotas, stashes y worktrees antes de empezar.
- `plan.json`: conjuntos calculados (worktrees a retirar, ramas candidatas y conservadas).
- `bundle.sha256` y `scratch/<worktree>/` (7 directorios de notas de trabajo).
- `stash-<n>-<sha>.patch`: los 8 stashes exportados como parche. Los stashes **no** se borraron.
