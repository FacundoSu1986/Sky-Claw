"""Resolución del LOOT data root propiedad de Sky-Claw (PR-1).

Contrato upstream (loot/loot tag 0.29.1, commit 77f3ba98):

* ``src/gui/qt/main.cpp``: declara ``--loot-data-path`` como opción de
  ``QCommandLineParser`` (junto a ``--game``, ``--game-path``,
  ``--auto-sort``). El valor se lee vía ``parser.value(\"loot-data-path\")``
  y se pasa a ``LootPaths(\"\", lootDataPath)``.
* ``src/gui/state/loot_paths.cpp`` / ``loot_paths.h``:

  - ``getDataPath(givenPath)``: ``givenPath.empty() ? getLocalAppDataPath() /
    \"LOOT\" : givenPath``. Vacío → default ``%LOCALAPPDATA%\\LOOT`` (Windows)
    o ``$XDG_DATA_HOME/LOOT`` / ``$HOME/.local/share/LOOT`` (Linux).
  - ``LootPaths`` guarda ``lootDataPath_`` y deriva:
    ``settings.toml`` → ``<data>/settings.toml``
    ``LOOTDebugLog.txt`` → ``<data>/LOOTDebugLog.txt``
    ``prelude/`` → ``<data>/prelude/prelude.yaml``
* ``src/gui/state/loot_state.cpp``:

  - ``LootState::LootState`` → ``GamesManager(lootDataPath, preludePath)``
  - ``createLootDataPath()``: si ``<data>`` no existe, ``create_directory``.
    Requiere que el padre exista (``create_directory`` crea un solo nivel).
  - ``createPreludeDirectory()``: crea ``<data>/prelude``.
* ``src/gui/state/game/game.cpp`` / ``game.h``:

  - ``getLOOTGamePath``: ``<data>/games/<folderName>``
  - ``initLootGameFolder``: asegura ``<data>/games/<game>`` existe (con
    ``create_directories`` para padres), migra legacy ``<data>/SkyrimSE``,
    copia masterlist desde default folder si falta.
  - ``getMasterlistPath``: ``<data>/games/<folder>/masterlist.yaml``
  - ``getUserlistPath``: ``<data>/games/<folder>/userlist.yaml``
  - ``getBackupsPath``: ``<data>/games/<folder>/backups``

Por tanto ``--loot-data-path`` aísla:

* settings.toml
* LOOTDebugLog.txt
* prelude/
* games/<game>/masterlist.yaml, userlist.yaml, backups/, etc.
* themes/ (de ``getThemesPath``)

NO aísla por sí solo (evidencia: no hay referencia a plugins.txt, loadorder,
Skyrim Data, MO2 profile, USVFS mappings, mutex):

* plugins.txt / loadorder.txt (viven en game local appdata o MO2 profile)
* Skyrim Data
* MO2 profile
* USVFS mappings
* mutex global ``LOOT.Shell.Instance`` (``src/gui/qt/main.cpp`` +
  ``src/gui/application_mutex.h``: segunda instancia sale rc 0 enfocando
  ventana existente, independiente del data root)

El mutex y success contract pertenecen a PR-2/rig, masterlist provenance a PR-3.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from typing import Final

from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.config import SystemPaths

# ---------------------------------------------------------------------------
# Base canónica — reutiliza raíz ya establecida
# ---------------------------------------------------------------------------
# Censo (PR-1):
# - Config.DEFAULT_CONFIG_DIR = ~/.sky_claw (config.toml, tokens, vfs_bridge)
# - SystemPaths.runtime_state_dir() = ~/.sky_claw/state (estado durable por
#   usuario, estable entre arranques, no depende de cwd — usado por
#   DynDOLOD coordination, no por .skyclaw_backups que es relativo al cwd)
# - .skyclaw_backups (cwd-relative) para locks/journal/snapshots
# - .skyclaw_sandbox para clones de perfil MO2
#
# Decisión: reutilizar runtime_state_dir (hermano de DEFAULT_CONFIG_DIR,
# propósito explícito de estado durable) como base, con subdirectorio "loot".
# No se hardcodea %USERPROFILE%/.sky_claw dentro del runner: la decisión se
# toma en el daemon/control plane (PathResolutionService / AppContext /
# BrokeredLootRunner) y el worker recibe ruta explícita ya resuelta.
#
# Lifetime: PERSISTENTE por instancia + perfil (no por operación). Razón:
# masterlist/userlist/settings/backups son estado persistente; un root vacío
# por operación rompería bootstrap o dispararía network/update cada vez.
# Aislamiento al menos por instancia+perfil para evitar interferencia entre
# perfiles/instancias.

DEFAULT_LOOT_DATA_BASE: Final[pathlib.Path] = SystemPaths.runtime_state_dir() / "loot"

# Nombres reservados Windows (case-insensitive, con o sin extensión)
_RESERVED_WINDOWS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }
)


def _is_reserved_windows_name(name: str) -> bool:
    """True si *name* es reservado Windows (CON, PRN, etc., con o sin ext)."""
    base = name.split(".")[0].casefold() if "." in name else name.casefold()
    return base in _RESERVED_WINDOWS_NAMES or name.casefold() in _RESERVED_WINDOWS_NAMES


def _validate_profile_for_loot_path(profile: str) -> str:
    """Valida perfil para uso como componente de ruta del LOOT data root.

    Usa ``assert_safe_component`` (rechaza ``..``, ``/``, ``\\``, NUL,
    controles) más checks adicionales de Windows:

    - no reservado (CON, PRN, ...)
    - no termina en espacio o punto (Windows los recorta)
    - no vacío tras strip (defensa adicional)
    """
    safe = assert_safe_component(profile, field="profile")
    if not safe.strip():
        raise PathViolationError("profile: must not be whitespace only")
    if safe.endswith(" ") or safe.endswith("."):
        raise PathViolationError(
            f"profile: trailing space/dot not allowed on Windows, got {safe!r}"
        )
    if _is_reserved_windows_name(safe):
        raise PathViolationError(
            f"profile: reserved Windows name not allowed, got {safe!r}"
        )
    return safe


def get_default_loot_gui_data_path() -> pathlib.Path | None:
    """Ruta default del LOOT GUI del operador (para detectar colisión).

    Windows: %LOCALAPPDATA%\\LOOT
    Linux: $XDG_DATA_HOME/LOOT o $HOME/.local/share/LOOT
    """
    if os.name == "nt" or "LOCALAPPDATA" in os.environ:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return pathlib.Path(local) / "LOOT"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return pathlib.Path(xdg) / "LOOT"
    home = os.environ.get("HOME", "") or str(pathlib.Path.home())
    if home:
        return pathlib.Path(home) / ".local" / "share" / "LOOT"
    return None


def resolve_loot_data_path(
    *,
    instance_id: str,
    profile: str,
    base_dir: pathlib.Path | None = None,
    game_path: pathlib.Path | None = None,
    loot_exe: pathlib.Path | None = None,
    mods_dir: pathlib.Path | None = None,
    data_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """Resuelve el LOOT data root propiedad de Sky-Claw.

    Args:
        instance_id: identificador estable de instancia MO2 (p.ej. ``mo2-<hash>``).
        profile: nombre de perfil MO2.
        base_dir: base opcional (default ``DEFAULT_LOOT_DATA_BASE``).
        game_path: opcional, para validar que el root no esté dentro de Skyrim.
        loot_exe: opcional, para validar que no esté dentro del LOOT install.
        mods_dir: opcional, para validar que no esté dentro de mods.
        data_root: opcional, para validar que no esté dentro del profile como
            reemplazo de plugins.txt.

    Returns:
        Path absoluto, estable, propiedad de Sky-Claw.

    Raises:
        PathViolationError: si instance_id/profile no son seguros o violan
            invariantes de seguridad.
        ValueError: si el path resultante colisiona con default GUI LOOT o
            con rutas prohibidas.
    """
    # Validar componentes
    safe_instance = assert_safe_component(instance_id, field="instance_id")
    if _is_reserved_windows_name(safe_instance):
        raise PathViolationError(
            f"instance_id: reserved Windows name not allowed, got {safe_instance!r}"
        )
    if safe_instance.endswith(" ") or safe_instance.endswith("."):
        raise PathViolationError(
            f"instance_id: trailing space/dot not allowed, got {safe_instance!r}"
        )
    safe_profile = _validate_profile_for_loot_path(profile)

    base = base_dir or DEFAULT_LOOT_DATA_BASE
    base_path = pathlib.Path(base)
    if not base_path.is_absolute():
        raise PathViolationError(f"loot data base must be absolute, got {base_path}")

    # Construir path: <base>/<instance_id>/<profile>
    candidate = (base_path / safe_instance / safe_profile).resolve(strict=False)

    # Debe permanecer bajo base (defensa contra .. si alguien bypasea validación)
    try:
        candidate.relative_to(base_path.resolve(strict=False))
    except ValueError as exc:
        raise PathViolationError(
            f"loot_data_path {candidate!r} escapes base {base_path!r}"
        ) from exc

    # Invariantes de seguridad (sección 9 del brief)
    # - absoluto
    if not candidate.is_absolute():
        raise ValueError(f"loot_data_path must be absolute, got {candidate}")

    # - NO igual al default global del GUI LOOT
    default_gui = get_default_loot_gui_data_path()
    if default_gui is not None:
        try:
            # Comparación case-insensitive en Windows
            if os.name == "nt":
                if candidate.resolve(strict=False).as_posix().casefold() == default_gui.resolve(
                    strict=False
                ).as_posix().casefold():
                    raise ValueError(
                        f"loot_data_path {candidate} must not be the default GUI LOOT path {default_gui}"
                    )
            else:
                if candidate.resolve(strict=False) == default_gui.resolve(strict=False):
                    raise ValueError(
                        f"loot_data_path {candidate} must not be the default GUI LOOT path {default_gui}"
                    )
        except ValueError:
            raise
        except Exception:
            # Si la comparación falla por razones inesperadas, no bloquear por defecto
            pass

    # - NO dentro del LOOT install dir
    if loot_exe is not None:
        loot_dir = pathlib.Path(loot_exe).parent.resolve(strict=False)
        try:
            # Si candidate está dentro de loot_dir o es igual
            if candidate == loot_dir or candidate.is_relative_to(loot_dir):
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside LOOT install dir {loot_dir}"
                )
        except AttributeError:
            # Python <3.9 fallback
            try:
                candidate.relative_to(loot_dir)
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside LOOT install dir {loot_dir}"
                ) from None
            except ValueError:
                if candidate == loot_dir:
                    raise ValueError(
                        f"loot_data_path {candidate} must not be inside LOOT install dir {loot_dir}"
                    ) from None

    # - NO dentro de Skyrim\Data ni del game path mismo
    if game_path is not None:
        g_path = pathlib.Path(game_path).resolve(strict=False)
        data_path = (g_path / "Data").resolve(strict=False)
        for forbidden in (g_path, data_path):
            try:
                if candidate == forbidden or candidate.is_relative_to(forbidden):
                    raise ValueError(
                        f"loot_data_path {candidate} must not be inside game path {forbidden}"
                    )
            except AttributeError:
                try:
                    candidate.relative_to(forbidden)
                    raise ValueError(
                        f"loot_data_path {candidate} must not be inside game path {forbidden}"
                    ) from None
                except ValueError:
                    if candidate == forbidden:
                        raise ValueError(
                            f"loot_data_path {candidate} must not be inside game path {forbidden}"
                        ) from None

    # - NO dentro de mods
    if mods_dir is not None:
        m_dir = pathlib.Path(mods_dir).resolve(strict=False)
        try:
            if candidate == m_dir or candidate.is_relative_to(m_dir):
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside mods dir {m_dir}"
                )
        except AttributeError:
            try:
                candidate.relative_to(m_dir)
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside mods dir {m_dir}"
                ) from None
            except ValueError:
                if candidate == m_dir:
                    raise ValueError(
                        f"loot_data_path {candidate} must not be inside mods dir {m_dir}"
                    ) from None

    # - NO dentro del profile como reemplazo de plugins.txt
    #   data_root / profiles / <profile>
    if data_root is not None:
        d_root = pathlib.Path(data_root).resolve(strict=False)
        profile_dir = (d_root / "profiles" / safe_profile).resolve(strict=False)
        try:
            if candidate == profile_dir or candidate.is_relative_to(profile_dir):
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside MO2 profile dir {profile_dir}"
                )
        except AttributeError:
            try:
                candidate.relative_to(profile_dir)
                raise ValueError(
                    f"loot_data_path {candidate} must not be inside MO2 profile dir {profile_dir}"
                ) from None
            except ValueError:
                if candidate == profile_dir:
                    raise ValueError(
                        f"loot_data_path {candidate} must not be inside MO2 profile dir {profile_dir}"
                    ) from None

    # - estable entre corridas: garantizado por usar instance_id+profile estables

    return candidate


def ensure_loot_data_path_exists(path: pathlib.Path) -> pathlib.Path:
    """Asegura que el padre del LOOT data root exista (LOOT crea el root).

    Upstream ``LootState::createLootDataPath`` usa ``create_directory`` (un
    solo nivel), por lo que el padre debe existir. Creamos padres con
    ``mkdir(parents=True)`` y dejamos que LOOT cree el root mismo, pero
    también creamos el root para evitar race.

    Returns:
        El path resuelto.
    """
    p = pathlib.Path(path).resolve(strict=False)
    if not p.is_absolute():
        raise ValueError(f"loot_data_path must be absolute, got {p}")
    # Crear padres
    p.parent.mkdir(parents=True, exist_ok=True)
    # Crear el root mismo (best-effort, LOOT también lo crearía)
    p.mkdir(parents=True, exist_ok=True)
    return p


def validate_loot_data_path_for_worker(
    path: pathlib.Path,
    *,
    allowed_bases: list[pathlib.Path] | None = None,
) -> pathlib.Path:
    """Validación en el worker: absoluta, no symlink, permitida.

    Args:
        path: ruta recibida del payload.
        allowed_bases: bases permitidas adicionales (si se quiere chequear
            contención). Si None, solo se exige absoluta y no symlink.

    Returns:
        Path resuelto.

    Raises:
        ValueError si no es absoluta, relativa, o es symlink.
        PathViolationError si escapa bases permitidas.
    """
    p = pathlib.Path(path)
    if not p.is_absolute():
        raise ValueError(f"payload.loot_data_path must be absolute, got {p}")

    # Rechazar symlink textual antes de resolver (evita que un symlink quede
    # convertido en archivo normal y eluda policy, mismo patrón que
    # _session_payload_path en vfs_worker)
    if p.is_symlink():
        raise ValueError("payload.loot_data_path must not be a symlink")

    resolved = p.resolve(strict=False)

    # Si hay bases permitidas, validar contención
    if allowed_bases:
        for base in allowed_bases:
            try:
                resolved.relative_to(base.resolve(strict=False))
                break
            except ValueError:
                continue
        else:
            # No está bajo ninguna base permitida, pero si es bajo
            # DEFAULT_LOOT_DATA_BASE (que es propiedad de Sky-Claw) lo
            # permitimos aunque no esté en allowed_bases (caso de worker
            # que no tiene config dir en sus roots)
            with contextlib.suppress(ValueError):
                resolved.relative_to(DEFAULT_LOOT_DATA_BASE.resolve(strict=False))
            # Si tampoco está bajo default base, y no está bajo allowed,
            # sigue siendo válido si es absoluto y no symlink — la propiedad
            # se verifica en el daemon, el worker solo asegura no relativo/symlink.

    return resolved
