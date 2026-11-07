"""Auditoría independiente H-02: el worker prueba su Job ANTES de importar nada pesado.

El hallazgo: en el ejecutable congelado, ``--vfs-worker`` importaba ``vfs_worker`` (y con él ``uuid`` →
``platform`` → ``cmd.exe /c ver`` en Windows) ANTES de verificar la pertenencia al Job. Esos subprocesos nacían
sin haber probado contención. La propiedad es: **hasta que la verificación pase, solo se importa el conjunto
liviano** (``vfs_arranque`` + ``vfs_job_identity``), y los DOS puntos de entrada (despacho a nivel de módulo y
``main()``) pasan por la misma función.

Anclas que ENUMERAN, no muestrean: los imports de nivel módulo del conjunto liviano se congelan por igualdad
literal (un import nuevo pesado rompe el test), los sitios de despacho de ``__main__.py`` se detectan por AST
(un tercer despacho sin ``arrancar_worker`` rompe el test) y el orden verificación → logging de ``worker_main``
se comprueba sobre el árbol.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.local.mo2 import vfs_arranque
from sky_claw.local.mo2.vfs_job_identity import JobIdentityError, PruebaDeJob

REPO = pathlib.Path(__file__).resolve().parents[1]
NOMBRE_DE_JOB = "Local\\SkyClawVfs-" + "0123456789abcdef" * 2
OTRO_NOMBRE_DE_JOB = "Local\\SkyClawVfs-" + "fedcba9876543210" * 2
PRUEBA = PruebaDeJob(nombre=NOMBRE_DE_JOB, pid=1, procesos_totales=2, procesos_activos=2)
#: Los módulos pesados que NO pueden cargarse antes de que la verificación del Job pase.
MODULOS_PESADOS = (
    "sky_claw.local.mo2.vfs_worker",
    "sky_claw.logging_config",
    "sky_claw.local.mo2.vfs_contracts",
    "pydantic",
)


def _argv(nombre: str = NOMBRE_DE_JOB, job_id: str = "job-xyz") -> list[str]:
    return [
        "--manifest",
        "m.json",
        "--descriptor",
        "d.json",
        "--job-object",
        nombre,
        "--job-id",
        job_id,
    ]


def _imports_de_nivel_de_modulo(ruta: pathlib.Path) -> frozenset[str]:
    """Nombres de módulo importados a nivel de módulo (no dentro de funciones)."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    nombres: set[str] = set()
    for nodo in arbol.body:
        if isinstance(nodo, ast.Import):
            nombres.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module is not None:
            nombres.add(nodo.module)
    return frozenset(nombres)


# --------------------------------------------------------------------------------------------------------------------
# Comportamiento de ``arrancar_worker`` (en proceso, con los bordes parcheados)
# --------------------------------------------------------------------------------------------------------------------


def test_con_job_object_verifica_antes_de_delegar_en_el_worker() -> None:
    from sky_claw.local.mo2 import vfs_worker

    orden = MagicMock()
    with (
        patch.object(vfs_arranque, "verificar_identidad_del_job", orden.verificar),
        patch.object(vfs_worker, "worker_main", orden.worker_main),
    ):
        orden.verificar.return_value = PRUEBA
        orden.worker_main.return_value = 0
        codigo = vfs_arranque.arrancar_worker("--vfs-worker", _argv())

    assert codigo == 0
    nombres = [llamada[0] for llamada in orden.mock_calls]
    assert nombres == ["verificar", "worker_main"], "la verificación debe ocurrir ANTES de llamar al worker"
    orden.verificar.assert_called_once_with(NOMBRE_DE_JOB)
    orden.worker_main.assert_called_once_with(_argv())


def test_si_la_verificacion_falla_no_se_llama_al_worker_y_se_sale_con_72(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_arranque, "directorio_de_logs", return_value=tmp_path),
        patch.object(vfs_arranque, "verificar_identidad_del_job", side_effect=JobIdentityError("fuera del Job")),
        patch.object(vfs_arranque, "terminar_descendientes_vivos", return_value=(41, 42)) as limpiar,
        patch.object(vfs_worker, "worker_main") as worker,
    ):
        codigo = vfs_arranque.arrancar_worker("--vfs-worker", _argv())

    assert codigo == 72 == vfs_arranque.EXIT_IDENTIDAD_DEL_JOB
    worker.assert_not_called()
    limpiar.assert_called_once_with()


def test_la_ultima_aparicion_de_job_object_manda_igual_que_en_el_parser_del_worker() -> None:
    """El nombre que se verifica es el mismo que el parser pesado (argparse) entregaría: gana la última aparición."""
    from sky_claw.local.mo2 import vfs_worker

    argv = [*_argv(OTRO_NOMBRE_DE_JOB), "--job-object", NOMBRE_DE_JOB]
    with (
        patch.object(vfs_arranque, "verificar_identidad_del_job", return_value=PRUEBA) as verificar,
        patch.object(vfs_worker, "worker_main", return_value=0),
    ):
        vfs_arranque.arrancar_worker("--vfs-worker", argv)

    verificar.assert_called_once_with(NOMBRE_DE_JOB)


def test_acepta_la_forma_con_igual() -> None:
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_arranque, "verificar_identidad_del_job", return_value=PRUEBA) as verificar,
        patch.object(vfs_worker, "worker_main", return_value=0),
    ):
        vfs_arranque.arrancar_worker("--vfs-worker", ["--manifest", "m.json", f"--job-object={NOMBRE_DE_JOB}"])

    verificar.assert_called_once_with(NOMBRE_DE_JOB)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--manifest", "m.json"], id="sin_job_object"),
        pytest.param(["--manifest", "m.json", "--job-object"], id="job_object_sin_valor"),
        pytest.param(["--help"], id="solo_help"),
    ],
)
def test_sin_un_job_object_utilizable_delega_en_el_worker_que_decide_el_error_de_uso(argv: list[str]) -> None:
    """Sin ``--job-object`` no hay Job que verificar acá: ``worker_main`` devuelve 64 (uso) antes de abrir sockets.

    Delegar mantiene UN solo camino para el error de uso. No es un hueco de contención: el worker verifica el Job
    otra vez antes de cualquier socket, nieto o herramienta.
    """
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_arranque, "verificar_identidad_del_job") as verificar,
        patch.object(vfs_worker, "worker_main", return_value=64) as worker,
    ):
        codigo = vfs_arranque.arrancar_worker("--vfs-worker", argv)

    assert codigo == 64
    verificar.assert_not_called()
    worker.assert_called_once_with(argv)


def test_el_nieto_de_sonda_no_se_verifica_y_recibe_el_flag_del_parser() -> None:
    """``--vfs-probe-child`` ya nace DENTRO del Job (hijo del worker verificado) y no recibe ``--job-object``."""
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_arranque, "verificar_identidad_del_job") as verificar,
        patch.object(vfs_worker, "worker_main", return_value=17) as worker,
    ):
        codigo = vfs_arranque.arrancar_worker("--vfs-probe-child", ["canary.txt", "abc"])

    assert codigo == 17
    verificar.assert_not_called()
    worker.assert_called_once_with(["--probe-child", "canary.txt", "abc"])


def test_un_flag_desconocido_no_se_delega_en_silencio() -> None:
    with pytest.raises(ValueError, match="--vfs-otro"):
        vfs_arranque.arrancar_worker("--vfs-otro", [])


# --------------------------------------------------------------------------------------------------------------------
# El registro de la falla (sin ``logging_config``: no puede importarse antes de la prueba)
# --------------------------------------------------------------------------------------------------------------------


def test_la_falla_de_identidad_deja_registro_en_el_log_del_job(tmp_path: pathlib.Path) -> None:
    with (
        patch.object(vfs_arranque, "directorio_de_logs", return_value=tmp_path),
        patch.object(vfs_arranque, "terminar_descendientes_vivos", return_value=(41,)),
    ):
        codigo = vfs_arranque.abortar_por_identidad("job-xyz", JobIdentityError("fuera del Job"))

    assert codigo == 72
    contenido = (tmp_path / "workers" / "vfs-worker-job-xyz.log").read_text(encoding="utf-8")
    assert "vfs_worker_job_identity_failed" in contenido
    assert "fuera del Job" in contenido
    assert "[41]" in contenido  # los descendientes terminados también quedan registrados


def test_si_la_limpieza_tambien_falla_sale_con_72_y_registra_critical(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(vfs_arranque, "directorio_de_logs", return_value=tmp_path),
        patch.object(vfs_arranque, "terminar_descendientes_vivos", side_effect=JobIdentityError("no murió el 13")),
        caplog.at_level(logging.CRITICAL, logger="sky_claw.local.mo2.vfs_arranque"),
    ):
        codigo = vfs_arranque.abortar_por_identidad("job-xyz", JobIdentityError("fuera del Job"))

    assert codigo == 72
    assert any(getattr(r, "event", "") == "vfs_worker_cleanup_failed" for r in caplog.records)
    contenido = (tmp_path / "workers" / "vfs-worker-job-xyz.log").read_text(encoding="utf-8")
    assert "no murió el 13" in contenido


def test_si_el_directorio_de_logs_no_se_puede_escribir_igual_se_sale_con_72(tmp_path: pathlib.Path) -> None:
    """Un disco/permiso roto no puede enmascarar el aborto: el registro es de mejor esfuerzo."""
    archivo_en_lugar_de_directorio = tmp_path / "no_es_un_directorio"
    archivo_en_lugar_de_directorio.write_text("x", encoding="utf-8")
    with (
        patch.object(vfs_arranque, "directorio_de_logs", return_value=archivo_en_lugar_de_directorio),
        patch.object(vfs_arranque, "terminar_descendientes_vivos", return_value=()),
    ):
        codigo = vfs_arranque.abortar_por_identidad("job-xyz", JobIdentityError("fuera del Job"))

    assert codigo == 72


def test_sin_job_id_el_registro_va_solo_a_stderr_y_no_inventa_un_archivo(tmp_path: pathlib.Path) -> None:
    with (
        patch.object(vfs_arranque, "directorio_de_logs", return_value=tmp_path),
        patch.object(vfs_arranque, "terminar_descendientes_vivos", return_value=()),
    ):
        codigo = vfs_arranque.abortar_por_identidad(None, JobIdentityError("fuera del Job"))

    assert codigo == 72
    assert not (tmp_path / "workers").exists()


def test_un_job_id_sin_caracteres_validos_no_produce_un_nombre_vacio() -> None:
    """(El caso de path traversal vive en ``test_vfs_worker_entrypoint``, sobre la misma función.)"""
    assert vfs_arranque.nombre_de_log_del_worker("///") == "vfs-worker-unknown.log"


def test_el_directorio_de_logs_liviano_coincide_con_el_del_resto_de_la_app() -> None:
    """Ancla de drift: el módulo liviano no puede importar ``logging_config``, así que repite la RUTA. Si la de la
    app cambia sin esta, el registro de una falla de identidad caería en un lugar que nadie mira."""
    from sky_claw.logging_config import default_log_dir

    assert vfs_arranque.directorio_de_logs() == default_log_dir()


# --------------------------------------------------------------------------------------------------------------------
# Procesos reales: lo que la verificación NO puede haber cargado
# --------------------------------------------------------------------------------------------------------------------

#: Espía de ``platform.*`` (en Windows la primera llamada lanza ``cmd.exe /c ver``) y de ``subprocess.Popen``.
_ESPIAS = """
import json, platform, subprocess, sys
llamadas = []
for _nombre in ("system", "uname", "win32_ver", "platform", "release", "version", "machine", "processor", "node"):
    _original = getattr(platform, _nombre, None)
    if _original is None:
        continue
    def _gancho(*a, _n=_nombre, _o=_original, **kw):
        llamadas.append(_n)
        return _o(*a, **kw)
    setattr(platform, _nombre, _gancho)
lanzados = []
_Popen = subprocess.Popen
class _Espia(_Popen):
    def __init__(self, *a, **kw):
        lanzados.append(str(a[0] if a else kw.get("args")))
        super().__init__(*a, **kw)
subprocess.Popen = _Espia
"""

_INFORME = """
print(json.dumps({
    "codigo": codigo,
    "platform": llamadas,
    "popen": lanzados,
    "pesados_cargados": [m for m in %r if m in sys.modules],
}))
"""


def _correr_en_proceso_nuevo(accion: str, tmp_path: pathlib.Path) -> dict[str, object]:
    python_directo = getattr(sys, "_base_executable", sys.executable)
    rutas_python = os.pathsep.join([str(REPO), *(p for p in sys.path if p)])
    codigo = "\n".join([_ESPIAS, "codigo = None", accion, _INFORME % (list(MODULOS_PESADOS),)])
    completado = subprocess.run(  # noqa: S603 - subproceso de prueba con código fijo
        [python_directo, "-c", codigo],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": rutas_python, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    informe: dict[str, object] = json.loads(completado.stdout.strip().splitlines()[-1])
    return informe


def test_importar_el_arranque_no_llama_a_platform_ni_carga_lo_pesado(tmp_path: pathlib.Path) -> None:
    informe = _correr_en_proceso_nuevo("import sky_claw.local.mo2.vfs_arranque", tmp_path)

    assert informe["platform"] == [], "el conjunto liviano no debe tocar platform (en Windows: cmd.exe /c ver)"
    assert informe["popen"] == []
    assert informe["pesados_cargados"] == []


def test_una_falla_de_identidad_por_el_despacho_real_no_importa_el_worker_ni_llama_a_platform(
    tmp_path: pathlib.Path,
) -> None:
    """Camino REAL ``sky_claw.__main__ --vfs-worker``: la verificación (que falla cerrado fuera de un Job) corta
    el arranque ANTES de importar ``vfs_worker``. Es la propiedad de H-02 medida en un proceso nuevo."""
    accion = "\n".join(
        [
            f"sys.argv = ['sky_claw', '--vfs-worker', *{_argv()!r}]",
            "try:",
            "    import sky_claw.__main__",
            "except SystemExit as salida:",
            "    codigo = salida.code",
        ]
    )

    informe = _correr_en_proceso_nuevo(accion, tmp_path)

    assert informe["codigo"] == 72
    assert informe["platform"] == []
    assert informe["popen"] == []
    assert informe["pesados_cargados"] == []
    registro = tmp_path / ".sky_claw" / "logs" / "workers" / "vfs-worker-job-xyz.log"
    assert "vfs_worker_job_identity_failed" in registro.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------------------------------
# Anclas por AST (enumeran la familia)
# --------------------------------------------------------------------------------------------------------------------


def test_el_conjunto_liviano_tiene_los_imports_congelados() -> None:
    """Un import nuevo en estos módulos puede arrastrar ``uuid``/``pydantic``/``platform``: se decide a propósito."""
    raiz = REPO / "sky_claw" / "local" / "mo2"
    assert _imports_de_nivel_de_modulo(raiz / "vfs_arranque.py") == frozenset(
        {
            "__future__",
            "argparse",
            "contextlib",
            "logging",
            "pathlib",
            "re",
            "sys",
            "collections.abc",
            "typing",
            "sky_claw.local.mo2.vfs_job_identity",
        }
    )
    assert _imports_de_nivel_de_modulo(raiz / "vfs_job_identity.py") == frozenset(
        {
            "__future__",
            "ctypes",
            "logging",
            "os",
            "re",
            "secrets",
            "sys",
            "time",
            "collections.abc",
            "dataclasses",
            "typing",
            "psutil",
        }
    )


def _sitios_de_llamada(ruta: pathlib.Path, nombre: str) -> list[str]:
    """Funciones (o ``<module>``) que contienen una llamada a ``nombre``."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    sitios: list[str] = []

    def visitar(nodo: ast.AST, contenedor: str) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            actual = hijo.name if isinstance(hijo, ast.FunctionDef | ast.AsyncFunctionDef) else contenedor
            if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Name) and hijo.func.id == nombre:
                sitios.append(contenedor)
            visitar(hijo, actual)

    visitar(arbol, "<module>")
    return sitios


def test_los_dos_despachos_de_main_pasan_por_arrancar_worker_y_ninguno_importa_el_worker() -> None:
    ruta = REPO / "sky_claw" / "__main__.py"
    assert sorted(_sitios_de_llamada(ruta, "arrancar_worker")) == ["<module>", "main"]

    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    importados = {
        nodo.module for nodo in ast.walk(arbol) if isinstance(nodo, ast.ImportFrom) and nodo.module is not None
    } | {alias.name for nodo in ast.walk(arbol) if isinstance(nodo, ast.Import) for alias in nodo.names}
    assert "sky_claw.local.mo2.vfs_worker" not in importados, "el worker solo se alcanza por vfs_arranque"


def test_las_tuplas_de_flags_de_main_son_las_del_arranque() -> None:
    arbol = ast.parse((REPO / "sky_claw" / "__main__.py").read_text(encoding="utf-8"))
    tuplas = [
        tuple(elt.value for elt in nodo.elts if isinstance(elt, ast.Constant))
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Tuple)
        and any(isinstance(e, ast.Constant) and e.value == "--vfs-worker" for e in nodo.elts)
        and len(nodo.elts) > 1
    ]
    assert tuplas, "no se encontró la tupla de flags VFS en __main__.py"
    assert all(t == vfs_arranque.FLAGS_VFS for t in tuplas)


def test_worker_main_verifica_el_job_antes_de_configurar_el_logging() -> None:
    """Orden sobre el árbol: ``verificar_identidad_del_job`` precede a ``setup_logging`` en ``worker_main``.

    ``setup_logging`` abre archivos y listeners; hacerlo antes de la prueba es trabajo de un proceso sin contención
    probada. (El arranque liviano ya verificó; ``worker_main`` sigue verificando porque también es entrada directa.)
    """
    ruta = REPO / "sky_claw" / "local" / "mo2" / "vfs_worker.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    worker_main = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "worker_main")
    lineas: dict[str, int] = {}
    for nodo in ast.walk(worker_main):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            lineas.setdefault(nodo.func.id, nodo.lineno)
    assert lineas["verificar_identidad_del_job"] < lineas["setup_logging"]
    assert "abortar_por_identidad" in lineas, "el aborto debe ser el MISMO que usa el arranque liviano"
