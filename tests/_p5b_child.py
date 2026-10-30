"""Script hijo para pruebas de concurrencia interproceso en P5-B.

Ejecutado como subproceso independiente para comprobar exclusión mutua
y atomicidad entre procesos reales sobre el FrozenRuntimeRoot.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def _modo_registrar(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.clones import (
        RuntimeCloneLifecycle,
        RuntimeCloneRecord,
        registrar_runtime_clone_record,
    )

    record = RuntimeCloneRecord(
        schema_version=args.get("schema_version", 1),
        clone_id=args["clone_id"],
        source_generation_id=args["source_generation_id"],
        lifecycle=RuntimeCloneLifecycle(args["lifecycle"]),
        intended_runtime_setup_id=args["intended_runtime_setup_id"],
        verified_runtime_setup_id=args.get("verified_runtime_setup_id"),
        root_path=args["root_path"],
        admitted_role=args.get("admitted_role", "runtime_clone"),
        created_at_ns=args.get("created_at_ns", 0),
    )
    timeout = float(args.get("timeout", 0.0))
    res = registrar_runtime_clone_record(root, record, timeout=timeout)
    return {
        "ok": True,
        "clone_id": res.clone_id,
        "lifecycle": res.lifecycle.value,
        "pid": os.getpid(),
    }


def _modo_lock_hold(root: pathlib.Path, args: dict) -> dict:
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    ready_file = pathlib.Path(args["ready_file"])
    release_file = pathlib.Path(args["release_file"])
    timeout = float(args.get("timeout", 0.0))

    with participant_lock(root, timeout=timeout) as handle:
        handle.assert_owned()
        ready_file.write_text("ready", encoding="utf-8")
        # Esperar hasta que el test señale liberación
        inicio = time.time()
        while not release_file.exists():
            time.sleep(0.05)
            if time.time() - inicio > 30.0:
                raise TimeoutError("Timeout esperando señal de liberación en proceso hijo")

    return {"ok": True, "pid": os.getpid()}


def main() -> None:
    if len(sys.argv) < 4:
        sys.stderr.write("Uso: python _p5b_child.py <modo> <root> <json_args>\n")
        sys.exit(1)

    modo = sys.argv[1]
    root = pathlib.Path(sys.argv[2])
    args = json.loads(sys.argv[3])

    if modo == "registrar":
        resultado = _modo_registrar(root, args)
    elif modo == "lock_hold":
        resultado = _modo_lock_hold(root, args)
    else:
        sys.stderr.write(f"Modo desconocido: {modo}\n")
        sys.exit(1)

    sys.stdout.write(json.dumps(resultado) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
