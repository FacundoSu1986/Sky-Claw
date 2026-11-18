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
    "sky_claw/app/agent/tools/__init__.py",
    "sky_claw/app/comms/frontend_bridge.py",
    "sky_claw/app/gui/setup_wizard.py",
    "sky_claw/app/gui/sky_claw_gui.py",
    "sky_claw/app_context.py",
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
