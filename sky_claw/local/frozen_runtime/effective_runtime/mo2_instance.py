"""Verificación READ-ONLY de identidad de la instancia activa de MO2 (P7-A5 R3).

Cierra la vulnerabilidad de identidad detectada en A5-R1: la mera presencia de un
archivo ``ModOrganizer.ini`` dentro de un directorio (como el INI residual que MO2
o herramientas previas hayan dejado en ``base_directory``, p. ej.
``G:\\Modding\\MO2\\SkyrimSE\\ModOrganizer.ini``) NO demuestra que MO2 utilice ese
directorio como su instancia activa.

Modelo real de selección en Mod Organizer 2:

1. **Instancia global activa (``ACTIVE_GLOBAL``):**
   ``HKCU\\Software\\Mod Organizer Team\\Mod Organizer`` contiene ``CurrentInstance``
   (``REG_SZ`` no vacío) y ``mo2_instance_dir`` coincide exactamente con
   ``%LOCALAPPDATA%\\ModOrganizer\\<CurrentInstance>``, posee ``ModOrganizer.ini``
   regular y carece de artefactos de instalación portable (``ModOrganizer.exe``,
   ``portable.txt``).
2. **Instalación portable activa (``ACTIVE_PORTABLE``):**
   ``mo2_instance_dir`` está fuera de ``%LOCALAPPDATA%\\ModOrganizer``, contiene
   tanto ``ModOrganizer.exe`` como ``ModOrganizer.ini`` regulares y el selector
   del Registro declara explícitamente modo portable (``CurrentInstance == ""``) o
   bien la clave de Registro no existe (``ABSENT``) pero ``portable.txt`` fuerza
   explícitamente el modo portable sin conflicto global.
3. **Bloqueo fail-closed (``INACTIVE``, ``AMBIGUOUS``, ``UNKNOWN``):**
   Cualquier directorio huérfano/residual, discrepancia con ``CurrentInstance``,
   conflicto entre ``portable.txt`` y una instancia global seleccionada en ``HKCU``,
   redirección por enlace/reparse o error de lectura del Registro bloquea.
"""

from __future__ import annotations

import os
import pathlib
import stat
import sys
from collections.abc import Callable, Mapping

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    Mo2InstanceIdentityObservation,
    Mo2InstanceIdentityState,
    Mo2RegistryObservation,
    Mo2RegistryState,
)

_SUBCLAVE_MO2_HKCU = r"Software\Mod Organizer Team\Mod Organizer"
_VALOR_CURRENT_INSTANCE = "CurrentInstance"
_NOMBRE_INI = "modorganizer.ini"
_NOMBRE_EXE = "modorganizer.exe"
_NOMBRE_PORTABLE_TXT = "portable.txt"
_MARCADORES_MO2 = frozenset({_NOMBRE_INI, _NOMBRE_EXE, _NOMBRE_PORTABLE_TXT})
_REPARSE_ATTR = 0x00000400


def _normalizar_ruta(ruta: str | pathlib.Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


def _es_igual_o_hijo(candidato: str | pathlib.Path, contenedor: str | pathlib.Path) -> bool:
    hijo = _normalizar_ruta(candidato)
    padre = _normalizar_ruta(contenedor)
    if hijo == padre:
        return True
    return hijo.startswith(padre.rstrip(os.sep) + os.sep)


def _inspeccionar_reparse_ancestros(ruta: pathlib.Path) -> tuple[str, str]:
    """Devuelve ``('none'|'reparse'|'indeterminate', detalle)`` sin seguir enlaces."""
    actual = pathlib.Path(ruta)
    while True:
        try:
            tipo = link_kind_or_raise(actual)
        except FileNotFoundError:
            return "none", ""
        except OSError as exc:
            return "indeterminate", f"error al inspeccionar reparse en '{actual}': {exc}"
        if tipo is not None:
            return "reparse", f"'{actual}' es un enlace/reparse ({tipo})"
        if actual.parent == actual:
            return "none", ""
        actual = actual.parent


def _leer_current_instance_de_registro_windows() -> Mo2RegistryObservation:
    """Lee en modo estrictamente READ-ONLY (``KEY_READ``) ``CurrentInstance`` en ``HKCU``."""
    if sys.platform != "win32":
        return Mo2RegistryObservation(
            state=Mo2RegistryState.UNREADABLE,
            detail="lectura nativa de Registro HKCU sólo disponible en Windows (inyectar registry_reader en tests)",
        )
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SUBCLAVE_MO2_HKCU, 0, winreg.KEY_READ) as clave:
            valor, tipo = winreg.QueryValueEx(clave, _VALOR_CURRENT_INSTANCE)
    except FileNotFoundError:
        return Mo2RegistryObservation(
            state=Mo2RegistryState.ABSENT,
            detail="clave o valor CurrentInstance ausente en HKCU",
        )
    except OSError as exc:
        return Mo2RegistryObservation(
            state=Mo2RegistryState.UNREADABLE,
            detail=f"no se pudo leer CurrentInstance en HKCU: {exc}",
        )

    if tipo != winreg.REG_SZ or not isinstance(valor, str):
        return Mo2RegistryObservation(
            state=Mo2RegistryState.UNREADABLE,
            detail=f"CurrentInstance en HKCU tiene tipo inesperado ({tipo!r}); se esperaba REG_SZ",
        )
    return Mo2RegistryObservation(
        state=Mo2RegistryState.OBSERVED,
        current_instance=valor,
    )


def _inspeccionar_marcadores_instancia(
    inst_dir: pathlib.Path,
) -> tuple[str, bool, bool, bool, str]:
    """Escanea ``inst_dir`` buscando ``ModOrganizer.ini``, ``ModOrganizer.exe`` y ``portable.txt``.

    Returns:
        ``(estado_scan, tiene_ini, tiene_exe, tiene_portable_txt, detalle)`` donde
        ``estado_scan`` es ``'ok'``, ``'missing'``, ``'ambiguous'`` o ``'unknown'``.
    """
    tiene_ini = False
    tiene_exe = False
    tiene_portable = False
    try:
        with os.scandir(inst_dir) as entradas:
            for entrada in entradas:
                nombre = entrada.name.casefold()
                if nombre not in _MARCADORES_MO2:
                    continue
                if entrada.is_symlink():
                    return "unknown", False, False, False, f"marcador '{entrada.path}' es un symlink"
                st = os.stat(entrada.path, follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _REPARSE_ATTR:
                    return "unknown", False, False, False, f"marcador '{entrada.path}' es un reparse point"
                if not stat.S_ISREG(st.st_mode):
                    return (
                        "ambiguous",
                        False,
                        False,
                        False,
                        f"marcador '{entrada.path}' no es un archivo regular",
                    )
                if nombre == _NOMBRE_INI:
                    tiene_ini = True
                elif nombre == _NOMBRE_EXE:
                    tiene_exe = True
                elif nombre == _NOMBRE_PORTABLE_TXT:
                    tiene_portable = True
    except (FileNotFoundError, NotADirectoryError):
        return "missing", False, False, False, f"el directorio de instancia '{inst_dir}' no existe"
    except OSError as exc:
        return "unknown", False, False, False, f"no se pudo inspeccionar '{inst_dir}': {exc}"

    return "ok", tiene_ini, tiene_exe, tiene_portable, ""


def _nombre_instancia_seguro(nombre: str) -> bool:
    if not nombre or nombre in (".", ".."):
        return False
    return "/" not in nombre and "\\" not in nombre and ":" not in nombre


def probe_mo2_instance_identity(
    mo2_instance_dir: pathlib.Path,
    *,
    registry_reader: Callable[[], Mo2RegistryObservation] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Mo2InstanceIdentityObservation:
    """Evalúa de forma READ-ONLY y fail-closed si ``mo2_instance_dir`` es la instancia activa de MO2."""
    lector = registry_reader if registry_reader is not None else _leer_current_instance_de_registro_windows
    entorno = os.environ if environ is None else environ

    inst_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    inst_str = str(inst_dir)
    reg_obs = lector()

    clase_rep, det_rep = _inspeccionar_reparse_ancestros(inst_dir)
    if clase_rep != "none":
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.UNKNOWN,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=det_rep,
        )

    estado_scan, tiene_ini, tiene_exe, tiene_portable, det_scan = _inspeccionar_marcadores_instancia(inst_dir)
    if estado_scan == "missing":
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.INACTIVE,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=det_scan,
        )
    if estado_scan == "ambiguous":
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.AMBIGUOUS,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=det_scan,
        )
    if estado_scan != "ok":
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.UNKNOWN,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=det_scan,
        )
    if not tiene_ini:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.INACTIVE,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=f"'{inst_dir}' carece de ModOrganizer.ini regular",
        )

    if reg_obs.state is Mo2RegistryState.UNREADABLE:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.UNKNOWN,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=reg_obs.detail or "Registro HKCU de MO2 ilegible (UNKNOWN != PASS)",
        )

    local_appdata_raw = (entorno.get("LOCALAPPDATA") or "").strip()
    raiz_global = pathlib.Path(os.path.abspath(local_appdata_raw)) / "ModOrganizer" if local_appdata_raw else None
    bajo_raiz_global = raiz_global is not None and _es_igual_o_hijo(inst_dir, raiz_global)

    if reg_obs.state is Mo2RegistryState.OBSERVED:
        actual = (reg_obs.current_instance or "").strip()
        if actual:
            if not _nombre_instancia_seguro(actual):
                return Mo2InstanceIdentityObservation(
                    state=Mo2InstanceIdentityState.UNKNOWN,
                    instance_dir=inst_str,
                    registry_state=reg_obs.state,
                    registry_current_instance=reg_obs.current_instance,
                    detail=f"CurrentInstance en HKCU contiene segmentos de ruta inválidos: {actual!r}",
                )
            if raiz_global is None:
                return Mo2InstanceIdentityObservation(
                    state=Mo2InstanceIdentityState.UNKNOWN,
                    instance_dir=inst_str,
                    registry_state=reg_obs.state,
                    registry_current_instance=reg_obs.current_instance,
                    detail="CurrentInstance declara instancia global pero LOCALAPPDATA no está disponible",
                )
            esperada_global = raiz_global / actual
            if _normalizar_ruta(inst_dir) == _normalizar_ruta(esperada_global):
                if tiene_exe or tiene_portable:
                    return Mo2InstanceIdentityObservation(
                        state=Mo2InstanceIdentityState.AMBIGUOUS,
                        instance_dir=inst_str,
                        registry_state=reg_obs.state,
                        registry_current_instance=reg_obs.current_instance,
                        detail=(
                            f"la instancia global '{inst_dir}' contiene artefactos portables "
                            "(ModOrganizer.exe o portable.txt)"
                        ),
                    )
                return Mo2InstanceIdentityObservation(
                    state=Mo2InstanceIdentityState.ACTIVE_GLOBAL,
                    instance_dir=inst_str,
                    registry_state=reg_obs.state,
                    registry_current_instance=reg_obs.current_instance,
                    detail=f"instancia global activa verificada contra HKCU CurrentInstance='{actual}'",
                )

            if tiene_exe and tiene_portable and not bajo_raiz_global:
                return Mo2InstanceIdentityObservation(
                    state=Mo2InstanceIdentityState.AMBIGUOUS,
                    instance_dir=inst_str,
                    registry_state=reg_obs.state,
                    registry_current_instance=reg_obs.current_instance,
                    detail=(
                        f"ambigüedad entre instalación portable con portable.txt ('{inst_dir}') "
                        f"y CurrentInstance global '{actual}' en HKCU"
                    ),
                )
            return Mo2InstanceIdentityObservation(
                state=Mo2InstanceIdentityState.INACTIVE,
                instance_dir=inst_str,
                registry_state=reg_obs.state,
                registry_current_instance=reg_obs.current_instance,
                detail=(
                    f"el directorio '{inst_dir}' NO es la instancia activa; "
                    f"HKCU CurrentInstance selecciona '{esperada_global}'"
                ),
            )

        # CurrentInstance == "" => MO2 registra selección de modo portable
        if bajo_raiz_global:
            return Mo2InstanceIdentityObservation(
                state=Mo2InstanceIdentityState.INACTIVE,
                instance_dir=inst_str,
                registry_state=reg_obs.state,
                registry_current_instance=reg_obs.current_instance,
                detail="HKCU CurrentInstance está vacío (modo portable); la instancia global no está activa",
            )
        if not tiene_exe:
            return Mo2InstanceIdentityObservation(
                state=Mo2InstanceIdentityState.INACTIVE,
                instance_dir=inst_str,
                registry_state=reg_obs.state,
                registry_current_instance=reg_obs.current_instance,
                detail=(
                    f"'{inst_dir}' contiene ModOrganizer.ini pero carece de ModOrganizer.exe; "
                    "no es una instalación portable activa (posible base_directory residual)"
                ),
            )
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.AMBIGUOUS,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=(
                f"ambigüedad en instalación portable ('{inst_dir}'): el Registro HKCU CurrentInstance está vacío, "
                "lo cual no otorga autoridad de ruta entre múltiples directorios portables posibles"
            ),
        )

    # reg_obs.state is Mo2RegistryState.ABSENT
    if bajo_raiz_global:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.INACTIVE,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail="clave HKCU CurrentInstance ausente; ninguna instancia global está seleccionada",
        )
    if tiene_exe and tiene_portable:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.AMBIGUOUS,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=(
                f"ambigüedad en instalación portable con portable.txt ('{inst_dir}'): clave HKCU ausente, "
                "portable.txt no otorga autoridad de ruta única frente a múltiples directorios portables posibles"
            ),
        )
    if tiene_exe:
        return Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.UNKNOWN,
            instance_dir=inst_str,
            registry_state=reg_obs.state,
            registry_current_instance=reg_obs.current_instance,
            detail=(
                f"'{inst_dir}' contiene ModOrganizer.exe pero falta HKCU CurrentInstance y portable.txt; "
                "no se puede probar selección portable"
            ),
        )
    return Mo2InstanceIdentityObservation(
        state=Mo2InstanceIdentityState.INACTIVE,
        instance_dir=inst_str,
        registry_state=reg_obs.state,
        registry_current_instance=reg_obs.current_instance,
        detail=f"'{inst_dir}' carece de ModOrganizer.exe y no existe selección en HKCU",
    )


__all__ = [
    "probe_mo2_instance_identity",
]
