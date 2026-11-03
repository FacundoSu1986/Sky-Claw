"""El instrumento de censo se valida ANTES de usarlo como evidencia.

Un laboratorio que mide «qué nace antes de la verificación» solo vale si, sobre una verdad conocida, cuenta lo
que debe: lo creado después de la asignación entra al Job, lo creado antes queda fuera y sobrevive a la
terminación, y nada de eso depende de que una muestra lo haya visto de casualidad.

* La parte pura (``clasificar``, ``resumir``, el audit hook) corre en cualquier plataforma.
* La parte Windows (Job + IOCP reales, procesos reales) está marcada ``skipif`` fuera de Windows: un skip es
  «no verificado», nunca un PASS (el workflow del sandbox corre con ``-rA``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from scripts import windows_onefile_census as censo
from scripts import windows_spawn_origin as origen

ES_WINDOWS = sys.platform == "win32"
solo_windows = pytest.mark.skipif(not ES_WINDOWS, reason="Job Objects, IOCP y WMI solo existen en Windows")

RAIZ = 100


def _corrida(**campos: object) -> dict[str, object]:
    base: dict[str, object] = {
        "retardo_ms": 0.0,
        "raiz_pid": RAIZ,
        "raiz_ya_termino": False,
        "iocp": [],
        "muestreo": [],
        "total_procesos_job": None,
        "wmi": {"disponible": False, "motivo": None, "eventos": []},
        "supervivientes": [],
    }
    base.update(campos)
    return base


def _nuevo(pid: int, imagen: str | None = None) -> dict[str, object]:
    return {"t_ms": 1.0, "mensaje": "NEW_PROCESS", "pid": pid, "imagen": imagen, "cmdline": None}


# ---------------------------------------------------------------------------
# Parte pura
# ---------------------------------------------------------------------------


def test_la_clausura_incluye_nietos_y_excluye_a_los_ajenos() -> None:
    # Arrange: 100 → 101 → 102 ; 200 es de otro árbol
    aristas = [(101, 100), (102, 101), (200, 1)]
    # Act
    arbol = censo._descendientes(RAIZ, aristas)
    # Assert
    assert arbol == {100, 101, 102}


def test_un_hijo_creado_despues_de_asignar_queda_dentro_y_no_hay_fuera() -> None:
    # Arrange: el kernel reporta a la raíz y a su hijo; el muestreo solo alcanzó a ver a la raíz.
    corrida = _corrida(
        iocp=[_nuevo(RAIZ), _nuevo(101)],
        muestreo=[{"pid": RAIZ, "ppid": 1, "en_job": True}],
        total_procesos_job=2,
        wmi={"disponible": True, "motivo": None, "eventos": [{"pid": 101, "ppid": RAIZ, "nombre": "x.exe"}]},
    )
    # Act
    clasificacion = censo.clasificar(corrida)
    # Assert
    assert clasificacion["dentro"] == [RAIZ, 101]
    assert clasificacion["fuera"] == []
    assert clasificacion["efimeros_dentro"] == [101]  # lo que el muestreo de 10 ms pierde y el IOCP no
    assert clasificacion["total_job_coincide_con_iocp"] is True


def test_un_hijo_creado_antes_de_asignar_se_clasifica_fuera_con_la_traza_de_wmi() -> None:
    # Arrange: el hijo 101 nació antes del assign: WMI lo vio, el Job nunca lo reportó.
    corrida = _corrida(
        iocp=[_nuevo(RAIZ)],
        wmi={"disponible": True, "motivo": None, "eventos": [{"pid": 101, "ppid": RAIZ, "nombre": "py.exe"}]},
        supervivientes=[{"pid": 101, "imagen": "py.exe", "cmdline": "py", "dentro_del_job": None}],
    )
    # Act
    clasificacion = censo.clasificar(corrida)
    # Assert
    assert clasificacion["fuera"] == [101]
    assert clasificacion["supervivientes"] == [101]
    assert clasificacion["wmi_disponible"] is True


def test_sin_wmi_un_proceso_efimero_fuera_del_job_es_invisible_y_el_resultado_lo_declara() -> None:
    # Arrange: el proceso 101 nació fuera del Job y murió antes de que el muestreo lo viera.
    corrida = _corrida(iocp=[_nuevo(RAIZ)], muestreo=[{"pid": RAIZ, "ppid": 1, "en_job": True}])
    # Act
    clasificacion = censo.clasificar(corrida)
    # Assert: no se puede afirmar «cero fuera»: lo que se informa es que WMI NO estaba disponible.
    assert clasificacion["fuera"] == []
    assert clasificacion["wmi_disponible"] is False


def test_la_contabilidad_del_job_discrepante_se_marca() -> None:
    # Arrange: el kernel cuenta 3 procesos pero el IOCP solo entregó 2 mensajes (se perdió uno).
    corrida = _corrida(iocp=[_nuevo(RAIZ), _nuevo(101)], total_procesos_job=3)
    # Act / Assert
    assert censo.clasificar(corrida)["total_job_coincide_con_iocp"] is False


def test_el_resumen_agrupa_por_retardo_y_cuenta_las_imagenes(tmp_path: Path) -> None:
    # Arrange
    corridas = [
        _corrida(
            retardo_ms=0.0,
            iocp=[_nuevo(RAIZ, r"C:\x\App.exe"), _nuevo(101, r"C:\Windows\System32\cmd.exe")],
            total_procesos_job=2,
            wmi={"disponible": True, "motivo": None, "eventos": [{"pid": 101, "ppid": RAIZ, "nombre": "cmd.exe"}]},
        ),
        _corrida(
            retardo_ms=2000.0,
            iocp=[_nuevo(RAIZ, r"C:\x\App.exe")],
            wmi={"disponible": True, "motivo": None, "eventos": [{"pid": 102, "ppid": RAIZ, "nombre": "App.exe"}]},
            supervivientes=[{"pid": 102}],
        ),
    ]
    ruta = tmp_path / "censo.json"
    ruta.write_text(json.dumps(corridas), encoding="utf-8")
    # Act
    texto = censo.resumir(ruta)
    # Assert
    assert "censo.json: 2 corridas" in texto
    assert "cmd.exe" in texto
    filas = {linea.split("|")[0].strip(): linea for linea in texto.splitlines() if linea.strip()[:1].isdigit()}
    assert set(filas) == {"0", "2000"}
    assert filas["2000"].split("|")[4].strip() == "1-1"  # un proceso fuera del Job en la fila tardía
    assert filas["2000"].split("|")[7].strip() == "1"  # y uno sobrevivió a la terminación


def test_el_audit_hook_atribuye_el_proceso_al_import_que_lo_creo(tmp_path: Path) -> None:
    # Arrange: un módulo que lanza un proceso inocuo al importarse (como lo hace el arranque real).
    (tmp_path / "modulo_que_lanza.py").write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys
            subprocess.run([{sys.executable!r}, "-c", "pass"], check=True)
            """
        ),
        encoding="utf-8",
    )
    salida = tmp_path / "origen.json"
    # Act: en un subproceso, porque un audit hook no se puede desinstalar del intérprete de pytest.
    resultado = subprocess.run(  # noqa: S603 - argv fijo del test
        [sys.executable, str(Path(origen.__file__)), "--fase", "modulo_que_lanza", "--json", str(salida)],
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path), "SYSTEMROOT": "C:\\Windows"} if ES_WINDOWS else {"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Assert
    assert resultado.returncode == 0, resultado.stderr
    registro = json.loads(salida.read_text(encoding="utf-8"))
    assert [evento["fase"] for evento in registro] == ["import modulo_que_lanza"]
    assert any("modulo_que_lanza.py" in marco for marco in registro[0]["pila"])


def test_el_resumen_del_origen_nombra_la_fase_y_el_codigo_responsable(tmp_path: Path) -> None:
    # Arrange
    registro = [
        {
            "fase": "import sky_claw.__main__",
            "evento": "subprocess.Popen",
            "argumentos": "('cmd.exe', ['cmd.exe', '/c', 'ver'])",
            "pila": ["a.py:1:<module>", "C:/x/site-packages/y.py:2:f", "sky_claw/z.py:30:g"],
        }
    ]
    ruta = tmp_path / "origen.json"
    ruta.write_text(json.dumps(registro), encoding="utf-8")
    # Act
    resultado = subprocess.run(  # noqa: S603 - argv fijo del test
        [sys.executable, str(Path(origen.__file__)), "--resumir", str(ruta)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # Assert
    assert resultado.returncode == 0, resultado.stderr
    assert "import sky_claw.__main__" in resultado.stdout
    assert "sky_claw/z.py:30:g" in resultado.stdout  # el último marco que NO es de site-packages ni de importlib


class _RelojFalso:
    """Reloj controlado: ``sleep`` lo adelanta y nada espera de verdad (el estado de partida no depende del reloj real)."""

    def __init__(self) -> None:
        self.ahora = 1000.0

    def perf_counter(self) -> float:
        return self.ahora

    def time(self) -> float:
        return 1_700_000_000.0 + self.ahora

    def sleep(self, segundos: float) -> None:
        self.ahora += segundos


class _ProcesoFalso:
    pid = 4242

    def poll(self) -> None:
        return None  # sigue vivo: solo el límite de la ventana corta la observación

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _censar_con_traza_que_tarda(
    monkeypatch: pytest.MonkeyPatch, *, autoprueba_s: float, ventana_s: float, paso_s: float
) -> tuple[dict[str, object], int]:
    """Corre ``medir_censo`` sin Windows: la traza de WMI «tarda» ``autoprueba_s`` en montarse (como el canario sin eventos)."""
    reloj = _RelojFalso()
    muestreos: list[int] = []

    class _TrazaLentaFalsa:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            reloj.ahora += autoprueba_s  # la autoprueba espera su plazo entero cuando no llega ningún evento
            self.disponible = False
            self.motivo = "la traza se suscribió pero NO entregó el evento del proceso canario"
            self.eventos: list[dict[str, object]] = []

        def fijar_origen(self, _t0: float) -> None:  # lo usa el arreglo; el contrato lo fija el test, no la forma
            return None

        def detener(self) -> None:
            return None

    class _PuertoFalso:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.mensajes: list[dict[str, object]] = []

        def cerrar(self) -> None:
            return None

    class _PsutilFalso:
        Error = psutil.Error

        class Process:
            def __init__(self, pid: int) -> None:
                muestreos.append(pid)  # una construcción por iteración del bucle de observación
                self.pid = pid

            def children(self, recursive: bool = False) -> list[object]:
                return []

    def _no_se_puede_abrir(_pid: int) -> int:
        raise censo.sonda.JobObjectError("falso: nada que abrir")

    monkeypatch.setattr(censo, "time", reloj)
    monkeypatch.setattr(censo, "psutil", _PsutilFalso)
    monkeypatch.setattr(censo, "_TrazaWmi", _TrazaLentaFalsa)
    monkeypatch.setattr(censo, "_Puerto", _PuertoFalso)
    monkeypatch.setattr(censo, "_abrir_para_censo", _no_se_puede_abrir)
    monkeypatch.setattr(censo, "_total_y_activos", lambda _job: (None, None))
    monkeypatch.setattr(censo, "_supervivientes", lambda *_a, **_k: [])
    monkeypatch.setattr(censo, "_pids_del_arbol", lambda *_a, **_k: set())
    monkeypatch.setattr(censo, "_matar_vivos", lambda *_a, **_k: None)
    sonda = censo.sonda
    monkeypatch.setattr(sonda, "_exigir_windows", lambda: None)
    monkeypatch.setattr(sonda, "crear_job", lambda **_k: 11)
    monkeypatch.setattr(sonda, "lanzar_como_mo2", lambda _argv: _ProcesoFalso())
    monkeypatch.setattr(sonda, "abrir_proceso", lambda _pid: 7)
    monkeypatch.setattr(sonda, "asignar_a_job", lambda _job, _proceso: None)
    monkeypatch.setattr(sonda, "terminar_job", lambda _job, **_k: None)
    monkeypatch.setattr(sonda, "cerrar_handle", lambda _handle: None)
    monkeypatch.setattr(sonda, "matar_arbol", lambda _pid: None)
    corrida = censo.medir_censo(["falso.exe"], retardo_s=0.0, ventana_s=ventana_s, paso_s=paso_s, con_wmi=True)
    return corrida, len(muestreos)


@pytest.mark.parametrize("autoprueba_s", [0.0, 8.5], ids=["control-sin-espera", "wmi-sin-eventos-gasta-8.5s"])
def test_la_ventana_de_observacion_cuenta_desde_el_lanzamiento_y_no_desde_antes_de_montar_la_traza(
    monkeypatch: pytest.MonkeyPatch, autoprueba_s: float
) -> None:
    """Hallazgo de la revisión: con WMI suscripto pero mudo, la autoprueba gastaba la ventana y no se muestreaba nada.

    Con ``ventana_s=6`` y una autoprueba de 8,5 s el bucle de observación no corría ni una vez, así que los
    escapados faltaban del muestreo y de los «supervivientes» justo cuando WMI no podía verlos por su cuenta.
    """
    # Arrange / Act: ventana 6 s, paso 0,5 s ⇒ 12 observaciones si el reloj arranca en el lanzamiento.
    corrida, observaciones = _censar_con_traza_que_tarda(
        monkeypatch, autoprueba_s=autoprueba_s, ventana_s=6.0, paso_s=0.5
    )
    # Assert: la ventana entera se observa, haya tardado o no la traza (el control prueba que los falsos andan).
    assert observaciones == 12
    # Y el instante de asignación es relativo al lanzamiento, no inflado por la instrumentación previa.
    assert corrida["asignado_ms"] == pytest.approx(0.0, abs=1.0)


# ---------------------------------------------------------------------------
# Parte Windows: el instrumento contra una verdad conocida
# ---------------------------------------------------------------------------

#: La raíz espera ``ESPERA`` s y recién entonces crea un nieto que duerme; así el retardo de asignación
#: decide, sin carreras finas, si el nieto nace antes o después de entrar la raíz al Job.
_RAIZ_CON_NIETO = (
    "import subprocess, sys, time; time.sleep(1.5); "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); time.sleep(40)"
)


def _pids_vivos(supervivientes: list[dict[str, int]]) -> list[int]:
    return [s["pid"] for s in supervivientes]


@solo_windows
def test_un_nieto_creado_despues_de_asignar_entra_al_job_y_muere_con_el_job() -> None:
    # Arrange / Act: asignación inmediata → el nieto (a los 1.5 s) nace dentro del Job.
    corrida = censo.medir_censo([sys.executable, "-c", _RAIZ_CON_NIETO], retardo_s=0.0, ventana_s=4.0, con_wmi=False)
    clasificacion = censo.clasificar(corrida)
    # Assert
    assert len(clasificacion["dentro"]) == 2, corrida
    assert clasificacion["total_job"] == 2
    assert clasificacion["total_job_coincide_con_iocp"] is True
    assert clasificacion["fuera"] == []
    assert clasificacion["supervivientes"] == [], "TerminateJobObject debía matar a toda la membresía"


@solo_windows
def test_un_nieto_creado_antes_de_asignar_queda_fuera_y_sobrevive_a_la_terminacion() -> None:
    # Arrange / Act: asignación a los 3 s → el nieto nació a los ~1.5 s, antes de que la raíz entrara.
    corrida = censo.medir_censo([sys.executable, "-c", _RAIZ_CON_NIETO], retardo_s=3.0, ventana_s=6.0, con_wmi=False)
    clasificacion = censo.clasificar(corrida)
    # Assert: la raíz entró; el nieto NO se adopta y sobrevive (esto es lo que P2 tiene que detectar).
    assert len(clasificacion["dentro"]) == 1, corrida
    assert len(clasificacion["fuera"]) == 1
    assert clasificacion["fuera"] == clasificacion["supervivientes"]
    # Higiene del laboratorio: el propio censo mata lo que escapó; no quedan procesos huérfanos.
    time.sleep(1.0)
    assert not any(psutil.pid_exists(pid) for pid in _pids_vivos(corrida["supervivientes"]))


@solo_windows
def test_cerrar_el_handle_del_job_tambien_mata_a_los_miembros() -> None:
    # Arrange / Act: lo que ocurre si el bridge muere (KILL_ON_JOB_CLOSE), no solo el cancel explícito.
    corrida = censo.medir_censo(
        [sys.executable, "-c", _RAIZ_CON_NIETO], retardo_s=0.0, ventana_s=4.0, modo_fin="close", con_wmi=False
    )
    # Assert
    assert censo.clasificar(corrida)["supervivientes"] == []


@solo_windows
def test_la_traza_de_wmi_ve_al_nieto_creado_fuera_del_job_aunque_el_muestreo_no_lo_vea() -> None:
    # Arrange / Act
    corrida = censo.medir_censo([sys.executable, "-c", _RAIZ_CON_NIETO], retardo_s=3.0, ventana_s=6.0, con_wmi=True)
    if not corrida["wmi"]["disponible"]:  # type: ignore[index]
        pytest.skip(f"traza de WMI NO disponible (queda sin verificar): {corrida['wmi']['motivo']}")  # type: ignore[index]
    # Assert: WMI vio el nieto por su cuenta (independiente del muestreo y del IOCP).
    nietos = [e for e in corrida["wmi"]["eventos"] if e["ppid"] == corrida["raiz_pid"]]  # type: ignore[index]
    assert len(nietos) == 1


@solo_windows
def test_terminar_tras_corta_con_el_arbol_vivo_y_mide_al_escapado_aunque_el_payload_termine_solo_despues() -> None:
    # Arrange: el nieto (6 s) nace a los 1.5 s, ANTES de la asignación (2.5 s); sin `terminar_tras_s` el censo
    # esperaría a que todo terminara solo y el escapado habría muerto: «supervivientes = 0» sería un artefacto.
    carga = (
        "import subprocess, sys, time; time.sleep(1.5); "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)']); time.sleep(6)"
    )
    # Act
    corrida = censo.medir_censo(
        [sys.executable, "-c", carga], retardo_s=2.5, ventana_s=12.0, con_wmi=False, terminar_tras_s=0.5
    )
    clasificacion = censo.clasificar(corrida)
    # Assert
    assert len(clasificacion["fuera"]) == 1, corrida
    assert clasificacion["supervivientes"] == clasificacion["fuera"], (
        "el escapado debía seguir vivo tras terminar el Job"
    )
