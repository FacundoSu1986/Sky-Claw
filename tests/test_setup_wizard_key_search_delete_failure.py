"""H08-F1: desactivar ``search_api_key`` exige confirmar el borrado.

Contratos cubiertos:

- ``""`` en el campo de búsqueda significa "desactivar"; otros campos vacíos
  conservan el comportamiento histórico de "no cambiar".
- ``PasswordDeleteError`` no prueba ausencia: se verifica con una lectura.
- Si no puede verificarse la ausencia, hay notificación negativa, no falso
  éxito, no ``first_run=False`` y no se cierra el wizard.
- Un secreto histórico en TOML no debe resucitar tras desactivarlo.
- R3: persistencia y finalización se informan por separado; la comprobación
  de desactivación precede a cualquier escritura opcional; ``first_run`` se
  persiste sólo después de confirmar la ausencia tras el último guardado que
  podía resurrectar la credencial.
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
SYN_NEXUS_NEW = "SYN-H08-F1-NEXUS-NUEVA-3313"
SYN_TELEGRAM = "4413:OLD"
SYN_TELEGRAM_NEW = "4413:NEW"
SYN_LLM = "SYN-H08-F1-LLM-2210"
SYN_LLM_NEW = "SYN-H08-F1-LLM-NUEVA-2211"


class _Backend:
    def __init__(
        self,
        *,
        stored: dict[str, str] | None = None,
        delete_error: Exception | None = None,
        get_error: Exception | None = None,
        get_errors: dict[str, Exception] | None = None,
        delete_errors: dict[str, Exception] | None = None,
        delete_error_from_call: tuple[int, Exception] | None = None,
    ) -> None:
        self.store = dict(stored or {})
        self.delete_error = delete_error
        self.get_error = get_error
        self.get_errors = dict(get_errors or {})
        self.delete_errors = dict(delete_errors or {})
        self.delete_error_from_call = delete_error_from_call
        self.deletes: list[str] = []
        self.gets: list[str] = []
        self.sets: list[str] = []

    def get_password(self, service: str, name: str) -> str | None:
        self.gets.append(name)
        if name in self.get_errors:
            raise self.get_errors[name]
        if self.get_error is not None:
            raise self.get_error
        return self.store.get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.sets.append(name)
        self.store[name] = value

    def delete_password(self, service: str, name: str) -> None:
        self.deletes.append(name)
        if name in self.delete_errors:
            raise self.delete_errors[name]
        if (
            self.delete_error_from_call is not None
            and name == "search_api_key"
            and len(self.deletes) == self.delete_error_from_call[0]
        ):
            raise self.delete_error_from_call[1]
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
class _SaveControl:
    llamadas: int = 0
    error_en_llamada: int | None = None
    error: Exception | None = None
    inyectar_toml: tuple[int, str] | None = None
    fallar_get: int | None = None


@dataclass
class _Run:
    backend: _Backend
    notifications: list[tuple[str, str | None]]
    completed: list[bool]
    store: _Store
    overlay: _Overlay
    timer: _Timer
    config_path: pathlib.Path
    save_control: _SaveControl

    @property
    def negative(self) -> list[str]:
        return [msg for msg, kind in self.notifications if kind == "negative"]

    @property
    def warnings(self) -> list[str]:
        return [msg for msg, kind in self.notifications if kind == "warning"]

    @property
    def mensajes(self) -> str:
        return " ".join(msg for msg, _ in self.notifications)


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
        save_control: _SaveControl,
    ) -> None:
        self.wizard = wizard
        self.backend = backend
        self.notifications = notifications
        self.completed = completed
        self.store = store
        self.overlay = overlay
        self.timer = timer
        self.config_path = config_path
        self.save_control = save_control

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
            self.save_control,
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
        toml: dict[str, Any] | None = None,
        delete_error: Exception | None = None,
        get_error: Exception | None = None,
        get_errors: dict[str, Exception] | None = None,
        delete_errors: dict[str, Exception] | None = None,
        delete_error_from_call: tuple[int, Exception] | None = None,
        inject_toml_after_save: tuple[int, str] | None = None,
        fail_search_get_after_save: int | None = None,
        save_error: tuple[int, Exception] | None = None,
        js_error: Exception | None = None,
        completion_error: Exception | None = None,
    ) -> _Escenario:
        self.contador += 1
        backend = _Backend(
            stored=stored,
            delete_error=delete_error,
            get_error=get_error,
            get_errors=get_errors,
            delete_errors=delete_errors,
            delete_error_from_call=delete_error_from_call,
        )
        self.monkeypatch.setattr(keyring, "get_password", backend.get_password)
        self.monkeypatch.setattr(keyring, "set_password", backend.set_password)
        self.monkeypatch.setattr(keyring, "delete_password", backend.delete_password)

        config_path = self.tmp_path / f"config_{self.contador}.toml"
        if toml:
            config_path.write_text(
                "".join(f"{clave} = {_valor_toml(valor)}\n" for clave, valor in toml.items()),
                encoding="utf-8",
            )

        save_control = _SaveControl(
            error_en_llamada=save_error[0] if save_error is not None else None,
            error=save_error[1] if save_error is not None else None,
            inyectar_toml=inject_toml_after_save,
            fallar_get=fail_search_get_after_save,
        )
        guardado_original = Config.save

        def guardar_con_control(self: Config) -> str:
            save_control.llamadas += 1
            if save_control.error_en_llamada == save_control.llamadas and save_control.error is not None:
                raise save_control.error
            resultado = guardado_original(self)
            if save_control.inyectar_toml is not None and save_control.llamadas == save_control.inyectar_toml[0]:
                with config_path.open("a", encoding="utf-8") as fh:
                    fh.write(f'search_api_key = "{save_control.inyectar_toml[1]}"\n')
            if save_control.fallar_get == save_control.llamadas:
                backend.get_errors["search_api_key"] = RuntimeError(f"detalle={SYN_LEAK}")
            return resultado

        self.monkeypatch.setattr(Config, "save", guardar_con_control)

        notificaciones: list[tuple[str, str | None]] = []

        def notify(mensaje: str, type: str | None = None) -> None:  # noqa: A002
            notificaciones.append((mensaje, type))

        async def js(*_args: Any, **_kwargs: Any) -> str:
            if js_error is not None:
                raise js_error
            return ""

        store = _Store()
        overlay = _Overlay()
        timer = _Timer()
        completado: list[bool] = []

        async def on_complete() -> None:
            completado.append(True)
            if completion_error is not None:
                raise completion_error

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
            save_control=save_control,
        )


@pytest.fixture
def harness(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch)


def _valor_toml(valor: Any) -> str:
    if isinstance(valor, bool):
        return "true" if valor else "false"
    return f'"{valor}"'


def _toml(path: pathlib.Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------------------
# Contratos R2 preservados
# ---------------------------------------------------------------------------


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
    assert SYN_OLD not in resultado.mensajes


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
    assert SYN_LEAK not in resultado.mensajes


def test_excepcion_del_guardado_no_filtra_valor_sintetico(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    esc = harness.build(
        stored={},
        get_errors={"ws_auth_token": RuntimeError(f"detalle={SYN_LEAK}")},
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in resultado.mensajes


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


def test_f01_inyeccion_toml_despues_del_segundo_save_no_publica_exito(harness: _Harness) -> None:
    """El guardado que publica first_run no debe migrar un secreto inyectado después.

    La comprobación de keyring no ve el TOML. Si otra superficie deja plaintext
    entre el segundo guardado y el de ``first_run``, el wizard debe detectarlo,
    limpiarlo y no publicar éxito.
    """
    esc = harness.build(
        stored={},
        inject_toml_after_save=(2, SYN_OLD),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert "parcialmente" in resultado.mensajes
    assert _toml(resultado.config_path).get("first_run") is not False
    assert "search_api_key" not in _toml(resultado.config_path)
    assert resultado.backend.store.get("search_api_key") is None


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
    assert SYN_LEAK not in resultado.mensajes


# ---------------------------------------------------------------------------
# C2 — Mutación M1: el helper debe leer después de PasswordDeleteError
# ---------------------------------------------------------------------------


def test_helper_password_delete_error_con_credencial_presente_devuelve_fallo(harness: _Harness) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD},
        delete_error=keyring.errors.PasswordDeleteError("no se pudo borrar"),
    )

    assert sw._desactivar_search_key() is False
    assert "search_api_key" in esc.backend.gets


def test_helper_password_delete_error_con_ausencia_comprobada_devuelve_exito(harness: _Harness) -> None:
    esc = harness.build(
        stored={},
        delete_error=keyring.errors.PasswordDeleteError("nada que borrar"),
    )

    assert sw._desactivar_search_key() is True
    assert "search_api_key" in esc.backend.gets


def test_helper_fallo_de_lectura_posterior_no_declara_ausencia(harness: _Harness) -> None:
    esc = harness.build(
        stored={},
        get_errors={"search_api_key": OSError("lectura rota")},
    )

    assert sw._desactivar_search_key() is False
    assert "search_api_key" in esc.backend.gets


def test_helper_no_keyring_al_borrar_devuelve_fallo(harness: _Harness) -> None:
    esc = harness.build(delete_error=keyring.errors.NoKeyringError("sin backend"))

    assert sw._desactivar_search_key() is False
    assert "search_api_key" not in esc.backend.gets


def test_helper_no_keyring_al_leer_devuelve_fallo(harness: _Harness) -> None:
    esc = harness.build(get_error=keyring.errors.NoKeyringError("sin backend"))

    assert sw._search_key_ausente() is False
    assert "search_api_key" in esc.backend.gets


def test_helper_excepcion_inesperada_no_declara_ausencia(harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
    harness.build(delete_error=RuntimeError(f"detalle={SYN_LEAK}"))
    with caplog.at_level(0):
        assert sw._desactivar_search_key() is False
    assert SYN_LEAK not in caplog.text


# ---------------------------------------------------------------------------
# C3 / C4 — ninguna escritura opcional antes de confirmar la desactivación
# ---------------------------------------------------------------------------


def test_desactivacion_fallida_no_escribe_credenciales_ni_genera_ws(harness: _Harness) -> None:
    esc = harness.build(
        stored={
            "search_api_key": SYN_OLD,
            "nexus_api_key": SYN_NEXUS,
            "telegram_bot_token": SYN_TELEGRAM,
        },
        delete_errors={"search_api_key": keyring.errors.KeyringError("borrado roto")},
    )
    resultado = asyncio.run(
        esc.ejecutar(
            api_key=SYN_LLM,
            nexus_key=SYN_NEXUS_NEW,
            telegram_token=SYN_TELEGRAM_NEW,
            search_key="",
        )
    )

    assert resultado.completed == []
    assert resultado.negative
    assert resultado.backend.store.get("search_api_key") == SYN_OLD
    assert resultado.backend.store.get("nexus_api_key") == SYN_NEXUS
    assert resultado.backend.store.get("telegram_bot_token") == SYN_TELEGRAM
    assert "llm_api_key" not in resultado.backend.store
    assert "ollama_api_key" not in resultado.backend.store
    assert "ws_auth_token" not in resultado.backend.store
    assert _toml(resultado.config_path).get("first_run") is not False


def test_segunda_desactivacion_fallida_no_escribe_opcionales_y_recupera(
    harness: _Harness,
) -> None:
    esc = harness.build(
        stored={"search_api_key": SYN_OLD, "nexus_api_key": SYN_NEXUS},
        toml={"llm_provider": "ollama", "search_api_key": SYN_OLD},
        delete_error_from_call=(2, keyring.errors.KeyringError("segundo borrado roto")),
    )
    primero = asyncio.run(
        esc.ejecutar(
            api_key=SYN_LLM,
            nexus_key=SYN_NEXUS_NEW,
            telegram_token=SYN_TELEGRAM_NEW,
            search_key="",
        )
    )

    assert primero.completed == []
    assert any("desactivar" in msg for msg in primero.negative)
    assert primero.backend.store.get("nexus_api_key") == SYN_NEXUS
    assert "ollama_api_key" not in primero.backend.store
    assert "ws_auth_token" not in primero.backend.store

    esc.backend.delete_error_from_call = None
    segundo = asyncio.run(
        esc.ejecutar(
            api_key=SYN_LLM,
            nexus_key=SYN_NEXUS_NEW,
            telegram_token=SYN_TELEGRAM_NEW,
            search_key="",
        )
    )
    assert segundo.completed == [True]
    assert segundo.backend.store.get("nexus_api_key") == SYN_NEXUS_NEW
    assert segundo.backend.store.get("search_api_key") is None


def test_fallo_de_save_tras_escrituras_comunica_aplicacion_parcial(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    esc = harness.build(
        stored={},
        save_error=(1, RuntimeError(f"detalle={SYN_LEAK}")),
    )
    with caplog.at_level(0):
        resultado = asyncio.run(
            esc.ejecutar(
                api_key=SYN_LLM,
                nexus_key=SYN_NEXUS_NEW,
                telegram_token=SYN_TELEGRAM_NEW,
                search_key="",
            )
        )

    assert resultado.completed == []
    assert "parcialmente" in resultado.mensajes
    assert _toml(resultado.config_path).get("first_run") is not False
    assert resultado.backend.store.get("nexus_api_key") == SYN_NEXUS_NEW
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in resultado.mensajes


# ---------------------------------------------------------------------------
# C5 — first_run durable: la verificación precede a la persistencia del flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previo", "esperado"),
    [
        pytest.param(True, True, id="A-previo-true-explicito"),
        pytest.param(None, True, id="B-previo-ausente-default-true"),
        pytest.param(False, False, id="C-previo-false"),
    ],
)
def test_fallo_de_verificacion_con_compensacion_rota_no_publica_nuevo_first_run(
    harness: _Harness, previo: bool | None, esperado: bool
) -> None:
    base: dict[str, Any] = {"llm_provider": "ollama"}
    if previo is not None:
        base["first_run"] = previo
    esc = harness.build(
        stored={},
        toml=base,
        fail_search_get_after_save=2,
        save_error=(3, OSError("compensación rota")),
    )
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert resultado.negative
    assert _toml(resultado.config_path).get("first_run", True) is esperado
    assert resultado.store.values.get("first_run") is not False
    assert esc.save_control.llamadas == 2


def test_fallo_de_verificacion_no_intenta_guardado_compensatorio(harness: _Harness) -> None:
    esc = harness.build(stored={}, fail_search_get_after_save=2)
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == []
    assert esc.save_control.llamadas == 2
    assert _toml(resultado.config_path).get("first_run") is not False


def test_recuperacion_durable_tras_reparar_el_backend(harness: _Harness) -> None:
    esc = harness.build(
        stored={},
        fail_search_get_after_save=2,
        save_error=(3, OSError("compensación rota")),
    )
    primero = asyncio.run(esc.ejecutar(search_key=""))
    assert primero.completed == []
    assert _toml(primero.config_path).get("first_run") is not False

    esc.save_control.fallar_get = None
    esc.save_control.error_en_llamada = None
    esc.backend.get_errors.pop("search_api_key", None)
    segundo = asyncio.run(esc.ejecutar(search_key=""))

    assert segundo.completed == [True]
    assert _toml(segundo.config_path).get("first_run") is False
    assert segundo.backend.store.get("search_api_key") is None
    assert not Config(segundo.config_path)._data.get("search_api_key")


# ---------------------------------------------------------------------------
# C1 — R573: separar persistencia de finalización
# ---------------------------------------------------------------------------


def test_on_complete_falla_despues_de_persistir_y_no_dice_que_fallo_el_guardado(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    esc = harness.build(
        stored={},
        completion_error=RuntimeError(f"detalle={SYN_LEAK}"),
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert _toml(resultado.config_path).get("first_run") is False
    assert resultado.store.values.get("first_run") is False
    assert resultado.overlay.deleted is True
    assert resultado.timer.deactivated is True
    assert resultado.completed == [True]
    assert any("finalización" in msg for msg in resultado.warnings)
    assert "No se pudo guardar" not in resultado.mensajes
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in resultado.mensajes


def test_clear_drafts_falla_despues_de_persistir_y_finaliza_lo_demas(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    esc = harness.build(
        stored={},
        js_error=RuntimeError(f"detalle={SYN_LEAK}"),
    )
    with caplog.at_level(0):
        resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert _toml(resultado.config_path).get("first_run") is False
    assert resultado.store.values.get("first_run") is False
    assert resultado.overlay.deleted is True
    assert resultado.timer.deactivated is True
    assert resultado.completed == [True]
    assert any("borradores" in msg for msg in resultado.warnings)
    assert "No se pudo guardar" not in resultado.mensajes
    assert SYN_LEAK not in caplog.text
    assert SYN_LEAK not in resultado.mensajes


def test_exito_sin_fallos_de_finalizacion_no_agrega_advertencias(harness: _Harness) -> None:
    esc = harness.build(stored={})
    resultado = asyncio.run(esc.ejecutar(search_key=""))

    assert resultado.completed == [True]
    assert resultado.warnings == []
    assert resultado.negative == []
    assert _toml(resultado.config_path).get("first_run") is False
