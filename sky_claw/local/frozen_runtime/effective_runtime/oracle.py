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
    reparse (tri-estado: hay / no hay / no se pudo determinar) → Managed Source
    → Generation → lanzador del juego fuera de la raíz (selección no probada) →
    divergencia de superficies de Sky-Claw → Desired ausente → no existe →
    != Desired → identidad → compatibilidad

    y sobre la resolución misma:
    con un juego vivo observado pero tabla PARCIALMENTE ilegible, tampoco hay
    ruta afirmable (un pid denegado podría ser otro juego desde otra raíz).

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
    Mo2Executable,
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
    _NOMBRES_DE_JUEGO,
    _NOMBRES_DE_LOADER,
    probe_live_processes,
    raiz_de_la_imagen,
)
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
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


#: Resultado de ``_primer_reparse``: clase de desenlace + detalle.
#:
#: - ``"reparse"`` — hay symlink/junction: rechazo decidible.
#: - ``"none"`` — recorrido completo sin encontrar reparse.
#: - ``"indeterminado"`` — un componente existente dio ``OSError``: NO se
#:   colapsa en ``"none"``. Presentarlo como "inspección exitosa sin reparse"
#:   cuando la inspección falló es exactamente el defecto que la auditoría
#:   adversarial B4 demostró (MATCH fabricado sobre una duda).
_REPARSE_NONE = "none"
_REPARSE_HAY = "reparse"
_REPARSE_INDETERMINADO = "indeterminado"


def _primer_reparse(ruta: pathlib.Path) -> tuple[str, str | None]:
    """Clase de reparse del componente más alto de *ruta*, en tres estados.

    Recorre la ruta completa hasta la raíz de la unidad: una junction puede estar
    en cualquier nivel, y comprobar sólo el último componente deja pasar el caso
    ``<padre-junction>/Clone`` (el patrón que P3-T encontró en la materialización
    de directorios). Un componente inexistente corta el recorrido sin veredicto
    (``"none"``): la ausencia se reporta aparte como ``EFFECTIVE_PATH_MISSING``.
    Un componente EXISTENTE que da ``OSError`` es ``"indeterminado"``: no se pudo
    determinar, y el veredicto será UNKNOWN, nunca una inspección "exitosa".
    """
    actual = pathlib.Path(ruta)
    while True:
        try:
            tipo = link_kind_or_raise(actual)
        except FileNotFoundError:
            # Oracle #4 de la auditoría: hoy ``link_kind_or_raise`` ya trata
            # FileNotFoundError como "la ruta no existe" (no propaga), pero el
            # contrato de ESTA función no depende de ese detalle: un componente
            # inexistente corta el recorrido como "no hay reparse" y la
            # ausencia la reporta EFFECTIVE_PATH_MISSING. Si mañana
            # ``links.py`` cambiara a propagarla, una ruta simplemente
            # inexistente NO debe degradar a "inspección fallida".
            return _REPARSE_NONE, None
        except OSError as exc:
            return _REPARSE_INDETERMINADO, f"no se pudo inspeccionar '{actual}' como reparse point: {exc}"
        if tipo is not None:
            return _REPARSE_HAY, f"'{actual}' es {tipo}"
        padre = actual.parent
        if padre == actual:
            return _REPARSE_NONE, None
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
    """Desired leído de la autoridad durable: active.json y RuntimeCloneRecord.

    ``active.json`` v1 persiste sólo ``desired_active_generation`` (sin Clone
    pretendido), por lo que devuelve ``clone_root=None`` (fail-closed).
    ``active.json`` v2 persiste el par ``desired_generation_id`` y ``desired_clone_id``;
    si el clone_id existe, está PROVISIONED, su linaje coincide y su ruta es
    admisible, resuelve auténticamente el Desired Runtime.
    """
    carga = load_frozen_runtime_state(active_state_path(pathlib.Path(root)))
    if not carga.found or carga.state is None:
        return DesiredRuntime()

    gen_id = carga.state.desired_active_generation
    clone_id = carga.state.desired_clone_id
    if clone_id is None or gen_id is None:
        return DesiredRuntime(generation_id=gen_id)

    from sky_claw.local.frozen_runtime.clones import (
        CANONICAL_ADMITTED_ROLE,
        RuntimeCloneLifecycle,
        admitir_clone_root_path,
        cargar_runtime_clone_record,
        verificar_runtime_clone,
    )
    from sky_claw.local.frozen_runtime.generations import leer_generation_metadata
    from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState

    record = cargar_runtime_clone_record(pathlib.Path(root), clone_id)
    if record is None:
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    if record.source_generation_id != gen_id:
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    # Validar que la Generation exista de verdad en el storage
    try:
        leer_generation_metadata(pathlib.Path(root), record.source_generation_id)
    except (FrozenRuntimeError, OSError, ValueError, KeyError):
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    # Validar físicamente el clone contra la Generation registrada
    verif = verificar_runtime_clone(pathlib.Path(root), clone_id)
    if verif.state is not GenerationVerificationState.VALID:
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    try:
        clone_root = admitir_clone_root_path(record.root_path, storage_root=pathlib.Path(root))
    except (FrozenRuntimeError, OSError, ValueError, KeyError):
        return DesiredRuntime(clone_id=clone_id, generation_id=gen_id)

    return DesiredRuntime(clone_id=clone_id, clone_root=clone_root, generation_id=gen_id)


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
        if procesos.indeterminate:
            # Auditoría adversarial B1: la tabla es parcialmente ilegible. Hay
            # UNA raíz probada, pero un pid denegado podría ser otro juego
            # desde otra raíz — y una raíz que no se puede descartar impide
            # afirmar "el runtime efectivo es ÉSTA".
            notas.append(
                procesos.detail or "tabla de procesos parcialmente ilegible: no se puede descartar otra raíz de juego"
            )
            return (
                None,
                None,
                [ReasonCode.LIVE_GAME_PRESENT, ReasonCode.OTHER_ROOT_NOT_RULED_OUT],
                notas,
            )
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
        if mtime_ini and any(p.started_at_ns < mtime_ini for p in vivos_mo2):
            # Auditoría adversarial B2: ``all(...)`` sólo disparaba UNKNOWN si
            # TODAS las instancias eran anteriores al ini. Con una vieja y una
            # nueva, la mezcla pasaba y el ini persistido se daba por vigente
            # aunque la instancia vieja pudiera retener otro gamePath. Tampoco
            # se identifica de forma fiable qué instancia posee ESTE ini: si
            # ALGUNA es anterior, la evidencia persistida no se puede afirmar.
            notas.append(
                "hay una instancia MO2 viva anterior a la última escritura del ini: puede retener "
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
        # ``ABSENT`` sin más no distingue "medí y no hay" de "nunca pregunté".
        # El detalle es lo único que el consumidor puede leer para saberlo, y
        # el riesgo es concreto: un llamador que omita el parámetro apaga
        # ``CONFIG_DIVERGES_FROM_EFFECTIVE`` sin que nada lo delate.
        observaciones.append(
            SurfaceObservation(
                source=EvidenceSource.CONFIG_SKYRIM_PATH,
                state=ObservationState.ABSENT,
                detail=(
                    "no se suministró config_skyrim_path: la divergencia entre las tools de "
                    "Sky-Claw y el runtime efectivo NO se pudo verificar"
                ),
            )
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

    ejecutables_mo2 = leer_ejecutables_de_mo2(ini_path) if ini_path is not None else []
    for ejecutable in ejecutables_mo2:
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
    clase_reparse, detalle_reparse = _primer_reparse(ruta)
    if clase_reparse == _REPARSE_HAY:
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
            notas=[*notas, detalle_reparse or "reparse point"],
        )
    if clase_reparse == _REPARSE_INDETERMINADO:
        # Auditoría adversarial B4: no se pudo DETERMINAR si hay reparse.
        # Presentarlo como "no hay" fabricaría una inspección exitosa.
        return _assessment(
            verdict=EffectiveRuntimeVerdict.UNKNOWN,
            reasons=[*razones, ReasonCode.REPARSE_INSPECTION_FAILED],
            efectiva=efectiva,
            fuente=fuente_efectiva,
            desired=desired,
            observaciones=observaciones,
            identidad=None,
            compatibilidad=compatibilidad,
            motivo_compatibilidad=motivo_compatibilidad,
            sello=sello,
            notas=[*notas, detalle_reparse or "inspección de reparse indeterminada"],
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

    # 3b. La SELECCIÓN del ejecutable no está probada (auditoría adversarial
    #     B3): con la ruta efectiva resuelta de la configuración, un lanzador
    #     del juego registrado fuera de esa raíz significa que MO2 puede
    #     arrancar desde otro árbol. No es MISMATCH —no se sabe qué elegirá—
    #     pero el MATCH presupone una selección sin prueba. Sólo aplica a la
    #     evidencia configurada: un proceso vivo ya probó qué se está
    #     ejecutando, y las herramientas externas (LOOT/xEdit) no cuentan.
    if fuente_efectiva is EvidenceSource.MO2_GAME_PATH:
        lanzadores = _lanzadores_del_juego_fuera_de(efectiva, ejecutables_mo2)
        if lanzadores:
            return _assessment(
                verdict=EffectiveRuntimeVerdict.UNKNOWN,
                reasons=[*razones, ReasonCode.EXECUTABLE_SELECTION_UNPROVEN],
                efectiva=efectiva,
                fuente=fuente_efectiva,
                desired=desired,
                observaciones=observaciones,
                identidad=None,
                compatibilidad=compatibilidad,
                motivo_compatibilidad=motivo_compatibilidad,
                sello=sello,
                notas=[
                    *notas,
                    "hay un lanzador del juego registrado fuera de la raíz efectiva "
                    "y la selección no está probada: " + "; ".join(lanzadores),
                ],
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


#: Basenames que LANZAN el juego (con SKSE) — no herramientas externas. Si uno
#: está registrado en ``[customExecutables]`` fuera de la raíz efectiva, MO2
#: puede arrancar el juego desde otro árbol y ninguna evidencia prueba cuál se
#: seleccionará. LOOT, xEdit, DynDOLOD, etc. NO están acá: son herramientas
#: legítimas y no prueban nada sobre el runtime del juego.
#:
#: Es la UNIÓN de los sets del probe de procesos (Oracle #2 de la auditoría):
#: una sola fuente de verdad. Duplicar la lista fue exactamente el hueco que
#: el Oracle detectó — un loader que el probe clasifica pero el chequeo de
#: selección ignoraba (o viceversa) deja una de las dos audiciones coja.
_NOMBRES_LANZADOR = _NOMBRES_DE_JUEGO | _NOMBRES_DE_LOADER


def _lanzadores_del_juego_fuera_de(
    efectiva: str,
    ejecutables: Sequence[Mo2Executable],
) -> list[str]:
    """Lanzadores registrados cuya CARPETA no es la raíz efectiva.

    La comparación es de carpeta, no de "cuelga de": ``_es_igual_o_hijo``
    aceptaba cualquier descendiente, así que un ``skse64_loader.exe``
    registrado en ``<efectiva>/tools`` contaba como "mismo árbol" y habilitaba
    MATCH. Pero SKSE resuelve el juego relativo a SU directorio: si MO2 lanza
    ese loader, el runtime ejecutado es ``tools``, no la raíz. "Dentro de" no
    es "en la raíz".

    Un binario SIN componente de directorio (``skse64_loader.exe`` a secas) es
    la forma en que MO2 registra un ejecutable del propio juego, y significa
    "en la raíz": normalizarlo con ``abspath`` lo resolvería contra el CWD del
    proceso —que no es la instancia— y toda instancia normal caería en UNKNOWN
    por un artefacto del fixture. Un binario relativo CON directorio no se
    puede resolver sin la instancia, así que no prueba nada y cuenta como fuera.
    """
    fuera: list[str] = []
    for ejecutable in ejecutables:
        # ntpath.basename separa por / y \\ a la vez: MO2 escribe el binario
        # con barras hacia adelante y acá no se transforma la cadena (el ancla
        # AST del paquete prohíbe cualquier .replace() que parezca mutación).
        nombre = os.path.basename(ejecutable.binary).lower()
        if nombre not in _NOMBRES_LANZADOR:
            continue
        carpeta = os.path.dirname(ejecutable.binary)
        if not carpeta:
            continue
        if _normalizar(carpeta) != _normalizar(efectiva):
            fuera.append(f"{ejecutable.title} -> {ejecutable.binary}")
    return fuera


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
