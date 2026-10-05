# Archivo de ramas git obsoletas

> **Carpeta:** `archive/git-branches/`
> **Generado:** 2026-10-04 · **snapshot del manifiesto:** ver la cabecera `# snapshot=` de `MANIFEST.tsv`
> **Repo:** https://github.com/FacundoSu1986/Sky-Claw.git
> **Base:** `main` @ `ee4a67ec`

> 📦 El `.bundle` está **subido como asset de un release en borrador**
> (`branch-archive-20261004`), tras una auditoría de secretos sobre los 43 tips que
> nunca se publicaron en ninguna ref remota (sin coincidencias reales). Todavía **no
> publicado**: por eso las afirmaciones de "restauración probada" son locales hasta
> que se publique el release.

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
| `obsolete-branches-20261004.bundle` | no (gitignored) | 78 refs autocontenidas: 77 archivadas + `main` como base |
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

## Resumen (77 refs: 76 obsoletas + 1 con PR abierto)

| `kind` | MERGED | CLOSED | NO_PR | OPEN | total |
|---|---:|---:|---:|---:|---:|
| `local` (refs/heads) | 23 | 3 | 23 | 1 | **50** |
| `remote` (refs/remotes/origin) | 8 | 10 | 9 | 0 | **27** |
| **total** | **31** | **13** | **32** | **1** | **77** |

La única fila `OPEN` es `research/native-parallax-exp-m6-pair-mismatch-decomposition`
(**PR #675**): se archivó cuando figuraba `NO_PR` y quedó obsoleta mientras se
armaba el archivo. Figura en el manifiesto por completitud, pero **no es
obsoleta** y `delete-branches.ps1` la excluye por guarda.

Además, **23 de las 27 refs remotas existen solo en el remoto** (sin rama local):
`-IncludeRemote` las cubre vía git con una lease explícita sobre el SHA archivado.

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

# Si la rama local ya existe, -Branch aborta; con -Force la sobreescribe
./archive/git-branches/restore.ps1 -Branch 'wip/pr503-provenance-fix' -Force

# Restaurar todas sin pisar destinos modificados (-Force permite sobrescribir explícitamente)
# locales -> refs/heads/restored/*; remotas -> refs/heads/restored/origin/*
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

`-IncludeRemote` cubre **también las 23 refs que solo existen en el remoto** (sin
rama local). Sin él, sólo se consideran ramas locales.
El script **aborta** si `gh pr list`, `git worktree list`, `git bundle list-heads` o `git branch` fallan
(fail-closed: nunca sigue con una guarda vacía) y sale con exit 1 si algún borrado
queda pendiente.

Omite toda rama con PR abierto, checked-out en un worktree, la rama actual o
`main`, y toda rama que no esté en el bundle. **No borres a mano guiándote por el
manifiesto** (ver la advertencia de snapshot más arriba).

Una ref sólo se borra si su tip coincide con el SHA del bundle para esa superficie:
`refs/heads/*` y `refs/remotes/origin/*` se comparan por separado. Un tip nuevo se
omite. El borrado local usa `git update-ref -d <ref> <SHA>` y el remoto usa
`git push --force-with-lease=<ref>:<SHA> origin :<ref>`: un escritor que avance el
tip entre la lectura y el borrado hace que git rechace la operación. Se aborta
también si la consulta de PRs alcanza el límite de 500, porque puede estar truncada.
Las regresiones están en `tests/test_git_archive_scripts.py`, sobre repositorios
temporales y las dos superficies. Estos tests no borran ramas del repo del usuario.

## Publicación del bundle

El bundle está **subido como asset del release `branch-archive-20261004`**, hoy en
**borrador** (visible solo para quienes tienen acceso de escritura al repo).

- Asset: `obsolete-branches-20261004.bundle` (~17,8 MB)
- SHA-256: `BD3E2BE405F27434C7C3EE740D3349B48E9083025EF2A855333D90AAFD2CCD04`
- Release: https://github.com/FacundoSu1986/Sky-Claw/releases/tag/branch-archive-20261004

**Auditoría previa a subirlo.** El bundle contiene **43 tips** de commits que nunca
estuvieron publicados en ninguna ref remota. Se auditaron esos 43 trees:

| Búsqueda | Resultado |
|---|---|
| Patrones fuertes (API keys de OpenAI/OpenRouter/GitHub/AWS/Google/Slack, claves privadas PEM) | **0 coincidencias reales** — la única es la fixture sintética `sk-abc123...` de `tests/test_agent_guardrail.py`, ya pública |
| Patrón blando (credenciales asignadas a literales) | Solo referencias a *nombres* de variables de config (`llm_api_key`, `telegram_bot_token`, …), sin valores |
| Archivos sensibles trackeados (`.env`, `.pem`, `.key`, `id_rsa`, `.p12`, `.pfx`, `.netrc`, `.keystore`) | **ninguno** |

Quedó en borrador a propósito: publicar 43 commits de historia es una decisión
consciente del owner, no un efecto colateral. Para hacerlo público:

```bash
gh release edit branch-archive-20261004 --draft=false
```

Para verificar el asset una vez descargado:

```powershell
Get-FileHash obsolete-branches-20261004.bundle -Algorithm SHA256   # debe coincidir arriba
git bundle verify obsolete-branches-20261004.bundle              # "complete history"
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
