"""PathResolutionService — resolución stateless de rutas MO2/Skyrim.

Extrae la lógica de resolución de rutas desde :class:`SupervisorAgent`
en un servicio inyectable que aplica el Principio de Inversión de
Dependencias (DIP).  Todas las validaciones pasan por
:class:`~sky_claw.app.security.path_validator.PathValidator` para
garantizar la salvaguarda contra Path Traversal (CRIT-003).

Principio EAFP: se usa ``pathlib.Path.resolve(strict=True)`` en lugar
de ``exists()`` para mitigar TOCTOU en la detección de rutas.

Parte del Sprint 1.5: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sky_claw.app.core.contracts import PathValidatorProtocol
from sky_claw.app.security.path_validator import assert_safe_component

logger = logging.getLogger("SkyClaw.PathResolution")
security_logger = logging.getLogger("SkyClaw.Security")

#: Perfil MO2 cuando no hay ni perfil inyectado ni ``MO2_PROFILE``.
PERFIL_MO2_POR_DEFECTO = "Default"


def resolver_perfil_activo(profile_name: str | None) -> str:
    """Perfil MO2 efectivo dado un perfil inyectado (o su ausencia).

    Precedencia: **perfil inyectado → ``MO2_PROFILE`` → ``Default``**.

    El orden importa y estaba invertido: ``get_active_profile`` leía la variable
    de entorno ANTES del perfil inyectado, así que con ``--profile Requiem`` y
    ``MO2_PROFILE=Default`` a la vez, ``AppContext`` resolvía ``Requiem`` para las
    tools del agente y este resolver devolvía ``Default`` para LOOT, DynDOLOD,
    Pandora, Wrye Bash y Synthesis — la misma divergencia GUI↔agente, sobreviviendo
    a que el perfil se inyectara correctamente.

    Un perfil inyectado gana porque ``AppContext._resolve_mo2_profile`` YA consultó
    ``MO2_PROFILE`` al producirlo: volver a leer el entorno acá no agrega una fuente,
    pisa una decisión que ya se tomó con la precedencia correcta. Sin perfil
    inyectado (resolvers standalone, tests) el entorno sigue siendo la fuente.

    "Sin perfil inyectado" se evalúa por **truthiness**, no por ``is None``: la cadena
    vacía cuenta como ausencia. Es la misma prueba que hace
    ``AppContext._resolve_mo2_profile`` (``cli_profile or entorno or Default``), y las
    dos tienen que coincidir o vuelve la divergencia que este resolver vino a cerrar.
    No es hipotético: ``--profile`` declara ``default=""`` en ``__main__.py``, así que
    un caller que enhebre el valor crudo del CLI inyectaría ``""`` — con ``is None``
    eso devolvía ``""`` y los runners recibían un perfil inexistente en vez del
    fallback (hallazgo de review de Qodo, PR #460).

    **La validación vive acá, no en cada caller.** El valor que sale de esta función
    termina en rutas ``profiles/<perfil>/…`` (LOOT, DynDOLOD, Pandora, Wrye Bash) y en
    el argumento ``--profile`` del CLI de Synthesis. ``AppContext._resolve_mo2_profile``
    y ``AsyncToolRegistry.__init__`` ya validaban su entrada, pero ``MO2_PROFILE``
    como ÚNICA fuente (resolver standalone, supervisor sin perfil inyectado) llegaba
    crudo: un segundo punto de entrada que evadía la primitiva que este PR centraliza
    (hallazgo de review de Qodo, PR #460). Validar acá es el mismo argumento que el de
    la precedencia — la propiedad pertenece a quien PRODUCE el valor, no a quien lo
    consume, o vuelve a haber un camino sin cubrir.

    Fail-closed a propósito: un perfil malformado lanza en vez de degradar al
    fallback. Degradar en silencio operaría sobre un perfil distinto del pedido, que
    es exactamente la clase de defecto que este resolver cierra.

    Lanza:
        PathViolationError: si el perfil resuelto no es un componente de ruta seguro.
    """
    resuelto = profile_name or os.environ.get("MO2_PROFILE", "") or PERFIL_MO2_POR_DEFECTO
    return assert_safe_component(resuelto, field="profile")


# Rutas candidatas para auto-detección de MO2 (ordenadas por probabilidad).
_CANDIDATE_MO2_PATHS: tuple[str, ...] = (
    r"C:\Modding\MO2",
    r"D:\Modding\MO2",
    r"E:\Modding\MO2",
    r"C:\MO2Portable",
    r"D:\MO2Portable",
    r"C:\Games\MO2",
    r"D:\Games\MO2",
)

_CANDIDATE_PF_PATHS: tuple[str, ...] = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
)

# ---------------------------------------------------------------------------
# Metadata de instancia MO2 (ModOrganizer.ini)
# ---------------------------------------------------------------------------
# MO2 ≥ 2.4 separa la INSTALACIÓN del programa (ModOrganizer.exe) de los DATOS
# de la instancia: el INI de la instancia declara [Settings] base_directory y
# los datos pueden vivir en otro disco o árbol. La semántica de cada clave está
# verificada contra el código fuente de MO2 (src/settings.cpp, PathSettings):
#
#   - base()  = [Settings] base_directory; si la clave falta, el directorio
#               donde vive el propio ModOrganizer.ini (compat portable).
#   - mods()  = [Settings] mod_directory; si falta, "%BASE_DIR%/mods", y el
#               literal %BASE_DIR% se sustituye por base() (resolve()).
#
# Las claves se escriben con separadores "/" (Qt INI). gamePath y
# selected_profile van serializados como @ByteArray (Qt) y NO se usan acá:
# base_directory/mod_directory son texto plano. Un mod_directory vacío cuenta
# como ausente — MO2 mismo borra la clave al vaciarla (setConfigurablePath).
_SECCION_SETTINGS_DE_MO2 = "Settings"
_CLAVE_BASE_DIRECTORY_DE_MO2 = "base_directory"
_CLAVE_MOD_DIRECTORY_DE_MO2 = "mod_directory"
_VAR_BASE_DIR_DE_MO2 = "%BASE_DIR%"


def _leer_seccion_ini(texto: str, seccion: str) -> dict[str, str]:
    """Extrae ``clave=valor`` de una sola sección de un INI estilo Qt.

    Barrido manual a propósito: MO2 puede escribir secciones con valores
    binarios (@ByteArray) o líneas que un parser completo (configparser)
    rechazaría, y este resolver solo necesita las claves de texto plano de
    ``[Settings]``. Cualquier línea malformada fuera de la sección se ignora;
    dentro de la sección, una clave repetida gana la última (como QSettings).
    """
    valores: dict[str, str] = {}
    seccion_actual: str | None = None
    for linea_bruta in texto.splitlines():
        linea = linea_bruta.strip().lstrip("\ufeff").strip()
        if not linea or linea.startswith(";") or linea.startswith("#"):
            continue
        if linea.startswith("[") and linea.endswith("]"):
            nombre = linea[1:-1].strip()
            if nombre == seccion:
                seccion_actual = seccion
                continue
            if seccion_actual == seccion:
                break
            continue
        if seccion_actual != seccion or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        valores[clave.strip()] = valor.strip().strip('"')
    return valores


class _ModsDirUnavailableSentinel:
    """Señal opaca de que la instancia declaró un MODS_DIR pero no es resoluble."""

    def __repr__(self) -> str:
        return "<MODS_DIR_UNAVAILABLE>"


MODS_DIR_UNAVAILABLE = _ModsDirUnavailableSentinel()


def ini_declara_mod_directory(contenido_ini: str) -> bool:
    """True si la sección [Settings] del INI declara explícitamente `mod_directory`."""
    valores = _leer_seccion_ini(contenido_ini, _SECCION_SETTINGS_DE_MO2)
    return _CLAVE_MOD_DIRECTORY_DE_MO2 in valores


@dataclass(frozen=True, slots=True)
class MetadataInstanciaMO2:
    """Raíz de datos de la instancia MO2 según su ``ModOrganizer.ini``.

    ``raiz_datos`` es ``base_directory`` (o el directorio del INI en portable).
    ``mods`` es ``mod_directory`` (con ``%BASE_DIR%`` expandido) o
    ``raiz_datos/mods``. ``origen`` describe la fuente: ``"portable"`` (INI
    junto al ejecutable), ``"global"`` (única instancia bajo
    ``%LOCALAPPDATA%\\ModOrganizer``) o ``"mo2_path_datos"`` (deferencia ante
    un ``MO2_PATH`` explícito con semántica de datos).
    ``mod_directory_declarado`` indica si ``mod_directory`` estuvo explícitamente
    presente en la sección ``[Settings]`` del INI.
    """

    raiz_datos: pathlib.Path
    mods: pathlib.Path
    origen: str
    mod_directory_declarado: bool = False


def parsear_metadata_instancia_mo2(
    contenido_ini: str,
    directorio_del_ini: pathlib.Path,
) -> MetadataInstanciaMO2 | None:
    """Interpreta ``ModOrganizer.ini`` y devuelve la metadata de la instancia.

    Contrato ``PathSettings`` de MO2 (verificado contra ``src/settings.cpp``):
    ``base = [Settings] base_directory`` o ``directorio_del_ini``; ``mods =
    [Settings] mod_directory`` (con ``%BASE_DIR%`` expandido) o ``base/mods``;
    un valor no absoluto → fail-closed (``None``). Sin metadatos utilizables
    también devuelve ``None``.
    """
    valores = _leer_seccion_ini(contenido_ini, _SECCION_SETTINGS_DE_MO2)
    base_str = valores.get(_CLAVE_BASE_DIRECTORY_DE_MO2) or str(directorio_del_ini)
    custom_mods = _CLAVE_MOD_DIRECTORY_DE_MO2 in valores
    mods_str = valores.get(_CLAVE_MOD_DIRECTORY_DE_MO2) or f"{base_str}/mods"
    if _VAR_BASE_DIR_DE_MO2 in mods_str:
        mods_str = mods_str.replace(_VAR_BASE_DIR_DE_MO2, base_str)
    base = pathlib.Path(base_str)
    mods = pathlib.Path(mods_str)
    if not mods.is_absolute() or not base.is_absolute():
        return None
    return MetadataInstanciaMO2(
        raiz_datos=base,
        mods=mods,
        origen="portable",
        mod_directory_declarado=custom_mods,
    )


def resolver_mods_dir_de_instancia_mo2(
    contenido_ini: str,
    directorio_del_ini: pathlib.Path,
) -> pathlib.Path | None:
    """Directorio ``mods`` declarado por la metadata de la instancia MO2.

    Wrapper delgado sobre :func:`parsear_metadata_instancia_mo2` para
    compatibilidad con tests previos: la misma semántica del contrato
    ``PathSettings`` (mod_directory → ``%BASE_DIR%/mods`` → ``base/mods`` →
    ``<ini_dir>/mods``). Devuelve ``None`` ante resultado no absoluto.
    """
    metadata = parsear_metadata_instancia_mo2(contenido_ini, directorio_del_ini)
    return metadata.mods if metadata is not None else None


def _raiz_de_instancias_globales() -> pathlib.Path | None:
    """Raíz de instancias globales de MO2 (``%LOCALAPPDATA%/ModOrganizer``)."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    return pathlib.Path(local_app_data) / "ModOrganizer"


def mo2_path_apunta_a_datos(install_dir: pathlib.Path | None) -> bool:
    """``True`` si ``MO2_PATH`` explícito apunta a DATOS y no a una instalación.

    Un operador puede configurar ``MO2_PATH`` con el directorio de datos de su
    instancia (el que contiene ``mods/``, sin ``ModOrganizer.exe``) o con un
    contenedor legacy; esa configuración funcionaba vía ``<MO2_PATH>/mods``
    antes de la resolución por metadata y sigue siendo la declaración más
    explícita disponible. Cuando el directorio contiene el ejecutable
    (instalación), la metadata de la instancia manda aunque exista un
    ``<exe>/mods`` sobrante (el caso que este PR corrige).
    """
    if not os.environ.get("MO2_PATH", "") or install_dir is None:
        return False
    if (install_dir / "ModOrganizer.exe").is_file():
        return False
    return (install_dir / "mods").is_dir()


def descubrir_metadata_instancia_mo2(
    install_dir: pathlib.Path | None,
) -> MetadataInstanciaMO2 | None:
    """Descubre la instancia MO2 activa y devuelve su metadata sin validar sandbox.

    **Trust boundary explícito**: esta función SOLO deriva la raíz de datos
    de la instancia desde (a) el INI portable junto al ejecutable
    configurado/detectado, (b) un ``MO2_PATH`` explícito con semántica de
    datos, o (c) el único INI de instancia global bajo
    ``%LOCALAPPDATA%\\ModOrganizer``. Las raíces devueltas están
    canonicalizadas (``resolve()``) y son absolutas — la confianza es de
    configuración del operador/máquina (la misma clase que ``MO2_PATH`` o
    ``mo2_root``). Los callers deben validarlas contra su sandbox antes de
    operar sobre ellas; aquí se devuelven tal cual para que la capa de
    ``AppContext`` las pueda registrar como raíces explícitas con un log
    trazable.

    Devuelve ``None`` cuando no hay evidencia de instancia (no hay INI
    aplicable y no hay ``MO2_PATH`` de datos). Ante evidencia presente pero
    inválida (INI ilegible, base_directory relativo, scan de instancias
    abortado, varias instancias globales sin criterio) levanta
    ``RuntimeError`` con la evidencia: degradar al ``<exe>/mods`` legacy con
    evidencia en contra sería exactamente el defecto que este PR cierra.
    """
    # 1. Portable: el INI vive junto al ejecutable configurado o detectado.
    if install_dir is not None:
        ini_portable = install_dir / "ModOrganizer.ini"
        if ini_portable.is_file():
            try:
                contenido = ini_portable.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                raise RuntimeError(
                    f"No se pudo leer el ModOrganizer.ini portable en {ini_portable}: "
                    f"{exc}. Configure MO2_MODS_PATH o MO2_PATH con la ubicación correcta."
                ) from exc
            metadata = parsear_metadata_instancia_mo2(contenido, ini_portable.parent)
            if metadata is None:
                raise RuntimeError(
                    f"El ModOrganizer.ini portable ({ini_portable}) declara un "
                    f"directorio de mods o base no absoluto o vacío. Configure "
                    f"MO2_MODS_PATH o MO2_PATH con la ubicación correcta."
                )
            return MetadataInstanciaMO2(
                raiz_datos=_canonicalizar(metadata.raiz_datos),
                mods=_canonicalizar(metadata.mods),
                origen="portable",
                mod_directory_declarado=metadata.mod_directory_declarado,
            )

    # 2. Deferencia: un MO2_PATH explícito con semántica de datos (sin exe,
    # con mods/ resoluble) es la declaración del operador y funcionaba vía
    # <MO2_PATH>/mods antes de la resolución por metadata. Se respeta antes
    # de cualquier escaneo global, lo que también evita que un escaneo
    # accidental lea el LOCALAPPDATA real de la máquina del operador.
    if mo2_path_apunta_a_datos(install_dir):
        assert install_dir is not None
        return MetadataInstanciaMO2(
            raiz_datos=_canonicalizar(install_dir),
            mods=_canonicalizar(install_dir / "mods"),
            origen="mo2_path_datos",
            mod_directory_declarado=False,
        )

    # 3. Instancia global: %LOCALAPPDATA%/ModOrganizer/<instancia>/ModOrganizer.ini
    raiz = _raiz_de_instancias_globales()
    if raiz is None:
        return None
    candidatas: list[pathlib.Path] = []
    try:
        for hijo in raiz.iterdir():
            try:
                es_instancia = hijo.is_dir() and (hijo / "ModOrganizer.ini").is_file()
            except OSError:
                continue
            if es_instancia:
                candidatas.append(hijo)
    except OSError as exc:
        if raiz.is_dir():
            raise RuntimeError(
                f"No se pudo escanear la raíz de instancias globales de MO2 "
                f"{raiz}: {exc}. Configure MO2_MODS_PATH o MO2_PATH con la "
                f"ubicación correcta."
            ) from exc
        return None
    if not candidatas:
        return None
    if len(candidatas) > 1:
        nombres = ", ".join(sorted(h.name for h in candidatas))
        raise RuntimeError(
            f"Hay {len(candidatas)} instancias globales de MO2 bajo {raiz} "
            f"({nombres}) y ninguna forma de elegir la activa. Configure "
            f"MO2_MODS_PATH o MO2_PATH apuntando a la instancia correcta."
        )
    ini_global = candidatas[0] / "ModOrganizer.ini"
    try:
        contenido = ini_global.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise RuntimeError(
            f"No se pudo leer el ModOrganizer.ini de la instancia global en "
            f"{ini_global}: {exc}. Configure MO2_MODS_PATH o MO2_PATH con la "
            f"ubicación correcta."
        ) from exc
    metadata = parsear_metadata_instancia_mo2(contenido, ini_global.parent)
    if metadata is None:
        raise RuntimeError(
            f"El ModOrganizer.ini global ({ini_global}) declara un directorio "
            f"de mods o base no absoluto o vacío. Configure MO2_MODS_PATH o "
            f"MO2_PATH con la ubicación correcta."
        )
    return MetadataInstanciaMO2(
        raiz_datos=_canonicalizar(metadata.raiz_datos),
        mods=_canonicalizar(metadata.mods),
        origen="global",
        mod_directory_declarado=metadata.mod_directory_declarado,
    )


def _canonicalizar(path: pathlib.Path) -> pathlib.Path:
    """``resolve()`` no estricto para canonicalizar antes de validar.

    ``resolve(strict=False)`` normaliza separadores, colapsa ``..`` con
    segmentos existentes y sigue symlinks/junctions sin exigir que el path
    exista. La validación contra el sandbox y la existencia la hacen los
    callers; aquí solo canonicalizamos el resultado del INI antes de que
    ``PathValidator`` lo compare contra raíces también canonicalizadas.
    """
    return path.resolve(strict=False)


@runtime_checkable
class PathResolver(Protocol):
    """Interfaz abstracta para resolución de rutas MO2/Skyrim.

    Aplica DIP: ``SupervisorAgent`` depende de esta abstracción,
    no de la implementación concreta ``PathResolutionService``.
    """

    def validate_env_path(self, path_str: str, var_name: str) -> pathlib.Path | None:
        """Valida un path de variable de entorno con PathValidator.

        Args:
            path_str: String del path a validar.
            var_name: Nombre de la variable de entorno (para logging).

        Returns:
            Path validado o ``None`` si la validación falla.
        """
        ...

    def detect_mo2_path(self) -> pathlib.Path | None:
        """Auto-detecta la ruta de instalación de MO2 (EAFP anti-TOCTOU).

        Returns:
            Path validado al directorio de MO2, o ``None`` si no se detecta.
        """
        ...

    def resolve_modlist_path(self, profile: str) -> pathlib.Path:
        """Resuelve la ruta al ``modlist.txt`` para un perfil MO2.

        Prioridad: ``MO2_PATH`` env → auto-detección → fallback WSL2.

        Args:
            profile: Nombre del perfil MO2.

        Returns:
            Path al ``modlist.txt`` del perfil.

        Raises:
            RuntimeError: Si ninguna ruta puede ser resuelta y validada.
        """
        ...

    def get_mo2_mods_path(self) -> pathlib.Path:
        """Obtiene la ruta al directorio ``mods`` de la instancia MO2 activa.

        Precedencia: ``MO2_MODS_PATH`` → metadata de la instancia
        (``ModOrganizer.ini``: ``base_directory``/``mod_directory``) →
        legacy ``<instalación>/mods`` → ``RuntimeError``.
        La instalación del programa (``ModOrganizer.exe``) y los datos de la
        instancia pueden vivir en discos distintos.

        Returns:
            Path validado al directorio de mods.

        Raises:
            RuntimeError: Si no se puede resolver y validar la ruta, o si la
                metadata de la instancia es inconsistente (fail-closed).
        """
        ...

    def get_active_profile(self) -> str:
        """Obtiene el nombre del perfil activo de MO2.

        Returns:
            Nombre del perfil activo o ``'Default'`` si no se puede determinar.
        """
        ...

    def get_mo2_mods_path_best_effort(
        self,
    ) -> pathlib.Path | _ModsDirUnavailableSentinel | None:
        """MODS_DIR declarado de la instancia, señal opaca de no-resoluble, o ``None``."""
        ...


class PathResolutionService:
    """Implementación stateless de :class:`PathResolver`.

    Recibe ``PathValidator`` por inyección para garantizar la salvaguarda
    contra Path Traversal (CRIT-003) en todas las resoluciones.

    Args:
        path_validator: Instancia de ``PathValidator`` configurada con
            las raíces del sandbox.
        profile_name: Perfil MO2 de la sesión. Si se inyecta, **manda** sobre
            ``MO2_PROFILE`` (ver :func:`resolver_perfil_activo`). ``None`` deja que
            el entorno decida, para resolvers standalone y tests.
        mo2_install_dir: Instalación MO2 (directorio de ``ModOrganizer.exe``) ya
            seleccionada por el composition root (``AppContext``). Si se inyecta,
            **manda** sobre ``MO2_PATH``/auto-detección en
            :meth:`_directorio_instalacion_mo2`: el resolver reutiliza esa
            decisión en vez de volver a elegir una instalación por su cuenta
            (evita el split-brain instalación-seleccionada ≠ instalación-usada;
            la instalación es la que localiza el ``ModOrganizer.ini`` portable).
            ``None`` mantiene el comportamiento legacy (standalone, tests, y
            callers sin composition root).
    """

    def __init__(
        self,
        path_validator: PathValidatorProtocol,
        profile_name: str | None = None,
        *,
        mo2_install_dir: pathlib.Path | None = None,
    ) -> None:
        self._path_validator = path_validator
        self._profile_name = profile_name
        self._mo2_install_dir = mo2_install_dir

    def validate_env_path(self, path_str: str, var_name: str) -> pathlib.Path | None:
        """Valida un path de variable de entorno con PathValidator.

        CRIT-003: Mitigación para variables de entorno sin validación.

        Args:
            path_str: String del path a validar.
            var_name: Nombre de la variable de entorno (para logging).

        Returns:
            Path validado o ``None`` si la validación falla.
        """
        if not path_str:
            return None

        try:
            validated_path = self._path_validator.validate(path_str)
            return validated_path
        except Exception as exc:
            security_logger.warning(
                "%s inválido (posible intento de path traversal): %s - Error: %s",
                var_name,
                path_str,
                exc,
            )
            return None

    def detect_mo2_path(self) -> pathlib.Path | None:
        """Auto-detecta la ruta de instalación de MO2 usando EAFP.

        Reemplaza el anti-patrón ``if path.exists(): return path`` por
        ``Path.resolve(strict=True)`` dentro de bloques try/except para
        mitigar TOCTOU.  Cada ruta candidata exitosa se valida
        inmediatamente con ``PathValidator``.

        Returns:
            Path validado al directorio de MO2, o ``None`` si no se detecta.
        """
        # Fase 1: Rutas hardcodeadas comunes
        for raw in _CANDIDATE_MO2_PATHS:
            candidate = pathlib.Path(raw) / "ModOrganizer.exe"
            try:
                resolved_exe = candidate.resolve(strict=True)
                # Validar el directorio padre (directorio de MO2)
                validated = self._path_validator.validate(resolved_exe.parent)
                logger.debug(
                    "MO2 auto-detectado en ruta candidata: %s",
                    validated,
                )
                return validated
            except (FileNotFoundError, OSError) as exc:
                logger.debug(
                    "MO2 candidate path falló resolución: %s — %s",
                    raw,
                    exc,
                )
                continue
            except Exception:
                logger.error(
                    "MO2 candidate path falló validación de seguridad: %s",
                    raw,
                    exc_info=True,
                    extra={
                        "component": "PathResolutionService",
                        "operation": "detect_mo2_path",
                    },
                )
                continue

        # Fase 2: LOCALAPPDATA/ModOrganizer
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            md = pathlib.Path(local_app_data) / "ModOrganizer"
            try:
                resolved_md = md.resolve(strict=True)
            except (FileNotFoundError, OSError):
                resolved_md = None

            if resolved_md is not None:
                try:
                    for child in resolved_md.iterdir():
                        if not child.is_dir():
                            continue
                        exe_candidate = child / "ModOrganizer.exe"
                        try:
                            resolved_exe = exe_candidate.resolve(strict=True)
                            validated = self._path_validator.validate(
                                resolved_exe.parent,
                            )
                            logger.debug(
                                "MO2 auto-detectado en LOCALAPPDATA: %s",
                                validated,
                            )
                            return validated
                        except (FileNotFoundError, OSError):
                            continue
                        except Exception:
                            logger.error(
                                "MO2 LOCALAPPDATA child falló validación: %s",
                                child,
                                exc_info=True,
                                extra={
                                    "component": "PathResolutionService",
                                    "operation": "detect_mo2_path",
                                },
                            )
                            continue
                except OSError as exc:
                    logger.error(
                        "Error iterando LOCALAPPDATA/ModOrganizer: %s",
                        exc,
                        exc_info=True,
                        extra={
                            "component": "PathResolutionService",
                            "operation": "detect_mo2_path",
                        },
                    )

        # Fase 3: Program Files
        for pf_raw in _CANDIDATE_PF_PATHS:
            candidate = pathlib.Path(pf_raw) / "Mod Organizer 2" / "ModOrganizer.exe"
            try:
                resolved_exe = candidate.resolve(strict=True)
                validated = self._path_validator.validate(resolved_exe.parent)
                logger.debug(
                    "MO2 auto-detectado en Program Files: %s",
                    validated,
                )
                return validated
            except (FileNotFoundError, OSError):
                continue
            except Exception:
                logger.error(
                    "MO2 Program Files candidate falló validación: %s",
                    pf_raw,
                    exc_info=True,
                    extra={
                        "component": "PathResolutionService",
                        "operation": "detect_mo2_path",
                    },
                )
                continue

        logger.warning("Auto-detección de MO2 falló — ninguna ruta candidata válida")
        return None

    def _directorio_instalacion_mo2(self) -> pathlib.Path | None:
        """Instalación MO2 conocida (directorio del ejecutable), o ``None``.

        Precedencia:

        1. **Instalación inyectada** (``mo2_install_dir``): la que el
           composition root (``AppContext``) ya seleccionó y con la que armó el
           sandbox. Gana sobre el entorno porque la decisión ya está tomada;
           así el ``ModOrganizer.ini`` portable que se lee —y por ende la
           instancia y sus ``mods/``/``profiles/``— cuelga de esta ruta y no de
           una instalación distinta que ``MO2_PATH``/la auto-detección pudieran
           elegir. Se valida contra el sandbox igual que ``MO2_PATH``. Si la
           inyección NO valida, **no** se degrada al entorno: eso reintroduciría
           el split-brain que la inyección cierra. Se sigue como "sin
           instalación conocida" (``None``) y la resolución por metadata
           global/fail-closed decide con la evidencia.
        2. ``MO2_PATH`` validado (standalone/legacy, sin composition root).
        3. :meth:`detect_mo2_path` (auto-detección).

        Es solo una *pista de instalación*: desde MO2 2.4 el directorio del
        ejecutable NO implica que ``mods/`` cuelgue de él — la instancia lo
        declara en su ``ModOrganizer.ini``.
        """
        if self._mo2_install_dir is not None:
            validado = self.validate_env_path(str(self._mo2_install_dir), "mo2_install_dir")
            if validado is not None and self._es_directorio_real(validado, "mo2_install_dir"):
                return validado
            # Sin warning adicional: validate_env_path ya reportó el rechazo de
            # contención, y el cierre del capability gate va en debug para no
            # duplicar ruido por llamada. No se degrada a MO2_PATH/auto-detección
            # para no reintroducir el split-brain instalación-seleccionada !=
            # instalación-usada.
            logger.debug(
                "Instalación MO2 inyectada no utilizable (%s); sin degradar a MO2_PATH/auto-detección.",
                self._mo2_install_dir,
            )
            return None
        mo2_path_str = os.environ.get("MO2_PATH", "")
        if mo2_path_str:
            validado = self.validate_env_path(mo2_path_str, "MO2_PATH")
            if validado is not None and self._es_directorio_real(validado, "MO2_PATH"):
                return validado
            logger.debug(
                "MO2_PATH no utilizable como instalación; se intenta auto-detección.",
            )
        return self.detect_mo2_path()

    @staticmethod
    def _es_directorio_real(path: pathlib.Path, etiqueta: str) -> bool:
        """Capability gate: un hint/instalación solo existe si es un directorio real.

        :meth:`PathValidator.validate` verifica contención y symlinks con
        ``resolve()`` no estricto, así que un path fantasma o un archivo dentro
        de una raíz permitida validaría igual. Sin este gate, un hint inexistente
        abriría todos los ``is not None`` de capability (preflights, runners,
        sandbox) sobre una instalación que no está ahí.

        No se exige ``ModOrganizer.exe``: ``MO2_PATH`` admite por contrato la
        semántica de datos (directorio con ``mods/`` sin ejecutable,
        ``mo2_path_apunta_a_datos``), los resolvers standalone/herméticos operan
        sobre directorios sin exe, y la presencia del ejecutable la decide el
        composition root (broker F8) — no este getter.
        """
        try:
            es_dir = path.is_dir()
        except OSError as exc:
            logger.debug("%s validado pero no inspeccionable (%s); fail-closed.", etiqueta, exc)
            return False
        if not es_dir:
            logger.debug("%s validado pero no es un directorio (%s); fail-closed.", etiqueta, path)
            return False
        return True

    def _metadata_de_instancia(
        self,
        install_dir: pathlib.Path | None,
    ) -> MetadataInstanciaMO2 | None:
        """Descubre la metadata de la instancia y la valida contra el sandbox.

        Devuelve la metadata canonicalizada con ``raiz_datos`` y ``mods``
        validados (ambos deben caer dentro de las raíces permitidas: la base
        porque es la raíz de la que cuelgan ``profiles/`` y ``mods/``; el
        directorio de mods porque es contra lo que operará cada tool).
        ``None`` cuando no hay evidencia de instancia: el caller puede seguir
        al comportamiento legacy.
        """
        try:
            metadata = descubrir_metadata_instancia_mo2(install_dir)
        except RuntimeError:
            raise
        if metadata is None:
            return None
        try:
            raiz_validada = self._path_validator.validate(metadata.raiz_datos)
            mods_validado = self._path_validator.validate(metadata.mods)
        except Exception as exc:
            security_logger.warning(
                "Metadata de la instancia MO2 (raíz=%s, mods=%s) rechazada por "
                "PathValidator: %s — el sandbox debe incluir la base de la "
                "instancia o el operador debe configurar MO2_PATH/MO2_MODS_PATH.",
                metadata.raiz_datos,
                metadata.mods,
                exc,
            )
            raise RuntimeError(
                f"El directorio de la instancia MO2 (raíz={metadata.raiz_datos}, "
                f"mods={metadata.mods}) queda fuera de las raíces permitidas del "
                f"sandbox. Verifique que la base de la instancia esté registrada "
                f"o configure MO2_MODS_PATH o MO2_PATH con la ubicación correcta."
            ) from exc
        return MetadataInstanciaMO2(
            raiz_datos=raiz_validada,
            mods=mods_validado,
            origen=metadata.origen,
            mod_directory_declarado=metadata.mod_directory_declarado,
        )

    @staticmethod
    def _exigir_directorio_mods(resolved: pathlib.Path, contexto: str) -> pathlib.Path:
        """Fail-closed si el path existe pero no es un directorio (mods obligatorio)."""
        if not resolved.is_dir():
            raise RuntimeError(
                f"La ruta de mods de MO2 ({resolved}) existe pero no es un "
                f"directorio ({contexto}). Verifique la configuración de la "
                f"instancia o MO2_MODS_PATH/MO2_PATH."
            )
        return resolved

    def resolve_modlist_path(self, profile: str) -> pathlib.Path:
        """Resuelve ``profiles/<profile>/modlist.txt`` desde la MISMA instancia
        de datos que :meth:`get_mo2_mods_path` (sin split-brain).

        El split-brain pre-PR era: ``get_mo2_mods_path`` derivaba del INI
        mientras ``resolve_modlist_path`` seguía usando ``MO2_PATH/profiles``
        (potencialmente otro árbol). Ahora ambos derivan de
        :func:`descubrir_metadata_instancia_mo2` — la misma raíz de datos
        proporciona tanto ``mods/`` como ``profiles/<profile>/``.

        Precedencia:
        1. **Metadata de la instancia** (misma función que
           :meth:`get_mo2_mods_path`): portable junto al exe, deferencia por
           ``MO2_PATH`` explícito de datos, o única instancia global. El
           ``modlist.txt`` se construye como
           ``raiz_datos/profiles/<profile>/modlist.txt`` y se valida contra
           el sandbox (CRIT-003: ``profile`` no puede escapar la raíz).
        2. Sin evidencia de instancia: ``<MO2_PATH>/profiles/<profile>/modlist.txt``
           (legacy, preservado para compat portable y ``MO2_PATH`` de datos).
        3. Fallback WSL2 ``/mnt/c/Modding/MO2`` (validado).

        Args:
            profile: Nombre del perfil MO2 (inmutable para el caller).

        Returns:
            Path al ``modlist.txt`` del perfil dentro de una raíz del sandbox.

        Raises:
            RuntimeError: Si la metadata es inválida, si el ``modlist.txt``
                construido escapa las raíces del sandbox, o si ninguna ruta
                puede resolverse y validarse.
        """
        install_dir = self._directorio_instalacion_mo2()
        metadata = self._metadata_de_instancia(install_dir)
        if metadata is not None:
            modlist = metadata.raiz_datos / "profiles" / profile / "modlist.txt"
            try:
                return self._path_validator.validate(modlist)
            except Exception as exc:
                security_logger.warning(
                    "modlist (%s) rechazado por PathValidator: %s",
                    modlist,
                    exc,
                )
                raise RuntimeError(
                    f"La ruta de modlist ({modlist}) queda fuera de las raíces "
                    f"permitidas del sandbox. Verifique que la base de la "
                    f"instancia esté registrada o configure MO2_PATH."
                ) from exc

        # 2. Legacy portable (sin metadata de instancia): compat con configs
        # explícitas que solo setean MO2_PATH sin INI cerca.
        if install_dir is not None:
            try:
                return self._path_validator.validate(install_dir / "profiles" / profile / "modlist.txt")
            except Exception as exc:
                logger.debug(
                    "MO2/profiles legacy inválido: %s — %s",
                    install_dir / "profiles" / profile,
                    exc,
                )

        # 3. Fallback: WSL2 default path — también validado
        fallback_path = pathlib.Path("/mnt/c/Modding/MO2") / "profiles" / profile / "modlist.txt"
        try:
            validated_fallback = self._path_validator.validate(fallback_path)
            logger.warning(
                "MO2_PATH no configurado y auto-detección/metadata falló para "
                "perfil '%s'. Usando fallback WSL2 validado: %s. Configure la "
                "variable de entorno MO2_PATH para evitar este aviso.",
                profile,
                validated_fallback,
            )
            return validated_fallback
        except Exception as exc:
            logger.error(
                "Fallback WSL2 también falló validación para perfil '%s': %s",
                profile,
                exc,
                exc_info=True,
                extra={
                    "component": "PathResolutionService",
                    "operation": "resolve_modlist_path",
                },
            )
            raise RuntimeError(
                f"No se pudo resolver ni validar la ruta de modlist para el "
                f"perfil '{profile}'. Configure MO2_PATH en las variables de "
                f"entorno o verifique la instalación de MO2."
            ) from exc

    def get_mo2_mods_path(self) -> pathlib.Path:
        """Obtiene la ruta al directorio ``mods`` de la instancia MO2 activa.

        Precedencia explícita (cada paso usa EAFP y validación con
        PathValidator):

        1. ``MO2_MODS_PATH``: override explícito del operador (contrato
           vigente; gana siempre que sea válido, exista y sea un directorio).
        2. **Metadata de la instancia MO2** (misma fuente que
           :meth:`resolve_modlist_path`): ``ModOrganizer.ini`` portable junto
           al exe, deferencia por ``MO2_PATH`` explícito de datos, o única
           instancia global bajo ``%LOCALAPPDATA%\\ModOrganizer``. La raíz de
           datos y el directorio de mods se validan contra el sandbox (la base
           de la instancia debe estar registrada como raíz; ver
           ``AppContext._construir_raices_sandbox``). Si la metadata existe
           pero es inválida (INI ilegible, path relativo, fuera del sandbox,
           directorio inexistente o que no es directorio, varias instancias
           globales), falla cerrado: NO degrada a ``<instalación>/mods`` con
           evidencia en contra.
        3. Comportamiento legacy/portable (solo sin metadata de instancia):
           ``<instalación>/mods``, donde la instalación es ``MO2_PATH``
           validado o la auto-detección. También debe existir y ser un
           directorio; un archivo en esa ruta es fail-closed.
        4. ``RuntimeError`` fail-closed.

        Returns:
            Path validado al directorio de mods.

        Raises:
            RuntimeError: Si ninguna ruta puede ser resuelta y validada, o si
                la metadata de la instancia es inconsistente (incluye
                ``ModOrganizer.ini`` que apunta a un archivo en vez de un
                directorio).
        """
        # 1. Override explícito: MO2_MODS_PATH
        mo2_mods_path_str = os.environ.get("MO2_MODS_PATH", "")
        if mo2_mods_path_str:
            validated_path = self.validate_env_path(mo2_mods_path_str, "MO2_MODS_PATH")
            if validated_path is None:
                raise RuntimeError(f"MO2_MODS_PATH ({mo2_mods_path_str}) falló la validación de seguridad.")
            try:
                resolved = validated_path.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(f"MO2_MODS_PATH ({validated_path}) no existe o no es accesible.") from exc
            self._exigir_directorio_mods(resolved, "MO2_MODS_PATH")
            return resolved

        # 2. Metadata de la instancia MO2 (instalación != datos de la instancia).
        # La instancia global se descubre desde %LOCALAPPDATA% y no necesita que
        # la instalación del ejecutable sea conocida: se intenta igual sin ella.
        install_dir = self._directorio_instalacion_mo2()
        metadata = self._metadata_de_instancia(install_dir)
        if metadata is not None:
            try:
                resolved = metadata.mods.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(
                    f"La instancia MO2 declara su directorio de mods en "
                    f"{metadata.mods}, pero no existe. Verifique "
                    f"base_directory/mod_directory en el ModOrganizer.ini de "
                    f"la instancia o configure MO2_MODS_PATH."
                ) from exc
            self._exigir_directorio_mods(
                resolved,
                f"metadata de la instancia ({metadata.origen})",
            )
            return resolved

        # 3. Legacy portable (solo sin metadata de instancia): <instalación>/mods.
        # Reutiliza `install_dir` del paso 2: la única copia de "MO2_PATH
        # validado → detect_mo2_path" vive en `_directorio_instalacion_mo2`, así
        # que la auto-detección corre una sola vez por llamada.
        if install_dir is not None:
            mods_path = install_dir / "mods"
            try:
                resolved_mods = mods_path.resolve(strict=True)
            except (FileNotFoundError, OSError):
                logger.debug("MO2/mods legacy no existe: %s", mods_path)
            else:
                self._exigir_directorio_mods(resolved_mods, "legacy MO2_PATH")
                return resolved_mods

        raise RuntimeError(
            "No se pudo detectar la ruta de MO2. Configure MO2_PATH o MO2_MODS_PATH en las variables de entorno."
        )

    def tiene_mods_declarado(self) -> bool:
        """Indica si la instancia o el entorno DECLARARON una ruta de mods.

        True si:
        - ``MO2_MODS_PATH`` está configurado y no vacío en el entorno.
        - O el archivo ``ModOrganizer.ini`` de la instancia declara
          sintácticamente ``mod_directory`` en ``[Settings]`` (incluso si el
          path es inválido, no absoluto o falla validación).

        False cuando no hay declaración explícita y el layout es el default
        ``<base_directory>/mods`` o portable puro sin metadata.
        """
        if bool(os.environ.get("MO2_MODS_PATH", "")):
            return True

        install_dir = self._directorio_instalacion_mo2()
        if install_dir is not None:
            ini_portable = install_dir / "ModOrganizer.ini"
            if ini_portable.is_file():
                try:
                    contenido = ini_portable.read_text(encoding="utf-8-sig", errors="replace")
                    if ini_declara_mod_directory(contenido):
                        return True
                except OSError:
                    return True

        raiz = _raiz_de_instancias_globales()
        if raiz is not None and raiz.is_dir():
            try:
                for hijo in raiz.iterdir():
                    ini_global = hijo / "ModOrganizer.ini"
                    if ini_global.is_file():
                        try:
                            contenido = ini_global.read_text(encoding="utf-8-sig", errors="replace")
                            if ini_declara_mod_directory(contenido):
                                return True
                        except OSError:
                            return True
            except OSError:
                pass

        try:
            metadata = self._metadata_de_instancia(install_dir)
            if metadata is not None and metadata.mod_directory_declarado:
                return True
        except RuntimeError:
            return True

        return False

    def get_mo2_mods_path_best_effort(
        self,
    ) -> pathlib.Path | _ModsDirUnavailableSentinel | None:
        """MODS_DIR declarado de la instancia, señal opaca de no-resoluble, o ``None``.

        Espejo best-effort de :meth:`get_mo2_mods_path` para sensores de
        preflight y previews:
        - Si el directorio de mods resuelve: devuelve el :class:`pathlib.Path`
          validado (:data:`RESOLVED`).
        - Si la instancia DECLARÓ un ``mod_directory`` (o ``MO2_MODS_PATH``)
          pero este no es resoluble o es inválido: devuelve
          :data:`MODS_DIR_UNAVAILABLE` (:data:`DECLARED_BUT_UNAVAILABLE`). Los
          sensores omiten el escaneo y NO degradan al default ``<datos>/mods``
          (evita falso verde sobre un árbol inventado).
        - Si no hay metadata o la instancia no declaró un ``mod_directory``
          custom: devuelve ``None`` (:data:`DEFAULT_ALLOWED`), permitiendo el
          fallback al default histórico ``<datos>/mods`` para layouts portables.
        """
        try:
            mods = self.get_mo2_mods_path()
        except Exception:  # noqa: BLE001 — boundary best-effort declarado
            if self.tiene_mods_declarado():
                logger.debug(
                    "MODS_DIR declarado pero no resoluble (best-effort): "
                    "se emite MODS_DIR_UNAVAILABLE para omitir escaneo.",
                    exc_info=True,
                )
                return MODS_DIR_UNAVAILABLE
            logger.debug(
                "MODS_DIR no resoluble sin declaración custom (best-effort): fallback default permitido.",
                exc_info=True,
            )
            return None
        return mods if isinstance(mods, pathlib.Path) else None

    def get_mo2_mods_path_para_destino(self) -> pathlib.Path | None:
        """MODS_DIR para un destino de ESCRITURA, sin fallback silencioso.

        Semántica write-safe exigida por Synthesis, Grass y AppContext:
        1. ``MO2_MODS_PATH`` explícito válido → devuelve ese :class:`pathlib.Path`.
        2. ``MO2_MODS_PATH`` explícito inválido → ``RuntimeError`` / fail-closed.
        3. Metadata de instancia con ``mod_directory``/``base_directory`` válida
           → MODS_DIR correspondiente.
        4. Metadata declarada pero inconsistente → ``RuntimeError`` / fail-closed.
        5. Ausencia real de metadata Y ausencia de ``MO2_MODS_PATH`` → ``None``
           (:data:`DEFAULT_ALLOWED`), permitiendo al caller usar el default
           histórico (p. ej. ``data_root / "mods"``).
        """
        # 1-2. Override explícito de entorno: MO2_MODS_PATH manda y falla cerrado si es inválido
        if bool(os.environ.get("MO2_MODS_PATH", "")):
            return self.get_mo2_mods_path()

        # 3-4. Metadata de instancia (mod_directory / base_directory)
        install_dir = self._directorio_instalacion_mo2()
        try:
            metadata = self._metadata_de_instancia(install_dir)
        except RuntimeError:
            raise  # caso 4: declarado e inconsistente -> fail-closed

        if metadata is None:
            # 5. Ausencia real de metadata y ausencia de MO2_MODS_PATH -> None
            return None

        # 3. Metadata válida (o falla si el directorio declarado no existe / no es dir)
        return self.get_mo2_mods_path()

    def get_active_profile(self) -> str:
        """Obtiene el nombre del perfil activo de MO2.

        Precedencia en :func:`resolver_perfil_activo`: el perfil inyectado manda
        sobre ``MO2_PROFILE``. Es lo que hace que el perfil de sesión que resolvió
        ``AppContext`` llegue de verdad a LOOT, DynDOLOD, Pandora, Wrye Bash y
        Synthesis, en vez de que el entorno lo pise por debajo.

        Returns:
            Nombre del perfil activo o ``'Default'`` si no se puede determinar.
        """
        return resolver_perfil_activo(self._profile_name)

    # ------------------------------------------------------------------
    # Zero-Trust: resolved tool paths (centralised os.environ access)
    # ------------------------------------------------------------------

    def get_skyrim_path(self) -> pathlib.Path | None:
        """Resuelve SKYRIM_PATH desde entorno validado."""
        return self.validate_env_path(os.environ.get("SKYRIM_PATH", ""), "SKYRIM_PATH")

    def get_mo2_path(self) -> pathlib.Path | None:
        """Resuelve la instalación MO2 configurada/seleccionada (INSTALL ROOT).

        Espeja las ramas 1–2 de :meth:`_directorio_instalacion_mo2` —
        instalación inyectada validada → ``MO2_PATH`` validado— SIN su rama 3
        de auto-detección. Getter ≠ detector (ver :meth:`detect_mo2_path`):
        varios consumidores usan ``get_mo2_path() is not None`` como gate de
        capability (scan de mods en xEdit/LOOT, construcción de preflights),
        así que ``None`` significa "sin instalación configurada", nunca
        "intenta encontrar una" — y la auto-detección escanearía el
        filesystem real en cada llamada.

        Es **solo** el directorio del programa (``ModOrganizer.exe``): desde
        MO2 2.4 los datos de la instancia (``profiles/``, ``overwrite/``,
        ``mods/``) pueden vivir en otro árbol — ver
        :meth:`get_mo2_instance_data_root`. Quien necesite datos de instancia
        no debe derivarlos de acá.

        1. **Instalación inyectada** (``mo2_install_dir``, la que el
           composition root ya seleccionó): se valida contra el sandbox con
           la misma primitiva que ``MO2_PATH`` y debe ser un directorio real
           (:meth:`_es_directorio_real`). Si NO valida, **no** se degrada al
           entorno (reintroduciría el split-brain instalación-seleccionada ≠
           instalación-usada que la inyección cierra): falla cerrado a ``None``.
        2. ``MO2_PATH`` validado (standalone/legacy, sin composition root),
           también con gate de directorio real.

        Returns:
            Path validado a la instalación MO2, o ``None`` si no hay ninguna.
        """
        if self._mo2_install_dir is not None:
            validado = self.validate_env_path(str(self._mo2_install_dir), "mo2_install_dir")
            if validado is not None and self._es_directorio_real(validado, "mo2_install_dir"):
                return validado
            # Sin warning adicional: validate_env_path ya reportó el rechazo.
            logger.debug(
                "La instalación MO2 inyectada (%s) no es utilizable; "
                "get_mo2_path devuelve None (fail-closed, sin degradar a MO2_PATH).",
                self._mo2_install_dir,
            )
            return None
        validado = self.validate_env_path(os.environ.get("MO2_PATH", ""), "MO2_PATH")
        if validado is not None and not self._es_directorio_real(validado, "MO2_PATH"):
            return None
        return validado

    def has_explicit_mo2_install_selection(self) -> bool:
        """``True`` si el composition root inyectó una instalación (válida o no).

        Distingue **no hay hint** (fallback legacy/auto-detección permitido)
        de **hay hint pero es inválido** (prohibido detectar otra instalación:
        la selección explícita A invalidada no es vía libre para usar B).
        Los consumers con fallback a :meth:`detect_mo2_path` deben consultar
        esta señal antes de detectar.
        """
        return self._mo2_install_dir is not None

    def get_mo2_instance_data_root(self) -> pathlib.Path | None:
        """Raíz de DATOS de la instancia MO2 (``profiles/``, ``overwrite/``).

        Hermana de :meth:`get_mo2_path` (que es solo INSTALL ROOT): deriva de
        la metadata de la instancia —instalación seleccionada →
        :func:`descubrir_metadata_instancia_mo2` → ``raiz_datos`` validada con
        ``PathValidator``—, nunca de ``get_mo2_path() / "..."``.

        Best-effort y ``None``-safe (para preflights que omiten sensores y
        gates que fallan con su propio error): sin evidencia de instancia
        separada, degrada al legacy portable (los datos cuelgan de la
        instalación); sin instalación conocida, ``None``. El fail-closed con
        evidencia ante metadata inválida vive en :meth:`get_mo2_mods_path` y
        :meth:`resolve_modlist_path` (que lanzan); este accessor no los
        sustituye.

        Quien necesite el directorio de **mods** debe usar
        :meth:`get_mo2_mods_path` (``[Settings] mod_directory`` puede
        redefinirlo fuera de ``<data>/mods``), no ``<data>/mods`` a mano.
        """
        install_dir = self._directorio_instalacion_mo2()
        try:
            metadata = self._metadata_de_instancia(install_dir)
        except RuntimeError as exc:
            logger.debug("Metadata de instancia MO2 no utilizable (%s); sin raíz de datos.", exc)
            return None
        if metadata is not None:
            return metadata.raiz_datos
        if install_dir is not None and self._es_directorio_real(install_dir, "instance_data_legacy"):
            return install_dir
        return None

    def get_mo2_instance_data_root_estricto(self) -> pathlib.Path | None:
        """Raíz de DATOS de la instancia MO2 estricta (falla cerrado ante metadata corrupta).

        A diferencia de :meth:`get_mo2_instance_data_root`, propaga ``RuntimeError``
        si la metadata de la instancia existe pero es inválida o queda fuera del
        sandbox, evitando que operaciones mutantes degraden silenciosamente a la
        instalación. Devuelve ``None`` solo si no hay evidencia de instancia ni
        instalación conocida.
        """
        install_dir = self._directorio_instalacion_mo2()
        metadata = self._metadata_de_instancia(install_dir)
        if metadata is not None:
            return metadata.raiz_datos
        if install_dir is not None and self._es_directorio_real(install_dir, "instance_data_legacy"):
            return install_dir
        return None

    # Accessors CRUDOS (sin resolver): el validate() de los getters de arriba
    # sigue los symlinks, borrando exactamente lo que el VfsHealthChecker del
    # preflight necesita inspeccionar (review Codex PR #239). Solo para
    # inspección read-only (lstat); nunca para abrir/escribir archivos.

    @staticmethod
    def _sanitizar_raw(raw: str | pathlib.Path | None, etiqueta: str) -> pathlib.Path | None:
        """Path crudo sanitizado estructuralmente, degradando a None si es inválido.

        Mantiene el contrato raw —sin ``resolve()``, sin canonicalización, sin
        seguir symlinks— pero filtra lo que explotaría recién en los os-calls
        (``lstat``) del checker: un valor con bytes nulos no lanza al construir
        el Path pero sí en ``lstat`` (``ValueError``, que ``link_kind`` no
        captura — solo ``OSError``), así que se filtra acá con logging, como
        hacen los getters validados (review Copilot PR #240).

        Primitiva compartida entre el entorno (``_raw_env_path``) y el hint
        inyectado (``get_mo2_path_raw``): el hint crudo no puede bypassearla.
        """
        if raw is None:
            return None
        texto = str(raw)
        if not texto:
            return None
        if "\x00" in texto:
            security_logger.warning("%s crudo inválido (byte nulo embebido); se ignora.", etiqueta)
            return None
        try:
            return pathlib.Path(texto)
        except ValueError as exc:
            security_logger.warning("%s crudo inválido para pathlib: %s", etiqueta, exc)
            return None

    @classmethod
    def _raw_env_path(cls, var_name: str) -> pathlib.Path | None:
        """Path crudo desde *var_name*, degradando a None si es inválido."""
        return cls._sanitizar_raw(os.environ.get(var_name, ""), var_name)

    def get_skyrim_path_raw(self) -> pathlib.Path | None:
        """SKYRIM_PATH tal como está configurado, sin resolver symlinks."""
        return self._raw_env_path("SKYRIM_PATH")

    def get_mo2_path_raw(self) -> pathlib.Path | None:
        """MO2_PATH tal como está configurado, sin resolver symlinks.

        Hermano crudo de :meth:`get_mo2_path` (misma precedencia, SIN
        validación ni resolución — el ``VfsHealthChecker`` necesita hacer
        ``lstat`` del enlace, no del destino): con composición activa la
        instalación seleccionada ES la configurada, y que el accessor crudo
        siguiera leyendo solo ``MO2_PATH`` dejaría al preflight de LOOT
        inspeccionando el árbol del entorno mientras las tools operan sobre
        la instalación inyectada.

        Representa RAW INSTALL ROOT (no datos de instancia). Pasa por la
        misma sanitización estructural que el entorno (NUL/construcción),
        sin resolver ni canonicalizar.
        """
        if self._mo2_install_dir is not None:
            return self._sanitizar_raw(self._mo2_install_dir, "mo2_install_dir")
        return self._raw_env_path("MO2_PATH")

    def get_dyndolod_exe(self) -> pathlib.Path | None:
        """Resuelve DYNDLOD_EXE desde entorno validado."""
        return self.validate_env_path(os.environ.get("DYNDLOD_EXE", ""), "DYNDLOD_EXE")

    def get_texgen_exe(self) -> pathlib.Path | None:
        """Resuelve TEXGEN_EXE desde entorno validado."""
        return self.validate_env_path(os.environ.get("TEXGEN_EXE", ""), "TEXGEN_EXE")

    def get_synthesis_exe(self) -> pathlib.Path | None:
        """Resuelve SYNTHESIS_EXE desde entorno validado."""
        return self.validate_env_path(os.environ.get("SYNTHESIS_EXE", ""), "SYNTHESIS_EXE")

    def get_xedit_path(self) -> pathlib.Path | None:
        """Resuelve XEDIT_PATH desde entorno validado."""
        return self.validate_env_path(os.environ.get("XEDIT_PATH", ""), "XEDIT_PATH")

    def get_wrye_bash_path(self) -> pathlib.Path | None:
        """Resuelve WRYE_BASH_PATH desde entorno validado."""
        return self.validate_env_path(os.environ.get("WRYE_BASH_PATH", ""), "WRYE_BASH_PATH")

    def get_loot_exe(self) -> pathlib.Path | None:
        """Resuelve LOOT_EXE desde entorno validado."""
        return self.validate_env_path(os.environ.get("LOOT_EXE", ""), "LOOT_EXE")

    def get_pandora_exe(self) -> pathlib.Path | None:
        """Resuelve PANDORA_EXE desde entorno validado."""
        return self.validate_env_path(os.environ.get("PANDORA_EXE", ""), "PANDORA_EXE")
