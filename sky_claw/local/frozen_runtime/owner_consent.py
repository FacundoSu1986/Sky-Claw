"""Consentimiento HITL verificado del propietario (P4-S6; ADR 0012 §46).

Cierra ``OWNER_IDENTITY_BINDING = DEFERRED`` (§35.2). Hasta P4-S5 la emisión
productiva estaba cerrada porque no existía superficie HITL: el «consentimiento»
era un objeto de tres cadenas que cualquier módulo podía construir, así que
llamarlo consentimiento humano era una afirmación sin prueba.

**Qué agrega este módulo, exactamente.** Un protocolo de dos fases donde el
``solicitud_id`` y el digest los acuña **este** módulo, no el llamador:

```text
FASE B — PRESENTACIÓN (sin lock; §39.4)
  B1  verificar la IDENTIDAD del propietario contra el mecanismo declarado
      para su superficie (censo ``SUPERFICIES_DE_CONSENTIMIENTO``)
  B2  revalidar el scope propuesto (``validar_scope`` recomputa el digest)
  B3  acuñar ``solicitud_id`` y registrar la solicitud ABIERTA en memoria
  B4  devolver la presentación con los 14 campos y su digest

FASE B' — DECISIÓN
  responder(solicitud_id, decision, identidad):
      la identidad tiene que ser la MISMA (superficie, sujeto, sesión)
      APROBAR  ⇒ ConsentimientoAprobacion con el digest PRESENTADO
      RECHAZAR ⇒ None
      CANCELAR ⇒ None
      la solicitud queda RESUELTA: no se reabre (single-use)

FASE C — EMISIÓN (bajo el lock; la toma ``emitir_aprobacion_autorizada``)
  el consentimiento verificado entra al núcleo durable existente
  (``_emitir_aprobacion_bajo_lock``) sin inventar un segundo ledger ni una
  segunda autoridad
```

**Qué NO agrega.** No hay TTL, no hay firma criptográfica, no hay un segundo store
durable y **no hay cableado**: ningún módulo de producción llama a ``presentar`` ni
a ``responder`` (``HITL_PRODUCTIVE = NOT_WIRED``), porque cablear la superficie sin
un consumidor de la aprobación dejaría autorizaciones ``ISSUED`` sin salida
operativa. La solicitud vive en memoria a propósito: si el proceso muere, la
solicitud muere con él y no queda nada aprobable — el fail-closed es más barato
que un registro durable de propuestas, que sería una segunda autoridad.

**Modelo de amenaza (declarado, no insinuado).**

```text
IN_PROCESS_CODE_IS_TRUSTED          = YES   (código arbitrario en el proceso ya puede
                                             escribir el witness directamente)
AGENT_LLM_OUTPUT_IS_TRUSTED         = NO    (anclado por test enumerativo: ninguna
                                             superficie del agente importa este módulo)
TRANSPORT_IS_TRUSTED                = TELEGRAM (el ``from.id`` lo pone Telegram, no el
                                             cliente; el webhook se autentica con
                                             ``secret_token``)
GUI_BUTTON_IS_IDENTITY              = NO    (no hay login: presencia local ≠ identidad)
IDENTITY_IS_A_CAPABILITY            = YES   (constructor cerrado; sello acuñado por el
                                             verificador y comprobado por identidad)
PRESENTATION_IS_NON_AMBIGUOUS       = YES   (ningún valor del scope aporta una línea)
HITL_PRODUCTIVE                     = NOT_WIRED (ningún módulo de producción presenta
                                             ni responde: la superficie existe, el
                                             cableado no)
OWNER_SUBJECT_IS_DURABLE            = NO    (el witness durable registra la superficie
                                             como campo del scope; el ``solicitud_id``
                                             viaja dentro de ``approval_provenance``, que
                                             es un string OPACO del llamador — nada
                                             verifica que lo contenga. El registro
                                             autoritativo de solicitudes es en memoria;
                                             el sujeto se reconstruye del log del
                                             transporte)
DECISION_INSTANT_IS_DURABLE         = NO    (``decided_at_ns`` se VERIFICA al emitir
                                             —``>= proposed_at_ns``—, pero no se
                                             persiste; el ancla durable del instante es
                                             el log del transporte)
```

Las dos últimas líneas son limitaciones **elegidas**, no olvidos: agregar el sujeto
o el instante al witness exigiría un esquema v3 y no aporta frente al atacante que
importa —quien fabrique el campo fabrica también el sujeto y el instante—. Frente
al atacante externo la prueba está en el transporte, que sí la registra.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import pathlib
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from enum import StrEnum
from types import MappingProxyType

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalScope,
    ConsentimientoAprobacion,
    PropuestaAprobacion,
    payload_scope_sin_digest,
    validar_scope,
)
from sky_claw.local.frozen_runtime.errors import (
    ApprovalHitolNoDisponibleError,
    ApprovalSchemaError,
    ConsentimientoDuplicadoError,
    IdentidadPropietarioNoVerificadaError,
    SesionConsentimientoNoCoincideError,
    SolicitudConsentimientoDesconocidaError,
    SuperficieSinIdentidadVerificadaError,
)
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key, participant_lock

logger = logging.getLogger(__name__)

#: FASE F del brief P4-S6. ``PROMOTION`` y ``ROLLBACK`` productivos siguen
#: apagados: no hay consumidor seguro de una aprobación (ningún módulo de
#: producción llama a ``consumir_aprobacion``), así que emitir dejaría
#: autorizaciones ``ISSUED`` sin salida operativa y —con
#: ``MAX_ISSUED_APPROVALS_PER_ROOT = 1``— bloquearían la emisión siguiente hasta
#: que el dueño revoque.
#:
#: El interruptor NO se declara acá: es ``approval.EMISION_PRODUCTIVA_HABILITADA``
#: y este módulo lo LEE. Una segunda constante homónima gobernaría nada y podría
#: divergir de la que sí gobierna; un test enumera el árbol y exige una sola.

#: Nombre del mecanismo de identidad del propietario en Telegram. Es el valor que
#: ``verificar_identidad_telegram`` estampa en la identidad y el que el censo
#: declara; un llamador no puede inventar otro (cae en
#: ``IdentidadPropietarioNoVerificadaError``).
MECANISMO_TELEGRAM = "telegram_from_id"

#: Mecanismo de la GUI. Se declara explícitamente que NO verifica identidad: el
#: valor no es ``None`` para que el censo sea exhaustivo y la ausencia de prueba
#: quede escrita, en vez de inferida por omisión.
MECANISMO_PRESENCIA_LOCAL = "presencia_local"

#: Tope de ids resueltos que el registro recuerda para distinguir «ya respondida»
#: de «no existe». Acotado a propósito: el registro vive lo que vive el proceso y
#: un tope evita crecimiento sin cota. Pasado el tope, la solicitud vieja cae a
#: «desconocida» — las dos respuestas son fail-closed.
MAX_SOLICITUDES_RESUELTAS = 64


class SuperficieConsentimiento(StrEnum):
    """Superficies que pueden ORIGINAR una solicitud de aprobación.

    El censo (``SUPERFICIES_DE_CONSENTIMIENTO``) es la autoridad: una superficie
    que no esté ahí no puede presentar ni responder. Agregar una rompe el test
    enumerativo hasta que se le declare su mecanismo.
    """

    TELEGRAM = "telegram"
    GUI = "gui"


@dataclasses.dataclass(frozen=True, slots=True)
class MecanismoIdentidad:
    """Cómo se prueba quién es el propietario en una superficie.

    ``verifica_identidad = False`` no es un descuido: es la declaración de que la
    superficie **no puede** producir consentimiento, y por eso ``presentar`` la
    rechaza con ``SuperficieSinIdentidadVerificadaError``.
    """

    nombre: str
    verifica_identidad: bool
    prueba: str
    justificacion: str


SUPERFICIES_DE_CONSENTIMIENTO: Mapping[SuperficieConsentimiento, MecanismoIdentidad] = MappingProxyType(
    {
        SuperficieConsentimiento.TELEGRAM: MecanismoIdentidad(
            nombre=MECANISMO_TELEGRAM,
            verifica_identidad=True,
            prueba="update['from']['id'] == operador configurado, chat privado, no reenviado",
            justificacion=(
                "Telegram entrega el update autenticado con el bot token y un secret_token de webhook; "
                "``from.id`` lo fija el servidor de Telegram, no el cliente. Compararlo contra el operador "
                "configurado es la única prueba de identidad humana que el producto ya emite."
            ),
        ),
        SuperficieConsentimiento.GUI: MecanismoIdentidad(
            nombre=MECANISMO_PRESENCIA_LOCAL,
            verifica_identidad=False,
            prueba="presencia local (sin login ni credencial)",
            justificacion=(
                "La GUI no autentica a nadie: cualquiera con la sesión de escritorio puede pulsar el botón. "
                "Aceptarla como consentimiento sería reemplazar la prueba de identidad por un clic, que es "
                "exactamente lo que §35.2 prohíbe. Queda como blocker declarado, no como equivalencia."
            ),
        ),
    }
)


class _SelloDeVerificacion:
    """Capacidad OPACA: poseer este objeto ES la prueba de verificación.

    No se exporta ni se reexporta. Lo que cierra no es la posibilidad teórica de
    fabricarlo —código en proceso alcanza cualquier nombre privado, y el modelo de
    amenaza ya declara ``IN_PROCESS_CODE_IS_TRUSTED = YES``—, sino la vía
    ACCIDENTAL y la documentada: hasta este cambio ``IdentidadPropietario`` era un
    dataclass público de cinco campos que ponía el llamador, así que «identidad
    verificada» era una afirmación sobre strings del propio llamador. Es la misma
    tautología que P4-S5 cerró para el journal (``digest_canonico`` recalculado) y
    P4-S4 para ``root_id`` (verificado al leer): **un campo que sólo escribe el
    llamador no prueba nada**.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # no revelar nada útil a quien lo imprima
        return "<sello-de-verificacion>"


#: Instancia única. Se compara por IDENTIDAD (``is``), no por valor: una copia no
#: es el sello.
_SELLO_VERIFICADO = _SelloDeVerificacion()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class IdentidadPropietario:
    """Quién responde, probado por un mecanismo del censo.

    ``sujeto`` es el identificador del propietario **en su superficie** (para
    Telegram, el ``user id``); ``sesion`` es la conversación que originó la
    solicitud. Los dos se comparan al responder: ligar sólo el sujeto dejaría
    pasar el replay entre dos sesiones del mismo propietario.

    ``__init__`` está CERRADO a propósito. La única forma de obtener una instancia
    es :func:`verificar_identidad_telegram`, que la acuña desde el update del
    transporte con :func:`_acuñar_identidad` y le estampa el sello.
    ``IdentidadPropietario(superficie=..., sujeto=..., mecanismo="telegram_from_id")``
    —que antes era un one-liner que cualquier módulo podía escribir y que
    ``_exigir_identidad_verificada`` aceptaba— ya no produce un objeto utilizable.
    """

    superficie: SuperficieConsentimiento
    sujeto: str
    sesion: str
    mecanismo: str
    verificado_en_ns: int
    #: Procedencia. ``compare=False``/``repr=False``: no es un dato del propietario
    #: sino la prueba de que hubo verificación, y no debe filtrarse al imprimir.
    _sello: _SelloDeVerificacion = dataclasses.field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise IdentidadPropietarioNoVerificadaError(
            "IdentidadPropietario no se construye a mano: la acuña verificar_identidad_telegram a partir del "
            "update del transporte. Una identidad cuyos campos los pone el llamador no prueba quién respondió "
            "— es exactamente el defecto que P4-S6 cierra (§35.2)."
        )


def _acuñar_identidad(
    *,
    superficie: SuperficieConsentimiento,
    sujeto: str,
    sesion: str,
    mecanismo: str,
    verificado_en_ns: int,
) -> IdentidadPropietario:
    """Único productor de instancias. Privado: lo llama un VERIFICADOR.

    En producción el único llamador es :func:`verificar_identidad_telegram` y un
    test enumerativo por AST lo congela. Los tests adversariales lo usan para
    fabricar identidades que el verificador **no puede** producir —una superficie
    GUI, un mecanismo inventado— y comprobar que se rechazan: sin esa vía no
    habría forma de ejercitar los rechazos sin abrir la puerta que se cierra.
    """
    instancia = object.__new__(IdentidadPropietario)
    object.__setattr__(instancia, "superficie", superficie)
    object.__setattr__(instancia, "sujeto", sujeto)
    object.__setattr__(instancia, "sesion", sesion)
    object.__setattr__(instancia, "mecanismo", mecanismo)
    object.__setattr__(instancia, "verificado_en_ns", verificado_en_ns)
    object.__setattr__(instancia, "_sello", _SELLO_VERIFICADO)
    return instancia


@dataclasses.dataclass(frozen=True, slots=True)
class PresentacionConsentimiento:
    """Solicitud ABIERTA: lo que el propietario tiene delante para decidir.

    ``texto`` es la presentación completa —los 14 campos del ``ApprovalScope`` y
    su digest—, no un resumen: el propietario aprueba el digest, y un resumen
    dejaría fuera precisamente los campos que el digest compromete.
    """

    solicitud_id: str
    root_id: str
    superficie: SuperficieConsentimiento
    sesion: str
    propietario: str
    mecanismo: str
    approval_id: str
    approval_seq: int
    approval_scope_digest: str
    approval_provenance: str
    scope_payload: Mapping[str, object]
    ledger_revision_at_proposal: int
    presentada_en_ns: int
    texto: str


class DecisionConsentimiento(StrEnum):
    """Decisión EXPLÍCITA del propietario. No hay un cuarto valor ni un default."""

    APROBAR = "aprobar"
    RECHAZAR = "rechazar"
    CANCELAR = "cancelar"


#: Separadores de línea que ``json.dumps`` NO escapa con ``ensure_ascii=False``
#: pero que un renderizador (consola, HTML, cliente de Telegram) puede tratar como
#: salto de línea. Se escapan a mano: la garantía que se busca es «un valor no
#: puede aportar una línea», y dejarlos fuera la rompería en el último tramo.
_SEPARADORES_DE_LINEA = {"\u0085": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}


def render_valor_scope(valor: object) -> str:
    """Render canónico y NO AMBIGUO de un valor para la presentación.

    Todo valor sale por ``json.dumps``: las cadenas quedan entrecomilladas y con
    los caracteres de control escapados, los contenedores con claves ordenadas
    —el mismo orden que usa el digest— y los escalares con su forma JSON. La
    consecuencia que importa es estructural: **ningún valor puede aportar un
    salto de línea**, así que el número de líneas de la presentación no depende
    del contenido y un scope no puede dibujar la fila que quiera.

    Hasta este cambio las cadenas se devolvían con ``str()``, y
    ``approval_provenance`` es un string OPACO del llamador: bastaba
    ``"x\\napproval_scope_digest: <otro>"`` para que el propietario viera el
    digest dos veces, una de ellas falsa. El digest aprobado no cambiaba —lo que
    se emite sigue siendo lo que se presentó—, pero la superficie MENTÍA sobre lo
    aprobado, que es justo lo que ``WHAT_HUMAN_APPROVED == WHAT_WAS_ISSUED``
    prohíbe. Un campo del llamador no puede gobernar la forma de la presentación.
    """
    texto = json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)
    for separador, escapado in _SEPARADORES_DE_LINEA.items():
        texto = texto.replace(separador, escapado)
    return texto


def _texto_de_presentacion(presentacion: PresentacionConsentimiento) -> str:
    """Presentación legible: TODOS los campos, con nombre y valor.

    Cada valor pasa por :func:`render_valor_scope`, incluidos los de cabecera
    (``solicitud_id``, ``root_id``, ``sesion``, ``propietario``): la etiqueta la
    pone este módulo y sólo este módulo. Renderizar unos campos y otros no sería
    dejar la puerta abierta en la mitad de las filas.
    """
    lineas = [
        "APROBACIÓN DE TRANSICIÓN — FROZEN RUNTIME",
        f"solicitud_id: {render_valor_scope(presentacion.solicitud_id)}",
        f"root_id: {render_valor_scope(presentacion.root_id)}",
        f"superficie: {render_valor_scope(presentacion.superficie.value)}",
        f"sesion: {render_valor_scope(presentacion.sesion)}",
        f"propietario: {render_valor_scope(presentacion.propietario)}",
        f"mecanismo_identidad: {render_valor_scope(presentacion.mecanismo)}",
    ]
    for campo in dataclasses.fields(ApprovalScope):
        if campo.name == "approval_scope_digest":
            continue
        lineas.append(f"{campo.name}: {render_valor_scope(presentacion.scope_payload.get(campo.name))}")
    lineas.append(f"approval_scope_digest: {render_valor_scope(presentacion.approval_scope_digest)}")
    lineas.append(
        f"Responde APROBAR, RECHAZAR o CANCELAR citando la solicitud {render_valor_scope(presentacion.solicitud_id)}. "
        "Apruebas el digest, no una intención."
    )
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Verificación de identidad — el mecanismo real del transporte
# ---------------------------------------------------------------------------


def _entero_de_identidad(valor: object) -> int | None:
    """``bool`` es subclase de ``int``: ``True`` no es un id de usuario."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor


def verificar_identidad_telegram(
    update: Mapping[str, object],
    *,
    operador_configurado: int | None,
    clock_ns: Callable[[], int] = time.time_ns,
) -> IdentidadPropietario:
    """Prueba la identidad del propietario a partir del update CRUDO de Telegram.

    Se verifica sobre el payload del transporte y no sobre un campo de la
    aplicación: si el llamador pudiera declarar «soy el operador», no habría
    prueba. Las cuatro condiciones son las del guard que el producto ya usa para
    las tools mutantes (``TelegramWebhook._validate_private_operator``), y hay un
    test diferencial que impide que las dos implementaciones se separen:

    1. ``from.id`` es un entero igual al operador configurado;
    2. el mensaje no es reenviado (``forward_date``/``forward_from``), ni lo es
       el mensaje al que responde;
    3. el chat es privado (``chat.id == from.id``), y si el update declara
       ``type``, es ``private``;
    4. hay operador configurado: sin él, nadie está autorizado (fail-closed).

    Acepta **las tres formas** en que llega una decisión real:

    ```text
    update crudo con ``message``          remitente/chat en ``message``
    update crudo con ``callback_query``   remitente en ``callback_query.from``,
                                          chat en ``callback_query.message.chat``
    mensaje YA desenvuelto                remitente/chat en la raíz (es la forma
                                          que el dispatcher le pasa al guard)
    ```

    La forma del envoltorio no puede cambiar el veredicto: hay un test que
    compara la identidad producida por el update crudo y por su mensaje
    desenvuelto, y el ancla diferencial contra ``_validate_private_operator``
    cubre las tres.
    """
    if not isinstance(update, Mapping):
        raise IdentidadPropietarioNoVerificadaError("el update de Telegram no es un objeto")
    if operador_configurado is None:
        raise IdentidadPropietarioNoVerificadaError(
            "no hay operador configurado: sin identidad de referencia nadie está autorizado (fail-closed)"
        )

    consulta = update.get("callback_query")
    if isinstance(consulta, Mapping):
        carga: Mapping[str, object] = consulta
    else:
        # El update CRUDO envuelve el mensaje en ``message``; el dispatcher, en
        # cambio, entrega al guard del transporte el mensaje YA desenvuelto. Las
        # dos formas son el mismo objeto lógico y tienen que verificar igual: leer
        # sólo la plana rechazaba TODOS los mensajes de texto reales
        # (``/aprobar <solicitud>``) —fail-closed, pero con la superficie
        # inservible—, que es el mismo error que ya se había corregido para los
        # botones.
        crudo = update.get("message")
        carga = crudo if isinstance(crudo, Mapping) else update

    remitente = carga.get("from")
    if not isinstance(remitente, Mapping):
        raise IdentidadPropietarioNoVerificadaError("el update no trae remitente")
    sujeto = _entero_de_identidad(remitente.get("id"))
    if sujeto is None:
        raise IdentidadPropietarioNoVerificadaError("el remitente no trae un 'id' entero")
    if sujeto != operador_configurado:
        raise IdentidadPropietarioNoVerificadaError(
            f"el remitente {sujeto} no es el operador configurado: no hay consentimiento del propietario"
        )

    mensaje = carga.get("message")
    mensaje = mensaje if isinstance(mensaje, Mapping) else {}
    for candidato in (update, carga, mensaje):
        if candidato.get("forward_date") is not None or candidato.get("forward_from") is not None:
            raise IdentidadPropietarioNoVerificadaError("el mensaje es reenviado: un reenvío no prueba quién respondió")
    respuesta = update.get("reply_to_message") or carga.get("reply_to_message") or mensaje.get("reply_to_message")
    if isinstance(respuesta, Mapping) and (
        respuesta.get("forward_date") is not None or respuesta.get("forward_from") is not None
    ):
        raise IdentidadPropietarioNoVerificadaError("la respuesta cita un mensaje reenviado")

    chat = update.get("chat")
    if not isinstance(chat, Mapping):
        chat = carga.get("chat")
    if not isinstance(chat, Mapping):
        chat = mensaje.get("chat")
    if not isinstance(chat, Mapping):
        raise IdentidadPropietarioNoVerificadaError("el update no trae chat")
    chat_id = _entero_de_identidad(chat.get("id"))
    if chat_id is None:
        raise IdentidadPropietarioNoVerificadaError("el chat no trae un 'id' entero")
    tipo = chat.get("type")
    if tipo is not None and tipo != "private":
        raise IdentidadPropietarioNoVerificadaError(f"el chat es de tipo {tipo!r}: la aprobación es privada")
    if chat_id != sujeto:
        raise IdentidadPropietarioNoVerificadaError(
            "el chat no es privado del operador (chat.id != from.id): un grupo no identifica a una persona"
        )

    return _acuñar_identidad(
        superficie=SuperficieConsentimiento.TELEGRAM,
        sujeto=str(sujeto),
        sesion=str(chat_id),
        mecanismo=MECANISMO_TELEGRAM,
        verificado_en_ns=int(clock_ns()),
    )


def _exigir_identidad_verificada(identidad: IdentidadPropietario) -> MecanismoIdentidad:
    """La identidad tiene que venir de un mecanismo del censo que verifique.

    El orden importa. El **sello** se comprueba primero: es la procedencia, y sin
    procedencia los campos de abajo no dicen nada — comprobar el mecanismo antes
    sería preguntarle al llamador si él mismo es de fiar.
    """
    if not isinstance(identidad, IdentidadPropietario):
        raise IdentidadPropietarioNoVerificadaError("se esperaba una IdentidadPropietario")
    if getattr(identidad, "_sello", None) is not _SELLO_VERIFICADO:
        raise IdentidadPropietarioNoVerificadaError(
            "la identidad no fue acuñada por un verificador del transporte: los campos por sí solos no prueban "
            "quién respondió. Se obtiene con verificar_identidad_telegram, no construyéndola."
        )
    if not isinstance(identidad.superficie, SuperficieConsentimiento):
        raise IdentidadPropietarioNoVerificadaError(f"superficie desconocida: {identidad.superficie!r}")
    mecanismo = SUPERFICIES_DE_CONSENTIMIENTO.get(identidad.superficie)
    if mecanismo is None:
        raise IdentidadPropietarioNoVerificadaError(
            f"la superficie {identidad.superficie.value!r} no está en el censo de consentimiento"
        )
    if identidad.mecanismo != mecanismo.nombre:
        raise IdentidadPropietarioNoVerificadaError(
            f"el mecanismo {identidad.mecanismo!r} no es el del censo para {identidad.superficie.value!r} "
            f"({mecanismo.nombre!r}): un mecanismo declarado por el llamador no prueba nada"
        )
    if not mecanismo.verifica_identidad:
        raise SuperficieSinIdentidadVerificadaError(
            f"la superficie {identidad.superficie.value!r} no verifica identidad ({mecanismo.prueba}): "
            "no puede producir consentimiento del propietario"
        )
    for campo, valor in (("sujeto", identidad.sujeto), ("sesion", identidad.sesion)):
        if not isinstance(valor, str) or not valor.strip():
            raise IdentidadPropietarioNoVerificadaError(f"la identidad no trae {campo}")
    return mecanismo


# ---------------------------------------------------------------------------
# Registro de solicitudes ABIERTAS (en memoria, por proceso)
# ---------------------------------------------------------------------------


class RegistroConsentimientos:
    """Solicitudes abiertas de este proceso. No persiste nada, a propósito.

    Un registro durable de propuestas sería una segunda autoridad —el defecto que
    el ADR prohíbe—, y además dejaría solicitudes aprobables después de un
    reinicio. En memoria, el cierre del proceso extingue toda propuesta
    (``SESSION_CLOSE_INVALIDATES_PENDING = YES``).
    """

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        solicitud_id_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        self._clock_ns = clock_ns
        self._solicitud_id_factory = solicitud_id_factory
        self._abiertas: dict[str, PresentacionConsentimiento] = {}
        #: Ids ya resueltos, en orden de llegada. Se conservan ACOTADOS para poder
        #: distinguir «ya respondida» (``ConsentimientoDuplicadoError``) de «no
        #: existe» (``SolicitudConsentimientoDesconocidaError``) sin que el
        #: registro crezca sin cota en un proceso largo. Pasado el tope, la
        #: solicitud vieja cae a «desconocida»: las dos respuestas son fail-closed
        #: (ninguna produce consentimiento), así que el desalojo no abre nada.
        self._resueltas: collections.deque[str] = collections.deque(maxlen=MAX_SOLICITUDES_RESUELTAS)
        #: El registro se toca desde el hilo de la GUI y desde el del webhook de
        #: Telegram. El lock cubre SÓLO el estado del registro: nunca se sostiene
        #: durante la interacción humana ni se anida con el root lock.
        self._mutex = threading.Lock()

    def _resolver(self, solicitud_id: str) -> PresentacionConsentimiento:
        """Devuelve la solicitud abierta o falla cerrado. Debe llamarse con el mutex."""
        presentacion = self._abiertas.get(solicitud_id)
        if presentacion is not None:
            return presentacion
        if solicitud_id in self._resueltas:
            raise ConsentimientoDuplicadoError(
                f"la solicitud {solicitud_id} ya fue respondida: una decisión terminal no se reabre"
            )
        raise SolicitudConsentimientoDesconocidaError(
            f"no hay ninguna solicitud abierta con id {solicitud_id!r}: no se inventa un consentimiento"
        )

    def _marcar_resuelta(self, solicitud_id: str) -> None:
        """Retira la solicitud y la recuerda como resuelta. Debe llamarse con el mutex."""
        self._abiertas.pop(solicitud_id, None)
        self._resueltas.append(solicitud_id)

    # -- FASE B ------------------------------------------------------------

    def presentar(
        self,
        root: pathlib.Path,
        propuesta: PropuestaAprobacion,
        *,
        identidad: IdentidadPropietario,
    ) -> PresentacionConsentimiento:
        """Abre una solicitud y devuelve lo que el propietario debe ver.

        No toma el root lock (§39.4) y no escribe nada: la propuesta sigue siendo
        un valor en memoria sin autoridad. El digest se **recomputa** desde los 14
        campos, así que un scope adulterado no llega a presentarse.
        """
        if not isinstance(propuesta, PropuestaAprobacion):
            raise ApprovalSchemaError("presentar: se esperaba una PropuestaAprobacion")
        _exigir_identidad_verificada(identidad)
        scope = validar_scope(propuesta.scope)
        root_id = canonical_root_key(pathlib.Path(root))[0]

        solicitud_id = str(self._solicitud_id_factory())
        presentacion = PresentacionConsentimiento(
            solicitud_id=solicitud_id,
            root_id=root_id,
            superficie=identidad.superficie,
            sesion=identidad.sesion,
            propietario=identidad.sujeto,
            mecanismo=identidad.mecanismo,
            approval_id=scope.approval_id,
            approval_seq=scope.approval_seq,
            approval_scope_digest=scope.approval_scope_digest,
            approval_provenance=scope.approval_provenance,
            scope_payload=payload_scope_sin_digest(scope),
            ledger_revision_at_proposal=propuesta.ledger_revision_at_proposal,
            presentada_en_ns=int(self._clock_ns()),
            texto="",
        )
        presentacion = dataclasses.replace(presentacion, texto=_texto_de_presentacion(presentacion))
        with self._mutex:
            self._abiertas[solicitud_id] = presentacion
        logger.info(
            "consentimiento: solicitud %s presentada por %s/%s para la aprobación %s (seq %s)",
            solicitud_id,
            identidad.superficie.value,
            identidad.sujeto,
            scope.approval_id,
            scope.approval_seq,
        )
        return presentacion

    # -- FASE B' -----------------------------------------------------------

    def responder(
        self,
        solicitud_id: str,
        *,
        decision: DecisionConsentimiento,
        identidad: IdentidadPropietario,
    ) -> ConsentimientoAprobacion | None:
        """Registra la decisión y, sólo si es ``APROBAR``, produce el consentimiento.

        Este es el ÚNICO lugar del paquete que construye un
        ``ConsentimientoAprobacion`` (hay un test enumerativo que lo congela): el
        digest que viaja al núcleo durable es el que se presentó, no uno que el
        llamador elija.
        """
        if not isinstance(decision, DecisionConsentimiento):
            raise IdentidadPropietarioNoVerificadaError(f"decisión desconocida: {decision!r}")
        _exigir_identidad_verificada(identidad)

        with self._mutex:
            presentacion = self._resolver(solicitud_id)
            if identidad.superficie is not presentacion.superficie:
                raise SesionConsentimientoNoCoincideError(
                    f"la solicitud {solicitud_id} se presentó en {presentacion.superficie.value!r} y la respuesta "
                    f"llega desde {identidad.superficie.value!r}: otra superficie es otra sesión"
                )
            if identidad.sujeto != presentacion.propietario or identidad.sesion != presentacion.sesion:
                raise SesionConsentimientoNoCoincideError(
                    f"la solicitud {solicitud_id} se presentó a {presentacion.propietario} en la sesión "
                    f"{presentacion.sesion!r}; la respuesta llega de {identidad.sujeto} en {identidad.sesion!r}"
                )
            # Resolver es terminal: se hace bajo el mutex para que dos respuestas
            # concurrentes no puedan pasar las dos la comprobación de estado.
            self._marcar_resuelta(solicitud_id)

        if decision is not DecisionConsentimiento.APROBAR:
            logger.info(
                "consentimiento: %s %s la solicitud %s — no se emite nada",
                identidad.sujeto,
                decision.value,
                solicitud_id,
            )
            return None

        return ConsentimientoAprobacion(
            approval_scope_digest=presentacion.approval_scope_digest,
            approval_provenance=presentacion.approval_provenance,
            surface=presentacion.superficie.value,
            root_id=presentacion.root_id,
            solicitud_id=presentacion.solicitud_id,
            session_id=presentacion.sesion,
            owner_subject=presentacion.propietario,
            mecanismo=presentacion.mecanismo,
            decided_at_ns=int(self._clock_ns()),
        )

    # -- Cancelación / cierre ---------------------------------------------

    def cancelar(self, solicitud_id: str, *, motivo: str) -> None:
        """Cancela una solicitud abierta por decisión del sistema (shutdown, error).

        No produce consentimiento. Una solicitud ya resuelta no se toca: la
        decisión del propietario no la revierte un timeout tardío.
        """
        with self._mutex:
            if solicitud_id not in self._abiertas:
                if solicitud_id in self._resueltas:
                    return
                raise SolicitudConsentimientoDesconocidaError(
                    f"no hay ninguna solicitud abierta con id {solicitud_id!r}"
                )
            self._marcar_resuelta(solicitud_id)
        logger.info("consentimiento: solicitud %s cancelada por el sistema (%s)", solicitud_id, motivo)

    def descartar_todo(self) -> None:
        """Cierre ordenado: ninguna solicitud sobrevive al proceso que la presentó."""
        with self._mutex:
            self._abiertas.clear()

    def pendientes(self) -> tuple[PresentacionConsentimiento, ...]:
        """Solicitudes todavía abiertas (para la UI de la superficie)."""
        with self._mutex:
            return tuple(self._abiertas.values())


# ---------------------------------------------------------------------------
# FASE C — la conexión con el núcleo durable
# ---------------------------------------------------------------------------


def _exigir_binding_completo(consentimiento: ConsentimientoAprobacion) -> None:
    """Un consentimiento sin binding de propietario no autoriza nada.

    Los seis campos, no cinco: ``decided_at_ns`` entra en la lista porque el
    núcleo durable lo VUELVE A LEER (``approval._validar_consentimiento``) para
    ordenar la decisión respecto de la propuesta. Un campo que se exige en la
    puerta y no se verifica después sería el mismo ornamento que se está
    cerrando.
    """
    faltantes = [
        nombre
        for nombre in ("root_id", "solicitud_id", "session_id", "owner_subject", "mecanismo", "decided_at_ns")
        if getattr(consentimiento, nombre, None) is None
    ]
    if faltantes:
        raise ApprovalHitolNoDisponibleError(
            "el consentimiento no liga al propietario verificado (faltan "
            f"{', '.join(faltantes)}): una fixture de tres cadenas no es consentimiento humano (§35.2)"
        )


def emitir_aprobacion_autorizada(
    root: pathlib.Path,
    propuesta: PropuestaAprobacion,
    consentimiento: ConsentimientoAprobacion,
    *,
    timeout: float = 0.0,
) -> None:
    """``ISSUE`` a partir de un consentimiento VERIFICADO (FASE C del protocolo).

    Toma el root lock **acá** y sólo acá: la presentación y la respuesta del
    propietario ocurrieron antes, sin lock (§39.4). El núcleo durable
    (``_emitir_aprobacion_bajo_lock``) no se reimplementa ni se duplica: se
    invoca tal cual, con el witness y el CAS que P4-S4/P4-S5 ya congelaron.

    ``timeout`` es **fail-fast a propósito** (0.0): si el root lock está tomado,
    esto falla y no emite —fail-closed— en vez de atar el hilo del transporte a
    una espera que el dueño del lock no controla. El llamador que prefiera
    esperar pasa su propio valor; elegirlo acá sería decidir por un cableado que
    todavía no existe (``HITL_PRODUCTIVE = NOT_WIRED``, §46.7).
    """
    _exigir_binding_completo(consentimiento)
    if not approval_module.EMISION_PRODUCTIVA_HABILITADA:
        raise ApprovalHitolNoDisponibleError(
            "P4-S6 deja el consentimiento verificado implementado, pero la emisión productiva sigue cerrada "
            "(FASE F): ningún consumidor seguro de la aprobación está integrado, así que emitir dejaría "
            "autorizaciones ISSUED sin salida operativa. Falta el consumidor, no la superficie."
        )
    ruta = pathlib.Path(root)
    # ``participant_lock`` se importa DIRECTO y se invoca con su nombre: el censo
    # de P4-S2 resuelve el símbolo, no el atributo de otro módulo, y una
    # adquisición que el censo no ve es una sección crítica que nadie audita.
    with participant_lock(ruta, timeout=timeout) as handle:
        handle.assert_owned()
        approval_module._emitir_aprobacion_bajo_lock(handle, ruta, propuesta, consentimiento)


def describir_superficies() -> tuple[dict[str, object], ...]:
    """Censo legible de superficies, para la UI y para la auditoría del slice."""
    return tuple(
        {
            "superficie": superficie.value,
            "mecanismo": mecanismo.nombre,
            "verifica_identidad": mecanismo.verifica_identidad,
            "prueba": mecanismo.prueba,
        }
        for superficie, mecanismo in SUPERFICIES_DE_CONSENTIMIENTO.items()
    )
