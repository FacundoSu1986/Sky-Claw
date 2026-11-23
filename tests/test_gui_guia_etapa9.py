"""#997 — lo que ve el operador en la GUI: checklist antes de arrancar, el paso actual y el aviso en criollo.

Mismo estilo que el resto de la GUI: la lógica de display vive en funciones puras (view-models) y lo que se prueba es
eso; el glue de NiceGUI queda fino. La GUI NO decide nada: la autoridad del checklist es el servicio. Lo único que la
vista agrega es lo que solo ella sabe: que su modo Desatendido no tiene override de etapas previas (#9), así que con
etapas faltantes ofrece pasar a Asistido en vez de dejar apretar un botón que el dispatcher va a rechazar.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sky_claw import config as config_mod
from sky_claw.app.gui.controllers.etapa9_estado import (
    STORE_KEY_CHECKLIST_ETAPA9,
    leer_estado_de_la_etapa9,
    revisar_checklist,
)
from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views.sections import guia_etapa9
from sky_claw.app.gui.views.sections.guia_etapa9 import (
    RECORDATORIO_DE_LA_PESTANA,
    vista_de_checklist,
    vista_de_fase,
)
from sky_claw.local.tools import dyndolod_guia as guia
from sky_claw.local.tools.dyndolod_guia import CambioDeFase, ChecklistDeEtapa9, Estado, FaseAsistida, IdPrecondicion

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _checklist(*resultados: guia.ResultadoDePrecondicion) -> dict:
    return ChecklistDeEtapa9(resultados=resultados).to_dict()


# ---------------------------------------------------------------------------
# El aviso del resultado (toast): criollo primero, sin tocar la vista del toast (#996)
# ---------------------------------------------------------------------------


def test_el_aviso_de_un_fallo_con_guia_es_el_criollo_sin_el_tecnico() -> None:
    resultado = {
        "success": False,
        "message": "preflight UIA de Output bloqueó a TexGen: UNKNOWN (ERROR_UIA) — (-2147220991, 'Un evento...')",
        "guia": {
            "titulo": "Sky-Claw no pudo leer la ventana de TexGen.",
            "arreglo": "Dejá la ventana visible y volvé a correr.",
            "detalle_tecnico": "preflight UIA…",
        },
    }
    texto, tipo = summarize_ritual_result("dyndolod", resultado)
    assert texto == "Sky-Claw no pudo leer la ventana de TexGen. Dejá la ventana visible y volvé a correr."
    assert tipo == "negative"
    assert "ERROR_UIA" not in texto


def test_sin_guia_el_aviso_no_cambia() -> None:
    texto, tipo = summarize_ritual_result("loot", {"success": False, "message": "boom"})
    assert (texto, tipo) == ("El ritual «loot» falló: boom", "negative")


# ---------------------------------------------------------------------------
# Checklist antes de arrancar
# ---------------------------------------------------------------------------


def test_un_bloqueante_deshabilita_el_boton_y_dice_que_arreglar() -> None:
    vista = vista_de_checklist(
        _checklist(
            guia.resultado(IdPrecondicion.HERRAMIENTAS_FUERA_DEL_JUEGO, Estado.FALLA, detalle=r"C:\Skyrim\DynDOLOD"),
            guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),
        ),
        modo="asistido",
    )
    assert vista["puede_arrancar"] is False
    assert vista["boton"] == "Primero arreglá lo que falta"
    assert [item["marca"] for item in vista["items"]] == ["falta", "listo"]
    assert vista["items"][0]["detalle_tecnico"] == r"C:\Skyrim\DynDOLOD"


def test_los_avisos_no_frenan_y_se_ven_como_aviso() -> None:
    vista = vista_de_checklist(
        _checklist(guia.resultado(IdPrecondicion.DLL_NG_Y_MASTERS, Estado.FALLA, detalle="dll")), modo="asistido"
    )
    assert vista["puede_arrancar"] is True
    assert vista["items"][0]["marca"] == "ojo"


def test_lo_no_verificable_se_muestra_sin_frenar() -> None:
    vista = vista_de_checklist(
        _checklist(guia.resultado(IdPrecondicion.OUTPUT_DEL_PRESET, Estado.NO_VERIFICABLE, detalle="sin contrato")),
        modo="asistido",
    )
    assert vista["puede_arrancar"] is True
    assert vista["items"][0]["marca"] == "sin_revisar"
    assert vista["items"][0]["titulo"].startswith("No pude revisar")
    assert vista["items"][0]["arreglo"] == "", "no se le pide arreglar algo que no se comprobó que falle"


def test_una_fila_que_paso_no_afirma_la_falla() -> None:
    """Review Codex en #1007: con todo bien, la fila decía «✓ Listo · No encuentro DynDOLOD»."""
    vista = vista_de_checklist(
        _checklist(guia.resultado(IdPrecondicion.DYNDOLOD_ENCONTRADO, Estado.OK)), modo="asistido"
    )
    assert vista["items"][0]["titulo"] == "DynDOLOD está configurado y lo encuentro."
    assert vista["items"][0]["arreglo"] == ""


def test_cada_marca_lleva_una_palabra_ademas_del_color_y_su_glifo_es_del_tema() -> None:
    """El operador no tiene que deducir nada del color; y el shell no usa emojis (inventario de glifos del tema)."""
    from tests.test_gui_theme_contracts import _glifo_permitido

    assert set(guia_etapa9._MARCAS) == {"listo", "falta", "ojo", "sin_revisar"}  # noqa: SLF001
    for marca in guia_etapa9._MARCAS.values():  # noqa: SLF001
        assert marca["palabra"].strip()
    fuente = pathlib.Path(guia_etapa9.__file__).read_text(encoding="utf-8")
    intrusos = sorted({c for c in fuente if not _glifo_permitido(ord(c))})
    assert not intrusos, f"glifos fuera del alfabeto del tema en guia_etapa9.py: {intrusos}"


def test_desatendido_con_etapas_faltantes_ofrece_pasar_a_asistido() -> None:
    """#9: el dispatcher rechaza el desatendido sin etapas 6–8 y la GUI no tiene override: se ofrece el asistido."""
    checklist = _checklist(guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.FALLA, detalle="etapa 6", etapas="6"))
    desatendido = vista_de_checklist(checklist, modo="desatendido")
    asistido = vista_de_checklist(checklist, modo="asistido")

    assert desatendido["puede_arrancar"] is False
    assert desatendido["ofrecer_asistido"] is True
    assert asistido["puede_arrancar"] is True, "en asistido la confirmación de etapas previas (D-3) decide"
    assert asistido["ofrecer_asistido"] is False


def test_sin_revisar_todavia_no_se_frena_porque_la_autoridad_es_el_servicio() -> None:
    vista = vista_de_checklist(None, modo="asistido")
    assert vista["puede_arrancar"] is True
    assert vista["items"] == []


def test_el_recordatorio_de_la_pestana_usa_el_plazo_real_del_watchdog() -> None:
    """#2: una sola fuente para el plazo; si cambia la config, cambia el texto."""
    assert f"{int(config_mod.GUI_EXIT_WATCHDOG_GRACE_SECONDS)} segundos" in RECORDATORIO_DE_LA_PESTANA
    assert "pestaña abierta" in RECORDATORIO_DE_LA_PESTANA


def test_el_lanzador_avisa_el_plazo_real_de_la_primera_conexion() -> None:
    """#2: el .exe no tiene consola; el .bat es el único lugar donde se lee ANTES de que la GUI se cierre sola."""
    bat = (RAIZ / "SkyClawApp.bat").read_text(encoding="utf-8")
    assert f"Si en {int(config_mod.GUI_EXIT_WATCHDOG_FIRST_CONNECT_SECONDS)} segundos no hay ninguna pestana" in bat


def test_la_guia_no_es_un_aviso_con_cruz() -> None:
    """La guía no se descarta: no entra en la familia de avisos con ``×`` de #996."""
    fuente = pathlib.Path(guia_etapa9.__file__).read_text(encoding="utf-8")
    assert "&times;" not in fuente
    assert "×" not in fuente


# ---------------------------------------------------------------------------
# El paso actual durante la corrida
# ---------------------------------------------------------------------------


def test_la_vista_del_paso_dice_que_hacer_y_hace_cuanto() -> None:
    hechos = {
        "herramienta": "TexGen",
        "fase": "verificando_output",
        "texto": "Paso 3 de 4 · no toques Start",
        "hace_segundos": 75,
    }
    vista = vista_de_fase(hechos)
    assert vista["texto"] == "Paso 3 de 4 · no toques Start"
    assert vista["hace"] == "hace 1 min 15 s"
    assert vista["alerta"] is True, "mientras Sky-Claw verifica, el banner se ve como alerta"


async def test_el_estado_de_la_etapa9_lee_la_fase_del_servicio() -> None:
    servicio = SimpleNamespace(progreso_actual=None, fase_actual=None)
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    real = DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=AsyncMock(),
    )
    await real._al_cambiar_de_fase(CambioDeFase(tool_name="TexGen", fase=FaseAsistida.ABRIENDO_VENTANA))  # noqa: SLF001
    servicio.fase_actual = real.fase_actual

    estado = leer_estado_de_la_etapa9(
        supervisor=SimpleNamespace(dyndolod_service=servicio),
        consultar_cuarentena=None,
        ahora=real.fase_actual.desde + 3,  # type: ignore[union-attr]
    )

    assert estado.fase is not None
    assert estado.fase["fase"] == "abriendo_ventana"
    assert estado.fase["hace_segundos"] == 3


# ---------------------------------------------------------------------------
# La revisión del checklist desde la GUI (en segundo plano, best-effort)
# ---------------------------------------------------------------------------


async def test_revisar_checklist_guarda_el_dict_del_servicio_en_el_store() -> None:
    checklist = ChecklistDeEtapa9(resultados=(guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),))
    servicio = MagicMock()
    servicio.verificar_checklist = AsyncMock(return_value=checklist)
    store = ReactiveStore()

    await revisar_checklist(supervisor=SimpleNamespace(dyndolod_service=servicio), store=store, run_texgen=True)

    assert store.get(STORE_KEY_CHECKLIST_ETAPA9) == checklist.to_dict()
    servicio.verificar_checklist.assert_awaited_once_with(run_texgen=True)


async def test_revisar_checklist_sin_servicio_o_con_falla_no_rompe_y_lo_dice() -> None:
    store = ReactiveStore()
    await revisar_checklist(supervisor=None, store=store, run_texgen=True)
    assert store.get(STORE_KEY_CHECKLIST_ETAPA9) is None

    servicio = MagicMock()
    servicio.verificar_checklist = AsyncMock(side_effect=RuntimeError("disco caído"))
    await revisar_checklist(supervisor=SimpleNamespace(dyndolod_service=servicio), store=store, run_texgen=True)
    assert store.get(STORE_KEY_CHECKLIST_ETAPA9) == {"error": "No pude revisar la lista. Probá «Revisar de nuevo»."}


def test_la_clave_del_checklist_solo_la_escribe_su_controlador() -> None:
    """Un escritor nuevo de la clave (p. ej. la vista) rompería el «la GUI no decide»."""
    escritores = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8", errors="replace"))
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "set"
                and nodo.args
                and isinstance(nodo.args[0], ast.Name)
                and nodo.args[0].id == "STORE_KEY_CHECKLIST_ETAPA9"
            ):
                escritores.add(ruta.relative_to(RAIZ).as_posix())
    assert escritores == {"sky_claw/app/gui/controllers/etapa9_estado.py"}


async def test_una_revision_vieja_que_termina_tarde_no_pisa_a_la_nueva() -> None:
    """Review CodeRabbit en #1007: dos revisiones solapadas; gana la última que EMPEZÓ, no la última que terminó."""
    import asyncio

    vieja = ChecklistDeEtapa9(resultados=(guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.FALLA, detalle="v"),))
    nueva = ChecklistDeEtapa9(resultados=(guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),))
    soltar_la_vieja = asyncio.Event()

    async def _verificar(*, run_texgen: bool) -> ChecklistDeEtapa9:
        if not soltar_la_vieja.is_set():
            soltar_la_vieja.set()
            await asyncio.sleep(0.05)
            return vieja
        return nueva

    servicio = MagicMock()
    servicio.verificar_checklist = _verificar
    supervisor = SimpleNamespace(dyndolod_service=servicio)
    store = ReactiveStore()

    primera = asyncio.create_task(revisar_checklist(supervisor=supervisor, store=store, run_texgen=True))
    await asyncio.sleep(0)
    await revisar_checklist(supervisor=supervisor, store=store, run_texgen=True)
    await primera

    assert store.get(STORE_KEY_CHECKLIST_ETAPA9) == nueva.to_dict()
