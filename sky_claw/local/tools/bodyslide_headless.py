"""Contrato headless de BodySlide — argv, veredicto estricto y BuildSelection.

Módulo deliberadamente PURO: sin subprocess, sin disco, sin configuración. Es la
frontera del contrato ``PR 1`` («contrato headless de BodySlide, sin broker»): lo
que sale acá es un argv comprobado y un veredicto verificable, y todo lo que no
se puede comprobar se contesta tipado (``EVIDENCE_INCOMPLETE``) en vez de fingir
exito.

Fuentes verificadas (ousnius/BodySlide-and-Outfit-Studio, tag v6.2.0 commit
99103c3b52fff84beeb7a7bb2aab0bad2b5d4250, 2026-11-21 — NO el nombre del archivo):

* Flags declarados (``src/bodyslide/BodySlideApp.cpp::g_cmdLineDesc``):
  ``gbuild``, ``b``/build (outfits), ``f``/filter, ``regex``, ``t``/targetdir,
  ``p``/preset (nombre o ``ruta.xml[?nombre]``), ``tri``, ``conformaddons``,
  ``q``/quiet, ``preview``. Sky-Claw NO emite ``b``, ``f``, ``regex``,
  ``conformaddons`` ni ``preview`` por defecto: están declarados upstream pero
  no están autorizados en el contrato aprobado (ver ``pruebas`` en
  tests/test_contrato_argumentos_cli.py).
* ``q``/quiet — descriptor v6.2.0: «builds without dialogs: conflicting outputs
  take the saved or first choice without saving it, failures are only logged».
  ``-q`` NO suprime el modal de setup de TargetGame (``ShowSetup`` no mira
  ``cmdQuiet`` en OnInit), NO evita el fallback del Config.xml (``BuildListBodies``
  escribe ``GameDataPath`` en AppDir si datapath está vacío — hoy no aplica con
  ``-t``), y NO valida la existencia del preset (``GetBigPresetValue`` cae a
  valores defaults sin log ni error). Aun con ``-q``, un conflicto no resuelto
  NO puede delegarse a la herramienta: Sky-Claw exige ganador explícito.
* LogLevel (``src/utils/Log.cpp``): -1 Off / 0 wxLogFatalError / 1 wxLogError /
  2 wxLogWarning / 3 wxLogMessage. La firma ``[#Log <fecha>]`` se escribe al
  arrancar cada corrida (el único delimitador soportado); el archivo rota tras la
  cuarta corrida conservando sólo las dos últimas.
* BuildSelection.xml (``src/components/BuildSelection.h`` +
  ``BodySlideAppBuildRecords.cpp``): raíz ``<BuildSelection>``;
  ``<OutputChoice path="..." choice="..."/>``;
  ``<ZapChoice project="..." zap="..." choice="0|1"/>``;
  ``<Built path="..." folder="..." set="..." preset="..." time="...">`` con
  ``<SetSlider name="..." size="small|big" value="N"/>`` y
  ``<Zap name="..." zapped="0|1"/>``. Se guarda con BOM y declaración de XML.

Este módulo NO implementa el solver de conflictos (PR 3), no lanza procesos, no
lee ni escribe archivos externos.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

#: Extensión de artefacto de malla y morfos que un batch build emite (minúsculas).
EXTENSION_DE_ARTEFACTO_NIF = ".nif"
EXTENSION_DE_ARTEFACTO_TRI = ".tri"
EXTENSIONES_DE_ARTEFACTO = (EXTENSION_DE_ARTEFACTO_NIF, EXTENSION_DE_ARTEFACTO_TRI)

#: Coincidencia de un solo token de nombre razonable (mismo vocabulario que
#: ``_SAFE_NAME_PATTERN`` en schemas.py, que permite espacios dentro del preset).
_PATTERN_DE_NOMBRE = re.compile(r"^[a-zA-Z0-9_.\- ]+$")

#: Marker de fallo parcial que la herramienta escribe en su log al arrancar la
#: corrida (``wxLogError("Failed to build '%s': %s", ...)`` en GroupBuild/
#: CommandLineBuild). Es el único marker acotado de fallo parcial: con ret == 3
#: el exit code sigue siendo 0.
_MARCA_DE_FALLO_PARCIAL = "Failed to build"

#: Marker de conflicto SIN ganador explícito — la herramienta eligió por
#: cuenta (``BuildListBodies``/``CommandLineBuild``: «Outfits '%s' all write '%s',
#: building '%s'.», BuildListBodies.cpp línea 884). En ese caso no hay ningún
#: ganador certificado: la herramienta tomó una decisión a ciegas y el run no
#: puede declararse SUCCESS ni EVIDENCE_INCOMPLETE — es FAILURE (fail-closed).
_MARCA_DE_CONFLICTO_SIN_GANADOR = "all write"

#: Marker de conflicto RESUELTO POR SELECCIÓN GUARDADA (``CommandLineBuild``:
#: «Command-line build applied saved BuildSelection for output '%s': selected '%s',
#: skipped %d conflicting choice(s).», BodySlideAppBuild.cpp línea 1477). Es
#: DISTINTO del caso sin ganador: la elección existe (del BuildSelection guardado
#: del usuario) pero NO es el ganador explícito del plan de Sky-Claw — la
#: selección guardada es de otro run, no del plan actual. No puede declararse
#: SUCCESS; se distingue como EVIDENCE_INCOMPLETE verificable.
_MARCA_DE_CONFLICTO_VERIFICABLE = "applied saved BuildSelection"


class HeadlessContractError(ValueError):
    """Violación de contrato headless: argumento inválido, capacidad no verificada
    o conflicto sin ganador explícito. Es un error de PROGRAMACIÓN/plan, no de I/O."""


# --------------------------------------------------------------------------- #
# argv headless
# --------------------------------------------------------------------------- #


def construir_argv(
    exe: str,
    group: str,
    target: str,
    *,
    preset: str | None = None,
    preset_verified: bool = False,
    build_morphs: bool = True,
    quiet_verified: bool = False,
    stage_root: str | None = None,
) -> list[str]:
    """Construye el argv del batch headless. Lista de strings, sin shell.

    Reglas del contrato (PR 1):
    - ``-q`` sólo se emite cuando la capacidad está verificada; de lo contrario se
      NO emite nada y el caller tiene que rechazar el spawn (el runner lo hace).
    - ``-p`` sólo se emite con preset explícito **y verificado** (el plan debe
      certificar que el preset existe; la herramienta cae a valores defaults sin
      error si el nombre no existe — upstream ``SliderManager.cpp``). Un preset
      no verificado se RECHAZA, no se emite.
    - ``-p`` nunca recibe una ruta: el caller debe pasar sólo el nombre del
      preset (upstream también admite ``ruta.xml[?nombre]``, pero está fuera de
      este contrato).
    - ``-tri`` sólo se emite con ``build_morphs``.
    - Nunca ``-b``/``-f``/``-regex``/``-conformaddons``/``-preview``/``-gbuild``
      duplicado: el naming íntegro está fijado por los tests del contrato.
    - El ``target`` debe ser una ruta **absoluta** (Windows ``C:/...`` o POSIX
      ``/...``) y **normalizada**: ningún componente ``.`` o ``..`` literal —
      una ruta con ``..`` que escapa del staging pasa por el ``relative_to``
      léxico de la stdlib sin importar que el destino físico caiga fuera del
      staging administrado. Cuando ``stage_root`` se declara, el target debe
      estar **confinado** al staging administrado declarado — comparación
      léxica de componentes (enlaces físicos —symlinks/junctions— se verifican
      en el runner, no en el contrato puro).

    Returns:
        Lista de strings, ``argv[0]`` es el ejecutable.

    Raises:
        HeadlessContractError: grupo/target/preset inválidos, preset sin
            verificación, o target fuera del staging administrado.
    """
    exe = _validar_exe(exe)
    group = _validar_nombre_de_grupo(group)
    target = _validar_target(target, stage_root=stage_root)

    argv = [exe, "-gbuild", group, "-t", target]

    if preset is not None:
        if not preset_verified:
            raise HeadlessContractError(
                "preset no verificado: no se puede emitir -p sin evidencia de que el preset existe. "
                "Declará preset_verified=True (certificado por el plan/rig) o omití el preset."
            )
        preset = _validar_preset(preset)
        argv += ["-p", preset]

    if build_morphs:
        argv.append("-tri")

    if quiet_verified:
        argv.append("-q")

    return argv


def _validar_exe(exe: object) -> str:
    if not isinstance(exe, str) or not exe.strip():
        raise HeadlessContractError("exe inválido: debe ser un string no vacío")
    if "\x00" in exe:
        raise HeadlessContractError("exe contiene NUL byte")
    return exe


def _validar_nombre(value: object, nombre: str) -> str:
    if not isinstance(value, str):
        raise HeadlessContractError(f"{nombre} debe ser un string")
    v = value.strip()
    if not v:
        raise HeadlessContractError(f"{nombre} no puede estar vacío")
    if "\x00" in v:
        raise HeadlessContractError(f"{nombre} contiene NUL byte")
    if not (1 <= len(v) <= 128):
        raise HeadlessContractError(f"{nombre} debe tener entre 1 y 128 caracteres")
    # Se prohíbe PATHS/separadores: group se pasa como token único tras -gbuild.
    if "/" in v or "\\" in v or v in (".", ".."):
        raise HeadlessContractError(f"{nombre} no puede ser un path ('{v}')")
    if not _PATTERN_DE_NOMBRE.fullmatch(v):
        raise HeadlessContractError(f"{nombre} contiene caracteres que no son nombre válido: '{v}'")
    return v


#: Nombres de dispositivo reservados por Win32 (``CreateFile`` los abre como
#: dispositivo, no como archivo/carpeta), comparados sin distinguir mayúsculas.
#: Incluye ``COM0``/``LPT0`` y las variantes con superíndice ``¹²³`` que la
#: documentación de Win32 («Naming Files, Paths, and Namespaces») lista.
_DISPOSITIVOS_WIN32 = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{base}{d}" for base in ("com", "lpt") for d in "0123456789\u00b9\u00b2\u00b3"}
)

#: Caracteres que Win32 prohíbe en un componente de ruta (además de 0-31).
_CARACTERES_PROHIBIDOS_WIN32 = frozenset('<>:"/\\|?*')


def _validar_nombre_de_grupo(value: object) -> str:
    """Valida el grupo como token de ``-gbuild`` Y como carpeta física Win32 (P1-D, #1003).

    El grupo es el ÚLTIMO componente del destino canónico
    (``BodySlide_Output/<group>``, ``output_targets.bodyslide_output_target``).
    Win32 no lo respeta tal cual en estos casos, así que dos identidades
    lógicas distintas terminarían en la misma carpeta física (o en un
    dispositivo) — el aislamiento por grupo de P1-A dejaría de valer:

    - punto o espacio FINAL: Win32 los descarta (``CBBE.`` ≡ ``CBBE ``≡ ``CBBE``);
    - espacio INICIAL: el contrato recorta el token de ``-gbuild`` pero el
      destino canónico usaría el nombre crudo — se exige el nombre sin
      espacios alrededor en vez de elegir uno de los dos;
    - nombre de dispositivo reservado (``CON``, ``PRN``, ``AUX``, ``NUL``,
      ``COM0-9``, ``LPT0-9``, superíndices ``¹²³``), sin distinguir
      mayúsculas y TAMBIÉN con extensión (``CON.txt``, ``CON .x``): Win32
      mira la parte anterior al primer punto, sin espacios finales;
    - caracteres prohibidos ``<>:"/\\|?*`` y de control 0-31 (el patrón
      ASCII ya los excluye; se chequean explícitamente para que la propiedad
      no dependa de ese patrón).

    NO resuelve que dos grupos difieran sólo en mayúsculas (``CBBE`` y
    ``cbbe`` son la MISMA carpeta en NTFS): es una limitación documentada
    (sin catálogo de grupos en PR 1); ver la evidencia de PR 1.

    Raises:
        HeadlessContractError: si el nombre no es una carpeta Win32 independiente.
    """
    if isinstance(value, str) and value != value.strip():
        raise HeadlessContractError(f"group no puede tener espacios al inicio ni al final: '{value}'")
    v = _validar_nombre(value, "group")
    if any(ord(c) < 32 for c in v) or any(c in _CARACTERES_PROHIBIDOS_WIN32 for c in v):
        raise HeadlessContractError(f"group contiene caracteres prohibidos por Win32: '{v}'")
    if v.endswith((".", " ")):
        raise HeadlessContractError(f"group no puede terminar en punto ni en espacio (Win32 los descarta): '{v}'")
    raiz_del_nombre = v.split(".", 1)[0].rstrip(" ").casefold()
    if raiz_del_nombre in _DISPOSITIVOS_WIN32:
        raise HeadlessContractError(f"group '{v}' es un nombre de dispositivo reservado por Win32")
    return v


def validar_nombre_de_grupo(value: object) -> str:
    """Versión pública de las reglas Win32 del grupo (P1-D) para los caminos que tocan disco ANTES del runner.

    ``run_bodyslide_batch`` aparta ``BodySlide_Output/<group>`` con
    ``DirectoryRollback`` antes de llamar al runner: si el grupo fuera ``CBBE.``,
    Win32 descartaría el punto y el move-aside movería la carpeta ``CBBE``
    (review CodeRabbit #1003). Misma regla, una sola implementación.
    """
    return _validar_nombre_de_grupo(value)


def exigir_identidad_de_grupo_unica(group: str, existentes: Iterable[str]) -> None:
    """Rechaza un grupo que sólo difiere en mayúsculas de una carpeta existente del staging (cierre #1003).

    En NTFS ``BodySlide_Output/CBBE`` y ``BodySlide_Output/cbbe`` son la MISMA
    carpeta: tratarlos como destinos independientes haría que el build de uno
    aparte y descarte la salida del otro. Sin catálogo de grupos (PR 3), la
    única evidencia de la otra identidad es la grafía de la carpeta que ya
    existe; la misma grafía exacta es la misma identidad y se acepta.

    Puro: el caller lista la raíz del staging ANTES de cualquier move-aside
    (``DirectoryRollback`` renombra la carpeta con el nombre pedido y borra la
    grafía original).

    Raises:
        HeadlessContractError: si alguna entrada existente es equivalente sin
            distinguir mayúsculas pero con otra grafía.
    """
    clave = group.casefold()
    for existente in existentes:
        if existente != group and existente.casefold() == clave:
            raise HeadlessContractError(
                f"group '{group}' sólo difiere en mayúsculas del destino existente '{existente}': "
                "en NTFS son la misma carpeta y no pueden tratarse como grupos independientes"
            )


def _es_absoluta(ruta: str) -> bool:
    """Rutas absolutas: POSIX ``/...``, Windows drive-rooted ``C:/...`` y UNC ``\\\\x``."""
    if not isinstance(ruta, str):
        return False
    if ruta.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", ruta):
        return True
    return ruta.startswith("\\\\")


#: Patrón de ruta Windows drive-rooted (``C:\...`` o ``C:/...``).
_PATRON_DE_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _es_unc(ruta: str) -> bool:
    """Rutas UNC Windows: comienzan por dos barras invertidas."""
    return ruta.startswith("\\\\")


def _flavour_de_ruta(ruta: str) -> type:
    """Gramática léxica de la ruta, elegida por la ruta misma, no por el host.

    El contrato es PURO y valida contra el objetivo: una ruta UNC ``\\server``
    o ``C:/...`` siempre se compara como ruta de Windows (case-insensitive),
    aunque la suite corra en un host POSIX — de lo contrario un escape contra un
    staging de Windows pasaría desapercibido sobre un host Linux.
    """
    if _es_unc(ruta) or _PATRON_DE_DRIVE.match(ruta):
        return PureWindowsPath
    return PurePosixPath


def _componentes_residuales(ruta: str) -> list[str]:
    """Componentes literales '.' o '..' en la ruta (comparación de texto crudo).

    ``PurePath.parts`` ABSORBE el '.' (``pathlib`` lo colapsa silenciosamente) y
    conserva el '..' solo en la representación en memoria — el raw scan es
    autoritativo para rechazar rutas no normalizadas antes de confinar.
    """
    return [comp for comp in ruta.replace("\\", "/").split("/") if comp in (".", "..")]


def _estructuras_de_ruta(ruta: str, flavour: type, *, nombre: str) -> tuple[str, tuple[str, ...]]:
    """Descompone una ruta absoluta en ``(base, componentes)`` LÉXICAMENTE.

    POSIX: ``("/", principales)``. Windows drive-rooted: ``("G:", principales)``.
    Windows UNC: ``("<base UNC>", principales)``.

    Reemplaza el ``PurePath.relative_to`` por una comparación de componentes
    desincronizados contra la estructura de la ruta: ese método IGNORA los
    ``..`` literal (compara prefijos tal cual), así que una ruta con
    ``..`` que escapa del staging —``/safe/staging/../outside``— pasaba por el
    chequeo. Fail-closed: cualquier componente ``.`` o ``..`` se RECHAZA — el
    contrato no transporta rutas no normalizadas.

    Raises:
        HeadlessContractError: ruta no absoluta, de flavour no clasificable,
            o con componentes ``.``/``..`` literal.
    """
    residuales = _componentes_residuales(ruta)
    if residuales:
        raise HeadlessContractError(
            f"{nombre} contiene un componente '{residuales[0]}' — la ruta debe venir normalizada: '{ruta}'"
        )
    partes = flavour(ruta).parts
    if flavour is PurePosixPath:
        if not partes or partes[0] != "/":
            raise HeadlessContractError(f"{nombre} no es una ruta absoluta POSIX: '{ruta}'")
        base = "/"
        componentes = partes[1:]
    elif partes and _es_unc(partes[0]):
        base = partes[0].rstrip("\\/")
        componentes = partes[1:]
    elif partes and _PATRON_DE_DRIVE.match(partes[0]):
        base = partes[0][:2]
        componentes = partes[1:]
    else:
        raise HeadlessContractError(f"{nombre} no es una ruta de Windows clasificable (ni drive ni UNC): '{ruta}'")
    return base, tuple(componentes)


def _confinado(target: str, stage_root: str) -> bool:
    """Compara ``target`` contra ``stage_root`` tras normalización LÉXICA.

    Fail-closed: «fuera del staging», «otro volumen», «otra base UNC» o «no
    comparable» ⇒ False. El contrato valida contra el objetivo (Windows), no
    contra el host: la comparación de componentes ignora mayúsculas/minúsculas
    SOLO cuando ambas rutas son de Windows/UNC (la semántica del objetivo es
    case-insensitive, no la del host de la suite).

    Raises:
        HeadlessContractError: componente ``.``/``..`` literal, ruta no absoluta
            de flavour no clasificable, o flavours incompatibles entre sí.
    """
    flavour = (
        PureWindowsPath
        if (
            _es_unc(stage_root)
            or _es_unc(target)
            or _PATRON_DE_DRIVE.match(stage_root)
            or _PATRON_DE_DRIVE.match(target)
        )
        else PurePosixPath
    )
    root_base, root_comps = _estructuras_de_ruta(stage_root, flavour, nombre="stage_root")
    target_base, target_comps = _estructuras_de_ruta(target, flavour, nombre="target")
    if flavour is PureWindowsPath:
        root_base = root_base.casefold()
        target_base = target_base.casefold()
        root_comps = tuple(c.casefold() for c in root_comps)
        target_comps = tuple(c.casefold() for c in target_comps)
    if target_base != root_base:
        return False
    if len(target_comps) < len(root_comps):
        return False
    return target_comps[: len(root_comps)] == root_comps


def exigir_destino_canonico(target: str, canonico: str) -> None:
    """Exige que *target* sea EXACTAMENTE el destino canónico de su grupo (P1-A, revisión #1003).

    ``_confinado`` sólo prueba que el target cuelga del staging administrado: un
    grupo ``CBBE`` con destino en ``BodySlide_Output/3BA`` (la carpeta de OTRO
    grupo), en la raíz compartida o en un subdirectorio propio pasaba igual, y
    el move-aside por grupo (``DirectoryRollback``) protege sólo el target
    canónico. Esta comparación es de IGUALDAD de componentes, no de prefijo.

    La gramática la fija el canónico (la ruta que deriva
    ``bodyslide_output_target`` para el juego objetivo), no el host: contra un
    canónico de Windows (drive o UNC) el target se interpreta como ruta de
    Windows — separadores ``/`` o ``\\`` indistintos, barra final ignorada y
    comparación case-insensitive (NTFS) —; contra un canónico POSIX la
    comparación distingue mayúsculas. Componentes ``.``/``..`` se rechazan
    antes de comparar (fail-closed, igual que el resto del contrato).

    Raises:
        HeadlessContractError: target vacío/no absoluto/no normalizado, de otro
            volumen o servidor, la raíz compartida, otro grupo o un subdirectorio.
    """
    for nombre, valor in (("target", target), ("target canónico", canonico)):
        if not isinstance(valor, str) or not valor.strip():
            raise HeadlessContractError(f"{nombre} no puede estar vacío")
        if "\x00" in valor:
            raise HeadlessContractError(f"{nombre} contiene NUL byte")
    flavour: type = PureWindowsPath if (_es_unc(canonico) or _PATRON_DE_DRIVE.match(canonico)) else PurePosixPath
    base_canon, comps_canon = _estructuras_de_ruta(canonico, flavour, nombre="target canónico")
    base_target, comps_target = _estructuras_de_ruta(target, flavour, nombre="target")
    if flavour is PureWindowsPath:
        base_canon = base_canon.casefold()
        base_target = base_target.casefold()
        comps_canon = tuple(c.casefold() for c in comps_canon)
        comps_target = tuple(c.casefold() for c in comps_target)
    if (base_target, comps_target) != (base_canon, comps_canon):
        raise HeadlessContractError(
            f"target '{target}' no es el destino canónico del grupo ('{canonico}') — "
            "ni la raíz compartida, ni la carpeta de otro grupo, ni un subdirectorio"
        )


def _validar_target(target: object, *, stage_root: str | None = None) -> str:
    """Valida el target del batch: absoluto, normalizado y confinado al staging.

    ``stage_root`` es el origen del staging administrado (``BodySlide_Output``
    bajo el juego) — cuando se declara, el target debe estar contenido en él,
    comparación léxica de componentes (no ``relative_to``, que al comparar
    prefijos ignora ``..`` literal). La ruta y su ``stage_root`` deben venir
    normalizadas: un componente ``.`` o ``..`` literaRe se RECHAZA — el argv
    no transporta rutas ambiguas. Cuando ``stage_root`` no se declara, sólo se
    exige la ruta absoluta (contrato puro de PR 1; el runner siempre declara el
    staging real ante este contrato).
    """
    if not isinstance(target, str) or not target.strip():
        raise HeadlessContractError("target no puede estar vacío")
    if "\x00" in target:
        raise HeadlessContractError("target contiene NUL byte")
    if not _es_absoluta(target):
        raise HeadlessContractError(f"target debe ser una ruta absoluta, no '{target}'")
    if stage_root is not None:
        if not isinstance(stage_root, str) or not stage_root.strip():
            raise HeadlessContractError("stage_root no puede estar vacío")
        if not _es_absoluta(stage_root):
            raise HeadlessContractError(f"stage_root debe ser una ruta absoluta, no '{stage_root}'")
        if not _confinado(target, stage_root):
            raise HeadlessContractError(
                f"target '{target}' debe estar confinado al staging administrado '{stage_root}'"
            )
    return target


def _validar_preset(preset: str) -> str:
    """El preset se pasa SOLO como nombre, nunca como ruta.

    Upstream admite ``ruta.xml[?nombre]``, pero el contrato aprobado de Sky-Claw
    acepta sólo el nombre (el LLM no lo elige a ciegas; el plan de PR 3 debe
    certificarlo). Regla dura: ningún separador de path, ningún ``?``, ningún
    ``.xml``: un preset enviado como ruta es oculto de la sandbox.
    """
    v = preset.strip()
    if "\x00" in v:
        raise HeadlessContractError("preset contiene NUL byte")
    if not (1 <= len(v) <= 128):
        raise HeadlessContractError("preset debe tener entre 1 y 128 caracteres")
    if "/" in v or "\\" in v or "?" in v or ":" in v:
        raise HeadlessContractError(f"preset debe ser un NOMBRE, no una ruta ni un «ruta?nombre»: '{v}'")
    if v.lower().endswith(".xml"):
        raise HeadlessContractError(f"preset es un nombre, no un archivo XML: '{v}'")
    if not _PATTERN_DE_NOMBRE.fullmatch(v):
        raise HeadlessContractError(f"preset contiene caracteres que no son nombre válido: '{v}'")
    return v


def capability_quiet_permitida(quiet_verified: bool) -> bool:
    """Puerta de spawn: sólo se lanza el build autónomo si -q está verificado."""
    return bool(quiet_verified)


# --------------------------------------------------------------------------- #
# BuildSelection.xml — elección explícita + serialización determinista (PR 1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConflictoDeSalida:
    """Dos o más outfits que escriben el MISMO path de salida.

    El conflicto no se resuelve acá: este objeto sólo declara la evidencia de
    que el plan necesita un ganador.
    """

    path: str
    outfit: tuple[str, ...]  # >= 2


def validar_conflictos(conflictos: list[ConflictoDeSalida] | tuple[ConflictoDeSalida, ...]) -> None:
    """Valida la evidencia de conflicto: path no vacío + >= 2 outfits distintos."""
    for conflicto in conflictos:
        if not isinstance(conflicto.path, str) or not conflicto.path.strip():
            raise HeadlessContractError(f"conflicto con path inválido: {conflicto!r}")
        if "\x00" in conflicto.path:
            raise HeadlessContractError(f"conflicto con path que contiene NUL byte: {conflicto.path!r}")
        outfit = tuple(dict.fromkeys(o.strip() for o in conflicto.outfit if isinstance(o, str)))
        if len(outfit) < 2:
            raise HeadlessContractError(f"conflicto en '{conflicto.path}' debe nombrar al menos 2 outfits distintos")
        for o in outfit:
            _validar_nombre(o, "outfit")


def resolver_conflictos(
    conflictos: list[ConflictoDeSalida] | tuple[ConflictoDeSalida, ...],
    selecciones: Mapping[str, str],
) -> None:
    """Verifica que todo conflicto tenga ganador EXPLICITAMENTE declarado.

    No se infiere ganador por orden, por último construido, por prioridad MO2 ni
    por ninguna otra heurística: la selección debe nombrar un outfit que sea parte
    del conflicto, y el plan debe declarar esa selección para cada conflicto.
    Los selecciones de un conflicto pueden propagarse: una sola elección cubre
    el path.

    Raises:
        HeadlessContractError: si hay conflictos sin elección explícita válida.
    """
    # Tipo restringido (revisión #1003): sólo un mapping ``path → outfit``. La
    # firma anterior aceptaba una tupla de dicts que ``dict()`` no sabe leer
    # (``ValueError`` crudo) o, peor, reinterpreta un dict de dos claves como
    # un par ``(path, outfit)`` inventado. Fail-closed con error de contrato.
    if not isinstance(selecciones, Mapping):
        raise HeadlessContractError(f"selecciones debe ser un mapping path→outfit, no {type(selecciones).__name__}")
    selecciones = dict(selecciones)
    faltantes: list[str] = []
    for conflicto in conflictos:
        seleccion = selecciones.get(conflicto.path)
        if seleccion is None or not isinstance(seleccion, str):
            faltantes.append(conflicto.path)
            continue
        if seleccion.strip() not in conflicto.outfit:
            faltantes.append(f"{conflicto.path} (ganador '{seleccion}' no está en los outfits del conflicto)")
    if faltantes:
        raise HeadlessContractError("conflictos sin ganador explícito y válido: " + "; ".join(faltantes))


def serializar_build_selection(
    selecciones: dict[str, str] | tuple[tuple[str, str], ...],
    *,
    zap_selecciones: dict[tuple[str, str], bool] | None = None,
) -> str:
    """Serializa elecciones EXPLÍCITAS de BuildSelection a XML determinista.

    Determinismo y seguridad:
    - orden fijo por ruta (sort por ruta), zaps ordenados por ``(project, zap)``;
    - atributos se escapan con el serializador, nunca por concat de strings;
    - las selecciones se validan (path/choice no vacíos, sin NUL) ANTES de
      serializar;
    - NO escribe archivos: el caller es responsable de entregar el texto a un
      destino apropiado (PR 2). Nunca se sobrescribe el ``BuildSelection.xml``
      del usuario irreversiblemente.

    Returns:
        Texto XML determinista con declaración ``<?xml ...?>`` y raíz
        ``<BuildSelection>``.

    Raises:
        HeadlessContractError: si alguna selección es inválida.
    """
    items = list(selecciones.items()) if isinstance(selecciones, dict) else list(selecciones)
    zap_selecciones = dict(zap_selecciones) if zap_selecciones else {}

    selecciones_ordenadas: list[tuple[str, str]] = []
    for path, choice in items:
        if not isinstance(path, str) or not path.strip():
            raise HeadlessContractError("path de selección inválido")
        if "\x00" in path:
            raise HeadlessContractError("path de selección contiene NUL byte")
        if not isinstance(choice, str) or not choice.strip():
            raise HeadlessContractError(f"choice de selección inválida (path={path})")
        if "\x00" in choice:
            raise HeadlessContractError(f"choice de selección contiene NUL byte (path={path})")
        selecciones_ordenadas.append((path, choice))

    selecciones_ordenadas.sort(key=lambda p: p[0])

    zaps_ordenados: list[tuple[tuple[str, str], bool]] = []
    for (project, zap), choice in zap_selecciones.items():
        if not isinstance(project, str) or not project.strip():
            raise HeadlessContractError("project de zap inválido")
        if not isinstance(zap, str) or not zap.strip():
            raise HeadlessContractError("zap inválido")
        for campo, valor in (("project", project), ("zap", zap)):
            if "\x00" in valor:
                raise HeadlessContractError(f"{campo} de zap contiene NUL byte")
        if not isinstance(choice, bool):
            raise HeadlessContractError("zap choice debe ser bool")
        zaps_ordenados.append(((project, zap), choice))

    zaps_ordenados.sort(key=lambda p: (p[0][0], p[0][1]))

    import xml.etree.ElementTree as ET

    raiz = ET.Element("BuildSelection")
    for path, choice in selecciones_ordenadas:
        elem = ET.SubElement(raiz, "OutputChoice")
        elem.set("path", path)
        elem.set("choice", choice)
    for (project, zap), choice in zaps_ordenados:
        elem = ET.SubElement(raiz, "ZapChoice")
        elem.set("project", project)
        elem.set("zap", zap)
        elem.set("choice", "1" if choice else "0")

    texto_declarable = ET.tostring(raiz, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + texto_declarable


# --------------------------------------------------------------------------- #
# Veredicto estricto (pure contract, sin I/O)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenciaDeBuild:
    """Evidencia que el runner reporta sobre un build; NO se parsea ni confía."""

    return_code: int = 1
    proceso_terminado: bool = True
    log_atribuible: bool = False
    log_contiene_fallo_parcial: bool = False
    #: Conflicto SIN ganador explícito — la herramienta eligio por cuenta
    #: (BuildListBodies, línea 884: «Outfits '%s' all write '%s', building '%s'.»).
    #: Fail-closed ⇒ FAILURE.
    log_contiene_conflicto_sin_ganador: bool = False
    #: Conflicto resuelto por selección guardada (CommandLineBuild línea 1477).
    #: Distinto de sin-ganador: la elección existe pero NO es el ganador
    #: certificado del plan ⇒ EVIDENCE_INCOMPLETE (no SUCCESS).
    log_contiene_conflicto_verificable: bool = False
    destino_propio: bool = True
    artefactos_nif: int = 0
    artefactos_tri: int = 0
    #: Archivos preexistentes bajo el destino ANTES del spawn (distinto de los
    #: "nuevos" contados). Cualquier preexistente es evidencia de que el staging
    #: no estaba vacío y su reparación no es mecánica — el veredicto no puede
    #: afirmar un build limpio sobre un staging sucio.
    preexistentes: int = 0
    #: Preset EFECTIVO de la corrida verificado contra el plan (cierre #1003).
    #: Sin ``-p``, ``CommandLineBuild`` usa el ``SelectedPreset`` que la GUI dejó
    #: guardado en ``BodySlide.xml`` (upstream v6.2.0 ``BodySlideAppBuild.cpp``):
    #: el preset real es desconocido ⇒ nunca SUCCESS. ``True`` sólo cuando se
    #: emitió ``-p`` con un preset que el plan certificó (``preset_verified``).
    preset_efectivo_verificado: bool = False


@dataclass(frozen=True, slots=True)
class ManifiestoArtefactos:
    """Manifiesto de artefactos esperados (derivado del proyecto/catálogo cuando
    PR 3 lo expone). Conteo absoluto de archivos esperados bajo el destino.

    ``verificado=False`` (default) — manifesto de solo conteo. NO certifica
    completitud de mallas (P1-6, auditoría): un conteo de archivos mata falso
    verde, pero no certifica integridad ni completitud de las mallas (no hay
    hashes/índices ni procedencia verificada). Conteo verificado = evidencia
    parcial; el veredicto es EVIDENCE_INCOMPLETE aunque se satisfagan los conteos.

    ``verificado=True`` — manifesto certificado por una fuente verificable
    (PR 3: catálogo con hashes/índices). Sólo entonces SUCCESS es legítimo.
    ``verificado=True`` exige tres cosas, las tres legítimas por separado
    (P0-C, auditoría):

    1. **Requisitos positivos coherentes** — al menos uno de ``nif_requeridos``
       y ``tri_requeridos`` > 0. Un manifiesto verificado sin requisito positivo
       es vacío y certifica nada.
    2. **Procedencia declarada** — una cadena no vacía que declare QUÉ lo
       certifica (``procedencia="catálogo:v3"``). Un bool declarado por el
       caller no prueba nada sobre la integridad de las mallas, sólo que el
       caller lo asume; sin antecedente verificable, el manifiesto no autoriza
       SUCCESS.
    3. ``evaluar_veredicto`` exige cobertura EFECTIVA (conteos reales ≥ los
       requisitos declarados) — un manifiesto verificado con evidencia incompleta
       sigue siendo FAILURE, no SUCCESS ni EVIDENCE_INCOMPLETE.
    """

    nif_requeridos: int = 0
    tri_requeridos: int = 0
    verificado: bool = False
    #: Qué fuente certifica el manifiesto (pr 3: ``"catálogo:v3"``). Exigida
    #: cuando ``verificado=True``; ``None``/vacío ⇒ el manifiesto no autoriza
    #: SUCCESS aunque se satisfagan los conteos (P0-C).
    procedencia: str | None = None

    def __post_init__(self) -> None:
        for campo, valor in (
            ("nif_requeridos", self.nif_requeridos),
            ("tri_requeridos", self.tri_requeridos),
        ):
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise HeadlessContractError(f"{campo} debe ser un entero, no {type(valor).__name__}")
            if valor < 0:
                raise HeadlessContractError(f"{campo} no puede ser negativo ({valor})")
        if self.verificado:
            if self.nif_requeridos + self.tri_requeridos == 0:
                raise HeadlessContractError(
                    "un manifiesto verificado debe declarar requisitos positivos "
                    "(nif_requeridos y/o tri_requeridos > 0) — un conteo vacío no certifica nada"
                )
            procedencia = (self.procedencia or "").strip()
            if not procedencia:
                raise HeadlessContractError(
                    "un manifiesto verificado requiere procedencia declarada "
                    "(qué fuente lo certifica) — el caller sólo puede declarar "
                    "verificado cuando la fuente existe y está certificada"
                )
            if "\x00" in procedencia:
                raise HeadlessContractError("procedencia contiene NUL byte")


@dataclass(frozen=True, slots=True)
class VeredictoContrato:
    estado: Literal["SUCCESS", "FAILURE", "EVIDENCE_INCOMPLETE"]
    razon: str

    @property
    def exitoso(self) -> bool:
        return self.estado == "SUCCESS"


def evaluar_veredicto(
    evidencia: EvidenciaDeBuild,
    manifiesto: ManifiestoArtefactos | None,
) -> VeredictoContrato:
    """Conjunción estricta de PR 1 — nunca SUCCESS con evidencia incompleta.

    Reglas (orden = prioridad de lectura):
    1. Terminalidad desconocida ⇒ EVIDENCE_INCOMPLETE (HOLD).
    2. rc != 0 ⇒ FAILURE (rc no se convierte en éxito aunque haya mallas).
    3. Fallo parcial en log ⇒ FAILURE.
    4. Conflicto SIN ganador explícito ⇒ FAILURE (la herramienta eligió por
       cuenta — fail-closed).
    5. Conflicto resuelto por selección guardada ⇒ EVIDENCE_INCOMPLETE — la
       elección existe pero no es el ganador certificado del plan (distinción
       explícita de P1-7).
    6. Log ausente/no atribuible ⇒ EVIDENCE_INCOMPLETE (jamás SUCCESS).
    7. Destino enlazado/no propio ⇒ FAILURE (fail-closed).
    8. Destino no vacío antes del spawn ⇒ FAILURE ("preexisting file" ⇒ PROHIBITED).
    9. Cero artefactos nuevos ⇒ FAILURE.
    10. Sin manifiesto de artefactos ⇒ EVIDENCE_INCOMPLETE.
    11. Manifiesto no satisfecho (nif/tri insuficientes) ⇒ FAILURE.
    12. Manifiesto SATISFECHO pero NO verificado (solo conteo) ⇒ EVIDENCE_INCOMPLETE
        — un count no certifica completitud de mallas (distinción explícita de P1-6).
    13. Preset efectivo desconocido (sin ``-p`` verificado) ⇒ EVIDENCE_INCOMPLETE —
        la herramienta construyó con el preset guardado de la GUI (cierre #1003).
        Sólo puede bajar un SUCCESS: nunca enmascara un FAILURE de arriba.
    14. Todo lo anterior satisfecho ⇒ SUCCESS (contrato puro; NO implica rig
        certificado).
    """
    if not evidencia.proceso_terminado:
        return VeredictoContrato("EVIDENCE_INCOMPLETE", "terminalidad desconocida del proceso (HOLD)")
    if evidencia.return_code != 0:
        return VeredictoContrato("FAILURE", f"return_code={evidencia.return_code}")
    if evidencia.log_contiene_fallo_parcial:
        return VeredictoContrato("FAILURE", "fallo parcial en el log de la corrida (Failed to build)")
    if evidencia.log_contiene_conflicto_sin_ganador:
        return VeredictoContrato(
            "FAILURE",
            "la herramienta eligió por cuenta un conflicto sin ganador explícito del plan",
        )
    if evidencia.log_contiene_conflicto_verificable:
        return VeredictoContrato(
            "EVIDENCE_INCOMPLETE",
            "conflicto resuelto por selección guardada — no es el ganador certificado del plan",
        )
    if not evidencia.log_atribuible:
        return VeredictoContrato(
            "EVIDENCE_INCOMPLETE",
            "log ausente, vacío o no atribuible a esta corrida",
        )
    if not evidencia.destino_propio:
        return VeredictoContrato("FAILURE", "el destino de staging es un enlace, no un directorio propio")
    if evidencia.preexistentes > 0:
        return VeredictoContrato(
            "FAILURE",
            f"el destino llevaba {evidencia.preexistentes} archivo(s) PREEXISTENTES — "
            "el staging debe estar vacío antes del spawn (veredicto PROHIBITED)",
        )
    if evidencia.artefactos_nif + evidencia.artefactos_tri == 0:
        return VeredictoContrato("FAILURE", "sin artefactos nuevos en el destino (build vacío o imposible)")
    if manifiesto is None:
        return VeredictoContrato(
            "EVIDENCE_INCOMPLETE",
            "sin manifiesto de artefactos verificable (sin catálogo PR 3)",
        )
    if evidencia.artefactos_nif < manifiesto.nif_requeridos:
        return VeredictoContrato(
            "FAILURE",
            f"mallas insuficientes: {evidencia.artefactos_nif}/{manifiesto.nif_requeridos} .nif requeridos",
        )
    if evidencia.artefactos_tri < manifiesto.tri_requeridos:
        return VeredictoContrato(
            "FAILURE",
            f"morfos insuficientes: {evidencia.artefactos_tri}/{manifiesto.tri_requeridos} .tri requeridos",
        )
    if not manifiesto.verificado:
        return VeredictoContrato(
            "EVIDENCE_INCOMPLETE",
            "manifiesto de solo conteo — no certifica completitud de mallas "
            "(requiere fuentes verificadas: catálogo/hashes, PR 3)",
        )
    if not evidencia.preset_efectivo_verificado:
        return VeredictoContrato(
            "EVIDENCE_INCOMPLETE",
            "preset efectivo desconocido — sin -p verificado, BodySlide construye con el "
            "SelectedPreset guardado por la GUI, que el plan no certificó",
        )
    return VeredictoContrato("SUCCESS", "")


def marcadores_de_corrida(texto_de_corrida: str) -> tuple[bool, bool, bool]:
    """Devuelve ``(fallo_parcial, conflicto_sin_ganador, conflicto_verificable)``.

    Los dos conflictos son DISJUNTAS (P1-7): un log que contiene la selección
    guardada no es un conflicto sin ganador, y viceversa; nunca se confunden.
    """
    if not isinstance(texto_de_corrida, str):
        return False, False, False
    fallo = _MARCA_DE_FALLO_PARCIAL in texto_de_corrida
    sin_ganador = _MARCA_DE_CONFLICTO_SIN_GANADOR in texto_de_corrida
    verificable = _MARCA_DE_CONFLICTO_VERIFICABLE in texto_de_corrida
    return fallo, sin_ganador, verificable
