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
import os
import pathlib
import threading
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.golden_master_hitl import (
    CoordinadorTelegramGoldenMaster,
    ObjetivoTransicionGoldenMaster,
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
from sky_claw.app.comms.telegram_sender import TelegramMessage, TelegramSendError
from sky_claw.app.security.hitl import HITLGuard
from sky_claw.config import Config
from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import autorizacion_hitl_transicion as hitl_wiring
from sky_claw.local.frozen_runtime import binder_recovery as recovery_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import mo2_ini_exclusion
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
            assert liberar_worker.wait(timeout=5.0)
            return original(self, solicitud=solicitud, decision=decision, identidad=identidad)

        monkeypatch.setattr(SesionTransicionGoldenMaster, "decidir_y_emitir", _decidir_lento)

        coord, _webhook, polling, sender, _router = _construir_entorno(rig)
        try:
            await polling._process_raw_update(_update_texto(200, "/gm_preparar"))
            await coord.esperar_idle()
            cb_approve, msg_id, _ = _extraer_token_de_ultimo_envio(sender, "approve")

            # Primer clic entra al worker y queda bloqueado antes de terminar
            await polling._process_raw_update(_update_callback(201, cb_approve, message_id=msg_id))
            await asyncio.to_thread(barrera_entrada.wait, 5.0)

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
                assert soltar.wait(timeout=5.0)
                return original_preparar(self, identidad=identidad, entregar=entregar, transition_id=transition_id)
            finally:
                salio.set()

        monkeypatch.setattr(SesionTransicionGoldenMaster, "preparar", _preparar_lento)
        coord, _webhook, _polling, _sender, _router = _construir_entorno(rig)
        try:
            tarea = asyncio.create_task(coord.procesar_actualizacion(_update_texto(400, "/gm_preparar")))
            await asyncio.to_thread(adentro.wait, 5.0)

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
            envio_iniciado.set()
            await asyncio.Future()

        sender.send = AsyncMock(side_effect=_send_eterno)
        coord, _webhook, polling, _sender, _router = _construir_entorno(rig, sender=sender)
        await polling._process_raw_update(_update_texto(440, "/gm_preparar"))
        await asyncio.wait_for(envio_iniciado.wait(), timeout=3.0)

        # cerrar() cancela la entrega en vuelo y termina rápido sin deadlock
        await asyncio.wait_for(coord.cerrar(), timeout=3.0)
        assert coord.tiene_solicitud_abierta is False

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
