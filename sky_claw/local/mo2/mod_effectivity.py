"""¿Cada archivo del artifact de un mod es EFECTIVO en el overlay del perfil MO2?

``+TexGen Output`` demuestra ``ENABLED``; no demuestra ``EVERY FILE EFFECTIVE``.
Otro mod de mayor prioridad —o ``overwrite``, que en MO2 gana siempre— puede
proveer el mismo Data-relative path con bytes distintos, y entonces DynDOLOD
brokered vería por USVFS un contenido que NO es el del artifact autorizado.
Este módulo resuelve, host-side y completo sobre el árbol, el ganador efectivo
de cada path del artifact y exige que los bytes visibles sean exactamente los
esperados.

Es la mitad ESTRUCTURAL del gate del handoff TexGen → DynDOLOD (PR-586D). La
mitad runtime —que el mapping USVFS realmente se aplique— la aporta un canary
attested del propio mod (``vfs_attestation.build_attestation_challenge_for_source``
+ el probe ``health`` del broker). Un canary NO reemplaza este recorrido: un
archivo representativo no prueba que 1.448 rutas estén libres de conflictos, y
este recorrido no prueba que el mapping se aplique. Las dos evidencias juntas
responden la pregunta del gate: *¿DynDOLOD va a ver exactamente el TexGen
Output autorizado?*

Contrato de prioridad, unificado con ``vfs_attestation`` (misma fuente):
``read_enabled_mods`` normaliza ``modlist.txt`` a orden de prioridad creciente
(menor a mayor); ``overwrite`` está por encima de todo mod; el game data físico es
la capa más baja y un mod siempre lo gana. Los mods de prioridad estrictamente mayor
que el mod auditable pueden eclipsar sus paths; los de prioridad menor no.
Ocultar un archivo (``*.mohidden``) lo quita del overlay — mismo criterio que
``vfs_attestation._iter_mod_files``.

**Lo que este módulo NO hace:** editar el modlist, resolver conflictos,
materializar nada, ni ejecutar probes runtime. Mide y falla cerrado.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import stat
from collections.abc import Sequence

from sky_claw.app.security.links import iter_archivos_propios, link_kind_and_identity_or_raise

logger = logging.getLogger("SkyClaw.ModEffectivity")

#: Chunk de lectura al comparar bytes de un path eclipsado (mismo criterio que
#: ``texgen_visibility``/``artifact_digest``: los .dds de LOD pesan decenas de
#: MB; nunca un archivo entero en memoria).
_CHUNK = 1024 * 1024

#: Cuántos conflictos muestra el error (muestra, no muestreo: el gate decide
#: sobre TODOS; el mensaje sólo acota su tamaño).
_MUESTRA_CONFLICTOS = 5


class ModEffectivityError(Exception):
    """El artifact del mod no es efectivo byte-exacto en el overlay del perfil."""


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _mismo_archivo_fisico(origen: object, espejo: object) -> bool:
    """Fast-path de identidad física (hardlink): mismo criterio que ``texgen_visibility``.

    Un ``st_ino`` en cero no identifica nada y cae a la comparación de bytes.
    """
    dev_o, ino_o = getattr(origen, "st_dev", 0), getattr(origen, "st_ino", 0)
    dev_e, ino_e = getattr(espejo, "st_dev", 0), getattr(espejo, "st_ino", 0)
    return bool(ino_o) and bool(ino_e) and (dev_o, ino_o) == (dev_e, ino_e)


def _mismos_bytes(
    origen: pathlib.Path,
    identidad_origen: os.stat_result,
    espejo: pathlib.Path,
    identidad_espejo: os.stat_result,
) -> bool:
    """¿El archivo que eclipsa contiene EXACTAMENTE los bytes del artifact?"""
    if identidad_espejo.st_size != identidad_origen.st_size:
        return False
    if _mismo_archivo_fisico(identidad_origen, identidad_espejo):
        return True
    return _sha256_file(origen) == _sha256_file(espejo)


def _roots_eclipsantes(
    *,
    mod_name: str,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    enabled: Sequence[str],
) -> list[tuple[str, pathlib.Path]]:
    """(etiqueta, root) en orden de prioridad DESCENDENTE: ``overwrite`` primero.

    Sólo roots con prioridad estrictamente MAYOR que ``mod_name``. Un mod listado
    habilitado pero sin directorio es un perfil corrupto: falla cerrado en vez
    de fingir que no eclipsa nada.
    """
    if enabled.count(mod_name) > 1:
        raise ModEffectivityError(
            f"el modlist repite el mod {mod_name!r}: la prioridad efectiva es ambigua y no se puede afirmar"
        )
    if mod_name not in enabled:
        raise ModEffectivityError(f"el mod {mod_name!r} no está habilitado en el modlist del perfil")
    indice = enabled.index(mod_name)
    roots: list[tuple[str, pathlib.Path]] = []
    overwrite = data_root / "overwrite"
    if overwrite.is_dir():
        roots.append(("overwrite", overwrite))
    for superior in reversed(enabled[indice + 1 :]):
        root_superior = mods_dir / superior
        if not root_superior.is_dir():
            raise ModEffectivityError(f"el mod habilitado {superior!r} no existe en {mods_dir}")
        roots.append((f"mod {superior!r}", root_superior))
    return roots


def _ganador_eclipsante(
    data_relativo: pathlib.PurePosixPath,
    roots: Sequence[tuple[str, pathlib.Path]],
) -> tuple[str, pathlib.Path] | None:
    """Primer root (prioridad más alta) que contiene el path; ``None`` si ninguno.

    Un eclipsado que NO es archivo regular propio (directorio, symlink,
    junction) también "contiene" el path: devuelve ese root y el caller lo
    clasifica como incompatibilidad, que es lo que es — el overlay no va a
    entregar el byte del artifact por ese path.
    """
    relativo = pathlib.Path(*data_relativo.parts)
    for etiqueta, root in roots:
        if (root / relativo).exists():
            return etiqueta, root
    return None


def verificar_artifact_efectivo(
    *,
    artifact_root: pathlib.Path,
    mod_name: str,
    mods_dir: pathlib.Path,
    data_root: pathlib.Path,
    enabled: Sequence[str],
) -> int:
    """Falla cerrado salvo que TODO archivo del artifact gane el overlay byte-exacto.

    Args:
        artifact_root: el subárbol Data-relative del mod (p. ej.
            ``mods/TexGen Output/textures``). Su NOMBRE es el prefijo Data del
            mapping (``textures/...``), igual que en ``texgen_visibility``.
        mod_name: el mod MO2 que porta el artifact (p. ej. ``TexGen Output``).
        mods_dir: ``<raíz MO2>/mods``.
        data_root: raíz de datos MO2 (donde vive ``overwrite``).
        enabled: mods habilitados en orden de prioridad creciente
            (``vfs_attestation.read_enabled_mods``).

    Returns:
        Cantidad de archivos verificados.

    Raises:
        ModEffectivityError: si falta el árbol, si no hay archivos, si un path
            queda eclipsado por un ganador que no porta exactamente los bytes
            esperados, o si el estado del perfil no permite afirmar el overlay.
    """
    tipo, identidad = link_kind_and_identity_or_raise(artifact_root)
    if identidad is None or tipo is not None or not stat.S_ISDIR(identidad.st_mode):
        raise ModEffectivityError(
            f"el artifact '{artifact_root}' no es un directorio propio (falta o es un enlace): "
            "no hay árbol cuya efectividad se pueda afirmar"
        )

    roots = _roots_eclipsantes(mod_name=mod_name, mods_dir=mods_dir, data_root=data_root, enabled=enabled)
    eclipsados: list[str] = []
    incompatibles: list[str] = []
    total = 0

    try:
        for archivo, identidad_origen in iter_archivos_propios(artifact_root):
            total += 1
            rel = archivo.relative_to(artifact_root)
            data_relativo = pathlib.PurePosixPath(artifact_root.name, *rel.parts)
            ganador = _ganador_eclipsante(data_relativo, roots)
            if ganador is None:
                # Sin eclipsador el ganador es el propio mod: MO2 mapea los mods
                # por encima del game data, y los mods de prioridad menor pierden.
                continue
            etiqueta, root = ganador
            espejo = root / pathlib.Path(*data_relativo.parts)
            tipo_espejo, identidad_espejo = link_kind_and_identity_or_raise(espejo)
            if identidad_espejo is None or tipo_espejo is not None or not stat.S_ISREG(identidad_espejo.st_mode):
                # Un enlace/directorio que ocupa el path no entrega el byte del
                # artifact: incompatibilidad, sin preguntar a dónde apunta.
                incompatibles.append(f"{data_relativo.as_posix()} (eclipsado por {etiqueta}, sin archivo regular)")
                continue
            if _mismos_bytes(archivo, identidad_origen, espejo, identidad_espejo):
                # Override aceptado SÓLO por bytes idénticos: DynDOLOD vería
                # exactamente el contenido autorizado aunque venga de otra fuente.
                continue
            eclipsados.append(f"{data_relativo.as_posix()} (eclipsado por {etiqueta} con bytes distintos)")
    except ModEffectivityError:
        raise
    except OSError as exc:
        raise ModEffectivityError(
            f"No se pudo recorrer el artifact '{artifact_root}' para afirmar su efectividad: {exc}. "
            "No se lanza DynDOLOD sobre un overlay indeterminado."
        ) from exc

    if total == 0:
        raise ModEffectivityError(
            f"El artifact '{artifact_root}' no tiene archivos propios: no hay salida cuya efectividad se pueda afirmar."
        )

    if eclipsados or incompatibles:
        detalle = eclipsados + incompatibles
        logger.error(
            "Efectividad del artifact: %d/%d archivo(s) no efectivos en el overlay del perfil",
            len(detalle),
            total,
            extra={"operation_type": "dyndolod_texgen_no_efectivo_en_perfil"},
        )
        raise ModEffectivityError(
            f"El artifact '{mod_name}' no es efectivo en el overlay del perfil: "
            f"{len(detalle)} de {total} archivo(s) no entregan los bytes autorizados "
            f"(p. ej.: {'; '.join(detalle[:_MUESTRA_CONFLICTOS])}). Un mod de mayor prioridad o "
            "'overwrite' reemplaza esos paths: el contenido visible para DynDOLOD no sería el del "
            "artifact autorizado."
        )

    return total


__all__ = ["ModEffectivityError", "verificar_artifact_efectivo"]
