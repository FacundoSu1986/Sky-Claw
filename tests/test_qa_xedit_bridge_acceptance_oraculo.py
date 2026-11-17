"""Pruebas del ORÁCULO del harness de aceptación real de xEdit Bridge (``scripts/qa``).

No lanzan SSEEdit ni tocan ningún laboratorio: usan ``HITLGuard`` y ``GestorDeAdmisionesNativas`` reales con un
servicio y un registry falsos, y demuestran que el harness DETECTA resultados funcionales incorrectos
(un escenario que no cumple su criterio da FAIL y código de salida distinto de cero).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from sky_claw.app.agent.tools.xedit_bridge_tools import GestorDeAdmisionesNativas, admitir_plugin_nativo
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision

RUTA_HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "qa" / "xedit_bridge_real_acceptance.py"


def _cargar() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("xedit_bridge_real_acceptance_harness", RUTA_HARNESS)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


h = _cargar()
BYTES_FIXTURE = b"FIXTURE-T0"
ID_VALIDO = "20261115-200956-486e92"


class ServicioFalso:
    """Doble de ``XEditBridgeService``: copia de verdad (un archivo) y cuenta las llamadas."""

    def __init__(self, ctx: Any, *, resultado: dict[str, Any] | None = None, contenido: bytes = BYTES_FIXTURE) -> None:
        self.ctx = ctx
        self.llamadas: list[dict[str, Any]] = []
        self.resultado = resultado
        self.contenido = contenido

    def contexto_de_admision(self) -> ContextoAdmision:
        return ContextoAdmision(self.ctx.mo2, self.ctx.perfil)

    async def admitir(self, plugin: str, *, contexto: Any = None, requerir_contexto: bool = False) -> dict[str, Any]:
        self.llamadas.append({"plugin": plugin, "contexto": contexto, "requerir_contexto": requerir_contexto})
        destino = self.ctx.admitidos / "Data" / plugin
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(self.contenido)
        (self.ctx.admitidos / "admissions.json").write_text(json.dumps({"plugins": [plugin]}), encoding="utf-8")
        if self.resultado is not None:
            return {"plugin": plugin, **self.resultado}
        return {"plugin": plugin, "success": True, "message": "", "admission_id": ID_VALIDO, "copiados": [plugin]}


class GuardQueAprueba(HITLGuard):
    """Guard defectuoso: avisa al operador pero devuelve APPROVED sin esperar su decisión."""

    async def request_approval(self, request_id: str | None = None, reason: str = "", **_kw: Any) -> Decision:  # type: ignore[override]
        await self._notify(types.SimpleNamespace(request_id=request_id, reason=reason, category="tool_execution"))
        return Decision.APPROVED


class RegistryFalso:
    """Mismo despacho que ``AsyncToolRegistry`` hacia las tools reales, con servicio y auditor inyectados."""

    def __init__(self, ctx: Any, servicio: ServicioFalso, auditor: Any) -> None:
        self.ctx, self.servicio, self.auditor = ctx, servicio, auditor

    async def execute(self, nombre: str, argumentos: dict[str, Any]) -> str:
        if nombre == "admitir_plugin_nativo":
            return await admitir_plugin_nativo(
                self.servicio, self.ctx.hitl, argumentos["plugin"], admisiones=self.ctx.gestor
            )
        return await self.auditor(argumentos["plugin"])


def _montar(
    tmp_path: Path, *, guard: type[HITLGuard] = HITLGuard, servicio: ServicioFalso | None = None, auditor: Any = None
) -> Any:
    ctx = h.Contexto(tmp_path, tmp_path / "evidencia")
    ctx.armar = lambda timeout_hitl=120.0: None  # type: ignore[assignment]
    ctx.admitidos.mkdir(parents=True)

    async def notify(req: Any) -> None:
        await ctx.solicitudes.put(req)

    ctx.hitl = guard(notify_fn=notify, timeout=30)
    ctx.gestor = GestorDeAdmisionesNativas(notificar=ctx.hitl.notify_operator)
    servicio = servicio or ServicioFalso(ctx)

    async def sin_auditor(_plugin: str) -> str:
        raise AssertionError("no se esperaba una auditoría")

    ctx.registry = RegistryFalso(ctx, servicio, auditor or sin_auditor)
    ctx.servicio = servicio
    return ctx


@pytest.fixture(autouse=True)
def _fixture_conocido(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    sha16 = hashlib.sha256(BYTES_FIXTURE).hexdigest()[:16].upper()
    monkeypatch.setitem(h.SHA16_FIXTURES, h.PLUGIN_VALIDO, sha16)


def _correr(coro: Any) -> Any:
    return asyncio.run(coro)


async def _t1(ctx: Any) -> dict[str, Any]:
    return await h.ejecutar_escenario(ctx, "T1_denegacion", "real", h._escenario_admision(ctx, h.PLUGIN_VALIDO, False))


async def _t2(ctx: Any) -> dict[str, Any]:
    return await h.ejecutar_escenario(ctx, "T2_aprobacion", "real", h._escenario_admision(ctx, h.PLUGIN_VALIDO, True))


# ---------------------------------------------------------------- controles positivos (el oráculo no es siempre FAIL)


def test_controles_positivos_t1_y_t2_pasan_con_el_guard_real(tmp_path: Path) -> None:
    ctx = _montar(tmp_path)
    t1 = _correr(_t1(ctx))
    assert t1["veredicto"] == "PASS", t1["fallas"]
    assert not (ctx.admitidos / "Data").exists()  # denegada: nada copiado

    ctx.avisos.clear()
    t2 = _correr(_t2(ctx))
    assert t2["veredicto"] == "PASS", t2["fallas"]
    assert t2["datos"]["resultado"]["admission_id"] == ID_VALIDO
    assert ctx.servicio.llamadas[0]["contexto"] is not None  # el gestor liga la copia al contexto aprobado


# ---------------------------------------------------------------- Fase 4: pruebas negativas con dobles


def test_negativa_1_una_denegacion_que_termina_en_exito_da_fail(tmp_path: Path) -> None:
    ctx = _montar(tmp_path, guard=GuardQueAprueba)

    registro = _correr(_t1(ctx))

    assert registro["veredicto"] == "FAIL"
    texto = " ".join(registro["fallas"])
    assert "esperado 'rechazada'" in texto
    assert "respond(False) no fue aceptado" in texto
    assert "Admitidos cambió" in texto  # y además copió algo


def test_negativa_2_una_aprobacion_sin_admission_id_da_fail(tmp_path: Path) -> None:
    servicio_ctx = h.Contexto(tmp_path, None)  # solo para construir el servicio con las mismas rutas
    ctx = _montar(
        tmp_path,
        servicio=ServicioFalso(servicio_ctx, resultado={"success": True, "message": "", "copiados": [h.PLUGIN_VALIDO]}),
    )

    registro = _correr(_t2(ctx))

    assert registro["veredicto"] == "FAIL"
    assert any("admission_id inválido" in f for f in registro["fallas"])


def test_negativa_2b_una_copia_que_no_es_el_fixture_da_fail(tmp_path: Path) -> None:
    ctx = _montar(tmp_path)
    ctx.servicio.contenido = b"OTRO CONTENIDO"

    registro = _correr(_t2(ctx))

    assert registro["veredicto"] == "FAIL"
    assert any("no es el fixture esperado" in f for f in registro["fallas"])


def _auditoria(**cambios: Any) -> dict[str, Any]:
    base = {
        "plugin": h.PLUGIN_CON_HALLAZGO,
        "success": True,
        "aprobado": False,
        "clean": False,
        "check_complete": True,
        "errores_nativos": 1,
        "hallazgos": [{"signature": "LVLI", "formid": h.FORMID_HALLAZGO}],
        "integridad": "PASS",
        "bridge_version": "1.1.0",
        "run_id": "20261115_201029_926e80",
        "log_path": "x",
    }
    return {**base, **cambios}


def _datos_auditoria(cuerpo: dict[str, Any], **extra: Any) -> dict[str, Any]:
    base = {"json": cuerpo, "log_existe": True, "log_dentro_del_lab": True, "admitidos_intactos": True}
    return {**base, **extra}


def test_negativa_3_un_plugin_invalido_declarado_aprobado_da_fail(tmp_path: Path) -> None:
    mentiroso = _auditoria(aprobado=True, clean=True, errores_nativos=0, hallazgos=[])

    async def auditor(_plugin: str) -> str:
        return json.dumps(mentiroso)

    ctx = _montar(tmp_path, auditor=auditor)

    registro = _correr(h.ejecutar_escenario(ctx, "T3_auditoria_invalido", "real", _auditoria_falsa(ctx)))

    assert registro["veredicto"] == "FAIL"
    texto = " ".join(registro["fallas"])
    assert "aprobado no es False" in texto and f"FormID {h.FORMID_HALLAZGO}" in texto


def _auditoria_falsa(ctx: Any) -> Any:
    async def funcion(parcial: dict[str, Any]) -> dict[str, Any]:
        cuerpo = json.loads(await ctx.registry.execute("auditar_plugin_nativo", {"plugin": h.PLUGIN_CON_HALLAZGO}))
        return {**parcial, "json": cuerpo, "log_existe": True, "log_dentro_del_lab": True, "admitidos_intactos": True}

    return funcion


def test_negativa_4_una_respuesta_mcp_malformada_aceptada_da_fail(tmp_path: Path) -> None:
    async def auditor_que_acepta(_plugin: str) -> str:
        return json.dumps(
            {"plugin": h.PLUGIN_VALIDO, "success": True, "aprobado": True, "run_id": "20261115_000000_aaaaaa"}
        )

    ctx = _montar(tmp_path, auditor=auditor_que_acepta)

    _correr(_correr_mcp_sin_detener(ctx))

    casos = [
        r
        for r in ctx.resultados
        if r["prueba"].startswith("T4_mcp_invalida/") and r["prueba"] != "T4_mcp_invalida/admitidos_intactos"
    ]
    assert len(casos) == len(h.CASOS_MCP)
    assert all(r["veredicto"] == "FAIL" for r in casos)
    assert any("fue aprobada" in " ".join(r["fallas"]) for r in casos)
    assert all("success distinto de False" in " ".join(r["fallas"]) for r in casos)
    assert all("exactamente una vez" in " ".join(r["fallas"]) for r in casos)  # el cliente real no se invocó


async def _correr_mcp_sin_detener(ctx: Any) -> None:
    await h.mcp_invalida(ctx)


def test_negativa_5_una_excepcion_posterior_a_una_admision_terminada_conserva_la_evidencia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _montar(tmp_path)
    original = ctx.estado_admitidos
    llamadas = {"n": 0}

    def inventario_que_falla_despues() -> dict[str, str]:
        llamadas["n"] += 1
        if llamadas["n"] >= 2:  # 1.ª = línea base; 2.ª = tras la admisión ya copiada
            raise OSError("disco desconectado")
        return original()

    ctx.estado_admitidos = inventario_que_falla_despues  # type: ignore[method-assign]

    with pytest.raises(OSError, match="disco desconectado"):
        _correr(_t2(ctx))

    registro = ctx.resultados[-1]
    assert registro["prueba"] == "T2_aprobacion" and registro["veredicto"] == "FAIL"
    assert registro["datos"]["estado_final"] == "exitosa"  # estado conocido de la admisión terminada
    assert registro["datos"]["admission_id"] == ID_VALIDO
    assert registro["datos"]["copiados"] == [h.PLUGIN_VALIDO]
    assert "disco desconectado" in registro["datos"]["excepcion"]
    assert len(ctx.servicio.llamadas) == 1  # ninguna admisión nueva
    en_disco = json.loads(ctx.destino.read_text(encoding="utf-8"))  # persistido en el momento, no al final
    assert en_disco["pruebas"][-1]["datos"]["admission_id"] == ID_VALIDO
    assert en_disco["veredicto_global"] == "FAIL"


# ---------------------------------------------------------------- oráculo, salida y persistencia


def test_un_escenario_sin_oraculo_falla_cerrado() -> None:
    assert h.evaluar("T9_inventado", {"success": True}) != []


def test_un_oraculo_que_lanza_nunca_cuenta_como_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def roto(_datos: dict[str, Any]) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setitem(h.ORACULOS, "T1_denegacion", roto)
    assert h.evaluar("T1_denegacion", {}) == ["el oráculo lanzó RuntimeError: boom"]


@pytest.mark.parametrize(
    ("cambios", "fragmento"),
    [
        ({"success": False}, "success no es True"),
        ({"aprobado": True}, "aprobado no es False"),
        ({"hallazgos": [{"formid": "00000001"}]}, "FormID"),
        ({"run_id": "no-es-un-run-id"}, "run_id inválido"),
        ({"integridad": "FAIL"}, "integridad no es PASS"),
        ({"errores_nativos": 0}, "errores_nativos < 1"),
    ],
)
def test_t3_con_hallazgo_rechaza_veredictos_incorrectos(cambios: dict[str, Any], fragmento: str) -> None:
    assert h.oraculo_t3_hallazgo(_datos_auditoria(_auditoria())) == []  # control positivo
    fallas = h.oraculo_t3_hallazgo(_datos_auditoria(_auditoria(**cambios)))
    assert any(fragmento in f for f in fallas)


def test_t3_limpio_exige_success_y_aprobado_y_sin_hallazgos() -> None:
    limpio = _auditoria(plugin=h.PLUGIN_VALIDO, aprobado=True, clean=True, errores_nativos=0, hallazgos=[])
    assert h.oraculo_t3_limpio(_datos_auditoria(limpio)) == []
    assert any("aprobado no es True" in f for f in h.oraculo_t3_limpio(_datos_auditoria({**limpio, "aprobado": False})))
    assert any(
        "hallazgos" in f for f in h.oraculo_t3_limpio(_datos_auditoria({**limpio, "hallazgos": [{"formid": "1"}]}))
    )
    assert any("log_path" in f for f in h.oraculo_t3_limpio(_datos_auditoria(limpio, log_existe=False)))


def test_no_admitido_falla_si_se_ejecuto_una_auditoria_nativa() -> None:
    bueno = {
        "json": {"success": False, "aprobado": False, "no_admitido": True},
        "corridas_nuevas": [],
        "admitidos_intactos": True,
    }
    assert h.oraculo_no_admitido(bueno) == []
    con_corrida = {**bueno, "corridas_nuevas": ["20261115_000000_aaaaaa"]}
    assert any("corrida nativa" in f for f in h.oraculo_no_admitido(con_corrida))
    con_run_id = {**bueno, "json": {**bueno["json"], "run_id": "20261115_000000_aaaaaa"}}
    assert any("auditoría" in f for f in h.oraculo_no_admitido(con_run_id))


def test_admision_ambigua_falla_si_hubo_reintento() -> None:
    sin_cambios = {
        "admitidos_cambio": False,
        "archivos_nuevos": [],
        "archivos_modificados": [],
        "archivos_borrados": [],
    }
    ambigua = {
        "estado_final": "fallida",
        "resultado": {"success": False, "estado_admision": "ambigua", "admission_id": None},
        "llamadas_al_bridge": 1,
        **sin_cambios,
    }
    assert h.oraculo_ambigua(ambigua) == []
    assert any("reintento" in f for f in h.oraculo_ambigua({**ambigua, "llamadas_al_bridge": 2}))


def test_hitl_invalido_no_produce_aprobacion_y_el_oraculo_lo_exige() -> None:
    sin_cambios = {
        "admitidos_cambio": False,
        "archivos_nuevos": [],
        "archivos_modificados": [],
        "archivos_borrados": [],
    }
    bueno = {
        "errores": {"int_1": "TypeError: x", "str_true": "TypeError: x", "None": "TypeError: x"},
        "seguia_pendiente_tras_no_bool": True,
        "estado_final": "rechazada",
        **sin_cambios,
    }
    assert h.oraculo_bool_estricto(bueno) == []
    resolvio = {
        **bueno,
        "errores": {**bueno["errores"], "str_true": "NO LANZO"},
        "seguia_pendiente_tras_no_bool": False,
    }
    fallas = h.oraculo_bool_estricto(resolvio)
    assert any("no lanzó TypeError" in f for f in fallas) and any("resolvió la solicitud" in f for f in fallas)


def test_la_salida_es_distinta_de_cero_ante_un_fail_o_un_escenario_ausente(tmp_path: Path) -> None:
    ctx = h.Contexto(tmp_path, tmp_path / "ev")
    h.registrar(ctx, "T4_mcp_invalida/admitidos_intactos", "sintetica", {"intactos": True})
    assert h.finalizar(ctx, ("T4_mcp_invalida/admitidos_intactos",)) == 0

    faltante = h.Contexto(tmp_path / "b", tmp_path / "ev2")
    h.registrar(faltante, "T4_mcp_invalida/admitidos_intactos", "sintetica", {"intactos": True})
    assert h.finalizar(faltante, ("T4_mcp_invalida/admitidos_intactos", "T1_denegacion")) == 1

    roto = h.Contexto(tmp_path / "c", tmp_path / "ev3")
    h.registrar(roto, "T4_mcp_invalida/admitidos_intactos", "sintetica", {"intactos": False})
    assert h.finalizar(roto, ("T4_mcp_invalida/admitidos_intactos",)) == 1
    guardado = json.loads(roto.destino.read_text(encoding="utf-8"))
    assert guardado["veredicto_global"] == "FAIL" and guardado["pruebas"][0]["veredicto"] == "FAIL"


def test_la_persistencia_es_atomica_y_no_sobrescribe_evidencia_previa(tmp_path: Path) -> None:
    ctx = h.Contexto(tmp_path, tmp_path / "ev")
    h.registrar(ctx, "T4_mcp_invalida/admitidos_intactos", "sintetica", {"intactos": True})
    assert json.loads(ctx.destino.read_text(encoding="utf-8"))["pruebas"][0]["veredicto"] == "PASS"
    assert not list(ctx.destino.parent.glob("*.tmp"))  # el temporal se reemplazó, no quedó huérfano

    assert h.main(["--qa-root", str(tmp_path), "--out", str(tmp_path / "ev")]) == 2  # no pisa la evidencia
    assert json.loads(ctx.destino.read_text(encoding="utf-8"))["pruebas"][0]["veredicto"] == "PASS"


# ---------------------------------------------------------------- compatibilidad con el contrato post-#955 (estática)


def _violaciones_de_ruta_productiva(codigo: str) -> list[str]:
    """Accesos a ``admitir`` / ``requerir_contexto`` en el CÓDIGO (AST), no en comentarios ni docstrings.

    Detecta ``x.admitir`` (llamado o no), ``getattr(x, "admitir")``, ``x.__dict__["admitir"]`` y cualquier
    literal de texto ``"admitir"``/``"requerir_contexto"`` fuera de docstrings, y el uso del identificador o
    del keyword ``requerir_contexto``.
    """
    arbol = ast.parse(codigo)
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    prohibidos = {"admitir", "requerir_contexto"}
    hallazgos: list[str] = []
    for nodo in ast.walk(arbol):
        linea = getattr(nodo, "lineno", 0)
        if isinstance(nodo, ast.Attribute) and nodo.attr in prohibidos:
            hallazgos.append(f"{linea}: atributo {nodo.attr}")
        elif isinstance(nodo, ast.Name) and nodo.id in prohibidos:
            hallazgos.append(f"{linea}: nombre {nodo.id}")
        elif isinstance(nodo, ast.keyword) and nodo.arg in prohibidos:
            hallazgos.append(f"{linea}: keyword {nodo.arg}")
        elif (
            isinstance(nodo, ast.Constant)
            and isinstance(nodo.value, str)
            and id(nodo) not in docstrings
            and nodo.value in prohibidos
        ):
            hallazgos.append(f"{linea}: literal {nodo.value!r}")
    return hallazgos


@pytest.mark.parametrize(
    "codigo",
    [
        "servicio.admitir(plugin)",
        "f = servicio.admitir",
        "getattr(servicio, 'admitir')(plugin)",
        "servicio.__dict__['admitir'](plugin)",
        "gestor(requerir_contexto=False)",
        "requerir_contexto = True",
    ],
)
def test_el_detector_de_ruta_productiva_ve_los_accesos_directos_y_dinamicos(codigo: str) -> None:
    assert _violaciones_de_ruta_productiva(codigo) != []


def test_el_detector_ignora_comentarios_y_docstrings() -> None:
    codigo = (
        '"""menciona admitir y requerir_contexto"""'
        + chr(10)
        + "# admitir requerir_contexto"
        + chr(10)
        + "x = 1"
        + chr(10)
    )
    assert _violaciones_de_ruta_productiva(codigo) == []


def test_el_harness_admite_solo_por_la_ruta_productiva_y_nunca_llama_a_service_admitir() -> None:
    codigo = RUTA_HARNESS.read_text(encoding="utf-8")
    assert _violaciones_de_ruta_productiva(codigo) == []  # el gestor productivo impone el contexto (#955)
    assert '"admitir_plugin_nativo"' in codigo  # y las admisiones salen por la tool, no por el servicio
