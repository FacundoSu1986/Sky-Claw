"""Tests del binding durable ``operation_lock_binding.json`` (GP2-S4C, P1#1).

El binding es la ÚNICA evidencia durable que existe en la ventana entre
adquirir el GoldenMutationLock (§12.2 paso 6) y publicar el
``authorized_plan.json`` (paso 7). Estos tests fijan sus garantías:

- esquema CERRADO y versión exacta;
- ``lock_key`` DERIVADO de (vol, file_id): no puede reapuntar la identidad;
- ``binding_digest`` autodescriptivo: la sustitución wholesale falla cerrada;
- bytes canónicos exactos (un reformateo se rechaza, no se "normaliza");
- clasificación por evidencia: ABSENT / DURABLE / INDETERMINATE (nunca lanza);
- publicación create-once: un segundo intento NO sustituye;
- acuñamiento impossible sin la prueba privada (no hay autoridad fabricable).

Y que NO es autoridad de mutación: sin plan no hay PRE, luego no hay rollback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import uuid
from typing import Any

import pytest

from sky_claw.local.runtime_vault.golden_mutation_lock import derive_golden_lock_key
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.operation_lock_binding import (
    OPERATION_LOCK_BINDING_FILE_NAME,
    OPERATION_LOCK_BINDING_SCHEMA_VERSION,
    DurableOperationLockBinding,
    OperationLockBinding,
    OperationLockBindingAlreadyExistsError,
    OperationLockBindingError,
    OperationLockBindingEvidence,
    OperationLockBindingSchemaError,
    build_operation_lock_binding,
    classify_operation_lock_binding,
    compute_operation_lock_binding_digest,
    derive_operation_lock_binding_path,
    deserialize_operation_lock_binding,
    load_operation_lock_binding,
    promote_operation_lock_binding,
    serialize_operation_lock_binding,
)

_OPERACION = "7c9b1d34-8f2a-4c6e-b1d5-3a9e2f4c7b01"
_OTRA_OPERACION = "2e5a8c17-4b3d-4f92-9e60-8d1a3f5b6c22"
_VOL = 0xA1B2C3D4
_FID = 0x1122334455667788
_CREADO = "2026-09-30T12:00:00.000Z"


def _resolver(raiz: pathlib.Path) -> Any:
    return lambda: raiz


def _raiz(tmp_path: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    (tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / operation_id).mkdir(parents=True)
    return tmp_path


def _binding(**kwargs: Any) -> OperationLockBinding:
    datos = {
        "operation_id": _OPERACION,
        "volume_serial_number": _VOL,
        "root_file_id": _FID,
        "created_at": _CREADO,
    }
    datos.update(kwargs)
    return build_operation_lock_binding(**datos)  # type: ignore[arg-type]


class _WriterFalso:
    """Writer create-once en memoria: escribe el archivo y cuenta publicaciones."""

    def __init__(self, *, fallar: bool = False) -> None:
        self.fallos = fallar
        self.publicaciones: list[bytes] = []
        self.objetos: list[str] = []

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        self.objetos.append(object_name)
        if self.fallos:
            raise OSError("fallo de escritura simulado")
        if dest.exists():
            raise OperationLockBindingAlreadyExistsError(f"ya existe: '{dest}'")
        self.publicaciones.append(payload)
        dest.write_bytes(payload)


class TestModeloOperationLockBinding:
    def test_esquema_cerrado_por_igualdad_literal(self) -> None:
        crudo = serialize_operation_lock_binding(_binding())
        payload = json.loads(crudo)
        assert set(payload) == {
            "schema_version",
            "operation_id",
            "volume_serial_number",
            "root_file_id",
            "lock_key",
            "created_at",
            "binding_digest",
        }
        assert payload["schema_version"] == OPERATION_LOCK_BINDING_SCHEMA_VERSION
        assert payload["lock_key"] == derive_golden_lock_key(_VOL, _FID)

    def test_esquema_no_cerrado_es_rechazado(self) -> None:
        payload = json.loads(serialize_operation_lock_binding(_binding()))
        payload["campo_extra"] = "infiltrado"
        with pytest.raises(OperationLockBindingSchemaError, match="no cerrado"):
            deserialize_operation_lock_binding(json.dumps(payload).encode("utf-8"))
        del payload["binding_digest"]
        with pytest.raises(OperationLockBindingSchemaError, match="no cerrado"):
            deserialize_operation_lock_binding(json.dumps(payload).encode("utf-8"))

    def test_lock_key_se_rederiva_y_no_se_cree(self) -> None:
        """Un lock_key que no corresponde a la identidad declarada es rechazado."""
        payload = json.loads(serialize_operation_lock_binding(_binding()))
        payload["lock_key"] = derive_golden_lock_key(_VOL, _FID + 1)
        payload["binding_digest"] = hashlib.sha256(
            json.dumps(
                {k: v for k, v in payload.items() if k != "binding_digest"},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with pytest.raises(OperationLockBindingSchemaError, match="lock_key declarado no corresponde"):
            deserialize_operation_lock_binding(json.dumps(payload).encode("utf-8"))

    def test_sustitucion_wholesale_falla_por_digest(self) -> None:
        """Reapuntar la identidad SIN recalcular el digest falla cerrada.

        La sustitución deja un documento internamente coherente (lock_key
        derivada de la nueva identidad) pero con el digest del binding original:
        es exactamente el ataque de sustitución wholesale, y el digest lo corta.
        """
        payload = json.loads(serialize_operation_lock_binding(_binding()))
        payload["root_file_id"] = _FID + 99
        payload["lock_key"] = derive_golden_lock_key(_VOL, _FID + 99)
        # binding_digest se deja INTACTO a propósito.
        with pytest.raises(OperationLockBindingSchemaError, match="binding_digest no coincide"):
            deserialize_operation_lock_binding(json.dumps(payload).encode("utf-8"))

    def test_claves_duplicadas_son_rechazadas(self) -> None:
        crudo = serialize_operation_lock_binding(_binding())
        suplantado = crudo[:-1] + b',"operation_id":"x"}'
        with pytest.raises(OperationLockBindingSchemaError):
            deserialize_operation_lock_binding(suplantado)

    def test_bytes_no_canonicos_son_rechazados(self) -> None:
        payload = json.loads(serialize_operation_lock_binding(_binding()))
        espaciado = json.dumps(payload, indent=2).encode("utf-8")  # mismo contenido, no canónico
        with pytest.raises(OperationLockBindingSchemaError, match="serialización canónica"):
            deserialize_operation_lock_binding(espaciado)

    def test_version_increible_es_rechazada(self) -> None:
        with pytest.raises(OperationLockBindingSchemaError, match="schema_version"):
            OperationLockBinding(
                schema_version=99,
                operation_id=_OPERACION,
                volume_serial_number=_VOL,
                root_file_id=_FID,
                lock_key=derive_golden_lock_key(_VOL, _FID),
                created_at=_CREADO,
            )

    def test_operation_id_no_canonico_es_rechazado(self) -> None:
        with pytest.raises(OperationLockBindingSchemaError, match="UUID"):
            _binding(operation_id="no-canonico")

    def test_created_at_no_iso_z_es_rechazado(self) -> None:
        with pytest.raises(OperationLockBindingSchemaError, match="created_at"):
            _binding(created_at="30/09/2026")

    def test_digest_es_funcion_del_contenido(self) -> None:
        b1 = _binding()
        b2 = _binding(root_file_id=_FID + 1)
        assert compute_operation_lock_binding_digest(b1) != compute_operation_lock_binding_digest(b2)
        assert (
            compute_operation_lock_binding_digest(b1)
            == json.loads(serialize_operation_lock_binding(b1))["binding_digest"]
        )


class TestClasificacionYStore:
    def test_ausente_durable_e_indeterminate(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        assert classify_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz)) is (
            OperationLockBindingEvidence.ABSENT
        )

        destino.write_bytes(serialize_operation_lock_binding(_binding()))
        assert classify_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz)) is (
            OperationLockBindingEvidence.DURABLE
        )

        destino.write_bytes(b"\x00\xff basura")
        assert classify_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz)) is (
            OperationLockBindingEvidence.INDETERMINATE
        )

    def test_clasificar_nunca_lanza_por_datos_de_disco(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        destino.mkdir(parents=True)  # un directorio donde debería estar el archivo
        assert classify_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz)) is (
            OperationLockBindingEvidence.INDETERMINATE
        )

    def test_promocion_create_once_revalida_y_acuna(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        writer = _WriterFalso()
        durable = promote_operation_lock_binding(
            operation_id=_OPERACION,
            volume_serial_number=_VOL,
            root_file_id=_FID,
            created_at=_CREADO,
            programdata_resolver=_resolver(raiz),
            binding_writer=writer,
        )
        assert isinstance(durable, DurableOperationLockBinding)
        assert durable.operation_id == _OPERACION
        assert durable.physical_identity == (_VOL, _FID)
        assert writer.objetos == [OPERATION_LOCK_BINDING_FILE_NAME]
        # Los bytes publicados son los canónicos, byte a byte.
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        assert destino.read_bytes() == writer.publicaciones[0]
        assert destino.read_bytes() == serialize_operation_lock_binding(_binding())

    def test_promocion_es_idempotente_por_replay_y_nunca_sustituye(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        destino.write_bytes(serialize_operation_lock_binding(_binding()))
        writer = _WriterFalso()

        with pytest.raises(OperationLockBindingAlreadyExistsError):
            promote_operation_lock_binding(
                operation_id=_OPERACION,
                volume_serial_number=_VOL,
                root_file_id=_FID,
                created_at=_CREADO,
                programdata_resolver=_resolver(raiz),
                binding_writer=writer,
            )
        assert writer.publicaciones == []
        assert destino.read_bytes() == serialize_operation_lock_binding(_binding()), "create-once: nunca sustituye"

    def test_fallo_de_escritura_no_publica_ni_acuna(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        with pytest.raises(OperationLockBindingError):
            promote_operation_lock_binding(
                operation_id=_OPERACION,
                volume_serial_number=_VOL,
                root_file_id=_FID,
                created_at=_CREADO,
                programdata_resolver=_resolver(raiz),
                binding_writer=_WriterFalso(fallar=True),
            )
        assert not destino.exists()

    def test_acunamiento_exige_prueba_privada(self) -> None:
        with pytest.raises(OperationLockBindingError, match="acuña"):
            DurableOperationLockBinding(_binding(), pathlib.Path("x"), "d" * 64)

    def test_carga_detecta_binding_de_otra_operacion(self, tmp_path: pathlib.Path) -> None:
        """Un binding válido de OTRA operación en esta ruta es evidencia contradictoria."""
        raiz = _raiz(tmp_path)
        destino = derive_operation_lock_binding_path(_OPERACION, programdata_resolver=_resolver(raiz))
        destino.write_bytes(
            serialize_operation_lock_binding(
                build_operation_lock_binding(
                    operation_id=_OTRA_OPERACION,
                    volume_serial_number=_VOL,
                    root_file_id=_FID,
                    created_at=_CREADO,
                )
            )
        )
        # Clasificación: NUNCA "ausente" ni "durable" para evidencia ajena.
        assert classify_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz)) is (
            OperationLockBindingEvidence.INDETERMINATE
        )
        # Y la carga explícita también lo rehusa por identidad.
        with pytest.raises(OperationLockBindingSchemaError, match="otra operation_id"):
            load_operation_lock_binding(_OPERACION, programdata_resolver=_resolver(raiz))

    def test_jerarquia_de_excepciones(self) -> None:
        assert issubclass(OperationLockBindingError, RuntimeVaultError)
        assert issubclass(OperationLockBindingSchemaError, OperationLockBindingError)
        assert issubclass(OperationLockBindingAlreadyExistsError, OperationLockBindingError)

    def test_created_por_defecto_queda_sellado(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = promote_operation_lock_binding(
            operation_id=_OPERACION,
            volume_serial_number=_VOL,
            root_file_id=_FID,
            programdata_resolver=_resolver(raiz),
            binding_writer=_WriterFalso(),
        )
        assert durable.binding.created_at.endswith("Z")
        uuid.UUID(durable.operation_id)  # sigue siendo un UUID válido y canónico


class TestElBindingNoEsAutoridadDeMutacion:
    """El binding liga identidad; NO autoriza mutar ni restaurar nada."""

    def test_no_expone_tabla_de_nodos_ni_pre_sd(self) -> None:
        import dataclasses

        nombres = {campo.name for campo in dataclasses.fields(OperationLockBinding)}
        for prohibido in ("nodes", "node_count", "pre_sd_bytes_b64", "canonical_root", "tree_digest", "staging_digest"):
            assert prohibido not in nombres, sorted(nombres)

    def test_no_referencia_staging_ni_tgr_ni_setsecurityinfo(self) -> None:
        import ast

        raiz_modulo = pathlib.Path("sky_claw/local/runtime_vault/operation_lock_binding.py")
        arbol = ast.parse(raiz_modulo.read_text(encoding="utf-8"))
        simbolos: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Name):
                simbolos.add(nodo.id)
            elif isinstance(nodo, ast.Attribute):
                simbolos.add(nodo.attr)
            elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                simbolos.add(nodo.name)
        for prohibido in (
            "read_candidate_manifest_bytes",
            "derive_candidate_manifest_path",
            "candidate_manifest",
            "trusted_goldens",
            "refresh_trusted_registry",
            "SetSecurityInfo",
            "apply_target_dacl",
            "restore_pre_sd",
            "golden",
        ):
            assert prohibido not in simbolos, f"el binding no puede referenciar '{prohibido}'"

    def test_base64_no_aparece_como_carga_util(self) -> None:
        """El binding no transporta SDs; si aparece base64 sería un blob disfrazado."""
        texto = pathlib.Path("sky_claw/local/runtime_vault/operation_lock_binding.py").read_text(encoding="utf-8")
        assert "base64" not in texto
        # Y el documento real serializado no contiene estructura de descriptor.
        crudo = serialize_operation_lock_binding(_binding())
        assert base64.b64encode  # sanity del import del test
        assert b"pre_sd" not in crudo
