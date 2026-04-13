# -*- coding: utf-8 -*-
"""
schemas.py - Modelos de validación Pydantic para entrada/salida de agentes del sistema Sky-Claw.
"""

import logging
import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, ConfigDict

from .validators import validate_url_ssrf, validate_path_strict

logger = logging.getLogger("SkyClaw.schemas")


class ModMetadata(BaseModel):
    """Schema validado para metadata de mods de Skyrim."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mod_id: int = Field(..., gt=0, description="ID único del mod en Nexus")
    name: str = Field(..., min_length=1, max_length=200)
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    category: Literal["armor", "weapon", "quest", "interface", "gameplay", "other"]
    author: str = Field(..., max_length=100)
    dependencies: list[int] = Field(default_factory=list)
    description: Optional[str] = Field(None, max_length=2000)
    downloaded_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, v: str) -> str:
        """Sanitiza el nombre removiendo caracteres potencialmente peligrosos."""
        return re.sub(r'[<>"\'\n\r]', "", v).strip()


class ScrapingQuery(BaseModel):
    """Schema para consultas de scraping a Nexus Mods."""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(..., min_length=1, max_length=500)
    url: Optional[str] = Field(None, description="URL objetivo para scraping")
    mod_id: Optional[int] = Field(None, gt=0)
    force_stealth: bool = Field(
        default=False, description="Forzar scraping por Playwright omitiendo la API"
    )
    target_data: Optional[
        Literal["dependencies", "files", "changelog", "forum_known_issues"]
    ] = Field(default=None, description="Tipo de información a extraer")
    include_description: bool = True

    @field_validator("url", mode="before")
    @classmethod
    def validate_url_ssrf(cls, v: Optional[str]) -> Optional[str]:
        """Valida URL contra ataques SSRF."""
        if v is None:
            return v
        return validate_url_ssrf(v)

    @field_validator("query")
    @classmethod
    def sanitize_query(cls, v: str) -> str:
        """Sanitiza la consulta removiendo caracteres peligrosos."""
        return re.sub(r'[<>"\']', "", v).strip()


class SecurityAuditRequest(BaseModel):
    """Schema para solicitudes de auditoría de seguridad."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_path: str = Field(..., min_length=1)
    audit_type: Literal["file", "repository", "directory"] = "file"
    depth: int = Field(1, ge=1, le=5)
    include_vectors: bool = True

    @field_validator("target_path", mode="before")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Prevenir path traversal attacks mediante resolución robusta de paths.

        Utiliza pathlib.Path.resolve() e is_relative_to() a través de
        validate_path_strict() para confinar la ruta al sandbox autorizado.
        Registra cualquier intento de violación de seguridad.
        """
        # Truncate logged value to 120 chars to prevent log injection / flooding.
        _logged_v = repr(v[:120]) if len(v) > 120 else repr(v)
        if "\x00" in v or "%00" in v:
            logger.warning(
                "Security violation blocked: null byte detected in target_path %s",
                _logged_v,
            )
            raise ValueError("Byte nulo detectado en path")
        try:
            return validate_path_strict(v)
        except ValueError as exc:
            logger.warning(
                "Security violation blocked: path traversal attempt in target_path %s – %s",
                _logged_v,
                exc,
            )
            raise


class SecurityAuditResponse(BaseModel):
    """Schema para respuestas de auditoría de seguridad."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target: str
    findings: list[dict]
    risk_score: float = Field(..., ge=0.0, le=1.0)
    recommendations: list[str]
    audited_at: datetime = Field(default_factory=datetime.utcnow)


class AgentToolRequest(BaseModel):
    """Schema para solicitudes de ejecución de herramientas de agentes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: str = Field(..., min_length=1)
    parameters: dict = Field(default_factory=dict)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    requires_confirmation: bool = False
    timeout_seconds: int = Field(30, gt=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RouteClassification(BaseModel):
    """Schema para clasificación de rutas en el LLMRouter usando LangChain LCEL."""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal[
        "CONSULTA_MODDING",
        "COMANDO_SISTEMA",
        "EJECUCION_HERRAMIENTA",
        "RAG_CONSULTA",
        "CHAT_GENERAL",
    ] = Field(..., description="Intento clasificado del mensaje del usuario")

    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confianza de la clasificación"
    )

    target_agent: Optional[str] = Field(
        None, description="Agente objetivo si el intento requiere despacho específico"
    )

    tool_name: Optional[str] = Field(
        None,
        description="Nombre de la herramienta si el intento es EJECUCION_HERRAMIENTA",
    )

    parameters: dict = Field(
        default_factory=dict,
        description="Parámetros extraídos para la herramienta o agente",
    )

    requires_context: bool = Field(
        default=False,
        description="Si se requiere contexto adicional (RAG, historial, etc.)",
    )

    metadata: dict = Field(
        default_factory=dict, description="Metadatos adicionales para orquestación"
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Valida que la confianza esté en un rango aceptable."""
        if v < 0.5:
            logger.warning(f"Confianza baja en clasificación: {v}")
        return v


class AgentToolResponse(BaseModel):
    """Schema para respuestas de ejecución de herramientas de agentes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: str
    result: Optional[dict] = None
    success: bool
    error: Optional[str] = None
    execution_time_ms: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
