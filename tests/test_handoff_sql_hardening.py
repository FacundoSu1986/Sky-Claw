"""Post-push fix 0009 (PR #493): endurecimiento de SQL del handoff (Bandit B608).

Dos hallazgos de Bandit, dos tratamientos distintos:

- **B1 — handoffs.py, SELECT estático.** ``_SELECT_HANDOFF_ACTIVO_SQL``
  interpola ``_COLUMNAS_HANDOFF``, que es un literal cerrado del módulo (única
  asignación, materializado en import time, sin input de caller) y los datos de
  runtime siguen parametrizados con ``?``. Clasificación: falso positivo de
  identificador estático → ``# nosec B608`` localizado con rationale.

- **B2 — journal.py, UPDATE dinámico.** ``transicionar_handoff`` aceptaba
  cualquier keyword y la interpolaba como nombre de columna. Una llamada
  directa hostil podía formar SQL ejecutable (p. ej. hijackear
  ``artifact_path``). No se pone ``nosec`` sin cerrar el mecanismo: la
  construcción ahora resuelve CADA campo contra la allowlist cerrada
  ``_CAMPOS_TRANSICION_HANDOFF`` (``campo → fragmento SQL literal``) y rechaza
  cualquier clave ajena con ``JournalTransactionError`` ANTES de construir o
  ejecutar SQL. Recién con eso, el ``# nosec B608`` del statement dinámico es
  honesto: los fragmentos estructurales salen exclusivamente de la allowlist y
  los valores viajan parametrizados.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.app.db.handoffs import (
    HandoffState,
    clave_de_artifact,
)
from sky_claw.app.db.journal import (
    _CAMPOS_TRANSICION_HANDOFF,
    JournalTransactionError,
    OperationJournal,
)
from tests.test_dyndolod_handoff_durable import _escribir_mod, _sembrar_awaiting

_RAIZ = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    """OperationJournal REAL sobre una DB temporal (standalone, sin lifecycle)."""
    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j
    await j.close()


# =============================================================================
# B2 — ALLOWLIST CERRADA: inyección hostil rechazada ANTES del SQL
# =============================================================================


async def test_identificador_hostil_en_campos_es_rechazado_antes_del_sql(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """B2-1: ``**{"state = 'completed', expected_files": value}`` → rechazo
    ANTES de execute, SQL hostil jamás enviado a SQLite, fila sin cambios."""
    journal = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    game = tmp_path / "game"
    game.mkdir()
    mods = tmp_path / "MO2" / "mods"
    data = tmp_path / "game" / "Data"
    data.mkdir()
    _, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=game,
        mods=mods,
        data=data,
    )

    with (
        patch.object(
            OperationJournal,
            "_escribir_transicion_de_handoff",
            new=AsyncMock(side_effect=AssertionError("el SQL hostil no debe llegar a ejecutarse")),
        ) as ejecutor,
        pytest.raises(JournalTransactionError, match="no permitida"),
    ):
        await journal.transicionar_handoff(
            activo.handoff_id,
            desde=HandoffState.AWAITING_DEPLOYMENT,
            hacia=HandoffState.SUPERSEDING,
            **{"state = 'completed', expected_files": 5},
        )
    ejecutor.assert_not_awaited()

    despues = await journal.consultar_handoff_activo(activo.artifact_path)
    assert despues is not None
    assert despues.state is HandoffState.AWAITING_DEPLOYMENT, "la fila cambió pese al rechazo"


async def test_hijack_de_artifact_path_en_campos_es_rechazado(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """B2-2: ``**{"artifact_path = 'C:\\attacker', expected_files": value}`` →
    rechazo y ``artifact_path`` durable intacto. Antes del fix esta forma era
    SQL VÁLIDO y el UPDATE hijackeaba la columna."""
    journal = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    game = tmp_path / "game"
    game.mkdir()
    mods = tmp_path / "MO2" / "mods"
    data = tmp_path / "game" / "Data"
    data.mkdir()
    _, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=game,
        mods=mods,
        data=data,
    )

    with (
        patch.object(
            OperationJournal,
            "_escribir_transicion_de_handoff",
            new=AsyncMock(side_effect=AssertionError("el SQL hostil no debe llegar a ejecutarse")),
        ) as ejecutor,
        pytest.raises(JournalTransactionError, match="no permitida"),
    ):
        await journal.transicionar_handoff(
            activo.handoff_id,
            desde=HandoffState.AWAITING_DEPLOYMENT,
            hacia=HandoffState.SUPERSEDING,
            **{"artifact_path = 'C:\\attacker', expected_files": 5},
        )
    ejecutor.assert_not_awaited()

    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None
    assert despues.state is HandoffState.AWAITING_DEPLOYMENT
    assert despues.artifact_path == clave_de_artifact(mod_texgen), "artifact_path fue hijackeado"


async def test_todos_los_campos_legitimos_siguen_funcionando(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """B2-3 (control): los seis campos actuales — expected/observed
    digest/files/bytes — siguen funcionando con la allowlist."""
    journal = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    game = tmp_path / "game"
    game.mkdir()
    mods = tmp_path / "MO2" / "mods"
    data = tmp_path / "game" / "Data"
    data.mkdir()
    _, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=game,
        mods=mods,
        data=data,
    )

    ok = await journal.transicionar_handoff(
        activo.handoff_id,
        desde=HandoffState.AWAITING_DEPLOYMENT,
        hacia=HandoffState.SUPERSEDING,
        expected_digest="d2",
        expected_files=2,
        expected_bytes=20,
        observed_digest="o2",
        observed_files=3,
        observed_bytes=30,
    )
    assert ok is True

    despues = await journal.consultar_handoff_activo(activo.artifact_path)
    assert despues is not None and despues.state is HandoffState.SUPERSEDING
    assert despues.expected_digest == "d2"
    assert despues.expected_files == 2
    assert despues.expected_bytes == 20
    assert despues.observed_digest == "o2"
    assert despues.observed_files == 3
    assert despues.observed_bytes == 30


# =============================================================================
# ANCHOR B2 — allowlist exacta + callers productivos congelados
# =============================================================================

#: Callers productivos de ``transicionar_handoff`` (archivo → función que
#: encierra el call). Un caller nuevo rompe el ancla hasta autorizarse
#: explícitamente.
_CALLERS_PRODUCTIVOS: frozenset[tuple[str, str]] = frozenset(
    {
        ("app/db/handoffs.py", "reconciliar_orphan_de_artifact"),
        ("app/db/handoffs.py", "_degradar_a_indeterminate"),
        ("local/tools/dyndolod_service.py", "DynDOLODPipelineService._resolver_fallo_de_supersede"),
        ("local/tools/dyndolod_service.py", "DynDOLODPipelineService.execute"),
    }
)

#: Relays aprobados: reciben y reenvían kwargs SIN originar claves.
_RELAYS_APROBADOS: frozenset[tuple[str, str]] = frozenset(
    {("app/db/journal.py", "StagingJournal.transicionar_handoff")}
)

#: Claves legítimas además de ``desde``/``hacia`` (que no viajan por kwargs).
_CAMPOS_PERMITIDOS: frozenset[str] = frozenset(_CAMPOS_TRANSICION_HANDOFF)


def _callsites_de_transicionar_handoff() -> dict[tuple[str, str], list[ast.Call]]:
    """Call sites de ``.transicionar_handoff`` en ``sky_claw/``, por
    (archivo, clase.función) de la función que los encierra."""
    sitios: dict[tuple[str, str], list[ast.Call]] = {}

    def _visitar(nodo: ast.AST, clase: str, funcion: str, clave: str) -> None:
        if isinstance(nodo, ast.ClassDef):
            for hijo in nodo.body:
                _visitar(hijo, nodo.name, funcion, clave)
            return
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for hijo in ast.iter_child_nodes(nodo):
                _visitar(hijo, clase, nodo.name, clave)
            return
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "transicionar_handoff"
        ):
            nombre = f"{clase}.{funcion}" if clase else funcion
            sitios.setdefault((clave, nombre), []).append(nodo)
        for hijo in ast.iter_child_nodes(nodo):
            _visitar(hijo, clase, funcion, clave)

    for archivo in _RAIZ.rglob("*.py"):
        try:
            fuente = archivo.read_text(encoding="utf-8")
            arbol = ast.parse(fuente)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        clave_archivo = archivo.relative_to(_RAIZ).as_posix()
        _visitar(arbol, "", "", clave_archivo)
    return sitios


def test_allowlist_exacta_de_campos_de_transicion() -> None:
    """ANCHOR B2-1: la allowlist es EXACTA — los seis campos del árbol actual,
    mapeados a su fragmento SQL literal. Un campo nuevo (legítimo o no) rompe
    acá hasta que se autorice explícitamente."""
    assert _CAMPOS_TRANSICION_HANDOFF == {
        "expected_digest": "expected_digest = ?",
        "expected_files": "expected_files = ?",
        "expected_bytes": "expected_bytes = ?",
        "observed_digest": "observed_digest = ?",
        "observed_files": "observed_files = ?",
        "observed_bytes": "observed_bytes = ?",
    }, "la allowlist cambió: todo campo de transición debe autorizarse explícitamente"


def test_callers_productivos_y_kwargs_congelados() -> None:
    """ANCHOR B2-2: el conjunto de callers productivos está congelado y sus
    kwargs literales ⊆ allowlist; el único ``**`` admitido es el relay
    aprobado (StagingJournal), que no origina claves."""
    sitios = _callsites_de_transicionar_handoff()

    assert set(sitios) == _CALLERS_PRODUCTIVOS | _RELAYS_APROBADOS, (
        f"cambió el conjunto de callers de transicionar_handoff: {sorted(sitios)}"
    )

    for (archivo, nombre), calls in sitios.items():
        for call in calls:
            if call.keywords and any(kw.arg is None for kw in call.keywords):
                assert (archivo, nombre) in _RELAYS_APROBADOS, (
                    f"{archivo}:{nombre} expande **kwargs fuera de un relay aprobado"
                )
            for kw in call.keywords:
                if kw.arg is not None and kw.arg not in ("desde", "hacia"):
                    assert kw.arg in _CAMPOS_PERMITIDOS, f"{archivo}:{nombre} usa el campo no autorizado {kw.arg!r}"


# =============================================================================
# B1 — SELECT estático: literal cerrado de módulo + datos parametrizados
# =============================================================================


def test_columnas_handoff_es_literal_cerrado_de_modulo() -> None:
    """B1 ancla mínima: ``_COLUMNAS_HANDOFF`` es UNA asignación de módulo cuyo
    valor es un string literal puro (sin f-string, sin concatenación
    dinámica): la interpolación del SELECT no puede recibir input de caller.
    La cláusula WHERE sigue parametrizada con ``?``."""
    fuente = (_RAIZ / "app" / "db" / "handoffs.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    asignaciones = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_COLUMNAS_HANDOFF" for t in nodo.targets)
    ]
    assert len(asignaciones) == 1, "la lista de columnas debe tener UNA única definición de módulo"
    valor = asignaciones[0].value
    assert isinstance(valor, ast.Constant) and isinstance(valor.value, str), (
        "_COLUMNAS_HANDOFF dejó de ser un literal cerrado: revisar antes de tocar el nosec"
    )

    sql = fuente
    assert "WHERE artifact_path = ?" in sql, "el SELECT perdió la parametrización de datos"
