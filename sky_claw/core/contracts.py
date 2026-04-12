# -*- coding: utf-8 -*-
"""
contracts.py — Validación de contratos entre agentes del sistema Sky-Claw.

Implementa un SchemaRegistry poblado al inicializar el módulo con los modelos
Pydantic v2 de sky_claw.core.schemas, y decoradores @validate_input,
@validate_output y @validate_contract para asegurar cumplimiento de contratos
en tiempo de ejecución con lookup O(1).

Refactorizado: 2026-04-03 (Ticket 1.1 — C-03)
  - Corregido bug de import shadowing que anulaba la validación.
  - Eliminada doble ejecución en validate_contract.
  - Reemplazado importlib dinámico por SchemaRegistry O(1) a nivel de módulo.
  - Manejo estricto de ValidationError de Pydantic v2.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, Optional, Type

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("SkyClaw.Contracts")


# ============================================================================
# SchemaRegistry — Diccionario global O(1) poblado al importar el módulo
# ============================================================================

# Mapa nombre_de_clase (str) -> Clase Pydantic (Type[BaseModel])
# Poblado lazily en la primera llamada a _ensure_registry().
_SCHEMA_REGISTRY: Dict[str, Type[BaseModel]] = {}
_REGISTRY_POPULATED: bool = False


def _ensure_registry() -> None:
    """
    Lazy-init: escanea sky_claw.core.schemas la primera vez que se
    necesita resolver un schema.  Todas las llamadas subsequentes
    son un no-op (O(1) check de bandera booleana).

    Se difiere a primera invocación para evitar circular imports con
    sky_claw.core.__init__.py que importa este módulo.
    """
    global _REGISTRY_POPULATED
    if _REGISTRY_POPULATED:
        return
    _REGISTRY_POPULATED = True

    try:
        import importlib

        _schemas_module = importlib.import_module("sky_claw.core.schemas")
    except ImportError:
        logger.warning(
            "No se pudo importar sky_claw.core.schemas — "
            "los decoradores de contratos funcionarán en modo pass-through."
        )
        return

    for attr_name in dir(_schemas_module):
        attr = getattr(_schemas_module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, BaseModel)
            and attr is not BaseModel
        ):
            _SCHEMA_REGISTRY[attr_name] = attr

    logger.debug(
        "SchemaRegistry poblado con %d modelos: %s",
        len(_SCHEMA_REGISTRY),
        list(_SCHEMA_REGISTRY.keys()),
    )


# ============================================================================
# Registro de contratos — mapea "Agente.método" -> schemas de I/O
# ============================================================================

# Valores posibles:
#   - Nombre de un modelo Pydantic registrado (str) → se resuelve via
#     _SCHEMA_REGISTRY en O(1).
#   - None / tipo primitivo ("dict", "str", "list[dict]") → sin validación
#     Pydantic, se deja pasar.
CONTRACT_SCHEMAS: Dict[str, Dict[str, Optional[str]]] = {
    "SupervisorAgent.dispatch_tool": {
        "input": "AgentToolRequest",
        "output": "AgentToolResponse",
    },
    "ScraperAgent.query_nexus": {
        "input": "ScrapingQuery",
        "output": None,  # dict genérico — sin schema Pydantic
    },
    "DatabaseAgent.get_mods": {
        "input": None,
        "output": None,
    },
    "PurpleSecurityAgent.audit_local_file": {
        "input": "SecurityAuditRequest",
        "output": "SecurityAuditResponse",
    },
    "LLMRouter.chat": {
        "input": None,
        "output": None,
    },
}


def _resolve_schema(name: Optional[str]) -> Optional[Type[BaseModel]]:
    """Busca un modelo Pydantic en el registry O(1). Retorna None si no aplica."""
    if name is None:
        return None
    _ensure_registry()
    schema_cls = _SCHEMA_REGISTRY.get(name)
    if schema_cls is None and name not in ("dict", "str", "list[dict]"):
        logger.warning(
            "Schema '%s' declarado en CONTRACT_SCHEMAS pero no encontrado "
            "en SchemaRegistry. Verificar sky_claw.core.schemas.",
            name,
        )
    return schema_cls


# ============================================================================
# Decoradores de validación
# ============================================================================


def validate_input(method_name: str) -> Callable:
    """
    Decorador que valida los kwargs de entrada contra el schema Pydantic
    registrado para ``<ClassName>.<method_name>``.

    El schema se resuelve en O(1) desde _SCHEMA_REGISTRY al momento de la
    llamada, sin importaciones dinámicas.

    Args:
        method_name: Nombre del método tal como aparece en CONTRACT_SCHEMAS
                     (sin prefijo de clase — se construye en runtime).

    Raises:
        ValueError: Si la validación Pydantic falla (envuelve ValidationError
                    con contexto legible).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            schema_key = f"{self.__class__.__name__}.{method_name}"
            contract = CONTRACT_SCHEMAS.get(schema_key)

            if contract is None:
                # No hay contrato para este método — pass-through.
                return await func(self, *args, **kwargs)

            schema_cls = _resolve_schema(contract.get("input"))

            if schema_cls is None:
                # Contrato declarado pero sin modelo Pydantic — pass-through.
                return await func(self, *args, **kwargs)

            # --- Validación Pydantic v2 estricta ---
            try:
                # Construir dict de datos a validar.
                # Prioridad: kwargs explícitos.  Si hay un único arg posicional
                # que es dict, mezclarlo con kwargs.
                data = dict(kwargs)
                if args and isinstance(args[0], dict):
                    data = {**args[0], **data}
                    args = args[1:]  # consumir el dict posicional

                validated = schema_cls.model_validate(data)
                clean_kwargs = validated.model_dump()

                logger.debug(
                    "[CONTRACT·IN] %s validado → %s",
                    schema_key,
                    {k: type(v).__name__ for k, v in clean_kwargs.items()},
                )

                return await func(self, *args, **clean_kwargs)

            except ValidationError as exc:
                logger.error(
                    "[CONTRACT·IN] Validación fallida para %s:\n%s",
                    schema_key,
                    exc.errors(),
                )
                raise ValueError(
                    f"Entrada inválida para {schema_key}: "
                    f"{exc.error_count()} error(es) de validación.\n"
                    f"{exc}"
                ) from exc

        return wrapper

    return decorator


def validate_output(method_name: str) -> Callable:
    """
    Decorador que valida el valor retornado contra el schema Pydantic
    registrado para ``<ClassName>.<method_name>``.

    Args:
        method_name: Nombre del método (sin prefijo de clase).

    Raises:
        ValueError: Si la salida no cumple con el schema Pydantic.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = await func(self, *args, **kwargs)

            schema_key = f"{self.__class__.__name__}.{method_name}"
            contract = CONTRACT_SCHEMAS.get(schema_key)

            if contract is None:
                return result

            schema_cls = _resolve_schema(contract.get("output"))

            if schema_cls is None:
                return result

            # --- Validación del resultado ---
            try:
                if isinstance(result, dict):
                    validated = schema_cls.model_validate(result)
                elif isinstance(result, BaseModel):
                    validated = schema_cls.model_validate(result.model_dump())
                else:
                    # No es dict ni BaseModel — no se puede validar con Pydantic.
                    logger.debug(
                        "[CONTRACT·OUT] %s retornó %s — "
                        "salteando validación Pydantic (tipo no-dict).",
                        schema_key,
                        type(result).__name__,
                    )
                    return result

                logger.debug("[CONTRACT·OUT] %s validado correctamente.", schema_key)
                return validated.model_dump()

            except ValidationError as exc:
                logger.error(
                    "[CONTRACT·OUT] Validación fallida para %s:\n%s",
                    schema_key,
                    exc.errors(),
                )
                raise ValueError(
                    f"Salida inválida para {schema_key}: "
                    f"{exc.error_count()} error(es) de validación.\n"
                    f"{exc}"
                ) from exc

        return wrapper

    return decorator


def validate_contract(method_name: str) -> Callable:
    """
    Decorador combinado: valida entrada *y* salida en una sola pasada.

    Equivalente a aplicar ``@validate_input`` + ``@validate_output``
    pero sin la doble ejecución del decorador anterior.

    Args:
        method_name: Nombre del método (sin prefijo de clase).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            schema_key = f"{self.__class__.__name__}.{method_name}"
            contract = CONTRACT_SCHEMAS.get(schema_key)

            # ── Fase 1: Validar entrada ──
            clean_args = args
            clean_kwargs = kwargs

            if contract:
                input_cls = _resolve_schema(contract.get("input"))
                if input_cls is not None:
                    try:
                        data = dict(kwargs)
                        if args and isinstance(args[0], dict):
                            data = {**args[0], **data}
                            clean_args = args[1:]

                        validated_in = input_cls.model_validate(data)
                        clean_kwargs = validated_in.model_dump()
                        logger.debug("[CONTRACT·IN] %s validado.", schema_key)

                    except ValidationError as exc:
                        logger.error(
                            "[CONTRACT·IN] %s fallido:\n%s",
                            schema_key,
                            exc.errors(),
                        )
                        raise ValueError(
                            f"Entrada inválida para {schema_key}: {exc}"
                        ) from exc

            # ── Fase 2: Ejecutar función (una sola vez) ──
            result = await func(self, *clean_args, **clean_kwargs)

            # ── Fase 3: Validar salida ──
            if contract:
                output_cls = _resolve_schema(contract.get("output"))
                if output_cls is not None:
                    try:
                        if isinstance(result, dict):
                            validated_out = output_cls.model_validate(result)
                        elif isinstance(result, BaseModel):
                            validated_out = output_cls.model_validate(
                                result.model_dump()
                            )
                        else:
                            return result

                        logger.debug("[CONTRACT·OUT] %s validado.", schema_key)
                        return validated_out.model_dump()

                    except ValidationError as exc:
                        logger.error(
                            "[CONTRACT·OUT] %s fallido:\n%s",
                            schema_key,
                            exc.errors(),
                        )
                        raise ValueError(
                            f"Salida inválida para {schema_key}: {exc}"
                        ) from exc

            return result

        return wrapper

    return decorator


# ============================================================================
# Utilidades públicas
# ============================================================================


def get_contract_schema(agent_method: str) -> Dict[str, Optional[str]]:
    """
    Retorna las definiciones de schema para un método específico.

    Args:
        agent_method: Clave en formato "Agente.metodo"

    Returns:
        Dict con claves "input" y "output" (nombres de schemas o None).
    """
    return CONTRACT_SCHEMAS.get(agent_method, {})


def get_schema_class(name: str) -> Optional[Type[BaseModel]]:
    """
    Retorna la clase Pydantic registrada por nombre, o None.

    Útil para introspección y tests.
    """
    _ensure_registry()
    return _SCHEMA_REGISTRY.get(name)


def verify_contract(agent_class: type, method_name: str) -> bool:
    """
    Verifica si un método tiene decorador de contrato aplicado.

    Args:
        agent_class: Clase del agente.
        method_name: Nombre del método a verificar.

    Returns:
        True si el método tiene ``__wrapped__`` (indica @functools.wraps).
    """
    method = getattr(agent_class, method_name, None)
    if method is None:
        return False
    return hasattr(method, "__wrapped__")


def list_registered_schemas() -> Dict[str, Type[BaseModel]]:
    """Retorna una copia del SchemaRegistry para inspección."""
    _ensure_registry()
    return dict(_SCHEMA_REGISTRY)


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    "CONTRACT_SCHEMAS",
    "validate_input",
    "validate_output",
    "validate_contract",
    "get_contract_schema",
    "get_schema_class",
    "verify_contract",
    "list_registered_schemas",
]
