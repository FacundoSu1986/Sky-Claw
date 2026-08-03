"""Wrye Bash Runner for Sky-Claw.

Implements M-01 Wrye Bash Runner specifications.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)

#: Nombre canónico del plugin que genera Wrye Bash. Única fuente para el
#: runner y sus callers (supervisor / tool del agente), que lo necesitan para
#: snapshotearlo antes de regenerarlo. Debe coincidir con
#: ``DelegateToBashedPatch.BASHED_PATCH_NAME`` (anclado por test).
BASHED_PATCH_NAME = "Bashed Patch, 0.esp"


class WryeBashExecutionError(Exception):
    pass


class WryeBashHeadlessUnsupportedError(WryeBashExecutionError):
    """Wrye Bash no expone ninguna forma de construir el Bashed Patch sin GUI.

    Verificado contra ``wrye-bash/wrye-bash``, ``Mopy/bash/barg.py``: el parser
    declara ``-b``/``--backup`` (hace un backup de la configuración de Wrye
    Bash a un archivo antes de que arranque la app), ``-o``/``--oblivionPath``
    (directorio del juego) y ``--no-uac``. **No hay ningún argumento que
    reconstruya el parche.**

    Este runner pasaba ``-b "Bashed Patch, 0.esp"`` creyendo que construía: en
    realidad disparaba el backup de settings y devolvía ``success=True``. Reportar
    éxito sobre un no-op es peor que fallar, porque las etapas siguientes del DAG
    (Synthesis en la 7, DynDOLOD en la 9) consumen un parche que nunca se
    regeneró.

    Deriva de :class:`WryeBashExecutionError` para que los callers que ya capturan
    la base la traduzcan al contrato ``success=False``/``message`` sin cambios.

    Decisión pendiente del equipo: modelar la etapa 6 como *etapa asistida*
    (Sky-Claw hace preflight/sandbox y lanza vía MO2, el humano completa el paso
    GUI, Sky-Claw verifica el artefacto y promueve por HITL) o retirarla del
    dispatcher. Automatizarla por RPA de coordenadas queda descartado: no es
    determinista, no corre en el CI headless y no puede participar del contrato
    de journal/rollback.
    """

    def __init__(self) -> None:
        super().__init__(
            "Wrye Bash no soporta construir el Bashed Patch en modo headless: su CLI "
            "solo expone --backup, --oblivionPath y --no-uac (Mopy/bash/barg.py). "
            "La etapa 6 requiere intervención humana en la GUI."
        )


class WryeBashTimeoutError(WryeBashExecutionError):
    """Elevada cuando Wrye Bash excede su timeout (U-10).

    Dedicada (en vez de un ``WryeBashResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de salir
    limpio: es el prerequisito del rollback transaccional de salida (U-04). Deriva
    de :class:`WryeBashExecutionError`, así que los callers que ya capturan la base
    la traducen al contrato ``success=False`` sin cambios.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Wrye Bash excedió el timeout de {timeout_seconds:g}s generando el Bashed Patch")


@dataclass(frozen=True, slots=True)
class WryeBashConfig:
    wrye_bash_path: pathlib.Path
    game_path: pathlib.Path
    mo2_path: pathlib.Path
    timeout_seconds: float = 600.0


@dataclass
class WryeBashResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class WryeBashRunner:
    """Asynchronous runner for Wrye Bash (bash.py) for Bashed Patch generation."""

    def __init__(self, config: WryeBashConfig):
        self.config = config

    async def generate_bashed_patch(self) -> NoReturn:
        """Falla cerrado: Wrye Bash no puede construir el parche sin GUI.

        Ver :class:`WryeBashHeadlessUnsupportedError` para la verificación contra
        el parser oficial y la decisión de arquitectura pendiente.

        No se lanza el proceso a propósito. La implementación anterior ejecutaba
        ``bash.py -b "Bashed Patch, 0.esp"``, que en el parser real de Wrye Bash
        significa *backup de settings* con ese nombre como archivo destino — un
        efecto lateral silencioso sobre el que además reportaba éxito.
        """
        logger.error(
            "[M-01] Wrye Bash no expone build headless del Bashed Patch; "
            "la etapa 6 no puede completarse de forma desatendida."
        )
        raise WryeBashHeadlessUnsupportedError
