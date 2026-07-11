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
    SandboxDriftError,
    SandboxLocationError,
    SandboxRollbackError,
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
    # El diff se mide contra el baseline de clone-time: sigue describiendo lo
    # que hizo el ritual aunque ya se haya promovido (el clon post-promoción
    # está gastado — se descarta, no se reutiliza).
    assert len((await sandbox.diff(clone)).changes) == 3


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


# ---------------------------------------------------------------------------
# Baseline y drift (reviews Codex PR #245)
# ---------------------------------------------------------------------------


async def test_diff_refleja_solo_lo_que_hizo_el_ritual(mo2_root: pathlib.Path) -> None:
    """El diff compara contra el baseline de clone-time, no contra el estado
    real vivo: un archivo que MO2/el usuario creó en el real durante la ventana
    de aprobación NO aparece como 'removed' del ritual."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    # El lado real cambia después del clonado (MO2 abierto, usuario, etc.).
    (mo2_root / "overwrite" / "creado_por_mo2.txt").write_bytes(b"vivo")

    diff = await sandbox.diff(clone)

    assert diff.is_empty  # el ritual no hizo nada; el drift del real no es suyo


async def test_promote_con_drift_del_real_se_rechaza(mo2_root: pathlib.Path) -> None:
    """Si el real cambió desde el clonado, promover a ciegas borraría o
    pisaría esos cambios vivos: fail-closed con error tipado, sin escribir."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")
    vivo = mo2_root / "overwrite" / "creado_por_mo2.txt"
    vivo.write_bytes(b"vivo")  # drift en la ventana de aprobación

    with pytest.raises(SandboxDriftError):
        await sandbox.promote(clone)

    # Nada se aplicó y el archivo vivo sobrevive.
    assert vivo.read_bytes() == b"vivo"
    assert not (mo2_root / "overwrite" / "SkyClaw_Patch.esp").exists()


@_symlink_guard
async def test_promote_con_symlink_en_el_real_se_rechaza(mo2_root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Un symlink aparecido en el árbol real es destino inseguro para promote:
    se corta antes de escribir a través de él."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")
    fuera = tmp_path / "target_externo.txt"
    fuera.write_bytes(b"externo")
    (mo2_root / "overwrite" / "link.txt").symlink_to(fuera)

    with pytest.raises(SandboxSymlinkError):
        await sandbox.promote(clone)

    assert not (mo2_root / "overwrite" / "SkyClaw_Patch.esp").exists()


def test_sandbox_dentro_del_overwrite_es_rechazado(mo2_root: pathlib.Path) -> None:
    """El overwrite es un área clonada: un sandbox adentro contaminaría el
    propio árbol que se clona (recursión / artefactos en el diff)."""
    with pytest.raises(SandboxLocationError):
        ProfileSandbox(mo2_root=mo2_root, sandbox_root=mo2_root / "overwrite" / "sandbox")


async def test_promote_reemplaza_archivo_por_directorio(mo2_root: pathlib.Path) -> None:
    """removed se aplica antes que added: un ritual que reemplaza un archivo
    por un directorio con hijos debe promoverse completo, no fallar a medias."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    log = clone.overwrite_copy / "SKSE" / "plugin.log"
    log.unlink()
    log.mkdir()
    (log / "nuevo.txt").write_bytes(b"hijo")

    resultado = await sandbox.promote(clone)

    real = mo2_root / "overwrite" / "SKSE" / "plugin.log"
    assert real.is_dir()
    assert (real / "nuevo.txt").read_bytes() == b"hijo"
    assert resultado.files_deleted == 1
    assert resultado.files_written == 1


async def test_promote_no_deja_temporales(mo2_root: pathlib.Path) -> None:
    """La escritura es atómica (tmp + replace en el mismo directorio): tras
    promover no quedan archivos temporales en el árbol real."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n")

    await sandbox.promote(clone)

    residuos = [p for p in (mo2_root / "profiles" / "Default").rglob("*") if "skyclaw-tmp" in p.name]
    assert residuos == []


# ---------------------------------------------------------------------------
# Promote transaccional: rollback ante fallo de I/O a mitad (pre-T-27b)
# ---------------------------------------------------------------------------


def _snapshot_tree(root: pathlib.Path) -> dict[str, bytes]:
    """Foto byte-exacta de un árbol: {ruta relativa posix: contenido}."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


async def test_promote_con_fallo_a_mitad_hace_rollback_completo(mo2_root: pathlib.Path, monkeypatch) -> None:
    """El tmp+os.replace protege cada archivo, no el conjunto: si el cambio N
    falla, los N-1 anteriores ya estaban aplicados y el perfil quedaba mitad
    viejo/mitad nuevo. El promote debe ser todo-o-nada: ante un OSError a
    mitad, el árbol real vuelve byte-exacto al estado previo (incluidos los
    removed, que se aplican primero)."""
    import os as os_mod

    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    # 4 cambios mezclados: modified x2 (profile), added y removed (overwrite).
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n")
    (clone.profile_copy / "modlist.txt").write_bytes(b"\xef\xbb\xbf+ModB\r\n-ModA\r\n")
    (clone.overwrite_copy / "SkyClaw_Patch.esp").write_bytes(b"TES4-fake")
    (clone.overwrite_copy / "SKSE" / "plugin.log").unlink()

    foto_profile = _snapshot_tree(mo2_root / "profiles" / "Default")
    foto_overwrite = _snapshot_tree(mo2_root / "overwrite")

    # Falla SOLO la 2ª llamada a os.replace (el 1er modified ya se aplicó y el
    # removed también); las siguientes pasan para que el rollback pueda usarlas.
    replace_real = os_mod.replace
    llamadas = {"n": 0}

    def replace_con_fallo(src, dst, **kwargs):
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise OSError("disco falló a mitad del promote")
        return replace_real(src, dst, **kwargs)

    monkeypatch.setattr(os_mod, "replace", replace_con_fallo)

    with pytest.raises(OSError, match="disco falló"):
        await sandbox.promote(clone)

    assert _snapshot_tree(mo2_root / "profiles" / "Default") == foto_profile
    assert _snapshot_tree(mo2_root / "overwrite") == foto_overwrite


async def test_promote_con_fallo_en_unlink_restaura_borrados(mo2_root: pathlib.Path, monkeypatch) -> None:
    """Los removed se aplican primero con unlink: un fallo en el 2º borrado
    debe reponer el 1º desde el backup, no dejar el borrado ya efectivo."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    (clone.overwrite_copy / "SKSE" / "plugin.log").unlink()
    (clone.overwrite_copy / "textures" / "foo.dds").unlink()

    foto_overwrite = _snapshot_tree(mo2_root / "overwrite")

    # Falla SOLO el unlink del target real foo.dds (filtrado por ruta exacta:
    # los unlink de temporales y del clon deben seguir funcionando).
    objetivo = mo2_root / "overwrite" / "textures" / "foo.dds"
    unlink_real = pathlib.Path.unlink

    def unlink_con_fallo(self: pathlib.Path, missing_ok: bool = False) -> None:
        if self == objetivo:
            raise OSError("disco falló en el unlink")
        unlink_real(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink_con_fallo)

    with pytest.raises(OSError, match="disco falló"):
        await sandbox.promote(clone)

    assert _snapshot_tree(mo2_root / "overwrite") == foto_overwrite


async def test_promote_exitoso_limpia_el_rollback(mo2_root: pathlib.Path) -> None:
    """Tras un promote exitoso no queda el directorio de rollback en el clon
    ni temporales en el árbol real (regresión del cleanup)."""
    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n")
    (clone.overwrite_copy / "SKSE" / "plugin.log").unlink()

    await sandbox.promote(clone)

    assert list(clone.root.glob("rollback*")) == []
    residuos = [p for p in (mo2_root / "overwrite").rglob("*") if "skyclaw-tmp" in p.name]
    assert residuos == []


async def test_promote_doble_fallo_lanza_rollback_error(mo2_root: pathlib.Path, monkeypatch) -> None:
    """Si el promote falla Y el rollback también, se lanza SandboxRollbackError
    con la ruta del backup en el mensaje y el directorio de rollback se
    preserva para restauración manual."""
    import os as os_mod

    sandbox = ProfileSandbox(mo2_root=mo2_root)
    clone = await sandbox.clone()

    (clone.profile_copy / "modlist.txt").write_bytes(b"\xef\xbb\xbf+ModB\r\n-ModA\r\n")
    (clone.profile_copy / "plugins.txt").write_bytes(b"\xef\xbb\xbf*USSEP.esp\r\n*Skyrim.esm\r\n")

    # La 1ª llamada pasa (modlist.txt aplicado); todas las siguientes fallan:
    # falla el apply de plugins.txt Y el os.replace del rollback de modlist.txt.
    replace_real = os_mod.replace
    llamadas = {"n": 0}

    def replace_con_fallo(src, dst, **kwargs):
        llamadas["n"] += 1
        if llamadas["n"] >= 2:
            raise OSError("disco muerto")
        return replace_real(src, dst, **kwargs)

    monkeypatch.setattr(os_mod, "replace", replace_con_fallo)

    with pytest.raises(SandboxRollbackError) as excinfo:
        await sandbox.promote(clone)

    rollback_dirs = list(clone.root.glob("rollback*"))
    assert rollback_dirs, "el directorio de rollback debe preservarse para restauración manual"
    assert str(rollback_dirs[0]) in str(excinfo.value)
    # El backup del archivo aplicado sigue disponible para restaurar a mano.
    assert (rollback_dirs[0] / "profile" / "modlist.txt").read_bytes() == _MODLIST
