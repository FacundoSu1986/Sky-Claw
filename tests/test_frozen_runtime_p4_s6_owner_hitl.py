"""P4-S6 — consentimiento HITL del propietario ligado a la autoridad durable.

Cierra el bloqueante ``OWNER_IDENTITY_BINDING = DEFERRED`` del ADR 0012 (§35.2).
Hasta P4-S5 la emisión productiva estaba cerrada porque **no existía superficie
HITL**: ``ConsentimientoAprobacion`` era un objeto de tres cadenas que cualquiera
podía construir en código, así que llamarlo «consentimiento humano» era una
afirmación sin prueba.

La propiedad que estos tests congelan:

    UNA APROBACIÓN SÓLO SE EMITE SI UN PROPIETARIO IDENTIFICADO EN UNA SUPERFICIE
    CON MECANISMO DE IDENTIDAD DEMOSTRABLE RESPONDIÓ —SOBRE EL DIGEST EXACTO QUE
    SE LE PRESENTÓ, EN LA MISMA SESIÓN, PARA EL MISMO ROOT— Y NINGÚN CAMINO
    EQUIVALENTE EXISTE DESDE EL AGENTE LLM, TELEGRAM NI LA GUI.

Se verifica en tres niveles, y los tres se ejercitan acá:

1. **La propuesta es inmutable entre presentación y emisión**: el consentimiento
   liga el digest presentado, y cualquier cambio material del scope lo invalida
   (no se «adapta» el scope para que encaje).
2. **La identidad no es un campo del llamador**: el consentimiento sólo lo
   produce :func:`responder`, que exige una solicitud ABIERTA por
   :func:`presentar` y una :class:`IdentidadPropietario` cuyo mecanismo está en
   el censo y verifica identidad.
3. **La enumeración, no el muestreo**: el censo de superficies y el conjunto de
   módulos que construyen consentimiento se congelan por AST, así que una
   superficie nueva rompe el ancla hasta que se le declare su mecanismo.

``HITL_PRODUCTIVE = DISABLED`` a propósito (FASE F del brief): el consentimiento
verificable está implementado y probado end-to-end, pero la emisión productiva
sigue cerrada porque todavía no existe un consumidor seguro que evite dejar
autorizaciones ``ISSUED`` sin salida operativa. El interruptor es UNO
(``EMISION_PRODUCTIVA_HABILITADA``) y tiene su propio test.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import owner_consent
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    ApprovalOperation,
    ApprovalScope,
    ConsentimientoAprobacion,
    _emitir_aprobacion_bajo_lock,
    cargar_ledger_aprobacion,
    emitir_aprobacion,
    proponer_aprobacion,
    revocar_aprobacion,
)
from sky_claw.local.frozen_runtime.errors import (
    ApprovalConsentimientoError,
    ApprovalHitolNoDisponibleError,
    ApprovalIdentityError,
    ApprovalRootAjenoError,
    ApprovalSchemaError,
    ConsentimientoDuplicadoError,
    IdentidadPropietarioNoVerificadaError,
    SesionConsentimientoNoCoincideError,
    SolicitudConsentimientoDesconocidaError,
    SuperficieSinIdentidadVerificadaError,
)
from sky_claw.local.frozen_runtime.journal import SourceActivation
from sky_claw.local.frozen_runtime.owner_consent import (
    SUPERFICIES_DE_CONSENTIMIENTO,
    DecisionConsentimiento,
    IdentidadPropietario,
    RegistroConsentimientos,
    SuperficieConsentimiento,
    verificar_identidad_telegram,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import (
    approvals_state_dir,
    initialize_frozen_runtime_storage,
    issuances_state_dir,
)

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
OPERADOR = 424242


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = tmp_path / nombre
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success, resultado.admission.message
    return root


def _fuente(**overrides: object) -> SourceActivation:
    base: dict[str, object] = {
        "generation_id": GEN_ORIGEN,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": "b" * 64,
    }
    base.update(overrides)
    return SourceActivation(**base)  # type: ignore[arg-type]


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation": ApprovalOperation.PROMOTION,
        "source_activation": _fuente(),
        "generation_id": GEN_OBJETIVO,
        "clone_id": "clone-objetivo",
        "clone_evidence": "d" * 64,
        "runtime_setup_id": "setup-objetivo",
        "runtime_setup_evidence": "e" * 64,
        "compatibility_evidence_id": "f" * 64,
        "candidate_id": "cand-1",
        "approval_provenance": "telegram:req-1",
    }
    base.update(overrides)
    return base


def _propuesta(root: pathlib.Path, **overrides: object):
    return proponer_aprobacion(root, **_kwargs(**overrides))  # type: ignore[arg-type]


def _identidad(
    *,
    sesion: str = "chat-424242",
    sujeto: str = str(OPERADOR),
    superficie: SuperficieConsentimiento = SuperficieConsentimiento.TELEGRAM,
    mecanismo: str | None = None,
) -> IdentidadPropietario:
    return IdentidadPropietario(
        superficie=superficie,
        sujeto=sujeto,
        sesion=sesion,
        mecanismo=mecanismo or SUPERFICIES_DE_CONSENTIMIENTO[superficie].nombre,
        verificado_en_ns=1_700_000_000_000_000_000,
    )


def _presentar(root: pathlib.Path, propuesta, registro: RegistroConsentimientos, **overrides: object):
    datos: dict[str, object] = {"identidad": _identidad()}
    datos.update(overrides)
    return registro.presentar(root, propuesta, **datos)  # type: ignore[arg-type]


def _emitir_verificado(root: pathlib.Path, propuesta, consentimiento) -> None:
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        _emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)


def _instantanea_state(root: pathlib.Path) -> dict[str, bytes]:
    """Bytes de todo ``state/`` menos el lock, que es estado del mutex."""
    return {
        ruta.relative_to(root).as_posix(): ruta.read_bytes()
        for ruta in root.joinpath("state").rglob("*")
        if ruta.is_file() and ruta.name != "frozen-runtime.lock"
    }


def _modulos_bajo(directorio: pathlib.Path) -> list[pathlib.Path]:
    return sorted(ruta for ruta in directorio.rglob("*.py") if "__pycache__" not in ruta.parts)


def _modulos_importados(ruta: pathlib.Path) -> set[str]:
    """Módulos que el archivo importa, resueltos por AST.

    ``import a.b`` aporta ``a.b``. ``from a.b import c`` aporta ``a.b`` **y**
    ``a.b.c``: las dos formas alcanzan el símbolo, y un escaneo por substring de
    ``import a.b`` sólo ve la primera — ``from sky_claw.local.frozen_runtime
    import approval`` es la forma natural de importarlo y se le escapaba.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    modulos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and not nodo.level and nodo.module:
            modulos.add(nodo.module)
            modulos.update(f"{nodo.module}.{alias.name}" for alias in nodo.names)
    return modulos


def _nombres_llamados(ruta: pathlib.Path) -> set[str]:
    """Nombres de función invocados en un archivo, sin resolver el binding."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            nombres.add(nodo.func.id)
        elif isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            nombres.add(nodo.func.attr)
    return nombres


# ---------------------------------------------------------------------------
# 1. Aprobación humana exacta — el camino feliz, end-to-end
# ---------------------------------------------------------------------------


def test_aprobacion_humana_exacta_emite_el_witness(tmp_path: pathlib.Path) -> None:
    """Presentar → aprobar → emitir: el witness durable es el del digest presentado."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)

    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad(),
    )

    assert consentimiento is not None
    assert consentimiento.approval_scope_digest == presentacion.approval_scope_digest
    assert consentimiento.solicitud_id == presentacion.solicitud_id
    assert consentimiento.session_id == presentacion.sesion
    assert consentimiento.owner_subject == str(OPERADOR)
    assert consentimiento.root_id == presentacion.root_id

    _emitir_verificado(root, propuesta, consentimiento)

    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.ISSUED
    assert ledger.last_issued_scope_digest == presentacion.approval_scope_digest
    witness = json.loads((issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").read_text("utf-8"))
    assert witness["approval_scope_digest"] == presentacion.approval_scope_digest
    assert witness["approval_id"] == propuesta.scope.approval_id


def test_la_presentacion_muestra_los_14_campos_del_scope(tmp_path: pathlib.Path) -> None:
    """``FULL_SCOPE_PRESENTED``: el propietario ve TODOS los campos, no un resumen.

    Se enumera sobre ``dataclasses.fields(ApprovalScope)``: un campo nuevo en el
    scope rompe este test hasta que la presentación lo muestre.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)

    campos = [campo.name for campo in dataclasses.fields(ApprovalScope)]
    assert len(campos) == approval_module.APPROVAL_SCOPE_FIELDS == 14
    for nombre in campos:
        assert nombre in presentacion.texto, f"la presentación no muestra {nombre}"
    for nombre in campos:
        if nombre == "approval_scope_digest":
            assert presentacion.approval_scope_digest in presentacion.texto
            continue
        visible = owner_consent.render_valor_scope(presentacion.scope_payload[nombre])
        assert visible in presentacion.texto, f"la presentación no muestra el valor de {nombre}"


def test_el_lock_no_se_sostiene_durante_la_presentacion(tmp_path: pathlib.Path) -> None:
    """§39.4: la interacción humana ocurre FUERA de la sección crítica.

    El root lock NO es reentrante (``root_lock.participant_lock``): si
    ``presentar`` intentara adquirirlo mientras el test lo tiene tomado, fallaría
    con ``FROZEN_RUNTIME_LOCK_BUSY``. Que complete prueba que no lo toma. Una
    espera humana no acotada dentro del lock lo volvería ilimitado.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)

    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        presentacion = _presentar(root, propuesta, registro)

    assert presentacion.solicitud_id
    assert registro.pendientes() == (presentacion,)


# ---------------------------------------------------------------------------
# 2. Cancelación y rechazo — ninguna escritura durable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", [DecisionConsentimiento.RECHAZAR, DecisionConsentimiento.CANCELAR])
def test_denegacion_no_emite_ni_escribe_nada(tmp_path: pathlib.Path, decision: DecisionConsentimiento) -> None:
    """``SFR-23``: si el propietario rechaza o cancela, no se persiste NADA."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    antes = _instantanea_state(root)

    resultado = registro.responder(presentacion.solicitud_id, decision=decision, identidad=_identidad())

    assert resultado is None
    assert _instantanea_state(root) == antes
    assert not list(issuances_state_dir(root).iterdir())
    assert cargar_ledger_aprobacion(root).ledger is None


def test_una_denegacion_no_se_puede_convertir_en_aprobacion(tmp_path: pathlib.Path) -> None:
    """Una solicitud resuelta es terminal: el segundo intento NO cambia la decisión."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    assert (
        registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.RECHAZAR, identidad=_identidad())
        is None
    )

    with pytest.raises(ConsentimientoDuplicadoError):
        registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad())


# ---------------------------------------------------------------------------
# 3. Sesión equivocada / replay entre sesiones
# ---------------------------------------------------------------------------


def test_una_sesion_distinta_no_puede_responder(tmp_path: pathlib.Path) -> None:
    """La solicitud liga la SESIÓN que la originó, no sólo el sujeto."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)

    with pytest.raises(SesionConsentimientoNoCoincideError):
        registro.responder(
            presentacion.solicitud_id,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(sesion="chat-OTRO"),
        )
    assert not list(issuances_state_dir(root).iterdir())


def test_un_sujeto_distinto_en_la_misma_sesion_no_puede_responder(tmp_path: pathlib.Path) -> None:
    """El hermano del test anterior: la sesión queda igual y cambia el SUJETO.

    ``responder`` compara sujeto y sesión de forma independiente. El test de
    sesión cambia sólo la sesión, así que pasaría igual si la comprobación de
    sujeto desapareciera; éste la aísla.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)

    with pytest.raises(SesionConsentimientoNoCoincideError):
        registro.responder(
            presentacion.solicitud_id,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(sujeto="999"),
        )
    assert not list(issuances_state_dir(root).iterdir())


def test_el_consentimiento_lleva_la_sesion_del_propietario_no_la_del_proceso(tmp_path: pathlib.Path) -> None:
    """La sesión que viaja al consentimiento es la del PROPIETARIO que respondió.

    El ``session_id`` del root lock es un proceso, no una persona (§35.2). Si el
    consentimiento copiara ése, el binding de sesión sería decorativo.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro, identidad=_identidad(sesion="chat-del-dueno"))
    consentimiento = registro.responder(
        presentacion.solicitud_id,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad(sesion="chat-del-dueno"),
    )
    assert consentimiento is not None
    assert consentimiento.session_id == "chat-del-dueno"
    assert consentimiento.session_id != approval_module._root_id(root)
    assert consentimiento.mecanismo == "telegram_from_id"
    assert consentimiento.surface == "telegram"


def test_la_entrada_productiva_rechaza_una_fixture_sin_binding(tmp_path: pathlib.Path) -> None:
    """Sin binding de propietario no hay emisión, ni siquiera por el camino nuevo."""
    root = _root(tmp_path)
    propuesta = _propuesta(root)
    fixture = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture",
    )
    with pytest.raises(ApprovalHitolNoDisponibleError, match="propietario"):
        owner_consent.emitir_aprobacion_autorizada(root, propuesta, fixture)
    assert not list(issuances_state_dir(root).iterdir())


# ---------------------------------------------------------------------------
# 4. Solicitud sin propietario verificable
# ---------------------------------------------------------------------------


def test_una_superficie_sin_prueba_de_identidad_no_consiente(tmp_path: pathlib.Path) -> None:
    """La GUI no tiene login: presencia local NO es identidad demostrable (§35.2).

    Aceptarla sería exactamente el defecto que el slice cierra: reemplazar la
    prueba de identidad por un botón.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)

    with pytest.raises(SuperficieSinIdentidadVerificadaError):
        _presentar(root, propuesta, registro, identidad=_identidad(superficie=SuperficieConsentimiento.GUI))


def test_un_mecanismo_fuera_del_censo_no_consiente(tmp_path: pathlib.Path) -> None:
    """Un ``mecanismo`` inventado por el llamador no habilita nada."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)

    with pytest.raises(IdentidadPropietarioNoVerificadaError, match="censo"):
        _presentar(root, propuesta, registro, identidad=_identidad(mecanismo="confio_en_el_llamador"))


def test_la_identidad_de_telegram_se_verifica_contra_el_transporte() -> None:
    """El mecanismo real: ``from.id`` del update, no un campo de la aplicación."""
    update = {
        "from": {"id": OPERADOR, "is_bot": False},
        "chat": {"id": OPERADOR, "type": "private"},
    }
    identidad = verificar_identidad_telegram(update, operador_configurado=OPERADOR)
    assert identidad.superficie is SuperficieConsentimiento.TELEGRAM
    assert identidad.sujeto == str(OPERADOR)
    assert identidad.sesion == str(OPERADOR)
    assert identidad.mecanismo == "telegram_from_id"


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"from": {"id": 999}, "chat": {"id": 999, "type": "private"}}, id="otro-usuario"),
        pytest.param({"from": {"id": OPERADOR}, "chat": {"id": -100123, "type": "group"}}, id="grupo"),
        # Sin ``type`` declarado: la única prueba de que el chat es privado es
        # ``chat.id == from.id``. Un update que omita el tipo no puede colarse.
        pytest.param({"from": {"id": OPERADOR}, "chat": {"id": -100123}}, id="chat-ajeno-sin-tipo"),
        pytest.param(
            {"from": {"id": OPERADOR}, "chat": {"id": -100123, "type": "private"}}, id="chat-ajeno-tipo-mentido"
        ),
        pytest.param(
            {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}, "forward_date": 1},
            id="reenviado",
        ),
        pytest.param(
            {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}, "forward_from": {"id": 1}},
            id="forward_from",
        ),
        pytest.param({"from": {}, "chat": {"id": OPERADOR, "type": "private"}}, id="sin-from-id"),
        pytest.param({"from": {"id": str(OPERADOR)}, "chat": {"id": OPERADOR}}, id="id-como-texto"),
        pytest.param({"from": {"id": True}, "chat": {"id": OPERADOR}}, id="id-booleano"),
        pytest.param({}, id="vacio"),
    ],
)
def test_la_verificacion_de_telegram_rechaza_lo_que_el_transporte_rechaza(update: dict) -> None:
    """Matriz adversarial del mecanismo de identidad, caso por caso."""
    with pytest.raises(IdentidadPropietarioNoVerificadaError):
        verificar_identidad_telegram(update, operador_configurado=OPERADOR)


def test_la_identidad_de_telegram_acepta_un_callback_query() -> None:
    """La decisión real llega por botón: el remitente vive en ``callback_query.from``.

    Leer sólo el nivel superior rechazaría TODOS los botones —fail-closed, pero
    con la superficie inservible—. El chat viene del mensaje que originó el botón.
    """
    update = {
        "callback_query": {
            "from": {"id": OPERADOR, "is_bot": False},
            "message": {"chat": {"id": OPERADOR, "type": "private"}, "message_id": 7},
            "data": "aprobar:solicitud-1",
        }
    }
    identidad = verificar_identidad_telegram(update, operador_configurado=OPERADOR)
    assert identidad.sujeto == str(OPERADOR)
    assert identidad.sesion == str(OPERADOR)


@pytest.mark.parametrize(
    "callback",
    [
        pytest.param({"from": {"id": 999}, "message": {"chat": {"id": 999, "type": "private"}}}, id="otro-usuario"),
        pytest.param(
            {"from": {"id": OPERADOR}, "message": {"chat": {"id": -100123, "type": "supergroup"}}}, id="grupo"
        ),
        pytest.param(
            {"from": {"id": OPERADOR}, "message": {"chat": {"id": -100123, "type": "private"}}}, id="chat-ajeno"
        ),
        pytest.param(
            {"from": {"id": OPERADOR}, "message": {"forward_date": 1, "chat": {"id": OPERADOR}}}, id="reenviado"
        ),
    ],
)
def test_un_callback_query_no_autorizado_se_rechaza(callback: dict) -> None:
    """Misma matriz de ataques, por la vía del botón."""
    with pytest.raises(IdentidadPropietarioNoVerificadaError):
        verificar_identidad_telegram({"callback_query": callback}, operador_configurado=OPERADOR)


def test_la_verificacion_concuerda_con_el_guard_del_transporte() -> None:
    """Ancla del HERMANO: dos implementaciones del mismo contrato no pueden divergir.

    ``TelegramWebhook._validate_private_operator`` es el guard que ya usa el
    producto para las tools mutantes. Si la verificación de consentimiento y ese
    guard dejan de coincidir en cualquier caso de la matriz, uno de los dos está
    autorizando algo que el otro rechaza.
    """
    from sky_claw.app.comms.telegram import TelegramWebhook

    guard = TelegramWebhook(None, None, None, authorized_user_id=OPERADOR)  # type: ignore[arg-type]
    casos = [
        {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR, "type": "private"}},
        {"from": {"id": 999}, "chat": {"id": 999, "type": "private"}},
        {"from": {"id": OPERADOR}, "chat": {"id": -100123, "type": "group"}},
        {"from": {"id": OPERADOR}, "chat": {"id": -100123}},
        {"from": {"id": OPERADOR}, "chat": {"id": -100123, "type": "private"}},
        {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR}, "forward_date": 1},
        {"from": {"id": OPERADOR}, "chat": {"id": OPERADOR}, "forward_from": {"id": 1}},
        {"from": {}, "chat": {"id": OPERADOR}},
        # La forma en que llega un botón: el guard del transporte recibe el
        # callback_query entero, así que las dos implementaciones tienen que
        # aceptar y rechazar exactamente lo mismo.
        {
            "callback_query": {
                "from": {"id": OPERADOR},
                "message": {"chat": {"id": OPERADOR, "type": "private"}},
            }
        },
        {
            "callback_query": {
                "from": {"id": 999},
                "message": {"chat": {"id": 999, "type": "private"}},
            }
        },
    ]
    for update in casos:
        try:
            verificar_identidad_telegram(update, operador_configurado=OPERADOR)
            aceptado_por_consentimiento = True
        except IdentidadPropietarioNoVerificadaError:
            aceptado_por_consentimiento = False
        # El guard recibe el ``callback_query`` YA desenvuelto por el dispatcher
        # de Telegram; el verificador acepta las dos formas. Se comparan al mismo
        # nivel de envoltorio para que la diferencia no sea del test.
        carga = update.get("callback_query", update)
        assert aceptado_por_consentimiento is guard._validate_private_operator(carga), update


# ---------------------------------------------------------------------------
# 5. Scope modificado después de presentarse
# ---------------------------------------------------------------------------


def test_un_scope_adulterado_tras_la_presentacion_no_emite(tmp_path: pathlib.Path) -> None:
    """La propuesta es INMUTABLE entre presentación y emisión.

    El ataque: conservar el digest que el propietario aprobó y cambiar el resto
    del scope. ``validar_scope`` recomputa el digest ⇒ rechazo.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    adulterado = dataclasses.replace(propuesta.scope, clone_id="clone-ADULTERADO")
    propuesta_adulterada = dataclasses.replace(propuesta, scope=adulterado)
    with pytest.raises(ApprovalIdentityError):
        _emitir_verificado(root, propuesta_adulterada, consentimiento)
    assert not list(issuances_state_dir(root).iterdir())


def test_presentar_rechaza_un_scope_con_digest_incoherente(tmp_path: pathlib.Path) -> None:
    """El digest se RECOMPUTA al presentar: no se muestra algo que no liga.

    Sin esto, el propietario podría aprobar un digest que corresponde a otros
    campos y el error recién aparecería al emitir, cuando la decisión ya se tomó.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    incoherente = dataclasses.replace(propuesta, scope=dataclasses.replace(propuesta.scope, clone_id="clone-OTRO"))

    with pytest.raises(ApprovalIdentityError):
        _presentar(root, incoherente, registro)
    assert registro.pendientes() == ()


def test_un_consentimiento_con_otro_digest_no_emite(tmp_path: pathlib.Path) -> None:
    """``WHAT_HUMAN_APPROVED != WHAT_WAS_ISSUED`` ⇒ no se emite (§39.3 C2)."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    otro = dataclasses.replace(consentimiento, approval_scope_digest="0" * 64)
    with pytest.raises(ApprovalConsentimientoError):
        _emitir_verificado(root, propuesta, otro)


# ---------------------------------------------------------------------------
# 6. Root extranjero
# ---------------------------------------------------------------------------


def test_un_consentimiento_de_otro_root_no_emite(tmp_path: pathlib.Path) -> None:
    """La aprobación del root A no autoriza una transición en el root B."""
    root_a = _root(tmp_path, "root-a")
    root_b = _root(tmp_path, "root-b")
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root_a)
    presentacion = _presentar(root_a, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    with pytest.raises(ApprovalRootAjenoError):
        _emitir_verificado(root_b, propuesta, consentimiento)
    assert not list(issuances_state_dir(root_b).iterdir())


def test_presentar_en_el_root_equivocado_no_liga(tmp_path: pathlib.Path) -> None:
    """El ``root_id`` se toma del root FÍSICO auditado, no de un campo del scope."""
    root_a = _root(tmp_path, "root-a")
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root_a)
    presentacion = _presentar(root_a, propuesta, registro)
    assert presentacion.root_id == approval_module._root_id(root_a)


# ---------------------------------------------------------------------------
# 7. Consentimiento duplicado / carrera de dos aprobaciones
# ---------------------------------------------------------------------------


def test_el_consentimiento_es_single_use(tmp_path: pathlib.Path) -> None:
    """Responder dos veces la misma solicitud es un duplicado, no una aprobación nueva."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad())

    with pytest.raises(ConsentimientoDuplicadoError):
        registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad())


def test_el_registro_acota_los_ids_resueltos(tmp_path: pathlib.Path) -> None:
    """El registro no crece sin cota, y el desalojo NO abre nada.

    Un id desalojado cae a «desconocida» en vez de «ya respondida»: cambia el
    diagnóstico, no la consecuencia — ninguna de las dos produce consentimiento.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    primera = _presentar(root, propuesta, registro)
    registro.responder(primera.solicitud_id, decision=DecisionConsentimiento.RECHAZAR, identidad=_identidad())

    for _ in range(owner_consent.MAX_SOLICITUDES_RESUELTAS):
        presentacion = _presentar(root, propuesta, registro)
        registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.RECHAZAR, identidad=_identidad())

    assert registro.pendientes() == ()
    with pytest.raises(SolicitudConsentimientoDesconocidaError):
        registro.responder(primera.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad())


def test_una_solicitud_desconocida_no_produce_consentimiento(tmp_path: pathlib.Path) -> None:
    """Sin solicitud abierta no hay consentimiento: no se puede inventar uno."""
    registro = RegistroConsentimientos()
    with pytest.raises(SolicitudConsentimientoDesconocidaError):
        registro.responder(
            "00000000-0000-4000-8000-000000000000",
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(),
        )


def test_una_segunda_aprobacion_no_desplaza_a_la_emitida(tmp_path: pathlib.Path) -> None:
    """``MAX_ISSUED_APPROVALS_PER_ROOT = 1``: una emisión ISSUED bloquea la siguiente."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    primera = _propuesta(root)
    presentacion = _presentar(root, primera, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None
    _emitir_verificado(root, primera, consentimiento)

    with pytest.raises(approval_module.ApprovalMaxIssuedError):
        _propuesta(root)
    assert len(list(issuances_state_dir(root).iterdir())) == 1


# ---------------------------------------------------------------------------
# 8. Crash antes y después del witness / CAS obsoleto
# ---------------------------------------------------------------------------


def test_crash_antes_del_witness_no_deja_consentimiento_durable(tmp_path: pathlib.Path) -> None:
    """Un fallo ANTES del ``CREATE`` no deja autorización: no hay witness."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    def _explota(*args, **kwargs):
        raise OSError("crash simulado antes del CREATE")

    original = approval_module._crear_json_inmutable
    approval_module._crear_json_inmutable = _explota
    try:
        with pytest.raises(OSError):
            _emitir_verificado(root, propuesta, consentimiento)
    finally:
        approval_module._crear_json_inmutable = original

    assert not list(issuances_state_dir(root).iterdir())
    assert cargar_ledger_aprobacion(root).ledger is None


def test_crash_despues_del_witness_reconcilia_hacia_adelante(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Un fallo DESPUÉS del witness NO descarta la emisión: el witness es la autoridad."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    def _explota(handle, ruta, *, esperado, nuevo):
        raise approval_module.ApprovalCasError("CAS roto a propósito")

    monkeypatch.setattr(approval_module, "_escribir_ledger_cas", _explota)
    _emitir_verificado(root, propuesta, consentimiento)
    monkeypatch.undo()

    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.ISSUED
    assert ledger.last_issued_approval_id == propuesta.scope.approval_id
    assert len(list(issuances_state_dir(root).iterdir())) == 1


def test_un_cas_obsoleto_descarta_la_propuesta(tmp_path: pathlib.Path) -> None:
    """Si el ledger cambió desde la propuesta, la propuesta se DESCARTA (ABA)."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None

    obsoleta = dataclasses.replace(propuesta, ledger_revision_at_proposal=99)
    with pytest.raises(approval_module.ApprovalCasError):
        _emitir_verificado(root, obsoleta, consentimiento)
    assert not list(issuances_state_dir(root).iterdir())


# ---------------------------------------------------------------------------
# 9. Revocación
# ---------------------------------------------------------------------------


def test_una_aprobacion_revocada_no_se_consume(tmp_path: pathlib.Path) -> None:
    """Revocar cierra la aprobación: el token no queda reutilizable."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None
    _emitir_verificado(root, propuesta, consentimiento)

    revocar_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        motivo="el propietario cambió de opinión",
        timeout=30.0,
    )
    ledger = cargar_ledger_aprobacion(root).ledger
    assert ledger is not None
    assert ledger.state is ApprovalLifecycle.REVOKED

    transicion = journal_module.PropuestaTransicion(
        transition_id="8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90",
        operation=journal_module.JournalOperation.PROMOTION,
        source_activation=propuesta.scope.source_activation,
        source_activation_digest=propuesta.scope.source_activation_digest,
        target_generation_id=propuesta.scope.generation_id,
        target_clone_id=propuesta.scope.clone_id,
        target_runtime_setup_id=propuesta.scope.runtime_setup_id,
        candidate_id=propuesta.scope.candidate_id,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        evidence_digests=journal_module.EvidenceDigests(
            clone_evidence=propuesta.scope.clone_evidence,
            runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
            compatibility_evidence=propuesta.scope.compatibility_evidence_id,
        ),
        expected_effective_path="E:/rt/versions/x",
        created_at_ns=1_700_000_000_000_000_000,
    )
    with pytest.raises(ApprovalIdentityError, match="REVOKED"):
        approval_module.consumir_aprobacion(
            root,
            approval_id=propuesta.scope.approval_id,
            approval_seq=propuesta.scope.approval_seq,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            transition_id=transicion.transition_id,
            transicion=transicion,
            timeout=30.0,
        )
    assert len(list(approvals_state_dir(root).iterdir())) == 1


# ---------------------------------------------------------------------------
# 10. Cierre de la GUI / desaparición del dueño de la solicitud
# ---------------------------------------------------------------------------


def test_una_solicitud_huerfana_no_se_puede_aprobar(tmp_path: pathlib.Path) -> None:
    """Si el proceso que presentó desaparece, la solicitud desaparece con él.

    Se simula con un registro nuevo (proceso nuevo): el ``solicitud_id`` sigue
    existiendo como string, pero no hay solicitud abierta que responder.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)

    otro_proceso = RegistroConsentimientos()
    with pytest.raises(SolicitudConsentimientoDesconocidaError):
        otro_proceso.responder(
            presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
        )


def test_descartar_todo_invalida_las_solicitudes_abiertas(tmp_path: pathlib.Path) -> None:
    """Cierre ordenado: ninguna solicitud queda aprobable después del shutdown."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)

    registro.descartar_todo()
    with pytest.raises(SolicitudConsentimientoDesconocidaError):
        registro.responder(presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad())
    assert not list(issuances_state_dir(root).iterdir())


# ---------------------------------------------------------------------------
# 11. Compatibilidad de esquemas
# ---------------------------------------------------------------------------


def test_un_witness_v1_no_se_migra_ni_se_acepta(tmp_path: pathlib.Path) -> None:
    """El esquema v1 no tiene ``candidate_id``: se rechaza, no se fabrica el faltante.

    ``UNKNOWN != COMPATIBLE``: inventar el valor ausente produciría una aprobación
    que nadie dio sobre un candidato que nadie eligió.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None
    _emitir_verificado(root, propuesta, consentimiento)

    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    datos = json.loads(ruta.read_text("utf-8"))
    datos["schema_version"] = 1
    datos.pop("scope_payload")
    ruta.write_text(json.dumps(datos), encoding="utf-8")

    with pytest.raises(ApprovalSchemaError):
        approval_module.derivar_autoridad_aprobacion(root)


# ---------------------------------------------------------------------------
# 12. Ausencia de mutación del Effective Runtime
# ---------------------------------------------------------------------------


def test_emitir_no_escribe_fuera_del_root(tmp_path: pathlib.Path) -> None:
    """La emisión toca ``state/`` y nada más: ni el Effective Runtime ni el journal."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    antes = {
        ruta.relative_to(tmp_path).as_posix(): ruta.read_bytes()
        for ruta in tmp_path.rglob("*")
        if ruta.is_file() and ruta.name != "frozen-runtime.lock"
    }
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=_identidad()
    )
    assert consentimiento is not None
    _emitir_verificado(root, propuesta, consentimiento)

    despues = {
        ruta.relative_to(tmp_path).as_posix(): ruta.read_bytes()
        for ruta in tmp_path.rglob("*")
        if ruta.is_file() and ruta.name != "frozen-runtime.lock"
    }
    nuevas = set(despues) - set(antes)
    assert nuevas, "la emisión no escribió nada: el test no está ejercitando el camino"
    for relativa in nuevas:
        assert relativa.startswith("root/state/"), f"escritura fuera de state/: {relativa}"
    for relativa in set(antes) & set(despues):
        if relativa.startswith("root/state/transitions/"):
            assert antes[relativa] == despues[relativa], f"el journal cambió: {relativa}"


# ---------------------------------------------------------------------------
# 13. Activación: la emisión productiva sigue cerrada (FASE F)
# ---------------------------------------------------------------------------


def test_la_emision_productiva_sigue_cerrada(tmp_path: pathlib.Path) -> None:
    """FASE F: sin consumidor seguro, emitir dejaría ``ISSUED`` sin salida operativa.

    Se verifica por CONDUCTA, no por el literal de la constante. Un
    ``assert CONSTANTE is False`` mata al mutante que la flipa sin probar que la
    constante gobierna algo: es el patrón «campo que sólo se escribe es
    decoración», mudado a un flag de capacidad. Por eso acá NO se afirma el
    valor: un consentimiento VÁLIDO y completo llega hasta la puerta de emisión y
    no emite nada. Abrir el interruptor sin integrar el consumidor rompe este
    test por comportamiento.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    consentimiento = registro.responder(
        presentacion.solicitud_id,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad(),
    )
    assert consentimiento is not None

    with pytest.raises(ApprovalHitolNoDisponibleError, match="emisión productiva sigue cerrada"):
        owner_consent.emitir_aprobacion_autorizada(root, propuesta, consentimiento)
    assert not list(issuances_state_dir(root).iterdir())


def test_el_interruptor_de_emision_tiene_una_sola_definicion() -> None:
    """«El interruptor es UNO» es una afirmación verificable, no un comentario.

    Dos constantes homónimas en módulos distintos pueden divergir en silencio:
    una gobierna la conducta y la otra sólo la afirma un test. Esta ancla
    enumera el árbol y exige una única definición; reintroducir la segunda
    rompe el test antes de que las dos se separen.
    """
    patron = re.compile(r"^EMISION_PRODUCTIVA_HABILITADA\s*=", re.MULTILINE)
    definiciones: list[str] = []
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        if patron.search(ruta.read_text(encoding="utf-8")):
            definiciones.append(ruta.relative_to(RAIZ_REPO).as_posix())
    assert definiciones == ["sky_claw/local/frozen_runtime/approval.py"]


def test_ningun_modulo_de_produccion_consume_aprobaciones() -> None:
    """``PROMOTION``/``ROLLBACK`` productivos: censo por AST, no una promesa.

    ``consumir_aprobacion`` es la puerta que quema la aprobación y habilita la
    transición. Mientras no tenga llamador de producción, emitir una aprobación
    sólo puede dejar un token ``ISSUED`` sin salida operativa. El día que alguien
    la cablee, este test lo obliga a adjudicarlo en el mismo cambio (y a revisar
    ``EMISION_PRODUCTIVA_HABILITADA``).
    """
    llamadores: set[str] = set()
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        if "consumir_aprobacion(" in ruta.read_text(encoding="utf-8"):
            llamadores.add(ruta.relative_to(RAIZ_REPO).as_posix())
    assert llamadores == {"sky_claw/local/frozen_runtime/approval.py"}


def test_emitir_aprobacion_sin_binding_de_propietario_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """La entrada productiva no acepta la fixture de tres cadenas."""
    root = _root(tmp_path)
    propuesta = _propuesta(root)
    fixture = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture",
    )
    with pytest.raises(ApprovalHitolNoDisponibleError):
        emitir_aprobacion(root, propuesta, fixture)
    assert not list(issuances_state_dir(root).iterdir())


# ---------------------------------------------------------------------------
# 14. Anclas enumerativas: ninguna superficie equivalente puede aprobar
# ---------------------------------------------------------------------------


def test_las_operaciones_de_aprobacion_y_de_journal_son_iguales() -> None:
    """``ApprovalOperation`` y ``JournalOperation`` no pueden divergir (FASE D).

    Las dos son ``StrEnum`` con los mismos literales, así que ``==`` compara
    strings y **siempre da True** aunque una enumere una operación que la otra no
    conozca. Esa igualdad accidental es la que hay que anclar: se compara el
    conjunto de NOMBRES en las dos direcciones, de modo que agregar ``PROMOTION``
    o ``ROLLBACK`` a una sola de las dos rompa el test en vez de pasar por
    coincidencia de texto.
    """
    nombres_aprobacion = {operacion.name for operacion in ApprovalOperation}
    nombres_journal = {operacion.name for operacion in journal_module.JournalOperation}
    assert nombres_aprobacion == nombres_journal == {"PROMOTION", "ROLLBACK"}
    for nombre in nombres_aprobacion:
        assert ApprovalOperation[nombre].value == journal_module.JournalOperation[nombre].value


def test_solo_el_modulo_de_consentimiento_construye_consentimientos() -> None:
    """Censo por AST: quién construye ``ConsentimientoAprobacion`` en producción.

    Una superficie nueva que fabrique consentimiento rompe este test hasta que se
    le declare su mecanismo en ``SUPERFICIES_DE_CONSENTIMIENTO``. Es la forma de
    la regla «arreglar un hermano y no al otro»: enumerar, no muestrear.
    """
    constructores: set[str] = set()
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        if "ConsentimientoAprobacion(" in ruta.read_text(encoding="utf-8"):
            constructores.add(ruta.relative_to(RAIZ_REPO).as_posix())
    assert constructores == {"sky_claw/local/frozen_runtime/owner_consent.py"}


def test_solo_el_modulo_de_consentimiento_construye_identidades() -> None:
    """El hermano del censo anterior: quién construye ``IdentidadPropietario``.

    ``IdentidadPropietario`` es un dataclass público con campos que pone el
    llamador. ``_exigir_identidad_verificada`` comprueba tipo, superficie,
    mecanismo y campos no vacíos, pero **no** que lo haya creado
    ``verificar_identidad_telegram``. Un módulo de producción que lo construya
    por su cuenta puede saltarse la verificación del transporte; este censo lo
    detecta. No hace el tipo opaco ni prueba procedencia en runtime: congela la
    familia de constructores, igual que el censo de consentimientos.
    """
    constructores: set[str] = set()
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        if "IdentidadPropietario(" in ruta.read_text(encoding="utf-8"):
            constructores.add(ruta.relative_to(RAIZ_REPO).as_posix())
    assert constructores == {"sky_claw/local/frozen_runtime/owner_consent.py"}


def test_ninguna_superficie_del_agente_puede_emitir_aprobaciones() -> None:
    """El agente LLM y sus tools no pueden alcanzar la autoridad ni el consentimiento.

    ``SELF_ISSUED_APPROVAL_IS_AUTHORIZATION = NO``: si una tool importara
    ``owner_consent``, el modelo podría presentarse su propia solicitud y
    aprobarla en el mismo turno.
    """
    # La autoridad se alcanza por MÓDULO y por los SÍMBOLOS que el paquete
    # reexporta. Se resuelve por AST, no por substring: `from
    # sky_claw.local.frozen_runtime import approval` y `import
    # sky_claw.local.frozen_runtime.approval` se le escapaban al escaneo
    # anterior, y son la forma natural de importarlo.
    prohibidos = {
        "sky_claw.local.frozen_runtime.approval",
        "sky_claw.local.frozen_runtime.owner_consent",
        "sky_claw.local.frozen_runtime.ConsentimientoAprobacion",
        "sky_claw.local.frozen_runtime.PropuestaAprobacion",
        "sky_claw.local.frozen_runtime.consumir_aprobacion",
        "sky_claw.local.frozen_runtime.emitir_aprobacion",
        "sky_claw.local.frozen_runtime.RegistroConsentimientos",
        "sky_claw.local.frozen_runtime.emitir_aprobacion_autorizada",
        "sky_claw.local.frozen_runtime.verificar_identidad_telegram",
    }
    superficies = [
        RAIZ_REPO / "sky_claw" / "app" / "agent",
        RAIZ_REPO / "sky_claw" / "local",
    ]
    infractores: list[str] = []
    for base in superficies:
        for ruta in _modulos_bajo(base):
            posix = ruta.as_posix()
            if "/frozen_runtime/" in posix or posix.endswith("__init__.py"):
                continue
            for modulo in _modulos_importados(ruta):
                if modulo in prohibidos:
                    infractores.append(f"{ruta.relative_to(RAIZ_REPO).as_posix()} -> {modulo}")
    assert infractores == [], f"superficies del agente alcanzan la autoridad de aprobación: {infractores}"


def test_el_censo_de_superficies_es_explicito() -> None:
    """Igualdad literal del censo: agregar una superficie rompe el ancla.

    Mismo patrón que ``RITUAL_TOOL_MAP``: el diccionario no se «actualiza solo».
    """
    censo = {
        superficie.value: mecanismo.verifica_identidad
        for superficie, mecanismo in SUPERFICIES_DE_CONSENTIMIENTO.items()
    }
    assert censo == {"telegram": True, "gui": False}


def test_solo_el_nucleo_bajo_lock_escribe_el_witness() -> None:
    """Censo por AST: quién llama a ``_emitir_aprobacion_bajo_lock``."""
    llamadores: set[str] = set()
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        if "_emitir_aprobacion_bajo_lock(" in ruta.read_text(encoding="utf-8"):
            llamadores.add(ruta.relative_to(RAIZ_REPO).as_posix())
    assert llamadores == {
        "sky_claw/local/frozen_runtime/approval.py",
        "sky_claw/local/frozen_runtime/owner_consent.py",
    }
