"""D1 de E3-R2: el argv del worker no puede fragmentarse en los espacios.

Contrato real de MO2 2.5.2, leído en el fuente de la release (no inferido):

* ``OrganizerProxy::startApplication`` (``src/organizerproxy.cpp``) recibe la
  ``QStringList`` de argumentos y la entrega a
  ``ProcessRunner::setFromFileOrExecutable``.
* ``ProcessRunner::setFromFileOrExecutable`` (``src/processrunner.cpp``) hace
  ``setArguments(args.join(" "))``: **une los elementos con un espacio y no cita
  ninguno**.
* ``spawn::spawn`` (``src/spawn.cpp``) arma la línea como el binario entre
  comillas dobles, un espacio, y ``arguments`` **tal cual**; después la pasa a
  ``CreateProcess``.

Conclusión: para MO2 cada elemento de la lista es un *fragmento ya serializado*
de línea de comandos, no un argumento semántico. Pasar
``["--manifest", "C:\\\\Rig A\\\\job.json"]`` produce la línea
``--manifest C:\\Rig A\\job.json`` y el parser CRT del worker ve dos tokens:
``C:\\Rig A`` y ``job.json``.

Ese es el fallo medido en E3-R2B: muestra 38 de ``r2b.json``
(``--manifest C:\\SkyClaw_Rigs\\A209`` + ``F1\\home\\...json`` suelto) y exit
code 2 en ``_parse_worker_args``, antes de cualquier atestación de canario.

Estos tests modelan el transporte de MO2 de punta a punta: se toma lo que el
controlador le pasa a ``startApplication``, se reconstruye la línea de comandos
igual que ``spawn::spawn`` y se parsea con ``CommandLineToArgvW``, que es el
mismo parser que aplica el proceso hijo. No alcanza con mirar si la lista
original contiene un string con espacios.
"""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Sequence

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime as bridge
from sky_claw.local.mo2.vfs_worker import _parse_worker_args

RUTA_DE_RIG_CON_ESPACIOS = "A209 F1"
JOB_ID_DE_REFERENCIA = "c0ee146e-159f-49f0-95c2-5bfd58e8018f"


# ---------------------------------------------------------------------------
# Modelo del transporte de MO2 2.5.2
# ---------------------------------------------------------------------------


def _linea_de_comandos_de_mo2(executable: pathlib.Path, fragmentos: Sequence[str]) -> str:
    """Reproduce ``spawn::spawn`` de MO2 2.5.2 sobre una lista de fragmentos.

    ``QString commandLine = "\\"" + bin + "\\"";`` y, si hay argumentos,
    ``commandLine += " " + sp.arguments``, donde ``sp.arguments`` es
    ``args.join(" ")``. No hay ninguna cita adicional: ése es el defecto.
    """
    linea = f'"{executable}"'
    if fragmentos:
        linea += " " + " ".join(fragmentos)
    return linea


def _argv_del_proceso(linea: str) -> list[str]:
    """Parsea la línea con el mismo criterio que el proceso hijo.

    En Windows usa ``CommandLineToArgvW``, el parser autoritativo: es el que
    CPython aplica sobre ``GetCommandLineW`` para construir ``sys.argv``. Fuera
    de Windows no hay contrato CRT que probar y el test se saltea.
    """
    if sys.platform != "win32":
        pytest.skip("CommandLineToArgvW sólo existe en Windows")
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    cantidad = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(linea, ctypes.byref(cantidad))
    if not argv:
        raise AssertionError(f"CommandLineToArgvW falló con la línea {linea!r}")
    try:
        return [argv[indice] for indice in range(cantidad.value)]
    finally:
        kernel32.LocalFree(argv)


def _argv_que_recibe_el_worker(organizer: _Organizer, worker: pathlib.Path) -> list[str]:
    """Toma lo que el controlador pasó a ``startApplication`` y lo parsea como el hijo."""
    _, fragmentos, *_ = organizer.calls[0]
    linea = _linea_de_comandos_de_mo2(worker, fragmentos)
    return _argv_del_proceso(linea)[1:]  # descarta argv[0], que es el ejecutable


def _argv_para_el_parser(argv: list[str]) -> list[str]:
    """Reproduce el despacho de ``__main__.main``: el prefijo fijo se consume.

    ``main`` sólo entra al worker si ``argv[0]`` es ``--vfs-worker`` y le pasa el
    resto a ``worker_main``. Por eso el prefijo se afirma acá y se descarta antes
    de llamar al parser.
    """
    assert argv[0] == "--vfs-worker", f"el prefijo fijo del worker se perdió: {argv!r}"
    return argv[1:]


# ---------------------------------------------------------------------------
# Geometría del rig E3-R2
# ---------------------------------------------------------------------------


def _preparar_rig(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Arma la geometría real del rig: raíz, jobs y app bajo una carpeta con espacio."""
    raiz = tmp_path / RUTA_DE_RIG_CON_ESPACIOS
    jobs = raiz / "home" / ".sky_claw" / "vfs_bridge" / "mo2-c7027ac56c8d4887" / "jobs"
    jobs.mkdir(parents=True)
    manifest = jobs / f"{JOB_ID_DE_REFERENCIA}.json"
    manifest.write_text("{}", encoding="utf-8")
    descriptor = jobs.parent / "mo2-c7027ac56c8d4887.json"
    descriptor.write_text("{}", encoding="utf-8")
    app = raiz / "app"
    app.mkdir()
    worker = app / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    return {
        "raiz": raiz,
        "jobs": jobs,
        "manifest": manifest,
        "descriptor": descriptor,
        "worker": worker,
    }


def _vector_semantico(rig: dict[str, pathlib.Path], *, job_id: str = JOB_ID_DE_REFERENCIA) -> list[str]:
    """El argv que el worker *debe* ver, sin comillas: conceptos A del brief."""
    return [
        "--vfs-worker",
        "--manifest",
        str(rig["manifest"].resolve()),
        "--descriptor",
        str(rig["descriptor"].resolve()),
        "--job-id",
        job_id,
    ]


# ---------------------------------------------------------------------------
# Dobles de la API MO2
# ---------------------------------------------------------------------------


class _Organizer:
    """Captura los seis argumentos de ``startApplication`` tal como los ve MO2."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.release = threading.Event()

    def startApplication(self, *args: object) -> int:  # noqa: N802 - fake de API MO2
        self.calls.append(args)
        return 123

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802 - fake de API MO2
        assert handle == 123
        assert refresh is False
        self.release.wait(timeout=1)
        return True, 0


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


def _lanzar(
    rig: dict[str, pathlib.Path],
    organizer: _Organizer,
    job_object: _JobObject,
    eventos: list[dict[str, object]],
    *,
    job_id: str = JOB_ID_DE_REFERENCIA,
    overwrite_mod: str | None = None,
) -> bridge.BridgeLaunchController:
    controlador = bridge.BridgeLaunchController(
        organizer=organizer,
        worker_executable=rig["worker"],
        worker_prefix=("--vfs-worker",),
        descriptor_path=rig["descriptor"],
        jobs_root=rig["jobs"],
        send_event=eventos.append,
        job_factory=lambda: job_object,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    controlador.launch(
        {
            "protocol_version": bridge.PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": job_id,
            "profile": "A209RigStandalone",
            "manifest_path": str(rig["manifest"]),
            "overwrite_mod": overwrite_mod,
        }
    )
    return controlador


# ---------------------------------------------------------------------------
# D1-01 / D1-02 — cada valor con espacios llega entero
# ---------------------------------------------------------------------------


def test_d1_01_el_manifest_con_espacios_llega_como_un_solo_argumento(tmp_path: pathlib.Path) -> None:
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    _lanzar(rig, organizer, _JobObject(), [])

    argv = _argv_que_recibe_el_worker(organizer, rig["worker"])

    assert "--manifest" in argv, f"el worker no recibió --manifest; argv={argv!r}"
    posicion = argv.index("--manifest")
    assert argv[posicion + 1] == str(rig["manifest"].resolve()), (
        "el manifest con espacios llegó fragmentado: "
        f"esperado {str(rig['manifest'].resolve())!r}, recibido {argv[posicion + 1 : posicion + 3]!r}"
    )


def test_d1_02_el_descriptor_con_espacios_llega_como_un_solo_argumento(tmp_path: pathlib.Path) -> None:
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    _lanzar(rig, organizer, _JobObject(), [])

    argv = _argv_que_recibe_el_worker(organizer, rig["worker"])

    assert "--descriptor" in argv, f"el worker no recibió --descriptor; argv={argv!r}"
    posicion = argv.index("--descriptor")
    assert argv[posicion + 1] == str(rig["descriptor"].resolve()), (
        "el descriptor con espacios llegó fragmentado: "
        f"esperado {str(rig['descriptor'].resolve())!r}, recibido {argv[posicion + 1 : posicion + 3]!r}"
    )


# ---------------------------------------------------------------------------
# D1-03 — vector completo, sin tokens extra ni omisiones
# ---------------------------------------------------------------------------


def test_d1_03_el_vector_completo_con_ambos_paths_espaciados_es_exacto(tmp_path: pathlib.Path) -> None:
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    _lanzar(rig, organizer, _JobObject(), [])

    argv = _argv_que_recibe_el_worker(organizer, rig["worker"])

    assert argv == _vector_semantico(rig), (
        "el vector que recibe el worker no coincide con el autorizado: "
        f"esperado {_vector_semantico(rig)!r}, recibido {argv!r}"
    )


def test_d1_03_el_worker_parsea_el_manifest_y_el_descriptor_reales(tmp_path: pathlib.Path) -> None:
    """Cierra el círculo con el parser de producción del worker, no con un mock."""
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    _lanzar(rig, organizer, _JobObject(), [])

    argv = _argv_que_recibe_el_worker(organizer, rig["worker"])

    namespace = _parse_worker_args(_argv_para_el_parser(argv))

    assert namespace.manifest == rig["manifest"].resolve()
    assert namespace.descriptor == rig["descriptor"].resolve()
    assert namespace.job_id == JOB_ID_DE_REFERENCIA


# ---------------------------------------------------------------------------
# D1-04 — compatibilidad con instalaciones sin espacios
# ---------------------------------------------------------------------------


def test_d1_04_los_paths_sin_espacios_no_cambian_de_representacion() -> None:
    """Sin espacios la serialización es la identidad: no se agrega ninguna cita.

    Los valores son sintéticos a propósito. Tomarlos de ``tmp_path`` ataba el
    caso a que la ruta temporal de la máquina no tuviera espacios —un usuario
    ``John Doe`` la tiene—, y entonces dejaba de probar lo que su nombre afirma.
    """
    carpeta = r"C:\Rig\home\.sky_claw\vfs_bridge\mo2-c7027ac56c8d4887"
    vector = [
        "--vfs-worker",
        "--manifest",
        rf"{carpeta}\jobs\{JOB_ID_DE_REFERENCIA}.json",
        "--descriptor",
        rf"{carpeta}\mo2-c7027ac56c8d4887.json",
        "--job-id",
        JOB_ID_DE_REFERENCIA,
    ]

    serializado = bridge.encode_mo2_arguments(vector)
    assert serializado == vector, "sin espacios no debe agregarse ninguna cita de transporte"

    linea = _linea_de_comandos_de_mo2(pathlib.Path(r"C:\Rig\app\SkyClawApp.exe"), serializado)
    assert _argv_del_proceso(linea)[1:] == vector


def test_d1_04_la_funcion_semantica_sigue_siendo_pura(tmp_path: pathlib.Path) -> None:
    """``build_worker_arguments`` describe conceptos, no transporte: nunca cita."""
    rig = _preparar_rig(tmp_path)
    request = bridge.validate_launch_request(
        {
            "protocol_version": bridge.PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": JOB_ID_DE_REFERENCIA,
            "profile": "A209RigStandalone",
            "manifest_path": str(rig["manifest"]),
            "overwrite_mod": None,
        },
        jobs_root=rig["jobs"],
    )

    argumentos = bridge.build_worker_arguments(
        fixed_prefix=("--vfs-worker",),
        request=request,
        descriptor_path=rig["descriptor"],
    )

    assert argumentos == _vector_semantico(rig)
    assert not any('"' in argumento for argumento in argumentos), (
        f"los argumentos semánticos no llevan comillas: {argumentos!r}"
    )


# ---------------------------------------------------------------------------
# D1-05 — caracteres especiales: Unicode, comillas y barras invertidas finales
# ---------------------------------------------------------------------------

CASOS_ESPECIALES = [
    "sin-espacios",
    "con un espacio",
    "con  dos  espacios",
    'comillas"internas',
    'comilla al final"',
    '"comilla al inicio',
    "barra invertida final\\",
    "dos barras finales\\\\",
    'barra y comilla\\"',
    "unicode: ñandú, 日本語, Ω",
    "tab\tdentro",
    "",
    "--flag-disfrazado",
    'C:\\ruta con espacio\\y "comillas"\\',
]


@pytest.mark.parametrize("valor", CASOS_ESPECIALES, ids=repr)
def test_d1_05_el_valor_sobrevive_la_ida_y_vuelta_del_transporte(valor: str) -> None:
    exe = pathlib.Path("C:\\Rig A\\app\\SkyClawApp.exe")

    fragmentos = bridge.encode_mo2_arguments([valor])
    argv = _argv_del_proceso(_linea_de_comandos_de_mo2(exe, fragmentos))

    assert argv == [str(exe), valor]


def test_d1_05_la_barra_final_no_escapa_la_comilla_de_cierre() -> None:
    """El caso clásico del CRT: ``C:\\dir con espacio\\`` debe cerrar su propia cita."""
    exe = pathlib.Path("C:\\Rig A\\app\\SkyClawApp.exe")
    valor = "C:\\Rig A\\home con espacio\\"

    fragmentos = bridge.encode_mo2_arguments([valor])

    assert fragmentos == ['"C:\\Rig A\\home con espacio\\\\"'], (
        f"las barras finales deben duplicarse antes de la comilla de cierre: {fragmentos!r}"
    )
    assert _argv_del_proceso(_linea_de_comandos_de_mo2(exe, fragmentos)) == [str(exe), valor]


# ---------------------------------------------------------------------------
# D1-06 — límites de seguridad: el valor no puede alterar la estructura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor",
    [
        "x --descriptor evil.json",
        "x --manifest evil.json",
        "x --vfs-worker --probe-child",
        'x" --descriptor evil.json "',
        "x\ty --job-id z",
        "x && calc.exe",
        "x | calc.exe",
        "x; calc.exe",
    ],
    ids=repr,
)
def test_d1_06_un_valor_no_puede_agregar_argumentos(valor: str) -> None:
    """Invariante: la cantidad de elementos del argv es la del vector autorizado."""
    exe = pathlib.Path("C:\\Rig A\\app\\SkyClawApp.exe")
    vector = ["--vfs-worker", "--manifest", "C:\\Rig A\\m.json", "--descriptor", "C:\\Rig A\\d.json", "--job-id", valor]

    fragmentos = bridge.encode_mo2_arguments(vector)
    argv = _argv_del_proceso(_linea_de_comandos_de_mo2(exe, fragmentos))

    assert argv == [str(exe), *vector], f"el valor {valor!r} alteró la estructura del comando: {argv!r}"


def test_d1_06_un_job_id_con_flags_no_secuestra_el_descriptor(tmp_path: pathlib.Path) -> None:
    """El impacto real: sin citar, ``--descriptor`` del atacante gana el parseo."""
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    job_id_hostil = "x --descriptor evil.json"
    _lanzar(rig, organizer, _JobObject(), [], job_id=job_id_hostil)

    argv = _argv_que_recibe_el_worker(organizer, rig["worker"])
    namespace = _parse_worker_args(_argv_para_el_parser(argv))

    assert namespace.descriptor == rig["descriptor"].resolve(), (
        f"un job_id con flags reemplazó el descriptor autorizado: {namespace.descriptor!r}"
    )
    assert namespace.job_id == job_id_hostil


# ---------------------------------------------------------------------------
# D1-07 — exactamente un lanzamiento y lifecycle intacto
# ---------------------------------------------------------------------------


def test_d1_07_el_controlador_lanza_una_sola_vez_y_conserva_el_lifecycle(tmp_path: pathlib.Path) -> None:
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    job_object = _JobObject()
    eventos: list[dict[str, object]] = []
    controlador = _lanzar(rig, organizer, job_object, eventos)

    assert len(organizer.calls) == 1, f"startApplication se llamó {len(organizer.calls)} veces"
    ejecutable, fragmentos, cwd, perfil, overwrite, ignore_overwrite = organizer.calls[0]
    assert ejecutable == str(rig["worker"].resolve())
    assert isinstance(fragmentos, list)
    assert cwd == str(rig["worker"].resolve().parent)
    assert perfil == "A209RigStandalone"
    assert overwrite == ""
    assert ignore_overwrite is False
    assert job_object.assigned == [123]

    assert eventos[0] == {
        "protocol_version": bridge.PROTOCOL_VERSION,
        "type": "launch_ack",
        "job_id": JOB_ID_DE_REFERENCIA,
    }

    organizer.release.set()
    controlador.wait_for_monitors(timeout=2)

    assert eventos[-1]["event"] == "worker_exit"
    assert eventos[-1]["job_id"] == JOB_ID_DE_REFERENCIA
    assert eventos[-1]["exit_code"] == 0
    assert job_object.closed is True
    assert job_object.terminated is False
    assert len(organizer.calls) == 1


def test_d1_07_el_overwrite_del_mod_se_conserva(tmp_path: pathlib.Path) -> None:
    rig = _preparar_rig(tmp_path)
    organizer = _Organizer()
    _lanzar(rig, organizer, _JobObject(), [], overwrite_mod="SkyClaw Output")

    assert len(organizer.calls) == 1
    assert organizer.calls[0][4] == "SkyClaw Output"


# ---------------------------------------------------------------------------
# Simulación de integración del transporte (sin MO2, sin USVFS)
# ---------------------------------------------------------------------------

_GUION_REGISTRADOR = """\
import json
import os
import pathlib
import sys

pathlib.Path(os.environ["SKYCLAW_ARGV_SALIDA"]).write_text(
    json.dumps(sys.argv[1:], ensure_ascii=False), encoding="utf-8"
)
"""


def _correr_como_mo2(tmp_path: pathlib.Path, vector: Sequence[str], *, serializar: bool) -> list[str]:
    """Lanza un proceso real con la línea de comandos que armaría MO2.

    ``subprocess`` con una cadena en Windows pasa la cadena tal cual a
    ``CreateProcess``, igual que ``spawn::spawn``. No se usa shell.

    El guion es andamiaje: no existe en el escenario real, así que tiene que
    sobrevivir siempre para que el hijo pueda registrar su argv. Por eso vive en
    una carpeta con espacios y se cita **siempre**. La cita se hace con
    ``subprocess.list2cmdline`` y no con la función bajo prueba: el arnés no
    puede depender de la pieza que está midiendo. Así el único elemento que
    puede fragmentarse es el vector del worker, que es lo que el test mide.

    Dejarlo sin citar ataba el test a que ``tmp_path`` no tuviera espacios —es
    decir, al nombre de usuario de la máquina—: con un ``pytest-of-John Doe`` el
    guion se partía, el hijo moría y el test fallaba por la razón equivocada.
    """
    carpeta_del_guion = tmp_path / "arnes con espacios"
    carpeta_del_guion.mkdir()
    guion = carpeta_del_guion / "registrar_argv.py"
    guion.write_text(_GUION_REGISTRADOR, encoding="utf-8")
    salida = tmp_path / "argv_observado.json"

    cuerpo: Sequence[str] = bridge.encode_mo2_arguments(vector) if serializar else vector
    fragmentos: Sequence[str] = [subprocess.list2cmdline([str(guion)]), *cuerpo]

    linea = _linea_de_comandos_de_mo2(pathlib.Path(sys.executable), fragmentos)
    completado = subprocess.run(
        linea,
        shell=False,
        cwd=str(tmp_path),
        env={**os.environ, "SKYCLAW_ARGV_SALIDA": str(salida)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert completado.returncode == 0, f"el proceso hijo falló: {completado.stderr}"
    observado: list[str] = json.loads(salida.read_text(encoding="utf-8"))
    return observado  # el guion registra sys.argv[1:], sin su propio path


@pytest.mark.skipif(sys.platform != "win32", reason="la línea de comandos CRT sólo aplica en Windows")
def test_d1_simulacion_el_proceso_real_recibe_los_argumentos_enteros(tmp_path: pathlib.Path) -> None:
    """Integración del transporte: proceso real, sin MO2 ni USVFS."""
    rig = _preparar_rig(tmp_path)
    vector = _vector_semantico(rig)

    observado = _correr_como_mo2(tmp_path, vector, serializar=True)

    assert observado == vector, f"el proceso real recibió {observado!r} en vez de {vector!r}"


@pytest.mark.skipif(sys.platform != "win32", reason="la línea de comandos CRT sólo aplica en Windows")
def test_d1_el_modelo_de_transporte_detecta_la_fragmentacion_sin_serializar(tmp_path: pathlib.Path) -> None:
    """Control negativo: el modelo discrimina de verdad.

    Si se pasan los argumentos semánticos sin serializar, el mismo proceso real
    los parte en los espacios. Esto reproduce E3-R2B y demuestra que el test de
    arriba no pasa por casualidad.
    """
    rig = _preparar_rig(tmp_path)
    vector = _vector_semantico(rig)

    observado = _correr_como_mo2(tmp_path, vector, serializar=False)

    assert observado != vector, "el modelo de transporte no reproduce el defecto: no sirve como gate"
    assert str(rig["manifest"].resolve()) not in observado, (
        f"se esperaba el manifest fragmentado en los espacios; se observó {observado!r}"
    )
    assert any("A209" in token for token in observado) and any("F1" in token for token in observado), (
        f"se esperaban los dos fragmentos del rig; se observó {observado!r}"
    )
