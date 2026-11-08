"""Tests AAA y auditoría adversarial para P7-A5 PR 1 (preflight read-only Stock + runtime manual + instancia MO2).

Tracker: #867 (histórico: #672)
Contratos cubiertos:
- R1: Política explícita ``STOCK_INDEPENDENT_TRANSITION`` (Steam IDLE por defecto;
  admisión acotada en Steam ACTIVE sólo con procedencia Stock v2 íntegra,
  membresía en tres vías, autoridad externa aprobada A1, runtime saliente fuera
  de Steam, ejecutables limpios y procesos quiescentes).
- R2: Admisión estricta de ``outgoing_manual_runtime_root`` (ruta canónica, sin
  traversal/enlaces/alias 8.3, identidad real de Skyrim, disjunto de Steam,
  Stock, Generation, Clones registrados, Storage y ``base_directory`` de MO2,
  independencia física sin hardlinks y coherencia multi-superficie).
- R3: Identidad verificada de la instancia activa de MO2 (distingue instancia
  global ``%LOCALAPPDATA%\\ModOrganizer\\<CurrentInstance>`` vs instalación
  portable real con ``ModOrganizer.exe``, bloqueando instancias inactivas,
  ambiguas o desconocidas mediante ``RuntimeProbes`` inyectables).
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
from collections.abc import Sequence

import pytest

from sky_claw.local.frozen_runtime import (
    approval as approval_module,
)
from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightVerdict,
    BinderTransitionPolicy,
    PreflightCheckCode,
    PreflightCheckStatus,
    PreflightReasonCode,
    _validar_aislamiento_runtime_manual,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    registrar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime import (
    CompatibilityResult,
    DesiredRuntime,
    EffectiveRuntimeVerdict,
    LiveProcessSnapshot,
    Mo2InstanceIdentityState,
    Mo2RegistryObservation,
    Mo2RegistryState,
    RuntimeCompatibility,
    RuntimeProbes,
    assess_effective_runtime,
    probe_mo2_instance_identity,
)
from sky_claw.local.frozen_runtime.generations import (
    leer_generation_metadata,
    registrar_generation_metadata,
)
from sky_claw.local.frozen_runtime.models import (
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.provisioning import (
    PROVIDER_STOCK,
    StockProvenanceInvalidError,
    StockReference,
    StockReferenceUnapprovedError,
    adoptar_copia_stock,
    cargar_digest_aprobado_stock,
    construir_referencia_stock,
    crear_runtime_clone_desde_generation,
    publicar_generation_desde_stock,
    verificar_procedencia_stock_generacion,
)
from sky_claw.local.frozen_runtime.storage import (
    generation_dir,
    generations_state_dir,
    initialize_frozen_runtime_storage,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    RuntimeObservationError,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard

VERSION = "1.6.1170.0"
RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
RUTA_INSPECTOR = RAIZ_REPO / "scripts" / "p7_readonly_inspection.py"


def _cargar_inspector() -> types.ModuleType:
    nombre = "p7_readonly_inspection_p7_a5_test"
    spec = importlib.util.spec_from_file_location(nombre, RUTA_INSPECTOR)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


inspector = _cargar_inspector()


# ---------------------------------------------------------------------------
# Fixtures y constructores sintéticos (100% en tmp_path, sin tocar sistema real)
# ---------------------------------------------------------------------------


def _poblar_arbol_skyrim(
    raiz: pathlib.Path,
    *,
    version: str = VERSION,
    con_skse: bool = True,
    sello_extra: bytes = b"",
) -> pathlib.Path:
    (raiz / "Data" / "EmptyFolder").mkdir(parents=True, exist_ok=True)
    (raiz / "SkyrimSE.exe").write_bytes(f"SkyrimSE-{version}".encode() + sello_extra)
    (raiz / "SkyrimSELauncher.exe").write_bytes(b"Launcher-sintetico" + sello_extra)
    (raiz / "steam_api64.dll").write_bytes(b"steam-api-sintetica" + sello_extra)
    (raiz / "Data" / "Skyrim.esm").write_bytes(b"esm-payload" + sello_extra)
    if con_skse:
        (raiz / "skse64_loader.exe").write_bytes(b"loader-sintetico" + sello_extra)
        (raiz / "skse64_1_6_1170.dll").write_bytes(b"skse-dll-sintetico" + sello_extra)
    return raiz


def _crear_steam_sintetico(
    raiz_steam: pathlib.Path,
    *,
    state_flags: str = "4",
    con_descarga_activa: bool = False,
    appid: str = "489830",
    buildid: str = "13189953",
) -> pathlib.Path:
    steamapps = raiz_steam / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    _poblar_arbol_skyrim(common, sello_extra=b"-steam")
    manifest = steamapps / f"appmanifest_{appid}.acf"
    manifest.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"buildid"\t\t"{buildid}"\n\t"StateFlags"\t\t"{state_flags}"\n}}\n',
        encoding="utf-8",
    )
    if con_descarga_activa:
        descarga = steamapps / "downloading" / appid
        descarga.mkdir(parents=True, exist_ok=True)
        (descarga / "depot.chunk").write_bytes(b"actualizacion-steam-activa")
    return common


def _crear_mo2_global_sintetica(
    local_app_data: pathlib.Path,
    *,
    instance_name: str = "Skyrim Special Edition",
    game_path: pathlib.Path,
    base_directory: pathlib.Path | None = None,
    ejecutables: Sequence[tuple[str | None, ...]] | None = None,
) -> pathlib.Path:
    inst_dir = local_app_data / "ModOrganizer" / instance_name
    inst_dir.mkdir(parents=True, exist_ok=True)
    _escribir_ini_mo2(
        inst_dir / "modorganizer.ini",
        game_path=game_path,
        base_directory=base_directory,
        ejecutables=ejecutables,
    )
    return inst_dir


def _crear_mo2_portable_sintetica(
    portable_dir: pathlib.Path,
    *,
    game_path: pathlib.Path,
    con_exe: bool = True,
    con_portable_txt: bool = False,
    base_directory: pathlib.Path | None = None,
    ejecutables: Sequence[tuple[str | None, ...]] | None = None,
) -> pathlib.Path:
    portable_dir.mkdir(parents=True, exist_ok=True)
    if con_exe:
        (portable_dir / "ModOrganizer.exe").write_bytes(b"MZ-portable-mo2")
    if con_portable_txt:
        (portable_dir / "portable.txt").write_text("", encoding="utf-8")
    _escribir_ini_mo2(
        portable_dir / "modorganizer.ini",
        game_path=game_path,
        base_directory=base_directory,
        ejecutables=ejecutables,
    )
    return portable_dir


def _escribir_ini_mo2(
    ini_path: pathlib.Path,
    *,
    game_path: pathlib.Path,
    base_directory: pathlib.Path | None = None,
    ejecutables: Sequence[tuple[str | None, ...]] | None = None,
) -> None:
    escaped = str(game_path).replace("\\", "\\\\")
    lineas = [
        "[General]",
        f"gamePath=@ByteArray({escaped})",
        "gameName=Skyrim Special Edition",
        "",
    ]
    if ejecutables:
        lineas.append("[customExecutables]")
        for idx, item in enumerate(ejecutables, start=1):
            titulo = item[0]
            binario = item[1]
            workdir = item[2] if len(item) > 2 else None
            args = item[3] if len(item) > 3 else None
            lineas.append(f"{idx}\\title={titulo}")
            lineas.append(f"{idx}\\binary={binario}")
            if workdir is not None:
                lineas.append(f"{idx}\\workingDirectory={workdir}")
            if args is not None:
                lineas.append(f"{idx}\\arguments={args}")
        lineas.append("")
    lineas.append("[Settings]")
    if base_directory is not None:
        lineas.append(f"base_directory={base_directory.as_posix()}")
    ini_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def _probes_con_registro_simulado(
    *,
    local_app_data: pathlib.Path | None,
    registry_obs: Mo2RegistryObservation,
    compatibilidad: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
) -> RuntimeProbes:
    entorno_mo2 = {"LOCALAPPDATA": str(local_app_data)} if local_app_data is not None else {}

    def _identidad(raiz: pathlib.Path) -> RuntimeIdentity:
        exe = pathlib.Path(raiz) / "SkyrimSE.exe"
        if not exe.is_file():
            raise RuntimeObservationError(f"falta SkyrimSE.exe en '{raiz}'")
        contenido = exe.read_bytes()
        if not contenido.startswith(b"SkyrimSE-"):
            raise RuntimeObservationError(f"ejecutable no reconocido como Skyrim en '{raiz}'")
        return RuntimeIdentity(game_key="skyrimse", game_version=VERSION)

    return RuntimeProbes(
        identity=_identidad,
        compatibility=lambda _r: CompatibilityResult(
            state=compatibilidad,
            reason=f"sintético: {compatibilidad.value}",
            game_version=VERSION,
        ),
        processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        mo2_instance=lambda inst_dir: probe_mo2_instance_identity(
            inst_dir,
            registry_reader=lambda: registry_obs,
            environ=entorno_mo2,
        ),
    )


def _snapshot_sha256_recursivo(raices: list[pathlib.Path]) -> dict[str, str]:
    huellas: dict[str, str] = {}
    for raiz in raices:
        if raiz.is_file():
            huellas[str(raiz)] = hashlib.sha256(raiz.read_bytes()).hexdigest()
            continue
        if not raiz.exists():
            continue
        for actual, dirs, archivos in os.walk(raiz):
            dirs.sort()
            for nombre in sorted(archivos):
                p = pathlib.Path(actual) / nombre
                huellas[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return huellas


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
            game_version=VERSION,
            observed_exe_path=str(exe),
            observed_at_ns=1,
        )

    monkeypatch.setattr("sky_claw.local.frozen_runtime.provisioning.observe_runtime_identity_from_root", _identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.observe_runtime_identity_from_root", _identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root", _identidad)


class RigStockManual:
    """Escenario completo sintético similar a P7 real (Stock + Clone + Manual + Steam + MO2 Global)."""

    def __init__(self, base: pathlib.Path, *, steam_activo: bool = True) -> None:
        self.base = base
        self.managed_source = _crear_steam_sintetico(
            base / "SteamLibrary",
            state_flags="1026" if steam_activo else "4",
            con_descarga_activa=steam_activo,
        )
        self.stock_base = _poblar_arbol_skyrim(base / "GoldenMaster_Base", sello_extra=b"-stock")
        self.referencia: StockReference = construir_referencia_stock(self.stock_base)
        self.approved_digest: str = self.referencia.reference_digest
        self.approved_digest_file = base / "A1_Evidence" / "stock_reference.approved_digest.txt"
        self.approved_digest_file.parent.mkdir(parents=True, exist_ok=True)
        self.approved_digest_file.write_text(f"{self.approved_digest}\n", encoding="utf-8")

        self.stock_source = pathlib.Path(shutil.copytree(self.stock_base, base / "GoldenMaster_Source"))
        self.storage = base / "FrozenRuntime"
        assert initialize_frozen_runtime_storage(self.storage).success

        copia = adoptar_copia_stock(
            self.stock_source,
            self.referencia,
            storage_root=self.storage,
            managed_source_root=self.managed_source,
        )
        pub = publicar_generation_desde_stock(self.storage, copia, margen_bytes=0)
        assert pub.success and pub.generation_id is not None
        self.generation_id: str = pub.generation_id

        self.clones_parent = base / "FrozenClones"
        self.clones_parent.mkdir(parents=True, exist_ok=True)
        clone_res = crear_runtime_clone_desde_generation(
            self.storage,
            self.generation_id,
            self.clones_parent,
            "clone-p7-01",
            managed_source_root=self.managed_source,
            margen_bytes=0,
        )
        assert clone_res.success and clone_res.clone_root is not None
        self.clone_id = "clone-p7-01"
        self.clone_root: pathlib.Path = clone_res.clone_root

        self.manual_runtime = _poblar_arbol_skyrim(base / "Skyrim_Runtime_Manual", sello_extra=b"-manual")

        self.local_app_data = base / "LocalAppData"
        self.mo2_base_data = base / "MO2_Data" / "SkyrimSE"
        (self.mo2_base_data / "mods").mkdir(parents=True, exist_ok=True)
        # INI residual inactivo en base_directory (exactamente como en el rig real A5-R1)
        _escribir_ini_mo2(
            self.mo2_base_data / "modorganizer.ini",
            game_path=self.managed_source,
        )

        self.mo2_active_global = _crear_mo2_global_sintetica(
            self.local_app_data,
            instance_name="Skyrim Special Edition",
            game_path=self.manual_runtime,
            base_directory=self.mo2_base_data,
            ejecutables=[
                ("SKSE", f"{self.manual_runtime.as_posix()}/skse64_loader.exe", self.manual_runtime.as_posix()),
                ("LOOT", (base / "Tools" / "LOOT" / "LOOT.exe").as_posix(), None),
            ],
        )

    def probes_globales_activos(self) -> RuntimeProbes:
        return _probes_con_registro_simulado(
            local_app_data=self.local_app_data,
            registry_obs=Mo2RegistryObservation(
                state=Mo2RegistryState.OBSERVED,
                current_instance="Skyrim Special Edition",
            ),
        )


def _crear_generation_steam(storage: pathlib.Path, managed_source: pathlib.Path) -> tuple[str, pathlib.Path]:
    """Crea una Generation con provider=STEAM y un Clone válido asociado."""
    staging = storage.parent / "_staging_steam_gen"
    _poblar_arbol_skyrim(staging, sello_extra=b"-gen-steam")
    files = inventory_tree(staging)
    crit = next(f for f in files if f.rel_path == "SkyrimSE.exe")
    evidencia = SourceSnapshotEvidence(
        observed_at_ns=1000,
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        files=files,
        critical_files=(crit,),
        provider_metadata=ProviderMetadataObservation(
            provider=ManagedSourceProvider.STEAM,
            appid="489830",
            buildid="13189953",
        ),
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version=VERSION),
        tree_digest=tree_digest_from_files(files),
    )
    meta = registrar_generation_metadata(storage, evidencia)
    gen_path = generation_dir(storage, meta.generation_id)
    shutil.copytree(staging, gen_path)
    shutil.rmtree(staging)

    clone_dir = storage.parent / "FrozenClones" / "clone-steam-01"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(gen_path, clone_dir)
    registrar_runtime_clone_record(
        storage,
        RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-steam-01",
            source_generation_id=meta.generation_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-skse",
            verified_runtime_setup_id="setup-skse",
            root_path=str(clone_dir),
            admitted_role="runtime_clone",
            created_at_ns=1000,
        ),
        managed_source_root=managed_source,
    )
    return "clone-steam-01", clone_dir


# ===========================================================================
# CONTRATO R1 — Procedencia Stock y política STOCK_INDEPENDENT_TRANSITION
# ===========================================================================


class TestContratoR1StockIndependentTransition:
    """Pruebas AAA y adversariales del Contrato R1 (Tests 1 a 8)."""

    def test_01_steam_idle_conserva_comportamiento_por_defecto_y_bloquea_active_sin_politica(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 1: Con política por defecto (STRICT_PROVIDER_IDLE), Steam IDLE pasa y Steam ACTIVE bloquea."""
        # Arrange: Steam en IDLE
        rig_idle = RigStockManual(tmp_path / "idle", steam_activo=False)
        _escribir_ini_mo2(
            rig_idle.mo2_active_global / "modorganizer.ini",
            game_path=rig_idle.managed_source,
            base_directory=rig_idle.mo2_base_data,
        )

        # Act (Steam IDLE con política por defecto)
        eval_idle = evaluar_binder_preflight(
            storage_root=rig_idle.storage,
            mo2_instance_dir=rig_idle.mo2_active_global,
            clone_id=rig_idle.clone_id,
            managed_source_root=rig_idle.managed_source,
            config_skyrim_path=str(rig_idle.managed_source),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig_idle.managed_source)},
            probes=rig_idle.probes_globales_activos(),
        )

        # Assert IDLE
        assert eval_idle.transition_policy is BinderTransitionPolicy.STRICT_PROVIDER_IDLE
        assert eval_idle.verdict is BinderPreflightVerdict.READY
        assert eval_idle.is_ready is True

        # Arrange: Steam ACTIVE con Stock legítimo pero política por defecto (NO opt-in)
        rig_active = RigStockManual(tmp_path / "active_default", steam_activo=True)

        # Act
        eval_active_default = evaluar_binder_preflight(
            storage_root=rig_active.storage,
            mo2_instance_dir=rig_active.mo2_active_global,
            clone_id=rig_active.clone_id,
            managed_source_root=rig_active.managed_source,
            outgoing_manual_runtime_root=rig_active.manual_runtime,
            approved_stock_reference_digest=rig_active.approved_digest,
            config_skyrim_path=str(rig_active.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig_active.manual_runtime)},
            probes=rig_active.probes_globales_activos(),
        )

        # Assert: por defecto Steam ACTIVE SIEMPRE bloquea aunque exista Stock + A1 digest
        assert eval_active_default.verdict is BinderPreflightVerdict.BLOCKED
        assert eval_active_default.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in eval_active_default.reasons

    def test_02_steam_active_mas_stock_independiente_legitimo_pasa_bajo_politica_explicita(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 2: Steam ACTIVE + STOCK_INDEPENDENT_TRANSITION pasa sólo con las 12 condiciones probadas."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)

        # Act
        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=rig.stock_source,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )

        # Assert
        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert evaluacion.reasons == (PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,)
        assert evaluacion.stock_reference_digest == rig.approved_digest
        assert evaluacion.outgoing_manual_runtime_root == rig.manual_runtime.resolve()
        assert evaluacion.mo2_instance_identity_state is Mo2InstanceIdentityState.ACTIVE_GLOBAL

    def test_03_steam_active_mas_generation_steam_bloquea(self, tmp_path: pathlib.Path) -> None:
        """Test 3: Steam ACTIVE + Generation originada en Steam bloquea incluso bajo STOCK_INDEPENDENT_TRANSITION."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        clone_steam_id, _ = _crear_generation_steam(rig.storage, rig.managed_source)

        # Act
        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=clone_steam_id,
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

        # Assert
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.STOCK_PROVENANCE_INVALID in evaluacion.reasons
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_04_steam_indeterminate_bloquea_incluso_con_stock_valido(self, tmp_path: pathlib.Path) -> None:
        """Test 4: Steam INDETERMINATE (StateFlags vacío, corrupto o VDF roto) siempre bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        manifest = rig.managed_source.parent.parent / "appmanifest_489830.acf"
        casos_indeterminados = (
            '"AppState"\n{\n\t"appid"\t\t"489830"\n\t"buildid"\t\t"13189953"\n\t"StateFlags"\t\t""\n}\n',
            '"AppState"\n{\n\t"appid"\t\t"489830"\n\t"buildid"\t\t"13189953"\n\t"StateFlags"\t\t"corrupto"\n}\n',
            '"AppState"\n{\n\t"appid"\t\t"489830"\n\t"malformed_unclosed\n',
        )

        for contenido_acf in casos_indeterminados:
            manifest.write_text(contenido_acf, encoding="utf-8")
            evaluacion = evaluar_binder_preflight(
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
                probes=rig.probes_globales_activos(),
            )
            assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
            assert evaluacion.is_ready is False
            assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_05_sidecar_stock_ausente_bloquea_aunque_metadata_declare_provider_stock(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 5: provider='stock' en metadata sin sidecar .source.json jamás es prueba suficiente."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=False)
        sidecar = generations_state_dir(rig.storage) / f"{rig.generation_id}.source.json"
        assert leer_generation_metadata(rig.storage, rig.generation_id).provider == PROVIDER_STOCK
        sidecar.unlink()

        # Act & Assert sobre API directa y sobre preflight
        with pytest.raises(StockProvenanceInvalidError):
            verificar_procedencia_stock_generacion(
                rig.storage,
                rig.generation_id,
                approved_reference_digest=rig.approved_digest,
            )

        evaluacion = evaluar_binder_preflight(
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
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.STOCK_PROVENANCE_INVALID in evaluacion.reasons

    def test_06_sidecar_stock_adulterado_bloquea(self, tmp_path: pathlib.Path) -> None:
        """Test 6: Adulterar un campo del sidecar .source.json rompe su verificación y bloquea."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        sidecar_path = generations_state_dir(rig.storage) / f"{rig.generation_id}.source.json"
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        # Adulteración sutil: alterar el digest esperado dentro de reference sin poder falsificar el digest sellado
        payload["reference"]["expected_tree"]["files"] = 999999
        sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

        # Act
        evaluacion = evaluar_binder_preflight(
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

        # Assert
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.STOCK_PROVENANCE_INVALID in evaluacion.reasons

    def test_07_metadata_sidecar_y_membership_inconsistentes_bloquean(self, tmp_path: pathlib.Path) -> None:
        """Test 7: Desalineación entre GenerationMetadata, sidecar y directorios físicos bloquea.

        Caso adversarial discriminante: un directorio vacío extra dentro de ``versions/<gid>``
        NO altera ``TreeDigest`` (que inventaría sólo archivos), pero rompe la verificación
        de tres vías de ``directory_membership``.
        """
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        (generation_dir(rig.storage, rig.generation_id) / "Data" / "DirectorioFantasmaVacio").mkdir()

        # Act
        evaluacion = evaluar_binder_preflight(
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

        # Assert
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.STOCK_PROVENANCE_INVALID in evaluacion.reasons

    def test_08_reference_digest_distinto_o_ausente_frente_a_autoridad_aprobada_bloquea(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 8: Un sidecar internamente autoconsistente NO sustituye la aprobación externa A1."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        otro_arbol = _poblar_arbol_skyrim(tmp_path / "OtraFuenteNoAprobada", sello_extra=b"-no-aprobada")
        otra_ref = construir_referencia_stock(otro_arbol)
        assert otra_ref.reference_digest != rig.approved_digest

        # Subcaso 8a: Sin digest externo aprobado en Steam ACTIVE -> bloquea (no auto-autoriza)
        sin_digest = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=None,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            stock_source_root=rig.stock_source,
            probes=rig.probes_globales_activos(),
        )
        assert sin_digest.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.STOCK_REFERENCE_UNAPPROVED in sin_digest.reasons

        # Subcaso 8b: Digest externo aprobado de otra referencia -> bloquea
        con_digest_distinto = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=otra_ref.reference_digest,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            stock_source_root=rig.stock_source,
            probes=rig.probes_globales_activos(),
        )
        assert con_digest_distinto.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.STOCK_REFERENCE_UNAPPROVED in con_digest_distinto.reasons

        # Subcaso 8c: Archivo de digest malformado -> cargar_digest_aprobado_stock falla cerrado
        archivo_malo = tmp_path / "bad_digest.txt"
        archivo_malo.write_text("no-es-un-sha256-valido\n", encoding="utf-8")
        with pytest.raises(StockReferenceUnapprovedError):
            cargar_digest_aprobado_stock(archivo_malo)

        # Subcaso 8d (P3): Contrato completo de formato en cargar_digest_aprobado_stock (BOM y malformados)
        digest_valido = "a" * 64
        # 1. UTF-8 sin BOM -> éxito exacto
        f_sin_bom = tmp_path / "digest_sin_bom.txt"
        f_sin_bom.write_text(f"{digest_valido}\n", encoding="utf-8")
        assert cargar_digest_aprobado_stock(f_sin_bom) == digest_valido

        # 2. UTF-8 con BOM -> debe admitirse y decodificar el mismo digest exacto
        f_con_bom = tmp_path / "digest_con_bom.txt"
        f_con_bom.write_bytes(b"\xef\xbb\xbf" + f"{digest_valido}\n".encode())
        assert cargar_digest_aprobado_stock(f_con_bom) == digest_valido

        # 3. Digest alterado (carácter no hexadecimal o longitud incorrecta) -> rechazo fail-closed
        f_alterado = tmp_path / "digest_alterado.txt"
        f_alterado.write_text("a" * 63 + "z\n", encoding="utf-8")
        with pytest.raises(StockReferenceUnapprovedError, match="no es un SHA-256 hex minúsculas"):
            cargar_digest_aprobado_stock(f_alterado)

        # 4. Codificación inválida (bytes no decodificables como UTF-8) -> rechazo fail-closed
        f_invalido_enc = tmp_path / "digest_invalid_enc.bin"
        f_invalido_enc.write_bytes(b"\xff\xfe\x00\x00" + b"bytes_invalidos")
        with pytest.raises(StockReferenceUnapprovedError, match="no es UTF-8 válido"):
            cargar_digest_aprobado_stock(f_invalido_enc)

        # 5. Contenido malformado (vacío)
        f_vacio = tmp_path / "digest_vacio.txt"
        f_vacio.write_bytes(b"")
        with pytest.raises(StockReferenceUnapprovedError, match="tamaño inválido"):
            cargar_digest_aprobado_stock(f_vacio)


# ===========================================================================
# CONTRATO R2 — Admisión estricta de outgoing_manual_runtime_root
# ===========================================================================


class TestContratoR2RuntimeManualSaliente:
    """Pruebas AAA y adversariales del Contrato R2 (Tests 9 a 13 y 17)."""

    def test_09_runtime_manual_valido_y_explicito_se_admite_sin_convertirse_en_clone(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 9: Runtime manual explícito se admite para transición pero NO es un Clone válido en el oráculo."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=False)

        # Act
        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )

        # Assert preflight admite transición saliente
        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True

        # Assert que el oráculo de Effective Runtime NO trata el runtime manual como Desired Clone válido
        obs_oraculo = assess_effective_runtime(
            mo2_instance_dir=rig.mo2_active_global,
            desired=DesiredRuntime(
                clone_id=rig.clone_id,
                clone_root=rig.clone_root,
                generation_id=rig.generation_id,
            ),
            managed_source_root=rig.managed_source,
            generation_roots=(generation_dir(rig.storage, rig.generation_id),),
            config_skyrim_path=str(rig.manual_runtime),
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )
        assert obs_oraculo.verdict is EffectiveRuntimeVerdict.MISMATCH
        assert obs_oraculo.authorizes_activation is False

    def test_09b_runtime_manual_retorna_manual_norm_directo_sin_resolucion_incompatible(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2: _validar_runtime_manual_saliente devuelve manual_norm directamente (no resolve()),
        evitando divergencias falsas con config_skyrim_path ante unidades virtuales o caminos no canónicos.
        """
        rig = RigStockManual(tmp_path, steam_activo=False)
        orig_resolve = pathlib.Path.resolve

        def _mock_resolve(path_obj: pathlib.Path, strict: bool = False) -> pathlib.Path:
            # Simula una unidad virtual Windows (subst/network) cuya resolución a bajo nivel difiere
            if os.path.normcase(str(path_obj)) == os.path.normcase(str(rig.manual_runtime)):
                return tmp_path / "UnidadFisicaSubyacente" / "Skyrim_Runtime_Manual"
            return orig_resolve(path_obj, strict=strict)

        monkeypatch.setattr(pathlib.Path, "resolve", _mock_resolve)

        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )
        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True

    def test_10_runtime_manual_no_declarado_sigue_rechazandose_como_tercer_directorio(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 10: Con outgoing_manual_runtime_root=None, apuntar a un tercer directorio bloquea."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=False)

        # Act
        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=None,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            stock_source_root=rig.stock_source,
            probes=rig.probes_globales_activos(),
        )

        # Assert
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_11_runtime_manual_solapado_con_steam_storage_stock_clone_o_mo2_data_bloquea(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 11: outgoing_manual_runtime_root dentro de Steam, Stock, Storage, Clone o datos de MO2 bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=False)

        # Candidatos prohibidos: Steam common, otro subdirectorio de steamapps, Stock, Clone, MO2 base_directory
        en_steamapps = _poblar_arbol_skyrim(
            rig.managed_source.parent / "Skyrim Manual Dentro Steamapps",
            sello_extra=b"-in-steamapps",
        )
        en_mo2_datos = _poblar_arbol_skyrim(
            rig.mo2_base_data / "mods" / "RootBuilderFakeRuntime",
            sello_extra=b"-in-mo2-data",
        )

        for prohibido in (
            rig.managed_source,
            en_steamapps,
            rig.stock_source,
            rig.clone_root,
            en_mo2_datos,
        ):
            _escribir_ini_mo2(
                rig.mo2_active_global / "modorganizer.ini",
                game_path=prohibido,
                base_directory=rig.mo2_base_data,
            )
            evaluacion = evaluar_binder_preflight(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=prohibido,
                stock_source_root=rig.stock_source,
                config_skyrim_path=str(prohibido),
                exigir_config_skyrim_path=True,
                environ={"SKYRIM_PATH": str(prohibido)},
                probes=rig.probes_globales_activos(),
            )
            assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED, f"no bloqueó {prohibido}"
            assert evaluacion.is_ready is False
            assert (
                PreflightReasonCode.PATHS_COLLIDE in evaluacion.reasons
                or PreflightReasonCode.PATH_CONTAINMENT_VIOLATION in evaluacion.reasons
                or PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in evaluacion.reasons
            )

    def test_11b_directorio_sin_identidad_real_de_skyrim_bloquea_como_runtime_manual(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Un directorio arbitrario sin ejecutable válido de Skyrim se rechaza como runtime manual."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        falso_runtime = tmp_path / "CarpetaCualquiera"
        falso_runtime.mkdir()
        (falso_runtime / "SkyrimSE.exe").write_bytes(b"NO-ES-UN-EJECUTABLE-DE-SKYRIM")
        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=falso_runtime,
            base_directory=rig.mo2_base_data,
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=falso_runtime,
            config_skyrim_path=str(falso_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(falso_runtime)},
            probes=rig.probes_globales_activos(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in evaluacion.reasons

    def test_11c_otro_clone_en_state_corrupto_o_redirigido_bloquea_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """P1: Un registro de otro Clone corrupto o con formato inválido en storage bloquea fail-closed."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        clones_state = rig.storage / "state" / "clones"
        clones_state.mkdir(parents=True, exist_ok=True)
        # Inyectamos un registro corrupto para otro clone (no target_clone_id)
        (clones_state / "clone-corrupto.json").write_text("{json-invalido-y-truncado", encoding="utf-8")

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_RECORD_CORRUPT in evaluacion.reasons

    def test_12_ruta_relativa_evasiva_y_alias_83_en_runtime_manual_bloquean(self, tmp_path: pathlib.Path) -> None:
        """Test 12 (parte multiplataforma): traversal relativo ('..') y alias 8.3 bloquean."""
        rig = RigStockManual(tmp_path, steam_activo=False)

        # 12a: Traversal relativo evasivo (aunque resuelva al runtime manual legítimo)
        ruta_evasiva = pathlib.Path(f"{rig.manual_runtime}{os.sep}..{os.sep}{rig.manual_runtime.name}")
        eval_traversal = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=ruta_evasiva,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            stock_source_root=rig.stock_source,
            probes=rig.probes_globales_activos(),
        )
        assert eval_traversal.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in eval_traversal.reasons

        # 12b: Alias corto 8.3
        ruta_83 = tmp_path / "SKYRIM~1" / "Runtime"
        eval_83 = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=ruta_83,
            config_skyrim_path=str(ruta_83),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(ruta_83)},
            probes=rig.probes_globales_activos(),
        )
        assert eval_83.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED in eval_83.reasons

    @junction_guard
    def test_12b_junction_en_runtime_manual_bloquea(self, tmp_path: pathlib.Path) -> None:
        """Test 12 (junction Windows): un junction hacia el runtime manual legítimo bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        enlace_junction = tmp_path / "Manual_Junction"
        motivo = crear_junction(enlace_junction, rig.manual_runtime)
        assert motivo is None, motivo

        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=enlace_junction,
            base_directory=rig.mo2_base_data,
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=enlace_junction,
            config_skyrim_path=str(enlace_junction),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(enlace_junction)},
            probes=rig.probes_globales_activos(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.REPARSE_POINT_DETECTED in evaluacion.reasons

    @symlink_guard
    def test_12c_symlink_en_runtime_manual_bloquea(self, tmp_path: pathlib.Path) -> None:
        """Test 12 (symlink): un symlink de directorio hacia el runtime manual bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        enlace_sym = tmp_path / "Manual_Symlink"
        enlace_sym.symlink_to(rig.manual_runtime, target_is_directory=True)

        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=enlace_sym,
            base_directory=rig.mo2_base_data,
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=enlace_sym,
            config_skyrim_path=str(enlace_sym),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(enlace_sym)},
            probes=rig.probes_globales_activos(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.REPARSE_POINT_DETECTED in evaluacion.reasons

    def test_13_clone_o_runtime_manual_compartido_por_hardlink_con_steam_o_generation_bloquea(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Test 13: Hardlink compartido entre Clone↔Generation/Steam o Manual↔Steam bloquea."""
        # Subcaso 13a: Runtime manual comparte un hardlink con Steam durante Steam ACTIVE
        rig_manual_hl = RigStockManual(tmp_path / "hl_manual", steam_activo=True)
        (rig_manual_hl.manual_runtime / "steam_api64.dll").unlink()
        os.link(
            rig_manual_hl.managed_source / "steam_api64.dll",
            rig_manual_hl.manual_runtime / "steam_api64.dll",
        )

        eval_manual_hl = evaluar_binder_preflight(
            storage_root=rig_manual_hl.storage,
            mo2_instance_dir=rig_manual_hl.mo2_active_global,
            clone_id=rig_manual_hl.clone_id,
            managed_source_root=rig_manual_hl.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig_manual_hl.manual_runtime,
            approved_stock_reference_digest=rig_manual_hl.approved_digest,
            config_skyrim_path=str(rig_manual_hl.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig_manual_hl.manual_runtime)},
            probes=rig_manual_hl.probes_globales_activos(),
        )
        assert eval_manual_hl.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in eval_manual_hl.reasons

        # Subcaso 13b: Clone comparte un hardlink con la Generation publicada
        rig_clone_hl = RigStockManual(tmp_path / "hl_clone", steam_activo=True)
        gen_dll = generation_dir(rig_clone_hl.storage, rig_clone_hl.generation_id) / "steam_api64.dll"
        clone_dll = rig_clone_hl.clone_root / "steam_api64.dll"
        clone_dll.unlink()
        os.link(gen_dll, clone_dll)

        eval_clone_hl = evaluar_binder_preflight(
            storage_root=rig_clone_hl.storage,
            mo2_instance_dir=rig_clone_hl.mo2_active_global,
            clone_id=rig_clone_hl.clone_id,
            managed_source_root=rig_clone_hl.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig_clone_hl.manual_runtime,
            approved_stock_reference_digest=rig_clone_hl.approved_digest,
            config_skyrim_path=str(rig_clone_hl.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig_clone_hl.manual_runtime)},
            probes=rig_clone_hl.probes_globales_activos(),
        )
        assert eval_clone_hl.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID in eval_clone_hl.reasons

    def test_13c_runtime_manual_con_hardlinks_disjuntos_de_steam_clone_y_gen_pasa(self, tmp_path: pathlib.Path) -> None:
        """P1: Hardlinks históricos en runtime manual saliente disjuntos de Steam/Clone/Gen/Stock se admiten."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        # Creamos un archivo externo disjunto y un hardlink en el runtime manual
        carpeta_externa = tmp_path / "OtraHerramientaExterna"
        carpeta_externa.mkdir(parents=True, exist_ok=True)
        archivo_externo = carpeta_externa / "archivo_compartido.dll"
        archivo_externo.write_bytes(b"DLL-EXTERNA-DISJUNTA-12345")

        destino_hl = rig.manual_runtime / "archivo_compartido.dll"
        os.link(archivo_externo, destino_hl)

        # Verificamos que efectivamente tiene st_nlink == 2
        st = os.stat(destino_hl)
        assert st.st_nlink == 2

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert any("hardlink(s) históricos disjuntos" in n for n in evaluacion.notes)

    def test_13d_hardlink_con_distinta_ruta_relativa_hacia_steam_bloquea(self, tmp_path: pathlib.Path) -> None:
        """P1: Hardlink entre Runtime Manual ('archivo_B.dll') y Steam ('archivo_A.dll') con distinta ruta relativa bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        # Steam contiene 'archivo_A.dll'
        archivo_a = rig.managed_source / "archivo_A.dll"
        archivo_a.write_bytes(b"CONTENIDO-A-STEAM-12345")

        # Runtime manual contiene 'archivo_B.dll' que es un hardlink físico de 'archivo_A.dll'
        archivo_b = rig.manual_runtime / "archivo_B.dll"
        os.link(archivo_a, archivo_b)

        # Verificamos que comparten st_ino en el filesystem pero sus nombres son distintos
        st_a = os.stat(archivo_a)
        st_b = os.stat(archivo_b)
        assert (st_a.st_dev, st_a.st_ino) == (st_b.st_dev, st_b.st_ino)
        assert archivo_a.name != archivo_b.name

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in evaluacion.reasons
        check_safety = next(
            (c for c in evaluacion.checks if c.check is PreflightCheckCode.PATH_SAFETY),
            None,
        )
        assert check_safety is not None
        assert check_safety.status is PreflightCheckStatus.FAIL
        assert "archivo_B.dll" in check_safety.detail
        assert "archivo_A.dll" in check_safety.detail

    def test_13e_hardlink_cruzado_con_distinta_ruta_hacia_stock_o_generation_bloquea(
        self, tmp_path: pathlib.Path
    ) -> None:
        """P1: Hardlink con diferente ruta relativa hacia fuente Stock o Generation publicada bloquea."""
        # Subcaso Stock: Stock contiene 'fuente_stock_x.dll', manual contiene 'runtime_manual_y.dll'
        rig = RigStockManual(tmp_path / "caso_stock", steam_activo=True)
        stock_file = rig.stock_source / "fuente_stock_x.dll"
        stock_file.write_bytes(b"CONTENIDO-STOCK-FUENTE-XYZ")
        manual_hl_stock = rig.manual_runtime / "runtime_manual_y.dll"
        os.link(stock_file, manual_hl_stock)

        eval_stock = evaluar_binder_preflight(
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

        assert eval_stock.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in eval_stock.reasons
        check_safety = next(
            (c for c in eval_stock.checks if c.check is PreflightCheckCode.PATH_SAFETY),
            None,
        )
        assert check_safety is not None
        assert check_safety.status is PreflightCheckStatus.FAIL
        assert "runtime_manual_y.dll" in check_safety.detail
        assert "fuente_stock_x.dll" in check_safety.detail

        # Subcaso Generation: Generation contiene 'gen_archive_x.dll', manual contiene 'manual_archive_z.dll'
        rig_gen = RigStockManual(tmp_path / "caso_gen", steam_activo=True)
        gen_dir = generation_dir(rig_gen.storage, rig_gen.generation_id)
        gen_file = gen_dir / "gen_archive_x.dll"
        gen_file.write_bytes(b"CONTENIDO-GENERATION-XYZ")
        manual_hl_gen = rig_gen.manual_runtime / "manual_archive_z.dll"
        os.link(gen_file, manual_hl_gen)

        eval_gen = evaluar_binder_preflight(
            storage_root=rig_gen.storage,
            mo2_instance_dir=rig_gen.mo2_active_global,
            clone_id=rig_gen.clone_id,
            managed_source_root=rig_gen.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig_gen.manual_runtime,
            approved_stock_reference_digest=rig_gen.approved_digest,
            config_skyrim_path=str(rig_gen.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig_gen.manual_runtime)},
            stock_source_root=rig_gen.stock_source,
            probes=rig_gen.probes_globales_activos(),
        )

        assert eval_gen.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in eval_gen.reasons
        check_gen_safety = next(
            (c for c in eval_gen.checks if c.check is PreflightCheckCode.PATH_SAFETY),
            None,
        )
        assert check_gen_safety is not None
        assert check_gen_safety.status is PreflightCheckStatus.FAIL
        assert "manual_archive_z.dll" in check_gen_safety.detail
        assert "gen_archive_x.dll" in check_gen_safety.detail

    def test_13f_superficie_protegida_con_identidad_fisica_invalida_falla_cerrado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1: st_ino <= 0 o error de acceso al inspeccionar superficie protegida falla cerrado."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        real_stat = os.stat

        def mock_stat(
            path: os.PathLike[str] | str,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            res = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if "SteamLibrary" in str(path) and str(path).endswith("SkyrimSE.exe"):
                return os.stat_result(
                    (
                        res.st_mode,
                        0,
                        res.st_dev,
                        res.st_nlink,
                        res.st_uid,
                        res.st_gid,
                        res.st_size,
                        int(res.st_atime),
                        int(res.st_mtime),
                        int(res.st_ctime),
                    ),
                    {"st_file_attributes": 0},
                )
            return res

        monkeypatch.setattr(os, "stat", mock_stat)

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in evaluacion.reasons
        check_safety = next(
            (c for c in evaluacion.checks if c.check is PreflightCheckCode.PATH_SAFETY),
            None,
        )
        assert check_safety is not None
        assert check_safety.status is PreflightCheckStatus.FAIL
        assert (
            "identidad física no disponible" in check_safety.detail
            or "error al verificar inodos" in check_safety.detail
        )

    def test_13g_hardlink_con_archivo_en_steamapps_downloading_bloquea(self, tmp_path: pathlib.Path) -> None:
        """P1 adversarial: Hardlink entre Runtime Manual ('mod_manual.dll') y 'steamapps/downloading/489830/patch.dll' bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        # Crear archivo en área de staging de Steam (downloading/489830)
        steamapps_dir = rig.managed_source.parent.parent
        downloading_dir = steamapps_dir / "downloading" / "489830"
        downloading_dir.mkdir(parents=True, exist_ok=True)
        archivo_staging = downloading_dir / "patch.dll"
        archivo_staging.write_bytes(b"CONTENIDO-STAGING-STEAM-DOWNLOAD-12345")

        # Hardlink en runtime manual con distinto nombre
        archivo_manual = rig.manual_runtime / "mod_manual.dll"
        os.link(archivo_staging, archivo_manual)

        st_stage = os.stat(archivo_staging)
        st_manual = os.stat(archivo_manual)
        assert (st_stage.st_dev, st_stage.st_ino) == (st_manual.st_dev, st_manual.st_ino)
        assert archivo_staging.name != archivo_manual.name

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID in evaluacion.reasons
        check_safety = next(
            (c for c in evaluacion.checks if c.check is PreflightCheckCode.PATH_SAFETY),
            None,
        )
        assert check_safety is not None
        assert check_safety.status is PreflightCheckStatus.FAIL
        assert "mod_manual.dll" in check_safety.detail
        assert "patch.dll" in check_safety.detail

    def test_13h_raices_protegidas_obligatorias_ausentes_o_invalidas_bloquea(self, tmp_path: pathlib.Path) -> None:
        """P1 adversarial: Raíces protegidas obligatorias ausentes, inaccesibles o no-directorios nunca declaran independencia."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        gen_dir = generation_dir(rig.storage, rig.generation_id)

        # 1. generation_root ausente
        st_gen, rz_gen, det_gen = _validar_aislamiento_runtime_manual(
            rig.manual_runtime,
            generation_root=tmp_path / "GenerationInexistente",
            managed_source_root=rig.managed_source,
            clone_root=rig.clone_root,
            stock_source_root=rig.stock_source,
        )
        assert st_gen is PreflightCheckStatus.FAIL
        assert rz_gen is PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID
        assert "Generation ausente o no es directorio" in det_gen

        # 2. managed_source_root ausente
        st_ms, rz_ms, det_ms = _validar_aislamiento_runtime_manual(
            rig.manual_runtime,
            generation_root=gen_dir,
            managed_source_root=tmp_path / "SteamInexistente",
            clone_root=rig.clone_root,
            stock_source_root=rig.stock_source,
        )
        assert st_ms is PreflightCheckStatus.FAIL
        assert rz_ms is PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID
        assert "Managed Source (Steam) ausente o no es directorio" in det_ms

        # 3. clone_root ausente
        st_cl, rz_cl, det_cl = _validar_aislamiento_runtime_manual(
            rig.manual_runtime,
            generation_root=gen_dir,
            managed_source_root=rig.managed_source,
            clone_root=tmp_path / "CloneInexistente",
            stock_source_root=rig.stock_source,
        )
        assert st_cl is PreflightCheckStatus.FAIL
        assert rz_cl is PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID
        assert "Clone destino ausente o no es directorio" in det_cl

        # 4. stock_source_root declarado pero no es un directorio (archivo regular)
        archivo_falso_stock = tmp_path / "archivo_falso_stock.txt"
        archivo_falso_stock.write_text("no-es-directorio")
        st_stk, rz_stk, det_stk = _validar_aislamiento_runtime_manual(
            rig.manual_runtime,
            generation_root=gen_dir,
            managed_source_root=rig.managed_source,
            clone_root=rig.clone_root,
            stock_source_root=archivo_falso_stock,
        )
        assert st_stk is PreflightCheckStatus.FAIL
        assert rz_stk is PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID
        assert "fuente Stock declarada no es un directorio" in det_stk

        # 5. staging_dir que existe pero no es un directorio
        falso_staging = tmp_path / "staging_falso_archivo"
        falso_staging.write_text("no-es-directorio")
        st_stg, rz_stg, det_stg = _validar_aislamiento_runtime_manual(
            rig.manual_runtime,
            generation_root=gen_dir,
            managed_source_root=rig.managed_source,
            clone_root=rig.clone_root,
            stock_source_root=rig.stock_source,
            steam_staging_roots=(falso_staging,),
        )
        assert st_stg is PreflightCheckStatus.FAIL
        assert rz_stg is PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID
        assert "área de staging de Steam existe pero no es un directorio" in det_stg


# ===========================================================================
# CONTRATO R3 — Identidad de la instancia MO2 activa (Global vs Portable)
# ===========================================================================


class TestContratoR3IdentidadInstanciaMO2:
    """Pruebas AAA y adversariales del Contrato R3 (Tests 14, 15, 16 y 17)."""

    def test_14_instancia_mo2_inactiva_o_registro_ilegible_bloquea(self, tmp_path: pathlib.Path) -> None:
        """Test 14: Pasar el INI residual de base_directory (G:\\Modding\\MO2\\SkyrimSE) bloquea."""
        # Arrange: HKCU CurrentInstance apunta a 'Skyrim Special Edition' en LocalAppData,
        # pero el llamador pasa mo2_base_data (que también posee un ModOrganizer.ini residual!)
        rig = RigStockManual(tmp_path, steam_activo=True)
        _escribir_ini_mo2(
            rig.mo2_base_data / "modorganizer.ini",
            game_path=rig.manual_runtime,
        )

        # Act
        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_base_data,
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

        # Assert: bloquea específicamente por instancia MO2 inactiva
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MO2_INSTANCE_INACTIVE in evaluacion.reasons

        # Subcaso 14b: Registro HKCU ilegible (UNKNOWN != PASS)
        probes_reg_error = _probes_con_registro_simulado(
            local_app_data=rig.local_app_data,
            registry_obs=Mo2RegistryObservation(
                state=Mo2RegistryState.UNREADABLE,
                detail="Permiso denegado al abrir HKCU",
            ),
        )
        eval_unknown = evaluar_binder_preflight(
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
            probes=probes_reg_error,
        )
        assert eval_unknown.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.MO2_INSTANCE_IDENTITY_UNKNOWN in eval_unknown.reasons

    def test_15_instancia_mo2_global_valida_se_identifica_correctamente(self, tmp_path: pathlib.Path) -> None:
        """Test 15: CurrentInstance en HKCU + directorio coincidente bajo %LOCALAPPDATA%\\ModOrganizer => ACTIVE_GLOBAL."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        obs = probe_mo2_instance_identity(
            rig.mo2_active_global,
            registry_reader=lambda: Mo2RegistryObservation(
                state=Mo2RegistryState.OBSERVED,
                current_instance="Skyrim Special Edition",
            ),
            environ={"LOCALAPPDATA": str(rig.local_app_data)},
        )
        assert obs.state is Mo2InstanceIdentityState.ACTIVE_GLOBAL
        assert obs.is_active is True
        assert obs.registry_current_instance == "Skyrim Special Edition"

    def test_16_instancia_portable_valida_no_se_confunde_con_la_global(self, tmp_path: pathlib.Path) -> None:
        """Test 16: Con CurrentInstance="" o ABSENT fuera de LocalAppData, la identidad portable es AMBIGUOUS."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        # 16a: Instalación portable (ModOrganizer.exe + ModOrganizer.ini + CurrentInstance="")
        # Como el Registro no guarda rutas en modo portable, dos directorios portables son indistinguibles => AMBIGUOUS
        portable_dir = _crear_mo2_portable_sintetica(
            tmp_path / "MO2_Portable",
            game_path=rig.manual_runtime,
            con_exe=True,
            con_portable_txt=False,
            base_directory=rig.mo2_base_data,
        )
        obs_portable = probe_mo2_instance_identity(
            portable_dir,
            registry_reader=lambda: Mo2RegistryObservation(state=Mo2RegistryState.OBSERVED, current_instance=""),
            environ={"LOCALAPPDATA": str(rig.local_app_data)},
        )
        assert obs_portable.state is Mo2InstanceIdentityState.AMBIGUOUS
        assert obs_portable.is_active is False

        # Preflight fail-closed ante ambigüedad en directorio portable
        probes_portable = _probes_con_registro_simulado(
            local_app_data=rig.local_app_data,
            registry_obs=Mo2RegistryObservation(state=Mo2RegistryState.OBSERVED, current_instance=""),
        )
        eval_portable = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=portable_dir,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=rig.approved_digest,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=probes_portable,
        )
        assert eval_portable.verdict is BinderPreflightVerdict.BLOCKED
        assert eval_portable.is_ready is False
        assert PreflightReasonCode.MO2_INSTANCE_AMBIGUOUS in eval_portable.reasons

        # 16b: Instalación portable con portable.txt y clave en Registro ABSENT => AMBIGUOUS
        portable_txt_dir = _crear_mo2_portable_sintetica(
            tmp_path / "MO2_Portable_Txt",
            game_path=rig.manual_runtime,
            con_exe=True,
            con_portable_txt=True,
        )
        obs_portable_txt = probe_mo2_instance_identity(
            portable_txt_dir,
            registry_reader=lambda: Mo2RegistryObservation(state=Mo2RegistryState.ABSENT),
            environ={"LOCALAPPDATA": str(rig.local_app_data)},
        )
        assert obs_portable_txt.state is Mo2InstanceIdentityState.AMBIGUOUS
        assert obs_portable_txt.is_active is False

        # 16c: Con CurrentInstance="" (selector portable), pasar la carpeta GLOBAL en %LOCALAPPDATA% DEBE bloquear
        eval_global_con_selector_portable = evaluar_binder_preflight(
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
            probes=probes_portable,
        )
        assert eval_global_con_selector_portable.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.MO2_INSTANCE_INACTIVE in eval_global_con_selector_portable.reasons

        # 16d: Carpeta sin ModOrganizer.exe con CurrentInstance="" NO es portable válida => bloquea
        obs_huerfana = probe_mo2_instance_identity(
            rig.mo2_base_data,
            registry_reader=lambda: Mo2RegistryObservation(state=Mo2RegistryState.OBSERVED, current_instance=""),
            environ={"LOCALAPPDATA": str(rig.local_app_data)},
        )
        assert obs_huerfana.state is Mo2InstanceIdentityState.INACTIVE
        assert obs_huerfana.is_active is False

        # 16e: Ambigüedad: portable.txt + ModOrganizer.exe mientras HKCU selecciona una instancia global existente
        probes_conflicto = _probes_con_registro_simulado(
            local_app_data=rig.local_app_data,
            registry_obs=Mo2RegistryObservation(
                state=Mo2RegistryState.OBSERVED,
                current_instance="Skyrim Special Edition",
            ),
        )
        eval_ambigua = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=portable_txt_dir,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=rig.approved_digest,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=probes_conflicto,
        )
        assert eval_ambigua.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.MO2_INSTANCE_AMBIGUOUS in eval_ambigua.reasons

    def test_17_config_mo2_environ_divergentes_y_ejecutables_en_steam_bloquean(self, tmp_path: pathlib.Path) -> None:
        """Test 17: Divergencia entre config.skyrim_path, MO2 gamePath, SKYRIM_PATH o ejecutables Steam bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        # 17a: MO2 gamePath = manual_runtime, pero config.skyrim_path = Steam (split-brain real de A5-R1)
        eval_cfg_div = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=rig.approved_digest,
            config_skyrim_path=str(rig.managed_source),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )
        assert eval_cfg_div.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in eval_cfg_div.reasons

        # 17b: SKYRIM_PATH en environ apunta a Steam mientras MO2 y config apuntan a manual_runtime
        eval_env_div = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest=rig.approved_digest,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.managed_source)},
            probes=rig.probes_globales_activos(),
        )
        assert eval_env_div.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in eval_env_div.reasons

        # 17c: Entrada en [customExecutables] (p. ej. SkyrimSELauncher.exe) apuntando a Steam bloquea
        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=rig.manual_runtime,
            base_directory=rig.mo2_base_data,
            ejecutables=[
                ("SKSE", f"{rig.manual_runtime.as_posix()}/skse64_loader.exe", rig.manual_runtime.as_posix()),
                (
                    "Skyrim Special Edition Launcher",
                    f"{rig.managed_source.as_posix()}/SkyrimSELauncher.exe",
                    rig.managed_source.as_posix(),
                ),
            ],
        )
        eval_exe_steam = evaluar_binder_preflight(
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
        assert eval_exe_steam.verdict is BinderPreflightVerdict.BLOCKED
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in eval_exe_steam.reasons

    def test_17d_custom_executables_con_argumentos_apuntando_a_steam_bloquean(self, tmp_path: pathlib.Path) -> None:
        """P1: Herramienta externa con binario fuera de Steam pero argumentos apuntando a Steam bloquea."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        # Herramienta externa en C:\Tools\xEdit.exe, pero con argumentos que referencian la Managed Source de Steam
        tool_dir = tmp_path / "HerramientasExternas"
        tool_dir.mkdir(parents=True, exist_ok=True)
        tool_exe = tool_dir / "xEdit64.exe"
        tool_exe.write_bytes(b"MZ-xedit")

        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=rig.manual_runtime,
            base_directory=rig.mo2_base_data,
            ejecutables=[
                ("SKSE", f"{rig.manual_runtime.as_posix()}/skse64_loader.exe", rig.manual_runtime.as_posix()),
                (
                    "xEdit",
                    tool_exe.as_posix(),
                    tool_dir.as_posix(),
                    f'--game-path "{rig.managed_source.as_posix()}"',
                ),
            ],
        )

        evaluacion = evaluar_binder_preflight(
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

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons
        chk_cfg = next((c for c in evaluacion.checks if c.check.value == "config_coherence"), None)
        assert chk_cfg is not None
        assert "arguments (xEdit)" in chk_cfg.detail


# ===========================================================================
# Invariantes de seguridad, Read-Only, Inspector P7 y No-Activación
# ===========================================================================


class TestInvariantesReadOnlyYHermanoInspectorP7:
    """Pruebas 18, 19 y 20: 100% READ-ONLY, alineación con inspector P7 y cierre productivo intacto."""

    def test_18_preflight_y_sondas_permanecen_estrictamente_read_only(self, tmp_path: pathlib.Path) -> None:
        """Test 18: Snapshot criptográfico pre/post + auditoría AST de no-mutación."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        raices = [
            rig.managed_source.parent.parent,
            rig.stock_source,
            rig.storage,
            rig.clone_root,
            rig.manual_runtime,
            rig.local_app_data,
            rig.mo2_base_data,
            rig.approved_digest_file,
        ]
        antes = _snapshot_sha256_recursivo(raices)

        evaluacion = evaluar_binder_preflight(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=rig.stock_source,
            config_skyrim_path=str(rig.manual_runtime),
            exigir_config_skyrim_path=True,
            environ={"SKYRIM_PATH": str(rig.manual_runtime)},
            probes=rig.probes_globales_activos(),
        )
        assert evaluacion.is_ready is True

        despues = _snapshot_sha256_recursivo(raices)
        assert despues == antes, "evaluar_binder_preflight mutó algún archivo en disco"

        # Verificar además por AST que mo2_instance.py no usa winreg de escritura
        ruta_mo2_inst = RAIZ_REPO / "sky_claw" / "local" / "frozen_runtime" / "effective_runtime" / "mo2_instance.py"
        arbol = ast.parse(ruta_mo2_inst.read_text(encoding="utf-8"), filename=str(ruta_mo2_inst))
        prohibidas_winreg = {
            "CreateKey",
            "CreateKeyEx",
            "SetValue",
            "SetValueEx",
            "DeleteKey",
            "DeleteKeyEx",
            "DeleteValue",
            "SaveKey",
            "KEY_WRITE",
            "KEY_SET_VALUE",
            "KEY_ALL_ACCESS",
        }
        ofensas: list[str] = []
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and nodo.attr in prohibidas_winreg:
                ofensas.append(f"{nodo.lineno}: winreg.{nodo.attr}")
            elif isinstance(nodo, ast.Name) and nodo.id in prohibidas_winreg:
                ofensas.append(f"{nodo.lineno}: {nodo.id}")
        assert ofensas == [], f"operación mutante de Registro detectada: {ofensas}"

    def test_19_inspector_p7_aplica_criterios_compatibles_sin_declarar_runtime_activado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test 19: El inspector P7 propaga la política/runtime manual/digest al preflight, redacta rutas,
        pero MANTIENE listo_para_p7=False mientras Desired Runtime y MO2 sigan en el runtime manual!
        """
        rig = RigStockManual(tmp_path, steam_activo=True)
        cfg_toml = tmp_path / "config.toml"
        escaped_manual = str(rig.manual_runtime).replace("\\", "\\\\")
        cfg_toml.write_text(f'skyrim_path = "{escaped_manual}"\n', encoding="utf-8")

        monkeypatch.setattr(inspector, "default_runtime_probes", rig.probes_globales_activos)
        monkeypatch.setenv("SKYRIM_PATH", str(rig.manual_runtime))

        # 19a: Con STOCK_INDEPENDENT_TRANSITION + runtime manual + digest aprobado:
        # binder_preflight pasa a READY, PERO el informe global sigue BLOQUEADO porque aún no se conmutó al Clone!
        informe_ok_preflight, managed_efectiva = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=False,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=rig.stock_source,
        )
        assert managed_efectiva == rig.managed_source
        assert informe_ok_preflight["preflight_ready"] is True
        assert informe_ok_preflight["preflight_verdict"] == BinderPreflightVerdict.READY.value
        hallazgos_por_clave = {h["clave"]: h for h in informe_ok_preflight["hallazgos"]}
        assert [k for k in hallazgos_por_clave if k.startswith("preflight.")] == []
        assert hallazgos_por_clave["desired_runtime"]["veredicto"] == inspector.HALLAZGO_BLOQUEADO
        assert hallazgos_por_clave["mo2.game_path"]["veredicto"] == inspector.HALLAZGO_BLOQUEADO
        assert hallazgos_por_clave["skyclaw.config"]["veredicto"] == inspector.HALLAZGO_BLOQUEADO
        assert informe_ok_preflight["veredicto"] == inspector.VEREDICTO_BLOQUEADO
        assert informe_ok_preflight["listo_para_p7"] is False

        # Verificar que ninguna ruta privada filtró en los detalles con mostrar_rutas=False
        detalles = "\n".join(str(h.get("detalle", "")) for h in informe_ok_preflight["hallazgos"])
        assert str(tmp_path) not in detalles
        assert str(rig.manual_runtime) not in detalles
        assert rig.manual_runtime.as_posix() not in detalles

        # 19b: Sin digest externo aprobado en Steam ACTIVE -> binder_preflight también bloquea en el inspector
        informe_sin_digest, _ = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=False,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=None,
        )
        assert informe_sin_digest["preflight_ready"] is False
        assert informe_sin_digest["preflight_verdict"] == BinderPreflightVerdict.BLOCKED.value
        hallazgos_sin_digest = [h for h in informe_sin_digest["hallazgos"] if str(h["clave"]).startswith("preflight.")]
        assert hallazgos_sin_digest
        assert any(h["veredicto"] == inspector.HALLAZGO_BLOQUEADO for h in hallazgos_sin_digest)
        detalles_sin_digest = "\n".join(str(h.get("detalle", "")) for h in informe_sin_digest["hallazgos"])
        assert str(tmp_path) not in detalles_sin_digest
        assert str(rig.manual_runtime) not in detalles_sin_digest

        # 19c: CLI real del inspector acepta las banderas nuevas por línea de comandos
        proc = subprocess.run(
            [
                sys.executable,
                str(RUTA_INSPECTOR),
                "--storage-root",
                str(rig.storage),
                "--mo2-instance",
                str(rig.mo2_active_global),
                "--managed-source",
                str(rig.managed_source),
                "--clone-id",
                rig.clone_id,
                "--transition-policy",
                "stock_independent_transition",
                "--outgoing-manual-runtime",
                str(rig.manual_runtime),
                "--approved-stock-digest-file",
                str(rig.approved_digest_file),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(RAIZ_REPO),
        )
        assert proc.returncode == 1
        salida_cli = json.loads(proc.stdout)
        assert salida_cli["listo_para_p7"] is False

    def test_19d_inspector_y_preflight_aislamiento_stock_fail_closed_y_autoridad(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """P1: El inspector expone y transmite stock_source_root al preflight.
        - Si hay hardlink cruzado entre runtime manual y Stock (con nombres diferentes), bloquea.
        - Si no se suministra la evidencia requerida (stock_source_root ausente bajo STOCK_INDEPENDENT_TRANSITION), bloquea.
        - Si se suministra una ruta arbitraria sin autoridad A1, bloquea.
        - Tanto API como CLI emiten veredictos consistentes.
        """
        rig = RigStockManual(tmp_path, steam_activo=True)
        cfg_toml = tmp_path / "config.toml"
        escaped_manual = str(rig.manual_runtime).replace("\\", "\\\\")
        cfg_toml.write_text(f'skyrim_path = "{escaped_manual}"\n', encoding="utf-8")

        monkeypatch.setattr(inspector, "default_runtime_probes", rig.probes_globales_activos)
        monkeypatch.setenv("SKYRIM_PATH", str(rig.manual_runtime))

        # 1. Hardlink cruzado a Stock bajo nombres diferentes:
        # Stock tiene 'fuente_stock_alfa.dll', manual tiene 'manual_beta.dll'
        stock_file = rig.stock_source / "fuente_stock_alfa.dll"
        stock_file.write_bytes(b"CONTENIDO-STOCK-ALFA")
        manual_hl = rig.manual_runtime / "manual_beta.dll"
        os.link(stock_file, manual_hl)

        # 1a: Invocación por API del inspector pasando stock_source_root:
        # Debe detectar el hardlink cruzado con Stock y preflight_ready debe ser False
        informe_hl, _ = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=True,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=rig.stock_source,
        )
        assert informe_hl["preflight_ready"] is False
        assert informe_hl["preflight_verdict"] == BinderPreflightVerdict.BLOCKED.value
        hl_findings = [h for h in informe_hl["hallazgos"] if str(h.get("clave")).startswith("preflight.")]
        assert any(
            "manual_beta.dll" in str(h.get("detalle", "")) and "fuente_stock_alfa.dll" in str(h.get("detalle", ""))
            for h in hl_findings
        ), f"No se reportó la colisión física de inodos entre manual y Stock: {hl_findings}"

        # 1b: Invocación por CLI (--stock-source):
        argv = [
            "--storage-root",
            str(rig.storage),
            "--mo2-instance",
            str(rig.mo2_active_global),
            "--config",
            str(cfg_toml),
            "--managed-source",
            str(rig.managed_source),
            "--clone-id",
            rig.clone_id,
            "--transition-policy",
            BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION.value,
            "--outgoing-manual-runtime",
            str(rig.manual_runtime),
            "--approved-stock-digest-file",
            str(rig.approved_digest_file),
            "--stock-source",
            str(rig.stock_source),
            "--mostrar-rutas",
        ]
        ret = inspector.main(argv)
        assert ret == 1
        capturado = capsys.readouterr()
        salida_json = json.loads(capturado.out)
        assert salida_json["preflight_ready"] is False
        assert salida_json["preflight_verdict"] == BinderPreflightVerdict.BLOCKED.value

        # 2. Sin stock_source_root bajo STOCK_INDEPENDENT_TRANSITION:
        # Evidencia insuficiente para verificar aislamiento -> no debe producir READY
        informe_sin_stock, _ = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=True,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=None,
        )
        assert informe_sin_stock["preflight_ready"] is False
        assert informe_sin_stock["preflight_verdict"] == BinderPreflightVerdict.BLOCKED.value

        # 3. Ruta arbitraria como stock_source_root (sin autoridad de referencia Stock):
        falsa_stock = tmp_path / "falsa_stock_arbitraria"
        falsa_stock.mkdir()
        (falsa_stock / "SkyrimSE.exe").write_bytes(b"dummy")
        informe_falsa, _ = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=True,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=falsa_stock,
        )
        assert informe_falsa["preflight_ready"] is False
        assert informe_falsa["preflight_verdict"] == BinderPreflightVerdict.BLOCKED.value

        # 4. Eliminando el hardlink cruzado, con la fuente Stock legítima y autorizada -> preflight_ready=True
        manual_hl.unlink()
        stock_file.unlink()
        informe_limpio, _ = inspector.construir_informe(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            config_path=cfg_toml,
            managed_source_root=rig.managed_source,
            clone_id=rig.clone_id,
            mostrar_rutas=True,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            stock_source_root=rig.stock_source,
        )
        assert informe_limpio["preflight_ready"] is True
        assert informe_limpio["preflight_verdict"] == BinderPreflightVerdict.READY.value
        assert informe_limpio["stock_source"] == str(rig.stock_source)

        # 5. Retrocompatibilidad: bajo STRICT_PROVIDER_IDLE sin stock_source_root no se exige Stock
        rig_idle = RigStockManual(tmp_path / "rig_idle", steam_activo=False)
        cfg_idle = tmp_path / "rig_idle" / "config.toml"
        escaped_idle = str(rig_idle.manual_runtime).replace("\\", "\\\\")
        cfg_idle.write_text(f'skyrim_path = "{escaped_idle}"\n', encoding="utf-8")
        monkeypatch.setattr(inspector, "default_runtime_probes", rig_idle.probes_globales_activos)
        monkeypatch.setenv("SKYRIM_PATH", str(rig_idle.manual_runtime))
        informe_retro, _ = inspector.construir_informe(
            storage_root=rig_idle.storage,
            mo2_instance_dir=rig_idle.mo2_active_global,
            config_path=cfg_idle,
            managed_source_root=rig_idle.managed_source,
            clone_id=rig_idle.clone_id,
            mostrar_rutas=True,
            transition_policy=BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
            outgoing_manual_runtime_root=rig_idle.manual_runtime,
            stock_source_root=None,
        )
        assert informe_retro["preflight_ready"] is True
        assert informe_retro["preflight_verdict"] == BinderPreflightVerdict.READY.value

    def test_20_sin_cambios_en_seleccion_de_ejecutables_ni_activacion_productiva(self, tmp_path: pathlib.Path) -> None:
        """Test 20: EMISION_PRODUCTIVA_HABILITADA permanece False, _NOMBRES_LANZADOR intacto y
        entradas de lanzadores no migradas siguen bloqueando sin atajos artificiales.
        """
        from sky_claw.local.frozen_runtime.effective_runtime.oracle import _NOMBRES_LANZADOR

        assert approval_module.EMISION_PRODUCTIVA_HABILITADA is False
        esperados_lanzador = frozenset(
            {
                "skyrimse.exe",
                "skyrim.exe",
                "skyrimvr.exe",
                "skse64_loader.exe",
                "skse_loader.exe",
                "sksevr_loader.exe",
                "skse64.exe",
            }
        )
        assert esperados_lanzador == _NOMBRES_LANZADOR

        # Un skse64_loader.exe registrado en otro directorio sigue devolviendo BLOCKED
        rig = RigStockManual(tmp_path, steam_activo=True)
        otro_loader_dir = tmp_path / "OtroLoader"
        otro_loader_dir.mkdir()
        _escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=rig.manual_runtime,
            base_directory=rig.mo2_base_data,
            ejecutables=[
                ("SKSE Malo", f"{otro_loader_dir.as_posix()}/skse64_loader.exe", None),
            ],
        )
        evaluacion = evaluar_binder_preflight(
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
        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons
