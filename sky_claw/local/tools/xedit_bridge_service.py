"""Servicio de auditoría nativa de plugins con el xEdit Bridge (ADR 0018).

Lo consumen las dos superficies: el agente LLM (``auditar_plugin_nativo`` /
``admitir_plugin_nativo`` en ``AsyncToolRegistry``) y la GUI (estrategias homónimas en
``tool_dispatcher``). La aprobación humana de la admisión vive en cada superficie, no acá.

Contrato de resultado (``success``/``message``, ver ``tool_result.py``):

* ``success`` = la auditoría es CONFIABLE: chequeo nativo completo, integridad prospectiva PASS y
  respuesta sin recortar. Un plugin con errores auditado por completo es ``success=True``.
* ``aprobado`` = la regla de aprobación del bridge: confiable **y** ``clean``. Es el único campo
  que permite tratar un plugin como sin errores; ``success`` por sí solo nunca alcanza.
* Todo texto que sale de un plugin (nombres de registro, rutas, descripciones) se sanea con
  ``sanitize_for_prompt`` y se acota antes de llegar a un prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.tools.xedit_bridge_client import (
    MARGEN_TIMEOUT_S,
    BridgeError,
    InstalacionBridge,
    RespuestaAuditoria,
    admitir_plugin,
    auditar_plugin,
    validar_plugin,
)

logger = logging.getLogger("SkyClaw.XEditBridge")

TIMEOUT_POR_DEFECTO = 60
LIMITE_HALLAZGOS = 50
_LARGO_TEXTO = 300
_LARGO_NOMBRE = 255
_CAMPOS_HALLAZGO = ("signature", "formid", "record_name", "path", "description")


class _Rutas(Protocol):
    def get_xedit_bridge_root(self) -> Path | None: ...
    def get_mo2_instance_ini_dir(self) -> Path | None: ...
    def get_active_profile(self) -> str: ...


def _texto(valor: Any, largo: int = _LARGO_TEXTO) -> str:
    return sanitize_for_prompt(str(valor), max_length=largo)


def _lista(valor: Any) -> list[Any]:
    """Un campo de lista del bridge; cualquier otro tipo cuenta como vacío (nunca se itera un str)."""
    return valor if isinstance(valor, list) else []


class XEditBridgeService:
    """Auditoría nativa (lectura) y admisión (copia al laboratorio del bridge) de un plugin."""

    def __init__(self, path_resolver: _Rutas) -> None:
        self._rutas = path_resolver

    def perfil_activo(self) -> str:
        """Perfil de MO2 del que se resolvería una admisión (para el pedido de aprobación)."""
        return self._rutas.get_active_profile()

    def _instalacion(self) -> InstalacionBridge:
        raiz = self._rutas.get_xedit_bridge_root()
        if raiz is None:
            raise BridgeError(
                "xEdit Bridge no configurado: definí XEDIT_BRIDGE_ROOT con la carpeta BridgeTrabajo de la "
                "instalación (p. ej. ...\\tools\\xedit-bridge-1.1.0\\Laboratorio\\BridgeTrabajo)"
            )
        return InstalacionBridge.desde_raiz(raiz)

    async def auditar(
        self, plugin: str, timeout: int | None = None, *, margen_s: float = MARGEN_TIMEOUT_S
    ) -> dict[str, Any]:
        """Chequeo nativo de SSEEdit sobre la copia admitida de ``plugin``.

        ``margen_s`` se suma al timeout del motor para el plazo total del proceso; una superficie con su
        propio plazo (el round del router del agente) pasa uno menor para vencer antes que él.
        """
        base: dict[str, Any] = {"plugin": _texto(plugin, _LARGO_NOMBRE), "aprobado": False}
        try:
            validar_plugin(plugin)
            respuesta = await auditar_plugin(
                self._instalacion(), plugin, timeout=timeout or TIMEOUT_POR_DEFECTO, margen_s=margen_s
            )
        except (BridgeError, ValueError) as exc:
            logger.warning("Auditoría nativa sin veredicto para %r: %s", plugin, exc)
            return {**base, "success": False, "message": _texto(exc)}
        return self._veredicto(plugin, respuesta)

    def _veredicto(self, plugin: str, respuesta: RespuestaAuditoria) -> dict[str, Any]:
        payload = respuesta.payload
        datos = payload.get("untrusted_data")
        datos = datos if isinstance(datos, dict) else {}
        if not datos:
            # El bridge real informa las fallas en ``errors`` (lista); ``error`` se acepta por robustez.
            errores = [str(e) for e in _lista(payload.get("errors"))] or [str(payload.get("error") or "")]
            error = "; ".join(e for e in errores if e) or "el bridge no devolvió datos de auditoría"
            no_admitido = "not admitted" in error
            mensaje = (
                f"{plugin} no está admitido en el xEdit Bridge: usá admitir_plugin_nativo (requiere aprobación)"
                if no_admitido
                else f"el xEdit Bridge no auditó {plugin}: {error}"
            )
            return {
                "plugin": _texto(plugin, _LARGO_NOMBRE),
                "success": False,
                "message": _texto(mensaje),
                "aprobado": False,
                "no_admitido": no_admitido,
            }
        completo = datos.get("check_complete") is True
        integridad = datos.get("prospective_integrity")
        recortado = datos.get("truncated") is True
        confiable = completo and integridad == "PASS" and not recortado
        limpio = datos.get("clean") is True
        hallazgos_crudos = _lista(datos.get("findings"))
        hallazgos = [
            {campo: _texto(h[campo]) for campo in _CAMPOS_HALLAZGO if isinstance(h, dict) and h.get(campo) is not None}
            for h in hallazgos_crudos[:LIMITE_HALLAZGOS]
        ]
        fallas = [_texto(f) for f in _lista(datos.get("execution_failures"))[:10]]
        # De la causa más específica a la más general: el bridge marca un recorte también como incompleto.
        if confiable:
            mensaje = ""
        elif recortado:
            mensaje = "respuesta del bridge recortada: revisar el log crudo en log_path"
        elif integridad != "PASS":
            mensaje = f"auditoría nativa sin integridad prospectiva PASS (integridad={_texto(integridad, 40)})"
        else:
            mensaje = "auditoría nativa incompleta: " + (
                "; ".join(fallas) or "el chequeo no terminó de forma verificable"
            )
        return {
            "plugin": _texto(plugin, _LARGO_NOMBRE),
            "success": confiable,
            "message": mensaje,
            "aprobado": payload.get("ok") is True and confiable and limpio,
            "clean": limpio,
            "check_complete": completo,
            "errores_nativos": datos.get("native_counter_records"),
            "registros_procesados": datos.get("native_processed_records"),
            "avisos": datos.get("check_warning_count", 0),
            "hallazgos": hallazgos,
            "hallazgos_omitidos": max(0, len(hallazgos_crudos) - LIMITE_HALLAZGOS),
            "fallas": fallas,
            "integridad": integridad,
            "recortado": recortado,
            "bridge_version": respuesta.version,
            "run_id": datos.get("run_id"),
            "log_path": datos.get("log_path"),
            "no_admitido": False,
        }

    async def admitir(self, plugin: str) -> dict[str, Any]:
        """Copia ``plugin`` y sus masters del MO2 de Sky-Claw al laboratorio del bridge (MO2 no se toca).

        Solo debe llamarse después de una aprobación humana: la impone cada superficie.
        """
        base: dict[str, Any] = {"plugin": _texto(plugin, _LARGO_NOMBRE)}
        # El bridge re-deriva la instancia (base, mods, perfiles, juego) de su ModOrganizer.ini: se le
        # pasa la carpeta del MISMO INI que usa Sky-Claw, no la instalación (que en MO2 2.4+ no tiene INI)
        # ni la raíz de datos (que puede guardar una copia vieja).
        mo2 = self._rutas.get_mo2_instance_ini_dir()
        if mo2 is None:
            return {
                **base,
                "success": False,
                "message": "MO2 no configurado: no se encontró el ModOrganizer.ini de la instancia activa",
            }
        try:
            datos = await admitir_plugin(self._instalacion(), mo2, self._rutas.get_active_profile(), plugin)
        except (BridgeError, ValueError) as exc:
            logger.warning("Admisión de %r no realizada: %s", plugin, exc)
            return {**base, "success": False, "message": _texto(exc)}
        if not datos.get("ok"):
            return {**base, "success": False, "message": _texto(datos.get("error") or "el bridge rechazó la admisión")}
        logger.info("Plugin %s admitido en el xEdit Bridge (%s)", plugin, datos.get("admission_id"))
        return {
            **base,
            "success": True,
            "message": "",
            "admission_id": datos.get("admission_id"),
            "clausura": [_texto(n, _LARGO_NOMBRE) for n in _lista(datos.get("closure"))],
            "copiados": [_texto(n, _LARGO_NOMBRE) for n in _lista(datos.get("copied"))],
            "reutilizados": [_texto(n, _LARGO_NOMBRE) for n in _lista(datos.get("reused"))],
            "reemplazados": [_texto(n, _LARGO_NOMBRE) for n in _lista(datos.get("replaced"))],
            "siguiente": "auditar_plugin_nativo",
        }


__all__ = ["LIMITE_HALLAZGOS", "XEditBridgeService"]
