"""
FASE 5: Detector de Conflictos de Assets para Sky-Claw.

Este módulo proporciona capacidades de análisis de conflictos de archivos
"loose" (sueltos) dentro del sistema de archivos virtual de Mod Organizer 2.

RESTRICCIÓN DE SEGURIDAD: Este módulo es STRICTLY READ-ONLY.
No debe modificar, mover ni ocultar archivos.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Final, Optional
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


class AssetType(Enum):
    """Tipos de assets de Skyrim."""

    MESH = "mesh"  # .nif
    TEXTURE = "texture"  # .dds, .png, .jpg, .tga
    SCRIPT = "script"  # .pex
    CONFIG = "config"  # .ini, .json, .xml
    SOUND = "sound"  # .wav, .xwm, .fuz
    ANIMATION = "animation"  # .hkx
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AssetInfo:
    """Información de un asset individual."""

    relative_path: str  # Path relativo: meshes/armor/iron/ironcuirass.nif
    asset_type: AssetType
    mod_name: str
    size_bytes: int
    checksum: str  # MD5 hash


@dataclass(frozen=True, slots=True)
class AssetConflictReport:
    """Reporte de conflicto entre assets."""

    file_path: str  # Ruta relativa del archivo en conflicto
    winner_mod: str  # Mod que gana (mayor prioridad)
    overwritten_mods: tuple[str, ...]  # Mods sobrescritos (menor prioridad)
    asset_type: AssetType


class AssetConflictDetector:
    """
    Detector de conflictos de assets "loose" (sueltos) en el VFS de MO2.

    RESTRICCIÓN DE SEGURIDAD: Esta clase es STRICTLY READ-ONLY.
    No debe modificar, mover ni ocultar archivos.

    En MO2, el panel izquierdo determina la prioridad de assets "loose".
    El mod que aparece más abajo en la lista tiene mayor prioridad y
    sobrescribe a los de arriba.
    """

    # Directorios críticos a escanear
    CRITICAL_DIRS: Final[frozenset[str]] = frozenset(
        {
            "meshes",
            "textures",
            "scripts",
            "interface",
            "sound",
            "strings",
            "lodsettings",
            "grass",
            "music",
            "shadersfx",
        }
    )

    # Extensiones por tipo de asset
    ASSET_EXTENSIONS: Final[dict[AssetType, frozenset[str]]] = {
        AssetType.MESH: frozenset({".nif"}),
        AssetType.TEXTURE: frozenset({".dds", ".png", ".jpg", ".jpeg", ".tga"}),
        AssetType.SCRIPT: frozenset({".pex"}),
        AssetType.CONFIG: frozenset({".ini", ".json", ".xml"}),
        AssetType.SOUND: frozenset({".wav", ".xwm", ".fuz"}),
        AssetType.ANIMATION: frozenset({".hkx", ".anim"}),
    }

    def __init__(self, mo2_mods_path: Path, profile_name: str = "Default") -> None:
        """
        Inicializa el detector.

        Args:
            mo2_mods_path: Ruta al directorio "mods" de MO2
            profile_name: Nombre del perfil activo de MO2
        """
        self._mo2_mods_path = mo2_mods_path
        self._profile_name = profile_name
        self._modlist_path: Optional[Path] = None
        self._mods_path: Optional[Path] = None

        logger.debug(
            f"AssetConflictDetector inicializado: mods_path={mo2_mods_path}, "
            f"profile={profile_name}"
        )

    @property
    def mo2_mods_path(self) -> Path:
        """Ruta al directorio de mods de MO2."""
        return self._mo2_mods_path

    @property
    def profile_name(self) -> str:
        """Nombre del perfil activo."""
        return self._profile_name

    def _get_modlist_path(self) -> Path:
        """
        Obtiene la ruta al archivo modlist.txt del perfil.

        Returns:
            Path al archivo modlist.txt
        """
        # El modlist.txt está en MO2/profiles/<profile_name>/modlist.txt
        # El directorio mods_path generalmente está en MO2/mods/
        # Asumimos que mo2_mods_path es .../MO2/mods/
        mo2_root = self._mo2_mods_path.parent
        return mo2_root / "profiles" / self._profile_name / "modlist.txt"

    def parse_modlist(self) -> list[str]:
        """
        Parsea el archivo modlist.txt del perfil activo.

        MO2 usa un formato donde:
        - Líneas que empiezan con '+' están habilitadas
        - Líneas que empiezan con '-' están deshabilitadas
        - El orden es de abajo hacia arriba (último = mayor prioridad)

        Returns:
            Lista de nombres de mods en orden de prioridad (mayor a menor)

        Raises:
            FileNotFoundError: Si el archivo modlist.txt no existe
        """
        modlist_path = self._get_modlist_path()

        if not modlist_path.exists():
            logger.error(f"modlist.txt no encontrado: {modlist_path}")
            raise FileNotFoundError(f"modlist.txt no encontrado: {modlist_path}")

        enabled_mods: list[str] = []

        try:
            with open(modlist_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # MO2 guarda los mods en orden inverso de prioridad
            # El último mod en la lista tiene la mayor prioridad
            # Invertimos para que el primero sea el de mayor prioridad
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("+"):
                    mod_name = line[1:]  # Remover el prefijo '+'
                    enabled_mods.append(mod_name)
                    logger.debug(f"Mod habilitado encontrado: {mod_name}")
                elif line.startswith("-"):
                    # Mod deshabilitado, lo ignoramos
                    logger.debug(f"Mod deshabilitado ignorado: {line[1:]}")

            logger.info(
                f"Parseados {len(enabled_mods)} mods habilitados desde modlist.txt"
            )
            return enabled_mods

        except IOError as e:
            logger.error(f"Error leyendo modlist.txt: {e}")
            raise

    def get_asset_type(self, file_path: Path) -> AssetType:
        """
        Determina el tipo de asset basándose en la extensión.

        Args:
            file_path: Path al archivo

        Returns:
            AssetType correspondiente a la extensión del archivo
        """
        extension = file_path.suffix.lower()

        for asset_type, extensions in self.ASSET_EXTENSIONS.items():
            if extension in extensions:
                return asset_type

        logger.debug(f"Tipo de asset desconocido para extensión: {extension}")
        return AssetType.UNKNOWN

    def calculate_checksum(self, file_path: Path) -> str:
        """
        Calcula el checksum MD5 de un archivo.

        Args:
            file_path: Path al archivo

        Returns:
            String hexadecimal del checksum MD5

        Raises:
            FileNotFoundError: Si el archivo no existe
            IOError: Si hay error leyendo el archivo
        """
        if not file_path.exists():
            logger.error(f"Archivo no encontrado para checksum: {file_path}")
            raise FileNotFoundError(f"Archivo no encontrado: {file_path}")

        md5_hash = hashlib.md5(usedforsecurity=False)  # nosec
        mb = 1024 * 1024

        try:
            file_size = file_path.stat().st_size
            with open(file_path, "rb") as f:
                if file_size <= 2 * mb:
                    # Archivo pequeño, leer completo
                    for chunk in iter(lambda: f.read(8192), b""):
                        md5_hash.update(chunk)
                else:
                    # Archivo grande, hash parcial (primer y último MB)
                    md5_hash.update(f.read(mb))
                    f.seek(-mb, 2)
                    md5_hash.update(f.read(mb))

            checksum = md5_hash.hexdigest()
            logger.debug(f"Checksum calculado para {file_path.name}: {checksum}")
            return checksum

        except IOError as e:
            logger.error(f"Error calculando checksum para {file_path}: {e}")
            raise

    def scan_mod_directory(
        self, mod_name: str, calculate_checksums: bool = False
    ) -> dict[str, AssetInfo]:
        """
        Escanea recursivamente un directorio de mod y mapea todos los assets.

        Args:
            mod_name: Nombre del mod (nombre del directorio)

        Returns:
            Diccionario {relative_path: AssetInfo}
        """
        mod_dir = self._mo2_mods_path / mod_name

        if not mod_dir.exists():
            logger.warning(f"Directorio de mod no encontrado: {mod_dir}")
            return {}

        assets: dict[str, AssetInfo] = {}

        logger.info(f"Escaneando directorio de mod: {mod_name}")

        for file_path in mod_dir.rglob("*"):
            if not file_path.is_file():
                continue

            # Obtener la ruta relativa desde el directorio del mod
            try:
                relative_path = file_path.relative_to(mod_dir)
            except ValueError:
                logger.warning(f"No se pudo obtener ruta relativa: {file_path}")
                continue

            # Convertir a string con barras normales (estilo Skyrim)
            relative_path_str = str(relative_path).replace("\\", "/").lower()

            # Verificar si está en un directorio crítico
            path_parts = relative_path_str.split("/")
            if path_parts and path_parts[0] not in self.CRITICAL_DIRS:
                # No es un asset crítico, pero lo incluimos de todas formas
                logger.debug(
                    f"Asset fuera de directorios críticos: {relative_path_str}"
                )

            # Determinar tipo de asset
            asset_type = self.get_asset_type(file_path)

            # Calcular checksum
            if calculate_checksums:
                try:
                    checksum = self.calculate_checksum(file_path)
                except (FileNotFoundError, IOError) as e:
                    logger.warning(
                        f"Error calculando checksum, usando placeholder: {e}"
                    )
                    checksum = "ERROR"
            else:
                checksum = "SKIPPED"

            # Obtener tamaño del archivo
            try:
                size_bytes = file_path.stat().st_size
            except OSError as e:
                logger.warning(f"Error obteniendo tamaño de archivo: {e}")
                size_bytes = 0

            asset_info = AssetInfo(
                relative_path=relative_path_str,
                asset_type=asset_type,
                mod_name=mod_name,
                size_bytes=size_bytes,
                checksum=checksum,
            )

            assets[relative_path_str] = asset_info
            logger.debug(f"Asset mapeado: {relative_path_str} -> {mod_name}")

        logger.info(
            f"Escaneo completado para {mod_name}: {len(assets)} assets encontrados"
        )
        return assets

    def detect_conflicts(self) -> list[AssetConflictReport]:
        """
        Detecta todos los conflictos de assets en el VFS de MO2.

        Flujo:
        1. Obtener load order desde modlist.txt
        2. Para cada mod, escanear directorio y mapear assets
        3. Identificar paths duplicados entre mods
        4. Determinar ganador según prioridad (orden en modlist)
        5. Generar reportes de conflicto

        Returns:
            Lista de AssetConflictReport para todos los conflictos encontrados
        """
        logger.info("Iniciando detección de conflictos de assets")

        # 1. Obtener load order
        try:
            mod_list = self.parse_modlist()
        except FileNotFoundError as e:
            logger.error(f"No se pudo obtener modlist: {e}")
            return []

        if not mod_list:
            logger.warning("No se encontraron mods habilitados")
            return []

        # 2. Escanear todos los mods y mapear assets
        # Diccionario: {relative_path: [AssetInfo de cada mod que lo tiene]}
        asset_map: dict[str, list[AssetInfo]] = {}

        for mod_name in mod_list:
            mod_assets = self.scan_mod_directory(mod_name)

            for relative_path, asset_info in mod_assets.items():
                if relative_path not in asset_map:
                    asset_map[relative_path] = []
                asset_map[relative_path].append(asset_info)

        logger.info(f"Total de paths de assets únicos mapeados: {len(asset_map)}")

        # 3. Identificar conflictos (paths con más de un mod)
        conflicts: list[AssetConflictReport] = []

        for relative_path, asset_infos in asset_map.items():
            if len(asset_infos) <= 1:
                # No hay conflicto
                continue

            # 4. Determinar ganador según prioridad
            # El primer mod en la lista (mayor prioridad) gana
            # Los demás son sobrescritos
            winner = asset_infos[0]
            overwritten = tuple(info.mod_name for info in asset_infos[1:])

            conflict_report = AssetConflictReport(
                file_path=relative_path,
                winner_mod=winner.mod_name,
                overwritten_mods=overwritten,
                asset_type=winner.asset_type,
            )

            conflicts.append(conflict_report)

            logger.debug(
                f"Conflicto detectado: {relative_path} - "
                f"ganador: {winner.mod_name}, "
                f"sobrescritos: {overwritten}"
            )

        logger.info(f"Detección completada: {len(conflicts)} conflictos encontrados")
        return conflicts

    def scan_to_json(self) -> str:
        """
        Genera un JSON estructurado con el reporte completo de conflictos.

        Returns:
            JSON string con estructura:
            {
                "scan_timestamp": "ISO8601",
                "total_conflicts": int,
                "by_asset_type": { "mesh": int, "texture": int, ... },
                "conflicts": [
                    {
                        "file_path": "...",
                        "winner_mod": "...",
                        "overwritten_mods": ["...", "..."],
                        "asset_type": "mesh"
                    }
                ]
            }
        """
        logger.info("Generando reporte JSON de conflictos")

        conflicts = self.detect_conflicts()

        # Contar conflictos por tipo
        by_asset_type: dict[str, int] = {}
        for conflict in conflicts:
            type_name = conflict.asset_type.value
            by_asset_type[type_name] = by_asset_type.get(type_name, 0) + 1

        # Construir estructura del reporte
        report = {
            "scan_timestamp": datetime.now(timezone.utc).isoformat(),
            "profile": self._profile_name,
            "mods_path": str(self._mo2_mods_path),
            "total_conflicts": len(conflicts),
            "by_asset_type": by_asset_type,
            "conflicts": [
                {
                    "file_path": conflict.file_path,
                    "winner_mod": conflict.winner_mod,
                    "overwritten_mods": list(conflict.overwritten_mods),
                    "asset_type": conflict.asset_type.value,
                }
                for conflict in conflicts
            ],
        }

        json_output = json.dumps(report, indent=2, ensure_ascii=False)

        logger.info(f"Reporte JSON generado: {len(conflicts)} conflictos")
        return json_output
