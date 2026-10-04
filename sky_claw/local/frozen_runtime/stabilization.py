"""Estabilización de la Managed Source: ventana PRE/POST multi-señal (P1).

Contrato acotado (Q-04): ``STABLE`` = "la fuente estuvo estable durante la
ventana observada". NO promete estabilidad futura; P3 repite PRE/copy/POST y
SFR-15 sigue siendo la defensa principal contra mutación durante la copia.

Reglas del gate (en orden):

1. provider pre-check: manifest ilegible/ausente ⇒ INDETERMINATE (S07);
   cualquier señal de actividad ⇒ UNSTABLE (S06).
2. medición PRE (identidad + inventario sellado): fallo ⇒ INDETERMINATE (S08).
3. ventana silenciosa configurable (inyectable en tests).
4. provider post-check: manifest ilegible ⇒ INDETERMINATE; actividad ⇒ UNSTABLE
   (S11); buildid cambió ⇒ UNSTABLE (S09).
5. medición POST: fallo ⇒ INDETERMINATE (S08).
6. árbol PRE != POST ⇒ UNSTABLE (S02–S05, S10) — el árbol es la evidencia
   primaria; la metadata del proveedor es advisory.
7. identidad de runtime PRE != POST ⇒ UNSTABLE (S05).
8. todo igual y sin actividad ⇒ STABLE.

Respuesta a actividad: UNSTABLE (esperar/observar otra ventana). NUNCA se
bloquea, mata o modifica al proveedor (Steam) — invariante central del ADR.

Sólo lectura absoluta sobre la Managed Source.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError, FrozenRuntimeObservationError
from sky_claw.local.frozen_runtime.models import (
    ManagedSource,
    ProviderActivitySignals,
    SourceMeasurement,
    SourceStabilityResult,
    StabilityState,
    StableSourceObservation,
)
from sky_claw.local.frozen_runtime.observation import medir_fuente, snapshot_from_measurement
from sky_claw.local.frozen_runtime.provider_signals import observe_provider_activity

logger = logging.getLogger(__name__)

DEFAULT_QUIET_WINDOW_SECONDS: float = 2.0


def _indeterminate(
    message: str,
    *,
    pre: SourceMeasurement | None = None,
    pre_provider: ProviderActivitySignals | None = None,
    post_provider: ProviderActivitySignals | None = None,
) -> SourceStabilityResult:
    return SourceStabilityResult(
        state=StabilityState.INDETERMINATE,
        message=message,
        pre_tree_digest=pre.tree_digest if pre is not None else None,
        pre_runtime_identity=pre.runtime_identity if pre is not None else None,
        pre_provider=pre_provider,
        post_provider=post_provider,
        observed_at_ns=time.time_ns(),
    )


def _unstable(
    message: str,
    *,
    pre: SourceMeasurement | None = None,
    post: SourceMeasurement | None = None,
    pre_provider: ProviderActivitySignals | None = None,
    post_provider: ProviderActivitySignals | None = None,
) -> SourceStabilityResult:
    return SourceStabilityResult(
        state=StabilityState.UNSTABLE,
        message=message,
        pre_tree_digest=pre.tree_digest if pre is not None else None,
        post_tree_digest=post.tree_digest if post is not None else None,
        pre_runtime_identity=pre.runtime_identity if pre is not None else None,
        post_runtime_identity=post.runtime_identity if post is not None else None,
        pre_provider=pre_provider,
        post_provider=post_provider,
        observed_at_ns=time.time_ns(),
    )


def _artefactos_parciales(med: SourceMeasurement) -> list[str]:
    """Nombres de archivos ``*.part`` observados en el inventario sellado.

    Señal del proveedor dentro del propio árbol: una descarga parcial estable
    (manifest idle, staging vacío) no debe producir evidencia de snapshot.
    """
    return [f.rel_path for f in med.files if f.rel_path.casefold().endswith(".part")][:5]


def _run_window(
    source: ManagedSource,
    *,
    quiet_window_seconds: float,
    sleep: Callable[[float], None],
) -> tuple[SourceStabilityResult, SourceMeasurement | None]:
    inicio = time.perf_counter()

    pre_provider = observe_provider_activity(source)
    if not pre_provider.manifest_readable:
        return (
            _indeterminate(
                f"manifest del proveedor ilegible o ausente en el pre-check: {pre_provider.manifest_parse_error}",
                pre_provider=pre_provider,
            ),
            None,
        )
    if pre_provider.update_in_progress:
        return _unstable("el proveedor reporta actividad de actualización (pre-check)", pre_provider=pre_provider), None
    if not pre_provider.state_flags or not pre_provider.state_flags.strip():
        # Condición (a) del gate: un manifest legible SIN StateFlags legible no
        # demuestra reposo del proveedor ⇒ no se puede afirmar estabilidad.
        return (
            _indeterminate(
                "manifest legible sin StateFlags observable (pre-check): no se puede demostrar reposo",
                pre_provider=pre_provider,
            ),
            None,
        )

    try:
        pre = medir_fuente(source)
    except FrozenRuntimeObservationError as exc:
        return _indeterminate(f"no se pudo medir la fuente (PRE): {exc}", pre_provider=pre_provider), None

    sleep(quiet_window_seconds)

    post_provider = observe_provider_activity(source)
    if not post_provider.manifest_readable:
        return (
            _indeterminate(
                f"manifest del proveedor ilegible o ausente en el post-check: {post_provider.manifest_parse_error}",
                pre=pre,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )
    if post_provider.update_in_progress:
        return (
            _unstable(
                "el proveedor reporta actividad de actualización (post-check); el árbol no es la única señal (S11)",
                pre=pre,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )
    if post_provider.buildid != pre_provider.buildid:
        return (
            _unstable(
                f"el buildid del proveedor cambió durante la ventana ({pre_provider.buildid!r} → {post_provider.buildid!r})",
                pre=pre,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )
    if not post_provider.state_flags or not post_provider.state_flags.strip():
        return (
            _indeterminate(
                "manifest legible sin StateFlags observable (post-check): no se puede demostrar reposo",
                pre=pre,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )

    try:
        post = medir_fuente(source)
    except FrozenRuntimeObservationError as exc:
        return (
            _indeterminate(
                f"no se pudo medir la fuente (POST): {exc}",
                pre=pre,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )

    parciales = _artefactos_parciales(pre) + _artefactos_parciales(post)
    if parciales:
        return (
            _unstable(
                "artefacto(s) de descarga parcial presentes en el árbol "
                f"({', '.join(sorted(set(parciales)))}): el proveedor no está en reposo",
                pre=pre,
                post=post,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )
    if post.tree_digest.digest != pre.tree_digest.digest:
        return (
            _unstable(
                "el árbol cambió durante la ventana (TreeDigest PRE != POST); la metadata del proveedor no lo exculpa (S10)",
                pre=pre,
                post=post,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )
    if post.runtime_identity != pre.runtime_identity:
        return (
            _unstable(
                "la identidad de runtime cambió durante la ventana "
                f"({pre.runtime_identity.game_version} → {post.runtime_identity.game_version})",
                pre=pre,
                post=post,
                pre_provider=pre_provider,
                post_provider=post_provider,
            ),
            None,
        )

    elapsed = time.perf_counter() - inicio
    logger.info(
        "frozen_runtime managed source stability verdict",
        extra={
            "event": "frozen_runtime_stability",
            "provider": source.provider.value,
            "appid": source.appid,
            "root": str(source.root),
            "verdict": StabilityState.STABLE.value,
            "files": post.tree_digest.files,
            "bytes": post.tree_digest.bytes,
            "elapsed_seconds": round(elapsed, 3),
            "quiet_window_seconds": quiet_window_seconds,
            "runtime_version": post.runtime_identity.game_version,
        },
    )
    return (
        SourceStabilityResult(
            state=StabilityState.STABLE,
            message="la fuente estuvo estable durante la ventana observada (contrato acotado, no promete estabilidad futura)",
            pre_tree_digest=pre.tree_digest,
            post_tree_digest=post.tree_digest,
            pre_runtime_identity=pre.runtime_identity,
            post_runtime_identity=post.runtime_identity,
            pre_provider=pre_provider,
            post_provider=post_provider,
            observed_at_ns=time.time_ns(),
        ),
        post,
    )


def assess_managed_source_stability(
    source: ManagedSource,
    *,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> SourceStabilityResult:
    """Evalúa la estabilidad de la Managed Source sobre una ventana PRE/POST.

    ``sleep`` y ``quiet_window_seconds`` son inyectables: los tests usan
    hooks/clock controlados y ventana cero; la política de timing productiva
    (default 2 s) está separada del contrato del algoritmo.
    """
    if quiet_window_seconds < 0:
        raise FrozenRuntimeError("quiet_window_seconds no puede ser negativo")
    resultado, _post = _run_window(source, quiet_window_seconds=quiet_window_seconds, sleep=sleep)
    return resultado


def obtain_stable_source_snapshot(
    source: ManagedSource,
    *,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> StableSourceObservation:
    """Veredicto de estabilidad + evidencia de la MISMA ventana causal.

    Si el veredicto es STABLE, el snapshot se construye desde la medición POST
    de esa misma ventana (sin re-inventariar): evidencia y veredicto no pueden
    desincronizarse, acotando el TOCTOU "STABLE → snapshot". Si no es STABLE,
    ``snapshot=None`` y el estado/motivo queda explícito para decidir esperar
    y re-observar.
    """
    if quiet_window_seconds < 0:
        raise FrozenRuntimeError("quiet_window_seconds no puede ser negativo")
    resultado, post = _run_window(source, quiet_window_seconds=quiet_window_seconds, sleep=sleep)
    if resultado.state is not StabilityState.STABLE or post is None:
        return StableSourceObservation(source=source, stability=resultado, snapshot=None)
    return StableSourceObservation(
        source=source,
        stability=resultado,
        snapshot=snapshot_from_measurement(source, post),
    )
