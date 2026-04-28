# Troubleshooting: Antigravity IDE Agent Panel Freeze

> **Regla de oro:** Si el panel del agente de Antigravity se congela, **NO elimines `.git`**.  
> Sigue este runbook en orden. La mayoría de los casos se resuelven en la Capa 1.

---

## Diagnóstico rápido

| Síntoma | Causa probable | Ir a |
|---|---|---|
| Panel congela inmediatamente al abrir el workspace | File-watcher sin exclusiones | [Capa 1](#capa-1-file-watcher-sin-exclusiones) |
| Panel congela solo después de hacer `git commit` | Índice con archivos de churn alto | [Capa 2](#capa-2-archivos-rastreados-que-generan-churn) |
| `git status` tarda > 500 ms | FSMonitor o fscache inactivos | [Capa 3](#capa-3-git-performance) |
| Freeze desaparece al eliminar `.git` | Watcher está leyendo objetos de git | [Capa 1](#capa-1-file-watcher-sin-exclusiones) |
| Freeze solo con múltiples worktrees abiertas | Multiplicador de worktrees | [Capa 3](#capa-3-git-performance) + [Nota worktrees](#nota-worktrees) |

---

## Capa 1 — File-watcher sin exclusiones

**Causa raíz:** Antigravity (fork VS Code) monitorea *todo* el workspace por defecto. Con 13k+ archivos y `.git/objects/` en el scope, cada operación git dispara una cascada de eventos que satura el extension host.

**Solución:** Verificar que `.antigravity/settings.json` contiene estos bloques. Si no están, agregarlos:

```jsonc
// .antigravity/settings.json
{
    "files.watcherExclude": {
        "**/.git/objects/**": true,
        "**/.git/worktrees/**": true,
        "**/.venv/**": true,
        "**/node_modules/**": true,
        "**/.mypy_cache/**": true,
        "**/.ruff_cache/**": true,
        "**/.pytest_cache/**": true,
        "**/__pycache__/**": true,
        "**/.claude/worktrees/**": true,
        "**/.skyclaw_backups/**": true,
        "**/.serena/**": true,
        "**/dist/**": true,
        "**/build/**": true,
        "**/htmlcov/**": true
    },
    "search.exclude": {
        "**/.venv": true,
        "**/node_modules": true,
        "**/.mypy_cache": true,
        "**/.ruff_cache": true,
        "**/__pycache__": true,
        "**/.claude/worktrees": true,
        "**/dist": true,
        "**/build": true
    },
    "files.exclude": {
        "**/__pycache__": true,
        "**/.mypy_cache": true,
        "**/.ruff_cache": true,
        "**/*.pyc": true
    }
}
```

**Verificación:** Después de guardar, reiniciar el IDE. El process explorer debe estabilizar CPU del extension host < 5% en idle dentro de 10 segundos.

---

## Capa 2 — Archivos rastreados que generan churn

**Causa raíz:** Directorios con muchos archivos modificados frecuentemente (`.agents/`, cachés Python) estaban en el índice Git. Cada guardado dispara `git status` desde el IDE, que re-statea todos los archivos.

**Estado actual:** `.agents/` ya fue removido del índice (`git rm -r --cached .agents/`) y añadido a `.gitignore`. Los archivos siguen en disco.

### Si en el futuro necesitas repetir esta operación (nuevo directorio ruidoso):

```bash
# 1. Primero agregar a .gitignore
echo "nombre-directorio/" >> .gitignore

# 2. Luego remover del índice sin borrar del disco
git rm -r --cached nombre-directorio/

# 3. Verificar que los archivos siguen en disco
ls nombre-directorio/

# 4. Commitear
git add .gitignore
git commit -m "chore: untrack <nombre-directorio> (local-only artifacts)"
```

> **Importante:** `git rm --cached` NO borra archivos del disco. Solo los saca del índice.  
> Si luego haces `git checkout` o `git pull` en otro directorio, los archivos de ese directorio NO existirán hasta que los restaures.

---

## Capa 3 — Git performance

**Causa raíz:** Git en Windows re-statea el working tree completo en cada operación si fscache y fsmonitor están inactivos.

### Activar (solo se corre una vez por repo):

```bash
# Habilitar caché de filesystem de Windows
git config core.fscache true

# Pre-cargar el índice en paralelo
git config core.preloadIndex true

# Caché de archivos no rastreados
git config core.untrackedCache true

# Activar optimizaciones para repos con muchos archivos
git config feature.manyFiles true

# FSMonitor: delegar detección de cambios al OS (evita re-stat completo)
git config core.fsmonitor true

# Activar las flags en el índice actual
git update-index --untracked-cache
git update-index --fsmonitor
```

### Verificar:

```bash
time git status   # debe completar en < 50 ms
git config --get core.fsmonitor   # debe devolver "true"
```

### Compactación del objeto store (no destructiva):

```bash
git gc --prune=now
git repack -Ad
```

**Estado actual:** configuración ya aplicada. `git status` corre en ~40 ms.

---

## Nota worktrees

Las worktrees bajo `.claude/worktrees/` comparten el mismo `.git/objects/` con el repo principal. Si el IDE abre múltiples worktrees simultáneamente, el indexador del agente puede multiplicar el trabajo. Mitigación:

1. Abrir solo la worktree activa en Antigravity (no el workspace raíz + worktrees simultáneamente).
2. Las worktrees ya están excluidas del file-watcher via `files.watcherExclude`.
3. Si una worktree ya no se usa, eliminarla con `git worktree prune` (solo limpia las referencias; los archivos en `.claude/worktrees/<nombre>/` deben borrarse manualmente).

---

## Si ninguna capa resuelve el freeze

1. Abrir `Help > Toggle Developer Tools > Console` en Antigravity.
2. Buscar errores del tipo `ENOENT`, `EMFILE`, `chokidar`, o `watcher`.
3. Reportar el issue a Antigravity con el output completo de la consola y el siguiente repro mínimo:

```
Repro: workspace Python con .git válido + ~13k archivos (incluyendo .venv, .mypy_cache) 
sin files.watcherExclude configurado → agent panel congela al abrir.
```

---

## Historial de cambios aplicados a este repo

| Fecha | Acción | Impacto |
|---|---|---|
| 2026-04-28 | `files.watcherExclude` / `search.exclude` / `files.exclude` → `.antigravity/settings.json` | Elimina saturación primaria del file-watcher |
| 2026-04-28 | `.mypy_cache/`, `.ruff_cache/`, `.claude/worktrees/`, `.agents/` → `.gitignore` | Cierra asimetría `.gitignore` ↔ `.claudeignore` |
| 2026-04-28 | `git rm -r --cached .agents/` (188 archivos, 7.9 MB) | Elimina churn de índice por skills locales |
| 2026-04-28 | `core.fscache`, `core.untrackedCache`, `core.fsmonitor`, `feature.manyFiles` | Git status: O(N stats) → O(FSMonitor events) |
| 2026-04-28 | `git gc --prune=now && git repack -Ad` | 2 packs + 95 loose + 1 garbage → 1 pack limpio |
