"""Tests para Trusted Golden Registry (TGR) - GP2-S3a-1.

Verifica el contrato normativo de ADR 0010 §11.0, §11.3 y §11.4:
- TGR-01: Serialización determinista de registro vacío.
- TGR-02: Round-trip exacto (serializar -> deserializar -> igualdad).
- TGR-03: JSON inválido/corrupto -> fail closed.
- TGR-04: Schema inválido (no dict, claves desconocidas) -> fail closed.
- TGR-05: Campo obligatorio ausente -> fail closed.
- TGR-06: Rangos: VolumeSerialNumber uint64, root_file_id uint128 (admite 0).
- TGR-07: Conflicto de identidad física duplicada -> fail closed.
- TGR-08: Conflicto de canonical_root duplicado -> fail closed.
- TGR-09: Lookup exige coincidencia de los 4 bindings normativos.
- TGR-10: tree_digest mismatch -> no match / TrustedGoldenMismatchError.
- TGR-11: schema_version desconocido -> fail closed.
- TGR-12: write_trusted_registry_atomically preserva bytes canónicos.
- TGR-13: Archivo temporal nunca se crea fuera del directorio protegido.
- TGR-14: Fallo antes de replace conserva el registro previo intacto.
- TGR-15: Fallo post-escritura no produce un registro parcialmente parseable.
- TGR-AST: Módulos de apply nunca importan funciones de escritura del TGR (GP2-T44 partial).
- M-T1, M-T2: Mutation anchors para lookup y staged request.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.models import RuntimeVaultError, TreeDigest
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    TrustedGoldenMismatchError,
    TrustedGoldenNotFoundError,
    TrustedGoldenRegistry,
    TrustedRegistryDuplicateError,
    TrustedRegistryError,
    TrustedRegistryParseError,
    TrustedRegistrySchemaError,
    TrustedRegistryUnsupportedError,
    _write_trusted_registry_atomically_at,
    deserialize_trusted_golden_registry,
    load_trusted_golden_registry,
    serialize_trusted_golden_registry,
    verify_trusted_golden_binding,
)


def _crear_tree_digest_valido(digest: str = "a" * 64, files: int = 10, bytes_cnt: int = 5000) -> TreeDigest:
    return TreeDigest(digest=digest, files=files, bytes=bytes_cnt)


def _crear_entry_valida(
    canonical_root: str = "C:/Games/Skyrim",
    volume_serial_number: int = 0x12345678,
    root_file_id: int = 0x9ABCDEF0123456789ABCDEF012345678,
    tree_digest: TreeDigest | None = None,
    policy_version: str = "gp2-v1",
    registered_by: str = "S-1-5-32-544",
    registered_at: str = "2026-09-21T12:00:00Z",
) -> TrustedGoldenEntry:
    if tree_digest is None:
        tree_digest = _crear_tree_digest_valido()
    return TrustedGoldenEntry(
        canonical_root=canonical_root,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        tree_digest=tree_digest,
        policy_version=policy_version,
        registered_by=registered_by,
        registered_at=registered_at,
    )


# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class TestJerarquiaExcepcionesTGR:
    """Verifica la jerarquía de excepciones tipadas del TGR."""

    def test_herencia_de_runtime_vault_error(self) -> None:
        assert issubclass(TrustedRegistryError, RuntimeVaultError)
        assert issubclass(TrustedRegistryParseError, TrustedRegistryError)
        assert issubclass(TrustedRegistrySchemaError, TrustedRegistryError)
        assert issubclass(TrustedRegistryDuplicateError, TrustedRegistryError)
        assert issubclass(TrustedGoldenNotFoundError, TrustedRegistryError)
        assert issubclass(TrustedGoldenMismatchError, TrustedRegistryError)


# ============================================================================
# TGR-01 a TGR-11: Serialización, Validación y Lookup Puros
# ============================================================================


class TestTrustedGoldenRegistryPure:
    """Tests puros y deterministas del TGR ejecutables en cualquier plataforma."""

    def test_tgr_01_empty_registry_serializa_deterministicamente(self) -> None:
        """TGR-01: Registro vacío serializa determinísticamente sin whitespace incidental."""
        registry = TrustedGoldenRegistry(entries=(), schema_version="1.0")
        raw_bytes = serialize_trusted_golden_registry(registry)

        # Esperado: exactamente {"entries":[],"schema_version":"1.0"} en UTF-8
        esperado = b'{"entries":[],"schema_version":"1.0"}'
        assert raw_bytes == esperado

        # Repetición garantiza idempotencia exacta byte a byte
        assert serialize_trusted_golden_registry(registry) == raw_bytes

    def test_tgr_02_round_trip_exacto(self) -> None:
        """TGR-02: Round-trip exacto (serializar -> deserializar -> igualdad)."""
        entry1 = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim",
            volume_serial_number=0x11112222,
            root_file_id=1001,
            tree_digest=_crear_tree_digest_valido("1" * 64, files=5, bytes_cnt=2000),
        )
        entry2 = _crear_entry_valida(
            canonical_root="D:/Skyrim_Backup",
            volume_serial_number=0x33334444,
            root_file_id=2002,
            tree_digest=_crear_tree_digest_valido("2" * 64, files=8, bytes_cnt=4000),
        )

        registry_original = TrustedGoldenRegistry(entries=(entry2, entry1), schema_version="1.0")
        raw_bytes = serialize_trusted_golden_registry(registry_original)

        registry_recuperado = deserialize_trusted_golden_registry(raw_bytes)

        # Entradas ordenadas determinísticamente por canonical_root
        assert len(registry_recuperado.entries) == 2
        assert registry_recuperado.schema_version == "1.0"
        # C:/Games/Skyrim debe preceder a D:/Skyrim_Backup
        assert registry_recuperado.entries[0].canonical_root == entry1.canonical_root
        assert registry_recuperado.entries[0].volume_serial_number == entry1.volume_serial_number
        assert registry_recuperado.entries[0].root_file_id == entry1.root_file_id
        assert registry_recuperado.entries[0].tree_digest == entry1.tree_digest
        assert registry_recuperado.entries[1].canonical_root == entry2.canonical_root

        # Round-trip de bytes idéntico
        assert serialize_trusted_golden_registry(registry_recuperado) == raw_bytes

    def test_tgr_03_json_invalido_fail_closed(self) -> None:
        """TGR-03: JSON inválido o corrupto falla cerrado."""
        with pytest.raises(TrustedRegistryParseError, match="JSON inválido"):
            deserialize_trusted_golden_registry(b"esto no es json")

        with pytest.raises(TrustedRegistryParseError, match="JSON inválido"):
            deserialize_trusted_golden_registry(b'{"entries": [')

    def test_tgr_04_schema_invalido_fail_closed(self) -> None:
        """TGR-04: Schema inválido (no dict, claves extra o desconocidas) falla cerrado."""
        # Raíz no es un dict
        with pytest.raises(TrustedRegistrySchemaError, match="diccionario"):
            deserialize_trusted_golden_registry(b"[]")

        with pytest.raises(TrustedRegistrySchemaError, match="diccionario"):
            deserialize_trusted_golden_registry(b'"cadena"')

        # Claves no reconocidas en raíz (esquema cerrado)
        payload_extra = json.dumps({"entries": [], "schema_version": "1.0", "injected_field": "hack"}).encode("utf-8")
        with pytest.raises(TrustedRegistrySchemaError, match="Clave desconocida en raíz"):
            deserialize_trusted_golden_registry(payload_extra)

        # Clave no reconocida dentro de una entrada
        entry_extra = {
            "VolumeSerialNumber": 123,
            "canonical_root": "C:/Games/Skyrim",
            "policy_version": "gp2-v1",
            "registered_at": "2026-09-21T12:00:00Z",
            "registered_by": "S-1-5-32-544",
            "root_file_id": 456,
            "tree_digest": {"bytes": 100, "digest": "a" * 64, "files": 2},
            "untrusted_override": True,
        }
        payload_entry_extra = json.dumps({"entries": [entry_extra], "schema_version": "1.0"}).encode("utf-8")
        with pytest.raises(TrustedRegistrySchemaError, match="Clave desconocida en entrada"):
            deserialize_trusted_golden_registry(payload_entry_extra)

    def test_tgr_05_campo_obligatorio_ausente_fail_closed(self) -> None:
        """TGR-05: Campo obligatorio ausente en raíz o entrada falla cerrado sin defaults inseguros."""
        # Falta entries
        with pytest.raises(TrustedRegistrySchemaError, match="entries"):
            deserialize_trusted_golden_registry(b'{"schema_version":"1.0"}')

        # Falta schema_version
        with pytest.raises(TrustedRegistrySchemaError, match="schema_version"):
            deserialize_trusted_golden_registry(b'{"entries":[]}')

        # Falta VolumeSerialNumber en entrada
        entry_sin_vol = {
            "canonical_root": "C:/Games/Skyrim",
            "policy_version": "gp2-v1",
            "registered_at": "2026-09-21T12:00:00Z",
            "registered_by": "S-1-5-32-544",
            "root_file_id": 456,
            "tree_digest": {"bytes": 100, "digest": "a" * 64, "files": 2},
        }
        with pytest.raises(TrustedRegistrySchemaError, match="VolumeSerialNumber"):
            deserialize_trusted_golden_registry(json.dumps({"entries": [entry_sin_vol], "schema_version": "1.0"}))

        # Falta tree_digest
        entry_sin_digest = {
            "VolumeSerialNumber": 123,
            "canonical_root": "C:/Games/Skyrim",
            "policy_version": "gp2-v1",
            "registered_at": "2026-09-21T12:00:00Z",
            "registered_by": "S-1-5-32-544",
            "root_file_id": 456,
        }
        with pytest.raises(TrustedRegistrySchemaError, match="tree_digest"):
            deserialize_trusted_golden_registry(json.dumps({"entries": [entry_sin_digest], "schema_version": "1.0"}))

    def test_tgr_06_rangos_y_tipos_uint64_uint128(self) -> None:
        """TGR-06: VolumeSerialNumber uint64, root_file_id uint128 admitiendo 0."""
        # root_file_id = 0 es válido según ABI GP2
        entry_zero = _crear_entry_valida(root_file_id=0, volume_serial_number=0)
        reg_zero = TrustedGoldenRegistry(entries=(entry_zero,), schema_version="1.0")
        raw = serialize_trusted_golden_registry(reg_zero)
        rec = deserialize_trusted_golden_registry(raw)
        assert rec.entries[0].root_file_id == 0
        assert rec.entries[0].volume_serial_number == 0

        # root_file_id negativo
        with pytest.raises(TrustedRegistrySchemaError, match="root_file_id"):
            _crear_entry_valida(root_file_id=-1)

        # root_file_id fuera de rango 128-bit
        with pytest.raises(TrustedRegistrySchemaError, match="root_file_id"):
            _crear_entry_valida(root_file_id=1 << 128)

        # VolumeSerialNumber negativo
        with pytest.raises(TrustedRegistrySchemaError, match="VolumeSerialNumber"):
            _crear_entry_valida(volume_serial_number=-1)

        # VolumeSerialNumber fuera de rango 64-bit
        with pytest.raises(TrustedRegistrySchemaError, match="VolumeSerialNumber"):
            _crear_entry_valida(volume_serial_number=1 << 64)

        # Tipos inválidos (bool como int, str como int)
        entry_bool_vol = {
            "VolumeSerialNumber": True,
            "canonical_root": "C:/Games/Skyrim",
            "policy_version": "gp2-v1",
            "registered_at": "2026-09-21T12:00:00Z",
            "registered_by": "S-1-5-32-544",
            "root_file_id": 456,
            "tree_digest": {"bytes": 100, "digest": "a" * 64, "files": 2},
        }
        with pytest.raises(TrustedRegistrySchemaError, match="VolumeSerialNumber"):
            deserialize_trusted_golden_registry(json.dumps({"entries": [entry_bool_vol], "schema_version": "1.0"}))

    def test_tgr_07_conflicto_identidad_fisica_duplicada_fail_closed(self) -> None:
        """TGR-07: Conflicto de dos entradas para el mismo objeto físico (VolumeSerialNumber, root_file_id) falla cerrado."""
        e1 = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim_A",
            volume_serial_number=0x1234,
            root_file_id=9999,
        )
        e2 = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim_B",
            volume_serial_number=0x1234,
            root_file_id=9999,
        )

        with pytest.raises(TrustedRegistryDuplicateError, match="identidad física duplicada"):
            TrustedGoldenRegistry(entries=(e1, e2), schema_version="1.0")

    def test_tgr_08_conflicto_canonical_root_duplicado_fail_closed(self) -> None:
        """TGR-08: Conflicto de dos entradas con el mismo canonical_root falla cerrado."""
        e1 = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim",
            volume_serial_number=0x1111,
            root_file_id=1111,
        )
        e2 = _crear_entry_valida(
            canonical_root="c:/games/skyrim",  # case-insensitive match en Windows
            volume_serial_number=0x2222,
            root_file_id=2222,
        )

        with pytest.raises(TrustedRegistryDuplicateError, match="canonical_root duplicado"):
            TrustedGoldenRegistry(entries=(e1, e2), schema_version="1.0")

    def test_tgr_09_lookup_exige_coincidencia_de_los_cuatro_bindings(self) -> None:
        """TGR-09: verify_trusted_golden_binding exige coincidencia de root + volume + file_id + tree_digest."""
        td = _crear_tree_digest_valido("d" * 64, files=12, bytes_cnt=12345)
        entry = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim",
            volume_serial_number=0x5555,
            root_file_id=0x7777,
            tree_digest=td,
        )
        registry = TrustedGoldenRegistry(entries=(entry,), schema_version="1.0")

        # Coincidencia exacta de los 4 bindings -> éxito
        resultado = verify_trusted_golden_binding(
            registry=registry,
            canonical_root="C:/Games/Skyrim",
            volume_serial_number=0x5555,
            root_file_id=0x7777,
            tree_digest=td,
        )
        assert resultado == entry

        # Entrada no encontrada por root
        with pytest.raises(TrustedGoldenNotFoundError, match="No existe entrada"):
            verify_trusted_golden_binding(
                registry=registry,
                canonical_root="D:/Other/Path",
                volume_serial_number=0x5555,
                root_file_id=0x7777,
                tree_digest=td,
            )

        # Mismatch de VolumeSerialNumber
        with pytest.raises(TrustedGoldenMismatchError, match="VolumeSerialNumber"):
            verify_trusted_golden_binding(
                registry=registry,
                canonical_root="C:/Games/Skyrim",
                volume_serial_number=0x9999,  # difiere
                root_file_id=0x7777,
                tree_digest=td,
            )

        # Mismatch de root_file_id
        with pytest.raises(TrustedGoldenMismatchError, match="root_file_id"):
            verify_trusted_golden_binding(
                registry=registry,
                canonical_root="C:/Games/Skyrim",
                volume_serial_number=0x5555,
                root_file_id=0x8888,  # difiere
                tree_digest=td,
            )

    def test_tgr_10_tree_digest_mismatch_falla_en_cualquiera_de_las_tres_dimensiones(self) -> None:
        """TGR-10: tree_digest mismatch (digest, files o bytes) -> TrustedGoldenMismatchError."""
        td_base = _crear_tree_digest_valido("a" * 64, files=10, bytes_cnt=1000)
        entry = _crear_entry_valida(canonical_root="C:/Games/Skyrim", tree_digest=td_base)
        registry = TrustedGoldenRegistry(entries=(entry,), schema_version="1.0")

        # 1. Digest hash difiere
        td_hash_diff = _crear_tree_digest_valido("b" * 64, files=10, bytes_cnt=1000)
        with pytest.raises(TrustedGoldenMismatchError, match="tree_digest"):
            verify_trusted_golden_binding(
                registry, entry.canonical_root, entry.volume_serial_number, entry.root_file_id, td_hash_diff
            )

        # 2. Files difiere
        td_files_diff = _crear_tree_digest_valido("a" * 64, files=11, bytes_cnt=1000)
        with pytest.raises(TrustedGoldenMismatchError, match="tree_digest"):
            verify_trusted_golden_binding(
                registry, entry.canonical_root, entry.volume_serial_number, entry.root_file_id, td_files_diff
            )

        # 3. Bytes difiere
        td_bytes_diff = _crear_tree_digest_valido("a" * 64, files=10, bytes_cnt=1001)
        with pytest.raises(TrustedGoldenMismatchError, match="tree_digest"):
            verify_trusted_golden_binding(
                registry, entry.canonical_root, entry.volume_serial_number, entry.root_file_id, td_bytes_diff
            )

    def test_canonical_sha256_tree_digest_normalization(self) -> None:
        """P1: SHA-256 en mayúsculas se normaliza a minúsculas, produce mismos bytes y hace binding MATCH."""
        digest_upper = "A" * 64
        digest_lower = "a" * 64

        entry_upper = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim",
            tree_digest=_crear_tree_digest_valido(digest=digest_upper),
        )
        entry_lower = _crear_entry_valida(
            canonical_root="C:/Games/Skyrim",
            tree_digest=_crear_tree_digest_valido(digest=digest_lower),
        )

        # 1. Mismo TrustedGoldenEntry normalizado internamente a minúsculas
        assert entry_upper.tree_digest.digest == digest_lower
        assert entry_lower.tree_digest.digest == digest_lower
        assert entry_upper == entry_lower

        # 2. Misma serialización determinista
        reg_upper = TrustedGoldenRegistry(entries=(entry_upper,))
        reg_lower = TrustedGoldenRegistry(entries=(entry_lower,))
        bytes_upper = serialize_trusted_golden_registry(reg_upper)
        bytes_lower = serialize_trusted_golden_registry(reg_lower)
        assert bytes_upper == bytes_lower

        # 3. verify_trusted_golden_binding MATCH tanto con query mayúscula como minúscula
        matched_from_upper_query = verify_trusted_golden_binding(
            reg_lower,
            "C:/Games/Skyrim",
            entry_lower.volume_serial_number,
            entry_lower.root_file_id,
            TreeDigest(digest=digest_upper, files=10, bytes=5000),
        )
        assert matched_from_upper_query == entry_lower

        matched_from_lower_query = verify_trusted_golden_binding(
            reg_upper,
            "C:/Games/Skyrim",
            entry_upper.volume_serial_number,
            entry_upper.root_file_id,
            TreeDigest(digest=digest_lower, files=10, bytes=5000),
        )
        assert matched_from_lower_query == entry_upper

    def test_tgr_11_schema_version_desconocido_fail_closed(self) -> None:
        """TGR-11: schema_version != '1.0' es rechazado fail-closed."""
        for bad_ver in ["0.9", "2.0", "custom-v1", ""]:
            payload = json.dumps({"entries": [], "schema_version": bad_ver}).encode("utf-8")
            with pytest.raises(TrustedRegistrySchemaError, match="schema_version"):
                deserialize_trusted_golden_registry(payload)

    def test_registered_by_identidad_estable_sid_canonico(self) -> None:
        """Verifica que registered_by exija un SID canónico de Windows (^S-1-\\d+(?:-\\d+)+$) y rechace nombres no estables."""
        # SIDs canónicos válidos
        for valid_sid in [
            "S-1-5-18",  # LocalSystem
            "S-1-5-32-544",  # Administrators
            "S-1-5-21-123456789-987654321-11223344-1001",
            "S-1-1-0",  # Everyone
        ]:
            entry = _crear_entry_valida(registered_by=valid_sid)
            assert entry.registered_by == valid_sid

        # Nombres localizados, strings arbitrarios o no-SIDs deben fallar cerrado
        for invalid_name in [
            "",
            "   ",
            "Administrator",
            "Administrador",
            "SYSTEM",
            "NT AUTHORITY\\SYSTEM",
            "operator",
            "root",
            "S-2-5-18",  # No empieza con S-1-
            "S-1-",
            "S-1-abc",
            "S-1-5-32-XYZ",
            123,
            None,
        ]:
            with pytest.raises(TrustedRegistrySchemaError, match="registered_by"):
                _crear_entry_valida(registered_by=invalid_name)  # type: ignore[arg-type]

    def test_registered_at_utc_estricto(self) -> None:
        """Verifica que registered_at exija formato ISO 8601 UTC determinista con 'Z'."""
        # Naive sin zona
        with pytest.raises(TrustedRegistrySchemaError, match="registered_at"):
            _crear_entry_valida(registered_at="2026-09-21T12:00:00")

        # Offset distinto de Z
        with pytest.raises(TrustedRegistrySchemaError, match="registered_at"):
            _crear_entry_valida(registered_at="2026-09-21T12:00:00-03:00")


# ============================================================================
# TGR-12 a TGR-15: Reemplazo Atómico y Manejo de Archivos
# ============================================================================


class TestTrustedGoldenRegistryAtomicStorage:
    """Tests del reemplazo atómico de TGR sobre disco."""

    @pytest.fixture(autouse=True)
    def _setup_simulated_elevated_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simula elevación en Windows para que la creación desde su nacimiento y verificación por handle funcionen en runner no elevado."""
        if sys.platform == "win32":
            from sky_claw.local.runtime_vault.trusted_namespace import (
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL,
                _GENERIC_READ,
                _GENERIC_WRITE,
                _kernel32,
            )

            def _simulated_birth(path: pathlib.Path | str, object_name: str = "trusted_goldens.json") -> int:
                h = _kernel32.CreateFileW(
                    str(path),
                    _GENERIC_READ | _GENERIC_WRITE,
                    0,
                    None,
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL,
                    None,
                )
                return int(h)

            monkeypatch.setattr(
                "sky_claw.local.runtime_vault.trusted_namespace.create_secured_file_from_birth",
                _simulated_birth,
            )
            monkeypatch.setattr(
                "sky_claw.local.runtime_vault.trusted_namespace.verify_secured_file_by_handle",
                lambda p: None,
            )

    def test_tgr_unelevated_without_privilege_fails_closed(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verifica causalmente que sin privilegios de elevación para SYSTEM owner, create_secured_file_from_birth falle cerrado (1307)."""
        if sys.platform != "win32":
            pytest.skip("Solo Windows")
        monkeypatch.undo()
        from sky_claw.local.runtime_vault.trusted_namespace import (
            TrustedNamespaceError,
        )
        from sky_claw.local.runtime_vault.trusted_namespace import (
            create_secured_file_from_birth as real_create_secured_file_from_birth,
        )

        target_file = tmp_path / "real_birth_test.json"
        # Usar la función real sin monkeypatch
        with pytest.raises(TrustedNamespaceError) as exc_info:
            real_create_secured_file_from_birth(target_file)
        assert "1307" in str(exc_info.value) or "1314" in str(exc_info.value)

    @pytest.mark.skipif(sys.platform != "win32", reason="Primitiva de almacenamiento atómico Win32")
    def test_tgr_12_atomic_replace_preserva_bytes_canonicos(self, tmp_path: pathlib.Path) -> None:
        """TGR-12: write_trusted_registry_atomically escribe exactamente los bytes canónicos."""
        target_file = tmp_path / "trusted_goldens.json"
        entry = _crear_entry_valida(canonical_root="C:/Games/Skyrim")
        reg = TrustedGoldenRegistry(entries=(entry,), schema_version="1.0")

        _write_trusted_registry_atomically_at(reg, target_file)

        raw_leido = target_file.read_bytes()
        assert raw_leido == serialize_trusted_golden_registry(reg)

        # Cargar de vuelta con load_trusted_golden_registry
        reg_cargado = load_trusted_golden_registry(target_file)
        assert reg_cargado == reg

    @pytest.mark.skipif(sys.platform != "win32", reason="Primitiva de almacenamiento atómico Win32")
    def test_tgr_13_temp_nunca_se_crea_fuera_del_directorio_protegido(self, tmp_path: pathlib.Path) -> None:
        """TGR-13: El archivo temporal se crea exclusivamente dentro del mismo directorio que target."""
        target_file = tmp_path / "trusted_goldens.json"
        reg = TrustedGoldenRegistry(entries=(), schema_version="1.0")

        created_temps: list[pathlib.Path] = []
        original_replace = os.replace

        def track_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            created_temps.append(pathlib.Path(src))
            original_replace(src, dst)

        with patch("sky_claw.local.runtime_vault.trusted_registry.os.replace", side_effect=track_replace):
            _write_trusted_registry_atomically_at(reg, target_file)

        assert len(created_temps) == 1
        temp_creado = created_temps[0]
        assert temp_creado.parent.resolve() == target_file.parent.resolve()
        assert temp_creado.name.startswith(".tmp_")

    @pytest.mark.skipif(sys.platform != "win32", reason="Primitiva de almacenamiento atómico Win32")
    def test_tgr_14_fallo_antes_de_replace_conserva_registry_anterior(self, tmp_path: pathlib.Path) -> None:
        """TGR-14: Si ocurre un fallo antes de replace, el archivo previo permanece 100% intacto."""
        target_file = tmp_path / "trusted_goldens.json"
        entry_inicial = _crear_entry_valida(canonical_root="C:/Games/Skyrim_Inicial")
        reg_inicial = TrustedGoldenRegistry(entries=(entry_inicial,), schema_version="1.0")
        _write_trusted_registry_atomically_at(reg_inicial, target_file)
        bytes_iniciales = target_file.read_bytes()

        entry_nueva = _crear_entry_valida(canonical_root="C:/Games/Skyrim_Nueva")
        reg_nuevo = TrustedGoldenRegistry(entries=(entry_nueva,), schema_version="1.0")

        # Simular fallo durante fsync / antes de replace
        with (
            patch("os.replace", side_effect=OSError("Disk write error")),
            pytest.raises(OSError, match="Disk write error"),
        ):
            _write_trusted_registry_atomically_at(reg_nuevo, target_file)

        # El archivo original no fue tocado
        assert target_file.read_bytes() == bytes_iniciales
        # Y no quedaron temporales colgando
        temporales = list(tmp_path.glob(".tmp_*"))
        assert len(temporales) == 0

    @pytest.mark.skipif(sys.platform != "win32", reason="Primitiva de almacenamiento atómico Win32")
    def test_tgr_15_fallo_post_write_revalida_digest_y_no_produce_estado_parcial(self, tmp_path: pathlib.Path) -> None:
        """TGR-15: Si el archivo reemplazado queda con digest inconsistente, falla cerrado."""
        target_file = tmp_path / "trusted_goldens.json"
        reg = TrustedGoldenRegistry(entries=(), schema_version="1.0")

        # Simular que al reabrir el archivo para revalidar el digest, el contenido se corrompió
        with (
            patch.object(pathlib.Path, "read_bytes", return_value=b"corrupted bytes post replace"),
            pytest.raises(TrustedRegistryError, match="Revalidación post-reemplazo falló"),
        ):
            _write_trusted_registry_atomically_at(reg, target_file)

    def test_write_trusted_registry_atomically_posix_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """P1: En plataformas no Windows, write_trusted_registry_atomically falla cerrado sin tocar el disco."""
        target_file = tmp_path / "trusted_goldens.json"
        reg = TrustedGoldenRegistry(entries=(), schema_version="1.0")
        with (
            patch("sys.platform", "linux"),
            pytest.raises(TrustedRegistryUnsupportedError, match="solo está soportado en Windows"),
        ):
            _write_trusted_registry_atomically_at(reg, target_file)

        assert not target_file.exists()
        assert len(list(tmp_path.iterdir())) == 0


# ============================================================================
# TGR-AST: Aislamiento y Separación de Autoridad (GP2-T44 Partial)
# ============================================================================


class TestTgrAstIsolation:
    """Verificaciones AST y de flujo para garantizar separación estricta de autoridad (ADR 0010 §11.4)."""

    def test_gp2_t44_apply_modules_never_write_tgr(self) -> None:
        """GP2-T44 partial: planning_orchestrator, golden_protection_plan y target_dacl nunca importan ni escriben TGR."""
        runtime_vault_dir = pathlib.Path(__file__).parents[1] / "sky_claw" / "local" / "runtime_vault"
        apply_modules = [
            runtime_vault_dir / "planning_orchestrator.py",
            runtime_vault_dir / "golden_protection_plan.py",
            runtime_vault_dir / "target_dacl.py",
        ]

        forbidden_write_symbols = {
            "write_trusted_registry_atomically",
            "_write_trusted_registry_atomically_at",
            "write_trusted_goldens",
            "register_trusted_golden",
            "refresh_trusted_golden",
        }

        for mod_path in apply_modules:
            assert mod_path.exists(), f"Módulo no encontrado: {mod_path}"
            tree = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        assert alias.name not in forbidden_write_symbols, (
                            f"VIOLACIÓN GP2-T44: {mod_path.name} importa '{alias.name}', que es una primitiva de escritura del TGR. "
                            "GP2 apply NUNCA escribe TGR."
                        )
                elif isinstance(node, ast.Name) and node.id in forbidden_write_symbols:
                    raise AssertionError(f"VIOLACIÓN GP2-T44: Referencia a {node.id} en {mod_path.name}")

    def test_m_t2_staged_digest_se_rechaza_sin_verificacion_rv2(self) -> None:
        """M-T2: No existe ninguna primitiva pública que reciba un objeto staged arbitrario y lo escriba directamente en el TGR."""
        import sky_claw.local.runtime_vault.trusted_registry as tgr_mod

        public_functions = [f for f in dir(tgr_mod) if not f.startswith("_") and callable(getattr(tgr_mod, f))]

        # No debe existir ninguna función tipo register_from_staging o adopt_staged_request
        for name in public_functions:
            assert "staged" not in name.lower(), (
                f"Función sospechosa '{name}' en trusted_registry: los datos staged no se ingieren directamente."
            )
            assert "untrusted" not in name.lower()

    def test_trusted_registry_has_no_portable_test_writer(self) -> None:
        """P1: trusted_registry.py no contiene ningún writer portable ni helpers de test."""
        import sky_claw.local.runtime_vault.trusted_registry as tgr_mod

        src_path = pathlib.Path(tgr_mod.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert "portable" not in node.name.lower(), (
                    f"trusted_registry.py define una función portable de test en código productivo: {node.name}"
                )
                assert "fixture" not in node.name.lower()
