"""
Runner para Mutagen Synthesis - Pipeline de parcheo automatizado.

Este módulo proporciona la integración con Synthesis, la herramienta CLI de Mutagen
para ejecutar pipelines de patchers de forma headless.

Reference:
    - Patrón subprocess: xedit/runner.py:975-1021
    - Synthesis CLI: https://github.com/Mutagen-Modding/Synthesis
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class SynthesisExecutionError(Exception):
    """Error durante la ejecución de Synthesis."""

    def __init__(
        self,
        message: str,
        return_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class SynthesisTimeoutError(SynthesisExecutionError):
    """Error cuando Synthesis excede el timeout configurado."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            message=f"Synthesis timed out after {timeout_seconds}s",
            return_code=None,
            stderr=None,
        )
        self.timeout_seconds = timeout_seconds


class SynthesisNotFoundError(SynthesisExecutionError):
    """Error cuando el ejecutable de Synthesis no se encuentra."""

    def __init__(self, synthesis_path: pathlib.Path) -> None:
        super().__init__(
            message=f"Synthesis executable not found at {synthesis_path}",
            return_code=None,
            stderr=None,
        )


class SynthesisValidationError(SynthesisExecutionError):
    """Error cuando el ESP generado está corrupto o es inválido."""

    def __init__(self, message: str, esp_path: pathlib.Path | None = None) -> None:
        super().__init__(message=message, return_code=None, stderr=None)
        self.esp_path = esp_path


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class SynthesisConfig:
    """Configuración para ejecutar Synthesis.

    Attributes:
        game_path: Ruta al directorio del juego (Skyrim SE/AE).
        mo2_path: Ruta al directorio de Mod Organizer 2.
        output_path: Ruta donde se genera Synthesis.esp.
        synthesis_exe: Ruta al ejecutable de Synthesis CLI.
        timeout_seconds: Timeout en segundos para la ejecución.
    """

    game_path: pathlib.Path
    mo2_path: pathlib.Path
    output_path: pathlib.Path  # Donde se genera Synthesis.esp
    synthesis_exe: pathlib.Path
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        """Valida que los paths requeridos existan."""
        if not self.synthesis_exe.exists():
            raise SynthesisNotFoundError(self.synthesis_exe)


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Resultado de la ejecución de Synthesis.

    Attributes:
        success: True si la ejecución fue exitosa.
        output_esp: Path al ESP generado (o None si falló).
        return_code: Código de retorno del proceso.
        stdout: Salida estándar capturada.
        stderr: Salida de error capturada.
        patchers_executed: Lista de patchers que se ejecutaron.
        errors: Lista de errores detectados en la salida.
    """

    success: bool
    output_esp: pathlib.Path | None
    return_code: int
    stdout: str
    stderr: str
    patchers_executed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# =============================================================================
# SYNTHESIS RUNNER
# =============================================================================


class SynthesisRunner:
    """
    Ejecuta pipelines de Mutagen Synthesis en modo CLI headless.

    Synthesis es una herramienta de patching automatizado que ejecuta
    múltiples patchers en secuencia para generar un plugin final.

    Usage:
        config = SynthesisConfig(
            game_path=Path("C:/Games/Skyrim Special Edition"),
            mo2_path=Path("C:/Modding/MO2"),
            output_path=Path("C:/Modding/MO2/overwrite"),
            synthesis_exe=Path("C:/Modding/Synthesis/Synthesis.CLI.exe"),
        )

        runner = SynthesisRunner(config)
        result = await runner.run_pipeline(
            patcher_ids=["LeveledListsPatcher", "EnemyLevelScalingPatcher"]
        )

        if result.success:
            print(f"ESP generado: {result.output_esp}")
    """

    # Patrones regex para parsear salida de Synthesis
    _PATCHER_PATTERN = re.compile(
        r"(?:Running|Executing|Patchers?:)\s*[:\[]?\s*['\"]?([A-Za-z0-9_.]+)",
        re.IGNORECASE,
    )
    _ERROR_PATTERN = re.compile(
        r"(?:ERROR|Error|FAIL|Exception|Critical):\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    _SUCCESS_PATTERN = re.compile(
        r"(?:Successfully|Complete|Finished|Generated)\s+(?:patch|output|plugin)",
        re.IGNORECASE,
    )

    def __init__(self, config: SynthesisConfig) -> None:
        """
        Inicializa el runner de Synthesis.

        Args:
            config: Configuración con paths y timeout.
        """
        self._config = config
        logger.info(
            "SynthesisRunner inicializado: exe=%s, timeout=%ds",
            config.synthesis_exe,
            config.timeout_seconds,
        )

    async def run_pipeline(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
    ) -> SynthesisResult:
        """
        Ejecuta Synthesis en modo CLI headless.

        Args:
            patcher_ids: Lista de IDs de patchers a ejecutar.
            extra_args: Argumentos adicionales para el CLI.

        Returns:
            SynthesisResult con el resultado de la ejecución.

        Raises:
            SynthesisNotFoundError: Si el ejecutable no existe.
            SynthesisTimeoutError: Si excede el timeout.
        """
        logger.info(
            "Iniciando pipeline Synthesis con %d patchers: %s",
            len(patcher_ids),
            ", ".join(patcher_ids),
        )

        # Construir argumentos del comando
        args = self._build_cli_args(patcher_ids, extra_args)

        try:
            stdout, stderr, return_code = await self._execute_process(args)
        except SynthesisExecutionError:
            raise
        except (TimeoutError, OSError, RuntimeError) as e:
            logger.exception("Error inesperado ejecutando Synthesis: %s", e)
            return SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(e),
                patchers_executed=[],
                errors=[str(e)],
            )

        # Parsear salida para determinar éxito y extraer información
        success, patchers_executed, errors = self.parse_output(stdout, stderr)

        # Determinar path del ESP generado
        output_esp: pathlib.Path | None = None
        if success:
            expected_esp = self._config.output_path / "Synthesis.esp"
            if expected_esp.exists():
                output_esp = expected_esp
            else:
                # Buscar cualquier .esp en el directorio de salida
                esp_files = list(self._config.output_path.glob("*.esp"))
                if esp_files:
                    output_esp = esp_files[0]
                    logger.warning(
                        "Synthesis.esp no encontrado, usando: %s",
                        output_esp,
                    )

        result = SynthesisResult(
            success=success and return_code == 0,
            output_esp=output_esp,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            patchers_executed=patchers_executed,
            errors=errors,
        )

        if result.success:
            logger.info(
                "Pipeline Synthesis exitoso: %s (%d patchers)",
                output_esp,
                len(patchers_executed),
            )
        else:
            logger.error(
                "Pipeline Synthesis falló (code %d): %s",
                return_code,
                "; ".join(errors) if errors else "Unknown error",
            )

        return result

    def _build_cli_args(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
    ) -> list[str]:
        """
        Construye los argumentos del comando CLI de Synthesis.

        Args:
            patcher_ids: Lista de IDs de patchers.
            extra_args: Argumentos adicionales.

        Returns:
            Lista de argumentos para create_subprocess_exec.
        """
        args: list[str] = [
            str(self._config.synthesis_exe),
            "--game-path",
            str(self._config.game_path),
            "--output-path",
            str(self._config.output_path),
            "--no-prompt",  # Modo headless
            "--profile",
            "Default",  # Hardcode profile interact MO2
        ]

        # Añadir patchers
        for patcher_id in patcher_ids:
            args.extend(["--patcher", patcher_id])

        # Añadir argumentos extra
        if extra_args:
            args.extend(extra_args)

        logger.debug("CLI args: %s", " ".join(args))
        return args

    async def _execute_process(
        self,
        args: list[str],
    ) -> tuple[str, str, int]:
        """
        Ejecuta el proceso de Synthesis y captura su salida.

        Patrón tomado de xedit/runner.py:975-1021.

        Args:
            args: Argumentos del comando a ejecutar.

        Returns:
            Tupla con (stdout, stderr, return_code).

        Raises:
            SynthesisNotFoundError: Si el ejecutable no existe.
            SynthesisTimeoutError: Si excede el timeout.
        """
        # Windows: CREATE_NO_WINDOW to avoid console popups.
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.timeout_seconds,
            )
        except FileNotFoundError:
            raise SynthesisNotFoundError(self._config.synthesis_exe) from None
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SynthesisTimeoutError(self._config.timeout_seconds) from None

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        return stdout_text, stderr_text, proc.returncode or 0

    def parse_output(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[bool, list[str], list[str]]:
        """
        Parsea salida para detectar éxito/fallo y patchers ejecutados.

        Args:
            stdout: Salida estándar del proceso.
            stderr: Salida de error del proceso.

        Returns:
            Tupla con (success, patchers_executed, errors).
        """
        combined_output = stdout + "\n" + stderr

        # Detectar patchers ejecutados
        patchers_executed: list[str] = []
        for match in self._PATCHER_PATTERN.finditer(combined_output):
            patcher_name = match.group(1)
            if patcher_name not in patchers_executed:
                patchers_executed.append(patcher_name)

        # Detectar errores
        errors: list[str] = []
        for match in self._ERROR_PATTERN.finditer(combined_output):
            error_msg = match.group(1).strip()
            if error_msg and error_msg not in errors:
                errors.append(error_msg)

        # Determinar éxito
        success = bool(self._SUCCESS_PATTERN.search(combined_output))

        # Si hay errores detectados, el éxito es False
        if errors:
            success = False

        logger.debug(
            "Parse output: success=%s, patchers=%d, errors=%d",
            success,
            len(patchers_executed),
            len(errors),
        )

        return success, patchers_executed, errors

    async def validate_synthesis_esp(self, esp_path: pathlib.Path) -> bool:
        """
        Valida que el ESP generado no esté corrupto.

        Realiza validaciones básicas de integridad:
        - El archivo existe y tiene tamaño > 0
        - Tiene el header de archivo TES4 válido
        - No está vacío o truncado

        Args:
            esp_path: Path al archivo ESP a validar.

        Returns:
            True si el ESP parece válido, False en caso contrario.
        """
        logger.debug("Validando ESP: %s", esp_path)

        try:
            loop = asyncio.get_running_loop()

            def _check_esp() -> tuple[bool, str | int]:
                if not esp_path.exists():
                    return False, "ESP no existe"
                file_size = esp_path.stat().st_size
                if file_size < 100:
                    return False, f"ESP muy pequeño ({file_size} bytes)"
                with open(esp_path, "rb") as f:
                    header = f.read(4)
                    if header != b"TES4":
                        return False, f"ESP no tiene header TES4 válido (got {header})"
                return True, file_size

            valid, result = await loop.run_in_executor(None, _check_esp)

            if not valid:
                logger.warning(f"Error validando ESP {esp_path}: {result}")
                return False

            logger.info("ESP validado correctamente: %s (%s bytes)", esp_path, result)
            return True

        except OSError as e:
            logger.error("Error de I/O validando ESP %s: %s", esp_path, e)
            return False
        except RuntimeError as e:
            logger.exception("Error inesperado validando ESP %s: %s", esp_path, e)
            return False
