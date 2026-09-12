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
from sky_claw.local.tools.output_targets import (
    BODYSLIDE_MESHES_RESOURCE_ID,
    bodyslide_output_root,
    derivar_layout_de_dyndolod,
    dyndolod_legacy_recovery_target,
    pandora_output_target,
)
from sky_claw.local.tools.pandora_service import BEHAVIOR_GRAPHS_RESOURCE_ID
from sky_claw.local.tools.rollback_reconciler import (
    PRODUCTOR_DYNDOLOD,
    PRODUCTOR_LEGACY,
    PRODUCTORES_CABLEADOS,
    VENTANA_DE_GRACIA_SEGUNDOS,
    ProductorDeMoveAside,
    _listar_backups_move_aside,
    construir_productores_de_move_aside,
    reconcile_orphan_rollback_backups,
)
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard

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
    "sky_claw/app/agent/tools/system_tools.py": "bodyslide",
}

#: Excluidos con motivo: **consumen** el prefijo, no lo producen. El
#: reconciliador es quien barre el residuo; `dyndolod_workspace` lo LEE para
#: decidir si un `external_work_root` viejo está quiescente antes de dejar de
#: usarlo (P0 de ADR 0011), y lo hace con la MISMA
#: `rollback_reconciler.SUFIJO_MOVE_ASIDE` —no con una regex propia—, que es
#: justamente lo que lo mantiene del lado de los consumidores. Si algún día
#: creara backups, tendría que pasar a `PRODUCTORES_DEL_NOMBRE` con su familia.
_CONSUMIDORES = {
    "sky_claw/local/tools/rollback_reconciler.py",
    "sky_claw/local/tools/dyndolod_workspace.py",
}


def _modulos_con_el_nombre(literal: str) -> set[str]:
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if literal in ruta.read_text(encoding="utf-8")
    }


def test_todo_productor_de_backups_tiene_su_familia_reconciliada() -> None:
    """Guard de completitud: un productor nuevo de ``rollback-*`` obliga a decidir
    cómo se reconcilia su residuo, en vez de dejarlo leakear en silencio."""
    assert _modulos_con_el_nombre("rollback-") - _CONSUMIDORES == set(PRODUCTORES_DEL_NOMBRE)


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


# ---------------------------------------------------------------------------
# BodySlide — raíz EXCLUSIVA, destinos descubiertos por escaneo (no declarados)
# ---------------------------------------------------------------------------
#
# A diferencia de DynDOLOD/Pandora, el nombre del hijo (``group``) lo elige el
# LLM en cada corrida: no hay una lista fija que declarar de antemano. Pero
# ``BodySlide_Output`` es un namespace EXCLUSIVO de Sky-Claw (nada más escribe
# ahí), así que el constructor puede descubrir los destinos escaneando sus
# hijos directos con el sufijo de move-aside, sin el riesgo de "autoridad
# sobre siblings ajenos" que el docstring de ``ProductorDeMoveAside`` nombra
# para un padre COMPARTIDO como ``mods/`` o ``game/``.


async def test_bodyslide_restaura_su_backup_huerfano(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    sandbox: pathlib.Path,
) -> None:
    """Muerte dura entre el move-aside del subárbol de un grupo y su regeneración:
    el backup es el último estado bueno de ESE grupo, igual que DynDOLOD/Pandora."""
    game = tmp_path / "game"
    root = game / "BodySlide_Output"
    root.mkdir(parents=True)
    backup = root / "CBBE.rollback-1753700000000000000"
    backup.mkdir()
    (backup / "vieja.nif").write_text("malla previa", encoding="utf-8")

    resultado = await reconcile_orphan_rollback_backups(
        productores=construir_productores_de_move_aside(mo2_root=None, game=game),
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    destino = root / "CBBE"
    assert destino.is_dir()
    assert (destino / "vieja.nif").read_text(encoding="utf-8") == "malla previa"
    assert not backup.exists()
    assert resultado.restaurados == (destino,)


def test_el_constructor_descubre_los_destinos_de_bodyslide_por_escaneo(tmp_path: pathlib.Path) -> None:
    """El constructor no declara nombres de grupo a mano: los descubre de los
    backups ``.rollback-*`` que YA existen bajo la raíz exclusiva. Dos grupos
    huérfanos a la vez no se pisan entre sí — cada uno es su propio destino."""
    game = tmp_path / "game"
    root = game / "BodySlide_Output"
    root.mkdir(parents=True)
    (root / "CBBE.rollback-1753700000000000000").mkdir()
    (root / "3BA.rollback-1753700000000000001").mkdir()

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    bodyslide = next(p for p in productores if p.nombre == "bodyslide")
    assert bodyslide.lock_resource_id == BODYSLIDE_MESHES_RESOURCE_ID
    assert set(bodyslide.destinos) == {root / "CBBE", root / "3BA"}
    assert bodyslide_output_root(game=game) == root


def test_el_constructor_de_bodyslide_ignora_un_backup_con_basename_vacio(tmp_path: pathlib.Path) -> None:
    """Un hijo cuyo nombre completo ES el sufijo de move-aside (``.rollback-<12+
    dígitos>``, sin nada antes del punto) deja basename vacío tras quitarle el
    sufijo. ``pathlib.Path.with_name("")`` levanta ``ValueError`` — sin este
    guard, un solo directorio con ese nombre tira abajo el constructor entero
    (Copilot, PR #430), no solo el descubrimiento de BodySlide: los demás
    productores (DynDOLOD, Pandora) que arma la misma función nunca llegan a
    declararse."""
    game = tmp_path / "game"
    root = game / "BodySlide_Output"
    root.mkdir(parents=True)
    (root / ".rollback-123456789012").mkdir()

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    bodyslide = next(p for p in productores if p.nombre == "bodyslide")
    assert bodyslide.destinos == ()


def test_el_constructor_de_bodyslide_ignora_un_hijo_sin_sufijo_de_rollback(tmp_path: pathlib.Path) -> None:
    """Un grupo YA regenerado (sin backup pendiente) no es un destino: el escaneo
    solo reconoce hijos con el sufijo ``.rollback-<nonce>``, nunca por nombre."""
    game = tmp_path / "game"
    root = game / "BodySlide_Output"
    (root / "CBBE").mkdir(parents=True)  # grupo normal, ya commiteado — sin backup

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    bodyslide = next(p for p in productores if p.nombre == "bodyslide")
    assert bodyslide.destinos == ()


def test_el_constructor_de_bodyslide_sin_raiz_en_disco_no_declara_destinos(tmp_path: pathlib.Path) -> None:
    """``BodySlide_Output`` todavía no existe (ningún run corrió): el productor
    se declara igual (mismo criterio que Pandora, "resoluble" no es "presente"),
    pero sin destinos que barrer."""
    game = tmp_path / "game"

    productores = construir_productores_de_move_aside(mo2_root=None, game=game)

    bodyslide = next(p for p in productores if p.nombre == "bodyslide")
    assert bodyslide.destinos == ()


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


def test_el_staging_de_texgen_legacy_esta_declarado_como_recovery_only(tmp_path: pathlib.Path) -> None:
    """El destino EXACTO del staging legacy, en su productor RECOVERY-ONLY (P2.3).

    El ancla de familia de más arriba se afirma sobre MÓDULOS, y
    ``dyndolod_service.py`` ya figuraba en ella: agregarle un
    ``DirectoryRollback`` sobre un destino nuevo no la pone roja. Ese es
    justamente el riesgo que su propio comentario nombra —"un **destino exacto**
    nuevo"— y el que este test cubre: tras una muerte dura, un
    ``textures.rollback-<nonce>`` bajo la raíz legacy es la ÚNICA copia del
    staging previo, y si el reconciliador no mira ahí queda huérfano para siempre.
    P2.3 lo separa en su propio productor con el MISMO lock: el barrido histórico
    restaura, nunca adopta ni migra, y la familia mutante no lo declara.
    """
    from sky_claw.local.tools.rollback_reconciler import PRODUCTOR_LEGACY

    mo2 = tmp_path / "mo2"
    game = tmp_path / "game"

    productores = construir_productores_de_move_aside(mo2_root=mo2, game=game)

    legacy = next(p for p in productores if p.nombre == PRODUCTOR_LEGACY)
    raiz = dyndolod_legacy_recovery_target(game=game)
    assert raiz is not None
    assert legacy.destinos == (raiz / DynDOLODRunner.TEXGEN_OUTPUT_NAME,)
    assert legacy.lock_resource_id == "dyndolod-pipeline"
    # El productor mutante NO declara el legacy.
    dyndolod = next(p for p in productores if p.nombre == "dyndolod")
    assert (raiz / DynDOLODRunner.TEXGEN_OUTPUT_NAME) not in dyndolod.destinos


def test_construir_productores_con_mods_dir_separado_ignora_trampas(tmp_path: pathlib.Path) -> None:
    """En layout install != data != mods, los destinos de DynDOLOD se ubican en mods_dir
    y nunca en <install>/mods ni <data>/mods (trampas)."""
    install = tmp_path / "MO2_Install"
    data = tmp_path / "MO2_Data"
    custom_mods = tmp_path / "Custom_Mods"
    game = tmp_path / "Skyrim"
    for p in (install, data, custom_mods, game):
        p.mkdir(parents=True, exist_ok=True)

    trap_install_mods = install / "mods"
    trap_data_mods = data / "mods"
    trap_install_mods.mkdir()
    trap_data_mods.mkdir()

    productores = construir_productores_de_move_aside(
        mo2_root=install,
        mods_dir=custom_mods,
        game=game,
    )
    dyndolod = next(p for p in productores if p.nombre == "dyndolod")
    dyndolod_destinos = set(dyndolod.destinos)

    # DynDOLOD Output y TexGen Output deben residir bajo custom_mods
    assert custom_mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME in dyndolod_destinos
    assert custom_mods / DynDOLODRunner.TEXGEN_MOD_NAME in dyndolod_destinos

    # Las trampas en install/mods y data/mods no deben ser alcanzadas
    assert trap_install_mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME not in dyndolod_destinos
    assert trap_data_mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME not in dyndolod_destinos
    assert trap_install_mods / DynDOLODRunner.TEXGEN_MOD_NAME not in dyndolod_destinos
    assert trap_data_mods / DynDOLODRunner.TEXGEN_MOD_NAME not in dyndolod_destinos


async def test_reconcile_sandbox_con_data_root_separado_descarta_clon(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """El sandbox huérfano reside bajo data_root/.skyclaw_sandbox y se descarta correctamente."""
    data = tmp_path / "MO2_Data"
    sandbox = data / ".skyclaw_sandbox"
    sandbox.mkdir(parents=True)
    clon = _clon_sandbox(sandbox, con_backup_de_promote=False)
    _envejecer(clon)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )
    assert not clon.exists()
    assert resultado.descartados == (clon,)


# =============================================================================
# P2.3 — recovery de arranque de los ACTIVE_TARGET externos
#
# Una muerte dura entre el move-aside del root exclusivo y su restauración deja
# `<family>/<Tool>.rollback-<nonce>`. Hasta P2.3 ese residuo no se barría: la
# única copia del staging previo quedaba huérfana. Los tests de acá ejercen el
# boundary real (`reconcile_orphan_rollback_backups`) contra los dos roots y
# afirman disco: restaurar sólo con el target ausente, preservar la ambigüedad
# cuando hay destino, y nunca tocar el árbol externo.
# =============================================================================


def _productor_dyndolod_externo(external: pathlib.Path) -> ProductorDeMoveAside:
    """Productor real de los ACTIVE_TARGET externos, tal cual lo arma el arranque."""
    productores = construir_productores_de_move_aside(external_work_roots=(external,))
    return next(p for p in productores if p.nombre == PRODUCTOR_DYNDOLOD)


def _backup_del_root(root: pathlib.Path, *, contenido: bytes = b"BYTES-PREVIOS") -> pathlib.Path:
    """Backup tal cual lo deja ``DirectoryRollback.__aenter__`` (rename O(1))."""
    backup = root.with_name(f"{root.name}.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "payload.dds").write_bytes(contenido)
    return backup


def test_los_targets_activos_y_el_legacy_estan_separados_por_rol(tmp_path: pathlib.Path) -> None:
    """T-PR2-14 (P2.3): igualdad literal de la familia de targets por rol.

    Los ACTIVE_TARGET —dos mods + dos roots crudos externos— son los únicos
    destinos mutables de los productores nuevos; el LEGACY_RECOVERY_ONLY_TARGET
    vive en su propio productor. Un target nuevo rompe acá hasta que se decida
    su rol.
    """
    mo2 = tmp_path / "mo2"
    game = tmp_path / "game"
    external = tmp_path / "Work Root"
    layout = derivar_layout_de_dyndolod(external_work_root=external)

    productores = construir_productores_de_move_aside(
        mo2_root=mo2,
        game=game,
        external_work_roots=(external,),
    )
    por_nombre = {p.nombre: set(p.destinos) for p in productores}

    assert por_nombre[PRODUCTOR_DYNDOLOD] == {
        mo2 / "mods" / DynDOLODRunner.DYNDOLLOD_MOD_NAME,
        mo2 / "mods" / DynDOLODRunner.TEXGEN_MOD_NAME,
        layout.texgen_root,
        layout.dyndolod_root,
    }
    legacy = dyndolod_legacy_recovery_target(game=game)
    assert legacy is not None
    assert por_nombre[PRODUCTOR_LEGACY] == {legacy / DynDOLODRunner.TEXGEN_OUTPUT_NAME}
    assert set(por_nombre) == {PRODUCTOR_DYNDOLOD, PRODUCTOR_LEGACY, "pandora", "bodyslide"}


@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
async def test_crash_tras_move_aside_del_root_externo_restaura(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    herramienta: str,
) -> None:
    """T-PR2-06: muerte dura ENTRE el move-aside y el mkdir → RESTORE byte-exacto."""
    external = tmp_path / "Work Root"
    layout = derivar_layout_de_dyndolod(external_work_root=external)
    root = layout.texgen_root if herramienta == "TexGen" else layout.dyndolod_root
    backup = _backup_del_root(root)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_productor_dyndolod_externo(external)],
        sandbox_root=None,
        lock_manager=lock_manager,
    )

    assert root.is_dir()
    assert (root / "payload.dds").read_bytes() == b"BYTES-PREVIOS"
    assert not backup.exists()
    assert resultado.restaurados == (root,)


@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
async def test_crash_tras_mkdir_del_root_externo_preserva_ambos(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    herramienta: str,
) -> None:
    """T-PR2-06: crash DESPUÉS del born-empty (target vacío) → PRESERVE BOTH.

    El filesystem no prueba si ese root vacío es de una corrida nueva
    interrumpida o del estado final de un commit: restaurar encima o borrar el
    backup destruiría trabajo en una de las dos hipótesis.
    """
    external = tmp_path / "Work Root"
    layout = derivar_layout_de_dyndolod(external_work_root=external)
    root = layout.texgen_root if herramienta == "TexGen" else layout.dyndolod_root
    backup = _backup_del_root(root)
    root.mkdir()

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_productor_dyndolod_externo(external)],
        sandbox_root=None,
        lock_manager=lock_manager,
    )

    assert root.is_dir() and not any(root.iterdir())
    assert backup.exists() and (backup / "payload.dds").read_bytes() == b"BYTES-PREVIOS"
    assert resultado.preservados == (backup,)
    assert resultado.restaurados == ()


@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
async def test_crash_tras_copia_parcial_del_root_externo_preserva_ambos(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    herramienta: str,
) -> None:
    """T-PR2-06: la herramienta alcanzó a escribir salida incompleta → ambigüedad."""
    external = tmp_path / "Work Root"
    layout = derivar_layout_de_dyndolod(external_work_root=external)
    root = layout.texgen_root if herramienta == "TexGen" else layout.dyndolod_root
    backup = _backup_del_root(root)
    root.mkdir()
    (root / "parcial.dds").write_bytes(b"A-MEDIAS")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_productor_dyndolod_externo(external)],
        sandbox_root=None,
        lock_manager=lock_manager,
    )

    assert (root / "parcial.dds").read_bytes() == b"A-MEDIAS"
    assert backup.exists()
    assert resultado.preservados == (backup,)


@pytest.mark.parametrize("mod_name", [DynDOLODRunner.DYNDOLLOD_MOD_NAME, DynDOLODRunner.TEXGEN_MOD_NAME])
async def test_crash_del_mod_empaquetado_sigue_cubierto(
    lock_manager: DistributedLockManager,
    mods: pathlib.Path,
    sandbox: pathlib.Path,
    mod_name: str,
) -> None:
    """Hermano en el mismo productor: los dos mods siguen declarados y se restauran."""
    backup = _backup_move_aside(mods, mod_name, contenido="texturas previas")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[_productor_dyndolod_externo_desde_mods(mods)],
        sandbox_root=sandbox,
        lock_manager=lock_manager,
    )

    destino = mods / mod_name
    assert (destino / "DynDOLOD.esp").read_text(encoding="utf-8") == "texturas previas"
    assert not backup.exists()
    assert resultado.restaurados == (destino,)


def _productor_dyndolod_externo_desde_mods(mods: pathlib.Path) -> ProductorDeMoveAside:
    productores = construir_productores_de_move_aside(mods_dir=mods)
    return next(p for p in productores if p.nombre == PRODUCTOR_DYNDOLOD)


# =============================================================================
# T-PR2-23 — familia completa A–H del recovery LEGACY (ADR 0011 §2.9)
#
# El predicado cerrado se ejerce sobre el ÚNICO LEGACY_RECOVERY_ONLY_TARGET,
# `<game>/Sky-Claw/DynDOLOD/textures`, vía `reconcile_orphan_rollback_backups`
# real y afirmando disco. El barrido histórico RESTAURA un backup válido; nunca
# adopta, migra, borra ni usa el legacy como staging productivo.
# =============================================================================


def _entorno_legacy(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, ProductorDeMoveAside]:
    """``(game, target, productor_legacy)`` con el productor real del arranque."""
    game = tmp_path / "game"
    game.mkdir()
    productores = construir_productores_de_move_aside(game=game)
    legacy = next(p for p in productores if p.nombre == PRODUCTOR_LEGACY)
    target = dyndolod_legacy_recovery_target(game=game)
    assert target is not None
    return game, target / DynDOLODRunner.TEXGEN_OUTPUT_NAME, legacy


async def test_tpr223_a_backup_legacy_valido_con_target_ausente_restaura(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso A: el backup es el último estado bueno y se devuelve con rename O(1)."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    backup = target.with_name(f"{target.name}.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "lod.dds").write_bytes(b"GENERACION-PREVIA")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert target.is_dir()
    assert (target / "lod.dds").read_bytes() == b"GENERACION-PREVIA"
    assert not backup.exists()
    assert resultado.restaurados == (target,)


async def test_tpr223_b_backup_legacy_valido_con_target_presente_preserva_ambos(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso B: estado ambiguo — no se sobrescribe ni se borra ninguno."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    backup = target.with_name(f"{target.name}.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "lod.dds").write_bytes(b"PREVIA")
    target.mkdir(parents=True)
    (target / "lod.dds").write_bytes(b"NUEVA")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert (target / "lod.dds").read_bytes() == b"NUEVA"
    assert (backup / "lod.dds").read_bytes() == b"PREVIA"
    assert resultado.preservados == (backup,)


async def test_tpr223_c_sibling_no_relacionado_se_ignora(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso C: sufijo válido pero basename ≠ ``textures`` → no es de este target."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    ajeno = target.with_name("DynDOLOD.rollback-1753700000000000000")
    ajeno.mkdir(parents=True)
    (ajeno / "x.dds").write_bytes(b"AJENO")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert ajeno.is_dir() and not (target.with_name("DynDOLOD")).exists()
    assert resultado.restaurados == () and resultado.preservados == ()


async def test_tpr223_d_sufijo_invalido_se_ignora(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso D: piso ``\\d{12,}`` — nonces cortos o no numéricos no son backups."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    corto = target.with_name(f"{target.name}.rollback-123")
    corto.mkdir(parents=True)
    no_numerico = target.with_name(f"{target.name}.rollback-abcdefghijkl")
    no_numerico.mkdir(parents=True)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert corto.is_dir() and no_numerico.is_dir()
    assert not target.exists()
    assert resultado.restaurados == () and resultado.preservados == ()


@junction_guard
async def test_tpr223_e_enlace_con_nombre_de_backup_se_ignora(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso E: un junction con nombre de backup NUNCA se restaura siguiendo el enlace."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    ajeno = tmp_path / "arbol_ajeno"
    ajeno.mkdir()
    (ajeno / "importante.dds").write_bytes(b"NO-SOY-BACKUP")
    enlace = target.with_name(f"{target.name}.rollback-1753700000000000000")
    enlace.parent.mkdir(parents=True, exist_ok=True)
    if (motivo := crear_junction(enlace, ajeno)) is not None:
        pytest.fail(f"no se pudo crear el junction: {motivo}")

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert enlace.is_dir()  # el junction sigue donde estaba
    assert not target.exists(), "el enlace no puede convertirse en el target administrado"
    assert (ajeno / "importante.dds").read_bytes() == b"NO-SOY-BACKUP"
    assert resultado.restaurados == () and resultado.preservados == ()


async def test_tpr223_f_lock_del_productor_vivo_salta_sin_mutar(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso F: ritual en curso (aun de otra instancia) ⇒ SKIP sin tocar el backup."""
    _game, target, legacy = _entorno_legacy(tmp_path)
    backup = target.with_name(f"{target.name}.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "lod.dds").write_bytes(b"PREVIA")
    await lock_manager.acquire_lock("dyndolod-pipeline", "otra-instancia", ttl=60.0)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert backup.is_dir()
    assert not target.exists()
    assert PRODUCTOR_LEGACY in resultado.omitidos_por_lock
    assert resultado.restaurados == ()


async def test_tpr223_g_sin_backup_valido_es_no_op(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Caso G: nada que reconciliar ⇒ no-op, ni siquiera crea el target."""
    _game, target, legacy = _entorno_legacy(tmp_path)

    resultado = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )

    assert not target.exists()
    assert resultado.restaurados == () and resultado.preservados == () and resultado.descartados == ()


async def test_tpr223_h_rename_fallido_preserva_el_backup_y_reintenta(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso H + idempotencia: si el rename falla, la única copia NO se pierde.

    Primera pasada con el rename roto: el backup queda y el target sigue ausente.
    Segunda pasada sana: restaura. Tercera: no-op (ya no hay backup).
    """
    _game, target, legacy = _entorno_legacy(tmp_path)
    backup = target.with_name(f"{target.name}.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "lod.dds").write_bytes(b"UNICA-COPIA")

    rename_real = pathlib.Path.rename

    def _rename_roto(self: pathlib.Path, destino: pathlib.Path) -> pathlib.Path:
        if self == backup:
            raise OSError("disco lleno (simulado)")
        return rename_real(self, destino)

    monkeypatch.setattr(pathlib.Path, "rename", _rename_roto)
    primera = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )
    monkeypatch.undo()

    assert backup.is_dir() and (backup / "lod.dds").read_bytes() == b"UNICA-COPIA"
    assert not target.exists()
    assert primera.preservados == (backup,)

    segunda = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )
    assert target.is_dir() and (target / "lod.dds").read_bytes() == b"UNICA-COPIA"
    assert segunda.restaurados == (target,)

    tercera = await reconcile_orphan_rollback_backups(
        productores=[legacy], sandbox_root=None, lock_manager=lock_manager
    )
    assert tercera.restaurados == () and tercera.preservados == () and tercera.descartados == ()


def test_ningun_productor_nuevo_declara_el_legacy(tmp_path: pathlib.Path) -> None:
    """El legacy sólo aparece en su productor recovery-only — nunca en los activos.

    Es la frontera normativa de ADR 0011 §2.9: ninguna corrida nueva usa, mueve,
    adopta, borra ni pasa el legacy como ``-o:``. Acá se ancla sobre la
    declaración REAL de los productores cableados.
    """
    mo2 = tmp_path / "mo2"
    game = tmp_path / "game"
    external = tmp_path / "Work Root"

    productores = construir_productores_de_move_aside(
        mo2_root=mo2,
        mods_dir=mo2 / "mods",
        game=game,
        external_work_roots=(external,),
    )
    legacy = dyndolod_legacy_recovery_target(game=game)
    assert legacy is not None
    declarantes = {
        p.nombre for p in productores if legacy in p.destinos or any(legacy in d.parents for d in p.destinos)
    }
    assert declarantes == {PRODUCTOR_LEGACY}


async def test_el_barrido_restaura_el_root_viejo_de_una_transicion_pendiente(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editar la preferencia no pierde los backups del root anterior (P2.3).

    End-to-end: el helper de AppContext lee el registro (activo B + `desde` A),
    el reconciliador recibe AMBAS raíces y restaura el backup huérfano que quedó
    en A. Sin incluir el `desde`, esa copia sería irrecuperable desde Sky-Claw.
    """
    from types import SimpleNamespace

    from sky_claw.app_context import AppContext
    from sky_claw.local.tools import dyndolod_workspace as wsm

    game = tmp_path / "game"
    game.mkdir()
    mo2_root = tmp_path / "mo2"
    (mo2_root / "mods").mkdir(parents=True)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game,
        mo2_instance_data_root=mo2_root,
        mo2_mods_path=mo2_root / "mods",
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    viejo = tmp_path / "Work A"
    nuevo = tmp_path / "Work B"
    registro.registrar_activa(clave=recursos.clave(), root=viejo, binding_id="binding-a")
    registro.registrar_transicion(clave=recursos.clave(), hacia=nuevo, motivo="cambio de preferencia")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    layout_viejo = derivar_layout_de_dyndolod(external_work_root=viejo)
    backup = layout_viejo.texgen_root.with_name("TexGen.rollback-1753700000000000000")
    backup.mkdir(parents=True)
    (backup / "lod.dds").write_bytes(b"PREVIO-EN-A")

    mo2 = SimpleNamespace(data_root=mo2_root, mods_dir=mo2_root / "mods")
    roots = AppContext._roots_externos_para_recovery(game=game, mo2=mo2)
    assert roots == (nuevo, viejo)

    resultado = await reconcile_orphan_rollback_backups(
        productores=construir_productores_de_move_aside(external_work_roots=roots),
        sandbox_root=None,
        lock_manager=lock_manager,
    )

    assert layout_viejo.texgen_root.is_dir()
    assert (layout_viejo.texgen_root / "lod.dds").read_bytes() == b"PREVIO-EN-A"
    assert not backup.exists()
    assert resultado.restaurados == (layout_viejo.texgen_root,)
