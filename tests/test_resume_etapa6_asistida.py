"""Resume de la etapa 6 ASISTIDA desde el panel — cierra el hueco de ADR 0016 §6.

El PR B cabló las **tres superficies de routing** (`dispatcher_dependencies`,
`SupervisorAgent`, tool del agente LLM) al punto único `ejecutar_fase_asistida`. Lo
que quedó afuera —y la revisión adversarial marcó como S-8— es la **cuarta capa**: el
botón del ritual despacha payload vacío, así que siempre cae en `preparar`, y
`resume_action_from_result` sólo conocía `dyndolod`. Consecuencia real, no cosmética:
el operador veía las instrucciones de la fase 1 y **no podía pedir la fase 2 desde la
GUI**; la etapa no se completaba end-to-end sin pasar por el agente LLM o la API.

El contrato que este módulo congela:

1. La acción de resume se **deriva del resultado estructural** (nunca del texto del
   `message`) y **lleva su payload** — el mismo principio de F-001, extendido.
2. `run_ritual_resume` **deja de hardcodear** ``{"run_texgen": False}``: recibe el
   payload de la acción. El hardcode era exactamente lo que hacía que sólo DynDOLOD
   pudiera reanudar, y es la clase de defecto que este repo ya pagó varias veces
   (una superficie con su lógica adentro deja a la hermana sin nada).
3. Una fase 1 que **falló** (preflight rojo, M-04, rutas irresolubles) **no** ofrece
   verificar: no hay nada preparado que verificar.
4. El testigo viaja sólo si es bien formado. Si no, la acción igual se ofrece y la
   fase 2 declara ``frescura_probada=False``: honesto, no inventado.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from sky_claw.app.gui.controllers import ritual_runner as rr_mod
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.orchestrator.dispatcher_dependencies import build_wrye_bash_pipeline
from sky_claw.app.orchestrator.tool_strategies.generate_bashed_patch import (
    GenerateBashedPatchStrategy,
)
from sky_claw.local.tools.wrye_bash_asistida import (
    BASHED_PATCH_NAME,
    PASOS_ASISTIDOS,
    EstadoEnPlugins,
    PreparacionAsistida,
    TestigoPrevio,
    preparacion_como_resultado,
)
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _payload_de_preparar(testigo: dict[str, Any] | None = None) -> dict[str, object]:
    """Resultado REAL de la fase 1, armado por el módulo de dominio (no a mano)."""
    return preparacion_como_resultado(
        PreparacionAsistida(
            artefacto="C:/mo2/overwrite/Bashed Patch, 0.esp",
            plugins_txt="C:/mo2/profiles/Requiem/plugins.txt",
            huella_previa=None,
            testigo_previo=(
                TestigoPrevio(existia=True, sha256="a" * 64) if testigo is None else TestigoPrevio(**testigo)
            ),
            estado_plugins=EstadoEnPlugins.AUSENTE,
            instrucciones=PASOS_ASISTIDOS,
        )
    )


class _FakeSupervisor:
    """Boundary real de `run_ritual`: registra (tool, payload) y devuelve un result."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result if result is not None else {"success": True}

    async def dispatch_tool(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, dict(payload)))
        return dict(self.result)


# ==============================================================================
# 1. La acción de resume de la etapa 6: derivada del resultado y con su payload
# ==============================================================================


def test_la_fase_1_ofrece_verificar_con_el_testigo() -> None:
    """El operador que ve las instrucciones recibe el botón que cierra la etapa.

    El `testigo_previo` viaja en el payload porque es lo único que permite PROBAR
    frescura en la fase 2 (comparar contra lo que había antes del build). Sin
    llevarlo, el botón daría verdes con ``frescura_probada=False``.
    """
    accion = rr_mod.resume_action_from_result("wrye_bash", _payload_de_preparar())

    assert accion is not None
    assert accion["tool_key"] == "wrye_bash"
    assert accion["payload"] == {
        "fase": "verificar",
        "testigo_previo": {"existia": True, "sha256": "a" * 64},
    }
    assert accion["label"]
    # El detalle nombra el artefacto concreto: el operador tiene que saber CUÁL
    # archivo verificar sin leer el message completo.
    assert BASHED_PATCH_NAME in accion["detail"]


def test_la_fase_1_conserva_generacion_en_el_testigo_del_cta() -> None:
    """PR D / Review #5770094299 (P1): el CTA de la GUI preserva ``generacion`` dentro de
    ``testigo_previo`` para que la fase 2 consuma condicionalmente sólo su propia época."""
    payload = _payload_de_preparar()
    generacion = "0123456789abcdef" * 2
    payload["testigo_previo"] = {"existia": True, "sha256": "a" * 64, "generacion": generacion}

    accion = rr_mod.resume_action_from_result("wrye_bash", payload)

    assert accion is not None
    assert accion["payload"] == {
        "fase": "verificar",
        "testigo_previo": {"existia": True, "sha256": "a" * 64, "generacion": generacion},
    }


def test_sin_testigo_la_accion_igual_se_ofrece_sin_inventarlo() -> None:
    """Verificar existencia y estado en `plugins.txt` sigue siendo útil sin testigo."""
    payload = _payload_de_preparar()
    payload["testigo_previo"] = None

    accion = rr_mod.resume_action_from_result("wrye_bash", payload)

    assert accion is not None
    assert accion["payload"] == {"fase": "verificar"}


def test_un_testigo_malformado_no_viaja() -> None:
    """Un testigo inventado es peor que ninguno: el servicio igual lo descartaría,
    pero el payload no debe afirmar frescura que nadie puede probar."""
    payload = _payload_de_preparar()
    payload["testigo_previo"] = {"existia": True}  # sin sha256

    accion = rr_mod.resume_action_from_result("wrye_bash", payload)

    assert accion is not None
    assert "testigo_previo" not in accion["payload"]


@pytest.mark.parametrize(
    ("testigo", "por_que"),
    [
        ({"existia": True, "sha256": ""}, "digest vacío"),
        ({"existia": True, "sha256": "a" * 63}, "digest de 63 caracteres"),
        ({"existia": True, "sha256": "A" * 64}, "mayúsculas (el dominio exige [0-9a-f])"),
        ({"existia": False, "sha256": "a" * 64}, "estado IMPOSIBLE: ausencia con identidad"),
        ({"existia": 1, "sha256": "a" * 64}, "bool que no es bool"),
        ({"existia": True, "sha256": 123}, "identidad que no es texto"),
        (
            {"existia": False, "sha256": None, "generacion": "NO_ES_HEX_32"},
            "generacion con formato inválido",
        ),
        (
            {"existia": False, "sha256": None, "generation": "a" * 32},
            "clave extra desconocida (typo generation)",
        ),
    ],
)
def test_un_testigo_que_el_dominio_rechaza_no_viaja(testigo: dict[str, Any], por_que: str) -> None:
    """La forma canónica es la del DOMINIO, no una aproximación (hallazgo de la auditoría).

    ``TestigoPrevio.__post_init__`` rechaza cinco formas que un validador laxo acepta
    (digest vacío/corto/en mayúsculas, y el estado imposible *ausencia CON identidad*).
    Si el seam las dejara pasar, el botón afirmaría en su payload un testigo que
    ``_testigo_desde_resultado`` descarta en silencio: el operador vería
    ``frescura_probada=False`` después de que el panel le prometió frescura. Dos
    validadores con reglas distintas es el defecto que este repo paga siempre.
    """
    payload = _payload_de_preparar()
    payload["testigo_previo"] = testigo

    accion = rr_mod.resume_action_from_result("wrye_bash", payload)

    assert accion is not None, f"la acción se ofrece igual (sin el testigo): {por_que}"
    assert "testigo_previo" not in accion["payload"], por_que


def test_un_fallo_real_no_ofrece_verificar() -> None:
    """Preflight rojo, M-04 o rutas irresolubles: no hay etapa preparada que cerrar.

    Ofrecer «ya lo construí» después de un bloqueo mandaría al operador a hacer un
    trabajo manual que el propio sistema acaba de declarar inviable, y después a un
    veredicto que sólo puede fallar.
    """
    for reason in ("PreflightBlocked", "SinRutasResolubles", "CoberturaDeOverlayIncompleta"):
        assert (
            rr_mod.resume_action_from_result(
                "wrye_bash", {"success": False, "reason": reason, "message": "no se puede"}
            )
            is None
        ), reason


def test_un_veredicto_no_ofrece_otra_verificacion() -> None:
    """Sin `pendiente` no hay etapa abierta: ni el OK ni el SIN_CAMBIO ofrecen CTA.

    Si el veredicto ofreciera «verificar» otra vez, el panel quedaría en un bucle de
    botones que nunca cierran la etapa.
    """
    assert rr_mod.resume_action_from_result("wrye_bash", {"success": True, "motivo": "OK"}) is None
    assert (
        rr_mod.resume_action_from_result(
            "wrye_bash", {"success": False, "motivo": "SIN_CAMBIO", "message": "no cambió"}
        )
        is None
    )


def test_la_tabla_de_acciones_esta_cerrada_por_igualdad() -> None:
    """Cada tool con acción de resume declara SU payload; una nueva sin entrada no la hereda.

    La igualdad literal (no subconjunto) es la que obliga a decidir el payload de la
    tool nueva en la tabla, en vez de que caiga en el ``else`` de otra.
    """
    tabla = rr_mod._ACCIONES_DE_RESUME

    assert set(tabla) == {"dyndolod", "wrye_bash"}
    for tool_key, constructor in tabla.items():
        accion = constructor({"success": False, "needs_deployment": True, "pendiente": True})
        # Cada constructor decide por SU señal: `needs_deployment` (etapa 9) o
        # `pendiente` (etapa 6). Con las dos presentes no puede haber ambigüedad
        # porque cada uno lee la suya — pero el payload tiene que ser el suyo.
        if accion is not None:
            assert accion["tool_key"] == tool_key
            assert accion["payload"], f"{tool_key} ofrece una acción sin payload"


def test_las_dos_acciones_no_se_contaminan() -> None:
    """`needs_deployment` no dispara la etapa 6, y `pendiente` no dispara DynDOLOD."""
    assert rr_mod.resume_action_from_result("dyndolod", {"success": False, "pendiente": True}) is None
    assert rr_mod.resume_action_from_result("wrye_bash", {"success": False, "needs_deployment": True}) is None


# ==============================================================================
# 2. `run_ritual_resume` despacha el payload de la acción (no un hardcode)
# ==============================================================================


async def test_run_ritual_resume_despacha_la_fase_de_verificacion() -> None:
    """El botón del panel atraviesa el MISMO dispatcher con `fase=verificar`."""
    sup = _FakeSupervisor({"success": True, "motivo": "OK"})
    store = ReactiveStore()
    accion = rr_mod.resume_action_from_result("wrye_bash", _payload_de_preparar())
    assert accion is not None

    await rr_mod.run_ritual_resume("wrye_bash", payload=accion["payload"], supervisor=sup, store=store)

    assert sup.calls == [
        ("generate_bashed_patch", {"fase": "verificar", "testigo_previo": {"existia": True, "sha256": "a" * 64}})
    ]


def test_el_payload_del_resume_es_obligatorio() -> None:
    """Sin default: el hardcode ``{"run_texgen": False}`` no puede volver por omisión.

    Es el contrato que hace que una tool nueva no herede el payload de otra. Un
    default —aunque fuera ``{}``— convertiría un wiring olvidado en un dispatch
    silencioso con la intención equivocada.
    """
    parametro = inspect.signature(rr_mod.run_ritual_resume).parameters["payload"]

    assert parametro.kind is inspect.Parameter.KEYWORD_ONLY
    assert parametro.default is inspect.Parameter.empty


def test_el_payload_del_resume_llega_al_dispatcher_sin_filtrar_en_la_gui() -> None:
    """La GUI no reescribe el payload: es la acción la que lo deriva (F-001).

    Anclas por AST y no por texto: un `ruff format` que parta la línea, o un comentario
    que nombre la expresión, romperían una búsqueda de substring sin que el contrato
    haya cambiado. Es la misma razón por la que la comprobación del rótulo mira
    constantes.
    """
    arbol = ast.parse((RAIZ / "sky_claw/app/gui/views/forge_dashboard.py").read_text(encoding="utf-8"))

    # 1. El payload del botón sale de la acción derivada, no de un literal.
    asignaciones = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.Assign, ast.AnnAssign))
        and any(
            getattr(t, "id", None) == "payload_de_la_accion"
            for t in (nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target])
        )
    ]
    assert len(asignaciones) == 1, "debe haber UNA sola fuente del payload del botón"
    # Estructura, no texto: `dict(<algo>.get("payload") or {})`. Comparar substrings
    # choca con la comilla que elija `ast.unparse`, que es cosmética.
    valor = asignaciones[0].value
    assert isinstance(valor, ast.Call) and getattr(valor.func, "id", None) == "dict", ast.unparse(valor)
    origen = valor.args[0]
    assert isinstance(origen, ast.BoolOp) and isinstance(origen.op, ast.Or), ast.unparse(origen)
    lectura = origen.values[0]
    assert isinstance(lectura, ast.Call) and isinstance(lectura.func, ast.Attribute), ast.unparse(lectura)
    assert ast.unparse(lectura.func.value) == "accion" and lectura.func.attr == "get", ast.unparse(lectura)
    assert ast.literal_eval(lectura.args[0]) == "payload", (
        f"el payload del botón debe salir de la acción derivada, no de un literal: {ast.unparse(valor)!r}"
    )

    # 2. El callback recibe (tool_key, payload) — dos argumentos, el segundo el payload.
    # Se mira DENTRO de `_despachar_resume` —el único despachador, que comparten el
    # toast y la tarjeta del ritual (antes era el closure `_on_resume` del toast)—: el
    # módulo tiene otros `fn(...)` legítimos (el aprobador de HITL) y buscarlos a nivel
    # módulo mezclaba dos contratos.
    on_resume = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == "_despachar_resume"
    ]
    assert len(on_resume) == 1, f"se esperaba UN `_despachar_resume` en la vista, hay {len(on_resume)}"
    llamadas = [
        nodo for nodo in ast.walk(on_resume[0]) if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "fn"
    ]
    assert len(llamadas) == 1, f"se esperaba UNA llamada al callback de resume, hay {len(llamadas)}"
    assert len(llamadas[0].args) == 2, (
        "la vista no le pasa el payload de la acción al callback de resume "
        f"(argumentos: {[ast.unparse(a) for a in llamadas[0].args]})"
    )
    assert "payload_de_la_accion" in ast.unparse(llamadas[0].args[1]), (
        f"el segundo argumento no es el payload derivado: {ast.unparse(llamadas[0].args[1])!r}"
    )

    # 3. El detalle ya no se rotula como TexGen: se afirma sobre las CONSTANTES del AST
    # y no sobre el texto del archivo, porque un comentario que nombre el rótulo (como el
    # que explica este mismo cambio) haría fallar la búsqueda de texto sin que el rótulo
    # exista en el render.
    rotulos = [
        nodo.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and "TexGen Output listo en:" in nodo.value
    ]
    assert rotulos == [], f"la vista sigue rotulando el detalle como TexGen: {rotulos}"


def test_el_wiring_de_la_gui_no_hardcodea_el_payload() -> None:
    """AST: `run_ritual_resume(...)` en `sky_claw_gui.py` recibe el payload por parámetro."""
    arbol = ast.parse((RAIZ / "sky_claw/app/gui/sky_claw_gui.py").read_text(encoding="utf-8"))
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "run_ritual_resume"
    ]

    assert len(llamadas) == 1, f"se esperaba UNA llamada productiva, hay {len(llamadas)}"
    payload = [kw for kw in llamadas[0].keywords if kw.arg == "payload"]
    assert payload, "el wiring no pasa payload: el resume despacharía la intención de otra tool"
    assert isinstance(payload[0].value, ast.Name), (
        "el payload del wiring debe venir del callback (acción derivada), no de un literal"
    )


# ==============================================================================
# 3. End-to-end: el testigo sobrevive GUI → estrategia → servicio y PRUEBA frescura
# ==============================================================================


def _servicio(tmp_path: pathlib.Path) -> tuple[WryeBashPipelineService, pathlib.Path, pathlib.Path]:
    """Servicio real con perfil en disco; preflight aislado (tiene su propia suite)."""
    plugins = tmp_path / "profiles" / "Requiem" / "plugins.txt"
    plugins.parent.mkdir(parents=True, exist_ok=True)
    plugins.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True, exist_ok=True)
    parche = tmp_path / "overwrite" / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True, exist_ok=True)
    parche.write_bytes(b"TES4" + b"\x00" * 60)

    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = tmp_path
    resolver.get_mo2_mods_path_best_effort.return_value = tmp_path / "mods"
    servicio = WryeBashPipelineService(
        path_resolver=resolver,
        preflight=MagicMock(),
    )
    servicio._ensure_preflight = lambda **kwargs: None  # type: ignore[method-assign]
    return servicio, parche, plugins


@pytest.mark.asyncio
async def test_el_testigo_del_boton_prueba_frescura_de_punta_a_punta(tmp_path: pathlib.Path) -> None:
    """GUI → acción → estrategia (filtro de claves) → enrutado → veredicto.

    Es el test que el hueco hacía imposible: sin affordance, la única forma de probar
    frescura era llamar la API a mano. Acá se simula el ciclo completo del operador:
    preparar (fase 1), reconstruir el parche, y pulsar el botón que la GUI deriva.
    """
    servicio, parche, _ = _servicio(tmp_path)
    estrategia = GenerateBashedPatchStrategy(
        wrye_bash_pipeline=build_wrye_bash_pipeline(service=servicio, default_profile="Requiem")
    )

    # 1. El operador aprieta «Generar»: payload vacío ⇒ preparar.
    preparacion = await estrategia.execute({})
    accion = rr_mod.resume_action_from_result("wrye_bash", preparacion)
    assert accion is not None, "la fase 1 no ofreció el botón de verificación"

    # 2. Construye el parche en la GUI de Wrye Bash (fuera de Sky-Claw).
    parche.write_bytes(b"TES4" + b"\x01" * 60)

    # 3. Pulsa el botón: el payload de la acción atraviesa el filtro de la estrategia.
    veredicto = await estrategia.execute(accion["payload"])

    assert veredicto["success"] is True
    assert veredicto["motivo"] == "OK"
    assert veredicto["frescura_probada"] is True, (
        "el testigo no sobrevivió el round-trip: el veredicto es verde pero no prueba "
        "que ESTA corrida escribió el parche"
    )


@pytest.mark.asyncio
async def test_sin_reconstruir_el_boton_reporta_sin_cambio(tmp_path: pathlib.Path) -> None:
    """Control negativo del end-to-end: si el operador no reconstruyó, el botón lo dice."""
    servicio, _parche, _ = _servicio(tmp_path)
    estrategia = GenerateBashedPatchStrategy(
        wrye_bash_pipeline=build_wrye_bash_pipeline(service=servicio, default_profile="Requiem")
    )

    preparacion = await estrategia.execute({})
    accion = rr_mod.resume_action_from_result("wrye_bash", preparacion)
    assert accion is not None

    veredicto = await estrategia.execute(accion["payload"])

    assert veredicto["success"] is False
    assert veredicto["motivo"] == "SIN_CAMBIO"
    assert veredicto["frescura_probada"] is False
