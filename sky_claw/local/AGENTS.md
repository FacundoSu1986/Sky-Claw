<!--
  Canonical SOP for the Skyrim modding pipeline consumed by AI agents.
  Source: human-authored SOP (Spanish) → transformed to imperative English agent.md.
  Audience: any LLM agent (Claude Code, Cursor, Aider, Gemini, Codex) editing pipeline code.
  Scope: operational rules for xEdit, CAO, BodySlide, Pandora, LOOT, Wrye Bash,
         Synthesis, No Grass In Objects, TexGen, DynDOLOD.
  Companion file: ../../AGENTS.md (repo-wide coding conventions, two levels up).
-->

# SKYRIM MODDING PIPELINE — AGENT OPERATING PROCEDURES

> **READ THIS BEFORE touching any file under `sky_claw/local/tools/`, `sky_claw/app/orchestrator/tool_strategies/`, or `sky_claw/local/xedit/`.**
> Violating the pipeline order below will corrupt load orders, break patches, and silently desync the VFS.
>
> Human-readable companion: [`../../docs/pipeline/skyrim_sop.md`](../../docs/pipeline/skyrim_sop.md).
> This file remains the only canonical source for the complete pipeline DAG.

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
│          │ (after XPMSSE skeleton mods)  │ into engine format       │
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
│  STAGE 8 │ No Grass In Objects           │ Grass precache (NG)      │
│          │ (before DynDOLOD)             │ Prevents grass clipping  │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 9 │ TexGen → DynDOLOD 3           │ Dynamic LOD generation   │
│          │ (FINAL stage)                 │ Reads full topology      │
└─────────────────────────────────────────────────────────────────────┘
```

### Stage dependencies (documented here, NOT yet enforced at runtime)

| Stage | Requires completed | Blocks |
|-------|-------------------|--------|
| 1 xEdit QAC | (nothing) | 2, 5, 6, 7, 8, 9 |
| 2 CAO | (nothing, per-mod) | — |
| 3 BodySlide | skeleton + physics mods installed | 4 |
| 4 Pandora | animation mods + XPMSSE positioned | — |
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

**Inputs:** Structural body base mods (CBBE, 3BA), physics mods (CBPC, XPMSSE), and equipment mods with BodySlide support.

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

**Inputs:** Animation mods and universal skeleton patches (XPMSSE) installed and positioned in the load order FIRST.

**Procedure:**
1. Launch from the mod manager.
2. Allow full recompilation of the animation behavior list.
3. Re-run AFTER ANY change to animation mods. Stale behaviors cause T-poses and broken combat anims.

**Outputs:** Compiled behavior files injected into the engine's behavior graph.

**Verdict:** The exit code does NOT report the patching result. Pandora is
error-tolerant by design — on a malformed node it reverts that node to its
original state and keeps generating the rest, so it exits 0 having silently
dropped animation mods. That is exactly the "stale behaviors" state described
above, reached while reporting success. `Engine.log` is the only signal of what
was actually applied: `ERROR`/`FATAL` invalidate the run, `WARN` is reported but
does not decide.

If `Engine.log` is missing or unreadable, the runner MUST log that inability —
never silently treat it as a clean pass — and fall back to the exit-code
behavior described above. Because the log's location is itself an assumption
(not yet verified on a real rig), an unreadable log is also a signal to confirm
the path: run the pending smoke test (U-04) to verify where Pandora actually
writes `Engine.log`. Which exact severity Pandora uses for a node reversion is
**not verified against a real rig**:
`ERROR`'s own definition ("prevented [this] work from completing") reads as the
more literal match, so today's `ERROR`/`FATAL` gate is believed to cover it —
but if a real run shows reversions logged as `WARN` instead, this verdict does
NOT close the "stale behaviors" scenario and the gate needs to grow. See
`docs/pending_ooda_status.md` (Contrato de veredicto de éxito) for the pending
rig confirmation.

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
- **Error "DotNet SDK Not Detected":** Windows environment collision with x86 (32-bit) dotnet vestiges. Solution: use the official .NET uninstall tool (`dotnet-core-uninstall`) or "Apps & Features" to remove the x86 runtime, then install the **x64 channel SDK** (not the runtime alone — see §0 directive #10). Do NOT hand-delete `C:\Program Files (x86)\dotnet`; that path is owned by Windows Installer and direct removal can break MSI-revert.
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

**Procedure (STAGE 9 IS ASSISTED — the tools have NO headless mode):**
1. TexGen and DynDOLOD are GUI applications (PE Subsystem 2): they never write to stdout/stderr, so an exit-code-only success check is a false green. Sky-Claw launches them with the verified vector (`dyndolod.info/Help/Command-Line-Argument` + `xeInit.pas`): the loose game-mode switch `-sse` (or `-tes5vr` for VR) plus `-o:<root>` (administered output root), `-d:<Data>`, `-t:<temp>`, and — when configured — explicit `-m:<ini dir>` and `-p:<plugins.txt>`. The old vector (`-game SSE`, `-p <preset>`, bare `-t`, `--expert`) does not exist and is silently ignored; `--expert` is `Expert=1` in the INI, not an argument. **The quotes the official doc shows (`-o:"c:\Output\"`) belong to the command LINE, not to the value.** They are what you type in a `.lnk` or in MO2's *Arguments* field, where the Windows parser consumes them. Embedded in an argv element they become part of the path: `create_subprocess_exec` serializes with `subprocess.list2cmdline`, which escapes the inner quote, and the tool receives `-o:"C:\…\"` — not an absolute path, so it prepends the current drive and dies with `Can not create path C:\C:\…`. Measured on rig 2026-08-10: with that defect **no** TexGen or DynDOLOD run could start (2/2 binaries) even in a correct environment. Dropping the trailing backslash is NOT the fix (`-p:` has none and failed the same way) and contradicts the binary, which documents *"All path parameters must be specified with trailing backslash"*. The three tests that only asserted the string Sky-Claw produces had frozen the defect instead of catching it. **OPEN, pending T5:** `list2cmdline` quotes only when an argument contains spaces, and when it quotes it **doubles the trailing backslash** so it does not escape its own closing quote — correct under CRT rules, wrong under Delphi's, which never treats `\` as an escape (`ParamCount` is in the binary; xEdit reads `ParamStr`). Measured: with spaces, the CRT sees `-o:…\out\` and Delphi sees `-o:…\out\\`. **That is the branch that always runs on a real rig** (the game folder is `Skyrim Special Edition`, the INIs live under `My Games`), so `-d:` and `-m:` always contain spaces. Whether the doubled separator harms DynDOLOD is unverified; T5 settles it in one run because the tool echoes the parsed path in its log (`Using Output Path:`). Anchors: `test_dyndolod_argv_sin_espacios_sobrevive_los_dos_parsers` (real verification, both parsers agree), `test_switch_de_ruta_round_trip_sintetico` (same property without touching the filesystem, so it also covers machines whose username has a space) and `test_dyndolod_argv_con_espacios_diverge_entre_crt_y_delphi` (anchors the measured divergence; goes red if it ever disappears, forcing the decision to be re-read). Do NOT read any of them as end-to-end evidence. **Acceptance gate — this is a blocker, not a follow-up:** do NOT approve or merge a change to the launch path until a real TexGen/DynDOLOD run uses an administered output root **whose path contains a space** and the binary's log line `Using Output Path:` matches that root **exactly**. If they differ, fix the serialization in the shared `_build_xedit_args` path first, keeping the managed-switch and extra-argument validation behaviour intact. Passing unit tests are not a substitute: none of them exercises the Delphi runtime. **The five path switches have an OWNER — `DynDOLODConfig` — and `extra_args` cannot touch them.** `texgen_args`/`dyndolod_args` reach the runner from payload (`GenerateLodsStrategy._VALID_LOD_KEYS` filters the KEY, never the value), so `DynDOLODRunner._extra_args_admisibles` rejects, fail-closed and without normalizing: anything that is not `list[str]`, any element carrying its own quotes, any element redefining `-o:`/`-d:`/`-m:`/`-p:`/`-t:` (case-insensitive, `/o:` and leading-whitespace forms included — the xEdit parser does not document which one wins if a switch appears twice, and the staging, post-check and rollback all assume the output is where the config says), and **any element introducing a second game mode** (`-sse`/`-tes5`/`-tes5vr`/…). The game mode is a LOOSE switch with no `:`, so the path-switch rule does not see it, and its blast radius is larger than a diverted path: `-tes5` sends the run looking for the Skyrim LE registry key and dies with `Could not determine … installation path` (the 2026-08-05 rig error), `-tes5vr` on an SSE rig runs with the wrong definitions. It all lives in `_build_xedit_args` — the single piece BOTH executables cross — so neither launcher can be the sibling that got left out. Foreign xEdit switches (`-B:`, `-C:`, `-D:`…) stay allowed on purpose. Anchors: `test_dyndolod_extra_args_*` (every path switch and every known game mode × both launchers, plus prefix and case variants), `test_generate_lods_no_puede_inyectar_switches_por_payload` (the real `payload → strategy → service → runner` boundary), and the two AST anchors `test_letras_administradas_cubre_los_switches_que_emite_el_runner` / `test_game_modes_administrados_cubre_los_que_emite_el_runner`, which read the `switches` tuple and the `game_mode` assignment out of the runner and freeze them against the rejected sets — without the first, adding a sixth managed switch left it outside the rejection with the suite still green (measured by mutation in the #462 review). The game-mode list is fail-closed over the modes xEdit is known to document; it is NOT a proof of exhaustiveness, and a new xEdit mode would not make it go red.
2. The preset (Low/Med/High) and worldspace selection are GUI buttons chosen by the human in the wizard. Sky-Claw publishes `assisted=True` with operator instructions and waits; the 4h timeout covers the interaction window by design. `-m:`/`-p:` explicit are REQUIRED on rigs whose Documents folder is OneDrive-redirected: without them the tool dies with `Fatal: Could not find ini` (verified on rig 2026-08-05).
3. Success is decided by exit code AND artifact at the `-o:` root (`DynDOLOD.esp` as a file for DynDOLOD) AND no error lines in `Logs/{Tool}_{modo}_log.txt` — never by exit code alone. A missing or unreadable log is a warning, not a failure: the hard gate is the artifact at the `-o:` root. **The artifact must also be FRESH** — the file that DECIDES the verdict (`DynDOLOD.esp` for DynDOLOD; the most recent regular file in the staging for TexGen) has to have changed during the run, compared against a signature taken before launch. A GUI app the operator closed mid-way exits 0 without flushing a log, so without this the previous run's output satisfies the artifact check and stale LODs get packaged as a fresh result. The staging is not cleaned or moved aside between runs, so its mere presence proves nothing. Signing the whole *tree* is not enough either: a run that rewrites some LOD meshes and dies before regenerating the plugin changes the tree while leaving another run's `.esp` in place. Two limits are known and NOT closed, both for the same reason — there is no completeness marker other than the log, and the log may legitimately be missing: (a) TexGen has no named artifact, so an aborted run that wrote some textures still counts as fresh; (b) for DynDOLOD, "the plugin changed" equals "the run completed" **only if the tool persists `DynDOLOD.esp` at the end of its work** — an assumption nobody has verified against the binary. If it wrote the plugin early instead, a run closed after that point would pass the gate with a half-generated tree. Anyone with a real rig: confirm when `DynDOLOD.esp` is written, and this criterion can be tightened.
4. The administered output root is `<game>/Sky-Claw/DynDOLOD`. It hangs off the `Sky-Claw/` namespace on purpose: bare `<game>/DynDOLOD` is the folder the DynDOLOD Standalone archive extracts to, so an operator with the tool installed there would get its own installation directory back as `-o:`. Sky-Claw's write-permission preflight probes that root (plus the first existing ancestor, to prove the root can be CREATED on the first run) **and the executable's own directory**, where the tool writes its INI and the `Logs/` that step 3 reads.
5. Deploy the packaged results and insert them as the ABSOLUTE FINAL entry of the load order to guarantee overwrite priority.

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

**Rule:** Resolved EXCLUSIVELY by manipulating the priority hierarchy of the mod manager's left-panel VFS. In MO2's `modlist.txt`, the mod listed **LAST** has the **highest** loose-file priority (the file is read bottom-up; see `sky_claw/local/assets/asset_scanner.py::parse_modlist` which reverses the file). In MO2's left-pane UI, that means the mod **lower** in the list (towards the bottom of the pane) wins. There is no record-level merge for assets.

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
| Synthesis: "DotNet SDK Not Detected" | x86 dotnet vestiges on Windows | Use the official uninstall tool or Apps & Features, then reinstall the x64 SDK; do not delete managed folders by hand |
| Synthesis: "Max Masters Exceeded" | Output `.esp` exceeds 254 masters | Enable `Split Files if Max Masters Exceeded` |
| xEdit QAC: hang on NavMesh | Heavily corrupted NavMesh | Kill process, isolate plugin, skip with logged warning |
| Dawnguard: residual dirty edits after QAC | Single-pass cleaning insufficient | Run QAC TWICE + manual cleanup of CELL 00016BCF, 0001FA4C, 0006C3B6 |
| DynDOLOD: "Resources SE version information not found" | DLL hierarchy wrong | Place DynDOLOD DLL NG folder BENEATH Resources SE |
| DynDOLOD: engine crash / pointer overflow | Reference limit exceeded | Set `Temporary=1` in `DynDOLOD_SSE.ini` |
| No Grass In Objects: empty output (zero-bounds) | Third-party mod has null bounds `(0,0,0)` | Purge broken mesh via Creation Kit |
| Pandora: exit code 0 but animation mods silently missing (T-poses in game) | Engine is error-tolerant: it reverts the invalid node and keeps going, so the exit code does not report the patching result | Read `Engine.log`; `ERROR`/`FATAL` lines invalidate the run. Wired in `pandora_runner._leer_engine_log` — do NOT derive Pandora's verdict from the exit code alone |
| Grass clipping through roads after DynDOLOD | Grass precache ran AFTER DynDOLOD | Re-run pipeline: precache FIRST, DynDOLOD LAST |

---

## 5. AGENT CODE-EDITING RULES

When modifying pipeline code in this repository, the following rules apply ON TOP of `../../AGENTS.md`:

1. **NEVER** assume the strategy registration order in `build_orchestration_dispatcher()` (`sky_claw/app/orchestrator/tool_dispatcher.py`) mirrors the pipeline order in §1. It does NOT — strategies are registered as callable orchestration tools, not executed in pipeline order. The §1 order is enforced by the caller's tool sequence, not by registration order. Do not "fix" the registration order to match §1; that would be a regression.
2. **NEVER** introduce a code path that invokes DynDOLOD before Wrye Bash + Synthesis have completed. The current `GenerateLodsStrategy.execute()` (see `sky_claw/app/orchestrator/tool_strategies/generate_lods.py`) does NOT check upstream completion — a runtime guard is still needed and any new LOD-invocation path MUST add one.
3. **NEVER** allow Wrye Bash strategy to merge categories beyond Leveled Lists without an explicit user override flag. Default scope = Leveled Lists ONLY.
4. **ALWAYS** emit a `success: bool` + `message: str` from any new tool runner (see `tool_result.py` contract in `../../AGENTS.md`).
5. **ALWAYS** log the pipeline stage index when a tool fails. Stage index is the primary debugging signal. The canonical form is **structured**, not prose: `extra={"pipeline_stage": N}` (as in `grass_cache_service.py`), or `extra=subprocess_error_extra(..., pipeline_stage=N)` when the failure is a subprocess exit (as in `loot/cli.py` and `xedit/runner.py`). Prose markers alone do NOT satisfy the rule — the repo carries three mutually incompatible spellings (`(stage 9)`, `(fase 6)`, `[FASE-6]`) that no log query can join on; keep them for human readability, but the field is the contract. Prefer emitting it from the **service** layer (`*_service.py`), which owns the §1 DAG position; a runner that hardcodes its own stage number is coupled to that position and silently lies if the stage is ever reused or reordered. Note this is a preference the codebase does not yet honour uniformly: `loot/cli.py` and `xedit/runner.py` hardcode `pipeline_stage=5` and `=1` in the runner. Do not cite "runners never carry the stage index" as settled precedent — it is not. The obligation is per **failure record**, not per handler: an early `return` that logs and bails, and a helper that emits the canonical outcome record on the handler's behalf, both owe the field. Tagging the `except` and leaving the record it produces untagged is the sibling defect of `../../AGENTS.md`, committed inside the fix for it. Anchors (`tests/test_dyndolod_service.py`): every `logger.error`/`warning`/`critical` in `dyndolod_service.py` is detected by AST and must emit the field; the `except` family of `execute` is frozen with multiplicity; all eight `*_service.py` are enumerated and split into compliant vs explicitly exempt, so a new service breaks the test until someone decides which it is. **Known debt:** only 2 of the 8 comply (grass_cache, dyndolod). The other six are exempt *by enumeration, not by verification* — `_SERVICIOS_SIN_STAGE_INDEX` records each one's stage (loot 5, pandora 4, synthesis 7, wrye_bash 6, xedit 1, vramr undetermined). Closing that is one PR per service; the enumeration only guarantees the debt cannot grow silently.
6. **NEVER** mock the 254-master limit in tests. The Auto-Split directive is load-bearing for large mod lists and must be exercised, not stubbed.
7. **ALWAYS** write tests in Spanish (repo convention from `../../AGENTS.md`) even though this document is in English. The SOP is English-canonical for agent consumption; the test suite is Spanish-canonical for human convention.
8. **NEVER** remove the Dawnguard double-clean special case from `quick_auto_clean.py`. It is a documented anomaly, not a bug.

---

*End of pipeline operating procedures. For repo-wide coding conventions, see [`../../AGENTS.md`](../../AGENTS.md).*
