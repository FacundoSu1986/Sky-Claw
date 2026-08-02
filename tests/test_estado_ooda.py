"""Contrato ejecutable del inventario vivo de pendientes OODA.

El documento no es fuente canónica por sí solo: estas pruebas cruzan sus filas
con las anclas de familia que enumeran el código productivo.
"""

from __future__ import annotations

import pathlib
import re

from tests.test_borrado_recursivo import MECANISMO_DE_BORRADO, MECANISMO_DE_MEDICION
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
        "Medición de árboles",
        "Fugas de lifecycle en tests",
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
    deployment = (_RAIZ / "docs" / "operations" / "deployment_standalone_usvfs.md").read_text(encoding="utf-8")

    assert "<game>/Pandora_Output" in sandbox
    assert "<game>/Pandora_Output" in adr
    assert "<juego resuelto>/Pandora_Output" in deployment
    for texto in (sandbox, adr, deployment):
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


def _ruta_del_test_citado(nombre: str) -> pathlib.Path:
    """Resuelve un ``.py`` citado en la tabla, conservando su directorio.

    Acepta tanto el nombre pelado (``test_links.py``, que es como está citada
    hoy toda la tabla) como una ruta con subdirectorio (``bdd/test_x.py``) o con
    el prefijo ``tests/`` explícito.

    **No se colapsa a ``.name``**: quedarse con el nombre del archivo haría que
    un ``tests/test_x.py`` cualquiera satisficiera una cita de
    ``bdd/test_x.py``, y el gate diría que existe un test que no existe — el
    mismo error que este gate existe para atajar, un nivel más abajo. Con
    ``tests/bdd/`` ya en el árbol, la ruta con subdirectorio es una cita
    plausible.
    """
    ruta = pathlib.Path(nombre)
    return _RAIZ / (ruta if ruta.parts[0] == "tests" else "tests" / ruta)


def test_todo_test_citado_como_verificacion_existe() -> None:
    """Ningún ``.py`` citado en «Verificado por» puede ser un archivo inexistente.

    Una fila que se apoya en un test borrado sigue diciendo «Cerrado» y ya no la
    respalda nada: el inventario pasa de consolidar estado a inventarlo, que es
    exactamente lo que este documento existe para no hacer.

    Pasó en #415: la fila de lifecycle citaba ``test_lifecycle_de_la_sesion.py``,
    que el propio PR eliminó al cambiar de un hook defensivo al fix de raíz. Los
    gates de Python siguieron verdes —ninguno lee este documento— y lo atajó un
    revisor automático. Este test lo convierte en rojo.

    Se enumeran **todas** las filas en vez de revisar la que se acaba de tocar:
    un caso escrito a mano para la fila de hoy no ataja a la de mañana.
    """
    citados = {nombre for fila in _tabla().values() for nombre in re.findall(r"[\w/]+\.py", fila["Verificado por"])}
    faltantes = {nombre for nombre in citados if not _ruta_del_test_citado(nombre).is_file()}

    assert not faltantes, f"el inventario cita tests que no existen: {sorted(faltantes)}"


def test_los_smokes_sin_ancla_automatizable_declaran_verificacion_humana() -> None:
    filas = _tabla()

    for item in ("Smoke real de QuickAutoClean", "Smokes reales restantes"):
        assert filas[item]["Estado"] == "Bloqueado (rig humano)"
        assert filas[item]["Verificado por"] == "humano"


def test_medicion_de_arboles_se_apoya_en_el_censo_de_medidores() -> None:
    """La fila sólo cierra con un censo poblado y de vocabulario cerrado.

    El docstring anterior decía que la fila «no puede decir Cerrado con un
    medidor sin clasificar», y la aserción no verificaba eso: un medidor sin
    clasificar no está en el dict, así que no aporta ningún valor y la
    comprobación de vocabulario seguía verde. Afirmaba más de lo que probaba,
    que es el defecto que este archivo persigue.

    La cobertura —que ningún medidor quede afuera— la verifica
    ``test_borrado_recursivo.py::test_la_enumeracion_cubre_a_todo_el_que_mide_arboles``,
    que es a lo que apunta la celda «Verificado por». Acá se ata lo que sí es
    comprobable desde este archivo: que el censo no esté vacío y que todo lo
    declarado ``"sin-contraparte-que-borre"`` efectivamente no borre.
    """
    fila = _tabla()["Medición de árboles"]
    eximidos = {m for m, mecanismo in MECANISMO_DE_MEDICION.items() if mecanismo == "sin-contraparte-que-borre"}

    assert MECANISMO_DE_MEDICION
    assert set(MECANISMO_DE_MEDICION.values()) <= {"link-aware", "sin-contraparte-que-borre"}
    assert not (eximidos & set(MECANISMO_DE_BORRADO))
    assert fila["Estado"] == "Cerrado"
    assert "test_borrado_recursivo.py" in fila["Verificado por"]
