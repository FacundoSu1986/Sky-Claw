"""#807: ``mod_effectivity`` no puede decidir sobre un árbol que no es una capa del overlay.

``_roots_eclipsantes`` armaba ``mods_dir / superior`` con nombres que salen de ``read_enabled_mods`` (que valida la sintaxis de
la línea, no que el nombre sea UNA carpeta) y aceptaba la capa con ``is_dir()``, que sigue enlaces. Resultado: un nombre como
``../../ajeno`` o un mod/``overwrite`` que es un symlink hacía que el gate mirara OTRO árbol y emitiera un veredicto sobre él.

El caso peligroso es el árbol ajeno con los MISMOS bytes del artifact: el gate lo aceptaba. Con bytes distintos ya fallaba
cerrado, pero por la razón equivocada ("eclipsado con bytes distintos") y con un mensaje que no nombra el problema real. Por eso
cada caso corre con las dos variantes de bytes.

La validación es la de su hermano ``mo2.ganadores`` (misma fuente, no una copia que pueda divergir); acá se verifica, con discos
reales y sin mocks salvo el reparse point de Windows, que:

- TODA capa superior se valida (no solo la primera ni la última) y ``overwrite`` también;
- el error es ``ModEffectivityError``: los callers (``brokered_dyndolod``, ``vfs_attestation``) capturan ese tipo y un
  ``GanadoresError`` que se filtrara se les escaparía de los handlers;
- un perfil legítimo sigue verificándose igual.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.mo2.mod_effectivity import ModEffectivityError, verificar_artifact_efectivo

_MOD = "TexGen Output"
_BYTES_AUTORIZADOS = b"AUTORIZADO"
_BYTES_AJENOS_DISTINTOS = b"AJENO-DISTINTO"
#: Las dos variantes que importan: idénticos (el gate aceptaba) y distintos (rechazaba por la razón equivocada).
_VARIANTES_DE_BYTES = [_BYTES_AUTORIZADOS, _BYTES_AJENOS_DISTINTOS]


class Perfil:
    """Una instalación de MO2 mínima con el artifact ``TexGen Output/textures/a.dds`` ya escrito."""

    def __init__(self, raiz: pathlib.Path) -> None:
        self.raiz = raiz / "instancia"
        self.mods = self.raiz / "mods"
        self.overwrite = self.raiz / "overwrite"
        self.artifact = self.mods / _MOD / "textures"
        self.artifact.mkdir(parents=True)
        (self.artifact / "a.dds").write_bytes(_BYTES_AUTORIZADOS)
        #: Árbol que NO es capa de ningún perfil: hermano de ``instancia``.
        self.ajeno = raiz / "ajeno"

    def poblar_ajeno(self, contenido: bytes) -> pathlib.Path:
        (self.ajeno / "textures").mkdir(parents=True)
        (self.ajeno / "textures" / "a.dds").write_bytes(contenido)
        return self.ajeno

    def capa_real(self, nombre: str, contenido: bytes) -> None:
        carpeta = self.mods / nombre / "textures"
        carpeta.mkdir(parents=True)
        (carpeta / "a.dds").write_bytes(contenido)

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


def _enlace_a_directorio(enlace: pathlib.Path, destino: pathlib.Path) -> None:
    """Crea un symlink a directorio; si el SO no lo permite sin privilegios, saltea el test en vez de fallar."""
    try:
        enlace.symlink_to(destino, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("este SO no permite crear symlinks sin privilegios")


# ---------------------------------------------------------------------------
# Nombres que no son UNA carpeta de mods_dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenido", _VARIANTES_DE_BYTES)
def test_un_nombre_que_escapa_de_mods_dir_no_se_acepta_aunque_el_arbol_ajeno_tenga_los_mismos_bytes(
    perfil: Perfil, contenido: bytes
) -> None:
    """``mods/../../ajeno`` es ``<tmp>/ajeno``: un árbol que no es una capa del overlay."""
    perfil.poblar_ajeno(contenido)

    with pytest.raises(ModEffectivityError, match="nombre de mod inválido"):
        perfil.verificar(_MOD, "../../ajeno")


@pytest.mark.parametrize("nombre", ["../afuera", "..", ".", "a/b", "a\\b", "/abs", "C:\\x", "", "x\x00y"])
def test_un_nombre_de_capa_superior_que_no_es_una_sola_carpeta_se_rechaza(perfil: Perfil, nombre: str) -> None:
    """Misma enumeración que el ancla de ``test_mo2_ganadores``: lo que ahí se rechaza, acá también."""
    with pytest.raises(ModEffectivityError, match="nombre de mod inválido"):
        perfil.verificar(_MOD, nombre)


@pytest.mark.parametrize("posicion_del_invalido", [0, 1, 2])
def test_se_valida_toda_capa_superior_y_no_solo_la_primera_ni_la_ultima(
    perfil: Perfil, posicion_del_invalido: int
) -> None:
    """El invariante es sobre TODAS las capas que se recorren: un muestreo de una sola posición no lo ancla."""
    perfil.capa_real("Uno", b"UNO")
    perfil.capa_real("Dos", b"DOS")
    perfil.capa_real("Tres", b"TRES")
    superiores = ["Uno", "Dos", "Tres"]
    superiores[posicion_del_invalido] = "../afuera"

    with pytest.raises(ModEffectivityError, match="nombre de mod inválido"):
        perfil.verificar(_MOD, *superiores)


# ---------------------------------------------------------------------------
# Capas que son enlaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenido", _VARIANTES_DE_BYTES)
def test_un_mod_superior_que_es_un_enlace_no_se_toma_como_capa_propia(perfil: Perfil, contenido: bytes) -> None:
    """``is_dir()`` sigue el symlink: sin validar la raíz, el veredicto salía del árbol ajeno."""
    _enlace_a_directorio(perfil.mods / "Superior", perfil.poblar_ajeno(contenido))

    with pytest.raises(ModEffectivityError, match=r"'Superior' es un enlace"):
        perfil.verificar(_MOD, "Superior")


@pytest.mark.parametrize("contenido", _VARIANTES_DE_BYTES)
def test_overwrite_que_es_un_enlace_no_se_toma_como_capa_propia(perfil: Perfil, contenido: bytes) -> None:
    _enlace_a_directorio(perfil.overwrite, perfil.poblar_ajeno(contenido))

    with pytest.raises(ModEffectivityError, match=r"overwrite es un enlace"):
        perfil.verificar(_MOD)


def test_un_mod_superior_que_es_un_archivo_regular_no_es_una_capa(perfil: Perfil) -> None:
    (perfil.mods / "Superior").write_bytes(b"no soy una carpeta")

    with pytest.raises(ModEffectivityError, match=r"'Superior' no es una carpeta"):
        perfil.verificar(_MOD, "Superior")


def test_un_punto_de_reanalisis_no_clasificado_se_traduce_a_error_del_gate(
    perfil: Perfil, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En Windows hay reparse points que no son symlink ni junction; ``links`` los rechaza con ``OSError``."""
    from sky_claw.local.mo2 import ganadores

    perfil.capa_real("Superior", b"x")

    def rechazar(_ruta: pathlib.Path, _identidad: object) -> None:
        raise PermissionError("punto de reanálisis no clasificado")

    monkeypatch.setattr(ganadores, "reject_unclassified_reparse_point", rechazar)

    with pytest.raises(ModEffectivityError, match="no se pudo inspeccionar"):
        perfil.verificar(_MOD, "Superior")


# ---------------------------------------------------------------------------
# Lo que NO cambia: un perfil legítimo se verifica como antes
# ---------------------------------------------------------------------------


def test_una_capa_superior_real_con_los_mismos_bytes_sigue_siendo_efectiva(perfil: Perfil) -> None:
    perfil.capa_real("Superior", _BYTES_AUTORIZADOS)

    assert perfil.verificar(_MOD, "Superior") == 1


def test_una_capa_superior_real_con_otros_bytes_sigue_eclipsando(perfil: Perfil) -> None:
    perfil.capa_real("Superior", _BYTES_AJENOS_DISTINTOS)

    with pytest.raises(ModEffectivityError, match="bytes distintos"):
        perfil.verificar(_MOD, "Superior")


def test_un_mod_superior_habilitado_sin_carpeta_sigue_fallando_cerrado(perfil: Perfil) -> None:
    with pytest.raises(ModEffectivityError, match=r"'Fantasma' no existe"):
        perfil.verificar(_MOD, "Fantasma")


def test_un_overwrite_ausente_no_es_un_error(perfil: Perfil) -> None:
    assert not perfil.overwrite.exists()

    assert perfil.verificar(_MOD) == 1
