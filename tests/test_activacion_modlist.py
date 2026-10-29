"""Contrato del helper compartido que activa mods en el ``modlist.txt`` del perfil.

``activar_en_modlist`` (``sky_claw/local/mo2/activacion_modlist.py``) es el ÚNICO sitio que decide
si un lote de mods recién instalados quedó activado. Lo consumen las dos superficies que
instalan bajo ``mods/``: el agente LLM (``setup_tools``) y el ritual de la GUI
(``run_ritual_install``). Repartir esa decisión fue el defecto que dejó a NGIO reportando éxito
sin activar mientras Community Shaders ya lo había corregido.

Acá se prueba el contrato del helper aislado; que las DOS superficies lo usen (y que nadie
reimplemente el bucle) lo ancla ``tests/test_cs_activacion_modlist.py`` por AST.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
from typing import Any, get_args

import pytest

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.activacion_modlist import (
    EstadoActivacion,
    ResultadoActivacion,
    activar_en_modlist,
)
from sky_claw.local.mo2.vfs import MO2Controller

PERFIL = "Sesion"
NOMBRES = ("Address Library for SKSE Plugins", "SSE Engine Fixes", "Community Shaders")


class _Mo2Falso:
    """Doble mínimo de ``MO2Controller``: registra llamadas y puede fallar en UN mod."""

    def __init__(self, falla_en: str | None = None, error: BaseException | None = None) -> None:
        self._falla_en = falla_en
        self._error = error if error is not None else OSError("disco lleno")
        self.llamadas: list[tuple[str, str]] = []

    async def add_mod_to_modlist(self, mod_name: str, profile: str = "Default") -> None:
        if mod_name == self._falla_en:
            raise self._error
        self.llamadas.append((mod_name, profile))


# ── Vocabulario cerrado ──────────────────────────────────────────────────────────
def test_el_vocabulario_de_estados_es_cerrado_y_esta_congelado() -> None:
    """Igualdad literal: un estado nuevo rompe esto hasta que cada superficie decida qué hacer con él."""
    assert get_args(EstadoActivacion) == ("ok", "parcial", "fallo", "no_disponible", "vacio")


# ── Estados ──────────────────────────────────────────────────────────────────────
async def test_activar_todos_devuelve_ok_en_orden_y_con_el_perfil_pedido() -> None:
    mo2 = _Mo2Falso()

    resultado = await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "CS")

    assert resultado.estado == "ok"
    assert resultado.activados == NOMBRES
    assert resultado.exito is True
    assert resultado.mensaje == ""
    assert mo2.llamadas == [(n, PERFIL) for n in NOMBRES]


@pytest.mark.parametrize(
    ("falla_en", "estado", "activados"),
    [
        (1, "parcial", NOMBRES[:1]),  # falla el 2.º: el 1.º ya quedó escrito
        (0, "fallo", ()),  # falla el 1.º: nada quedó escrito
    ],
)
async def test_si_la_escritura_falla_distingue_parcial_de_fallo(
    falla_en: int, estado: str, activados: tuple[str, ...]
) -> None:
    mo2 = _Mo2Falso(falla_en=NOMBRES[falla_en])

    resultado = await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "CS")

    assert resultado.estado == estado
    assert resultado.activados == activados
    assert resultado.exito is False
    assert resultado.error == "disco lleno"


async def test_no_sigue_activando_despues_de_la_primera_falla() -> None:
    """El bucle corta: los mods posteriores a la falla no se tocan (el perfil queda como estaba)."""
    mo2 = _Mo2Falso(falla_en=NOMBRES[0])

    await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "CS")

    assert mo2.llamadas == []


@pytest.mark.parametrize(
    ("mo2_presente", "perfil"),
    [(False, PERFIL), (True, None), (True, "")],
    ids=["sin_mo2", "perfil_none", "perfil_vacio"],
)
async def test_sin_mo2_o_sin_perfil_es_no_disponible_y_no_toca_nada(mo2_presente: bool, perfil: str | None) -> None:
    mo2 = _Mo2Falso() if mo2_presente else None

    resultado = await activar_en_modlist(list(NOMBRES), mo2, perfil, "CS")

    assert resultado.estado == "no_disponible"
    assert resultado.activados == ()
    assert resultado.exito is False
    assert resultado.error == "no hay MO2 o perfil en el contexto de esta sesión"
    if mo2 is not None:
        assert mo2.llamadas == []


async def test_lote_vacio_no_es_exito_vacuo_aunque_haya_mo2_y_perfil() -> None:
    """``[]`` no es «todo activado»: ``0 == 0`` no puede leerse como éxito."""
    mo2 = _Mo2Falso()

    resultado = await activar_en_modlist([], mo2, PERFIL, "CS")

    assert resultado.estado == "vacio"
    assert resultado.exito is False
    assert resultado.error == "el instalador no devolvió componentes"
    assert mo2.llamadas == []


async def test_lote_vacio_gana_sobre_la_falta_de_mo2() -> None:
    """Orden de las guardas heredado del helper original: ``vacio`` se decide antes que ``no_disponible``."""
    resultado = await activar_en_modlist([], None, None, "CS")

    assert resultado.estado == "vacio"


# ── Campos del contrato de tool (success / message) ──────────────────────────────
async def test_como_campos_en_exito_sigue_el_contrato_de_tools() -> None:
    resultado = await activar_en_modlist(list(NOMBRES), _Mo2Falso(), PERFIL, "CS")

    assert resultado.como_campos() == {
        "activados": list(NOMBRES),
        "activacion": "ok",
        "success": True,
        "message": "",
    }


@pytest.mark.parametrize(
    ("mo2", "perfil", "nombres", "mensaje"),
    [
        (
            _Mo2Falso(falla_en=NOMBRES[1]),
            PERFIL,
            NOMBRES,
            "Instalado en disco pero no activado en el perfil (parcial): disco lleno",
        ),
        (
            _Mo2Falso(falla_en=NOMBRES[0]),
            PERFIL,
            NOMBRES,
            "Instalado en disco pero no activado en el perfil (fallo): disco lleno",
        ),
        (
            None,
            PERFIL,
            NOMBRES,
            "Instalado en disco pero no activado en el perfil (no_disponible): "
            "no hay MO2 o perfil en el contexto de esta sesión",
        ),
        (
            _Mo2Falso(),
            PERFIL,
            (),
            "Instalado en disco pero no activado en el perfil (vacio): el instalador no devolvió componentes",
        ),
    ],
    ids=["parcial", "fallo", "no_disponible", "vacio"],
)
async def test_el_mensaje_conserva_el_formato_que_el_agente_ya_emitia(
    mo2: Any, perfil: str, nombres: tuple[str, ...], mensaje: str
) -> None:
    """Texto idéntico al del helper original del agente: lo lee el LLM, no se reformula por prolijidad."""
    resultado = await activar_en_modlist(list(nombres), mo2, perfil, "CS")

    campos = resultado.como_campos()
    assert campos["message"] == mensaje
    assert campos["success"] is False
    assert campos["activacion"] == resultado.estado


async def test_excepcion_sin_texto_no_deja_un_dos_puntos_colgando() -> None:
    """``OSError()`` tiene ``str() == ""``: el mensaje no debe terminar en ``": "``."""
    mo2 = _Mo2Falso(falla_en=NOMBRES[0], error=OSError())

    resultado = await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "CS")

    assert resultado.mensaje == "Instalado en disco pero no activado en el perfil (fallo)"


# ── Fronteras ────────────────────────────────────────────────────────────────────
async def test_la_cancelacion_se_propaga_y_no_se_disfraza_de_fallo() -> None:
    """``CancelledError`` es ``BaseException``: tragarla dejaría una tarea cancelada reportando «fallo»."""
    mo2 = _Mo2Falso(falla_en=NOMBRES[0], error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "CS")


async def test_la_falla_se_loguea_con_el_nombre_de_la_herramienta(caplog: pytest.LogCaptureFixture) -> None:
    mo2 = _Mo2Falso(falla_en=NOMBRES[0])

    with caplog.at_level(logging.ERROR, logger="sky_claw.local.mo2.activacion_modlist"):
        await activar_en_modlist(list(NOMBRES), mo2, PERFIL, "NGIO")

    assert any("NGIO" in r.getMessage() for r in caplog.records)


def test_el_resultado_es_inmutable() -> None:
    resultado = ResultadoActivacion(estado="ok", activados=("A",), error=None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        resultado.estado = "fallo"  # type: ignore[misc]


# ── Con el controlador REAL ──────────────────────────────────────────────────────
async def test_con_el_controlador_real_deshabilita_habilitado_en_su_lugar(tmp_path: pathlib.Path) -> None:
    """El helper llama al ``MO2Controller`` de verdad (firma ``profile=`` incluida), no a un doble.

    ``-Community Shaders`` preexistente se habilita en su lugar: es el escenario de #825 que un doble
    append-only no puede reproducir.
    """
    raiz = tmp_path / "MO2"
    perfil = raiz / "profiles" / PERFIL
    perfil.mkdir(parents=True)
    modlist = perfil / "modlist.txt"
    modlist.write_text("+Otra\n-Community Shaders\n", encoding="utf-8-sig")
    mo2 = MO2Controller(raiz, path_validator=PathValidator(roots=[tmp_path]))

    resultado = await activar_en_modlist(["Address Library for SKSE Plugins", "Community Shaders"], mo2, PERFIL, "CS")

    lineas = [ln.strip() for ln in modlist.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    assert resultado.estado == "ok"
    assert lineas == ["+Otra", "+Community Shaders", "+Address Library for SKSE Plugins"]
