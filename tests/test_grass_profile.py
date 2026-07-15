"""Tests del ``GrassProfileManager`` (PR-3 del plan grass cache, Fase B del SOP).

Preparación aislada del Stage 8 (No Grass In Objects): en vez de mutar el perfil
ACTIVO del usuario y sus INIs (y depender de un rollback que puede fallar — la
mitad de la matriz de riesgos de los planes externos), se clona el perfil a uno
**dedicado y lanzable** (``profiles/SkyClaw-GrassCache``) y todos los cambios del
ritual viven ahí: el mod de configuración (``GrassControl.ini`` +
``SSEDisplayTweaks.ini``) y los toggles de mods conflictivos. **El perfil real
jamás se toca.**

Anclas del contrato:
- Clonado byte-fiel (BOM UTF-8 + CRLF, como los escribe MO2): mismo estándar que
  ``ProfileSandbox`` y ``IniEditor``.
- El mod de config nace con ``meta.ini`` válido y **máxima prioridad VFS** (última
  línea del ``modlist.txt`` en MO2) para que su ``GrassControl.ini`` gane.
- Aislamiento demostrable: toggles y config solo tocan el clon; el modlist real y
  los INIs reales quedan byte-idénticos.
- ``teardown`` idempotente (borra clon + mod; un segundo llamado no falla).
- Symlink en el árbol → fail-closed con la política ``SandboxSymlinkError``.
"""

from __future__ import annotations

import configparser
import pathlib
import sys
import tempfile

import pytest

from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import (
    GrassProfileError,
    GrassProfileManager,
)
from sky_claw.local.mo2.profile_sandbox import (
    ProfileNotFoundError,
    SandboxSymlinkError,
)


def _puede_crear_symlinks() -> bool:
    """En Windows crear symlinks requiere privilegios; mismo guard que test_profile_sandbox."""
    try:
        with tempfile.TemporaryDirectory() as td:
            origen = pathlib.Path(td) / "src.txt"
            origen.touch()
            (pathlib.Path(td) / "link.txt").symlink_to(origen)
        return True
    except (OSError, NotImplementedError):
        return False


_symlink_guard = pytest.mark.skipif(
    sys.platform == "win32" and not _puede_crear_symlinks(),
    reason="Crear symlinks requiere privilegios elevados en Windows",
)

# Contenidos byte-exactos: BOM UTF-8 + CRLF, tal como MO2 los escribe en Windows.
_MODLIST = b"\xef\xbb\xbf+ModA\r\n-ModB\r\n+ConflictoENB\r\n"
_PLUGINS = b"\xef\xbb\xbf*Skyrim.esm\r\n*USSEP.esp\r\n"
_SKYRIM_INI = b"[General]\r\nsLanguage=ENGLISH\r\n\r\n[Grass]\r\nbAllowCreateGrass=1\r\n"
_SETTINGS = b"[General]\r\ngameName=Skyrim Special Edition\r\n"


@pytest.fixture
def mo2_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instancia MO2 sintética: perfil ``Default`` con modlist + INIs, y ``mods/``."""
    root = tmp_path / "mo2"
    profile = root / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "modlist.txt").write_bytes(_MODLIST)
    (profile / "plugins.txt").write_bytes(_PLUGINS)
    (profile / "Skyrim.ini").write_bytes(_SKYRIM_INI)
    (profile / "settings.txt").write_bytes(_SETTINGS)
    (root / "mods").mkdir()
    (root / "overwrite").mkdir()
    return root


@pytest.fixture
def manager(mo2_root: pathlib.Path) -> GrassProfileManager:
    validator = PathValidator(roots=[mo2_root])
    return GrassProfileManager(mo2_root, validator)


def _leer(path: pathlib.Path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# create_clone_profile
# ---------------------------------------------------------------------------


async def test_clona_perfil_byte_a_byte(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    clon = await manager.create_clone_profile()

    assert clon == mo2_root / "profiles" / "SkyClaw-GrassCache"
    assert clon.is_dir()
    # Cada archivo del perfil se copia byte-idéntico (BOM/CRLF incluidos).
    for nombre in ("modlist.txt", "plugins.txt", "Skyrim.ini", "settings.txt"):
        assert _leer(clon / nombre) == _leer(mo2_root / "profiles" / "Default" / nombre), nombre


async def test_perfil_source_inexistente_lanza(mo2_root: pathlib.Path) -> None:
    validator = PathValidator(roots=[mo2_root])
    mgr = GrassProfileManager(mo2_root, validator, source_profile="NoExiste")

    with pytest.raises(ProfileNotFoundError):
        await mgr.create_clone_profile()


async def test_create_falla_si_el_clon_ya_existe(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    # Fail-closed: no pisar un clon previo (podría tener trabajo en curso).
    # La idempotencia se obtiene vía teardown, no reventando el clon existente.
    with pytest.raises(GrassProfileError):
        await manager.create_clone_profile()


@_symlink_guard
async def test_symlink_en_el_arbol_fail_closed(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    # Un symlink en el perfil real podría sacar la copia fuera del árbol MO2.
    (mo2_root / "profiles" / "Default" / "link.ini").symlink_to(mo2_root / "profiles" / "Default" / "Skyrim.ini")

    with pytest.raises(SandboxSymlinkError):
        await manager.create_clone_profile()

    # Fail-closed real: no dejó un clon a medias.
    assert not (mo2_root / "profiles" / "SkyClaw-GrassCache").exists()


# ---------------------------------------------------------------------------
# build_config_mod
# ---------------------------------------------------------------------------


async def test_mod_de_config_meta_ini_y_maxima_prioridad(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel", "DLC2SolstheimWorld"])

    assert mod_dir == mo2_root / "mods" / "SkyClaw - Grass Precache Config"
    # meta.ini válido y parseable, con el nombre del mod.
    meta = configparser.ConfigParser()
    meta.read(mod_dir / "meta.ini", encoding="utf-8")
    assert meta["General"]["name"] == "SkyClaw - Grass Precache Config"

    # El mod queda con máxima prioridad VFS: en MO2 la ÚLTIMA línea del
    # modlist.txt es la de mayor prioridad (gana conflictos de archivos).
    clon_modlist = (mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt").read_text(encoding="utf-8-sig")
    lineas = [ln.strip() for ln in clon_modlist.splitlines() if ln.strip()]
    assert lineas[-1] == "+SkyClaw - Grass Precache Config"


async def test_grasscontrol_ini_worldspaces_y_flags(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel", "DLC2SolstheimWorld"])

    grass_ini = (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_text(encoding="utf-8")
    # NGIO-NG documenta arrancar la generación con AMBOS en true (README).
    assert "Use-grass-cache = True" in grass_ini
    assert "Only-load-from-cache = True" in grass_ini
    # Clave NGIO-NG con guiones, worldspaces entre comillas separados por ';'
    # (GrassControl.toml oficial) — la forma legacy space-joined la ignoraría.
    assert 'Only-pregenerate-world-spaces = "Tamriel;DLC2SolstheimWorld"' in grass_ini


async def test_worldspaces_vacio_escribe_comillas_vacias(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod([])

    grass_ini = (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_text(encoding="utf-8")
    assert 'Only-pregenerate-world-spaces = ""' in grass_ini


async def test_ssedisplaytweaks_baja_resolucion(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel"])

    sse_ini = (mod_dir / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini").read_text(encoding="utf-8")
    # Ventana marginal para acelerar los micro-lanzamientos entre CTDs. En
    # secciones clásicas el IniEditor usa "key=value" (estilo INI del juego).
    # 800x400 es la resolución que exige el SOP §2.8 para tolerar los scans.
    assert "[Render]" in sse_ini
    assert "Resolution=800x400" in sse_ini
    assert "Borderless=true" in sse_ini


async def test_params_override_gana_sobre_default(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel"], params={"Use-grass-cache": "False", "Extra-flag": "1"})

    grass_ini = (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_text(encoding="utf-8")
    assert "Use-grass-cache = False" in grass_ini  # override
    assert "Extra-flag = 1" in grass_ini  # clave nueva
    # Los defaults no pisados siguen presentes.
    assert "Only-load-from-cache = True" in grass_ini


async def test_build_config_mod_requiere_clon_primero(manager: GrassProfileManager) -> None:
    # Sin clon no hay dónde habilitar el mod: fail-closed antes de escribir nada.
    with pytest.raises(GrassProfileError):
        await manager.build_config_mod(["Tamriel"])


@_symlink_guard
async def test_mod_de_config_symlink_no_borra_su_target(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    # Si el nombre del mod de config ya existe como symlink a OTRO mod, borrarlo
    # con _rmtree_force sobre la ruta resuelta arrasaría ese mod. Fail-closed.
    await manager.create_clone_profile()
    otro = mo2_root / "mods" / "OtroModImportante"
    otro.mkdir()
    (otro / "importante.esp").write_bytes(b"no-borrar")
    (mo2_root / "mods" / "SkyClaw - Grass Precache Config").symlink_to(otro, target_is_directory=True)

    with pytest.raises(GrassProfileError):
        await manager.build_config_mod(["Tamriel"])

    # El árbol al que apunta el symlink quedó intacto.
    assert (otro / "importante.esp").read_bytes() == b"no-borrar"


# ---------------------------------------------------------------------------
# disable_conflicting_mods — aislamiento
# ---------------------------------------------------------------------------


async def test_toggles_solo_en_clon_real_intacto(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    real_modlist = mo2_root / "profiles" / "Default" / "modlist.txt"
    bytes_reales_antes = _leer(real_modlist)

    await manager.create_clone_profile()
    await manager.disable_conflicting_mods(["ModA", "ConflictoENB"])

    # El clon: ambos mods desactivados.
    clon_modlist = (mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt").read_text(encoding="utf-8-sig")
    assert "-ModA" in clon_modlist
    assert "-ConflictoENB" in clon_modlist
    # El real: byte-idéntico a como estaba (jamás se tocó).
    assert _leer(real_modlist) == bytes_reales_antes


async def test_disable_requiere_clon_primero(manager: GrassProfileManager) -> None:
    with pytest.raises(GrassProfileError):
        await manager.disable_conflicting_mods(["ModA"])


# ---------------------------------------------------------------------------
# teardown — idempotente, restaura el entorno
# ---------------------------------------------------------------------------


async def test_teardown_borra_clon_y_mod(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()
    await manager.build_config_mod(["Tamriel"])
    clon = mo2_root / "profiles" / "SkyClaw-GrassCache"
    mod = mo2_root / "mods" / "SkyClaw - Grass Precache Config"
    assert clon.is_dir() and mod.is_dir()

    fallidos = await manager.teardown()

    assert fallidos == []
    assert not clon.exists()
    assert not mod.exists()
    # El perfil real sigue en pie.
    assert (mo2_root / "profiles" / "Default" / "modlist.txt").exists()


async def test_teardown_es_idempotente(manager: GrassProfileManager) -> None:
    # Sin haber creado nada, y dos veces seguidas: no debe fallar.
    assert await manager.teardown() == []
    await manager.create_clone_profile()
    await manager.teardown()
    await manager.teardown()


async def test_teardown_reporta_fallos_e_intenta_ambos(
    mo2_root: pathlib.Path, manager: GrassProfileManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1.6: si el borrado del clon lanza, teardown igual intenta el mod y
    devuelve el path del clon como fallo (en vez de tragarlo o cortar)."""
    await manager.create_clone_profile()
    await manager.build_config_mod(["Tamriel"])
    clon = mo2_root / "profiles" / "SkyClaw-GrassCache"
    mod = mo2_root / "mods" / "SkyClaw - Grass Precache Config"

    import sky_claw.local.mo2.grass_profile as gp

    def _rmtree_selectivo(path: pathlib.Path) -> None:
        if path == clon:
            raise PermissionError("handle abierto por un SkyrimSE huérfano (Windows)")
        _borrar_real(path)

    _borrar_real = gp._rmtree_force
    monkeypatch.setattr(gp, "_rmtree_force", _rmtree_selectivo)

    fallidos = await manager.teardown()

    assert fallidos == [clon]
    assert clon.exists(), "el clon quedó (borrado falló) y se reporta"
    assert not mod.exists(), "el mod SÍ se intentó y borró pese al fallo previo"
