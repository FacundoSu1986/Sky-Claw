"""Security: the redaction filter must scrub secrets from exception tracebacks.

``exc_info`` is rendered by the Formatter, which runs AFTER filters, so without
explicit handling a token/password inside an exception message would be logged
unredacted in the traceback. The filter renders + scrubs ``exc_text`` itself.
"""

from __future__ import annotations

import logging
import sys

from sky_claw.logging_config import SecurityRedactionFilter


def test_redaction_filter_scrubs_secret_in_traceback():
    secret = "ghp_" + "A" * 36  # GitHub token shape -> redacted
    try:
        raise ValueError(f"boom {secret}")
    except ValueError:
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="task failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    assert SecurityRedactionFilter().filter(record) is True

    formatted = logging.Formatter("%(message)s").format(record)  # appends exc_text
    assert secret not in formatted
    assert "[REDACTED]" in (record.exc_text or "")


def test_redaction_filter_scrubs_secret_in_stack_info():
    """``stack_info`` (logger.warning(..., stack_info=True)) llega ya renderizado
    como texto plano por la stdlib, a diferencia de ``exc_info`` (una tupla que
    el propio filtro renderiza). Como no es una excepción, ninguna otra rama de
    ``filter()`` lo toca — sin manejo explícito, un token o una ruta de usuario
    que aparezca en la traza de pila viaja sin redactar hasta el archivo.

    Vector real: NiceGUI (la GUI de sky_claw) llama
    ``logging.getLogger("nicegui").warning(..., stack_info=True)`` en
    ``Client.check_existence()`` — no es un caso hipotético.
    """
    secret = "ghp_" + "A" * 36
    record = logging.LogRecord(
        name="t",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="client desconectado",
        args=(),
        exc_info=None,
        sinfo=f"Stack (most recent call last):\n  File {__file__!r}, secret={secret}",
    )

    assert SecurityRedactionFilter().filter(record) is True

    assert secret not in (record.stack_info or "")
    assert "[REDACTED]" in (record.stack_info or "")
