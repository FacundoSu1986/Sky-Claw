"""Pruebas del Preflight de integración read-only para el binder de MO2 (P5-C0).

Tracker: #672
ADR 0012 §13, §29.4
ADR 0015 §12, §13

Contratos auditados:
- Managed Source identificada mediante evidencia confiable de Steam.
- Clone físico independiente y en estado PROVISIONED.
- Generation de origen en estado estrictamente VALID (no DRIFTED ni ausente).
- Runtime y SKSE compatibles (UNKNOWN != COMPATIBLE).
- Rutas sin reparse points, sin alias 8.3 y sin colisiones ni contención indebida.
- Concurrencia de procesos: tabla legible, sin Skyrim/SKSE activos, sin MO2 activo.
- Configuración de MO2 válida y decodificable según formato Qt.
- Ausencia de mutaciones (paquete y preflight 100% read-only).
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess

import pytest

from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightAssessment,
    BinderPreflightVerdict,
    PreflightCheckCode,
    PreflightCheckResult,
    PreflightCheckStatus,
    PreflightReasonCode,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    registrar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    CompatibilityResult,
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
    RuntimeCompatibility,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.generations import (
    registrar_generation_metadata,
)
from sky_claw.local.frozen_runtime.models import (
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.state import (
    save_frozen_runtime_state,
    write_json_atomic,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    initialize_frozen_runtime_storage,
)
from sky_claw.local.frozen_runtime.storage_models import (
    FrozenRuntimeState,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

_RAIZ_WORKTREE = pathlib.Path(__file__).resolve().parent.parent


def _probes_limpios(
    *,
    version: str = "1.6.1170.0",
    compatibilidad: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
    procesos: tuple[LiveProcessEvidence, ...] = (),
    indeterminate_proc: bool = False,
) -> RuntimeProbes:
    return RuntimeProbes(
        identity=lambda _r: RuntimeIdentity(game_key="skyrimse", game_version=version),
        compatibility=lambda _r: CompatibilityResult(
            state=compatibilidad,
            reason=f"test: {compatibilidad}",
            game_version=version,
        ),
        processes=lambda: LiveProcessSnapshot(
            processes=procesos,
            indeterminate=indeterminate_proc,
        ),
    )


def _crear_arbol_juego(
    ruta: pathlib.Path,
    *,
    version: str = "1.6.1170.0",
    skse_loader: bool = True,
    skse_dll: bool = True,
) -> pathlib.Path:
    ruta.mkdir(parents=True, exist_ok=True)
    (ruta / "SkyrimSE.exe").write_bytes(f"SkyrimSE-{version}".encode())
    if skse_loader:
        (ruta / "skse64_loader.exe").write_bytes(b"loader")
    if skse_dll:
        (ruta / "skse64_1_6_1170.dll").write_bytes(b"dll")
    return ruta


def _crear_managed_source_sintetica(
    raiz_steam: pathlib.Path,
    *,
    appid: str = "489830",
    version: str = "1.6.1170.0",
) -> pathlib.Path:
    steamapps = raiz_steam / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    common.mkdir(parents=True, exist_ok=True)
    _crear_arbol_juego(common, version=version)
    manifest = steamapps / f"appmanifest_{appid}.acf"
    manifest.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"buildid"\t\t"13189953"\n}}\n',
        encoding="utf-8",
    )
    return common


def _crear_mo2_instance_sintetica(
    instance_dir: pathlib.Path,
    *,
    game_path: str,
) -> pathlib.Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini = instance_dir / "modorganizer.ini"
    escaped = game_path.replace("\\", "\\\\")
    ini.write_text(
        f"[General]\ngamePath=@ByteArray({escaped})\ngameName=SkyrimSE\n\n[Settings]\n",
        encoding="utf-8",
    )
    return instance_dir


def _crear_generation_valida(
    storage_root: pathlib.Path,
    *,
    version: str = "1.6.1170.0",
    digest_prefix: str = "gen_p5c_base",
) -> str:
    staging = storage_root / f"_staging_gen_{digest_prefix}"
    if staging.exists():
        shutil.rmtree(staging)
    _crear_arbol_juego(staging, version=version)
    (staging / f"extra_{digest_prefix}.dat").write_bytes(digest_prefix.encode())
    files = inventory_tree(staging)
    tree_digest = tree_digest_from_files(files)
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
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version=version),
        tree_digest=tree_digest,
    )
    meta = registrar_generation_metadata(storage_root, evidencia)
    gen_dir = storage_root / "versions" / meta.generation_id
    if gen_dir.exists():
        shutil.rmtree(gen_dir)
    shutil.copytree(staging, gen_dir)
    shutil.rmtree(staging)
    return meta.generation_id


def _escribir_record_directo(
    storage_root: pathlib.Path,
    record: RuntimeCloneRecord,
) -> RuntimeCloneRecord:
    clones_dir = storage_root / "state" / "clones"
    clones_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": record.schema_version,
        "clone_id": record.clone_id,
        "source_generation_id": record.source_generation_id,
        "lifecycle": record.lifecycle.value,
        "intended_runtime_setup_id": record.intended_runtime_setup_id,
        "verified_runtime_setup_id": record.verified_runtime_setup_id,
        "root_path": record.root_path,
        "admitted_role": record.admitted_role,
        "created_at_ns": record.created_at_ns,
    }
    write_json_atomic(clones_dir / f"{record.clone_id}.json", payload)
    return record


def _crear_clone_sintetico_valido(
    storage_root: pathlib.Path,
    clone_root: pathlib.Path,
    generation_id: str,
    *,
    clone_id: str = "clone-p5c-valido",
    lifecycle: RuntimeCloneLifecycle = RuntimeCloneLifecycle.PROVISIONED,
    version: str = "1.6.1170.0",
    managed_source_root: pathlib.Path | None = None,
) -> RuntimeCloneRecord:
    gen_dir = storage_root / "versions" / generation_id
    if clone_root.exists():
        shutil.rmtree(clone_root)
    shutil.copytree(gen_dir, clone_root)

    rec = RuntimeCloneRecord(
        schema_version=1,
        clone_id=clone_id,
        source_generation_id=generation_id,
        intended_runtime_setup_id="setup-skse-clean",
        root_path=str(clone_root),
        lifecycle=lifecycle,
        verified_runtime_setup_id="setup-skse-clean" if lifecycle is RuntimeCloneLifecycle.PROVISIONED else None,
        admitted_role="runtime_clone",
        created_at_ns=1000,
    )
    return registrar_runtime_clone_record(
        storage_root,
        rec,
        managed_source_root=managed_source_root,
    )


@pytest.fixture(autouse=True)
def _mock_pe_version_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _mock_read(exe: pathlib.Path) -> str:
        try:
            content = exe.read_bytes()
            if content.startswith(b"SkyrimSE-"):
                return content.split(b"-")[1].decode("ascii", errors="ignore").strip()
        except OSError:
            pass
        return "1.6.1170.0"

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
        _mock_read,
    )


class TestFrozenRuntimeP5CBinderPreflight:
    """Suites AAA completas del preflight de integración P5-C0."""

    def test_camino_nominal_preflight_pasa_exitosamente(self, tmp_path: pathlib.Path) -> None:
        """Todas las precondiciones válidas -> veredicto READY con is_ready True."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(
            storage,
            clone_root,
            gen_id,
            managed_source_root=ms_root,
        )
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        probes = _probes_limpios()

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert evaluacion.reasons == (PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,)
        assert all(c.status is PreflightCheckStatus.PASS for c in evaluacion.checks)
        assert evaluacion.target_clone_id == clone_record.clone_id
        assert evaluacion.target_clone_root == clone_root
        assert evaluacion.source_generation_id == gen_id
        assert evaluacion.managed_source_root == ms_root
        assert evaluacion.mo2_instance_dir == mo2

    def test_managed_source_ausente_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """Si la Managed Source no existe en disco, el preflight rechaza fail-closed."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        ms_inexistente = tmp_path / "no_existe" / "steamapps" / "common" / "Skyrim Special Edition"

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_inexistente,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND in evaluacion.reasons

    def test_managed_source_sin_manifest_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """Copia de Skyrim sin manifest de Steam -> rechaza como fuente sin evidencia."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        falsa_steam = tmp_path / "fake_steam" / "steamapps" / "common" / "Skyrim Special Edition"
        _crear_arbol_juego(falsa_steam)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=falsa_steam,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_managed_source_ambigua_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """Dos libraries de Steam con Skyrim SE -> fail-closed sin first-match-wins."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        steam1 = tmp_path / "steam1"
        steam2 = tmp_path / "steam2"
        _crear_managed_source_sintetica(steam1)
        _crear_managed_source_sintetica(steam2)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=None,
            steam_roots=(str(steam1), str(steam2)),
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_AMBIGUOUS in evaluacion.reasons

    def test_rutas_coincidentes_clone_y_managed_source_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Un clone que apunta exactamente a la Managed Source produce rechazo inmediato."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # Registrar clone apuntando ilícitamente a la Managed Source
        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-en-steam",
                source_generation_id=gen_id,
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(ms_root),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert (
            PreflightReasonCode.PATHS_COLLIDE in evaluacion.reasons
            or PreflightReasonCode.PATH_CONTAINMENT_VIOLATION in evaluacion.reasons
        )

    def test_clone_dentro_de_steamapps_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Un clone dentro de steamapps/common de Steam viola aislamiento físico."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        sub_clone = steam / "steamapps" / "common" / "Skyrim Clone"
        _crear_arbol_juego(sub_clone)
        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-sub-steam",
                source_generation_id=gen_id,
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(sub_clone),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert (
            PreflightReasonCode.PATH_CONTAINMENT_VIOLATION in evaluacion.reasons
            or PreflightReasonCode.CLONE_ADMISSION_REJECTED in evaluacion.reasons
        )

    def test_clone_dentro_de_versions_rechaza(self, tmp_path: pathlib.Path) -> None:
        """SFR-19: nunca se ejecuta desde una Generation ni dentro de versions/."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        clone_en_version = storage / "versions" / gen_id
        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-en-versions",
                source_generation_id=gen_id,
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(clone_en_version),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert (
            PreflightReasonCode.PATH_CONTAINMENT_VIOLATION in evaluacion.reasons
            or PreflightReasonCode.CLONE_ADMISSION_REJECTED in evaluacion.reasons
        )

    def test_generation_drifted_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """Si la Generation de origen sufrió alteraciones o drift, el preflight rechaza."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # Introducir drift en la generation
        gen_exe = storage / "versions" / gen_id / "SkyrimSE.exe"
        gen_exe.write_bytes(b"tampered-generation-binary")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.GENERATION_DRIFTED in evaluacion.reasons

    def test_generation_inexistente_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """Si la metadata o directorio de la generation no existe, rechaza fail-closed."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        clone_root = tmp_path / "clones" / "c1"
        _crear_arbol_juego(clone_root)

        # Clone con generation_id fantasma
        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-sin-gen",
                source_generation_id="1.6.1170__000000000000",
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(clone_root),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.GENERATION_NOT_FOUND in evaluacion.reasons

    def test_clone_lifecycle_no_provisioned_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Un clone en estado CREATED, PROVISIONING o INVALID es rechazado."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        record_created = _crear_clone_sintetico_valido(
            storage,
            clone_root,
            gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            managed_source_root=ms_root,
        )
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record_created.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_LIFECYCLE_NOT_PROVISIONED in evaluacion.reasons

    def test_clone_record_inexistente_rechaza(self, tmp_path: pathlib.Path) -> None:
        """clone_id inexistente en state/clones/ produce CLONE_RECORD_MISSING."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id="clone-no-existe",
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_RECORD_MISSING in evaluacion.reasons

    def test_clone_fisico_invalido_por_hash_critico_mismatch(self, tmp_path: pathlib.Path) -> None:
        """Si un binario crítico en el clone difiere del digest registrado, rechaza."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # Corromper SkyrimSE.exe en el clone
        (clone_root / "SkyrimSE.exe").write_bytes(b"SkyrimSE-corrupto")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID in evaluacion.reasons

    def test_clone_con_reparse_point_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Junction o reparse point en el root del clone produce rechazo de seguridad."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        real_target = tmp_path / "real_target"
        _crear_arbol_juego(real_target)
        clone_root = tmp_path / "clones" / "junction_clone"
        clone_root.parent.mkdir(parents=True, exist_ok=True)

        creado = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(clone_root), str(real_target)],
            capture_output=True,
        )
        if creado.returncode != 0:
            pytest.skip("sin permiso para crear junctions en esta máquina")

        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-junction",
                source_generation_id=gen_id,
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(clone_root),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.REPARSE_POINT_DETECTED in evaluacion.reasons

    def test_mo2_instance_con_reparse_point_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Junction en mo2_instance_dir produce REPARSE_POINT_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)

        real_mo2 = tmp_path / "real_mo2"
        _crear_mo2_instance_sintetica(real_mo2, game_path=str(ms_root))
        junction_mo2 = tmp_path / "mo2_junction"

        creado = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_mo2), str(real_mo2)],
            capture_output=True,
        )
        if creado.returncode != 0:
            pytest.skip("sin permiso para crear junctions en esta máquina")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=junction_mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.REPARSE_POINT_DETECTED in evaluacion.reasons

    def test_proceso_juego_vivo_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """SkyrimSE.exe o loader vivo bloquea cualquier mutación."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        proc_juego = LiveProcessEvidence(
            pid=1234,
            image_path=str(ms_root / "SkyrimSE.exe"),
            kind=LiveProcessKind.GAME,
            started_at_ns=100,
        )
        probes = _probes_limpios(procesos=(proc_juego,))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.LIVE_GAME_RUNNING in evaluacion.reasons

    def test_proceso_mo2_vivo_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """ModOrganizer.exe vivo bloquea el preflight para evitar clobber on exit de Qt."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        proc_mo2 = LiveProcessEvidence(
            pid=5678,
            image_path=str(mo2 / "ModOrganizer.exe"),
            kind=LiveProcessKind.MO2,
            started_at_ns=100,
        )
        probes = _probes_limpios(procesos=(proc_mo2,))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.LIVE_MO2_RUNNING in evaluacion.reasons

    def test_tabla_procesos_ilegible_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """indeterminate=True en tabla de procesos -> PROCESS_TABLE_UNREADABLE."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        probes = _probes_limpios(indeterminate_proc=True)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.PROCESS_TABLE_UNREADABLE in evaluacion.reasons

    def test_compatibilidad_skse_incompatible_rechaza(self, tmp_path: pathlib.Path) -> None:
        """SKSE incompatible produce SKSE_INCOMPATIBLE."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        probes = _probes_limpios(compatibilidad=RuntimeCompatibility.INCOMPATIBLE)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.SKSE_INCOMPATIBLE in evaluacion.reasons

    def test_compatibilidad_skse_unknown_rechaza(self, tmp_path: pathlib.Path) -> None:
        """UNKNOWN != COMPATIBLE: si compatibilidad no se demuestra, rechaza."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        probes = _probes_limpios(compatibilidad=RuntimeCompatibility.UNKNOWN)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.COMPATIBILITY_UNKNOWN in evaluacion.reasons

    def test_modorganizer_ini_ausente_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Si falta modorganizer.ini en la instancia MO2, rechaza fail-closed."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance_vacia"
        mo2.mkdir(parents=True, exist_ok=True)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MO2_INI_MISSING in evaluacion.reasons

    def test_gamepath_en_seccion_incorrecta_rechaza(self, tmp_path: pathlib.Path) -> None:
        """gamePath en [Settings] en vez de [General] es ilegible para MO2."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        mo2.mkdir(parents=True, exist_ok=True)
        ini = mo2 / "modorganizer.ini"
        escaped = str(ms_root).replace("\\", "\\\\")
        ini.write_text(f"[Settings]\ngamePath=@ByteArray({escaped})\n", encoding="utf-8")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MO2_INI_UNREADABLE in evaluacion.reasons

    def test_ruta_con_alias_83_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Ruta con componentes de nombre corto 8.3 de Windows produce rechazo."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "PROGRA~1" / "c1"
        _crear_arbol_juego(clone_root)

        # Registro con ruta corta
        record = _escribir_record_directo(
            storage,
            RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-alias",
                source_generation_id=gen_id,
                intended_runtime_setup_id="setup-skse",
                verified_runtime_setup_id="setup-skse",
                root_path=str(clone_root),
                lifecycle=RuntimeCloneLifecycle.PROVISIONED,
                admitted_role="runtime_clone",
                created_at_ns=1000,
            ),
        )
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED in evaluacion.reasons

    def test_resolucion_automatica_de_clone_desde_active_json(self, tmp_path: pathlib.Path) -> None:
        """Si clone_id es None, se resuelve automáticamente del active.json v2."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # Configurar active.json v2 con este clone
        save_frozen_runtime_state(
            active_state_path(storage),
            FrozenRuntimeState(
                schema_version=2,
                updated_at_ns=100,
                desired_active_generation=gen_id,
                desired_clone_id=clone_record.clone_id,
            ),
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=None,  # Resolución automática
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert evaluacion.target_clone_id == clone_record.clone_id

    def test_preflight_es_completamente_read_only_ancla_ast(self) -> None:
        """El preflight es de SOLO LECTURA: se congela por AST.

        Cualquier llamada a métodos de escritura en disco en binder_preflight.py
        hace fallar este test de forma inmediata.
        """
        modulo_path = _RAIZ_WORKTREE / "sky_claw" / "local" / "frozen_runtime" / "binder_preflight.py"
        arbol = ast.parse(modulo_path.read_text(encoding="utf-8"), filename=str(modulo_path))

        prohibidas_attr = {
            "write_text",
            "write_bytes",
            "mkdir",
            "unlink",
            "rmdir",
            "touch",
            "chmod",
            "rename",
            "replace",
            "symlink_to",
            "hardlink_to",
            "rmtree",
            "remove",
            "makedirs",
            "copy",
            "copy2",
            "copytree",
        }

        errores: list[str] = []
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                if isinstance(func, ast.Attribute) and func.attr in prohibidas_attr:
                    errores.append(f"Llamada prohibida '.{func.attr}' en línea {nodo.lineno}")
                elif isinstance(func, ast.Name) and func.id in prohibidas_attr:
                    errores.append(f"Llamada prohibida direct '{func.id}' en línea {nodo.lineno}")
                elif isinstance(func, ast.Name) and func.id == "open":
                    for kw in nodo.keywords:
                        if (
                            kw.arg == "mode"
                            and isinstance(kw.value, ast.Constant)
                            and any(m in str(kw.value.value) for m in ("w", "a", "x", "+"))
                        ):
                            errores.append(f"open() en modo escritura en línea {nodo.lineno}")
                    if (
                        len(nodo.args) >= 2
                        and isinstance(nodo.args[1], ast.Constant)
                        and any(m in str(nodo.args[1].value) for m in ("w", "a", "x", "+"))
                    ):
                        errores.append(f"open() en modo escritura en línea {nodo.lineno}")

        assert not errores, "\n".join(errores)

    def test_ancla_de_enums_por_igualdad_literal(self) -> None:
        """Los conjuntos de checks, motivos y veredictos están congelados por igualdad literal."""
        checks_esperados = {
            "path_safety",
            "managed_source",
            "clone_record",
            "clone_lifecycle",
            "generation_integrity",
            "clone_physical_integrity",
            "runtime_compatibility",
            "process_concurrency",
            "mo2_configuration",
            "config_coherence",
        }
        assert {c.value for c in PreflightCheckCode} == checks_esperados

        veredictos_esperados = {"ready", "blocked"}
        assert {v.value for v in BinderPreflightVerdict} == veredictos_esperados

        reasons_esperados = {
            "preflight_all_checks_passed",
            "managed_source_not_found",
            "managed_source_invalid",
            "managed_source_ambiguous",
            "clone_id_unspecified",
            "clone_id_invalid",
            "clone_record_missing",
            "clone_record_corrupt",
            "clone_record_schema_invalid",
            "clone_lifecycle_not_provisioned",
            "clone_role_invalid",
            "clone_admission_rejected",
            "generation_not_found",
            "generation_drifted",
            "generation_invalid",
            "clone_physical_integrity_invalid",
            "clone_physical_integrity_indeterminate",
            "reparse_point_detected",
            "reparse_inspection_failed",
            "short_name_alias_detected",
            "path_containment_violation",
            "paths_collide",
            "skse_incompatible",
            "compatibility_unknown",
            "identity_unreadable",
            "process_table_unreadable",
            "live_game_running",
            "live_mo2_running",
            "mo2_ini_missing",
            "mo2_ini_unreadable",
            "mo2_game_path_unreadable",
            "config_divergence_detected",
        }
        assert {r.value for r in PreflightReasonCode} == reasons_esperados

    def test_invariante_is_ready_es_propiedad_del_tipo(self) -> None:
        """is_ready solo puede ser True si verdict es READY y todos los checks son PASS."""
        check_pass = PreflightCheckResult(
            check=PreflightCheckCode.PATH_SAFETY,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
        )
        check_fail = PreflightCheckResult(
            check=PreflightCheckCode.MANAGED_SOURCE,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND,
        )

        # Caso 1: READY con checks PASS -> is_ready True
        a1 = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=(check_pass,),
        )
        assert a1.is_ready is True

        # Caso 2: READY pero con algún check FAIL -> is_ready False (invariante)
        a2 = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND,),
            checks=(check_pass, check_fail),
        )
        assert a2.is_ready is False

        # Caso 3: BLOCKED con checks PASS -> is_ready False
        a3 = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=(PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND,),
            checks=(check_pass,),
        )
        assert a3.is_ready is False
