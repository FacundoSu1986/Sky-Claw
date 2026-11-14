"""Seams puros de las pantallas del shell Forge (Descargas, Ajustes, sidebar).

Dos propiedades:

- *una solicitud HITL se dibuja igual en todas las superficies que la muestran*:
  el modal global y la Puerta de Descargas comparten ``_hitl_solicitud_html``
  (antes cada uno armaba su HTML y el modal no mostraba la URL de la descarga);
  el ancla enumera los llamadores.
- *la GUI no afirma lo que no sabe*: el entorno y el perfil se pintan desde lo
  que el escaneo y la sesión realmente tienen, y sin dato no se inventa uno.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.gui.views import forge_dashboard as fd
from sky_claw.local.discovery.environment import (
    EnvironmentSnapshot,
    MissingTool,
    MO2Info,
    SkyrimEdition,
    SkyrimInfo,
    ToolInfo,
)

_GUI = pathlib.Path(fd.__file__).resolve().parent.parent


# ── Solicitud HITL compartida ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("pending", "rotulo"),
    [
        ({"category": "download", "reason": "r", "url": "https://x/y.zip"}, "DESCARGA DE RED"),
        # Envelope legacy sin categoría: la URL basta para tratarlo como descarga.
        ({"reason": "r", "url": "https://x/y.zip"}, "DESCARGA DE RED"),
        ({"category": "tool_execution", "reason": "r"}, "ACCIÓN SOBRE TU INSTALACIÓN"),
        ({"category": "sandbox_promotion", "reason": "r"}, "ACCIÓN SOBRE TU INSTALACIÓN"),
        ({"reason": "r"}, "ACCIÓN SOBRE TU INSTALACIÓN"),
    ],
)
def test_la_solicitud_rotula_su_categoria(pending: dict[str, str], rotulo: str) -> None:
    html = fd._hitl_solicitud_html(pending)
    assert rotulo in html
    assert ("DESCARGA DE RED" in html) is (rotulo == "DESCARGA DE RED")


def test_la_solicitud_muestra_motivo_detalle_y_origen_escapados() -> None:
    html = fd._hitl_solicitud_html(
        {
            "category": "download",
            "reason": "<b>motivo</b>",
            "detail": "<i>detalle</i>",
            "url": 'https://x/y.zip?a="1"&b=<2>',
        }
    )
    assert "&lt;b&gt;motivo&lt;/b&gt;" in html
    assert "&lt;i&gt;detalle&lt;/i&gt;" in html
    assert "https://x/y.zip?a=&quot;1&quot;&amp;b=&lt;2&gt;" in html
    assert "<b>" not in html and "<i>" not in html


def test_sin_detalle_ni_url_no_hay_caja_tecnica_y_el_motivo_tiene_default() -> None:
    html = fd._hitl_solicitud_html({"category": "tool_execution"})
    assert "Esta acción requiere tu aprobación." in html
    assert "Spline Sans Mono',monospace; font-size:11px" not in html


@pytest.mark.parametrize(
    ("pending", "fragmento"),
    [
        # Sólo se sugiere un atajo donde el atajo EXISTE para esa categoría.
        ({"category": "tool_execution"}, "Modo local"),
        ({"category": "download", "url": "https://x"}, "descargas"),
        ({"url": "https://x"}, "descargas"),
        # Estas nunca se auto-aprueban: prometer «Modo local» sería mentirle al operador.
        ({"category": "sandbox_promotion"}, None),
        ({"category": "dyndolod_configuracion_lista"}, None),
        ({"category": "dyndolod_etapas_previas_asistido"}, None),
        ({}, None),
    ],
)
def test_el_tip_del_modal_solo_promete_atajos_que_existen(pending: dict[str, str], fragmento: str | None) -> None:
    tip = fd._hitl_tip(pending)
    if fragmento is None:
        assert tip == ""
    else:
        assert fragmento in tip and "Ajustes → Aprobaciones" in tip


def _llamadores(nombre: str, archivo: str) -> set[str]:
    arbol = ast.parse((_GUI / archivo).read_text(encoding="utf-8"))
    return {
        funcion.name
        for funcion in ast.walk(arbol)
        if isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef) and funcion.name != nombre
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre
    }


def test_modal_y_puerta_de_descargas_dibujan_la_misma_solicitud() -> None:
    assert _llamadores("_hitl_solicitud_html", "views/forge_dashboard.py") == {
        "_hitl_modal_panel",
        "_downloads_screen",
    }


# ── Entorno detectado (Ajustes) ─────────────────────────────────────────────────
def test_sin_escaneo_no_se_afirma_nada_del_entorno() -> None:
    assert fd._entorno_secciones(None) == []
    assert fd._entorno_secciones(object()) == []


def test_el_entorno_lista_lo_que_el_escaneo_encontro() -> None:
    snapshot = EnvironmentSnapshot(
        skyrim=SkyrimInfo(
            path=pathlib.Path("D:/Skyrim"), exe_name="SkyrimSE.exe", edition=SkyrimEdition.AE, version=""
        ),
        mo2=MO2Info(path=pathlib.Path("D:/MO2"), profiles=["Default", "Pruebas"]),
        tools={"loot": ToolInfo(name="LOOT", exe_path=pathlib.Path("D:/Tools/LOOT/loot.exe"))},
        missing=[
            MissingTool(name="Pandora", technical_name="Pandora", friendly_description="x", download_url="https://p")
        ],
    )

    secciones = dict(fd._entorno_secciones(snapshot))

    assert secciones["SKYRIM"] == [
        ("Edición", SkyrimEdition.AE.value),
        ("Versión", "No detectada"),
        ("Ruta", str(pathlib.Path("D:/Skyrim"))),
    ]
    assert secciones["MOD ORGANIZER 2"] == [
        ("Ruta", str(pathlib.Path("D:/MO2"))),
        ("Perfiles", "Default, Pruebas"),
    ]
    assert secciones["HERRAMIENTAS"] == [
        ("LOOT", str(pathlib.Path("D:/Tools/LOOT/loot.exe"))),
        ("Pandora", "No detectada"),
    ]


def test_lo_no_detectado_se_dice_no_se_omite() -> None:
    secciones = dict(fd._entorno_secciones(EnvironmentSnapshot()))
    assert secciones["SKYRIM"] == [("Estado", "No detectado")]
    assert secciones["MOD ORGANIZER 2"] == [("Estado", "No detectado")]
    assert secciones["HERRAMIENTAS"] == [("Estado", "Ninguna detectada")]


def test_el_entorno_se_dibuja_escapado() -> None:
    html = fd._entorno_html([("SKYRIM", [("Ruta", "D:/<Skyrim>&co")])])
    assert "D:/&lt;Skyrim&gt;&amp;co" in html
    assert fd._entorno_html([]) == ""


# ── Perfil de MO2 de la sesión (sidebar) ────────────────────────────────────────
@pytest.mark.parametrize("perfil", [None, "", "   "])
def test_sin_perfil_de_sesion_no_se_dibuja_nada(perfil: str | None) -> None:
    assert fd._perfil_mo2_html(perfil) == ""


def test_el_perfil_de_sesion_se_muestra_escapado() -> None:
    html = fd._perfil_mo2_html("Mi <perfil>")
    assert "PERFIL MO2" in html
    assert "Mi &lt;perfil&gt;" in html


def test_el_perfil_sale_de_la_sesion_y_no_del_escaneo() -> None:
    """El perfil que se muestra es el que los rituales USAN (``app_context.mo2_profile``).

    El ``active_profile`` del escaneo de arranque puede diferir del de la sesión
    (ver test_perfil_sesion_ritual_gui.py): mostrarlo le nombraría al operador un
    perfil sobre el que la GUI no opera.
    """
    arbol = ast.parse((_GUI / "sky_claw_gui.py").read_text(encoding="utf-8"))
    fuentes = [
        ast.unparse(kw.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        for kw in nodo.keywords
        if kw.arg == "perfil_mo2"
    ]
    assert fuentes == ["getattr(runtime.app_context, 'mo2_profile', None)"]
    assert "active_profile" not in (_GUI / "views" / "forge_dashboard.py").read_text(encoding="utf-8")
