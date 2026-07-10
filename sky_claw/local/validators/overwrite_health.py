"""Sensor de overwrite sucio para el preflight (T-30·3, Oleada 7).

El ``overwrite`` de MO2 es el destino compartido donde cae la salida de las
herramientas (bashed patch, DynDOLOD, Synthesis, Pandora, BodySlide). Correr un
Ritual mutante con residuos previos ahí tiene dos costos:

* el diff del Ritual deja de ser atribuible — la salida nueva se mezcla con lo
  viejo ("corrí dos tools a la vez y me destrocé el overwrite");
* el clon del sandbox (T-27) arranca contaminado.

El sensor es **read-only y best-effort**: escanea el overwrite recursivamente y
reporta lo que hay; nunca borra ni lanza. Suciedad = **amarillo, nunca rojo**:
un Bashed Patch recién generado es un estado legítimo a mitad de flujo (MO2
mismo solo advierte). Un overwrite inexistente está limpio — MO2 lo crea on
demand.

:func:`overwrite_preflight_check` compone el resultado en un
:class:`PreflightCheck` para el semáforo; el cableado al ``PreflightService``
es un parámetro inyectable (mismo patrón que masters/límites).
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

logger = logging.getLogger(__name__)

#: Extensiones de plugin que el juego carga (mismo criterio que plugin_limits).
_PLUGIN_SUFFIXES: frozenset[str] = frozenset({".esp", ".esm", ".esl"})

#: Cuántas rutas se listan en los details antes de resumir el resto — un
#: overwrite con cientos de meshes (BodySlide) no debe inflar el reporte.
_MAX_DETAIL_FILES = 10

_REMEDIATION = (
    "Mové los residuos a un mod (clic derecho sobre Overwrite → 'Create Mod' en MO2) "
    "o limpialos antes del Ritual: así el diff de la próxima herramienta es atribuible."
)


def _is_link(path: pathlib.Path) -> bool:
    """True si *path* es un symlink o junction (reparse point).

    Mira el enlace mismo, no su destino: un link a directorio o uno roto no es
    ``is_file()`` pero sí es un residuo — y el sandbox (T-27) los rechaza, así
    que dejarlos pasar en verde contaminaría el Ritual (review Codex #254).
    Espeja la detección de ``vfs_health._link_kind`` (junctions vía
    ``is_junction`` de Py3.12 o el ``st_reparse_tag`` del lstat en 3.11/Windows).
    """
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        return bool(getattr(path.lstat(), "st_reparse_tag", 0))
    except OSError as exc:
        logger.debug("No se pudo inspeccionar el enlace %s: %s", path, exc)
        return False


@dataclass(frozen=True, slots=True)
class OverwriteScan:
    """Contenido residual del overwrite compartido.

    Attributes:
        files: Rutas relativas al overwrite (POSIX) de todos los archivos.
        plugins: Subconjunto de ``files`` con sufijo de plugin — entran al
            load order con máxima precedencia sin estar gestionados como mod.
    """

    files: tuple[str, ...]
    plugins: tuple[str, ...]


class OverwriteHealthChecker:
    """Escanea el overwrite compartido de MO2 en busca de residuos.

    Args:
        overwrite_dir: ``<mo2>/overwrite``. Inexistente = limpio.
    """

    def __init__(self, *, overwrite_dir: pathlib.Path) -> None:
        self._overwrite_dir = overwrite_dir

    def check(self) -> OverwriteScan:
        """Enumera archivos (recursivo); dirs vacíos no cuentan como suciedad."""
        files: list[str] = []
        plugins: list[str] = []
        try:
            # sorted() materializa el generador acá adentro: un OSError a mitad
            # de la iteración también cae en este except (best-effort real).
            entries = sorted(self._overwrite_dir.rglob("*"))
        except OSError as exc:
            logger.debug("No se pudo escanear el overwrite %s: %s", self._overwrite_dir, exc)
            entries = []
        for entry in entries:
            try:
                # Un symlink/junction cuenta como residuo aunque is_file() sea
                # falso (link a directorio o roto): is_file() sigue el enlace, así
                # que un link-a-archivo ya caía acá; esto suma los link-a-dir/rotos
                # que si no darían un falso verde (review Codex #254).
                if not (_is_link(entry) or entry.is_file()):
                    continue
            except OSError as exc:
                logger.debug("No se pudo inspeccionar %s: %s", entry, exc)
                continue
            relative = entry.relative_to(self._overwrite_dir).as_posix()
            files.append(relative)
            if entry.suffix.lower() in _PLUGIN_SUFFIXES:
                plugins.append(relative)
        if files:
            logger.warning(
                "Overwrite sucio: %d archivo(s), %d plugin(s) en %s",
                len(files),
                len(plugins),
                self._overwrite_dir,
            )
        return OverwriteScan(files=tuple(files), plugins=tuple(plugins))


def overwrite_preflight_check(scan: OverwriteScan) -> PreflightCheck:
    """Compone el escaneo en un :class:`PreflightCheck` para el semáforo.

    Amarillo si hay residuos (advierte, nunca bloquea); verde si está limpio.
    """
    if not scan.files:
        return PreflightCheck(name="overwrite", status=PreflightStatus.GREEN, summary="Overwrite limpio.")
    # Priorizar los plugins: un plugin residual entra al load order con máxima
    # precedencia (alto impacto), así que debe verse en los details aunque el
    # overwrite tenga cientos de archivos genéricos (BodySlide) y el plugin
    # ordene después del cap (review Codex #254).
    plugins_set = set(scan.plugins)
    ordered = list(scan.plugins) + [f for f in scan.files if f not in plugins_set]
    listed = ordered[:_MAX_DETAIL_FILES]
    remaining = len(ordered) - len(listed)
    details = list(listed)
    if remaining:
        details.append(f"… y {remaining} más")
    details.append(_REMEDIATION)
    return PreflightCheck(
        name="overwrite",
        status=PreflightStatus.YELLOW,
        summary=f"{len(scan.files)} archivo(s) residual(es) en el overwrite ({len(scan.plugins)} plugin(s)).",
        details=tuple(details),
    )
