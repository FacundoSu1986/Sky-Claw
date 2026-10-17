"""Backend Windows MUTANTE de UI Automation para la etapa 9 desatendida (#661 P3).

**El único archivo del paquete autorizado a nombrar las primitivas mutantes.**
``LegacyIAccessiblePattern.SetValue``, ``ValuePattern.SetValue`` (presente por
contrato, no usado por Alpha-209) e ``InvokePattern.Invoke``. El ancla por AST
``tests/test_dyndolod_uia_driver_superficie.py`` recorre TODO ``sky_claw/`` y
falla si esos nombres aparecen en cualquier otro módulo, o si este módulo nombra
cualquier otra primitiva (foco, teclado, mouse, mensajes Win32, ``getattr``).
La superficie read-only (``dyndolod_uia_preflight`` / ``dyndolod_uia_windows``
/ ``dyndolod_uia_gate``) sigue sin poder nombrarlas.

**Las llamadas son las MEDIDAS, no unas parecidas.** Replican la forma que la
sonda P0-B ejecutó en el rig Alpha-209
(``docs/validation/2026-10-02_p0_uia_alpha209/probe/p0a_probe.py`` /
``p0b_probe.py``): ``GetCurrentPattern(id)`` + ``QueryInterface(interfaz)`` y la
llamada del patrón, con el HRESULT devuelto como texto. **No verificado en
runtime productivo:** este módulo no corrió contra los binarios reales (el
rig de aceptación de #661 sigue pendiente); lo que lo respalda es que es la
misma secuencia COM que sí corrió.

**Sin fallback, sin reintento.** :meth:`BackendUIAWindows.escribir_output`
recibe EL mecanismo del contrato y no prueba otro; :meth:`BackendUIAWindows.invocar`
llama a ``Invoke`` una vez. Un error COM vuelve como HRESULT y el conductor
decide (desenlace desconocido, sin reintento).

**Import-safe en Linux.** ``comtypes`` se importa perezoso en ``__init__``; fuera
de Windows la construcción lanza :class:`UIANoDisponibleError` y el paso del
helper responde ``UIA_NO_DISPONIBLE`` sin actuar.

**Un hilo, un apartamento.** Igual que el observador read-only: el helper
construye el backend en el hilo que lo usa y lo libera en su ``finally``.
"""

from __future__ import annotations

import sys
from typing import Any

from sky_claw.local.tools.dyndolod_uia_contrato import MecanismoDeEscritura
from sky_claw.local.tools.dyndolod_uia_driver import HRESULT_OK, ElementoUI, InstantaneaUI, VentanaUI
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ObservacionUIAError,
    UIANoDisponibleError,
    exigir_enumeracion_completa,
)
from sky_claw.local.tools.dyndolod_uia_windows import (
    CLSID_CUIAUTOMATION,
    NOMBRES_DE_CONTROL_TYPE,
    primer_texto_no_vacio,
)

#: Propiedades que la instantánea materializa por elemento, en bloque vía
#: ``CacheRequest`` (mismo idioma que H3-A en el backend read-only).
PROPIEDADES_DE_LA_INSTANTANEA: tuple[str, ...] = (
    "UIA_AutomationIdPropertyId",
    "UIA_NamePropertyId",
    "UIA_ControlTypePropertyId",
    "UIA_ClassNamePropertyId",
    "UIA_IsEnabledPropertyId",
)

#: Texto que devuelven las primitivas cuando el control no expone el patrón:
#: no es un HRESULT de COM, pero tampoco es ``S_OK``, así que el conductor lo
#: trata como fallo del mecanismo (nunca como éxito).
PATRON_NO_DISPONIBLE = "PATRON_NO_DISPONIBLE"


def _texto(valor: object) -> str:
    return "" if valor is None else str(valor)


def _tipo_de_control(crudo: object) -> str:
    try:
        numero = int(crudo) if isinstance(crudo, int) else int(str(crudo))
    except (TypeError, ValueError):
        return str(crudo)
    return NOMBRES_DE_CONTROL_TYPE.get(numero, str(numero))


def hresult_de(exc: BaseException) -> str:
    """El HRESULT exacto como texto, como lo registró la sonda (``COMError 0x…``).

    ``COMError`` lleva ``(hresult, texto, detalles)`` en ``args``; un ``OSError``
    del runtime lleva su ``errno``. Nunca devuelve ``S_OK``: es el texto de un
    fallo y el conductor lo trata como tal.
    """
    if isinstance(exc, OSError):
        return f"OSError {exc.errno}"
    if exc.args and isinstance(exc.args[0], int):
        return f"COMError {hex(exc.args[0])}"
    return f"COMError {type(exc).__name__}"


class BackendUIAWindows:
    """Implementa ``BackendDeConduccion`` sobre ``comtypes``. Windows-only."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UIANoDisponibleError(f"UI Automation es Windows-only; esta plataforma es {sys.platform!r}")
        try:
            import comtypes  # noqa: PLC0415
            import comtypes.client  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover -- depende del rig
            raise UIANoDisponibleError("falta el binding COM `comtypes` (dependencia win32 de Sky-Claw)") from exc
        self._comtypes: Any = comtypes
        self._errores_com: tuple[type[BaseException], ...] = (comtypes.COMError, OSError)
        self._apartamento = False
        try:
            comtypes.CoInitialize()
            self._apartamento = True
            self._mod: Any = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia: Any = comtypes.client.CreateObject(CLSID_CUIAUTOMATION, interface=self._mod.IUIAutomation)
        except (comtypes.COMError, OSError, ImportError) as exc:  # pragma: no cover -- depende del rig
            self.liberar()
            raise UIANoDisponibleError(f"no se pudo inicializar UI Automation: {exc}") from exc

    def liberar(self) -> None:
        """Referencias primero, apartamento después (orden contractual de COM). Idempotente."""
        if not self._apartamento:
            return
        self._apartamento = False
        self._uia = None
        self._mod = None
        self._comtypes.CoUninitialize()

    # -- lectura ---------------------------------------------------------------

    def _id(self, nombre: str) -> Any:
        return self._mod.__dict__[nombre]

    def _cache_request(self) -> Any:
        solicitud = self._uia.CreateCacheRequest()
        for nombre in PROPIEDADES_DE_LA_INSTANTANEA:
            solicitud.AddProperty(self._id(nombre))
        solicitud.TreeScope = self._id("TreeScope_Element")
        solicitud.AutomationElementMode = self._id("AutomationElementMode_Full")
        return solicitud

    def _elementos(self, coleccion: Any, contexto: str) -> list[Any]:
        total = int(coleccion.Length)
        exigir_enumeracion_completa(total, contexto=contexto)
        return [coleccion.GetElement(indice) for indice in range(total)]

    def _describir(self, elemento: Any) -> ElementoUI:
        return ElementoUI(
            tipo_de_control=_tipo_de_control(elemento.GetCachedPropertyValue(self._id("UIA_ControlTypePropertyId"))),
            class_name=_texto(elemento.GetCachedPropertyValue(self._id("UIA_ClassNamePropertyId"))),
            nombre=_texto(elemento.GetCachedPropertyValue(self._id("UIA_NamePropertyId"))),
            automation_id=_texto(elemento.GetCachedPropertyValue(self._id("UIA_AutomationIdPropertyId"))),
            habilitado=bool(elemento.GetCachedPropertyValue(self._id("UIA_IsEnabledPropertyId"))),
            referencia=elemento,
        )

    def instantanea(self, pid: int) -> InstantaneaUI:
        """Ventanas top-level del pid y TODOS sus descendientes (o ``EnumeracionIncompletaError``)."""
        try:
            raiz = self._uia.GetRootElement()
            condicion = self._uia.CreatePropertyCondition(self._id("UIA_ProcessIdPropertyId"), pid)
            ventanas = self._elementos(raiz.FindAll(self._id("TreeScope_Children"), condicion), "ventanas")
            verdadera = self._uia.CreateTrueCondition()
            cache = self._cache_request()
            salida: list[VentanaUI] = []
            for ventana in ventanas:
                descendientes = self._elementos(
                    ventana.FindAllBuildCache(self._id("TreeScope_Descendants"), verdadera, cache),
                    "descendientes",
                )
                salida.append(
                    VentanaUI(
                        titulo=_texto(ventana.GetCurrentPropertyValue(self._id("UIA_NamePropertyId"))),
                        class_name=_texto(ventana.GetCurrentPropertyValue(self._id("UIA_ClassNamePropertyId"))),
                        elementos=tuple(self._describir(elemento) for elemento in descendientes),
                    )
                )
            return InstantaneaUI(pid=pid, ventanas=tuple(salida))
        except self._errores_com as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al tomar la instantánea del pid {pid}: {exc}") from exc

    def _patron(self, elemento: Any, nombre_id: str, interfaz: Any) -> Any | None:
        """``GetCurrentPattern`` + ``QueryInterface``; ``None`` si el control no lo expone."""
        try:
            crudo = elemento.GetCurrentPattern(self._id(nombre_id))
        except (*self._errores_com, ValueError):
            return None
        if crudo is None:
            return None
        try:
            return crudo.QueryInterface(interfaz)
        except (*self._errores_com, ValueError):
            return None

    def leer_output(self, elemento: ElementoUI) -> str | None:
        """Readback por ``ValuePattern`` y luego ``LegacyIAccessible`` (lo que la sonda leyó)."""
        referencia = elemento.referencia

        def _por_value() -> str | None:
            patron = self._patron(referencia, "UIA_ValuePatternId", self._mod.IUIAutomationValuePattern)
            return None if patron is None else _texto(patron.CurrentValue)

        def _por_legacy() -> str | None:
            patron = self._patron(
                referencia,
                "UIA_LegacyIAccessiblePatternId",
                self._mod.IUIAutomationLegacyIAccessiblePattern,
            )
            return None if patron is None else _texto(patron.CurrentValue)

        try:
            return primer_texto_no_vacio((_por_value, _por_legacy))
        except self._errores_com as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al leer el Output: {exc}") from exc

    # -- primitivas mutantes (las ÚNICAS del paquete) --------------------------

    def escribir_output(self, elemento: ElementoUI, mecanismo: MecanismoDeEscritura, valor: str) -> str:
        """Escribe por EL mecanismo del contrato. Devuelve el HRESULT; nunca prueba otro."""
        if mecanismo is MecanismoDeEscritura.LEGACY_IACCESSIBLE:
            patron = self._patron(
                elemento.referencia,
                "UIA_LegacyIAccessiblePatternId",
                self._mod.IUIAutomationLegacyIAccessiblePattern,
            )
        else:
            patron = self._patron(elemento.referencia, "UIA_ValuePatternId", self._mod.IUIAutomationValuePattern)
        if patron is None:
            return PATRON_NO_DISPONIBLE
        try:
            patron.SetValue(valor)
        except self._errores_com as exc:
            return hresult_de(exc)
        return HRESULT_OK

    def invocar(self, elemento: ElementoUI) -> str:
        """``InvokePattern.Invoke`` exactamente una vez. Devuelve el HRESULT."""
        patron = self._patron(elemento.referencia, "UIA_InvokePatternId", self._mod.IUIAutomationInvokePattern)
        if patron is None:
            return PATRON_NO_DISPONIBLE
        try:
            patron.Invoke()
        except self._errores_com as exc:
            return hresult_de(exc)
        return HRESULT_OK


def construir_backend_windows() -> BackendUIAWindows:
    """La fábrica que inyecta el helper. Se llama DENTRO del hilo que usa el backend."""
    return BackendUIAWindows()
