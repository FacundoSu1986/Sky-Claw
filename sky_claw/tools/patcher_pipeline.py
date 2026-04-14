"""
Pipeline de Parcheo para Mutagen Synthesis.

Este módulo define la configuración y gestión del pipeline de patchers
que se ejecutan con Synthesis CLI.

Reference:
    - Synthesis CLI: https://github.com/Mutagen-Modding/Synthesis
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib

    from sky_claw.security.path_validator import PathValidator

from sky_claw.security.path_validator import PathViolation

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class PatcherPipelineError(Exception):
    """Error en la configuración del pipeline de patchers."""

    pass


class PatcherNotFoundError(PatcherPipelineError):
    """Error cuando un patcher no se encuentra en el pipeline."""

    def __init__(self, patcher_id: str) -> None:
        super().__init__(f"Patcher not found in pipeline: {patcher_id}")
        self.patcher_id = patcher_id


class PatcherConfigError(PatcherPipelineError):
    """Error en la configuración de un patcher."""

    def __init__(self, patcher_id: str, reason: str) -> None:
        super().__init__(f"Invalid config for patcher '{patcher_id}': {reason}")
        self.patcher_id = patcher_id
        self.reason = reason


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class PatcherDefinition:
    """
    Definición de un patcher de Mutagen Synthesis.

    Attributes:
        patcher_id: ID único del patcher (ej: "LeveledListsPatcher").
        enabled: Si el patcher está habilitado para ejecución.
        order: Orden de ejecución (menor = antes).
        config: Configuración específica del patcher (key-value).
    """

    patcher_id: str
    enabled: bool = True
    order: int = 0
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Valida que el patcher_id no esté vacío."""
        if not self.patcher_id or not self.patcher_id.strip():
            raise PatcherConfigError("", "patcher_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        """
        Serializa el patcher a diccionario.

        Returns:
            Diccionario con los datos del patcher.
        """
        return {
            "patcher_id": self.patcher_id,
            "enabled": self.enabled,
            "order": self.order,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatcherDefinition:
        """
        Crea un PatcherDefinition desde un diccionario.

        Args:
            data: Diccionario con los datos del patcher.

        Returns:
            Nueva instancia de PatcherDefinition.

        Raises:
            PatcherConfigError: Si faltan campos requeridos.
        """
        if "patcher_id" not in data:
            raise PatcherConfigError("", "missing required field: patcher_id")

        return cls(
            patcher_id=data["patcher_id"],
            enabled=data.get("enabled", True),
            order=data.get("order", 0),
            config=data.get("config", {}),
        )


# =============================================================================
# PATCHER PIPELINE
# =============================================================================


class PatcherPipeline:
    """
    Gestiona el pipeline de patchers para Synthesis.

    El pipeline mantiene una lista ordenada de patchers con sus configuraciones,
    y puede serializarse para consumo de Synthesis CLI.

    Usage:
        # Crear pipeline desde cero
        pipeline = PatcherPipeline()
        pipeline.add_patcher(PatcherDefinition(
            patcher_id="LeveledListsPatcher",
            enabled=True,
            order=1,
        ))

        # Guardar configuración
        pipeline.to_json(Path("synthesis_pipeline.json"))

        # Cargar desde archivo
        pipeline = PatcherPipeline.from_json(Path("synthesis_pipeline.json"))

        # Obtener patchers habilitados para Synthesis
        enabled = pipeline.get_enabled_patchers()
    """

    def __init__(
        self,
        pipeline_config_path: pathlib.Path | None = None,
        path_validator: PathValidator | None = None,
    ) -> None:
        """
        Inicializa el pipeline de patchers.

        Args:
            pipeline_config_path: Path opcional al archivo de configuración.
            path_validator: Optional PathValidator for sandbox enforcement.
        """
        self._patchers: dict[str, PatcherDefinition] = {}
        self._config_path = pipeline_config_path
        self._path_validator = path_validator

        # Cargar configuración inicial si existe
        if pipeline_config_path and pipeline_config_path.exists():
            try:
                self._load_from_path(pipeline_config_path)
                logger.info(
                    "Pipeline cargado desde %s: %d patchers",
                    pipeline_config_path,
                    len(self._patchers),
                )
            except Exception as e:
                logger.warning(
                    "Error cargando pipeline desde %s: %s. Iniciando vacío.",
                    pipeline_config_path,
                    e,
                )

    def _load_from_path(self, path: pathlib.Path) -> None:
        """
        Carga el pipeline desde un archivo JSON.

        Args:
            path: Path al archivo JSON.

        Raises:
            PatcherPipelineError: Si el archivo está corrupto.
        """
        # S2-FIX: Validate path before opening.
        if self._path_validator is not None:
            try:
                self._path_validator.validate(path, strict_symlink=False)
            except PathViolation:
                logger.error("Path traversal blocked for pipeline load: %s", path)
                raise

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise PatcherPipelineError("Pipeline config must be a JSON object")

        patchers_data = data.get("patchers", [])
        if not isinstance(patchers_data, list):
            raise PatcherPipelineError("'patchers' must be a list")

        for patcher_data in patchers_data:
            patcher = PatcherDefinition.from_dict(patcher_data)
            self._patchers[patcher.patcher_id] = patcher

    def add_patcher(self, patcher: PatcherDefinition) -> None:
        """
        Añade un patcher al pipeline.

        Si ya existe un patcher con el mismo ID, se reemplaza.

        Args:
            patcher: Definición del patcher a añadir.
        """
        existing = self._patchers.get(patcher.patcher_id)
        self._patchers[patcher.patcher_id] = patcher

        if existing:
            logger.debug(
                "Patcher reemplazado: %s (order %d -> %d)",
                patcher.patcher_id,
                existing.order,
                patcher.order,
            )
        else:
            logger.debug(
                "Patcher añadido: %s (order %d, enabled=%s)",
                patcher.patcher_id,
                patcher.order,
                patcher.enabled,
            )

    def remove_patcher(self, patcher_id: str) -> bool:
        """
        Elimina un patcher del pipeline.

        Args:
            patcher_id: ID del patcher a eliminar.

        Returns:
            True si se eliminó, False si no existía.
        """
        if patcher_id in self._patchers:
            del self._patchers[patcher_id]
            logger.debug("Patcher eliminado: %s", patcher_id)
            return True

        logger.debug("Patcher no encontrado para eliminar: %s", patcher_id)
        return False

    def get_patcher(self, patcher_id: str) -> PatcherDefinition | None:
        """
        Obtiene un patcher por su ID.

        Args:
            patcher_id: ID del patcher.

        Returns:
            PatcherDefinition o None si no existe.
        """
        return self._patchers.get(patcher_id)

    def get_enabled_patchers(self) -> list[PatcherDefinition]:
        """
        Obtiene la lista de patchers habilitados, ordenados por orden de ejecución.

        Returns:
            Lista de PatcherDefinition habilitados, ordenados por 'order'.
        """
        enabled = [p for p in self._patchers.values() if p.enabled]
        return sorted(enabled, key=lambda p: p.order)

    def get_all_patchers(self) -> list[PatcherDefinition]:
        """
        Obtiene todos los patchers del pipeline, ordenados por orden de ejecución.

        Returns:
            Lista de todos los PatcherDefinition, ordenados por 'order'.
        """
        return sorted(self._patchers.values(), key=lambda p: p.order)

    def enable_patcher(self, patcher_id: str) -> bool:
        """
        Habilita un patcher existente.

        Args:
            patcher_id: ID del patcher a habilitar.

        Returns:
            True si se habilitó, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        if not patcher.enabled:
            # Crear nueva instancia con enabled=True (frozen dataclass)
            self._patchers[patcher_id] = PatcherDefinition(
                patcher_id=patcher.patcher_id,
                enabled=True,
                order=patcher.order,
                config=patcher.config,
            )
            logger.debug("Patcher habilitado: %s", patcher_id)

        return True

    def disable_patcher(self, patcher_id: str) -> bool:
        """
        Deshabilita un patcher existente.

        Args:
            patcher_id: ID del patcher a deshabilitar.

        Returns:
            True si se deshabilitó, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        if patcher.enabled:
            # Crear nueva instancia con enabled=False (frozen dataclass)
            self._patchers[patcher_id] = PatcherDefinition(
                patcher_id=patcher.patcher_id,
                enabled=False,
                order=patcher.order,
                config=patcher.config,
            )
            logger.debug("Patcher deshabilitado: %s", patcher_id)

        return True

    def set_patcher_order(self, patcher_id: str, new_order: int) -> bool:
        """
        Cambia el orden de ejecución de un patcher.

        Args:
            patcher_id: ID del patcher.
            new_order: Nuevo orden de ejecución.

        Returns:
            True si se cambió, False si no existe.
        """
        patcher = self._patchers.get(patcher_id)
        if patcher is None:
            return False

        # Crear nueva instancia con el nuevo orden (frozen dataclass)
        self._patchers[patcher_id] = PatcherDefinition(
            patcher_id=patcher.patcher_id,
            enabled=patcher.enabled,
            order=new_order,
            config=patcher.config,
        )
        logger.debug("Orden de patcher cambiado: %s -> %d", patcher_id, new_order)
        return True

    def serialize_for_synthesis(self) -> dict[str, Any]:
        """
        Serializa configuración para consumo de Synthesis CLI.

        El formato de salida es compatible con los argumentos de Synthesis CLI,
        permitiendo pasar directamente los patchers habilitados.

        Returns:
            Diccionario con la configuración serializada.
        """
        enabled_patchers = self.get_enabled_patchers()

        return {
            "patchers": [p.patcher_id for p in enabled_patchers],
            "patcher_configs": {
                p.patcher_id: p.config for p in enabled_patchers if p.config
            },
            "total_enabled": len(enabled_patchers),
            "total_disabled": len(self._patchers) - len(enabled_patchers),
        }

    def to_dict(self) -> dict[str, Any]:
        """
        Serializa el pipeline completo a diccionario.

        Returns:
            Diccionario con todos los patchers del pipeline.
        """
        return {
            "version": 1,
            "patchers": [p.to_dict() for p in self.get_all_patchers()],
        }

    @classmethod
    def from_json(cls, json_path: pathlib.Path) -> PatcherPipeline:
        """
        Carga pipeline desde archivo JSON.

        Args:
            json_path: Path al archivo JSON de configuración.

        Returns:
            Nueva instancia de PatcherPipeline.

        Raises:
            FileNotFoundError: Si el archivo no existe.
            PatcherPipelineError: Si el archivo está corrupto.
        """
        if not json_path.exists():
            raise FileNotFoundError(f"Pipeline config not found: {json_path}")

        pipeline = cls(pipeline_config_path=json_path)
        return pipeline

    def to_json(self, json_path: pathlib.Path) -> None:
        """
        Guarda configuración del pipeline en archivo JSON.

        Args:
            json_path: Path donde guardar el archivo.
        """
        # S2-FIX: Validate path before writing.
        if self._path_validator is not None:
            try:
                self._path_validator.validate(json_path, strict_symlink=False)
            except PathViolation:
                logger.error("Path traversal blocked for pipeline save: %s", json_path)
                raise

        data = self.to_dict()

        # Crear directorio padre si no existe
        json_path.parent.mkdir(parents=True, exist_ok=True)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(
            "Pipeline guardado en %s: %d patchers",
            json_path,
            len(self._patchers),
        )

    def __len__(self) -> int:
        """Retorna el número total de patchers en el pipeline."""
        return len(self._patchers)

    def __contains__(self, patcher_id: str) -> bool:
        """Verifica si un patcher está en el pipeline."""
        return patcher_id in self._patchers

    def __repr__(self) -> str:
        """Representación del pipeline."""
        enabled = len(self.get_enabled_patchers())
        total = len(self._patchers)
        return f"PatcherPipeline(patchers={total}, enabled={enabled})"
