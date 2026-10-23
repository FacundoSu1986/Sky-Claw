"""Script hijo para los tests de concurrencia real de P4-S5.

Vive en su PROPIO módulo y es autónomo a propósito: no importa el módulo de
tests, así que no puede degradarse en silencio junto con un helper compartido.
El test debe demostrar que el hijo ejecuta la maquinaria REAL de consumo
(``consumir_aprobacion``), que adquiere el root lock REAL, y no una
reimplementación ni un mock del store.

Se invoca como ``python _p4_s5_child.py consumir <root> <json-args>`` y escribe
UNA línea JSON a stdout con el resultado tipado (``ok`` o ``error_type``).

La transición se reconstruye desde el **payload canónico del scope durable** que
recibe el test, de modo que la única diferencia con la transición legítima es la
que declara ``override``. Así el hijo no puede «arreglar» el ataque sin querer.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import traceback

T1 = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90"


def _assert_worktree() -> None:
    raiz = os.environ.get("SKYCLAW_P4_S5_WORKTREE")
    if not raiz:
        return
    import sky_claw.local.frozen_runtime.approval as modulo

    assert pathlib.Path(modulo.__file__).resolve().is_relative_to(pathlib.Path(raiz).resolve()), (
        f"el hijo importó approval de {modulo.__file__}, no del worktree {raiz}"
    )


def _transicion_desde_scope(scope: dict, digest: str, args: dict):
    from sky_claw.local.frozen_runtime import journal as journal_module

    campos: dict[str, object] = {
        "transition_id": args.get("transition_id", T1),
        "operation": journal_module.JournalOperation(scope["operation"]),
        "source_activation": journal_module.SourceActivation(**scope["source_activation"]),
        "source_activation_digest": scope["source_activation_digest"],
        "target_generation_id": scope["generation_id"],
        "target_clone_id": scope["clone_id"],
        "target_runtime_setup_id": scope["runtime_setup_id"],
        "candidate_id": scope["candidate_id"],
        "approval_scope_digest": digest,
        "evidence_digests": journal_module.EvidenceDigests(
            clone_evidence=scope["clone_evidence"],
            runtime_setup_evidence=scope["runtime_setup_evidence"],
            compatibility_evidence=scope["compatibility_evidence_id"],
        ),
        "expected_effective_path": f"E:/rt/versions/{scope['generation_id']}",
        "created_at_ns": 1_700_000_000_000_000_000,
    }
    campos.update(args.get("override", {}))
    return journal_module.PropuestaTransicion(**campos)


def _modo_consumir(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.approval import consumir_aprobacion

    transicion = _transicion_desde_scope(args["scope"], args["digest"], args)
    resultado = consumir_aprobacion(
        root,
        approval_id=args["approval_id"],
        approval_seq=args["approval_seq"],
        approval_scope_digest=args["digest"],
        transition_id=args.get("transition_id", T1),
        transicion=transicion,
        timeout=args.get("lock_timeout", 60.0),
    )
    return {
        "ok": True,
        "pid": os.getpid(),
        "approval_seq": resultado.approval_seq,
        "consumed_by_transition_id": resultado.consumed_by_transition_id,
        "cache_reconciled": resultado.cache_reconciled,
    }


_MODOS = {"consumir": _modo_consumir}


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
