"""PR-586D: gate MODE-AWARE de visibilidad TexGen → DynDOLOD.

Matriz obligatoria del contrato:

* **Standalone (S1–S4)** — la primitive física histórica, sin degradar.
* **Brokered positivo (B1)** — perfil + artifact + enablement + efectividad +
  evidencia runtime ⇒ handoff verificado y el spawn DE DynDOLOD se abriría.
* **Brokered negativo (B2–B12)** — cada falla bloquea; lo indeterminado bloquea.
* **Resume/preservación (R2/R3/R6)** — la identidad durable se re-verifica cerca
  del spawn; el profile drift es fail-closed.
* **No-bypass** — sin la capability explícita, o con veredicto bloqueado, el
  spawn NO se abre; el runner jamás pregunta el backend por ``isinstance``.

Nada de esto ejecuta DynDOLOD real: los spawns son fakes y los probes runtime
usan el contrato de attestation real (``verify_vfs_attestation``) contra un
bridge simulado.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import shutil
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy
from sky_claw.local.mo2.mod_effectivity import ModEffectivityError, verificar_artifact_efectivo
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge_for_source,
    read_enabled_mods,
    verify_vfs_attestation,
)
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsJobResult
from sky_claw.local.tools.artifact_digest import TreeDigest, digest_arbol
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODPipelineResult,
    DynDOLODRunner,
    ReadinessMode,
    StandaloneDynDOLODSpawnStrategy,
    ToolExecutionResult,
)
from sky_claw.local.tools.texgen_handoff import TexGenHandoffRequest, TexGenHandoffResult

# =============================================================================
# Escenario MO2 mínimo (perfil + mods + overwrite + Data físico/virtual)
# =============================================================================

_PERFIL = "Perfil-A"
_MOD = "TexGen Output"


@dataclasses.dataclass
class _Escenario:
    data_root: pathlib.Path
    mods_dir: pathlib.Path
    physical_data: pathlib.Path
    virtual_data: pathlib.Path
    artifact_root: pathlib.Path  # mods/TexGen Output/textures

    def modlist(self, contenido: str) -> None:
        (self.data_root / "profiles" / _PERFIL / "modlist.txt").write_text(contenido, encoding="utf-8")

    def montar_vista_virtual(self) -> None:
        """Simula el mapping USVFS: el contenido del mod aparece en virtual Data."""
        for archivo in self.artifact_root.rglob("*"):
            if archivo.is_file():
                destino = self.virtual_data / archivo.relative_to(self.artifact_root.parent)
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archivo, destino)

    def escribir_en_mod(self, relativo: str, contenido: bytes) -> pathlib.Path:
        ruta = self.artifact_root / relativo
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        return ruta


def _escenario(tmp_path: pathlib.Path) -> _Escenario:
    data_root = tmp_path / "MO2"
    mods_dir = data_root / "mods"
    profile_dir = data_root / "profiles" / _PERFIL
    profile_dir.mkdir(parents=True)
    (profile_dir / "plugins.txt").write_text("", encoding="utf-8")
    (profile_dir / "modlist.txt").write_text(f"+OtroMod\n+{_MOD}\n", encoding="utf-8")
    (mods_dir / "OtroMod").mkdir(parents=True)
    (mods_dir / _MOD / "meta.ini").parent.mkdir(parents=True)
    (mods_dir / _MOD / "meta.ini").write_text("[General]\n", encoding="utf-8")
    artifact_root = mods_dir / _MOD / "textures"
    artifact_root.mkdir(parents=True)
    (artifact_root / "a.dds").write_bytes(b"CURRENT!")
    (artifact_root / "sub").mkdir()
    (artifact_root / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")
    # Los DOS `Data` cuelgan del MISMO game root: en producción el host lee el
    # `Data` físico y el worker lee el mismo `<juego>/Data` A TRAVÉS de USVFS;
    # acá son directorios distintos para modelar las dos vistas por separado.
    physical_data = tmp_path / "game" / "Data"
    physical_data.mkdir(parents=True)
    virtual_data = tmp_path / "game" / "DataVista"
    virtual_data.mkdir()
    return _Escenario(
        data_root=data_root,
        mods_dir=mods_dir,
        physical_data=physical_data,
        virtual_data=virtual_data,
        artifact_root=artifact_root,
    )


# =============================================================================
# Bridge simulado: ejecuta el MISMO contrato de attestation que el worker
# =============================================================================


class _BridgeFalso:
    """Simula ``execute_worker_manifest`` para ``tool_id=health``.

    Corre ``verify_vfs_attestation`` REAL (fingerprint del perfil + hash del
    canary visible en el virtual Data) y el probe del nieto, como el worker
    productivo. Los modos permiten indeterminar cada pieza por separado.
    """

    def __init__(
        self,
        esc: _Escenario,
        *,
        modo: str = "ok",
        antes_de_verificar: Callable[[], None] | None = None,
    ) -> None:
        self.esc = esc
        self.modo = modo
        self.antes_de_verificar = antes_de_verificar
        self.probes = 0
        self.challenges: list[VfsAttestationChallenge] = []
        self.jobs: list[VfsJob] = []
        self.sessions: list[Any] = []

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
        self.jobs.append(job)
        self.challenges.append(challenge)
        if self.modo == "bridge_down":
            raise RuntimeError("bridge disconnected")
        if self.antes_de_verificar is not None:
            self.antes_de_verificar()
        if self.modo == "sin_atestacion":
            return _resultado(job, success=True, attestation=None, message="")
        try:
            proof = verify_vfs_attestation(
                challenge=challenge,
                data_root=data_root,
                mods_dir=mods_dir,
                profile=job.profile,
                virtual_data_dir=virtual_data_dir,
            )
        except VfsAttestationError as e:
            return _resultado(job, success=False, attestation=None, message=f"attestation VFS falló: {e}")
        atestacion = dict(proof.to_dict())
        if self.modo == "sha_virtual_tampered":
            atestacion["visible_sha256"] = "f" * 64
        elif self.modo == "sin_nieto":
            pass  # falta grandchild_sha256
        else:
            atestacion["grandchild_sha256"] = challenge.sha256
        return _resultado(job, success=True, attestation=atestacion, message="")

    async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:
        self.jobs.append(job)
        self.sessions.append((job, kwargs))
        session = AsyncMock()
        session.pid = 4321
        session.returncode = None
        return session


def _resultado(
    job: VfsJob,
    *,
    success: bool,
    attestation: dict[str, Any] | None,
    message: str,
) -> VfsJobResult:
    return VfsJobResult(
        protocol_version=2,
        job_id=job.job_id,
        success=success,
        message=message,
        exit_code=0 if success else 1,
        stdout="",
        stderr="",
        outputs=(),
        rollback_state="not_required" if success else "pending",
        attestation=attestation,
        tool_result={},
    )


def _strategy(esc: _Escenario, bridge: Any | None = None) -> BrokeredDynDOLODSpawnStrategy:
    return BrokeredDynDOLODSpawnStrategy(
        broker=bridge if bridge is not None else _BridgeFalso(esc),
        instance_id="portable-main",
        profile=_PERFIL,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        install_root=esc.data_root,
        physical_data_dir=esc.physical_data,
        virtual_data_dir=esc.virtual_data,
        output_roots={},
    )


def _request(esc: _Escenario, **cambios: Any) -> TexGenHandoffRequest:
    base = {
        "mod_name": _MOD,
        "staging": esc.artifact_root,
        "data_dir": esc.virtual_data,
        "expected_profile": _PERFIL,
        "authorized": None,
    }
    base.update(cambios)
    return TexGenHandoffRequest(**base)


# =============================================================================
# Standalone: la primitive física NO se degrada (S1–S4)
# =============================================================================


def _staging_fisico(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    staging = tmp_path / "salida" / "textures"
    (staging / "sub").mkdir(parents=True)
    (staging / "a.dds").write_bytes(b"CURRENT!")
    (staging / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    return staging, data_dir


async def _verificar_standalone(staging: pathlib.Path, data_dir: pathlib.Path) -> TexGenHandoffResult:
    return await StandaloneDynDOLODSpawnStrategy().verify_texgen_handoff(
        TexGenHandoffRequest(
            mod_name=_MOD,
            staging=staging,
            data_dir=data_dir,
            expected_profile=_PERFIL,
        )
    )


@pytest.mark.asyncio
async def test_s1_standalone_match_fisico_completo_pasa(tmp_path: pathlib.Path) -> None:
    staging, data_dir = _staging_fisico(tmp_path)
    shutil.copytree(staging, data_dir / "textures")

    resultado = await _verificar_standalone(staging, data_dir)

    assert resultado.verified is True
    assert resultado.pending_action is None


@pytest.mark.asyncio
async def test_s2_standalone_archivo_ausente_bloquea(tmp_path: pathlib.Path) -> None:
    staging, data_dir = _staging_fisico(tmp_path)
    (data_dir / "textures").mkdir()
    (data_dir / "textures" / "a.dds").write_bytes(b"CURRENT!")

    resultado = await _verificar_standalone(staging, data_dir)

    assert resultado.verified is False
    assert resultado.pending_action == "physical_deployment"


@pytest.mark.asyncio
async def test_s3_standalone_bytes_distintos_bloquea(tmp_path: pathlib.Path) -> None:
    staging, data_dir = _staging_fisico(tmp_path)
    shutil.copytree(staging, data_dir / "textures")
    (data_dir / "textures" / "a.dds").write_bytes(b"OLDBYTE!")

    resultado = await _verificar_standalone(staging, data_dir)

    assert resultado.verified is False
    assert resultado.pending_action == "physical_deployment"


@pytest.mark.asyncio
async def test_s4_standalone_materializacion_parcial_bloquea(tmp_path: pathlib.Path) -> None:
    staging, data_dir = _staging_fisico(tmp_path)
    shutil.copytree(staging, data_dir / "textures")
    (data_dir / "textures" / "sub" / "b.dds").unlink()

    resultado = await _verificar_standalone(staging, data_dir)

    assert resultado.verified is False
    assert resultado.pending_action == "physical_deployment"


# =============================================================================
# Brokered positivo (B1)
# =============================================================================


@pytest.mark.asyncio
async def test_b1_handoff_verificado_cuando_todo_coincide(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso(esc)
    esc.montar_vista_virtual()

    resultado = await _strategy(esc, bridge).verify_texgen_handoff(_request(esc))

    assert resultado.verified is True, resultado.reason
    assert bridge.probes == 1
    assert bridge.jobs[0].tool_id == "health", "el probe reutiliza el contrato allowlisted, sin tool_id genérico"
    assert bridge.challenges[0].source_mod == _MOD


@pytest.mark.asyncio
async def test_b1_el_spawn_de_dyndolod_se_abre_con_gate_verificado() -> None:
    """El gate verificado AUTORIZA el spawn — sin ejecutar DynDOLOD real."""
    estrategia = _EstrategiaConGate(TexGenHandoffResult.aprobado("ok"))
    result = await _correr_pipeline_con_gate(estrategia, verdict_ok=True)

    assert result.success is True
    assert estrategia.verificadas == 1
    assert estrategia.run_dyndolod.await_count == 1, "gate abierto ⇒ DynDOLOD se lanza (mock)"


# =============================================================================
# Brokered negativo (B2–B12)
# =============================================================================


@pytest.mark.asyncio
async def test_b2_artifact_ausente_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    shutil.rmtree(esc.artifact_root)

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action is None, "sin artifact no hay nada que el operador pueda entregar"


@pytest.mark.asyncio
async def test_b3_mod_deshabilitado_bloquea_como_accion_de_perfil(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    bridge = _BridgeFalso(esc)
    esc.modlist("+OtroMod\n-" + _MOD + "\n")

    resultado = await _strategy(esc, bridge).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action == "profile_enablement"
    assert "no edita modlist" in resultado.reason or "Sky-Claw no edita" in resultado.reason
    assert bridge.probes == 0, "sin enablement no se gasta el probe runtime"


@pytest.mark.asyncio
async def test_b3b_mod_ausente_del_modlist_tambien_es_profile_enablement(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.modlist("+OtroMod\n")

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action == "profile_enablement"


@pytest.mark.asyncio
async def test_b4_perfil_distinto_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()

    resultado = await _strategy(esc).verify_texgen_handoff(
        _request(esc, expected_profile="Perfil-B"),
    )

    assert resultado.verified is False
    assert resultado.pending_action is None
    assert "Perfil-B" in resultado.reason


@pytest.mark.asyncio
async def test_b4b_perfil_desconocido_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc, expected_profile=None))

    assert resultado.verified is False


@pytest.mark.asyncio
async def test_b5_artifact_con_digest_distinto_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    # staging autorizado con el MISMO nombre Data-relative ("textures") pero en
    # otra raíz: el mod empaquetado diverge de él y el gate lo detecta.
    staging_autorizado = tmp_path / "staging_fresco" / "textures"
    shutil.copytree(esc.artifact_root, staging_autorizado)
    (esc.artifact_root / "a.dds").write_bytes(b"OTROS-BYTES!")

    resultado = await _strategy(esc).verify_texgen_handoff(
        _request(esc, staging=staging_autorizado),
    )

    assert resultado.verified is False
    assert resultado.pending_action is None
    assert "no coincide" in resultado.reason or "atribuir" in resultado.reason


@pytest.mark.asyncio
async def test_b6_drift_de_perfil_entre_challenge_y_probe_bloquea(tmp_path: pathlib.Path) -> None:
    """El proof no sobrevive al drift: el fingerprint se vuelve a medir en el probe."""

    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()

    def _drift() -> None:
        esc.modlist(f"+{_MOD}\n+OtroMod\n")  # cambia la prioridad después del challenge

    bridge = _BridgeFalso(esc, antes_de_verificar=_drift)

    resultado = await _strategy(esc, bridge).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert bridge.probes == 1
    assert "perfil" in resultado.reason.lower() or "fingerprint" in resultado.reason.lower()


@pytest.mark.asyncio
async def test_b7_canary_no_visible_bajo_usvfs_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    # vista virtual SIN montar: el canary del mod no aparece en virtual Data

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action is None


@pytest.mark.asyncio
async def test_b8_sha_virtual_distinto_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    (esc.virtual_data / "textures" / "a.dds").write_bytes(b"VIRTUAL-TAMPERED")
    bridge = _BridgeFalso(esc, modo="sha_virtual_tampered")

    resultado = await _strategy(esc, bridge).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    # la evidencia mintió sobre el hash: fuera de contrato, igual bloquea
    assert resultado.pending_action is None


@pytest.mark.asyncio
async def test_b8b_bytes_virtuales_distintos_bloquean_en_el_probe(tmp_path: pathlib.Path) -> None:
    """El worker real compara el hash: bytes virtuales ≠ autorizados ⇒ BLOCK."""
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    (esc.virtual_data / "textures" / "a.dds").write_bytes(b"VIRTUAL-TAMPERED")

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert "canary" in resultado.reason.lower() or "USVFS" in resultado.reason


@pytest.mark.asyncio
async def test_b9_override_de_mayor_prioridad_con_bytes_distintos_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    alto = esc.mods_dir / "ModAlto" / "textures"
    alto.mkdir(parents=True)
    (alto / "a.dds").write_bytes(b"BYTES-AJENOS!")
    esc.modlist(f"+OtroMod\n+{_MOD}\n+ModAlto\n")

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action is None
    assert "ModAlto" in resultado.reason


@pytest.mark.asyncio
async def test_b9b_override_con_bytes_identicos_no_bloquea_por_efectividad(tmp_path: pathlib.Path) -> None:
    """Override aceptado ÚNICAMENTE por bytes idénticos: el consumidor ve lo autorizado."""
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    alto = esc.mods_dir / "ModAlto" / "textures"
    alto.mkdir(parents=True)
    (alto / "a.dds").write_bytes(b"CURRENT!")  # mismos bytes que el artifact
    esc.modlist(f"+OtroMod\n+{_MOD}\n+ModAlto\n")

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is True, resultado.reason


@pytest.mark.asyncio
async def test_b10_overwrite_que_eclipsa_incompatible_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    overwrite = esc.data_root / "overwrite" / "textures"
    overwrite.mkdir(parents=True)
    (overwrite / "sub" / "b.dds").parent.mkdir(parents=True)
    (overwrite / "sub" / "b.dds").write_bytes(b"OVERWRITE-AJENO")

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert "overwrite" in resultado.reason


@pytest.mark.asyncio
async def test_b10b_overwrite_no_regular_sobre_el_path_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    overwrite = esc.data_root / "overwrite" / "textures"
    overwrite.mkdir(parents=True)
    (overwrite / "a.dds").mkdir()  # un DIRECTORIO ocupa el path del artifact

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action is None


@pytest.mark.asyncio
async def test_b11_bridge_caido_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    bridge = _BridgeFalso(esc, modo="bridge_down")

    resultado = await _strategy(esc, bridge).verify_texgen_handoff(_request(esc))

    assert resultado.verified is False
    assert resultado.pending_action is None
    assert "bridge" in resultado.reason.lower()


@pytest.mark.asyncio
async def test_b12_lo_indeterminado_bloquea(tmp_path: pathlib.Path) -> None:
    """UNKNOWN ⇒ BLOCK: modlist ilegible, atestación faltante, campos incompletos."""
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()

    # (a) modlist malformado
    esc.modlist("esto no es un modlist\n")
    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc))
    assert resultado.verified is False

    # (b) probe sin atestación estructurada
    esc.modlist(f"+OtroMod\n+{_MOD}\n")
    resultado = await _strategy(esc, _BridgeFalso(esc, modo="sin_atestacion")).verify_texgen_handoff(_request(esc))
    assert resultado.verified is False

    # (c) atestación sin el probe del nieto
    resultado = await _strategy(esc, _BridgeFalso(esc, modo="sin_nieto")).verify_texgen_handoff(_request(esc))
    assert resultado.verified is False


# =============================================================================
# Resume / identidad durable (R2/R3/R6) — el runner compone autoridad + capability
# =============================================================================


class _EstrategiaConGate:
    """Strategy doble: registra el gate y NO ejecuta nada real."""

    def __init__(self, veredicto: TexGenHandoffResult) -> None:
        self.veredicto = veredicto
        self.verificadas = 0
        self.requests: list[TexGenHandoffRequest] = []
        self.run_dyndolod: AsyncMock = AsyncMock()
        self.recibio_spawn = False

    async def verify_texgen_handoff(self, request: TexGenHandoffRequest) -> TexGenHandoffResult:
        self.verificadas += 1
        self.requests.append(request)
        return self.veredicto

    async def spawn(self, **kwargs: Any) -> Any:
        self.recibio_spawn = True
        raise AssertionError("el pipeline no debe llegar al spawn con este doble")


async def _correr_pipeline_con_gate(
    estrategia: _EstrategiaConGate,
    *,
    verdict_ok: bool,
) -> DynDOLODPipelineResult:
    """``run_full_pipeline(run_texgen=True)`` con tooling falso hasta el gate."""
    config = MagicMock()
    config.data_dir = pathlib.Path("/data")
    config.mo2_mods_path = pathlib.Path("/mods")
    config.timeout_seconds = 10
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.ini_primaria_requerida = None
    config.game_mode = "sse"
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=estrategia)
    staging = pathlib.Path("/salida/textures")

    async def _texgen(**_kw: object) -> ToolExecutionResult:
        return ToolExecutionResult(True, "TexGen", 0, "", "", output_path=staging)

    async def _empaquetar(*_args: object, **_kw: object) -> pathlib.Path:
        return pathlib.Path("/mods/TexGen Output")

    runner.run_texgen = AsyncMock(side_effect=_texgen)  # type: ignore[method-assign]
    runner._package_output_as_mod = AsyncMock(side_effect=_empaquetar)  # type: ignore[method-assign]
    estrategia.run_dyndolod = AsyncMock(
        side_effect=lambda **_kw: ToolExecutionResult(True, "DynDOLOD", 0, "", "", output_path=pathlib.Path("/out"))
    )
    runner.run_dyndolod = estrategia.run_dyndolod  # type: ignore[method-assign]
    result = await runner.run_full_pipeline(run_texgen=True)
    assert result is not None
    if verdict_ok:
        assert result.dyndolod_result is not None
    return result


@pytest.mark.asyncio
async def test_r2_resume_mismo_perfil_y_artifact_reevalua_el_gate(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    identidad = digest_arbol(esc.artifact_root)

    resultado = await _strategy(esc).verify_texgen_handoff(_request(esc, authorized=identidad))
    assert resultado.verified is True, resultado.reason

    # y el runner re-verifica la autoridad durable ANTES de consultar al backend
    estrategia = _EstrategiaConGate(TexGenHandoffResult.aprobado("ok"))
    config = MagicMock()
    config.data_dir = esc.virtual_data
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=estrategia)
    veredicto = await runner._verificar_handoff_de_texgen(
        TexGenHandoffRequest(
            mod_name=_MOD,
            staging=esc.artifact_root,
            data_dir=esc.virtual_data,
            expected_profile=_PERFIL,
            authorized=identidad,
        )
    )
    assert veredicto.verified is True
    assert estrategia.verificadas == 1


@pytest.mark.asyncio
async def test_r3_resume_con_perfil_distinto_bloquea(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()

    resultado = await _strategy(esc).verify_texgen_handoff(
        _request(esc, expected_profile="Perfil-B"),
    )
    assert resultado.verified is False
    assert resultado.pending_action is None


@pytest.mark.asyncio
async def test_r6_artifact_mutado_tras_identidad_durable_bloquea_sin_alcanzar_el_backend(
    tmp_path: pathlib.Path,
) -> None:
    esc = _escenario(tmp_path)
    esc.montar_vista_virtual()
    identidad = digest_arbol(esc.artifact_root)
    (esc.artifact_root / "a.dds").write_bytes(b"BYTE-MUTADO")

    estrategia = _EstrategiaConGate(TexGenHandoffResult.aprobado("nunca debe llegar acá"))
    config = MagicMock()
    config.data_dir = esc.virtual_data
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=estrategia)

    veredicto = await runner._verificar_handoff_de_texgen(
        TexGenHandoffRequest(
            mod_name=_MOD,
            staging=esc.artifact_root,
            data_dir=esc.virtual_data,
            expected_profile=_PERFIL,
            authorized=identidad,
        )
    )

    assert veredicto.verified is False
    assert veredicto.pending_action is None
    assert "identidad durable" in veredicto.reason
    assert estrategia.verificadas == 0, "la capa de autoridad bloquea sin consultar al backend"


# =============================================================================
# No-bypass: anclas contra reintroducir `if brokered: handoff_verificado = True`
# =============================================================================


@pytest.mark.asyncio
async def test_sin_capability_no_hay_spawn() -> None:
    """Un objeto con `spawn` pero sin `verify_texgen_handoff` NO obtiene default permisivo."""

    class _SoloSpawn:
        async def spawn(self, **_kwargs: Any) -> Any:
            raise AssertionError("no debe spawnear")

    config = MagicMock()
    config.data_dir = pathlib.Path("/data")
    config.mo2_mods_path = pathlib.Path("/mods")
    config.timeout_seconds = 10
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.ini_primaria_requerida = None
    config.game_mode = "sse"
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=_SoloSpawn())
    staging = pathlib.Path("/salida/textures")

    async def _texgen(**_kw: object) -> ToolExecutionResult:
        return ToolExecutionResult(True, "TexGen", 0, "", "", output_path=staging)

    run_dyndolod = AsyncMock()
    runner.run_texgen = AsyncMock(side_effect=_texgen)  # type: ignore[method-assign]
    runner._package_output_as_mod = AsyncMock(return_value=pathlib.Path("/mods/TexGen Output"))  # type: ignore[method-assign]
    runner.run_dyndolod = run_dyndolod  # type: ignore[method-assign]

    result = await runner.run_full_pipeline(run_texgen=True)

    run_dyndolod.assert_not_awaited()
    assert result.success is False
    assert any("verify_texgen_handoff" in e for e in result.errors)


@pytest.mark.asyncio
async def test_gate_bloqueado_no_abre_spawn_ni_deja_de_cortar() -> None:
    estrategia = _EstrategiaConGate(
        TexGenHandoffResult.bloqueado("bloqueado por el test", pending_action=None),
    )

    result = await _correr_pipeline_con_gate(estrategia, verdict_ok=False)

    assert result.success is False
    assert estrategia.run_dyndolod.await_count == 0
    assert result.needs_deployment is False, "un bloqueo duro NO es 'espera acción humana'"


@pytest.mark.asyncio
async def test_gate_con_accion_pendiente_marca_needs_deployment() -> None:
    estrategia = _EstrategiaConGate(
        TexGenHandoffResult.bloqueado("habilitá el mod", pending_action="profile_enablement"),
    )

    result = await _correr_pipeline_con_gate(estrategia, verdict_ok=False)

    assert result.success is False
    assert result.needs_deployment is True
    assert result.handoff_action == "profile_enablement"
    assert estrategia.run_dyndolod.await_count == 0


def test_el_runner_no_pregunta_el_backend_por_isinstance() -> None:
    """Ancla estructural: la capability explícita es la ÚNICA forma de decidir el dominio."""
    fuente = pathlib.Path(__import__("sky_claw.local.tools.dyndolod_runner", fromlist=["__file__"]).__file__).read_text(
        encoding="utf-8"
    )
    assert "isinstance(self._spawn_strategy" not in fuente
    assert "isinstance(estrategia" not in fuente
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Compare):
            izquierda = ast.unparse(nodo.left)
            if "_spawn_strategy" in izquierda and any(
                isinstance(op, (ast.Is, ast.IsNot)) and "None" in ast.unparse(c)
                for op, c in zip(nodo.ops, nodo.comparators, strict=True)
            ):
                continue
            assert "BrokeredDynDOLOD" not in ast.unparse(nodo), "el runner no debe discriminar por tipo de backend"


def test_las_dos_estrategias_exponen_la_capability() -> None:
    assert callable(getattr(StandaloneDynDOLODSpawnStrategy, "verify_texgen_handoff", None))
    assert callable(getattr(BrokeredDynDOLODSpawnStrategy, "verify_texgen_handoff", None))


# =============================================================================
# Primitives: canary por source y efectividad del overlay
# =============================================================================


def test_canary_for_source_pertenece_al_mod_pedido(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    otro = esc.mods_dir / "OtroMod" / "skse"
    otro.mkdir(parents=True)
    (otro / "otro.txt").write_bytes(b"otro")
    (esc.data_root / "profiles" / _PERFIL / "modlist.txt").write_text(f"+OtroMod\n+{_MOD}\n", encoding="utf-8")

    challenge = build_attestation_challenge_for_source(
        source_mod=_MOD,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
    )

    assert challenge.source_mod == _MOD
    assert challenge.relative_path.parts[0] == "textures"


def test_canary_for_source_rechaza_mod_deshabilitado(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.modlist("+OtroMod\n")

    with pytest.raises(VfsAttestationError, match="no está habilitado"):
        build_attestation_challenge_for_source(
            source_mod=_MOD,
            data_root=esc.data_root,
            mods_dir=esc.mods_dir,
            profile=_PERFIL,
            physical_data_dir=esc.physical_data,
        )


def test_canary_for_source_rechaza_nombre_con_path(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    with pytest.raises(VfsAttestationError):
        build_attestation_challenge_for_source(
            source_mod="../escape",
            data_root=esc.data_root,
            mods_dir=esc.mods_dir,
            profile=_PERFIL,
            physical_data_dir=esc.physical_data,
        )


def test_read_enabled_mods_es_orden_de_prioridad_y_falla_cerrado(tmp_path: pathlib.Path) -> None:
    modlist = tmp_path / "modlist.txt"
    modlist.write_text("# comentario\n*---------\n-Bajo\n+Medio\n+Alto\n", encoding="utf-8")
    assert read_enabled_mods(modlist) == ("Medio", "Alto")
    modlist.write_text("+Ok\nlinea rota\n", encoding="utf-8")
    with pytest.raises(VfsAttestationError):
        read_enabled_mods(modlist)


def test_efectividad_pasa_con_overlay_limpio(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    total = verificar_artifact_efectivo(
        artifact_root=esc.artifact_root,
        mod_name=_MOD,
        mods_dir=esc.mods_dir,
        data_root=esc.data_root,
        enabled=("OtroMod", _MOD),
    )
    assert total == 2


def test_efectividad_falla_si_un_mod_mas_alto_reemplaza_bytes(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    alto = esc.mods_dir / "ModAlto" / "textures"
    alto.mkdir(parents=True)
    (alto / "a.dds").write_bytes(b"AJENO")
    with pytest.raises(ModEffectivityError, match="ModAlto"):
        verificar_artifact_efectivo(
            artifact_root=esc.artifact_root,
            mod_name=_MOD,
            mods_dir=esc.mods_dir,
            data_root=esc.data_root,
            enabled=("OtroMod", _MOD, "ModAlto"),
        )


def test_efectividad_falla_con_artifact_vacio(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    for archivo in list(esc.artifact_root.rglob("*")):
        if archivo.is_file():
            archivo.unlink()
    with pytest.raises(ModEffectivityError, match="no tiene archivos"):
        verificar_artifact_efectivo(
            artifact_root=esc.artifact_root,
            mod_name=_MOD,
            mods_dir=esc.mods_dir,
            data_root=esc.data_root,
            enabled=("OtroMod", _MOD),
        )


def test_efectividad_falla_si_el_mod_no_esta_habilitado(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    with pytest.raises(ModEffectivityError, match="no está habilitado"):
        verificar_artifact_efectivo(
            artifact_root=esc.artifact_root,
            mod_name=_MOD,
            mods_dir=esc.mods_dir,
            data_root=esc.data_root,
            enabled=("OtroMod",),
        )


@pytest.mark.asyncio
async def test_autoridad_durable_con_digest_distinto_bloquea_sin_llegar_al_backend(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    estrategia = _EstrategiaConGate(TexGenHandoffResult.aprobado("no debe llegar"))
    config = MagicMock()
    config.data_dir = esc.virtual_data
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=estrategia)
    ajena = TreeDigest(digest="0" * 64, files=1, bytes=1)

    veredicto = await runner._verificar_handoff_de_texgen(
        TexGenHandoffRequest(
            mod_name=_MOD,
            staging=esc.artifact_root,
            data_dir=esc.virtual_data,
            expected_profile=_PERFIL,
            authorized=ajena,
        )
    )

    assert veredicto.verified is False
    assert estrategia.verificadas == 0
