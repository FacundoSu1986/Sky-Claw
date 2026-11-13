"""Registra y verifica el SHA-256 del ejecutable congelado que producen los workflows.

La evidencia sobre el binario final solo vale si se sabe CUÁL fue. Este script es la única
implementación que usan los tres workflows que construyen ``SkyClawApp.exe`` (``ci.yml``,
``release.yml`` y el laboratorio de Windows); el ancla que los enumera vive en
``tests/test_release_smoke_wiring.py``.

Dos subcomandos, solo con la biblioteca estándar:

* ``registrar EXE [--json RUTA]``: calcula el SHA-256 del ejecutable, lo imprime en una línea
  (``sha256=<hex> bytes=<n> archivo=<nombre>``), lo agrega al resumen del job si existe
  ``GITHUB_STEP_SUMMARY`` y, con ``--json``, lo guarda junto con el contexto del run.
* ``verificar REGISTRO EXE``: vuelve a calcular el hash y falla si el ejecutable cambió después
  de registrarlo. Es lo que hace que «el ejecutable que se probó» y «el que se hasheó» sean el
  mismo archivo.

Códigos de salida: 0 correcto, 1 el ejecutable es distinto del registrado, 2 error (archivo
ausente, registro ilegible o inválido). Ante cualquier duda NO se aprueba.

Lo que un hash NO demuestra: identifica ESTE archivo, no que otro build del mismo commit
produzca los mismos bytes (no se verificó que los builds de PyInstaller sean reproducibles).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_DISTINTO = 1
EXIT_ERROR = 2

#: El ejecutable real pesa decenas de MB: se hashea por bloques, sin leerlo entero en memoria.
TAMANO_DE_BLOQUE = 1024 * 1024

#: Variable de entorno de GitHub Actions -> clave del registro (``None`` fuera de CI).
_CONTEXTO_DE_CI = {
    "GITHUB_SHA": "commit",
    "GITHUB_WORKFLOW": "workflow",
    "GITHUB_JOB": "job",
    "GITHUB_RUN_ID": "run_id",
    "GITHUB_RUN_ATTEMPT": "run_attempt",
}
_SHA256_VALIDO = re.compile(r"[0-9a-f]{64}")


def sha256_de(ruta: Path) -> tuple[str, int]:
    """Devuelve ``(sha256 en hexadecimal minúsculo, tamaño en bytes)`` leyendo por bloques."""
    resumen = hashlib.sha256()
    total = 0
    with ruta.open("rb") as archivo:
        while bloque := archivo.read(TAMANO_DE_BLOQUE):
            resumen.update(bloque)
            total += len(bloque)
    return resumen.hexdigest(), total


def _error(mensaje: str) -> int:
    print(f"registrar_sha256_ejecutable: {mensaje}", file=sys.stderr)
    return EXIT_ERROR


def _agregar_al_resumen_del_job(registro: dict[str, str | int | None]) -> None:
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    linea = (
        f"- `{registro['archivo']}` — SHA-256 `{registro['sha256']}` ({registro['bytes']} bytes)"
        f" — commit `{registro['commit']}`\n"
    )
    with Path(destino).open("a", encoding="utf-8") as resumen:
        resumen.write(linea)


def registrar(exe: Path, json_destino: Path | None) -> int:
    if not exe.is_file():
        return _error(f"el ejecutable no existe o no es un archivo: {exe}")
    try:
        digest, tamano = sha256_de(exe)
    except OSError as exc:
        return _error(f"no se pudo leer {exe}: {exc}")
    registro: dict[str, str | int | None] = {"archivo": exe.name, "sha256": digest, "bytes": tamano}
    registro.update({clave: os.environ.get(variable) for variable, clave in _CONTEXTO_DE_CI.items()})
    print(f"sha256={digest} bytes={tamano} archivo={exe.name}")
    try:
        if json_destino is not None:
            json_destino.parent.mkdir(parents=True, exist_ok=True)
            json_destino.write_text(json.dumps(registro, indent=2) + "\n", encoding="utf-8")
        _agregar_al_resumen_del_job(registro)
    except OSError as exc:
        return _error(f"no se pudo escribir el registro: {exc}")
    return EXIT_OK


def verificar(registro_ruta: Path, exe: Path) -> int:
    try:
        datos = json.loads(registro_ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _error(f"registro ilegible ({registro_ruta}): {exc}")
    esperado = datos.get("sha256") if isinstance(datos, dict) else None
    if not isinstance(esperado, str) or _SHA256_VALIDO.fullmatch(esperado) is None:
        return _error(f"el registro no trae un sha256 válido: {registro_ruta}")
    if not exe.is_file():
        return _error(f"el ejecutable no existe o no es un archivo: {exe}")
    try:
        actual, _ = sha256_de(exe)
    except OSError as exc:
        return _error(f"no se pudo leer {exe}: {exc}")
    if actual != esperado:
        print(
            f"registrar_sha256_ejecutable: ejecutable DISTINTO del registrado (registrado={esperado} actual={actual})",
            file=sys.stderr,
        )
        return EXIT_DISTINTO
    print(f"sha256={actual} coincide con el registro")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    subcomandos = parser.add_subparsers(dest="orden", required=True)
    registrar_parser = subcomandos.add_parser("registrar", help="calcula y registra el SHA-256 del ejecutable")
    registrar_parser.add_argument("exe", type=Path)
    registrar_parser.add_argument("--json", type=Path, default=None, dest="json_destino")
    verificar_parser = subcomandos.add_parser("verificar", help="falla si el ejecutable cambió tras registrarlo")
    verificar_parser.add_argument("registro", type=Path)
    verificar_parser.add_argument("exe", type=Path)
    argumentos = parser.parse_args(argv)
    if argumentos.orden == "registrar":
        return registrar(argumentos.exe, argumentos.json_destino)
    return verificar(argumentos.registro, argumentos.exe)


if __name__ == "__main__":
    raise SystemExit(main())
