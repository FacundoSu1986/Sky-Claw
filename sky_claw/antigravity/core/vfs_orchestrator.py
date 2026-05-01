"""
VFS Orchestrator Module - Sky-Claw Fase 3

Motor de orquestación para ejecutar herramientas de modding (DynDOLOD, xEdit)
a través del Virtual File System (VFS) de Mod Organizer 2.

Este módulo proporciona ejecución asíncrona de herramientas externas con
manejo robusto de timeouts y logging estructurado.
"""

import asyncio
import logging
from typing import Final

# Configuración del logger del módulo
logger: Final[logging.Logger] = logging.getLogger(__name__)


class VFSTimeoutError(Exception):
    """
    Excepción personalizada para timeouts del VFS.

    Se lanza cuando una operación de herramienta de modding excede
    el límite de tiempo configurado en el orquestador VFS.

    Attributes:
        message: Descripción detallada del timeout ocurrido.
        timeout_seconds: Duración del timeout en segundos.
        tool_path: Ruta de la herramienta que causó el timeout (opcional).
    """

    def __init__(
        self,
        message: str,
        timeout_seconds: int | None = None,
        tool_path: str | None = None,
    ) -> None:
        """
        Inicializa la excepción VFSTimeoutError.

        Args:
            message: Descripción del error de timeout.
            timeout_seconds: Duración del timeout configurado.
            tool_path: Ruta de la herramienta que causó el timeout.
        """
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.tool_path = tool_path

    def __str__(self) -> str:
        """Representación en string de la excepción."""
        base_msg = super().__str__()
        if self.tool_path and self.timeout_seconds:
            return f"{base_msg} (Herramienta: {self.tool_path}, Timeout: {self.timeout_seconds}s)"
        return base_msg


class VFSOrchestrator:
    """
    Motor de orquestación para ejecutar herramientas de modding
    a través del Virtual File System de Mod Organizer 2.

    Esta clase encapsula la lógica de ejecución de herramientas externas
    (como DynDOLOD, xEdit, LOOT) mediante el mecanismo VFS de MO2,
    permitiendo que estas herramientas operen sobre el sistema de archivos
    virtualizado sin modificar los archivos originales del juego.

    Attributes:
        mo2_path: Ruta absoluta al ejecutable de Mod Organizer 2.
        timeout_seconds: Límite de tiempo por defecto para operaciones.

    Example:
        >>> orchestrator = VFSOrchestrator(
        ...     mo2_path="C:/MO2/ModOrganizer.exe",
        ...     timeout_seconds=1800
        ... )
        >>> exit_code, stdout, stderr = await orchestrator.run_tool(
        ...     tool_path="C:/MO2/DynDOLOD/DynDOLODx64.exe",
        ...     args=["-SSE"]
        ... )
    """

    # Constantes de configuración
    _DEFAULT_TIMEOUT: Final[int] = 3600  # 1 hora por defecto
    _MO2_VFS_FLAG: Final[str] = "-m"

    def __init__(self, mo2_path: str, timeout_seconds: int = _DEFAULT_TIMEOUT) -> None:
        """
        Inicializador del orquestador VFS.

        Configura el orquestador con la ruta a Mod Organizer 2 y el
        timeout por defecto para todas las operaciones de herramientas.

        Args:
            mo2_path: Ruta absoluta al ejecutable de Mod Organizer 2.
                Debe ser una ruta válida a ModOrganizer.exe.
            timeout_seconds: Límite de tiempo por defecto para operaciones
                de herramientas. Valor por defecto: 3600 segundos (1 hora).

        Raises:
            ValueError: Si mo2_path está vacío o es None.
            ValueError: Si timeout_seconds es menor o igual a cero.

        Note:
            El timeout debe configurarse adecuadamente según la complejidad
            de las operaciones. DynDOLOD puede requerir varios minutos
            para mundos de juego extensos.
        """
        if not mo2_path or not mo2_path.strip():
            raise ValueError("La ruta a Mod Organizer 2 no puede estar vacía")

        if timeout_seconds <= 0:
            raise ValueError(f"El timeout debe ser mayor a cero, se recibió: {timeout_seconds}")

        self._mo2_path: str = mo2_path.strip()
        self._timeout_seconds: int = timeout_seconds

        logger.info(
            "VFSOrchestrator inicializado - MO2: %s, Timeout: %ds",
            self._mo2_path,
            self._timeout_seconds,
        )

    @property
    def mo2_path(self) -> str:
        """Retorna la ruta al ejecutable de Mod Organizer 2."""
        return self._mo2_path

    @property
    def timeout_seconds(self) -> int:
        """Retorna el timeout configurado en segundos."""
        return self._timeout_seconds

    async def run_tool(self, tool_path: str, args: list[str]) -> tuple[int, str, str]:
        """
        Ejecuta una herramienta de modding a través del VFS de MO2.

        Lanza la herramienta especificada utilizando el mecanismo VFS
        de Mod Organizer 2, permitiendo que la herramienta acceda al
        sistema de archivos virtualizado.

        El comando ejecutado sigue el formato:
        `[mo2_path, "-m", tool_path] + args`

        Args:
            tool_path: Ruta al ejecutable de la herramienta de modding.
                Ejemplo: "C:/MO2/DynDOLOD/DynDOLODx64.exe"
            args: Lista de argumentos de línea de comandos para la herramienta.
                Ejemplo: ["-SSE", "-Clean"]

        Returns:
            Tupla con (código_salida, stdout, stderr):
                - código_salida (int): Código de retorno del proceso.
                    0 indica éxito, valores distintos indican error.
                - stdout (str): Salida estándar capturada del proceso.
                - stderr (str): Salida de error capturada del proceso.

        Raises:
            VFSTimeoutError: Si la operación excede el timeout configurado.
                El proceso es terminado antes de lanzar la excepción.
            ValueError: Si tool_path está vacío o es None.
            OSError: Si ocurre un error al crear el subproceso.

        Example:
            >>> exit_code, stdout, stderr = await orchestrator.run_tool(
            ...     "C:/MO2/Edit Scripts/xEdit.exe",
            ...     ["-IKnowWhatImDoing", "-Script"]
            ... )
            >>> if exit_code == 0:
            ...     print("Operación exitosa")
        """
        # Validación de argumentos
        if not tool_path or not tool_path.strip():
            raise ValueError("La ruta de la herramienta no puede estar vacía")

        tool_path = tool_path.strip()

        # Construcción del comando VFS de MO2
        command: list[str] = [self._mo2_path, self._MO2_VFS_FLAG, tool_path, *args]

        logger.info(
            "Iniciando ejecución VFS - Herramienta: %s, Argumentos: %s",
            tool_path,
            " ".join(args) if args else "(sin argumentos)",
        )
        logger.debug("Comando completo: %s", " ".join(command))

        try:
            # Creación del subproceso asíncrono
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            logger.debug("Proceso iniciado con PID: %d", process.pid)

            # Espera con timeout
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout_seconds
                )
            except TimeoutError:
                logger.critical(
                    "Timeout excedido para %s después de %d segundos",
                    tool_path,
                    self._timeout_seconds,
                )

                # Terminación segura del proceso
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    logger.warning(
                        "Proceso no terminó graciosamente, forzando kill - PID: %d",
                        process.pid,
                    )
                    process.kill()
                    await process.wait()

                raise VFSTimeoutError(
                    f"Operación excedió {self._timeout_seconds} segundos",
                    timeout_seconds=self._timeout_seconds,
                    tool_path=tool_path,
                ) from None

            # Decodificación de salidas
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            # Obtención del código de salida
            exit_code: int = process.returncode if process.returncode is not None else -1

            logger.info(
                "Ejecución completada - Herramienta: %s, Código de salida: %d",
                tool_path,
                exit_code,
            )

            if stderr:
                logger.debug("Stderr de %s: %s", tool_path, stderr[:500])

            return (exit_code, stdout, stderr)

        except VFSTimeoutError:
            # Re-lanzar VFSTimeoutError sin modificar
            raise
        except OSError as e:
            logger.error("Error de sistema al ejecutar %s: %s", tool_path, str(e))
            raise
        except Exception as e:
            logger.exception("Error inesperado durante ejecución de %s: %s", tool_path, str(e))
            raise

    def __repr__(self) -> str:
        """Representación formal del orquestador."""
        return f"VFSOrchestrator(mo2_path='{self._mo2_path}', timeout_seconds={self._timeout_seconds})"

    def __str__(self) -> str:
        """Representación en string del orquestador."""
        return f"VFSOrchestrator(MO2: {self._mo2_path}, Timeout: {self._timeout_seconds}s)"
