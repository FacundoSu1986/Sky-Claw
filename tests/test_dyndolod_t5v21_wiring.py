"""T5-v2.1 — tests del adapter ``ConfirmadorHITL`` y su cableado en composition.

Cubre:

* ``HITLGuard.request_approval(timeout=...)`` con override por-solicitud
  (delta §39).
* Categoría ``dyndolod_configuracion_lista`` NUNCA se auto-aprueba con
  «Modo local» (delta §12 / §38).
* ``ConfirmadorHITL`` traduce ``Decision`` a ``ResultadoConfirmacion``.
* El composition root inyecta el adapter en el service y el service lo
  pasa al runner (delta §40, M5).
* ``ConfirmadorNoDisponible`` (delta §14).
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL
from sky_claw.app.security.hitl import (
    CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
    Decision,
    HITLGuard,
)
from sky_claw.local.tools.dyndolod_uia_gate import (
    ConfirmadorNoDisponible,
    OperatorConfigurationReadyRequest,
    ResultadoConfirmacion,
)

PID = 4242


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _solicitud() -> OperatorConfigurationReadyRequest:
    return OperatorConfigurationReadyRequest(
        tool_name="DynDOLOD",
        pid=PID,
        executable=pathlib.Path(r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
        expected_output=pathlib.Path(r"C:\Games\Sky-Claw\DynDOLOD"),
        timeout_seconds=60.0,
    )


# ---------------------------------------------------------------------------
# §39 — HITLGuard request_approval(timeout=...) override
# ---------------------------------------------------------------------------


async def test_hitl_guard_request_approval_timeout_override():
    """§39 — ``timeout`` explícito usa override; ``None`` usa ``self._timeout``."""
    import inspect

    sig = inspect.signature(HITLGuard.request_approval)
    assert "timeout" in sig.parameters
    assert sig.parameters["timeout"].default is None, "el default debe ser None (self._timeout)"


async def test_hitl_guard_usa_override_si_se_pasa_explicito():
    """El override por-solicitud gana sobre ``self._timeout``."""
    # Guard con self._timeout = 10s; con override 0.05s el guard debe
    # cortar antes.
    guard = HITLGuard(timeout=10)
    # Sin respond: el guard debe devolver TIMEOUT al pasar 0.05s, no 10s.
    decision = await guard.request_approval(
        request_id="req-override-test",
        reason="x",
        detail="x",
        category="scope",
        timeout=0.05,
    )
    assert decision is Decision.TIMEOUT, "el override no se aplicó"


async def test_hitl_guard_usa_self_timeout_si_no_hay_override():
    """Sin override explícito, el guard usa ``self._timeout``."""
    guard = HITLGuard(timeout=0.05)
    decision = await guard.request_approval(
        request_id="req-default-test",
        reason="x",
        detail="x",
        category="scope",
    )
    assert decision is Decision.TIMEOUT


# ---------------------------------------------------------------------------
# §12/§38 — categoría dyndolod_configuracion_lista NUNCA se auto-aprueba
# ---------------------------------------------------------------------------


def test_categoria_dyndolod_esta_definida_y_es_constante():
    assert CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA == "dyndolod_configuracion_lista"
    assert isinstance(CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA, str)


async def test_categoria_dyndolod_no_se_autoaprueba_con_modo_local(monkeypatch):
    """§12/§38 — con Modo local ON, la categoría ``dyndolod_configuracion_lista``
    NO se auto-aprueba: ``make_gui_hitl_notify`` la parkea con modal manual.

    La rama de la GUI con ``category == tool_execution`` SÍ se auto-aprueba
    (comportamiento previo). Esta prueba aísla ambas categorías.
    """
    from sky_claw.app.gui.controllers import ritual_runner

    # Spy del respond.
    respondidas: list[tuple[str, bool]] = []

    async def respond(request_id: str, approved: bool) -> None:
        respondidas.append((request_id, approved))

    parked: list[dict] = []

    def set_pending(entry: dict) -> None:
        parked.append(entry)

    auto_aprobadas: list[str] = []

    def auto_approve_getter() -> bool:
        return True  # modo local ON

    class _Req:
        def __init__(self, rid, category):
            self.request_id = rid
            self.reason = ""
            self.detail = ""
            self.url = ""
            self.category = category

    notify = ritual_runner.make_gui_hitl_notify(
        respond=respond,
        set_pending=set_pending,
        auto_approve_getter=auto_approve_getter,
        delegate=None,
    )

    # tool_execution se auto-aprueba: queda en ``auto_aprobadas`` y NUNCA
    # parkea modal.
    await notify(_Req("req-te", "tool_execution"))
    assert auto_aprobadas == []  # helper acumula en ``respondidas``
    assert respondidas == [("req-te", True)], "tool_execution debe auto-aprobar"
    assert parked == []

    # dyndolod_configuracion_lista NUNCA se auto-aprueba: parkea y NO
    # responde.
    await notify(_Req("req-dc", CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA))
    assert [r for r in respondidas if r[0] == "req-dc"] == [], (
        "dyndolod_configuracion_lista NO debe auto-aprobarse con Modo local"
    )
    assert [p for p in parked if p["request_id"] == "req-dc"], "dyndolod_configuracion_lista debe parquear modal manual"


# ---------------------------------------------------------------------------
# §15 — ConfirmadorHITL traduce Decision correctamente
# ---------------------------------------------------------------------------


async def test_confirmador_hitl_traduce_aprobada():
    """APPROVED → APROBADA."""
    guard = MagicMock(spec=HITLGuard)
    guard.request_approval = AsyncMock(return_value=Decision.APPROVED)
    adapter = ConfirmadorHITL(hitl_guard=guard)
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.APROBADA


async def test_confirmador_hitl_traduce_denegada():
    """DENIED → DENEGADA."""
    guard = MagicMock(spec=HITLGuard)
    guard.request_approval = AsyncMock(return_value=Decision.DENIED)
    adapter = ConfirmadorHITL(hitl_guard=guard)
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.DENEGADA


async def test_confirmador_hitl_traduce_timeout():
    """TIMEOUT → TIMEOUT."""
    guard = MagicMock(spec=HITLGuard)
    guard.request_approval = AsyncMock(return_value=Decision.TIMEOUT)
    adapter = ConfirmadorHITL(hitl_guard=guard)
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.TIMEOUT


async def test_confirmador_hitl_sin_guard_es_canal_no_disponible():
    """§16 — sin HITLGuard, el adapter responde CANAL_NO_DISPONIBLE (fail-closed)."""
    adapter = ConfirmadorHITL(hitl_guard=None)
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


async def test_confirmador_hitl_pasa_categoria_correcta():
    """El adapter pasa la categoría ``dyndolod_configuracion_lista`` al guard."""
    capturado: dict = {}

    class _Guard:
        async def request_approval(self, **kwargs):
            capturado.update(kwargs)
            return Decision.APPROVED

    adapter = ConfirmadorHITL(hitl_guard=_Guard())
    await adapter.confirmar(_solicitud())
    assert capturado["category"] == CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA


async def test_confirmador_hitl_pasa_timeout_de_la_solicitud():
    """El adapter usa ``solicitud.timeout_seconds`` como override."""
    capturado: dict = {}

    class _Guard:
        async def request_approval(self, **kwargs):
            capturado.update(kwargs)
            return Decision.APPROVED

    adapter = ConfirmadorHITL(hitl_guard=_Guard())
    await adapter.confirmar(_solicitud())
    assert capturado["timeout"] == 60.0


async def test_confirmador_hitl_guard_explotando_es_canal_no_disponible():
    """Un guard que lanza una excepción se traduce a CANAL_NO_DISPONIBLE."""

    class _GuardRoto:
        async def request_approval(self, **kwargs):
            raise RuntimeError("guard roto")

    adapter = ConfirmadorHITL(hitl_guard=_GuardRoto())
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


async def test_confirmador_hitl_informar_sin_on_informar_solo_logea():
    """Sin ``on_informar`` inyectado, el adapter loguea pero no toca nada."""
    adapter = ConfirmadorHITL(hitl_guard=None)
    # No debe lanzar.
    await adapter.informar(tool="DynDOLOD", mensaje="test")


async def test_confirmador_hitl_informar_llama_on_informar_si_inyectado():
    """Con ``on_informar``, el adapter lo invoca con (tool, mensaje)."""
    llamadas: list[tuple[str, str]] = []

    async def on(tool: str, mensaje: str) -> None:
        llamadas.append((tool, mensaje))

    adapter = ConfirmadorHITL(hitl_guard=None, on_informar=on)
    await adapter.informar(tool="DynDOLOD", mensaje="Start habilitado")
    assert llamadas == [("DynDOLOD", "Start habilitado")]


async def test_confirmador_hitl_informar_traga_excepciones_de_on_informar():
    """Si ``on_informar`` falla, el adapter lo loguea y NO propaga."""

    async def on_fail(tool: str, mensaje: str) -> None:
        raise RuntimeError("informar roto")

    adapter = ConfirmadorHITL(hitl_guard=None, on_informar=on_fail)
    # No debe lanzar.
    await adapter.informar(tool="DynDOLOD", mensaje="test")


# ---------------------------------------------------------------------------
# §14 — ConfirmadorNoDisponible: fail-closed por canal
# ---------------------------------------------------------------------------


async def test_confirmador_no_disponible_devuelve_canal_no_disponible():
    """``ConfirmadorNoDisponible.confirmar`` SIEMPRE CANAL_NO_DISPONIBLE."""
    adapter = ConfirmadorNoDisponible()
    resultado = await adapter.confirmar(_solicitud())
    assert resultado is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


async def test_confirmador_no_disponible_informar_no_lanza():
    """``ConfirmadorNoDisponible.informar`` no lanza y deja que el caller siga."""
    adapter = ConfirmadorNoDisponible()
    await adapter.informar(tool="DynDOLOD", mensaje="x")


# ---------------------------------------------------------------------------
# §40 — wiring: composition → service → runner (M5, M3 reales)
# ---------------------------------------------------------------------------


def test_ensure_runner_del_service_pasa_confirmador(tmp_path, monkeypatch):
    """M5 — el service pasa el confirmador al lazy runner.

    Anclamos por AST: ``DynDOLODPipelineService._ensure_runner`` invoca
    ``DynDOLODRunner(...)`` con el kwarg ``confirmador_de_configuracion``
    cableado desde el campo ``self._confirmador_de_configuracion``. Sin
    esta propiedad, M5 dejaría los tests verdes: el runner arrancaría
    sin confirmador y la readyness caería en CANAL_NO_DISPONIBLE
    silenciosamente.
    """
    import ast

    fuente = pathlib.Path(
        "E:/Skyclaw/worktrees/t5v2-uia-output-gate/sky_claw/local/tools/dyndolod_service.py"
    ).read_text(encoding="utf-8")
    nodo = None
    for top in ast.parse(fuente).body:
        if isinstance(top, ast.ClassDef) and top.name == "DynDOLODPipelineService":
            for sub in top.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "_ensure_runner":
                    nodo = sub
                    break
    assert nodo is not None
    found = False
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "DynDOLODRunner":
            for kw in sub.keywords:
                if kw.arg == "confirmador_de_configuracion":
                    found = True
    assert found, (
        "DynDOLODPipelineService._ensure_runner no cablea "
        "confirmador_de_configuracion en el constructor de DynDOLODRunner (M5)"
    )


def test_composition_root_construye_confirmador_y_pasa_al_service(monkeypatch):
    """El composition root crea ``ConfirmadorHITL`` y lo pasa al service."""
    import ast

    fuente = pathlib.Path(
        "E:/Skyclaw/worktrees/t5v2-uia-output-gate/sky_claw/app/orchestrator/orchestration_composition.py"
    ).read_text(encoding="utf-8")
    # ``ConfirmadorHITL`` aparece en el módulo.
    assert "ConfirmadorHITL" in fuente
    # Y se pasa a DynDOLODPipelineService como ``confirmador_de_configuracion``.
    nodo_service_ctor = None
    for top in ast.parse(fuente).body:
        if isinstance(top, ast.FunctionDef) and top.name == "build_orchestration_composition":
            for sub in ast.walk(top):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "DynDOLODPipelineService"
                ):
                    nodo_service_ctor = sub
    assert nodo_service_ctor is not None
    found = any(kw.arg == "confirmador_de_configuracion" for kw in nodo_service_ctor.keywords)
    assert found, "build_orchestration_composition no pasa confirmador_de_configuracion a DynDOLODPipelineService"
