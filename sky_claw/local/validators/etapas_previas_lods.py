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
* etapa 8 — al menos un ``*.cgid`` bajo ``Grass/`` en el ``overwrite`` de MO2 o en
  un mod habilitado del ``modlist.txt``.

**No** prueba que se hayan regenerado DESPUÉS del último cambio de load order:
eso exige un registro de corridas por etapa que el repo no tiene. Un artefacto
viejo pasa el guard; un pipeline que nunca corrió esas etapas, no.

**Fail-closed.** Sin perfil MO2 (standalone), con un ``plugins.txt`` ilegible o
un ``modlist.txt`` malformado, la etapa se declara faltante: "no lo pude leer"
nunca es "está".
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods

#: Plugin que produce la etapa 6 (SOP §2.6) y el de la etapa 7 (SOP §2.7).
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
    try:
        return carpeta.is_dir() and any(carpeta.glob("*.cgid"))
    except OSError:
        return False


def verificar_etapas_previas_de_lods(
    *,
    plugins_txt: pathlib.Path | None,
    modlist_txt: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    overwrite_dir: pathlib.Path | None,
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
        if PLUGIN_DE_SYNTHESIS.lower() not in habilitados:
            faltantes.append(f"etapa 7 (Synthesis): '{PLUGIN_DE_SYNTHESIS}' no está habilitado en el perfil")

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
