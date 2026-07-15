"""GrassProfileManager — perfil MO2 dedicado + mod de config (PR-3 grass cache).

Fase B del Stage 8 del SOP (No Grass In Objects). En lugar de mutar el perfil
ACTIVO del usuario y sus INIs — y depender de un rollback que puede fallar (la
mitad de la matriz de riesgos de los planes externos vive ahí) — se clona el
perfil a uno **dedicado y lanzable** (``profiles/SkyClaw-GrassCache``) y todo el
ritual opera sobre esa copia:

* el **mod de configuración** (``GrassControl.ini`` con los worldspaces de Fase A
  + ``SSEDisplayTweaks.ini`` con resolución marginal), habilitado **solo** en el
  clon con máxima prioridad VFS;
* los **toggles** de mods conflictivos (ENB/Community Shaders/etc.), **solo** en
  el clon.

**El perfil real y sus INIs no se tocan nunca.** El rollback de esta fase es
simplemente ``teardown()``: borrar el clon + el mod de config.

A diferencia de :class:`~sky_claw.local.mo2.profile_sandbox.ProfileSandbox` (que
esconde el clon fuera de ``profiles/`` para que MO2 no lo liste), acá el clon
**debe** vivir en ``profiles/`` porque el crash-loop de Fase C lo lanza con
``MO2Controller.launch_game(profile="SkyClaw-GrassCache")``.

Reutiliza infraestructura existente: :class:`MO2Controller` (modlist atómico),
:class:`IniEditor` (escritura byte-fiel), :class:`PathValidator` (sandbox) y la
política ``SandboxSymlinkError`` de ``profile_sandbox``.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import pathlib
import shutil
from typing import TYPE_CHECKING

from sky_claw.antigravity.security.path_validator import assert_safe_component
from sky_claw.local.mo2.ini_editor import IniEditor
from sky_claw.local.mo2.profile_sandbox import (
    ProfileNotFoundError,
    SandboxSymlinkError,
    _rmtree_force,
)
from sky_claw.local.mo2.vfs import MO2Controller

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sky_claw.antigravity.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

#: Perfil MO2 dedicado para el ritual (visible/lanzable, en ``profiles/``).
_DEFAULT_CLONE_PROFILE = "SkyClaw-GrassCache"
#: Mod de configuración creado en ``mods/``.
_DEFAULT_CONFIG_MOD = "SkyClaw - Grass Precache Config"

#: Ruta del ``GrassControl.ini`` de NGIO-NG dentro del árbol de un mod MO2.
_GRASSCONTROL_REL = pathlib.PurePosixPath("SKSE/Plugins/GrassControl.ini")
#: Ruta del ``SSEDisplayTweaks.ini`` dentro del árbol de un mod MO2.
_SSEDISPLAYTWEAKS_REL = pathlib.PurePosixPath("SKSE/Plugins/SSEDisplayTweaks.ini")

#: Flags planos de ``GrassControl.ini`` (sintaxis NGIO-NG, sin secciones) para
#: la fase de GENERACIÓN. El README de NGIO-NG documenta arrancar el precache con
#: ``Use-grass-cache = true`` **y** ``Only-load-from-cache = true`` (este último
#: es también el estado de uso normal posterior: cargar solo del cache generado).
_DEFAULT_GRASSCONTROL: dict[str, str] = {
    "Use-grass-cache": "True",
    "Only-load-from-cache": "True",
}

#: Clave NGIO-NG que limita el precache a una lista de worldspaces (los que la
#: Fase A detectó con pasto). Ojo: es con guiones y semicolon-delimited, no la
#: forma legacy ``OnlyPregenerateWorldSpaces`` space-joined (GrassControl.toml
#: oficial de NGIO-NG) — la forma legacy la ignora NGIO-NG y escanearía TODO.
_WORLDSPACES_KEY = "Only-pregenerate-world-spaces"

#: ``SSEDisplayTweaks.ini`` (con secciones): ventana marginal para acelerar los
#: micro-lanzamientos entre CTDs y bajar la presión de VRAM durante el precache.
#: 800x400 es la resolución que exige el SOP §2.8 para tolerar los scans de celda.
_DEFAULT_SSEDISPLAYTWEAKS: dict[str, dict[str, str]] = {
    "Render": {
        "Resolution": "800x400",
        "Fullscreen": "false",
        "Borderless": "true",
        "BorderlessUpscale": "false",
    },
}


class GrassProfileError(Exception):
    """Error de la gestión del perfil/mod de grass (precondición o colisión)."""


class GrassProfileManager:
    """Clona un perfil MO2 dedicado y le arma el mod de config del precache.

    Args:
        mo2_root: Raíz de la instancia portable de MO2.
        path_validator: Sandbox de rutas (todas las escrituras se validan).
        source_profile: Perfil a clonar (default ``"Default"``).
        clone_profile: Nombre del perfil dedicado (default
            ``"SkyClaw-GrassCache"``).
        config_mod_name: Nombre del mod de configuración (default
            ``"SkyClaw - Grass Precache Config"``).
        controller: :class:`MO2Controller` inyectable (default: uno nuevo sobre
            ``mo2_root``/``path_validator``).
        ini_editor: :class:`IniEditor` inyectable (default: uno nuevo).
    """

    def __init__(
        self,
        mo2_root: pathlib.Path,
        path_validator: PathValidator,
        *,
        source_profile: str = "Default",
        clone_profile: str = _DEFAULT_CLONE_PROFILE,
        config_mod_name: str = _DEFAULT_CONFIG_MOD,
        controller: MO2Controller | None = None,
        ini_editor: IniEditor | None = None,
    ) -> None:
        assert_safe_component(source_profile, field="source_profile")
        assert_safe_component(clone_profile, field="clone_profile")
        assert_safe_component(config_mod_name, field="config_mod_name")
        self._root = mo2_root.resolve()
        self._validator = path_validator
        self._source_profile = source_profile
        self._clone_profile = clone_profile
        self._config_mod_name = config_mod_name
        self._controller = controller or MO2Controller(mo2_root, path_validator)
        self._ini = ini_editor or IniEditor()

    @property
    def clone_profile(self) -> str:
        """Nombre del perfil dedicado (para lanzar el juego en Fase C)."""
        return self._clone_profile

    # ------------------------------------------------------------------
    # create_clone_profile
    # ------------------------------------------------------------------

    async def create_clone_profile(self) -> pathlib.Path:
        """Clona el perfil real a ``profiles/<clone_profile>`` byte-fiel.

        Copia byte-idéntico (BOM/CRLF intactos, ``copy2``) todo el árbol del
        perfil de origen. Rechaza symlinks (fail-closed) antes de copiar nada.

        Returns:
            Ruta del perfil clonado.

        Raises:
            ProfileNotFoundError: Si el perfil de origen no existe.
            GrassProfileError: Si el clon ya existe (fail-closed: no se pisa un
                ritual en curso; usar ``teardown`` primero).
            SandboxSymlinkError: Si el árbol de origen contiene symlinks.
        """
        source = self._validator.validate(self._root / "profiles" / self._source_profile, strict_symlink=False)
        dest = self._validator.validate(self._root / "profiles" / self._clone_profile, strict_symlink=False)
        return await asyncio.to_thread(self._clone_sync, source, dest)

    def _clone_sync(self, source: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
        if not source.is_dir():
            raise ProfileNotFoundError(f"El perfil de origen '{self._source_profile}' no existe en {source}.")
        if dest.exists():
            raise GrassProfileError(
                f"El perfil clon '{self._clone_profile}' ya existe en {dest}: "
                "corré teardown() antes de reclonar (no se pisa un ritual en curso)."
            )
        _reject_symlinks(source)
        # copy2 preserva bytes (y mtime): el modlist/plugins/INIs quedan
        # byte-idénticos, BOM UTF-8 y CRLF incluidos.
        shutil.copytree(source, dest, copy_function=shutil.copy2)
        logger.info("Perfil '%s' clonado a '%s' en %s", self._source_profile, self._clone_profile, dest)
        return dest

    # ------------------------------------------------------------------
    # build_config_mod
    # ------------------------------------------------------------------

    async def build_config_mod(
        self,
        worldspaces: Sequence[str],
        *,
        params: Mapping[str, str] | None = None,
    ) -> pathlib.Path:
        """Crea el mod de config y lo habilita en el clon con máxima prioridad.

        Escribe ``SKSE/Plugins/GrassControl.ini`` (flags de generación +
        ``Only-pregenerate-world-spaces`` con los worldspaces de Fase A entre
        comillas dobles, separados por ``;``), ``SKSE/Plugins/SSEDisplayTweaks.ini``
        (resolución marginal) y ``meta.ini``; luego agrega el mod al ``modlist.txt`` del
        clon — en MO2 la última línea es la de mayor prioridad, así que el
        ``GrassControl.ini`` del mod gana los conflictos.

        Args:
            worldspaces: EditorIDs de los worldspaces con pasto (Fase A).
            params: Overrides/extras planos de ``GrassControl.ini`` (pisan los
                defaults, agregan claves nuevas).

        Returns:
            Ruta del directorio del mod creado.

        Raises:
            GrassProfileError: Si el clon todavía no existe (fail-closed).
        """
        clon = self._root / "profiles" / self._clone_profile
        if not clon.is_dir():
            raise GrassProfileError(
                f"El perfil clon '{self._clone_profile}' no existe: llamá create_clone_profile() primero."
            )
        # Fail-closed si el destino ya existe como symlink/junction: validate()
        # resuelve el symlink y _rmtree_force borraría su TARGET (otro mod o
        # perfil del árbol MO2), no el enlace. Se chequea el path CRUDO — el
        # resuelto nunca reporta is_symlink (review Codex #284).
        raw_mod_dir = self._root / "mods" / self._config_mod_name
        if raw_mod_dir.is_symlink():
            raise GrassProfileError(
                f"El mod de config '{self._config_mod_name}' ya existe como symlink ({raw_mod_dir}): "
                "fail-closed para no borrar el árbol al que apunta."
            )
        mod_dir = self._validator.validate(raw_mod_dir, strict_symlink=False)

        grass_values = {**_DEFAULT_GRASSCONTROL, _WORLDSPACES_KEY: _format_worldspaces(worldspaces)}
        if params:
            grass_values.update(params)

        await asyncio.to_thread(self._scaffold_mod_sync, mod_dir)
        await self._write_grasscontrol(mod_dir, grass_values)
        await self._write_ssedisplaytweaks(mod_dir)
        # Máxima prioridad VFS: add_mod_to_modlist hace append, y en modlist.txt
        # la última línea es el mod de mayor prioridad.
        await self._controller.add_mod_to_modlist(self._config_mod_name, profile=self._clone_profile)
        logger.info("Mod de config '%s' creado en %s y habilitado en el clon", self._config_mod_name, mod_dir)
        return mod_dir

    def _scaffold_mod_sync(self, mod_dir: pathlib.Path) -> None:
        """Directorio del mod limpio + ``meta.ini`` (idempotente: recrea si existía)."""
        if mod_dir.exists():
            _rmtree_force(mod_dir)
        (mod_dir / "SKSE" / "Plugins").mkdir(parents=True)
        self._write_meta_ini(mod_dir)

    def _write_meta_ini(self, mod_dir: pathlib.Path) -> None:
        config = configparser.ConfigParser()
        config["General"] = {
            "modid": "0",
            "version": "1.0.0",
            "name": self._config_mod_name,
            "comments": "Generado por Sky-Claw para el precache de grass (NGIO).",
        }
        with (mod_dir / "meta.ini").open("w", encoding="utf-8") as fh:
            config.write(fh)

    async def _write_grasscontrol(self, mod_dir: pathlib.Path, values: Mapping[str, str]) -> None:
        path = mod_dir / _GRASSCONTROL_REL
        # Sintaxis plana NGIO-NG (sin secciones): section=None en el IniEditor.
        for key, value in values.items():
            await self._ini.set(path, key, value)

    async def _write_ssedisplaytweaks(self, mod_dir: pathlib.Path) -> None:
        path = mod_dir / _SSEDISPLAYTWEAKS_REL
        for section, entries in _DEFAULT_SSEDISPLAYTWEAKS.items():
            for key, value in entries.items():
                await self._ini.set(path, key, value, section=section)

    # ------------------------------------------------------------------
    # disable_conflicting_mods
    # ------------------------------------------------------------------

    async def disable_conflicting_mods(self, mod_names: Sequence[str]) -> None:
        """Desactiva *mod_names* **solo** en el clon (el perfil real no se toca).

        Raises:
            GrassProfileError: Si el clon todavía no existe (fail-closed).
        """
        if not (self._root / "profiles" / self._clone_profile).is_dir():
            raise GrassProfileError(
                f"El perfil clon '{self._clone_profile}' no existe: llamá create_clone_profile() primero."
            )
        for mod_name in mod_names:
            await self._controller.toggle_mod_in_modlist(mod_name, profile=self._clone_profile, enable=False)

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    async def teardown(self) -> list[pathlib.Path]:
        """Borra el perfil clon y el mod de config (idempotente).

        Es el rollback de la Fase B: como el ritual jamás tocó el perfil real,
        deshacer todo es simplemente eliminar el clon y el mod. No falla si
        alguno (o ambos) no existen.

        ``_rmtree_force`` LANZA si un borrado no completa (p.ej. un SkyrimSE
        huérfano mantiene un handle abierto en Windows), así que cada objetivo
        se intenta por separado — un fallo en el clon no impide intentar el mod
        (análisis hostil §1.6) — y se devuelven los paths que no se pudieron
        borrar para que el caller los exponga en vez de tragarlos.

        Returns:
            Lista de rutas que NO se pudieron eliminar (vacía en éxito total).
        """
        objetivos = [
            self._root / "profiles" / self._clone_profile,
            self._root / "mods" / self._config_mod_name,
        ]
        fallidos: list[pathlib.Path] = []
        for objetivo in objetivos:
            try:
                await asyncio.to_thread(_rmtree_force, objetivo)
            except Exception:  # noqa: BLE001 — un objetivo trabado no debe frenar al otro
                logger.warning("Teardown del ritual grass: no se pudo borrar %s", objetivo, exc_info=True)
                fallidos.append(objetivo)
        if not fallidos:
            logger.info(
                "Teardown del ritual grass: clon '%s' y mod '%s' eliminados",
                self._clone_profile,
                self._config_mod_name,
            )
        return fallidos


def _format_worldspaces(worldspaces: Sequence[str]) -> str:
    """``Only-pregenerate-world-spaces`` de NGIO-NG: nombres entre comillas, separados por ``;``.

    El separador es semicolon (no espacio): así lo define y ejemplifica el
    ``GrassControl.toml`` oficial de NGIO-NG (``"WorldA;WorldB;WorldC"``).
    """
    return '"' + ";".join(worldspaces) + '"'


def _reject_symlinks(root: pathlib.Path) -> None:
    """Corta con :class:`SandboxSymlinkError` si hay symlinks bajo ``root``.

    Misma política que ``ProfileSandbox``: un symlink podría sacar la copia
    fuera del árbol MO2 (leer o escribir contenido externo).
    """
    for p in root.rglob("*"):
        if p.is_symlink():
            raise SandboxSymlinkError(
                f"Symlink detectado en el perfil a clonar: {p}. No se sigue (podría apuntar fuera del árbol)."
            )


__all__ = ["GrassProfileError", "GrassProfileManager"]
