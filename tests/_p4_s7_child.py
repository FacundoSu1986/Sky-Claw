"""Script hijo para la carrera entre procesos de P4-S7.

Vive en su PROPIO módulo (no como string embebido) y es autónomo a propósito: no
importa el módulo de tests, así que no puede degradarse en silencio junto con un
helper compartido. Ejecuta el camino REAL completo —``verificar_identidad_telegram``
→ ``RegistroConsentimientos.presentar`` → ``responder`` →
``_emitir_aprobacion_bajo_lock``— y no una reimplementación ni un mock.

Cada proceso acuña SU propia solicitud, así que el test puede comprobar que el
binding persistido es el del proceso que ganó y no el del perdedor.

Se invoca como ``python _p4_s7_child.py <root>`` y escribe UNA línea JSON a stdout
con ``ok`` o ``error_type``.

Aislamiento de imports: ``SKYCLAW_P4_S7_WORKTREE`` apunta al worktree de la slice;
se verifica que el módulo importado es el del worktree y no el editable-install del
repo principal.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import traceback

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
OPERADOR = 424242


def _assert_worktree() -> None:
    raiz = os.environ.get("SKYCLAW_P4_S7_WORKTREE")
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


def _emitir(root: pathlib.Path) -> dict:
    from sky_claw.local.frozen_runtime.approval import (
        ApprovalOperation,
        _emitir_aprobacion_bajo_lock,
        proponer_aprobacion,
    )
    from sky_claw.local.frozen_runtime.owner_consent import (
        DecisionConsentimiento,
        RegistroConsentimientos,
        verificar_identidad_telegram,
    )
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    # `timeout` acotado: la concurrencia es legítima, así que el perdedor debe
    # ESPERAR el lock y fallar por la semántica de la aprobación (MAX_ISSUED /
    # CAS / CARRERA_NORMAL), no por LOCK_BUSY. Un lock-busy mediría el lock.
    propuesta = proponer_aprobacion(
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
        approval_provenance=f"telegram:pid-{os.getpid()}",
        timeout=30.0,
    )
    registro = RegistroConsentimientos()
    identidad = verificar_identidad_telegram(
        {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}},
        operador_configurado=OPERADOR,
    )
    presentacion = registro.presentar(root, propuesta, identidad=identidad)
    consentimiento = registro.responder(
        presentacion.solicitud_id,
        decision=DecisionConsentimiento.APROBAR,
        identidad=identidad,
    )
    if consentimiento is None:
        raise AssertionError("responder(APROBAR) devolvió None: el hijo no puede emitir")
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        ledger = _emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)
    return {
        "ok": True,
        "pid": os.getpid(),
        "solicitud_id": presentacion.solicitud_id,
        "approval_id": ledger.last_issued_approval_id,
        "approval_seq": ledger.last_issued_approval_seq,
        "state": ledger.state.value if ledger.state is not None else None,
    }


def main() -> int:
    _assert_worktree()
    root = pathlib.Path(sys.argv[1])
    try:
        resultado = _emitir(root)
    except Exception as exc:  # el test exige el TIPO de fallo, tipado acá
        resultado = {
            "ok": False,
            "pid": os.getpid(),
            "error_type": type(exc).__name__,
            "error_module": type(exc).__module__,
            "detalle": f"{type(exc).__name__}: {exc}",
            "traza": traceback.format_exc()[-2000:],
        }
    sys.stdout.write(json.dumps(resultado) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
