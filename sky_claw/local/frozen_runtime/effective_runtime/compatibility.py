"""Gate ``RUNTIME_COMPATIBILITY`` (F6): SKSE cargable contra el build del ejecutable.

El ADR 0012 (§13) deja claro por qué esto **no** es un chequeo cosmético: SKSE no
es un tool que Sky-Claw ejecute desde donde esté, es un runtime que el juego
carga por ruta fija, y sólo carga si el loader **y** el DLL de runtime del build
exacto están en la raíz del juego. Un loader con el DLL de otra versión produce
el peor desenlace posible: MO2 arranca, el juego se cierra, y el usuario se
entera al final.

La autoridad del veredicto es ``scanner.find_skse_installation`` — el mismo
predicado que ya usa el resto del repo—, **no** una segunda implementación del
match. Este módulo sólo lo envuelve para dos cosas que el original colapsa:

1. traducir ``None`` a ``INCOMPATIBLE`` vs ``UNKNOWN`` (no son lo mismo);
2. explicar *por qué*, listando las versiones observadas.

``UNKNOWN != COMPATIBLE``: sin versión legible del ejecutable no hay
``COMPATIBLE`` posible, ni siquiera con un SKSE perfecto en disco, porque el
match de build es justamente lo que no se puede afirmar.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable

from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.discovery.scanner import (
    find_skse_installation,
    skse_dll_game_version,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    CompatibilityResult,
    RuntimeCompatibility,
)
from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)

#: Loader que corresponde a ``skyrimse`` (SE y AE comparten familia ``skse64_*``).
_LOADER_SKYRIMSE = "skse64_loader.exe"
#: ``skse_dll_game_version`` devuelve algo con forma ``major.minor.patch`` sólo
#: cuando el nombre codifica un build del juego; los DLL auxiliares (p. ej.
#: ``skse64_steam_loader.dll``) no, y por eso no cuentan como runtime.
_VERSION_DE_BUILD = re.compile(r"^\d+\.\d+\.\d+$")


def _identidad_por_defecto(raiz: pathlib.Path) -> RuntimeIdentity:
    """Identidad del runtime medida del PE (misma primitiva que usa el vault)."""
    return observe_runtime_identity_from_root(raiz, expected_game_key="skyrimse").runtime_identity


def _dlls_de_runtime(raiz: pathlib.Path) -> list[pathlib.Path]:
    """DLLs ``skse*.dll`` cuyo nombre codifica un build del juego."""
    return [
        dll
        for dll in raiz.glob("skse*.dll")
        if dll.is_file() and _VERSION_DE_BUILD.match(skse_dll_game_version(dll.name))
    ]


def assess_runtime_compatibility(
    root: pathlib.Path,
    *,
    identity: Callable[[pathlib.Path], RuntimeIdentity] | None = None,
) -> CompatibilityResult:
    """Clasifica si SKSE es cargable desde *root* contra su propio ejecutable.

    Args:
        root: raíz del runtime candidato (el Clone, no una Generation).
        identity: observador inyectable; por defecto lee el recurso PE.

    Returns:
        ``COMPATIBLE`` sólo con loader + DLL de runtime del build exacto;
        ``INCOMPATIBLE`` cuando ambos existen pero el build no coincide;
        ``UNKNOWN`` en todo lo demás (sin versión, sin loader, sin DLL).
    """
    raiz = pathlib.Path(root)
    if not raiz.is_dir():
        return CompatibilityResult(
            state=RuntimeCompatibility.UNKNOWN,
            reason=f"la raíz '{raiz}' no existe o no es un directorio",
        )

    observador = identity if identity is not None else _identidad_por_defecto
    try:
        identidad = observador(raiz)
    except RuntimeObservationError as exc:
        return CompatibilityResult(
            state=RuntimeCompatibility.UNKNOWN,
            reason=f"no se pudo leer la identidad del runtime: {exc}",
        )

    version = identidad.game_version
    if not version:
        # Sin versión observada el match de build es inafirmable. Llamar a
        # ``find_skse_installation`` con game_version="" lo degradaría a
        # "loader + algún DLL" y devolvería COMPATIBLE sin haber verificado nada.
        return CompatibilityResult(
            state=RuntimeCompatibility.UNKNOWN,
            reason="el ejecutable no expone versión: la compatibilidad de SKSE no es demostrable",
        )

    loader = find_skse_installation(raiz, edition=SkyrimEdition.AE, game_version=version)
    if loader is not None:
        dlls = _dlls_de_runtime(raiz)
        return CompatibilityResult(
            state=RuntimeCompatibility.COMPATIBLE,
            reason=f"SKSE {skse_dll_game_version(dlls[0].name)} sirve al build {version}",
            game_version=version,
            skse_loader=loader.name,
            skse_dll=dlls[0].name if dlls else None,
        )

    # ``find_skse_installation`` devolvió None: se explica el motivo, no se
    # adivina el veredicto. INCOMPATIBLE exige que loader Y DLL existan — si
    # falta alguno, la causa es ausencia, no desajuste de build.
    ruta_loader = raiz / _LOADER_SKYRIMSE
    if not ruta_loader.is_file():
        return CompatibilityResult(
            state=RuntimeCompatibility.UNKNOWN,
            reason=f"sin '{_LOADER_SKYRIMSE}' en la raíz: SKSE no está instalado ahí",
            game_version=version,
        )

    dlls = _dlls_de_runtime(raiz)
    if not dlls:
        return CompatibilityResult(
            state=RuntimeCompatibility.UNKNOWN,
            reason="sin DLL de runtime de SKSE en la raíz: SKSE no está instalado ahí",
            game_version=version,
            skse_loader=ruta_loader.name,
        )

    versiones = ", ".join(sorted({skse_dll_game_version(dll.name) for dll in dlls}))
    return CompatibilityResult(
        state=RuntimeCompatibility.INCOMPATIBLE,
        reason=(f"SKSE targetea {versiones} y el ejecutable es {version}: el loader no cargará (build distinto)"),
        game_version=version,
        skse_loader=ruta_loader.name,
        skse_dll=dlls[0].name,
    )
