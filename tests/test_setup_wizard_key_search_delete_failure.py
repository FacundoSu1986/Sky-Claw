"""H08-F1: desactivar ``search_api_key`` exige confirmar el borrado.

Contratos cubiertos:

- ``""`` en el campo de búsqueda significa "desactivar"; otros campos vacíos
  conservan el comportamiento histórico de "no cambiar".
- ``PasswordDeleteError`` no prueba ausencia: se verifica con una lectura.
- Si no puede verificarse la ausencia, hay notificación negativa, no falso
  éxito, no ``first_run=False`` y no se cierra el wizard.
- Un secreto histórico en TOML no debe resucitar tras desactivarlo.
"""

from __future__ import annotations

import asyncio
import pathlib
import tomllib
from dataclasses import dataclass, field
from typing import Any

import keyring
import keyring.errors
import pytest

from sky_claw.app.gui import setup_wizard as sw
from sky_claw.config import Config

SYN_OLD = "SYN-H08-F1-SEARCH-VIEJA-5511"
SYN_NEW = "SYN-H08-F1-SEARCH-NUEVA-7741"
SYN_LEAK = "SYN-H08-F1-FILTRABLE-9003"
SYN_NEXUS = "SYN-H08-F1-NEXUS-3312"
SYN_TELEGRAM = "SYN-H08-F1-TG-4413"


class _Backend:
    def __init__(
        self,
        *,
        stored: dict[str, str] | None = None,
        delete_error: Exception | None = None,
        get_error: Exception | None = None,
        get_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.store = dict(stored or {})
        self.delete_error = delete_error
        self.get_error = get_error
        self.get_errors = dict(get_errors or {})
        self.deletes: list[str] = []
        self.gets: list[str] = []

    def get_password(self, service: str, name: str) -> str | None:
        self.gets.append(name)
        if name in self.get_errors:
            raise self.get_errors[name]
        if self.get_error is not None:
            raise self.get_error
        return self.store.get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[name] = value

    def delete_password(self, service: str, name: str) -> None:
        self.deletes.append(name)
        if self.delete_error is not None:
            raise self.delete_error
        self.store.pop(name, None)


@dataclass
class _Store:
    values: dict[str, Any] = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


@dataclass
class _Overlay:
    deleted: bool = False

    def delete(self) -> None:
        self.deleted = True


@dataclass
class _Timer:
    deactivated: bool = False

    def deactivate(self) -> None:
        self.deactivated = True


@dataclass
class _Run:
    backend: _Backend
    notifications: list[tuple[str, str | None]]
    completed: list[bool]
    store: _Store
    overlay: _Overlay
    timer: _Timer
    config_path: pathlib.Path

    @property
    def negative(self) -> list[str]:
        return [msg for msg, kind in self.notifications if kind == "negative"]


class _Escenario:
    def __init__(
        self,
        *,
        wizard: sw.SetupWizardModal,
        backend: _Backend,
        notifications: list[tuple[str, str | None]],
        completed: list[bool],
        store: _Store,
        overlay: _Overlay,
        timer: _Timer,
        config_path: pathlib.Path,
    ) -> None:
        self.wizard = wizard
        self.backend = backend
        self.notifications = notifications
        self.completed = completed
        self.store = store
        self.overlay = overlay
        self.timer = timer
        self.config_path = config_path

    async def ejecutar(
        self,
        *,
        api_key: str = "",
        nexus_key: str = "",
        search_key: str = "",
        telegram_token: str = "",
        telegram_chatid: str = "",
    ) -> _Run:
        await self.wizard._validate_and_save("ollama", api_key, nexus_key, search_key, telegram_token, telegram_chatid)
        return _Run(
            self.backend,
            self.notifications,
            self.completed,
            self.store,
            self.overlay,
            self.timer,
            self.config_path,
        )


class _Harness:
    def __init__(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.contador = 0

    def build(
        self,
        *,
        stored: dict[str, str] | None = None,
        toml: dict[str, str] | None = None,
        delete_error: Exception | None = None,
        get_error: Exception | None = None,
        get_errors: dict[str, Exception] | None = None,
        inject_toml_after_save: tuple[int, str] | None = None,
        fail_search_get_after_save: int | None = None,
    ) -> _Escenario:
        self.contador += 1
        backend = _Backend(
            stored=stored,
            delete_error=delete_error,
            get_error=get_error,
            get_errors=get_errors,
        )
        self.monkeypatch.setattr(keyring, "get_password", backend.get_password)
        self.monkeypatch.setattr(keyring, "set_password", backend.set_password)
        self.monkeypatch.setattr(keyring, "delete_password", backend.delete_password)

        config_path = self.tmp_path / f"config_{self.contador}.toml"
        if toml:
            config_path.write_text(
                "".join(f'{clave} = "{valor}"\n' for clave, valor in toml.items()),
                encoding="utf-8",
            )

        if inject_toml_after_save is not None or fail_search_get_after_save is not None:
            guardado_original = Config.save
            llamadas = {"n": 0}

            def guardar_con_inyeccion(self: Config) -> str:
                resultado = guardado_original(self)
                llamadas["n"] += 1
                if inject_toml_after_save is not None and llamadas["n"] == inject_toml_after_save[0]:
                    with config_path.open("a", encoding="utf-8") as fh:
                        fh.write(f'search_api_key = "{inject_toml_after_save[1]}"\n')
                if fail_search_get_after_save is not None and llamadas["n"] == fail_search_get_after_save:
                    backend.get_errors["search_api_key"] = RuntimeError(f"detalle={SYN_LEAK}")
                return resultado

            self.monkeypatch.setattr(Config, "save", guardar_con_inyeccion)

        notificaciones: list[tuple[str, str | None]] = []

        def notify(mensaje: str, type: str | None = None) -> None:  # noqa: A002
            notificaciones.append((mensaje, type))

        async def js(*_args: Any, **_kwargs: Any) -> str:
            return ""

        store = _Store()
        overlay = _Overlay()
        timer = _Timer()
        completado: list[bool] = []

        async def on_complete() -> None:
            completado.append(True)

        self.monkeypatch.setattr(sw.ui, "notify", notify)
        self.monkeypatch.setattr(sw.ui, "run_javascript", js)
        self.monkeypatch.setattr(sw, "get_store", lambda: store)

        wizard = sw.SetupWizardModal(config_path, on_complete)
        wizard._overlay_el = overlay  # type: ignore[assignment]
        wizard._lore_timer = timer  # type: ignore[assignment]

        return _Escenario(
            wizard=wizard,
            backend=backend,
            notifications=notificaciones,
            completed=completado,
            store=store,
            overlay=overlay,
            timer=timer,
            config_path=config_path,
        )


@pytest.fixture
def harness(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch)


def _toml(path: pathlib.Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_credencial_realmente_ausente_es_exito_legitimo(harness: _Harness) -> None:
    esc = harness.build(stored={})
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.negative == []
    assert resultado.backend.store.get("search_api_key") is None
    assert resultado.store.values.get("first_run") is False


def test_borrado_exitoso_no_resucita_tras_recargar_config(harness: _Harness) -> None:
    esc = harness.build(stored={"search_api_key": SYN_OLD})
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.backend.store.get("search_api_key") is None
    assert "search_api_key" not in _toml(resultado.config_path)
    assert not Config(resultado.config_path)._data.get("search_api_key")


def test_fallo_de_borrado_con_credencial_presente_es_fallo(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=keyring.errors.KeyringError("backend roto"),
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert resultado.backend.store.get("search_api_key") == SYN_OLD
    assert SYN_OLD not in caplog.text
    assert SYN_OLD not in " ".join(resultado.negative)


def test_password_delete_error_con_credencial_presente_es_fallo(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=keyring.errors.PasswordDeleteError("no se pudo borrar"),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert resultado.backend.store.get("search_api_key") == SYN_OLD


def test_password_delete_error_con_ausencia_comprobada_es_benigno(harness: _Harness) -> None:
    esc = harness.build(
        stored={},
        delete_error=keyring.errors.PasswordDeleteError("nada que borrar"),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.negative == []
    assert resultado.backend.store.get("search_api_key") is None


def test_fallo_de_lectura_impide_verificar_la_ausencia(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        get_error=OSError("lectura indisponible"),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert "search_api_key" in resultado.backend.gets


def test_backend_inesperadamente_averiado_no_falso_exito(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=RuntimeError("fallo inesperado del backend"),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert resultado.overlay.deleted is False
    assert resultado.timer.deactivated is False


def test_excepcion_con_credencial_sintetica_no_se_expone(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=RuntimeError(f"detalle={SYN_LEAK}"),
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in " ".join(msg for msg, _ in resultado.notifications)


def test_excepcion_del_guardado_no_filtra_valor_sintetico(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    esc = harness.build(
        stored={},
        get_errors={"ws_auth_token": RuntimeError(f"detalle={SYN_LEAK}")},
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in " ".join(msg for msg, _ in resultado.notifications)


def test_credencial_historica_en_toml_no_resucita(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        toml={"llm_provider": "ollama", "search_api_key": SYN_OLD},
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.backend.store.get("search_api_key") is None
    assert "search_api_key" not in _toml(resultado.config_path)
    assert not Config(resultado.config_path)._data.get("search_api_key")


def test_campo_search_no_vacio_escritura_valida_preservada(harness: _Harness) -> None:
    esc = harness.build(stored={})
    resultado = asyncio.run(esc.ejecutar(search_key=SYN_NEW))

    assert resultado.completed == [True]
    assert resultado.backend.store.get("search_api_key") == SYN_NEW
    assert Config(resultado.config_path)._data.get("search_api_key") == SYN_NEW


def test_otros_campos_vacios_conservan_comportamiento_historico(harness: _Harness) -> None:
    esc = harness.build(stored={"nexus_api_key": SYN_NEXUS, "telegram_bot_token": SYN_TELEGRAM})
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.backend.store.get("nexus_api_key") == SYN_NEXUS
    assert resultado.backend.store.get("telegram_bot_token") == SYN_TELEGRAM
    assert resultado.backend.store.get("search_api_key") is None


def test_recuperacion_tras_reparar_el_backend_y_reintentar(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=RuntimeError(f"detalle={SYN_LEAK}"),
    )
    primero = asyncio.run(esc.ejecutar(search_key=""))
    assert primero.completed == []

    esc.backend.delete_error = None
    esc.backend.get_error = None
    segundo = asyncio.run(esc.ejecutar(search_key=""))

    assert len(segundo.negative) == 1
    assert segundo.completed == [True]
    assert segundo.backend.store.get("search_api_key") is None


def test_fallo_no_publica_first_run_ni_cierra_wizard(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=keyring.errors.KeyringError("backend roto"),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.store.values.get("first_run") is not False
    assert resultado.overlay.deleted is False
    assert resultado.timer.deactivated is False


def test_f01_segundo_save_no_reintroduce_credencial(harness: _Harness) -> None:
    """Otra superficie escribe el secreto al TOML entre ambos saves.

    La verificación intermedia mira sólo el keyring; sin reafirmar la intención
    de borrado, el segundo save migra el plaintext al keyring y el wizard publica
    éxito con la credencial activa.
    """
    esc = harness.build(
        stored={},
        inject_toml_after_save=(1, SYN_OLD),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.backend.store.get("search_api_key") is None
    assert "search_api_key" not in _toml(resultado.config_path)
    assert not Config(resultado.config_path)._data.get("search_api_key")


def test_f01_fallo_en_verificacion_final_no_deja_first_run_persistido(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    esc = harness.build(stored={}, fail_search_get_after_save=2)
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert _toml(resultado.config_path).get("first_run") is not False
    assert resultado.store.values.get("first_run") is not False
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in " ".join(msg for msg, _ in resultado.notifications)
