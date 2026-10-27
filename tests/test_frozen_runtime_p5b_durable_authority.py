"""Pruebas de autoridad durable de RuntimeCloneRecord e integración con el oráculo (P5-B).

Contratos auditados:
- Persistencia tipada e inmutable bajo state/clones/<clone-id>.json.
- Integridad de linaje, setup y exclusión física.
- Máquina de estados RuntimeCloneLifecycle (CREATED | PROVISIONING | PROVISIONED | INVALID).
- Exclusión concurrente interproceso mediante participant_lock del FrozenRuntimeRoot.
- Conexión de desired_runtime_from_storage con autoridad durable auténtica en active.json v2.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.clones import (
    CLONE_RECORD_SCHEMA_VERSION,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    actualizar_lifecycle_clone,
    admitir_clone_root_path,
    cargar_runtime_clone_record,
    leer_runtime_clone_record,
    registrar_runtime_clone_record,
    validar_clone_id,
    verificar_runtime_clone,
)
from sky_claw.local.frozen_runtime.effective_runtime import (
    assess_effective_runtime,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    CompatibilityResult,
    EffectiveRuntimeVerdict,
    LiveProcessSnapshot,
    ReasonCode,
    RuntimeCompatibility,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeLockBusyError,
    FrozenRuntimeStorageError,
    InvalidCloneIdError,
    RuntimeCloneAdmissionError,
    RuntimeCloneCollisionError,
    RuntimeCloneCorruptError,
    RuntimeCloneLifecycleError,
    RuntimeCloneSchemaError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generations import registrar_generation_metadata
from sky_claw.local.frozen_runtime.models import (
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.state import (
    load_frozen_runtime_state,
    save_frozen_runtime_state,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    clone_record_path,
    clones_state_dir,
    initialize_frozen_runtime_storage,
)
from sky_claw.local.frozen_runtime.storage_models import (
    FrozenRuntimeState,
    GenerationVerificationState,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

_RAIZ_WORKTREE = pathlib.Path(__file__).resolve().parent.parent
_CHILD_SCRIPT = _RAIZ_WORKTREE / "tests" / "_p5b_child.py"


def _entorno_hijo() -> dict[str, str]:
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(_RAIZ_WORKTREE) + os.pathsep + entorno.get("PYTHONPATH", "")
    return entorno


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


def _probes_limpios(
    *,
    version: str = "1.6.1170.0",
    compatibilidad: RuntimeCompatibility = RuntimeCompatibility.COMPATIBLE,
) -> RuntimeProbes:
    return RuntimeProbes(
        identity=lambda _r: RuntimeIdentity(game_key="skyrimse", game_version=version),
        compatibility=lambda _r: CompatibilityResult(
            state=compatibilidad, reason="test: compatible", game_version=version
        ),
        processes=lambda: LiveProcessSnapshot(processes=(), indeterminate=False),
    )


def _crear_generation_valida(
    root: pathlib.Path,
    *,
    version: str = "1.6.1170.0",
    digest_prefix: str = "a1b2c3d4e5f6",
) -> str:
    staging = root / f"_staging_gen_{digest_prefix}"
    if staging.exists():
        shutil.rmtree(staging)
    _crear_arbol_juego(staging, version=version)
    if digest_prefix != "a1b2c3d4e5f6":
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
    meta = registrar_generation_metadata(root, evidencia)
    gen_dir = root / "versions" / meta.generation_id
    if gen_dir.exists():
        shutil.rmtree(gen_dir)
    shutil.copytree(staging, gen_dir)
    shutil.rmtree(staging)
    return meta.generation_id


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


class TestFrozenRuntimeP5BDurableAuthority:
    """Suite de pruebas para P5-B: Autoridad durable de RuntimeCloneRecord."""

    def test_01_persistencia_y_lectura_mismo_registro(self, tmp_path: pathlib.Path) -> None:
        """1. Persistencia y lectura idéntica del mismo RuntimeCloneRecord."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_1")

        record = RuntimeCloneRecord(
            schema_version=CLONE_RECORD_SCHEMA_VERSION,
            clone_id="1.6.1170__a1b2c3d4e5f6--c1",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-skse-1.6.1170",
            verified_runtime_setup_id="setup-skse-1.6.1170",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=123456789,
        )

        registrado = registrar_runtime_clone_record(root, record)
        assert registrado == record

        leido = leer_runtime_clone_record(root, "1.6.1170__a1b2c3d4e5f6--c1")
        assert leido == record
        assert leido.schema_version == 1
        assert leido.clone_id == "1.6.1170__a1b2c3d4e5f6--c1"
        assert leido.lifecycle is RuntimeCloneLifecycle.PROVISIONED
        assert leido.admitted_role == "runtime_clone"
        assert leido.root_path == str(clone_root)

    def test_02_corrupcion_truncamiento_y_schema_desconocido(self, tmp_path: pathlib.Path) -> None:
        """2. Corrupción, truncamiento, campo faltante y schema desconocido fallan cerrado."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        clones_dir = clones_state_dir(root)
        clones_dir.mkdir(parents=True, exist_ok=True)

        # Archivo vacío (0 bytes)
        (clones_dir / "clone_empty.json").write_bytes(b"")
        with pytest.raises(RuntimeCloneCorruptError, match="vacío o corrupto"):
            leer_runtime_clone_record(root, "clone_empty")

        # JSON truncado
        (clones_dir / "clone_trunc.json").write_text('{"schema_version": 1, "clone_id":', encoding="utf-8")
        with pytest.raises(RuntimeCloneCorruptError, match="JSON malformado o truncado"):
            leer_runtime_clone_record(root, "clone_trunc")

        # No es objeto JSON
        (clones_dir / "clone_array.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(RuntimeCloneCorruptError, match="debe ser un objeto JSON"):
            leer_runtime_clone_record(root, "clone_array")

        # Schema desconocido
        (clones_dir / "clone_schema.json").write_text(
            json.dumps({"schema_version": 99, "clone_id": "clone_schema"}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeCloneSchemaError, match="schema desconocido 99"):
            leer_runtime_clone_record(root, "clone_schema")

        # Campo obligatorio faltante (ej: source_generation_id)
        incompleto = {
            "schema_version": 1,
            "clone_id": "clone_inc",
            "lifecycle": "PROVISIONED",
            "root_path": "C:\\fake",
            "admitted_role": "runtime_clone",
            "created_at_ns": 0,
        }
        (clones_dir / "clone_inc.json").write_text(json.dumps(incompleto), encoding="utf-8")
        with pytest.raises(RuntimeCloneSchemaError, match="falta el campo obligatorio 'source_generation_id'"):
            leer_runtime_clone_record(root, "clone_inc")

        # admitted_role inválido
        invalido_rol = dict(incompleto)
        invalido_rol["source_generation_id"] = "1.6.1170__a1b2c3d4e5f6"
        invalido_rol["intended_runtime_setup_id"] = "s1"
        invalido_rol["verified_runtime_setup_id"] = "s1"
        invalido_rol["admitted_role"] = "generation"  # violación SFR-19
        (clones_dir / "clone_rol.json").write_text(json.dumps(invalido_rol), encoding="utf-8")
        with pytest.raises(RuntimeCloneAdmissionError, match="admitted_role inválido"):
            leer_runtime_clone_record(root, "clone_rol")

    def test_03_identificadores_invalidos(self, tmp_path: pathlib.Path) -> None:
        """3. Rechazo estricto de identificadores inválidos, con traversal o nombres reservados."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)

        for id_invalido in (
            "",
            "   ",
            "../clone_escape",
            "..\\clone_escape",
            "clone/slash",
            "clone\\backslash",
            "con",
            "nul",
            "prn",
            "aux",
            "com1",
            "lpt2",
            "clone*bad",
            "clone:bad",
        ):
            with pytest.raises(InvalidCloneIdError):
                validar_clone_id(id_invalido)

        # Normalización a minúsculas
        assert validar_clone_id("Clone-Alpha_01") == "clone-alpha_01"

    def test_04_registros_huerfanos_o_incompatibles(self, tmp_path: pathlib.Path) -> None:
        """4. Un clone cuya Generation declarada no existe en el storage es huérfano."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_orphan")

        record = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-orphan",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=0,
        )
        registrar_runtime_clone_record(root, record)

        # Simular huérfano eliminando la Generation de origen
        (root / "state" / "generations" / f"{gen_id}.json").unlink()

        resultado = verificar_runtime_clone(root, "clone-orphan")
        assert resultado.state is GenerationVerificationState.INVALID
        assert "Generation de origen no existe" in resultado.message

    def test_05_raices_inexistentes_o_con_reparse_points(self, tmp_path: pathlib.Path) -> None:
        """5. Rechazo de raíces con reparse points o superpuestas con Managed Source o versions."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)

        # Dentro de versions/ (prohibido por SFR-19)
        versions_path = root / "versions" / "1.6.1170__a1b2c3d4e5f6"
        with pytest.raises(RuntimeCloneAdmissionError, match="dentro de versions"):
            admitir_clone_root_path(versions_path, storage_root=root)

        # Dentro de steamapps/common (Managed Source)
        steamapps_path = tmp_path / "SteamLibrary" / "steamapps" / "common" / "Skyrim Special Edition"
        with pytest.raises(RuntimeCloneAdmissionError, match="área administrada por Steam"):
            admitir_clone_root_path(steamapps_path, storage_root=root)

        # Ruta no absoluta
        with pytest.raises(RuntimeCloneAdmissionError, match="debe ser absoluta"):
            admitir_clone_root_path("relativa/clone", storage_root=root)

        # Reparse point / junction rechazado
        target_dir = tmp_path / "target_reparse"
        target_dir.mkdir()
        junction_dir = tmp_path / "junction_reparse"
        if os.name == "nt":
            import subprocess

            cmd_res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_dir), str(target_dir)], capture_output=True
            )
            if cmd_res.returncode == 0:
                with pytest.raises(RuntimeCloneAdmissionError, match="reparse point"):
                    admitir_clone_root_path(junction_dir, storage_root=root)
        else:
            try:
                junction_dir.symlink_to(target_dir, target_is_directory=True)
                with pytest.raises(RuntimeCloneAdmissionError, match="reparse point"):
                    admitir_clone_root_path(junction_dir, storage_root=root)
            except OSError:
                pass

    def test_06_cambios_de_identidad_o_incompatibilidad_detectada(self, tmp_path: pathlib.Path) -> None:
        """6. Verificación física detecta divergencia en ejecutables críticos contra la Generation."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root, version="1.6.1170.0")

        # Clone con versión divergente
        divergent_root = _crear_arbol_juego(tmp_path / "clone_divergent", version="1.5.97.0")
        record = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-divergent",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(divergent_root),
            admitted_role="runtime_clone",
            created_at_ns=0,
        )
        registrar_runtime_clone_record(root, record)

        resultado = verificar_runtime_clone(root, "clone-divergent")
        assert resultado.state is GenerationVerificationState.INVALID
        assert "identidad de runtime" in resultado.message or "críticos" in resultado.message

    def test_07_dos_escritores_concurrentes_procesos_independientes(self, tmp_path: pathlib.Path) -> None:
        """7. Exclusión interproceso real sobre el FrozenRuntimeRoot."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)

        ready_file = tmp_path / "child_ready.flag"
        release_file = tmp_path / "child_release.flag"

        # Lanzar proceso hijo que sostiene el participant_lock
        proc = subprocess.Popen(
            [
                sys.executable,
                str(_CHILD_SCRIPT),
                "lock_hold",
                str(root),
                json.dumps(
                    {
                        "ready_file": str(ready_file),
                        "release_file": str(release_file),
                        "timeout": 5.0,
                    }
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_entorno_hijo(),
            cwd=str(_RAIZ_WORKTREE),
        )

        try:
            # Esperar a que el hijo tome el lock
            inicio = time.time()
            while not ready_file.exists():
                time.sleep(0.05)
                assert time.time() - inicio < 10.0, "Timeout esperando a que el proceso hijo tome el lock"

            # Intentar registrar un clone desde el proceso padre con timeout=0.0 debe fallar con FrozenRuntimeLockBusyError
            clone_root = _crear_arbol_juego(tmp_path / "clone_conc")
            rec = RuntimeCloneRecord(
                schema_version=1,
                clone_id="clone-concurrent",
                source_generation_id=gen_id,
                lifecycle=RuntimeCloneLifecycle.CREATED,
                intended_runtime_setup_id="setup-1",
                verified_runtime_setup_id=None,
                root_path=str(clone_root),
                admitted_role="runtime_clone",
                created_at_ns=0,
            )

            with pytest.raises(FrozenRuntimeLockBusyError):
                registrar_runtime_clone_record(root, rec, timeout=0.0)

        finally:
            # Señalar al hijo para liberar
            release_file.write_text("release", encoding="utf-8")
            out, err = proc.communicate(timeout=10.0)
            assert proc.returncode == 0, f"Hijo falló: {err}"

        # Tras liberar, el registro ahora debe tener éxito
        res = registrar_runtime_clone_record(root, rec, timeout=5.0)
        assert res.clone_id == "clone-concurrent"

    def test_08_escritura_interrumpida_preserva_registro_previo(self, tmp_path: pathlib.Path) -> None:
        """8. Ante fallo antes de os.replace, el registro anterior permanece intacto."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_atomic")

        original = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-atomic",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, original)

        # Dejar un temporal huérfano simulado
        tmp_left = clones_state_dir(root) / ".sky_claw_simulated.tmp"
        tmp_left.write_text("corrupted tmp", encoding="utf-8")

        # El registro original sigue siendo válido
        leido = leer_runtime_clone_record(root, "clone-atomic")
        assert leido == original

    def test_09_duplicados_y_proteccion_no_clobber(self, tmp_path: pathlib.Path) -> None:
        """9. Idempotencia ante registro idéntico y colisión fail-closed ante identidades distintas."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_dup")

        rec1 = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-dup",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec1)

        # Mismo registro: idempotente
        idem = registrar_runtime_clone_record(root, rec1)
        assert idem == rec1

        # Mismo id con datos distintos (source_generation_id distinto): RuntimeCloneCollisionError
        gen_id2 = _crear_generation_valida(root, digest_prefix="b1b2b3b4b5b6")
        rec_distinto = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-dup",
            source_generation_id=gen_id2,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneCollisionError, match="ya existe con identidad distinta"):
            registrar_runtime_clone_record(root, rec_distinto)

    def test_10_ausencia_desired_clone_permanece_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """10. Sin Desired Clone auténtico o con clone no provisionado, el oráculo permanece UNKNOWN."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        _crear_generation_valida(root)

        # active.json v1 (sin desired_clone_id)
        deseado_v1 = desired_runtime_from_storage(root)
        assert deseado_v1.clone_root is None

        # Instancia MO2 apuntando a cualquier lado
        instancia = tmp_path / "mo2_inst"
        instancia.mkdir()
        (instancia / "modorganizer.ini").write_text(
            f"[General]\ngamePath=@ByteArray({str(tmp_path / 'anything').replace(chr(92), chr(92) * 2)})\n",
            encoding="utf-8",
        )

        res = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=deseado_v1,
            config_skyrim_path=str(tmp_path / "anything"),
            probes=_probes_limpios(),
        )
        assert res.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.NO_DESIRED_CLONE in res.reasons
        assert not res.authorizes_activation

        # active.json v2 apuntando a clone en estado CREATED (no PROVISIONED)
        gen_id = _crear_generation_valida(root, digest_prefix="f1f2f3f4f5f6")
        clone_root_created = _crear_arbol_juego(tmp_path / "clone_created")
        rec_created = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-created",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root_created),
            admitted_role="runtime_clone",
            created_at_ns=1,
        )
        registrar_runtime_clone_record(root, rec_created)
        save_frozen_runtime_state(
            active_state_path(root),
            FrozenRuntimeState(
                schema_version=2,
                updated_at_ns=100,
                desired_active_generation=gen_id,
                desired_clone_id="clone-created",
            ),
        )
        deseado_created = desired_runtime_from_storage(root)
        assert deseado_created.clone_root is None
        res_created = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=deseado_created,
            config_skyrim_path=str(tmp_path / "anything"),
            probes=_probes_limpios(),
        )
        assert res_created.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert ReasonCode.NO_DESIRED_CLONE in res_created.reasons
        assert not res_created.authorizes_activation

    def test_11_transiciones_fsm_lifecycle_clone(self, tmp_path: pathlib.Path) -> None:
        """11. Máquina de estados RuntimeCloneLifecycle y verificación de setups."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_fsm")

        # Crear en CREATED
        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-fsm",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=0,
        )
        registrar_runtime_clone_record(root, rec)

        # Transición válida: CREATED -> PROVISIONING
        p1 = actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONING)
        assert p1.lifecycle is RuntimeCloneLifecycle.PROVISIONING

        # Transición inválida: PROVISIONED sin verified_runtime_setup_id
        with pytest.raises(RuntimeCloneLifecycleError, match="verified_runtime_setup_id"):
            actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONED)

        # Transición válida: PROVISIONING -> PROVISIONED con verified_runtime_setup_id
        p2 = actualizar_lifecycle_clone(
            root,
            "clone-fsm",
            RuntimeCloneLifecycle.PROVISIONED,
            verified_runtime_setup_id="setup-1",
        )
        assert p2.lifecycle is RuntimeCloneLifecycle.PROVISIONED
        assert p2.verified_runtime_setup_id == "setup-1"

        # Transición terminal: PROVISIONED -> INVALID
        p3 = actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.INVALID)
        assert p3.lifecycle is RuntimeCloneLifecycle.INVALID

        # Transición inválida desde INVALID (terminal)
        with pytest.raises(RuntimeCloneLifecycleError, match="estado terminal"):
            actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONED)

    def test_12_oraculo_obtiene_match_con_autoridad_durable_completa(self, tmp_path: pathlib.Path) -> None:
        """12. Oráculo utiliza evidencia persistente auténtica y produce MATCH legítimo."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root, version="1.6.1170.0")
        clone_root = _crear_arbol_juego(tmp_path / "clone_match", version="1.6.1170.0")

        # Registrar clone en PROVISIONED
        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="1.6.1170__a1b2c3d4e5f6--c1",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # Persistir active.json en schema v2
        estado_v2 = FrozenRuntimeState(
            schema_version=2,
            desired_active_generation=gen_id,
            desired_clone_id="1.6.1170__a1b2c3d4e5f6--c1",
            updated_at_ns=200,
        )
        save_frozen_runtime_state(root / "state" / "active.json", estado_v2)

        # Leer DesiredRuntime a través de la autoridad durable
        deseado = desired_runtime_from_storage(root)
        assert deseado.generation_id == gen_id
        assert deseado.clone_id == "1.6.1170__a1b2c3d4e5f6--c1"
        assert deseado.clone_root == clone_root

        # Configurar MO2 apuntando al clone_root
        instancia = tmp_path / "mo2_match"
        instancia.mkdir()
        ini_content = f"[General]\ngamePath=@ByteArray({str(clone_root).replace(chr(92), chr(92) * 2)})\n"
        (instancia / "modorganizer.ini").write_text(ini_content, encoding="utf-8")

        resultado = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=deseado,
            config_skyrim_path=str(clone_root),
            probes=_probes_limpios(),
        )

        assert resultado.verdict is EffectiveRuntimeVerdict.MATCH
        assert resultado.authorizes_activation is True
        assert ReasonCode.EFFECTIVE_MATCHES_DESIRED_CLONE in resultado.reasons
        assert resultado.desired_clone_id == "1.6.1170__a1b2c3d4e5f6--c1"
        assert resultado.desired_clone_root == str(clone_root)

    def test_13_active_state_v2_par_parcialmente_nulo_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """13. active.json v2 con sólo generation_id o sólo clone_id es corrupto (fail-closed)."""
        active_path = tmp_path / "active.json"

        # Solo desired_generation_id
        active_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "desired_generation_id": "1.6.1170__a1b2c3d4e5f6",
                    "desired_clone_id": None,
                    "updated_at_ns": 0,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(StateSchemaError, match="ambos null o ambos no-null"):
            load_frozen_runtime_state(active_path)

    def test_14_falsificacion_autoridad_clone_con_generation_inexistente_rechazada(
        self, tmp_path: pathlib.Path
    ) -> None:
        """14. Registro de clone con Generation inexistente debe rechazarse, y desired_runtime permanece fail-closed."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_fake")

        gen_id_inexistente = "1.6.1170__999999999999"
        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-fake",
            source_generation_id=gen_id_inexistente,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )

        # 1. registrar_runtime_clone_record debe rechazar un clone cuya Generation de origen no existe
        with pytest.raises(FrozenRuntimeStorageError, match="metadata ausente para la generation"):
            registrar_runtime_clone_record(root, rec)

        # 2. Aun si el registro existiese en disco (bypass/manipulación directa),
        # desired_runtime_from_storage debe permanecer fail-closed (clone_root is None)
        # y el oráculo NUNCA debe emitir MATCH ni autorizar activación.
        from sky_claw.local.frozen_runtime.clones import _payload_de_clone_record
        from sky_claw.local.frozen_runtime.state import write_json_atomic

        destino = clones_state_dir(root) / "clone-fake.json"
        write_json_atomic(destino, _payload_de_clone_record(rec))

        save_frozen_runtime_state(
            active_state_path(root),
            FrozenRuntimeState(
                schema_version=2,
                updated_at_ns=100,
                desired_active_generation=gen_id_inexistente,
                desired_clone_id="clone-fake",
            ),
        )

        deseado = desired_runtime_from_storage(root)
        assert deseado.clone_root is None  # FAIL-CLOSED: no autoriza ruta sin generación válida

        instancia = tmp_path / "mo2_inst"
        instancia.mkdir()
        (instancia / "modorganizer.ini").write_text(
            f"[General]\ngamePath=@ByteArray({str(clone_root).replace(chr(92), chr(92) * 2)})\n",
            encoding="utf-8",
        )

        res = assess_effective_runtime(
            mo2_instance_dir=instancia,
            desired=deseado,
            config_skyrim_path=str(clone_root),
            probes=_probes_limpios(),
        )
        assert res.verdict is EffectiveRuntimeVerdict.UNKNOWN
        assert not res.authorizes_activation

    def test_15_integridad_archivos_criticos_detecta_divergencia_sha256_mismo_tamano(
        self, tmp_path: pathlib.Path
    ) -> None:
        """15. verificar_runtime_clone detecta alteración de archivo crítico con mismo tamaño pero distinto digest."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)

        gen_id = _crear_generation_valida(root, version="1.6.1170.0")

        # Crear clone root donde SkyrimSE.exe tiene exactamente el mismo tamaño pero bytes distintos
        clone_root = tmp_path / "clone_tampered"
        clone_root.mkdir()
        original_bytes = b"SkyrimSE-1.6.1170.0"
        tampered_bytes = b"X" * len(original_bytes)
        assert len(tampered_bytes) == len(original_bytes)
        assert hashlib.sha256(tampered_bytes).hexdigest() != hashlib.sha256(original_bytes).hexdigest()

        (clone_root / "SkyrimSE.exe").write_bytes(tampered_bytes)
        (clone_root / "skse64_loader.exe").write_bytes(b"loader")
        (clone_root / "skse64_1_6_1170.dll").write_bytes(b"dll")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-tampered",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)
        verif = verificar_runtime_clone(root, "clone-tampered")

        assert verif.state is GenerationVerificationState.INVALID
        assert "digest de archivo crítico" in verif.message or "difiere" in verif.message

    def test_16_contencion_fisica_rechaza_storage_root_y_ancestros(self, tmp_path: pathlib.Path) -> None:
        """16. admitir_clone_root_path rechaza storage_root, sus ancestros y solapamientos con managed_source."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)

        # Clone root == storage_root
        with pytest.raises(RuntimeCloneAdmissionError, match="storage root"):
            admitir_clone_root_path(root, storage_root=root)

        # Clone root es ancestro de storage_root
        with pytest.raises(RuntimeCloneAdmissionError, match="storage root"):
            admitir_clone_root_path(tmp_path, storage_root=root)

        # Solapamiento con Managed Source (managed_source dentro de clone_root o viceversa)
        sources_dir = tmp_path / "OtherSources"
        ms_root = sources_dir / "SkyrimGame"
        ms_root.mkdir(parents=True)
        with pytest.raises(RuntimeCloneAdmissionError, match="Managed Source"):
            admitir_clone_root_path(ms_root, storage_root=root, managed_source_root=ms_root)

        with pytest.raises(RuntimeCloneAdmissionError, match="Managed Source"):
            admitir_clone_root_path(sources_dir, storage_root=root, managed_source_root=ms_root)

    def test_17_corrupcion_active_json_y_clone_record_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """17. Corrupción en active.json o en clone record propaga excepción tipada y nunca se disfraza de ausencia."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        act_path = active_state_path(root)

        # 1. active.json truncado -> StateCorruptError
        act_path.write_text('{"schema_version": 2, "desired', encoding="utf-8")
        with pytest.raises(StateCorruptError, match="JSON malformado o truncado"):
            desired_runtime_from_storage(root)

        # 2. active.json con schema desconocido -> StateSchemaError
        act_path.write_text('{"schema_version": 999, "updated_at_ns": 1}', encoding="utf-8")
        with pytest.raises(StateSchemaError, match="schema desconocido"):
            desired_runtime_from_storage(root)

        # 3. active.json con par incompleto -> StateSchemaError
        act_path.write_text(
            '{"schema_version": 2, "updated_at_ns": 1, "desired_generation_id": "1.6.1170__a1b2c3d4e5f6", "desired_clone_id": null}',
            encoding="utf-8",
        )
        with pytest.raises(StateSchemaError, match="ambos null o ambos no-null"):
            desired_runtime_from_storage(root)

        # 4. active.json válido, pero state/clones/<clone_id>.json corrupto -> RuntimeCloneCorruptError
        gen_id = _crear_generation_valida(root)
        save_frozen_runtime_state(
            act_path,
            FrozenRuntimeState(
                schema_version=2,
                updated_at_ns=100,
                desired_active_generation=gen_id,
                desired_clone_id="clone-corrupto",
            ),
        )
        clone_file = clones_state_dir(root) / "clone-corrupto.json"
        clone_file.write_text('{"schema_version": 1, "clone_id', encoding="utf-8")

        with pytest.raises(RuntimeCloneCorruptError, match="JSON malformado"):
            desired_runtime_from_storage(root)

    def test_18_vinculacion_filename_y_clone_id_en_payload(self, tmp_path: pathlib.Path) -> None:
        """18. Discrepancia entre filename y clone_id en payload se rechaza como corrupta (fail-closed)."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_a")

        # Guardar en state/clones/clone-a.json un payload que declara clone_id="clone-b"
        payload_falso = {
            "schema_version": 1,
            "clone_id": "clone-b",
            "source_generation_id": gen_id,
            "lifecycle": "PROVISIONED",
            "intended_runtime_setup_id": "setup-1",
            "verified_runtime_setup_id": "setup-1",
            "root_path": str(clone_root),
            "admitted_role": "runtime_clone",
            "created_at_ns": 100,
        }
        archivo_a = clones_state_dir(root) / "clone-a.json"
        archivo_a.write_text(json.dumps(payload_falso), encoding="utf-8")

        # 1. leer_runtime_clone_record debe rechazar la discrepancia
        with pytest.raises(RuntimeCloneCorruptError, match="no coincide con el namespace|difiere"):
            leer_runtime_clone_record(root, "clone-a")

        # 2. cargar_runtime_clone_record también debe fallar cerrado
        with pytest.raises(RuntimeCloneCorruptError, match="no coincide con el namespace|difiere"):
            cargar_runtime_clone_record(root, "clone-a")

        # 3. desired_runtime_from_storage debe propagar la corrupción y no redefinir identidad
        save_frozen_runtime_state(
            active_state_path(root),
            FrozenRuntimeState(
                schema_version=2,
                updated_at_ns=100,
                desired_active_generation=gen_id,
                desired_clone_id="clone-a",
            ),
        )
        with pytest.raises(RuntimeCloneCorruptError):
            desired_runtime_from_storage(root)

    def test_19_reparse_point_colgante_falla_cerrado_no_ausencia(self, tmp_path: pathlib.Path) -> None:
        """19. Symlink o junction colgante en state/clones/ lanza RuntimeCloneCorruptError, nunca None."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        _crear_generation_valida(root)

        clones_dir = clones_state_dir(root)
        target_inexistente = tmp_path / "inexistente_target.json"
        link_path = clones_dir / "clone-dangling.json"

        # Crear enlace colgante real si el SO lo permite (junction o symlink)
        creado = False
        if os.name == "nt":
            try:
                os.symlink(target_inexistente, link_path)
                creado = True
            except OSError:
                target_dir = tmp_path / "target_dir"
                target_dir.mkdir()
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)], capture_output=True)
                target_dir.rmdir()
                creado = link_path.is_symlink() or link_kind_or_raise(link_path) is not None
        else:
            try:
                link_path.symlink_to(target_inexistente)
                creado = True
            except OSError:
                pass

        if os.name == "nt":
            assert creado is True, "En Windows debe haberse ejercitado un junction o symlink NTFS real"

        if creado:
            assert not link_path.exists()
            assert link_kind_or_raise(link_path) is not None

            with pytest.raises(RuntimeCloneCorruptError, match="enlace/reparse"):
                cargar_runtime_clone_record(root, "clone-dangling")

            with pytest.raises(RuntimeCloneCorruptError, match="enlace/reparse"):
                leer_runtime_clone_record(root, "clone-dangling")
        else:
            import sky_claw.local.frozen_runtime.clones as clones_mod

            link_fake = clones_dir / "clone-dangling.json"
            assert not link_fake.exists()

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(clones_mod, "link_kind_or_raise", lambda p: "symlink" if p == link_fake else None)
                with pytest.raises(RuntimeCloneCorruptError, match="enlace/reparse"):
                    cargar_runtime_clone_record(root, "clone-dangling")

    def test_20_coherencia_escritor_valida_completamente_objeto(self, tmp_path: pathlib.Path) -> None:
        """20. registrar_runtime_clone_record valida completamente el objeto y no publica lo que el lector rechaza."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_coherence")

        # 1. schema_version inválido
        rec_bad_schema = RuntimeCloneRecord(
            schema_version=99,
            clone_id="clone-coherence",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneSchemaError, match="schema_version"):
            registrar_runtime_clone_record(root, rec_bad_schema)

        # 2. intended_runtime_setup_id vacío o con traversal
        rec_empty_setup = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-coherence",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneSchemaError, match="runtime_setup_id"):
            registrar_runtime_clone_record(root, rec_empty_setup)

        rec_traversal_setup = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-coherence",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="../escape",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneSchemaError, match="traversal"):
            registrar_runtime_clone_record(root, rec_traversal_setup)

        # 3. created_at_ns negativo o inválido
        rec_bad_created = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-coherence",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=-5,
        )
        with pytest.raises(RuntimeCloneSchemaError, match="created_at_ns"):
            registrar_runtime_clone_record(root, rec_bad_created)

    def test_21_escritor_roundtrip_normaliza_canonicamente_y_es_idempotente(self, tmp_path: pathlib.Path) -> None:
        """21. registrar_runtime_clone_record normaliza clone_id y setup_id canónicamente y es idempotente."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_alpha")

        # Registro con mayúsculas y espacios en setup_id
        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="Clone-Alpha",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="  setup-1  ",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )

        res = registrar_runtime_clone_record(root, rec)
        assert res.clone_id == "clone-alpha"
        assert res.intended_runtime_setup_id == "setup-1"

        # Archivo físico persistido debe coincidir con el ID canónico
        p = clone_record_path(root, "clone-alpha")
        assert p.exists()

        # Lectura inmediata recupera exactamente el mismo registro canónico
        rec_leido = leer_runtime_clone_record(root, "clone-alpha")
        assert rec_leido == res

        # Idempotencia: segundo registro con el objeto original debe devolver el registro canónico sin colisión
        res_idempotente = registrar_runtime_clone_record(root, rec)
        assert res_idempotente == res

    def test_22_fsm_actualizar_lifecycle_valida_argumentos_y_evita_estado_invalido(
        self, tmp_path: pathlib.Path
    ) -> None:
        """22. actualizar_lifecycle_clone valida verified_runtime_setup_id en todas las transiciones (CREATED/PROVISIONING/PROVISIONED -> INVALID)."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_fsm")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-fsm",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # 1. CREATED -> INVALID con path traversal en verified_runtime_setup_id debe fallar
        with pytest.raises(RuntimeCloneSchemaError, match="traversal|separadores"):
            actualizar_lifecycle_clone(
                root, "clone-fsm", RuntimeCloneLifecycle.INVALID, verified_runtime_setup_id="../escape"
            )

        # 2. CREATED -> PROVISIONING pasando verified_runtime_setup_id debe ser rechazado
        with pytest.raises(RuntimeCloneSchemaError, match="no admite verified_runtime_setup_id"):
            actualizar_lifecycle_clone(
                root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONING, verified_runtime_setup_id="setup-1"
            )

        # 3. Transición legítima CREATED -> PROVISIONING
        actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONING)

        # 4. PROVISIONING -> INVALID con caracteres inválidos
        with pytest.raises(RuntimeCloneSchemaError, match="traversal|separadores"):
            actualizar_lifecycle_clone(
                root, "clone-fsm", RuntimeCloneLifecycle.INVALID, verified_runtime_setup_id="foo/bar"
            )

        # 5. Transición legítima PROVISIONING -> PROVISIONED
        actualizar_lifecycle_clone(
            root, "clone-fsm", RuntimeCloneLifecycle.PROVISIONED, verified_runtime_setup_id="setup-1"
        )

        # 6. PROVISIONED -> INVALID con traversal debe ser rechazado
        with pytest.raises(RuntimeCloneSchemaError, match="traversal|separadores"):
            actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.INVALID, verified_runtime_setup_id="..")

        # 7. Transición legítima PROVISIONED -> INVALID preservando el setup existente
        rec_inv = actualizar_lifecycle_clone(root, "clone-fsm", RuntimeCloneLifecycle.INVALID)
        assert rec_inv.lifecycle is RuntimeCloneLifecycle.INVALID
        assert rec_inv.verified_runtime_setup_id == "setup-1"

        # Lectura posterior del registro persistido debe ser válida
        rec_leido = leer_runtime_clone_record(root, "clone-fsm")
        assert rec_leido == rec_inv

    def test_23_no_clobber_reparses_colgantes_y_reserva_exclusiva(self, tmp_path: pathlib.Path) -> None:
        """23. registrar_runtime_clone_record respeta no-clobber, falla cerrado ante reparse colgante o fallo de inspección."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_noclobber")

        clones_dir = clones_state_dir(root)
        clones_dir.mkdir(parents=True, exist_ok=True)

        rec1 = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-nc-1",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )

        # 1. Registro legítimamente ausente: creación funciona
        res1 = registrar_runtime_clone_record(root, rec1)
        assert res1 == rec1
        p1 = clone_record_path(root, "clone-nc-1")
        assert p1.exists()

        # 2. Registro existente idéntico: idempotencia
        res1_idem = registrar_runtime_clone_record(root, rec1)
        assert res1_idem == rec1

        # 3. Registro existente diferente: RuntimeCloneCollisionError
        rec1_diff = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-nc-1",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-different",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneCollisionError, match="no-clobber fail-closed"):
            registrar_runtime_clone_record(root, rec1_diff)

        # 4 & 5. Reparse point o enlace colgante en la ruta de registro
        rec_dangling = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-dangling",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        destino_dangling = clone_record_path(root, "clone-dangling")
        target_inexistente = tmp_path / "inexistente_target.json"

        creado = False
        if os.name == "nt":
            try:
                os.symlink(target_inexistente, destino_dangling)
                creado = True
            except OSError:
                target_dir = tmp_path / "target_dir_dangling"
                target_dir.mkdir()
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(destino_dangling), str(target_dir)], capture_output=True
                )
                target_dir.rmdir()
                creado = True
        else:
            try:
                destino_dangling.symlink_to(target_inexistente)
                creado = True
            except OSError:
                pass

        if os.name == "nt":
            assert creado is True, "En Windows debe haberse ejercitado un junction o symlink NTFS real"

        if creado:
            assert not destino_dangling.exists()
            assert link_kind_or_raise(destino_dangling) is not None

            # Debe fallar cerrado con RuntimeCloneCorruptError y NUNCA clobber/sustituir el enlace
            with pytest.raises(RuntimeCloneCorruptError, match="enlace/reparse"):
                registrar_runtime_clone_record(root, rec_dangling)

            assert link_kind_or_raise(destino_dangling) is not None, "El enlace colgante fue sustituido (clobber)"

        # 6. Fallos de inspección: OSError nunca equivale a ausencia legítima
        rec_insp = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-insp-fail",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        destino_insp = clone_record_path(root, "clone-insp-fail")
        assert not destino_insp.exists()

        import sky_claw.local.frozen_runtime.clones as clones_mod

        def _raise_io(path: pathlib.Path) -> str | None:
            if path == destino_insp:
                raise OSError("Fallo simulado de E/S de bajo nivel")
            return None

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(clones_mod, "link_kind_or_raise", _raise_io)
            with pytest.raises(RuntimeCloneCorruptError, match="inspeccionar"):
                registrar_runtime_clone_record(root, rec_insp)

        assert not destino_insp.exists()

    def test_24_admision_rechaza_rutas_con_puntos_relativos_y_evasion_steam(self, tmp_path: pathlib.Path) -> None:
        """24. admitir_clone_root_path rechaza traversal (..) y evasión de Steam; canonicaliza persistencia."""
        # 1. Evasión de steamapps mediante componentes relativos
        steam_base = tmp_path / "steamapps"
        steam_tmp = steam_base / "tmp"
        steam_common_skyrim = steam_base / "common" / "Skyrim"
        steam_common_skyrim.mkdir(parents=True, exist_ok=True)
        steam_tmp.mkdir(parents=True, exist_ok=True)

        evasion_path = steam_tmp / ".." / "common" / "Skyrim"
        with pytest.raises(RuntimeCloneAdmissionError, match="área administrada por Steam|relativos|no normalizados"):
            admitir_clone_root_path(evasion_path)

        # 2. Rutas con '..' en cualquier posición
        with pytest.raises(RuntimeCloneAdmissionError, match="relativos|no normalizados"):
            admitir_clone_root_path(tmp_path / "foo" / ".." / "bar")

        # 3. Rutas con '.' explícito en string o path
        with pytest.raises(RuntimeCloneAdmissionError, match="relativos|no normalizados"):
            admitir_clone_root_path(f"{tmp_path}/foo/./bar")

        # 4. registrar_runtime_clone_record persiste la ruta canonicalizada
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_canonical")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-canon",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.CREATED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id=None,
            root_path=str(clone_root).replace("/", "\\") if os.name == "nt" else str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        guardado = registrar_runtime_clone_record(root, rec)
        leido = leer_runtime_clone_record(root, "clone-canon")
        assert guardado.root_path == leido.root_path
        # El archivo JSON en disco debe almacenar la ruta canónica normalizada
        raw_json = json.loads((clones_state_dir(root) / "clone-canon.json").read_text(encoding="utf-8"))
        assert raw_json["root_path"] == str(pathlib.Path(clone_root).resolve()) or raw_json[
            "root_path"
        ] == os.path.normpath(str(clone_root))

    def test_25_admision_y_verificacion_rechazan_reparses_intermedios_e_internos(self, tmp_path: pathlib.Path) -> None:
        """25. admitir_clone_root_path y verificar_runtime_clone rechazan reparse points intermedios e internos."""
        # 1. Reparse point en componente ancestro intermedio
        real_parent = tmp_path / "real_parent"
        real_parent.mkdir()
        junc_parent = tmp_path / "junc_parent"

        creado_junc = False
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junc_parent), str(real_parent)],
                capture_output=True,
            )
            if res.returncode == 0:
                creado_junc = True
        else:
            try:
                junc_parent.symlink_to(real_parent, target_is_directory=True)
                creado_junc = True
            except OSError:
                pass

        if creado_junc:
            c_target = junc_parent / "clone1"
            with pytest.raises(RuntimeCloneAdmissionError, match="reparse point/enlace"):
                admitir_clone_root_path(c_target)

        # 2. Reparse point insertado dentro del namespace interno del Clone
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_internal_reparse")

        # Insertar un subdirectorio con symlink o junction
        target_ext = tmp_path / "target_ext"
        target_ext.mkdir()
        internal_link = clone_root / "Data" / "internal_junc"

        creado_int = False
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(internal_link), str(target_ext)],
                capture_output=True,
            )
            if res.returncode == 0:
                creado_int = True
        else:
            try:
                internal_link.symlink_to(target_ext, target_is_directory=True)
                creado_int = True
            except OSError:
                pass

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-int-rep",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        if creado_int:
            verif = verificar_runtime_clone(root, "clone-int-rep")
            assert verif.state is GenerationVerificationState.INVALID
            assert (
                "reparse" in verif.message.lower()
                or "enlace" in verif.message.lower()
                or "física" in verif.message.lower()
            )

    def test_26_aislamiento_fisico_rechaza_clone_con_hardlinks_y_objetos_compartidos(
        self, tmp_path: pathlib.Path
    ) -> None:
        """26. verificar_runtime_clone rechaza hardlinks (st_nlink > 1) y objetos compartidos (SFR-18, ADR 0012 §35.4)."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_hardlink")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-hl",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # 1. Hardlink en archivo crítico (SkyrimSE.exe hacia la Generation)
        from sky_claw.local.frozen_runtime.storage import generation_dir

        gen_exe = generation_dir(root, gen_id) / "SkyrimSE.exe"
        clone_exe = clone_root / "SkyrimSE.exe"
        clone_exe.unlink()

        creado_hl = False
        try:
            os.link(gen_exe, clone_exe)
            creado_hl = True
        except OSError:
            pass

        if creado_hl:
            assert clone_exe.stat().st_nlink > 1
            verif = verificar_runtime_clone(root, "clone-hl")
            # Un clon con hardlinks a la Generation JAMÁS puede ser VALID
            assert verif.state is GenerationVerificationState.INVALID
            assert (
                "hardlink" in verif.message.lower()
                or "compartido" in verif.message.lower()
                or "física" in verif.message.lower()
            )

            # desired_runtime_from_storage debe resolver clone_root=None
            active_state = FrozenRuntimeState(
                schema_version=2,
                desired_active_generation=gen_id,
                desired_clone_id="clone-hl",
                updated_at_ns=100,
            )
            save_frozen_runtime_state(active_state_path(root), active_state)
            desired = desired_runtime_from_storage(root)
            assert desired.clone_root is None

        # 2. Hardlink en archivo auxiliar no crítico (Data/aux.txt hacia archivo externo)
        clone_root_2 = _crear_arbol_juego(tmp_path / "clone_aux_hl")
        rec2 = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-aux-hl",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root_2),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec2)

        aux_orig = tmp_path / "aux_external.txt"
        aux_orig.write_text("external content", encoding="utf-8")
        aux_link = clone_root_2 / "Data" / "aux_linked.txt"
        creado_aux = False
        try:
            os.link(aux_orig, aux_link)
            creado_aux = True
        except OSError:
            pass

        if creado_aux:
            assert aux_link.stat().st_nlink > 1
            verif2 = verificar_runtime_clone(root, "clone-aux-hl")
            assert verif2.state is GenerationVerificationState.INVALID
            assert "hardlink" in verif2.message.lower() or "física" in verif2.message.lower()

    def test_27_lineage_binding_metadata_generation_incoherente_rechazada(self, tmp_path: pathlib.Path) -> None:
        """27. leer_generation_metadata y verificar_runtime_clone exigen vinculación estricta de generation_id."""
        from sky_claw.local.frozen_runtime.generations import (
            generations_state_dir,
            leer_generation_metadata,
        )

        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_lineage")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-lineage",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # Adulterar metadata de gen_id en disco para que declare "gen-fake"
        meta_file = generations_state_dir(root) / f"{gen_id}.json"
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        data["generation_id"] = "1.6.1170__000000000000"
        meta_file.write_text(json.dumps(data), encoding="utf-8")

        # 1. leer_generation_metadata debe fallar cerrado con StateCorruptError
        with pytest.raises(StateCorruptError, match="coincide|namespace"):
            leer_generation_metadata(root, gen_id)

        # 2. verificar_runtime_clone debe retornar INVALID
        verif = verificar_runtime_clone(root, "clone-lineage")
        assert verif.state is GenerationVerificationState.INVALID

        # 3. desired_runtime_from_storage resuelve clone_root=None
        active_state = FrozenRuntimeState(
            schema_version=2,
            desired_active_generation=gen_id,
            desired_clone_id="clone-lineage",
            updated_at_ns=100,
        )
        save_frozen_runtime_state(active_state_path(root), active_state)
        desired = desired_runtime_from_storage(root)
        assert desired.clone_root is None

    def test_28_state_schema_versioning_estricto_sin_downgrade_silencioso(self, tmp_path: pathlib.Path) -> None:
        """28. _save_frozen_runtime_state_locked rechaza schemas no soportados y no degrada silenciosamente a v1."""
        from sky_claw.local.frozen_runtime.state import (
            load_frozen_runtime_state,
            save_frozen_runtime_state,
        )

        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)

        # Inicializar estado legítimo v2
        active_v2 = FrozenRuntimeState(
            schema_version=2,
            desired_active_generation="1.6.1170__abcdef123456",
            updated_at_ns=1000,
            desired_clone_id="clone-v2",
        )
        save_frozen_runtime_state(active_state_path(root), active_v2)

        # Intentar escribir estados con schema_version inválido o no soportado
        for bad_schema in (99, True, -1, 0, 3, "2"):
            bad_state = FrozenRuntimeState(
                schema_version=bad_schema,  # type: ignore[arg-type]
                desired_active_generation="1.6.1170__abcdef123456",
                updated_at_ns=2000,
                desired_clone_id="clone-v2",
            )
            with pytest.raises(StateSchemaError, match="schema_version"):
                save_frozen_runtime_state(active_state_path(root), bad_state)

        # Verificar que active.json v2 se mantiene intacto sin downgrade a v1
        cargado = load_frozen_runtime_state(active_state_path(root))
        assert cargado.state is not None
        assert cargado.state.schema_version == 2
        assert cargado.state.desired_generation_id == "1.6.1170__abcdef123456"
        assert cargado.state.desired_clone_id == "clone-v2"
        assert cargado.state.updated_at_ns == 1000

    def test_29_verificar_runtime_clone_exige_source_generation_valida_sin_drift(self, tmp_path: pathlib.Path) -> None:
        """29. Codex: verificar_runtime_clone exige Generation de origen VALID; INVALID si se borra o sufre drift."""
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)
        clone_root = _crear_arbol_juego(tmp_path / "clone_live")

        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-drift-check",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(clone_root),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # Baseline: cuando la Generation está intacta, el clone es VALID
        verif_ok = verificar_runtime_clone(root, "clone-drift-check")
        assert verif_ok.state is GenerationVerificationState.VALID

        # Caso A: Se borra el directorio versions/<gen_id> dejando metadata huérfana
        gen_dir = root / "versions" / gen_id
        shutil.rmtree(gen_dir)
        verif_missing = verificar_runtime_clone(root, "clone-drift-check")
        assert verif_missing.state is GenerationVerificationState.INVALID
        assert "no es VALID" in verif_missing.message or "no existe" in verif_missing.message

        # Re-crear generation y causar drift (Caso B)
        _crear_arbol_juego(gen_dir)
        # Modificar un archivo crítico para forzar DRIFTED en la Generation
        (gen_dir / "SkyrimSE.exe").write_bytes(b"SkyrimSE-mutated-content")
        verif_drift = verificar_runtime_clone(root, "clone-drift-check")
        assert verif_drift.state is GenerationVerificationState.INVALID
        assert "no es VALID" in verif_drift.message

        # desired_runtime_from_storage resuelve clone_root=None ante Generation en drift
        active_state = FrozenRuntimeState(
            schema_version=2,
            desired_active_generation=gen_id,
            desired_clone_id="clone-drift-check",
            updated_at_ns=100,
        )
        save_frozen_runtime_state(active_state_path(root), active_state)
        desired = desired_runtime_from_storage(root)
        assert desired.clone_root is None

    def test_30_admision_y_lectura_rechazan_reparse_en_ancestros_intermedios(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """30. Qodo: admitir_clone_root_path y _leer_record_de_ruta rechazan reparse points en ancestros intermedios."""
        from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace

        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gen_id = _crear_generation_valida(root)

        # Simular reparse point en un ancestro intermedio de la ruta del clone
        intermedio = tmp_path / "ancestor_reparse"
        clone_sub = intermedio / "sub" / "clone"
        clone_sub.mkdir(parents=True)

        original_desc = descripcion_de_enlace

        def _mock_desc(path: pathlib.Path | os.PathLike[str]) -> str | None:
            p = pathlib.Path(path)
            if p == intermedio:
                return "junction"
            return original_desc(path)

        monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.descripcion_de_enlace", _mock_desc)

        # admitir_clone_root_path debe rechazar la ruta del clone si un ancestro es un reparse point
        with pytest.raises(RuntimeCloneAdmissionError, match="reparse point/enlace"):
            admitir_clone_root_path(clone_sub, storage_root=root)

        # Registrar un clone legítimo antes de simular redirección en state
        rec = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-reparse-ancestro",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(_crear_arbol_juego(tmp_path / "clone_normal")),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        registrar_runtime_clone_record(root, rec)

        # _leer_record_de_ruta debe rechazar la lectura si un ancestro de state/clones/ es un reparse point
        def _mock_desc_state(path: pathlib.Path | os.PathLike[str]) -> str | None:
            p = pathlib.Path(path)
            if p == root / "state":
                return "junction"
            return original_desc(path)

        monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.descripcion_de_enlace", _mock_desc_state)

        with pytest.raises(RuntimeCloneCorruptError, match="namespace de clones redirigido"):
            leer_runtime_clone_record(root, "clone-reparse-ancestro")

        # Intentar registrar otro clone bajo state redirigido también debe fallar cerrado
        rec_nuevo = RuntimeCloneRecord(
            schema_version=1,
            clone_id="clone-segundo",
            source_generation_id=gen_id,
            lifecycle=RuntimeCloneLifecycle.PROVISIONED,
            intended_runtime_setup_id="setup-1",
            verified_runtime_setup_id="setup-1",
            root_path=str(_crear_arbol_juego(tmp_path / "clone_normal2")),
            admitted_role="runtime_clone",
            created_at_ns=100,
        )
        with pytest.raises(RuntimeCloneCorruptError, match="namespace de clones redirigido"):
            registrar_runtime_clone_record(root, rec_nuevo)
