"""H05: validación de ``ScrapingQuery`` sin bloquear el loop ni el executor.

El ``field_validator`` de ``ScrapingQuery.url`` llama a ``validate_url_ssrf`` →
``socket.getaddrinfo`` de forma síncrona. La corrección C-min concede UNA sola
validación H05 en vuelo, en un hilo daemon, y rechaza de inmediato cualquier
otra. No hay cola, no se usa el executor por defecto y la capacidad se libera
cuando termina el trabajo, no cuando se cancela el ``await``.

Los tests usan resolvers simulados y sincronización determinista; no hacen DNS
real ni tocan credenciales o rigs.
"""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

import sky_claw.app.core.validators.ssrf as ssrf_mod
import sky_claw.app.orchestrator.tool_strategies.query_mod_metadata as qmm
from sky_claw.app.core.schemas import ScrapingQuery
from sky_claw.app.orchestrator.tool_dispatcher import OrchestrationToolDispatcher
from sky_claw.app.orchestrator.tool_strategies.query_mod_metadata import QueryModMetadataStrategy

IP_PUBLICA = "93.184.216.34"
IP_METADATA = "169.254.169.254"
RAIZ = Path(__file__).resolve().parents[1]


def _respuesta_dns(ip: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def _estrategia() -> tuple[QueryModMetadataStrategy, MagicMock]:
    """Estrategia con un scraper doble; el resultado expone ``model_dump``."""
    scraper = MagicMock()
    resultado = MagicMock()
    resultado.model_dump.return_value = {"mod_id": 42, "name": "TestMod"}
    scraper.query_nexus = AsyncMock(return_value=resultado)
    return QueryModMetadataStrategy(scraper=scraper), scraper


def _senalar(futuro: asyncio.Future[bool], valor: bool = True) -> None:
    """Completa un future desde otro hilo sin InvalidStateError."""
    if not futuro.done():
        futuro.set_result(valor)


# --- Inventario acotado de consumidores productivos -------------------------

_CLASE = "ScrapingQuery"
_METODOS_DE_CONSTRUCCION = {
    "model_validate",
    "model_validate_json",
    "model_validate_strings",
    "model_construct",
    "parse_obj",
}
# Funciones que reciben la clase para comprobar tipos, no para construirla.
_FUNCIONES_DE_TIPO = {"isinstance", "issubclass", "type", "cast", "get_type_hints"}


def _alias_de(arbol: ast.AST) -> tuple[set[str], set[str]]:
    """Nombres que pueden referirse a la clase, incluidos aliases y reexports."""
    clases = {_CLASE}
    modulos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                if alias.name == _CLASE:
                    clases.add(alias.asname or alias.name)
                elif (
                    alias.name == "core"
                    or alias.name.endswith(".core")
                    or (alias.name == "schemas" and (nodo.module or "").endswith("core"))
                ):
                    modulos.add(alias.asname or alias.name)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                nombre = alias.name
                if nombre.endswith("core.schemas") or nombre.endswith(".core") or nombre.endswith("core"):
                    modulos.add(alias.asname or nombre.split(".")[-1])
    return clases, modulos


def _segmentos(nodo: ast.AST) -> list[str]:
    if isinstance(nodo, ast.Name):
        return [nodo.id]
    if isinstance(nodo, ast.Attribute):
        return _segmentos(nodo.value) + [nodo.attr]
    return []


def _nombre_de_llamada(func: ast.AST) -> str | None:
    segmentos = _segmentos(func)
    return segmentos[-1] if segmentos else None


def _es_referencia_a_clase(nodo: ast.AST, clases: set[str], modulos: set[str]) -> bool:
    if isinstance(nodo, ast.Name):
        return nodo.id in clases
    if isinstance(nodo, ast.Attribute):
        if nodo.attr in clases and any(parte in modulos for parte in _segmentos(nodo.value)):
            return True
        if nodo.attr in _METODOS_DE_CONSTRUCCION:
            return _es_referencia_a_clase(nodo.value, clases, modulos)
    return False


def sitios_de_construccion(arbol: ast.AST, ruta: str = "<sintetico>") -> set[tuple[str, int, int]]:
    """Enumera sitios estáticos de construcción dentro de ``ast.Call``.

    Alcance declarado: código Python productivo y formas estáticas:
    constructor directo, reexport calificado, alias de importación,
    ``model_validate*``/``model_construct`` y la clase pasada como argumento.
    No afirma cubrir reflexión dinámica, ``globals()`` ni construcciones
    indirectas. Anotaciones y comprobaciones de tipo no cuentan.
    """
    clases, modulos = _alias_de(arbol)
    sitios: set[tuple[str, int, int]] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        if _nombre_de_llamada(nodo.func) in _FUNCIONES_DE_TIPO:
            continue
        objetivos = [nodo.func, *nodo.args, *(kw.value for kw in nodo.keywords)]
        if any(_es_referencia_a_clase(obj, clases, modulos) for obj in objetivos):
            sitios.add((ruta, nodo.lineno, nodo.col_offset))
    return sitios


# --- Contrato de éxito, SSRF y errores de validación ------------------------


async def test_execute_con_url_valida_entrega_scraping_query_normalizada(monkeypatch: pytest.MonkeyPatch) -> None:
    hostnames: list[str] = []

    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        hostnames.append(hostname)
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()

    salida = await estrategia.execute({"query": "cool mod", "url": "https://Example.com/path"})

    assert hostnames == ["example.com"]
    scraper.query_nexus.assert_awaited_once()
    query = scraper.query_nexus.await_args.args[0]
    assert isinstance(query, ScrapingQuery)
    assert query.url == "https://example.com/path"
    assert query.query == "cool mod"
    assert salida == {"mod_id": 42, "name": "TestMod"}


async def test_execute_con_ip_privada_falla_cerrado_y_no_llama_al_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        return _respuesta_dns(IP_METADATA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()

    with pytest.raises(ValidationError):
        await estrategia.execute({"query": "cool mod", "url": "https://meta.example.com/"})

    scraper.query_nexus.assert_not_awaited()


async def test_execute_con_resolucion_dns_vacia_falla_cerrado(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        return []

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()

    with pytest.raises(ValidationError):
        await estrategia.execute({"query": "cool mod", "url": "https://sin-ip.example.com/"})

    scraper.query_nexus.assert_not_awaited()


async def test_execute_sin_url_no_resuelve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        raise AssertionError("no se debe resolver DNS cuando el payload no trae URL")

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()

    salida = await estrategia.execute({"query": "cool mod"})

    scraper.query_nexus.assert_awaited_once()
    assert scraper.query_nexus.await_args.args[0].url is None
    assert salida == {"mod_id": 42, "name": "TestMod"}


async def test_execute_con_payload_invalido_propaga_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        raise AssertionError("un payload inválido no debe resolver DNS")

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()

    with pytest.raises(ValidationError):
        await estrategia.execute({"query": ""})

    scraper.query_nexus.assert_not_awaited()


async def test_execute_con_dns_bloqueante_deja_progresar_el_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    liberar = threading.Event()
    callback_ejecutado = threading.Event()
    progreso_al_soltar: list[bool] = []
    loop = asyncio.get_running_loop()

    def desbloquear_desde_el_loop() -> None:
        callback_ejecutado.set()
        liberar.set()

    def resolver_bloqueante(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        loop.call_soon_threadsafe(desbloquear_desde_el_loop)
        liberar.wait(timeout=2.0)
        progreso_al_soltar.append(callback_ejecutado.is_set())
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver_bloqueante)
    estrategia, scraper = _estrategia()

    await asyncio.wait_for(
        estrategia.execute({"query": "cool mod", "url": "https://example.com/"}),
        timeout=5.0,
    )

    assert progreso_al_soltar == [True]
    scraper.query_nexus.assert_awaited_once()


# --- C-min: executor compartido, admisión y cancelación ---------------------


def test_h05_no_ocupa_el_executor_y_la_resolucion_del_loop_progresa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con un único worker del executor, ``loop.getaddrinfo`` debe progresar.

    ``SafeResolver`` usa exactamente ``loop.getaddrinfo``; si H05 ocupa el
    executor por defecto, esa resolución queda esperando detrás del DNS
    bloqueante de H05. El hilo de H05 es daemon y ajeno al executor.
    """

    async def escenario() -> None:
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h05-probe")
        loop.set_default_executor(executor)
        liberar = threading.Event()
        empezo: asyncio.Future[bool] = loop.create_future()

        def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
            if hostname == "bloqueada.example.com":
                loop.call_soon_threadsafe(_senalar, empezo)
                liberar.wait(timeout=3.0)
            return _respuesta_dns(IP_PUBLICA)

        monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
        estrategia, scraper = _estrategia()
        tarea = asyncio.create_task(estrategia.execute({"query": "cool mod", "url": "https://bloqueada.example.com/"}))
        try:
            assert await asyncio.wait_for(empezo, timeout=2.0) is True
            infos = await asyncio.wait_for(loop.getaddrinfo("probe.example.com", 0), timeout=2.0)
            assert infos[0][4][0] == IP_PUBLICA
        finally:
            liberar.set()
            await asyncio.wait_for(tarea, timeout=5.0)
            executor.shutdown(wait=True, cancel_futures=True)

        scraper.query_nexus.assert_awaited_once()

    asyncio.run(escenario())


async def test_segunda_solicitud_se_rechaza_de_inmediato_y_por_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La capacidad agotada usa el dict de error canónico del dispatcher."""
    loop = asyncio.get_running_loop()
    liberar = threading.Event()
    empezo: asyncio.Future[bool] = loop.create_future()

    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        loop.call_soon_threadsafe(_senalar, empezo)
        liberar.wait(timeout=3.0)
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    estrategia, scraper = _estrategia()
    dispatcher = OrchestrationToolDispatcher()
    dispatcher.register(estrategia)

    primera = asyncio.create_task(
        dispatcher.dispatch("query_mod_metadata", {"query": "uno", "url": "https://uno.example.com/"})
    )
    try:
        assert await asyncio.wait_for(empezo, timeout=2.0) is True
        rechazo = await asyncio.wait_for(
            dispatcher.dispatch("query_mod_metadata", {"query": "dos", "url": "https://dos.example.com/"}),
            timeout=1.0,
        )
        assert rechazo["status"] == "error"
        assert rechazo["reason"] == "CapacityExhausted"
        assert "details" in rechazo
        scraper.query_nexus.assert_not_awaited()
    finally:
        liberar.set()
        assert (await asyncio.wait_for(primera, timeout=5.0))["mod_id"] == 42

    scraper.query_nexus.assert_awaited_once()


async def test_instancias_distintas_no_evaden_la_capacidad_unica(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    liberar = threading.Event()
    empezo: asyncio.Future[bool] = loop.create_future()

    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        loop.call_soon_threadsafe(_senalar, empezo)
        liberar.wait(timeout=3.0)
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    primera, scraper_primera = _estrategia()
    segunda, scraper_segunda = _estrategia()

    tarea = asyncio.create_task(primera.execute({"query": "uno", "url": "https://uno.example.com/"}))
    try:
        assert await asyncio.wait_for(empezo, timeout=2.0) is True
        rechazo = await segunda.execute({"query": "dos", "url": "https://dos.example.com/"})
        assert rechazo["status"] == "error"
        assert rechazo["reason"] == "CapacityExhausted"
        scraper_primera.query_nexus.assert_not_awaited()
        scraper_segunda.query_nexus.assert_not_awaited()
    finally:
        liberar.set()
        await asyncio.wait_for(tarea, timeout=5.0)


async def test_cancelacion_no_libera_capacidad_y_no_acumula_hilos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    liberar = threading.Event()
    empezo: asyncio.Future[bool] = loop.create_future()
    liberada: asyncio.Future[bool] = loop.create_future()
    hilos_iniciados: list[Any] = []

    class HiloContado(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)

        def start(self) -> None:
            hilos_iniciados.append(self)
            super().start()

    liberar_original = qmm._liberar_capacidad

    def liberar_y_senalar() -> None:
        liberar_original()
        loop.call_soon_threadsafe(_senalar, liberada)

    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        loop.call_soon_threadsafe(_senalar, empezo)
        liberar.wait(timeout=3.0)
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(qmm, "_Thread", HiloContado)
    monkeypatch.setattr(qmm, "_liberar_capacidad", liberar_y_senalar)
    estrategia, scraper = _estrategia()

    tarea = asyncio.create_task(estrategia.execute({"query": "uno", "url": "https://uno.example.com/"}))
    try:
        assert await asyncio.wait_for(empezo, timeout=2.0) is True
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        assert not liberada.done(), "la cancelación liberó la capacidad antes de que terminara el worker"
        assert len(hilos_iniciados) == 1
        rechazo = await estrategia.execute({"query": "dos", "url": "https://dos.example.com/"})
        assert rechazo["reason"] == "CapacityExhausted"
        assert len(hilos_iniciados) == 1, "se inició un segundo worker con el anterior todavía en vuelo"
        scraper.query_nexus.assert_not_awaited()
    finally:
        liberar.set()
        assert await asyncio.wait_for(liberada, timeout=3.0) is True

    assert (await asyncio.wait_for(estrategia.execute({"query": "tres"}), timeout=5.0)) == {
        "mod_id": 42,
        "name": "TestMod",
    }
    assert len(hilos_iniciados) == 2
    assert scraper.query_nexus.await_count == 1


async def test_capacidad_se_recupera_tras_excepcion_y_fallo_de_inicio(monkeypatch: pytest.MonkeyPatch) -> None:
    class HiloQueFallaAlArrancar:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("no se pudo iniciar el hilo")

    def resolver(hostname: str, puerto: object = None, *args: object, **kwargs: object) -> list[tuple]:
        return _respuesta_dns(IP_PUBLICA)

    monkeypatch.setattr(ssrf_mod.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(qmm, "_Thread", HiloQueFallaAlArrancar)
    estrategia, scraper = _estrategia()

    with pytest.raises(RuntimeError, match="no se pudo iniciar el hilo"):
        await estrategia.execute({"query": "fallo de arranque"})

    monkeypatch.setattr(qmm, "_Thread", threading.Thread)
    salida = await asyncio.wait_for(
        estrategia.execute({"query": "cool mod", "url": "https://example.com/"}), timeout=5.0
    )
    assert salida == {"mod_id": 42, "name": "TestMod"}
    scraper.query_nexus.assert_awaited_once()


def test_subprocess_terminala_con_worker_daemon_bloqueado() -> None:
    """El proceso puede cerrarse aunque el worker daemon siga bloqueado."""
    codigo = r"""
import asyncio
import socket
import sys
import threading

import sky_claw
import sky_claw.app.core.validators.ssrf as ssrf
from sky_claw.app.orchestrator.tool_strategies.query_mod_metadata import QueryModMetadataStrategy

assert sky_claw.__file__.startswith(sys.argv[1]), sky_claw.__file__

def resolver(hostname, puerto=None, *args, **kwargs):
    if not empezo.done():
        loop.call_soon_threadsafe(empezo.set_result, True)
    threading.Event().wait(30.0)
    return []

ssrf.socket.getaddrinfo = resolver
scraper = type("S", (), {"query_nexus": None})()

async def principal():
    global empezo, loop
    loop = asyncio.get_running_loop()
    empezo = loop.create_future()
    tarea = asyncio.create_task(
        QueryModMetadataStrategy(scraper=scraper).execute(
            {"query": "cool mod", "url": "https://bloqueada.example.com/"}
        )
    )
    await asyncio.wait_for(empezo, timeout=2.0)
    tarea.cancel()
    try:
        await tarea
    except asyncio.CancelledError:
        pass
    print("CHILD_OK")

asyncio.run(principal())
"""
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(RAIZ)
    comando = [sys.executable, "-P", "-c", codigo, str(RAIZ)]
    resultado = subprocess.run(comando, cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=8.0)
    assert resultado.returncode == 0, resultado.stderr
    assert "CHILD_OK" in resultado.stdout
    assert "Exception in thread" not in resultado.stderr, resultado.stderr


# --- Detector acotado y pruebas del propio detector -------------------------


@pytest.mark.parametrize(
    "escenario,codigo,esperado",
    [
        (
            "constructor directo",
            "from sky_claw.app.core.schemas import ScrapingQuery\nq = ScrapingQuery(**payload)\n",
            1,
        ),
        (
            "constructor calificado de schemas",
            "import sky_claw.app.core.schemas as schemas\nq = schemas.ScrapingQuery(**payload)\n",
            1,
        ),
        (
            "reexport import sky_claw.app.core as core",
            "import sky_claw.app.core as core\nq = core.ScrapingQuery(**payload)\n",
            1,
        ),
        (
            "reexport from sky_claw.app import core",
            "from sky_claw.app import core\nq = core.ScrapingQuery(**payload)\n",
            1,
        ),
        ("alias de importación", "from sky_claw.app.core.schemas import ScrapingQuery as SQ\nq = SQ(**payload)\n", 1),
        (
            "método model_validate",
            "from sky_claw.app.core.schemas import ScrapingQuery\nq = ScrapingQuery.model_validate(payload)\n",
            1,
        ),
        (
            "método model_construct",
            "from sky_claw.app.core.schemas import ScrapingQuery\nq = ScrapingQuery.model_construct(**payload)\n",
            1,
        ),
        (
            "pasada a to_thread",
            "from sky_claw.app.core.schemas import ScrapingQuery\nq = asyncio.to_thread(ScrapingQuery, **payload)\n",
            1,
        ),
        (
            "sólo anotaciones de tipo",
            "from sky_claw.app.core.schemas import ScrapingQuery\ndef f(q: ScrapingQuery) -> ScrapingQuery:\n    return q\n",
            0,
        ),
        ("nombre dentro de un string", "logger.info('ScrapingQuery')\n", 0),
        ("clase distinta", "class ScrapingQueryable: pass\nx = ScrapingQueryable()\n", 0),
        (
            "isinstance no es construcción",
            "from sky_claw.app.core.schemas import ScrapingQuery\nes = isinstance(query, ScrapingQuery)\n",
            0,
        ),
    ],
)
def test_detector_con_arboles_sinteticos_cubre_formas_y_evita_falsos_positivos(
    escenario: str, codigo: str, esperado: int
) -> None:
    sitios = sitios_de_construccion(ast.parse(codigo))
    assert len(sitios) == esperado, escenario


def test_detector_distingue_dos_constructores_en_el_mismo_archivo() -> None:
    codigo = "from sky_claw.app.core.schemas import ScrapingQuery\na = ScrapingQuery(**uno)\nb = ScrapingQuery(**dos)\n"
    sitios = sitios_de_construccion(ast.parse(codigo), "otro.py")
    assert len(sitios) == 2
    assert len({(linea, columna) for _, linea, columna in sitios}) == 2


def test_inventario_de_consumidores_productivos_no_elude_el_contrato_h05() -> None:
    """Inventario acotado a Python productivo; no cubre reflexión dinámica.

    Toda construcción estática de ``ScrapingQuery`` en ``sky_claw/`` debe estar
    en el único worker H05. Una nueva entrada productiva rompe el ancla hasta
    que se escriba su receta, en lugar de eludir la validación fuera del hilo.
    """
    sitios: set[tuple[str, int, int]] = set()
    for archivo in (RAIZ / "sky_claw").rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        sitios |= sitios_de_construccion(arbol, archivo.relative_to(RAIZ).as_posix())

    assert {ruta for ruta, _, _ in sitios} == {"sky_claw/app/orchestrator/tool_strategies/query_mod_metadata.py"}
    assert len(sitios) == 1
