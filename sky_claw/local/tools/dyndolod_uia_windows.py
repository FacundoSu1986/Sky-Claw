"""Backend Windows READ-ONLY de UI Automation para TexGen/DynDOLOD (T5-v2).

**Qué es esto.** El adaptador COM que ``local_scripts/scripts/probe_dyndolod_uia_readonly.py``
llevó al rig T5A (2026-08-29) promovido a runtime: **el mismo código que se
ejercitó en la medición es el que producción usa** para el gate de Output
(``dyndolod_uia_gate`` / ``dyndolod_runner``). No hay una segunda copia "de
diagnóstico" y otra "productiva" divergiendo — la probe importa de acá.

**Read-only, como propiedad y no como recordatorio.** Cada llamada COM de este
módulo es una consulta: obtener la raíz, construir una condición, ``FindAll``
acotado, leer una propiedad, leer el valor de un patrón de lectura. No hay
ninguna que modifique el estado de la GUI — ni ``SetValue``, ni Invoke, ni
input sintético. El ancla por AST de ``tests/test_dyndolod_uia_preflight.py``
cubre ESTE archivo igual que al módulo de decisión: no puede siquiera nombrar
una primitiva mutante (ni ``getattr``/``setattr``/``eval``/``exec``, que son el
despacho dinámico que dejaría llegar a una sin que su nombre aparezca en el
árbol). Por eso el lookup de ids de propiedad es ``self._uia_mod.__dict__[...]``
y no ``getattr``.

**Import-safe en cualquier plataforma, por construcción.** El módulo se importa
en el CI de Ubuntu: nada de ``comtypes``/``ctypes`` a nivel de módulo. La
dependencia vive perezosa dentro de ``ObservadorUIAWindows.__init__``, donde su
ausencia (o la plataforma) sale como :class:`UIANoDisponibleError` — que el
preflight traduce a ``UNKNOWN`` — y nunca como un ``ImportError`` que revienta
la importación del paquete. El ancla
``test_el_adaptador_windows_no_importa_comtypes_en_tope_de_modulo`` lo congela.

**Ciclo de vida de COM: inicialización y cierre SIMÉTRICOS por ejecución del
gate, en el mismo hilo.** Cada construcción del adaptador hace ``CoInitialize``
y cada :meth:`ObservadorUIAWindows.liberar` hace su ``CoUninitialize`` — el
runtime exige el par aunque la segunda inicialización de un hilo reutilizado
del pool devuelva ``S_FALSE`` (esa llamada incrementa el balance igual).
``liberar`` primero suelta las referencias (``_uia``, el módulo generado) y
recién después desinicializa: el orden inverso es la forma documentada de
conseguir un crash en vez de una limpieza. El gate (``dyndolod_uia_gate``) la
invoca desde su ``finally`` vía :class:`ObservadorLiberable`, así que TODO
veredicto —y toda excepción— deja el hilo del pool como lo encontró. Lo que
sigue prohibido es usar UNA instancia desde DOS hilos: el apartamento STA no
es ajeno a eso, así que quien construya el adaptador lo construye en el hilo
que lo va a usar (el gate lo hace dentro del callable que manda al pool).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from sky_claw.local.tools.dyndolod_uia_preflight import (
    PID_ILEGIBLE,
    TOPE_DE_ELEMENTOS_UIA,
    ControlObservado,
    EnumeracionIncompletaError,
    ObservacionUIAError,
    UIANoDisponibleError,
    VentanaObservada,
    exigir_enumeracion_completa,
)

#: CLSID de ``CUIAutomation``, el objeto COM raíz de UI Automation.
CLSID_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

#: ``UIA_ControlTypePropertyId`` devuelve un entero. Se traducen los tipos que
#: pueden plausiblemente contener una ruta; el resto se reporta como su id crudo,
#: que sigue siendo evidencia utilizable.
#:
#: **Los ids están verificados contra los headers de UIA, no escritos de
#: memoria** — y hacía falta: la primera versión cruzaba `50007`/`50008` y
#: omitía `Document`. Un tipo mal traducido no es cosmético acá: el operador
#: ESCRIBE el selector a partir del volcado de la sonda, así que un `List` que en
#: realidad es un `ListItem` produce un selector que apunta al control
#: equivocado, y de ahí puede salir un MATCH que no es del campo Output.
#: Fuente de verificación: `uiautomation` 2.0.29 (`class ControlType`, generada
#: de los headers de UI Automation). Congelado en los tests por igualdad literal.
NOMBRES_DE_CONTROL_TYPE = {
    50000: "Button",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50007: "ListItem",
    50008: "List",
    50020: "Text",
    50026: "Group",
    50030: "Document",
    50032: "Window",
    50033: "Pane",
}


def primer_texto_no_vacio(lecturas: Iterable[Callable[[], str | None]]) -> str | None:
    """Primer texto NO VACÍO de una secuencia de lecturas perezosas, o ``None``.

    Vive acá, puro y sin COM, porque el ORDEN de los patrones de lectura y su
    caída son comportamiento que hay que poder testear. Tres anclas por AST
    intentaron fijarlo mirando la forma de `leer_valor` y las tres pasaron en
    verde con un defecto puesto: la última contaba `return None` como única
    salida temprana y no veía que `if valor is not None: return str(valor)`
    corta la lectura con `""`. La lección es dejar de adivinar la forma y hacer
    la conducta ejecutable. Hallazgo de review (Qodo).

    Vacío NO es una lectura: un ``ValuePattern`` que devuelve ``""`` en un Edit
    deshabilitado no puede impedir que se pruebe ``TextPattern``, que puede
    tener el texto. Si TODAS dan vacío, la respuesta es ``None`` —"no lo
    expone"— y el preflight responde ``UNKNOWN``.
    """
    for leer in lecturas:
        valor = leer()
        if valor:
            return valor
    return None


def describir_tolerando_fallos(
    elementos: Sequence[object],
    describir: Callable[[object], ControlObservado],
    errores: tuple[type[BaseException], ...],
) -> tuple[list[ControlObservado], int]:
    """``(descritos, cuántos fallaron)``. Un elemento roto NO aborta el volcado.

    Un control *stale* —invalidado por un repaint de la GUI a mitad de la
    enumeración, frecuente en árboles UIA grandes— hacía que la excepción se
    llevara puesto el volcado de la ventana ENTERA, y el operador se quedaba sin
    la evidencia de los controles que sí se habían leído. Que es exactamente el
    insumo que la sonda existe para producir. Hallazgo de review (Qodo).

    Los que fallan se CUENTAN, no se esconden: el volcado imprime el número.
    """
    descritos: list[ControlObservado] = []
    fallidos = 0
    for elemento in elementos:
        try:
            descritos.append(describir(elemento))
        except errores:
            fallidos += 1
    return descritos, fallidos


class ObservadorUIAWindows:
    """Adaptador READ-ONLY sobre UI Automation, vía ``comtypes``.

    Implementa los tres métodos de ``ObservadorUIA`` y ninguno más; el
    volcado diagnóstico (``controles_para_volcado`` / ``patrones_de_lectura``)
    es el que mantiene la sonda y **no** alimenta ningún veredicto.

    ``comtypes`` se importa dentro de ``__init__`` a propósito: importar este
    módulo en cualquier plataforma no debe fallar, y la ausencia del binding
    tiene que llegar como :class:`UIANoDisponibleError` —que el preflight
    traduce a ``UNKNOWN``— y no como un ``ImportError`` que reviente arriba.

    **Un hilo, un apartamento.** La instancia se crea en el hilo que la va a
    usar y no viaja a otro; el gate de T5-v2 la construye dentro del
    ``to_thread`` que ejecuta la observación entera.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UIANoDisponibleError(f"UI Automation es Windows-only; esta plataforma es {sys.platform!r}")
        try:
            import comtypes  # noqa: PLC0415
            import comtypes.client  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover -- depende del rig
            raise UIANoDisponibleError(
                "falta el binding COM: `comtypes` es dependencia declarada de Sky-Claw con marker "
                "win32 (T5-v2); en esta instalación no está. Reinstalá el entorno del rig."
            ) from exc
        # Los fallos que ESTE adaptador puede tener sin ser un bug suyo. Se
        # enumeran en vez de capturar `Exception`: `coding_conventions.md` §3 lo
        # prohíbe, y acá el motivo es concreto y no de estilo. Hay que poder
        # distinguir "COM falló" de "el adaptador tiene un typo": un `KeyError`
        # por un id de propiedad mal escrito disfrazado de "el control no expone
        # el patrón" haría que el operador elija un selector sobre evidencia
        # falsa — justo lo que la sonda existe para medir. `COMError` es la
        # excepción pública de comtypes (re-exportada de `_ctypes`); `OSError`
        # cubre `CoInitialize`, e `ImportError` la generación de módulo de
        # `GetModule`. Hallazgo de review (Qodo).
        self._errores_del_rig: tuple[type[BaseException], ...] = (comtypes.COMError, OSError)
        self._comtypes: Any = comtypes
        #: Bandera de ownership del apartamento: ``True`` sólo entre un
        #: ``CoInitialize`` exitoso y su ``CoUninitialize`` correspondiente.
        self._apartamento_inicializado = False
        try:
            comtypes.CoInitialize()
            self._apartamento_inicializado = True
            self._uia_mod: Any = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia: Any = comtypes.client.CreateObject(
                CLSID_CUIAUTOMATION,
                interface=self._uia_mod.IUIAutomation,
            )
        except (comtypes.COMError, OSError, ImportError) as exc:  # pragma: no cover -- depende del rig
            # Ownership también en el fracaso: si CoInitialize ya había
            # inicializado el apartamento, se desinicializa antes de propagar —
            # el ``S_FALSE`` de un hilo de pool reutilizado cuenta igual.
            self.liberar()
            raise UIANoDisponibleError(f"no se pudo inicializar UI Automation: {exc}") from exc

    def liberar(self) -> None:
        """Cierra el apartamento COM del hilo. Idempotente.

        Orden contractual (MSDN ``CoUninitialize``): PRIMERO se sueltan las
        referencias COM del objeto (``_uia``, el módulo generado con sus
        handles de patrón) y DESPUÉS se desinicializa el apartamento; al
        revés es un crash del runtime, no una limpieza. En CPython el
        ``= None`` decrementa la referencia en el acto — determinista, no a
        merced del GC.

        El gate la invoca desde su ``finally`` si el observador implementa
        :class:`ObservadorLiberable`, así que cada ejecución del gate deja el
        hilo del pool exactamente como lo encontró.
        """
        if not self._apartamento_inicializado:
            return
        self._apartamento_inicializado = False
        self._uia = None
        self._uia_mod = None
        self._comtypes.CoUninitialize()

    # -- lectura de propiedades ------------------------------------------------

    def _propiedad(self, elemento: object, nombre_de_id: str) -> object:
        identificador = self._uia_mod.__dict__[nombre_de_id]
        return elemento.GetCurrentPropertyValue(identificador)  # type: ignore[attr-defined]

    def _texto(self, elemento: object, nombre_de_id: str) -> str:
        valor = self._propiedad(elemento, nombre_de_id)
        return "" if valor is None else str(valor)

    def _control_type(self, elemento: object) -> str:
        crudo = self._propiedad(elemento, "UIA_ControlTypePropertyId")
        try:
            # ``crudo`` es un ``VARIANT`` de COM: suele venir como ``int``, pero
            # puede venir como string u otro tipo si el proveedor se lo da mal
            # al adaptador. ``isinstance(..., int)`` es la conversión más
            # razonable y la que no defiende dos veces errónea.
            numero = int(crudo) if isinstance(crudo, int) else int(str(crudo))
        except (TypeError, ValueError):
            return str(crudo)
        return NOMBRES_DE_CONTROL_TYPE.get(numero, str(numero))

    def _pid(self, elemento: object) -> int:
        crudo = self._propiedad(elemento, "UIA_ProcessIdPropertyId")
        try:
            return int(crudo) if isinstance(crudo, int) else int(str(crudo))
        except (TypeError, ValueError):
            return PID_ILEGIBLE

    def _elementos(self, coleccion: object) -> list[object]:
        """Materializa la colección ENTERA, o no materializa nada.

        `exigir_enumeracion_completa` lanza si no entra en la cota. No hay una
        rama que recorte: devolver los primeros N de N+1 le daría al decisor una
        unicidad que el árbol real no tiene. Para mirar un árbol grande está
        `_elementos_truncados`, que es sólo para el volcado y lo anuncia.
        """
        total = int(coleccion.Length)  # type: ignore[attr-defined]
        exigir_enumeracion_completa(total, contexto="elementos de UI Automation")
        return [coleccion.GetElement(indice) for indice in range(total)]  # type: ignore[attr-defined]

    def _elementos_truncados(self, coleccion: object) -> tuple[list[object], int]:
        """``(primeros N, total)`` para el VOLCADO. Nunca alimenta un veredicto."""
        total = int(coleccion.Length)  # type: ignore[attr-defined]
        mostrados = min(total, TOPE_DE_ELEMENTOS_UIA)
        return [coleccion.GetElement(indice) for indice in range(mostrados)], total  # type: ignore[attr-defined]

    def _coleccion_de_controles(self, ventana: VentanaObservada) -> object:
        condicion = self._uia.CreateTrueCondition()
        return ventana.handle.FindAll(self._uia_mod.TreeScope_Descendants, condicion)  # type: ignore[attr-defined]

    def _describir(self, elemento: object) -> ControlObservado:
        return ControlObservado(
            pid=self._pid(elemento),
            automation_id=self._texto(elemento, "UIA_AutomationIdPropertyId"),
            nombre=self._texto(elemento, "UIA_NamePropertyId"),
            tipo_de_control=self._control_type(elemento),
            class_name=self._texto(elemento, "UIA_ClassNamePropertyId"),
            handle=elemento,
        )

    # -- protocolo ObservadorUIA ----------------------------------------------

    def ventanas_de_proceso(self, pid: int) -> Sequence[VentanaObservada]:
        """Ventanas top-level del pid. Hijas directas de la raíz, no el Desktop entero."""
        try:
            raiz = self._uia.GetRootElement()
            condicion = self._uia.CreatePropertyCondition(self._uia_mod.UIA_ProcessIdPropertyId, pid)
            encontradas = raiz.FindAll(self._uia_mod.TreeScope_Children, condicion)
            return tuple(
                VentanaObservada(
                    pid=self._pid(elemento),
                    titulo=self._texto(elemento, "UIA_NamePropertyId"),
                    class_name=self._texto(elemento, "UIA_ClassNamePropertyId"),
                    handle=elemento,
                )
                for elemento in self._elementos(encontradas)
            )
        except EnumeracionIncompletaError:
            raise
        except self._errores_del_rig as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al enumerar ventanas del pid {pid}: {exc}") from exc

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]:
        """Descendientes de ESA ventana. Completos, o :class:`EnumeracionIncompletaError`."""
        try:
            encontrados = self._coleccion_de_controles(ventana)
            return tuple(self._describir(elemento) for elemento in self._elementos(encontrados))
        except EnumeracionIncompletaError:
            raise
        except self._errores_del_rig as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al enumerar controles de {ventana.titulo!r}: {exc}") from exc

    def controles_para_volcado(self, ventana: VentanaObservada) -> tuple[Sequence[ControlObservado], int, int]:
        """``(mostrados, total real, ilegibles)`` para el diagnóstico, nunca para decidir.

        Los ilegibles viajan en el CONTRATO y no en un atributo suelto: son una
        condición distinta de la truncación —un control *stale* no es un árbol
        que no entra en la cota— y confundirlas le haría creer al operador que
        el volcado se recortó cuando en realidad algo falló al leerse.
        """
        try:
            encontrados = self._coleccion_de_controles(ventana)
            elementos, total = self._elementos_truncados(encontrados)
            descritos, ilegibles = describir_tolerando_fallos(elementos, self._describir, self._errores_del_rig)
            return tuple(descritos), total, ilegibles
        except self._errores_del_rig as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al enumerar controles de {ventana.titulo!r}: {exc}") from exc

    def leer_valor(self, control: ControlObservado) -> str | None:
        """Valor del control por un patrón de LECTURA, o ``None`` si no expone ninguno.

        Se prueba ``ValuePattern`` y, si no está, ``TextPattern``. Ninguno de los
        dos modifica el control: ``ValuePattern`` también tiene una operación de
        escritura y acá simplemente no se usa. Si ninguno está disponible, la
        respuesta es ``None`` y el preflight responde ``UNKNOWN``: no se recurre
        al portapapeles ni al teclado para arrancar el dato por otra vía.
        """
        try:
            return primer_texto_no_vacio(
                (
                    lambda: self._texto_por_value_pattern(control),
                    lambda: self._texto_por_text_pattern(control),
                )
            )
        except self._errores_del_rig as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al leer el valor de {control.describir()}: {exc}") from exc

    def _texto_por_value_pattern(self, control: ControlObservado) -> str | None:
        """``ValuePattern.CurrentValue``, o ``None`` si el control no lo expone."""
        if not self._propiedad(control.handle, "UIA_IsValuePatternAvailablePropertyId"):
            return None
        patron = control.handle.GetCurrentPattern(self._uia_mod.UIA_ValuePatternId)  # type: ignore[attr-defined]
        if not patron:
            return None
        valor = patron.QueryInterface(self._uia_mod.IUIAutomationValuePattern).CurrentValue
        return None if valor is None else str(valor)

    def _texto_por_text_pattern(self, control: ControlObservado) -> str | None:
        """``TextPattern.DocumentRange.GetText``, o ``None`` si no lo expone."""
        if not self._propiedad(control.handle, "UIA_IsTextPatternAvailablePropertyId"):
            return None
        patron = control.handle.GetCurrentPattern(self._uia_mod.UIA_TextPatternId)  # type: ignore[attr-defined]
        if not patron:
            return None
        rango = patron.QueryInterface(self._uia_mod.IUIAutomationTextPattern).DocumentRange
        return str(rango.GetText(-1))

    def patrones_de_lectura(self, control: ControlObservado) -> str:
        """Qué patrones de lectura expone el control. Sólo para el volcado."""
        disponibles = []
        for etiqueta, propiedad in (
            ("ValuePattern", "UIA_IsValuePatternAvailablePropertyId"),
            ("TextPattern", "UIA_IsTextPatternAvailablePropertyId"),
            ("LegacyIAccessible", "UIA_IsLegacyIAccessiblePatternAvailablePropertyId"),
        ):
            try:
                if self._propiedad(control.handle, propiedad):
                    disponibles.append(etiqueta)
            except self._errores_del_rig:  # pragma: no cover -- depende del rig
                # Sólo un fallo del rig se reporta como `=?`. Un bug del
                # adaptador (un id mal escrito → `KeyError`) PROPAGA: si se
                # disfrazara de "no expone el patrón", el operador elegiría el
                # selector sobre evidencia inventada.
                disponibles.append(f"{etiqueta}=?")
        return ",".join(disponibles) or "(ninguno)"


def construir_observador_windows() -> ObservadorUIAWindows:
    """La fábrica que inyecta el runtime. Un punto, para que sea intercambiable.

    Existe como función (y no como "instanciá la clase en cada call site") para
    que el gate reciba UNA fábrica y los tests puedan sustituirla sin tocar la
    clase: la decisión de QUÉ backend se usa vive acá, una sola vez.

    La instancia se crea en el hilo llamante: COM ata el apartamento a ese hilo,
    así que quien la usa desde async la invoca dentro del ``to_thread`` que
    ejecuta la observación entera — nunca construirla en el hilo del loop y
    llamarla desde otro.
    """
    return ObservadorUIAWindows()
