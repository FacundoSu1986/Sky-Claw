from __future__ import annotations

import logging
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.pandora_runner import (
    _SEVERIDADES_QUE_FALLAN,
    _TAMANO_DE_BLOQUE,
    NOMBRE_DEL_LOG_DE_PANDORA,
    PandoraConfig,
    PandoraExecutionError,
    PandoraRunner,
    _escanear_severidades,
    _leer_engine_log,
    _MarcaDelLog,
    _marcas_del_log,
    _VeredictoDelLog,
)


def _preparar(tmp_path: pathlib.Path) -> tuple[PandoraRunner, pathlib.Path]:
    """Runner con el exe en su propio directorio y la ruta de su ``Engine.log``."""
    game = tmp_path / "Skyrim"
    game.mkdir(parents=True, exist_ok=True)
    dir_exe = tmp_path / "Pandora"
    dir_exe.mkdir(parents=True, exist_ok=True)
    runner = PandoraRunner(PandoraConfig(pandora_exe=dir_exe / "Pandora Behaviour Engine+.exe", game_path=game))
    return runner, dir_exe / NOMBRE_DEL_LOG_DE_PANDORA


async def _correr(runner: PandoraRunner, *, escribe: str = "", return_code: int = 0, log: pathlib.Path | None = None):
    """Ejecuta el runner simulando que Pandora APENDEA *escribe* a su log."""

    async def _fake(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, int]:
        if log is not None and escribe:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(escribe)
        return b"", b"", return_code

    with patch("sky_claw.local.tools.pandora_runner.run_capture", AsyncMock(side_effect=_fake)):
        return await runner.run_pandora()


async def test_pandora_pasa_una_sola_salida_absoluta_y_preserva_espacios(
    tmp_path: pathlib.Path,
) -> None:
    game = tmp_path / "Skyrim con espacios"
    exe = tmp_path / "Pandora con espacios" / "Pandora Behaviour Engine+.exe"
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))
    run_capture = AsyncMock(return_value=(b"ok", b"", 0))

    with patch("sky_claw.local.tools.pandora_runner.run_capture", run_capture):
        resultado = await runner.run_pandora()

    assert resultado.success is True
    argv = run_capture.await_args.args[0]
    assert argv.count("--output") == 1
    output = pathlib.Path(argv[argv.index("--output") + 1])
    assert output == game.resolve() / "Pandora_Output"
    assert output.is_absolute()
    assert run_capture.await_args.kwargs["cwd"] == str(game.resolve())


def test_output_path_del_runner_es_el_destino_administrado(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "segmento" / ".." / "game"
    exe = tmp_path / "Pandora.exe"
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))

    assert runner.output_path == game.resolve() / "Pandora_Output"


def test_output_path_falla_con_error_de_dominio_si_falta_game_path(tmp_path: pathlib.Path) -> None:
    config = PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=tmp_path)
    object.__setattr__(config, "game_path", None)
    runner = PandoraRunner(config)

    with pytest.raises(PandoraExecutionError, match="game_path.*Pandora_Output"):
        _ = runner.output_path


# --------------------------------------------------------------------------- #
# El veredicto NO sale del exit code: Pandora es error-tolerant
#
# El motor revierte el nodo inválido y sigue generando el resto (README de
# Monitor221hz/Pandora-Behaviour-Engine-Plus). O sea que termina en 0 habiendo
# descartado mods de animación, y el SOP (`sky_claw/local/AGENTS.md` §2.4) dice
# que eso es justamente lo que produce T-poses y combat anims rotas. La única
# señal de qué se aplicó es `Engine.log`.
#
# Mismo mecanismo que `bodyslide_runner._leer_log` (#433), con una diferencia
# deliberada: BodySlide acota la corrida por una firma de inicio documentada en
# su fuente (`Log::Initialize`), y para Pandora NO hay una firma verificada. Acá
# la atribución se hace por OFFSET —tamaño del log antes de spawnear— que no
# depende de ningún formato y por lo tanto no puede envejecer mal.
# --------------------------------------------------------------------------- #


async def test_exit_cero_con_error_en_el_log_no_es_exito(tmp_path: pathlib.Path) -> None:
    """El caso que este mecanismo existe para cerrar: falso verde por exit code."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(
        runner,
        escribe="INFO: Assembler > x > parse > y > ok\nERROR: Assembler > mod.xml > parse > nodo > falló\n",
        log=log,
    )

    assert resultado.success is False
    assert resultado.return_code == 0
    assert "ERROR" in resultado.message
    assert "mod.xml" in resultado.message


async def test_exit_cero_con_fatal_en_el_log_no_es_exito(tmp_path: pathlib.Path) -> None:
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="FATAL: Dispatcher > x > export > y > murió\n", log=log)

    assert resultado.success is False
    assert "FATAL" in resultado.message


async def test_exit_cero_con_log_limpio_es_exito_y_message_vacio(tmp_path: pathlib.Path) -> None:
    """Contrato ``success``/``message``: en éxito el message queda vacío."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="INFO: Assembler > x > parse > y > ok\n", log=log)

    assert resultado.success is True
    assert resultado.message == ""


async def test_warn_se_reporta_pero_no_decide(tmp_path: pathlib.Path) -> None:
    """``WARN`` es "unexpected condition", no "prevented work" (README).

    Hacerlo fallar convertiría advertencias rutinarias en falsos ROJOS, que es
    tan malo como el falso verde que este mecanismo cierra. Se cuenta y se
    surface, no decide.
    """
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="WARN: Validator > x > check > y > revertido\n", log=log)

    assert resultado.success is True
    assert any("revertido" in w for w in resultado.warnings)


async def test_un_error_de_una_corrida_anterior_no_ensucia_esta(tmp_path: pathlib.Path) -> None:
    """La prueba de la atribución por offset, y el falso rojo más probable.

    `Engine.log` se acumula entre corridas. Sin acotar por el tamaño previo, el
    `ERROR` de la corrida de ayer haría fallar para siempre todas las siguientes.
    """
    runner, log = _preparar(tmp_path)
    log.write_text("ERROR: Assembler > viejo.xml > parse > nodo > falló\n", encoding="utf-8")

    resultado = await _correr(runner, escribe="INFO: Assembler > x > parse > y > ok\n", log=log)

    assert resultado.success is True
    assert "viejo.xml" not in resultado.message


async def test_log_ausente_no_cambia_el_veredicto(tmp_path: pathlib.Path) -> None:
    """Sin log no hay señal: se conserva el comportamiento por exit code.

    Best-effort a propósito — la ubicación de `Engine.log` no está verificada
    contra un rig real, así que una ruta equivocada degrada al comportamiento de
    hoy y nunca a un falso rojo.
    """
    runner, _log = _preparar(tmp_path)

    resultado = await _correr(runner)

    assert resultado.success is True
    assert resultado.message == ""


async def test_log_truncado_durante_la_corrida_no_decide(tmp_path: pathlib.Path) -> None:
    """Si el log se rotó, el texto no es atribuible: se muestra pero no decide."""
    runner, log = _preparar(tmp_path)
    log.write_text("x" * 5000, encoding="utf-8")

    async def _fake(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, int]:
        # Pandora rota el log: queda MÁS CHICO que la marca previa.
        log.write_text("ERROR: Assembler > a > b > c > d\n", encoding="utf-8")
        return b"", b"", 0

    with patch("sky_claw.local.tools.pandora_runner.run_capture", AsyncMock(side_effect=_fake)):
        resultado = await runner.run_pandora()

    assert resultado.success is True, "texto no atribuible no debe producir un falso rojo"


async def test_exit_non_zero_sigue_fallando_aunque_el_log_este_limpio(tmp_path: pathlib.Path) -> None:
    """El log AGREGA una condición de fallo; no relaja la que ya había."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="INFO: todo bien\n", return_code=3, log=log)

    assert resultado.success is False
    assert "3" in resultado.message


async def test_exit_non_zero_adjunta_la_cola_cruda_si_no_reconocio_severidades(
    tmp_path: pathlib.Path,
) -> None:
    """La red bajo la suposición del formato de log.

    El patrón de severidad no está verificado contra un log real. Si el formato
    fuera otro, ``fallas`` queda vacío; sin adjuntar la cola cruda el operador se
    queda con un exit code pelado y ninguna pista de por qué falló.
    """
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="algo salió mal en un formato inesperado\n", return_code=2, log=log)

    assert resultado.success is False
    assert "formato inesperado" in resultado.message


async def test_exit_cero_con_formato_no_reconocido_deja_un_breadcrumb(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """La contraparte del test de arriba, en la rama de ÉXITO.

    ``_diagnostico`` sólo adjunta la cola cruda cuando ``not success`` — con
    exit 0 y ningún patrón de severidad reconocido, ``success`` sale ``True`` y
    esa cola nunca llega a ``message``. Sin un breadcrumb en el log de la
    aplicación, el operador se queda sin NINGUNA señal de que el patrón de
    severidad no matcheó, exactamente el escenario que este módulo existe para
    cerrar (hallazgo qodo, PR #439).
    """
    runner, log = _preparar(tmp_path)

    with caplog.at_level(logging.WARNING):
        resultado = await _correr(runner, escribe="algo salió mal en un formato inesperado\n", return_code=0, log=log)

    assert resultado.success is True
    assert any("formato inesperado" in r.message for r in caplog.records)


async def test_exit_cero_sin_crecimiento_de_log_no_deja_breadcrumb(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Una corrida sana y silenciosa (sin ninguna línea nueva en el log) no debe
    generar ruido en los logs de la aplicación."""
    runner, log = _preparar(tmp_path)

    with caplog.at_level(logging.WARNING):
        resultado = await _correr(runner, escribe="", return_code=0, log=log)

    assert resultado.success is True
    assert not caplog.records


async def test_reconoce_la_severidad_con_y_sin_corchetes(tmp_path: pathlib.Path) -> None:
    """El README documenta la plantilla ``[Severity]: ...`` con los corchetes como
    marcador de campo. No está verificado contra un log real si la línea sale
    ``ERROR:`` o ``[ERROR]:``, así que se aceptan las dos formas."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe="[ERROR]: Assembler > a > b > c > d\n", log=log)

    assert resultado.success is False


# --------------------------------------------------------------------------- #
# Marca no verificable ≠ marca ausente (review CodeRabbit + Copilot, PR #439)
#
# Un ``stat()`` fallido (permisos, lock de otro proceso, error transitorio de
# red en un share) sobre un log EXISTENTE no es lo mismo que un log ausente:
# tratar ambos como ``0`` hace que ``_leer_engine_log`` lea el archivo COMPLETO
# y atribuya a esta corrida los ``ERROR``/``FATAL`` de corridas anteriores — un
# falso rojo, tan malo como el falso verde que este módulo cierra.
# --------------------------------------------------------------------------- #


def test_marca_distingue_ausente_de_no_verificable(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``FileNotFoundError`` ⇒ ``0`` (ausente, el caso esperado con dos
    candidatas sondeadas). Cualquier OTRO ``OSError`` ⇒ ``None`` (no
    verificable) — nunca ``0``, que significaría "leer todo el archivo"."""
    ausente = tmp_path / "ausente.log"
    bloqueado = tmp_path / "bloqueado.log"
    bloqueado.write_text("contenido preexistente\n", encoding="utf-8")

    stat_original = pathlib.Path.stat

    def _stat_que_falla(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        if self == bloqueado:
            raise PermissionError("bloqueado por otro proceso")
        return stat_original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _stat_que_falla)

    marcas = _marcas_del_log((ausente, bloqueado))

    assert marcas[ausente] == _MarcaDelLog(tamano=0, ino=0, dev=0)
    assert marcas[bloqueado] is None


def test_marca_no_verificable_no_atribuye_contenido_de_corridas_anteriores() -> None:
    """Con marca ``None``, ``_leer_engine_log`` SALTA esa candidata: un ``ERROR``
    preexistente no puede convertirse en el veredicto de una corrida que nunca
    se pudo acotar. Sin esta guarda, ``None`` colapsaría a "leer desde 0" y
    produciría exactamente el falso rojo que la marca ``None`` existe para evitar.
    """
    log = pathlib.Path("/no/se/lee/nunca/Engine.log")

    veredicto = _leer_engine_log({log: None})

    assert veredicto == _VeredictoDelLog()


def test_leer_log_no_avisa_cuando_la_candidata_simplemente_no_existe(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Con dos candidatas sondeadas (``exe.parent`` y ``output``), lo normal es
    que UNA no exista. Eso no es una condición de error digna de warning en cada
    corrida sana del pipeline (review Copilot, PR #439)."""
    inexistente = tmp_path / "no_existe" / NOMBRE_DEL_LOG_DE_PANDORA

    with caplog.at_level(logging.WARNING):
        veredicto = _leer_engine_log({inexistente: _MarcaDelLog(tamano=0, ino=0, dev=0)})

    assert veredicto == _VeredictoDelLog()
    assert not caplog.records, [r.message for r in caplog.records]


def test_leer_log_avisa_ante_un_error_de_io_real(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Un ``OSError`` real durante la lectura (no un ``FileNotFoundError``
    esperado) sí debe quedar visible en los logs de la aplicación — es la
    contraparte del test de arriba: no todo I/O fallido debe silenciarse."""
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    log.write_text("contenido\n", encoding="utf-8")

    stat_original = pathlib.Path.stat

    def _stat_que_falla(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        if self == log:
            raise PermissionError("bloqueado")
        return stat_original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _stat_que_falla)

    with caplog.at_level(logging.WARNING):
        veredicto = _leer_engine_log({log: _MarcaDelLog(tamano=0, ino=0, dev=0)})

    assert veredicto == _VeredictoDelLog()
    assert any("bloqueado" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Anclas de enumeración (nitpick CodeRabbit, PR #439): muestrear ERROR/FATAL/WARN
# a mano no ataja una severidad nueva que se agregue a `_PATRON_DE_SEVERIDAD`
# sin cablearla en `_SEVERIDADES_QUE_FALLAN`. Y las dos candidatas de
# `_rutas_candidatas_del_log` (junto al exe, bajo `--output`) necesitan las DOS
# ejercitadas: sólo la primera se prueba en el resto del archivo.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("severidad", _SEVERIDADES_QUE_FALLAN)
async def test_toda_severidad_declarada_invalida_la_corrida(tmp_path: pathlib.Path, severidad: str) -> None:
    """Ancla de igualdad: cada severidad en ``_SEVERIDADES_QUE_FALLAN`` tiene que
    decidir el veredicto. Agregar una severidad al patrón sin cablearla acá
    rompe este test, en vez de pasar en silencio."""
    runner, log = _preparar(tmp_path)

    resultado = await _correr(runner, escribe=f"{severidad}: Assembler > a > b > c > d\n", log=log)

    assert resultado.success is False
    assert severidad in resultado.message


async def test_el_log_bajo_output_path_tambien_decide(tmp_path: pathlib.Path) -> None:
    """La segunda candidata de ``_rutas_candidatas_del_log`` (``runner.output_path``)
    tiene que estar cableada — el resto del archivo sólo ejercita la primera
    (``exe.parent``). Si un refactor la elimina, la suite debe notarlo."""
    runner, _log_junto_al_exe = _preparar(tmp_path)
    log_de_output = runner.output_path / NOMBRE_DEL_LOG_DE_PANDORA
    log_de_output.parent.mkdir(parents=True, exist_ok=True)

    resultado = await _correr(runner, escribe="ERROR: Assembler > mod.xml > parse > nodo > falló\n", log=log_de_output)

    assert resultado.success is False
    assert "ERROR" in resultado.message


# --------------------------------------------------------------------------- #
# Identidad del log, no sólo tamaño (hallazgo qodo, PR #439)
#
# Si Pandora trunca y RECREA `Engine.log` en vez de apendear —comportamiento
# no verificado contra un rig real—, un chequeo de sólo tamaño puede fallar:
# archivo viejo de 1000 bytes, el motor lo trunca y escribe 1500 bytes nuevos
# con el ERROR en los primeros bytes → `tamano(1500) > marca(1000)` sale
# True, pero leer desde el byte 1000 del archivo NUEVO saltea justo el ERROR
# que debería invalidar la corrida. La identidad (inode/device) distingue
# "este archivo creció" de "este archivo es OTRO que casualmente es más
# grande", sin necesitar verificar la semántica real de truncado de Pandora.
# --------------------------------------------------------------------------- #


def test_identidad_distinta_no_atribuye_pese_a_tamano_mayor(tmp_path: pathlib.Path) -> None:
    """El escenario exacto de la revisión: archivo recreado, más grande, con el
    ERROR en los primeros bytes — el tamaño solo lo dejaría pasar como éxito.

    La identidad se FABRICA en vez de simularse con ``unlink()`` + recrear: en
    este filesystem (y potencialmente en CI) el inode liberado se reutiliza de
    inmediato para el archivo siguiente, así que ``unlink``+recrear no garantiza
    una identidad distinta — el test sería flaky por una razón ajena a la lógica
    que se quiere probar. Fabricar la marca prueba la COMPARACIÓN en aislamiento.
    """
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    # El ERROR queda en los primeros bytes del archivo, pero el tamaño total es
    # mayor que la marca vieja — exactamente lo que dejaría pasar un chequeo
    # de sólo tamaño.
    log.write_text("ERROR: Assembler > mod.xml > parse > nodo > falló\n" + "x" * 1500, encoding="utf-8")
    identidad_real = log.stat()
    marca_vieja = _MarcaDelLog(
        tamano=1000,
        ino=identidad_real.st_ino + 1,  # deliberadamente OTRA identidad
        dev=identidad_real.st_dev,
    )

    veredicto = _leer_engine_log({log: marca_vieja})

    assert veredicto.fallas == (), "una identidad distinta no es atribuible aunque el tamaño haya crecido"
    assert veredicto.texto == ""


def test_identidad_distinta_deja_un_breadcrumb(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    log.write_text("contenido nuevo\n", encoding="utf-8")
    identidad_real = log.stat()
    marca_vieja = _MarcaDelLog(tamano=1, ino=identidad_real.st_ino + 1, dev=identidad_real.st_dev)

    with caplog.at_level(logging.WARNING):
        _leer_engine_log({log: marca_vieja})

    assert any("identidad" in r.message for r in caplog.records)


def test_misma_identidad_con_crecimiento_normal_si_decide(tmp_path: pathlib.Path) -> None:
    """El caso común (apéndice real, sin truncar) no debe romperse por el
    chequeo de identidad nuevo: es el MISMO archivo, sólo creció."""
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    log.write_text("x" * 100, encoding="utf-8")
    marcas = _marcas_del_log((log,))

    with log.open("a", encoding="utf-8") as handle:
        handle.write("ERROR: Assembler > mod.xml > parse > nodo > falló\n")

    veredicto = _leer_engine_log(marcas)

    assert veredicto.fallas != ()
    assert "mod.xml" in veredicto.texto


def test_marca_ausente_no_dispara_el_chequeo_de_identidad(tmp_path: pathlib.Path) -> None:
    """Sin identidad previa (marca en cero, archivo no existía al medir) no hay
    nada que comparar: se lee el archivo nuevo desde el byte 0 normalmente."""
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    marcas = _marcas_del_log((log,))
    assert marcas[log] == _MarcaDelLog(tamano=0, ino=0, dev=0)

    log.write_text("ERROR: Assembler > mod.xml > parse > nodo > falló\n", encoding="utf-8")

    veredicto = _leer_engine_log(marcas)

    assert veredicto.fallas != ()


# --------------------------------------------------------------------------- #
# La ventana de lectura ya no trunca el escaneo (hallazgo qodo, PR #439, 3ra ronda)
#
# Antes, un ``read()`` único acotado a un techo fijo desde la marca sólo veía
# el PRIMER bloque del apéndice: un run que logueara más que ese techo antes
# de un ``ERROR`` final lo perdía en silencio — el mismo falso verde que este
# módulo existe para cerrar. Ahora el tramo se recorre ENTERO en bloques
# acotados en memoria (``_TAMANO_DE_BLOQUE``), sin importar su tamaño total.
# --------------------------------------------------------------------------- #


async def test_error_mas_alla_de_varios_bloques_de_info_se_detecta(tmp_path: pathlib.Path) -> None:
    """El escenario exacto de la revisión: mucho ``INFO`` antes de un ``ERROR`` final."""
    runner, log = _preparar(tmp_path)
    mucho_info = "INFO: Assembler > x > parse > y > ok\n" * 400_000
    assert len(mucho_info.encode("utf-8")) > _TAMANO_DE_BLOQUE * 8, "el fixture debe cruzar varios bloques"

    resultado = await _correr(
        runner,
        escribe=mucho_info + "ERROR: Assembler > mod.xml > parse > nodo > falló\n",
        log=log,
    )

    assert resultado.success is False
    assert "mod.xml" in resultado.message


def test_severidad_partida_exactamente_en_un_limite_de_bloque_se_detecta(tmp_path: pathlib.Path) -> None:
    """Corrección de límite: el corte del PRIMER bloque leído cae EXACTAMENTE
    dentro de la línea que matchea. Sin reensamblar la línea pendiente entre
    bloques, el corte partiría el match y ninguno de los dos lados lo
    reconocería.
    """
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    linea_de_relleno = b"INFO: relleno\n"
    linea_de_error = b"ERROR: Assembler > mod.xml > parse > nodo > fallo\n"  # ASCII, sin acentos a proposito

    relleno = bytearray()
    while len(relleno) + len(linea_de_relleno) <= _TAMANO_DE_BLOQUE - 10:
        relleno += linea_de_relleno
    contenido = bytes(relleno) + linea_de_error
    # El primer read() de _escanear_severidades trae exactamente _TAMANO_DE_BLOQUE
    # bytes: confirmar que ese corte cae DENTRO de linea_de_error, no antes ni después.
    assert len(relleno) < _TAMANO_DE_BLOQUE < len(relleno) + len(linea_de_error)
    log.write_bytes(contenido)

    with log.open("rb") as handle:
        veredicto = _escanear_severidades(handle, 0, len(contenido))

    assert veredicto.fallas != ()
    assert "mod.xml" in veredicto.fallas[0]


def test_linea_sin_salto_no_crece_sin_cota(tmp_path: pathlib.Path) -> None:
    """Un archivo sin ningún ``\\n`` (binario/corrupto) no debe hacer crecer el
    buffer de línea pendiente sin límite ni reventar el escaneo."""
    log = tmp_path / NOMBRE_DEL_LOG_DE_PANDORA
    log.write_bytes(b"x" * (_TAMANO_DE_BLOQUE * 5))

    with log.open("rb") as handle:
        veredicto = _escanear_severidades(handle, 0, log.stat().st_size)

    assert veredicto.fallas == ()
    assert veredicto.advertencias == ()
