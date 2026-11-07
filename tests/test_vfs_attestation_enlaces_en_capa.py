"""#875 (parte B): ``vfs_attestation`` no puede elegir ni descartar un canary mirando a través de un enlace de capa.

``_construir_challenge`` validaba las capas del overlay con ``is_dir()`` y ``(capa / relativo).exists()``, que siguen enlaces.
Reproducido antes del cambio, con las funciones reales:

- un mod candidato que es un symlink a un árbol fuera de ``mods/`` se ACEPTABA y el canary salía de ese árbol ajeno (tanto con
  ``source_mod`` como en selección libre);
- un mod superior o ``overwrite`` que es un symlink se trataba como capa propia: si el árbol ajeno no tenía el archivo se
  ACEPTABA, y si lo tenía se rechazaba como "eclipsado" sin nombrar el enlace;
- un enlace DENTRO de una capa superior (``High/textures`` o el propio archivo) se ACEPTABA si el árbol ajeno no tenía el archivo.

La regla es la del resto del repo (``ganadores``, ``mod_effectivity``, y el propio Data físico de este módulo): un enlace no es
contenido propio. Cambio de producción que estos tests detectan: volver a validar una capa con ``is_dir()`` / ``exists()``.
Con discos reales; solo se inyecta el error del SO, porque el runner puede leerlo todo.

Límite: se prueba con symlinks. Junctions y reparse points de Windows se clasifican con la misma primitiva
(``links.link_kind_and_identity_or_raise``) pero no se ejercitan acá.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.mo2 import mod_effectivity, vfs_attestation
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    build_attestation_challenge_for_source,
)

_MOD = "TexGen Output"
_CANARY = b"CANARY_BYTES"
_OTROS = b"OTROS_BYTES"
_AJENO_CON_Y_SIN_ARCHIVO = [
    pytest.param(None, id="ajeno_sin_el_archivo"),
    pytest.param(_OTROS, id="ajeno_con_el_archivo"),
]


class Perfil:
    """Una instalación de MO2 mínima: perfil ``Default``, Data físico vacío y el mod ``TexGen Output`` con su canary."""

    def __init__(self, base: pathlib.Path) -> None:
        self.data_root = base / "MO2"
        self.mods = self.data_root / "mods"
        self.overwrite = self.data_root / "overwrite"
        self.fisico = base / "game" / "Data"
        self.fisico.mkdir(parents=True)
        #: Árbol que NO es capa de ningún perfil.
        self.ajeno = base / "ajeno"

    def modlist(self, texto: str) -> None:
        """Escribe ``modlist.txt``: la PRIMERA línea es la de MAYOR prioridad (``read_enabled_mods`` lo normaliza)."""
        carpeta = self.data_root / "profiles" / "Default"
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "modlist.txt").write_text(texto, encoding="utf-8")

    def texgen(self) -> None:
        """Crea ``TexGen Output/textures/canary.dds`` como carpeta real."""
        destino = self.mods / _MOD / "textures"
        destino.mkdir(parents=True)
        (destino / "canary.dds").write_bytes(_CANARY)

    def poblar_ajeno(self, contenido: bytes | None) -> pathlib.Path:
        """Árbol ajeno con ``textures/``; con ``contenido`` además lleva ``textures/canary.dds``."""
        (self.ajeno / "textures").mkdir(parents=True)
        if contenido is not None:
            (self.ajeno / "textures" / "canary.dds").write_bytes(contenido)
        return self.ajeno

    def elegir(self, seleccion: str = "source_mod") -> VfsAttestationChallenge:
        """Pide el canary: ``source_mod`` (restringido a ``TexGen Output``) o ``libre`` (todos los mods habilitados)."""
        comunes = {
            "data_root": self.data_root,
            "mods_dir": self.mods,
            "profile": "Default",
            "physical_data_dir": self.fisico,
        }
        if seleccion == "libre":
            return build_attestation_challenge(**comunes)
        return build_attestation_challenge_for_source(source_mod=_MOD, **comunes)


@pytest.fixture
def perfil(tmp_path: pathlib.Path) -> Perfil:
    return Perfil(tmp_path)


def _enlazar(enlace: pathlib.Path, destino: pathlib.Path, *, directorio: bool = True) -> None:
    """Crea ``enlace`` como symlink a ``destino`` (y sus carpetas padre); salta si el SO no lo permite sin privilegios."""
    enlace.parent.mkdir(parents=True, exist_ok=True)
    try:
        enlace.symlink_to(destino, target_is_directory=directorio)
    except (OSError, NotImplementedError):
        pytest.skip("este SO no permite crear symlinks sin privilegios")


# ---------------------------------------------------------------------------
# La RAÍZ de una capa que es un enlace no es una capa
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenido_ajeno", _AJENO_CON_Y_SIN_ARCHIVO)
@pytest.mark.parametrize("es_overwrite", [False, True], ids=["mod_superior", "overwrite"])
def test_una_raiz_de_capa_superior_que_es_un_enlace_no_se_toma_como_capa(
    perfil: Perfil, es_overwrite: bool, contenido_ajeno: bytes | None
) -> None:
    """Con el archivo en el árbol ajeno antes rechazaba como "eclipsado"; sin él, ACEPTABA. Ahora nombra el enlace."""
    perfil.modlist("+TexGen Output\n" if es_overwrite else "+High\n+TexGen Output\n")
    perfil.texgen()
    destino = perfil.poblar_ajeno(contenido_ajeno)
    _enlazar(perfil.overwrite if es_overwrite else perfil.mods / "High", destino)

    with pytest.raises(VfsAttestationError, match="enlace"):
        perfil.elegir()


@pytest.mark.parametrize(
    "contenido_ajeno", [pytest.param(_CANARY, id="bytes_identicos"), pytest.param(_OTROS, id="bytes_distintos")]
)
@pytest.mark.parametrize("seleccion", ["source_mod", "libre"])
def test_un_mod_candidato_que_es_un_enlace_no_se_acepta(perfil: Perfil, seleccion: str, contenido_ajeno: bytes) -> None:
    """El caso más serio: el canary salía de un árbol fuera de ``mods/`` y el gate lo aceptaba."""
    perfil.modlist("+TexGen Output\n")
    destino = perfil.poblar_ajeno(contenido_ajeno)
    (destino / "textures" / "canary.dds").write_bytes(contenido_ajeno)
    _enlazar(perfil.mods / _MOD, destino)

    with pytest.raises(VfsAttestationError, match="enlace"):
        perfil.elegir(seleccion)


# ---------------------------------------------------------------------------
# Un enlace DENTRO de una capa superior no deja elegir el canary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenido_ajeno", _AJENO_CON_Y_SIN_ARCHIVO)
@pytest.mark.parametrize("posicion", ["textures", "canary.dds"])
def test_un_enlace_en_el_path_dentro_de_una_capa_superior_no_deja_elegir_el_canary(
    perfil: Perfil, posicion: str, contenido_ajeno: bytes | None
) -> None:
    """Se enumeran los 2 componentes del path. Un enlace nunca es contenido propio, apunte o no a algo que exista."""
    perfil.modlist("+High\n+TexGen Output\n")
    perfil.texgen()
    destino = perfil.poblar_ajeno(contenido_ajeno)
    if posicion == "textures":
        _enlazar(perfil.mods / "High" / "textures", destino / "textures")
    else:
        _enlazar(
            perfil.mods / "High" / "textures" / "canary.dds", destino / "textures" / "canary.dds", directorio=False
        )

    with pytest.raises(VfsAttestationError, match="canary elegible"):
        perfil.elegir()


@pytest.mark.parametrize("posicion", ["textures", "canary.dds"])
def test_un_error_del_so_al_inspeccionar_una_capa_falla_cerrado_con_el_error_del_modulo(
    perfil: Perfil, monkeypatch: pytest.MonkeyPatch, posicion: str
) -> None:
    """Un ``OSError`` (p. ej. sin permiso de lectura) ni se absorbe como "path ausente" ni escapa crudo del módulo.

    Se inyecta porque este runner puede leerlo todo (p. ej. corre como root). Se enumeran los 2 componentes del path.
    """
    perfil.modlist("+High\n+TexGen Output\n")
    perfil.texgen()
    alto = perfil.mods / "High" / "textures"
    alto.mkdir(parents=True)
    (alto / "canary.dds").write_bytes(_OTROS)
    original = mod_effectivity.link_kind_and_identity_or_raise

    def falla_en_la_capa_alta(ruta: pathlib.Path) -> object:
        if "High" in ruta.parts and ruta.name == posicion:
            raise PermissionError(13, "Permission denied", str(ruta))
        return original(ruta)

    monkeypatch.setattr(mod_effectivity, "link_kind_and_identity_or_raise", falla_en_la_capa_alta)

    with pytest.raises(VfsAttestationError, match="no se pudo inspeccionar") as excinfo:
        perfil.elegir()

    assert isinstance(excinfo.value.__cause__, PermissionError)


# ---------------------------------------------------------------------------
# Lo que NO cambia
# ---------------------------------------------------------------------------


def test_una_capa_superior_real_con_el_mismo_path_sigue_eclipsando(perfil: Perfil) -> None:
    """Una capa superior real que provee el path sigue invalidando el canary."""
    perfil.modlist("+High\n+TexGen Output\n")
    perfil.texgen()
    (perfil.mods / "High" / "textures").mkdir(parents=True)
    (perfil.mods / "High" / "textures" / "canary.dds").write_bytes(_OTROS)

    with pytest.raises(VfsAttestationError, match="eclipsado"):
        perfil.elegir()


def test_un_overwrite_real_con_el_mismo_path_sigue_eclipsando(perfil: Perfil) -> None:
    """``overwrite`` gana a todo mod: si provee el path, el canary no es elegible."""
    perfil.modlist("+TexGen Output\n")
    perfil.texgen()
    (perfil.overwrite / "textures").mkdir(parents=True)
    (perfil.overwrite / "textures" / "canary.dds").write_bytes(_OTROS)

    with pytest.raises(VfsAttestationError, match="eclipsado"):
        perfil.elegir()


def test_un_overwrite_ausente_no_es_un_error(perfil: Perfil) -> None:
    """Un perfil sin carpeta ``overwrite`` es válido."""
    perfil.modlist("+TexGen Output\n")
    perfil.texgen()
    assert not perfil.overwrite.exists()

    assert perfil.elegir().relative_path.as_posix() == "textures/canary.dds"


def test_un_enlace_en_una_capa_de_menor_prioridad_no_importa(perfil: Perfil) -> None:
    """Con ``source_mod`` solo importan las capas por ENCIMA del mod: la de menor prioridad ni se mira."""
    perfil.modlist("+TexGen Output\n+Low\n")
    perfil.texgen()
    _enlazar(perfil.mods / "Low", perfil.poblar_ajeno(_OTROS))

    assert perfil.elegir().source_mod == _MOD


def test_un_mod_superior_habilitado_sin_carpeta_sigue_fallando_cerrado(perfil: Perfil) -> None:
    """Un mod habilitado sin carpeta es un perfil corrupto: no se finge que no eclipsa nada."""
    perfil.modlist("+Fantasma\n+TexGen Output\n")
    perfil.texgen()

    with pytest.raises(VfsAttestationError, match="no existe"):
        perfil.elegir()


# ---------------------------------------------------------------------------
# Ancla: los sitios de vfs_attestation que siguen enlaces están enumerados
# ---------------------------------------------------------------------------

#: Sitios que quedan, por (función, llamada), con su razón. Igualdad literal: un sitio nuevo rompe el ancla hasta que se le
#: escribe su receta, y agregarlo acá no es la salida sin una razón que el revisor pueda verificar.
#:
#: - ``_iter_mod_files`` / ``is_file``: se evalúa DESPUÉS de ``is_symlink()`` sobre un archivo ya enumerado por ``os.walk`` sin
#:   ``followlinks``: solo descarta lo que no es un archivo regular.
#: - ``verify_vfs_attestation`` / ``is_file``: es la vista RUNTIME ("¿qué ve el proceso bajo USVFS?"); ahí seguir lo que la
#:   proyección expone es justamente lo que se mide. No es resolución de capas.
_SITIOS_QUE_SIGUEN_ENLACES_PERMITIDOS = frozenset(
    {("_iter_mod_files", "is_file"), ("verify_vfs_attestation", "is_file")}
)
_LLAMADAS_QUE_SIGUEN_ENLACES = frozenset({"exists", "is_dir", "is_file", "isdir", "isfile", "stat"})


def _sitios_que_siguen_enlaces(fuente: str) -> set[tuple[str, str]]:
    """``(función contenedora, nombre)`` de cada llamada a ``_LLAMADAS_QUE_SIGUEN_ENLACES`` en *fuente*."""
    arbol = ast.parse(fuente)
    padres = {hijo: padre for padre in ast.walk(arbol) for hijo in ast.iter_child_nodes(padre)}

    def contenedora(nodo: ast.AST) -> str:
        while nodo in padres:
            nodo = padres[nodo]
            if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
                return nodo.name
        return "<módulo>"

    return {
        (contenedora(nodo), nodo.func.attr)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _LLAMADAS_QUE_SIGUEN_ENLACES
    }


def test_vfs_attestation_no_resuelve_capas_con_llamadas_que_siguen_enlaces() -> None:
    """Las capas se validan con la primitiva por ``lstat``; lo único que sigue enlaces está enumerado y justificado."""
    sitios = _sitios_que_siguen_enlaces(pathlib.Path(vfs_attestation.__file__).read_text(encoding="utf-8"))

    assert sitios == _SITIOS_QUE_SIGUEN_ENLACES_PERMITIDOS, (
        f"sitios que siguen enlaces en vfs_attestation: {sorted(sitios)}. Una capa se valida con "
        "`_capa_propia` / `_ocupa_el_path` (lstat); si el sitio nuevo no es de capas, justificalo en la receta del ancla."
    )
