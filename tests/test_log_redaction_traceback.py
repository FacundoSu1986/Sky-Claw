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
