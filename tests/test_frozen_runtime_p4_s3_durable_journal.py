"""Tests RED-first para P4-S3: journal durable de transición (ADR 0012).

Autoridad normativa: §34.4 (`P4_DURABLE_TRANSITION`), §34.5 (reconciliación de
arranque), §35.6 (evidencia durable de finalización + no-clobber), §36.6 (CAS),
§36.9/§36.10 (matriz de crashes y ciclo de vida), §37.5 (store `FINALIZED`),
§37.6 (concurrencia), §38.3 (`finalization_seq` derivado + reserva).

Este slice NO implementa promoción, rollback ni binding: construye las
primitivas durables (lectura/validación/clasificación, persistencia atómica,
CAS, historial inmutable, reserva de secuencia y reconciliación fail-closed).
La creación de un `PENDING` de producción queda BLOQUEADA porque la autoridad
durable de aprobación consumida (§36.5/§37.3) todavía no existe; los tests
ejercitan la maquinaria interna con evidencia explícita (fault injection),
nunca con una aprobación simulada en la ruta de producción.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.errors import (
    JournalCasError,
    JournalCorruptError,
    JournalDependenciaNoDisponibleError,
    JournalEvidenciaPostError,
    JournalIdentityError,
    JournalObservacionEffectiveError,
    JournalSchemaError,
    JournalStoreNoInicializadoError,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import (
    finalization_seqs_state_dir,
    initialize_frozen_runtime_storage,
    transition_journal_path,
    transitions_state_dir,
)

WORKTREE = pathlib.Path(__file__).resolve().parents[1]
ENTORNO_HIJO = {
    **os.environ,
    "PYTHONPATH": str(WORKTREE),
    "SKYCLAW_P4_S3_WORKTREE": str(WORKTREE),
}

# Identidades UUID v4 válidas y fijas (determinismo de digests).
T1 = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90"
T2 = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
T3 = "2b3c4d5e-6f70-4818-9a9b-c0d1e2f3a4b5"
APROBACION = "3c4d5e6f-7081-4242-9b8c-7d6e5f4a3b2c"
SESION = "4d5e6f70-8192-4344-9c8d-7e6f5a4b3c2d"

GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
#: Generation-id CONFORME y distinto de los dos anteriores. Reemplaza a los
#: fixtures que usaban ids sintácticamente inválidos (p. ej. `"1.6.1170__otra"`)
#: para expresar "distinto": con la validación canónica de R3-01 esos casos
#: pasaban a rechazarse por FORMATO y dejaban de ejercitar el mismatch que
#: declaran.
GEN_TERCERA = "1.6.1170.0__ffffffffffff"
# Identidad del Runtime Clone efectivo que el bind produce (ADR §34.4:
# "expected_effective_path — la ruta que el bind va a producir"). El oráculo
# de P5 observa QUÉ RUTA ejecuta el runtime (§34.5), no la Generation: una
# Generation a secas no demuestra identidad de Clone (§35.4).
RUTA_EFECTIVA_OBJETIVO = "E:/rt/versions/" + GEN_OBJETIVO
DIGEST_64 = "a" * 64
OTRO_DIGEST_64 = "0" * 64

# payloads de referencia (el test escribe el documento crudo: los tests de
# parseo no deben depender del serializador que se está probando).


def _digest(payload: object) -> str:
    canonico = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _fuente(generation: str = GEN_ORIGEN) -> dict:
    return {
        "generation_id": generation,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": "b" * 64,
    }


def _fuente_objetivo() -> dict:
    """Source ya promovido a ``GEN_OBJETIVO`` (escenarios de autoridad cruzada)."""
    return {
        "generation_id": GEN_OBJETIVO,
        "clone_id": "clone-objetivo",
        "runtime_setup_id": "setup-objetivo",
        "effective_runtime_evidence": "b" * 64,
    }


def _payload_pendiente(
    *,
    transition_id: str = T1,
    revision: int = 0,
    operation: str = "PROMOTION",
    scope_digest: str = "c" * 64,
    fuente: dict | None = None,
) -> dict:
    fuente = _fuente() if fuente is None else fuente
    return {
        "schema_version": 1,
        "state": f"PENDING_{operation}",
        "journal_revision": revision,
        "operation": operation,
        "transition_id": transition_id,
        "source_activation": fuente,
        "source_activation_digest": _digest(fuente),
        "target_generation_id": GEN_OBJETIVO,
        "target_clone_id": "clone-objetivo",
        "target_runtime_setup_id": "setup-objetivo",
        "approval_scope_digest": scope_digest,
        "consumed_approval_id": APROBACION,
        "consumed_approval_seq": 12,
        "consumed_scope_digest": scope_digest,
        "evidence_digests": {
            "clone_evidence": "d" * 64,
            "runtime_setup_evidence": "e" * 64,
            "compatibility_evidence": "f" * 64,
        },
        "expected_effective_path": "E:/rt/versions/" + GEN_OBJETIVO,
        "created_at_ns": 1_700_000_000_000_000_000,
        "lock_session_id": SESION,
    }


def _payload_none(*, revision: int = 1, witness: str | None = T1) -> dict:
    payload: dict = {
        "schema_version": 1,
        "state": "NONE",
        "journal_revision": revision,
        "created_at_ns": 1_700_000_000_000_000_000,
        "lock_session_id": SESION,
    }
    if witness is not _SIN_TESTIGO:
        payload["last_finalized_transition_id"] = witness
    return payload


_SIN_TESTIGO = object()  # centinela: journal NONE sin campo testigo


def _payload_registro(
    *,
    transition_id: str = T1,
    seq: int = 1,
    previa: str | None = None,
    scope_digest: str = "c" * 64,
) -> dict:
    fuente = _fuente()
    return {
        "schema_version": 1,
        "record_state": "FINALIZED",
        "transition_id": transition_id,
        "finalization_seq": seq,
        "finalized_from_transition_id": previa,
        "operation": "PROMOTION",
        "source_activation": fuente,
        "source_activation_digest": _digest(fuente),
        "previous_activation_target": fuente,
        "target_generation_id": GEN_OBJETIVO,
        "target_clone_id": "clone-objetivo",
        "target_runtime_setup_id": "setup-objetivo",
        "approval_scope_digest": scope_digest,
        "consumed_approval_id": APROBACION,
        "consumed_approval_seq": 12,
        "post_verify_evidence_digest": "1" * 64,
        "post_verified_at_ns": 1_700_000_000_000_000_001,
        "finalized_at_ns": 1_700_000_000_000_000_002,
    }


def _payload_reserva(root: pathlib.Path, *, seq: int = 1, transition_id: str = T1) -> dict:
    from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

    return {
        "schema_version": 1,
        "root_id": canonical_root_key(root)[0],
        "finalization_seq": seq,
        "transition_id": transition_id,
    }


def _escribir_json(ruta: pathlib.Path, payload: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _root(tmp_path: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = tmp_path / nombre
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success, resultado.admission.message
    return root


def _quitar_enlace_sin_seguir(ruta: pathlib.Path) -> None:
    """Borra *ruta* si existe, sin seguir el enlace (el destino queda intacto).

    Tolerante a la ruta AUSENTE: el caller limpia el remanente de un intento de
    enlace que puede no haber creado NADA (``mklink /J`` sobre un volumen
    no-NTFS o una ruta de red). Ahí "no hay nada que limpiar" es el caso normal;
    sin esta tolerancia el fallback terminaría en ``FileNotFoundError`` —un ERROR
    de test— en vez del ``pytest.skip`` documentado.
    """
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError:
        tipo = None
    if tipo is None and not ruta.exists():
        return
    if tipo is not None and (tipo == "junction" or ruta.is_dir()):
        os.rmdir(ruta)  # quita el reparse point, NUNCA el destino
        return
    if tipo is None and ruta.is_dir():
        shutil.rmtree(ruta, ignore_errors=True)
        return
    ruta.unlink()


def _crear_enlace_de_directorio(destino: pathlib.Path, origen: pathlib.Path) -> str:
    """Crea un enlace de DIRECTORIO real en *destino*; devuelve ``"symlink"`` o ``"junction"``.

    ``os.symlink`` es el camino primario (POSIX y Windows con Developer Mode).
    Un régimen que intercepte ``os.symlink`` y materialice una COPIA no produce
    enlace alguno, y el caso quedaría **sin ejercitar** en silencio. Ahí se cae a
    un **junction** de NTFS (``mklink /J``), que sí es un reparse point real: el
    mismo que ``link_kind_and_identity_or_raise`` clasifica como ``junction`` y
    que el código de producción trata igual que un symlink (ambos ⇒
    ``link_kind_or_raise`` != ``None``). Si ninguna vía produce un enlace real,
    el test se omite con un motivo explícito — nunca se asume que "se ejecutó"
    porque el contador global de ``skipped`` no subió.
    """
    try:
        os.symlink(origen, destino, target_is_directory=True)
    except OSError:
        pass
    else:
        if link_kind_or_raise(destino) == "symlink":
            return "symlink"
        _quitar_enlace_sin_seguir(destino)  # era una copia, no un enlace
    if os.name == "nt":
        # Sin `shell=True` y con la lista de argumentos, no hay interpretación de
        # shell: `destino`/`origen` salen de `tmp_path` (pytest), no de entrada
        # externa. El timeout evita que un `cmd` colgado bloquee el test hasta el
        # timeout global.
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
    """Aserta que *ruta* ES un enlace real y devuelve su tipo (evidencia por test)."""
    tipo = link_kind_or_raise(ruta)
    assert tipo is not None, f"{ruta} debía ser un enlace real y no lo es: el caso no se ejerció"
    return tipo


def _propuesta(transition_id: str = T1, *, operation: str = "PROMOTION") -> object:
    from sky_claw.local.frozen_runtime.journal import (
        EvidenceDigests,
        JournalOperation,
        PropuestaTransicion,
        SourceActivation,
    )

    fuente = _fuente()
    return PropuestaTransicion(
        transition_id=transition_id,
        operation=JournalOperation(operation),
        source_activation=SourceActivation(
            generation_id=GEN_ORIGEN,
            clone_id="clone-origen",
            runtime_setup_id="setup-origen",
            effective_runtime_evidence="b" * 64,
        ),
        source_activation_digest=_digest(fuente),
        target_generation_id=GEN_OBJETIVO,
        target_clone_id="clone-objetivo",
        target_runtime_setup_id="setup-objetivo",
        approval_scope_digest="c" * 64,
        evidence_digests=EvidenceDigests(
            clone_evidence="d" * 64,
            runtime_setup_evidence="e" * 64,
            compatibility_evidence="f" * 64,
        ),
        expected_effective_path=RUTA_EFECTIVA_OBJETIVO,
        created_at_ns=1_700_000_000_000_000_000,
    )


def _propuesta_hacia(
    transition_id: str,
    *,
    target_generation: str,
    target_clone: str,
    target_setup: str,
) -> object:
    """Propuesta con un target EXPLÍCITO (escenarios de autoridad cruzada).

    ``_propuesta`` fija el target a ``GEN_OBJETIVO``; para probar que un tip de
    OTRA Generation no es autoridad del Clone hace falta poder finalizar una
    transición cuyo target sea otra Generation.
    """
    from sky_claw.local.frozen_runtime.journal import (
        EvidenceDigests,
        JournalOperation,
        PropuestaTransicion,
        SourceActivation,
    )

    fuente = _fuente_objetivo()
    return PropuestaTransicion(
        transition_id=transition_id,
        operation=JournalOperation("PROMOTION"),
        source_activation=SourceActivation(
            generation_id=GEN_OBJETIVO,
            clone_id="clone-objetivo",
            runtime_setup_id="setup-objetivo",
            effective_runtime_evidence="b" * 64,
        ),
        source_activation_digest=_digest(fuente),
        target_generation_id=target_generation,
        target_clone_id=target_clone,
        target_runtime_setup_id=target_setup,
        approval_scope_digest="c" * 64,
        evidence_digests=EvidenceDigests(
            clone_evidence="d" * 64,
            runtime_setup_evidence="e" * 64,
            compatibility_evidence="f" * 64,
        ),
        expected_effective_path="E:/rt/versions/" + target_generation,
        created_at_ns=1_700_000_000_000_000_000,
    )


def _aprobacion(transition_id: str = T1) -> object:
    from sky_claw.local.frozen_runtime.journal import AprobacionConsumida

    return AprobacionConsumida(
        approval_id=APROBACION,
        approval_seq=12,
        scope_digest="c" * 64,
        consumed_by_transition_id=transition_id,
    )


def _post(
    transition_id: str = T1,
    *,
    resultado: str = "PASS",
    target_generation: str = GEN_OBJETIVO,
    revision: int = 0,
    desired: str | None = GEN_OBJETIVO,
    target_clone: str = "clone-objetivo",
    target_setup: str = "setup-objetivo",
    fuente: dict | None = None,
) -> object:
    from sky_claw.local.frozen_runtime.journal import PostVerifyEvidence

    return PostVerifyEvidence(
        transition_id=transition_id,
        resultado=resultado,
        source_activation_digest=_digest(_fuente() if fuente is None else fuente),
        target_generation_id=target_generation,
        target_clone_id=target_clone,
        target_runtime_setup_id=target_setup,
        desired_active_generation=desired,
        effective_observation_digest="1" * 64,
        payload_digest="2" * 64,
        verified_at_ns=1_700_000_000_000_000_001,
        expected_journal_revision=revision,
    )


def _revision_vigente(root: pathlib.Path) -> int:
    """Revisión del journal vigente: la que un POST honesto declara (§36.6)."""
    snapshot = journal_module.cargar_journal_transicion(root)
    if snapshot.journal is None:
        return 0
    return snapshot.journal.journal_revision


def _post_para(root: pathlib.Path, transition_id: str = T1, **cambios: object) -> object:
    """Evidencia POST calculada sobre la revisión VIGENTE (el caso honesto)."""
    return _post(transition_id, revision=_revision_vigente(root), **cambios)


def _observacion(
    *,
    generation: str | None = GEN_OBJETIVO,
    clone: str | None = "clone-objetivo",
    path: str | None = RUTA_EFECTIVA_OBJETIVO,
) -> object:
    """Observación tipada del Effective Runtime (vocabulario único N0/PENDING)."""
    from sky_claw.local.frozen_runtime.journal import ObservacionEffective

    return ObservacionEffective(generation_id=generation, clone_id=clone, effective_path=path)


def _crear_pendiente(root: pathlib.Path, propuesta: object = None, aprobacion: object = None) -> object:
    """Driver de test: crea el PENDING vía la maquinaria interna bajo el lock."""
    from sky_claw.local.frozen_runtime.journal import _crear_pendiente_bajo_lock

    return _crear_pendiente_bajo_lock(root, propuesta or _propuesta(), aprobacion or _aprobacion())


def _finalizar(root: pathlib.Path, evidencia: object = None) -> object:
    """Driver de test: ejecuta la finalización (F1a/F1b/F2) bajo el lock."""
    from sky_claw.local.frozen_runtime.journal import _finalizar_transicion_bajo_lock

    return _finalizar_transicion_bajo_lock(root, evidencia or _post_para(root))


def _start_next(root: pathlib.Path, propuesta: object = None, aprobacion: object = None) -> object:
    from sky_claw.local.frozen_runtime.journal import _start_next_bajo_lock

    return _start_next_bajo_lock(root, propuesta or _propuesta(T2), aprobacion or _aprobacion(T2))


def _dump_estado(root: pathlib.Path) -> dict:
    journal = transition_journal_path(root)
    return {
        "journal_existe": journal.exists(),
        "journal_bytes": journal.read_bytes() if journal.exists() else None,
        "registros": sorted(p.name for p in transitions_state_dir(root).glob("*.json")),
        "reservas": sorted(p.name for p in finalization_seqs_state_dir(root).glob("*.json")),
    }


# ============================================================================
# Integridad: ausente ≠ corrupto, schema estricta, campos obligatorios
# ============================================================================


def test_journal_ausente_es_arranque_limpio_y_no_es_corrupcion(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "ABSENT"
    assert snapshot.journal is None
    # La ausencia NUNCA se degrada a NONE con transición inventada.
    assert snapshot.journal is None


def test_journal_corrupto_nunca_se_lee_como_none(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    casos = {
        "json_malformado": b'{"schema_version": 1, "state": "NONE"',
        "vacio": b"",
        "blanco": b"   \n  ",
        "no_utf8": b"\xff\xfe\x00bad",
        "no_objeto": b"[1, 2, 3]",
        "string_json": b'"NONE"',
    }
    for nombre, contenido in casos.items():
        ruta.write_bytes(contenido)
        with pytest.raises(JournalCorruptError):
            journal_module.cargar_journal_transicion(root)
        # El intento de reconciliación también falla cerrado (N2).
        resultado = journal_module.reconciliar_journal(root)
        assert resultado.veredicto.value == "FAIL_CLOSED", nombre


def test_esquema_desconocido_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    payload = _payload_none(revision=3, witness=None)
    payload["schema_version"] = 2
    _escribir_json(transition_journal_path(root), payload)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)


def test_campo_obligatorio_ausente_rechaza(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    for campo in (
        "schema_version",
        "state",
        "journal_revision",
        "operation",
        "transition_id",
        "source_activation",
        "source_activation_digest",
        "target_generation_id",
        "target_clone_id",
        "target_runtime_setup_id",
        "approval_scope_digest",
        "consumed_approval_id",
        "consumed_approval_seq",
        "consumed_scope_digest",
        "evidence_digests",
        "expected_effective_path",
        "created_at_ns",
        "lock_session_id",
    ):
        payload = _payload_pendiente()
        del payload[campo]
        _escribir_json(ruta, payload)
        with pytest.raises((JournalSchemaError, JournalIdentityError)):
            journal_module.cargar_journal_transicion(root)


def test_tipos_invalidos_rechazan(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    mutaciones: dict[str, object] = {
        "journal_revision": -1,
        "journal_revision_str": None,
        "created_at_ns_negativo": None,
        "consumed_approval_seq_str": None,
        "state_desconocido": "PENDING_CANCEL",
        "operation_desconocida": "FREEZE",
    }
    base = _payload_pendiente()
    base["journal_revision"] = "cero"
    _escribir_json(ruta, base)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)

    base = _payload_pendiente()
    base["created_at_ns"] = -5
    _escribir_json(ruta, base)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)

    base = _payload_pendiente()
    base["consumed_approval_seq"] = "12"
    _escribir_json(ruta, base)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)

    base = _payload_pendiente()
    base["state"] = "PENDING_CANCEL"
    _escribir_json(ruta, base)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)

    base = _payload_pendiente()
    base["operation"] = "FREEZE"
    _escribir_json(ruta, base)
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)
    assert mutaciones  # el mapa documenta las familias cubiertas arriba


@pytest.mark.parametrize(
    "transition_id",
    [
        T1.upper(),
        T1.replace("-", ""),
        "no-es-uuid",
        str(uuid.uuid1()),  # v1 no es v4
        "8f4e1a90-5ac1-1b6d-9c21-0d2b8f4e1a90",  # versión 1
    ],
)
def test_transition_id_debe_ser_uuid_v4_canonico(tmp_path: pathlib.Path, transition_id: str) -> None:
    root = _root(tmp_path)
    _escribir_json(transition_journal_path(root), _payload_pendiente(transition_id=transition_id))
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_digests_deben_ser_sha256_hex(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    for campo in ("source_activation_digest", "approval_scope_digest", "consumed_scope_digest"):
        payload = _payload_pendiente()
        payload[campo] = "zz" + DIGEST_64[2:]
        _escribir_json(ruta, payload)
        with pytest.raises((JournalSchemaError, JournalIdentityError)):
            journal_module.cargar_journal_transicion(root)
    payload = _payload_pendiente()
    payload["evidence_digests"]["clone_evidence"] = "corto"
    _escribir_json(ruta, payload)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_source_activation_digest_debe_ligar_el_contenido(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    payload = _payload_pendiente()
    payload["source_activation_digest"] = OTRO_DIGEST_64
    _escribir_json(transition_journal_path(root), payload)
    with pytest.raises(JournalIdentityError):
        journal_module.cargar_journal_transicion(root)


def test_source_activation_y_evidence_no_admiten_campos_extra(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    payload = _payload_pendiente()
    payload["source_activation"]["extra"] = "campo-inventado"
    _escribir_json(ruta, payload)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)
    payload = _payload_pendiente()
    payload["evidence_digests"]["extra"] = DIGEST_64
    _escribir_json(ruta, payload)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_journal_none_no_admite_campos_de_transicion(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    payload = _payload_none(revision=2, witness=T1)
    payload["transition_id"] = T1
    _escribir_json(ruta, payload)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


# ============================================================================
# Historial FINALIZED: cadena, contigüidad, anti-fork, testigo, reservas
# ============================================================================


def test_store_vacio_next_es_uno_y_latest_none(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    vista = journal_module.derivar_historial_finalizados(root)
    assert vista.registros == ()
    assert vista.next_seq == 1
    assert vista.latest is None


def test_cadena_contigua_valida_y_next_derivado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1, previa=None))
    _escribir_json(transitions_state_dir(root) / f"{T2}.json", _payload_registro(transition_id=T2, seq=2, previa=T1))
    _escribir_json(transitions_state_dir(root) / f"{T3}.json", _payload_registro(transition_id=T3, seq=3, previa=T2))
    vista = journal_module.derivar_historial_finalizados(root)
    assert tuple(r.transition_id for r in vista.registros) == (T1, T2, T3)
    assert vista.next_seq == 4
    assert vista.latest is not None and vista.latest.transition_id == T3
    assert vista.frontera_seq == 1


def test_registro_incompatible_con_su_nombre_rechaza(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T2, seq=1))
    with pytest.raises(JournalIdentityError):
        journal_module.derivar_historial_finalizados(root)


def test_registro_corrupto_nunca_se_saltea(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    (transitions_state_dir(root) / f"{T2}.json").write_text("{roto", encoding="utf-8")
    with pytest.raises(JournalCorruptError):
        journal_module.derivar_historial_finalizados(root)


def test_historial_rechaza_duplicado_de_seq(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=7))
    _escribir_json(transitions_state_dir(root) / f"{T2}.json", _payload_registro(transition_id=T2, seq=7))
    with pytest.raises(JournalIdentityError):
        journal_module.derivar_historial_finalizados(root)


def test_historial_rechaza_hueco_interior(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    _escribir_json(transitions_state_dir(root) / f"{T3}.json", _payload_registro(transition_id=T3, seq=3, previa=T1))
    with pytest.raises(JournalIdentityError):
        journal_module.derivar_historial_finalizados(root)


def test_historial_rechaza_predecesor_roto(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    _escribir_json(
        transitions_state_dir(root) / f"{T2}.json",
        _payload_registro(transition_id=T2, seq=2, previa=T3),
    )
    with pytest.raises(JournalIdentityError):
        journal_module.derivar_historial_finalizados(root)


def test_frontera_de_retencion_admite_predecesor_fuera_de_ventana(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    # Truncamiento frontal lícito: la frontera puede apuntar fuera de la ventana.
    _escribir_json(
        transitions_state_dir(root) / f"{T2}.json",
        _payload_registro(transition_id=T2, seq=5, previa=T1),
    )
    _escribir_json(
        transitions_state_dir(root) / f"{T3}.json",
        _payload_registro(transition_id=T3, seq=6, previa=T2),
    )
    vista = journal_module.derivar_historial_finalizados(root)
    assert vista.frontera_seq == 5
    assert vista.next_seq == 7
    assert vista.latest is not None and vista.latest.transition_id == T3


def test_journal_none_sin_testigo_con_store_no_vacio_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transition_journal_path(root), _payload_none(revision=1, witness=_SIN_TESTIGO))
    # Store COHERENTE (registro con su reserva): la única anomalía es el testigo.
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", _payload_reserva(root, seq=1, transition_id=T1))
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "JOURNAL_NONE_SIN_TESTIGO_CON_STORE_NO_VACIO" in resultado.motivo


def test_journal_none_con_testigo_distinto_al_store_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", _payload_reserva(root, seq=1, transition_id=T1))
    _escribir_json(transition_journal_path(root), _payload_none(revision=1, witness=T2))
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "TESTIGO_DIFIERE_DEL_STORE_DERIVADO" in resultado.motivo


def test_journal_ausente_con_store_no_vacio_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", _payload_reserva(root, seq=1, transition_id=T1))
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "HISTORIAL_O_RESERVAS_SIN_JOURNAL" in resultado.motivo


def test_reserva_con_seq_incoherente_con_el_nombre_rechaza(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", _payload_reserva(root, seq=2))
    with pytest.raises(JournalIdentityError):
        journal_module.auditar_reservas_finalizacion(root)


def test_reserva_con_transition_id_invalido_rechaza(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    payload = _payload_reserva(root, seq=1, transition_id=T1)
    payload["transition_id"] = "no-es-uuid"
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", payload)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.auditar_reservas_finalizacion(root)


def test_reserva_huerfana_y_registro_sin_reserva(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    # Registro finalizado CON su reserva comprometida (F1a precede a F1b).
    _escribir_json(finalization_seqs_state_dir(root) / "1.json", _payload_reserva(root, seq=1, transition_id=T1))
    _escribir_json(transitions_state_dir(root) / f"{T1}.json", _payload_registro(transition_id=T1, seq=1))
    # Reserva para una seq que el store todavía no alcanzó: huérfana completable.
    _escribir_json(finalization_seqs_state_dir(root) / "2.json", _payload_reserva(root, seq=2, transition_id=T2))
    auditoria = journal_module.auditar_reservas_finalizacion(root, journal_module.derivar_historial_finalizados(root))
    assert auditoria.huerfanas == ((2, T2),)
    assert auditoria.inconsistentes == ()
    # Registro finalizado SIN su reserva: autoridad incoherente ⇒ FAIL_CLOSED.
    (finalization_seqs_state_dir(root) / "1.json").unlink()
    with pytest.raises(JournalIdentityError):
        journal_module.auditar_reservas_finalizacion(root, journal_module.derivar_historial_finalizados(root))


# ============================================================================
# CAS, exclusión de PENDING, aprobación consumida, inmutabilidad
# ============================================================================


def test_crear_pendiente_aumenta_revision_y_excluye_segundo(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    journal1 = _crear_pendiente(root)
    assert journal1.journal_revision == 0
    assert journal1.transition_id == T1
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "PENDING_PROMOTION"
    # AT_MOST_ONE_PENDING_PER_ROOT: un segundo PENDING (incluso con otro id) falla.
    with pytest.raises(JournalCasError):
        _crear_pendiente(root, _propuesta(T2), _aprobacion(T2))
    # Reintentar la MISMA transición también falla (el PENDING vigente es otro
    # momento de la revisión: no hay escritura nueva).
    with pytest.raises(JournalCasError):
        _crear_pendiente(root)


def test_crear_pendiente_rechaza_transition_id_ya_finalizado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _finalizar(root)
    with pytest.raises(JournalCasError):
        _crear_pendiente(root)  # T1 ya existe en el historial: no se reutiliza


def test_crear_pendiente_exige_aprobacion_consumida_coherente(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.journal import _crear_pendiente_bajo_lock

    # scope digest distinto del propuesto
    aprobacion = _aprobacion()
    aprobacion = type(aprobacion)(
        approval_id=aprobacion.approval_id,
        approval_seq=aprobacion.approval_seq,
        scope_digest=OTRO_DIGEST_64,
        consumed_by_transition_id=T1,
    )
    with pytest.raises(JournalIdentityError):
        _crear_pendiente_bajo_lock(root, _propuesta(), aprobacion)
    # consumida por otra transición
    with pytest.raises(JournalIdentityError):
        _crear_pendiente_bajo_lock(root, _propuesta(), _aprobacion(T2))
    # secuencia no positiva
    aprobacion = _aprobacion()
    aprobacion = type(aprobacion)(
        approval_id=aprobacion.approval_id,
        approval_seq=0,
        scope_digest=aprobacion.scope_digest,
        consumed_by_transition_id=T1,
    )
    with pytest.raises(JournalSchemaError):
        _crear_pendiente_bajo_lock(root, _propuesta(), aprobacion)
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "ABSENT"  # nada se persistió


def test_entrada_publica_de_creacion_esta_bloqueada(tmp_path: pathlib.Path) -> None:
    """P4-S3 no tiene autoridad durable de aprobación: crear PENDING falla cerrado."""
    root = _root(tmp_path)
    with pytest.raises(JournalDependenciaNoDisponibleError):
        journal_module.iniciar_transicion_pendiente(root, _propuesta())


def test_escritura_bajo_lock_verifica_ownership(tmp_path: pathlib.Path) -> None:
    """La maquinaria interna exige el handle dueño: sin lock no hay escritura."""
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime import journal as journal_mod
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockOwnershipError

    with participant_lock(root) as handle:
        handle.release()
        with pytest.raises(FrozenRuntimeLockOwnershipError):
            journal_mod._finalizar_locked(handle, root, _post())


def test_journal_sin_dirs_de_store_falla_cerrado(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    import shutil

    shutil.rmtree(transitions_state_dir(root))
    shutil.rmtree(finalization_seqs_state_dir(root))
    from sky_claw.local.frozen_runtime.journal import _crear_pendiente_bajo_lock

    with pytest.raises(JournalStoreNoInicializadoError):
        _crear_pendiente_bajo_lock(root, _propuesta(), _aprobacion())


def test_actualizacion_con_revision_desactualizada_falla(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import _actualizar_bajo_lock

    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.journal is not None
    actual = snapshot.journal
    # revisión anterior a la vigente
    with pytest.raises(JournalCasError):
        _actualizar_bajo_lock(
            root,
            expected_transition_id=T1,
            expected_journal_revision=actual.journal_revision - 1,
            cambios={
                "evidence_digests": {
                    "clone_evidence": "9" * 64,
                    "runtime_setup_evidence": "e" * 64,
                    "compatibility_evidence": "f" * 64,
                }
            },
        )
    # identidad distinta
    with pytest.raises(JournalCasError):
        _actualizar_bajo_lock(
            root,
            expected_transition_id=T2,
            expected_journal_revision=actual.journal_revision,
            cambios={},
        )


def test_actualizacion_valida_avanza_revision_sin_tocar_identidad(tmp_path: pathlib.Path) -> None:
    """El ÚNICO campo actualizable es ``expected_effective_path`` (no está en el scope).

    ``evidence_digests`` son los campos 6/8/9 del ``ApprovalScope`` (§38.5) y
    quedan inmutables: ver ``test_f1_evidencia_aprobada_no_se_sustituye``.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import _actualizar_bajo_lock

    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.journal is not None
    revision = snapshot.journal.journal_revision
    nueva_ruta = "E:/rt/versions/otra-ruta-de-clone"
    nuevo = _actualizar_bajo_lock(
        root,
        expected_transition_id=T1,
        expected_journal_revision=revision,
        cambios={"expected_effective_path": nueva_ruta},
    )
    assert nuevo.journal_revision == revision + 1
    assert nuevo.transition_id == T1
    despues = journal_module.cargar_journal_transicion(root)
    assert despues.journal is not None
    assert despues.journal.expected_effective_path == nueva_ruta
    # La evidencia aprobada NO se toca.
    assert despues.journal.evidence_digests == snapshot.journal.evidence_digests


def test_actualizacion_rechaza_drift_de_identidad(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import _actualizar_bajo_lock

    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.journal is not None
    with pytest.raises(JournalIdentityError):
        _actualizar_bajo_lock(
            root,
            expected_transition_id=T1,
            expected_journal_revision=snapshot.journal.journal_revision,
            cambios={"target_generation_id": "otra-generation"},
        )


def test_finalizar_crea_registro_reserva_y_rota_journal(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    registro = _finalizar(root)
    assert registro.transition_id == T1
    assert registro.finalization_seq == 1
    assert registro.finalized_from_transition_id is None
    assert registro.record_state.value == "FINALIZED"
    estado = _dump_estado(root)
    assert estado["registros"] == [f"{T1}.json"]
    assert estado["reservas"] == ["1.json"]
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"
    assert snapshot.journal is not None
    assert snapshot.journal.last_finalized_transition_id == T1


def test_reintento_de_la_misma_finalizacion_es_idempotente(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    primero = _finalizar(root)
    bytes_antes = (transitions_state_dir(root) / f"{T1}.json").read_bytes()
    journal_antes = transition_journal_path(root).read_bytes()
    segundo = _finalizar(root)
    assert segundo.transition_id == primero.transition_id
    assert segundo.finalization_seq == primero.finalization_seq
    assert (transitions_state_dir(root) / f"{T1}.json").read_bytes() == bytes_antes
    assert transition_journal_path(root).read_bytes() == journal_antes


def test_evidencia_post_distinta_nunca_sobrescribe_el_registro(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _finalizar(root)
    bytes_antes = (transitions_state_dir(root) / f"{T1}.json").read_bytes()
    from sky_claw.local.frozen_runtime.journal import PostVerifyEvidence

    otra = PostVerifyEvidence(
        transition_id=T1,
        resultado="PASS",
        source_activation_digest=_digest(_fuente()),
        target_generation_id=GEN_OBJETIVO,
        target_clone_id="clone-objetivo",
        target_runtime_setup_id="setup-objetivo",
        desired_active_generation=GEN_OBJETIVO,
        effective_observation_digest="3" * 64,
        payload_digest="4" * 64,
        verified_at_ns=1_700_000_000_000_000_009,
        expected_journal_revision=_revision_vigente(root),
    )
    with pytest.raises(JournalEvidenciaPostError):
        _finalizar(root, otra)
    assert (transitions_state_dir(root) / f"{T1}.json").read_bytes() == bytes_antes


def test_finalizar_sin_post_valido_no_finaliza(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import PostVerifyEvidence

    base_revision = _revision_vigente(root)

    def _evidencia(**cambios: object) -> object:
        base = _post_para(root)
        campos = {
            "transition_id": base.transition_id,
            "resultado": base.resultado,
            "source_activation_digest": base.source_activation_digest,
            "target_generation_id": base.target_generation_id,
            "target_clone_id": base.target_clone_id,
            "target_runtime_setup_id": base.target_runtime_setup_id,
            "desired_active_generation": base.desired_active_generation,
            "effective_observation_digest": base.effective_observation_digest,
            "payload_digest": base.payload_digest,
            "verified_at_ns": base.verified_at_ns,
            "expected_journal_revision": base.expected_journal_revision,
        }
        campos.update(cambios)
        return PostVerifyEvidence(**campos)

    casos = {
        "post_falla": _evidencia(resultado="FAIL"),
        "digest_invalido": _evidencia(payload_digest="corto"),
        "otra_transicion": _evidencia(transition_id=T2),
        "target_distinto": _evidencia(target_generation_id=GEN_TERCERA),
        "source_distinto": _evidencia(source_activation_digest=OTRO_DIGEST_64),
        "sin_timestamp": _evidencia(verified_at_ns=0),
        "desired_none": _evidencia(desired_active_generation=None),
        "desired_de_otra_generation": _evidencia(desired_active_generation=GEN_ORIGEN),
        "revision_de_otro_momento": _evidencia(expected_journal_revision=base_revision + 7),
    }
    for nombre, evidencia in casos.items():
        with pytest.raises((JournalEvidenciaPostError, JournalCasError)):
            _finalizar(root, evidencia)
        estado = _dump_estado(root)
        assert estado["registros"] == [], nombre
        snapshot = journal_module.cargar_journal_transicion(root)
        assert snapshot.disposition.value == "PENDING_PROMOTION", nombre


def test_transicion_posterior_no_sustituye_evidencia_historica(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _finalizar(root)
    bytes_t1 = (transitions_state_dir(root) / f"{T1}.json").read_bytes()
    _start_next(root)
    segundo = _finalizar(root, _post_para(root, T2))
    assert segundo.finalization_seq == 2
    assert segundo.finalized_from_transition_id == T1
    assert (transitions_state_dir(root) / f"{T1}.json").read_bytes() == bytes_t1
    vista = journal_module.derivar_historial_finalizados(root)
    assert tuple(r.transition_id for r in vista.registros) == (T1, T2)
    assert vista.next_seq == 3
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"
    assert snapshot.journal is not None
    assert snapshot.journal.last_finalized_transition_id == T2


def test_start_next_exige_latest_y_rechaza_reutilizacion_de_id(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _finalizar(root)
    # El propuesto igual al predecesor finalizado se rechaza (no ABA).
    with pytest.raises((JournalCasError, JournalIdentityError)):
        _start_next(root, _propuesta(T1), _aprobacion(T1))


def test_reserva_keyed_por_seq_evita_dos_finalizadores(tmp_path: pathlib.Path) -> None:
    """Dos finalizadores con el mismo NEXT y distinto id: sólo uno reserva."""
    root = _root(tmp_path)
    _crear_pendiente(root)
    with participant_lock(root) as handle:
        handle.assert_owned()
        from sky_claw.local.frozen_runtime.journal import _reservar_seq_locked

        _reservar_seq_locked(handle, root, seq=1, transition_id=T1)
        with pytest.raises(JournalCasError):
            _reservar_seq_locked(handle, root, seq=1, transition_id=T2)
        # El mismo transition_id sobre la misma reserva es idempotente (reintento).
        _reservar_seq_locked(handle, root, seq=1, transition_id=T1)
    contenido = json.loads((finalization_seqs_state_dir(root) / "1.json").read_text(encoding="utf-8"))
    assert contenido["transition_id"] == T1


# ============================================================================
# Crash matrix y recuperación (fault injection determinista)
# ============================================================================


def _parchear_fallo_en_escritura(monkeypatch: pytest.MonkeyPatch, *, fallar_en_llamada: int) -> list[int]:
    """Falla la escritura atómica nº ``fallar_en_llamada`` (1-indexada) y cuenta.

    El orden dentro de una finalización es: 1=reserva, 2=registro, 3=rotación.
    """
    from sky_claw.local.frozen_runtime import state as state_module

    llamadas: list[int] = []

    def _envuelto(ruta: pathlib.Path, payload: dict) -> None:
        llamadas.append(len(llamadas) + 1)
        if len(llamadas) == fallar_en_llamada:
            raise OSError("falla inyectada de I/O")
        original(ruta, payload)

    original = state_module.write_json_atomic
    monkeypatch.setattr(state_module, "write_json_atomic", _envuelto)
    return llamadas


def test_crash_antes_de_f1_deja_pending_reintentable(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=1)
    with pytest.raises(OSError):
        _finalizar(root)
    estado = _dump_estado(root)
    # La ruta quedó reservada (placeholder de 0 bytes) pero SIN contenido: la
    # escritura del payload falló, así que no hay reserva comprometida.
    assert estado["registros"] == []
    assert estado["reservas"] == ["1.json"]
    assert (finalization_seqs_state_dir(root) / "1.json").stat().st_size == 0
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "PENDING_PROMOTION"
    # Sin evidencia comprometida, la reconciliación no inventa un éxito.
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "UNKNOWN_EFFECTIVE"
    # Reintento completo tras levantar la falla (el placeholder se completa).
    monkeypatch.undo()
    registro = _finalizar(root)
    assert registro.finalization_seq == 1
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"


def test_crash_entre_f1b_y_f2_deja_registro_y_classifica_finalizado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=3)
    with pytest.raises(OSError):
        _finalizar(root)
    estado = _dump_estado(root)
    assert estado["registros"] == [f"{T1}.json"]
    assert estado["reservas"] == ["1.json"]
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "PENDING_PROMOTION"
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "REGISTRO_FINALIZADO_CON_JOURNAL_PENDING"
    assert resultado.registro_finalizado_presente is True
    # La reconciliación NUNCA muta: ambas evidencias se preservan.
    assert _dump_estado(root)["journal_bytes"] == estado["journal_bytes"]
    # Completar la rotación (misma evidencia) es la reconciliación permitida.
    monkeypatch.undo()
    registro = _finalizar(root)
    assert registro.finalization_seq == 1
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"


def test_crash_entre_reserva_y_registro_deja_reserva_huerfana(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=2)
    with pytest.raises(OSError):
        _finalizar(root)
    estado = _dump_estado(root)
    # F1b interrumpida: la ruta del registro quedó reservada con 0 bytes. La
    # derivación NO lo lee como registro finalizado (ni como corrupción).
    assert estado["registros"] == [f"{T1}.json"]
    assert (transitions_state_dir(root) / f"{T1}.json").stat().st_size == 0
    assert estado["reservas"] == ["1.json"]
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "RECOVER"
    assert "RESERVA_HUERFANA" in resultado.motivo
    assert resultado.registro_finalizado_presente is False
    # Con la MISMA transición se puede completar; con otra, REQUIRE_OWNER.
    monkeypatch.undo()
    registro = _finalizar(root)
    assert registro.finalization_seq == 1

    root2 = _root(tmp_path, "root2")
    _crear_pendiente(root2)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=2)
    with pytest.raises(OSError):
        _finalizar(root2)
    # La reserva huérfana quedó para T1: un cierre con evidencia AJENA no puede
    # reclamarla (la evidencia POST debe ligar la transición pendiente).
    monkeypatch.undo()
    with pytest.raises(JournalEvidenciaPostError):
        _finalizar(root2, _post_para(root2, T2))


def test_store_no_omite_enlace_de_directorio_como_placeholder(tmp_path: pathlib.Path) -> None:
    """Un junction llamado ``<T>.json`` NUNCA se omite como placeholder (§34.5 N2).

    ``_placeholder_vacio`` filtraba con ``is_symlink()`` —CIEGO a los junctions de
    NTFS— y medía con ``stat()``, que SIGUE el enlace. Un junction llamado
    ``<T>.json`` apunta a un directorio, y en NTFS un directorio reporta
    ``st_size == 0``: el guard no lo veía como enlace y la medición daba 0, así
    que se clasificaba como placeholder y se omitía en la derivación del historial
    sin pasar por ``_leer_json_estricto``, que sí rechaza enlaces. Es el mismo
    hueco que el reparse point del journal, pero en los stores históricos.
    """
    root = _root(tmp_path)
    externo = tmp_path / "externo-dir-store"
    externo.mkdir()
    ruta = transitions_state_dir(root) / f"{T2}.json"
    tipo = _crear_enlace_de_directorio(ruta, externo)
    # Evidencia por test: el régimen produjo un enlace REAL y se ejerció el caso.
    assert _exigir_enlace_real(ruta) == tipo
    assert not ruta.is_symlink() or tipo == "symlink"
    with pytest.raises(JournalCorruptError):
        journal_module.derivar_historial_finalizados(root)


def test_auditoria_no_omite_enlace_de_directorio_como_placeholder(tmp_path: pathlib.Path) -> None:
    """Ídem en el store de reservas: el enlace va al lector estricto, no se omite."""
    root = _root(tmp_path)
    externo = tmp_path / "externo-dir-reservas"
    externo.mkdir()
    ruta = finalization_seqs_state_dir(root) / "2.json"
    tipo = _crear_enlace_de_directorio(ruta, externo)
    assert _exigir_enlace_real(ruta) == tipo
    with pytest.raises(JournalCorruptError):
        journal_module.auditar_reservas_finalizacion(root)


def test_f14_limpiar_enlace_ausente_no_es_error(tmp_path: pathlib.Path) -> None:
    """F14 — limpiar el remanente de un enlace fallido no puede ERROR-ear.

    ``_crear_enlace_de_directorio`` limpia el remanente cuando el intento de
    enlace no produjo un enlace real. Si ``os.symlink`` y ``mklink /J`` fallan
    los dos (volumen no-NTFS, ruta de red), ``destino`` **no existe** y limpiar
    "nada" es el caso NORMAL: debe ser un no-op para llegar al ``pytest.skip``
    documentado. Con ``unlink()`` sin guarda, ese camino revienta con
    ``FileNotFoundError`` y convierte un skip en un ERROR en todo test que use
    el helper.
    """
    ausente = tmp_path / "no-existe-en-absoluto"
    assert not ausente.exists()
    _quitar_enlace_sin_seguir(ausente)  # no-op: no debe lanzar
    assert not ausente.exists()


def test_reconciliacion_pending_sin_oraculo_nunca_es_complete(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "UNKNOWN_EFFECTIVE"
    assert resultado.punto_crash is not None and resultado.punto_crash.startswith("C")
    # Nunca afirma COMPLETE ni revierte solo.
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "PENDING_PROMOTION"


def test_reconciliacion_none_sin_oraculo_nunca_es_complete(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "UNKNOWN_EFFECTIVE"


def test_reconciliacion_n0_y_n1_con_oraculo_de_effective(tmp_path: pathlib.Path) -> None:
    """F10: dos Generation iguales NO prueban la identidad del Clone efectivo.

    Sin un registro FINALIZED que ancle qué Runtime Clone corresponde al
    Desired, P4-S3 no puede demostrar la coherencia del Clone: el veredicto es
    INDETERMINADO (``UNKNOWN_EFFECTIVE``), nunca ``COMPLETE`` (§34.5).
    """
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=GEN_ORIGEN, updated_at_ns=1),
    )
    # Generation igual, pero SIN autoridad durable del Clone ⇒ indeterminado.
    sin_ancla = journal_module.reconciliar_journal(
        root,
        observador_effective=lambda: _observacion(
            generation=GEN_ORIGEN, clone="clone-origen", path="E:/rt/versions/" + GEN_ORIGEN
        ),
    )
    assert sin_ancla.veredicto.value != "COMPLETE"
    assert sin_ancla.veredicto.value == "UNKNOWN_EFFECTIVE"
    assert "IDENTIDAD_DE_CLONE_NO_DEMOSTRABLE" in sin_ancla.motivo
    # N1: sin transición y Desired != Effective ⇒ FAIL_CLOSED, dueño requerido.
    n1 = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert n1.veredicto.value == "FAIL_CLOSED"
    assert n1.requiere_dueno is True


def test_reconciliacion_n0_complete_exige_clone_registrado(tmp_path: pathlib.Path) -> None:
    """N0 sólo es COMPLETE con la identidad del Runtime Clone DEMOSTRADA (§34.5).

    ``N0_COMPLETE_REQUIRES_PROVEN_IDENTITY = YES``: la autoridad durable del
    Clone activo es el registro FINALIZED más reciente (§37.5).
    """
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    _crear_pendiente(root)
    _finalizar(root)  # registro T1: target GEN_OBJETIVO / clone "clone-objetivo"
    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=GEN_OBJETIVO, updated_at_ns=1),
    )
    # Clone observado == el que la autoridad durable registra ⇒ COMPLETE.
    ok = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert ok.veredicto.value == "COMPLETE"
    assert ok.punto_crash == "N0"
    # OTRO Clone de la MISMA Generation ⇒ FAIL_CLOSED (antes daba COMPLETE).
    otro_clone = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-ajeno")
    )
    assert otro_clone.veredicto.value == "FAIL_CLOSED"
    assert "Runtime Clone" in otro_clone.motivo
    # Identidad de Clone indeterminada ⇒ FAIL_CLOSED, nunca COMPLETE.
    indeterminado = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone=None)
    )
    assert indeterminado.veredicto.value == "FAIL_CLOSED"


def test_f10_tip_de_otra_generation_no_prueba_el_clone_activo(tmp_path: pathlib.Path) -> None:
    """F10 — el tip de la autoridad sólo prueba el Clone si es de ESA Generation.

    La autoridad durable del Clone activo es el registro FINALIZED más reciente
    *de la Generation deseada* (§37.5). Si el tip pertenece a OTRA Generation no
    hay demostración posible: el veredicto es ``UNKNOWN_EFFECTIVE``. Tomar el
    ``target_clone_id`` del tip ignorando la Generation convertiría un
    indeterminado en ``COMPLETE`` (``UNKNOWN != SUCCESS``, §34.5) — y lo haría
    justamente cuando el Desired NO tiene ningún registro que lo respalde.
    """
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    # El tip queda en GEN_ORIGEN: ninguna autoridad registra el Clone de GEN_OBJETIVO.
    _crear_pendiente(
        root,
        _propuesta_hacia(T1, target_generation=GEN_ORIGEN, target_clone="clone-origen", target_setup="setup-origen"),
        _aprobacion(T1),
    )
    _finalizar(
        root,
        _post_para(
            root,
            T1,
            target_generation=GEN_ORIGEN,
            target_clone="clone-origen",
            target_setup="setup-origen",
            desired=GEN_ORIGEN,
            fuente=_fuente_objetivo(),
        ),
    )
    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=GEN_OBJETIVO, updated_at_ns=1),
    )
    # El oráculo devuelve el MISMO clone_id que el tip, pero de OTRA Generation:
    # sin registro para GEN_OBJETIVO la identidad NO está demostrada.
    resultado = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-origen")
    )
    assert resultado.veredicto.value == "UNKNOWN_EFFECTIVE"
    assert resultado.veredicto.value != "COMPLETE"


def test_reconciliacion_desired_none_y_oraculo_none_nunca_es_complete(tmp_path: pathlib.Path) -> None:
    """UNKNOWN != SUCCESS (ADR §34.5): la igualdad de dos indeterminados NO es N0.

    El oráculo que devuelve None no pudo determinar el runtime (indeterminado),
    y un Desired sin generación declarada no es una declaración contra la que
    comparar. ``None == None`` jamás puede producir COMPLETE.
    """
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=None, updated_at_ns=1),
    )
    resultado = journal_module.reconciliar_journal(root, observador_effective=lambda: None)
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "EFFECTIVE_INDETERMINADO" in resultado.motivo
    assert resultado.requiere_dueno is True


def test_reconciliacion_c5_c6_c7_con_evidencia_y_registro(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    _crear_pendiente(root)
    # C6: el oráculo observa la RUTA efectiva (identidad del Clone, §34.5):
    # la reconciliación permite re-correr el POST (RECOVER) sin finalizar sola.
    c6 = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert c6.veredicto.value == "RECOVER"
    _finalizar(root)
    # C7 equivale a N0 una vez rotado: Desired == Effective y sin transición.
    # (La actualización de Desired=C3 pertenece al flujo de promoción, diferido;
    # el test la ejerce con el participante REAL de estado.)
    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=GEN_OBJETIVO, updated_at_ns=1),
    )
    c7 = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert c7.veredicto.value == "COMPLETE"
    assert c7.punto_crash == "N0"


def test_reconciliacion_pending_exige_identidad_de_clone_no_de_generation(tmp_path: pathlib.Path) -> None:
    """El ADR §34.5 exige demostrar la identidad del Runtime Clone efectivo.

    En un PENDING, la fila que habilita re-correr el POST es "Effective
    **coincide** con ``expected_effective_path``". Comparar contra
    ``target_generation_id`` (la Generation) NO demuestra que el runtime
    efectivo apunte al Runtime Clone del par Desired: la integridad del Clone
    es física (§35.4) y la Generation no la prueba.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    # El oráculo observa la ruta que ejecuta el runtime: la esperada por el bind.
    c6 = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert c6.veredicto.value == "RECOVER"
    assert "EXPECTED_PATH" in c6.motivo
    # Una observación cuya "ruta" es la Generation a secas no es la del Clone.
    por_generation = journal_module.reconciliar_journal(
        root,
        observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo", path=GEN_OBJETIVO),
    )
    assert por_generation.veredicto.value == "REQUIRE_OWNER"
    assert "EXPECTED_PATH" in por_generation.motivo
    # F10: un callback que devuelve una Generation suelta ya no es contrato válido.
    no_tipado = journal_module.reconciliar_journal(root, observador_effective=lambda: GEN_OBJETIVO)
    assert no_tipado.veredicto.value == "FAIL_CLOSED"
    assert "EFFECTIVE_NO_TIPADO" in no_tipado.motivo


def test_reconciliacion_oraculo_indeterminado_es_fail_closed(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)

    def _oraculo_roto() -> str:
        raise RuntimeError("no se pudo observar el runtime")

    resultado = journal_module.reconciliar_journal(root, observador_effective=_oraculo_roto)
    assert resultado.veredicto.value == "FAIL_CLOSED"


def test_desired_corrupto_en_reconciliacion_es_fail_closed(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    (root / "state" / "active.json").write_text("{roto", encoding="utf-8")
    resultado = journal_module.reconciliar_journal(root)
    assert resultado.veredicto.value == "FAIL_CLOSED"


def test_reconciliacion_rechaza_enlaces_en_el_journal(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    ruta = transition_journal_path(root)
    externo = tmp_path / "externo-journal"
    externo.mkdir()
    tipo = _crear_enlace_de_directorio(ruta, externo)
    assert _exigir_enlace_real(ruta) == tipo
    with pytest.raises((JournalCorruptError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


# ============================================================================
# Concurrencia real (dos procesos) sobre el mismo root
# ============================================================================

_CHILD = pathlib.Path(__file__).resolve().parent / "_p4_s3_child.py"


def _lanzar(modo: str, root: pathlib.Path, args: dict) -> dict:
    proceso = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), modo, str(root), json.dumps(args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENTORNO_HIJO,
        timeout=90,
        cwd=str(WORKTREE),
    )
    assert proceso.returncode == 0, proceso.stderr
    return json.loads(proceso.stdout.strip().splitlines()[-1])


def _esperar(path: pathlib.Path, *, timeout: float = 30.0) -> None:
    limite = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < limite:
        time.sleep(0.02)
    assert path.exists(), f"nunca apareció {path}"


def test_s3_dos_procesos_creando_pending_solo_uno_gana(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    dentro = tmp_path / "dentro"
    liberar = tmp_path / "liberar"
    ganador = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(_CHILD),
            "crear",
            str(root),
            json.dumps({"transition_id": T1, "dentro": str(dentro), "liberar": str(liberar)}),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=ENTORNO_HIJO,
        cwd=str(WORKTREE),
    )
    try:
        _esperar(dentro)
        # El ganador está DENTRO de la sección crítica (handshake sobre la
        # única escritura de CREATE): el journal todavía no existe y el lock
        # está tomado, así que el contendente sólo puede fallar por busy.
        assert journal_module.cargar_journal_transicion(root).disposition.value == "ABSENT"
        perdedor = _lanzar("crear", root, {"transition_id": T2})
        assert perdedor["ok"] is False
        assert perdedor["error_type"] == "FrozenRuntimeLockBusyError", perdedor
    finally:
        liberar.write_text("1", encoding="utf-8")
        salida, _err = ganador.communicate(timeout=90)
        resultado = json.loads(salida.strip().splitlines()[-1])
        assert resultado["ok"] is True, resultado
    # El ganador sigue siendo el único escritor del journal vigente.
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "PENDING_PROMOTION"
    assert snapshot.journal is not None and snapshot.journal.transition_id == T1


def test_s3_dos_procesos_finalizando_un_solo_registro_y_reserva(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    dentro = tmp_path / "dentro"
    liberar = tmp_path / "liberar"
    ganador = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(_CHILD),
            "finalizar",
            str(root),
            json.dumps({"transition_id": T1, "dentro": str(dentro), "liberar": str(liberar)}),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=ENTORNO_HIJO,
        cwd=str(WORKTREE),
    )
    try:
        _esperar(dentro)
        perdedor = _lanzar("finalizar", root, {"transition_id": T1})
        assert perdedor["ok"] is False
        assert perdedor["error_type"] == "FrozenRuntimeLockBusyError", perdedor
        # El ganador está DENTRO de la sección crítica (handshake sobre la
        # primera escritura de F1a): el journal sigue PENDING y nadie más escribió.
        snapshot = journal_module.cargar_journal_transicion(root)
        assert snapshot.disposition.value == "PENDING_PROMOTION"
        assert snapshot.journal is not None and snapshot.journal.transition_id == T1
        assert _dump_estado(root)["registros"] == []
    finally:
        liberar.write_text("1", encoding="utf-8")
        salida, _err = ganador.communicate(timeout=90)
        resultado = json.loads(salida.strip().splitlines()[-1])
        assert resultado["ok"] is True, resultado
    estado = _dump_estado(root)
    assert estado["registros"] == [f"{T1}.json"]
    assert estado["reservas"] == ["1.json"]
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.disposition.value == "NONE"
    vista = journal_module.derivar_historial_finalizados(root)
    assert vista.next_seq == 2


def test_s3_lock_ocupado_falla_cerrado_sin_deadlock(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    dentro = tmp_path / "dentro"
    liberar = tmp_path / "liberar"
    ganador = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(_CHILD),
            "crear",
            str(root),
            json.dumps({"transition_id": T1, "dentro": str(dentro), "liberar": str(liberar)}),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=ENTORNO_HIJO,
        cwd=str(WORKTREE),
    )
    try:
        _esperar(dentro)
        inicio = time.monotonic()
        perdedor = _lanzar("crear", root, {"transition_id": T2})
        assert time.monotonic() - inicio < 30.0, "el contendiente no falló acotado"
        assert perdedor["ok"] is False
        assert perdedor["error_type"] == "FrozenRuntimeLockBusyError", perdedor
    finally:
        liberar.write_text("1", encoding="utf-8")
        ganador.communicate(timeout=90)


def test_s3_contencion_no_deja_temporales_ni_basura(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    _crear_pendiente(root)
    _finalizar(root)
    _start_next(root)
    _finalizar(root, _post_para(root, T2))
    basura = [p.name for p in (root / "state").iterdir() if p.name.startswith(".") or p.name.endswith(".tmp")]
    assert basura == []


# ============================================================================
# Anclas estructurales del slice (oráculos, disciplina de lock, superficie)
# ============================================================================


def _fuente_journal() -> str:
    import sky_claw.local.frozen_runtime.journal as modulo

    return pathlib.Path(modulo.__file__).read_text(encoding="utf-8")


def test_s3_journal_no_declara_mutadores_fuera_del_inventario() -> None:
    """journal.py no usa mkdir/open-escritura: escribe por delegación declarada."""
    from tests.test_frozen_runtime_p1_readonly import (
        MUTADORES_FILESYSTEM,
        _detectar_open_no_declarado,
        _mutadores_de_fuente,
    )

    fuente = _fuente_journal()
    assert _detectar_open_no_declarado(fuente, "journal.py") == []
    assert _mutadores_de_fuente(fuente) == set()
    assert fuente.count("state_module.") > 0  # escribe por el serializador canónico
    assert MUTADORES_FILESYSTEM  # el inventario sigue vivo


def test_s3_toda_escritura_del_journal_corre_bajo_el_lock() -> None:
    """Cada helper de escritura del journal sólo se llama bajo el root lock.

    Se reutilizan los resolutores del censo de P4-S2 (no se reimplementan): un
    helper que escribe vía ``state_module.<escritor>``, o adquiere el lock, o es
    primitiva ``_locked`` cuyos call sites viven dentro de funciones que
    adquieren. journal.py NO figura en el inventario de mutadores (escribe por
    delegación declarada), así que este ancla es su cobertura de lock.
    """
    from tests.test_frozen_runtime_p4_s2_participant_wiring import (
        _arbol_de,
        _call_sites_de,
        _funciones_que_adquieren_lock,
        _llamadas_propias,
        _nombre_llamado,
    )

    ruta = pathlib.Path(journal_module.__file__)
    arbol = _arbol_de(ruta)
    adquirentes = _funciones_que_adquieren_lock(arbol)
    assert adquirentes, "journal.py no declara ninguna función que adquiera el root lock"

    escritores_canonica = {"write_json_atomic", "reservar_ruta_json_exclusiva"}
    escritores: set[str] = set()
    for funcion in arbol.body:
        if not isinstance(funcion, ast.FunctionDef):
            continue
        for llamada in _llamadas_propias(funcion):
            nombre = _nombre_llamado(llamada)
            if nombre in escritores_canonica:
                escritores.add(funcion.name)
    assert escritores, "no se detectaron los escritores canónicos del journal"
    for nombre in sorted(escritores):
        assert nombre in adquirentes or nombre.endswith("_locked"), f"{nombre} escribe sin convención _locked"
        for modulo, contenedor in _call_sites_de(nombre):
            assert modulo == "journal.py", f"{nombre} se invoca desde {modulo}"
            assert contenedor in adquirentes or contenedor.endswith("_locked"), (
                f"{contenedor} llama a {nombre} fuera del lock"
            )


def test_s3_los_flanos_bajo_lock_solo_se_llaman_entre_ellos() -> None:
    """Las primitivas ``_locked`` del journal no se llaman desde el exterior."""
    from tests.test_frozen_runtime_p4_s2_participant_wiring import _arbol_de, _call_sites_de, _funciones_top_level

    ruta = pathlib.Path(journal_module.__file__)
    arbol = _arbol_de(ruta)
    primitivas = {funcion.name for funcion in _funciones_top_level(arbol) if funcion.name.endswith("_locked")}
    assert primitivas, "el journal no declara primitivas _locked"
    for nombre in sorted(primitivas):
        sitios = _call_sites_de(nombre)
        # Sólo dentro de journal.py, y sólo desde adquirentes u otras primitivas.
        for modulo, contenedor in sitios:
            assert modulo == "journal.py", f"{nombre} se invoca desde {modulo}"
            assert contenedor in primitivas or contenedor.endswith("_bajo_lock"), (
                f"{contenedor} llama a la primitiva {nombre} sin ser adquirente"
            )


def test_s3_superficie_publica_no_expone_mutacion() -> None:
    """P4-S3 no expone mutación pública: la aprobación y el POST son dependencias.

    La superficie pública es sólo lectura/validación/clasificación más la entrada
    de creación BLOQUEADA (falla cerrado por dependencia ausente).
    """
    import sky_claw.local.frozen_runtime.journal as modulo

    publicas = {
        nombre
        for nombre in dir(modulo)
        if not nombre.startswith("_")
        and callable(getattr(modulo, nombre))
        and not isinstance(getattr(modulo, nombre), type)
        and getattr(modulo, nombre).__module__ == modulo.__name__
    }
    esperadas = {
        "cargar_journal_transicion",
        "derivar_historial_finalizados",
        "auditar_reservas_finalizacion",
        "reconciliar_journal",
        "iniciar_transicion_pendiente",
        "iniciar_transicion_rollback",
    }
    assert publicas == esperadas, f"superficie pública inesperada: {sorted(publicas)}"
    assert modulo.iniciar_transicion_pendiente.__module__ == modulo.__name__


def test_s3_finalize_y_create_internos_exigen_handle() -> None:
    """La firma de los mutadores internos exige el handle dueño (no hay bypass)."""
    import inspect

    from sky_claw.local.frozen_runtime import journal as modulo

    for nombre in ("_finalizar_locked", "_crear_pendiente_locked", "_start_next_locked", "_actualizar_locked"):
        firma = inspect.signature(getattr(modulo, nombre))
        assert "handle" in firma.parameters, nombre


# ============================================================================
# Ronda de revisión R2 (PR #736): adjudicación de los 15 hallazgos
#
# Autoridad: §35.6 (evidencia durable liga desired state), §36.3 (source
# baseline coherente), §36.6 (JOURNAL_FINALIZE_CAS = transition_id +
# journal_revision), §36.9 C6 (reintento exige mismo source/target/Desired),
# §37.5/§38.3 (autoridad del store y de la reserva), §34.5 N2 (corrupto/redirigido
# nunca se lee como ausente).
# ============================================================================


def test_f1_evidencia_aprobada_no_se_sustituye(tmp_path: pathlib.Path) -> None:
    """F1 — el conjunto aprobado de artefactos no puede alterarse reutilizando la aprobación.

    ``clone_evidence`` / ``runtime_setup_evidence`` / ``compatibility_evidence``
    son los campos 6/8/9 del ``ApprovalScope`` (§38.5) y ``approval_scope_digest``
    los compromete. Permitir sustituirlos conservando el scope consumido dejaba
    finalizar contenido nuevo bajo la aprobación del contenido viejo.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import _actualizar_bajo_lock

    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.journal is not None
    revision = snapshot.journal.journal_revision
    with pytest.raises(JournalIdentityError):
        _actualizar_bajo_lock(
            root,
            expected_transition_id=T1,
            expected_journal_revision=revision,
            cambios={
                "evidence_digests": {
                    "clone_evidence": "9" * 64,
                    "runtime_setup_evidence": "e" * 64,
                    "compatibility_evidence": "f" * 64,
                }
            },
        )
    despues = journal_module.cargar_journal_transicion(root)
    assert despues.journal is not None
    # El journal NO se movió: la evidencia aprobada sigue siendo la misma.
    assert despues.journal.evidence_digests == snapshot.journal.evidence_digests
    assert despues.journal.journal_revision == revision
    # Y la transición sigue finalizando con la aprobación ORIGINAL (no se invalidó).
    _finalizar(root)
    assert _dump_estado(root)["registros"] == [f"{T1}.json"]


def test_f2_evidencia_post_de_otra_revision_no_finaliza(tmp_path: pathlib.Path) -> None:
    """F2 — POST calculado para la revisión R no puede finalizar R+1 (§36.6).

    ``JOURNAL_FINALIZE_CAS = (transition_id, journal_revision)``. Comparar sólo
    ``transition_id`` aceptaba evidencia obsoleta y finalizaba después de que la
    ruta efectiva esperada hubiera cambiado.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    from sky_claw.local.frozen_runtime.journal import _actualizar_bajo_lock

    revision_r = _revision_vigente(root)
    _actualizar_bajo_lock(
        root,
        expected_transition_id=T1,
        expected_journal_revision=revision_r,
        cambios={"expected_effective_path": "E:/rt/versions/ruta-post-bind"},
    )
    assert _revision_vigente(root) == revision_r + 1

    with pytest.raises(JournalCasError):
        _finalizar(root, _post(T1, revision=revision_r))
    assert _dump_estado(root)["registros"] == []
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"

    # La evidencia de la revisión VIGENTE sí finaliza: la idempotencia válida se preserva.
    _finalizar(root, _post_para(root))
    assert _dump_estado(root)["registros"] == [f"{T1}.json"]


def test_f3_post_pass_con_desired_incorrecto_no_publica_registro(tmp_path: pathlib.Path) -> None:
    """F3 — POST PASS con ``desired_active_generation`` distinto del target no finaliza.

    §35.6: la evidencia durable liga, al MISMO, ``{transition_id, source, target,
    desired state, effective observation}``.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    for deseado in (None, GEN_ORIGEN, GEN_TERCERA):
        with pytest.raises(JournalEvidenciaPostError):
            _finalizar(root, _post_para(root, desired=deseado))
        assert _dump_estado(root)["registros"] == [], deseado
        assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"
    # El Desired correcto (== target) sí finaliza.
    _finalizar(root, _post_para(root, desired=GEN_OBJETIVO))
    assert _dump_estado(root)["registros"] == [f"{T1}.json"]


def test_f4_registro_sin_reserva_bloquea_finalize_y_start_next(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 — la autoridad de la secuencia se audita ANTES de ampliar el historial (§38.3).

    ``registro sin reserva ⇒ INCONSISTENT_AUTHORITY ⇒ FAIL_CLOSED``. Antes, sólo
    la reconciliación lo detectaba: FINALIZE y START_NEXT avanzaban igual.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=3)  # F1a+F1b ok, F2 falla
    with pytest.raises(OSError):
        _finalizar(root)
    monkeypatch.undo()
    assert _dump_estado(root)["reservas"] == ["1.json"]
    (finalization_seqs_state_dir(root) / "1.json").unlink()

    # Completar la rotación exige autoridad coherente.
    with pytest.raises(JournalIdentityError):
        _finalizar(root)
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"

    # START_NEXT (el otro mutador que amplía el historial) también falla cerrado.
    root2 = _root(tmp_path, "root2")
    _crear_pendiente(root2)
    _finalizar(root2)
    (finalization_seqs_state_dir(root2) / "1.json").unlink()
    with pytest.raises(JournalIdentityError):
        _start_next(root2)
    # La reconciliación clasifica la misma incoherencia.
    assert journal_module.reconciliar_journal(root2).veredicto.value == "FAIL_CLOSED"


def test_f5_ancestro_redirigido_nunca_es_absent(tmp_path: pathlib.Path) -> None:
    """F5 — un namespace redirigido no es 'primer arranque' (§34.5 N2).

    Con ``transition.json`` ausente bajo un ancestro enlazado, ``exists()`` es
    False y el loader reportaba ABSENT limpio sin inspeccionar el parent.
    """
    root = _root(tmp_path)
    externo = tmp_path / "externo-vacio"
    externo.mkdir()
    real = root / "state-real"
    os.rename(root / "state", real)
    tipo = _crear_enlace_de_directorio(root / "state", externo)
    assert _exigir_enlace_real(root / "state") == tipo
    assert not transition_journal_path(root).exists()
    with pytest.raises(JournalCorruptError):
        journal_module.cargar_journal_transicion(root)


def test_f6_source_activation_parcial_rechaza(tmp_path: pathlib.Path) -> None:
    """F6 — un source a medias no es ni bootstrap ni observación (§36.3).

    ``{generation_id, clone_id, runtime_setup_id, effective_runtime_evidence:
    null}`` pasaba y podía volverse ``previous_activation_target`` sin demostrar
    el baseline observado.
    """
    root = _root(tmp_path)
    parcial = {
        "generation_id": GEN_ORIGEN,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": None,
    }
    _escribir_json(transition_journal_path(root), _payload_pendiente(fuente=parcial))
    with pytest.raises(JournalSchemaError):
        journal_module.cargar_journal_transicion(root)

    # El bootstrap íntegramente vacío sigue siendo legítimo (no es un parcial).
    vacio = {
        "generation_id": None,
        "clone_id": None,
        "runtime_setup_id": None,
        "effective_runtime_evidence": None,
    }
    _escribir_json(transition_journal_path(root), _payload_pendiente(fuente=vacio))
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"


def test_f7_registro_finalizado_de_otro_estado_no_rota(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F7 — un registro de forma válida pero de OTRO estado no vuelve autoridad (§36.9 C6).

    Comparar sólo ``transition_id`` + digest POST + timestamp aceptaba el
    registro y rotaba el journal, dejando un historial que contradice la
    transición pendiente.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    _parchear_fallo_en_escritura(monkeypatch, fallar_en_llamada=3)
    with pytest.raises(OSError):
        _finalizar(root)
    monkeypatch.undo()
    ruta = transitions_state_dir(root) / f"{T1}.json"
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    payload["operation"] = "ROLLBACK"  # forma válida; identidad inmutable distinta
    _escribir_json(ruta, payload)
    with pytest.raises(JournalIdentityError):
        _finalizar(root)
    # El journal NO rotó: ambas evidencias se preservan.
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"
    assert json.loads(ruta.read_text(encoding="utf-8"))["operation"] == "ROLLBACK"


def test_f8_transicion_concurrente_no_permite_complete(tmp_path: pathlib.Path) -> None:
    """F8 — el snapshot durable es causalmente coherente (§34.5).

    Una transición creada mientras el reconciliador observa el Effective (fuera
    del lock, como exige §34.3/§39.4) no puede quedar invisible y producir
    ``COMPLETE`` sobre el estado anterior.
    """
    root = _root(tmp_path)
    from sky_claw.local.frozen_runtime.state import save_frozen_runtime_state
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    _crear_pendiente(root)
    _finalizar(root)
    save_frozen_runtime_state(
        root / "state" / "active.json",
        FrozenRuntimeState(schema_version=1, desired_active_generation=GEN_OBJETIVO, updated_at_ns=1),
    )
    # Control: sin concurrencia, este estado es N0 COMPLETE.
    control = journal_module.reconciliar_journal(
        root, observador_effective=lambda: _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")
    )
    assert control.veredicto.value == "COMPLETE"

    def _oraculo_con_carrera() -> object:
        # El oráculo corre FUERA del lock: acá entra una transición nueva, que el
        # snapshot ya tomado no vio.
        _start_next(root, _propuesta(T2), _aprobacion(T2))
        return _observacion(generation=GEN_OBJETIVO, clone="clone-objetivo")

    resultado = journal_module.reconciliar_journal(root, observador_effective=_oraculo_con_carrera)
    assert resultado.veredicto.value != "COMPLETE"
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "AUTORIDAD_CAMBIADA" in resultado.motivo
    # La transición concurrente sigue intacta: la reconciliación nunca muta.
    assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION"


def test_f9_reserva_de_otro_root_no_es_autoridad_local(tmp_path: pathlib.Path) -> None:
    """F9 — la reserva está root-keyed: la de otro root no es evidencia local (§38.3)."""
    from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

    root = _root(tmp_path)
    otro = _root(tmp_path, "otro-root")
    _crear_pendiente(root)
    _finalizar(root)
    ruta = finalization_seqs_state_dir(root) / "1.json"
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    assert payload["root_id"] == canonical_root_key(root)[0]
    payload["root_id"] = canonical_root_key(otro)[0]
    _escribir_json(ruta, payload)
    with pytest.raises(JournalIdentityError):
        journal_module.auditar_reservas_finalizacion(root)
    assert journal_module.reconciliar_journal(root).veredicto.value == "FAIL_CLOSED"


def test_f11_generation_id_invalido_no_aborta_el_barrido(tmp_path: pathlib.Path) -> None:
    """F11 — ``InvalidGenerationIdError`` se clasifica; no escapa (§34.5).

    Es hermana de ``StateCorruptError`` (ambas ``FrozenRuntimeStorageError``), no
    subclase: sin incluirla, un root malformado abortaba el barrido de arranque
    en vez de aislarse para recuperación por dueño.
    """
    root = _root(tmp_path)
    (root / "state" / "active.json").write_text(
        json.dumps({"schema_version": 1, "desired_active_generation": "../escape", "updated_at_ns": 1}),
        encoding="utf-8",
    )
    resultado = journal_module.reconciliar_journal(root)  # NO debe propagar la excepción
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "DESIRED_ILEGIBLE" in resultado.motivo
    assert resultado.requiere_dueno is True


def test_f12_directorio_o_enlace_de_directorio_nunca_es_placeholder(tmp_path: pathlib.Path) -> None:
    """F12 — sólo un archivo REGULAR de 0 bytes es placeholder.

    ``stat()`` sigue enlaces y en NTFS un directorio reporta ``st_size == 0``:
    un junction/directorio llamado ``<T>.json`` se omitía como placeholder y
    nunca llegaba al lector estricto.
    """
    root = _root(tmp_path)
    # (a) directorio real llamado <T>.json
    (transitions_state_dir(root) / f"{T2}.json").mkdir()
    with pytest.raises(JournalCorruptError):
        journal_module.derivar_historial_finalizados(root)
    # La reconciliación CLASIFICA (no lanza): el directorio es corrupción, no ausencia.
    assert journal_module.reconciliar_journal(root).veredicto.value == "FAIL_CLOSED"

    # (b) enlace a un DIRECTORIO en el store de reservas
    root2 = _root(tmp_path, "root2")
    externo = tmp_path / "externo-dir"
    externo.mkdir()
    ruta_enlace = finalization_seqs_state_dir(root2) / "2.json"
    tipo = _crear_enlace_de_directorio(ruta_enlace, externo)
    assert _exigir_enlace_real(ruta_enlace) == tipo
    with pytest.raises(JournalCorruptError):
        journal_module.auditar_reservas_finalizacion(root2)

    # (c) control positivo: el archivo regular de 0 bytes SÍ es placeholder
    root3 = _root(tmp_path, "root3")
    (transitions_state_dir(root3) / f"{T2}.json").write_bytes(b"")
    vista = journal_module.derivar_historial_finalizados(root3)
    assert vista.registros == ()


def test_f13_store_redirigido_no_es_historial_vacio(tmp_path: pathlib.Path) -> None:
    """F13 — la admisión del directorio precede a ``exists()`` (§34.5 N2).

    Un enlace colgado en ``state/transitions/`` hace ``exists() == False`` y el
    store se degradaba a "historial vacío legítimo" ⇒ N0/N1 limpio.
    """
    root = _root(tmp_path)
    real = root / "state" / "transitions-real"
    os.rename(transitions_state_dir(root), real)
    tipo = _crear_enlace_de_directorio(transitions_state_dir(root), tmp_path / "destino-inexistente")
    assert _exigir_enlace_real(transitions_state_dir(root)) == tipo
    assert not transitions_state_dir(root).exists()  # el caso exacto del hallazgo
    with pytest.raises(JournalIdentityError):
        journal_module.derivar_historial_finalizados(root)
    assert journal_module.reconciliar_journal(root).veredicto.value == "FAIL_CLOSED"


# ============================================================================
# R3 — adjudicación de los hallazgos de Qodo Adversarial sobre `d69d9701`
#
# Autoridad: §36.3/§36.6 (identidad persistida y CAS), §34.4/§34.5
# (`expected_effective_path` = "la ruta que el bind va a producir";
# `UNKNOWN != SUCCESS`), §35.6 (evidencia durable), P2 G03–G05
# (`validar_generation_id`, fail-closed, "todo id que llegue a un path pasó por
# acá").
# ============================================================================

#: Blancos ASCII: `str.strip()` los colapsa a vacío. `""` va como CONTROL — el
#: código previo ya lo rechazaba por `not valor`, así que no aporta RED.
_BLANCOS = ("", " ", "   ", "\t", "\n", " \t\n ")


def _pendiente_crudo(root: pathlib.Path, **cambios: object) -> None:
    """Escribe el PENDING crudo en disco con los campos sobrescritos."""
    payload = _payload_pendiente()
    payload.update(cambios)
    _escribir_json(transition_journal_path(root), payload)


def test_r3_01_identidad_de_transicion_en_blanco_rechaza(tmp_path: pathlib.Path) -> None:
    """R3-01 — un `target_*` solo-whitespace no es una identidad persistible.

    El parser usaba `not valor`, que rechaza `""` pero acepta `"   "`. La
    identidad del target se persiste en el documento durable y se copia al
    registro FINALIZED (§34.4/§36.6): una cadena en blanco no es una identidad,
    y aguas abajo ya no hay forma de distinguirla de un id real.
    """
    root = _root(tmp_path)
    for campo in ("target_generation_id", "target_clone_id", "target_runtime_setup_id"):
        for valor in _BLANCOS:
            _pendiente_crudo(root, **{campo: valor})
            with pytest.raises((JournalSchemaError, JournalIdentityError)):
                journal_module.cargar_journal_transicion(root)


@pytest.mark.parametrize(
    "generation_id",
    [
        "1.6.1170__otra",  # el "digest" no es 12 hex
        "1.6.1170__A1B2C3D4E5F6",  # no canónico (mayúsculas)
        "../escape",  # traversal
        "sin-separador",  # falta `__<digest12>`
        "con__a1b2c3d4e5f6",  # nombre reservado de Windows
        "1.6.1170.0__a1b2c3d4e5f6 ",  # espacio final
    ],
)
def test_r3_01_target_generation_id_no_conforme_rechaza(tmp_path: pathlib.Path, generation_id: str) -> None:
    """R3-01 — el journal no inventa su propio criterio de Generation ID.

    Existe un validador canónico del dominio (`validar_generation_id`, P2
    G03–G05) que rechaza el whitespace de borde, el formato y el traversal; el
    parser sólo exigía "string no vacío". Se exige el id **ya canónico**: se
    VALIDA, no se normaliza — recortar o pasar a minúsculas en silencio cambiaría
    la identidad persistida y el digest que la liga.
    """
    root = _root(tmp_path)
    _pendiente_crudo(root, target_generation_id=generation_id)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_r3_01_source_activation_con_generation_id_en_blanco_rechaza(tmp_path: pathlib.Path) -> None:
    """R3-01 — el `generation_id` del `source_activation` es una identidad más."""
    root = _root(tmp_path)
    fuente = _fuente()
    fuente["generation_id"] = "   "
    _escribir_json(transition_journal_path(root), _payload_pendiente(fuente=fuente))
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_r3_01_evidencia_post_con_identidad_en_blanco_no_finaliza(tmp_path: pathlib.Path) -> None:
    """R3-01 — la evidencia POST tampoco admite identidades en blanco (§35.6).

    Camino HERMANO del parser: `_validar_post_evidence` corre ANTES que la
    comparación de coherencia, así que la identidad en blanco tiene que
    rechazarse por FORMA y no porque "no coincide" con el journal. Se pincha el
    motivo para que el test no pueda pasar por el camino equivocado: sin la
    validación de forma, el rechazo viene de la comparación y el caso queda verde
    sin haber ejercitado lo que declara.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    for campo, kwargs in (
        ("target_generation_id", {"target_generation": "   "}),
        ("target_clone_id", {"target_clone": "   "}),
        ("target_runtime_setup_id", {"target_setup": "\t"}),
        ("desired_active_generation", {"desired": "   "}),
    ):
        with pytest.raises(JournalEvidenciaPostError) as excinfo:
            _finalizar(root, _post_para(root, **kwargs))
        mensaje = str(excinfo.value)
        assert "debe ser" in mensaje, (campo, mensaje)
        assert "inconsistente" not in mensaje, (campo, mensaje)
        assert _dump_estado(root)["registros"] == [], campo
        assert journal_module.cargar_journal_transicion(root).disposition.value == "PENDING_PROMOTION", campo


def test_r3_01_propuesta_con_identidad_en_blanco_rechaza(tmp_path: pathlib.Path) -> None:
    """R3-01 — el camino CREATE valida las mismas identidades que el parser."""
    root = _root(tmp_path)
    for campo, valor in (
        ("target_generation_id", "   "),
        ("target_clone_id", "   "),
        ("target_runtime_setup_id", "\t"),
        ("expected_effective_path", "   "),
    ):
        propuesta = dataclasses.replace(_propuesta(), **{campo: valor})
        with pytest.raises((JournalSchemaError, JournalIdentityError)):
            _crear_pendiente(root, propuesta=propuesta)


def test_r3_01_valores_conformes_siguen_aceptandose(tmp_path: pathlib.Path) -> None:
    """R3-01 (control positivo) — el endurecimiento no rechaza lo válido."""
    root = _root(tmp_path)
    _crear_pendiente(root)
    snapshot = journal_module.cargar_journal_transicion(root)
    assert snapshot.journal is not None
    assert snapshot.journal.target_generation_id == GEN_OBJETIVO
    assert snapshot.journal.target_clone_id == "clone-objetivo"
    assert snapshot.journal.target_runtime_setup_id == "setup-objetivo"
    assert snapshot.journal.expected_effective_path == RUTA_EFECTIVA_OBJETIVO
    _finalizar(root, _post_para(root))
    assert _dump_estado(root)["registros"] == [f"{T1}.json"]


@pytest.mark.parametrize("valor", ["", " ", "   ", "\t", "\n", "E:/rt/versions/x "])
def test_r3_02_update_de_ruta_en_blanco_rechaza(tmp_path: pathlib.Path, valor: str) -> None:
    """R3-02 — UPDATE de `expected_effective_path`: ni blancos ni bordes.

    Es el camino HERMANO del parser y de la propuesta: los tres escriben el mismo
    campo durable (§34.4). `""` ya se rechazaba; `"   "` se persistía y una ruta
    con espacio final no es la ruta que el bind produce.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    revision = _revision_vigente(root)
    with pytest.raises(JournalSchemaError):
        journal_module._actualizar_bajo_lock(
            root,
            expected_transition_id=T1,
            expected_journal_revision=revision,
            cambios={"expected_effective_path": valor},
        )
    # El rechazo no dejó estado a medias: la revisión no avanzó.
    assert _revision_vigente(root) == revision


@pytest.mark.parametrize("valor", [" ", "   ", "\t", "\n", "E:/rt/versions/x "])
def test_r3_02_parser_de_ruta_en_blanco_rechaza(tmp_path: pathlib.Path, valor: str) -> None:
    """R3-02 — el parser del journal es el otro camino de entrada del mismo campo."""
    root = _root(tmp_path)
    _pendiente_crudo(root, expected_effective_path=valor)
    with pytest.raises((JournalSchemaError, JournalIdentityError)):
        journal_module.cargar_journal_transicion(root)


def test_r3_02_update_con_ruta_valida_conserva_cas_y_identidad(tmp_path: pathlib.Path) -> None:
    """R3-02 (control positivo) — una ruta válida sigue siendo actualizable.

    §36.6: el UPDATE avanza la revisión y NO toca la identidad de la transición.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)
    revision = _revision_vigente(root)
    doc = journal_module._actualizar_bajo_lock(
        root,
        expected_transition_id=T1,
        expected_journal_revision=revision,
        cambios={"expected_effective_path": "E:/rt/versions/ruta-post-bind"},
    )
    assert doc.expected_effective_path == "E:/rt/versions/ruta-post-bind"
    assert doc.journal_revision == revision + 1
    assert doc.target_generation_id == GEN_OBJETIVO


def test_r3_03_error_inesperado_del_oraculo_se_registra_y_no_es_exito(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-03 — un bug de programación del oráculo NO se enmascara.

    El `except Exception` de `_invocar_oraculo_effective` convertía TODO fallo en
    `FAIL_CLOSED` con el mismo motivo y sin dejar rastro: un `AttributeError` del
    callback quedaba indistinguible de "el runtime no se pudo observar". Ahora los
    fallos ESPERADOS se traducen a INDETERMINADO y los INESPERADOS se registran
    con traceback en la frontera única de `reconciliar_journal`, que sigue
    fallando cerrado (§34.5: `UNKNOWN != SUCCESS`) pero con motivo propio.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)

    def _oraculo_con_bug() -> object:
        raise AttributeError("'NoneType' object has no attribute 'generation_id'")

    with caplog.at_level(logging.ERROR, logger="sky_claw.local.frozen_runtime.journal"):
        resultado = journal_module.reconciliar_journal(root, observador_effective=_oraculo_con_bug)

    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert resultado.veredicto.value != "COMPLETE"
    assert resultado.requiere_dueno is True
    assert "INESPERADO" in resultado.motivo
    registros = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert registros, "un fallo inesperado del oráculo debe quedar registrado, no enmascarado"
    assert any(r.exc_info is not None and r.exc_info[0] is AttributeError for r in registros), (
        "el registro debe conservar el traceback del fallo inesperado"
    )


def test_r3_03_error_esperado_del_oraculo_es_indeterminado(tmp_path: pathlib.Path) -> None:
    """R3-03 — un fallo ESPERADO de observación sí se traduce a INDETERMINADO.

    El contrato del callback es explícito: los fallos de observación se señalan
    con el error tipado del dominio, y ése SÍ es el vocabulario de
    "no se pudo observar" (§34.5).
    """
    root = _root(tmp_path)
    _crear_pendiente(root)

    def _oraculo_no_disponible() -> object:
        raise JournalObservacionEffectiveError("el runtime no responde")

    resultado = journal_module.reconciliar_journal(root, observador_effective=_oraculo_no_disponible)
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert "EFFECTIVE_INDETERMINADO" in resultado.motivo


@pytest.mark.parametrize(
    "excepcion",
    [RuntimeError("x"), AttributeError("y"), TypeError("z"), KeyError("k"), ValueError("v")],
)
def test_r3_03_ninguna_excepcion_del_oraculo_produce_exito(tmp_path: pathlib.Path, excepcion: Exception) -> None:
    """R3-03 — ninguna excepción del oráculo puede terminar en éxito (§34.5)."""
    root = _root(tmp_path)
    _crear_pendiente(root)

    def _oraculo(exc: Exception = excepcion) -> object:
        raise exc

    resultado = journal_module.reconciliar_journal(root, observador_effective=_oraculo)
    assert resultado.veredicto.value not in {"COMPLETE", "RECOVER"}
    assert resultado.veredicto.value == "FAIL_CLOSED"
    assert resultado.requiere_dueno is True


def test_r3_03_interrupcion_no_se_traga(tmp_path: pathlib.Path) -> None:
    """R3-03 (guarda) — una interrupción NO es un fallo de observación.

    `KeyboardInterrupt` es `BaseException`, no `Exception`: no se traduce a
    veredicto, se propaga. La frontera superior de `reconciliar_journal` tampoco
    la captura. Antes tampoco se tragaba; el test congela la propiedad.
    """
    root = _root(tmp_path)
    _crear_pendiente(root)

    def _oraculo_interrumpido() -> object:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        journal_module.reconciliar_journal(root, observador_effective=_oraculo_interrumpido)


def test_r3_01_todo_error_del_journal_se_exporta_por_el_paquete() -> None:
    """R3-01 (hermano) — un error nuevo del journal no puede quedar sin exportar.

    Lo señaló CodeRabbit en la ronda R3: `JournalObservacionEffectiveError` se
    agregó a `errors.py` pero **no** al paquete. La superficie del paquete es el
    contrato del autor de oráculo: sin el export, un fallo ESPERADO de observación
    sólo se puede señalar importando por ruta interna y, si no se hace, cae en el
    camino "inesperado" (`ORACULO_FALLO_INESPERADO`) en vez de INDETERMINADO.

    El ancla ENUMERA (AGENTS.md: "arreglar un hermano y no al otro"): recorre
    TODAS las clases `Journal*Error` definidas en `errors.py`, así la próxima
    tampoco puede olvidarse.
    """
    import sky_claw.local.frozen_runtime as paquete
    from sky_claw.local.frozen_runtime import errors as errors_module

    definidos = {
        nombre
        for nombre, obj in vars(errors_module).items()
        if isinstance(obj, type)
        and issubclass(obj, errors_module.JournalError)
        and obj.__module__ == errors_module.__name__
    }
    assert definidos, "el censo debe encontrar los errores del journal"
    faltantes = sorted(n for n in definidos if n not in paquete.__all__)
    assert not faltantes, f"errores del journal sin exportar por el paquete: {faltantes}"
    for nombre in sorted(definidos):
        assert hasattr(paquete, nombre), nombre
