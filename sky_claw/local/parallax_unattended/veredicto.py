"""Veredicto de un lote de parallax: ¿el trabajo SIRVE, o sólo terminó?

Por qué existe este módulo
--------------------------
El informe del generador (``informe_parallax.json``) describe cada textura con DOS ejes que
no son intercambiables:

===============  ==========================================  ==============================
eje              pregunta que contesta                      valores
===============  ==========================================  ==============================
``estado``       ¿dónde terminó el archivo?                  ESCRITA / EN_REVISION / NO_*
``veredicto``    ¿el resultado sirve?                        AUTO_ACCEPT / REVIEW
===============  ==========================================  ==============================

``conteos`` agrega el PRIMERO, y el código de salida del generador se deriva de ``completo``,
que también mira el primero. Medido en el rig real (issue #792): con ``--incluir-revision``
una textura con ``veredicto: REVIEW`` queda ``estado: ESCRITA``, ``conteos.EN_REVISION`` da
cero y el proceso sale 0. Así se publicaron tres de quince mapas planos.

Este módulo contesta la otra pregunta, y es el ÚNICO lugar que decide éxito en una corrida
desatendida. **No importa el generador**: consume el informe como artefacto con su
``version_del_lote``, para que un cambio en el generador no pueda redefinir en silencio qué
significa "listo".

Reglas, en orden de precedencia (la primera que aplica gana)
-----------------------------------------------------------
1. ``INCONSISTENTE`` — el informe no se puede creer: forma inválida, ``conteos`` que no
   cuadran con ``archivos``, archivo escrito sin veredicto, o artefacto publicado sin
   registro del codificador. Un informe que se contradice no certifica nada, ni siquiera
   un fallo: por eso va por encima de ``FAIL``.
2. ``FAIL`` — hay trabajo perdido: ``NO_PROCESADA``, ``NO_ESCRITA``, o lote vacío.
3. ``REVIEW`` — todo se escribió, pero algo quedó en revisión. **Aunque esté en disco.**
4. ``PASS`` — sólo si ningún archivo quedó en revisión.

``PASS`` es el único veredicto que devuelve 0. Los otros tres devuelven códigos distintos
entre sí para que quien llama pueda distinguir "hay que mirar" de "está roto" sin parsear
texto.

Límites declarados
------------------
Este módulo NO verifica que el artefacto en disco exista ni que tenga relieve: eso es
``auditor.py``. Un ``PASS`` acá significa "el informe no se contradice y nada quedó en
revisión", no "la textura está bien".
"""

from __future__ import annotations

import enum
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class VeredictoDelLote(enum.StrEnum):
    """Veredicto de la corrida completa. Sólo ``PASS`` es éxito."""

    PASS = "pass"
    REVIEW = "review"
    FAIL = "fail"
    INCONSISTENTE = "inconsistente"


class MotivoDelLote(enum.StrEnum):
    """Por qué el lote no es ``PASS``. Vocabulario cerrado: agregar uno exige revisar la tabla."""

    FORMA_INVALIDA = "forma_invalida"
    LOTE_VACIO = "lote_vacio"
    CONTEO_DIVERGENTE = "conteo_divergente"
    ENCODER_NO_REGISTRADO = "encoder_no_registrado"
    ARCHIVO_SIN_VEREDICTO = "archivo_sin_veredicto"
    VEREDICTO_INCOHERENTE = "veredicto_incoherente"
    ARTEFACTO_INVALIDO = "artefacto_invalido"
    ARTEFACTO_NO_CONFIABLE = "artefacto_no_confiable"
    ARCHIVO_NO_PROCESADO = "archivo_no_procesado"
    ARCHIVO_NO_ESCRITO = "archivo_no_escrito"
    ARCHIVO_EN_REVISION = "archivo_en_revision"


#: Motivos que hacen que el informe no se pueda creer (precedencia 1).
MOTIVOS_DE_INCONSISTENCIA: Final[frozenset[MotivoDelLote]] = frozenset(
    {
        MotivoDelLote.FORMA_INVALIDA,
        MotivoDelLote.CONTEO_DIVERGENTE,
        MotivoDelLote.ENCODER_NO_REGISTRADO,
        MotivoDelLote.ARCHIVO_SIN_VEREDICTO,
        MotivoDelLote.VEREDICTO_INCOHERENTE,
    }
)

#: Motivos de trabajo perdido o artefacto corrupto/ausente en disco (precedencia 2).
MOTIVOS_DE_FALLA: Final[frozenset[MotivoDelLote]] = frozenset(
    {
        MotivoDelLote.LOTE_VACIO,
        MotivoDelLote.ARCHIVO_NO_PROCESADO,
        MotivoDelLote.ARCHIVO_NO_ESCRITO,
        MotivoDelLote.ARTEFACTO_INVALIDO,
    }
)

#: Motivos que dejan el lote en revisión (precedencia 3). ``ARTEFACTO_NO_CONFIABLE`` lo pone
#: ``composicion.combinar`` cuando el post-check del artefacto encontró altura plana o un formato
#: no medible: un mapa plano es indistinguible de una superficie lisa mirando sólo el archivo,
#: así que no puede ser ``FAIL``.
MOTIVOS_DE_REVISION: Final[frozenset[MotivoDelLote]] = frozenset(
    {
        MotivoDelLote.ARCHIVO_EN_REVISION,
        MotivoDelLote.ARTEFACTO_NO_CONFIABLE,
    }
)

#: Versiones del contrato del informe que esta capa sabe interpretar (``lote.VERSION_DEL_LOTE``).
VERSIONES_DEL_LOTE_CONOCIDAS: Final[frozenset[str]] = frozenset({"np-a0a.v1"})

#: Estados que declara el generador. Vocabulario cerrado (``lote.EstadoDeArchivo``).
ESTADOS_CONOCIDOS: Final[frozenset[str]] = frozenset({"ESCRITA", "EN_REVISION", "NO_PROCESADA", "NO_ESCRITA"})

#: Veredictos que declara el generador. Vocabulario cerrado (``gates.Veredicto``).
VEREDICTOS_CONOCIDOS: Final[frozenset[str]] = frozenset({"AUTO_ACCEPT", "REVIEW"})

#: Efectos que declara el escritor (``output_writer.EfectoDeSalida``). Vocabulario cerrado.
EFECTOS_CONOCIDOS: Final[frozenset[str]] = frozenset(
    {"PUBLICADA", "REEMPLAZADA", "OMITIDA", "RETIRADA", "PRESERVADA", "SOLTADA", "CONFLICTO", "FALLIDA"}
)

#: Efectos del escritor que dejan un archivo en la salida (``output_writer.EfectoDeSalida``).
#: ``OMITIDA`` cuenta: la caché vigente dejó el archivo de una corrida anterior.
EFECTOS_QUE_DEJAN_ARCHIVO: Final[frozenset[str]] = frozenset({"PUBLICADA", "REEMPLAZADA", "OMITIDA"})

#: Efectos del escritor que reportan fallo al aplicar sobre la salida.
EFECTOS_FALLIDOS: Final[frozenset[str]] = frozenset({"CONFLICTO", "FALLIDA"})

_PATRON_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}\Z")

#: Códigos de salida. Distintos a propósito: ``PASS`` es el único 0.
CODIGOS_DE_SALIDA: Final[Mapping[VeredictoDelLote, int]] = MappingProxyType(
    {
        VeredictoDelLote.PASS: 0,
        VeredictoDelLote.FAIL: 1,
        VeredictoDelLote.INCONSISTENTE: 2,
        VeredictoDelLote.REVIEW: 3,
    }
)


class InformeInvalidoError(ValueError):
    """El informe no tiene la forma del contrato: no se adivina, se rechaza."""


@dataclass(frozen=True, slots=True)
class _Archivo:
    """Lo que este módulo necesita de cada entrada de ``archivos``. El resto se ignora."""

    ruta: str
    estado: str
    efecto: str | None
    veredicto: str | None


@dataclass(frozen=True, slots=True)
class _OtraRuta:
    """Entrada de ``otras_rutas``: salidas previas conservadas, retiradas o con conflicto."""

    ruta: str
    efecto: str


@dataclass(frozen=True, slots=True)
class DecisionDelLote:
    """Veredicto + todo lo que lo sostiene, para que el operador no tenga que re-derivarlo."""

    veredicto: VeredictoDelLote
    motivos: tuple[MotivoDelLote, ...]
    rutas: tuple[str, ...]
    conteos: Mapping[str, int]
    detalle: str = ""

    @property
    def es_exito(self) -> bool:
        return self.veredicto is VeredictoDelLote.PASS


def _texto(valor: object, campo: str) -> str:
    if not isinstance(valor, str) or not valor:
        raise InformeInvalidoError(f"{campo} debe ser un texto no vacío, no {valor!r}")
    return valor


def _opcional(valor: object, campo: str) -> str | None:
    if valor is None:
        return None
    return _texto(valor, campo)


def _validar_amplitud(valor: object, campo: str) -> None:
    if valor is None:
        return
    if not isinstance(valor, Mapping):
        raise InformeInvalidoError(f"{campo} debe ser un objeto o None")
    desviacion = valor.get("desviacion")
    if (
        isinstance(desviacion, bool)
        or not isinstance(desviacion, (int, float))
        or not math.isfinite(desviacion)
        or not (0.0 <= float(desviacion) <= 1.0)
    ):
        raise InformeInvalidoError(f"{campo}.desviacion = {desviacion!r} debe ser un número finito en [0, 1]")


def _leer_archivos(informe: Mapping[str, object], vistas: set[str]) -> tuple[_Archivo, ...]:
    crudo = informe.get("archivos")
    if not isinstance(crudo, list):
        raise InformeInvalidoError("falta 'archivos' o no es una lista")

    archivos: list[_Archivo] = []
    for posicion, entrada in enumerate(crudo):
        if not isinstance(entrada, Mapping):
            raise InformeInvalidoError(f"archivos[{posicion}] no es un objeto")
        ruta = _texto(entrada.get("ruta_de_salida"), f"archivos[{posicion}].ruta_de_salida")
        clave = ruta.casefold()
        if clave in vistas:
            raise InformeInvalidoError(f"archivos[{posicion}].ruta_de_salida = {ruta!r} está duplicada")
        vistas.add(clave)
        estado = _texto(entrada.get("estado"), f"archivos[{posicion}].estado")
        if estado not in ESTADOS_CONOCIDOS:
            raise InformeInvalidoError(
                f"archivos[{posicion}].estado = {estado!r} no está en {sorted(ESTADOS_CONOCIDOS)}"
            )
        efecto = _opcional(entrada.get("efecto"), f"archivos[{posicion}].efecto")
        if efecto is not None and efecto not in EFECTOS_CONOCIDOS:
            raise InformeInvalidoError(
                f"archivos[{posicion}].efecto = {efecto!r} no está en {sorted(EFECTOS_CONOCIDOS)}"
            )
        veredicto = _opcional(entrada.get("veredicto"), f"archivos[{posicion}].veredicto")
        if veredicto is not None and veredicto not in VEREDICTOS_CONOCIDOS:
            raise InformeInvalidoError(
                f"archivos[{posicion}].veredicto = {veredicto!r} no está en {sorted(VEREDICTOS_CONOCIDOS)}"
            )
        _validar_amplitud(entrada.get("amplitud"), f"archivos[{posicion}].amplitud")
        archivos.append(
            _Archivo(
                ruta=ruta,
                estado=estado,
                efecto=efecto,
                veredicto=veredicto,
            )
        )
    return tuple(archivos)


def _leer_otras_rutas(informe: Mapping[str, object], vistas: set[str]) -> tuple[_OtraRuta, ...]:
    if "otras_rutas" not in informe or informe.get("otras_rutas") is None:
        return ()
    crudo = informe.get("otras_rutas")
    if not isinstance(crudo, list):
        raise InformeInvalidoError("'otras_rutas' debe ser una lista")
    otras: list[_OtraRuta] = []
    for posicion, entrada in enumerate(crudo):
        if not isinstance(entrada, Mapping):
            raise InformeInvalidoError(f"otras_rutas[{posicion}] no es un objeto")
        ruta = _texto(entrada.get("ruta"), f"otras_rutas[{posicion}].ruta")
        clave = ruta.casefold()
        if clave in vistas:
            raise InformeInvalidoError(f"otras_rutas[{posicion}].ruta = {ruta!r} colisiona con otra ruta del informe")
        vistas.add(clave)
        efecto = _texto(entrada.get("efecto"), f"otras_rutas[{posicion}].efecto")
        if efecto not in EFECTOS_CONOCIDOS or efecto in ("PUBLICADA", "REEMPLAZADA"):
            raise InformeInvalidoError(f"otras_rutas[{posicion}].efecto = {efecto!r} no es válido para una ruta previa")
        otras.append(_OtraRuta(ruta=ruta, efecto=efecto))
    return tuple(otras)


def _leer_conteos(informe: Mapping[str, object]) -> Mapping[str, int]:
    crudo = informe.get("conteos")
    if not isinstance(crudo, Mapping):
        raise InformeInvalidoError("falta 'conteos' o no es un objeto")
    conteos: dict[str, int] = {}
    for clave, valor in crudo.items():
        if not isinstance(clave, str) or not isinstance(valor, int) or isinstance(valor, bool):
            raise InformeInvalidoError(f"conteos[{clave!r}] = {valor!r} no es un entero")
        conteos[clave] = valor
    return conteos


def _contar(archivos: Sequence[_Archivo]) -> dict[str, int]:
    """Recuenta desde ``archivos``. ``conteos`` del informe NUNCA se usa como fuente de verdad."""

    conteos = {estado: 0 for estado in sorted(ESTADOS_CONOCIDOS)}
    for archivo in archivos:
        conteos[archivo.estado] += 1
    return conteos


def _encoder_registrado(informe: Mapping[str, object]) -> bool:
    """Un artefacto sin registro de qué binario lo codificó no es auditable."""

    crudo = informe.get("encoder")
    if not isinstance(crudo, Mapping):
        return False
    version = crudo.get("texconv_version")
    huella = crudo.get("texconv_sha256")
    return (
        isinstance(version, str)
        and bool(version.strip())
        and isinstance(huella, str)
        and _PATRON_SHA256_HEX.fullmatch(huella) is not None
    )


def _agregar(motivos: list[MotivoDelLote], motivo: MotivoDelLote) -> None:
    if motivo not in motivos:
        motivos.append(motivo)


def _agregar_ruta(rutas: list[str], ruta: str) -> None:
    if ruta not in rutas:
        rutas.append(ruta)


def _leer_version(informe: Mapping[str, object]) -> str:
    version = _texto(informe.get("version_del_lote"), "version_del_lote")
    if version not in VERSIONES_DEL_LOTE_CONOCIDAS:
        raise InformeInvalidoError(f"version_del_lote = {version!r} no está en {sorted(VERSIONES_DEL_LOTE_CONOCIDAS)}")
    return version


def dictar(informe: Mapping[str, object]) -> DecisionDelLote:
    """Decide si el lote sirve. Fail-closed: ante la duda, nunca ``PASS``."""

    try:
        if not isinstance(informe, Mapping):
            raise InformeInvalidoError("el informe debe ser un objeto")
        _leer_version(informe)
        vistas: set[str] = set()
        archivos = _leer_archivos(informe, vistas)
        otras_rutas = _leer_otras_rutas(informe, vistas)
        declarados = _leer_conteos(informe)
    except InformeInvalidoError as exc:
        return DecisionDelLote(
            veredicto=VeredictoDelLote.INCONSISTENTE,
            motivos=(MotivoDelLote.FORMA_INVALIDA,),
            rutas=(),
            conteos={},
            detalle=str(exc),
        )

    motivos: list[MotivoDelLote] = []
    rutas: list[str] = []
    conteos = _contar(archivos)

    if not archivos:
        _agregar(motivos, MotivoDelLote.LOTE_VACIO)
    if dict(declarados) != conteos:
        _agregar(motivos, MotivoDelLote.CONTEO_DIVERGENTE)

    hay_artefacto = any(a.efecto in EFECTOS_QUE_DEJAN_ARCHIVO for a in archivos)
    if hay_artefacto and not _encoder_registrado(informe):
        _agregar(motivos, MotivoDelLote.ENCODER_NO_REGISTRADO)

    for archivo in archivos:
        if archivo.estado == "NO_PROCESADA":
            if archivo.efecto is not None:
                _agregar(motivos, MotivoDelLote.FORMA_INVALIDA)
            _agregar(motivos, MotivoDelLote.ARCHIVO_NO_PROCESADO)
            _agregar_ruta(rutas, archivo.ruta)
        elif archivo.estado == "NO_ESCRITA":
            if archivo.efecto in EFECTOS_QUE_DEJAN_ARCHIVO:
                _agregar(motivos, MotivoDelLote.FORMA_INVALIDA)
            _agregar(motivos, MotivoDelLote.ARCHIVO_NO_ESCRITO)
            _agregar_ruta(rutas, archivo.ruta)
        elif archivo.estado == "EN_REVISION":
            if archivo.efecto is not None:
                _agregar(motivos, MotivoDelLote.FORMA_INVALIDA)
            # No se escribió porque el gate la retuvo: es una decisión, pero no es éxito.
            _agregar(motivos, MotivoDelLote.ARCHIVO_EN_REVISION)
            _agregar_ruta(rutas, archivo.ruta)
            if archivo.veredicto != "REVIEW":
                # "En revisión" con veredicto AUTO_ACCEPT se contradice: o el gate lo retuvo, o pasó.
                _agregar(motivos, MotivoDelLote.VEREDICTO_INCOHERENTE)
        elif archivo.estado == "ESCRITA":
            if archivo.efecto not in EFECTOS_QUE_DEJAN_ARCHIVO:
                # Un estado de escritura con un efecto que no deja archivo es un informe que miente.
                _agregar(motivos, MotivoDelLote.FORMA_INVALIDA)
                _agregar_ruta(rutas, archivo.ruta)
                continue
            if archivo.veredicto is None:
                _agregar(motivos, MotivoDelLote.ARCHIVO_SIN_VEREDICTO)
                _agregar_ruta(rutas, archivo.ruta)
            elif archivo.veredicto == "REVIEW":
                # EL CASO QUE EL CÓDIGO DE SALIDA DEL GENERADOR NO VE: escrito y en revisión.
                _agregar(motivos, MotivoDelLote.ARCHIVO_EN_REVISION)
                _agregar_ruta(rutas, archivo.ruta)

    for otra in otras_rutas:
        if otra.efecto in EFECTOS_FALLIDOS:
            _agregar(motivos, MotivoDelLote.ARCHIVO_NO_ESCRITO)
            _agregar_ruta(rutas, otra.ruta)

    if any(m in MOTIVOS_DE_INCONSISTENCIA for m in motivos):
        veredicto = VeredictoDelLote.INCONSISTENTE
    elif any(m in MOTIVOS_DE_FALLA for m in motivos):
        veredicto = VeredictoDelLote.FAIL
    elif any(m in MOTIVOS_DE_REVISION for m in motivos):
        veredicto = VeredictoDelLote.REVIEW
    else:
        veredicto = VeredictoDelLote.PASS

    return DecisionDelLote(
        veredicto=veredicto,
        motivos=tuple(motivos),
        rutas=tuple(rutas),
        conteos=conteos,
    )


def codigo_de_salida(decision: DecisionDelLote) -> int:
    """Traduce el veredicto a un código de proceso. ``PASS`` es el único 0."""

    return CODIGOS_DE_SALIDA[decision.veredicto]
