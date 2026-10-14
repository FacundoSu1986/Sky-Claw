"""Smoke de arranque del ejecutable PyInstaller, apto para CI de Windows.

El test ejecuta ``SkyClawApp.exe --help`` con perfil, config, logs y temporales
acotados a un directorio descartable. No requiere Skyrim, MO2, credenciales ni
ninguna herramienta de modding instalada.
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Final

DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_DIAGNOSTIC_LIMIT: Final[int] = 4_000
_SENSITIVE_ENV_MARKERS: Final[tuple[str, ...]] = (
    "ACCESS_KEY",
    "API_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class FrozenExecutableSmokeError(RuntimeError):
    """El ejecutable empaquetado no superó la prueba de arranque."""


def _variable_es_sensible(nombre: str) -> bool:
    nombre_normalizado = nombre.upper()
    return (
        nombre_normalizado.startswith(("SKY_CLAW_", "SKYCLAW_"))
        or nombre_normalizado in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}
        or any(marca in nombre_normalizado for marca in _SENSITIVE_ENV_MARKERS)
    )


def _construir_entorno_aislado(perfil: pathlib.Path, temporal: pathlib.Path) -> dict[str, str]:
    """Conserva el entorno de Windows y descarta config y secretos heredados."""
    entorno = {nombre: valor for nombre, valor in os.environ.items() if not _variable_es_sensible(nombre)}
    appdata = perfil / "AppData" / "Roaming"
    local_appdata = perfil / "AppData" / "Local"
    for directorio in (perfil, appdata, local_appdata, temporal):
        directorio.mkdir(parents=True, exist_ok=True)

    entorno.update(
        {
            "HOME": str(perfil),
            "USERPROFILE": str(perfil),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(local_appdata),
            "TEMP": str(temporal),
            "TMP": str(temporal),
        }
    )
    return entorno


def _recortar_evidencia(texto: str | bytes | None) -> str:
    if not texto:
        return ""
    if isinstance(texto, bytes):
        texto = texto.decode("utf-8", errors="replace")
    texto = texto.strip()
    if len(texto) > _DIAGNOSTIC_LIMIT:
        return f"…\n{texto[-_DIAGNOSTIC_LIMIT:]}"
    return texto


def _leer_crash_log(ruta: pathlib.Path) -> str:
    try:
        return _recortar_evidencia(ruta.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _formatear_evidencia(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    crash_log: pathlib.Path,
) -> str:
    secciones: list[str] = []
    for nombre, contenido in (("stderr", stderr), ("stdout", stdout)):
        evidencia = _recortar_evidencia(contenido)
        if evidencia:
            secciones.append(f"{nombre}:\n{evidencia}")
    log = _leer_crash_log(crash_log)
    if log:
        secciones.append(f"crash.log:\n{log}")
    return "\n\n".join(secciones)


def smoke_frozen_executable(
    executable: pathlib.Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Arranca el binario congelado en un perfil temporal y falla si no inicia.

    ``--help`` fuerza el bootstrap del bundle y del parser sin iniciar la GUI,
    abrir servicios de red o realizar operaciones sobre MO2/Skyrim.
    """
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise FrozenExecutableSmokeError("El timeout debe ser finito y mayor que cero.")

    ruta_ejecutable = pathlib.Path(executable)
    try:
        ruta_ejecutable = ruta_ejecutable.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FrozenExecutableSmokeError(f"El ejecutable no existe o no se puede resolver: {ruta_ejecutable}") from exc
    if not ruta_ejecutable.is_file():
        raise FrozenExecutableSmokeError(f"La ruta no apunta a un ejecutable: {ruta_ejecutable}")

    with tempfile.TemporaryDirectory(prefix="skyclaw-frozen-smoke-") as directorio_temporal:
        raiz_temporal = pathlib.Path(directorio_temporal)
        perfil = raiz_temporal / "perfil"
        temporal = raiz_temporal / "temp"
        entorno = _construir_entorno_aislado(perfil, temporal)
        crash_log = perfil / ".sky_claw" / "logs" / "crash.log"
        comando = [str(ruta_ejecutable), "--help"]
        resultado: subprocess.CompletedProcess[str] | None = None

        try:
            resultado = subprocess.run(
                comando,
                cwd=raiz_temporal,
                env=entorno,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                shell=False,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            evidencia = _formatear_evidencia(exc.stdout, exc.stderr, crash_log)
            detalle = f"\n\n{evidencia}" if evidencia else ""
            raise FrozenExecutableSmokeError(
                f"El arranque del ejecutable superó el timeout de {timeout_s:g} s.{detalle}"
            ) from exc
        except OSError as exc:
            raise FrozenExecutableSmokeError(f"No se pudo iniciar el ejecutable empaquetado: {exc}") from exc

        if resultado.returncode != 0:
            evidencia = _formatear_evidencia(resultado.stdout, resultado.stderr, crash_log)
            detalle = f"\n\n{evidencia}" if evidencia else ""
            raise FrozenExecutableSmokeError(
                f"El ejecutable empaquetado terminó con código {resultado.returncode}.{detalle}"
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke de arranque de Sky-Claw empaquetado.")
    parser.add_argument("executable", type=pathlib.Path, help="Ruta al ejecutable generado por PyInstaller.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Tiempo máximo de arranque en segundos (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        smoke_frozen_executable(args.executable, timeout_s=args.timeout)
    except FrozenExecutableSmokeError as exc:
        print(f"Smoke del ejecutable congelado falló: {exc}", file=sys.stderr)
        return 1

    print(f"Smoke del ejecutable congelado aprobado: {args.executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
