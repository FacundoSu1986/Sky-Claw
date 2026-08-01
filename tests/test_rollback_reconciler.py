"""Reconciliador de arranque de backups huérfanos — U-08 mitad 2.

La mitad 1 (#378) cerró el clon parcial en el punto de ``clone()``. Esta cubre lo
que ninguna limpieza in-process puede cubrir: una **muerte dura** (SIGKILL, OOM,
corte de luz) entre el move-aside y su restauración. Los locks se auto-curan por
TTL; las promesas de filesystem no.

**Barrer NO es borrar.** U-08 pide *"complete/revierta según marcador durable; como
piso, GC de backups huérfanos"*, y el matiz es la parte que importa: un
``<dir>.rollback-<nonce>`` es la **única copia** del output previo. Borrarlo a
ciegas convierte un reconciliador en un destructor de datos — el operador pierde su
generación anterior de LODs (horas de cómputo) sin haber pedido nada. Por eso cada
familia se reconcilia según el marcador durable que ya está en disco, y los tests
de acá afirman tanto lo que SÍ se toca como lo que **no** se toca.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner
from sky_claw.local.tools.output_targets import pandora_output_target
from sky_claw.local.tools.pandora_service import BEHAVIOR_GRAPHS_RESOURCE_ID
from sky_claw.local.tools.rollback_reconciler import (
    PRODUCTORES_CABLEADOS,
    VENTANA_DE_GRACIA_SEGUNDOS,
    ProductorDeMoveAside,
    _listar_backups_move_aside,
    construir_productores_de_move_aside,
    reconcile_orphan_rollback_backups,
)
from tests._symlink_guard import symlink_guard

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_PAQUETE = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"


# ---------------------------------------------------------------------------
# Ancla de familia — todo productor de backups tiene su reconciliación
# ---------------------------------------------------------------------------

#: Módulo que **compone** un nombre ``rollback-*`` → familia que deja en disco. Es
#: la contracara del ancla de U-04 (``tests/test_rollback_salida.py``): allá se
#: enumera quién **revierte**, acá quién deja **residuo durable** que reconciliar.
PRODUCTORES_DEL_NOMBRE: dict[str, str] = {
    "sky_claw/local/tools/_dir_rollback.py": "move-aside",
    "sky_claw/local/mo2/profile_sandbox.py": "clon-sandbox",
}

#: Módulos que **usan** ``DirectoryRollback`` sobre un directorio propio, y el
#: productor con el que el reconciliador los barre. Se enumeran aparte porque el
#: riesgo que traen no es un nombre nuevo sino un **destino exacto** nuevo: un
#: servicio que mueva aparte otro dir —o use otro lock— deja residuo donde el
#: reconciliador no mira. Un usuario nuevo rompe el ancla hasta que se le declare
#: su ``ProductorDeMoveAside``.
USUARIOS_DEL_MOVE_ASIDE: dict[str, str] = {
    "sky_claw/local/tools/dyndolod_service.py": "dyndolod",
    "sky_claw/local/tools/pandora_service.py": "pandora",
}

#: Excluido con motivo: consume el prefijo, no lo produce (es este reconciliador).
_CONSUMIDOR = "sky_claw/local/tools/rollback_reconciler.py"


def _modulos_con_el_nombre(literal: str) -> set[str]:
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if literal in ruta.read_text(encoding="utf-8")
    }


def test_todo_productor_de_backups_tiene_su_familia_reconciliada() -> None:
    """Guard de completitud: un productor nuevo de ``rollback-*`` obliga a decidir
    cómo se reconcilia su residuo, en vez de dejarlo leakear en silencio."""
    assert _modulos_con_el_nombre("rollback-") - {_CONSUMIDOR} == set(PRODUCTORES_DEL_NOMBRE)


def test_todo_usuario_del_move_aside_tiene_su_productor_declarado() -> None:
    """Cada usuario de ``DirectoryRollback`` deja residuo de destinos exactos bajo
    SU lock. Un usuario nuevo rompe acá hasta que alguien decida ambas cosas — que
    es la pregunta que importa, no el nombre del archivo."""
    usuarios = _modulos_con_el_nombre("DirectoryRollback(") - {
        "sky_claw/local/tools/_dir_rollback.py",  # define la clase
    }

    assert usuarios == set(USUARIOS_DEL_MOVE_ASIDE)
    assert set(USUARIOS_DEL_MOVE_ASIDE.values()) <= set(PRODUCTORES_CABLEADOS)


#: Reconciliadores de arranque que DEBEN estar invocados en ``app_context``. Un
#: reconciliador que existe pero que nadie llama es exactamente el modo de falla de
#: #240/#252/#362: todo verde en la suite y un no-op en producción.
RECONCILIADORES_DE_ARRANQUE: frozenset[str] = frozenset(
    {
        "reconcile_orphan_precache_flag",  # U-03 — PrecacheGrass.txt
        "reconcile_orphan_rollback_backups",  # U-08 mitad 2 — este módulo
    }
)


def test_los_reconciliadores_estan_invocados_en_el_arranque() -> None:
    """Se afirma sobre el AST, no sobre un grep: un import huérfano o una mención
    en un comentario NO cuentan como cableado. Lo que se exige es una llamada real
    dentro de ``app_context.py``."""
    import ast

    arbol = ast.parse((_PAQUETE / "app_context.py").read_text(encoding="utf-8"))
    invocados = {
        nodo.func.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    }

    assert invocados >= RECONCILIADORES_DE_ARRANQUE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> AsyncIterator[DistributedLockManager]:
    mgr = DistributedLockManager(
        tmp_path / "locks.db",
        default_ttl=30.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def mods(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "mo2" / "mods"
    raiz.mkdir(parents=True)
    return raiz


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "mo2" / ".skyclaw_sandbox"
    raiz.mkdir(parents=True)
    return raiz


def _dyndolod(mods: pathlib.Path) -> ProductorDeMoveAside:
    """Productor de DynDOLOD: mueve aparte sus mods de salida bajo ``<mo2>/mods``."""
    return ProductorDeMoveAside(
        nombre="dyndolod",
        lock_resource_id="dyndolod-pipeline",
        destinos=(mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME, mods / DynDOLODRunner.TEXGEN_MOD_NAME),
    )


def _backup_move_aside(mods: pathlib.Path, nombre: str, *, contenido: str) -> pathlib.Path:
    """Backup tal cual lo deja ``DirectoryRollback.__aenter__`` (rename O(1))."""
    backup = mods / f"{nombre}.rollback-1753700000000000000"
    backup.mkdir()
    (backup / "DynDOLOD.esp").write_text(contenido, encoding="utf-8")
    return backup


def _clon_sandbox(sandbox: pathlib.Path, *, con_backup_de_promote: bool) -> pathlib.Path:
    """Clon tal cual lo deja ``ProfileSandbox.clone()`` (dir con UUID)."""
    clon = sandbox / "Default-a1b2c3d4e5f6"
    (clon / "profile").mkdir(parents=True)
    (clon / "profile" / "plugins.txt").write_text("Skyrim.esm\n", encoding="utf-8")
    if con_backup_de_promote:
        backup = clon / "rollback-deadbeef"
        backup.mkdir()
        (backup / "plugins.txt").write_text("estado real previo al promote\n", encoding="utf-8")
    return clon


# ---------------------------------------------------------------------------
# Familia move-aside — el marcador durable es la presencia del target
# ---------------------------------------------------------------------------


async def test_restaura_el_backup_cuando_el_target_no_existe(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Muerte dura ENTRE el move-aside y la regeneración: el tool nunca recreó el
    dir, así que el backup es el último estado bueno. Es exactamente lo que habría
    hecho ``DirectoryRollback.__aexit__`` — solo que el proceso no llegó."""
    backup = _backup_move_aside(mods, "DynDOLOD Output", contenido="LODs previos")
    destino = mods / "DynDOLOD Output"

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert destino.is_dir()
    assert (destino / "DynDOLOD.esp").read_text(encoding="utf-8") == "LODs previos"
    assert not backup.exists()
    assert resultado.restaurados == (destino,)


async def test_preserva_el_backup_cuando_el_target_ya_existe(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Muerte dura DESPUÉS de que el tool escribió output nuevo. Estado ambiguo:
    no se sabe si ese output está completo. Borrar el backup destruiría la única
    copia del anterior; pisar el nuevo tiraría el trabajo del run. No se toca
    ninguno de los dos — se reporta para que decida el operador."""
    backup = _backup_move_aside(mods, "DynDOLOD Output", contenido="LODs previos")
    destino = mods / "DynDOLOD Output"
    destino.mkdir()
    (destino / "DynDOLOD.esp").write_text("LODs nuevos, quizas parciales", encoding="utf-8")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert backup.exists(), "el backup previo no debe borrarse: es la única copia del output anterior"
    assert (destino / "DynDOLOD.esp").read_text(encoding="utf-8") == "LODs nuevos, quizas parciales"
    assert resultado.preservados == (backup,)
    assert resultado.restaurados == ()


async def test_barre_un_productor_cuya_salida_no_cuelga_de_mods(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """El residuo NO siempre vive bajo ``<mo2>/mods``.

    DynDOLOD mueve aparte sus mods de salida, que sí cuelgan de ahí. Pandora
    mueve aparte su único ``Pandora_Output`` administrado junto al juego. Un
    reconciliador que solo barra ``<mo2>/mods`` es ciego a ese residuo: el backup
    queda huérfano para siempre y los behavior graphs previos no vuelven — el
    modo de falla exacto que U-08 mitad 2 existe para cubrir.
    """
    game = tmp_path / "game"
    game.mkdir()
    backup = game / "Pandora_Output.rollback-1753700000000000000"
    backup.mkdir()
    (backup / "behaviors.hkx").write_text("behavior graphs previos", encoding="utf-8")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id="behavior-graphs",
                destinos=(game / "Pandora_Output",),
            )
        ],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    destino = game / "Pandora_Output"
    assert destino.is_dir()
    assert (destino / "behaviors.hkx").read_text(encoding="utf-8") == "behavior graphs previos"
    assert resultado.restaurados == (destino,)


async def test_pandora_no_restaura_un_sibling_ajeno_con_sufijo_valido(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """El productor declara el target exacto; un sibling no le pertenece.

    El sufijo prueba que el directorio podría ser un backup real de otro
    componente. Pandora no puede restaurarlo bajo el lock de behavior graphs.
    """
    game = tmp_path / "game"
    game.mkdir()
    pandora_backup = game / "Pandora_Output.rollback-1753700000000000000"
    pandora_backup.mkdir()
    crashlogs_backup = game / "CrashLogs.rollback-1753700000000000001"
    crashlogs_backup.mkdir()

    resultado = await reconcile_orphan_rollback_backups(
        productores=[
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id="behavior-graphs",
                destinos=(game / "Pandora_Output",),
            )
        ],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    assert (game / "Pandora_Output").is_dir()
    assert not pandora_backup.exists()
    assert crashlogs_backup.is_dir()
    assert not (game / "CrashLogs").exists()
    assert resultado.restaurados == (game / "Pandora_Output",)


async def test_cada_productor_se_guarda_con_su_propio_lock(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """El guard es por productor, no global.

    Un ritual de Pandora en curso (lock ``behavior-graphs``) no debe frenar el
    barrido del residuo de DynDOLOD, ni al revés — y sobre todo: el residuo de
    Pandora NO puede barrerse mirando el lock de DynDOLOD, que es lo que haría
    un guard único. Cada familia se reconcilia bajo el lock del ritual que la
    produce, igual que ``reconcile_orphan_precache_flag``.
    """
    game = tmp_path / "game"
    game.mkdir()
    (game / "Pandora_Output.rollback-1753700000000000000").mkdir()
    _backup_move_aside(mods, "DynDOLOD Output", contenido="LODs previos")
    await lock_manager.acquire_lock("behavior-graphs", "otra-instancia", ttl=60.0)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[
            _dyndolod(mods),
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id="behavior-graphs",
                destinos=(game / "Pandora_Output",),
            ),
        ],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    # Pandora en curso: su backup queda intacto.
    assert (game / "Pandora_Output.rollback-1753700000000000000").exists()
    assert not (game / "Pandora_Output").exists()
    assert "pandora" in resultado.omitidos_por_lock
    # DynDOLOD no está corriendo: el suyo SÍ se restaura.
    assert (mods / "DynDOLOD Output" / "DynDOLOD.esp").read_text(encoding="utf-8") == "LODs previos"
    assert "dyndolod" not in resultado.omitidos_por_lock


def test_el_constructor_resuelve_los_destinos_reales_de_cada_productor(tmp_path: pathlib.Path) -> None:
    """El cableado de producción sale de acá, no de una lista escrita a mano en
    ``app_context``: los nombres que devuelve deben ser los que el ancla declara
    como cableados, y los destinos los que el servicio realmente usa."""
    mo2 = tmp_path / "mo2"

    productores = construir_productores_de_move_aside(mo2_root=mo2)

    assert {p.nombre for p in productores} <= PRODUCTORES_CABLEADOS
    dyndolod = next(p for p in productores if p.nombre == "dyndolod")
    assert dyndolod.destinos == (
        mo2 / "mods" / DynDOLODRunner.DYNDOLLOD_MOD_NAME,
        mo2 / "mods" / DynDOLODRunner.TEXGEN_MOD_NAME,
    )
    assert dyndolod.lock_resource_id == "dyndolod-pipeline"


def test_el_constructor_barre_solo_el_destino_administrado_de_pandora(tmp_path: pathlib.Path) -> None:
    """El destino de Pandora es el único output que administra el servicio.

    Es el hermano de #388: allá el sondeo de permisos y la búsqueda de salida
    divergieron por tener dos fuentes. Acá el rollback y el barrido comparten
    ``pandora_output_target``.
    """
    game = tmp_path / "segmento" / ".." / "game"

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    pandora = next(p for p in productores if p.nombre == "pandora")
    assert pandora.lock_resource_id == BEHAVIOR_GRAPHS_RESOURCE_ID
    output = pandora_output_target(game=game)
    assert output is not None
    assert pandora.destinos == (game.resolve() / "Pandora_Output",)
    assert pandora.destinos == (output,)


def test_el_constructor_sin_rutas_resolubles_no_inventa_nada() -> None:
    """Sin MO2 ni juego resolubles no se barre nada ni se inventa una ruta."""
    assert construir_productores_de_move_aside(mo2_root=None, game=None) == []


async def test_no_toca_nada_si_el_ritual_de_dyndolod_esta_en_curso(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Mismo guard que ``reconcile_orphan_precache_flag``: con el lock del ritual
    vivo (aun en OTRA instancia de Sky-Claw) el backup es legítimo, no huérfano —
    restaurarlo pisaría un run en vuelo."""
    backup = _backup_move_aside(mods, "DynDOLOD Output", contenido="LODs previos")
    await lock_manager.acquire_lock("dyndolod-pipeline", "otra-instancia", ttl=60.0)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert backup.exists()
    assert not (mods / "DynDOLOD Output").exists()
    assert "dyndolod" in resultado.omitidos_por_lock


# ---------------------------------------------------------------------------
# Familia clon-sandbox — el marcador durable es el rollback- del promote
# ---------------------------------------------------------------------------


async def test_preserva_el_clon_cuyo_promote_quedo_a_mitad(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Un ``rollback-*`` dentro del clon significa que ``_apply_changes`` llegó a
    la fase de backup y no llegó a limpiarlo: el perfil real puede estar
    medio-aplicado y ese directorio es la ÚNICA ruta de recuperación manual (el
    mensaje de ``SandboxRollbackError`` apunta ahí). Es el caso donde un GC ciego
    hace daño real."""
    clon = _clon_sandbox(sandbox, con_backup_de_promote=True)
    _envejecer(clon)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert clon.exists()
    assert (clon / "rollback-deadbeef" / "plugins.txt").exists()
    assert resultado.preservados == (clon / "rollback-deadbeef",)
    assert resultado.descartados == ()


async def test_descarta_el_clon_huerfano_sin_promote_pendiente(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Sin ``rollback-*`` nada se promovió: el clon es una copia del árbol real más
    la salida de un ritual que ya nadie va a aprobar. Puro leak de disco (varios GB
    por corrida) — el piso de GC que pide U-08."""
    clon = _clon_sandbox(sandbox, con_backup_de_promote=False)
    _envejecer(clon)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert not clon.exists()
    assert resultado.descartados == (clon,)


async def test_respeta_la_ventana_de_gracia_de_la_aprobacion_hitl(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """El clon sobrevive al ritual a propósito: espera la decisión HITL del
    operador, y en esa ventana NINGÚN lock lo protege (el del ritual ya se
    liberó). Un clon recién creado por otra instancia no es huérfano."""
    clon = _clon_sandbox(sandbox, con_backup_de_promote=False)  # mtime = ahora

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert clon.exists()
    assert resultado.descartados == ()


async def test_ignora_lo_que_no_parece_un_clon(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """El GC solo toca dirs con la forma que produce ``ProfileSandbox.clone()``
    (``<perfil>-<12 hex>``). Cualquier otra cosa que el operador haya dejado ahí
    queda intacta — un reconciliador no es un ``rm -rf`` del directorio."""
    ajeno = sandbox / "notas-del-operador"
    ajeno.mkdir()
    (ajeno / "leeme.txt").write_text("no borrar", encoding="utf-8")
    _envejecer(ajeno)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert (ajeno / "leeme.txt").exists()
    assert resultado.descartados == ()


# ---------------------------------------------------------------------------
# Propiedades transversales
# ---------------------------------------------------------------------------


async def test_no_toca_un_directorio_ajeno_que_se_parece_a_un_backup(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Aunque se declara un target exacto bajo el JUEGO, el sufijo también debe ser
    específico. ``DirectoryRollback`` usa
    ``time.time_ns()`` (19 dígitos); cualquier cosa con un sufijo corto es del
    operador y no se toca."""
    game = tmp_path / "game"
    game.mkdir()
    ajeno = game / "MisCosas.rollback-1"
    ajeno.mkdir()
    (ajeno / "nota.txt").write_text("no borrar", encoding="utf-8")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id="behavior-graphs",
                destinos=(game / "MisCosas",),
            )
        ],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    assert (ajeno / "nota.txt").exists()
    assert not (game / "MisCosas").exists()
    assert resultado.restaurados == ()


async def test_es_idempotente(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Correrlo dos veces deja el mismo estado: es un hook de arranque y el
    arranque se repite. La segunda pasada no debe encontrar nada que hacer sobre
    lo ya reconciliado (ni volver a restaurar sobre un target ya restaurado)."""
    _backup_move_aside(mods, "DynDOLOD Output", contenido="LODs previos")
    _envejecer(_clon_sandbox(sandbox, con_backup_de_promote=False))

    primera = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )
    segunda = await reconcile_orphan_rollback_backups(
        productores=[_dyndolod(mods)], sandbox_root=sandbox, lock_manager=lock_manager
    )

    assert primera.restaurados and primera.descartados
    assert segunda.restaurados == ()
    assert segunda.descartados == ()
    assert (mods / "DynDOLOD Output" / "DynDOLOD.esp").read_text(encoding="utf-8") == "LODs previos"


async def test_sin_raices_configuradas_no_explota(
    lock_manager: DistributedLockManager,
) -> None:
    """En una instalación sin MO2 resoluble el hook debe ser un no-op silencioso,
    no un error que tiña el arranque."""
    resultado = await reconcile_orphan_rollback_backups(productores=[], sandbox_root=None, lock_manager=lock_manager)

    assert resultado.restaurados == ()
    assert resultado.descartados == ()
    assert resultado.preservados == ()


def _envejecer(directorio: pathlib.Path) -> None:
    """Envejece el mtime más allá de la ventana de gracia (sin dormir el test)."""
    import os

    viejo = directorio.stat().st_mtime - VENTANA_DE_GRACIA_SEGUNDOS - 60
    os.utime(directorio, (viejo, viejo))


# ---------------------------------------------------------------------------
# Enlaces: un enlace con nombre de backup no es un backup
# ---------------------------------------------------------------------------


@symlink_guard
def test_un_enlace_con_nombre_de_backup_no_se_toma_como_backup(tmp_path: pathlib.Path) -> None:
    """El hermano de ``DirectoryRollback``: acá tampoco "existe" es "es un backup".

    ``_listar_backups_move_aside`` filtraba con ``is_dir()``, que **sigue** el
    enlace: un symlink (o junction) a directorio llamado ``X.rollback-<nonce>``
    entraba como backup legítimo y después ``backup.rename(destino)`` lo movía
    encima del target real. El resultado es doblemente malo — el target queda
    siendo un enlace a un árbol ajeno, y el reconciliador lo reporta como
    "restaurado desde el último estado bueno".

    Este módulo es el que **no tiene** el fail-closed de ``__aenter__``: opera
    sobre lo que encuentra en disco al arrancar, así que es el único camino por el
    que un artefacto enlazado llega a una operación destructiva. Dejarlo afuera del
    fix de ``_dir_rollback`` habría sido arreglar un hermano y no al otro.
    """
    raiz = tmp_path / "juego"
    raiz.mkdir()
    ajeno = tmp_path / "arbol_ajeno"
    ajeno.mkdir()
    (ajeno / "importante.txt").write_text("no soy un backup", encoding="utf-8")

    impostor = raiz / "Pandora_Output.rollback-1782000000000000000"
    impostor.symlink_to(ajeno, target_is_directory=True)

    assert impostor.is_dir(), "is_dir() sigue el enlace — el motivo del bug"
    assert _listar_backups_move_aside([raiz / "Pandora_Output"]) == []


@symlink_guard
def test_un_backup_real_si_se_lista(tmp_path: pathlib.Path) -> None:
    """Hermano del anterior: el filtro nuevo no puede volverse ciego a los backups
    de verdad. Sin este par, "no listar nada nunca" pasaría el test de arriba."""
    raiz = tmp_path / "juego"
    raiz.mkdir()
    real = raiz / "Pandora_Output.rollback-1782000000000000000"
    real.mkdir()
    (real / "previo.hkx").write_text("estado previo", encoding="utf-8")

    assert _listar_backups_move_aside([raiz / "Pandora_Output"]) == [real]
