"""D2: canario diferencial MO2/USVFS (path presente en Data físico, bytes distintos).

Un canario diferencial es un archivo de un mod habilitado y efectivo cuyo path
EXISTE en el ``Data`` físico pero cuyos bytes difieren de los del mod. Sirve
porque una ejecución SIN overlay leería los bytes físicos y no superaría el SHA
esperado; una CON overlay lee los bytes del mod y sí lo supera.

Estos tests cubren la selección determinista, el fail-closed de los casos
inseguros y la revalidación temporal (TOCTOU) del estado físico. No ejecutan
MO2, USVFS real, TexGen ni DynDOLOD.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    build_attestation_challenge_for_source,
    verify_vfs_attestation,
)
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsJobResult
from sky_claw.local.mo2.vfs_worker import run_probe_child
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.texgen_handoff import (
    HandoffDriftError,
    TexGenHandoffApproval,
    TexGenHandoffRequest,
    TexGenHandoffResult,
)

_PERFIL = "Perfil-A"
_MOD = "TexGen Output"
_OTRO = "OtroMod"


def _sha(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


@dataclasses.dataclass
class _Escenario:
    data_root: pathlib.Path
    mods_dir: pathlib.Path
    physical_data: pathlib.Path
    virtual_data: pathlib.Path

    @property
    def mod_dir(self) -> pathlib.Path:
        return self.mods_dir / _MOD

    @property
    def artifact_root(self) -> pathlib.Path:
        return self.mod_dir / "textures"

    def escribir_mod(self, mod: str, relativo: str, contenido: bytes) -> pathlib.Path:
        ruta = self.mods_dir / mod / "textures" / relativo
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        return ruta

    def escribir_fisico(self, relativo: str, contenido: bytes) -> pathlib.Path:
        ruta = self.physical_data / "textures" / relativo
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        return ruta

    def montar_vista_virtual(self) -> None:
        """Simula el mapping USVFS: los bytes del mod aparecen en la vista virtual."""
        for archivo in self.artifact_root.rglob("*"):
            if archivo.is_file():
                destino = self.virtual_data / archivo.relative_to(self.mod_dir)
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(archivo, destino)

    def modlist(self, contenido: str) -> None:
        (self.data_root / "profiles" / _PERFIL / "modlist.txt").write_text(contenido, encoding="utf-8")


def _escenario(tmp_path: pathlib.Path, *, modlist: str = f"+{_MOD}\n+{_OTRO}\n") -> _Escenario:
    data_root = tmp_path / "MO2"
    mods_dir = data_root / "mods"
    profile_dir = data_root / "profiles" / _PERFIL
    profile_dir.mkdir(parents=True)
    (profile_dir / "plugins.txt").write_text("", encoding="utf-8")
    (profile_dir / "modlist.txt").write_text(modlist, encoding="utf-8")
    (mods_dir / _MOD / "textures").mkdir(parents=True)
    (mods_dir / _MOD / "meta.ini").write_text("[General]\n", encoding="utf-8")
    (mods_dir / _OTRO).mkdir(parents=True)
    physical_data = tmp_path / "game" / "Data"
    physical_data.mkdir(parents=True)
    virtual_data = tmp_path / "game" / "DataVista"
    virtual_data.mkdir()
    return _Escenario(data_root, mods_dir, physical_data, virtual_data)


def _challenge_del_mod(esc: _Escenario) -> VfsAttestationChallenge:
    return build_attestation_challenge_for_source(
        source_mod=_MOD,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
        permitir_diferencial=True,
    )


def _assert_fisicamente_distinguible(esc: _Escenario, challenge: VfsAttestationChallenge) -> None:
    """Helper que implementa el invariante nuevo (import perezoso: RED primero)."""
    from sky_claw.local.mo2.vfs_attestation import assert_canary_fisicamente_distinguible

    assert_canary_fisicamente_distinguible(challenge=challenge, physical_data_dir=esc.physical_data)


# =============================================================================
# Selección: ausente (comportamiento actual) y diferencial (nuevo)
# =============================================================================


def test_canario_ausente_fisicamente_sigue_siendo_elegible(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")

    challenge = _challenge_del_mod(esc)

    assert challenge.relative_path.as_posix() == "textures/a.dds"
    assert challenge.sha256 == _sha(b"BYTES-DEL-MOD")


def test_canario_fisico_identico_no_prueba_diferenciacion(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"MISMO")
    esc.escribir_fisico("a.dds", b"MISMO")

    with pytest.raises(VfsAttestationError):
        _challenge_del_mod(esc)


def test_canario_fisico_distinto_es_elegible(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"BYTES-FISICOS-DISTINTOS")

    challenge = _challenge_del_mod(esc)

    assert challenge.relative_path.as_posix() == "textures/a.dds"
    assert challenge.sha256 == _sha(b"BYTES-DEL-MOD")


def test_seleccion_diferencial_no_salta_lstat_indeterminado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un OSError de inspección en un candidato NO se salta: falla cerrado.

    ``os.path.lexists`` absorbe el ``OSError`` y devuelve ``False``: sin este
    test, un candidato indeterminado se saltearía y la selección podría usar un
    candidato posterior. El contrato exige cortar, no elegir otro.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD-A")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-DEL-MOD-B")
    esc.escribir_fisico("a.dds", b"FISICO-A")
    esc.escribir_fisico("b.dds", b"FISICO-B")

    import os

    from sky_claw.local.mo2 import vfs_attestation as att

    real_lstat = att.os.lstat
    destino_a = esc.physical_data / "textures" / "a.dds"
    # Clave calculada ANTES del monkeypatch: dentro del fake no se usa
    # Path.resolve(), que en POSIX volvería a entrar a os.lstat.
    objetivo = os.path.normcase(os.path.abspath(os.fspath(destino_a.resolve())))

    def lstat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == objetivo:
            raise PermissionError("share violation simulado")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(att.os, "lstat", lstat_falso)

    with pytest.raises(VfsAttestationError, match="inspeccionar"):
        _challenge_del_mod(esc)


# =============================================================================
# Primera pasada: `Path.exists()` no es una inspección
#
# `Path.exists()` sigue el enlace y absorbe cualquier `OSError` devolviendo
# False. Para el selector eso significa "ausente del Data físico" ⇒ un enlace
# roto, un reparse point colgante o un path ilegible se convertían en el canario
# MÁS FUERTE de la escala. `_inspeccionar_fisico_seguro` distingue los tres
# estados (ausente / regular / no atribuible) y corta en el tercero.
# =============================================================================


def _crear_enlace(enlace: pathlib.Path, objetivo: pathlib.Path, *, directorio: bool) -> bool:
    """Crea un enlace REAL en *enlace*. Devuelve False si la plataforma no lo permite.

    En Windows sin privilegios (``WinError 1314``) se cae a una junction, que es
    un reparse point y sirve para directorios: reproduce el mismo caso que un
    symlink (``Path.exists()`` sigue el target; ``os.lstat`` ve el enlace).

    Ojo: un ``CreateSymbolicLink`` rechazado por privilegio deja un DIRECTORIO
    vacío en la ruta del enlace; hay que retirarlo antes de crear la junction.
    """
    enlace.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(objetivo, enlace, target_is_directory=directorio)
        return True
    except (OSError, NotImplementedError):
        try:
            if os.path.lexists(enlace) and not os.path.islink(enlace):
                os.rmdir(enlace)
        except OSError:
            return False
    if sys.platform != "win32" or not directorio:
        return False
    creado = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(objetivo)], capture_output=True)
    return creado.returncode == 0 and os.path.lexists(enlace)


def test_enlace_roto_no_es_ausencia_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Un enlace colgante EXISTE como enlace: no puede clasificarse como ausente.

    Arrange: el mod aporta `textures/a.dds`; el Data físico tiene ese path como
    enlace cuyo target no existe (`Path.exists()` → False).
    Act: seleccionar canario restringido al mod.
    Assert: falla cerrado por enlace/reparse, no devuelve un challenge "ausente".
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    enlace = esc.physical_data / "textures" / "a.dds"
    if not _crear_enlace(enlace, esc.physical_data / "textures" / "no-existe.bin", directorio=True):
        pytest.skip("la plataforma no permite crear un enlace colgante")
    assert not enlace.exists(), "el enlace debe colgar para reproducir el caso"

    with pytest.raises(VfsAttestationError, match="enlace|reparse"):
        _challenge_del_mod(esc)


def test_enlace_valido_no_es_un_archivo_regular_atribuible(tmp_path: pathlib.Path) -> None:
    """Un symlink válido tampoco es fuente de bytes atribuible.

    Arrange: el mod aporta `a.dds` y `b.dds`; el Data físico tiene `a.dds` como
    symlink a un archivo real y no tiene `b.dds`.
    Act: seleccionar canario restringido al mod.
    Assert: corta en `a.dds`; NO lo saltea para quedarse con `b.dds`.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-B")
    objetivo = tmp_path / "real.bin"
    objetivo.write_bytes(b"X")
    enlace = esc.physical_data / "textures" / "a.dds"
    enlace.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(objetivo, enlace)
    except (OSError, NotImplementedError):
        pytest.skip("la plataforma no permite crear symlinks de archivo")
    assert enlace.exists()

    with pytest.raises(VfsAttestationError, match="enlace|reparse"):
        _challenge_del_mod(esc)


@pytest.mark.skipif(sys.platform != "win32", reason="reparse points son de Windows")
def test_reparse_point_en_la_primera_pasada_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """La junction es el reparse point nativo: `exists()` la ve, `lstat` la delata.

    Arrange: `a.dds` como junction a un directorio real y `b.dds` ausente del
    Data físico (candidato clásico disponible).
    Act: seleccionar canario restringido al mod.
    Assert: corta en la junction; NO cae a `b.dds`.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-B")
    destino = tmp_path / "dir-real"
    destino.mkdir()
    enlace = esc.physical_data / "textures" / "a.dds"
    enlace.parent.mkdir(parents=True, exist_ok=True)
    creado = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)], capture_output=True)
    if creado.returncode != 0 or not enlace.exists():
        pytest.skip("no se pudo crear la junction")
    assert os.lstat(enlace).st_file_attributes & 0x400, "debe llevar FILE_ATTRIBUTE_REPARSE_POINT"

    with pytest.raises(VfsAttestationError, match="enlace|reparse"):
        _challenge_del_mod(esc)


def test_error_de_acceso_no_es_ausencia_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un error de inspección debe salir como error del DOMINIO, no crudo.

    `Path.exists()` sólo absorbe ENOENT/ENOTDIR/EBADF/ELOOP; un EACCES se propaga
    tal cual y el builder lo deja escapar como `PermissionError`, que ningún
    llamador atrapa (el contrato es `VfsAttestationError`). La primitiva
    compartida lo convierte en fallo cerrado del dominio.

    Arrange: `textures/a.dds` existe en el Data físico como archivo regular, pero
    `stat`/`lstat` fallan con permiso denegado (path indeterminado).
    Act: seleccionar canario restringido al mod.
    Assert: `VfsAttestationError` por inspección, no un `PermissionError` crudo.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    fisico = esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    objetivo = os.path.normcase(os.path.abspath(os.fspath(fisico)))
    stat_real = os.stat
    lstat_real = os.lstat

    def _es_el_objetivo(path: object) -> bool:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == objetivo
        except TypeError:
            return False

    def stat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if _es_el_objetivo(path):
            raise PermissionError(13, "acceso denegado simulado", str(path))
        return stat_real(path, *args, **kwargs)

    def lstat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if _es_el_objetivo(path):
            raise PermissionError(13, "acceso denegado simulado", str(path))
        return lstat_real(path, *args, **kwargs)

    from sky_claw.local.mo2 import vfs_attestation as att

    monkeypatch.setattr(att.os, "stat", stat_falso)
    monkeypatch.setattr(att.os, "lstat", lstat_falso)

    with pytest.raises(VfsAttestationError, match="inspeccionar"):
        _challenge_del_mod(esc)


def test_error_de_acceso_no_se_salta_al_siguiente_candidato(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un candidato indeterminado corta la selección: no se elige otro en su lugar.

    Arrange: `a.dds` con inspección fallida y `b.dds` ausente del Data físico.
    Act: seleccionar canario restringido al mod.
    Assert: corta con error del dominio; NO devuelve un challenge de `b.dds`.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD-A")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-DEL-MOD-B")
    fisico = esc.escribir_fisico("a.dds", b"FISICO-A")
    objetivo = os.path.normcase(os.path.abspath(os.fspath(fisico)))
    stat_real = os.stat
    lstat_real = os.lstat

    def _es_el_objetivo(path: object) -> bool:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == objetivo
        except TypeError:
            return False

    def stat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if _es_el_objetivo(path):
            raise PermissionError(13, "acceso denegado simulado", str(path))
        return stat_real(path, *args, **kwargs)

    def lstat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if _es_el_objetivo(path):
            raise PermissionError(13, "acceso denegado simulado", str(path))
        return lstat_real(path, *args, **kwargs)

    from sky_claw.local.mo2 import vfs_attestation as att

    monkeypatch.setattr(att.os, "stat", stat_falso)
    monkeypatch.setattr(att.os, "lstat", lstat_falso)

    with pytest.raises(VfsAttestationError, match="inspeccionar"):
        _challenge_del_mod(esc)


def test_archivo_regular_no_bloquea_la_primera_pasada(tmp_path: pathlib.Path) -> None:
    """Guard de no-debilitamiento inverso: un regular NO es un objeto inseguro.

    Arrange: `a.dds` presente como regular con bytes distintos y `b.dds` ausente.
    Act: seleccionar canario restringido al mod.
    Assert: la preferencia histórica se mantiene (ausente primero) ⇒ `b.dds`.
    """
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-A")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-B")
    esc.escribir_fisico("a.dds", b"FISICO-A")

    challenge = _challenge_del_mod(esc)

    assert challenge.relative_path.as_posix() == "textures/b.dds"
    assert challenge.sha256 == _sha(b"BYTES-B")


def test_seleccion_diferencial_salta_ausencia_fisica_genuina(tmp_path: pathlib.Path) -> None:
    """Una ausencia física REAL de un candidato eclipsado se salta; el siguiente gana."""
    esc = _escenario(tmp_path, modlist=f"+{_OTRO}\n+{_MOD}\n")
    esc.escribir_mod(_OTRO, "a.dds", b"ECLIPSA-A")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD-A")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-DEL-MOD-B")
    esc.escribir_fisico("b.dds", b"FISICO-B")
    assert not (esc.physical_data / "textures" / "a.dds").exists()

    challenge = _challenge_del_mod(esc)

    assert challenge.relative_path.as_posix() == "textures/b.dds"
    assert challenge.sha256 == _sha(b"BYTES-DEL-MOD-B")


def test_canario_eclipsado_por_mod_superior_bloquea(tmp_path: pathlib.Path) -> None:
    # `modlist.txt` va de mayor a menor prioridad en disco: el PRIMER mod
    # habilitado es el de mayor prioridad regular, así que el eclipsante va
    # primero.
    esc = _escenario(tmp_path, modlist=f"+{_OTRO}\n+{_MOD}\n")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_mod(_OTRO, "a.dds", b"BYTES-DEL-OTRO")
    esc.escribir_fisico("a.dds", b"BYTES-FISICOS")

    with pytest.raises(VfsAttestationError):
        _challenge_del_mod(esc)


def test_archivo_fisico_inaccesible_falla_cerrado(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    # El path físico existe pero no es un archivo regular: no se puede leer.
    (esc.physical_data / "textures" / "a.dds").mkdir(parents=True)

    with pytest.raises(VfsAttestationError):
        _challenge_del_mod(esc)


def test_archivo_fisico_inaccesible_no_se_salta_a_otro_candidato(tmp_path: pathlib.Path) -> None:
    """Un archivo físico que existe pero no se puede leer es fail-closed, no se salta."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-DEL-MOD-B")
    (esc.physical_data / "textures" / "a.dds").mkdir(parents=True)
    esc.escribir_fisico("b.dds", b"FISICO-B-DISTINTO")

    with pytest.raises(VfsAttestationError):
        _challenge_del_mod(esc)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions son de Windows")
def test_archivo_fisico_con_reparse_falla_cerrado(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    destino = tmp_path / "otro-dir"
    destino.mkdir()
    (destino / "contenido.bin").write_bytes(b"X")
    junction = esc.physical_data / "textures" / "a.dds"
    junction.parent.mkdir(parents=True, exist_ok=True)
    creado = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(destino)], capture_output=True, text=True)
    if creado.returncode != 0 or not junction.exists():
        pytest.skip(f"no se pudo crear la junction: {creado.stderr[-200:]}")

    with pytest.raises(VfsAttestationError):
        _challenge_del_mod(esc)


def test_sin_candidatos_conserva_blocked_canary(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"MISMO")
    esc.escribir_fisico("a.dds", b"MISMO")

    with pytest.raises(VfsAttestationError, match="canary elegible"):
        _challenge_del_mod(esc)


def test_seleccion_restringida_al_mod_pedido(tmp_path: pathlib.Path) -> None:
    # `_OTRO` va PRIMERO en el modlist: es el mod de mayor prioridad regular y
    # tiene un candidato ausente más fuerte. La selección libre debe elegirlo a
    # él; la restringida al mod pedido NO puede quedarse con el canario ajeno.
    esc = _escenario(tmp_path, modlist=f"+{_OTRO}\n+{_MOD}\n")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-A")
    esc.escribir_mod(_OTRO, "b.dds", b"BYTES-DEL-OTRO")

    libre = build_attestation_challenge(
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
    )
    assert libre.source_mod == _OTRO
    assert libre.relative_path.as_posix() == "textures/b.dds"

    propio = _challenge_del_mod(esc)
    assert propio.source_mod == _MOD
    assert propio.relative_path.as_posix() == "textures/a.dds"


def test_builder_libre_no_habilita_diferencial_para_consumidores_sin_revalidacion(
    tmp_path: pathlib.Path,
) -> None:
    """LOOT/legacy/health usan el builder libre: el diferencial es opt-in del gate."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")

    with pytest.raises(VfsAttestationError, match="canary elegible"):
        build_attestation_challenge(
            data_root=esc.data_root,
            mods_dir=esc.mods_dir,
            profile=_PERFIL,
            physical_data_dir=esc.physical_data,
        )

    opt_in = build_attestation_challenge(
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
        permitir_diferencial=True,
    )
    assert opt_in.relative_path.as_posix() == "textures/a.dds"


def test_reparse_de_archivo_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La rama de reparse de ARCHIVO (no junction de directorio) también corta."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    destino = esc.physical_data / "textures" / "a.dds"
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"FISICO")

    import os

    from sky_claw.local.mo2 import vfs_attestation as att

    real_lstat = att.os.lstat
    # Se calcula ANTES del monkeypatch: dentro del fake no se puede llamar a
    # Path.resolve(), porque en POSIX vuelve a entrar a os.lstat (módulo
    # compartido) y el fake se recursaría.
    objetivo = os.path.normcase(os.path.abspath(os.fspath(destino.resolve())))

    def lstat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == objetivo:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(att.os, "lstat", lstat_falso)
    # Los paths ajenos se delegan al lstat real (sin recursión ni resolución).
    assert lstat_falso(esc.virtual_data) == real_lstat(esc.virtual_data)

    with pytest.raises(VfsAttestationError, match="enlace"):
        _challenge_del_mod(esc)


def test_archivo_fisico_con_oserror_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo real de inspección (permiso/bloqueo) no se salta: corta.

    Se ejercita la primitive directo: ``os.path.lexists`` (que usa la selección)
    traduce cualquier ``OSError`` a ``False`` y no permite distinguir "ausente" de
    "inaccesible"; la primitive que sí debe cortar es la inspección fail-closed.
    """
    esc = _escenario(tmp_path)
    destino = esc.physical_data / "textures" / "a.dds"
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"FISICO")

    import os

    from sky_claw.local.mo2 import vfs_attestation as att

    real_lstat = att.os.lstat
    # Clave normalizada calculada ANTES del monkeypatch; dentro del fake se usa
    # abspath/normcase/fspath, que no invocan os.lstat (sin recursión en POSIX).
    objetivo = os.path.normcase(os.path.abspath(os.fspath(destino.resolve())))

    def lstat_falso(path: object, *args: object, **kwargs: object) -> Any:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == objetivo:
            raise PermissionError("share violation simulado")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(att.os, "lstat", lstat_falso)
    # Los paths ajenos se delegan al lstat real (sin recursión ni resolución).
    assert lstat_falso(esc.virtual_data) == real_lstat(esc.virtual_data)

    with pytest.raises(VfsAttestationError, match="inspeccionar"):
        att._sha256_fisico_seguro(destino)


@pytest.mark.asyncio
async def test_preprobe_ancla_el_invariante_fisico(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si el invariante físico falla ANTES del probe, no se lanza ningún job."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    esc.montar_vista_virtual()

    class _BrokerQueNoDebeLlamarse:
        probes = 0

        async def submit(self, *args: object, **kwargs: object) -> VfsJobResult:  # pragma: no cover
            self.probes += 1
            raise AssertionError("no debe llegar al probe con el invariante roto")

        async def open_session(self, *args: object, **kwargs: object) -> Any:  # pragma: no cover
            raise AssertionError("no debe abrir sesión")

    from sky_claw.local.mo2 import brokered_dyndolod as mod

    def invariante_roto(**kwargs: object) -> None:
        from sky_claw.local.mo2.vfs_attestation import VfsAttestationError as _Error

        raise _Error("el Data físico ya entrega los bytes esperados")

    monkeypatch.setattr(mod, "assert_canary_fisicamente_distinguible", invariante_roto)
    broker = _BrokerQueNoDebeLlamarse()
    resultado = await _gate(esc, broker)

    assert resultado.verified is False
    assert resultado.pending_action is None
    assert broker.probes == 0


@pytest.mark.asyncio
async def test_revalidacion_de_spawn_ancla_el_invariante_fisico(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El boundary del spawn revalida el invariante físico, no sólo el rebuild."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")

    from sky_claw.local.mo2 import brokered_dyndolod as mod
    from sky_claw.local.mo2 import vfs_attestation as att

    challenge = att.build_attestation_challenge_for_source(
        source_mod=_MOD,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
        permitir_diferencial=True,
    )
    approval = TexGenHandoffApproval(
        mod_name=_MOD,
        artifact_root=esc.artifact_root,
        artifact=digest_arbol(esc.artifact_root),
        data_dir=esc.virtual_data,
        profile=_PERFIL,
        profile_fingerprint=challenge.profile_fingerprint,
        canary_relative_path=challenge.relative_path,
        canary_sha256=challenge.sha256,
    )
    monkeypatch.setattr(mod, "build_attestation_challenge_for_source", lambda **kwargs: challenge)

    def invariante_roto(**kwargs: object) -> None:
        raise att.VfsAttestationError("el Data físico ya entrega los bytes esperados")

    monkeypatch.setattr(mod, "assert_canary_fisicamente_distinguible", invariante_roto)
    estrategia = BrokeredDynDOLODSpawnStrategy(
        broker=object(),
        instance_id="portable-main",
        profile=_PERFIL,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        install_root=esc.data_root,
        physical_data_dir=esc.physical_data,
        virtual_data_dir=esc.virtual_data,
        output_roots={},
    )

    with pytest.raises(HandoffDriftError, match="físico"):
        await estrategia._revalidar_aprobacion(approval)


def test_canario_ausente_tiene_prioridad_sobre_diferencial(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_mod(_MOD, "z.dds", b"BYTES-DEL-MOD-Z")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")

    challenge = _challenge_del_mod(esc)

    assert challenge.relative_path.as_posix() == "textures/z.dds", (
        "el canario ausente del Data físico es más fuerte y debe preferirse"
    )


# =============================================================================
# Invariante físico (fail-closed y TOCTOU)
# =============================================================================


def test_assert_fisicamente_distinguible_acepta_ausente_y_diferente(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    challenge = _challenge_del_mod(esc)
    _assert_fisicamente_distinguible(esc, challenge)  # ausente: ok

    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    challenge = _challenge_del_mod(esc)
    _assert_fisicamente_distinguible(esc, challenge)  # diferente: ok


def test_assert_fisicamente_distinguible_rechaza_bytes_esperados(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    challenge = _challenge_del_mod(esc)

    # El Data físico pasa a entregar los mismos bytes que el mod: sin overlay no
    # habría diferencia observable y el canary dejaría de probar el mapping.
    esc.escribir_fisico("a.dds", b"BYTES-DEL-MOD")

    with pytest.raises(VfsAttestationError):
        _assert_fisicamente_distinguible(esc, challenge)


# =============================================================================
# Worker y nieto: overlay correcto / sin overlay / vista incorrecta
# =============================================================================


def _worker_verifica(esc: _Escenario, challenge: VfsAttestationChallenge) -> Any:
    return verify_vfs_attestation(
        challenge=challenge,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        virtual_data_dir=esc.virtual_data,
    )


def test_worker_sin_overlay_falla(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    challenge = _challenge_del_mod(esc)

    # Sin overlay, el worker vería el Data físico (bytes distintos).
    with pytest.raises(VfsAttestationError):
        verify_vfs_attestation(
            challenge=challenge,
            data_root=esc.data_root,
            mods_dir=esc.mods_dir,
            profile=_PERFIL,
            virtual_data_dir=esc.physical_data,
        )


def test_worker_con_overlay_acepta_solo_sha_autorizado(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    challenge = _challenge_del_mod(esc)
    esc.montar_vista_virtual()

    proof = _worker_verifica(esc, challenge)
    assert proof.visible_sha256 == _sha(b"BYTES-DEL-MOD")

    # Si la vista virtual entregara otros bytes, la atestación debe fallar.
    (esc.virtual_data / "textures" / "a.dds").write_bytes(b"OTRA-VISTA")
    with pytest.raises(VfsAttestationError):
        _worker_verifica(esc, challenge)


def test_nieto_con_vista_incorrecta_falla(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    esc.montar_vista_virtual()
    virtual = esc.virtual_data / "textures" / "a.dds"
    esperado = _sha(b"BYTES-DEL-MOD")

    assert run_probe_child(virtual, esperado) == 0
    (esc.virtual_data / "textures" / "a.dds").write_bytes(b"FISICO-DISTINTO")
    assert run_probe_child(virtual, esperado) != 0


def test_drift_de_perfil_bloquea_la_atestacion(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    challenge = _challenge_del_mod(esc)
    esc.montar_vista_virtual()

    esc.modlist(f"+{_MOD}\n+{_OTRO}\n+NuevoMod\n")
    (esc.mods_dir / "NuevoMod").mkdir()

    with pytest.raises(VfsAttestationError):
        _worker_verifica(esc, challenge)


# =============================================================================
# Gate brokered: coherencia temporal (TOCTOU) y revalidación en el spawn
# =============================================================================


class _BrokerMutante:
    """Bridge falso: atestigua de verdad y muta el Data físico DURANTE el probe."""

    def __init__(self, esc: _Escenario, mutacion) -> None:
        self.esc = esc
        self.mutacion = mutacion
        self.probes = 0

    async def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        data_root=None,
        mods_dir=None,
        install_root=None,
        virtual_data_dir,
        overwrite_mod=None,
    ) -> VfsJobResult:
        self.probes += 1
        proof = verify_vfs_attestation(
            challenge=challenge,
            data_root=data_root,
            mods_dir=mods_dir,
            profile=job.profile,
            virtual_data_dir=virtual_data_dir,
        )
        self.mutacion()
        atestacion = dict(proof.to_dict())
        atestacion["grandchild_sha256"] = challenge.sha256
        return VfsJobResult(
            protocol_version=2,
            job_id=job.job_id,
            success=True,
            message="",
            exit_code=0,
            stdout="",
            stderr="",
            outputs=(),
            rollback_state="not_required",
            attestation=atestacion,
            tool_result={},
        )

    async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:  # pragma: no cover - no se usa
        raise AssertionError("el gate no debe abrir sesiones")


def _strategy(esc: _Escenario, broker: Any) -> BrokeredDynDOLODSpawnStrategy:
    return BrokeredDynDOLODSpawnStrategy(
        broker=broker,
        instance_id="portable-main",
        profile=_PERFIL,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        install_root=esc.data_root,
        physical_data_dir=esc.physical_data,
        virtual_data_dir=esc.virtual_data,
        output_roots={},
    )


def _request(esc: _Escenario) -> TexGenHandoffRequest:
    return TexGenHandoffRequest(
        mod_name=_MOD,
        staging=esc.artifact_root,
        data_dir=esc.virtual_data,
        expected_profile=_PERFIL,
        authorized=None,
    )


async def _gate(esc: _Escenario, broker: Any) -> TexGenHandoffResult:
    return await _strategy(esc, broker).verify_texgen_handoff(_request(esc))


@pytest.mark.asyncio
async def test_cambio_fisico_entre_mediciones_bloquea_el_gate(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    esc.montar_vista_virtual()

    def mutar() -> None:
        # El Data físico pasa a tener los bytes del mod: sin overlay ya no habría
        # diferencia observable, así que el gate no puede aprobar con esa evidencia.
        esc.escribir_fisico("a.dds", b"BYTES-DEL-MOD")

    broker = _BrokerMutante(esc, mutar)
    resultado = await _gate(esc, broker)

    assert broker.probes == 1
    assert resultado.verified is False
    assert resultado.pending_action is None
    assert "Data físico" in resultado.reason or "físico" in resultado.reason


@pytest.mark.asyncio
async def test_aprobacion_diferencial_revalida_estado_fisico_en_spawn(tmp_path: pathlib.Path) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    esc.montar_vista_virtual()

    class _BrokerHonesto:
        async def submit(
            self,
            job: VfsJob,
            *,
            challenge: VfsAttestationChallenge,
            data_root=None,
            mods_dir=None,
            install_root=None,
            virtual_data_dir,
            overwrite_mod=None,
        ) -> VfsJobResult:
            proof = verify_vfs_attestation(
                challenge=challenge,
                data_root=data_root,
                mods_dir=mods_dir,
                profile=job.profile,
                virtual_data_dir=virtual_data_dir,
            )
            atestacion = dict(proof.to_dict())
            atestacion["grandchild_sha256"] = challenge.sha256
            return VfsJobResult(
                protocol_version=2,
                job_id=job.job_id,
                success=True,
                message="",
                exit_code=0,
                stdout="",
                stderr="",
                outputs=(),
                rollback_state="not_required",
                attestation=atestacion,
                tool_result={},
            )

        async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("no debe abrir sesión")

    estrategia = _strategy(esc, _BrokerHonesto())
    resultado = await estrategia.verify_texgen_handoff(_request(esc))
    assert resultado.verified is True, resultado.reason
    approval = resultado.approval
    assert approval is not None

    # Drift del estado físico entre el gate y el spawn: el approval ya no se sostiene.
    esc.escribir_fisico("a.dds", b"BYTES-DEL-MOD")
    from sky_claw.local.tools.texgen_handoff import HandoffDriftError

    with pytest.raises(HandoffDriftError):
        await estrategia._revalidar_aprobacion(approval)


# =============================================================================
# Anclas de familia: el diferencial sólo lo habilita el gate brokered
# =============================================================================

_BUILDERS = frozenset({"_construir_challenge", "build_attestation_challenge", "build_attestation_challenge_for_source"})


def _sitios_que_tocan_el_flag() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Enumera, por AST, todo call site que pasa ``permitir_diferencial``.

    Devuelve ``(literales_true, propagaciones)``: los sitios que habilitan el
    diferencial con un literal ``True`` y los que sólo propagan el parámetro
    dentro de la capa de builders. Un call site nuevo con ``True`` rompe el ancla
    hasta que se lo clasifique explícitamente.
    """
    raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
    literales: set[tuple[str, str]] = set()
    propagaciones: set[tuple[str, str]] = set()

    def visitar(nodo: ast.AST, funcion: str) -> None:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcion = nodo.name
        if isinstance(nodo, ast.Call):
            for keyword in nodo.keywords:
                if keyword.arg != "permitir_diferencial":
                    continue
                sitio = (str(archivo.relative_to(raiz).as_posix()), funcion)
                valor = keyword.value
                if isinstance(valor, ast.Constant) and valor.value is True:
                    literales.add(sitio)
                elif isinstance(valor, ast.Name):
                    propagaciones.add(sitio)
                elif isinstance(valor, ast.Constant) and valor.value is False:
                    continue
                else:
                    literales.add((sitio[0], f"{sitio[1]}#no-literal"))
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo, funcion)

    for archivo in sorted(raiz.rglob("*.py")):
        visitar(ast.parse(archivo.read_text(encoding="utf-8")), "<module>")
    return literales, propagaciones


def test_solo_el_gate_brokered_habilita_el_diferencial() -> None:
    """Familia enumerada: ningún consumidor fuera del gate puede pasar True."""
    literales, propagaciones = _sitios_que_tocan_el_flag()

    assert literales == {
        ("local/mo2/brokered_dyndolod.py", "_verificar_handoff_mo2"),
        ("local/mo2/brokered_dyndolod.py", "_revalidar_aprobacion"),
    }, "un call site nuevo habilitó el canario diferencial sin clasificarlo"
    assert propagaciones == {
        ("local/mo2/vfs_attestation.py", "build_attestation_challenge"),
        ("local/mo2/vfs_attestation.py", "build_attestation_challenge_for_source"),
    }, "el flag debe propagarse sólo desde los dos builders"


@pytest.mark.asyncio
async def test_spawn_revalida_diferencial_sin_flip(tmp_path: pathlib.Path) -> None:
    """Camino feliz: el spawn revalida el MISMO canario diferencial aprobado."""

    class _BrokerHonesto:
        async def submit(
            self,
            job: VfsJob,
            *,
            challenge: VfsAttestationChallenge,
            data_root=None,
            mods_dir=None,
            install_root=None,
            virtual_data_dir,
            overwrite_mod=None,
        ) -> VfsJobResult:
            proof = verify_vfs_attestation(
                challenge=challenge,
                data_root=data_root,
                mods_dir=mods_dir,
                profile=job.profile,
                virtual_data_dir=virtual_data_dir,
            )
            atestacion = dict(proof.to_dict())
            atestacion["grandchild_sha256"] = challenge.sha256
            return VfsJobResult(
                protocol_version=2,
                job_id=job.job_id,
                success=True,
                message="",
                exit_code=0,
                stdout="",
                stderr="",
                outputs=(),
                rollback_state="not_required",
                attestation=atestacion,
                tool_result={},
            )

        async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("no debe abrir sesión")

    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-DISTINTO")
    esc.montar_vista_virtual()
    estrategia = _strategy(esc, _BrokerHonesto())

    resultado = await estrategia.verify_texgen_handoff(_request(esc))
    assert resultado.verified is True, resultado.reason
    approval = resultado.approval
    assert approval is not None

    fresh = await estrategia._revalidar_aprobacion(approval)

    assert fresh.relative_path == approval.canary_relative_path
    assert fresh.sha256 == approval.canary_sha256


# =============================================================================
# Subárbol autorizado: la elegibilidad se restringe ANTES de seleccionar
# =============================================================================


def _escribir_mod_relativo(esc: _Escenario, relativo: str, contenido: bytes) -> pathlib.Path:
    """Escribe un archivo del mod en una ruta relativa al MOD (no al artifact)."""
    ruta = esc.mods_dir / _MOD / relativo
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    return ruta


def _challenge_del_subarbol(esc: _Escenario, subarbol: str) -> VfsAttestationChallenge:
    return build_attestation_challenge_for_source(
        source_mod=_MOD,
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
        permitir_diferencial=True,
        subarbol_autorizado=subarbol,
    )


def test_subarbol_autorizado_filtra_antes_de_seleccionar(tmp_path: pathlib.Path) -> None:
    """El ausente de `meshes/` no puede ganarle al diferencial de `textures/`.

    Sin restricción el walk elige `meshes/a.nif` (ausente del Data físico, y por
    orden alfabético el primero). Restringido al artifact, el único elegible es
    el diferencial de `textures/`.
    """
    esc = _escenario(tmp_path)
    _escribir_mod_relativo(esc, "meshes/a.nif", b"NIF-DEL-MOD")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-ARTIFACT")
    esc.escribir_fisico("a.dds", b"DDS-FISICO-DISTINTO")

    libre = _challenge_del_mod(esc)
    assert libre.relative_path.as_posix() == "meshes/a.nif", "baseline: el walk del mod entero gana por orden"

    restringido = _challenge_del_subarbol(esc, "textures")
    assert restringido.relative_path.as_posix() == "textures/a.dds"
    assert restringido.source_mod == _MOD


def test_subarbol_autorizado_conserva_ausente_antes_que_diferencial(tmp_path: pathlib.Path) -> None:
    """Dentro del subárbol se mantiene la preferencia histórica: ausente primero."""
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"AUSENTE-DEL-FISICO")
    esc.escribir_mod(_MOD, "b.dds", b"BYTES-B")
    esc.escribir_fisico("b.dds", b"B-FISICO-DISTINTO")

    challenge = _challenge_del_subarbol(esc, "textures")

    assert challenge.relative_path.as_posix() == "textures/a.dds"
    assert challenge.sha256 == _sha(b"AUSENTE-DEL-FISICO")


def test_subarbol_autorizado_sin_candidatos_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Ningún elegible dentro del subárbol ⇒ bloquea; jamás cae a un externo."""
    esc = _escenario(tmp_path)
    _escribir_mod_relativo(esc, "meshes/a.nif", b"NIF-DEL-MOD")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-ARTIFACT")
    esc.escribir_fisico("a.dds", b"BYTES-DEL-ARTIFACT")  # idéntico: no prueba diferenciación

    with pytest.raises(VfsAttestationError, match="textures"):
        _challenge_del_subarbol(esc, "textures")


@pytest.mark.parametrize(
    "subarbol",
    [
        "/textures",  # absoluta POSIX
        "../meshes",  # escape por ..
        "textures/../meshes",  # escape por ..
        "textures/",  # componente vacío final
        "textures//sub",  # componente vacío intermedio
        "textures/./sub",  # componente "."
        "C:/textures",  # designador de unidad Windows
        "textures\\meshes",  # separador Windows
        "",  # vacío
        "   ",  # sólo espacios
    ],
)
def test_subarbol_autorizado_rechaza_rutas_inseguras(tmp_path: pathlib.Path, subarbol: str) -> None:
    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-ARTIFACT")
    esc.escribir_fisico("a.dds", b"DDS-FISICO-DISTINTO")

    with pytest.raises(VfsAttestationError):
        _challenge_del_subarbol(esc, subarbol)


def test_subarbol_autorizado_es_opcional_para_consumidores_historicos(tmp_path: pathlib.Path) -> None:
    """El builder libre y el `for_source` sin restricción conservan su semántica."""
    esc = _escenario(tmp_path, modlist=f"+{_OTRO}\n+{_MOD}\n")
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")
    esc.escribir_fisico("a.dds", b"FISICO-A")
    esc.escribir_mod(_OTRO, "b.dds", b"BYTES-DEL-OTRO")

    libre = build_attestation_challenge(
        data_root=esc.data_root,
        mods_dir=esc.mods_dir,
        profile=_PERFIL,
        physical_data_dir=esc.physical_data,
    )
    assert libre.source_mod == _OTRO
    assert libre.relative_path.as_posix() == "textures/b.dds"

    sin_restriccion = _challenge_del_mod(esc)
    assert sin_restriccion.relative_path.as_posix() == "textures/a.dds"


def test_subarbol_sin_mod_fuente_es_incoherente(tmp_path: pathlib.Path) -> None:
    """Un subárbol sin mod fuente no delimita nada: es un contrato inválido."""
    from sky_claw.local.mo2.vfs_attestation import _construir_challenge

    esc = _escenario(tmp_path)
    esc.escribir_mod(_MOD, "a.dds", b"BYTES-DEL-MOD")

    with pytest.raises(VfsAttestationError, match="mod fuente"):
        _construir_challenge(
            data_resolved=esc.data_root.resolve(),
            mods_resolved=esc.mods_dir.resolve(),
            profile_dir=esc.data_root / "profiles" / _PERFIL,
            profile_name=_PERFIL,
            data=esc.physical_data.resolve(),
            always_active=frozenset(),
            source_mod=None,
            permitir_diferencial=False,
            subarbol_autorizado="textures",
        )


def test_todo_caller_de_for_source_restringe_al_artifact() -> None:
    """Ancla enumerada: un call site nuevo sin restricción rompe el ancla.

    La corrección del selector vale sólo si TODOS los caminos que construyen el
    challenge de un artifact lo restringen — el gate y la reconstrucción del
    spawn. Un tercer llamador sin el filtro reintroduce el falso negativo.

    El detector cubre las dos formas de invocación del repo: llamada directa y
    ``asyncio.to_thread(build_attestation_challenge_for_source, ...)`` (donde el
    builder es el PRIMER ARGUMENTO, no el ``func`` del ``Call``).
    """
    raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
    nombre_objetivo = "build_attestation_challenge_for_source"
    sitios: list[tuple[str, frozenset[str]]] = []

    def _invoca(nodo: ast.Call) -> bool:
        candidatos = [nodo.func, *nodo.args]
        for candidato in candidatos:
            etiqueta = candidato.id if isinstance(candidato, ast.Name) else getattr(candidato, "attr", None)
            if etiqueta == nombre_objetivo:
                return True
        return False

    for archivo in sorted(raiz.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not _invoca(nodo):
                continue
            sitios.append(
                (
                    str(archivo.relative_to(raiz)).replace("\\", "/"),
                    frozenset(kw.arg for kw in nodo.keywords if kw.arg is not None),
                )
            )

    assert sitios, "el ancla no puede quedar vacía: no se encontró ningún call site"
    sin_restriccion = [ruta for ruta, kwargs in sitios if "subarbol_autorizado" not in kwargs]
    assert sin_restriccion == [], f"call sites sin restricción al artifact: {sin_restriccion}"
