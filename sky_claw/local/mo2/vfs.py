"""MO2 Virtual File System control.

This module manages the MO2 portable instance: modlist.txt
manipulation, tool execution (LOOT CLI, SSEEdit) via the
``ModOrganizer.exe`` proxy, and profile management.

TASK-011 enhancements:
- All subprocess invocations are fully async with ``asyncio.wait_for`` timeout.
- Zombie process prevention: ``proc.kill()`` + ``proc.wait()`` on timeout.
- Blocking ``psutil`` calls wrapped in ``asyncio.to_thread``.
- WSL2 conditional path translation via :func:`translate_path_if_wsl`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import uuid
from typing import TYPE_CHECKING, Any

import aiofiles
import psutil

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sky_claw.app.security.path_validator import PathValidator

from sky_claw.app.security.links import link_kind_or_raise_with_retry, rmtree_link_aware
from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component

logger = logging.getLogger(__name__)


def _rmtree_force(path: pathlib.Path) -> None:
    """Borra *path* recursivamente, limpiando el read-only de Windows.

    Los mods extraídos y los ``.git`` traen archivos de sólo-lectura que hacen
    fallar el borrado; ``limpiar_readonly=True`` suma el bit de escritura y
    reintenta **por entrada**, sólo ante el fallo.

    Delega en :func:`~sky_claw.app.security.links.rmtree_link_aware` en vez de
    usar ``shutil.rmtree``: éste es ciego a los junctions de Windows en la raíz
    y en cualquier nivel anidado, así que atravesaba el reparse point y borraba
    el árbol destino. Este helper además estaba **duplicado** en
    ``profile_sandbox.py`` (su propio docstring lo declaraba), y su limpieza de
    read-only usaba un ``os.walk`` previo que desciende por junctions — llegaba
    a chmodear el árbol ajeno antes de borrarlo.

    A diferencia de ``rmtree(ignore_errors=True)``, nunca deja un directorio a
    medio borrar en silencio: un fallo que persiste tras el reintento se propaga.
    """
    rmtree_link_aware(path, limpiar_readonly=True)


# TASK-011: Default timeout for game-launch *spawn* verification (seconds).
# ModOrganizer.exe is a long-running GUI process; we only verify that the
# OS registered the PID, we do NOT wait for the process to finish.
DEFAULT_SPAWN_TIMEOUT = 5


async def _write_modlist_atomic(path: pathlib.Path, lines: list[str]) -> None:
    """Write *lines* to *path* with UTF-8 BOM, using an atomic tmp->rename swap.

    Each line is normalised to end with ``\\n`` so MO2 is happy on all
    platforms.  The file is first written to a uniquely-named temporary
    file with ``encoding="utf-8-sig"`` (which prepends the BOM
    automatically), then renamed over the original so the swap is atomic.
    """
    tmp: pathlib.Path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    normalised = [(line if line.endswith("\n") else line.rstrip("\r\n") + "\n") for line in lines]
    try:
        async with aiofiles.open(tmp, mode="w", encoding="utf-8-sig") as fh:
            await fh.writelines(normalised)
        await asyncio.to_thread(os.replace, tmp, path)
    except Exception:
        # Clean up orphaned tmp if write or rename fails
        with contextlib.suppress(OSError):
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
        raise


def _clave_de_nombre_de_mod(nombre: str) -> str:
    """Identidad de un nombre de mod en ``modlist.txt``: **sin distinguir mayúsculas** (#838).

    MO2 resuelve ``mods/<Nombre>`` contra un sistema de archivos de Windows, que no
    distingue mayúsculas: ``X`` y ``x`` son el MISMO mod. ``ganadores._exigir_enabled``
    ya lo asume —rechaza el perfil que trae el mismo nombre dos veces comparando con
    ``lower()``—, así que los mutadores del modlist tienen que usar esa misma
    identidad. Con la comparación exacta que tenían, ``add_mod_to_modlist("X")``
    sobre un perfil con ``+x`` no lo reconocía como presente: agregaba un segundo
    ``+X`` y dejaba un perfil que el diagnóstico de overlay rechazaba, sin aviso.

    Se usa ``lower()`` y **no** ``casefold()``, por la misma razón que
    ``vfs_attestation._case_insensitive_key``: ``casefold`` colapsa pares que NTFS
    **no** unifica (``Straße`` / ``Strasse``) y fabricaría un «ya presente» —o un
    duplicado— que no existe. ``tests/test_mo2_ganadores.py`` congela el mismo límite
    para los duplicados del modlist.

    La función devuelve **sólo** la clave de comparación; la grafía que ya está en el
    archivo es la que se conserva al escribir (ver ``add_mod_to_modlist``).
    """
    return nombre.lower()


class ModlistParseError(Exception):
    """Raised when a modlist.txt line cannot be parsed."""


class GameLaunchTimeoutError(RuntimeError):
    """Raised when the game launch subprocess fails to appear in the process table."""

    def __init__(self, timeout: int) -> None:
        super().__init__(f"Game launch timed out after {timeout}s")
        self.timeout = timeout


class MO2Controller:
    """Controller for a portable Mod Organizer 2 instance.

    TASK-011: All external process invocations are fully async with
    timeouts and zombie prevention.  Blocking I/O is wrapped in
    ``asyncio.to_thread``.
    """

    def __init__(
        self,
        install_root: pathlib.Path | None = None,
        path_validator: PathValidator | None = None,
        launch_timeout: int = DEFAULT_SPAWN_TIMEOUT,
        *,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        mo2_root: pathlib.Path | None = None,
    ) -> None:
        if path_validator is None:
            raise ValueError("path_validator es obligatorio")

        legacy_root = mo2_root or (install_root if data_root is None and mods_dir is None else None)

        if legacy_root is not None and data_root is None and mods_dir is None:
            # Modo legacy portable: install == data, mods == data / "mods"
            if mo2_root is not None and install_root is not None and mo2_root.resolve() != install_root.resolve():
                raise ValueError("mo2_root e install_root divergen en modo legacy")
            self._install_root = legacy_root.resolve()
            self._data_root = self._install_root
            self._mods_dir = (self._data_root / "mods").resolve()
        else:
            # Modo explícito: install_root, data_root y mods_dir son OBLIGATORIOS.
            # Se rechazan combinaciones parciales sin fallback silencioso.
            if not (install_root is not None and data_root is not None and mods_dir is not None):
                raise ValueError(
                    "MO2Controller en modo explícito exige install_root, data_root y mods_dir. "
                    "No se permiten combinaciones parciales."
                )
            if mo2_root is not None and mo2_root.resolve() != install_root.resolve():
                raise ValueError("mo2_root diverge de install_root")
            self._install_root = path_validator.validate(install_root)
            self._data_root = path_validator.validate(data_root)
            self._mods_dir = path_validator.validate(mods_dir)

        self._root = self._install_root  # alias legacy para self.root
        self._validator = path_validator
        self._modlist_lock = asyncio.Lock()
        self._spawn_timeout = launch_timeout
        # M-8: ModOrganizer.exe lanzados por ESTA instancia, indexados por PID →
        # ``create_time`` (identidad estable contra reuso de PID del SO; ``None``
        # si no se pudo leer al lanzar). close_game mata SÓLO estos árboles (+
        # descendientes), no los procesos homónimos del host. Es un dict (no un
        # solo PID) para no perder un MO2 previo si se relanzó sin cerrar, y cada
        # PID se registra ANTES de verificar el spawn para no dejar huérfanos si
        # MO2 muere o la operación se cancela durante la verificación (§1.1/§1.2).
        self._launched_procs: dict[int, float | None] = {}
        # Serializa la región snapshot→matar→pop de close_game: dos close_game
        # concurrentes tomarían el mismo snapshot y matarían el árbol dos veces
        # (el segundo kill es no-op hoy, pero el lock lo blinda ante un futuro
        # sin GIL — verificación auditoría PRs #300-#304, follow-up H1).
        # launch_game NO toma este lock: registra el PID con una escritura de
        # dict plana (atómica) para no reabrir la ventana de huérfano de #302 si
        # la task se cancela esperando el lock (review Codex #305 C1).
        self._procs_lock = asyncio.Lock()

    @property
    def install_root(self) -> pathlib.Path:
        return self._install_root

    @property
    def data_root(self) -> pathlib.Path:
        return self._data_root

    @property
    def mods_dir(self) -> pathlib.Path:
        return self._mods_dir

    @property
    def root(self) -> pathlib.Path:
        """Alias legacy de install_root para compatibilidad de callers externos."""
        return self._install_root

    async def read_modlist(
        self,
        profile: str = "Default",
    ) -> AsyncGenerator[tuple[str, bool], None]:
        """Parse ``modlist.txt`` for *profile*, yielding ``(mod_name, enabled)``.

        Each line in MO2's ``modlist.txt`` follows the format::

            +ModName   (enabled)
            -ModName   (disabled)
            *ModName   (unmanaged / separator -- skipped)

        Yields one tuple per valid mod entry, consuming O(1) memory.
        Corrupt or unparseable lines are logged and skipped.
        """
        assert_safe_component(profile, field="profile")
        modlist_path = self._data_root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
            async for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                prefix = line[0]
                if prefix == "*":
                    continue

                if prefix not in ("+", "-"):
                    logger.warning("Skipping unparseable modlist line: %r", line)
                    continue

                mod_name = line[1:].strip()
                if not mod_name:
                    logger.warning("Skipping empty mod name in line: %r", line)
                    continue

                yield mod_name, (prefix == "+")

    async def add_mod_to_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
        *,
        highest_priority: bool = False,
        priority: str | None = None,
    ) -> None:
        """Add *mod_name* as enabled (``+``) to the profile modlist.

        If *highest_priority* is True (or *priority* == "highest"), the mod is
        inserted at the top of the mod list (highest priority in MO2's physical
        order, right after any leading comments/header). If the mod is already
        present in the modlist (enabled or disabled), it is moved cleanly to
        highest priority without duplication.

        If *highest_priority* is False (default), the mod is appended to the
        bottom of the modlist (lowest priority) if absent. If it is already
        present but disabled (``-``), it is flipped to ``+`` in place,
        preserving its position. An already-enabled entry is left unchanged; if the
        file already carries the same ``+X`` more than once (only possible by
        hand-editing ``modlist.txt``) it is NOT repaired: a warning is logged and
        the repeated ``+X`` entries are kept as they are (any ``-X`` is still
        discarded). ``read_enabled_mods`` still returns both entries; it is
        ``ganadores._exigir_enabled`` that rejects the repeated name, until the
        operator fixes the profile in MO2.

        **La identidad del mod no distingue mayúsculas** (#838): ``X`` y ``x`` son
        el mismo mod —MO2 resuelve ``mods/<Nombre>`` sobre un sistema de archivos
        de Windows—, así que ``add_mod_to_modlist("X")`` sobre un perfil con ``+x``
        es un no-op, ``-x`` se habilita en su lugar, y ``highest_priority=True``
        mueve la entrada existente en vez de duplicarla. La comparación usa
        :func:`_clave_de_nombre_de_mod`, la misma de ``ganadores._exigir_enabled``.
        Lo que se **conserva** es la grafía que ya está en el archivo: el nombre lo
        escribió el operador y reescribirlo sería tocar su ``modlist.txt`` sin que
        el caller lo haya pedido. Sólo se usa la grafía del caller cuando el mod no
        estaba en el perfil.

        Args:
            mod_name: The mod directory name (e.g. ``"Requiem"``).
            profile: MO2 profile name.
            highest_priority: Whether to insert at the top of the modlist (highest priority).
            priority: Optional string priority specifier (e.g. ``"highest"`` or ``"lowest"``).
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._data_root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        is_highest = highest_priority or (priority == "highest")
        clave_del_mod = _clave_de_nombre_de_mod(mod_name)

        async with self._modlist_lock:
            # Read the existing entries (and their order) so the atomic rewrite
            # keeps them. The rewrite normalizes line endings and always writes a
            # UTF-8 BOM (not a byte-for-byte copy of the original).
            lines: list[str] = []
            # Entradas del mod pedido que ya estaban en el perfil, en orden físico:
            # (índice en `lines`, signo, nombre tal como está escrito). El
            # nombre se guarda para conservar su grafía al escribir (``x`` no se
            # reescribe como ``X``) y para que el WARNING muestre las variantes que
            # el operador tiene que corregir en MO2.
            entradas_del_mod: list[tuple[int, str, str]] = []
            # Grafía que el operador ya tenía escrita para el mod (la primera entrada
            # que coincide, habilitada o no), para conservarla al escribir.
            nombre_existente: str | None = None
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        stripped = raw_line.strip()
                        es_entrada = bool(stripped) and stripped[0] in ("+", "-")
                        nombre_en_linea = stripped[1:].strip() if es_entrada else ""
                        coincide = es_entrada and _clave_de_nombre_de_mod(nombre_en_linea) == clave_del_mod
                        if coincide:
                            if nombre_existente is None:
                                nombre_existente = nombre_en_linea
                            # Si es highest_priority, filtramos cualquier ocurrencia
                            # previa del mod (habilitado o deshabilitado, en cualquier
                            # grafía) para moverlo limpiamente.
                            if is_highest:
                                continue
                            entradas_del_mod.append((len(lines), stripped[0], nombre_en_linea))
                        lines.append(raw_line)
            except FileNotFoundError:
                pass

            if not is_highest:
                if entradas_del_mod:
                    # Entradas del mod, en orden físico. Un `modlist.txt` escrito por
                    # MO2 trae una sola; más de una solo sale de un archivo editado a mano
                    # (o de variantes de mayúsculas, que son el mismo mod en Windows).
                    habilitadas = [i for i, signo, _ in entradas_del_mod if signo == "+"]
                    deshabilitadas = [i for i, signo, _ in entradas_del_mod if signo == "-"]
                    nombres_por_indice = {i: nombre for i, _, nombre in entradas_del_mod}
                    # Una `+X` existente manda: su posición ES la prioridad efectiva del
                    # mod, así que se conserva y las `-X` se descartan. Flipear una `-X`
                    # junto a una `+X` dejaría dos habilitadas y `ganadores._exigir_enabled`
                    # falla cerrado ante un nombre repetido. Sin ninguna habilitada se
                    # flipea la primera `-X` in-place (preservar orden) y se descartan
                    # las demás: cortar en la primera dejaría un `-X` posterior vivo.
                    if len(habilitadas) > 1:
                        # No se repara: MO2 no define cuál `+X` gana y borrar una línea
                        # del usuario excede lo que pidió el caller. Solo se avisa.
                        logger.warning(
                            "Mod %r aparece habilitado %d veces en el modlist del perfil %r "
                            "(grafías encontradas: %s; el nombre se compara sin distinguir "
                            "mayúsculas); las entradas habilitadas repetidas no se reparan "
                            "(solo se descartan las deshabilitadas, si las hay); "
                            "ganadores._exigir_enabled rechazará el perfil hasta corregirlo en MO2",
                            mod_name,
                            len(habilitadas),
                            profile,
                            ", ".join(f"+{n}" for n in sorted({nombres_por_indice[i] for i in habilitadas})),
                        )
                    flipear = deshabilitadas[0] if not habilitadas and deshabilitadas else None
                    descartar = set(deshabilitadas) - {flipear}
                    if flipear is None and not descartar:
                        logger.info("Mod %r already enabled in modlist for profile %r", mod_name, profile)
                        return
                    if flipear is not None:
                        # Se conserva la grafía que ya estaba en el archivo.
                        lines[flipear] = f"+{nombres_por_indice[flipear]}\n"
                    lines = [ln for i, ln in enumerate(lines) if i not in descartar]
                    if flipear is not None:
                        logger.info(
                            "Enabled +%s in modlist for profile %r (in place)",
                            nombres_por_indice[flipear],
                            profile,
                        )
                    else:
                        logger.info(
                            "Discarded %d disabled -%s entries in modlist for profile %r",
                            len(descartar),
                            mod_name,
                            profile,
                        )
                else:
                    lines.append(f"+{mod_name}\n")
            else:
                # Posición de inserción para máxima prioridad: inmediatamente después
                # de comentarios iniciales ('#') o líneas en blanco de header.
                insert_idx = 0
                while insert_idx < len(lines):
                    line_str = lines[insert_idx].strip()
                    if line_str and not line_str.startswith("#"):
                        break
                    insert_idx += 1
                # Si el mod ya estaba (en cualquier grafía) se reusa cómo lo escribió
                # el operador; si no, la grafía del caller.
                nombre_a_escribir = mod_name if nombre_existente is None else nombre_existente
                lines.insert(insert_idx, f"+{nombre_a_escribir}\n")

            # Atomic tmp->rename rewrite (with BOM), consistent with
            # remove/toggle — a non-atomic append could expose a partial line to
            # an external reader (watcher / MO2.exe) and omit the BOM (obs #192).
            await _write_modlist_atomic(validated, lines)

            logger.info(
                "Added +%s to modlist for profile %r (highest_priority=%s)",
                mod_name,
                profile,
                is_highest,
            )

    async def add_mod_to_modlist_highest_priority(
        self,
        mod_name: str,
        profile: str = "Default",
    ) -> None:
        """Add *mod_name* as enabled (``+``) with highest priority in the profile modlist."""
        await self.add_mod_to_modlist(mod_name, profile=profile, highest_priority=True)

    async def remove_mod_from_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
    ) -> None:
        """Remove *mod_name* entirely from the profile modlist.

        Quita **todas** las entradas del mod, sin distinguir mayúsculas (#838): en
        Windows ``X`` y ``x`` son el mismo mod, así que dejar viva una variante que
        ``add_mod_to_modlist`` considera «ya presente» haría que desinstalar no
        desinstale. La identidad es :func:`_clave_de_nombre_de_mod`, la misma de
        ``add_mod_to_modlist`` y de ``ganadores._exigir_enabled``.

        Args:
            mod_name: The mod directory name.
            profile: MO2 profile name.
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._data_root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        clave_del_mod = _clave_de_nombre_de_mod(mod_name)

        async with self._modlist_lock:
            lines: list[str] = []
            found = False
            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if (
                            line
                            and line[0] in ("+", "-")
                            and _clave_de_nombre_de_mod(line[1:].strip()) == clave_del_mod
                        ):
                            found = True
                            continue  # Skip this line
                        lines.append(raw_line)
            except FileNotFoundError:
                return

            if not found:
                return

            await _write_modlist_atomic(validated, lines)
            logger.info("Removed %s from modlist for profile %r", mod_name, profile)

    async def toggle_mod_in_modlist(
        self,
        mod_name: str,
        profile: str = "Default",
        enable: bool = True,
    ) -> None:
        """Toggle the enabled state of *mod_name* in the modlist.

        El nombre se compara sin distinguir mayúsculas (#838), igual que en
        ``add_mod_to_modlist`` y ``remove_mod_from_modlist``: con ``+x`` en el
        perfil, ``toggle_mod_in_modlist("X", enable=False)`` deshabilita ``x`` en
        vez de no encontrar nada. Al cambiar el signo se **conserva la grafía** que
        ya estaba escrita; sólo el signo cambia.

        Args:
            mod_name: The mod directory name.
            profile: MO2 profile name.
            enable: True to enable (+), False to disable (-).
        """
        assert_safe_component(mod_name, field="mod_name")
        assert_safe_component(profile, field="profile")
        modlist_path = self._data_root / "profiles" / profile / "modlist.txt"
        validated = self._validator.validate(modlist_path)

        clave_del_mod = _clave_de_nombre_de_mod(mod_name)

        async with self._modlist_lock:
            lines: list[str] = []
            changed = False
            target_prefix = "+" if enable else "-"

            try:
                async with aiofiles.open(validated, encoding="utf-8-sig") as fh:
                    async for raw_line in fh:
                        line = raw_line.strip()
                        if (
                            line
                            and line[0] in ("+", "-")
                            and _clave_de_nombre_de_mod(line[1:].strip()) == clave_del_mod
                        ):
                            if line[0] != target_prefix:
                                # Se conserva la grafía existente: sólo cambia el signo.
                                lines.append(f"{target_prefix}{line[1:].strip()}\n")
                                changed = True
                            else:
                                lines.append(raw_line)
                        else:
                            lines.append(raw_line)
            except FileNotFoundError:
                return

            if changed:
                await _write_modlist_atomic(validated, lines)
                state = "Enabled" if enable else "Disabled"
                logger.info("%s %s in modlist for profile %r", state, mod_name, profile)

    async def delete_mod_files(self, mod_name: str) -> None:
        """Delete the mod directory from MO2's mods folder entirely.

        Args:
            mod_name: The mod directory name.
        """
        assert_safe_component(mod_name, field="mod_name")
        mod_dir = self._mods_dir / mod_name

        # Fail-closed sobre la ruta CRUDA, antes de validate(). No es redundante
        # con el borrado link-aware de abajo: PathValidator.validate() gatea
        # symlinks con is_symlink() —ciego a junctions— y después hace
        # target.resolve() INCONDICIONAL, así que para un junction devuelve el
        # DESTINO RESUELTO. El borrado ni siquiera necesitaría atravesar un
        # reparse point: recibiría directamente el árbol ajeno como si fuera el
        # mod. Mismo orden y mismo motivo que grass_profile.build_config_mod.
        #
        # Pesa que este camino arranca en una tool del agente LLM
        # (system_tools.uninstall_mod → delete_mod_files) con el nombre del mod
        # como parámetro del modelo.
        try:
            tipo_de_enlace = await asyncio.to_thread(link_kind_or_raise_with_retry, mod_dir)
        except OSError as exc:
            raise PathViolationError(f"No se pudo inspeccionar el mod '{mod_name}' antes de borrarlo: {exc}") from exc
        if tipo_de_enlace is not None:
            raise PathViolationError(
                f"El mod '{mod_name}' es un enlace ({tipo_de_enlace}), no un directorio real: "
                "borrarlo destruiría el árbol al que apunta. Quitá el enlace a mano si "
                "realmente querés desinstalarlo."
            )

        validated = self._validator.validate(mod_dir)

        if validated.exists() and validated.is_dir():
            await asyncio.to_thread(_rmtree_force, validated)
            logger.info("Deleted mod directory: %s", validated)

    async def launch_game(self, profile: str = "Default") -> dict[str, Any]:
        """Launch Skyrim via SKSE through MO2 for the given profile.

        TASK-011: Fully async spawn verification with zombie prevention.
        ModOrganizer.exe is a long-running GUI process; we only verify
        that the OS registered the PID within ``_spawn_timeout`` seconds.
        We do **not** block waiting for the process to finish.

        Args:
            profile: The MO2 profile to use.

        Returns:
            Dict containing the PID of the spawned ModOrganizer process.

        Raises:
            FileNotFoundError: If the MO2 executable does not exist.
            GameLaunchTimeoutError: If the launch process fails to appear
                in the process table within the configured timeout.
        """
        assert_safe_component(profile, field="profile")
        mo2_exe = self._install_root / "ModOrganizer.exe"
        validated_exe = self._validator.validate(mo2_exe)

        if not validated_exe.exists():
            raise FileNotFoundError(f"MO2 executable not found: {validated_exe}")

        # TASK-011: cwd must be the native filesystem path.
        # Under WSL2 this is the Linux path (/mnt/c/...); on native Windows
        # it is the Windows path (C:\...).  We do NOT translate it here.
        cwd_native = str(self._install_root)

        cmd = [str(validated_exe), "-p", profile, "moshortcut://SKSE"]

        logger.info("Launching game with command: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd_native,
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"MO2 executable not found: {validated_exe}") from None

        # §1.1: registrar el PID ANTES de verificar el spawn. Si MO2 muere (o la
        # operación se cancela) entre la verificación y el registro, un PID sin
        # trackear quedaría FUERA del alcance de close_game → MO2/Skyrim huérfano
        # corriendo el precache contra la instalación real (análisis hostil §1.1).
        # Se guarda el create_time (identidad estable) para que close_game no
        # mate un proceso ajeno si el SO reusó el PID (review Codex #302).
        create_time: float | None = None
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            create_time = psutil.Process(proc.pid).create_time()
        # Escritura de dict PLANA (atómica bajo el GIL): NO se toma _procs_lock
        # acá. Un await entre el spawn y el registro reabriría la ventana de
        # huérfano que #302 cerró — si la task se cancela esperando el lock (un
        # close_game concurrente lo sostiene durante su kill loop), el PID nunca
        # se registra y el snapshot ya tomado de close_game no lo ve (review
        # Codex #305 C1). close_game snapshotea+popea solo su snapshot, así que
        # este registro concurrente sobrevive sin necesidad del lock.
        self._launched_procs[proc.pid] = create_time

        # TASK-011: Verify the process actually spawned (short grace period).
        try:
            await asyncio.wait_for(
                _verify_pid_alive(proc.pid),
                timeout=self._spawn_timeout,
            )
        except TimeoutError:
            # Spawn failed -- el proceso nunca apareció: dejar de trackearlo y
            # limpiar el defunto. Pop plano (atómico, idempotente).
            self._launched_procs.pop(proc.pid, None)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            logger.error(
                "Game launch process did not appear in process table for profile %r",
                profile,
            )
            raise GameLaunchTimeoutError(self._spawn_timeout) from None

        return {"pid": proc.pid, "status": "launched", "profile": profile}

    async def close_game(self) -> dict[str, Any]:
        """Attempt to forcefully close the game/MO2 tree launched by this controller.

        M-8: sólo se matan los árboles de los ModOrganizer.exe que ESTA instancia
        lanzó (los PIDs de :attr:`_launched_pids` + sus descendientes), no todos
        los procesos del host que se llamen ``skyrimse.exe``/``modorganizer.exe``.
        Así una segunda instancia de MO2/Skyrim del usuario no se ve afectada.

        §1.2: se matan TODOS los PIDs trackeados, no solo el último — si el
        crash-loop relanzó MO2 sin cerrarlo (o un cierre previo no alcanzó a
        limpiar), un MO2 viejo vivo no debe quedar huérfano (análisis hostil §1.2).

        TASK-011: The ``psutil`` iteration is wrapped in ``asyncio.to_thread`` to
        avoid blocking the event loop.

        Returns:
            Dict showing which processes were killed.
        """
        # Snapshot: se procesan SOLO estos PIDs. No se usa clear() porque un
        # launch_game concurrente podría registrar un PID nuevo mientras matamos
        # (await), y clear() lo descartaría dejándolo huérfano (review Codex
        # #302). Toda la región snapshot→matar→pop va bajo _procs_lock: dos
        # close_game concurrentes no toman el mismo snapshot ni matan el árbol
        # dos veces (el segundo ve el dict ya vaciado), y un launch_game
        # concurrente espera al lock y registra su PID DESPUÉS del pop, así que
        # no se pierde (se matará en la próxima corrida). Verificación auditoría
        # PRs #300-#304, follow-up H1.
        async with self._procs_lock:
            snapshot = sorted(self._launched_procs.items())
            if not snapshot:
                logger.info("close_game: no hay un juego lanzado por esta instancia; no-op.")
                return {"status": "closed", "killed_processes": []}

            killed: list[str] = []
            for pid, create_time in snapshot:
                killed.extend(await asyncio.to_thread(self._kill_process_tree, pid, create_time))
            for pid, _ in snapshot:
                self._launched_procs.pop(pid, None)
        pids = [pid for pid, _ in snapshot]
        logger.info("Closed game process tree(s) (pids=%s): %s", pids, killed)
        return {"status": "closed", "killed_processes": killed}

    @staticmethod
    def _kill_process_tree(pid: int, expected_create_time: float | None = None) -> list[str]:
        """Mata SÓLO el proceso ``pid`` y sus descendientes (no por nombre).

        Separado para envolver en ``asyncio.to_thread``. Best-effort: procesos ya
        muertos o sin permiso se ignoran.

        Si se pasa *expected_create_time*, se verifica la identidad del proceso
        antes de matar: un PID reusado por el SO (tras morir el MO2 original)
        tendría otro ``create_time`` → no se mata un proceso ajeno del usuario
        (review Codex #302). ``None`` (create_time no capturado al lanzar) omite
        el chequeo: el proceso casi siempre ya murió (``NoSuchProcess``).
        """
        killed: list[str] = []
        try:
            root = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return killed

        if expected_create_time is not None:
            try:
                if root.create_time() != expected_create_time:
                    return killed  # PID reusado: NO es el proceso que lanzamos
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return killed

        # Recolectar el árbol (hijos primero) antes de matar, para no perder la
        # relación padre-hijo cuando el padre muere.
        try:
            procs = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            procs = []
        procs.append(root)

        for proc in procs:
            try:
                name = proc.name()
                proc.kill()
                killed.append(f"{name}({proc.pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return killed


async def _verify_pid_alive(pid: int) -> None:
    """Poll until *pid* is visible in the process table.

    Uses ``asyncio.to_thread`` so the synchronous ``psutil.pid_exists``
    call does not block the event loop.
    """
    for _ in range(50):  # 5 seconds total @ 0.1 s
        exists = await asyncio.to_thread(psutil.pid_exists, pid)
        if exists:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError
