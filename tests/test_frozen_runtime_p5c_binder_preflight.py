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
import os
import pathlib
import shutil
import subprocess
from typing import Any

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
from sky_claw.local.runtime_vault.runtime_observation import RuntimeObservationError
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


def _enlace_directorio(destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Enlace de directorio: junction en Windows, symlink en POSIX."""
    if os.name == "nt":
        creado = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(enlace), str(destino)],
            capture_output=True,
        )
        if creado.returncode != 0:
            pytest.skip("sin permiso para crear junctions en esta máquina")
    else:
        try:
            enlace.symlink_to(destino, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlinks no disponibles en este entorno: {exc}")


def _crear_managed_source_sintetica(
    raiz_steam: pathlib.Path,
    *,
    appid: str = "489830",
    version: str = "1.6.1170.0",
    buildid: str = "13189953",
    state_flags: str = "4",
) -> pathlib.Path:
    steamapps = raiz_steam / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    common.mkdir(parents=True, exist_ok=True)
    _crear_arbol_juego(common, version=version)
    manifest = steamapps / f"appmanifest_{appid}.acf"
    manifest.write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"buildid"\t\t"{buildid}"\n\t"StateFlags"\t\t"{state_flags}"\n}}\n',
        encoding="utf-8",
    )
    return common


def _crear_mo2_instance_sintetica(
    instance_dir: pathlib.Path,
    *,
    game_path: str,
    custom_executables: list[tuple[str, str]] | None = None,
) -> pathlib.Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini = instance_dir / "modorganizer.ini"
    escaped = game_path.replace("\\", "\\\\")
    lineas = [
        "[General]",
        f"gamePath=@ByteArray({escaped})",
        "gameName=SkyrimSE",
        "",
    ]
    if custom_executables:
        lineas.append("[customExecutables]")
        for idx, (title, binary) in enumerate(custom_executables, start=1):
            lineas.append(f"{idx}\\title={title}")
            lineas.append(f"{idx}\\binary={binary}")
        lineas.append("")
    lineas.append("[Settings]")
    ini.write_text("\n".join(lineas) + "\n", encoding="utf-8")
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

        _enlace_directorio(real_target, clone_root)

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

        _enlace_directorio(real_mo2, junction_mo2)

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

    # -------------------------------------------------------------------------
    # Hallazgos Auditoría P5-C0: P1-A, P1-B, P2-A, P2-B, P2-C, P2-D
    # -------------------------------------------------------------------------

    def test_p1a_gamepath_tercer_directorio_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P1-A: mo2 gamePath apuntando a un 3er directorio no admitido genera CONFIG_DIVERGENCE_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        tercer_path = tmp_path / "otro_skyrim_desconocido"
        _crear_mo2_instance_sintetica(mo2, game_path=str(tercer_path))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_p1a_config_skyrim_path_tercer_arbol_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P1-A: Config.skyrim_path apuntando a un 3er árbol produce CONFIG_DIVERGENCE_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        tercer_arbol = tmp_path / "arbol_externo"

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(tercer_arbol),
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_p1a_config_skyrim_path_omitida_cuando_es_obligatoria_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P1-A: Config de Sky-Claw omitida cuando exigir_config_skyrim_path=True produce CONFIG_DIVERGENCE_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=None,
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_p1a_situacion_legitima_pre_switch_con_mo2_en_managed_source_pasa(self, tmp_path: pathlib.Path) -> None:
        """P1-A: Estado legítimo pre-switch: MO2 apuntando a Managed Source y Config coherente con Managed Source."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED in evaluacion.reasons

    def test_p1a_situacion_legitima_post_switch_con_mo2_en_clone_pasa(self, tmp_path: pathlib.Path) -> None:
        """P1-A: Estado legítimo post-switch (idempotente): MO2 apuntando al Clone y Config coherente con Clone."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(clone_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED in evaluacion.reasons

    def test_p1b_custom_executables_divergente_hacia_steam_o_tercero_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P1-B: customExecutables registrando lanzador de juego fuera de gamePath produce CONFIG_DIVERGENCE_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        # gamePath es clone_root, pero customExecutables registra skse64_loader apuntando a Steam
        launcher_divergente = ms_root / "skse64_loader.exe"
        _crear_mo2_instance_sintetica(
            mo2,
            game_path=str(clone_root),
            custom_executables=[("SKSE", str(launcher_divergente))],
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_p1b_custom_executables_coherente_con_gamepath_pasa(self, tmp_path: pathlib.Path) -> None:
        """P1-B: customExecutables con lanzador dentro de gamePath pasa el preflight."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        launcher_coherente = clone_root / "skse64_loader.exe"
        _crear_mo2_instance_sintetica(
            mo2,
            game_path=str(clone_root),
            custom_executables=[("SKSE", str(launcher_coherente))],
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True

    def test_p2a_steam_manifest_corrupto_o_ilegible_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P2-A: appmanifest_489830.acf corrupto / malformado produce MANAGED_SOURCE_INVALID."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        manifest = steam / "steamapps" / "appmanifest_489830.acf"
        manifest.write_text('"AppState" { corrupt vdf no closing brace', encoding="utf-8")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_p2a_steam_manifest_sin_buildid_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P2-A: appmanifest_489830.acf sin buildid válido produce MANAGED_SOURCE_INVALID."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam, buildid="")
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_p2a_steam_manifest_con_descarga_activa_o_inestable_rechaza(self, tmp_path: pathlib.Path) -> None:
        """P2-A: Steam con StateFlags!=4 (descarga o actualización en curso) produce MANAGED_SOURCE_INVALID."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam, state_flags="1026")
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_p2b_modorganizer_ini_es_reparse_point_rechaza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-B: modorganizer.ini detectado como reparse point / symlink produce REPARSE_POINT_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        ini_path = mo2 / "modorganizer.ini"
        import sky_claw.local.frozen_runtime.binder_preflight as bp_mod

        real_primer_reparse = bp_mod._primer_reparse

        def _mock_reparse(p: pathlib.Path) -> tuple[str, str | None]:
            if p == ini_path:
                return bp_mod._REPARSE_HAY, f"symlink_file en '{ini_path}'"
            return real_primer_reparse(p)

        monkeypatch.setattr(bp_mod, "_primer_reparse", _mock_reparse)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.REPARSE_POINT_DETECTED in evaluacion.reasons

    def test_p2b_modorganizer_ini_inspeccion_indeterminada_rechaza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-B: Inspección de reparse point en modorganizer.ini que falla con OSError produce REPARSE_INSPECTION_FAILED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        ini_path = mo2 / "modorganizer.ini"
        import sky_claw.local.frozen_runtime.binder_preflight as bp_mod

        real_primer_reparse = bp_mod._primer_reparse

        def _mock_reparse(p: pathlib.Path) -> tuple[str, str | None]:
            if p == ini_path:
                return bp_mod._REPARSE_INDETERMINADO, "error de acceso al inspeccionar ini"
            return real_primer_reparse(p)

        monkeypatch.setattr(bp_mod, "_primer_reparse", _mock_reparse)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.REPARSE_INSPECTION_FAILED in evaluacion.reasons

    def test_p2b_modorganizer_ini_mutado_concurrentemente_rechaza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-B: modorganizer.ini modificado durante la observación (TOCTOU) produce MO2_INI_UNREADABLE."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        import sky_claw.local.frozen_runtime.binder_preflight as bp_mod

        real_leer = bp_mod.leer_game_path_de_mo2

        def _mutating_leer(p: pathlib.Path) -> Any:
            res = real_leer(p)
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 10))
            return res

        monkeypatch.setattr(bp_mod, "leer_game_path_de_mo2", _mutating_leer)

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

    def test_p2c_is_ready_rechaza_lista_parcial_o_vacia(self) -> None:
        """P2-C: is_ready es False ante checks vacíos o lista parcial, aunque verdict sea READY."""
        check_pass = PreflightCheckResult(
            check=PreflightCheckCode.PATH_SAFETY,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
        )

        a_vacio = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=(),
        )
        assert a_vacio.is_ready is False

        a_parcial = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=(check_pass,),
        )
        assert a_parcial.is_ready is False

    def test_p2c_is_ready_rechaza_checks_duplicados(self) -> None:
        """P2-C: is_ready es False si hay checks duplicados aunque la cantidad sea 10."""
        checks_duplicados = [
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            ),
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            ),
        ]
        otros_codigos = [
            c
            for c in PreflightCheckCode
            if c not in (PreflightCheckCode.PATH_SAFETY, PreflightCheckCode.CONFIG_COHERENCE)
        ]
        for cod in otros_codigos:
            checks_duplicados.append(
                PreflightCheckResult(
                    check=cod, status=PreflightCheckStatus.PASS, reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED
                )
            )

        assert len(checks_duplicados) == 10
        a_dup = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=tuple(checks_duplicados),
        )
        assert a_dup.is_ready is False

    def test_p2c_is_ready_pasa_con_exactamente_los_diez_checks_obligatorios(self) -> None:
        """P2-C: is_ready es True únicamente con los 10 checks obligatorios únicos y PASS."""
        checks_completos = tuple(
            PreflightCheckResult(
                check=cod, status=PreflightCheckStatus.PASS, reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED
            )
            for cod in PreflightCheckCode
        )
        assert len(checks_completos) == 10
        a_ok = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=checks_completos,
        )
        assert a_ok.is_ready is True

    def test_p2d_active_json_corrupto_rechaza_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """P2-D: active.json con JSON malformado / corrupto produce CLONE_RECORD_CORRUPT fail-closed."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        active_file = active_state_path(storage)
        active_file.write_text("{ corrupt json not terminated", encoding="utf-8")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=None,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_RECORD_CORRUPT in evaluacion.reasons

    def test_p2d_active_json_schema_invalido_rechaza_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """P2-D: active.json con schema inválido produce CLONE_RECORD_SCHEMA_INVALID fail-closed."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        active_file = active_state_path(storage)
        write_json_atomic(active_file, {"schema_version": 99, "updated_at_ns": 100})

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=None,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID in evaluacion.reasons

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

        # Caso 1: READY con todos los 10 checks PASS -> is_ready True
        checks_completos = tuple(
            PreflightCheckResult(
                check=cod,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            )
            for cod in PreflightCheckCode
        )
        a1 = BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.READY,
            reasons=(PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,),
            checks=checks_completos,
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

    # -------------------------------------------------------------------------
    # Escenarios Final Coherence Gate (A, B, C, D)
    # -------------------------------------------------------------------------

    def test_coherencia_cruzada_config_clone_mo2_managed_source_bloquea(self, tmp_path: pathlib.Path) -> None:
        """A.1: Config.skyrim_path = Clone, MO2 gamePath = Managed Source diverge y bloquea."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(clone_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_coherencia_cruzada_config_managed_source_mo2_clone_bloquea(self, tmp_path: pathlib.Path) -> None:
        """A.2: Config.skyrim_path = Managed Source, MO2 gamePath = Clone diverge y bloquea."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_coherencia_cruzada_ambas_en_managed_source_ready_pre_switch(self, tmp_path: pathlib.Path) -> None:
        """A.3: Ambas superficies en Managed Source: READY para estado legítimo pre-switch."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED in evaluacion.reasons

    def test_coherencia_cruzada_ambas_en_clone_ready_post_switch(self, tmp_path: pathlib.Path) -> None:
        """A.4: Ambas superficies en Clone: READY para estado legítimo post-switch."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(clone_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(clone_root),
            exigir_config_skyrim_path=True,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.READY
        assert evaluacion.is_ready is True
        assert PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED in evaluacion.reasons

    def test_environ_skyrim_path_tercer_arbol_bloquea(self, tmp_path: pathlib.Path) -> None:
        """B.1: SKYRIM_PATH en environ apuntando a un tercer árbol bloquea con CONFIG_DIVERGENCE_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))
        tercer_arbol = tmp_path / "otro_skyrim"
        tercer_arbol.mkdir(parents=True, exist_ok=True)

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            environ={"SKYRIM_PATH": str(tercer_arbol)},
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_environ_skyrim_path_divergente_de_mo2_bloquea(self, tmp_path: pathlib.Path) -> None:
        """B.2: SKYRIM_PATH en environ apuntando al Clone mientras MO2 apunta a Managed Source diverge y bloquea."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            environ={"SKYRIM_PATH": str(clone_root)},
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED in evaluacion.reasons

    def test_environ_skyrim_path_con_alias_83_bloquea(self, tmp_path: pathlib.Path) -> None:
        """B.3: SKYRIM_PATH en environ con alias 8.3 bloquea con SHORT_NAME_ALIAS_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path=str(ms_root),
            environ={"SKYRIM_PATH": "C:\\PROGRA~1\\Skyrim"},
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED in evaluacion.reasons

    def test_config_skyrim_path_con_alias_83_bloquea(self, tmp_path: pathlib.Path) -> None:
        """P1-A.alias: Config.skyrim_path con alias 8.3 bloquea con SHORT_NAME_ALIAS_DETECTED."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            config_skyrim_path="C:\\PROGRA~1\\Skyrim",
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED in evaluacion.reasons

    def test_steam_downloading_activo_rechaza_preflight(self, tmp_path: pathlib.Path) -> None:
        """C: Steam con archivos en steamapps/downloading/489830 produce MANAGED_SOURCE_INVALID por actividad en curso."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam, state_flags="4")
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # Crear archivos activos en downloading/489830
        downloading_dir = steam / "steamapps" / "downloading" / "489830"
        downloading_dir.mkdir(parents=True, exist_ok=True)
        (downloading_dir / "patch.chunk").write_bytes(b"downloading payload")

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.MANAGED_SOURCE_INVALID in evaluacion.reasons

    def test_runtime_compatibility_no_enmascara_errores_de_programacion(self, tmp_path: pathlib.Path) -> None:
        """D.1: Errores de programación (TypeError) en compatibilidad no se capturan como COMPATIBILITY_UNKNOWN."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        def _falla_con_type_error(_r: pathlib.Path) -> CompatibilityResult:
            raise TypeError("bug de programacion")

        probes_bug = RuntimeProbes(
            identity=lambda _r: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
            compatibility=_falla_con_type_error,
            processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        )

        with pytest.raises(TypeError, match="bug de programacion"):
            evaluar_binder_preflight(
                storage_root=storage,
                mo2_instance_dir=mo2,
                clone_id=clone_record.clone_id,
                managed_source_root=ms_root,
                probes=probes_bug,
            )

    def test_runtime_compatibility_falla_cerrado_ante_errores_de_observacion_tipados(
        self, tmp_path: pathlib.Path
    ) -> None:
        """D.2: Errores tipados de observación (RuntimeObservationError / OSError) fallan cerrado con COMPATIBILITY_UNKNOWN."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        gen_id = _crear_generation_valida(storage)
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_id, managed_source_root=ms_root)
        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        def _falla_con_obs_error(_r: pathlib.Path) -> CompatibilityResult:
            raise RuntimeObservationError("pe ilegible")

        probes_obs_err = RuntimeProbes(
            identity=lambda _r: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
            compatibility=_falla_con_obs_error,
            processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        )

        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=clone_record.clone_id,
            managed_source_root=ms_root,
            probes=probes_obs_err,
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.COMPATIBILITY_UNKNOWN in evaluacion.reasons

    def test_modorganizer_ini_error_de_filesystem_en_is_file_rechaza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError / PermissionError al evaluar ini_path.is_file() produce rechazo con MO2_INI_UNREADABLE."""
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
        ini.write_text(f"[General]\ngamePath=@ByteArray({escaped})\n", encoding="utf-8")

        orig_is_file = pathlib.Path.is_file

        def _is_file_falla(self_path: pathlib.Path) -> bool:
            if self_path.name == "modorganizer.ini":
                raise PermissionError("Acceso denegado por permisos de filesystem")
            return orig_is_file(self_path)

        monkeypatch.setattr(pathlib.Path, "is_file", _is_file_falla)

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

    def test_fallback_active_json_con_generacion_divergente_del_clone_rechaza(self, tmp_path: pathlib.Path) -> None:
        """Fallback sin clone_id: si active.json selecciona generation A pero el clone pertenece a B, rechaza."""
        storage = tmp_path / "storage"
        initialize_frozen_runtime_storage(storage)
        steam = tmp_path / "steam"
        ms_root = _crear_managed_source_sintetica(steam)
        # Dos generations válidas distintas A y B
        gen_a = _crear_generation_valida(storage, digest_prefix="gen_a")
        gen_b = _crear_generation_valida(storage, digest_prefix="gen_b")
        assert gen_a != gen_b

        # Clone perteneciente físicamente a generation B
        clone_root = tmp_path / "clones" / "c1"
        clone_record = _crear_clone_sintetico_valido(storage, clone_root, gen_b, managed_source_root=ms_root)

        # active.json asocia desired_generation_id=gen_a con desired_clone_id=clone_record.clone_id
        save_frozen_runtime_state(
            active_state_path(storage),
            FrozenRuntimeState(
                schema_version=2,
                desired_active_generation=gen_a,
                desired_clone_id=clone_record.clone_id,
                updated_at_ns=1000,
            ),
        )

        mo2 = tmp_path / "mo2_instance"
        _crear_mo2_instance_sintetica(mo2, game_path=str(ms_root))

        # clone_id se omite para activar el fallback desde active.json
        evaluacion = evaluar_binder_preflight(
            storage_root=storage,
            mo2_instance_dir=mo2,
            clone_id=None,
            managed_source_root=ms_root,
            probes=_probes_limpios(),
        )

        assert evaluacion.verdict is BinderPreflightVerdict.BLOCKED
        assert evaluacion.is_ready is False
        assert PreflightReasonCode.GENERATION_INVALID in evaluacion.reasons
