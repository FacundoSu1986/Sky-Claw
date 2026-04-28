# Project: Skyclaw Main Sync

## Git — Reglas de operación

`.git` **debe preservarse**. No lo elimines aunque el IDE congele el panel del agente.

**Operaciones PROHIBIDAS bajo cualquier circunstancia:**

- `git init` — crea un nuevo repositorio (nunca necesario aquí)
- `git clone` — crea un checkout separado (usar worktrees en su lugar)
- `git worktree add` / `git worktree remove` — solo el sistema de agentes gestiona worktrees
- Crear o eliminar manualmente directorios `.git/`

**Si el panel de Antigravity se congela con `.git` presente:**  
→ Consultar `docs/TROUBLESHOOTING_ANTIGRAVITY.md` antes de cualquier acción destructiva.  
→ La causa habitual es `files.watcherExclude` incompleto en `.antigravity/settings.json`, NO el tamaño de `.git`.

**Operaciones de lectura permitidas en cualquier momento:**  
`git log`, `git diff`, `git show`, `git status`, `git branch`, `git config --list`

## Worktrees

El sistema de agentes gestiona worktrees bajo `.claude/worktrees/`. Estas rutas están excluidas del file-watcher del IDE y del índice Git (`.gitignore`). No las toques manualmente.

**NO usar el skill `using-git-worktrees`** — las worktrees son creadas y eliminadas por el harness de Claude Code automáticamente.

## Parallel Agents

Cuando uses `dispatching-parallel-agents`, ningún sub-agente debe ejecutar `git init` ni crear worktrees fuera de `.claude/worktrees/`. Todos los agentes deben respetar las reglas anteriores.
