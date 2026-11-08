"""Anclas de la identidad del Job: enumeran la familia, no muestrean un caso.

La clase de defecto dominante del repo es arreglar un camino y dejar intacto a su gemelo. Acá la identidad del Job
tiene hermanos concretos: dos superficies que piden un Job (``submit`` y ``open_session``), dos caminos que sueltan
el estado de un job, tres módulos que leen o escriben el nombre, y una única fuente legítima de una prueba. Cada
ancla fija por IGUALDAD LITERAL el conjunto detectado por AST: añadir un miembro sin revisarlo rompe el test.
"""

from __future__ import annotations

import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"
MO2 = PAQUETE / "local" / "mo2"
BRIDGE = MO2 / "plugin_bundle" / "skyclaw_bridge" / "runtime.py"
BROKER = MO2 / "vfs_broker.py"
WORKER = MO2 / "vfs_worker.py"
IDENTIDAD = MO2 / "vfs_job_identity.py"


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _relativa(ruta: pathlib.Path) -> str:
    return ruta.relative_to(RAIZ).as_posix()


def _produccion() -> list[pathlib.Path]:
    return sorted(PAQUETE.rglob("*.py"))


def test_solo_cuatro_modulos_de_produccion_tocan_el_campo_job_object() -> None:
    """Broker (lo pide y lo compara), bridge (lo crea y lo confirma), worker (lo verifica y lo declara) y el
    arranque liviano (lo lee para verificar el Job ANTES de importar el worker, H-02)."""
    # Arrange / Act: toda constante de texto que ES el nombre del campo o del flag, en cualquier posición.
    modulos: set[str] = set()
    for ruta in _produccion():
        for nodo in ast.walk(_arbol(ruta)):
            if isinstance(nodo, ast.Constant) and nodo.value in ("job_object", "--job-object"):
                modulos.add(_relativa(ruta))
    # Assert: un cuarto lector/escritor del nombre (otro lanzador, otro broker) debe revisarse contra este mecanismo.
    assert modulos == {
        "sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/runtime.py",
        "sky_claw/local/mo2/vfs_arranque.py",
        "sky_claw/local/mo2/vfs_broker.py",
        "sky_claw/local/mo2/vfs_worker.py",
    }


def test_los_llamadores_del_verificador_del_aborto_y_de_la_limpieza_son_exactamente_los_dos_arranques() -> None:
    """Dos superficies, un recurso (H-02): el arranque liviano y ``worker_main`` verifican Y abortan igual.

    Si un tercer llamador verificara por su cuenta, o abortara sin pasar por ``abortar_por_identidad``, volvería la
    clase de defecto «arreglar un hermano y no al otro»: el aborto (registro + limpieza + código 72) es UNO.
    """

    def llamadores(nombre: str) -> set[tuple[str, str]]:
        encontrados: set[tuple[str, str]] = set()
        for ruta in _produccion():
            for funcion in ast.walk(_arbol(ruta)):
                if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == nombre
                    for n in ast.walk(funcion)
                ):
                    encontrados.add((_relativa(ruta), funcion.name))
        return encontrados

    arranques = {
        ("sky_claw/local/mo2/vfs_arranque.py", "arrancar_worker"),
        ("sky_claw/local/mo2/vfs_worker.py", "worker_main"),
    }
    assert llamadores("verificar_identidad_del_job") == arranques
    assert llamadores("abortar_por_identidad") == arranques
    assert llamadores("terminar_descendientes_vivos") == {
        ("sky_claw/local/mo2/vfs_arranque.py", "abortar_por_identidad")
    }


def test_todo_camino_del_broker_que_suelta_un_job_suelta_tambien_su_identidad_y_su_estado_de_fence() -> None:
    """``_limpiar_registro`` y ``_descartar_apertura_sin_driver`` (y cualquier otro que haga ``_pending.pop``)."""
    # Arrange: funciones que hacen `self._pending.pop(...)`.
    arbol = _arbol(BROKER)
    sueltan: dict[str, set[str]] = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        tocados = {
            f"{n.func.value.attr}.{n.func.attr}"  # type: ignore[attr-defined]
            for n in ast.walk(nodo)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Attribute)
            and isinstance(n.func.value.value, ast.Name)
            and n.func.value.value.id == "self"
        }
        if "_pending.pop" in tocados:
            sueltan[nodo.name] = tocados

    # Assert 1: la familia de caminos que sueltan un job es exactamente esta.
    assert set(sueltan) == {"_limpiar_registro", "_descartar_apertura_sin_driver"}
    # Assert 2: TODOS descartan el nombre del Job y el estado del fence (un camino que olvidara uno filtraría estado).
    for nombre, tocados in sueltan.items():
        assert "_job_objects.pop" in tocados, nombre
        assert "_launch_acked.discard" in tocados, nombre
        assert "_supervised_sent.discard" in tocados, nombre


def test_el_nombre_del_job_nace_en_un_unico_punto_del_broker() -> None:
    """``_registrar_job`` es el único que lo genera: ``submit`` y ``open_session`` pasan por él."""
    # Arrange / Act
    generadores = sorted(
        nodo.name
        for nodo in ast.walk(_arbol(BROKER))
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef)
        for n in ast.walk(nodo)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "generar_nombre_de_job"
    )
    # Assert
    assert generadores == ["_registrar_job"]


def test_una_prueba_de_job_solo_se_construye_en_el_verificador() -> None:
    """Nadie en producción fabrica una ``PruebaDeJob`` a mano: solo ``verificar_identidad_del_job`` la emite."""
    # Arrange / Act
    constructores: dict[str, int] = {}
    for ruta in _produccion():
        cuenta = sum(
            1
            for nodo in ast.walk(_arbol(ruta))
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "PruebaDeJob"
        )
        if cuenta:
            constructores[_relativa(ruta)] = cuenta
    # Assert
    assert constructores == {"sky_claw/local/mo2/vfs_job_identity.py": 1}


def test_todo_run_worker_session_de_produccion_pasa_la_prueba_de_identidad() -> None:
    """La prueba es un argumento obligatorio y cada llamada de producción lo entrega."""
    # Arrange / Act
    llamadas: list[tuple[str, bool]] = []
    for ruta in _produccion():
        for nodo in ast.walk(_arbol(ruta)):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "run_worker_session":
                llamadas.append((_relativa(ruta), any(k.arg == "job_identity" for k in nodo.keywords)))
    # Assert
    assert llamadas == [("sky_claw/local/mo2/vfs_worker.py", True)]


def test_ningun_job_nativo_se_crea_sin_nombre() -> None:
    """``Win32JobObject()`` sin nombre sería un Job anónimo: el worker no podría abrirlo y la identidad no existiría."""
    # Arrange / Act
    sin_nombre = [
        _relativa(ruta)
        for ruta in _produccion()
        for nodo in ast.walk(_arbol(ruta))
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id == "Win32JobObject"
        and not nodo.args
        and not nodo.keywords
    ]
    # Assert: el controlador lo recibe como FÁBRICA (`job_factory or Win32JobObject`) y la llama con el nombre.
    assert sin_nombre == []


def test_la_pertenencia_nunca_se_pregunta_contra_un_job_nulo_en_produccion() -> None:
    """``IsProcessInJob(p, NULL)`` responde TRUE para CUALQUIER job: no prueba identidad (#856 §4.1)."""
    # Arrange / Act: llamadas a IsProcessInJob cuyo segundo argumento es el literal None.
    contra_null = [
        f"{_relativa(ruta)}:{nodo.lineno}"
        for ruta in _produccion()
        for nodo in ast.walk(_arbol(ruta))
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "IsProcessInJob"
        and len(nodo.args) >= 2
        and isinstance(nodo.args[1], ast.Constant)
        and nodo.args[1].value is None
    ]
    # Assert
    assert contra_null == []


def test_el_unico_hello_de_worker_declara_la_identidad_del_job() -> None:
    """Hermano de ``test_el_unico_hello_de_worker_declara_la_capacidad_de_fence``: mismo literal, mismo contrato."""
    # Arrange / Act
    literales: list[tuple[str, set[str]]] = []
    for ruta in _produccion():
        for nodo in ast.walk(_arbol(ruta)):
            if not isinstance(nodo, ast.Dict):
                continue
            pares = {
                clave.value: valor.value
                for clave, valor in zip(nodo.keys, nodo.values, strict=True)
                if isinstance(clave, ast.Constant) and isinstance(valor, ast.Constant)
            }
            if pares.get("role") == "worker":
                claves = {k.value for k in nodo.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                literales.append((_relativa(ruta), claves))
    # Assert
    assert [archivo for archivo, _ in literales] == ["sky_claw/local/mo2/vfs_worker.py"]
    assert {"supervision_fence", "job_object"} <= literales[0][1]


def test_el_selector_del_nieto_de_los_e2e_cubre_todas_las_formas_en_que_el_worker_lo_lanza() -> None:
    """Un selector con una sola forma volvió VACUA la aserción «no se creó el nieto» con el ejecutable congelado."""
    from tests.test_vfs_job_identity_e2e_windows import BANDERAS_DEL_NIETO

    # Arrange / Act: todo literal «--...probe-child» que el worker usa para lanzar su nieto.
    usadas = {
        nodo.value
        for nodo in ast.walk(_arbol(WORKER))
        if isinstance(nodo, ast.Constant)
        and isinstance(nodo.value, str)
        and nodo.value.startswith("--")
        and "probe-child" in nodo.value
    }
    # Assert: la familia está congelada (una forma nueva rompe esto hasta revisarla) y el selector la cubre entera.
    assert usadas == {"--probe-child", "--vfs-probe-child"}
    assert usadas <= set(BANDERAS_DEL_NIETO)
    assert "--probe-child" not in "--vfs-probe-child", "premisa: no son subcadena una de la otra"


def test_la_api_por_defecto_solo_se_construye_en_un_punto_que_falla_cerrado() -> None:
    """Dos consumidores la construían a mano fuera de todo ``try``; un tercero repetiría el defecto."""
    # Arrange / Act: funciones del módulo de identidad que llaman a `_api_por_defecto`.
    constructores = sorted(
        nodo.name
        for nodo in ast.walk(_arbol(IDENTIDAD))
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef)
        for n in ast.walk(nodo)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_api_por_defecto"
    )
    # Assert: únicamente el helper que envuelve cualquier falla en `JobIdentityError`.
    assert constructores == ["_abrir_motor"]


def test_cada_metodo_de_la_api_de_jobs_tiene_su_modo_de_falla_probado() -> None:
    """La familia completa de llamadas al SO: el error crudo de CUALQUIERA tiene que salir como ``JobIdentityError``.

    Se arregló la construcción de la API en los dos consumidores y la limpieza quedó con llamadas sin envolver: arreglar
    una mitad de la familia es el defecto dominante. Un método nuevo rompe esto hasta entrar en las matrices de
    ``verificar_identidad_del_job`` y de ``terminar_descendientes_vivos`` (``tests/test_vfs_job_identity.py``).
    """
    # Arrange / Act
    metodos = {
        miembro.name
        for clase in ast.walk(_arbol(IDENTIDAD))
        if isinstance(clase, ast.ClassDef) and clase.name == "ApiDeJobs"
        for miembro in clase.body
        if isinstance(miembro, ast.FunctionDef)
    }
    # Assert
    assert metodos == {
        "abrir_job",  # verificar: matriz «cualquier_falla_al_consultar»
        "proceso_actual_en_job",  # verificar: ídem
        "banderas_de_limite",  # verificar: ídem
        "contabilidad",  # verificar: ídem
        "descendientes_vivos",  # verificar: ídem + limpieza: «la_limpieza_de_descendientes_tambien_falla_cerrado»
        "proceso_en_job",  # verificar: «un_error_al_preguntar_por_un_descendiente_falla_cerrado»
        "terminar_proceso",  # limpieza: «la_limpieza_de_descendientes_tambien_falla_cerrado»
        "cerrar",  # verificar: matriz «cualquier_falla_al_consultar» + «la_verificacion_falla_y_el_cierre_tambien»
    }


def test_el_modulo_de_identidad_abre_el_job_solo_con_permiso_de_consulta() -> None:
    """Mínimo privilegio: el worker no puede terminar el Job, cambiarle límites ni asignarle procesos."""
    # Arrange / Act
    fuente = IDENTIDAD.read_text(encoding="utf-8")
    # Assert: JOB_OBJECT_QUERY (0x0004) y ningún otro derecho de Job (ASSIGN 0x1, SET_ATTRIBUTES 0x10, TERMINATE 0x8).
    assert "_JOB_OBJECT_QUERY = 0x0004" in fuente
    assert fuente.count("OpenJobObjectW(") == 1
    assert "OpenJobObjectW(_JOB_OBJECT_QUERY," in fuente
