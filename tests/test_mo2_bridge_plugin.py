"""Núcleo stdlib del plugin: validación cerrada y comando fijo del worker."""

from __future__ import annotations

import pathlib
import queue
import threading
import time

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    BridgeLaunchController,
    build_worker_arguments,
    flush_pending_events,
    validate_launch_request,
)


class _Organizer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.release = threading.Event()

    def startApplication(self, *args: object) -> int:  # noqa: N802 - fake de API MO2
        self.calls.append(args)
        return 123

    def waitForApplication(  # noqa: N802 - fake de API MO2
        self, handle: int, refresh: bool
    ) -> tuple[bool, int]:
        assert handle == 123
        assert refresh is False
        self.release.wait(timeout=1)
        return True, 7


class _FailingWaitOrganizer(_Organizer):
    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802
        raise RuntimeError("waitForApplication falló")


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


def test_monitor_emite_worker_exit_si_wait_for_application_falla(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    events: list[dict[str, object]] = []
    controller = BridgeLaunchController(
        organizer=_FailingWaitOrganizer(),
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=events.append,
        job_factory=_JobObject,
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
    assert "waitForApplication" in str(events[-1]["message"])


def test_wait_for_monitors_espera_el_worker_exit_aunque_el_job_ya_se_desregistro(
    tmp_path: pathlib.Path,
) -> None:
    """El monitor des-registra el job ANTES de emitir worker_exit: el join del
    shutdown debe ser sobre los threads reales, no sobre el dict de jobs, o el
    drenaje corre la carrera y el broker se queda esperando el fence terminal."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    emision = threading.Event()
    events: list[dict[str, object]] = []

    def _send_event(event: dict[str, object]) -> None:
        if event.get("type") == "event":
            emision.wait(timeout=2)
        events.append(event)

    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=_send_event,
        job_factory=_JobObject,
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

    organizer.release.set()
    vacio = False
    for _ in range(200):
        with controller._lock:
            vacio = not controller._jobs
        if vacio:
            break
        time.sleep(0.01)
    assert vacio, "el monitor nunca des-registró el job"

    threading.Timer(0.05, emision.set).start()
    controller.wait_for_monitors(timeout=2)

    assert events[-1]["event"] == "worker_exit"


def test_flush_pending_events_espera_el_task_done_del_consumidor() -> None:
    pendientes: queue.Queue[dict[str, object]] = queue.Queue()
    pendientes.put({"type": "event", "event": "worker_exit"})

    def _consumidor() -> None:
        time.sleep(0.05)
        pendientes.get_nowait()
        pendientes.task_done()

    hilo = threading.Thread(target=_consumidor)
    hilo.start()
    try:
        assert flush_pending_events(pendientes, timeout=2, still_running=hilo.is_alive) is True
    finally:
        hilo.join(timeout=2)


def test_flush_pending_events_no_bloquea_si_el_consumidor_murio() -> None:
    pendientes: queue.Queue[dict[str, object]] = queue.Queue()
    pendientes.put({"type": "event", "event": "worker_exit"})

    inicio = time.monotonic()
    assert flush_pending_events(pendientes, timeout=5, still_running=lambda: False) is False
    assert time.monotonic() - inicio < 1


def test_flush_pending_events_expira_con_eventos_sin_enviar() -> None:
    pendientes: queue.Queue[dict[str, object]] = queue.Queue()
    pendientes.put({"type": "event", "event": "worker_exit"})

    assert flush_pending_events(pendientes, timeout=0.1, still_running=lambda: True) is False


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
