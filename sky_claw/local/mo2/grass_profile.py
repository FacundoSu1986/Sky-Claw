"""GrassProfileManager — perfil MO2 dedicado + mod de config (PR-3 grass cache).

Fase B del Stage 8 del SOP (No Grass In Objects). En lugar de mutar el perfil
ACTIVO del usuario y sus INIs — y depender de un rollback que puede fallar (la
mitad de la matriz de riesgos de los planes externos vive ahí) — se clona el
perfil a uno **dedicado y lanzable** (``profiles/SkyClaw-GrassCache``) y todo el
ritual opera sobre esa copia:

* el **mod de configuración** (``GrassControl.ini`` con los worldspaces de Fase A
  + ``SSEDisplayTweaks.ini`` con resolución marginal), habilitado **solo** en el
  clon con máxima prioridad entre mods regulares; antes de habilitarlo se valida
  que ``overwrite`` no imponga bytes distintos (``overwrite`` permanece por encima);
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
import os
import pathlib
import shutil
import stat
from typing import TYPE_CHECKING

from sky_claw.app.security.links import (
    link_kind_and_identity_or_raise_with_retry,
    link_kind_or_raise_with_retry,
    reject_unclassified_reparse_point,
    same_file_identity,
)
from sky_claw.app.security.path_validator import assert_safe_component
from sky_claw.local.mo2.ini_editor import IniEditor
from sky_claw.local.mo2.profile_sandbox import (
    ProfileNotFoundError,
    SandboxSymlinkError,
)
from sky_claw.local.mo2.vfs import MO2Controller, _rmtree_force

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sky_claw.app.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

#: Perfil MO2 dedicado para el ritual (visible/lanzable, en ``profiles/``).
_DEFAULT_CLONE_PROFILE = "SkyClaw-GrassCache"
#: Mod de configuración creado en ``mods/``.
_DEFAULT_CONFIG_MOD = "SkyClaw - Grass Precache Config"

#: Ruta del ``GrassControl.ini`` de NGIO-NG dentro del árbol de un mod MO2.
#: El plugin lee exactamente ``Data/SKSE/Plugins/GrassControl.ini`` (verificado
#: en el source oficial: ``include/GrassControl/Config.h``,
#: ``iniPath = "Data/SKSE/Plugins/GrassControl.ini"``). El ``GrassControl.toml``
#: del repo es material de referencia; el runtime lee el ``.ini``.
_GRASSCONTROL_REL = pathlib.PurePosixPath("SKSE/Plugins/GrassControl.ini")
#: Sección que NGIO-NG exige para las claves de grass. El plugin parsea con
#: ``CSimpleIniA`` y lee ``ReadBoolSetting(ini, "GrassConfig", <clave>, ...)``
#: (``src/Config.cpp``): sin este header las claves caen en la sección vacía y
#: el plugin las IGNORA (usaría sus defaults → escanearía TODOS los worldspaces).
_GRASSCONTROL_SECTION = "GrassConfig"
#: Ruta del ``SSEDisplayTweaks.ini`` dentro del árbol de un mod MO2.
_SSEDISPLAYTWEAKS_REL = pathlib.PurePosixPath("SKSE/Plugins/SSEDisplayTweaks.ini")

#: Claves de ``[GrassConfig]`` (ver ``_GRASSCONTROL_SECTION``) para la fase de
#: GENERACIÓN. El README de NGIO-NG documenta arrancar el precache con
#: ``Use-grass-cache = true`` **y** ``Only-load-from-cache = true`` (este último
#: es también el estado de uso normal posterior: cargar solo del cache generado).
_DEFAULT_GRASSCONTROL: dict[str, str] = {
    "Use-grass-cache": "True",
    "Only-load-from-cache": "True",
}

#: Clave de ``[GrassConfig]`` que limita el precache a una lista de worldspaces
#: (los que la Fase A detectó con pasto). Con guiones y semicolon-delimited entre
#: comillas (``"WorldA;WorldB"``) — la sintaxis exacta que parsea NGIO-NG.
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


def _reject_link_or_reparse(
    path: pathlib.Path,
    link_kind: str | None,
    info: os.stat_result,
) -> None:
    """Rechaza enlaces conocidos y cualquier reparse tag no clasificado."""
    if link_kind is not None:
        raise OSError(f"el componente '{path}' es un {link_kind}")
    reject_unclassified_reparse_point(path, info)


def _inspect_real_directory_chain(root: pathlib.Path) -> list[tuple[pathlib.Path, os.stat_result]]:
    """Captura cada directorio de *root* con lstat, sin cruzar enlaces/reparse points."""
    if not root.is_absolute() or ".." in root.parts:
        raise OSError(f"la raíz '{root}' no es una ruta absoluta segura")

    current = pathlib.Path(root.anchor)
    captured: list[tuple[pathlib.Path, os.stat_result]] = []
    for component in ("", *root.parts[1:]):
        if component:
            current = current / component
        link_kind, info = link_kind_and_identity_or_raise_with_retry(current)
        if info is None:
            raise FileNotFoundError(f"el directorio '{current}' no existe")
        _reject_link_or_reparse(current, link_kind, info)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"el componente '{current}' no es un directorio")
        captured.append((current, info))
    return captured


def _revalidate_real_directories(captured: list[tuple[pathlib.Path, os.stat_result]]) -> None:
    """Confirma que los directorios capturados siguen siendo los mismos y reales."""
    for path, before in reversed(captured):
        link_kind, after = link_kind_and_identity_or_raise_with_retry(path)
        if after is None:
            raise OSError(f"el directorio '{path}' desapareció durante la inspección")
        _reject_link_or_reparse(path, link_kind, after)
        if not stat.S_ISDIR(after.st_mode) or not same_file_identity(before, after):
            raise OSError(f"la identidad del directorio '{path}' cambió durante la inspección")


def _read_file_bytes_link_safe(path: pathlib.Path, expected: os.stat_result) -> bytes:
    """Lee un archivo regular y confirma la identidad del handle frente a lstat."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not same_file_identity(expected, opened):
            raise OSError(f"el archivo '{path}' cambió entre lstat y open")

        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)

        after_read = os.fstat(descriptor)
        if (
            not same_file_identity(opened, after_read)
            or opened.st_size != after_read.st_size
            or opened.st_mtime_ns != after_read.st_mtime_ns
            or opened.st_ctime_ns != after_read.st_ctime_ns
        ):
            # El metadata sólo detecta una mutación concurrente durante la
            # lectura; la identidad de contenido siempre se decide por bytes.
            raise OSError(f"el contenido del archivo '{path}' cambió durante la lectura")
        return bytes(content)
    finally:
        os.close(descriptor)


def _read_regular_file_beneath(
    root: pathlib.Path,
    relative_path: pathlib.PurePosixPath,
    *,
    allow_missing: bool,
    expected_size: int | None = None,
) -> bytes | None:
    """Lee un archivo bajo *root* tras inspeccionar y revalidar toda la cadena física.

    ``None`` significa exclusivamente que falta algún componente del path. Errores
    de permisos, I/O, enlaces, reparse points y tipos inesperados se propagan. Si
    se pasa ``expected_size``, un tamaño distinto se rechaza antes de leer; un
    tamaño igual nunca sustituye la comparación byte-exact del caller.
    """
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise OSError(f"ruta relativa insegura: {relative_path}")

    captured_dirs = _inspect_real_directory_chain(root)
    current = root
    for index, component in enumerate(relative_path.parts):
        path = current / component
        link_kind, info = link_kind_and_identity_or_raise_with_retry(path)
        if info is None:
            if not allow_missing:
                raise FileNotFoundError(f"falta el archivo generado '{path}'")
            _revalidate_real_directories(captured_dirs)
            # Re-chequea ausencia: un error transitorio de I/O no se convierte en
            # "no existe", y la segunda lectura detecta creación durante el gate.
            _link_kind, appeared = link_kind_and_identity_or_raise_with_retry(path)
            if appeared is not None:
                raise OSError(f"el path '{path}' apareció durante la inspección")
            _revalidate_real_directories(captured_dirs)
            return None

        _reject_link_or_reparse(path, link_kind, info)
        is_file = index == len(relative_path.parts) - 1
        if is_file:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"el archivo esperado '{path}' no es un archivo regular")
            if expected_size is not None and info.st_size != expected_size:
                raise OSError(f"tamaño distinto al generado: overwrite={info.st_size}, configuración={expected_size}")
            content = _read_file_bytes_link_safe(path, info)
            final_kind, final_info = link_kind_and_identity_or_raise_with_retry(path)
            if final_info is None:
                raise OSError(f"el archivo '{path}' desapareció durante la lectura")
            _reject_link_or_reparse(path, final_kind, final_info)
            if not stat.S_ISREG(final_info.st_mode) or not same_file_identity(info, final_info):
                raise OSError(f"la identidad del archivo '{path}' cambió durante la lectura")
            _revalidate_real_directories(captured_dirs)
            return content

        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"el componente intermedio '{path}' no es un directorio")
        captured_dirs.append((path, info))
        current = path

    raise OSError(f"la ruta relativa '{relative_path}' no identifica un archivo")


class GrassProfileManager:
    """Clona un perfil MO2 dedicado y le arma el mod de config del precache.

    Args:
        mo2_root: Raíz de la instancia portable de MO2 (compatibilidad legacy).
        path_validator: Sandbox de rutas (todas las escrituras se validan).
        install_root: Directorio de instalación de MO2 (ModOrganizer.exe).
        data_root: Directorio de datos de la instancia (profiles/, overwrite/).
        mods_dir: Directorio donde residen los mods instalados.
        source_profile: Perfil a clonar (default ``"Default"``).
        clone_profile: Nombre del perfil dedicado (default
            ``"SkyClaw-GrassCache"``).
        config_mod_name: Nombre del mod de configuración (default
            ``"SkyClaw - Grass Precache Config"``).
        controller: :class:`MO2Controller` inyectable (default: uno nuevo sobre
            raíces / ``path_validator``).
        ini_editor: :class:`IniEditor` inyectable (default: uno nuevo).
    """

    def __init__(
        self,
        mo2_root: pathlib.Path | None = None,
        path_validator: PathValidator | None = None,
        *,
        install_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        source_profile: str = "Default",
        clone_profile: str = _DEFAULT_CLONE_PROFILE,
        config_mod_name: str = _DEFAULT_CONFIG_MOD,
        controller: MO2Controller | None = None,
        ini_editor: IniEditor | None = None,
    ) -> None:
        assert_safe_component(source_profile, field="source_profile")
        assert_safe_component(clone_profile, field="clone_profile")
        assert_safe_component(config_mod_name, field="config_mod_name")

        if path_validator is None:
            raise ValueError("path_validator es obligatorio")

        if controller is not None:
            if install_root is not None and install_root.resolve() != controller.install_root.resolve():
                raise ValueError(
                    f"install_root explícito ({install_root}) diverge del controller provisto ({controller.install_root})"
                )
            if data_root is not None and data_root.resolve() != controller.data_root.resolve():
                raise ValueError(
                    f"data_root explícito ({data_root}) diverge del controller provisto ({controller.data_root})"
                )
            if mods_dir is not None and mods_dir.resolve() != controller.mods_dir.resolve():
                raise ValueError(
                    f"mods_dir explícito ({mods_dir}) diverge del controller provisto ({controller.mods_dir})"
                )
            if mo2_root is not None and mo2_root.resolve() != controller.data_root.resolve():
                raise ValueError(
                    f"mo2_root explícito ({mo2_root}) diverge del controller provisto ({controller.data_root})"
                )
            self._controller = controller
            self._install_root = controller.install_root
            self._data_root = controller.data_root
            self._mods_dir = controller.mods_dir
        else:
            tiene_alguno_explicito = install_root is not None or data_root is not None or mods_dir is not None
            if tiene_alguno_explicito:
                if install_root is None or data_root is None or mods_dir is None:
                    raise ValueError(
                        "GrassProfileManager en modo explícito exige install_root, data_root y mods_dir completos."
                    )
                if mo2_root is not None and mo2_root.resolve() != data_root.resolve():
                    raise ValueError("mo2_root diverge de data_root explícito")
                self._install_root = install_root.resolve()
                self._data_root = data_root.resolve()
                self._mods_dir = mods_dir.resolve()
                self._controller = MO2Controller(
                    install_root=self._install_root,
                    data_root=self._data_root,
                    mods_dir=self._mods_dir,
                    path_validator=path_validator,
                )
            else:
                if mo2_root is None:
                    raise ValueError(
                        "GrassProfileManager exige install_root, data_root y mods_dir, o mo2_root + path_validator."
                    )
                resolved_legacy = mo2_root.resolve()
                self._install_root = resolved_legacy
                self._data_root = resolved_legacy
                self._controller = MO2Controller(resolved_legacy, path_validator)
                self._mods_dir = self._controller.mods_dir

        self._root = self._data_root
        self._validator = path_validator
        self._source_profile = source_profile
        self._clone_profile = clone_profile
        self._config_mod_name = config_mod_name
        self._ini = ini_editor or IniEditor()

    @property
    def install_root(self) -> pathlib.Path:
        """Ruta de instalación de MO2 (donde reside ModOrganizer.exe)."""
        return self._install_root

    @property
    def data_root(self) -> pathlib.Path:
        """Ruta de datos de la instancia MO2 (donde residen profiles/ y overwrite/)."""
        return self._data_root

    @property
    def mods_dir(self) -> pathlib.Path:
        """Directorio donde residen los mods instalados."""
        return self._mods_dir

    @property
    def root(self) -> pathlib.Path:
        """Alias legacy para la raíz de datos."""
        return self._data_root

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
        source = self._validator.validate(self._data_root / "profiles" / self._source_profile, strict_symlink=False)
        dest = self._validator.validate(self._data_root / "profiles" / self._clone_profile, strict_symlink=False)
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
        """Crea el mod de config y lo habilita con máxima prioridad entre mods regulares.

        Escribe ``SKSE/Plugins/GrassControl.ini`` (flags de generación +
        ``Only-pregenerate-world-spaces`` con los worldspaces de Fase A entre
        comillas dobles, separados por ``;``), ``SKSE/Plugins/SSEDisplayTweaks.ini``
        (resolución marginal) y ``meta.ini``. Antes de tocar ``modlist.txt``, verifica
        que ``overwrite`` no contenga versiones diferentes de ninguno de los dos
        archivos: en MO2, ``overwrite`` está por encima de todos los mods regulares.
        Ausencia o igualdad byte-exacta se acepta; conflictos, paths ilegibles o
        enlaces/reparse points bloquean fail-closed. Solo entonces inserta el mod
        como primera entrada física (máxima prioridad ENTRE mods regulares).

        Si el gate falla, el directorio generado puede quedar como staging local,
        pero el modlist del clon no se modifica.

        Args:
            worldspaces: EditorIDs de los worldspaces con pasto (Fase A).
            params: Overrides/extras planos de ``GrassControl.ini`` (pisan los
                defaults, agregan claves nuevas).

        Returns:
            Ruta del directorio del mod creado.

        Raises:
            GrassProfileError: Si el clon todavía no existe, overwrite contiene
                bytes distintos o no se puede inspeccionar con seguridad. Ante un
                fallo del gate, la carpeta del mod puede quedar staged localmente,
                pero nunca se agrega ni reordena en ``modlist.txt``.
        """
        clon = self._data_root / "profiles" / self._clone_profile
        if not clon.is_dir():
            raise GrassProfileError(
                f"El perfil clon '{self._clone_profile}' no existe: llamá create_clone_profile() primero."
            )
        # Fail-closed si el destino ya existe como symlink/junction: validate()
        # resuelve el enlace y _rmtree_force borraría su TARGET (otro mod o
        # perfil del árbol MO2), no el enlace. Se chequea el path CRUDO — el
        # resuelto nunca reporta is_symlink (review Codex #284). ``link_kind`` y
        # no ``is_symlink()``: éste es ciego a los junctions de Windows
        # (``os.path.islink()`` da False para un ``IO_REPARSE_TAG_MOUNT_POINT``),
        # el mismo agujero que ``profile_sandbox`` y ``_dir_rollback`` tenían
        # antes de consolidar en ``links.py`` — quedaba sin el fix acá.
        raw_mod_dir = self._mods_dir / self._config_mod_name
        try:
            tipo_de_enlace = await asyncio.to_thread(link_kind_or_raise_with_retry, raw_mod_dir)
        except OSError as exc:
            raise GrassProfileError(f"No se pudo inspeccionar el mod de config '{raw_mod_dir}': {exc}") from exc
        if tipo_de_enlace is not None:
            raise GrassProfileError(
                f"El mod de config '{self._config_mod_name}' ya existe como enlace "
                f"({tipo_de_enlace}, {raw_mod_dir}): fail-closed para no borrar el árbol al que apunta."
            )
        mod_dir = self._validator.validate(raw_mod_dir, strict_symlink=False)

        grass_values = {**_DEFAULT_GRASSCONTROL, _WORLDSPACES_KEY: _format_worldspaces(worldspaces)}
        if params:
            grass_values.update(params)

        await asyncio.to_thread(self._scaffold_mod_sync, mod_dir)
        await self._write_grasscontrol(mod_dir, grass_values)
        await self._write_ssedisplaytweaks(mod_dir)
        # ``overwrite`` está por encima de los mods regulares. Validar ambos
        # outputs antes del writer: un conflicto nunca debe dejar el config mod
        # agregado o reordenado en ``modlist.txt``.
        await asyncio.to_thread(self._verify_overwrite_compatibility, mod_dir)
        # Registrar y habilitar el mod de configuración con máxima prioridad
        # ENTRE mods regulares. No es una garantía absoluta del overlay: el gate
        # previo demuestra que ``overwrite`` está ausente o entrega bytes iguales.
        await self._controller.add_mod_to_modlist(
            self._config_mod_name,
            profile=self._clone_profile,
            highest_priority=True,
        )
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
        # NGIO-NG lee las claves bajo [GrassConfig] (CSimpleIniA, src/Config.cpp):
        # deben escribirse EN esa sección, no planas, o el plugin las ignora.
        for key, value in values.items():
            await self._ini.set(path, key, value, section=_GRASSCONTROL_SECTION)

    async def _write_ssedisplaytweaks(self, mod_dir: pathlib.Path) -> None:
        path = mod_dir / _SSEDISPLAYTWEAKS_REL
        for section, entries in _DEFAULT_SSEDISPLAYTWEAKS.items():
            for key, value in entries.items():
                await self._ini.set(path, key, value, section=section)

    def _verify_overwrite_compatibility(self, mod_dir: pathlib.Path) -> None:
        """Exige que overwrite esté ausente o byte-idéntico para cada output generado."""
        for relative_path in (_GRASSCONTROL_REL, _SSEDISPLAYTWEAKS_REL):
            relative_name = relative_path.as_posix()
            generated_path = mod_dir.joinpath(*relative_path.parts)
            try:
                generated_bytes = _read_regular_file_beneath(mod_dir, relative_path, allow_missing=False)
            except (OSError, ValueError) as exc:
                raise GrassProfileError(
                    f"No se pudo inspeccionar el archivo generado {relative_name} en {generated_path}: {exc}"
                ) from exc
            if generated_bytes is None:
                raise GrassProfileError(
                    f"No se pudo inspeccionar el archivo generado {relative_name} en {generated_path}: falta el archivo."
                )

            overwrite_relative = pathlib.PurePosixPath("overwrite").joinpath(relative_path)
            overwrite_path = self._data_root.joinpath(*overwrite_relative.parts)
            try:
                overwrite_bytes = _read_regular_file_beneath(
                    self._data_root,
                    overwrite_relative,
                    allow_missing=True,
                    expected_size=len(generated_bytes),
                )
            except (OSError, ValueError) as exc:
                raise GrassProfileError(
                    f"No se pudo demostrar que el archivo generado {relative_name} será efectivo: "
                    f"no se pudo inspeccionar overwrite en {overwrite_path}: {exc}"
                ) from exc

            if overwrite_bytes is not None and overwrite_bytes != generated_bytes:
                raise GrassProfileError(
                    f"El archivo generado {relative_name} no será efectivo: "
                    f"overwrite contiene bytes diferentes en {overwrite_path}."
                )

    # ------------------------------------------------------------------
    # disable_conflicting_mods
    # ------------------------------------------------------------------

    async def disable_conflicting_mods(self, mod_names: Sequence[str]) -> None:
        """Desactiva *mod_names* **solo** en el clon (el perfil real no se toca).

        Raises:
            GrassProfileError: Si el clon todavía no existe (fail-closed).
        """
        if not (self._data_root / "profiles" / self._clone_profile).is_dir():
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

        **Fail-closed ante un objetivo enlazado**, igual que ``build_config_mod``
        (:meth:`create_config_mod`): los dos objetivos cuelgan del árbol MO2 del
        usuario, y ``mods/<config_mod>`` es LITERALMENTE la misma ruta que aquel
        método ya protege. Tener el guard sólo en el camino de creación y no en
        el de borrado era la asimetría entre hermanos dentro de un mismo archivo
        que este repo nombra como su defecto #1. Un enlace se reporta como
        fallido —no se toca— para que el operador decida.

        Returns:
            Lista de rutas que NO se pudieron eliminar (vacía en éxito total).
        """
        objetivos = [
            self._data_root / "profiles" / self._clone_profile,
            self._mods_dir / self._config_mod_name,
        ]
        fallidos: list[pathlib.Path] = []
        for objetivo in objetivos:
            try:
                tipo_de_enlace = await asyncio.to_thread(link_kind_or_raise_with_retry, objetivo)
            except OSError as exc:
                logger.warning("Teardown del ritual grass: no se pudo inspeccionar '%s': %s", objetivo, exc)
                fallidos.append(objetivo)
                continue
            if tipo_de_enlace is not None:
                logger.warning(
                    "Teardown del ritual grass: '%s' es un enlace (%s) y NO se borra — "
                    "hacerlo destruiría el árbol al que apunta. Quitalo a mano si corresponde.",
                    objetivo,
                    tipo_de_enlace,
                )
                fallidos.append(objetivo)
                continue
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
