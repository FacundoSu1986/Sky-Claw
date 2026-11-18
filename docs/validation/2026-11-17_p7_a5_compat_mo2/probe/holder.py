"""Tenedor de exclusión: usa el módulo REAL de Sky-Claw, no una reimplementación.

Protocolo:
  1. adquiere la exclusión sobre <ini>;
  2. escribe <ready> con el diagnóstico serializado;
  3. espera a que exista <release> (o vence el plazo);
  4. cierra el handle y escribe <released>.

Uso: python holder.py <ini> <ready> <release> <released> [timeout_s]
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def main() -> int:
    ini = pathlib.Path(sys.argv[1])
    ready = pathlib.Path(sys.argv[2])
    release = pathlib.Path(sys.argv[3])
    released = pathlib.Path(sys.argv[4])
    timeout_s = float(sys.argv[5]) if len(sys.argv) > 5 else 120.0

    from sky_claw.local.frozen_runtime import mo2_ini_exclusion as gate

    exclusion = gate.adquirir_exclusion_ini(ini, intentos=1, espera_s=0.001)
    diagnostico = {
        "estado": exclusion.estado.value,
        "autoriza": exclusion.autoriza,
        "motivo": exclusion.diagnostico.motivo,
        "intentos": exclusion.diagnostico.intentos,
        "identidad": None if exclusion.identidad is None else str(exclusion.identidad),
        "pid": os.getpid(),
    }
    # Publicación atómica: el padre parsea apenas el archivo existe, así que un
    # write_text directo le puede dejar JSON a medias y abortar la corrida.
    ready.parent.mkdir(parents=True, exist_ok=True)
    temporal = ready.with_name(ready.name + ".tmp")
    temporal.write_text(json.dumps(diagnostico), encoding="utf-8")
    temporal.replace(ready)

    limite = time.monotonic() + timeout_s
    while not release.exists():
        if time.monotonic() > limite:
            break
        time.sleep(0.005)

    exclusion.cerrar()
    released.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
