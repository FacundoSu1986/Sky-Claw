"""Smoke de arranque del ejecutable PyInstaller, apto para CI de Windows.

El test ejecuta ``SkyClawApp.exe --help`` con perfil, config, logs y temporales
acotados a un directorio descartable. No requiere Skyrim, MO2, credenciales ni
ninguna herramienta de modding instalada.
"""

from __future__ import annotations

import argparse
import locale
import math
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Final

DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_DIAGNOSTIC_LIMIT: Final[int] = 4_000
_TASKKILL_TIMEOUT_SECONDS: Final[float] = 5.0
_PROCESS_REAP_TIMEOUT_SECONDS: Final[float] = 2.0
_SIGNAL_KILL: Final[int] = int(getattr(signal, "SIGKILL", signal.SIGTERM))
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

    def __init__(self, message: str, *, cleanup_warning: str | None = None) -> None:
        super().__init__(message)
        self.cleanup_warning = cleanup_warning


class _ProcessTimeoutError(RuntimeError):
    """Timeout tras intentar terminar el árbol del proceso lanzado."""

    def __init__(self, *, cleanup_warning: str | None = None) -> None:
        super().__init__("El proceso superó el timeout configurado.")
        self.cleanup_warning = cleanup_warning


def _variable_es_sensible(nombre: str) -> bool:
    nombre_normalizado = nombre.upper()
    return (
        nombre_normalizado.startswith(("SKY_CLAW_", "SKYCLAW_"))
        or nombre_normalizado
        in {
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "DOCKER_AUTH_CONFIG",
            "SSH_AUTH_SOCK",
        }
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


def _decodificar_salida(contenido: bytes) -> str:
    """Decodifica UTF-8 y hace fallback a la página local de Windows/CP1252."""
    codificaciones = ["utf-8", locale.getpreferredencoding(do_setlocale=False), "cp1252"]
    visitadas: set[str] = set()
    for codificacion in codificaciones:
        if not codificacion or codificacion.lower() in visitadas:
            continue
        visitadas.add(codificacion.lower())
        try:
            return contenido.decode(codificacion)
        except (LookupError, UnicodeDecodeError):
            continue
    return contenido.decode("utf-8", errors="replace")


def _leer_final(ruta: pathlib.Path, limite: int = _DIAGNOSTIC_LIMIT) -> bytes:
    try:
        with ruta.open("rb") as archivo:
            archivo.seek(0, os.SEEK_END)
            tamano = archivo.tell()
            archivo.seek(max(0, tamano - limite), os.SEEK_SET)
            return archivo.read(limite)
    except OSError:
        return b""


def _recortar_evidencia(texto: str) -> str:
    texto = texto.strip()
    if len(texto) > _DIAGNOSTIC_LIMIT:
        return f"…\n{texto[-_DIAGNOSTIC_LIMIT:]}"
    return texto


def _leer_crash_log(ruta: pathlib.Path) -> str:
    return _recortar_evidencia(_decodificar_salida(_leer_final(ruta)))


def _formatear_evidencia(
    stdout: str,
    stderr: str,
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


def _taskkill_arbol(pid: int) -> str | None:
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        candidato = pathlib.Path(system_root) / "System32" / "taskkill.exe"
        if candidato.is_file():
            taskkill = str(candidato)
    if taskkill is None:
        return "no se encontró taskkill para terminar el árbol de procesos"

    opciones: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        opciones["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        taskkill_process = subprocess.Popen(
            [taskkill, "/T", "/F", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
            **opciones,
        )
    except OSError as exc:
        return f"no se pudo iniciar taskkill: {exc}"

    try:
        taskkill_process.wait(timeout=_TASKKILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            taskkill_process.kill()
        try:
            taskkill_process.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return "taskkill excedió su límite y no se pudo recolectar a tiempo"
        return "taskkill excedió su límite; se forzó el cierre de taskkill"

    if taskkill_process.returncode != 0:
        return f"taskkill terminó con código {taskkill_process.returncode}"
    return None


def _terminar_arbol(proceso: subprocess.Popen[bytes]) -> str | None:
    advertencias: list[str] = []
    if os.name == "nt":
        advertencia = _taskkill_arbol(proceso.pid)
        if advertencia:
            advertencias.append(advertencia)
    else:
        kill_group = getattr(os, "killpg", None)
        if kill_group is None:
            advertencias.append("os.killpg no está disponible para terminar el grupo de procesos")
        else:
            try:
                kill_group(proceso.pid, _SIGNAL_KILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                advertencias.append(f"no se pudo terminar el grupo de procesos: {exc}")

        if advertencias:
            try:
                proceso.kill()
            except OSError as kill_exc:
                advertencias.append(f"tampoco se pudo terminar el proceso padre: {kill_exc}")

    try:
        proceso.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        advertencias.append("el proceso padre no terminó dentro del plazo de recolección")
        try:
            proceso.kill()
        except OSError as exc:
            advertencias.append(f"no se pudo forzar el cierre del proceso padre: {exc}")
        try:
            proceso.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            advertencias.append("el proceso padre siguió activo tras el cierre forzado")

    return "; ".join(advertencias) or None


def _ejecutar_proceso(
    comando: Sequence[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    timeout_s: float,
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
) -> int:
    opciones: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        opciones["creationflags"] = subprocess.CREATE_NO_WINDOW

    proceso: subprocess.Popen[bytes] | None = None
    limpieza_intentada = False
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        try:
            proceso = subprocess.Popen(
                list(comando),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                shell=False,
                **opciones,
            )
            try:
                return proceso.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                limpieza_intentada = True
                advertencia = _terminar_arbol(proceso)
                raise _ProcessTimeoutError(cleanup_warning=advertencia) from exc
        finally:
            if proceso is not None and proceso.poll() is None and not limpieza_intentada:
                _terminar_arbol(proceso)


def _run_smoke_command(
    comando: Sequence[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    timeout_s: float,
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
    crash_log: pathlib.Path,
) -> None:
    try:
        returncode = _ejecutar_proceso(
            comando,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except _ProcessTimeoutError as exc:
        evidencia = _formatear_evidencia(
            _decodificar_salida(_leer_final(stdout_path)),
            _decodificar_salida(_leer_final(stderr_path)),
            crash_log,
        )
        detalle = f"\n\n{evidencia}" if evidencia else ""
        cleanup_warning = exc.cleanup_warning
        limpieza = f"\nAdvertencia de cleanup: {cleanup_warning}" if cleanup_warning else ""
        raise FrozenExecutableSmokeError(
            f"El arranque del ejecutable superó el timeout de {timeout_s:g} s.{detalle}{limpieza}",
            cleanup_warning=cleanup_warning,
        ) from exc
    except OSError as exc:
        raise FrozenExecutableSmokeError(f"No se pudo iniciar el ejecutable empaquetado: {exc}") from exc

    if returncode != 0:
        evidencia = _formatear_evidencia(
            _decodificar_salida(_leer_final(stdout_path)),
            _decodificar_salida(_leer_final(stderr_path)),
            crash_log,
        )
        detalle = f"\n\n{evidencia}" if evidencia else ""
        raise FrozenExecutableSmokeError(f"El ejecutable empaquetado terminó con código {returncode}.{detalle}")


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

    with tempfile.TemporaryDirectory(prefix="skyclaw-frozen-smoke-", ignore_cleanup_errors=True) as directorio_temporal:
        raiz_temporal = pathlib.Path(directorio_temporal)
        perfil = raiz_temporal / "perfil"
        temporal = raiz_temporal / "temp"
        entorno = _construir_entorno_aislado(perfil, temporal)
        crash_log = perfil / ".sky_claw" / "logs" / "crash.log"
        comando = [str(ruta_ejecutable), "--help"]
        _run_smoke_command(
            comando,
            cwd=raiz_temporal,
            env=entorno,
            timeout_s=timeout_s,
            stdout_path=raiz_temporal / "stdout.bin",
            stderr_path=raiz_temporal / "stderr.bin",
            crash_log=crash_log,
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
