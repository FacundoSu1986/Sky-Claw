"""Núcleo stdlib del plugin: validación cerrada y comando fijo del worker."""

from __future__ import annotations

import ast
import inspect
import pathlib
import queue
import threading

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime as bridge_runtime
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    BridgeEventOutbox,
    BridgeLaunchController,
    build_worker_arguments,
    validate_launch_request,
)


def test_outbox_espera_hasta_que_evento_terminal_fue_enviado() -> None:
    outbox = BridgeEventOutbox()
    outbox.put({"event": "worker_exit"})

    assert outbox.wait_until_drained(timeout=0.01) is False

    assert outbox.get_nowait() == {"event": "worker_exit"}
    outbox.task_done()
    assert outbox.wait_until_drained(timeout=0.01) is True


def test_inbox_notifica_despues_de_encolar_comando(tmp_path: pathlib.Path) -> None:
    assert hasattr(bridge_runtime, "BridgeCommandInbox")
    inbox_type = bridge_runtime.BridgeCommandInbox
    comandos: queue.Queue[dict[str, object]] = queue.Queue()
    entregados: list[dict[str, object]] = []

    def notificar() -> None:
        entregados.append(comandos.get_nowait())

    inbox = inbox_type(commands=comandos, notify=notificar)

    inbox.deliver({"type": "launch_worker", "job_id": "job-1"}, jobs_root=tmp_path)

    assert entregados == [
        {
            "type": "launch_worker",
            "job_id": "job-1",
            "_jobs_root": str(tmp_path.resolve()),
        }
    ]


def test_plugin_despacha_por_senal_qt_sin_qtimer() -> None:
    plugin_path = (
        pathlib.Path(__file__).parents[1]
        / "sky_claw"
        / "local"
        / "mo2"
        / "plugin_bundle"
        / "skyclaw_bridge"
        / "plugin.py"
    )
    tree = ast.parse(plugin_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "PyQt6.QtCore"
        for alias in node.names
    }

    assert "QTimer" not in imported_names
    assert {"QObject", "Qt", "pyqtSignal", "pyqtSlot"} <= imported_names


def test_shutdown_drena_monitor_y_outbox_antes_de_cerrar_socket() -> None:
    plugin_path = (
        pathlib.Path(__file__).parents[1]
        / "sky_claw"
        / "local"
        / "mo2"
        / "plugin_bundle"
        / "skyclaw_bridge"
        / "plugin.py"
    )
    source = plugin_path.read_text(encoding="utf-8")

    stop_job = source.index("self._controller.stop()")
    drain_monitor = source.index("self._controller.wait_for_monitors", stop_job)
    stop_client = source.index("self._client.stop", drain_monitor)

    assert stop_job < drain_monitor < stop_client


def test_mensaje_qt_convierte_unicode_a_ascii_seguro() -> None:
    assert hasattr(bridge_runtime, "ascii_safe_message")

    message = bridge_runtime.ascii_safe_message("conexión rechazada")

    assert message.isascii()
    assert message == r"conexi\xf3n rechazada"


class _Organizer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.wait_calls = 0
        self.release = threading.Event()

    def startApplication(self, *args: object) -> int:  # noqa: N802 - fake de API MO2
        self.calls.append(args)
        return 123

    def waitForApplication(  # noqa: N802 - fake de API MO2
        self, handle: int, refresh: bool
    ) -> tuple[bool, int]:
        self.wait_calls += 1
        assert handle == 123
        assert refresh is False
        self.release.wait(timeout=1)
        return True, 7


class _ProcessWaiter:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.release = threading.Event()

    def __call__(self, handle: int) -> tuple[bool, int]:
        self.calls.append(handle)
        self.release.wait(timeout=1)
        return True, 7


class _JobObject:
    def __init__(self) -> None:
        self.assigned: list[int] = []
        self.terminated = False
        self.closed = False

    def assign(self, process_handle: int) -> None:
        self.assigned.append(process_handle)

    def terminate(self) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True


def test_launch_request_solo_acepta_manifest_bajo_jobs_root(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")

    request = validate_launch_request(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        },
        jobs_root=jobs,
    )

    assert request.manifest_path == manifest.resolve()
    assert request.profile == "Default"


def test_launch_request_overwrite_es_nombre_de_mod_y_no_ruta(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")

    request = validate_launch_request(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": "SkyClaw Output",
        },
        jobs_root=jobs,
    )

    assert request.overwrite_mod == "SkyClaw Output"

    with pytest.raises(BridgeCommandError, match="overwrite_mod"):
        validate_launch_request(
            {
                "protocol_version": 1,
                "type": "launch_worker",
                "job_id": "job-2",
                "profile": "Default",
                "manifest_path": str(manifest),
                "overwrite_mod": str(tmp_path / "MO2" / "overwrite"),
            },
            jobs_root=jobs,
        )


def test_launch_request_rechaza_manifest_fuera_del_jobs_root(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    outside = tmp_path / "otro.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(BridgeCommandError, match="jobs_root"):
        validate_launch_request(
            {
                "protocol_version": 1,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": str(outside),
                "overwrite_mod": None,
            },
            jobs_root=jobs,
        )


def test_worker_args_no_admiten_executable_desde_el_request(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    request = validate_launch_request(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        },
        jobs_root=jobs,
    )

    args = build_worker_arguments(
        fixed_prefix=("--vfs-worker",),
        request=request,
        descriptor_path=tmp_path / "descriptor.json",
    )

    assert "malware.exe" not in args
    assert args[:1] == ["--vfs-worker"]
    assert args[-2:] == ["--job-id", "job-1"]


def test_launch_request_rechaza_campos_desconocidos() -> None:
    with pytest.raises(BridgeCommandError, match="campos no permitidos"):
        validate_launch_request(
            {
                "protocol_version": 1,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": "C:/jobs/job-1.json",
                "overwrite_mod": None,
                "executable": "malware.exe",
            },
            jobs_root=pathlib.Path("C:/jobs"),
        )


def test_controller_lanza_worker_fijo_con_perfil_explicito(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    job_object = _JobObject()
    events: list[dict[str, object]] = []
    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=events.append,
        job_factory=lambda: job_object,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )

    controller.launch(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "ExplicitProfile",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        }
    )

    executable, args, cwd, profile, overwrite, ignore_overwrite = organizer.calls[0]
    assert executable == str(worker.resolve())
    assert args[0] == "--vfs-worker"
    assert cwd == str(worker.resolve().parent)
    assert profile == "ExplicitProfile"
    assert overwrite == ""
    assert ignore_overwrite is False
    assert job_object.assigned == [123]
    assert events[0]["type"] == "launch_ack"

    organizer.release.set()
    controller.wait_for_monitors(timeout=1)
    assert events[-1]["event"] == "worker_exit"
    assert events[-1]["exit_code"] == 7
    assert job_object.closed is True


def test_controller_monitor_usa_waiter_win32_y_no_api_mo2(tmp_path: pathlib.Path) -> None:
    assert "process_waiter" in inspect.signature(BridgeLaunchController).parameters
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    waiter = _ProcessWaiter()
    events: list[dict[str, object]] = []
    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=events.append,
        job_factory=_JobObject,
        process_waiter=waiter,
    )

    controller.launch(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        }
    )
    waiter.release.set()
    controller.wait_for_monitors(timeout=1)

    assert waiter.calls == [123]
    assert organizer.wait_calls == 0
    assert events[-1]["event"] == "worker_exit"
    assert events[-1]["exit_code"] == 7


def test_controller_no_lanza_si_no_puede_crear_job_object(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()

    def _fallar_job_object():
        raise BridgeCommandError("Job Object indisponible")

    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=lambda _event: None,
        job_factory=_fallar_job_object,
    )

    with pytest.raises(BridgeCommandError, match="indisponible"):
        controller.launch(
            {
                "protocol_version": 1,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": str(manifest),
                "overwrite_mod": None,
            }
        )

    assert organizer.calls == []


def test_monitor_emite_worker_exit_si_espera_win32_falla(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    events: list[dict[str, object]] = []

    def _fallar_espera(_handle: int) -> tuple[bool, int]:
        raise RuntimeError("WaitForSingleObject falló")

    controller = BridgeLaunchController(
        organizer=_Organizer(),
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=events.append,
        job_factory=_JobObject,
        process_waiter=_fallar_espera,
    )

    controller.launch(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        }
    )
    controller.wait_for_monitors(timeout=1)

    assert events[-1]["event"] == "worker_exit"
    assert events[-1]["wait_ok"] is False
    assert "espera Win32" in str(events[-1]["message"])


def test_controller_cancel_termina_job_object_completo(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    job_object = _JobObject()
    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=lambda _event: None,
        job_factory=lambda: job_object,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    controller.launch(
        {
            "protocol_version": 1,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
        }
    )

    controller.cancel({"protocol_version": 1, "type": "cancel", "job_id": "job-1"})

    assert job_object.terminated is True
    organizer.release.set()
    controller.wait_for_monitors(timeout=1)
