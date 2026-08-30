"""Provider lazy de las dependencias de Fases B/C del ritual de grass (Stage 8).

PR3 del Strangler Fig de ``SupervisorAgent``: extrae del supervisor la
construcción perezosa de :class:`GrassRuntimeDeps` y la resolución memoizada
del ``MO2Controller`` PROPIO del ritual. Sin cambio funcional intencional.

El provider recibe sus dependencias de forma explícita (resolver de rutas,
validator de modding y perfil de la sesión) y NO conoce ni importa al
supervisor: es un adapter de dominio consumido por la composition root
(``build_orchestration_composition``) y, a través de ella, por
:class:`GrassCacheService`.
"""

from __future__ import annotations

import pathlib

from sky_claw.app.core.path_resolver import PathResolutionService
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import GrassProfileManager
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools.grass_cache_service import GrassRuntimeDeps


class GrassRuntimeDepsProvider:
    """Resuelve de forma perezosa las deps de Fases B/C del ritual de grass.

    Lo llama :class:`GrassCacheService` al **ejecutar** el ritual (no al
    construirse): en la GUI ``MO2_PATH``/``SKYRIM_PATH`` se hidratan después
    de construir el supervisor, así que resolverlas antes daría ``None``
    permanente (review Codex #301). Devuelve ``None`` si todavía faltan — el
    servicio responde con su error de contrato accionable y se reintenta en la
    próxima corrida.

    Usa el validator de MODDING (el mismo que el path resolver), no el rollback
    backup-only que rechazaría ``<MO2>/profiles`` y ``<MO2>/mods``; y clona el
    perfil ACTIVO (``profile_name``), no el ``Default`` por defecto.

    El ``MO2Controller`` es PROPIO del ritual — aislado del AppContext — y se
    memoiza para no recrearlo en reintentos y mantener un tracking de PIDs
    estable entre corridas (review Codex #305 C2): el runner de grass llama
    ``close_game()`` entre relanzamientos, y compartir el tracking con el
    controller del AppContext mataría el Skyrim que el usuario lanzó con la
    tool normal.
    """

    def __init__(
        self,
        *,
        path_resolver: PathResolutionService,
        path_validator: PathValidator,
        profile_name: str,
    ) -> None:
        self._path_resolver = path_resolver
        self._path_validator = path_validator
        self._profile_name = profile_name
        self._mo2_controller: MO2Controller | None = None

    def __call__(self) -> GrassRuntimeDeps | None:
        mo2_root = self._path_resolver.get_mo2_path()
        game_path = self._path_resolver.get_skyrim_path()
        if mo2_root is None or game_path is None:
            return None
        mo2 = self._resolve_mo2_controller(mo2_root)
        profile_manager = GrassProfileManager(
            mo2_root,
            self._path_validator,
            source_profile=self._profile_name,
            controller=mo2,
        )
        return GrassRuntimeDeps(
            profile_manager=profile_manager,
            mo2=mo2,
            game_path=game_path,
            overwrite_grass_dir=mo2_root / "overwrite" / "Grass",
        )

    def _resolve_mo2_controller(self, mo2_root: pathlib.Path) -> MO2Controller:
        """Devuelve el MO2Controller PROPIO del ritual, memoizado por root.

        La memoización evita construir uno nuevo por resolución, para que el
        tracking de PIDs del runner (que ``close_game`` mata entre
        relanzamientos) sobreviva entre corridas del ritual.
        """
        resolved = mo2_root.resolve()
        if self._mo2_controller is not None and self._mo2_controller.root == resolved:
            return self._mo2_controller
        self._mo2_controller = MO2Controller(resolved, self._path_validator)
        return self._mo2_controller


__all__ = ["GrassRuntimeDepsProvider"]
