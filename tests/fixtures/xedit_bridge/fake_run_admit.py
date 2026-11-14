"""``run_admit.py`` falso del xEdit Bridge para tests (solo stdlib).

El escenario sale del ``config.json`` (``argv[1]``). Registra los argumentos recibidos en
``admit_args.json`` para que el test verifique que Sky-Claw pasó su propio MO2 y perfil.
"""

import json
import os
import sys
import time
from pathlib import Path

CONFIG = Path(sys.argv[1])
_CONFIG_DATA = json.loads(CONFIG.read_text(encoding="utf-8"))
ESCENARIO = _CONFIG_DATA["escenario"]
CONFIG.with_name("admit_args.json").write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
CONFIG.with_name("entorno_admit.json").write_text(
    json.dumps(
        {
            "TEMP": os.environ.get("TEMP"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT"),
            "SKYCLAW_SECRET_CANARY": os.environ.get("SKYCLAW_SECRET_CANARY"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
            "cwd": os.getcwd(),
        }
    ),
    encoding="utf-8",
)
PLUGIN = sys.argv[sys.argv.index("--plugin") + 1]

if ESCENARIO == "colgado":
    time.sleep(600)
if ESCENARIO == "descontrolado":
    flood_bytes = int(_CONFIG_DATA.get("flood_bytes", 65536))
    sys.stderr.write("A" * flood_bytes)
    sys.stderr.flush()
    time.sleep(60)
if ESCENARIO == "no_json":
    print("Traceback (most recent call last): boom")
    sys.exit(1)
if ESCENARIO == "master_faltante":
    print(
        json.dumps(
            {
                "ok": False,
                "error": "ValueError: missing masters (not in overwrite, enabled mods or game Data): Absent.esm",
            }
        )
    )
    sys.exit(1)
resultado = {
    "ok": True,
    "admission_id": "20261111-000000-abcdef",
    "target": PLUGIN,
    "closure": ["Skyrim.esm", "Update.esm", PLUGIN],
    "copied": [PLUGIN],
    "reused": ["Skyrim.esm", "Update.esm"],
    "replaced": [],
    "data": "C:/bridge/Admitidos/Data",
    "mo2_profile": "Perfil Ñandú",
}
if ESCENARIO == "cp1252":
    # run_admit.py imprime con la codificación de la consola (cp1252 en Windows) cuando la salida es un pipe.
    sys.stdout.buffer.write(json.dumps(resultado, ensure_ascii=False).encode("cp1252") + b"\n")
else:
    print(json.dumps(resultado))
