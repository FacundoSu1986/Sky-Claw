"""Oráculo de Effective Runtime — P5-A (ADR 0012 §13/§29.4, Q12).

Responde UNA pregunta con evidencia: *¿qué ruta ejecutan realmente MO2/SKSE?*
El consumidor ya existía (``journal.reconciliar_journal`` acepta un
``observador_effective``); lo que faltaba era el productor. Este módulo lo es, y
lo hace sin consultar el bridge de MO2 —que no expone el game path— ni mutar
nada.

Procedimiento, en orden, con la regla de que **lo observado gana a lo
persistido** y de que la duda nunca se resuelve a favor del camino feliz::

    1. procesos vivos del juego      → la ruta está PROBADA por el SO
    2. tabla de procesos ilegible    → UNKNOWN (no se puede probar ausencia)
    3. MO2 vivo anterior al ini      → UNKNOWN (puede retener otro gamePath)
    4. gamePath persistido de MO2    → ruta CONFIGURADA (no probada)
    5. nada                          → UNKNOWN

    y luego, sobre la ruta efectiva:
    reparse → Managed Source → Generation → Desired ausente → no existe →
    != Desired → identidad → compatibilidad

Por qué el orden importa y no es arbitrario: los cuatro primeros rechazos son
**decidibles sin el Desired** (una junction no es un Clone, la Managed Source no
es un Clone, una Generation no es un Clone). Ponerlos después de "no hay Desired"
haría que el oráculo devolviera ``UNKNOWN`` ante un defecto que sí puede
demostrar, y ``UNKNOWN`` no distingue "no sé" de "está mal" — que es justo lo que
el propietario necesita saber.

**Sin autoridad durable del Desired Clone, no hay MATCH.** El store de
``RuntimeCloneRecord`` no existe (P4/P5 diferido), así que
``desired_runtime_from_storage`` devuelve ``clone_root=None`` y el veredicto es
``UNKNOWN``. Es un hallazgo de FASE D, no una carencia del oráculo: fabricar el
Desired desde un parámetro sería aceptar una ruta declarada por el llamador como
prueba, que es lo que el brief prohíbe.
"""

from __future__ import annotations

import os
import pathlib
import time
from collections.abc import Iterable, Mapping, Sequence

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.effective_runtime.compatibility import (
    assess_runtime_compatibility,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2GamePathEvidence,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    DesiredRuntime,
    EffectiveRuntimeAssessment,
    EffectiveRuntimeVerdict,
    EvidenceSource,
    LiveProcessKind,
    LiveProcessSnapshot,
    ObservationState,
    ReasonCode,
    RuntimeCompatibility,
    RuntimeProbes,
    SurfaceObservation,
)
from sky_claw.local.frozen_runtime.effective_runtime.processes import (
    probe_live_processes,
    raiz_de_la_imagen,
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import active_state_path
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)

#: Nombre del ini de instancia de MO2 (case-insensitive en Windows).
_NOMBRE_INI_MO2 = "modorganizer.ini"
#: Etiqueta de la observación de ``gamePath`` (para el reporte, no para decidir).
_ETIQUETA_GAME_PATH = "[General] gamePath"


def _normalizar(ruta: str | pathlib.Path) -> str:
    """Forma canónica para COMPARAR rutas, sin resolver reparse points.

    ``normcase``+``normpath``+``abspath`` normalizan caja, separadores y ``..``,
    pero **no** siguen un symlink/junction. Resolverlos acá borraría la
    evidencia que el escenario 6 necesita ver: dos rutas pueden normalizar al
    mismo string y seguir siendo objetos físicos distintos.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


def _es_igual_o_hijo(candidato: str | pathlib.Path, contenedor: str | pathlib.Path) -> bool:
    """``True`` si *candidato* es *contenedor* o cuelga de él."""
    hijo = _normalizar(candidato)
    padre = _normalizar(contenedor)
    if hijo == padre:
        return True
    return hijo.startswith(padre + os.sep)


def _primer_reparse(ruta: pathlib.Path) -> str | None:
    """Componente más alto de *ruta* que es symlink/junction, o ``None``.

    Recorre la ruta completa hasta la raíz de la unidad: una junction puede estar
    en cualquier nivel, y comprobar sólo el último componente deja pasar el caso
    ``<padre-junction>/Clone`` (el patrón que P3-T encontró en la materialización
    de directorios). Un componente inexistente corta el recorrido sin veredicto:
    la ausencia se reporta aparte como ``EFFECTIVE_PATH_MISSING``.
    """
    actual = pathlib.Path(ruta)
    while True:
        try:
            tipo = link_kind_or_raise(actual)
        except OSError:
            return None
        if tipo is not None:
            return f"'{actual}' es {tipo}"
        padre = actual.parent
        if padre == actual:
            return None
        actual = padre


def _observacion_de_ruta(
    source: EvidenceSource,
    *,
    ruta: str | None,
    state: ObservationState,
    label: str = "",
    detail: str = "",
) -> SurfaceObservation:
    return SurfaceObservation(
        source=source,
        state=state,
        raw=ruta,
        normalized=_normalizar(ruta) if ruta else None,
        label=label,
        detail=detail,
    )


def _raices_de_procesos_vivos(
    procesos: LiveProcessSnapshot,
) -> tuple[list[pathlib.Path], str]:
    """Raíces que los procesos vivos demuestran, y una nota de procedencia.

    Un ``SkyrimSE.exe`` vivo prueba su raíz. Si no hay ninguno pero sí un loader
    de SKSE vivo, el loader vive en la raíz del juego y la carga desde ahí: sirve
    como la misma prueba (se informa como ``LIVE_GAME_PROCESS`` porque es la
    evidencia del runtime del juego, con la procedencia anotada en ``detail``).
    """
    juegos = [p for p in procesos.processes if p.kind is LiveProcessKind.GAME]
    if juegos:
        return [raiz for raiz in (raiz_de_la_imagen(p.image_path) for p in juegos) if raiz], ""

    loaders = [p for p in procesos.processes if p.kind is LiveProcessKind.SKSE_LOADER]
    raices = [raiz for raiz in (raiz_de_la_imagen(p.image_path) for p in loaders) if raiz]
    if raices:
        return raices, "ruta probada por un loader de SKSE vivo (no por SkyrimSE.exe)"
    return [], ""


def _desired_ausente(desired: DesiredRuntime | None) -> bool:
    """Sin ``clone_root`` no hay igualdad demostrable (y ``clone_id`` no basta)."""
    return desired is None or desired.clone_root is None


def desired_runtime_from_storage(root: pathlib.Path) -> DesiredRuntime:
    """Desired leído de la autoridad durable disponible — hoy, sólo la Generation.

    ``active.json`` v1 persiste ``desired_active_generation`` y **nada más**. No
    existe store de ``RuntimeCloneRecord``, así que ``clone_root`` queda en
    ``None`` por construcción y el oráculo devolverá ``UNKNOWN``: ésa es la
    dependencia real que separa P4 de P5 (FASE D del brief), no un hueco de
    implementación de este slice.
    """
    carga = load_frozen_runtime_state(active_state_path(pathlib.Path(root)))
    if not carga.found or carga.state is None:
        return DesiredRuntime()
    return DesiredRuntime(generation_id=carga.state.desired_active_generation)


def default_runtime_probes() -> RuntimeProbes:
    """Observadores de producción: PE real, SKSE real, tabla de procesos real."""

    def identidad(raiz: pathlib.Path) -> RuntimeIdentity:
        return observe_runtime_identity_from_root(raiz, expected_game_key="skyrimse").runtime_identity

    return RuntimeProbes(
        identity=identidad,
        compatibility=lambda raiz: assess_runtime_compatibility(raiz),
        processes=probe_live_processes,
    )


def _resolver_efectiva(
    *,
    procesos: LiveProcessSnapshot,
    game_path: SurfaceObservation,
    ini_path: pathlib.Path | None,
) -> tuple[str | None, EvidenceSource | None, list[ReasonCode], list[str]]:
    """Determina la ruta efectiva. Devuelve ``(ruta, fuente, razones, notas)``."""
    razones: list[ReasonCode] = []
    notas: list[str] = []

    raices, procedencia = _raices_de_procesos_vivos(procesos)
    normalizadas = {_normalizar(raiz) for raiz in raices}
    if len(normalizadas) > 1:
        notas.append(f"dos raíces de juego vivas: {sorted(normalizadas)}")
        return None, None, [ReasonCode.AMBIGUOUS_LIVE_GAME], notas
    if len(normalizadas) == 1:
        razones.append(ReasonCode.LIVE_GAME_PRESENT)
        if procedencia:
            notas.append(procedencia)
        if game_path.normalized is not None and game_path.normalized != next(iter(normalizadas)):
            razones.append(ReasonCode.PERSISTED_DIVERGES_FROM_LIVE)
            notas.append(
                "el gamePath persistido difiere de la ruta del proceso vivo; "
                "manda el proceso (es lo que se está ejecutando)"
            )
        return next(iter(normalizadas)), EvidenceSource.LIVE_GAME_PROCESS, razones, notas

    if procesos.indeterminate:
        # No se puede probar AUSENCIA de un juego corriendo. Un ``gamePath``
        # persistido en esta situación no es "la ruta efectiva": es una
        # intención que podría estar contradicha por un proceso que no vemos.
        notas.append(procesos.detail or "tabla de procesos ilegible")
        return None, None, [ReasonCode.PROCESS_TABLE_UNREADABLE], notas

    vivos_mo2 = [p for p in procesos.processes if p.kind is LiveProcessKind.MO2]
    if vivos_mo2 and game_path.state is ObservationState.OBSERVED and ini_path is not None:
        try:
            mtime_ini = ini_path.stat().st_mtime_ns
        except OSError:
            mtime_ini = 0
        if mtime_ini and all(p.started_at_ns < mtime_ini for p in vivos_mo2):
            notas.append(
                "MO2 arrancó antes de la última escritura del ini: puede retener "
                "en memoria un gamePath distinto del persistido"
            )
            return None, None, [ReasonCode.MO2_IN_MEMORY_MAY_DIVERGE], notas

    if game_path.state is ObservationState.OBSERVED and game_path.normalized is not None:
        return game_path.normalized, EvidenceSource.MO2_GAME_PATH, razones, notas

    if game_path.state is ObservationState.UNREADABLE:
        notas.append(game_path.detail or "gamePath ilegible")
    return None, None, [ReasonCode.NO_EFFECTIVE_EVIDENCE], notas


def assess_effective_runtime(
    *,
    mo2_instance_dir: pathlib.Path | None,
    desired: DesiredRuntime | None,
    managed_source_root: pathlib.Path | None = None,
    generation_roots: Iterable[pathlib.Path] = (),
    config_skyrim_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    observed_at_ns: int | None = None,
) -> EffectiveRuntimeAssessment:
    """Evalúa si el runtime efectivo de MO2/SKSE es el Desired Clone.

    Args:
        mo2_instance_dir: directorio de la instancia MO2 (donde vive
            ``modorganizer.ini``). ``None`` = no hay instancia que observar.
        desired: la EXPECTATIVA (nunca la evidencia). Ver ``DesiredRuntime``.
        managed_source_root: raíz de la instalación administrada por Steam. Se
            usa **sólo** como referencia negativa.
        generation_roots: raíces de Generations; ejecutar desde una viola SFR-19.
        config_skyrim_path: ``local_cfg.skyrim_path`` persistido, si se conoce.
        environ: entorno a inspeccionar (por defecto ``os.environ``).
        probes: observadores inyectables (por defecto, los de producción).
        observed_at_ns: sello temporal; por defecto, ahora.

    Returns:
        Un ``EffectiveRuntimeAssessment``. ``authorizes_activation`` es la única
        puerta y exige ``MATCH`` **y** ``COMPATIBLE``.
    """
    observadores = probes if probes is not None else default_runtime_probes()
    entorno = environ if environ is not None else os.environ
    sello = observed_at_ns if observed_at_ns is not None else time.time_ns()

    observaciones: list[SurfaceObservation] = []

    # ── Superficies de Sky-Claw (lo que Sky-Claw cree, no lo que MO2 usa) ──
    if config_skyrim_path:
        observaciones.append(
            _observacion_de_ruta(
                EvidenceSource.CONFIG_SKYRIM_PATH, ruta=config_skyrim_path, state=ObservationState.OBSERVED
            )
        )
    else:
        observaciones.append(
            SurfaceObservation(source=EvidenceSource.CONFIG_SKYRIM_PATH, state=ObservationState.ABSENT)
        )

    ruta_del_entorno = (entorno.get("SKYRIM_PATH") or "").strip()
    if ruta_del_entorno:
        observaciones.append(
            _observacion_de_ruta(EvidenceSource.ENV_SKYRIM_PATH, ruta=ruta_del_entorno, state=ObservationState.OBSERVED)
        )
    else:
        observaciones.append(SurfaceObservation(source=EvidenceSource.ENV_SKYRIM_PATH, state=ObservationState.ABSENT))

    # ── Superficies de MO2 ──
    ini_path = pathlib.Path(mo2_instance_dir) / _NOMBRE_INI_MO2 if mo2_instance_dir else None
    if ini_path is None:
        # Sin instancia declarada no se inventa un ini: la ausencia es la evidencia.
        evidencia_game_path = Mo2GamePathEvidence(
            state=ObservationState.ABSENT,
            detail="no se declaró una instancia MO2 que observar",
        )
    else:
        evidencia_game_path = leer_game_path_de_mo2(ini_path)
    game_path_obs = _observacion_de_ruta(
        EvidenceSource.MO2_GAME_PATH,
        ruta=evidencia_game_path.path,
        state=evidencia_game_path.state,
        label=_ETIQUETA_GAME_PATH,
        detail=evidencia_game_path.detail,
    )
    observaciones.append(game_path_obs)

    if ini_path is not None:
        for ejecutable in leer_ejecutables_de_mo2(ini_path):
            observaciones.append(
                _observacion_de_ruta(
                    EvidenceSource.MO2_EXECUTABLE,
                    ruta=ejecutable.binary,
                    state=ObservationState.OBSERVED,
                    label=f"{ejecutable.index}\\binary ({ejecutable.title})",
                )
            )

    # ── Procesos vivos ──
    procesos = observadores.processes()
    for proceso in procesos.processes:
        fuente = (
            EvidenceSource.LIVE_GAME_PROCESS
            if proceso.kind in (LiveProcessKind.GAME, LiveProcessKind.SKSE_LOADER)
            else EvidenceSource.LIVE_MO2_PROCESS
        )
        observaciones.append(
            _observacion_de_ruta(
                fuente,
                ruta=proceso.image_path,
                state=ObservationState.OBSERVED,
                label=f"pid {proceso.pid} ({proceso.kind})",
            )
        )

    # ── Ruta efectiva ──
    efectiva, fuente_efectiva, razones, notas = _resolver_efectiva(
        procesos=procesos,
        game_path=game_path_obs,
        ini_path=ini_path,
    )

    identidad: RuntimeIdentity | None = None
    compatibilidad = RuntimeCompatibility.UNKNOWN
    motivo_compatibilidad = "no evaluada: el veredicto se resolvió antes de medir la identidad del runtime"

    if efectiva is None:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=razones,
            efectiva=None,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=notas,
        )

    ruta = pathlib.Path(efectiva)

    # 1. Reparse: no es un Clone aunque el string coincida con el Desired.
    reparse = _primer_reparse(ruta)
    if reparse is not None:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.MISMATCH,
            reasons=[*razones, ReasonCode.REPARSE_NOT_ALLOWED],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, reparse],
        )

    # 2. La Managed Source nunca es el runtime efectivo (SFR-05/SFR-19).
    if managed_source_root is not None and _es_igual_o_hijo(efectiva, managed_source_root):
        return _assessment(
            verdict=EffectiveRuntimeVerdict.MISMATCH,
            reasons=[*razones, ReasonCode.EFFECTIVE_IS_MANAGED_SOURCE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, "la ruta efectiva está dentro de la instalación administrada por Steam"],
        )

    # 3. Nunca se ejecuta desde una Generation (SFR-19).
    for raiz_generacion in generation_roots:
        if _es_igual_o_hijo(efectiva, raiz_generacion):
            return _assessment(
                verdict=EffectiveRuntimeVerdict.MISMATCH,
                reasons=[*razones, ReasonCode.EFFECTIVE_IS_GENERATION],
                efectiva=efectiva,
                fuente=fuente_efectiva,
                desired=desired,
                observaciones=observaciones,
                identidad=None,
                compatibilidad=compatibilidad,
                motivo_compatibilidad=motivo_compatibilidad,
                sello=sello,
                notas=[*notas, f"la ruta efectiva cuelga de la Generation '{raiz_generacion}'"],
            )

    # 4. Las superficies de Sky-Claw deben apuntar al mismo árbol que el runtime
    #    efectivo. Si no, las tools de Sky-Claw escriben en el runtime saliente
    #    (§29.4: "un binder que repunte una y no las otras deja escrituras
    #    aterrizando en el runtime saliente"). Es decidible sin el Desired.
    divergentes = [
        obs
        for obs in observaciones
        if obs.source in (EvidenceSource.CONFIG_SKYRIM_PATH, EvidenceSource.ENV_SKYRIM_PATH)
        and obs.state is ObservationState.OBSERVED
        and obs.normalized is not None
        and obs.normalized != _normalizar(efectiva)
    ]
    if divergentes:
        detalle = "; ".join(f"{obs.source}='{obs.raw}'" for obs in divergentes)
        return _assessment(
            verdict=EffectiveRuntimeVerdict.MISMATCH,
            reasons=[*razones, ReasonCode.CONFIG_DIVERGES_FROM_EFFECTIVE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, f"las tools de Sky-Claw apuntan a otro árbol que el runtime efectivo: {detalle}"],
        )

    # 5. Sin Desired demostrable no hay MATCH posible.
    if _desired_ausente(desired):
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.NO_DESIRED_CLONE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, "no hay autoridad durable del Desired Clone (falta RuntimeCloneRecord)"],
        )

    assert desired is not None and desired.clone_root is not None  # narrowing
    raiz_deseada = pathlib.Path(desired.clone_root)

    # 6. Una ruta configurada que no existe no es un runtime.
    if not ruta.is_dir():
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.EFFECTIVE_PATH_MISSING],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, f"la ruta efectiva '{ruta}' no existe o no es un directorio"],
        )

    # 7. Igualdad con el Desired (comparación normalizada, no textual).
    if _normalizar(efectiva) != _normalizar(raiz_deseada):
        return _assessment(
            verdict=EffectiveRuntimeVerdict.MISMATCH,
            reasons=[*razones, ReasonCode.EFFECTIVE_IS_NOT_DESIRED_CLONE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, f"la ruta efectiva es '{ruta}' y el Desired Clone es '{raiz_deseada}'"],
        )

    # 8. Identidad física y compatibilidad real del build.
    try:
        identidad = observadores.identity(ruta)
    except RuntimeObservationError as exc:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.IDENTITY_UNREADABLE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, f"identidad no legible: {exc}"],
        )

    if not identidad.game_version:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.IDENTITY_UNREADABLE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=identidad,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, "la identidad observada no declara versión del juego"],
        )

    compat = observadores.compatibility(ruta)
    compatibilidad = compat.state
    motivo_compatibilidad = compat.reason

    if compat.state is RuntimeCompatibility.INCOMPATIBLE:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.MISMATCH,
            reasons=[*razones, ReasonCode.SKSE_INCOMPATIBLE],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=identidad,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, compat.reason],
        )

    if compat.state is not RuntimeCompatibility.COMPATIBLE:
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.COMPATIBILITY_UNKNOWN],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=identidad,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, compat.reason],
        )

    return _assessment(
        verdict=EffectiveRuntimeVerdict.MATCH,
        reasons=[*razones, ReasonCode.EFFECTIVE_MATCHES_DESIRED_CLONE],
        efectiva=efectiva,
        fuente=fuente_efectiva,
        desired=desired,
        observaciones=observaciones,
        identidad=identidad,
        compatibilidad=compatibilidad,
        motivo_compatibilidad=motivo_compatibilidad,
        sello=sello,
        notas=notas,
    )


def _assessment(
    *,
    verdict: EffectiveRuntimeVerdict,
    reasons: Sequence[ReasonCode],
    efectiva: str | None,
    fuente: EvidenceSource | None,
    desired: DesiredRuntime | None,
    observaciones: Sequence[SurfaceObservation],
    identidad: RuntimeIdentity | None,
    compatibilidad: RuntimeCompatibility,
    motivo_compatibilidad: str,
    sello: int,
    notas: Sequence[str],
) -> EffectiveRuntimeAssessment:
    """Ensambla el assessment. Las notas se anexan al motivo de compatibilidad.

    ``reasons`` nunca queda vacío (lo exige el modelo) y ``notas`` viaja dentro
    de ``compatibility_reason`` para no inventar un campo que el consumidor de
    P4 no conoce. Es una decisión de alcance: el journal consume
    ``ObservacionEffective``, no este dataclass.
    """
    motivos = list(reasons) or [ReasonCode.NO_EFFECTIVE_EVIDENCE]
    motivo = motivo_compatibilidad
    if notas:
        motivo = f"{motivo} | {' | '.join(notas)}"
    return EffectiveRuntimeAssessment(
        verdict=verdict,
        reasons=tuple(motivos),
        effective_path=efectiva,
        effective_source=fuente,
        desired_clone_id=desired.clone_id if desired is not None else None,
        desired_clone_root=str(desired.clone_root) if desired is not None and desired.clone_root else None,
        observations=tuple(observaciones),
        runtime_identity=identidad,
        compatibility=compatibilidad,
        compatibility_reason=motivo,
        observed_at_ns=sello,
    )
