# Archivo de ramas git obsoletas

> **Carpeta:** `archive/git-branches/`
> **Generado:** 2026-10-04 · **snapshot del manifiesto:** ver la cabecera `# snapshot=` de `MANIFEST.tsv`
> **Repo:** https://github.com/FacundoSu1986/Sky-Claw.git
> **Base:** `main` @ `ee4a67ec`

> ⚠️ El `.bundle` **vive solo en esta máquina** (gitignored, no publicado en ningún
> Release). Ningún otro clon puede obtenerlo ni verificar sus hashes: las
> afirmaciones de "restauración probada" son locales hasta que se publique.

## Por qué existe esta carpeta

Las ramas obsoletas se **archivan en lugar de borrarse**. El mecanismo es
[`git bundle`](https://git-scm.com/docs/git-bundle): un único archivo portable que
contiene el historial completo y es restaurable en cualquier clon, incluso en un
repo vacío. Es un respaldo autocontenido —no depende de que la rama siga viva ni
de que el remoto exista.

Los `.bundle` son binarios pesados, por eso **no se versionan** (están en
`.gitignore`). Lo que sí se versiona —para dejar constancia en git— es este README,
el manifiesto `MANIFEST.tsv` y el script `restore.ps1`.

## Contenido

| Archivo | ¿Versionado? | Descripción |
|---|---|---|
| `obsolete-branches-20261004.bundle` | no (gitignored) | 78 refs autocontenidas: 77 obsoletas + `main` como base |
| `MANIFEST.tsv` | sí | una fila por ref: nombre, tipo, estado de PR, nº de PR, fecha, SHA, subject |
| `restore.ps1` | sí | restaura una rama o todas desde el bundle (incluye refs remotas) |
| `delete-branches.ps1` | sí | borra ramas con guardas fail-closed (recomputa PRs abiertos y worktrees en runtime); dry-run por defecto |
| `README.md` | sí | este documento |
| `../WORKTREES.md` | sí | convención e inventario de worktrees (poda relacionada) |

### Integridad del bundle

```
$ git bundle verify archive/git-branches/obsolete-branches-20261004.bundle
The bundle records a complete history.
```

- Tamaño: ~17,8 MB · SHA-256 (completo): `BD3E2BE405F27434C7C3EE740D3349B48E9083025EF2A855333D90AAFD2CCD04`
- Restauración probada contra un repo temporal: los SHA de las ramas restauradas
  coinciden byte a byte con los originales.

## Resumen (77 refs obsoletas)

| `kind` | MERGED | CLOSED | NO_PR | total |
|---|---:|---:|---:|---:|
| `local` (refs/heads) | 23 | 3 | 24 | **50** |
| `remote` (refs/remotes/origin) | 8 | 10 | 9 | **27** |
| **total** | **31** | **13** | **33** | **77** |

Criterio de obsolescencia: la rama tiene un PR **mergeado** o **cerrado** en GitHub,
o nunca tuvo PR y no aporta trabajo vivo. La detección NO se apoya solo en ancestros
de `main` porque el repo usa squash/rebase (lo que hace que `git branch --merged`
subestime).

### Ramas que NO se archivan (siguen activas)

`main`, `feat/steam-frozen-runtime` (PR #673) y `dependabot/github_actions/...`
(PR #652). Verificá en vivo con `gh pr list --state open`: al momento de archivar
también estaba abierto `research/dyndolod-p0-alpha209-uia` (PR #670), que ya no
figura entre los abiertos (snapshot desactualizado).

### ⚠️ El manifiesto es un snapshot: no borres a mano guiándote por él

`MANIFEST.tsv` es un **snapshot** del estado de PR al momento de generarlo (ver la
cabecera `# snapshot=`). Puede quedar obsoleto en horas: al archivarse, la rama
`research/native-parallax-exp-m6-pair-mismatch-decomposition` figuraba como `NO_PR`,
y poco después se le abrió el **PR #675** (y su remoto avanzó a `e6b81317`). Un
borrado guiado solo por el snapshot hubiera eliminado la rama de un PR abierto.

Por eso el borrado **no se hace a mano**: usá `delete-branches.ps1`, que recomputa
en runtime los PR abiertos y los worktrees y **omite** toda rama que no sea segura.

Además, ~14 tips del manifiesto **no existen en GitHub** (ramas locales o
reescrituras por rebase), entre ellos `feat/t5v2-uia-output-gate` (PR #528 CLOSED,
14 commits sin publicar), `feat/runtime-vault-s4e-production-wiring` (PR #669
CLOSED), `feat/skse-autoinstall` (PR #422 CLOSED) y los `backup/*`, `codex/*`,
`wip/*`. El bundle los preserva; revisá su contenido antes de darlos por perdidos.

## Cómo restaurar

```powershell
# Ver qué contiene el bundle
./archive/git-branches/restore.ps1 -List

# Restaurar una rama puntual (con su nombre original)
./archive/git-branches/restore.ps1 -Branch 'wip/pr503-provenance-fix'

# Restaurar todas: locales -> refs/heads/restored/*; remotas -> refs/heads/restored/origin/*
# (incluye 'restored/main', el ref base del bundle)
./archive/git-branches/restore.ps1 -All
```

O con git directamente:

```bash
git bundle list-heads archive/git-branches/obsolete-branches-20261004.bundle
git fetch archive/git-branches/obsolete-branches-20261004.bundle \
    'refs/heads/wip/pr503-provenance-fix:refs/heads/wip/pr503-provenance-fix'
```

Para restaurar en un repo **vacío** (bundle autocontenido):

```bash
git clone archive/git-branches/obsolete-branches-20261004.bundle mi-repo-restaurado
```

> `-Branch <nombre>` resuelve el ref real contra el bundle, así que también
> funciona para las refs que existen **solo** como `refs/remotes/origin/*`
> (p. ej. `claude/analyze-pr-503-zusebc`, `release/v2.1-titan`). `-All` y
> `-Branch` **abortan con error si git falla** (no hay falso éxito silencioso).

## Cómo borrar las ramas una vez archivadas

Usá el script con guardas fail-closed (recomputa PRs abiertos y worktrees en
runtime). Corre en **dry-run** por defecto:

```powershell
./archive/git-branches/delete-branches.ps1                          # dry-run
./archive/git-branches/delete-branches.ps1 -Execute                # borra locales seguras
./archive/git-branches/delete-branches.ps1 -Execute -IncludeRemote # + remotas (origin)
```

Omite toda rama con PR abierto, checked-out en un worktree, la rama actual o
`main`, y toda rama que no esté en el bundle. **No borres a mano guiándote por el
manifiesto** (ver la advertencia de snapshot más arriba).

## Publicación del bundle (pendiente)

El bundle **no está publicado**: solo existe en esta máquina. Para que el respaldo
sea verificable por terceros y sobreviva a esta máquina, subilo como **asset de un
Release** (repo público; 17,8 MB ≪ 2 GB) y pegá la URL + el SHA-256 completo acá:

```bash
gh release create branch-archive-20261004 \
  --title "Branch archive 2026-10-04" \
  --notes "Bundle de 77 ramas obsoletas archivadas (git bundle)" \
  archive/git-branches/obsolete-branches-20261004.bundle
```

> Antes de publicarlo, revisá que los commits del bundle —incluidos los ~14 que NO
> están en GitHub— no contengan nada que no deba ser público. El repo es público.

## Cómo regenerar este archivo

```powershell
# 1) bundle autocontenido con todas las ramas obsoletas + main
$dir = 'archive/git-branches'
$refs = git for-each-ref --format='%(refname)' refs/heads refs/remotes/origin |
        Where-Object { $_ -notin @(
          'refs/heads/main',
          'refs/heads/feat/steam-frozen-runtime',
          'refs/heads/research/dyndolod-p0-alpha209-uia',
          'refs/remotes/origin/HEAD',
          'refs/remotes/origin/main',
          'refs/remotes/origin/feat/steam-frozen-runtime',
          'refs/remotes/origin/research/dyndolod-p0-alpha209-uia',
          'refs/remotes/origin/dependabot/github_actions/github-actions-minor-patch-220bed8b0f'
        ) }
git bundle create "$dir/obsolete-branches-$(Get-Date -Format yyyyMMdd).bundle" $refs refs/heads/main
git bundle verify "$dir/obsolete-branches-$(Get-Date -Format yyyyMMdd).bundle"
```
