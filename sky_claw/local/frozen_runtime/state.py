"""Estado persistente de Frozen Runtime: ``state/active.json`` (P2).

Schema v1 — la intención de Sky-Claw (SFR-16):

```json
{"schema_version": 1, "desired_active_generation": null, "updated_at_ns": 0}
```

- ``desired_active_generation`` es el **Desired Active Generation**; este
  archivo NO registra Effective Runtime y jamás afirma por sí solo que
  MO2/SKSE estén ejecutando esa generación (eso se demuestra en P5).
- ``null`` = arranque limpio (sin Generation activa todavía).
- Reader fail-closed: ``state absent`` = arranque limpio; ``state present but
  corrupt`` (JSON malformado, truncado, UTF-8 inválido, schema desconocido,
  campo ausente o con tipo incorrecto) LANZA — nunca se interpreta como
  ausente.
- Escritura atómica (patrón del repo, ``local_config.py``): ``mkstemp`` en el
  MISMO directorio (mismo volumen) → write → flush → ``fsync`` del archivo →
  ``os.replace`` → cleanup del temporal ante fallo. El temporal nunca vive en
  ``%TEMP%`` global (podría estar en otro volumen).

POWER_LOSS_GUARANTEE = NOT_CLAIMED: ``os.replace`` es un reemplazo atómico del
namespace bajo semántica normal de proceso/filesystem local; no se afirma
durabilidad ante corte de energía (no hay WAL ni GP2).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import tempfile

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeStorageError,
    StateCasError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace, exigir_namespace_escribible
from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState, FrozenRuntimeStateLoadResult

SCHEMA_VERSIONS_SOPORTADOS = frozenset({1, 2})
SCHEMA_VERSION = 1
STATE_FILE_NAME = "active.json"


def _parsear_estado(raw: str, *, source_label: str) -> FrozenRuntimeState:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"{source_label}: JSON malformado o truncado: {exc}") from exc
    if not isinstance(data, dict):
        raise StateCorruptError(f"{source_label}: el estado debe ser un objeto JSON")

    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise StateSchemaError(f"{source_label}: schema_version debe ser int")
    if schema not in SCHEMA_VERSIONS_SOPORTADOS:
        raise StateSchemaError(
            f"{source_label}: schema desconocido {schema}; soportados: {sorted(SCHEMA_VERSIONS_SOPORTADOS)} (fail-closed, "
            "no se reinterpreta silenciosamente)"
        )

    actualizado = data.get("updated_at_ns")
    if not isinstance(actualizado, int) or isinstance(actualizado, bool) or actualizado < 0:
        raise StateSchemaError(f"{source_label}: updated_at_ns debe ser un int >= 0")

    if schema == 1:
        for campo in ("schema_version", "desired_active_generation", "updated_at_ns"):
            if campo not in data:
                raise StateSchemaError(f"{source_label}: falta el campo obligatorio '{campo}'")
        if "desired_clone_id" in data:
            raise StateSchemaError(
                f"{source_label}: 'desired_clone_id' queda fuera del schema v1 (use schema v2; fail-closed)"
            )
        deseado = data.get("desired_active_generation")
        if deseado is not None:
            if not isinstance(deseado, str):
                raise StateSchemaError(f"{source_label}: desired_active_generation debe ser string o null")
            deseado = validar_generation_id(deseado)
        return FrozenRuntimeState(
            schema_version=1,
            desired_active_generation=deseado,
            updated_at_ns=actualizado,
            desired_clone_id=None,
        )

    # Schema v2 (ADR 0012 §19a): el par desired_generation_id + desired_clone_id
    for campo in ("schema_version", "desired_generation_id", "desired_clone_id", "updated_at_ns"):
        if campo not in data:
            raise StateSchemaError(f"{source_label}: falta el campo obligatorio '{campo}'")

    if "desired_active_generation" in data:
        raise StateSchemaError(
            f"{source_label}: 'desired_active_generation' queda fuera del schema v2 (use 'desired_generation_id' y 'desired_clone_id')"
        )

    gen_id_raw = data.get("desired_generation_id")
    clone_id_raw = data.get("desired_clone_id")

    if (gen_id_raw is None) != (clone_id_raw is None):
        raise StateSchemaError(
            f"{source_label}: desired_generation_id y desired_clone_id deben ser ambos null o ambos no-null (fail-closed)"
        )

    deseado_gen: str | None = None
    deseado_clone: str | None = None
    if gen_id_raw is not None:
        if not isinstance(gen_id_raw, str):
            raise StateSchemaError(f"{source_label}: desired_generation_id debe ser string o null")
        deseado_gen = validar_generation_id(gen_id_raw)

        if not isinstance(clone_id_raw, str):
            raise StateSchemaError(f"{source_label}: desired_clone_id debe ser string o null")
        from sky_claw.local.frozen_runtime.clones import validar_clone_id

        deseado_clone = validar_clone_id(clone_id_raw)

    return FrozenRuntimeState(
        schema_version=2,
        desired_active_generation=deseado_gen,
        updated_at_ns=actualizado,
        desired_clone_id=deseado_clone,
    )


def load_frozen_runtime_state(path: pathlib.Path) -> FrozenRuntimeStateLoadResult:
    """Carga tipada del estado; distingue ausente (limpio) de corrupto (fail-closed).

    Un `active.json` redirigido (symlink/junction, incluso colgado) NUNCA es
    "ausente": se rechaza fail-closed — de lo contrario un restart perdería en
    silencio el estado deseado o leería un target externo.
    """
    ruta = pathlib.Path(path)
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise StateCorruptError(f"{ruta}: no se pudo inspeccionar el estado: {exc}") from exc
    if kind is not None:
        raise StateCorruptError(f"{ruta}: el estado es un enlace/reparse ({kind}): fail-closed")
    # Namespace read-side: si `state/` (o un ancestro) fue redirigido, leer a
    # través del enlace aceptaría estado externo o reportaría arranque limpio
    # cuando en el target no hay archivo. Un directorio ausente sí es válido
    # (pre-init ⇒ arranque limpio); un enlace no.
    motivo_padre = descripcion_de_enlace(ruta.parent)
    if motivo_padre is not None:
        raise StateCorruptError(f"{ruta}: namespace del estado redirigido: {motivo_padre}")
    if not ruta.exists():
        return FrozenRuntimeStateLoadResult(
            found=False, state=None, message="estado ausente: arranque limpio (sin Generation activa)"
        )
    try:
        raw = ruta.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise StateCorruptError(f"{ruta}: contenido no UTF-8: {exc}") from exc
    except OSError as exc:
        raise StateCorruptError(f"{ruta}: no se pudo leer: {exc}") from exc
    if not raw.strip():
        raise StateCorruptError(f"{ruta}: el estado está vacío (archivo de 0 bytes)")
    state = _parsear_estado(raw, source_label=str(ruta))
    return FrozenRuntimeStateLoadResult(found=True, state=state, message="estado cargado")


def save_frozen_runtime_state(path: pathlib.Path, state: FrozenRuntimeState) -> None:
    """Persiste el estado con escritura atómica (temporal en el mismo directorio).

    Ante cualquier fallo antes del ``os.replace``, el estado anterior permanece
    válido y el temporal se limpia. Nunca se escribe directamente sobre
    ``active.json``.

    Exclusión (P4-S2): la escritura del estado durable corre bajo el root lock,
    adquirido a partir del FrozenRuntimeRoot (``path.parent.parent`` — ``path`` es
    ``<root>/state/active.json``). El import es diferido para no crear el ciclo
    ``state → root_lock → storage → state``.
    """
    ruta = pathlib.Path(path)
    root = ruta.parent.parent
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    with participant_lock(root, timeout=0.0) as handle:
        handle.assert_owned()
        _save_frozen_runtime_state_locked(ruta, state)


def _save_frozen_runtime_state_locked(ruta: pathlib.Path, state: FrozenRuntimeState) -> None:
    """Cuerpo de la escritura de estado, ya bajo el root lock (no re-adquiere)."""
    if not isinstance(state.schema_version, int) or isinstance(state.schema_version, bool):
        raise StateSchemaError(f"schema_version debe ser int: {state.schema_version!r}")
    if state.schema_version not in SCHEMA_VERSIONS_SOPORTADOS:
        raise StateSchemaError(
            f"schema_version no soportado {state.schema_version}; soportados: {sorted(SCHEMA_VERSIONS_SOPORTADOS)} "
            "(fail-closed, no se degrada silenciosamente)"
        )

    if state.schema_version == 1 and state.desired_clone_id is not None:
        raise StateSchemaError(
            "schema_version 1 no admite desired_clone_id (use schema_version 2 para persistir la intención de clone; "
            "fail-closed para evitar pérdida silenciosa de autoridad)"
        )

    if state.schema_version == 2:
        payload = {
            "schema_version": 2,
            "desired_generation_id": state.desired_generation_id,
            "desired_clone_id": state.desired_clone_id,
            "updated_at_ns": state.updated_at_ns,
        }
    else:
        payload = {
            "schema_version": 1,
            "desired_active_generation": state.desired_active_generation,
            "updated_at_ns": state.updated_at_ns,
        }
    _parsear_estado(json.dumps(payload), source_label=str(ruta))  # valida antes de escribir
    # Re-admisión del namespace justo antes de escribir: si `state/` (o un
    # ancestro) fue reemplazado por un enlace DESPUÉS del init, mkstemp/
    # os.replace escaparían del root.
    exigir_namespace_escribible(ruta.parent)
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=ruta.parent, prefix=".active.json.", suffix=".tmp")
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"{ruta.parent}: no se pudo crear el temporal: {exc}") from exc
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def reservar_ruta_json_exclusiva(path: pathlib.Path) -> None:
    """Reserva *path* de forma EXCLUSIVA (``O_CREAT | O_EXCL``), sin escribir contenido.

    Es el single-winner de una RUTA -- hermano de ``mkdir(exist_ok=False)`` para un
    directorio. Si el destino ya existe (incluido un enlace colgado) falla con
    ``FileExistsError`` y NUNCA reemplaza lo que habia.

    Deja un placeholder vacio a proposito: a partir de aca el proceso que gano la
    reserva es el unico dueno de esa ruta, asi que ``write_json_atomic`` (que
    reemplaza) solo puede pisar SU PROPIO placeholder y nunca la evidencia de otro.
    Un fallo antes de la escritura deja el JSON vacio, que el lector clasifica como
    corrupcion: fail-closed, y sin riesgo para nada preexistente.

    No se usa ``os.link`` (rechazado por SFR-18 y por el oraculo de escritura). Se
    usa el modo exclusivo ``"x"`` de ``open`` a proposito: el oraculo AST que
    congela el vocabulario de escritura por modulo SI lo inspecciona, asi que la
    primitive queda visible y declarada en `MODULOS_CON_OPEN_ESCRITURA` (un
    ``os.open`` crudo escaparia a los dos oraculos).
    """
    exigir_namespace_escribible(path.parent)
    with open(path, "x", encoding="utf-8"):
        # Solo la CREACION exclusiva importa: el contenido lo escribe despues el
        # escritor atomico sobre ESTE placeholder, del que ya somos duenos.
        pass


def write_json_atomic(path: pathlib.Path, payload: dict[str, object]) -> None:
    """Escritura atómica genérica (temporal en el mismo directorio + ``os.replace``).

    Reutilizada por la metadata de generations (``state/generations/``) y por
    la inicialización del estado. Re-admite el namespace antes de escribir
    (falla cerrado si un ancestro fue redirigido por un enlace).
    """
    ruta = pathlib.Path(path)
    exigir_namespace_escribible(ruta.parent)
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=ruta.parent, prefix=".sky_claw_", suffix=".tmp")
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"{ruta.parent}: no se pudo crear el temporal: {exc}") from exc
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# CAS sobre ``state/active.json`` (P5-C1, FIX 2)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class EstadoPrevioObservado:
    """Identidad y contenido del ``active.json`` que un mutador observó.

    Es el token del CAS. ``sha256`` es el valor esperado del CONTENIDO;
    ``generation_id``/``clone_id`` son la identidad SEMÁNTICA del mismo estado.

    Se llevan los dos a propósito: el digest detecta cualquier cambio
    concurrente —incluido un ``updated_at_ns`` distinto con la misma intención—
    y la identidad hace que el error diga QUÉ cambió, en vez de sólo «el digest
    no coincide». Un CAS que sólo compara identidad deja pasar una escritura
    intermedia que vuelve al mismo par (ABA); uno que sólo compara bytes no
    puede explicarse.
    """

    ausente: bool
    sha256: str | None
    generation_id: str | None
    clone_id: str | None


def observar_estado_previo(path: pathlib.Path) -> EstadoPrevioObservado:
    """Lee el estado y devuelve su token de CAS. Ausente NO es corrupción."""
    ruta = pathlib.Path(path)
    try:
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return EstadoPrevioObservado(ausente=True, sha256=None, generation_id=None, clone_id=None)
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"{ruta}: no se pudo leer el estado para el CAS: {exc}") from exc
    carga = load_frozen_runtime_state(ruta)
    estado = carga.state
    return EstadoPrevioObservado(
        ausente=False,
        sha256=hashlib.sha256(crudo).hexdigest(),
        generation_id=None if estado is None else estado.desired_active_generation,
        clone_id=None if estado is None else estado.desired_clone_id,
    )


def escribir_estado_activo_cas(
    path: pathlib.Path,
    payload: dict[str, object],
    *,
    esperado: EstadoPrevioObservado,
) -> EstadoPrevioObservado:
    """Escribe ``active.json`` sólo si el estado previo sigue siendo el observado.

    **Participante público**: adquiere el root lock por su cuenta. Es la PUERTA,
    no una convención del llamador. Hasta P5-C1 el binder hacía
    ``write_json_atomic(active_state_path(...), payload)`` directo: si otro
    participante cambiaba el Desired entre el preflight y el CAS UPDATE, el
    binder lo pisaba en silencio.

    Dos barreras, no una: el root lock serializa a los participantes de Sky-Claw,
    y el CAS cubre a un escritor que NO participa del lock (o a un handle que lo
    perdió). El lock no reemplaza al CAS porque el lock sólo obliga a quien lo
    toma.

    Orden: validar forma → comparar contra el esperado → escribir → releer.

    Raises:
        StateCasError: el estado cambió desde que se observó (no se escribe).
        StateSchemaError: el payload no parsea como estado válido.
    """
    ruta = pathlib.Path(path)
    root = ruta.parent.parent
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    with participant_lock(root, timeout=0.0) as handle:
        handle.assert_owned()
        return _escribir_estado_activo_cas_locked(ruta, payload, esperado=esperado)


def _escribir_estado_activo_cas_locked(
    path: pathlib.Path,
    payload: dict[str, object],
    *,
    esperado: EstadoPrevioObservado,
) -> EstadoPrevioObservado:
    """Cuerpo del CAS, ya bajo el root lock (no re-adquiere)."""
    ruta = pathlib.Path(path)
    # La forma se valida ANTES de tocar el disco: un payload inválido no puede
    # dejar el estado a medio camino ni consumir el CAS.
    _parsear_estado(json.dumps(payload), source_label=f"{ruta} (CAS)")
    actual = observar_estado_previo(ruta)
    if actual.ausente != esperado.ausente:
        raise StateCasError(
            f"{ruta}: CAS fallido — se esperaba {'ausente' if esperado.ausente else 'presente'} y está "
            f"{'ausente' if actual.ausente else 'presente'}"
        )
    if actual.sha256 != esperado.sha256:
        raise StateCasError(
            f"{ruta}: CAS fallido — el contenido cambió desde que se observó "
            f"(esperado sha256={esperado.sha256}, actual sha256={actual.sha256})"
        )
    if (actual.generation_id, actual.clone_id) != (esperado.generation_id, esperado.clone_id):
        raise StateCasError(
            f"{ruta}: CAS fallido — la identidad cambió: se esperaba "
            f"(generation={esperado.generation_id!r}, clone={esperado.clone_id!r}) y hay "
            f"(generation={actual.generation_id!r}, clone={actual.clone_id!r})"
        )
    write_json_atomic(ruta, payload)
    despues = observar_estado_previo(ruta)
    generacion_nueva = payload.get("desired_generation_id", payload.get("desired_active_generation"))
    clone_nuevo = payload.get("desired_clone_id")
    if (despues.generation_id, despues.clone_id) != (generacion_nueva, clone_nuevo):
        raise FrozenRuntimeStorageError(
            f"{ruta}: el readback del CAS no coincide con lo escrito: se escribió "
            f"(generation={generacion_nueva!r}, clone={clone_nuevo!r}) y se leyó "
            f"(generation={despues.generation_id!r}, clone={despues.clone_id!r})"
        )
    return despues
