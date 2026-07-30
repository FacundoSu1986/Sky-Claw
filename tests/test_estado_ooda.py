"""Contrato ejecutable del inventario vivo de pendientes OODA.

El documento no es fuente canónica por sí solo: estas pruebas cruzan sus filas
con las anclas de familia que enumeran el código productivo.
"""

from __future__ import annotations

import pathlib

from tests.test_borrado_recursivo import MECANISMO_DE_BORRADO
from tests.test_rollback_reconciler import (
    PRODUCTORES_DEL_NOMBRE,
    RECONCILIADORES_DE_ARRANQUE,
)
from tests.test_rollback_salida import MECANISMO_DE_ROLLBACK

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INVENTARIO = _RAIZ / "docs" / "pending_ooda_status.md"
_ESTADOS = {"Cerrado", "Parcial", "Abierto", "Bloqueado (rig humano)"}
_COLUMNAS = ["Ítem", "Estado", "Cerrado en", "Qué falta", "Verificado por"]


def _tabla() -> dict[str, dict[str, str]]:
    lineas = _INVENTARIO.read_text(encoding="utf-8").splitlines()
    cabecera = "| " + " | ".join(_COLUMNAS) + " |"
    inicio = lineas.index(cabecera)
    filas: dict[str, dict[str, str]] = {}
    for linea in lineas[inicio + 2 :]:
        if not linea.startswith("|"):
            break
        valores = [celda.strip() for celda in linea.strip("|").split("|")]
        assert len(valores) == len(_COLUMNAS), linea
        fila = dict(zip(_COLUMNAS, valores, strict=True))
        assert fila["Ítem"] not in filas
        filas[fila["Ítem"]] = fila
    return filas


def test_la_tabla_tiene_estados_cerrados_y_anclas_externas() -> None:
    texto = _INVENTARIO.read_text(encoding="utf-8")
    filas = _tabla()

    assert {fila["Estado"] for fila in filas.values()} <= _ESTADOS
    assert "## 2.3 Zero-Trust" in texto
    assert "## Decide — recomendación de próximo frente" in texto
    assert "audits/2026-07_historial_ooda.md" in texto
    assert {f"U-{numero:02d}" for numero in range(1, 13)} <= set(filas)
    assert {f"F{numero}" for numero in range(1, 10)} <= set(filas)


def test_u04_no_puede_figurar_cerrado_mientras_haya_un_mutador_pendiente() -> None:
    fila = _tabla()["U-04"]
    pendientes = {modulo for modulo, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo == "pendiente"}

    assert pendientes
    assert fila["Estado"] == "Parcial"
    assert "BodySlide" in fila["Qué falta"]
    assert "test_rollback_salida.py" in fila["Verificado por"]


def test_u08_cerrado_se_apoya_en_el_reconciliador_enumerativo() -> None:
    fila = _tabla()["U-08"]

    assert PRODUCTORES_DEL_NOMBRE
    assert RECONCILIADORES_DE_ARRANQUE
    assert fila["Estado"] == "Cerrado"
    assert "test_rollback_reconciler.py" in fila["Verificado por"]


def test_deteccion_de_enlaces_delega_en_un_solo_modulo() -> None:
    fila = _tabla()["Detección de enlaces"]
    ancla = (_RAIZ / "tests" / "test_links.py").read_text(encoding="utf-8")

    assert 'implementan == {"sky_claw/app/security/links.py"}' in ancla
    assert fila["Estado"] == "Cerrado"
    assert "test_links.py" in fila["Verificado por"]


def test_borrado_recursivo_no_puede_cerrarse_con_modulos_pendientes() -> None:
    fila = _tabla()["Borrado recursivo"]
    pendientes = {modulo for modulo, mecanismo in MECANISMO_DE_BORRADO.items() if mecanismo == "pendiente"}

    assert not pendientes
    assert fila["Estado"] == "Cerrado"
    assert "test_borrado_recursivo.py" in fila["Verificado por"]


def test_los_smokes_sin_ancla_automatizable_declaran_verificacion_humana() -> None:
    filas = _tabla()

    for item in ("T-25", "Smoke real de QuickAutoClean"):
        assert filas[item]["Estado"] == "Bloqueado (rig humano)"
        assert filas[item]["Verificado por"] == "humano"
