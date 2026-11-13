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
        preset: Nivel de calidad del preset (Low, Medium, High) — metadato:
            lo elige el humano en el asistente de la herramienta.
        run_texgen: Si se ejecutará TexGen antes de DynDOLOD.
        assisted: ``True`` cuando la corrida es ASISTIDA (el operador opera la
            ventana del asistente en la PC); ``False`` cuando es DESATENDIDA
            (#661: Sky-Claw opera la GUI por UI Automation tras una aprobación
            remota de la configuración).
        operator_instructions: Qué debe hacer (o no hacer) el operador.
        interaction_mode: ``"asistido"`` / ``"desatendido"`` — el modo explícito
            de la corrida, para que un consumidor no lo infiera del bool.
        started_at: Timestamp de inicio (epoch float, autogenerado).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    preset: str
    run_texgen: bool
    assisted: bool = True
    operator_instructions: str = (
        "La etapa 9 es asistida: seleccioná el preset y los worldspaces en la "
        "ventana de TexGen/DynDOLOD y completá el asistente; el pipeline espera."
    )
    interaction_mode: str = "asistido"
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
        log_paths: Rutas de los logs de las herramientas que ESCRIBIERON en esta
            corrida (``TexGen_SSE_log.txt``/``DynDOLOD_SSE_log.txt``), sólo en un
            fallo de herramienta; ``()`` en éxito, cancelación o fallos previos al
            spawn. Existe para que un consumidor (el notificador de operador)
            pueda adjuntar el log que explica el fallo: sin esto el evento sólo
            llevaba el texto de las líneas terminales. Es un hecho de filesystem
            leído por el servicio, nunca un dato del tool.
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
    log_paths: tuple[str, ...] = ()
    completed_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()


class DynDOLODPipelineProgressPayload(BaseModel):
    """Payload inmutable para el evento ``pipeline.dyndolod.progress`` (H-11).

    Publicado por :class:`DynDOLODPipelineService` en cada latido de TexGen/DynDOLOD, entre ``started`` y
    ``completed``. La etapa 9 dura horas: sin esto no había forma de distinguir «generando» de «colgado».
    Son HECHOS medidos, no un veredicto: el umbral de «sin actividad» lo juzga quien consume el evento.

    Attributes:
        tool_name: Fase vista desde el runner (``TexGen``/``DynDOLOD``).
        elapsed_seconds: Segundos desde que arrancó el proceso de esa herramienta.
        log_path: Log de la herramienta que se midió; ``None`` si todavía no existe o no se puede leer. Bajo USVFS
            el archivo puede aparecer en otra capa y quedar siempre en ``None`` (no medido en un rig).
        log_bytes: Tamaño actual de ese log; ``None`` si no hay log.
        log_idle_seconds: Segundos desde la última modificación del log (nunca negativo); ``None`` si no hay log.
            Un valor que crece mientras ``elapsed_seconds`` crece es la señal de «no escribe hace rato».
        sampled_at: Timestamp de la muestra (epoch float, autogenerado). Permite a un consumidor saber cuán vieja
            es la última muestra: un latido que dejó de llegar también es información.

    Bases de tiempo (límite conocido): ``elapsed_seconds`` es de reloj MONOTÓNICO (inmune a ajustes del reloj del
    sistema); ``log_idle_seconds`` y ``sampled_at`` son de reloj de PARED, porque la única fecha que el sistema de
    archivos da del log es su ``mtime``, que es de pared, y una edad de muestra que se publica y se compara entre
    procesos también lo es. Un salto del reloj durante la corrida (NTP, cambio manual) corre esas dos edades por el
    tamaño del salto sin tocar a ``elapsed_seconds``. No se compara una con la otra para decidir «colgado»: el
    criterio robusto es que ``log_bytes`` CREZCA entre muestras, que no depende de ningún reloj.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    tool_name: str
    elapsed_seconds: float
    log_path: str | None = None
    log_bytes: int | None = None
    log_idle_seconds: float | None = None
    sampled_at: float = Field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, object]:
        """Serialización compatible con el sistema de logging estructurado."""
        return self.model_dump()
