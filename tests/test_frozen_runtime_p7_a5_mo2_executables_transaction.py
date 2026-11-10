"""Tests transaccionales y recuperables para migración de [customExecutables] de MO2 (P7-A5 PR 2).

Verifica la taxonomía de ejecutables, planificación inmutable, escritura quirúrgica
simultánea con gamePath, persistencia en registro durable, comprobación de autoría,
recuperación ante interrupción y gobernanza en preflight.
"""

from __future__ import annotations

import json
import pathlib
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
    evaluar_binder_preflight,
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
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.mo2_executables_plan import (
    ClasificacionEjecutableMO2,
    MutacionCampoEjecutable,
    _dentro_de_area_steamapps,
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
