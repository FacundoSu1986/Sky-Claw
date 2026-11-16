"""MISIÓN B: contexto de admisión OBLIGATORIO en las dos superficies productivas.

``XEditBridgeService.admitir`` copia archivos al laboratorio del bridge. El contexto
(``ContextoAdmision``) es el que se le mostró al operador al pedir la aprobación; la autorización
HITL es OTRO control, que vive en cada superficie. Acá se prueba que:

* las dos superficies productivas (GUI y agente LLM) le piden explícitamente al servicio que
  exija el contexto, sin reemplazar ni debilitar su gate HITL;
* el servicio rechaza sin copiar cuando falta el contexto o cuando quedó desactualizado;
* la decisión HITL denegada, caducada o de tipo inválido nunca produce una copia;
* el contexto está ligado a UNA solicitud HITL y se consume UNA sola vez.

Los tests de superficie usan dobles (unitarios). Los tests del servicio usan el bridge FALSO de
``tests/fixtures/xedit_bridge``: no declaran ninguna validación sobre SSEEdit real.
"""

from __future__ import annotations

import ast
import json
import pathlib
import tomllib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.xedit_bridge_tools import (
    GestorDeAdmisionesNativas,
    admitir_plugin_nativo,
)
from sky_claw.app.orchestrator.tool_strategies.admitir_plugin_nativo import (
    _SCOPE_CONTEXT_KEY,
    AdmitirPluginNativoStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.middleware import _hitl_invocation_state
from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision

from .test_xedit_bridge_service import _servicio as _servicio_real

RAIZ = pathlib.Path(__file__).resolve().parents[1]
CONTEXTO = ContextoAdmision(pathlib.Path("C:/MO2/Instancia"), "prueva huevo 3")

#: Los DOS únicos llamadores productivos de ``.admitir(``. Un tercero tiene que romper el ancla.
LLAMADORES_PRODUCTIVOS = frozenset(
    {
        "sky_claw/app/agent/tools/xedit_bridge_tools.py",
        "sky_claw/app/orchestrator/tool_strategies/admitir_plugin_nativo.py",
    }
)

#: Los cinco módulos del bridge que heredaban ``ignore_errors = true`` (objetivo 2).
MODULOS_BRIDGE = frozenset(
    {
        "sky_claw.local.tools.xedit_bridge_client",
        "sky_claw.local.tools.xedit_bridge_service",
        "sky_claw.app.agent.tools.xedit_bridge_tools",
        "sky_claw.app.orchestrator.tool_strategies.admitir_plugin_nativo",
        "sky_claw.app.orchestrator.tool_strategies.auditar_plugin_nativo",
    }
)


def _servicio_doble() -> MagicMock:
    servicio = MagicMock()
    servicio.admitir = AsyncMock(
        return_value={"success": True, "message": "", "admission_id": "adm-1", "clausura": ["Mod.esp"]}
    )
    servicio.contexto_de_admision = MagicMock(return_value=CONTEXTO)
    return servicio


def _hitl(decision: object) -> MagicMock:
    guard = MagicMock()
    guard.request_approval = AsyncMock(return_value=decision)
    return guard


def _gestor() -> GestorDeAdmisionesNativas:
    async def _notificar(_mensaje: str) -> None:
        return None

    return GestorDeAdmisionesNativas(notificar=_notificar)


async def _terminar(gestor: GestorDeAdmisionesNativas) -> None:
    assert gestor.actual is not None and gestor.actual.tarea is not None
    await gestor.actual.tarea


# --- 1. Contexto ausente -> rechazo ---------------------------------------------------------


async def test_contexto_ausente_en_el_servicio_rechaza_sin_lanzar_el_bridge(tmp_path: pathlib.Path) -> None:
    servicio, raiz = _servicio_real(tmp_path, "admitido")

    resultado = await servicio.admitir("Mod.esp", requerir_contexto=True)

    assert resultado["success"] is False
    assert "contexto" in resultado["message"].casefold()
    assert not (raiz / "admit_args.json").exists(), "sin contexto aprobado no se puede lanzar run_admit.py"


async def test_contexto_ausente_en_la_gui_rechaza_sin_invocar_la_admision() -> None:
    servicio = _servicio_doble()
    estrategia = AdmitirPluginNativoStrategy(service=servicio)

    resultado = await estrategia.execute({"plugin": "Mod.esp"})

    assert resultado["success"] is False
    servicio.admitir.assert_not_awaited()


# --- 2. Contexto desactualizado -> rechazo --------------------------------------------------


async def test_contexto_desactualizado_rechaza_sin_lanzar_el_bridge(tmp_path: pathlib.Path) -> None:
    servicio, raiz = _servicio_real(tmp_path, "admitido", mo2=pathlib.Path("C:/MO2/A"), perfil="P1")
    autorizado = servicio.contexto_de_admision()
    servicio._rutas.perfil = "P2"

    resultado = await servicio.admitir("Mod.esp", contexto=autorizado, requerir_contexto=True)

    assert resultado["success"] is False
    assert "cambió" in resultado["message"]
    assert not (raiz / "admit_args.json").exists()


# --- 3. Contexto válido + aprobación efectiva -> continúa (y exige el contexto) -------------


async def test_contexto_valido_y_aprobacion_efectiva_permite_continuar() -> None:
    servicio, guard, gestor = _servicio_doble(), _hitl(Decision.APPROVED), _gestor()

    salida = json.loads(await admitir_plugin_nativo(servicio, guard, "Mod.esp", admisiones=gestor))
    await _terminar(gestor)

    assert salida["success"] is True
    assert gestor.actual is not None and gestor.actual.estado == "exitosa"
    # El pedido HITL no es decoración: la categoría y el texto son lo que el operador ve y aprueba.
    # Si el nombre real o el contexto no llegan al pedido, la aprobación sería para otra cosa.
    guard.request_approval.assert_awaited_once()
    args_hitl = guard.request_approval.call_args.kwargs
    assert args_hitl["category"] == "tool_execution"
    assert "Mod.esp" in args_hitl["reason"], "el nombre real del plugin tiene que estar en el pedido HITL"
    assert CONTEXTO.resumen() in args_hitl["reason"], "el pedido HITL tiene que mostrar el contexto COMPLETO"
    servicio.admitir.assert_awaited_once_with("Mod.esp", contexto=CONTEXTO, requerir_contexto=True)


# --- 4/5. Decisión denegada / caducada -> NO se invoca la admisión --------------------------


@pytest.mark.parametrize("decision", [Decision.DENIED, Decision.TIMEOUT], ids=["denegada", "caducada_por_timeout"])
async def test_decision_denegada_o_caducada_no_invoca_la_admision(decision: Decision) -> None:
    servicio, guard, gestor = _servicio_doble(), _hitl(decision), _gestor()

    await admitir_plugin_nativo(servicio, guard, "Mod.esp", admisiones=gestor)
    await _terminar(gestor)

    servicio.admitir.assert_not_awaited()
    assert gestor.actual is not None and gestor.actual.estado == "rechazada"


# --- 6. Tipo HITL inválido -> NO produce aprobación -----------------------------------------


@pytest.mark.parametrize("valor", ["approved", None, True, 1], ids=["texto", "none", "bool", "entero"])
async def test_tipo_hitl_invalido_no_produce_aprobacion_ni_error_inesperado(valor: object) -> None:
    servicio, guard, gestor = _servicio_doble(), _hitl(valor), _gestor()

    await admitir_plugin_nativo(servicio, guard, "Mod.esp", admisiones=gestor)
    await _terminar(gestor)

    servicio.admitir.assert_not_awaited()
    assert gestor.actual is not None and gestor.actual.estado == "rechazada"
    assert gestor.actual.resultado is not None
    assert gestor.actual.resultado["success"] is False
    assert "error inesperado" not in gestor.actual.resultado["message"].casefold()


# --- 7/8. Vinculación con la solicitud HITL y consumo único (GUI) ---------------------------


async def test_contexto_de_otra_solicitud_hitl_se_rechaza_sin_copiar() -> None:
    servicio = _servicio_doble()
    estrategia = AdmitirPluginNativoStrategy(service=servicio)
    token = _hitl_invocation_state.set({"request_id": "tool-R1"})
    try:
        estrategia.describe_for_approval({"plugin": "Mod.esp"})  # liga el contexto a R1
        alcance = _hitl_invocation_state.get()
        assert alcance is not None and _SCOPE_CONTEXT_KEY in alcance, "R1 tiene que dejar su autorización en el scope"
        # Cambiar SOLO el request_id, en el MISMO dict: la autorización de R1 sigue presente pero ahora
        # execute() corre bajo R2. Reemplazar el dict entero borraría la autorización y el test pasaría
        # aunque el consumidor no comparara request_id (mutante sobreviviente).
        alcance["request_id"] = "tool-R2"

        resultado = await estrategia.execute({"plugin": "Mod.esp"})
    finally:
        _hitl_invocation_state.reset(token)

    assert resultado["success"] is False
    servicio.admitir.assert_not_awaited()


async def test_reintento_de_autorizacion_consumida_se_rechaza_sin_copiar() -> None:
    servicio = _servicio_doble()
    estrategia = AdmitirPluginNativoStrategy(service=servicio)
    token = _hitl_invocation_state.set({"request_id": "tool-R1"})
    try:
        estrategia.describe_for_approval({"plugin": "Mod.esp"})
        primero = await estrategia.execute({"plugin": "Mod.esp"})
        segundo = await estrategia.execute({"plugin": "Mod.esp"})
    finally:
        _hitl_invocation_state.reset(token)

    assert primero["success"] is True
    assert segundo["success"] is False
    servicio.admitir.assert_awaited_once_with("Mod.esp", contexto=CONTEXTO, requerir_contexto=True)


# --- Ancla estructural: enumerar TODOS los llamadores, no muestrear uno --------------------


def _llamadas_a_admitir_por_archivo() -> dict[str, list[ast.Call]]:
    llamadas: dict[str, list[ast.Call]] = {}
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
        nodos = [
            n
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "admitir"
        ]
        if nodos:
            llamadas[ruta.relative_to(RAIZ).as_posix()] = nodos
    return llamadas


def test_todos_los_llamadores_productivos_exigen_contexto_obligatorio() -> None:
    """Regla #1 del repo: arreglar un hermano y no al otro es el defecto dominante.

    El ancla congela el conjunto COMPLETO de llamadores de ``admitir`` en ``sky_claw/``:
    uno nuevo rompe el test hasta que se lo clasifique y se le escriba su receta.
    """
    llamadas = _llamadas_a_admitir_por_archivo()

    assert set(llamadas) == LLAMADORES_PRODUCTIVOS, (
        f"cambió el conjunto de llamadores productivos de admitir(): {sorted(llamadas)}"
    )
    for archivo, nodos in llamadas.items():
        assert len(nodos) == 1, f"{archivo}: se esperaba una única llamada a admitir()"
        kwargs = {kw.arg: kw.value for kw in nodos[0].keywords}
        assert "contexto" in kwargs, f"{archivo}: la admisión tiene que pasar el contexto aprobado"
        flag = kwargs.get("requerir_contexto")
        assert isinstance(flag, ast.Constant) and flag.value is True, (
            f"{archivo}: la ruta productiva tiene que pedir requerir_contexto=True explícitamente"
        )


# --- Ancla de configuración: los cinco módulos del bridge ya NO están silenciados -----------


def test_los_cinco_modulos_del_bridge_tienen_override_especifico_de_mypy() -> None:
    """Evita que el ``ignore_errors = true`` amplio vuelva a tragar los errores del bridge."""
    config = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = config["tool"]["mypy"]["overrides"]

    especificos = [bloque for bloque in overrides if set(bloque.get("module", [])) >= MODULOS_BRIDGE]

    assert len(especificos) == 1, "tiene que existir UN override literal para los cinco módulos del bridge"
    bloque = especificos[0]
    assert bloque.get("ignore_errors") is False
    # ``strict`` NO es una opción per-module en mypy 1.20.2: el override tiene que enumerar
    # los flags que sí lo son, para no depender de un efecto global de otro bloque.
    for flag in (
        "disallow_untyped_defs",
        "disallow_untyped_calls",
        "disallow_any_generics",
        "disallow_incomplete_defs",
        "check_untyped_defs",
        "warn_return_any",
        "warn_unused_ignores",
        "strict_equality",
        "extra_checks",
    ):
        assert bloque.get(flag) is True, f"falta el flag per-module {flag} en el override del bridge"
