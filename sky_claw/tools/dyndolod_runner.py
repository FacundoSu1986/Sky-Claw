"""
Runner para DynDOLOD y TexGen - Pipeline de generación de LODs.

Este módulo proporciona la integración con DynDOLOD y TexGen para generar
LODs de forma automatizada con empaquetado para Mod Organizer 2.

Reference:
    - Patrón subprocess: synthesis_runner.py:303-348
    - DynDOLOD CLI: https://dyndolod.info/Help/Command-Line-Interface
"""

from __future__ import annotations

import contextlib
import asyncio
import configparser
import logging
import pathlib
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class DynDOLODExecutionError(Exception):
    """Base exception for DynDOLOD execution errors."""

    def __init__(
        self,
        message: str,
        return_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class DynDOLODTimeoutError(DynDOLODExecutionError):
    """Raised when DynDOLOD/TexGen execution exceeds timeout."""

    def __init__(self, timeout_seconds: int, tool_name: str = "DynDOLOD") -> None:
        super().__init__(
            message=f"{tool_name} execution timed out after {timeout_seconds} seconds",
            return_code=None,
            stderr=None,
        )
        self.timeout_seconds = timeout_seconds
        self.tool_name = tool_name


class DynDOLODNotFoundError(DynDOLODExecutionError):
    """Raised when DynDOLOD/TexGen executable cannot be found."""

    def __init__(self, executable_path: pathlib.Path) -> None:
        super().__init__(
            message=f"Executable not found: {executable_path}",
            return_code=None,
            stderr=None,
        )
        self.executable_path = executable_path


class DynDOLODValidationError(DynDOLODExecutionError):
    """Raised when output validation fails."""

    def __init__(self, message: str, output_path: pathlib.Path | None = None) -> None:
        super().__init__(message=message, return_code=None, stderr=None)
        self.output_path = output_path


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class DynDOLODConfig:
    """Configuration for DynDOLOD/TexGen execution.

    Attributes:
        game_path: Ruta al directorio del juego (Skyrim SE/AE).
        mo2_path: Ruta al directorio de Mod Organizer 2.
        mo2_mods_path: Ruta a la carpeta mods de MO2.
        dyndolod_exe: Ruta al ejecutable de DynDOLOD.
        texgen_exe: Ruta al ejecutable de TexGen (opcional).
        timeout_seconds: Timeout en segundos para la ejecución (default: 4 horas).
        heartbeat_interval: Segundos entre logs de heartbeat.
        preset: Nivel de calidad del preset (Low, Medium, High).
    """

    game_path: pathlib.Path
    mo2_path: pathlib.Path
    mo2_mods_path: pathlib.Path
    dyndolod_exe: pathlib.Path
    texgen_exe: pathlib.Path | None = None
    timeout_seconds: int = 14400  # 4 horas por defecto
    heartbeat_interval: int = 60  # Segundos entre logs de heartbeat
    preset: str = "Medium"  # Low, Medium, High

    def __post_init__(self) -> None:
        """Valida que los paths requeridos existan."""
        if not self.game_path.exists():
            raise ValueError(f"Game path does not exist: {self.game_path}")
        if not self.dyndolod_exe.exists():
            raise DynDOLODNotFoundError(self.dyndolod_exe)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Result of a single tool execution (TexGen or DynDOLOD).

    Attributes:
        success: True si la ejecución fue exitosa.
        tool_name: Nombre de la herramienta ejecutada.
        return_code: Código de retorno del proceso.
        stdout: Salida estándar capturada.
        stderr: Salida de error capturada.
        output_path: Path al directorio de salida generado.
        errors: Lista de errores detectados en la salida.
        warnings: Lista de warnings detectados en la salida.
        duration_seconds: Duración de la ejecución en segundos.
    """

    success: bool
    tool_name: str
    return_code: int
    stdout: str
    stderr: str
    output_path: pathlib.Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class DynDOLODPipelineResult:
    """Complete result of TexGen + DynDOLOD pipeline.

    Attributes:
        success: True si todo el pipeline fue exitoso.
        texgen_result: Resultado de la ejecución de TexGen.
        dyndolod_result: Resultado de la ejecución de DynDOLOD.
        texgen_mod_path: Path al mod empaquetado de TexGen.
        dyndolod_mod_path: Path al mod empaquetado de DynDOLOD.
        errors: Lista de errores acumulados del pipeline.
    """

    success: bool
    texgen_result: ToolExecutionResult | None
    dyndolod_result: ToolExecutionResult | None
    texgen_mod_path: pathlib.Path | None = None
    dyndolod_mod_path: pathlib.Path | None = None
    errors: list[str] = field(default_factory=list)


# =============================================================================
# DYNDOLLOD RUNNER
# =============================================================================


class DynDOLODRunner:
    """
    Runner asíncrono para TexGen y DynDOLOD con empaquetado automático para MO2.

    DynDOLOD es una herramienta de generación de LODs que crea objetos distantes
    y texturas optimizadas para mejorar la visualización a larga distancia.

    Patrones de salida esperados:
    - TexGen: <TexGen_Output>/ - Contiene texturas LOD
    - DynDOLOD: <DynDOLOD_Output>/ - Contiene DynDOLOD.esp y assets

    Usage:
        config = DynDOLODConfig(
            game_path=Path("C:/Games/Skyrim Special Edition"),
            mo2_path=Path("C:/Modding/MO2"),
            mo2_mods_path=Path("C:/Modding/MO2/mods"),
            dyndolod_exe=Path("C:/Modding/DynDOLOD/DynDOLODx64.exe"),
            texgen_exe=Path("C:/Modding/DynDOLOD/TexGenx64.exe"),
        )

        runner = DynDOLODRunner(config)
        result = await runner.run_full_pipeline(preset="Medium")

        if result.success:
            print(f"TexGen mod: {result.texgen_mod_path}")
            print(f"DynDOLOD mod: {result.dyndolod_mod_path}")
    """

    # Patrones regex para parsing de salida
    _SUCCESS_PATTERN = re.compile(
        r"(?:Successfully|Complete|Finished|Generated|Created)\s+(?:LOD|output|textures|plugin)",
        re.IGNORECASE,
    )
    _ERROR_PATTERN = re.compile(
        r"(?:ERROR|Error|FAIL|Exception|Critical|FATAL):\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    _WARNING_PATTERN = re.compile(
        r"(?:WARNING|Warning|WARN):\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    _PROGRESS_PATTERN = re.compile(
        r"(?:Processing|Generating|Creating|Writing)\s*[:\[]?\s*['\"]?([A-Za-z0-9_./\\]+)",
        re.IGNORECASE,
    )

    # Nombres de directorios de salida estándar
    TEXGEN_OUTPUT_NAME = "TexGen_Output"
    DYNDOLLOD_OUTPUT_NAME = "DynDOLOD_Output"
    TEXGEN_MOD_NAME = "TexGen Output"
    DYNDOLLOD_MOD_NAME = "DynDOLOD Output"

    def __init__(self, config: DynDOLODConfig) -> None:
        """
        Inicializa el runner de DynDOLOD.

        Args:
            config: Configuración con paths y timeouts.
        """
        self._config = config
        logger.info(
            "DynDOLODRunner inicializado: dyndolod_exe=%s, texgen_exe=%s, timeout=%ds",
            config.dyndolod_exe,
            config.texgen_exe or "N/A",
            config.timeout_seconds,
        )

    async def run_texgen(
        self, extra_args: list[str] | None = None
    ) -> ToolExecutionResult:
        """
        Ejecuta TexGen en modo headless.

        CLI esperado: TexGenx64.exe -t (para Skyrim SE/AE)

        Args:
            extra_args: Argumentos adicionales de línea de comandos.

        Returns:
            ToolExecutionResult con el resultado de la ejecución.

        Raises:
            DynDOLODNotFoundError: Si el ejecutable de TexGen no existe.
            DynDOLODTimeoutError: Si excede el timeout.
        """
        if self._config.texgen_exe is None:
            return ToolExecutionResult(
                success=False,
                tool_name="TexGen",
                return_code=-1,
                stdout="",
                stderr="TexGen executable not configured",
                errors=["TexGen executable path not provided in configuration"],
            )

        logger.info("Iniciando TexGen para generación de texturas LOD")

        args = self._build_texgen_args(extra_args)

        try:
            stdout, stderr, return_code, duration = await self._execute_process(
                executable=self._config.texgen_exe,
                args=args,
                tool_name="TexGen",
            )
        except DynDOLODExecutionError:
            raise
        except Exception as e:
            logger.exception("Error inesperado ejecutando TexGen: %s", e)
            return ToolExecutionResult(
                success=False,
                tool_name="TexGen",
                return_code=-1,
                stdout="",
                stderr=str(e),
                errors=[str(e)],
            )

        # Parsear salida
        errors, warnings = self._parse_output(stdout, stderr)
        success = return_code == 0 and not errors

        # Determinar path de salida
        output_path = self._find_texgen_output()

        result = ToolExecutionResult(
            success=success,
            tool_name="TexGen",
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            output_path=output_path,
            errors=errors,
            warnings=warnings,
            duration_seconds=duration,
        )

        if result.success:
            logger.info(
                "TexGen completado exitosamente en %.1fs: %s",
                duration,
                output_path,
            )
        else:
            logger.error(
                "TexGen falló (code %d): %s",
                return_code,
                "; ".join(errors) if errors else "Unknown error",
            )

        return result

    async def run_dyndolod(
        self,
        preset: str = "Medium",
        extra_args: list[str] | None = None,
    ) -> ToolExecutionResult:
        """
        Ejecuta DynDOLOD en modo headless.

        CLI esperado: DynDOLODx64.exe -p <preset> -t (para Skyrim SE/AE)

        Args:
            preset: Nivel de calidad (Low, Medium, High).
            extra_args: Argumentos adicionales de línea de comandos.

        Returns:
            ToolExecutionResult con el resultado de la ejecución.

        Raises:
            DynDOLODNotFoundError: Si el ejecutable no existe.
            DynDOLODTimeoutError: Si excede el timeout.
        """
        logger.info("Iniciando DynDOLOD con preset: %s", preset)

        args = self._build_dyndolod_args(preset, extra_args)

        try:
            stdout, stderr, return_code, duration = await self._execute_process(
                executable=self._config.dyndolod_exe,
                args=args,
                tool_name="DynDOLOD",
            )
        except DynDOLODExecutionError:
            raise
        except Exception as e:
            logger.exception("Error inesperado ejecutando DynDOLOD: %s", e)
            return ToolExecutionResult(
                success=False,
                tool_name="DynDOLOD",
                return_code=-1,
                stdout="",
                stderr=str(e),
                errors=[str(e)],
            )

        # Parsear salida
        errors, warnings = self._parse_output(stdout, stderr)
        success = return_code == 0 and not errors

        # Determinar path de salida
        output_path = self._find_dyndolod_output()

        result = ToolExecutionResult(
            success=success,
            tool_name="DynDOLOD",
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            output_path=output_path,
            errors=errors,
            warnings=warnings,
            duration_seconds=duration,
        )

        if result.success:
            logger.info(
                "DynDOLOD completado exitosamente en %.1fs: %s",
                duration,
                output_path,
            )
        else:
            logger.error(
                "DynDOLOD falló (code %d): %s",
                return_code,
                "; ".join(errors) if errors else "Unknown error",
            )

        return result

    async def _execute_process(
        self,
        executable: pathlib.Path,
        args: list[str],
        tool_name: str,
        timeout: int | None = None,
    ) -> tuple[str, str, int, float]:
        """
        Ejecuta un proceso con manejo de heartbeat para procesos largos.

        REQUISITOS:
        - Usar asyncio.create_subprocess_exec
        - Flag CREATE_NO_WINDOW (0x08000000) en Windows
        - Timeout configurable (default: 14400 segundos = 4 horas)
        - Sistema de Heartbeat: log cada 60 segundos mientras el proceso está vivo
        - Capturar stdout y stderr por separado

        Args:
            executable: Path al ejecutable.
            args: Argumentos del comando.
            tool_name: Nombre de la herramienta para logs.
            timeout: Timeout en segundos (usa config default si es None).

        Returns:
            tuple[str, str, int, float]: (stdout, stderr, return_code, duration_seconds)

        Raises:
            DynDOLODNotFoundError: Si el ejecutable no existe.
            DynDOLODTimeoutError: Si se excede el timeout.
            DynDOLODExecutionError: Si el proceso falla críticamente.
        """
        effective_timeout = (
            timeout if timeout is not None else self._config.timeout_seconds
        )
        heartbeat_interval = self._config.heartbeat_interval

        # Windows: CREATE_NO_WINDOW to avoid console popups.
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        logger.info(
            "Ejecutando %s: %s %s (timeout: %ds)",
            tool_name,
            executable,
            " ".join(args),
            effective_timeout,
        )

        start_time = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                str(executable),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError:
            raise DynDOLODNotFoundError(executable)
        except OSError as e:
            raise DynDOLODExecutionError(
                f"Failed to start {tool_name}: {e}",
                return_code=None,
                stderr=str(e),
            )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _drain(
            stream: asyncio.StreamReader,
            target: list[bytes],
        ) -> None:
            """Read stream until EOF, collecting chunks."""
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                target.append(chunk)

        async def _heartbeat_watcher() -> None:
            """Log a heartbeat every heartbeat_interval seconds."""
            while True:
                await asyncio.sleep(heartbeat_interval)
                elapsed = time.monotonic() - start_time
                logger.info(
                    "%s heartbeat: %.1fs elapsed, process still running",
                    tool_name,
                    elapsed,
                )

        drain_out = asyncio.create_task(_drain(proc.stdout, stdout_chunks))
        drain_err = asyncio.create_task(_drain(proc.stderr, stderr_chunks))
        heartbeat = asyncio.create_task(_heartbeat_watcher())

        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            # Global timeout exceeded — kill process and cancel tasks.
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(
                    heartbeat, drain_out, drain_err, return_exceptions=True
                )
            raise DynDOLODTimeoutError(effective_timeout, tool_name)
        except Exception as e:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(
                    heartbeat, drain_out, drain_err, return_exceptions=True
                )
            raise DynDOLODExecutionError(
                f"Unexpected error during {tool_name} execution: {e}",
                return_code=proc.returncode,
                stderr=str(e),
            )
        else:
            # Process exited normally — cancel heartbeat and wait for drains to finish.
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            results = await asyncio.gather(drain_out, drain_err, return_exceptions=True)
            for r in results:
                if isinstance(r, BaseException):
                    logger.warning(
                        "%s drain task falló inesperadamente: %r",
                        tool_name,
                        r,
                    )

        duration = time.monotonic() - start_time
        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")

        logger.info(
            "%s finalizado: return_code=%d, duration=%.1fs",
            tool_name,
            proc.returncode if proc.returncode is not None else -1,
            duration,
        )

        return (
            stdout_text,
            stderr_text,
            proc.returncode if proc.returncode is not None else -1,
            duration,
        )

    async def _package_output_as_mod(
        self,
        output_path: pathlib.Path,
        mod_name: str,
    ) -> pathlib.Path:
        """
        Empaqueta la salida de una herramienta como un mod válido para MO2.

        Pasos:
        1. Crear directorio en self._config.mo2_mods_path / mod_name
        2. Copiar todo el contenido de output_path al nuevo directorio
        3. Generar meta.ini válido

        Args:
            output_path: Path al directorio de salida de la herramienta.
            mod_name: Nombre del mod a crear.

        Returns:
            pathlib.Path: Ruta al directorio del mod creado.

        Raises:
            DynDOLODValidationError: Si falla la creación del mod.
        """
        mod_path = self._config.mo2_mods_path / mod_name

        logger.info("Empaquetando mod: %s -> %s", output_path, mod_path)

        try:
            # Verificar que el directorio de salida existe
            if not output_path.exists():
                raise DynDOLODValidationError(
                    f"Output directory does not exist: {output_path}",
                    output_path=output_path,
                )

            # Verificar que tiene contenido
            if not any(output_path.iterdir()):
                raise DynDOLODValidationError(
                    f"Output directory is empty: {output_path}",
                    output_path=output_path,
                )

            # Crear directorio del mod (o limpiar si existe)
            if mod_path.exists():
                logger.debug("Limpiando directorio existente: %s", mod_path)
                shutil.rmtree(mod_path)

            mod_path.mkdir(parents=True, exist_ok=True)

            # Copiar contenido
            for item in output_path.iterdir():
                src = item
                dst = mod_path / item.name
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

            # Generar meta.ini
            self._generate_meta_ini(mod_path, mod_name)

            logger.info("Mod empaquetado exitosamente: %s", mod_path)
            return mod_path

        except DynDOLODValidationError:
            raise
        except PermissionError as e:
            raise DynDOLODValidationError(
                f"Permission denied creating mod: {e}",
                output_path=mod_path,
            )
        except OSError as e:
            raise DynDOLODValidationError(
                f"Failed to package mod: {e}",
                output_path=mod_path,
            )

    async def run_full_pipeline(
        self,
        run_texgen: bool = True,
        preset: str = "Medium",
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
    ) -> DynDOLODPipelineResult:
        """
        Ejecuta el pipeline completo: TexGen → Empaquetado → DynDOLOD → Empaquetado.

        Flujo:
        1. Ejecutar TexGen (si run_texgen=True)
        2. Empaquetar TexGen_Output como "TexGen Output"
        3. Ejecutar DynDOLOD (después de que TexGen esté listo)
        4. Empaquetar DynDOLOD_Output como "DynDOLOD Output"

        Args:
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD.
            preset: Nivel de calidad para DynDOLOD (Low, Medium, High).
            texgen_args: Argumentos adicionales para TexGen.
            dyndolod_args: Argumentos adicionales para DynDOLOD.

        Returns:
            DynDOLODPipelineResult con resultados completos.
        """
        logger.info(
            "Iniciando pipeline DynDOLOD completo: run_texgen=%s, preset=%s",
            run_texgen,
            preset,
        )

        errors: list[str] = []
        texgen_result: ToolExecutionResult | None = None
        dyndolod_result: ToolExecutionResult | None = None
        texgen_mod_path: pathlib.Path | None = None
        dyndolod_mod_path: pathlib.Path | None = None

        # Paso 1: Ejecutar TexGen si está habilitado
        if run_texgen:
            try:
                texgen_result = await self.run_texgen(extra_args=texgen_args)

                if texgen_result.success and texgen_result.output_path:
                    # Empaquetar TexGen Output
                    try:
                        texgen_mod_path = await self._package_output_as_mod(
                            texgen_result.output_path,
                            self.TEXGEN_MOD_NAME,
                        )
                    except DynDOLODValidationError as e:
                        errors.append(f"Failed to package TexGen output: {e}")
                        logger.error("Error empaquetando TexGen: %s", e)
                elif not texgen_result.success:
                    errors.extend(texgen_result.errors)

            except DynDOLODExecutionError as e:
                errors.append(f"TexGen execution failed: {e}")
                logger.error("Error ejecutando TexGen: %s", e)
                texgen_result = ToolExecutionResult(
                    success=False,
                    tool_name="TexGen",
                    return_code=e.return_code or -1,
                    stdout="",
                    stderr=e.stderr or "",
                    errors=[str(e)],
                )

        # Paso 2: Ejecutar DynDOLOD
        # Nota: DynDOLOD puede ejecutarse sin TexGen si ya existe TexGen_Output
        try:
            dyndolod_result = await self.run_dyndolod(
                preset=preset,
                extra_args=dyndolod_args,
            )

            if dyndolod_result.success and dyndolod_result.output_path:
                # Empaquetar DynDOLOD Output
                try:
                    dyndolod_mod_path = await self._package_output_as_mod(
                        dyndolod_result.output_path,
                        self.DYNDOLLOD_MOD_NAME,
                    )
                except DynDOLODValidationError as e:
                    errors.append(f"Failed to package DynDOLOD output: {e}")
                    logger.error("Error empaquetando DynDOLOD: %s", e)
            elif not dyndolod_result.success:
                errors.extend(dyndolod_result.errors)

        except DynDOLODExecutionError as e:
            errors.append(f"DynDOLOD execution failed: {e}")
            logger.error("Error ejecutando DynDOLOD: %s", e)
            dyndolod_result = ToolExecutionResult(
                success=False,
                tool_name="DynDOLOD",
                return_code=e.return_code or -1,
                stdout="",
                stderr=e.stderr or "",
                errors=[str(e)],
            )

        # Determinar éxito general
        success = (
            dyndolod_result is not None
            and dyndolod_result.success
            and dyndolod_mod_path is not None
            and (
                not run_texgen or (texgen_result is not None and texgen_result.success)
            )
        )

        result = DynDOLODPipelineResult(
            success=success,
            texgen_result=texgen_result,
            dyndolod_result=dyndolod_result,
            texgen_mod_path=texgen_mod_path,
            dyndolod_mod_path=dyndolod_mod_path,
            errors=errors,
        )

        if result.success:
            logger.info(
                "Pipeline DynDOLOD completado exitosamente: TexGen=%s, DynDOLOD=%s",
                texgen_mod_path,
                dyndolod_mod_path,
            )
        else:
            logger.error(
                "Pipeline DynDOLOD falló: %s",
                "; ".join(errors) if errors else "Unknown error",
            )

        return result

    # =========================================================================
    # Métodos Auxiliares
    # =========================================================================

    def _build_texgen_args(self, extra_args: list[str] | None) -> list[str]:
        """
        Construye argumentos CLI para TexGen.

        Args:
            extra_args: Argumentos adicionales.

        Returns:
            Lista de argumentos para create_subprocess_exec.
        """
        args: list[str] = [
            "-game",
            "TES5VR" if "VR" in str(self._config.game_path) else "SSE",
            "-t",  # Modo headless
        ]

        if extra_args:
            args.extend(extra_args)

        logger.debug("TexGen CLI args: %s", " ".join(args))
        return args

    def _build_dyndolod_args(
        self,
        preset: str,
        extra_args: list[str] | None,
    ) -> list[str]:
        """
        Construye argumentos CLI para DynDOLOD.

        Args:
            preset: Nivel de calidad (Low, Medium, High).
            extra_args: Argumentos adicionales.

        Returns:
            Lista de argumentos para create_subprocess_exec.
        """
        args: list[str] = [
            "-game",
            "TES5VR" if "VR" in str(self._config.game_path) else "SSE",
            "-p",
            preset,
            "-t",  # Modo headless
            "--expert",  # Prevenir bloqueos UI
        ]

        if extra_args:
            args.extend(extra_args)

        logger.debug("DynDOLOD CLI args: %s", " ".join(args))
        return args

    def _parse_output(self, stdout: str, stderr: str) -> tuple[list[str], list[str]]:
        """
        Extrae errores y warnings del output usando regex.

        Args:
            stdout: Salida estándar del proceso.
            stderr: Salida de error del proceso.

        Returns:
            Tupla con (errors, warnings).
        """
        combined_output = stdout + "\n" + stderr

        # Detectar errores
        errors: list[str] = []
        for match in self._ERROR_PATTERN.finditer(combined_output):
            error_msg = match.group(1).strip()
            if error_msg and error_msg not in errors:
                errors.append(error_msg)

        # Detectar warnings
        warnings: list[str] = []
        for match in self._WARNING_PATTERN.finditer(combined_output):
            warning_msg = match.group(1).strip()
            if warning_msg and warning_msg not in warnings:
                warnings.append(warning_msg)

        logger.debug(
            "Parse output: errors=%d, warnings=%d",
            len(errors),
            len(warnings),
        )

        return errors, warnings

    def _generate_meta_ini(self, mod_path: pathlib.Path, mod_name: str) -> None:
        """
        Genera el archivo meta.ini para MO2.

        Args:
            mod_path: Path al directorio del mod.
            mod_name: Nombre del mod.
        """
        meta_ini_path = mod_path / "meta.ini"

        config = configparser.ConfigParser()
        config["General"] = {
            "modid": "0",
            "version": "1.0.0",
            "name": mod_name,
            "comments": "Generated by Sky Claw",
        }

        try:
            with open(meta_ini_path, "w", encoding="utf-8") as f:
                config.write(f)
            logger.debug("meta.ini generado: %s", meta_ini_path)
        except OSError as e:
            logger.error("Error generando meta.ini: %s", e)
            raise

    def _find_texgen_output(self) -> pathlib.Path | None:
        """
        Busca el directorio de salida de TexGen.

        Returns:
            Path al directorio de salida o None si no se encuentra.
        """
        # Ubicaciones comunes de TexGen_Output
        search_paths = [
            self._config.mo2_path / self.TEXGEN_OUTPUT_NAME,
            self._config.dyndolod_exe.parent / self.TEXGEN_OUTPUT_NAME,
            pathlib.Path.cwd() / self.TEXGEN_OUTPUT_NAME,
        ]

        for path in search_paths:
            if path.exists() and any(path.iterdir()):
                logger.debug("TexGen output encontrado: %s", path)
                return path

        logger.warning("No se encontró directorio de salida de TexGen")
        return None

    def _find_dyndolod_output(self) -> pathlib.Path | None:
        """
        Busca el directorio de salida de DynDOLOD.

        Returns:
            Path al directorio de salida o None si no se encuentra.
        """
        # Ubicaciones comunes de DynDOLOD_Output
        search_paths = [
            self._config.mo2_path / self.DYNDOLLOD_OUTPUT_NAME,
            self._config.dyndolod_exe.parent / self.DYNDOLLOD_OUTPUT_NAME,
            pathlib.Path.cwd() / self.DYNDOLLOD_OUTPUT_NAME,
        ]

        for path in search_paths:
            if path.exists() and any(path.iterdir()):
                logger.debug("DynDOLOD output encontrado: %s", path)
                return path

        logger.warning("No se encontró directorio de salida de DynDOLOD")
        return None

    async def validate_dyndolod_output(self, output_path: pathlib.Path) -> bool:
        """
        Valida que la salida de DynDOLOD sea válida.

        Realiza validaciones básicas de integridad:
        - El directorio existe y tiene contenido
        - Contiene DynDOLOD.esp
        - Contiene al menos algunos assets esperados

        Args:
            output_path: Path al directorio de salida.

        Returns:
            True si la salida parece válida, False en caso contrario.
        """
        logger.debug("Validando DynDOLOD output: %s", output_path)

        try:
            # Verificar existencia
            if not output_path.exists():
                logger.warning("Directorio de salida no existe: %s", output_path)
                return False

            # Verificar que tiene contenido
            items = list(output_path.iterdir())
            if not items:
                logger.warning("Directorio de salida vacío: %s", output_path)
                return False

            # Buscar DynDOLOD.esp
            esp_files = list(output_path.glob("*.esp"))
            if not esp_files:
                logger.warning("No se encontró archivo ESP en: %s", output_path)
                return False

            # Verificar que al menos DynDOLOD.esp existe
            dyndolod_esp = output_path / "DynDOLOD.esp"
            if not dyndolod_esp.exists():
                logger.warning(
                    "DynDOLOD.esp no encontrado, encontrado: %s",
                    [e.name for e in esp_files],
                )

            logger.info(
                "DynDOLOD output validado: %s (%d items)",
                output_path,
                len(items),
            )
            return True

        except OSError as e:
            logger.error("Error de I/O validando output %s: %s", output_path, e)
            return False
        except Exception as e:
            logger.exception("Error inesperado validando output %s: %s", output_path, e)
            return False


__all__ = [
    "DynDOLODRunner",
    "DynDOLODConfig",
    "DynDOLODPipelineResult",
    "DynDOLODExecutionError",
    "DynDOLODTimeoutError",
    "DynDOLODNotFoundError",
]
