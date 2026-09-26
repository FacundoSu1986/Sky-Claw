"""Binding gate→spawn del handoff TexGen → DynDOLOD (cierre TOCTOU, PR-586D).

La ventana que estos tests cierran es explícita::

    verify_texgen_handoff() PASS
    → profile/artifact cambia (drift)
    → run_dyndolod() / spawn()
    → SESIÓN/PROCESO sobre un estado que NADIE aprobó

El gate aprueba un ESTADO (``TexGenHandoffApproval``) y ``spawn()`` lo
REVALIDA completo, fail-closed, en su boundary — nunca un challenge
reconstruido a ciegas que certifique el estado nuevo. Los tests atraviesan la
strategy REAL (gate real + spawn real) con un broker falso cuyo ``submit`` y
``open_session`` corren la ``verify_vfs_attestation`` REAL, como el worker
productivo; ``run_dyndolod`` jamás se mockea de forma que saltee ``spawn()``.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

import pytest

from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    verify_vfs_attestation,
)
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsJobResult
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODRunner,
    ReadinessMode,
)
from sky_claw.local.tools.texgen_handoff import (
    HandoffDriftError,
    TexGenHandoffApproval,
    TexGenHandoffRequest,
)

_MOD = "TexGen Output"
_PERFIL = "Perfil-A"
_BYTES_A = b"CURRENT!"
_BYTES_B = b"CURRENT-SUB"


# =============================================================================
# Escenario MO2/USVFS real (mismo layout que test_texgen_handoff_gate)
# =============================================================================


class _Escenario:
    def __init__(
        self,
        *,
        data_root: pathlib.Path,
        mods_dir: pathlib.Path,
        physical_data: pathlib.Path,
        virtual_data: pathlib.Path,
        artifact_root: pathlib.Path,
    ) -> None:
        self.data_root = data_root
        self.mods_dir = mods_dir
        self.physical_data = physical_data
        self.virtual_data = virtual_data
        self.artifact_root = artifact_root

    def montar_vista_virtual(self) -> None:
        """La vista USVFS del perfil: el overlay entrega los bytes del mod."""
        dest = self.virtual_data / "textures"
        (dest / "sub").mkdir(parents=True, exist_ok=True)
        (dest / "a.dds").write_bytes(_BYTES_A)
        (dest / "sub" / "b.dds").write_bytes(_BYTES_B)

    def deshabilitar_mod(self) -> None:
        self._escribir_modlist(["+OtroMod"])  # sin la línea '+' del mod pedido

    def habilitar_mod(self) -> None:
        self._escribir_modlist(["+OtroMod", f"+{_MOD}"])

    def con_prioridad(self, lineas: list[str]) -> None:
        self._escribir_modlist(lineas)

    def _escribir_modlist(self, lineas: list[str]) -> None:
        # read_enabled_mods: el orden del archivo crece de menor a mayor
        # prioridad (la última línea habilitada es la de mayor prioridad).
        (self.data_root / "profiles" / _PERFIL / "modlist.txt").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def _escenario(tmp_path: pathlib.Path) -> _Escenario:
    data_root = tmp_path / "MO2"
    mods_dir = data_root / "mods"
    profile_dir = data_root / "profiles" / _PERFIL
    profile_dir.mkdir(parents=True)
    (profile_dir / "plugins.txt").write_text("", encoding="utf-8")
    (mods_dir / "OtroMod").mkdir(parents=True)
    (mods_dir / _MOD).mkdir(parents=True)
    (mods_dir / _MOD / "meta.ini").write_text("[General]\n", encoding="utf-8")
    artifact_root = mods_dir / _MOD / "textures"
    (artifact_root / "sub").mkdir(parents=True)
    (artifact_root / "a.dds").write_bytes(_BYTES_A)
    (artifact_root / "sub" / "b.dds").write_bytes(_BYTES_B)
    physical_data = tmp_path / "game" / "Data"
    physical_data.mkdir(parents=True)
    virtual_data = tmp_path / "game" / "DataVista"
    virtual_data.mkdir()
    esc = _Escenario(
        data_root=data_root,
        mods_dir=mods_dir,
        physical_data=physical_data,
        virtual_data=virtual_data,
        artifact_root=artifact_root,
    )
    # modlist inicial: +TexGen Output (mayor prioridad) sobre +OtroMod.
    esc.con_prioridad(["+OtroMod", f"+{_MOD}"])
    esc.montar_vista_virtual()
    return esc


# =============================================================================
# Broker falso: mismo contrato que execute_worker_manifest (attestation REAL)
# =============================================================================


def _resultado(**campos: Any) -> VfsJobResult:
    return VfsJobResult(
        protocol_version=1,
        job_id="job-falso",
        success=campos.get("success", True),
        message=campos.get("message", ""),
        exit_code=campos.get("exit_code", 0),
        stdout=campos.get("stdout", ""),
        stderr=campos.get("stderr", ""),
        outputs=(),
        rollback_state="not_rolled_back",
        attestation=campos.get("attestation"),
        tool_result={},
    )


class _SesionFalsa:
    """VfsProcessSession mínimo para un spawn feliz."""

    def __init__(self) -> None:
        self._resultado = _resultado(exit_code=0)
        self.cancelada = False

    @property
    def pid(self) -> int:
        return 4242

    @property
    def returncode(self) -> int | None:
        return 0

    async def wait(self) -> int:
        return 0

    async def result(self) -> VfsJobResult:
        return self._resultado

    async def cancel(self) -> None:
        self.cancelada = True


class _BridgeFalso:
    """Broker falso cuyo ``submit``/``open_session`` atestiguan REAL.

    ``despues_de_verificar`` simula el drift del mundo real: el worker atestigua
    el estado F1, y el estado muta ANTES de que el gate devuelva el PASS (o de
    que el spawn se abra) — exactamente la ventana TOCTOU.
    """

    def __init__(
        self,
        *,
        despues_de_verificar: Callable[[], None] | None = None,
    ) -> None:
        self.despues_de_verificar = despues_de_verificar
        self.probes = 0
        self.sessions: list[Any] = []
        self.challenges: list[VfsAttestationChallenge] = []

    async def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsJobResult:
        self.probes += 1
        proof = verify_vfs_attestation(
            challenge=challenge,
            data_root=data_root,
            mods_dir=mods_dir,
            profile=job.profile,
            virtual_data_dir=virtual_data_dir,
        )
        if self.despues_de_verificar is not None:
            self.despues_de_verificar()
        atestacion = dict(proof.to_dict())
        atestacion["grandchild_sha256"] = challenge.sha256
        return _resultado(attestation=atestacion)

    async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:
        challenge: VfsAttestationChallenge = kwargs["challenge"]
        self.challenges.append(challenge)
        # El worker productivo atesta ANTES de despachar la herramienta
        # (execute_worker_manifest): el mapping runtime se re-prueba en el
        # boundary del spawn, no sólo en el gate.
        verify_vfs_attestation(
            challenge=challenge,
            data_root=kwargs.get("data_root"),
            mods_dir=kwargs.get("mods_dir"),
            profile=job.profile,
            virtual_data_dir=kwargs["virtual_data_dir"],
        )
        sesion = _SesionFalsa()
        self.sessions.append(sesion)
        return sesion


def _strategy(esc: _Escenario, bridge: Any, tmp_path: pathlib.Path) -> BrokeredDynDOLODSpawnStrategy:
    work = tmp_path / "Work Root"
    return BrokeredDynDOLODSpawnStrategy(
        broker=bridge,  # type: ignore[arg-type]
        instance_id="instancia-1",
        profile=_PERFIL,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        install_root=tmp_path / "MO2" / "install",
        physical_data_dir=esc.physical_data,
        virtual_data_dir=esc.virtual_data,
        output_roots={"texgen": work / "DynDOLOD" / "TexGen", "dyndolod": work / "DynDOLOD" / "DynDOLOD"},
    )


def _request(esc: _Escenario) -> TexGenHandoffRequest:
    return TexGenHandoffRequest(
        mod_name=_MOD,
        staging=esc.artifact_root,
        data_dir=esc.virtual_data,
        expected_profile=_PERFIL,
        authorized=None,
    )


async def _gate_pasa(esc: _Escenario, bridge: _BridgeFalso, tmp_path: pathlib.Path) -> TexGenHandoffApproval:
    """verify_texgen_handoff REAL devuelve PASS con el approval ligado."""
    resultado = await _strategy(esc, bridge, tmp_path).verify_texgen_handoff(_request(esc))
    assert resultado.verified is True, resultado.reason
    assert resultado.approval is not None
    assert bridge.probes == 1
    return resultado.approval


def _exe_dyndolod(tmp_path: pathlib.Path) -> pathlib.Path:
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir(exist_ok=True)
    exe = exe_dir / "DynDOLODx64.exe"
    exe.touch()
    return exe


async def _spawn(
    esc: _Escenario,
    bridge: _BridgeFalso,
    tmp_path: pathlib.Path,
    approval: TexGenHandoffApproval,
) -> Any:
    return await _strategy(esc, bridge, tmp_path).spawn(
        executable=_exe_dyndolod(tmp_path),
        args=[],
        tool_name="DynDOLOD",
        cwd=tmp_path,
        timeout=30.0,
        handoff=approval,
    )


def _archivo_no_canary(approval: TexGenHandoffApproval) -> pathlib.Path:
    """Un archivo del artifact que NO es el canary elegido por el gate."""
    canary = approval.canary_relative_path
    assert canary is not None
    rel = pathlib.Path(*canary.parts[1:])  # quita el prefijo Data ("textures")
    candidatos = [
        p for p in approval.artifact_root.rglob("*") if p.is_file() and p.relative_to(approval.artifact_root) != rel
    ]
    assert candidatos, "el escenario necesita al menos un archivo no-canary"
    return candidatos[0]


# =============================================================================
# Los 5 escenarios del review: PASS → drift → spawn → BLOCK
# =============================================================================


@pytest.mark.asyncio
async def test_1_pass_luego_deshabilitar_texgen_output_bloquea_el_spawn(tmp_path: pathlib.Path) -> None:
    """PASS → `+TexGen Output` deshabilitado → spawn → BLOCK (sin sesión)."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    esc.deshabilitar_mod()

    with pytest.raises(HandoffDriftError, match="habilitado"):
        await _spawn(esc, bridge, tmp_path, approval)
    assert bridge.sessions == [], "el spawn bloqueado jamás abre una sesión"


@pytest.mark.asyncio
async def test_2_pass_luego_cambiar_el_modlist_bloquea_el_spawn(tmp_path: pathlib.Path) -> None:
    """PASS → prioridad del modlist cambiada → spawn → BLOCK (fingerprint)."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    # TexGen Output pasa a prioridad MENOR: el fingerprint del perfil cambia.
    esc.con_prioridad([f"+{_MOD}", "+OtroMod"])

    with pytest.raises(HandoffDriftError):
        await _spawn(esc, bridge, tmp_path, approval)
    assert bridge.sessions == []


@pytest.mark.asyncio
async def test_3_pass_luego_mutar_archivo_no_canary_bloquea_el_spawn(tmp_path: pathlib.Path) -> None:
    """PASS → un archivo NO-canary del artifact mutado → spawn → BLOCK."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    objetivo = _archivo_no_canary(approval)
    objetivo.write_bytes(b"BYTE-MUTADO-DESPUES-DEL-PASE")

    with pytest.raises(HandoffDriftError, match="cambió después de la aprobación"):
        await _spawn(esc, bridge, tmp_path, approval)
    assert bridge.sessions == []


@pytest.mark.asyncio
async def test_4_pass_luego_override_incompatible_bloquea_el_spawn(tmp_path: pathlib.Path) -> None:
    """PASS → `overwrite` con bytes distintos sobre un path del artifact → BLOCK."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    objetivo = _archivo_no_canary(approval)
    rel = objetivo.relative_to(approval.artifact_root)
    override = esc.data_root / "overwrite" / approval.artifact_root.name / rel
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_bytes(b"BYTES-DEL-OVERWRITE")

    with pytest.raises(HandoffDriftError, match="efectividad"):
        await _spawn(esc, bridge, tmp_path, approval)
    assert bridge.sessions == [], "un override nuevo no habilita el spawn aprobado"


@pytest.mark.asyncio
async def test_5_sin_drift_el_spawn_brokered_se_abre(tmp_path: pathlib.Path) -> None:
    """Sin drift el spawn se abre, y la sesión atestigua el MISMO estado aprobado."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    proceso = await _spawn(esc, bridge, tmp_path, approval)

    assert len(bridge.sessions) == 1
    challenge = bridge.challenges[0]
    assert challenge.profile_fingerprint == approval.profile_fingerprint
    assert challenge.relative_path == approval.canary_relative_path
    assert challenge.sha256 == approval.canary_sha256
    assert challenge.source_mod == _MOD
    assert proceso.pid == 4242


# =============================================================================
# El runner THREADA el approval hasta spawn (run_dyndolod NUNCA se saltea)
# =============================================================================


@pytest.mark.asyncio
async def test_run_full_pipeline_no_spawnea_sobre_estado_posterior_al_pase(tmp_path: pathlib.Path) -> None:
    """El drift entre el PASS del gate y el spawn bloquea la corrida completa.

    El broker falso muta el modlist DESPUÉS de atestiguar (la ventana real:
    evidencia de F1, estado F2 al spawn). Si el runner olvidara threadar
    ``handoff=`` hasta ``spawn()``, la revalidación no correría, la sesión se
    abriría y este test se pondría rojo.
    """
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso(despues_de_verificar=esc.deshabilitar_mod)
    runner = _runner_brokered(tmp_path, esc, bridge)

    result = await runner.run_full_pipeline(
        run_texgen=False,
        expected_profile=_PERFIL,
    )

    assert result.success is False
    assert bridge.sessions == [], "el spawn jamás abrió sesión sobre el estado mutado"
    assert any("handoff" in e.lower() for e in result.errors), result.errors
    assert bridge.probes == 1, "el gate sí corrió y aprobó (evidencia de F1)"


@pytest.mark.asyncio
async def test_run_full_pipeline_sin_drift_llega_al_spawn_brokered(tmp_path: pathlib.Path) -> None:
    """Sin drift la cadena completa gate → run_dyndolod → spawn se abre."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    runner = _runner_brokered(tmp_path, esc, bridge)

    await runner.run_full_pipeline(run_texgen=False, expected_profile=_PERFIL)

    assert len(bridge.sessions) == 1, "la sesión brokered se abrió en el boundary"


def _runner_brokered(tmp_path: pathlib.Path, esc: _Escenario, bridge: _BridgeFalso) -> DynDOLODRunner:
    exe = _exe_dyndolod(tmp_path)
    texgen_exe = exe.parent / "TexGenx64.exe"
    texgen_exe.touch()
    # El work root administrado existe y sus subroots exclusivos nacen vacíos:
    # precondiciones P2.2 (contención física + born-empty) del `_execute_process`.
    raiz_dyndolod = tmp_path / "Work Root" / "DynDOLOD" / "DynDOLOD"
    raiz_dyndolod.mkdir(parents=True)
    (tmp_path / "Work Root" / "DynDOLOD" / "TexGen").mkdir(parents=True)
    config = DynDOLODConfig(
        game_path=tmp_path / "game",
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=esc.mods_dir,
        dyndolod_exe=exe,
        texgen_exe=texgen_exe,
        external_work_root=tmp_path / "Work Root",
        data_dir=esc.virtual_data,
    )
    strategy = _strategy(esc, bridge, tmp_path)
    return DynDOLODRunner(
        config,
        readiness=ReadinessMode.DISABLED_FOR_TEST,
        spawn_strategy=strategy,  # type: ignore[arg-type]
    )


# =============================================================================
# Las otras capas del boundary: mapping runtime y el backend standalone
# =============================================================================


@pytest.mark.asyncio
async def test_pass_luego_romper_el_mapping_usvfs_bloquea_al_abrir_la_sesion(tmp_path: pathlib.Path) -> None:
    """El mapping runtime se re-prueba al abrir la sesión: el worker no despacha.

    Los pasos host-side (identidad, enablement, efectividad, fingerprint) ven el
    mod intacto; la vista USVFS es la que se rompió. La ``verify_vfs_attestation``
    REAL que corre el broker falso al abrir la sesión es la que bloquea.
    """
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    approval = await _gate_pasa(esc, bridge, tmp_path)

    (esc.virtual_data / "textures" / "a.dds").write_bytes(b"VISTA-VIRTUAL-ROTA")

    with pytest.raises(VfsAttestationError):
        await _spawn(esc, bridge, tmp_path, approval)
    assert bridge.sessions == [], "sin atestación no hay sesión ni proceso"


@pytest.mark.asyncio
async def test_standalone_revalida_identidad_y_visibilidad_en_el_spawn(tmp_path: pathlib.Path) -> None:
    """El backend standalone también liga el approval y lo revalida (fail-closed).

    Sólo se ejercitan los cortes: la revalidación corre ANTES de crear el
    subprocess, así que ningún proceso real nace en estos tests.
    """
    from sky_claw.local.tools.dyndolod_runner import StandaloneDynDOLODSpawnStrategy

    staging = tmp_path / "Work Root" / "DynDOLOD" / "TexGen" / "textures"
    (staging / "sub").mkdir(parents=True)
    (staging / "a.dds").write_bytes(_BYTES_A)
    (staging / "sub" / "b.dds").write_bytes(_BYTES_B)
    data_dir = tmp_path / "game" / "Data"
    (data_dir / "textures" / "sub").mkdir(parents=True)
    (data_dir / "textures" / "a.dds").write_bytes(_BYTES_A)
    (data_dir / "textures" / "sub" / "b.dds").write_bytes(_BYTES_B)

    estrategia = StandaloneDynDOLODSpawnStrategy()
    request = TexGenHandoffRequest(
        mod_name=_MOD,
        staging=staging,
        data_dir=data_dir,
        expected_profile=_PERFIL,
    )
    resultado = await estrategia.verify_texgen_handoff(request)
    assert resultado.verified is True, resultado.reason
    approval = resultado.approval
    assert approval is not None

    # Drift en el Data: la visibilidad aprobada ya no se sostiene → BLOCK.
    (data_dir / "textures" / "a.dds").write_bytes(b"DATOS-MUTADOS")
    with pytest.raises(HandoffDriftError, match="visibilidad física"):
        await estrategia.spawn(
            executable=tmp_path / "DynDOLOD" / "DynDOLODx64.exe",
            args=[],
            tool_name="DynDOLOD",
            cwd=tmp_path,
            timeout=30.0,
            handoff=approval,
        )

    # Drift del artifact mismo: la identidad aprobada ya no se sostiene → BLOCK.
    (data_dir / "textures" / "a.dds").write_bytes(_BYTES_A)  # se restaura la vista
    (staging / "sub" / "b.dds").write_bytes(b"STAGING-MUTADO")
    with pytest.raises(HandoffDriftError, match="aprobación"):
        await estrategia.spawn(
            executable=tmp_path / "DynDOLOD" / "DynDOLODx64.exe",
            args=[],
            tool_name="DynDOLOD",
            cwd=tmp_path,
            timeout=30.0,
            handoff=approval,
        )


@pytest.mark.asyncio
async def test_sin_approval_el_camino_legacy_no_se_rompe(tmp_path: pathlib.Path) -> None:
    """``handoff=None`` sigue siendo el camino legacy ("DynDOLOD solo")."""
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso()
    estrategia = _strategy(esc, bridge, tmp_path)

    proceso = await estrategia.spawn(
        executable=_exe_dyndolod(tmp_path),
        args=[],
        tool_name="DynDOLOD",
        cwd=tmp_path,
        timeout=30.0,
    )

    assert len(bridge.sessions) == 1
    assert proceso.pid == 4242


def test_aprobado_sin_approval_es_contrato_roto() -> None:
    """``verified=True`` implica ``approval`` (y viceversa): invariante del tipo."""
    from sky_claw.local.tools.artifact_digest import TreeDigest
    from sky_claw.local.tools.texgen_handoff import TexGenHandoffResult

    with pytest.raises(ValueError, match="approval"):
        TexGenHandoffResult(verified=True, reason="ok")
    with pytest.raises(ValueError, match="approval"):
        TexGenHandoffResult(
            verified=False,
            reason="no",
            approval=TexGenHandoffApproval(
                mod_name=_MOD,
                artifact_root=pathlib.Path("x"),
                artifact=TreeDigest(digest="0" * 64, files=1, bytes=1),
                data_dir=pathlib.Path("y"),
            ),
        )
