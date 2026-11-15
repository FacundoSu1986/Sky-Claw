"""Harness QA opt-in: aceptación real AsyncToolRegistry -> Gestor -> HITLGuard -> XEditBridgeService -> bridge -> SSEEdit.

NO es código de producción ni un test de CI: lanza SSEEdit real y copia plugins al laboratorio QA.
Solo corre si se le pasa la raíz del laboratorio QA descartable y la carpeta de evidencias.

    python scripts/qa/xedit_bridge_real_acceptance.py --qa-root G:/Modding/SSEDIT/QA-20261115 --out <carpeta de evidencias>

Cada escenario queda etiquetado ``real`` (todo el camino de producción, sin dobles) o ``sintetica`` (se
sustituye UN punto con un doble, descripto en ``doble``). El harness aborta antes de pedir aprobación si
el contexto de MO2 que ve el operador no es el de la instancia QA.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

PLUGIN_VALIDO = "T0_ValidCandidate.esp"
PLUGIN_CON_HALLAZGO = "T1_InvalidReference.esp"
PLUGIN_NO_ADMITIDO = "T5_LegitimateOverride.esp"
PLUGIN_INEXISTENTE = "NoExiste_QA.esp"
ESTADOS_TERMINALES = {"exitosa", "fallida", "rechazada", "cancelada"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventario(carpeta: Path) -> dict[str, str]:
    """{ruta relativa: sha256} de todo archivo bajo ``carpeta`` (vacío si no existe)."""
    if not carpeta.exists():
        return {}
    return {p.relative_to(carpeta).as_posix(): _sha(p) for p in sorted(carpeta.rglob("*")) if p.is_file()}


class Contexto:
    def __init__(self, qa_root: Path, out: Path) -> None:
        self.qa_root = qa_root.resolve()
        self.lab = self.qa_root / "inst" / "Laboratorio"
        self.bridge = self.lab / "BridgeTrabajo"
        self.admitidos = self.lab / "Admitidos"
        self.mo2 = self.qa_root / "MO2QA" / "Bin"
        self.out = out
        self.solicitudes: asyncio.Queue[Any] = asyncio.Queue()
        self.avisos: list[str] = []
        self.hitl: Any = None
        self.gestor: Any = None
        self.registry: Any = None
        self.resolver: Any = None

    def estado_admitidos(self) -> dict[str, str]:
        return inventario(self.admitidos)


def armar(ctx: Contexto, timeout_hitl: float = 120.0) -> None:
    from sky_claw.app.agent.tools import AsyncToolRegistry
    from sky_claw.app.agent.tools.xedit_bridge_tools import GestorDeAdmisionesNativas
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.app.security.path_validator import PathValidator

    # Sandbox = solo la carpeta QA: el MO2 real y Steam quedan fuera por construcción.
    resolver = PathResolutionService(PathValidator([ctx.qa_root]), profile_name="QA", mo2_install_dir=ctx.mo2)

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
        mo2_profile="QA",
        admisiones_nativas=gestor,
    )
    ctx.hitl, ctx.gestor, ctx.resolver = hitl, gestor, resolver


def control_de_aislamiento(ctx: Contexto) -> dict[str, Any]:
    """Falla cerrado si el resolver no apunta SOLO al laboratorio QA."""
    ini = ctx.resolver.get_mo2_instance_ini_dir()
    raiz = ctx.resolver.get_xedit_bridge_root()
    perfil = ctx.resolver.get_active_profile()
    resultado = {"mo2_ini_dir": str(ini), "bridge_root": str(raiz), "perfil": perfil}
    assert ini is not None and Path(ini).resolve() == ctx.mo2.resolve(), f"MO2 no es el QA: {ini}"
    assert raiz is not None and Path(raiz).resolve() == ctx.bridge.resolve(), f"bridge no es el QA: {raiz}"
    assert perfil == "QA", f"perfil inesperado: {perfil}"
    return resultado


async def esperar_estado(ctx: Contexto, limite: float = 600.0) -> Any:
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        trabajo = ctx.gestor.actual
        if trabajo is not None and trabajo.estado in ESTADOS_TERMINALES:
            await asyncio.gather(*[t for t in list(ctx.gestor._tareas)], return_exceptions=True)
            return trabajo
        await asyncio.sleep(0.2)
    raise TimeoutError("la admisión no terminó")


async def pedir_admision(ctx: Contexto, plugin: str) -> dict[str, Any]:
    while not ctx.solicitudes.empty():
        ctx.solicitudes.get_nowait()
    t0 = time.monotonic()
    respuesta = json.loads(await ctx.registry.execute("admitir_plugin_nativo", {"plugin": plugin}))
    return {"respuesta_tool": respuesta, "t_respuesta_s": round(time.monotonic() - t0, 3)}


async def escenario_admision(ctx: Contexto, plugin: str, aprobar: bool | None, **extra: Any) -> dict[str, Any]:
    """Admisión por la ruta real; ``aprobar`` None = no responder (timeout del guard)."""
    antes = ctx.estado_admitidos()
    inicio = await pedir_admision(ctx, plugin)
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    info = {"request_id": solicitud.request_id, "reason": solicitud.reason, "category": solicitud.category}
    if aprobar is not None:
        if "MO2QA" not in solicitud.reason or "«QA»" not in solicitud.reason:
            await ctx.hitl.respond(solicitud.request_id, False)
            raise AssertionError(f"contexto mostrado al operador no es el QA: {solicitud.reason}")
        info["respond_ack"] = await ctx.hitl.respond(solicitud.request_id, aprobar)
    trabajo = await esperar_estado(ctx)
    despues = ctx.estado_admitidos()
    return {
        **inicio,
        "hitl": info,
        "estado_final": trabajo.estado,
        "resultado": trabajo.resultado,
        "contexto_aprobado": trabajo.contexto.resumen(),
        "admitidos_cambio": antes != despues,
        "archivos_nuevos": sorted(set(despues) - set(antes)),
        "avisos": list(ctx.avisos),
        **extra,
    }


async def auditar(ctx: Contexto, plugin: str) -> dict[str, Any]:
    antes = ctx.estado_admitidos()
    t0 = time.monotonic()
    crudo = await ctx.registry.execute("auditar_plugin_nativo", {"plugin": plugin})
    return {
        "salida_cruda": crudo,
        "json": json.loads(crudo),
        "duracion_s": round(time.monotonic() - t0, 2),
        "admitidos_intactos": antes == ctx.estado_admitidos(),
    }


def etiquetar(nombre: str, tipo: str, datos: dict[str, Any], doble: str | None = None) -> dict[str, Any]:
    return {"prueba": nombre, "tipo": tipo, "doble": doble, "datos": datos}


async def correr(ctx: Contexto) -> list[dict[str, Any]]:
    res: list[dict[str, Any]] = []
    armar(ctx)
    aislamiento = control_de_aislamiento(ctx)
    res.append(etiquetar("aislamiento", "real", aislamiento))

    base = ctx.estado_admitidos()
    # T1 — denegación
    t1 = await escenario_admision(ctx, PLUGIN_VALIDO, aprobar=False)
    t1["sin_cambios_vs_base"] = ctx.estado_admitidos() == base
    res.append(etiquetar("T1_denegacion", "real", t1))

    # T2 — aprobación
    ctx.avisos.clear()
    t2 = await escenario_admision(ctx, PLUGIN_VALIDO, aprobar=True)
    res.append(etiquetar("T2_aprobacion", "real", t2))

    # T3 — auditoría nativa real (válido y con hallazgo)
    for nombre, plugin in (("T3_auditoria_valido", PLUGIN_VALIDO),):
        res.append(etiquetar(nombre, "real", await auditar(ctx, plugin)))
    # El plugin con hallazgo se admite también por la ruta real (aprobación) y se audita.
    t3b = await escenario_admision(ctx, PLUGIN_CON_HALLAZGO, aprobar=True)
    res.append(etiquetar("T3_admision_invalido", "real", t3b))
    res.append(etiquetar("T3_auditoria_invalido", "real", await auditar(ctx, PLUGIN_CON_HALLAZGO)))

    # T4 — fallos seguros
    res.append(etiquetar("T4_no_admitido", "real", await auditar(ctx, PLUGIN_NO_ADMITIDO)))
    res.append(etiquetar("T4_inexistente", "real", await escenario_admision(ctx, PLUGIN_INEXISTENTE, aprobar=True)))

    # timeout: mismo guard real con plazo corto, nadie responde
    armar(ctx, timeout_hitl=2)
    res.append(
        etiquetar(
            "T4_timeout_hitl",
            "sintetica",
            await escenario_admision(ctx, PLUGIN_NO_ADMITIDO, aprobar=None),
            doble="HITLGuard real con timeout=2 s en lugar de 120 s; nadie responde",
        )
    )

    # contexto cambiado durante la espera: el doble cambia el perfil inyectado entre pedido y copia
    armar(ctx)
    antes = ctx.estado_admitidos()
    inicio = await pedir_admision(ctx, PLUGIN_NO_ADMITIDO)
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    ctx.resolver._profile_name = "OtroPerfil"  # el operador aprueba lo que vio; el entorno cambió
    ack = await ctx.hitl.respond(solicitud.request_id, True)
    trabajo = await esperar_estado(ctx)
    res.append(
        etiquetar(
            "T4_contexto_cambiado",
            "sintetica",
            {
                **inicio,
                "respond_ack": ack,
                "estado_final": trabajo.estado,
                "resultado": trabajo.resultado,
                "admitidos_cambio": antes != ctx.estado_admitidos(),
            },
            doble="se muta resolver._profile_name tras el pedido, antes de la aprobación",
        )
    )
    ctx.resolver._profile_name = "QA"

    # bool estricto (#945): respond con no-bool debe lanzar TypeError y no resolver
    armar(ctx)
    inicio = await pedir_admision(ctx, PLUGIN_NO_ADMITIDO)
    solicitud = await asyncio.wait_for(ctx.solicitudes.get(), 30)
    errores = {}
    for etiqueta, valor in (("int_1", 1), ("str_true", "true"), ("None", None)):
        try:
            await ctx.hitl.respond(solicitud.request_id, valor)  # type: ignore[arg-type]
            errores[etiqueta] = "NO LANZO"
        except TypeError as exc:
            errores[etiqueta] = f"TypeError: {exc}"
    pendiente = ctx.gestor.actual is not None and ctx.gestor.actual.estado == "esperando_aprobacion"
    await ctx.hitl.respond(solicitud.request_id, False)
    await esperar_estado(ctx)
    res.append(
        etiquetar(
            "T4_bool_estricto",
            "real",
            {"errores": errores, "seguia_pendiente_tras_no_bool": pendiente, **inicio},
        )
    )

    # respuesta MCP inválida y admisión ambigua: dobles sobre el cliente del bridge
    from sky_claw.local.tools import xedit_bridge_service as svc

    armar(ctx)
    llamadas = {"n": 0}

    async def admision_ambigua(*_a: Any, **_k: Any) -> dict[str, Any]:
        llamadas["n"] += 1
        return {"ok": True, "copied": ["x.esp"]}  # sin admission_id

    antes = ctx.estado_admitidos()
    with mock.patch.object(svc, "admitir_plugin", admision_ambigua):
        amb = await escenario_admision(ctx, PLUGIN_NO_ADMITIDO, aprobar=True)
    amb["llamadas_al_bridge"] = llamadas["n"]
    amb["admitidos_cambio_real"] = antes != ctx.estado_admitidos()
    res.append(
        etiquetar("T4_admision_ambigua", "sintetica", amb, doble="svc.admitir_plugin devuelve ok=true sin admission_id")
    )

    res.extend(await mcp_invalida(ctx))
    return res


def _init_ok() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "xedit-bridge", "version": "1.1.0"}}}


def _llamada(payload: Any, es_error: bool) -> dict[str, Any]:
    texto = payload if isinstance(payload, str) else json.dumps(payload)
    return {"jsonrpc": "2.0", "id": 2, "result": {"isError": es_error, "content": [{"type": "text", "text": texto}]}}


async def mcp_invalida(ctx: Contexto) -> list[dict[str, Any]]:
    """Respuestas MCP inválidas sustituyendo SOLO la salida del proceso (el parseo del cliente es el real)."""
    from sky_claw.local.tools import xedit_bridge_client as cli

    armar(ctx)
    casos: dict[str, str] = {
        "linea_no_json": "esto no es json\n",
        "ok_con_iserror_incoherente": "\n".join(
            json.dumps(m) for m in (_init_ok(), _llamada({"ok": True, "clean": True}, True))
        )
        + "\n",
        "payload_sin_ok_booleano": "\n".join(
            json.dumps(m) for m in (_init_ok(), _llamada({"ok": "yes", "clean": True}, False))
        )
        + "\n",
        "servidor_ajeno": "\n".join(
            json.dumps(m)
            for m in (
                {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "otro", "version": "1.1.0"}}},
                _llamada({"ok": True, "clean": True}, False),
            )
        )
        + "\n",
        "id_duplicado": "\n".join(json.dumps(m) for m in (_init_ok(), _init_ok())) + "\n",
        "veredicto_limpio_falso_sin_log": "\n".join(
            json.dumps(m) for m in (_init_ok(), _llamada({"ok": True, "clean": True}, False))
        )
        + "\n",
    }
    salida: list[dict[str, Any]] = []
    antes = ctx.estado_admitidos()
    for nombre, texto in casos.items():
        llamadas = {"n": 0}

        async def falso(*_a: Any, _t: str = texto, _c: dict[str, int] = llamadas, **_k: Any) -> tuple[str, str, int]:
            _c["n"] += 1
            return _t, "", 0

        with mock.patch.object(cli, "_ejecutar", falso):
            try:
                crudo = await ctx.registry.execute("auditar_plugin_nativo", {"plugin": PLUGIN_VALIDO})
                datos: dict[str, Any] = {"json": json.loads(crudo)}
            except Exception as exc:  # noqa: BLE001 -- se registra tal cual
                datos = {"excepcion": f"{type(exc).__name__}: {exc}"}
        datos["llamadas_al_proceso"] = llamadas["n"]
        salida.append(
            etiquetar(f"T4_mcp_invalida/{nombre}", "sintetica", datos, doble="cli._ejecutar devuelve stdout sintético")
        )
    salida.append(
        etiquetar("T4_mcp_invalida/admitidos_intactos", "sintetica", {"intactos": antes == ctx.estado_admitidos()})
    )
    return salida


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--solo", choices=["mcp_invalida"], help="repite solo un grupo sintético (no re-admite nada)")
    args = parser.parse_args()
    ctx = Contexto(args.qa_root, args.out)
    for var in ("XEDIT_BRIDGE_ROOT",):
        os.environ[var] = str(ctx.bridge)
    args.out.mkdir(parents=True, exist_ok=True)
    import sky_claw

    meta = {
        "sky_claw_file": sky_claw.__file__,
        "python": sys.version,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    resultado: dict[str, Any] = {"meta": meta}
    codigo = 0
    try:
        if args.solo == "mcp_invalida":

            async def solo() -> list[dict[str, Any]]:
                armar(ctx)
                control_de_aislamiento(ctx)
                return await mcp_invalida(ctx)

            resultado["pruebas"] = asyncio.run(solo())
        else:
            resultado["pruebas"] = asyncio.run(correr(ctx))
    except Exception as exc:  # noqa: BLE001
        import traceback

        resultado["error"] = f"{type(exc).__name__}: {exc}"
        resultado["traceback"] = traceback.format_exc()
        codigo = 1
    destino = args.out / "resultado-harness.json"
    destino.write_text(json.dumps(resultado, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"escrito {destino}")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
