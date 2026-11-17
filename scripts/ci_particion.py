"""Reparto determinista de la suite de pytest en shards para CI, y verificación de que no se perdió ningún test.

Repartir una suite entre varios jobs tiene un modo de falla silencioso: que algún test no corra en NINGÚN shard (un
shard de menos en la matriz, un archivo que cada shard asigna distinto, una colección que cambia entre jobs). Ningún job
falla y el verde miente. Por eso este módulo tiene dos mitades que se prueban juntas:

* un **plugin de pytest** (``-p scripts.ci_particion``) que reparte **por archivo** usando la colección propia de
  pytest, no una lista de archivos mantenida a mano (``tests/`` tiene subdirectorios, y una lista los omitiría sin
  avisar). Se activa con ``SKYCLAW_SHARD=<indice>/<total>``; sin la variable no hace nada;
* un **verificador** (``python -m scripts.ci_particion verificar``) que lee el manifiesto que cada shard deja y exige
  que, entre todos, hayan seleccionado exactamente la suite: misma huella, mismo total, sin archivos repetidos y con la
  suma de tests igual al total colectado.

El peso de un archivo es su cantidad de tests: es un proxy de su duración, no la duración. Cada manifiesto registra
también cuánto tardó cada archivo, para calibrar el reparto con datos reales en vez de adivinar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ENV_SHARD = "SKYCLAW_SHARD"
ENV_MANIFIESTO = "SKYCLAW_SHARD_MANIFEST"
NOMBRE_DEL_MANIFIESTO = "manifest.json"
VERSION_DEL_MANIFIESTO = 1

_FORMA_SHARD = re.compile(r"[0-9]+/[0-9]+")

#: Campos que todo manifiesto debe traer, con el tipo que se exige de cada uno.
_CAMPOS_REQUERIDOS: dict[str, type] = {
    "version": int,
    "shard": int,
    "total_shards": int,
    "total_colectado": int,
    "seleccionados": int,
    "huella": str,
    "archivos": dict,
}


@dataclass(frozen=True, slots=True)
class Shard:
    """Qué parte de la suite corre este proceso: ``indice`` de ``total`` (ambos desde 1)."""

    indice: int
    total: int


def parsear_shard(texto: str) -> Shard:
    """Interpreta ``<indice>/<total>`` de forma estricta (sin espacios, signos ni decimales)."""
    if not _FORMA_SHARD.fullmatch(texto):
        raise ValueError(f"{ENV_SHARD}={texto!r} no tiene la forma <indice>/<total> (por ejemplo 2/4)")
    indice_txt, total_txt = texto.split("/")
    indice, total = int(indice_txt), int(total_txt)
    if total < 1 or not 1 <= indice <= total:
        raise ValueError(f"{ENV_SHARD}={texto!r}: el total debe ser >= 1 y el índice estar entre 1 y {total}")
    return Shard(indice, total)


def repartir(pesos: Mapping[str, int], total: int) -> tuple[frozenset[str], ...]:
    """Reparte archivos entre ``total`` shards: primero los más pesados, cada uno al shard menos cargado.

    Es determinista y no depende del orden de ``pesos``: ordena por (peso descendente, nombre) y desempata el shard
    por índice, así que dos procesos que colectan en distinto orden llegan a la misma asignación. La carga máxima no
    supera el promedio más el peso del archivo más grande.
    """
    if total < 1:
        raise ValueError(f"total de shards inválido: {total} (debe ser >= 1)")
    for nombre, peso in pesos.items():
        if peso < 1:
            raise ValueError(f"peso inválido para {nombre!r}: {peso} (debe ser >= 1)")

    cargas = [0] * total
    grupos: list[set[str]] = [set() for _ in range(total)]
    for nombre in sorted(pesos, key=lambda n: (-pesos[n], n)):
        destino = min(range(total), key=lambda i: (cargas[i], i))
        grupos[destino].add(nombre)
        cargas[destino] += pesos[nombre]
    return tuple(frozenset(g) for g in grupos)


def huella_de_suite(pesos: Mapping[str, int]) -> str:
    """Identifica la suite colectada (archivo y cantidad de tests), sin importar el orden: dos shards que vieron la
    misma suite tienen la misma huella."""
    contenido = "\n".join(f"{nombre}\t{pesos[nombre]}" for nombre in sorted(pesos))
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


def construir_manifiesto(
    shard: Shard,
    pesos: Mapping[str, int],
    propios: Collection[str],
    duraciones: Mapping[str, float],
) -> dict[str, Any]:
    """Lo que un shard declara haber seleccionado, para que el verificador lo contraste con los demás."""
    archivos = {nombre: pesos[nombre] for nombre in sorted(propios)}
    por_archivo = {nombre: round(duraciones.get(nombre, 0.0), 3) for nombre in archivos}
    return {
        "version": VERSION_DEL_MANIFIESTO,
        "shard": shard.indice,
        "total_shards": shard.total,
        "total_colectado": sum(pesos.values()),
        "seleccionados": sum(archivos.values()),
        "huella": huella_de_suite(pesos),
        "archivos": archivos,
        "duraciones_s": por_archivo,
        "duracion_total_s": round(sum(por_archivo.values()), 3),
    }


# ---------------------------------------------------------------------------
# Plugin de pytest
# ---------------------------------------------------------------------------


def _archivo_de(nodeid: str) -> str:
    """El archivo de un test (``tests/test_x.py::Clase::test``): relativo a la raíz y con ``/`` en cualquier SO."""
    return nodeid.split("::", 1)[0]


class _Particion:
    """Deja solo los tests de los archivos que le tocan a este shard y registra qué hizo."""

    def __init__(self, shard: Shard, ruta_manifiesto: Path | None) -> None:
        self._shard = shard
        self._ruta_manifiesto = ruta_manifiesto
        self._pesos: dict[str, int] = {}
        self._propios: frozenset[str] = frozenset()
        self._reparto_aplicado = False
        self._duraciones: defaultdict[str, float] = defaultdict(float)

    def pytest_report_header(self) -> str:
        return f"skyclaw-particion: shard {self._shard.indice}/{self._shard.total}"

    # trylast: otros filtros (-k, -m) ya actuaron, y se reparte lo que queda: todos los shards ven lo mismo.
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self._pesos = dict(Counter(_archivo_de(item.nodeid) for item in items))
        propios = repartir(self._pesos, self._shard.total)[self._shard.indice - 1]
        conservados = [item for item in items if _archivo_de(item.nodeid) in propios]
        descartados = [item for item in items if _archivo_de(item.nodeid) not in propios]
        if descartados:
            config.hook.pytest_deselected(items=descartados)
        items[:] = conservados
        self._propios = propios
        self._reparto_aplicado = True

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._duraciones[_archivo_de(report.nodeid)] += report.duration

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG002
        # Sin reparto aplicado (p. ej. la colección abortó) no hay nada fiable que declarar: el job ya falló.
        if self._ruta_manifiesto is None or not self._reparto_aplicado:
            return
        manifiesto = construir_manifiesto(self._shard, self._pesos, self._propios, self._duraciones)
        self._ruta_manifiesto.parent.mkdir(parents=True, exist_ok=True)
        self._ruta_manifiesto.write_text(json.dumps(manifiesto, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    """Activa el reparto si ``SKYCLAW_SHARD`` está definida; un valor mal escrito ABORTA (nunca corre todo en silencio)."""
    valor = os.environ.get(ENV_SHARD, "")
    if not valor:
        return
    try:
        shard = parsear_shard(valor)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    ruta = os.environ.get(ENV_MANIFIESTO)
    config.pluginmanager.register(_Particion(shard, Path(ruta) if ruta else None), "skyclaw-particion")


# ---------------------------------------------------------------------------
# Verificador
# ---------------------------------------------------------------------------


def _cargar_manifiestos(directorio: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Lee todos los ``manifest.json`` bajo ``directorio``; devuelve los bien formados y los errores del resto."""
    manifiestos: list[dict[str, Any]] = []
    errores: list[str] = []
    for ruta in sorted(directorio.rglob(NOMBRE_DEL_MANIFIESTO)):
        try:
            contenido = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errores.append(f"{ruta}: no se pudo leer {NOMBRE_DEL_MANIFIESTO} ({exc})")
            continue
        faltantes = [
            campo
            for campo, tipo in _CAMPOS_REQUERIDOS.items()
            if not isinstance(contenido, dict) or not isinstance(contenido.get(campo), tipo)
        ]
        if faltantes:
            errores.append(f"{ruta}: {NOMBRE_DEL_MANIFIESTO} sin los campos esperados o mal tipados: {faltantes}")
            continue
        manifiestos.append(contenido)
    return manifiestos, errores


def verificar(directorio: Path, total: int) -> list[str]:
    """Contrasta los manifiestos de todos los shards; devuelve la lista de problemas (vacía si la suite está completa)."""
    manifiestos, errores = _cargar_manifiestos(directorio)
    cantidad = len(manifiestos) + len(errores)
    if cantidad != total:
        errores.append(f"se esperaban {total} manifiestos y hay {cantidad} en {directorio}")
    if not manifiestos:
        return errores

    for m in manifiestos:
        if m["version"] != VERSION_DEL_MANIFIESTO:
            errores.append(
                f"manifiesto del shard {m['shard']} con versión {m['version']} no soportada (se espera {VERSION_DEL_MANIFIESTO})"
            )

    indices = sorted(m["shard"] for m in manifiestos)
    if indices != list(range(1, total + 1)):
        errores.append(f"los índices de shard son {indices} y deberían ser 1..{total}")

    for m in manifiestos:
        if m["total_shards"] != total:
            errores.append(
                f"el manifiesto del shard {m['shard']} declara total_shards={m['total_shards']}, se esperaba {total}"
            )
        suma_propia = sum(m["archivos"].values())
        if m["seleccionados"] != suma_propia:
            errores.append(
                f"el shard {m['shard']} declara {m['seleccionados']} tests seleccionados pero sus archivos suman {suma_propia}"
            )

    huellas = {m["huella"] for m in manifiestos}
    if len(huellas) > 1:
        errores.append(f"los shards no vieron la misma suite (huellas distintas: {sorted(h[:12] for h in huellas)})")

    totales = {m["total_colectado"] for m in manifiestos}
    if len(totales) > 1:
        errores.append(f"los shards no colectaron el mismo total de tests ({sorted(totales)})")
    else:
        (total_colectado,) = totales
        seleccionados = sum(m["seleccionados"] for m in manifiestos)
        if seleccionados != total_colectado:
            errores.append(
                f"la suma de tests seleccionados ({seleccionados}) no coincide con el total colectado ({total_colectado}): "
                "hay tests sin correr o repetidos"
            )

    dueños: defaultdict[str, list[int]] = defaultdict(list)
    for m in manifiestos:
        for archivo in m["archivos"]:
            dueños[archivo].append(m["shard"])
    errores.extend(
        f"el archivo {archivo} figura en más de un shard ({sorted(shards)})"
        for archivo, shards in sorted(dueños.items())
        if len(shards) > 1
    )
    return errores


def _resumen(directorio: Path) -> list[str]:
    """Líneas legibles para el log del job: cuánto tardó cada shard (insumo para calibrar el reparto)."""
    manifiestos, _ = _cargar_manifiestos(directorio)
    lineas = []
    for m in sorted(manifiestos, key=lambda x: x["shard"]):
        duracion = m.get("duracion_total_s")
        tiempo = f"{duracion:.0f} s" if isinstance(duracion, int | float) else "sin dato"
        lineas.append(
            f"  shard {m['shard']}/{m['total_shards']}: {m['seleccionados']} tests en {len(m['archivos'])} archivos, {tiempo}"
        )
    return lineas


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_particion", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="orden", required=True)
    verif = sub.add_parser("verificar", help="exige que los manifiestos de los shards cubran la suite exactamente")
    verif.add_argument("directorio", type=Path, help="carpeta con los artefactos de los shards")
    verif.add_argument("--total", type=int, required=True, help="cantidad de shards esperada")
    args = parser.parse_args(argv)

    errores = verificar(args.directorio, args.total)
    if errores:
        for error in errores:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    manifiestos, _ = _cargar_manifiestos(args.directorio)
    print(f"OK: {args.total} shards, {manifiestos[0]['total_colectado']} tests, huella {manifiestos[0]['huella'][:12]}")
    print("\n".join(_resumen(args.directorio)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
