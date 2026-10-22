"""Núcleo puro de la salida gestionada del generador nativo de parallax (NP-O0a, ADR 0014 §2.5).

El generador escribe sus ``*_p.dds`` en un mod de salida PROPIO (``NOMBRE_DEL_MOD_DE_SALIDA``). Este módulo decide, sin
tocar el disco, tres cosas que el escritor (NP-O0b) va a dar por sentadas:

- **Quién es dueño de qué** (``Manifiesto``). El manifiesto es la ÚNICA prueba de propiedad: un archivo que no figura
  en él no es nuestro y no se pisa (``PRESERVE_EXISTING``); uno que figura pero cambió de contenido tampoco, lo tocó un
  tercero. Cada entrada lleva la huella SHA-256 de lo que escribimos y su procedencia (algoritmo, parámetros, orígenes,
  encoder, entorno y nivel de reproducibilidad). El formato es JSON canónico y de esquema cerrado: leerlo falla cerrado
  ante cualquier campo de más o de menos, clave repetida, constante no finita, clave de caché que no coincide con la
  procedencia o formato que no sea byte a byte el canónico.
- **Cuándo se puede saltear un archivo** (``clave_de_cache``). La clave es una huella de TODO lo que determina los
  bytes de la salida. Un campo que no entrara a la clave dejaría una caché que sirve resultados viejos, y una clave que
  incluya de más solo reconstruye de más: por eso el ``entorno`` (versiones que cambian los bits de un cálculo en
  coma flotante) es obligatorio y va en la clave. ``NivelDeReproducibilidad`` dice qué se promete: hoy solo
  «determinista en el MISMO entorno», sin prometer nada entre máquinas.
- **Qué hacer con cada ruta** (``planificar``). Función pura de (salidas deseadas, manifiesto previo, estado observado
  del disco). Solo se crea donde no hay nada, y solo se reconstruye o se borra un archivo que es nuestro Y sigue
  intacto; esas acciones llevan la huella que las autoriza para que el escritor la vuelva a comprobar justo antes de
  tocar el disco. Todo lo demás se preserva o se suelta del manifiesto.

Límites declarados (qué NO hace este módulo):

- No lee ni escribe archivos, no calcula huellas de archivos, no toma locks, no escribe en etapas (staging) ni
  intercambia: eso es NP-O0b. Las huellas de lo que hay en disco las trae el llamador como ``Observacion``.
- No sabe de dónde salen las salidas deseadas ni de los orígenes: eso es NP-A0.
- Las rutas son solo ASCII imprimible: la normalización de Unicode y el plegado de mayúsculas cambian entre sistemas
  de archivos. La comparación de rutas ignora mayúsculas (``clave_de_ruta``), como Windows.
- Cambiar el formato de la clave o del manifiesto exige subir ``ESQUEMA_DEL_MANIFIESTO``: invalida toda caché.
- El manifiesto serializado no puede superar ``TAMANO_MAXIMO_DEL_MANIFIESTO`` (128 MiB, unas 100.000 entradas típicas): el
  serializador falla con el MISMO tope que usa el lector, así que todo lo que se puede escribir se puede leer.

Puro: solo biblioteca estándar; no importa ``numpy`` ni ``research`` (ancla en
``tests/test_native_parallax_output.py``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ESQUEMA_DEL_MANIFIESTO",
    "NOMBRE_DEL_MOD_DE_SALIDA",
    "TAMANO_MAXIMO_DEL_MANIFIESTO",
    "AccionDeSalida",
    "AccionPlanificada",
    "EncoderUsado",
    "EntradaDeManifiesto",
    "Manifiesto",
    "ManifiestoInvalidoError",
    "MotivoDeSalida",
    "NivelDeReproducibilidad",
    "Observacion",
    "OrigenDeEntrada",
    "PlanDeSalida",
    "PlanInvalidoError",
    "Procedencia",
    "ProcedenciaInvalidaError",
    "RutaDeSalidaInvalidaError",
    "SalidaDeseada",
    "SalidaGestionadaError",
    "TipoObservado",
    "clave_de_cache",
    "clave_de_ruta",
    "crear_entrada",
    "huella_del_manifiesto",
    "leer_manifiesto",
    "parametros_canonicos",
    "planificar",
    "serializar_manifiesto",
    "validar_ruta_de_salida",
]

ESQUEMA_DEL_MANIFIESTO = "sky-claw.native-parallax.output-manifest/1"
NOMBRE_DEL_MOD_DE_SALIDA = "Sky-Claw - Native Parallax Output"
TAMANO_MAXIMO_DEL_MANIFIESTO = 128 * 1024 * 1024

# Dominios separados: la misma entrada hasheada para dos fines distintos no puede dar la misma huella.
_DOMINIO_DE_LA_CLAVE = b"sky-claw/native-parallax/clave-de-cache/1\n"
_DOMINIO_DE_LA_HUELLA = b"sky-claw/native-parallax/huella-del-manifiesto/1\n"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTIFICADOR = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ROL = re.compile(r"[a-z][a-z0-9_]{0,31}")
_NOMBRE_DE_ENTORNO = re.compile(r"[a-z][a-z0-9_]{0,31}")
_VALOR_DE_ENTORNO = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_VERSION_DE_TEXCONV = re.compile(r"[A-Za-z0-9._-]{1,32}")

_LARGO_MAXIMO_DE_RUTA = 200  # con la raíz del mod dentro de MAX_PATH (260) de Windows
_CARPETA_RAIZ = "textures"
_SUFIJO = "_p.dds"
_CARACTERES_PROHIBIDOS_EN_RUTAS = frozenset('<>:"|?*\\')
# Lista conservadora: rechazar de más es seguro (la ruta sale de una textura de la modlist y se puede renombrar),
# aceptar de más hace fallar al escritor DESPUÉS de validar. Incluye COM0/LPT0 y los dispositivos de consola.
_NOMBRES_RESERVADOS_DE_WINDOWS = frozenset(
    {
        "con",
        "conin$",
        "conout$",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
)

_LARGO_MAXIMO_DE_PARAMETROS = 65_536
_PROFUNDIDAD_MAXIMA_DE_PARAMETROS = 8
_ENTERO_MINIMO = -(2**63)
_ENTERO_MAXIMO = 2**63 - 1
_TAMANO_MAXIMO_DE_ARCHIVO = 2**30
_MAXIMO_DE_ORIGENES = 64
_MAXIMO_DE_VARIABLES_DE_ENTORNO = 32
_MAXIMO_DE_ARGUMENTOS = 64
_LARGO_MAXIMO_DE_ARGUMENTO = 256


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class SalidaGestionadaError(Exception):
    """Raíz de los errores de este módulo."""


class RutaDeSalidaInvalidaError(SalidaGestionadaError, ValueError):
    """La ruta no es una ruta de salida admisible."""


class ProcedenciaInvalidaError(SalidaGestionadaError, ValueError):
    """La procedencia (o una de sus partes) no es admisible."""


class ManifiestoInvalidoError(SalidaGestionadaError, ValueError):
    """El manifiesto, una entrada o un contenido a leer no es admisible."""


class PlanInvalidoError(SalidaGestionadaError, ValueError):
    """Los argumentos del planificador, o una acción del plan, no son coherentes."""


# ---------------------------------------------------------------------------
# Rutas de salida
# ---------------------------------------------------------------------------


def clave_de_ruta(ruta: str) -> str:
    """Clave con la que se comparan rutas: Windows (y Skyrim) no distinguen mayúsculas."""
    return ruta.casefold()


def validar_ruta_de_salida(ruta: str) -> str:
    """Devuelve ``ruta`` si es una ruta de salida admisible; si no, lanza ``RutaDeSalidaInvalidaError``.

    Admisible: relativa al mod de salida, con ``/``, solo ASCII imprimible, bajo ``textures/`` y de la forma
    ``*_p.dds``. Se RECHAZA en vez de normalizar: aceptar ``a\\b`` o ``a//b`` y reescribirlas haría que dos escrituras
    de la misma ruta dieran manifiestos distintos.
    """
    if not isinstance(ruta, str):
        raise RutaDeSalidaInvalidaError(f"la ruta debe ser texto, no {type(ruta).__name__}")
    if len(ruta) > _LARGO_MAXIMO_DE_RUTA:
        raise RutaDeSalidaInvalidaError(f"la ruta supera {_LARGO_MAXIMO_DE_RUTA} caracteres")
    for caracter in ruta:
        if not 0x20 <= ord(caracter) <= 0x7E or caracter in _CARACTERES_PROHIBIDOS_EN_RUTAS:
            raise RutaDeSalidaInvalidaError(f"carácter no admitido en la ruta {ruta!r}: {caracter!r}")
    segmentos = ruta.split("/")
    for segmento in segmentos:
        if not segmento:
            raise RutaDeSalidaInvalidaError(f"segmento vacío en la ruta {ruta!r}")
        if segmento.endswith((".", " ")):  # incluye los segmentos relativos '.' y '..'
            raise RutaDeSalidaInvalidaError(f"punto o espacio final (Windows lo recorta) en la ruta {ruta!r}")
        if segmento.split(".")[0].casefold() in _NOMBRES_RESERVADOS_DE_WINDOWS:
            raise RutaDeSalidaInvalidaError(f"nombre reservado de Windows en la ruta {ruta!r}")
    if segmentos[0].casefold() != _CARPETA_RAIZ:
        raise RutaDeSalidaInvalidaError(f"la ruta debe estar bajo {_CARPETA_RAIZ}/: {ruta!r}")
    nombre = segmentos[-1].casefold()
    if not nombre.endswith(_SUFIJO) or len(nombre) == len(_SUFIJO):
        raise RutaDeSalidaInvalidaError(f"la ruta debe ser un mapa de altura '<nombre>{_SUFIJO}': {ruta!r}")
    return ruta


# ---------------------------------------------------------------------------
# JSON estricto y parámetros canónicos
# ---------------------------------------------------------------------------


def _sin_claves_repetidas(pares: list[tuple[str, Any]]) -> dict[str, Any]:
    objeto: dict[str, Any] = {}
    for clave, valor in pares:
        if clave in objeto:
            raise ValueError(f"clave repetida en el JSON: {clave!r}")
        objeto[clave] = valor
    return objeto


def _rechazar_constante(nombre: str) -> Any:
    raise ValueError(f"constante no finita en el JSON: {nombre}")


def _cargar_json_estricto(texto: str) -> Any:
    return json.loads(texto, object_pairs_hook=_sin_claves_repetidas, parse_constant=_rechazar_constante)


def _volcar_canonico(valor: Any) -> str:
    return json.dumps(valor, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _validar_valor_json(valor: Any, profundidad: int) -> None:
    if valor is None:
        return
    if isinstance(valor, int):  # incluye bool: True/False entran como 1/0 en el rango, y se serializan distinto
        if not _ENTERO_MINIMO <= valor <= _ENTERO_MAXIMO:
            raise ProcedenciaInvalidaError(f"entero fuera del rango de 64 bits en los parámetros: {valor}")
        return
    if isinstance(valor, float):
        if not math.isfinite(valor):
            raise ProcedenciaInvalidaError("los parámetros no admiten NaN ni infinitos")
        return
    if isinstance(valor, str):
        if not valor.isascii():
            raise ProcedenciaInvalidaError("los parámetros admiten solo texto ASCII")
        return
    if profundidad >= _PROFUNDIDAD_MAXIMA_DE_PARAMETROS:
        raise ProcedenciaInvalidaError(
            f"los parámetros superan la profundidad máxima de {_PROFUNDIDAD_MAXIMA_DE_PARAMETROS}"
        )
    if isinstance(valor, list):
        for elemento in valor:
            _validar_valor_json(elemento, profundidad + 1)
        return
    if isinstance(valor, dict):
        for clave, elemento in valor.items():
            if not isinstance(clave, str):
                raise ProcedenciaInvalidaError(
                    f"las claves de los parámetros deben ser texto, no {type(clave).__name__}"
                )
            _validar_valor_json(clave, profundidad + 1)
            _validar_valor_json(elemento, profundidad + 1)
        return
    raise ProcedenciaInvalidaError(f"tipo no admitido en los parámetros: {type(valor).__name__}")


def parametros_canonicos(parametros: dict[str, Any]) -> str:
    """JSON canónico (claves ordenadas, sin espacios, solo ASCII) de un objeto de parámetros.

    Pide un ``dict`` (o subclase), no cualquier ``Mapping``: un ``MappingProxyType`` se rechaza en ejecución, y la firma lo
    dice para que mypy lo vea antes.

    Solo ``None``, booleanos, enteros de 64 bits, flotantes finitos, texto ASCII, listas y objetos con claves de
    texto; las tuplas no entran (que el llamador decida si son listas). ``True``, ``1`` y ``1.0`` dan texto
    distinto, así que dan claves de caché distintas.
    """
    if not isinstance(parametros, dict):
        raise ProcedenciaInvalidaError(f"los parámetros deben ser un objeto (dict), no {type(parametros).__name__}")
    _validar_valor_json(parametros, 0)
    texto = _volcar_canonico(parametros)
    if len(texto) > _LARGO_MAXIMO_DE_PARAMETROS:
        raise ProcedenciaInvalidaError(f"los parámetros superan {_LARGO_MAXIMO_DE_PARAMETROS} caracteres")
    return texto


# ---------------------------------------------------------------------------
# Procedencia y clave de caché
# ---------------------------------------------------------------------------


class NivelDeReproducibilidad(Enum):
    """Qué se promete sobre repetir una generación. Vocabulario cerrado: ampliarlo es una decisión deliberada."""

    DETERMINISTA_MISMO_ENTORNO = "determinista_mismo_entorno"


def _exigir_huella(valor: object, nombre: str, error: type[SalidaGestionadaError]) -> None:
    if not isinstance(valor, str) or _SHA256.fullmatch(valor) is None:
        raise error(f"{nombre} debe ser una huella SHA-256 en hex minúscula de 64 caracteres: {valor!r}")


def _exigir_patron(valor: object, patron: re.Pattern[str], nombre: str) -> None:
    if not isinstance(valor, str) or patron.fullmatch(valor) is None:
        raise ProcedenciaInvalidaError(f"{nombre} no es admisible: {valor!r}")


@dataclass(frozen=True, slots=True)
class OrigenDeEntrada:
    """Un archivo del que depende la salida (p. ej. la normal), identificado por rol y huella de su contenido."""

    rol: str
    sha256: str

    def __post_init__(self) -> None:
        _exigir_patron(self.rol, _ROL, "el rol del origen")
        _exigir_huella(self.sha256, "la huella del origen", ProcedenciaInvalidaError)


@dataclass(frozen=True, slots=True)
class EncoderUsado:
    """Identidad del exportador: nuestra tubería (``exportador_id``) y el ``texconv`` fijado con sus argumentos."""

    exportador_id: str
    texconv_version: str
    texconv_sha256: str
    argumentos: tuple[str, ...]

    def __post_init__(self) -> None:
        _exigir_patron(self.exportador_id, _IDENTIFICADOR, "el identificador del exportador")
        _exigir_patron(self.texconv_version, _VERSION_DE_TEXCONV, "la versión de texconv")
        _exigir_huella(self.texconv_sha256, "la huella de texconv", ProcedenciaInvalidaError)
        if not isinstance(self.argumentos, tuple) or len(self.argumentos) > _MAXIMO_DE_ARGUMENTOS:
            raise ProcedenciaInvalidaError(f"los argumentos deben ser una tupla de hasta {_MAXIMO_DE_ARGUMENTOS}")
        for argumento in self.argumentos:
            if (
                not isinstance(argumento, str)
                or not 1 <= len(argumento) <= _LARGO_MAXIMO_DE_ARGUMENTO
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in argumento)
            ):
                raise ProcedenciaInvalidaError(f"argumento de texconv no admisible: {argumento!r}")


@dataclass(frozen=True, slots=True)
class Procedencia:
    """TODO lo que determina los bytes de una salida: es la entrada de la clave de caché."""

    algoritmo_id: str
    parametros: str  # JSON canónico: ver ``parametros_canonicos``
    origenes: tuple[OrigenDeEntrada, ...]
    encoder: EncoderUsado
    entorno: tuple[tuple[str, str], ...]
    nivel: NivelDeReproducibilidad

    def __post_init__(self) -> None:
        _exigir_patron(self.algoritmo_id, _IDENTIFICADOR, "el identificador del algoritmo")
        self._validar_parametros()
        self._validar_origenes()
        if not isinstance(self.encoder, EncoderUsado):
            raise ProcedenciaInvalidaError(f"el encoder debe ser un EncoderUsado, no {type(self.encoder).__name__}")
        self._validar_entorno()
        if not isinstance(self.nivel, NivelDeReproducibilidad):
            raise ProcedenciaInvalidaError(f"el nivel debe ser un NivelDeReproducibilidad: {self.nivel!r}")

    def _validar_parametros(self) -> None:
        if not isinstance(self.parametros, str):
            raise ProcedenciaInvalidaError("los parámetros deben venir como texto JSON canónico (parametros_canonicos)")
        try:
            cargado = _cargar_json_estricto(self.parametros)
        except (ValueError, RecursionError) as exc:
            raise ProcedenciaInvalidaError(f"los parámetros no son JSON válido: {exc}") from exc
        if parametros_canonicos(cargado) != self.parametros:
            raise ProcedenciaInvalidaError("los parámetros no están en forma canónica")

    def _validar_origenes(self) -> None:
        if not isinstance(self.origenes, tuple) or not 1 <= len(self.origenes) <= _MAXIMO_DE_ORIGENES:
            raise ProcedenciaInvalidaError(f"los orígenes deben ser una tupla de 1 a {_MAXIMO_DE_ORIGENES}")
        if not all(isinstance(o, OrigenDeEntrada) for o in self.origenes):
            raise ProcedenciaInvalidaError("cada origen debe ser un OrigenDeEntrada")
        roles = [o.rol for o in self.origenes]
        if roles != sorted(set(roles)):
            raise ProcedenciaInvalidaError("los orígenes deben estar ordenados por rol y sin roles repetidos")

    def _validar_entorno(self) -> None:
        if not isinstance(self.entorno, tuple) or not 1 <= len(self.entorno) <= _MAXIMO_DE_VARIABLES_DE_ENTORNO:
            raise ProcedenciaInvalidaError(
                f"el entorno debe ser una tupla de 1 a {_MAXIMO_DE_VARIABLES_DE_ENTORNO} pares"
            )
        for par in self.entorno:
            if not isinstance(par, tuple) or len(par) != 2:
                raise ProcedenciaInvalidaError(f"cada variable del entorno debe ser un par (nombre, valor): {par!r}")
            _exigir_patron(par[0], _NOMBRE_DE_ENTORNO, "el nombre de la variable de entorno")
            _exigir_patron(par[1], _VALOR_DE_ENTORNO, "el valor de la variable de entorno")
        nombres = [nombre for nombre, _ in self.entorno]
        if nombres != sorted(set(nombres)):
            raise ProcedenciaInvalidaError("el entorno debe estar ordenado por nombre y sin nombres repetidos")


def _documento_de_procedencia(procedencia: Procedencia) -> dict[str, Any]:
    return {
        "algoritmo_id": procedencia.algoritmo_id,
        "parametros": _cargar_json_estricto(procedencia.parametros),
        "origenes": [{"rol": o.rol, "sha256": o.sha256} for o in procedencia.origenes],
        "encoder": {
            "exportador_id": procedencia.encoder.exportador_id,
            "texconv_version": procedencia.encoder.texconv_version,
            "texconv_sha256": procedencia.encoder.texconv_sha256,
            "argumentos": list(procedencia.encoder.argumentos),
        },
        "entorno": dict(procedencia.entorno),
        "nivel": procedencia.nivel.value,
    }


def clave_de_cache(procedencia: Procedencia) -> str:
    """Huella SHA-256 (hex) de la procedencia completa, con el esquema del manifiesto y un dominio propio."""
    if not isinstance(procedencia, Procedencia):
        raise ProcedenciaInvalidaError(f"se esperaba una Procedencia, no {type(procedencia).__name__}")
    documento = {"esquema": ESQUEMA_DEL_MANIFIESTO, **_documento_de_procedencia(procedencia)}
    return hashlib.sha256(_DOMINIO_DE_LA_CLAVE + _volcar_canonico(documento).encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Manifiesto
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntradaDeManifiesto:
    """Un archivo que generamos: dónde está, qué bytes escribimos, y por qué (la clave y su procedencia)."""

    ruta: str
    sha256: str
    tamano: int
    clave_de_cache: str
    procedencia: Procedencia

    def __post_init__(self) -> None:
        validar_ruta_de_salida(self.ruta)
        _exigir_huella(self.sha256, "la huella de la entrada", ManifiestoInvalidoError)
        if (
            isinstance(self.tamano, bool)
            or not isinstance(self.tamano, int)
            or not 1 <= self.tamano <= _TAMANO_MAXIMO_DE_ARCHIVO
        ):
            raise ManifiestoInvalidoError(
                f"el tamaño debe ser un entero entre 1 y {_TAMANO_MAXIMO_DE_ARCHIVO}: {self.tamano!r}"
            )
        if not isinstance(self.procedencia, Procedencia):
            raise ManifiestoInvalidoError("la procedencia de la entrada debe ser una Procedencia")
        if self.clave_de_cache != clave_de_cache(self.procedencia):
            raise ManifiestoInvalidoError(f"la clave de caché de {self.ruta!r} no coincide con su procedencia")


def crear_entrada(ruta: str, sha256: str, tamano: int, procedencia: Procedencia) -> EntradaDeManifiesto:
    """Arma una entrada DERIVANDO la clave de caché de la procedencia: nadie la escribe a mano."""
    if not isinstance(procedencia, Procedencia):
        raise ManifiestoInvalidoError("la procedencia de la entrada debe ser una Procedencia")
    return EntradaDeManifiesto(ruta, sha256, tamano, clave_de_cache(procedencia), procedencia)


@dataclass(frozen=True, slots=True)
class Manifiesto:
    """Las entradas que nos pertenecen, en orden canónico (por ``clave_de_ruta``) y sin rutas repetidas."""

    entradas: tuple[EntradaDeManifiesto, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entradas, tuple) or not all(isinstance(e, EntradaDeManifiesto) for e in self.entradas):
            raise ManifiestoInvalidoError("las entradas deben ser una tupla de EntradaDeManifiesto")
        claves = [clave_de_ruta(e.ruta) for e in self.entradas]
        if len(set(claves)) != len(claves):
            raise ManifiestoInvalidoError("hay rutas repetidas (ignorando mayúsculas)")
        if claves != sorted(claves):
            raise ManifiestoInvalidoError("las entradas no están en el orden canónico")

    @classmethod
    def ordenado(cls, entradas: Iterable[EntradaDeManifiesto]) -> Manifiesto:
        """Construye el manifiesto ordenando las entradas; las rutas repetidas siguen siendo un error."""
        lista = list(entradas)
        if not all(isinstance(e, EntradaDeManifiesto) for e in lista):
            raise ManifiestoInvalidoError("las entradas deben ser EntradaDeManifiesto")
        return cls(entradas=tuple(sorted(lista, key=lambda e: clave_de_ruta(e.ruta))))


def _documento_de_manifiesto(manifiesto: Manifiesto) -> dict[str, Any]:
    return {
        "esquema": ESQUEMA_DEL_MANIFIESTO,
        "mod": NOMBRE_DEL_MOD_DE_SALIDA,
        "entradas": [
            {
                "ruta": e.ruta,
                "sha256": e.sha256,
                "tamano": e.tamano,
                "clave_de_cache": e.clave_de_cache,
                "procedencia": _documento_de_procedencia(e.procedencia),
            }
            for e in manifiesto.entradas
        ],
    }


def serializar_manifiesto(manifiesto: Manifiesto) -> bytes:
    """Bytes canónicos del manifiesto: JSON ASCII, claves ordenadas, sangría de 2 y salto de línea final."""
    if not isinstance(manifiesto, Manifiesto):
        raise ManifiestoInvalidoError(f"se esperaba un Manifiesto, no {type(manifiesto).__name__}")
    texto = json.dumps(
        _documento_de_manifiesto(manifiesto), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    datos = (texto + "\n").encode("ascii")
    if len(datos) > TAMANO_MAXIMO_DEL_MANIFIESTO:
        # El MISMO tope que aplica ``leer_manifiesto``: un manifiesto que el lector rechaza dejaría sin registro de
        # propiedad (y sin caché) a todo el lote. Se falla al escribir, no al leer en la corrida siguiente.
        raise ManifiestoInvalidoError(
            f"el manifiesto serializado ({len(datos)} bytes) supera {TAMANO_MAXIMO_DEL_MANIFIESTO} bytes: dividí el lote"
        )
    return datos


def huella_del_manifiesto(manifiesto: Manifiesto) -> str:
    """SHA-256 (hex) de los bytes canónicos del manifiesto, con un dominio propio."""
    return hashlib.sha256(_DOMINIO_DE_LA_HUELLA + serializar_manifiesto(manifiesto)).hexdigest()


def _objeto(valor: object, claves: frozenset[str], donde: str) -> dict[str, Any]:
    if not isinstance(valor, dict):
        raise ManifiestoInvalidoError(f"{donde} debe ser un objeto JSON")
    if set(valor) != claves:
        faltan, sobran = sorted(claves - set(valor)), sorted(set(valor) - claves)
        raise ManifiestoInvalidoError(f"{donde}: faltan {faltan} y sobran {sobran}")
    return valor


def _lista(valor: object, donde: str) -> list[Any]:
    if not isinstance(valor, list):
        raise ManifiestoInvalidoError(f"{donde} debe ser una lista JSON")
    return valor


def _procedencia_de(documento: object, donde: str) -> Procedencia:
    campos = _objeto(
        documento,
        frozenset({"algoritmo_id", "parametros", "origenes", "encoder", "entorno", "nivel"}),
        donde,
    )
    encoder = _objeto(
        campos["encoder"],
        frozenset({"exportador_id", "texconv_version", "texconv_sha256", "argumentos"}),
        f"{donde}.encoder",
    )
    origenes = tuple(
        OrigenDeEntrada(**_objeto(o, frozenset({"rol", "sha256"}), f"{donde}.origenes[{i}]"))
        for i, o in enumerate(_lista(campos["origenes"], f"{donde}.origenes"))
    )
    if not isinstance(campos["entorno"], dict):
        raise ManifiestoInvalidoError(f"{donde}.entorno debe ser un objeto JSON")
    try:
        nivel = NivelDeReproducibilidad(campos["nivel"])
    except ValueError as exc:
        raise ManifiestoInvalidoError(f"{donde}.nivel desconocido: {campos['nivel']!r}") from exc
    return Procedencia(
        algoritmo_id=campos["algoritmo_id"],
        parametros=parametros_canonicos(campos["parametros"]),
        origenes=origenes,
        encoder=EncoderUsado(
            exportador_id=encoder["exportador_id"],
            texconv_version=encoder["texconv_version"],
            texconv_sha256=encoder["texconv_sha256"],
            argumentos=tuple(_lista(encoder["argumentos"], f"{donde}.encoder.argumentos")),
        ),
        entorno=tuple(campos["entorno"].items()),
        nivel=nivel,
    )


def _entrada_de(documento: object, indice: int) -> EntradaDeManifiesto:
    donde = f"entradas[{indice}]"
    campos = _objeto(documento, frozenset({"ruta", "sha256", "tamano", "clave_de_cache", "procedencia"}), donde)
    return EntradaDeManifiesto(
        ruta=campos["ruta"],
        sha256=campos["sha256"],
        tamano=campos["tamano"],
        clave_de_cache=campos["clave_de_cache"],
        procedencia=_procedencia_de(campos["procedencia"], f"{donde}.procedencia"),
    )


def leer_manifiesto(datos: bytes) -> Manifiesto:
    """Interpreta los bytes de un manifiesto. Falla cerrado ante todo lo que no sea EXACTAMENTE el formato canónico.

    Las partes se validan con los mismos constructores que usa el escritor, así que un manifiesto editado a mano no
    puede colar un valor que el escritor no habría producido; la clave de caché se recalcula y se compara.
    """
    if not isinstance(datos, bytes | bytearray):
        raise ManifiestoInvalidoError(f"el manifiesto debe leerse de bytes, no de {type(datos).__name__}")
    if len(datos) > TAMANO_MAXIMO_DEL_MANIFIESTO:
        raise ManifiestoInvalidoError(f"el manifiesto supera {TAMANO_MAXIMO_DEL_MANIFIESTO} bytes")
    try:
        documento = _cargar_json_estricto(datos.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError y JSONDecodeError son ValueError
        raise ManifiestoInvalidoError(f"el manifiesto no es JSON válido: {exc}") from exc
    try:
        campos = _objeto(documento, frozenset({"esquema", "mod", "entradas"}), "el manifiesto")
        if campos["esquema"] != ESQUEMA_DEL_MANIFIESTO:
            raise ManifiestoInvalidoError(f"esquema desconocido: {campos['esquema']!r}")
        if campos["mod"] != NOMBRE_DEL_MOD_DE_SALIDA:
            raise ManifiestoInvalidoError(f"el manifiesto es de otro mod: {campos['mod']!r}")
        manifiesto = Manifiesto(
            entradas=tuple(_entrada_de(e, i) for i, e in enumerate(_lista(campos["entradas"], "entradas")))
        )
    except ManifiestoInvalidoError:
        raise
    except SalidaGestionadaError as exc:
        raise ManifiestoInvalidoError(f"manifiesto inválido: {exc}") from exc
    if serializar_manifiesto(manifiesto) != datos:
        raise ManifiestoInvalidoError("el manifiesto no está en la forma canónica byte a byte")
    return manifiesto


# ---------------------------------------------------------------------------
# Planificador
# ---------------------------------------------------------------------------


class TipoObservado(Enum):
    """Qué hay en disco en una ruta, según quien la observó."""

    AUSENTE = "ausente"
    ARCHIVO = "archivo"  # un archivo regular, con su huella
    OTRO = "otro"  # un directorio, un enlace, un dispositivo...: nunca se toca


class AccionDeSalida(Enum):
    CONSTRUIR = "construir"  # crear donde no hay nada
    RECONSTRUIR = "reconstruir"  # reemplazar un archivo nuestro e intacto
    OMITIR = "omitir"  # la caché está vigente y el archivo intacto
    PRESERVAR = "preservar"  # no escribir: el archivo (o lo que ocupa la ruta) no es nuestro o cambió
    RETIRAR = "retirar"  # borrar un archivo nuestro e intacto que ya no se desea
    SOLTAR = "soltar"  # dejar de administrarlo (sale del manifiesto) sin tocar el disco


class MotivoDeSalida(Enum):
    NUEVA = "nueva"
    FALTA_EN_DISCO = "falta_en_disco"
    CACHE_VIGENTE = "cache_vigente"
    CLAVE_CAMBIO = "clave_cambio"
    AJENO_EXISTENTE = "ajeno_existente"
    MODIFICADO_POR_TERCEROS = "modificado_por_terceros"
    OCUPADO_POR_OTRO_TIPO = "ocupado_por_otro_tipo"
    HUERFANO_PROPIO = "huerfano_propio"
    HUERFANO_AUSENTE = "huerfano_ausente"


_ACCIONES_QUE_AUTORIZAN_UNA_HUELLA = frozenset({AccionDeSalida.RECONSTRUIR, AccionDeSalida.RETIRAR})
_ACCIONES_QUE_TOCAN_EL_DISCO = frozenset({AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR, AccionDeSalida.RETIRAR})


@dataclass(frozen=True, slots=True)
class Observacion:
    """Lo que el llamador vio en disco en una ruta. ``sha256`` va si y solo si es un archivo regular."""

    tipo: TipoObservado
    sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.tipo, TipoObservado):
            raise PlanInvalidoError(f"el tipo observado debe ser un TipoObservado: {self.tipo!r}")
        if self.tipo is TipoObservado.ARCHIVO:
            _exigir_huella(self.sha256, "la huella de un archivo observado", PlanInvalidoError)
        elif self.sha256 is not None:
            raise PlanInvalidoError("solo un archivo observado lleva huella")


@dataclass(frozen=True, slots=True)
class SalidaDeseada:
    """Una salida que el generador quiere tener: su ruta y la clave de caché de lo que la produciría."""

    ruta: str
    clave_de_cache: str

    def __post_init__(self) -> None:
        validar_ruta_de_salida(self.ruta)
        _exigir_huella(self.clave_de_cache, "la clave de caché deseada", PlanInvalidoError)


@dataclass(frozen=True, slots=True)
class AccionPlanificada:
    """Qué hacer con una ruta. ``sha256_autorizado`` es la huella que debe tener el archivo para poder tocarlo."""

    ruta: str
    accion: AccionDeSalida
    motivo: MotivoDeSalida
    sha256_autorizado: str | None

    def __post_init__(self) -> None:
        validar_ruta_de_salida(self.ruta)
        if not isinstance(self.accion, AccionDeSalida) or not isinstance(self.motivo, MotivoDeSalida):
            raise PlanInvalidoError("la acción y el motivo deben ser AccionDeSalida y MotivoDeSalida")
        if self.accion in _ACCIONES_QUE_AUTORIZAN_UNA_HUELLA:
            _exigir_huella(self.sha256_autorizado, "la huella que autoriza la acción", PlanInvalidoError)
        elif self.sha256_autorizado is not None:
            raise PlanInvalidoError(f"la acción {self.accion.name} no lleva huella autorizante")

    @property
    def modifica_disco(self) -> bool:
        return self.accion in _ACCIONES_QUE_TOCAN_EL_DISCO


@dataclass(frozen=True, slots=True)
class PlanDeSalida:
    """Las acciones, una por ruta, ordenadas por ``clave_de_ruta``."""

    acciones: tuple[AccionPlanificada, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.acciones, tuple) or not all(isinstance(a, AccionPlanificada) for a in self.acciones):
            raise PlanInvalidoError("las acciones deben ser una tupla de AccionPlanificada")
        claves = [clave_de_ruta(a.ruta) for a in self.acciones]
        if claves != sorted(set(claves)):
            raise PlanInvalidoError("las acciones deben estar ordenadas por ruta y sin rutas repetidas")

    @property
    def hay_cambios(self) -> bool:
        """True si alguna acción toca el disco (``SOLTAR`` y ``PRESERVAR`` solo afectan al manifiesto)."""
        return any(a.modifica_disco for a in self.acciones)


def _decidir(
    deseada: SalidaDeseada | None, entrada: EntradaDeManifiesto | None, observacion: Observacion
) -> tuple[AccionDeSalida, MotivoDeSalida, str | None]:
    if entrada is None:  # nada es nuestro todavía: PRESERVE_EXISTING
        if observacion.tipo is TipoObservado.AUSENTE:
            return AccionDeSalida.CONSTRUIR, MotivoDeSalida.NUEVA, None
        if observacion.tipo is TipoObservado.ARCHIVO:
            return AccionDeSalida.PRESERVAR, MotivoDeSalida.AJENO_EXISTENTE, None
        return AccionDeSalida.PRESERVAR, MotivoDeSalida.OCUPADO_POR_OTRO_TIPO, None
    if observacion.tipo is TipoObservado.OTRO:
        accion = AccionDeSalida.PRESERVAR if deseada is not None else AccionDeSalida.SOLTAR
        return accion, MotivoDeSalida.OCUPADO_POR_OTRO_TIPO, None
    if observacion.tipo is TipoObservado.AUSENTE:
        if deseada is not None:
            return AccionDeSalida.CONSTRUIR, MotivoDeSalida.FALTA_EN_DISCO, None
        return AccionDeSalida.SOLTAR, MotivoDeSalida.HUERFANO_AUSENTE, None
    intacto = observacion.sha256 == entrada.sha256
    if deseada is None:
        if intacto:
            return AccionDeSalida.RETIRAR, MotivoDeSalida.HUERFANO_PROPIO, entrada.sha256
        return AccionDeSalida.SOLTAR, MotivoDeSalida.MODIFICADO_POR_TERCEROS, None
    if not intacto:
        return AccionDeSalida.PRESERVAR, MotivoDeSalida.MODIFICADO_POR_TERCEROS, None
    if deseada.clave_de_cache == entrada.clave_de_cache:
        return AccionDeSalida.OMITIR, MotivoDeSalida.CACHE_VIGENTE, None
    return AccionDeSalida.RECONSTRUIR, MotivoDeSalida.CLAVE_CAMBIO, entrada.sha256


def planificar(
    deseadas: Iterable[SalidaDeseada],
    previo: Manifiesto | None,
    observado: Mapping[str, Observacion],
) -> PlanDeSalida:
    """Decide qué hacer con cada ruta relevante (la deseada o la que figura en el manifiesto previo).

    ``observado`` se indexa por ``clave_de_ruta`` y debe tener la observación de TODA ruta relevante: planificar a
    ciegas no es una opción. Las observaciones de rutas no relevantes se ignoran. Una ruta que ni se desea ni figura en
    el manifiesto no genera acción (no es asunto nuestro). Las rutas que solo difieren en mayúsculas son la misma.
    """
    if previo is not None and not isinstance(previo, Manifiesto):
        raise PlanInvalidoError(f"el manifiesto previo debe ser un Manifiesto o None, no {type(previo).__name__}")
    if not isinstance(observado, Mapping):
        raise PlanInvalidoError(f"las observaciones deben ser un mapa, no {type(observado).__name__}")
    if not all(isinstance(o, Observacion) for o in observado.values()):
        raise PlanInvalidoError("cada observación debe ser una Observacion")
    lista = list(deseadas)
    if not all(isinstance(d, SalidaDeseada) for d in lista):
        raise PlanInvalidoError("cada salida deseada debe ser una SalidaDeseada")

    por_deseada: dict[str, SalidaDeseada] = {}
    for salida in lista:
        clave = clave_de_ruta(salida.ruta)
        if clave in por_deseada:
            raise PlanInvalidoError(f"dos salidas deseadas apuntan a la misma ruta: {salida.ruta!r}")
        por_deseada[clave] = salida
    por_entrada = {clave_de_ruta(e.ruta): e for e in (previo.entradas if previo is not None else ())}
    # La ortografía de la ruta es la de la salida deseada; si solo figura en el manifiesto, la de la entrada.
    rutas = {clave: e.ruta for clave, e in por_entrada.items()}
    rutas.update({clave: s.ruta for clave, s in por_deseada.items()})

    acciones: list[AccionPlanificada] = []
    for clave in sorted(rutas):
        ruta = rutas[clave]
        deseada = por_deseada.get(clave)
        entrada = por_entrada.get(clave)
        if clave not in observado:
            raise PlanInvalidoError(
                f"falta la observación de {ruta!r}: el mapa se indexa por clave_de_ruta, o sea {clave!r}"
            )
        accion, motivo, autorizada = _decidir(deseada, entrada, observado[clave])
        acciones.append(AccionPlanificada(ruta, accion, motivo, autorizada))
    return PlanDeSalida(acciones=tuple(acciones))
