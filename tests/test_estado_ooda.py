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
from tests.test_rollback_salida import MECANISMO_DE_ROLLBACK, MOTIVO

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INVENTARIO = _RAIZ / "docs" / "pending_ooda_status.md"
_ESTADOS = {"Cerrado", "Parcial", "Abierto", "Bloqueado (rig humano)"}
_COLUMNAS = ["Ítem", "Estado", "Cerrado en", "Qué falta", "Verificado por"]
_ITEMS = frozenset(
    {
        "T-10",
        "T-11",
        "T-12",
        "T-16c",
        "T-22",
        "T-23",
        "T-24",
        "T-25",
        "T-26",
        "T-27",
        "T-28",
        "T-29",
        "T-30",
        *(f"U-{numero:02d}" for numero in range(1, 13)),
        *(f"F{numero}" for numero in range(1, 10)),
        "F8 USVFS",
        "Detección de enlaces",
        "Borrado recursivo",
        "Smoke real de QuickAutoClean",
        "Smokes reales restantes",
        "Residuos OODA de bajo valor",
        "Residuos de crash logging",
    }
)


def _celdas(linea: str) -> list[str]:
    """Normaliza el espaciado exterior de una fila Markdown."""
    return [celda.strip() for celda in linea.strip().strip("|").split("|")]


def _tabla() -> dict[str, dict[str, str]]:
    """Lee la tabla de estado sin acoplarla al espaciado Markdown."""
    lineas = _INVENTARIO.read_text(encoding="utf-8").splitlines()
    inicio = next(indice for indice, linea in enumerate(lineas) if _celdas(linea) == _COLUMNAS)
    filas: dict[str, dict[str, str]] = {}
    for linea in lineas[inicio + 2 :]:
        if not linea.strip().startswith("|"):
            break
        valores = _celdas(linea)
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
    assert set(filas) == _ITEMS


def test_el_parser_tolera_espacios_exteriores_en_la_tabla() -> None:
    assert _celdas("  | Ítem | Estado | Cerrado en | Qué falta | Verificado por |   ") == _COLUMNAS


def test_u04_sigue_parcial_hasta_bodyslide_y_el_smoke_real_de_pandora() -> None:
    fila = _tabla()["U-04"]
    pendientes = {modulo for modulo, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo == "pendiente"}
    modulo_bodyslide = "sky_claw/app/agent/tools/system_tools.py"

    assert pendientes == {modulo_bodyslide}
    assert "BodySlide" in MOTIVO[modulo_bodyslide]
    assert fila["Estado"] == "Parcial"
    assert "BodySlide" in fila["Qué falta"]
    assert "smoke real de rollback de Pandora" in fila["Qué falta"]
    assert "test_rollback_salida.py" in fila["Verificado por"]
    assert "test_pandora_service.py" in fila["Verificado por"]


def test_pandora_documenta_la_salida_administrada_sin_cerrar_el_sandbox() -> None:
    sandbox = (_RAIZ / "sky_claw" / "local" / "mo2" / "sandbox_run.py").read_text(encoding="utf-8")
    adr = (_RAIZ / "docs" / "adr" / "0005-sandbox-promocion-sincrona-hitl.md").read_text(encoding="utf-8")

    for texto in (sandbox, adr):
        assert "--output" in texto
        assert "Pandora_Output" in texto
        assert "sandbox" in texto.lower()
        assert "USVFS" in texto
    assert "supersedida" in adr.lower()


def test_t25_nombra_t27_antes_del_rig_humano() -> None:
    fila = _tabla()["T-25"]
    que_falta = fila["Qué falta"]

    assert fila["Estado"] == "Parcial"
    assert "T-27" in que_falta
    assert que_falta.index("T-27") < que_falta.index("matriz E2E")
    assert "TECHNICAL_REVIEW_TASKS.md" in fila["Verificado por"]


def test_t27_sigue_parcial_hasta_aislar_los_mutadores_restantes() -> None:
    fila = _tabla()["T-27"]

    assert fila["Estado"] == "Parcial"
    assert "USVFS" in fila["Qué falta"]
    assert "Pandora" in fila["Qué falta"]
    assert "DynDOLOD" in fila["Qué falta"]
    assert "Wrye Bash" in fila["Qué falta"]
    assert "test_pandora_service.py" in fila["Verificado por"]


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

    for item in ("Smoke real de QuickAutoClean", "Smokes reales restantes"):
        assert filas[item]["Estado"] == "Bloqueado (rig humano)"
        assert filas[item]["Verificado por"] == "humano"
