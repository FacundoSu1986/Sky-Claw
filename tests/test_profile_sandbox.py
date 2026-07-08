"""Tests del ``ProfileSandbox`` (T-27 de TECHNICAL_REVIEW_TASKS.md, ADR 0002).

El flujo must-have de la "caja negra de vuelo" empieza por *clonar el perfil
MO2*: los rituales mutantes operan sobre una copia aislada — que incluye el
**overwrite compartido** de MO2, porque Synthesis/Pandora escriben ahí, fuera
del árbol del perfil — y el perfil real solo se toca al *promover* un diff
aprobado. Estos tests anclan el núcleo del servicio: clonado byte-fiel
(BOM/CRLF incluidos), aislamiento, diff explicable y promoción.

El clon vive FUERA de ``profiles/`` para que MO2 nunca lo cargue como perfil
(interacción con el VFS documentada en el backlog).
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

import pytest

from sky_claw.local.mo2.profile_sandbox import (
    ProfileNotFoundError,
    ProfileSandbox,
    SandboxLocationError,
    SandboxSymlinkError,
)


def _puede_crear_symlinks() -> bool:
    """En Windows crear symlinks requiere privilegios; mismo guard que test_preflight_wiring."""
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

# Contenidos byte-exactos: BOM UTF-8 + CRLF, como los escribe MO2 en Windows.
_PLUGINS = b"\xef\xbb\xbf*Skyrim.esm\r\n*USSEP.esp\r\n"
_MODLIST = b"\xef\xbb\xbf+ModA\r\n-ModB\r\n"
_SETTINGS = b"[General]\r\nskyrim=1\r\n"
_DDS = b"DDS-fake-bytes"
_LOG = b"skse log"


@pytest.fixture
def mo2_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instancia MO2 sintética: perfil Default + overwrite compartido."""
    profile = tmp_path / "mo2" / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(_PLUGINS)
    (profile / "modlist.txt").write_bytes(_MODLIST)
    (profile / "settings.ini").write_bytes(_SETTINGS)

    overwrite = tmp_path / "mo2" / "overwrite"
    (overwrite / "textures").mkdir(parents=True)
    (overwrite / "textures" / "foo.dds").write_bytes(_DDS)
    (overwrite / "SKSE").mkdir(parents=True)
    (overwrite / "SKSE" / "plugin.log").write_bytes(_LOG)

    return tmp_path / "mo2"


# ---------------------------------------------------------------------------
# Clonado
# ---------------------------------------------------------------------------


async def test_clone_copia_byte_fiel_incluyendo_bom(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)

    clone = await sandbox.clone()

    # Perfil: byte-fiel (BOM y CRLF intactos).
    assert (clone.profile_copy / "plugins.txt").read_bytes() == _PLUGINS
    assert (clone.profile_copy / "modlist.txt").read_bytes() == _MODLIST
    assert (clone.profile_copy / "settings.ini").read_bytes() == _SETTINGS
    # Overwrite compartido: también clonado (Synthesis/Pandora escriben ahí).
    assert (clone.overwrite_copy / "textures" / "foo.dds").read_bytes() == _DDS
    assert (clone.overwrite_copy / "SKSE" / "plugin.log").read_bytes() == _LOG


async def test_el_clon_vive_fuera_de_profiles(mo2_root: pathlib.Path) -> None:
    """MO2 lista los perfiles desde profiles/: el clon NO debe aparecer ahí."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)

    clone = await sandbox.clone()

    profiles_dir = (mo2_root / "profiles").resolve()
    assert not clone.profile_copy.resolve().is_relative_to(profiles_dir)
    assert not clone.overwrite_copy.resolve().is_relative_to(profiles_dir)


async def test_mutar_el_clon_no_toca_el_original(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    # Un "ritual" muta la copia: reordena plugins y deja salida en overwrite.
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n")
    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")

    assert (mo2_root / "profiles" / "Default" / "plugins.txt").read_bytes() == _PLUGINS
    assert not (mo2_root / "overwrite" / "SkyClaw_Patch.esp").exists()


# ---------------------------------------------------------------------------
# Diff explicable
# ---------------------------------------------------------------------------


async def test_diff_sin_cambios_es_vacio(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    diff = await sandbox.diff(clone)

    assert diff.is_empty
    assert diff.changes == ()


async def test_diff_reporta_exactamente_los_cambios(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    # modified en el perfil, added y removed en el overwrite.
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n")
    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")
    (clone.overwrite_copy / "SKSE" / "plugin.log").unlink()

    diff = await sandbox.diff(clone)

    cambios = {(c.area, c.relative_path, c.kind) for c in diff.changes}
    assert cambios == {
        ("profile", "plugins.txt", "modified"),
        ("overwrite", "SkyClaw_Patch.esp", "added"),
        ("overwrite", "SKSE/plugin.log", "removed"),
    }
    assert not diff.is_empty


# ---------------------------------------------------------------------------
# Promoción (solo tras aprobación del caller)
# ---------------------------------------------------------------------------


async def test_promote_aplica_los_cambios_al_perfil_real(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    nuevo_orden = b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n"
    (clone.profile_copy / "plugins.txt").write_bytes(nuevo_orden)
    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")
    (clone.overwrite_copy / "SKSE" / "plugin.log").unlink()

    resultado = await sandbox.promote(clone)

    assert resultado.files_written == 2  # plugins.txt + SkyClaw_Patch.esp
    assert resultado.files_deleted == 1  # SKSE/plugin.log
    assert (mo2_root / "profiles" / "Default" / "plugins.txt").read_bytes() == nuevo_orden
    assert (mo2_root / "overwrite" / "SkyClaw_Patch.esp").read_bytes() == b"TES4-fake"
    assert not (mo2_root / "overwrite" / "SKSE" / "plugin.log").exists()
    # Tras promover, el clon y el real coinciden: diff vacío.
    assert (await sandbox.diff(clone)).is_empty


# ---------------------------------------------------------------------------
# Errores tipados y ubicación del sandbox
# ---------------------------------------------------------------------------


async def test_profile_inexistente_lanza_error_tipado(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root, profile="NoExiste")

    with pytest.raises(ProfileNotFoundError):
        await sandbox.clone()


def test_sandbox_dentro_de_profiles_es_rechazado(mo2_root: pathlib.Path) -> None:
    """Un sandbox bajo profiles/ aparecería como perfil en MO2: prohibido."""
    with pytest.raises(SandboxLocationError):
        ProfileSandbox(mo2_root=mo2_root, sandbox_root=mo2_root / "profiles" / "sandbox")


async def test_discard_elimina_el_arbol_del_clon(mo2_root: pathlib.Path) -> None:
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()
    assert clone.root.exists()

    await sandbox.discard(clone)

    assert not clone.root.exists()


# ---------------------------------------------------------------------------
# Robustez (reviews Copilot PR #245)
# ---------------------------------------------------------------------------


async def test_mo2_root_se_normaliza_con_resolve(mo2_root: pathlib.Path) -> None:
    """Un mo2_root con `..` o symlinks se normaliza al construir, como hace
    MO2Controller — las rutas del clon no arrastran segmentos sin resolver."""
    torcido = mo2_root.parent / "mo2" / ".." / "mo2"
    sandbox = ProfileSandbox(mo2_root=torcido)

    clone = await sandbox.clone()

    assert ".." not in clone.profile_source.parts
    assert clone.profile_source == (mo2_root / "profiles" / "Default").resolve()


async def test_promote_sobreescribe_archivo_readonly(mo2_root: pathlib.Path) -> None:
    """Los árboles de mods suelen traer archivos read-only: promote debe
    limpiar el bit de escritura antes de sobreescribir, no morir a medias."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    real = mo2_root / "profiles" / "Default" / "plugins.txt"
    real.chmod(0o444)  # read-only, como los deja más de un instalador de mods
    nuevo = b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n"
    (clone.profile_copy / "plugins.txt").write_bytes(nuevo)

    resultado = await sandbox.promote(clone)

    assert resultado.files_written == 1
    assert real.read_bytes() == nuevo


@_symlink_guard
async def test_symlink_en_el_origen_se_rechaza(mo2_root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Fail-closed: un symlink en el árbol real puede sacar la copia/lectura
    fuera del sandbox (misma política que file_permissions/vfs_health)."""
    fuera = tmp_path / "fuera.txt"
    fuera.write_bytes(b"contenido externo")
    (mo2_root / "overwrite" / "link.txt").symlink_to(fuera)

    sandbox = ProfileSandbox(mo2_root=mo2_root)

    with pytest.raises(SandboxSymlinkError):
        await sandbox.clone()


@_symlink_guard
async def test_symlink_plantado_en_el_clon_se_rechaza(mo2_root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Una herramienta externa corriendo sobre el clon podría plantar un
    symlink; diff/promote deben cortar antes de leer/escribir a través de él."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    fuera = tmp_path / "objetivo.txt"
    fuera.write_bytes(b"target externo")
    (clone.overwrite_copy / "plantado.txt").symlink_to(fuera)

    with pytest.raises(SandboxSymlinkError):
        await sandbox.diff(clone)
