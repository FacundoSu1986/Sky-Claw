"""RecordConflictScanner: seam extraído del SupervisorAgent (PR6 del Strangler Fig).

Análisis PROFUNDO de conflictos a nivel *record* vía xEdit headless (read-only).
A diferencia del escaneo liviano de assets, corre xEdit como subproceso —lento,
requiere SSEEdit instalado— y detecta los conflictos que causan CTDs.

Recibe sus dependencias explícitamente (``PathResolutionService`` y el
``output_dir`` de los patches) — NO el supervisor, NO un service locator. El
supervisor conserva ``scan_record_conflicts`` como facade delegante porque hay
un caller externo real: la GUI llama ``runtime.supervisor.scan_record_conflicts()``
(``sky_claw/app/gui/sky_claw_gui.py``) y el bridge ``persist_record_conflicts``
lleva el reporte a la tabla ``conflicts``.

Contratos preservados verbatim respecto del código pre-PR6 (sin cambio funcional):

- **Perfil**: ``profile`` explícito manda; ``None`` cae en
  ``path_resolver.get_active_profile()``.
- **Plugins explícitos**: si ``plugins is not None`` NO se lee el load order del
  disco, ni siquiera para una lista vacía.
- **Precedencia**: ``loadorder.txt`` primero, ``plugins.txt`` como siguiente
  candidato — el ESPEJO de la del :class:`~sky_claw.app.orchestrator.plugin_limit_guard.PluginLimitGuard`.
- **Fallback en lista vacía**: si ``loadorder.txt`` existe pero parsea a ``[]``,
  se sigue al siguiente candidato. El guard M-04 hace lo CONTRARIO (la lista
  vacía gana). Las dos políticas son distintas a propósito y no se unifican;
  ``tests/test_scan_record_conflicts.py`` congela ambas por igualdad literal.
- **Cero plugins**: ``ConflictReport`` vacío y xEdit NO se ejecuta (tampoco se
  resuelven sus rutas).
- **Rutas obligatorias**: sin ``SKYRIM_PATH``/``XEDIT_PATH`` se lanza el mismo
  ``RuntimeError`` con el mensaje histórico.

I/O bloqueante: la lectura del load order queda síncrona dentro del método
async, tal como estaba. El ``asyncio.to_thread`` que sí usa ``PluginLimitGuard``
sería un cambio de semántica de concurrencia, fuera del alcance de un PR de
extracción sin cambio funcional.
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

from sky_claw.app.orchestrator.active_plugins import PluginListSource, parse_active_plugins
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer, ConflictReport

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import PathResolutionService

logger = logging.getLogger(__name__)

#: Timeout (s) del análisis profundo de xEdit: el default de 120s mata escaneos
#: de load orders grandes que legítimamente tardan varios minutos (review Codex
#: #226). 15 min cubre perfiles pesados sin colgar la UI indefinidamente.
DEEP_SCAN_TIMEOUT_SECONDS = 900

#: Precedencia congelada de record-conflicts: ``loadorder.txt`` primero. Es el
#: ESPEJO del orden del ``PluginLimitGuard`` y su regla de fallback también
#: difiere — ver el docstring del módulo. La anotación fija cada ``source`` a
#: ``PluginListSource`` para que ``parse_active_plugins`` reciba el ``Literal``
#: exacto: una expresión condicional inline infiere ``str`` y rompe el gate
#: estricto (el P1 que la review de #540 encontró en PR5).
_LOAD_ORDER_CANDIDATES: tuple[tuple[str, PluginListSource], ...] = (
    ("loadorder.txt", "loadorder"),
    ("plugins.txt", "plugins_txt"),
)


class RecordConflictScanner:
    """Corre el análisis de records de xEdit para un perfil de MO2.

    La construcción es barata y sin efectos: ni resuelve rutas ni toca el disco.
    El ``XEditRunner`` se construye recién cuando hay plugins que analizar.

    Args:
        path_resolver: Resolver de rutas de MO2 (el MISMO del supervisor, no un
            validator ni un objeto fabricado).
        output_dir: Directorio de salida de los patches de xEdit. Se inyecta
            desde el wiring (``<BACKUP_STAGING_DIR>/patches``) para que este
            módulo no importe al supervisor. Se conserva **relativo** si llega
            relativo: ``XEditRunner`` lo resuelve contra el cwd del scan.
        timeout: Segundos de timeout del subproceso de xEdit.
    """

    def __init__(
        self,
        *,
        path_resolver: PathResolutionService,
        output_dir: pathlib.Path,
        timeout: int = DEEP_SCAN_TIMEOUT_SECONDS,
    ) -> None:
        self._path_resolver = path_resolver
        self._output_dir = output_dir
        self._timeout = timeout

    async def scan(
        self,
        profile: str | None = None,
        plugins: list[str] | None = None,
    ) -> ConflictReport:
        """FASE 6: análisis PROFUNDO de conflictos a nivel record vía xEdit (read-only).

        Args:
            profile: perfil MO2 a inspeccionar (por defecto, el activo).
            plugins: lista explícita de plugins; si es ``None`` se lee el load
                order del perfil (``loadorder.txt``, fallback ``plugins.txt``).

        Returns:
            ``ConflictReport`` con los pares de plugins en disputa (vacío si no
            hay plugins activos).

        Raises:
            RuntimeError: si faltan SKYRIM_PATH o XEDIT_PATH, o si xEdit falla.
        """
        # Import perezoso preservado del código pre-PR6: importar el runner acá
        # mantiene el grafo de imports en tiempo de carga tal como estaba.
        from sky_claw.local.xedit.runner import XEditRunner

        profile = profile or self._path_resolver.get_active_profile()
        if plugins is None:
            # El load order de plugins vive en loadorder.txt (fallback plugins.txt),
            # siblings de modlist.txt en el dir del perfil — NO en modlist.txt, que
            # lista mods (review Copilot #226). utf-8-sig tolera BOM.
            profile_dir = self._path_resolver.resolve_modlist_path(profile).parent
            plugins = []
            for candidate, source in _LOAD_ORDER_CANDIDATES:
                lo_path = profile_dir / candidate
                if lo_path.exists():
                    plugins = parse_active_plugins(
                        lo_path.read_text(encoding="utf-8-sig"),
                        source=source,
                    )
                    # Fallback propio de record-conflicts: un candidato que existe
                    # pero parsea a [] NO gana; se sigue al siguiente.
                    if plugins:
                        break
        if not plugins:
            logger.info("Análisis profundo: sin plugins activos en perfil '%s'.", profile)
            return ConflictReport(total_conflicts=0, critical_conflicts=0)

        game_path = self._path_resolver.get_skyrim_path()
        xedit_path = self._path_resolver.get_xedit_path()
        if game_path is None or xedit_path is None:
            raise RuntimeError("El análisis profundo requiere SKYRIM_PATH y XEDIT_PATH configurados.")

        xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=self._output_dir,
            timeout=self._timeout,
        )
        logger.info("Análisis profundo de conflictos: %d plugins, perfil '%s'.", len(plugins), profile)
        return await ConflictAnalyzer().analyze(plugins, xedit_runner)


__all__ = ["DEEP_SCAN_TIMEOUT_SECONDS", "RecordConflictScanner"]
