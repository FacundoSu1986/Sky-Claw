"""Etapa 6 sin camino headless — el contrato del RETIRO (ADR 0016 §7, PR C).

`WryeBashPipelineService.execute_pipeline` nació como el ritual mutante serializado de la
etapa 6 (locks `bashed-patch` + `load-order`, `ActionManifest`, TX con rollback del patch
previo). El ADR 0016 demostró que ese camino **no puede construir el parche**: `bash.py` no
expone build y ningún flag lo reconstruye, así que cada invocación tomaba los locks, abría
una TX y fallaba cerrado. Tras el PR B quedó **sin un solo caller de producción** (verificado
por `ast` sobre todo `sky_claw/`: sólo prosa en docstrings).

Este módulo congela la decisión del PR C —**retirar**, no conservar como compat— para que el
camino no vuelva por la puerta de atrás:

1. El servicio ya no expone `execute_pipeline` ni sus helpers de caja negra.
2. Su firma ya no pide las dependencias que sólo ese camino usaba (`lock_manager`,
   `snapshot_manager`, `journal`, `wrye_bash_runner`): un servicio que las acepta invita a
   reconstruir el camino.
3. Ningún módulo de producción construye el servicio con esas dependencias.
4. El lock externo `Bashed Patch, 0.esp` deja de existir: después del retiro no hay nada que
   serializar contra otra corrida (la etapa no muta), y un id de lock sin tomador es una
   invitación a creer que la etapa sigue protegiéndose.

Se retira en vez de documentarse como compat porque el hueco del saneo de `stdout`/`stderr`
(que el PR B declaró para el PR C) **se cierra por retiro**: sin camino que devuelva la salida
cruda del subproceso, no hay texto de terceros que sanear. Conservar el método habría exigido
agregarle saneo y mantener 36 tests de un camino inalcanzable.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO = RAIZ / "sky_claw/local/tools/wrye_bash_service.py"

#: El nombre canónico, medido desde el módulo que lo define y no desde un literal acá: si
#: el contrato cambia, este archivo no tiene que cambiar para seguir midiendo.
_NOMBRE_DEL_ARTEFACTO = __import__(
    "sky_claw.local.tools.wrye_bash_runner", fromlist=["BASHED_PATCH_NAME"]
).BASHED_PATCH_NAME

#: Helpers del camino headless. Enumeración literal: si alguno sobrevive —o vuelve— el
#: ancla falla hasta que alguien decida si es parte del camino vivo.
_HELPERS_DEL_CAMINO_MUERTO = frozenset(
    {
        "execute_pipeline",
        "_bashed_patch_target",
        "_emit_action_manifest",
        "_emit_flight_report",
        "_cerrar_tx_tras_rollback",
        "_mark_journal_rolled_back",
        "_rollback_target_files",
        "_destino_desde_runner",
        "ensure_runner",
    }
)

#: Dependencias que sólo existían para ese camino.
_DEPENDENCIAS_DEL_CAMINO_MUERTO = frozenset({"lock_manager", "snapshot_manager", "journal", "wrye_bash_runner"})


def _clase_del_modulo() -> ast.ClassDef:
    arbol = ast.parse(MODULO.read_text(encoding="utf-8"))
    return next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "WryeBashPipelineService")


#: Los métodos EXACTOS que quedan tras el retiro, medidos con `ast` contra `b247aea`.
#: No es una lista de "lo que no puede estar": es la lista cerrada de lo que hay. Agregar
#: un helper —aunque no esté en `_HELPERS_DEL_CAMINO_MUERTO`— obliga a decidir su contrato.
_METODOS_DEL_SERVICIO_ASISTIDO = frozenset(
    {
        "__init__",
        "_ensure_preflight",
        "_permission_targets",
        "_bashed_patch_path",
        "_mods_habilitados",
        "_artefacto_asistido",
        "_plugins_txt_path",
        "ejecutar_fase_asistida",
        "preparar_etapa_asistida",
        "verificar_etapa_asistida",
    }
)


def test_el_servicio_ya_no_expone_el_camino_headless() -> None:
    """`execute_pipeline` y sus helpers no sobreviven al PR C — y no hay hueco para otros.

    Verifica por AST (no por `hasattr`) para que el ancla siga funcionando aunque alguien
    reintroduzca el método con otro cuerpo. La primera aserción da el mensaje con el NOMBRE
    del helper que volvió; la segunda es la que no deja pasar un helper nuevo con otro
    nombre: la clase es un conjunto cerrado, no un subconjunto.
    """
    metodos = {n.name for n in _clase_del_modulo().body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert metodos & _HELPERS_DEL_CAMINO_MUERTO == set(), (
        "sobrevivió un helper del camino headless: "
        f"{sorted(metodos & _HELPERS_DEL_CAMINO_MUERTO)} — el retiro del PR C es completo o no es"
    )
    assert metodos == _METODOS_DEL_SERVICIO_ASISTIDO, (
        "la clase cambió de superficie: nuevos="
        f"{sorted(metodos - _METODOS_DEL_SERVICIO_ASISTIDO)}, faltantes="
        f"{sorted(_METODOS_DEL_SERVICIO_ASISTIDO - metodos)}. La etapa 6 es de SÓLO LECTURA "
        "(ADR 0016 §2): un método nuevo acá tiene que decidirse y documentarse en el ADR."
    )


def test_la_superficie_publica_del_servicio_esta_congelada() -> None:
    """Igualdad, no subconjunto: el servicio tiene exactamente las tres fases asistidas.

    Es la forma de que un ejecutor nuevo del ritual —el defecto que el ADR 0016 señala como
    dominante en este repo— no entre sin que alguien lo escriba acá y decida su contrato.
    """
    publicos = {
        n.name
        for n in _clase_del_modulo().body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
    }

    assert publicos == {"ejecutar_fase_asistida", "preparar_etapa_asistida", "verificar_etapa_asistida"}


def test_la_firma_no_pide_las_dependencias_del_camino_muerto() -> None:
    """Un constructor que acepta `lock_manager`/`journal` invita a reconstruir el camino.

    La firma es el contrato de DI del servicio: si el retiro deja los parámetros vivos, el
    siguiente que cablee el servicio va a creer que hay locks que tomar y TX que cerrar.
    """
    parametros = set(inspect.signature(WryeBashPipelineService.__init__).parameters) - {"self"}
    dep_muertas = parametros & _DEPENDENCIAS_DEL_CAMINO_MUERTO

    assert dep_muertas == set(), f"la firma todavía pide dependencias del camino headless: {sorted(dep_muertas)}"
    assert parametros == {"path_resolver", "plugin_limit_guard", "preflight", "default_domain"}


def test_ningun_modulo_de_produccion_construye_el_servicio_con_dependencias_muertas() -> None:
    """Enumera TODO `sky_claw/`: el servicio se construye sin lock/snapshot/journal/runner."""
    ofensores: list[str] = []
    for py in sorted((RAIZ / "sky_claw").rglob("*.py")):
        texto = py.read_text(encoding="utf-8")
        try:
            arbol = ast.parse(texto)
        except SyntaxError:  # pragma: no cover - un módulo ilegible ya rompe otros gates
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
            if nombre != "WryeBashPipelineService":
                continue
            claves = {kw.arg for kw in nodo.keywords} & _DEPENDENCIAS_DEL_CAMINO_MUERTO
            if claves:
                ofensores.append(f"{py.relative_to(RAIZ)}:{nodo.lineno} pasa {sorted(claves)}")

    assert ofensores == [], "construcciones con dependencias del camino muerto:\n" + "\n".join(ofensores)


#: Módulo que DEFINE el nombre del artefacto. Es el único lugar de `sky_claw/` donde el
#: literal puede existir: todo lo demás lo nombra por la constante.
_MODULO_CANONICO = pathlib.Path("sky_claw/local/tools/wrye_bash_runner.py")

#: Duplicado PREEXISTENTE en el carril de xEdit (``DelegateToBashedPatch``), declarado y
#: no tocado por este PR: ``sky_claw/local/xedit/**`` es de otro carril (ADR 0016 §7). Se
#: enumera para que la excepción sea exacta —una tercera definición, o mover esta de
#: archivo, rompe el ancla— y el drift ya está cubierto por comportamiento en
#: ``test_wrye_bash_lock.py::test_nombre_canonico_del_bashed_patch_sincronizado``.
_DEFINICIONES_TOLERADAS = frozenset({"sky_claw/local/xedit/patch_orchestrator.py"})


def _literales_del_artefacto(py: pathlib.Path) -> list[tuple[int, str]]:
    """`(línea, valor)` de CADA constante `str` que mencione el artefacto, por AST.

    Por AST y no por texto: un docstring o un comentario que lo nombre en prosa es un
    `ast.Constant` como cualquier otro (y también se reporta), mientras que un archivo que
    sólo lo menciona de pasada —``"mods"``, una lista de rutas— ya no gana la exención que
    la versión anterior daba por contener una subcadena cualquiera.
    """
    arbol = ast.parse(py.read_text(encoding="utf-8"))
    return [
        (nodo.lineno, nodo.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and _NOMBRE_DEL_ARTEFACTO in nodo.value
    ]


def test_el_lock_del_bashed_patch_ya_no_existe() -> None:
    """Sin tomador no hay contrato: el id de lock del Bashed Patch se retira con el camino.

    Se afirma sobre el MÓDULO: un id de lock que nadie toma es una mentira silenciosa sobre
    la etapa 6 (alguien podría creer que sigue serializada).
    """
    assert not hasattr(__import__("sky_claw.local.tools.wrye_bash_service", fromlist=["x"]), "BASHED_PATCH_RESOURCE_ID")


def test_el_registry_del_agente_ya_no_recibe_un_runner_de_wrye_bash() -> None:
    """El asa que quedaba: `AsyncToolRegistry(wrye_bash_runner=...)`.

    El handler dejó de admitir un runner, pero el **registry** seguía con el parámetro (y con
    su atributo) para pasárselo. Ningún caller lo usaba, así que era un parámetro muerto que
    sólo servía de asa para reconstruir el camino. Se retiró en el PR C; este test es lo que
    lo mantiene retirado — hallado por mutación, no por lectura: sin él, volver a agregarlo
    no rompía **ningún** test.

    Se distinguen las dependencias: ``lock_manager``/``snapshot_manager``/``journal`` siguen
    siendo legítimas en el registry (las usan otras tools), así que el ancla es específica.
    """
    from sky_claw.app.agent.tools import AsyncToolRegistry

    parametros = set(inspect.signature(AsyncToolRegistry.__init__).parameters)
    assert "wrye_bash_runner" not in parametros, "el registry volvió a aceptar un runner del camino retirado"

    ofensores: list[str] = []
    for py in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
            if nombre not in {"AsyncToolRegistry", "ToolRegistry"}:
                continue
            if any(kw.arg == "wrye_bash_runner" for kw in nodo.keywords):
                ofensores.append(f"{py.relative_to(RAIZ)}:{nodo.lineno}")

    assert ofensores == [], "construcciones del registry con un runner retirado:\n" + "\n".join(ofensores)


def test_el_nombre_del_artefacto_se_define_una_sola_vez() -> None:
    """Igualdad exacta: el literal vive en UNA definición, y su duplicado es uno declarado.

    La versión anterior de este ancla eximía por archivo (``'"mods"' not in texto``) y esa
    exención era un punto ciego medible: un literal suelto en ``output_targets.py`` —que
    menciona ``"mods"``— pasaba el test. Acá la unidad de cuenta es la CONSTANTE, así que
    no hay subcadena que pueda comprar una exención: cada aparición se enumera por archivo
    y línea, y sólo se toleran las dos definiciones conocidas (la canónica y la del carril
    de xEdit). Cualquier otra —en código o en prosa— rompe el ancla.
    """
    apariciones: list[tuple[str, int, str]] = []
    for py in sorted((RAIZ / "sky_claw").rglob("*.py")):
        for linea, valor in _literales_del_artefacto(py):
            apariciones.append((py.relative_to(RAIZ).as_posix(), linea, valor))

    archivos = {archivo for archivo, _, _ in apariciones}
    assert archivos - _DEFINICIONES_TOLERADAS == {_MODULO_CANONICO.as_posix()}, (
        "el nombre del artefacto se define en módulos no declarados:\n"
        + "\n".join(
            f"{a}:{n}: {v[:80]}"
            for a, n, v in apariciones
            if a not in _DEFINICIONES_TOLERADAS | {_MODULO_CANONICO.as_posix()}
        )
    )

    del_canonico = [(n, v) for a, n, v in apariciones if a == _MODULO_CANONICO.as_posix()]
    assert del_canonico == [(21, _NOMBRE_DEL_ARTEFACTO)], (
        "el módulo canónico debe contener el literal UNA vez, en la asignación de "
        f"``BASHED_PATCH_NAME``; encontré {del_canonico}. Fuera de ahí, se nombra la constante."
    )

    del_tolerado = [(n, v) for a, n, v in apariciones if a in _DEFINICIONES_TOLERADAS]
    assert del_tolerado == [(384, _NOMBRE_DEL_ARTEFACTO)], (
        "el duplicado declarado del carril de xEdit cambió (o apareció otro): "
        f"encontré {del_tolerado}. Si la duplicación se consolidó, sacá la excepción de "
        "``_DEFINICIONES_TOLERADAS`` en el mismo commit."
    )


@pytest.mark.parametrize(
    "simbolo",
    [
        "DistributedLockManager",
        "SnapshotTransactionLock",
        "LockAcquisitionError",
        "LockError",
        "LOAD_ORDER_RESOURCE_ID",
    ],
)
def test_el_modulo_ya_no_importa_la_maquinaria_del_camino_muerto(simbolo: str) -> None:
    """El ancla enumera los imports: reintroducir locks/TX en la etapa 6 obliga a decidirlo.

    La etapa asistida es de SÓLO LECTURA por contrato (ADR 0016 §2): si el módulo vuelve a
    importar el lock distribuido o la TX de snapshots, o alguien cambió el contrato o está
    reconstruyendo el camino headless por partes.
    """
    arbol = ast.parse(MODULO.read_text(encoding="utf-8"))
    importados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.ImportFrom, ast.Import)):
            importados |= {alias.name for alias in nodo.names}

    assert simbolo not in importados, f"el módulo volvió a importar {simbolo} con la etapa 6 de sólo lectura"
