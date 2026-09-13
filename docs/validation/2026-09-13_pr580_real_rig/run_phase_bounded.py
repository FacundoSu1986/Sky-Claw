"""run_phase_bounded.py - Wrapper de harness del REAL RIG PR-2.

NO es codigo productivo y NO reemplaza a run_phase.py: lo carga sin modificarlo
(modulo importado, no __main__) y ejecuta su main() con asyncio.run. Al volver,
sale con os._exit(rc).

Motivo (medido con faulthandler sobre la corrida real): el cierre del harness
deja vivo un hilo worker de aiosqlite, y el interprete queda bloqueado en
threading._shutdown esperando ese hilo NO-daemon. asyncio.run(main()) SI
retorna; el cuelgue es posterior, en el apagado del interprete, y no retiene
ninguna lease (etapa 9 y pipeline quedan en 0 filas). os._exit evita ese
apagado sin cambiar la semantica de la corrida ni el codigo producto.

Uso: python run_phase_bounded.py <precheck|A|B> <session-dir>
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import sys

RUN_PHASE = pathlib.Path(__file__).with_name("run_phase.py")


def main() -> int:
    spec = importlib.util.spec_from_file_location("run_phase_harness", RUN_PHASE)
    if spec is None or spec.loader is None:
        print("no se pudo cargar run_phase.py", file=sys.stderr)
        return 2
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)  # setea PHASE/SESSION desde sys.argv
    rc = asyncio.run(modulo.main())
    try:
        return int(rc)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    codigo = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(codigo)
