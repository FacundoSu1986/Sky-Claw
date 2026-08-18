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

import ast
import hashlib
import os
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.local.tools.dyndolod_runner import (
    _CODECS_DEL_LOG,
    MARCADORES_DE_COMPLETITUD,
    NO_FATALES_DEL_DOMINIO,
    PATRONES_TERMINALES,
    DynDOLODRunner,
    clasificar_log,
)
from tests.test_dyndolod_service import (
    _MARCADOR_DE_FIN,
    _clase_runner_ast,
    _corre_en_hilo,
    _corrida_que_completa,
    _EjecucionFalsa,
    _escribir_log,
    _escribir_salida,
    _llamadas,
    _log_completo,
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
        ),
    }


def test_un_hito_intermedio_no_puede_ser_marcador_de_completitud() -> None:
    """Regresión del falso verde de `generated object LOD for` (revisor, PR #488).

    Los marcadores se evalúan con `any()`, así que cada entrada basta por sí sola.
    Una entrada que describa un HITO INTERMEDIO —LODGen terminó su parte— convierte
    en "completa" a una corrida que murió antes de generar los plugins. Y truncada
    en el `<ws>` para tolerar el worldspace variable, además perdía el
    `successfully` y matcheaba líneas de error.

    Las dos mitades del contrato: la corrida a medias NO es completa, y una línea
    de error que mencione LODGen tampoco la vuelve completa.
    """
    solo_lodgen = "[00:19:55] LODGenx64Win6.exe generated object LOD for Tamriel successfully\n"
    _, _, completo = clasificar_log(solo_lodgen, "DynDOLOD")
    assert completo is False, "una corrida que no generó los plugins no está completa"

    con_error = "[00:19:55] Error: failed, never generated object LOD for Tamriel\n"
    terminales, _, completo_error = clasificar_log(con_error, "DynDOLOD")
    assert completo_error is False
    assert terminales, "y además es terminal por la regla fail-closed"


def test_todo_marcador_afirma_el_fin_del_trabajo() -> None:
    """Propiedad del mecanismo, no un caso: ningún marcador sin `successfully`.

    Es lo que distingue "la herramienta terminó" de "una etapa interna terminó".
    Un marcador nuevo sin esa palabra rompe acá antes de llegar a producción, sin
    depender de que alguien escriba a mano su caso de falso verde.
    """
    for tool, marcadores in MARCADORES_DE_COMPLETITUD.items():
        for marcador in marcadores:
            assert "successfully" in marcador, f"{tool}: {marcador!r} no afirma un final exitoso"


def test_las_tres_cajas_estan_congeladas() -> None:
    """Igualdad literal de no-fatales y terminales.

    Mover una línea de una caja a la otra cambia el veredicto de corridas reales:
    ampliar `NO_FATALES_DEL_DOMINIO` a mano es cómo vuelve el falso verde, y
    recortarlo es cómo vuelve el falso rojo de las 121 líneas.
    """
    # Cada entrada es una tupla de substrings que deben aparecer TODOS. La del DLL
    # lleva dos partes a propósito: exentar por `"DynDOLOD.DLL"` suelto declaraba
    # no-fatal a `Critical: DynDOLOD.DLL is corrupt` (revisor Codex, PR #488).
    assert NO_FATALES_DEL_DOMINIO == (
        ("Deleted reference",),
        ("Unresolved FormID",),
        ("LOD billboard(s) not found",),
        ("No TexGen output detected",),
        ("DynDOLOD.DLL", "not found"),
        ("File not found SkyrimSE.exe",),
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
    """Introspección sobre el runner, no repetición del dict.

    La versión anterior afirmaba `set(MARCADORES) == {"TexGen", "DynDOLOD"}`, que
    es la MISMA información que ya congela `test_los_marcadores_..._congelados`:
    un tercer lanzador sin marcador dejaba las claves en dos y los dos tests en
    verde — exactamente el hermano que el docstring decía atajar (lo marcaron
    Codex y CodeRabbit por separado, PR #488).

    Ahora se leen del AST los `tool_name=` que el runner pasa a
    `ToolExecutionResult` y se comparan contra el dict. Un lanzador nuevo rompe
    acá hasta que declare su marcador.
    """
    import ast
    import inspect

    from sky_claw.local.tools import dyndolod_runner

    arbol = ast.parse(inspect.getsource(dyndolod_runner))
    lanzadas = {
        kw.value.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        for kw in nodo.keywords
        if kw.arg == "tool_name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    }

    assert lanzadas, "el detector no encontró ningún tool_name: la introspección se rompió"
    assert lanzadas <= set(MARCADORES_DE_COMPLETITUD), (
        f"herramientas lanzadas sin marcador de completitud: {sorted(lanzadas - set(MARCADORES_DE_COMPLETITUD))}"
    )


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


#: Severidades que declaran que la corrida se rompió, en las formas que el binario
#: usa. `Fatal:` ya vive en `PATRONES_TERMINALES`; `Critical:` y `FAIL:` sólo
#: vivían en `_ERROR_SUELTO`, que se evalúa DESPUÉS de las categorías de dominio.
_SEVERIDADES_TERMINALES = ("Critical:", "Fatal:", "FATAL:", "FAIL:")


def _id_de_categoria(categoria: tuple[str, ...]) -> str:
    return "+".join(categoria)


@pytest.mark.parametrize("severidad", _SEVERIDADES_TERMINALES)
@pytest.mark.parametrize("categoria", NO_FATALES_DEL_DOMINIO, ids=_id_de_categoria)
def test_la_severidad_terminal_le_gana_a_la_categoria_de_dominio(
    categoria: tuple[str, ...],
    severidad: str,
) -> None:
    """PRODUCTO EXHAUSTIVO: toda categoría no-fatal × toda severidad terminal.

    Las tres cajas se evalúan en orden, y la caja de dominio estaba ANTES que el
    barrido de severidades sueltas. Consecuencia: una línea que trae una
    severidad terminal Y menciona una categoría conocida —`Critical: Deleted
    reference […] is corrupt`— se clasificaba como ruido de dominio y la corrida
    salía verde. La categoría describe DE QUÉ habla la línea; la severidad dice
    si la corrida se rompió, y eso manda.

    Enumerar el producto y no dos ejemplos es deliberado: la lista de categorías
    crece cuando el rig aporta una nueva, y una categoría agregada sin esta
    precedencia vuelve a abrir el agujero sin que ningún test la vea. Acá entra
    sola al producto.
    """
    linea = f"[00:04:12] {severidad} {' '.join(categoria)} en Tamriel"
    terminales, no_fatales, _ = clasificar_log(linea + "\n", "DynDOLOD")

    assert terminales == [linea], f"{severidad} sobre {_id_de_categoria(categoria)} no salió terminal"
    assert no_fatales == [], f"{severidad} sobre {_id_de_categoria(categoria)} se tragó como ruido: {no_fatales}"


@pytest.mark.parametrize("categoria", NO_FATALES_DEL_DOMINIO, ids=_id_de_categoria)
def test_un_error_suelto_sobre_categoria_conocida_sigue_siendo_no_fatal(
    categoria: tuple[str, ...],
) -> None:
    """La contracara: `Error:` NO es una severidad terminal, y no puede volverse una.

    El rig 2026-08-10 midió 121 líneas `Error:` en una corrida EXITOSA. Si la
    precedencia de severidad se implementara barriendo todo `_ERROR_SUELTO` antes
    de las categorías, cada una de esas 121 pasaría a terminal y ninguna corrida
    buena volvería a salir verde — el falso rojo que T2 vino a cerrar.

    Este test es el que impide arreglar de más, y por eso enumera las mismas
    categorías que el de arriba.
    """
    linea = f"[00:04:12] Error: {' '.join(categoria)} en Tamriel"
    terminales, no_fatales, _ = clasificar_log(linea + "\n", "DynDOLOD")

    assert terminales == [], f"`Error:` sobre una categoría conocida salió terminal: {terminales}"
    assert no_fatales == [linea]


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
async def test_la_corrida_cortada_despues_de_lodgen_sale_roja(tmp_path: pathlib.Path) -> None:
    """El falso verde del hito intermedio, probado END-TO-END y no solo en el clasificador.

    Escenario que los dos revisores señalaron por separado (PR #488): el proceso
    se cierra justo después de que LODGen termina el primer worldspace. Queda
    `rc == 0`, artefacto fresco —`DynDOLOD.esp` puede haberse persistido antes, y
    el SOP deja esa persistencia temprana explícitamente sin verificar— y ni una
    línea terminal. Con `generated object LOD for` en los marcadores, los cuatro
    conjuntos daban verde sobre una generación a medias.

    El test de `clasificar_log` no alcanzaba para cerrarlo: prueba la taxonomía,
    no el veredicto. Este cruza el runner entero, que es donde el falso verde se
    habría manifestado.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    _escribir_log(
        tmp_path,
        "DynDOLOD",
        "[00:00:01] Using Output Path: C:\\Modding\\out\\\n"
        "[00:19:55] LODGenx64Win6.exe generated object LOD for Tamriel successfully\n",
    )

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert result.return_code == 0, "el exit code era 0: lo que falla es la completitud"


@pytest.mark.asyncio
async def test_el_mismo_log_con_marcador_es_exito(tmp_path: pathlib.Path) -> None:
    """Contracara exacta del anterior: se agrega SOLO el marcador y sale verde.

    Las dos mitades juntas son lo que prueba que el conjunto de completitud es lo
    que decide, y no un efecto lateral del fixture.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path,
            "DynDOLOD",
            lambda: _escribir_salida(staging, "DynDOLOD.esp"),
            log=LOG_SIN_MARCADOR + "[00:20:10] DynDOLOD plugins generated successfully\n",
        ),
    )
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

    def _correr() -> None:
        _escribir_salida(staging, "DynDOLOD.esp")
        # Durante la corrida: el marcador sólo cuenta si el log cambió.
        log.write_bytes((acentuado + "[00:20:10] DynDOLOD plugins generated successfully\n").encode(codec))

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert any("Añocuervo" in w and "\ufffd" not in w for w in result.warnings)


@pytest.mark.asyncio
async def test_un_byte_que_ningun_codec_acepta_no_se_lleva_puesto_el_veredicto(
    tmp_path: pathlib.Path,
) -> None:
    """El último recurso `replace` existe para que un byte roto no borre el log.

    `0x81` no está definido NI en utf-8 NI en cp1252 —los dos codecs estrictos
    de `_CODECS_DEL_LOG` lo rechazan, y el test lo afirma en vez de suponerlo—,
    así que es el único caso que alcanza la tercera rama. Sin ella,
    `read_bytes().decode(...)` levantaría `UnicodeDecodeError` y una corrida
    buena moriría por un byte de basura en una línea que ni siquiera participa
    del veredicto.

    Lo que se afirma es que el resto del log SÍ se lee: el marcador de
    completitud está DESPUÉS del byte roto, así que si la decodificación se
    cortara ahí el veredicto saldría rojo.
    """
    for codec in _CODECS_DEL_LOG:
        with pytest.raises(UnicodeDecodeError):
            b"\x81".decode(codec)

    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    log = tmp_path / "DynDOLOD" / "Logs" / "DynDOLOD_SSE_log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)

    def _correr() -> None:
        _escribir_salida(staging, "DynDOLOD.esp")
        log.write_bytes(
            b"[00:04:12] Error: Deleted reference \x81\n[00:20:10] DynDOLOD plugins generated successfully\n"
        )

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True


# ---------------------------------------------------------------------------
# El marcador tiene que ser de ESTA corrida
#
# Los tres tests de abajo son el ancla que faltaba. Con el gate ya escrito, la
# suite quedaba verde al borrar el bloque entero de invalidación del marcador
# rancio: el falso verde volvía sin que CI dijera nada (revisores qodo y
# CodeRabbit, PR #488; confirmado por mutación antes de escribirlos).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_el_marcador_de_la_corrida_anterior_no_vale_si_el_log_no_cambio(
    tmp_path: pathlib.Path,
) -> None:
    """Log viejo CON marcador + corrida que sólo toca la salida → rojo.

    Es el falso verde P1: la corrida de hoy escribe el artefacto —así que el gate
    de frescura pasa— y muere antes de tocar el log. Los otros tres conjuntos
    (`rc == 0`, artefacto fresco, sin terminales) quedan satisfechos sobre el log
    de AYER, que sí tiene su marcador.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    _escribir_log(tmp_path, "DynDOLOD", _log_completo("DynDOLOD"))

    # `al_ejecutar` NO toca el log a propósito: ésa es la corrida que muere antes.
    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    # El motivo dice "no creció", no "no cambió": desde que el append se demuestra
    # por digest del prefijo, lo que descalifica la evidencia es la ausencia de
    # BYTES NUEVOS, no la igualdad de la firma. La propiedad del test es la misma.
    assert any("no creció" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_el_marcador_viejo_no_revive_porque_la_corrida_apendee_lineas(
    tmp_path: pathlib.Path,
) -> None:
    """Si el binario APENDEA, cambiar de firma no prueba que el marcador sea de hoy.

    El hermano del test de arriba, y el que la firma mtime+tamaño sola no ataja:
    el log de ayer tiene su marcador, la corrida de hoy apendea sus líneas de
    progreso —la firma CAMBIA— y muere sin llegar a declarar que terminó. Por eso
    el marcador se busca sólo en los bytes que esta corrida agregó.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    log = _escribir_log(tmp_path, "DynDOLOD", _log_completo("DynDOLOD"))

    def _correr() -> None:
        _escribir_salida(staging, "DynDOLOD.esp")
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[01:00:00] Building LOD for Tamriel\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("YA existía" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_la_corrida_que_apendea_su_propio_marcador_sí_es_exito(
    tmp_path: pathlib.Path,
) -> None:
    """Contracara exacta del anterior: mismo montaje, y esta vez el marcador es de hoy.

    Sin este caso, el test de arriba lo satisface cualquier implementación que
    diga que no ante un log con historia — incluida la que rompe el append
    legítimo. Lo único que cambia entre los dos es QUIÉN escribió el marcador.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    log = _escribir_log(tmp_path, "DynDOLOD", _log_completo("DynDOLOD"))

    def _correr() -> None:
        _escribir_salida(staging, "DynDOLOD.esp")
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[01:00:00] Building LOD for Tamriel\n")
            fh.write("[01:30:00] DynDOLOD plugins generated successfully\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True, result.errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "marcador", "artefacto", "lanzar"),
    [
        ("TexGen", "TexGen completed successfully", "a.dds", lambda r: r.run_texgen()),
        (
            "DynDOLOD",
            "DynDOLOD plugins generated successfully",
            "DynDOLOD.esp",
            lambda r: r.run_dyndolod(preset="Medium"),
        ),
    ],
    ids=["TexGen", "DynDOLOD"],
)
async def test_un_terminal_de_una_sesion_anterior_no_enrojece_esta_corrida(
    tmp_path: pathlib.Path,
    tool: str,
    marcador: str,
    artefacto: str,
    lanzar,
) -> None:
    """El hermano del scoping del marcador, en la OTRA dirección.

    `600e48f` delimita el MARCADOR a los bytes que la corrida agregó, pero los
    terminales se siguen clasificando sobre el log COMPLETO. La documentación
    oficial dice que el log normal apendea entre sesiones (dyndolod.info/Messages:
    "All messages are appended … when the tool is closed"), así que un `Fatal:`
    de una sesión fallida anterior permanece en el prefijo y enrojece TODA
    corrida buena posterior: falso rojo PERMANENTE hasta que el operador trunque
    el log a mano, con un diagnóstico que muestra la línea vieja y su timestamp
    como si fueran de hoy.

    La propiedad: la evidencia de ESTA corrida —el marcador Y los terminales—
    son los bytes que esta corrida agregó, bajo append y bajo truncado. Un
    terminal heredado no puede tumbar una corrida que no lo emitió.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / (
        DynDOLODRunner.TEXGEN_OUTPUT_NAME if tool == "TexGen" else DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    )

    # Arrange: la sesión N dejó su terminal en el log acumulado.
    log = _escribir_log(tmp_path, tool, "[00:07:45] Fatal: madExcept caught an access violation\n")

    # Act: la sesión N+1 es una corrida buena: rc 0, artefacto fresco y su propio
    # marcador apendeado DURANTE la corrida.
    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[01:00:00] Building LOD for Tamriel\n")
            fh.write(f"[01:30:00] {marcador}\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is True, result.errors


@pytest.mark.asyncio
async def test_con_frontera_indeterminada_no_se_atribuyen_terminales_historicos(
    tmp_path: pathlib.Path,
) -> None:
    """Si no se puede establecer qué bytes son de ESTA corrida, el run sigue
    fail-closed por el motivo de sondeo, pero un terminal histórico no puede
    aparecer en el diagnóstico como si lo hubiera emitido esta corrida.

    ARRANGE: el log histórico contiene `Fatal: old failure`; la firma previa
    del log falla (``None``) → la frontera temporal es indeterminada.
    ACT: la corrida de hoy apendea su marcador y escribe su artefacto.
    ASSERT: rojo POR el motivo de sondeo; el terminal histórico NO figura en
    los errores de esta corrida.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / "DynDOLOD_Output"
    log = _escribir_log(tmp_path, "DynDOLOD", "[00:07:45] Fatal: old failure\n")

    def _correr() -> None:
        _escribir_salida(staging, "DynDOLOD.esp")
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[01:00:00] Building LOD for Tamriel\n")
            fh.write("[01:30:00] DynDOLOD plugins generated successfully\n")

    original = runner._firma_del_log
    llamadas = {"n": 0}

    def _firma_indeterminada_antes_de_lanzar(tool: str):
        # SÓLO la firma que toma el LANZADOR antes de arrancar falla: no hay con
        # qué delimitar qué bytes son de hoy. Cualquier firma posterior usa la
        # implementación original — la rama fail-closed igual ya se disparó con
        # la previa, pero el mock queda exacto a la propiedad del test.
        if tool != "DynDOLOD":
            return original(tool)
        llamadas["n"] += 1
        return None if llamadas["n"] == 1 else original(tool)

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with (
        patch.object(runner, "_firma_del_log", _firma_indeterminada_antes_de_lanzar),
        patch.object(runner, "_execute_process", fake),
    ):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("No se pudo sondear el log de DynDOLOD ANTES" in e for e in result.errors), result.errors
    assert not any("Fatal: old failure" in e for e in result.errors), result.errors


# =============================================================================
# CONTINUIDAD DEL LOG: el append hay que DEMOSTRARLO, no suponerlo
# =============================================================================
#
# `mtime + tamaño` NO demuestra que un crecimiento sea un append: un archivo
# truncado y reescrito que termine MÁS GRANDE que el previo tiene exactamente la
# misma firma que un append legítimo. Recortar `bytes[:previo]` en ese caso deja
# afuera bytes que escribió ESTA corrida — y si entre ellos hay un `Fatal:`, el
# veredicto sale verde sobre una corrida que abortó.
#
# La documentación oficial de DynDOLOD dice que el log normal apendea entre
# sesiones, y por eso el recorte es la semántica correcta en el caso corriente.
# Pero una desviación de esa semántica no puede convertirse en falso verde: el
# recorte sólo es lícito cuando se DEMOSTRÓ que el prefijo sigue intacto.
#
# Estos casos enumeran los cuatro desenlaces posibles de comparar el tamaño
# actual contra el previo (crece-y-el-prefijo-coincide, crece-y-no-coincide,
# igual, achica) sobre los DOS lanzadores, que es el hermano de este repo.

#: Los dos lanzadores con lo que cada uno necesita para llegar al post-check.
#: El camino de continuidad (`_validar_completitud_de_la_corrida`) es compartido,
#: así que parametrizar no es ceremonia: es el defecto dominante del repo
#: —cablear un hermano y no al otro— convertido en enumeración.
_LANZADORES = [
    pytest.param("TexGen", "a.dds", lambda r: r.run_texgen(), id="TexGen"),
    pytest.param("DynDOLOD", "DynDOLOD.esp", lambda r: r.run_dyndolod(preset="Medium"), id="DynDOLOD"),
]

#: Prefijo inocuo de una sesión anterior. Doce líneas para que el recorte por
#: tamaño tenga dónde equivocarse: con un prefijo de pocos bytes, un `Fatal:`
#: escrito al principio de la reescritura caería igual del lado de la cola y el
#: test pasaría por accidente.
_PREFIJO_VIEJO = "[00:00:01] Building LOD for Tamriel\n" * 12


def _staging_de(config: object, tool: str) -> pathlib.Path:
    """Staging administrado de cada herramienta, por nombre y no por posición."""
    nombre = DynDOLODRunner.TEXGEN_OUTPUT_NAME if tool == "TexGen" else DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    return config.output_root / nombre  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "artefacto", "lanzar"), _LANZADORES)
async def test_una_reescritura_que_crece_no_se_toma_por_append(
    tmp_path: pathlib.Path,
    tool: str,
    artefacto: str,
    lanzar,
) -> None:
    """CASO A — el contraejemplo que `mtime + tamaño` no puede distinguir.

    La corrida TRUNCA el log y lo reescribe terminando MÁS GRANDE que el previo.
    Su firma es indistinguible de la de un append, así que recortar
    ``bytes[:previo]`` descarta bytes de ESTA corrida. Entre los descartados hay
    un `Fatal:` de HOY, y el marcador de fin queda en la cola: `rc == 0`,
    artefacto fresco y completitud satisfecha sobre una corrida que abortó.

    La propiedad: **un terminal ACTUAL no puede quedar fuera de la evidencia
    porque se supuso un append que nadie demostró.** Si el prefijo no se puede
    verificar intacto, la corrida falla cerrado — no se adivina qué parte es de
    quién.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, tool)
    log = _escribir_log(tmp_path, tool, _PREFIJO_VIEJO)
    tamano_previo = len(_PREFIJO_VIEJO.encode())

    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        nuevo = (
            "[01:00:00] Fatal: madExcept caught an access violation\n"
            + "[01:00:01] Building LOD for Tamriel\n" * 20
            + f"[01:30:00] {_MARCADOR_DE_FIN[tool]}\n"
        )
        crudo = nuevo.encode()
        # El montaje tiene que ser el que dice el docstring, o el test prueba otra
        # cosa: el Fatal ANTES del corte, y el archivo terminando más grande.
        assert crudo.index(b"Fatal:") < tamano_previo, "el Fatal quedó fuera del prefijo recortado"
        assert len(crudo) > tamano_previo, "la reescritura no terminó más grande"
        log.write_text(nuevo, encoding="utf-8")  # `write_text` TRUNCA

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is False, "falso verde: el `Fatal:` de ESTA corrida quedó fuera de la evidencia"
    assert result.errors, "falla cerrado, pero sin decir por qué: eso es un 'Unknown error' nuevo"


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "artefacto", "lanzar"), _LANZADORES)
async def test_el_append_real_con_marcador_propio_sigue_siendo_exito(
    tmp_path: pathlib.Path,
    tool: str,
    artefacto: str,
    lanzar,
) -> None:
    """CASO B — contracara de A: el append legítimo no puede volverse rojo.

    Sin este caso, A lo satisface cualquier implementación que falle cerrado ante
    cualquier log con historia — incluida la que rompe el append documentado y
    reintroduce el falso rojo permanente que este PR vino a cerrar.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, tool)
    log = _escribir_log(tmp_path, tool, _PREFIJO_VIEJO)

    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        with log.open("a", encoding="utf-8") as fh:  # append REAL: el prefijo queda intacto
            fh.write("[01:00:00] Building LOD for Tamriel\n")
            fh.write(f"[01:30:00] {_MARCADOR_DE_FIN[tool]}\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is True, result.errors


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "artefacto", "lanzar"), _LANZADORES)
async def test_un_terminal_de_esta_corrida_en_la_cola_del_append_tumba_la_corrida(
    tmp_path: pathlib.Path,
    tool: str,
    artefacto: str,
    lanzar,
) -> None:
    """CASO C — el append demostrado no es un permiso para ignorar terminales.

    Delimitar la evidencia sirve para no heredar culpas ajenas, no para dejar de
    mirar. El `Fatal:` está en los bytes que agregó ESTA corrida, junto con su
    propio marcador de fin: el marcador no puede taparlo.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, tool)
    log = _escribir_log(tmp_path, tool, _PREFIJO_VIEJO)

    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[01:00:00] Fatal: madExcept caught an access violation\n")
            fh.write(f"[01:30:00] {_MARCADOR_DE_FIN[tool]}\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is False
    assert any("Fatal:" in e for e in result.errors), result.errors


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "artefacto", "lanzar"), _LANZADORES)
async def test_el_mtime_sin_crecimiento_no_es_evidencia_de_esta_corrida(
    tmp_path: pathlib.Path,
    tool: str,
    artefacto: str,
    lanzar,
) -> None:
    """CASO E — un `mtime` nuevo sobre el mismo tamaño no es evidencia de nada.

    El log de AYER trae su marcador. La corrida de hoy toca la salida —así que el
    gate de frescura del artefacto pasa— y al log sólo le cambia el `mtime` sin
    escribirle un byte. Tomar eso por "lo truncó y lo reescribió" hereda el
    marcador viejo y da verde sobre una corrida que no declaró haber terminado.

    Sin bytes nuevos demostrables no hay evidencia: falla cerrado.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, tool)
    log = _escribir_log(tmp_path, tool, _log_completo(tool))
    estado = log.stat()

    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        os.utime(log, (estado.st_atime + 500, estado.st_mtime + 500))

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is False, "falso verde: heredó el marcador de la corrida anterior"
    assert log.stat().st_size == estado.st_size, "el montaje del test cambió el tamaño: prueba otra cosa"


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "artefacto", "lanzar"), _LANZADORES)
async def test_un_log_mas_chico_que_antes_de_lanzar_falla_cerrado(
    tmp_path: pathlib.Path,
    tool: str,
    artefacto: str,
    lanzar,
) -> None:
    """CASO F — si el log ACHICÓ, no hay forma de delimitar nada.

    Truncado o reemplazo: el prefijo previo ya no está, así que no se puede
    demostrar qué bytes son de esta corrida. Incluso con el marcador de fin
    presente, "no pude verificarlo" no puede significar "terminó bien".
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, tool)
    log = _escribir_log(tmp_path, tool, _PREFIJO_VIEJO)
    tamano_previo = len(_PREFIJO_VIEJO.encode())

    def _correr() -> None:
        _escribir_salida(staging, artefacto)
        nuevo = f"[01:30:00] {_MARCADOR_DE_FIN[tool]}\n"
        assert len(nuevo.encode()) < tamano_previo, "el montaje no achicó el log"
        log.write_text(nuevo, encoding="utf-8")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_correr)
    with patch.object(runner, "_execute_process", fake):
        result = await lanzar(runner)

    assert result.success is False
    assert result.errors, "falla cerrado sin diagnóstico"


@pytest.mark.asyncio
async def test_un_prefijo_mas_corto_que_la_firma_previa_no_se_acepta_como_continuidad(
    tmp_path: pathlib.Path,
) -> None:
    """Short read: la firma previa dice N bytes y el archivo ya no los tiene.

    Es el hueco entre el sondeo del tamaño y la re-firma del prefijo: el tamaño
    se lee en un momento y el prefijo se hashea en otro, así que el archivo puede
    haber achicado en el medio (rotación, truncado externo, otro proceso). El
    chequeo ``actual < previo`` no lo ataja porque compara los tamaños SONDEADOS,
    no lo que se pudo leer después.

    **El montaje hace que el digest parcial COINCIDA a propósito:** la firma
    previa lleva el sha256 del contenido corto real, así que si
    `_digest_del_prefijo` devolviera el hash de menos de N bytes, la comparación
    daría MATCH y el append quedaría "demostrado" sobre una lectura incompleta.
    Lo único que lo impide es la guarda ``leidos != hasta_byte``.

    Por eso el test no se conforma con el rojo: exige que el rojo sea POR no haber
    podido verificar el prefijo. Si cayera por "el marcador es de una corrida
    anterior", el hash parcial se habría aceptado como continuidad válida y el
    recorte se habría hecho sobre una frontera inventada.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, "DynDOLOD")
    log = _escribir_log(tmp_path, "DynDOLOD", _log_completo("DynDOLOD"))

    contenido_real = log.read_bytes()
    digest_real = hashlib.sha256(contenido_real).hexdigest()
    # N INFLADO: la firma previa reclama más bytes de los que el archivo tiene.
    n_inflado = len(contenido_real) + 4096

    llamadas = {"n": 0}

    def _firma_inflada(tool: str):
        if tool != "DynDOLOD":
            return runner.__class__._firma_del_log(runner, tool)
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            # Antes de lanzar: tamaño inflado + digest del contenido REAL, para que
            # un hash parcial coincida y sólo la guarda pueda distinguirlo.
            return (n_inflado, digest_real)
        # Después de la corrida: creció, así que se entra a la rama del append.
        return (n_inflado + 1, "da-igual-no-se-compara")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with (
        patch.object(runner, "_firma_del_log", _firma_inflada),
        patch.object(runner, "_execute_process", fake),
    ):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("prefijo" in e for e in result.errors), f"no falló por la verificación del prefijo: {result.errors}"
    assert not any("parte que YA existía" in e for e in result.errors), (
        f"aceptó como continuidad el hash de MENOS de {n_inflado} bytes: {result.errors}"
    )
    # Y el digest parcial habría coincidido: es lo que hace load-bearing a la guarda.
    assert hashlib.sha256(log.read_bytes()[:n_inflado]).hexdigest() == digest_real


#: Lo que ningún motivo puede afirmar cuando el marcador NO está en el archivo.
#: Son las dos formas textuales que tenían las ramas de frontera: afirmaban la
#: presencia del marcador para explicar por qué no se le podía atribuir a esta
#: corrida, y esa explicación es falsa cuando el marcador no existe.
_AFIRMA_QUE_HAY_MARCADOR = ("está en el log", "que tiene no se puede atribuir")


@pytest.mark.asyncio
async def test_sin_frontera_el_motivo_no_inventa_un_marcador_que_no_existe(
    tmp_path: pathlib.Path,
) -> None:
    """El diagnóstico no puede afirmar que el marcador existe si no existe.

    Rama de frontera indeterminada (no se pudo sondear el log ANTES de lanzar)
    sobre un log que NO tiene marcador. El veredicto correcto es rojo, y lo era
    antes también; lo que se corrige es el texto, que le decía al operador que su
    log tiene un marcador de completitud cuando no lo tiene.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, "DynDOLOD")
    _escribir_log(tmp_path, "DynDOLOD", LOG_SIN_MARCADOR)

    original = runner._firma_del_log
    llamadas = {"n": 0}

    def _sin_frontera(tool: str):
        if tool != "DynDOLOD":
            return original(tool)
        llamadas["n"] += 1
        return None if llamadas["n"] == 1 else original(tool)

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with (
        patch.object(runner, "_firma_del_log", _sin_frontera),
        patch.object(runner, "_execute_process", fake),
    ):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert "successfully" not in LOG_SIN_MARCADOR, "la fixture dejó de servir: ahora tiene marcador"
    for error in result.errors:
        for afirmacion in _AFIRMA_QUE_HAY_MARCADOR:
            assert afirmacion not in error, f"el motivo afirma un marcador inexistente: {error!r}"


@pytest.mark.asyncio
async def test_sin_crecimiento_el_motivo_no_inventa_un_marcador_que_no_existe(
    tmp_path: pathlib.Path,
) -> None:
    """Hermano del anterior en la otra rama de frontera: log sin crecer.

    Mismo defecto textual, otra rama. Escribirlo una sola vez dejaría la mitad
    del arreglo sin ancla, que es exactamente la forma del defecto dominante de
    este repo.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = _staging_de(config, "DynDOLOD")
    _escribir_log(tmp_path, "DynDOLOD", LOG_SIN_MARCADOR)

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    for error in result.errors:
        for afirmacion in _AFIRMA_QUE_HAY_MARCADOR:
            assert afirmacion not in error, f"el motivo afirma un marcador inexistente: {error!r}"


def test_todo_lanzador_que_pide_veredicto_firma_el_log_antes_de_lanzar() -> None:
    """La familia de lanzadores se detecta por introspección y se congela.

    El scoping del marcador a ESTA corrida sólo funciona si el lanzador tomó la
    firma del log ANTES de arrancar el proceso y se la pasó al post-check. Un
    lanzador que se la olvide recibe el default `None`, y aunque eso es
    fail-closed —rojo permanente, no verde falso— es la forma exacta del defecto
    dominante de este repo: cablearlo en un camino y no en su gemelo.

    Enumera, no muestrea: el conjunto de métodos que piden un post-check se
    descubre leyendo el AST, así que un TERCER lanzador rompe este test hasta que
    se lo cablee, en vez de entrar sin gate con la suite en verde.
    """
    cls = _clase_runner_ast()

    lanzadores: dict[str, ast.Call] = {}
    for miembro in cls.body:
        if not isinstance(miembro, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for nodo in ast.walk(miembro):
            if (
                isinstance(nodo, ast.Call)
                and nodo.args
                and isinstance(nodo.args[0], ast.Attribute)
                and nodo.args[0].attr == "_post_check"
            ):
                lanzadores[miembro.name] = nodo

    assert set(lanzadores) == {"run_texgen", "run_dyndolod"}

    for nombre, llamada in lanzadores.items():
        # to_thread(self._post_check, tool, firmas_previas, firma_previa_del_log)
        assert len(llamada.args) == 4, f"{nombre} no le pasa la firma previa del log al post-check"
        miembro = next(
            m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == nombre
        )
        # Los lanzadores la corren en un hilo (`to_thread(self._firma_del_log, …)`),
        # así que el nombre aparece como ARGUMENTO y no como llamada: mirar sólo
        # `_llamadas` da un rojo falso. Se aceptan las dos formas.
        firma = _corre_en_hilo(miembro, "_firma_del_log") or "_firma_del_log" in _llamadas(miembro)
        assert firma, f"{nombre} no firma el log antes de lanzar"
