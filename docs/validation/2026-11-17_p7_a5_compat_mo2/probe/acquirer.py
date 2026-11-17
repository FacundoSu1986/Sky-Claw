"""Intento ÚNICO de adquirir la exclusión, con el módulo real.

Sirve para preguntar «¿el archivo está retenido por otro proceso AHORA?» sin
reintentos que enmascaren una contención transitoria.

Uso: python acquirer.py <ini>
"""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> int:
    ini = pathlib.Path(sys.argv[1])

    from sky_claw.local.frozen_runtime import mo2_ini_exclusion as gate

    exclusion = gate.adquirir_exclusion_ini(ini, intentos=1, espera_s=0.0)
    try:
        print(
            json.dumps(
                {
                    "estado": exclusion.estado.value,
                    "autoriza": exclusion.autoriza,
                    "motivo": exclusion.diagnostico.motivo,
                }
            )
        )
    finally:
        exclusion.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
