"""Post-push fix 0009 (PR #493): orden del arranque y evidencia física.

Finding A (Major, CodeRabbit): el comentario D2 de ``app_context`` afirma

    journal sweep → deployment handoff reconciliation → rollback reconciler

pero el código ejecutaba ``reconcile_orphan_rollback_backups`` ANTES de
``journal.open()`` y de ``reconciliar_handoffs_de_deployment``. El rollback
reconciler restaura/desplaza evidencia física entre los destinos DynDOLOD —
``mods/TexGen Output``, ``mods/DynDOLOD Output`` y
``<raíz administrada>/textures`` —, y ``mods/TexGen Output`` es exactamente el
recurso que el oracle del handoff inspecciona para decidir.

La reproducción (A2) demuestra la diferencia semántica: con una muerte dura
ENTRE el move-aside y la regeneración (target ausente + backup ``.rollback-*``
con la generación previa) y evidencia durable de la corrida interrumpida
(receipt UNRESOLVED del stale sweep), ejecutar el reconciler PRIMERO restaura
la generación previa y el oracle —que mira DESPUÉS— ve el artifact PRESENTE y
fabrica un INDETERMINATE espurio con la identidad observada de la generación
VIEJA, consumiendo los receipts sobre ese estado contaminado. El orden
requerido decide sobre el estado PREVIO a la restauración (ausencia demostrada
→ NO_ARTIFACT + legacy) y recién después el reconciler devuelve la generación
previa — convergiendo con lo que el camino de cancelación cooperativa ya
produce (rollback → TX ROLLED_BACK sin receipt → legacy).

Estos tests congelan el orden por posición en el AST (A1) y por comportamiento
(A2), de modo que mover el bloque vuelva a ser un cambio visible.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.app.db.handoffs import (
    HandoffState,
    SweepReceiptState,
    clave_de_artifact,
)
from sky_claw.app.db.journal import (
    JournalConnectionError,
    OperationJournal,
)
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner
from tests.test_dyndolod_handoff_durable import _escribir_mod, _sembrar_pending_vieja

_RAIZ = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"

# =============================================================================
# A1 — ORDEN ANCLA (wiring por AST, no por presencia de llamadas)
# =============================================================================


def _posiciones_de_llamadas_de_startup() -> dict[str, int]:
    """Posición real (línea) de cada llamada del arranque en ``_start_full_inner``."""
    fuente = (_RAIZ / "app_context.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    funcion = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_start_full_inner"
    )
    posiciones: dict[str, int] = {}
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            if nodo.func.id in ("reconciliar_handoffs_de_deployment", "reconcile_orphan_rollback_backups"):
                posiciones.setdefault(nodo.func.id, nodo.lineno)
        elif (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "open"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == "journal"
        ):
            posiciones.setdefault("journal.open", nodo.lineno)
    return posiciones


def test_orden_de_reconciliacion_del_arranque_congelado() -> None:
    """A1 (0009): ``journal.open`` → ``reconciliar_handoffs_de_deployment`` →
    ``reconcile_orphan_rollback_backups``, por posición real en el AST de
    ``_start_full_inner`` — la presencia de las tres llamadas no alcanza.

    RED contra el orden rollback-primero publicado: el reconciler restaura la
    generación previa y el oracle del handoff decide sobre evidencia física ya
    contaminada.
    """
    posiciones = _posiciones_de_llamadas_de_startup()
    assert set(posiciones) == {
        "journal.open",
        "reconciliar_handoffs_de_deployment",
        "reconcile_orphan_rollback_backups",
    }, f"faltan llamadas del arranque: {sorted(posiciones)}"
    assert (
        posiciones["journal.open"]
        < posiciones["reconciliar_handoffs_de_deployment"]
        < posiciones["reconcile_orphan_rollback_backups"]
    ), (
        "orden roto: el oracle del handoff debe decidir ANTES de que el rollback "
        "reconciler restaure la evidencia física que inspecciona"
    )


# =============================================================================
# FAILURE ORDERING — la estructura best-effort sobrevive al movimiento
# =============================================================================


def _nodo_de_llamada(funcion: ast.AsyncFunctionDef, nombre: str) -> ast.AST:
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre:
            return nodo
        if (
            nombre == "journal.open"
            and isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "open"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == "journal"
        ):
            return nodo
    raise AssertionError(f"llamada no encontrada en el arranque: {nombre}")


def _tries_ancestros(funcion: ast.AsyncFunctionDef, nodo: ast.AST) -> list[ast.Try]:
    """Los ``try`` de ``_start_full_inner`` que contienen a ``nodo`` (cercano primero)."""
    padres: dict[ast.AST, ast.AST] = {}
    for ancestro in ast.walk(funcion):
        for hijo in ast.iter_child_nodes(ancestro):
            padres[hijo] = ancestro
    tries: list[ast.Try] = []
    actual: ast.AST = nodo
    while actual in padres:
        actual = padres[actual]
        if isinstance(actual, ast.Try):
            tries.append(actual)
        if isinstance(actual, (ast.FunctionDef, ast.AsyncFunctionDef)):
            break
    return tries


def test_estructura_best_effort_del_arranque() -> None:
    """FAILURE ORDERING (0009): mover el bloque no convierte un hook
    best-effort en fatal, ni acopla los fallos de las dos reconciliaciones.

    - ``journal.open()`` no tiene guard propio: todo try que lo contiene es
      común a los hooks (el wrapper de la función, que re-lanza). Un try que
      envuelva SOLO al open absorbería el fallo fatal del journal;
    - cada reconciliación conserva SU try/except best-effort, y ninguno
      contiene al otro: un fallo del handoff no saltea el rollback reconciler
      ni viceversa.
    """
    fuente = (_RAIZ / "app_context.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    funcion = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_start_full_inner"
    )
    llamada_open = _nodo_de_llamada(funcion, "journal.open")
    llamada_handoff = _nodo_de_llamada(funcion, "reconciliar_handoffs_de_deployment")
    llamada_rollback = _nodo_de_llamada(funcion, "reconcile_orphan_rollback_backups")

    tries_open = _tries_ancestros(funcion, llamada_open)
    tries_handoff = _tries_ancestros(funcion, llamada_handoff)
    tries_rollback = _tries_ancestros(funcion, llamada_rollback)

    for t in tries_open:
        assert llamada_handoff in ast.walk(t) and llamada_rollback in ast.walk(t), (
            "journal.open quedó dentro de un try que no comparte con los hooks: "
            "un fallo del journal debe propagar, no absorberse"
        )
    # El guard PROPIO de cada hook es el try que NO contiene al open (el
    # wrapper común de la función re-lanza y no absorbe nada).
    guard_handoff = [t for t in tries_handoff if llamada_open not in ast.walk(t)]
    guard_rollback = [t for t in tries_rollback if llamada_open not in ast.walk(t)]
    assert guard_handoff, "la reconciliación del handoff perdió su guard best-effort"
    assert guard_rollback, "la reconciliación del rollback perdió su guard best-effort"
    assert not any(llamada_rollback in ast.walk(t) for t in guard_handoff), (
        "el rollback reconciler quedó DENTRO del try del handoff: un fallo del handoff lo saltearía"
    )
    assert not any(llamada_handoff in ast.walk(t) for t in guard_rollback), (
        "la reconciliación del handoff quedó DENTRO del try del rollback"
    )


# =============================================================================
# A2 — EVIDENCIA FÍSICA (reproducción conductual)
# =============================================================================

_ORDEN_CABLEADO: tuple[str, ...] = (
    "journal.open",
    "reconciliar_handoffs_de_deployment",
    "reconcile_orphan_rollback_backups",
)


@dataclass(frozen=True, slots=True)
class _Escenario:
    """Muerte dura ENTRE el move-aside y la regeneración de ``mods/TexGen Output``."""

    mo2: pathlib.Path
    mods: pathlib.Path
    mod_texgen: pathlib.Path
    game: pathlib.Path
    data: pathlib.Path
    db_path: pathlib.Path
    locks_db: pathlib.Path
    tx: int


async def _escenario_de_muerte_dura(tmp_path: pathlib.Path, *, horas: int = 48) -> _Escenario:
    """Residuo ``.rollback-*`` de TexGen (única copia de la generación previa),
    target ausente, y TX PENDING envejecida que nombra el artifact: al abrir el
    journal, el stale sweep la convierte y deja su receipt UNRESOLVED."""
    mo2 = tmp_path / "MO2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True)
    mod_texgen = mods / DynDOLODRunner.TEXGEN_MOD_NAME
    game = tmp_path / "game"
    game.mkdir()
    data = game / "Data"
    data.mkdir()

    backup = mods / f"{DynDOLODRunner.TEXGEN_MOD_NAME}.rollback-1753700000000000000"
    _escribir_mod(backup, contenido=b"GEN-VIEJA")

    db_path = tmp_path / "journal.db"
    j1 = OperationJournal(db_path)
    await j1.open()
    tx = await _sembrar_pending_vieja(j1, mod_texgen, horas=horas)
    await j1.close()

    return _Escenario(
        mo2=mo2,
        mods=mods,
        mod_texgen=mod_texgen,
        game=game,
        data=data,
        db_path=db_path,
        locks_db=tmp_path / "locks.db",
        tx=tx,
    )


async def _correr_secuencia(
    escenario: _Escenario,
    orden: tuple[str, ...],
) -> tuple[str | None, str | None, bool]:
    """Ejecuta los pasos del arranque en el orden dado y reporta la decisión
    durable del oracle: estado del handoff activo, estado del receipt y si el
    artifact quedó restaurado con la generación previa."""
    from sky_claw.app.db.handoffs import reconciliar_handoffs_de_deployment
    from sky_claw.local.tools.artifact_digest import digest_arbol
    from sky_claw.local.tools.rollback_reconciler import (
        construir_productores_de_move_aside,
        reconcile_orphan_rollback_backups,
    )

    locks = DistributedLockManager(
        escenario.locks_db, default_ttl=30.0, max_retries=2, backoff_base=0.05, backoff_max=0.2
    )
    await locks.initialize()
    journal = OperationJournal(escenario.db_path)
    try:
        for paso in orden:
            if paso == "journal.open":
                await journal.open()
            elif paso == "reconciliar_handoffs_de_deployment":
                await reconciliar_handoffs_de_deployment(
                    journal=journal,
                    mod_texgen=escenario.mod_texgen,
                    game_key=clave_de_artifact(escenario.game),
                    mods_root_key=clave_de_artifact(escenario.mods),
                    data_key=clave_de_artifact(escenario.data),
                    expected_profile="Perfil-A",
                    digest_arbol=digest_arbol,
                )
            elif paso == "reconcile_orphan_rollback_backups":
                await reconcile_orphan_rollback_backups(
                    productores=construir_productores_de_move_aside(
                        mo2_root=escenario.mo2,
                        game=escenario.game,
                    ),
                    sandbox_root=escenario.mo2 / ".skyclaw_sandbox",
                    lock_manager=locks,
                )
            else:  # pragma: no cover — el orden lo fijan las constantes de arriba
                raise AssertionError(f"paso desconocido: {paso}")

        activo = await journal.consultar_handoff_activo(clave_de_artifact(escenario.mod_texgen))
        estado_handoff = activo.state.value if activo is not None else None
        cur = await journal._db.execute(  # noqa: SLF001
            "SELECT state FROM stale_pending_sweep_receipts WHERE transaction_id = ?",
            (escenario.tx,),
        )
        fila = await cur.fetchone()
        estado_receipt = None if fila is None else str(fila[0])
        restaurado = (
            escenario.mod_texgen.is_dir() and (escenario.mod_texgen / "textures" / "a.dds").read_bytes() == b"GEN-VIEJA"
        )
        return estado_handoff, estado_receipt, restaurado
    finally:
        await journal.close()
        await locks.close()


async def test_ejecutar_rollback_primero_cambia_la_decision_del_oracle(tmp_path: pathlib.Path) -> None:
    """A2 (0009): la reproducción física — ejecutar el rollback reconciler
    PRIMERO cambia la decisión que después toma ``reconciliar_handoffs_de_deployment``.

    Rollback primero (orden publicado): restaura la generación previa, el
    oracle ve el artifact PRESENTE y fabrica un INDETERMINATE espurio con la
    identidad observada de la generación VIEJA; los receipts se CONSUMEN sobre
    ese estado contaminado (pérdida de recovery evidence).

    Handoff primero (orden requerido): ausencia demostrada → receipt cerrado
    NO_ARTIFACT + legacy; el reconciler restaura después la generación previa
    sin que la evidencia de la corrida interrumpida se atribuya a ella.
    """
    escenario_actual = await _escenario_de_muerte_dura(tmp_path / "actual")
    decision_actual = await _correr_secuencia(
        escenario_actual,
        ("reconcile_orphan_rollback_backups", "journal.open", "reconciliar_handoffs_de_deployment"),
    )

    escenario_requerido = await _escenario_de_muerte_dura(tmp_path / "requerido")
    decision_requerida = await _correr_secuencia(escenario_requerido, _ORDEN_CABLEADO)

    assert decision_actual != decision_requerida, "la reproducción no mostró diferencia semántica entre los dos órdenes"
    assert decision_actual[0] == HandoffState.INDETERMINATE.value, (
        "el orden rollback-primero no produjo el INDETERMINATE espurio esperado"
    )
    assert decision_actual[1] == SweepReceiptState.CONSUMED.value, (
        "el receipt no se consumió sobre el estado contaminado"
    )
    assert decision_actual[2] is True

    assert decision_requerida[0] is None, "el orden requerido debe terminar en legacy (sin handoff activo)"
    assert decision_requerida[1] == SweepReceiptState.NO_ARTIFACT.value
    assert decision_requerida[2] is True


async def test_la_secuencia_cableada_del_arranque_produce_la_semantica_correcta(
    tmp_path: pathlib.Path,
) -> None:
    """A2-regresión: la secuencia REAL cableada en ``app_context`` (derivada de
    las posiciones del AST) debe terminar en legacy + NO_ARTIFACT + artifact
    restaurado — no en un INDETERMINATE espurio. RED contra el orden
    rollback-primero, GREEN con el orden congelado por A1."""
    posiciones = _posiciones_de_llamadas_de_startup()
    orden_cableado = tuple(sorted(_ORDEN_CABLEADO, key=lambda nombre: posiciones[nombre]))
    escenario = await _escenario_de_muerte_dura(tmp_path)
    estado_handoff, estado_receipt, restaurado = await _correr_secuencia(escenario, orden_cableado)

    assert estado_handoff is None, "la secuencia cableada fabricó un INDETERMINATE sobre la generación restaurada"
    assert estado_receipt == SweepReceiptState.NO_ARTIFACT.value, "el receipt no quedó cerrado como ausencia demostrada"
    assert restaurado is True, "el reconciler no restauró la generación previa"


# =============================================================================
# FAILURE ORDERING — comportamiento best-effort de la secuencia
# =============================================================================


async def test_fallo_del_journal_propaga_journal_connection_error(tmp_path: pathlib.Path) -> None:
    """FAILURE A: si ``journal.open()`` falla, la excepción es la primaria del
    startup (``JournalConnectionError``): en ``app_context`` esa llamada NO vive
    dentro de un try (ancla estructural de arriba), así que el arranque se
    revierte sin correr las reconciliaciones."""
    escenario = await _escenario_de_muerte_dura(tmp_path)
    # El path del journal queda ocupado por un directorio: open() falla de verdad.
    escenario.db_path.unlink()
    escenario.db_path.mkdir()
    journal = OperationJournal(escenario.db_path)
    with pytest.raises(JournalConnectionError):
        await journal.open()


async def test_fallo_del_handoff_no_impide_el_rollback_reconciler(tmp_path: pathlib.Path) -> None:
    """FAILURE B: un fallo de la reconciliación del handoff no impide que el
    rollback reconciler corra después (guard best-effort independiente). Se
    verifica sobre la estructura real: la llamada al rollback reconciler NO
    vive dentro del try del handoff (ver ancla estructural) y, ejecutada la
    secuencia con el handoff fallando, el reconciler restaura igual."""
    escenario = await _escenario_de_muerte_dura(tmp_path)

    from sky_claw.app.db.handoffs import reconciliar_handoffs_de_deployment
    from sky_claw.local.tools.artifact_digest import digest_arbol
    from sky_claw.local.tools.rollback_reconciler import (
        construir_productores_de_move_aside,
        reconcile_orphan_rollback_backups,
    )

    locks = DistributedLockManager(
        escenario.locks_db, default_ttl=30.0, max_retries=2, backoff_base=0.05, backoff_max=0.2
    )
    await locks.initialize()
    journal = OperationJournal(escenario.db_path)
    try:
        await journal.open()

        real = journal.transacciones_que_nombran

        async def _boom(_objetivo: str) -> list[int]:
            raise RuntimeError("reconciler roto")

        journal.transacciones_que_nombran = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="reconciler roto"):
            await reconciliar_handoffs_de_deployment(
                journal=journal,
                mod_texgen=escenario.mod_texgen,
                game_key=clave_de_artifact(escenario.game),
                mods_root_key=clave_de_artifact(escenario.mods),
                data_key=clave_de_artifact(escenario.data),
                expected_profile="Perfil-A",
                digest_arbol=digest_arbol,
            )
        journal.transacciones_que_nombran = real  # type: ignore[method-assign]

        # El hook del handoff falló; el rollback reconciler SIGUE ejecutándose.
        resultado = await reconcile_orphan_rollback_backups(
            productores=construir_productores_de_move_aside(mo2_root=escenario.mo2, game=escenario.game),
            sandbox_root=escenario.mo2 / ".skyclaw_sandbox",
            lock_manager=locks,
        )
        assert escenario.mod_texgen in resultado.restaurados
    finally:
        await journal.close()
        await locks.close()


async def test_fallo_del_rollback_reconciler_no_aborta_el_contrato_del_handoff(
    tmp_path: pathlib.Path,
) -> None:
    """FAILURE C: un fallo del rollback reconciler no revierte la decisión
    durable ya tomada por el handoff (NO_ARTIFACT + legacy): el residuo queda
    para el próximo arranque y el receipt ya cerrado no se reabre."""
    escenario = await _escenario_de_muerte_dura(tmp_path)

    from sky_claw.app.db.handoffs import reconciliar_handoffs_de_deployment
    from sky_claw.local.tools.artifact_digest import digest_arbol
    from sky_claw.local.tools.rollback_reconciler import (
        construir_productores_de_move_aside,
        reconcile_orphan_rollback_backups,
    )

    locks = DistributedLockManager(
        escenario.locks_db, default_ttl=30.0, max_retries=2, backoff_base=0.05, backoff_max=0.2
    )
    await locks.initialize()
    journal = OperationJournal(escenario.db_path)
    try:
        await journal.open()
        await reconciliar_handoffs_de_deployment(
            journal=journal,
            mod_texgen=escenario.mod_texgen,
            game_key=clave_de_artifact(escenario.game),
            mods_root_key=clave_de_artifact(escenario.mods),
            data_key=clave_de_artifact(escenario.data),
            expected_profile="Perfil-A",
            digest_arbol=digest_arbol,
        )

        with (
            patch(
                "sky_claw.local.tools.rollback_reconciler._reconciliar_move_aside",
                new=AsyncMock(side_effect=RuntimeError("rollback reconciler roto")),
            ),
            pytest.raises(RuntimeError, match="rollback reconciler roto"),
        ):
            await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(mo2_root=escenario.mo2, game=escenario.game),
                sandbox_root=escenario.mo2 / ".skyclaw_sandbox",
                lock_manager=locks,
            )

        # La decisión durable del handoff permanece: legacy + NO_ARTIFACT.
        assert await journal.consultar_handoff_activo(clave_de_artifact(escenario.mod_texgen)) is None
        cur = await journal._db.execute(  # noqa: SLF001
            "SELECT state FROM stale_pending_sweep_receipts WHERE transaction_id = ?",
            (escenario.tx,),
        )
        fila = await cur.fetchone()
        assert fila is not None and fila[0] == SweepReceiptState.NO_ARTIFACT.value
        # El residuo sigue intacto para el próximo arranque.
        assert not escenario.mod_texgen.exists()
    finally:
        await journal.close()
        await locks.close()
