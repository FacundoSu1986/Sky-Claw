"""Tests transaccionales y recuperables para migración de [customExecutables] de MO2 (P7-A5 PR 2).

Verifica la taxonomía de ejecutables, planificación inmutable, escritura quirúrgica
simultánea con gamePath, persistencia en registro durable, comprobación de autoría,
recuperación ante interrupción y gobernanza en preflight.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

import pytest

from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightVerdict,
    BinderTransitionPolicy,
    PreflightCheckCode,
    PreflightCheckStatus,
    PreflightReasonCode,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.binder_recovery import (
    _coincide_con_lo_escrito,
    capturar_registro,
    cargar_registro,
    escribir_registro_locked,
    restaurar_registro_locked,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.mo2_executables_plan import (
    ClasificacionEjecutableMO2,
    MutacionCampoEjecutable,
    planificar_migracion_ejecutables,
)
from sky_claw.local.frozen_runtime.mo2_ini_escritura import (
    EscrituraIniNoVerificadaError,
    actualizar_game_path_en_ini,
    actualizar_game_path_por_handle,
    prever_bytes,
)
from sky_claw.local.frozen_runtime.storage import recovery_state_dir
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    RuntimeObservationError,
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
