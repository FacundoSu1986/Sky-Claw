"""Script hijo para los tests de concurrencia real de P4-S4.

Vive en su PROPIO módulo (no como string embebido) y es autónomo a propósito: no
importa el módulo de tests, así que no puede degradarse en silencio junto con un
helper compartido. El test debe demostrar que el hijo ejecuta la maquinaria REAL
de la autoridad de aprobación (``proponer_aprobacion`` +
``_emitir_aprobacion_bajo_lock`` + ``consumir_aprobacion``), que adquiere el root
lock REAL, y no una reimplementación del lock ni un mock del store.

Se invoca como ``python _p4_s4_child.py <modo> <root> <json-args>`` y escribe UNA
línea JSON a stdout con el resultado tipado (``ok`` o ``error_type``).

Aislamiento de imports: PYTHONPATH apunta al worktree P4-S4
(``SKYCLAW_P4_S4_WORKTREE``); se verifica que el módulo importado es el del
worktree y no el editable-install del repo principal.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import traceback

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"


def _assert_worktree() -> None:
    raiz = os.environ.get("SKYCLAW_P4_S4_WORKTREE")
    if not raiz:
        return
    import sky_claw.local.frozen_runtime.approval as modulo

    assert pathlib.Path(modulo.__file__).resolve().is_relative_to(pathlib.Path(raiz).resolve()), (
        f"el hijo importó approval de {modulo.__file__}, no del worktree {raiz}"
    )


def _fuente() -> object:
    from sky_claw.local.frozen_runtime.journal import SourceActivation

    return SourceActivation(
        generation_id=GEN_ORIGEN,
        clone_id="clone-origen",
        runtime_setup_id="setup-origen",
        effective_runtime_evidence="b" * 64,
    )


def _proponer(root: pathlib.Path, provenance: str, timeout: float) -> object:
    from sky_claw.local.frozen_runtime.approval import ApprovalOperation, proponer_aprobacion

    return proponer_aprobacion(
        root,
        operation=ApprovalOperation.PROMOTION,
        source_activation=_fuente(),
        generation_id=GEN_OBJETIVO,
        clone_id="clone-objetivo",
        clone_evidence="d" * 64,
        runtime_setup_id="setup-objetivo",
        runtime_setup_evidence="e" * 64,
        compatibility_evidence_id="f" * 64,
        candidate_id="cand-1",
        approval_provenance=provenance,
        timeout=timeout,
    )


def _modo_emitir(root: pathlib.Path, args: dict) -> dict:
    """Propone y emite: dos procesos que compiten deben producir UN solo witness."""
    from sky_claw.local.frozen_runtime.approval import (
        ConsentimientoAprobacion,
        _emitir_aprobacion_bajo_lock,
    )
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    # `timeout` acotado: la concurrencia es legítima, así que el perdedor debe
    # ESPERAR el lock y fallar por la semántica de la aprobación (CARRERA_NORMAL /
    # MAX_ISSUED / CAS), no por LOCK_BUSY. Un lock-busy acá mediría el lock, no la
    # autoridad durable.
    propuesta = _proponer(root, args["provenance"], args.get("lock_timeout", 30.0))
    consentimiento = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture-hijo",
    )
    with participant_lock(root, timeout=args.get("lock_timeout", 30.0)) as handle:
        handle.assert_owned()
        ledger = _emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)
    return {
        "ok": True,
        "pid": os.getpid(),
        "approval_seq": ledger.last_issued_approval_seq,
        "approval_id": ledger.last_issued_approval_id,
        "state": ledger.state.value if ledger.state is not None else None,
        "approval_revision": ledger.approval_revision,
    }


def _modo_consumir(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.approval import consumir_aprobacion

    resultado = consumir_aprobacion(
        root,
        approval_id=args["approval_id"],
        approval_seq=args["approval_seq"],
        approval_scope_digest=args["digest"],
        transition_id=args["transition_id"],
        timeout=args.get("lock_timeout", 30.0),
    )
    return {
        "ok": True,
        "pid": os.getpid(),
        "approval_seq": resultado.approval_seq,
        "cache_reconciled": resultado.cache_reconciled,
    }


def _modo_revocar(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.approval import revocar_aprobacion

    ruta = revocar_aprobacion(
        root,
        approval_id=args["approval_id"],
        approval_seq=args["approval_seq"],
        approval_scope_digest=args["digest"],
        motivo=args.get("motivo", "cancelado por el dueño"),
        timeout=args.get("lock_timeout", 30.0),
    )
    return {"ok": True, "pid": os.getpid(), "terminal": ruta.name}


_MODOS = {"emitir": _modo_emitir, "consumir": _modo_consumir, "revocar": _modo_revocar}


def main() -> int:
    _assert_worktree()
    modo = sys.argv[1]
    root = pathlib.Path(sys.argv[2])
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    try:
        resultado = _MODOS[modo](root, args)
    except Exception as exc:  # el test exige el TIPO de fallo, tipado acá
        resultado = {
            "ok": False,
            "pid": os.getpid(),
            "error_type": type(exc).__name__,
            "error_module": type(exc).__module__,
            "detalle": f"{type(exc).__name__}: {exc}",
            "traza": traceback.format_exc()[-2500:],
        }
    sys.stdout.write(json.dumps(resultado) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
