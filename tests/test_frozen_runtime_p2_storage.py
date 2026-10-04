"""Tests de P2 — storage: admisión de root, layout, inicialización y estado (L*/ST*).

Convención: AAA en español, fail-closed en todos los caminos de error.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

from sky_claw.local.frozen_runtime import (
    FrozenRuntimeState,
    FrozenRuntimeStorageError,
    InvalidGenerationIdError,
    StateCorruptError,
    StateSchemaError,
    StorageAdmissionState,
    StorageInitResult,
    default_storage_root,
    initialize_frozen_runtime_storage,
    load_frozen_runtime_state,
    same_volume,
    save_frozen_runtime_state,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    admitir_directorio_storage,
    admitir_storage_root,
    candidates_dir,
    generations_state_dir,
    state_dir,
    versions_dir,
)

GID_VALIDO = "1.6.1170__a1b2c3d4e5f6"


def _enlace_directorio(destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Crea un enlace de directorio: junction en Windows, symlink en POSIX."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)], check=True, capture_output=True)
    else:
        enlace.symlink_to(destino, target_is_directory=True)


def _escenario_reparse(
    tmp_path: pathlib.Path, componente: str, *, tipo: str = "junction"
) -> tuple[pathlib.Path, pathlib.Path]:
    """Root con un componente persistente enlazado a un árbol externo con sentinel."""
    root = tmp_path / "frozen"
    (root / "state").mkdir(parents=True) if componente == "state/generations" else root.mkdir()
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "sentinel.txt").write_text("intacto", encoding="utf-8")
    enlace = root / componente
    if tipo == "symlink" and os.name == "nt":
        try:
            enlace.symlink_to(externo, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink no disponible en este Windows (dev mode): {exc}")
    else:
        _enlace_directorio(externo, enlace)
    return root, externo


def _fuente_sintetica(base: pathlib.Path) -> pathlib.Path:
    """Estructura Steam mínima para pruebas de admisión (sin inventario real)."""
    common = base / "steamapps" / "common" / "Skyrim Special Edition"
    common.mkdir(parents=True)
    (common / "SkyrimSE.exe").write_bytes(b"exe")
    return common


# ── Layout / admisión / inicialización ───────────────────────────────────


class TestLayout:
    def test_l01_inicializa_root_vacio(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        resultado = initialize_frozen_runtime_storage(root)
        assert resultado.success
        assert versions_dir(root).is_dir()
        assert candidates_dir(root).is_dir()
        assert state_dir(root).is_dir()
        carga = load_frozen_runtime_state(active_state_path(root))
        assert carga.found and carga.state is not None
        assert carga.state.desired_active_generation is None

    def test_l02_inicializar_dos_veces_idempotente(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        (versions_dir(root) / "contenido_ajeno.txt").write_text("no borrar", encoding="utf-8")
        segundo = initialize_frozen_runtime_storage(root)
        assert segundo.success
        assert (versions_dir(root) / "contenido_ajeno.txt").read_text(encoding="utf-8") == "no borrar"
        assert not segundo.state_initialized  # el estado ya existía

    def test_l03_archivo_donde_se_espera_versions_falla(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        root.mkdir()
        (root / "versions").write_text("soy un archivo", encoding="utf-8")
        resultado = initialize_frozen_runtime_storage(root)
        assert not resultado.success
        assert "archivo donde se esperaba" in resultado.admission.message

    def test_l04_root_reparse_rechazado(self, tmp_path: pathlib.Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        enlace = tmp_path / "enlace"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(real)], check=True, capture_output=True)
        else:
            enlace.symlink_to(real, target_is_directory=True)
        resultado = admitir_storage_root(enlace)
        assert resultado.state is StorageAdmissionState.REJECTED
        assert "redirigido" in resultado.message or "enlace" in resultado.message

    def test_l05_root_igual_a_managed_source_rechazado(self, tmp_path: pathlib.Path) -> None:
        fuente = _fuente_sintetica(tmp_path)
        resultado = admitir_storage_root(fuente, managed_source_root=fuente)
        assert resultado.state is StorageAdmissionState.REJECTED

    def test_l06_managed_source_dentro_del_root_rechazado(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        root.mkdir()
        fuente = _fuente_sintetica(root / "adentro")
        resultado = admitir_storage_root(root, managed_source_root=fuente)
        assert resultado.state is StorageAdmissionState.REJECTED

    def test_l07_root_fuera_de_la_fuente_admitido(self, tmp_path: pathlib.Path) -> None:
        fuente = _fuente_sintetica(tmp_path / "steam")
        root = tmp_path / "frozen"
        resultado = admitir_storage_root(root, managed_source_root=fuente)
        assert resultado.state is StorageAdmissionState.ADMITTED

    def test_l08_root_dentro_de_steamapps_common_rechazado(self, tmp_path: pathlib.Path) -> None:
        common = _fuente_sintetica(tmp_path)
        root = common / "storage" / "frozen"
        resultado = admitir_storage_root(root)
        assert resultado.state is StorageAdmissionState.REJECTED
        assert "Steam" in resultado.message

    def test_l09_default_root_convencion_per_user(self) -> None:
        default = default_storage_root()
        assert default.is_absolute()
        assert default == pathlib.Path.home() / ".sky_claw" / "frozen-runtime"
        temp = pathlib.Path(os.environ.get("TEMP", ""))
        if str(temp):
            assert not str(default).casefold().startswith(str(temp).casefold())

    def test_l08a_root_igual_a_steamapps_common_rechazado(self, tmp_path: pathlib.Path) -> None:
        common = tmp_path / "steamapps" / "common"
        common.mkdir(parents=True)
        resultado = admitir_storage_root(common)
        assert resultado.state is StorageAdmissionState.REJECTED
        assert "Steam" in resultado.message

    def test_l08b_root_debajo_de_steamapps_common_rechazado(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "steamapps" / "common" / "cualquier_cosa" / "storage"
        root.mkdir(parents=True)
        resultado = admitir_storage_root(root)
        assert resultado.state is StorageAdmissionState.REJECTED

    def test_l08c_hermano_de_steamapps_common_admitido(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "steamapps" / "otro" / "frozen"
        root.mkdir(parents=True)
        resultado = admitir_storage_root(root)
        assert resultado.state is StorageAdmissionState.ADMITTED

    def test_l10_same_volume(self, tmp_path: pathlib.Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert same_volume(a, b)
        with pytest.raises(FrozenRuntimeStorageError):
            same_volume(a, tmp_path / "no_existe")


# ── Estado persistente ───────────────────────────────────────────────────


def _estado(deseado: str | None, ts: int = 1) -> FrozenRuntimeState:
    return FrozenRuntimeState(schema_version=1, desired_active_generation=deseado, updated_at_ns=ts)


class TestEstado:
    def test_st01_ausente_es_arranque_limpio(self, tmp_path: pathlib.Path) -> None:
        carga = load_frozen_runtime_state(tmp_path / "active.json")
        assert not carga.found
        assert carga.state is None
        assert "limpio" in carga.message

    def test_st02_round_trip_v1(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        save_frozen_runtime_state(path, _estado(None))
        assert load_frozen_runtime_state(path).state == _estado(None)
        save_frozen_runtime_state(path, _estado(GID_VALIDO, ts=42))
        carga = load_frozen_runtime_state(path)
        assert carga.found and carga.state is not None
        assert carga.state.desired_active_generation == GID_VALIDO
        assert carga.state.updated_at_ns == 42

    def test_st03_json_malformado_fail_closed(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        path.write_text("{ esto no es json", encoding="utf-8")
        with pytest.raises(StateCorruptError):
            load_frozen_runtime_state(path)

    def test_st04_json_truncado_fail_closed(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        path.write_text('{"schema_version": 1, "desired', encoding="utf-8")
        with pytest.raises(StateCorruptError):
            load_frozen_runtime_state(path)

    def test_st05_schema_desconocido_fail_closed(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        path.write_text(json.dumps({"schema_version": 2, "desired_active_generation": None, "updated_at_ns": 0}))
        with pytest.raises(StateSchemaError):
            load_frozen_runtime_state(path)

    def test_st06_temp_replace_deja_target_valido(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        save_frozen_runtime_state(path, _estado(GID_VALIDO))
        sobrantes = [p.name for p in tmp_path.iterdir() if p.name != "active.json"]
        assert sobrantes == []
        assert load_frozen_runtime_state(path).state is not None

    def test_st07_fallo_antes_del_replace_conserva_estado_previo(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "active.json"
        save_frozen_runtime_state(path, _estado(GID_VALIDO, ts=1))

        def replace_roto(src: object, dst: object) -> None:
            raise OSError("fallo inyectado antes del replace")

        monkeypatch.setattr("sky_claw.local.frozen_runtime.state.os.replace", replace_roto)
        with pytest.raises(OSError):
            save_frozen_runtime_state(path, _estado(None, ts=2))
        monkeypatch.undo()
        carga = load_frozen_runtime_state(path)
        assert carga.state == _estado(GID_VALIDO, ts=1)
        sobrantes = [p.name for p in tmp_path.iterdir() if p.name != "active.json"]
        assert sobrantes == []  # el temporal se limpió

    def test_st08_tipos_incorrectos_rechazados(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        base = {"schema_version": 1, "desired_active_generation": None, "updated_at_ns": 0}
        for campo, valor in (("desired_active_generation", 123), ("updated_at_ns", "x"), ("schema_version", "1")):
            data = dict(base)
            data[campo] = valor
            path.write_text(json.dumps(data), encoding="utf-8")
            with pytest.raises(StateSchemaError):
                load_frozen_runtime_state(path)

    def test_st09_traversal_en_generation_id_rechazado(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "desired_active_generation": "../evil__aaaaaaaaaaaa",
                    "updated_at_ns": 0,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(InvalidGenerationIdError):
            load_frozen_runtime_state(path)

    def test_st10_estado_vacio_no_es_ausente(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "active.json"
        path.write_text("", encoding="utf-8")
        with pytest.raises(StateCorruptError):
            load_frozen_runtime_state(path)


# ── Namespace de storage: reparse/junction en subdirectorios (P2-B1) ─────


class TestNamespaceReparse:
    """Un componente enlazado no puede redirigir escrituras fuera del root."""

    def _assert_rechazo_sin_escritura(
        self, root: pathlib.Path, externo: pathlib.Path, resultado: StorageInitResult
    ) -> None:
        assert not resultado.success
        # El árbol externo NO fue mutado: sentinel intacto y sin active.json.
        assert (externo / "sentinel.txt").read_text(encoding="utf-8") == "intacto"
        assert not (externo / "active.json").exists()

    def test_sr01_versions_junction_falla(self, tmp_path: pathlib.Path) -> None:
        root, externo = _escenario_reparse(tmp_path, "versions")
        resultado = initialize_frozen_runtime_storage(root)
        self._assert_rechazo_sin_escritura(root, externo, resultado)

    def test_sr02_candidates_junction_falla(self, tmp_path: pathlib.Path) -> None:
        root, externo = _escenario_reparse(tmp_path, "candidates")
        resultado = initialize_frozen_runtime_storage(root)
        self._assert_rechazo_sin_escritura(root, externo, resultado)

    def test_sr03_state_junction_falla_sin_escribir_afuera(self, tmp_path: pathlib.Path) -> None:
        root, externo = _escenario_reparse(tmp_path, "state")
        resultado = initialize_frozen_runtime_storage(root)
        self._assert_rechazo_sin_escritura(root, externo, resultado)
        # El caso crítico: active.json habría ido a parar a 'externo'.
        assert not (root / "state" / "active.json").exists()

    def test_sr04_state_generations_junction_falla(self, tmp_path: pathlib.Path) -> None:
        root, externo = _escenario_reparse(tmp_path, "state/generations")
        resultado = initialize_frozen_runtime_storage(root)
        self._assert_rechazo_sin_escritura(root, externo, resultado)
        assert not (root / "state" / "active.json").exists()  # init abortó antes de escribir estado

    def test_sr05_symlink_subdir_falla(self, tmp_path: pathlib.Path) -> None:
        root, externo = _escenario_reparse(tmp_path, "state", tipo="symlink")
        resultado = initialize_frozen_runtime_storage(root)
        self._assert_rechazo_sin_escritura(root, externo, resultado)

    def test_sr06_error_de_inspeccion_rechazado(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        componente = tmp_path / "componente"
        componente.mkdir()

        def lstat_roto(path: object) -> str | None:
            raise OSError("inspección imposible inyectada")

        monkeypatch.setattr("sky_claw.local.frozen_runtime.storage.link_kind_or_raise", lstat_roto)
        resultado = admitir_directorio_storage(componente)
        assert resultado.state is StorageAdmissionState.REJECTED
        assert "inspeccionar" in resultado.message

    def test_sr07_directorios_reales_idempotente(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        for directorio in (
            root,
            versions_dir(root),
            candidates_dir(root),
            state_dir(root),
            generations_state_dir(root),
        ):
            directorio.mkdir(parents=True, exist_ok=True)
        (root / "state" / "ajeno.txt").write_text("no borrar", encoding="utf-8")
        resultado = initialize_frozen_runtime_storage(root)
        assert resultado.success
        assert (root / "state" / "ajeno.txt").read_text(encoding="utf-8") == "no borrar"

    def test_sr08_admitir_directorio_archivo_rechazado(self, tmp_path: pathlib.Path) -> None:
        archivo = tmp_path / "un_archivo"
        archivo.write_text("x", encoding="utf-8")
        resultado = admitir_directorio_storage(archivo)
        assert resultado.state is StorageAdmissionState.REJECTED
        assert "archivo" in resultado.message
