"""Resumen del laboratorio de Job Objects: una medición nula o inconclusa no cuenta como «dentro» ni «fuera».

El laboratorio mide si el proceso Python efectivo de un ejecutable *onefile* queda dentro del Job
al que se asigna el proceso lanzado. Dos situaciones no son una medición válida y, si se las contara
como filas normales, inflarían (o desinflarían) la conclusión:

* ``raiz_ya_termino``: con una demora larga el proceso lanzado ya había terminado cuando llegó el
  ``AssignProcessToJobObject`` (Windows responde ``ACCESS_DENIED``); no hay árbol que observar.
* ``efectiva_es_la_raiz``: nunca se vio un descendiente con la imagen del ejecutable (el hijo de
  *onefile* ya había terminado), así que el «Python efectivo» es la propia raíz.

El resumen es Python puro: se ejerce en cualquier plataforma. Las pruebas del comportamiento de
``medir_asignacion`` frente a una raíz terminada viven en ``test_vfs_job_containment_windows.py``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from scripts.windows_job_probe import resumir


def _fila(
    retardo_ms: float,
    *,
    en_job: bool | None,
    procesos: int = 2,
    arbol: bool = True,
    raiz_ya_termino: bool | None = None,
    efectiva_es_la_raiz: bool | None = None,
) -> dict[str, Any]:
    fila: dict[str, Any] = {
        "retardo_de_asignacion_ms": retardo_ms,
        "asignacion_completada_ms": retardo_ms + 3,
        "procesos": [{"pid": n} for n in range(procesos)],
        "imagen_python_efectiva_en_job": en_job,
        "arbol_completo_en_job": arbol,
    }
    if raiz_ya_termino is not None:
        fila["raiz_ya_termino"] = raiz_ya_termino
    if efectiva_es_la_raiz is not None:
        fila["efectiva_es_la_raiz"] = efectiva_es_la_raiz
    return fila


def _lineas_por_retardo(tmp_path: pathlib.Path, filas: list[dict[str, Any]]) -> dict[int, list[str]]:
    ruta = tmp_path / "resultados.json"
    ruta.write_text(json.dumps(filas), encoding="utf-8")
    salida = resumir(ruta).splitlines()
    return {int(linea.split("|")[0]): [c.strip() for c in linea.split("|")] for linea in salida[2:]}


def test_una_raiz_ya_terminada_no_cuenta_como_dentro_ni_como_fuera(tmp_path: pathlib.Path) -> None:
    # Arrange: 2 mediciones válidas (1 dentro, 1 fuera) y 1 nula por raíz ya terminada.
    filas = [
        _fila(1000, en_job=True),
        _fila(1000, en_job=False, arbol=False),
        _fila(1000, en_job=None, procesos=0, arbol=False, raiz_ya_termino=True),
    ]

    # Act
    celdas = _lineas_por_retardo(tmp_path, filas)[1000]

    # Assert: n=3, efectivo 1/2 (sobre las concluyentes), 1 inconclusa.
    assert celdas[1] == "3"
    assert celdas[2] == "1/2"
    assert celdas[-1] == "1"


def test_una_efectiva_que_es_la_propia_raiz_es_inconclusa(tmp_path: pathlib.Path) -> None:
    # Arrange: la raíz sola «en el Job» (el hijo ya había muerto) no prueba nada sobre el hijo.
    filas = [
        _fila(4000, en_job=True, procesos=1, efectiva_es_la_raiz=True),
        _fila(4000, en_job=True, procesos=1, efectiva_es_la_raiz=True),
        _fila(4000, en_job=False, arbol=False),
    ]

    # Act
    celdas = _lineas_por_retardo(tmp_path, filas)[4000]

    # Assert: solo la tercera es concluyente (y quedó fuera); las otras dos NO suman a favor.
    assert celdas[2] == "0/1"
    assert celdas[-1] == "2"


def test_las_filas_de_un_json_viejo_sin_los_campos_nuevos_cuentan_como_concluyentes(tmp_path: pathlib.Path) -> None:
    # Arrange: resultados generados antes de que existieran los campos de inconclusión.
    filas = [_fila(0, en_job=True), _fila(0, en_job=True)]

    # Act
    celdas = _lineas_por_retardo(tmp_path, filas)[0]

    # Assert
    assert celdas[2] == "2/2"
    assert celdas[-1] == "0"


def test_un_retardo_sin_ninguna_medicion_valida_no_rompe_el_resumen(tmp_path: pathlib.Path) -> None:
    # Arrange: todas nulas (p. ej. el exe termina siempre antes del assign).
    filas = [_fila(9000, en_job=None, procesos=0, arbol=False, raiz_ya_termino=True) for _ in range(3)]

    # Act
    celdas = _lineas_por_retardo(tmp_path, filas)[9000]

    # Assert: 0/0 concluyentes, 3 inconclusas, y el rango de procesos no inventa un número.
    assert celdas[2] == "0/0"
    assert celdas[-1] == "3"
