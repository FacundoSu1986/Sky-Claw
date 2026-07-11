<!--
  Canonical SOP for the Skyrim modding pipeline consumed by AI agents.
  Source: human-authored SOP (Spanish) → transformed to imperative English agent.md.
  Audience: any LLM agent (Claude Code, Cursor, Aider, Gemini, Codex) editing pipeline code.
  Scope: operational rules for xEdit, CAO, BodySlide, Pandora, LOOT, Wrye Bash,
         Synthesis, No Grass In Objects, TexGen, DynDOLOD.
  Companion file: ../AGENTS.md (repo-wide coding conventions).
-->

# SKYRIM MODDING PIPELINE — AGENT OPERATING PROCEDURES

> **READ THIS BEFORE touching any file under `sky_claw/local/tools/`, `sky_claw/antigravity/orchestrator/tool_strategies/`, or `sky_claw/local/xedit/`.**
> Violating the pipeline order below will corrupt load orders, break patches, and silently desync the VFS.

---

## 0. AGENT DIRECTIVES (NON-NEGOTIABLE)

1. **ALWAYS** execute pipeline stages in the exact chronological order defined in §1. Reordering stages corrupts downstream patches.
2. **NEVER** generate a dynamic patch (Wrye Bash, Synthesis) before LOOT has stabilized `loadorder.txt`. Patches built on an unstable order are garbage.
3. **NEVER** run TexGen or DynDOLOD before Wrye Bash + Synthesis are complete. LOD generation reads the full topological state; missing patches produce pop-in and missing references.
4. **NEVER** use Wrye Bash to merge magic effects, acoustic parameters, or spell cost records. Doing so multiplies mana costs in magic overhauls. Leveled Lists ONLY.
5. **NEVER** clean more than ONE plugin per xEdit QAC invocation. Batch cleaning causes cross-contamination of NavMesh fixes.
6. **CRITICAL:** Dawnguard.esm requires TWO QAC passes plus manual cell cleanup. Treat single-pass cleaning of Dawnguard as a defect.
7. **CRITICAL:** Skyrim rejects any `.esp` with more than 254 masters. In loads exceeding ~1000 mods, Synthesis MUST enable `Split Files if Max Masters Exceeded` (Auto-Split) or it will crash.
8. **CRITICAL:** Grass precache (No Grass In Objects) MUST run before DynDOLOD. Reversed order produces grass clipping through roads and ruins.
9. **NEVER** install Synthesis inside the MO2-managed directory tree. It belongs in a virgin path (e.g. `C:\Tools\Synthesis`). Pre-cache patch config from GitHub OUTSIDE MO2; only the final render pass runs inside MO2.
10. **NEVER** rely on the .NET Runtime alone for Synthesis. The SDK is mandatory. Runtime-only installs produce `DotNet SDK Not Detected`.

---

## 1. PIPELINE ARCHITECTURE (CHRONOLOGICAL EXECUTION ORDER)

The pipeline is a strict DAG. Each stage consumes the output of the previous stage. Skipping, reordering, or re-running a middle stage invalidates every downstream artifact.

```
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 1 │ xEdit / QuickAutoClean        │ Sanitize Master Files    │
│          │ (Update.esm, DLCs)            │ Remove ITMs / UDRs       │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 2 │ Cathedral Assets Optimizer    │ Per-mod asset packaging  │
│          │ (CAO)                         │ Textures, mipmaps, .bsa  │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 3 │ BodySlide + Outfit Studio     │ Build morphological      │
│          │ (Batch Build)                 │ meshes + armor conform   │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 4 │ Pandora Behaviour Engine      │ Compile AI behaviors     │
│          │ (after XMPSSE skeleton mods)  │ into engine format       │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 5 │ LOOT                          │ Stabilize load order     │
│          │ (BEFORE any patch)            │ Verify master deps       │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 6 │ Wrye Bash                     │ Merge Leveled Lists into │
│          │ (after LOOT)                  │ Bashed Patch, 0.esp      │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 7 │ Synthesis                     │ Dynamic mutators +       │
│          │ (after Wrye Bash)             │ Synthesis.esp            │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 8 │ No Grass In Objects           │ Grass precache cache     │
│          │ (before DynDOLOD)             │ Prevents grass clipping  │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 9 │ TexGen → DynDOLOD 3           │ Dynamic LOD generation   │
│          │ (FINAL stage)                 │ Reads full topology      │
└─────────────────────────────────────────────────────────────────────┘
```

### Stage dependencies (enforced in code)

| Stage | Requires completed | Blocks |
|-------|-------------------|--------|
| 1 xEdit QAC | (nothing) | 2, 5, 6, 7, 8, 9 |
| 2 CAO | (nothing, per-mod) | — |
| 3 BodySlide | skeleton + physics mods installed | 4 |
| 4 Pandora | animation mods + XMPSSE positioned | — |
| 5 LOOT | 1 (clean masters) | 6, 7, 8, 9 |
| 6 Wrye Bash | 5 (LOOT) | 7, 9 |
| 7 Synthesis | 6 (Wrye Bash) | 9 |
| 8 No Grass In Objects | 5 (LOOT) | 9 |
| 9 TexGen + DynDOLOD | 6 + 7 + 8 ALL complete | — |

---

## 2. TOOL CONSTRAINTS (IMPERATIVE RULES PER TOOL)

### 2.1 xEdit / SSEEdit (QuickAutoClean — QAC)

**Purpose:** Structural debugger for plugin hierarchies. Neutralizes ITMs (Identical to Master), repairs UDRs (Undeleted Reference), prevents NavMesh corruption.

**Inputs:** Official master files (`Update.esm`, DLC `.esm` files) and conflicting plugins flagged by LOOT.

**Procedure:**
1. Configure `SSEEdit.exe` in the mod manager with the argument `-quickautoclean` (or `-qac`).
2. Select EXACTLY ONE file per run. NEVER batch-clean.
3. Allow the three logical debug passes to complete. Save when prompted.

**Outputs:** Sanitized master files / plugins written back to disk.

**Exceptions — MANDATORY handling:**
- **Header 1.71 warnings on stale platforms:** expected; do not abort.
- **Hangs on heavily corrupted NavMeshes:** kill the process, isolate the offending plugin, and skip its QAC pass with a logged warning.
- **Dawnguard.esm — CRITICAL ANOMALY:** requires TWO automatic QAC passes, followed by MANUAL cleanup of three specific cells: `CELL 00016BCF`, `CELL 0001FA4C`, `CELL 0006C3B6`. Single-pass cleaning of Dawnguard is a defect.

---

### 2.2 Cathedral Assets Optimizer (CAO)

**Purpose:** Per-mod asset optimization. Compresses textures, generates mipmaps, packs loose meshes and scripts into `.bsa` archives.

**Inputs:** Individual, already-installed mods (process ONE at a time).

**Procedure:**
1. Launch CAO outside the mod manager (standalone).
2. Point at a single mod's asset directory.
3. Run the optimization profile. Verify output `.bsa` integrity before deploying.

**Outputs:** Compressed textures, generated mipmaps, packed `.bsa` archives.

**Rule:** NEVER run CAO on the entire `mods/` directory at once. Per-mod processing is mandatory to isolate failures.

---

### 2.3 BodySlide and Outfit Studio

**Purpose:** Interactive mesh configuration. Builds morphological body bases (CBBE, 3BA) and batch-conforms armors and clothing to preset proportions.

**Inputs:** Structural body base mods (CBBE, 3BA), physics mods (CBPC, XMPSSE), and equipment mods with BodySlide support.

**Procedure:**
1. Launch from the mod manager.
2. Designate the body base in the interface.
3. Choose muscular proportions or a predefined preset.
4. Click **Batch Build** to mass-assemble all armors.

**Outputs:** 3D body meshes (male and female) and conforming armor sets (`.nif` format).

**Rule:** Re-run Batch Build after ANY change to the body base mod. Stale armor meshes will not match the active body.

---

### 2.4 Pandora Behaviour Engine

**Purpose:** Dynamically compiles AI behaviors, character/creature animations, and skeleton schemas into a format the Bethesda engine can ingest.

**Inputs:** Animation mods and universal skeleton patches (XMPSSE) installed and positioned in the load order FIRST.

**Procedure:**
1. Launch from the mod manager.
2. Allow full recompilation of the animation behavior list.
3. Re-run AFTER ANY change to animation mods. Stale behaviors cause T-poses and broken combat anims.

**Outputs:** Compiled behavior files injected into the engine's behavior graph.

---

### 2.5 LOOT (Load Order Optimisation Tool)

**Purpose:** Automatic load-order sorting. Verifies structural errors (incompatibilities, missing master dependencies) and emits per-mod warnings.

**Inputs:** Installed mods (`.esp`, `.esm`, `.esl`) and the system file `plugins.txt`.

**Procedure:**
1. Launch LOOT through the mod manager (MO2 or Vortex).
2. Trigger the **sort** operation.
3. Review emitted messages (ITM warnings, missing requirements).
4. Apply the generated order.

**Outputs:** Automatic updates to `loadorder.txt` and `plugins.txt`.

**Exceptions:**
- **Error "Something went wrong!":** `plugins.txt` is read-only. Clear the read-only attribute and retry.
- **LOOT detects zero installed mods:** the VFS game path is built on symlinks. LOOT cannot resolve mods through symlinked VFS. Use a direct path or reconfigure the VFS without symlinks.

---

### 2.6 Wrye Bash

**Purpose:** Consolidates conflicting databases into a single optimized plugin (`Bashed Patch, 0.esp`). Primary focus: merging **Leveled Lists** so items injected by multiple mods coexist additively instead of overwriting each other.

**Inputs:** Active plugins previously sorted by LOOT into a logical sequence.

**Procedure:**
1. Launch Wrye Bash STRICTLY from the mod manager's virtualized environment.
2. Locate the inert plugin `Bashed Patch, 0.esp` (bottom of the load order).
3. Right-click → **Rebuild patch**.
4. Configure categories (preferably Leveled Lists ONLY).
5. Confirm with **Build patch**.

**Outputs:** Unified consolidation plugin `Bashed Patch, 0.esp`.

**Exceptions:**
- **Error "FILE NOT FOUND":** a master was moved or deleted between runs. Re-run LOOT to refresh the master list, then rebuild.
- **"Unrecognized version" or CTD on Skyrim ≥ 1.6.1130 (Header 1.71):** Wrye Bash cannot process Header 1.71 plugins natively. Inject the **BEES** mod (Backported Extended ESL Support) before rebuilding.

**CRITICAL RULE:** NEVER use Wrye Bash to merge acoustic parameters or magic effect values. Magic overhauls will suffer mana-cost multiplication bugs. Leveled Lists ONLY.

---

### 2.7 Synthesis

**Purpose:** Multi-threaded dynamic patch framework. Replaces hundreds of static individual patches with algorithmic mutators in a single consolidated plugin. Handles combat AI, cell lighting, and climate variables via real-time mathematical/logical resolution.

**Inputs:** .NET SDK (NOT Runtime) installed at the OS level. For 2026 revisions: .NET 10 SDK.

**Procedure:**
1. Extract Synthesis into a virgin, exclusively-owned directory OUTSIDE the mod manager and game folders (e.g. `C:\Tools\Synthesis`).
2. Launch and configure patches from GitHub repositories (pre-cache phase, OUTSIDE MO2).
3. For the final render of the compiled output, invoke Synthesis THROUGH the MO2 environment so the output `.esp` is deposited correctly.

**Outputs:** A single consolidated plugin `Synthesis.esp`.

**Exceptions:**
- **Error "DotNet SDK Not Detected":** Windows environment collision with x86 (32-bit) dotnet vestiges. Solution: delete `/Program Files (x86)/dotnet` and reinstall the x64 channel SDK.
- **Failure "Max Masters Exceeded":** Skyrim rejects any `.esp` requiring more than 254 masters. In loads exceeding ~1000 mods, Synthesis will fail here. ENABLE the directive **`Split Files if Max Masters Exceeded`** (Auto-Split) to fragment the output.

---

### 2.8 No Grass In Objects (Grass Precache)

**Purpose:** Generates spatial metrics and integrates them into a cache file to prevent grass geometry from clipping through asphalt, roads, and ruins.

**Inputs:** Native mod No Grass in Objects NG, Address Library for SKSE, and (mandatory on Anniversary Edition) Grass Cache Helper NG.

**Procedure (MO2 / VFS automated):**
1. Install the **Grass Generation MO2 Plugin v1.5** and point it at the valid SKSE binary.
2. TEMPORARILY limit the environment to 800x400 resolution with visual shaders and ENB disabled. This tolerates the engine's repeated cell scans without thermal hangs.
3. Trigger **Precache Grass** from MO2. The system will systematically open and close the game while reading cells.
4. Output: a compiled `\Grass\` subfolder auto-deposited in the `Overwrite` folder.

**Exceptions:**
- **"Zero-bounds" failure (empty output folders/files):** a third-party mod contains records with null bounds `(0,0,0)`. Purge the broken mesh dependency via the Creation Kit before retrying.

---

### 2.9 TexGen & DynDOLOD 3

**Purpose:** Parametric compilation suite for Dynamic Distant LOD. Eliminates pop-in by projecting a coordinated horizon aligned with the active tree, rock, and building assets.

**Inputs:** DynDOLOD Resources SE, Address Library for SKSE Plugins, and DynDOLOD DLL NG (required for executable version 1.6.1170).

**Procedure:**
1. Run `TexGen.exe` from the mod manager. Package and mount its output as an active mod.
2. Run `DynDOLODx64.exe`. Select desired worldspaces and quality level (Low / Med / High).
3. Deploy the packaged results and insert them as the ABSOLUTE FINAL entry of the load order to guarantee overwrite priority.

**Outputs:** Packaged visual geographic memory data, spatial `.esp`/`.esm` plugins, and temporal visual injection.

**Exceptions:**
- **Error "DynDOLOD Resources SE version information not found":** force the dynamic DLL folder (DynDOLOD DLL NG) to sit BENEATH the official Resources SE directory so it dominates the overwrite hierarchy.
- **Engine crash from pointer overflow:** set `Temporary=1` in `DynDOLOD_SSE.ini`. This releases real-time reference limits at the engine boundary.

---

## 3. CONFLICT RESOLUTION PROTOCOL

The Skyrim engine does NOT resolve all conflicts via disk overwrites. Three conceptual layers MUST be respected.

### Layer 1 — Rule of One (Plugin Databases)

**Scope:** Plugin databases (`.esp` / `.esm` / `.esl` records).

**Rule:** If Mod A and Mod B both alter the health or inventory of the same actor, the mod that loads PHYSICALLY LAST in the load order permanently nullifies the changes of the earlier mod. There is no additive merge at this layer without an external patcher.

### Layer 2 — Systematic Pure-Record Management (Lists and Databases)

To escape the paralyzing effect of the Rule of One, two patchers are used and they MUST NOT overlap:

| Conflict class | Resolver | Behavior |
|---------------|----------|----------|
| Leveled Lists (inventories injected into containers and NPCs) | **Wrye Bash** | Additive merge — engine SUMS entries instead of overwriting |
| Mass overwrites (AI behaviors, logical conditions, climate variables) | **Synthesis** | Real-time parametric mutators unify all overrides into one plugin |

**NEVER** delegate Leveled Lists to Synthesis when Wrye Bash has already merged them. **NEVER** delegate AI/climate logic to Wrye Bash. The split is canonical.

### Layer 3 — Physical Asset Management (Loose Files and BSAs)

**Scope:** Graphical conflicts (e.g. two mods overwriting the same brick texture).

**Rule:** Resolved EXCLUSIVELY by manipulating the priority hierarchy of the mod manager's left-panel VFS. The mod higher in the left panel wins precedence for loose files. There is no record-level merge for assets.

**Coherence rule:** Use CAO to compress loose files into `.bsa` archives. This prevents unnecessary disk reads and ensures spatial coherence. Uncompressed loose-file loads are a performance defect.

---

## 4. CRITICAL FAILURE MODES — QUICK REFERENCE

| Symptom | Root cause | Mandatory fix |
|---------|-----------|---------------|
| LOOT: "Something went wrong!" | `plugins.txt` read-only | Clear read-only attribute |
| LOOT: zero mods detected | VFS path uses symlinks | Reconfigure VFS with direct path |
| Wrye Bash: "FILE NOT FOUND" | Master moved/deleted between runs | Re-run LOOT, then rebuild |
| Wrye Bash: "Unrecognized version" / CTD on Header 1.71 | Native incompatibility with 1.71 | Inject BEES mod before rebuild |
| Magic overhaul: mana costs multiplied | Wrye Bash merged magic effects | Rebuild patch with Leveled Lists ONLY; NEVER merge magic |
| Synthesis: "DotNet SDK Not Detected" | x86 dotnet vestiges on Windows | Delete `/Program Files (x86)/dotnet`, reinstall x64 SDK |
| Synthesis: "Max Masters Exceeded" | Output `.esp` exceeds 254 masters | Enable `Split Files if Max Masters Exceeded` |
| xEdit QAC: hang on NavMesh | Heavily corrupted NavMesh | Kill process, isolate plugin, skip with logged warning |
| Dawnguard: residual dirty edits after QAC | Single-pass cleaning insufficient | Run QAC TWICE + manual cleanup of CELL 00016BCF, 0001FA4C, 0006C3B6 |
| DynDOLOD: "Resources SE version information not found" | DLL hierarchy wrong | Place DynDOLOD DLL NG folder BENEATH Resources SE |
| DynDOLOD: engine crash / pointer overflow | Reference limit exceeded | Set `Temporary=1` in `DynDOLOD_SSE.ini` |
| No Grass In Objects: empty output (zero-bounds) | Third-party mod has null bounds `(0,0,0)` | Purge broken mesh via Creation Kit |
| Grass clipping through roads after DynDOLOD | Grass precache ran AFTER DynDOLOD | Re-run pipeline: precache FIRST, DynDOLOD LAST |

---

## 5. AGENT CODE-EDITING RULES

When modifying pipeline code in this repository, the following rules apply ON TOP of `../AGENTS.md`:

1. **ALWAYS** preserve the stage ordering encoded in `sky_claw/antigravity/orchestrator/tool_strategies/`. The strategy registration order mirrors §1.
2. **NEVER** introduce a code path that invokes DynDOLOD before Wrye Bash + Synthesis have completed. Add a runtime guard if one does not exist.
3. **NEVER** allow Wrye Bash strategy to merge categories beyond Leveled Lists without an explicit user override flag. Default scope = Leveled Lists ONLY.
4. **ALWAYS** emit a `success: bool` + `message: str` from any new tool runner (see `tool_result.py` contract in `../AGENTS.md`).
5. **ALWAYS** log the pipeline stage index when a tool fails. Stage index is the primary debugging signal.
6. **NEVER** mock the 254-master limit in tests. The Auto-Split directive is load-bearing for large mod lists and must be exercised, not stubbed.
7. **ALWAYS** write tests in Spanish (repo convention from `../AGENTS.md`) even though this document is in English. The SOP is English-canonical for agent consumption; the test suite is Spanish-canonical for human convention.
8. **NEVER** remove the Dawnguard double-clean special case from `quick_auto_clean.py`. It is a documented anomaly, not a bug.

---

*End of pipeline operating procedures. For repo-wide coding conventions, see [`../AGENTS.md`](../AGENTS.md).*
