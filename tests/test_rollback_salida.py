"""Rollback de la SALIDA ante un resultado fallido — ancla de familia de U-04.

**Las dos mitades, y por qué ninguna alcanza sola.** Que un ritual pase su destino
real como ``target_files`` al :class:`SnapshotTransactionLock` —o que envuelva su
directorio en un :class:`DirectoryRollback`— es condición necesaria pero **no
suficiente**: los dos mecanismos restauran solo ante excepción o ante un rollback
forzado. Un subproceso que sale non-zero (o que agota su timeout y se traduce a
``success=False``) hace que el ``async with`` salga *limpio*, así que el backup se
**descarta** y el output parcial queda en disco — con la agravante de que el caller
cree que hubo rollback. Wrye Bash (#397) y Pandora (#399) cerraron eso elevando una
excepción interna DENTRO del context; este archivo ancla la **clase**, no los casos.

**Enumera la familia, no muestrea un caso** (patrón ``RITUAL_TOOL_MAP`` de
``tests/test_ritual_dispatch.py`` y ``SERVICIOS_CON_SALIDA`` de
``tests/test_output_targets.py``): **todo módulo del paquete** que construya un
``SnapshotTransactionLock`` debe estar clasificado con su mecanismo de rollback, y
los que hoy no tienen uno figuran con el motivo **nombrado**.

El alcance del barrido es en sí mismo la lección: la primera versión de este archivo
enumeraba ``local/tools/*_service.py`` y por eso no veía a ``run_bodyslide_batch``,
que toma el lock desde ``app/agent/tools/system_tools.py`` — un ancla de familia que
muestrea el directorio equivocado reproduce el defecto que existe para atajar.
"""

from __future__ import annotations

import pathlib

_PAQUETE = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

#: Módulos que construyen el lock SIN ser un mutador de salida — se excluyen de la
#: familia con su motivo, no por omisión.
_NO_SON_MUTADORES: dict[str, str] = {
    "sky_claw/app/db/locks.py": "define la clase; no la usa sobre un output.",
    "sky_claw/app/orchestrator/preview/chain_preview_service.py": (
        "es el dry-run: usa force_rollback=True, que YA revierte siempre — el "
        "caso que este archivo cubre es el contrario (salida limpia sin revertir)."
    ),
}

#: Módulo → mecanismo con el que revierte su SALIDA ante un run fallido.
#:
#: ``"snapshot"``    — ``target_files`` reales en el ``SnapshotTransactionLock``.
#: ``"directorio"``  — move-aside O(1) de ``DirectoryRollback``.
#: ``"no-muta"``     — no produce salida propia que revertir (motivo nombrado).
#: ``"pendiente"``   — U-04 sigue ABIERTO acá, con el motivo nombrado. NO es un
#:                     "ya lo miramos": es deuda declarada, y el test de abajo
#:                     exige que el motivo esté escrito.
MECANISMO_DE_ROLLBACK: dict[str, str] = {
    "sky_claw/local/tools/wrye_bash_service.py": "snapshot",
    "sky_claw/local/tools/synthesis_service.py": "snapshot",
    "sky_claw/local/tools/xedit_service.py": "snapshot",
    "sky_claw/local/tools/loot_service.py": "snapshot",
    "sky_claw/local/tools/dyndolod_service.py": "directorio",
    "sky_claw/local/tools/pandora_service.py": "directorio",
    "sky_claw/local/tools/vramr_service.py": "no-muta",
    "sky_claw/app/agent/tools/system_tools.py": "pendiente",
}

#: Motivo, para los que no revierten su salida. Escribir el racional NO cuenta como
#: cerrarlo (#318/#373): estos siguen contando como U-04 abierto en
#: ``docs/pending_ooda_status.md``.
MOTIVO: dict[str, str] = {
    "sky_claw/local/tools/vramr_service.py": (
        "VRAMr optimiza texturas hacia su propio output y no muta la entrada; el "
        "lock serializa, no snapshotea. Anclado en tests/test_vramr_service.py."
    ),
    "sky_claw/app/agent/tools/system_tools.py": (
        "run_bodyslide_batch escribe en game_path/<output_path>, un DIRECTORIO cuyo "
        "nombre elige el LLM (BodySlideBatchParams.output_path, default 'meshes'). "
        "El move-aside que resolvió Pandora no aplica: su destino es una constante "
        "del código y este es un parámetro del modelo, así que apuntar a un árbol "
        "compartido (Data/meshes) haría destructivo al propio rollback."
    ),
}


def _modulos_que_toman_el_lock() -> set[str]:
    """Familia a clasificar: TODO módulo que construya el lock transaccional.

    Se detecta por el uso de ``SnapshotTransactionLock(``: tomar el lock es,
    literalmente, declarar "esta operación muta estado compartido". Se barre el
    paquete entero a propósito — acotarlo a ``local/tools`` fue lo que dejó a
    BodySlide fuera de la primera versión de este ancla.
    """
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if "SnapshotTransactionLock(" in ruta.read_text(encoding="utf-8")
    } - set(_NO_SON_MUTADORES)


def test_la_enumeracion_cubre_todos_los_mutadores() -> None:
    """Guard de completitud: un mutador nuevo obliga a declarar CÓMO revierte su
    salida, en vez de heredar en silencio el ``target_files=[]``."""
    assert _modulos_que_toman_el_lock() == set(MECANISMO_DE_ROLLBACK)


def test_todo_mutador_sin_rollback_nombra_su_motivo() -> None:
    """ "Pendiente" sin motivo escrito es indistinguible de un olvido."""
    sin_rollback = {m for m, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo in {"pendiente", "no-muta"}}

    assert sin_rollback == set(MOTIVO)
    assert all(MOTIVO[m].strip() for m in sin_rollback)


def test_quien_dice_revertir_por_directorio_usa_el_move_aside() -> None:
    """La clasificación no es prosa: quien figura como ``"directorio"`` tiene que
    construir un ``DirectoryRollback`` de verdad, y quien lo construye tiene que
    estar clasificado así. Sin esto, un servicio podría quedar etiquetado como
    protegido sin tener el mecanismo."""
    declarados = {m for m, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo == "directorio"}
    reales = {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if "DirectoryRollback(" in ruta.read_text(encoding="utf-8")
    } - {"sky_claw/local/tools/_dir_rollback.py"}  # define la clase

    assert declarados == reales
