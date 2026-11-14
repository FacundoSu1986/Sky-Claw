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
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeGuard

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
_LARGO_RUTA_LOG = 500
_CAMPOS_HALLAZGO = ("signature", "formid", "record_name", "path", "description")
_PATRON_IDENTIFICADOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SALTOS_RE = re.compile(r"[\r\n\t]+")


class _Rutas(Protocol):
    def get_xedit_bridge_root(self) -> Path | None: ...
    def get_mo2_instance_ini_dir(self) -> Path | None: ...
    def get_active_profile(self) -> str: ...


def _texto(valor: Any, largo: int = _LARGO_TEXTO) -> str:
    return sanitize_for_prompt(str(valor), max_length=largo)


def _texto_una_linea(valor: Any, largo: int = _LARGO_TEXTO) -> str:
    return _SALTOS_RE.sub(" ", _texto(valor, largo)).strip()


def _lista(valor: Any) -> list[Any]:
    """Un campo de lista del bridge; cualquier otro tipo cuenta como vacío (nunca se itera un str)."""
    return valor if isinstance(valor, list) else []


def _lista_textos(valor: Any, largo: int = _LARGO_NOMBRE) -> list[str]:
    """Extrae solo elementos ``str`` de una lista del bridge y los sanea en una línea."""
    return [_texto_una_linea(n, largo) for n in _lista(valor) if isinstance(n, str)]


def _es_entero_no_negativo(valor: Any) -> TypeGuard[int]:
    """Entero estricto >= 0 (rechaza ``bool``, ``float``, strings y negativos)."""
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0


def _validar_identificador(valor: Any) -> str | None:
    """Valida ``run_id`` / ``admission_id`` externos sin saltos, espacios ni traversal."""
    if not isinstance(valor, str) or ".." in valor:
        return None
    return valor if _PATRON_IDENTIFICADOR.fullmatch(valor) else None


def _tiene_controles_o_formato(valor: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F or unicodedata.category(c) == "Cf" for c in valor)


def _validar_ruta_log(valor: Any) -> str | None:
    """Valida y sanea ``log_path`` para impedir saltos de rol, controles e intentos de traversal."""
    if not isinstance(valor, str):
        return None
    if not valor.strip() or _tiene_controles_o_formato(valor):
        return None
    if ".." in valor.replace("\\", "/").split("/"):
        return None
    saneado = _texto_una_linea(valor, _LARGO_RUTA_LOG)
    return saneado or None


def _validar_hallazgos(crudo: Any) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(crudo, list):
        return [], "findings debe ser una lista"
    normalizados: list[dict[str, str]] = []
    for item in crudo:
        if not isinstance(item, dict):
            return [], "findings contiene elementos que no son objetos"
        limpio: dict[str, str] = {}
        for campo in _CAMPOS_HALLAZGO:
            val = item.get(campo)
            if val is None:
                continue
            if not isinstance(val, str):
                return [], f"findings.{campo} debe ser texto"
            limpio[campo] = _texto_una_linea(val)
        if len(normalizados) < LIMITE_HALLAZGOS:
            normalizados.append(limpio)
    return normalizados, None


def _validar_fallas(crudo: Any) -> tuple[list[str], str | None]:
    if not isinstance(crudo, list):
        return [], "execution_failures debe ser una lista"
    fallas: list[str] = []
    for item in crudo:
        if not isinstance(item, str):
            return [], "execution_failures contiene elementos no textuales"
        if len(fallas) < 10:
            fallas.append(_texto_una_linea(item))
    return fallas, None


@dataclass(frozen=True)
class ContextoAdmision:
    """De DÓNDE se copia una admisión: la instancia MO2 (carpeta de su INI) y el perfil.

    Es lo que el operador aprueba. Se captura al pedir la aprobación y se pasa a :meth:`XEditBridgeService.admitir`,
    que lo compara con el contexto vigente al copiar: si cambió durante la espera, no se copia nada.
    """

    ini_dir: Path | None
    perfil: str

    def resumen(self) -> str:
        """Texto para el operador: el perfil y la instancia exactos de los que se copiaría."""
        instancia = f"«{self.ini_dir.name}» ({self.ini_dir})" if self.ini_dir is not None else "sin instancia MO2"
        return f"perfil «{self.perfil}» de la instancia MO2 {instancia}"


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
        base: dict[str, Any] = {"plugin": _texto_una_linea(plugin, _LARGO_NOMBRE), "aprobado": False}
        try:
            validar_plugin(plugin)
            respuesta = await auditar_plugin(
                # ``is None``, no ``or``: un 0 explícito tiene que llegar a la validación de rango y rechazarse.
                self._instalacion(),
                plugin,
                timeout=TIMEOUT_POR_DEFECTO if timeout is None else timeout,
                margen_s=margen_s,
            )
        except (BridgeError, ValueError) as exc:
            logger.warning("Auditoría nativa sin veredicto para %r: %s", plugin, exc)
            return {**base, "success": False, "message": _texto_una_linea(exc)}
        return self._veredicto(plugin, respuesta)

    def _veredicto(self, plugin: str, respuesta: RespuestaAuditoria) -> dict[str, Any]:
        payload = respuesta.payload
        crudo = payload.get("untrusted_data")
        if crudo is not None and not isinstance(crudo, dict):
            return {
                "plugin": _texto_una_linea(plugin, _LARGO_NOMBRE),
                "success": False,
                "message": "respuesta del bridge con esquema inválido: untrusted_data no es un objeto",
                "aprobado": False,
                "no_admitido": False,
            }
        datos: dict[str, Any] = crudo if isinstance(crudo, dict) else {}
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
                "plugin": _texto_una_linea(plugin, _LARGO_NOMBRE),
                "success": False,
                "message": _texto_una_linea(mensaje),
                "aprobado": False,
                "no_admitido": no_admitido,
            }

        errores_esquema: list[str] = []
        ok_crudo = payload.get("ok")
        if not isinstance(ok_crudo, bool):
            errores_esquema.append("ok debe ser booleano")

        check_complete_crudo = datos.get("check_complete")
        if not isinstance(check_complete_crudo, bool):
            errores_esquema.append("check_complete debe ser booleano")
        completo = check_complete_crudo is True

        clean_crudo = datos.get("clean")
        if not isinstance(clean_crudo, bool):
            errores_esquema.append("clean debe ser booleano")
        limpio = clean_crudo is True

        truncated_crudo = datos.get("truncated")
        if not isinstance(truncated_crudo, bool):
            errores_esquema.append("truncated debe ser booleano")
        mcp_recortado = datos.get("mcp_response_truncated")
        if mcp_recortado is not None and not isinstance(mcp_recortado, bool):
            errores_esquema.append("mcp_response_truncated debe ser booleano")
        recortado = (truncated_crudo is True) or (mcp_recortado is True)

        integridad_cruda = datos.get("prospective_integrity")
        integridad_saneada: str | None
        if (
            not isinstance(integridad_cruda, str)
            or not integridad_cruda.strip()
            or _tiene_controles_o_formato(integridad_cruda)
        ):
            errores_esquema.append("prospective_integrity inválido o ausente")
            integridad_saneada = None
        else:
            integridad_saneada = _texto_una_linea(integridad_cruda, 40)

        contador_crudo = datos.get("native_counter_records")
        errores_nativos: int | None = contador_crudo if _es_entero_no_negativo(contador_crudo) else None
        if errores_nativos is None:
            errores_esquema.append("native_counter_records debe ser entero >= 0")

        procesados_crudo = datos.get("native_processed_records")
        registros_procesados: int | None = procesados_crudo if _es_entero_no_negativo(procesados_crudo) else None
        if registros_procesados is None:
            errores_esquema.append("native_processed_records debe ser entero >= 0")

        avisos: int | None
        if "check_warning_count" in datos:
            avisos_crudo = datos.get("check_warning_count")
            avisos = avisos_crudo if _es_entero_no_negativo(avisos_crudo) else None
            if avisos is None:
                errores_esquema.append("check_warning_count debe ser entero >= 0")
        else:
            avisos = 0

        hallazgos_crudo_raw = datos.get("findings")
        hallazgos, err_hallazgos = _validar_hallazgos(hallazgos_crudo_raw)
        if err_hallazgos is not None:
            errores_esquema.append(err_hallazgos)

        fallas_crudo_raw = datos.get("execution_failures")
        fallas, err_fallas = _validar_fallas(fallas_crudo_raw)
        if err_fallas is not None:
            errores_esquema.append(err_fallas)

        run_id = _validar_identificador(datos.get("run_id"))
        if run_id is None:
            errores_esquema.append("run_id inválido o ausente")

        log_path = _validar_ruta_log(datos.get("log_path"))
        if log_path is None:
            errores_esquema.append("log_path inválido o ausente")

        # Coherencia entre campos del propio bridge: nunca derivar aprobación de datos contradictorios.
        incoherencias = []
        if limpio and errores_nativos is not None and errores_nativos > 0:
            incoherencias.append(f"clean=true con {errores_nativos} errores nativos")
        if limpio and isinstance(hallazgos_crudo_raw, list) and hallazgos_crudo_raw:
            incoherencias.append(f"clean=true con {len(hallazgos_crudo_raw)} hallazgos")
        if limpio and isinstance(fallas_crudo_raw, list) and fallas_crudo_raw:
            incoherencias.append("clean=true con fallas de ejecución")
        if limpio and completo and integridad_cruda == "PASS" and not recortado and ok_crudo is False:
            incoherencias.append("ok=false con clean=true")

        tiene_fallas_ejecucion = isinstance(fallas_crudo_raw, list) and len(fallas_crudo_raw) > 0
        confiable = (
            ok_crudo is True
            and completo
            and integridad_cruda == "PASS"
            and not recortado
            and not tiene_fallas_ejecucion
            and not incoherencias
            and not errores_esquema
        )

        # De la causa más específica a la más general: incoherencias -> esquema -> recorte -> integridad -> incompleto.
        if confiable:
            mensaje = ""
        elif incoherencias:
            mensaje = "resultado incoherente del bridge, no se aprueba: " + "; ".join(incoherencias)
        elif errores_esquema:
            mensaje = "respuesta del bridge con esquema inválido: " + "; ".join(errores_esquema[:5])
        elif recortado:
            mensaje = "respuesta del bridge recortada: revisar el log crudo en log_path"
        elif integridad_cruda != "PASS":
            mensaje = f"auditoría nativa sin integridad prospectiva PASS (integridad={integridad_saneada})"
        else:
            razones_incompleto = list(fallas)
            if ok_crudo is False and not razones_incompleto:
                razones_incompleto.append("el bridge informó ok=false")
            mensaje = "auditoría nativa incompleta: " + (
                "; ".join(razones_incompleto) or "el chequeo no terminó de forma verificable"
            )

        aprobado = (ok_crudo is True) and confiable and limpio
        return {
            "plugin": _texto_una_linea(plugin, _LARGO_NOMBRE),
            "success": confiable,
            "message": _texto_una_linea(mensaje) if mensaje else "",
            "aprobado": aprobado,
            "clean": limpio,
            "check_complete": completo,
            "errores_nativos": errores_nativos,
            "registros_procesados": registros_procesados,
            "avisos": avisos,
            "hallazgos": hallazgos,
            "hallazgos_omitidos": (
                max(0, len(hallazgos_crudo_raw) - LIMITE_HALLAZGOS)
                if err_hallazgos is None and isinstance(hallazgos_crudo_raw, list)
                else 0
            ),
            "fallas": fallas,
            "integridad": integridad_saneada,
            "recortado": recortado,
            "bridge_version": _texto_una_linea(respuesta.version, 40),
            "run_id": run_id,
            "log_path": log_path,
            "no_admitido": False,
        }

    def contexto_de_admision(self) -> ContextoAdmision:
        """El contexto del que se copiaría hoy: instancia MO2 (carpeta del INI) y perfil activo."""
        return ContextoAdmision(self._rutas.get_mo2_instance_ini_dir(), self._rutas.get_active_profile())

    async def admitir(
        self,
        plugin: str,
        *,
        contexto: ContextoAdmision | None = None,
        requerir_contexto: bool = False,
    ) -> dict[str, Any]:
        """Copia ``plugin`` y sus masters del MO2 de Sky-Claw al laboratorio del bridge (MO2 no se toca).

        Solo debe llamarse después de una aprobación humana: la impone cada superficie. ``contexto`` es el que
        se mostró al operador al pedirla: si el vigente es otro, no se copia nada (la aprobación era para ese).
        """
        base: dict[str, Any] = {"plugin": _texto_una_linea(plugin, _LARGO_NOMBRE)}
        if requerir_contexto and contexto is None:
            logger.warning("Admisión de %r rechazada: falta el contexto de autorización obligatorio", plugin)
            return {
                **base,
                "success": False,
                "message": "admisión rechazada: falta el contexto de autorización de MO2 aprobado",
            }
        # El bridge re-deriva la instancia (base, mods, perfiles, juego) de su ModOrganizer.ini: se le
        # pasa la carpeta del MISMO INI que usa Sky-Claw, no la instalación (que en MO2 2.4+ no tiene INI)
        # ni la raíz de datos (que puede guardar una copia vieja).
        vigente = self.contexto_de_admision()
        if contexto is not None and contexto != vigente:
            logger.warning("Admisión de %r cancelada: el contexto cambió (%s -> %s)", plugin, contexto, vigente)
            return {
                **base,
                "success": False,
                "message": _texto_una_linea(
                    f"el contexto de MO2 cambió durante la espera de la aprobación (se aprobó {contexto.resumen()}; "
                    f"ahora es {vigente.resumen()}): no se copió nada, volvé a pedir la admisión",
                    largo=500,
                ),
            }
        mo2 = vigente.ini_dir
        if mo2 is None:
            return {
                **base,
                "success": False,
                "message": "MO2 no configurado: no se encontró el ModOrganizer.ini de la instancia activa",
            }
        try:
            datos = await admitir_plugin(self._instalacion(), mo2, vigente.perfil, plugin)
        except (BridgeError, ValueError) as exc:
            logger.warning("Admisión de %r no realizada: %s", plugin, exc)
            return {**base, "success": False, "message": _texto_una_linea(exc)}
        if datos.get("ok") is not True:
            return {
                **base,
                "success": False,
                "message": _texto_una_linea(datos.get("error") or "el bridge rechazó la admisión"),
            }
        admission_id = _validar_identificador(datos.get("admission_id"))
        logger.info("Plugin %s admitido en el xEdit Bridge (%s)", plugin, admission_id or "-")
        return {
            **base,
            "success": True,
            "message": "",
            "admission_id": admission_id,
            "clausura": _lista_textos(datos.get("closure")),
            "copiados": _lista_textos(datos.get("copied")),
            "reutilizados": _lista_textos(datos.get("reused")),
            "reemplazados": _lista_textos(datos.get("replaced")),
            "siguiente": "auditar_plugin_nativo",
        }


__all__ = ["LIMITE_HALLAZGOS", "ContextoAdmision", "XEditBridgeService"]
