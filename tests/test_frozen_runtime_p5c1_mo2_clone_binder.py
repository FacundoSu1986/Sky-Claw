"""Pruebas del Binder Transaccional de MO2 (P5-C1).

Tracker: #672
ADR 0012 §13, §29.4, §34.4, §35.6, §36.6, §37.5
ADR 0015 §12, §13

Orden causal de transición:
PERSIST PENDING → BIND EFFECTIVE → VERIFY EFFECTIVE → CAS UPDATE DESIRED → POST VERIFY → FINALIZE

Casos mínimos requeridos:
1. Cambio válido Managed Source → Runtime Clone (transición completa finalizada en journal).
2. MO2 abierto: rechazo sin mutación.
3. Skyrim/SKSE abierto: rechazo sin mutación.
4. Clone no PROVISIONED: rechazo.
5. Generation DRIFTED: rechazo.
6. Runtime incompatible: rechazo.
7. Config y MO2 divergentes: rechazo.
8. Alias 8.3 / reparse point: rechazo.
9. Fallo de snapshot: no mutar.
10. Fallo durante escritura temporal: recuperación verificada.
11. Fallo durante reemplazo: recuperación verificada.
12. Fallo de readback: rollback verificado.
13. Interrupción entre pasos del journal: recuperación determinista.
14. Ejecución repetida: idempotencia sin pérdida de datos.
15. Cancelación: no dejar una transición declarada exitosa.
16. Ningún escritor apunta a Steam o a una Generation tras el cambio.
17. Ningún usuario no autorizado puede aprobar el binding.
18. Ninguna entrada corrupta consigue SUCCESS.
19. Censo enumerativo de escritores del game dir.
"""

from __future__ import annotations

import base64
import dataclasses
import pathlib
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import mo2_ini_escritura, mo2_ini_exclusion
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalOperation,
    ConsentimientoAprobacion,
    PropuestaAprobacion,
    proponer_aprobacion,
)
from sky_claw.local.frozen_runtime.binder_recovery import (
    RecuperacionError,
    _es_escritura_propia_interrumpida,
)
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    registrar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    decode_qt_scalar,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    CompatibilityResult,
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
    Mo2InstanceIdentityObservation,
    Mo2InstanceIdentityState,
    Mo2RegistryState,
    ObservationState,
    RuntimeCompatibility,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.generations import (
    inventory_tree,
    registrar_generation_metadata,
    tree_digest_from_files,
)
from sky_claw.local.frozen_runtime.journal import (
    JournalDisposition,
    SourceActivation,
    cargar_journal_transicion,
    derivar_historial_finalizados,
)
from sky_claw.local.frozen_runtime.mo2_ini_exclusion import Mo2IniExclusionError
from sky_claw.local.frozen_runtime.models import (
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.runtime_clone_binder import (
    CENSO_ESCRITORES_GAME_DIR,
    BinderApprovalError,
    BinderError,
    BinderEscrituraIniError,
    BinderPreflightFailedError,
    BinderQuiescenceError,
    BinderVerificationError,
    codificar_qt_byte_array,
    ejecutar_transicion_binder,
)
from sky_claw.local.frozen_runtime.storage import (
    initialize_frozen_runtime_storage,
)
from sky_claw.local.runtime_vault.models import RuntimeIdentity

# ---------------------------------------------------------------------------
# Fixtures y Helpers de Configuración Sintética
# ---------------------------------------------------------------------------


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
    digest_prefix: str = "gen_p5c1_base",
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


def _crear_clone_sintetico_valido(
    storage_root: pathlib.Path,
    clone_root: pathlib.Path,
    generation_id: str,
    *,
    clone_id: str = "clone-p5c1-valido",
    lifecycle: RuntimeCloneLifecycle = RuntimeCloneLifecycle.PROVISIONED,
    version: str = "1.6.1170.0",
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
    registrar_runtime_clone_record(storage_root, rec)
    return rec


def _crear_config_toml(path: pathlib.Path, skyrim_path: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = skyrim_path.replace("\\", "\\\\")
    path.write_text(f'skyrim_path = "{escaped}"\n', encoding="utf-8")
    return path


def _sonda_mo2_global(ruta: pathlib.Path) -> Mo2InstanceIdentityObservation:
    ini = ruta / "modorganizer.ini"
    try:
        es_archivo = ini.is_file()
    except OSError as exc:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.UNKNOWN,
            instance_dir=str(ruta),
            registry_state=Mo2RegistryState.OBSERVED,
            registry_current_instance=ruta.name,
            detail=f"error de filesystem al acceder a modorganizer.ini: {exc}",
        )
    if not es_archivo:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.INACTIVE,
            instance_dir=str(ruta),
            registry_state=Mo2RegistryState.OBSERVED,
            registry_current_instance=ruta.name,
            detail=f"'{ruta}' carece de modorganizer.ini regular",
        )
    try:
        tiene_artefactos_portables = (ruta / "ModOrganizer.exe").exists() or (ruta / "portable.txt").exists()
    except OSError:
        tiene_artefactos_portables = False
    if tiene_artefactos_portables:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.AMBIGUOUS,
            instance_dir=str(ruta),
            registry_state=Mo2RegistryState.OBSERVED,
            registry_current_instance=ruta.name,
            detail=f"la instancia global '{ruta}' contiene artefactos portables",
        )
    return Mo2InstanceIdentityObservation(
        state=Mo2InstanceIdentityState.ACTIVE_GLOBAL,
        instance_dir=str(ruta),
        registry_state=Mo2RegistryState.OBSERVED,
        registry_current_instance=ruta.name,
        detail=f"instancia global activa verificada contra HKCU CurrentInstance='{ruta.name}'",
    )


_sonda_mo2_portable = _sonda_mo2_global


def _probes_quiescentes(version: str = "1.6.1170.0") -> RuntimeProbes:
    return RuntimeProbes(
        identity=lambda r: RuntimeIdentity(game_key="skyrimse", game_version=version),
        compatibility=lambda r: CompatibilityResult(
            state=RuntimeCompatibility.COMPATIBLE,
            reason="compatible sintético",
        ),
        processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        mo2_instance=_sonda_mo2_global,
    )


def _preparar_aprobacion(
    storage_root: pathlib.Path,
    *,
    target_generation_id: str,
    target_clone_id: str,
    operation: ApprovalOperation = ApprovalOperation.PROMOTION,
    transition_id: str = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90",
) -> tuple[PropuestaAprobacion, ConsentimientoAprobacion]:
    source_act = SourceActivation(
        generation_id=target_generation_id,
        clone_id="clone-source",
        runtime_setup_id="setup-source",
        effective_runtime_evidence="b" * 64,
    )
    propuesta = proponer_aprobacion(
        storage_root,
        operation=operation,
        source_activation=source_act,
        generation_id=target_generation_id,
        clone_id=target_clone_id,
        clone_evidence="d" * 64,
        runtime_setup_id="setup-skse-clean",
        runtime_setup_evidence="e" * 64,
        compatibility_evidence_id="f" * 64,
        candidate_id="cand-p5c1",
        approval_provenance="hitl:user-authorized",
    )
    consentimiento = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture",
    )
    # Emisión bajo lock de la fixture de aprobación
    with participant_lock(storage_root, timeout=10.0) as handle:
        handle.assert_owned()
        approval_module._emitir_aprobacion_bajo_lock(handle, storage_root, propuesta, consentimiento)

    return propuesta, consentimiento


# ---------------------------------------------------------------------------
# Tests TDD
# ---------------------------------------------------------------------------


def test_01_cambio_valido_managed_source_a_runtime_clone(tmp_path: pathlib.Path) -> None:
    """Caso 1: Cambio válido Managed Source → Runtime Clone (flujo completo finalizado en journal)."""
    # Arrange
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    assert initialize_frozen_runtime_storage(storage_root).success

    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    clone_rec = _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id=clone_rec.clone_id
    )

    probes = _probes_quiescentes()
    environ = {"SKYRIM_PATH": str(managed_src)}

    # Act
    resultado = ejecutar_transicion_binder(
        storage_root=storage_root,
        mo2_instance_dir=mo2_dir,
        target_clone_id="clone-1",
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=managed_src,
        config_path=config_file,
        environ=environ,
        probes=probes,
    )

    # Assert
    assert resultado.success is True
    assert resultado.target_clone_id == "clone-1"
    assert resultado.target_clone_root == clone_root.resolve()

    # Verificar que el INI fue mutado con el formato Qt correcto
    evidencia_ini = leer_game_path_de_mo2(mo2_dir / "modorganizer.ini")
    assert evidencia_ini.state is ObservationState.OBSERVED
    assert evidencia_ini.path is not None
    assert pathlib.Path(evidencia_ini.path).resolve() == clone_root.resolve()

    # Verificar que config.toml fue actualizado
    assert f'skyrim_path = "{str(clone_root).replace(chr(92), chr(92) * 2)}"' in config_file.read_text(encoding="utf-8")
    assert environ["SKYRIM_PATH"] == str(clone_root)

    # Verificar que el journal rotó a NONE y se finalizó la transición
    historial = derivar_historial_finalizados(storage_root)
    assert historial.latest is not None
    assert historial.latest.target_clone_id == "clone-1"
    assert historial.latest.target_generation_id == gen_id

    journal_snap = cargar_journal_transicion(storage_root)
    assert journal_snap.disposition is JournalDisposition.NONE


def test_02_mo2_abierto_rechazo_sin_mutacion(tmp_path: pathlib.Path) -> None:
    """Caso 2: MO2 abierto: rechazo por quiescencia sin mutación."""
    # Arrange
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_antes = (mo2_dir / "modorganizer.ini").read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    probes = RuntimeProbes(
        identity=lambda r: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        compatibility=lambda r: CompatibilityResult(
            state=RuntimeCompatibility.COMPATIBLE, reason="compatible sintético"
        ),
        processes=lambda: LiveProcessSnapshot(
            processes=(
                LiveProcessEvidence(
                    kind=LiveProcessKind.MO2,
                    pid=1234,
                    image_path=str(mo2_dir / "ModOrganizer.exe"),
                    started_at_ns=time.time_ns() - 1_000_000,
                ),
            ),
            indeterminate=False,
        ),
        mo2_instance=_sonda_mo2_portable,
    )

    # Act & Assert
    with pytest.raises(BinderQuiescenceError, match="MO2 viv"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=probes,
        )

    # Verificar que el INI no fue mutado
    assert (mo2_dir / "modorganizer.ini").read_bytes() == ini_antes


def test_03_skyrim_skse_abierto_rechazo_sin_mutacion(tmp_path: pathlib.Path) -> None:
    """Caso 3: Skyrim o SKSE abierto: rechazo por quiescencia sin mutación."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_antes = (mo2_dir / "modorganizer.ini").read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    probes = RuntimeProbes(
        identity=lambda r: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        compatibility=lambda r: CompatibilityResult(
            state=RuntimeCompatibility.COMPATIBLE, reason="compatible sintético"
        ),
        processes=lambda: LiveProcessSnapshot(
            processes=(
                LiveProcessEvidence(
                    kind=LiveProcessKind.GAME,
                    pid=5678,
                    image_path=str(managed_src / "SkyrimSE.exe"),
                    started_at_ns=time.time_ns() - 1_000_000,
                ),
            ),
            indeterminate=False,
        ),
        mo2_instance=_sonda_mo2_portable,
    )

    with pytest.raises(BinderQuiescenceError, match="juego o SKSE vivo"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=probes,
        )

    assert (mo2_dir / "modorganizer.ini").read_bytes() == ini_antes


def test_04_clone_no_provisioned_rechazo(tmp_path: pathlib.Path) -> None:
    """Caso 4: Clone en estado != PROVISIONED (ej. CREATED): rechazo fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(
        storage_root, clone_root, gen_id, clone_id="clone-1", lifecycle=RuntimeCloneLifecycle.CREATED
    )

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    with pytest.raises(BinderPreflightFailedError, match="preflight"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_05_generation_drifted_rechazo(tmp_path: pathlib.Path) -> None:
    """Caso 5: Generation DRIFTED (archivos modificados tras el sellado): rechazo fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    # Provocar drift en la Generation
    (storage_root / "versions" / gen_id / "SkyrimSE.exe").write_bytes(b"DRIFT-MODIFIED")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    with pytest.raises(BinderPreflightFailedError, match="preflight"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_06_runtime_incompatible_rechazo(tmp_path: pathlib.Path) -> None:
    """Caso 6: Runtime incompatible (SKSE DLL/loader no matching): rechazo fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    # Incompatibilidad inyectada por probes
    probes = RuntimeProbes(
        identity=lambda r: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        compatibility=lambda r: CompatibilityResult(
            state=RuntimeCompatibility.INCOMPATIBLE,
            reason="SKSE no coincide con build de runtime",
        ),
        processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
        mo2_instance=_sonda_mo2_portable,
    )

    with pytest.raises(BinderPreflightFailedError, match="preflight"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=probes,
        )


def test_07_config_y_mo2_divergentes_rechazo(tmp_path: pathlib.Path) -> None:
    """Caso 7: config.toml y MO2 divergentes previo al switch: rechazo por preflight."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    # Divergencia: config_file apunta a otro lado distinto de mo2
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(tmp_path / "OtroSkyrim"))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    with pytest.raises(BinderPreflightFailedError, match="preflight"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_08_alias_83_rechazo(tmp_path: pathlib.Path) -> None:
    """Caso 8: Rutas con alias corto 8.3 de Windows: rechazo fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    # Alias corto 8.3 inyectado en config.toml
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(tmp_path / "SKYRIM~1"))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    with pytest.raises(BinderPreflightFailedError, match="preflight"):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_09_fallo_de_snapshot_no_muta(tmp_path: pathlib.Path) -> None:
    """Caso 9: Fallo durante captura de snapshot: no se ejecuta ninguna mutación."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    # Inyectar fallo al capturar el snapshot durable (P5-C1 FIX 3: el snapshot
    # dejó de vivir en RAM; la captura es la que puede fallar por I/O).
    with (
        patch(
            "sky_claw.local.frozen_runtime.binder_recovery.capturar_registro",
            side_effect=RecuperacionError("Permiso denegado al leer snapshot"),
        ),
        pytest.raises(BinderError, match="snapshot"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    # Verificar que el ini sigue intacto
    assert ini_path.read_bytes() == ini_bytes_antes


def test_10_fallo_durante_escritura_temporal_recuperacion(tmp_path: pathlib.Path) -> None:
    """Caso 10: Fallo de I/O al escribir el INI: el destino original no se corrompe.

    El punto de inyección cambió con P5-C2 y el cambio es de fondo, no cosmético:
    el binder ya no escribe un temporal + ``os.replace`` —con la exclusión de
    mutación tomada, el reemplazo por ruta es imposible porque el propio handle
    se bloquea a sí mismo— sino que escribe por el handle retenido. Por eso el
    fallo se inyecta en la escritura por handle, que es el único punto donde
    ahora puede fallar.

    Un parche GLOBAL de ``tempfile.mkstemp`` (lo que hacía antes este test)
    también reventaba el burn de la aprobación, y el binder fallaba como
    ``BinderApprovalError`` ANTES de tocar el INI: el test pasaba por la razón
    equivocada (CodeRabbit sobre #843). El punto nuevo tiene el mismo cuidado:
    es el ``_escribir_handle`` del gate, no una escritura cualquiera.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    def _escribir_que_falla(handle: int, datos: bytes, al_escribir=None) -> None:
        # Acepta y propaga el hook: el escritor real lo recibe y lo invoca antes
        # de mutar. Un doble con otra firma rompe el cableado, no el escenario.
        if al_escribir is not None:
            al_escribir()
        raise Mo2IniExclusionError(f"ENOSPC simulado en la escritura por handle ({len(datos)} B)")

    with (
        patch("sky_claw.local.frozen_runtime.mo2_ini_exclusion._escribir_handle", side_effect=_escribir_que_falla),
        pytest.raises(BinderEscrituraIniError) as excinfo,
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    assert "ENOSPC" in str(excinfo.value), (
        f"el fallo inyectado nunca llegó al escritor del INI; la excepción fue "
        f"{type(excinfo.value).__name__}: {excinfo.value}"
    )
    assert ini_path.read_bytes() == ini_bytes_antes
    # El PENDING ya se escribió: la transición quedó a medias para su dueño.
    assert cargar_journal_transicion(storage_root).disposition is JournalDisposition.PENDING_PROMOTION


def test_11_fallo_durante_reemplazo_recuperacion(tmp_path: pathlib.Path) -> None:
    """Caso 11: Escritura PARCIAL del INI (muerte a mitad): rollback restaura los bytes.

    Este es el caso que la arquitectura de P5-C2 hace posible y que la anterior
    no podía producir: escribir por handle no es atómico, así que un fallo a
    mitad deja el archivo con bytes mezclados. La garantía que se exige —y que
    este test ancla— es que el registro durable ya tiene los bytes previos y el
    rollback los repone.

    Se escribe de verdad la primera mitad por el handle real y recién entonces se
    levanta el error: si el rollback no corriera, el INI quedaría truncado y el
    assert de bytes lo vería.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    real_escribir = mo2_ini_exclusion._escribir_handle
    parciales = {"escritas": 0}

    # Estado intermedio REAL: el que deja morir el escritor durante WriteFile,
    # sin SetEndOfFile. Se construye con los bytes del snapshot y la previsión, y
    # tiene el MISMO tamaño que el snapshot (el truncado no ocurrió).
    #
    # La versión anterior escribía ``datos[:mitad]``, que trunca el archivo: ese
    # estado NO es alcanzable por el escritor real —SetEndOfFile corre sólo tras
    # una escritura completa— y por eso el predicado endurecido, con razón, no lo
    # atribuye a la transición.
    previos = ini_bytes_antes
    nuevo = mo2_ini_escritura.prever_bytes(previos, str(clone_root))
    divergencia = 0
    while divergencia < min(len(previos), len(nuevo)) and previos[divergencia] == nuevo[divergencia]:
        divergencia += 1
    corte = min(divergencia + 5, len(previos))
    intermedio = nuevo[:corte] + previos[corte:]
    assert intermedio != previos, "el corte no produjo un estado intermedio real"

    def _escribir_a_medias(handle: int, datos: bytes, al_escribir=None) -> None:
        # Sólo la PRIMERA escritura (la del contenido nuevo) se corta; las que
        # hace el rollback después tienen que poder completarse, si no el test
        # pasaría porque el rollback también falla.
        if al_escribir is not None:
            al_escribir()
        if parciales["escritas"] == 0:
            parciales["escritas"] += 1
            real_escribir(handle, intermedio)
            raise Mo2IniExclusionError("EIO simulado tras escritura parcial del INI")
        real_escribir(handle, datos)

    with (
        patch("sky_claw.local.frozen_runtime.mo2_ini_exclusion._escribir_handle", side_effect=_escribir_a_medias),
        pytest.raises(BinderError) as excinfo,
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    assert parciales["escritas"] == 1, (
        f"el fallo inyectado nunca se ejerció; la excepción fue {type(excinfo.value).__name__}: {excinfo.value}"
    )
    # El rollback SE EJECUTÓ y restauró los bytes previos: el INI no quedó a medias.
    assert ini_path.read_bytes() == ini_bytes_antes


def test_12_fallo_de_readback_rollback(tmp_path: pathlib.Path) -> None:
    """Caso 12: Si el readback del INI no coincide tras el reemplazo: rollback automático."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    # Forzar que el readback retorne una ruta incorrecta o un estado corrupto
    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.leer_game_path_de_mo2",
            side_effect=[
                # Llamada de readback
                type("FakeEvidencia", (), {"state": ObservationState.UNREADABLE, "path": None, "detail": "corrupto"}),
                # Llamadas subsiguientes
                leer_game_path_de_mo2(ini_path),
            ],
        ),
        pytest.raises(BinderVerificationError, match="readback"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    # Rollback verificado: modorganizer.ini restaurado a su valor previo
    assert ini_path.read_bytes() == ini_bytes_antes


def test_13_interrupcion_entre_pasos_del_journal(tmp_path: pathlib.Path) -> None:
    """Caso 13: Interrupción / fallo en POST VERIFY: el journal permanece PENDING recuperable."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    # Inyectar fallo en assess_effective_runtime durante POST VERIFY
    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.assess_effective_runtime",
            side_effect=RuntimeError("Fallo inesperado durante POST VERIFY"),
        ),
        pytest.raises(RuntimeError, match="POST VERIFY"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    # El journal no declara SUCCESS ni rotó a NONE como exitoso
    journal_snap = cargar_journal_transicion(storage_root)
    assert journal_snap.disposition is not JournalDisposition.NONE


def test_14_ejecucion_repetida_idempotencia(tmp_path: pathlib.Path) -> None:
    """Caso 14: Ejecución repetida cuando ya está bindeado: idempotente sin corrupción ni pérdida."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    res1 = ejecutar_transicion_binder(
        storage_root=storage_root,
        mo2_instance_dir=mo2_dir,
        target_clone_id="clone-1",
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=managed_src,
        config_path=config_file,
        probes=_probes_quiescentes(),
    )
    assert res1.success is True

    # Segunda ejecución: ya está en el clone, la misma llamada es no-op idempotente segura
    res2 = ejecutar_transicion_binder(
        storage_root=storage_root,
        mo2_instance_dir=mo2_dir,
        target_clone_id="clone-1",
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=managed_src,
        config_path=config_file,
        probes=_probes_quiescentes(),
    )
    assert res2.success is True
    assert res2.target_clone_root == clone_root.resolve()


def test_15_cancelacion_no_deja_transicion_exitosa(tmp_path: pathlib.Path) -> None:
    """Caso 15: Interrupción por BaseException (KeyboardInterrupt): no se declara SUCCESS.

    El punto de inyección es el escritor por HANDLE desde P5-C2: el binder ya no
    llama a ``actualizar_game_path_en_ini`` (ver el docstring del módulo
    ``mo2_ini_exclusion``). Parchear el nombre viejo dejaba el test sin ejercer
    nada —la excepción nunca se lanzaba y el ``pytest.raises`` fallaba—, que es
    como se detectó.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.actualizar_game_path_por_handle",
            side_effect=KeyboardInterrupt("Cancelación forzada"),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    # Verificar que el historial de finalizados no registra éxito falso
    historial = derivar_historial_finalizados(storage_root)
    assert historial.latest is None


def test_16_ningun_escritor_apunta_a_steam_o_generation(tmp_path: pathlib.Path) -> None:
    """Caso 16: Ningún escritor ni superficie apunta a Steam o a una Generation tras el cambio."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )
    environ = {"SKYRIM_PATH": str(managed_src)}

    resultado = ejecutar_transicion_binder(
        storage_root=storage_root,
        mo2_instance_dir=mo2_dir,
        target_clone_id="clone-1",
        propuesta=propuesta,
        consentimiento=consentimiento,
        managed_source_root=managed_src,
        config_path=config_file,
        environ=environ,
        probes=_probes_quiescentes(),
    )
    assert resultado.success is True

    # 1. modorganizer.ini apunta al clone
    ev_ini = leer_game_path_de_mo2(mo2_dir / "modorganizer.ini")
    assert ev_ini.path is not None
    assert pathlib.Path(ev_ini.path).resolve() == clone_root.resolve()
    assert str(managed_src) not in ev_ini.path
    assert gen_id not in ev_ini.path

    # 2. config.toml apunta al clone
    assert (
        str(clone_root.resolve()) in str(pathlib.Path(config_file.read_text()).resolve())
        or str(clone_root) in config_file.read_text()
    )
    assert str(managed_src) not in config_file.read_text()

    # 3. environ apunta al clone
    assert environ["SKYRIM_PATH"] == str(clone_root)


def test_17_ningun_usuario_no_autorizado_puede_aprobar(tmp_path: pathlib.Path) -> None:
    """Caso 17: Solicitud sin aprobación o con consentimiento adulterado es rechazada fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    # Consentimiento adulterado (digest que no coincide con el scope)
    consentimiento_falso = dataclasses.replace(consentimiento, approval_scope_digest="0" * 64)

    with pytest.raises(BinderApprovalError):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento_falso,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_18_ninguna_entrada_corrupta_consigue_success(tmp_path: pathlib.Path) -> None:
    """Caso 18: Clone_id inexistente o basura: rechazo fail-closed."""
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-inexistente"
    )

    with pytest.raises(BinderPreflightFailedError):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-inexistente",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )


def test_19_censo_enumerativo_escritores_game_dir() -> None:
    """Caso 19: el censo de escritores del game dir se DERIVA del AST (§29.4).

    Antes este test comparaba un ``frozenset`` literal contra la constante que
    ese mismo literal definía: pasaba siempre y el censo estaba desactualizado
    (``grass_cache`` y ``wrye_bash`` no existen; ``pandora``, ``bodyslide`` y
    ``dyndolod_legacy`` no son los nombres reales). FIX 7 reemplaza el espejo por
    descubrimiento: lo que se compara ahora es el resultado del AST contra el
    conjunto congelado, y agregar un adaptador nuevo rompe el ancla.

    El detalle de las tres clases de superficie y de la coherencia entre ellas
    vive en ``tests/test_frozen_runtime_p5c1_superficies.py``.
    """
    from sky_claw.local.frozen_runtime import surface_census

    assert surface_census.descubrir_adaptadores_destino_en_game_dir() == surface_census.ADAPTADORES_DESTINO_EN_GAME_DIR
    assert surface_census.CENSO_ESCRITORES_GAME_DIR == CENSO_ESCRITORES_GAME_DIR
    # Y ningún nombre del censo es inventado.
    assert "grass_cache" not in CENSO_ESCRITORES_GAME_DIR
    assert "wrye_bash" not in CENSO_ESCRITORES_GAME_DIR


def test_20_codificacion_qt_reciproca() -> None:
    """Prueba unitaria de codificación y decodificación recíproca Qt @ByteArray."""
    ruta_original = r"C:\Modding\Skyrim Clone 1.6.1170"
    codificado = codificar_qt_byte_array(ruta_original)
    assert codificado.startswith("@ByteArray(")
    assert codificado.endswith(")")
    assert r"\\" in codificado

    decodificado = decode_qt_scalar(codificado)
    assert decodificado == ruta_original


# ---------------------------------------------------------------------------
# Caso 21 — la exclusión ya se liberó: un prefijo ajeno NO es nuestro
# ---------------------------------------------------------------------------
#
# ``_rollback`` corre DESPUÉS de ``exclusion.cerrar()``: el ``finally`` de la
# escritura del INI suelta el handle antes de CONFIG, ENTORNO, VERIFY, CAS y
# POST-VERIFY. A partir de ese instante el archivo lo puede escribir cualquiera.
#
# ``ini_escritura_intentada=True`` es un dato de PROCESO y sigue siendo True
# para siempre, así que autorizaba ``_es_escritura_propia_interrumpida`` sin
# preguntar si el archivo sigue siendo el que dejamos. Un tercero que escriba un
# prefijo de la previsión —lo que pasa cuando SU escritura muere a mitad, porque
# todo el texto antes del valor de ``gamePath`` es idéntico— hace que el rollback
# lo tome por propio, restaure el snapshot y destruya esos bytes, declarando
# éxito.
#
# La prueba de autoría que sí vale es la que se toma BAJO la exclusión: si el
# archivo ya no es eso, alguien escribió después y no hay nada que atribuir.


def _escribir_desde_otro_proceso(ruta: pathlib.Path, datos: bytes) -> None:
    """Escribe ``datos`` desde el byte 0 en un proceso Windows REAL.

    Un ``Thread`` no sirve: comparte la tabla de handles del intérprete y no
    puede demostrar que la exclusión ya no protege.
    """
    programa = "import base64, pathlib, sys\npathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]))\n"
    subprocess.run(
        [sys.executable, "-c", programa, str(ruta), base64.b64encode(datos).decode("ascii")],
        check=True,
        capture_output=True,
    )


def test_21_rollback_no_atribuye_un_prefijo_escrito_por_un_tercero(tmp_path: pathlib.Path) -> None:
    """Caso 21: tras ``cerrar()``, un prefijo ajeno se conserva, no se restaura.

    Se reproduce la carrera completa: el binder escribe el INI, libera la
    exclusión, un SEGUNDO PROCESO escribe un prefijo de la previsión —lo que deja
    SU escritura al morir a mitad— y después falla CONFIG, que dispara el
    rollback.

    El Clone va en una ruta LARGA a propósito: así la previsión es más grande que
    el INI original y el corte del tercero ya extendió el archivo, que es el
    desenlace que el rig midió como «extiende» y el único que el predicado
    atribuye por prefijo puro.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1" / ("R" * 90)
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    previsto = mo2_ini_escritura.prever_bytes(ini_bytes_antes, str(clone_root))
    # Un tercero con OTRA ruta: su texto coincide con la previsión hasta el
    # valor de ``gamePath`` y más allá, porque todo lo anterior es idéntico.
    tercero = mo2_ini_escritura.prever_bytes(ini_bytes_antes, str(clone_root) + "_ajena")
    corte = next(i for i in range(min(len(previsto), len(tercero))) if previsto[i] != tercero[i])
    parcial_ajeno = tercero[:corte]

    assert previsto != ini_bytes_antes, "la previsión no cambia el INI: el test no probaría nada"
    assert parcial_ajeno == previsto[:corte], "el prefijo del tercero no coincide con la previsión"
    assert len(ini_bytes_antes) < corte < len(previsto), (
        f"el corte ({corte}) tiene que caer más allá del EOF original ({len(ini_bytes_antes)}) "
        f"y antes del final de la previsión ({len(previsto)}): es el desenlace «extiende»"
    )
    assert _es_escritura_propia_interrumpida(ini_bytes_antes, previsto, parcial_ajeno), (
        "el predicado no atribuye este estado: el test no reproduciría el defecto"
    )

    def _config_que_falla(*_args: object, **_kwargs: object) -> None:
        """Falla CONFIG después de que la exclusión ya se liberó."""
        _escribir_desde_otro_proceso(ini_path, parcial_ajeno)
        raise OSError(13, "config no escribible")

    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.persistir_campo",
            side_effect=_config_que_falla,
        ),
        pytest.raises(OSError, match="config no escribible"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    # El tercero escribió mientras el binder ya no tenía la exclusión: sus bytes
    # NO se pueden atribuir a la transición. Restaurar el snapshot los destruye.
    assert ini_path.read_bytes() == parcial_ajeno, (
        "el rollback pisó bytes que no puede demostrar propios: "
        f"esperaba {len(parcial_ajeno)} bytes del tercero y hay {len(ini_path.read_bytes())}"
    )

    evidencias = list((storage_root / "recovery_evidence").rglob("*.bin"))
    assert evidencias, "no se conservó evidencia del contenido no atribuible"
    assert evidencias[0].read_bytes() == parcial_ajeno


def test_22_rollback_legitimo_sigue_restaurando_tras_fallo_posterior(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control positivo: si nadie tocó el INI, el rollback legítimo restaura.

    Sin este control, «no restaurar nunca» pasaría el caso 21 y rompería la
    transacción: es exactamente la mitad que hay que conservar.

    Además se ancla el ORDEN: la huella tiene que leerse ANTES de ``cerrar()``.
    Leerla después la convierte en el estado del tercero y el chequeo deja de
    discriminar nada.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    ini_bytes_antes = ini_path.read_bytes()
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    estado = {"cerrado": False, "huellas": 0}
    cerrar_original = mo2_ini_exclusion.ExclusionIni.cerrar
    bytes_original = binder_module._bytes_bajo_exclusion

    def _cerrar_espia(self) -> None:  # type: ignore[no-untyped-def]
        estado["cerrado"] = True
        cerrar_original(self)

    def _bytes_espia(exclusion) -> bytes | None:  # type: ignore[no-untyped-def]
        assert not estado["cerrado"], "la huella se leyó DESPUÉS de liberar la exclusión: no prueba autoría"
        estado["huellas"] += 1
        return bytes_original(exclusion)

    monkeypatch.setattr(mo2_ini_exclusion.ExclusionIni, "cerrar", _cerrar_espia)
    monkeypatch.setattr(binder_module, "_bytes_bajo_exclusion", _bytes_espia)

    def _config_que_falla(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "config no escribible")

    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.persistir_campo",
            side_effect=_config_que_falla,
        ),
        pytest.raises(OSError, match="config no escribible"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    assert estado["huellas"] == 1, "la huella bajo exclusión no se tomó exactamente una vez"
    assert ini_path.read_bytes() == ini_bytes_antes, "el rollback legítimo dejó de restaurar el INI"


def test_23_sin_huella_bajo_exclusion_no_se_atribuye_el_ini(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la huella no se pudo leer, la autoría es DESCONOCIDA: no se restaura.

    Es la rama fail-closed del arreglo. Sin ella, «no hay huella» se confundiría
    con «la huella coincide» y volvería la atribución por forma que el caso 21
    prohíbe.
    """
    steam_root = tmp_path / "Steam"
    managed_src = _crear_managed_source_sintetica(steam_root)
    storage_root = tmp_path / "Storage"
    initialize_frozen_runtime_storage(storage_root)
    gen_id = _crear_generation_valida(storage_root)
    clone_root = tmp_path / "Clones" / "Clone1"
    _crear_clone_sintetico_valido(storage_root, clone_root, gen_id, clone_id="clone-1")

    mo2_dir = tmp_path / "MO2"
    _crear_mo2_instance_sintetica(mo2_dir, game_path=str(managed_src))
    ini_path = mo2_dir / "modorganizer.ini"
    config_file = _crear_config_toml(tmp_path / "config.toml", skyrim_path=str(managed_src))
    propuesta, consentimiento = _preparar_aprobacion(
        storage_root, target_generation_id=gen_id, target_clone_id="clone-1"
    )

    previsto = mo2_ini_escritura.prever_bytes(ini_path.read_bytes(), str(clone_root))
    monkeypatch.setattr(binder_module, "_bytes_bajo_exclusion", lambda _exclusion: None)

    def _config_que_falla(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "config no escribible")

    with (
        patch(
            "sky_claw.local.frozen_runtime.runtime_clone_binder.persistir_campo",
            side_effect=_config_que_falla,
        ),
        pytest.raises(OSError, match="config no escribible"),
    ):
        ejecutar_transicion_binder(
            storage_root=storage_root,
            mo2_instance_dir=mo2_dir,
            target_clone_id="clone-1",
            propuesta=propuesta,
            consentimiento=consentimiento,
            managed_source_root=managed_src,
            config_path=config_file,
            probes=_probes_quiescentes(),
        )

    assert ini_path.read_bytes() == previsto, (
        "sin huella bajo exclusión se atribuyó igual el INI a la transición y se restauró"
    )
    assert list((storage_root / "recovery_evidence").rglob("*.bin")), "no se conservó evidencia"
