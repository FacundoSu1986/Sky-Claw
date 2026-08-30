"""¿La salida que TexGen acaba de generar es visible donde DynDOLOD va a leer?

Sky-Claw corre **standalone**: no hereda la USVFS de Mod Organizer 2 y lanza
DynDOLOD con ``create_subprocess_exec`` contra el ``-d:<Data>`` FÍSICO
(``docs/operations/deployment_standalone_usvfs.md``). Empaquetar la salida de
TexGen como un mod bajo ``<mo2>/mods`` es **entrega**, no despliegue: ese árbol
es invisible para un proceso externo salvo que el operador lo haya materializado
en el ``Data`` real.

Sin este gate la etapa 9 tenía un falso verde entero: DynDOLOD corría 30+ min
generando LODs contra texturas que nunca vio, salía con código 0 y el pipeline
reportaba éxito (review de #493, hallazgo C).

**Lo que este módulo NO hace:** materializar, copiar, enlazar, editar el modlist
o lanzar ``ModOrganizer.exe``. Sólo mide y responde; la materialización es
responsabilidad del operador y la decisión de no automatizarla es de deployment,
no de este validador.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import stat

from sky_claw.app.security.links import iter_archivos_propios, link_kind_and_identity_or_raise

logger = logging.getLogger("SkyClaw.TexGenVisibility")

#: Tamaño de chunk del digest. Igual que el del post-check del runner: los .dds
#: de LOD llegan a decenas de MB y el árbol entero a varios GB, así que nunca se
#: sostiene un archivo completo en memoria.
_CHUNK = 1024 * 1024


class TexGenVisibilityError(Exception):
    """La salida recién generada de TexGen no es visible en el ``Data`` de DynDOLOD."""


def _digest(ruta: pathlib.Path) -> str:
    acumulador = hashlib.sha256()
    with ruta.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            acumulador.update(chunk)
    return acumulador.hexdigest()


def _mismo_archivo_fisico(origen: object, espejo: object) -> bool:
    """¿Las dos identidades son el MISMO archivo en disco (hardlink/junction)?

    Es un fast-path, no el criterio: cuando el operador materializa con enlaces
    duros —lo que MO2 sabe hacer— ``(st_dev, st_ino)`` coinciden y no hay nada que
    comparar byte a byte, porque no hay dos copias. Sólo vale si el par es
    inequívoco: un ``st_ino`` en cero (algunos filesystems de red, y Windows sin
    índice de archivo) no identifica nada y cae al digest.
    """
    dev_o, ino_o = getattr(origen, "st_dev", 0), getattr(origen, "st_ino", 0)
    dev_e, ino_e = getattr(espejo, "st_dev", 0), getattr(espejo, "st_ino", 0)
    return bool(ino_o) and bool(ino_e) and (dev_o, ino_o) == (dev_e, ino_e)


def verificar_visibilidad_de_texgen(
    *,
    staging: pathlib.Path,
    data_dir: pathlib.Path,
) -> None:
    """Falla cerrado si algún archivo del staging no es visible, idéntico, en ``Data``.

    El destino se deriva del NOMBRE del staging (``<raíz>/textures`` →
    ``<Data>/textures``): ése es el contrato Data-relative de TexGen, y derivarlo
    evita una segunda constante que pueda quedar descalzada de
    ``DynDOLODRunner.TEXGEN_OUTPUT_NAME``.

    **Es completo sobre el árbol, no muestreado.** Un muestreo responde "algunos
    coinciden", que es una afirmación distinta de la que el gate necesita hacer, y
    la que falta es justo la del archivo que no se miró. Lo que sí se acota es el
    trabajo por archivo: primero ``lstat`` (existencia, tipo y tamaño, sin leer
    bytes), después identidad física, y sólo si no coinciden se hashean los dos.

    **"Existe" no alcanza, y "mismo tamaño" tampoco.** El despliegue rancio de una
    corrida anterior tiene el nombre correcto y con frecuencia el tamaño correcto:
    es el mismo error del gate de frescura —confundir "hay algo" con "es esto"—
    una capa más arriba.

    La dirección de la afirmación es ``generado ⊆ visible``: se exige que TODO lo
    que esta corrida generó esté en ``Data`` con los mismos bytes, y NO que
    ``Data`` no tenga nada más. ``Data`` es un namespace compartido por diseño —
    ahí conviven las texturas de todos los mods del perfil— así que un archivo de
    más no es evidencia de nada.

    Args:
        staging: el árbol que TexGen acaba de generar (``<raíz>/textures``), ya
            probado como propiedad de esta corrida por el move-aside del servicio.
        data_dir: el ``Data`` físico que DynDOLOD recibe en ``-d:``.

    Raises:
        TexGenVisibilityError: si falta un archivo, si el que hay no es un archivo
            propio, si difiere en tamaño o en contenido, o si el árbol no se pudo
            recorrer. Un staging sin archivos también falla: no hay salida cuya
            visibilidad afirmar.
    """
    destino = data_dir / staging.name
    faltantes: list[str] = []
    divergentes: list[str] = []
    total = 0

    try:
        for archivo, identidad_origen in iter_archivos_propios(staging):
            total += 1
            rel = archivo.relative_to(staging)
            espejo = destino / rel
            tipo, identidad_espejo = link_kind_and_identity_or_raise(espejo)
            if identidad_espejo is None or tipo is not None or not stat.S_ISREG(identidad_espejo.st_mode):
                # Un enlace cuenta como ausente a propósito: la pregunta es si
                # DynDOLOD abre ESTE contenido, y un enlace puede apuntar a
                # cualquier otro lado. Verificarlo sería seguir el enlace, que es
                # justo lo que el recorrido link-safe del origen evita.
                faltantes.append(str(rel))
                continue
            if identidad_espejo.st_size != identidad_origen.st_size:
                divergentes.append(str(rel))
                continue
            if _mismo_archivo_fisico(identidad_origen, identidad_espejo):
                continue
            if _digest(archivo) != _digest(espejo):
                divergentes.append(str(rel))
    except OSError as e:
        # Fail-closed: no poder mirar no es haber verificado. Mismo criterio que
        # las sondas del post-check del runner.
        raise TexGenVisibilityError(
            f"No se pudo verificar si la salida de TexGen en '{staging}' es visible bajo "
            f"'{destino}': {e}. No se lanza DynDOLOD sobre una visibilidad indeterminada."
        ) from e

    if total == 0:
        raise TexGenVisibilityError(
            f"El staging de TexGen '{staging}' no tiene archivos propios: no hay salida cuya "
            "visibilidad se pueda afirmar."
        )

    if faltantes or divergentes:
        muestra_f = ", ".join(faltantes[:5])
        muestra_d = ", ".join(divergentes[:5])
        detalle = []
        if faltantes:
            detalle.append(f"{len(faltantes)} sin aparecer (p. ej.: {muestra_f})")
        if divergentes:
            detalle.append(f"{len(divergentes)} con contenido distinto (p. ej.: {muestra_d})")
        logger.error(
            "Visibilidad de TexGen: %d/%d archivo(s) no verificables bajo %s",
            len(faltantes) + len(divergentes),
            total,
            destino,
            extra={"operation_type": "dyndolod_texgen_no_visible_en_data"},
        )
        raise TexGenVisibilityError(
            f"TexGen se generó y se empaquetó, pero su contenido no es visible en el Data físico "
            f"configurado para DynDOLOD ('{destino}'): {'; '.join(detalle)} de {total} archivo(s). "
            "Sky-Claw corre standalone y no hereda la USVFS de MO2, así que un mod bajo "
            "<mo2>/mods no le llega a DynDOLOD: materializá el árbol de mods del perfil activo "
            "en el Data del juego antes de reintentar (docs/operations/deployment_standalone_usvfs.md)."
        )

    logger.info("Visibilidad de TexGen confirmada: %d archivo(s) idénticos bajo %s", total, destino)


__all__ = ["TexGenVisibilityError", "verificar_visibilidad_de_texgen"]
