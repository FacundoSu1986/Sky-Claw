"""T5-v2.1 — worker UIA: corre UN gate en un proceso descartable.

**Qué es.** El proceso que ``EjecutorGatePorHelper`` lanza para cada observación
del protocolo de readiness. Lee el :class:`PedidoDeGate` del directorio de
trabajo, corre ``ejecutar_gate_sincrono`` con el backend COM REAL
(``construir_observador_windows``: la misma pieza que midió el rig T5A), escribe
el resultado serializable y termina. El padre lo mata si excede su deadline
externo, así que una llamada COM que nunca retorna sólo puede colgar a este
proceso — que es descartable por diseño.

**Import-safe en cualquier plataforma.** El módulo se importa (y sus tests
corren) en Linux: ``construir_observador_windows`` no toca COM hasta que el gate
construye el observador, y ahí la ausencia de backend sale como
``UNKNOWN``/``UIA_NO_DISPONIBLE`` por el camino normal del pipeline. El ancla
``test_el_helper_en_linux_responde_uia_no_disponible`` congela esa conducta:
este worker NUNCA revienta por plataforma, responde el fail-closed que
corresponde.

**Por qué archivos y no stdin/stdout.** El ejecutable congelado de Windows es
una app de subsistema GUI (``console=False``): ``sys.stdout`` puede no existir.
Un canal que sólo funciona desde la fuente no sirve para el producto que se
distribuye. Los archivos del directorio temporal funcionan en los dos modos.

**Cómo se lo invoca.** En modo fuente, ``python -m
sky_claw.local.tools.dyndolod_uia_helper <directorio>``. En modo congelado,
``SkyClawApp.exe --skyclaw-uia-helper <directorio>``, despachado por
``sky_claw/__main__.py`` ANTES de importar la aplicación. El worker no sabe de
cuál de las dos formas llegó.
"""

from __future__ import annotations

import pathlib
import sys

from sky_claw.local.tools.dyndolod_uia_gate import (
    NOMBRE_DEL_PEDIDO,
    NOMBRE_DEL_RESULTADO,
    RAZONES_TRANSITORIAS_POR_POLITICA,
    ejecutar_gate_sincrono,
    pedido_desde_json,
    resultado_a_json,
)
from sky_claw.local.tools.dyndolod_uia_preflight import LocalizadorPsutil
from sky_claw.local.tools.dyndolod_uia_windows import construir_observador_windows


def main(argv: list[str] | None = None) -> int:
    """Corre el pedido del directorio indicado. Devuelve el exit code del helper.

    Un fallo de contrato (pedido ilegible) o de escritura sale como excepción y
    exit code distinto de cero: el padre lo traduce a ``UIA_NO_DISPONIBLE``, que
    es el veredicto honesto. Acá no hay una rama que "siga igual".
    """
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if len(argumentos) != 1:
        _escribir_error("uso: dyndolod_uia_helper <directorio-de-trabajo>")
        return 2

    directorio = pathlib.Path(argumentos[0])
    pedido = pedido_desde_json((directorio / NOMBRE_DEL_PEDIDO).read_text(encoding="utf-8"))

    resultado = ejecutar_gate_sincrono(
        pedido.solicitud,
        fabrica_observador=construir_observador_windows,
        localizador=LocalizadorPsutil(),
        timeout_segundos=pedido.timeout_segundos,
        intervalo_segundos=pedido.intervalo_segundos,
        razones_transitorias=RAZONES_TRANSITORIAS_POR_POLITICA[pedido.politica],
    )
    (directorio / NOMBRE_DEL_RESULTADO).write_text(resultado_a_json(resultado), encoding="utf-8")
    return 0


def _escribir_error(mensaje: str) -> None:
    """stderr puede ser ``None`` en el ejecutable congelado (subsistema GUI)."""
    if sys.stderr is not None:
        sys.stderr.write(mensaje + "\n")
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
