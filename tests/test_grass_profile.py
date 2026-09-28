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
- El mod de config nace con ``meta.ini`` válido y **máxima prioridad entre mods
  regulares** (primera línea de mods del ``modlist.txt``); ``overwrite`` sigue por
  encima y se valida para que no imponga bytes distintos en los dos INIs.
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
from types import SimpleNamespace

import pytest

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import (
    GrassProfileError,
    GrassProfileManager,
)
from sky_claw.local.mo2.profile_sandbox import (
    ProfileNotFoundError,
    SandboxSymlinkError,
)
from sky_claw.local.mo2.vfs import MO2Controller
from tests._symlink_guard import crear_junction, junction_guard


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


async def _capturar_configs_generadas(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> tuple[dict[str, bytes], bytes, pathlib.Path]:
    """Genera una referencia de bytes y restaura el modlist inicial del clon."""
    await manager.create_clone_profile()
    modlist = mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt"
    modlist_before = modlist.read_bytes()
    mod_dir = await manager.build_config_mod(["Tamriel"])
    generated = {
        "SKSE/Plugins/GrassControl.ini": (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_bytes(),
        "SKSE/Plugins/SSEDisplayTweaks.ini": (mod_dir / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini").read_bytes(),
    }
    # El test empieza el escenario del gate desde el mismo estado de modlist,
    # sin la inserción de la generación de referencia.
    modlist.write_bytes(modlist_before)
    return generated, modlist_before, modlist


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

    # El mod queda con máxima prioridad ENTRE mods regulares: en MO2 la PRIMERA
    # línea de mods del modlist.txt es la de mayor prioridad regular. overwrite
    # permanece por encima y su compatibilidad se comprueba antes del writer.
    clon_modlist = (mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt").read_text(encoding="utf-8-sig")
    lineas = [ln.strip() for ln in clon_modlist.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert lineas[0] == "+SkyClaw - Grass Precache Config"


async def test_build_config_mod_sin_directorio_overwrite(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> None:
    """Un overwrite ausente no impide construir y habilitar el mod de config."""
    (mo2_root / "overwrite").rmdir()
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel"])

    assert (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").is_file()
    modlist = (mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt").read_text(encoding="utf-8-sig")
    assert modlist.splitlines()[0] == "+SkyClaw - Grass Precache Config"


async def test_overwrite_gate_usa_data_root_de_la_instancia(tmp_path: pathlib.Path) -> None:
    """El gate sigue data_root de MO2, no install_root ni mods_dir configurables."""
    install_root = tmp_path / "MO2_Install"
    data_root = tmp_path / "MO2_Instance_Data"
    mods_dir = tmp_path / "Custom_Mods"
    for directory in (install_root, data_root, mods_dir):
        directory.mkdir()
    (install_root / "ModOrganizer.exe").write_bytes(b"fake-exe")
    profile = data_root / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "modlist.txt").write_bytes(_MODLIST)

    # Una trampa en INSTALL_ROOT no representa el overwrite de esta instancia.
    install_decoy = install_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    install_decoy.parent.mkdir(parents=True)
    install_decoy.write_bytes(b"different install-root bytes")

    validator = PathValidator(roots=[tmp_path])
    controller = MO2Controller(
        install_root=install_root,
        data_root=data_root,
        mods_dir=mods_dir,
        path_validator=validator,
    )
    manager = GrassProfileManager(
        install_root=install_root,
        data_root=data_root,
        mods_dir=mods_dir,
        path_validator=validator,
        controller=controller,
    )
    await manager.create_clone_profile()

    config_mod = await manager.build_config_mod(["Tamriel"])
    assert config_mod == mods_dir / "SkyClaw - Grass Precache Config"
    assert install_decoy.read_bytes() == b"different install-root bytes"

    modlist = data_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt"
    modlist_before = modlist.read_bytes()
    data_conflict = data_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    data_conflict.parent.mkdir(parents=True)
    data_conflict.write_bytes(b"different instance-data bytes")
    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*tamaño distinto"):
        await manager.build_config_mod(["Tamriel"])
    assert modlist.read_bytes() == modlist_before


async def test_overwrite_byte_identico_aceptado_para_ambos_configs(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> None:
    """Los dos outputs exactos en overwrite equivalen al overlay generado y pasan."""
    generated, _modlist_before, modlist = await _capturar_configs_generadas(manager, mo2_root)
    overwrite = mo2_root / "overwrite"
    for relative, content in generated.items():
        path = overwrite.joinpath(*pathlib.PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    mod_dir = await manager.build_config_mod(["Tamriel"])

    assert (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_bytes() == generated[
        "SKSE/Plugins/GrassControl.ini"
    ]
    assert (mod_dir / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini").read_bytes() == generated[
        "SKSE/Plugins/SSEDisplayTweaks.ini"
    ]
    assert modlist.read_bytes() != _modlist_before
    assert modlist.read_text(encoding="utf-8-sig").splitlines()[0] == "+SkyClaw - Grass Precache Config"


async def test_overwrite_grasscontrol_diferente_bloquea_sin_mutar_modlist(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> None:
    """GrassControl conflictivo falla con error de dominio y conserva modlist byte a byte."""
    generated, modlist_before, modlist = await _capturar_configs_generadas(manager, mo2_root)
    relative = "SKSE/Plugins/GrassControl.ini"
    conflicting = generated[relative].replace(b"Use-grass-cache=True", b"Use-grass-cache=Fake", 1)
    assert len(conflicting) == len(generated[relative]) and conflicting != generated[relative]
    path = mo2_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    path.parent.mkdir(parents=True)
    path.write_bytes(conflicting)

    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*bytes diferentes"):
        await manager.build_config_mod(["Tamriel"])

    assert modlist.read_bytes() == modlist_before
    assert "+SkyClaw - Grass Precache Config" not in modlist.read_text(encoding="utf-8-sig")


async def test_overwrite_ssedisplaytweaks_diferente_bloquea_sin_mutar_modlist(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> None:
    """SSEDisplayTweaks también queda cubierto; el caso usa bytes distintos del mismo tamaño."""
    generated, modlist_before, modlist = await _capturar_configs_generadas(manager, mo2_root)
    relative = "SKSE/Plugins/SSEDisplayTweaks.ini"
    conflicting = generated[relative].replace(b"Resolution=800x400", b"Resolution=800x401", 1)
    assert len(conflicting) == len(generated[relative]) and conflicting != generated[relative]
    path = mo2_root / "overwrite" / "SKSE" / "Plugins" / "SSEDisplayTweaks.ini"
    path.parent.mkdir(parents=True)
    path.write_bytes(conflicting)

    with pytest.raises(GrassProfileError, match=r"SSEDisplayTweaks\.ini.*overwrite.*bytes diferentes"):
        await manager.build_config_mod(["Tamriel"])

    assert modlist.read_bytes() == modlist_before


async def test_overwrite_igual_y_distinto_bloquea_universalmente(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
) -> None:
    """Un output idéntico no oculta un conflicto del segundo output generado."""
    generated, modlist_before, modlist = await _capturar_configs_generadas(manager, mo2_root)
    overwrite = mo2_root / "overwrite" / "SKSE" / "Plugins"
    overwrite.mkdir(parents=True)
    (overwrite / "GrassControl.ini").write_bytes(generated["SKSE/Plugins/GrassControl.ini"])
    (overwrite / "SSEDisplayTweaks.ini").write_bytes(
        generated["SKSE/Plugins/SSEDisplayTweaks.ini"].replace(b"Resolution=800x400", b"Resolution=800x401", 1)
    )

    with pytest.raises(GrassProfileError, match=r"SSEDisplayTweaks\.ini.*overwrite.*bytes diferentes"):
        await manager.build_config_mod(["Tamriel"])

    assert modlist.read_bytes() == modlist_before


async def test_overwrite_ilegible_bloquea_fail_closed(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un error de lectura no se interpreta como ausencia de overlay."""
    import sky_claw.local.mo2.grass_profile as grass_profile

    generated, modlist_before, modlist = await _capturar_configs_generadas(manager, mo2_root)
    overwrite_file = mo2_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    overwrite_file.parent.mkdir(parents=True)
    overwrite_file.write_bytes(generated["SKSE/Plugins/GrassControl.ini"])
    read_real = grass_profile._read_file_bytes_link_safe

    def _read_with_permission_error(path: pathlib.Path, expected: object) -> bytes:
        if path == overwrite_file:
            raise PermissionError("synthetic unreadable overwrite")
        return read_real(path, expected)  # type: ignore[arg-type]

    monkeypatch.setattr(grass_profile, "_read_file_bytes_link_safe", _read_with_permission_error)

    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*synthetic unreadable"):
        await manager.build_config_mod(["Tamriel"])

    assert modlist.read_bytes() == modlist_before


@_symlink_guard
async def test_overwrite_symlink_bloquea_sin_seguir_target(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Un archivo de overwrite enlazado se bloquea antes de leer su target."""
    await manager.create_clone_profile()
    modlist = mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt"
    modlist_before = modlist.read_bytes()
    target = tmp_path / "external.ini"
    target.write_bytes(b"external bytes")
    overwrite_file = mo2_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    overwrite_file.parent.mkdir(parents=True)
    overwrite_file.symlink_to(target)

    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*symlink"):
        await manager.build_config_mod(["Tamriel"])

    assert target.read_bytes() == b"external bytes"
    assert overwrite_file.is_symlink()
    assert modlist.read_bytes() == modlist_before


async def test_overwrite_reparse_tag_no_clasificado_bloquea(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un reparse tag no clasificado también bloquea mediante el seam portable."""
    import sky_claw.local.mo2.grass_profile as grass_profile

    await manager.create_clone_profile()
    modlist = mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt"
    modlist_before = modlist.read_bytes()
    overwrite_file = mo2_root / "overwrite" / "SKSE" / "Plugins" / "GrassControl.ini"
    overwrite_file.parent.mkdir(parents=True)
    overwrite_file.write_bytes(b"present but not trusted")
    inspect_real = grass_profile.link_kind_and_identity_or_raise_with_retry

    def _inspect_with_unknown_reparse(path: pathlib.Path) -> tuple[str | None, object | None]:
        link_kind, info = inspect_real(path)
        if path == overwrite_file and info is not None:
            return link_kind, SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_reparse_tag=0x9000001A,
            )
        return link_kind, info

    monkeypatch.setattr(grass_profile, "link_kind_and_identity_or_raise_with_retry", _inspect_with_unknown_reparse)

    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*reparse tag"):
        await manager.build_config_mod(["Tamriel"])

    assert overwrite_file.read_bytes() == b"present but not trusted"
    assert modlist.read_bytes() == modlist_before


@junction_guard
async def test_overwrite_junction_bloquea(
    manager: GrassProfileManager,
    mo2_root: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Un junction en un componente del overwrite se rechaza en Windows."""
    await manager.create_clone_profile()
    modlist = mo2_root / "profiles" / "SkyClaw-GrassCache" / "modlist.txt"
    modlist_before = modlist.read_bytes()
    target = tmp_path / "external-overwrite"
    (target / "Plugins").mkdir(parents=True)
    (target / "Plugins" / "GrassControl.ini").write_bytes(b"external bytes")
    overwrite_skse = mo2_root / "overwrite" / "SKSE"
    motivo = crear_junction(overwrite_skse, target)
    assert motivo is None, motivo

    with pytest.raises(GrassProfileError, match=r"GrassControl\.ini.*overwrite.*junction"):
        await manager.build_config_mod(["Tamriel"])

    assert (target / "Plugins" / "GrassControl.ini").read_bytes() == b"external bytes"
    assert modlist.read_bytes() == modlist_before


async def test_grasscontrol_ini_worldspaces_y_flags(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod(["Tamriel", "DLC2SolstheimWorld"])

    grass_ini = (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_text(encoding="utf-8")
    # NGIO-NG parsea GrassControl.ini con CSimpleIniA y lee las claves bajo la
    # sección [GrassConfig] (verificado en el source: include/GrassControl/Config.h
    # define iniPath="Data/SKSE/Plugins/GrassControl.ini"; src/Config.cpp hace
    # ReadBoolSetting(ini, "GrassConfig", "Use-grass-cache", ...)). Sin el header,
    # las claves quedan en la sección vacía y el plugin las ignora.
    assert "[GrassConfig]" in grass_ini
    assert grass_ini.index("[GrassConfig]") < grass_ini.index("Use-grass-cache")
    # En modo-sección el IniEditor escribe key=value (sin espacios, estilo INT
    # clásico); CSimpleIniA trimea al leer, así que el plugin lo acepta. El valor
    # "True" también: GetBoolValue de SimpleIni mira solo el 1er char ('T'→true).
    assert "Use-grass-cache=True" in grass_ini
    assert "Only-load-from-cache=True" in grass_ini
    # Clave NGIO-NG con guiones, worldspaces entre comillas separados por ';'.
    assert 'Only-pregenerate-world-spaces="Tamriel;DLC2SolstheimWorld"' in grass_ini


async def test_worldspaces_vacio_escribe_comillas_vacias(manager: GrassProfileManager) -> None:
    await manager.create_clone_profile()

    mod_dir = await manager.build_config_mod([])

    grass_ini = (mod_dir / "SKSE" / "Plugins" / "GrassControl.ini").read_text(encoding="utf-8")
    # Modo-sección: el IniEditor escribe key=value (sin espacios).
    assert 'Only-pregenerate-world-spaces=""' in grass_ini


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
    # Modo-sección: el IniEditor escribe key=value (sin espacios).
    assert "Use-grass-cache=False" in grass_ini  # override
    assert "Extra-flag=1" in grass_ini  # clave nueva
    # Los defaults no pisados siguen presentes.
    assert "Only-load-from-cache=True" in grass_ini


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


@junction_guard
async def test_mod_de_config_junction_no_borra_su_target(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    """Mismo caso que el symlink, con un junction de Windows.

    ``raw_mod_dir.is_symlink()`` es **ciego** a los junctions (``os.path.islink()``
    da False para un ``IO_REPARSE_TAG_MOUNT_POINT``) — el fail-closed anterior sólo
    cubría symlinks y dejaba pasar exactamente el reparse point que
    ``PathValidator.validate()`` resuelve y que ``_scaffold_mod_sync`` borraría con
    ``_rmtree_force`` sobre el destino ajeno.
    """
    await manager.create_clone_profile()
    otro = mo2_root / "mods" / "OtroModImportante"
    otro.mkdir()
    (otro / "importante.esp").write_bytes(b"no-borrar")
    enlace = mo2_root / "mods" / "SkyClaw - Grass Precache Config"
    motivo = crear_junction(enlace, otro)
    assert motivo is None, motivo

    with pytest.raises(GrassProfileError):
        await manager.build_config_mod(["Tamriel"])

    # El árbol al que apunta el junction quedó intacto.
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


@_symlink_guard
async def test_teardown_no_borra_un_objetivo_enlazado(mo2_root: pathlib.Path, manager: GrassProfileManager) -> None:
    """El teardown hereda el fail-closed que ``build_config_mod`` ya tenía.

    ``mods/<config_mod>`` es LITERALMENTE la misma ruta que
    :meth:`create_config_mod` protege desde #404 con ``link_kind`` sobre la ruta
    cruda. Tener el guard sólo en el camino de creación y no en el de borrado
    era la asimetría entre hermanos **dentro de un mismo archivo** que este repo
    nombra como su defecto dominante.

    El objetivo enlazado se reporta como fallido —para que el operador lo vea—
    en vez de borrarse o de tragarse en silencio.
    """
    ajeno = mo2_root / "mods" / "OtroModImportante"
    ajeno.mkdir(parents=True)
    (ajeno / "importante.esp").write_bytes(b"no-borrar")
    enlazado = mo2_root / "mods" / "SkyClaw - Grass Precache Config"
    enlazado.symlink_to(ajeno, target_is_directory=True)

    fallidos = await manager.teardown()

    assert enlazado in fallidos, "un objetivo enlazado debe reportarse, no borrarse"
    assert (ajeno / "importante.esp").read_bytes() == b"no-borrar"
    assert enlazado.is_symlink(), "el enlace tampoco se toca"


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


async def test_grass_profile_manager_con_raices_separadas(tmp_path: pathlib.Path) -> None:
    """Issue #557: GrassProfileManager opera correctamente con install != data != mods.

    Verifica que el clon se crea en data_root/profiles, el mod en mods_dir, y que
    ninguna operación contamina install_root ni data_root/mods (trampas).
    """
    install_dir = tmp_path / "MO2_Install"
    install_dir.mkdir()
    (install_dir / "ModOrganizer.exe").write_bytes(b"fake exe")

    data_dir = tmp_path / "MO2_Data"
    profile = data_dir / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "modlist.txt").write_bytes(_MODLIST)
    (profile / "plugins.txt").write_bytes(_PLUGINS)
    (profile / "Skyrim.ini").write_bytes(_SKYRIM_INI)
    (profile / "settings.txt").write_bytes(_SETTINGS)
    (data_dir / "overwrite").mkdir()

    mods_dir = tmp_path / "MO2_Mods"
    mods_dir.mkdir()

    # Trampas: directorios mods/ en install y en data deben quedar intactos y vacíos.
    trampa_install_mods = install_dir / "mods"
    trampa_install_mods.mkdir(parents=True)
    trampa_data_mods = data_dir / "mods"
    trampa_data_mods.mkdir(parents=True)

    validator = PathValidator(roots=[tmp_path])
    mgr = GrassProfileManager(
        install_root=install_dir,
        data_root=data_dir,
        mods_dir=mods_dir,
        path_validator=validator,
        source_profile="Default",
    )

    assert mgr.install_root == install_dir.resolve()
    assert mgr.data_root == data_dir.resolve()
    assert mgr.mods_dir == mods_dir.resolve()

    # 1. create_clone_profile: clon en data_root / profiles
    clon = await mgr.create_clone_profile()
    assert clon == data_dir / "profiles" / "SkyClaw-GrassCache"
    assert clon.is_dir()
    assert not (install_dir / "profiles").exists()

    # 2. build_config_mod: mod en mods_dir
    mod_path = await mgr.build_config_mod(["Tamriel"])
    assert mod_path == mods_dir / "SkyClaw - Grass Precache Config"
    assert (mod_path / "SKSE" / "Plugins" / "GrassControl.ini").is_file()
    assert list(trampa_install_mods.iterdir()) == []
    assert list(trampa_data_mods.iterdir()) == []

    # 3. teardown: limpia clon en data_root y mod en mods_dir
    fallidos = await mgr.teardown()
    assert fallidos == []
    assert not clon.exists()
    assert not mod_path.exists()
    assert list(trampa_install_mods.iterdir()) == []
    assert list(trampa_data_mods.iterdir()) == []


def test_grass_profile_manager_adopta_raices_de_controller(tmp_path: pathlib.Path) -> None:
    """GrassProfileManager adopta install_root, data_root y mods_dir del controller provisto."""
    install = tmp_path / "MO2_Install"
    data = tmp_path / "MO2_Data"
    mods = tmp_path / "MO2_Mods"
    for p in (install, data, mods):
        p.mkdir()
    (install / "ModOrganizer.exe").write_bytes(b"fake exe")

    validator = PathValidator(roots=[tmp_path])
    ctrl = MO2Controller(
        install_root=install,
        data_root=data,
        mods_dir=mods,
        path_validator=validator,
    )

    mgr = GrassProfileManager(controller=ctrl, path_validator=validator)
    assert mgr.install_root == install.resolve()
    assert mgr.data_root == data.resolve()
    assert mgr.mods_dir == mods.resolve()

    mgr2 = GrassProfileManager(
        install_root=install,
        data_root=data,
        mods_dir=mods,
        controller=ctrl,
        path_validator=validator,
    )
    assert mgr2.install_root == install.resolve()


def test_grass_profile_manager_rechaza_raices_divergentes_de_controller(tmp_path: pathlib.Path) -> None:
    """GrassProfileManager rechaza con ValueError si las raíces explícitas difieren del controller."""
    install = tmp_path / "MO2_Install"
    data = tmp_path / "MO2_Data"
    mods = tmp_path / "MO2_Mods"
    otradir = tmp_path / "Other"
    for p in (install, data, mods, otradir):
        p.mkdir()
    (install / "ModOrganizer.exe").write_bytes(b"fake exe")

    validator = PathValidator(roots=[tmp_path])
    ctrl = MO2Controller(
        install_root=install,
        data_root=data,
        mods_dir=mods,
        path_validator=validator,
    )

    with pytest.raises(ValueError, match="install_root explícito .* diverge del controller"):
        GrassProfileManager(install_root=otradir, controller=ctrl, path_validator=validator)

    with pytest.raises(ValueError, match="data_root explícito .* diverge del controller"):
        GrassProfileManager(data_root=otradir, controller=ctrl, path_validator=validator)

    with pytest.raises(ValueError, match="mods_dir explícito .* diverge del controller"):
        GrassProfileManager(mods_dir=otradir, controller=ctrl, path_validator=validator)

    with pytest.raises(ValueError, match="mo2_root explícito .* diverge del controller"):
        GrassProfileManager(mo2_root=otradir, controller=ctrl, path_validator=validator)


def test_grass_profile_manager_rechaza_raices_parciales(tmp_path: pathlib.Path) -> None:
    """Modo explícito exige la terna install_root + data_root + mods_dir."""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    validator = PathValidator(roots=[tmp_path])

    with pytest.raises(ValueError, match="exige install_root, data_root y mods_dir completos"):
        GrassProfileManager(install_root=d1, path_validator=validator)

    with pytest.raises(ValueError, match="exige install_root, data_root y mods_dir completos"):
        GrassProfileManager(data_root=d1, path_validator=validator)

    with pytest.raises(ValueError, match="exige install_root, data_root y mods_dir completos"):
        GrassProfileManager(mods_dir=d1, path_validator=validator)

    with pytest.raises(ValueError, match="exige install_root, data_root y mods_dir completos"):
        GrassProfileManager(install_root=d1, data_root=d2, path_validator=validator)


def test_grass_profile_manager_exige_path_validator_incluso_con_controller(tmp_path: pathlib.Path) -> None:
    """path_validator es obligatorio; no se extrae de controller._validator."""
    install = tmp_path / "MO2_Install"
    data = tmp_path / "MO2_Data"
    mods = tmp_path / "MO2_Mods"
    for p in (install, data, mods):
        p.mkdir()
    (install / "ModOrganizer.exe").write_bytes(b"fake exe")

    validator = PathValidator(roots=[tmp_path])
    ctrl = MO2Controller(
        install_root=install,
        data_root=data,
        mods_dir=mods,
        path_validator=validator,
    )

    with pytest.raises(ValueError, match="path_validator es obligatorio"):
        GrassProfileManager(controller=ctrl)


async def test_grass_integracion_custom_mod_directory_y_trampas(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integración Grass con custom mod_directory:
    - Instancia portable sin metadata de mod_directory
    - Variable MO2_MODS_PATH apuntando a carpeta custom externa
    - Flujo Grass completo (create_clone_profile + build_config_mod)
    - Mod de configuración se escribe EXCLUSIVAMENTE en la carpeta custom
    - Trampas <data>/mods e <install>/mods quedan intactas.
    """
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.orchestrator.grass_runtime_deps import GrassRuntimeDepsProvider

    mo2_dir = tmp_path / "MO2_Portable"
    mo2_dir.mkdir()
    (mo2_dir / "ModOrganizer.exe").write_bytes(b"fake-exe")

    profile = mo2_dir / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "modlist.txt").write_bytes(_MODLIST)
    (profile / "plugins.txt").write_bytes(_PLUGINS)
    (profile / "Skyrim.ini").write_bytes(_SKYRIM_INI)
    (profile / "settings.txt").write_bytes(_SETTINGS)
    (mo2_dir / "overwrite").mkdir()

    game_dir = tmp_path / "Skyrim"
    game_dir.mkdir()
    (game_dir / "Data").mkdir()
    (game_dir / "SkyrimSE.exe").write_bytes(b"fake-exe")

    custom_mods_dir = tmp_path / "External_Custom_Mods"
    custom_mods_dir.mkdir()

    # Trampas de regresión: <data>/mods e <install>/mods
    trampa_install_mods = mo2_dir / "mods"
    trampa_install_mods.mkdir()

    monkeypatch.setenv("MO2_MODS_PATH", str(custom_mods_dir))
    monkeypatch.setenv("SKYRIM_PATH", str(game_dir))

    validator = PathValidator(roots=[tmp_path])
    path_resolver = PathResolutionService(
        path_validator=validator,
        profile_name="Default",
        mo2_install_dir=mo2_dir,
    )

    provider = GrassRuntimeDepsProvider(
        path_resolver=path_resolver,
        path_validator=validator,
        profile_name="Default",
    )

    deps = provider()
    assert deps is not None
    pm = deps.profile_manager

    # 1. Crear clon
    clon = await pm.create_clone_profile()
    assert clon.is_dir()
    assert clon == mo2_dir / "profiles" / "SkyClaw-GrassCache"

    # 2. Construir mod de config
    mod_path = await pm.build_config_mod(["Tamriel"])
    assert mod_path == custom_mods_dir / "SkyClaw - Grass Precache Config"
    assert (mod_path / "SKSE" / "Plugins" / "GrassControl.ini").is_file()

    # Verificar que las trampas no fueron contaminadas
    assert list(trampa_install_mods.iterdir()) == []
    assert custom_mods_dir != trampa_install_mods

    # 3. Teardown
    fallidos = await pm.teardown()
    assert fallidos == []
    assert not mod_path.exists()
    assert not clon.exists()
    assert list(trampa_install_mods.iterdir()) == []
