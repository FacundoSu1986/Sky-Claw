"""NP-A0j (parte 2): el subcomando ``elegibilidad`` de la CLI del generador nativo de parallax (diagnóstico de SOLO lectura).

Dice, por cada textura de un mod, si alguna malla del perfil de MO2 usaría el ``*_p.dds`` que el generador publicaría (``SI``), si
seguro no (``NO``) o si no se puede afirmar (``NO_VERIFICADO``), con la razón. No genera nada, no necesita ``texconv`` y no deja ningún
archivo. Sale en 0 cuando el informe se produjo, haya o no texturas sin efecto posible: ese es el contenido del informe, no un error.
"""

from __future__ import annotations

import io
import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

from sky_claw.local.native_parallax.cli import CODIGO_DE_USO, CODIGO_OK, construir_parser, main
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from tests._nif_sintetico import (
    bloque_forma_bs,
    bloque_juego_de_texturas,
    bloque_nodo,
    bloque_shader_de_iluminacion,
    construir_nif,
)

_CARPETA = "textures/architecture/whiterun"
_ENTRADA = f"{_CARPETA}/muro01_n.dds"
_SALIDA = f"{_CARPETA}/muro01_p.dds"
_MOD = "Texturas"
_MALLAS = "Mallas"


def _nif(*, tipo_de_shader: int = 0) -> bytes:
    return construir_nif(
        [
            bloque_nodo(hijos=(1,)),
            bloque_forma_bs(shader=2),
            bloque_shader_de_iluminacion(tipo_de_shader=tipo_de_shader, flags_2=1 << 5, juego_de_texturas=3),
            bloque_juego_de_texturas(
                (
                    "textures\\architecture\\whiterun\\muro01.dds",
                    "textures\\architecture\\whiterun\\muro01_n.dds",
                    "",
                    "",
                )
            ),
        ]
    )


@dataclass
class Mundo:
    raiz: pathlib.Path

    @property
    def mods(self) -> pathlib.Path:
        return self.raiz / "mods"

    @property
    def data(self) -> pathlib.Path:
        return self.raiz / "data"

    def archivo(self, mod: str, ruta: str, contenido: bytes = b"x") -> None:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido + mod.encode())

    def modlist(self, *lineas: str, perfil: str = "Default") -> None:
        ruta = self.data / "profiles" / perfil / "modlist.txt"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# generado\n" + "\n".join(lineas) + "\n", encoding="utf-8")


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> Mundo:
    m = Mundo(tmp_path)
    m.archivo(_MOD, _ENTRADA)
    m.archivo(NOMBRE_DEL_MOD_DE_SALIDA, "textures/.vacio")
    (m.mods / _MALLAS).mkdir(parents=True)
    m.data.mkdir()
    m.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MALLAS}", f"+{_MOD}")
    return m


def _elegibilidad(mundo: Mundo, *extra: str, mod: str = _MOD) -> tuple[int, str]:
    def no_debe_pedir_texconv(_ruta: pathlib.Path) -> Any:  # pragma: no cover - solo corre si el comando lo pide
        raise AssertionError("la elegibilidad no debe pedir texconv")

    salida = io.StringIO()
    codigo = main(
        ["elegibilidad", "--mod", mod, "--mods", str(mundo.mods), "--data-root", str(mundo.data), *extra],
        entorno={},
        fabrica_de_exportador=no_debe_pedir_texconv,
        base_de_estado=mundo.raiz / "estado",
        salida=salida,
    )
    return codigo, salida.getvalue()


def test_elegibilidad_una_textura_con_malla_elegible_sale_en_cero_y_se_marca_si_con_su_malla(mundo: Mundo) -> None:
    mundo.archivo(_MALLAS, "meshes/muro.nif", _nif())

    codigo, texto = _elegibilidad(mundo)

    assert codigo == CODIGO_OK
    assert f"[SI] {_SALIDA}" in texto
    assert "meshes/muro.nif [Mallas]" in texto
    assert "1 SI, 0 NO, 0 NO_VERIFICADO" in texto


def test_elegibilidad_una_textura_sin_efecto_posible_tambien_sale_en_cero_porque_es_el_contenido_del_informe(
    mundo: Mundo,
) -> None:
    mundo.archivo(_MALLAS, "meshes/muro.nif", _nif(tipo_de_shader=3))

    codigo, texto = _elegibilidad(mundo)

    assert codigo == CODIGO_OK
    assert f"[NO] {_SALIDA}" in texto
    assert "relieve de fábrica" in texto
    assert "0 SI, 1 NO, 0 NO_VERIFICADO" in texto


def test_elegibilidad_sin_mallas_la_textura_queda_no_verificada_y_se_dice_por_que(mundo: Mundo) -> None:
    codigo, texto = _elegibilidad(mundo)

    assert codigo == CODIGO_OK
    assert f"[NO_VERIFICADO] {_SALIDA}" in texto
    assert ".bsa" in texto


def test_elegibilidad_la_salida_resume_el_perfil_el_mod_y_cuantas_mallas_se_leyeron(mundo: Mundo) -> None:
    mundo.archivo(_MALLAS, "meshes/muro.nif", _nif())

    _codigo, texto = _elegibilidad(mundo)

    assert "Perfil 'Default': 3 mods habilitados" in texto
    assert "1 textura de 'Texturas'" in texto
    assert "1 malla leída" in texto


def test_elegibilidad_las_mallas_ilegibles_se_listan_acotadas(mundo: Mundo) -> None:
    for i in range(8):
        mundo.archivo(_MALLAS, f"meshes/rota{i}.nif", b"no es un nif")

    codigo, texto = _elegibilidad(mundo)

    assert codigo == CODIGO_OK
    assert "8 mallas no se pudieron leer por completo" in texto
    assert "y 3 más" in texto  # se listan 5 y el resto se cuenta


def test_elegibilidad_la_salida_termina_con_el_alcance_para_no_presentarse_como_prueba_in_game(mundo: Mundo) -> None:
    _codigo, texto = _elegibilidad(mundo)

    assert "bAutoEnableParallax" in texto
    assert "Static" in texto


def test_elegibilidad_un_mod_que_no_esta_habilitado_es_un_error_de_uso(mundo: Mundo) -> None:
    codigo, texto = _elegibilidad(mundo, mod="Fantasma")

    assert codigo == CODIGO_DE_USO
    assert "Error:" in texto
    assert "no está habilitado" in texto


def test_elegibilidad_el_perfil_se_elige_con_el_argumento_perfil(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MALLAS}", f"+{_MOD}", perfil="Pruebas")
    mundo.archivo(_MALLAS, "meshes/muro.nif", _nif())

    codigo, texto = _elegibilidad(mundo, "--perfil", "Pruebas")

    assert codigo == CODIGO_OK
    assert "Perfil 'Pruebas'" in texto


def _foto(raiz: pathlib.Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None) for p in sorted(raiz.rglob("*"))}


def test_elegibilidad_no_escribe_nada_ni_pide_texconv_ni_crea_la_base_de_estado(mundo: Mundo) -> None:
    mundo.archivo(_MALLAS, "meshes/muro.nif", _nif())
    antes = _foto(mundo.raiz)

    codigo, _texto = _elegibilidad(mundo)

    assert codigo == CODIGO_OK
    assert _foto(mundo.raiz) == antes
    assert not (mundo.raiz / "estado").exists()


def test_elegibilidad_la_ayuda_documenta_los_codigos_de_salida() -> None:
    parser = construir_parser()
    acciones = [a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict)]  # noqa: SLF001
    ayuda = acciones[0].choices["elegibilidad"].format_help()

    for codigo in ("0 ", "2 "):
        assert codigo in ayuda
    assert "NO_VERIFICADO" in ayuda
