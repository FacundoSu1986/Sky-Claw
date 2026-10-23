"""Smoke REAL de NP-E0b: corre el ``texconv.exe`` fijado de verdad (``SKYCLAW_TEXCONV_EXE``).

Se salta sin esa variable. Es la única prueba que toca el binario real: ``tests/test_texconv_runner.py`` usa un
``texconv`` falso y por sí solo NO sostiene ninguna afirmación sobre el binario (``sky_claw/local/tools/AGENTS.md``:
«No afirmar smoke real a partir de un mock»).

Cómo correrlo (Windows, con el ``texconv.exe`` oficial ``may2026`` descargado aparte)::

    set SKYCLAW_TEXCONV_EXE=C:\\ruta\\a\\texconv.exe
    .venv\\Scripts\\python -m pytest tests/test_texconv_real.py -v -rA

Qué comprueba: (1) que el binario instalado tiene la huella del pin; (2) que lo que devuelve pasa el validador BC4 con
la cadena COMPLETA de mips; (3) que dos corridas son idénticas byte a byte; (4) que cada nivel decodificado sigue la
cadena propia (esto descarta POINT, sRGB o una cadena regenerada con otro filtro) con una tolerancia calibrada contra
un codificador ingenuo; (5) que un plazo vencido mata el proceso y lo traduce. Las observaciones (tipo de cabecera,
tamaños, errores por nivel) se escriben a stderr: ``-rA`` las muestra.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

from sky_claw.local.native_parallax.export import cuantizar_a_8_bits, generar_mips, verificar_texconv
from sky_claw.local.tools.texconv_runner import TexconvTimeoutError, exportar_altura_bc4
from tests._bc4 import campo_sintetico, decodificar_nivel, limites_de_tolerancia, partir_niveles

_EXE = os.environ.get("SKYCLAW_TEXCONV_EXE")

pytestmark = pytest.mark.skipif(not _EXE, reason="smoke real: definí SKYCLAW_TEXCONV_EXE con el texconv.exe oficial")


def _observar(texto: str) -> None:
    sys.stderr.write(f"[smoke texconv] {texto}\n")


@pytest.fixture
def exe() -> Path:
    assert _EXE is not None
    return Path(_EXE)


def test_el_binario_instalado_tiene_la_huella_del_pin_vigente(exe: Path) -> None:
    pin = verificar_texconv(exe)

    _observar(f"binario verificado contra el pin {pin.version} ({pin.sha256})")


async def test_exporta_y_la_salida_pasa_el_validador_con_la_cadena_completa(exe: Path, tmp_path: Path) -> None:
    resultado = await exportar_altura_bc4(campo_sintetico(256, 256), texconv=exe, directorio_temporal=tmp_path)

    assert (resultado.info.ancho, resultado.info.alto, resultado.info.niveles) == (256, 256, 9)
    assert resultado.info.formato == "BC4_UNORM"
    _observar(
        f"256x256 -> {len(resultado.datos)} bytes, {resultado.info.niveles} niveles, "
        f"extensión DX10: {resultado.info.con_extension_dx10}, sha256 {resultado.sha256}"
    )
    assert list(tmp_path.iterdir()) == []  # no quedan restos


@pytest.mark.parametrize("forma", [(4, 4), (8, 4), (16, 64), (128, 32)])
async def test_funciona_con_tamanos_chicos_y_no_cuadrados(exe: Path, tmp_path: Path, forma: tuple[int, int]) -> None:
    resultado = await exportar_altura_bc4(campo_sintetico(*forma), texconv=exe, directorio_temporal=tmp_path)

    assert (resultado.info.alto, resultado.info.ancho) == forma
    _observar(f"{forma[1]}x{forma[0]} -> {len(resultado.datos)} bytes, {resultado.info.niveles} niveles")


async def test_dos_exportaciones_son_identicas_byte_a_byte(exe: Path, tmp_path: Path) -> None:
    altura = campo_sintetico(256, 256, semilla=5)

    primera = await exportar_altura_bc4(altura, texconv=exe, directorio_temporal=tmp_path)
    segunda = await exportar_altura_bc4(altura, texconv=exe, directorio_temporal=tmp_path)

    assert primera.datos == segunda.datos
    _observar(f"determinismo: {primera.sha256} == {segunda.sha256}")


async def test_cada_nivel_decodificado_sigue_la_cadena_propia(exe: Path, tmp_path: Path) -> None:
    altura = campo_sintetico(256, 256, semilla=3)
    referencia = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(altura)]

    resultado = await exportar_altura_bc4(altura, texconv=exe, directorio_temporal=tmp_path)

    inicio = 148 if resultado.info.con_extension_dx10 else 128
    niveles = partir_niveles(resultado.datos[inicio:], 256, 256, 9)
    for i, (carga, esperado) in enumerate(zip(niveles, referencia, strict=True)):
        alto, ancho = esperado.shape
        error = np.abs(decodificar_nivel(carga, ancho, alto) - esperado.astype(np.float64))
        # Tolerancia PROVISIONAL (ver ``tests/_bc4.py``): ``tests/test_bc4_helper.py`` comprueba que distingue POINT y sRGB.
        limite_medio, limite_maximo = limites_de_tolerancia(esperado)
        _observar(
            f"nivel {i} {ancho}x{alto}: error medio {error.mean():.3f} (límite {limite_medio:.3f}), "
            f"máximo {error.max():.1f} (límite {limite_maximo:.1f})"
        )
        assert error.mean() <= limite_medio
        assert error.max() <= limite_maximo


async def test_un_plazo_vencido_mata_el_proceso_y_se_traduce(exe: Path, tmp_path: Path) -> None:
    grande = np.random.default_rng(1).random((2048, 2048))

    with pytest.raises(TexconvTimeoutError):
        await exportar_altura_bc4(grande, texconv=exe, timeout_s=0.02, directorio_temporal=tmp_path)

    assert list(tmp_path.iterdir()) == []
