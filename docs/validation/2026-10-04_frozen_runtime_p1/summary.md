# P1 Frozen Runtime — validación del entorno real (2026-10-04)

## Qué se ejecutó

Probe **READ-ONLY** (ninguna operación de escritura sobre disco del sistema,
anclado por AST en `tests/test_frozen_runtime_p1_readonly.py`):

```text
discover_managed_source()            → NOT_FOUND (0 candidatos, 0.0 s)
observe_source_snapshot()            → no ejecutado (sin source)
assess_managed_source_stability()    → no ejecutado (sin source)
```

## Resultado honesto

- `RUN=YES`, `RESULT=NOT_FOUND`.
- Esta máquina no tiene una instalación de Skyrim administrada por Steam
  detectable en `STEAM_DEFAULT_PATHS` (`C:\Program Files (x86)\Steam`,
  `C:\Program Files\Steam`, `D:\Steam`, `D:\SteamLibrary`).
- **No demuestra ni refuta** el contrato de estabilización con Steam real: la
  demostración del gate `STABLE(ManagedSource)` con señales reales de Steam
  queda pendiente del rig completo de **P7** (máquina con Skyrim + Steam).
- La cobertura sintética del contrato completo (discovery FOUND/AMBIGUOUS/
  INVALID, identidad, estabilización S01–S11, adversarial PRE/POST) está en
  `tests/test_frozen_runtime_p1.py` (34 tests en verde, mypy estricto).

## Q-04 — alcance cerrado

P1 cierra Q-04 **sólo** en el alcance demostrado:

> STABLE = la fuente estuvo estable durante la ventana observada
> (contrato acotado). NO promete estabilidad futura, NO es detección
> absoluta de actualizaciones de Steam.

Lo no demostrado sigue declarado como gate de P7, no asumido.
