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


def test_el_lock_del_bashed_patch_ya_no_existe() -> None:
    """Sin tomador no hay contrato: el id de lock del Bashed Patch se retira con el camino.

    Se afirma sobre el MÓDULO y sobre el paquete: un id de lock que nadie toma es una mentira
    silenciosa sobre la etapa 6 (alguien podría creer que sigue serializada).
    """
    assert not hasattr(__import__("sky_claw.local.tools.wrye_bash_service", fromlist=["x"]), "BASHED_PATCH_RESOURCE_ID")

    apariciones: list[str] = []
    for py in sorted((RAIZ / "sky_claw").rglob("*.py")):
        texto = py.read_text(encoding="utf-8")
        if "Bashed Patch, 0.esp" in texto and '"mods"' not in texto:
            for nro, linea in enumerate(texto.splitlines(), 1):
                if "Bashed Patch, 0.esp" in linea and "BASHED_PATCH_NAME" not in linea:
                    apariciones.append(f"{py.relative_to(RAIZ)}:{nro}: {linea.strip()[:90]}")

    assert apariciones == [], "el id de lock/literal sigue en producción:\n" + "\n".join(apariciones)


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
