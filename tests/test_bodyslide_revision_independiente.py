"""Revisión independiente de PR #1003 (CHANGES_REQUESTED): P1-A, P1-B, P1-C y tipos.

Cada test de este archivo se escribió ANTES del fix y se verificó ROJO sobre
``e2370fb7`` (evidencia en el cuerpo del PR):

- P1-A — el destino del batch debe ser EXACTAMENTE el target canónico de su
  grupo (``bodyslide_output_target(game, group)``): ni la raíz compartida, ni
  la carpeta de otro grupo, ni un subdirectorio. ``_confinado`` sólo exige
  prefijo, así que un grupo CBBE con destino en la carpeta de 3BA pasaba.
- P1-B — un log EXISTENTE pero ilegible al tomar el snapshot no puede
  confundirse con "no había log": si se confunde, el log viejo se atribuye a la
  corrida nueva cuando vuelve a ser legible.
- P1-C — cancelar el ``await`` de la siembra no detiene su hilo; la
  restauración del ``finally`` no puede correr mientras la siembra todavía
  puede escribir. Sincronización determinista con ``threading.Event`` (sin
  sleeps); los ``wait(timeout)`` son sólo guardas contra cuelgues.
- ``resolver_conflictos`` no acepta una tupla de dicts (antes: ``ValueError``
  crudo o un mapeo equivocado).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.local.tools.bodyslide_headless as headless
import sky_claw.local.tools.bodyslide_runner as mod
from sky_claw.local.tools.bodyslide_headless import (
    ConflictoDeSalida,
    HeadlessContractError,
    ManifiestoArtefactos,
    resolver_conflictos,
)
from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner

_CONFIG_VÁLIDA = "<Config>\n  <TargetGame>4</TargetGame>\n</Config>\n"
_GUARDA_S = 10.0  # guarda contra cuelgues, NO sincronización


def _runner(tmp_path: pathlib.Path) -> BodySlideRunner:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG_VÁLIDA, encoding="utf-8")
    return BodySlideRunner(BodySlideConfig(bodyslide_exe=exe, game_path=tmp_path / "game", quiet_verified=True))


def _raiz(tmp_path: pathlib.Path) -> pathlib.Path:
    return (tmp_path / "game").resolve() / "BodySlide_Output"


def _proceso_que_construye(destino: pathlib.Path) -> AsyncMock:
    def _efecto(*_a, **_k):
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        return (b"", b"", 0)

    return AsyncMock(side_effect=_efecto)


# --------------------------------------------------------------------------- #
# P1-A — aislamiento de salida por grupo (runner)
# --------------------------------------------------------------------------- #


async def test_p1a_grupo_cbbe_con_destino_en_la_carpeta_de_3ba_no_spawnea(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    ajeno = _raiz(tmp_path) / "3BA"
    ajeno.mkdir(parents=True)
    proceso = _proceso_que_construye(ajeno)
    with patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", str(ajeno))
    assert proceso.await_count == 0, "un grupo no puede escribir en la carpeta de otro grupo"
    assert result.success is False
    assert result.estado == "ARGS_INVALID"


async def test_p1a_destino_en_la_raiz_compartida_no_spawnea(tmp_path: pathlib.Path) -> None:
    """Primer run: la raíz todavía no existe, así que el chequeo físico no la ve."""
    runner = _runner(tmp_path)
    raiz = _raiz(tmp_path)
    proceso = _proceso_que_construye(raiz)
    with patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", str(raiz))
    assert proceso.await_count == 0, "la raíz compartida no es el target de ningún grupo"
    assert result.estado == "ARGS_INVALID"


async def test_p1a_subdirectorio_del_propio_grupo_no_es_el_target(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    sub = _raiz(tmp_path) / "CBBE" / "sub"
    proceso = _proceso_que_construye(sub)
    with patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", str(sub))
    assert proceso.await_count == 0
    assert result.estado == "ARGS_INVALID"


async def test_p1a_destino_canonico_del_grupo_si_spawnea(tmp_path: pathlib.Path) -> None:
    """Control: el target canónico sigue pasando (no se cierra de más)."""
    runner = _runner(tmp_path)
    propio = _raiz(tmp_path) / "CBBE"
    propio.mkdir(parents=True)
    proceso = _proceso_que_construye(propio)
    with patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", str(propio))
    assert proceso.await_count == 1
    assert result.estado != "ARGS_INVALID"
    assert result.artifacts_built == 1


@pytest.mark.skipif(sys.platform != "win32", reason="sólo en NTFS una ruta con otra capitalización es la MISMA carpeta")
async def test_p1a_variante_de_mayusculas_en_windows_es_el_mismo_target(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    propio = _raiz(tmp_path) / "CBBE"
    propio.mkdir(parents=True)
    variante = str(propio).upper().replace("\\", "/") + "/"
    proceso = _proceso_que_construye(propio)
    with patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", variante)
    assert proceso.await_count == 1
    assert result.estado != "ARGS_INVALID"


# --------------------------------------------------------------------------- #
# P1-A — contrato puro: igualdad con el target canónico (variantes Windows)
# --------------------------------------------------------------------------- #

_CANON_WIN = r"C:\Games\Skyrim Special Edition\BodySlide_Output\CBBE"
_CANON_UNC = r"\\srv\juegos\SSE\BodySlide_Output\CBBE"


@pytest.mark.parametrize(
    "target",
    [
        _CANON_WIN,
        r"c:\games\skyrim special edition\bodyslide_output\cbbe",
        "C:/Games/Skyrim Special Edition/BodySlide_Output/CBBE",
        r"C:\Games/Skyrim Special Edition\BodySlide_Output/CBBE",
        _CANON_WIN + "\\",
        "C:/Games/Skyrim Special Edition/BodySlide_Output/CBBE/",
    ],
)
def test_p1a_contrato_acepta_variantes_windows_del_mismo_target(target: str) -> None:
    headless.exigir_destino_canonico(target, _CANON_WIN)


@pytest.mark.parametrize(
    "target",
    [
        r"C:\Games\Skyrim Special Edition\BodySlide_Output\3BA",
        r"C:\Games\Skyrim Special Edition\BodySlide_Output",
        r"C:\Games\Skyrim Special Edition\BodySlide_Output\CBBE\sub",
        r"D:\Games\Skyrim Special Edition\BodySlide_Output\CBBE",
        r"C:\Games\Skyrim Special Edition\BodySlide_Output\CBBE\..\3BA",
        r"\\srv\juegos\SSE\BodySlide_Output\CBBE",
        "Games/Skyrim Special Edition/BodySlide_Output/CBBE",
    ],
)
def test_p1a_contrato_rechaza_otro_grupo_raiz_subdir_otro_volumen(target: str) -> None:
    with pytest.raises(HeadlessContractError):
        headless.exigir_destino_canonico(target, _CANON_WIN)


def test_p1a_contrato_unc_igual_acepta_y_otro_servidor_rechaza() -> None:
    headless.exigir_destino_canonico(r"\\SRV\Juegos\sse\BodySlide_Output\cbbe", _CANON_UNC)
    headless.exigir_destino_canonico("//srv/juegos/SSE/BodySlide_Output/CBBE/", _CANON_UNC)
    with pytest.raises(HeadlessContractError):
        headless.exigir_destino_canonico(r"\\otro\juegos\SSE\BodySlide_Output\CBBE", _CANON_UNC)
    with pytest.raises(HeadlessContractError):
        headless.exigir_destino_canonico(r"\\srv\juegos\SSE\BodySlide_Output\3BA", _CANON_UNC)


def test_p1a_contrato_posix_distingue_mayusculas() -> None:
    canon = "/juegos/sse/BodySlide_Output/CBBE"
    headless.exigir_destino_canonico(canon + "/", canon)
    with pytest.raises(HeadlessContractError):
        headless.exigir_destino_canonico("/juegos/sse/BodySlide_Output/cbbe", canon)
    with pytest.raises(HeadlessContractError):
        headless.exigir_destino_canonico("/juegos/sse/BodySlide_Output/3BA", canon)


# --------------------------------------------------------------------------- #
# P1-B — snapshot del log fallido ≠ log ausente
# --------------------------------------------------------------------------- #


async def test_p1b_snapshot_ilegible_y_log_viejo_legible_despues_jamas_success(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    log = exe.parent / "Log_BS.txt"
    # Log VIEJO, firmado y limpio, de una corrida anterior.
    log.write_text("[#Log 2026-11-20]\nAll sets processed successfully!\n", encoding="utf-8")
    destino = _raiz(tmp_path) / "CBBE"
    destino.mkdir(parents=True)

    lecturas_del_log = 0
    real_read_bytes = pathlib.Path.read_bytes

    def _read_bytes(self: pathlib.Path) -> bytes:
        nonlocal lecturas_del_log
        if self.name == "Log_BS.txt":
            lecturas_del_log += 1
            if lecturas_del_log == 1:
                # El snapshot inicial no puede leerlo (AV, lock de otro proceso…).
                raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    proceso = _proceso_que_construye(destino)  # artefacto NUEVO, el log NO se toca
    manifiesto = ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="catalogo-test")
    with patch.object(pathlib.Path, "read_bytes", _read_bytes), patch.object(mod, "run_capture", proceso):
        result = await runner.run_batch("CBBE", str(destino), manifest=manifiesto)

    assert result.success is False, "un log viejo no puede atribuirse a la corrida por un snapshot fallido"
    assert result.estado != "SUCCESS"


def test_p1b_snapshot_distingue_ausente_de_ilegible(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    assert mod._snapshot_log_inicial(exe) is None  # ausente: legítimo
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nviejo\n", encoding="utf-8")

    def _falla(self: pathlib.Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(self))

    with patch.object(pathlib.Path, "read_bytes", _falla), pytest.raises(mod.BodySlideExecutionError):
        mod._snapshot_log_inicial(exe)


# --------------------------------------------------------------------------- #
# P1-C — la siembra cancelada no escribe después de la restauración
# --------------------------------------------------------------------------- #


async def test_p1c_cancelar_durante_la_siembra_no_deja_la_config_sembrada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La siembra nunca escribe después de la restauración (P1-C), ahora con C3 (PR 2c-1).

    Cancelar con la siembra en vuelo la ABANDONA: la cuarentena queda escrita antes de
    soltar al caller, y el propio hilo de la siembra —el último escritor— restaura al
    terminar y retira la cuarentena. Nadie restaura con la siembra todavía en vuelo.
    """
    from sky_claw.local.tools import bodyslide_journal as journal_mod

    runner = _runner(tmp_path)
    config = runner.config.bodyslide_exe.parent / "Config.xml"
    original = config.read_bytes()
    destino = _raiz(tmp_path) / "CBBE"
    destino.mkdir(parents=True)
    almacen = journal_mod.AlmacenDeJournal()

    entro = threading.Event()  # la siembra empezó (en su hilo)
    liberar = threading.Event()  # el test deja que la siembra escriba
    sembrar_real = journal_mod.TransaccionDeConfig.sembrar

    def _siembra_lenta(self: journal_mod.TransaccionDeConfig) -> None:
        entro.set()
        assert liberar.wait(_GUARDA_S), "guarda: el test nunca liberó la siembra"
        sembrar_real(self)

    monkeypatch.setattr(journal_mod.TransaccionDeConfig, "sembrar", _siembra_lenta)

    proceso = AsyncMock(return_value=(b"", b"", 0))
    with patch.object(mod, "run_capture", proceso):
        tarea = asyncio.create_task(runner.run_batch("CBBE", str(destino)))
        assert await asyncio.to_thread(entro.wait, _GUARDA_S)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        assert almacen.marcador_de_cuarentena.exists(), "responder no es soltar: la cuarentena va antes"
        assert config.read_bytes() == original, "nadie restauró ni sembró todavía"
        liberar.set()

        def _cuarentena_retirada() -> bool:
            limite = time.monotonic() + _GUARDA_S
            while time.monotonic() < limite:
                if not almacen.marcador_de_cuarentena.exists():
                    return True
                time.sleep(0.02)
            return False

        assert await asyncio.to_thread(_cuarentena_retirada), "el hilo de la siembra debe cerrar la corrida"

    assert proceso.await_count == 0
    assert config.read_bytes() == original, "la siembra escribió después de la restauración"


# --------------------------------------------------------------------------- #
# resolver_conflictos — tipo declarado
# --------------------------------------------------------------------------- #


def test_resolver_conflictos_rechaza_tupla_de_dicts_con_error_de_contrato() -> None:
    conflictos = [ConflictoDeSalida("meshes/femalebody.nif", ("Outfit A", "Outfit B"))]
    with pytest.raises(HeadlessContractError):
        resolver_conflictos(conflictos, ({"meshes/femalebody.nif": "Outfit A"},))  # type: ignore[arg-type]
    # Un dict de dos claves dentro de la tupla tampoco se reinterpreta como par.
    with pytest.raises(HeadlessContractError):
        resolver_conflictos(conflictos, ({"meshes/femalebody.nif": "x", "Outfit A": "y"},))  # type: ignore[arg-type]


def test_resolver_conflictos_mapping_sigue_funcionando() -> None:
    conflictos = [ConflictoDeSalida("meshes/femalebody.nif", ("Outfit A", "Outfit B"))]
    resolver_conflictos(conflictos, {"meshes/femalebody.nif": "Outfit A"})


# --------------------------------------------------------------------------- #
# P1-D (segunda revisión de #1003) — alias físicos de nombres de grupo en Win32
# --------------------------------------------------------------------------- #
#
# El grupo es el ÚLTIMO componente del destino canónico (``BodySlide_Output/<group>``).
# Win32 descarta puntos y espacios finales (``CBBE.`` ≡ ``CBBE``) y reserva nombres
# de dispositivo (``CON``, ``NUL``, ``COM1``… también con extensión: ``CON.txt``):
# esos nombres no son carpetas físicas independientes. Se rechazan antes de mutar
# o spawnear. Los nombres válidos existentes siguen pasando.

_GRUPOS_ALIAS_WIN32 = [
    "CBBE.",
    "CBBE..",
    "CBBE ",
    " CBBE",
    "CBBE. ",
    "CON",
    "con",
    "Con",
    "PRN",
    "AUX",
    "NUL",
    "COM0",
    "COM1",
    "com9",
    "LPT0",
    "LPT1",
    "lpt9",
    "COM¹",
    "LPT³",
    "CON.txt",
    "nul.body",
    "AUX.",
    "CON .x",
    "COM1 .tri",
    "CBBE:ads",
    "CBBE*",
    "CBBE?",
    'CB"BE',
    "CB<BE",
    "CB>BE",
    "CB|BE",
    "CB\tBE",
    "CB\x1fBE",
]

_GRUPOS_VALIDOS = [
    "CBBE",
    "CBBE Body",
    "Build",
    "3BA",
    "Preset 1.0",
    "CBBE_SE-v2",
    "Console",
    "CONFIG",
    "COM10",
    "LPT",
    "NULx",
    "AUX_body",
    "con-body",
    "BHUNP 3BBB Advanced",
]


@pytest.mark.parametrize("group", _GRUPOS_ALIAS_WIN32)
def test_p1d_contrato_rechaza_grupos_que_no_son_carpetas_win32_independientes(group: str) -> None:
    with pytest.raises(HeadlessContractError):
        headless.construir_argv("C:/BodySlide/BodySlide.exe", group, "C:/salida")


@pytest.mark.parametrize("group", _GRUPOS_VALIDOS)
def test_p1d_contrato_conserva_los_grupos_validos(group: str) -> None:
    argv = headless.construir_argv("C:/BodySlide/BodySlide.exe", group, "C:/salida")
    assert argv[1:3] == ["-gbuild", group]


@pytest.mark.parametrize("group", ["CBBE.", "CON", "nul.body", "COM1 .tri", "CBBE "])
async def test_p1d_runner_rechaza_alias_win32_sin_mutar_ni_spawnear(tmp_path: pathlib.Path, group: str) -> None:
    runner = _runner(tmp_path)
    config = runner.config.bodyslide_exe.parent / "Config.xml"
    antes = config.read_bytes()
    destino = _raiz(tmp_path) / group  # el target "canónico" que derivaría el nombre crudo
    proceso = _proceso_que_construye(destino)
    sembrar = MagicMock(return_value=True)  # la siembra real es síncrona (corre en un hilo)
    with patch.object(mod, "run_capture", proceso), patch.object(mod, "calcular_config_sembrada", sembrar):
        result = await runner.run_batch(group, str(destino))
    assert proceso.await_count == 0, "un alias Win32 no puede lanzar BodySlide"
    assert sembrar.call_count == 0, "la siembra (mutación) no puede correr"
    assert result.success is False
    assert result.estado == "ARGS_INVALID"
    assert config.read_bytes() == antes, "Config.xml debe quedar intacto"
    assert not (_raiz(tmp_path)).exists(), "no se crea nada bajo el staging"
