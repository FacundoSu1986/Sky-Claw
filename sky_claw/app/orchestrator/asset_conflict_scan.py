"""Adapter de dominio para el escaneo de conflictos de assets (PR4).

PR4 del Strangler Fig de ``SupervisorAgent``: extrae del supervisor la
creación perezosa del :class:`AssetConflictDetector` y los dos contratos de
escaneo (plain y JSON). Sin cambio funcional intencional.

El scanner recibe únicamente su dependencia real — el resolver de rutas — y
NO conoce ni importa al supervisor. La composition root recibe
``scanner.scan`` / ``scanner.scan_json`` como callables estrechos para el
dispatcher; el supervisor conserva facades delegantes (``asset_detector``,
``scan_asset_conflicts``, ``scan_asset_conflicts_json``) porque existen
callers externos reales (la GUI accede a ``runtime.supervisor.asset_detector``
para persistir disputas, y tests/harness BDD monkeypatchean los métodos del
supervisor con late-binding).

Contratos preservados respecto del código pre-PR4:

- **Lazy**: el detector se construye en el PRIMER acceso a ``detector``
  (``MO2_PATH`` puede hidratarse después de construir el supervisor).
- **Memoización**: el mismo detector se reutiliza entre accesos.
- **Read-only**: el detector solo lee el VFS de MO2 (``AssetConflictDetector``
  es estrictamente read-only por contrato de módulo).
- **Errores**: ``(OSError, RuntimeError)`` se loggean y re-lanzan tal cual,
  en ambos contratos.
"""

from __future__ import annotations

import logging

from sky_claw.app.core.path_resolver import PathResolutionService
from sky_claw.local.assets import AssetConflictDetector, AssetConflictReport

logger = logging.getLogger(__name__)


class AssetConflictScanner:
    """Resuelve el detector de assets y ejecuta los escaneos de conflictos.

    La construcción es barata y sin efectos: el ``AssetConflictDetector`` se
    crea al primer acceso a :attr:`detector` (semántica lazy del supervisor
    pre-PR4) y se memoiza (misma instancia entre accesos).
    """

    def __init__(self, *, path_resolver: PathResolutionService) -> None:
        self._path_resolver = path_resolver
        self._detector: AssetConflictDetector | None = None

    @property
    def detector(self) -> AssetConflictDetector:
        """Inicialización lazy del AssetConflictDetector (FASE 5).

        Returns:
            AssetConflictDetector inicializado.

        Raises:
            RuntimeError: Si no se puede detectar la ruta de MO2.
        """
        if self._detector is None:
            mo2_mods_path = self._path_resolver.get_mo2_mods_path()
            profile = self._path_resolver.get_active_profile()
            self._detector = AssetConflictDetector(mo2_mods_path, profile)
            logger.info(
                "AssetConflictDetector inicializado: mods=%s, profile=%s",
                mo2_mods_path,
                profile,
            )
        return self._detector

    def scan(self) -> list[AssetConflictReport]:
        """Herramienta READ-ONLY para escanear conflictos de assets.

        Escanea el VFS de MO2 y detecta archivos "loose" sobrescritos.

        Returns:
            Lista de AssetConflictReport con todos los conflictos detectados.

        SECURITY: Esta herramienta es estrictamente READ-ONLY.
        No modifica, mueve ni oculta archivos.
        """
        logger.info("Iniciando escaneo de conflictos de assets...")
        try:
            conflicts = self.detector.detect_conflicts()
            logger.info(f"Detectados {len(conflicts)} conflictos de assets")
            return conflicts
        except (OSError, RuntimeError) as e:
            logger.error(f"Error durante escaneo de conflictos: {e}", exc_info=True)
            raise

    def scan_json(self) -> str:
        """Herramienta READ-ONLY que devuelve el reporte en formato JSON.

        Returns:
            JSON string estructurado con el reporte completo de conflictos.

        SECURITY: Esta herramienta es estrictamente READ-ONLY.
        """
        logger.info("Generando reporte JSON de conflictos de assets...")
        try:
            json_report = self.detector.scan_to_json()
            logger.info("Reporte JSON de conflictos generado exitosamente")
            return json_report
        except (OSError, RuntimeError) as e:
            logger.error(f"Error generando reporte JSON de conflictos: {e}", exc_info=True)
            raise


__all__ = ["AssetConflictScanner"]
