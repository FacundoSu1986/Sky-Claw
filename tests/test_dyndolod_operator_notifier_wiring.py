"""Wiring del notificador de operador de DynDOLOD en el bootloader de la GUI.

El notificador y su adaptador Telegram tienen tests propios
(``test_dyndolod_operator_notifier.py``, ``test_telegram_operator_channel.py``). Lo
que ninguno de los dos puede decir es si el proceso REAL los instala: un módulo
perfecto que nadie suscribe es exactamente el hueco que la auditoría de la etapa 9
midió (``pipeline.dyndolod.*`` se publicaba y no lo escuchaba nadie).

Propiedades que este archivo fija:

* Instalar suscribe el notificador al bus del supervisor y rastrea su worker como
  tarea de fondo del ``AppContext`` (el shutdown la cancela).
* ``sender`` y chat se leen del ``AppContext`` EN CADA envío: el orden real es
  ``start_full`` -> bootloader, pero un ``AppContext`` que republique ``sender`` no
  puede dejar al notificador hablándole al viejo.
* **Quién escucha es una propiedad del mecanismo, no de un call site.** Hoy la
  etapa 9 sólo se alcanza desde el supervisor que arma el bootloader de la GUI. Si
  aparece un segundo sitio de producción que construya ``SupervisorAgent`` (otro
  modo, un headless), este archivo se rompe hasta que se le cablee el notificador
  o se declare la exención: la regla del hermano sin cablear (ver ``AGENTS.md``).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.core.event_payloads import DynDOLODPipelineCompletedPayload
from sky_claw.app.gui import _bootloader
from sky_claw.app.orchestrator.dyndolod_operator_notifier import DynDOLODOperatorNotifier

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"
BOOTLOADER = PAQUETE / "app" / "gui" / "_bootloader.py"

#: Sitios de producción que construyen ``SupervisorAgent`` (fuera de un ``__main__``
#: de desarrollo) y por lo tanto son alcanzables por la etapa 9. Igualdad literal:
#: un sitio nuevo rompe esto hasta que se decida cómo se entera el operador.
SITIOS_QUE_CONSTRUYEN_EL_SUPERVISOR = {"sky_claw/app/gui/_bootloader.py"}


class _Rastreador:
    """``AppContext._track_task`` mínimo: crea la tarea, la guarda y recuerda su nombre."""

    def __init__(self) -> None:
        self.tareas: list[asyncio.Task[Any]] = []

    def __call__(self, coro: Coroutine[Any, Any, Any], *, name: str = "") -> asyncio.Task[Any]:
        tarea = asyncio.create_task(coro, name=name)
        self.tareas.append(tarea)
        return tarea

    async def cancelar_todo(self) -> None:
        for tarea in self.tareas:
            tarea.cancel()
        await asyncio.gather(*self.tareas, return_exceptions=True)


def _ctx(rastreador: _Rastreador, *, sender: Any = None, chat: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(sender=sender, operator_chat_id=chat, _track_task=rastreador)


def _sender() -> MagicMock:
    sender = MagicMock()
    sender.send = AsyncMock()
    sender.send_document = AsyncMock()
    return sender


async def _esperar(condicion: Callable[[], object], *, descripcion: str) -> None:
    async def _girar() -> None:
        while not condicion():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_girar(), timeout=5)
    except TimeoutError:  # pragma: no cover - sólo si el comportamiento falla
        pytest.fail(f"nunca se cumplió: {descripcion}")


def _completed_fallido(log: pathlib.Path) -> Event:
    payload = DynDOLODPipelineCompletedPayload(
        preset="Medium",
        run_texgen=False,
        success=False,
        texgen_success=False,
        dyndolod_success=False,
        errors=("<Error: Unresolved FormID [0207B5B9]>",),
        duration_seconds=75.0,
        rolled_back=True,
        log_paths=(str(log),),
    ).to_log_dict()
    return Event(topic="pipeline.dyndolod.completed", payload=payload)


class TestInstalacion:
    async def test_un_fallo_publicado_en_el_bus_real_llega_al_chat_del_operador_con_su_log(
        self, tmp_path: pathlib.Path
    ) -> None:
        log = tmp_path / "DynDOLOD_SSE_log.txt"
        log.write_bytes(b"[00:10] Fatal: Can not create path\n")
        rastreador = _Rastreador()
        ctx = _ctx(rastreador)  # sin sender ni chat AL INSTALAR: el orden real puede ser cualquiera
        bus = CoreEventBus()
        await bus.start()
        try:
            _bootloader._install_dyndolod_operator_notifier(ctx, SimpleNamespace(event_bus=bus))
            sender = _sender()
            ctx.sender, ctx.operator_chat_id = sender, 42  # se publican DESPUÉS de instalar
            await bus.publish(_completed_fallido(log))
            await _esperar(lambda: sender.send_document.await_count, descripcion="el log adjunto llegó al sender")
        finally:
            await bus.stop()
            await rastreador.cancelar_todo()

        sender.send.assert_awaited_once()
        chat, texto = sender.send.await_args.args
        assert chat == 42
        assert sender.send.await_args.kwargs == {"parse_mode": "HTML"}
        assert texto.startswith("❌") and "&lt;Error: Unresolved FormID" in texto
        sender.send_document.assert_awaited_once()
        assert sender.send_document.await_args.args == (42, b"[00:10] Fatal: Can not create path\n", log.name)

    async def test_instalar_rastrea_el_worker_y_devuelve_el_notificador(self) -> None:
        rastreador = _Rastreador()
        bus = MagicMock()

        notificador = _bootloader._install_dyndolod_operator_notifier(_ctx(rastreador), SimpleNamespace(event_bus=bus))

        try:
            assert isinstance(notificador, DynDOLODOperatorNotifier)
            bus.subscribe.assert_called_once_with("pipeline.dyndolod.*", notificador.on_event)
            assert [t.get_name() for t in rastreador.tareas] == ["dyndolod-operator-notifier"]
        finally:
            await rastreador.cancelar_todo()

    async def test_sin_canal_de_operador_la_corrida_no_se_entera(self, tmp_path: pathlib.Path) -> None:
        """Sin Telegram configurado el aviso es un no-op: un aviso informativo no puede gatear una corrida."""
        rastreador = _Rastreador()
        bus = CoreEventBus()
        await bus.start()
        try:
            notificador = _bootloader._install_dyndolod_operator_notifier(
                _ctx(rastreador), SimpleNamespace(event_bus=bus)
            )
            await bus.publish(_completed_fallido(tmp_path / "DynDOLOD_SSE_log.txt"))
            await _esperar(lambda: notificador._cola.empty(), descripcion="el worker consumió la notificación")
            await asyncio.sleep(0)
        finally:
            await bus.stop()
            await rastreador.cancelar_todo()

        assert bus.events_lost == 0


# ---------------------------------------------------------------------------
# Censo: quién construye el supervisor es quién debe instalar el notificador
# ---------------------------------------------------------------------------


def _dentro_de_un_main(arbol: ast.AST) -> set[int]:
    """``id`` de los nodos que viven dentro de un ``if __name__ == "__main__":``."""
    ids: set[int] = set()
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.If)
            and isinstance(nodo.test, ast.Compare)
            and isinstance(nodo.test.left, ast.Name)
            and nodo.test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in nodo.test.comparators)
        ):
            for hijo in nodo.body:
                ids.update(id(n) for n in ast.walk(hijo))
    return ids


def _sitios_que_construyen_el_supervisor() -> set[str]:
    sitios: set[str] = set()
    for archivo in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        en_main = _dentro_de_un_main(arbol)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or id(nodo) in en_main:
                continue
            nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", "")
            if nombre == "SupervisorAgent":
                sitios.add(str(archivo.relative_to(RAIZ)).replace("\\", "/"))
    return sitios


def test_los_sitios_que_construyen_el_supervisor_estan_congelados() -> None:
    assert _sitios_que_construyen_el_supervisor() == SITIOS_QUE_CONSTRUYEN_EL_SUPERVISOR, (
        "apareció (o desapareció) un sitio de producción que construye SupervisorAgent: la etapa 9 es alcanzable "
        "desde él y el operador remoto no se enteraría de su fin. Cablearle el notificador (o declarar la exención)"
    )


def test_cada_sitio_que_construye_el_supervisor_instala_el_notificador_de_operador() -> None:
    sin_notificador = []
    for clave in sorted(SITIOS_QUE_CONSTRUYEN_EL_SUPERVISOR):
        arbol = ast.parse((RAIZ / clave).read_text(encoding="utf-8"))
        llamadas = {
            nodo.func.id if isinstance(nodo.func, ast.Name) else getattr(nodo.func, "attr", "")
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Call)
        }
        if "_install_dyndolod_operator_notifier" not in llamadas:
            sin_notificador.append(clave)

    assert sin_notificador == []


def test_el_bootstrap_instala_el_notificador_antes_de_arrancar_el_supervisor() -> None:
    """Instalar tras ``supervisor.start()`` perdería el ``started`` de una corrida lanzada al arranque."""
    arbol = ast.parse(BOOTLOADER.read_text(encoding="utf-8"))
    [bootstrap] = [
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_bootstrap"
    ]
    lineas: dict[str, list[int]] = {}
    for nodo in ast.walk(bootstrap):
        if not isinstance(nodo, ast.Call):
            continue
        if isinstance(nodo.func, ast.Name) and nodo.func.id == "_install_dyndolod_operator_notifier":
            lineas.setdefault("instalar", []).append(nodo.lineno)
        if isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "start":
            propietario = nodo.func.value
            if isinstance(propietario, ast.Name) and propietario.id == "supervisor":
                lineas.setdefault("start", []).append(nodo.lineno)

    assert len(lineas.get("instalar", [])) == 1, "_bootstrap debe instalar el notificador exactamente una vez"
    assert len(lineas.get("start", [])) == 1
    assert lineas["instalar"][0] < lineas["start"][0]


def test_el_pipeline_de_etapa_9_publica_en_el_bus_del_supervisor() -> None:
    """El notificador escucha ``supervisor.event_bus``: el servicio productivo tiene que publicar AHÍ.

    ``build_orchestration_composition`` recibe el bus del supervisor y se lo entrega al servicio. Si alguien le
    pasara otro, el notificador quedaría sordo sin que ningún test del notificador lo note.
    """
    fuente = (PAQUETE / "app" / "orchestrator" / "supervisor.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and getattr(nodo.func, "id", getattr(nodo.func, "attr", "")) == "build_orchestration_composition"
    ]
    assert len(llamadas) == 1
    [bus] = [kw.value for kw in llamadas[0].keywords if kw.arg == "event_bus"]
    assert ast.unparse(bus) == "self._event_bus"

    # ...y que `event_bus` (el accesor que usa el bootloader) devuelve ESE mismo atributo.
    [propiedad] = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef)
        and nodo.name == "event_bus"
        and any(getattr(d, "id", "") == "property" for d in nodo.decorator_list)
    ]
    assert [ast.unparse(n.value) for n in ast.walk(propiedad) if isinstance(n, ast.Return) and n.value] == [
        "self._event_bus"
    ]
