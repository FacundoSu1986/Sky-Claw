"""Contratos y attestation del broker de ejecución bajo USVFS (F8)."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    verify_vfs_attestation,
)
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_SESSION_EVENTS,
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsJobResult,
    VfsProtocolError,
    VfsSessionEventError,
    VfsToolExitEvent,
    VfsToolStartedEvent,
    parse_worker_event,
)


def _crear_perfil(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    mo2_root = tmp_path / "MO2"
    profile = mo2_root / "profiles" / "Default"
    mod = mo2_root / "mods" / "CanaryMod"
    data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n-DisabledMod\n", encoding="utf-8-sig")
    (mod / "SKSE" / "Plugins").mkdir(parents=True)
    (mod / "SKSE" / "Plugins" / "skyclaw-canary.txt").write_bytes(b"canary-v1")
    return mo2_root, profile, data


def test_vfs_job_roundtrip_con_allowlist_y_contrato_canonico(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "profiles" / "Default" / "plugins.txt"
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={"update_masterlist": False},
        timeout_seconds=120.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(target,),
    )

    restored = VfsJob.from_dict(job.to_dict())

    assert restored == job
    assert restored.protocol_version == VFS_PROTOCOL_VERSION
    assert restored.job_id
    assert restored.mutation_targets == (target.resolve(),)


def test_vfs_job_rechaza_tool_arbitraria() -> None:
    with pytest.raises(VfsProtocolError, match="tool_id no permitido"):
        VfsJob.create(
            instance_id="portable-main",
            profile="Default",
            tool_id="powershell.exe",
            payload={},
            timeout_seconds=30.0,
            expected_fingerprint="a" * 64,
            mutation_targets=(),
        )


def test_vfs_job_rechaza_version_de_protocolo_incompatible(tmp_path: pathlib.Path) -> None:
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=30.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(tmp_path / "target",),
    ).to_dict()
    job["protocol_version"] = VFS_PROTOCOL_VERSION + 1

    with pytest.raises(VfsProtocolError, match="versión de protocolo"):
        VfsJob.from_dict(job)


def test_vfs_job_rechaza_job_id_con_traversal(tmp_path: pathlib.Path) -> None:
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=30.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(tmp_path / "target",),
    ).to_dict()
    job["job_id"] = "../fuera"

    with pytest.raises(VfsProtocolError, match="job_id"):
        VfsJob.from_dict(job)


def test_vfs_job_result_exige_success_y_message_canonicos() -> None:
    result = VfsJobResult.from_dict(
        {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "job_id": "job-1",
            "success": False,
            "message": "perfil incorrecto",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "outputs": [],
            "rollback_state": "not_started",
            "attestation": None,
        }
    )

    assert result.success is False
    assert result.message == "perfil incorrecto"
    assert VfsJobResult.from_dict(result.to_dict()) == result


def test_attestation_elige_canary_ausente_de_data_fisico(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)

    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )

    assert challenge.relative_path == pathlib.PurePosixPath("SKSE/Plugins/skyclaw-canary.txt")
    assert challenge.source_mod == "CanaryMod"
    assert challenge.sha256
    assert not (physical_data / pathlib.Path(*challenge.relative_path.parts)).exists()

    assert VfsAttestationChallenge.from_dict(challenge.to_dict()) == challenge


def test_attestation_challenge_rechaza_ruta_relativa_con_traversal() -> None:
    with pytest.raises(VfsAttestationError, match="relative_path"):
        VfsAttestationChallenge.from_dict(
            {
                "profile": "Default",
                "source_mod": "CanaryMod",
                "relative_path": "../fuera.txt",
                "sha256": "a" * 64,
                "profile_fingerprint": "b" * 64,
            }
        )


def test_attestation_rechaza_worker_fuera_de_usvfs(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)
    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )

    with pytest.raises(VfsAttestationError, match="canary no visible"):
        verify_vfs_attestation(
            challenge=challenge,
            mo2_root=mo2_root,
            profile="Default",
            virtual_data_dir=physical_data,
        )


def test_attestation_acepta_vista_virtual_y_detecta_drift(tmp_path: pathlib.Path) -> None:
    mo2_root, profile, physical_data = _crear_perfil(tmp_path)
    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )
    virtual_data = tmp_path / "VistaVirtual" / "Data"
    virtual_canary = virtual_data / pathlib.Path(*challenge.relative_path.parts)
    virtual_canary.parent.mkdir(parents=True)
    virtual_canary.write_bytes(b"canary-v1")

    proof = verify_vfs_attestation(
        challenge=challenge,
        mo2_root=mo2_root,
        profile="Default",
        virtual_data_dir=virtual_data,
    )

    assert proof.profile_fingerprint == challenge.profile_fingerprint
    assert proof.visible_sha256 == challenge.sha256

    (profile / "modlist.txt").write_text("-CanaryMod\n", encoding="utf-8-sig")
    with pytest.raises(VfsAttestationError, match="fingerprint"):
        verify_vfs_attestation(
            challenge=challenge,
            mo2_root=mo2_root,
            profile="Default",
            virtual_data_dir=virtual_data,
        )


def test_attestation_falla_cerrado_sin_canary_elegible(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)
    physical_canary = physical_data / "SKSE" / "Plugins" / "skyclaw-canary.txt"
    physical_canary.parent.mkdir(parents=True)
    physical_canary.write_bytes(b"vanilla-copy")

    with pytest.raises(VfsAttestationError, match="canary elegible"):
        build_attestation_challenge(
            mo2_root=mo2_root,
            profile="Default",
            physical_data_dir=physical_data,
        )


# ---------------------------------------------------------------------------
# Eventos mid-job de sesión (PR-586A)
# ---------------------------------------------------------------------------


def _evento_started(**campos: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol_version": VFS_PROTOCOL_VERSION,
        "type": "event",
        "event": "tool_started",
        "job_id": "job-1",
        "tool_pid": 4242,
    }
    base.update(campos)
    return base


def _evento_exit(**campos: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol_version": VFS_PROTOCOL_VERSION,
        "type": "event",
        "event": "tool_exit",
        "job_id": "job-1",
        "exit_code": 0,
    }
    base.update(campos)
    return base


def test_evento_tool_started_roundtrip_canonico() -> None:
    evento = VfsToolStartedEvent(job_id="job-1", tool_pid=4242)

    assert evento.to_dict() == _evento_started()
    assert parse_worker_event(evento.to_dict()) == evento


def test_evento_tool_exit_roundtrip_canonico() -> None:
    evento = VfsToolExitEvent(job_id="job-1", exit_code=7)

    assert evento.to_dict() == _evento_exit(exit_code=7)
    assert parse_worker_event(evento.to_dict()) == evento


def test_eventos_de_sesion_permitidos_congelados() -> None:
    """Ancla de enumeración: agregar un evento de sesión exige decisión explícita.

    Sin esto, un ``magic_execute`` nuevo entraría a la allowlist sin romper nada,
    que es exactamente la superficie que el allowlist de tool_ids existe para
    cerrar (ADR 0007).
    """
    assert frozenset({"tool_started", "tool_exit"}) == ALLOWED_VFS_SESSION_EVENTS


@pytest.mark.parametrize("exit_code", [-(2**31), -15, -1, 0, 1, 255, 2**32 - 1])
def test_exit_code_admite_el_rango_de_windows_y_posix(exit_code: int) -> None:
    """Windows entrega DWORD sin signo; POSIX entrega ``-señal``."""
    parseado = parse_worker_event(_evento_exit(exit_code=exit_code))

    assert isinstance(parseado, VfsToolExitEvent)
    assert parseado.exit_code == exit_code


@pytest.mark.parametrize(
    "crudo",
    [
        pytest.param(_evento_started(tool_pid=0), id="pid-cero"),
        pytest.param(_evento_started(tool_pid=-3), id="pid-negativo"),
        pytest.param(_evento_started(tool_pid=True), id="pid-bool"),
        pytest.param(_evento_started(tool_pid=False), id="pid-bool-false"),
        pytest.param(_evento_started(tool_pid=1.5), id="pid-float"),
        pytest.param(_evento_started(tool_pid="12"), id="pid-string"),
        pytest.param(_evento_started(tool_pid=None), id="pid-nulo"),
        pytest.param(_evento_started(tool_pid=2**32), id="pid-fuera-de-rango"),
        pytest.param(_evento_started(extra="no"), id="campo-inesperado"),
        pytest.param(_evento_started(job_id="../fuera"), id="job-id-traversal"),
        pytest.param(_evento_started(job_id=""), id="job-id-vacio"),
        pytest.param(_evento_exit(exit_code=True), id="exit-bool"),
        pytest.param(_evento_exit(exit_code="0"), id="exit-string"),
        pytest.param(_evento_exit(exit_code=1.5), id="exit-float"),
        pytest.param(_evento_exit(exit_code=None), id="exit-nulo"),
        pytest.param(_evento_exit(exit_code=2**32), id="exit-fuera-de-rango"),
        pytest.param(_evento_exit(extra="no"), id="exit-campo-inesperado"),
        pytest.param(
            {"protocol_version": VFS_PROTOCOL_VERSION, "type": "event", "event": "tool_started", "job_id": "job-1"},
            id="started-sin-pid",
        ),
        pytest.param(
            {"protocol_version": VFS_PROTOCOL_VERSION, "type": "event", "event": "tool_exit", "job_id": "job-1"},
            id="exit-sin-codigo",
        ),
        pytest.param(
            {"protocol_version": VFS_PROTOCOL_VERSION, "type": "event", "event": "magic_execute", "job_id": "job-1"},
            id="evento-desconocido",
        ),
        pytest.param(_evento_started(type="job_result"), id="tipo-incorrecto"),
        pytest.param(_evento_started(protocol_version=VFS_PROTOCOL_VERSION + 1), id="version-incompatible"),
        pytest.param(_evento_started(protocol_version=True), id="version-bool"),
        pytest.param([], id="no-es-objeto"),
    ],
)
def test_evento_de_sesion_malformado_falla_cerrado(crudo: object) -> None:
    with pytest.raises(VfsSessionEventError):
        parse_worker_event(crudo)


def test_evento_de_sesion_malformado_expone_la_causa_como_protocolo() -> None:
    """``VfsSessionEventError`` es un ``VfsProtocolError``: los call sites que ya
    capturan la familia amplia siguen funcionando."""
    assert issubclass(VfsSessionEventError, VfsProtocolError)

    with pytest.raises(VfsProtocolError):
        parse_worker_event(_evento_started(tool_pid=True))
