"""Anclas del PERFIL AISLADO (``SKYCLAW_ISOLATED``): un proceso aislado no toca el keyring ni la máquina real.

Origen (R0, rig real, 2026-11-16/17): el arnés construyó ``AppContext`` con un ``HOME`` aislado, pero el keyring de Windows
es global al usuario: leyó el token de Telegram del operador y ejecutó long-polling (más de 700 llamadas, todas 401). Y el
zero-config buscó Skyrim en la máquina real, lo persistió en el ``config.toml`` aislado y lo admitió como raíz del sandbox.
Cada pieza tenía su propio acceso a la máquina real, así que el arreglo no puede ser un parche en un punto.

La propiedad, enunciada como propiedad del mecanismo: *el aislamiento solo protege si TODOS los accesos participan*.

* **keyring**: hay ~25 llamadas repartidas en 7 módulos y TODAS pasan por el backend global de ``keyring``. El interruptor
  instala UN backend que falla (``keyring.backends.fail``) y por construcción cubre todos los sitios, incluidos los futuros.
  Se elige el que FALLA y no el nulo: con el nulo ``set_password`` «funciona» sin guardar nada, y la migración de
  ``Config._load_from_keyring`` creería haber movido el secreto al keyring y lo BORRARÍA del TOML.
* **descubrimiento de la máquina real**: hay dos implementaciones (``AutoDetector`` y ``EnvironmentScanner``, con paridad
  anclada en ``test_paridad_deteccion_skyrim.py``); las dos respetan el interruptor.

Los censos de abajo enumeran en vez de muestrear: un módulo nuevo que toque keyring o lea el registro rompe el ancla hasta
que se decide su receta.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail, null

from sky_claw import aislamiento
from sky_claw.aislamiento import ENV_PERFIL_AISLADO, aplicar_aislamiento, perfil_aislado
from sky_claw.local.auto_detect import AutoDetector

RAIZ = Path(__file__).resolve().parents[1]
TOKEN_DEL_OPERADOR = "token-del-operador-que-NO-debe-leerse"


class _KeyringReal(KeyringBackend):
    """Backend «real» de mentira: guarda secretos del operador y registra CADA acceso (leer, escribir o borrar)."""

    priority = 1  # type: ignore[assignment]  # atributo de clase, como en los backends reales

    def __init__(self, secretos: dict[str, str]) -> None:
        super().__init__()
        self.secretos = dict(secretos)
        self.accesos: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.accesos.append(("get", username))
        return self.secretos.get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.accesos.append(("set", username))
        self.secretos[username] = password

    def delete_password(self, service: str, username: str) -> None:
        self.accesos.append(("delete", username))
        self.secretos.pop(username, None)


@pytest.fixture(autouse=True)
def _estado_global_restaurado(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """El backend de keyring es global al proceso: cada test lo restaura y parte sin el interruptor."""
    monkeypatch.delenv(ENV_PERFIL_AISLADO, raising=False)
    anterior = keyring.get_keyring()
    yield
    keyring.set_keyring(anterior)


@pytest.fixture
def keyring_del_operador() -> _KeyringReal:
    """Instala el backend «real» con el token de Telegram del operador y lo devuelve para inspeccionar sus accesos."""
    real = _KeyringReal({"telegram_bot_token": TOKEN_DEL_OPERADOR, "nexus_api_key": "nexus-del-operador"})
    keyring.set_keyring(real)
    return real


# ---------------------------------------------------------------------------
# El interruptor
# ---------------------------------------------------------------------------

#: Enumeración de lo que apaga y lo que enciende el interruptor. Un valor desconocido ENCIENDE (fail-closed): es un
#: interruptor de seguridad y un typo («ture») no puede dejar al proceso tocando la máquina real.
VALORES_DEL_INTERRUPTOR: list[tuple[str | None, bool]] = [
    (None, False),
    ("", False),
    ("   ", False),
    ("0", False),
    ("false", False),
    ("False", False),
    ("no", False),
    ("NO", False),
    ("off", False),
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("si", True),
    ("sí", True),
    ("yes", True),
    ("on", True),
    ("2", True),
    ("ture", True),
]


@pytest.mark.parametrize(("valor", "esperado"), VALORES_DEL_INTERRUPTOR)
def test_perfil_aislado_interpreta_cada_valor_y_un_valor_desconocido_enciende(
    valor: str | None, esperado: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if valor is not None:
        monkeypatch.setenv(ENV_PERFIL_AISLADO, valor)

    assert perfil_aislado() is esperado


def test_el_nombre_del_interruptor_es_estable() -> None:
    """Lo escriben el runbook del rig y los arneses: renombrarlo en silencio los dejaría sin aislar."""
    assert ENV_PERFIL_AISLADO == "SKYCLAW_ISOLATED"


# ---------------------------------------------------------------------------
# El backend que se instala
# ---------------------------------------------------------------------------


def test_con_el_perfil_aislado_el_backend_pasa_a_fallar_en_toda_operacion(
    monkeypatch: pytest.MonkeyPatch, keyring_del_operador: _KeyringReal
) -> None:
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert aplicar_aislamiento() is True

    assert isinstance(keyring.get_keyring(), fail.Keyring)
    for operacion, args in (
        (keyring.get_password, ("sky_claw", "telegram_bot_token")),
        (keyring.set_password, ("sky_claw", "telegram_bot_token", "x")),
        (keyring.delete_password, ("sky_claw", "telegram_bot_token")),
    ):
        with pytest.raises(keyring.errors.KeyringError):
            operacion(*args)
    assert keyring_del_operador.accesos == [], "el keyring «real» no debe recibir ni una llamada"


def test_sin_el_perfil_aislado_no_toca_el_backend(keyring_del_operador: _KeyringReal) -> None:
    assert aplicar_aislamiento() is False

    assert keyring.get_keyring() is keyring_del_operador


def test_aplicar_aislamiento_es_idempotente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert aplicar_aislamiento() is True
    assert aplicar_aislamiento() is True
    assert isinstance(keyring.get_keyring(), fail.Keyring)


def test_nunca_se_instala_el_backend_nulo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con el backend nulo ``set_password`` «funciona» sin guardar y la migración de Config borraría el secreto del TOML.

    El que se instala FALLA: el secreto que está en el archivo se queda ahí (contrato fail-closed de AGENTS.md).
    """
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    aplicar_aislamiento()

    assert not isinstance(keyring.get_keyring(), null.Keyring)


def test_el_secreto_en_el_toml_no_se_pierde_con_el_backend_que_falla_y_si_con_el_nulo(tmp_path: Path) -> None:
    """Por qué NO el nulo, demostrado: la misma config con un token en texto plano.

    ``Config`` migra el plaintext al keyring y lo borra del TOML SOLO si el keyring aceptó la escritura.
    """
    from sky_claw.config import Config

    def _config_con_token(carpeta: Path) -> Path:
        carpeta.mkdir()
        ruta = carpeta / "config.toml"
        ruta.write_text('telegram_bot_token = "token-en-el-archivo"\n', encoding="utf-8")
        return ruta

    keyring.set_keyring(null.Keyring())
    ruta_nulo = _config_con_token(tmp_path / "con_nulo")
    Config(ruta_nulo)
    assert "token-en-el-archivo" not in ruta_nulo.read_text(encoding="utf-8"), (
        "con el nulo la migración borra el secreto"
    )

    keyring.set_keyring(fail.Keyring())
    ruta_falla = _config_con_token(tmp_path / "con_falla")
    Config(ruta_falla)
    assert "token-en-el-archivo" in ruta_falla.read_text(encoding="utf-8"), "con el que falla el secreto se conserva"


# ---------------------------------------------------------------------------
# Los puntos de entrada: el interruptor se aplica ANTES de que alguien lea el keyring
# ---------------------------------------------------------------------------


def _backend_tras_importar_sky_claw(**entorno: str) -> str:
    """Módulo del backend de keyring que queda en un proceso NUEVO tras ``import sky_claw``."""
    env = {k: v for k, v in os.environ.items() if k not in {ENV_PERFIL_AISLADO, "PYTHON_KEYRING_BACKEND"}}
    env["PYTHONPATH"] = str(RAIZ)
    # Partimos de un backend que NO es el de falla (el nulo), para distinguir «no tocó» de «dejó el de falla».
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    env.update(entorno)
    resultado = subprocess.run(  # noqa: S603 -- ejecuta el intérprete actual sobre un snippet fijo
        [sys.executable, "-c", "import sky_claw, keyring; print(type(keyring.get_keyring()).__module__)"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    return resultado.stdout.strip().splitlines()[-1]


def test_importar_sky_claw_con_el_interruptor_instala_el_backend_que_falla() -> None:
    assert _backend_tras_importar_sky_claw(**{ENV_PERFIL_AISLADO: "1"}) == "keyring.backends.fail"


@pytest.mark.parametrize("valor", [None, "0", "false"])
def test_importar_sky_claw_sin_el_interruptor_no_toca_el_backend(valor: str | None) -> None:
    entorno = {} if valor is None else {ENV_PERFIL_AISLADO: valor}

    assert _backend_tras_importar_sky_claw(**entorno) == "keyring.backends.null"


def _importar_sky_claw_con_platform_prohibido(**entorno: str) -> subprocess.CompletedProcess[str]:
    """Importa ``sky_claw`` en un proceso NUEVO donde ``platform.system()`` está prohibido.

    En Windows la primera llamada a ``platform.system()`` lanza ``cmd.exe /c ver``, y ``import keyring`` la hace a nivel
    de módulo (``keyring/util/platform_.py``). Es el subproceso que el worker VFS no puede crear antes de probar que
    vive en el Job (``vfs_arranque.py``, H-02): ``sky_claw/__main__.py`` importa el paquete ANTES de despachar a
    ``arrancar_worker``, así que lo que haga ``sky_claw/__init__.py`` corre sin contención probada.
    """
    env = {k: v for k, v in os.environ.items() if k not in {ENV_PERFIL_AISLADO, "PYTHON_KEYRING_BACKEND"}}
    env["PYTHONPATH"] = str(RAIZ)
    env.update(entorno)
    snippet = (
        "import platform, sys\n"
        "def _prohibido(*a, **k):\n"
        "    raise AssertionError('platform.system() llamado al importar sky_claw')\n"
        "platform.system = _prohibido\n"
        "import sky_claw\n"
        "assert 'keyring' not in sys.modules, 'import sky_claw importo keyring'\n"
        "print('ok')\n"
    )
    return subprocess.run(  # noqa: S603 -- ejecuta el intérprete actual sobre un snippet fijo
        [sys.executable, "-c", snippet], capture_output=True, text=True, env=env, timeout=120, check=False
    )


@pytest.mark.parametrize("valor", [None, "1"], ids=["sin_interruptor_control", "con_interruptor"])
def test_importar_sky_claw_no_importa_keyring_ni_llama_a_platform_system(valor: str | None) -> None:
    """El worker VFS prueba su Job ANTES de importar nada pesado; el aislamiento no puede romper esa propiedad.

    Con el interruptor el hook del paquete NO puede importar ``keyring`` (lo haría ``set_keyring``): fija el backend por
    la variable de entorno que ``keyring`` lee en su primer uso. El control sin interruptor prueba que el test mide el
    paquete y no el entorno."""
    entorno = {} if valor is None else {ENV_PERFIL_AISLADO: valor}

    resultado = _importar_sky_claw_con_platform_prohibido(**entorno)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert resultado.stdout.strip().splitlines()[-1] == "ok"


def test_preparar_aislamiento_fija_el_backend_que_falla_sin_importar_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

    assert aislamiento.preparar_aislamiento() is True
    # El perfil aislado gana sobre un backend que el arnés haya fijado antes: igual que `aplicar_aislamiento`.
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"


@pytest.mark.parametrize("valor", [None, "", "0", "false"])
def test_preparar_aislamiento_sin_el_interruptor_no_toca_el_entorno(
    valor: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if valor is None:
        monkeypatch.delenv(ENV_PERFIL_AISLADO, raising=False)
    else:
        monkeypatch.setenv(ENV_PERFIL_AISLADO, valor)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

    assert aislamiento.preparar_aislamiento() is False
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def test_config_aplica_el_aislamiento_antes_de_leer_el_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyring_del_operador: _KeyringReal
) -> None:
    """El caso del rig: el interruptor se activa DESPUÉS de importar (el arnés lo setea en su script)."""
    from sky_claw.config import Config

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    config = Config(tmp_path / "config.toml")

    assert config.telegram_bot_token == "", "el token del keyring del operador no puede llegar a un perfil aislado"
    assert keyring_del_operador.accesos == []


def test_sin_aislamiento_config_si_lee_el_keyring_control(tmp_path: Path, keyring_del_operador: _KeyringReal) -> None:
    """Control: sin el interruptor el mismo test LEE el token. Prueba que el test anterior tiene dientes."""
    from sky_claw.config import Config

    config = Config(tmp_path / "config.toml")

    assert config.telegram_bot_token == TOKEN_DEL_OPERADOR
    assert keyring_del_operador.accesos != []


def test_app_context_aplica_el_aislamiento_al_construirse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keyring_del_operador: _KeyringReal
) -> None:
    from sky_claw.app_context import AppContext

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    args = SimpleNamespace(
        db_path=str(tmp_path / "test.db"),
        mo2_root=tmp_path / "MO2",
        staging_dir=tmp_path / "staging",
        provider="ollama",
        operator_chat_id=None,
        loot_exe=None,
        install_dir=None,
        mode="cli",
    )

    AppContext(args)

    assert isinstance(keyring.get_keyring(), fail.Keyring)
    assert keyring_del_operador.accesos == []


# ---------------------------------------------------------------------------
# «Todos los accesos participan»: tras el aislamiento, cada lector del keyring recibe «no configurado»
# ---------------------------------------------------------------------------


def test_cada_lector_de_secretos_ve_no_configurado_y_ninguno_llega_al_keyring_real(
    monkeypatch: pytest.MonkeyPatch, keyring_del_operador: _KeyringReal
) -> None:
    from sky_claw.app.agent.tools import _safe_keyring_get
    from sky_claw.local.local_config import LocalConfig

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    aplicar_aislamiento()

    assert _safe_keyring_get("telegram_bot_token") is None
    assert _safe_keyring_get("nexus_api_key") is None
    local = LocalConfig()
    assert local.get_telegram_bot_token() is None
    assert local.get_nexus_api_key() is None
    assert keyring_del_operador.accesos == []


async def test_el_bridge_del_frontend_trata_el_backend_ausente_como_token_no_configurado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermano que el censo de abajo cazó: ``_authenticate`` leía el keyring SIN ``try`` y, con un backend que falla,
    escapaba ``NoKeyringError`` en vez del ``AuthError`` documentado («WS_AUTH_TOKEN no configurado»)."""
    from sky_claw.app.comms.frontend_bridge import AuthError, FrontendBridge

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    aplicar_aislamiento()
    puente = FrontendBridge(router=MagicMock(), session=MagicMock(), config=MagicMock(), app_context=MagicMock())
    ws = AsyncMock()

    with pytest.raises(AuthError, match="no configurado"):
        await puente._authenticate(ws)

    ws.send.assert_not_awaited()


def _puente_con_keyring_que_falla(error: BaseException) -> Any:
    """Un ``FrontendBridge`` cuyo cliente de keyring (inyectable) levanta ``error`` al leer el token."""
    from sky_claw.app.comms.frontend_bridge import FrontendBridge

    cliente = MagicMock()
    cliente.get_password.side_effect = error
    return FrontendBridge(
        router=MagicMock(), session=MagicMock(), config=MagicMock(), app_context=MagicMock(), keyring_client=cliente
    )


@pytest.mark.parametrize("clase", [keyring.errors.KeyringError, keyring.errors.NoKeyringError, OSError])
async def test_el_bridge_trata_la_jerarquia_del_keyring_como_token_no_configurado(clase: type[Exception]) -> None:
    """Lo que el resto del producto llama «backend ausente o caído» (``config.py``: ``KeyringError`` y ``OSError``)
    se degrada al ``AuthError`` documentado, y el token no sale por el websocket."""
    from sky_claw.app.comms.frontend_bridge import AuthError

    puente = _puente_con_keyring_que_falla(clase("backend caído"))
    ws = AsyncMock()

    with pytest.raises(AuthError, match="no configurado"):
        await puente._authenticate(ws)

    ws.send.assert_not_awaited()


@pytest.mark.parametrize("clase", [RuntimeError, AttributeError, TypeError])
async def test_el_bridge_no_traga_un_error_de_programacion_como_token_no_configurado(clase: type[Exception]) -> None:
    """Un bug NO es «token no configurado». ``_authenticate`` corre dentro del bucle de reconexión, que ya es el borde
    que registra lo inesperado con su traza completa: tragarlo acá lo dejaría en un warning con solo el nombre de la
    clase. Falla cerrado igual (no se envía el token) y propaga para que se vea."""
    puente = _puente_con_keyring_que_falla(clase("bug"))
    ws = AsyncMock()

    with pytest.raises(clase):
        await puente._authenticate(ws)

    ws.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Censos por AST: enumeran la familia (un sitio nuevo rompe el ancla hasta que se decide su receta)
# ---------------------------------------------------------------------------

LLAMADAS_AL_KEYRING = {"get_password", "set_password", "delete_password"}
#: Captura que SOBREVIVE a un backend que falla: ``NoKeyringError`` es ``KeyringError`` pero NO ``OSError``.
CAPTURAS_QUE_SOBREVIVEN = {"Exception", "BaseException", "KeyringError", "NoKeyringError"}


def _arboles(*carpetas: str) -> Iterator[tuple[str, ast.Module]]:
    """Todo ``.py`` de las carpetas, parseado. Un archivo que no parsea FALLA el test (no se descarta en silencio)."""
    for carpeta in carpetas:
        for ruta in sorted((RAIZ / carpeta).rglob("*.py")):
            yield ruta.relative_to(RAIZ).as_posix(), ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _modulos_que_importan(prefijo: str) -> set[str]:
    encontrados = set()
    for ruta, arbol in _arboles("sky_claw", "scripts"):
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                nombres = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
                nombres = [nodo.module]
            else:
                continue
            if any(n == prefijo or n.startswith(prefijo + ".") for n in nombres):
                encontrados.add(ruta)
    return encontrados


#: Módulos que importan ``keyring``. Todos pasan por el backend GLOBAL: por eso el aislamiento los cubre a todos. Un
#: módulo nuevo rompe esto: hay que confirmar que usa el módulo ``keyring`` (y no un backend propio) y sumarlo acá.
MODULOS_QUE_TOCAN_KEYRING = {
    "sky_claw/aislamiento.py",
    "sky_claw/app/comms/frontend_bridge.py",
    "sky_claw/app/gui/setup_wizard.py",
    "sky_claw/app/gui/sky_claw_gui.py",
    "sky_claw/app/security/keyring_reader.py",
    "sky_claw/config.py",
    "sky_claw/local/local_config.py",
}


def test_el_censo_de_modulos_que_tocan_keyring_esta_congelado() -> None:
    assert _modulos_que_importan("keyring") == MODULOS_QUE_TOCAN_KEYRING


def test_ningun_modulo_fuera_del_interruptor_instancia_un_backend_ni_usa_otro_almacen_de_secretos() -> None:
    """Un backend instanciado a mano (``keyring.backends.Windows.WinVaultKeyring()``) o una API de credenciales de
    Windows se salta el backend global y con él el aislamiento. Solo ``aislamiento.py`` puede nombrar backends."""
    assert _modulos_que_importan("keyring.backends") <= {"sky_claw/aislamiento.py"}
    for almacen in ("win32cred", "win32credential"):
        assert _modulos_que_importan(almacen) == set(), f"{almacen} se salta el backend global de keyring"
    atributos_de_backend = []
    for ruta, arbol in _arboles("sky_claw", "scripts"):
        if ruta == "sky_claw/aislamiento.py":
            continue
        atributos_de_backend += [
            ruta
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Attribute) and "backends" in ast.unparse(nodo) and "keyring" in ast.unparse(nodo)
        ]
    assert atributos_de_backend == []


def _nombre(nodo: ast.expr) -> str | None:
    if isinstance(nodo, ast.Attribute):
        return nodo.attr
    if isinstance(nodo, ast.Name):
        return nodo.id
    return None


def _capturas_del_handler(handler: ast.ExceptHandler) -> set[str | None]:
    if handler.type is None:
        return {"Exception"}  # ``except:`` desnudo
    if isinstance(handler.type, ast.Tuple):
        return {_nombre(e) for e in handler.type.elts}
    return {_nombre(handler.type)}


def _llamadas_a_keyring_sin_proteger() -> tuple[int, list[str]]:
    """(total de llamadas get/set/delete_password analizadas, las que NO están dentro de una captura que sobreviva)."""
    total, sin_proteger = 0, []
    for ruta, arbol in _arboles("sky_claw"):
        for padre in ast.walk(arbol):
            for hijo in ast.iter_child_nodes(padre):
                hijo._padre = padre  # type: ignore[attr-defined]
        for nodo in ast.walk(arbol):
            if not (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr in LLAMADAS_AL_KEYRING
                and "keyring" in ast.unparse(nodo.func.value)
            ):
                continue
            total += 1
            protegido, actual = False, nodo
            while hasattr(actual, "_padre") and not protegido:
                actual = actual._padre
                if isinstance(actual, ast.Try):
                    protegido = any(_capturas_del_handler(h) & CAPTURAS_QUE_SOBREVIVEN for h in actual.handlers)
                elif isinstance(actual, ast.With):
                    protegido = any(
                        isinstance(item.context_expr, ast.Call)
                        and _nombre(item.context_expr.func) == "suppress"
                        and any(_nombre(a) in CAPTURAS_QUE_SOBREVIVEN for a in item.context_expr.args)
                        for item in actual.items
                    )
            if not protegido:
                sin_proteger.append(f"{ruta}:{nodo.lineno} {nodo.func.attr}")
    return total, sin_proteger


def test_toda_llamada_al_keyring_sobrevive_a_un_backend_que_falla() -> None:
    """El estado que impone el aislamiento (y el de un Linux headless sin backend) no puede tumbar a ningún sitio."""
    total, sin_proteger = _llamadas_a_keyring_sin_proteger()

    assert total >= 20, f"el análisis solo vio {total} llamadas: ¿cambió la forma de llamar al keyring?"
    assert sin_proteger == [], f"llamadas sin captura de KeyringError/Exception: {sin_proteger}"


#: Módulos que leen el REGISTRO de Windows, con su receta. Los «guardado» respetan el interruptor (se prueban abajo); el
#: «exento» queda a propósito, con su razón: este censo obliga a decidirlo para un lector nuevo.
LECTORES_DEL_REGISTRO = {
    "sky_claw/local/auto_detect.py": "guardado",
    "sky_claw/local/discovery/scanner.py": "guardado",
    # BodySlide PR 2b (brief §13 P-4): MachineGuid de solo lectura para la identidad del journal;
    # con el perfil aislado no se lee y la recuperación queda en HOLD_IDENTIDAD.
    "sky_claw/local/tools/bodyslide_journal.py": "guardado",
    "sky_claw/local/frozen_runtime/effective_runtime/mo2_instance.py": (
        "exento: lectura de solo lectura (KEY_READ) de HKCU CurrentInstance; es la identidad de la instancia de MO2 que "
        "el runtime congelado compara, no descubre ni persiste rutas. PREGUNTA ABIERTA: ¿debe aislarse también?"
    ),
}


def test_el_censo_de_lectores_del_registro_esta_congelado() -> None:
    assert _modulos_que_importan("winreg") == set(LECTORES_DEL_REGISTRO)


def test_los_lectores_del_registro_guardados_importan_el_interruptor() -> None:
    for ruta, receta in LECTORES_DEL_REGISTRO.items():
        if receta != "guardado":
            continue
        arbol = ast.parse((RAIZ / ruta).read_text(encoding="utf-8"))
        importa = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "sky_claw.aislamiento"
            and any(a.name == "perfil_aislado" for a in n.names)
            for n in ast.walk(arbol)
        )
        assert importa, f"{ruta} lee el registro y dice estar guardado, pero no importa perfil_aislado"


# ---------------------------------------------------------------------------
# El descubrimiento de la máquina real, por AMBAS implementaciones
# ---------------------------------------------------------------------------

#: Los finders públicos de ``AutoDetector``, enumerados por introspección y congelados: un finder nuevo rompe esto hasta
#: que respete el interruptor.
FINDERS_DE_AUTODETECTOR = ["find_loot", "find_mo2", "find_skyrim", "find_xedit"]


def test_los_finders_de_autodetector_estan_enumerados() -> None:
    encontrados = sorted(
        nombre
        for nombre, valor in inspect.getmembers(AutoDetector)
        if nombre.startswith("find_") and inspect.iscoroutinefunction(valor)
    )

    assert encontrados == FINDERS_DE_AUTODETECTOR


@pytest.mark.parametrize("finder", FINDERS_DE_AUTODETECTOR)
async def test_con_el_perfil_aislado_cada_finder_de_autodetector_no_toca_la_maquina_real(
    finder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _toco_la_maquina_real() -> Path:
        msg = f"{finder} tocó la máquina real con el perfil aislado"
        raise AssertionError(msg)

    monkeypatch.setattr(AutoDetector, f"_{finder}_inner", staticmethod(_toco_la_maquina_real))
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert await getattr(AutoDetector, finder)() is None


@pytest.mark.parametrize("finder", FINDERS_DE_AUTODETECTOR)
async def test_sin_el_perfil_aislado_cada_finder_de_autodetector_si_busca_control(
    finder: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: sin el interruptor el finder llega al detector real (aquí, uno que devuelve un canario)."""
    monkeypatch.setattr(AutoDetector, f"_{finder}_inner", staticmethod(lambda: tmp_path))

    assert await getattr(AutoDetector, finder)() == tmp_path


async def test_detect_all_con_el_perfil_aislado_no_devuelve_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert await AutoDetector.detect_all() == {
        "mo2_root": None,
        "skyrim_path": None,
        "loot_exe": None,
        "xedit_exe": None,
    }


@pytest.fixture
def maquina_real_simulada(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Una «máquina real» con Skyrim y MO2 instalados, alcanzable por el registro y por las rutas comunes."""
    from sky_claw.local.discovery import scanner

    skyrim = tmp_path / "SkyrimReal"
    skyrim.mkdir()
    (skyrim / "SkyrimSE.exe").write_bytes(b"")
    mo2 = tmp_path / "MO2Real"
    mo2.mkdir()
    (mo2 / "ModOrganizer.exe").write_bytes(b"")
    monkeypatch.setattr(scanner, "_reg_read", lambda *_a, **_k: str(skyrim))
    monkeypatch.setattr(scanner, "_parse_steam_libraries", lambda: [])
    monkeypatch.setattr(scanner, "SKYRIM_COMMON_PATHS", (str(skyrim),))
    monkeypatch.setattr(scanner, "MO2_COMMON_PATHS", (str(mo2),))
    return SimpleNamespace(skyrim=skyrim, mo2=mo2)


async def test_el_scanner_encuentra_la_maquina_real_sin_el_perfil_aislado_control(
    maquina_real_simulada: SimpleNamespace,
) -> None:
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    scanner = EnvironmentScanner()

    assert await scanner._find_skyrim() == maquina_real_simulada.skyrim
    assert await scanner._find_mo2() == maquina_real_simulada.mo2


async def test_con_el_perfil_aislado_el_scanner_no_descubre_la_maquina_real(
    maquina_real_simulada: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    scanner = EnvironmentScanner()

    assert await scanner._find_skyrim() is None
    assert await scanner._find_mo2() is None


async def test_con_el_perfil_aislado_la_ruta_configurada_explicitamente_sigue_valiendo(
    maquina_real_simulada: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """El aislamiento apaga el DESCUBRIMIENTO, no la configuración: lo que el operador del perfil fijó se respeta."""
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    fijado = tmp_path / "SkyrimDelPerfil"
    fijado.mkdir()
    (fijado / "SkyrimSE.exe").write_bytes(b"")
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert await EnvironmentScanner(skyrim_path=fijado)._find_skyrim() == fijado


# ---------------------------------------------------------------------------
# Las HERRAMIENTAS externas también son de la máquina real (hallazgo de Codex): se enumera el registro entero
# ---------------------------------------------------------------------------

#: Las herramientas con su propio detector (no pasan por el resolutor genérico ``_resolve_tool_path``).
_HERRAMIENTAS_CON_DETECTOR_PROPIO = frozenset({"skse", "community_shaders"})


def _herramientas_del_resolutor_generico() -> list[Any]:
    """Todas las del registro que resuelve ``_resolve_tool_path``: la familia que el aislamiento tiene que cubrir."""
    from sky_claw.local.discovery import scanner

    return [
        pytest.param(spec, id=spec.key)
        for spec in scanner.iter_external_tool_specs()
        if spec.key not in _HERRAMIENTAS_CON_DETECTOR_PROPIO
    ]


@pytest.fixture
def herramientas_del_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Una máquina con TODAS las herramientas externas en una raíz común (``COMMON_TOOL_ROOTS``), sin ``PATH``."""
    from sky_claw.local.discovery import scanner

    raiz = tmp_path / "ModdingDelHost"
    raiz.mkdir()
    for spec in scanner.iter_external_tool_specs():
        for nombre in spec.exe_names:
            (raiz / nombre).write_bytes(b"")
    monkeypatch.setattr(scanner, "COMMON_TOOL_ROOTS", (str(raiz),))
    monkeypatch.setattr(scanner.shutil, "which", lambda *_a, **_k: None)
    return SimpleNamespace(raiz=raiz)


@pytest.mark.parametrize("spec", _herramientas_del_resolutor_generico())
def test_el_scanner_encuentra_las_herramientas_del_host_sin_el_perfil_aislado_control(
    spec: Any, herramientas_del_host: SimpleNamespace
) -> None:
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    scanner = EnvironmentScanner()

    encontrada = scanner._resolve_tool_path(spec.key, spec.exe_names, scanner._build_search_roots(None, None))

    assert encontrada is not None and encontrada.parent == herramientas_del_host.raiz


@pytest.mark.parametrize("spec", _herramientas_del_resolutor_generico())
def test_con_el_perfil_aislado_el_scanner_no_descubre_las_herramientas_del_host(
    spec: Any, herramientas_del_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    scanner = EnvironmentScanner()

    assert scanner._resolve_tool_path(spec.key, spec.exe_names, scanner._build_search_roots(None, None)) is None


@pytest.mark.parametrize("aislado", [False, True], ids=["control_sin_aislamiento", "aislado"])
def test_el_scanner_busca_las_herramientas_en_el_path_solo_sin_el_perfil_aislado(
    aislado: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El ``PATH`` del arnés es el de la máquina (el rig necesita los directorios del sistema): también es el host."""
    from sky_claw.local.discovery import scanner as modulo
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    en_path = tmp_path / "EnPath"
    en_path.mkdir()
    exe = en_path / "LOOT.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(modulo, "COMMON_TOOL_ROOTS", ())
    monkeypatch.setattr(modulo.shutil, "which", lambda *_a, **_k: str(exe))
    if aislado:
        monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    encontrada = EnvironmentScanner()._find_tool(("LOOT.exe",), [])

    assert encontrada == (None if aislado else exe)


def test_con_el_perfil_aislado_un_ejecutable_de_herramienta_configurado_sigue_valiendo(
    herramientas_del_host: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Se apaga el DESCUBRIMIENTO, no la configuración: el ``SSEEdit.exe`` que fijó el perfil gana, el del host no."""
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    fijado = tmp_path / "PerfilAislado" / "SSEEdit.exe"
    fijado.parent.mkdir()
    fijado.write_bytes(b"")
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    scanner = EnvironmentScanner(tool_paths={"xedit": str(fijado)})

    resuelta = scanner._resolve_tool_path("xedit", ("SSEEdit.exe",), scanner._build_search_roots(None, None))

    assert resuelta == fijado


async def test_un_escaneo_aislado_con_xedit_fijado_no_descubre_ni_ejecuta_herramientas_del_host(
    maquina_real_simulada: SimpleNamespace,
    herramientas_del_host: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El escenario que reprodujo Codex: solo xEdit fijado, un LOOT ajeno en el host. Ni se lo descubre ni se lo
    ejecuta (la sonda de versión ejecuta el binario), y la GUI no exporta una ruta del host al despacho de rituales."""
    from sky_claw.local.discovery import scanner as modulo
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    juego = tmp_path / "SkyrimDelPerfil"
    juego.mkdir()
    (juego / "SkyrimSE.exe").write_bytes(b"")
    xedit = tmp_path / "PerfilAislado" / "SSEEdit.exe"
    xedit.parent.mkdir()
    xedit.write_bytes(b"")
    sondeadas: list[Path] = []

    async def _sonda(_clave: str, ruta: Path, **_k: Any) -> str:
        sondeadas.append(Path(ruta))
        return ""

    monkeypatch.setattr(modulo, "detect_tool_version", _sonda)
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    snap = await EnvironmentScanner(skyrim_path=juego, tool_paths={"xedit": str(xedit)})._scan_inner()

    assert "loot" not in snap.tools
    assert all(ruta.parent != herramientas_del_host.raiz for ruta in sondeadas), sondeadas
    assert all(info.exe_path.parent != herramientas_del_host.raiz for info in snap.tools.values()), snap.tools
    # El ejecutable que el perfil fijó SÍ se reporta: se apagó el descubrimiento, no la configuración.
    assert snap.tools["xedit"].exe_path == xedit


# ---------------------------------------------------------------------------
# «Fail-closed en las dos direcciones»: el backend que falla no puede costar el secreto legado (hallazgo de Codex)
# ---------------------------------------------------------------------------

TOKEN_LEGADO = "token-del-json-legado"


def _json_legado_con_token(directorio: Path) -> Path:
    """Un ``sky_claw_config.json`` legado (base64) con un token de Telegram, donde ``AppContext`` lo busca (cwd)."""
    import base64
    import json

    ruta = directorio / "sky_claw_config.json"
    ruta.write_text(
        json.dumps({"telegram_bot_token_b64": base64.b64encode(TOKEN_LEGADO.encode()).decode()}), encoding="utf-8"
    )
    return ruta


def _migrar_json_legado(config_path: Path) -> None:
    """Ejecuta ``AppContext._migrate_legacy_json`` de verdad (los demás tests lo reemplazan por un ``MagicMock``)."""
    from sky_claw.app_context import AppContext

    AppContext._migrate_legacy_json(SimpleNamespace(config_path=config_path))  # type: ignore[arg-type]


def test_con_el_perfil_aislado_la_migracion_legada_conserva_el_json_porque_nada_guardo_el_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el backend que falla ``Config.save()`` se NIEGA a bajar el token al TOML (a propósito: contrato fail-closed).
    Borrar entonces el JSON dejaría el token en ningún lado: perderlo es tan malo como filtrarlo."""
    monkeypatch.chdir(tmp_path)
    json_legado = _json_legado_con_token(tmp_path)
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    aplicar_aislamiento()

    _migrar_json_legado(config_path)

    texto_toml = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    assert json_legado.exists(), "el JSON legado era la única copia del token"
    assert TOKEN_LEGADO not in texto_toml, "el token no puede bajar en plano al TOML"


def test_sin_aislamiento_la_migracion_legada_guarda_el_token_en_el_keyring_y_purga_el_json_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: con un keyring que SÍ guarda, la migración sigue purgando el JSON. Prueba que el guardado de arriba es
    por el secreto pendiente y no un «nunca borrar»."""
    monkeypatch.chdir(tmp_path)
    json_legado = _json_legado_con_token(tmp_path)
    real = _KeyringReal({})
    keyring.set_keyring(real)

    _migrar_json_legado(tmp_path / "config.toml")

    assert not json_legado.exists()
    assert real.secretos["telegram_bot_token"] == TOKEN_LEGADO


def test_la_migracion_legada_se_completa_cuando_el_keyring_vuelve_a_funcionar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Una escritura rechazada queda pendiente para reintento»: conservar el JSON es lo que hace posible el reintento."""
    monkeypatch.chdir(tmp_path)
    json_legado = _json_legado_con_token(tmp_path)
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")
    aplicar_aislamiento()
    _migrar_json_legado(config_path)
    assert json_legado.exists()

    monkeypatch.delenv(ENV_PERFIL_AISLADO)
    real = _KeyringReal({})
    keyring.set_keyring(real)
    _migrar_json_legado(config_path)

    assert not json_legado.exists()
    assert real.secretos["telegram_bot_token"] == TOKEN_LEGADO


# ---------------------------------------------------------------------------
# El escaneo de rutas comunes del ARRANQUE (LOOT y xEdit) es de la máquina real (hallazgo de CodeRabbit)
# ---------------------------------------------------------------------------


def _referencias_a(nombre: str) -> set[tuple[str, str]]:
    """``(archivo, función que la contiene)`` de TODA referencia a ``nombre`` en ``sky_claw/``: llamada, pasado como
    valor (``functools.partial(nombre, ...)``) o atributo. No cuenta el import ni la definición."""
    encontradas: set[tuple[str, str]] = set()

    def visitar(nodo: ast.AST, archivo: str, funcion: str) -> None:
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
            funcion = nodo.name
        if isinstance(nodo, ast.Name) and nodo.id == nombre and isinstance(nodo.ctx, ast.Load):
            encontradas.add((archivo, funcion))
        if isinstance(nodo, ast.Attribute) and nodo.attr == nombre:
            encontradas.add((archivo, funcion))
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo, archivo, funcion)

    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        visitar(ast.parse(ruta.read_text(encoding="utf-8")), ruta.relative_to(RAIZ).as_posix(), "<módulo>")
    return encontradas


async def test_con_el_perfil_aislado_el_arranque_no_escanea_las_rutas_comunes_del_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start_full`` busca LOOT y xEdit en Program Files y las raíces de modding y PERSISTE lo que encuentra en la
    config del perfil (``escribir_campo``): con el perfil aislado terminaría con una ruta del host escrita."""
    from sky_claw.app_context import AppContext

    (tmp_path / "loot.exe").write_bytes(b"")
    monkeypatch.setenv(ENV_PERFIL_AISLADO, "1")

    assert await AppContext._scan_tool_paths(SimpleNamespace(), (tmp_path,), "loot.exe") is None  # type: ignore[arg-type]


async def test_sin_aislamiento_el_arranque_escanea_las_rutas_comunes_control(tmp_path: Path) -> None:
    """Control: sin el interruptor el mismo escaneo SÍ encuentra el ejecutable. Prueba que el test anterior mide el
    escaneo y no un directorio vacío."""
    from sky_claw.app_context import AppContext

    (tmp_path / "loot.exe").write_bytes(b"")

    encontrado = await AppContext._scan_tool_paths(SimpleNamespace(), (tmp_path,), "loot.exe")  # type: ignore[arg-type]

    assert encontrado == tmp_path / "loot.exe"


def test_toda_referencia_a_scan_common_paths_pasa_por_el_unico_punto_guardado() -> None:
    """Enumera, no muestrea: el escaneo de rutas comunes tiene UN solo llamador y es el que consulta el interruptor.
    Un llamador nuevo (otra superficie que persista rutas del host) rompe el ancla hasta que se decide su receta."""
    assert _referencias_a("scan_common_paths") == {("sky_claw/app_context.py", "_scan_tool_paths")}


def test_scan_tool_paths_consulta_el_interruptor_antes_de_escanear() -> None:
    arbol = ast.parse((RAIZ / "sky_claw" / "app_context.py").read_text(encoding="utf-8"))
    metodo = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef | ast.FunctionDef) and nodo.name == "_scan_tool_paths"
    )
    consultas = [
        n.lineno
        for n in ast.walk(metodo)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "perfil_aislado"
    ]
    escaneos = [n.lineno for n in ast.walk(metodo) if isinstance(n, ast.Name) and n.id == "scan_common_paths"]

    assert consultas and escaneos, "`_scan_tool_paths` tiene que consultar `perfil_aislado()` y escanear"
    assert min(consultas) < min(escaneos), "el interruptor se consulta ANTES de escanear"


# ---------------------------------------------------------------------------
# El runbook del rig tiene que decirle al ejecutor que lo active
# ---------------------------------------------------------------------------


def test_el_runbook_del_rig_exige_activar_el_perfil_aislado() -> None:
    """La regla no se borra en silencio: el hallazgo de R0 fue justamente que el arnés no lo sabía."""
    runbook = (RAIZ / "docs" / "operations" / "r0_recertificacion_en_rig.md").read_text(encoding="utf-8")

    assert ENV_PERFIL_AISLADO in runbook


def test_agents_md_documenta_el_perfil_aislado_y_sus_anclas() -> None:
    """AGENTS.md es la fuente canónica para los agentes: la regla y su ancla no se borran en silencio."""
    agents = (RAIZ / "AGENTS.md").read_text(encoding="utf-8")

    assert ENV_PERFIL_AISLADO in agents
    assert "tests/test_aislamiento_perfil.py" in agents


def test_el_modulo_de_aislamiento_no_importa_nada_pesado_al_cargarse() -> None:
    """``import sky_claw`` lo ejecuta SIEMPRE (los workers livianos no deben arrastrar ``config``/``keyring``)."""
    importaciones = [
        nodo for nodo in ast.parse(inspect.getsource(aislamiento)).body if isinstance(nodo, ast.Import | ast.ImportFrom)
    ]
    # Módulos importados: el alias de ``import x`` y el ``module`` de ``from x import y`` (no los nombres importados).
    nombres = {
        (n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in importaciones
        for a in (n.names if isinstance(n, ast.Import) else [None])  # type: ignore[list-item]
    }

    assert nombres <= {"__future__", "logging", "os"}, f"imports de módulo no livianos: {nombres}"


def _sin_uso(_: Any) -> None:  # pragma: no cover -- evita que ruff quite el import de ``Any`` si se edita el archivo
    return None
