# Registro de worktrees (higiene de git)

> **Generado:** 2026-10-04
> **Base `main`:** `ee4a67ec`
> **Repo:** https://github.com/FacundoSu1986/Sky-Claw.git

Registra la poda de **worktrees** de git: qué se removió, qué se conserva y por
qué. Complementa `archive/git-branches/README.md` (archivo de ramas obsoletas).

**No se borró ninguna rama**: solo se removieron worktrees (copias de trabajo); las
ramas siguen existiendo y están archivadas en el bundle.

## Convención recomendada

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
| `C:/Users/Facu2/AppData/Local/Temp/kilo/m5-final` | detached |
| `C:/Users/Facu2/AppData/Local/Temp/kilo/m5-full` | detached |
| `C:/Users/Facu2/AppData/Local/Temp/kilo/m5-provfix` | `research/native-parallax-exp-m5-frequency-coherence` |
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
borrar sin permisos de administrador. **No** están registrados como worktrees
(no afectan a git). Borrado en PowerShell **como Administrador**:

```powershell
takeown /f .worktrees\.task3-* /r
icacls .worktrees\.task3-* /grant "$env:USERNAME:(F)" /t
Remove-Item .worktrees\.task3-* -Recurse -Force
```

## Recrear un worktree

```bash
git worktree add .worktrees/<nombre> <rama>
# Si la rama ya no existe, restaurarla primero desde el bundle:
git fetch archive/git-branches/obsolete-branches-20261004.bundle \
    'refs/heads/<rama>:refs/heads/<rama>'
```
