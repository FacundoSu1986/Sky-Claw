"""Superficie de operador de la cuarentena del broker VFS (H-4 de la auditoría de la etapa 9).

Cuando el broker pierde la certeza de que un job terminó (``terminalidad desconocida``), pone la instancia en
cuarentena y lo PERSISTE: un marcador ``.<instancia>.quarantine`` y, si no pudo escribirlo, un lock «envenenado»
(``quarantined: true``) que retiene al cerrar. Sobrevive a los reinicios a propósito. Pero ``release_quarantine`` del
broker solo sirve con un broker vivo que ya es dueño del lock, y con el lock envenenado ni siquiera se puede arrancar
uno: el operador salía borrando dotfiles a mano.

Este módulo es esa salida, sin broker vivo:

* :func:`leer_estado_de_cuarentena` — solo lectura; nunca escribe.
* :func:`liberar_cuarentena` — con evidencia explícita y solo si NO hay un dueño vivo del lock.

Cubre las TRES formas persistidas (marcador, lock envenenado y lock huérfano) porque borrar solo el marcador no
alcanza: si queda el lock de un broker que murió, el próximo arranque lo «reclama» y vuelve a poner la instancia en
cuarentena. ``tests/test_vfs_quarantine.py`` fabrica cada estado con el broker REAL y exige que lo que dice este
módulo coincida con lo que el broker hace al arrancar.
"""

from __future__ import annotations

import enum
import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any

from sky_claw.local.mo2.vfs_broker import (
    VfsBrokerError,
    lock_de_instancia_tiene_dueno_vivo,
    proceso_dueno_sigue_vivo,
)

logger = logging.getLogger(__name__)

_PASOS_PREVIOS_A_LIBERAR = (
    "Cerrá Sky-Claw, confirmá en el Administrador de tareas que no queda vivo ningún proceso del job (el worker "
    "SkyClawApp.exe --vfs-worker ni la herramienta que lanzó: TexGen, DynDOLOD, LODGen…) y corré "
)


def como_liberar(mo2_root: pathlib.Path | None = None) -> str:
    """El procedimiento para el operador, en UN solo lugar: lo muestran ``--mode vfs-quarantine`` y ``lods_status``.

    La instancia sale de ``--mo2-root``: con la raíz de la sesión que quedó en cuarentena el comando apunta a ESA
    instancia, y no a la que el modo elegiría por defecto (otra raíz → otro directorio de estado → «nada que liberar»
    mientras la instancia real sigue aislada). ``None`` solo cuando quien muestra el texto no conoce la raíz.
    """
    raiz = "" if mo2_root is None else f' --mo2-root "{mo2_root}"'
    return f'{_PASOS_PREVIOS_A_LIBERAR}--mode vfs-quarantine{raiz} --vfs-quarantine-action release --vfs-evidence "<qué verificaste>"'


#: El mismo procedimiento sin raíz (compatibilidad); quien conozca la raíz usa :func:`como_liberar`.
COMO_LIBERAR = como_liberar()
_RAZON_SIN_DATO = "terminalidad indeterminada previa"
_RAZON_MARCADOR_ILEGIBLE = "marcador de cuarentena presente pero ilegible (fail-closed)"
_RAZON_LOCK_ENVENENADO = "envenenamiento fail-closed"
_RAZON_LOCK_HUERFANO = (
    "lock de un broker que murió sin cerrar: el próximo arranque lo reclama y pone la instancia en cuarentena"
)


class CuarentenaNoLiberableError(VfsBrokerError):
    """No se puede liberar la cuarentena (sin evidencia, dueño vivo, o el disco falló). La instancia sigue aislada."""


class OrigenDeCuarentena(enum.StrEnum):
    """Cómo quedó persistida la cuarentena. Cada valor es una forma que el broker real reconoce al arrancar."""

    MARCADOR = "marcador"
    MARCADOR_ILEGIBLE = "marcador_ilegible"
    LOCK_ENVENENADO = "lock_envenenado"
    LOCK_HUERFANO = "lock_huerfano"


@dataclass(frozen=True, slots=True)
class RutasDeEstado:
    """Los dos archivos que persisten la cuarentena (mismos nombres que ``VfsExecutionBroker``)."""

    marcador: pathlib.Path
    lock: pathlib.Path


@dataclass(frozen=True, slots=True)
class EstadoDeCuarentena:
    instance_id: str
    state_dir: pathlib.Path
    origen: OrigenDeCuarentena | None
    razon: str | None
    job_id: str | None
    error_type: str | None
    detalle: str | None
    desde: float | None
    pid_dueno: int | None
    dueno_vivo: bool

    @property
    def en_cuarentena(self) -> bool:
        return self.origen is not None


@dataclass(frozen=True, slots=True)
class _Lock:
    contenido: bytes | None
    envenenado: bool
    pid: int | None
    dueno_vivo: bool
    crudo: dict[str, Any]


def rutas_de_estado(state_dir: pathlib.Path, instance_id: str) -> RutasDeEstado:
    base = state_dir.resolve()
    return RutasDeEstado(marcador=base / f".{instance_id}.quarantine", lock=base / f".{instance_id}.lock")


def _como_pid(valor: object) -> int | None:
    return valor if type(valor) is int and valor > 0 else None


def _como_momento(valor: object) -> float | None:
    """El ``timestamp`` del marcador como float; ``None`` si falta o no es representable (p. ej. un entero gigante)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    try:
        return float(valor)
    except OverflowError:
        return None


def _inspeccionar_lock(lock: pathlib.Path) -> _Lock | None:
    """Lee el lock sin escribir. ``None`` si no existe. Ante lo ilegible, el dueño se da por vivo (conservador)."""
    try:
        contenido = lock.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return _Lock(contenido=None, envenenado=False, pid=None, dueno_vivo=True, crudo={})
    try:
        crudo = json.loads(contenido.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _Lock(contenido=contenido, envenenado=False, pid=None, dueno_vivo=True, crudo={})
    if not isinstance(crudo, dict):
        return _Lock(contenido=contenido, envenenado=False, pid=None, dueno_vivo=True, crudo={})
    pid = _como_pid(crudo.get("pid"))
    if crudo.get("quarantined"):
        # El broker nunca reclama un lock envenenado, pero el operador sí puede liberarlo si su dueño murió. El dueño
        # se identifica por PID Y ``create_time`` (un PID reciclado por otro proceso no es el dueño); un lock
        # antiguo sin ``create_time`` queda conservador: se asume vivo.
        vivo = True if pid is None else proceso_dueno_sigue_vivo(pid, crudo.get("create_time"))
        return _Lock(contenido=contenido, envenenado=True, pid=pid, dueno_vivo=vivo, crudo=crudo)
    return _Lock(
        contenido=contenido,
        envenenado=False,
        pid=pid,
        dueno_vivo=lock_de_instancia_tiene_dueno_vivo(lock),
        crudo=crudo,
    )


def _leer(state_dir: pathlib.Path, instance_id: str) -> tuple[EstadoDeCuarentena, _Lock | None]:
    rutas = rutas_de_estado(state_dir, instance_id)
    lock = _inspeccionar_lock(rutas.lock)
    pid_dueno = None if lock is None else lock.pid
    dueno_vivo = lock is not None and lock.dueno_vivo

    def _estado(origen: OrigenDeCuarentena | None, datos: dict[str, Any], razon: str | None) -> EstadoDeCuarentena:
        return EstadoDeCuarentena(
            instance_id=instance_id,
            state_dir=state_dir,
            origen=origen,
            razon=razon,
            job_id=datos.get("job_id") if isinstance(datos.get("job_id"), str) else None,
            error_type=datos.get("error_type") if isinstance(datos.get("error_type"), str) else None,
            detalle=datos.get("detail") if isinstance(datos.get("detail"), str) else None,
            desde=_como_momento(datos.get("timestamp")),
            pid_dueno=pid_dueno,
            dueno_vivo=dueno_vivo,
        )

    # Mismo orden de precedencia que ``VfsExecutionBroker._load_quarantine``: marcador, lock envenenado, huérfano.
    if rutas.marcador.exists():
        try:
            crudo = json.loads(rutas.marcador.read_text(encoding="utf-8"))
            if not isinstance(crudo, dict):
                raise ValueError("el marcador no es un objeto JSON")
        except Exception:  # noqa: BLE001 -- igual que el broker: cualquier ilegible es cuarentena fail-closed
            return _estado(OrigenDeCuarentena.MARCADOR_ILEGIBLE, {}, _RAZON_MARCADOR_ILEGIBLE), lock
        return _estado(OrigenDeCuarentena.MARCADOR, crudo, str(crudo.get("reason", _RAZON_SIN_DATO))), lock
    if lock is not None and lock.envenenado:
        razon = str(lock.crudo.get("quarantine_reason", _RAZON_LOCK_ENVENENADO))
        return _estado(OrigenDeCuarentena.LOCK_ENVENENADO, lock.crudo, razon), lock
    if lock is not None and not lock.dueno_vivo:
        return _estado(OrigenDeCuarentena.LOCK_HUERFANO, lock.crudo, _RAZON_LOCK_HUERFANO), lock
    return _estado(None, {}, None), lock


def leer_estado_de_cuarentena(state_dir: pathlib.Path, instance_id: str) -> EstadoDeCuarentena:
    """Estado persistido de la cuarentena de una instancia. Solo lectura: nunca escribe ni borra nada."""
    return _leer(state_dir, instance_id)[0]


def listar_instancias_con_cuarentena(base_dir: pathlib.Path) -> list[EstadoDeCuarentena]:
    """Las instancias bajo ``base_dir`` (``<config>/vfs_bridge``) que están en cuarentena, por nombre."""
    if not base_dir.is_dir():
        return []
    encontradas = []
    for hijo in sorted(base_dir.iterdir()):
        if not hijo.is_dir():
            continue
        estado = leer_estado_de_cuarentena(hijo, hijo.name)
        if estado.en_cuarentena:
            encontradas.append(estado)
    return encontradas


def _borrar_si_no_cambio(path: pathlib.Path, *, visto: bytes) -> bool:
    """Borra ``path`` solo si sigue teniendo el contenido que se juzgó huérfano.

    Cierra la carrera con un arranque concurrente: si entre la lectura y el borrado otro broker tomó el lock, ya no
    es el que se miró y NO se toca. Queda una ventana mínima entre esta relectura y el ``unlink``; por eso la
    liberación pide cerrar Sky-Claw antes (es una acción manual del operador, no un camino automático).
    """
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        return True
    if actual != visto:
        return False
    path.unlink(missing_ok=True)
    return True


def liberar_cuarentena(state_dir: pathlib.Path, instance_id: str, *, evidencia: str) -> EstadoDeCuarentena:
    """Libera la cuarentena persistida de ``instance_id``, con evidencia explícita y SIN broker vivo.

    Devuelve el estado que había antes de liberar. Sin cuarentena es un no-op. Fail-closed: si algo falla, lanza
    :class:`CuarentenaNoLiberableError` y la instancia sigue aislada (el lock se resuelve ANTES que el marcador, así un
    fallo a mitad deja siempre el marcador, o sea cuarentena entera).
    """
    if not isinstance(evidencia, str) or not evidencia.strip():
        raise CuarentenaNoLiberableError(
            "se requiere evidencia explícita (qué verificaste y cómo) para levantar la cuarentena"
        )
    previo, lock = _leer(state_dir, instance_id)
    if not previo.en_cuarentena:
        logger.info("La instancia %s no está en cuarentena: no hay nada que liberar", instance_id)
        return previo
    if previo.dueno_vivo:
        pid = "desconocido" if previo.pid_dueno is None else str(previo.pid_dueno)
        raise CuarentenaNoLiberableError(
            f"la instancia {instance_id} tiene un dueño vivo del lock (PID {pid}): cerrá Sky-Claw y todo broker que "
            "use esta instancia antes de liberar la cuarentena. Con un broker vivo la liberación no tendría efecto: "
            "su estado en memoria la volvería a persistir"
        )
    rutas = rutas_de_estado(state_dir, instance_id)
    try:
        if (
            lock is not None
            and lock.contenido is not None
            and not _borrar_si_no_cambio(rutas.lock, visto=lock.contenido)
        ):
            raise CuarentenaNoLiberableError(
                f"el lock de la instancia {instance_id} cambió mientras se liberaba: otro proceso lo tomó; "
                "no se borró nada, reintentá"
            )
        rutas.marcador.unlink(missing_ok=True)
    except OSError as exc:
        raise CuarentenaNoLiberableError(
            f"no se pudo borrar el estado de cuarentena de la instancia {instance_id}: {exc}. "
            "La instancia sigue en cuarentena"
        ) from exc
    logger.critical(
        "Cuarentena de la instancia %s liberada por el operador (origen=%s, job=%s, razón previa=%r). Evidencia: %s",
        instance_id,
        previo.origen.value if previo.origen is not None else None,
        previo.job_id,
        previo.razon,
        evidencia.strip(),
        extra={"instance_id": instance_id, "evidence": evidencia.strip(), "job_id": previo.job_id},
    )
    return previo
