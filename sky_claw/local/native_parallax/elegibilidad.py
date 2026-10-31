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
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from sky_claw.local.assets.auto_parallax_regla import (
    CandidatoDeRelieve,
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
    TIPO_SALIDA_NO_HABILITADA,
    TIPO_SALIDA_TAPADA,
    TIPOS_QUE_IMPIDEN_MEDIR,
    Hallazgo,
    PerfilError,
    PerfilResuelto,
    cargar_mallas_del_perfil,
    cargar_perfil,
    cargar_plugin_del_perfil,
    descubrir_normales_del_mod,
    hallazgos_del_perfil,
    linea_de_hallazgo,
)
from sky_claw.local.native_parallax.plugin_relieve import AjusteDelIni, EstadoDelPlugin, leer_estado_del_plugin

__all__ = [
    "ALCANCE_DE_LA_ELEGIBILIDAD",
    "MAXIMO_DE_ARCHIVOS_EN_MESHES",
    "MAXIMO_DE_MALLAS_POR_TEXTURA",
    "MAXIMO_DE_TEXTURAS_EN_EL_RESULTADO",
    "PLAZO_DE_LAS_SUPERFICIES",
    "EfectoPosible",
    "ElegibilidadDeTextura",
    "InformeDeElegibilidad",
    "elegibilidad_como_resultado",
    "evaluar_elegibilidad",
]

#: Tope de archivos visitados bajo ``meshes/`` entre todas las capas: un perfil real pesa decenas de miles; pasarlo no devuelve un
#: índice a medias.
MAXIMO_DE_ARCHIVOS_EN_MESHES: Final = 500_000
#: Cuántas mallas se nombran por textura (el resto se cuenta pero no se lista).
MAXIMO_DE_MALLAS_POR_TEXTURA: Final = 20
#: Tope del resultado de tool: lo lee el contexto de un modelo o un panel de la GUI. El total real viaja siempre en ``n_texturas``.
MAXIMO_DE_TEXTURAS_EN_EL_RESULTADO: Final = 50
#: Cuántas mallas no leídas se nombran en el resultado de tool (el total viaja en ``n_mallas_no_leidas``).
_MAXIMO_DE_MALLAS_NO_LEIDAS_EN_EL_RESULTADO: Final = 5
#: Plazo de la lectura de mallas para las superficies de tool (segundos). La lectura es lo único que escala con el perfil y corre en un
#: hilo que quien cancela su espera no puede detener: acotarla evita un hilo huérfano leyendo decenas de miles de NIFs. Tiene que vencer
#: ANTES que el presupuesto por tool del router del agente (``DEFAULT_TOOL_ROUND_TIMEOUT``, 120 s): si no, el router cancela su espera con el
#: hilo todavía leyendo y el agente nunca ve ni el resultado ni el fallo estructurado del plazo (lo ancla
#: ``tests/test_evaluar_parallax_elegibilidad_superficies.py``). Un perfil que necesite más usa la línea de comandos, que no tiene plazo.
PLAZO_DE_LAS_SUPERFICIES: Final = 100.0

#: Tope de archivos visitados bajo ``SKSE/Plugins`` entre todas las capas (el INI y la DLL del plugin): una carpeta de plugins pesa decenas.
_MAXIMO_DE_ARCHIVOS_EN_PLUGINS: Final = 5_000

#: Lo que esta lectura prueba y lo que no: viaja en cada informe para que ninguna superficie lo presente como una prueba in-game.
ALCANCE_DE_LA_ELEGIBILIDAD: Final = (
    "Lectura estructural de las mallas sueltas del perfil. El plugin solo actúa sobre objetos base Static, Movable Static y Container "
    "(eso no se ve en la malla) y solo enciende relieve si bAutoEnableParallax está activo en su INI (por defecto no): ese INI se lee del "
    "overlay del perfil y, si está apagado, ninguna salida es SI; si no se puede ver, se avisa y el veredicto no cambia. "
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
    plugin: EstadoDelPlugin


@dataclass(slots=True)
class _Acumulado:
    mallas_si: set[str] = field(default_factory=set)
    hay_pendientes: bool = False
    con_ruta_implicita: bool = (
        False  #: Alguna forma nombra una ruta sin ``textures\\`` que el motor podría resolver a esta salida.
    )
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


def _salidas_que_nombra_sin_carpeta(candidatos: tuple[CandidatoDeRelieve, ...], salidas: dict[str, str]) -> set[str]:
    """Salidas que un candidato SIN ``textures/`` podría estar nombrando si el motor antepusiera esa carpeta (no se confirma, se anota)."""
    posibles: set[str] = set()
    for candidato in candidatos:
        clave = _clave(candidato.ruta)
        if not clave.startswith("textures/"):
            posible = f"textures/{clave}"
            if posible in salidas:
                posibles.add(posible)
    return posibles


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
    if acum.con_ruta_implicita:
        return con(
            EfectoPosible.NO_VERIFICADO,
            "una forma nombra una ruta sin la carpeta textures\\ que el motor podría resolver a esta salida, y no se puede saber si "
            f"es así{aviso_de_lectura}",
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


def _con_el_ini_apagado(textura: ElegibilidadDeTextura, plugin: EstadoDelPlugin) -> ElegibilidadDeTextura:
    """Un ``SI`` con el INI apagado es ``NO``: el plugin no enciende relieve en ninguna forma. Lo demás no cambia (``NO`` ya lo es y
    ``NO_VERIFICADO`` no pasa a ``NO`` por esto: sigue sin saberse si alguna malla la usaría)."""
    if textura.efecto is not EfectoPosible.SI:
        return textura
    return ElegibilidadDeTextura(
        ruta_de_salida=textura.ruta_de_salida,
        efecto=EfectoPosible.NO,
        razon=(
            f"el INI del plugin (mod {plugin.capa_del_ini!r}) tiene bAutoEnableParallax apagado: el plugin no enciende relieve en ninguna "
            f"forma, aunque {_mallas_en_texto(textura.n_mallas)} sea elegible"
        ),
        mallas=(),
        n_mallas=0,
    )


def _estado_del_plugin(*, mods_dir: pathlib.Path, data_root: pathlib.Path, resuelto: PerfilResuelto) -> EstadoDelPlugin:
    """El INI y la DLL del plugin según el overlay. Si ``SKSE/Plugins`` no se puede resolver el ajuste queda sin determinar: no es motivo para
    perder la evaluación entera, pero tampoco se da por activo ni por apagado."""
    try:
        indice = cargar_plugin_del_perfil(
            mods_dir=mods_dir, data_root=data_root, resuelto=resuelto, maximo_de_archivos=_MAXIMO_DE_ARCHIVOS_EN_PLUGINS
        )
    except PerfilError as exc:
        return EstadoDelPlugin(AjusteDelIni.NO_DETERMINADO, None, False, str(exc))
    return leer_estado_del_plugin(indice)


def evaluar_elegibilidad(
    *,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    perfil: str,
    mod: str,
    maximo_de_mallas: int = MAXIMO_DE_ARCHIVOS_EN_MESHES,
    plazo: float | None = None,
) -> InformeDeElegibilidad:
    """Para cada ``*_n.dds`` de *mod*, ¿alguna malla del perfil usaría el ``*_p.dds`` que el generador publicaría?

    Síncrono y de solo lectura: lee todas las mallas ganadoras del overlay, así que quien lo llame desde código async lo corre en un hilo.

    ``plazo`` (segundos, ``None`` = sin límite) acota la LECTURA de mallas, que es lo único que escala con el perfil: se comprueba ANTES
    de empezar cada malla y, pasado el plazo, se falla cerrado, sin un veredicto parcial (las primeras mallas leídas no bastan para afirmar
    ``NO`` ni ``SI`` de nada). Lo que NO promete: una lectura ya empezada no se interrumpe (la acota el tope de tamaño del lector, no el
    tiempo: un disco o una unidad de red colgados la bloquean, y interrumpirla pediría aislarla en otro proceso), así que el hilo termina,
    como mucho, una lectura después del plazo; y si la última malla se lee pasado el plazo, el resultado es completo y se devuelve (no se
    descarta una evaluación entera y correcta). La enumeración del overlay, previa a la lectura, no se interrumpe: ya está acotada por
    ``maximo_de_mallas``.

    Raises:
        PerfilError: el perfil no permite afirmar quién gana, el mod no tiene normales procesables, hay más archivos que el tope o se
            agotó el plazo.
    """
    inicio = time.monotonic()
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

    # Lo que ``generar`` con perfil NO mide (otra capa gana su entrada o el overlay no la entrega) no se publica: su salida no puede decidir
    # la búsqueda del plugin ni contar como «presente». Es el mismo veredicto que usa ``generar`` (``hallazgos_del_perfil``).
    hallazgos = hallazgos_del_perfil(resuelto.indice, resuelto.enabled, resuelto.mod, normales)
    sin_medir = {h.ruta: h for h in hallazgos if h.tipo in TIPOS_QUE_IMPIDEN_MEDIR}
    medidas = [n for n in normales if n.ruta_de_origen not in sin_medir]
    if not medidas:
        detalle = "\n".join(linea_de_hallazgo(h) for h in hallazgos)
        raise PerfilError(
            f"todas las texturas de {resuelto.mod!r} están tapadas o fuera del overlay: no hay nada que evaluar\n{detalle}"
        )
    # La otra mitad del mismo veredicto: se publicaría, pero el juego no cargaría lo publicado (una capa de mayor prioridad gana la ruta de
    # salida, o el mod de salida no está habilitado). Tampoco cuenta como «presente» ni decide la búsqueda del plugin: la malla elegible
    # usaría el archivo que SÍ gana, no el nuestro.
    salida_no_habilitada = next((h for h in hallazgos if h.tipo == TIPO_SALIDA_NO_HABILITADA), None)
    salidas_tapadas = {h.ruta: h for h in hallazgos if h.tipo == TIPO_SALIDA_TAPADA}
    sin_carga: dict[str, Hallazgo] = {}
    for n in medidas:
        impide = salida_no_habilitada if salida_no_habilitada is not None else salidas_tapadas.get(n.ruta_de_salida)
        if impide is not None:
            sin_carga[n.ruta_de_salida] = impide
    publicadas = [n for n in medidas if n.ruta_de_salida not in sin_carga]
    no_publicadas = tuple(
        ElegibilidadDeTextura(
            ruta_de_salida=n.ruta_de_salida,
            efecto=EfectoPosible.NO,
            razon=(
                f"el generador no la publicaría: {sin_medir[n.ruta_de_origen].tipo} "
                f"({sin_medir[n.ruta_de_origen].detalle}); ver el subcomando perfil"
                if n.ruta_de_origen in sin_medir
                else f"el generador la publicaría, pero el juego no la cargaría: {sin_carga[n.ruta_de_salida].tipo} "
                f"({sin_carga[n.ruta_de_salida].detalle}); ver el subcomando perfil"
            ),
            mallas=(),
            n_mallas=0,
        )
        for n in normales
        if n.ruta_de_origen in sin_medir or n.ruta_de_salida in sin_carga
    )
    salidas = {_clave(n.ruta_de_salida): n.ruta_de_salida for n in publicadas}
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
    # Sin salidas evaluables no hay nada que una malla decida: no se leen para nada.
    mallas_a_leer = indice_de_mallas.con_sufijo(".nif") if salidas else ()
    for ganador in mallas_a_leer:
        if plazo is not None and time.monotonic() - inicio >= plazo:
            raise PerfilError(
                f"la lectura de mallas superó el plazo de {plazo:g} s (con {leidas} leídas): no se afirma nada de un perfil que no se "
                "leyó entero. Para un perfil así usá la línea de comandos (`cli elegibilidad`), que no tiene plazo."
            )
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
            for implicita in _salidas_que_nombra_sin_carpeta(candidatos.candidatos, salidas):
                acumulados[implicita].con_ruta_implicita = True
            referidas = {_clave(c.ruta) for c in candidatos.candidatos if c.ruta} & salidas.keys()
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
    plugin = _estado_del_plugin(mods_dir=mods_dir, data_root=data_root, resuelto=resuelto)
    evaluadas = (_veredicto_de_la_textura(salidas[clave], acumulados[clave], sin_leer) for clave in salidas)
    if plugin.apaga_el_relieve:
        evaluadas = (_con_el_ini_apagado(t, plugin) for t in evaluadas)
    texturas = tuple(sorted((*evaluadas, *no_publicadas), key=lambda t: _clave(t.ruta_de_salida)))
    return InformeDeElegibilidad(
        perfil=perfil,
        mod=resuelto.mod,
        n_habilitados=len(resuelto.enabled),
        n_mallas_leidas=leidas,
        mallas_no_leidas=sin_leer,
        texturas=texturas,
        alcance=ALCANCE_DE_LA_ELEGIBILIDAD,
        plugin=plugin,
    )


#: Orden del resultado acotado: lo accionable primero. Con el recorte alfabético los ``SI`` podrían quedar fuera del resultado.
_ORDEN_DE_EFECTOS: Final[dict[EfectoPosible, int]] = {
    EfectoPosible.SI: 0,
    EfectoPosible.NO_VERIFICADO: 1,
    EfectoPosible.NO: 2,
}


def elegibilidad_como_resultado(
    *,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    perfil: str,
    mod: str,
    maximo_de_texturas: int = MAXIMO_DE_TEXTURAS_EN_EL_RESULTADO,
    plazo: float | None = PLAZO_DE_LAS_SUPERFICIES,
) -> dict[str, Any]:
    """La elegibilidad como resultado de tool (``success`` + ``message`` canónico + campos estructurados), ACOTADA.

    Es lo que devuelven la GUI (dispatcher) y el agente LLM: las dos superficies llaman a ESTA función con las mismas raíces y el mismo
    perfil, así que no pueden contestar distinto (AGENTS.md: dos superficies, un recurso). Síncrona y bloqueante (lee las mallas del
    perfil): quien la llame desde código async la corre en un hilo. El ``plazo`` por defecto es acotado a propósito: una superficie que
    no lo pasa no deja, igual, un hilo sin límite.

    Solo un ``PerfilError`` (incluido el plazo agotado) se vuelve resultado (``success: False`` con el motivo en ``message``): cualquier
    otra excepción es un bug o un fallo inesperado y sube a la capa de errores de la superficie. ``success`` dice que la evaluación
    CORRIÓ, no que haya texturas con efecto: eso va en ``conteo``.

    ``texturas`` va ordenado con lo accionable primero (``SI``, luego ``NO_VERIFICADO``, luego ``NO``) y recortado a
    ``maximo_de_texturas``: ``n_texturas`` y ``truncado`` dicen cuántas había. Los textos (rutas de mallas y de texturas, razones) salen
    del disco y los elige un tercero: quien los muestre a un modelo o a un panel los sanea.
    """
    try:
        informe = evaluar_elegibilidad(mods_dir=mods_dir, data_root=data_root, perfil=perfil, mod=mod, plazo=plazo)
    except PerfilError as exc:
        return {
            "success": False,
            "message": str(exc),
            "perfil": perfil,
            "mod": mod,
            "alcance": ALCANCE_DE_LA_ELEGIBILIDAD,
        }
    ordenadas = sorted(informe.texturas, key=lambda t: (_ORDEN_DE_EFECTOS[t.efecto], _clave(t.ruta_de_salida)))
    visibles = ordenadas[: max(0, maximo_de_texturas)]
    conteo = Counter(t.efecto for t in informe.texturas)
    visibles_no_leidas = informe.mallas_no_leidas[:_MAXIMO_DE_MALLAS_NO_LEIDAS_EN_EL_RESULTADO]
    return {
        "success": True,
        "message": "",
        "perfil": informe.perfil,
        "mod": informe.mod,
        "n_habilitados": informe.n_habilitados,
        "n_mallas_leidas": informe.n_mallas_leidas,
        "n_mallas_no_leidas": len(informe.mallas_no_leidas),
        "mallas_no_leidas": list(visibles_no_leidas),
        "mallas_no_leidas_truncado": len(visibles_no_leidas) < len(informe.mallas_no_leidas),
        "n_texturas": len(informe.texturas),
        "conteo": {efecto.value: conteo.get(efecto, 0) for efecto in EfectoPosible},
        "texturas": [
            {
                "ruta_de_salida": t.ruta_de_salida,
                "efecto": t.efecto.value,
                "razon": t.razon,
                "mallas": list(t.mallas),
                "n_mallas": t.n_mallas,
            }
            for t in visibles
        ],
        "truncado": len(visibles) < len(informe.texturas),
        "plugin": {
            "ini": informe.plugin.ini.value,
            "capa_del_ini": informe.plugin.capa_del_ini,
            "dll_presente": informe.plugin.dll_presente,
            "avisos": list(informe.plugin.avisos()),
        },
        "alcance": ALCANCE_DE_LA_ELEGIBILIDAD,
    }
