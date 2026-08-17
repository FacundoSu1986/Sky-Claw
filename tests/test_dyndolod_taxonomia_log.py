"""Contrato ejecutable de la taxonomía del log de la etapa 9 (T2).

El gate anterior era ``not errors`` sobre un patrón que matchea ``Error:``, y el
rig 2026-08-10 midió **121 líneas `Error:` en una corrida EXITOSA** de DynDOLOD:
rechazaba todo éxito real. Estos tests congelan las tres cajas que lo reemplazan
—completitud, no-fatal de dominio, terminal— y el orden de decodificación.

**Límite de la evidencia, escrito acá porque condiciona lo que estos tests
prueban:** los informes de rig viven en la máquina del operador y no están en el
árbol (misma nota que `docs/pending_ooda_status.md`). Las fixtures de abajo se
construyeron a partir de las CATEGORÍAS que el informe enumera (§11.4), no de las
121 ocurrencias reales. Eso deja una pregunta abierta que solo cierra el rig:
**si alguna de esas 121 líneas cae fuera de `NO_FATALES_DEL_DOMINIO`, la regla
fail-closed la clasifica terminal y la corrida buena sale roja.** Ver
`test_una_categoria_no_prevista_sale_terminal_y_eso_es_deliberado`.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from sky_claw.local.tools.dyndolod_runner import (
    MARCADORES_DE_COMPLETITUD,
    NO_FATALES_DEL_DOMINIO,
    PATRONES_TERMINALES,
    clasificar_log,
)
from tests.test_dyndolod_service import (
    _EjecucionFalsa,
    _escribir_log,
    _escribir_salida,
    _runner_texgen,
)

# ---------------------------------------------------------------------------
# Fixtures de log. Sintéticas y construidas desde las categorías del informe.
# ---------------------------------------------------------------------------

#: Cola de una corrida EXITOSA de DynDOLOD: mucho ruido `Error:` de dominio y el
#: marcador al final. Es la forma que el gate viejo rechazaba.
LOG_EXITO_DYNDOLOD = """[00:00:01] Using Output Path: C:\\Modding\\out\\
[00:00:01] Using Data Path: C:\\Games\\Skyrim Special Edition\\Data\\
[00:04:12] Error: Deleted reference [REFR:0004A2B1] in Tamriel
[00:04:12] Error: Deleted reference [REFR:0004A2B2] in Tamriel
[00:05:30] Error: Unresolved FormID [0102ABCD] in Skyrim.esm
[00:06:02] Error: LOD billboard(s) not found for TreePineForest03
[00:06:11] Error: No TexGen output detected
[00:07:40] Error: DynDOLOD.DLL SE not found in Data
[00:08:03] Error: File not found SkyrimSE.exe
[00:19:55] LODGenx64Win6.exe generated object LOD for Tamriel successfully
[00:20:10] DynDOLOD plugins generated successfully
[00:20:12] Occlusion.esp completed successfully
"""

#: Corrida de TexGen que terminó bien.
LOG_EXITO_TEXGEN = """[00:00:01] Using Output Path: C:\\Modding\\out\\
[00:02:30] Error: Deleted reference [REFR:00001111] in Tamriel
[00:09:12] TexGen completed successfully
"""

#: El operador cerró la GUI a mitad: hay errores y NO hay marcador.
LOG_CIERRE_MID_RUN = """[00:00:01] Using Output Path: C:\\Modding\\out\\
[00:03:00] Error: Deleted reference [REFR:0004A2B1] in Tamriel
[00:07:45] Fatal: madExcept caught an access violation
"""

#: El caso que aísla la completitud: limpio, sin una sola línea terminal, y lo
#: ÚNICO que le falta es el marcador.
LOG_SIN_MARCADOR = """[00:00:01] Using Output Path: C:\\Modding\\out\\
[00:04:12] Error: Deleted reference [REFR:0004A2B1] in Tamriel
[00:06:02] Error: LOD billboard(s) not found for TreePineForest03
[00:19:55] Processing worldspace Tamriel
"""


# ---------------------------------------------------------------------------
# Anclas enumerativas
# ---------------------------------------------------------------------------


def test_los_marcadores_de_completitud_estan_congelados() -> None:
    """Igualdad LITERAL del dict, y con las DOS herramientas dentro.

    Los marcadores difieren por binario, así que este es exactamente el lugar
    donde se cuela el defecto hermano de `AGENTS.md`: cablear el de DynDOLOD y
    dejar TexGen sin el suyo deja media etapa 9 sin gate de completitud, con la
    suite en verde. Enumerar —no muestrear— es lo que lo ataja: una herramienta
    nueva sin marcador rompe acá.
    """
    assert MARCADORES_DE_COMPLETITUD == {
        "TexGen": ("TexGen completed successfully",),
        "DynDOLOD": (
            "DynDOLOD plugins generated successfully",
            "Occlusion.esp completed successfully",
            "generated object LOD for",
        ),
    }


def test_las_tres_cajas_estan_congeladas() -> None:
    """Igualdad literal de no-fatales y terminales.

    Mover una línea de una caja a la otra cambia el veredicto de corridas reales:
    ampliar `NO_FATALES_DEL_DOMINIO` a mano es cómo vuelve el falso verde, y
    recortarlo es cómo vuelve el falso rojo de las 121 líneas.
    """
    assert NO_FATALES_DEL_DOMINIO == (
        "Deleted reference",
        "Unresolved FormID",
        "LOD billboard(s) not found",
        "No TexGen output detected",
        "DynDOLOD.DLL",
        "File not found SkyrimSE.exe",
    )
    assert PATRONES_TERMINALES == (
        "Fatal:",
        "Can not create path",
        "Path not allowed",
        "core files are outdated",
        "madExcept",
        "Exception",
    )


def test_toda_herramienta_lanzada_por_el_runner_declara_su_marcador() -> None:
    """El dict cubre exactamente a los dos binarios que el runner lanza.

    Un lanzador nuevo sin entrada en el dict haría `KeyError` en `clasificar_log`,
    que es ruidoso pero tarde. Esto lo dice antes y por enumeración.
    """
    assert set(MARCADORES_DE_COMPLETITUD) == {"TexGen", "DynDOLOD"}


# ---------------------------------------------------------------------------
# Comportamiento del clasificador
# ---------------------------------------------------------------------------


def test_la_corrida_exitosa_con_121_lineas_de_ruido_no_tiene_terminales() -> None:
    """El caso que motivó T2: `Error:` de dominio en cantidad, y aun así éxito."""
    terminales, no_fatales, completo = clasificar_log(LOG_EXITO_DYNDOLOD, "DynDOLOD")

    assert terminales == []
    assert len(no_fatales) == 7
    assert completo is True


def test_el_cierre_mid_run_es_terminal_y_no_completo() -> None:
    terminales, _, completo = clasificar_log(LOG_CIERRE_MID_RUN, "DynDOLOD")

    assert any("madExcept" in linea for linea in terminales)
    assert completo is False


def test_una_categoria_no_prevista_sale_terminal_y_eso_es_deliberado() -> None:
    """Fail-closed sobre lo que la lista de no-fatales no contempla.

    `NO_FATALES_DEL_DOMINIO` salió del §11.4 del informe, que enumera CATEGORÍAS
    y no las 121 ocurrencias. Una séptima categoría que el rig haya emitido y que
    nadie transcribió cae acá: rojo revisable. Es la mitad correcta del
    trade-off, pero también es la razón por la que el roadmap exige volcar el log
    real completo contra esta taxonomía antes de dar T2 por cerrado.
    """
    terminales, no_fatales, _ = clasificar_log(
        "[00:01:00] Error: something nobody transcribed from the rig\n", "DynDOLOD"
    )

    assert terminales == ["[00:01:00] Error: something nobody transcribed from the rig"]
    assert no_fatales == []


@pytest.mark.parametrize(
    ("tool", "log"),
    [("TexGen", LOG_EXITO_TEXGEN), ("DynDOLOD", LOG_EXITO_DYNDOLOD)],
)
def test_cada_herramienta_reconoce_su_propio_marcador(tool: str, log: str) -> None:
    """Parametrizado sobre las dos: el par hermano, cubierto por construcción."""
    _, _, completo = clasificar_log(log, tool)

    assert completo is True


def test_el_marcador_de_una_herramienta_no_vale_para_la_otra() -> None:
    """Cruzar los marcadores no puede dar completitud.

    Sin esto, un dict con la entrada equivocada pasaría los tests de arriba: el
    log de DynDOLOD tiene marcador, el de TexGen también, y nadie mira cuál.
    """
    _, _, completo = clasificar_log(LOG_EXITO_TEXGEN, "DynDOLOD")

    assert completo is False


# ---------------------------------------------------------------------------
# El conjunto de completitud, aislado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_falta_solo_el_marcador_y_la_corrida_sale_roja(tmp_path: pathlib.Path) -> None:
    """`rc == 0`, artefacto fresco, log sin terminales, y SOLO falta el marcador.

    Es el único caso que hace fallar al conjunto `AND completo` de forma
    independiente. El fixture del cierre mid-run no sirve para esto: trae 101
    errores *y* ausencia de marcador, así que cae por el gate de terminales
    aunque la implementación nunca mire la completitud — una mutación que borre
    el conjunto nuevo seguiría verde (revisor Codex, PR #485).
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    _escribir_log(tmp_path, "DynDOLOD", LOG_SIN_MARCADOR)

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    # Y la prueba de que cae por completitud y no por otra cosa: no hay terminales.
    assert not [e for e in result.errors if "Fatal" in e or "Exception" in e]


@pytest.mark.asyncio
async def test_el_mismo_log_con_marcador_es_exito(tmp_path: pathlib.Path) -> None:
    """Contracara exacta del anterior: se agrega SOLO el marcador y sale verde.

    Las dos mitades juntas son lo que prueba que el conjunto de completitud es lo
    que decide, y no un efecto lateral del fixture.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    _escribir_log(tmp_path, "DynDOLOD", LOG_SIN_MARCADOR + "[00:20:10] DynDOLOD plugins generated successfully\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True


# ---------------------------------------------------------------------------
# Decodificación
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("codec", ["utf-8", "cp1252"])
async def test_el_log_se_recupera_intacto_en_los_dos_encodings(tmp_path: pathlib.Path, codec: str) -> None:
    """Ni el log cp1252 ni el utf-8 pueden salir mojibake.

    El orden importa y la variante ingenua no funciona: `cp1252` acepta casi
    cualquier byte, así que "leer cp1252 con fallback a utf-8" NUNCA alcanza el
    fallback y un log utf-8 legítimo se decodifica igual, en silencio y mal.
    `utf-8` va primero porque es el único autovalidante de los dos.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    acentuado = "[00:04:12] Error: Deleted reference en Añocuervo — sección ñ\n"
    log = tmp_path / "DynDOLOD" / "Logs" / "DynDOLOD_SSE_log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes((acentuado + "[00:20:10] DynDOLOD plugins generated successfully\n").encode(codec))

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert any("Añocuervo" in w and "\ufffd" not in w for w in result.warnings)
