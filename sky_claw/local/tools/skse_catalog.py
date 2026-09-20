"""Catálogo puro: runtime EXACTO de Skyrim → release de SKSE conocido.

Reemplazo conceptual de la clave de ``SKSE_CONFIG`` en
``sky_claw/local/tools_installer.py``: la EDICIÓN (AE/SE/LE) ya no identifica
un build de SKSE — ``1.6.1170``, ``1.7.99`` y ``1.7.104`` son todos "AE" y cada
uno requiere un build distinto. La clave de verdad es el runtime exacto que
reporta el recurso PE de ``SkyrimSE.exe``.

Propiedades del catálogo (ancladas en ``tests/test_skse_catalog.py``):

* Exactitud: un runtime conocido resuelve a EXACTAMENTE un descriptor.
* Sin fallback: un runtime desconocido devuelve ``None``. Nunca "el último
  conocido", nunca SemVer: SKSE está pinneado al build del ejecutable.
* DLL coherente: ``dll_name`` codifica siempre el mismo runtime que
  ``game_version`` (convención upstream ``skse64_1_7_104.dll`` ↔ ``1.7.104``,
  la misma que ``discovery.scanner.skse_dll_game_version`` ya decodifica).
* Fuente explícita: todo release declara de dónde se obtiene.
* Inmutabilidad: los descriptores son ``frozen`` y el catálogo una tupla.

Fronteras deliberadas — lo que este módulo NO hace:

* No descarga ni verifica artifacts. ``artifact_name`` es sólo el nombre del
  archive cuando hay descarga directa verificada (fuente SILVERLOCK); los
  builds de NEXUS no tienen URL estática verificable hoy y declaran ``None``.
* No detecta el runtime (leer el PE es de ``discovery``) ni decide qué hacer
  con un ``None`` (informar, HITL, abortar): eso es del consumidor — en el PR
  que integre esto, ``ToolsInstaller.ensure_skse``.
* Sin I/O ni side effects DE ESTE MÓDULO: la regla de comparación vive en
  ``discovery.scanner`` (una sola fuente, no se duplica). Ojo con una precisión
  medida con audit hook: ``import sky_claw`` por sí solo ya lee
  ``config.toml`` y ``security_policy.yaml`` (lo hace el ``__init__`` del
  paquete raíz, deuda pre-existente de TODO módulo del repo) — el catálogo no
  AGREGA ninguna lectura propia; ese estado no puede arreglarse desde acá sin
  reestructurar los ``__init__``, que es deuda de otro PR.
* No conoce ediciones, stores, GOG ni VR: el repo no soporta VR y retiró GOG
  de ``SKSE_CONFIG`` (anclado en ``tests/test_tools_installer.py``); este
  catálogo no los reintroduce.

Evidencia upstream de la tabla (verificada 2026-09-20):

* ``skse.silverlock.org`` (página oficial del equipo SKSE): declara "Current
  Anniversary Edition build 2.3.1 (game version 1.7.104): Nexus", "Current
  Special Edition build 2.0.20 (game version 1.5.97)" y "Current classic
  build 1.7.3", con 7z directos para los dos últimos; ``/download/archive/``
  confirma los nombres exactos de los artifacts servidos por silverlock y que
  la familia 2.2.x se detiene en ``skse64_2_02_06.7z`` (y su variante GOG):
  los builds 2.2.7/2.2.8 no tienen 7z directo, se distribuyen vía Nexus.
* ``ianpatt/skse64``, ``skse64_whatsnew.txt`` (master): "2.2.6 — support for
  1.6.1170"; los builds posteriores del mismo runtime no repiten la línea
  "support for" (convención del changelog) y "2.2.8 — fix regression in old
  commonlib": 2.2.8 es el build recomendado para quien permanece en
  1.6.1170, y su archivo de Nexus se describe "Compatible with Skyrim Special
  Edition 1.6.1170 from Steam". También "2.3.0 — support for 1.7.99" y
  "2.3.1 — support for 1.7.104". Nota: el whatsnew ya lista un 2.3.2 (bugfix
  que no agrega soporte de runtime) pero ``skse64_common/skse_version.h``
  (master) declara ``CURRENT_RELEASE_SKSE_STR "2.3.1"`` y
  ``CURRENT_RELEASE_RUNTIME`` 1.7.104, la página oficial publica 2.3.1 como
  build AE vigente y Nexus la distribuye — un bump es una decisión explícita
  de este archivo, no automática.
* El pin de un runtime puede diferir del payload de adquisición de
  ``SKSE_CONFIG`` mientras convivan los dos modelos (1.6.1170: 2.2.8 acá vs
  ``skse64_2_02_06.7z`` allí): ambos son builds válidos del mismo runtime y
  el ancla de coherencia compara la identidad del DLL, no el pin. La
  unificación es del PR que integre el catálogo en ``ensure_skse``.
* El nombre del DLL sigue la convención upstream verificada en los tres
  artifacts descargables directos; para ``1.7.99`` además está confirmado por
  fuera (las guías de migración a 2.3.0 listan sus archivos:
  ``skse64_loader.exe`` + ``skse64_1_7_99.dll``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from sky_claw.local.discovery.scanner import skyrim_version_matches

__all__ = ["SKSE_RELEASES", "SkseRelease", "SkseSource", "resolve_skse_release"]


class SkseSource(enum.Enum):
    """Canal desde el que se obtiene un build de SKSE.

    SILVERLOCK: ``skse.silverlock.org`` sirve un archive 7z directo.
    NEXUS: el build se distribuye vía Nexus Mods (mod 30379); no hay archive
    directo verificado, por eso esos releases tienen ``artifact_name=None``.
    """

    SILVERLOCK = "silverlock"
    NEXUS = "nexus"


@dataclass(frozen=True, slots=True)
class SkseRelease:
    """Descriptor inmutable de un build de SKSE conocido para un runtime exacto.

    ``game_version`` se guarda SIEMPRE en forma canónica de tres segmentos
    (``"1.7.104"``). El PE del ejecutable puede reportar un cuarto segmento
    (``"1.7.99.0"``, observado en rigs reales); la equivalencia la resuelve
    ``resolve_skse_release``, no este campo.
    """

    game_version: str
    skse_version: str
    dll_name: str
    source: SkseSource
    artifact_name: str | None


# Tabla única y cerrada. Agregar o mutar una fila es una decisión explícita
# respaldada por evidencia upstream: el ancla de igualdad literal de
# ``tests/test_skse_catalog.py`` rompe ante cualquier cambio silencioso.
SKSE_RELEASES: tuple[SkseRelease, ...] = (
    SkseRelease(
        game_version="1.5.97",
        skse_version="2.0.20",
        dll_name="skse64_1_5_97.dll",
        source=SkseSource.SILVERLOCK,
        artifact_name="skse64_2_00_20.7z",
    ),
    # 1.6.1170: 2.2.6 introdujo el soporte, pero 2.2.8 es el build recomendado
    # para permanecer en ese runtime (2.2.7 trajo cambios y 2.2.8 corrige su
    # regresión de old CommonLib). No existe como 7z de silverlock → NEXUS, y
    # sin archive directo verificado: artifact_name=None (no se inventa).
    SkseRelease(
        game_version="1.6.1170",
        skse_version="2.2.8",
        dll_name="skse64_1_6_1170.dll",
        source=SkseSource.NEXUS,
        artifact_name=None,
    ),
    SkseRelease(
        game_version="1.7.99",
        skse_version="2.3.0",
        dll_name="skse64_1_7_99.dll",
        source=SkseSource.NEXUS,
        artifact_name=None,
    ),
    SkseRelease(
        game_version="1.7.104",
        skse_version="2.3.1",
        dll_name="skse64_1_7_104.dll",
        source=SkseSource.NEXUS,
        artifact_name=None,
    ),
    SkseRelease(
        game_version="1.9.32",
        skse_version="1.7.3",
        dll_name="skse_1_9_32.dll",
        source=SkseSource.SILVERLOCK,
        artifact_name="skse_1_07_03.7z",
    ),
)


def resolve_skse_release(game_version: str) -> SkseRelease | None:
    """Release de SKSE conocido para el runtime *game_version*, o ``None``.

    Formato admisible: 3 ó 4 segmentos NUMÉRICOS (``"1.7.104"`` /
    ``"1.7.104.0"``), que es lo que el recurso PE de ``SkyrimSE.exe`` reporta.
    Cualquier otra forma se rechaza ANTES de comparar: la semántica de prefijo
    de ``skyrim_version_matches`` por sí sola dejaría pasar un cuarto segmento
    arbitrario (``"1.7.104.0rc1"`` matcheaba porque sólo mira los primeros
    tres), y esa entrada no proviene del PE, así que no resuelve.

    La comparación la sigue haciendo ``skyrim_version_matches`` — la MISMA
    regla con la que el scanner decide si un DLL en disco sirve para un
    ejecutable: igualdad por prefijo sobre la clave canónica de tres
    segmentos. Por eso ``"1.7.104"`` y ``"1.7.104.0"`` resuelven al mismo
    descriptor, sin una segunda implementación de comparación de versiones.

    ``None`` es ausencia explícita (convención de ``discovery``), nunca
    fallback al release "más nuevo". Y si el catálogo estuviera corrupto (dos
    filas que matchean el mismo runtime), también responde ``None``: ante la
    ambigüedad este contrato dice "no sé", no adivina.
    """
    if not _es_runtime_admisible(game_version):
        return None
    matches = [release for release in SKSE_RELEASES if skyrim_version_matches(game_version, release.game_version)]
    if len(matches) != 1:
        return None
    return matches[0]


def _es_runtime_admisible(game_version: str) -> bool:
    """``"1.7.104"`` o ``"1.7.104.0"``: exactamente 3 ó 4 segmentos numéricos.

    Rechaza todo lo demás — más segmentos, sufijos no numéricos, espacios —
    antes de la comparación por prefijo. No es una segunda regla de
    compatibilidad: sólo delimita qué strings cuentan como versión de PE.
    """
    segmentos = game_version.split(".")
    return 3 <= len(segmentos) <= 4 and all(s.isdigit() for s in segmentos)
