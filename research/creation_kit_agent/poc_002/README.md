# Creation Kit Agent — POC-002

Status: **PASS** (research-only proof of concept).

This directory contains the validated POC-002 code for a synthetic, headless TES4 inspection pipeline.

## Scope

- Synthetic TES4 fixture only; no Bethesda assets or binaries.
- `INSPECT_HEADER` is the only supported runtime capability.
- Candidate-only workspace model with strict path containment.
- Fail-closed orchestration, universal receipts, and original SHA-256 invariants.
- Golden fixture targets Skyrim SE header `1.70` with FormVersion `44`.
- Header `1.71` and real modern AE plugins are not covered by this POC.

## Not included

- POC-003 subprocess IPC code (still under hardening).
- Mutagen, xEdit, PapyrusCompiler, Creation Kit, CKPE, UI automation, network services, or LLM execution.

## Validation

Run from this directory:

```bash
python -m compileall .
python -m unittest test_suite.py -v
```

Expected validated baseline: **43 tests, OK**.

This code is intentionally isolated from the production `sky_claw/` package until later architecture gates are satisfied.
