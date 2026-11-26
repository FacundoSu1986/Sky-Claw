"""PR 2c-2b (D7, mitad del worker): el exe se re-hashea con un handle que nadie puede reemplazar.

Entre el hash del daemon y la ejecución en el worker hay una ventana (lección F-02 de
Synthesis): si el exe se reemplaza ahí, se ejecuta otro binario con el hash del primero.
El worker abre el exe sin compartir escritura ni borrado, lo re-hashea, lo compara con el
hash del job y RETIENE el handle mientras corre: nadie puede reemplazarlo ni renombrarlo.

Propiedad: *lo que se ejecuta es lo que se hasheó*. En Windows lo garantiza la compartición
del handle (``FILE_SHARE_READ`` sin ``WRITE`` ni ``DELETE``); en POSIX no hay compartición
obligatoria y el helper solo verifica hash, tipo de archivo y enlaces (documentado).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import pytest

from sky_claw.local.tools.exe_retenido import ExeNoCoincideError, retener_exe_verificado

CONTENIDO = b"MZ binario de prueba"


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture
def exe(tmp_path: pathlib.Path) -> pathlib.Path:
    ruta = tmp_path / "BodySlide x64.exe"
    ruta.write_bytes(CONTENIDO)
    return ruta


def test_con_el_hash_correcto_entra_y_devuelve_el_hash(exe: pathlib.Path) -> None:
    with retener_exe_verificado(exe, _sha(CONTENIDO)) as observado:
        assert observado == _sha(CONTENIDO)


def test_un_hash_distinto_falla_antes_de_entrar(exe: pathlib.Path) -> None:
    entro = False
    with pytest.raises(ExeNoCoincideError, match="hash"), retener_exe_verificado(exe, _sha(b"otro binario")):
        entro = True
    assert entro is False


@pytest.mark.parametrize("hash_invalido", ["", "abc", "Z" * 64, _sha(CONTENIDO).upper() + "0"])
def test_un_hash_esperado_mal_formado_se_rechaza(exe: pathlib.Path, hash_invalido: str) -> None:
    with pytest.raises(ExeNoCoincideError), retener_exe_verificado(exe, hash_invalido):
        pass


def test_un_exe_ausente_se_rechaza(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ExeNoCoincideError), retener_exe_verificado(tmp_path / "no-existe.exe", _sha(CONTENIDO)):
        pass


def test_un_directorio_se_rechaza(tmp_path: pathlib.Path) -> None:
    carpeta = tmp_path / "BodySlide x64.exe"
    carpeta.mkdir()
    with pytest.raises(ExeNoCoincideError), retener_exe_verificado(carpeta, _sha(CONTENIDO)):
        pass


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
def test_un_enlace_simbolico_se_rechaza(exe: pathlib.Path, tmp_path: pathlib.Path) -> None:
    enlace = tmp_path / "enlace.exe"
    try:
        enlace.symlink_to(exe)
    except OSError:
        pytest.skip("sin privilegio para crear symlinks")
    with pytest.raises(ExeNoCoincideError), retener_exe_verificado(enlace, _sha(CONTENIDO)):
        pass


def test_un_exe_con_un_segundo_enlace_duro_se_rechaza(exe: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Con dos nombres, reemplazar el contenido por el otro nombre esquiva el handle retenido."""
    try:
        os.link(exe, tmp_path / "copia.exe")
    except OSError:
        pytest.skip("el sistema de archivos no admite enlaces duros")
    with pytest.raises(ExeNoCoincideError, match="enlace"), retener_exe_verificado(exe, _sha(CONTENIDO)):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="la compartición obligatoria del handle es de Windows")
def test_en_windows_nadie_puede_escribir_ni_borrar_el_exe_mientras_esta_retenido(exe: pathlib.Path) -> None:
    with retener_exe_verificado(exe, _sha(CONTENIDO)):
        with pytest.raises(PermissionError):
            exe.write_bytes(b"binario reemplazado")
        with pytest.raises(PermissionError):
            exe.unlink()
        with pytest.raises(PermissionError):
            exe.rename(exe.with_name("renombrado.exe"))
        # Leer sí se puede: BodySlide tiene que poder cargarse.
        assert exe.read_bytes() == CONTENIDO
    # Al salir, el handle se libera.
    exe.write_bytes(b"ahora si")


def test_al_salir_por_una_excepcion_el_handle_se_libera(exe: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="adentro"), retener_exe_verificado(exe, _sha(CONTENIDO)):
        raise RuntimeError("adentro")
    exe.write_bytes(b"liberado")
    assert exe.read_bytes() == b"liberado"
