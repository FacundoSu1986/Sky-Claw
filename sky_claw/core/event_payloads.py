"""Payloads inmutables y validados para eventos del CoreEventBus.

Todos los payloads heredan de ``pydantic.BaseModel`` con
``ConfigDict(frozen=True, strict=True)`` para garantizar serialización
y validación de esquemas al cruzar los límites del bus.

Parte del Sprint 1.5: Strangler Fig — desacoplamiento de ``supervisor.py``.
Fase 6: añadidos payloads ops.* para el puente GUI del Operations Hub.
"""

from __future__ import annotations

import time
from typing import Literal

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


class XEditPatchStartedPayload(BaseModel):
    """Payload inmutable para el evento ``xedit.patch.started``.

    Publicado por :class:`XEditPipelineService` al iniciar la ejecución
    de un parche xEdit transaccional.

    Attributes:
        target_plugin: Nombre del plugin objetivo del parcheo.
        total_conflicts: Total de conflictos a resolver.
        started_at: Timestamp de inicio (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    target_plugin: str
    total_conflicts: int
    started_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class XEditPatchCompletedPayload(BaseModel):
    """Payload inmutable para el evento ``xedit.patch.completed``.

    Publicado por :class:`XEditPipelineService` al finalizar la ejecución
    de un parche xEdit (éxito o fallo).

    Attributes:
        target_plugin: Nombre del plugin objetivo del parcheo.
        total_conflicts: Total de conflictos solicitados.
        success: Si la ejecución fue exitosa.
        records_patched: Número de records procesados.
        conflicts_resolved: Número de conflictos resueltos.
        duration_seconds: Duración total de la ejecución.
        rolled_back: Si se ejecutó rollback automático.
        completed_at: Timestamp de finalización (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    target_plugin: str
    total_conflicts: int
    success: bool
    records_patched: int
    conflicts_resolved: int
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


# ---------------------------------------------------------------------------
# Payloads ops.* — tópicos GUI-facing del Operations Hub (Fase 6)
# ---------------------------------------------------------------------------


class OpsTelemetryPayload(BaseModel):
    """Payload para el tópico ``ops.telemetry``.

    Emitido por :class:`TelemetryDaemon` a 1 Hz con las métricas de sistema
    que el Operations Hub necesita para pintar los paneles de Telemetría.

    Attributes:
        cpu:          Uso de CPU del proceso (0–100, float).
        ram_mb:       RAM del proceso en megabytes.
        ram_percent:  RAM del sistema global en porcentaje (0–100).
        uptime_s:     Segundos de uptime del demonio de telemetría.
        ts:           Epoch en segundos (autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    cpu: float
    ram_mb: float
    ram_percent: float
    uptime_s: float
    ts: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class OpsProcessChangePayload(BaseModel):
    """Payload para el tópico ``ops.process_change``.

    Emitido por el ToolDispatcher (y orquestadores de pipeline) cada vez que
    una herramienta inicia, termina o falla.  El Operations Hub lo usa para
    actualizar el panel de Arsenal y el contador de procesos activos.

    Attributes:
        process_id:       Identificador único del proceso (UUID o hash).
        tool_name:        Nombre humano de la herramienta (ej. "DynDOLOD").
        state:            Estado de la transición: ``started`` | ``completed``
                          | ``error``.
        exit_code:        Código de salida del proceso (``None`` si en curso).
        error_message:    Mensaje de error resumido (solo para ``state=error``).
        duration_seconds: Duración en segundos (solo para terminal states).
        ts:               Epoch en segundos (autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    process_id: str
    tool_name: str
    state: Literal["started", "completed", "error"]
    exit_code: int | None = None
    error_message: str | None = None
    duration_seconds: float | None = None
    ts: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class OpsSystemLogPayload(BaseModel):
    """Payload para el tópico ``ops.system_log``.

    Entrada de log estructurado destinada al Orbe de Visión de la GUI.
    Cualquier capa del backend puede emitir este tópico para notificar al
    operador (ej. advertencia de RAM alta, error de red, conflicto detectado).

    Attributes:
        level:   Nivel de severidad: ``info`` | ``warning`` | ``error``
                 | ``critical``.
        message: Mensaje legible por el humano.
        source:  Nombre del componente emisor.
        ts:      Epoch en segundos (autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    level: Literal["info", "warning", "error", "critical"]
    message: str
    source: str
    ts: float = Field(default_factory=time.time)

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
