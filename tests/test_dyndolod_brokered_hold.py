"""E3-R3: el ``WIRING_SECURITY_HOLD`` se aplica en CÓDIGO, no sólo en la documentación.

El backend ``brokered`` quedó seleccionable con una línea de ``config.toml``. Mientras la
contención del worker en el Job Object no esté VERIFICADA de punta a punta (la implementan el
fence de supervisión #856, la identidad del Job #860/#872 y la adopción de descendientes #877;
faltan la evidencia sobre el binario final y el visto bueno del auditor independiente), esa
línea no puede bastar para lanzar TexGen/DynDOLOD. Una prosa en el CHANGELOG no detiene a un
operador; una barrera sí.

Propiedades que fijan estos tests (cada una con su ancla, no con un recordatorio):

* **Con la barrera activa, pedir ``brokered`` devuelve el guard fail-closed del HOLD**, incluso
  con broker y rutas completos y coherentes: la strategy real no se construye, el broker no
  recibe ninguna sesión y no hay subprocess local (jamás fallback a standalone).
* **La barrera es INTERNA**: una constante literal del módulo, no una clave de config, ni una
  variable de entorno, ni un parámetro del resolver. Sólo la levanta un ``monkeypatch`` en los
  tests que prueban el cableado (``test_dyndolod_brokered_wiring.py``).
* **Standalone y la configuración inválida no cambian**: ``None`` para standalone; el guard
  de "backend no válido" (con su propio motivo) para un valor desconocido.
* **Hermanos cerrados por enumeración**: quién construye la strategy real, quién llama a quién y
  quién lee o toca la constante en producción y en los tests están congelados por igualdad literal.

La constante NO pasa a ``True`` por fusionar un PR de contención: el fence (#856), la identidad
del Job (#860/#872) y la adopción (#877) ya están en ``main``. Habilitarla exige la contención
verificada de punta a punta (evidencia sobre el binario final y visto bueno del auditor
independiente) y se hace en un PR aparte, con revisión adversarial.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import pathlib
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sky_claw.local.mo2 import brokered_dyndolod as brokered_mod
from sky_claw.local.mo2.brokered_dyndolod import (
    DYNDOLOD_BACKEND_BROKERED,
    DYNDOLOD_BACKEND_STANDALONE,
    BrokeredDynDOLODSpawnStrategy,
    VfsRequiredDynDOLODSpawnStrategy,
    resolve_dyndolod_spawn_strategy,
)
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODRunner,
    ReadinessMode,
    normalize_dyndolod_spawn_strategy,
)
from sky_claw.local.tools.texgen_handoff import TexGenHandoffRequest
from tests.test_dyndolod_brokered_wiring import (
    ENTRADAS_OBLIGATORIAS,
    PAQUETE,
    REPO,
    _BrokerFalso,
    _desafio,
    _Escenario,
    _escenario,
    _kwargs,
    _spawn,
)

BARRERA = "_CONTENCION_DEL_WORKER_VERIFICADA"
AUDITORIA = "docs/audits/2026-11-01_e3r3_job_object_supervision_fence.md"
MODULO = "sky_claw/local/mo2/brokered_dyndolod.py"

# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


@pytest.fixture
def sin_subprocess_local(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Cualquier subprocess local del daemon es el fallback que NO debe ocurrir."""
    llamadas: list[tuple[Any, ...]] = []

    async def _prohibido(*args: Any, **_kwargs: Any) -> Any:
        llamadas.append(args)
        raise AssertionError(f"fallback a subprocess local prohibido: {args!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _prohibido)
    return llamadas


@pytest.fixture
def sin_construccion_real(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Si algo construye la strategy real (o su attestation) con la barrera activa, queda registrado."""
    construcciones: list[str] = []

    def _espia(nombre: str) -> Any:
        def _prohibido(*_args: Any, **_kwargs: Any) -> Any:
            construcciones.append(nombre)
            raise AssertionError(f"con la barrera activa no se construye nada brokered: {nombre}")

        return _prohibido

    for nombre in ("_construir_brokered", "build_brokered_dyndolod_spawn_strategy", "build_attestation_challenge"):
        monkeypatch.setattr(brokered_mod, nombre, _espia(nombre))
    return construcciones


@pytest.fixture
def sin_sesion_en_el_broker(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """El broker REAL no puede recibir ni una sesión ni un submit."""
    espias = {
        "open_session": AsyncMock(side_effect=AssertionError("el broker no debía abrir una sesión")),
        "submit": AsyncMock(side_effect=AssertionError("el broker no debía recibir un submit")),
    }
    for nombre, espia in espias.items():
        monkeypatch.setattr(VfsExecutionBroker, nombre, espia)
    return espias


@contextlib.asynccontextmanager
async def _broker_real(esc: _Escenario, tmp_path: pathlib.Path) -> AsyncIterator[VfsExecutionBroker]:
    """Broker REAL, arrancado y de la MISMA instancia que el install_root: no hay excusa para el rechazo."""
    broker = VfsExecutionBroker(
        instance_id=esc.instance_id,
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _ruta: None,
    )
    await broker.start()
    try:
        yield broker
    finally:
        await broker.close()


def _es_el_guard_del_hold(resultado: object) -> VfsRequiredDynDOLODSpawnStrategy:
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy), (
        f"con la barrera activa pedir 'brokered' debía devolver el guard del HOLD; devolvió {resultado!r}"
    )
    assert "WIRING_SECURITY_HOLD" in resultado.reason
    assert "Job Object" in resultado.reason
    assert AUDITORIA in resultado.reason
    return resultado


def _runner(esc: _Escenario, estrategia: Any) -> DynDOLODRunner:
    return DynDOLODRunner(
        DynDOLODConfig(
            game_path=esc.game_data_dir.parent,
            mo2_path=esc.install_root,
            mo2_mods_path=esc.mods_dir,
            dyndolod_exe=esc.dyndolod_exe,
            texgen_exe=esc.texgen_exe,
            external_work_root=esc.external_work_root,
        ),
        readiness=ReadinessMode.DISABLED_FOR_TEST,
        spawn_strategy=estrategia,
    )


# ---------------------------------------------------------------------------
# 1. Con la barrera activa: ni la strategy real, ni sesión en el broker, ni fallback
# ---------------------------------------------------------------------------


def test_la_barrera_nace_activa() -> None:
    # Arrange / Act / Assert: igualdad literal. Cambiarla a True rompe ESTE test a propósito:
    # quien lo haga debe leer el docstring del módulo y cumplir las condiciones de habilitación.
    assert getattr(brokered_mod, BARRERA) is False


def test_brokered_completo_y_coherente_devuelve_el_guard_del_hold_sin_construir_la_strategy(
    tmp_path: pathlib.Path, sin_construccion_real: list[str]
) -> None:
    # Arrange: TODO está disponible y es coherente (broker de la instancia correcta, perfil, raíces).
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker))

    # Assert: el guard del HOLD, nada construido y el broker intacto.
    _es_el_guard_del_hold(resultado)
    assert not isinstance(resultado, BrokeredDynDOLODSpawnStrategy)
    assert sin_construccion_real == []
    assert broker.aperturas == [] and broker.sumisiones == []


def test_levantar_la_barrera_con_monkeypatch_restituye_la_strategy_real(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contraste: las MISMAS entradas producen la strategy real sin la barrera (el test anterior no es vacuo)."""
    # Arrange
    esc = _escenario(tmp_path)
    monkeypatch.setattr(brokered_mod, BARRERA, True)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso()))

    # Assert
    assert isinstance(resultado, BrokeredDynDOLODSpawnStrategy)


@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
async def test_con_un_broker_real_y_la_barrera_activa_ninguna_herramienta_se_lanza(
    tmp_path: pathlib.Path,
    herramienta: str,
    sin_subprocess_local: list[tuple[Any, ...]],
    sin_sesion_en_el_broker: dict[str, AsyncMock],
) -> None:
    # Arrange: broker REAL, de la instancia correcta, con todas las raíces válidas.
    esc = _escenario(tmp_path)
    async with _broker_real(esc, tmp_path) as broker:
        guard = _es_el_guard_del_hold(resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker)))

        # Act / Assert 1: el spawn falla con el error tipado del dominio, nombrando el HOLD.
        with pytest.raises(DynDOLODExecutionError, match="WIRING_SECURITY_HOLD"):
            await _spawn(guard, esc, herramienta)

    # Assert 2: el broker no recibió nada y no hubo fallback a un subprocess local.
    sin_sesion_en_el_broker["open_session"].assert_not_called()
    sin_sesion_en_el_broker["submit"].assert_not_called()
    assert sin_subprocess_local == []


async def test_el_runner_con_el_guard_del_hold_no_lanza_texgen_ni_cae_a_standalone(
    tmp_path: pathlib.Path, sin_subprocess_local: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: el runner de producción con la strategy que el resolver entrega (sin atajos).
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    monkeypatch.setattr(brokered_mod, "build_attestation_challenge", lambda **_kw: _desafio())
    guard = _es_el_guard_del_hold(resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker)))
    runner = _runner(esc, guard)
    assert normalize_dyndolod_spawn_strategy(guard) is guard, "el guard conserva su identidad: no se vuelve standalone"

    # Act / Assert: TexGen no arranca (error tipado del dominio), y nada llegó al broker ni al SO.
    with pytest.raises(DynDOLODExecutionError, match="WIRING_SECURITY_HOLD"):
        await runner.run_texgen()
    assert broker.aperturas == [] and broker.sumisiones == []
    assert sin_subprocess_local == []


async def test_el_gate_de_handoff_bloquea_con_el_motivo_del_hold(tmp_path: pathlib.Path) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    guard = _es_el_guard_del_hold(resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso())))

    # Act
    veredicto = await guard.verify_texgen_handoff(
        TexGenHandoffRequest(
            mod_name="TexGen Output",
            staging=esc.cwd,
            data_dir=esc.game_data_dir,
            expected_profile="Default",
        )
    )

    # Assert: bloqueo duro (DynDOLOD no se lanza), no una acción humana pendiente.
    assert veredicto.verified is False
    assert veredicto.pending_action is None
    assert "WIRING_SECURITY_HOLD" in veredicto.reason


def test_con_la_barrera_activa_toda_entrada_faltante_devuelve_el_guard_del_hold(
    tmp_path: pathlib.Path, sin_construccion_real: list[str]
) -> None:
    """Enumera TODAS las entradas del resolver (por introspección): ninguna combinación llega a construir."""
    # Arrange
    esc = _escenario(tmp_path)
    entradas = [n for n in inspect.signature(resolve_dyndolod_spawn_strategy).parameters if n != "backend"]
    assert set(entradas) == {"broker", *ENTRADAS_OBLIGATORIAS}, "una entrada nueva del resolver entra a esta grilla"

    # Act / Assert: ninguna ausente, cada una ausente y todas ausentes.
    for ausentes in [(), *[(n,) for n in entradas], tuple(entradas)]:
        entradas_pedidas = _kwargs(esc, _BrokerFalso())
        entradas_pedidas.update({n: None for n in ausentes})  # incluye ``broker``: no es un argumento de _kwargs
        resultado = resolve_dyndolod_spawn_strategy(**entradas_pedidas)
        assert resultado is not None, f"None (= standalone) con ausentes={ausentes}"
        _es_el_guard_del_hold(resultado)
    assert sin_construccion_real == []


# ---------------------------------------------------------------------------
# 2. Standalone y configuración inválida: sin cambios
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [DYNDOLOD_BACKEND_STANDALONE, "  Standalone ", "STANDALONE"])
def test_standalone_sigue_devolviendo_none_sin_tocar_nada_brokered(
    tmp_path: pathlib.Path, valor: str, sin_construccion_real: list[str]
) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker, backend=valor))

    # Assert
    assert resultado is None
    assert sin_construccion_real == []
    assert broker.aperturas == [] and broker.sumisiones == []


@pytest.mark.parametrize("valor", ["brokerd", "", "   ", "brokered,standalone", 3, None, ["brokered"], True])
def test_un_backend_invalido_conserva_su_propio_guard_y_no_lo_pisa_el_hold(
    tmp_path: pathlib.Path, valor: object
) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso(), backend=valor))

    # Assert: sigue siendo el guard de «valor no válido» (con su motivo), no el del HOLD.
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    assert "no es válido" in resultado.reason
    assert "WIRING_SECURITY_HOLD" not in resultado.reason


# ---------------------------------------------------------------------------
# 3. La barrera es INTERNA: ni config, ni entorno, ni parámetro
# ---------------------------------------------------------------------------


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def test_la_barrera_es_un_literal_false_del_modulo_y_no_se_deriva_de_nada() -> None:
    # Arrange / Act
    asignaciones = [
        n
        for n in _arbol(PAQUETE / "local" / "mo2" / "brokered_dyndolod.py").body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == BARRERA for t in n.targets)
    ]

    # Assert: una sola asignación, a nivel de módulo, con el literal False (no config/env/llamada).
    assert len(asignaciones) == 1
    valor = asignaciones[0].value
    assert isinstance(valor, ast.Constant) and valor.value is False, ast.unparse(valor)


def test_la_barrera_no_es_configurable_por_el_usuario(tmp_path: pathlib.Path) -> None:
    # Arrange
    from sky_claw.config import Config

    # Act
    parametros = set(inspect.signature(resolve_dyndolod_spawn_strategy).parameters)
    claves = list(Config(tmp_path / "config.toml")._data)

    # Assert 1: el resolver no admite un override; Assert 2: ninguna clave de config la expone.
    assert parametros == {"backend", "broker", *ENTRADAS_OBLIGATORIAS}
    assert [c for c in claves if re.search(r"hold|fence|contencion|supervis", c, re.IGNORECASE)] == []
    assert "dyndolod_backend" in claves, "la clave que SÍ es del usuario sigue siendo sólo el backend pedido"


def _usos_de(nombre: str, raiz: pathlib.Path) -> list[tuple[str, int, str]]:
    """``(archivo relativo, línea, tipo)`` de cada Name / atributo / cadena que sea ``nombre`` bajo ``raiz``."""
    usos: list[tuple[str, int, str]] = []
    for ruta in sorted(raiz.rglob("*.py")):
        for nodo in ast.walk(_arbol(ruta)):
            if isinstance(nodo, ast.Name) and nodo.id == nombre:
                tipo = type(nodo.ctx).__name__
            elif isinstance(nodo, ast.Attribute) and nodo.attr == nombre:
                tipo = "Attribute"
            elif isinstance(nodo, ast.Constant) and nodo.value == nombre:
                tipo = "Cadena"
            else:
                continue
            usos.append((ruta.relative_to(REPO).as_posix(), nodo.lineno, tipo))
    return usos


def test_en_produccion_solo_el_resolver_lee_la_barrera_y_nadie_mas_la_toca() -> None:
    # Arrange / Act
    usos = _usos_de(BARRERA, PAQUETE)

    # Assert: la definición (Store) y UNA lectura (Load) en el resolver — enumerado, no muestreado.
    assert sorted((archivo, tipo) for archivo, _linea, tipo in usos) == [(MODULO, "Load"), (MODULO, "Store")]
    resolver = next(
        n
        for n in ast.walk(_arbol(PAQUETE / "local" / "mo2" / "brokered_dyndolod.py"))
        if isinstance(n, ast.FunctionDef) and n.name == "resolve_dyndolod_spawn_strategy"
    )
    lecturas = [linea for _archivo, linea, tipo in usos if tipo == "Load"]
    assert resolver.lineno < lecturas[0] <= resolver.end_lineno  # type: ignore[operator]


def test_solo_estos_tests_pueden_nombrar_la_barrera() -> None:
    # Arrange / Act
    archivos = sorted({archivo for archivo, _l, _t in _usos_de(BARRERA, REPO / "tests")})

    # Assert: quien levanta la barrera lo hace por monkeypatch controlado, y está enumerado.
    assert archivos == ["tests/test_dyndolod_brokered_hold.py", "tests/test_dyndolod_brokered_wiring.py"]


def test_la_barrera_se_evalua_antes_de_construir_la_strategy_y_devuelve_el_guard() -> None:
    # Arrange
    resolver = next(
        n
        for n in ast.walk(_arbol(PAQUETE / "local" / "mo2" / "brokered_dyndolod.py"))
        if isinstance(n, ast.FunctionDef) and n.name == "resolve_dyndolod_spawn_strategy"
    )

    # Act
    condiciones = [
        n
        for n in ast.walk(resolver)
        if isinstance(n, ast.If) and any(isinstance(x, ast.Name) and x.id == BARRERA for x in ast.walk(n.test))
    ]
    construcciones = [
        n.lineno
        for n in ast.walk(resolver)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_construir_brokered"
    ]

    # Assert: UNA condición, que corta con el guard, y ANTES de la construcción.
    assert len(condiciones) == 1 and len(construcciones) == 1
    cuerpo = condiciones[0].body
    assert len(cuerpo) == 1 and isinstance(cuerpo[0], ast.Return)
    retorno = cuerpo[0].value
    assert isinstance(retorno, ast.Call) and ast.unparse(retorno.func) == "_guard"
    assert condiciones[0].lineno < construcciones[0]


def _llamadores_en_produccion(nombre: str) -> list[tuple[str, str]]:
    """``(archivo, función contenedora)`` de cada llamada a ``nombre`` en ``sky_claw/``."""
    encontrados: list[tuple[str, str]] = []
    for ruta in sorted(PAQUETE.rglob("*.py")):
        for funcion in ast.walk(_arbol(ruta)):
            if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for nodo in ast.walk(funcion):
                if isinstance(nodo, ast.Call) and ast.unparse(nodo.func).split(".")[-1] == nombre:
                    encontrados.append((ruta.relative_to(REPO).as_posix(), funcion.name))
    return encontrados


def test_la_strategy_brokered_real_solo_se_alcanza_por_el_resolver() -> None:
    """Cierra la familia: si otro camino de producción construyera la strategy real, saltearía la barrera."""
    # Arrange / Act / Assert: igualdad literal de la cadena completa de llamadores.
    assert _llamadores_en_produccion("BrokeredDynDOLODSpawnStrategy") == [
        (MODULO, "build_brokered_dyndolod_spawn_strategy")
    ]
    assert _llamadores_en_produccion("build_brokered_dyndolod_spawn_strategy") == [(MODULO, "_construir_brokered")]
    assert _llamadores_en_produccion("_construir_brokered") == [(MODULO, "resolve_dyndolod_spawn_strategy")]
    assert _llamadores_en_produccion("resolve_dyndolod_spawn_strategy") == [
        ("sky_claw/app_context.py", "_resolver_estrategia_de_dyndolod")
    ]


# ---------------------------------------------------------------------------
# 4. La ruta productiva COMPARTIDA por la GUI y el agente LLM
# ---------------------------------------------------------------------------


def _mo2_de(esc: _Escenario) -> SimpleNamespace:
    return SimpleNamespace(install_root=esc.install_root, data_root=esc.data_root, mods_dir=esc.mods_dir)


def _resolver_de_appcontext(esc: _Escenario, broker: Any) -> Any:
    from sky_claw.app_context import AppContext

    return AppContext._resolver_estrategia_de_dyndolod(
        local_cfg=SimpleNamespace(dyndolod_backend=DYNDOLOD_BACKEND_BROKERED),
        broker=broker,
        instance_id=esc.instance_id,
        active_profile="Default",
        mo2=_mo2_de(esc),
        game=esc.game_data_dir.parent,
        workspace=SimpleNamespace(root=esc.external_work_root),
    )


def test_appcontext_con_brokered_y_todo_valido_resuelve_el_guard_del_hold(
    tmp_path: pathlib.Path, sin_construccion_real: list[str]
) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()

    # Act: el composition root real, con las raíces de un arranque completo.
    resultado = _resolver_de_appcontext(esc, broker)

    # Assert
    _es_el_guard_del_hold(resultado)
    assert sin_construccion_real == []
    assert broker.aperturas == [] and broker.sumisiones == []


@pytest.fixture
def mo2_para_supervisor(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Patrón de ``test_supervisor_construction.py``: los SQLite caen en tmp."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


async def test_gui_y_agente_comparten_el_servicio_cuyo_spawn_esta_bloqueado_por_el_hold(
    tmp_path: pathlib.Path,
    mo2_para_supervisor: pathlib.Path,
    sin_subprocess_local: list[tuple[Any, ...]],
    sin_sesion_en_el_broker: dict[str, AsyncMock],
) -> None:
    # Arrange: broker REAL; AppContext resuelve la strategy; el supervisor construye el servicio de etapa 9.
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.app_context import AppContext

    esc = _escenario(tmp_path / "esc")
    async with _broker_real(esc, tmp_path) as broker:
        guard = _es_el_guard_del_hold(_resolver_de_appcontext(esc, broker))
        supervisor = SupervisorAgent(
            profile_name="Default",
            path_validator=PathValidator(roots=[mo2_para_supervisor, tmp_path]),
            dyndolod_spawn_strategy=guard,
        )
        ctx = object.__new__(AppContext)
        ctx.servicio_de_etapa9 = None
        ctx.publicar_servicio_de_etapa9(supervisor.dyndolod_service)  # lo que hace el bootloader

        # Act: las DOS superficies llegan al MISMO servicio, que lleva el guard.
        servicio = supervisor.dyndolod_service
        assert supervisor._dispatcher_dependencies.dyndolod_service is servicio  # GUI
        assert ctx.servicio_de_etapa9 is servicio  # agente LLM (generate_lods)
        assert servicio._spawn_strategy is guard

        # Assert: desde ese servicio ninguna herramienta puede iniciarse, ni hay fallback.
        for herramienta in ("TexGen", "DynDOLOD"):
            with pytest.raises(DynDOLODExecutionError, match="WIRING_SECURITY_HOLD"):
                await _spawn(servicio._spawn_strategy, esc, herramienta)

    sin_sesion_en_el_broker["open_session"].assert_not_called()
    sin_sesion_en_el_broker["submit"].assert_not_called()
    assert sin_subprocess_local == []
