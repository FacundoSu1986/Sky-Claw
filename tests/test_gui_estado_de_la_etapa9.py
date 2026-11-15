"""P-3c-2 (H-11 y H-4): la GUI muestra el progreso de la etapa 9 y la cuarentena del broker VFS.

Hasta ahora solo los veía el agente (``lods_status``): el operador frente a la GUI no podía distinguir «generando» de
«colgado» ni saber que su instancia estaba aislada. Las DOS superficies leen la MISMA fuente y el MISMO formateador de
hechos (``progreso_en_hechos``): repartirlo garantizaba que cada una contara el progreso a su manera.

Son HECHOS medidos, nunca un veredicto «colgado»: lo juzga el operador con la edad de la muestra a la vista.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import time
import types
from typing import Any

import pytest

from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods, TrabajoDeLods
from sky_claw.app.core.event_payloads import DynDOLODPipelineProgressPayload
from sky_claw.app.gui.controllers.etapa9_estado import (
    EstadoDeLaEtapa9,
    leer_estado_de_la_etapa9,
    lineas_de_progreso,
    texto_de_cuarentena,
)
from sky_claw.local.mo2 import vfs_quarantine
from sky_claw.local.tools.dyndolod_progreso import progreso_en_hechos

RAIZ = pathlib.Path(__file__).resolve().parents[1]
AHORA = 1_800_000_000.0


def _muestra(**cambios: object) -> DynDOLODPipelineProgressPayload:
    base: dict[str, object] = {
        "tool_name": "DynDOLOD",
        "elapsed_seconds": 3725.46,
        "log_path": "C:/DynDOLOD/Logs/DynDOLOD_SSE_log.txt",
        "log_bytes": 8192,
        "log_idle_seconds": 41.04,
        "sampled_at": AHORA - 30,
    }
    base.update(cambios)
    return DynDOLODPipelineProgressPayload(**base)  # type: ignore[arg-type]


def _supervisor(servicio: object | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(dyndolod_service=servicio)


def _servicio(muestra: DynDOLODPipelineProgressPayload | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(progreso_actual=muestra)


_CUARENTENA = {
    "activa": True,
    "razon": "terminalidad desconocida",
    "como_liberar": "SkyClawApp.exe --mode vfs-quarantine",
}


# ---------------------------------------------------------------------------------------- los hechos


def test_los_hechos_de_la_muestra_son_los_que_ya_veia_el_agente() -> None:
    hechos = progreso_en_hechos(_muestra(), ahora=AHORA)

    assert hechos == {
        "herramienta": "DynDOLOD",
        "transcurrido_segundos": 3725.5,
        "log_bytes": 8192,
        "log_sin_cambios_segundos": 41.0,
        "muestra_hace_segundos": 30.0,
    }


def test_sin_log_medible_los_hechos_lo_dicen_con_none_y_no_con_un_cero() -> None:
    hechos = progreso_en_hechos(_muestra(log_path=None, log_bytes=None, log_idle_seconds=None), ahora=AHORA)

    assert hechos["log_bytes"] is None and hechos["log_sin_cambios_segundos"] is None


def test_una_muestra_del_futuro_no_da_una_edad_negativa() -> None:
    assert progreso_en_hechos(_muestra(sampled_at=AHORA + 90), ahora=AHORA)["muestra_hace_segundos"] == 0.0


def test_la_gui_y_el_agente_cuentan_el_mismo_progreso() -> None:
    """Paridad por igualdad: misma muestra ⇒ mismos hechos por las dos superficies (salvo el reloj de la consulta)."""
    muestra = _muestra(sampled_at=time.time() - 30)
    servicio = _servicio(muestra)
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    gestor._actual = TrabajoDeLods(id="lods-1", parametros={}, iniciado_en=time.time() - 60, estado="en_curso")  # noqa: SLF001

    del_agente = gestor.estado()["progreso"]
    de_la_gui = leer_estado_de_la_etapa9(
        supervisor=_supervisor(servicio), consultar_cuarentena=None, ahora=time.time()
    ).progreso

    assert de_la_gui is not None
    assert set(de_la_gui) == set(del_agente)
    for clave in set(del_agente) - {"muestra_hace_segundos"}:
        assert de_la_gui[clave] == del_agente[clave], clave
    assert de_la_gui["muestra_hace_segundos"] == pytest.approx(del_agente["muestra_hace_segundos"], abs=2.0)


# --------------------------------------------------------------------------------- la lectura de estado


def test_con_la_etapa_en_curso_y_una_muestra_la_gui_ve_el_progreso() -> None:
    estado = leer_estado_de_la_etapa9(
        supervisor=_supervisor(_servicio(_muestra())), consultar_cuarentena=None, ahora=AHORA
    )

    assert isinstance(estado, EstadoDeLaEtapa9)
    assert estado.progreso is not None and estado.progreso["herramienta"] == "DynDOLOD"
    assert estado.cuarentena is None


def test_el_progreso_se_ve_con_una_corrida_lanzada_por_cualquier_superficie() -> None:
    """Una corrida remota (Telegram, ``/api/chat``) no pasa por el single-flight de la GUI y igual se ve.

    El servicio descarta la muestra al iniciar y al terminar cada corrida (``progreso_actual`` es «de la corrida EN CURSO»),
    así que su ciclo de vida es la fuente común. Condicionarla a un flag de la GUI hacía invisible lo que lanza el agente.
    """
    estado = leer_estado_de_la_etapa9(
        supervisor=_supervisor(_servicio(_muestra())), consultar_cuarentena=None, ahora=AHORA
    )

    assert estado.progreso is not None


def test_la_lectura_no_depende_del_single_flight_de_la_gui() -> None:
    """Ancla por AST: ni el seam ni el callback de la GUI consultan ``STORE_KEY_RITUAL_IN_FLIGHT``."""
    seam = ast.dump(_arbol(RAIZ / "sky_claw" / "app" / "gui" / "controllers" / "etapa9_estado.py"))
    gui = _arbol(RAIZ / "sky_claw" / "app" / "gui" / "sky_claw_gui.py")
    callbacks = [
        n
        for n in ast.walk(gui)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "on_etapa9_estado"
            for t in n.targets
        )
    ]

    assert len(callbacks) == 1
    assert "IN_FLIGHT" not in seam and "en_curso" not in seam
    assert "IN_FLIGHT" not in ast.dump(callbacks[0]) and "en_curso" not in ast.dump(callbacks[0])


@pytest.mark.parametrize(
    "supervisor",
    [
        None,
        _supervisor(None),
        _supervisor(types.SimpleNamespace()),  # servicio sin ``progreso_actual`` (un doble o un servicio viejo)
        _supervisor(_servicio(None)),
    ],
    ids=["sin_supervisor", "sin_servicio", "servicio_sin_atributo", "sin_muestra"],
)
def test_sin_de_donde_leer_el_progreso_la_lectura_responde_igual_y_sin_progreso(supervisor: object) -> None:
    estado = leer_estado_de_la_etapa9(supervisor=supervisor, consultar_cuarentena=None, ahora=AHORA)

    assert estado.progreso is None


def test_un_servicio_que_falla_al_leer_el_progreso_no_rompe_la_gui(caplog: pytest.LogCaptureFixture) -> None:
    class _Roto:
        @property
        def progreso_actual(self) -> object:
            raise RuntimeError("se cayó")

    with caplog.at_level(logging.WARNING):
        estado = leer_estado_de_la_etapa9(supervisor=_supervisor(_Roto()), consultar_cuarentena=None, ahora=AHORA)

    assert estado.progreso is None
    assert any("progreso" in r.getMessage() for r in caplog.records)


def test_la_cuarentena_se_informa_aunque_no_haya_corrida_en_curso() -> None:
    estado = leer_estado_de_la_etapa9(
        supervisor=_supervisor(None), consultar_cuarentena=lambda: dict(_CUARENTENA), ahora=AHORA
    )

    assert estado.cuarentena == _CUARENTENA


def test_sin_cuarentena_no_se_informa_nada() -> None:
    estado = leer_estado_de_la_etapa9(supervisor=_supervisor(None), consultar_cuarentena=lambda: None, ahora=AHORA)

    assert estado.cuarentena is None


def test_una_consulta_de_cuarentena_que_falla_no_rompe_la_gui(caplog: pytest.LogCaptureFixture) -> None:
    def consulta_rota() -> dict[str, Any] | None:
        raise OSError("sin acceso")

    with caplog.at_level(logging.WARNING):
        estado = leer_estado_de_la_etapa9(supervisor=_supervisor(None), consultar_cuarentena=consulta_rota, ahora=AHORA)

    assert estado.cuarentena is None
    assert any("cuarentena" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------ el texto


def test_las_lineas_de_progreso_dicen_hechos_y_la_edad_de_la_muestra() -> None:
    lineas = lineas_de_progreso(progreso_en_hechos(_muestra(), ahora=AHORA))

    texto = "\n".join(lineas)
    assert "DynDOLOD" in texto
    assert "1 h 02 min" in texto  # 3725 s
    assert "8,0 KiB" in texto
    assert "41 s" in texto  # sin cambios hace
    assert "30 s" in texto  # edad de la muestra


def test_sin_log_medible_el_texto_lo_declara() -> None:
    texto = "\n".join(
        lineas_de_progreso(
            progreso_en_hechos(_muestra(log_path=None, log_bytes=None, log_idle_seconds=None), ahora=AHORA)
        )
    )

    assert "sin log" in texto.lower()


@pytest.mark.parametrize(
    "cambios",
    [
        {},
        {"elapsed_seconds": 5.0},
        {"elapsed_seconds": 59.9},
        {"elapsed_seconds": 7200.0, "log_bytes": 5 * 1024 * 1024 * 1024},
        {"log_idle_seconds": 0.0},
        {"log_idle_seconds": 86_400.0},
        {"log_path": None, "log_bytes": None, "log_idle_seconds": None},
    ],
)
def test_el_texto_de_progreso_nunca_emite_un_veredicto_de_colgado(cambios: dict[str, object]) -> None:
    """Son hechos: el umbral de «sin actividad» lo juzga quien mira, con la edad de la muestra a la vista."""
    texto = "\n".join(lineas_de_progreso(progreso_en_hechos(_muestra(**cambios), ahora=AHORA))).lower()

    assert not any(palabra in texto for palabra in ("colgad", "congelad", "trabad", "frozen", "hung", "stuck"))


def test_el_texto_de_cuarentena_dice_que_esta_aislada_por_que_y_como_liberarla() -> None:
    texto = texto_de_cuarentena(_CUARENTENA)

    assert "cuarentena" in texto.lower()
    assert "terminalidad desconocida" in texto
    assert _CUARENTENA["como_liberar"] in texto


def test_el_texto_de_cuarentena_incluye_el_procedimiento_real_del_modulo_de_cuarentena() -> None:
    real = {"activa": True, "razon": "x", "como_liberar": vfs_quarantine.como_liberar(pathlib.Path("D:/MO2"))}

    assert vfs_quarantine.como_liberar(pathlib.Path("D:/MO2")) in texto_de_cuarentena(real)


# ------------------------------------------------------------------------------------------- las anclas


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _produccion() -> list[pathlib.Path]:
    return sorted((RAIZ / "sky_claw").rglob("*.py"))


def _llamadas_a(arbol: ast.AST, nombre: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == nombre)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
        )
    ]


def test_los_hechos_de_progreso_se_arman_en_un_solo_modulo_y_los_dos_consumidores_lo_llaman() -> None:
    """Quien escribe la clave ``log_sin_cambios_segundos`` es ``dyndolod_progreso``; el formateador de texto la LEE."""
    con_la_clave = {
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _produccion()
        if any(isinstance(n, ast.Constant) and n.value == "log_sin_cambios_segundos" for n in ast.walk(_arbol(ruta)))
    }
    assert con_la_clave == {
        "sky_claw/local/tools/dyndolod_progreso.py",
        "sky_claw/app/gui/controllers/etapa9_estado.py",
    }
    for consumidor in ("sky_claw/app/agent/tools/lod_tools.py", "sky_claw/app/gui/controllers/etapa9_estado.py"):
        assert _llamadas_a(_arbol(RAIZ / consumidor), "progreso_en_hechos"), consumidor


def test_el_panel_de_estado_de_la_etapa_9_se_refresca_con_el_timer_del_dashboard() -> None:
    forge = _arbol(RAIZ / "sky_claw" / "app" / "gui" / "views" / "forge_dashboard.py")
    timers = [c for c in _llamadas_a(forge, "timer")]

    assert any(_llamadas_a(timer, "refresh") and "_etapa9_estado_panel" in ast.dump(timer) for timer in timers), (
        "el timer vivo del dashboard tiene que refrescar el panel de la etapa 9"
    )


def test_la_gui_cablea_el_callback_de_estado_a_la_unica_lectura() -> None:
    gui = _arbol(RAIZ / "sky_claw" / "app" / "gui" / "sky_claw_gui.py")
    asignaciones = [
        n
        for n in ast.walk(gui)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "on_etapa9_estado"
            for t in n.targets
        )
    ]

    assert len(asignaciones) == 1
    assert len(_llamadas_a(asignaciones[0], "leer_estado_de_la_etapa9")) == 1


def test_el_contexto_publica_la_consulta_de_cuarentena_para_las_dos_superficies() -> None:
    """La GUI y el agente piden la cuarentena por el MISMO método público del contexto (no por uno privado)."""
    from sky_claw.app_context import AppContext

    assert callable(getattr(AppContext, "estado_de_cuarentena_vfs", None))
    assert not hasattr(AppContext, "_estado_de_cuarentena_vfs")
