"""#875 (parte A): un enlace DENTRO de una capa real no puede dar el veredicto de ``mod_effectivity``.

#807 validó la RAÍZ de cada capa (nombre y enlace). Quedaba un nivel más adentro: ``_ganador_eclipsante`` decidía si una capa
"contiene" un path con ``(root / relativo).exists()``, que sigue enlaces en TODOS los componentes, y el espejo se clasificaba con
``lstat``, que solo mira el ÚLTIMO. Con ``Superior/textures`` como symlink a un árbol ajeno, el gate leía el árbol ajeno como si
fuera de la capa: con bytes idénticos a los del artifact, aceptaba.

Cambio de producción que estos tests detectan: volver a decidir la existencia del path con ``.exists()`` / ``.is_dir()`` (que
siguen enlaces). Con discos reales, sin mocks:

- el enlace puede estar en CUALQUIER componente del path (se enumeran todos, no se muestrea uno), en un mod superior y en
  ``overwrite``, con bytes idénticos y distintos a los del artifact;
- el veredicto nombra el enlace;
- lo que NO cambia: un enlace que no está en el path del artifact, o en una capa de menor prioridad, no afecta; un archivo
  regular donde se esperaba un directorio no eclipsa; un perfil legítimo se verifica igual.

Límite: se prueba con symlinks. Junctions y reparse points de Windows se clasifican con la misma primitiva
(``links.link_kind_and_identity_or_raise``) pero no se ejercitan acá.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.mo2 import mod_effectivity
from sky_claw.local.mo2.mod_effectivity import ModEffectivityError, verificar_artifact_efectivo

_MOD = "TexGen Output"
#: Componentes del path Data-relativo del archivo del artifact; ``textures`` es el nombre del ``artifact_root``.
_RUTA = ("textures", "arq", "muro", "a.dds")
_AUTORIZADOS = b"AUTORIZADO"
_AJENOS_DISTINTOS = b"AJENO-DISTINTO"
_VARIANTES_DE_BYTES = [
    pytest.param(_AUTORIZADOS, id="bytes_identicos"),
    pytest.param(_AJENOS_DISTINTOS, id="bytes_distintos"),
]
_POSICIONES = [pytest.param(i, id=f"enlace_en_{_RUTA[i]}") for i in range(len(_RUTA))]
_CAPAS = [pytest.param(False, id="mod_superior"), pytest.param(True, id="overwrite")]


class Perfil:
    """Una instalación de MO2 mínima con el artifact ``TexGen Output/textures/arq/muro/a.dds`` ya escrito."""

    def __init__(self, base: pathlib.Path) -> None:
        self.raiz = base / "instancia"
        self.mods = self.raiz / "mods"
        self.overwrite = self.raiz / "overwrite"
        self.artifact = self.mods / _MOD / _RUTA[0]
        archivo = self.artifact.joinpath(*_RUTA[1:])
        archivo.parent.mkdir(parents=True)
        archivo.write_bytes(_AUTORIZADOS)
        #: Árbol que NO es capa de ningún perfil: hermano de ``instancia``.
        self.ajeno = base / "ajeno"

    def poblar_ajeno(self, contenido: bytes) -> pathlib.Path:
        archivo = self.ajeno.joinpath(*_RUTA)
        archivo.parent.mkdir(parents=True)
        archivo.write_bytes(contenido)
        return self.ajeno

    def capa_real(self, nombre: str, contenido: bytes) -> None:
        archivo = (self.mods / nombre).joinpath(*_RUTA)
        archivo.parent.mkdir(parents=True)
        archivo.write_bytes(contenido)

    def verificar(self, *enabled: str) -> int:
        return verificar_artifact_efectivo(
            artifact_root=self.artifact,
            mod_name=_MOD,
            mods_dir=self.mods,
            data_root=self.raiz,
            enabled=enabled,
        )


@pytest.fixture
def perfil(tmp_path: pathlib.Path) -> Perfil:
    return Perfil(tmp_path)


def _enlazar(capa: pathlib.Path, posicion: int, destino: pathlib.Path) -> None:
    """Deja ``capa`` con directorios reales hasta ``_RUTA[posicion]``, que es un symlink a ``destino``."""
    carpeta = capa.joinpath(*_RUTA[:posicion])
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        (carpeta / _RUTA[posicion]).symlink_to(destino, target_is_directory=posicion < len(_RUTA) - 1)
    except (OSError, NotImplementedError):
        pytest.skip("este SO no permite crear symlinks sin privilegios")


def _enabled_con_capa_superior(es_overwrite: bool) -> tuple[str, ...]:
    return (_MOD,) if es_overwrite else (_MOD, "Superior")


def _capa_a_enlazar(perfil: Perfil, es_overwrite: bool) -> pathlib.Path:
    return perfil.overwrite if es_overwrite else perfil.mods / "Superior"


# ---------------------------------------------------------------------------
# Un enlace en CUALQUIER componente del path del artifact no es contenido propio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenido", _VARIANTES_DE_BYTES)
@pytest.mark.parametrize("posicion", _POSICIONES)
@pytest.mark.parametrize("es_overwrite", _CAPAS)
def test_un_enlace_en_cualquier_componente_del_path_no_se_toma_como_contenido_de_la_capa(
    perfil: Perfil, es_overwrite: bool, posicion: int, contenido: bytes
) -> None:
    """Se enumeran los 4 componentes: un muestreo de uno solo no ancla el invariante."""
    ajeno = perfil.poblar_ajeno(contenido)
    _enlazar(_capa_a_enlazar(perfil, es_overwrite), posicion, ajeno.joinpath(*_RUTA[: posicion + 1]))

    with pytest.raises(ModEffectivityError, match="enlace"):
        perfil.verificar(*_enabled_con_capa_superior(es_overwrite))


def test_el_veredicto_nombra_el_enlace_que_atraviesa_el_path(perfil: Perfil) -> None:
    ajeno = perfil.poblar_ajeno(_AUTORIZADOS)
    _enlazar(perfil.mods / "Superior", 1, ajeno.joinpath(*_RUTA[:2]))

    with pytest.raises(ModEffectivityError, match=r"mod 'Superior'.*enlace 'textures/arq'"):
        perfil.verificar(_MOD, "Superior")


def test_un_enlace_colgante_en_el_path_tambien_es_un_ocupante_incompatible(
    perfil: Perfil, tmp_path: pathlib.Path
) -> None:
    """Decisión fail-closed: un enlace nunca es contenido propio, apunte o no a algo que exista."""
    _enlazar(perfil.mods / "Superior", 0, tmp_path / "no_existe")

    with pytest.raises(ModEffectivityError, match="enlace"):
        perfil.verificar(_MOD, "Superior")


# ---------------------------------------------------------------------------
# Lo que NO cambia
# ---------------------------------------------------------------------------


def test_un_enlace_que_no_esta_en_el_path_del_artifact_no_afecta(perfil: Perfil) -> None:
    ajeno = perfil.poblar_ajeno(_AJENOS_DISTINTOS)
    carpeta = perfil.mods / "Superior" / _RUTA[0]
    carpeta.mkdir(parents=True)
    try:
        (carpeta / "otro").symlink_to(ajeno.joinpath(*_RUTA[:2]), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("este SO no permite crear symlinks sin privilegios")

    assert perfil.verificar(_MOD, "Superior") == 1


def test_un_enlace_en_una_capa_de_menor_prioridad_no_importa(perfil: Perfil) -> None:
    ajeno = perfil.poblar_ajeno(_AJENOS_DISTINTOS)
    _enlazar(perfil.mods / "Inferior", 0, ajeno.joinpath(_RUTA[0]))

    assert perfil.verificar("Inferior", _MOD) == 1


def test_un_archivo_regular_donde_se_esperaba_un_directorio_no_eclipsa(perfil: Perfil) -> None:
    """Antes ``exists()`` lo absorbía como ausente (ENOTDIR): el recorrido por componentes tiene que seguir haciéndolo."""
    (perfil.mods / "Superior").mkdir()
    (perfil.mods / "Superior" / _RUTA[0]).write_bytes(b"soy un archivo, no una carpeta")

    assert perfil.verificar(_MOD, "Superior") == 1


def test_una_capa_superior_real_con_los_mismos_bytes_sigue_siendo_efectiva(perfil: Perfil) -> None:
    perfil.capa_real("Superior", _AUTORIZADOS)

    assert perfil.verificar(_MOD, "Superior") == 1


def test_una_capa_superior_real_con_otros_bytes_sigue_eclipsando(perfil: Perfil) -> None:
    perfil.capa_real("Superior", _AJENOS_DISTINTOS)

    with pytest.raises(ModEffectivityError, match="bytes distintos"):
        perfil.verificar(_MOD, "Superior")


# ---------------------------------------------------------------------------
# Ancla: el módulo no vuelve a decidir la existencia de un path siguiendo enlaces
# ---------------------------------------------------------------------------

#: ``Path.exists`` / ``is_dir`` / ``is_file`` siguen enlaces: en ``mod_effectivity`` la existencia y el tipo se deciden con
#: ``link_kind_and_identity_or_raise`` (``lstat``). Igualdad literal: un sitio nuevo rompe el ancla hasta que se le escribe su
#: receta, y agregarlo a este conjunto no es la salida sin una razón que el revisor pueda verificar.
_LLAMADAS_QUE_SIGUEN_ENLACES = frozenset({"exists", "is_dir", "is_file"})


def test_mod_effectivity_no_decide_existencia_ni_tipo_con_llamadas_que_siguen_enlaces() -> None:
    fuente = pathlib.Path(mod_effectivity.__file__).read_text(encoding="utf-8")
    sitios = sorted(
        (nodo.lineno, nodo.func.attr)
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _LLAMADAS_QUE_SIGUEN_ENLACES
    )

    assert sitios == [], (
        f"mod_effectivity usa llamadas que siguen enlaces (línea, nombre): {sitios}. Clasificá con "
        "`link_kind_and_identity_or_raise` (lstat) como el resto del módulo."
    )
