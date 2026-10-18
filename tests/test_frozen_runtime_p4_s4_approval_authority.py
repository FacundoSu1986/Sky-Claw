"""Tests RED-first para P4-S4: autoridad durable de aprobación (ADR 0012).

Autoridad normativa: §34.6/§35.2/§38.5 (``ApprovalScope`` de 14 campos),
§36.5/§37.3 (ledger con CAS único), §37.2/§38.2 (burn-first + tombstone),
§37.4 (FSM, ``MAX_ISSUED = 1``, sin TTL), §39.3 (preasignar → digest →
presentar → consentir → CAS), §40.2/§40.3 (witness de emisión y semántica única
de colisión), §45.10 (N6 reparable vs. autoridad incoherente).

Este slice NO activa nada: no hay bind, ni promoción, ni rollback. La EMISIÓN
productiva está bloqueada (``ApprovalHitolNoDisponibleError``) porque el producto
no tiene superficie HITL; el núcleo durable se ejercita con fixtures explícitas.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid

import pytest

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.approval import (
    APPROVAL_SCOPE_FIELDS,
    MAX_ISSUED_APPROVALS_PER_ROOT,
    TIME_BASED_EXPIRATION_REQUIRED,
    ApprovalCasError,
    ApprovalConsentimientoError,
    ApprovalCorruptError,
    ApprovalHitolNoDisponibleError,
    ApprovalIdentityError,
    ApprovalLedgerDisposition,
    ApprovalLedgerRetrocedidoError,
    ApprovalLifecycle,
    ApprovalMaxIssuedError,
    ApprovalMintingDefectError,
    ApprovalOperation,
    ApprovalRootAjenoError,
    ApprovalSchemaError,
    ApprovalScope,
    ApprovalStoreNoInicializadoError,
    ConsentimientoAprobacion,
    PropuestaAprobacion,
    TerminalState,
    VeredictoAutoridad,
    calcular_approval_scope_digest,
    cargar_ledger_aprobacion,
    clasificar_autoridad,
    construir_scope,
    consumir_aprobacion,
    derivar_autoridad_aprobacion,
    emitir_aprobacion,
    proponer_aprobacion,
    reconciliar_aprobacion,
    revocar_aprobacion,
    validar_scope,
    verificar_consumo_aprobacion,
)
from sky_claw.local.frozen_runtime.errors import JournalDependenciaNoDisponibleError
from sky_claw.local.frozen_runtime.journal import SourceActivation
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import (
    approval_ledger_path,
    approvals_state_dir,
    initialize_frozen_runtime_storage,
    issuances_state_dir,
)

WORKTREE = pathlib.Path(__file__).resolve().parents[1]
ENTORNO_HIJO = {
    **os.environ,
    "PYTHONPATH": str(WORKTREE),
    "SKYCLAW_P4_S4_WORKTREE": str(WORKTREE),
}
_HIJO = pathlib.Path(__file__).resolve().parent / "_p4_s4_child.py"

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
T1 = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90"
T2 = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = tmp_path / nombre
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success, resultado.admission.message
    return root


def _instantanea_state(root: pathlib.Path) -> dict[str, bytes]:
    """Bytes de todo ``state/`` menos el lock, que es estado del mutex.

    Se indexa por ruta relativa, no por basename: dos artefactos en directorios
    distintos pueden compartir nombre y colapsarían en el dict.
    """
    return {
        ruta.relative_to(root).as_posix(): ruta.read_bytes()
        for ruta in root.joinpath("state").rglob("*")
        if ruta.is_file() and ruta.name != "frozen-runtime.lock"
    }


def _fuente(**overrides: object) -> SourceActivation:
    base = {
        "generation_id": GEN_ORIGEN,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": "b" * 64,
    }
    base.update(overrides)
    return SourceActivation(**base)  # type: ignore[arg-type]


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation": ApprovalOperation.PROMOTION,
        "source_activation": _fuente(),
        "generation_id": GEN_OBJETIVO,
        "clone_id": "clone-objetivo",
        "clone_evidence": "d" * 64,
        "runtime_setup_id": "setup-objetivo",
        "runtime_setup_evidence": "e" * 64,
        "compatibility_evidence_id": "f" * 64,
        "candidate_id": "cand-1",
        "approval_provenance": "ui:req-1",
    }
    base.update(overrides)
    return base


def _proponer(root: pathlib.Path, **overrides: object) -> PropuestaAprobacion:
    return proponer_aprobacion(root, **_kwargs(**overrides))  # type: ignore[arg-type]


def _consentimiento(propuesta: PropuestaAprobacion, **overrides: object) -> ConsentimientoAprobacion:
    base = {
        "approval_scope_digest": propuesta.scope.approval_scope_digest,
        "approval_provenance": propuesta.scope.approval_provenance,
        "surface": "fixture",
    }
    base.update(overrides)
    return ConsentimientoAprobacion(**base)  # type: ignore[arg-type]


def _emitir(root: pathlib.Path, propuesta: PropuestaAprobacion, **overrides: object) -> object:
    """Emite por el núcleo durable (fixture explícita de consentimiento)."""
    consentimiento = _consentimiento(propuesta, **overrides)
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        return approval_module._emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)


def _emitir_fixture(root: pathlib.Path, **overrides: object) -> tuple[PropuestaAprobacion, object]:
    propuesta = _proponer(root, **overrides)
    return propuesta, _emitir(root, propuesta)


def _consumir(root: pathlib.Path, propuesta: PropuestaAprobacion, transition_id: str = T1, **overrides: object):
    datos = {
        "approval_id": propuesta.scope.approval_id,
        "approval_seq": propuesta.scope.approval_seq,
        "approval_scope_digest": propuesta.scope.approval_scope_digest,
        "transition_id": transition_id,
    }
    datos.update(overrides)
    return consumir_aprobacion(root, **datos)  # type: ignore[arg-type]


def _leer_json(ruta: pathlib.Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir_json(ruta: pathlib.Path, payload: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _snapshot_ledger(root: pathlib.Path) -> str:
    return approval_ledger_path(root).read_text(encoding="utf-8")


def _restaurar_ledger(root: pathlib.Path, contenido: str) -> None:
    approval_ledger_path(root).write_text(contenido, encoding="utf-8")


def _quitar_enlace_sin_seguir(ruta: pathlib.Path) -> None:
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError:
        tipo = None
    if tipo is None and not ruta.exists():
        return
    if tipo is not None and (tipo == "junction" or ruta.is_dir()):
        os.rmdir(ruta)
        return
    if tipo is None and ruta.is_dir():
        shutil.rmtree(ruta, ignore_errors=True)
        return
    ruta.unlink()


def _crear_enlace_de_directorio(destino: pathlib.Path, origen: pathlib.Path) -> str:
    """Crea un enlace de DIRECTORIO real; ``os.symlink`` o junction como fallback.

    El régimen sandbox puede materializar una COPIA en vez de un enlace: en ese
    caso el caso no se ejerció y hay que decirlo, no asumirlo.
    """
    try:
        os.symlink(origen, destino, target_is_directory=True)
    except OSError:
        pass
    else:
        if link_kind_or_raise(destino) == "symlink":
            return "symlink"
        _quitar_enlace_sin_seguir(destino)
    if os.name == "nt":
        proceso = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(destino), str(origen)],
            capture_output=True,
            timeout=30,
        )
        if proceso.returncode == 0 and link_kind_or_raise(destino) == "junction":
            return "junction"
        _quitar_enlace_sin_seguir(destino)
    pytest.skip("el régimen no puede crear un enlace de directorio real (ni symlink ni junction)")


def _exigir_enlace_real(ruta: pathlib.Path) -> str:
    tipo = link_kind_or_raise(ruta)
    assert tipo is not None, f"{ruta} debía ser un enlace real y no lo es: el caso no se ejerció"
    return tipo


def _lanzar(modo: str, root: pathlib.Path, args: dict) -> dict:
    proceso = subprocess.run(  # noqa: S603
        [sys.executable, str(_HIJO), modo, str(root), json.dumps(args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENTORNO_HIJO,
        timeout=120,
        cwd=str(WORKTREE),
    )
    assert proceso.returncode == 0, proceso.stderr
    return json.loads(proceso.stdout.strip().splitlines()[-1])


def _lanzar_concurrentes(modo: str, root: pathlib.Path, lista_args: list[dict]) -> list[dict]:
    procesos = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(_HIJO), modo, str(root), json.dumps(args)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=ENTORNO_HIJO,
            cwd=str(WORKTREE),
        )
        for args in lista_args
    ]
    resultados: list[dict] = []
    for proceso in procesos:
        salida, error = proceso.communicate(timeout=180)
        assert proceso.returncode == 0, error
        resultados.append(json.loads(salida.strip().splitlines()[-1]))
    return resultados


# ---------------------------------------------------------------------------
# Censo normativo del ApprovalScope (§38.5)
# ---------------------------------------------------------------------------


def test_el_scope_tiene_exactamente_los_14_campos_canonicos() -> None:
    """El censo se afirma contra ``dataclasses.fields``, no contra un comentario."""
    campos = [campo.name for campo in dataclasses.fields(ApprovalScope)]
    assert len(campos) == APPROVAL_SCOPE_FIELDS == 14
    assert campos == [
        "operation",
        "source_activation",
        "source_activation_digest",
        "generation_id",
        "clone_id",
        "clone_evidence",
        "runtime_setup_id",
        "runtime_setup_evidence",
        "compatibility_evidence_id",
        "candidate_id",
        "approval_id",
        "approval_seq",
        "approval_scope_digest",
        "approval_provenance",
    ]
    # §38.5: el prefijo `target_` está reservado al REGISTRO, no al scope.
    assert not [campo for campo in campos if campo.startswith("target_")]


def test_el_digest_compromete_los_13_campos_restantes() -> None:
    scope = _scope_ejemplo()
    payload = approval_module.payload_scope_sin_digest(scope)
    assert len(payload) == 13
    assert "approval_scope_digest" not in payload
    assert set(payload) == {campo.name for campo in dataclasses.fields(ApprovalScope)} - {"approval_scope_digest"}


def test_cambiar_un_solo_campo_cambia_el_digest() -> None:
    base = _scope_ejemplo()
    original = base.approval_scope_digest
    for campo, valor in (
        ("clone_evidence", "0" * 64),
        ("runtime_setup_evidence", "1" * 64),
        ("compatibility_evidence_id", "2" * 64),
        ("generation_id", "1.6.353.0__deadbeef0001"),
        ("clone_id", "otro-clone"),
        ("runtime_setup_id", "otro-setup"),
        ("candidate_id", "otro-cand"),
        ("approval_provenance", "ui:req-9"),
    ):
        adulterado = dataclasses.replace(base, **{campo: valor})
        assert calcular_approval_scope_digest(adulterado) != original, campo


def test_el_digest_del_source_activation_coincide_con_el_del_journal() -> None:
    """Una sola implementación del digest: la autoridad reusa la del journal.

    Si divergieran, la aprobación se emitiría con un digest que el journal
    rechaza al crear el PENDING («no liga el contenido»). Es el modo de falla
    «arreglar un hermano y no al otro» aplicado a un primitivo compartido.
    """
    fuente = _fuente()
    scope = _scope_ejemplo()
    assert scope.source_activation_digest == journal_module.digest_canonico(fuente)
    assert scope.source_activation_digest == journal_module._digest_canonico(fuente)


def test_los_validadores_de_forma_coinciden_con_los_del_journal() -> None:
    """Paridad de validadores: el mismo valor se acepta o se rechaza en ambos lados.

    La autoridad de aprobación tiene sus propios validadores (para tipar el error
    en su familia), así que la paridad hay que AFIRMARLA, no suponerla.
    """
    casos_digest = ["a" * 64, "A" * 64, "a" * 63, "", 1, None, "a" * 65]
    for valor in casos_digest:
        assert _falla(approval_module._exigir_digest, valor) == _falla(journal_module._exigir_digest, valor), valor
    casos_uuid = [T1, T1.upper(), "no-es-uuid", "", 3, None, "8f4e1a90-5ac1-1b6d-9c21-0d2b8f4e1a90"]
    for valor in casos_uuid:
        assert _falla(approval_module._exigir_uuid_v4, valor) == _falla(journal_module._exigir_uuid_v4, valor), valor
    casos_texto = ["x", " x", "x ", "", "   ", 1, None, "a b"]
    for valor in casos_texto:
        assert _falla(approval_module._exigir_texto, valor) == _falla(journal_module._exigir_texto_valor, valor), valor
    casos_gen = [GEN_ORIGEN, GEN_ORIGEN.upper(), "sin-separador", "../escape", "", None]
    for valor in casos_gen:
        assert _falla(approval_module._exigir_generation_id, valor) == _falla(
            journal_module._exigir_generation_id_valor, valor
        ), valor


def _falla(funcion: object, valor: object) -> bool:
    try:
        funcion(valor, campo="x", etiqueta="paridad")  # type: ignore[operator]
    except Exception:
        return True
    return False


def _scope_ejemplo(**overrides: object) -> ApprovalScope:
    """Scope bien formado SIN tocar disco: censo, digest y validación de forma."""
    datos = _kwargs(**overrides)
    return construir_scope(
        operation=datos["operation"],  # type: ignore[arg-type]
        source_activation=datos["source_activation"],  # type: ignore[arg-type]
        generation_id=datos["generation_id"],  # type: ignore[arg-type]
        clone_id=datos["clone_id"],  # type: ignore[arg-type]
        clone_evidence=datos["clone_evidence"],  # type: ignore[arg-type]
        runtime_setup_id=datos["runtime_setup_id"],  # type: ignore[arg-type]
        runtime_setup_evidence=datos["runtime_setup_evidence"],  # type: ignore[arg-type]
        compatibility_evidence_id=datos["compatibility_evidence_id"],  # type: ignore[arg-type]
        candidate_id=datos["candidate_id"],  # type: ignore[arg-type]
        approval_id=T1,
        approval_seq=1,
        approval_provenance=datos["approval_provenance"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Fase A — propuesta
# ---------------------------------------------------------------------------


def test_la_propuesta_preasigna_seq_e_id_antes_del_digest(tmp_path: pathlib.Path) -> None:
    """§39.3 A2/A4: el scope presentado YA lleva los dos identificadores."""
    root = _root(tmp_path)
    propuesta = _proponer(root)
    assert propuesta.scope.approval_seq == 1
    assert propuesta.scope.approval_id == str(uuid.UUID(propuesta.scope.approval_id))
    assert propuesta.ledger_revision_at_proposal == 0
    # El digest compromete ambos: recomputarlo sobre el mismo scope da lo mismo.
    assert propuesta.scope.approval_scope_digest == calcular_approval_scope_digest(propuesta.scope)


def test_la_propuesta_no_persiste_nada(tmp_path: pathlib.Path) -> None:
    """Una propuesta no tiene autoridad: nada durable cambia (§39.5 P0/P1).

    ``frozen-runtime.lock`` se excluye porque es el estado del mutex, no
    autoridad: cambia en CUALQUIER sección crítica (incluida una de sólo
    lectura) y su contenido es un par acquire/release con session_id. Lo que la
    propiedad prohíbe es que la propuesta deje rastro en la CADENA DE APROBACIÓN.
    """
    root = _root(tmp_path)
    antes = _instantanea_state(root)
    _proponer(root)
    despues = _instantanea_state(root)
    assert antes == despues
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT
    assert derivar_autoridad_aprobacion(root).witnesses == ()


def test_promotion_exige_candidate_id(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(ApprovalSchemaError, match="candidate_id"):
        _proponer(root, candidate_id=None)


def test_rollback_exige_candidate_id_not_applicable(tmp_path: pathlib.Path) -> None:
    """§38.5 campo 10: ROLLBACK lleva ``NOT_APPLICABLE``, no un id cualquiera."""
    root = _root(tmp_path)
    propuesta = _proponer(root, operation=ApprovalOperation.ROLLBACK, candidate_id=None)
    assert propuesta.scope.candidate_id is None
    with pytest.raises(ApprovalSchemaError, match="NOT_APPLICABLE"):
        _proponer(root, operation=ApprovalOperation.ROLLBACK, candidate_id="cand-1")


def test_un_source_activation_incoherente_no_se_propone(tmp_path: pathlib.Path) -> None:
    """El ``source_activation`` se valida como dato, no se copia a ciegas.

    En la propuesta el ``source_activation_digest`` lo DERIVA ``construir_scope``,
    así que por esta puerta no puede quedar desligado; lo que sí puede llegar es
    una activación de origen malformada, y eso se rechaza antes de acuñar nada.
    """
    root = _root(tmp_path)
    with pytest.raises(ApprovalSchemaError, match="generation-id válido"):
        _proponer(root, source_activation=_fuente(generation_id="no-es-una-generacion"))
    with pytest.raises(ApprovalSchemaError, match="clone_id"):
        _proponer(root, source_activation=_fuente(clone_id=""))


def test_un_source_activation_desligado_del_digest_no_valida() -> None:
    """§35.2: el ``source_activation_digest`` tiene que LIGAR el contenido.

    Es la mitad que el digest del scope no cubre: cambiar ``source_activation``
    sin recomputar su digest deja el scope internamente incoherente aunque los
    otros 13 campos estén intactos. El chequeo del digest de scope es tautológico
    si el presentador controla ambos lados; éste no, porque el contenido se
    rehashea desde el objeto.
    """
    scope = _scope_ejemplo()
    otro_origen = dataclasses.replace(scope.source_activation, clone_id="clone-distinto")
    desligado = dataclasses.replace(scope, source_activation=otro_origen)
    assert desligado.approval_scope_digest == scope.approval_scope_digest  # los 13 campos no cambiaron
    with pytest.raises(ApprovalIdentityError, match="no liga el contenido"):
        validar_scope(desligado)


def test_la_propuesta_no_se_presenta_con_la_autoridad_incoherente(tmp_path: pathlib.Path) -> None:
    """§39.3 A5: con la autoridad durable incoherente NO se propone."""
    root = _root(tmp_path)
    _emitir_fixture(root)
    # Ledger restaurado a génesis con witness presente: LEDGER_RETROCEDIDO.
    _escribir_json(
        approval_ledger_path(root),
        {
            "schema_version": 1,
            "root_id": approval_module._root_id(root),
            "approval_revision": 0,
            "last_issued_approval_seq": 0,
            "last_issued_approval_id": None,
            "last_issued_scope_digest": None,
            "last_issued_provenance": None,
            "last_consumed_approval_seq": 0,
            "consumed_approval_id": None,
            "consumed_scope_digest": None,
            "consumed_by_transition_id": None,
            "state": None,
            "issued_at_ns": None,
            "consumed_at_ns": None,
            "revoked_at_ns": None,
            "approval_terminal_store": None,
        },
    )
    with pytest.raises(ApprovalLedgerRetrocedidoError, match="LEDGER_RETROCEDIDO"):
        _proponer(root, approval_provenance="ui:req-2")


# ---------------------------------------------------------------------------
# Emisión
# ---------------------------------------------------------------------------


def test_la_emision_publica_exactamente_lo_propuesto(tmp_path: pathlib.Path) -> None:
    """``WHAT_HUMAN_APPROVED == WHAT_WAS_ISSUED`` (§39.3 C2)."""
    root = _root(tmp_path)
    propuesta, ledger = _emitir_fixture(root)
    assert ledger.state is ApprovalLifecycle.ISSUED
    assert ledger.last_issued_approval_seq == propuesta.scope.approval_seq
    assert ledger.last_issued_approval_id == propuesta.scope.approval_id
    assert ledger.last_issued_scope_digest == propuesta.scope.approval_scope_digest
    assert ledger.last_issued_provenance == propuesta.scope.approval_provenance
    assert ledger.approval_revision == propuesta.ledger_revision_at_proposal + 1


def test_el_witness_es_la_autoridad_de_la_emision(tmp_path: pathlib.Path) -> None:
    """El ``CREATE`` del witness ES la emisión: existe aunque el ledger se pierda."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    assert ruta.exists()
    witness = _leer_json(ruta)
    assert witness["approval_id"] == propuesta.scope.approval_id
    assert witness["approval_scope_digest"] == propuesta.scope.approval_scope_digest
    assert witness["approval_provenance"] == propuesta.scope.approval_provenance
    assert witness["ledger_revision_at_issue"] == propuesta.ledger_revision_at_proposal


def test_la_emision_productiva_esta_bloqueada(tmp_path: pathlib.Path) -> None:
    """No hay superficie HITL: la entrada productiva falla cerrado, tipada."""
    root = _root(tmp_path)
    propuesta = _proponer(root)
    with pytest.raises(ApprovalHitolNoDisponibleError):
        emitir_aprobacion(root, propuesta, _consentimiento(propuesta))
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT


def test_el_consentimiento_que_no_liga_el_digest_no_emite(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta = _proponer(root)
    with pytest.raises(ApprovalConsentimientoError, match="WHAT_HUMAN_APPROVED"):
        _emitir(root, propuesta, approval_scope_digest="a" * 64)
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT
    assert derivar_autoridad_aprobacion(root).witnesses == ()


def test_el_consentimiento_con_otra_procedencia_no_emite(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta = _proponer(root)
    with pytest.raises(ApprovalConsentimientoError, match="procedencia"):
        _emitir(root, propuesta, approval_provenance="ui:req-otra")
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT


def test_max_issued_bloquea_una_segunda_emision(tmp_path: pathlib.Path) -> None:
    """``MAX_ISSUED_APPROVALS_PER_ROOT = 1`` (§37.4)."""
    root = _root(tmp_path)
    _emitir_fixture(root)
    with pytest.raises(ApprovalMaxIssuedError):
        _proponer(root, approval_provenance="ui:req-2")
    assert MAX_ISSUED_APPROVALS_PER_ROOT == 1


def test_la_emision_rechaza_un_scope_adulterado(tmp_path: pathlib.Path) -> None:
    """Un scope con el digest recomputado sobre OTRO contenido no se emite si el
    consentimiento aprobó el digest original."""
    root = _root(tmp_path)
    propuesta = _proponer(root)
    adulterado = dataclasses.replace(propuesta.scope, clone_evidence="9" * 64, approval_scope_digest="")
    adulterado = dataclasses.replace(adulterado, approval_scope_digest=calcular_approval_scope_digest(adulterado))
    consentimiento = _consentimiento(propuesta)  # aprobó el digest ORIGINAL
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        with pytest.raises(ApprovalConsentimientoError):
            approval_module._emitir_aprobacion_bajo_lock(
                handle, root, dataclasses.replace(propuesta, scope=adulterado), consentimiento
            )
    assert derivar_autoridad_aprobacion(root).witnesses == ()


# ---------------------------------------------------------------------------
# Consumo
# ---------------------------------------------------------------------------


def test_el_consumo_crea_tombstone_y_actualiza_la_cache(tmp_path: pathlib.Path) -> None:
    """Burn-first: tombstone CONSUMED + caché CONSUMED (§37.2/§38.2)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    resultado = _consumir(root, propuesta)
    assert resultado.approval_seq == propuesta.scope.approval_seq
    assert resultado.cache_reconciled is False
    assert resultado.terminal_path.exists()
    tombstone = _leer_json(resultado.terminal_path)
    assert tombstone["terminal_state"] == TerminalState.CONSUMED.value
    assert tombstone["consumed_by_transition_id"] == T1
    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.CONSUMED
    assert ledger.last_consumed_approval_seq == propuesta.scope.approval_seq
    assert ledger.consumed_by_transition_id == T1


def test_el_consumo_es_una_sola_vez(tmp_path: pathlib.Path) -> None:
    """Otra transición con la MISMA aprobación es un replay (§35.2)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta, T1)
    with pytest.raises(ApprovalIdentityError, match="single-use|CONSUMED"):
        _consumir(root, propuesta, T2)
    assert len(list(approvals_state_dir(root).iterdir())) == 1


def test_el_reintento_idempotente_devuelve_el_mismo_resultado(tmp_path: pathlib.Path) -> None:
    """Reintentar el MISMO consumo (respuesta perdida) no es un replay."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    primero = _consumir(root, propuesta, T1)
    segundo = _consumir(root, propuesta, T1)
    assert segundo.approval_seq == primero.approval_seq
    assert segundo.terminal_path == primero.terminal_path
    assert len(list(approvals_state_dir(root).iterdir())) == 1


def test_el_consumo_sin_aprobacion_emitida_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(ApprovalIdentityError, match="ninguna aprobación emitida"):
        consumir_aprobacion(
            root,
            approval_id=T1,
            approval_seq=1,
            approval_scope_digest="a" * 64,
            transition_id=T2,
        )
    assert not list(approvals_state_dir(root).iterdir())


def test_el_consumo_rechaza_otro_digest(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalIdentityError, match="adulterado"):
        _consumir(root, propuesta, approval_scope_digest="a" * 64)
    assert not list(approvals_state_dir(root).iterdir())


def test_el_consumo_rechaza_otro_approval_id(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalIdentityError, match="approval_id"):
        _consumir(root, propuesta, approval_id=str(uuid.uuid4()))
    assert not list(approvals_state_dir(root).iterdir())


def test_el_consumo_rechaza_otra_seq(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalIdentityError, match="approval_seq"):
        _consumir(root, propuesta, approval_seq=7)
    assert not list(approvals_state_dir(root).iterdir())


def test_el_consumo_valida_antes_de_escribir_el_tombstone(tmp_path: pathlib.Path) -> None:
    """R11.2: un request malformado NO terminaliza una aprobación válida.

    Es la diferencia entre «rechazar» y «quemar el token por un error de tipeo»:
    si el tombstone precediera a la validación, el dueño perdería la aprobación.
    """
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    for overrides in (
        {"approval_scope_digest": "a" * 64},
        {"approval_id": str(uuid.uuid4())},
        {"approval_seq": 9},
    ):
        with pytest.raises(ApprovalIdentityError):
            _consumir(root, propuesta, **overrides)
    assert not list(approvals_state_dir(root).iterdir())
    assert cargar_ledger_aprobacion(root).ledger.state is ApprovalLifecycle.ISSUED
    # La aprobación sigue siendo consumible: la validación no la quemó.
    assert _consumir(root, propuesta).approval_seq == propuesta.scope.approval_seq


# ---------------------------------------------------------------------------
# Revocación
# ---------------------------------------------------------------------------


def test_la_revocacion_es_terminal_e_irreversible(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    terminal = revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="el dueño canceló",
    )
    assert terminal.exists()
    assert _leer_json(terminal)["terminal_state"] == TerminalState.REVOKED.value
    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.REVOKED
    with pytest.raises(ApprovalIdentityError, match="ISSUED"):
        revocar_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            motivo="otra vez",
        )


def test_una_aprobacion_revocada_no_se_consume(tmp_path: pathlib.Path) -> None:
    """``REVOKED_APPROVAL_IS_CONSUMABLE = NO`` / ``_REUSABLE = NO`` (§37.4)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="cancelado",
    )
    # El motivo se afirma, no sólo el veredicto: «no se consume» también lo
    # devuelve el guard de evidencia terminal (`_exigir_evidencia_terminal_previa`),
    # así que aceptar cualquiera de los dos mensajes dejaba el guard de estado sin
    # discriminar (mutante M03 sobrevivía a la suite entera).
    with pytest.raises(ApprovalIdentityError, match="el estado de la aprobación es REVOKED, no ISSUED"):
        _consumir(root, propuesta)
    assert len(list(approvals_state_dir(root).iterdir())) == 1


def test_la_revocacion_deja_evidencia_terminal_inmutable(tmp_path: pathlib.Path) -> None:
    """Sin tombstone la revocación no era demostrable tras un rollback del ledger."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    terminal = revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="cancelado",
    )
    contenido = terminal.read_bytes()
    # Restaurar el ledger a ISSUED NO resucita el token: el tombstone manda.
    _restaurar_ledger(root, _snapshot_ledger(root).replace('"REVOKED"', '"ISSUED"'))
    with pytest.raises(ApprovalLedgerRetrocedidoError):
        _consumir(root, propuesta)
    assert terminal.read_bytes() == contenido


def test_la_revocacion_requiere_estado_issued(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    with pytest.raises(ApprovalIdentityError, match="ISSUED"):
        revocar_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            motivo="tarde",
        )


# ---------------------------------------------------------------------------
# Expiración: no hay reloj, hay bloqueo
# ---------------------------------------------------------------------------


def test_no_hay_expiracion_por_reloj() -> None:
    """§37.4: la invalidación es por cambio de contexto, no por TTL."""
    assert TIME_BASED_EXPIRATION_REQUIRED is False


def test_una_aprobacion_abandonada_bloquea_la_siguiente_hasta_revocarla(tmp_path: pathlib.Path) -> None:
    """``ABANDONED_ISSUED_APPROVAL_BLOCKS_NEW_ISSUE = YES`` (fail-closed sin reloj)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalMaxIssuedError):
        _proponer(root, approval_provenance="ui:req-2")
    revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="abandonada",
    )
    nueva = _proponer(root, approval_provenance="ui:req-3")
    assert nueva.scope.approval_seq == propuesta.scope.approval_seq + 1


# ---------------------------------------------------------------------------
# Adulteración del contexto aprobado
# ---------------------------------------------------------------------------


def test_un_cambio_de_source_activation_invalida_la_aprobacion(tmp_path: pathlib.Path) -> None:
    """§35.2: una aprobación obtenida en otro contexto no pasa con el target intacto."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    otra_fuente = _fuente(clone_id="clone-origen-v2")
    otro_scope = construir_scope(
        operation=propuesta.scope.operation,
        source_activation=otra_fuente,
        generation_id=propuesta.scope.generation_id,
        clone_id=propuesta.scope.clone_id,
        clone_evidence=propuesta.scope.clone_evidence,
        runtime_setup_id=propuesta.scope.runtime_setup_id,
        runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
        compatibility_evidence_id=propuesta.scope.compatibility_evidence_id,
        candidate_id=propuesta.scope.candidate_id,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_provenance=propuesta.scope.approval_provenance,
    )
    assert otro_scope.approval_scope_digest != propuesta.scope.approval_scope_digest
    with pytest.raises(ApprovalIdentityError):
        _consumir(root, propuesta, approval_scope_digest=otro_scope.approval_scope_digest)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("clone_evidence", "0" * 64),
        ("runtime_setup_evidence", "1" * 64),
        ("compatibility_evidence_id", "2" * 64),
        ("candidate_id", "cand-2"),
        ("generation_id", "1.6.353.0__deadbeef0001"),
        ("clone_id", "clone-otro"),
        ("runtime_setup_id", "setup-otro"),
    ],
)
def test_un_cambio_de_cualquier_campo_del_scope_invalida_el_consumo(
    tmp_path: pathlib.Path, campo: str, valor: object
) -> None:
    """El digest compromete los 13 campos: cualquiera cambia ⇒ CONSUME falla cerrado."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    adulterado = dataclasses.replace(propuesta.scope, **{campo: valor})
    adulterado = dataclasses.replace(adulterado, approval_scope_digest=calcular_approval_scope_digest(adulterado))
    with pytest.raises(ApprovalIdentityError):
        _consumir(root, propuesta, approval_scope_digest=adulterado.approval_scope_digest)
    assert not list(approvals_state_dir(root).iterdir())


def test_un_cambio_de_operation_invalida_la_aprobacion(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    adulterado = dataclasses.replace(propuesta.scope, operation=ApprovalOperation.ROLLBACK, candidate_id=None)
    adulterado = dataclasses.replace(adulterado, approval_scope_digest=calcular_approval_scope_digest(adulterado))
    with pytest.raises(ApprovalIdentityError):
        _consumir(root, propuesta, approval_scope_digest=adulterado.approval_scope_digest)


def test_un_scope_adulterado_no_pasa_la_validacion_de_forma() -> None:
    scope = _scope_ejemplo()
    roto = dataclasses.replace(scope, clone_evidence="3" * 64)
    with pytest.raises(ApprovalIdentityError, match="no recomputa"):
        validar_scope(roto)


# ---------------------------------------------------------------------------
# Anti-rollback del ledger y reconciliación
# ---------------------------------------------------------------------------


def test_witness_sin_cache_se_reconcilia_hacia_adelante(tmp_path: pathlib.Path) -> None:
    """S1: el witness sobrevive a la pérdida del ledger y la emisión se RECUPERA."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    approval_ledger_path(root).unlink()
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT
    with pytest.raises(ApprovalLedgerRetrocedidoError):
        _consumir(root, propuesta)
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.veredicto is VeredictoAutoridad.LEDGER_RETROCEDIDO
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.state is ApprovalLifecycle.ISSUED
    assert reconciliacion.ledger.last_issued_approval_id == propuesta.scope.approval_id
    assert _consumir(root, propuesta).approval_seq == propuesta.scope.approval_seq


def test_ledger_restaurado_a_issued_con_tombstone_no_se_consume(tmp_path: pathlib.Path) -> None:
    """R1 — resurrección directa: el ledger presenta como ISSUED algo ya terminal."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    snapshot_issued = _snapshot_ledger(root)
    _consumir(root, propuesta)
    _restaurar_ledger(root, snapshot_issued)
    with pytest.raises(ApprovalLedgerRetrocedidoError, match="R1"):
        _consumir(root, propuesta)
    assert len(list(approvals_state_dir(root).iterdir())) == 1


def test_reconciliar_repara_el_ledger_restaurado_desde_el_tombstone(tmp_path: pathlib.Path) -> None:
    """R1 en la OPERACIÓN (no se consume) y reparación determinista en RECOVERY.

    ``FAIL_CLOSED`` no significa «roto para siempre»: la reconciliación explícita
    del dueño reconstruye la caché desde la evidencia inmutable y el token queda
    terminal — nunca «resucitado».
    """
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    snapshot_issued = _snapshot_ledger(root)
    _consumir(root, propuesta)
    _restaurar_ledger(root, snapshot_issued)
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.state is ApprovalLifecycle.CONSUMED
    assert reconciliacion.ledger.consumed_by_transition_id == T1
    # Y el token no vuelve a consumirse con otra transición.
    with pytest.raises(ApprovalIdentityError):
        _consumir(root, propuesta, T2)


def test_secuencia_reutilizada_se_detecta_como_ledger_retrocedido(tmp_path: pathlib.Path) -> None:
    """§40.3: ``proposed_seq = last_issued + 1``; un registro durable en ESA seq ES
    la detección disparándose. No se busca otra secuencia."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    approval_ledger_path(root).unlink()
    with pytest.raises(ApprovalLedgerRetrocedidoError, match="LEDGER_RETROCEDIDO"):
        _proponer(root, approval_provenance="ui:req-2")


def test_approval_id_reutilizado_es_minting_defect(tmp_path: pathlib.Path) -> None:
    """§40.3: un UUID ya usado por otro registro durable no se reacuña a ciegas."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    with pytest.raises(ApprovalMintingDefectError, match="APPROVAL_ID_MINTING_DEFECT"):
        proponer_aprobacion(
            root,
            approval_id_factory=lambda: propuesta.scope.approval_id,
            **_kwargs(approval_provenance="ui:req-2"),  # type: ignore[arg-type]
        )


def test_ledger_emitido_sin_witness_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """S2: el ledger afirma una emisión que ningún witness sustenta."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    (issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").unlink()
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.veredicto is VeredictoAutoridad.LEDGER_ISSUED_WITHOUT_WITNESS
    assert reconciliacion.cache_reconstruida is False
    with pytest.raises(ApprovalLedgerRetrocedidoError):
        _consumir(root, propuesta)


def test_tombstone_sin_ledger_se_reconcilia(tmp_path: pathlib.Path) -> None:
    """Crash entre el burn y el CREATE del journal: CONSUMED sin transición."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    approval_ledger_path(root).unlink()
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.veredicto is VeredictoAutoridad.LEDGER_RETROCEDIDO
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.state is ApprovalLifecycle.CONSUMED
    assert reconciliacion.ledger.last_consumed_approval_seq == propuesta.scope.approval_seq


def test_retroceso_solo_de_la_revision_se_repara_sin_pedir_aprobacion_nueva(tmp_path: pathlib.Path) -> None:
    """N6 (§45.10.1): retrocede SÓLO la revisión, con evidencia coherente."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    ledger = _leer_json(approval_ledger_path(root))
    ledger["approval_revision"] = 0
    _escribir_json(approval_ledger_path(root), ledger)
    autoridad = derivar_autoridad_aprobacion(root)
    veredicto, motivo = clasificar_autoridad(cargar_ledger_aprobacion(root), autoridad)
    assert veredicto is VeredictoAutoridad.CACHE_REVISION_ROLLBACK_ONLY, motivo
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.approval_revision >= autoridad.revision_minima
    assert reconciliacion.ledger.state is ApprovalLifecycle.ISSUED
    # La aprobación sigue siendo válida: no se pidió una nueva.
    assert _consumir(root, propuesta).approval_seq == propuesta.scope.approval_seq


def test_last_consumed_filtra_por_estado_consumed(tmp_path: pathlib.Path) -> None:
    """R11.13: ``seq 20 = CONSUMED``, ``seq 21 = REVOKED`` ⇒ last_consumed sigue 20."""
    root = _root(tmp_path)
    primera, _ = _emitir_fixture(root)
    _consumir(root, primera)
    segunda = _proponer(root, approval_provenance="ui:req-2")
    _emitir(root, segunda)
    revocar_aprobacion(
        root,
        approval_id=segunda.scope.approval_id,
        approval_seq=segunda.scope.approval_seq,
        approval_scope_digest=segunda.scope.approval_scope_digest,
        motivo="cancelada",
    )
    autoridad = derivar_autoridad_aprobacion(root)
    assert autoridad.max_terminal_seq == segunda.scope.approval_seq
    assert autoridad.last_consumed is not None
    assert autoridad.last_consumed.approval_seq == primera.scope.approval_seq
    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.last_consumed_approval_seq == primera.scope.approval_seq


# ---------------------------------------------------------------------------
# Evidencia ilegible: nunca «ausente»
# ---------------------------------------------------------------------------


def test_ledger_corrupto_es_corrupt_no_ausente(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _emitir_fixture(root)
    approval_ledger_path(root).write_text("{ esto no es json", encoding="utf-8")
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.CORRUPT
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.veredicto is VeredictoAutoridad.FAIL_CLOSED
    assert reconciliacion.cache_reconstruida is False


def test_ledger_vacio_es_corrupto(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    approval_ledger_path(root).write_text("", encoding="utf-8")
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.CORRUPT


def test_witness_corrupto_no_se_lee_como_ausente(tmp_path: pathlib.Path) -> None:
    """S4: un witness ilegible es FAIL_CLOSED, nunca «no hubo emisión»."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    (issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").write_text("{", encoding="utf-8")
    with pytest.raises(ApprovalCorruptError):
        derivar_autoridad_aprobacion(root)


def test_tombstone_parcial_es_fail_closed(tmp_path: pathlib.Path) -> None:
    """Un tombstone de 0 bytes es ``FAIL_CLOSED``, no «ausente» (§38.2)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    (approvals_state_dir(root) / f"{propuesta.scope.approval_seq}-{propuesta.scope.approval_id}.json").write_text(
        "", encoding="utf-8"
    )
    with pytest.raises(ApprovalCorruptError):
        derivar_autoridad_aprobacion(root)


def test_duplicado_de_seq_en_tombstones_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """R3: dos tombstones con el mismo ``approval_seq`` son ambiguos."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    original = next(iter(approvals_state_dir(root).iterdir()))
    otro = approvals_state_dir(root) / f"{propuesta.scope.approval_seq}-{uuid.uuid4()}.json"
    payload = _leer_json(original)
    payload["approval_id"] = otro.name.split("-", 1)[1].removesuffix(".json")
    _escribir_json(otro, payload)
    with pytest.raises(ApprovalIdentityError, match="duplicado"):
        derivar_autoridad_aprobacion(root)


def test_hueco_interior_en_la_cadena_de_witnesses_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """La secuencia no salta: un hueco significa evidencia borrada."""
    root = _root(tmp_path)
    primera, _ = _emitir_fixture(root)
    _consumir(root, primera)
    segunda = _proponer(root, approval_provenance="ui:req-2")
    _emitir(root, segunda)
    (issuances_state_dir(root) / f"{primera.scope.approval_seq}.json").unlink()
    with pytest.raises(ApprovalIdentityError, match="hueco interior"):
        derivar_autoridad_aprobacion(root)


def test_tombstone_sin_witness_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    (issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").unlink()
    with pytest.raises(ApprovalIdentityError, match="sin witness"):
        derivar_autoridad_aprobacion(root)


def test_nombre_de_tombstone_fuera_de_contrato_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(approvals_state_dir(root) / "1-ABC.json", {"cualquier": "cosa"})
    with pytest.raises(ApprovalCorruptError, match="nombre fuera del contrato"):
        derivar_autoridad_aprobacion(root)


def test_nombre_de_witness_con_ceros_a_la_izquierda_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(issuances_state_dir(root) / "01.json", {"cualquier": "cosa"})
    with pytest.raises(ApprovalCorruptError, match="nombre fuera del contrato"):
        derivar_autoridad_aprobacion(root)


# ---------------------------------------------------------------------------
# Crash entre operaciones durables
# ---------------------------------------------------------------------------


def test_crash_entre_witness_y_cas_recupera_la_emision(tmp_path: pathlib.Path, monkeypatch) -> None:
    """P9: el witness es la autoridad; el ledger atrasado se reconcilia."""
    root = _root(tmp_path)
    propuesta = _proponer(root)

    def _explota(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash simulado entre el witness y el CAS")

    monkeypatch.setattr(approval_module, "_escribir_ledger_cas", _explota)
    with pytest.raises(RuntimeError):
        _emitir(root, propuesta)
    monkeypatch.undo()
    assert cargar_ledger_aprobacion(root).disposition is ApprovalLedgerDisposition.ABSENT
    assert derivar_autoridad_aprobacion(root).max_issued_seq == 1
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.state is ApprovalLifecycle.ISSUED
    assert reconciliacion.ledger.last_issued_approval_id == propuesta.scope.approval_id


def test_crash_entre_tombstone_y_cas_no_resucita_el_token(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El tombstone manda: la aprobación ES terminal aunque la caché quedó atrás."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)

    def _explota(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash simulado entre el tombstone y el CAS")

    monkeypatch.setattr(approval_module, "_escribir_ledger_cas", _explota)
    with pytest.raises(RuntimeError):
        _consumir(root, propuesta)
    monkeypatch.undo()
    assert len(list(approvals_state_dir(root).iterdir())) == 1
    assert cargar_ledger_aprobacion(root).ledger.state is ApprovalLifecycle.ISSUED
    with pytest.raises(ApprovalLedgerRetrocedidoError):
        _consumir(root, propuesta, T2)
    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.cache_reconstruida is True
    assert reconciliacion.ledger is not None
    assert reconciliacion.ledger.state is ApprovalLifecycle.CONSUMED


def test_el_fallo_del_cas_post_tombstone_reconcilia_en_el_mismo_llamado(tmp_path: pathlib.Path, monkeypatch) -> None:
    """§39.3 C4 / §38.2: un fallo del CAS DESPUÉS del tombstone no descarta nada."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    original = approval_module._escribir_ledger_cas
    llamadas = {"n": 0}

    def _falla_una_vez(*args: object, **kwargs: object) -> object:
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise ApprovalCasError("CAS fallido simulado")
        return original(*args, **kwargs)

    monkeypatch.setattr(approval_module, "_escribir_ledger_cas", _falla_una_vez)
    resultado = _consumir(root, propuesta)
    assert resultado.cache_reconciled is True
    assert cargar_ledger_aprobacion(root).ledger.state is ApprovalLifecycle.CONSUMED


def test_el_create_del_witness_es_no_clobber(tmp_path: pathlib.Path) -> None:
    """§40.2: el ``CREATE`` del witness **es** la emisión — no-clobber, sin lock extra.

    Se ejerce el guard EN SÍ. El camino público llega al ``CREATE`` sólo cuando el
    ledger y la cadena de witnesses ya son coherentes, así que un ``open("w")`` en
    ese punto sería invisible para toda la suite: la emisión anterior se pisaría en
    silencio y el ABA de §40.2 —``R → R+1 → R``— volvería a ser indetectable. El
    mutante ``open(x) → open(w)`` sobrevivía a los 82 tests antes de este ancla.
    """
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    antes = ruta.read_bytes()
    intruso = approval_module.IssuanceWitness(
        schema_version=approval_module.ISSUANCE_WITNESS_SCHEMA_VERSION,
        root_id=approval_module._root_id(root),
        approval_seq=propuesta.scope.approval_seq,
        approval_id=str(uuid.uuid4()),
        approval_scope_digest="a" * 64,
        approval_provenance="ui:req-intruso",
        issued_at_ns=1,
        ledger_revision_at_issue=0,
    )
    with participant_lock(root, timeout=30) as handle, pytest.raises(ApprovalLedgerRetrocedidoError):
        approval_module._crear_witness(
            handle,
            root,
            intruso,
            ledger=approval_module._ledger_genesis(root),
        )
    # La evidencia de la emisión original quedó BYTE A BYTE intacta.
    assert ruta.read_bytes() == antes
    assert len(list(issuances_state_dir(root).iterdir())) == 1


def test_el_cas_rechaza_una_revision_stale_y_no_pisa_la_cache(tmp_path: pathlib.Path) -> None:
    """El CAS cubre al escritor que NO participa del root lock (§37.3/§45.10.1).

    El lock serializa a los participantes; el CAS existe para el que escribe por
    fuera. Sin el guard, el CAS es un no-op y la caché sufre el lost update que
    el diseño dice impedir: por eso se ejerce el guard EN SÍ, no el camino que lo
    rodea (los tests del crash lo monkeypatchean y nunca lo ejecutan).
    """
    root = _root(tmp_path)
    _emitir_fixture(root)
    actual = cargar_ledger_aprobacion(root).ledger
    assert actual is not None
    assert actual.approval_revision == 1
    with participant_lock(root, timeout=30) as handle:
        # Escritor ajeno: avanza la caché sin CAS (lost update en ciernes).
        approval_module._escribir_ledger(handle, root, dataclasses.replace(actual, approval_revision=7))
        with pytest.raises(ApprovalCasError, match="CAS fallido"):
            approval_module._escribir_ledger_cas(handle, root, esperado=1, nuevo=actual)
    # La escritura stale NO se publicó: la caché conserva la revisión del otro.
    assert cargar_ledger_aprobacion(root).ledger.approval_revision == 7


# ---------------------------------------------------------------------------
# Concurrencia real entre procesos
# ---------------------------------------------------------------------------


def test_dos_procesos_emitiendo_solo_uno_gana(tmp_path: pathlib.Path) -> None:
    """Single-winner por no-clobber del witness: una sola ``approval_seq``."""
    root = _root(tmp_path)
    resultados = _lanzar_concurrentes(
        "emitir",
        root,
        [{"provenance": f"ui:req-{indice}", "lock_timeout": 60.0} for indice in range(4)],
    )
    ganadores = [item for item in resultados if item["ok"]]
    perdedores = [item for item in resultados if not item["ok"]]
    assert len(ganadores) == 1, resultados
    assert ganadores[0]["approval_seq"] == 1
    assert len(perdedores) == 3
    for perdedor in perdedores:
        assert perdedor["error_type"].startswith("Approval"), perdedor["traza"]
    assert len(list(issuances_state_dir(root).iterdir())) == 1
    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.ISSUED
    assert ledger.last_issued_approval_seq == 1


def test_dos_procesos_consumiendo_solo_uno_gana(tmp_path: pathlib.Path) -> None:
    """El consumo es single-use incluso entre procesos (un solo tombstone)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    base = {
        "approval_id": propuesta.scope.approval_id,
        "approval_seq": propuesta.scope.approval_seq,
        "digest": propuesta.scope.approval_scope_digest,
        "lock_timeout": 60.0,
    }
    resultados = _lanzar_concurrentes(
        "consumir",
        root,
        [{**base, "transition_id": T1}, {**base, "transition_id": T2}],
    )
    ganadores = [item for item in resultados if item["ok"]]
    perdedores = [item for item in resultados if not item["ok"]]
    assert len(ganadores) == 1, resultados
    assert len(perdedores) == 1
    assert perdedores[0]["error_type"].startswith("Approval"), perdedores[0]["traza"]
    assert len(list(approvals_state_dir(root).iterdir())) == 1


# ---------------------------------------------------------------------------
# Namespace: symlinks, junctions y rutas fuera del root
# ---------------------------------------------------------------------------


def test_un_junction_en_issuances_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Un store redirigido NO se lee como «vacío legítimo» (§34.5 N2)."""
    root = _root(tmp_path)
    externo = tmp_path / "externo"
    externo.mkdir()
    destino = issuances_state_dir(root)
    shutil.rmtree(destino)
    tipo = _crear_enlace_de_directorio(destino, externo)
    _exigir_enlace_real(destino)
    with pytest.raises(ApprovalIdentityError, match="enlace/reparse|redirigido"):
        derivar_autoridad_aprobacion(root)
    assert tipo in {"symlink", "junction"}


def test_un_tombstone_que_es_enlace_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    destino = approvals_state_dir(root) / f"{propuesta.scope.approval_seq}-{propuesta.scope.approval_id}.json"
    _crear_enlace_de_directorio(destino, tmp_path / "externo")
    _exigir_enlace_real(destino)
    with pytest.raises(ApprovalCorruptError, match="enlace/reparse"):
        derivar_autoridad_aprobacion(root)


def test_el_ledger_que_es_enlace_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _emitir_fixture(root)
    externo = tmp_path / "externo"
    externo.mkdir()
    ruta = approval_ledger_path(root)
    ruta.unlink()
    _crear_enlace_de_directorio(ruta, externo)
    _exigir_enlace_real(ruta)
    with pytest.raises(ApprovalCorruptError, match="enlace/reparse"):
        cargar_ledger_aprobacion(root)


def test_el_store_no_inicializado_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """El camino de escritura no crea directorios: el layout es del storage."""
    root = _root(tmp_path)
    propuesta = _proponer(root)
    shutil.rmtree(issuances_state_dir(root))
    with pytest.raises(ApprovalStoreNoInicializadoError):
        _emitir(root, propuesta)


# ---------------------------------------------------------------------------
# Identidad de root (§37.3): la evidencia de OTRO root no es evidencia local
# ---------------------------------------------------------------------------


def _trasplantar_evidencia(origen: pathlib.Path, destino: pathlib.Path) -> None:
    """Copia los TRES artefactos de la autoridad de un root a otro.

    Es el escenario que el cross-check de ``root_id`` existe para atajar: no un
    archivo suelto, sino el ``state/`` completo de la autoridad —ledger, cadena
    de witnesses y tombstones— movido a otro FrozenRuntimeRoot. Todo es
    internamente coherente, así que ninguna validación de forma lo distingue.
    """
    shutil.copytree(issuances_state_dir(origen), issuances_state_dir(destino), dirs_exist_ok=True)
    shutil.copytree(approvals_state_dir(origen), approvals_state_dir(destino), dirs_exist_ok=True)
    shutil.copy2(approval_ledger_path(origen), approval_ledger_path(destino))


def test_el_ledger_de_otro_root_no_se_lee_como_local(tmp_path: pathlib.Path) -> None:
    """``root_id`` se escribe en el ledger, así que se VERIFICA al leer (§37.3).

    Un campo que sólo se escribe es decoración: el ledger de otro root es
    coherente consigo mismo y pasaría cualquier validación de forma.
    """
    origen = _root(tmp_path, "origen")
    destino = _root(tmp_path, "destino")
    _emitir_fixture(origen)
    shutil.copy2(approval_ledger_path(origen), approval_ledger_path(destino))
    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        cargar_ledger_aprobacion(destino)


def test_el_witness_de_otro_root_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """La cadena de emisiones es autoridad LOCAL del root (§40.2)."""
    origen = _root(tmp_path, "origen")
    destino = _root(tmp_path, "destino")
    _emitir_fixture(origen)
    shutil.copytree(issuances_state_dir(origen), issuances_state_dir(destino), dirs_exist_ok=True)
    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        derivar_autoridad_aprobacion(destino)


def test_una_aprobacion_de_otro_root_no_se_consume(tmp_path: pathlib.Path) -> None:
    """La consecuencia material: la aprobación del root A no autoriza al root B.

    Con los tres artefactos trasplantados, sin el cross-check el ``CONSUME`` del
    root B encontraba ledger, witness y digest coincidentes y creaba un tombstone
    CONSUMED local: una promoción aprobada para A habilitaba una transición en B.
    """
    origen = _root(tmp_path, "origen")
    destino = _root(tmp_path, "destino")
    propuesta, _ = _emitir_fixture(origen)
    _trasplantar_evidencia(origen, destino)
    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        _consumir(destino, propuesta)
    # La evidencia terminal NO se creó: el rechazo ocurre antes del write-ahead.
    assert list(approvals_state_dir(destino).iterdir()) == []
    assert list(approvals_state_dir(origen).iterdir()) == []


def test_reconciliar_con_evidencia_de_otro_root_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """La reconciliación tampoco adopta la caché de otro root."""
    origen = _root(tmp_path, "origen")
    destino = _root(tmp_path, "destino")
    _emitir_fixture(origen)
    _trasplantar_evidencia(origen, destino)
    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        reconciliar_aprobacion(destino)


# ---------------------------------------------------------------------------
# UNKNOWN != SUCCESS
# ---------------------------------------------------------------------------


def test_verificar_consumo_sin_evidencia_terminal_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """``PENDING_WITHOUT_APPROVAL_PROOF = PROHIBIDO`` (§36.6)."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalIdentityError, match="no hay evidencia terminal"):
        verificar_consumo_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            transition_id=T1,
        )


def test_verificar_consumo_de_una_aprobacion_revocada_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="cancelada",
    )
    with pytest.raises(ApprovalIdentityError, match="REVOKED"):
        verificar_consumo_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            transition_id=T1,
        )


def test_verificar_consumo_con_otra_transicion_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta, T1)
    with pytest.raises(ApprovalIdentityError, match="no por"):
        verificar_consumo_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            transition_id=T2,
        )


# ---------------------------------------------------------------------------
# Integración P4-S3 ↔ P4-S4 y activación deshabilitada
# ---------------------------------------------------------------------------


def _propuesta_transicion(propuesta: PropuestaAprobacion, transition_id: str = T1) -> object:
    return journal_module.PropuestaTransicion(
        transition_id=transition_id,
        operation=journal_module.JournalOperation.PROMOTION,
        source_activation=propuesta.scope.source_activation,
        source_activation_digest=propuesta.scope.source_activation_digest,
        target_generation_id=propuesta.scope.generation_id,
        target_clone_id=propuesta.scope.clone_id,
        target_runtime_setup_id=propuesta.scope.runtime_setup_id,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        evidence_digests=journal_module.EvidenceDigests(
            clone_evidence=propuesta.scope.clone_evidence,
            runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
            compatibility_evidence=propuesta.scope.compatibility_evidence_id,
        ),
        expected_effective_path=f"E:/rt/versions/{propuesta.scope.generation_id}",
        created_at_ns=1_700_000_000_000_000_000,
    )


def test_la_evidencia_de_consumo_alimenta_el_create_del_journal(tmp_path: pathlib.Path) -> None:
    """Puente real P4-S3 ↔ P4-S4: el journal acepta la aprobación consumida.

    Se ejercita la maquinaria interna del journal (el ``CREATE`` de producción
    sigue bloqueado): la evidencia sale de la autoridad durable real, no de un
    objeto inventado por el test.
    """
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    resultado = _consumir(root, propuesta, T1)
    evidencia = verificar_consumo_aprobacion(
        root,
        approval_id=resultado.approval_id,
        approval_seq=resultado.approval_seq,
        approval_scope_digest=resultado.scope_digest,
        transition_id=T1,
    )
    doc = journal_module._crear_pendiente_bajo_lock(root, _propuesta_transicion(propuesta), evidencia)
    assert doc.state is journal_module.JournalState.PENDING_PROMOTION
    assert doc.consumed_approval_id == propuesta.scope.approval_id
    assert doc.consumed_scope_digest == propuesta.scope.approval_scope_digest


def test_el_journal_rechaza_una_aprobacion_no_consumida(tmp_path: pathlib.Path) -> None:
    """La precondición del CREATE es real: una aprobación ISSUED no autoriza nada."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    with pytest.raises(ApprovalIdentityError, match="no hay evidencia terminal"):
        verificar_consumo_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            transition_id=T1,
        )


def test_la_activacion_productiva_sigue_deshabilitada(tmp_path: pathlib.Path) -> None:
    """Evidencia de que P4-S4 NO habilita PROMOTION ni ROLLBACK."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta, T1)
    with pytest.raises(JournalDependenciaNoDisponibleError):
        journal_module.iniciar_transicion_pendiente(root, _propuesta_transicion(propuesta))
    with pytest.raises(JournalDependenciaNoDisponibleError):
        journal_module.iniciar_transicion_rollback(root, _propuesta_transicion(propuesta))
    with pytest.raises(ApprovalHitolNoDisponibleError):
        emitir_aprobacion(root, _proponer(root, approval_provenance="ui:req-2"), _consentimiento(propuesta))


def test_el_slice_no_toca_el_runtime_efectivo(tmp_path: pathlib.Path) -> None:
    """P4-S4 escribe SÓLO dentro de ``state/``: no hay bind ni Effective Runtime."""
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    _consumir(root, propuesta)
    archivos = sorted(ruta.relative_to(root).as_posix() for ruta in root.rglob("*") if ruta.is_file())
    assert all(ruta.startswith("state/") for ruta in archivos), archivos
    assert "state/approval.json" in archivos
    assert f"state/issuances/{propuesta.scope.approval_seq}.json" in archivos
    assert f"state/approvals/{propuesta.scope.approval_seq}-{propuesta.scope.approval_id}.json" in archivos
    # Ni un bind, ni un Effective Runtime, ni una promoción: `active.json` intacto.
    assert _leer_json(root / "state" / "active.json")["desired_active_generation"] is None
    # ``list()``, no ``iterdir()``: un generador es siempre truthy y la aserción
    # pasaría aunque el directorio tuviera generaciones.
    assert not list((root / "versions").iterdir())


# ---------------------------------------------------------------------------
# Hardening de identidad: el root esperado no es opcional ni al escribir
# ---------------------------------------------------------------------------


def test_parsear_ledger_exige_root_esperado(tmp_path: pathlib.Path) -> None:
    """Ningún caller nuevo puede parsear el ledger sin identidad de root."""
    with pytest.raises(TypeError, match="root_id_esperado"):
        approval_module._parsear_ledger({}, tmp_path / "approval.json")


def test_parsear_witness_exige_root_esperado(tmp_path: pathlib.Path) -> None:
    """El parser de emisiones no admite volver al modo fail-open."""
    with pytest.raises(TypeError, match="root_id_esperado"):
        approval_module._parsear_witness({}, tmp_path / "1.json", seq_esperada=1)


def test_guard_root_no_admite_esperado_none(tmp_path: pathlib.Path) -> None:
    """Incluso un caller Python no tipado que mande None debe fallar cerrado."""
    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        approval_module._exigir_root_local("root-ajeno", esperado=None, ruta=tmp_path / "approval.json")


def test_crear_witness_ajeno_falla_antes_de_escribir(tmp_path: pathlib.Path) -> None:
    """La escritura no confía en la identidad que le entrega su propio caller."""
    destino = _root(tmp_path, "destino")
    origen = _root(tmp_path, "origen")
    witness = approval_module.IssuanceWitness(
        schema_version=approval_module.ISSUANCE_WITNESS_SCHEMA_VERSION,
        root_id=approval_module._root_id(origen),
        approval_seq=1,
        approval_id=T1,
        approval_scope_digest="a" * 64,
        approval_provenance="ui:solicitud-de-otro-root",
        issued_at_ns=1,
        ledger_revision_at_issue=0,
    )
    antes = _instantanea_state(destino)
    with (
        participant_lock(destino, timeout=0.0) as handle,
        pytest.raises(ApprovalRootAjenoError, match="root_id"),
    ):
        approval_module._crear_witness(handle, destino, witness, ledger=approval_module._ledger_genesis(destino))
    assert _instantanea_state(destino) == antes
    assert not (issuances_state_dir(destino) / "1.json").exists()


# ---------------------------------------------------------------------------
# Fuente de activación: el hermano del validador del journal
# ---------------------------------------------------------------------------


def test_una_fuente_parcial_no_se_puede_aprobar(tmp_path: pathlib.Path) -> None:
    """§36.3: una fuente a medias no es una observación.

    ``journal._validar_source_activation`` ya rechaza un ``SourceActivation`` con
    tres de sus cuatro campos. El validador de la aprobación sólo miraba la forma
    de los campos PRESENTES y aceptaba el parcial, así que la asimetría tenía un
    costo concreto y asimétrico: la aprobación se emitía, se QUEMABA al consumirse
    (burn-first) y recién el ``CREATE`` del journal la rechazaba por parcial — el
    dueño quedaba sin aprobación y sin transición.
    """
    root = _root(tmp_path)
    with pytest.raises(ApprovalSchemaError, match="PARCIAL"):
        _proponer(root, source_activation=_fuente(effective_runtime_evidence=None))


def test_la_fuente_vacia_sigue_siendo_legitima(tmp_path: pathlib.Path) -> None:
    """El arranque limpio (§36.8) es los CUATRO en null: rechazar el parcial no
    puede degenerar en rechazar el bootstrap."""
    root = _root(tmp_path)
    propuesta = _proponer(
        root,
        source_activation=SourceActivation(
            generation_id=None,
            clone_id=None,
            runtime_setup_id=None,
            effective_runtime_evidence=None,
        ),
    )
    assert propuesta.scope.source_activation.generation_id is None


def test_el_ledger_que_no_coincide_con_su_witness_se_reconstruye(tmp_path: pathlib.Path) -> None:
    """La caché puede quedar ATRÁS, pero no puede CONTRADECIR a su autoridad.

    Un ``approval.json`` restaurado con la misma ``approval_seq``, el mismo
    lifecycle y una revisión alta —pero con OTRO ``approval_id``— satisface
    ``max(H,T) == last_issued`` y la clasificación devolvía ``CONSISTENT``.

    El daño no es de autorización sino de bloqueo permanente: la propuesta ve una
    aprobación viva, el consumo rechaza tanto el id del ledger como el del
    witness, y la reconciliación se niega a reconstruir una caché que §37.6
    declara derivable de la evidencia durable. El witness es la autoridad; si
    discrepan, la caché se reescribe.
    """
    root = _root(tmp_path)
    propuesta, _ = _emitir_fixture(root)
    ruta = approval_ledger_path(root)
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    payload["last_issued_approval_id"] = T2
    ruta.write_text(json.dumps(payload), encoding="utf-8")

    veredicto, _motivo = clasificar_autoridad(cargar_ledger_aprobacion(root), derivar_autoridad_aprobacion(root))
    assert veredicto is not VeredictoAutoridad.CONSISTENT

    reconciliacion = reconciliar_aprobacion(root)
    assert reconciliacion.cache_reconstruida is True
    reconstruido = cargar_ledger_aprobacion(root).ledger
    assert reconstruido is not None
    assert reconstruido.last_issued_approval_id == propuesta.scope.approval_id
