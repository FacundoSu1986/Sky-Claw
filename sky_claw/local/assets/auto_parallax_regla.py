"""La regla con la que el plugin de relieve automático (Auto Parallax) decide sobre cada forma de una malla (NP-A0j).

Transcripción de ``src/XSEPlugin.cpp`` del plugin público ``doodlum/skyrim-auto-parallax`` (rama ``main`` descargada el 2026-10-27,
SHA-256 ``c70f6e21e2cc2c5241b711db358963bda5ee3bb297a9f05fbd52cab2a4a188a3``). **Lo que NO está verificado:** que la versión instalada
en un rig (v1-0-6-0 en el del operador) se comporte como esa rama; el log del plugin en el juego lo confirma o lo desmiente.

Lo que dice el código, para cada forma cuyo shader es un ``BSLightingShaderProperty``:

- tipo de shader **Parallax (3)**: el plugin apaga el relieve de fábrica. Nuestra salida no se usa.
- tipo de shader **Default (0)**: enciende relieve solo si la forma NO tiene ``Projected_UV``, ``Skinned``, ``LOD_Objects``,
  ``Tree_Anim`` ni ``Vertex_Lighting``, SÍ tiene ``Vertex_Colors``, NO tiene ninguna propiedad de alfa y encuentra un ``_p``.
- cualquier otro tipo de shader (o un shader que no sea de iluminación): no hace nada.

Y para encontrar el ``_p`` prueba, en este orden, la ranura de altura del juego de texturas (la ruta tal cual, si existe), la ruta
derivada del difuso y la derivada de la normal. La derivación del original distingue mayúsculas; el motor puede no hacerlo, así que
una ruta cuyo resultado depende de eso se declara ``ambigua`` y nunca se adivina.

**Alcance.** Además de la forma, el plugin solo actúa sobre objetos base de tipo Static, Movable Static y Container (puertas,
activadores, árboles, mobiliario… quedan fuera) y solo enciende relieve si ``bAutoEnableParallax`` está activo en su INI (por defecto
no). Ninguna de las dos cosas se ve en el NIF: quien consuma este módulo tiene que decirlo.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from sky_claw.local.assets.nif_materiales import FormaDeNif

__all__ = [
    "BIT_VERTEX_COLORS",
    "EXCLUSIONES_DE_LA_RAMA_QUE_ENCIENDE",
    "CandidatoDeRelieve",
    "Candidatos",
    "Decision",
    "EstadoDeLaBusqueda",
    "Motivo",
    "OrigenDelCandidato",
    "ResultadoDeLaBusqueda",
    "Veredicto",
    "buscar_el_relieve",
    "candidatos_de_relieve",
    "evaluar_forma",
]


class Veredicto(Enum):
    ELEGIBLE = "ELEGIBLE"  #: el plugin enciende relieve si encuentra un ``_p``.
    PARALLAX_DE_FABRICA = (
        "PARALLAX_DE_FABRICA"  #: la malla ya trae relieve y el plugin lo apaga: nuestro ``_p`` no se usa.
    )
    NO_ACTUA = "NO_ACTUA"  #: el plugin no toca esta forma.
    EXCLUIDA = "EXCLUIDA"  #: shader Default, pero con alguna condición que impide encender el relieve.


class Motivo(Enum):
    PROJECTED_UV = "PROJECTED_UV"
    SKINNED = "SKINNED"
    LOD_OBJECTS = "LOD_OBJECTS"
    TREE_ANIM = "TREE_ANIM"
    VERTEX_LIGHTING = "VERTEX_LIGHTING"
    SIN_VERTEX_COLORS = "SIN_VERTEX_COLORS"
    CON_PROPIEDAD_DE_ALFA = "CON_PROPIEDAD_DE_ALFA"
    SIN_JUEGO_DE_TEXTURAS = "SIN_JUEGO_DE_TEXTURAS"
    TIPO_DE_SHADER_NO_ADMITIDO = "TIPO_DE_SHADER_NO_ADMITIDO"
    SHADER_NO_DE_ILUMINACION = "SHADER_NO_DE_ILUMINACION"
    SIN_PROPIEDAD_DE_SHADER = "SIN_PROPIEDAD_DE_SHADER"


#: ``(registro de flags, bit)`` de cada flag que, si está presente, impide que el plugin encienda el relieve. Los nombres son los de
#: ``SkyrimShaderPropertyFlags1/2`` de la especificación NifTools; el plugin usa los mismos (``kProjectedUV``…). **Verificado** contra la cabecera
#: pública de CommonLibSSE (``include/RE/B/BSShaderProperty.h``, rama ``master`` del 2026-10-27, SHA-256
#: ``ba6b95d2346568f84f8d7eee7a181b1862f034841f719dd65f9cda6e05a54cf5``): ahí los flags son un ``uint64`` donde el registro 1 ocupa los bits 0-31 y
#: el 2 los 32-63 (``kSkinned = 1<<1``, ``kProjectedUV = 1<<23``, ``kLODObjects = 1<<34``, ``kVertexLighting = 1<<42``, ``kTreeAnim = 1<<61``,
#: ``kVertexColors = 1<<37``): coinciden con estas posiciones. Lo que sigue sin verificarse es la versión instalada del plugin y de CommonLibSSE.
EXCLUSIONES_DE_LA_RAMA_QUE_ENCIENDE: Final[dict[Motivo, tuple[int, int]]] = {
    Motivo.PROJECTED_UV: (1, 23),
    Motivo.SKINNED: (1, 1),
    Motivo.LOD_OBJECTS: (2, 2),
    Motivo.TREE_ANIM: (2, 29),
    Motivo.VERTEX_LIGHTING: (2, 10),
}
#: El flag que el plugin EXIGE (``kVertexColors``).
BIT_VERTEX_COLORS: Final[tuple[int, int]] = (2, 5)

_SHADER_DE_ILUMINACION: Final = "BSLightingShaderProperty"
_TIPO_DEFAULT: Final = 0
_TIPO_PARALLAX: Final = 3


@dataclass(frozen=True, slots=True)
class Decision:
    veredicto: Veredicto
    motivos: frozenset[Motivo]


def _tiene(forma: FormaDeNif, ubicacion: tuple[int, int]) -> bool:
    registro, bit = ubicacion
    flags = forma.flags_1 if registro == 1 else forma.flags_2
    if flags is None:
        raise ValueError("una forma con shader de iluminación debe traer flags")
    return bool(flags & (1 << bit))


def evaluar_forma(forma: FormaDeNif) -> Decision:
    """El veredicto del plugin sobre *forma* (ver el docstring del módulo para la regla y su alcance)."""
    if forma.propiedad_de_shader is None:
        return Decision(Veredicto.NO_ACTUA, frozenset({Motivo.SIN_PROPIEDAD_DE_SHADER}))
    if forma.propiedad_de_shader != _SHADER_DE_ILUMINACION:
        return Decision(Veredicto.NO_ACTUA, frozenset({Motivo.SHADER_NO_DE_ILUMINACION}))
    if forma.tipo_de_shader == _TIPO_PARALLAX:
        return Decision(Veredicto.PARALLAX_DE_FABRICA, frozenset())
    if forma.tipo_de_shader != _TIPO_DEFAULT:
        return Decision(Veredicto.NO_ACTUA, frozenset({Motivo.TIPO_DE_SHADER_NO_ADMITIDO}))
    motivos = {motivo for motivo, ubicacion in EXCLUSIONES_DE_LA_RAMA_QUE_ENCIENDE.items() if _tiene(forma, ubicacion)}
    if not _tiene(forma, BIT_VERTEX_COLORS):
        motivos.add(Motivo.SIN_VERTEX_COLORS)
    if forma.tiene_propiedad_de_alfa:
        motivos.add(Motivo.CON_PROPIEDAD_DE_ALFA)
    if forma.texturas is None:
        motivos.add(Motivo.SIN_JUEGO_DE_TEXTURAS)
    return Decision(Veredicto.EXCLUIDA if motivos else Veredicto.ELEGIBLE, frozenset(motivos))


# --- Qué `_p` busca el plugin -------------------------------------------------------------------------------------------

_RANURA_DIFUSA: Final = 0
_RANURA_NORMAL: Final = 1
_RANURA_ALTURA: Final = 3
_EXTENSION: Final = ".dds"
_LARGO_DE_SUFIJO_Y_EXTENSION: Final = 6  # ``_d.dds`` / ``_n.dds``


class OrigenDelCandidato(Enum):
    ALTURA = "ALTURA"
    DIFUSO = "DIFUSO"
    NORMAL = "NORMAL"


@dataclass(frozen=True, slots=True)
class CandidatoDeRelieve:
    origen: OrigenDelCandidato
    ruta: str  #: Vacía si la ruta original es demasiado corta para razonar sobre ella (siempre con ``ambigua``).
    ambigua: bool = False  #: Su ruta depende de cómo el motor trate mayúsculas o de una carpeta ``textures`` implícita.


@dataclass(frozen=True, slots=True)
class Candidatos:
    candidatos: tuple[CandidatoDeRelieve, ...]
    ambiguo: (
        bool  #: alguna ruta usada depende de cómo el motor trate mayúsculas o de una carpeta ``textures`` implícita.
    )


class EstadoDeLaBusqueda(Enum):
    ENCUENTRA = "ENCUENTRA"
    NO_ENCUENTRA = "NO_ENCUENTRA"
    NO_VERIFICADO = "NO_VERIFICADO"


@dataclass(frozen=True, slots=True)
class ResultadoDeLaBusqueda:
    ruta: str | None
    origen: OrigenDelCandidato | None
    estado: EstadoDeLaBusqueda


def _derivar(ruta: str, sufijo: str, *, distinguir_mayusculas: bool) -> str | None:
    """``Parallaxilize`` del plugin: ``x<sufijo>.dds`` y ``x.dds`` pasan a ``x_p.dds``; otra extensión no deriva nada."""
    texto = ruta if distinguir_mayusculas else ruta.lower()
    if texto.endswith(sufijo + _EXTENSION):
        return ruta[:-_LARGO_DE_SUFIJO_Y_EXTENSION] + "_p.dds"
    if texto.endswith(_EXTENSION):
        return ruta[: -len(_EXTENSION)] + "_p.dds"
    return None


def _empieza_por_textures(ruta: str) -> bool:
    return ruta.lower().startswith(("textures\\", "textures/"))


def candidatos_de_relieve(texturas: tuple[str, ...]) -> Candidatos:
    """Los ``_p`` que el plugin probaría para este juego de texturas, en su orden, cada uno con su propia ambigüedad."""
    candidatos: list[CandidatoDeRelieve] = []

    if len(texturas) > _RANURA_ALTURA and texturas[_RANURA_ALTURA]:
        altura = texturas[_RANURA_ALTURA]
        candidatos.append(
            CandidatoDeRelieve(OrigenDelCandidato.ALTURA, altura, ambigua=not _empieza_por_textures(altura))
        )

    for ranura, sufijo, origen in (
        (_RANURA_DIFUSA, "_d", OrigenDelCandidato.DIFUSO),
        (_RANURA_NORMAL, "_n", OrigenDelCandidato.NORMAL),
    ):
        if len(texturas) <= ranura or not texturas[ranura]:
            continue
        ruta = texturas[ranura]
        if len(ruta) < _LARGO_DE_SUFIJO_Y_EXTENSION:
            # el original hace aritmética sin signo sobre el largo: no se razona sobre rutas tan cortas
            candidatos.append(CandidatoDeRelieve(origen, "", ambigua=True))
            continue
        distingue = _derivar(ruta, sufijo, distinguir_mayusculas=True)
        no_distingue = _derivar(ruta, sufijo, distinguir_mayusculas=False)
        if no_distingue is None:
            continue  # ni distinguiendo mayúsculas ni sin distinguirlas deriva nada: esta ranura no aporta candidato
        ambigua = distingue != no_distingue or not _empieza_por_textures(ruta)
        candidatos.append(CandidatoDeRelieve(origen, no_distingue, ambigua=ambigua))
    return Candidatos(candidatos=tuple(candidatos), ambiguo=any(c.ambigua for c in candidatos))


def buscar_el_relieve(texturas: tuple[str, ...], *, existe: Callable[[str], bool | None]) -> ResultadoDeLaBusqueda:
    """Reproduce la búsqueda del plugin sobre un juego de texturas.

    Args:
        texturas: las ranuras del ``BSShaderTextureSet`` de la forma.
        existe: ``True`` si la ruta existe en el overlay, ``False`` si seguro no, ``None`` si no se puede saber (por ejemplo si
            podría estar dentro de un ``.bsa`` que no se lee). Se le pasa la ruta como la derivó el plugin; comparar sin distinguir
            mayúsculas ni tipo de barra es cosa de quien la implementa.
    """
    for candidato in candidatos_de_relieve(texturas).candidatos:
        if candidato.ambigua:
            # La búsqueda llegó a un candidato cuya ruta depende de lo que no se sabe: no se consulta ni se adivina. Uno anterior que
            # existía ya cortó la búsqueda (el plugin devuelve en cuanto encuentra).
            return ResultadoDeLaBusqueda(ruta=None, origen=None, estado=EstadoDeLaBusqueda.NO_VERIFICADO)
        presente = existe(candidato.ruta)
        if presente is None:
            return ResultadoDeLaBusqueda(ruta=None, origen=None, estado=EstadoDeLaBusqueda.NO_VERIFICADO)
        if presente:
            return ResultadoDeLaBusqueda(
                ruta=candidato.ruta, origen=candidato.origen, estado=EstadoDeLaBusqueda.ENCUENTRA
            )
    return ResultadoDeLaBusqueda(ruta=None, origen=None, estado=EstadoDeLaBusqueda.NO_ENCUENTRA)
