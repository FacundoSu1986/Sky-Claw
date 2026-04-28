"""PathResolutionService — resolución stateless de rutas MO2/Skyrim.

Extrae la lógica de resolución de rutas desde :class:`SupervisorAgent`
en un servicio inyectable que aplica el Principio de Inversión de
Dependencias (DIP).  Todas las validaciones pasan por
:class:`~sky_claw.security.path_validator.PathValidator` para
garantizar la salvaguarda contra Path Traversal (CRIT-003).

Principio EAFP: se usa ``pathlib.Path.resolve(strict=True)`` en lugar
de ``exists()`` para mitigar TOCTOU en la detección de rutas.

Parte del Sprint 1.5: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Protocol, runtime_checkable

from sky_claw.core.contracts import PathValidatorProtocol

logger = logging.getLogger("SkyClaw.PathResolution")
security_logger = logging.getLogger("SkyClaw.Security")

# Rutas candidatas para auto-detección de MO2 (ordenadas por probabilidad).
_CANDIDATE_MO2_PATHS: tuple[str, ...] = (
    r"C:\Modding\MO2",
    r"D:\Modding\MO2",
    r"E:\Modding\MO2",
    r"C:\MO2Portable",
    r"D:\MO2Portable",
    r"C:\Games\MO2",
    r"D:\Games\MO2",
)

_CANDIDATE_PF_PATHS: tuple[str, ...] = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
)


@runtime_checkable
class PathResolver(Protocol):
    """Interfaz abstracta para resolución de rutas MO2/Skyrim.

    Aplica DIP: ``SupervisorAgent`` depende de esta abstracción,
    no de la implementación concreta ``PathResolutionService``.
    """

    def validate_env_path(self, path_str: str, var_name: str) -> pathlib.Path | None:
        """Valida un path de variable de entorno con PathValidator.

        Args:
            path_str: String del path a validar.
            var_name: Nombre de la variable de entorno (para logging).

        Returns:
            Path validado o ``None`` si la validación falla.
        """
        ...

    def detect_mo2_path(self) -> pathlib.Path | None:
        """Auto-detecta la ruta de instalación de MO2 (EAFP anti-TOCTOU).

        Returns:
            Path validado al directorio de MO2, o ``None`` si no se detecta.
        """
        ...

    def resolve_modlist_path(self, profile: str) -> pathlib.Path:
        """Resuelve la ruta al ``modlist.txt`` para un perfil MO2.

        Prioridad: ``MO2_PATH`` env → auto-detección → fallback WSL2.

        Args:
            profile: Nombre del perfil MO2.

        Returns:
            Path al ``modlist.txt`` del perfil.

        Raises:
            RuntimeError: Si ninguna ruta puede ser resuelta y validada.
        """
        ...

    def get_mo2_mods_path(self) -> pathlib.Path:
        """Obtiene la ruta al directorio ``mods`` de MO2.

        Returns:
            Path validado al directorio de mods.

        Raises:
            RuntimeError: Si no se puede detectar la ruta de MO2.
        """
        ...

    def get_active_profile(self) -> str:
        """Obtiene el nombre del perfil activo de MO2.

        Returns:
            Nombre del perfil activo o ``'Default'`` si no se puede determinar.
        """
        ...


class PathResolutionService:
    """Implementación stateless de :class:`PathResolver`.

    Recibe ``PathValidator`` por inyección para garantizar la salvaguarda
    contra Path Traversal (CRIT-003) en todas las resoluciones.

    Args:
        path_validator: Instancia de ``PathValidator`` configurada con
            las raíces del sandbox.
        profile_name: Nombre del perfil MO2 por defecto.
    """

    def __init__(
        self,
        path_validator: PathValidatorProtocol,
        profile_name: str = "Default",
    ) -> None:
        self._path_validator = path_validator
        self._profile_name = profile_name

    def validate_env_path(self, path_str: str, var_name: str) -> pathlib.Path | None:
        """Valida un path de variable de entorno con PathValidator.

        CRIT-003: Mitigación para variables de entorno sin validación.

        Args:
            path_str: String del path a validar.
            var_name: Nombre de la variable de entorno (para logging).

        Returns:
            Path validado o ``None`` si la validación falla.
        """
        if not path_str:
            return None

        try:
            validated_path = self._path_validator.validate(path_str)
            return validated_path
        except Exception as exc:
            security_logger.warning(
                "%s inválido (posible intento de path traversal): %s - Error: %s",
                var_name,
                path_str,
                exc,
            )
            return None

    def detect_mo2_path(self) -> pathlib.Path | None:
        """Auto-detecta la ruta de instalación de MO2 usando EAFP.

        Reemplaza el anti-patrón ``if path.exists(): return path`` por
        ``Path.resolve(strict=True)`` dentro de bloques try/except para
        mitigar TOCTOU.  Cada ruta candidata exitosa se valida
        inmediatamente con ``PathValidator``.

        Returns:
            Path validado al directorio de MO2, o ``None`` si no se detecta.
        """
        # Fase 1: Rutas hardcodeadas comunes
        for raw in _CANDIDATE_MO2_PATHS:
            candidate = pathlib.Path(raw) / "ModOrganizer.exe"
            try:
                resolved_exe = candidate.resolve(strict=True)
                # Validar el directorio padre (directorio de MO2)
                validated = self._path_validator.validate(resolved_exe.parent)
                logger.debug(
                    "MO2 auto-detectado en ruta candidata: %s",
                    validated,
                )
                return validated
            except (FileNotFoundError, OSError) as exc:
                logger.debug(
                    "MO2 candidate path falló resolución: %s — %s",
                    raw,
                    exc,
                )
                continue
            except Exception:
                logger.error(
                    "MO2 candidate path falló validación de seguridad: %s",
                    raw,
                    exc_info=True,
                    extra={
                        "component": "PathResolutionService",
                        "operation": "detect_mo2_path",
                    },
                )
                continue

        # Fase 2: LOCALAPPDATA/ModOrganizer
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            md = pathlib.Path(local_app_data) / "ModOrganizer"
            try:
                resolved_md = md.resolve(strict=True)
            except (FileNotFoundError, OSError):
                resolved_md = None

            if resolved_md is not None:
                try:
                    for child in resolved_md.iterdir():
                        if not child.is_dir():
                            continue
                        exe_candidate = child / "ModOrganizer.exe"
                        try:
                            resolved_exe = exe_candidate.resolve(strict=True)
                            validated = self._path_validator.validate(
                                resolved_exe.parent,
                            )
                            logger.debug(
                                "MO2 auto-detectado en LOCALAPPDATA: %s",
                                validated,
                            )
                            return validated
                        except (FileNotFoundError, OSError):
                            continue
                        except Exception:
                            logger.error(
                                "MO2 LOCALAPPDATA child falló validación: %s",
                                child,
                                exc_info=True,
                                extra={
                                    "component": "PathResolutionService",
                                    "operation": "detect_mo2_path",
                                },
                            )
                            continue
                except OSError as exc:
                    logger.error(
                        "Error iterando LOCALAPPDATA/ModOrganizer: %s",
                        exc,
                        exc_info=True,
                        extra={
                            "component": "PathResolutionService",
                            "operation": "detect_mo2_path",
                        },
                    )

        # Fase 3: Program Files
        for pf_raw in _CANDIDATE_PF_PATHS:
            candidate = pathlib.Path(pf_raw) / "Mod Organizer 2" / "ModOrganizer.exe"
            try:
                resolved_exe = candidate.resolve(strict=True)
                validated = self._path_validator.validate(resolved_exe.parent)
                logger.debug(
                    "MO2 auto-detectado en Program Files: %s",
                    validated,
                )
                return validated
            except (FileNotFoundError, OSError):
                continue
            except Exception:
                logger.error(
                    "MO2 Program Files candidate falló validación: %s",
                    pf_raw,
                    exc_info=True,
                    extra={
                        "component": "PathResolutionService",
                        "operation": "detect_mo2_path",
                    },
                )
                continue

        logger.warning("Auto-detección de MO2 falló — ninguna ruta candidata válida")
        return None

    def resolve_modlist_path(self, profile: str) -> pathlib.Path:
        """Resuelve la ruta al ``modlist.txt`` para un perfil MO2.

        Prioridad:
        1. ``MO2_PATH`` environment variable (validada con PathValidator).
        2. Auto-detección vía :meth:`detect_mo2_path`.
        3. Fallback WSL2 ``/mnt/c/Modding/MO2`` (validado con PathValidator).

        Args:
            profile: Nombre del perfil MO2.

        Returns:
            Path al ``modlist.txt`` del perfil.

        Raises:
            RuntimeError: Si ninguna ruta puede ser resuelta y validada.
        """
        # 1. MO2_PATH environment variable takes precedence
        mo2_base_str = os.environ.get("MO2_PATH", "")
        if mo2_base_str:
            validated_base = self.validate_env_path(mo2_base_str, "MO2_PATH")
            if validated_base:
                return validated_base / "profiles" / profile / "modlist.txt"
            logger.warning(
                "MO2_PATH='%s' rechazado por validación de seguridad (CRIT-003). Intentando auto-detección.",
                mo2_base_str,
            )

        # 2. Best-effort auto-detection
        mo2_base = self.detect_mo2_path()
        if mo2_base:
            return mo2_base / "profiles" / profile / "modlist.txt"

        # 3. Fallback: WSL2 default path — también validado
        fallback_path = pathlib.Path("/mnt/c/Modding/MO2") / "profiles" / profile / "modlist.txt"
        try:
            validated_fallback = self._path_validator.validate(fallback_path)
            logger.warning(
                "MO2_PATH no configurado y auto-detección falló para perfil '%s'. "
                "Usando fallback WSL2 validado: %s. "
                "Configure la variable de entorno MO2_PATH para evitar este aviso.",
                profile,
                validated_fallback,
            )
            return validated_fallback
        except Exception as exc:
            logger.error(
                "Fallback WSL2 también falló validación para perfil '%s': %s",
                profile,
                exc,
                exc_info=True,
                extra={
                    "component": "PathResolutionService",
                    "operation": "resolve_modlist_path",
                },
            )
            raise RuntimeError(
                f"No se pudo resolver ni validar la ruta de modlist para el "
                f"perfil '{profile}'. Configure MO2_PATH en las variables de "
                f"entorno o verifique la instalación de MO2."
            ) from exc

    def get_mo2_mods_path(self) -> pathlib.Path:
        """Obtiene la ruta al directorio ``mods`` de MO2.

        Intenta obtener la ruta desde variables de entorno o usar auto-detección.
        Cada paso usa EAFP y validación con PathValidator.

        Returns:
            Path validado al directorio de mods.

        Raises:
            RuntimeError: Si no se puede detectar la ruta de MO2.
        """
        # Intentar obtener desde variable de entorno MO2_MODS_PATH
        mo2_mods_path_str = os.environ.get("MO2_MODS_PATH", "")
        if mo2_mods_path_str:
            validated_path = self.validate_env_path(mo2_mods_path_str, "MO2_MODS_PATH")
            if validated_path is not None:
                try:
                    resolved = validated_path.resolve(strict=True)
                    return resolved
                except (FileNotFoundError, OSError) as exc:
                    logger.debug(
                        "MO2_MODS_PATH resolved pero no existe: %s — %s",
                        validated_path,
                        exc,
                    )

        # Intentar construir desde MO2_PATH
        mo2_base_str = os.environ.get("MO2_PATH", "")
        if mo2_base_str:
            validated_base = self.validate_env_path(mo2_base_str, "MO2_PATH")
            if validated_base is not None:
                mods_path = validated_base / "mods"
                try:
                    resolved_mods = mods_path.resolve(strict=True)
                    return resolved_mods
                except (FileNotFoundError, OSError):
                    logger.debug(
                        "MO2_PATH/mods no existe: %s",
                        mods_path,
                    )

        # Fallback: usar auto-detección
        mo2_base = self.detect_mo2_path()
        if mo2_base:
            mods_path = mo2_base / "mods"
            try:
                resolved_mods = mods_path.resolve(strict=True)
                return resolved_mods
            except (FileNotFoundError, OSError):
                logger.error(
                    "Auto-detect MO2 mods path falló: %s",
                    mods_path,
                    exc_info=True,
                    extra={
                        "component": "PathResolutionService",
                        "operation": "get_mo2_mods_path",
                    },
                )

        raise RuntimeError(
            "No se pudo detectar la ruta de MO2. Configure MO2_PATH o MO2_MODS_PATH en las variables de entorno."
        )

    def get_active_profile(self) -> str:
        """Obtiene el nombre del perfil activo de MO2.

        Returns:
            Nombre del perfil activo o ``'Default'`` si no se puede determinar.
        """
        # Intentar obtener desde variable de entorno
        profile = os.environ.get("MO2_PROFILE", "")
        if profile:
            return profile

        # Usar el perfil almacenado en la instancia
        if self._profile_name:
            return self._profile_name

        return "Default"
