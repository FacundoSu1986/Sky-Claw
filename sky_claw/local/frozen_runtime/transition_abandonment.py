"""Registro durable de ABANDONO de transiciones consumidas (``state/abandoned_transitions/``).

Vive en un módulo de bajo nivel —sin importar ``autorizacion_hitl_transicion`` ni el binder— por una
razón de mecanismo, no de orden: el abandono sólo protege si TODOS los que pueden reanudar una
transición lo consultan **bajo el mismo ``participant_lock``**. ``SesionTransicionGoldenMaster.reanudar``
lo mira al diagnosticar (sin lock), así que entre ese diagnóstico y el lock del binder otra sesión puede
abandonar; por eso el binder lo vuelve a mirar dentro de su sección crítica
(:func:`registro_de_abandono`), y por eso el lector tiene que ser importable desde el binder sin ciclo.
Ancla: ``tests/test_frozen_runtime_p7_a5_pr3_hitl_wiring.py`` (intercalado real entre diagnóstico y lock).

Durabilidad: igual que el resto del subsistema (``approval.py``, ``binder_recovery.py``),
``POWER_LOSS_GUARANTEE = NOT_CLAIMED`` y ``DIRECTORY_FSYNC = NOT_CLAIMED``: ``os.replace`` y el ``fsync`` del
archivo son lo máximo que se afirma. Perder un registro tras un corte de energía deja la transición
BLOQUEADA (el lado seguro) y el abandono es idempotente: el dueño lo reitera.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import uuid
from collections.abc import Mapping

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import storage
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
from sky_claw.local.frozen_runtime.journal import digest_canonico
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key


class AbandonoTransicionInvalidoError(FrozenRuntimeError):
    """El abandono solicitado es inválido o viola una precondición durable (fail-closed)."""


class AbandonoCorruptoError(FrozenRuntimeError):
    """El registro de abandono persistido es ilegible, incompleto o su digest no coincide (fail-closed)."""


#: Versión canónica del esquema de registros de abandono durable (§Codex discussion_r4543251726)
SCHEMA_VERSION_ABANDONO = 1


@dataclasses.dataclass(frozen=True, slots=True)
class RegistroAbandonoTransicion:
    """Registro inmutable y verificable del abandono explícito de una transición consumida.

    Liga durablemente el storage root, la transición abandonada, la aprobación consumida,
    la identidad del propietario que la autorizó, el motivo y el timestamp, sellado por un digest canónico.
    """

    schema_version: int
    root_id: str
    transition_id: str
    approval_id: str
    approval_seq: int
    scope_digest: str
    propietario_sujeto: str
    propietario_superficie: str
    propietario_sesion: str
    propietario_mecanismo: str
    motivo: str
    abandonado_en_ns: int
    record_digest: str


#: Campos del registro de abandono v1. Esquema CERRADO (igual que el binding v1): un campo de más o de
#: menos es corrupción, no extensión, y el digest cubre exactamente este conjunto.
_CAMPOS_REGISTRO_ABANDONO: frozenset[str] = frozenset(
    {
        "schema_version",
        "root_id",
        "transition_id",
        "approval_id",
        "approval_seq",
        "scope_digest",
        "propietario_sujeto",
        "propietario_superficie",
        "propietario_sesion",
        "propietario_mecanismo",
        "motivo",
        "abandonado_en_ns",
        "record_digest",
    }
)


def _calcular_digest_abandono(payload: Mapping[str, object]) -> str:
    """Calcula el digest canónico del registro de abandono sobre todos sus campos salvo 'record_digest'."""
    data = {k: v for k, v in payload.items() if k != "record_digest"}
    return digest_canonico(data)


def _exigir_transition_id_canonico(valor: object) -> str:
    """``transition_id`` del subsistema: UUID v4 canónico en minúsculas (mismo criterio que el journal).

    Se valida la FORMA, no se normaliza: el id se interpola en un nombre de archivo del store de abandonos
    y un valor con separadores o ``..`` escaparía de él. Es validación, no recorte.
    """
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor.strip():
        raise AbandonoTransicionInvalidoError("transition_id debe ser una cadena no vacía")
    try:
        parseado = uuid.UUID(valor)
    except ValueError as exc:
        raise AbandonoTransicionInvalidoError(f"transition_id no es un UUID válido: {valor!r}") from exc
    if parseado.version != 4 or str(parseado) != valor:
        raise AbandonoTransicionInvalidoError(f"transition_id debe ser un UUID v4 canónico en minúsculas: {valor!r}")
    return valor


def _texto_del_registro(contenido: Mapping[str, object], campo: str, ruta: pathlib.Path) -> str:
    valor = contenido[campo]
    if not isinstance(valor, str) or not valor.strip():
        raise AbandonoCorruptoError(f"el registro de abandono en {ruta}: '{campo}' debe ser un string no vacío")
    return valor


def _entero_del_registro(contenido: Mapping[str, object], campo: str, ruta: pathlib.Path) -> int:
    valor = contenido[campo]
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise AbandonoCorruptoError(f"el registro de abandono en {ruta}: '{campo}' debe ser un entero positivo")
    return valor


def _exigir_store_admitido(dir_abandonos: pathlib.Path) -> bool:
    """``True`` si el store existe y es un directorio real, ``False`` si está ausente.

    Un enlace/reparse, un archivo en su lugar o un directorio no inspeccionable levantan: no se lee a través
    de un namespace redirigido. Mismas reglas de admisión que los lectores de ``approval.py`` y ``journal.py``.
    """
    admision = storage.admitir_directorio_storage(dir_abandonos)
    if not admision.success:
        raise AbandonoCorruptoError(f"el store de abandonos fue rechazado: {admision.message}")
    return dir_abandonos.is_dir()


def _exigir_registro_sin_enlace(ruta: pathlib.Path) -> None:
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError as exc:
        raise AbandonoCorruptoError(f"no se pudo inspeccionar el registro de abandono {ruta}: {exc}") from exc
    if tipo is not None:
        raise AbandonoCorruptoError(
            f"el registro de abandono {ruta} es un enlace/reparse ({tipo}): no se lee a través de él (fail-closed)"
        )


def cargar_registro_abandono(
    ruta: pathlib.Path,
    *,
    expected_root_id: str | None = None,
) -> RegistroAbandonoTransicion:
    """Lee y valida un registro durable de abandono. Falla cerrado ante cualquier anomalía.

    La validación es de TIPOS estrictos (sin coerción con ``str()``/``int()``) y de esquema cerrado: todo
    registro mal formado sale como :class:`AbandonoCorruptoError`, nunca como ``ValueError``/``TypeError``
    que un llamador que sólo atrapa errores del dominio dejaría escapar.
    """
    p = pathlib.Path(ruta)
    _exigir_registro_sin_enlace(p)
    if not p.is_file():
        raise AbandonoTransicionInvalidoError(f"el archivo de registro de abandono no existe: {p}")
    try:
        contenido = json.loads(p.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:  # ValueError cubre JSONDecodeError y UnicodeDecodeError
        raise AbandonoCorruptoError(f"el registro de abandono en {p} no es JSON válido: {exc}") from exc

    if not isinstance(contenido, dict):
        raise AbandonoCorruptoError(f"el registro de abandono en {p} debe ser un objeto JSON")

    version = contenido.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION_ABANDONO:
        raise AbandonoCorruptoError(
            f"versión de schema no soportada en {p}: {version!r} (se esperaba {SCHEMA_VERSION_ABANDONO})"
        )

    campos = frozenset(contenido)
    if campos != _CAMPOS_REGISTRO_ABANDONO:
        raise AbandonoCorruptoError(
            f"el registro de abandono en {p} no respeta el esquema cerrado "
            f"(faltan: {sorted(_CAMPOS_REGISTRO_ABANDONO - campos)}; sobran: {sorted(campos - _CAMPOS_REGISTRO_ABANDONO)})"
        )

    root_id = _texto_del_registro(contenido, "root_id", p)
    transition_id = _texto_del_registro(contenido, "transition_id", p)
    try:
        _exigir_transition_id_canonico(transition_id)
    except AbandonoTransicionInvalidoError as exc:
        raise AbandonoCorruptoError(f"el registro de abandono en {p}: {exc}") from exc
    approval_id = _texto_del_registro(contenido, "approval_id", p)
    approval_seq = _entero_del_registro(contenido, "approval_seq", p)
    scope_digest = _texto_del_registro(contenido, "scope_digest", p)
    sujeto = _texto_del_registro(contenido, "propietario_sujeto", p)
    superficie = _texto_del_registro(contenido, "propietario_superficie", p)
    sesion = _texto_del_registro(contenido, "propietario_sesion", p)
    mecanismo = _texto_del_registro(contenido, "propietario_mecanismo", p)
    motivo = _texto_del_registro(contenido, "motivo", p)
    abandonado_en_ns = _entero_del_registro(contenido, "abandonado_en_ns", p)
    digest_declarado = _texto_del_registro(contenido, "record_digest", p)

    if expected_root_id is not None and root_id != expected_root_id:
        raise AbandonoTransicionInvalidoError(
            f"el root_id del registro ({root_id}) no coincide con el esperado ({expected_root_id})"
        )

    digest_calculado = _calcular_digest_abandono(contenido)
    if digest_declarado != digest_calculado:
        raise AbandonoCorruptoError(
            f"el digest del registro de abandono en {p} está corrupto (declarado={digest_declarado}, calculado={digest_calculado})"
        )

    return RegistroAbandonoTransicion(
        schema_version=SCHEMA_VERSION_ABANDONO,
        root_id=root_id,
        transition_id=transition_id,
        approval_id=approval_id,
        approval_seq=approval_seq,
        scope_digest=scope_digest,
        propietario_sujeto=sujeto,
        propietario_superficie=superficie,
        propietario_sesion=sesion,
        propietario_mecanismo=mecanismo,
        motivo=motivo,
        abandonado_en_ns=abandonado_en_ns,
        record_digest=digest_declarado,
    )


def _cargar_abandonos(storage_root: pathlib.Path) -> dict[str, RegistroAbandonoTransicion]:
    """Carga todos los registros de abandono del storage. Falla cerrado si alguno está corrupto.

    El nombre del archivo ES el ``transition_id`` (``abandoned_transition_record_path``): un registro cuyo
    contenido declara otro id está copiado/renombrado y no se confía en él. Sin esta igualdad, un archivo
    podría resolver una transición distinta de la que su nombre anuncia.
    """
    dir_abandonos = storage.abandoned_transitions_state_dir(storage_root)
    if not _exigir_store_admitido(dir_abandonos):
        return {}
    esperado_root = canonical_root_key(storage_root)[0]
    registros: dict[str, RegistroAbandonoTransicion] = {}
    # ``iterdir`` y no ``glob``: en Python 3.11 ``Path.glob`` SUPRIME ``PermissionError`` durante la
    # enumeración, y un store ilegible se leería como vacío (la transición abandonada volvería a ser
    # reanudable). ``iterdir`` propaga el error al ``except OSError`` del diagnóstico (CodeRabbit sobre #909).
    for ruta in sorted(dir_abandonos.iterdir()):
        if ruta.suffix != ".json":
            continue
        reg = cargar_registro_abandono(ruta, expected_root_id=esperado_root)
        if ruta.stem != reg.transition_id:
            raise AbandonoCorruptoError(
                f"el registro de abandono {ruta.name} declara la transición {reg.transition_id!r}: el nombre del "
                "archivo y su contenido no coinciden (fail-closed)"
            )
        registros[reg.transition_id] = reg
    return registros


def registro_de_abandono(storage_root: pathlib.Path, transition_id: str) -> RegistroAbandonoTransicion | None:
    """Registro de abandono VÁLIDO de ``transition_id``, o ``None`` si esa transición no fue abandonada.

    Pensado para llamarse BAJO ``participant_lock`` (el binder y el propio abandono lo hacen). Un
    ``transition_id`` que no es UUID v4 canónico no puede haber sido abandonado (el abandono lo exige), así
    que devuelve ``None`` sin tocar el disco. Un registro presente pero ilegible, de otro root o cuyo nombre
    no coincide con su contenido NO se interpreta como "no abandonada": levanta, y el llamador falla cerrado.
    """
    try:
        t_id = _exigir_transition_id_canonico(transition_id)
    except AbandonoTransicionInvalidoError:
        return None
    if not _exigir_store_admitido(storage.abandoned_transitions_state_dir(storage_root)):
        return None
    ruta = storage.abandoned_transition_record_path(storage_root, t_id)
    _exigir_registro_sin_enlace(ruta)
    if not ruta.is_file():
        return None
    registro = cargar_registro_abandono(ruta, expected_root_id=canonical_root_key(storage_root)[0])
    if registro.transition_id != t_id:
        raise AbandonoCorruptoError(
            f"el registro {ruta.name} declara la transición {registro.transition_id!r}: el nombre del archivo y su "
            "contenido no coinciden (fail-closed)"
        )
    return registro


__all__ = [
    "SCHEMA_VERSION_ABANDONO",
    "AbandonoCorruptoError",
    "AbandonoTransicionInvalidoError",
    "RegistroAbandonoTransicion",
    "cargar_registro_abandono",
    "registro_de_abandono",
]
