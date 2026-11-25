"""PR 2c-2a: el schema de ``run_bodyslide`` rechaza exactamente lo que rechaza el contrato.

Propiedad: *el schema rechaza exactamente lo que rechaza el contrato*. Antes de 2c-2a,
``BodySlideBatchParams.group`` aceptaba todo lo que ``_SAFE_NAME_PATTERN`` admite
(``CBBE.``, ``CON``, `` CBBE``) y el servicio lo rechazaba después, dentro del lock. Dos
implementaciones de la misma regla divergen; la receta es una sola: el validador del
schema delega en ``bodyslide_headless.validar_nombre_de_grupo``.

El corpus se ENUMERA desde el contrato (cada dispositivo Win32, cada carácter prohibido),
no se muestrea: un dispositivo nuevo en ``_DISPOSITIVOS_WIN32`` entra solo al corpus.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pydantic
import pytest

from sky_claw.app.agent.tools.schemas import BodySlideBatchParams
from sky_claw.local.tools import bodyslide_headless
from sky_claw.local.tools.bodyslide_headless import HeadlessContractError, validar_nombre_de_grupo


def _corpus() -> list[str]:
    """Nombres que ejercitan cada regla del contrato, en sus dos lados."""
    nombres: list[str] = [
        # válidos: punto y espacio interiores, guiones, dígitos
        "CBBE",
        "3BA",
        "CBBE 3BA",
        "Preset 1.0",
        "my-group_2",
        "a",
        "x" * 128,
        # longitud fuera de rango
        "x" * 129,
        # espacios alrededor y punto final (Win32 los descarta)
        " CBBE",
        "CBBE ",
        "CBBE.",
        "CBBE..",
        "CBBE .",
        # segmentos especiales
        ".",
        "..",
        "...",
        # fuera del vocabulario ASCII
        "CBBE$",
        "CBBÉ",
        "grupo\ttab",
    ]
    for caracter in sorted(bodyslide_headless._CARACTERES_PROHIBIDOS_WIN32):
        nombres.append(f"CB{caracter}BE")
    for codigo in range(32):
        nombres.append(f"CB{chr(codigo)}BE")
    for dispositivo in sorted(bodyslide_headless._DISPOSITIVOS_WIN32):
        for variante in (dispositivo, dispositivo.upper(), dispositivo.capitalize()):
            nombres.extend([variante, f"{variante}.txt", f"{variante} .x", f"{variante}.tar.gz", f"{variante}x"])
    return nombres


def _contrato_acepta(nombre: str) -> bool:
    try:
        validar_nombre_de_grupo(nombre)
    except HeadlessContractError:
        return False
    return True


def _schema_acepta(nombre: str) -> bool:
    try:
        BodySlideBatchParams.model_validate({"group": nombre}, strict=True)
    except pydantic.ValidationError:
        return False
    return True


@pytest.mark.parametrize("nombre", _corpus(), ids=repr)
def test_el_schema_acepta_un_grupo_si_y_solo_si_lo_acepta_el_contrato(nombre: str) -> None:
    # Arrange / Act
    contrato = _contrato_acepta(nombre)
    schema = _schema_acepta(nombre)

    # Assert
    assert schema is contrato, (
        f"{nombre!r}: el schema {'acepta' if schema else 'rechaza'} y el contrato {'acepta' if contrato else 'rechaza'}"
    )


def test_el_corpus_cubre_los_dos_lados_de_cada_regla() -> None:
    """Sin esto, un corpus que solo tuviera nombres válidos pasaría la paridad sin probar nada."""
    corpus = _corpus()
    aceptados = [n for n in corpus if _contrato_acepta(n)]
    rechazados = [n for n in corpus if not _contrato_acepta(n)]

    assert len(aceptados) >= 5
    # Los casos que motivaron la paridad: el patrón del schema los admitía.
    for nombre in ("CBBE.", "CBBE ", " CBBE", "CON", "nul.txt", "COM1", "LPT9 .x"):
        assert nombre in rechazados


def test_el_validador_del_schema_delega_en_el_contrato() -> None:
    """Una sola implementación: el validador de ``group`` llama a ``validar_nombre_de_grupo``.

    Copiar la regla en el schema volvería a abrir la divergencia que esta paridad cierra.
    """
    validadores = BodySlideBatchParams.__pydantic_decorators__.field_validators
    de_group = [d for d in validadores.values() if "group" in d.info.fields]
    assert de_group, "BodySlideBatchParams.group no tiene validador"

    llamadas: set[str] = set()
    for decorador in de_group:
        fuente = textwrap.dedent(inspect.getsource(decorador.func))
        for nodo in ast.walk(ast.parse(fuente)):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
                llamadas.add(nodo.func.id)
    assert "validar_nombre_de_grupo" in llamadas


def test_el_preset_no_se_valida_como_carpeta() -> None:
    """``preset`` no es un componente de ruta: un punto final sigue siendo un preset válido."""
    params = BodySlideBatchParams.model_validate({"preset": "Zeroed Sliders."}, strict=True)

    assert params.preset == "Zeroed Sliders."
