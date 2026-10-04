# Archivo de ramas git obsoletas

> **Carpeta:** `archive/git-branches/`
> Ruta absoluta en esta máquina: `E:\Skyclaw_Main_Sync\archive\git-branches\`
> **Generado:** 2026-10-04
> **Repo:** https://github.com/FacundoSu1986/Sky-Claw.git
> **Base:** `main` @ `ee4a67ec`

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
| `restore.ps1` | sí | restaura una rama o todas desde el bundle |
| `README.md` | sí | este documento |

### Integridad del bundle

```
$ git bundle verify archive/git-branches/obsolete-branches-20261004.bundle
The bundle records a complete history.
```

- Tamaño: ~17,8 MB · SHA-256: `BD3E2BE4...FD2CCD04`
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

`main`, `feat/steam-frozen-runtime` (PR #673), `research/dyndolod-p0-alpha209-uia`
(PR #670) y `dependabot/github_actions/...` (PR #652).

### ⚠️ Verificar antes de borrar la rama original

Tres ramas tienen commits locales **sin publicar** encima de un PR cerrado. El
bundle las preserva, pero revisá su contenido antes de dar por perdido el trabajo:

- `feat/t5v2-uia-output-gate` — PR #528 CLOSED, 14 commits por delante de su remoto.
- `feat/runtime-vault-s4e-production-wiring` — PR #669 CLOSED.
- `feat/skse-autoinstall` — PR #422 CLOSED, 1 commit por delante de su remoto.

## Cómo restaurar

```powershell
# Ver qué contiene el bundle
./archive/git-branches/restore.ps1 -List

# Restaurar una rama puntual (con su nombre original)
./archive/git-branches/restore.ps1 -Branch 'wip/pr503-provenance-fix'

# Restaurar todas bajo refs/heads/restored/*
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

## Cómo borrar las ramas una vez archivadas

Una vez conforme con el archivo, las ramas locales se pueden eliminar con
`git branch -D <rama>` (usar `-D` porque las squash-merge no son ancestro de
`main`) y las remotas con:

```bash
gh api -X DELETE repos/FacundoSu1986/Sky-Claw/git/refs/heads/<rama>
```

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
