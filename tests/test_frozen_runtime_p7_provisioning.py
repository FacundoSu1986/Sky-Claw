"""Tests sintéticos del provisionamiento P7-A1 (Stock verificado → Generation → Clone).

Cubren el camino NUEVO que este cambio agrega, siempre en ``tmp_path``: no se
toca ninguna instalación real. La propiedad central es que la copia Stock NO se
auto-autoriza: se verifica contra una referencia INDEPENDIENTE (TreeDigest +
RuntimeIdentity + críticos) aportada por el operador.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import threading
import types

import pytest

from sky_claw.local.frozen_runtime import (
    CandidateCopyError,
    GenerationVerificationState,
    ProvisioningError,
    RuntimeCloneLifecycle,
    StockReferenceInvalidError,
    StockSourceError,
    adoptar_copia_stock,
    capturar_membership_directorios,
    cargar_referencia_stock,
    construir_referencia_stock,
    crear_runtime_clone_desde_generation,
    initialize_frozen_runtime_storage,
    leer_runtime_clone_record,
    publicar_generation_desde_stock,
    verificar_generation,
    verificar_runtime_clone,
    verificar_runtime_setup,
    verificar_sin_multilinks,
)
from sky_claw.local.frozen_runtime.storage import generation_dir
from tests._symlink_guard import crear_junction, junction_guard

VERSION = "1.6.1170.0"


# ── helpers ────────────────────────────────────────────────────────────────


def _escribir_arbol(raiz: pathlib.Path, *, con_skse: bool = True) -> pathlib.Path:
    (raiz / "Data" / "EmptyFolder").mkdir(parents=True)
    (raiz / "SkyrimSE.exe").write_bytes(b"exe-sintetico")
    (raiz / "steam_api64.dll").write_bytes(b"steam-api-sintetica")
    (raiz / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    if con_skse:
        (raiz / "skse64_loader.exe").write_bytes(b"loader-sintetico")
        (raiz / "skse64_1_6_1170.dll").write_bytes(b"skse-dll-sintetico")
    return raiz


def _hash_arbol(raiz: pathlib.Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for actual, _dirs, archivos in os.walk(raiz):
        for nombre in sorted(archivos):
            ruta = pathlib.Path(actual) / nombre
            hashes[str(ruta.relative_to(raiz)).replace("\\", "/")] = hashlib.sha256(ruta.read_bytes()).hexdigest()
    return hashes


def _sin_hardlinks(raiz: pathlib.Path) -> bool:
    for actual, _dirs, archivos in os.walk(raiz):
        for nombre in archivos:
            if os.lstat(pathlib.Path(actual) / nombre).st_nlink != 1:
                return False
    return True


@pytest.fixture
def parche_identidad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identidad PE sintética (los archivos de test no son PE reales)."""
    from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

    def identidad(raiz: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version=VERSION,
            observed_exe_path=str(pathlib.Path(raiz) / "SkyrimSE.exe"),
            observed_at_ns=1,
        )

    monkeypatch.setattr("sky_claw.local.frozen_runtime.provisioning.observe_runtime_identity_from_root", identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.clones.observe_runtime_identity_from_root", identidad)
    monkeypatch.setattr("sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root", identidad)


@pytest.fixture
def entorno(tmp_path: pathlib.Path, parche_identidad: None) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, object]:
    """(storage inicializado, copia Stock, clone_parent, referencia independiente)."""
    base = _escribir_arbol(tmp_path / "base-sellada")
    referencia = construir_referencia_stock(base)
    source = pathlib.Path(shutil.copytree(base, tmp_path / "stock-verificado"))
    storage = tmp_path / "storage"
    assert initialize_frozen_runtime_storage(storage).success
    clones = tmp_path / "clones"
    clones.mkdir()
    return storage, source, clones, referencia


def _publicar(entorno, **kwargs) -> tuple[object, object]:
    storage, source, _clones, referencia = entorno
    copia = adoptar_copia_stock(source, referencia)
    return publicar_generation_desde_stock(storage, copia, **kwargs), copia


# ── referencia independiente ───────────────────────────────────────────────


class TestReferenciaStock:
    def test_roundtrip_estable_y_digest_recomputado(self, entorno) -> None:
        _storage, _source, _clones, referencia = entorno
        recargada = cargar_referencia_stock(referencia.como_dict())
        assert recargada == referencia
        assert recargada.reference_digest == referencia.reference_digest
        assert recargada.expected_runtime.game_version == VERSION

    def test_rechaza_referencia_alterada(self, entorno) -> None:
        _storage, _source, _clones, referencia = entorno
        alterada = dataclasses.replace(
            referencia,
            expected_tree=dataclasses.replace(referencia.expected_tree, digest="a" * 64),
        )
        with pytest.raises(StockReferenceInvalidError, match="alterada"):
            cargar_referencia_stock(alterada.como_dict())

    def test_rechaza_campo_extra_en_el_schema(self, entorno) -> None:
        _storage, _source, _clones, referencia = entorno
        payload = referencia.como_dict()
        payload["campo_no_contratado"] = 1
        with pytest.raises(StockReferenceInvalidError, match="campos"):
            cargar_referencia_stock(payload)

    def test_referencia_sin_membership_es_incompatible(self, entorno) -> None:
        """Una referencia previa (sin evidencia de directorios) NO puede cargarse.

        Fail-closed: sin membership no se puede afirmar que el árbol sellado sea
        el mismo (el TreeDigest es ciego a directorios vacíos)."""
        _storage, _source, _clones, referencia = entorno
        payload = referencia.como_dict()
        payload.pop("directory_membership", None)
        with pytest.raises(StockReferenceInvalidError, match="campos|membership"):
            cargar_referencia_stock(payload)

    def test_rechaza_directorio_no_string_aunque_coaccione_igual(self, entorno) -> None:
        """`directories` debe ser estrictamente list[str], sin casts silenciosos.

        `str(123) == "123"`: si el loader coacciona, un entero que representa un
        directorio real pasa la validación de membership/digest y la referencia
        queda autorizada con un payload que no es el schema contratado."""
        _storage, source, _clones, _referencia = entorno
        (source / "123").mkdir()
        referencia = construir_referencia_stock(source)
        payload = referencia.como_dict()
        directorios = payload["directory_membership"]["directories"]
        assert isinstance(directorios, list) and "123" in directorios
        payload["directory_membership"]["directories"] = [
            123 if directorio == "123" else directorio for directorio in directorios
        ]
        with pytest.raises(StockReferenceInvalidError, match="strings|directorios"):
            cargar_referencia_stock(payload)

    def test_rechaza_runtime_no_string_aunque_el_digest_coincida(self, entorno) -> None:
        """`game_key`/`game_version` deben ser strings; si no, la adopción revienta
        con AttributeError fuera del contrato tipado del loader."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        _storage, _source, _clones, referencia = entorno
        payload = referencia.como_dict()
        payload["expected_runtime"] = {"game_key": 123, "game_version": 456}
        sin_digest = {clave: valor for clave, valor in payload.items() if clave != "reference_digest"}
        payload["reference_digest"] = provisioning_module._sha256_bytes(provisioning_module._canonical_json(sin_digest))
        with pytest.raises(StockReferenceInvalidError, match="runtime|strings"):
            cargar_referencia_stock(payload)

    def test_rechaza_referencia_bytes_no_utf8_con_error_tipado(self) -> None:
        """`json.loads(bytes)` puede fallar con UnicodeDecodeError, no sólo JSONDecodeError.

        El contrato de `cargar_referencia_stock` es StockReferenceInvalidError:
        un archivo binario corrupto no puede escapar como excepción sin tipo."""
        with pytest.raises(StockReferenceInvalidError, match="JSON|UTF|decode"):
            cargar_referencia_stock(b'\xff\xfe{"schema_version": 2}')

    @pytest.mark.parametrize(
        "directorios_invalidos",
        [
            lambda originales: [originales[0], originales[0]],
            lambda originales: ["../escape"],
        ],
        ids=["duplicado", "traversal"],
    )
    def test_rechaza_membership_invalida_con_error_tipado(self, entorno, directorios_invalidos) -> None:
        """Duplicados/traversal lanzan DirectoryMembershipError: debe mapearse al error tipado del loader."""
        _storage, _source, _clones, referencia = entorno
        payload = referencia.como_dict()
        originales = payload["directory_membership"]["directories"]
        assert isinstance(originales, list) and originales
        payload["directory_membership"]["directories"] = directorios_invalidos(originales)
        with pytest.raises(StockReferenceInvalidError, match="directory_membership|membership|inválida"):
            cargar_referencia_stock(payload)

    def test_rechaza_referencia_sin_skyrimse(self, tmp_path: pathlib.Path, parche_identidad: None) -> None:
        raiz = tmp_path / "sin-exe"
        (raiz / "Data").mkdir(parents=True)
        (raiz / "Data" / "Skyrim.esm").write_bytes(b"esm")
        with pytest.raises(StockSourceError, match="SkyrimSE"):
            construir_referencia_stock(raiz)


# ── adopción de la copia Stock ─────────────────────────────────────────────


class TestAdoptarCopiaStock:
    def test_acepta_copia_que_coincide_con_la_referencia(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        copia = adoptar_copia_stock(source, referencia)
        assert copia.provider == "stock"
        assert copia.reference_digest == referencia.reference_digest
        assert copia.runtime_identity.game_version == VERSION
        assert any(f.rel_path.casefold() == "skyrimse.exe" for f in copia.critical_files)

    def test_rechaza_directorio_vacio_eliminado(self, entorno) -> None:
        """TreeDigest no ve directorios vacíos: la membership de la referencia sí."""
        _storage, source, _clones, referencia = entorno
        (source / "Data" / "EmptyFolder").rmdir()
        with pytest.raises(StockSourceError, match="estructura de directorios"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_directorio_vacio_agregado(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        (source / "Data" / "DirectorioColado").mkdir()
        with pytest.raises(StockSourceError, match="estructura de directorios"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_area_steam_por_forma_canonica(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un alias 8.3 puede ocultar `steamapps` en la ruta cruda.

        La adopción debe comprobar también la ruta canónica (`resolve(strict=True)`),
        no sólo la forma recibida."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        _storage, source, _clones, referencia = entorno
        original = provisioning_module._dentro_de_area_steam
        llamadas: list[pathlib.Path] = []

        def falso(ruta: pathlib.Path):
            llamadas.append(ruta)
            if len(llamadas) == 1:
                return None
            return original(ruta) or "G:/Steam/steamapps/common"

        monkeypatch.setattr(provisioning_module, "_dentro_de_area_steam", falso)
        with pytest.raises(StockSourceError, match="Steam"):
            adoptar_copia_stock(source, referencia)
        assert len(llamadas) >= 2, "no se verificó la ruta canónica además de la cruda"

    def test_rechaza_byte_distinto(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        (source / "Data" / "Skyrim.esm").write_bytes(b"esm-manipulado")
        with pytest.raises(StockSourceError, match="NO coincide"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_archivo_extra(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        (source / "Data" / "colado.esp").write_bytes(b"extra")
        with pytest.raises(StockSourceError, match="NO coincide"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_archivo_critico_ausente(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        (source / "steam_api64.dll").unlink()
        with pytest.raises(StockSourceError, match="NO coincide|crític"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_hardlink_dentro_de_la_copia(self, entorno) -> None:
        _storage, source, _clones, referencia = entorno
        os.link(source / "Data" / "Skyrim.esm", source / "Data" / "Skyrim-copia.esm")
        with pytest.raises(StockSourceError, match="independiente|hardlink"):
            adoptar_copia_stock(source, referencia)

    @junction_guard
    def test_rechaza_junction_dentro_de_la_copia(self, entorno, tmp_path: pathlib.Path) -> None:
        _storage, source, _clones, referencia = entorno
        fuera = tmp_path / "fuera"
        fuera.mkdir()
        (fuera / "ajeno.bin").write_bytes(b"ajeno")
        motivo = crear_junction(source / "Data" / "Enlace", fuera)
        if motivo is not None:
            pytest.skip(f"no se pudo crear el junction: {motivo}")
        with pytest.raises(StockSourceError, match="inspeccionable|enlace|reparse"):
            adoptar_copia_stock(source, referencia)

    def test_rechaza_referencia_alterada_en_adopcion(self, entorno) -> None:
        """La segunda capa: aun salteando el loader, la adopción recomputa el digest."""
        _storage, source, _clones, referencia = entorno
        alterada = dataclasses.replace(
            referencia,
            expected_tree=dataclasses.replace(referencia.expected_tree, digest="b" * 64),
        )
        with pytest.raises(StockReferenceInvalidError, match="alterada"):
            adoptar_copia_stock(source, alterada)

    def test_rechaza_copia_dentro_de_area_steam(self, tmp_path: pathlib.Path, parche_identidad: None) -> None:
        steamapps = tmp_path / "lib" / "steamapps"
        (steamapps / "common" / "Skyrim Special Edition").mkdir(parents=True)
        source = _escribir_arbol(steamapps / "common" / "Skyrim Special Edition")
        with pytest.raises(StockSourceError, match="Steam"):
            adoptar_copia_stock(source, construir_referencia_stock(source))


# ── publicación de Generation ──────────────────────────────────────────────


class TestPublicarGeneration:
    def test_publica_generation_independiente_y_verificable(self, entorno) -> None:
        storage, source, _clones, _referencia = entorno
        antes = _hash_arbol(source)
        resultado, copia = _publicar(entorno)

        assert resultado.success, resultado.message
        assert resultado.generation_id is not None
        metadata = resultado.generation_metadata
        assert metadata is not None
        assert metadata.provider == "stock"
        assert metadata.provider_appid is None
        assert metadata.provider_buildid is None
        destino = generation_dir(storage, resultado.generation_id)
        assert destino.is_dir()
        assert _sin_hardlinks(destino)
        verificacion = verificar_generation(storage, resultado.generation_id)
        assert verificacion.success, verificacion.message
        assert verificacion.observed_digest == copia.tree_digest
        assert _hash_arbol(source) == antes
        sidecar = storage / "state" / "generations" / f"{resultado.generation_id}.source.json"
        assert sidecar.is_file()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["provider"] == "stock"
        assert payload["reference"]["reference_digest"] == copia.reference_digest

    def test_no_publica_sin_storage_inicializado(self, tmp_path: pathlib.Path, parche_identidad: None) -> None:
        base = _escribir_arbol(tmp_path / "base")
        referencia = construir_referencia_stock(base)
        copia = adoptar_copia_stock(base, referencia)
        resultado = publicar_generation_desde_stock(tmp_path / "sin-inicializar", copia)
        assert resultado.success is False
        assert "inicializado" in resultado.message

    def test_no_publica_con_journal_pendiente(self, entorno) -> None:
        storage, _source, _clones, _referencia = entorno
        (storage / "state" / "transition.json").write_text("{}", encoding="utf-8")
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        assert "journal" in resultado.message

    def test_no_publica_con_recuperacion_pendiente(self, entorno) -> None:
        storage, _source, _clones, _referencia = entorno
        (storage / "state" / "recovery" / "transicion.json").write_text("{}", encoding="utf-8")
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        assert "recuperaci" in resultado.message

    def test_no_publica_sin_espacio(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sky_claw.local.frozen_runtime.provisioning.shutil.disk_usage",
            lambda _p: types.SimpleNamespace(total=1, used=1, free=0),
        )
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        assert "espacio" in resultado.message

    def test_fallo_de_copia_no_publica_metadata_ni_toca_el_source(
        self, entorno, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage, source, _clones, _referencia = entorno
        antes = _hash_arbol(source)

        def falla(*_args: object, **_kwargs: object) -> None:
            raise CandidateCopyError("fallo de copia simulado")

        monkeypatch.setattr("sky_claw.local.frozen_runtime.provisioning.copiar_arbol_independiente", falla)
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        assert "copia" in resultado.message
        assert not list((storage / "state" / "generations").glob("*.json"))
        assert _hash_arbol(source) == antes

    def test_fallo_del_sidecar_no_publica_metadata(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        """El sidecar se escribe ANTES que la metadata: si falla, no queda una
        Generation 'válida' (metadata) sin procedencia recuperable."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, _clones, _referencia = entorno
        original = provisioning_module.write_json_atomic

        def falla(ruta: pathlib.Path, payload: dict[str, object]) -> None:
            if str(ruta).endswith(".source.json"):
                raise OSError("fallo de sidecar simulado")
            original(ruta, payload)

        monkeypatch.setattr(provisioning_module, "write_json_atomic", falla)
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        assert "sidecar" in resultado.message
        metadatas = [
            p for p in (storage / "state" / "generations").glob("*.json") if not p.name.endswith(".source.json")
        ]
        assert metadatas == [], "se publicó metadata sin sidecar de procedencia"

    def test_no_publica_dos_veces_el_mismo_contenido(self, entorno) -> None:
        resultado1, _copia = _publicar(entorno)
        resultado2, _copia2 = _publicar(entorno)
        assert resultado1.success
        assert resultado2.success is False
        assert "ya existe" in resultado2.message

    def test_no_consulta_señales_de_steam(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("el provisionamiento Stock no debe consultar señales de Steam")

        monkeypatch.setattr("sky_claw.local.frozen_runtime.provider_signals.observe_provider_activity", boom)
        monkeypatch.setattr("sky_claw.local.frozen_runtime.provider_signals.read_steam_manifest_observation", boom)
        resultado, _copia = _publicar(entorno)
        assert resultado.success, resultado.message


# ── creación de Runtime Clone ──────────────────────────────────────────────


class TestCrearRuntimeClone:
    def test_crea_clone_independiente_provisionado_y_verificable(self, entorno) -> None:
        storage, source, clones, _referencia = entorno
        publicado, copia = _publicar(entorno)
        assert publicado.success
        antes_source = _hash_arbol(source)
        antes_gen = _hash_arbol(generation_dir(storage, publicado.generation_id))

        resultado = crear_runtime_clone_desde_generation(
            storage,
            publicado.generation_id,
            clones,
            "clone-p7-a1",
        )
        assert resultado.success, resultado.message
        assert resultado.clone_record is not None
        assert resultado.clone_record.lifecycle is RuntimeCloneLifecycle.PROVISIONED
        clone_root = clones / "clone-p7-a1"
        assert clone_root.is_dir()
        assert _sin_hardlinks(clone_root)
        assert _hash_arbol(clone_root) == _hash_arbol(generation_dir(storage, publicado.generation_id))
        verificacion = verificar_runtime_clone(storage, "clone-p7-a1")
        assert verificacion.success, verificacion.message
        assert resultado.runtime_setup_id is not None
        assert resultado.runtime_setup_id.startswith("setup-")
        assert _hash_arbol(source) == antes_source
        assert _hash_arbol(generation_dir(storage, publicado.generation_id)) == antes_gen
        # El setup es content-bound y estable.
        assert verificar_runtime_setup(clone_root).setup_id == resultado.runtime_setup_id
        assert copia.provider == "stock"

    def test_rechaza_clone_sin_compatibilidad_skse(self, tmp_path: pathlib.Path, parche_identidad: None) -> None:
        base = _escribir_arbol(tmp_path / "base", con_skse=False)
        referencia = construir_referencia_stock(base)
        source = pathlib.Path(shutil.copytree(base, tmp_path / "source"))
        storage = tmp_path / "storage"
        assert initialize_frozen_runtime_storage(storage).success
        publicado = publicar_generation_desde_stock(storage, adoptar_copia_stock(source, referencia))
        assert publicado.success
        clones = tmp_path / "clones"
        clones.mkdir()
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-sin-skse")
        assert resultado.success is False
        assert "SKSE" in resultado.message or "compatib" in resultado.message

    def test_rechaza_destino_existente(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        (clones / "clone-existente").mkdir()
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-existente")
        assert resultado.success is False
        assert "existe" in resultado.message

    def test_rechaza_generation_inexistente(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        resultado = crear_runtime_clone_desde_generation(storage, "1.6.1170__000000000000", clones, "clone-x")
        assert resultado.success is False
        assert "generation" in resultado.message.lower()

    def test_rechaza_sidecar_ausente(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        (storage / "state" / "generations" / f"{publicado.generation_id}.source.json").unlink()
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-sin-sidecar")
        assert resultado.success is False
        assert "sidecar" in resultado.message

    def test_rechaza_generation_con_drift(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        (generation_dir(storage, publicado.generation_id) / "Data" / "Skyrim.esm").write_bytes(b"drift")
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-drift")
        assert resultado.success is False
        assert "VALID" in resultado.message or "drift" in resultado.message.lower()

    def test_rechaza_drift_de_directorio_vacio_en_generation(self, entorno) -> None:
        """El TreeDigest es ciego a directorios vacíos: la membership del sidecar
        es la que detecta que la Generation dejó de ser la sellada."""
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        (generation_dir(storage, publicado.generation_id) / "Data" / "EmptyFolder").rmdir()
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-vacio")
        assert resultado.success is False
        assert "Generation" in resultado.message and (
            "referencia" in resultado.message or "sidecar" in resultado.message
        )

    def test_rechaza_con_journal_pendiente(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        (storage / "state" / "transition.json").write_text("{}", encoding="utf-8")
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-journal")
        assert resultado.success is False
        assert "journal" in resultado.message

    def test_rechazo_preserva_source_y_generation(self, entorno) -> None:
        storage, source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        antes_source = _hash_arbol(source)
        antes_gen = _hash_arbol(generation_dir(storage, publicado.generation_id))
        (clones / "clone-ocupado").mkdir()
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-ocupado")
        assert resultado.success is False
        assert _hash_arbol(source) == antes_source
        assert _hash_arbol(generation_dir(storage, publicado.generation_id)) == antes_gen
        assert not (clones / "clone-ocupado-2").exists()


# ── setup mínimo ───────────────────────────────────────────────────────────


def test_verificar_sin_multilinks_detecta_hardlink(tmp_path: pathlib.Path) -> None:
    """Ancla directa: sin el chequeo de nlink se podría copiar un árbol que
    comparte file object con un socio externo (la copia no sería independiente)."""
    raiz = tmp_path / "arbol"
    raiz.mkdir()
    original = raiz / "a.bin"
    original.write_bytes(b"payload")
    os.link(original, tmp_path / "socio-externo.bin")
    resultado = verificar_sin_multilinks(raiz)
    assert resultado.success is False
    assert "hardlink" in resultado.message or "multi-link" in resultado.message


def test_setup_es_content_bound(entorno) -> None:
    _storage, source, _clones, _referencia = entorno
    primero = verificar_runtime_setup(source)
    segundo = verificar_runtime_setup(source)
    assert primero.setup_id == segundo.setup_id
    assert primero.critical_files == segundo.critical_files
    (source / "skse64_loader.exe").write_bytes(b"loader-distinto")
    tercero = verificar_runtime_setup(source)
    assert tercero.setup_id != primero.setup_id


# ── sidecar de procedencia: integridad y autoridad ─────────────────────────


def test_los_schemas_de_procedencia_estan_frozen() -> None:
    """Cambiar el schema sin bump explícito rompe este ancla."""
    from sky_claw.local.frozen_runtime import provisioning as provisioning_module

    assert (provisioning_module.REFERENCE_SCHEMA_VERSION, provisioning_module.SIDECAR_SCHEMA_VERSION) == (2, 2)


def _sidecar_path(storage: pathlib.Path, generation_id: str) -> pathlib.Path:
    return storage / "state" / "generations" / f"{generation_id}.source.json"


def _leer_sidecar(storage: pathlib.Path, generation_id: str) -> dict[str, object]:
    return json.loads(_sidecar_path(storage, generation_id).read_text(encoding="utf-8"))


def _escribir_sidecar(storage: pathlib.Path, generation_id: str, payload: dict[str, object]) -> None:
    _sidecar_path(storage, generation_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TestSidecarProvenance:
    def test_rechaza_reference_digest_adulterado(self, entorno) -> None:
        """No alcanza con 64 caracteres: el digest debe recomputarse contra la referencia embebida."""
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        payload = _leer_sidecar(storage, publicado.generation_id)
        if isinstance(payload.get("reference"), dict):
            payload["reference"]["reference_digest"] = "Z" * 64
        else:
            payload["reference_digest"] = "Z" * 64
        _escribir_sidecar(storage, publicado.generation_id, payload)

        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-tamper")
        assert resultado.success is False
        assert "referencia" in resultado.message.lower() or "alterad" in resultado.message.lower()

    def test_rechaza_critical_files_adulterados(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        payload = _leer_sidecar(storage, publicado.generation_id)
        criticos = payload["critical_files"]
        assert isinstance(criticos, list) and criticos
        criticos[0]["digest"] = "0" * 64
        _escribir_sidecar(storage, publicado.generation_id, payload)

        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-criticos")
        assert resultado.success is False
        assert "crític" in resultado.message.lower() or "critical" in resultado.message.lower()

    def test_rechaza_membership_sincronizada_con_un_drift(self, entorno) -> None:
        """Si se borra un directorio vacío de la Generation y se reescribe el sidecar
        para que coincida con el árbol observado, la referencia original debe rechazarlo."""
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        gen_dir = generation_dir(storage, publicado.generation_id)
        (gen_dir / "Data" / "EmptyFolder").rmdir()
        evidencia = capturar_membership_directorios(gen_dir)
        payload = _leer_sidecar(storage, publicado.generation_id)
        payload["directory_membership"] = {
            "digest": evidencia.digest,
            "directory_count": evidencia.directory_count,
            "directories": list(evidencia.directories),
        }
        _escribir_sidecar(storage, publicado.generation_id, payload)

        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-membership")
        assert resultado.success is False
        assert "membership" in resultado.message.lower() or "directorios" in resultado.message.lower()

    def test_rechaza_sidecar_v1_obsoleto(self, entorno) -> None:
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        payload = _leer_sidecar(storage, publicado.generation_id)
        payload.pop("reference", None)
        payload["schema_version"] = 1
        _escribir_sidecar(storage, publicado.generation_id, payload)

        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-v1")
        assert resultado.success is False
        assert "schema" in resultado.message.lower() or "obsolet" in resultado.message.lower()

    def test_sidecar_no_utf8_devuelve_resultado_tipado(self, entorno) -> None:
        """Un sidecar con bytes no-UTF8 debe fallar con ProvisionamientoResult, no con excepción cruda."""
        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        _sidecar_path(storage, publicado.generation_id).write_bytes(b"\xff\xfe\x00no-utf8")

        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-binario")
        assert resultado.success is False
        assert "utf" in resultado.message.lower()

    def test_sidecar_membership_invalida_es_provisioning_error(self, entorno) -> None:
        """DirectoryMembershipError del sidecar también debe mapearse a ProvisioningError."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, _clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        payload = _leer_sidecar(storage, publicado.generation_id)
        membership = payload["directory_membership"]
        directorios = membership["directories"]
        assert isinstance(directorios, list) and directorios
        membership["directories"] = [directorios[0], directorios[0]]
        _escribir_sidecar(storage, publicado.generation_id, payload)

        with pytest.raises(ProvisioningError, match="directory_membership|membership|inválida"):
            provisioning_module._cargar_sidecar(storage, publicado.generation_id)


# ── atomicidad lógica del provisionamiento ────────────────────────────────


def _sonda_lock(root: pathlib.Path) -> list[str]:
    """Desde OTRO hilo: intenta adquirir el root lock y reporta el resultado."""
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockError
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    resultado: list[str] = []

    def intentar() -> None:
        try:
            with participant_lock(root, timeout=0.0) as handle:
                handle.assert_owned()
                resultado.append("adquirido")
        except FrozenRuntimeLockError:
            resultado.append("busy")

    hilo = threading.Thread(target=intentar)
    hilo.start()
    hilo.join()
    return resultado


class TestAtomicidadProvisionamiento:
    def test_publicar_mantiene_el_lock_hasta_publicar_metadata(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mientras se verifica/publica, otro participante NO puede entrar al root."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, _clones, _referencia = entorno
        original = provisioning_module._verificar_arbol_en_destino
        sondas: list[list[str]] = []

        def con_sonda(*args: object, **kwargs: object):
            resultado = original(*args, **kwargs)
            sondas.append(_sonda_lock(storage))
            return resultado

        monkeypatch.setattr(provisioning_module, "_verificar_arbol_en_destino", con_sonda)
        resultado, _copia = _publicar(entorno)
        assert resultado.success, resultado.message
        assert sondas, "la verificación no corrió"
        assert sondas[0] == ["busy"], f"otro participante adquirió el lock durante la publicación: {sondas}"

    def test_clone_mantiene_el_lock_hasta_registrar_el_record(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        original = provisioning_module.verificar_runtime_setup
        sondas: list[list[str]] = []

        def con_sonda(*args: object, **kwargs: object):
            resultado = original(*args, **kwargs)
            sondas.append(_sonda_lock(storage))
            return resultado

        monkeypatch.setattr(provisioning_module, "verificar_runtime_setup", con_sonda)
        resultado = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-lock")
        assert resultado.success, resultado.message
        assert sondas
        assert sondas[0] == ["busy"], f"otro participante adquirió el lock durante la creación: {sondas}"

    def test_el_lock_se_libera_ante_un_fallo(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        storage, _source, _clones, _referencia = entorno

        def falla(*_args: object, **_kwargs: object) -> None:
            raise CandidateCopyError("fallo de copia simulado")

        monkeypatch.setattr("sky_claw.local.frozen_runtime.provisioning.copiar_arbol_independiente", falla)
        resultado, _copia = _publicar(entorno)
        assert resultado.success is False
        with participant_lock(storage, timeout=0.0) as handle:
            handle.assert_owned()

    def test_recupera_un_clone_que_quedo_en_provisioning(self, entorno, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fallo entre registrar PROVISIONING y PROVISIONED: el reintento debe completar
        el mismo Clone (mismo id/generation/setup), no pedir borrar el árbol a mano."""
        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        original = getattr(provisioning_module, "_actualizar_lifecycle_clone_bajo_lock", None)
        usado: list[bool] = []

        if original is None:
            from sky_claw.local.frozen_runtime import clones as clones_module

            original_public = clones_module.actualizar_lifecycle_clone

            def falla_una_vez(root, clone_id, nuevo_lifecycle, **kwargs):  # type: ignore[no-untyped-def]
                if nuevo_lifecycle is RuntimeCloneLifecycle.PROVISIONED and not usado:
                    usado.append(True)
                    raise ProvisioningError("fallo simulado antes de PROVISIONED")
                return original_public(root, clone_id, nuevo_lifecycle, **kwargs)

            monkeypatch.setattr(clones_module, "actualizar_lifecycle_clone", falla_una_vez)
        else:

            def falla_una_vez(root, clone_id, nuevo_lifecycle, **kwargs):  # type: ignore[no-untyped-def]
                if nuevo_lifecycle is RuntimeCloneLifecycle.PROVISIONED and not usado:
                    usado.append(True)
                    raise ProvisioningError("fallo simulado antes de PROVISIONED")
                return original(root, clone_id, nuevo_lifecycle, **kwargs)

            monkeypatch.setattr(provisioning_module, "_actualizar_lifecycle_clone_bajo_lock", falla_una_vez)

        primero = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-recuperable")
        assert primero.success is False
        assert usado, "el fallo simulado no llegó a la transición PROVISIONED"

        segundo = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-recuperable")
        assert segundo.success, segundo.message
        assert segundo.clone_record is not None
        assert segundo.clone_record.lifecycle is RuntimeCloneLifecycle.PROVISIONED

    def test_verificacion_indeterminada_no_marca_invalid_y_el_reintento_reverifica(
        self, entorno, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un fallo transitorio de verificación no puede degradar a INVALID un Clone
        PROVISIONED: INVALID es terminal y quemaría el id. El reintento debe correr
        la verificación completa antes de devolver éxito."""
        from types import SimpleNamespace

        from sky_claw.local.frozen_runtime import provisioning as provisioning_module

        storage, _source, clones, _referencia = entorno
        publicado, _copia = _publicar(entorno)
        original = provisioning_module.verificar_runtime_clone
        llamadas: list[int] = []

        def indeterminada_una_vez(*args: object, **kwargs: object):
            llamadas.append(1)
            if len(llamadas) == 1:
                return SimpleNamespace(
                    state=GenerationVerificationState.INDETERMINATE,
                    success=False,
                    message="lectura transitoria de un crítico",
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(provisioning_module, "verificar_runtime_clone", indeterminada_una_vez)
        primero = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-indet")
        assert primero.success is False
        assert "indetermin" in primero.message.lower() or "concluyente" in primero.message.lower()
        registro = leer_runtime_clone_record(storage, "clone-indet")
        assert registro.lifecycle is RuntimeCloneLifecycle.PROVISIONED, "un INDETERMINATE degradó el registro a INVALID"

        segundo = crear_runtime_clone_desde_generation(storage, publicado.generation_id, clones, "clone-indet")
        assert segundo.success, segundo.message
        assert len(llamadas) >= 2, "el reintento no re-ejecutó la verificación completa del Clone"
