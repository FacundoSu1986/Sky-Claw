"""Diff de activación/contigüidad de la salida administrada de Synthesis, en SOLO LECTURA (PR-2b3).

ADR 0019 §2.3: mientras el perfil de MO2 no sea quiescente (hasta PR-5), ``modlist.txt`` y
``plugins.txt`` se conservan byte a byte. Este módulo solo LEE los dos archivos del perfil y dice qué
habría que cambiar para que la corrida tenga efecto: habilitar ``+Sky-Claw Synthesis Output``, activar
cada salida y dejar los splits de cada grupo (``<Grupo>.esp``, ``<Grupo>_2.esp``…) adyacentes,
ordenados y antes de los plugins de LOD. La activación transaccional es de PR-5.

El ancla ``test_el_modulo_no_contiene_primitivas_de_escritura`` congela que acá no haya ninguna
primitiva de escritura.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sky_claw.local.mo2.ganadores import _coincidencias_de_mod
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods
from sky_claw.local.tools.output_targets import SYNTHESIS_MANAGED_MOD_NAME

#: Plugins de LOD que deben cargar DESPUÉS de las salidas de Synthesis (ADR 0019 §2.3).
_PLUGINS_DE_LOD: frozenset[str] = frozenset({"dyndolod.esm", "dyndolod.esp", "occlusion.esp"})

_SPLIT = re.compile(r"(?P<grupo>.+)_(?P<n>[0-9]+)", re.IGNORECASE)


class DiffDeActivacionError(RuntimeError):
    """El perfil no se pudo leer o es ambiguo: el diff no se puede afirmar (falla cerrado)."""


@dataclass(frozen=True, slots=True)
class DiffDeActivacion:
    """Lo que falta para activar la salida administrada. ``aplicado`` es siempre ``False`` hasta PR-5."""

    estado_del_mod: str  # "habilitado" | "deshabilitado" | "ausente"
    plugins_ausentes: tuple[str, ...]
    plugins_inactivos: tuple[str, ...]
    grupos_no_contiguos: tuple[str, ...]
    despues_de_lod: tuple[str, ...]
    cambios_requeridos: tuple[str, ...]
    aplicado: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod": SYNTHESIS_MANAGED_MOD_NAME,
            "estado_del_mod": self.estado_del_mod,
            "plugins_ausentes": list(self.plugins_ausentes),
            "plugins_inactivos": list(self.plugins_inactivos),
            "grupos_no_contiguos": list(self.grupos_no_contiguos),
            "despues_de_lod": list(self.despues_de_lod),
            "cambios_requeridos": list(self.cambios_requeridos),
            "aplicado": self.aplicado,
        }


def _lineas(archivo: pathlib.Path) -> list[str]:
    """Líneas útiles (sin vacías ni comentarios ``#``)."""
    try:
        texto = archivo.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DiffDeActivacionError(f"no se pudo leer {archivo.name} del perfil: {exc}") from exc
    return [linea.strip() for linea in texto.splitlines() if linea.strip() and not linea.strip().startswith("#")]


def _estado_del_mod(modlist_path: pathlib.Path) -> str:
    """Estado de ``+Sky-Claw Synthesis Output``.

    "Habilitado" sale de ``read_enabled_mods``, la primitiva canónica del perfil: una línea malformada
    o un mod duplicado fallan cerrado ahí, no se ignoran acá. La identidad del mod se resuelve con
    ``_coincidencias_de_mod`` (``lower()``, la de NTFS; nunca ``casefold()``), igual que el resto de
    los consumidores del modlist. Solo se distingue además ``deshabilitado`` (``-Nombre``) de ``ausente``.
    """
    try:
        habilitados = read_enabled_mods(modlist_path)
    except VfsAttestationError as exc:
        raise DiffDeActivacionError(str(exc)) from exc
    if _coincidencias_de_mod(habilitados, SYNTHESIS_MANAGED_MOD_NAME):
        return "habilitado"
    deshabilitados = [linea[1:].strip() for linea in _lineas(modlist_path) if linea.startswith("-")]
    return "deshabilitado" if _coincidencias_de_mod(deshabilitados, SYNTHESIS_MANAGED_MOD_NAME) else "ausente"


def _grupos(salidas: Sequence[str]) -> dict[str, list[tuple[int, str]]]:
    """Agrupa las salidas por grupo, cada una con su número de split (1 para ``<Grupo>.esp``).

    ``<X>_<N>.esp`` es split de ``X`` solo si ``N >= 2`` y ``<X>.esp`` también es salida de la corrida;
    si no, es la salida propia de un grupo cuyo nombre termina en número.
    """
    stems = {pathlib.PurePath(s).stem.lower(): pathlib.PurePath(s).stem for s in salidas}
    grupos: dict[str, list[tuple[int, str]]] = {}
    for nombre in salidas:
        stem = pathlib.PurePath(nombre).stem
        coincidencia = _SPLIT.fullmatch(stem)
        if coincidencia and int(coincidencia["n"]) >= 2 and coincidencia["grupo"].lower() in stems:
            base = stems[coincidencia["grupo"].lower()]
            grupos.setdefault(base, []).append((int(coincidencia["n"]), nombre))
        else:
            grupos.setdefault(stem, []).append((1, nombre))
    return grupos


def calcular_diff_de_activacion(*, perfil: pathlib.Path, salidas: Sequence[str]) -> DiffDeActivacion:
    """Compara el perfil de MO2 con las *salidas* (nombres de plugin) de la corrida, sin tocar nada.

    Raises:
        DiffDeActivacionError: ``modlist.txt`` o ``plugins.txt`` ilegibles, o un ``modlist.txt``
            malformado o ambiguo.
    """
    estado = _estado_del_mod(perfil / "modlist.txt")
    plugins = _lineas(perfil / "plugins.txt")

    orden: dict[str, int] = {}
    activos: set[str] = set()
    for indice, linea in enumerate(plugins):
        activo = linea.startswith("*")
        nombre = linea[1:].strip() if activo else linea
        clave = nombre.lower()
        orden.setdefault(clave, indice)
        if activo:
            activos.add(clave)

    nombres = sorted(dict.fromkeys(pathlib.PurePath(s).name for s in salidas), key=str.lower)
    ausentes = tuple(n for n in nombres if n.lower() not in orden)
    inactivos = tuple(n for n in nombres if n.lower() in orden and n.lower() not in activos)

    no_contiguos: list[str] = []
    for base, miembros in _grupos(nombres).items():
        posiciones = [orden.get(nombre.lower()) for _, nombre in sorted(miembros)]
        indices = [i for i in posiciones if i is not None]
        if len(indices) != len(posiciones):
            continue  # ya informado como ausente; la contigüidad se evalúa con el grupo completo
        if any(b != a + 1 for a, b in zip(indices, indices[1:], strict=False)):
            no_contiguos.append(base)

    primer_lod = min((i for clave, i in orden.items() if clave in _PLUGINS_DE_LOD), default=None)
    despues_de_lod = tuple(
        n for n in nombres if primer_lod is not None and n.lower() in orden and orden[n.lower()] > primer_lod
    )

    cambios: list[str] = []
    if estado != "habilitado":
        cambios.append(f"Habilitar '+{SYNTHESIS_MANAGED_MOD_NAME}' en modlist.txt (hoy: {estado}).")
    cambios.extend(f"Agregar y activar {n} en plugins.txt." for n in ausentes)
    cambios.extend(f"Activar {n} en plugins.txt." for n in inactivos)
    cambios.extend(f"Dejar adyacentes y ordenados los splits de {g} en plugins.txt." for g in sorted(no_contiguos))
    cambios.extend(f"Mover {n} antes de los plugins de LOD (DynDOLOD/Occlusion)." for n in despues_de_lod)

    return DiffDeActivacion(
        estado_del_mod=estado,
        plugins_ausentes=ausentes,
        plugins_inactivos=inactivos,
        grupos_no_contiguos=tuple(sorted(no_contiguos)),
        despues_de_lod=despues_de_lod,
        cambios_requeridos=tuple(cambios),
    )


__all__ = ["DiffDeActivacion", "DiffDeActivacionError", "calcular_diff_de_activacion"]
