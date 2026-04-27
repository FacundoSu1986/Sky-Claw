# Project: Skyclaw Main Sync

## CRITICAL RULES — GIT OPERATIONS BLOCKED

**NEVER execute any of the following under any circumstances:**

- `git init`
- `git clone`
- `git worktree add`
- `git worktree remove`
- Creating or modifying `.git/` directories
- Any command that initializes, creates, or modifies Git repository structures

**Rationale:** This project must NOT have a `.git` directory. The presence of `.git` causes a critical failure that disables the AI agent panel in the Antigravity IDE.

**If you need version control context:** Use `git log`, `git diff`, `git show`, or `git status` ONLY if a `.git` already exists elsewhere. Never create one.

## Worktrees

**DO NOT use the `using-git-worktrees` skill in this project.** The worktree feature requires a Git repository and will trigger `git init` automatically.

## Parallel Agents

When using `dispatching-parallel-agents`, ensure no sub-agent executes `git init` or creates worktrees. All agents must respect the rules above.
