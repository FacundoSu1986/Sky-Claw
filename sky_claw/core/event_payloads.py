"""Payloads inmutables y validados para eventos del CoreEventBus.

Todos los payloads heredan de ``pydantic.BaseModel`` con
``ConfigDict(frozen=True, strict=True)`` para garantizar serialización
y validación de esquemas al cruzar los límites del bus.

Parte del Sprint 1.5: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field


class ModlistChangedPayload(BaseModel):
    """Payload inmutable para el evento ``system.modlist.changed``.

    Publicado por :class:`WatcherDaemon` cuando detecta una modificación
    externa en el ``modlist.txt`` de MO2.

    Attributes:
        profile_name: Nombre del perfil MO2 donde se detectó el cambio.
        modlist_path: Ruta absoluta al ``modlist.txt`` monitoreado.
        previous_mtime: ``st_mtime`` anterior del archivo (epoch float).
        current_mtime: ``st_mtime`` actual del archivo (epoch float).
        detected_at: Timestamp de detección (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    profile_name: str
    modlist_path: str
    previous_mtime: float
    current_mtime: float
    detected_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, str | float]:
        """Serialización compatible con el sistema de logging estructurado.

        Returns:
            Diccionario plano con todos los campos del payload.
        """
        return self.model_dump()


class SynthesisPipelineStartedPayload(BaseModel):
    """Payload inmutable para el evento ``synthesis.pipeline.started``.

    Publicado por :class:`SynthesisPipelineService` al iniciar la ejecución
    del pipeline de Synthesis.

    Attributes:
        patcher_ids: IDs de los patchers a ejecutar.
        target_esp: Ruta absoluta al ESP objetivo.
        snapshot_enabled: Si se habilitó snapshot pre-ejecución.
        started_at: Timestamp de inicio (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    patcher_ids: tuple[str, ...]
    target_esp: str
    snapshot_enabled: bool
    started_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class SynthesisPipelineCompletedPayload(BaseModel):
    """Payload inmutable para el evento ``synthesis.pipeline.completed``.

    Publicado por :class:`SynthesisPipelineService` al finalizar la ejecución
    del pipeline de Synthesis (éxito o fallo).

    Attributes:
        patcher_ids: IDs de los patchers solicitados.
        target_esp: Ruta absoluta al ESP objetivo.
        success: Si la ejecución fue exitosa.
        patchers_executed: IDs de los patchers que se ejecutaron.
        errors: Errores detectados durante la ejecución.
        duration_seconds: Duración total de la ejecución.
        rolled_back: Si se ejecutó rollback automático.
        completed_at: Timestamp de finalización (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    patcher_ids: tuple[str, ...]
    target_esp: str
    success: bool
    patchers_executed: tuple[str, ...]
    errors: tuple[str, ...]
    duration_seconds: float
    rolled_back: bool
    completed_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class DynDOLODPipelineStartedPayload(BaseModel):
    """Payload inmutable para el evento ``pipeline.dyndolod.started``.

    Publicado por :class:`DynDOLODPipelineService` al iniciar la ejecución
    del pipeline de generación de LODs (TexGen + DynDOLOD).

    Attributes:
        preset: Nivel de calidad del preset (Low, Medium, High).
        run_texgen: Si se ejecutará TexGen antes de DynDOLOD.
        started_at: Timestamp de inicio (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    preset: str
    run_texgen: bool
    started_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class DynDOLODPipelineCompletedPayload(BaseModel):
    """Payload inmutable para el evento ``pipeline.dyndolod.completed``.

    Publicado por :class:`DynDOLODPipelineService` al finalizar la ejecución
    del pipeline de generación de LODs (éxito o fallo).

    Attributes:
        preset: Nivel de calidad del preset (Low, Medium, High).
        run_texgen: Si se ejecutó TexGen antes de DynDOLOD.
        success: Si la ejecución fue exitosa.
        texgen_success: Si TexGen se ejecutó correctamente.
        dyndolod_success: Si DynDOLOD se ejecutó correctamente.
        errors: Errores detectados durante la ejecución.
        duration_seconds: Duración total de la ejecución.
        rolled_back: Si se ejecutó rollback automático.
        completed_at: Timestamp de finalización (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    preset: str
    run_texgen: bool
    success: bool
    texgen_success: bool
    dyndolod_success: bool
    errors: tuple[str, ...]
    duration_seconds: float
    rolled_back: bool
    completed_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()
