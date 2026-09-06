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

from sky_claw.app.core.path_resolver import MODS_DIR_UNAVAILABLE, PathResolutionService
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

        # Separación INSTALL_ROOT vs INSTANCE_DATA_ROOT vs MODS_DIR (Issue #557):
        # install_root es para lanzar ModOrganizer.exe / SkyrimSE.exe.
        # data_root es para profiles/ y overwrite/ (degrada a install_root si no hay instancia separada).
        data_root_candidate = self._path_resolver.get_mo2_instance_data_root()
        data_root = data_root_candidate if isinstance(data_root_candidate, pathlib.Path) else mo2_root

        # Contrato write-safe: Grass escribe en mods/ (config mod) y overwrite/.
        # get_mo2_mods_path_para_destino() falla cerrado con RuntimeError si la
        # instancia declaró mod_directory y no es resoluble (nunca inventa <data>/mods).
        mods_dir_candidate: object = self._path_resolver.get_mo2_mods_path_para_destino()
        if mods_dir_candidate is MODS_DIR_UNAVAILABLE:
            raise RuntimeError(
                "MODS_DIR declarado pero no disponible; GrassRuntimeDepsProvider rehúsa operar con mods_dir corrupto."
            )
        mods_dir = mods_dir_candidate if isinstance(mods_dir_candidate, pathlib.Path) else None
        if mods_dir is None:
            mods_dir = data_root / "mods"

        mo2 = self._resolve_mo2_controller(
            install_root=mo2_root,
            data_root=data_root,
            mods_dir=mods_dir,
        )
        profile_manager = GrassProfileManager(
            install_root=mo2_root,
            data_root=data_root,
            mods_dir=mods_dir,
            path_validator=self._path_validator,
            source_profile=self._profile_name,
            controller=mo2,
        )
        return GrassRuntimeDeps(
            profile_manager=profile_manager,
            mo2=mo2,
            game_path=game_path,
            overwrite_grass_dir=data_root / "overwrite" / "Grass",
        )

    def _resolve_mo2_controller(
        self,
        *,
        install_root: pathlib.Path,
        data_root: pathlib.Path,
        mods_dir: pathlib.Path,
    ) -> MO2Controller:
        """Devuelve el MO2Controller PROPIO del ritual, memoizado por roots.

        La memoización evita construir uno nuevo por resolución, para que el
        tracking de PIDs del runner (que ``close_game`` mata entre
        relanzamientos) sobreviva entre corridas del ritual.
        """
        resolved_install = install_root.resolve()
        resolved_data = data_root.resolve()
        resolved_mods = mods_dir.resolve()
        if (
            self._mo2_controller is not None
            and self._mo2_controller.install_root == resolved_install
            and self._mo2_controller.data_root == resolved_data
            and self._mo2_controller.mods_dir == resolved_mods
        ):
            return self._mo2_controller
        self._mo2_controller = MO2Controller(
            resolved_install,
            self._path_validator,
            data_root=resolved_data,
            mods_dir=resolved_mods,
        )
        return self._mo2_controller


__all__ = ["GrassRuntimeDepsProvider"]
