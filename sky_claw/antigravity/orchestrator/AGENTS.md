<!--
  Pointer for AI agents working inside sky_claw/antigravity/orchestrator/.
  This subtree does not have a per-directory AGENTS.md with rules of its own;
  instead it redirects to the canonical pipeline SOP, which is the only
  authoritative source for pipeline-ordering, tool constraints, and
  failure-mode rules that affect tool_strategies/.
-->

# AGENTS.md — orchestrator pointer

This subtree (`sky_claw/antigravity/orchestrator/`) governs how the LLM
agent *calls* the Skyrim-modding tools. It does **not** re-state the
pipeline rules — those live in the canonical SOP and apply to every
strategy under `tool_strategies/`.

## Read this first

**`sky_claw/local/AGENTS.md`** — the canonical Skyrim modding pipeline
SOP. If you are editing any file under
`sky_claw/antigravity/orchestrator/tool_strategies/`, the §5
"AGENT CODE-EDITING RULES" block applies to your change.

## What this pointer is NOT

It is **not** a duplicate of the SOP. Do not paste pipeline rules here:
the SOP is the single source of truth and duplicating them creates
drift. Update the SOP, not this file.

## Repo-wide conventions

For coding conventions, contracts, and CI gates, see
[`../../../AGENTS.md`](../../../AGENTS.md).
