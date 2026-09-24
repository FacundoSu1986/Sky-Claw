"""PR-586B: launch brokered de DynDOLOD/TexGen y contratos adversariales."""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.local.mo2.brokered_dyndolod import (
    BrokeredDynDOLODProcess,
    BrokeredDynDOLODSpawnStrategy,
)
from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsProtocolError
from sky_claw.local.mo2.vfs_worker import (
    VfsProcessOutcome,
    _session_tool_handler,
    _validate_session_launch,
)
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODProcess,
    DynDOLODRunner,
    ReadinessMode,
)


class _FakeSession:
    def __init__(self, *, pid: int = 4321) -> None:
        self._pid = pid
        self._returncode: int | None = None
        self.cancel_calls = 0
        self.result_calls = 0

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        self._returncode = 0
        return 0

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self._returncode = -1

    async def result(self):
        self.result_calls += 1
        self._returncode = 0
        return MagicMock(stdout="worker stdout", stderr="worker stderr")


class _FakeBroker:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.jobs: list[VfsJob] = []
        self.challenges: list[VfsAttestationChallenge] = []

    async def open_session(self, job: VfsJob, *, challenge: VfsAttestationChallenge, **_kwargs):
        self.jobs.append(job)
        self.challenges.append(challenge)
        return self.session


class _FakeStrategy:
    def __init__(self, process: DynDOLODProcess) -> None:
        self.process = process
        self.calls: list[dict[str, object]] = []

    async def spawn(self, **kwargs):
        self.calls.append(kwargs)
        return self.process


class _FailingStrategy:
    async def spawn(self, **_kwargs):
        raise RuntimeError("bridge unavailable")


class _Process:
    pid = 9876
    returncode = 0
    stdout = None
    stderr = None
    backend_managed = True

    def __init__(self) -> None:
        self.wait_calls = 0
        self.terminate_calls = 0

    def assign_job(self):
        return None

    def kill(self) -> None:
        raise AssertionError("un proceso brokered no debe recibir kill local")

    async def wait(self) -> int:
        self.wait_calls += 1
        return 0

    async def terminate(self) -> None:
        self.terminate_calls += 1

    async def captured_output(self):
        return "", ""


def _challenge() -> VfsAttestationChallenge:
    return VfsAttestationChallenge(
        profile="Default",
        source_mod="CanaryMod",
        relative_path=pathlib.PurePosixPath("canary.txt"),
        sha256="a" * 64,
        profile_fingerprint="b" * 64,
    )


@pytest.mark.asyncio
async def test_strategy_separa_texgen_y_dyndolod_y_abre_session(monkeypatch, tmp_path: pathlib.Path) -> None:
    exe_dir = tmp_path / "tools"
    exe_dir.mkdir()
    texgen = exe_dir / "TexGenx64.exe"
    dyndolod = exe_dir / "DynDOLODx64.exe"
    texgen.touch()
    dyndolod.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    output_texgen = tmp_path / "work" / "TexGen"
    output_dyndolod = tmp_path / "work" / "DynDOLOD"
    broker = _FakeBroker(_FakeSession())
    monkeypatch.setattr(
        "sky_claw.local.mo2.brokered_dyndolod.build_attestation_challenge",
        lambda **_kwargs: _challenge(),
    )
    strategy = BrokeredDynDOLODSpawnStrategy(
        broker=broker,
        instance_id="portable-main",
        profile="Default",
        data_root=tmp_path / "MO2",
        mods_dir=tmp_path / "MO2" / "mods",
        install_root=tmp_path / "MO2",
        physical_data_dir=tmp_path / "Skyrim" / "Data",
        virtual_data_dir=tmp_path / "Skyrim" / "Data",
        output_roots={"texgen": output_texgen, "dyndolod": output_dyndolod},
    )

    await strategy.spawn(
        executable=texgen,
        args=["-sse", f"-o:{output_texgen}", "-d:x", "-m:x", "-p:x", "-t:x"],
        tool_name="TexGen",
        cwd=cwd,
        timeout=120,
    )
    await strategy.spawn(
        executable=dyndolod,
        args=["-sse", f"-o:{output_dyndolod}", "-d:x", "-m:x", "-p:x", "-t:x"],
        tool_name="DynDOLOD",
        cwd=cwd,
        timeout=120,
    )

    assert [job.tool_id for job in broker.jobs] == ["texgen", "dyndolod"]
    assert broker.jobs[0].payload["argv"] != broker.jobs[1].payload["argv"]
    assert broker.jobs[0].profile == broker.jobs[1].profile == "Default"
    assert broker.challenges[0] is not broker.challenges[1]


@pytest.mark.asyncio
async def test_strategy_rechaza_familia_de_executable_equivocada(tmp_path: pathlib.Path) -> None:
    exe_dir = tmp_path / "tools"
    exe_dir.mkdir()
    dyndolod = exe_dir / "DynDOLODx64.exe"
    dyndolod.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    strategy = BrokeredDynDOLODSpawnStrategy(
        broker=_FakeBroker(_FakeSession()),
        instance_id="portable-main",
        profile="Default",
        data_root=tmp_path / "MO2",
        mods_dir=tmp_path / "MO2" / "mods",
        install_root=tmp_path / "MO2",
        physical_data_dir=tmp_path / "Skyrim" / "Data",
        virtual_data_dir=tmp_path / "Skyrim" / "Data",
        output_roots={"texgen": tmp_path / "out"},
    )
    with pytest.raises(ValueError, match="no corresponde"):
        await strategy.spawn(
            executable=dyndolod,
            args=[],
            tool_name="TexGen",
            cwd=cwd,
            timeout=10,
        )


@pytest.mark.asyncio
async def test_process_brokered_cancel_siempre_va_por_session_cancel() -> None:
    session = _FakeSession()
    process = BrokeredDynDOLODProcess(session)  # type: ignore[arg-type]

    await process.terminate()

    assert session.cancel_calls == 1
    assert session.returncode == -1


def test_payload_brokered_es_cerrado_y_no_acepta_exec_arbitrario(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "TexGenx64.exe"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    common = {
        "instance_id": "portable-main",
        "profile": "Default",
        "timeout_seconds": 10,
        "expected_fingerprint": "a" * 64,
        "mutation_targets": (tmp_path / "output",),
    }
    with pytest.raises(VfsProtocolError):
        VfsJob.create(
            tool_id="texgen", payload={"executable": str(exe), "argv": [], "cwd": str(cwd), "extra": 1}, **common
        )
    with pytest.raises(VfsProtocolError):
        VfsJob.create(
            tool_id="texgen", payload={"executable": "relative/TexGenx64.exe", "argv": [], "cwd": str(cwd)}, **common
        )
    with pytest.raises(VfsProtocolError, match="no corresponde"):
        VfsJob.create(
            tool_id="texgen",
            payload={"executable": str(tmp_path / "DynDOLODx64.exe"), "argv": [], "cwd": str(cwd)},
            **common,
        )
    with pytest.raises(VfsProtocolError):
        VfsJob.create(
            tool_id="dyndolod",
            payload={"executable": str(tmp_path / "DynDOLODx64.exe"), "argv": ["-sse", 3], "cwd": str(cwd)},
            **common,
        )
    literal = VfsJob.create(
        tool_id="texgen",
        payload={
            "executable": str(exe),
            "argv": ["-sse", "literal;not-a-shell-command"],
            "cwd": str(cwd),
        },
        **common,
    )
    assert literal.payload["argv"] == ["-sse", "literal;not-a-shell-command"]


@pytest.mark.asyncio
async def test_captura_truncada_es_warning_y_no_fallo_de_herramienta(monkeypatch) -> None:
    manifest = MagicMock()
    manifest.job.tool_id = "texgen"
    sink = MagicMock()
    monkeypatch.setattr(
        "sky_claw.local.mo2.vfs_worker._validate_session_launch",
        lambda _manifest: (pathlib.Path("/tmp/TexGenx64.exe"), (), pathlib.Path("/tmp")),
    )
    monkeypatch.setattr(
        "sky_claw.local.mo2.vfs_worker.run_brokered_process",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=VfsProcessOutcome(
                exit_code=0,
                stdout="bounded",
                stderr="",
                duration_seconds=1.0,
                stdout_truncated=True,
                stderr_truncated=False,
            ),
        ),
    )

    result = await _session_tool_handler(manifest, sink)

    assert result.success is True
    assert result.tool_result == {"stdout_truncated": True, "stderr_truncated": False}


def test_worker_rechaza_p_plugins_de_otro_perfil(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "TexGenx64.exe"
    exe.write_text("#!/bin/sh\\n")
    exe.chmod(0o755)
    data_root = tmp_path / "MO2"
    profile_dir = data_root / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    plugins = profile_dir / "plugins.txt"
    plugins.write_text("", encoding="utf-8")
    ini_dir = tmp_path / "ini"
    ini_dir.mkdir()
    (ini_dir / "Skyrim.ini").write_text("", encoding="utf-8")
    virtual_data = tmp_path / "Data"
    virtual_data.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    wrong_plugins = data_root / "profiles" / "Other" / "plugins.txt"
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="texgen",
        payload={
            "executable": str(exe),
            "argv": [
                "-sse",
                f"-d:{virtual_data}",
                f"-m:{ini_dir}",
                f"-p:{wrong_plugins}",
                f"-t:{tmp_path}",
                f"-o:{output}",
            ],
            "cwd": str(cwd),
        },
        timeout_seconds=10,
        expected_fingerprint="a" * 64,
        mutation_targets=(output,),
    )
    manifest = MagicMock()
    manifest.job = job
    manifest.data_root = data_root
    manifest.virtual_data_dir = virtual_data

    with pytest.raises(ValueError, match="argv.-p"):
        _validate_session_launch(manifest)


def test_runner_usa_strategy_inyectada_y_no_spawn_local() -> None:
    config = MagicMock(timeout_seconds=10, heartbeat_interval=60, fence_ownership=None)
    strategy = _FakeStrategy(_Process())
    runner = DynDOLODRunner(config, readiness=ReadinessMode.DISABLED_FOR_TEST, spawn_strategy=strategy)
    runner._exigir_contencion_fisica_del_destino = lambda _tool: None  # type: ignore[method-assign]
    runner._exigir_root_born_empty = lambda _tool: None  # type: ignore[method-assign]

    result = asyncio.run(
        runner._execute_process(
            pathlib.Path("TexGenx64.exe"),
            ["-sse", "-o:/safe", "literal;not-shell"],
            "TexGen",
            cwd=pathlib.Path.cwd(),
        )
    )

    assert result[2] == 0
    assert strategy.calls[0]["args"] == ["-sse", "-o:/safe", "literal;not-shell"]


@pytest.mark.asyncio
async def test_broker_failure_no_hace_fallback_a_create_subprocess(monkeypatch) -> None:
    config = MagicMock(timeout_seconds=10, heartbeat_interval=60, fence_ownership=None)
    runner = DynDOLODRunner(
        config,
        readiness=ReadinessMode.DISABLED_FOR_TEST,
        spawn_strategy=_FailingStrategy(),
    )
    runner._exigir_contencion_fisica_del_destino = lambda _tool: None  # type: ignore[method-assign]
    runner._exigir_root_born_empty = lambda _tool: None  # type: ignore[method-assign]
    local_spawn = MagicMock()
    monkeypatch.setattr("sky_claw.local.tools.dyndolod_runner.asyncio.create_subprocess_exec", local_spawn)

    with pytest.raises(RuntimeError, match="bridge unavailable"):
        await runner._execute_process(pathlib.Path("TexGenx64.exe"), [], "TexGen", cwd=pathlib.Path.cwd())
    local_spawn.assert_not_called()
