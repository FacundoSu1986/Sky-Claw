"""``python -m sky_claw.local.parallax_unattended <informe> <raiz_de_salida>`` (#802).

Imprime el veredicto y sale con ``codigo_de_salida`` (``0`` solo con ``PASS``). Ver ``corrida.py``.
"""

from __future__ import annotations

from .corrida import main

if __name__ == "__main__":
    raise SystemExit(main())
