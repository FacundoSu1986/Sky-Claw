"""REVIEW_RATE simulada y candidatos derivados del benchmark (NP-X0b, ADR 0014 §6). PURO: sin numpy.

Dado un conjunto de ``Medicion`` (las métricas del gate de una textura más su error de altura contra el ground truth),
este módulo responde dos preguntas que el gate no puede responder solo:

- **¿Qué límites saldrían de este corpus?** ``derivar_limites`` toma un cuantil de cada métrica gateada sobre las
  mediciones BUENAS. Es una derivación, no una validación: queda registrada en ``huella_de_derivacion`` y el candidato
  nunca trae ``validacion`` (``AUTO_ACCEPT`` exige un corpus real, ADR 0014 §6 y ``gates.py``).
- **¿Cuánto se revisaría y cuánto malo se aceptaría con esos límites?** ``simular_revision`` aplica un registro a las
  mediciones y cuenta. ``simular_dejando_un_caso_fuera`` lo hace SIN fugar: los límites con que se evalúa un caso se
  derivan sin ese caso, porque evaluar con límites derivados de los mismos casos acepta de más.

**No compara límites por su cuenta.** Un candidato sin validación manda toda textura a ``REVIEW`` en el gate (motivo
``SIN_VALIDACION_CONTRA_CORPUS_REAL``), así que «qué haría con los límites» se lee de los motivos que ``decidir_con``
informa para la textura, descartando los que no son de ella: los del registro (que invalidan la simulación) y los de
la validación (evidencia que este módulo no pretende tener). La partición de ``Motivo`` se congela por igualdad en
``tests/test_native_parallax_x0_revision.py``: un motivo nuevo en el gate rompe el ancla hasta que se lo clasifica.

**La etiqueta «mala» es declarada.** Un control negativo lo es por construcción (NP-D0: violan la periodicidad a
propósito y el integrador periódico debe detectarlos), una altura sin rango también, y el resto por el error de altura
contra una tolerancia que NO tiene valor por defecto (``criterio_de_etiqueta`` la registra en el reporte).

Límite declarado: los números salen del corpus que se mida. Sobre el corpus sintético NO validan nada (EXP-M2 midió que
las señales solo-normal no predicen el riesgo en texturas reales, mejor AUC 0.62), y este módulo no construye ni puede
construir una ``ValidacionContraCorpusReal`` (ancla por AST en ``tests/test_native_parallax_producto_invariant.py``).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import TypeGuard

from sky_claw.local.native_parallax.gates import (
    VERSION_DE_METRICAS,
    LimitesDeAutoaceptacion,
    MetricasDeCalidad,
    Motivo,
    Umbrales,
    decidir_con,
)

__all__ = [
    "MOTIVOS_DEL_REGISTRO",
    "MOTIVOS_DE_LA_TEXTURA",
    "MOTIVOS_DE_LA_VALIDACION",
    "Conteo",
    "Medicion",
    "MedicionInvalidaError",
    "ParametroDeRevisionInvalidoError",
    "ReporteDeRevision",
    "RevisionError",
    "SinMedicionesBuenasError",
    "candidato_desde_limites",
    "criterio_de_etiqueta",
    "derivar_limites",
    "es_mala",
    "simular_dejando_un_caso_fuera",
    "simular_revision",
]

_HUELLA = re.compile(r"[0-9a-f]{64}")

MOTIVOS_DEL_REGISTRO = frozenset({Motivo.SIN_UMBRALES, Motivo.VERSION_DE_METRICAS_DISTINTA})
"""Dicen que el REGISTRO no sirve para medir (otra versión de métricas): la simulación se niega a seguir."""

MOTIVOS_DE_LA_VALIDACION = frozenset({Motivo.SIN_VALIDACION_CONTRA_CORPUS_REAL, Motivo.VALIDACION_INSUFICIENTE})
"""Hablan de la evidencia del registro, no de la textura: la simulación mide los límites, no la evidencia."""

MOTIVOS_DE_LA_TEXTURA = frozenset(
    {
        Motivo.METRICA_FUERA_DE_DOMINIO,
        Motivo.NO_FINITOS_EN_LA_ALTURA,
        Motivo.SIGNO_DISTINTO_DEL_ESPERADO,
        Motivo.SOBRE_EL_LIMITE,
    }
)
"""Los que dependen de las métricas de la textura: son los que cuentan como «iría a revisión»."""

_MOTIVOS_DE_LA_TEXTURA_EN_ORDEN = tuple(motivo for motivo in Motivo if motivo in MOTIVOS_DE_LA_TEXTURA)


class RevisionError(Exception):
    """Base de los errores de la simulación de revisión."""


class MedicionInvalidaError(RevisionError, ValueError):
    """Una medición con un campo del tipo o del rango equivocado."""


class ParametroDeRevisionInvalidoError(RevisionError, ValueError):
    """Un parámetro de la simulación fuera de su dominio (cuantil, tolerancia, registro, conjunto de mediciones)."""


class SinMedicionesBuenasError(RevisionError):
    """No hay ninguna medición buena de la que derivar límites."""


# ---------------------------------------------------------------------------
# Validación de campos
# ---------------------------------------------------------------------------


def _es_numero(valor: object) -> TypeGuard[float]:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _es_entero(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _exigir_texto(valor: object, nombre: str) -> None:
    if not isinstance(valor, str) or not valor.strip():
        raise MedicionInvalidaError(f"{nombre} debe ser un texto no vacío, no {valor!r}")


def _exigir_tolerancia(valor: object) -> float:
    if not _es_numero(valor) or not math.isfinite(valor) or valor <= 0.0:
        raise ParametroDeRevisionInvalidoError(f"tolerancia_de_altura debe ser un real finito > 0, no {valor!r}")
    return float(valor)


def _exigir_cuantil(valor: object) -> float:
    if not _es_numero(valor) or not math.isfinite(valor) or not 0.0 < valor <= 1.0:
        raise ParametroDeRevisionInvalidoError(f"cuantil debe estar en (0, 1], no {valor!r}")
    return float(valor)


def _exigir_huella(valor: object, nombre: str) -> str:
    if not isinstance(valor, str) or _HUELLA.fullmatch(valor) is None:
        raise ParametroDeRevisionInvalidoError(
            f"{nombre} (huella) debe ser un SHA-256 en hexadecimal minúscula (64 caracteres)"
        )
    return valor


def _exigir_mediciones(mediciones: Iterable[Medicion]) -> tuple[Medicion, ...]:
    lista = tuple(mediciones)
    for elemento in lista:
        if not isinstance(elemento, Medicion):
            raise ParametroDeRevisionInvalidoError(f"cada elemento debe ser una Medicion, no {elemento!r}")
    return lista


# ---------------------------------------------------------------------------
# La medición y la etiqueta
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Medicion:
    """Lo que el gate mira de una textura (``metricas``) y lo que el ground truth dice de ella.

    ``caso``: la superficie de origen (se agrupa por él al dejar un caso fuera); ``escenario``: la degradación aplicada.
    ``es_control_negativo``: el caso viola la periodicidad a propósito. ``error_de_altura``: RMSE entre la altura
    integrada y la verdadera, ambas normalizadas a [0, 1]; ``None`` si alguna no tiene rango y la otra sí.
    """

    caso: str
    escenario: str
    es_control_negativo: bool
    metricas: MetricasDeCalidad
    error_de_altura: float | None

    def __post_init__(self) -> None:
        _exigir_texto(self.caso, "caso")
        _exigir_texto(self.escenario, "escenario")
        if not isinstance(self.es_control_negativo, bool):
            raise MedicionInvalidaError(f"es_control_negativo debe ser un bool, no {self.es_control_negativo!r}")
        if not isinstance(self.metricas, MetricasDeCalidad):
            raise MedicionInvalidaError("metricas debe ser una MetricasDeCalidad")
        error = self.error_de_altura
        if error is not None and (not _es_numero(error) or not math.isfinite(error) or error < 0.0):
            raise MedicionInvalidaError(f"error_de_altura debe ser None o un real finito >= 0, no {error!r}")


def criterio_de_etiqueta(tolerancia_de_altura: float) -> str:
    """Texto que describe qué se llamó «mala», para registrarlo junto con el resultado."""
    tolerancia = _exigir_tolerancia(tolerancia_de_altura)
    return (
        "mala = control negativo (viola la periodicidad por construcción), o altura sin rango, o RMSE de la altura "
        f"normalizada a [0, 1] (min-max) contra el ground truth estrictamente mayor que {tolerancia}"
    )


def es_mala(medicion: Medicion, *, tolerancia_de_altura: float) -> bool:
    """``True`` si la medición es mala según ``criterio_de_etiqueta`` (el borde de la tolerancia es inclusivo)."""
    tolerancia = _exigir_tolerancia(tolerancia_de_altura)
    if medicion.es_control_negativo or medicion.error_de_altura is None:
        return True
    return medicion.error_de_altura > tolerancia


# ---------------------------------------------------------------------------
# Derivación de límites
# ---------------------------------------------------------------------------


def _rango_mas_proximo(valores_ordenados: list[float], cuantil: float) -> float:
    """El valor de posición ``ceil(cuantil · n)`` (1-based). El cuantil se lee como el decimal más corto que lo escribe.

    ``math.ceil(0.28 * 25)`` es 8 porque ``0.28 * 25 == 7.000000000000001``; con el decimal ``0.28`` exacto el producto
    es 7 y sale el séptimo valor, que es lo que quien llama escribió.
    """
    posicion = math.ceil(Decimal(repr(float(cuantil))) * len(valores_ordenados))
    return valores_ordenados[max(posicion, 1) - 1]


def derivar_limites(
    mediciones: Iterable[Medicion], *, cuantil: float, tolerancia_de_altura: float
) -> LimitesDeAutoaceptacion:
    """Un límite por métrica gateada: el valor de rango más próximo del ``cuantil`` entre las mediciones BUENAS.

    Con ``cuantil = 1`` el límite es el máximo observado entre las buenas. Una métrica no finita en una medición
    buena no se deriva en silencio: es un error. Los límites los valida el dominio del gate (``LimitesDeAutoaceptacion``).
    """
    cuantil_valido = _exigir_cuantil(cuantil)
    tolerancia = _exigir_tolerancia(tolerancia_de_altura)
    buenas = [m for m in _exigir_mediciones(mediciones) if not es_mala(m, tolerancia_de_altura=tolerancia)]
    if not buenas:
        raise SinMedicionesBuenasError("no hay mediciones buenas de las que derivar límites")
    limites: dict[str, float] = {}
    for campo in fields(LimitesDeAutoaceptacion):
        valores = [float(getattr(m.metricas, campo.name)) for m in buenas]
        if not all(math.isfinite(valor) for valor in valores):
            raise MedicionInvalidaError(f"hay una medición buena con {campo.name} no finito: no se puede derivar")
        limites[campo.name] = _rango_mas_proximo(sorted(valores), cuantil_valido)
    return LimitesDeAutoaceptacion(**limites)


def candidato_desde_limites(
    limites: LimitesDeAutoaceptacion, *, etiqueta: str, huella_de_derivacion: str, signo_esperado: int
) -> Umbrales:
    """Un registro SIN validación: derivar queda registrado, validar exige un corpus real (``gates.py``)."""
    return Umbrales(
        etiqueta=etiqueta,
        version_de_metricas=VERSION_DE_METRICAS,
        huella_de_derivacion=huella_de_derivacion,
        signo_esperado=signo_esperado,
        limites=limites,
        validacion=None,
    )


# ---------------------------------------------------------------------------
# El reporte
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conteo:
    """Cuántas texturas hubo, cuántas se aceptarían, cuántas eran malas y cuántas de las aceptadas lo eran."""

    n_total: int
    n_aceptadas: int
    n_malas: int
    n_aceptadas_malas: int

    def __post_init__(self) -> None:
        for nombre in ("n_total", "n_aceptadas", "n_malas", "n_aceptadas_malas"):
            valor = getattr(self, nombre)
            if not _es_entero(valor) or valor < 0:
                raise ParametroDeRevisionInvalidoError(f"{nombre} debe ser un entero >= 0, no {valor!r}")
        if self.n_total < 1:
            raise ParametroDeRevisionInvalidoError(f"n_total debe ser >= 1, no {self.n_total!r}")
        if self.n_aceptadas > self.n_total or self.n_malas > self.n_total:
            raise ParametroDeRevisionInvalidoError("n_aceptadas y n_malas no pueden superar a n_total")
        if self.n_aceptadas_malas > min(self.n_aceptadas, self.n_malas):
            raise ParametroDeRevisionInvalidoError("n_aceptadas_malas no puede superar a las aceptadas ni a las malas")
        # Las malas que NO se aceptaron están todas en revisión: no pueden ser más que las texturas en revisión.
        if self.n_malas - self.n_aceptadas_malas > self.n_total - self.n_aceptadas:
            raise ParametroDeRevisionInvalidoError("hay más malas en revisión que texturas en revisión")

    @property
    def n_en_revision(self) -> int:
        return self.n_total - self.n_aceptadas

    @property
    def tasa_de_revision(self) -> float:
        """Un solo cociente de enteros, correctamente redondeado (igual que ``ValidacionContraCorpusReal``)."""
        return (self.n_total - self.n_aceptadas) / self.n_total

    @property
    def tasa_de_falsos_aceptos(self) -> float | None:
        """Fracción de las aceptadas que eran malas; ``None`` si no se aceptó ninguna (no está definida)."""
        if self.n_aceptadas == 0:
            return None
        return self.n_aceptadas_malas / self.n_aceptadas


@dataclass(frozen=True, slots=True)
class ReporteDeRevision:
    """Resultado de una simulación. ``motivos`` cuenta, en el orden del gate, las veces que cada motivo de la textura
    apareció (una textura con varios motivos suma en cada uno); ``por_escenario`` conserva el orden de aparición.
    """

    criterio_de_etiqueta: str
    tolerancia_de_altura: float
    total: Conteo
    por_escenario: tuple[tuple[str, Conteo], ...]
    motivos: tuple[tuple[Motivo, int], ...]


# ---------------------------------------------------------------------------
# Simulación
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Evaluada:
    medicion: Medicion
    mala: bool
    motivos: tuple[Motivo, ...]  # solo los de la textura

    @property
    def aceptada(self) -> bool:
        return not self.motivos


def _evaluar(medicion: Medicion, umbrales: Umbrales, tolerancia: float) -> _Evaluada:
    decision = decidir_con(medicion.metricas, umbrales)
    del_registro = [motivo.value for motivo in decision.motivos if motivo in MOTIVOS_DEL_REGISTRO]
    if del_registro:
        raise RevisionError(f"el registro de umbrales no es simulable: {', '.join(del_registro)}")
    de_la_textura = tuple(motivo for motivo in decision.motivos if motivo in MOTIVOS_DE_LA_TEXTURA)
    return _Evaluada(medicion, es_mala(medicion, tolerancia_de_altura=tolerancia), de_la_textura)


def _contar(evaluadas: list[_Evaluada]) -> Conteo:
    aceptadas = [e for e in evaluadas if e.aceptada]
    return Conteo(
        n_total=len(evaluadas),
        n_aceptadas=len(aceptadas),
        n_malas=sum(e.mala for e in evaluadas),
        n_aceptadas_malas=sum(e.mala for e in aceptadas),
    )


def _reportar(evaluadas: list[_Evaluada], tolerancia: float) -> ReporteDeRevision:
    por_escenario: dict[str, list[_Evaluada]] = {}
    for evaluada in evaluadas:
        por_escenario.setdefault(evaluada.medicion.escenario, []).append(evaluada)
    cuentas = {motivo: 0 for motivo in _MOTIVOS_DE_LA_TEXTURA_EN_ORDEN}
    for evaluada in evaluadas:
        for motivo in evaluada.motivos:
            cuentas[motivo] += 1
    return ReporteDeRevision(
        criterio_de_etiqueta=criterio_de_etiqueta(tolerancia),
        tolerancia_de_altura=tolerancia,
        total=_contar(evaluadas),
        por_escenario=tuple((nombre, _contar(grupo)) for nombre, grupo in por_escenario.items()),
        motivos=tuple(cuentas.items()),
    )


def simular_revision(
    mediciones: Iterable[Medicion], umbrales: Umbrales, *, tolerancia_de_altura: float
) -> ReporteDeRevision:
    """Aplica los límites de ``umbrales`` a ``mediciones`` y cuenta (ver el docstring del módulo).

    Un registro con otra versión de métricas no se simula. Los límites se evalúan sobre las MISMAS mediciones de las que
    pudieron derivarse: para una estimación sin fuga, ``simular_dejando_un_caso_fuera``.
    """
    tolerancia = _exigir_tolerancia(tolerancia_de_altura)
    if not isinstance(umbrales, Umbrales):
        raise ParametroDeRevisionInvalidoError(f"umbrales debe ser un registro Umbrales, no {umbrales!r}")
    lista = _exigir_mediciones(mediciones)
    if not lista:
        raise ParametroDeRevisionInvalidoError("se necesita al menos una medición")
    return _reportar([_evaluar(m, umbrales, tolerancia) for m in lista], tolerancia)


def _huella_del_pliegue(huella_del_corpus: str, caso: str) -> str:
    return hashlib.sha256(f"{huella_del_corpus}|sin:{caso}".encode()).hexdigest()


def simular_dejando_un_caso_fuera(
    mediciones: Iterable[Medicion],
    *,
    cuantil: float,
    tolerancia_de_altura: float,
    signo_esperado: int,
    huella_del_corpus: str,
) -> ReporteDeRevision:
    """Estimación SIN fuga: cada caso se evalúa con límites derivados de TODOS los demás casos.

    Los escenarios de un mismo caso están correlacionados (misma superficie), así que se deja fuera el caso entero y
    no una medición. Las mediciones de todos los pliegues se juntan en un solo reporte. Si al dejar un caso fuera no
    quedan mediciones buenas, falla en voz alta en vez de omitir el pliegue.
    """
    tolerancia = _exigir_tolerancia(tolerancia_de_altura)
    _exigir_cuantil(cuantil)
    huella = _exigir_huella(huella_del_corpus, "huella_del_corpus")
    lista = _exigir_mediciones(mediciones)
    casos = list(dict.fromkeys(m.caso for m in lista))
    if len(casos) < 2:
        raise ParametroDeRevisionInvalidoError("dejar un caso fuera exige al menos dos casos distintos")
    evaluadas: list[_Evaluada] = []
    for caso in casos:
        limites = derivar_limites(
            [m for m in lista if m.caso != caso], cuantil=cuantil, tolerancia_de_altura=tolerancia
        )
        umbrales = candidato_desde_limites(
            limites,
            etiqueta=f"sin-{caso}",
            huella_de_derivacion=_huella_del_pliegue(huella, caso),
            signo_esperado=signo_esperado,
        )
        evaluadas.extend(_evaluar(m, umbrales, tolerancia) for m in lista if m.caso == caso)
    return _reportar(evaluadas, tolerancia)
