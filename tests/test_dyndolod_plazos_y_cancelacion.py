"""Un plazo no puede tragarse la cancelación de quien detiene la corrida (causa del flake de ``test_dyndolod_progreso.py``).

El mecanismo está demostrado por instrumentación y por intervención, no supuesto:

- En Python 3.11 ``asyncio.wait_for`` envuelve el awaitable en una tarea interna. Si la cancelación de quien espera llega en
  la ventana en la que el interno YA terminó pero el plazo todavía no resumió, ``wait_for`` devuelve el resultado y la
  cancelación se pierde. 3.12 lo reimplementó sobre ``asyncio.timeout`` (se espera en línea, sin tarea interna: no hay
  ventana), así que el defecto es de 3.11, que el proyecto soporta (``requires-python >= 3.11``).
- En el latido del runner eso deja a la tarea viva (``cancelling() == 1`` y detenida en su ``sleep``) y
  ``_limpiar_helpers_en_salida_normal`` espera «para siempre» en ``gather(heartbeat)``. Bajo carga: 48 cuelgues en 480
  corridas; con ``asyncio.timeout`` en ese único sitio, 0 en 480.

Lo que estos tests anclan es la PROPIEDAD y no el síntoma: **una cancelación aceptada por una tarea viva nunca se pierde.**
No se compara contra una versión de Python: en 3.12 pasan trivialmente y en 3.11 son los que fallan si un sitio vuelve a
envolver su plazo con ``wait_for``.

La ventana depende de cuántas vueltas del bucle pasan entre «el interno terminó» y «el plazo resume», y cuántas son
depende del seam. Se ENUMERAN las fases en vez de muestrear una (el repo ya ancla así: ``assert RITUAL_TOOL_MAP == {...}``).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import types
from collections.abc import Callable

import pytest

import sky_claw.local.tools.dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from tests.test_dyndolod_progreso import _runner

#: Vueltas del bucle entre «el awaitable interno terminó» y «la cancelación llega». La carrera de ``wait_for`` en 3.11 vive
#: en una de ellas; cuál, depende del seam, y las que no son esa solo prueban el caso común (la cancelación propaga).
_FASES = range(6)

#: Una tarea ya DENTRO del envoltorio de plazo con su awaitable interno detenido en una puerta, y cómo abrir esa puerta.
_Armado = tuple["asyncio.Task[object]", Callable[[], None]]


async def _la_cancelacion_aceptada_no_se_pierde(armado: _Armado, fases: int) -> None:
    tarea, liberar = armado
    liberar()
    for _ in range(fases):
        await asyncio.sleep(0)
    # ``cancel()`` devuelve True solo si la tarea seguía viva: esa es la cancelación que NO se puede perder. Si la tarea ya
    # había terminado (el interno y el plazo resumieron antes), no hay nada que cancelar y volver normal es correcto.
    aceptada = tarea.cancel()
    try:
        async with asyncio.timeout(5):
            await tarea
    except asyncio.CancelledError:
        return
    assert not aceptada, (
        "cancel() fue aceptado por una tarea viva y la tarea terminó con normalidad: el plazo se tragó la cancelación"
    )


# ---------------------------------------------------------------------------
# Seams: cada uno deja una tarea DENTRO de su plazo, con el awaitable interno esperando en una puerta
# ---------------------------------------------------------------------------


async def _armar_entrega_de_progreso(tmp_path: pathlib.Path) -> _Armado:
    """La entrega del latido (``_reportar_progreso``): el sitio que cuelga la limpieza si pierde la cancelación."""
    adentro, puerta = asyncio.Event(), asyncio.Event()

    async def aviso(_progreso: object) -> None:
        adentro.set()
        await puerta.wait()

    runner = _runner(tmp_path, on_progress=aviso)
    tarea = asyncio.create_task(runner._reportar_progreso("TexGen", 1.0))  # noqa: SLF001 -- el seam bajo prueba ES privado
    async with asyncio.timeout(5):
        await adentro.wait()
    return tarea, puerta.set


class _ConfirmadorQueEspera:
    """El canal humano: el aviso y la confirmación quedan esperando hasta que se abre la puerta."""

    def __init__(self) -> None:
        self.adentro = asyncio.Event()
        self.puerta = asyncio.Event()

    async def confirmar(self, _solicitud: object) -> ResultadoConfirmacion:
        self.adentro.set()
        await self.puerta.wait()
        return ResultadoConfirmacion.APROBADA

    async def informar(self, *, tool: str, mensaje: str) -> None:
        del tool, mensaje
        self.adentro.set()
        await self.puerta.wait()


def _con_confirmador(runner: ddl.DynDOLODRunner, monkeypatch: pytest.MonkeyPatch, confirmador: object) -> None:
    capacidad = types.SimpleNamespace(confirmador=confirmador, gracia_externa_segundos=5.0)
    monkeypatch.setattr(runner, "_capacidad_de_readiness", lambda: capacidad)


async def _armar_confirmacion_acotada(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> _Armado:
    confirmador = _ConfirmadorQueEspera()
    runner = _runner(tmp_path)
    _con_confirmador(runner, monkeypatch, confirmador)
    solicitud = types.SimpleNamespace(timeout_seconds=30.0)
    tarea = asyncio.create_task(runner._confirmar_acotado(solicitud))  # type: ignore[arg-type]  # noqa: SLF001
    async with asyncio.timeout(5):
        await confirmador.adentro.wait()
    return tarea, confirmador.puerta.set


async def _armar_aviso_al_operador(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> _Armado:
    confirmador = _ConfirmadorQueEspera()
    runner = _runner(tmp_path)
    _con_confirmador(runner, monkeypatch, confirmador)
    tarea = asyncio.create_task(runner._informar_operador(tool_name="TexGen", mensaje="listo"))  # noqa: SLF001
    async with asyncio.timeout(5):
        await confirmador.adentro.wait()
    return tarea, confirmador.puerta.set


async def _armar_limpieza_de_helpers() -> _Armado:
    """La gracia del drenaje en la limpieza de salida normal: la cancelación del caller cruza ese plazo."""
    puerta = asyncio.Event()

    async def _drenar() -> None:
        await puerta.wait()

    heartbeat = asyncio.create_task(asyncio.sleep(3600))
    drain_out = asyncio.create_task(_drenar())
    drain_err = asyncio.create_task(_drenar())
    tarea = asyncio.create_task(
        ddl._limpiar_helpers_en_salida_normal("TexGen", heartbeat, drain_out, drain_err)  # noqa: SLF001
    )
    async with asyncio.timeout(5):
        while not heartbeat.done():  # la limpieza canceló al latido y pasó a esperar los drains
            await asyncio.sleep(0)
    for _ in range(3):
        await asyncio.sleep(0)
    return tarea, puerta.set


# ---------------------------------------------------------------------------
# La propiedad, sitio por sitio y fase por fase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fases", _FASES)
async def test_la_entrega_del_progreso_no_se_traga_la_cancelacion(tmp_path: pathlib.Path, fases: int) -> None:
    await _la_cancelacion_aceptada_no_se_pierde(await _armar_entrega_de_progreso(tmp_path), fases)


@pytest.mark.parametrize("fases", _FASES)
async def test_la_confirmacion_acotada_no_se_traga_la_cancelacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fases: int
) -> None:
    await _la_cancelacion_aceptada_no_se_pierde(await _armar_confirmacion_acotada(tmp_path, monkeypatch), fases)


@pytest.mark.parametrize("fases", _FASES)
async def test_el_aviso_al_operador_no_se_traga_la_cancelacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fases: int
) -> None:
    await _la_cancelacion_aceptada_no_se_pierde(await _armar_aviso_al_operador(tmp_path, monkeypatch), fases)


@pytest.mark.parametrize("fases", _FASES)
async def test_la_gracia_del_drenaje_no_se_traga_la_cancelacion(fases: int) -> None:
    await _la_cancelacion_aceptada_no_se_pierde(await _armar_limpieza_de_helpers(), fases)


# ---------------------------------------------------------------------------
# El helper: el contrato de ``wait_for`` sin su defecto
# ---------------------------------------------------------------------------


async def test_el_helper_devuelve_el_resultado_del_awaitable() -> None:
    async def _valor() -> int:
        return 7

    assert await ddl._esperar_con_plazo(_valor(), timeout=5) == 7  # noqa: SLF001


async def test_el_helper_levanta_timeout_y_cancela_al_awaitable() -> None:
    cancelado = asyncio.Event()

    async def _eterno() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelado.set()
            raise

    with pytest.raises(TimeoutError):
        await ddl._esperar_con_plazo(_eterno(), timeout=0.01)  # noqa: SLF001

    assert cancelado.is_set(), "al vencer el plazo el awaitable queda cancelado, no huérfano"


async def test_el_helper_sin_plazo_espera_sin_cota() -> None:
    async def _tarda() -> str:
        await asyncio.sleep(0.02)
        return "ok"

    assert await ddl._esperar_con_plazo(_tarda(), timeout=None) == "ok"  # noqa: SLF001


@pytest.mark.parametrize("fases", _FASES)
async def test_el_helper_no_se_traga_la_cancelacion(fases: int) -> None:
    puerta = asyncio.Event()

    async def _espera() -> None:
        await puerta.wait()

    tarea = asyncio.create_task(ddl._esperar_con_plazo(_espera(), timeout=30))  # noqa: SLF001
    for _ in range(2):  # que la tarea ya esté DENTRO del plazo, esperando en la puerta
        await asyncio.sleep(0)

    await _la_cancelacion_aceptada_no_se_pierde((tarea, puerta.set), fases)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# El ancla: el runner no puede volver a envolver un plazo con ``asyncio.wait_for``
# ---------------------------------------------------------------------------


def _referencias_a_wait_for(arbol: ast.AST) -> list[int]:
    """Líneas de toda referencia a ``wait_for``, tenga o no una llamada: es el hermano que un revisor sí encuentra.

    Se compara el NOMBRE FINAL del atributo y no la forma del import (lección del ancla de B-4 en #956): cubre
    ``asyncio.wait_for``, ``aio.wait_for`` (``import asyncio as aio``), ``ddl.asyncio.wait_for``, el import
    ``from asyncio import wait_for`` —con o sin alias— y sus usos, y una referencia que no se llama
    (``partial(asyncio.wait_for, ...)``). Lo que NO ve —``getattr(asyncio, "wait_for")``— no es una forma que el repo use.
    """
    nombres = {"wait_for"}
    lineas: list[int] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module in {"asyncio", "asyncio.tasks"}:
            for alias in nodo.names:
                if alias.name == "wait_for":
                    nombres.add(alias.asname or alias.name)
                    lineas.append(nodo.lineno)
    for nodo in ast.walk(arbol):
        # ``nombres`` ya incluye ``wait_for`` y cada alias importado: un solo criterio para los usos por nombre.
        if (isinstance(nodo, ast.Attribute) and nodo.attr == "wait_for") or (
            isinstance(nodo, ast.Name) and nodo.id in nombres
        ):
            lineas.append(nodo.lineno)
    return sorted(lineas)


@pytest.mark.parametrize(
    ("fuente", "esperadas"),
    [
        ("import asyncio\nawait asyncio.wait_for(x, 1)", 1),
        ("import asyncio as aio\nawait aio.wait_for(x, 1)", 1),
        ("await ddl.asyncio.wait_for(x, 1)", 1),
        ("import asyncio\nf = asyncio.wait_for", 1),
        ("from asyncio import wait_for\nawait wait_for(x, 1)", 2),
        ("from asyncio import wait_for as esperar\nawait esperar(x, 1)", 2),
        ("await _esperar_con_plazo(x, timeout=1)", 0),
        ("import asyncio\nasync with asyncio.timeout(1):\n    await x", 0),
        ("obj.wait_for_release()", 0),
        ("texto = 'asyncio.wait_for'  # asyncio.wait_for(x, 1)", 0),
    ],
    ids=[
        "asyncio_directo",
        "alias_de_modulo",
        "ruta_anidada",
        "referencia_sin_llamar",
        "import_directo",
        "import_con_alias",
        "el_helper",
        "asyncio_timeout",
        "nombre_parecido",
        "texto_y_comentario",
    ],
)
def test_el_detector_de_wait_for_no_depende_del_estilo_de_import(fuente: str, esperadas: int) -> None:
    assert len(_referencias_a_wait_for(ast.parse(fuente))) == esperadas


def _arbol_del_runner() -> ast.Module:
    return ast.parse(pathlib.Path(ddl.__file__).read_text(encoding="utf-8"))


def test_el_runner_no_usa_asyncio_wait_for() -> None:
    """Todo plazo del runner pasa por ``_esperar_con_plazo``: es la pieza que no se traga la cancelación externa.

    Cada ``wait_for`` que vuelva es un sitio donde, en 3.11, un shutdown puede perderse; en el latido eso cuelga la limpieza.
    Los siete que había se enumeraron por AST (no de memoria) y se movieron juntos: arreglar uno y dejar al hermano es el
    defecto dominante de este repo.
    """
    assert _referencias_a_wait_for(_arbol_del_runner()) == [], (
        "``asyncio.wait_for`` volvió al runner: usar ``_esperar_con_plazo``"
    )


def test_el_helper_de_plazos_espera_con_asyncio_timeout() -> None:
    """La propiedad vive en una sola pieza: si el helper dejara de usar ``asyncio.timeout``, los siete sitios la pierden."""
    (helper,) = [
        n
        for n in ast.walk(_arbol_del_runner())
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_esperar_con_plazo"
    ]
    usa_timeout = any(
        isinstance(n, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "timeout"
            for item in n.items
        )
        for n in ast.walk(helper)
    )
    assert usa_timeout, "``_esperar_con_plazo`` debe esperar dentro de ``async with asyncio.timeout(...)``"
