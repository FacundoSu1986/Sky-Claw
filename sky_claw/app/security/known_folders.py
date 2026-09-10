"""Identidad de las carpetas de usuario de Windows (Known Folders).

**Qué propiedad da y por qué no la daba nada del árbol.** La admisión del
`external_work_root` (ADR 0011 §2.7) prohíbe un conjunto CERRADO de carpetas de
usuario —`Documents`, `Desktop`, `Downloads`— porque las tres tienen ciclo de
vida ajeno a Sky-Claw: sincronización cloud con placeholders (que rompe la
precondición de rename/lock del move-aside), limpieza y borrado manual
rutinarios, y agentes externos (navegador, políticas de almacenamiento) que
mueven o borran contenido como parte de su operación normal. El ADR justifica
carpeta por carpeta; este módulo sólo materializa la IDENTIDAD.

**La identidad es el identificador, no la ruta ni el nombre.** Las dos formas
fáciles están prohibidas por escrito en el ADR y las dos fallan en el mismo
escenario real, una carpeta redirigida:

* ``%USERPROFILE%\\Documents`` **no** refleja la redirección — el usuario mueve
  `Documents` a ``D:\\Users\\x\\Docs`` y la variable sigue apuntando al perfil,
  así que la carpeta prohibida quedaría admitida;
* buscar el substring ``"Documents"`` u ``"OneDrive"`` confunde identidad con
  nombre en las dos direcciones: ``E:\\Mis Documentos de Trabajo`` no es la
  Known Folder (falso positivo) y ``D:\\Users\\x\\Docs`` sí lo es (falso
  negativo).

Por eso la única fuente es ``SHGetKnownFolderPath`` con el GUID oficial de cada
carpeta, pidiendo la ruta **efectiva vigente** en el momento de admitir. Ancla
de que las dos formas prohibidas no reaparecen:
``tests/test_known_folders.py::test_ancla_de_fuente_sin_userprofile_ni_substrings``.

**Honestidad de alcance.** Esto NO es detección universal de proveedores cloud
(el ADR lo declara non-goal): resuelve tres identificadores y devuelve lo que el
sistema responde. Fuera de Windows no existen Known Folders y la primitiva lo
dice devolviendo ``None`` — no sintetiza un equivalente POSIX, que sería el
``%USERPROFILE%`` prohibido con otro nombre. La contención en esas plataformas
la aportan las otras reglas de admisión (solapamiento con game/MO2/TEMP, drive
root, UNC), no una adivinanza de este módulo.
"""

from __future__ import annotations

import ctypes
import logging
import pathlib
import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: GUID oficial (``KNOWNFOLDERID``) de cada carpeta contractual. Son los
#: identificadores publicados por Microsoft, estables entre versiones de Windows
#: y entre idiomas de la interfaz — que es justamente lo que un nombre de
#: carpeta no es (``Desktop`` se llama ``Escritorio`` en un Windows en español y
#: la ruta efectiva lo refleja).
IDENTIFICADORES: Final[Mapping[str, str]] = {
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}

#: Conjunto CERRADO v1 de carpetas prohibidas como `external_work_root`
#: (ADR 0011 §2.7). Agregar una exige enmienda del ADR con su propia
#: justificación de riesgo, no intuición: el ancla de igualdad literal en
#: ``tests/test_known_folders.py`` rompe si la tupla crece sola.
KNOWN_FOLDERS_PROHIBIDOS: Final[tuple[str, ...]] = ("Documents", "Desktop", "Downloads")

#: ``KF_FLAG_DEFAULT``: la ruta vigente, sin crear la carpeta ni forzar el
#: default de fábrica. Pedir ``KF_FLAG_DEFAULT_PATH`` devolvería la ruta ANTES
#: de la redirección — exactamente el bug que este módulo existe para no tener.
_KF_FLAG_DEFAULT: Final[int] = 0


def _resolver_por_api(guid: str) -> str | None:
    """Ruta efectiva del Known Folder *guid*, o ``None`` si no se puede obtener.

    Único punto del módulo que toca el sistema operativo; el resto es política.
    Los tests sustituyen esta función para ejercer el contrato
    identificador → ruta efectiva → veredicto (incluida una carpeta redirigida a
    otro volumen) sin depender de correr sobre Windows.

    Devuelve ``None`` —nunca una ruta inventada— cuando la plataforma no es
    Windows, cuando ``shell32`` no expone la API o cuando la llamada falla.
    """
    if sys.platform != "win32":
        return None

    # `ctypes.wintypes` sólo se importa dentro de la rama Windows: en algunas
    # plataformas su import a nivel de módulo levanta, y este archivo lo importa
    # todo el árbol (la admisión corre en cualquier SO).
    from ctypes import wintypes

    try:
        # `ctypes.windll` sólo existe en Windows; mypy lo sabe porque el
        # early-return de arriba estrecha `sys.platform` a "win32".
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
    except (AttributeError, OSError) as exc:  # pragma: no cover - depende de la plataforma
        logger.debug("Known Folders no disponibles en esta plataforma: %s", exc)
        return None

    class _GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        )

    rfid = _GUID()
    puntero = ctypes.c_wchar_p()
    try:
        # CLSIDFromString parsea el GUID canónico; no se arma la estructura a
        # mano para no reimplementar el parseo (y equivocarse en el endianness
        # de Data1/2/3, que es un error clásico y silencioso).
        hresultado = ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(rfid))
        if hresultado != 0:  # pragma: no cover - GUID constante del módulo
            logger.debug("GUID de Known Folder no parseable (%s): HRESULT=%s", guid, hresultado)
            return None
        hresultado = shell32.SHGetKnownFolderPath(ctypes.byref(rfid), _KF_FLAG_DEFAULT, None, ctypes.byref(puntero))
        if hresultado != 0 or not puntero.value:
            logger.debug("SHGetKnownFolderPath(%s) devolvió HRESULT=%s", guid, hresultado)
            return None
        return str(puntero.value)
    except OSError as exc:  # pragma: no cover - depende de la plataforma
        logger.debug("Fallo al resolver el Known Folder %s: %s", guid, exc)
        return None
    finally:
        if puntero.value:
            ole32.CoTaskMemFree(puntero)


def resolver_known_folder(nombre: str) -> pathlib.PureWindowsPath | None:
    """Ruta efectiva de la carpeta contractual *nombre*, o ``None``.

    ``None`` significa "el sistema no la reporta", no "no está prohibida": el
    caller decide qué hacer con esa ausencia. La admisión de
    :mod:`sky_claw.local.tools.dyndolod_workspace` simplemente no puede comparar
    contra una carpeta que no existe en esta plataforma, y su contención viene
    de las otras reglas.

    Se devuelve ``PureWindowsPath`` a propósito: la ruta la produce Windows y
    compararla con la semántica POSIX (case-sensitive, ``\\`` como carácter
    normal) daría veredictos falsos.

    Raises:
        KeyError: *nombre* no pertenece al conjunto cerrado v1. No hay
            resolución best-effort de nombres arbitrarios.
    """
    guid = IDENTIFICADORES[nombre]
    crudo = _resolver_por_api(guid)
    if not crudo:
        return None
    return pathlib.PureWindowsPath(crudo)


def known_folders_prohibidos() -> tuple[tuple[str, pathlib.PureWindowsPath], ...]:
    """Las carpetas prohibidas v1 que ESTE sistema reporta, con su ruta vigente.

    Enumera :data:`KNOWN_FOLDERS_PROHIBIDOS` completo —no una muestra— y omite
    las que la API no resuelve. El orden es el del conjunto congelado, para que
    el diagnóstico sea reproducible.
    """
    encontradas: list[tuple[str, pathlib.PureWindowsPath]] = []
    for nombre in KNOWN_FOLDERS_PROHIBIDOS:
        ruta = resolver_known_folder(nombre)
        if ruta is not None:
            encontradas.append((nombre, ruta))
    return tuple(encontradas)
