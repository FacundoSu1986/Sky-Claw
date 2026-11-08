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
    PROTOCOL_VERSION,
    BridgeCommandError,
    BridgeEventOutbox,
    BridgeLaunchController,
    build_worker_arguments,
    validate_launch_request,
)

JOB_OBJECT = "Local\\SkyClawVfs-" + "0123456789abcdef" * 2


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
    def __init__(self, nombre: str | None = None) -> None:
        self.nombre = nombre
        self.assigned: list[int] = []
        self.terminated = False
        self.closed = False

    def assign(self, process_handle: int) -> None:
        self.assigned.append(process_handle)

    def terminate(self) -> None:
        self.terminated = True

    def adopt_descendants(self, process_handle: int) -> int:
        return 0

    def active_processes(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


def test_launch_request_solo_acepta_manifest_bajo_jobs_root(tmp_path: pathlib.Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")

    request = validate_launch_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
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
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": "SkyClaw Output",
            "job_object": JOB_OBJECT,
        },
        jobs_root=jobs,
    )

    assert request.overwrite_mod == "SkyClaw Output"

    with pytest.raises(BridgeCommandError, match="overwrite_mod"):
        validate_launch_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": "job-2",
                "profile": "Default",
                "manifest_path": str(manifest),
                "overwrite_mod": str(tmp_path / "MO2" / "overwrite"),
                "job_object": JOB_OBJECT,
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
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": str(outside),
                "overwrite_mod": None,
                "job_object": JOB_OBJECT,
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
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
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
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": "C:/jobs/job-1.json",
                "overwrite_mod": None,
                "job_object": JOB_OBJECT,
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
        job_factory=lambda _nombre: job_object,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )

    controller.launch(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "ExplicitProfile",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
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
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
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

    def _fallar_job_object(_nombre: str):
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
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": str(manifest),
                "overwrite_mod": None,
                "job_object": JOB_OBJECT,
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
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
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
        job_factory=lambda _nombre: job_object,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    controller.launch(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
        }
    )

    controller.cancel({"protocol_version": PROTOCOL_VERSION, "type": "cancel", "job_id": "job-1"})

    assert job_object.terminated is True
    organizer.release.set()
    controller.wait_for_monitors(timeout=1)


def test_verify_mo2_breakaway_allowed_rechaza_job_sin_breakaway_y_acepta_con_breakaway_o_fuera_de_job() -> None:
    verify = bridge_runtime.verify_mo2_breakaway_allowed
    # Fuera de cualquier Job Object (None) -> permitido.
    verify(query_flags=lambda: None)
    # Dentro de un Job con JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800) -> permitido.
    verify(query_flags=lambda: 0x2000 | 0x0800)
    # Dentro de un Job con JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (0x1000) -> permitido.
    verify(query_flags=lambda: 0x2000 | 0x1000)
    # Dentro de un Job con solo KILL_ON_JOB_CLOSE (0x2000) sin breakaway -> falla cerrado antes de startApplication.
    with pytest.raises(BridgeCommandError, match="breakaway"):
        verify(query_flags=lambda: 0x2000)


def test_query_current_process_job_limit_flags_en_no_windows_retorna_none() -> None:
    if bridge_runtime.sys.platform != "win32":
        assert bridge_runtime._query_current_process_job_limit_flags() is None


@pytest.mark.skipif(
    bridge_runtime.sys.platform != "win32",
    reason="Win32 QueryInformationJobObject y sus estructuras ctypes solo existen en Windows",
)
def test_query_current_process_job_limit_flags_distingue_fuera_de_job_y_error_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ultimo_error = 5

    class _Kernel32Falso:
        class QueryInformationJobObject:
            argtypes: list[object] = []
            restype: object = None

            def __call__(self, *_args: object) -> int:
                return 0

    kernel32 = _Kernel32Falso()
    kernel32.QueryInformationJobObject = _Kernel32Falso.QueryInformationJobObject()  # type: ignore[assignment]
    monkeypatch.setattr(bridge_runtime.ctypes, "WinDLL", lambda *_a, **_k: kernel32, raising=False)
    monkeypatch.setattr(bridge_runtime.ctypes, "get_last_error", lambda: ultimo_error, raising=False)

    # ERROR_ACCESS_DENIED (5) al consultar hJob=NULL indica que el proceso no pertenece a ningún Job.
    assert bridge_runtime._query_current_process_job_limit_flags() is None

    # Cualquier otro error de Win32 falla cerrado con BridgeCommandError.
    ultimo_error = 87
    with pytest.raises(BridgeCommandError, match="QueryInformationJobObject"):
        bridge_runtime._query_current_process_job_limit_flags()


def test_controller_ejecuta_breakaway_checker_antes_de_crear_job_ni_llamar_start_application(
    tmp_path: pathlib.Path,
) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    jobs_creados: list[str] = []
    eventos: list[dict[str, object]] = []

    def _rechazar_breakaway() -> None:
        raise BridgeCommandError("ModOrganizer.exe corre dentro de un Job Object sin permiso de breakaway")

    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=eventos.append,
        job_factory=lambda nombre: (jobs_creados.append(nombre), _JobObject(nombre))[1],
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
        breakaway_checker=_rechazar_breakaway,
    )

    with pytest.raises(BridgeCommandError, match="breakaway"):
        controller.launch(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": "job-1",
                "profile": "Default",
                "manifest_path": str(manifest),
                "overwrite_mod": None,
                "job_object": JOB_OBJECT,
            }
        )

    assert jobs_creados == [], "no debe crear el Job si MO2 no puede hacer breakaway"
    assert organizer.calls == [], "no debe llamar startApplication (evita colgar el hilo Qt de MO2)"
    assert eventos == []


def test_controller_registra_tiempos_de_launch_start_application_assign_y_ack(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _Organizer()
    logs: list[str] = []
    llamadas_perf_counter = 0
    reloj_real = bridge_runtime.time.perf_counter

    def _perf_counter_espia() -> float:
        nonlocal llamadas_perf_counter
        llamadas_perf_counter += 1
        return reloj_real()

    monkeypatch.setattr(bridge_runtime.time, "perf_counter", _perf_counter_espia)
    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=lambda _event: None,
        job_factory=_JobObject,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
        log_info=logs.append,
    )

    controller.launch(
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": "job-1",
            "profile": "Default",
            "manifest_path": str(manifest),
            "overwrite_mod": None,
            "job_object": JOB_OBJECT,
        }
    )
    organizer.release.set()
    controller.wait_for_monitors(timeout=1)

    assert llamadas_perf_counter >= 7
    assert len(logs) == 2
    assert "Sky-Claw VFS Bridge launch timings ts=" in logs[0]
    assert "job_id=job-1" in logs[0]
    assert "startApplication_ms=" in logs[0]
    assert "assign_ms=" in logs[0]
    assert "total_to_ack_ms=" in logs[0]
    assert "Sky-Claw VFS Bridge worker_exit confirmed ts=" in logs[1]
    assert "job_id=job-1" in logs[1]
    assert "wait_ok=True exit_code=7 wait_ms=" in logs[1]


def _controlador_con_log_info(
    tmp_path: pathlib.Path,
    organizer: _Organizer,
    log_info: object,
) -> tuple[BridgeLaunchController, dict[str, object], list[dict[str, object]], threading.Event]:
    """Controlador con dobles y un ``log_info`` a medida; ``salio`` se activa al enviarse ``worker_exit``."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifest = jobs / "job-1.json"
    manifest.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    eventos: list[dict[str, object]] = []
    salio = threading.Event()

    def _enviar(evento: dict[str, object]) -> None:
        eventos.append(evento)
        if evento.get("event") == "worker_exit":
            salio.set()

    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=_enviar,
        job_factory=_JobObject,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
        log_info=log_info,
    )
    mensaje: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "launch_worker",
        "job_id": "job-1",
        "profile": "Default",
        "manifest_path": str(manifest),
        "overwrite_mod": None,
        "job_object": JOB_OBJECT,
    }
    return controller, mensaje, eventos, salio


def test_una_falla_de_log_info_no_corta_el_lanzamiento_ni_deja_al_worker_sin_monitor(
    tmp_path: pathlib.Path,
) -> None:
    """La telemetría nunca altera el flujo del protocolo: ``launch`` sigue hasta arrancar el monitor."""
    organizer = _Organizer()

    def _log_roto(_mensaje: str) -> None:
        raise RuntimeError("qInfo no disponible")

    controller, mensaje, eventos, salio = _controlador_con_log_info(tmp_path, organizer, _log_roto)

    controller.launch(mensaje)
    organizer.release.set()

    assert salio.wait(timeout=2), "sin monitor no hay worker_exit: el broker nunca vería la salida del worker"
    tipos = [(evento["type"], evento.get("event")) for evento in eventos]
    assert ("launch_ack", None) in tipos
    assert ("event", "worker_exit") in tipos


def test_una_falla_de_log_info_en_la_salida_no_impide_enviar_worker_exit(tmp_path: pathlib.Path) -> None:
    """El evento terminal habilita rollback y libera locks en el broker: el log no puede impedirlo."""
    organizer = _Organizer()

    def _log_roto_al_salir(mensaje: str) -> None:
        if "worker_exit confirmed" in mensaje:
            raise RuntimeError("qInfo no disponible")

    controller, mensaje, eventos, salio = _controlador_con_log_info(tmp_path, organizer, _log_roto_al_salir)

    controller.launch(mensaje)
    organizer.release.set()

    assert salio.wait(timeout=2), "worker_exit no llegó: el log de la salida cortó el monitor"
    assert ("event", "worker_exit") in [(evento["type"], evento.get("event")) for evento in eventos]


def test_toda_llamada_a_log_info_del_controlador_esta_aislada_con_suppress() -> None:
    """Ancla: enumera los sitios de ``self._log_info(...)``; uno nuevo sin aislar rompe el test."""
    arbol = ast.parse(inspect.getsource(bridge_runtime))
    padres = {hijo: padre for padre in ast.walk(arbol) for hijo in ast.iter_child_nodes(padre)}

    def _es_llamada_a_log_info(nodo: ast.AST) -> bool:
        return (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "_log_info"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == "self"
        )

    def _aislada(nodo: ast.AST) -> bool:
        actual = nodo
        while actual in padres:
            actual = padres[actual]
            if isinstance(actual, ast.With) and any(
                isinstance(item.context_expr, ast.Call) and ast.unparse(item.context_expr.func) == "contextlib.suppress"
                for item in actual.items
            ):
                return True
        return False

    llamadas = [nodo for nodo in ast.walk(arbol) if _es_llamada_a_log_info(nodo)]

    assert len(llamadas) == 2  # el log de ``launch`` y el de ``_monitor``
    assert all(_aislada(nodo) for nodo in llamadas)


def _construcciones_del_controlador(fuente: str) -> list[str | None]:
    """Una entrada por CADA ``BridgeLaunchController(...)`` de ``fuente``: el ``breakaway_checker`` que pasa, o ``None``."""
    return [
        next((ast.unparse(clave.value) for clave in nodo.keywords if clave.arg == "breakaway_checker"), None)
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Call) and ast.unparse(nodo.func).rsplit(".", 1)[-1] == "BridgeLaunchController"
    ]


@pytest.mark.parametrize(
    ("fuente", "esperado"),
    [
        # Una construcción sin checker no puede quedar tapada por otra del mismo archivo que sí lo pasa (ni al revés).
        (
            "def f():\n    BridgeLaunchController(a=1)\n\ndef g():\n    BridgeLaunchController(a=1, breakaway_checker=h)\n",
            [None, "h"],
        ),
        (
            "def g():\n    BridgeLaunchController(a=1, breakaway_checker=h)\n\ndef f():\n    BridgeLaunchController(a=1)\n",
            [None, "h"],
        ),
        # Pasar ALGO no basta: se compara el valor, así que un no-op (``lambda: None``) queda a la vista.
        ("BridgeLaunchController(a=1, breakaway_checker=lambda: None)\n", ["lambda: None"]),
        ("BridgeLaunchController(a=1, breakaway_checker=h)\n", ["h"]),
        ("x = 1\n", []),
    ],
)
def test_construcciones_del_controlador_conserva_un_resultado_por_llamada(
    fuente: str, esperado: list[str | None]
) -> None:
    assert sorted(_construcciones_del_controlador(fuente), key=str) == esperado


def test_todo_constructor_de_produccion_del_controlador_pasa_breakaway_checker() -> None:
    """Ancla: el default ``lambda: None`` es solo para tests con dobles; producción debe pasar el checker real."""
    raiz = pathlib.Path(__file__).resolve().parents[1]
    sitios: dict[str, list[str | None]] = {}
    for ruta in sorted((raiz / "sky_claw").rglob("*.py")):
        construcciones = _construcciones_del_controlador(ruta.read_text(encoding="utf-8"))
        if construcciones:
            sitios[ruta.relative_to(raiz).as_posix()] = construcciones

    # Igualdad literal sobre el VALOR: un segundo constructor de producción, uno sin checker o uno con un no-op
    # (``lambda: None``) rompe el test hasta revisarlo.
    assert sitios == {"sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/plugin.py": ["verify_mo2_breakaway_allowed"]}
