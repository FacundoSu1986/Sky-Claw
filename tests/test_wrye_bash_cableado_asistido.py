"""Cableado de la etapa 6 ASISTIDA — ADR 0016 §7, PR B.

La etapa tiene **tres superficies de producción** y el defecto que este repositorio pagó
nueve veces es cablear una y dejar a sus gemelas con el comportamiento viejo:

1. ``dispatcher_dependencies.build_wrye_bash_pipeline`` — el callable que consume la
   estrategia del dispatcher (GUI / ``RitualRunner`` / API).
2. ``SupervisorAgent.execute_wrye_bash_pipeline`` — el método público del supervisor.
3. ``system_tools.generate_bashed_patch`` — la tool que ve el agente LLM.

Antes del PR B las tres llamaban a ``WryeBashPipelineService.execute_pipeline``: el camino
headless que **no puede construir el parche** (``bash.py`` no expone build), que tomaba los
locks ``bashed-patch`` + ``load-order``, emitía ``ActionManifest`` y abría una TX para
fallar cerrado siempre. La etapa 6 existía como ritual muerto en producción.

Este módulo congela el cableado en dos capas:

* **Ancla por AST que enumera** (bloque 1): el conjunto de call sites de
  ``ejecutar_fase_asistida`` en ``sky_claw/`` es EXACTAMENTE el de las tres superficies, y
  ninguno de esos archivos vuelve a llamar ``execute_pipeline``. Una cuarta superficie —o
  una que regrese al camino headless— rompe el test sin que nadie tenga que recordarla.
* **Conducta** (bloques 2 a 5): la decisión de fase vive en UN solo lugar, la estrategia
  deja pasar ``fase``/``testigo_previo`` sin aflojar el filtro, el estado ``AWAITING_OPERATOR``
  se alcanza por contrato y no por accidente, y la tool del agente LLM resuelve rutas con el
  perfil de la sesión (H-1) en vez de con el literal ``"agent-tool"``.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.schemas import GenerateBashedPatchParams
from sky_claw.app.orchestrator.dispatcher_dependencies import (
    WryeBashPipeline,
    build_wrye_bash_pipeline,
)
from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.middleware import IdempotencyMiddleware
from sky_claw.local.tools.wrye_bash_asistida import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Las TRES superficies, con la función que debe contener el enrutado —nombre punteado,
#: no solo el de nivel superior. En el dispatcher la llamada vive en el closure ``execute``
#: que devuelve el builder, y decir eso es parte del ancla: el builder no ejecuta nada.
#: Igualdad literal, no subconjunto: es la diferencia entre atajar a la cuarta superficie
#: y solo a las que ya conocíamos.
SUPERFICIES: dict[str, str] = {
    "sky_claw/app/orchestrator/dispatcher_dependencies.py": "build_wrye_bash_pipeline.execute",
    "sky_claw/app/orchestrator/supervisor.py": "execute_wrye_bash_pipeline",
    "sky_claw/app/agent/tools/system_tools.py": "generate_bashed_patch",
}

#: Nombre del punto ÚNICO de enrutado. Cambiarlo obliga a actualizar este ancla a mano,
#: que es exactamente lo que se quiere de un ancla.
ENRUTADO = "ejecutar_fase_asistida"


class _VisitanteDeLlamadas(ast.NodeVisitor):
    """Recolecta llamadas a atributos, anotando la función que las envuelve."""

    def __init__(self, atributos: set[str]) -> None:
        self._atributos = atributos
        self._pila: list[str] = []
        self.hallados: list[tuple[str, str, int]] = []

    def visit_FunctionDef(self, nodo: ast.FunctionDef) -> None:
        self._pila.append(nodo.name)
        self.generic_visit(nodo)
        self._pila.pop()

    def visit_AsyncFunctionDef(self, nodo: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(nodo)

    def visit_Call(self, nodo: ast.Call) -> None:
        attr = getattr(nodo.func, "attr", None)
        if attr in self._atributos:
            self.hallados.append((".".join(self._pila) or "<modulo>", attr, nodo.lineno))
        self.generic_visit(nodo)


def _llamadas(archivo: pathlib.Path, atributos: set[str]) -> list[tuple[str, str, int]]:
    visitante = _VisitanteDeLlamadas(atributos)
    visitante.visit(ast.parse(archivo.read_text(encoding="utf-8")))
    return visitante.hallados


# ==============================================================================
# 1. Ancla por AST: las superficies se enumeran, no se muestrean
# ==============================================================================


def test_el_enrutado_asistido_tiene_exactamente_tres_superficies() -> None:
    """Enumera los call sites de ``ejecutar_fase_asistida`` en todo el paquete.

    Se cuenta por (archivo, función envolvente) y no solo por archivo: una segunda
    llamada dentro de la misma función también es una divergencia (dos lugares que
    eligen fase en la misma superficie).
    """
    medido: list[tuple[str, str]] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        for funcion, _attr, _linea in _llamadas(archivo, {ENRUTADO}):
            medido.append((str(archivo.relative_to(RAIZ)).replace("\\", "/"), funcion))

    esperado = [(ruta, funcion) for ruta, funcion in SUPERFICIES.items()]
    assert sorted(medido) == sorted(esperado), (
        "El cableado de la etapa 6 asistida dejó de ser el de ADR 0016 §7. "
        f"Superficies medidas: {sorted(medido)}. Una superficie NUEVA obliga a actualizar "
        "el ADR y este ancla; una que desapareció es una regresión de cableado."
    )


def test_ninguna_superficie_vuelve_al_camino_headless() -> None:
    """Ninguno de los tres archivos puede llamar ``execute_pipeline`` otra vez.

    ``execute_pipeline`` spawnea ``bash.py``, que no soporta build: la corrida siempre
    falla cerrado y deja lock, manifiesto y TX por el camino. Volver a llamarlo desde una
    superficie asistida reintroduce el ritual muerto que el PR B cierra.
    """
    culpables: list[str] = []
    for ruta in SUPERFICIES:
        for funcion, attr, linea in _llamadas(
            PAQUETE / pathlib.Path(ruta).relative_to("sky_claw"), {"execute_pipeline"}
        ):
            culpables.append(f"{ruta}:{linea} ({funcion} → {attr})")

    assert culpables == [], f"superficies que volvieron al camino headless: {culpables}"


def test_el_protocol_y_las_tres_superficies_aceptan_fase() -> None:
    """La firma del Protocol es la que las tres superficies tienen que honrar.

    Un ``fase`` que exista en el Protocol pero no en una superficie (o al revés) es la
    forma en que este cableado se rompe en silencio: el argumento se descarta antes de
    llegar al servicio y la fase 2 se convierte en una segunda preparación.
    """
    parametros = set(WryeBashPipeline.__call__.__annotations__) | set(_parametros_de(WryeBashPipeline.__call__))
    assert {"profile", "validate_limit", "fase", "testigo_previo"} <= parametros

    for ruta, funcion in SUPERFICIES.items():
        arbol = ast.parse((PAQUETE / pathlib.Path(ruta).relative_to("sky_claw")).read_text(encoding="utf-8"))
        firma = _firma_por_camino(arbol, funcion)
        assert firma is not None, f"{ruta} ya no define {funcion}()"
        nombres = {arg.arg for arg in firma.args.kwonlyargs} | {arg.arg for arg in firma.args.args}
        assert {"fase", "testigo_previo"} <= nombres, f"{funcion}() en {ruta} no acepta fase/testigo_previo"


def _firma_por_camino(arbol: ast.Module, camino: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Busca ``a.b.c`` entre las definiciones, descendiendo por cada tramo."""
    nivel: list[Any] = [arbol]
    encontrado: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for tramo in camino.split("."):
        encontrado = next(
            (
                nodo
                for contenedor in nivel
                for nodo in ast.walk(contenedor)
                if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == tramo
            ),
            None,
        )
        if encontrado is None:
            return None
        nivel = [encontrado]
    return encontrado


def test_el_literal_preparar_no_deriva_entre_superficies() -> None:
    """`SupervisorAgent` no puede importar dominio (#553), así que repite el literal.

    El guardrail de frontera le prohíbe importar `sky_claw.local.*`, y por eso su default
    de `fase` es la cadena `"preparar"` y no `FASE_PREPARAR`. Esta ancla cierra el
    hueco que eso abre: si el módulo de dominio renombrara la fase, las tres superficies
    seguirían mandando un valor que el servicio rechaza con `FaseInvalida`.
    """
    from sky_claw.local.tools.wrye_bash_asistida import FASE_PREPARAR

    assert FASE_PREPARAR == "preparar"

    for ruta, funcion in SUPERFICIES.items():
        arbol = ast.parse((PAQUETE / pathlib.Path(ruta).relative_to("sky_claw")).read_text(encoding="utf-8"))
        firma = _firma_por_camino(arbol, funcion)
        assert firma is not None
        defaults = dict(
            zip(
                [arg.arg for arg in firma.args.kwonlyargs]
                + [arg.arg for arg in firma.args.args][-len(firma.args.defaults or []) :],
                [d for d in firma.args.kw_defaults if d is not None] + list(firma.args.defaults),
                strict=False,
            )
        )
        if "fase" not in defaults:
            continue
        default = defaults["fase"]
        if isinstance(default, ast.Name):
            # La superficie SÍ puede importar el dominio (sólo el supervisor no puede).
            assert default.id == "FASE_PREPARAR", f"{funcion}() en {ruta} defaulta fase a {default.id}"
            continue
        assert isinstance(default, ast.Constant), f"{funcion}() en {ruta}: default de fase no constante"
        assert default.value == FASE_PREPARAR, (
            f"{funcion}() en {ruta} defaulta fase a {default.value!r}, "
            f"no a {FASE_PREPARAR!r}: el servicio lo rechazaría con FaseInvalida"
        )


def _parametros_de(funcion: Any) -> tuple[str, ...]:
    import inspect

    return tuple(inspect.signature(funcion).parameters)


# ==============================================================================
# 2. Una sola decisión: el enrutado elige y delega, no reimplementa
# ==============================================================================


def _servicio(**kwargs: Any) -> WryeBashPipelineService:
    return WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_el_enrutado_delega_en_la_fase_correcta() -> None:
    """``preparar`` y ``verificar`` van a métodos distintos, con el testigo solo en la 2."""
    servicio = _servicio()
    servicio.preparar_etapa_asistida = AsyncMock(return_value={"fase": "uno"})
    servicio.verificar_etapa_asistida = AsyncMock(return_value={"fase": "dos"})
    testigo = {"existia": True, "sha256": "a" * 64}

    assert await servicio.ejecutar_fase_asistida(profile="P") == {"fase": "uno"}
    servicio.preparar_etapa_asistida.assert_awaited_once_with(profile="P", validate_limit=True)

    assert await servicio.ejecutar_fase_asistida(profile="P", fase="verificar", testigo_previo=testigo) == {
        "fase": "dos"
    }
    servicio.verificar_etapa_asistida.assert_awaited_once_with(profile="P", testigo_previo=testigo)
    # La fase 1 no recibe el testigo: no tiene nada que comparar y aceptarlo sugeriría
    # que puede probar frescura, que es justo lo que solo la fase 2 hace.
    servicio.preparar_etapa_asistida.assert_awaited_once_with(profile="P", validate_limit=True)


@pytest.mark.asyncio
async def test_la_fase_invalida_falla_cerrado_antes_de_cualquier_io() -> None:
    """Un ``fase`` desconocido no cae en un default silencioso ni corre el preflight."""
    servicio = _servicio()
    servicio._ensure_preflight = MagicMock()  # type: ignore[method-assign]
    servicio.preparar_etapa_asistida = AsyncMock()
    servicio.verificar_etapa_asistida = AsyncMock()

    resultado = await servicio.ejecutar_fase_asistida(profile="P", fase="construir")

    assert resultado["success"] is False
    assert resultado["reason"] == "FaseInvalida"
    assert "construir" in resultado["message"]
    servicio._ensure_preflight.assert_not_called()
    servicio.preparar_etapa_asistida.assert_not_awaited()
    servicio.verificar_etapa_asistida.assert_not_awaited()


@pytest.mark.asyncio
async def test_la_primera_superficie_no_reimplementa_la_decision() -> None:
    """El adaptador de la GUI pasa la fase al enrutado; no elige entre preparar/verificar."""
    servicio = MagicMock()
    servicio.ejecutar_fase_asistida = AsyncMock(return_value={"success": False, "pendiente": True})
    pipeline = build_wrye_bash_pipeline(service=servicio, default_profile="Sesion")

    await pipeline(fase="verificar", testigo_previo={"sha256": "b" * 64})

    servicio.ejecutar_fase_asistida.assert_awaited_once_with(
        profile="Sesion",
        fase="verificar",
        validate_limit=True,
        testigo_previo={"sha256": "b" * 64},
    )


# ==============================================================================
# 3. La estrategia deja pasar fase/testigo sin aflojar el filtro
# ==============================================================================


@pytest.mark.asyncio
async def test_la_estrategia_reenvia_fase_y_testigo() -> None:
    pipeline = AsyncMock(return_value={"success": True})
    estrategia = GenerateBashedPatchStrategy(wrye_bash_pipeline=pipeline)
    testigo = {"existia": False, "sha256": None}

    await estrategia.execute(
        {"profile": "P", "fase": "verificar", "testigo_previo": testigo, "tool_name": "x", "extra": 1}
    )

    pipeline.assert_awaited_once_with(profile="P", fase="verificar", testigo_previo=testigo)


@pytest.mark.asyncio
async def test_la_estrategia_sigue_filtrando_lo_que_no_honra() -> None:
    """El filtro no se aflojó: lo desconocido se descarta y ``fase=None`` no viaja."""
    pipeline = AsyncMock(return_value={"success": True})
    estrategia = GenerateBashedPatchStrategy(wrye_bash_pipeline=pipeline)

    await estrategia.execute({"fase": None, "testigo_previo": None, "dry_run": True})

    pipeline.assert_awaited_once_with()


# ==============================================================================
# 4. El estado del task: ``pendiente`` NO es un fallo
# ==============================================================================


class _Estrategia:
    name = "generate_bashed_patch"


@pytest.mark.asyncio
async def test_una_etapa_pendiente_queda_esperando_al_operador() -> None:
    """``success=False`` + ``pendiente=True`` → ``AWAITING_OPERATOR``, con el lock libre.

    Sin este mapeo (el ``pendiente_no_ratificado`` que el PR A dejó a propósito como
    marcador provisional) la etapa se registraba ``FAILED``: el panel decía que el ritual
    falló cuando en realidad estaba esperando a que el humano abriera Wrye Bash.
    """
    sm = ToolStateMachine()
    middleware = IdempotencyMiddleware(state_machine=sm)

    async def resultado() -> dict[str, Any]:
        return {"success": False, "pendiente": True, "motivo": "ARTEFACTO_AUSENTE"}

    await middleware(_Estrategia(), {"fase": "preparar"}, resultado)

    tarea = next(iter(sm._tasks.values()))
    assert tarea.state == "AWAITING_OPERATOR"
    # Terminal para el conteo: la etapa no sigue "en curso" mientras espera al operador.
    assert sm.active_task_count == 0
    # Y la clave se libera como en cualquier estado terminal: reintentar la preparación
    # o pasar a ``verificar`` con el mismo payload no puede dar ``DuplicateExecution``.
    assert sm.guard.is_active(tarea.idempotency_key) is False


@pytest.mark.asyncio
async def test_un_fallo_real_sigue_siendo_failed() -> None:
    """Preflight rojo, M-04 o rutas irresolubles: ``success=False`` SIN ``pendiente``."""
    sm = ToolStateMachine()
    middleware = IdempotencyMiddleware(state_machine=sm)

    async def resultado() -> dict[str, Any]:
        return {"success": False, "reason": "PreflightBlocked", "error": "Data sin permisos"}

    await middleware(_Estrategia(), {}, resultado)

    tarea = next(iter(sm._tasks.values()))
    assert tarea.state == "FAILED"
    # `_extract_error_message` prefiere `error` (el texto para el operador) al `reason`.
    assert "Data sin permisos" in str(tarea.error_message)


@pytest.mark.asyncio
async def test_el_exito_sigue_siendo_completed() -> None:
    sm = ToolStateMachine()
    middleware = IdempotencyMiddleware(state_machine=sm)

    async def resultado() -> dict[str, Any]:
        return {"success": True, "motivo": "OK", "frescura_probada": True}

    await middleware(_Estrategia(), {}, resultado)

    assert next(iter(sm._tasks.values())).state == "COMPLETED"


# ==============================================================================
# 5. La superficie del agente LLM: perfil de sesión y rutas resueltas (H-1)
# ==============================================================================


def _perfil(tmp_path: pathlib.Path, *, perfil: str) -> tuple[pathlib.Path, pathlib.Path]:
    """``<data>/profiles/<perfil>/plugins.txt`` + Data del juego, como MO2 en disco."""
    plugins = tmp_path / "profiles" / perfil / "plugins.txt"
    plugins.parent.mkdir(parents=True, exist_ok=True)
    plugins.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True, exist_ok=True)
    return game, plugins


def _resolver(game: pathlib.Path, mo2: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = mo2
    resolver.get_mo2_mods_path_best_effort.return_value = mo2 / "mods"
    return resolver


@pytest.fixture()
def sin_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla los sensores de preflight (tienen su propia suite): acá se mide el cableado."""
    monkeypatch.setattr(WryeBashPipelineService, "_ensure_preflight", lambda self, **kwargs: None)


def _registry(tmp_path: pathlib.Path, *, perfil: str = "Requiem", parche: bytes | None = None) -> AsyncToolRegistry:
    game, _ = _perfil(tmp_path, perfil=perfil)
    if parche is not None:
        artefacto = game / "Data" / BASHED_PATCH_NAME
        artefacto.parent.mkdir(parents=True, exist_ok=True)
        artefacto.write_bytes(parche)
    return AsyncToolRegistry(
        registry=None,
        mo2=None,
        sync_engine=None,
        mo2_profile=perfil,
        path_resolver=_resolver(game, tmp_path),
    )


@pytest.mark.asyncio
async def test_la_tool_del_agente_prepara_con_el_perfil_de_sesion(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """H-1: sin ``path_resolver`` la tool nacía con ``SinRutasResolubles``.

    Se afirma sobre el payload, no sobre el fuente del wiring: el ``plugins_txt`` que
    devuelve la preparación tiene que ser el del perfil de la sesión. Un lambda que
    ignorara ``self._mo2_profile`` compondría ``profiles/agent-tool/plugins.txt`` y este
    test lo vería.
    """
    registry = _registry(tmp_path, perfil="Requiem")

    salida = json.loads(await registry.execute("generate_bashed_patch", {"fase": "preparar"}))

    assert salida["success"] is False
    assert salida["pendiente"] is True
    assert salida["plugins_txt"] == str(tmp_path / "profiles" / "Requiem" / "plugins.txt")
    assert "pendiente_no_ratificado" not in salida
    assert salida["instrucciones"]


@pytest.mark.asyncio
async def test_la_tool_del_agente_verifica_sin_testigo_y_declara_frescura_no_probada(
    tmp_path: pathlib.Path, sin_preflight: None
) -> None:
    """La fase 2 llega por la tool y es honesta: sin testigo no prueba frescura.

    El LLM no puede aportar ``testigo_previo`` (persistirlo es el PR D de ADR 0016 §7),
    así que un artefacto correcto da verde con ``frescura_probada=False`` en vez de
    aceptar un hash inventado.
    """
    registry = _registry(tmp_path, perfil="Requiem", parche=b"TES4" + b"\x00" * 60)

    salida = json.loads(await registry.execute("generate_bashed_patch", {"fase": "verificar"}))

    assert salida["success"] is True
    assert salida["motivo"] == "OK"
    assert salida["frescura_probada"] is False


@pytest.mark.asyncio
async def test_la_tool_del_agente_falla_cerrado_sin_resolver(tmp_path: pathlib.Path) -> None:
    """Sin resolver inyectado no se inventa un verde: ``SinRutasResolubles``."""
    registry = AsyncToolRegistry(registry=None, mo2=None, sync_engine=None, mo2_profile="Requiem")

    salida = json.loads(await registry.execute("generate_bashed_patch", {}))

    assert salida["success"] is False
    assert salida["reason"] == "SinRutasResolubles"


def test_el_schema_de_la_tool_solo_expone_fase(tmp_path: pathlib.Path) -> None:
    """El LLM elige la fase —las dos son de sólo lectura— y nada más.

    ``profile`` lo fija el registry por sesión (spec de #454) y ``testigo_previo`` no se
    expone: un hash que el modelo podría inventar convertiría la prueba de frescura en
    una afirmación sin evidencia.
    """
    # `tmp_path` y no una ruta fija: `_registry` crea `profiles/<perfil>/` bajo esa raíz,
    # y en Windows una ruta con raíz absoluta sin unidad (`/tmp/...`) no es escribible.
    descriptor = _registry(tmp_path, perfil="Requiem").tools["generate_bashed_patch"]

    assert descriptor.params_model is GenerateBashedPatchParams
    assert sorted(descriptor.params_model.model_fields) == ["fase"]
    assert list(descriptor.input_schema["properties"]) == ["fase"]
