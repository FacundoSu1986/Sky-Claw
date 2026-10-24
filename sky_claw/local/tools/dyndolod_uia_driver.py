"""Etapa 9 DESATENDIDA — driver UIA mutante acotado por contrato de build (#661).

**La pregunta que responde.** *"Con el proceso recién lanzado de un build que
tiene contrato, ¿cómo se lleva la GUI desde que nace hasta que se cierra sola,
tocando sólo los controles del contrato, cada acción exactamente una vez y
cortando cerrado ante cualquier cosa que no esté medida?"*

**Tres capas, y la decisión vive en la del medio.**

1. *Pasos del helper* (:func:`ejecutar_paso_sincrono`): corren DENTRO de un
   proceso descartable con el backend COM. Cada paso re-prueba la identidad del
   proceso (pid + ruta del exe), toma una instantánea fresca, la clasifica con
   la MISMA función pura que usa el padre y sólo entonces —si la precondición
   del paso se cumple en ESA instantánea— escribe o invoca. Es el anti-TOCTOU
   de P6: snapshot → revalidar → actuar, en la misma llamada.
2. *Conductor* (:class:`ConductorDesatendido`): corre en el padre, async. Pide
   pasos al ejecutor (``EjecutorDePasoUIA``), RE-CLASIFICA cada instantánea que
   vuelve (no confía en el veredicto del helper) y decide el siguiente paso o el
   corte con un :class:`CodigoDesatendido`. Lleva el ledger exactly-once.
3. *Backend* (``dyndolod_uia_driver_windows``): las llamadas COM. Es el único
   archivo del paquete autorizado a nombrar las primitivas mutantes; este módulo
   sólo las alcanza a través del puerto :class:`BackendDeConduccion`.

**Exactly-once.** El ledger reserva la acción ANTES de pedir el paso que la
invoca. Si el helper vuelve sin respuesta (timeout, crash, JSON ilegible) o el
COM devuelve error después de haber llamado a la primitiva, el desenlace es
DESCONOCIDO y NO se reintenta: ``BEGIN_OUTCOME_UNKNOWN`` /
``TERMINAL_OUTCOME_UNKNOWN``. Una acción que el helper NO llegó a invocar (la
precondición no se cumplía) es un corte limpio con su código propio.

**Hueco de observación antes del Invoke (F2, #773).** El rig Alpha-209 aprobó un
modal conocido y abortó con ``UIA_CONTROL_MAP_MISMATCH``: la única lectura del
paso ``INVOCAR`` cayó mientras la ventana se repintaba (``pantalla=CARGANDO``,
``invocado=False``). Un hueco así no se recupera desde el padre —la reserva del
ledger ya está hecha y un segundo ``_invocar`` es
``UNATTENDED_CONTRACT_VIOLATION``— sino DENTRO del paso: el helper vuelve a
leer, con presupuesto finito, sólo cuando la instantánea no muestra NINGUNA
pantalla contractual, y ejecuta la primitiva como máximo una vez. Ver
:func:`_paso_invocar`.

**El mismo hueco, en la otra operación mutante (F2b, #781).** ``ESCRIBIR_OUTPUT``
tenía el defecto hermano: UNA sola lectura, y si caía en un repintado el paso
abortaba sin escribir el campo Output aunque la pantalla contractual volviera un
instante después. Ahí no hay ledger que reservar (la escritura no es una acción
del contrato), pero el criterio es el mismo: se repite la LECTURA con el
presupuesto de :func:`_esperar_otra_lectura`, se revalida identidad en cada
vuelta, y ``backend.escribir_output`` se ejecuta por UN solo camino, con el
deadline verificado inmediatamente antes. Los dos pasos comparten el
presupuesto, NO el contrato: la pantalla requerida, las precondiciones y el
readback posterior son distintos. Ver :func:`_paso_escribir_output`.

**Durante la generación, cero mutaciones** (P7): entre el Begin aceptado y el
diálogo terminal contractual el conductor sólo pide pasos ``OBSERVAR``.

**Lo que este módulo NO decide.** Si la corrida fue exitosa: eso sigue siendo
del post-check existente del runner (exit code + artefacto + log + born-empty).
El driver sólo consigue que la herramienta arranque, genere y se cierre.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import os
import pathlib
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from sky_claw.local.tools.dyndolod_uia_contrato import (
    MARCAS_DEL_DIALOGO_DE_TAREA,
    AccionDeUI,
    CodigoDesatendido,
    ContextoDeAccion,
    ContratoDeUIHerramienta,
    MecanismoDeEscritura,
    ModalConocido,
    resolver_contrato,
)
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion, exigir_plazo_positivo_finito
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ControlObservado,
    CriteriosDeControl,
    LocalizadorDeProcesos,
    ObservacionUIAError,
    UIANoDisponibleError,
    canonicalizar_ruta_windows,
)

logger = logging.getLogger(__name__)


class DesatendidoError(Exception):
    """Corte fail-closed de una corrida desatendida, con su código estable."""

    def __init__(self, codigo: CodigoDesatendido, detalle: str, *, tool: str | None = None) -> None:
        self.codigo = codigo
        self.detalle = detalle
        self.tool = tool
        prefijo = f"{tool}: " if tool else ""
        super().__init__(f"{prefijo}{codigo.value}: {detalle}")


# =============================================================================
# Modelo de instantánea (serializable: cruza la frontera del helper)
# =============================================================================


@dataclass(frozen=True, slots=True)
class ElementoUI:
    """Un descendiente de una ventana, con lo que el contrato necesita comparar.

    ``referencia`` es el handle COM del helper: no se serializa, no se compara y
    nunca llega al padre. Fuera del helper vale ``None``.
    """

    tipo_de_control: str
    class_name: str
    nombre: str
    automation_id: str
    habilitado: bool
    referencia: object = field(default=None, compare=False, repr=False)

    def como_control(self) -> ControlObservado:
        """Vista comparable con :meth:`CriteriosDeControl.coincide` (una sola semántica)."""
        return ControlObservado(
            pid=1,
            automation_id=self.automation_id,
            nombre=self.nombre,
            tipo_de_control=self.tipo_de_control,
            class_name=self.class_name,
        )


@dataclass(frozen=True, slots=True)
class VentanaUI:
    """Ventana top-level del proceso y TODOS sus descendientes (completos o ninguno)."""

    titulo: str
    class_name: str
    elementos: tuple[ElementoUI, ...]


@dataclass(frozen=True, slots=True)
class InstantaneaUI:
    """Lo que el helper vio en UNA pasada. ``valor_output`` sólo si había asistente."""

    pid: int
    ventanas: tuple[VentanaUI, ...]
    valor_output: str | None = None


# =============================================================================
# Clasificación de pantalla (pura; la usan el helper y el padre)
# =============================================================================


class TipoDePantalla(enum.Enum):
    """Qué muestra la GUI en una instantánea, según el contrato del build."""

    SIN_VENTANA = "SIN_VENTANA"
    CARGANDO = "CARGANDO"
    ASISTENTE = "ASISTENTE"
    MODAL_CONOCIDO = "MODAL_CONOCIDO"
    DIALOGO_TERMINAL = "DIALOGO_TERMINAL"
    DIALOGO_DESCONOCIDO = "DIALOGO_DESCONOCIDO"
    AMBIGUA = "AMBIGUA"


@dataclass(frozen=True, slots=True)
class Pantalla:
    """Veredicto de :func:`clasificar_pantalla` con la evidencia que lo sostiene.

    ``acciones_visibles`` / ``acciones_habilitadas`` son ids de acciones del
    contrato cuyo selector resuelve EXACTAMENTE un elemento en su contexto.
    """

    tipo: TipoDePantalla
    detalle: str = ""
    modal: ModalConocido | None = None
    instruccion: str | None = None
    botones: tuple[tuple[str, str], ...] = ()
    acciones_visibles: frozenset[str] = frozenset()
    acciones_habilitadas: frozenset[str] = frozenset()

    def describir_dialogo(self) -> str:
        """Fingerprint legible para el operador remoto (instrucción + botones)."""
        nombres = ", ".join(nombre for nombre, _ in self.botones) or "(sin botones)"
        return f"instrucción={self.instruccion!r} botones=[{nombres}]"


def _es_marca_de_dialogo(elemento: ElementoUI) -> bool:
    return (
        elemento.tipo_de_control == MARCAS_DEL_DIALOGO_DE_TAREA["tipo_de_control"]
        and elemento.class_name == MARCAS_DEL_DIALOGO_DE_TAREA["class_name"]
    )


def _ventanas_con_dialogo(instantanea: InstantaneaUI) -> tuple[list[VentanaUI], int]:
    """Ventanas que contienen un ``#32770`` (anidado o top-level) y cuántas marcas hay."""
    marcas = 0
    con_dialogo: list[VentanaUI] = []
    for ventana in instantanea.ventanas:
        propias = sum(1 for elemento in ventana.elementos if _es_marca_de_dialogo(elemento))
        if ventana.class_name == MARCAS_DEL_DIALOGO_DE_TAREA["class_name"]:
            propias += 1
        if propias:
            con_dialogo.append(ventana)
            marcas += propias
    return con_dialogo, marcas


def _botones_de_dialogo(ventana: VentanaUI) -> tuple[tuple[str, str], ...]:
    clase = MARCAS_DEL_DIALOGO_DE_TAREA["class_name_boton"]
    return tuple(
        (elemento.nombre, elemento.automation_id)
        for elemento in ventana.elementos
        if elemento.tipo_de_control == "Button" and elemento.class_name == clase
    )


def _instruccion_de_dialogo(ventana: VentanaUI) -> str | None:
    """La instrucción si hay EXACTAMENTE un ``MainInstruction``; si no, ``None``."""
    instrucciones = [
        elemento.nombre
        for elemento in ventana.elementos
        if elemento.automation_id == MARCAS_DEL_DIALOGO_DE_TAREA["automation_id_instruccion"]
    ]
    return instrucciones[0] if len(instrucciones) == 1 else None


def elementos_que_coinciden(elementos: Iterable[ElementoUI], selector: CriteriosDeControl) -> list[ElementoUI]:
    """Elementos que satisfacen el selector (AND exacto). Selector vacío: ninguno."""
    if selector.esta_vacio():
        return []
    return [elemento for elemento in elementos if selector.coincide(elemento.como_control())]


def _resolver_acciones(
    contrato: ContratoDeUIHerramienta,
    ventana: VentanaUI,
    contexto: ContextoDeAccion,
) -> tuple[frozenset[str], frozenset[str], list[str]]:
    visibles: set[str] = set()
    habilitadas: set[str] = set()
    duplicadas: list[str] = []
    for accion in contrato.todas_las_acciones():
        if accion.contexto is not contexto:
            continue
        candidatos = elementos_que_coinciden(ventana.elementos, accion.selector)
        if len(candidatos) > 1:
            duplicadas.append(accion.id)
        elif len(candidatos) == 1:
            visibles.add(accion.id)
            if candidatos[0].habilitado:
                habilitadas.add(accion.id)
    return frozenset(visibles), frozenset(habilitadas), duplicadas


def clasificar_pantalla(contrato: ContratoDeUIHerramienta, instantanea: InstantaneaUI) -> Pantalla:
    """Clasifica UNA instantánea contra el contrato. Fail-closed por construcción.

    Orden, y el orden es parte del contrato:

    1. **Diálogos primero.** Si hay un ``#32770`` en el árbol, la pantalla es ese
       diálogo, haya o no wizard detrás: un modal tapa la ventana y un Begin
       invocado por debajo es exactamente el clic que no se puede probar. Dos
       marcas de diálogo → ``AMBIGUA``. Un diálogo se reconoce por instrucción
       EXACTA + botones requeridos; lo que no coincide con un modal conocido ni
       con el terminal es ``DIALOGO_DESCONOCIDO``.
    2. **Ventana contractual.** Exactamente una ventana de la clase del contrato
       (``TfrmMain``) con exactamente un control de Output → ``ASISTENTE``. Cero
       controles de Output → ``CARGANDO`` (arrancando o generando: el conductor
       lo desambigua por fase). Más de uno, o una acción del contrato que resuelve
       a más de un elemento → ``AMBIGUA``.
    """
    if not instantanea.ventanas:
        return Pantalla(TipoDePantalla.SIN_VENTANA, detalle="el proceso no tiene ventanas top-level")

    con_dialogo, marcas = _ventanas_con_dialogo(instantanea)
    if marcas > 1:
        return Pantalla(TipoDePantalla.AMBIGUA, detalle=f"{marcas} diálogos #32770 simultáneos")
    if marcas == 1:
        ventana = con_dialogo[0]
        instruccion = _instruccion_de_dialogo(ventana)
        botones = _botones_de_dialogo(ventana)
        visibles, habilitadas, duplicadas = _resolver_acciones(contrato, ventana, ContextoDeAccion.DIALOGO)
        if duplicadas:
            return Pantalla(
                TipoDePantalla.AMBIGUA,
                detalle=f"acciones de diálogo con más de un candidato: {sorted(duplicadas)}",
                instruccion=instruccion,
                botones=botones,
            )
        coincidencias: list[ModalConocido] = [
            modal for modal in contrato.modales_conocidos if modal.firma.coincide(instruccion, botones)
        ]
        es_terminal = contrato.dialogo_terminal.coincide(instruccion, botones)
        if len(coincidencias) + int(es_terminal) > 1:
            return Pantalla(
                TipoDePantalla.AMBIGUA,
                detalle="el diálogo coincide con más de una firma del contrato",
                instruccion=instruccion,
                botones=botones,
            )
        if coincidencias:
            return Pantalla(
                TipoDePantalla.MODAL_CONOCIDO,
                modal=coincidencias[0],
                instruccion=instruccion,
                botones=botones,
                acciones_visibles=visibles,
                acciones_habilitadas=habilitadas,
            )
        if es_terminal:
            return Pantalla(
                TipoDePantalla.DIALOGO_TERMINAL,
                instruccion=instruccion,
                botones=botones,
                acciones_visibles=visibles,
                acciones_habilitadas=habilitadas,
            )
        return Pantalla(
            TipoDePantalla.DIALOGO_DESCONOCIDO,
            detalle="diálogo sin firma en el contrato del build",
            instruccion=instruccion,
            botones=botones,
        )

    principales = [v for v in instantanea.ventanas if v.class_name == contrato.clase_ventana_principal]
    if not principales:
        return Pantalla(TipoDePantalla.CARGANDO, detalle="todavía no existe la ventana contractual")
    if len(principales) > 1:
        return Pantalla(
            TipoDePantalla.AMBIGUA, detalle=f"{len(principales)} ventanas {contrato.clase_ventana_principal}"
        )
    ventana = principales[0]
    salidas = elementos_que_coinciden(ventana.elementos, contrato.selector_output)
    if not salidas:
        return Pantalla(TipoDePantalla.CARGANDO, detalle="la ventana contractual no expone el campo Output")
    if len(salidas) > 1:
        return Pantalla(TipoDePantalla.AMBIGUA, detalle=f"{len(salidas)} candidatos al campo Output")
    visibles, habilitadas, duplicadas = _resolver_acciones(contrato, ventana, ContextoDeAccion.VENTANA_PRINCIPAL)
    if duplicadas:
        return Pantalla(
            TipoDePantalla.AMBIGUA,
            detalle=f"acciones de la ventana con más de un candidato: {sorted(duplicadas)}",
        )
    return Pantalla(TipoDePantalla.ASISTENTE, acciones_visibles=visibles, acciones_habilitadas=habilitadas)


def output_coincide(observado: str | None, esperado: str) -> bool:
    """Igualdad CANÓNICA de rutas Win32 (la misma del gate read-only). Ilegible = no."""
    canon_observado = canonicalizar_ruta_windows(observado)
    canon_esperado = canonicalizar_ruta_windows(esperado)
    return canon_observado is not None and canon_esperado is not None and canon_observado == canon_esperado


# =============================================================================
# Protocolo de pasos (padre ↔ helper)
# =============================================================================


class TipoDePaso(enum.Enum):
    OBSERVAR = "OBSERVAR"
    ESCRIBIR_OUTPUT = "ESCRIBIR_OUTPUT"
    INVOCAR = "INVOCAR"


class FaseDeObservacion(enum.Enum):
    """Qué cuenta como pantalla decisiva para un paso ``OBSERVAR``."""

    #: El asistente con la acción esperada visible, habilitada y el Output legible;
    #: o cualquier diálogo / ambigüedad.
    ARRANQUE = "ARRANQUE"
    #: Cualquier diálogo / ambigüedad. Lo demás es "sigue generando".
    GENERACION = "GENERACION"


class EstadoDePaso(enum.Enum):
    """Desenlace de un paso. ``invocado`` del resultado dice si se llamó a la primitiva."""

    OK = "OK"
    PROCESO_AUSENTE = "PROCESO_AUSENTE"
    IDENTIDAD_NO_COINCIDE = "IDENTIDAD_NO_COINCIDE"
    UIA_NO_DISPONIBLE = "UIA_NO_DISPONIBLE"
    PRECONDICION_FALLIDA = "PRECONDICION_FALLIDA"
    MECANISMO_FALLO = "MECANISMO_FALLO"
    PLAZO_AGOTADO = "PLAZO_AGOTADO"
    #: Lo sintetiza el EJECUTOR (no el helper): timeout duro, crash o respuesta
    #: ilegible. Para un paso mutante significa "no se sabe si actuó".
    SIN_RESPUESTA = "SIN_RESPUESTA"


class MotivoDePrecondicion(enum.Enum):
    """Por qué el helper NO actuó. Siempre implica ``invocado=False``."""

    PANTALLA_INCORRECTA = "PANTALLA_INCORRECTA"
    CONTROL_NO_UNICO = "CONTROL_NO_UNICO"
    CONTROL_DESHABILITADO = "CONTROL_DESHABILITADO"
    OUTPUT_DIFIERE = "OUTPUT_DIFIERE"
    ACCION_FUERA_DE_CONTRATO = "ACCION_FUERA_DE_CONTRATO"
    CONTRATO_DESCONOCIDO = "CONTRATO_DESCONOCIDO"


@dataclass(frozen=True, slots=True)
class PedidoDePaso:
    """La unidad de trabajo del helper. El contrato viaja por CLAVE, nunca por valor.

    El helper re-resuelve ``(tool, exe_sha256, modo_de_juego)`` contra su propio
    registro: no se le puede pedir que invoque un selector que no esté en el
    contrato, porque el pedido no transporta selectores.
    """

    tipo: TipoDePaso
    tool: str
    exe_sha256: str
    modo_de_juego: str
    pid: int
    ejecutable: str
    timeout_segundos: float
    intervalo_segundos: float
    fase: FaseDeObservacion | None = None
    accion_esperada: str | None = None
    valor: str | None = None
    accion_id: str | None = None
    precondicion_output: str | None = None
    modal_id: str | None = None

    def __post_init__(self) -> None:
        exigir_plazo_positivo_finito(self.timeout_segundos, "timeout_segundos")
        exigir_plazo_positivo_finito(self.intervalo_segundos, "intervalo_segundos")
        if isinstance(self.pid, bool) or self.pid <= 0:
            raise ValueError(f"pid inválido para un paso UIA: {self.pid!r}")
        if self.tipo is TipoDePaso.OBSERVAR and self.fase is None:
            raise ValueError("un paso OBSERVAR necesita su fase")
        if self.tipo is TipoDePaso.ESCRIBIR_OUTPUT and not self.valor:
            raise ValueError("un paso ESCRIBIR_OUTPUT necesita el valor a escribir")
        if self.tipo is TipoDePaso.INVOCAR and not self.accion_id:
            raise ValueError("un paso INVOCAR necesita el id de la acción")


@dataclass(frozen=True, slots=True)
class ResultadoDePaso:
    """Lo que vuelve del helper (o lo que el ejecutor sintetiza si no volvió nada)."""

    estado: EstadoDePaso
    detalle: str = ""
    instantanea: InstantaneaUI | None = None
    valor_leido: str | None = None
    invocado: bool = False
    hresult: str | None = None
    motivo: MotivoDePrecondicion | None = None


class ContratoDePasoError(ValueError):
    """El pedido o el resultado no respetan el schema cerrado del protocolo."""


NOMBRE_DEL_PEDIDO_DE_PASO = "paso.json"
NOMBRE_DEL_RESULTADO_DE_PASO = "resultado_paso.json"

_CAMPOS_DEL_ELEMENTO = ("tipo_de_control", "class_name", "nombre", "automation_id", "habilitado")
_CAMPOS_DE_LA_VENTANA = ("titulo", "class_name", "elementos")
_CAMPOS_DE_LA_INSTANTANEA = ("pid", "ventanas", "valor_output")
_CAMPOS_DEL_PEDIDO = (
    "tipo",
    "tool",
    "exe_sha256",
    "modo_de_juego",
    "pid",
    "ejecutable",
    "timeout_segundos",
    "intervalo_segundos",
    "fase",
    "accion_esperada",
    "valor",
    "accion_id",
    "precondicion_output",
    "modal_id",
)
_CAMPOS_DEL_RESULTADO = ("estado", "detalle", "instantanea", "valor_leido", "invocado", "hresult", "motivo")


def _exigir_campos(datos: object, campos: Sequence[str], contexto: str) -> dict[str, object]:
    if not isinstance(datos, dict):
        raise ContratoDePasoError(f"{contexto} debe ser un objeto JSON; llegó {type(datos).__name__}")
    claves = {str(clave) for clave in datos}
    if claves != set(campos):
        raise ContratoDePasoError(f"{contexto} debe traer exactamente {sorted(campos)}; llegó {sorted(claves)}")
    return {str(clave): valor for clave, valor in datos.items()}


def _texto(datos: dict[str, object], clave: str) -> str:
    valor = datos[clave]
    if not isinstance(valor, str):
        raise ContratoDePasoError(f"{clave!r} debe ser string; llegó {type(valor).__name__}")
    return valor


def _texto_opcional(datos: dict[str, object], clave: str) -> str | None:
    valor = datos[clave]
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ContratoDePasoError(f"{clave!r} debe ser null o string; llegó {type(valor).__name__}")
    return valor


def _booleano(datos: dict[str, object], clave: str) -> bool:
    valor = datos[clave]
    if not isinstance(valor, bool):
        raise ContratoDePasoError(f"{clave!r} debe ser booleano; llegó {type(valor).__name__}")
    return valor


def _entero(datos: dict[str, object], clave: str) -> int:
    valor = datos[clave]
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ContratoDePasoError(f"{clave!r} debe ser entero; llegó {type(valor).__name__}")
    return valor


def _numero(datos: dict[str, object], clave: str) -> float:
    valor = datos[clave]
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(float(valor)):
        raise ContratoDePasoError(f"{clave!r} debe ser numérico finito; llegó {valor!r}")
    return float(valor)


_E = TypeVar("_E", bound=enum.Enum)


def _enum_opcional(tipo: type[_E], valor: object, clave: str) -> _E | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ContratoDePasoError(f"{clave!r} debe ser null o string; llegó {type(valor).__name__}")
    try:
        return tipo(valor)
    except ValueError as exc:
        raise ContratoDePasoError(f"{clave!r} fuera del contrato: {valor!r}") from exc


def instantanea_a_dict(instantanea: InstantaneaUI) -> dict[str, object]:
    return {
        "pid": instantanea.pid,
        "valor_output": instantanea.valor_output,
        "ventanas": [
            {
                "titulo": ventana.titulo,
                "class_name": ventana.class_name,
                "elementos": [
                    {
                        "tipo_de_control": elemento.tipo_de_control,
                        "class_name": elemento.class_name,
                        "nombre": elemento.nombre,
                        "automation_id": elemento.automation_id,
                        "habilitado": elemento.habilitado,
                    }
                    for elemento in ventana.elementos
                ],
            }
            for ventana in instantanea.ventanas
        ],
    }


def instantanea_desde_dict(crudo: object) -> InstantaneaUI:
    datos = _exigir_campos(crudo, _CAMPOS_DE_LA_INSTANTANEA, "instantanea")
    ventanas_crudas = datos["ventanas"]
    if not isinstance(ventanas_crudas, list):
        raise ContratoDePasoError("'ventanas' debe ser una lista")
    ventanas: list[VentanaUI] = []
    for ventana_cruda in ventanas_crudas:
        ventana = _exigir_campos(ventana_cruda, _CAMPOS_DE_LA_VENTANA, "ventana")
        elementos_crudos = ventana["elementos"]
        if not isinstance(elementos_crudos, list):
            raise ContratoDePasoError("'elementos' debe ser una lista")
        elementos: list[ElementoUI] = []
        for elemento_crudo in elementos_crudos:
            elemento = _exigir_campos(elemento_crudo, _CAMPOS_DEL_ELEMENTO, "elemento")
            elementos.append(
                ElementoUI(
                    tipo_de_control=_texto(elemento, "tipo_de_control"),
                    class_name=_texto(elemento, "class_name"),
                    nombre=_texto(elemento, "nombre"),
                    automation_id=_texto(elemento, "automation_id"),
                    habilitado=_booleano(elemento, "habilitado"),
                )
            )
        ventanas.append(
            VentanaUI(
                titulo=_texto(ventana, "titulo"),
                class_name=_texto(ventana, "class_name"),
                elementos=tuple(elementos),
            )
        )
    return InstantaneaUI(
        pid=_entero(datos, "pid"),
        ventanas=tuple(ventanas),
        valor_output=_texto_opcional(datos, "valor_output"),
    )


def pedido_de_paso_a_json(pedido: PedidoDePaso) -> str:
    return json.dumps(
        {
            "tipo": pedido.tipo.value,
            "tool": pedido.tool,
            "exe_sha256": pedido.exe_sha256,
            "modo_de_juego": pedido.modo_de_juego,
            "pid": pedido.pid,
            "ejecutable": pedido.ejecutable,
            "timeout_segundos": pedido.timeout_segundos,
            "intervalo_segundos": pedido.intervalo_segundos,
            "fase": pedido.fase.value if pedido.fase is not None else None,
            "accion_esperada": pedido.accion_esperada,
            "valor": pedido.valor,
            "accion_id": pedido.accion_id,
            "precondicion_output": pedido.precondicion_output,
            "modal_id": pedido.modal_id,
        },
        ensure_ascii=False,
    )


def pedido_de_paso_desde_json(texto: str) -> PedidoDePaso:
    try:
        crudo = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ContratoDePasoError(f"el pedido no es JSON válido: {exc}") from exc
    datos = _exigir_campos(crudo, _CAMPOS_DEL_PEDIDO, "pedido")
    tipo = _enum_opcional(TipoDePaso, datos["tipo"], "tipo")
    if tipo is None:
        raise ContratoDePasoError("'tipo' es obligatorio")
    try:
        return PedidoDePaso(
            tipo=tipo,
            tool=_texto(datos, "tool"),
            exe_sha256=_texto(datos, "exe_sha256"),
            modo_de_juego=_texto(datos, "modo_de_juego"),
            pid=_entero(datos, "pid"),
            ejecutable=_texto(datos, "ejecutable"),
            timeout_segundos=_numero(datos, "timeout_segundos"),
            intervalo_segundos=_numero(datos, "intervalo_segundos"),
            fase=_enum_opcional(FaseDeObservacion, datos["fase"], "fase"),
            accion_esperada=_texto_opcional(datos, "accion_esperada"),
            valor=_texto_opcional(datos, "valor"),
            accion_id=_texto_opcional(datos, "accion_id"),
            precondicion_output=_texto_opcional(datos, "precondicion_output"),
            modal_id=_texto_opcional(datos, "modal_id"),
        )
    except ValueError as exc:
        if isinstance(exc, ContratoDePasoError):
            raise
        raise ContratoDePasoError(f"pedido fuera de contrato: {exc}") from exc


def resultado_de_paso_a_json(resultado: ResultadoDePaso) -> str:
    return json.dumps(
        {
            "estado": resultado.estado.value,
            "detalle": resultado.detalle,
            "instantanea": instantanea_a_dict(resultado.instantanea) if resultado.instantanea is not None else None,
            "valor_leido": resultado.valor_leido,
            "invocado": resultado.invocado,
            "hresult": resultado.hresult,
            "motivo": resultado.motivo.value if resultado.motivo is not None else None,
        },
        ensure_ascii=False,
    )


def resultado_de_paso_desde_json(texto: str) -> ResultadoDePaso:
    """Reconstruye el resultado; cualquier dato raro LANZA (el ejecutor lo traduce).

    ``SIN_RESPUESTA`` NO puede venir del helper: lo sintetiza el ejecutor. Un
    helper que lo devolviera estaría fabricando la ambigüedad que el padre usa
    para no reintentar, así que se rechaza como contrato roto.
    """
    try:
        crudo = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ContratoDePasoError(f"el resultado no es JSON válido: {exc}") from exc
    datos = _exigir_campos(crudo, _CAMPOS_DEL_RESULTADO, "resultado")
    estado = _enum_opcional(EstadoDePaso, datos["estado"], "estado")
    if estado is None or estado is EstadoDePaso.SIN_RESPUESTA:
        raise ContratoDePasoError(f"estado inválido desde el helper: {datos['estado']!r}")
    hresult_crudo = datos["hresult"]
    if hresult_crudo is not None and not isinstance(hresult_crudo, str):
        raise ContratoDePasoError("'hresult' debe ser null o string")
    motivo = _enum_opcional(MotivoDePrecondicion, datos["motivo"], "motivo")
    invocado = _booleano(datos, "invocado")
    if motivo is not None and (estado is not EstadoDePaso.PRECONDICION_FALLIDA or invocado):
        raise ContratoDePasoError("un motivo de precondición sólo acompaña a PRECONDICION_FALLIDA sin invocación")
    if estado is EstadoDePaso.PRECONDICION_FALLIDA and motivo is None:
        raise ContratoDePasoError("PRECONDICION_FALLIDA exige su motivo")
    return ResultadoDePaso(
        estado=estado,
        detalle=_texto(datos, "detalle"),
        instantanea=instantanea_desde_dict(datos["instantanea"]) if datos["instantanea"] is not None else None,
        valor_leido=_texto_opcional(datos, "valor_leido"),
        invocado=invocado,
        hresult=hresult_crudo,
        motivo=motivo,
    )


# =============================================================================
# Pasos del helper (síncronos; corren con el backend COM en el proceso descartable)
# =============================================================================


@runtime_checkable
class BackendDeConduccion(Protocol):
    """Puerto del backend del helper: lectura del árbol + las dos primitivas mutantes.

    La implementación real es ``dyndolod_uia_driver_windows.BackendUIAWindows``.
    ``escribir_output`` e ``invocar`` devuelven el HRESULT como texto (``"S_OK"``
    o ``"COMError 0x…"``) y NUNCA reintentan ni prueban otro mecanismo.
    """

    def instantanea(self, pid: int) -> InstantaneaUI:
        """Instantánea completa del proceso, con ``referencia`` en cada elemento."""
        ...

    def leer_output(self, elemento: ElementoUI) -> str | None:
        """Valor que el control muestra (readback). ``None`` si no lo expone."""
        ...

    def escribir_output(self, elemento: ElementoUI, mecanismo: MecanismoDeEscritura, valor: str) -> str:
        """Escribe por el mecanismo del contrato. Devuelve el HRESULT."""
        ...

    def invocar(self, elemento: ElementoUI) -> str:
        """``InvokePattern.Invoke`` exactamente una vez. Devuelve el HRESULT."""
        ...

    def liberar(self) -> None:
        """Suelta referencias y cierra el apartamento COM. Idempotente."""
        ...


HRESULT_OK = "S_OK"


def _identidad(pedido: PedidoDePaso, localizador: LocalizadorDeProcesos) -> EstadoDePaso | None:
    """``None`` si el pid vivo es el ejecutable pedido; si no, por qué no."""
    try:
        procesos = localizador.procesos()
    except ObservacionUIAError:
        return EstadoDePaso.UIA_NO_DISPONIBLE
    propio = [proceso for proceso in procesos if proceso.pid == pedido.pid]
    if not propio:
        return EstadoDePaso.PROCESO_AUSENTE
    esperado = canonicalizar_ruta_windows(pedido.ejecutable)
    observado = canonicalizar_ruta_windows(propio[0].ruta_ejecutable)
    if esperado is None or observado is None or esperado != observado:
        return EstadoDePaso.IDENTIDAD_NO_COINCIDE
    return None


def _ventana_principal(contrato: ContratoDeUIHerramienta, instantanea: InstantaneaUI) -> VentanaUI | None:
    principales = [v for v in instantanea.ventanas if v.class_name == contrato.clase_ventana_principal]
    return principales[0] if len(principales) == 1 else None


def _ventana_del_dialogo(instantanea: InstantaneaUI) -> VentanaUI | None:
    con_dialogo, marcas = _ventanas_con_dialogo(instantanea)
    return con_dialogo[0] if marcas == 1 else None


def _con_output(
    contrato: ContratoDeUIHerramienta,
    instantanea: InstantaneaUI,
    backend: BackendDeConduccion,
) -> InstantaneaUI:
    """Adjunta el valor del Output si la pantalla es un asistente inequívoco."""
    if clasificar_pantalla(contrato, instantanea).tipo is not TipoDePantalla.ASISTENTE:
        return instantanea
    ventana = _ventana_principal(contrato, instantanea)
    if ventana is None:
        return instantanea
    salida = elementos_que_coinciden(ventana.elementos, contrato.selector_output)
    if len(salida) != 1:
        return instantanea
    return InstantaneaUI(
        pid=instantanea.pid,
        ventanas=instantanea.ventanas,
        valor_output=backend.leer_output(salida[0]),
    )


def _es_decisiva(
    contrato: ContratoDeUIHerramienta,
    pantalla: Pantalla,
    instantanea: InstantaneaUI,
    pedido: PedidoDePaso,
) -> bool:
    if pantalla.tipo in {
        TipoDePantalla.MODAL_CONOCIDO,
        TipoDePantalla.DIALOGO_TERMINAL,
        TipoDePantalla.DIALOGO_DESCONOCIDO,
        TipoDePantalla.AMBIGUA,
    }:
        return True
    if pedido.fase is FaseDeObservacion.ARRANQUE and pantalla.tipo is TipoDePantalla.ASISTENTE:
        esperada = pedido.accion_esperada
        return (
            esperada is not None and esperada in pantalla.acciones_habilitadas and instantanea.valor_output is not None
        )
    return False


def _sin_referencias(instantanea: InstantaneaUI) -> InstantaneaUI:
    """Copia sin handles COM: lo que cruza la frontera nunca lleva punteros."""
    return InstantaneaUI(
        pid=instantanea.pid,
        valor_output=instantanea.valor_output,
        ventanas=tuple(
            VentanaUI(
                titulo=ventana.titulo,
                class_name=ventana.class_name,
                elementos=tuple(
                    ElementoUI(
                        tipo_de_control=e.tipo_de_control,
                        class_name=e.class_name,
                        nombre=e.nombre,
                        automation_id=e.automation_id,
                        habilitado=e.habilitado,
                    )
                    for e in ventana.elementos
                ),
            )
            for ventana in instantanea.ventanas
        ),
    )


def _paso_observar(
    pedido: PedidoDePaso,
    contrato: ContratoDeUIHerramienta,
    backend: BackendDeConduccion,
    localizador: LocalizadorDeProcesos,
    reloj: Callable[[], float],
    dormir: Callable[[float], None],
) -> ResultadoDePaso:
    limite = reloj() + pedido.timeout_segundos
    ultima: InstantaneaUI | None = None
    ultimo_error = ""
    while True:
        identidad = _identidad(pedido, localizador)
        if identidad is not None:
            return ResultadoDePaso(
                estado=identidad,
                detalle="identidad del proceso no demostrada en la observación",
                instantanea=_sin_referencias(ultima) if ultima is not None else None,
            )
        try:
            instantanea = _con_output(contrato, backend.instantanea(pedido.pid), backend)
        except UIANoDisponibleError as exc:
            return ResultadoDePaso(estado=EstadoDePaso.UIA_NO_DISPONIBLE, detalle=str(exc))
        except ObservacionUIAError as exc:
            # Un elemento stale a mitad de un repaint: se reintenta dentro del
            # plazo; si el plazo vence así, el veredicto es "no se pudo observar".
            ultimo_error = str(exc)
        else:
            ultima = instantanea
            ultimo_error = ""
            if _es_decisiva(contrato, clasificar_pantalla(contrato, instantanea), instantanea, pedido):
                return ResultadoDePaso(estado=EstadoDePaso.OK, instantanea=_sin_referencias(instantanea))
        restante = limite - reloj()
        if restante <= 0:
            if ultima is None:
                return ResultadoDePaso(
                    estado=EstadoDePaso.UIA_NO_DISPONIBLE,
                    detalle=f"no hubo una sola observación legible en el plazo: {ultimo_error}",
                )
            return ResultadoDePaso(
                estado=EstadoDePaso.PLAZO_AGOTADO,
                detalle=ultimo_error or "plazo agotado sin pantalla decisiva",
                instantanea=_sin_referencias(ultima),
            )
        dormir(min(pedido.intervalo_segundos, restante))


def _precondicion(
    motivo: MotivoDePrecondicion,
    detalle: str,
    instantanea: InstantaneaUI | None,
) -> ResultadoDePaso:
    return ResultadoDePaso(
        estado=EstadoDePaso.PRECONDICION_FALLIDA,
        detalle=detalle,
        instantanea=_sin_referencias(instantanea) if instantanea is not None else None,
        motivo=motivo,
    )


def _paso_escribir_output(
    pedido: PedidoDePaso,
    contrato: ContratoDeUIHerramienta,
    backend: BackendDeConduccion,
    localizador: LocalizadorDeProcesos,
    reloj: Callable[[], float],
    dormir: Callable[[float], None],
) -> ResultadoDePaso:
    """Escribe el Output UNA vez, releyendo sólo los huecos de observación (F2b).

    **Por qué el paso relee.** El paso hacía UNA sola lectura. Si TexGen o
    DynDOLOD estaban repintando, esa lectura devolvía ``CARGANDO``/
    ``SIN_VENTANA`` —o moría con ``ObservacionUIAError`` por un elemento stale a
    mitad de repaint— y el paso abortaba sin escribir, aunque la pantalla
    contractual volviera un instante después. Es el mismo hueco que F2 (#773)
    arregló para ``INVOCAR``, por la otra puerta.

    **Por qué la reobservación vive acá y no en el conductor.** El padre no
    reserva la escritura en el ledger (no es una acción del contrato), así que
    podría reintentar el paso entero desde arriba. No se hace: un reintento del
    paso completo, después de una respuesta ambigua, es exactamente el segundo
    ``escribir_output`` que I1 prohíbe. Dentro del paso la lectura es lo único
    que se repite y la primitiva se alcanza por UN solo camino.

    **Qué se revalida en cada vuelta** (nunca se saltea un chequeo, sólo se
    repite): pid vivo y ruta canónica del ejecutable (:func:`_identidad`), la
    instantánea fresca, que la pantalla sea ``ASISTENTE``, y que el campo Output
    sea ÚNICO y esté HABILITADO. El SHA-256 y la versión del build son la CLAVE
    del contrato, que se resolvió al entrar al paso
    (``ejecutar_paso_sincrono``) y no cambia.

    **Qué NO se comparte con ``_paso_invocar``, aunque el presupuesto sí.** Los
    dos pasos mutan, pero sus contratos no son el mismo y tratarlos como
    iguales sería el defecto, no el arreglo:

    * la pantalla requerida de la escritura es SIEMPRE ``ASISTENTE``; la de una
      invocación depende de la acción (asistente, modal medido o terminal);
    * la invocación tiene precondiciones propias (identidad del modal,
      ``precondicion_output``); la escritura no tiene ninguna;
    * la escritura tiene READBACK después de la primitiva y la invocación no, y
      el readback corre con la mutación YA hecha: un readback ilegible no
      autoriza reintentar nada (se reporta ``invocado=True`` sin valor leído).

    **Sólo se recuperan huecos.** ``CARGANDO``/``SIN_VENTANA`` y
    ``ObservacionUIAError`` se releen; cualquier pantalla decisiva distinta
    (otro modal, un diálogo desconocido, ``AMBIGUA``) y un control no
    inequívoco o deshabilitado cortan en la PRIMERA lectura. ``UIANoDisponibleError``
    es el sensor, no un repintado: corta en la primera lectura, igual que en
    ``_paso_observar``.

    **El plazo acota también la ACCIÓN.** ``reloj() >= limite`` se verifica
    pegado a la primitiva, después de todas las precondiciones: si la lectura
    que habilita la escritura llega a ``limite`` o después, el paso NO escribe y
    devuelve ``PLAZO_AGOTADO`` con ``invocado=False``. ``>=`` y no ``>``: el
    presupuesto se agota EN el límite, la misma convención que los otros seis
    sitios del archivo que comparan contra uno.

    **Una sola escritura.** Después de ``backend.escribir_output`` no hay
    ``continue`` ni reintento: el HRESULT fallido y el readback ilegible o
    distinto se devuelven tal cual, con ``invocado=True``, para que el padre
    sepa que la mutación pudo haber ocurrido.
    """
    limite = reloj() + pedido.timeout_segundos
    lecturas = 0
    ultima: InstantaneaUI | None = None
    while True:
        identidad = _identidad(pedido, localizador)
        if identidad is not None:
            return ResultadoDePaso(estado=identidad, detalle="identidad no demostrada antes de escribir")
        lecturas += 1
        try:
            instantanea = backend.instantanea(pedido.pid)
        except UIANoDisponibleError as exc:
            # El SENSOR, no el repintado: reintentar no aporta y sólo retrasa el
            # corte que de todos modos hay que dar.
            return ResultadoDePaso(estado=EstadoDePaso.UIA_NO_DISPONIBLE, detalle=str(exc))
        except ObservacionUIAError as exc:
            # Hermano-excepción del hueco: un elemento stale a mitad de repaint.
            # La lectura no ocurrió, pero tampoco se mutó nada.
            corte = _esperar_otra_lectura(
                detalle=f"lectura ilegible antes de escribir el Output ({exc})",
                efecto="no se escribió nada",
                lecturas=lecturas,
                limite=limite,
                ultima=ultima,
                pedido=pedido,
                reloj=reloj,
                dormir=dormir,
            )
            if corte is not None:
                return corte
            continue
        ultima = instantanea
        pantalla = clasificar_pantalla(contrato, instantanea)
        if pantalla.tipo is not TipoDePantalla.ASISTENTE:
            detalle = (
                f"sólo se escribe el Output con el asistente a la vista; pantalla={pantalla.tipo.value} "
                f"{pantalla.detalle}"
            ).strip()
            if not _es_hueco_de_observacion(pantalla):
                return _precondicion(MotivoDePrecondicion.PANTALLA_INCORRECTA, detalle, instantanea)
            corte = _esperar_otra_lectura(
                detalle=detalle,
                efecto="no se escribió nada",
                lecturas=lecturas,
                limite=limite,
                ultima=ultima,
                pedido=pedido,
                reloj=reloj,
                dormir=dormir,
            )
            if corte is not None:
                return corte
            continue
        ventana = _ventana_principal(contrato, instantanea)
        salida = elementos_que_coinciden(ventana.elementos, contrato.selector_output) if ventana is not None else []
        if len(salida) != 1:
            return _precondicion(MotivoDePrecondicion.CONTROL_NO_UNICO, "el campo Output no es único", instantanea)
        if not salida[0].habilitado:
            return _precondicion(
                MotivoDePrecondicion.CONTROL_DESHABILITADO, "el campo Output no está habilitado", instantanea
            )
        # El presupuesto acota la ACCIÓN, no sólo la espera, y va pegado a la
        # primitiva a propósito: es el ÚNICO punto de mutación del paso, así que
        # ninguna ruta puede escribir sin pasar por este chequeo. ``>=`` porque el
        # presupuesto se agota EN el límite (misma convención que ``_paso_invocar``
        # y las esperas del cierre).
        if reloj() >= limite:
            return ResultadoDePaso(
                estado=EstadoDePaso.PLAZO_AGOTADO,
                detalle=(
                    f"el presupuesto de {pedido.timeout_segundos:.1f}s venció durante la lectura "
                    f"({lecturas} lecturas); el campo Output está a la vista y habilitado pero no se "
                    "escribió nada"
                ),
                instantanea=_sin_referencias(instantanea),
            )
        valor = pedido.valor or ""
        hresult = backend.escribir_output(salida[0], contrato.mecanismo_output, valor)
        if hresult != HRESULT_OK:
            return ResultadoDePaso(
                estado=EstadoDePaso.MECANISMO_FALLO,
                detalle=f"{contrato.mecanismo_output.value} rechazó la escritura",
                instantanea=_sin_referencias(instantanea),
                invocado=True,
                hresult=hresult,
            )
        # El readback va DESPUÉS de la primitiva: si falla, la escritura ya ocurrió y
        # el resultado lo dice (``invocado=True`` sin valor leído), en vez de dejar
        # que la red general lo reporte como "no se actuó".
        try:
            leido = backend.leer_output(salida[0])
        except ObservacionUIAError as exc:
            return ResultadoDePaso(
                estado=EstadoDePaso.OK,
                detalle=f"readback ilegible tras escribir: {exc}",
                instantanea=_sin_referencias(instantanea),
                valor_leido=None,
                invocado=True,
                hresult=hresult,
            )
        return ResultadoDePaso(
            estado=EstadoDePaso.OK,
            instantanea=_sin_referencias(instantanea),
            valor_leido=leido,
            invocado=True,
            hresult=hresult,
        )


def _pantalla_requerida(contrato: ContratoDeUIHerramienta, accion: AccionDeUI) -> TipoDePantalla:
    if accion.id == contrato.accion_terminal.id:
        return TipoDePantalla.DIALOGO_TERMINAL
    if any(modal.accion_continuar.id == accion.id for modal in contrato.modales_conocidos):
        return TipoDePantalla.MODAL_CONOCIDO
    return TipoDePantalla.ASISTENTE


#: Tope de lecturas de UN paso que reobserva (``INVOCAR`` desde F2, ``ESCRIBIR_OUTPUT``
#: desde F2b). El límite PRODUCTIVO es el presupuesto temporal
#: (``PedidoDePaso.timeout_segundos``); este tope es el riel anti-giro para cuando
#: el reloj no avanza (reloj falso de test, o un ``dormir`` que retorna de
#: inmediato): sin él, un presupuesto "finito" podría no vencer nunca y el paso
#: giraría para siempre dentro del helper. Es UN solo tope y no uno por tipo de
#: paso: el riel protege al helper, y el helper es el mismo proceso.
MAXIMO_DE_LECTURAS_POR_PASO = 16


def _es_hueco_de_observacion(pantalla: Pantalla) -> bool:
    """¿La instantánea no muestra NINGUNA pantalla contractual? (hueco de repintado)

    La distinción es la que hace segura la reobservación: un snapshot sin
    pantalla decisiva (``CARGANDO``, ``SIN_VENTANA``) es un hueco de
    OBSERVACIÓN —el árbol UIA se leyó mientras la ventana se repintaba— y volver
    a leer no muta nada. Un snapshot que sí muestra una pantalla decisiva
    DISTINTA de la esperada (otro modal, un diálogo desconocido, el asistente
    donde se esperaba el modal, ``AMBIGUA``) es un DESACUERDO real: el mundo
    cambió y no se reintenta.
    """
    return pantalla.tipo in {TipoDePantalla.CARGANDO, TipoDePantalla.SIN_VENTANA}


def _esperar_otra_lectura(
    *,
    detalle: str,
    efecto: str,
    lecturas: int,
    limite: float,
    ultima: InstantaneaUI | None,
    pedido: PedidoDePaso,
    reloj: Callable[[], float],
    dormir: Callable[[float], None],
) -> ResultadoDePaso | None:
    """Espera la próxima lectura, o devuelve el corte: UN solo sitio de presupuesto.

    ``None`` significa "todavía hay presupuesto": se durmió y se puede releer. Un
    corte devuelto es siempre con ``invocado=False`` — nunca se llegó a la
    primitiva. Que el presupuesto viva en una sola función es deliberado: el
    hueco por pantalla (``CARGANDO``) y el hueco por excepción (elemento stale)
    tienen que agotarse con el MISMO criterio, o el segundo se queda sin riel.

    ``efecto`` es lo que el corte tiene que declarar que NO pasó, y es
    obligatorio a propósito: ``INVOCAR`` y ``ESCRIBIR_OUTPUT`` comparten el
    presupuesto, pero no comparten el verbo, y un texto heredado del hermano
    diría "no se invocó nada" sobre un paso que escribe. Cada llamador nombra su
    propio efecto; ninguno lo hereda por defecto.

    Sin una sola lectura legible el veredicto es el sensor (``UIA_NO_DISPONIBLE``),
    no la pantalla: mismo criterio que ``_paso_observar``.
    """
    restante = limite - reloj()
    if lecturas < MAXIMO_DE_LECTURAS_POR_PASO and restante > 0:
        dormir(min(pedido.intervalo_segundos, restante))
        return None
    if ultima is None:
        return ResultadoDePaso(
            estado=EstadoDePaso.UIA_NO_DISPONIBLE,
            detalle=f"no hubo una sola lectura legible en {pedido.timeout_segundos:.1f}s: {detalle}",
        )
    return _precondicion(
        MotivoDePrecondicion.PANTALLA_INCORRECTA,
        f"{detalle} — {lecturas} lecturas en {pedido.timeout_segundos:.1f}s: la pantalla "
        f"contractual no apareció y {efecto}",
        ultima,
    )


def _paso_invocar(
    pedido: PedidoDePaso,
    contrato: ContratoDeUIHerramienta,
    backend: BackendDeConduccion,
    localizador: LocalizadorDeProcesos,
    reloj: Callable[[], float],
    dormir: Callable[[float], None],
) -> ResultadoDePaso:
    """Invoca la acción UNA vez, releyendo sólo los huecos de observación (F2).

    **Por qué la reobservación vive acá y no en el conductor.** El padre reserva
    la acción en el ledger antes de pedir el paso; una segunda llamada a
    ``_invocar`` con el mismo ``accion.id`` es ``UNATTENDED_CONTRACT_VIOLATION``.
    Reintentar desde arriba exigiría liberar la reserva ante una respuesta
    "no invocada", que es exactamente la puerta que el exactly-once no abre.
    Dentro del paso, en cambio, la reserva es una sola y ``backend.invocar`` se
    ejecuta como máximo una vez: lo único que se repite es la LECTURA.

    **Qué se revalida en cada lectura** (nunca se saltea un chequeo, sólo se
    repite): pid vivo y ruta canónica del ejecutable (:func:`_identidad`), la
    instantánea fresca, la pantalla requerida por la acción, la identidad del
    modal pedido, la precondición de Output y la unicidad/habilitación del
    control. El SHA-256 y la versión del build son la CLAVE del contrato, que se
    resolvió al entrar al paso (``ejecutar_paso_sincrono``) y no cambia: el
    hash se midió sobre el archivo que el runner lanzó, y re-leerlo del disco en
    cada vuelta no agregaría información sobre lo que la GUI muestra.

    **Dos formas del mismo hueco.** El hueco medido en el rig fue una pantalla sin
    nada contractual (``CARGANDO``/``SIN_VENTANA``); el hermano es la lectura que
    ni siquiera devuelve árbol porque el elemento quedó stale a mitad del
    repaint (``ObservacionUIAError``). Las dos se releen con el MISMO presupuesto
    (:func:`_esperar_otra_lectura`), porque las dos son "todavía no se pudo
    observar" y ninguna mutó nada. ``UIANoDisponibleError`` NO se relee: ése es
    el sensor, y corta en la primera lectura — la misma distinción que ya hacía
    ``_paso_observar``.

    **Presupuesto.** ``timeout_segundos`` es el techo temporal y
    :data:`MAXIMO_DE_LECTURAS_POR_PASO` el tope de lecturas; lo que venza
    primero corta. Agotado el presupuesto sin pantalla contractual el desenlace
    es ``PRECONDICION_FALLIDA``/``PANTALLA_INCORRECTA`` con ``invocado=False``.
    Si no hubo ni una sola lectura legible, el desenlace es ``UIA_NO_DISPONIBLE``:
    el veredicto honesto es el sensor. En los tres casos el padre lo ve como un
    corte limpio, no como un desenlace desconocido.

    **El plazo acota la ESPERA y la ACCIÓN.** Si la lectura que habilita el
    Invoke llega a ``limite`` o después —una lectura LENTA, no una pantalla
    ausente— el paso no invoca: devuelve ``PLAZO_AGOTADO`` con
    ``invocado=False``. El borde es ``reloj() >= limite``: el presupuesto se
    agota EN el límite, igual que en la observación y en las esperas del cierre.
    El modal puede estar a la vista y la instantánea ser
    válida; actuar fuera del presupuesto que el operador aprobó no es un
    desenlace aceptable, y el corte es limpio (se puede re-lanzar), no ambiguo.
    Esto es deliberadamente MÁS estricto que ``_paso_observar``, que devuelve en
    cuanto ve la pantalla decisiva: observar de más no muta, invocar de más sí.

    **Nunca se reintenta la primitiva.** Cualquier desvío que no sea un hueco
    —incluido un control no inequívoco— corta antes de invocar, y una vez
    llamado ``backend.invocar`` el resultado se devuelve tal cual, con su
    HRESULT, sin segunda llamada.
    """
    accion = contrato.accion_por_id(pedido.accion_id or "")
    if accion is None:
        return _precondicion(
            MotivoDePrecondicion.ACCION_FUERA_DE_CONTRATO,
            f"la acción {pedido.accion_id!r} no está en el contrato del build",
            None,
        )
    requerida = _pantalla_requerida(contrato, accion)
    limite = reloj() + pedido.timeout_segundos
    lecturas = 0
    ultima: InstantaneaUI | None = None
    while True:
        identidad = _identidad(pedido, localizador)
        if identidad is not None:
            return ResultadoDePaso(estado=identidad, detalle="identidad no demostrada antes de invocar")
        lecturas += 1
        try:
            instantanea = _con_output(contrato, backend.instantanea(pedido.pid), backend)
        except UIANoDisponibleError as exc:
            # El SENSOR, no el repintado: reintentar no aporta y sólo retrasa el
            # corte que de todos modos hay que dar.
            return ResultadoDePaso(estado=EstadoDePaso.UIA_NO_DISPONIBLE, detalle=str(exc))
        except ObservacionUIAError as exc:
            # Hermano-excepción del hueco: un elemento stale a mitad de repaint. La
            # lectura no ocurrió, así que no hay instantánea que clasificar, pero
            # tampoco se mutó nada: es exactamente el caso que se puede releer.
            corte = _esperar_otra_lectura(
                detalle=f"{accion.id}: lectura ilegible ({exc})",
                efecto="no se invocó nada",
                lecturas=lecturas,
                limite=limite,
                ultima=ultima,
                pedido=pedido,
                reloj=reloj,
                dormir=dormir,
            )
            if corte is not None:
                return corte
            continue
        ultima = instantanea
        pantalla = clasificar_pantalla(contrato, instantanea)
        if pantalla.tipo is not requerida:
            detalle = f"{accion.id} exige {requerida.value}; pantalla={pantalla.tipo.value} {pantalla.detalle}".strip()
            if not _es_hueco_de_observacion(pantalla):
                return _precondicion(MotivoDePrecondicion.PANTALLA_INCORRECTA, detalle, instantanea)
            corte = _esperar_otra_lectura(
                detalle=detalle,
                efecto="no se invocó nada",
                lecturas=lecturas,
                limite=limite,
                ultima=ultima,
                pedido=pedido,
                reloj=reloj,
                dormir=dormir,
            )
            if corte is not None:
                return corte
            continue
        if requerida is TipoDePantalla.MODAL_CONOCIDO and (
            pantalla.modal is None or pantalla.modal.id != pedido.modal_id
        ):
            return _precondicion(
                MotivoDePrecondicion.PANTALLA_INCORRECTA,
                f"el modal a la vista no es {pedido.modal_id!r}",
                instantanea,
            )
        if pedido.precondicion_output is not None and not output_coincide(
            instantanea.valor_output, pedido.precondicion_output
        ):
            return _precondicion(
                MotivoDePrecondicion.OUTPUT_DIFIERE,
                f"el Output muestra {instantanea.valor_output!r} y se esperaba {pedido.precondicion_output!r}",
                instantanea,
            )
        ventana = (
            _ventana_principal(contrato, instantanea)
            if accion.contexto is ContextoDeAccion.VENTANA_PRINCIPAL
            else _ventana_del_dialogo(instantanea)
        )
        candidatos = elementos_que_coinciden(ventana.elementos, accion.selector) if ventana is not None else []
        if len(candidatos) != 1:
            return _precondicion(
                MotivoDePrecondicion.CONTROL_NO_UNICO,
                f"{accion.id}: {len(candidatos)} candidatos al control",
                instantanea,
            )
        if not candidatos[0].habilitado:
            return _precondicion(
                MotivoDePrecondicion.CONTROL_DESHABILITADO,
                f"{accion.id}: el control no está habilitado",
                instantanea,
            )
        # El presupuesto acota también la ACCIÓN (P1 del Tech Lead). Va pegado a
        # la primitiva a propósito: es el ÚNICO punto de mutación del paso, así
        # que ninguna ruta puede invocar sin pasar por este chequeo.
        #
        # ``>=`` y no ``>``: el presupuesto se agota EN el límite, no después. Es
        # la convención de los otros cinco sitios del archivo que comparan contra
        # un límite (``restante <= 0`` en la observación y en
        # ``_esperar_otra_lectura``, ``time.monotonic() >= limite`` en las dos
        # esperas del cierre). Con ``>`` este paso invocaría en el único instante
        # en que el resto del archivo ya cortó.
        if reloj() >= limite:
            return ResultadoDePaso(
                estado=EstadoDePaso.PLAZO_AGOTADO,
                detalle=(
                    f"{accion.id}: el presupuesto de {pedido.timeout_segundos:.1f}s venció durante la "
                    f"lectura ({lecturas} lecturas); la pantalla contractual está a la vista pero no se "
                    "invocó nada"
                ),
                instantanea=_sin_referencias(instantanea),
            )
        hresult = backend.invocar(candidatos[0])
        return ResultadoDePaso(
            estado=EstadoDePaso.OK if hresult == HRESULT_OK else EstadoDePaso.MECANISMO_FALLO,
            detalle="" if hresult == HRESULT_OK else f"{accion.id}: Invoke devolvió {hresult}",
            instantanea=_sin_referencias(instantanea),
            invocado=True,
            hresult=hresult,
        )


def ejecutar_paso_sincrono(
    pedido: PedidoDePaso,
    *,
    fabrica_backend: Callable[[], BackendDeConduccion],
    localizador: LocalizadorDeProcesos,
    reloj: Callable[[], float] = time.monotonic,
    dormir: Callable[[float], None] = time.sleep,
) -> ResultadoDePaso:
    """Corre UN paso con el backend construido en ESTE hilo. Nunca lanza por el sensor.

    El contrato se re-resuelve acá desde la clave del pedido: un helper con un
    registro distinto al del padre (versión cruzada) responde
    ``CONTRATO_DESCONOCIDO`` y no actúa.
    """
    contrato = resolver_contrato(pedido.tool, pedido.exe_sha256, pedido.modo_de_juego)
    if contrato is None:
        return _precondicion(
            MotivoDePrecondicion.CONTRATO_DESCONOCIDO,
            f"sin contrato para ({pedido.tool}, {pedido.exe_sha256}, {pedido.modo_de_juego})",
            None,
        )
    try:
        backend = fabrica_backend()
    except ObservacionUIAError as exc:
        return ResultadoDePaso(estado=EstadoDePaso.UIA_NO_DISPONIBLE, detalle=f"sin backend UIA: {exc}")
    try:
        if pedido.tipo is TipoDePaso.OBSERVAR:
            return _paso_observar(pedido, contrato, backend, localizador, reloj, dormir)
        if pedido.tipo is TipoDePaso.ESCRIBIR_OUTPUT:
            return _paso_escribir_output(pedido, contrato, backend, localizador, reloj, dormir)
        return _paso_invocar(pedido, contrato, backend, localizador, reloj, dormir)
    except ObservacionUIAError as exc:
        # Sólo llega acá un fallo de LECTURA previo a la primitiva (las primitivas
        # devuelven su HRESULT): no se actuó, y el veredicto honesto es el sensor.
        return ResultadoDePaso(estado=EstadoDePaso.UIA_NO_DISPONIBLE, detalle=f"fallo de observación: {exc}")
    finally:
        backend.liberar()


# =============================================================================
# Puertos del padre
# =============================================================================


class EjecutorDePasoUIA(Protocol):
    """Ejecuta un paso y devuelve su resultado. Sólo propaga ``CancelledError``.

    El productivo (``EjecutorDePasoPorHelper``) corre el paso en un proceso
    descartable con deadline duro; cualquier fallo del mecanismo se sintetiza
    como ``SIN_RESPUESTA``.
    """

    async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso: ...


@dataclass(frozen=True, slots=True)
class ResumenDeHerramienta:
    """Lo que el operador remoto ve de una herramienta antes de aprobar la corrida."""

    tool: str
    version: str
    exe_sha256: str
    preset: str
    preset_huella: str
    preset_de_calidad: str | None
    output_esperado: str


@dataclass(frozen=True, slots=True)
class SolicitudAprobacionDesatendida:
    """Pedido de aprobación PRE-spawn: la configuración exacta que va a correr."""

    herramientas: tuple[ResumenDeHerramienta, ...]
    preset_pedido: str
    run_texgen: bool
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SolicitudModalConocido:
    """Pedido de aprobación MID-RUN para continuar un modal medido."""

    tool: str
    pid: int
    modal_id: str
    instruccion: str
    descripcion: str
    timeout_seconds: float


class AprobadorDesatendido(Protocol):
    """Puerto hacia el operador REMOTO (Telegram vía ``HITLGuard``).

    Reemplaza formalmente el significado del HITL asistido: en vez de "terminé de
    configurar la GUI" el operador aprueba (a) la configuración exacta con la que
    va a correr cada herramienta y (b) continuar un modal medido.
    """

    async def aprobar_configuracion(self, solicitud: SolicitudAprobacionDesatendida) -> ResultadoConfirmacion: ...

    async def aprobar_modal(self, solicitud: SolicitudModalConocido) -> ResultadoConfirmacion: ...

    async def informar(self, *, tool: str, mensaje: str) -> None: ...


async def consultar_aprobador(
    consulta: Callable[[], Awaitable[ResultadoConfirmacion]],
    *,
    timeout_segundos: float,
    gracia_segundos: float,
) -> ResultadoConfirmacion:
    """Acota el canal humano y aplasta sus fallos a fail-closed.

    Mismo contrato que ``DynDOLODRunner._confirmar_acotado``: un adaptador roto
    o un valor fuera del enum es ``CANAL_NO_DISPONIBLE``, jamás "seguir".
    """
    try:
        resultado = await asyncio.wait_for(consulta(), timeout=timeout_segundos + gracia_segundos)
    except TimeoutError:
        return ResultadoConfirmacion.TIMEOUT
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- canal humano: cualquier fallo es "no disponible", nunca "seguir"
        logger.warning("el aprobador desatendido falló; se corta fail-closed", exc_info=True)
        return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
    if not isinstance(resultado, ResultadoConfirmacion):
        return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
    return resultado


async def informar_sin_gatear(aprobador: AprobadorDesatendido, *, tool: str, mensaje: str, cota: float) -> None:
    """Aviso best-effort al operador: nunca corta una corrida."""
    try:
        await asyncio.wait_for(aprobador.informar(tool=tool, mensaje=mensaje), timeout=cota)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- aviso best-effort, nunca gatea
        logger.warning("no se pudo avisar al operador sobre %s", tool, exc_info=True)


@dataclass(frozen=True, slots=True)
class CapacidadDesatendidaUIA:
    """Paquete inyectable del modo desatendido. Se cablea UNA vez en el composition root.

    Su ausencia en el runner NO es un permiso: una corrida que pide
    ``DESATENDIDO`` sin capacidad falla con ``UNATTENDED_CAPABILITY_UNAVAILABLE``.

    Los plazos por defecto cubren un operador REMOTO: 15 minutos para contestar
    una aprobación por Telegram, 5 para que la GUI quede lista (el gate read-only
    usa el mismo), y ventanas de observación de 2 minutos por helper durante una
    generación que puede durar horas (el techo lo pone el timeout global de la
    corrida, no el driver).
    """

    ejecutor: EjecutorDePasoUIA
    aprobador: AprobadorDesatendido
    #: Estado durable de los snapshots de presets (P8). ``None`` = no hay dónde
    #: garantizar el restore, y una corrida desatendida falla cerrada.
    directorio_de_presets: pathlib.Path | None = None
    arranque_timeout_segundos: float = 300.0
    observacion_intervalo_segundos: float = 1.0
    ventana_de_generacion_segundos: float = 120.0
    generacion_intervalo_segundos: float = 3.0
    confirmacion_de_inicio_segundos: float = 300.0
    confirmacion_intervalo_segundos: float = 3.0
    cierre_timeout_segundos: float = 180.0
    aprobacion_timeout_segundos: float = 900.0
    gracia_externa_segundos: float = 15.0
    #: Presupuesto de la reobservación acotada previa a la ÚNICA primitiva mutante
    #: de un paso: el Invoke de ``INVOCAR`` (F2, #773) y la escritura de
    #: ``ESCRIBIR_OUTPUT`` (F2b, #781). Es el plazo del paso: el helper lo gasta
    #: releyendo huecos de repintado y el ejecutor lo usa como base de su deadline
    #: duro. 15 s cubren de sobra un repintado; lo que no cubre es un ``CARGANDO``
    #: persistente, y eso corta cerrado.
    reobservacion_timeout_segundos: float = 15.0
    fallos_de_observacion_tolerados: int = 3

    def __post_init__(self) -> None:
        # Enumeración explícita y no un bucle con acceso dinámico: cada plazo se
        # valida por su nombre, y un plazo nuevo sin validar es visible en el diff.
        exigir_plazo_positivo_finito(self.arranque_timeout_segundos, "arranque_timeout_segundos")
        exigir_plazo_positivo_finito(self.observacion_intervalo_segundos, "observacion_intervalo_segundos")
        exigir_plazo_positivo_finito(self.ventana_de_generacion_segundos, "ventana_de_generacion_segundos")
        exigir_plazo_positivo_finito(self.generacion_intervalo_segundos, "generacion_intervalo_segundos")
        exigir_plazo_positivo_finito(self.confirmacion_de_inicio_segundos, "confirmacion_de_inicio_segundos")
        exigir_plazo_positivo_finito(self.confirmacion_intervalo_segundos, "confirmacion_intervalo_segundos")
        exigir_plazo_positivo_finito(self.cierre_timeout_segundos, "cierre_timeout_segundos")
        exigir_plazo_positivo_finito(self.aprobacion_timeout_segundos, "aprobacion_timeout_segundos")
        exigir_plazo_positivo_finito(self.gracia_externa_segundos, "gracia_externa_segundos")
        exigir_plazo_positivo_finito(self.reobservacion_timeout_segundos, "reobservacion_timeout_segundos")
        if isinstance(self.fallos_de_observacion_tolerados, bool) or self.fallos_de_observacion_tolerados < 1:
            raise ValueError("fallos_de_observacion_tolerados tiene que ser un entero >= 1")


# =============================================================================
# Ledger exactly-once + conductor
# =============================================================================


@dataclass(frozen=True, slots=True)
class EntradaDeLedger:
    accion_id: str
    estado: EstadoDePaso
    invocado: bool
    hresult: str | None


class LedgerDeAcciones:
    """Exactly-once: una acción reservada jamás se vuelve a pedir, aun ambigua."""

    def __init__(self) -> None:
        self._reservadas: set[str] = set()
        self.entradas: list[EntradaDeLedger] = []

    def reservar(self, accion_id: str, *, tool: str) -> None:
        if accion_id in self._reservadas:
            raise DesatendidoError(
                CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                f"la acción {accion_id!r} ya fue intentada en esta corrida: no se re-invoca",
                tool=tool,
            )
        self._reservadas.add(accion_id)

    def registrar(self, accion_id: str, resultado: ResultadoDePaso) -> None:
        self.entradas.append(
            EntradaDeLedger(
                accion_id=accion_id,
                estado=resultado.estado,
                invocado=resultado.invocado,
                hresult=resultado.hresult,
            )
        )

    def intentadas(self) -> tuple[str, ...]:
        return tuple(entrada.accion_id for entrada in self.entradas)


@dataclass(frozen=True, slots=True)
class ContextoDeConduccion:
    """Lo que el runner sabe de la instancia que el conductor va a operar."""

    contrato: ContratoDeUIHerramienta
    pid: int
    ejecutable: str
    output_esperado: str
    raiz_output: pathlib.Path
    ruta_log: pathlib.Path | None


def _contar_archivos(raiz: pathlib.Path) -> int:
    total = 0
    for _actual, _directorios, archivos in os.walk(raiz):
        total += len(archivos)
    return total


def _tamano(ruta: pathlib.Path | None) -> int:
    if ruta is None:
        return 0
    try:
        return ruta.stat().st_size
    except OSError:
        return 0


class ConductorDesatendido:
    """Lleva la GUI de una instancia desde el spawn hasta su cierre. Fail-closed.

    Dos fases públicas que el runner llama en orden y entre las que no hay
    mutación: :meth:`conducir_hasta_inicio` (arranque → Begin confirmado) y
    :meth:`esperar_fin_y_cerrar` (observación → acción terminal → proceso
    saliendo). Todo corte es un :class:`DesatendidoError` con su código.
    """

    def __init__(self, capacidad: CapacidadDesatendidaUIA, *, dormir: Callable[[float], Awaitable[None]] | None = None):
        self._capacidad = capacidad
        self._dormir = dormir if dormir is not None else asyncio.sleep

    # -- infraestructura -------------------------------------------------------

    def _pedido(
        self,
        ctx: ContextoDeConduccion,
        tipo: TipoDePaso,
        *,
        timeout_segundos: float,
        intervalo_segundos: float | None = None,
        fase: FaseDeObservacion | None = None,
        accion_esperada: str | None = None,
        valor: str | None = None,
        accion_id: str | None = None,
        precondicion_output: str | None = None,
        modal_id: str | None = None,
    ) -> PedidoDePaso:
        return PedidoDePaso(
            tipo=tipo,
            tool=ctx.contrato.tool,
            exe_sha256=ctx.contrato.exe_sha256,
            modo_de_juego=ctx.contrato.modo_de_juego,
            pid=ctx.pid,
            ejecutable=ctx.ejecutable,
            timeout_segundos=timeout_segundos,
            intervalo_segundos=(
                intervalo_segundos if intervalo_segundos is not None else self._capacidad.observacion_intervalo_segundos
            ),
            fase=fase,
            accion_esperada=accion_esperada,
            valor=valor,
            accion_id=accion_id,
            precondicion_output=precondicion_output,
            modal_id=modal_id,
        )

    def _error(self, ctx: ContextoDeConduccion, codigo: CodigoDesatendido, detalle: str) -> DesatendidoError:
        return DesatendidoError(codigo, detalle, tool=ctx.contrato.tool)

    def _error_de_identidad(self, ctx: ContextoDeConduccion) -> DesatendidoError:
        """El corte por identidad, con UN código y UN texto en todo el conductor.

        Siete sitios interpretan un ``ResultadoDePaso``. Un drift del proceso no
        es "el mapa de controles cambió": es que el proceso que se está tocando
        ya no es el autorizado. Que el código dependiera de por dónde llegó el
        paso era un defecto de familia, no una decisión (F2, #773).
        """
        return self._error(
            ctx,
            CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
            "el pid observado no es el ejecutable autorizado",
        )

    def _pantalla(self, ctx: ContextoDeConduccion, resultado: ResultadoDePaso) -> Pantalla | None:
        """El padre RE-CLASIFICA: el veredicto del helper no es autoridad."""
        if resultado.instantanea is None:
            return None
        if resultado.instantanea.pid != ctx.pid:
            raise self._error(
                ctx,
                CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                f"el helper devolvió una instantánea del pid {resultado.instantanea.pid}, no del {ctx.pid}",
            )
        return clasificar_pantalla(ctx.contrato, resultado.instantanea)

    async def _observar(
        self,
        ctx: ContextoDeConduccion,
        fase: FaseDeObservacion,
        *,
        accion_esperada: str | None,
        timeout_segundos: float,
        intervalo_segundos: float,
    ) -> ResultadoDePaso:
        return await self._capacidad.ejecutor.ejecutar(
            self._pedido(
                ctx,
                TipoDePaso.OBSERVAR,
                fase=fase,
                accion_esperada=accion_esperada,
                timeout_segundos=timeout_segundos,
                intervalo_segundos=intervalo_segundos,
            )
        )

    async def _invocar(
        self,
        ctx: ContextoDeConduccion,
        ledger: LedgerDeAcciones,
        accion: AccionDeUI,
        *,
        precondicion_output: str | None = None,
        modal_id: str | None = None,
    ) -> ResultadoDePaso:
        ledger.reservar(accion.id, tool=ctx.contrato.tool)
        resultado = await self._capacidad.ejecutor.ejecutar(
            self._pedido(
                ctx,
                TipoDePaso.INVOCAR,
                accion_id=accion.id,
                precondicion_output=precondicion_output,
                modal_id=modal_id,
                # El paso puede gastar este plazo releyendo huecos de observación
                # antes del ÚNICO Invoke (F2, #773); no es un reintento del padre.
                timeout_segundos=self._capacidad.reobservacion_timeout_segundos,
            )
        )
        ledger.registrar(accion.id, resultado)
        logger.info(
            "UIA desatendido: %s %s → %s (invocado=%s hresult=%s)",
            ctx.contrato.tool,
            accion.id,
            resultado.estado.value,
            resultado.invocado,
            resultado.hresult,
        )
        return resultado

    def _desenlace_desconocido(self, resultado: ResultadoDePaso) -> bool:
        """¿Pudo haber ocurrido la acción sin que lo sepamos?"""
        return resultado.estado in {EstadoDePaso.SIN_RESPUESTA, EstadoDePaso.MECANISMO_FALLO} or (
            resultado.invocado and resultado.estado is not EstadoDePaso.OK
        )

    # -- fase 1: arranque → Begin ---------------------------------------------

    async def _esperar_asistente(
        self,
        ctx: ContextoDeConduccion,
        ledger: LedgerDeAcciones,
        *,
        accion_esperada: AccionDeUI,
        modales_atendidos: set[str],
        admite_modales: bool,
    ) -> ResultadoDePaso:
        """Observa hasta el asistente listo, atendiendo modales conocidos con aprobación remota."""
        capacidad = self._capacidad
        limite = time.monotonic() + capacidad.arranque_timeout_segundos
        while True:
            restante = limite - time.monotonic()
            if restante <= 0:
                raise self._error(
                    ctx,
                    CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH,
                    f"el asistente con {accion_esperada.id} no quedó listo en {capacidad.arranque_timeout_segundos:.0f}s",
                )
            resultado = await self._observar(
                ctx,
                FaseDeObservacion.ARRANQUE,
                accion_esperada=accion_esperada.id,
                timeout_segundos=restante,
                intervalo_segundos=capacidad.observacion_intervalo_segundos,
            )
            if resultado.estado is EstadoDePaso.PROCESO_AUSENTE:
                raise self._error(ctx, CodigoDesatendido.PROCESS_DIED_PRE_BEGIN, "el proceso terminó antes del Begin")
            if resultado.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
                raise self._error_de_identidad(ctx)
            if resultado.estado in {EstadoDePaso.UIA_NO_DISPONIBLE, EstadoDePaso.SIN_RESPUESTA}:
                raise self._error(ctx, CodigoDesatendido.UIA_UNAVAILABLE, resultado.detalle or "sensor UIA caído")
            if resultado.estado is EstadoDePaso.PRECONDICION_FALLIDA:
                raise self._error(ctx, CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION, resultado.detalle)
            pantalla = self._pantalla(ctx, resultado)
            if resultado.estado is EstadoDePaso.PLAZO_AGOTADO:
                if (
                    pantalla is not None
                    and pantalla.tipo is TipoDePantalla.ASISTENTE
                    and accion_esperada.id in pantalla.acciones_visibles
                    and accion_esperada.id not in pantalla.acciones_habilitadas
                ):
                    raise self._error(
                        ctx,
                        CodigoDesatendido.BEGIN_NOT_ENABLED,
                        f"{accion_esperada.id} visible pero deshabilitado al vencer el plazo de arranque",
                    )
                raise self._error(
                    ctx,
                    CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH,
                    f"plazo de arranque agotado: {pantalla.tipo.value if pantalla else 'sin instantánea'} "
                    f"{pantalla.detalle if pantalla else resultado.detalle}".strip(),
                )
            if pantalla is None:
                raise self._error(ctx, CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION, "observación sin instantánea")
            if pantalla.tipo is TipoDePantalla.ASISTENTE:
                if (
                    accion_esperada.id in pantalla.acciones_habilitadas
                    and resultado.instantanea is not None
                    and resultado.instantanea.valor_output is not None
                ):
                    return resultado
                raise self._error(
                    ctx,
                    CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                    "el helper declaró decisivo un asistente que no cumple la condición de arranque",
                )
            if pantalla.tipo is TipoDePantalla.MODAL_CONOCIDO and pantalla.modal is not None and admite_modales:
                await self._atender_modal(ctx, ledger, pantalla.modal, modales_atendidos)
                # La espera humana (hasta `aprobacion_timeout_segundos`) no cuenta
                # contra el arranque de la GUI: lo que sigue al modal es una fase
                # de arranque nueva. Acotado: cada modal se atiende una sola vez
                # (`modales_atendidos`), así que el plazo se reinicia a lo sumo
                # una vez por modal conocido del contrato.
                limite = time.monotonic() + capacidad.arranque_timeout_segundos
                continue
            if pantalla.tipo in {TipoDePantalla.MODAL_CONOCIDO, TipoDePantalla.DIALOGO_DESCONOCIDO}:
                raise self._error(ctx, CodigoDesatendido.UNEXPECTED_MODAL, pantalla.describir_dialogo())
            raise self._error(
                ctx,
                CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH,
                f"pantalla inesperada en el arranque: {pantalla.tipo.value} {pantalla.detalle}".strip(),
            )

    async def _atender_modal(
        self,
        ctx: ContextoDeConduccion,
        ledger: LedgerDeAcciones,
        modal: ModalConocido,
        modales_atendidos: set[str],
    ) -> None:
        if modal.id in modales_atendidos:
            raise self._error(
                ctx,
                CodigoDesatendido.UNEXPECTED_MODAL,
                f"el modal {modal.id!r} reapareció después de continuarlo",
            )
        capacidad = self._capacidad
        decision = await consultar_aprobador(
            lambda: capacidad.aprobador.aprobar_modal(
                SolicitudModalConocido(
                    tool=ctx.contrato.tool,
                    pid=ctx.pid,
                    modal_id=modal.id,
                    instruccion=modal.firma.instruccion,
                    descripcion=modal.descripcion,
                    timeout_seconds=capacidad.aprobacion_timeout_segundos,
                )
            ),
            timeout_segundos=capacidad.aprobacion_timeout_segundos,
            gracia_segundos=capacidad.gracia_externa_segundos,
        )
        if decision is not ResultadoConfirmacion.APROBADA:
            raise self._error(
                ctx,
                CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED,
                f"el operador no aprobó continuar {modal.id!r}: {decision.value}",
            )
        resultado = await self._invocar(ctx, ledger, modal.accion_continuar, modal_id=modal.id)
        if resultado.estado is EstadoDePaso.OK:
            modales_atendidos.add(modal.id)
            return
        if resultado.estado is EstadoDePaso.PROCESO_AUSENTE:
            raise self._error(ctx, CodigoDesatendido.PROCESS_DIED_PRE_BEGIN, "el proceso terminó con el modal abierto")
        if resultado.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
            raise self._error_de_identidad(ctx)
        if self._desenlace_desconocido(resultado):
            raise self._error(
                ctx,
                CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                f"desenlace desconocido al continuar {modal.id!r} ({resultado.estado.value} {resultado.hresult}); "
                "no se reintenta",
            )
        raise self._error(ctx, CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH, resultado.detalle)

    async def _converger_output(self, ctx: ContextoDeConduccion, valor_actual: str | None) -> None:
        if output_coincide(valor_actual, ctx.output_esperado):
            return
        resultado = await self._capacidad.ejecutor.ejecutar(
            self._pedido(
                ctx,
                TipoDePaso.ESCRIBIR_OUTPUT,
                valor=ctx.output_esperado,
                # El paso puede gastar este plazo releyendo huecos de observación
                # antes de la ÚNICA escritura (F2b, #781). Es el MISMO presupuesto
                # que el hermano ``_invocar`` recibe desde F2 (#773): hasta F2b acá
                # se pasaba ``observacion_intervalo_segundos`` (1 s), así que aun
                # con la reobservación implementada el hueco de repintado no
                # entraba y el fix no servía.
                timeout_segundos=self._capacidad.reobservacion_timeout_segundos,
            )
        )
        if resultado.estado is EstadoDePaso.PROCESO_AUSENTE:
            raise self._error(ctx, CodigoDesatendido.PROCESS_DIED_PRE_BEGIN, "el proceso terminó antes de escribir")
        if resultado.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
            raise self._error_de_identidad(ctx)
        if resultado.estado is EstadoDePaso.PRECONDICION_FALLIDA:
            raise self._error(ctx, CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH, resultado.detalle)
        if resultado.estado is EstadoDePaso.PLAZO_AGOTADO:
            # Corte LIMPIO: el plazo venció antes de la primitiva, así que no se
            # escribió nada y no hay desenlace desconocido que reportar. Va ANTES
            # del chequeo de desenlace desconocido a propósito: si algún día ese
            # predicado se ampliara, este estado no puede quedar rotulado como
            # "la escritura pudo haberse ejecutado". El código es el mismo que usa
            # ``_esperar_asistente`` cuando vence su propio plazo de arranque.
            raise self._error(
                ctx,
                CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH,
                f"plazo agotado antes de escribir el Output: {resultado.detalle}",
            )
        if resultado.estado is EstadoDePaso.MECANISMO_FALLO:
            raise self._error(
                ctx,
                CodigoDesatendido.OUTPUT_WRITE_UNSUPPORTED,
                f"{ctx.contrato.mecanismo_output.value} devolvió {resultado.hresult}; no se prueba otro mecanismo",
            )
        if self._desenlace_desconocido(resultado):
            # Sin respuesta del helper (o invocado con estado no-OK) la escritura
            # PUDO haber ocurrido: el corte lo dice y no se reintenta. Antes caía
            # en el ``UIA_UNAVAILABLE`` de abajo, que afirma "no pude mirar" sobre
            # un paso que quizá ya mutó el campo del usuario (F2b, #781).
            raise self._error(
                ctx,
                CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                f"desenlace desconocido al escribir el Output ({resultado.estado.value} "
                f"{resultado.hresult or ''} {resultado.detalle}".strip()
                + "); la escritura pudo haberse ejecutado y no se reintenta",
            )
        if resultado.estado is not EstadoDePaso.OK:
            raise self._error(ctx, CodigoDesatendido.UIA_UNAVAILABLE, resultado.detalle or resultado.estado.value)
        if not output_coincide(resultado.valor_leido, ctx.output_esperado):
            raise self._error(
                ctx,
                CodigoDesatendido.OUTPUT_READBACK_MISMATCH,
                f"readback {resultado.valor_leido!r} != esperado {ctx.output_esperado!r}",
            )

    async def _linea_base_de_progreso(self, ctx: ContextoDeConduccion) -> tuple[int, int]:
        return await asyncio.to_thread(lambda: (_contar_archivos(ctx.raiz_output), _tamano(ctx.ruta_log)))

    async def _confirmar_inicio(
        self,
        ctx: ContextoDeConduccion,
        linea_base: tuple[int, int],
        proceso_vivo: Callable[[], bool],
    ) -> None:
        """Begin confirmado = vivo AND (creció el root administrado OR creció el log)."""
        capacidad = self._capacidad
        limite = time.monotonic() + capacidad.confirmacion_de_inicio_segundos
        while True:
            if not proceso_vivo():
                # Salió solo después del Begin: el post-check decide (crash o éxito).
                return
            archivos, log = await self._linea_base_de_progreso(ctx)
            if archivos > linea_base[0] or log > linea_base[1]:
                return
            if time.monotonic() >= limite:
                raise self._error(
                    ctx,
                    CodigoDesatendido.BEGIN_NOT_CONFIRMED,
                    f"sin progreso observable {capacidad.confirmacion_de_inicio_segundos:.0f}s después del Begin",
                )
            await self._dormir(capacidad.confirmacion_intervalo_segundos)

    async def conducir_hasta_inicio(
        self,
        ctx: ContextoDeConduccion,
        ledger: LedgerDeAcciones,
        *,
        proceso_vivo: Callable[[], bool],
    ) -> None:
        """Arranque → (modales) → Output → (Advanced → reconvergencia) → Begin confirmado."""
        contrato = ctx.contrato
        modales_atendidos: set[str] = set()
        primera = contrato.accion_previa if contrato.accion_previa is not None else contrato.accion_inicio
        resultado = await self._esperar_asistente(
            ctx,
            ledger,
            accion_esperada=primera,
            modales_atendidos=modales_atendidos,
            admite_modales=True,
        )
        await self._converger_output(ctx, resultado.instantanea.valor_output if resultado.instantanea else None)

        if contrato.accion_previa is not None:
            previa = await self._invocar(ctx, ledger, contrato.accion_previa)
            if previa.estado is EstadoDePaso.PROCESO_AUSENTE:
                raise self._error(ctx, CodigoDesatendido.PROCESS_DIED_PRE_BEGIN, "el proceso terminó antes del Begin")
            if previa.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
                raise self._error_de_identidad(ctx)
            if previa.estado is not EstadoDePaso.OK:
                codigo = (
                    CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION
                    if self._desenlace_desconocido(previa)
                    else CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH
                )
                raise self._error(ctx, codigo, f"{contrato.accion_previa.id}: {previa.detalle}")
            # Advanced recarga el Output desde el preset (medido): se re-observa y
            # se reconverge ANTES del Begin. Un modal acá no está medido → corte.
            resultado = await self._esperar_asistente(
                ctx,
                ledger,
                accion_esperada=contrato.accion_inicio,
                modales_atendidos=modales_atendidos,
                admite_modales=False,
            )
            await self._converger_output(ctx, resultado.instantanea.valor_output if resultado.instantanea else None)

        linea_base = await self._linea_base_de_progreso(ctx)
        inicio = await self._invocar(ctx, ledger, contrato.accion_inicio, precondicion_output=ctx.output_esperado)
        if inicio.estado is not EstadoDePaso.OK:
            if inicio.estado is EstadoDePaso.PROCESO_AUSENTE and not inicio.invocado:
                raise self._error(ctx, CodigoDesatendido.PROCESS_DIED_PRE_BEGIN, "el proceso terminó antes del Begin")
            if inicio.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
                raise self._error_de_identidad(ctx)
            if self._desenlace_desconocido(inicio):
                raise self._error(
                    ctx,
                    CodigoDesatendido.BEGIN_OUTCOME_UNKNOWN,
                    f"{inicio.estado.value} {inicio.hresult or ''} {inicio.detalle}".strip() + "; no se reintenta",
                )
            if inicio.motivo is MotivoDePrecondicion.CONTROL_DESHABILITADO:
                raise self._error(ctx, CodigoDesatendido.BEGIN_NOT_ENABLED, inicio.detalle)
            if inicio.motivo is MotivoDePrecondicion.OUTPUT_DIFIERE:
                raise self._error(ctx, CodigoDesatendido.OUTPUT_READBACK_MISMATCH, inicio.detalle)
            raise self._error(ctx, CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH, inicio.detalle or inicio.estado.value)
        await self._confirmar_inicio(ctx, linea_base, proceso_vivo)

    # -- fase 2: generación → terminal ----------------------------------------

    async def esperar_fin_y_cerrar(
        self,
        ctx: ContextoDeConduccion,
        ledger: LedgerDeAcciones,
        *,
        proceso_vivo: Callable[[], bool],
    ) -> None:
        """Sólo observa hasta el diálogo terminal; ahí invoca su acción una vez."""
        capacidad = self._capacidad
        fallos = 0
        while True:
            resultado = await self._observar(
                ctx,
                FaseDeObservacion.GENERACION,
                accion_esperada=None,
                timeout_segundos=capacidad.ventana_de_generacion_segundos,
                intervalo_segundos=capacidad.generacion_intervalo_segundos,
            )
            if resultado.estado is EstadoDePaso.PROCESO_AUSENTE:
                return
            if resultado.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
                raise self._error_de_identidad(ctx)
            if resultado.estado in {EstadoDePaso.UIA_NO_DISPONIBLE, EstadoDePaso.SIN_RESPUESTA}:
                fallos += 1
                if fallos >= capacidad.fallos_de_observacion_tolerados:
                    raise self._error(
                        ctx,
                        CodigoDesatendido.UIA_UNAVAILABLE,
                        f"{fallos} observaciones seguidas fallaron durante la generación: {resultado.detalle}",
                    )
                continue
            fallos = 0
            if resultado.estado is EstadoDePaso.PLAZO_AGOTADO:
                if not proceso_vivo():
                    return
                continue
            if resultado.estado is not EstadoDePaso.OK:
                raise self._error(ctx, CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION, resultado.detalle)
            pantalla = self._pantalla(ctx, resultado)
            if pantalla is None:
                raise self._error(ctx, CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION, "observación sin instantánea")
            if pantalla.tipo is TipoDePantalla.DIALOGO_TERMINAL:
                break
            if pantalla.tipo in {TipoDePantalla.MODAL_CONOCIDO, TipoDePantalla.DIALOGO_DESCONOCIDO}:
                raise self._error(
                    ctx,
                    CodigoDesatendido.UNEXPECTED_DIALOG_DURING_GENERATION,
                    pantalla.describir_dialogo(),
                )
            if pantalla.tipo is TipoDePantalla.AMBIGUA:
                raise self._error(ctx, CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH, pantalla.detalle)
            raise self._error(
                ctx,
                CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION,
                f"el helper declaró decisiva una pantalla {pantalla.tipo.value} durante la generación",
            )

        terminal = await self._invocar(ctx, ledger, ctx.contrato.accion_terminal)
        if terminal.estado is EstadoDePaso.PROCESO_AUSENTE and not terminal.invocado:
            return
        if terminal.estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE:
            raise self._error_de_identidad(ctx)
        if terminal.estado is not EstadoDePaso.OK:
            if self._desenlace_desconocido(terminal):
                raise self._error(
                    ctx,
                    CodigoDesatendido.TERMINAL_OUTCOME_UNKNOWN,
                    f"{terminal.estado.value} {terminal.hresult or ''} {terminal.detalle}".strip()
                    + "; no se reintenta",
                )
            raise self._error(ctx, CodigoDesatendido.TERMINAL_CONTROL_MISMATCH, terminal.detalle)
        limite = time.monotonic() + capacidad.cierre_timeout_segundos
        while proceso_vivo():
            if time.monotonic() >= limite:
                raise self._error(
                    ctx,
                    CodigoDesatendido.TERMINAL_OUTCOME_UNKNOWN,
                    f"{ctx.contrato.accion_terminal.id} devolvió S_OK y el proceso sigue vivo tras "
                    f"{capacidad.cierre_timeout_segundos:.0f}s",
                )
            await self._dormir(min(1.0, capacidad.confirmacion_intervalo_segundos))
