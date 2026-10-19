"""Etapa 9 desatendida — worker del driver: corre UN paso en un proceso descartable.

Lee ``paso.json`` del directorio de trabajo, corre :func:`ejecutar_paso_sincrono`
con el backend COM real (``construir_backend_windows``) y la identidad por
``psutil``, escribe ``resultado_paso.json`` y termina. El padre
(``EjecutorDePasoPorHelper``) lo mata si excede el deadline: una llamada COM que
nunca retorna sólo puede colgar a este proceso.

**Import-safe en cualquier plataforma.** En Linux el backend no se construye y el
paso responde ``UIA_NO_DISPONIBLE`` sin actuar; el test lo ancla corriendo el
worker de verdad.

**Invocación.** Fuente: ``python -m sky_claw.local.tools.dyndolod_uia_driver_helper
<directorio>``. Congelado: ``SkyClawApp.exe --skyclaw-uia-driver-helper
<directorio>``, despachado por ``sky_claw/__main__.py`` antes de importar la app.
"""

from __future__ import annotations

import pathlib
import sys

from sky_claw.local.tools.dyndolod_uia_driver import (
    NOMBRE_DEL_PEDIDO_DE_PASO,
    NOMBRE_DEL_RESULTADO_DE_PASO,
    ejecutar_paso_sincrono,
    pedido_de_paso_desde_json,
    resultado_de_paso_a_json,
)
from sky_claw.local.tools.dyndolod_uia_driver_windows import construir_backend_windows
from sky_claw.local.tools.dyndolod_uia_preflight import LocalizadorPsutil


def main(argv: list[str] | None = None) -> int:
    """Corre el paso del directorio indicado. Un pedido ilegible es exit != 0.

    El padre traduce cualquier exit distinto de cero a ``SIN_RESPUESTA``: para un
    paso mutante eso es "desenlace desconocido", que es la lectura honesta de un
    worker que no pudo ni contestar.
    """
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if len(argumentos) != 1:
        _escribir_error("uso: dyndolod_uia_driver_helper <directorio-de-trabajo>")
        return 2
    directorio = pathlib.Path(argumentos[0])
    pedido = pedido_de_paso_desde_json((directorio / NOMBRE_DEL_PEDIDO_DE_PASO).read_text(encoding="utf-8"))
    resultado = ejecutar_paso_sincrono(
        pedido,
        fabrica_backend=construir_backend_windows,
        localizador=LocalizadorPsutil(),
    )
    (directorio / NOMBRE_DEL_RESULTADO_DE_PASO).write_text(resultado_de_paso_a_json(resultado), encoding="utf-8")
    return 0


def _escribir_error(mensaje: str) -> None:
    """stderr puede ser ``None`` en el ejecutable congelado (subsistema GUI)."""
    if sys.stderr is not None:
        sys.stderr.write(mensaje + "\n")
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
