"""Journal durable de la config de BodySlide y su recuperación fail-closed (BodySlide PR 2b).

Diseño: ``docs/design/specs/2026-11-23-bodyslide-pr2-concepts-brief.md`` — D5, y §13 (P-4, C1),
que prevalece sobre D5 donde difieren.

**La propiedad, enunciada sobre el mecanismo:** *la configuración del usuario sólo se
reescribe cuando el journal prueba que el contenido actual es nuestro y que nadie más
puede estar escribiéndolo*. Todo lo que no se puede probar es ``HOLD`` y se preserva:
el archivo, el journal y los temporales ajenos quedan como estaban.

Qué hace falta probar para escribir (cada uno es un veredicto distinto si falta):

1. **Journal íntegro** — schema v1 cerrado (un campo de más o de menos es corrupto),
   checksum propio, nombre de archivo = instancia, sin enlaces ni segundo enlace duro.
2. **Identidad** — misma máquina (``MachineGuid``, lectura de solo lectura del
   registro; con el perfil aislado no se lee y la identidad es desconocida) y misma
   instancia (carpeta de BodySlide identificada por ruta + volumen + índice físico).
3. **Sin cuarentena** del recurso (marcador que escribe PR 2c, C3).
4. **Dueño muerto** — pid + hora de creación del proceso que abrió el journal. Vivo:
   la mutación puede estar en vuelo. Indeterminado: terminalidad desconocida.
5. **Terminalidad** — ``LANZADO`` se escribe ANTES de pedir el spawn; un journal que
   no llegó a ``LANZADO`` prueba que su dueño nunca lo pidió. ``LANZADO`` sin
   ``TERMINAL`` es ``HOLD``: el reinicio de la máquina (``arranque_del_sistema``) se
   registra como dato y **por sí solo no autoriza** (P-4).
6. **Bytes** — cada archivo se clasifica ANTES de escribir nada: igual al original
   (no se toca), igual a un contenido esperado registrado (se restaura) o tercero
   (``CONCURRENT_MODIFICATION``: no se toca ninguno).

La restauración es reintentable: si falla, el journal queda durable en
``RESTAURACION_FALLIDA`` y el próximo arranque repite la clasificación; un archivo ya
restaurado se clasifica como original y no se vuelve a escribir.

**Fuera de alcance (PR 2c):** plazo de la siembra, escritura del marcador de
cuarentena y la evidencia de terminalidad del broker (``worker_exit``). El estado
``SIEMBRA_INDETERMINADA`` y el marcador existen acá para que 2c los use sin rediseñar
el journal. Este módulo no tiene call sites productivos: lo invoca sólo el consumidor
``bodyslide-config`` del reconciliador, que a su vez todavía no está cableado.

**Límites declarados:** en Windows no hay ``fsync`` de directorio (la entrada del
``os.replace`` queda a cargo del journaling de NTFS); cada escritura se relee y se
verifica. El checksum detecta corrupción y edición accidental, no a un adversario con
escritura sobre el directorio de estado: esa frontera es el ACL de solo-dueño.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import enum
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import stat
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import psutil

from sky_claw.aislamiento import perfil_aislado
from sky_claw.app.security.file_permissions import restrict_to_owner
from sky_claw.app.security.links import link_kind_or_raise, path_present
from sky_claw.config import SystemPaths
from sky_claw.local.frozen_runtime.root_lock import OwnerLiveness, _check_process_liveness

logger = logging.getLogger("SkyClaw.BodySlideJournal")

SCHEMA_VERSION: Final[int] = 1
NOMBRE_DEL_DIRECTORIO: Final[str] = "bodyslide_journal"

#: Los mismos tres archivos que el runner snapshotea (``bodyslide_runner._ARCHIVOS_DE_CONFIGURACION``).
ARCHIVOS_JOURNALEADOS: Final[tuple[str, ...]] = ("Config.xml", "BodySlide.xml", "BuildSelection.xml")
#: El único que Sky-Claw escribe antes del spawn.
ARCHIVO_SEMBRADO: Final[str] = "Config.xml"
#: Huella de "el archivo no existe": original ausente o borrado por la corrida.
AUSENTE: Final[str] = "ausente"
#: Son XML de configuración chicos; uno más grande no es de BodySlide y no se journalea.
MAX_BYTES_POR_ARCHIVO: Final[int] = 4 * 1024 * 1024
_MAX_BYTES_DEL_JOURNAL: Final[int] = 8 * MAX_BYTES_POR_ARCHIVO
_MAX_ESPERADOS: Final[int] = 4
NOMBRE_DEL_MARCADOR_DE_CUARENTENA: Final[str] = "bodyslide-meshes.cuarentena.json"

_HEX32: Final = re.compile(r"[0-9a-f]{32}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_NOMBRE_DE_JOURNAL: Final = re.compile(r"([0-9a-f]{32})\.json")
#: Temporal que dejan la siembra y la restauración (mismo patrón que ``bodyslide_config``).
_TEMPORAL_PROPIO: Final = re.compile(r"(Config\.xml|BodySlide\.xml|BuildSelection\.xml)\.[0-9a-f]{32}\.tmp")
_O_BINARY: Final[int] = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_REINTENTOS_DE_REEMPLAZO: Final[int] = 5


class EstadoDelJournal(enum.StrEnum):
    """Estado transaccional. Congelado por igualdad literal en ``tests/test_bodyslide_journal.py``."""

    PREPARADO = "PREPARADO"
    #: Reservado para PR 2c (C3): el plazo de la siembra venció con el hilo vivo.
    SIEMBRA_INDETERMINADA = "SIEMBRA_INDETERMINADA"
    SEMBRADO = "SEMBRADO"
    LANZADO = "LANZADO"
    TERMINAL = "TERMINAL"
    RESTAURACION_FALLIDA = "RESTAURACION_FALLIDA"
    RESTAURADO = "RESTAURADO"


_E = EstadoDelJournal

#: Transiciones autorizadas. ``LANZADO`` sólo sale a ``TERMINAL``: sin terminalidad
#: registrada no existe ningún camino a restaurar. ``RESTAURACION_FALLIDA`` sólo se
#: alcanza desde estados con terminalidad probada (nunca lanzado, o terminal).
TRANSICIONES: Final[Mapping[EstadoDelJournal, frozenset[EstadoDelJournal]]] = types.MappingProxyType(
    {
        _E.PREPARADO: frozenset({_E.SEMBRADO, _E.SIEMBRA_INDETERMINADA, _E.RESTAURADO, _E.RESTAURACION_FALLIDA}),
        _E.SIEMBRA_INDETERMINADA: frozenset({_E.SEMBRADO, _E.RESTAURADO, _E.RESTAURACION_FALLIDA}),
        _E.SEMBRADO: frozenset({_E.LANZADO, _E.RESTAURADO, _E.RESTAURACION_FALLIDA}),
        _E.LANZADO: frozenset({_E.TERMINAL}),
        _E.TERMINAL: frozenset({_E.RESTAURADO, _E.RESTAURACION_FALLIDA}),
        _E.RESTAURACION_FALLIDA: frozenset({_E.RESTAURADO, _E.RESTAURACION_FALLIDA}),
        _E.RESTAURADO: frozenset(),
    }
)

#: Desde dónde puede restaurar la transacción VIVA (su propio proceso prueba que no hay
#: otro escritor). ``SIEMBRA_INDETERMINADA`` queda afuera: el hilo puede seguir vivo (C3).
_RESTAURABLES_EN_VIVO: Final[frozenset[EstadoDelJournal]] = frozenset(
    {_E.PREPARADO, _E.SEMBRADO, _E.TERMINAL, _E.RESTAURACION_FALLIDA}
)

CLAVES_DEL_JOURNAL: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "run_id",
        "generacion",
        "estado",
        "maquina",
        "instancia",
        "directorio",
        "directorio_volumen",
        "directorio_indice",
        "duenio_pid",
        "duenio_creado",
        "arranque_del_sistema",
        "archivos",
        "integridad",
    }
)
CLAVES_DE_ARCHIVO: Final[frozenset[str]] = frozenset({"nombre", "original_b64", "original_sha256", "esperados_sha256"})


class Veredicto(enum.StrEnum):
    """Resultado de una recuperación. Los tres compartidos se llaman igual que en ``EstadoBodySlide``."""

    RESTAURADO = "RESTAURADO"
    SIN_CAMBIOS = "SIN_CAMBIOS"
    SIN_JOURNAL = "SIN_JOURNAL"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    RESTORE_FAILED = "RESTORE_FAILED"
    HOLD_TERMINALITY_UNKNOWN = "HOLD_TERMINALITY_UNKNOWN"
    HOLD_MUTACION_EN_VUELO = "HOLD_MUTACION_EN_VUELO"
    HOLD_JOURNAL_CORRUPTO = "HOLD_JOURNAL_CORRUPTO"
    HOLD_IDENTIDAD = "HOLD_IDENTIDAD"
    HOLD_RUTA_NO_CONFIABLE = "HOLD_RUTA_NO_CONFIABLE"
    HOLD_CUARENTENA = "HOLD_CUARENTENA"


class JournalError(Exception):
    """Base de los errores del journal: la transacción no puede seguir sin violar la propiedad."""


class JournalCorruptoError(JournalError):
    """El journal no es legible, no es del schema v1 cerrado o no es un archivo regular propio."""


class JournalOcupadoError(JournalError):
    """Ya hay un journal pendiente para esta instancia: primero hay que recuperarlo."""


class TransicionNoAutorizadaError(JournalError):
    """La transición pedida no está en ``TRANSICIONES``."""


class ModificacionConcurrenteError(JournalError):
    """Un archivo cambió por fuera de la transacción: no se escribe encima."""


class PrecondicionDelJournalError(JournalError):
    """No se puede abrir una transacción segura (identidad, enlaces, tamaño)."""


@dataclass(frozen=True, slots=True)
class IdentidadDeEjecucion:
    """Máquina e instancia de BodySlide a las que pertenece un journal."""

    maquina: str
    instancia: str


@dataclass(frozen=True, slots=True)
class IdentidadDeProceso:
    """Dueño del journal: pid más hora de creación (un pid solo se recicla)."""

    pid: int
    creado: float

    @classmethod
    def actual(cls) -> IdentidadDeProceso:
        proceso = psutil.Process(os.getpid())
        return cls(pid=proceso.pid, creado=proceso.create_time())


@dataclass(frozen=True, slots=True)
class InformeDeRecuperacion:
    """Qué decidió la recuperación de un journal. ``restaurados`` son nombres de archivo."""

    veredicto: Veredicto
    journal: pathlib.Path
    detalle: str = ""
    restaurados: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchivoDelJournal:
    nombre: str
    original: bytes | None
    original_sha256: str | None
    esperados_sha256: tuple[str, ...]

    @property
    def huella_original(self) -> str:
        return self.original_sha256 if self.original_sha256 is not None else AUSENTE

    def como_dict(self) -> dict[str, Any]:
        return {
            "nombre": self.nombre,
            "original_b64": None if self.original is None else base64.b64encode(self.original).decode("ascii"),
            "original_sha256": self.original_sha256,
            "esperados_sha256": list(self.esperados_sha256),
        }


@dataclass(frozen=True, slots=True)
class DocumentoDeJournal:
    run_id: str
    generacion: str
    estado: EstadoDelJournal
    maquina: str
    instancia: str
    directorio: str
    directorio_volumen: int
    directorio_indice: int
    duenio_pid: int
    duenio_creado: float
    arranque_del_sistema: float
    archivos: tuple[ArchivoDelJournal, ...]

    def como_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "generacion": self.generacion,
            "estado": self.estado.value,
            "maquina": self.maquina,
            "instancia": self.instancia,
            "directorio": self.directorio,
            "directorio_volumen": self.directorio_volumen,
            "directorio_indice": self.directorio_indice,
            "duenio_pid": self.duenio_pid,
            "duenio_creado": self.duenio_creado,
            "arranque_del_sistema": self.arranque_del_sistema,
            "archivos": [a.como_dict() for a in self.archivos],
        }


# ---------------------------------------------------------------------------
# Identidad
# ---------------------------------------------------------------------------


def identidad_de_maquina() -> str | None:
    """``MachineGuid`` (Windows) o ``/etc/machine-id``; ``None`` si no se puede saber.

    Con el perfil aislado no se lee la máquina real (``AGENTS.md``, perfil aislado): la
    identidad queda desconocida y toda recuperación es ``HOLD_IDENTIDAD``.
    """
    if perfil_aislado():
        return None
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as clave:
                valor, _tipo = winreg.QueryValueEx(clave, "MachineGuid")
        except OSError:
            return None
        return valor if isinstance(valor, str) and valor else None
    try:
        texto = pathlib.Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return texto or None


def _identidad_fisica(directorio: pathlib.Path) -> tuple[str, int, int]:
    """Ruta canónica + volumen + índice del directorio. Un enlace no tiene identidad propia."""
    if link_kind_or_raise(directorio) is not None:
        raise PrecondicionDelJournalError(f"'{directorio}' es un enlace: no se journalea a través de enlaces")
    absoluto = os.path.normcase(os.path.abspath(directorio))
    estado = os.lstat(absoluto)
    if not stat.S_ISDIR(estado.st_mode):
        raise PrecondicionDelJournalError(f"'{directorio}' no es un directorio")
    return absoluto, estado.st_dev, estado.st_ino


def identidad_de_instancia(exe_dir: pathlib.Path) -> str:
    """Identidad de la instalación de BodySlide: ruta canónica, volumen e índice físico."""
    absoluto, volumen, indice = _identidad_fisica(exe_dir)
    return hashlib.sha256(f"{absoluto}\0{volumen}\0{indice}".encode()).hexdigest()[:32]


def identidad_actual(exe_dir: pathlib.Path) -> IdentidadDeEjecucion | None:
    """Identidad de esta máquina e instancia, o ``None`` si alguna no se puede establecer."""
    maquina = identidad_de_maquina()
    if maquina is None:
        return None
    try:
        return IdentidadDeEjecucion(maquina=maquina, instancia=identidad_de_instancia(exe_dir))
    except (OSError, JournalError):
        return None


# ---------------------------------------------------------------------------
# Serialización y validación del schema v1 cerrado
# ---------------------------------------------------------------------------


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _canonico(datos: Mapping[str, Any]) -> bytes:
    return json.dumps(datos, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _con_integridad(datos: Mapping[str, Any]) -> dict[str, Any]:
    """Copia de ``datos`` con ``integridad`` recalculada sobre el resto de los campos."""
    sin = {k: v for k, v in datos.items() if k != "integridad"}
    return {**sin, "integridad": _sha256(_canonico(sin))}


def _serializar(datos: Mapping[str, Any]) -> bytes:
    return json.dumps(datos, sort_keys=True, indent=2, ensure_ascii=True).encode("ascii") + b"\n"


def _corrupto(ruta: pathlib.Path, razon: str) -> JournalCorruptoError:
    return JournalCorruptoError(f"journal '{ruta.name}' corrupto: {razon}")


def _entero(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _real_finito(valor: Any) -> bool:
    return (isinstance(valor, float) or _entero(valor)) and math.isfinite(valor)


def _texto_hex(valor: Any, patron: re.Pattern[str]) -> bool:
    return isinstance(valor, str) and patron.fullmatch(valor) is not None


def _parsear_archivo(ruta: pathlib.Path, crudo: Any, nombre_esperado: str) -> ArchivoDelJournal:
    if not isinstance(crudo, dict) or set(crudo) != CLAVES_DE_ARCHIVO:
        raise _corrupto(ruta, f"entrada de archivo fuera de schema ({nombre_esperado})")
    if crudo["nombre"] != nombre_esperado:
        raise _corrupto(ruta, f"archivo inesperado {crudo['nombre']!r} (se esperaba {nombre_esperado!r})")
    b64, sha = crudo["original_b64"], crudo["original_sha256"]
    original: bytes | None
    if b64 is None and sha is None:
        original = None
    elif isinstance(b64, str) and _texto_hex(sha, _HEX64):
        try:
            original = base64.b64decode(b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise _corrupto(ruta, f"base64 inválido en {nombre_esperado}") from exc
        if len(original) > MAX_BYTES_POR_ARCHIVO or _sha256(original) != sha:
            raise _corrupto(ruta, f"bytes originales de {nombre_esperado} no coinciden con su hash")
    else:
        raise _corrupto(ruta, f"original de {nombre_esperado} incoherente")
    esperados = crudo["esperados_sha256"]
    if (
        not isinstance(esperados, list)
        or len(esperados) > _MAX_ESPERADOS
        or len(set(esperados)) != len(esperados)
        or not all(e == AUSENTE or _texto_hex(e, _HEX64) for e in esperados)
    ):
        raise _corrupto(ruta, f"esperados de {nombre_esperado} fuera de schema")
    return ArchivoDelJournal(
        nombre=nombre_esperado,
        original=original,
        original_sha256=sha,
        esperados_sha256=tuple(esperados),
    )


def _parsear(ruta: pathlib.Path, crudo: bytes) -> DocumentoDeJournal:
    """Valida el schema v1 por IGUALDAD de claves y el checksum; cualquier desvío es corrupto."""
    try:
        datos = json.loads(crudo.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _corrupto(ruta, f"no es JSON legible ({exc})") from exc
    if not isinstance(datos, dict) or set(datos) != CLAVES_DEL_JOURNAL:
        raise _corrupto(ruta, "claves fuera del schema v1")
    if datos["integridad"] != _sha256(_canonico({k: v for k, v in datos.items() if k != "integridad"})):
        raise _corrupto(ruta, "el checksum de integridad no coincide")
    if not _entero(datos["schema_version"]) or datos["schema_version"] != SCHEMA_VERSION:
        raise _corrupto(ruta, f"schema_version no soportada: {datos['schema_version']!r}")
    for clave in ("run_id", "generacion", "instancia"):
        if not _texto_hex(datos[clave], _HEX32):
            raise _corrupto(ruta, f"{clave} inválido")
    nombre = _NOMBRE_DE_JOURNAL.fullmatch(ruta.name)
    if nombre is None or nombre.group(1) != datos["instancia"]:
        raise _corrupto(ruta, "la instancia no coincide con el nombre del journal")
    if datos["estado"] not in {e.value for e in EstadoDelJournal}:
        raise _corrupto(ruta, f"estado desconocido {datos['estado']!r}")
    if not isinstance(datos["maquina"], str) or not datos["maquina"]:
        raise _corrupto(ruta, "maquina inválida")
    if not isinstance(datos["directorio"], str) or not os.path.isabs(datos["directorio"]):
        raise _corrupto(ruta, "directorio no absoluto")
    for clave in ("directorio_volumen", "directorio_indice"):
        if not _entero(datos[clave]) or datos[clave] < 0:
            raise _corrupto(ruta, f"{clave} inválido")
    # pid > 0: un pid <= 0 se lee como "dueño muerto" y autorizaría a restaurar.
    if not _entero(datos["duenio_pid"]) or datos["duenio_pid"] <= 0:
        raise _corrupto(ruta, "duenio_pid inválido")
    for clave in ("duenio_creado", "arranque_del_sistema"):
        if not _real_finito(datos[clave]):
            raise _corrupto(ruta, f"{clave} inválido")
    archivos = datos["archivos"]
    if not isinstance(archivos, list) or len(archivos) != len(ARCHIVOS_JOURNALEADOS):
        raise _corrupto(ruta, "lista de archivos fuera de schema")
    return DocumentoDeJournal(
        run_id=datos["run_id"],
        generacion=datos["generacion"],
        estado=EstadoDelJournal(datos["estado"]),
        maquina=datos["maquina"],
        instancia=datos["instancia"],
        directorio=datos["directorio"],
        directorio_volumen=datos["directorio_volumen"],
        directorio_indice=datos["directorio_indice"],
        duenio_pid=datos["duenio_pid"],
        duenio_creado=float(datos["duenio_creado"]),
        arranque_del_sistema=float(datos["arranque_del_sistema"]),
        archivos=tuple(
            _parsear_archivo(ruta, crudo_archivo, nombre_archivo)
            for crudo_archivo, nombre_archivo in zip(archivos, ARCHIVOS_JOURNALEADOS, strict=True)
        ),
    )


# ---------------------------------------------------------------------------
# Primitivas de disco
# ---------------------------------------------------------------------------


def _fsync_directorio(directorio: pathlib.Path) -> None:
    """Durabilidad de la entrada de directorio en POSIX. Windows no expone ``fsync`` de directorio."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(directorio, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _escribir_y_sincronizar(ruta: pathlib.Path, blob: bytes) -> None:
    """Crea ``ruta`` con ``O_EXCL`` (nunca reemplaza), escribe todo y hace ``fsync``."""
    fd = os.open(ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
    try:
        vista = memoryview(blob)
        while vista:
            escritos = os.write(fd, vista)
            vista = vista[escritos:]
        os.fsync(fd)
    except BaseException:
        # Un archivo a medio escribir no queda en disco (disco lleno, AV): ni un
        # temporal huérfano ni un journal truncado que bloquee la instancia.
        os.close(fd)
        with contextlib.suppress(OSError):
            ruta.unlink()
        raise
    os.close(fd)


def _reemplazar_con_reintentos(origen: pathlib.Path, destino: pathlib.Path) -> None:
    """``os.replace`` tolerando el bloqueo transitorio de un lector concurrente en Windows."""
    espera = 0.02
    for intento in range(1, _REINTENTOS_DE_REEMPLAZO + 1):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == _REINTENTOS_DE_REEMPLAZO:
                raise
            time.sleep(espera)
            espera *= 2


def _crear_temporal(destino: pathlib.Path, blob: bytes) -> pathlib.Path:
    temporal = destino.with_name(f"{destino.name}.{uuid.uuid4().hex}.tmp")
    _escribir_y_sincronizar(temporal, blob)
    return temporal


def _publicar_temporal(temporal: pathlib.Path, destino: pathlib.Path) -> None:
    try:
        _reemplazar_con_reintentos(temporal, destino)
    except BaseException:
        with contextlib.suppress(OSError):
            temporal.unlink()
        raise
    _fsync_directorio(destino.parent)


def _reemplazar_atomico(destino: pathlib.Path, blob: bytes) -> None:
    """Temporal propio en el mismo directorio + ``fsync`` + ``os.replace``: nunca un archivo truncado."""
    _publicar_temporal(_crear_temporal(destino, blob), destino)


def _retirar(destino: pathlib.Path) -> None:
    destino.unlink()
    _fsync_directorio(destino.parent)


def _tipo_de_enlace(ruta: pathlib.Path) -> str | None:
    """``AUSENTE`` si no existe, el tipo de enlace si es uno, ``None`` si es una entrada real.

    ``link_kind_or_raise`` devuelve ``None`` tanto para "no es enlace" como para "no
    existe"; acá las dos respuestas deciden cosas distintas, así que se separan con un
    ``lstat`` (que no sigue el enlace).
    """
    try:
        os.lstat(ruta)
    except FileNotFoundError:
        return AUSENTE
    return link_kind_or_raise(ruta)


def _huella_actual(destino: pathlib.Path) -> str:
    """SHA-256 del archivo, ``AUSENTE`` si no existe. Un enlace o algo que no es archivo es ajeno."""
    tipo = _tipo_de_enlace(destino)
    if tipo == AUSENTE:
        return AUSENTE
    if tipo is not None:
        raise ModificacionConcurrenteError(f"'{destino.name}' es un enlace")
    fd = os.open(destino, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
    try:
        estado = os.fstat(fd)
        if not stat.S_ISREG(estado.st_mode):
            raise ModificacionConcurrenteError(f"'{destino.name}' no es un archivo regular")
        if estado.st_size > MAX_BYTES_POR_ARCHIVO:
            raise ModificacionConcurrenteError(f"'{destino.name}' excede el tamaño de una config de BodySlide")
        with os.fdopen(fd, "rb", closefd=False) as archivo:
            return _sha256(archivo.read(MAX_BYTES_POR_ARCHIVO + 1))
    finally:
        os.close(fd)


def _leer_original(destino: pathlib.Path) -> bytes | None:
    tipo = _tipo_de_enlace(destino)
    if tipo == AUSENTE:
        return None
    if tipo is not None:
        raise PrecondicionDelJournalError(f"'{destino}' es un enlace: la siembra no escribe a través de enlaces")
    if not destino.is_file():
        raise PrecondicionDelJournalError(f"'{destino}' no es un archivo regular")
    blob = destino.read_bytes()
    if len(blob) > MAX_BYTES_POR_ARCHIVO:
        raise PrecondicionDelJournalError(f"'{destino}' excede {MAX_BYTES_POR_ARCHIVO} bytes")
    return blob


def _retirar_temporales_huerfanos(exe_dir: pathlib.Path) -> None:
    """Retira sólo temporales con el patrón propio que sean archivos regulares (C1 paso 2)."""
    try:
        entradas = list(exe_dir.iterdir())
    except OSError:
        return
    for entrada in entradas:
        if _TEMPORAL_PROPIO.fullmatch(entrada.name) is None:
            continue
        try:
            if link_kind_or_raise(entrada) is None and entrada.is_file():
                entrada.unlink()
        except OSError:
            logger.warning("No se pudo retirar el temporal huérfano '%s'", entrada, exc_info=True)


# ---------------------------------------------------------------------------
# Almacén
# ---------------------------------------------------------------------------


class AlmacenDeJournal:
    """Directorio de journals, uno por instancia, bajo ``SystemPaths.runtime_state_dir()``.

    La base es durable y no relativa al cwd (misma lección que ADR 0011). El directorio
    se endurece a solo-dueño al crearlo; en Windows el ACL ``(OI)(CI)`` lo heredan los
    journals que se crean adentro.
    """

    def __init__(
        self,
        directorio: pathlib.Path | None = None,
        *,
        endurecer: Callable[[pathlib.Path], None] = restrict_to_owner,
    ) -> None:
        self._directorio = directorio
        self._endurecer = endurecer

    @property
    def directorio(self) -> pathlib.Path:
        if self._directorio is not None:
            return self._directorio
        return SystemPaths.runtime_state_dir() / NOMBRE_DEL_DIRECTORIO

    @property
    def marcador_de_cuarentena(self) -> pathlib.Path:
        """Marcador persistente de cuarentena del recurso (lo escribe PR 2c; acá sólo se respeta)."""
        return self.directorio / NOMBRE_DEL_MARCADOR_DE_CUARENTENA

    def ruta_de(self, instancia: str) -> pathlib.Path:
        if _HEX32.fullmatch(instancia) is None:
            raise ValueError(f"instancia inválida: {instancia!r}")
        return self.directorio / f"{instancia}.json"

    def listar(self) -> list[pathlib.Path]:
        try:
            entradas = list(self.directorio.iterdir())
        except FileNotFoundError:
            return []
        return sorted(e for e in entradas if _NOMBRE_DE_JOURNAL.fullmatch(e.name) is not None)

    def asegurar_directorio(self) -> pathlib.Path:
        directorio = self.directorio
        if _tipo_de_enlace(directorio) not in {None, AUSENTE}:
            raise PrecondicionDelJournalError(f"el directorio de journals '{directorio}' es un enlace")
        try:
            directorio.mkdir(parents=True)
        except FileExistsError:
            pass
        else:
            self._endurecer(directorio)
        if link_kind_or_raise(directorio) is not None or not directorio.is_dir():
            raise PrecondicionDelJournalError(f"el directorio de journals '{directorio}' no es un directorio propio")
        return directorio

    def leer(self, ruta: pathlib.Path) -> DocumentoDeJournal:
        """Lee y valida un journal. ``FileNotFoundError`` si no existe; ``JournalCorruptoError`` si no es válido."""
        if ruta.parent != self.directorio or _NOMBRE_DE_JOURNAL.fullmatch(ruta.name) is None:
            raise _corrupto(ruta, "ruta fuera del directorio de journals")
        try:
            tipo = _tipo_de_enlace(ruta)
        except OSError as exc:
            raise _corrupto(ruta, f"no se pudo inspeccionar ({exc})") from exc
        if tipo == AUSENTE:
            raise FileNotFoundError(ruta)
        if tipo is not None:
            raise _corrupto(ruta, "es un enlace")
        try:
            fd = os.open(ruta, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _corrupto(ruta, f"no se pudo abrir ({exc})") from exc
        try:
            estado = os.fstat(fd)
            if not stat.S_ISREG(estado.st_mode):
                raise _corrupto(ruta, "no es un archivo regular")
            if estado.st_nlink != 1:
                raise _corrupto(ruta, f"tiene {estado.st_nlink} enlaces duros")
            if estado.st_size > _MAX_BYTES_DEL_JOURNAL:
                raise _corrupto(ruta, "excede el tamaño máximo")
            with os.fdopen(fd, "rb", closefd=False) as archivo:
                crudo = archivo.read(_MAX_BYTES_DEL_JOURNAL + 1)
        finally:
            os.close(fd)
        return _parsear(ruta, crudo)


def _escribir_documento(ruta: pathlib.Path, documento: DocumentoDeJournal, *, exclusivo: bool) -> None:
    blob = _serializar(_con_integridad(documento.como_dict()))
    if exclusivo:
        _escribir_y_sincronizar(ruta, blob)
        _fsync_directorio(ruta.parent)
        return
    temporal = ruta.with_name(f".{ruta.name}.{uuid.uuid4().hex}.tmp")
    _escribir_y_sincronizar(temporal, blob)
    _publicar_temporal(temporal, ruta)


def _verificar_durable(almacen: AlmacenDeJournal, ruta: pathlib.Path, esperado: DocumentoDeJournal) -> None:
    """Relee lo escrito: la durabilidad se verifica, no se supone."""
    if almacen.leer(ruta) != esperado:
        raise JournalError(f"el journal '{ruta.name}' releído no coincide con lo escrito")


def _transicionar(
    almacen: AlmacenDeJournal,
    ruta: pathlib.Path,
    actual: DocumentoDeJournal,
    destino: EstadoDelJournal,
    *,
    archivos: tuple[ArchivoDelJournal, ...] | None = None,
) -> DocumentoDeJournal:
    """Escribe ``destino`` sólo si está autorizado y el journal en disco sigue siendo ``actual``."""
    if destino not in TRANSICIONES[actual.estado]:
        raise TransicionNoAutorizadaError(f"{actual.estado.value} → {destino.value} no está autorizada")
    try:
        en_disco = almacen.leer(ruta)
    except FileNotFoundError as exc:
        raise JournalError(f"el journal '{ruta.name}' desapareció a mitad de la transacción") from exc
    if en_disco != actual:
        raise JournalError(f"el journal '{ruta.name}' fue reemplazado por otra generación")
    nuevo = dataclasses.replace(
        actual,
        estado=destino,
        generacion=uuid.uuid4().hex,
        archivos=actual.archivos if archivos is None else archivos,
    )
    _escribir_documento(ruta, nuevo, exclusivo=False)
    _verificar_durable(almacen, ruta, nuevo)
    return nuevo


def _cerrar(ruta: pathlib.Path, exe_dir: pathlib.Path) -> None:
    """Retira el journal ya RESTAURADO, sus temporales propios y los temporales propios de la config."""
    _retirar(ruta)
    patron = re.compile(re.escape(f".{ruta.name}.") + r"[0-9a-f]{32}\.tmp")
    for entrada in ruta.parent.iterdir():
        if patron.fullmatch(entrada.name) is None:
            continue
        with contextlib.suppress(OSError):
            if _tipo_de_enlace(entrada) is None and entrada.is_file():
                entrada.unlink()
    _retirar_temporales_huerfanos(exe_dir)


def _restaurar(
    almacen: AlmacenDeJournal,
    ruta: pathlib.Path,
    documento: DocumentoDeJournal,
    exe_dir: pathlib.Path,
) -> tuple[InformeDeRecuperacion, DocumentoDeJournal]:
    """Clasifica todo, después restaura lo nuestro; un tercero frena todo. Reintentable."""
    nuestros: list[ArchivoDelJournal] = []
    for archivo in documento.archivos:
        destino = exe_dir / archivo.nombre
        try:
            huella = _huella_actual(destino)
        except ModificacionConcurrenteError as exc:
            return InformeDeRecuperacion(Veredicto.CONCURRENT_MODIFICATION, ruta, str(exc)), documento
        except OSError as exc:
            documento = _transicionar(almacen, ruta, documento, _E.RESTAURACION_FALLIDA)
            return InformeDeRecuperacion(Veredicto.RESTORE_FAILED, ruta, f"{archivo.nombre}: {exc}"), documento
        if huella == archivo.huella_original:
            continue
        if huella in archivo.esperados_sha256:
            nuestros.append(archivo)
            continue
        logger.warning(
            "BodySlide: '%s' tiene un contenido que la corrida %s no escribió; no se restaura nada.",
            destino,
            documento.run_id,
        )
        return (
            InformeDeRecuperacion(Veredicto.CONCURRENT_MODIFICATION, ruta, f"{archivo.nombre}: contenido de terceros"),
            documento,
        )

    # Un fallo en un archivo NO corta la restauración de los demás (review CodeRabbit
    # #1003, propiedad heredada de PR 1): cada archivo restaurado deja de ser "nuestro"
    # y el reintento sólo vuelve sobre los que fallaron.
    restaurados: list[str] = []
    fallos: list[str] = []
    for archivo in nuestros:
        destino = exe_dir / archivo.nombre
        try:
            if archivo.original is None:
                _retirar(destino)
            else:
                _reemplazar_atomico(destino, archivo.original)
            if _huella_actual(destino) != archivo.huella_original:
                raise OSError(f"'{archivo.nombre}' no quedó igual al original tras restaurar")
        except (OSError, ModificacionConcurrenteError) as exc:
            fallos.append(f"{archivo.nombre}: {exc}")
            continue
        restaurados.append(archivo.nombre)
    if fallos:
        detalle = "; ".join(fallos)
        logger.error("BodySlide: falló la restauración de la corrida %s: %s", documento.run_id, detalle)
        documento = _transicionar(almacen, ruta, documento, _E.RESTAURACION_FALLIDA)
        return InformeDeRecuperacion(Veredicto.RESTORE_FAILED, ruta, detalle, tuple(restaurados)), documento

    documento = _transicionar(almacen, ruta, documento, _E.RESTAURADO)
    _cerrar(ruta, exe_dir)
    veredicto = Veredicto.RESTAURADO if restaurados else Veredicto.SIN_CAMBIOS
    return InformeDeRecuperacion(veredicto, ruta, restaurados=tuple(restaurados)), documento


# ---------------------------------------------------------------------------
# Transacción viva
# ---------------------------------------------------------------------------


class TransaccionDeConfig:
    """Una corrida con su journal. Cada método deja el estado durable ANTES de volver."""

    def __init__(
        self,
        *,
        almacen: AlmacenDeJournal,
        ruta: pathlib.Path,
        documento: DocumentoDeJournal,
        exe_dir: pathlib.Path,
        bytes_sembrados: bytes,
    ) -> None:
        self._almacen = almacen
        self._ruta = ruta
        self._documento = documento
        self._exe_dir = exe_dir
        self._bytes_sembrados = bytes_sembrados

    @property
    def ruta(self) -> pathlib.Path:
        return self._ruta

    @property
    def estado(self) -> EstadoDelJournal:
        return self._documento.estado

    @property
    def run_id(self) -> str:
        return self._documento.run_id

    @property
    def almacen(self) -> AlmacenDeJournal:
        return self._almacen

    def _tras_escribir_temporal(self) -> None:
        """Gancho de observación (tests de crash): el temporal de la siembra existe, el destino no cambió."""

    def _tras_reemplazar(self) -> None:
        """Gancho de observación (tests de crash): la siembra reemplazó el destino, falta registrar SEMBRADO."""

    def _exigir(self, destino: EstadoDelJournal) -> None:
        if destino not in TRANSICIONES[self.estado]:
            raise TransicionNoAutorizadaError(f"{self.estado.value} → {destino.value} no está autorizada")

    def _archivo(self, nombre: str) -> ArchivoDelJournal:
        return next(a for a in self._documento.archivos if a.nombre == nombre)

    def _verificar_journal(self) -> None:
        """El journal en disco sigue siendo el que esta transacción escribió (nadie lo reemplazó)."""
        try:
            en_disco = self._almacen.leer(self._ruta)
        except FileNotFoundError as exc:
            raise JournalError(f"el journal '{self._ruta.name}' desapareció a mitad de la transacción") from exc
        if en_disco != self._documento:
            raise JournalError(f"el journal '{self._ruta.name}' fue reemplazado por otra generación")

    def sembrar(self) -> None:
        """Escribe la siembra registrada en PREPARADO y deja SEMBRADO durable."""
        self._exigir(_E.SEMBRADO)
        self._verificar_journal()
        destino = self._exe_dir / ARCHIVO_SEMBRADO
        archivo = self._archivo(ARCHIVO_SEMBRADO)
        if _huella_actual(destino) != archivo.huella_original:
            raise ModificacionConcurrenteError(f"'{destino}' cambió desde que se registró el original")
        temporal = _crear_temporal(destino, self._bytes_sembrados)
        self._tras_escribir_temporal()
        _publicar_temporal(temporal, destino)
        self._tras_reemplazar()
        if _huella_actual(destino) != _sha256(self._bytes_sembrados):
            raise ModificacionConcurrenteError(f"'{destino}' no quedó con la siembra registrada")
        self._documento = _transicionar(self._almacen, self._ruta, self._documento, _E.SEMBRADO)

    def marcar_lanzado(self) -> None:
        """Se llama ANTES de pedir el spawn: sin esto, un crash no distingue "no arrancó"."""
        self._documento = _transicionar(self._almacen, self._ruta, self._documento, _E.LANZADO)

    def marcar_terminal(self) -> None:
        """Registra la terminalidad y lo que la corrida dejó en cada archivo (lo que luego se restaura)."""
        self._exigir(_E.TERMINAL)
        archivos = []
        for archivo in self._documento.archivos:
            observado = _huella_actual(self._exe_dir / archivo.nombre)
            esperados = () if observado == archivo.huella_original else (observado,)
            archivos.append(dataclasses.replace(archivo, esperados_sha256=esperados))
        self._documento = _transicionar(
            self._almacen, self._ruta, self._documento, _E.TERMINAL, archivos=tuple(archivos)
        )

    def restaurar(self) -> InformeDeRecuperacion:
        """Restauración desde el proceso dueño (su vida prueba que no hay otro escritor)."""
        if self.estado not in _RESTAURABLES_EN_VIVO:
            raise TransicionNoAutorizadaError(f"no se restaura en vivo desde {self.estado.value}")
        self._verificar_journal()
        informe, self._documento = _restaurar(self._almacen, self._ruta, self._documento, self._exe_dir)
        return informe

    def abortar_sin_mutar(self) -> InformeDeRecuperacion:
        """Cierra un journal que nunca llegó a sembrar (precondición fallida antes de escribir)."""
        if self.estado is not _E.PREPARADO:
            raise TransicionNoAutorizadaError(f"abortar sin mutar exige PREPARADO, no {self.estado.value}")
        return self.restaurar()


def iniciar_transaccion(
    *,
    almacen: AlmacenDeJournal,
    identidad: IdentidadDeEjecucion,
    exe_dir: pathlib.Path,
    bytes_sembrados: bytes,
    duenio: IdentidadDeProceso | None = None,
) -> TransaccionDeConfig:
    """Snapshotea los originales y deja PREPARADO durable ANTES de cualquier mutación.

    Exclusivo por instancia (``O_EXCL``): con un journal pendiente lanza
    ``JournalOcupadoError`` y no toca nada.
    """
    if not identidad.maquina:
        raise PrecondicionDelJournalError("identidad de máquina desconocida: no se abre una transacción irrecuperable")
    absoluto, volumen, indice = _identidad_fisica(exe_dir)
    if identidad.instancia != identidad_de_instancia(exe_dir):
        raise PrecondicionDelJournalError("la identidad de instancia no corresponde a la carpeta de BodySlide")
    if len(bytes_sembrados) > MAX_BYTES_POR_ARCHIVO:
        raise PrecondicionDelJournalError("la siembra excede el tamaño de una config de BodySlide")
    huella_sembrada = _sha256(bytes_sembrados)
    archivos = []
    for nombre in ARCHIVOS_JOURNALEADOS:
        original = _leer_original(exe_dir / nombre)
        huella_original = AUSENTE if original is None else _sha256(original)
        sembrado = nombre == ARCHIVO_SEMBRADO and huella_sembrada != huella_original
        archivos.append(
            ArchivoDelJournal(
                nombre=nombre,
                original=original,
                original_sha256=None if original is None else huella_original,
                esperados_sha256=(huella_sembrada,) if sembrado else (),
            )
        )
    propietario = duenio if duenio is not None else IdentidadDeProceso.actual()
    documento = DocumentoDeJournal(
        run_id=uuid.uuid4().hex,
        generacion=uuid.uuid4().hex,
        estado=_E.PREPARADO,
        maquina=identidad.maquina,
        instancia=identidad.instancia,
        directorio=absoluto,
        directorio_volumen=volumen,
        directorio_indice=indice,
        duenio_pid=propietario.pid,
        duenio_creado=propietario.creado,
        arranque_del_sistema=float(psutil.boot_time()),
        archivos=tuple(archivos),
    )
    almacen.asegurar_directorio()
    ruta = almacen.ruta_de(identidad.instancia)
    try:
        _escribir_documento(ruta, documento, exclusivo=True)
    except FileExistsError as exc:
        raise JournalOcupadoError(f"ya hay un journal pendiente para esta instancia: '{ruta}'") from exc
    _verificar_durable(almacen, ruta, documento)
    return TransaccionDeConfig(
        almacen=almacen, ruta=ruta, documento=documento, exe_dir=exe_dir, bytes_sembrados=bytes_sembrados
    )


# ---------------------------------------------------------------------------
# Cuarentena persistente del recurso (PR 2c-1, C3: "responder no es soltar")
# ---------------------------------------------------------------------------


def poner_en_cuarentena(almacen: AlmacenDeJournal, *, run_id: str, motivo: str) -> None:
    """Escribe el marcador de cuarentena, durable, ANTES de responder al caller.

    Mientras exista, ninguna corrida nueva arranca y el reconciliador no restaura: un
    hilo de la corrida puede seguir escribiendo. Lo retira el propio hilo al terminar
    (con el resultado verificado contra el journal) o el operador con evidencia.
    """
    almacen.asegurar_directorio()
    contenido = {"recurso": "bodyslide-meshes", "run_id": run_id, "motivo": motivo, "creado": time.time()}
    destino = almacen.marcador_de_cuarentena
    temporal = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.tmp")
    _escribir_y_sincronizar(temporal, _serializar(contenido))
    _publicar_temporal(temporal, destino)
    logger.error("BodySlide: recurso bodyslide-meshes en cuarentena (corrida %s): %s", run_id, motivo)


def cuarentena_activa(almacen: AlmacenDeJournal) -> bool:
    return path_present(almacen.marcador_de_cuarentena)


def _retirar_cuarentena(almacen: AlmacenDeJournal) -> bool:
    try:
        _retirar(almacen.marcador_de_cuarentena)
    except FileNotFoundError:
        return False
    return True


class SiembraColgadaError(JournalError):
    """El plazo de la siembra venció con el hilo vivo: hay cuarentena y no se lanza nada."""


class SiembraConPlazo:
    """Corre ``tx.sembrar`` en un hilo con plazo de RESPUESTA, sin soltar la mutación (C3).

    Un hilo de Python no se puede matar. Si el plazo vence (o el caller se cancela) con
    la siembra en vuelo, la siembra queda **abandonada**: se escribe la cuarentena
    ANTES de devolver el control y el propio hilo, al terminar, restaura con la
    transacción y retira la cuarentena sólo si la restauración quedó verificada. El
    candado garantiza el orden: o el hilo terminó antes de abandonar (y el caller sigue
    normalmente), o la cuarentena ya existe cuando el hilo mira si fue abandonado.
    """

    def __init__(self, tx: TransaccionDeConfig, *, plazo: float) -> None:
        self._tx = tx
        self._plazo = plazo
        self._candado = threading.Lock()
        self._terminado = threading.Event()
        self._abandonada = False
        self._error: BaseException | None = None
        self._aviso: Callable[[], None] | None = None

    @property
    def abandonada(self) -> bool:
        return self._abandonada

    def _hilo(self) -> None:
        try:
            self._tx.sembrar()
        except BaseException as exc:  # noqa: BLE001 — se re-eleva en el caller si no fue abandonada
            self._error = exc
        with self._candado:
            abandonada = self._abandonada
            self._terminado.set()
        aviso = self._aviso
        if aviso is not None:
            aviso()
        if abandonada:
            self._cerrar_abandonada()

    def _cerrar_abandonada(self) -> None:
        """Último escritor de la corrida abandonada: restaura y, si queda probado, levanta la cuarentena."""
        try:
            informe = self._tx.restaurar()
        except (JournalError, OSError):
            logger.exception("BodySlide: la siembra abandonada terminó pero no se pudo restaurar; sigue en cuarentena")
            return
        if informe.veredicto in {Veredicto.RESTAURADO, Veredicto.SIN_CAMBIOS}:
            _retirar_cuarentena(self._tx.almacen)
            logger.warning("BodySlide: la siembra abandonada terminó y se restauró; cuarentena retirada.")
        else:
            logger.error("BodySlide: la siembra abandonada terminó en %s; sigue en cuarentena", informe.veredicto.value)

    def _abandonar_si_sigue_viva(self, motivo: str) -> bool:
        with self._candado:
            if self._terminado.is_set():
                return False
            self._abandonada = True
            poner_en_cuarentena(self._tx.almacen, run_id=self._tx.run_id, motivo=motivo)
            return True

    async def ejecutar(self) -> None:
        """Vuelve con la siembra hecha (o su error); ``SiembraColgadaError`` si se abandonó por plazo.

        El plazo se mide en el event loop (review Codex #1021): esperarlo con
        ``to_thread`` lo hacía depender de que el executor compartido tuviera un worker
        libre. El abandono corre en un hilo PROPIO y se retiene hasta terminar aun ante
        cancelaciones repetidas: el caller nunca sale con la siembra viva y
        ``abandonada`` en falso.
        """
        loop = asyncio.get_running_loop()
        lista = asyncio.Event()

        def _avisar() -> None:
            with contextlib.suppress(RuntimeError):  # loop ya cerrado: la siembra se abandonó
                loop.call_soon_threadsafe(lista.set)

        self._aviso = _avisar
        hilo = threading.Thread(target=self._hilo, name="bodyslide-siembra", daemon=True)
        hilo.start()
        try:
            await asyncio.wait_for(lista.wait(), timeout=self._plazo)
            vencido = False
        except TimeoutError:
            vencido = True
        except asyncio.CancelledError:
            await _en_hilo_propio_hasta_terminal(
                self._abandonar_si_sigue_viva, "corrida cancelada con la siembra en vuelo"
            )
            raise
        if vencido and await _en_hilo_propio_hasta_terminal(
            self._abandonar_si_sigue_viva, f"la siembra superó el plazo de {self._plazo:g} s"
        ):
            raise SiembraColgadaError(
                f"La siembra de Config.xml superó el plazo de {self._plazo:g} s: el recurso bodyslide-meshes "
                "queda en cuarentena hasta que la escritura termine y se restaure, o el operador la libere."
            )
        if self._error is not None:
            raise self._error


async def _en_hilo_propio_hasta_terminal(funcion: Callable[[str], bool], argumento: str) -> bool:
    """Corre ``funcion`` en un hilo dedicado (no en el executor) y retiene al caller hasta que termina.

    Una cancelación (aun repetida) no suelta al caller antes de tiempo: se espera el
    resultado y recién después se propaga ``CancelledError``.
    """
    loop = asyncio.get_running_loop()
    futuro: asyncio.Future[bool] = loop.create_future()

    def _resolver(ok: bool, valor: object) -> None:
        if futuro.done():
            return
        if ok:
            futuro.set_result(bool(valor))
        else:
            assert isinstance(valor, BaseException)
            futuro.set_exception(valor)

    def _correr() -> None:
        try:
            resultado: tuple[bool, object] = (True, funcion(argumento))
        except BaseException as exc:  # noqa: BLE001 — viaja al caller por el futuro
            resultado = (False, exc)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_resolver, *resultado)

    threading.Thread(target=_correr, name="bodyslide-abandono", daemon=True).start()
    cancelado = False
    while not futuro.done():
        try:
            await asyncio.shield(futuro)
        except asyncio.CancelledError:
            if futuro.done():
                break
            cancelado = True
    resultado = futuro.result()
    if cancelado:
        raise asyncio.CancelledError
    return resultado


def liberar_cuarentena(almacen: AlmacenDeJournal, *, evidencia: str) -> bool:
    """Liberación MANUAL del operador. Exige evidencia escrita; queda en el log.

    Devuelve ``True`` si había cuarentena y se retiró. No toca la config ni el journal:
    el journal pendiente lo resuelve después el reconciliador con sus propias pruebas.
    """
    if not evidencia.strip():
        raise ValueError("liberar la cuarentena exige evidencia del operador")
    retirada = _retirar_cuarentena(almacen)
    if retirada:
        logger.warning("BodySlide: cuarentena de bodyslide-meshes liberada por el operador: %s", evidencia.strip())
    return retirada


# ---------------------------------------------------------------------------
# Recuperación (consumidor: ``rollback_reconciler.reconcile_bodyslide_config_journals``)
# ---------------------------------------------------------------------------


def _ruta_confiable(documento: DocumentoDeJournal, exe_dir: pathlib.Path) -> str | None:
    """``None`` si la carpeta configurada es FÍSICAMENTE la del journal; si no, el motivo."""
    try:
        absoluto, volumen, indice = _identidad_fisica(exe_dir)
    except (OSError, JournalError) as exc:
        return f"la carpeta configurada no es verificable ({exc})"
    if absoluto != documento.directorio:
        return "el journal apunta a otra carpeta que la configurada"
    if (volumen, indice) != (documento.directorio_volumen, documento.directorio_indice):
        return "la carpeta configurada no es la misma carpeta física que registró el journal"
    return None


def _hold(veredicto: Veredicto, ruta: pathlib.Path, detalle: str) -> InformeDeRecuperacion:
    logger.warning("BodySlide: journal '%s' en %s: %s", ruta.name, veredicto.value, detalle)
    return InformeDeRecuperacion(veredicto, ruta, detalle)


def recuperar_journal(
    ruta: pathlib.Path,
    *,
    almacen: AlmacenDeJournal,
    identidad_actual: IdentidadDeEjecucion | None,
    exe_dir: pathlib.Path,
    vivacidad: Callable[[int, float], OwnerLiveness] = _check_process_liveness,
) -> InformeDeRecuperacion:
    """Recupera UN journal. Debe correr bajo el lock ``bodyslide-meshes`` (lo garantiza el consumidor)."""
    try:
        documento = almacen.leer(ruta)
    except FileNotFoundError:
        return InformeDeRecuperacion(Veredicto.SIN_JOURNAL, ruta)
    except JournalCorruptoError as exc:
        return _hold(Veredicto.HOLD_JOURNAL_CORRUPTO, ruta, str(exc))

    if (
        identidad_actual is None
        or not identidad_actual.maquina
        or (documento.maquina, documento.instancia) != (identidad_actual.maquina, identidad_actual.instancia)
    ):
        return _hold(Veredicto.HOLD_IDENTIDAD, ruta, "máquina o instancia distinta o desconocida")
    if cuarentena_activa(almacen):
        return _hold(Veredicto.HOLD_CUARENTENA, ruta, "el recurso bodyslide-meshes está en cuarentena")
    vida = vivacidad(documento.duenio_pid, documento.duenio_creado)
    if vida is OwnerLiveness.ALIVE:
        return _hold(Veredicto.HOLD_MUTACION_EN_VUELO, ruta, f"el dueño (pid {documento.duenio_pid}) sigue vivo")
    if vida is not OwnerLiveness.DEAD:
        return _hold(Veredicto.HOLD_TERMINALITY_UNKNOWN, ruta, "no se pudo determinar si el dueño terminó")
    motivo = _ruta_confiable(documento, exe_dir)
    if motivo is not None:
        return _hold(Veredicto.HOLD_RUTA_NO_CONFIABLE, ruta, motivo)
    if documento.estado is _E.LANZADO:
        return _hold(
            Veredicto.HOLD_TERMINALITY_UNKNOWN,
            ruta,
            "la corrida pidió el spawn y no hay terminalidad registrada (el reinicio no alcanza)",
        )

    try:
        if documento.estado is _E.RESTAURADO:
            _cerrar(ruta, exe_dir)
            return InformeDeRecuperacion(Veredicto.SIN_CAMBIOS, ruta, "ya estaba restaurado")
        informe, _ = _restaurar(almacen, ruta, documento, exe_dir)
    except JournalError as exc:
        return _hold(Veredicto.HOLD_JOURNAL_CORRUPTO, ruta, str(exc))
    except OSError as exc:
        return _hold(Veredicto.RESTORE_FAILED, ruta, f"no se pudo actualizar el journal ({exc})")
    if informe.veredicto in {Veredicto.RESTAURADO, Veredicto.SIN_CAMBIOS}:
        logger.info("BodySlide: journal '%s' recuperado (%s).", ruta.name, informe.veredicto.value)
    return informe


def recuperar_todos(
    *,
    almacen: AlmacenDeJournal,
    identidad_actual: IdentidadDeEjecucion | None,
    exe_dir: pathlib.Path,
    vivacidad: Callable[[int, float], OwnerLiveness] = _check_process_liveness,
) -> tuple[InformeDeRecuperacion, ...]:
    """Recupera cada journal del almacén; los de otra instancia o máquina quedan en HOLD y se reportan."""
    return tuple(
        recuperar_journal(
            ruta, almacen=almacen, identidad_actual=identidad_actual, exe_dir=exe_dir, vivacidad=vivacidad
        )
        for ruta in almacen.listar()
    )
