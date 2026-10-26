"""¿Qué gana, en el perfil de MO2, la entrada y la salida de un mod? Una sola respuesta, de SOLO lectura (NP-A0g, antes en ``cli.py``).

El generador nativo mide ``textures/.../x_n.dds`` y publica ``x_p.dds``. Medir una normal que otro mod tapa produce un mapa para una
textura que el juego no va a ver, y publicar una salida que un mod de control tapa fue lo que enmascaró el producto en el rig real
(#792 H12). Este módulo compara lo que el generador mediría y publicaría con lo que el overlay del perfil entrega de verdad, y lo
dice: el resolutor de ganadores (``mo2/ganadores.py``) contesta quién provee cada ruta; acá se decide qué de eso es un problema.

Lo comparten TODAS las superficies que lo muestran (la línea de comandos ``perfil`` y ``generar``, el agente LLM y el dispatcher de la
GUI): ninguna resuelve el perfil ni el veredicto por su cuenta, porque cada una contestaría distinto. Por eso no importa ``cli.py``
(arrastra el lote, el exportador y la CLI) y el censo de quién llama al resolutor está congelado
(``tests/test_native_parallax_diagnostico_perfil.py``).

**Lo que NO hace:** no escribe, no toca el lote ni el exportador, no lee el contenido de las texturas más que para identificarlas
(lo hace ``provider.py``) y no prueba lo que se ve en el juego. Es una vista ESTRUCTURAL del ``modlist.txt``: no prueba que USVFS mapee
lo resuelto ni considera el game data físico. Falla cerrado (``PerfilError``) ante cualquier cosa que no permita afirmar quién gana.

El orden del ``modlist.txt`` es el de MO2: la primera línea habilitada es la de MAYOR prioridad; ``read_enabled_mods`` lo invierte a
prioridad creciente (el último gana) y ``overwrite`` gana a todo mod.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Sequence
from typing import Final

from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.ganadores import CAPA_OVERWRITE, GanadoresDelPerfil, GanadoresError, indexar_ganadores
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from sky_claw.local.native_parallax.provider import (
    MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO,
    DemasiadosArchivosError,
    NormalDescubierta,
    RaizInvalidaError,
    descubrir_normales,
)

__all__ = [
    "MAXIMO_DE_ARCHIVOS_DE_ENTRADA",
    "SUFIJO_DE_ENTRADA",
    "TIPOS_QUE_IMPIDEN_MEDIR",
    "Hallazgo",
    "InformeDelPerfil",
    "PerfilError",
    "PerfilResuelto",
    "cargar_perfil",
    "descubrir_normales_del_mod",
    "diagnosticar_perfil",
    "hallazgos_del_perfil",
    "linea_de_hallazgo",
    "texto_de_omision",
]

#: Tope de archivos de entrada de la herramienta (más bajo que el del proveedor: es una herramienta experimental para un rig).
MAXIMO_DE_ARCHIVOS_DE_ENTRADA: Final = 10_000
SUFIJO_DE_ENTRADA: Final = "_n.dds"

#: Hallazgos de la ENTRADA: la textura no se mide (otro mod la tapa, o el overlay no la entrega).
TIPOS_QUE_IMPIDEN_MEDIR: Final = frozenset({"ENTRADA TAPADA", "ENTRADA FUERA DEL OVERLAY"})


class PerfilError(Exception):
    """El perfil no permite afirmar quién gana (modlist ilegible, mod ausente o ambiguo, enumeración incompleta…)."""


@dataclasses.dataclass(frozen=True)
class Hallazgo:
    """Una ruta que el overlay del perfil no entrega como el generador supone."""

    tipo: str
    ruta: str
    detalle: str


@dataclasses.dataclass(frozen=True)
class InformeDelPerfil:
    """El resultado de ``diagnosticar_perfil``: inmutable, para que cada superficie lo serialice o lo recorte sin alterarlo."""

    perfil: str
    mod: str
    n_habilitados: int
    n_texturas: int
    hallazgos: tuple[Hallazgo, ...]


@dataclasses.dataclass(frozen=True)
class PerfilResuelto:
    """El perfil de MO2 ya leído: los mods habilitados (prioridad creciente), el mod elegido y quién gana cada ruta."""

    enabled: tuple[str, ...]
    mod: str
    carpeta_del_mod: pathlib.Path
    indice: GanadoresDelPerfil


def texto_de_omision(ruta: str, detalle: str) -> str:
    return f"{ruta}: {detalle}"


def linea_de_hallazgo(h: Hallazgo) -> str:
    return f"  [{h.tipo}] {h.ruta}: {h.detalle}"


def descubrir_normales_del_mod(
    carpeta: pathlib.Path,
    *,
    exigir_completa: bool = False,
    maximo_de_archivos: int = MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
) -> tuple[list[NormalDescubierta], list[str]]:
    """Los ``*_n.dds`` bajo ``<carpeta>/textures`` y los motivos por los que se omitió algo. Solo lee.

    Adaptador delgado de ``provider.descubrir_normales``: traduce sus errores a ``PerfilError`` y sus omisiones a una línea de texto
    cada una.

    ``exigir_completa``: una carpeta que no se pudo leer o una entrada que no se pudo inspeccionar levanta ``PerfilError`` en vez de
    omitirse. Lo pide quien actúa sobre lo que NO figura (retirar salidas) o quien declara «nada tapado»: sin ver una carpeta no se sabe
    qué hay en ella. Las omisiones deliberadas (enlaces, un ``*_p.dds`` autoral, rutas inadmisibles, duplicados) no son errores de
    lectura y no la hacen fallar (``Descubrimiento.completo``).
    """
    try:
        descubrimiento = descubrir_normales(carpeta, maximo_de_archivos=maximo_de_archivos)
    except (RaizInvalidaError, DemasiadosArchivosError) as exc:
        raise PerfilError(str(exc)) from exc
    if exigir_completa and not descubrimiento.completo:
        sin_ver = "; ".join(
            texto_de_omision(o.ruta_de_origen, o.detalle)
            for o in descubrimiento.omitidas
            if o.motivo in MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO
        )
        raise PerfilError(
            f"la enumeración quedó incompleta (lo que no se pudo ver no se puede dar por ausente): {sin_ver}"
        )
    return list(descubrimiento.normales), [
        texto_de_omision(o.ruta_de_origen, o.detalle) for o in descubrimiento.omitidas
    ]


def _prioridades(enabled: Sequence[str]) -> dict[str, int]:
    """Prioridad creciente por nombre (sin mayúsculas, como el resto del repo); ``overwrite`` gana a todo mod."""
    prioridades = {nombre.lower(): indice for indice, nombre in enumerate(enabled)}
    prioridades[CAPA_OVERWRITE] = len(enabled)
    return prioridades


def hallazgos_del_perfil(
    indice: GanadoresDelPerfil, enabled: Sequence[str], mod: str, normales: Sequence[NormalDescubierta]
) -> list[Hallazgo]:
    """Compara, por cada normal, lo que el generador mediría y publicaría con lo que el overlay entrega de verdad.

    - Entrada: la ruta ``*_n.dds`` debe resolverse a ``mod``; si la gana otra capa (o ninguna: está oculta), medir ese archivo
      produciría un mapa para una textura que el juego no va a ver.
    - Salida: la ruta ``*_p.dds`` debe resolverse al mod de salida (o a nadie, si todavía no se publicó). Una capa de MAYOR
      prioridad que el mod de salida la tapa (#792 H12); una de menor prioridad no, porque la salida la reemplazaría.
    """
    prioridades = _prioridades(enabled)
    salida = NOMBRE_DEL_MOD_DE_SALIDA.lower()
    hallazgos: list[Hallazgo] = []
    if salida not in prioridades:
        hallazgos.append(
            Hallazgo(
                "SALIDA NO HABILITADA",
                NOMBRE_DEL_MOD_DE_SALIDA,
                "el mod de salida no está habilitado en el perfil: lo que se publique ahí no se cargaría",
            )
        )
    for normal in normales:
        ganador_n = indice.ganador(normal.ruta_de_origen)
        if ganador_n is None:
            hallazgos.append(
                Hallazgo(
                    "ENTRADA FUERA DEL OVERLAY",
                    normal.ruta_de_origen,
                    "ninguna capa la entrega (¿oculta con .mohidden?): el juego no la ve",
                )
            )
        elif ganador_n.capa.lower() != mod.lower():
            hallazgos.append(
                Hallazgo("ENTRADA TAPADA", normal.ruta_de_origen, f"la gana {ganador_n.capa!r}, no {mod!r}")
            )
        ganador_p = indice.ganador(normal.ruta_de_salida)
        if ganador_p is None or ganador_p.capa.lower() == salida or salida not in prioridades:
            continue
        if prioridades[ganador_p.capa.lower()] > prioridades[salida]:
            hallazgos.append(
                Hallazgo(
                    "SALIDA TAPADA",
                    normal.ruta_de_salida,
                    f"la gana {ganador_p.capa!r}, que está por encima de {NOMBRE_DEL_MOD_DE_SALIDA!r}",
                )
            )
    return hallazgos


def cargar_perfil(*, mods_dir: pathlib.Path, data_root: pathlib.Path, perfil: str, mod: str) -> PerfilResuelto:
    """LA resolución del perfil que comparten todas las superficies: si cada una la hiciera por su cuenta contestarían distinto (el
    hermano). Falla cerrado con ``PerfilError`` ante cualquier cosa que no permita afirmar quién gana.

    ``perfil`` tiene que ser UNA carpeta de ``<data>/profiles``: un nombre que se sale (``..``, un separador, un NUL) leería el
    ``modlist.txt`` de otra carpeta y lo daría por el del perfil. El nombre llega de afuera (argumento de la CLI, parámetro de una
    superficie), así que se valida acá y no en cada consumidor.
    """
    try:
        assert_safe_component(perfil, field="perfil")
    except PathViolationError as exc:
        raise PerfilError(f"nombre de perfil inválido: {exc}") from exc
    try:
        enabled = read_enabled_mods(data_root / "profiles" / perfil / "modlist.txt")
        elegido = next((m for m in enabled if m.lower() == mod.lower()), None)
        if elegido is None:
            raise PerfilError(f"el mod {mod!r} no está habilitado en el perfil {perfil!r}")
        if elegido.lower() == NOMBRE_DEL_MOD_DE_SALIDA.lower():
            raise PerfilError(
                f"{elegido!r} es el mod de salida del generador: indicá el mod de texturas que se quiere revisar"
            )
        indice = indexar_ganadores(mods_dir=mods_dir, data_root=data_root, enabled=enabled)
    except (VfsAttestationError, GanadoresError) as exc:
        raise PerfilError(str(exc)) from exc
    # El nombre sale de ``enabled`` (ya validado como UNA carpeta por el resolutor): no se une lo que escribió el operador.
    return PerfilResuelto(enabled=enabled, mod=elegido, carpeta_del_mod=mods_dir / elegido, indice=indice)


def diagnosticar_perfil(
    *,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    perfil: str,
    mod: str,
    maximo_de_archivos: int = MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
) -> InformeDelPerfil:
    """El diagnóstico completo de un mod: resuelve el perfil, enumera SUS normales y compara con lo que el overlay entrega.

    Síncrono y de solo lectura: quien lo llame desde código async lo corre en un hilo (``asyncio.to_thread``): enumera y hashea las
    normales del mod y recorre el overlay entero. Completa o nada: una textura que no se pudo leer no se puede dar por «no tapada»,
    y revisar cero texturas no es «nada tapado» (es el hermano de ``generar``, que lo rechaza).
    """
    resuelto = cargar_perfil(mods_dir=mods_dir, data_root=data_root, perfil=perfil, mod=mod)
    normales, _omitidas = descubrir_normales_del_mod(
        resuelto.carpeta_del_mod, exigir_completa=True, maximo_de_archivos=maximo_de_archivos
    )
    if not normales:
        raise PerfilError(
            f"no encontré ningún *{SUFIJO_DE_ENTRADA} procesable bajo {resuelto.carpeta_del_mod}/textures"
        )
    hallazgos = hallazgos_del_perfil(resuelto.indice, resuelto.enabled, resuelto.mod, normales)
    return InformeDelPerfil(
        perfil=perfil,
        mod=resuelto.mod,
        n_habilitados=len(resuelto.enabled),
        n_texturas=len(normales),
        hallazgos=tuple(hallazgos),
    )
