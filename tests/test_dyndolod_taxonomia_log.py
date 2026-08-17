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
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.local.tools.dyndolod_runner import (
    _CODECS_DEL_LOG,
    MARCADORES_DE_COMPLETITUD,
    NO_FATALES_DEL_DOMINIO,
    PATRONES_TERMINALES,
    clasificar_log,
)
from tests.test_dyndolod_service import (
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
    assert any("el log no cambió" in e for e in result.errors), result.errors


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
