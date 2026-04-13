"""
Herramientas de Sky Claw para integración con herramientas externas.

Este paquete proporciona integración con:
- Mutagen Synthesis: Pipeline de parcheo automatizado
- Gestión de pipelines de patchers
"""

from .synthesis_runner import (
    SynthesisRunner,
    SynthesisConfig,
    SynthesisResult,
    SynthesisExecutionError,
    SynthesisTimeoutError,
    SynthesisNotFoundError,
    SynthesisValidationError,
)
from .patcher_pipeline import (
    PatcherPipeline,
    PatcherDefinition,
    PatcherPipelineError,
    PatcherNotFoundError,
    PatcherConfigError,
)
from .synthesis_service import SynthesisPipelineService
from .dyndolod_runner import (
    DynDOLODRunner,
    DynDOLODConfig,
    DynDOLODPipelineResult,
    DynDOLODExecutionError,
    DynDOLODTimeoutError,
    DynDOLODNotFoundError,
)

__all__ = [
    # Synthesis Runner
    "SynthesisRunner",
    "SynthesisConfig",
    "SynthesisResult",
    "SynthesisExecutionError",
    "SynthesisTimeoutError",
    "SynthesisNotFoundError",
    "SynthesisValidationError",
    # Synthesis Service
    "SynthesisPipelineService",
    # Patcher Pipeline
    "PatcherPipeline",
    "PatcherDefinition",
    "PatcherPipelineError",
    "PatcherNotFoundError",
    "PatcherConfigError",
    # DynDOLOD Runner
    "DynDOLODRunner",
    "DynDOLODConfig",
    "DynDOLODPipelineResult",
    "DynDOLODExecutionError",
    "DynDOLODTimeoutError",
    "DynDOLODNotFoundError",
]
