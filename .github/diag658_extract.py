"""[DIAGNÓSTICO TEMPORAL #658] — REVERTIR junto con el paso del workflow.

Extrae del output de pytest (``ci-test-output.txt``) las líneas
``FAILED``/``ERROR`` y la cola del run, y las publica en el summary del
check (``GITHUB_STEP_SUMMARY``) para que sean legibles por la API del
check-run: los logs de blob de GitHub no son accesibles desde el sandbox
donde se desarrolla este PR.
"""

import os
import pathlib
import re
import traceback

_src = pathlib.Path("ci-test-output.txt")
_dst = pathlib.Path(os.environ.get("GITHUB_STEP_SUMMARY") or "diag658-summary.txt")

try:
    if _src.exists():
        lines = _src.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = []
    falls = [l for l in lines if re.match(r"^(FAILED|ERROR) ", l)][:40]
    partes = ["## [DIAG 658] Fallos del run", ""]
    if falls:
        partes.extend(f"- `{f}`" for f in falls)
    else:
        partes.append("- (sin líneas FAILED/ERROR capturadas; archivo vacío o inexistente)")
    partes += ["", "### Cola del output (últimas 80 líneas)", "```"]
    partes += lines[-80:]
    partes.append("```")
    _dst.write_text("\n".join(partes) + "\n", encoding="utf-8")
except Exception:
    try:
        _dst.write_text("## [DIAG 658] Error en la extracción\n```\n" + traceback.format_exc() + "\n```\n", encoding="utf-8")
    except Exception:
        pass
