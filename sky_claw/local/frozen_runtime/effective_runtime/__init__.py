"""Oráculo de Effective Runtime — P5-A (ADR 0012 §13/§29.4, Q12).

Punto de entrada del slice: determina, **con evidencia y sin mutar nada**, qué
runtime ejecutarán MO2/SKSE, y expresa ``MATCH`` / ``MISMATCH`` / ``UNKNOWN``.

Alcance declarado:

- **Read-only.** Sin operaciones mutantes, y anclado por AST
  (``tests/test_frozen_runtime_p5a_effective_runtime_oracle.py``). El bridge de
  MO2 no se consulta ni se modifica: el plugin sólo acepta
  ``health``/``launch_worker``/``cancel`` (ADR 0007) y no expone el game path.
- **Sin placebo.** Si no hay evidencia suficiente el veredicto es ``UNKNOWN``, y
  ``UNKNOWN`` no autoriza activación (``EffectiveRuntimeAssessment`` lo garantiza
  en el tipo, no en cada llamador).
- **Sin autoridad inventada.** El Desired Clone sale de la autoridad durable; hoy
  no existe store de ``RuntimeCloneRecord``, así que ``desired_runtime_from_storage``
  deja ``clone_root=None`` y el veredicto de producción es ``UNKNOWN``. Es un
  hallazgo de FASE D, no un hueco de este slice.
"""

from __future__ import annotations

from sky_claw.local.frozen_runtime.effective_runtime.compatibility import (
    assess_runtime_compatibility,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    Mo2GamePathEvidence,
    decode_qt_byte_array,
    decode_qt_scalar,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    CompatibilityResult,
    DesiredRuntime,
    EffectiveRuntimeAssessment,
    EffectiveRuntimeOracleError,
    EffectiveRuntimeVerdict,
    EvidenceSource,
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
    ObservationState,
    ReasonCode,
    RuntimeCompatibility,
    RuntimeProbes,
    SurfaceObservation,
)
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    assess_effective_runtime,
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.effective_runtime.processes import (
    probe_live_processes,
    raiz_de_la_imagen,
)

__all__ = [
    "CompatibilityResult",
    "DesiredRuntime",
    "EffectiveRuntimeAssessment",
    "EffectiveRuntimeOracleError",
    "EffectiveRuntimeVerdict",
    "EvidenceSource",
    "LiveProcessEvidence",
    "LiveProcessKind",
    "LiveProcessSnapshot",
    "Mo2Executable",
    "Mo2GamePathEvidence",
    "ObservationState",
    "ReasonCode",
    "RuntimeCompatibility",
    "RuntimeProbes",
    "SurfaceObservation",
    "assess_effective_runtime",
    "assess_runtime_compatibility",
    "decode_qt_byte_array",
    "decode_qt_scalar",
    "default_runtime_probes",
    "desired_runtime_from_storage",
    "leer_ejecutables_de_mo2",
    "leer_game_path_de_mo2",
    "probe_live_processes",
    "raiz_de_la_imagen",
]
