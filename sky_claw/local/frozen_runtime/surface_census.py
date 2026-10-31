"""Censo ENUMERATIVO de superficies del binder, derivado por AST (P5-C1, FIX 7).

Tracker: #672
ADR 0012 §29.4

El defecto que este módulo cierra
---------------------------------
``CENSO_ESCRITORES_GAME_DIR`` era un ``frozenset`` literal con seis nombres y un
test que comparaba el literal contra sí mismo::

    assert frozenset({"ensure_skse", "grass_cache", ..., "dyndolod_legacy"}) == CENSO_ESCRITORES_GAME_DIR

Eso no es un censo, es una constante con un espejo. Y estaba **desactualizado**:
``grass_cache`` y ``wrye_bash`` no existen como funciones en el árbol, y las
otras cuatro no se llaman así (``pandora_output_target``,
``bodyslide_output_root``/``bodyslide_output_target``,
``dyndolod_legacy_recovery_target``, ``bashed_patch_target``). El test pasaba
mientras el censo no describía el código.

Qué hace este módulo en su lugar
--------------------------------
1. **Descubre por AST** las funciones que derivan un destino DENTRO del game dir
   (parámetro ``game`` + un ``return`` que lo usa) y las que escriben cada
   superficie del binder (llamando a la primitiva de escritura con el ancla de
   la superficie).
2. **Congela el resultado por igualdad literal** en un test: una función nueva
   que escriba la misma superficie rompe el ancla hasta que se decida si es un
   adaptador del catálogo o una excepción justificada. Es la forma concreta de
   atajar el defecto dominante del repo (arreglar un hermano y no al otro).
3. **Catálogo de adaptadores implementados**: para cada superficie declara la
   función que la escribe y la que la VERIFICA. El test no se conforma con que
   los nombres existan: ejerce cada verificador y comprueba que detecta una
   divergencia. Un set de nombres no demuestra integración; un verificador que
   falla cuando debe, sí.

Límite declarado del análisis estático
--------------------------------------
Cubre funciones ``def``/``async def`` de los módulos de ``sky_claw/`` y las
primitivas de escritura nombradas en ``PRIMITIVAS_DE_ESCRITURA``. No sigue
indirecciones dinámicas (``getattr``, ``globals()[...]``, ``functools.partial``)
ni métodos de clase fuera de ``sky_claw/local/tools/``. Ese límite lo cubren las
pruebas de COMPORTAMIENTO de ``tests/test_frozen_runtime_p5c1_superficies.py``,
que no dependen del análisis estático.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from collections.abc import Iterable
from enum import StrEnum

__all__ = [
    "ADAPTADORES_DESTINO_EN_GAME_DIR",
    "ANCLAS_DE_SUPERFICIE",
    "CATALOGO_ADAPTADORES",
    "CENSO_ESCRITORES_GAME_DIR",
    "CLASES_DE_SUPERFICIE",
    "ESCRITORES_DECLARADOS",
    "AdaptadorSuperficie",
    "ClaseSuperficie",
    "descubrir_adaptadores_destino_en_game_dir",
    "descubrir_escritores_de_superficies",
    "modulo_de",
]

_RAIZ_PAQUETE = pathlib.Path(__file__).resolve().parent.parent.parent  # .../sky_claw
_MODULO_OUTPUT_TARGETS = _RAIZ_PAQUETE / "local" / "tools" / "output_targets.py"


class ClaseSuperficie(StrEnum):
    """Las tres clases de §29.4, con qué las distingue."""

    #: Vive en el disco y MO2/Sky-Claw la leen de ahí (INI, config, active.json).
    PERSISTIDA = "PERSISTIDA"
    #: Se deriva de una persistida y vive en memoria del proceso (``SKYRIM_PATH``).
    DERIVADA = "DERIVADA"
    #: Evidencia de Sky-Claw sobre la transición; no la lee MO2 (journal, registro).
    CACHE = "CACHE"


@dataclasses.dataclass(frozen=True, slots=True)
class AdaptadorSuperficie:
    """Una superficie con su escritor y su verificador IMPLEMENTADOS.

    ``verificador`` no es documentación: el test lo resuelve y lo ejerce. Si el
    verificador no distingue el estado correcto del incorrecto, el adaptador no
    está integrado aunque los dos nombres existan.
    """

    clave: str
    clase: ClaseSuperficie
    escritor: str
    verificador: str
    descripcion: str


#: Catálogo de las superficies que el binder muta o produce.
CATALOGO_ADAPTADORES: tuple[AdaptadorSuperficie, ...] = (
    AdaptadorSuperficie(
        clave="mo2_ini",
        clase=ClaseSuperficie.PERSISTIDA,
        escritor="sky_claw.local.frozen_runtime.mo2_ini_escritura:actualizar_game_path_en_ini",
        verificador="sky_claw.local.frozen_runtime.effective_runtime.mo2_ini:leer_game_path_de_mo2",
        descripcion="modorganizer.ini [General] gamePath — lo que MO2 usa para lanzar el juego",
    ),
    AdaptadorSuperficie(
        clave="config",
        clase=ClaseSuperficie.PERSISTIDA,
        escritor="sky_claw.local.local_config:persistir_campo",
        verificador="sky_claw.local.frozen_runtime.runtime_clone_binder:_leer_skyrim_path_persistido",
        descripcion="config.toml skyrim_path — lo que las tools de Sky-Claw usan como raíz del juego",
    ),
    AdaptadorSuperficie(
        clave="active_json",
        clase=ClaseSuperficie.PERSISTIDA,
        escritor="sky_claw.local.frozen_runtime.state:escribir_estado_activo_cas",
        verificador="sky_claw.local.frozen_runtime.effective_runtime.oracle:desired_runtime_from_storage",
        descripcion="state/active.json — el Desired Runtime, escrito con CAS",
    ),
    AdaptadorSuperficie(
        clave="environ",
        clase=ClaseSuperficie.DERIVADA,
        escritor="sky_claw.local.frozen_runtime.runtime_clone_binder:ejecutar_transicion_binder",
        verificador="sky_claw.local.frozen_runtime.surface_census:_verificar_environ",
        descripcion="SKYRIM_PATH en el entorno del proceso — derivada de config.toml",
    ),
    AdaptadorSuperficie(
        clave="journal",
        clase=ClaseSuperficie.CACHE,
        escritor="sky_claw.local.frozen_runtime.journal:_finalizar_locked",
        verificador="sky_claw.local.frozen_runtime.journal:cargar_journal_transicion",
        descripcion="state/transition.json — la intención durable de la transición",
    ),
    AdaptadorSuperficie(
        clave="recuperacion",
        clase=ClaseSuperficie.CACHE,
        escritor="sky_claw.local.frozen_runtime.binder_recovery:escribir_registro_locked",
        verificador="sky_claw.local.frozen_runtime.binder_recovery:cargar_registro",
        descripcion="state/recovery/<transition_id>.json — el estado previo de las superficies",
    ),
)

#: Clase de cada superficie, indexada por la clave del catálogo. Se DERIVA del
#: catálogo (no es una segunda lista que se pueda desincronizar).
CLASES_DE_SUPERFICIE: dict[str, ClaseSuperficie] = {a.clave: a.clase for a in CATALOGO_ADAPTADORES}

#: Ancla sintáctica de cada superficie: el nombre que aparece en el código
#: cuando alguien la escribe. Es lo que el AST busca.
ANCLAS_DE_SUPERFICIE: dict[str, tuple[str, ...]] = {
    "mo2_ini": ("actualizar_game_path_en_ini",),
    "config": ("persistir_campo", "guardar_config"),
    "active_json": (
        "write_json_atomic",
        "save_frozen_runtime_state",
        "escribir_estado_activo_cas",
        "_escribir_estado_activo_cas_locked",
    ),
    "environ": (),
}

#: Campo de config que constituye la superficie. ``persistir_campo`` escribe
#: docenas de campos; sólo ``skyrim_path`` es superficie del binder.
_CAMPO_CONFIG = "skyrim_path"

#: Nombre del ancla de ruta del estado durable.
_ANCLA_ACTIVE = "active_state_path"


def modulo_de(referencia: str) -> tuple[str, str]:
    """``"a.b.c:func"`` → ``("a.b.c", "func")``."""
    modulo, _, nombre = referencia.partition(":")
    if not modulo or not nombre:
        raise ValueError(f"referencia de adaptador malformada: {referencia!r}")
    return modulo, nombre


# ---------------------------------------------------------------------------
# AST: escritores de cada superficie del binder
# ---------------------------------------------------------------------------


def _funciones(arbol: ast.Module) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield nodo


def _nombre_de_llamada(nodo: ast.Call) -> str | None:
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    return None


def _menciona(nodo: ast.AST, nombre: str) -> bool:
    """¿El subárbol menciona *nombre* como identificador o atributo?"""
    for hijo in ast.walk(nodo):
        if isinstance(hijo, ast.Name) and hijo.id == nombre:
            return True
        if isinstance(hijo, ast.Attribute) and hijo.attr == nombre:
            return True
    return False


def _constantes_de_texto(nodo: ast.Call) -> set[str]:
    return {a.value for a in ast.walk(nodo) if isinstance(a, ast.Constant) and isinstance(a.value, str)}


def _escribe_config_skyrim_path(nodo: ast.Call) -> bool:
    if _nombre_de_llamada(nodo) not in ANCLAS_DE_SUPERFICIE["config"]:
        return False
    return _CAMPO_CONFIG in _constantes_de_texto(nodo)


def _escribe_active_json(nodo: ast.Call) -> bool:
    nombre = _nombre_de_llamada(nodo)
    if nombre not in ANCLAS_DE_SUPERFICIE["active_json"]:
        return False
    if nombre in ("save_frozen_runtime_state", "escribir_estado_activo_cas", "_escribir_estado_activo_cas_locked"):
        return True
    return any(_menciona(arg, _ANCLA_ACTIVE) for arg in nodo.args)


#: Mutadores que operan sobre una ruta que la función NO nombra: la recibe.
_MUTADORES_SOBRE_RUTA = frozenset(
    {"mkstemp", "replace", "unlink", "write_json_atomic", "fdopen", "write", "_escribir_bytes_atomico"}
)


def _escribe_active_json_por_ancla_local(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Escritura del estado cuando la ruta se enlaza a una variable antes de usarla.

    ``p = active_state_path(root); write_json_atomic(p, ...)`` escribe
    ``active.json`` y el argumento de la llamada es un ``Name``, no la expresión
    ancla. La regla anterior sólo miraba los argumentos, así que ese patrón
    —ordinario— escapaba al censo. Ahora se exige que la función MENCIONE el
    ancla y contenga un mutador; el conjunto congelado absorbe el posible falso
    positivo, que en un censo es preferible a un falso negativo.
    """
    if not any(_menciona(nodo, _ANCLA_ACTIVE) for nodo in ast.walk(funcion)):
        return False
    return any(
        isinstance(nodo, ast.Call) and _nombre_de_llamada(nodo) in _MUTADORES_SOBRE_RUTA for nodo in ast.walk(funcion)
    )


def _restaura_superficies_genericas(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Restaurador GENÉRICO: escribe la superficie que le diga ``SuperficieCapturada``.

    ``_restaurar_persistida`` no repite ``active_state_path`` ni el literal del
    archivo: recibe la ruta dentro de la superficie y escribe. Es un escritor de
    las TRES superficies persistidas, con el ``kind`` decidido en runtime, y el
    censo tiene que enumerarlo en las tres o el ancla declara cobertura que no
    tiene (Codex sobre #843).
    """
    menciona_superficie = any(
        (isinstance(nodo, ast.Name) and nodo.id == "SuperficieCapturada")
        or (isinstance(nodo, ast.Attribute) and nodo.attr == "SuperficieCapturada")
        for nodo in ast.walk(funcion)
    )
    if not menciona_superficie:
        return False
    return any(
        isinstance(nodo, ast.Call) and _nombre_de_llamada(nodo) in _MUTADORES_SOBRE_RUTA for nodo in ast.walk(funcion)
    )


def _escribe_active_json_por_nombre(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Escritura del estado por NOMBRE de archivo, no por la primitiva.

    ``_save_frozen_runtime_state_locked`` no llama a ``write_json_atomic``:
    hace ``mkstemp`` + ``os.replace`` con el prefijo ``.active.json.``. Un censo
    anclado sólo en la primitiva no lo vería, y es justamente el hermano que hay
    que enumerar. La regla es: el nombre del archivo aparece como literal Y la
    función tiene un mutador.
    """
    menciona_archivo = any(
        isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and "active.json" in nodo.value
        for nodo in ast.walk(funcion)
    )
    if not menciona_archivo:
        return False
    mutadores = {"mkstemp", "replace", "unlink", "write_json_atomic", "fdopen"}
    return any(isinstance(nodo, ast.Call) and _nombre_de_llamada(nodo) in mutadores for nodo in ast.walk(funcion))


def _escribe_mo2_ini(nodo: ast.Call) -> bool:
    return _nombre_de_llamada(nodo) in ANCLAS_DE_SUPERFICIE["mo2_ini"]


def _escribe_environ(nodo: ast.stmt) -> bool:
    objetivos: list[ast.AST] = []
    if isinstance(nodo, ast.Assign):
        objetivos = list(nodo.targets)
    elif isinstance(nodo, (ast.AugAssign, ast.AnnAssign)):
        objetivos = [nodo.target]
    for objetivo in objetivos:
        if (
            isinstance(objetivo, ast.Subscript)
            and isinstance(objetivo.slice, ast.Constant)
            and objetivo.slice.value == "SKYRIM_PATH"
        ):
            return True
    return False


_REGLAS_ESCRITURA = {
    "mo2_ini": _escribe_mo2_ini,
    "config": _escribe_config_skyrim_path,
    "active_json": _escribe_active_json,
}


def _modulos_del_paquete() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(_RAIZ_PAQUETE.rglob("*.py")))


def descubrir_escritores_de_superficies(
    raiz: pathlib.Path | None = None,
) -> dict[str, frozenset[str]]:
    """``{superficie: {"modulo::funcion"}}`` de quien la escribe, por AST.

    Recorre todos los módulos del paquete y atribuye cada escritura a la función
    que la contiene (la más interna). Un módulo nuevo con una escritura de
    ``active.json`` aparece acá sin que nadie lo declare.
    """
    base = pathlib.Path(raiz) if raiz is not None else _RAIZ_PAQUETE
    encontrados: dict[str, set[str]] = {clave: set() for clave in ANCLAS_DE_SUPERFICIE}
    for modulo in sorted(base.rglob("*.py")):
        try:
            arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
        except (OSError, SyntaxError):
            continue
        relativo = modulo.relative_to(base).as_posix()
        for funcion in _funciones(arbol):
            for superficie, regla in _REGLAS_ESCRITURA.items():
                if any(isinstance(nodo, ast.Call) and regla(nodo) for nodo in ast.walk(funcion)):
                    encontrados[superficie].add(f"{relativo}::{funcion.name}")
            if _escribe_active_json_por_nombre(funcion) or _escribe_active_json_por_ancla_local(funcion):
                encontrados["active_json"].add(f"{relativo}::{funcion.name}")
            if _restaura_superficies_genericas(funcion):
                for superficie in ("mo2_ini", "config", "active_json"):
                    encontrados[superficie].add(f"{relativo}::{funcion.name}")
            if any(_escribe_environ(nodo) for nodo in ast.walk(funcion) if isinstance(nodo, ast.stmt)):
                encontrados["environ"].add(f"{relativo}::{funcion.name}")
    return {clave: frozenset(valor) for clave, valor in encontrados.items()}


#: Escritores que el censo DEBE encontrar, congelados por igualdad literal.
#:
#: Cada entrada que no es el adaptador del catálogo está acá porque se decidió
#: que lo es, y el test de comportamiento la ejerce:
#:
#: - ``storage.py::_initialize_frozen_runtime_storage_locked`` crea el
#:   ``active.json`` inicial (placeholder de arranque limpio) bajo el lock y con
#:   admisión de namespace. No compite con el binder: corre una sola vez, cuando
#:   el archivo NO existe.
#: - ``state.py::escribir_estado_activo_cas`` es la PUERTA del CAS: el único
#:   escritor del binder y el que valida revisión, identidad y valor previo.
#: - ``state.py::_save_frozen_runtime_state_locked`` es el cuerpo de la API
#:   general de P2/P3 (``save_frozen_runtime_state``), que delega en él. Escribe
#:   ``active.json`` con ``mkstemp`` + ``os.replace`` y NO hace CAS: no es un
#:   camino del binder y el binder no lo usa. Que aparezca en el censo es el
#:   punto — antes era invisible.
#: - ``binder_recovery.py::_restaurar_environ`` escribe ``SKYRIM_PATH`` al
#:   restaurar. Es el hermano de escritura que el censo encontró solo: el binder
#:   lo escribe al bindear y el rollback al volver atrás.
ESCRITORES_DECLARADOS: dict[str, frozenset[str]] = {
    "mo2_ini": frozenset(
        {
            "local/frozen_runtime/runtime_clone_binder.py::ejecutar_transicion_binder",
            # Restaurador genérico: escribe la superficie que le indique el
            # `kind` de la SuperficieCapturada. Ver `_restaura_superficies_genericas`.
            "local/frozen_runtime/binder_recovery.py::_restaurar_persistida",
        }
    ),
    "config": frozenset(
        {
            "local/frozen_runtime/runtime_clone_binder.py::ejecutar_transicion_binder",
            "local/frozen_runtime/binder_recovery.py::_restaurar_persistida",
        }
    ),
    "active_json": frozenset(
        {
            "local/frozen_runtime/runtime_clone_binder.py::ejecutar_transicion_binder",
            "local/frozen_runtime/binder_recovery.py::_restaurar_persistida",
            "local/frozen_runtime/state.py::_save_frozen_runtime_state_locked",
            "local/frozen_runtime/state.py::escribir_estado_activo_cas",
            "local/frozen_runtime/storage.py::_initialize_frozen_runtime_storage_locked",
        }
    ),
    "environ": frozenset(
        {
            "local/frozen_runtime/binder_recovery.py::_restaurar_environ",
            "local/frozen_runtime/runtime_clone_binder.py::ejecutar_transicion_binder",
        }
    ),
}


# ---------------------------------------------------------------------------
# AST: adaptadores que derivan un destino DENTRO del game dir
# ---------------------------------------------------------------------------


def _parametros(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = funcion.args
    nombres = {a.arg for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs)}
    return nombres


def _retorna_usando(funcion: ast.FunctionDef | ast.AsyncFunctionDef, nombre: str) -> bool:
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Return) and nodo.value is not None and _menciona(nodo.value, nombre):
            return True
    return False


def _retorna_ruta_compuesta(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """¿Algún ``return`` construye una ruta con ``/``?

    ``bodyslide_output_target`` no menciona ``game`` en su ``return`` (devuelve
    ``root / group``, con ``root`` derivado de ``bodyslide_output_root(game=...)``).
    Exigir que ``game`` aparezca en el ``return`` dejaba afuera justo al
    adaptador que compone. La regla que sí lo cubre y sigue siendo estructural:
    el parámetro se llama ``game`` **y** la función devuelve una ruta compuesta.
    """
    for nodo in ast.walk(funcion):
        if (
            isinstance(nodo, ast.Return)
            and nodo.value is not None
            and any(isinstance(hijo, ast.BinOp) and isinstance(hijo.op, ast.Div) for hijo in ast.walk(nodo.value))
        ):
            return True
    return False


def descubrir_adaptadores_destino_en_game_dir(
    modulo: pathlib.Path | None = None,
) -> frozenset[str]:
    """Funciones que derivan un destino dentro del game dir, por AST.

    La regla es estructural, no nominal: **parámetro llamado ``game``** y un
    ``return`` que (i) usa ``game`` o (ii) compone una ruta con ``/``. Una
    función nueva que devuelva ``game / "algo"`` entra sola al censo; una que se
    llame ``dyndolod_output_target`` pero devuelva otra cosa, no.
    """
    ruta = pathlib.Path(modulo) if modulo is not None else _MODULO_OUTPUT_TARGETS
    if not ruta.exists():
        return frozenset()
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    encontradas: set[str] = set()
    for nodo in arbol.body:
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "game" not in _parametros(nodo):
            continue
        if _retorna_usando(nodo, "game") or _retorna_ruta_compuesta(nodo):
            encontradas.add(nodo.name)
    return frozenset(encontradas)


#: Resultado congelado de ``descubrir_adaptadores_destino_en_game_dir``. El test
#: compara por igualdad literal: agregar un adaptador rompe el ancla hasta que se
#: decida si escribe en el game dir.
ADAPTADORES_DESTINO_EN_GAME_DIR: frozenset[str] = frozenset(
    {
        "bashed_patch_target",
        "bodyslide_output_root",
        "bodyslide_output_target",
        "dyndolod_legacy_recovery_target",
        "pandora_output_target",
    }
)

#: Escritores del game dir que NO son adaptadores de destino (no derivan una
#: ruta: reciben el directorio y escriben dentro). Se declaran aparte porque
#: mezclarlos con los adaptadores haría que el censo describiera dos conceptos
#: con un solo conjunto.
ESCRITORES_DIRECTOS_EN_GAME_DIR: frozenset[str] = frozenset({"tools_installer.py::ensure_skse"})

#: Censo consolidado: lo que el repo escribe dentro del game dir.
CENSO_ESCRITORES_GAME_DIR: frozenset[str] = ADAPTADORES_DESTINO_EN_GAME_DIR | ESCRITORES_DIRECTOS_EN_GAME_DIR


# ---------------------------------------------------------------------------
# Verificadores auxiliares (usados por el catálogo)
# ---------------------------------------------------------------------------


def _verificar_environ(environ: dict[str, str], esperado: str | None) -> bool:
    """Verificador de la superficie DERIVADA: el valor del entorno, o su ausencia."""
    actual = environ.get("SKYRIM_PATH")
    if esperado is None:
        return actual is None
    return actual == esperado
