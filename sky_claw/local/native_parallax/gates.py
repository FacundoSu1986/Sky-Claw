"""Gate de calidad FAIL-CLOSED del generador nativo de parallax (NP-Q0b, ADR 0014 §2.5 y §6).

Decide, a partir de las métricas de ``metrics.py`` (NP-Q0a), si una textura puede aceptarse sola
(``AUTO_ACCEPT``) o tiene que pasar por una persona (``REVIEW``). Es PURO: no escribe, no consulta el entorno,
no importa ``numpy`` ni ``research`` (ADR 0014, decisión 2).

**Hoy todo va a ``REVIEW``.** El repo no versiona ningún registro de umbrales (``UMBRALES_VIGENTES is None``) porque
no hay con qué validarlo: EXP-M2, sobre texturas authored REALES, midió que las señales solo-normal no predicen el
riesgo (mejor AUC 0.62, ``REVIEW_RATE`` no viable, falsos aceptos >= 30 %). Calibrar un ``AUTO_ACCEPT`` con el
corpus sintético daría una confianza que los datos reales no respaldan.

Propiedades del mecanismo (cada una con su ancla en ``tests/test_native_parallax_gates.py``):

- **Sin registro, o con uno que no alcanza, el resultado es ``REVIEW``.** Un ``REVIEW`` siempre dice por qué
  (``Motivo``) y todos los motivos aplicables se acumulan.
- **``AUTO_ACCEPT`` exige evidencia contra un corpus real, CALCULADA y no declarada.** El registro lleva los
  CONTEOS de la validación (cuántas texturas hubo, cuántas se habrían aceptado y cuántas de esas eran malas) y el
  gate calcula la cota superior binomial (Clopper-Pearson) de la tasa de malas entre las aceptadas; no hay un
  booleano ``validado`` que alguien pueda poner en verdadero. Se pide además que la tasa de revisión sea viable.
- **El corpus sintético no puede validar.** Derivar umbrales ahí queda permitido y registrado
  (``huella_de_derivacion``); lo que el código rechaza es que su huella aparezca como corpus de VALIDACIÓN.
- **El signo cuenta.** Un flip global de gradientes produce ``-altura`` exacta y el error angular lo absorbe: solo el
  signo del ajuste lo delata. El registro declara el ``signo_esperado`` y cualquier otro va a ``REVIEW``.
- **Los umbrales se versionan junto con la definición de las métricas** (``VERSION_DE_METRICAS``): un registro derivado
  con otra definición va a ``REVIEW``.
- **No hay ``REJECT``.** No existe base para derivar un umbral de rechazo, y ``REVIEW`` ya impide el auto-aceptado. Si
  llega, llega con sus umbrales y su evidencia.
- **Cada métrica pública de ``metrics.py`` está gateada o excluida CON motivo** (``METRICAS_GATEADAS`` y
  ``METRICAS_NO_GATEADAS``): una métrica nueva rompe el ancla hasta que se decide.

Decisión de política ABIERTA (no se fija acá): la tolerancia ``alpha_objetivo`` de falsos aceptos, la ``confianza``
de la cota y la ``tasa_de_revision_maxima`` viven en el registro que declara la validación. Qué valores son
aceptables lo decide el operador en el PR que publique el primer registro, que además tiene que cambiar el ancla
``UMBRALES_VIGENTES is None``. El código garantiza la consistencia (la evidencia cumple lo declarado), no la sensatez.

Límites declarados: que un corpus declarado "real" lo sea no se puede verificar acá (solo se excluye el sintético que
el repo conoce); que subir ``VERSION_DE_METRICAS`` al cambiar una definición sea algo más que una convención no está
anclado (los valores analíticos de ``test_native_parallax_metrics.py`` fijan esas definiciones); no hay derivación de
umbrales ni runner del benchmark (NP-X0); y las métricas se calcularon sobre normales sintéticas.
"""

from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, fields
from types import MappingProxyType

from sky_claw.local.native_parallax.bench.sintetico import ESPECIFICACION_V1, huella_especificacion
from sky_claw.local.native_parallax.metrics import AcuerdoNormal

__all__ = [
    "METRICAS_GATEADAS",
    "METRICAS_NO_GATEADAS",
    "UMBRALES_VIGENTES",
    "VERSION_DE_METRICAS",
    "Decision",
    "DecisionInvalidaError",
    "GateError",
    "LimitesDeAutoaceptacion",
    "MetricasDeCalidad",
    "MetricasInvalidasError",
    "Motivo",
    "Umbrales",
    "UmbralesInvalidosError",
    "ValidacionContraCorpusReal",
    "Veredicto",
    "cota_superior_binomial",
    "decidir",
    "decidir_con",
]

VERSION_DE_METRICAS = "np-q0a.v1"
"""Definición de las métricas con la que se derivan y validan los umbrales; subirla al cambiar una (ver límites)."""

_HUELLA = re.compile(r"[0-9a-f]{64}")
_HUELLA_DEL_CORPUS_SINTETICO = huella_especificacion(ESPECIFICACION_V1)
_ITERACIONES_DE_BISECCION = 100

METRICAS_GATEADAS = MappingProxyType(
    {
        "acuerdo_normal": ("acuerdo_p95_grados", "signo"),
        "residuo_de_integrabilidad": ("residuo_de_integrabilidad",),
        "razon_de_costura": ("razon_de_costura",),
        "contar_no_finitos": ("no_finitos",),
    }
)
"""Función de ``metrics.py`` → campos de ``MetricasDeCalidad`` que alimenta."""

METRICAS_NO_GATEADAS = MappingProxyType(
    {
        "rango_dinamico": (
            "Descriptiva: depende de los percentiles que se elijan al authorizar (ADR 0014 §4 y §6, los decide "
            "NP-RIG) y no mide un error."
        ),
        "razon_de_recorte": (
            "Depende del modo de normalización y de sus percentiles, que decide NP-RIG: gatearla ahora ataría el "
            "umbral a un parámetro sin decidir."
        ),
    }
)
"""Función de ``metrics.py`` excluida del gate → motivo."""


class GateError(Exception):
    """Base de los errores del gate."""


class UmbralesInvalidosError(GateError, ValueError):
    """Un registro de umbrales, una validación o un argumento estadístico inconsistente."""


class MetricasInvalidasError(GateError, ValueError):
    """Métricas de calidad de otro tipo del esperado."""


class DecisionInvalidaError(GateError, ValueError):
    """Una decisión que rompe su invariante (``AUTO_ACCEPT`` con motivos o ``REVIEW`` sin ellos)."""


class Veredicto(enum.Enum):
    """Qué hacer con la textura."""

    AUTO_ACCEPT = "auto_accept"
    REVIEW = "review"


class Motivo(enum.Enum):
    """Por qué una textura va a revisión; el orden de definición es el orden en que se informan."""

    SIN_UMBRALES = "sin_umbrales"
    VERSION_DE_METRICAS_DISTINTA = "version_de_metricas_distinta"
    SIN_VALIDACION_CONTRA_CORPUS_REAL = "sin_validacion_contra_corpus_real"
    VALIDACION_INSUFICIENTE = "validacion_insuficiente"
    METRICA_NO_FINITA = "metrica_no_finita"
    NO_FINITOS_EN_LA_ALTURA = "no_finitos_en_la_altura"
    SIGNO_DISTINTO_DEL_ESPERADO = "signo_distinto_del_esperado"
    SOBRE_EL_LIMITE = "sobre_el_limite"


# ---------------------------------------------------------------------------
# Validación de campos
# ---------------------------------------------------------------------------


def _es_numero(valor: object) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _es_entero(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _exigir_texto(valor: object, nombre: str) -> None:
    if not isinstance(valor, str) or not valor.strip():
        raise UmbralesInvalidosError(f"{nombre} debe ser un texto no vacío, no {valor!r}")


def _exigir_huella(valor: object, nombre: str) -> None:
    if not isinstance(valor, str) or _HUELLA.fullmatch(valor) is None:
        raise UmbralesInvalidosError(f"{nombre} debe ser un SHA-256 en hexadecimal minúscula (64 caracteres)")


def _exigir_conteo(valor: object, nombre: str) -> None:
    if not _es_entero(valor) or valor < 0:  # type: ignore[operator]
        raise UmbralesInvalidosError(f"{nombre} debe ser un entero >= 0, no {valor!r}")


def _exigir_probabilidad(valor: object, nombre: str, *, abierta: bool) -> None:
    """``(0, 1)`` si ``abierta`` y ``[0, 1]`` si no."""
    if not _es_numero(valor) or not math.isfinite(valor):  # type: ignore[arg-type]
        raise UmbralesInvalidosError(f"{nombre} debe ser un real finito, no {valor!r}")
    minimo_ok = valor > 0.0 if abierta else valor >= 0.0  # type: ignore[operator]
    maximo_ok = valor < 1.0 if abierta else valor <= 1.0  # type: ignore[operator]
    if not (minimo_ok and maximo_ok):
        rango = "(0, 1)" if abierta else "[0, 1]"
        raise UmbralesInvalidosError(f"{nombre} debe estar en {rango}, no {valor!r}")


def _exigir_signo(valor: object, nombre: str, error: type[GateError]) -> None:
    if not _es_entero(valor) or valor not in (1, -1):
        raise error(f"{nombre} debe ser +1 o -1, no {valor!r}")


# ---------------------------------------------------------------------------
# Estadística: cota superior binomial
# ---------------------------------------------------------------------------


def _cdf_binomial(fallos: int, n: int, p: float) -> float:
    """``P(X <= fallos)`` con ``X ~ Binomial(n, p)`` para ``0 < p < 1``, sumando en espacio logarítmico."""
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n_factorial = math.lgamma(n + 1)
    total = 0.0
    for i in range(fallos + 1):
        log_pmf = log_n_factorial - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q
        total += math.exp(log_pmf)
    return min(total, 1.0)


def cota_superior_binomial(fallos: int, n: int, confianza: float) -> float:
    """Cota superior de Clopper-Pearson, a un lado, de la probabilidad de fallo tras ``fallos`` en ``n`` pruebas.

    Es el ``p`` tal que ``P(X <= fallos | n, p) = 1 - confianza``: el mayor valor que, con esa confianza, no se
    descarta con la evidencia. Con ``fallos == n`` vale 1. Se resuelve por bisección (la distribución acumulada es
    decreciente en ``p``) sin dependencias externas.
    """
    if not _es_entero(n) or n < 1:
        raise UmbralesInvalidosError(f"n debe ser un entero >= 1, no {n!r}")
    if not _es_entero(fallos) or not 0 <= fallos <= n:
        raise UmbralesInvalidosError(f"fallos debe ser un entero en [0, n], no {fallos!r}")
    _exigir_probabilidad(confianza, "confianza", abierta=True)
    if fallos == n:
        return 1.0
    objetivo = 1.0 - confianza
    bajo, alto = 0.0, 1.0
    for _ in range(_ITERACIONES_DE_BISECCION):
        medio = (bajo + alto) / 2.0
        if medio in (bajo, alto):
            break
        if _cdf_binomial(fallos, n, medio) > objetivo:
            bajo = medio
        else:
            alto = medio
    return alto


# ---------------------------------------------------------------------------
# Registro de umbrales
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LimitesDeAutoaceptacion:
    """Máximo de cada métrica con que una textura puede aceptarse sola (inclusive). Un campo por métrica gateada."""

    acuerdo_p95_grados: float
    residuo_de_integrabilidad: float
    razon_de_costura: float

    def __post_init__(self) -> None:
        for campo in fields(self):
            valor = getattr(self, campo.name)
            if not _es_numero(valor) or not math.isfinite(valor) or valor < 0.0:
                raise UmbralesInvalidosError(f"el límite {campo.name} debe ser un real finito >= 0, no {valor!r}")


@dataclass(frozen=True, slots=True)
class ValidacionContraCorpusReal:
    """Evidencia de que los límites aceptan poco malo, medida sobre un corpus REAL con etiquetas.

    ``n_total`` texturas; los límites habrían aceptado ``n_aceptadas`` y de esas ``n_aceptadas_malas`` eran malas
    según ``criterio_de_etiqueta``. La cota superior de la tasa de malas entre las aceptadas, con ``confianza``, tiene
    que ser ``<= alpha_objetivo``, y la tasa de revisión (las no aceptadas) ``<= tasa_de_revision_maxima``.
    """

    id_del_corpus: str
    huella_del_corpus: str
    criterio_de_etiqueta: str
    n_total: int
    n_aceptadas: int
    n_aceptadas_malas: int
    confianza: float
    alpha_objetivo: float
    tasa_de_revision_maxima: float

    def __post_init__(self) -> None:
        _exigir_texto(self.id_del_corpus, "id_del_corpus")
        _exigir_texto(self.criterio_de_etiqueta, "criterio_de_etiqueta")
        _exigir_huella(self.huella_del_corpus, "huella_del_corpus")
        if self.huella_del_corpus == _HUELLA_DEL_CORPUS_SINTETICO:
            raise UmbralesInvalidosError(
                "el corpus sintético de NP-D0 no puede validar: EXP-M2 mostró que no representa texturas reales"
            )
        for nombre in ("n_total", "n_aceptadas", "n_aceptadas_malas"):
            _exigir_conteo(getattr(self, nombre), nombre)
        if self.n_total < 1:
            raise UmbralesInvalidosError(f"n_total debe ser >= 1, no {self.n_total!r}")
        if not self.n_aceptadas_malas <= self.n_aceptadas <= self.n_total:
            raise UmbralesInvalidosError("deben cumplirse n_aceptadas_malas <= n_aceptadas <= n_total")
        _exigir_probabilidad(self.confianza, "confianza", abierta=True)
        _exigir_probabilidad(self.alpha_objetivo, "alpha_objetivo", abierta=True)
        _exigir_probabilidad(self.tasa_de_revision_maxima, "tasa_de_revision_maxima", abierta=False)

    @property
    def tasa_de_revision(self) -> float:
        """Fracción de las texturas del corpus que los límites mandarían a revisión."""
        return 1.0 - self.n_aceptadas / self.n_total

    @property
    def cota_superior_de_aceptadas_malas(self) -> float:
        """Cota superior, con ``confianza``, de la tasa de malas entre las aceptadas (1 si no se aceptó ninguna)."""
        if self.n_aceptadas == 0:
            return 1.0
        return cota_superior_binomial(self.n_aceptadas_malas, self.n_aceptadas, self.confianza)

    @property
    def alcanza(self) -> bool:
        """``True`` si la evidencia cumple lo que el propio registro declara."""
        return (
            self.n_aceptadas >= 1
            and self.cota_superior_de_aceptadas_malas <= self.alpha_objetivo
            and self.tasa_de_revision <= self.tasa_de_revision_maxima
        )


@dataclass(frozen=True, slots=True)
class Umbrales:
    """Registro versionado de umbrales: de qué se derivaron, para qué métricas valen y con qué evidencia se validaron.

    ``huella_de_derivacion`` es el SHA-256 del corpus del que se derivaron los límites (puede ser el sintético);
    ``validacion`` es la evidencia contra un corpus real, o ``None`` si no la hay (entonces solo produce ``REVIEW``).
    """

    etiqueta: str
    version_de_metricas: str
    huella_de_derivacion: str
    signo_esperado: int
    limites: LimitesDeAutoaceptacion
    validacion: ValidacionContraCorpusReal | None

    def __post_init__(self) -> None:
        _exigir_texto(self.etiqueta, "etiqueta")
        _exigir_texto(self.version_de_metricas, "version_de_metricas")
        _exigir_huella(self.huella_de_derivacion, "huella_de_derivacion")
        _exigir_signo(self.signo_esperado, "signo_esperado", UmbralesInvalidosError)
        if not isinstance(self.limites, LimitesDeAutoaceptacion):
            raise UmbralesInvalidosError("limites debe ser una LimitesDeAutoaceptacion")
        if self.validacion is not None and not isinstance(self.validacion, ValidacionContraCorpusReal):
            raise UmbralesInvalidosError("validacion debe ser una ValidacionContraCorpusReal o None")


UMBRALES_VIGENTES: Umbrales | None = None
"""El registro que aplica ``decidir``. ``None`` = no hay umbrales validados: todo va a ``REVIEW``."""


# ---------------------------------------------------------------------------
# Métricas y decisión
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricasDeCalidad:
    """Las métricas de ``metrics.py`` que el gate mira, con el estadístico que se usa de cada una.

    Los valores no finitos SE ACEPTAN acá (van a ``REVIEW``, no a una excepción); lo que se rechaza es el tipo.
    """

    acuerdo_p95_grados: float
    signo: int
    residuo_de_integrabilidad: float
    razon_de_costura: float
    no_finitos: int

    def __post_init__(self) -> None:
        for nombre in ("acuerdo_p95_grados", "residuo_de_integrabilidad", "razon_de_costura"):
            if not _es_numero(getattr(self, nombre)):
                raise MetricasInvalidasError(f"{nombre} debe ser un real, no {getattr(self, nombre)!r}")
        _exigir_signo(self.signo, "signo", MetricasInvalidasError)
        if not _es_entero(self.no_finitos) or self.no_finitos < 0:
            raise MetricasInvalidasError(f"no_finitos debe ser un entero >= 0, no {self.no_finitos!r}")

    @classmethod
    def desde_metricas(
        cls,
        acuerdo: AcuerdoNormal,
        *,
        residuo_de_integrabilidad: float,
        razon_de_costura: float,
        no_finitos: int,
    ) -> MetricasDeCalidad:
        """Arma las métricas del gate desde los resultados de ``metrics.py`` (del acuerdo toma el p95 y el signo)."""
        return cls(
            acuerdo_p95_grados=acuerdo.p95_grados,
            signo=acuerdo.signo,
            residuo_de_integrabilidad=residuo_de_integrabilidad,
            razon_de_costura=razon_de_costura,
            no_finitos=no_finitos,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """Veredicto y sus motivos. ``AUTO_ACCEPT`` no trae motivos; ``REVIEW`` trae al menos uno."""

    veredicto: Veredicto
    motivos: tuple[Motivo, ...]
    sobre_el_limite: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.veredicto is Veredicto.AUTO_ACCEPT and (self.motivos or self.sobre_el_limite):
            raise DecisionInvalidaError("AUTO_ACCEPT no puede traer motivos")
        if self.veredicto is Veredicto.REVIEW and not self.motivos:
            raise DecisionInvalidaError("REVIEW tiene que traer al menos un motivo")


def decidir_con(metricas: MetricasDeCalidad, umbrales: Umbrales | None) -> Decision:
    """Decide con un registro dado (inyectable: lo usan los tests y NP-X0 para medir candidatos)."""
    if umbrales is None:
        return Decision(Veredicto.REVIEW, (Motivo.SIN_UMBRALES,), ())
    motivos: list[Motivo] = []
    if umbrales.version_de_metricas != VERSION_DE_METRICAS:
        motivos.append(Motivo.VERSION_DE_METRICAS_DISTINTA)
    if umbrales.validacion is None:
        motivos.append(Motivo.SIN_VALIDACION_CONTRA_CORPUS_REAL)
    elif not umbrales.validacion.alcanza:
        motivos.append(Motivo.VALIDACION_INSUFICIENTE)
    campos = [campo.name for campo in fields(LimitesDeAutoaceptacion)]
    valores = {campo: getattr(metricas, campo) for campo in campos}
    if not all(math.isfinite(valor) for valor in valores.values()):
        motivos.append(Motivo.METRICA_NO_FINITA)
    if metricas.no_finitos != 0:
        motivos.append(Motivo.NO_FINITOS_EN_LA_ALTURA)
    if metricas.signo != umbrales.signo_esperado:
        motivos.append(Motivo.SIGNO_DISTINTO_DEL_ESPERADO)
    # Una métrica no finita no se compara con el límite: ya tiene su propio motivo.
    sobre = tuple(
        campo for campo in campos if math.isfinite(valores[campo]) and valores[campo] > getattr(umbrales.limites, campo)
    )
    if sobre:
        motivos.append(Motivo.SOBRE_EL_LIMITE)
    if motivos:
        return Decision(Veredicto.REVIEW, tuple(motivos), sobre)
    return Decision(Veredicto.AUTO_ACCEPT, (), ())


def decidir(metricas: MetricasDeCalidad) -> Decision:
    """Decide con ``UMBRALES_VIGENTES``: el único lugar donde el producto resuelve qué umbrales aplican."""
    return decidir_con(metricas, UMBRALES_VIGENTES)
