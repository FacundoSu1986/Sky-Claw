"""
Herramientas de Sky Claw para integración con herramientas externas.

Este paquete proporciona integración con:
- Mutagen Synthesis: Pipeline de parcheo automatizado
- Gestión de pipelines de patchers
"""

from .dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODNotFoundError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
)
from .patcher_pipeline import (
    PatcherConfigError,
    PatcherDefinition,
    PatcherNotFoundError,
    PatcherPipeline,
    PatcherPipelineError,
)
from .synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisNotFoundError,
    SynthesisResult,
    SynthesisRunner,
    SynthesisTimeoutError,
    SynthesisValidationError,
)
from .synthesis_service import SynthesisPipelineService

__all__ = [
    "DynDOLODConfig",
    "DynDOLODExecutionError",
    "DynDOLODNotFoundError",
    "DynDOLODPipelineResult",
    # DynDOLOD Runner
    "DynDOLODRunner",
    "DynDOLODTimeoutError",
    "PatcherConfigError",
    "PatcherDefinition",
    "PatcherNotFoundError",
    # Patcher Pipeline
    "PatcherPipeline",
    "PatcherPipelineError",
    "SynthesisConfig",
    "SynthesisExecutionError",
    "SynthesisNotFoundError",
    # Synthesis Service
    "SynthesisPipelineService",
    "SynthesisResult",
    # Synthesis Runner
    "SynthesisRunner",
    "SynthesisTimeoutError",
    "SynthesisValidationError",
]
