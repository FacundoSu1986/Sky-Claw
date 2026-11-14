"""Pruebas de transporte Telegram HITL para Golden Master (P7-A5 PR 4).

Cubre:
- Tokenización y separación estricta del espacio ``gm:`` frente a ``hitl:`` y al LLM.
- Validación de identidad exclusivamente desde updates crudos de Telegram.
- Rechazo de reenvíos, chats no privados, terceros, replay, doble clic, sesiones expiradas y args arbitrarios.
- Bandera ``EMISION_PRODUCTIVA_HABILITADA = False`` intacta de extremo a extremo.
- Camino autorizado completo en rig aislado Windows/POSIX (con monkeypatch explícito de prueba).
- Coordinador single-flight no bloqueante, seguro ante desconexión, cancelación con worker vivo y shutdown.
- Diagnóstico (``/gm_estado``), reanudación pre-PENDING (``/gm_reanudar``), post-PENDING y abandono (``/gm_abandonar``).
- Fronteras AST de aislamiento respecto a LLM, GUI, tools y ``HITLGuard``, y simetría de los tres llamadores.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import html
import os
import pathlib
import threading
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.golden_master_hitl import (
    _TELEGRAM_MAX_CHAR_RESUMEN,
    CoordinadorTelegramGoldenMaster,
    EntregaPresentacionError,
    ObjetivoTransicionGoldenMaster,
    _formatear_mensaje_presentacion_desde_pres,
    asegurar_coordinador_golden_master,
    build_gm_callback_data,
    parse_gm_callback_data,
    resolver_objetivo_golden_master_desde_config,
)
from sky_claw.app.comms.telegram import (
    TelegramHITLMessageRegistry,
    TelegramWebhook,
)
from sky_claw.app.comms.telegram_polling import TelegramPolling
from sky_claw.app.comms.telegram_sender import (
    TelegramMessage,
    TelegramRateLimitError,
    TelegramSendError,
    TelegramUploadTimeoutError,
)
from sky_claw.app.security.hitl import HITLGuard
from sky_claw.config import Config
from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import autorizacion_hitl_transicion as hitl_wiring
from sky_claw.local.frozen_runtime import binder_recovery as recovery_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import mo2_ini_exclusion
from sky_claw.local.frozen_runtime import owner_consent as owner_consent_module
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    cargar_ledger_aprobacion,
    derivar_autoridad_aprobacion,
)
from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
    EstadoDurable,
    SesionTransicionGoldenMaster,
    diagnosticar_estado_durable,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.models import ObservationState
from sky_claw.local.frozen_runtime.owner_consent import (
    DecisionConsentimiento,
    verificar_identidad_telegram,
)
from sky_claw.local.frozen_runtime.storage import (
    issuances_state_dir,
    transition_journal_path,
)
from tests._rig_p7_a5 import (
    RigStockManual,
    aplicar_parche_identidad_pe,
    snapshot_sha256_recursivo,
)

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
OPERADOR = 424242
OTRO_USUARIO = 999999
E2E_POSIX_PEDIDO = os.environ.get("SKY_CLAW_PR3_E2E_POSIX") == "1"
SOLO_WINDOWS = pytest.mark.skipif(
    os.name != "nt" and not E2E_POSIX_PEDIDO,
    reason="la mutación real de modorganizer.ini requiere Win32 FILE_SHARE_NONE (o SKY_CLAW_PR3_E2E_POSIX=1)",
)


@pytest.fixture(autouse=True)
def _identidad_pe(monkeypatch: pytest.MonkeyPatch) -> None:
    aplicar_parche_identidad_pe(monkeypatch)


class _ExclusionPosix:
    """Doble POSIX acotado sólo cuando se solicita SKY_CLAW_PR3_E2E_POSIX=1."""

    def __init__(self, ruta: pathlib.Path) -> None:
        self.ruta = pathlib.Path(ruta)
        self._abierta = True
        self.diagnostico = mo2_ini_exclusion.DiagnosticoExclusion(
            estado=mo2_ini_exclusion.EstadoExclusion.ESTABLISHED,
            ruta=self.ruta,
            motivo="doble POSIX de test",
            intentos=0,
        )

    @property
    def autoriza(self) -> bool:
        return self._abierta

    @property
    def estado(self) -> mo2_ini_exclusion.EstadoExclusion:
        return self.diagnostico.estado

    def leer_bytes(self) -> bytes:
        if not self._abierta:
            raise mo2_ini_exclusion.Mo2IniExclusionError("exclusión cerrada")
        return self.ruta.read_bytes()

    def huella(self) -> str:
        return hashlib.sha256(self.leer_bytes()).hexdigest()

    def verificar_huella(self, esperada: str) -> None:
        if self.huella() != esperada:
            raise mo2_ini_exclusion.Mo2IniExclusionError("huella divergente")

    def escribir_bytes(self, datos: bytes, *, al_escribir: object = None) -> None:
        if not self._abierta:
            raise mo2_ini_exclusion.Mo2IniExclusionError("exclusión cerrada")
        if callable(al_escribir):
            al_escribir()
        self.ruta.write_bytes(datos)

    def cerrar(self) -> None:
        self._abierta = False


@pytest.fixture
def exclusion_de_test(monkeypatch: pytest.MonkeyPatch) -> str:
    if os.name == "nt":
        return "real"
    monkeypatch.setattr(
        binder_module,
        "adquirir_exclusion_ini",
        lambda ruta, **_kwargs: _ExclusionPosix(pathlib.Path(ruta)),
    )
    return "doble_posix"


def _objetivo_desde_rig(rig: RigStockManual) -> ObjetivoTransicionGoldenMaster:
    return ObjetivoTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        clone_id=rig.clone_id,
        transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        managed_source_root=rig.managed_source,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest_file=rig.approved_digest_file,
        config_path=rig.config_path,
        environ=rig.environ,
        probes=rig.probes_globales_activos(),
    )


def _crear_sender_simulado() -> MagicMock:
    sender = MagicMock()
    siguiente_id = {"valor": 100}
    sender._mensajes_enviados = []

    async def _send(chat_id: int, text: str, reply_markup: dict | None = None, **_kwargs: object) -> TelegramMessage:
        siguiente_id["valor"] += 1
        msg = TelegramMessage(chat_id=chat_id, message_id=siguiente_id["valor"])
        sender._mensajes_enviados.append(msg)
        return msg

    sender.send = AsyncMock(side_effect=_send)
    sender.edit_message = AsyncMock()
    sender.answer_callback_query = AsyncMock()
    return sender


def _construir_entorno(
    rig: RigStockManual,
    *,
    operador: int | None = OPERADOR,
    sender: MagicMock | None = None,
    hitl: HITLGuard | None = None,
) -> tuple[CoordinadorTelegramGoldenMaster, TelegramWebhook, TelegramPolling, MagicMock, MagicMock]:
    sender_mock = sender or _crear_sender_simulado()
    router = MagicMock()
    router.chat = AsyncMock(return_value="respuesta-llm-no-esperada")
    session = MagicMock(spec=aiohttp.ClientSession)
    coord = CoordinadorTelegramGoldenMaster(
        sender=sender_mock,
        operador_autorizado=operador,
        objetivo=_objetivo_desde_rig(rig),
    )
    webhook = TelegramWebhook(
        router=router,
        sender=sender_mock,
        session=session,
        hitl=hitl,
        authorized_user_id=operador,
        hitl_registry=TelegramHITLMessageRegistry(),
        golden_master_coordinator=coord,
    )
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=webhook,
        gateway=MagicMock(),
        session=session,
        interval=0,
        authorized_chat_id=operador,
    )
    return coord, webhook, polling, sender_mock, router


def _update_texto(
    update_id: int,
    text: str,
    *,
    user_id: int = OPERADOR,
    chat_id: int = OPERADOR,
    chat_type: str | None = "private",
    extra_msg: dict[str, object] | None = None,
    extra_root: dict[str, object] | None = None,
) -> dict[str, object]:
    chat: dict[str, object] = {"id": chat_id}
    if chat_type is not None:
        chat["type"] = chat_type
    msg: dict[str, object] = {
        "message_id": 500 + update_id,
        "text": text,
        "chat": chat,
        "from": {"id": user_id},
    }
    if extra_msg:
        msg.update(extra_msg)
    update: dict[str, object] = {"update_id": update_id, "message": msg}
    if extra_root:
        update.update(extra_root)
    return update


def _update_callback(
    update_id: int,
    data: str,
    *,
    user_id: int = OPERADOR,
    chat_id: int = OPERADOR,
    chat_type: str | None = "private",
    message_id: int = 101,
    extra_msg: dict[str, object] | None = None,
    extra_query: dict[str, object] | None = None,
) -> dict[str, object]:
    chat: dict[str, object] = {"id": chat_id}
    if chat_type is not None:
        chat["type"] = chat_type
    msg: dict[str, object] = {"message_id": message_id, "chat": chat}
    if extra_msg:
        msg.update(extra_msg)
    query: dict[str, object] = {
        "id": f"cb-{update_id}",
        "from": {"id": user_id},
        "message": msg,
        "data": data,
    }
    if extra_query:
        query.update(extra_query)
    return {"update_id": update_id, "callback_query": query}


def _extraer_token_de_ultimo_envio(sender: MagicMock, accion: str = "approve") -> tuple[str, int, str]:
    """Extrae (callback_data, message_id, solicitud_id) del mensaje de presentación enviado."""
    import re

    assert sender.send.await_count >= 1
    ultimo_call = sender.send.await_args_list[-1]
    texto = ultimo_call.args[1]
    markup = ultimo_call.kwargs["reply_markup"]
    fila = markup["inline_keyboard"][0]
    prefijo = f"gm:{accion}:"
    callback_data = next(btn["callback_data"] for btn in fila if btn["callback_data"].startswith(prefijo))
    enviados = getattr(sender, "_mensajes_enviados", None)
    msg_id = enviados[-1].message_id if isinstance(enviados, list) and enviados else 100 + sender.send.await_count
    match = re.search(r"<code>([^<]+)</code>", texto)
    assert match is not None, texto
    solicitud_id = match.group(1)
    return callback_data, msg_id, solicitud_id


def _huellas(rig: RigStockManual) -> dict[str, str]:
    base = rig.storage.parent
    return {
        ruta: digest
        for ruta, digest in snapshot_sha256_recursivo([base]).items()
        if pathlib.Path(ruta).name != "frozen-runtime.lock"
    }


# ---------------------------------------------------------------------------
# 1. Contrato de callback data y aislamiento de espacios de nombres
# ---------------------------------------------------------------------------


class TestContratoCallbacksYNamespaces:
    def test_callback_gm_respeta_limite_64_bytes_y_parsea_acciones(self) -> None:
        for accion in ("approve", "deny", "cancel"):
            cb = build_gm_callback_data(accion, "tok-abc_123")
            assert cb.startswith(f"gm:{accion}:")
            assert len(cb.encode("utf-8")) <= 64
            assert parse_gm_callback_data(cb) == (accion, "tok-abc_123")

        assert parse_gm_callback_data("hitl:approve:tok-abc_123") is None
        assert parse_gm_callback_data("gm:escalate:tok-abc_123") is None
        with pytest.raises(ValueError):
            build_gm_callback_data("approve", "x" * 60)

    @pytest.mark.asyncio
    async def test_gm_callback_y_comandos_nunca_tocan_hitl_guard_ni_llm(self, tmp_path: pathlib.Path) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        hitl = HITLGuard()
        hitl.respond = AsyncMock()
        coord, _webhook, polling, sender, router = _construir_entorno(rig, hitl=hitl)
        try:
            await polling._process_raw_update(_update_callback(1, "gm:approve:token-inexistente"))
            await polling._process_raw_update(_update_texto(2, "/gm_desconocido"))

            hitl.respond.assert_not_awaited()
            router.chat.assert_not_awaited()
            sender.answer_callback_query.assert_awaited()
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 2. Compuerta productiva cerrada por defecto y flujo E2E autorizado
# ---------------------------------------------------------------------------


class TestEmisionCerradaYFlujoAutorizado:
    @pytest.mark.asyncio
    async def test_flujo_telegram_con_bandera_cerrada_rechaza_emision_sin_mutar(self, tmp_path: pathlib.Path) -> None:
        """AAA: con EMISION_PRODUCTIVA_HABILITADA=False de fábrica, el botón Aprobar no emite nada."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        antes = _huellas(rig)
        coord, _webhook, polling, sender, router = _construir_entorno(rig)
        try:
            # Act
            await polling._process_raw_update(_update_texto(10, "/gm_preparar"))
            await coord.esperar_idle()
            cb_data, msg_id, _ = _extraer_token_de_ultimo_envio(sender, "approve")
            await polling._process_raw_update(_update_callback(11, cb_data, message_id=msg_id))
            await coord.esperar_idle()

            # Assert
            router.chat.assert_not_awaited()
            assert not list(issuances_state_dir(rig.storage).iterdir())
            assert not transition_journal_path(rig.storage).exists()
            assert _huellas(rig) == antes
            ultimo_msg = sender.send.await_args_list[-1].args[1]
            assert "EMISION_PRODUCTIVA_HABILITADA" in ultimo_msg or "cerrada" in ultimo_msg.casefold()
            # El prompt se cerró limpiamente: se puede volver a preparar después.
            await polling._process_raw_update(_update_texto(12, "/gm_preparar"))
            await coord.esperar_idle()
            assert sender.send.await_count >= 3
        finally:
            await coord.cerrar()

    @SOLO_WINDOWS
    @pytest.mark.asyncio
    async def test_e2e_autorizado_boton_inline_ejecuta_transicion_completa(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """AAA: /gm_estado → /gm_preparar → clic botón Aprobar → witness CONSUMED e INI apuntando al Clone."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        antes = _huellas(rig)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        coord, _webhook, polling, sender, router = _construir_entorno(rig)
        try:
            # Act 1: diagnóstico inicial
            await polling._process_raw_update(_update_texto(20, "/gm_estado"))
            await coord.esperar_idle()
            assert "limpio" in sender.send.await_args_list[-1].args[1].casefold()

            # Act 2: preparar presentación en Telegram
            await polling._process_raw_update(_update_texto(21, "/gm_preparar"))
            await coord.esperar_idle()
            cb_data, msg_id, _ = _extraer_token_de_ultimo_envio(sender, "approve")

            # Act 3: clic del operador autorizado en botón inline
            await polling._process_raw_update(_update_callback(22, cb_data, message_id=msg_id))
            await coord.esperar_idle()

            # Assert: superficies y autoridad durable
            router.chat.assert_not_awaited()
            evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
            assert evidencia_ini.state is ObservationState.OBSERVED
            assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.clone_root.resolve()
            cfg = tomllib.loads(rig.config_path.read_text(encoding="utf-8"))
            assert os.path.normcase(cfg["skyrim_path"]) == os.path.normcase(str(rig.clone_root))

            autoridad = derivar_autoridad_aprobacion(rig.storage)
            assert len(autoridad.witnesses) == 1
            assert len(autoridad.tombstones) == 1
            assert cargar_ledger_aprobacion(rig.storage).ledger.state is ApprovalLifecycle.CONSUMED
            assert (
                journal_module.cargar_journal_transicion(rig.storage).disposition
                is journal_module.JournalDisposition.NONE
            )

            despues = _huellas(rig)
            for prohibida in (rig.managed_source, rig.stock_source, rig.manual_runtime, rig.storage / "versions"):
                prefijo = str(prohibida)
                for clave, valor in antes.items():
                    if clave.startswith(prefijo):
                        assert despues[clave] == valor
            assert exclusion_de_test in {"real", "doble_posix"}
        finally:
            await coord.cerrar()

    @SOLO_WINDOWS
    @pytest.mark.asyncio
    async def test_e2e_comando_texto_gm_aprobar_con_solicitud_id(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """AAA: /gm_aprobar <solicitud_id> exacto también ejecuta la transición verificada."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(30, "/gm_preparar"))
            await coord.esperar_idle()
            _cb, _msg_id, solicitud_id = _extraer_token_de_ultimo_envio(sender, "approve")

            await polling._process_raw_update(_update_texto(31, f"/gm_aprobar {solicitud_id}"))
            await coord.esperar_idle()

            evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
            assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.clone_root.resolve()
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("accion", ["deny", "cancel"])
    async def test_rechazo_y_cancelacion_no_emiten_y_permiten_nueva_preparacion(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, accion: str
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(40, "/gm_preparar"))
            await coord.esperar_idle()
            cb_data, msg_id, _ = _extraer_token_de_ultimo_envio(sender, accion)

            await polling._process_raw_update(_update_callback(41, cb_data, message_id=msg_id))
            await coord.esperar_idle()

            assert not list(issuances_state_dir(rig.storage).iterdir())
            sender.edit_message.assert_awaited()
            # Reintento limpio permitido
            await polling._process_raw_update(_update_texto(42, "/gm_preparar"))
            await coord.esperar_idle()
            assert sender.send.await_count >= 2
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 3. Pruebas negativas de autenticación, identidad, replay y sesión
# ---------------------------------------------------------------------------


class TestSeguridadIdentidadReplayYSesion:
    @pytest.mark.asyncio
    async def test_rechaza_tercero_grupo_faltante_tipo_y_reenvios(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        coord, webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(100, "/gm_preparar"))
            await coord.esperar_idle()
            cb_approve, msg_id, solicitud_id = _extraer_token_de_ultimo_envio(sender, "approve")

            updates_maliciosos = [
                # Tercero en texto y en callback
                _update_texto(101, f"/gm_aprobar {solicitud_id}", user_id=OTRO_USUARIO, chat_id=OTRO_USUARIO),
                _update_callback(102, cb_approve, user_id=OTRO_USUARIO, chat_id=OTRO_USUARIO, message_id=msg_id),
                # Grupo / chat != from / sin chat.type=private
                _update_texto(103, f"/gm_aprobar {solicitud_id}", user_id=OPERADOR, chat_id=-100123, chat_type="group"),
                _update_callback(
                    104, cb_approve, user_id=OPERADOR, chat_id=OPERADOR, chat_type="group", message_id=msg_id
                ),
                _update_callback(
                    105, cb_approve, user_id=OPERADOR, chat_id=OPERADOR, chat_type=None, message_id=msg_id
                ),
                # Reenvíos clásicos y modernos Bot API 7+ / respuesta a reenviado / canal
                _update_texto(106, f"/gm_aprobar {solicitud_id}", extra_msg={"forward_date": 1700000000}),
                _update_texto(107, f"/gm_aprobar {solicitud_id}", extra_msg={"forward_origin": {"type": "user"}}),
                _update_callback(
                    108,
                    cb_approve,
                    message_id=msg_id,
                    extra_msg={"forward_origin": {"type": "user"}},
                ),
                _update_callback(
                    109,
                    cb_approve,
                    message_id=msg_id,
                    extra_msg={"reply_to_message": {"forward_from": {"id": OTRO_USUARIO}}},
                ),
                _update_texto(110, f"/gm_aprobar {solicitud_id}", extra_msg={"sender_chat": {"id": -1009}}),
                # Bool spoofing como entero
                _update_texto(111, "/gm_preparar", user_id=True, chat_id=True),  # type: ignore[arg-type]
                # Llamada directa saltando polling
                _update_callback(112, cb_approve, user_id=OTRO_USUARIO, chat_id=OPERADOR, message_id=msg_id),
            ]
            for upd in updates_maliciosos:
                await webhook.process_update(upd)
                await coord.esperar_idle()

            assert not list(issuances_state_dir(rig.storage).iterdir())
            # La sesión legítima sigue abierta tras los rechazos (ningún tercero la rompió)
            # Rechaza además argumentos inyectados en /gm_preparar o ID incorrecto en /gm_aprobar
            await polling._process_raw_update(_update_texto(113, "/gm_preparar C:\\ruta\\inyectada"))
            await polling._process_raw_update(_update_texto(114, "/gm_aprobar solicitud-falsa"))
            await polling._process_raw_update(_update_texto(115, "/gm_aprobar"))
            await coord.esperar_idle()
            assert not list(issuances_state_dir(rig.storage).iterdir())
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_rechaza_doble_clic_y_replay_con_worker_bloqueado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        barrera_entrada = threading.Event()
        liberar_worker = threading.Event()
        invocaciones = {"conteo": 0}
        original = SesionTransicionGoldenMaster.decidir_y_emitir

        def _decidir_lento(self, *, solicitud, decision, identidad):
            invocaciones["conteo"] += 1
            barrera_entrada.set()
            assert liberar_worker.wait(timeout=15.0)
            return original(self, solicitud=solicitud, decision=decision, identidad=identidad)

        monkeypatch.setattr(SesionTransicionGoldenMaster, "decidir_y_emitir", _decidir_lento)

        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(200, "/gm_preparar"))
            await coord.esperar_idle()
            cb_approve, msg_id, _ = _extraer_token_de_ultimo_envio(sender, "approve")

            # Primer clic entra al worker y queda bloqueado antes de terminar
            await polling._process_raw_update(_update_callback(201, cb_approve, message_id=msg_id))
            assert await asyncio.to_thread(barrera_entrada.wait, 15.0)

            # Segundo clic (doble clic con update_id nuevo) + /gm_preparar concurrente deben rechazarse al instante
            await polling._process_raw_update(_update_callback(202, cb_approve, message_id=msg_id))
            await polling._process_raw_update(_update_texto(203, "/gm_preparar"))

            liberar_worker.set()
            await coord.esperar_idle()

            assert invocaciones["conteo"] == 1
            # Replay posterior de un token ya resuelto/consumido
            await polling._process_raw_update(_update_callback(204, cb_approve, message_id=msg_id))
            await coord.esperar_idle()
            assert invocaciones["conteo"] == 1
        finally:
            liberar_worker.set()
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_sesion_expirada_tras_reinicio_y_token_de_sesion_anterior_no_afectan_la_nueva(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)

        coord_viejo, _w1, polling_viejo, sender_viejo, _ = _construir_entorno(rig)
        await polling_viejo._process_raw_update(_update_texto(300, "/gm_preparar"))
        await coord_viejo.esperar_idle()
        cb_viejo, msg_viejo, id_viejo = _extraer_token_de_ultimo_envio(sender_viejo, "approve")
        await coord_viejo.cerrar()

        # Nuevo proceso / instancia limpia: el botón del proceso muerto NO puede aprobar ni mutar
        coord_nuevo, _w2, polling_nuevo, sender_nuevo, _ = _construir_entorno(rig)
        try:
            await polling_nuevo._process_raw_update(_update_callback(301, cb_viejo, message_id=msg_viejo))
            await polling_nuevo._process_raw_update(_update_texto(302, f"/gm_aprobar {id_viejo}"))
            await coord_nuevo.esperar_idle()
            assert not list(issuances_state_dir(rig.storage).iterdir())

            # Ahora preparamos una sesión nueva B; reenviar el token de A NO rompe ni aprueba B
            await polling_nuevo._process_raw_update(_update_texto(303, "/gm_preparar"))
            await coord_nuevo.esperar_idle()
            await polling_nuevo._process_raw_update(_update_callback(304, cb_viejo, message_id=msg_viejo))
            await polling_nuevo._process_raw_update(_update_texto(305, f"/gm_aprobar {id_viejo}"))
            await coord_nuevo.esperar_idle()
            assert not list(issuances_state_dir(rig.storage).iterdir())
            assert coord_nuevo.tiene_solicitud_abierta is True
        finally:
            await coord_nuevo.cerrar()

    @pytest.mark.asyncio
    async def test_fail_closed_con_operador_none(self, tmp_path: pathlib.Path) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        coord, webhook, _polling, sender, _router = _construir_entorno(rig, operador=None)
        try:
            await webhook.process_update(_update_texto(350, "/gm_estado"))
            await coord.esperar_idle()
            sender.send.assert_not_awaited()
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 4. Event loop libre, cancelación con worker vivo, desconexión y shutdown
# ---------------------------------------------------------------------------


class TestConcurrenciaCancelacionYDesconexion:
    @pytest.mark.asyncio
    async def test_no_bloquea_el_event_loop_durante_worker_y_resiste_cancelacion_repetida(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: aunque preparar tarde bloqueado en un hilo, el event loop late y cancel espera terminalidad."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        adentro = threading.Event()
        soltar = threading.Event()
        salio = threading.Event()
        original_preparar = SesionTransicionGoldenMaster.preparar

        def _preparar_lento(self, *, identidad, entregar, transition_id=None):
            adentro.set()
            try:
                assert soltar.wait(timeout=15.0)
                return original_preparar(self, identidad=identidad, entregar=entregar, transition_id=transition_id)
            finally:
                salio.set()

        monkeypatch.setattr(SesionTransicionGoldenMaster, "preparar", _preparar_lento)
        coord, _webhook, _polling, _sender, _router = _construir_entorno(rig)
        try:
            tarea = asyncio.create_task(coord.procesar_actualizacion(_update_texto(400, "/gm_preparar")))
            assert await asyncio.to_thread(adentro.wait, 15.0)

            # Verificar que el event loop NO está bloqueado mientras el worker corre
            latidos = 0
            for _ in range(5):
                await asyncio.sleep(0.01)
                latidos += 1
            assert latidos == 5

            # Cancelación repetida mientras el worker sigue vivo
            tarea.cancel()
            await asyncio.sleep(0.02)
            assert not tarea.done(), "la coroutine no debe declararse terminada con el worker aún vivo"
            assert not salio.is_set()
            tarea.cancel()
            await asyncio.sleep(0.02)
            assert not tarea.done()

            soltar.set()
            with pytest.raises(asyncio.CancelledError):
                await tarea

            assert salio.is_set()
            assert coord.tiene_solicitud_abierta is False
        finally:
            soltar.set()
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_desconexion_durante_entrega_de_presentacion_cancela_sin_dejar_huerfanos(
        self, tmp_path: pathlib.Path
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        sender = _crear_sender_simulado()
        fallos = {"primero": True}

        async def _send_con_corte(chat_id: int, text: str, reply_markup: dict | None = None, **_kw: object):
            if fallos["primero"] and reply_markup is not None:
                fallos["primero"] = False
                raise TelegramSendError("network drop 502")
            return TelegramMessage(chat_id=chat_id, message_id=701)

        sender.send = AsyncMock(side_effect=_send_con_corte)
        coord, _webhook, polling, _sender, _router = _construir_entorno(rig, sender=sender)
        try:
            await polling._process_raw_update(_update_texto(420, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is False

            # Tras reconexión, otro /gm_preparar debe funcionar sin estado pegado
            await polling._process_raw_update(_update_texto(421, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is True
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_cierre_desbloquea_entrega_colgada_sin_deadlock(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        sender = _crear_sender_simulado()
        envio_iniciado = asyncio.Event()

        async def _send_eterno(chat_id: int, text: str, reply_markup: dict | None = None, **_kw: object):
            assert reply_markup is not None, f"se esperaba entrega de presentación interactiva, no {text!r}"
            envio_iniciado.set()
            await asyncio.Future()

        sender.send = AsyncMock(side_effect=_send_eterno)
        coord, _webhook, polling, _sender, _router = _construir_entorno(rig, sender=sender)
        try:
            await polling._process_raw_update(_update_texto(440, "/gm_preparar"))
            # RigStockManual + hashing + preflight pueden exceder 3s bajo carga Windows/CI antes de enviar
            await asyncio.wait_for(envio_iniciado.wait(), timeout=15.0)

            # cerrar() cancela la entrega en vuelo y termina rápido sin deadlock ni esperar delivery_timeout (20s)
            await asyncio.wait_for(coord.cerrar(), timeout=3.0)
            assert coord.tiene_solicitud_abierta is False
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_desconexion_post_decision_en_envio_final_libera_single_flight(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: si Telegram cae al reportar o editar tras decidir, el coordinador libera el estado single-flight."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sender = _crear_sender_simulado()
        coord, _webhook, polling, _sender, _router = _construir_entorno(rig, sender=sender)
        try:
            await polling._process_raw_update(_update_texto(450, "/gm_preparar"))
            await coord.esperar_idle()
            cb_deny, msg_id, _ = _extraer_token_de_ultimo_envio(sender, "deny")

            sender.edit_message = AsyncMock(side_effect=TelegramSendError("edit drop"))
            sender.send = AsyncMock(side_effect=TelegramSendError("send drop"))
            await polling._process_raw_update(_update_callback(451, cb_deny, message_id=msg_id))
            await coord.esperar_idle()

            assert coord.tiene_solicitud_abierta is False
            sender.send = AsyncMock(return_value=TelegramMessage(chat_id=OPERADOR, message_id=777))
            await polling._process_raw_update(_update_texto(452, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is True
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 5. Config fail-closed, hot-reload, crash, reanudación y abandono autorizado
# ---------------------------------------------------------------------------


def _simular_crash_pre_pending(rig: RigStockManual, monkeypatch: pytest.MonkeyPatch) -> str:
    """Emite, quema aprobación e inyecta fallo antes de publicar PENDING; devuelve transition_id reanudable."""
    monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
    sesion_prev = SesionTransicionGoldenMaster(**_objetivo_desde_rig(rig).to_sesion_kwargs())
    identidad = verificar_identidad_telegram(
        {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}},
        operador_configurado=OPERADOR,
    )
    solicitud = sesion_prev.preparar(identidad=identidad, entregar=lambda _p: None)
    consentimiento = sesion_prev.decidir_y_emitir(
        solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=identidad
    )
    assert consentimiento is not None
    original = recovery_module.capturar_registro

    def _fallar_antes_de_pending(*_args: object, **_kwargs: object) -> object:
        raise recovery_module.RecuperacionError("crash simulado entre CONSUME y PENDING")

    monkeypatch.setattr(recovery_module, "capturar_registro", _fallar_antes_de_pending)
    try:
        with pytest.raises(Exception):  # noqa: B017 — el tipo de fallo pre-PENDING lo envuelve el binder
            sesion_prev.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
    finally:
        monkeypatch.setattr(recovery_module, "capturar_registro", original)

    diag = diagnosticar_estado_durable(rig.storage)
    assert diag.estado is EstadoDurable.REANUDABLE
    assert diag.transicion_reanudable is not None
    return diag.transicion_reanudable


class TestReanudacionCrashYAbandono:
    @SOLO_WINDOWS
    @pytest.mark.asyncio
    async def test_crash_pre_pending_diagnostico_y_reanudacion_por_telegram(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """AAA: tras emitir y consumir sin publicar PENDING, un coordinador nuevo diagnostica REANUDABLE y reanuda."""
        # Arrange: simular aprobación emitida y consumida sin PENDING publicado
        rig = RigStockManual(tmp_path, steam_activo=True)
        tx_id = _simular_crash_pre_pending(rig, monkeypatch)

        # Act: nuevo proceso Telegram consulta /gm_estado y luego ejecuta /gm_reanudar
        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(500, "/gm_estado"))
            await coord.esperar_idle()
            texto_diag = sender.send.await_args_list[-1].args[1]
            assert "reanudable" in texto_diag.casefold()
            assert tx_id in texto_diag

            # Un tercero NO puede reanudar
            await polling._process_raw_update(
                _update_texto(501, f"/gm_reanudar {tx_id}", user_id=OTRO_USUARIO, chat_id=OTRO_USUARIO)
            )
            await coord.esperar_idle()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.REANUDABLE

            # Operador autorizado sí reanuda
            await polling._process_raw_update(_update_texto(502, f"/gm_reanudar {tx_id}"))
            await coord.esperar_idle()

            evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
            assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.clone_root.resolve()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO
            assert exclusion_de_test in {"real", "doble_posix"}
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_abandono_autorizado_y_rechazo_post_pending(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        tx_id = _simular_crash_pre_pending(rig, monkeypatch)

        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            # Exige motivo explícito
            await polling._process_raw_update(_update_texto(600, f"/gm_abandonar {tx_id}"))
            await coord.esperar_idle()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.REANUDABLE

            # Abandona con motivo y autoría verificada
            await polling._process_raw_update(_update_texto(601, f"/gm_abandonar {tx_id} cancelar ventana previa"))
            await coord.esperar_idle()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

            # Post-PENDING: /gm_reanudar falla cerrado y nunca re-emite
            jp = transition_journal_path(rig.storage)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(
                '{"schema_version": 2, "state": "PENDING_PROMOTION", "journal_revision": 1}', encoding="utf-8"
            )
            await polling._process_raw_update(_update_texto(602, f"/gm_reanudar {tx_id}"))
            await coord.esperar_idle()
            assert (
                "dueño" in sender.send.await_args_list[-1].args[1].casefold()
                or "pending" in sender.send.await_args_list[-1].args[1].casefold()
            )
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_resolver_objetivo_desde_config_y_rechazo_sin_configurar(self, tmp_path: pathlib.Path) -> None:
        """AAA: TOML incompleto o policy inválida devuelven None (fail-closed) y /gm_preparar no muta nada."""
        incompleto = tmp_path / "incompleto.toml"
        incompleto.write_text('[golden_master]\nstorage_root = "X:\\\\FR"\n', encoding="utf-8")
        assert resolver_objetivo_golden_master_desde_config(Config(incompleto), config_path=incompleto) is None

        politica_invalida = tmp_path / "politica_mala.toml"
        politica_invalida.write_text(
            '[golden_master]\nstorage_root = "X:\\\\FR"\nmo2_instance_dir = "X:\\\\MO2"\nclone_id = "c1"\ntransition_policy = "INVENTADA"\n',
            encoding="utf-8",
        )
        assert (
            resolver_objetivo_golden_master_desde_config(Config(politica_invalida), config_path=politica_invalida)
            is None
        )

        sender = _crear_sender_simulado()
        coord = CoordinadorTelegramGoldenMaster(sender=sender, operador_autorizado=OPERADOR, objetivo=None)
        webhook = TelegramWebhook(
            router=MagicMock(),
            sender=sender,
            session=MagicMock(spec=aiohttp.ClientSession),
            hitl=HITLGuard(),
            authorized_user_id=OPERADOR,
            hitl_registry=TelegramHITLMessageRegistry(),
            golden_master_coordinator=coord,
        )
        try:
            await webhook.process_update(_update_texto(610, "/gm_preparar"))
            await webhook.process_update(_update_callback(611, "gm:approve:token-x"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is False
            assert "no configurado" in sender.send.await_args_list[-1].args[1].casefold()
            sender.answer_callback_query.assert_awaited()
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_hot_reload_invalida_solicitud_abierta_y_limpia_operador_y_objetivo(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AAA: asegurar_coordinador_golden_master ante hot-reload descarta prompts y aplica eliminación de operador/config."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        cfg_file = rig.config_path

        def _esc(p: pathlib.Path) -> str:
            return str(p).replace("\\", "\\\\")

        base_toml = cfg_file.read_text(encoding="utf-8")
        cfg_file.write_text(
            base_toml
            + "\n[golden_master]\n"
            + f'storage_root = "{_esc(rig.storage)}"\n'
            + f'mo2_instance_dir = "{_esc(rig.mo2_active_global)}"\n'
            + f'clone_id = "{rig.clone_id}"\n'
            + 'transition_policy = "stock_independent_transition"\n'
            + f'managed_source_root = "{_esc(rig.managed_source)}"\n'
            + f'outgoing_manual_runtime_root = "{_esc(rig.manual_runtime)}"\n'
            + f'stock_source_root = "{_esc(rig.stock_source)}"\n'
            + f'approved_stock_reference_digest_file = "{_esc(rig.approved_digest_file)}"\n',
            encoding="utf-8",
        )
        ctx = SimpleNamespace(config_path=cfg_file, golden_master_coordinator=None, _golden_master_objetivo=None)
        sender_a = _crear_sender_simulado()
        coord = await asegurar_coordinador_golden_master(
            ctx,
            sender=sender_a,
            authorized_user_id=OPERADOR,
            local_cfg=Config(cfg_file),
        )
        assert ctx._golden_master_objetivo is not None
        assert ctx._golden_master_objetivo.storage_root == rig.storage
        assert ctx._golden_master_objetivo.clone_id == rig.clone_id
        # Inyectar entorno y probes sintéticos del rig aislado sobre el objetivo ya parseado desde Config
        coord._objetivo = dataclasses.replace(
            ctx._golden_master_objetivo,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )
        try:
            # Simular solicitud abierta previa en sesión (el reload debe invalidarla)
            webhook = TelegramWebhook(
                router=MagicMock(),
                sender=sender_a,
                session=MagicMock(spec=aiohttp.ClientSession),
                hitl=HITLGuard(),
                authorized_user_id=OPERADOR,
                hitl_registry=TelegramHITLMessageRegistry(),
                golden_master_coordinator=coord,
            )
            await webhook.process_update(_update_texto(620, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is True
            cb_viejo, msg_viejo, _ = _extraer_token_de_ultimo_envio(sender_a, "approve")

            # Hot-reload con operador revocado y sección [golden_master] eliminada
            cfg_file.write_text("# sin configuracion golden master\n", encoding="utf-8")
            sender_b = _crear_sender_simulado()
            reloaded = await asegurar_coordinador_golden_master(
                ctx,
                sender=sender_b,
                authorized_user_id=None,
                local_cfg=Config(cfg_file),
            )
            assert reloaded is coord
            assert ctx._golden_master_objetivo is None
            assert coord._objetivo is None
            assert coord.tiene_solicitud_abierta is False
            sender_a.edit_message.assert_awaited()

            # Tras el reload sin operador/config, el callback viejo rechaza cerrado
            await coord.iniciar_callback(_update_callback(621, cb_viejo, message_id=msg_viejo))
            sender_b.answer_callback_query.assert_awaited()
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 6. Fronteras AST y simetría de los tres llamadores de producción
# ---------------------------------------------------------------------------


class TestFronterasArquitectonicasYSimetria:
    def test_llm_gui_tools_y_hitl_guard_no_importan_adaptador_golden_master(self) -> None:
        prohibidos = {
            "sky_claw.app.comms.golden_master_hitl",
            "sky_claw.local.frozen_runtime.owner_consent",
            "sky_claw.local.frozen_runtime.autorizacion_hitl_transicion",
            "sky_claw.local.frozen_runtime.approval",
        }
        carpetas_aisladas = [
            RAIZ_REPO / "sky_claw" / "app" / "agent",
            RAIZ_REPO / "sky_claw" / "app" / "gui",
            RAIZ_REPO / "sky_claw" / "app" / "orchestrator",
            RAIZ_REPO / "sky_claw" / "local" / "tools",
            RAIZ_REPO / "sky_claw" / "app" / "security" / "hitl.py",
        ]
        ofensas: list[str] = []
        for destino in carpetas_aisladas:
            archivos = [destino] if destino.is_file() else list(destino.rglob("*.py"))
            for ruta in archivos:
                arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
                for nodo in ast.walk(arbol):
                    if isinstance(nodo, ast.Import):
                        for alias in nodo.names:
                            if alias.name in prohibidos:
                                ofensas.append(f"{ruta}: import {alias.name}")
                    elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                        for proh in prohibidos:
                            if nodo.module == proh or nodo.module.startswith(f"{proh}."):
                                ofensas.append(f"{ruta}: from {nodo.module}")
        assert ofensas == []

    def test_tres_llamadores_de_telegram_webhook_cablean_golden_master_coordinator(self) -> None:
        """Ancla enumerativa hermano contra hermano: todos los constructores productivos de TelegramWebhook."""
        sitios_esperados = {
            "sky_claw/app_context.py",
            "sky_claw/app/modes/telegram_mode.py",
            "sky_claw/app/comms/frontend_bridge.py",
        }
        encontrados: dict[str, list[bool]] = {}
        for ruta in (RAIZ_REPO / "sky_claw").rglob("*.py"):
            rel = ruta.relative_to(RAIZ_REPO).as_posix()
            arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "TelegramWebhook":
                    tiene_kw = any(kw.arg == "golden_master_coordinator" for kw in nodo.keywords)
                    encontrados.setdefault(rel, []).append(tiene_kw)

        assert set(encontrados) == sitios_esperados
        for rel, flags in encontrados.items():
            assert all(flags), f"{rel} instancia TelegramWebhook sin golden_master_coordinator"
        assert approval_module.EMISION_PRODUCTIVA_HABILITADA is False
        assert hitl_wiring.TRANSPORT_ADAPTER == "WIRED_BUT_GATED"

    def test_llamadores_productivos_usan_asegurar_y_cierran_coordinador(self) -> None:
        for rel in (
            "sky_claw/app_context.py",
            "sky_claw/app/modes/telegram_mode.py",
            "sky_claw/app/comms/frontend_bridge.py",
        ):
            codigo = (RAIZ_REPO / rel).read_text(encoding="utf-8")
            assert "asegurar_coordinador_golden_master" in codigo
        assert ".cerrar()" in (RAIZ_REPO / "sky_claw" / "app_context.py").read_text(encoding="utf-8")
        assert ".cerrar()" in (RAIZ_REPO / "sky_claw" / "app" / "modes" / "telegram_mode.py").read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 7. Presentación completa, truncamiento fail-closed, transporte y cancelación (H-01)
# ---------------------------------------------------------------------------


def _presentacion_prueba(
    *,
    solicitud_id: str = "sol-h01",
    approval_seq: int = 1,
    digest: str = "digest-critico-al-final-de-presentacion",
    texto: str | None = None,
) -> owner_consent_module.PresentacionConsentimiento:
    cuerpo = texto if texto is not None else f"operation: INITIAL_MIGRATION\napproval_scope_digest: {digest}\n"
    return owner_consent_module.PresentacionConsentimiento(
        solicitud_id=solicitud_id,
        root_id="root-test",
        superficie=owner_consent_module.SuperficieConsentimiento.TELEGRAM,
        sesion="telegram:424242:424242",
        propietario="424242",
        mecanismo="telegram_private_chat",
        approval_id="apr-test",
        approval_seq=approval_seq,
        approval_scope_digest=digest,
        approval_provenance="hitl_telegram_private_chat",
        scope_payload={"operation": "INITIAL_MIGRATION"},
        ledger_revision_at_proposal=0,
        presentada_en_ns=1_000_000,
        texto=cuerpo,
    )


class TestPresentacionCompletaYEntregaFailClosed:
    @pytest.mark.asyncio
    async def test_presentacion_normal_incluye_todos_los_campos_y_digest(self, tmp_path: pathlib.Path) -> None:
        """AAA: una presentación normal se entrega íntegra en un solo mensaje con todos los campos y el digest."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        coord, _webhook, polling, sender, router = _construir_entorno(rig)
        try:
            # Act
            await polling._process_raw_update(_update_texto(700, "/gm_preparar"))
            await coord.esperar_idle()

            # Assert
            router.chat.assert_not_awaited()
            assert coord.tiene_solicitud_abierta is True
            assert coord._sesion_activa is not None
            pendientes = coord._sesion_activa.registro.pendientes()
            assert len(pendientes) == 1
            pres_original = pendientes[0]

            assert sender.send.await_count == 1
            call = sender.send.await_args_list[-1]
            texto_enviado = call.args[1]
            assert len(texto_enviado) <= _TELEGRAM_MAX_CHAR_RESUMEN
            assert "...</pre>" not in texto_enviado
            assert pres_original.solicitud_id in texto_enviado
            assert f"<code>{pres_original.approval_seq}</code>" in texto_enviado
            assert pres_original.approval_scope_digest in texto_enviado
            for linea in pres_original.texto.splitlines():
                if linea.strip():
                    assert html.escape(linea, quote=True) in texto_enviado
            assert "/gm_aprobar" in texto_enviado
            assert call.kwargs.get("reply_markup") is not None
        finally:
            await coord.cerrar()

    def test_formateador_presentacion_larga_rechaza_fail_closed_sin_truncar(self) -> None:
        """AAA (H-01 unit): superar el límite máximo rechaza fail-closed en lugar de truncar a 2.400 chars."""
        # Arrange
        digest = "digest-critico-al-final-de-presentacion"
        pres_larga = _presentacion_prueba(
            solicitud_id="sol-h01-larga",
            digest=digest,
            texto=("campo_largo: " + ("A" * 3700) + f"\napproval_scope_digest: {digest}\n"),
        )

        # Act & Assert
        with pytest.raises(EntregaPresentacionError):
            _formatear_mensaje_presentacion_desde_pres(pres_larga)

    @pytest.mark.asyncio
    async def test_presentacion_larga_no_trunca_ni_permite_aprobar_sin_digest_completo(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA (H-01 e2e): exceso de longitud falla cerrado, sin botones ni solicitud aprobable sin digest."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        original_texto = owner_consent_module._texto_de_presentacion

        def _texto_extendido(scope: owner_consent_module.ApprovalScope) -> str:
            base = original_texto(scope)
            bloque_largo = "\n".join(f"campo_scope_extra_{idx:02d}: " + ("A" * 120) for idx in range(25))
            prefijo, sep, cola = base.partition("approval_scope_digest:")
            if sep:
                return f"{prefijo}{bloque_largo}\n{sep}{cola}"
            return f"{base}\n{bloque_largo}"

        monkeypatch.setattr(owner_consent_module, "_texto_de_presentacion", _texto_extendido)

        # Act: intento completo por Telegram con /gm_preparar
        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(701, "/gm_preparar"))
            await coord.esperar_idle()

            # Assert fail-closed: nunca se enviaron botones aprobables ni quedó solicitud/autorización abierta
            assert coord.tiene_solicitud_abierta is False
            assert coord._sesion_activa is None
            assert coord._operacion_en_curso is False
            assert not list(issuances_state_dir(rig.storage).iterdir())
            autoridad = derivar_autoridad_aprobacion(rig.storage)
            assert autoridad.witnesses == ()
            assert autoridad.tombstones == ()

            llamadas_con_botones = [
                call for call in sender.send.await_args_list if call.kwargs.get("reply_markup") is not None
            ]
            assert llamadas_con_botones == []
            assert sender.send.await_count >= 1

            # Intento de aprobación posterior no muta el INI ni emite witness
            await polling._process_raw_update(_update_texto(702, "/gm_aprobar sol-inexistente"))
            await coord.esperar_idle()
            assert not list(issuances_state_dir(rig.storage).iterdir())
            evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
            assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.manual_runtime.resolve()

            # Recuperabilidad del coordinador: restaurando longitud normal, /gm_preparar vuelve a operar
            monkeypatch.setattr(owner_consent_module, "_texto_de_presentacion", original_texto)
            await polling._process_raw_update(_update_texto(703, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is True
        finally:
            await coord.cerrar()

    def test_presentacion_incompleta_sin_digest_o_campos_vacios_rechaza_fail_closed(self) -> None:
        """AAA: si faltan solicitud_id, texto o approval_scope_digest en el cuerpo, falla cerrado."""
        # Arrange
        base = _presentacion_prueba(solicitud_id="sol-ok", digest="abc123digest")
        incompletas = [
            dataclasses.replace(base, solicitud_id="   "),
            dataclasses.replace(base, texto="   "),
            dataclasses.replace(base, approval_scope_digest=""),
            dataclasses.replace(base, texto="operation: INITIAL_MIGRATION\n# digest ausente\n"),
        ]

        # Act & Assert
        for pres in incompletas:
            with pytest.raises(EntregaPresentacionError):
                _formatear_mensaje_presentacion_desde_pres(pres)

    @pytest.mark.parametrize(
        "efecto_envio",
        [
            TelegramSendError("caída de red Telegram"),
            TelegramRateLimitError("rate limit 429", retry_after=5.0),
            TelegramUploadTimeoutError("timeout subiendo presentación"),
            None,
        ],
    )
    @pytest.mark.asyncio
    async def test_error_transporte_y_retorno_invalido_en_entrega_no_dejan_solicitud_ni_autorizacion(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        efecto_envio: object,
    ) -> None:
        """AAA: cualquier fallo de entrega (o respuesta inválida) cancela la solicitud y deja cero huérfanos."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        sender = _crear_sender_simulado()

        async def _fallar_primero(
            chat_id: int, text: str, reply_markup: dict | None = None, **_kwargs: object
        ) -> object:
            if reply_markup is not None:
                if isinstance(efecto_envio, BaseException):
                    raise efecto_envio
                return efecto_envio
            return TelegramMessage(chat_id=chat_id, message_id=900)

        sender.send = AsyncMock(side_effect=_fallar_primero)
        coord, _webhook, polling, _sender, _router = _construir_entorno(rig, sender=sender)
        try:
            # Act
            await polling._process_raw_update(_update_texto(710, "/gm_preparar"))
            await coord.esperar_idle()

            # Assert: ninguna solicitud ni autorización queda abierta/huérfana
            assert coord.tiene_solicitud_abierta is False
            assert coord._sesion_activa is None
            assert coord._operacion_en_curso is False
            assert not list(issuances_state_dir(rig.storage).iterdir())
            autoridad = derivar_autoridad_aprobacion(rig.storage)
            assert autoridad.witnesses == ()
            assert autoridad.tombstones == ()

            # Recuperabilidad tras restablecer el transporte
            restaurado = _crear_sender_simulado()
            sender.send = restaurado.send
            await polling._process_raw_update(_update_texto(711, "/gm_preparar"))
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is True
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_cancelacion_en_entrega_no_deja_solicitud_abierta_ni_autorizacion(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: CancelledError y cierre del coordinador en plena entrega cancelan fail-closed sin huérfanos."""
        # Arrange (caso A: coroutine de envío cancelada)
        rig_a = RigStockManual(tmp_path / "caso_a", steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        sender_a = _crear_sender_simulado()

        async def _cancelar_envio(
            chat_id: int, text: str, reply_markup: dict | None = None, **_kwargs: object
        ) -> TelegramMessage:
            if reply_markup is not None:
                raise asyncio.CancelledError()
            return TelegramMessage(chat_id=chat_id, message_id=910)

        sender_a.send = AsyncMock(side_effect=_cancelar_envio)
        coord_a, _w_a, polling_a, _s_a, _r_a = _construir_entorno(rig_a, sender=sender_a)
        try:
            # Act A
            await polling_a._process_raw_update(_update_texto(720, "/gm_preparar"))
            await coord_a.esperar_idle()

            # Assert A
            assert coord_a.tiene_solicitud_abierta is False
            assert coord_a._sesion_activa is None
            assert coord_a._operacion_en_curso is False
            assert not list(issuances_state_dir(rig_a.storage).iterdir())
            assert derivar_autoridad_aprobacion(rig_a.storage).witnesses == ()
        finally:
            await coord_a.cerrar()

        # Arrange (caso B: envío colgado interrumpido por cerrar())
        rig_b = RigStockManual(tmp_path / "caso_b", steam_activo=True)
        sender_b = _crear_sender_simulado()
        entrega_iniciada = asyncio.Event()
        entrega_colgada = asyncio.Event()

        async def _envio_lento(
            chat_id: int, text: str, reply_markup: dict | None = None, **_kwargs: object
        ) -> TelegramMessage:
            if reply_markup is not None:
                entrega_iniciada.set()
                await entrega_colgada.wait()
            return TelegramMessage(chat_id=chat_id, message_id=920)

        sender_b.send = AsyncMock(side_effect=_envio_lento)
        coord_b, _w_b, polling_b, _s_b, _r_b = _construir_entorno(rig_b, sender=sender_b)
        try:
            # Act B: lanzar preparación y cerrar mientras _entregar espera en el event loop
            await polling_b._process_raw_update(_update_texto(721, "/gm_preparar"))
            await asyncio.wait_for(entrega_iniciada.wait(), timeout=5.0)
            await coord_b.cerrar()

            # Assert B: nada abierto ni huérfano tras cancelación por shutdown
            assert coord_b.tiene_solicitud_abierta is False
            assert coord_b._sesion_activa is None
            assert not list(issuances_state_dir(rig_b.storage).iterdir())
            assert derivar_autoridad_aprobacion(rig_b.storage).witnesses == ()
        finally:
            await coord_b.cerrar()


# ---------------------------------------------------------------------------
# 8. Alcance de EMISION_PRODUCTIVA_HABILITADA = False sobre recuperación (H-04)
# ---------------------------------------------------------------------------


class TestAlcanceBanderaProductivaYRecuperacion:
    @SOLO_WINDOWS
    @pytest.mark.asyncio
    async def test_reanudar_pre_pending_opera_con_bandera_false_pero_nueva_emision_sigue_bloqueada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """AAA (H-04): con bandera en False, /gm_reanudar pre-PENDING completa sobre witness ya consumido."""
        # Arrange: generar crash pre-PENDING histórico y cerrar explícitamente EMISION_PRODUCTIVA_HABILITADA = False
        rig = RigStockManual(tmp_path, steam_activo=True)
        tx_id = _simular_crash_pre_pending(rig, monkeypatch)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", False)
        assert approval_module.EMISION_PRODUCTIVA_HABILITADA is False

        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            # Act: diagnosticar y reanudar con bandera productiva en False
            await polling._process_raw_update(_update_texto(800, "/gm_estado"))
            await coord.esperar_idle()
            assert "reanudable" in sender.send.await_args_list[-1].args[1].casefold()

            await polling._process_raw_update(_update_texto(801, f"/gm_reanudar {tx_id}"))
            await coord.esperar_idle()

            # Assert: reanudación completó el switch usando la aprobación ya consumida
            evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
            assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.clone_root.resolve()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO
            assert exclusion_de_test in {"real", "doble_posix"}
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_abandono_reconciliacion_revocacion_y_recovery_operan_con_bandera_false(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA (H-04): bandera False solo bloquea nuevas emisiones; abandono, recovery, reconciliación y revocación operan."""
        # Arrange: crash pre-PENDING y retorno explícito a EMISION_PRODUCTIVA_HABILITADA = False
        rig = RigStockManual(tmp_path, steam_activo=True)
        tx_id = _simular_crash_pre_pending(rig, monkeypatch)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", False)
        assert approval_module.EMISION_PRODUCTIVA_HABILITADA is False

        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            # Act 1: abandono autorizado por Telegram con bandera en False
            await polling._process_raw_update(_update_texto(810, f"/gm_abandonar {tx_id} rollback operativo"))
            await coord.esperar_idle()
            assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

            # Act 2: emisión previa no consumida puede revocarse con bandera en False
            rig_rev = RigStockManual(tmp_path / "rev_no_consumida", steam_activo=True)
            monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
            sesion_rev = SesionTransicionGoldenMaster(**_objetivo_desde_rig(rig_rev).to_sesion_kwargs())
            identidad_rev = verificar_identidad_telegram(
                {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}},
                operador_configurado=OPERADOR,
            )
            sol_rev = sesion_rev.preparar(identidad=identidad_rev, entregar=lambda _p: None)
            cons_rev = sesion_rev.decidir_y_emitir(
                solicitud=sol_rev, decision=DecisionConsentimiento.APROBAR, identidad=identidad_rev
            )
            assert cons_rev is not None
            monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", False)
            witness_rev = derivar_autoridad_aprobacion(rig_rev.storage).witnesses[0]
            approval_module.revocar_aprobacion(
                rig_rev.storage,
                approval_id=witness_rev.approval_id,
                approval_seq=witness_rev.approval_seq,
                approval_scope_digest=witness_rev.approval_scope_digest,
                motivo="revocación con bandera cerrada",
            )
            assert len(derivar_autoridad_aprobacion(rig_rev.storage).tombstones) == 1

            # Act 3: primitivas durables de recovery/reconciliación operan con bandera en False
            rec_recovery = recovery_module.recuperar_transicion_interrumpida(rig.storage)
            assert rec_recovery.estado is recovery_module.EstadoRecuperacion.SIN_REGISTRO
            rec_journal = journal_module.reconciliar_journal(rig.storage)
            assert rec_journal.veredicto is not None
            rec_aprob = approval_module.reconciliar_aprobacion(rig.storage)
            assert rec_aprob.veredicto is not None

            # Act 4: emisión nueva por Telegram sigue bloqueada fail-closed con bandera en False
            antes = derivar_autoridad_aprobacion(rig.storage)
            await polling._process_raw_update(_update_texto(811, "/gm_preparar"))
            await coord.esperar_idle()
            cb_data, msg_id, _sol_id = _extraer_token_de_ultimo_envio(sender, "approve")
            await polling._process_raw_update(_update_callback(812, cb_data, message_id=msg_id))
            await coord.esperar_idle()

            despues = derivar_autoridad_aprobacion(rig.storage)
            assert len(despues.witnesses) == len(antes.witnesses)
            assert "EMISION_PRODUCTIVA_HABILITADA = False" in sender.send.await_args_list[-1].args[1]
        finally:
            await coord.cerrar()


# ---------------------------------------------------------------------------
# 9. Auditoría adversarial: timeout/cancelación, bots, recarga y AppContext
# ---------------------------------------------------------------------------


class TestAuditoriaAdversarialTransporte:
    @pytest.mark.asyncio
    async def test_timeout_en_entrega_cancela_coro_en_vuelo_y_no_entrega_botones_tardios(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: si _entregar vence por delivery_timeout, cancela la coroutine de envío sin botones tardíos."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        sender = _crear_sender_simulado()
        cancelada = asyncio.Event()
        cleanup_drenado = asyncio.Event()
        entregado_tarde = asyncio.Event()

        async def _send_lento(
            chat_id: int, text: str, reply_markup: dict | None = None, **_kw: object
        ) -> TelegramMessage:
            if reply_markup is not None:
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    cancelada.set()
                    await asyncio.sleep(0.03)
                    cleanup_drenado.set()
                    raise
                entregado_tarde.set()
            return TelegramMessage(chat_id=chat_id, message_id=850)

        sender.send = AsyncMock(side_effect=_send_lento)
        coord = CoordinadorTelegramGoldenMaster(
            sender=sender,
            operador_autorizado=OPERADOR,
            objetivo=_objetivo_desde_rig(rig),
            delivery_timeout=0.05,
        )
        try:
            # Act
            await coord.procesar_actualizacion(_update_texto(900, "/gm_preparar"))

            # Assert inmediato: el worker canceló Y drenó el cleanup async antes de liberar single-flight
            assert cancelada.is_set(), "timeout en _entregar debe cancelar fut para no entregar botones tardíos"
            assert cleanup_drenado.is_set(), "el envío cancelado debe drenarse antes de retirar el futuro y retornar"
            assert not coord._entregas_en_vuelo
            await asyncio.sleep(0.1)
            assert not entregado_tarde.is_set()
            assert coord.tiene_solicitud_abierta is False
            assert coord._operacion_en_curso is False
            assert derivar_autoridad_aprobacion(rig.storage).witnesses == ()
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_timeout_con_carrera_de_envio_completado_retira_botones_y_rechaza_aprobacion(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: si sendMessage terminó justo al vencer el timeout, se invalidan token y botones emitidos."""
        # Arrange
        import sky_claw.app.comms.golden_master_hitl as gm_mod

        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        sender = _crear_sender_simulado()
        coord = CoordinadorTelegramGoldenMaster(
            sender=sender,
            operador_autorizado=OPERADOR,
            objetivo=_objetivo_desde_rig(rig),
            delivery_timeout=1.0,
        )
        real_rcts = gm_mod.asyncio.run_coroutine_threadsafe

        class _FutCarreraTimeout:
            def __init__(self, inner: object) -> None:
                self._inner = inner

            def cancel(self) -> bool:
                return self._inner.cancel()  # type: ignore[attr-defined]

            def done(self) -> bool:
                return self._inner.done()  # type: ignore[attr-defined]

            def cancelled(self) -> bool:
                return self._inner.cancelled()  # type: ignore[attr-defined]

            def exception(self, timeout: float | None = None) -> BaseException | None:
                return self._inner.exception(timeout=timeout)  # type: ignore[attr-defined]

            def result(self, timeout: float | None = None) -> object:
                if timeout == 0:
                    return self._inner.result(timeout=0)  # type: ignore[attr-defined]
                # Simula la ventana exacta donde sendMessage ya devolvió message_id pero el caller ve TimeoutError.
                self._inner.result(timeout=5.0)  # type: ignore[attr-defined]
                raise TimeoutError("ventana de timeout tras enviar")

        monkeypatch.setattr(
            gm_mod.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _FutCarreraTimeout(real_rcts(coro, loop))
        )

        try:
            # Act
            await coord.procesar_actualizacion(_update_texto(901, "/gm_preparar"))
            cb_tardio, msg_id, _sol_id = _extraer_token_de_ultimo_envio(sender, "approve")

            # Assert: botones eliminados (reply_markup=None) y aprobación posterior imposible
            sender.edit_message.assert_awaited()
            edit_call = sender.edit_message.await_args_list[-1]
            assert edit_call.args[1] == msg_id
            assert edit_call.kwargs.get("reply_markup") is None

            await coord.procesar_actualizacion(_update_callback(902, cb_tardio, message_id=msg_id))
            assert coord.tiene_solicitud_abierta is False
            assert derivar_autoridad_aprobacion(rig.storage).witnesses == ()
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_cancelar_tarea_durante_entrega_colgada_desbloquea_rapido(self, tmp_path: pathlib.Path) -> None:
        """AAA: cancelar la Task durante _entregar colgado aborta la entrega sin esperar 20s de delivery_timeout."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        sender = _crear_sender_simulado()
        envio_iniciado = asyncio.Event()
        envio_cancelado = asyncio.Event()

        async def _send_colgado(
            chat_id: int, text: str, reply_markup: dict | None = None, **_kw: object
        ) -> TelegramMessage:
            if reply_markup is not None:
                envio_iniciado.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    envio_cancelado.set()
                    raise
            return TelegramMessage(chat_id=chat_id, message_id=860)

        sender.send = AsyncMock(side_effect=_send_colgado)
        coord = CoordinadorTelegramGoldenMaster(
            sender=sender,
            operador_autorizado=OPERADOR,
            objetivo=_objetivo_desde_rig(rig),
            delivery_timeout=20.0,
        )
        try:
            # Act
            tarea = asyncio.create_task(coord.procesar_actualizacion(_update_texto(903, "/gm_preparar")))
            await asyncio.wait_for(envio_iniciado.wait(), timeout=5.0)
            tarea.cancel()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tarea, timeout=5.0)

            # Assert
            assert envio_cancelado.is_set()
            assert coord.tiene_solicitud_abierta is False
            assert coord._operacion_en_curso is False
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_rechaza_bots_spoof_is_bot_via_bot_ediciones_y_envoltorio_malformado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: rechaza from.is_bot (incluidos no-bool), via_bot, edited/channel/business updates y envoltorio roto."""
        # Arrange
        rig = RigStockManual(tmp_path, steam_activo=True)
        monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
        coord, webhook, _polling, sender, _router = _construir_entorno(rig)
        try:
            await webhook.process_update(_update_texto(910, "/gm_preparar"))
            await coord.esperar_idle()
            cb_approve, msg_id, solicitud_id = _extraer_token_de_ultimo_envio(sender, "approve")
            cb_deny, _msg_id2, _ = _extraer_token_de_ultimo_envio(sender, "deny")

            ataques: list[object] = [
                # Remitente bot explícito o is_bot spoofeado en texto y en callback
                {
                    "update_id": 911,
                    "message": {
                        "message_id": 1,
                        "from": {"id": OPERADOR, "is_bot": True},
                        "chat": {"id": OPERADOR, "type": "private"},
                        "text": f"/gm_aprobar {solicitud_id}",
                    },
                },
                {
                    "update_id": 912,
                    "message": {
                        "message_id": 2,
                        "from": {"id": OPERADOR, "is_bot": "false"},
                        "chat": {"id": OPERADOR, "type": "private"},
                        "text": f"/gm_aprobar {solicitud_id}",
                    },
                },
                {
                    "update_id": 913,
                    "message": {
                        "message_id": 3,
                        "from": {"id": OPERADOR, "is_bot": 1},
                        "chat": {"id": OPERADOR, "type": "private"},
                        "text": f"/gm_aprobar {solicitud_id}",
                    },
                },
                _update_callback(
                    914,
                    cb_approve,
                    message_id=msg_id,
                    extra_query={"from": {"id": OPERADOR, "is_bot": True}},
                ),
                _update_callback(
                    915,
                    cb_approve,
                    message_id=msg_id,
                    extra_query={"from": {"id": OPERADOR, "is_bot": "false"}},
                ),
                # Comando mediado por inline bot (via_bot)
                _update_texto(
                    916,
                    f"/gm_aprobar {solicitud_id}",
                    extra_msg={"via_bot": {"id": 777, "is_bot": True}},
                ),
                # Ediciones / posts de canal / mensajes de negocio
                {
                    "update_id": 917,
                    "edited_message": {
                        "message_id": 4,
                        "from": {"id": OPERADOR, "is_bot": False},
                        "chat": {"id": OPERADOR, "type": "private"},
                        "text": f"/gm_aprobar {solicitud_id}",
                    },
                },
                {
                    "update_id": 918,
                    "channel_post": {
                        "message_id": 5,
                        "from": {"id": OPERADOR, "is_bot": False},
                        "chat": {"id": OPERADOR, "type": "private"},
                        "text": f"/gm_aprobar {solicitud_id}",
                    },
                },
                # Contenedor message malformado con atributos suplantados en la raíz
                {
                    "update_id": 919,
                    "message": "corrupto",
                    "from": {"id": OPERADOR, "is_bot": False},
                    "chat": {"id": OPERADOR, "type": "private"},
                    "text": f"/gm_aprobar {solicitud_id}",
                },
                # Update no-Mapping pasado directo al coordinador
                "update-invalido",
            ]

            # Act
            for ataque in ataques:
                if isinstance(ataque, dict):
                    await webhook.process_update(ataque)
                await coord.procesar_actualizacion(ataque)  # type: ignore[arg-type]
                await coord.esperar_idle()

            # Assert: ninguno aprobó ni cerró la sesión legítima abierta
            assert not list(issuances_state_dir(rig.storage).iterdir())
            assert coord.tiene_solicitud_abierta is True

            # Control positivo Telegram real: en un callback real, message.from IS nuestro bot (is_bot=True),
            # mientras que callback_query.from ES el operador humano (is_bot=False). Debe seguir funcionando.
            await webhook.process_update(
                _update_callback(
                    920,
                    cb_deny,
                    message_id=msg_id,
                    extra_query={"from": {"id": OPERADOR, "is_bot": False}},
                    extra_msg={"from": {"id": 888999, "is_bot": True}},
                )
            )
            await coord.esperar_idle()
            assert coord.tiene_solicitud_abierta is False
            assert not list(issuances_state_dir(rig.storage).iterdir())
        finally:
            await coord.cerrar()

    @pytest.mark.asyncio
    async def test_actualizar_transporte_bloquea_admision_durante_recarga_y_app_context_stop_cierra_reload(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AAA: actualizar_transporte no admite requests con operador viejo mid-reload y AppContext.stop() cierra lo recargado."""
        import argparse

        from sky_claw.app_context import AppContext

        rig = RigStockManual(tmp_path, steam_activo=True)
        sender_a = _crear_sender_simulado()
        edit_entro = asyncio.Event()
        liberar_edit = asyncio.Event()

        async def _edit_lento(*_args: object, **_kwargs: object) -> None:
            edit_entro.set()
            await asyncio.wait_for(liberar_edit.wait(), timeout=5.0)

        sender_a.edit_message = AsyncMock(side_effect=_edit_lento)
        coord = CoordinadorTelegramGoldenMaster(
            sender=sender_a,
            operador_autorizado=OPERADOR,
            objetivo=_objetivo_desde_rig(rig),
        )
        # 1) Bloqueo mid-reload
        await coord.procesar_actualizacion(_update_texto(930, "/gm_preparar"))
        assert coord.tiene_solicitud_abierta is True

        sender_b = _crear_sender_simulado()
        recarga = asyncio.create_task(coord.actualizar_transporte(sender=sender_b, operador_autorizado=None))
        try:
            await asyncio.wait_for(edit_entro.wait(), timeout=5.0)
            # Mientras edit_message de la recarga sigue en vuelo, NO debe admitir comandos para el operador anterior
            tarea_tardia = await coord.iniciar_comando(_update_texto(931, "/gm_estado"))
            assert tarea_tardia is None
        finally:
            liberar_edit.set()
        await asyncio.wait_for(recarga, timeout=5.0)

        # 2) AppContext.stop() cierra polling y coordinador agregados tras hot-reload (sin start_full con token)
        args = argparse.Namespace(
            db_path=str(tmp_path / "app.db"),
            mo2_root=tmp_path / "MO2",
            staging_dir=tmp_path / "staging",
            provider="ollama",
            operator_chat_id=None,
            loot_exe=None,
            install_dir=None,
            mode="cli",
        )
        ctx = AppContext(args)
        fake_polling = MagicMock()
        fake_polling.stop = AsyncMock()
        ctx.polling = fake_polling
        ctx.golden_master_coordinator = coord

        await ctx.stop()
        fake_polling.stop.assert_awaited_once()
        assert coord._cerrado is True
        assert ctx.polling is None
        assert ctx.golden_master_coordinator is None

        # 3) Si el polling ya estaba registrado en startup cleanup, stop() no lo detiene dos veces
        polling_registrado = MagicMock()
        polling_registrado.stop = AsyncMock()
        ctx._push_startup_cleanup(ctx._stop_polling, polling_registrado)
        ctx.polling = polling_registrado
        ctx._cleanup_polling_registrado = polling_registrado
        await ctx.stop()
        polling_registrado.stop.assert_awaited_once()
        assert ctx.polling is None

    @pytest.mark.asyncio
    async def test_frontend_bridge_reload_crea_primer_coordinador_y_app_context_stop_lo_drena_antes_de_red(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AAA: si arranca sin token y FrontendBridge._reload_telegram crea el primer coordinador, stop() cierra en orden LIFO."""
        import argparse
        from types import SimpleNamespace

        from sky_claw.app.comms.frontend_bridge import FrontendBridge
        from sky_claw.app.comms.telegram_polling import TelegramPolling
        from sky_claw.app_context import AppContext

        rig = RigStockManual(tmp_path, steam_activo=True)
        args = argparse.Namespace(
            db_path=str(tmp_path / "app.db"),
            mo2_root=tmp_path / "MO2",
            staging_dir=tmp_path / "staging",
            provider="ollama",
            operator_chat_id=None,
            loot_exe=None,
            install_dir=None,
            mode="cli",
        )
        ctx = AppContext(args)
        ctx.config_path = tmp_path / "config.toml"
        ctx.router = MagicMock()
        ctx.hitl = MagicMock()
        ctx.network = MagicMock(gateway=MagicMock(), session=MagicMock())
        ctx._golden_master_objetivo = _objetivo_desde_rig(rig)

        eventos: list[str] = []
        red_abierta = True

        async def _cerrar_red() -> None:
            nonlocal red_abierta
            red_abierta = False
            eventos.append("network_close")

        ctx._push_startup_cleanup(_cerrar_red)

        async def _send_ok(chat_id: int, text: str, reply_markup: dict | None = None, **_kw: object) -> TelegramMessage:
            assert red_abierta is True
            return TelegramMessage(chat_id=chat_id, message_id=951)

        async def _edit_ok(*_args: object, **_kwargs: object) -> bool:
            assert red_abierta is True, "cerrar coordinador debe limpiar botones con red aun abierta"
            eventos.append("gm_edit")
            return True

        monkeypatch.setattr("sky_claw.app.comms.telegram_sender.TelegramSender.send", AsyncMock(side_effect=_send_ok))
        monkeypatch.setattr(
            "sky_claw.app.comms.telegram_sender.TelegramSender.edit_message",
            AsyncMock(side_effect=_edit_ok),
        )
        monkeypatch.setattr(TelegramPolling, "start", AsyncMock())

        async def _stop_poll(self_poll: object) -> None:
            assert red_abierta is True
            eventos.append("polling_stop")

        monkeypatch.setattr(TelegramPolling, "stop", _stop_poll)

        bridge = FrontendBridge(ctx.router, ctx.network.session, SimpleNamespace(), ctx)
        ok = await bridge._reload_telegram("123456:TESTTOKEN", str(OPERADOR))
        assert ok is True
        coord = ctx.golden_master_coordinator
        assert coord is not None

        await coord.procesar_actualizacion(_update_texto(940, "/gm_preparar"))
        assert coord.tiene_solicitud_abierta is True

        await ctx.stop()
        assert eventos == ["polling_stop", "gm_edit", "network_close"]
        assert coord._cerrado is True
        assert ctx.polling is None
        assert ctx.golden_master_coordinator is None
