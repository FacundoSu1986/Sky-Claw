"""NP-A0h, superficie GUI: la tool ``diagnosticar_parallax_perfil`` del dispatcher.

Solo lectura (sin HITL, como ``scan_asset_conflicts``). Contesta con la MISMA función que el agente LLM
(``diagnostico_como_resultado``) sobre las raíces que le da el resolver; el perfil es el de la sesión y no sale del payload (el perfil
queda fijo durante la sesión: el hermano LLM tampoco lo expone). Qué se verifica: el resultado sobre un disco real, que corre fuera del
event loop, que un payload inválido se rechaza SIN tocar el disco, y que el wiring del dispatcher la registra sin gate.
"""

from __future__ import annotations

import pathlib
import threading
from typing import Any

import pytest

from sky_claw.app.core.path_resolver import RaicesDeInstanciaMO2
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_strategies import diagnosticar_perfil_parallax as modulo
from sky_claw.app.orchestrator.tool_strategies.diagnosticar_perfil_parallax import DiagnosticarPerfilParallaxStrategy
from sky_claw.local.native_parallax.diagnostico_perfil import diagnostico_como_resultado
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

_CARPETA = "textures/architecture/whiterun"
_MOD = "Texturas"
_CONTROL = "SkyClaw-Control-Visual"


def _archivo(raiz: pathlib.Path, mod: str, ruta: str) -> None:
    destino = raiz / "mods" / mod / ruta
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"x" + mod.encode())


@pytest.fixture
def instancia(tmp_path: pathlib.Path) -> pathlib.Path:
    _archivo(tmp_path, _MOD, f"{_CARPETA}/muro01_n.dds")
    _archivo(tmp_path, NOMBRE_DEL_MOD_DE_SALIDA, "textures/.vacio")
    _archivo(tmp_path, _CONTROL, f"{_CARPETA}/muro01_p.dds")
    modlist = tmp_path / "profiles" / "Default" / "modlist.txt"
    modlist.parent.mkdir(parents=True)
    modlist.write_text(f"+{_CONTROL}\n+{NOMBRE_DEL_MOD_DE_SALIDA}\n+{_MOD}\n", encoding="utf-8")
    return tmp_path


def _estrategia(raiz: pathlib.Path, perfil: str = "Default") -> DiagnosticarPerfilParallaxStrategy:
    raices = RaicesDeInstanciaMO2(install_root=raiz, data_root=raiz, mods_dir=raiz / "mods")
    return DiagnosticarPerfilParallaxStrategy(raices_provider=lambda: raices, default_profile_getter=lambda: perfil)


async def test_contesta_lo_mismo_que_la_funcion_compartida_sobre_las_raices_dadas(instancia: pathlib.Path) -> None:
    esperado = diagnostico_como_resultado(mods_dir=instancia / "mods", data_root=instancia, perfil="Default", mod=_MOD)

    resultado = await _estrategia(instancia).execute({"mod": _MOD})

    assert resultado == esperado
    assert resultado["success"] is True and resultado["n_hallazgos"] == 1


async def test_el_perfil_es_el_de_la_sesion_y_no_el_del_payload(instancia: pathlib.Path) -> None:
    resultado = await _estrategia(instancia, perfil="Otro").execute({"mod": _MOD})

    assert resultado["success"] is False
    assert "Otro" in resultado["message"] or "modlist" in resultado["message"]


async def test_corre_fuera_del_event_loop(instancia: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hilos: list[int] = []
    real = modulo.diagnostico_como_resultado

    def espiar(**kw: Any) -> dict[str, Any]:
        hilos.append(threading.get_ident())
        return real(**kw)

    monkeypatch.setattr(modulo, "diagnostico_como_resultado", espiar)

    await _estrategia(instancia).execute({"mod": _MOD})

    assert hilos and hilos[0] != threading.get_ident()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"mod": ""},
        {"mod": "   "},
        {"mod": 3},
        {"mod": None},
        {"mod": "x" * 300},
        {"mod": _MOD, "perfil": "Otro"},  # el perfil NO se elige por payload
        {"mod": _MOD, "extra": 1},
    ],
)
async def test_un_payload_invalido_se_rechaza_sin_tocar_el_disco(payload: dict[str, Any]) -> None:
    llamadas: list[str] = []

    def raices() -> RaicesDeInstanciaMO2:
        llamadas.append("raices")
        raise AssertionError("no debía resolver raíces")

    estrategia = DiagnosticarPerfilParallaxStrategy(raices_provider=raices, default_profile_getter=lambda: "Default")

    resultado = await estrategia.execute(payload)

    assert resultado["success"] is False and resultado["message"]
    assert llamadas == []


async def test_sin_instalacion_de_mo2_es_un_resultado_con_motivo_y_no_una_excepcion() -> None:
    def sin_mo2() -> RaicesDeInstanciaMO2:
        raise RuntimeError("No se pudo resolver la instalación de MO2")

    estrategia = DiagnosticarPerfilParallaxStrategy(raices_provider=sin_mo2, default_profile_getter=lambda: "Default")

    resultado = await estrategia.execute({"mod": _MOD})

    assert resultado["success"] is False and "MO2" in resultado["message"]


def test_el_dispatcher_la_registra_sin_gate_hitl() -> None:
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware

    gate = HitlGateMiddleware(allow_unattended=True)
    deps = crear_dependencias_dispatcher()
    dispatcher = build_orchestration_dispatcher(deps, hitl_gate=gate)

    assert DiagnosticarPerfilParallaxStrategy.name in dispatcher.registered_tools()
    estrategia = dispatcher._strategies[DiagnosticarPerfilParallaxStrategy.name]
    assert estrategia._raices_provider is deps.raices_mo2_provider
    assert estrategia._default_profile_getter is deps.default_profile_getter
    assert not any(m is gate for m in dispatcher._middleware[DiagnosticarPerfilParallaxStrategy.name])
