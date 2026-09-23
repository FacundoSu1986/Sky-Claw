"""LOOT CLI wrapper — runs LOOT with correct game path.

The previous implementation incorrectly passed ``mo2_root`` as
``--game-path``.  LOOT requires the real Skyrim SE installation
directory (where ``SkyrimSE.exe`` lives).

TASK-011 enhancements:
- WSL2 conditional path translation via :func:`translate_path_if_wsl`.
- Full async subprocess with ``asyncio.wait_for`` timeout.
- Zombie prevention after timeout, cancellation, or pipe failure: ``kill()`` + ``wait()``.

PR-0 (contrato de identificador de juego): :attr:`LOOTConfig.game` guarda el
**id interno de Sky-Claw** (dominio), no el string de CLI. La conversión al
identificador exacto de ``LOOT.exe --game`` pasa por UNA sola frontera
(:data:`LOOT_CLI_GAME_IDENTIFIERS` / :func:`to_loot_cli_game_id`), evaluada
al construir el argv. Ver la evidencia upstream en el docstring de la frontera.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sky_claw.app.core.windows_interop import translate_path_if_wsl
from sky_claw.local.loot.parser import LOOTOutputParser, LOOTResult
from sky_claw.logging_config import subprocess_error_extra

if TYPE_CHECKING:
    import pathlib

    from sky_claw.app.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60

#: Id de juego interno por defecto del dominio Sky-Claw (Skyrim SE). El
#: dominio usa ids internos; el string de CLI/UI vive solo en la frontera
#: ``LOOT_CLI_GAME_IDENTIFIERS`` (no se propaga al resto del código).
DEFAULT_LOOT_INTERNAL_GAME_ID: Final[str] = "SkyrimSE"

#: Frontera ÚNICA de traducción: id interno de Sky-Claw → identificador exacto
#: de ``LOOT.exe --game``.
#:
#: Evidencia contra ``loot/loot`` tag ``0.29.1`` (commit 77f3ba98):
#:
#: * ``src/gui/qt/main.cpp``: las opciones declaradas son ``--game``,
#:   ``--game-path``, ``--loot-data-path`` y ``--auto-sort`` (más help/version).
#:   ``--update-masterlist`` y ``--sort`` NO existen.
#: * ``src/gui/state/game/game_id.cpp`` (``toString(GameId)``): el dialecto de
#:   ``--game`` son los nombres de carpeta/display — ``"Skyrim Special
#:   Edition"``, ``"Skyrim VR"``. ``"SkyrimSE"``/``"SkyrimVR"`` (dialecto
#:   legacy ≤0.27) NO matchean ningún juego en 0.28+/0.29.x.
#: * ``src/gui/state/loot_state.cpp`` (``setInitialGame``): un ``--game`` no
#:   reconocido NO falla cerrado — ``getPreferredGameFolderName`` devuelve
#:   ``nullopt`` y se cae a ``getFirstInstalledGameFolderName()``: LOOT
#:   selecciona SILENCIOSAMENTE el primer juego instalado y ``--auto-sort``
#:   ordena EL JUEGO EQUIVOCADO. Por eso un id sin traducción se rechaza aquí
#:   (fail-closed) antes de armar el subprocess — nunca se delega en LOOT la
#:   decisión de qué juego es.
LOOT_CLI_GAME_IDENTIFIERS: Final[dict[str, str]] = {
    "SkyrimSE": "Skyrim Special Edition",
    "SkyrimVR": "Skyrim VR",
}


class LOOTGameIdError(ValueError):
    """Id de juego interno sin traducción al dialecto CLI de LOOT (fail-closed).

    Lanzar ``LOOT.exe --game <valor no reconocido>`` en 0.29.x no es un error:
    LOOT cae al primer juego instalado (``setInitialGame``), así que un
    desbalance del mapping silencioso es peor que un fallo explícito.
    """


def to_loot_cli_game_id(internal_game_id: str) -> str:
    """Traduce un id interno de Sky-Claw al identificador CLI exacto de LOOT.

    Única frontera entre el dominio (ids internos) y el dialecto de ``--game``
    de LOOT.exe. Id desconocido → :class:`LOOTGameIdError` (fail-closed, sin
    fallback ni normalización: upstream hace match de string exacto).
    """
    try:
        return LOOT_CLI_GAME_IDENTIFIERS[internal_game_id]
    except KeyError:
        known = ", ".join(sorted(LOOT_CLI_GAME_IDENTIFIERS))
        raise LOOTGameIdError(
            f"El id de juego interno {internal_game_id!r} no tiene traducción al "
            f"dialecto CLI de LOOT (ids conocidos: {known}). No se ejecuta con un "
            "--game no reconocido: LOOT 0.29.x no falla cerrado y ordenaría el "
            "primer juego instalado (loot/loot src/gui/state/loot_state.cpp, setInitialGame)."
        ) from None


@dataclass(frozen=True, slots=True)
class LOOTConfig:
    """Configuration for the LOOT CLI runner.

    ``game`` es el **id interno de Sky-Claw** (p. ej. ``"SkyrimSE"``), no el
    string de CLI: :meth:`LOOTRunner.sort` lo traduce en la frontera única
    :func:`to_loot_cli_game_id` al armar el argv.
    """

    loot_exe: pathlib.Path
    game_path: pathlib.Path
    game: str = DEFAULT_LOOT_INTERNAL_GAME_ID
    timeout: int = DEFAULT_TIMEOUT


class LOOTNotFoundError(FileNotFoundError):
    """Raised when the LOOT executable is not found."""


class LOOTTimeoutError(RuntimeError):
    """Raised when LOOT execution exceeds the configured timeout."""

    def __init__(self, timeout: int) -> None:
        super().__init__(f"LOOT timed out after {timeout}s")
        self.timeout = timeout


async def _reap_process(proc: asyncio.subprocess.Process, *, timeout: float) -> bool:
    """Observa ``proc.wait()`` hasta terminal pese a cancelaciones repetidas.

    Devuelve ``True`` si el proceso fue reapeado dentro del deadline, o
    ``False`` si el ``proc.wait()`` no completó a tiempo — en ese caso el
    proceso puede seguir vivo sin reapear y el caller debe dejar constancia.
    """
    reap_task = asyncio.create_task(asyncio.wait_for(proc.wait(), timeout=timeout))

    while not reap_task.done():
        try:
            await asyncio.shield(reap_task)
        except asyncio.CancelledError:
            if reap_task.cancelled():
                raise
        except (AsyncTimeoutError, TimeoutError):
            return False

    try:
        reap_task.result()
    except (AsyncTimeoutError, TimeoutError):
        return False
    return True


class LOOTRunner:
    """Async wrapper for LOOT CLI with correct path handling.

    TASK-011: Integrates WSL2 path translation so that ``game_path`` is
    automatically converted to Windows format (``C:\\...``) when the agent
    runs inside WSL2.

    Args:
        config: LOOT CLI configuration.
        path_validator: Validator to check paths against sandbox.
    """

    def __init__(
        self,
        config: LOOTConfig,
        path_validator: PathValidator | None = None,
    ) -> None:
        self._config = config
        self._validator = path_validator

    async def sort(self, *, update_masterlist: bool = False) -> LOOTResult:
        """Run LOOT CLI to sort the load order.

        Args:
            update_masterlist: Se conserva por compatibilidad con el caller
                (`loot_service.py` lo propaga con default ``True`` en corridas
                reales). Verificado contra ``loot/loot`` `src/gui/qt/main.cpp`:
                **`--update-masterlist` no es una opción declarada** — pasarlo
                hacía que ``QCommandLineParser`` rechazara TODA la invocación,
                incluido el `--auto-sort` válido. Ya no se agrega. El refresco
                de masterlist tiene una implementación real pero sin cablear en
                :class:`sky_claw.local.loot.masterlist.MasterlistDownloader`
                (descarga vía ``NetworkGateway`` con cache TTL de 24h) que
                ningún caller invocó jamás — cablearlo requiere confirmar el
                layout exacto de directorio que LOOT espera para
                `--loot-data-path` contra el fuente de `libloot`, fuera de
                alcance para una corrección de flag CLI. Hasta entonces este
                parámetro es un no-op documentado: LOOT ordena contra el
                masterlist que ya tenga localmente.

        Returns:
            Parsed LOOT result with warnings, errors, and suggested order.

        Raises:
            LOOTNotFoundError: If the LOOT executable does not exist.
            LOOTTimeoutError: If LOOT exceeds the configured timeout.
            RuntimeError: If LOOT fails for other reasons.
        """
        loot_path = self._config.loot_exe
        game_path = self._config.game_path

        if self._validator is not None:
            self._validator.validate(loot_path)

        if not loot_path.exists():
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}")

        # Frontera única de traducción id interno → dialecto CLI de LOOT (PR-0).
        # Fail-closed ANTES de crear el subprocess: un id sin traducción no se
        # delega a LOOT (0.29.x caería al primer juego instalado y ordenaría
        # el juego equivocado — ver LOOT_CLI_GAME_IDENTIFIERS).
        game_cli_id = to_loot_cli_game_id(self._config.game)

        # TASK-011: Translate game_path to Windows format when under WSL2.
        game_path_win = await translate_path_if_wsl(game_path)

        args = [
            str(loot_path),
            "--game",
            game_cli_id,
            "--game-path",
            game_path_win,
            # Verificado en loot/loot `src/gui/qt/main.cpp`: las opciones
            # declaradas son --game, --game-path, --loot-data-path y --auto-sort.
            # `--sort` no existe y QCommandLineParser rechaza opciones desconocidas.
            "--auto-sort",
        ]

        if update_masterlist:
            logger.warning(
                "LOOT: se pidió update_masterlist=True pero `--update-masterlist` no es un "
                "flag válido de LOOT (verificado contra src/gui/qt/main.cpp) y "
                "MasterlistDownloader no está cableado a este runner; se ordena sin refrescar."
            )

        logger.info("Running LOOT: %s", " ".join(args))

        proc: asyncio.subprocess.Process | None = None
        completed = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.timeout,
            )
            completed = True
        except FileNotFoundError:
            raise LOOTNotFoundError(f"LOOT executable not found at {loot_path}") from None
        except (AsyncTimeoutError, TimeoutError):
            raise LOOTTimeoutError(self._config.timeout) from None
        finally:
            if proc is not None and not completed:
                # Bajo WSL2 proc.pid es el PID Linux de interop: nunca usar taskkill.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as cleanup_error:
                    logger.warning(
                        "LOOT cleanup: proc.kill() falló: %s",
                        cleanup_error,
                        exc_info=True,
                    )

                try:
                    reaped = await _reap_process(proc, timeout=3.0)
                except asyncio.CancelledError as cleanup_error:
                    logger.warning(
                        "LOOT cleanup: proc.wait() fue cancelado: %s",
                        cleanup_error,
                        exc_info=True,
                    )
                except Exception as cleanup_error:
                    logger.warning(
                        "LOOT cleanup: proc.wait() falló: %s",
                        cleanup_error,
                        exc_info=True,
                    )
                else:
                    if not reaped:
                        logger.warning(
                            "LOOT cleanup: proc.wait() no terminó dentro de 3.0s; "
                            "LOOT puede seguir corriendo sin reapear (pid=%s)",
                            proc.pid,
                        )

        assert proc is not None

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.error(
                "LOOT exited with code %d",
                proc.returncode,
                extra=subprocess_error_extra(
                    operation="loot_sort",
                    tool="LOOT",
                    exit_code=proc.returncode,
                    child_pid=proc.pid,
                    stderr=stderr_text,
                    pipeline_stage=5,
                ),
            )

        return LOOTOutputParser.parse(
            stdout=stdout_text,
            stderr=stderr_text,
            return_code=proc.returncode or 0,
        )
