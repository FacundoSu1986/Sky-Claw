"""Adaptador de transporte HITL por Telegram para transiciones Golden Master (P7-A5 PR 4).

Reutiliza:
- ``TelegramWebhook`` / ``TelegramPolling`` / ``TelegramSender`` (capa de transporte).
- ``owner_consent.verificar_identidad_telegram`` (única prueba de identidad del propietario).
- ``SesionTransicionGoldenMaster``, ``diagnosticar_estado_durable`` y
  ``abandonar_transicion_consumida`` (único consumidor autorizado).

Invariantes de seguridad:
1. Toda decisión mutante o diagnóstica valida identidad desde el update **crudo**
   de Telegram (chat privado, sin reenvíos, ``from.id == chat.id == operador``).
2. El espacio de callbacks ``gm:<accion>:<token>`` y comandos ``/gm*`` es
   estrictamente disjunto de ``hitl:`` (``HITLGuard``) y nunca cae al LLM/GUI/tools.
3. No existe segunda autoridad ni segunda compuerta de emisión: el interruptor
   ``EMISION_PRODUCTIVA_HABILITADA`` vive solo en ``approval.py`` y sigue en
   ``False``.
4. Coordinador single-flight no bloqueante en el event loop (ejecuta hashing y
   locks Win32 en un worker dedicado de 1 hilo, drenado con ``asyncio.shield``
   ante cancelaciones repetidas, desconexión o cierre del proceso).
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import dataclasses
import html
import logging
import pathlib
import secrets
import threading
from collections.abc import Callable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp

from sky_claw.app.comms.telegram_sender import (
    MAX_MESSAGE_LENGTH,
    TelegramMessage,
    TelegramRateLimitError,
    TelegramSender,
    TelegramSendError,
    TelegramUploadTimeoutError,
)
from sky_claw.app.security.network_gateway import (
    EgressViolationError,
    NetworkGatewayTimeoutError,
)
from sky_claw.local.frozen_runtime import approval as _approval_module
from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
    AutorizacionTransicionError,
    DiagnosticoDurable,
    ReanudacionNoDisponibleError,
    ReanudacionRequiereDuenoError,
    RegistroAbandonoTransicion,
    ResultadoTransicion,
    SesionTransicionGoldenMaster,
    SolicitudTransicion,
    abandonar_transicion_consumida,
    diagnosticar_estado_durable,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.errors import (
    ApprovalHitolNoDisponibleError,
    FrozenRuntimeError,
    IdentidadPropietarioNoVerificadaError,
)
from sky_claw.local.frozen_runtime.owner_consent import (
    DecisionConsentimiento,
    IdentidadPropietario,
    PresentacionConsentimiento,
    RegistroConsentimientos,
    verificar_identidad_telegram,
)

if TYPE_CHECKING:
    from sky_claw.local.frozen_runtime.approval import ConsentimientoAprobacion
    from sky_claw.local.frozen_runtime.effective_runtime import RuntimeProbes

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_GM_CALLBACK_NAMESPACE = "gm"
_GM_CALLBACK_ACTIONS = frozenset({"approve", "deny", "cancel"})
_GM_CALLBACK_MAX_BYTES = 64
_MAX_TOKENS_INVALIDADOS = 512
_ENTREGA_TIMEOUT_SEGUNDOS = 20.0
_TELEGRAM_MAX_CHAR_RESUMEN = 3800

_ACCION_A_DECISION: dict[str, DecisionConsentimiento] = {
    "approve": DecisionConsentimiento.APROBAR,
    "deny": DecisionConsentimiento.RECHAZAR,
    "cancel": DecisionConsentimiento.CANCELAR,
}
_COMANDO_A_DECISION: dict[str, DecisionConsentimiento] = {
    "/gm_aprobar": DecisionConsentimiento.APROBAR,
    "/gm_rechazar": DecisionConsentimiento.RECHAZAR,
    "/gm_cancelar": DecisionConsentimiento.CANCELAR,
}
_ETIQUETA_DECISION: dict[DecisionConsentimiento, str] = {
    DecisionConsentimiento.APROBAR: "APROBADA",
    DecisionConsentimiento.RECHAZAR: "RECHAZADA",
    DecisionConsentimiento.CANCELAR: "CANCELADA",
}
_CLAVES_REENVIO_ADICIONALES = (
    "forward_date",
    "forward_from",
    "forward_origin",
    "forward_from_chat",
    "forward_from_message_id",
    "forward_sender_name",
    "is_automatic_forward",
    "sender_chat",
    "business_connection_id",
    "via_bot",
    "sender_business_bot",
)
_CLAVES_UPDATE_NO_MENSAJE = (
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "message_reaction",
    "message_reaction_count",
)
_ERRORES_TRANSPORTE_TELEGRAM = (
    TelegramSendError,
    TelegramRateLimitError,
    TelegramUploadTimeoutError,
    aiohttp.ClientError,
    OSError,
    TimeoutError,
    EgressViolationError,
    NetworkGatewayTimeoutError,
)


class EntregaPresentacionError(RuntimeError):
    """Fallo controlado al entregar la presentación HITL por Telegram."""


def _escape_html(texto: object) -> str:
    if texto is None:
        return ""
    return html.escape(str(texto))


def build_gm_callback_data(action: str, token: str) -> str:
    """Construye ``callback_data`` acotado a 64 bytes UTF-8 para el espacio ``gm:``."""
    if action not in _GM_CALLBACK_ACTIONS:
        raise ValueError(f"Acción de callback Golden Master no soportada: {action!r}")
    if not token or ":" in token or any(ch.isspace() for ch in token):
        raise ValueError("Token de callback Golden Master inválido")
    encoded = f"{_GM_CALLBACK_NAMESPACE}:{action}:{token}"
    if len(encoded.encode("utf-8")) > _GM_CALLBACK_MAX_BYTES:
        raise ValueError("callback_data Golden Master excede 64 bytes UTF-8")
    return encoded


def parse_gm_callback_data(data: str) -> tuple[str, str] | None:
    """Parsea ``gm:<accion>:<token>`` de forma estricta o devuelve ``None``."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > _GM_CALLBACK_MAX_BYTES:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    namespace, action, token = parts
    if namespace != _GM_CALLBACK_NAMESPACE or action not in _GM_CALLBACK_ACTIONS:
        return None
    if not token or any(ch.isspace() for ch in token):
        return None
    return action, token


def es_callback_golden_master(data: object) -> bool:
    """Indica si el payload del callback pertenece al espacio ``gm:``."""
    return isinstance(data, str) and data.startswith(f"{_GM_CALLBACK_NAMESPACE}:")


def es_comando_golden_master(raw_text: object) -> bool:
    """Indica si el texto invoca un comando ``/gm`` o ``/gm_*``."""
    if not isinstance(raw_text, str):
        return False
    limpio = raw_text.strip()
    if not limpio:
        return False
    cmd = limpio.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return cmd == "/gm" or cmd.startswith("/gm_")


@dataclasses.dataclass(frozen=True, slots=True)
class ObjetivoTransicionGoldenMaster:
    """Configuración inmutable y confiable del destino para ``SesionTransicionGoldenMaster``.

    Nunca se construye desde texto libre recibido por Telegram ni desde el LLM.
    """

    storage_root: pathlib.Path
    mo2_instance_dir: pathlib.Path
    clone_id: str
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE
    managed_source_root: pathlib.Path | None = None
    outgoing_manual_runtime_root: pathlib.Path | None = None
    stock_source_root: pathlib.Path | None = None
    approved_stock_reference_digest: str | None = None
    approved_stock_reference_digest_file: pathlib.Path | None = None
    config_path: pathlib.Path | None = None
    environ: MutableMapping[str, str] | None = None
    probes: RuntimeProbes | None = None

    def to_sesion_kwargs(self) -> dict[str, Any]:
        """Devuelve los argumentos exactos para ``SesionTransicionGoldenMaster``."""
        kwargs: dict[str, Any] = {
            "storage_root": self.storage_root,
            "mo2_instance_dir": self.mo2_instance_dir,
            "clone_id": self.clone_id,
            "transition_policy": self.transition_policy,
            "managed_source_root": self.managed_source_root,
            "outgoing_manual_runtime_root": self.outgoing_manual_runtime_root,
            "stock_source_root": self.stock_source_root,
            "approved_stock_reference_digest": self.approved_stock_reference_digest,
            "approved_stock_reference_digest_file": self.approved_stock_reference_digest_file,
            "config_path": self.config_path,
        }
        if self.environ is not None:
            kwargs["environ"] = self.environ
        if self.probes is not None:
            kwargs["probes"] = self.probes
        return kwargs


def _leer_campo_opcional_config(local_cfg: Any, clave: str, seccion: Mapping[str, Any] | None) -> str | None:
    if seccion is not None:
        crudo = seccion.get(clave)
        if isinstance(crudo, str) and crudo.strip():
            return crudo.strip()
    if isinstance(local_cfg, Mapping):
        crudo = local_cfg.get(f"golden_master_{clave}")
    else:
        crudo = getattr(local_cfg, f"golden_master_{clave}", None)
    if isinstance(crudo, str) and crudo.strip():
        return crudo.strip()
    return None


def _ruta_opcional(valor: str | None) -> pathlib.Path | None:
    return pathlib.Path(valor) if valor else None


def resolver_objetivo_golden_master_desde_config(
    local_cfg: Any,
    *,
    config_path: pathlib.Path | None = None,
) -> ObjetivoTransicionGoldenMaster | None:
    """Lee el destino Golden Master desde la config local o devuelve ``None`` (fail-closed).

    No inventa rutas por defecto ni usa fallbacks heurísticos cuando falta alguno
    de los tres campos obligatorios (``storage_root``, ``mo2_instance_dir``, ``clone_id``).
    """
    if local_cfg is None:
        return None
    datos = local_cfg.as_dict if hasattr(local_cfg, "as_dict") and isinstance(local_cfg.as_dict, Mapping) else None
    seccion_cruda = datos.get("golden_master") if datos is not None else getattr(local_cfg, "golden_master", None)
    seccion = seccion_cruda if isinstance(seccion_cruda, Mapping) else None

    storage = _leer_campo_opcional_config(datos or local_cfg, "storage_root", seccion)
    mo2_dir = _leer_campo_opcional_config(datos or local_cfg, "mo2_instance_dir", seccion)
    clone_id = _leer_campo_opcional_config(datos or local_cfg, "clone_id", seccion)
    if not storage or not mo2_dir or not clone_id:
        return None

    politica_cruda = _leer_campo_opcional_config(datos or local_cfg, "transition_policy", seccion)
    if politica_cruda is None:
        politica = BinderTransitionPolicy.STRICT_PROVIDER_IDLE
    else:
        try:
            politica = BinderTransitionPolicy(politica_cruda)
        except ValueError:
            logger.warning(
                "golden_master.transition_policy inválida (%r): objetivo deshabilitado (fail-closed)",
                politica_cruda,
            )
            return None

    digest_file = _ruta_opcional(
        _leer_campo_opcional_config(datos or local_cfg, "approved_stock_reference_digest_file", seccion)
    )
    return ObjetivoTransicionGoldenMaster(
        storage_root=pathlib.Path(storage),
        mo2_instance_dir=pathlib.Path(mo2_dir),
        clone_id=clone_id,
        transition_policy=politica,
        managed_source_root=_ruta_opcional(
            _leer_campo_opcional_config(datos or local_cfg, "managed_source_root", seccion)
        ),
        outgoing_manual_runtime_root=_ruta_opcional(
            _leer_campo_opcional_config(datos or local_cfg, "outgoing_manual_runtime_root", seccion)
        ),
        stock_source_root=_ruta_opcional(_leer_campo_opcional_config(datos or local_cfg, "stock_source_root", seccion)),
        approved_stock_reference_digest=_leer_campo_opcional_config(
            datos or local_cfg, "approved_stock_reference_digest", seccion
        ),
        approved_stock_reference_digest_file=digest_file,
        config_path=config_path,
    )


@dataclasses.dataclass(slots=True)
class _SesionActiva:
    sesion: SesionTransicionGoldenMaster
    registro: RegistroConsentimientos
    solicitud: SolicitudTransicion
    token: str
    chat_id: int
    message_id: int
    fase: str = "ABIERTA"


def _es_entero_estricto(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _contiene_senales_de_reenvio_o_canal(obj: object) -> bool:
    if not isinstance(obj, Mapping):
        return False
    for clave in _CLAVES_REENVIO_ADICIONALES:
        if obj.get(clave) is not None:
            return True
    respuesta = obj.get("reply_to_message")
    return isinstance(respuesta, Mapping) and _contiene_senales_de_reenvio_o_canal(respuesta)


def _validar_forma_privada_strict(raw_update: Mapping[str, object]) -> None:
    """Exige invariantes adicionales de transporte sobre el update crudo de Telegram."""
    if not isinstance(raw_update, Mapping):
        raise IdentidadPropietarioNoVerificadaError("el update de Telegram no es un objeto")
    for clave_ajena in _CLAVES_UPDATE_NO_MENSAJE:
        if raw_update.get(clave_ajena) is not None:
            raise IdentidadPropietarioNoVerificadaError(f"Golden Master rechaza updates de tipo {clave_ajena}")
    if _contiene_senales_de_reenvio_o_canal(raw_update):
        raise IdentidadPropietarioNoVerificadaError("el update contiene marcadores de reenvío o canal externo")

    consulta = raw_update.get("callback_query")
    if consulta is not None and not isinstance(consulta, Mapping):
        raise IdentidadPropietarioNoVerificadaError("callback_query malformado")
    if isinstance(consulta, Mapping):
        if _contiene_senales_de_reenvio_o_canal(consulta):
            raise IdentidadPropietarioNoVerificadaError("el callback contiene marcadores de reenvío o canal")
        remitente = consulta.get("from")
        mensaje_cb = consulta.get("message")
        if not isinstance(mensaje_cb, Mapping):
            raise IdentidadPropietarioNoVerificadaError("el callback no trae un mensaje privado asociado")
        if _contiene_senales_de_reenvio_o_canal(mensaje_cb):
            raise IdentidadPropietarioNoVerificadaError("el mensaje del callback es reenviado")
        if not _es_entero_estricto(mensaje_cb.get("message_id")):
            raise IdentidadPropietarioNoVerificadaError("el mensaje del callback no trae message_id entero")
        chat = mensaje_cb.get("chat")
    else:
        if "message" in raw_update and not isinstance(raw_update.get("message"), Mapping):
            raise IdentidadPropietarioNoVerificadaError("el campo 'message' del update está malformado")
        crudo = raw_update.get("message")
        carga = crudo if isinstance(crudo, Mapping) else raw_update
        if _contiene_senales_de_reenvio_o_canal(carga):
            raise IdentidadPropietarioNoVerificadaError("el mensaje contiene marcadores de reenvío o canal")
        remitente = carga.get("from")
        chat = carga.get("chat")

    if not isinstance(remitente, Mapping):
        raise IdentidadPropietarioNoVerificadaError("el update no trae remitente")
    if "is_bot" in remitente and remitente["is_bot"] is not False:
        raise IdentidadPropietarioNoVerificadaError("Golden Master rechaza remitentes bot o is_bot inválido")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        raise IdentidadPropietarioNoVerificadaError("Golden Master exige chat explícitamente de tipo 'private'")


def _construir_teclado_golden_master(token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aprobar", "callback_data": build_gm_callback_data("approve", token)},
                {"text": "❌ Rechazar", "callback_data": build_gm_callback_data("deny", token)},
                {"text": "🚫 Cancelar", "callback_data": build_gm_callback_data("cancel", token)},
            ]
        ]
    }


class CoordinadorTelegramGoldenMaster:
    """Adaptador single-flight de Telegram para sesiones HITL de Golden Master.

    Toda validación de identidad pasa por ``verificar_identidad_telegram`` sobre
    el update crudo y todas las operaciones bloqueantes (TreeDigest, locks,
    binder) se ejecutan fuera del event loop bajo espera protegida por escudo.
    """

    def __init__(
        self,
        *,
        sender: TelegramSender,
        operador_autorizado: int | None,
        objetivo: ObjetivoTransicionGoldenMaster | None = None,
        delivery_timeout: float = _ENTREGA_TIMEOUT_SEGUNDOS,
    ) -> None:
        self._sender = sender
        self._operador_autorizado = operador_autorizado
        self._objetivo = objetivo
        self._delivery_timeout = delivery_timeout

        self._operacion_en_curso: bool = False
        self._cerrado: bool = False
        self._sesion_activa: _SesionActiva | None = None
        self._tokens_invalidados: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._tareas_activas: set[asyncio.Task[None]] = set()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._worker_fut_activo: asyncio.Future[Any] | None = None
        self._entregas_lock = threading.Lock()
        self._entregas_en_vuelo: set[concurrent.futures.Future[TelegramMessage]] = set()
        self._recargas_en_curso: int = 0
        self._reload_lock = asyncio.Lock()

    @property
    def tiene_solicitud_abierta(self) -> bool:
        return self._sesion_activa is not None and self._sesion_activa.fase == "ABIERTA"

    def _asegurar_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="skyclaw-gm-hitl",
            )
        return self._executor

    def _registrar_token_invalidado(self, token: str) -> None:
        self._tokens_invalidados[token] = None
        self._tokens_invalidados.move_to_end(token)
        while len(self._tokens_invalidados) > _MAX_TOKENS_INVALIDADOS:
            self._tokens_invalidados.popitem(last=False)

    def _verificar_identidad(self, raw_update: Mapping[str, object]) -> IdentidadPropietario:
        _validar_forma_privada_strict(raw_update)
        return verificar_identidad_telegram(
            raw_update,
            operador_configurado=self._operador_autorizado,
        )

    async def _enviar_seguro(
        self,
        chat_id: int,
        texto: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramMessage | None:
        try:
            return await self._sender.send(chat_id, texto, reply_markup=reply_markup)
        except _ERRORES_TRANSPORTE_TELEGRAM:
            logger.warning("Fallo de transporte al enviar mensaje Golden Master a %s", chat_id, exc_info=True)
            return None

    async def _editar_seguro(
        self,
        chat_id: int,
        message_id: int,
        texto: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._sender.edit_message(chat_id, message_id, texto, reply_markup=reply_markup)
        except _ERRORES_TRANSPORTE_TELEGRAM:
            logger.warning(
                "Fallo de transporte al editar mensaje Golden Master %s/%s",
                chat_id,
                message_id,
                exc_info=True,
            )

    async def _responder_callback_seguro(self, callback_id: object, text: str | None = None) -> None:
        if not isinstance(callback_id, str) or not callback_id:
            return
        try:
            await self._sender.answer_callback_query(callback_id, text=text)
        except _ERRORES_TRANSPORTE_TELEGRAM:
            logger.warning("No se pudo confirmar callback Golden Master %s", callback_id, exc_info=True)

    async def _ejecutar_en_worker(
        self,
        fn: Callable[[], _T],
        *,
        cancelacion_event: threading.Event | None = None,
    ) -> _T:
        """Ejecuta ``fn`` en el hilo único del coordinador, resistiendo cancelaciones repetidas."""
        loop = asyncio.get_running_loop()
        executor = self._asegurar_executor()
        worker_fut = loop.run_in_executor(executor, fn)
        self._worker_fut_activo = worker_fut
        current_task = asyncio.current_task()
        initial_cancels = current_task.cancelling() if current_task is not None else 0
        caller_cancelled = False

        try:
            while not worker_fut.done():
                try:
                    await asyncio.shield(worker_fut)
                except asyncio.CancelledError:
                    if current_task is None or current_task.cancelling() > initial_cancels:
                        caller_cancelled = True
                    else:
                        caller_cancelled = True
                    if cancelacion_event is not None:
                        cancelacion_event.set()
                        with self._entregas_lock:
                            entregas = list(self._entregas_en_vuelo)
                        for entrega_fut in entregas:
                            entrega_fut.cancel()
                except Exception as exc:  # noqa: BLE001 - worker_fut ya finalizó; el retorno/propagación ocurre abajo
                    logger.debug("Worker Golden Master finalizó con excepción %s", type(exc).__name__)
                    break
        finally:
            if self._worker_fut_activo is worker_fut:
                self._worker_fut_activo = None

        if caller_cancelled:
            if worker_fut.done() and not worker_fut.cancelled():
                _ = worker_fut.exception()
            raise asyncio.CancelledError
        return worker_fut.result()

    def _lanzar_tarea(self, coro: Any) -> asyncio.Task[None]:
        tarea = asyncio.create_task(coro)
        self._tareas_activas.add(tarea)
        tarea.add_done_callback(self._tareas_activas.discard)
        return tarea

    async def _invalidar_sesion_abierta(self, *, motivo: str) -> None:
        sesion_abierta = self._sesion_activa
        if sesion_abierta is None:
            return
        self._sesion_activa = None
        self._registrar_token_invalidado(sesion_abierta.token)
        sesion_abierta.registro.descartar_todo()
        await self._editar_seguro(
            sesion_abierta.chat_id,
            sesion_abierta.message_id,
            f"🚫 Solicitud Golden Master cerrada ({_escape_html(motivo)}).",
            reply_markup=None,
        )

    async def actualizar_transporte(
        self,
        *,
        sender: TelegramSender,
        operador_autorizado: int | None,
        objetivo: ObjetivoTransicionGoldenMaster | None = None,
        actualizar_objetivo: bool = False,
    ) -> None:
        """Actualiza sender/operador ante un hot-reload invalidando antes cualquier prompt abierto."""
        self._recargas_en_curso += 1
        try:
            async with self._reload_lock:
                await self.esperar_idle()
                await self._invalidar_sesion_abierta(motivo="recarga del transporte Telegram")
                self._sender = sender
                self._operador_autorizado = operador_autorizado
                if actualizar_objetivo or objetivo is not None:
                    self._objetivo = objetivo
        finally:
            self._recargas_en_curso -= 1

    async def esperar_idle(self) -> None:
        """Espera de manera determinista a que terminen todas las operaciones en curso."""
        while self._tareas_activas:
            pendientes = list(self._tareas_activas)
            await asyncio.gather(*pendientes, return_exceptions=True)

    async def cerrar(self) -> None:
        """Detiene entregas pendientes, espera la terminalidad del worker y descarta prompts abiertos."""
        self._cerrado = True
        with self._entregas_lock:
            entregas = list(self._entregas_en_vuelo)
        for fut in entregas:
            fut.cancel()

        worker_fut = self._worker_fut_activo
        if worker_fut is not None:
            while not worker_fut.done():
                try:
                    await asyncio.shield(worker_fut)
                except asyncio.CancelledError:
                    continue
                except Exception as exc:  # noqa: BLE001 - drenado seguro del worker al cerrar
                    logger.debug("Worker Golden Master cerrado con %s", type(exc).__name__)
                    break

        pendientes = [t for t in self._tareas_activas if not t.done()]
        if pendientes:
            try:
                await asyncio.wait_for(asyncio.gather(*pendientes, return_exceptions=True), timeout=0.5)
            except TimeoutError:
                for tarea in pendientes:
                    if not tarea.done():
                        tarea.cancel()
                await asyncio.gather(*pendientes, return_exceptions=True)

        await self._invalidar_sesion_abierta(motivo="cierre del servicio")
        if self._executor is not None:
            executor = self._executor
            self._executor = None
            await asyncio.to_thread(executor.shutdown, wait=True)

    # ------------------------------------------------------------------
    # Despacho de entradas crudas (webhook / polling / llamadas directas)
    # ------------------------------------------------------------------

    async def procesar_actualizacion(self, raw_update: Mapping[str, object]) -> None:
        """Ejecuta y aguarda en línea el manejo completo de un update crudo Golden Master."""
        if not isinstance(raw_update, Mapping):
            return
        if isinstance(raw_update.get("callback_query"), Mapping):
            tarea = await self.iniciar_callback(raw_update)
        else:
            tarea = await self.iniciar_comando(raw_update)
        if tarea is not None:
            await tarea

    async def iniciar_callback(self, raw_update: Mapping[str, object]) -> asyncio.Task[None] | None:
        """Valida e inicia en single-flight un callback ``gm:<accion>:<token>``."""
        if not isinstance(raw_update, Mapping):
            return None
        query = raw_update.get("callback_query")
        if not isinstance(query, Mapping):
            return None
        callback_id = query.get("id")
        data = query.get("data")
        if not es_callback_golden_master(data):
            return None

        try:
            identidad = self._verificar_identidad(raw_update)
        except IdentidadPropietarioNoVerificadaError as exc:
            logger.warning("Rechazo de identidad en callback Golden Master: %s", exc)
            await self._responder_callback_seguro(callback_id, text="No autorizado")
            return None

        assert isinstance(data, str)
        parsed = parse_gm_callback_data(data)
        if parsed is None:
            await self._responder_callback_seguro(callback_id, text="Callback Golden Master inválido")
            return None
        action, token = parsed

        if self._cerrado:
            await self._responder_callback_seguro(callback_id, text="Adaptador cerrado")
            return None
        if self._recargas_en_curso > 0:
            await self._responder_callback_seguro(callback_id, text="Recarga de transporte en curso")
            return None
        if self._objetivo is None:
            await self._responder_callback_seguro(callback_id, text="Golden Master no configurado")
            return None
        if token in self._tokens_invalidados:
            logger.warning("Replay detectado para token Golden Master")
            await self._responder_callback_seguro(callback_id, text="Solicitud ya procesada (replay rechazado)")
            return None

        sesion_activa = self._sesion_activa
        if sesion_activa is None or sesion_activa.token != token:
            await self._responder_callback_seguro(callback_id, text="Solicitud inexistente, expirada o de otra sesión")
            return None
        if sesion_activa.fase != "ABIERTA" or self._operacion_en_curso:
            await self._responder_callback_seguro(callback_id, text="Solicitud ya en procesamiento")
            return None

        # Reserva atómica en el event loop sin ningún await intermedio antes de crear la Task.
        sesion_activa.fase = "PROCESANDO"
        self._registrar_token_invalidado(token)
        self._operacion_en_curso = True

        decision = _ACCION_A_DECISION[action]
        cb_id = callback_id if isinstance(callback_id, str) and callback_id else None
        return self._lanzar_tarea(
            self._correr_decision_y_ejecucion(
                sesion_activa=sesion_activa,
                decision=decision,
                identidad=identidad,
                callback_id=cb_id,
            )
        )

    async def iniciar_comando(self, raw_update: Mapping[str, object]) -> asyncio.Task[None] | None:
        """Valida e inicia en single-flight un comando ``/gm_*`` desde un update crudo."""
        if not isinstance(raw_update, Mapping):
            return None
        msg_obj = raw_update.get("message")
        if "message" in raw_update and not isinstance(msg_obj, Mapping):
            return None
        msg = msg_obj if isinstance(msg_obj, Mapping) else raw_update
        raw_text = msg.get("text")
        if not es_comando_golden_master(raw_text):
            return None

        try:
            identidad = self._verificar_identidad(raw_update)
        except IdentidadPropietarioNoVerificadaError as exc:
            logger.warning("Rechazo de identidad en comando Golden Master: %s", exc)
            return None

        chat_id = int(identidad.sujeto)
        assert isinstance(raw_text, str)
        tokens = raw_text.strip().split(maxsplit=1)
        comando = tokens[0].split("@", 1)[0].lower()
        resto = tokens[1].strip() if len(tokens) > 1 else ""

        if self._cerrado:
            await self._enviar_seguro(chat_id, "⚠️ Adaptador Golden Master cerrado.")
            return None
        if self._recargas_en_curso > 0:
            await self._enviar_seguro(chat_id, "⏳ Transporte Telegram en recarga; reintentá en unos instantes.")
            return None
        if comando == "/gm" or comando == "/gm_ayuda":
            if resto:
                await self._enviar_seguro(chat_id, "❌ /gm_ayuda no admite argumentos.")
                return None
            await self._enviar_seguro(chat_id, self._texto_ayuda())
            return None
        if self._objetivo is None:
            await self._enviar_seguro(
                chat_id,
                "❌ Golden Master no configurado en config.toml (storage_root / mo2_instance_dir / clone_id).",
            )
            return None

        if comando == "/gm_estado":
            return await self._admitir_estado(chat_id=chat_id, resto=resto)
        if comando == "/gm_preparar":
            return await self._admitir_preparar(chat_id=chat_id, resto=resto, identidad=identidad)
        if comando in _COMANDO_A_DECISION:
            return await self._admitir_decision_texto(
                chat_id=chat_id,
                decision=_COMANDO_A_DECISION[comando],
                resto=resto,
                identidad=identidad,
            )
        if comando == "/gm_reanudar":
            return await self._admitir_reanudar(chat_id=chat_id, resto=resto)
        if comando == "/gm_abandonar":
            return await self._admitir_abandonar(chat_id=chat_id, resto=resto, identidad=identidad)

        await self._enviar_seguro(
            chat_id, f"❌ Comando Golden Master no reconocido: <code>{_escape_html(comando)}</code>."
        )
        return None

    async def _admitir_estado(self, *, chat_id: int, resto: str) -> asyncio.Task[None] | None:
        if resto:
            await self._enviar_seguro(chat_id, "❌ /gm_estado no admite argumentos.")
            return None
        if self._operacion_en_curso:
            await self._enviar_seguro(
                chat_id,
                "⏳ Hay una operación Golden Master en curso; reintentá en unos instantes.",
            )
            return None
        self._operacion_en_curso = True
        return self._lanzar_tarea(self._correr_diagnostico(chat_id=chat_id))

    async def _admitir_preparar(
        self,
        *,
        chat_id: int,
        resto: str,
        identidad: IdentidadPropietario,
    ) -> asyncio.Task[None] | None:
        if resto:
            await self._enviar_seguro(
                chat_id,
                "❌ /gm_preparar no admite rutas ni argumentos por Telegram (usa únicamente config verificada).",
            )
            return None
        if self._operacion_en_curso or self._sesion_activa is not None:
            await self._enviar_seguro(
                chat_id,
                "⛔ Ya existe una operación o solicitud Golden Master abierta.",
            )
            return None
        self._operacion_en_curso = True
        return self._lanzar_tarea(self._correr_preparar(chat_id=chat_id, identidad=identidad))

    async def _admitir_decision_texto(
        self,
        *,
        chat_id: int,
        decision: DecisionConsentimiento,
        resto: str,
        identidad: IdentidadPropietario,
    ) -> asyncio.Task[None] | None:
        partes = resto.split()
        if len(partes) != 1:
            await self._enviar_seguro(chat_id, "❌ Debés indicar exactamente el ID de solicitud activo.")
            return None
        solicitud_id_pedida = partes[0]
        if self._operacion_en_curso:
            await self._enviar_seguro(chat_id, "⏳ Ya hay una operación Golden Master en curso.")
            return None
        sesion_activa = self._sesion_activa
        if sesion_activa is None:
            await self._enviar_seguro(chat_id, "❌ No hay solicitud Golden Master abierta o ya expiró.")
            return None
        if sesion_activa.solicitud.presentacion.solicitud_id != solicitud_id_pedida:
            await self._enviar_seguro(
                chat_id,
                "❌ El ID indicado no coincide con la solicitud abierta actual.",
            )
            return None
        if sesion_activa.fase != "ABIERTA":
            await self._enviar_seguro(chat_id, "⏳ La solicitud ya está siendo procesada.")
            return None

        sesion_activa.fase = "PROCESANDO"
        self._registrar_token_invalidado(sesion_activa.token)
        self._operacion_en_curso = True
        return self._lanzar_tarea(
            self._correr_decision_y_ejecucion(
                sesion_activa=sesion_activa,
                decision=decision,
                identidad=identidad,
                callback_id=None,
            )
        )

    async def _admitir_reanudar(self, *, chat_id: int, resto: str) -> asyncio.Task[None] | None:
        partes = resto.split()
        if len(partes) != 1:
            await self._enviar_seguro(chat_id, "❌ Uso: <code>/gm_reanudar &lt;transition_id&gt;</code>")
            return None
        if self._operacion_en_curso or self._sesion_activa is not None:
            await self._enviar_seguro(
                chat_id,
                "⛔ No se puede reanudar mientras exista otra operación o solicitud abierta.",
            )
            return None
        self._operacion_en_curso = True
        return self._lanzar_tarea(self._correr_reanudar(chat_id=chat_id, transition_id=partes[0]))

    async def _admitir_abandonar(
        self,
        *,
        chat_id: int,
        resto: str,
        identidad: IdentidadPropietario,
    ) -> asyncio.Task[None] | None:
        partes = resto.split(maxsplit=1)
        if len(partes) != 2 or not partes[0].strip() or not partes[1].strip():
            await self._enviar_seguro(
                chat_id,
                "❌ Uso: <code>/gm_abandonar &lt;transition_id&gt; &lt;motivo&gt;</code> (el motivo es obligatorio).",
            )
            return None
        if self._operacion_en_curso or self._sesion_activa is not None:
            await self._enviar_seguro(
                chat_id,
                "⛔ No se puede abandonar mientras exista otra operación o solicitud abierta.",
            )
            return None
        transition_id, motivo = partes[0].strip(), partes[1].strip()
        self._operacion_en_curso = True
        return self._lanzar_tarea(
            self._correr_abandonar(chat_id=chat_id, transition_id=transition_id, motivo=motivo, identidad=identidad)
        )

    # ------------------------------------------------------------------
    # Ejecución off-loop protegida contra cancelación y cortes
    # ------------------------------------------------------------------

    async def _correr_diagnostico(self, *, chat_id: int) -> None:
        objetivo = self._objetivo
        assert objetivo is not None
        try:
            diagnostico = await self._ejecutar_en_worker(lambda: diagnosticar_estado_durable(objetivo.storage_root))
            if not self._cerrado:
                await self._enviar_seguro(chat_id, self._formatear_diagnostico(diagnostico))
        except FrozenRuntimeError as exc:
            if not self._cerrado:
                await self._enviar_seguro(chat_id, f"❌ Diagnóstico falló cerrado: {_escape_html(exc)}")
        finally:
            self._operacion_en_curso = False

    async def _correr_preparar(self, *, chat_id: int, identidad: IdentidadPropietario) -> None:
        objetivo = self._objetivo
        assert objetivo is not None
        loop = asyncio.get_running_loop()
        registro = RegistroConsentimientos()
        sesion = SesionTransicionGoldenMaster(**objetivo.to_sesion_kwargs(), registro=registro)
        entrega_ref: dict[str, Any] = {}
        cancelacion_pedida = threading.Event()

        def _capturar_si_respondio(
            fut_entregada: concurrent.futures.Future[TelegramMessage],
            token_entregado: str,
        ) -> None:
            if not fut_entregada.done() or fut_entregada.cancelled() or fut_entregada.exception() is not None:
                return
            entregado = fut_entregada.result(timeout=0)
            msg_id_tardio = getattr(entregado, "message_id", None)
            if not isinstance(msg_id_tardio, bool) and isinstance(msg_id_tardio, int) and msg_id_tardio > 0:
                entrega_ref["token"] = token_entregado
                entrega_ref["message_id"] = msg_id_tardio

        def _entregar(presentacion: PresentacionConsentimiento) -> None:
            if self._cerrado or cancelacion_pedida.is_set():
                raise EntregaPresentacionError("preparación Golden Master cancelada antes de entregar")
            # Renderizado fail-closed ANTES de crear/enviar la coroutine: nunca entregar prompt parcial ni botones sin scope íntegro.
            mensaje_html = _formatear_mensaje_presentacion_desde_pres(presentacion)
            token = secrets.token_urlsafe(16)
            coro = self._sender.send(
                chat_id,
                mensaje_html,
                reply_markup=_construir_teclado_golden_master(token),
            )
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError as exc:
                coro.close()
                raise EntregaPresentacionError(f"no se pudo agendar la entrega por Telegram: {exc}") from exc
            with self._entregas_lock:
                if self._cerrado or cancelacion_pedida.is_set():
                    fut.cancel()
                    raise EntregaPresentacionError("adaptador Golden Master cerrado durante la entrega")
                self._entregas_en_vuelo.add(fut)
            try:
                mensaje = fut.result(timeout=self._delivery_timeout)
            except concurrent.futures.CancelledError as exc:
                fut.cancel()
                _capturar_si_respondio(fut, token)
                raise EntregaPresentacionError("entrega Golden Master cancelada por cierre") from exc
            except (*_ERRORES_TRANSPORTE_TELEGRAM, TimeoutError, RuntimeError) as exc:
                fut.cancel()
                _capturar_si_respondio(fut, token)
                raise EntregaPresentacionError(f"fallo al entregar presentación por Telegram: {exc}") from exc
            except BaseException:
                fut.cancel()
                _capturar_si_respondio(fut, token)
                raise
            finally:
                with self._entregas_lock:
                    self._entregas_en_vuelo.discard(fut)

            msg_id = getattr(mensaje, "message_id", None)
            if isinstance(msg_id, bool) or not isinstance(msg_id, int) or msg_id <= 0:
                raise EntregaPresentacionError(
                    "la entrega por Telegram no devolvió un message_id entero válido (rechazo fail-closed)"
                )
            entrega_ref["token"] = token
            entrega_ref["message_id"] = msg_id

        try:
            solicitud = await self._ejecutar_en_worker(
                lambda: sesion.preparar(identidad=identidad, entregar=_entregar),
                cancelacion_event=cancelacion_pedida,
            )
            if self._cerrado:
                registro.descartar_todo()
                self._registrar_token_invalidado(entrega_ref["token"])
                await self._editar_seguro(
                    chat_id,
                    entrega_ref["message_id"],
                    "🚫 Solicitud Golden Master cerrada durante el apagado.",
                    reply_markup=None,
                )
                return
            self._sesion_activa = _SesionActiva(
                sesion=sesion,
                registro=registro,
                solicitud=solicitud,
                token=entrega_ref["token"],
                chat_id=chat_id,
                message_id=entrega_ref["message_id"],
                fase="ABIERTA",
            )
        except asyncio.CancelledError:
            registro.descartar_todo()
            if "message_id" in entrega_ref:
                self._registrar_token_invalidado(entrega_ref["token"])
                if not self._cerrado:
                    await self._editar_seguro(
                        chat_id,
                        entrega_ref["message_id"],
                        "🚫 Preparación Golden Master cancelada.",
                        reply_markup=None,
                    )
            raise
        except (EntregaPresentacionError, FrozenRuntimeError) as exc:
            registro.descartar_todo()
            if "message_id" in entrega_ref:
                self._registrar_token_invalidado(entrega_ref["token"])
                if not self._cerrado:
                    await self._editar_seguro(
                        chat_id,
                        entrega_ref["message_id"],
                        f"❌ Preparación abortada: {_escape_html(exc)}",
                        reply_markup=None,
                    )
            elif not self._cerrado:
                await self._enviar_seguro(chat_id, f"❌ No se pudo preparar la solicitud: {_escape_html(exc)}")
        finally:
            self._operacion_en_curso = False

    async def _correr_decision_y_ejecucion(
        self,
        *,
        sesion_activa: _SesionActiva,
        decision: DecisionConsentimiento,
        identidad: IdentidadPropietario,
        callback_id: str | None = None,
    ) -> None:
        chat_id = sesion_activa.chat_id

        def _trabajo_consentimiento() -> tuple[ConsentimientoAprobacion | None, ResultadoTransicion | None]:
            consentimiento = sesion_activa.sesion.decidir_y_emitir(
                solicitud=sesion_activa.solicitud,
                decision=decision,
                identidad=identidad,
            )
            if decision is not DecisionConsentimiento.APROBAR or consentimiento is None:
                return None, None
            resultado = sesion_activa.sesion.ejecutar(
                solicitud=sesion_activa.solicitud,
                consentimiento=consentimiento,
            )
            return consentimiento, resultado

        try:
            if callback_id is not None:
                await self._responder_callback_seguro(callback_id, text="Procesando decisión...")
            await self._editar_seguro(
                sesion_activa.chat_id,
                sesion_activa.message_id,
                f"⏳ Procesando decisión ({_ETIQUETA_DECISION[decision]})...",
                reply_markup=None,
            )
            _consentimiento, resultado = await self._ejecutar_en_worker(_trabajo_consentimiento)
            if self._cerrado:
                return
            if resultado is not None:
                await self._enviar_seguro(chat_id, self._formatear_resultado_transicion(resultado))
            else:
                etiqueta = _ETIQUETA_DECISION[decision]
                await self._enviar_seguro(
                    chat_id,
                    f"ℹ️ Solicitud <code>{_escape_html(sesion_activa.solicitud.presentacion.solicitud_id)}</code> "
                    f"<b>{etiqueta}</b> sin emisión.",
                )
        except ApprovalHitolNoDisponibleError as exc:
            if not self._cerrado:
                await self._enviar_seguro(
                    chat_id,
                    "🔒 Consentimiento verificado, pero la emisión sigue cerrada "
                    f"(<code>EMISION_PRODUCTIVA_HABILITADA = False</code>): {_escape_html(exc)}",
                )
        except (FrozenRuntimeError, AutorizacionTransicionError) as exc:
            if not self._cerrado:
                await self._enviar_seguro(
                    chat_id,
                    f"❌ La transición Golden Master falló cerrado: {_escape_html(exc)}",
                )
        finally:
            sesion_activa.registro.descartar_todo()
            if self._sesion_activa is sesion_activa:
                self._sesion_activa = None
            self._operacion_en_curso = False

    async def _correr_reanudar(self, *, chat_id: int, transition_id: str) -> None:
        objetivo = self._objetivo
        assert objetivo is not None

        def _trabajo_reanudar() -> ResultadoTransicion:
            sesion = SesionTransicionGoldenMaster(**objetivo.to_sesion_kwargs())
            return sesion.reanudar(transition_id=transition_id)

        try:
            resultado = await self._ejecutar_en_worker(_trabajo_reanudar)
            if not self._cerrado:
                await self._enviar_seguro(chat_id, self._formatear_resultado_transicion(resultado))
        except ReanudacionRequiereDuenoError as exc:
            if not self._cerrado:
                await self._enviar_seguro(
                    chat_id,
                    "🛑 La transición ya publicó <code>PENDING</code> y requiere recuperación manual del dueño "
                    f"(<code>recuperar_transicion_interrumpida</code> + <code>reconciliar_journal</code>): "
                    f"{_escape_html(exc)}",
                )
        except (ReanudacionNoDisponibleError, FrozenRuntimeError, AutorizacionTransicionError) as exc:
            if not self._cerrado:
                await self._enviar_seguro(chat_id, f"❌ No se pudo reanudar: {_escape_html(exc)}")
        finally:
            self._operacion_en_curso = False

    async def _correr_abandonar(
        self,
        *,
        chat_id: int,
        transition_id: str,
        motivo: str,
        identidad: IdentidadPropietario,
    ) -> None:
        objetivo = self._objetivo
        assert objetivo is not None

        def _trabajo_abandonar() -> RegistroAbandonoTransicion:
            return abandonar_transicion_consumida(
                objetivo.storage_root,
                transition_id=transition_id,
                identidad=identidad,
                motivo=motivo,
            )

        try:
            registro = await self._ejecutar_en_worker(_trabajo_abandonar)
            if not self._cerrado:
                await self._enviar_seguro(
                    chat_id,
                    "🗑️ Abandono durable registrado.\n"
                    f"<b>Transición:</b> <code>{_escape_html(registro.transition_id)}</code>\n"
                    f"<b>Approval seq:</b> <code>{registro.approval_seq}</code>\n"
                    f"<b>Digest:</b> <code>{_escape_html(registro.record_digest)}</code>",
                )
        except FrozenRuntimeError as exc:
            if not self._cerrado:
                await self._enviar_seguro(chat_id, f"❌ No se pudo abandonar la transición: {_escape_html(exc)}")
        finally:
            self._operacion_en_curso = False

    # ------------------------------------------------------------------
    # Formateadores de salida HTML escapada
    # ------------------------------------------------------------------

    def _formatear_diagnostico(self, diagnostico: DiagnosticoDurable) -> str:
        abierta = (
            self._sesion_activa.solicitud.presentacion.solicitud_id if self._sesion_activa is not None else "ninguna"
        )
        en_curso = diagnostico.transicion_en_curso or "ninguna"
        reanudable = diagnostico.transicion_reanudable or "ninguna"
        recovery = ", ".join(diagnostico.registros_recuperacion) or "ninguno"
        emision = _approval_module.EMISION_PRODUCTIVA_HABILITADA
        return (
            "📊 <b>Estado Golden Master</b>\n\n"
            f"<b>Estado durable:</b> <code>{_escape_html(diagnostico.estado.value)}</code>\n"
            f"<b>Emisión productiva habilitada:</b> <code>{emision}</code>\n"
            f"<b>Transición en curso (PENDING):</b> <code>{_escape_html(en_curso)}</code>\n"
            f"<b>Transición reanudable:</b> <code>{_escape_html(reanudable)}</code>\n"
            f"<b>Registros de recuperación:</b> <code>{_escape_html(recovery)}</code>\n"
            f"<b>Solicitud abierta en memoria:</b> <code>{_escape_html(abierta)}</code>\n"
            f"<b>Detalle:</b> {_escape_html(diagnostico.motivo)}"
        )

    @staticmethod
    def _formatear_resultado_transicion(resultado: ResultadoTransicion) -> str:
        clone_str = (
            str(resultado.binder.target_clone_root)
            if resultado.binder is not None and resultado.binder.target_clone_root is not None
            else "n/a"
        )
        return (
            "✅ <b>Transición Golden Master finalizada</b>\n\n"
            f"<b>Escenario:</b> <code>{_escape_html(resultado.escenario.value)}</code>\n"
            f"<b>Transición:</b> <code>{_escape_html(resultado.transicion_id)}</code>\n"
            f"<b>Approval seq:</b> <code>{resultado.aprobacion_seq}</code>\n"
            f"<b>Clone activo:</b> <code>{_escape_html(clone_str)}</code>"
        )

    @staticmethod
    def _texto_ayuda() -> str:
        return (
            "🛡️ <b>Comandos Golden Master HITL</b>\n\n"
            "• <code>/gm_estado</code> — Diagnóstico durable y estado de compuertas.\n"
            "• <code>/gm_preparar</code> — Calcula evidencia fresca y presenta botones HITL.\n"
            "• <code>/gm_aprobar &lt;solicitud_id&gt;</code> / <code>/gm_rechazar &lt;id&gt;</code> / "
            "<code>/gm_cancelar &lt;id&gt;</code> — Decisión por texto.\n"
            "• <code>/gm_reanudar &lt;transition_id&gt;</code> — Reanuda transición consumida pre-PENDING.\n"
            "• <code>/gm_abandonar &lt;transition_id&gt; &lt;motivo&gt;</code> — Registra abandono durable."
        )


def _formatear_mensaje_presentacion_desde_pres(presentacion: PresentacionConsentimiento) -> str:
    """Formatea la presentación HITL completa para Telegram o falla cerrado si no cabe íntegra.

    Nunca recorta el cuerpo del ``ApprovalScope`` ni el ``approval_scope_digest``:
    el propietario no puede recibir botones aprobables sin ver la presentación completa.
    """
    solicitud_id = presentacion.solicitud_id.strip()
    texto = presentacion.texto
    digest = presentacion.approval_scope_digest.strip()
    if not solicitud_id or not texto.strip() or not digest:
        raise EntregaPresentacionError(
            "presentación Golden Master incompleta (faltan solicitud_id, texto o approval_scope_digest): "
            "rechazo fail-closed antes de entregar"
        )
    if digest not in texto:
        raise EntregaPresentacionError(
            "la presentación Golden Master no incluye el approval_scope_digest comprometido en su cuerpo: "
            "rechazo fail-closed antes de entregar"
        )

    cuerpo_escapado = _escape_html(texto)
    solicitud_escapada = _escape_html(solicitud_id)
    digest_escapado = _escape_html(digest)
    mensaje = (
        "🛡️ <b>Autorización HITL — Golden Master</b>\n\n"
        f"<b>Solicitud:</b> <code>{solicitud_escapada}</code>\n"
        f"<b>Approval seq:</b> <code>{presentacion.approval_seq}</code>\n\n"
        f"<pre>{cuerpo_escapado}</pre>\n\n"
        "<i>Confirmá con los botones o respondé <code>/gm_aprobar &lt;id&gt;</code> / "
        "<code>/gm_rechazar &lt;id&gt;</code> / <code>/gm_cancelar &lt;id&gt;</code>.</i>"
    )
    limite_seguro = min(_TELEGRAM_MAX_CHAR_RESUMEN, MAX_MESSAGE_LENGTH)
    if (
        len(mensaje) > limite_seguro
        or digest_escapado not in mensaje
        or solicitud_escapada not in mensaje
        or f"<code>{presentacion.approval_seq}</code>" not in mensaje
    ):
        raise EntregaPresentacionError(
            f"la presentación HITL completa excede el límite seguro de un único mensaje en Telegram "
            f"({len(mensaje)} > {limite_seguro} caracteres) o perdió campos obligatorios: "
            "rechazo fail-closed sin truncar para preservar el scope y approval_scope_digest completos"
        )
    return mensaje


async def asegurar_coordinador_golden_master(
    ctx: Any,
    *,
    sender: TelegramSender,
    authorized_user_id: int | None,
    local_cfg: Any = None,
) -> CoordinadorTelegramGoldenMaster:
    """Reutiliza o construye el coordinador del contexto de forma segura ante hot-reload/mocks."""
    existente = getattr(ctx, "golden_master_coordinator", None)
    objetivo_guardado = getattr(ctx, "_golden_master_objetivo", None)
    objetivo_ctx = objetivo_guardado if isinstance(objetivo_guardado, ObjetivoTransicionGoldenMaster) else None
    actualizar_objetivo = local_cfg is not None
    objetivo = (
        resolver_objetivo_golden_master_desde_config(local_cfg, config_path=getattr(ctx, "config_path", None))
        if actualizar_objetivo
        else objetivo_ctx
    )
    if actualizar_objetivo:
        ctx._golden_master_objetivo = objetivo

    if isinstance(existente, CoordinadorTelegramGoldenMaster):
        await existente.actualizar_transporte(
            sender=sender,
            operador_autorizado=authorized_user_id,
            objetivo=objetivo,
            actualizar_objetivo=actualizar_objetivo,
        )
        return existente

    coordinador = CoordinadorTelegramGoldenMaster(
        sender=sender,
        operador_autorizado=authorized_user_id,
        objetivo=objetivo,
    )
    ctx.golden_master_coordinator = coordinador
    return coordinador
