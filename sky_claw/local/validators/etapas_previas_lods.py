"""Guard de etapas previas de la etapa 9 (SOP §1 / §5 regla 2) para caminos nuevos de LODs.

**La regla.** ``sky_claw/local/AGENTS.md`` §5.2: *ningún camino nuevo puede
invocar DynDOLOD antes de que Wrye Bash + Synthesis estén completos, y todo
camino nuevo de invocación de LODs DEBE agregar un guard de runtime*. La tabla de
dependencias de §1 exige además la etapa 8 (grass precache). El camino de la GUI
(``GenerateLodsStrategy``) arrastra esa deuda documentada; el camino REMOTO
(#661, tool del agente) es nuevo y nace con el guard.

**Qué prueba y qué no, dicho como propiedad.** Prueba que los ARTEFACTOS de las
etapas 6, 7 y 8 existen y están activos en el perfil MO2 que la etapa 9 va a
leer:

* etapa 6 — ``Bashed Patch, 0.esp`` habilitado en el ``plugins.txt`` del perfil;
* etapa 7 — ``Synthesis.esp`` habilitado en el mismo ``plugins.txt``;
* etapa 8 — al menos un ``*.cgid`` (archivo regular, no vacío, no enlace) bajo
  ``Grass/`` en el ``overwrite`` de MO2 o en un mod habilitado del ``modlist.txt``.

**No** prueba que se hayan regenerado DESPUÉS del último cambio de load order:
eso exige un registro de corridas por etapa que el repo no tiene. Un artefacto
viejo pasa el guard; un pipeline que nunca corrió esas etapas, no.

**Fail-closed.** Sin perfil MO2 (standalone), con un ``plugins.txt`` ilegible o
un ``modlist.txt`` malformado, la etapa se declara faltante: "no lo pude leer"
nunca es "está".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods

logger = logging.getLogger(__name__)

#: Base histórica. El nombre real es el del GRUPO de Synthesis (V2) y puede
#: tener splits ``_2.._N`` (Mutagen `KeyForIndex`), así que el caller pasa la
#: lista efectiva; esta constante queda sólo como default retrocompatible.
PLUGIN_DE_WRYE_BASH = "Bashed Patch, 0.esp"
PLUGIN_DE_SYNTHESIS = "Synthesis.esp"


@dataclass(frozen=True, slots=True)
class EvidenciaDeEtapasPrevias:
    """Etapas cuyo artefacto falta, en lenguaje del operador. Vacío = completas."""

    faltantes: tuple[str, ...]

    def completas(self) -> bool:
        return not self.faltantes


def _plugins_habilitados(plugins_txt: pathlib.Path) -> set[str] | None:
    try:
        lineas = plugins_txt.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError):
        return None
    return {linea[1:].strip().lower() for linea in lineas if linea.startswith("*")}


def _tiene_cgid(carpeta: pathlib.Path) -> bool:
    """Al menos un ``*.cgid`` que sea archivo REGULAR (no enlace, no carpeta) y no vacío.

    El nombre solo no prueba un cache: un directorio, un placeholder de una corrida
    fallida o un enlace colgado también matchean ``*.cgid``. La extensión se compara
    sin mayúsculas (en Windows el filesystem tampoco las distingue).
    """
    try:
        if not carpeta.is_dir():
            return False
        with os.scandir(carpeta) as entradas:
            return any(
                entrada.name.lower().endswith(".cgid")
                and entrada.is_file(follow_symlinks=False)
                and entrada.stat(follow_symlinks=False).st_size > 0
                for entrada in entradas
            )
    except OSError:
        return False


def descubrir_plugins_de_synthesis(
    *,
    settings_path: pathlib.Path | None = None,
    profile_identifier: str | None = None,
    carpetas_de_salida: Sequence[pathlib.Path] = (),
) -> tuple[str, ...]:
    """Descubre los plugins ESP esperados de Synthesis según PipelineSettings.json y splits en disco.

    SOP §2.7: cada grupo habilitado en el perfil activo genera ``<Grupo>.esp``.
    Si además se generaron splits ``_2.._N`` (Mutagen MultiModFileSplitter) en
    alguna de las carpetas de salida (p. ej. overwrite/ o mods/Synthesis Output),
    también deben estar habilitados.
    Si no hay settings o no se pueden resolver, retorna (PLUGIN_DE_SYNTHESIS,).
    """
    group_names: list[str] = []
    ruta_settings = settings_path
    if ruta_settings is None:
        env_settings = os.environ.get("SYNTHESIS_PIPELINE_SETTINGS")
        if env_settings:
            ruta_settings = pathlib.Path(env_settings)
        else:
            default_path = pathlib.Path.cwd() / "PipelineSettings.json"
            if default_path.is_file():
                ruta_settings = default_path

    if ruta_settings is not None and ruta_settings.is_file():
        try:
            data = json.loads(ruta_settings.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                profiles = data.get("Profiles")
                if isinstance(profiles, list):
                    target_profile: dict[str, Any] | None = None
                    if profile_identifier:
                        pid_folded = profile_identifier.casefold()
                        for p in profiles:
                            if isinstance(p, dict):
                                nick = p.get("Nickname")
                                p_id = p.get("ID")
                                if (isinstance(nick, str) and nick.casefold() == pid_folded) or (
                                    isinstance(p_id, str) and p_id.casefold() == pid_folded
                                ):
                                    target_profile = p
                                    break
                    if target_profile is None and profiles:
                        for p in profiles:
                            if isinstance(p, dict) and p.get("Nickname") == "Default":
                                target_profile = p
                                break
                        if target_profile is None and isinstance(profiles[0], dict):
                            target_profile = profiles[0]

                    if target_profile is not None:
                        groups = target_profile.get("Groups")
                        if isinstance(groups, list):
                            for g in groups:
                                if isinstance(g, dict) and g.get("On") is True:
                                    name = g.get("Name")
                                    if isinstance(name, str) and name.strip():
                                        group_names.append(name.strip())
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    if not group_names:
        group_names = [pathlib.Path(PLUGIN_DE_SYNTHESIS).stem]

    plugins: set[str] = {f"{name}.esp" for name in group_names}

    for carpeta in carpetas_de_salida:
        if not carpeta.is_dir():
            continue
        try:
            with os.scandir(carpeta) as entradas:
                for entrada in entradas:
                    if not entrada.is_file(follow_symlinks=False):
                        continue
                    fname = entrada.name
                    if not fname.lower().endswith(".esp"):
                        continue
                    for gname in group_names:
                        patron = re.compile(rf"^{re.escape(gname)}_(?:[2-9]|[1-9][0-9]+)\.esp$", re.IGNORECASE)
                        if patron.fullmatch(fname):
                            plugins.add(fname)
        except OSError:
            continue

    return tuple(sorted(plugins, key=lambda s: s.casefold()))


def verificar_etapas_previas_de_lods(
    *,
    plugins_txt: pathlib.Path | None,
    modlist_txt: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    overwrite_dir: pathlib.Path | None,
    plugins_de_synthesis: Sequence[str] | None = None,
) -> EvidenciaDeEtapasPrevias:
    """Verifica los artefactos de las etapas 6, 7 y 8 en el perfil activo."""
    faltantes: list[str] = []
    habilitados = _plugins_habilitados(plugins_txt) if plugins_txt is not None else None
    if habilitados is None:
        faltantes.append(
            "no se pudo leer el plugins.txt del perfil MO2 activo: sin load order no hay forma de probar "
            "las etapas 6 (Wrye Bash) y 7 (Synthesis)"
        )
    else:
        if PLUGIN_DE_WRYE_BASH.lower() not in habilitados:
            faltantes.append(f"etapa 6 (Wrye Bash): '{PLUGIN_DE_WRYE_BASH}' no está habilitado en el perfil")
        esperados_synthesis = tuple(plugins_de_synthesis or (PLUGIN_DE_SYNTHESIS,))
        ausentes_synthesis = [p for p in esperados_synthesis if p.lower() not in habilitados]
        if ausentes_synthesis:
            if len(ausentes_synthesis) == 1 and plugins_de_synthesis is None:
                faltantes.append(f"etapa 7 (Synthesis): '{PLUGIN_DE_SYNTHESIS}' no está habilitado en el perfil")
            else:
                faltantes.append(
                    f"etapa 7 (Synthesis): no está(n) habilitado(s) en el perfil: {', '.join(ausentes_synthesis)}"
                )

    grass = overwrite_dir is not None and _tiene_cgid(overwrite_dir / "Grass")
    if not grass and modlist_txt is not None and mods_dir is not None:
        try:
            habilitados_mods = read_enabled_mods(modlist_txt)
        except VfsAttestationError:
            habilitados_mods = ()
        grass = any(_tiene_cgid(mods_dir / nombre / "Grass") for nombre in habilitados_mods)
    if not grass:
        faltantes.append(
            "etapa 8 (No Grass In Objects): no hay cache de grass (*.cgid) en overwrite/Grass ni en un mod habilitado"
        )
    return EvidenciaDeEtapasPrevias(faltantes=tuple(faltantes))


async def etapas_previas_faltantes(verificar: Callable[[], EvidenciaDeEtapasPrevias]) -> tuple[str, ...]:
    """Corre ``verificar`` (I/O de disco) fuera del loop y devuelve las etapas que faltan.

    Es la ÚNICA puerta de las superficies que lanzan la etapa 9 sin un humano en la
    PC (tool del agente y dispatcher en modo desatendido): las dos comparten así el
    mismo fail-closed. Un error al verificar es una etapa faltante, nunca "está".
    """
    try:
        evidencia = await asyncio.to_thread(verificar)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- el guard no puede abrirse por un error de lectura
        logger.warning("no se pudieron verificar las etapas previas de la etapa 9", exc_info=True)
        return (f"no se pudieron verificar las etapas previas: {exc}",)
    return tuple(evidencia.faltantes)
