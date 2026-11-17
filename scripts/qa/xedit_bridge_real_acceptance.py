"""Harness QA opt-in: aceptación real AsyncToolRegistry -> Gestor -> HITLGuard -> XEditBridgeService -> bridge -> SSEEdit.

NO es código de producción ni un test de CI: lanza SSEEdit real y copia plugins al laboratorio QA.
Solo corre si se le pasa la raíz del laboratorio QA descartable y una carpeta de evidencias NUEVA.

    python scripts/qa/xedit_bridge_real_acceptance.py --qa-root G:/Modding/SSEDIT/QA-20261115 --out <carpeta nueva>

Cada escenario queda etiquetado ``real`` (todo el camino de producción, sin dobles) o ``sintetica`` (se
sustituye UN punto con un doble, descripto en ``doble``) y lo juzga un ORÁCULO explícito: ``veredicto``
``PASS`` o ``FAIL`` con la lista de ``fallas``. Un FAIL, una excepción o un escenario requerido ausente
dan código de salida distinto de cero. La presencia de una clave JSON nunca basta: se verifica su valor.

Evidencia: tras CADA escenario el resultado se reescribe de forma atómica (archivo temporal + ``os.replace``).
Si un escenario falla después de que una admisión ya terminó, se registra un resultado FAIL con el
``admission_id`` y el estado conocidos en ese momento (sin lanzar ninguna admisión nueva) y la corrida se
detiene. El harness se niega a sobrescribir un ``resultado-harness.json`` existente.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

PLUGIN_VALIDO = "T0_ValidCandidate.esp"
PLUGIN_CON_HALLAZGO = "T1_InvalidReference.esp"
PLUGIN_NO_ADMITIDO = "T5_LegitimateOverride.esp"
PLUGIN_INEXISTENTE = "NoExiste_QA.esp"
FORMID_HALLAZGO = "05000800"
ESTADOS_TERMINALES = {"exitosa", "fallida", "rechazada", "cancelada"}
#: SHA-256 (16 primeros hex, mayúsculas) de los fixtures canónicos: la copia admitida debe ser ESA.
SHA16_FIXTURES = {PLUGIN_VALIDO: "36DD1E63B48F6E3F", PLUGIN_CON_HALLAZGO: "AE8B2E15F3C803BD"}
ID_ADMISION = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
ID_CORRIDA = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}")
CASOS_MCP = (
    "linea_no_json",
    "ok_con_iserror_incoherente",
    "payload_sin_ok_booleano",
    "servidor_ajeno",
    "id_duplicado",
    "veredicto_limpio_falso_sin_log",
)
REQUERIDOS_COMPLETA = (
    "aislamiento",
    "T1_denegacion",
    "T2_aprobacion",
    "T3_auditoria_valido",
    "T3_admision_invalido",
    "T3_auditoria_invalido",
    "T4_no_admitido",
    "T4_inexistente",
    "T4_timeout_hitl",
    "T4_contexto_cambiado",
    "T4_bool_estricto",
    "T4_admision_ambigua",
    *(f"T4_mcp_invalida/{c}" for c in CASOS_MCP),
    "T4_mcp_invalida/admitidos_intactos",
)
REQUERIDOS_MCP = tuple(n for n in REQUERIDOS_COMPLETA if n.startswith("T4_mcp_invalida/"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventario(carpeta: Path) -> dict[str, str]:
    """{ruta relativa: sha256} de todo archivo bajo ``carpeta`` (vacío si no existe)."""
    if not carpeta.exists():
        return {}
    return {p.relative_to(carpeta).as_posix(): _sha(p) for p in sorted(carpeta.rglob("*")) if p.is_file()}


def diferencias(antes: dict[str, str], despues: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(nuevos, modificados, borrados) entre dos inventarios."""
    nuevos = sorted(set(despues) - set(antes))
    borrados = sorted(set(antes) - set(despues))
    modificados = sorted(k for k in set(antes) & set(despues) if antes[k] != despues[k])
    return nuevos, modificados, borrados


class EscenarioFallidoError(RuntimeError):
    """Un escenario real no cumplió su criterio: la corrida se detiene."""


class Contexto:
    def __init__(self, qa_root: Path, out: Path | None) -> None:
        self.qa_root = qa_root.resolve()
        self.lab = self.qa_root / "inst" / "Laboratorio"
        self.bridge = self.lab / "BridgeTrabajo"
        self.admitidos = self.lab / "Admitidos"
        self.corridas = self.lab / "Temporales" / "Bridge" / "runs"
        self.mo2 = self.qa_root / "MO2QA" / "Bin"
        self.marca_instancia = "MO2QA"
        self.perfil = "QA"
        self.destino: Path | None = None if out is None else out / "resultado-harness.json"
        self.solicitudes: asyncio.Queue[Any] = asyncio.Queue()
        self.avisos: list[str] = []
        self.hitl: Any = None
        self.gestor: Any = None
        self.registry: Any = None
        self.resolver: Any = None
        self.meta: dict[str, Any] = {}
        self.error: str | None = None
        self.finalizado = False  # hasta finalizar() el artefacto nunca declara PASS
        self.detener_si_falla = True  # un FAIL en la secuencia real detiene la corrida (no se sigue mutando el lab)
        self.resultados: list[dict[str, Any]] = []
        #: Punto de extensión: arma hitl/gestor/registry/resolver. Los tests lo reemplazan por dobles.
        self.armar: Callable[..., None] = lambda timeout_hitl=120.0: armar(self, timeout_hitl)

    def estado_admitidos(self) -> dict[str, str]:
        return inventario(self.admitidos)

    def corridas_existentes(self) -> list[str]:
        return sorted(p.name for p in self.corridas.iterdir()) if self.corridas.is_dir() else []

    def persistir(self) -> None:
        """Reescribe el resultado completo de forma atómica (temporal + ``os.replace``)."""
        if self.destino is None:
            return
        payload = {
            "meta": self.meta,
            "veredicto_global": veredicto_global(self, requeridos=None, finalizado=self.finalizado),
            "error": self.error,
            "pruebas": self.resultados,
        }
        self.destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.destino.with_name(self.destino.name + ".tmp")
        with temporal.open("w", encoding="utf-8") as flujo:
            flujo.write(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
            flujo.flush()
            os.fsync(flujo.fileno())
        os.replace(temporal, self.destino)


def armar(ctx: Contexto, timeout_hitl: float = 120.0) -> None:
    from sky_claw.app.agent.tools import AsyncToolRegistry
    from sky_claw.app.agent.tools.xedit_bridge_tools import GestorDeAdmisionesNativas
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.app.security.path_validator import PathValidator

    # Sandbox = solo la carpeta QA: el MO2 real y Steam quedan fuera por construcción.
    resolver = PathResolutionService(PathValidator([ctx.qa_root]), profile_name=ctx.perfil, mo2_install_dir=ctx.mo2)

    async def notify(req: Any) -> None:
        await ctx.solicitudes.put(req)

    async def aviso(mensaje: str) -> None:
        ctx.avisos.append(mensaje)

    hitl = HITLGuard(notify_fn=notify, timeout=int(timeout_hitl), notice_fn=aviso)
    gestor = GestorDeAdmisionesNativas(notificar=hitl.notify_operator)
    ctx.registry = AsyncToolRegistry(
        registry=None,
        mo2=None,
        sync_engine=None,
        hitl=hitl,
        path_resolver=resolver,
        mo2_profile=ctx.perfil,
        admisiones_nativas=gestor,
    )
    ctx.hitl, ctx.gestor, ctx.resolver = hitl, gestor, resolver


def control_de_aislamiento(ctx: Contexto) -> dict[str, Any]:
    """Falla cerrado si el resolver no apunta SOLO al laboratorio QA."""
    ini = ctx.resolver.get_mo2_instance_ini_dir()
    raiz = ctx.resolver.get_xedit_bridge_root()
    perfil = ctx.resolver.get_active_profile()
    return {
        "mo2_ini_dir": str(ini),
        "bridge_root": str(raiz),
        "perfil": perfil,
        "mo2_es_qa": ini is not None and Path(ini).resolve() == ctx.mo2.resolve(),
        "bridge_es_qa": raiz is not None and Path(raiz).resolve() == ctx.bridge.resolve(),
        "perfil_es_qa": perfil == ctx.perfil,
    }


# --------------------------------------------------------------------------------------------------
# Oráculo: cada función devuelve la lista de fallas (vacía = PASS). Nunca basta con que una clave exista.
# --------------------------------------------------------------------------------------------------


def _req(fallas: list[str], condicion: bool, mensaje: str) -> None:
    if not condicion:
        fallas.append(mensaje)


def _resultado(datos: dict[str, Any]) -> dict[str, Any]:
    valor = datos.get("resultado")
    return valor if isinstance(valor, dict) else {}


def _json(datos: dict[str, Any]) -> dict[str, Any]:
    valor = datos.get("json")
    return valor if isinstance(valor, dict) else {}


def _lista(origen: dict[str, Any], clave: str) -> list[Any]:
    valor = origen.get(clave)
    return valor if isinstance(valor, list) else []


def _dic(origen: dict[str, Any], clave: str) -> dict[str, Any]:
    valor = origen.get(clave)
    return valor if isinstance(valor, dict) else {}


def _sin_cambios(fallas: list[str], datos: dict[str, Any]) -> None:
    _req(fallas, datos.get("admitidos_cambio") is False, "Admitidos cambió y no debía")
    _req(fallas, datos.get("archivos_nuevos") == [], "aparecieron archivos nuevos en Admitidos")
    _req(fallas, datos.get("archivos_modificados") == [], "se modificaron archivos de Admitidos")
    _req(fallas, datos.get("archivos_borrados") == [], "se borraron archivos de Admitidos")


def _contexto_qa(fallas: list[str], datos: dict[str, Any], ctx_marca: str, perfil: str) -> None:
    razon = str(_hitl(datos).get("reason", ""))
    _req(fallas, ctx_marca in razon and f"«{perfil}»" in razon, "el pedido HITL no muestra el contexto QA")
    aprobado = str(datos.get("contexto_aprobado", ""))
    _req(fallas, ctx_marca in aprobado and f"«{perfil}»" in aprobado, "el contexto aprobado no es el QA")


def _hitl(datos: dict[str, Any]) -> dict[str, Any]:
    valor = datos.get("hitl")
    return valor if isinstance(valor, dict) else {}


def oraculo_t1(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(
        fallas,
        datos.get("estado_final") == "rechazada",
        f"estado_final={datos.get('estado_final')!r}, esperado 'rechazada'",
    )
    _req(fallas, resultado.get("success") is False, "success no es False")
    _req(fallas, resultado.get("status") == "denied" and resultado.get("decision") == "denied", "decisión no es denied")
    _req(fallas, _hitl(datos).get("respond_ack") is True, "respond(False) no fue aceptado")
    _req(fallas, not resultado.get("admission_id") and not resultado.get("copiados"), "hay admission_id/copiados")
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_t2(
    datos: dict[str, Any], plugin: str = PLUGIN_VALIDO, marca: str = "MO2QA", perfil: str = "QA"
) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(
        fallas,
        datos.get("estado_final") == "exitosa",
        f"estado_final={datos.get('estado_final')!r}, esperado 'exitosa'",
    )
    _req(fallas, resultado.get("success") is True, "success no es True")
    identificador = resultado.get("admission_id")
    _req(
        fallas,
        isinstance(identificador, str) and ID_ADMISION.fullmatch(identificador) is not None,
        f"admission_id inválido: {identificador!r}",
    )
    _req(fallas, _hitl(datos).get("respond_ack") is True, "respond(True) no fue aceptado")
    _contexto_qa(fallas, datos, marca, perfil)
    copiados = resultado.get("copiados")
    _req(fallas, isinstance(copiados, list) and plugin in copiados, "copiados no incluye el plugin")
    nuevos = _lista(datos, "archivos_nuevos")
    _req(fallas, f"Data/{plugin}" in nuevos, "no hay copia física nueva del plugin en Admitidos/Data")
    _req(
        fallas,
        "admissions.json" in nuevos or "admissions.json" in _lista(datos, "archivos_modificados"),
        "admissions.json no se creó/actualizó",
    )
    _req(fallas, datos.get("admitidos_cambio") is True, "Admitidos no cambió")
    _req(fallas, _lista(datos, "archivos_borrados") == [], "se borraron archivos previamente admitidos")
    previos_modificados = [m for m in _lista(datos, "archivos_modificados") if m != "admissions.json"]
    _req(fallas, previos_modificados == [], f"se modificaron archivos admitidos previamente: {previos_modificados}")
    copia = (datos.get("copias") or {}).get(plugin)
    esperado = SHA16_FIXTURES.get(plugin)
    _req(
        fallas,
        esperado is None or (isinstance(copia, str) and copia[:16].upper() == esperado),
        "la copia física no es el fixture esperado (SHA-256)",
    )
    return fallas


def _auditoria_comun(fallas: list[str], datos: dict[str, Any], plugin: str) -> dict[str, Any]:
    cuerpo = _json(datos)
    _req(fallas, cuerpo.get("plugin") == plugin, "el veredicto es de otro plugin")
    _req(fallas, cuerpo.get("success") is True, "success no es True")
    _req(fallas, cuerpo.get("check_complete") is True, "check_complete no es True")
    _req(fallas, cuerpo.get("integridad") == "PASS", "integridad no es PASS")
    _req(fallas, cuerpo.get("bridge_version") == "1.1.0", "bridge_version inesperada")
    corrida = cuerpo.get("run_id")
    _req(
        fallas, isinstance(corrida, str) and ID_CORRIDA.fullmatch(corrida) is not None, f"run_id inválido: {corrida!r}"
    )
    _req(
        fallas,
        isinstance(corrida, str) and corrida in _lista(datos, "corridas_nuevas"),
        "el run_id no pertenece a una corrida nueva (posible respuesta reutilizada)",
    )
    _req(
        fallas,
        datos.get("log_en_directorio_de_la_corrida") is True,
        "log_path no es <runs>/<run_id>/run.log del laboratorio QA (otra carpeta o corrida)",
    )
    _req(
        fallas,
        datos.get("log_existe") is True and datos.get("log_dentro_del_lab") is True,
        "log_path no existe o está fuera del lab QA",
    )
    _req(fallas, datos.get("admitidos_intactos") is True, "la auditoría modificó Admitidos")
    return cuerpo


def oraculo_t3_limpio(datos: dict[str, Any], plugin: str = PLUGIN_VALIDO) -> list[str]:
    fallas: list[str] = []
    cuerpo = _auditoria_comun(fallas, datos, plugin)
    _req(fallas, cuerpo.get("aprobado") is True, "aprobado no es True")
    _req(
        fallas,
        cuerpo.get("clean") is True and cuerpo.get("errores_nativos") == 0,
        "no es limpio (clean/errores_nativos)",
    )
    _req(fallas, cuerpo.get("hallazgos") == [], "un plugin limpio trae hallazgos")
    return fallas


def oraculo_t3_hallazgo(datos: dict[str, Any], plugin: str = PLUGIN_CON_HALLAZGO) -> list[str]:
    fallas: list[str] = []
    cuerpo = _auditoria_comun(fallas, datos, plugin)
    _req(fallas, cuerpo.get("aprobado") is False, "aprobado no es False (un plugin con error no puede aprobar)")
    _req(fallas, cuerpo.get("clean") is False, "clean no es False")
    errores = cuerpo.get("errores_nativos")
    _req(fallas, isinstance(errores, int) and not isinstance(errores, bool) and errores >= 1, "errores_nativos < 1")
    hallazgos = _lista(cuerpo, "hallazgos")
    _req(
        fallas,
        any(isinstance(h, dict) and h.get("formid") == FORMID_HALLAZGO for h in hallazgos),
        f"falta el hallazgo con FormID {FORMID_HALLAZGO}",
    )
    return fallas


def oraculo_t3_admision(datos: dict[str, Any]) -> list[str]:
    return oraculo_t2(datos, plugin=PLUGIN_CON_HALLAZGO)


def oraculo_no_admitido(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    cuerpo = _json(datos)
    _req(fallas, cuerpo.get("success") is False, "success no es False")
    _req(fallas, cuerpo.get("aprobado") is False, "aprobado no es False")
    _req(fallas, cuerpo.get("no_admitido") is True, "no_admitido no es True")
    _req(
        fallas, not cuerpo.get("run_id") and not cuerpo.get("log_path"), "hay run_id/log_path: se ejecutó una auditoría"
    )
    _req(fallas, datos.get("corridas_nuevas") == [], "apareció una corrida nativa nueva")
    _req(fallas, datos.get("admitidos_intactos") is True, "Admitidos cambió")
    return fallas


def oraculo_inexistente(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(
        fallas,
        datos.get("estado_final") == "fallida",
        f"estado_final={datos.get('estado_final')!r}, esperado 'fallida'",
    )
    _req(fallas, resultado.get("success") is False, "success no es False")
    _req(fallas, not resultado.get("admission_id"), "hay admission_id para un plugin inexistente")
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_timeout(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(
        fallas,
        datos.get("estado_final") == "rechazada",
        f"estado_final={datos.get('estado_final')!r}, esperado 'rechazada'",
    )
    _req(
        fallas,
        resultado.get("decision") == "timeout" and resultado.get("success") is False,
        "la decisión no es timeout/success=False",
    )
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_contexto_cambiado(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(fallas, datos.get("respond_ack") is True, "la aprobación no se entregó (el caso exige aprobar)")
    _req(
        fallas,
        datos.get("estado_final") == "fallida",
        f"estado_final={datos.get('estado_final')!r}, esperado 'fallida'",
    )
    _req(fallas, resultado.get("success") is False, "success no es False")
    _req(fallas, "contexto" in str(resultado.get("message", "")).lower(), "el mensaje no indica el cambio de contexto")
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_bool_estricto(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    errores = _dic(datos, "errores")
    _req(fallas, set(errores) == {"int_1", "str_true", "None"}, "faltan casos de no-bool")
    for caso, texto in errores.items():
        _req(fallas, str(texto).startswith("TypeError"), f"respond con {caso} no lanzó TypeError")
    _req(fallas, datos.get("seguia_pendiente_tras_no_bool") is True, "un no-bool resolvió la solicitud")
    _req(fallas, datos.get("estado_final") == "rechazada", "la solicitud no terminó en denegación explícita")
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_ambigua(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    resultado = _resultado(datos)
    _req(
        fallas,
        datos.get("estado_final") == "fallida",
        f"estado_final={datos.get('estado_final')!r}, esperado 'fallida'",
    )
    _req(fallas, resultado.get("success") is False, "success no es False")
    _req(
        fallas,
        resultado.get("estado_admision") == "ambigua" and resultado.get("admission_id") is None,
        "no quedó marcada como ambigua",
    )
    _req(
        fallas,
        datos.get("llamadas_al_bridge") == 1,
        f"llamadas_al_bridge={datos.get('llamadas_al_bridge')!r}: hubo reintento",
    )
    _sin_cambios(fallas, datos)
    return fallas


def oraculo_mcp(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    _req(fallas, "excepcion" not in datos, f"escapó una excepción: {datos.get('excepcion')}")
    cuerpo = _json(datos)
    _req(fallas, cuerpo.get("success") is False, "una respuesta MCP inválida produjo success distinto de False")
    _req(fallas, cuerpo.get("aprobado") is False, "una respuesta MCP inválida fue aprobada")
    _req(fallas, not cuerpo.get("run_id"), "una respuesta MCP inválida trae run_id")
    _req(
        fallas, datos.get("llamadas_al_proceso") == 1, "el cliente reintentó o no llamó al proceso exactamente una vez"
    )
    return fallas


def oraculo_mcp_intactos(datos: dict[str, Any]) -> list[str]:
    return [] if datos.get("intactos") is True else ["Admitidos cambió durante las pruebas MCP inválidas"]


def oraculo_aislamiento(datos: dict[str, Any]) -> list[str]:
    fallas: list[str] = []
    for clave in ("mo2_es_qa", "bridge_es_qa", "perfil_es_qa"):
        _req(fallas, datos.get(clave) is True, f"{clave} no es True: el contexto no es el laboratorio QA")
    return fallas


ORACULOS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "aislamiento": oraculo_aislamiento,
    "T1_denegacion": oraculo_t1,
    "T2_aprobacion": oraculo_t2,
    "T3_auditoria_valido": oraculo_t3_limpio,
    "T3_admision_invalido": oraculo_t3_admision,
    "T3_auditoria_invalido": oraculo_t3_hallazgo,
    "T4_no_admitido": oraculo_no_admitido,
    "T4_inexistente": oraculo_inexistente,
    "T4_timeout_hitl": oraculo_timeout,
    "T4_contexto_cambiado": oraculo_contexto_cambiado,
    "T4_bool_estricto": oraculo_bool_estricto,
    "T4_admision_ambigua": oraculo_ambigua,
    "T4_mcp_invalida/admitidos_intactos": oraculo_mcp_intactos,
}


def evaluar(nombre: str, datos: dict[str, Any]) -> list[str]:
    """Fallas del escenario ``nombre``. Un escenario sin oráculo falla cerrado."""
    oraculo = ORACULOS.get(nombre) or (oraculo_mcp if nombre.startswith("T4_mcp_invalida/") else None)
    if oraculo is None:
        return [f"sin oráculo para el escenario {nombre!r}"]
    try:
        return oraculo(datos)
    except Exception as exc:  # noqa: BLE001 -- un oráculo roto nunca cuenta como PASS
        return [f"el oráculo lanzó {type(exc).__name__}: {exc}"]


def veredicto_global(ctx: Contexto, requeridos: tuple[str, ...] | None, finalizado: bool = True) -> str:
    """``PASS`` solo si no hay error, ningún FAIL y (si se pasan) todos los escenarios requeridos están."""
    if ctx.error is not None:
        return "FAIL"
    if any(r.get("veredicto") != "PASS" for r in ctx.resultados):
        return "FAIL"
    if requeridos is not None and not set(requeridos) <= {r["prueba"] for r in ctx.resultados}:
        return "FAIL"
    if not finalizado:
        return "EN_CURSO"  # una corrida interrumpida (kill, Ctrl+C) nunca deja un PASS en el artefacto
    return "PASS" if ctx.resultados else "FAIL"


def registrar(
    ctx: Contexto,
    nombre: str,
    tipo: str,
    datos: dict[str, Any],
    doble: str | None = None,
    forzadas: list[str] | None = None,
) -> dict[str, Any]:
    fallas = [*evaluar(nombre, datos), *(forzadas or [])]
    registro = {
        "prueba": nombre,
        "tipo": tipo,
        "doble": doble,
        "veredicto": "FAIL" if fallas else "PASS",
        "fallas": fallas,
        "datos": datos,
    }
    ctx.resultados.append(registro)
    ctx.persistir()
    return registro


async def ejecutar_escenario(
    ctx: Contexto,
    nombre: str,
    tipo: str,
    funcion: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    doble: str | None = None,
) -> dict[str, Any]:
    """Corre un escenario y lo registra SIEMPRE. Si lanza, queda un FAIL con el estado parcial y se re-lanza."""
    parcial: dict[str, Any] = {}
    try:
        datos = await funcion(parcial)
    except Exception as exc:
        parcial["excepcion"] = f"{type(exc).__name__}: {exc}"
        trabajo = getattr(ctx.gestor, "actual", None)
        if trabajo is not None:
            # Estado conocido de la admisión (sin lanzar nada): puede haber terminado y copiado ya.
            resultado = trabajo.resultado if isinstance(trabajo.resultado, dict) else {}
            parcial.setdefault("estado_final", trabajo.estado)
            parcial["admission_id"] = resultado.get("admission_id")
            parcial["copiados"] = resultado.get("copiados")
            parcial["resultado"] = resultado or None
        try:
            parcial["inventario_admitidos_actual"] = ctx.estado_admitidos()
        except Exception as exc2:  # noqa: BLE001 -- best-effort: la evidencia parcial nunca oculta el error original
            parcial["inventario_admitidos_error"] = f"{type(exc2).__name__}: {exc2}"
        registrar(ctx, nombre, tipo, parcial, doble, forzadas=[f"el escenario lanzó {parcial['excepcion']}"])
        raise
    registro = registrar(ctx, nombre, tipo, datos, doble)
    if registro["veredicto"] != "PASS" and ctx.detener_si_falla:
        # No se sigue mutando el laboratorio tras detectar un resultado incorrecto: la evidencia ya está guardada.
        raise EscenarioFallidoError(f"{nombre}: {'; '.join(registro['fallas'])}")
    return registro


# --------------------------------------------------------------------------------------------------
# Escenarios
# --------------------------------------------------------------------------------------------------


async def pedir_admision(ctx: Contexto, plugin: str) -> dict[str, Any]:
    while not ctx.solicitudes.empty():
        ctx.solicitudes.get_nowait()
    t0 = time.monotonic()
    respuesta = json.loads(await ctx.registry.execute("admitir_plugin_nativo", {"plugin": plugin}))
    return {"respuesta_tool": respuesta, "t_respuesta_s": round(time.monotonic() - t0, 3)}


async def esperar_estado(ctx: Contexto, limite: float = 600.0) -> Any:
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        trabajo = ctx.gestor.actual
        if trabajo is not None and trabajo.estado in ESTADOS_TERMINALES:
            await asyncio.gather(*list(ctx.gestor._tareas), return_exceptions=True)
            return trabajo
        await asyncio.sleep(0.05)
    raise TimeoutError("la admisión no terminó")


def _cambios(ctx: Contexto, antes: dict[str, str]) -> dict[str, Any]:
    despues = ctx.estado_admitidos()
    nuevos, modificados, borrados = diferencias(antes, despues)
    return {
        "admitidos_cambio": bool(nuevos or modificados or borrados),
        "archivos_nuevos": nuevos,
        "archivos_modificados": modificados,
        "archivos_borrados": borrados,
    }


async def datos_de_admision(
    ctx: Contexto, parcial: dict[str, Any], plugin: str, aprobar: bool | None
) -> dict[str, Any]:
    """Admisión por la ruta real; ``aprobar`` None = nadie responde (vence el timeout del guard)."""
    antes = ctx.estado_admitidos()
    parcial.update(await pedir_admision(ctx, plugin))
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    parcial["hitl"] = {"request_id": solicitud.request_id, "reason": solicitud.reason, "category": solicitud.category}
    if aprobar is not None:
        if ctx.marca_instancia not in solicitud.reason or f"«{ctx.perfil}»" not in solicitud.reason:
            await ctx.hitl.respond(solicitud.request_id, False)
            raise AssertionError(f"contexto mostrado al operador no es el QA: {solicitud.reason}")
        parcial["hitl"]["respond_ack"] = await ctx.hitl.respond(solicitud.request_id, aprobar)
    trabajo = await esperar_estado(ctx)
    # Estado conocido ANTES de cualquier otra lectura: si algo falla después, esto ya está en ``parcial``.
    parcial["estado_final"] = trabajo.estado
    parcial["resultado"] = trabajo.resultado
    parcial["contexto_aprobado"] = trabajo.contexto.resumen()
    datos = {**parcial, **_cambios(ctx, antes), "avisos": list(ctx.avisos)}
    resultado = trabajo.resultado if isinstance(trabajo.resultado, dict) else {}
    copiados = _lista(resultado, "copiados")
    datos["copias"] = {
        n: _sha(ctx.admitidos / "Data" / n)
        for n in copiados
        if isinstance(n, str) and (ctx.admitidos / "Data" / n).is_file()
    }
    return datos


def _log_de_la_corrida(ctx: Contexto, cuerpo: dict[str, Any]) -> bool:
    """El log informado es EXACTAMENTE ``<corridas>/<run_id>/run.log`` (rutas resueltas, no solo el nombre)."""
    corrida, ruta = cuerpo.get("run_id"), cuerpo.get("log_path")
    if not isinstance(corrida, str) or not isinstance(ruta, str) or not ruta:
        return False
    try:
        return Path(ruta).resolve() == (ctx.corridas / corrida / "run.log").resolve()
    except OSError:
        return False


async def datos_de_auditoria(ctx: Contexto, parcial: dict[str, Any], plugin: str) -> dict[str, Any]:
    antes = ctx.estado_admitidos()
    corridas = ctx.corridas_existentes()
    t0 = time.monotonic()
    crudo = await ctx.registry.execute("auditar_plugin_nativo", {"plugin": plugin})
    parcial["salida_cruda"] = crudo
    cuerpo = json.loads(crudo)
    parcial["json"] = cuerpo
    registro = Path(str(cuerpo.get("log_path") or ""))
    dentro = False
    try:
        dentro = bool(cuerpo.get("log_path")) and registro.resolve().is_relative_to(ctx.lab.resolve())
    except OSError:
        dentro = False
    existe = dentro and registro.is_file()
    return {
        **parcial,
        "duracion_s": round(time.monotonic() - t0, 2),
        "admitidos_intactos": antes == ctx.estado_admitidos(),
        "corridas_nuevas": sorted(set(ctx.corridas_existentes()) - set(corridas)),
        "log_dentro_del_lab": dentro,
        "log_en_directorio_de_la_corrida": _log_de_la_corrida(ctx, cuerpo),
        "log_existe": existe,
        "log_sha256": _sha(registro) if existe else None,
    }


def _escenario_admision(
    ctx: Contexto, plugin: str, aprobar: bool | None
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def funcion(parcial: dict[str, Any]) -> dict[str, Any]:
        return await datos_de_admision(ctx, parcial, plugin, aprobar)

    return funcion


def _escenario_auditoria(ctx: Contexto, plugin: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def funcion(parcial: dict[str, Any]) -> dict[str, Any]:
        return await datos_de_auditoria(ctx, parcial, plugin)

    return funcion


async def t4_contexto_cambiado(ctx: Contexto, parcial: dict[str, Any]) -> dict[str, Any]:
    antes = ctx.estado_admitidos()
    parcial.update(await pedir_admision(ctx, PLUGIN_NO_ADMITIDO))
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    ctx.resolver._profile_name = "OtroPerfil"  # el operador aprueba lo que vio; el entorno cambió
    try:
        parcial["respond_ack"] = await ctx.hitl.respond(solicitud.request_id, True)
        trabajo = await esperar_estado(ctx)
    finally:
        ctx.resolver._profile_name = ctx.perfil
    parcial["estado_final"] = trabajo.estado
    parcial["resultado"] = trabajo.resultado
    return {**parcial, **_cambios(ctx, antes)}


async def t4_bool_estricto(ctx: Contexto, parcial: dict[str, Any]) -> dict[str, Any]:
    antes = ctx.estado_admitidos()
    parcial.update(await pedir_admision(ctx, PLUGIN_NO_ADMITIDO))
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    errores: dict[str, str] = {}
    for etiqueta, valor in (("int_1", 1), ("str_true", "true"), ("None", None)):
        try:
            await ctx.hitl.respond(solicitud.request_id, valor)
            errores[etiqueta] = "NO LANZO"
        except TypeError as exc:
            errores[etiqueta] = f"TypeError: {exc}"
    parcial["errores"] = errores
    trabajo_vivo = ctx.gestor.actual
    parcial["seguia_pendiente_tras_no_bool"] = (
        trabajo_vivo is not None and trabajo_vivo.estado == "esperando_aprobacion"
    )
    await ctx.hitl.respond(solicitud.request_id, False)
    trabajo = await esperar_estado(ctx)
    parcial["estado_final"] = trabajo.estado
    return {**parcial, **_cambios(ctx, antes)}


async def t4_admision_ambigua(ctx: Contexto, parcial: dict[str, Any]) -> dict[str, Any]:
    from sky_claw.local.tools import xedit_bridge_service as svc

    llamadas = {"n": 0}

    async def admision_ambigua(*_a: Any, **_k: Any) -> dict[str, Any]:
        llamadas["n"] += 1
        return {"ok": True, "copied": ["x.esp"]}  # sin admission_id

    with mock.patch.object(svc, "admitir_plugin", admision_ambigua):
        datos = await datos_de_admision(ctx, parcial, PLUGIN_NO_ADMITIDO, True)
    datos["llamadas_al_bridge"] = llamadas["n"]
    return datos


def _init_ok() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "xedit-bridge", "version": "1.1.0"}}}


def _llamada(payload: Any, es_error: bool) -> dict[str, Any]:
    texto = payload if isinstance(payload, str) else json.dumps(payload)
    return {"jsonrpc": "2.0", "id": 2, "result": {"isError": es_error, "content": [{"type": "text", "text": texto}]}}


def _lineas(*mensajes: Any) -> str:
    return "\n".join(json.dumps(m) for m in mensajes) + "\n"


def salidas_mcp_invalidas() -> dict[str, str]:
    """stdout sintético del proceso del bridge para cada caso inválido (el parseo del cliente es el real)."""
    ajeno = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "otro", "version": "1.1.0"}}}
    return {
        "linea_no_json": "esto no es json\n",
        "ok_con_iserror_incoherente": _lineas(_init_ok(), _llamada({"ok": True, "clean": True}, True)),
        "payload_sin_ok_booleano": _lineas(_init_ok(), _llamada({"ok": "yes", "clean": True}, False)),
        "servidor_ajeno": _lineas(ajeno, _llamada({"ok": True, "clean": True}, False)),
        "id_duplicado": _lineas(_init_ok(), _init_ok()),
        "veredicto_limpio_falso_sin_log": _lineas(_init_ok(), _llamada({"ok": True, "clean": True}, False)),
    }


async def mcp_invalida(ctx: Contexto) -> None:
    """Respuestas MCP inválidas sustituyendo SOLO la salida del proceso; cada caso se registra por separado."""
    from sky_claw.local.tools import xedit_bridge_client as cli

    antes = ctx.estado_admitidos()
    for nombre, texto in salidas_mcp_invalidas().items():
        llamadas = {"n": 0}

        async def falso(*_a: Any, _t: str = texto, _c: dict[str, int] = llamadas, **_k: Any) -> tuple[str, str, int]:
            _c["n"] += 1
            return _t, "", 0

        async def caso(parcial: dict[str, Any], _c: dict[str, int] = llamadas) -> dict[str, Any]:
            try:
                crudo = await ctx.registry.execute("auditar_plugin_nativo", {"plugin": PLUGIN_VALIDO})
                parcial["json"] = json.loads(crudo)
            except Exception as exc:  # noqa: BLE001 -- se registra tal cual; el oráculo lo juzga
                parcial["excepcion"] = f"{type(exc).__name__}: {exc}"
            parcial["llamadas_al_proceso"] = _c["n"]
            return parcial

        with mock.patch.object(cli, "_ejecutar", falso):
            await ejecutar_escenario(
                ctx, f"T4_mcp_invalida/{nombre}", "sintetica", caso, "cli._ejecutar devuelve stdout sintético"
            )

    async def intactos(_parcial: dict[str, Any]) -> dict[str, Any]:
        return {"intactos": antes == ctx.estado_admitidos()}

    await ejecutar_escenario(ctx, "T4_mcp_invalida/admitidos_intactos", "sintetica", intactos)


async def correr(ctx: Contexto) -> None:
    ctx.armar()

    async def aislamiento(_p: dict[str, Any]) -> dict[str, Any]:
        return control_de_aislamiento(ctx)

    await ejecutar_escenario(ctx, "aislamiento", "real", aislamiento)
    if ctx.resultados[-1]["veredicto"] != "PASS":
        raise RuntimeError("aislamiento: el contexto resuelto no es el laboratorio QA; no se pide ninguna aprobación")

    esc, aud = _escenario_admision, _escenario_auditoria
    await ejecutar_escenario(ctx, "T1_denegacion", "real", esc(ctx, PLUGIN_VALIDO, False))
    ctx.avisos.clear()
    await ejecutar_escenario(ctx, "T2_aprobacion", "real", esc(ctx, PLUGIN_VALIDO, True))
    await ejecutar_escenario(ctx, "T3_auditoria_valido", "real", aud(ctx, PLUGIN_VALIDO))
    await ejecutar_escenario(ctx, "T3_admision_invalido", "real", esc(ctx, PLUGIN_CON_HALLAZGO, True))
    await ejecutar_escenario(ctx, "T3_auditoria_invalido", "real", aud(ctx, PLUGIN_CON_HALLAZGO))
    await ejecutar_escenario(ctx, "T4_no_admitido", "real", aud(ctx, PLUGIN_NO_ADMITIDO))
    await ejecutar_escenario(ctx, "T4_inexistente", "real", esc(ctx, PLUGIN_INEXISTENTE, True))

    ctx.armar(timeout_hitl=2)
    await ejecutar_escenario(
        ctx,
        "T4_timeout_hitl",
        "sintetica",
        esc(ctx, PLUGIN_NO_ADMITIDO, None),
        "HITLGuard real con timeout=2 s en lugar de 120 s; nadie responde",
    )

    ctx.armar()

    async def contexto(parcial: dict[str, Any]) -> dict[str, Any]:
        return await t4_contexto_cambiado(ctx, parcial)

    await ejecutar_escenario(
        ctx,
        "T4_contexto_cambiado",
        "sintetica",
        contexto,
        "se muta resolver._profile_name tras el pedido, antes de aprobar",
    )

    ctx.armar()

    async def estricto(parcial: dict[str, Any]) -> dict[str, Any]:
        return await t4_bool_estricto(ctx, parcial)

    await ejecutar_escenario(ctx, "T4_bool_estricto", "real", estricto)

    ctx.armar()

    async def ambigua(parcial: dict[str, Any]) -> dict[str, Any]:
        return await t4_admision_ambigua(ctx, parcial)

    await ejecutar_escenario(
        ctx, "T4_admision_ambigua", "sintetica", ambigua, "svc.admitir_plugin devuelve ok=true sin admission_id"
    )

    ctx.armar()
    await mcp_invalida(ctx)


async def correr_solo_mcp(ctx: Contexto) -> None:
    ctx.armar()

    async def aislamiento(_p: dict[str, Any]) -> dict[str, Any]:
        return control_de_aislamiento(ctx)

    await ejecutar_escenario(ctx, "aislamiento", "real", aislamiento)
    if ctx.resultados[-1]["veredicto"] != "PASS":
        raise RuntimeError("aislamiento: el contexto resuelto no es el laboratorio QA")
    await mcp_invalida(ctx)


def finalizar(ctx: Contexto, requeridos: tuple[str, ...]) -> int:
    """Persiste el resultado final y devuelve el código de salida (0 solo con PASS global)."""
    ctx.finalizado = True
    veredicto = veredicto_global(ctx, requeridos)
    if ctx.error is None and veredicto == "FAIL":
        faltan = sorted(set(requeridos) - {r["prueba"] for r in ctx.resultados})
        ctx.error = f"veredicto global FAIL (fallas o escenarios requeridos ausentes: {faltan})"
    ctx.persistir()
    return 0 if veredicto == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="carpeta de evidencias NUEVA (no se sobrescribe nada)")
    parser.add_argument("--solo", choices=["mcp_invalida"], help="repite solo un grupo sintético (no re-admite nada)")
    args = parser.parse_args(argv)
    ctx = Contexto(args.qa_root, args.out)
    if ctx.destino is not None and ctx.destino.exists():
        print(f"ya existe {ctx.destino}: la evidencia previa no se sobrescribe", file=sys.stderr)
        return 2
    os.environ["XEDIT_BRIDGE_ROOT"] = str(ctx.bridge)
    import sky_claw

    ctx.meta = {
        "sky_claw_file": sky_claw.__file__,
        "python": sys.version,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "modo": args.solo or "completa",
    }
    requeridos = REQUERIDOS_MCP if args.solo == "mcp_invalida" else REQUERIDOS_COMPLETA
    try:
        asyncio.run(correr_solo_mcp(ctx) if args.solo == "mcp_invalida" else correr(ctx))
    except Exception as exc:  # noqa: BLE001 -- el resultado parcial ya está persistido por escenario
        import traceback

        ctx.error = f"{type(exc).__name__}: {exc}"
        ctx.meta["traceback"] = traceback.format_exc()
    codigo = finalizar(ctx, requeridos)
    print(f"escrito {ctx.destino} veredicto={veredicto_global(ctx, requeridos)}")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
