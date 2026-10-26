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
    clones_state_dir,
    initialize_frozen_runtime_storage,
)
from sky_claw.local.frozen_runtime.storage_models import (
    FrozenRuntimeState,
    GenerationVerificationState,
)
from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest

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
    gen_dir = root / "versions" / f"1.6.1170__{digest_prefix}"
    _crear_arbol_juego(gen_dir, version=version)
    full_digest = (digest_prefix * 6)[:64]
    exe_bytes = f"SkyrimSE-{version}".encode()
    crit_digest = hashlib.sha256(exe_bytes).hexdigest()
    crit = FileIdentity(rel_path="SkyrimSE.exe", size=len(exe_bytes), digest=crit_digest)
    evidencia = SourceSnapshotEvidence(
        observed_at_ns=1000,
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        files=(crit,),
        critical_files=(crit,),
        provider_metadata=ProviderMetadataObservation(
            provider=ManagedSourceProvider.STEAM,
            appid="489830",
            buildid="13189953",
        ),
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version=version),
        tree_digest=TreeDigest(digest=full_digest, files=1, bytes=100),
    )
    meta = registrar_generation_metadata(root, evidencia)
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
