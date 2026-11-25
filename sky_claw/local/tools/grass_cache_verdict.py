"""PolÃ­tica ÃšNICA del veredicto del precache de grass (P1).

Contesta **una sola pregunta**: *Â¿quÃ© se puede afirmar del cachÃ© que quedÃ³?* y la separa de
*Â¿cÃ³mo terminÃ³ el ritual?*. Son dos ejes y no se confunden (P1, F-02):

- ``outcome`` (``GrassCacheOutcome``) dice **cÃ³mo terminÃ³** el ritual;
- ``verdict`` dice **cuÃ¡nto se puede certificar** del cachÃ©, y ``certificado`` es su booleano.

Reglas que esta pieza implementa, y por quÃ©:

1. **La ausencia del flag NO certifica.** ``End()`` de NGIO borra el archivo de progreso al
   terminar, pero tambiÃ©n puede desaparecer por otra causa (un barrido, un tercero). El veredicto
   exige, ademÃ¡s, **trabajo de ESTA corrida demostrado** (``trabajo_de_la_corrida``).
2. **Un cachÃ© preexistente no es salida nueva.** Si el inventario final es el mismo conjunto que el
   del arranque (``archivos_heredados`` cubre todo), no hay Ã©xito operacional: es
   ``completed_unverified`` con alcance ``"ninguno"``.
3. **``.fail`` es un contador de intentos por celda** (``GidFileCellGenerateTask::Process`` de
   NGIO 1.0.13): se escribe antes de generar y se borra cuando la celda termina. Un ``.fail`` que
   sobrevive al ritual es una celda que no completÃ³. Se atribuye al intento actual comparando
   **contenido** contra el baseline, nunca por fecha.
4. **La ausencia de fallos conocidos no es completitud global.** ``completitud_global`` es siempre
   ``"no_verificada"`` en P1: probarla exige atestaciÃ³n del load order (P2).
5. **Dos fases.** ``limpieza_limpia=None`` significa Â«todavÃ­a no se sabeÂ» y produce un veredicto
   **provisional** que el runner no publica: lo recalcula despuÃ©s de neutralizar el flag.

El mÃ³dulo es **puro**: sin I/O, sin asyncio, sin psutil. Toda la evidencia entra como datos.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

#: CÃ³mo terminÃ³ el ritual. Vive acÃ¡ (y el runner lo re-exporta) para que la polÃ­tica no dependa
#: del supervisor. Los diez cÃ³digos son los del contrato vigente: no se agrega ni se quita ninguno.
GrassCacheOutcome = Literal[
    "completed",
    "stalled",
    "disk_full",
    "timeout",
    "cancelled",
    "max_restarts",
    "spawn_failed",
    "flag_sin_progreso",
    "flag_alternativo_presente",
    "cache_cambio_despues_del_plan",
]

#: CuÃ¡nto se puede certificar del cachÃ© que quedÃ³.
GrassCacheVerdict = Literal[
    "completed_verified",  # terminÃ³ por el camino de End() y el cachÃ© se puede afirmar
    "completed_unverified",  # terminÃ³, pero el cachÃ© NO se puede certificar
    "partial",  # no terminÃ³ y hay cachÃ© conservado (reanudable)
    "failed",  # no terminÃ³ y no hay cachÃ©, o el contrato P0 lo cortÃ³
    "cancelled",
    "unknown",  # ni terminaciÃ³n ni estado del cachÃ© se pueden afirmar
]

#: QuÃ© parte del cachÃ© cubre la certificaciÃ³n (D2 del propietario, RD-12).
AlcanceCertificado = Literal["corrida_completa", "trabajo_de_la_corrida", "ninguno"]

#: Tri-estado de la terminalidad del proceso del juego.
Terminalidad = Literal["terminado", "vivo", "indeterminado"]

TrabajoDeLaCorrida = Literal["demostrado", "no_demostrado"]
IntegridadObservable = Literal["sin_fallos_conocidos", "con_fallos_conocidos", "no_evaluada"]

#: Vocabulario CERRADO de razones. Una razÃ³n nueva rompe `test_vocabulario_de_razones_congelado`
#: hasta que se declare acÃ¡ y en el test (convenciÃ³n del repo para no dejar prosa suelta).
CODIGOS_DE_RAZON: Final[tuple[str, ...]] = (
    "cache_ilegible",
    "terminalidad_no_verificada",
    "cache_vacio",
    "limpieza_incompleta",
    "archivos_heredados_sin_procedencia",
    "sin_evidencia_de_esta_corrida",
    "fallos_de_celda_atribuibles",
    "fallos_de_celda_historicos",
    "worldspaces_sin_artefactos",
    "outcome_no_completado",
    "sin_certificacion",
    "cancelado",
)

_TERMINACIONES_PARCIALES: Final[frozenset[str]] = frozenset({"stalled", "timeout", "max_restarts", "disk_full"})
_CORTES_DE_CONTRATO: Final[frozenset[str]] = frozenset(
    {"spawn_failed", "flag_sin_progreso", "flag_alternativo_presente", "cache_cambio_despues_del_plan"}
)


@dataclass(frozen=True, slots=True)
class CacheObservado:
    """Lo mÃ­nimo que la polÃ­tica necesita del cachÃ©, sin depender del inventario de P0."""

    cgid_count: int = 0
    cgid_bytes: int = 0
    dgid_count: int = 0
    fail_count: int = 0
    archivos: int = 0

    @property
    def vacio(self) -> bool:
        """Sin ``.cgid`` o con todos de 0 bytes: el fallo silencioso por zero-bounds."""
        return self.cgid_count <= 0 or self.cgid_bytes <= 0

    def a_dict(self) -> dict[str, int]:
        return {
            "cgid_count": self.cgid_count,
            "cgid_bytes": self.cgid_bytes,
            "dgid_count": self.dgid_count,
            "fail_count": self.fail_count,
            "archivos": self.archivos,
        }


@dataclass(frozen=True, slots=True)
class EvidenciaVeredicto:
    """Todo lo que el runner observÃ³, en datos. La polÃ­tica no hace I/O.

    Attributes:
        outcome: cÃ³mo terminÃ³ el ritual.
        modo: ``new``/``resume``/``rebuild`` â€” decide quÃ© se puede atribuir.
        terminalidad: si se pudo demostrar que el proceso del juego terminÃ³.
        limpieza_limpia: ``None`` = todavÃ­a no se sabe (fase 1, veredicto provisional).
        inventario_final: foto del cachÃ© al salir; ``None`` = ilegible.
        archivos_heredados: archivos del inventario final que ya estaban en el baseline con el
            MISMO nombre y tamaÃ±o. Es la medida de Â«esto no lo produjo esta corridaÂ».
        archivos_nuevos: archivos del final que NO estaban en el baseline.
        fallos_finales / fallos_previos: nombre â†’ contenido crudo de cada ``.fail``.
        worldspaces_solicitados: los que el payload pidiÃ³ precachear.
        worldspaces_con_artefactos: prefijo de worldspace de cada ``.cgid``/``.dgid`` final.
        worldspaces_declarados_por_ngio: lÃ­neas ``ws_`` observadas en el flag (evidencia, no
            requisito: el archivo bufferiza y desaparece al terminar).
        progreso_observado: el watchdog vio avanzar algÃºn indicador durante la corrida.
        evidencia_ilegible: no se pudo leer el cachÃ© al salir.
    """

    outcome: GrassCacheOutcome
    modo: str
    terminalidad: Terminalidad
    limpieza_limpia: bool | None
    inventario_final: CacheObservado | None = None
    archivos_heredados: int = 0
    archivos_nuevos: int = 0
    fallos_finales: Mapping[str, bytes] = field(default_factory=dict)
    fallos_previos: Mapping[str, bytes] = field(default_factory=dict)
    worldspaces_solicitados: tuple[str, ...] = ()
    worldspaces_con_artefactos: tuple[str, ...] = ()
    worldspaces_declarados_por_ngio: tuple[str, ...] = ()
    progreso_observado: bool = False
    evidencia_ilegible: bool = False


@dataclass(frozen=True, slots=True)
class VeredictoCache:
    """Resultado de la polÃ­tica: veredicto, certificaciÃ³n y los tres ejes de alcance (D2)."""

    verdict: GrassCacheVerdict
    certificado: bool
    exito_operacional: bool
    razones: tuple[str, ...]
    alcance_certificado: AlcanceCertificado
    trabajo_de_la_corrida: TrabajoDeLaCorrida
    integridad_observable: IntegridadObservable
    completitud_global: Literal["no_verificada"] = "no_verificada"

    def a_dict(self) -> dict[str, Any]:
        """Resumen serializable para resultado, journal y eventos."""
        return {
            "verdict": self.verdict,
            "certificado": self.certificado,
            "exito_operacional": self.exito_operacional,
            "verdict_razones": list(self.razones),
            "alcance_certificado": self.alcance_certificado,
            "trabajo_de_la_corrida": self.trabajo_de_la_corrida,
            "integridad_observable": self.integridad_observable,
            "completitud_global": self.completitud_global,
        }


def _fallos_atribuibles(ev: EvidenciaVeredicto) -> tuple[int, int, int]:
    """(atribuibles, historicos, borrados) comparando CONTENIDO contra el baseline.

    Un ``.fail`` cuyo contador subiÃ³ (o que no estaba) lo escribiÃ³ esta corrida. Uno que sigue con
    el mismo contenido no se le puede atribuir â€”NGIO saltea la celda sin reescribir el archivo
    cuando ``fails >= MaxFailures``â€” pero igual deja constancia de que esa celda nunca completÃ³.
    """
    atribuibles = 0
    historicos = 0
    for nombre, contenido in ev.fallos_finales.items():
        previo = ev.fallos_previos.get(nombre)
        if previo is None or previo != contenido:
            atribuibles += 1
        else:
            historicos += 1
    borrados = sum(1 for nombre in ev.fallos_previos if nombre not in ev.fallos_finales)
    return atribuibles, historicos, borrados


def _trabajo_demostrado(ev: EvidenciaVeredicto, *, fallos_borrados: int) -> bool:
    """Â¿El cachÃ© presente debe algo a ESTA corrida? Sin esto no hay Ã©xito ni certificaciÃ³n."""
    if ev.modo == "new":
        # P0 garantiza que NEW arranca con el cachÃ© vacÃ­o (y lo revalida antes del spawn): si el
        # final no tiene heredados, TODO lo que hay lo escribiÃ³ esta corrida.
        return ev.archivos_heredados == 0
    if ev.modo == "rebuild":
        # REBUILD escribe el flag vacÃ­o: NGIO borra el cachÃ© viejo en Init(). Si sobreviviÃ³ algo del
        # baseline, la premisa no se cumpliÃ³ y no se puede atribuir el resto.
        return ev.archivos_heredados == 0 and (ev.inventario_final is not None and ev.inventario_final.archivos > 0)
    # resume: el cachÃ© anterior es legÃ­timo. Se exige ALGUNA evidencia de avance de esta corrida;
    # no se exige crecimiento arbitrario (un resume casi completo puede no agregar archivos).
    return ev.progreso_observado or ev.archivos_nuevos > 0 or fallos_borrados > 0


def _worldspaces_faltantes(ev: EvidenciaVeredicto) -> tuple[str, ...]:
    if not ev.worldspaces_solicitados:
        return ()
    presentes = {ws.casefold() for ws in ev.worldspaces_con_artefactos}
    return tuple(ws for ws in ev.worldspaces_solicitados if ws.casefold() not in presentes)


def evaluar_veredicto(ev: EvidenciaVeredicto) -> VeredictoCache:
    """Aplica la polÃ­tica. Pura y determinista: misma evidencia, mismo veredicto."""
    atribuibles, historicos, borrados = _fallos_atribuibles(ev)
    final = ev.inventario_final

    def _veredicto(
        verdict: GrassCacheVerdict,
        razones: tuple[str, ...],
        *,
        exito: bool = False,
        certificado: bool = False,
        alcance: AlcanceCertificado = "ninguno",
        trabajo: TrabajoDeLaCorrida = "no_demostrado",
        integridad: IntegridadObservable = "no_evaluada",
    ) -> VeredictoCache:
        return VeredictoCache(
            verdict=verdict,
            certificado=certificado,
            exito_operacional=exito,
            razones=razones,
            alcance_certificado=alcance,
            trabajo_de_la_corrida=trabajo,
            integridad_observable=integridad,
        )

    # 1. Sin evidencia legible no se afirma nada (fail-closed).
    if ev.evidencia_ilegible or final is None:
        return _veredicto("unknown", ("cache_ilegible",))

    # 2. Si no se puede probar que el proceso terminÃ³, tampoco se puede certificar el cachÃ©: pudo
    #    seguir escribiÃ©ndose despuÃ©s de la Ãºltima lectura.
    if ev.terminalidad != "terminado":
        return _veredicto("unknown", ("terminalidad_no_verificada",))

    integridad: IntegridadObservable = "con_fallos_conocidos" if (atribuibles or historicos) else "sin_fallos_conocidos"
    trabajo: TrabajoDeLaCorrida = "demostrado" if _trabajo_demostrado(ev, fallos_borrados=borrados) else "no_demostrado"

    if ev.outcome == "completed":
        if final.vacio:
            # TerminÃ³ y no hay cachÃ©: el fallo silencioso por zero-bounds (D5 de P0).
            return _veredicto(
                "failed",
                ("cache_vacio",),
                integridad=integridad,
                trabajo=trabajo,
            )
        razones: list[str] = []
        exito = trabajo == "demostrado"
        if not exito:
            # Un cachÃ© preexistente SIN evidencia de generaciÃ³n actual no puede producir un falso
            # Ã©xito (D1 del propietario). En `resume` los heredados son legÃ­timos: lo que falta es
            # evidencia de avance, no procedencia.
            if ev.modo == "resume":
                razones.append("sin_evidencia_de_esta_corrida")
            else:
                razones.append(
                    "archivos_heredados_sin_procedencia"
                    if ev.archivos_heredados > 0
                    else "sin_evidencia_de_esta_corrida"
                )
        if ev.limpieza_limpia is False:
            exito = False
            razones.append("limpieza_incompleta")
        if atribuibles:
            razones.append("fallos_de_celda_atribuibles")
        if historicos:
            razones.append("fallos_de_celda_historicos")
        faltantes = _worldspaces_faltantes(ev)
        if faltantes:
            razones.append("worldspaces_sin_artefactos")

        certificado = exito and not atribuibles and not historicos and not faltantes
        if certificado:
            alcance: AlcanceCertificado = "trabajo_de_la_corrida" if ev.modo == "resume" else "corrida_completa"
            return _veredicto(
                "completed_verified",
                (),
                exito=True,
                certificado=True,
                alcance=alcance,
                trabajo=trabajo,
                integridad=integridad,
            )
        if not razones:  # defensa: certificado False siempre deja al menos una razÃ³n
            razones.append("sin_certificacion")
        return _veredicto(
            "completed_unverified",
            tuple(razones),
            exito=exito,
            alcance="ninguno",
            trabajo=trabajo,
            integridad=integridad,
        )

    if ev.outcome == "cancelled":
        return _veredicto(
            "cancelled",
            ("cancelado",),
            integridad=integridad,
            trabajo=trabajo,
        )

    if ev.outcome in _CORTES_DE_CONTRATO:
        # El contrato P0 no autorizÃ³ el arranque (o lo cortÃ³ antes del spawn): no hay producto.
        return _veredicto(
            "failed",
            ("outcome_no_completado",),
            integridad=integridad,
            trabajo=trabajo,
        )

    # stalled / timeout / max_restarts / disk_full: hay cachÃ© parcial y es reanudable.
    if ev.outcome in _TERMINACIONES_PARCIALES:
        if final.archivos > 0:
            return _veredicto(
                "partial",
                ("sin_certificacion",),
                integridad=integridad,
                trabajo=trabajo,
            )
        return _veredicto("failed", ("cache_vacio",), integridad=integridad, trabajo=trabajo)

    # Cualquier outcome no enumerado (no deberÃ­a existir) cae en el lado seguro.
    return _veredicto("unknown", ("sin_certificacion",), integridad=integridad, trabajo=trabajo)


__all__ = [
    "CODIGOS_DE_RAZON",
    "AlcanceCertificado",
    "CacheObservado",
    "EvidenciaVeredicto",
    "GrassCacheOutcome",
    "GrassCacheVerdict",
    "IntegridadObservable",
    "Terminalidad",
    "TrabajoDeLaCorrida",
    "VeredictoCache",
    "evaluar_veredicto",
]
