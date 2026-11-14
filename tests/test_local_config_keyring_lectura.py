"""#575 §2 (hermano): ``LocalConfig._migrate_and_get`` clasifica los fallos del keyring.

El mismo defecto que ``_safe_keyring_get`` tenía antes de #928: ``except (KeyringError, OSError,
Exception)`` trataba igual un secreto ausente, un backend roto y un bug, y además logueaba
``exc`` (el MENSAJE de la excepción del backend). El contrato, igual que en #928:

- secreto inexistente (el backend devuelve ``None``)  -> cae al legado, sin warning.
- backend ausente (``NoKeyringError``)                -> WARNING propio y cae al legado.
- backend roto (``KeyringError`` / ``OSError``)       -> WARNING con la CLASE y cae al legado.
- cualquier otra excepción (p. ej. ``pywintypes.error``) -> ERROR y cae al legado.

Siempre cae al legado: perder la lectura del keyring no puede dejar al usuario sin su clave si el
legado base64 sigue en el archivo. Los logs llevan SOLO el nombre de la clase, nunca el mensaje.
"""

from __future__ import annotations

import base64
import logging

import keyring.errors
import pytest

from sky_claw.local.local_config import LocalConfig

_LOGGER = "sky_claw.local.local_config"
_CANARIO = "SECRETO-CANARIO-77c1"
_LEGADO = "clave-del-legado"


def _con_legado() -> LocalConfig:
    cfg = LocalConfig()
    cfg.api_key_b64 = base64.b64encode(_LEGADO.encode()).decode()
    return cfg


class _Almacen:
    """``set_password`` que registra la migración al keyring (el camino feliz de la migración)."""

    def __init__(self) -> None:
        self.guardados: list[tuple[str, str]] = []

    def set_password(self, servicio: str, nombre: str, valor: str) -> None:
        self.guardados.append((nombre, valor))


def _parchear(monkeypatch: pytest.MonkeyPatch, *, get=None, get_lanza=None, set_lanza=None) -> _Almacen:
    almacen = _Almacen()

    def _get(servicio: str, nombre: str) -> str | None:
        if get_lanza is not None:
            raise get_lanza
        return get

    def _set(servicio: str, nombre: str, valor: str) -> None:
        if set_lanza is not None:
            raise set_lanza
        almacen.set_password(servicio, nombre, valor)

    monkeypatch.setattr("sky_claw.local.local_config.keyring.get_password", _get)
    monkeypatch.setattr("sky_claw.local.local_config.keyring.set_password", _set)
    return almacen


def _avisos(caplog: pytest.LogCaptureFixture, minimo: int = logging.WARNING) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= minimo]


def test_devuelve_el_valor_del_keyring_sin_tocar_el_legado(monkeypatch: pytest.MonkeyPatch) -> None:
    almacen = _parchear(monkeypatch, get="clave-del-keyring")

    assert _con_legado().get_api_key() == "clave-del-keyring"
    assert almacen.guardados == []


def test_secreto_inexistente_sin_legado_devuelve_none_sin_avisos(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _parchear(monkeypatch, get=None)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = LocalConfig().get_api_key()

    assert resultado is None
    assert _avisos(caplog) == []


def test_secreto_inexistente_migra_el_legado_al_keyring_sin_avisos(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    almacen = _parchear(monkeypatch, get=None)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = _con_legado().get_api_key()

    assert resultado == _LEGADO
    assert almacen.guardados == [("api_key", _LEGADO)]
    assert _avisos(caplog) == []


def test_backend_ausente_cae_al_legado_con_warning_propio(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _parchear(monkeypatch, get_lanza=keyring.errors.NoKeyringError("sin backend"))

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = _con_legado().get_api_key()

    assert resultado == _LEGADO
    warnings = [r for r in _avisos(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no hay backend" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "error",
    [
        keyring.errors.KeyringError("roto"),
        keyring.errors.KeyringLocked("bloqueado"),
        keyring.errors.InitError("no inicializa"),
        OSError("bus de mensajes caído"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_backend_que_falla_cae_al_legado_con_warning_que_nombra_la_clase(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    _parchear(monkeypatch, get_lanza=error)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = _con_legado().get_api_key()

    assert resultado == _LEGADO
    warnings = [r for r in _avisos(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert type(error).__name__ in warnings[0].getMessage()
    assert "no hay backend" not in warnings[0].getMessage()


def test_excepcion_inesperada_cae_al_legado_pero_en_nivel_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BackendNativoError(Exception):
        """Simula ``pywintypes.error``: el backend de Windows lo relanza sin envolver."""

    _parchear(monkeypatch, get_lanza=BackendNativoError("detalle interno"))

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = _con_legado().get_api_key()

    assert resultado == _LEGADO
    errores = [r for r in _avisos(caplog) if r.levelno >= logging.ERROR]
    assert len(errores) == 1
    assert "BackendNativoError" in errores[0].getMessage()


@pytest.mark.parametrize(
    "error",
    [
        keyring.errors.KeyringError(f"token={_CANARIO}"),
        OSError(f"fallo con {_CANARIO}"),
        RuntimeError(f"inesperado {_CANARIO}"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_los_logs_de_lectura_y_de_migracion_no_filtran_el_mensaje_de_la_excepcion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    # La lectura falla y, al migrar el legado, la escritura también: ambos caminos loguean.
    _parchear(monkeypatch, get_lanza=error, set_lanza=error)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        resultado = _con_legado().get_api_key()

    assert resultado == _LEGADO
    assert _CANARIO not in caplog.text
    assert len(_avisos(caplog)) == 2, "uno por la lectura y otro por la migración"
