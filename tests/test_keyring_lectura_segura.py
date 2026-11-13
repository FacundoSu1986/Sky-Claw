"""#575 §2: ``_safe_keyring_get`` distingue «no configurado» de «el backend falló».

Antes, cualquier excepción del backend (incluida una de programación) se convertía en
``None`` con el mismo mensaje que un secreto simplemente ausente. El contrato nuevo:

- secreto inexistente (el backend devuelve ``None``)  -> ``None``, sin warning.
- backend ausente (``NoKeyringError``)                -> ``None`` + WARNING propio.
- backend roto (``KeyringError`` / ``OSError``)       -> ``None`` + WARNING con la clase.
- cualquier otra excepción (p. ej. ``pywintypes.error``) -> ``None`` + ERROR, distinguible.

Los logs llevan SOLO el nombre de la clase: ni el mensaje ni el traceback, porque el
mensaje de un backend puede arrastrar datos sensibles.

La segunda mitad del archivo son anclas AST que enumeran la familia (no un caso a mano):
quién lee keyring dentro del paquete de tools, y qué ``try`` de producción envuelven
``keyring.get_password`` con un ``except Exception`` sin clasificar.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import keyring.errors
import pytest

from sky_claw.app.agent.tools import _safe_keyring_get

_MODULO = "sky_claw.app.agent.tools"
_CANARIO = "SECRETO-CANARIO-9f3a"


def _parchear_get_password(monkeypatch: pytest.MonkeyPatch, *, devuelve=None, lanza=None) -> None:
    def _falso(servicio: str, nombre: str) -> str | None:
        if lanza is not None:
            raise lanza
        return devuelve

    monkeypatch.setattr(f"{_MODULO}.keyring.get_password", _falso)


# ---------------------------------------------------------------------------
# Comportamiento
# ---------------------------------------------------------------------------


def test_devuelve_el_valor_cuando_el_secreto_existe(monkeypatch: pytest.MonkeyPatch) -> None:
    _parchear_get_password(monkeypatch, devuelve="clave-valida")

    assert _safe_keyring_get("nexus_api_key") == "clave-valida"


def test_secreto_inexistente_devuelve_none_sin_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _parchear_get_password(monkeypatch, devuelve=None)

    with caplog.at_level(logging.DEBUG, logger=_MODULO):
        resultado = _safe_keyring_get("nexus_api_key")

    assert resultado is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_backend_ausente_devuelve_none_con_warning_propio(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _parchear_get_password(monkeypatch, lanza=keyring.errors.NoKeyringError("sin backend"))

    with caplog.at_level(logging.DEBUG, logger=_MODULO):
        resultado = _safe_keyring_get("nexus_api_key")

    assert resultado is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
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
def test_backend_que_falla_devuelve_none_con_warning_que_nombra_la_clase(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    _parchear_get_password(monkeypatch, lanza=error)

    with caplog.at_level(logging.DEBUG, logger=_MODULO):
        resultado = _safe_keyring_get("nexus_api_key")

    assert resultado is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert type(error).__name__ in warnings[0].getMessage()
    assert "no hay backend" not in warnings[0].getMessage()


def test_excepcion_inesperada_degrada_a_none_pero_en_nivel_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BackendNativoError(Exception):
        """Simula ``pywintypes.error``: el backend de Windows lo relanza sin envolver."""

    _parchear_get_password(monkeypatch, lanza=BackendNativoError("detalle interno"))

    with caplog.at_level(logging.DEBUG, logger=_MODULO):
        resultado = _safe_keyring_get("nexus_api_key")

    assert resultado is None
    errores = [r for r in caplog.records if r.levelno >= logging.ERROR]
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
def test_los_logs_no_filtran_el_mensaje_ni_el_traceback_de_la_excepcion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    _parchear_get_password(monkeypatch, lanza=error)

    with caplog.at_level(logging.DEBUG, logger=_MODULO):
        _safe_keyring_get("nexus_api_key")

    assert _CANARIO not in caplog.text


# ---------------------------------------------------------------------------
# Anclas AST: la familia se enumera, no se muestrea
# ---------------------------------------------------------------------------

_RAIZ = Path(__file__).resolve().parent.parent
_PAQUETE_TOOLS = "sky_claw/app/agent/tools"


def _es_get_password(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "get_password"
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == "keyring"
    )


def _funcion_contenedora(arbol: ast.AST, objetivo: ast.AST) -> str:
    """Nombre de la función (def) más interna que contiene ``objetivo``."""
    mejor = "<modulo>"
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(h is objetivo for h in ast.walk(nodo)):
            mejor = nodo.name
    return mejor


def _nombre_de_excepcion(tipo: ast.expr | None) -> list[str]:
    if tipo is None:
        return ["<desnudo>"]
    elementos = tipo.elts if isinstance(tipo, ast.Tuple) else [tipo]
    nombres: list[str] = []
    for el in elementos:
        if isinstance(el, ast.Name):
            nombres.append(el.id)
        elif isinstance(el, ast.Attribute):
            nombres.append(el.attr)
    return nombres


def _try_sin_clasificar(fuente: str) -> list[str]:
    """Funciones cuyo ``try`` envuelve ``keyring.get_password`` con un ``except`` amplio
    (``Exception``/``BaseException``/desnudo) SIN un handler previo de ``KeyringError``."""
    arbol = ast.parse(fuente)
    encontradas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Try):
            continue
        if not any(_es_get_password(n) for stmt in nodo.body for n in ast.walk(stmt)):
            continue
        clasificado_antes = False
        for handler in nodo.handlers:
            nombres = _nombre_de_excepcion(handler.type)
            # Primero se evalúa lo amplio contra lo clasificado por handlers PREVIOS: una tupla
            # que mezcla ``KeyringError`` con ``Exception`` traga todo sin distinguir, así que
            # no puede clasificarse a sí misma (hallazgo de CodeRabbit en #928).
            if {"Exception", "BaseException", "<desnudo>"} & set(nombres) and not clasificado_antes:
                encontradas.append(_funcion_contenedora(arbol, nodo))
            if "KeyringError" in nombres or "NoKeyringError" in nombres:
                clasificado_antes = True
    return sorted(encontradas)


def _produccion() -> list[Path]:
    return sorted((_RAIZ / "sky_claw").rglob("*.py"))


@pytest.mark.parametrize(
    ("fuente", "esperadas"),
    [
        ("def f():\n    try:\n        keyring.get_password('s','n')\n    except Exception:\n        pass\n", ["f"]),
        ("def f():\n    try:\n        keyring.get_password('s','n')\n    except:\n        pass\n", ["f"]),
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except (keyring.errors.KeyringError, OSError):\n        pass\n",
            [],
        ),
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except keyring.errors.KeyringError:\n        pass\n"
            "    except Exception:\n        pass\n",
            [],
        ),
        ("def f():\n    try:\n        otra.cosa()\n    except Exception:\n        pass\n", []),
        # #575 §2 (hallazgo de CodeRabbit en #928): una tupla que MEZCLA una clase clasificada
        # con una amplia no clasifica nada. La clasificación solo vale si la estableció un
        # handler ANTERIOR, nunca el mismo que ya traga todo.
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except (keyring.errors.KeyringError, OSError, Exception):\n        pass\n",
            ["f"],
        ),
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except (keyring.errors.NoKeyringError, BaseException):\n        pass\n",
            ["f"],
        ),
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except keyring.errors.KeyringError:\n        pass\n"
            "    except (OSError, Exception):\n        pass\n",
            [],
        ),
        (
            "def f():\n    try:\n        keyring.get_password('s','n')\n"
            "    except Exception:\n        pass\n"
            "    except keyring.errors.KeyringError:\n        pass\n",
            ["f"],
        ),
    ],
)
def test_detector_de_try_sin_clasificar_sintetico(fuente: str, esperadas: list[str]) -> None:
    assert _try_sin_clasificar(fuente) == esperadas


#: Hermanos con el MISMO patrón que #575 §2 describe y que este PR NO toca (viven en
#: ``app_context.py`` y la GUI, fuera del write-set). Congelados por igualdad literal:
#: un ``except Exception`` nuevo sobre ``keyring.get_password`` rompe este test, y el
#: día que se clasifique un hermano hay que sacarlo de acá.
#:
#: Enumerados por AST, no a mano (la lista manual se había dejado ``_validate_and_save``).
#: ``save_settings`` aparece dos veces: son dos ``try`` distintos dentro de la misma función.
#: ``_migrate_and_get`` (``local_config.py``) es el hermano de forma MIXTA
#: ``(KeyringError, OSError, Exception)``: el detector original lo daba por clasificado.
_HERMANOS_PENDIENTES = {
    "sky_claw/app/gui/setup_wizard.py": ["_validate_and_save"],
    "sky_claw/app/gui/sky_claw_gui.py": ["_configured", "_on_check_updates", "save_settings", "save_settings"],
    "sky_claw/app_context.py": ["_is_configured", "_read_key"],
    "sky_claw/local/local_config.py": ["_migrate_and_get"],
}


def test_los_try_sin_clasificar_sobre_keyring_estan_congelados() -> None:
    encontrados: dict[str, list[str]] = {}
    for ruta in _produccion():
        funciones = _try_sin_clasificar(ruta.read_text(encoding="utf-8"))
        if funciones:
            encontrados[ruta.relative_to(_RAIZ).as_posix()] = funciones

    assert encontrados == _HERMANOS_PENDIENTES


def test_el_paquete_de_tools_solo_lee_keyring_dentro_de_safe_keyring_get() -> None:
    fuera: list[str] = []
    for ruta in sorted((_RAIZ / _PAQUETE_TOOLS).rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if _es_get_password(nodo) and _funcion_contenedora(arbol, nodo) != "_safe_keyring_get":
                fuera.append(f"{ruta.relative_to(_RAIZ).as_posix()}:{nodo.lineno}")  # type: ignore[attr-defined]

    assert fuera == []
