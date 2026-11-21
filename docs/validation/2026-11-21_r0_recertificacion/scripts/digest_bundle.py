"""R0 Ronda 6 — digest independiente del bundle del bridge (no se commitea).

Carga ``protocol.py`` del checkout fijado (676f4c29) por ruta, SIN importar ``sky_claw``, y calcula
``bundle_digest`` sobre la carpeta indicada, más el SHA-256 de cada archivo. Correr con el intérprete base y ``-I``.

Uso: python -I digest_bundle.py <protocol.py de la fuente> <carpeta del bundle> <salida.json> [--copia-alterada]
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import pathlib
import sys


def _cargar_protocolo(ruta: pathlib.Path):
    spec = importlib.util.spec_from_file_location("r0r6_protocol", ruta)
    modulo = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(modulo)
    return modulo


def main() -> int:
    protocolo, carpeta, salida = (pathlib.Path(a) for a in sys.argv[1:4])
    modulo = _cargar_protocolo(protocolo)
    archivos = {}
    for path in sorted(carpeta.rglob("*")):
        if path.is_file():
            archivos[path.relative_to(carpeta).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    resultado = {
        "calculado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "protocol_py": str(protocolo),
        "protocol_py_sha256": hashlib.sha256(protocolo.read_bytes()).hexdigest(),
        "carpeta": str(carpeta),
        "bundle_digest": modulo.bundle_digest(carpeta),
        "archivos": archivos,
    }
    salida.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
    print(resultado["bundle_digest"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
