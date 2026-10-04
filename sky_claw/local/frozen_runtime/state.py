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
import json
import os
import pathlib
import tempfile

from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeStorageError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState, FrozenRuntimeStateLoadResult

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
    if schema != SCHEMA_VERSION:
        raise StateSchemaError(
            f"{source_label}: schema desconocido {schema}; soportado: {SCHEMA_VERSION} (fail-closed, "
            "no se reinterpreta silenciosamente)"
        )
    deseado = data.get("desired_active_generation")
    if deseado is not None:
        if not isinstance(deseado, str):
            raise StateSchemaError(f"{source_label}: desired_active_generation debe ser string o null")
        validar_generation_id(deseado)
    actualizado = data.get("updated_at_ns")
    if not isinstance(actualizado, int) or isinstance(actualizado, bool) or actualizado < 0:
        raise StateSchemaError(f"{source_label}: updated_at_ns debe ser un int >= 0")
    return FrozenRuntimeState(schema_version=schema, desired_active_generation=deseado, updated_at_ns=actualizado)


def load_frozen_runtime_state(path: pathlib.Path) -> FrozenRuntimeStateLoadResult:
    """Carga tipada del estado; distingue ausente (limpio) de corrupto (fail-closed)."""
    ruta = pathlib.Path(path)
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
    """
    ruta = pathlib.Path(path)
    payload = {
        "schema_version": state.schema_version,
        "desired_active_generation": state.desired_active_generation,
        "updated_at_ns": state.updated_at_ns,
    }
    _parsear_estado(json.dumps(payload), source_label=str(ruta))  # valida antes de escribir
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


def write_json_atomic(path: pathlib.Path, payload: dict[str, object]) -> None:
    """Escritura atómica genérica (temporal en el mismo directorio + ``os.replace``).

    Reutilizada por la metadata de generations (``state/generations/``) y por
    la inicialización del estado.
    """
    ruta = pathlib.Path(path)
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
