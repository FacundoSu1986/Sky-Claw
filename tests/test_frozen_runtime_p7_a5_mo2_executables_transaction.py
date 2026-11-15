"""Tests transaccionales y recuperables para migración de [customExecutables] de MO2 (P7-A5 PR 2).

Verifica la taxonomía de ejecutables, planificación inmutable, escritura quirúrgica
simultánea con gamePath, persistencia en registro durable, comprobación de autoría,
recuperación ante interrupción y gobernanza en preflight.
"""

from __future__ import annotations

import json
import pathlib
import tracemalloc
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightVerdict,
    BinderTransitionPolicy,
    PreflightCheckCode,
    PreflightCheckStatus,
    PreflightReasonCode,
    _ejecutables_dependientes_de_steam,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.binder_preflight import (
    _argumentos_dependientes_de_steam as _preflight_argumentos_dependientes_de_steam,
)
from sky_claw.local.frozen_runtime.binder_recovery import (
    RegistroCorruptoError,
    _coincide_con_lo_escrito,
    capturar_registro,
    cargar_registro,
    escribir_registro_locked,
    restaurar_registro_locked,
)
from sky_claw.local.frozen_runtime.clones import (
    cargar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime import mo2_ini as mo2_ini_mod
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.mo2_executables_plan import (
    ClasificacionEjecutableMO2,
    MutacionCampoEjecutable,
    _analizar_variantes_argumentos,
    _argumentos_dependientes_de_steam,
    _contiene_referencia_a_ruta,
    _dentro_de_area_steamapps,
    _desescapar_qt_argumentos,
    _tiene_escape_argumento_malformado,
    planificar_migracion_ejecutables,
)
from sky_claw.local.frozen_runtime.mo2_ini_escritura import (
    EscrituraIniNoVerificadaError,
    FormatoIniNoAdmitidoError,
    actualizar_game_path_en_ini,
    actualizar_game_path_por_handle,
    prever_bytes,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.runtime_clone_binder import (
    BinderPreflightFailedError,
    BinderResult,
    ejecutar_transicion_binder,
)
from sky_claw.local.frozen_runtime.storage import recovery_state_dir
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    RuntimeObservationError,
)
from tests._rig_p7_a5 import procedencia_con_vinculo_mo2
from tests.test_frozen_runtime_p5c1_mo2_clone_binder import (
    _crear_config_toml,
    _preparar_aprobacion,
)
from tests.test_frozen_runtime_p7_a5_stock_manual_preflight import (
    RigStockManual,
    _escribir_ini_mo2,
)

if TYPE_CHECKING:
    pass


@pytest.fixture(autouse=True)
def _parche_identidad_pe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observación PE sintética para árboles de test (sin depender de binarios propietarios)."""

    def _identidad(raiz: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        exe = pathlib.Path(raiz) / "SkyrimSE.exe"
        if not exe.is_file():
            raise RuntimeObservationError(f"SkyrimSE.exe ausente en '{raiz}'")
        raw = exe.read_bytes()
        if not raw.startswith(b"SkyrimSE-"):
            raise RuntimeObservationError(f"PE inválido en '{exe}'")
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version="1.6.1170.0",
            observed_exe_path=str(exe),
            observed_at_ns=1,
        )

    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.provisioning.observe_runtime_identity_from_root",
        _identidad,
    )
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.clones.observe_runtime_identity_from_root",
        _identidad,
    )
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root",
        _identidad,
    )


def test_clasificacion_ejecutable_migrable_al_clone(tmp_path: pathlib.Path) -> None:
    """Un ejecutable dentro del runtime saliente con destino presente en el clone es MIGRABLE_AL_CLONE."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    skse_src = outgoing / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE_SRC")

    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()
    skse_dst = clone / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE_DST")

    exe = Mo2Executable(
        index=1,
        title="SKSE",
        binary=str(skse_src).replace("\\", "/"),
        working_directory=str(outgoing).replace("\\", "/"),
        arguments='-profile "Default"',
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert not plan.bloqueado
    assert len(plan.entradas) == 1
    entrada = plan.entradas[0]
    assert entrada.index == 1
    assert entrada.clasificacion is ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE
    assert entrada.binary_nuevo == str(skse_dst).replace("\\", "/")
    assert entrada.working_dir_nuevo == str(clone).replace("\\", "/")
    assert entrada.arguments_nuevo == '-profile "Default"'

    assert len(plan.mutaciones) == 2
    assert (
        MutacionCampoEjecutable(1, "binary", str(skse_src).replace("\\", "/"), str(skse_dst).replace("\\", "/"))
        in plan.mutaciones
    )
    assert (
        MutacionCampoEjecutable(1, "workingDirectory", str(outgoing).replace("\\", "/"), str(clone).replace("\\", "/"))
        in plan.mutaciones
    )


def test_clasificacion_ejecutable_externo_preservable(tmp_path: pathlib.Path) -> None:
    """Una herramienta externa legítima fuera del runtime y de Steam es EXTERNA_PRESERVABLE sin mutaciones."""
    tools_dir = tmp_path / "Tools" / "SSEEdit"
    tools_dir.mkdir(parents=True)
    sseedit = tools_dir / "SSEEdit.exe"
    sseedit.write_bytes(b"MZ_XEDIT")

    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    exe = Mo2Executable(
        index=2,
        title="xEdit",
        binary=str(sseedit).replace("\\", "/"),
        working_directory=str(tools_dir).replace("\\", "/"),
        arguments="-IKnowWhatImDoing",
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert not plan.bloqueado
    assert len(plan.entradas) == 1
    entrada = plan.entradas[0]
    assert entrada.clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE
    assert entrada.binary_nuevo is None
    assert entrada.working_dir_nuevo is None
    assert len(plan.mutaciones) == 0


def test_clasificacion_ejecutable_dependiente_de_steam(tmp_path: pathlib.Path) -> None:
    """Un ejecutable que referencia rutas de Steam se clasifica como DEPENDIENTE_DE_STEAM y bloquea."""
    steam_root = tmp_path / "Steam" / "steamapps" / "common" / "Skyrim Special Edition"
    steam_root.mkdir(parents=True)
    steam_exe = steam_root / "SkyrimSE.exe"
    steam_exe.write_bytes(b"MZ_STEAM_EXE")

    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    exe = Mo2Executable(
        index=3,
        title="Skyrim Steam Launcher",
        binary=str(steam_exe).replace("\\", "/"),
        working_directory=str(steam_root).replace("\\", "/"),
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=steam_root,
    )

    assert plan.bloqueado
    assert len(plan.entradas) == 1
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM
    assert any("Steam" in m for m in plan.motivos_bloqueo)


def test_clasificacion_ejecutable_obsoleta_o_inexistente(tmp_path: pathlib.Path) -> None:
    """Un binario inexistente en origen o destino en clone se clasifica como OBSOLETA_O_INEXISTENTE y bloquea."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    fantasma = outgoing / "inexistente.exe"

    exe = Mo2Executable(
        index=4,
        title="Inexistente",
        binary=str(fantasma).replace("\\", "/"),
        working_directory=str(outgoing).replace("\\", "/"),
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert plan.bloqueado
    assert len(plan.entradas) == 1
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.OBSOLETA_O_INEXISTENTE
    assert any("inexistente" in m.lower() for m in plan.motivos_bloqueo)


def test_clasificacion_ejecutable_ambigua_o_bloqueada(tmp_path: pathlib.Path) -> None:
    """Rutas relativas con saltos de directorio '../' o malformadas se clasifican como AMBIGUA_O_BLOQUEADA."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    exe = Mo2Executable(
        index=5,
        title="Ambigua",
        binary="../otro/relativo.exe",
        working_directory=None,
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert plan.bloqueado
    assert len(plan.entradas) == 1
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA


def test_no_reemplazo_global_de_substrings(tmp_path: pathlib.Path) -> None:
    """Una ruta que contenga el nombre del runtime como prefijo de carpeta no debe ser alterada."""
    outgoing = tmp_path / "Skyrim_Runtime"
    outgoing.mkdir()

    herramienta_parecida = tmp_path / "Skyrim_Runtime_Extras" / "Herramienta.exe"
    herramienta_parecida.parent.mkdir()
    herramienta_parecida.write_bytes(b"MZ_TOOL")

    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    exe = Mo2Executable(
        index=6,
        title="Herramienta Parecida",
        binary=str(herramienta_parecida).replace("\\", "/"),
        working_directory=str(herramienta_parecida.parent).replace("\\", "/"),
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert not plan.bloqueado
    entrada = plan.entradas[0]
    assert entrada.clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE
    assert len(plan.mutaciones) == 0


def test_preservacion_exacta_argumentos_complejos(tmp_path: pathlib.Path) -> None:
    """Los argumentos con comillas, barras y opciones complejas se preservan idénticos."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    skse_src = outgoing / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ")
    skse_dst = clone / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ")

    args_complejos = '-profile "Perfil Con Espacios" -option:foo/bar -flag="quoted value"'
    exe = Mo2Executable(
        index=1,
        title="SKSE",
        binary=str(skse_src).replace("\\", "/"),
        arguments=args_complejos,
    )

    plan = planificar_migracion_ejecutables(
        ejecutables=(exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=None,
    )

    assert not plan.bloqueado
    assert plan.entradas[0].arguments_nuevo == args_complejos


# ---------------------------------------------------------------------------
# Task 2: Escritura quirúrgica de gamePath y [customExecutables]
# ---------------------------------------------------------------------------


class IniRetenidoDouble:
    """Doble de test para IniRetenido que almacena bytes en memoria."""

    def __init__(self, datos: bytes) -> None:
        self._datos = datos
        self.escrituras: list[bytes] = []
        self.al_escribir_invocado = False

    def leer_bytes(self) -> bytes:
        return self._datos

    def escribir_bytes(self, datos: bytes, *, al_escribir: object = None) -> None:
        if callable(al_escribir):
            al_escribir()
            self.al_escribir_invocado = True
        self._datos = datos
        self.escrituras.append(datos)


def test_prever_bytes_con_mutaciones_de_ejecutables() -> None:
    """prever_bytes actualiza gamePath y entradas de [customExecutables] preservando el resto."""
    original = (
        b"[General]\r\n"
        b"gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_Old)\r\n"
        b"selected_profile=@ByteArray(Default)\r\n"
        b"\r\n"
        b"[customExecutables]\r\n"
        b"size=2\r\n"
        b"1\\title=SKSE\r\n"
        b"1\\binary=G:/Modding/Skyrim_Runtime_Old/skse64_loader.exe\r\n"
        b"1\\arguments=-profile Default\r\n"
        b"1\\workingDirectory=G:/Modding/Skyrim_Runtime_Old\r\n"
        b"2\\title=SSEEdit\r\n"
        b"2\\binary=G:/Modding/Tools/SSEEdit/SSEEdit.exe\r\n"
        b"\r\n"
        b"[Settings]\r\n"
        b"log_level=1\r\n"
    )

    mutaciones = (
        MutacionCampoEjecutable(
            1,
            "binary",
            "G:/Modding/Skyrim_Runtime_Old/skse64_loader.exe",
            "G:/Modding/Skyrim_Runtime_Clone/skse64_loader.exe",
        ),
        MutacionCampoEjecutable(
            1, "workingDirectory", "G:/Modding/Skyrim_Runtime_Old", "G:/Modding/Skyrim_Runtime_Clone"
        ),
    )

    nuevo_game_path = "G:\\Modding\\Skyrim_Runtime_Clone"
    previsto = prever_bytes(original, nuevo_game_path, plan_ejecutables=mutaciones)

    assert b"gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_Clone)\r\n" in previsto
    assert b"1\\binary=G:/Modding/Skyrim_Runtime_Clone/skse64_loader.exe\r\n" in previsto
    assert b"1\\workingDirectory=G:/Modding/Skyrim_Runtime_Clone\r\n" in previsto
    # Preservación de líneas no modificadas
    assert b"selected_profile=@ByteArray(Default)\r\n" in previsto
    assert b"1\\title=SKSE\r\n" in previsto
    assert b"1\\arguments=-profile Default\r\n" in previsto
    assert b"2\\title=SSEEdit\r\n" in previsto
    assert b"2\\binary=G:/Modding/Tools/SSEEdit/SSEEdit.exe\r\n" in previsto
    assert b"[Settings]\r\nlog_level=1\r\n" in previsto


def test_actualizar_game_path_en_ini_con_plan_ejecutables(tmp_path: pathlib.Path) -> None:
    """actualizar_game_path_en_ini muta quirúrgicamente el INI en disco y verifica readback."""
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Old_Path)\r\n"
        b"\r\n"
        b"[customExecutables]\r\n"
        b"size=1\r\n"
        b"1\\title=SKSE\r\n"
        b"1\\binary=C:/Old_Path/skse.exe\r\n"
        b"1\\workingDirectory=C:/Old_Path\r\n"
    )

    mutaciones = (
        MutacionCampoEjecutable(1, "binary", "C:/Old_Path/skse.exe", "D:/Clone_Path/skse.exe"),
        MutacionCampoEjecutable(1, "workingDirectory", "C:/Old_Path", "D:/Clone_Path"),
    )

    actualizar_game_path_en_ini(
        ini_path,
        "D:\\Clone_Path",
        plan_ejecutables=mutaciones,
    )

    gp_obs = leer_game_path_de_mo2(ini_path)
    assert gp_obs.path == "D:\\Clone_Path"

    exes = leer_ejecutables_de_mo2(ini_path)
    assert len(exes) == 1
    assert exes[0].binary == "D:/Clone_Path/skse.exe"
    assert exes[0].working_directory == "D:/Clone_Path"


def test_actualizar_game_path_por_handle_con_plan_ejecutables() -> None:
    """actualizar_game_path_por_handle actualiza INI bajo exclusión y verifica bytes devueltos."""
    original = (
        b"[General]\n"
        b"gamePath=@ByteArray(C:\\\\Old_Path)\n"
        b"\n"
        b"[customExecutables]\n"
        b"size=1\n"
        b"1\\title=SKSE\n"
        b"1\\binary=C:/Old_Path/skse.exe\n"
    )
    double = IniRetenidoDouble(original)
    mutaciones = (MutacionCampoEjecutable(1, "binary", "C:/Old_Path/skse.exe", "D:/Clone/skse.exe"),)

    resultado = actualizar_game_path_por_handle(
        double,
        "D:\\Clone",
        plan_ejecutables=mutaciones,
    )

    assert b"gamePath=@ByteArray(D:\\\\Clone)\n" in resultado
    assert b"1\\binary=D:/Clone/skse.exe\n" in resultado
    assert double.al_escribir_invocado is False


def test_idempotencia_escritura_ejecutables(tmp_path: pathlib.Path) -> None:
    """Llamar al escritor cuando el contenido ya tiene los valores pedidos no reescribe ni altera mtime."""
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(D:\\\\Target_Path)\r\n"
        b"\r\n"
        b"[customExecutables]\r\n"
        b"size=1\r\n"
        b"1\\title=Launcher\r\n"
        b"1\\binary=D:/Target_Path/SkyrimSE.exe\r\n"
    )

    mutaciones = (MutacionCampoEjecutable(1, "binary", "D:/Target_Path/SkyrimSE.exe", "D:/Target_Path/SkyrimSE.exe"),)

    mtime_inicial = ini_path.stat().st_mtime_ns
    bytes_iniciales = ini_path.read_bytes()

    actualizar_game_path_en_ini(
        ini_path,
        "D:\\Target_Path",
        plan_ejecutables=mutaciones,
    )

    assert ini_path.read_bytes() == bytes_iniciales
    assert ini_path.stat().st_mtime_ns == mtime_inicial


def test_preservacion_formato_crlf_y_bom_con_ejecutables(tmp_path: pathlib.Path) -> None:
    """Preserva BOM UTF-8 y CRLF en todas las líneas modificadas y ajenas."""
    bom = b"\xef\xbb\xbf"
    cuerpo = b"[General]\r\ngamePath=@ByteArray(C:\\\\Old)\r\n\r\n[customExecutables]\r\n1\\binary=C:/Old/app.exe\r\n"
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_bytes(bom + cuerpo)

    mutaciones = (MutacionCampoEjecutable(1, "binary", "C:/Old/app.exe", "D:/New/app.exe"),)

    actualizar_game_path_en_ini(
        ini_path,
        "D:\\New",
        plan_ejecutables=mutaciones,
    )

    final = ini_path.read_bytes()
    assert final.startswith(bom)
    assert b"\r\n" in final
    assert b"\n" not in final.replace(b"\r\n", b"")


def test_fallo_si_mutacion_especifica_clave_inexistente(tmp_path: pathlib.Path) -> None:
    """Falla cerrado con EscrituraIniNoVerificadaError si una mutación requerida no existe en el INI."""
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_bytes(
        b"[General]\r\ngamePath=@ByteArray(C:\\\\Old)\r\n\r\n[customExecutables]\r\n1\\binary=C:/Old/app.exe\r\n"
    )

    mutaciones_invalidas = (MutacionCampoEjecutable(99, "binary", "C:/Ghost.exe", "D:/Ghost.exe"),)

    with pytest.raises(EscrituraIniNoVerificadaError, match="no se encontraron las entradas"):
        actualizar_game_path_en_ini(
            ini_path,
            "D:\\New",
            plan_ejecutables=mutaciones_invalidas,
        )


# ---------------------------------------------------------------------------
# Task 3: Recuperación, discriminación y rollback durable
# ---------------------------------------------------------------------------


def test_capturar_y_recuperar_registro_con_mutaciones_ejecutables(tmp_path: pathlib.Path) -> None:
    """capturar_registro persiste mutaciones de ejecutables y cargar_registro las restaura fielmente."""
    storage = tmp_path / "storage"
    storage.mkdir()
    recovery_state_dir(storage).mkdir(parents=True)
    ini_path = tmp_path / "ModOrganizer.ini"
    ini_path.write_bytes(
        b"[General]\r\ngamePath=@ByteArray(C:\\\\Old)\r\n\r\n[customExecutables]\r\n1\\binary=C:/Old/app.exe\r\n"
    )

    mutaciones = (MutacionCampoEjecutable(1, "binary", "C:/Old/app.exe", "D:/Clone/app.exe"),)

    registro = capturar_registro(
        storage_root=storage,
        transition_id="tx_p7_a5_01",
        journal_revision=1,
        operation="bind_clone",
        target_clone_id="clone_01",
        target_generation_id="gen_01",
        expected_effective_path="D:\\Clone",
        mo2_ini_path=ini_path,
        config_path=None,
        environ_skyrim_path=None,
        plan_ejecutables=mutaciones,
    )

    assert registro.plan_ejecutables_mutaciones == (
        {"index": 1, "campo": "binary", "valor_previo": "C:/Old/app.exe", "valor_nuevo": "D:/Clone/app.exe"},
    )

    # Persistir y recargar
    escribir_registro_locked(None, storage, registro)
    recargado = cargar_registro(storage, "tx_p7_a5_01")
    assert recargado is not None
    assert recargado.plan_ejecutables_mutaciones == registro.plan_ejecutables_mutaciones


def test_coincide_con_lo_escrito_reconoce_game_path_y_ejecutables_mutados(tmp_path: pathlib.Path) -> None:
    """_coincide_con_lo_escrito exige que tanto gamePath como los ejecutables coincidan con la previsión."""
    storage = tmp_path / "storage"
    storage.mkdir()
    ini_path = tmp_path / "ModOrganizer.ini"
    original = b"[General]\r\ngamePath=@ByteArray(C:\\\\Old)\r\n\r\n[customExecutables]\r\n1\\binary=C:/Old/app.exe\r\n"
    ini_path.write_bytes(original)

    mutaciones = (MutacionCampoEjecutable(1, "binary", "C:/Old/app.exe", "D:/Clone/app.exe"),)

    registro = capturar_registro(
        storage_root=storage,
        transition_id="tx_p7_a5_02",
        journal_revision=1,
        operation="bind_clone",
        target_clone_id="clone_01",
        target_generation_id="gen_01",
        expected_effective_path="D:\\Clone",
        mo2_ini_path=ini_path,
        config_path=None,
        environ_skyrim_path=None,
        plan_ejecutables=mutaciones,
    )

    superficie_ini = registro.superficie("mo2_ini")
    assert superficie_ini is not None

    # Previsión completa (gamePath + customExecutables)
    previsto = prever_bytes(original, "D:\\Clone", plan_ejecutables=mutaciones)

    # Coincidencia exacta con escritura intentada -> True
    assert _coincide_con_lo_escrito(
        superficie_ini,
        registro,
        previsto,
        escritura_intentada=True,
    )

    # Si solo se modificó gamePath pero NO los ejecutables -> False
    solo_game_path = prever_bytes(original, "D:\\Clone", plan_ejecutables=None)
    assert not _coincide_con_lo_escrito(
        superficie_ini,
        registro,
        solo_game_path,
        escritura_intentada=True,
    )


def test_rollback_restaura_game_path_y_ejecutables(tmp_path: pathlib.Path) -> None:
    """restaurar_registro_locked revierte gamePath y [customExecutables] al snapshot previo."""
    storage = tmp_path / "storage"
    storage.mkdir()
    ini_path = tmp_path / "ModOrganizer.ini"
    original = (
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Original_Game)\r\n"
        b"\r\n"
        b"[customExecutables]\r\n"
        b"size=1\r\n"
        b"1\\title=SKSE\r\n"
        b"1\\binary=C:/Original_Game/skse.exe\r\n"
    )
    ini_path.write_bytes(original)

    mutaciones = (MutacionCampoEjecutable(1, "binary", "C:/Original_Game/skse.exe", "D:/Clone/skse.exe"),)

    registro = capturar_registro(
        storage_root=storage,
        transition_id="tx_p7_a5_03",
        journal_revision=1,
        operation="bind_clone",
        target_clone_id="clone_01",
        target_generation_id="gen_01",
        expected_effective_path="D:\\Clone",
        mo2_ini_path=ini_path,
        config_path=None,
        environ_skyrim_path=None,
        plan_ejecutables=mutaciones,
    )

    # Mutamos el INI completo
    actualizar_game_path_en_ini(ini_path, "D:\\Clone", plan_ejecutables=mutaciones)
    assert ini_path.read_bytes() != original

    # Ejecutamos rollback
    verificaciones = restaurar_registro_locked(
        None,
        storage,
        registro,
        {},
        solo_superficies=frozenset(["mo2_ini"]),
        escritura_intentada=True,
    )

    verif_ini = next((v for v in verificaciones if v.kind == "mo2_ini"), None)
    assert verif_ini is not None and verif_ini.ok
    assert ini_path.read_bytes() == original


# ---------------------------------------------------------------------------
# Task 4: Preflight y Gobernanza de Ejecutables (R4)
# ---------------------------------------------------------------------------


def test_preflight_bloquea_si_ejecutable_es_obsoleto_o_inexistente(tmp_path: pathlib.Path) -> None:
    """Preflight bajo STOCK_INDEPENDENT_TRANSITION bloquea con CONFIG_DIVERGENCE_DETECTED ante ejecutable inexistente."""
    rig = RigStockManual(tmp_path, steam_activo=True)
    fantasma = rig.manual_runtime / "fantasma.exe"

    _escribir_ini_mo2(
        rig.mo2_active_global / "modorganizer.ini",
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("Fantasma", str(fantasma).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )

    assessment = evaluar_binder_preflight(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_source,
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        approved_stock_reference_digest=rig.approved_digest,
        config_skyrim_path=str(rig.manual_runtime),
        exigir_config_skyrim_path=True,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        stock_source_root=rig.stock_source,
        probes=rig.probes_globales_activos(),
    )

    assert assessment.verdict is BinderPreflightVerdict.BLOCKED
    check_coh = next(c for c in assessment.checks if c.check is PreflightCheckCode.CONFIG_COHERENCE)
    assert check_coh.status is PreflightCheckStatus.FAIL
    assert check_coh.reason is PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
    assert "inexistente" in check_coh.detail.lower() or "fantasma" in check_coh.detail.lower()


def test_preflight_bloquea_si_ejecutable_depende_de_steam(tmp_path: pathlib.Path) -> None:
    """Preflight bajo STOCK_INDEPENDENT_TRANSITION bloquea con CONFIG_DIVERGENCE_DETECTED ante dependencia de Steam."""
    rig = RigStockManual(tmp_path, steam_activo=True)
    steam_exe = rig.managed_source / "SkyrimSE.exe"

    _escribir_ini_mo2(
        rig.mo2_active_global / "modorganizer.ini",
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("Steam Launcher", str(steam_exe).replace("\\", "/")),
        ],
    )

    assessment = evaluar_binder_preflight(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_source,
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        approved_stock_reference_digest=rig.approved_digest,
        config_skyrim_path=str(rig.manual_runtime),
        exigir_config_skyrim_path=True,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        stock_source_root=rig.stock_source,
        probes=rig.probes_globales_activos(),
    )

    assert assessment.verdict is BinderPreflightVerdict.BLOCKED
    check_coh = next(c for c in assessment.checks if c.check is PreflightCheckCode.CONFIG_COHERENCE)
    assert check_coh.status is PreflightCheckStatus.FAIL
    assert check_coh.reason is PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
    assert "steam" in check_coh.detail.lower()


def test_preflight_aprueba_plan_migracion_ejecutables_completo(tmp_path: pathlib.Path) -> None:
    """Preflight bajo STOCK_INDEPENDENT_TRANSITION aprueba y vincula plan de ejecutables verificado."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    tools_dir = tmp_path / "Tools" / "SSEEdit"
    tools_dir.mkdir(parents=True)
    sseedit = tools_dir / "SSEEdit.exe"
    sseedit.write_bytes(b"MZ_SSEEDIT")

    _escribir_ini_mo2(
        rig.mo2_active_global / "modorganizer.ini",
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
            ("xEdit", str(sseedit).replace("\\", "/"), str(tools_dir).replace("\\", "/")),
        ],
    )

    assessment = evaluar_binder_preflight(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_source,
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        approved_stock_reference_digest=rig.approved_digest,
        config_skyrim_path=str(rig.manual_runtime),
        exigir_config_skyrim_path=True,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        stock_source_root=rig.stock_source,
        probes=rig.probes_globales_activos(),
    )

    assert assessment.verdict is BinderPreflightVerdict.READY
    check_coh = next(c for c in assessment.checks if c.check is PreflightCheckCode.CONFIG_COHERENCE)
    assert check_coh.status is PreflightCheckStatus.PASS
    assert getattr(assessment, "plan_migracion_ejecutables", None) is not None
    plan = assessment.plan_migracion_ejecutables
    assert not plan.bloqueado
    assert len(plan.entradas) == 2


# ---------------------------------------------------------------------------
# Task P1 / P2: Auditoría Adversarial y Fixes
# ---------------------------------------------------------------------------


def test_p1_c_deteccion_steam_no_restringida_a_skyrim_special_edition() -> None:
    """_dentro_de_area_steamapps veta cualquier ruta bajo steamapps, no solo 'skyrim special edition'."""
    assert _dentro_de_area_steamapps(pathlib.Path("C:/Steam/steamapps/common/Skyrim Special Edition/SkyrimSE.exe"))
    assert _dentro_de_area_steamapps(pathlib.Path("C:/Steam/steamapps/downloading/489830/SkyrimSE.exe"))
    assert _dentro_de_area_steamapps(pathlib.Path("C:/Steam/steamapps/temp/489830/SkyrimSE.exe"))
    assert _dentro_de_area_steamapps(pathlib.Path("C:/Steam/steamapps/common/Fallout 4/Fallout4.exe"))
    assert not _dentro_de_area_steamapps(pathlib.Path("D:/Modding/Games/Skyrim/SkyrimSE.exe"))


def test_p1_b_dependencias_residuales_al_runtime_anterior_bloquean(tmp_path: pathlib.Path) -> None:
    """Referencias residuales en workingDirectory o arguments al runtime manual saliente bloquean la migración."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    tools_dir = tmp_path / "Tools" / "SSEEdit"
    tools_dir.mkdir(parents=True)
    sseedit = tools_dir / "SSEEdit.exe"
    sseedit.write_bytes(b"MZ_SSEEDIT")

    # Caso 1: Binario externo SSEEdit con arguments=-D "RUNTIME_MANUAL/Data" -> BLOCKED
    exe_sseedit_args = Mo2Executable(
        index=1,
        title="SSEEdit",
        binary=str(sseedit).replace("\\", "/"),
        arguments=f'-D "{outgoing / "Data"}"',
    )
    plan1 = planificar_migracion_ejecutables(
        ejecutables=(exe_sseedit_args,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan1.bloqueado
    assert plan1.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA
    assert "referencias residuales" in plan1.motivos_bloqueo[0].lower()

    # Caso 2: Binario externo con workingDirectory dentro de outgoing runtime -> BLOCKED
    exe_sseedit_wd = Mo2Executable(
        index=1,
        title="SSEEdit",
        binary=str(sseedit).replace("\\", "/"),
        working_directory=str(outgoing / "Data").replace("\\", "/"),
    )
    plan2 = planificar_migracion_ejecutables(
        ejecutables=(exe_sseedit_wd,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan2.bloqueado
    assert plan2.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA
    assert "workingdirectory" in plan2.motivos_bloqueo[0].lower()

    # Caso 3: Binario migrable (SKSE) con argumentos apuntando al runtime manual -> BLOCKED
    skse_src = outgoing / "skse.exe"
    skse_src.write_bytes(b"MZ")
    skse_dst = clone / "skse.exe"
    skse_dst.write_bytes(b"MZ")
    exe_skse_args = Mo2Executable(
        index=1,
        title="SKSE",
        binary=str(skse_src).replace("\\", "/"),
        arguments=f'-profile "{outgoing / "Profiles"}"',
    )
    plan3 = planificar_migracion_ejecutables(
        ejecutables=(exe_skse_args,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan3.bloqueado
    assert plan3.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA

    # Caso 4: Argumentos entrecomillados y rutas con espacios al runtime saliente -> BLOCKED
    outgoing_sp = tmp_path / "Runtime Manual Con Espacios"
    outgoing_sp.mkdir()
    exe_skse_espacios = Mo2Executable(
        index=1,
        title="SKSE",
        binary=str(skse_src).replace("\\", "/"),
        arguments=f'-o "{outgoing_sp / "Directorio Espacios"}"',
    )
    plan4 = planificar_migracion_ejecutables(
        ejecutables=(exe_skse_espacios,),
        outgoing_runtime_root=outgoing_sp,
        target_clone_root=clone,
    )
    assert plan4.bloqueado

    # Caso 5: Argumentos ya apuntando al Clone -> NO bloqueado
    exe_skse_clone = Mo2Executable(
        index=1,
        title="SKSE",
        binary=str(skse_src).replace("\\", "/"),
        arguments=f'-D "{clone / "Data"}"',
    )
    plan5 = planificar_migracion_ejecutables(
        ejecutables=(exe_skse_clone,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert not plan5.bloqueado
    assert plan5.entradas[0].clasificacion is ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE

    # Caso 6: Herramienta externa totalmente independiente -> EXTERNA_PRESERVABLE
    exe_loot = Mo2Executable(
        index=1,
        title="LOOT",
        binary=str(sseedit).replace("\\", "/"),
        working_directory=str(tools_dir).replace("\\", "/"),
        arguments="--game=SkyrimSE",
    )
    plan6 = planificar_migracion_ejecutables(
        ejecutables=(exe_loot,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert not plan6.bloqueado
    assert plan6.entradas[0].clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE


def test_p1_d_duplicados_y_anomalias_en_custom_executables_rechazados(tmp_path: pathlib.Path) -> None:
    """Fixtures INI con duplicados o anomalías son rechazados en planificación y previsión de bytes."""
    outgoing = tmp_path / "Runtime_Manual"
    outgoing.mkdir()
    clone = tmp_path / "Runtime_Clone"
    clone.mkdir()

    # Fixture 1: 1\binary duplicado con valores distintos
    ini1 = tmp_path / "ini1.ini"
    ini1.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Manual)\r\n"
        b"[customExecutables]\r\n"
        b"1\\title=Tool\r\n"
        b"1\\binary=C:/Manual/tool_a.exe\r\n"
        b"1\\binary=C:/Manual/tool_b.exe\r\n"
    )
    exes1 = leer_ejecutables_de_mo2(ini1)
    plan1 = planificar_migracion_ejecutables(exes1, outgoing, clone)
    assert plan1.bloqueado or any(
        e.clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA for e in plan1.entradas
    )
    with pytest.raises(FormatoIniNoAdmitidoError, match="[Cc]lave duplicada"):
        prever_bytes(ini1.read_bytes(), str(clone), plan_ejecutables=plan1)

    # Fixture 2: 1\workingDirectory duplicado
    ini2 = tmp_path / "ini2.ini"
    ini2.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Manual)\r\n"
        b"[customExecutables]\r\n"
        b"1\\title=Tool\r\n"
        b"1\\binary=C:/Manual/tool.exe\r\n"
        b"1\\workingDirectory=C:/DirA\r\n"
        b"1\\workingDirectory=C:/DirB\r\n"
    )
    exes2 = leer_ejecutables_de_mo2(ini2)
    plan2 = planificar_migracion_ejecutables(exes2, outgoing, clone)
    assert plan2.bloqueado
    with pytest.raises(FormatoIniNoAdmitidoError, match="[Cc]lave duplicada"):
        prever_bytes(ini2.read_bytes(), str(clone), plan_ejecutables=plan2)

    # Fixture 3: 1\arguments sin 1\binary
    ini3 = tmp_path / "ini3.ini"
    ini3.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Manual)\r\n"
        b"[customExecutables]\r\n"
        b"1\\title=Orphan\r\n"
        b"1\\arguments=-foo -bar\r\n"
    )
    exes3 = leer_ejecutables_de_mo2(ini3)
    plan3 = planificar_migracion_ejecutables(exes3, outgoing, clone)
    assert plan3.bloqueado

    # Fixture 4: Índice inválido y clave malformada
    ini4 = tmp_path / "ini4.ini"
    ini4.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Manual)\r\n"
        b"[customExecutables]\r\n"
        b"0\\binary=C:/Manual/zero.exe\r\n"
        b"malformed_entry_no_slash=val\r\n"
    )
    exes4 = leer_ejecutables_de_mo2(ini4)
    plan4 = planificar_migracion_ejecutables(exes4, outgoing, clone)
    assert plan4.bloqueado

    # Fixture 5: Sección [customExecutables] duplicada
    ini5 = tmp_path / "ini5.ini"
    ini5.write_bytes(
        b"[General]\r\n"
        b"gamePath=@ByteArray(C:\\\\Manual)\r\n"
        b"[customExecutables]\r\n"
        b"1\\binary=C:/Tools/loot.exe\r\n"
        b"[customExecutables]\r\n"
        b"2\\binary=C:/Tools/xedit.exe\r\n"
    )
    with pytest.raises(FormatoIniNoAdmitidoError, match="[Ss]ección.*duplicada"):
        prever_bytes(ini5.read_bytes(), str(clone))


def test_p2_recovery_fail_closed_ante_plan_ejecutables_corrupto(tmp_path: pathlib.Path) -> None:
    """Cargar registro con plan_ejecutables_mutaciones corrupto falla cerrado con RegistroCorruptoError."""
    storage = tmp_path / "storage"
    storage.mkdir()
    rec_dir = recovery_state_dir(storage)
    rec_dir.mkdir(parents=True)

    base_payload = {
        "schema_version": 1,
        "transition_id": "test-tid-001",
        "journal_revision": 1,
        "operation": "promotion",
        "target_clone_id": "clone-1",
        "target_generation_id": "gen-1",
        "expected_effective_path": "C:/Clone",
        "fase": "PREPARADO",
        "superficies": [
            {"kind": "active_json", "path": "C:/active.json", "existed": False},
            {"kind": "mo2_ini", "path": "C:/mo2/modorganizer.ini", "existed": False},
            {"kind": "config", "path": "C:/cfg/config.toml", "existed": False},
            {"kind": "environ", "path": None, "existed": False},
        ],
    }

    def _intentar(mut_val: object, match_err: str) -> None:
        p = dict(base_payload)
        p["plan_ejecutables_mutaciones"] = mut_val
        reg_file = rec_dir / "test-tid-001.json"
        reg_file.write_text(json.dumps(p), encoding="utf-8")
        with pytest.raises(RegistroCorruptoError, match=match_err):
            cargar_registro(storage, "test-tid-001")

    # 1. Elemento que no es lista
    _intentar("no_es_lista", "debe ser una lista")
    # 2. Elemento que no es dict
    _intentar(["no_es_dict"], "no es un objeto")
    # 3. Campos ausentes
    _intentar([{"index": 1, "campo": "binary"}], "faltantes")
    # 4. Tipos incorrectos
    _intentar([{"index": "1", "campo": "binary", "valor_previo": "A", "valor_nuevo": "B"}], "inválido")
    _intentar([{"index": 1, "campo": "binary", "valor_previo": 123, "valor_nuevo": "B"}], "no string")
    # 5. Índices duplicados para el mismo campo
    _intentar(
        [
            {"index": 1, "campo": "binary", "valor_previo": "A", "valor_nuevo": "B"},
            {"index": 1, "campo": "binary", "valor_previo": "A", "valor_nuevo": "C"},
        ],
        "mutación duplicada",
    )
    # 6. Campos desconocidos
    _intentar([{"index": 1, "campo": "binary", "valor_previo": "A", "valor_nuevo": "B", "injected": True}], "sobrantes")

    # 7. Registro antiguo sin plan_ejecutables_mutaciones -> se carga correctamente (retrocompatible)
    p_retro = dict(base_payload)
    reg_file = rec_dir / "test-tid-001.json"
    reg_file.write_text(json.dumps(p_retro), encoding="utf-8")
    reg_cargado = cargar_registro(storage, "test-tid-001")
    assert reg_cargado.plan_ejecutables_mutaciones is None


def test_p1_a_binder_transmite_politica_y_ejecuta_migracion(tmp_path: pathlib.Path) -> None:
    """ejecutar_transicion_binder transmite politica STOCK_INDEPENDENT_TRANSITION y ejecuta mutaciones de ejecutables."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    _escribir_ini_mo2(
        rig.mo2_active_global / "modorganizer.ini",
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    resultado: BinderResult = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )

    assert resultado.success
    # Verificar que el INI fue mutado con el clone y el ejecutable también
    exes = leer_ejecutables_de_mo2(rig.mo2_active_global / "modorganizer.ini")
    assert len(exes) == 1
    assert exes[0].binary == str(skse_dst).replace("\\", "/")
    assert exes[0].working_directory == str(rig.clone_root).replace("\\", "/")
    gp = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
    assert gp.path == str(rig.clone_root)


def test_integral_transicion_con_rollback_restaura_bytes_exactos(tmp_path: pathlib.Path) -> None:
    """Fallo en persistencia de config tras mutar el INI induce rollback que restaura el archivo byte a byte."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )
    ini_bytes_original = ini_file.read_bytes()

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    def _fallo_config(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "error de E/S simulado en persistencia de config")

    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.persistir_campo",
            side_effect=_fallo_config,
        ),
        pytest.raises(OSError, match="error de E/S simulado"),
    ):
        ejecutar_transicion_binder(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            target_clone_id=rig.clone_id,
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=rig.managed_source,
            config_path=cfg_path,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest=rig.approved_digest,
        )

    # Verificar que el rollback restauró el INI byte a byte
    assert ini_file.read_bytes() == ini_bytes_original


def test_p2_working_directory_destino_inexistente_bloquea_plan(tmp_path: pathlib.Path) -> None:
    """Si workingDirectory apunta a subcarpeta del runtime manual pero la equivalente no existe en Clone, el plan bloquea."""
    outgoing = tmp_path / "manual"
    clone = tmp_path / "clone"
    outgoing.mkdir()
    clone.mkdir()

    bin_manual = outgoing / "tools" / "tool.exe"
    bin_manual.parent.mkdir()
    bin_manual.write_bytes(b"MZ_TOOL")

    bin_clone = clone / "tools" / "tool.exe"
    bin_clone.parent.mkdir()
    bin_clone.write_bytes(b"MZ_TOOL")

    # workingDirectory existe en manual, pero NO en clone
    wd_manual = outgoing / "subfolder"
    wd_manual.mkdir()

    exe = Mo2Executable(
        index=1,
        title="Tool",
        binary=str(bin_manual),
        arguments="",
        working_directory=str(wd_manual),
    )

    plan = planificar_migracion_ejecutables(
        (exe,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )

    assert plan.bloqueado, "El plan debió bloquear al no existir la subcarpeta en Clone"
    assert plan.entradas[0].clasificacion == ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA
    assert any("Directorio de trabajo" in m for m in plan.motivos_bloqueo)

    # También verificar si el destino es archivo en vez de directorio
    wd_clone_archivo = clone / "subfolder_file"
    wd_clone_archivo.write_text("no_es_directorio")
    wd_manual_2 = outgoing / "subfolder_file"
    wd_manual_2.mkdir()
    exe2 = Mo2Executable(
        index=2,
        title="ToolFile",
        binary=str(bin_manual),
        arguments="",
        working_directory=str(wd_manual_2),
    )
    plan2 = planificar_migracion_ejecutables(
        (exe2,),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan2.bloqueado
    assert plan2.entradas[0].clasificacion == ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA
    assert any("no es un directorio" in m for m in plan2.motivos_bloqueo)


def test_p2_migracion_solo_ejecutables_cuando_gamepath_ya_es_objetivo(tmp_path: pathlib.Path) -> None:
    """Cuando gamePath ya tiene el valor objetivo pero [customExecutables] necesita cambiar, el escritor debe tener éxito quirúrgico."""
    ini_file = tmp_path / "modorganizer.ini"
    target_clone = tmp_path / "clone_target"
    target_clone.mkdir()

    _escribir_ini_mo2(
        ini_file,
        game_path=target_clone,
        base_directory=tmp_path / "mo2_base",
        ejecutables=[
            ("SKSE", "E:/old/manual/skse64_loader.exe", "E:/old/manual"),
        ],
    )

    nuevo_bin = str(target_clone / "skse64_loader.exe").replace("\\", "/")
    nuevo_wd = str(target_clone).replace("\\", "/")
    mutaciones = (
        MutacionCampoEjecutable(
            index=1, campo="binary", valor_previo="E:/old/manual/skse64_loader.exe", valor_nuevo=nuevo_bin
        ),
        MutacionCampoEjecutable(index=1, campo="workingDirectory", valor_previo="E:/old/manual", valor_nuevo=nuevo_wd),
    )

    # Debe ejecutarse sin EscrituraIniNoVerificadaError (error artificial de preservación)
    actualizar_game_path_en_ini(
        ini_file,
        nuevo_game_path=target_clone,
        plan_ejecutables=mutaciones,
    )

    # Verificar que gamePath no se alteró y que customExecutables fue actualizado
    gp = leer_game_path_de_mo2(ini_file)
    assert gp.path == str(target_clone)
    exes = leer_ejecutables_de_mo2(ini_file)
    assert len(exes) == 1
    assert exes[0].binary == nuevo_bin
    assert exes[0].working_directory == nuevo_wd

    # Segunda ejecución idéntica: debe ser un NO-OP sin modificar el archivo
    bytes_tras_primera = ini_file.read_bytes()
    actualizar_game_path_en_ini(
        ini_file,
        nuevo_game_path=target_clone,
        plan_ejecutables=mutaciones,
    )
    assert ini_file.read_bytes() == bytes_tras_primera


def test_p1_retorno_idempotente_no_se_emite_con_ejecutables_incoherentes(tmp_path: pathlib.Path) -> None:
    """Si gamePath y Desired ya apuntan a Clone y hay finalización previa, pero un ejecutable externo tiene argumentos al manual, el binder NO devuelve éxito."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    # 1. Primera transición: pasa de manual a Clone y finaliza normalmente
    res1: BinderResult = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )
    assert res1.success is True

    # 2. Ahora gamePath y Desired ya apuntan a Clone y hay finalización durable.
    # Inyectamos en [customExecutables] una herramienta externa (SSEEdit) que contiene
    # argumentos con referencias residuales al runtime manual.
    sseedit_bin = rig.manual_runtime.parent / "SSEEdit" / "SSEEdit.exe"
    sseedit_bin.parent.mkdir(parents=True, exist_ok=True)
    sseedit_bin.write_bytes(b"MZ_SSEEDIT")

    manual_data_arg = str(rig.manual_runtime).replace("\\", "/") + "/Data"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.clone_root,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_dst).replace("\\", "/"), str(rig.clone_root).replace("\\", "/")),
            ("SSEEdit", str(sseedit_bin).replace("\\", "/"), str(sseedit_bin.parent).replace("\\", "/")),
        ],
    )
    ini_texto = ini_file.read_text(encoding="utf-8")
    ini_texto = ini_texto.replace(
        "2\\workingDirectory=",
        f'2\\arguments=-D "{manual_data_arg}"\n2\\workingDirectory=',
    )
    ini_file.write_text(ini_texto, encoding="utf-8")

    # 3. Segunda llamada: bajo STOCK_INDEPENDENT_TRANSITION, NO debe devolver idempotent-noop
    # sino fallar en preflight por la referencia residual en ejecutables.
    with pytest.raises(BinderPreflightFailedError):
        ejecutar_transicion_binder(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            target_clone_id=rig.clone_id,
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=rig.managed_source,
            config_path=cfg_path,
            environ={"SKYRIM_PATH": str(rig.clone_root)},
            probes=rig.probes_globales_activos(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest=rig.approved_digest,
        )


def test_p1_retorno_idempotente_opt_in_exito_con_ejecutables_coherentes(tmp_path: pathlib.Path) -> None:
    """Si gamePath, Desired y customExecutables ya están completamente migrados y coherentes, el binder devuelve idempotent-noop."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    # 1. Primera transición: pasa de manual a Clone y finaliza normalmente
    res1: BinderResult = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )
    assert res1.success is True
    assert res1.transition_id != "idempotent-noop"

    # 2. Segunda ejecución idéntica: todo ya apunta a Clone y está coherente (0 mutaciones pendientes)
    res2: BinderResult = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.clone_root)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )
    assert res2.success is True
    assert res2.transition_id == "idempotent-noop"


def test_recuperacion_tras_corte_restaura_custom_executables_desde_registro_durable(tmp_path: pathlib.Path) -> None:
    """Simula una interrupción abrupta de proceso (corte) tras mutar el INI con customExecutables.

    Verifica que la recuperación en frío (startup/recovery locked) restaura el INI
    byte por byte desde el registro durable y no depende del contexto de una excepción en vuelo.
    """
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )
    ini_bytes_original = ini_file.read_bytes()

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    t_id = "test-corte-simulado-001"
    with participant_lock(rig.storage, timeout=10.0) as handle:
        handle.assert_owned()
        plan_ejecutables = planificar_migracion_ejecutables(
            ejecutables=leer_ejecutables_de_mo2(ini_file),
            outgoing_runtime_root=rig.manual_runtime,
            target_clone_root=rig.clone_root,
        )
        registro = recovery.capturar_registro(
            storage_root=rig.storage,
            transition_id=t_id,
            journal_revision=1,
            operation="PROMOTION",
            target_clone_id=rig.clone_id,
            target_generation_id=rig.generation_id,
            expected_effective_path=str(rig.clone_root),
            mo2_ini_path=ini_file,
            config_path=cfg_path,
            environ_skyrim_path=str(rig.manual_runtime),
            plan_ejecutables=plan_ejecutables,
        )
        recovery.escribir_registro_locked(handle, rig.storage, registro)

        # Mutar el INI como lo haría el binder en fase MO2_INI_ESCRITO
        actualizar_game_path_en_ini(
            ini_file,
            nuevo_game_path=rig.clone_root,
            plan_ejecutables=plan_ejecutables.mutaciones,
        )
        recovery.marcar_fase_locked(handle, rig.storage, transition_id=t_id, fase=recovery.FaseRecuperacion.INI_ESCRITO)

    # Verificamos que el INI quedó mutado (con la ruta del Clone) en el "momento del corte"
    assert ini_file.read_bytes() != ini_bytes_original
    gp_mutado = leer_game_path_de_mo2(ini_file)
    assert gp_mutado.path == str(rig.clone_root)

    # 1. Simulación de recuperación en frío (proceso nuevo tras reinicio/corte):
    # La recuperación en proceso nuevo no puede asumir autoría del INI sin evidencia en proceso,
    # por lo que clasifica la superficie como escritura ajena y preserva la evidencia durable.
    with participant_lock(rig.storage, timeout=10.0) as handle:
        handle.assert_owned()
        registro_cargado = recovery.cargar_registro(rig.storage, t_id)
        assert registro_cargado.plan_ejecutables_mutaciones is not None
        assert len(registro_cargado.plan_ejecutables_mutaciones) > 0
        verificaciones = recovery.restaurar_registro_locked(
            handle,
            rig.storage,
            registro_cargado,
            {},
        )
        ini_v = next(v for v in verificaciones if v.kind == recovery.SUPERFICIE_MO2_INI)
        assert ini_v.escritura_ajena is True
        assert ini_v.ok is True
        # El INI no se sobreescribe a ciegas sin prueba de autoría
        assert ini_file.read_bytes() != ini_bytes_original

    # 2. Con confirmación de autoría o decisión explícita de restauración (escritura_intentada=True):
    # El registro durable restaura byte a byte el INI previo al corte, incluyendo customExecutables.
    with participant_lock(rig.storage, timeout=10.0) as handle:
        handle.assert_owned()
        verificaciones_forzadas = recovery.restaurar_registro_locked(
            handle,
            rig.storage,
            registro_cargado,
            {},
            escritura_intentada=True,
        )
        ini_v2 = next(v for v in verificaciones_forzadas if v.kind == recovery.SUPERFICIE_MO2_INI)
        assert ini_v2.ok is True
        assert ini_v2.escritura_ajena is False

    # Verificamos que el INI se restauró byte por byte a su estado original previo al corte
    assert ini_file.read_bytes() == ini_bytes_original
    exes_restaurados = leer_ejecutables_de_mo2(ini_file)
    assert exes_restaurados[0].binary == str(skse_src).replace("\\", "/")
    assert exes_restaurados[0].working_directory == str(rig.manual_runtime).replace("\\", "/")


def test_adversarial_modificacion_ini_tras_liberar_exclusion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test adversarial: modificacion del INI tras liberar exclusion antes de VERIFY EFFECTIVE."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    manual_rt_str = str(rig.manual_runtime).replace("\\", "/")
    clone_rt_str = str(rig.clone_root).replace("\\", "/")

    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), manual_rt_str),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    from sky_claw.local.frozen_runtime.mo2_ini_exclusion import ExclusionIni

    orig_cerrar = ExclusionIni.cerrar
    modificacion_inyectada = False

    def interceptar_cerrar(self_exclusion: ExclusionIni) -> None:
        nonlocal modificacion_inyectada
        orig_cerrar(self_exclusion)
        texto = ini_file.read_text(encoding="utf-8")
        # El escritor migró workingDirectory al Clone.
        # Simulamos una modificación externa concurrente que conserva gamePath al Clone,
        # pero reintroduce la dependencia al runtime saliente en workingDirectory:
        if f"1\\workingDirectory={clone_rt_str}" in texto:
            texto_mutado = texto.replace(
                f"1\\workingDirectory={clone_rt_str}",
                f"1\\workingDirectory={manual_rt_str}",
            )
            ini_file.write_text(texto_mutado, encoding="utf-8")
            modificacion_inyectada = True

    monkeypatch.setattr(ExclusionIni, "cerrar", interceptar_cerrar)

    from sky_claw.local.frozen_runtime.journal import (
        JournalDisposition,
        cargar_journal_transicion,
    )
    from sky_claw.local.frozen_runtime.runtime_clone_binder import BinderVerificationError

    with pytest.raises(BinderVerificationError, match="modorganizer.ini divergente tras liberar exclusión"):
        ejecutar_transicion_binder(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            target_clone_id=rig.clone_id,
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=rig.managed_source,
            config_path=cfg_path,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest=rig.approved_digest,
        )

    assert modificacion_inyectada is True

    # 1. El rollback NO debe haber sobrescrito la modificación externa
    ini_post_rollback = ini_file.read_text(encoding="utf-8")
    assert f"1\\workingDirectory={manual_rt_str}" in ini_post_rollback

    # 2. La evidencia de la modificación externa debe haber quedado preservada
    evidence_dir = rig.storage / "recovery_evidence"
    assert evidence_dir.exists()
    archivos_evidencia = list(evidence_dir.rglob("mo2_ini-*.bin"))
    assert len(archivos_evidencia) >= 1

    # 3. El journal debe permanecer en estado recuperable (no FINALIZED)
    snap_journal = cargar_journal_transicion(rig.storage)
    assert snap_journal.disposition is not JournalDisposition.NONE


def test_p1_bytes_bajo_exclusion_none_bloquea_falso_exito_ante_modificacion_externa(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: si _bytes_bajo_exclusion devuelve None tras modificar el INI, debe fallar fail-closed."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    manual_rt_str = str(rig.manual_runtime).replace("\\", "/")
    clone_rt_str = str(rig.clone_root).replace("\\", "/")

    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), manual_rt_str),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    from sky_claw.local.frozen_runtime.mo2_ini_exclusion import ExclusionIni

    orig_cerrar = ExclusionIni.cerrar
    modificacion_inyectada = False

    def interceptar_cerrar(self_exclusion: ExclusionIni) -> None:
        nonlocal modificacion_inyectada
        orig_cerrar(self_exclusion)
        texto = ini_file.read_text(encoding="utf-8")
        if f"1\\workingDirectory={clone_rt_str}" in texto:
            texto_mutado = texto.replace(
                f"1\\workingDirectory={clone_rt_str}",
                f"1\\workingDirectory={manual_rt_str}",
            )
            ini_file.write_text(texto_mutado, encoding="utf-8")
            modificacion_inyectada = True

    monkeypatch.setattr(ExclusionIni, "cerrar", interceptar_cerrar)

    # Inyectamos fallo en la captura de evidencia bajo exclusión:
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.runtime_clone_binder._bytes_bajo_exclusion",
        lambda exc: None,
    )

    from sky_claw.local.frozen_runtime.journal import (
        JournalDisposition,
        cargar_journal_transicion,
    )
    from sky_claw.local.frozen_runtime.runtime_clone_binder import BinderVerificationError

    # El binder NO debe tener falso éxito. Debe fallar con BinderVerificationError.
    with pytest.raises(BinderVerificationError, match="evidencia insuficiente|divergente"):
        ejecutar_transicion_binder(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            target_clone_id=rig.clone_id,
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=rig.managed_source,
            config_path=cfg_path,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest=rig.approved_digest,
        )

    assert modificacion_inyectada is True

    # 1. El rollback conservador NO debe sobreescribir la modificación externa sin autoría
    ini_post_rollback = ini_file.read_text(encoding="utf-8")
    assert f"1\\workingDirectory={manual_rt_str}" in ini_post_rollback

    # 2. La evidencia de la modificación externa debe haber quedado preservada
    evidence_dir = rig.storage / "recovery_evidence"
    assert evidence_dir.exists()
    archivos_evidencia = list(evidence_dir.rglob("mo2_ini-*.bin"))
    assert len(archivos_evidencia) >= 1

    # 3. El journal debe permanecer en estado recuperable (no FINALIZED)
    snap_journal = cargar_journal_transicion(rig.storage)
    assert snap_journal.disposition is not JournalDisposition.NONE


def test_p1_bytes_bajo_exclusion_none_sin_modificacion_externa_falla_fail_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: una transición que mutó el INI falla fail-closed si faltó evidencia bajo exclusión."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    manual_rt_str = str(rig.manual_runtime).replace("\\", "/")

    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), manual_rt_str),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    # Inyectamos fallo de lectura bajo exclusión (sin modificación externa):
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.runtime_clone_binder._bytes_bajo_exclusion",
        lambda exc: None,
    )

    from sky_claw.local.frozen_runtime.journal import (
        JournalDisposition,
        cargar_journal_transicion,
    )
    from sky_claw.local.frozen_runtime.runtime_clone_binder import BinderVerificationError

    with pytest.raises(BinderVerificationError, match="evidencia insuficiente en modorganizer.ini"):
        ejecutar_transicion_binder(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            target_clone_id=rig.clone_id,
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=rig.managed_source,
            config_path=cfg_path,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest=rig.approved_digest,
        )

    # El journal no llegó a FINALIZED
    snap_journal = cargar_journal_transicion(rig.storage)
    assert snap_journal.disposition is not JournalDisposition.NONE

    # Evidencia archivada por degradación conservadora
    evidence_dir = rig.storage / "recovery_evidence"
    assert evidence_dir.exists()
    archivos_evidencia = list(evidence_dir.rglob("mo2_ini-*.bin"))
    assert len(archivos_evidencia) >= 1


def test_p1_noop_no_bloquea_si_ini_no_fue_modificado_incluso_sin_bytes_bajo_exclusion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: si el INI ya contenía los valores objetivo (NO-OP), no se bloquea espuriamente."""
    rig = RigStockManual(tmp_path, steam_activo=True)

    skse_src = rig.manual_runtime / "skse64_loader.exe"
    skse_src.write_bytes(b"MZ_SKSE")
    skse_dst = rig.clone_root / "skse64_loader.exe"
    skse_dst.write_bytes(b"MZ_SKSE")

    ini_file = rig.mo2_active_global / "modorganizer.ini"
    _escribir_ini_mo2(
        ini_file,
        game_path=rig.manual_runtime,
        base_directory=rig.mo2_base_data,
        ejecutables=[
            ("SKSE", str(skse_src).replace("\\", "/"), str(rig.manual_runtime).replace("\\", "/")),
        ],
    )

    clone_rec = cargar_runtime_clone_record(rig.storage, rig.clone_id)
    assert clone_rec is not None

    propuesta, consentimiento = _preparar_aprobacion(
        rig.storage,
        target_generation_id=rig.generation_id,
        target_clone_id=rig.clone_id,
        approval_provenance=procedencia_con_vinculo_mo2(
            mo2_dir=rig.mo2_active_global, outgoing_manual_runtime_root=rig.manual_runtime
        ),
        runtime_setup_id=clone_rec.verified_runtime_setup_id or "setup-skse-clean",
    )

    cfg_path = _crear_config_toml(
        rig.mo2_active_global / "config.toml",
        skyrim_path=str(rig.manual_runtime),
    )

    # 1. Primera transición: pasa de manual a Clone
    res1 = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.manual_runtime)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )
    assert res1.success is True

    # 2. Inyectamos fallo en _bytes_bajo_exclusion para la corrida NO-OP:
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.runtime_clone_binder._bytes_bajo_exclusion",
        lambda exc: None,
    )

    # La segunda ejecución es un NO-OP (no modifica el INI) y debe tener éxito sin bloquearse
    res2 = ejecutar_transicion_binder(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        target_clone_id=rig.clone_id,
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=rig.managed_source,
        config_path=cfg_path,
        environ={"SKYRIM_PATH": str(rig.clone_root)},
        probes=rig.probes_globales_activos(),
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest=rig.approved_digest,
    )

    assert res2.success is True
    assert res2.transition_id == "idempotent-noop"


# =============================================================================
# P7-A5-F01 / P7-A5-F02: QSettings size y escapes Qt en [customExecutables]
# =============================================================================


def _preparar_par_runtime_sintetico(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Crea un par outgoing/clone sintético con SKSE válido para planificar."""
    outgoing = tmp_path / "Skyrim_Manual_1.6.1170"
    clone = tmp_path / "FrozenClone_p7_01"
    tools = tmp_path / "Tools"
    for root in (outgoing, clone, tools):
        root.mkdir(parents=True, exist_ok=True)
        (root / "Data").mkdir(exist_ok=True)
    (outgoing / "skse64_loader.exe").write_bytes(b"MZ_SKSE")
    (clone / "skse64_loader.exe").write_bytes(b"MZ_SKSE")
    (tools / "SSEEdit.exe").write_bytes(b"MZ_SSEEDIT")
    return outgoing, clone, tools


def test_f01_size_10_con_diez_ejecutables_validos(tmp_path: pathlib.Path) -> None:
    """QSettings size=10 con 10 entradas válidas se lee sin anomalías y se planifica/preserva."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    extra_bins: list[pathlib.Path] = []
    for idx in range(2, 11):
        exe_file = tools / f"Herramienta{idx}.exe"
        exe_file.write_bytes(b"MZ_TOOL")
        extra_bins.append(exe_file)

    lineas = [
        "[General]",
        f"gamePath=@ByteArray({str(outgoing).replace(chr(92), chr(92) * 2)})",
        "",
        "[customExecutables]",
        "size=10",
        "1\\title=SKSE",
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe",
        f"1\\workingDirectory={outgoing.as_posix()}",
        "1\\arguments=",
        "1\\custom=true",
        "1\\toolbar=false",
        "1\\ownicon=true",
        "1\\hide=false",
    ]
    for offset, exe_file in enumerate(extra_bins, start=2):
        lineas.extend(
            [
                f"{offset}\\title=Herramienta {offset}",
                f"{offset}\\binary={exe_file.as_posix()}",
                f"{offset}\\workingDirectory={tools.as_posix()}",
                f"{offset}\\arguments=-autoload",
            ]
        )

    ini_path = tmp_path / "modorganizer.ini"
    ini_bytes = ("\r\n".join(lineas) + "\r\n").encode("utf-8")
    ini_path.write_bytes(ini_bytes)

    ejecutables = leer_ejecutables_de_mo2(ini_path)
    assert len(ejecutables) == 10
    assert [exe.index for exe in ejecutables] == list(range(1, 11))
    assert all(exe.anomalia is None for exe in ejecutables)

    plan = planificar_migracion_ejecutables(ejecutables, outgoing, clone)
    assert plan.bloqueado is False
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE
    assert all(entrada.clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE for entrada in plan.entradas[1:])

    salida_bytes = prever_bytes(ini_bytes, clone.as_posix(), plan_ejecutables=plan)
    assert b"size=10\r\n" in salida_bytes
    assert f"1\\binary={clone.as_posix()}/skse64_loader.exe".encode() in salida_bytes


def test_f01_size_cero_sin_entradas(tmp_path: pathlib.Path) -> None:
    """QSettings size=0 sin ejecutables devuelve tupla vacía sin anomalía sintética."""
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_bytes(b"[General]\r\ngamePath=C:/Dummy\r\n[customExecutables]\r\nsize=0\r\n")

    assert leer_ejecutables_de_mo2(ini_path) == ()


def test_f01_size_valido_con_entrada_incompleta_sigue_bloqueada(tmp_path: pathlib.Path) -> None:
    """size=2 válido no oculta una entrada incompleta sin binary."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[General]\n"
        f"gamePath={outgoing.as_posix()}\n"
        "[customExecutables]\n"
        "size=2\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "2\\title=Incompleta\n"
        "2\\arguments=-foo\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert len(exes) == 2
    assert exes[1].anomalia is not None
    assert "binary" in exes[1].anomalia
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)
    assert plan.bloqueado is True


@pytest.mark.parametrize(
    "lineas_size",
    [
        ["size=1", "size=1"],
        ["size=1", "size=2"],
        ["size=1", "SIZE=1"],
    ],
)
def test_f01_size_duplicado_bloqueado(tmp_path: pathlib.Path, lineas_size: list[str]) -> None:
    """Metadata size duplicada (idéntica, conflictiva o por casing) bloquea."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[customExecutables]\n"
        + "\n".join(lineas_size)
        + f"\n1\\title=SKSE\n1\\binary={outgoing.as_posix()}/skse64_loader.exe\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert exes and exes[0].anomalia is not None
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)
    assert plan.bloqueado is True


@pytest.mark.parametrize("valor_invalido", ["", "abc", "-1", "+2", "1.5", "²", "10 # extra"])
def test_f01_size_invalido_bloqueado(tmp_path: pathlib.Path, valor_invalido: str) -> None:
    """Valores no enteros ASCII positivos en size= se rechazan con anomalía."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[customExecutables]\n"
        f"size={valor_invalido}\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert exes and exes[0].anomalia is not None
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)
    assert plan.bloqueado is True


@pytest.mark.parametrize(
    ("size_val", "indices"),
    [
        (3, [1, 2]),
        (3, [1, 3]),
        (2, [1, 2, 3]),
        (0, [1]),
    ],
)
def test_f01_indices_inconsistentes_con_size_bloqueado(
    tmp_path: pathlib.Path,
    size_val: int,
    indices: list[int],
) -> None:
    """Desajuste entre size declarado e índices 1..size falla cerrado."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    lineas = ["[customExecutables]", f"size={size_val}"]
    for idx in indices:
        bin_path = outgoing / "skse64_loader.exe" if idx == 1 else tools / "SSEEdit.exe"
        lineas.extend([f"{idx}\\title=Tool{idx}", f"{idx}\\binary={bin_path.as_posix()}"])
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    exes = leer_ejecutables_de_mo2(ini_path)
    assert exes and exes[0].anomalia is not None
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)
    assert plan.bloqueado is True


def test_f01_claves_duplicadas_y_entradas_huerfanas_con_size(tmp_path: pathlib.Path) -> None:
    """Campos duplicados, entradas sin binary e índice 0 siguen bloqueando con size presente."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)

    # Campo duplicado bajo size=1
    ini_dup = tmp_path / "dup.ini"
    ini_dup.write_text(
        "[customExecutables]\n"
        "size=1\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n",
        encoding="utf-8",
    )
    exes_dup = leer_ejecutables_de_mo2(ini_dup)
    assert exes_dup[0].anomalia is not None
    assert planificar_migracion_ejecutables(exes_dup, outgoing, clone).bloqueado is True

    # Entrada huérfana sin binary + índice 0 inválido
    ini_huerf = tmp_path / "huerf.ini"
    ini_huerf.write_text(
        "[customExecutables]\n"
        "size=1\n"
        f"0\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "1\\title=Huerfana\n"
        "1\\arguments=-foo\n",
        encoding="utf-8",
    )
    exes_huerf = leer_ejecutables_de_mo2(ini_huerf)
    assert any(e.anomalia is not None for e in exes_huerf)
    assert planificar_migracion_ejecutables(exes_huerf, outgoing, clone).bloqueado is True


@pytest.mark.parametrize("clave_desconocida", ["foo=bar", "count=1", "SIZE=1", "Size=1", "customFlag=true"])
def test_f01_claves_desconocidas_en_custom_executables_bloqueadas(
    tmp_path: pathlib.Path,
    clave_desconocida: str,
) -> None:
    """Solo se admite la metadata exacta size=<entero>; cualquier otra clave suelta bloquea."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[customExecutables]\n"
        "size=1\n"
        f"{clave_desconocida}\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert exes and exes[0].anomalia is not None
    assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is True


def test_f01_seccion_duplicada_con_size_bloqueada(tmp_path: pathlib.Path) -> None:
    """Dos secciones [customExecutables] con size siguen fallando cerrado en lector y escritor."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    contenido = (
        "[General]\r\n"
        f"gamePath={outgoing.as_posix()}\r\n"
        "[customExecutables]\r\n"
        "size=1\r\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\r\n"
        "[customExecutables]\r\n"
        "size=1\r\n"
        f"2\\binary={tools.as_posix()}/SSEEdit.exe\r\n"
    ).encode()
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_bytes(contenido)

    exes = leer_ejecutables_de_mo2(ini_path)
    assert exes and exes[0].anomalia is not None
    assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is True
    with pytest.raises(FormatoIniNoAdmitidoError, match="[Ss]ección.*duplicada"):
        prever_bytes(contenido, clone.as_posix())


def test_f01_archivo_sin_size_y_entradas_validas_permitido(tmp_path: pathlib.Path) -> None:
    """INIs sintéticos heredados sin size= siguen siendo válidos si todas las entradas son íntegras."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[customExecutables]\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "2\\title=SSEEdit\n"
        f"2\\binary={tools.as_posix()}/SSEEdit.exe\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert len(exes) == 2
    assert all(e.anomalia is None for e in exes)
    assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is False


@pytest.mark.parametrize(
    ("argumentos", "ruta_str", "esperado"),
    [
        # Referencias verdaderas con escapes Qt reales de MO2 2.5.2 y variantes
        (
            r"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"G:\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\\" -autoload"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"\"G:\\Modding\\Skyrim_Runtime_1.6.1170\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-p:\"G:\\Modding\\Skyrim_Runtime_1.6.1170\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            '-D:"G:/Modding/Skyrim_Runtime_1.6.1170/Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"G:\tools\new\Skyrim_Runtime_1.6.1170\Data"',
            r"G:\tools\new\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"G:\\Modding\\Skyrim Runtime 1.6.1170\\Data\\\"",
            r"G:\Modding\Skyrim Runtime 1.6.1170",
            True,
        ),
        (
            r"-D:\"g:\\modding\\SKYRIM_RUNTIME_1.6.1170\\data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170\\\\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        # Fail-closed ante escapes malformados / truncados / NUL
        (
            r"@ByteArray(G:\\Other\\truncado",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            "G:\\\\Other\x00Hidden",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"G:\\Other\\" + "\\",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"G:\\Other\xZZ\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"G:\\Other\\xEdit\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        # No falsopositivos en prefijos parciales ni rutas ajenas
        (
            r"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.1170_Backup\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (
            r"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.11700\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (
            r"-D:\"G:\\Modding\\OtraCarpeta\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (
            r'-D:"C:\Tools\xEdit\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (
            "-sse -autoload",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
    ],
)
def test_f02_contiene_referencia_a_ruta_variantes_qt_y_falsos_positivos(
    argumentos: str,
    ruta_str: str,
    esperado: bool,
) -> None:
    """_contiene_referencia_a_ruta detecta escapes Qt (\\\\ y \\\") sin falsos positivos."""
    assert _contiene_referencia_a_ruta(argumentos, pathlib.PureWindowsPath(ruta_str)) is esperado


def test_f02_planificar_bloquea_sseedit_con_escapes_qt_reales(tmp_path: pathlib.Path) -> None:
    """SSEEdit externo con arguments en formato Qt QSettings hacia outgoing bloquea el plan; con sufijo distinto pasa."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    outgoing_qt = str(outgoing).replace("\\", "\\\\")

    ini_bloqueado = tmp_path / "mo2_bloqueado.ini"
    ini_bloqueado.write_text(
        "[customExecutables]\n"
        "size=2\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "2\\title=SSEEdit\n"
        f"2\\binary={tools.as_posix()}/SSEEdit.exe\n"
        f"2\\workingDirectory={tools.as_posix()}\n"
        f'2\\arguments=-D:\\"{outgoing_qt}\\\\Data\\\\\\"\n',
        encoding="utf-8",
    )

    exes_bloqueado = leer_ejecutables_de_mo2(ini_bloqueado)
    assert all(e.anomalia is None for e in exes_bloqueado)
    plan_bloqueado = planificar_migracion_ejecutables(exes_bloqueado, outgoing, clone)
    assert plan_bloqueado.bloqueado is True
    assert plan_bloqueado.entradas[1].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA

    ini_limpio = tmp_path / "mo2_limpio.ini"
    ini_limpio.write_text(
        "[customExecutables]\n"
        "size=2\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "2\\title=SSEEdit\n"
        f"2\\binary={tools.as_posix()}/SSEEdit.exe\n"
        f"2\\workingDirectory={tools.as_posix()}\n"
        f'2\\arguments=-D:\\"{outgoing_qt}_Backup\\\\Data\\\\\\"\n',
        encoding="utf-8",
    )
    exes_limpio = leer_ejecutables_de_mo2(ini_limpio)
    plan_limpio = planificar_migracion_ejecutables(exes_limpio, outgoing, clone)
    assert plan_limpio.bloqueado is False
    assert plan_limpio.entradas[1].clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE


def test_f02_argumentos_dependientes_de_steam_ambos_hermanos_alineados() -> None:
    """Los hermanos en mo2_executables_plan y binder_preflight comparten detección Qt y sin falsos positivos."""
    ms_root = pathlib.PureWindowsPath(r"D:\CustomGames\SkyrimSE")
    arg_qt = r"-D:\"D:\\CustomGames\\SkyrimSE\\Data\\\""
    arg_prefijo = r"-D:\"D:\\CustomGames\\SkyrimSE_Copy\\Data\\\""
    arg_malformado = r"@ByteArray(D:\\CustomGames\\truncado"

    for fn in (_argumentos_dependientes_de_steam, _preflight_argumentos_dependientes_de_steam):
        assert fn(arg_qt, ms_root) is True
        assert fn(arg_prefijo, ms_root) is False
        assert fn(arg_malformado, ms_root) is True


def test_f02_planificar_escapes_malformados_en_arguments_clasificados_como_ambigua(tmp_path: pathlib.Path) -> None:
    """Un escape malformado en arguments sin mención a Steam se clasifica como AMBIGUA_O_BLOQUEADA, no DEPENDIENTE_DE_STEAM."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    ms_root = tmp_path / "SteamLibrary" / "steamapps" / "common" / "Skyrim Special Edition"
    ms_root.mkdir(parents=True)

    exes = (
        Mo2Executable(
            index=1,
            title="SSEEdit",
            binary=f"{tools.as_posix()}/SSEEdit.exe",
            working_directory=tools.as_posix(),
            arguments=r"@ByteArray(C:\\Tools\\truncado",
        ),
    )
    plan = planificar_migracion_ejecutables(exes, outgoing, clone, managed_source_root=ms_root)
    assert plan.bloqueado is True
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA


# =============================================================================
# P7-A5-F03: Acotación O(k) de QSettings size e índices + auditoría F02 UNC
# =============================================================================


def test_f03_04_size_desproporcionado_no_asigna_rango_en_memoria(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F03-04: size=1000000000 se rechaza como anomalía sin invocar range proporcional a size."""
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text("[customExecutables]\nsize=1000000000\n", encoding="utf-8")

    rango_original = range

    def _range_vigilado(*args: int) -> range:
        stop = args[1] if len(args) >= 2 else args[0]
        if stop > 1000:
            raise AssertionError(f"Construcción proporcional a size detectada en mo2_ini: range(..., stop={stop})")
        return rango_original(*args)

    monkeypatch.setattr(mo2_ini_mod, "range", _range_vigilado, raising=False)

    tracemalloc.start()
    try:
        exes = leer_ejecutables_de_mo2(ini_path)
        _actual, pico_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(exes) == 1
    assert exes[0].anomalia is not None
    assert "1000000000" in exes[0].anomalia
    assert pico_bytes < 1_000_000


def test_f03_05_size_decimal_miles_de_digitos_no_lanza_y_marca_anomalia(tmp_path: pathlib.Path) -> None:
    """F03-05: Un decimal de miles de caracteres en size= nunca lanza ValueError y reporta anomalía acotada."""
    size_gigante = "9" * 6000
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(f"[customExecutables]\nsize={size_gigante}\n", encoding="utf-8")

    exes = leer_ejecutables_de_mo2(ini_path)
    assert len(exes) == 1
    assert exes[0].anomalia is not None
    assert len(exes[0].anomalia) < 512


@pytest.mark.parametrize(
    ("size_val", "indices", "esperado_valido"),
    [
        ("0", [], True),  # F03-01
        ("1", [1], True),  # F03-02
        ("10", [1, 2], False),  # F03-06
        ("3", [1, 3, 4], False),  # F03-07
        ("0", [1], False),  # F03-08
        ("-1", [1], False),  # F03-09
        ("abc", [1], False),  # F03-10
        ("01", [1], False),  # cero a la izquierda no canónico
        ("0" * 5000, [], False),  # miles de ceros no canónicos
    ],
)
def test_f03_matriz_contrato_size_e_indices(
    tmp_path: pathlib.Path,
    size_val: str,
    indices: list[int],
    esperado_valido: bool,
) -> None:
    """Verifica la matriz F03-01..F03-10 de metadata size e integridad de índices."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    lineas = ["[customExecutables]", f"size={size_val}"]
    for idx in indices:
        bin_path = outgoing / "skse64_loader.exe" if idx == 1 else tools / "SSEEdit.exe"
        lineas.extend([f"{idx}\\title=Tool{idx}", f"{idx}\\binary={bin_path.as_posix()}"])
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    exes = leer_ejecutables_de_mo2(ini_path)
    if esperado_valido:
        assert len(exes) == len(indices)
        assert all(exe.anomalia is None for exe in exes)
        assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is False
    else:
        assert exes and any(exe.anomalia is not None for exe in exes)
        assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is True


@pytest.mark.parametrize(
    "prefijo_invalido",
    [
        "²",  # dígito Unicode no ASCII: int("²") lanzaba ValueError
        "١",  # dígito árabe-índico no ASCII
        "01",  # cero a la izquierda ambiguo
        "9" * 6000,  # excede sys.get_int_max_str_digits() (4300)
        "9999999999",  # excede INT32_MAX de Qt QSettings::setArrayIndex(int)
        "2147483648",  # INT32_MAX + 1
    ],
)
def test_f03_indice_individual_malformado_o_desproporcionado_nunca_lanza(
    tmp_path: pathlib.Path,
    prefijo_invalido: str,
) -> None:
    """Un índice no ASCII, con cero a la izquierda o fuera de rango Qt nunca lanza y falla cerrado."""
    outgoing, clone, _tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[customExecutables]\n"
        f"{prefijo_invalido}\\title=Tool\n"
        f"{prefijo_invalido}\\binary={outgoing.as_posix()}/skse64_loader.exe\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert len(exes) == 1
    assert exes[0].anomalia is not None
    assert len(exes[0].anomalia) < 512
    assert planificar_migracion_ejecutables(exes, outgoing, clone).bloqueado is True


@pytest.mark.parametrize(
    ("argumentos", "ruta_unc", "esperado"),
    [
        (
            r"-D:\"\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\\"",
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"\\Servidor\Modding\Skyrim_Runtime_1.6.1170\Data"',
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170_Backup\\Data\\\"",
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (
            r"-D:\"\x47:\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r"-D:\"\107:\\Modding\\Skyrim_Runtime_1.6.1170\\Data\\\"",
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
    ],
)
def test_f02_auditoria_unc_y_escapes_hex_octal_en_arguments(
    argumentos: str,
    ruta_unc: str,
    esperado: bool,
) -> None:
    """Auditoría F02: rutas UNC escapadas en Qt y escapes hex/octal dentro de arguments."""
    assert _contiene_referencia_a_ruta(argumentos, pathlib.PureWindowsPath(ruta_unc)) is esperado


# =============================================================================
# CR-01 / CR-02: Orden independiente de escapes Qt y quoting Windows en Capa 2
# =============================================================================


@pytest.mark.parametrize(
    ("argumentos", "esperado_malformado", "descripcion"),
    [
        # 1. Escape inválido antes del primer escape Qt válido -> fail-closed
        (r"\xZZ C:\\Tools", True, "invalido_xzz_antes_de_barra_doble_qt"),
        (r"-cfg \xZZ -p \"D:\\Tools\"", True, "invalido_xzz_antes_de_comilla_y_barra_qt"),
        (r"-flag \x C:\\Tools", True, "invalido_x_con_espacio_antes_de_qt"),
        (r"\xZZ \xYY C:\\Tools", True, "multiples_invalidos_antes_de_qt"),
        # 2. Escape Qt válido antes del escape inválido -> fail-closed
        (r"C:\\Tools \xZZ", True, "qt_antes_de_xzz"),
        (r"-p \"D:\\Tools\" -cfg \xZZ", True, "comilla_qt_antes_de_xzz"),
        (r"C:\\Tools \xZZ \x", True, "qt_y_multiples_invalidos"),
        (r"-D:\"C:\xZZ\"", True, "xzz_dentro_de_comillas_qt"),
        # 3. Escape inválido aislado sin evidencia de serialización Qt -> literal por contrato
        (r"\xZZ", False, "xzz_aislado_sin_escapes_qt"),
        # 4. Ruta Windows literal con \x como parte del nombre (sin escapes Qt)
        (r"C:\xZZ", False, "ruta_literal_xzz"),
        (r"-cfg C:\xZZ\preset.ini", False, "flag_con_ruta_literal_xzz"),
        (r'-D:"G:\Modding\xTools\Data"', False, "ruta_literal_con_barra_x_y_sin_qt"),
        # 5. Escapes Qt válidos
        (r"-D:\"D:\\Tools\\SSEEdit\\Data\"", False, "escapes_qt_validos_barra_y_comillas"),
        (r"-D:\"C:\\xZZ\"", False, "ruta_xzz_correctamente_escapada_en_qt"),
        (r"-p \"D:\\Tools\\x64\\bin\"", False, "x64_correctamente_escapado_en_qt"),
        # 6. NUL embebido (literal, \0, \x00, \000) -> siempre fail-closed
        ("G:\\\\Other\x00Hidden", True, "nul_crudo_en_cadena"),
        (r"C:\Tools\0", True, "escape_octal_nul_en_ruta"),
        (r"\x00 C:\\Tools", True, "escape_hex_nul_antes_de_qt"),
        (r"C:\\Tools \x10000", True, "overflow_16_bits_a_nul_fail_closed"),
        # 7. Barra final suelta: literal vs serialización Qt
        ("C:\\Tools\\", False, "barra_final_en_ruta_literal_sin_qt"),
        ("-o D:\\Tools\\", False, "argumento_literal_terminado_en_barra"),
        ("C:\\Tools\\\\\\", True, "barra_final_suelta_impar_con_qt_previo"),
        ('-D:\\"C:\\\\Tools\\" \\', True, "barra_final_suelta_tras_escapes_qt"),
        ("C:\\\\Tools\\\\", False, "barra_final_escapada_par_en_qt"),
    ],
)
def test_cr01_desescapar_qt_argumentos_independiente_del_orden(
    argumentos: str,
    esperado_malformado: bool,
    descripcion: str,
) -> None:
    """CR-01: la detección de escapes Qt inválidos o truncados es independiente del orden."""
    _decodificado, malformado = _desescapar_qt_argumentos(argumentos)
    assert malformado is esperado_malformado, descripcion
    assert _tiene_escape_argumento_malformado(argumentos) is esperado_malformado, descripcion


@pytest.mark.parametrize(
    ("argumentos", "esperado_preservable"),
    [
        # 1. Comillas simples de agrupación con comillas dobles internas
        (r"-msg 'it\"s ok'", True),
        (r"-filter='tag\"'", True),
        (r"-name 'John \"The\" Tester'", True),
        # 2. Comillas dobles equilibradas
        (r"-D:\"D:\\Tools\\SSEEdit\\Data\"", True),
        # 3. Comillas literales escapadas (caso CodeRabbit PRRT_kwDOR1JjU863q1Qp)
        (r"-label \"a\\\"b\"", True),
        (r"-title \"Skyrim \\\"AE\\\" Edition\"", True),
        # 4. Barra invertida inmediatamente anterior a comilla (estilo xEdit / Delphi)
        (r"-D:\"D:\\Tools\\SSEEdit\\Data\\\"", True),
        # 5. Secuencias con 2 y 3 barras anteriores a comillas (regla Windows)
        (r"-D:\"D:\\Tools\\\\\"", True),
        (r"-label \"a\\\\\\\"b\"", True),
        # 6. Rutas Windows externas con espacios
        (r"-D:\"C:\\Program Files\\Mod Tools\\SSEEdit\\Data\"", True),
        # 7. Argumentos externos literales sin escapes Qt con comillas literales
        (r'-label "a\"b" -autoload', True),
        # 8. Casos realmente malformados (Capa 1 Qt corrupta) -> bloqueados fail-closed
        (r"@ByteArray(C:\\Tools\\truncado", False),
        ("C:\\Tools\x00Hidden", False),
        (r"\xZZ C:\\Tools", False),
        (r"-label \"a\\\"b\" \xZZ", False),
        ('-D:\\"D:\\\\Tools\\\\" \\', False),
    ],
)
def test_cr02_comillas_literales_y_quoting_windows_integracion_completa(
    tmp_path: pathlib.Path,
    argumentos: str,
    esperado_preservable: bool,
) -> None:
    """CR-02: comillas literales válidas preservan EXTERNA_PRESERVABLE; escapes Qt inválidos bloquean."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_path = tmp_path / "modorganizer.ini"
    ini_path.write_text(
        "[General]\n"
        f"gamePath={outgoing.as_posix()}\n"
        "[customExecutables]\n"
        "size=2\n"
        "1\\title=SKSE\n"
        f"1\\binary={outgoing.as_posix()}/skse64_loader.exe\n"
        "2\\title=SSEEdit\n"
        f"2\\binary={tools.as_posix()}/SSEEdit.exe\n"
        f"2\\workingDirectory={tools.as_posix()}\n"
        f"2\\arguments={argumentos}\n",
        encoding="utf-8",
    )

    exes = leer_ejecutables_de_mo2(ini_path)
    assert all(exe.anomalia is None for exe in exes)
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)

    if esperado_preservable:
        assert plan.bloqueado is False
        assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE
        assert plan.entradas[1].clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE
        assert plan.entradas[1].arguments_original == argumentos
    else:
        assert plan.bloqueado is True
        assert plan.entradas[1].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA


def test_cr01_cr02_integracion_runtime_saliente_steam_y_prefijos_con_comillas_literales(
    tmp_path: pathlib.Path,
) -> None:
    """CR-01/CR-02: eliminar conteo de comillas NO debilita detección de outgoing, Steam o prefijos."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    outgoing_qt = str(outgoing).replace("\\", "\\\\")
    ms_root = tmp_path / "SteamLibrary" / "steamapps" / "common" / "Skyrim Special Edition"
    ms_root.mkdir(parents=True)
    ms_qt = str(ms_root).replace("\\", "\\\\")

    # 1. Referencia al runtime saliente combinada con comillas literales impares -> BLOQUEADA
    exe_outgoing_comilla = Mo2Executable(
        index=1,
        title="SSEEdit",
        binary=f"{tools.as_posix()}/SSEEdit.exe",
        working_directory=tools.as_posix(),
        arguments=f'-label \\"a\\\\\\"b\\" -D:\\"{outgoing_qt}\\\\Data\\"',
    )
    plan_out = planificar_migracion_ejecutables((exe_outgoing_comilla,), outgoing, clone, managed_source_root=ms_root)
    assert plan_out.bloqueado is True
    assert plan_out.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA

    # 2. Comilla sin cerrar apuntando al runtime saliente -> BLOQUEADA
    exe_outgoing_sin_cerrar = Mo2Executable(
        index=1,
        title="SSEEdit",
        binary=f"{tools.as_posix()}/SSEEdit.exe",
        working_directory=tools.as_posix(),
        arguments=f'-D:\\"{outgoing_qt}\\\\Data',
    )
    plan_unclosed = planificar_migracion_ejecutables(
        (exe_outgoing_sin_cerrar,), outgoing, clone, managed_source_root=ms_root
    )
    assert plan_unclosed.bloqueado is True
    assert plan_unclosed.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA

    # 3. Prefijo similar + comillas literales impares -> NO bloqueado por falso positivo
    exe_prefijo_similar = Mo2Executable(
        index=1,
        title="SSEEdit",
        binary=f"{tools.as_posix()}/SSEEdit.exe",
        working_directory=tools.as_posix(),
        arguments=f'-label \\"a\\\\\\"b\\" -D:\\"{outgoing_qt}_Backup\\\\Data\\\\\\"',
    )
    plan_sim = planificar_migracion_ejecutables((exe_prefijo_similar,), outgoing, clone, managed_source_root=ms_root)
    assert plan_sim.bloqueado is False
    assert plan_sim.entradas[0].clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE

    # 4. Steam / Managed Source con comillas literales impares -> DEPENDIENTE_DE_STEAM y alineado con preflight
    args_steam = f'-label \\"a\\\\\\"b\\" -D:\\"{ms_qt}\\\\Data\\"'
    exe_steam = Mo2Executable(
        index=1,
        title="ToolSteam",
        binary=f"{tools.as_posix()}/SSEEdit.exe",
        working_directory=tools.as_posix(),
        arguments=args_steam,
    )
    plan_steam = planificar_migracion_ejecutables((exe_steam,), outgoing, clone, managed_source_root=ms_root)
    assert plan_steam.bloqueado is True
    assert plan_steam.entradas[0].clasificacion is ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM
    assert _argumentos_dependientes_de_steam(args_steam, ms_root) is True
    assert _preflight_argumentos_dependientes_de_steam(args_steam, ms_root) is True
    assert _ejecutables_dependientes_de_steam((exe_steam,), ms_root) != []


def test_cr01_integracion_ini_orden_escapes_end_to_end(tmp_path: pathlib.Path) -> None:
    """CR-01 end-to-end: INI con escape inválido antes de escape Qt bloquea la planificación."""
    outgoing, clone, tools = _preparar_par_runtime_sintetico(tmp_path)
    ini_invalido = tmp_path / "mo2_cr01.ini"
    ini_invalido.write_text(
        "[customExecutables]\n"
        "size=1\n"
        "1\\title=SSEEdit\n"
        f"1\\binary={tools.as_posix()}/SSEEdit.exe\n"
        f"1\\workingDirectory={tools.as_posix()}\n"
        "1\\arguments=\\xZZ C:\\\\Tools\n",
        encoding="utf-8",
    )
    exes = leer_ejecutables_de_mo2(ini_invalido)
    assert exes[0].anomalia is None
    plan = planificar_migracion_ejecutables(exes, outgoing, clone)
    assert plan.bloqueado is True
    assert plan.entradas[0].clasificacion is ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA

    ini_literal = tmp_path / "mo2_literal.ini"
    ini_literal.write_text(
        "[customExecutables]\n"
        "size=1\n"
        "1\\title=SSEEdit\n"
        f"1\\binary={tools.as_posix()}/SSEEdit.exe\n"
        f"1\\workingDirectory={tools.as_posix()}\n"
        "1\\arguments=-cfg C:\\xZZ\\tool.ini\n",
        encoding="utf-8",
    )
    exes_lit = leer_ejecutables_de_mo2(ini_literal)
    plan_lit = planificar_migracion_ejecutables(exes_lit, outgoing, clone)
    assert plan_lit.bloqueado is False
    assert plan_lit.entradas[0].clasificacion is ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE


@pytest.mark.parametrize(
    ("argumentos", "ruta_objetivo", "esperado"),
    [
        # Qt 5.15 QSettingsPrivate::iniUnescapedStringList consume dígitos hex/octales como char16_t (16 bits, wrap)
        (
            r'-D:"\x10047:\\Modding\\Skyrim_Runtime_1.6.1170\\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'-D:"\00107:\\Modding\\Skyrim_Runtime_1.6.1170\\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        # UNC en @ByteArray y comillas anidadas
        (
            r"@ByteArray(\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170\\Data)",
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'@ByteArray(-D:"\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170\\Data")',
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'"\"\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170\\Data\""',
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (
            r'"\"\\\\Servidor\\Modding\\Skyrim_Runtime_1.6.1170_Backup\\Data\""',
            r"\\Servidor\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
    ],
)
def test_auditoria_qt_escape_wrap_16bit_y_unc_bytearray_comillas_anidadas(
    argumentos: str,
    ruta_objetivo: str,
    esperado: bool,
) -> None:
    """Verifica equivalencia Qt 5.15 en escapes >4 hex / >3 octales y UNC dentro de @ByteArray o comillas."""
    ruta_win = pathlib.PureWindowsPath(ruta_objetivo)
    assert _contiene_referencia_a_ruta(argumentos, ruta_win) is esperado
    variantes, _malformado = _analizar_variantes_argumentos(argumentos)
    assert len(variantes) >= 1


# =========================================================================
# CR-03: Descarte de escapes desconocidos y \x sin hex según Qt 5.15
# =========================================================================


@pytest.mark.parametrize(
    ("entrada", "decodificado_esperado", "malformado_esperado"),
    [
        # Hallazgo CodeRabbit CR-03: Qt 5.15 iniUnescapedStringList descarta backslash + char desconocido
        (r"Steam\qapps", "Steamapps", False),
        (r"G:\\Games\\Steam\qapps", r"G:\Games\Steamapps", False),
        (r"-p Steam\qapps\\common", r"-p Steamapps\common", False),
        # \x sin dígito hexadecimal: en Qt 5.15 StHexEscape descarta '\x' y reanuda en 'p'
        (r"steama\xpps", "steamapps", False),
        # Tanto \q como \p son escapes desconocidos y Qt los descarta ambos
        (r"-cfg C:\qTools\preset.ini", r"-cfg C:Toolsreset.ini", False),
        (
            r'-D:"G:\\Modding\\Skyrim_\qRuntime_1.6.1170"',
            r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170"',
            False,
        ),
        (r"-o C:\new\test\run", "-o C:\new\test\run", False),
        # Evidencia explícita de escape Qt (\\ o \") + \x sin hex -> malformado fail-closed
        (r"G:\\steama\xpps", r"G:\steamapps", True),
        (r"\"steama\xpps\"", '"steamapps"', True),
        (r"G:\\Steam\qapps", r"G:\Steamapps", False),
    ],
)
def test_cr03_desescapar_qt_argumentos_coincide_con_qt515(
    entrada: str,
    decodificado_esperado: str,
    malformado_esperado: bool,
) -> None:
    """Verifica que _desescapar_qt_argumentos descarte escapes desconocidos y \\x sin hex como Qt 5.15."""
    decodificado, malformado = _desescapar_qt_argumentos(entrada)
    assert decodificado == decodificado_esperado
    assert malformado is malformado_esperado


@pytest.mark.parametrize(
    ("argumentos", "esperado_steam"),
    [
        (r"Steam\qapps", True),
        (r"G:\\Games\\Steam\qapps", True),
        (r"-p Steam\qapps\\common", True),
        (r"steama\xpps", True),
        (r"@ByteArray(steama\xpps)", True),
        (r"-cfg C:\qTools\preset.ini", False),
        (r"-o C:\new\test\run", False),
    ],
)
def test_cr03_deteccion_steam_con_escapes_desconocidos_y_no_falsos_positivos(
    argumentos: str,
    esperado_steam: bool,
) -> None:
    """Confirma que evasiones \\q / \\x se detectan como Steam y rutas Windows benignas no dan falso positivo."""
    assert _argumentos_dependientes_de_steam(argumentos, None) is esperado_steam
    if argumentos == r"@ByteArray(steama\xpps)":
        variantes, malformado = _analizar_variantes_argumentos(argumentos)
        assert malformado is False
        assert variantes == ("steamapps",)


def test_cr03_contiene_referencia_a_ruta_con_escape_desconocido_y_benigno() -> None:
    """Valida detección de bypass \\q hacia runtime base sin bloquear herramientas externas literales."""
    base_runtime = pathlib.PureWindowsPath(r"G:\Modding\Skyrim_Runtime_1.6.1170")
    assert _contiene_referencia_a_ruta(r'-D:"G:\\Modding\\Skyrim_\qRuntime_1.6.1170"', base_runtime) is True
    assert _contiene_referencia_a_ruta(r'-D:"G:/Modding/Skyrim_\qRuntime_1.6.1170"', base_runtime) is True
    assert _contiene_referencia_a_ruta(r"-cfg C:\qTools\preset.ini", base_runtime) is False
    assert _contiene_referencia_a_ruta(r"-o C:\new\test\run", base_runtime) is False


def test_cr03_planificacion_clasificacion_y_no_falsos_bloqueos(tmp_path: pathlib.Path) -> None:
    """Verifica clasificación completa en planificar_migracion_ejecutables para casos CR-03."""
    outgoing = tmp_path / "Skyrim_Runtime_1.6.1170"
    clone = tmp_path / "Clone"
    tools = tmp_path / "Tools"
    outgoing.mkdir()
    clone.mkdir()
    tools.mkdir()
    exe_ext = tools / "xEdit.exe"
    exe_ext.write_bytes(b"MZ")

    # Casos benignos con rutas literales Windows que contienen '\q', '\n', '\t', '\r'
    plan_benigno = planificar_migracion_ejecutables(
        (
            Mo2Executable(1, "xEdit qTools", str(exe_ext), str(tools), r"-cfg C:\qTools\preset.ini"),
            Mo2Executable(2, "xEdit LiteralEscapes", str(exe_ext), str(tools), r"-o C:\new\test\run"),
        ),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan_benigno.bloqueado is False
    assert [e.clasificacion for e in plan_benigno.entradas] == [
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
    ]

    # Casos de evasión Steam o referencia residual ofuscada con \q / \x
    outgoing_esc = (
        str(outgoing)
        .replace("\\", "\\\\")
        .replace(
            "Skyrim_Runtime_1.6.1170",
            r"Skyrim_\qRuntime_1.6.1170",
        )
    )
    plan_bloqueado = planificar_migracion_ejecutables(
        (
            Mo2Executable(1, "Evasion Steam q", str(exe_ext), str(tools), r"-p Steam\qapps\\common"),
            Mo2Executable(2, "Evasion Steam x", str(exe_ext), str(tools), r"steama\xpps"),
            Mo2Executable(3, "Evasion Runtime q", str(exe_ext), str(tools), f'-D:"{outgoing_esc}"'),
        ),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
    )
    assert plan_bloqueado.bloqueado is True
    assert [e.clasificacion for e in plan_bloqueado.entradas] == [
        ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
        ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
        ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
    ]


# =========================================================================
# CR-04: Límites entre comillas con espacios internos en rutas Windows
# =========================================================================


@pytest.mark.parametrize(
    ("argumentos", "ruta_objetivo", "esperado"),
    [
        # Hallazgo CodeRabbit CR-04: ruta hermana entrecomillada con espacio interno -> False
        (r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170 Backup\Data"', r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (
            r'"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.1170 Backup\\Data\""',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (r'-D:"G:/Modding/Skyrim_Runtime_1.6.1170 Old/Data"', r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (r"-D:'G:\Modding\Skyrim_Runtime_1.6.1170 Archive\Data'", r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (r'-p "G:\Modding\Skyrim_Runtime_1.6.1170 Copy 2\Data"', r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (
            r'-p "\\server\share\Skyrim_Runtime_1.6.1170 Backup\Data"',
            r"\\server\share\Skyrim_Runtime_1.6.1170",
            False,
        ),
        (r'-p "D:\G:\Modding\Skyrim_Runtime_1.6.1170"', r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170_Backup\Data"', r"G:\Modding\Skyrim_Runtime_1.6.1170", False),
        (
            r'-D:"G:\Modding\Skyrim Runtime 1.6.1170 Backup\Data"',
            r"G:\Modding\Skyrim Runtime 1.6.1170",
            False,
        ),
        # Verdaderos positivos que DEBEN seguir detectándose
        (r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170"', r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
        (r'-D:"G:\Modding\Skyrim_Runtime_1.6.1170\Data"', r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
        (r"-p G:\Modding\Skyrim_Runtime_1.6.1170 -autoload", r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
        (r'-p "G:\Modding\Skyrim_Runtime_1.6.1170" "C:\Out"', r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
        # Espacios finales dentro de comillas: Win32 recorta espacios finales / entrada sospechosa
        (r'-p "G:\Modding\Skyrim_Runtime_1.6.1170   "', r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
        # Segunda coincidencia real tras una primera hermana inocua con espacio
        (
            r'-backup "G:\Modding\Skyrim_Runtime_1.6.1170 Backup\Data" -D:"G:\Modding\Skyrim_Runtime_1.6.1170\Data"',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        # Comilla previa sin cerrar NO debe silenciar referencias reales posteriores (fail-closed)
        (
            r'-title "Foo -p G:\Modding\Skyrim_Runtime_1.6.1170 -autoload',
            r"G:\Modding\Skyrim_Runtime_1.6.1170",
            True,
        ),
        (r'-D:"G:\Modding\Skyrim Runtime 1.6.1170\Data"', r"G:\Modding\Skyrim Runtime 1.6.1170", True),
        (r'"-D:\"G:\\Modding\\Skyrim_Runtime_1.6.1170\""', r"G:\Modding\Skyrim_Runtime_1.6.1170", True),
    ],
)
def test_cr04_limites_entre_comillas_con_espacios_internos(
    argumentos: str,
    ruta_objetivo: str,
    esperado: bool,
) -> None:
    """Verifica que espacios internos entre comillas no generen falsos positivos sin crear bypasses."""
    ruta_win = pathlib.PureWindowsPath(ruta_objetivo)
    assert _contiene_referencia_a_ruta(argumentos, ruta_win) is esperado


def test_cr04_planificacion_sseedit_backup_en_argumentos_es_externa_preservable(
    tmp_path: pathlib.Path,
) -> None:
    """Confirma que SSEEdit con -D hacia '<runtime> Backup\\Data' se preserva y no se bloquea."""
    outgoing = tmp_path / "Skyrim_Runtime_1.6.1170"
    clone = tmp_path / "Clone"
    managed = tmp_path / "GoldenSource"
    tools = tmp_path / "Tools"
    outgoing.mkdir()
    clone.mkdir()
    managed.mkdir()
    tools.mkdir()
    sseedit = tools / "SSEEdit.exe"
    sseedit.write_bytes(b"MZ")

    backup_data = tmp_path / "Skyrim_Runtime_1.6.1170 Backup" / "Data"
    managed_backup = tmp_path / "GoldenSource Backup" / "Data"

    plan_preservable = planificar_migracion_ejecutables(
        (
            Mo2Executable(1, "SSEEdit Backup", str(sseedit), str(tools), f'-D:"{backup_data}"'),
            Mo2Executable(2, "SSEEdit ManagedBackup", str(sseedit), str(tools), f'-D:"{managed_backup}"'),
        ),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=managed,
    )
    assert plan_preservable.bloqueado is False
    assert [e.clasificacion for e in plan_preservable.entradas] == [
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
    ]

    plan_real_ref = planificar_migracion_ejecutables(
        (
            Mo2Executable(1, "SSEEdit Activo", str(sseedit), str(tools), f'-D:"{outgoing / "Data"}"'),
            Mo2Executable(2, "SSEEdit Managed", str(sseedit), str(tools), f'-D:"{managed / "Data"}"'),
        ),
        outgoing_runtime_root=outgoing,
        target_clone_root=clone,
        managed_source_root=managed,
    )
    assert plan_real_ref.bloqueado is True
    assert [e.clasificacion for e in plan_real_ref.entradas] == [
        ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
        ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
    ]


# =========================================================================
# Sección 6: Validación cruzada integrada (ModOrganizer.ini -> leer -> planificar)
# =========================================================================


def test_cruzada_cr03_cr04_ini_a_planificacion(tmp_path: pathlib.Path) -> None:
    """Valida extremo a extremo desde ModOrganizer.ini real con size=N y escapes Qt hasta la planificación."""
    outgoing = tmp_path / "Skyrim_Runtime_1.6.1170"
    clone = tmp_path / "Clone_1.6.1170"
    tools = tmp_path / "Tools"
    outgoing.mkdir()
    clone.mkdir()
    tools.mkdir()
    (outgoing / "skse64_loader.exe").write_bytes(b"MZ")
    (clone / "skse64_loader.exe").write_bytes(b"MZ")
    sseedit = tools / "SSEEdit.exe"
    sseedit.write_bytes(b"MZ")

    backup_data = tmp_path / "Skyrim_Runtime_1.6.1170 Backup" / "Data"
    skse_posix = str(outgoing / "skse64_loader.exe").replace("\\", "/")
    outgoing_posix = str(outgoing).replace("\\", "/")
    sseedit_posix = str(sseedit).replace("\\", "/")
    tools_posix = str(tools).replace("\\", "/")
    backup_ini = str(backup_data).replace("\\", "\\\\")

    ini_ok = tmp_path / "ModOrganizer_ok.ini"
    ini_ok.write_text(
        "\n".join(
            [
                "[customExecutables]",
                "size=3",
                "1\\title=SKSE",
                f"1\\binary={skse_posix}",
                f"1\\workingDirectory={outgoing_posix}",
                "1\\arguments=-forcesteamloader",
                "2\\title=SSEEdit Backup",
                f"2\\binary={sseedit_posix}",
                f"2\\workingDirectory={tools_posix}",
                f'2\\arguments="-D:\\"{backup_ini}\\""',
                "3\\title=SSEEdit qTools",
                f"3\\binary={sseedit_posix}",
                f"3\\workingDirectory={tools_posix}",
                "3\\arguments=-cfg C:\\\\qTools\\\\preset.ini",
                "",
            ]
        ),
        encoding="utf-8",
    )

    ejecutables_ok = tuple(leer_ejecutables_de_mo2(ini_ok))
    plan_ok = planificar_migracion_ejecutables(ejecutables_ok, outgoing, clone)
    assert plan_ok.bloqueado is False
    assert [e.clasificacion for e in plan_ok.entradas] == [
        ClasificacionEjecutableMO2.MIGRABLE_AL_CLONE,
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
        ClasificacionEjecutableMO2.EXTERNA_PRESERVABLE,
    ]

    # Casos negativos mediante INI real con doble capa (QSettings INI + _desescapar_qt_argumentos)
    outgoing_q_ini = (
        str(outgoing).replace("\\", "\\\\").replace("Skyrim_Runtime_1.6.1170", r"Skyrim_\qRuntime_1.6.1170")
    )
    outgoing_ini = str(outgoing).replace("\\", "\\\\")
    ini_neg = tmp_path / "ModOrganizer_neg.ini"
    ini_neg.write_text(
        "\n".join(
            [
                "[customExecutables]",
                "size=4",
                "1\\title=Evasion Steam q via INI",
                f"1\\binary={sseedit_posix}",
                f"1\\workingDirectory={tools_posix}",
                r"1\arguments=Steam\qapps",
                "2\\title=Evasion Steam x via INI",
                f"2\\binary={sseedit_posix}",
                f"2\\workingDirectory={tools_posix}",
                r"2\arguments=steama\xpps",
                "3\\title=Evasion Runtime q via INI",
                f"3\\binary={sseedit_posix}",
                f"3\\workingDirectory={tools_posix}",
                f'3\\arguments=-D:"{outgoing_q_ini}"',
                "4\\title=Comilla previa sin cerrar",
                f"4\\binary={sseedit_posix}",
                f"4\\workingDirectory={tools_posix}",
                f'4\\arguments=-title "Foo -p {outgoing_ini} -autoload',
                "",
            ]
        ),
        encoding="utf-8",
    )

    ejecutables_neg = tuple(leer_ejecutables_de_mo2(ini_neg))
    plan_neg = planificar_migracion_ejecutables(ejecutables_neg, outgoing, clone)
    assert plan_neg.bloqueado is True
    assert [e.clasificacion for e in plan_neg.entradas] == [
        ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
        ClasificacionEjecutableMO2.DEPENDIENTE_DE_STEAM,
        ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
        ClasificacionEjecutableMO2.AMBIGUA_O_BLOQUEADA,
    ]
