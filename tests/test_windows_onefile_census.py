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
