"""Exclusión verificable de ``modorganizer.ini`` durante la transición (P5-C2).

Tracker: #672
ADR 0012 §13, §29.4, §34.4

El problema que este módulo resuelve
------------------------------------
P5-C1 dejó declarado ``EXCLUSION_WITH_MO2 = NOT_ESTABLISHED``: el binder
comprueba que ``ModOrganizer.exe`` no esté vivo y revalida el INI justo antes de
mutarlo, pero entre esa revalidación y el reemplazo queda una ventana en la que
otro proceso puede escribir el archivo. El lock de Sky-Claw no la cubre porque
MO2 no participa de él.

La propiedad que este módulo hace cumplir es más estrecha y más fuerte que
«nadie abre MO2»:

    Mientras el handle está retenido, ningún otro proceso puede ESCRIBIR,
    TRUNCAR, BORRAR ni RENOMBRAR ``modorganizer.ini``.

Eso es exactamente la ventana TOCTOU. Que otro proceso lo LEA no la abre: una
lectura no cambia los bytes que el binder está a punto de reemplazar.

La configuración, y por qué es esa
----------------------------------
Medido en un rig Windows 10 19045 con procesos externos reales
(``E:/Skyclaw_Rig_MO2_Exclusion``), no deducido de la documentación:

===================================  ==========================================
``dwDesiredAccess``                  ``GENERIC_READ | GENERIC_WRITE | DELETE``
``dwShareMode``                      ``0`` (``FILE_SHARE_NONE``)
===================================  ==========================================

Con esa combinación, y medido sonda por sonda:

- escritura externa: ``ERROR_SHARING_VIOLATION`` (32), incluso si el otro
  proceso pide ``FILE_SHARE_READ|WRITE|DELETE``;
- ``MoveFileEx`` externo sobre el INI: ``ERROR_ACCESS_DENIED`` (5);
- ``ReplaceFileW`` externo: ``ERROR_SHARING_VIOLATION`` (32);
- ``DeleteFile`` externo: ``ERROR_SHARING_VIOLATION`` (32);
- apertura con sólo ``DELETE``: ``ERROR_SHARING_VIOLATION`` (32);
- lectura, tanto CRT como **compatible**: ``ERROR_SHARING_VIOLATION`` (32).
- escritura por un alias hardlink del mismo archivo: bloqueada también, porque
  la exclusión es sobre el OBJETO de archivo, no sobre el nombre.

Las alternativas se midieron y se descartaron por evidencia, no por gusto:

- ``FILE_SHARE_READ|FILE_SHARE_DELETE`` deja pasar ``ReplaceFileW`` y
  ``DeleteFile`` externos: no excluye nada útil.
- ``FILE_SHARE_READ|FILE_SHARE_WRITE`` deja pasar la escritura externa.
- ``FILE_SHARE_READ`` —la primera versión de este módulo— bloquea a los
  escritores pero **no** a los lectores compatibles, que observan la escritura a
  medias. Ver más abajo, «Lectores».

Que ``dwShareMode = 0`` también impida leer el INI durante la transición **no es
un costo que se acepta a regañadientes: es la garantía**. El INI no tiene por qué
ser legible mientras se lo está reescribiendo, y permitirlo fue justamente lo que
abrió la ventana de lectura rasgada.

Por qué la escritura es in-place y no ``os.replace``
----------------------------------------------------
Con ``DELETE`` en el acceso propio, el reemplazo por ruta es imposible: el
propio handle se bloquea a sí mismo. Se midió ``os.replace`` →
``ERROR_ACCESS_DENIED`` (5), ``MoveFileEx`` → 5, y
``SetFileInformationByHandle(FileRenameInfoEx)`` con
``FILE_RENAME_FLAG_POSIX_SEMANTICS`` sobre el destino retenido →
``ERROR_SHARING_VIOLATION`` (32). No hay reemplazo atómico posible mientras se
sostiene la exclusión.

Quedaban dos protocolos, y se midieron los dos bajo agresor:

- **rename-away + install**: deja el NOMBRE libre entre los dos renombres. Un
  creador externo en bucle ganó la carrera **40 de 40 veces** y rompió **27 de
  40** transiciones con el original ya movido.
- **escritura in-place por el handle**: el nombre nunca queda libre. El mismo
  creador ganó **0 de 123.910** intentos.

Por eso se escribe in-place. El costo es que la escritura no es atómica: una
muerte del proceso a mitad deja el INI truncado. Eso NO queda descubierto —
``binder_recovery`` ya captura los bytes completos del INI antes de mutarlo y
restaura superficie por superficie— y por eso este módulo no agrega un mecanismo
de recuperación propio.

Lectores: por qué la compartición es NINGUNA
-------------------------------------------
La primera versión de este módulo concedía ``FILE_SHARE_READ``, con el argumento
de que así el INI seguía siendo legible y de que, con ``DELETE`` en el acceso
propio, la lectura rasgada era «imposible por construcción». **Esa afirmación era
falsa**, y se corrigió midiendo.

La validación de compartición es **bidireccional**: el acceso pedido tiene que
estar contenido en el share existente, y el acceso existente tiene que estar
contenido en el share pedido. Con ``share = READ`` del lado del tenedor, un
lector que pida ``GENERIC_READ`` y conceda ``READ|WRITE|DELETE`` satisface las dos
direcciones y abre. Medido: ese lector coexistió y capturó **11.038 lecturas
rasgadas** mientras el tenedor escribía in-place con la ventana real ensanchada
(``WriteFile`` sin ``SetEndOfFile``). Un MO2 que concediera ``DELETE`` habría
leído un INI a medias.

Con ``COMPARTICION_EXCLUSIVA = 0`` ningún proceso externo abre el archivo, ni
para leer. El mismo lector recibe ``ERROR_SHARING_VIOLATION`` (32) y la escritura
a medias deja de ser observable. Ésa es la razón por la que el share es 0, y no
una preferencia estética.

Las dos garantías quedan separadas a propósito, y se prueban por separado:

- **exclusión frente a escritores**: nadie escribe, trunca, borra ni renombra;
- **no observabilidad**: nadie lee un estado intermedio.

La primera no implica la segunda —era exactamente el error de la versión
anterior— y por eso tienen tests distintos.

Consecuencia operativa: mientras el handle está retenido, el código que lee el
INI por ruta falla. Por eso el binder lee y escribe por el handle y libera antes
de los readbacks por ruta.

Qué NO afirma este módulo
-------------------------
- ``POWER_LOSS_GUARANTEE = NOT_CLAIMED``. Se hace ``FlushFileBuffers`` sobre el
  handle, que vuelca el contenido del archivo; no se hace ``fsync`` del
  directorio ni hay WAL. Un corte eléctrico sigue sin garantía.
- No impide que un proceso ABRA ``ModOrganizer.exe``. Impide que ese proceso
  modifique el INI mientras la transición lo retiene, que es la condición que
  la corrección necesita.
- No coordina con MO2 por ningún protocolo: MO2 no expone uno.
- Fuera de Windows no hay exclusión de archivos compartidos. El estado es
  ``INDETERMINATE`` y nunca autoriza una mutación.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import hashlib
import os
import pathlib
import time
from enum import StrEnum
from typing import Any, Final

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError

__all__ = [
    "ACCESO_EXCLUSIVO",
    "COMPARTICION_EXCLUSIVA",
    "ESTADOS_AUTORIZANTES",
    "DiagnosticoExclusion",
    "EstadoExclusion",
    "ExclusionIni",
    "IdentidadIni",
    "Mo2IniExclusionError",
    "adquirir_exclusion_ini",
    "es_reparse_point",
    "excluir_ini",
]

# ---------------------------------------------------------------------------
# Constantes Win32 (los valores, no los nombres: ctypes no los exporta)
# ---------------------------------------------------------------------------

_GENERIC_READ: Final = 0x80000000
_GENERIC_WRITE: Final = 0x40000000
_DELETE: Final = 0x00010000
_OPEN_EXISTING: Final = 3
_FILE_ATTRIBUTE_NORMAL: Final = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
# ``CreateFileW`` con ``restype = c_void_p`` devuelve el puntero SIN SIGNO,
# así que el centinela -1 nunca compara igual y una apertura FALLIDA pasaría
# por válida: el gate escribiría sobre un handle inválido y reportaría éxito.
_INVALID_HANDLE_VALUE: Final = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
_FILE_BEGIN: Final = 0

_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_SHARING_VIOLATION: Final = 32
_ERROR_LOCK_VIOLATION: Final = 33
_ERROR_FILE_EXISTS: Final = 80
_ERROR_PATH_NOT_FOUND: Final = 3

#: Acceso propio. ``DELETE`` no se usa para borrar: se usa porque es lo que
#: impide que otro proceso abra con ``DELETE``, que es el permiso que habilita
#: renombrar y borrar. Ver el docstring del módulo.
ACCESO_EXCLUSIVO: Final = _GENERIC_READ | _GENERIC_WRITE | _DELETE

#: Compartición propia: **ninguna**. Ni ``FILE_SHARE_READ``, ni ``WRITE``, ni
#: ``DELETE``.
#:
#: ``FILE_SHARE_READ`` se usó primero, con el argumento de que así el INI seguía
#: siendo legible. Ese argumento era falso en el punto que importa: la validación
#: de compartición es BIDIRECCIONAL, así que un lector que pida ``GENERIC_READ``
#: con ``share = READ|WRITE|DELETE`` satisface las dos direcciones y **sí abre**
#: aunque el tenedor sólo conceda ``READ``. Medido: ese lector coexistió y observó
#: **11.038 lecturas rasgadas** durante una escritura in-place ensanchada en su
#: ventana real (``WriteFile`` sin ``SetEndOfFile``).
#:
#: Con ``0`` ningún proceso externo puede abrir el archivo —ni para leer—, así
#: que la escritura a medias deja de ser observable. Medido: el mismo lector
#: recibe ``ERROR_SHARING_VIOLATION`` (32).
#:
#: Costo medido, y es acotado: la adquisición falla cerrado mientras un lector de
#: esos tenga el archivo abierto (195/200 adquisiciones con un lector en bucle).
#: El reintento acotado ya existente lo resuelve: 200/200 con 40 reintentos de
#: 5 ms. ``os.stat`` por ruta sigue funcionando porque en Windows usa
#: ``GetFileAttributesExW`` y no abre handle, así que la comprobación de
#: identidad del gate no se rompe.
COMPARTICION_EXCLUSIVA: Final = 0

#: Códigos que significan «no pude tomar la exclusión», no «no sé».
_ERRORES_DE_CONTENCION: Final = frozenset(
    {_ERROR_SHARING_VIOLATION, _ERROR_ACCESS_DENIED, _ERROR_LOCK_VIOLATION, _ERROR_FILE_EXISTS}
)

#: Códigos que significan «el archivo no está donde el preflight lo vio».
_ERRORES_DE_AUSENCIA: Final = frozenset({_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND})


class EstadoExclusion(StrEnum):
    """Veredicto de la adquisición. Sólo ``ESTABLISHED`` autoriza mutar."""

    ESTABLISHED = "EXCLUSION_ESTABLISHED"
    NOT_ESTABLISHED = "EXCLUSION_NOT_ESTABLISHED"
    #: No se pudo determinar. Nunca autoriza una mutación productiva.
    INDETERMINATE = "INDETERMINATE"


#: Conjunto explícito, para que un estado nuevo no herede autorización por
#: accidente. Es el patrón que el repo usa para los gates fail-closed.
ESTADOS_AUTORIZANTES: Final[frozenset[EstadoExclusion]] = frozenset({EstadoExclusion.ESTABLISHED})


class Mo2IniExclusionError(FrozenRuntimeError):
    """Error base de la exclusión de ``modorganizer.ini``."""


class ExclusionNoAutorizadaError(Mo2IniExclusionError):
    """Se intentó mutar sin una exclusión en estado autorizante."""


@dataclasses.dataclass(frozen=True, slots=True)
class IdentidadIni:
    """Identidad de archivo de grado handle.

    ``volumen`` + ``indice`` identifican el OBJETO, no el nombre. Sobrevive a un
    rename y es la misma para todos los hardlinks, así que sirve para detectar
    que el path apunta a otro archivo que el que se abrió.
    """

    volumen: int
    indice: int

    def __str__(self) -> str:
        return f"{self.volumen:08x}:{self.indice:016x}"


@dataclasses.dataclass(frozen=True, slots=True)
class DiagnosticoExclusion:
    """Qué pasó al intentar tomar la exclusión, con evidencia reproducible."""

    estado: EstadoExclusion
    ruta: pathlib.Path
    motivo: str
    intentos: int
    identidad: IdentidadIni | None = None

    @property
    def autoriza(self) -> bool:
        """``True`` sólo si el estado está en :data:`ESTADOS_AUTORIZANTES`."""
        return self.estado in ESTADOS_AUTORIZANTES


# ---------------------------------------------------------------------------
# Capa Win32. Se declara una sola vez y con firmas explícitas.
# ---------------------------------------------------------------------------


class _FileTime(ctypes.Structure):
    """``FILETIME`` como DOS ``DWORD``, no como ``uint64``.

    La alineación importa: ``FILETIME`` alinea a 4, un ``c_uint64`` alinearía a
    8 y desplazaría TODOS los campos siguientes cuatro bytes. Con ese error
    ``nFileIndexLow`` lee basura, la comparación de identidad falla y el gate
    rechaza archivos sanos. Se declara explícito para que el layout sea el de
    ``BY_HANDLE_FILE_INFORMATION`` y no el que sugiera el tamaño.
    """

    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FileTime),
        ("ftLastAccessTime", _FileTime),
        ("ftLastWriteTime", _FileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


def _cargar_kernel32() -> Any | None:
    """``kernel32`` con firmas declaradas, o ``None`` fuera de Windows.

    Devolver ``None`` en vez de fallar al importar es deliberado: el paquete se
    importa en cualquier plataforma y el gate tiene que poder decir
    ``INDETERMINATE`` en lugar de romper el import de todo Frozen Runtime.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):  # pragma: no cover - sólo Windows real
        return None

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_int
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_int
    kernel32.SetEndOfFile.argtypes = [ctypes.c_void_p]
    kernel32.SetEndOfFile.restype = ctypes.c_int
    kernel32.SetFilePointer.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetFilePointer.restype = ctypes.c_uint32
    kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    kernel32.FlushFileBuffers.restype = ctypes.c_int
    kernel32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetFileAttributesW.restype = ctypes.c_uint32
    return kernel32


_KERNEL32: Final[Any | None] = _cargar_kernel32()


def _ultimo_error() -> int:
    return int(ctypes.get_last_error())


def _atributos_archivo(ruta: str | os.PathLike[str]) -> tuple[int | None, int]:
    """Atributos del archivo y el código de error si no se pudieron leer.

    Devuelve ``(attrs, 0)`` en éxito y ``(None, codigo)`` en fallo. Se separa de
    :func:`es_reparse_point` porque el código de error distingue «no existe»
    —que es un veredicto— de «no sé qué pasó», que es otro.
    """
    if _KERNEL32 is None:
        return None, 0
    attrs = int(_KERNEL32.GetFileAttributesW(os.fspath(ruta)))
    if attrs == 0xFFFFFFFF:
        return None, _ultimo_error()
    return attrs, 0


def es_reparse_point(ruta: str | os.PathLike[str]) -> bool | None:
    """``True``/``False``, o ``None`` si no se pudo determinar.

    Un reparse point en la ruta del INI rompe la premisa del gate: la identidad
    del PATH deja de ser la identidad del archivo que se va a mutar, y el
    destino real queda fuera de la exclusión. Por eso se rechaza antes de abrir.

    ``None`` no es ``False``: no poder leer los atributos no prueba que el
    archivo sea ordinario.
    """
    attrs, _ = _atributos_archivo(ruta)
    if attrs is None:
        return None
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


@dataclasses.dataclass(slots=True)
class _Apertura:
    handle: int
    identidad: IdentidadIni


class _AperturaError(Exception):
    """Fallo de ``CreateFileW`` con el código Win32 preservado.

    No se usa ``OSError``: ``OSError(codigo, mensaje)`` deja ``winerror`` en
    ``None`` en Windows —el cuarto argumento posicional es el que lo puebla— y
    entonces la clasificación por código cae siempre en «no clasificado» y el
    gate devuelve ``INDETERMINATE`` donde debía decir ``NOT_ESTABLISHED``. Un
    error con el código perdido es un error que no se puede adjudicar.
    """

    def __init__(self, codigo: int, detalle: str) -> None:
        self.codigo = codigo
        self.detalle = detalle
        super().__init__(f"WinError {codigo}: {detalle}")


def _abrir_exclusivo(ruta: pathlib.Path) -> _Apertura:
    """Un intento de ``CreateFileW`` con la configuración medida.

    Raises:
        _AperturaError: cuando Windows rechaza la apertura, con el código exacto.
    """
    if _KERNEL32 is None:
        raise _AperturaError(0, "kernel32 no disponible")
    handle = _KERNEL32.CreateFileW(
        os.fspath(ruta),
        ACCESO_EXCLUSIVO,
        COMPARTICION_EXCLUSIVA,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or int(handle) == _INVALID_HANDLE_VALUE:
        codigo = _ultimo_error()
        raise _AperturaError(codigo, f"CreateFileW({ruta})")

    info = _ByHandleFileInformation()
    if not _KERNEL32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
        codigo = _ultimo_error()
        _KERNEL32.CloseHandle(ctypes.c_void_p(handle))
        raise _AperturaError(codigo, f"GetFileInformationByHandle({ruta})")

    return _Apertura(
        handle=int(handle),
        identidad=IdentidadIni(
            volumen=int(info.dwVolumeSerialNumber),
            indice=(int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
        ),
    )


def _cerrar_handle(handle: int) -> None:
    if _KERNEL32 is not None and handle:
        _KERNEL32.CloseHandle(ctypes.c_void_p(handle))


def _leer_handle(handle: int) -> bytes:
    """Lee el archivo completo desde el principio.

    Rebobina primero: ``ReadFile`` avanza el puntero, así que sin esto un
    readback después de escribir devuelve ``b""`` y una verificación ingenua
    compara vacío contra vacío y «pasa».
    """
    if _KERNEL32 is None:
        raise Mo2IniExclusionError("kernel32 no disponible: no se puede leer por handle")
    if _KERNEL32.SetFilePointer(ctypes.c_void_p(handle), 0, None, _FILE_BEGIN) == 0xFFFFFFFF:
        codigo = _ultimo_error()
        raise Mo2IniExclusionError(f"SetFilePointer(rewind) falló con WinError {codigo}")

    trozos: list[bytes] = []
    buffer = ctypes.create_string_buffer(65536)
    leidos = ctypes.c_uint32(0)
    while True:
        ok = _KERNEL32.ReadFile(ctypes.c_void_p(handle), buffer, len(buffer), ctypes.byref(leidos), None)
        if not ok:
            codigo = _ultimo_error()
            raise Mo2IniExclusionError(f"ReadFile falló con WinError {codigo}")
        if leidos.value == 0:
            return b"".join(trozos)
        trozos.append(buffer.raw[: leidos.value])


def _escribir_handle(handle: int, datos: bytes) -> None:
    """Escribe ``datos`` desde el byte 0 y trunca al final, por el handle."""
    if _KERNEL32 is None:
        raise Mo2IniExclusionError("kernel32 no disponible: no se puede escribir por handle")
    if _KERNEL32.SetFilePointer(ctypes.c_void_p(handle), 0, None, _FILE_BEGIN) == 0xFFFFFFFF:
        codigo = _ultimo_error()
        raise Mo2IniExclusionError(f"SetFilePointer falló con WinError {codigo}")

    escritos = ctypes.c_uint32(0)
    if datos:
        ok = _KERNEL32.WriteFile(ctypes.c_void_p(handle), datos, len(datos), ctypes.byref(escritos), None)
        if not ok:
            codigo = _ultimo_error()
            raise Mo2IniExclusionError(f"WriteFile falló con WinError {codigo}")
        if escritos.value != len(datos):
            raise Mo2IniExclusionError(f"WriteFile escribió {escritos.value} de {len(datos)} bytes: escritura parcial")

    if not _KERNEL32.SetEndOfFile(ctypes.c_void_p(handle)):
        codigo = _ultimo_error()
        raise Mo2IniExclusionError(f"SetEndOfFile falló con WinError {codigo}")
    # Vuelca el contenido del archivo. NO es un fsync de directorio: no cambia
    # POWER_LOSS_GUARANTEE, que sigue en NOT_CLAIMED.
    if not _KERNEL32.FlushFileBuffers(ctypes.c_void_p(handle)):
        codigo = _ultimo_error()
        raise Mo2IniExclusionError(f"FlushFileBuffers falló con WinError {codigo}")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class ExclusionIni:
    """Handle que excluye la mutación externa de un INI mientras esté abierto.

    No se construye directamente: se obtiene con :func:`adquirir_exclusion_ini`
    o con :func:`excluir_ini`. Cerrar es obligatorio; el ``__exit__`` lo hace
    aunque el cuerpo lance.
    """

    __slots__ = ("_diagnostico", "_handle")

    def __init__(self, handle: int | None, diagnostico: DiagnosticoExclusion) -> None:
        self._handle = handle
        self._diagnostico = diagnostico

    @property
    def diagnostico(self) -> DiagnosticoExclusion:
        return self._diagnostico

    @property
    def estado(self) -> EstadoExclusion:
        return self._diagnostico.estado

    @property
    def autoriza(self) -> bool:
        return self._diagnostico.autoriza

    @property
    def identidad(self) -> IdentidadIni | None:
        return self._diagnostico.identidad

    def _exigir_autorizacion(self) -> int:
        if self._handle is None or not self.autoriza:
            raise ExclusionNoAutorizadaError(
                f"la exclusión no autoriza mutar {self._diagnostico.ruta}: "
                f"estado={self.estado.value} motivo={self._diagnostico.motivo}"
            )
        return self._handle

    def leer_bytes(self) -> bytes:
        """Bytes actuales del INI, leídos por el handle retenido.

        Raises:
            ExclusionNoAutorizadaError: si la exclusión no está establecida.
            Mo2IniExclusionError: si la lectura por handle falla.
        """
        return _leer_handle(self._exigir_autorizacion())

    def huella(self) -> str:
        """``sha256`` de los bytes leídos por el handle."""
        return hashlib.sha256(self.leer_bytes()).hexdigest()

    def verificar_huella(self, esperada: str) -> None:
        """Falla cerrado si el contenido actual no es el que se autorizó.

        Es el cierre de la ventana TOCTOU: la comprobación y la mutación usan el
        MISMO handle, así que ningún escritor externo puede interponerse entre
        ambas.

        Raises:
            Mo2IniExclusionError: si el contenido cambió.
        """
        actual = self.huella()
        if actual != esperada:
            raise Mo2IniExclusionError(
                f"{self._diagnostico.ruta}: el contenido cambió respecto de la huella autorizada "
                f"({actual} != {esperada}): no se muta una superficie cuya identidad no liga"
            )

    def escribir_bytes(self, datos: bytes) -> None:
        """Reemplaza el contenido del INI por ``datos``, por el handle retenido.

        La escritura NO es atómica: no hay reemplazo atómico posible mientras se
        sostiene la exclusión (ver el docstring del módulo). La recuperación ante
        una muerte a mitad la cubre ``binder_recovery``, que captura los bytes
        previos antes de mutar.

        Raises:
            ExclusionNoAutorizadaError: si la exclusión no está establecida.
            Mo2IniExclusionError: si la escritura o el volcado fallan.
        """
        _escribir_handle(self._exigir_autorizacion(), datos)

    def cerrar(self) -> None:
        """Libera el handle. Idempotente."""
        if self._handle is not None:
            _cerrar_handle(self._handle)
            self._handle = None

    def __enter__(self) -> ExclusionIni:
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()


def _diagnostico_por_error(ruta: pathlib.Path, exc: _AperturaError, intentos: int) -> DiagnosticoExclusion:
    codigo = exc.codigo
    if codigo in _ERRORES_DE_AUSENCIA:
        return DiagnosticoExclusion(
            estado=EstadoExclusion.NOT_ESTABLISHED,
            ruta=ruta,
            motivo=f"el archivo no existe (WinError {codigo}): no hay nada que excluir",
            intentos=intentos,
        )
    if codigo in _ERRORES_DE_CONTENCION:
        return DiagnosticoExclusion(
            estado=EstadoExclusion.NOT_ESTABLISHED,
            ruta=ruta,
            motivo=(
                f"otro proceso mantiene el archivo abierto con un modo incompatible (WinError {codigo}): "
                "no se puede tomar la exclusión sin desalojarlo"
            ),
            intentos=intentos,
        )
    return DiagnosticoExclusion(
        estado=EstadoExclusion.INDETERMINATE,
        ruta=ruta,
        motivo=f"la apertura exclusiva falló por un motivo no clasificado (WinError {codigo}): {exc.detalle}",
        intentos=intentos,
    )


def adquirir_exclusion_ini(
    ruta: str | os.PathLike[str],
    *,
    intentos: int = 40,
    espera_s: float = 0.005,
) -> ExclusionIni:
    """Toma la exclusión sobre ``ruta``, con reintento acotado.

    El reintento no es cosmético: un lector que tenga el archivo abierto en el
    instante del intento hace fallar la apertura con ``ERROR_SHARING_VIOLATION``
    (medido: 291 de 300 intentos bajo un lector en bucle). Con 40 reintentos y
    5 ms de espera, la misma prueba dio 0 fallos. El reintento es acotado a
    propósito: esperar indefinidamente convertiría una negativa en un cuelgue.

    Args:
        ruta: camino al ``modorganizer.ini``.
        intentos: cantidad máxima de intentos de apertura. ``>= 1``.
        espera_s: espera entre intentos.

    Returns:
        Una :class:`ExclusionIni`. El llamador DEBE consultar
        :attr:`ExclusionIni.autoriza` antes de mutar, y cerrarla siempre.
    """
    destino = pathlib.Path(os.path.abspath(os.fspath(ruta)))
    total = max(1, int(intentos))

    if _KERNEL32 is None:
        return ExclusionIni(
            None,
            DiagnosticoExclusion(
                estado=EstadoExclusion.INDETERMINATE,
                ruta=destino,
                motivo="la exclusión de archivos compartidos no existe fuera de Windows",
                intentos=0,
            ),
        )

    # Un reparse point en la ruta rompe la premisa: la identidad del path deja
    # de ser la del archivo que se muta, y el destino real queda fuera del gate.
    attrs, error_atributos = _atributos_archivo(destino)
    if attrs is None:
        if error_atributos in _ERRORES_DE_AUSENCIA:
            return ExclusionIni(
                None,
                DiagnosticoExclusion(
                    estado=EstadoExclusion.NOT_ESTABLISHED,
                    ruta=destino,
                    motivo=f"el archivo no existe (WinError {error_atributos}): no hay nada que excluir",
                    intentos=0,
                ),
            )
        return ExclusionIni(
            None,
            DiagnosticoExclusion(
                estado=EstadoExclusion.INDETERMINATE,
                ruta=destino,
                motivo=(
                    "no se pudieron leer los atributos del archivo "
                    f"(WinError {error_atributos}): no se puede descartar un reparse point"
                ),
                intentos=0,
            ),
        )
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return ExclusionIni(
            None,
            DiagnosticoExclusion(
                estado=EstadoExclusion.NOT_ESTABLISHED,
                ruta=destino,
                motivo="la ruta es un reparse point (symlink/junction): la exclusión no cubriría el destino real",
                intentos=0,
            ),
        )

    ultimo: _AperturaError | None = None
    for intento in range(1, total + 1):
        try:
            apertura = _abrir_exclusivo(destino)
        except _AperturaError as exc:
            ultimo = exc
            if exc.codigo in _ERRORES_DE_AUSENCIA:
                # No tiene sentido reintentar: el archivo no está.
                return ExclusionIni(None, _diagnostico_por_error(destino, exc, intento))
            if intento < total:
                time.sleep(espera_s)
            continue

        # La ruta pudo convertirse en enlace entre la comprobación y la apertura.
        reparse_post = es_reparse_point(destino)
        if reparse_post is None or reparse_post:
            _cerrar_handle(apertura.handle)
            return ExclusionIni(
                None,
                DiagnosticoExclusion(
                    estado=EstadoExclusion.NOT_ESTABLISHED if reparse_post else EstadoExclusion.INDETERMINATE,
                    ruta=destino,
                    motivo="la ruta cambió de naturaleza durante la apertura: no se puede afirmar qué archivo se retuvo",
                    intentos=intento,
                ),
            )

        # El handle tiene que ser el archivo que el path nombra AHORA. Si no,
        # el path se movió y la exclusión protege a otro objeto.
        try:
            en_disco = os.stat(destino)
        except OSError as exc:
            _cerrar_handle(apertura.handle)
            return ExclusionIni(
                None,
                DiagnosticoExclusion(
                    estado=EstadoExclusion.NOT_ESTABLISHED,
                    ruta=destino,
                    motivo=f"el archivo retenido dejó de estar visible en el path: {exc}",
                    intentos=intento,
                ),
            )
        if int(en_disco.st_ino) != apertura.identidad.indice:
            _cerrar_handle(apertura.handle)
            return ExclusionIni(
                None,
                DiagnosticoExclusion(
                    estado=EstadoExclusion.NOT_ESTABLISHED,
                    ruta=destino,
                    motivo=(
                        "la identidad del path no coincide con la del handle "
                        f"({en_disco.st_ino} != {apertura.identidad.indice}): el path apunta a otro archivo"
                    ),
                    intentos=intento,
                ),
            )

        return ExclusionIni(
            apertura.handle,
            DiagnosticoExclusion(
                estado=EstadoExclusion.ESTABLISHED,
                ruta=destino,
                motivo="exclusión de mutación establecida sobre el archivo retenido",
                intentos=intento,
                identidad=apertura.identidad,
            ),
        )

    assert ultimo is not None
    return ExclusionIni(None, _diagnostico_por_error(destino, ultimo, total))


@contextlib.contextmanager
def excluir_ini(
    ruta: str | os.PathLike[str],
    *,
    intentos: int = 40,
    espera_s: float = 0.005,
) -> Any:
    """Context manager sobre :func:`adquirir_exclusion_ini`.

    No oculta el estado: el cuerpo tiene que consultar ``.autoriza``. Un
    context manager que «libera y sigue» cuando no pudo excluir sería justo el
    falso PASS que este módulo existe para evitar.
    """
    exclusion = adquirir_exclusion_ini(ruta, intentos=intentos, espera_s=espera_s)
    try:
        yield exclusion
    finally:
        exclusion.cerrar()
