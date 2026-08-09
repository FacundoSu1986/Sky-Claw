"""Desenlace de ``setup_tools`` cuando la configuración no se puede guardar.

Por qué existe: la instalación de tools tiene dos superficies sobre el MISMO
mecanismo, y sólo una sabía degradar. ``run_ritual_install`` (GUI) trata la
persistencia fallida como operación parcial y avisa —endurecido por la review
adversarial del PR #445, que rechazó explícitamente dejar vivo "el éxito
visual"—; ``setup_tools`` (agente LLM) tenía los dos agujeros que su gemelo ya
había cerrado:

* el ``await guardar_config_bloqueante`` vivía FUERA del manejo de errores, así
  que una excepción se llevaba puesto el dict de resultados entero: el caller ni
  se enteraba de que las tools habían quedado en disco;
* sin ``local_cfg`` o sin ``config_path`` el guardado se salteaba y el JSON
  informaba éxito total, con el campo nunca escrito.

Los dos desenlaces son la misma operación parcial: las tools están en disco pero
el path/registro que las hace utilizables no sobrevive al reinicio.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`). El ancla por AST congela la tabla completa de ramas de
``setup_tools``: una rama nueva que escriba config y no se registre rompe el
test en vez de heredar el defecto en silencio, que es exactamente cómo esta
familia se propagó las 13 veces anteriores.
"""

from __future__ import annotations

import ast
import json
import pathlib
import textwrap
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.agent.tools import external_tools
from sky_claw.app.agent.tools.external_tools import setup_tools
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.local.local_config import LocalConfig
from sky_claw.local.tools.tool_result import normalize_tool_result
from sky_claw.local.tools_installer import ToolInstallError

# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway() -> NetworkGateway:
    """Zero-Trust: ``setup_tools`` aborta sin gateway antes de tocar nada."""
    return NetworkGateway(EgressPolicy(block_private_ips=False))


@pytest.fixture
def sesion() -> Any:
    """Sesión inyectada: evita que ``setup_tools`` abra una real."""
    return MagicMock(spec=aiohttp.ClientSession)


def _resultado_loot(*, already_existed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        exe_path=pathlib.Path("C:/tools/LOOT/loot.exe"),
        version="0.22.1",
        already_existed=already_existed,
    )


def _instalador(*, already_existed: bool = False, side_effect: Any = None) -> MagicMock:
    inst = MagicMock()
    if side_effect is not None:
        inst.ensure_loot = AsyncMock(side_effect=side_effect)
    else:
        inst.ensure_loot = AsyncMock(return_value=_resultado_loot(already_existed=already_existed))
    inst.ensure_xedit = AsyncMock(
        return_value=SimpleNamespace(
            exe_path=pathlib.Path("C:/tools/SSEEdit/SSEEdit.exe"),
            version="4.1.5",
            already_existed=already_existed,
        )
    )
    return inst


def _romper_el_guardado(monkeypatch: pytest.MonkeyPatch) -> None:
    """El guardado lanza. ``setup_tools`` importa el símbolo DENTRO de la función,
    así que parchear el módulo dueño alcanza."""

    async def _explota(*_a: Any, **_k: Any) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _explota)


def _espiar_el_guardado(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    llamadas: list[tuple[Any, Any]] = []

    async def _espia(cfg: Any, ruta: Any) -> None:
        llamadas.append((cfg, ruta))

    monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _espia)
    return llamadas


# ---------------------------------------------------------------------------
# Comportamiento: los dos desenlaces que la GUI ya cubría
# ---------------------------------------------------------------------------


class TestDegradacionPorPersistencia:
    async def test_una_excepcion_al_guardar_no_se_lleva_puesto_el_resultado(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defecto A: el ``await`` vivía fuera del manejo de errores.

        La excepción se propagaba y el caller perdía el dict entero — sin forma
        de saber que LOOT sí había quedado en disco.
        """
        _romper_el_guardado(monkeypatch)

        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            LocalConfig(),
            tmp_path / "cfg.json",
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["loot"]
        assert entrada["success"] is False  # NO éxito: la operación fue parcial
        assert entrada["message"]
        # El detalle de lo que SÍ pasó sobrevive: el LLM tiene que poder decir
        # "está en disco" sin volver a instalar a ciegas.
        assert entrada["status"] == "installed"
        assert entrada["exe_path"].endswith("loot.exe")
        assert entrada["version"] == "0.22.1"
        # Y el contrato canónico lo entiende sin caer al fallback.
        normalizado = normalize_tool_result(entrada)
        assert normalizado["success"] is False
        assert normalizado["message"] != "error desconocido"

    async def test_sin_config_path_degrada_igual_que_si_hubiera_lanzado(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path
    ) -> None:
        """Defecto B, primera mitad: sin ruta el guardado no puede correr.

        Simetría con ``ritual_runner``, que hace ``persistencia_ok = config_path
        is not None`` justamente porque cubrir sólo la excepción dejaba vivo el
        éxito visual (review del PR #445).
        """
        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            LocalConfig(),
            None,
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["loot"]
        assert entrada["success"] is False
        assert entrada["message"]
        assert entrada["status"] == "installed"

    async def test_sin_local_cfg_degrada_igual(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path
    ) -> None:
        """Defecto B, segunda mitad: sin config no hay dónde escribir el campo.

        La GUI no tiene este caso (siempre trae su ``app_context``); acá el
        registry puede construirse sin ``local_cfg`` y hoy eso devolvía éxito.
        """
        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            None,
            tmp_path / "cfg.json",
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["loot"]
        assert entrada["success"] is False
        assert entrada["message"]

    async def test_el_mensaje_nombra_el_disco_y_la_configuracion(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El aviso tiene que separar los dos hechos: qué sí pasó y qué no.

        "Falló" a secas invita a reinstalar a ciegas; el operador necesita saber
        que el binario ya está y que lo que falta es el registro.
        """
        _romper_el_guardado(monkeypatch)

        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            LocalConfig(),
            tmp_path / "cfg.json",
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        mensaje = json.loads(salida)["loot"]["message"].lower()
        assert "disco" in mensaje
        assert "configuración" in mensaje
        # Presupuesto de contexto: `router.py` trunca el resultado a 4000 chars y
        # un JSON de seis tools ya es voluminoso. Un JSON cortado a la mitad es
        # peor para el LLM que el defecto que este aviso vino a cerrar.
        assert len(mensaje) <= 200

    async def test_xedit_preexistente_participa_igual_que_sus_hermanos(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``xedit`` era el único que escribía sólo con ``not already_existed``.

        Con SSEEdit ya presente en ``install_dir`` y ``xedit_exe`` ausente de la
        config, la tool devolvía éxito y el path nunca se registraba: el agente
        no podía resolver xEdit tras reiniciar. Recortarlo del aviso habría
        dejado cinco ramas avisando y una no — la misma familia de defecto que
        este cambio vino a cerrar (review #455).
        """
        _romper_el_guardado(monkeypatch)

        salida = await setup_tools(
            _instalador(already_existed=True),
            tmp_path / "tools",
            LocalConfig(),
            tmp_path / "cfg.json",
            None,
            tools=["xedit"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["xedit"]
        assert entrada["success"] is False
        assert entrada["message"]
        assert entrada["status"] == "already_installed"

    async def test_xedit_preexistente_escribe_el_path_en_la_config(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path
    ) -> None:
        """La otra mitad del mismo defecto: el campo ahora sí se escribe."""
        cfg = LocalConfig()

        await setup_tools(
            _instalador(already_existed=True),
            tmp_path / "tools",
            cfg,
            tmp_path / "cfg.json",
            None,
            tools=["xedit"],
            gateway=gateway,
            session=sesion,
        )

        assert cfg.xedit_exe is not None
        assert cfg.xedit_exe.endswith("SSEEdit.exe")


class TestQueNoSeDegrada:
    async def test_una_tool_que_ya_habia_fallado_conserva_su_mensaje(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SetupToolsParams.tools`` es ``list[str]`` sin dedupe y el bucle tampoco
        deduplica: con ``["loot", "loot"]`` la segunda vuelta puede reemplazar la
        entrada por un error. Pisarle encima el aviso de persistencia perdería el
        motivo real del fallo.
        """
        _romper_el_guardado(monkeypatch)
        inst = _instalador(side_effect=[_resultado_loot(), ToolInstallError("el hash no coincide")])

        salida = await setup_tools(
            inst,
            tmp_path / "tools",
            LocalConfig(),
            tmp_path / "cfg.json",
            None,
            tools=["loot", "loot"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["loot"]
        assert entrada["success"] is False
        assert entrada["message"] == "el hash no coincide"  # el motivo REAL, no el aviso

    async def test_con_la_persistencia_sana_el_exito_sigue_siendo_exito(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path
    ) -> None:
        """Guarda de regresión: el contrato dice que ``message`` va vacío en éxito."""
        cfg = LocalConfig()
        config_path = tmp_path / "cfg.json"

        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            cfg,
            config_path,
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["loot"]
        assert entrada["success"] is True
        assert entrada["message"] == ""
        assert json.loads(config_path.read_text(encoding="utf-8"))["loot_exe"].endswith("loot.exe")

    @pytest.mark.parametrize(
        "excepcion",
        [OSError("disco lleno"), ValueError("TOML ilegible"), TypeError("no sabe persistirse")],
        ids=["disco", "config_ilegible", "config_no_persistible"],
    )
    async def test_los_fallos_conocidos_de_persistencia_degradan(
        self,
        excepcion: Exception,
        gateway: NetworkGateway,
        sesion: Any,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Los tres que la capa de config documenta como suyos: ninguno se relanza.

        ``TOMLDecodeError`` y ``JSONDecodeError`` heredan de ``ValueError``, así
        que el lector fail-closed entra por esa rama.
        """

        async def _explota(*_a: Any, **_k: Any) -> None:
            raise excepcion

        monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _explota)

        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            LocalConfig(),
            tmp_path / "cfg.json",
            None,
            tools=["loot"],
            gateway=gateway,
            session=sesion,
        )

        assert json.loads(salida)["loot"]["success"] is False

    async def test_una_excepcion_desconocida_se_relanza_en_vez_de_disfrazarse(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.github/coding_conventions.md` §2.5: relanzar lo desconocido tras loggear.

        Un ``AttributeError`` de una implementación de config rota no es "no pude
        guardar en disco". Devolver el aviso de operación parcial afirmaría un
        estado que nadie verificó y dejaría el defecto invisible. El gate no lo
        ataja: `BLE001` está exento en todo `sky_claw/app/**` (review #455).
        """

        async def _bug(*_a: Any, **_k: Any) -> None:
            raise AttributeError("'Config' object has no attribute 'save'")

        monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _bug)

        with pytest.raises(AttributeError):
            await setup_tools(
                _instalador(),
                tmp_path / "tools",
                LocalConfig(),
                tmp_path / "cfg.json",
                None,
                tools=["loot"],
                gateway=gateway,
                session=sesion,
            )

    async def test_el_guardado_corre_aunque_ninguna_tool_haya_escrito_un_campo(
        self, gateway: NetworkGateway, sesion: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El save NO se condiciona a que alguien haya registrado algo.

        ``Config.save()`` persiste por generaciones sucias, no por diff de
        valores (merge-on-save F1): saltearlo dejaría colgados los cambios
        pendientes que otro camino dejó sobre el objeto ``Config`` compartido.
        """
        llamadas = _espiar_el_guardado(monkeypatch)
        cfg = LocalConfig()

        salida = await setup_tools(
            _instalador(),
            tmp_path / "tools",
            cfg,
            tmp_path / "cfg.json",
            None,
            tools=["no_existe_esta_tool"],
            gateway=gateway,
            session=sesion,
        )

        entrada = json.loads(salida)["no_existe_esta_tool"]
        assert entrada["success"] is False
        assert set(entrada) == {"success", "message"}  # sin aviso de persistencia encima
        assert llamadas == [(cfg, tmp_path / "cfg.json")]


# ---------------------------------------------------------------------------
# Ancla por AST: toda rama que escribe config participa del registro
# ---------------------------------------------------------------------------
#
# La garantía es una propiedad del mecanismo, no un recordatorio de proceso: el
# aviso de operación parcial sólo protege si TODAS las ramas que escriben config
# se registran. Su vía de escape es una rama nueva —copiada de otra, que es como
# nacieron `ngio` y `community_shaders`— que llame `escribir_campo` y se olvide
# del `append`, o que lo haga con el literal de la rama de al lado.
#
# Por eso se congela la TABLA COMPLETA y con TRES literales por rama, no la
# igualdad de dos conjuntos: `{escribe} == {registra}` deja pasar un `append`
# con el literal equivocado, y `community_shaders` es copia literal de `ngio`.

LISTA_DE_REGISTRO = "requieren_persistencia"

# Igualdad literal. Formato: clave de la condición → (escribe config, clave del
# dict de éxito, clave del registro). Una rama nueva rompe esto hasta que se
# decida si participa; cambiar un literal por el de otra rama, también.
RAMAS_DE_SETUP_TOOLS = {
    "loot": (True, "loot", "loot"),
    "xedit": (True, "xedit", "xedit"),
    "pandora": (True, "pandora", "pandora"),
    "bodyslide": (True, "bodyslide", "bodyslide"),
    "ngio": (True, "ngio", "ngio"),
    "community_shaders": (True, "community_shaders", "community_shaders"),
}


def _clave_de_la_condicion(rama: ast.If) -> str | None:
    """``elif tool_name_lower == "<clave>"`` → ``"<clave>"``.

    Devuelve ``None`` para cualquier otro ``if`` (los ``if local_cfg:`` anidados
    y el ``else`` final, que ni siquiera es un nodo ``If``).
    """
    test = rama.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return None
    if not (isinstance(test.left, ast.Name) and test.left.id == "tool_name_lower"):
        return None
    comparador = test.comparators[0]
    if isinstance(comparador, ast.Constant) and isinstance(comparador.value, str):
        return comparador.value
    return None


def _nodos_del_cuerpo(rama: ast.If) -> Iterator[ast.AST]:
    """Sólo el cuerpo de ESTA rama.

    Nunca ``ast.walk(rama)``: el chain ``elif`` vive en el ``orelse``, así que
    ``walk`` metería las cinco ramas siguientes dentro de la primera y "loot se
    registra" daría verde porque quien se registra es ``community_shaders``.
    Recorrer ``rama.body`` sí alcanza los ``if local_cfg:`` anidados y los
    ``continue`` tempranos de ngio/CS, que es lo que hace falta.
    """
    for sentencia in rama.body:
        yield from ast.walk(sentencia)


def _escribe_config(rama: ast.If) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "escribir_campo"
        for n in _nodos_del_cuerpo(rama)
    )


def _clave_del_resultado(rama: ast.If) -> str | None:
    """Clave del dict de ÉXITO: ``results["x"] = {...}`` con un literal de dict.

    Exigir ``ast.Dict`` excluye las asignaciones de ``_error_result(...)``, que
    son ``Call`` y describen el desenlace contrario.
    """
    for nodo in _nodos_del_cuerpo(rama):
        if not isinstance(nodo, ast.Assign) or not isinstance(nodo.value, ast.Dict):
            continue
        for destino in nodo.targets:
            if (
                isinstance(destino, ast.Subscript)
                and isinstance(destino.value, ast.Name)
                and destino.value.id == "results"
                and isinstance(destino.slice, ast.Constant)
                and isinstance(destino.slice.value, str)
            ):
                return destino.slice.value
    return None


def _clave_del_registro(rama: ast.If) -> str | None:
    """Literal que la rama appendea a la lista de registro."""
    for nodo in _nodos_del_cuerpo(rama):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "append"
            and isinstance(nodo.func.value, ast.Name)
            and nodo.func.value.id == LISTA_DE_REGISTRO
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            return nodo.args[0].value
    return None


def _tabla_de_ramas(arbol: ast.AST) -> dict[str, tuple[bool, str | None, str | None]]:
    """Tabla de las ramas por tool de ``setup_tools`` en un árbol cualquiera."""
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "setup_tools")
    tabla: dict[str, tuple[bool, str | None, str | None]] = {}
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.If):
            continue
        clave = _clave_de_la_condicion(nodo)
        if clave is None:
            continue
        tabla[clave] = (_escribe_config(nodo), _clave_del_resultado(nodo), _clave_del_registro(nodo))
    return tabla


def _tabla_real() -> dict[str, tuple[bool, str | None, str | None]]:
    fuente = pathlib.Path(external_tools.__file__).read_text(encoding="utf-8")
    return _tabla_de_ramas(ast.parse(fuente, filename=external_tools.__file__))


def test_ancla_congela_la_tabla_de_ramas_de_setup_tools() -> None:
    """Igualdad literal: una rama nueva rompe el test hasta que se decida si
    participa del registro, en vez de heredar el defecto en silencio."""
    assert _tabla_real() == RAMAS_DE_SETUP_TOOLS


def test_toda_rama_que_escribe_config_se_registra_con_su_propia_clave() -> None:
    """La propiedad que el ancla protege, enunciada aparte de la tabla: si
    escribís config, participás del aviso, y con TU clave."""
    for clave, (escribe, clave_resultado, clave_registro) in _tabla_real().items():
        if not escribe:
            continue
        assert clave_registro == clave, f"la rama {clave!r} se registra como {clave_registro!r}"
        assert clave_resultado == clave, f"la rama {clave!r} publica su resultado como {clave_resultado!r}"


# ---------------------------------------------------------------------------
# Meta-tests: el detector detecta
# ---------------------------------------------------------------------------
#
# Un ancla que no falla ante el defecto que persigue es peor que no tenerla:
# da la señal de estar cubierto. Se ejerce contra fuentes sintéticas.


def _tabla_de_fuente(fuente: str) -> dict[str, tuple[bool, str | None, str | None]]:
    return _tabla_de_ramas(ast.parse(textwrap.dedent(fuente)))


FUENTE_SIN_REGISTRO = """
    async def setup_tools(local_cfg, tools):
        for tool_name in tools:
            tool_name_lower = tool_name.lower()
            if tool_name_lower == "loot":
                result = ensure_loot()
                if local_cfg:
                    escribir_campo(local_cfg, "loot_exe", result)
                results["loot"] = {"success": True, "message": ""}
            elif tool_name_lower == "xedit":
                result = ensure_xedit()
                if local_cfg:
                    escribir_campo(local_cfg, "xedit_exe", result)
                results["xedit"] = {"success": True, "message": ""}
                requieren_persistencia.append("xedit")
            else:
                results[tool_name] = _error_result("desconocida")
"""


def test_el_detector_ve_la_rama_que_escribe_y_no_se_registra() -> None:
    """El defecto central: copiar una rama y olvidar el ``append``."""
    tabla = _tabla_de_fuente(FUENTE_SIN_REGISTRO)
    assert tabla["loot"] == (True, "loot", None)
    assert tabla["xedit"] == (True, "xedit", "xedit")


def test_el_registro_de_una_rama_no_se_le_atribuye_a_la_anterior() -> None:
    """Regresión del bug que haría inútil al ancla.

    Con ``ast.walk(rama)`` en vez de ``rama.body``, el recorrido baja al
    ``orelse`` y el ``append`` de ``xedit`` se contaría como de ``loot``: la
    tabla daría verde con el defecto puesto.
    """
    assert _tabla_de_fuente(FUENTE_SIN_REGISTRO)["loot"][2] is None


def test_el_detector_ve_el_literal_cruzado_entre_ramas() -> None:
    """La copia literal de una rama a otra sin renombrar el ``append`` — cómo
    nació ``community_shaders`` a partir de ``ngio``."""
    tabla = _tabla_de_fuente(
        """
        async def setup_tools(local_cfg, tools):
            for tool_name in tools:
                tool_name_lower = tool_name.lower()
                if tool_name_lower == "ngio":
                    escribir_campo(local_cfg, "ngio_mods", mods)
                    results["ngio"] = {"success": True, "message": ""}
                    requieren_persistencia.append("ngio")
                elif tool_name_lower == "community_shaders":
                    escribir_campo(local_cfg, "community_shaders_mods", mods)
                    results["community_shaders"] = {"success": True, "message": ""}
                    requieren_persistencia.append("ngio")
        """
    )
    assert tabla["community_shaders"] == (True, "community_shaders", "ngio")


def test_el_detector_ignora_las_entradas_de_error() -> None:
    """``results[...] = _error_result(...)`` no es el dict de éxito: si lo
    contara, una rama que sólo falla parecería publicar un resultado."""
    tabla = _tabla_de_fuente(
        """
        async def setup_tools(local_cfg, tools):
            for tool_name in tools:
                tool_name_lower = tool_name.lower()
                if tool_name_lower == "ngio":
                    if not mo2_root:
                        results["ngio"] = _error_result("sin mo2_root")
                        continue
                    results["ngio"] = {"success": True, "message": ""}
        """
    )
    assert tabla["ngio"] == (False, "ngio", None)
