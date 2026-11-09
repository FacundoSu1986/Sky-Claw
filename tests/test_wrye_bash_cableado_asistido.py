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
import copy
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pydantic
import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.schemas import GenerateBashedPatchParams
from sky_claw.app.agent.tools.system_tools import generate_bashed_patch
from sky_claw.app.orchestrator.dispatcher_dependencies import (
    WryeBashPipeline,
    build_wrye_bash_pipeline,
)
from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.app.orchestrator.tool_strategies.middleware import IdempotencyMiddleware
from sky_claw.app.security.sanitize import sanitize_for_prompt
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


def test_las_dos_fases_solo_se_invocan_desde_el_enrutado() -> None:
    """Cierre del ancla anterior: enumerar el ROUTER no alcanza.

    ``test_el_enrutado_asistido_tiene_exactamente_tres_superficies`` pinea quién llama a
    ``ejecutar_fase_asistida``. Una superficie nueva que llamara ``preparar_etapa_asistida``
    o ``verificar_etapa_asistida`` DIRECTO no aparecería ahí —y volvería a tener su propia
    copia de la decisión de fase, que es exactamente lo que el punto único evita—. Acá se
    enumera a los llamadores de los dos métodos: en producción sólo puede ser el router.

    (Las llamadas al nombre desnudo —``preparar_etapa_asistida(artefacto=…, plugins_txt=…)``—
    son la función PURA del módulo de dominio, no el método del servicio: no cuentan.)
    """
    medido: list[tuple[str, str]] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        relativo = str(archivo.relative_to(RAIZ)).replace("\\", "/")
        for funcion, _attr, _linea in _llamadas(archivo, {"preparar_etapa_asistida", "verificar_etapa_asistida"}):
            medido.append((relativo, funcion))

    assert sorted(medido) == sorted([("sky_claw/local/tools/wrye_bash_service.py", "ejecutar_fase_asistida")] * 2), (
        f"alguien elige la fase fuera del enrutado único: {sorted(medido)}"
    )


def test_ningun_caller_de_produccion_usa_el_camino_headless() -> None:
    """Pin global del camino headless, más fuerte que el de las tres superficies.

    ``execute_pipeline`` no puede construir el parche, toma los locks y deja la TX. Tras
    el cableado **no tiene ningún caller de producción** de Wrye Bash; el único
    ``.execute_pipeline(`` que queda en el paquete es el de Synthesis, que es otro
    servicio y otro camino (legítimo). Se enumera el conjunto completo, así que:

    * una superficie que vuelva al camino headless rompe acá aunque no esté en
      :data:`SUPERFICIES`;
    * el PR C de ADR 0016 §7 (retirar ``execute_pipeline``) tiene su tripwire: borrarlo
      deja este test en verde sólo si además se actualiza el ancla, que es la decisión
      que hay que tomar a mano.
    """
    medido: list[tuple[str, str, str]] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        relativo = str(archivo.relative_to(RAIZ)).replace("\\", "/")
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        visitante = _VisitanteDeLlamadas({"execute_pipeline"})
        visitante.visit(arbol)
        for funcion, _attr, _linea in visitante.hallados:
            medido.append((relativo, funcion, "execute_pipeline"))

    assert sorted(medido) == [
        ("sky_claw/app/orchestrator/tool_strategies/execute_synthesis.py", "execute.ritual", "execute_pipeline")
    ], f"call sites de execute_pipeline en producción: {sorted(medido)}"


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


# ==============================================================================
# 7. La superficie que el humano ve: el toast del ritual
# ==============================================================================


def _payload_de_preparar() -> dict[str, object]:
    """Payload REAL de la fase 1, armado por el módulo de dominio (no a mano)."""
    from sky_claw.local.tools.wrye_bash_asistida import (
        PASOS_ASISTIDOS,
        EstadoEnPlugins,
        PreparacionAsistida,
        TestigoPrevio,
        preparacion_como_resultado,
    )

    return preparacion_como_resultado(
        PreparacionAsistida(
            artefacto="C:/mo2/overwrite/Bashed Patch, 0.esp",
            plugins_txt="C:/mo2/profiles/Requiem/plugins.txt",
            huella_previa=None,
            testigo_previo=TestigoPrevio(existia=False, sha256=None),
            estado_plugins=EstadoEnPlugins.AUSENTE,
            instrucciones=PASOS_ASISTIDOS,
        )
    )


def test_el_toast_del_operador_no_dice_fallo_cuando_la_etapa_espera() -> None:
    """El cableado arregla el `ToolStateMachine`, pero el operador lee el TOAST.

    Antes de esta rama, la fase 1 producía literalmente «El ritual «wrye_bash» falló:
    Etapa 6 (Bashed Patch) requiere tu intervención…» en ROJO: un mensaje que se
    contradice solo y que reporta como fracaso lo que es una instrucción. Es la misma
    mala lectura que el estado ``FAILED``, pero en la superficie que el humano ve — y el
    estado no tiene hoy ningún consumidor de UI, así que arreglar sólo el estado no
    alcanzaba.
    """
    from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result

    mensaje, kind = summarize_ritual_result("wrye_bash", _payload_de_preparar())

    assert kind == "warning"
    assert "falló" not in mensaje
    assert "requiere tu intervención" in mensaje


def test_un_fallo_real_del_ritual_sigue_siendo_negativo() -> None:
    """La rama nueva no puede tapar los fallos: preflight rojo, M-04 o rutas irresolubles."""
    from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result

    mensaje, kind = summarize_ritual_result(
        "wrye_bash", {"success": False, "reason": "PreflightBlocked", "error": "Data sin permisos"}
    )

    assert kind == "negative"
    assert "falló" in mensaje
    assert "Data sin permisos" in mensaje


def test_la_verificacion_exitosa_sigue_siendo_positiva() -> None:
    from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result

    assert summarize_ritual_result("wrye_bash", {"success": True, "message": ""}) == (
        "Ritual «wrye_bash» completado.",
        "positive",
    )


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
@pytest.mark.parametrize("fase_invalida", [[], {}, ["preparar"], 1, b"preparar"])
async def test_una_fase_no_hashable_o_no_str_falla_cerrado_sin_excepcion(fase_invalida: Any) -> None:
    """``[] not in frozenset`` lanza ``TypeError``: la validación debe devolver el dict, no explotar.

    La estrategia reenvía ``fase`` tal cual (solo descarta ``None``) y el supervisor no
    atrapa excepciones, así que un valor no hashable cruzaba la frontera como traceback.
    """
    servicio = _servicio()
    servicio._ensure_preflight = MagicMock()  # type: ignore[method-assign]
    servicio.preparar_etapa_asistida = AsyncMock()
    servicio.verificar_etapa_asistida = AsyncMock()

    resultado = await servicio.ejecutar_fase_asistida(profile="P", fase=fase_invalida)

    assert resultado["success"] is False
    assert resultado["reason"] == "FaseInvalida"
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
# 4. El estado del task y el toast: ``pendiente`` NO es un fallo
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
    """La fase 2 llega por la tool y es honesta: sin fase 1 previa no prueba frescura.

    El LLM no puede aportar ``testigo_previo`` (A-4), y si nadie corrió ``preparar`` antes
    el almacén durable está vacío: un artefacto correcto da verde con
    ``frescura_probada=False`` en vez de inventar un testigo.
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


@pytest.mark.asyncio
async def test_sin_perfil_la_tool_falla_cerrado_en_vez_de_adivinar_un_default(tmp_path: pathlib.Path) -> None:
    """``profile`` no tiene default: inventar "Default" prepararía otra instancia.

    El registry SIEMPRE pasa ``mo2_profile``; un caller nuevo que lo omita tiene que
    recibir ``PerfilInvalido`` —la verdad— y no instrucciones compuestas contra un perfil
    que puede no existir en la instancia MO2 del operador.
    """
    game, _ = _perfil(tmp_path, perfil="Requiem")

    salida = json.loads(await generate_bashed_patch(path_resolver=_resolver(game, tmp_path)))

    assert salida["success"] is False
    assert salida["reason"] == "PerfilInvalido"


@pytest.mark.asyncio
async def test_las_rutas_que_ve_el_llm_estan_saneadas(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """El contrato asistido devuelve rutas compuestas con nombres de MODS: son texto de terceros.

    Antes del PR B esta tool saneaba el ``stdout``/``stderr`` del runner. Esa rama se
    retiró con el cableado, pero la superficie de inyección no desapareció: los nombres
    de carpeta de mods llegan al prompt dentro de ``artefacto``. Windows prohíbe ``<`` y
    ``>`` en un nombre, así que el vector no es el marcador ASCII sino lo que NFKC
    colapsa (delimitadores de ancho completo) y lo que Windows SÍ permite (zero-width y
    bidi). Se construye ese nombre y se afirma que no sobrevive al boundary del tool.
    """
    perfil_con_veneno = "Requiem"
    mod = "Salida\u200b\uff1ctool_call\uff1e"
    game, plugins = _perfil(tmp_path, perfil=perfil_con_veneno)
    (plugins.parent / "modlist.txt").write_text(f"+{mod}\n", encoding="utf-8")
    parche = tmp_path / "mods" / mod / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True)
    parche.write_bytes(b"TES4" + b"\x00" * 60)

    salida = json.loads(
        await generate_bashed_patch(
            path_resolver=_resolver(game, tmp_path),
            profile=perfil_con_veneno,
        )
    )

    assert salida["pendiente"] is True
    # Control positivo: el veneno EXISTE en la ruta real. Sin esto la prueba no podría
    # fallar nunca (el artefacto podría haberse resuelto en otra capa sin nombre de mod).
    assert "\u200b" in str(parche) and "\uff1c" in str(parche)

    # Se afirma sobre el VALOR y no sobre `json.dumps(salida)`: con `ensure_ascii` (el
    # default) el JSON escapa el zero-width como la secuencia `\u200b` de seis
    # caracteres, así que buscar el carácter dentro del JSON siempre da negativo y el
    # test sería decorativo. Esta fue la primera versión, y la mutación lo demostró.
    artefacto = salida["artefacto"]
    assert isinstance(artefacto, str)
    assert "\u200b" not in artefacto, "el zero-width del nombre del mod llegó al prompt"
    assert "\uff1c" not in artefacto, "el delimitador de ancho completo llegó al prompt"
    # Oráculo fuerte: es exactamente la ruta saneada, no una ruta "parecida".
    assert artefacto == sanitize_for_prompt(str(parche))
    assert artefacto != str(parche), "el saneo no cambió nada: el test no está midiendo"


def test_el_preflight_adjunto_se_sanea_en_la_copia_que_ve_el_llm() -> None:
    """``preflight.checks[].details`` lleva nombres de archivo del overwrite: texto de terceros.

    ``_sanear_rutas`` solo cubría ``artefacto``/``plugins_txt``; el reporte amarillo
    adjunto cruzaba al prompt sin pasar por ``sanitize_for_prompt``.
    """
    from sky_claw.app.agent.tools.system_tools import _sanear_rutas

    veneno = "x\u200b\uff1ctool_call\uff1e.esp"
    original = {
        "success": False,
        "pendiente": True,
        "preflight": {
            "status": "yellow",
            "blocks_mutations": False,
            "checks": [
                {"name": "overwrite", "status": "yellow", "summary": f"hay {veneno}", "details": [veneno, "ok"]}
            ],
        },
    }
    esperado_original = copy.deepcopy(original)

    saneado = _sanear_rutas(original)

    check = saneado["preflight"]["checks"][0]
    assert check["details"] == [sanitize_for_prompt(veneno), "ok"]
    assert check["summary"] == sanitize_for_prompt(f"hay {veneno}")
    assert check["details"][0] != veneno, "el saneo no cambió nada: el test no está midiendo"
    # Estructura no textual intacta y el original sin mutar (el journal lo comparte).
    assert saneado["preflight"]["status"] == "yellow"
    assert saneado["preflight"]["blocks_mutations"] is False
    assert original == esperado_original


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


@pytest.mark.asyncio
async def test_el_llm_no_puede_colar_el_perfil_ni_el_testigo(tmp_path: pathlib.Path) -> None:
    """`extra="forbid"`: el rechazo es explícito, no un silencio.

    Con `extra="ignore"` (el default de pydantic) un `profile="Otro"` del modelo se
    descartaba sin decirle nada: el LLM creería haber elegido el perfil, que es
    exactamente lo que la spec de #454 prohíbe. Y un `testigo_previo` inventado pasaría
    por la puerta como si fuera evidencia de la fase 1.
    """
    registry = _registry(tmp_path, perfil="Requiem")

    for campo in ({"profile": "Otro"}, {"testigo_previo": {"sha256": "c" * 64}}):
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("generate_bashed_patch", campo)


# ==============================================================================
# 6. Retiro de la compuerta HITL (ADR 0016 §7): ambas fases son no destructivas
# ==============================================================================


def test_generate_bashed_patch_no_es_destructiva_ni_declara_protocolos_hitl() -> None:
    """Tras el retiro de ``execute_pipeline`` (#862), ninguna de las dos fases muta el
    parche, el perfil ni el load order: ``generate_bashed_patch`` sale de
    ``DESTRUCTIVE_TOOL_PATTERNS`` y ``GenerateBashedPatchStrategy`` no declara
    protocolos opcionales de prompt HITL sin consumidor."""
    from sky_claw.app.orchestrator.tool_strategies.base import (
        ApprovalPayloadDescriber,
        ApprovalPayloadValidator,
    )
    from sky_claw.app.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS

    assert "generate_bashed_patch" not in DESTRUCTIVE_TOOL_PATTERNS
    estrategia = GenerateBashedPatchStrategy(wrye_bash_pipeline=AsyncMock())
    assert not isinstance(estrategia, ApprovalPayloadDescriber)
    assert not isinstance(estrategia, ApprovalPayloadValidator)


@pytest.mark.asyncio
async def test_dispatcher_ejecuta_preparar_y_verificar_sin_hitl_guard(
    tmp_path: pathlib.Path, sin_preflight: None
) -> None:
    """Cierre de la deuda declarada en ADR 0016 §7: con ``HitlGateMiddleware()`` sin
    ``HITLGuard`` (el default fail-closed que niega con ``HITLGateUnavailable`` toda
    herramienta destructiva), el dispatcher real ejecuta ``preparar`` y ``verificar``
    mientras sigue denegando a los rituales destructivos."""
    from sky_claw.app.orchestrator.dispatcher_dependencies import build_wrye_bash_pipeline
    from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
    from sky_claw.local.tools.etapa6_testigos import AlmacenEnMemoria
    from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

    game, _ = _perfil(tmp_path, perfil="Requiem")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir(parents=True, exist_ok=True)
    servicio = WryeBashPipelineService(
        path_resolver=_resolver(game, tmp_path),
        testigo_store=AlmacenEnMemoria(),
    )
    sm = ToolStateMachine()
    dispatcher = build_orchestration_dispatcher(
        crear_dependencias_dispatcher(
            wrye_bash_pipeline=build_wrye_bash_pipeline(service=servicio, default_profile="Requiem"),
        ),
        hitl_gate=HitlGateMiddleware(),  # sin HITLGuard, allow_unattended=False
        idempotency=IdempotencyMiddleware(state_machine=sm),
    )

    # 1. En el mismo dispatcher sin guard, un ritual destructivo sigue fallando cerrado.
    denegado = await dispatcher.dispatch("execute_loot_sorting", {})
    assert denegado["status"] == "error"
    assert denegado["reason"] == "HITLGateUnavailable"

    # 2. Fase 1 (preparar) corre sin HITLGuard y registra AWAITING_OPERATOR.
    prep = await dispatcher.dispatch("generate_bashed_patch", {"fase": "preparar"})
    assert prep["pendiente"] is True
    assert prep["success"] is False

    # 3. El operador construye el Bashed Patch en MO2/Wrye Bash y Fase 2 (verificar)
    #    corre sin HITLGuard probando frescura y cerrando en COMPLETED.
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_HITL_FREE_" + b"\x11" * 49)
    ver = await dispatcher.dispatch("generate_bashed_patch", {"fase": "verificar"})
    assert ver["success"] is True
    assert ver["frescura_probada"] is True
    estados = [t.state for t in sm._tasks.values()]
    assert "AWAITING_OPERATOR" in estados
    assert estados[-1] == "COMPLETED"


@pytest.mark.asyncio
async def test_ritual_gui_preparar_y_verificar_con_modo_local_apagado_no_abre_modal_hitl(
    tmp_path: pathlib.Path, sin_preflight: None
) -> None:
    """Superficie GUI real (``run_ritual`` + ``run_ritual_resume`` con ``auto_approve=False``,
    "Modo local" apagado): ni pulsar «Crear Parche» ni pulsar «Ya construí el parche —
    verificar» estacionan una aprobación en ``STORE_KEY_PENDING_HITL`` ni invocan ``HITLGuard``."""
    from sky_claw.app.gui.controllers.ritual_runner import (
        STORE_KEY_PENDING_HITL,
        STORE_KEY_RITUAL_FEEDBACK,
        enqueue_pending_hitl,
        make_gui_hitl_notify,
        resolve_ritual_resume_action,
        ritual_auto_approve_armed,
        ritual_tab_id,
        run_ritual,
        run_ritual_resume,
    )
    from sky_claw.app.gui.state.reactive_store import ReactiveStore
    from sky_claw.app.orchestrator.dispatcher_dependencies import build_wrye_bash_pipeline
    from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.local.tools.etapa6_testigos import AlmacenEnMemoria
    from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

    game, _ = _perfil(tmp_path, perfil="Requiem")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir(parents=True, exist_ok=True)
    servicio = WryeBashPipelineService(
        path_resolver=_resolver(game, tmp_path),
        testigo_store=AlmacenEnMemoria(),
    )

    store = ReactiveStore()
    notificaciones_hitl: list[Any] = []
    guard_holder: dict[str, HITLGuard] = {}

    async def _respond(rid: str, approved: bool) -> None:
        await guard_holder["g"].respond(rid, approved=approved)

    def _park(payload: dict[str, Any]) -> None:
        notificaciones_hitl.append(payload)
        enqueue_pending_hitl(store, payload)

    guard = HITLGuard(
        notify_fn=make_gui_hitl_notify(
            respond=_respond,
            set_pending=_park,
            auto_approve_getter=ritual_auto_approve_armed,
            tab_id_getter=ritual_tab_id,
            delegate=None,
        ),
        timeout=1,
    )
    guard_holder["g"] = guard
    dispatcher = build_orchestration_dispatcher(
        crear_dependencias_dispatcher(
            wrye_bash_pipeline=build_wrye_bash_pipeline(service=servicio, default_profile="Requiem"),
        ),
        hitl_gate=HitlGateMiddleware(hitl_guard=guard),
        idempotency=IdempotencyMiddleware(state_machine=ToolStateMachine()),
    )

    class _SupervisorConDispatcherReal:
        async def dispatch_tool(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return await dispatcher.dispatch(tool_name, payload)

    sup = _SupervisorConDispatcherReal()

    # Fase 1 desde botón del ritual con Modo local APAGADO (auto_approve=False): no bloquea en modal.
    await run_ritual("wrye_bash", supervisor=sup, store=store, auto_approve=False, tab_id="tab-1")
    assert notificaciones_hitl == []
    assert store.get(STORE_KEY_PENDING_HITL) is None
    assert store.get(STORE_KEY_RITUAL_FEEDBACK)["type"] == "warning"

    accion = resolve_ritual_resume_action(store, "tab-1")
    assert accion is not None

    # El operador construye el parche y pulsa el CTA de Fase 2 también con auto_approve=False.
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_GUI_REAL_" + b"\x22" * 50)
    await run_ritual_resume(
        "wrye_bash",
        payload=accion["payload"],
        supervisor=sup,
        store=store,
        auto_approve=False,
        tab_id="tab-1",
    )
    assert notificaciones_hitl == []
    assert store.get(STORE_KEY_PENDING_HITL) is None
    assert store.get(STORE_KEY_RITUAL_FEEDBACK)["type"] == "positive"


# ==============================================================================
# 3. Auditoría: el resultado de cada fase queda en el log (#880 H-5)
# ==============================================================================


_RUTA_SENSIBLE = "C:\\Users\\operador\\SkyrimSE\\Data\\Bashed Patch, 0.esp"


@pytest.mark.asyncio
@pytest.mark.parametrize("fase", ["preparar", "verificar"])
async def test_el_enrutado_registra_el_resultado_de_cada_fase_sin_rutas(
    fase: str, caplog: pytest.LogCaptureFixture
) -> None:
    """El punto único loguea los campos de auditoría y NUNCA rutas ni mensajes libres.

    La GUI no devolvía el JSON de ``verificar`` a ningún log, así que una validación real
    por GUI no dejaba evidencia. Como las tres superficies pasan por
    ``ejecutar_fase_asistida``, loguear ahí cubre a todas a la vez (no un camino y su gemelo).
    """
    servicio = _servicio()
    resultado = {
        "success": False,
        "message": f"No se encontró {_RUTA_SENSIBLE}",
        "motivo": "artefacto_ausente",
        "artefacto": _RUTA_SENSIBLE,
        "plugins_txt": _RUTA_SENSIBLE,
        "reason": "ArtefactoAusente",
        "estado_en_plugins": "ausente",
        "frescura_probada": False,
        "testigo_origen": "durable",
        "testigo_contexto": "coincide",
        "testigo_durable": "guardado",
    }
    servicio.preparar_etapa_asistida = AsyncMock(return_value=dict(resultado))
    servicio.verificar_etapa_asistida = AsyncMock(return_value=dict(resultado))

    with caplog.at_level("INFO", logger="sky_claw.local.tools.wrye_bash_service"):
        devuelto = await servicio.ejecutar_fase_asistida(profile="P", fase=fase)

    assert devuelto == resultado  # el log no altera lo que ve el llamador
    registros = [r for r in caplog.records if getattr(r, "fase_asistida", None) == fase]
    assert len(registros) == 1, "debe haber exactamente un registro de auditoría por fase"
    # Igualdad exacta del dict logueado (clave → valor): lo que entra y lo que no. Comparar
    # strings sueltos en el texto daría verde con un valor bajo la clave equivocada.
    campos = registros[0].args[1]  # type: ignore[index]
    # Y el mismo dict viaja como campo estructurado para el log JSON, no solo en el mensaje.
    assert registros[0].resultado_asistido == campos  # type: ignore[attr-defined]
    assert campos == {
        "success": False,
        "reason": "ArtefactoAusente",
        "motivo": "artefacto_ausente",
        "frescura_probada": False,
        "testigo_origen": "durable",
        "testigo_contexto": "coincide",
        "testigo_durable": "guardado",
        "estado_en_plugins": "ausente",
    }
    for excluida in ("message", "artefacto", "plugins_txt"):
        assert excluida not in campos, f"{excluida!r} puede llevar rutas y no va al log"
    assert _RUTA_SENSIBLE not in caplog.text
    assert "operador" not in caplog.text
