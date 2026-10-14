"""Script hijo para los tests de concurrencia real de P4-S3.

Vive en su PROPIO módulo (no como string embebido): el test debe demostrar que
el hijo ejecuta la maquinaria real del journal (``_crear_pendiente_bajo_lock`` /
``_finalizar_transicion_bajo_lock``), que adquiere el root lock REAL, y no una
reimplementación del lock. Es autónomo a propósito: no importa el módulo de
tests (un helper compartido se degradaría sin que nadie lo note).

Se invoca como ``python _p4_s3_child.py <modo> <root> <json-args>`` y escribe
UNA línea JSON a stdout con el resultado tipado.

Aislamiento de imports: PYTHONPATH apunta al worktree P4-S3
(``SKYCLAW_P4_S3_WORKTREE``); se verifica que el journal importado es el del
worktree y no el editable-install del repo principal.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
import traceback

T1 = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90"
T2 = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
SESION = "4d5e6f70-8192-4344-9c8d-7e6f5a4b3c2d"
APROBACION = "3c4d5e6f-7081-4242-9b8c-7d6e5f4a3b2c"
GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"


def _digest(payload: object) -> str:
    canonico = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _fuente() -> dict:
    return {
        "generation_id": GEN_ORIGEN,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": "b" * 64,
    }


def _assert_worktree() -> None:
    raiz = os.environ.get("SKYCLAW_P4_S3_WORKTREE")
    if not raiz:
        return
    import sky_claw.local.frozen_runtime.journal as modulo

    assert pathlib.Path(modulo.__file__).resolve().is_relative_to(pathlib.Path(raiz).resolve()), (
        f"el hijo importó el journal de {modulo.__file__}, no del worktree {raiz}"
    )


def _propuesta(transition_id: str) -> object:
    from sky_claw.local.frozen_runtime.journal import (
        EvidenceDigests,
        JournalOperation,
        PropuestaTransicion,
        SourceActivation,
    )

    fuente = _fuente()
    return PropuestaTransicion(
        transition_id=transition_id,
        operation=JournalOperation.PROMOTION,
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
        expected_effective_path="E:/rt/versions/" + GEN_OBJETIVO,
        created_at_ns=1_700_000_000_000_000_000,
    )


def _aprobacion(transition_id: str) -> object:
    from sky_claw.local.frozen_runtime.journal import AprobacionConsumida

    return AprobacionConsumida(
        approval_id=APROBACION,
        approval_seq=12,
        scope_digest="c" * 64,
        consumed_by_transition_id=transition_id,
    )


def _post(transition_id: str) -> object:
    from sky_claw.local.frozen_runtime.journal import PostVerifyEvidence

    return PostVerifyEvidence(
        transition_id=transition_id,
        resultado="PASS",
        source_activation_digest=_digest(_fuente()),
        target_generation_id=GEN_OBJETIVO,
        target_clone_id="clone-objetivo",
        target_runtime_setup_id="setup-objetivo",
        desired_active_generation=GEN_OBJETIVO,
        effective_observation_digest="1" * 64,
        payload_digest="2" * 64,
        verified_at_ns=1_700_000_000_000_000_001,
    )


def _handshake(args: dict) -> None:
    """Marca el interior de la sección crítica y espera la liberación del test."""
    dentro = args.get("dentro")
    if dentro:
        pathlib.Path(dentro).write_text(str(os.getpid()), encoding="utf-8")
    liberar = args.get("liberar")
    if liberar:
        limite = time.monotonic() + 60.0
        while not pathlib.Path(liberar).exists() and time.monotonic() < limite:
            time.sleep(0.02)


def _envolver_escritura(args: dict) -> None:
    """El handshake corre DENTRO de la sección crítica: se intercepta el commit."""
    from sky_claw.local.frozen_runtime import state as state_module

    original = state_module.write_json_atomic
    marcar = args.get("marcar", True)

    def _envuelto(ruta: pathlib.Path, payload: dict) -> None:
        if marcar:
            _handshake(args)
        return original(ruta, payload)

    state_module.write_json_atomic = _envuelto


def _modo_crear(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.journal import _crear_pendiente_bajo_lock

    _envolver_escritura(args)
    journal = _crear_pendiente_bajo_lock(root, _propuesta(args["transition_id"]), _aprobacion(args["transition_id"]))
    return {
        "ok": True,
        "pid": os.getpid(),
        "transition_id": journal.transition_id,
        "journal_revision": journal.journal_revision,
    }


def _modo_finalizar(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.journal import _finalizar_transicion_bajo_lock

    _envolver_escritura(args)
    registro = _finalizar_transicion_bajo_lock(root, _post(args["transition_id"]))
    return {
        "ok": True,
        "pid": os.getpid(),
        "transition_id": registro.transition_id,
        "finalization_seq": registro.finalization_seq,
    }


_MODOS = {"crear": _modo_crear, "finalizar": _modo_finalizar}


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
