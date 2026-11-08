"""Auditoría independiente H-01 (P1): el bridge contiene a TODO el árbol del worker, no solo al proceso que lanzó.

El hallazgo: en un ejecutable PyInstaller onefile, ``startApplication`` devuelve el HANDLE del *bootloader*; el
intérprete que ejecuta el worker es su HIJO, y puede haber nacido antes de que el bridge asigne al padre al Job
(``AssignProcessToJobObject`` no adopta a quien ya existía). P2 hace que ese intérprete aborte si no prueba su Job,
pero un proceso suelto fuera del Job sobrevive. Y el monitor del bridge esperaba solo al bootloader: emitía
``worker_exit`` (que el broker toma como terminalidad confirmada y con lo que libera la cuarentena) sin saber si
quedaba algo vivo.

Las dos mitades de P1, ancladas acá con dobles (el kernel real se mide en ``test_mo2_bridge_adopcion_windows.py``):

* **adopción**: tras asignar al lanzado, se adoptan sus descendientes vivos que no estén en el Job, hasta un punto
  fijo; si alguno no se puede adoptar, NO hay ``launch_ack`` y se termina el Job;
* **confirmación**: ``worker_exit`` solo se emite con el Job VACÍO confirmado; si no se puede confirmar, el bridge
  emite ``bridge_error`` de terminación (el broker pone la instancia en cuarentena) y NUNCA ``worker_exit``.

La adopción identifica a un hijo por (PPID ∈ procesos retenidos) Y (creación posterior a la del padre), con los
handles de los ya vistos ABIERTOS durante todo el recorrido: así un PID reutilizado no se confunde con un hijo.
"""

from __future__ import annotations

import ast
import pathlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime as puente
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    BridgeLaunchController,
    BridgeTerminationError,
    adopt_descendants,
)
from tests.test_vfs_job_identity_bridge import NOMBRE, _escenario, _mensaje, _Organizer

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = RAIZ / "sky_claw" / "local" / "mo2" / "plugin_bundle" / "skyclaw_bridge" / "runtime.py"

PID_RAIZ = 100
HANDLE_RAIZ = 1100
CREACION_RAIZ = 50


# --------------------------------------------------------------------------------------------------------------------
# Tabla de procesos en memoria (el borde: lo único que la adopción necesita del sistema operativo)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Proc:
    ppid: int
    creacion: int
    vivo: bool = True


class _ApiFalsa:
    """Los procesos del sistema como datos. El handle de un pid es ``1000 + pid`` (el de la raíz lo da el bridge)."""

    def __init__(self) -> None:
        self.tabla: dict[int, _Proc] = {PID_RAIZ: _Proc(ppid=1, creacion=CREACION_RAIZ)}
        self.en_job: set[int] = {PID_RAIZ}
        self.asignados: list[int] = []
        self.terminados: list[int] = []
        self.abiertos: list[int] = []
        self.cerrados: list[int] = []
        self.fallar_abrir: set[int] = set()
        self.desaparece_al_abrir: set[int] = set()
        self.fallar_asignar: set[int] = set()
        self.muere_al_asignar: set[int] = set()
        self.no_muere: set[int] = set()
        self.al_asignar: dict[int, Callable[[], None]] = {}

    def hijo(self, pid: int, ppid: int, creacion: int, *, en_job: bool = False) -> None:
        self.tabla[pid] = _Proc(ppid=ppid, creacion=creacion)
        if en_job:
            self.en_job.add(pid)

    @staticmethod
    def _pid(handle: int) -> int:
        return handle - 1000

    def processes(self) -> list[tuple[int, int]]:
        return [(pid, p.ppid) for pid, p in self.tabla.items() if p.vivo]

    def process_id(self, handle: int) -> int:
        return self._pid(handle)

    def open_process(self, pid: int) -> int | None:
        if pid in self.fallar_abrir:
            raise BridgeCommandError(f"OpenProcess({pid}) denegado")
        proceso = self.tabla.get(pid)
        if proceso is None or not proceso.vivo:
            return None
        if pid in self.desaparece_al_abrir:
            proceso.vivo = False
            return None
        self.abiertos.append(1000 + pid)
        return 1000 + pid

    def creation_time(self, handle: int) -> int:
        return self.tabla[self._pid(handle)].creacion

    def has_exited(self, handle: int) -> bool:
        return not self.tabla[self._pid(handle)].vivo

    def in_job(self, handle: int) -> bool:
        return self._pid(handle) in self.en_job

    def assign(self, handle: int) -> None:
        pid = self._pid(handle)
        if pid in self.muere_al_asignar:
            self.tabla[pid].vivo = False
            raise BridgeCommandError("AssignProcessToJobObject falló: 5")
        if pid in self.fallar_asignar:
            raise BridgeCommandError("AssignProcessToJobObject falló: 5")
        self.en_job.add(pid)
        self.asignados.append(pid)
        gancho = self.al_asignar.get(pid)
        if gancho is not None:
            gancho()

    def terminate(self, handle: int) -> None:
        pid = self._pid(handle)
        self.terminados.append(pid)
        if pid in self.no_muere:
            raise BridgeCommandError(f"TerminateProcess({pid}) falló")
        self.tabla[pid].vivo = False

    def close(self, handle: int) -> None:
        self.cerrados.append(handle)


def _adoptar(api: _ApiFalsa, **kwargs: int) -> int:
    return adopt_descendants(api, HANDLE_RAIZ, **kwargs)


# --------------------------------------------------------------------------------------------------------------------
# Adopción
# --------------------------------------------------------------------------------------------------------------------


def test_adopta_al_hijo_previo_que_esta_fuera_del_job() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)

    adoptados = _adoptar(api)

    assert adoptados == 1
    assert api.asignados == [101]
    assert 101 in api.en_job


def test_no_vuelve_a_asignar_a_quien_ya_esta_en_el_job() -> None:
    """Un hijo nacido DESPUÉS de la asignación del padre hereda el Job: asignarlo otra vez sería ruido (o un error)."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60, en_job=True)

    assert _adoptar(api) == 0
    assert api.asignados == []


def test_adopta_tambien_a_los_nietos_hasta_un_punto_fijo() -> None:
    """Un nieto nacido antes de que se adopte a su padre aparece recién en la pasada siguiente."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.al_asignar[101] = lambda: api.hijo(102, 101, 61)  # el hijo ya había creado al nieto

    adoptados = _adoptar(api)

    assert adoptados == 2
    assert api.asignados == [101, 102]


def test_un_nieto_cuyo_padre_ya_murio_se_sigue_adoptando_porque_su_handle_se_retiene() -> None:
    """El intermediario muere pero su handle sigue abierto: su PID no puede reutilizarse y el nieto no se pierde."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)

    def _padre_muere_y_deja_un_nieto() -> None:
        api.tabla[101].vivo = False
        api.hijo(102, 101, 61)

    api.al_asignar[101] = _padre_muere_y_deja_un_nieto

    adoptados = _adoptar(api)

    assert api.asignados == [101, 102]
    assert adoptados == 2


def test_nunca_toca_a_un_proceso_que_no_desciende_del_lanzado() -> None:
    api = _ApiFalsa()
    api.hijo(300, 999, 70)  # ppid ajeno

    assert _adoptar(api) == 0
    assert api.abiertos == [], "ni siquiera se abre un proceso ajeno"


def test_un_ppid_coincidente_con_creacion_anterior_a_la_del_padre_no_es_un_hijo() -> None:
    """PID reutilizado: un proceso viejo cuyo PPID numérico coincide con el nuestro nació ANTES que el padre."""
    api = _ApiFalsa()
    api.hijo(150, PID_RAIZ, CREACION_RAIZ - 10)

    assert _adoptar(api) == 0
    assert api.asignados == []
    assert 1150 in api.cerrados, "se abrió para leer su creación y el handle no puede quedar abierto"


def test_un_hijo_que_desaparece_entre_la_enumeracion_y_la_apertura_no_es_un_fallo() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.desaparece_al_abrir.add(101)

    assert _adoptar(api) == 0


def test_un_hijo_que_muere_durante_la_asignacion_no_es_un_fallo() -> None:
    """Asignar un proceso que ya salió falla con ACCESS_DENIED: si murió, no hay nada que contener."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.muere_al_asignar.add(101)

    assert _adoptar(api) == 0
    assert api.terminados == []


def test_un_hijo_vivo_que_no_se_puede_adoptar_se_termina_y_el_error_lo_nombra() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.fallar_asignar.add(101)

    with pytest.raises(BridgeCommandError, match="101"):
        _adoptar(api)

    assert api.terminados == [101], "un descendiente que no se puede contener no puede quedar vivo"


@pytest.mark.parametrize("culpable", [101, 102, 103])
def test_el_fallo_de_un_hijo_no_impide_adoptar_a_los_demas(culpable: int) -> None:
    """Misma propiedad que H-03: el error es por PID, no corta la ronda."""
    api = _ApiFalsa()
    for pid in (101, 102, 103):
        api.hijo(pid, PID_RAIZ, 60)
    api.fallar_asignar.add(culpable)

    with pytest.raises(BridgeCommandError, match=str(culpable)):
        _adoptar(api)

    assert sorted(api.asignados) == sorted({101, 102, 103} - {culpable})


def test_un_hijo_que_no_se_puede_abrir_es_un_fallo_porque_no_se_puede_probar_su_contencion() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.hijo(102, PID_RAIZ, 60)
    api.fallar_abrir.add(101)

    with pytest.raises(BridgeCommandError, match="101"):
        _adoptar(api)

    assert api.asignados == [102]


def test_si_ni_siquiera_se_puede_terminar_al_que_no_se_adopto_el_error_lo_dice() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.fallar_asignar.add(101)
    api.no_muere.add(101)

    with pytest.raises(BridgeCommandError, match=r"101.*TerminateProcess"):
        _adoptar(api)


def test_si_no_converge_en_las_pasadas_permitidas_falla_cerrado() -> None:
    """Algo que crea procesos más rápido de lo que se adoptan no se da por contenido."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    siguiente = iter(range(102, 200))

    def _otro_hijo_cada_vez() -> None:
        api.hijo(next(siguiente), PID_RAIZ, 61)
        api.al_asignar[max(api.tabla)] = _otro_hijo_cada_vez

    api.al_asignar[101] = _otro_hijo_cada_vez

    with pytest.raises(BridgeCommandError, match="no convergió"):
        _adoptar(api, max_passes=3)


def test_si_no_converge_el_error_es_de_terminacion_porque_no_se_puede_afirmar_que_no_quedo_nada_fuera() -> None:
    """Sin punto fijo hay procesos nacidos después de la última pasada: no se sabe si alguno quedó fuera del Job."""
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    siguiente = iter(range(102, 200))

    def _otro_hijo_cada_vez() -> None:
        api.hijo(next(siguiente), PID_RAIZ, 61)
        api.al_asignar[max(api.tabla)] = _otro_hijo_cada_vez

    api.al_asignar[101] = _otro_hijo_cada_vez

    with pytest.raises(BridgeTerminationError, match="no convergió"):
        _adoptar(api, max_passes=3)


# Auditoría independiente, hallazgo de Codex (P1) sobre #877: el camino del ``launch`` debe distinguir entre
# «no se pudo contener PERO se terminó» (un rechazo ordinario: nada vivo) y «queda un descendiente vivo fuera del Job»
# (terminalidad desconocida, como la del monitor: el broker debe poner la instancia en cuarentena). La tabla ENUMERA
# los escenarios; un modo de fallo nuevo en ``adopt_descendants`` obliga a clasificarlo acá.
def _asignar_falla_y_terminar_ok(api: _ApiFalsa) -> None:
    api.hijo(101, PID_RAIZ, 60)
    api.fallar_asignar.add(101)


def _asignar_y_terminar_fallan(api: _ApiFalsa) -> None:
    api.hijo(101, PID_RAIZ, 60)
    api.fallar_asignar.add(101)
    api.no_muere.add(101)


def _no_se_puede_abrir(api: _ApiFalsa) -> None:
    api.hijo(101, PID_RAIZ, 60)
    api.fallar_abrir.add(101)


def _uno_terminado_y_otro_vivo(api: _ApiFalsa) -> None:
    api.hijo(101, PID_RAIZ, 60)
    api.hijo(102, PID_RAIZ, 60)
    api.fallar_asignar.update({101, 102})
    api.no_muere.add(102)


@pytest.mark.parametrize(
    ("preparar", "queda_vivo_fuera_del_job"),
    [
        pytest.param(_asignar_falla_y_terminar_ok, False, id="no_se_adopta_pero_se_termina"),
        pytest.param(_asignar_y_terminar_fallan, True, id="no_se_adopta_ni_se_termina"),
        pytest.param(_no_se_puede_abrir, True, id="no_se_puede_abrir"),
        pytest.param(_uno_terminado_y_otro_vivo, True, id="mixto_queda_uno_vivo"),
    ],
)
def test_solo_un_descendiente_vivo_fuera_del_job_hace_el_error_de_terminacion(
    preparar: Callable[[_ApiFalsa], None], queda_vivo_fuera_del_job: bool
) -> None:
    api = _ApiFalsa()
    preparar(api)

    with pytest.raises(BridgeCommandError, match="no se pudo contener a los descendientes") as salida:
        _adoptar(api)

    assert isinstance(salida.value, BridgeTerminationError) is queda_vivo_fuera_del_job


def test_el_error_de_terminacion_nombra_a_los_que_quedaron_vivos_y_no_a_los_terminados() -> None:
    api = _ApiFalsa()
    _uno_terminado_y_otro_vivo(api)

    with pytest.raises(BridgeTerminationError) as salida:
        _adoptar(api)

    mensaje = str(salida.value)
    assert "vivos fuera del Job" in mensaje
    assert "[102]" in mensaje, "debe nombrar el PID que quedó vivo"
    assert "[101" not in mensaje, "el PID terminado con éxito no está vivo"


def test_el_controlador_conserva_el_tipo_de_terminacion_al_rechazar_el_launch(tmp_path: pathlib.Path) -> None:
    """El ``launch`` limpia el Job y reenvía: no puede degradar el error de terminación a uno ordinario."""
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(
        tmp_path, bitacora, eventos, falla_adopcion=BridgeTerminationError("quedan vivos fuera del Job: [101]")
    )
    organizer.liberar.set()

    with pytest.raises(BridgeTerminationError, match="101"):
        controlador.launch(_mensaje(manifiesto))

    assert "evento:launch_ack" not in bitacora
    assert controlador._jobs == {}


@pytest.mark.parametrize(
    ("tipo", "excepcion", "comando", "kind"),
    [
        pytest.param(
            "launch_worker", BridgeTerminationError("vivos"), "terminate", "termination_failed", id="launch_con_vivos"
        ),
        pytest.param("launch_worker", BridgeCommandError("no"), "launch_worker", "rejected", id="launch_rechazado"),
        pytest.param("launch_worker", OSError("disco"), "launch_worker", "rejected", id="launch_oserror"),
        pytest.param("cancel", BridgeTerminationError("vivos"), "cancel", "termination_failed", id="cancel_con_vivos"),
        pytest.param("cancel", BridgeCommandError("no"), "cancel", "rejected", id="cancel_rechazado"),
    ],
)
def test_el_evento_de_error_clasifica_los_vivos_del_launch_como_terminacion(
    tipo: str, excepcion: Exception, comando: str, kind: str
) -> None:
    """El broker pone en cuarentena solo ante ``cancel``/``terminate``: un launch con vivos se reporta como ``terminate``."""
    evento = puente.bridge_error_event_for_command({"type": tipo, "job_id": "job-1"}, excepcion)

    assert evento["command"] == comando
    assert evento["kind"] == kind
    assert evento["job_id"] == "job-1"
    assert evento["event"] == "bridge_error"


def test_el_plugin_reporta_los_errores_solo_por_la_funcion_que_clasifica() -> None:
    """Ancla: un tercer sitio que arme ``bridge_error`` con ``command=message.get('type')`` saltearía la clasificación."""
    plugin = RUNTIME.parent / "plugin.py"
    arbol = ast.parse(plugin.read_text(encoding="utf-8"))
    llamadas = [
        nodo.func.id
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id.startswith("bridge_error_event")
    ]

    assert llamadas == ["bridge_error_event_for_command"]


def test_cierra_todos_los_handles_que_abrio_y_nunca_el_de_la_raiz() -> None:
    api = _ApiFalsa()
    api.hijo(101, PID_RAIZ, 60)
    api.hijo(102, PID_RAIZ, 60)
    api.fallar_asignar.add(102)

    with pytest.raises(BridgeCommandError):
        _adoptar(api)

    assert sorted(api.cerrados) == sorted(api.abiertos)
    assert HANDLE_RAIZ not in api.cerrados, "el handle de la raíz es del bridge/MO2, no de la adopción"


# --------------------------------------------------------------------------------------------------------------------
# El controlador: adopción antes del ack, y worker_exit solo con el Job vacío
# --------------------------------------------------------------------------------------------------------------------


class _JobFalso:
    def __init__(
        self,
        nombre: str,
        bitacora: list[str],
        *,
        activos: tuple[int, ...] = (0,),
        falla_adopcion: Exception | None = None,
        falla_terminate: Exception | None = None,
        falla_consulta: Exception | None = None,
    ) -> None:
        self.nombre = nombre
        self.bitacora = bitacora
        self.activos = list(activos)
        self.falla_adopcion = falla_adopcion
        self.falla_terminate = falla_terminate
        self.falla_consulta = falla_consulta
        bitacora.append(f"job:{nombre}")

    def assign(self, process_handle: int) -> None:
        self.bitacora.append("assign")

    def adopt_descendants(self, process_handle: int) -> int:
        self.bitacora.append("adopt")
        if self.falla_adopcion is not None:
            raise self.falla_adopcion
        return 0

    def terminate(self) -> None:
        self.bitacora.append("terminate")
        if self.falla_terminate is not None:
            raise self.falla_terminate

    def active_processes(self) -> int:
        self.bitacora.append("active")
        if self.falla_consulta is not None:
            raise self.falla_consulta
        return self.activos.pop(0) if len(self.activos) > 1 else self.activos[0]

    def close(self) -> None:
        self.bitacora.append("close")


def _controlador(
    tmp_path: pathlib.Path,
    bitacora: list[str],
    eventos: list[dict[str, object]],
    *,
    waiter: Callable[[int], tuple[bool, int]] | None = None,
    **opciones_del_job: object,
) -> tuple[BridgeLaunchController, _Organizer, pathlib.Path]:
    jobs, manifiesto, worker = _escenario(tmp_path)
    organizer = _Organizer(bitacora)

    def _enviar(evento: dict[str, object]) -> None:
        bitacora.append(f"evento:{evento.get('event') or evento.get('type')}")
        eventos.append(evento)

    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=tmp_path / "descriptor.json",
        jobs_root=jobs,
        send_event=_enviar,
        job_factory=lambda nombre: _JobFalso(nombre, bitacora, **opciones_del_job),  # type: ignore[arg-type]
        process_waiter=waiter or (lambda handle: organizer.waitForApplication(handle, False)),
        job_empty_timeout_s=0.05,
        job_empty_poll_s=0.001,
    )
    return controlador, organizer, manifiesto


def _esperar_evento(eventos: list[dict[str, object]], nombre: str, *, timeout: float = 5.0) -> dict[str, object]:
    fin = threading.Event()
    for _ in range(int(timeout / 0.01)):
        for evento in eventos:
            if evento.get("event") == nombre:
                return evento
        fin.wait(0.01)
    raise AssertionError(f"no apareció el evento {nombre!r}: {eventos}")


def test_launch_adopta_a_los_descendientes_despues_de_asignar_y_antes_del_ack(tmp_path: pathlib.Path) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, eventos)

    controlador.launch(_mensaje(manifiesto))

    assert bitacora[:5] == [f"job:{NOMBRE}", "startApplication", "assign", "adopt", "evento:launch_ack"]
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=2)


def test_si_la_adopcion_falla_no_hay_ack_y_el_job_se_termina_y_se_cierra(tmp_path: pathlib.Path) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(
        tmp_path, bitacora, eventos, falla_adopcion=BridgeCommandError("no se pudo adoptar a 101")
    )
    organizer.liberar.set()  # que ``waitForApplication`` del camino de error no espere el plazo completo

    with pytest.raises(BridgeCommandError, match="101"):
        controlador.launch(_mensaje(manifiesto))

    assert "evento:launch_ack" not in bitacora, "sin contención completa el broker no autoriza al worker"
    assert eventos == []
    assert "terminate" in bitacora
    assert bitacora[-1] == "close"
    assert controlador._jobs == {}, "no queda registrado un worker que no se pudo contener"


def _monitor_directo(controlador: BridgeLaunchController, manifiesto: pathlib.Path) -> None:
    """Registra el job como lo haría ``launch`` y corre ``_monitor`` en el hilo del test (determinista)."""
    request = puente.validate_launch_request(_mensaje(manifiesto), jobs_root=controlador._jobs_root)
    job = controlador._job_factory(request.job_object)
    running = puente._RunningJob(request=request, process_handle=7, job_object=job)
    controlador._jobs[request.job_id] = running
    controlador._monitor(running)


def test_worker_exit_se_emite_solo_tras_confirmar_el_job_vacio(tmp_path: pathlib.Path) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path, bitacora, eventos, waiter=lambda handle: (True, 0), activos=(2, 1, 0)
    )

    _monitor_directo(controlador, manifiesto)

    esperado = [f"job:{NOMBRE}", "active", "terminate", "active", "active", "close", "evento:worker_exit"]
    assert bitacora == esperado
    assert eventos[-1]["event"] == "worker_exit"
    assert controlador._jobs == {}


def test_si_el_job_ya_esta_vacio_no_se_termina_nada(tmp_path: pathlib.Path) -> None:
    """Salida natural: el Job vacío solo se cierra (el ciclo de vida de siempre), no se le manda ``Terminate``."""
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(tmp_path, bitacora, eventos, waiter=lambda handle: (True, 0))

    _monitor_directo(controlador, manifiesto)

    assert bitacora == [f"job:{NOMBRE}", "active", "close", "evento:worker_exit"]


def test_si_el_job_conserva_procesos_no_hay_worker_exit_sino_bridge_error_de_terminacion(
    tmp_path: pathlib.Path,
) -> None:
    """El broker libera la cuarentena con CUALQUIER ``worker_exit``: emitirlo con procesos vivos sería mentir."""
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path, bitacora, eventos, waiter=lambda handle: (True, 0), activos=(1,)
    )

    _monitor_directo(controlador, manifiesto)

    assert [e.get("event") for e in eventos] == ["bridge_error"]
    error = eventos[0]
    assert error["command"] == "terminate"
    assert error["kind"] == "termination_failed"
    assert error["job_id"] == "job-1"
    assert "activos=1" in str(error["message"])
    assert "close" in bitacora, "el handle se cierra igual: kill-on-close es la última red"
    assert controlador._jobs == {}


def test_una_falla_de_log_info_no_se_traga_el_error_de_terminacion_si_el_job_conserva_procesos(
    tmp_path: pathlib.Path,
) -> None:
    """Combinación de #871 (la telemetría no corta el flujo) y #877 (el Job que no se vacía avisa al broker).

    El log de la salida se emite ANTES de decidir entre ``worker_exit`` y ``bridge_error``: si su falla escapara, el
    broker no recibiría ninguno de los dos y esperaría el plazo completo con un Job que sigue vivo.
    """
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path, bitacora, eventos, waiter=lambda handle: (True, 0), activos=(1,)
    )

    def _log_roto(_mensaje: str) -> None:
        raise RuntimeError("qInfo no disponible")

    controlador._log_info = _log_roto

    _monitor_directo(controlador, manifiesto)

    assert [e.get("event") for e in eventos] == ["bridge_error"]
    assert eventos[0]["kind"] == "termination_failed"
    assert controlador._jobs == {}


@pytest.mark.parametrize(
    ("opciones_del_job", "esperado"),
    [({"activos": (1,)}, "job_empty=False"), ({}, "job_empty=True")],
    ids=["job_con_procesos", "job_vacio"],
)
def test_el_log_de_salida_informa_si_el_job_quedo_vacio(
    tmp_path: pathlib.Path, opciones_del_job: dict[str, object], esperado: str
) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path, bitacora, eventos, waiter=lambda handle: (True, 0), **opciones_del_job
    )
    logs: list[str] = []
    controlador._log_info = logs.append

    _monitor_directo(controlador, manifiesto)

    salida = [linea for linea in logs if "worker_exit confirmed" in linea]
    assert len(salida) == 1
    assert esperado in salida[0]


def test_si_no_se_puede_consultar_el_job_tampoco_se_confirma_la_salida(tmp_path: pathlib.Path) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path,
        bitacora,
        eventos,
        waiter=lambda handle: (True, 0),
        falla_consulta=BridgeCommandError("QueryInformationJobObject falló: 6"),
    )

    _monitor_directo(controlador, manifiesto)

    assert [e.get("event") for e in eventos] == ["bridge_error"]
    assert "QueryInformationJobObject" in str(eventos[0]["message"])
    assert "close" in bitacora


def test_si_terminate_falla_pero_el_job_ya_esta_vacio_se_confirma_igual(tmp_path: pathlib.Path) -> None:
    """Lo que decide es lo que sigue vivo, no si el kernel aceptó terminar un Job que ya no tenía nada."""
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(
        tmp_path,
        bitacora,
        eventos,
        waiter=lambda handle: (True, 0),
        activos=(1, 0),  # había un proceso, el kernel rechazó el Terminate, pero cuando se vuelve a mirar ya no está
        falla_terminate=BridgeTerminationError("TerminateJobObject falló: 5"),
    )

    _monitor_directo(controlador, manifiesto)

    assert [e.get("event") for e in eventos] == ["worker_exit"]


def test_un_fallo_del_waiter_se_reporta_en_el_worker_exit_pero_no_impide_confirmar_el_job(
    tmp_path: pathlib.Path,
) -> None:
    def _waiter_roto(handle: int) -> tuple[bool, int]:
        raise OSError("WaitForSingleObject")

    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _o, manifiesto = _controlador(tmp_path, bitacora, eventos, waiter=_waiter_roto)

    _monitor_directo(controlador, manifiesto)

    assert eventos[-1]["event"] == "worker_exit"
    assert eventos[-1]["wait_ok"] is False
    assert "WaitForSingleObject" in str(eventos[-1]["message"])


def test_el_flujo_completo_con_hilo_de_monitor_emite_ack_y_luego_worker_exit(tmp_path: pathlib.Path) -> None:
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, eventos)

    controlador.launch(_mensaje(manifiesto))
    organizer.liberar.set()
    _esperar_evento(eventos, "worker_exit")

    assert [e.get("type") for e in eventos] == ["launch_ack", "event"]


# --------------------------------------------------------------------------------------------------------------------
# Anclas por AST (enumeran la familia)
# --------------------------------------------------------------------------------------------------------------------


def _metodo(clase: str, nombre: str) -> ast.FunctionDef:
    arbol = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    cls = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == clase)
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == nombre)


def _lineas_de_llamadas(funcion: ast.FunctionDef) -> dict[str, int]:
    lineas: dict[str, int] = {}
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            lineas.setdefault(nodo.func.attr, nodo.lineno)
    return lineas


def test_launch_asigna_adopta_y_recien_entonces_confirma() -> None:
    """Orden sobre el árbol de ``launch``: assign → adopt_descendants → _send_event(launch_ack)."""
    lineas = _lineas_de_llamadas(_metodo("BridgeLaunchController", "launch"))
    assert lineas["assign"] < lineas["adopt_descendants"] < lineas["_send_event"]


def test_el_monitor_confirma_el_job_vacio_antes_de_emitir_cualquier_evento() -> None:
    monitor = _metodo("BridgeLaunchController", "_monitor")
    lineas = _lineas_de_llamadas(monitor)
    assert lineas["_terminate_and_confirm_empty"] < lineas["_send_event"]
    # Los dos eventos posibles del monitor salen de ``_send_event`` DESPUÉS de la confirmación, y solo uno por
    # cada resultado: un tercer ``_send_event`` sin revisar rompe este ancla.
    emisiones = [
        n for n in ast.walk(monitor) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_send_event"
    ]
    assert len(emisiones) == 2


def test_la_unica_implementacion_de_produccion_del_job_cumple_el_contrato_completo() -> None:
    """``JobObject`` (Protocol) y ``Win32JobObject`` exponen EXACTAMENTE los mismos métodos públicos."""
    arbol = ast.parse(RUNTIME.read_text(encoding="utf-8"))

    def publicos(clase: str) -> set[str]:
        cls = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == clase)
        return {n.name for n in cls.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}

    contrato = {"assign", "adopt_descendants", "terminate", "active_processes", "close"}
    assert publicos("JobObject") == contrato
    assert publicos("Win32JobObject") == contrato
