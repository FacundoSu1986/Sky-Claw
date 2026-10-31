"""Registro DURABLE de recuperación del binder transaccional (P5-C1, FIX 3).

Tracker: #672
ADR 0012 §34.4, §35.6, §37.5
ADR 0015 §12, §13

El defecto que este módulo cierra
---------------------------------
El binder capturaba las superficies en un ``BinderSnapshot`` que vivía **sólo en
RAM**. Un crash del proceso entre ``PERSIST PENDING`` y ``FINALIZE`` dejaba el
journal en ``PENDING_*`` y las superficies mutadas (INI, config, active.json,
entorno) sin ninguna evidencia de cuál era su estado anterior: la información
para volver atrás moría con el proceso. Eso hace que la transición no sea
recuperable, sólo abortable a mano por el dueño.

Qué se persiste y por qué alcanza
---------------------------------
Un archivo por transición en ``state/recovery/<transition_id>.json`` con:

- ``transition_id`` y ``journal_revision``: el binding al journal. Un registro
  que no liga la revisión de la transición que dice recuperar no se usa.
- Los **bytes completos** de cada superficie persistida mutada, más su
  ``sha256``. Guardar sólo el valor parseado (lo que hacía el snapshot en RAM
  con ``skyrim_path``) no permite restaurar: ``persistir_campo`` reescribe el
  TOML entero, así que el valor previo NO reconstruye el archivo previo.
- El valor previo de la superficie derivada (``SKYRIM_PATH``) o su ausencia.
- La ``fase`` alcanzada. No se usa para decidir *si* restaurar —se restauran
  todas las superficies capturadas, la restauración es idempotente— sino para
  poder DECLARAR en qué frontera se interrumpió.

La FSM del journal NO se reemplaza
----------------------------------
Este registro es un **subordinado** del journal, no un segundo FSM:

- El journal decide qué transición existe y en qué estado está.
- El registro sólo dice cómo volver las superficies físicas a su estado previo.
- ``recuperar_transicion_interrumpida`` NUNCA escribe el journal. Si la
  transición ya está FINALIZED en el historial, el registro es obsoleto y se
  retira sin restaurar nada (restaurar una transición completada sería destruir
  el resultado de una operación exitosa).
- Si el journal está ``PENDING_*``, la restauración devuelve las superficies a
  su lugar y la clasificación del journal queda para ``journal.reconciliar_journal``,
  que es su dueño. El resultado declara ``requiere_dueno=True``.

Alcance de la garantía (declarado, no insinuado)
------------------------------------------------
``PROCESS_CRASH_RECOVERY = DEMONSTRATED`` (tests de reinicio con contexto nuevo).
``POWER_LOSS_GUARANTEE = NOT_CLAIMED``: se usa el mismo ``os.replace`` atómico
del resto del paquete, sin WAL ni ``fsync`` de directorio, igual que
``state.write_json_atomic``.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import tempfile
import time
from collections.abc import Mapping, MutableMapping
from enum import StrEnum

from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
from sky_claw.local.frozen_runtime.journal import (
    JournalDisposition,
    JournalState,
    cargar_journal_transicion,
    derivar_historial_finalizados,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import active_state_path, recovery_state_dir

__all__ = [
    "EstadoRecuperacion",
    "FaseRecuperacion",
    "RegistroCorruptoError",
    "RegistroRecuperacion",
    "ResultadoRecuperacion",
    "SuperficieCapturada",
    "capturar_registro",
    "cargar_registro",
    "eliminar_registro_locked",
    "escribir_registro_locked",
    "listar_registros",
    "marcar_fase_locked",
    "recuperar_transicion_interrumpida",
    "restaurar_registro_locked",
]

SCHEMA_VERSION = 1
#: Tope de bytes por superficie. Un INI de MO2 y un ``config.toml`` están tres
#: órdenes de magnitud abajo; un archivo mayor no es una superficie que este
#: binder deba estar reescribiendo, y copiarlo entero al registro sería inflar
#: el estado durable con algo que no se entiende.
MAX_BYTES_SUPERFICIE = 4 * 1024 * 1024

_KIND_MO2_INI = "mo2_ini"
_KIND_CONFIG = "config"
_KIND_ACTIVE = "active_json"
_KIND_ENVIRON = "environ"
_KINDS_PERSISTIDAS = (_KIND_MO2_INI, _KIND_CONFIG, _KIND_ACTIVE)

#: Nombres públicos de las superficies, para que el binder no repita literales.
SUPERFICIE_MO2_INI = _KIND_MO2_INI
SUPERFICIE_CONFIG = _KIND_CONFIG
SUPERFICIE_ACTIVE = _KIND_ACTIVE
SUPERFICIE_ENVIRON = _KIND_ENVIRON


class RecuperacionError(FrozenRuntimeError):
    """Error base del registro de recuperación."""


class RegistroCorruptoError(RecuperacionError):
    """El registro existe pero no se puede interpretar: no se usa (fail-closed)."""


class FaseRecuperacion(StrEnum):
    """Frontera mutante alcanzada, en el orden causal de ADR 0012.

    El orden es el del ``ORDEN_CAUSAL`` y se verifica al avanzar: una fase sólo
    puede avanzar a la siguiente, nunca retroceder. Un registro que declare un
    retroceso es evidencia de corrupción, no de un reintento.
    """

    PREPARADO = "PREPARADO"
    INI_ESCRITO = "INI_ESCRITO"
    CONFIG_ESCRITA = "CONFIG_ESCRITA"
    ENTORNO_ACTUALIZADO = "ENTORNO_ACTUALIZADO"
    DESEADO_ACTUALIZADO = "DESEADO_ACTUALIZADO"
    FINALIZADO = "FINALIZADO"


ORDEN_CAUSAL: tuple[FaseRecuperacion, ...] = (
    FaseRecuperacion.PREPARADO,
    FaseRecuperacion.INI_ESCRITO,
    FaseRecuperacion.CONFIG_ESCRITA,
    FaseRecuperacion.ENTORNO_ACTUALIZADO,
    FaseRecuperacion.DESEADO_ACTUALIZADO,
    FaseRecuperacion.FINALIZADO,
)


class EstadoRecuperacion(StrEnum):
    """Qué hizo (o qué corresponde hacer con) la recuperación."""

    #: Las superficies se restauraron y se verificaron.
    RESTAURADO = "RESTAURADO"
    #: No había registro: nada que recuperar.
    SIN_REGISTRO = "SIN_REGISTRO"
    #: La transición ya está FINALIZED: el registro era obsoleto y se retiró.
    TRANSICION_YA_FINALIZADA = "TRANSICION_YA_FINALIZADA"
    #: El registro existe pero no liga el journal: no se toca nada.
    REGISTRO_NO_LIGADO = "REGISTRO_NO_LIGADO"
    #: Se restauró, pero el journal sigue PENDING: decide el dueño.
    RESTAURADO_REQUIERE_DUENO = "RESTAURADO_REQUIERE_DUENO"


@dataclasses.dataclass(frozen=True, slots=True)
class SuperficieCapturada:
    """Estado previo de UNA superficie, con lo necesario para restaurarla.

    ``contenido_b64`` es ``None`` cuando la superficie no existía (``existed``
    en ``False``): "no existía" y "existía vacía" son estados distintos y
    restaurar el segundo donde había el primero dejaría un archivo de 0 bytes
    que el lector clasifica como corrupción.
    """

    kind: str
    path: str | None
    existed: bool
    contenido_b64: str | None
    sha256: str | None

    def bytes_previos(self) -> bytes | None:
        if self.contenido_b64 is None:
            return None
        return base64.b64decode(self.contenido_b64.encode("ascii"))


@dataclasses.dataclass(frozen=True, slots=True)
class RegistroRecuperacion:
    """Registro durable de una transición, con las superficies previas."""

    schema_version: int
    transition_id: str
    journal_revision: int
    operation: str
    target_clone_id: str
    target_generation_id: str
    expected_effective_path: str
    superficies: tuple[SuperficieCapturada, ...]
    fase: FaseRecuperacion
    created_at_ns: int
    updated_at_ns: int
    #: Identidad del Desired que la transición pretendía escribir. Permite
    #: distinguir, tras un reinicio, «active.json lo escribimos nosotros» de
    #: «lo escribió un tercero que ganó la carrera del CAS»: restaurar en el
    #: segundo caso pisaría una escritura ajena.
    desired_previsto_generation: str | None = None
    desired_previsto_clone: str | None = None

    def superficie(self, kind: str) -> SuperficieCapturada | None:
        return next((s for s in self.superficies if s.kind == kind), None)


@dataclasses.dataclass(frozen=True, slots=True)
class VerificacionSuperficie:
    """Resultado por superficie. ``escritura_ajena`` no es un fallo: es una decisión."""

    kind: str
    ok: bool
    detail: str
    escritura_ajena: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class ResultadoRecuperacion:
    """Resultado de una recuperación, con la verificación por superficie."""

    estado: EstadoRecuperacion
    transition_id: str | None
    fase: FaseRecuperacion | None
    verificaciones: tuple[VerificacionSuperficie, ...]
    journal_state: JournalState | None
    requiere_dueno: bool
    motivo: str

    @property
    def superficies_verificadas(self) -> tuple[str, ...]:
        return tuple(v.kind for v in self.verificaciones if v.ok)


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------


def _sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _capturar_bytes(kind: str, path: pathlib.Path | None) -> SuperficieCapturada:
    if path is None:
        return SuperficieCapturada(kind=kind, path=None, existed=False, contenido_b64=None, sha256=None)
    ruta = pathlib.Path(os.path.abspath(os.fspath(path)))
    try:
        datos = ruta.read_bytes()
    except FileNotFoundError:
        return SuperficieCapturada(kind=kind, path=str(ruta), existed=False, contenido_b64=None, sha256=None)
    except OSError as exc:
        raise RecuperacionError(f"no se pudo capturar {kind} ({ruta}) para el registro durable: {exc}") from exc
    if len(datos) > MAX_BYTES_SUPERFICIE:
        raise RecuperacionError(
            f"la superficie {kind} ({ruta}) tiene {len(datos)} bytes y el tope es {MAX_BYTES_SUPERFICIE}: "
            "no se captura un archivo que el binder no debería reescribir"
        )
    return SuperficieCapturada(
        kind=kind,
        path=str(ruta),
        existed=True,
        contenido_b64=base64.b64encode(datos).decode("ascii"),
        sha256=_sha256(datos),
    )


def _capturar_environ(valor_previo: str | None) -> SuperficieCapturada:
    """La superficie derivada no es un archivo: se guarda su valor o su ausencia."""
    if valor_previo is None:
        return SuperficieCapturada(kind=_KIND_ENVIRON, path=None, existed=False, contenido_b64=None, sha256=None)
    datos = valor_previo.encode("utf-8", "surrogatepass")
    return SuperficieCapturada(
        kind=_KIND_ENVIRON,
        path=None,
        existed=True,
        contenido_b64=base64.b64encode(datos).decode("ascii"),
        sha256=_sha256(datos),
    )


def capturar_registro(
    *,
    storage_root: pathlib.Path,
    transition_id: str,
    journal_revision: int,
    operation: str,
    target_clone_id: str,
    target_generation_id: str,
    expected_effective_path: str,
    mo2_ini_path: pathlib.Path,
    config_path: pathlib.Path | None,
    environ_skyrim_path: str | None,
    clock_ns: object = time.time_ns,
) -> RegistroRecuperacion:
    """Construye el registro con los bytes REALES de cada superficie.

    Se lee del disco, no de objetos en memoria: el registro tiene que describir
    lo que hay, no lo que el llamador cree que hay.
    """
    sello = clock_ns() if callable(clock_ns) else time.time_ns()
    superficies = (
        _capturar_bytes(_KIND_MO2_INI, mo2_ini_path),
        _capturar_bytes(_KIND_CONFIG, config_path),
        _capturar_bytes(_KIND_ACTIVE, active_state_path(storage_root)),
        _capturar_environ(environ_skyrim_path),
    )
    return RegistroRecuperacion(
        schema_version=SCHEMA_VERSION,
        transition_id=transition_id,
        journal_revision=journal_revision,
        operation=operation,
        target_clone_id=target_clone_id,
        target_generation_id=target_generation_id,
        expected_effective_path=expected_effective_path,
        superficies=superficies,
        fase=FaseRecuperacion.PREPARADO,
        created_at_ns=sello,
        updated_at_ns=sello,
        desired_previsto_generation=target_generation_id,
        desired_previsto_clone=target_clone_id,
    )


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


def _serializar(registro: RegistroRecuperacion) -> dict[str, object]:
    return {
        "schema_version": registro.schema_version,
        "transition_id": registro.transition_id,
        "journal_revision": registro.journal_revision,
        "operation": registro.operation,
        "target_clone_id": registro.target_clone_id,
        "target_generation_id": registro.target_generation_id,
        "expected_effective_path": registro.expected_effective_path,
        "fase": registro.fase.value,
        "created_at_ns": registro.created_at_ns,
        "updated_at_ns": registro.updated_at_ns,
        "desired_previsto_generation": registro.desired_previsto_generation,
        "desired_previsto_clone": registro.desired_previsto_clone,
        "superficies": [
            {
                "kind": s.kind,
                "path": s.path,
                "existed": s.existed,
                "contenido_b64": s.contenido_b64,
                "sha256": s.sha256,
            }
            for s in registro.superficies
        ],
    }


def _parsear(payload: object, *, ruta: pathlib.Path) -> RegistroRecuperacion:
    if not isinstance(payload, dict):
        raise RegistroCorruptoError(f"{ruta}: el registro debe ser un objeto JSON")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RegistroCorruptoError(f"{ruta}: schema_version desconocido {payload.get('schema_version')!r}")
    try:
        fase = FaseRecuperacion(payload["fase"])
    except (KeyError, ValueError) as exc:
        raise RegistroCorruptoError(f"{ruta}: fase inválida: {exc}") from exc
    crudas = payload.get("superficies")
    if not isinstance(crudas, list) or not crudas:
        raise RegistroCorruptoError(f"{ruta}: 'superficies' debe ser una lista no vacía")
    superficies: list[SuperficieCapturada] = []
    for cruda in crudas:
        if not isinstance(cruda, dict):
            raise RegistroCorruptoError(f"{ruta}: superficie malformada")
        kind = cruda.get("kind")
        existed = cruda.get("existed")
        if not isinstance(kind, str) or not isinstance(existed, bool):
            raise RegistroCorruptoError(f"{ruta}: superficie sin 'kind'/'existed' válidos")
        contenido = cruda.get("contenido_b64")
        sha = cruda.get("sha256")
        if existed:
            if not isinstance(contenido, str) or not isinstance(sha, str):
                raise RegistroCorruptoError(f"{ruta}: la superficie {kind} declara existir y no trae contenido/digest")
            try:
                datos = base64.b64decode(contenido.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise RegistroCorruptoError(f"{ruta}: base64 inválido en {kind}: {exc}") from exc
            if _sha256(datos) != sha:
                # El digest se RECALCULA: un registro cuyo contenido no liga su
                # propio digest restauraría bytes que nadie verificó.
                raise RegistroCorruptoError(f"{ruta}: el digest de {kind} no liga su contenido")
        else:
            contenido, sha = None, None
        superficies.append(
            SuperficieCapturada(
                kind=kind,
                path=cruda.get("path") if isinstance(cruda.get("path"), str) else None,
                existed=existed,
                contenido_b64=contenido,
                sha256=sha,
            )
        )
    for kind in _KINDS_PERSISTIDAS:
        if not any(s.kind == kind for s in superficies):
            raise RegistroCorruptoError(f"{ruta}: falta la superficie persistida '{kind}'")
    if not any(s.kind == _KIND_ENVIRON for s in superficies):
        raise RegistroCorruptoError(f"{ruta}: falta la superficie derivada '{_KIND_ENVIRON}'")
    revision = payload.get("journal_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise RegistroCorruptoError(f"{ruta}: journal_revision debe ser un entero >= 0")
    for campo in ("transition_id", "operation", "target_clone_id", "target_generation_id", "expected_effective_path"):
        if not isinstance(payload.get(campo), str):
            raise RegistroCorruptoError(f"{ruta}: falta '{campo}'")
    return RegistroRecuperacion(
        schema_version=SCHEMA_VERSION,
        transition_id=str(payload["transition_id"]),
        journal_revision=revision,
        operation=str(payload["operation"]),
        target_clone_id=str(payload["target_clone_id"]),
        target_generation_id=str(payload["target_generation_id"]),
        expected_effective_path=str(payload["expected_effective_path"]),
        superficies=tuple(superficies),
        fase=fase,
        created_at_ns=int(payload.get("created_at_ns", 0)),
        updated_at_ns=int(payload.get("updated_at_ns", 0)),
        desired_previsto_generation=(
            payload["desired_previsto_generation"]
            if isinstance(payload.get("desired_previsto_generation"), str)
            else None
        ),
        desired_previsto_clone=(
            payload["desired_previsto_clone"] if isinstance(payload.get("desired_previsto_clone"), str) else None
        ),
    )


def _ruta_registro(root: pathlib.Path, transition_id: str) -> pathlib.Path:
    if not transition_id or any(c in transition_id for c in ("/", "\\", ":", "..")):
        raise RecuperacionError(f"transition_id inválido para el registro durable: {transition_id!r}")
    return recovery_state_dir(root) / f"{transition_id}.json"


# ---------------------------------------------------------------------------
# Escritura (bajo lock)
# ---------------------------------------------------------------------------


def _exigir_store(root: pathlib.Path) -> pathlib.Path:
    directorio = recovery_state_dir(root)
    if not directorio.exists():
        raise RecuperacionError(
            f"{directorio}: el store de recuperación no está inicializado. "
            "Falla cerrado: el binder no crea stores en su camino de escritura"
        )
    return directorio


def escribir_registro_locked(handle: object, root: pathlib.Path, registro: RegistroRecuperacion) -> None:
    """Persiste el registro. NO clobber: si ya existe, es un error de uso.

    El no-clobber es lo que impide que un segundo intento con el mismo
    ``transition_id`` pise las superficies previas de la primera transición con
    las del estado ya mutado — que es exactamente cómo un rollback se vuelve
    irreversible.
    """
    _exigir_store(root)
    ruta = _ruta_registro(root, registro.transition_id)
    if ruta.exists():
        raise RecuperacionError(
            f"{ruta}: ya existe un registro de recuperación para {registro.transition_id}. "
            "Un segundo registro del mismo transition_id pisaría el snapshot previo (fail-closed)"
        )
    state_module.write_json_atomic(ruta, _serializar(registro))


def marcar_fase_locked(
    handle: object,
    root: pathlib.Path,
    *,
    transition_id: str,
    fase: FaseRecuperacion,
) -> RegistroRecuperacion:
    """Avanza la fase del registro, con CAS sobre la revisión y el orden causal.

    ``journal_revision`` no cambia durante la transición (el PENDING se escribe
    una sola vez), así que el CAS se hace sobre la fase: sólo se avanza desde la
    fase inmediatamente anterior. Un registro que "retrocede" se rechaza.
    """
    actual = cargar_registro(root, transition_id)
    if actual is None:
        raise RecuperacionError(f"no hay registro de recuperación para {transition_id}")
    indice_actual = ORDEN_CAUSAL.index(actual.fase)
    indice_nuevo = ORDEN_CAUSAL.index(fase)
    if indice_nuevo <= indice_actual:
        raise RecuperacionError(
            f"fase {fase.value} no AVANZA desde {actual.fase.value} en el orden causal "
            f"({[f.value for f in ORDEN_CAUSAL]}): el registro no retrocede ni se re-marca"
        )
    actualizado = dataclasses.replace(actual, fase=fase, updated_at_ns=time.time_ns())
    state_module.write_json_atomic(_ruta_registro(root, transition_id), _serializar(actualizado))
    return actualizado


def eliminar_registro_locked(handle: object, root: pathlib.Path, transition_id: str) -> bool:
    """Retira el registro. Sólo después de validar que la transición terminó."""
    ruta = _ruta_registro(root, transition_id)
    if not ruta.exists():
        return False
    ruta.unlink()
    return True


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def cargar_registro(root: pathlib.Path, transition_id: str) -> RegistroRecuperacion | None:
    ruta = _ruta_registro(root, transition_id)
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistroCorruptoError(f"{ruta}: no se pudo leer el registro: {exc}") from exc
    try:
        payload = json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise RegistroCorruptoError(f"{ruta}: JSON malformado: {exc}") from exc
    registro = _parsear(payload, ruta=ruta)
    if registro.transition_id != transition_id:
        raise RegistroCorruptoError(
            f"{ruta}: el registro declara {registro.transition_id} y vive en el archivo de {transition_id}"
        )
    return registro


def listar_registros(root: pathlib.Path) -> tuple[RegistroRecuperacion, ...]:
    directorio = recovery_state_dir(root)
    if not directorio.exists():
        return ()
    registros: list[RegistroRecuperacion] = []
    for ruta in sorted(directorio.glob("*.json")):
        registro = cargar_registro(root, ruta.stem)
        if registro is not None:
            registros.append(registro)
    return tuple(registros)


# ---------------------------------------------------------------------------
# Restauración
# ---------------------------------------------------------------------------


def _escribir_bytes_atomico(ruta: pathlib.Path, datos: bytes) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.rollback.", suffix=".tmp")
    except OSError as exc:
        raise RecuperacionError(f"no se pudo crear el temporal para restaurar {ruta}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(datos)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)
    except BaseException:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def huella(datos: bytes) -> str:
    """sha256 hex de un contenido: el token con el que se compara una superficie."""
    return _sha256(datos)


def _identidad_activa(superficie: SuperficieCapturada) -> tuple[str | None, str | None]:
    """(generation, clone) que declara un ``active.json``, sin lanzar."""
    if not superficie.existed:
        return (None, None)
    crudo = superficie.bytes_previos()
    if crudo is None:
        return (None, None)
    try:
        payload = json.loads(crudo.decode("utf-8", "surrogateescape"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (None, None)
    if not isinstance(payload, dict):
        return (None, None)
    generacion = payload.get("desired_generation_id", payload.get("desired_active_generation"))
    clon = payload.get("desired_clone_id")
    return (generacion if isinstance(generacion, str) else None, clon if isinstance(clon, str) else None)


def _activa_escrita_por_tercero(registro: RegistroRecuperacion, superficie: SuperficieCapturada) -> bool:
    """¿El ``active.json`` actual lo escribió alguien que NO es esta transición?

    Tres casos posibles cuando la fase dice ``DESEADO_ACTUALIZADO``:

    - el contenido actual es el capturado → nada que restaurar (nadie escribió);
    - el contenido actual declara el Desired que ESTA transición iba a escribir
      → lo escribimos nosotros, restaurar es correcto;
    - cualquier otra cosa → ganó la carrera del CAS un tercero. Restaurar
      pisaría su escritura, que es exactamente lo que FIX 2 prohíbe.
    """
    if not superficie.existed:
        return False
    ruta = pathlib.Path(superficie.path) if superficie.path else None
    if ruta is None or not ruta.exists():
        return False
    try:
        actuales = ruta.read_bytes()
    except OSError:
        return False
    if _sha256(actuales) == superficie.sha256:
        return False
    try:
        payload = json.loads(actuales.decode("utf-8", "surrogateescape"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    generacion = payload.get("desired_generation_id", payload.get("desired_active_generation"))
    clon = payload.get("desired_clone_id")
    return (generacion, clon) != (registro.desired_previsto_generation, registro.desired_previsto_clone)


def _restaurar_persistida(
    superficie: SuperficieCapturada,
    registro: RegistroRecuperacion,
) -> VerificacionSuperficie:
    """Devuelve la superficie a sus bytes previos y VERIFICA el resultado."""
    if superficie.path is None:
        return VerificacionSuperficie(kind=superficie.kind, ok=False, detail="la superficie no declara ruta")
    ruta = pathlib.Path(superficie.path)
    previos = superficie.bytes_previos()

    if superficie.kind == _KIND_ACTIVE and _activa_escrita_por_tercero(registro, superficie):
        return VerificacionSuperficie(
            kind=superficie.kind,
            ok=True,
            detail=(
                f"{ruta}: el Desired actual NO es el que esta transición iba a escribir —una escritura ajena "
                "ganó la carrera del CAS— así que NO se sobrescribe (FIX 2)"
            ),
            escritura_ajena=True,
        )

    try:
        if previos is None:
            if ruta.exists():
                ruta.unlink()
            if ruta.exists():
                return VerificacionSuperficie(
                    kind=superficie.kind, ok=False, detail=f"{ruta}: no existía antes y sigue existiendo"
                )
            return VerificacionSuperficie(
                kind=superficie.kind, ok=True, detail=f"{ruta}: ausencia restaurada (no existía antes)"
            )
        _escribir_bytes_atomico(ruta, previos)
        actuales = ruta.read_bytes()
    except (OSError, RecuperacionError) as exc:
        return VerificacionSuperficie(kind=superficie.kind, ok=False, detail=f"{ruta}: fallo al restaurar: {exc}")
    if _sha256(actuales) != superficie.sha256:
        return VerificacionSuperficie(
            kind=superficie.kind,
            ok=False,
            detail=f"{ruta}: el readback no coincide con el digest previo ({_sha256(actuales)} != {superficie.sha256})",
        )
    return VerificacionSuperficie(kind=superficie.kind, ok=True, detail=f"{ruta}: bytes previos restaurados")


def _restaurar_environ(
    superficie: SuperficieCapturada,
    environ: MutableMapping[str, str] | None,
) -> VerificacionSuperficie:
    if environ is None:
        return VerificacionSuperficie(
            kind=superficie.kind,
            ok=True,
            detail="no se administró el entorno en esta ejecución: la superficie derivada no se mutó",
        )
    previos = superficie.bytes_previos()
    if previos is None:
        environ.pop("SKYRIM_PATH", None)
        if "SKYRIM_PATH" in environ:
            return VerificacionSuperficie(
                kind=superficie.kind, ok=False, detail="SKYRIM_PATH no existía antes y sigue presente"
            )
        return VerificacionSuperficie(kind=superficie.kind, ok=True, detail="SKYRIM_PATH: ausencia restaurada")
    valor = previos.decode("utf-8", "surrogatepass")
    environ["SKYRIM_PATH"] = valor
    if environ.get("SKYRIM_PATH") != valor:
        return VerificacionSuperficie(kind=superficie.kind, ok=False, detail="SKYRIM_PATH no quedó con el valor previo")
    return VerificacionSuperficie(kind=superficie.kind, ok=True, detail="SKYRIM_PATH: valor previo restaurado")


def restaurar_registro_locked(
    handle: object,
    root: pathlib.Path,
    registro: RegistroRecuperacion,
    environ: MutableMapping[str, str] | None,
    *,
    solo_superficies: frozenset[str] | None = None,
) -> tuple[VerificacionSuperficie, ...]:
    """Restaura las superficies capturadas y devuelve la verificación de cada una.

    ``solo_superficies`` distingue las dos formas de saber qué se mutó:

    - **en proceso**: el binder sabe qué llamadas completaron y pasa ese
      conjunto exacto;
    - **tras un reinicio**: sólo queda la fase del registro, que se marca ANTES
      de cada mutación. El llamador pasa ``None`` y se restaura todo lo que la
      fase alcanzó, que puede incluir una superficie que el crash no llegó a
      mutar. Restaurarla escribe los MISMOS bytes que ya estaban: idempotente.

    Una superficie fuera del conjunto NO se toca y se declara como tal: no
    restaurar lo que no escribimos es la mitad de «no pisar a un tercero».

    No lanza por una superficie individual: devuelve el detalle para que el
    llamador decida. ``exigir_restauracion_completa`` es la puerta que convierte
    «alguna falló» en error.
    """
    alcanzadas = _superficies_alcanzadas_por_fase(registro.fase) if solo_superficies is None else solo_superficies
    verificaciones: list[VerificacionSuperficie] = []
    for superficie in registro.superficies:
        if superficie.kind not in alcanzadas:
            verificaciones.append(
                VerificacionSuperficie(
                    kind=superficie.kind,
                    ok=True,
                    detail="no mutada por esta transición: no se toca",
                )
            )
            continue
        if superficie.kind == _KIND_ENVIRON:
            verificaciones.append(_restaurar_environ(superficie, environ))
            continue
        verificaciones.append(_restaurar_persistida(superficie, registro))
    return tuple(verificaciones)


def _superficies_alcanzadas_por_fase(fase: FaseRecuperacion) -> frozenset[str]:
    """Qué superficies pudo haber mutado una transición que llegó a *fase*.

    El mapeo es explícito y total: una fase nueva sin entrada acá rompe el
    ``KeyError`` en vez de restaurar de menos en silencio.
    """
    mapa: dict[FaseRecuperacion, frozenset[str]] = {
        FaseRecuperacion.PREPARADO: frozenset(),
        FaseRecuperacion.INI_ESCRITO: frozenset({_KIND_MO2_INI}),
        FaseRecuperacion.CONFIG_ESCRITA: frozenset({_KIND_MO2_INI, _KIND_CONFIG}),
        FaseRecuperacion.ENTORNO_ACTUALIZADO: frozenset({_KIND_MO2_INI, _KIND_CONFIG, _KIND_ENVIRON}),
        FaseRecuperacion.DESEADO_ACTUALIZADO: frozenset({_KIND_MO2_INI, _KIND_CONFIG, _KIND_ENVIRON, _KIND_ACTIVE}),
    }
    return mapa[fase]


def exigir_restauracion_completa(verificaciones: tuple[VerificacionSuperficie, ...]) -> None:
    """Convierte una restauración parcial en ``RecuperacionError`` (fail-closed)."""
    fallidas = [v for v in verificaciones if not v.ok]
    if fallidas:
        detalle = "; ".join(f"{v.kind}: {v.detail}" for v in fallidas)
        raise RecuperacionError(f"la restauración NO cubrió todas las superficies: {detalle}")


# ---------------------------------------------------------------------------
# Recuperación tras reinicio (entrypoint público)
# ---------------------------------------------------------------------------


def _journal_finalizado(root: pathlib.Path, transition_id: str) -> bool:
    historial = derivar_historial_finalizados(root)
    return any(registro.transition_id == transition_id for registro in historial.registros)


def recuperar_transicion_interrumpida(
    root: pathlib.Path,
    *,
    transition_id: str | None = None,
    environ: MutableMapping[str, str] | None = None,
    timeout: float = 0.0,
    reclaim_orphaned: bool = True,
) -> ResultadoRecuperacion:
    """Recupera una transición interrumpida usando SÓLO evidencia durable.

    Este es el punto de entrada de un proceso NUEVO: no recibe ningún objeto en
    memoria del proceso que falló. Todo lo que usa viene del disco.

    ``reclaim_orphaned`` está en ``True`` a propósito y NO es una relajación del
    fail-closed: el proceso que dejó el lock tomado está muerto —es la premisa de
    «recuperar tras un reinicio»— y el camino de reclaim de ``root_lock`` prueba
    liveness (pid + ``create_time``) ANTES de apropiarse del lockfile; si el dueño
    sigue vivo, levanta. Con ``False`` la recuperación es imposible justo en el
    caso para el que existe.

    Reglas, en orden:

    1. Sin registro → ``SIN_REGISTRO`` (nada que hacer).
    2. La transición ya está FINALIZED en el historial → el registro es obsoleto:
       se retira SIN restaurar (restaurar destruiría una transición exitosa).
    3. El journal no está PENDING para esa transición → ``REGISTRO_NO_LIGADO``:
       no se toca nada y se pide al dueño.
    4. El journal está PENDING con la MISMA ``journal_revision`` y el mismo
       ``transition_id`` → se restauran las superficies y se verifica cada una.
       El journal NO se escribe: queda ``PENDING`` y lo clasifica su dueño.
    """
    ruta = pathlib.Path(os.path.abspath(os.fspath(root)))
    with participant_lock(ruta, timeout=timeout, reclaim_orphaned=reclaim_orphaned) as handle:
        handle.assert_owned()
        registros = listar_registros(ruta)
        if transition_id is not None:
            registro = cargar_registro(ruta, transition_id)
            registros = (registro,) if registro is not None else ()
        if not registros:
            return ResultadoRecuperacion(
                estado=EstadoRecuperacion.SIN_REGISTRO,
                transition_id=transition_id,
                fase=None,
                verificaciones=(),
                journal_state=None,
                requiere_dueno=False,
                motivo="no hay registros de recuperación en el storage",
            )
        if len(registros) > 1:
            return ResultadoRecuperacion(
                estado=EstadoRecuperacion.REGISTRO_NO_LIGADO,
                transition_id=None,
                fase=None,
                verificaciones=(),
                journal_state=None,
                requiere_dueno=True,
                motivo=(
                    f"hay {len(registros)} registros de recuperación y AT_MOST_ONE_PENDING_PER_ROOT exige uno: "
                    "la recuperación automática no elige (fail-closed)"
                ),
            )
        registro = registros[0]

        if _journal_finalizado(ruta, registro.transition_id):
            eliminar_registro_locked(handle, ruta, registro.transition_id)
            return ResultadoRecuperacion(
                estado=EstadoRecuperacion.TRANSICION_YA_FINALIZADA,
                transition_id=registro.transition_id,
                fase=registro.fase,
                verificaciones=(),
                journal_state=None,
                requiere_dueno=False,
                motivo=(
                    "la transición está FINALIZED en el historial: el registro era obsoleto y se retiró "
                    "SIN restaurar superficies"
                ),
            )

        snapshot = cargar_journal_transicion(ruta)
        journal = snapshot.journal
        if (
            snapshot.disposition not in (JournalDisposition.PENDING_PROMOTION, JournalDisposition.PENDING_ROLLBACK)
            or journal is None
            or journal.transition_id != registro.transition_id
            or journal.journal_revision != registro.journal_revision
        ):
            return ResultadoRecuperacion(
                estado=EstadoRecuperacion.REGISTRO_NO_LIGADO,
                transition_id=registro.transition_id,
                fase=registro.fase,
                verificaciones=(),
                journal_state=None if journal is None else journal.state,
                requiere_dueno=True,
                motivo=(
                    f"el registro declara transition_id={registro.transition_id!r} "
                    f"journal_revision={registro.journal_revision} y el journal está "
                    f"{snapshot.disposition.value}: no se restaura sobre evidencia que no liga"
                ),
            )

        verificaciones = restaurar_registro_locked(handle, ruta, registro, environ)
        exigir_restauracion_completa(verificaciones)
        return ResultadoRecuperacion(
            estado=EstadoRecuperacion.RESTAURADO_REQUIERE_DUENO,
            transition_id=registro.transition_id,
            fase=registro.fase,
            verificaciones=verificaciones,
            journal_state=journal.state,
            requiere_dueno=True,
            motivo=(
                f"superficies restauradas desde la fase {registro.fase.value}; el journal sigue "
                f"{journal.state.value} y su clasificación corresponde a journal.reconciliar_journal"
            ),
        )


def describir_entorno(environ: Mapping[str, str] | None) -> str | None:
    """Valor previo de ``SKYRIM_PATH`` tal como lo ve el binder."""
    if environ is None:
        return None
    return environ.get("SKYRIM_PATH")
