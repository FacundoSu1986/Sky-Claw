"""Pandora Behavior Engine Runner for Sky-Claw.

Implements M-02 Pandora Runner specifications.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.output_targets import pandora_output_target

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)


class PandoraExecutionError(Exception):
    pass


class PandoraTimeoutError(PandoraExecutionError):
    """Elevada cuando Pandora excede su timeout (U-10).

    Dedicada (en vez de un ``PandoraResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de salir
    limpio: prerequisito del rollback transaccional de salida (U-04). Deriva de
    :class:`PandoraExecutionError`, así que los callers que ya capturan la base la
    traducen al contrato ``success=False`` sin cambios.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Pandora excedió el timeout de {timeout_seconds:g}s ejecutando el Behavior Engine")


@dataclass(frozen=True, slots=True)
class PandoraConfig:
    pandora_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 300.0


@dataclass
class PandoraResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class PandoraRunner:
    """Asynchronous runner for Pandora Behavior Engine."""

    def __init__(self, config: PandoraConfig):
        self.config = config

    @property
    def output_path(self) -> pathlib.Path:
        output_path = pandora_output_target(game=self.config.game_path)
        if output_path is None:
            raise PandoraExecutionError(
                "No se pudo resolver game_path para construir el destino administrado Pandora_Output."
            )
        return output_path

    async def run_pandora(self) -> PandoraResult:
        """Ejecuta Pandora en modo automático para Skyrim Special Edition."""
        logger.info("[M-02] Executing Pandora Behavior Engine...")
        start_time = time.monotonic()
        game_path = self.config.game_path.resolve()

        # Verificado contra el README de Monitor221hz/Pandora-Behaviour-Engine-Plus,
        # sección "Startup Arguments": las opciones declaradas son `--output` (o
        # `-o`), `--auto_run`, `--auto_close`, `--tesv` y `--skyrim_debug64`.
        #
        # Este runner pasaba además `--game "Skyrim Special Edition"` y `--auto`:
        # ninguno de los dos existe.
        #
        # `--auto_run`/`--auto_close` sí están confirmados en el README: corre el
        # motor reusando los mismos mods activos cacheados de la última corrida
        # exitosa, y cierra el motor automáticamente al terminar un lanzamiento.
        # Su semántica exacta de disparo (¿corre de inmediato, o solo habilita
        # cerrar sin intervención una vez que corrió?) no está probada en rig
        # real — pero omitirlos por completo es estrictamente peor: sin ellos
        # Pandora queda esperando en la GUI hasta el timeout de 300s y hace
        # rollback, con cero chance de completar. Con ellos hay lectura verificada
        # de que es el par documentado para correr desatendido. Pendiente de smoke
        # en rig real (`docs/pending_ooda_status.md`, U-04).
        args = [
            str(self.config.pandora_exe),
            "--auto_run",
            "--auto_close",
            "--output",
            str(self.output_path),
        ]

        try:
            stdout, stderr, return_code = await run_capture(
                args,
                timeout=self.config.timeout_seconds,
                cwd=str(game_path),
            )
        except TimeoutError as exc:
            logger.error("Pandora execution timed out.")
            raise PandoraTimeoutError(self.config.timeout_seconds) from exc
        except Exception as exc:
            logger.error("Pandora execution failed: %s", exc)
            raise PandoraExecutionError(f"Failed to execute Pandora: {exc}") from exc

        return PandoraResult(
            success=return_code == 0,
            return_code=return_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            duration_seconds=time.monotonic() - start_time,
        )
