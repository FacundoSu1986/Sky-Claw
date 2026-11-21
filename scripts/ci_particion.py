"""Reparto determinista de la suite de pytest en shards para CI, y verificación de que no se perdió ningún test.

Repartir una suite entre varios jobs tiene un modo de falla silencioso: que algún test no corra en NINGÚN shard (un
shard de menos en la matriz, un archivo que cada shard asigna distinto, una colección que cambia entre jobs). Ningún job
falla y el verde miente. Por eso este módulo tiene dos mitades que se prueban juntas:

* un **plugin de pytest** (``-p scripts.ci_particion``) que reparte **por archivo** usando la colección propia de
  pytest, no una lista de archivos mantenida a mano (``tests/`` tiene subdirectorios, y una lista los omitiría sin
  avisar). Se activa con ``SKYCLAW_SHARD=<indice>/<total>``; sin la variable no hace nada;
* un **verificador** (``python -m scripts.ci_particion verificar``) que lee el manifiesto que cada shard deja y exige
  que, entre todos, hayan seleccionado exactamente la suite: misma huella (recalculada con los archivos que de verdad
  declararon), mismo total, sin archivos repetidos y con la suma de tests igual al total
  colectado. Con ``--con-cobertura`` exige además la base de ``coverage`` de cada shard, presente y legible.

El peso de un archivo para repartir es su **duración medida**, tomada de la calibración versionada
(``scripts/ci_duraciones.json``) y escalada por cuántos tests tiene hoy; un archivo sin medir se estima con el promedio
por test de la calibración. Sin calibración el peso es la cantidad de tests (el criterio anterior). El conteo era un mal
proxy: medido en main, los 4 shards por conteo tardaron 574/509/905/628 s de tests y el más lento decide cuándo termina
el check. La huella, el manifiesto y el verificador siguen trabajando con el CONTEO: el criterio de reparto no cambia
qué se verifica.

Cada manifiesto registra cuánto tardaron los tests de cada archivo (suma de sus fases setup/call/teardown, SIN la
colección ni el arranque del job); ``python -m scripts.ci_particion calibrar`` arma la calibración con esos datos. No es
la duración del job: para decidir un ``timeout-minutes`` manda la duración del job entero que muestra Actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ENV_SHARD = "SKYCLAW_SHARD"
ENV_MANIFIESTO = "SKYCLAW_SHARD_MANIFEST"
#: Nombre propio y NO ``manifest.json``: los tests (y el producto) escriben ese nombre en muchos árboles temporales
#: (p. ej. ``.skyclaw-output/manifest.json``) y una búsqueda por nombre genérico los recogería como si fueran de un shard.
NOMBRE_DEL_MANIFIESTO = "shard-manifest.json"
#: Base de datos de ``coverage`` que cada shard deja junto a su manifiesto (``COVERAGE_FILE`` del workflow).
NOMBRE_DE_LA_COBERTURA = "coverage.sqlite"
VERSION_DEL_MANIFIESTO = 1

#: Ruta de una calibración alternativa. Sin la variable se usa la versionada; si se define y el archivo no existe, la
#: sesión ABORTA (pedir una calibración que no está es un error de uso, no un permiso para repartir por conteo).
ENV_DURACIONES = "SKYCLAW_SHARD_DURACIONES"
#: Calibración versionada: duración medida de los tests de cada archivo. Se regenera con ``calibrar``.
RUTA_DE_LA_CALIBRACION = Path(__file__).with_name("ci_duraciones.json")
VERSION_DE_LA_CALIBRACION = 1

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


@dataclass(frozen=True, slots=True)
class Medicion:
    """Lo que se midió de un archivo de tests: cuánto tardaron sus tests y cuántos eran en esa corrida."""

    segundos: float
    tests: int


def pesos_de_reparto(conteos: Mapping[str, int], calibracion: Mapping[str, Medicion] | None) -> dict[str, int]:
    """El peso con que se reparte cada archivo: su duración estimada en milisegundos (entero >= 1).

    * archivo calibrado: su duración medida, escalada por ``conteo_actual / tests_medidos`` (un archivo que ganó tests no
      se queda con la duración vieja);
    * archivo sin calibrar: ``conteo_actual`` por el promedio de milisegundos por test de toda la calibración;
    * sin calibración (``None`` o vacía): el conteo tal cual, que es el reparto de antes.

    Es determinista: depende solo de ``conteos`` y de ``calibracion``, que todos los shards leen iguales.
    """
    if not calibracion:
        return dict(conteos)
    ms_por_test = 1000 * sum(m.segundos for m in calibracion.values()) / sum(m.tests for m in calibracion.values())
    pesos: dict[str, int] = {}
    for nombre, conteo in conteos.items():
        medicion = calibracion.get(nombre)
        estimado = 1000 * medicion.segundos * conteo / medicion.tests if medicion is not None else ms_por_test * conteo
        pesos[nombre] = max(1, round(estimado))
    return pesos


def _es_segundos(valor: object) -> bool:
    """Un número finito y no negativo: ``bool`` no es un tiempo y ``NaN``/``Infinity`` (que ``json`` acepta) tampoco."""
    return isinstance(valor, int | float) and not isinstance(valor, bool) and math.isfinite(valor) and valor >= 0


def _validar_calibracion(segundos: object, tests: object) -> dict[str, Medicion]:
    """Arma la calibración desde los dos mapas, o lanza ``ValueError`` con el primer problema encontrado."""
    if not isinstance(segundos, dict) or not isinstance(tests, dict):
        raise ValueError("calibración inválida: 'segundos_por_archivo' y 'tests_por_archivo' deben ser objetos")
    if set(segundos) != set(tests):
        distintos = sorted(set(segundos) ^ set(tests))
        raise ValueError(f"calibración inválida: los dos mapas no tienen los mismos archivos ({distintos[:5]})")
    if not segundos:
        raise ValueError("calibración inválida: no tiene ningún archivo")
    malos_segundos = sorted(str(n) for n, v in segundos.items() if not _es_segundos(v))
    if malos_segundos:
        raise ValueError(f"calibración inválida: segundos que no son un número finito >= 0: {malos_segundos[:5]}")
    malos_tests = sorted(str(n) for n, v in tests.items() if not _es_entero(v) or v < 1)
    if malos_tests:
        raise ValueError(f"calibración inválida: cantidades de tests que no son un entero >= 1: {malos_tests[:5]}")
    calibracion = {str(n): Medicion(segundos=float(segundos[n]), tests=int(tests[n])) for n in segundos}
    if sum(m.segundos for m in calibracion.values()) <= 0:
        raise ValueError("calibración inválida: sin tiempos medidos (todo en 0 s, ¿una corrida con --collect-only?)")
    return calibracion


def cargar_calibracion(ruta: Path) -> dict[str, Medicion] | None:
    """Lee una calibración; ``None`` si el archivo no existe. Una calibración mal formada lanza ``ValueError``.

    Mal formada es fail-closed a propósito: el plugin la convierte en un error de uso que aborta el shard. Repartir por
    conteo en silencio con una calibración rota no rompería la verificación, pero escondería que el balance se perdió.
    """
    try:
        texto = ruta.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{ruta}: no se pudo leer la calibración ({exc})") from exc
    try:
        contenido = json.loads(texto)
    except ValueError as exc:
        raise ValueError(f"{ruta}: la calibración no es JSON válido ({exc})") from exc
    if not isinstance(contenido, dict):
        raise ValueError(f"{ruta}: la calibración debe ser un objeto JSON")
    version = contenido.get("version")
    if not _es_entero(version) or version != VERSION_DE_LA_CALIBRACION:
        raise ValueError(
            f"{ruta}: calibración con versión {version!r} no soportada (se espera {VERSION_DE_LA_CALIBRACION})"
        )
    try:
        return _validar_calibracion(contenido.get("segundos_por_archivo"), contenido.get("tests_por_archivo"))
    except ValueError as exc:
        raise ValueError(f"{ruta}: {exc}") from exc


def escribir_calibracion(calibracion: Mapping[str, Medicion], ruta: Path) -> None:
    """Escribe la calibración ordenada por archivo (un diff por archivo que cambió, no un bloque reordenado).

    ``newline="\\n"``: los mismos bytes en Windows y en Linux. ``.gitattributes`` solo fija el fin de línea de ``*.py``, y
    sin esto regenerarla en Windows cambiaría cada línea del archivo de trabajo.
    """
    contenido = {
        "version": VERSION_DE_LA_CALIBRACION,
        "segundos_por_archivo": {n: round(calibracion[n].segundos, 3) for n in sorted(calibracion)},
        "tests_por_archivo": {n: calibracion[n].tests for n in sorted(calibracion)},
    }
    ruta.write_text(json.dumps(contenido, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def huella_de_suite(pesos: Mapping[str, int]) -> str:
    """Identifica la suite colectada (archivo y cantidad de tests), sin importar el orden: dos shards que vieron la
    misma suite tienen la misma huella.

    Es por archivo y conteo, A PROPÓSITO, y no sobre los node IDs completos: algunos IDs reales no son deterministas
    entre procesos (medido en ``tests/``: 2 de 20296, un ``parametrize`` con ``object()`` cuyo ID lleva la dirección de
    memoria y otro con ``uuid1()``). Una huella de IDs haría rojo el veredicto en toda corrida, y no agrega protección: el
    shard dueño de un archivo corre una colección completa de ese archivo, y si colectó MENOS casos, el conteo ya lo marca.
    """
    contenido = "\n".join(f"{nombre}\t{pesos[nombre]}" for nombre in sorted(pesos))
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


def construir_manifiesto(
    shard: Shard,
    pesos: Mapping[str, int],
    propios: Collection[str],
    duraciones: Mapping[str, float],
    codigo_de_salida: int = 0,
) -> dict[str, Any]:
    """Lo que un shard declara haber seleccionado, para que el verificador lo contraste con los demás.

    ``duraciones`` es el tiempo de las fases de los tests de cada archivo (no incluye colección ni arranque del job).
    ``codigo_de_salida`` es el de la sesión de pytest: con ``-x`` un shard rojo declara su selección COMPLETA aunque no
    haya corrido todos sus archivos, así que ``calibrar`` solo acepta shards que terminaron en 0.
    """
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
        "tiempo_de_tests_por_archivo_s": por_archivo,
        "tiempo_de_tests_s": round(sum(por_archivo.values()), 3),
        "codigo_de_salida": codigo_de_salida,
    }


# ---------------------------------------------------------------------------
# Plugin de pytest
# ---------------------------------------------------------------------------


def _archivo_de(nodeid: str) -> str:
    """El archivo de un test (``tests/test_x.py::Clase::test``): relativo a la raíz y con ``/`` en cualquier SO."""
    return nodeid.split("::", 1)[0]


class _Particion:
    """Deja solo los tests de los archivos que le tocan a este shard y registra qué hizo."""

    def __init__(
        self, shard: Shard, ruta_manifiesto: Path | None, calibracion: Mapping[str, Medicion] | None = None
    ) -> None:
        self._shard = shard
        self._ruta_manifiesto = ruta_manifiesto
        self._calibracion = calibracion
        #: Conteo de tests por archivo: es lo que entra a la huella y al manifiesto (el verificador trabaja con conteos).
        self._pesos: dict[str, int] = {}
        self._propios: frozenset[str] = frozenset()
        self._reparto_aplicado = False
        self._sin_calibrar = 0
        self._duraciones: defaultdict[str, float] = defaultdict(float)

    def pytest_report_header(self) -> str:
        criterio = (
            f"duración medida ({len(self._calibracion)} archivos calibrados)"
            if self._calibracion
            else "conteo de tests (sin calibración)"
        )
        return f"skyclaw-particion: shard {self._shard.indice}/{self._shard.total}, reparto por {criterio}"

    def pytest_report_collectionfinish(self) -> str | None:
        """Cuántos archivos se repartieron con un peso estimado: una calibración vieja se nota en el log del shard."""
        if not self._calibracion or not self._reparto_aplicado:
            return None
        return (
            f"skyclaw-particion: {self._sin_calibrar} de {len(self._pesos)} archivos colectados sin calibrar "
            "(peso estimado)"
        )

    # trylast: otros filtros (-k, -m) ya actuaron, y se reparte lo que queda: todos los shards ven lo mismo.
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self._pesos = dict(Counter(_archivo_de(item.nodeid) for item in items))
        self._sin_calibrar = sum(1 for nombre in self._pesos if nombre not in (self._calibracion or {}))
        para_repartir = pesos_de_reparto(self._pesos, self._calibracion)
        propios = repartir(para_repartir, self._shard.total)[self._shard.indice - 1]
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
        manifiesto = construir_manifiesto(
            self._shard, self._pesos, self._propios, self._duraciones, codigo_de_salida=int(exitstatus)
        )
        self._ruta_manifiesto.parent.mkdir(parents=True, exist_ok=True)
        self._ruta_manifiesto.write_text(json.dumps(manifiesto, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    """Activa el reparto si ``SKYCLAW_SHARD`` está definida; un valor mal escrito ABORTA (nunca corre todo en silencio)."""
    valor = os.environ.get(ENV_SHARD, "")
    if not valor:
        return
    try:
        shard = parsear_shard(valor)
        calibracion = _calibracion_de_la_sesion()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    ruta = os.environ.get(ENV_MANIFIESTO)
    config.pluginmanager.register(_Particion(shard, Path(ruta) if ruta else None, calibracion), "skyclaw-particion")


def _calibracion_de_la_sesion() -> dict[str, Medicion] | None:
    """La calibración que pide ``SKYCLAW_SHARD_DURACIONES`` (obligatoria si se define) o, sin ella, la versionada."""
    explicita = os.environ.get(ENV_DURACIONES, "")
    if not explicita:
        return cargar_calibracion(RUTA_DE_LA_CALIBRACION)
    calibracion = cargar_calibracion(Path(explicita))
    if calibracion is None:
        raise ValueError(f"{ENV_DURACIONES}={explicita!r}: el archivo de calibración no existe")
    return calibracion


# ---------------------------------------------------------------------------
# Verificador
# ---------------------------------------------------------------------------


def _rutas_de_manifiestos(directorio: Path) -> list[Path]:
    """Un manifiesto por artefacto, a UN solo nivel (``<directorio>/<artefacto>/shard-manifest.json``): es como
    ``download-artifact`` deja cada artefacto, y evita recoger archivos de un árbol temporal de tests que casualmente
    esté bajo el mismo directorio."""
    return sorted(directorio.glob(f"*/{NOMBRE_DEL_MANIFIESTO}"))


def _es_entero(valor: object) -> bool:
    """``int`` de verdad: ``bool`` es subclase de ``int`` y ``True`` no es un índice ni un conteo."""
    return isinstance(valor, int) and not isinstance(valor, bool)


def _tiene_el_tipo(valor: object, tipo: type) -> bool:
    return _es_entero(valor) if tipo is int else isinstance(valor, tipo)


def _conteos_invalidos(archivos: Mapping[Any, Any]) -> list[str]:
    """Los archivos cuyo conteo no es un entero >= 1: ``sum()`` sobre ellos revienta (texto) o miente (``True``, 0, <0)."""
    return sorted(str(nombre) for nombre, conteo in archivos.items() if not _es_entero(conteo) or conteo < 1)


def _cargar_manifiestos(directorio: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Lee el manifiesto de cada artefacto de shard; devuelve los bien formados y los errores del resto.

    Un manifiesto mal formado (campo ausente, tipo incorrecto, conteo inválido) se REPORTA como error con su ruta: nunca
    llega a las sumas del verificador, donde reventaría con un ``TypeError`` que no dice cuál archivo está roto.
    """
    manifiestos: list[dict[str, Any]] = []
    errores: list[str] = []
    for ruta in _rutas_de_manifiestos(directorio):
        try:
            contenido = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errores.append(f"{ruta}: no se pudo leer {NOMBRE_DEL_MANIFIESTO} ({exc})")
            continue
        faltantes = [
            campo
            for campo, tipo in _CAMPOS_REQUERIDOS.items()
            if not isinstance(contenido, dict) or not _tiene_el_tipo(contenido.get(campo), tipo)
        ]
        if faltantes:
            errores.append(f"{ruta}: {NOMBRE_DEL_MANIFIESTO} sin los campos esperados o mal tipados: {faltantes}")
            continue
        invalidos = _conteos_invalidos(contenido["archivos"])
        if invalidos:
            errores.append(
                f"{ruta}: {NOMBRE_DEL_MANIFIESTO} con conteos inválidos en 'archivos' "
                f"(se exige un entero >= 1 por archivo): {invalidos}"
            )
            continue
        manifiestos.append(contenido)
    return manifiestos, errores


def _base_legible(base: Path) -> bool:
    """¿La lee Coverage.py (el MISMO lector que usa ``coverage combine``) y trae al menos un archivo medido?

    ``PRAGMA quick_check`` de SQLite no alcanza: una base íntegra pero sin la tabla ``file`` da ``ok`` y ``coverage combine``
    la omite con una advertencia y sale con 0 (reproducido con Coverage.py 7.13.5). Y una base SQLite ajena la lee como una
    base nueva y vacía. Todo shard real mide al menos ``sky_claw`` o ``scripts``, así que cero archivos es un shard sin datos.

    El import es perezoso: solo lo necesita ``--con-cobertura``, y así los shards (que cargan este módulo como plugin) no
    importan ``coverage`` por su cuenta.
    """
    from coverage import CoverageData
    from coverage.exceptions import CoverageException

    try:
        datos = CoverageData(basename=str(base))
        datos.read()
        return bool(datos.measured_files())
    except (CoverageException, sqlite3.Error, OSError):
        return False


def _problemas_de_las_bases_de_cobertura(directorio: Path) -> list[str]:
    """Un error por cada shard (carpeta con manifiesto) cuya base de ``coverage`` falta, está vacía o no se puede leer.

    ``coverage combine`` sobre un glob al que le falta una base NO falla, y una base que no puede leer la OMITE con una
    advertencia y exit 0 (``--strict`` solo exige que haya algún dato): si las restantes alcanzan el umbral, el job queda
    verde y publica una cobertura que no incluye a ese shard.
    """
    errores = []
    for ruta in _rutas_de_manifiestos(directorio):
        base = ruta.parent / NOMBRE_DE_LA_COBERTURA
        try:
            hay_datos = base.is_file() and base.stat().st_size > 0
        except OSError:
            hay_datos = False
        if not hay_datos:
            errores.append(
                f"{ruta.parent.name}: falta {NOMBRE_DE_LA_COBERTURA} (o está vacío): "
                "la cobertura combinada quedaría sin los datos de ese shard"
            )
        elif not _base_legible(base):
            errores.append(
                f"{ruta.parent.name}: {NOMBRE_DE_LA_COBERTURA} ilegible para Coverage.py (truncada, corrupta, sin su esquema "
                "o sin archivos medidos): `coverage combine` la omitiría con una advertencia y la cobertura quedaría sin los "
                "datos de ese shard"
            )
    return errores


def verificar(directorio: Path, total: int, *, con_cobertura: bool = False) -> list[str]:
    """Contrasta los manifiestos de todos los shards; devuelve la lista de problemas (vacía si la suite está completa).

    Con ``con_cobertura`` exige además la base de ``coverage`` de cada shard, presente y legible
    (ver ``_problemas_de_las_bases_de_cobertura``).
    """
    if total < 1:
        # Con ``total=0`` y ningún manifiesto, «0 esperados y 0 hallados» cuadraba y devolvía OK sin verificar nada.
        return [f"total de shards inválido: {total} (debe ser >= 1)"]
    manifiestos, errores = _cargar_manifiestos(directorio)
    cantidad = len(manifiestos) + len(errores)
    if cantidad != total:
        errores.append(f"se esperaban {total} manifiestos y hay {cantidad} en {directorio}")
    if con_cobertura:
        errores.extend(_problemas_de_las_bases_de_cobertura(directorio))
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

    # La huella declarada es un dato de cada shard; lo que de verdad seleccionaron es la unión de sus ``archivos``. Si no
    # coinciden, algún shard declara un archivo que no es el de la suite colectada (p. ej. otro del mismo peso: el conteo
    # local, el total y los duplicados siguen cuadrando).
    if len(huellas) == 1:
        (declarada,) = huellas
        seleccionados_por_archivo = {archivo: peso for m in manifiestos for archivo, peso in m["archivos"].items()}
        recalculada = huella_de_suite(seleccionados_por_archivo)
        if recalculada != declarada:
            errores.append(
                f"la huella declarada por los shards ({declarada[:12]}) no corresponde a los archivos que seleccionaron "
                f"entre todos (recalculada: {recalculada[:12]}): algún shard declara un archivo distinto del de la suite"
            )
    return errores


def _resumen(directorio: Path) -> list[str]:
    """Líneas legibles para el log del job: cuánto tardó cada shard (insumo para calibrar el reparto)."""
    manifiestos, _ = _cargar_manifiestos(directorio)
    lineas = []
    for m in sorted(manifiestos, key=lambda x: x["shard"]):
        duracion = m.get("tiempo_de_tests_s")
        tiempo = f"{duracion:.0f} s de tests" if isinstance(duracion, int | float) else "sin dato de tiempo"
        lineas.append(
            f"  shard {m['shard']}/{m['total_shards']}: {m['seleccionados']} tests en {len(m['archivos'])} archivos, {tiempo}"
        )
    return lineas


def calibracion_desde_manifiestos(directorio: Path, total: int) -> tuple[dict[str, Medicion], list[str]]:
    """La calibración que dejaron los shards de UNA corrida completa, o los problemas que impiden armarla.

    Exige primero lo mismo que el veredicto (``verificar``): calibrar con un shard de menos dejaría sin medición a sus
    archivos sin que nada lo diga. Cada archivo seleccionado tiene que traer su tiempo.
    """
    errores = verificar(directorio, total)
    if errores:
        return {}, errores
    manifiestos, _ = _cargar_manifiestos(directorio)
    segundos: dict[str, object] = {}
    tests: dict[str, object] = {}
    for m in manifiestos:
        codigo = m.get("codigo_de_salida")
        if not _es_entero(codigo) or codigo != 0:
            errores.append(
                f"el shard {m['shard']} no terminó en verde (codigo_de_salida={codigo!r}): un shard cortado con -x o "
                "--maxfail declara su selección completa con 0 s en lo que no corrió; solo se calibra desde una corrida verde"
            )
            continue
        tiempos = m.get("tiempo_de_tests_por_archivo_s")
        if not isinstance(tiempos, dict):
            errores.append(f"el manifiesto del shard {m['shard']} no trae 'tiempo_de_tests_por_archivo_s'")
            continue
        sin_tiempo = sorted(set(m["archivos"]) - set(tiempos))
        if sin_tiempo:
            errores.append(f"el manifiesto del shard {m['shard']} no trae el tiempo de: {sin_tiempo}")
        for nombre, conteo in m["archivos"].items():
            if nombre in tiempos:
                segundos[nombre] = tiempos[nombre]
                tests[nombre] = conteo
    if errores:
        return {}, errores
    try:
        return _validar_calibracion(segundos, tests), []
    except ValueError as exc:
        return {}, [str(exc)]


def _entero_positivo(texto: str) -> int:
    """``type=`` de argparse: un ``--total`` no positivo (o no entero) es un error de uso, no un caso a verificar."""
    try:
        valor = int(texto)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{texto!r} no es un entero") from exc
    if valor < 1:
        raise argparse.ArgumentTypeError(f"debe ser >= 1 (se recibió {valor})")
    return valor


def _calibrar(directorio: Path, total: int, salida: Path) -> int:
    calibracion, errores = calibracion_desde_manifiestos(directorio, total)
    if errores:
        for error in errores:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    escribir_calibracion(calibracion, salida)
    total_s = sum(m.segundos for m in calibracion.values())
    print(f"OK: calibración de {len(calibracion)} archivos ({total_s:.0f} s de tests) escrita en {salida}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_particion", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="orden", required=True)
    verif = sub.add_parser("verificar", help="exige que los manifiestos de los shards cubran la suite exactamente")
    verif.add_argument("directorio", type=Path, help="carpeta con los artefactos de los shards")
    verif.add_argument("--total", type=_entero_positivo, required=True, help="cantidad de shards esperada (>= 1)")
    verif.add_argument(
        "--con-cobertura",
        action="store_true",
        help=f"exige además la base {NOMBRE_DE_LA_COBERTURA} de coverage en la carpeta de cada shard",
    )
    calib = sub.add_parser(
        "calibrar", help="escribe la calibración de duraciones con los manifiestos de una corrida completa"
    )
    calib.add_argument("directorio", type=Path, help="carpeta con los artefactos de los shards (una versión de Python)")
    calib.add_argument("--total", type=_entero_positivo, required=True, help="cantidad de shards de esa corrida (>= 1)")
    calib.add_argument(
        "--salida",
        type=Path,
        default=RUTA_DE_LA_CALIBRACION,
        help=f"destino (por defecto {RUTA_DE_LA_CALIBRACION.name})",
    )
    args = parser.parse_args(argv)

    if args.orden == "calibrar":
        return _calibrar(args.directorio, args.total, args.salida)

    errores = verificar(args.directorio, args.total, con_cobertura=args.con_cobertura)
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
