"""``EFECTO_POSIBLE`` por textura: ¿hay alguna malla del perfil donde el plugin de relieve use lo que el generador publica? (NP-A0j, #816).

El generador publica ``x_p.dds`` para cada ``x_n.dds`` de un mod. Que el archivo exista no quiere decir que se vea: el plugin Auto
Parallax solo enciende relieve en ciertas formas (``assets/auto_parallax_regla.py``) y apaga el de las que ya lo traen de fábrica. En la
prueba en el rig, 3 de 4 texturas estaban en mallas de esas últimas. Este módulo cruza las dos cosas con el perfil de MO2: para cada
salida que el generador publicaría busca, entre las mallas que el overlay entrega de verdad (las que GANAN), las formas que la
referencian y aplica la regla del plugin, incluido el orden de su búsqueda (altura → difuso → normal).

Tres respuestas, y solo tres (``EfectoPosible``): ``SI`` (una forma elegible terminaría usando esa salida), ``NO`` (TODAS las formas que la
referencian quedan fuera por una razón que se nombra, y nada de lo leído quedó incompleto) y ``NO_VERIFICADO`` (todo lo demás: ninguna
malla suelta la referencia, la búsqueda del plugin depende de un archivo que no se puede comprobar, o alguna malla no se pudo leer por
completo). Lo no observado no se da por ausente.

Es de **solo lectura** y no escribe ni genera nada. Reusa la resolución del perfil de ``diagnostico_perfil`` (``cargar_perfil``, el índice de
mallas y la enumeración de las normales del mod): si resolviera el perfil por su cuenta contestaría distinto que el resto del diagnóstico.
"""

from __future__ import annotations

import pathlib
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from sky_claw.local.assets.auto_parallax_regla import (
    Decision,
    Motivo,
    Veredicto,
    buscar_el_relieve,
    candidatos_de_relieve,
    evaluar_forma,
)
from sky_claw.local.assets.nif_materiales import NifError, leer_materiales_de_archivo
from sky_claw.local.mo2.ganadores import GanadoresError
from sky_claw.local.native_parallax.diagnostico_perfil import (
    MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
    SUFIJO_DE_ENTRADA,
    PerfilError,
    cargar_mallas_del_perfil,
    cargar_perfil,
    descubrir_normales_del_mod,
)

__all__ = [
    "ALCANCE_DE_LA_ELEGIBILIDAD",
    "MAXIMO_DE_ARCHIVOS_EN_MESHES",
    "MAXIMO_DE_MALLAS_POR_TEXTURA",
    "EfectoPosible",
    "ElegibilidadDeTextura",
    "InformeDeElegibilidad",
    "evaluar_elegibilidad",
]

#: Tope de archivos visitados bajo ``meshes/`` entre todas las capas: un perfil real pesa decenas de miles; pasarlo no devuelve un
#: índice a medias.
MAXIMO_DE_ARCHIVOS_EN_MESHES: Final = 500_000
#: Cuántas mallas se nombran por textura (el resto se cuenta pero no se lista).
MAXIMO_DE_MALLAS_POR_TEXTURA: Final = 20

#: Lo que esta lectura prueba y lo que no: viaja en cada informe para que ninguna superficie lo presente como una prueba in-game.
ALCANCE_DE_LA_ELEGIBILIDAD: Final = (
    "Lectura estructural de las mallas sueltas del perfil. El plugin solo actúa sobre objetos base Static, Movable Static y Container "
    "y solo enciende relieve si bAutoEnableParallax está activo en su INI (por defecto no): ninguna de las dos cosas se ve en la malla. "
    "No lee mallas dentro de un .bsa. SI quiere decir que una forma elegible terminaría usando la salida, no que se vea bien: no prueba "
    "lo que se ve en el juego, y la versión instalada del plugin puede diferir de la que se leyó."
)

_TEXTO_DEL_MOTIVO: Final[dict[Motivo, str]] = {
    Motivo.PROJECTED_UV: "Projected_UV",
    Motivo.SKINNED: "Skinned",
    Motivo.LOD_OBJECTS: "LOD_Objects",
    Motivo.TREE_ANIM: "Tree_Anim",
    Motivo.VERTEX_LIGHTING: "Vertex_Lighting",
    Motivo.SIN_VERTEX_COLORS: "sin colores de vértice",
    Motivo.CON_PROPIEDAD_DE_ALFA: "con transparencia (NiAlphaProperty)",
    Motivo.SIN_JUEGO_DE_TEXTURAS: "sin juego de texturas",
    Motivo.TIPO_DE_SHADER_NO_ADMITIDO: "otro tipo de shader",
    Motivo.SHADER_NO_DE_ILUMINACION: "shader que no es de iluminación",
    Motivo.SIN_PROPIEDAD_DE_SHADER: "sin propiedad de shader",
}
_TEXTO_DE_FABRICA: Final = "relieve de fábrica (el plugin lo apaga)"


class EfectoPosible(Enum):
    SI = "SI"
    NO = "NO"
    NO_VERIFICADO = "NO_VERIFICADO"


@dataclass(frozen=True, slots=True)
class ElegibilidadDeTextura:
    ruta_de_salida: str  #: ``textures/.../x_p.dds``, la que el generador publicaría.
    efecto: EfectoPosible
    razon: str
    mallas: tuple[
        str, ...
    ]  #: Solo con ``SI``: ``meshes/....nif [capa]`` donde se usaría (acotado a ``MAXIMO_DE_MALLAS_POR_TEXTURA``).
    n_mallas: int  #: Cuántas mallas la usarían (``len(mallas)`` puede ser menor por el tope).


@dataclass(frozen=True, slots=True)
class InformeDeElegibilidad:
    perfil: str
    mod: str
    n_habilitados: int
    n_mallas_leidas: int
    mallas_no_leidas: tuple[str, ...]  #: Ilegibles o con geometría que el lector no sabe abrir: lo no observado.
    texturas: tuple[ElegibilidadDeTextura, ...]
    alcance: str


@dataclass(slots=True)
class _Acumulado:
    mallas_si: set[str] = field(default_factory=set)
    hay_pendientes: bool = False
    otra_textura: int = 0
    referencias: int = 0
    motivos: Counter[str] = field(default_factory=Counter)


def _clave(ruta: str) -> str:
    """La ruta como la compara el juego (sin distinguir mayúsculas ni el tipo de barra)."""
    return ruta.replace("\\", "/").lower()


def _etiqueta(decision: Decision) -> str:
    if decision.veredicto is Veredicto.PARALLAX_DE_FABRICA:
        return _TEXTO_DE_FABRICA
    return " + ".join(sorted(_TEXTO_DEL_MOTIVO[m] for m in decision.motivos))


def _mallas_en_texto(cantidad: int) -> str:
    return "1 malla" if cantidad == 1 else f"{cantidad} mallas"


def _veredicto_de_la_textura(ruta: str, acum: _Acumulado, no_leidas: tuple[str, ...]) -> ElegibilidadDeTextura:
    def con(efecto: EfectoPosible, razon: str, mallas: tuple[str, ...] = (), n: int = 0) -> ElegibilidadDeTextura:
        return ElegibilidadDeTextura(ruta_de_salida=ruta, efecto=efecto, razon=razon, mallas=mallas, n_mallas=n)

    aviso_de_lectura = (
        f"; además {_mallas_en_texto(len(no_leidas))} no se pudieron leer por completo" if no_leidas else ""
    )
    if acum.mallas_si:
        ordenadas = tuple(sorted(acum.mallas_si))
        return con(
            EfectoPosible.SI,
            f"{_mallas_en_texto(len(ordenadas))} donde una forma elegible la usaría",
            ordenadas[:MAXIMO_DE_MALLAS_POR_TEXTURA],
            len(ordenadas),
        )
    if acum.hay_pendientes:
        return con(
            EfectoPosible.NO_VERIFICADO,
            "alguna forma elegible podría usarla, pero la búsqueda del plugin depende de un archivo del que no se puede comprobar si "
            f"existe (¿dentro de un .bsa?){aviso_de_lectura}",
        )
    if acum.referencias == 0:
        return con(
            EfectoPosible.NO_VERIFICADO,
            "ninguna malla suelta del perfil la referencia: podría usarla una malla dentro de un .bsa (no se leen)"
            f"{aviso_de_lectura}",
        )
    if no_leidas:
        return con(
            EfectoPosible.NO_VERIFICADO,
            f"{_mallas_en_texto(len(no_leidas))} no se pudieron leer por completo y no se descarta que alguna la use",
        )
    partes = [f"{etiqueta}: {cantidad}" for etiqueta, cantidad in sorted(acum.motivos.items())]
    if acum.otra_textura:
        partes.append(f"el plugin usaría otra textura antes que esta: {acum.otra_textura}")
    forma = "forma" if acum.referencias == 1 else "formas"
    return con(
        EfectoPosible.NO,
        f"ninguna de las {acum.referencias} {forma} que la referencian puede usarla ({'; '.join(partes)})",
    )


def evaluar_elegibilidad(
    *,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    perfil: str,
    mod: str,
    maximo_de_mallas: int = MAXIMO_DE_ARCHIVOS_EN_MESHES,
) -> InformeDeElegibilidad:
    """Para cada ``*_n.dds`` de *mod*, ¿alguna malla del perfil usaría el ``*_p.dds`` que el generador publicaría?

    Síncrono y de solo lectura: lee todas las mallas ganadoras del overlay, así que quien lo llame desde código async lo corre en un hilo.

    Raises:
        PerfilError: el perfil no permite afirmar quién gana, el mod no tiene normales procesables o hay más archivos que el tope.
    """
    resuelto = cargar_perfil(mods_dir=mods_dir, data_root=data_root, perfil=perfil, mod=mod)
    normales, _omitidas = descubrir_normales_del_mod(
        resuelto.carpeta_del_mod, exigir_completa=True, maximo_de_archivos=MAXIMO_DE_ARCHIVOS_DE_ENTRADA
    )
    if not normales:
        raise PerfilError(
            f"no encontré ningún *{SUFIJO_DE_ENTRADA} procesable bajo {resuelto.carpeta_del_mod}/textures"
        )
    indice_de_mallas = cargar_mallas_del_perfil(
        mods_dir=mods_dir, data_root=data_root, resuelto=resuelto, maximo_de_archivos=maximo_de_mallas
    )

    salidas = {_clave(n.ruta_de_salida): n.ruta_de_salida for n in normales}
    acumulados = {clave: _Acumulado() for clave in salidas}

    def existe(ruta: str) -> bool | None:
        """``True`` si es una salida que se publicaría o está suelta en el overlay; ``None`` si no se puede saber (¿dentro de un .bsa?)."""
        clave = _clave(ruta)
        if clave in salidas:
            return True
        try:
            ganador = resuelto.indice.ganador(clave)
        except GanadoresError:
            return None
        return True if ganador is not None else None

    leidas = 0
    no_leidas: list[str] = []
    for ganador in indice_de_mallas.con_sufijo(".nif"):
        etiqueta_de_malla = f"{ganador.ruta_virtual} [{ganador.capa}]"
        try:
            lectura = leer_materiales_de_archivo(ganador.archivo)
        except NifError:
            no_leidas.append(etiqueta_de_malla)
            continue
        leidas += 1
        if lectura.formas_no_soportadas:
            no_leidas.append(etiqueta_de_malla)
        for forma in lectura.formas:
            if forma.texturas is None:
                continue
            candidatos = candidatos_de_relieve(forma.texturas)
            referidas = {_clave(c.ruta) for c in candidatos.candidatos} & salidas.keys()
            if not referidas:
                continue
            for clave in referidas:
                acumulados[clave].referencias += 1
            decision = evaluar_forma(forma)
            if decision.veredicto is not Veredicto.ELEGIBLE:
                for clave in referidas:
                    acumulados[clave].motivos[_etiqueta(decision)] += 1
                continue
            busqueda = buscar_el_relieve(forma.texturas, existe=existe)
            if busqueda.ruta is None:  # NO_VERIFICADO (o, sin que debiera pasar, NO_ENCUENTRA): no se afirma nada
                for clave in referidas:
                    acumulados[clave].hay_pendientes = True
                continue
            usada = _clave(busqueda.ruta)
            if usada in salidas:
                acumulados[usada].mallas_si.add(etiqueta_de_malla)
            for clave in referidas - {usada}:
                acumulados[clave].otra_textura += 1

    sin_leer = tuple(no_leidas)
    texturas = tuple(_veredicto_de_la_textura(salidas[clave], acumulados[clave], sin_leer) for clave in sorted(salidas))
    return InformeDeElegibilidad(
        perfil=perfil,
        mod=resuelto.mod,
        n_habilitados=len(resuelto.enabled),
        n_mallas_leidas=leidas,
        mallas_no_leidas=sin_leer,
        texturas=texturas,
        alcance=ALCANCE_DE_LA_ELEGIBILIDAD,
    )
