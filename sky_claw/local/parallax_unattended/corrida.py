"""Punto de entrada de la corrida desatendida: la ÚNICA forma de decidir si un lote sirve (#802).

La secuencia ``dictar -> auditar -> combinar -> codigo_de_salida`` no es intercambiable: saltearse
``combinar`` deja un lote ``PASS`` con artefactos planos, que es el defecto que esta capa existe para cerrar.
Sin un punto de entrada, cada integrador la rearmaría por su cuenta. Acá vive UNA vez, y
``tests/test_parallax_unattended_corrida.py`` ancla por AST que nadie fuera de la capa la recompone.

La lectura del JSON va en el BORDE (este módulo), no dentro de ``dictar`` ni de ``auditar``: esas funciones siguen
recibiendo un ``Mapping`` ya cargado y no abren nada. Este módulo solo LEE (regla 3 de ``AGENTS.md``): ni archivos,
ni logs, ni caché. La salida de ``main`` va a los flujos que recibe, nunca a disco.

Fail-closed (regla 5): un informe que no se puede leer o que no es un objeto JSON sin ambigüedad es
``INCONSISTENTE`` con ``FORMA_INVALIDA``, el mismo veredicto que ``dictar`` da a un informe de forma inválida. Nunca
una excepción, nunca ``PASS``. Se rechaza lo que ``json`` aceptaría por tolerancia pero que el generador no emite:
claves repetidas (la última ganaría en silencio), ``NaN``/``Infinity`` y una marca BOM.

Códigos de salida de ``python -m sky_claw.local.parallax_unattended <informe> <raiz_de_salida>``: los de
``CODIGOS_DE_SALIDA`` (``0`` solo con ``PASS``) y ``CODIGO_DE_USO_INCORRECTO`` para los argumentos mal pasados, que no
se confunde con ningún veredicto (``argparse`` sale con 2 por defecto, y 2 es ``INCONSISTENTE``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Final, NoReturn, TextIO

from .auditor import auditar
from .composicion import combinar
from .veredicto import (
    DecisionDelLote,
    InformeInvalidoError,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
    dictar,
)

__all__ = ["CODIGO_DE_USO_INCORRECTO", "decidir_corrida", "leer_informe", "main"]

#: Argumentos mal pasados (``EX_USAGE`` de sysexits). Distinto de todo valor de ``CODIGOS_DE_SALIDA``.
CODIGO_DE_USO_INCORRECTO: Final[int] = 64

_PROG: Final[str] = "python -m sky_claw.local.parallax_unattended"


def _sin_claves_repetidas(pares: list[tuple[str, object]]) -> dict[str, object]:
    claves = [clave for clave, _ in pares]
    if len(set(claves)) != len(claves):
        repetidas = sorted({clave for clave in claves if claves.count(clave) > 1})
        raise InformeInvalidoError(f"el informe repite claves: {repetidas}")
    return dict(pares)


def _rechazar_constante(nombre: str) -> NoReturn:
    raise InformeInvalidoError(f"el informe contiene la constante no estándar {nombre}")


def leer_informe(ruta: pathlib.Path) -> dict[str, object]:
    """Carga el informe del generador. Solo lee. Lanza ``InformeInvalidoError`` ante cualquier duda."""

    try:
        crudo = ruta.read_bytes()
    except OSError as exc:
        raise InformeInvalidoError(f"no se pudo leer el informe {ruta}: {exc.strerror or exc}") from exc
    try:
        informe = json.loads(
            crudo.decode("utf-8"),
            object_pairs_hook=_sin_claves_repetidas,
            parse_constant=_rechazar_constante,
        )
    except InformeInvalidoError as exc:
        raise InformeInvalidoError(f"informe ilegible ({ruta}): {exc}") from exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InformeInvalidoError(f"informe ilegible ({ruta}): {exc}") from exc
    if not isinstance(informe, dict):
        raise InformeInvalidoError(f"informe ilegible ({ruta}): debe ser un objeto JSON, no {type(informe).__name__}")
    return informe


def decidir_corrida(ruta_informe: pathlib.Path, raiz_de_salida: pathlib.Path) -> DecisionDelLote:
    """Veredicto final de una corrida: el del informe, degradado por lo que el disco diga. Nunca lanza.

    ``ruta_informe`` es el ``informe_parallax.json`` del generador y ``raiz_de_salida`` la carpeta donde dejó los
    ``_p.dds``. Son dos rutas explícitas: el generador prohíbe que el informe viva DENTRO de la salida.
    """

    try:
        informe = leer_informe(ruta_informe)
    except InformeInvalidoError as exc:
        return DecisionDelLote(
            veredicto=VeredictoDelLote.INCONSISTENTE,
            motivos=(MotivoDelLote.FORMA_INVALIDA,),
            rutas=(),
            conteos={},
            detalle=str(exc),
        )
    decision = dictar(informe)
    auditoria = auditar(informe, raiz_de_salida)
    return combinar(decision, auditoria)


def _seguro(texto: str) -> str:
    """Escapa lo no imprimible: rutas y detalle vienen de un archivo no confiable y van a la terminal del operador."""

    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in texto)


def _formatear(decision: DecisionDelLote) -> str:
    lineas = [f"veredicto: {decision.veredicto.value}"]
    if decision.motivos:
        lineas.append("motivos: " + ", ".join(motivo.value for motivo in decision.motivos))
    lineas.extend(f"  ruta: {_seguro(ruta)}" for ruta in decision.rutas)
    if decision.detalle:
        lineas.append(f"detalle: {_seguro(decision.detalle)}")
    if decision.conteos:
        lineas.append("conteos: " + ", ".join(f"{_seguro(k)}={v}" for k, v in sorted(decision.conteos.items())))
    return "\n".join(lineas) + "\n"


def _emitir(flujo: TextIO, texto: str) -> None:
    """Escribe sin que un problema del flujo cambie el código de salida: el CÓDIGO es el contrato, no el texto."""

    try:
        flujo.write(texto)
    except UnicodeEncodeError:
        # Consola cp1252/ascii: se reintenta con escapes en vez de convertir el veredicto en un traceback.
        flujo.write(texto.encode("ascii", "backslashreplace").decode("ascii"))
    except OSError:
        # Salida cerrada (p. ej. tubería rota): no hay a quién avisar, y el código de salida sigue siendo el veredicto.
        return
    with contextlib.suppress(OSError):
        flujo.flush()


class _UsoIncorrectoError(Exception):
    """Argumentos mal pasados. Lleva el texto de ayuda que ``argparse`` habría impreso antes de salir."""


class _ParserDeCorrida(argparse.ArgumentParser):
    """``argparse`` sin ``sys.exit``: ``--help`` y los errores no pueden salir con 0 ni con 2 (que es un veredicto)."""

    def error(self, message: str) -> NoReturn:
        raise _UsoIncorrectoError(f"{self.format_help()}\n{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    # Sin ``-h``: un ``--help`` que saliera 0 pasaría por un PASS en una tubería mal configurada.
    parser = _ParserDeCorrida(
        prog=_PROG,
        description="Decide si una corrida de parallax sirve: cruza el informe del generador con los _p.dds en disco.",
        add_help=False,
    )
    parser.add_argument("informe", type=pathlib.Path, help="informe_parallax.json que escribió el generador")
    parser.add_argument("raiz_de_salida", type=pathlib.Path, help="carpeta donde el generador dejó los _p.dds")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    salida: TextIO | None = None,
    error: TextIO | None = None,
) -> int:
    """Decide la corrida, imprime el veredicto y devuelve su código de salida (``0`` solo con ``PASS``)."""

    flujo_de_salida = sys.stdout if salida is None else salida
    flujo_de_error = sys.stderr if error is None else error
    try:
        argumentos = _parser().parse_args(argv)
    except _UsoIncorrectoError as exc:
        _emitir(flujo_de_error, str(exc))
        return CODIGO_DE_USO_INCORRECTO
    decision = decidir_corrida(argumentos.informe, argumentos.raiz_de_salida)
    _emitir(flujo_de_salida, _formatear(decision))
    return codigo_de_salida(decision)
