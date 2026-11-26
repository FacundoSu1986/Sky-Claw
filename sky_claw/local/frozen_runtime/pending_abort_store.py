"""Store durable de abortos autorizados de transiciones ``PENDING`` (``state/aborted_transitions/``).

Módulo de bajo nivel (hermano de ``transition_abandonment.py``, ADR 0012 §48):
no importa ``journal``, ``approval``, ``binder_recovery``, ``runtime_clone_binder`` ni
``autorizacion_hitl_transicion``, de modo que todos esos consumidores pueden leer y validar
registros de aborto bajo ``participant_lock`` sin ciclos de importación.

No invoca primitivas de escritura directa (``open``/``replace``/``unlink``): toda escritura
delega en ``state.reservar_ruta_json_exclusiva`` y ``state.write_json_atomic``, manteniendo
intacto el boundary AST de ``test_frozen_runtime_p1_readonly.py``.

Propiedades del mecanismo (D3, D4, D5, D8):
- Esquema v1 CERRADO con ``record_digest`` recalculado al leer.
- Creación single-winner (``O_EXCL``). Un placeholder vacío de 0 bytes dejado por un crash
  entre la reserva exclusiva y la publicación de ``INTENCION`` se distingue de un archivo
  corrupto no vacío: el reintento del dueño puede completarlo con identidad y confirmaciones
  explícitas; un archivo corrupto no vacío jamás se sobrescribe (``AbortoCorruptoError``).
- Mientras exista cualquier registro con ``estado != COMPLETO`` (o un placeholder vacío),
  ``abortos_incompletos()`` bloquea ``CREATE``, ``START_NEXT``, ``consumir_aprobacion`` y el
  binder (D4).
- Compatibilidad de roots históricos (D8): un store ausente sólo se admite cuando el root y
  ``state/`` pasan admisión física sin enlaces/reparse y el journal no está en un estado
  inalcanzable sin aborto (``NONE`` con ``last_finalized_transition_id=None``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import uuid
from collections.abc import Mapping
from enum import StrEnum

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime import storage
from sky_claw.local.frozen_runtime.errors import (
    AbortoCorruptoError,
    AbortoPendienteInvalidoError,
)
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

SCHEMA_VERSION_ABORTO = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: Centinela para distinguir un placeholder vacío (0 bytes) de ausencia o corrupción (D3).
PLACEHOLDER_VACIO = "PLACEHOLDER_VACIO"


class EstadoSuperficieConfirmada(StrEnum):
    """Unión etiquetada de presencia/ausencia confirmada por el propietario (RQ-1)."""

    PRESENTE = "PRESENTE"
    AUSENTE = "AUSENTE"


class EstadoAbortoPendiente(StrEnum):
    """Máquina de estados durable y monótona del aborto autorizado (§48.3)."""

    INTENCION = "INTENCION"
    SUPERFICIES_VERIFICADAS = "SUPERFICIES_VERIFICADAS"
    JOURNAL_NONE = "JOURNAL_NONE"
    COMPLETO = "COMPLETO"


ORDEN_ESTADOS_ABORTO: tuple[EstadoAbortoPendiente, ...] = (
    EstadoAbortoPendiente.INTENCION,
    EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS,
    EstadoAbortoPendiente.JOURNAL_NONE,
    EstadoAbortoPendiente.COMPLETO,
)


class ModoAbortoPendiente(StrEnum):
    """Distingue aborto con snapshot durable de recuperación vs frontera IP-0 sin registro."""

    CON_REGISTRO_RECUPERACION = "CON_REGISTRO_RECUPERACION"
    IP0_SIN_REGISTRO = "IP0_SIN_REGISTRO"


@dataclasses.dataclass(frozen=True, slots=True)
class ConfirmacionSuperficie:
    """Confirmación explícita del dueño sobre el estado observado de UNA superficie.

    Autoriza el descarte de ese contenido exacto (o ausencia exacta); NUNCA acredita
    que Sky-Claw haya sido el autor de los bytes en disco.
    """

    estado: EstadoSuperficieConfirmada
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.estado, EstadoSuperficieConfirmada):
            raise AbortoPendienteInvalidoError(
                f"estado de confirmación inválido: {self.estado!r} (se exige EstadoSuperficieConfirmada)"
            )
        if self.estado is EstadoSuperficieConfirmada.PRESENTE:
            if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
                raise AbortoPendienteInvalidoError(
                    "una confirmación PRESENTE exige sha256 hexadecimal completo de 64 caracteres en minúsculas"
                )
        elif self.sha256 is not None:
            raise AbortoPendienteInvalidoError("una confirmación AUSENTE no puede declarar sha256")

    def como_dict(self) -> dict[str, object]:
        return {"estado": self.estado.value, "sha256": self.sha256}


@dataclasses.dataclass(frozen=True, slots=True)
class RegistroAbortoPendiente:
    """Registro durable, sellado por digest y de esquema cerrado del aborto autorizado."""

    schema_version: int
    root_id: str
    transition_id: str
    journal_revision: int
    approval_id: str
    approval_seq: int
    scope_digest: str
    modo: ModoAbortoPendiente
    recovery_record_digest: str | None
    confirmaciones: dict[str, ConfirmacionSuperficie]
    superficies_antes: dict[str, dict[str, object]]
    superficies_despues: dict[str, dict[str, object]] | None
    superficies_verificadas_digest: str | None
    propietario_sujeto: str
    propietario_superficie: str
    propietario_sesion: str
    propietario_mecanismo: str
    motivo: str
    estado: EstadoAbortoPendiente
    creado_en_ns: int
    actualizado_en_ns: int
    record_digest: str


_CAMPOS_REGISTRO_ABORTO: frozenset[str] = frozenset(
    {
        "schema_version",
        "root_id",
        "transition_id",
        "journal_revision",
        "approval_id",
        "approval_seq",
        "scope_digest",
        "modo",
        "recovery_record_digest",
        "confirmaciones",
        "superficies_antes",
        "superficies_despues",
        "superficies_verificadas_digest",
        "propietario_sujeto",
        "propietario_superficie",
        "propietario_sesion",
        "propietario_mecanismo",
        "motivo",
        "estado",
        "creado_en_ns",
        "actualizado_en_ns",
        "record_digest",
    }
)


def _digest_canonico_local(payload: object) -> str:
    crudo = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(crudo).hexdigest()


def calcular_digest_aborto(payload: Mapping[str, object]) -> str:
    """Calcula el digest canónico sobre todos los campos del registro salvo ``record_digest``."""
    data = {k: v for k, v in payload.items() if k != "record_digest"}
    return _digest_canonico_local(data)


def exigir_transition_id_canonico(valor: object) -> str:
    """UUID v4 canónico en minúsculas (misma regla que ``journal`` y ``transition_abandonment``)."""
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor.strip():
        raise AbortoPendienteInvalidoError("transition_id debe ser una cadena no vacía")
    try:
        parseado = uuid.UUID(valor)
    except ValueError as exc:
        raise AbortoPendienteInvalidoError(f"transition_id no es un UUID válido: {valor!r}") from exc
    if parseado.version != 4 or str(parseado) != valor:
        raise AbortoPendienteInvalidoError(f"transition_id debe ser un UUID v4 canónico en minúsculas: {valor!r}")
    return valor


def _exigir_store_admitido(dir_abortos: pathlib.Path) -> bool:
    """``True`` si ``state/aborted_transitions/`` existe y es directorio real; ``False`` si está ausente.

    Un enlace/reparse, archivo en su lugar o ancestro ilegible levantan ``AbortoCorruptoError``.
    """
    admision = storage.admitir_directorio_storage(dir_abortos)
    if not admision.success:
        raise AbortoCorruptoError(f"el store de abortos fue rechazado: {admision.message}")
    return dir_abortos.is_dir()


def _exigir_archivo_sin_enlace(ruta: pathlib.Path) -> None:
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError as exc:
        raise AbortoCorruptoError(f"no se pudo inspeccionar el registro de aborto {ruta}: {exc}") from exc
    if tipo is not None:
        raise AbortoCorruptoError(
            f"el registro de aborto {ruta} es un enlace/reparse ({tipo}): no se lee a través de él (fail-closed)"
        )


def es_placeholder_vacio_aborto(ruta: pathlib.Path) -> bool:
    """``True`` sólo si ``ruta`` es un archivo regular sin enlace de exactamente 0 bytes (D3)."""
    _exigir_archivo_sin_enlace(ruta)
    if not ruta.is_file():
        return False
    try:
        return ruta.stat().st_size == 0
    except OSError as exc:
        raise AbortoCorruptoError(f"no se pudo inspeccionar el tamaño de {ruta}: {exc}") from exc


def _texto(contenido: Mapping[str, object], campo: str, ruta: pathlib.Path) -> str:
    valor = contenido[campo]
    if not isinstance(valor, str) or not valor.strip():
        raise AbortoCorruptoError(f"registro de aborto {ruta}: '{campo}' debe ser string no vacío")
    return valor


def _entero(contenido: Mapping[str, object], campo: str, ruta: pathlib.Path, *, minimo: int) -> int:
    valor = contenido[campo]
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo:
        raise AbortoCorruptoError(f"registro de aborto {ruta}: '{campo}' debe ser entero >= {minimo}")
    return valor


def _sha_opcional(valor: object, campo: str, ruta: pathlib.Path) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str) or _SHA256_RE.fullmatch(valor) is None:
        raise AbortoCorruptoError(f"registro de aborto {ruta}: '{campo}' debe ser SHA-256 hex de 64 chars o null")
    return valor


def _parsear_confirmaciones(raw: object, ruta: pathlib.Path) -> dict[str, ConfirmacionSuperficie]:
    if not isinstance(raw, dict):
        raise AbortoCorruptoError(f"registro de aborto {ruta}: 'confirmaciones' debe ser un objeto JSON")
    salida: dict[str, ConfirmacionSuperficie] = {}
    for kind, item in raw.items():
        if not isinstance(kind, str) or not kind.strip():
            raise AbortoCorruptoError(f"registro de aborto {ruta}: clave de confirmación inválida: {kind!r}")
        if not isinstance(item, dict) or set(item.keys()) != {"estado", "sha256"}:
            raise AbortoCorruptoError(f"registro de aborto {ruta}: confirmación malformada para '{kind}'")
        try:
            estado = EstadoSuperficieConfirmada(item["estado"])
            sha = item["sha256"]
            salida[kind] = ConfirmacionSuperficie(estado=estado, sha256=sha)
        except (ValueError, AbortoPendienteInvalidoError) as exc:
            raise AbortoCorruptoError(f"registro de aborto {ruta}: confirmación inválida para '{kind}': {exc}") from exc
    return salida


def _parsear_mapa_superficies(
    raw: object, campo: str, ruta: pathlib.Path, *, permitir_none: bool
) -> dict[str, dict[str, object]] | None:
    if raw is None:
        if permitir_none:
            return None
        raise AbortoCorruptoError(f"registro de aborto {ruta}: '{campo}' no puede ser null")
    if not isinstance(raw, dict):
        raise AbortoCorruptoError(f"registro de aborto {ruta}: '{campo}' debe ser un objeto JSON")
    salida: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            raise AbortoCorruptoError(f"registro de aborto {ruta}: entrada inválida en '{campo}'")
        salida[k] = dict(v)
    return salida


def serializar_registro_aborto(registro: RegistroAbortoPendiente) -> dict[str, object]:
    """Serializa el registro al esquema cerrado v1 con su ``record_digest``."""
    return {
        "schema_version": registro.schema_version,
        "root_id": registro.root_id,
        "transition_id": registro.transition_id,
        "journal_revision": registro.journal_revision,
        "approval_id": registro.approval_id,
        "approval_seq": registro.approval_seq,
        "scope_digest": registro.scope_digest,
        "modo": registro.modo.value,
        "recovery_record_digest": registro.recovery_record_digest,
        "confirmaciones": {k: v.como_dict() for k, v in sorted(registro.confirmaciones.items())},
        "superficies_antes": {k: dict(v) for k, v in sorted(registro.superficies_antes.items())},
        "superficies_despues": (
            None
            if registro.superficies_despues is None
            else {k: dict(v) for k, v in sorted(registro.superficies_despues.items())}
        ),
        "superficies_verificadas_digest": registro.superficies_verificadas_digest,
        "propietario_sujeto": registro.propietario_sujeto,
        "propietario_superficie": registro.propietario_superficie,
        "propietario_sesion": registro.propietario_sesion,
        "propietario_mecanismo": registro.propietario_mecanismo,
        "motivo": registro.motivo,
        "estado": registro.estado.value,
        "creado_en_ns": registro.creado_en_ns,
        "actualizado_en_ns": registro.actualizado_en_ns,
        "record_digest": registro.record_digest,
    }


def construir_registro_aborto(
    *,
    root_id: str,
    transition_id: str,
    journal_revision: int,
    approval_id: str,
    approval_seq: int,
    scope_digest: str,
    modo: ModoAbortoPendiente,
    recovery_record_digest: str | None,
    confirmaciones: Mapping[str, ConfirmacionSuperficie],
    superficies_antes: Mapping[str, Mapping[str, object]],
    superficies_despues: Mapping[str, Mapping[str, object]] | None,
    superficies_verificadas_digest: str | None,
    propietario_sujeto: str,
    propietario_superficie: str,
    propietario_sesion: str,
    propietario_mecanismo: str,
    motivo: str,
    estado: EstadoAbortoPendiente,
    creado_en_ns: int,
    actualizado_en_ns: int,
) -> RegistroAbortoPendiente:
    """Construye un ``RegistroAbortoPendiente`` sellado con su ``record_digest`` canónico."""
    borrador: dict[str, object] = {
        "schema_version": SCHEMA_VERSION_ABORTO,
        "root_id": root_id,
        "transition_id": transition_id,
        "journal_revision": journal_revision,
        "approval_id": approval_id,
        "approval_seq": approval_seq,
        "scope_digest": scope_digest,
        "modo": modo.value,
        "recovery_record_digest": recovery_record_digest,
        "confirmaciones": {k: v.como_dict() for k, v in sorted(confirmaciones.items())},
        "superficies_antes": {k: dict(v) for k, v in sorted(superficies_antes.items())},
        "superficies_despues": (
            None if superficies_despues is None else {k: dict(v) for k, v in sorted(superficies_despues.items())}
        ),
        "superficies_verificadas_digest": superficies_verificadas_digest,
        "propietario_sujeto": propietario_sujeto,
        "propietario_superficie": propietario_superficie,
        "propietario_sesion": propietario_sesion,
        "propietario_mecanismo": propietario_mecanismo,
        "motivo": motivo,
        "estado": estado.value,
        "creado_en_ns": creado_en_ns,
        "actualizado_en_ns": actualizado_en_ns,
    }
    digest = calcular_digest_aborto(borrador)
    return RegistroAbortoPendiente(
        schema_version=SCHEMA_VERSION_ABORTO,
        root_id=root_id,
        transition_id=transition_id,
        journal_revision=journal_revision,
        approval_id=approval_id,
        approval_seq=approval_seq,
        scope_digest=scope_digest,
        modo=modo,
        recovery_record_digest=recovery_record_digest,
        confirmaciones=dict(confirmaciones),
        superficies_antes={k: dict(v) for k, v in superficies_antes.items()},
        superficies_despues=(
            None if superficies_despues is None else {k: dict(v) for k, v in superficies_despues.items()}
        ),
        superficies_verificadas_digest=superficies_verificadas_digest,
        propietario_sujeto=propietario_sujeto,
        propietario_superficie=propietario_superficie,
        propietario_sesion=propietario_sesion,
        propietario_mecanismo=propietario_mecanismo,
        motivo=motivo,
        estado=estado,
        creado_en_ns=creado_en_ns,
        actualizado_en_ns=actualizado_en_ns,
        record_digest=digest,
    )


def cargar_registro_aborto(
    ruta: pathlib.Path,
    *,
    expected_root_id: str | None = None,
) -> RegistroAbortoPendiente:
    """Lee y valida estrictamente un registro durable de aborto. Falla cerrado ante cualquier anomalía."""
    p = pathlib.Path(ruta)
    _exigir_archivo_sin_enlace(p)
    if not p.is_file():
        raise AbortoPendienteInvalidoError(f"el archivo de registro de aborto no existe: {p}")
    try:
        crudo = p.read_bytes()
    except OSError as exc:
        raise AbortoCorruptoError(f"no se pudo leer el registro de aborto {p}: {exc}") from exc
    if not crudo:
        raise AbortoCorruptoError(f"el registro de aborto {p} está vacío (0 bytes / placeholder incompleto)")
    try:
        contenido = json.loads(crudo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AbortoCorruptoError(f"el registro de aborto en {p} no es JSON UTF-8 válido: {exc}") from exc
    if not isinstance(contenido, dict):
        raise AbortoCorruptoError(f"el registro de aborto en {p} debe ser un objeto JSON")

    version = contenido.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION_ABORTO:
        raise AbortoCorruptoError(f"versión de schema no soportada en {p}: {version!r}")

    campos = frozenset(contenido)
    if campos != _CAMPOS_REGISTRO_ABORTO:
        raise AbortoCorruptoError(
            f"el registro de aborto en {p} no respeta el esquema cerrado "
            f"(faltan: {sorted(_CAMPOS_REGISTRO_ABORTO - campos)}; sobran: {sorted(campos - _CAMPOS_REGISTRO_ABORTO)})"
        )

    root_id = _texto(contenido, "root_id", p)
    t_id = _texto(contenido, "transition_id", p)
    try:
        exigir_transition_id_canonico(t_id)
    except AbortoPendienteInvalidoError as exc:
        raise AbortoCorruptoError(f"registro de aborto {p}: {exc}") from exc

    journal_revision = _entero(contenido, "journal_revision", p, minimo=0)
    approval_id = _texto(contenido, "approval_id", p)
    approval_seq = _entero(contenido, "approval_seq", p, minimo=1)
    scope_digest = _sha_opcional(contenido["scope_digest"], "scope_digest", p)
    if scope_digest is None:
        raise AbortoCorruptoError(f"registro de aborto {p}: 'scope_digest' no puede ser null")

    try:
        modo = ModoAbortoPendiente(contenido["modo"])
    except ValueError as exc:
        raise AbortoCorruptoError(f"registro de aborto {p}: 'modo' inválido: {contenido['modo']!r}") from exc

    recovery_digest = _sha_opcional(contenido["recovery_record_digest"], "recovery_record_digest", p)
    if modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION and recovery_digest is None:
        raise AbortoCorruptoError(f"registro de aborto {p}: CON_REGISTRO_RECUPERACION exige recovery_record_digest")
    if modo is ModoAbortoPendiente.IP0_SIN_REGISTRO and recovery_digest is not None:
        raise AbortoCorruptoError(f"registro de aborto {p}: IP0_SIN_REGISTRO no puede declarar recovery_record_digest")

    confirmaciones = _parsear_confirmaciones(contenido["confirmaciones"], p)
    superficies_antes = _parsear_mapa_superficies(
        contenido["superficies_antes"], "superficies_antes", p, permitir_none=False
    )
    assert superficies_antes is not None
    superficies_despues = _parsear_mapa_superficies(
        contenido["superficies_despues"], "superficies_despues", p, permitir_none=True
    )
    verif_digest = _sha_opcional(contenido["superficies_verificadas_digest"], "superficies_verificadas_digest", p)

    try:
        estado = EstadoAbortoPendiente(contenido["estado"])
    except ValueError as exc:
        raise AbortoCorruptoError(f"registro de aborto {p}: 'estado' inválido: {contenido['estado']!r}") from exc

    if estado is EstadoAbortoPendiente.INTENCION:
        if verif_digest is not None:
            raise AbortoCorruptoError(
                f"registro de aborto {p}: INTENCION no puede declarar superficies_verificadas_digest"
            )
    elif verif_digest is None or superficies_despues is None:
        raise AbortoCorruptoError(
            f"registro de aborto {p}: estado {estado.value} exige superficies_despues y superficies_verificadas_digest"
        )

    sujeto = _texto(contenido, "propietario_sujeto", p)
    superficie = _texto(contenido, "propietario_superficie", p)
    sesion = _texto(contenido, "propietario_sesion", p)
    mecanismo = _texto(contenido, "propietario_mecanismo", p)
    motivo = _texto(contenido, "motivo", p)
    creado_en_ns = _entero(contenido, "creado_en_ns", p, minimo=1)
    actualizado_en_ns = _entero(contenido, "actualizado_en_ns", p, minimo=1)
    if actualizado_en_ns < creado_en_ns:
        raise AbortoCorruptoError(f"registro de aborto {p}: actualizado_en_ns < creado_en_ns")
    digest_declarado = _sha_opcional(contenido["record_digest"], "record_digest", p)
    if digest_declarado is None:
        raise AbortoCorruptoError(f"registro de aborto {p}: 'record_digest' no puede ser null")

    if expected_root_id is not None and root_id != expected_root_id:
        raise AbortoCorruptoError(
            f"el root_id del registro de aborto ({root_id}) no coincide con el esperado ({expected_root_id})"
        )

    digest_calculado = calcular_digest_aborto(contenido)
    if digest_declarado != digest_calculado:
        raise AbortoCorruptoError(
            f"el digest del registro de aborto en {p} está corrupto "
            f"(declarado={digest_declarado}, calculado={digest_calculado})"
        )

    return RegistroAbortoPendiente(
        schema_version=SCHEMA_VERSION_ABORTO,
        root_id=root_id,
        transition_id=t_id,
        journal_revision=journal_revision,
        approval_id=approval_id,
        approval_seq=approval_seq,
        scope_digest=scope_digest,
        modo=modo,
        recovery_record_digest=recovery_digest,
        confirmaciones=confirmaciones,
        superficies_antes=superficies_antes,
        superficies_despues=superficies_despues,
        superficies_verificadas_digest=verif_digest,
        propietario_sujeto=sujeto,
        propietario_superficie=superficie,
        propietario_sesion=sesion,
        propietario_mecanismo=mecanismo,
        motivo=motivo,
        estado=estado,
        creado_en_ns=creado_en_ns,
        actualizado_en_ns=actualizado_en_ns,
        record_digest=digest_declarado,
    )


def leer_estado_archivo_aborto(storage_root: pathlib.Path, transition_id: str) -> RegistroAbortoPendiente | str | None:
    """Inspecciona el archivo de aborto de ``transition_id`` distinguiendo placeholder vacío (D3).

    Devuelve:
    - ``None`` si el store o el archivo no existen;
    - ``PLACEHOLDER_VACIO`` si el archivo existe con 0 bytes (creación single-winner interrumpida);
    - ``RegistroAbortoPendiente`` si existe y es válido;
    - levanta ``AbortoCorruptoError`` ante cualquier corrupción, enlace o discrepancia de nombre.
    """
    t_id = exigir_transition_id_canonico(transition_id)
    if not _exigir_store_admitido(storage.aborted_transitions_state_dir(storage_root)):
        return None
    ruta = storage.aborted_transition_record_path(storage_root, t_id)
    _exigir_archivo_sin_enlace(ruta)
    if not ruta.exists():
        return None
    if es_placeholder_vacio_aborto(ruta):
        return PLACEHOLDER_VACIO
    reg = cargar_registro_aborto(ruta, expected_root_id=canonical_root_key(storage_root)[0])
    if reg.transition_id != t_id:
        raise AbortoCorruptoError(
            f"el registro de aborto {ruta.name} declara {reg.transition_id!r}: nombre y contenido no coinciden"
        )
    return reg


def registro_de_aborto(storage_root: pathlib.Path, transition_id: str) -> RegistroAbortoPendiente | None:
    """Registro de aborto VÁLIDO de ``transition_id``, o ``None`` si no existe.

    Un placeholder vacío (0 bytes) o cualquier archivo corrupto levanta ``AbortoCorruptoError``
    para que ningún consumidor (journal, binder, aprobación) lo interprete como «sin aborto».
    """
    try:
        t_id = exigir_transition_id_canonico(transition_id)
    except AbortoPendienteInvalidoError:
        return None
    estado = leer_estado_archivo_aborto(storage_root, t_id)
    if estado is None:
        return None
    if estado == PLACEHOLDER_VACIO:
        raise AbortoCorruptoError(
            f"existe un placeholder vacío de aborto en curso para {t_id!r} (creación interrumpida): fail-closed"
        )
    assert isinstance(estado, RegistroAbortoPendiente)
    return estado


def cargar_abortos(
    storage_root: pathlib.Path,
    *,
    ignorar_placeholder_de: str | None = None,
) -> dict[str, RegistroAbortoPendiente]:
    """Carga todos los registros de aborto del storage. Falla cerrado ante corrupción o placeholders vacíos.

    Si ``ignorar_placeholder_de`` es un ``transition_id``, permite omitir únicamente el
    placeholder vacío (0 bytes) de esa transición al reanudar su propia creación interrumpida (D3/D4),
    sin relajar la validación de ningún otro archivo del store.

    Usa ``iterdir()`` y no ``glob()`` (en Python 3.11 ``glob`` suprime ``PermissionError``).
    """
    dir_abortos = storage.aborted_transitions_state_dir(storage_root)
    if not _exigir_store_admitido(dir_abortos):
        return {}
    esperado_root = canonical_root_key(storage_root)[0]
    registros: dict[str, RegistroAbortoPendiente] = {}
    try:
        entradas = sorted(dir_abortos.iterdir())
    except OSError as exc:
        raise AbortoCorruptoError(f"no se pudo enumerar el store de abortos {dir_abortos}: {exc}") from exc
    for ruta in entradas:
        if ruta.suffix != ".json":
            continue
        if (
            ignorar_placeholder_de is not None
            and ruta.stem == ignorar_placeholder_de
            and es_placeholder_vacio_aborto(ruta)
        ):
            continue
        reg = cargar_registro_aborto(ruta, expected_root_id=esperado_root)
        if ruta.stem != reg.transition_id:
            raise AbortoCorruptoError(
                f"el registro de aborto {ruta.name} declara {reg.transition_id!r}: nombre y contenido no coinciden"
            )
        registros[reg.transition_id] = reg
    return registros


def abortos_incompletos(storage_root: pathlib.Path) -> tuple[RegistroAbortoPendiente, ...]:
    """Registros en ``state/aborted_transitions/`` cuyo estado NO es ``COMPLETO`` (D4).

    Un placeholder vacío o registro corrupto levanta ``AbortoCorruptoError`` (fail-closed).
    """
    registros = cargar_abortos(storage_root)
    return tuple(r for r in registros.values() if r.estado is not EstadoAbortoPendiente.COMPLETO)


def exigir_store_abortos_coherente(
    storage_root: pathlib.Path,
    *,
    journal_disposition_value: str,
    last_finalized_transition_id: str | None,
) -> dict[str, RegistroAbortoPendiente]:
    """Valida el store de abortos y evita tratar un store ausente como «sin abortos» cuando contradice el journal (D8).

    En el ciclo de vida del journal, ``NONE`` con ``last_finalized_transition_id is None``
    sólo es alcanzable mediante un aborto autorizado sobre un bootstrap sin historial. Si el
    store está ausente o no contiene ningún aborto ``COMPLETO`` en ese estado, se falla cerrado.
    """
    dir_abortos = storage.aborted_transitions_state_dir(storage_root)
    existe_store = _exigir_store_admitido(dir_abortos)
    abortos = cargar_abortos(storage_root)
    if (
        journal_disposition_value == "NONE"
        and last_finalized_transition_id is None
        and (not existe_store or not any(r.estado is EstadoAbortoPendiente.COMPLETO for r in abortos.values()))
    ):
        raise AbortoCorruptoError(
            "el journal está en NONE sin last_finalized_transition_id (bootstrap abortado), pero no existe "
            "evidencia durable de un aborto COMPLETO en state/aborted_transitions/: no se interpreta como "
            "ausencia de abortos (fail-closed, D8)"
        )
    return abortos


def crear_intencion_aborto_locked(
    storage_root: pathlib.Path,
    registro: RegistroAbortoPendiente,
    *,
    completar_placeholder_vacio: bool = False,
) -> RegistroAbortoPendiente:
    """Persiste la intención inicial (``INTENCION``) de forma single-winner (``O_EXCL`` + ``write_json_atomic``)."""
    if registro.estado is not EstadoAbortoPendiente.INTENCION:
        raise AbortoPendienteInvalidoError("la creación inicial del registro de aborto exige estado INTENCION")
    storage.asegurar_directorio_abortos(storage_root)
    ruta = storage.aborted_transition_record_path(storage_root, registro.transition_id)
    try:
        state_module.reservar_ruta_json_exclusiva(ruta)
    except FileExistsError:
        if not (completar_placeholder_vacio and es_placeholder_vacio_aborto(ruta)):
            raise AbortoCorruptoError(
                f"ya existe un archivo de aborto en {ruta}: no se sobrescribe evidencia preexistente"
            ) from None
    state_module.write_json_atomic(ruta, serializar_registro_aborto(registro))
    return cargar_registro_aborto(ruta, expected_root_id=canonical_root_key(storage_root)[0])


def avanzar_estado_aborto_locked(
    storage_root: pathlib.Path,
    previo: RegistroAbortoPendiente,
    nuevo: RegistroAbortoPendiente,
) -> RegistroAbortoPendiente:
    """Avanza monótonamente el estado del registro de aborto existente con validación estricta."""
    if previo.transition_id != nuevo.transition_id or previo.root_id != nuevo.root_id:
        raise AbortoPendienteInvalidoError("no se puede avanzar un registro de aborto cambiando su identidad")
    idx_prev = ORDEN_ESTADOS_ABORTO.index(previo.estado)
    idx_new = ORDEN_ESTADOS_ABORTO.index(nuevo.estado)
    if idx_new <= idx_prev:
        raise AbortoPendienteInvalidoError(
            f"el estado de aborto debe avanzar monótonamente ({previo.estado.value} → {nuevo.estado.value} inválido)"
        )
    ruta = storage.aborted_transition_record_path(storage_root, nuevo.transition_id)
    _exigir_archivo_sin_enlace(ruta)
    state_module.write_json_atomic(ruta, serializar_registro_aborto(nuevo))
    return cargar_registro_aborto(ruta, expected_root_id=canonical_root_key(storage_root)[0])


__all__ = [
    "ORDEN_ESTADOS_ABORTO",
    "PLACEHOLDER_VACIO",
    "SCHEMA_VERSION_ABORTO",
    "ConfirmacionSuperficie",
    "EstadoAbortoPendiente",
    "EstadoSuperficieConfirmada",
    "ModoAbortoPendiente",
    "RegistroAbortoPendiente",
    "abortos_incompletos",
    "avanzar_estado_aborto_locked",
    "calcular_digest_aborto",
    "cargar_abortos",
    "cargar_registro_aborto",
    "construir_registro_aborto",
    "crear_intencion_aborto_locked",
    "es_placeholder_vacio_aborto",
    "exigir_store_abortos_coherente",
    "exigir_transition_id_canonico",
    "leer_estado_archivo_aborto",
    "registro_de_aborto",
    "serializar_registro_aborto",
]
