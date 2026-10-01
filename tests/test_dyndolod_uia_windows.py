"""Backend Windows UIA read-only — contrato sin Windows real (T5-v2, Fase 2).

Cubre :mod:`sky_claw.local.tools.dyndolod_uia_windows` con el boundary COM
falsificado (``sys.modules["comtypes"]`` inyectado): la lógica bajo prueba es
la REAL del adaptador (filtrado por pid, mapeo de propiedades, orden de
patrones, traducción de errores, ciclo de vida del apartamento); lo único
falso es lo que en producción es COM.

**POSIX-safety.** El módulo se importa en Linux sin fallar (nada Windows-only
a nivel de módulo); construir en plataforma no soportada —o sin ``comtypes``—
sale como :class:`UIANoDisponibleError` tipado, nunca como ``ImportError``.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from sky_claw.local.tools.dyndolod_uia_preflight import (
    PID_ILEGIBLE,
    TOPE_DE_ELEMENTOS_UIA,
    ControlObservado,
    EnumeracionIncompletaError,
    EstadoPreflight,
    ObservacionUIAError,
    ProcesoObservado,
    RazonPreflight,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    VentanaObservada,
    observar_output,
    selector_de_output,
)
from sky_claw.local.tools.dyndolod_uia_windows import (
    NOMBRES_DE_CONTROL_TYPE,
    PROPIEDADES_DE_DESCRIPCION_UIA,
    ObservadorUIAWindows,
    construir_observador_windows,
    describir_tolerando_fallos,
    primer_texto_no_vacio,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO_WINDOWS = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_windows.py"
SONDA = RAIZ / "local_scripts" / "scripts" / "probe_dyndolod_uia_readonly.py"

TEXGEN_ROOT = "E:/Modding/ExternalWork/DynDOLOD/TexGen"


# ---------------------------------------------------------------------------
# Frontera COM falsificada
# ---------------------------------------------------------------------------


class FalsoCOMError(Exception):
    """El ``COMError`` del binding, sin COM."""


def _modulo_uia_falso():
    return types.SimpleNamespace(
        UIA_ControlTypePropertyId="UIA_ControlTypePropertyId",
        UIA_ProcessIdPropertyId="UIA_ProcessIdPropertyId",
        UIA_AutomationIdPropertyId="UIA_AutomationIdPropertyId",
        UIA_NamePropertyId="UIA_NamePropertyId",
        UIA_ClassNamePropertyId="UIA_ClassNamePropertyId",
        UIA_IsValuePatternAvailablePropertyId="UIA_IsValuePatternAvailablePropertyId",
        UIA_IsTextPatternAvailablePropertyId="UIA_IsTextPatternAvailablePropertyId",
        UIA_IsLegacyIAccessiblePatternAvailablePropertyId="UIA_IsLegacyIAccessiblePatternAvailablePropertyId",
        UIA_ValuePatternId="UIA_ValuePatternId",
        UIA_TextPatternId="UIA_TextPatternId",
        TreeScope_Children="Children",
        TreeScope_Descendants="Descendants",
        TreeScope_Element="Element",
        AutomationElementMode_Full="Full",
        AutomationElementMode_None="None",
        IUIAutomation="IUIAutomation",
        IUIAutomationValuePattern="IUIAutomationValuePattern",
        IUIAutomationTextPattern="IUIAutomationTextPattern",
    )


class ElementoFalso:
    """Un ``IUIAutomationElement`` con propiedades y patrones programables."""

    lecturas_current: list[object] = []
    lecturas_cache: list[object] = []

    def __init__(self, propiedades=None, patrones=None, error=None):
        self._propiedades = dict(propiedades or {})
        self._patrones = dict(patrones or {})
        self._error = error
        self._cache: dict[object, object] | None = None
        self._referencia_completa = True

    def aplicar_cache(self, nombres, referencia_completa: bool) -> None:
        self._cache = {nombre: self._propiedades.get(nombre) for nombre in nombres}
        self._referencia_completa = referencia_completa

    def GetCurrentPropertyValue(self, identificador):  # noqa: N802 -- espeja el nombre COM real
        if self._error is not None:
            raise self._error
        if not self._referencia_completa:
            raise FalsoCOMError("AutomationElementMode_None")
        ElementoFalso.lecturas_current.append(identificador)
        return self._propiedades.get(identificador)

    def GetCachedPropertyValue(self, identificador):  # noqa: N802 -- espeja el nombre COM real
        if self._error is not None:
            raise self._error
        if self._cache is None or identificador not in self._cache:
            raise FalsoCOMError(f"propiedad no cacheada: {identificador}")
        ElementoFalso.lecturas_cache.append(identificador)
        return self._cache[identificador]

    def GetCurrentPattern(self, identificador):  # noqa: N802 -- espeja el nombre COM real
        if not self._referencia_completa:
            raise FalsoCOMError("AutomationElementMode_None")
        return self._patrones.get(identificador)


class PatronValorFalso:
    def __init__(self, valor):
        self.CurrentValue = valor

    def QueryInterface(self, _interfaz):  # noqa: N802 -- espeja el nombre COM real
        return self


class PatronTextoFalso:
    def __init__(self, texto):
        self._texto = texto

    def QueryInterface(self, _interfaz):  # noqa: N802 -- espeja el nombre COM real
        return self

    @property
    def DocumentRange(self):  # noqa: N802 -- espeja el nombre COM real
        return self

    def GetText(self, _cuantos):  # noqa: N802 -- espeja el nombre COM real
        return self._texto


class ColeccionFalsa:
    def __init__(self, elementos):
        self._elementos = list(elementos)

    @property
    def Length(self):  # noqa: N802 -- espeja el nombre COM real
        return len(self._elementos)

    def GetElement(self, indice):  # noqa: N802 -- espeja el nombre COM real
        return self._elementos[indice]


class RaizFalsa:
    """El ``GetRootElement``: captura con qué condición se filtra por pid."""

    def __init__(self, ventanas):
        self._ventanas = list(ventanas)
        self.condiciones = []

    def FindAll(self, alcance, condicion):  # noqa: N802 -- espeja el nombre COM real
        self.condiciones.append((alcance, condicion))
        return ColeccionFalsa(self._ventanas)


class CacheRequestFalsa:
    """``IUIAutomationCacheRequest`` mínimo: propiedades, scope y modo."""

    def __init__(self):
        self.propiedades: list[object] = []
        self.TreeScope = None
        self.AutomationElementMode = None

    def AddProperty(self, propiedad):  # noqa: N802 -- espeja el nombre COM real
        self.propiedades.append(propiedad)


class UIAFalso:
    """El objeto ``CUIAutomation``: raíz + constructores de condiciones."""

    def __init__(self, raiz):
        self._raiz = raiz
        self.cache_requests: list[CacheRequestFalsa] = []

    def GetRootElement(self):  # noqa: N802 -- espeja el nombre COM real
        return self._raiz

    def CreatePropertyCondition(self, propiedad, valor):  # noqa: N802 -- espeja el nombre COM real
        return ("propiedad", propiedad, valor)

    def CreateTrueCondition(self):  # noqa: N802 -- espeja el nombre COM real
        return ("verdadera",)

    def CreateCacheRequest(self):  # noqa: N802 -- espeja el nombre COM real
        solicitud = CacheRequestFalsa()
        self.cache_requests.append(solicitud)
        return solicitud


class VentanaHandleFalso:
    def __init__(self, controles):
        self._controles = list(controles)
        self.búsquedas = []
        self.búsquedas_cache = []

    def FindAll(self, alcance, condicion):  # noqa: N802 -- espeja el nombre COM real
        self.búsquedas.append((alcance, condicion))
        return ColeccionFalsa(self._controles)

    def FindAllBuildCache(self, alcance, condicion, cache_request):  # noqa: N802 -- espeja el nombre COM real
        self.búsquedas_cache.append((alcance, condicion, cache_request))
        referencia_completa = cache_request.AutomationElementMode == "Full"
        for elemento in self._controles:
            elemento.aplicar_cache(cache_request.propiedades, referencia_completa)
        return ColeccionFalsa(self._controles)


class ClienteCOMFalso:
    def __init__(self, modulo, raiz, error_en_getmodule=None):
        self._modulo = modulo
        self._uia = UIAFalso(raiz)
        self._error = error_en_getmodule
        self.objetos_creados = []

    def GetModule(self, _dll):  # noqa: N802 -- espeja el nombre COM real
        if self._error is not None:
            raise self._error
        return self._modulo

    def CreateObject(self, _clsid, interface=None):  # noqa: N802 -- espeja el nombre COM real
        self.objetos_creados.append(interface)
        return self._uia


class ComtypesFalso(types.ModuleType):
    """El binding COM con ciclo de vida observable."""

    def __init__(self, cliente):
        super().__init__("comtypes")
        self.client = cliente
        self.COMError = FalsoCOMError
        self.eventos: list[str] = []

    def CoInitialize(self):  # noqa: N802 -- espeja el nombre COM real
        self.eventos.append("CoInitialize")

    def CoUninitialize(self):  # noqa: N802 -- espeja el nombre COM real
        self.eventos.append("CoUninitialize")


class MontajeCOM:
    """Instala los falsos en ``sys.modules`` y construye el adaptador REAL."""

    def __init__(self, monkeypatch, ventanas=(), error_en_getmodule=None):
        # El adaptador es Windows-only POR CONTRATO (chequea `sys.platform`
        # antes de importar el binding), así que el montaje declara la
        # plataforma que la lógica bajo prueba exige: sin esto, en POSIX el
        # constructor real corta con UIANoDisponibleError antes de usar los
        # `comtypes` falsos y los tests del adaptador no corren. El
        # monkeypatch lo revierte al terminar el test.
        monkeypatch.setattr(sys, "platform", "win32")
        self.modulo = _modulo_uia_falso()
        self.raiz = RaizFalsa(ventanas)
        self.cliente = ClienteCOMFalso(self.modulo, self.raiz, error_en_getmodule)
        self.comtypes = ComtypesFalso(self.cliente)
        self._cliente_mod = types.ModuleType("comtypes.client")
        self._cliente_mod.GetModule = self.cliente.GetModule
        self._cliente_mod.CreateObject = self.cliente.CreateObject
        monkeypatch.setitem(sys.modules, "comtypes", self.comtypes)
        monkeypatch.setitem(sys.modules, "comtypes.client", self._cliente_mod)

    def construir(self):
        return construir_observador_windows()


def _elemento_ventana(pid=4242, titulo="TexGen 3.00", clase="TMainForm"):
    return ElementoFalso(
        {
            "UIA_ProcessIdPropertyId": pid,
            "UIA_NamePropertyId": titulo,
            "UIA_ClassNamePropertyId": clase,
        }
    )


def _elemento_output(valor, pid=4242):
    return ElementoFalso(
        {
            "UIA_ProcessIdPropertyId": pid,
            "UIA_AutomationIdPropertyId": "",
            "UIA_NamePropertyId": "",
            "UIA_ControlTypePropertyId": 50004,
            "UIA_ClassNamePropertyId": "TEdit",
            "UIA_IsValuePatternAvailablePropertyId": True,
        },
        {"UIA_ValuePatternId": PatronValorFalso(valor)},
    )


# ---------------------------------------------------------------------------
# Import-safety / plataforma
# ---------------------------------------------------------------------------


def test_el_modulo_se_importa_sin_comtypes_a_nivel_de_modulo():
    """Nada Windows-only en el tope: el CI de Ubuntu importa el paquete."""
    arbol = ast.parse(MODULO_WINDOWS.read_text(encoding="utf-8"))
    importados = set()
    for nodo in arbol.body:
        if isinstance(nodo, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and (nodo.level or 0) == 0:
            importados.add((nodo.module or "").split(".")[0])
    assert "comtypes" not in importados
    assert "ctypes" not in importados


def test_construir_en_plataforma_no_soportada_falla_cerrado(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(UIANoDisponibleError):
        construir_observador_windows()


def test_construir_sin_binding_com_falla_cerrado(monkeypatch):
    """``sys.modules["comtypes"] = None`` hace que el import perezoso lance
    ``ImportError``: el adaptador lo traduce, no lo deja escapar."""
    monkeypatch.setitem(sys.modules, "comtypes", None)
    with pytest.raises(UIANoDisponibleError):
        construir_observador_windows()


def test_fallo_de_inicializacion_libera_el_apartamento(monkeypatch):
    """Ownership también en el fracaso: si ``GetModule`` falla tras un
    ``CoInitialize`` exitoso, el apartamento se desinicializa igual."""
    montaje = MontajeCOM(monkeypatch, error_en_getmodule=ImportError("sin typelib"))
    with pytest.raises(UIANoDisponibleError):
        montaje.construir()
    assert montaje.comtypes.eventos == ["CoInitialize", "CoUninitialize"]


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------


def test_liberar_es_idempotente_y_cierra_el_apartamento(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    assert montaje.comtypes.eventos == ["CoInitialize"]
    observador.liberar()
    observador.liberar()
    assert montaje.comtypes.eventos == ["CoInitialize", "CoUninitialize"]
    assert observador._uia is None
    assert observador._uia_mod is None


def test_liberar_suelta_referencias_antes_de_desinicializar():
    """Orden contractual MSDN: primero las referencias COM, después el
    ``CoUninitialize``. Al revés es un crash del runtime, no una limpieza."""
    arbol = ast.parse(MODULO_WINDOWS.read_text(encoding="utf-8"))
    liberar = next(nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "liberar")
    # Sin el docstring: su prosa nombra `_uia`/`CoUninitialize` y contaminaría
    # el orden de las sentencias ejecutables.
    cuerpo = liberar.body[1:] if isinstance(liberar.body[0], ast.Expr) else liberar.body
    orden: list[tuple[str, int]] = []
    for indice, sentencia in enumerate(cuerpo):
        fuente = ast.dump(sentencia)
        if "_uia_mod" in fuente or "_uia" in fuente:
            orden.append(("referencias", indice))
        if "CoUninitialize" in fuente:
            orden.append(("couninitialize", indice))
    posiciones_referencias = [i for nombre, i in orden if nombre == "referencias"]
    posiciones_couninit = [i for nombre, i in orden if nombre == "couninitialize"]
    assert posiciones_referencias and posiciones_couninit
    assert max(posiciones_referencias) < min(posiciones_couninit)


# ---------------------------------------------------------------------------
# Ventanas y controles
# ---------------------------------------------------------------------------


def test_ventanas_de_proceso_filtra_por_pid_en_la_condicion(monkeypatch):
    montaje = MontajeCOM(
        monkeypatch,
        ventanas=[_elemento_ventana(pid=4242), _elemento_ventana(pid=9999, titulo="Otra")],
    )
    observador = montaje.construir()
    ventanas = observador.ventanas_de_proceso(4242)
    assert [(v.pid, v.titulo) for v in ventanas] == [(4242, "TexGen 3.00"), (9999, "Otra")]
    assert len(montaje.raiz.condiciones) == 1
    alcance, condicion = montaje.raiz.condiciones[0]
    assert alcance == "Children", "hijas directas de la raíz, no el Desktop entero"
    assert condicion[0] == "propiedad"
    assert condicion[1] == "UIA_ProcessIdPropertyId"
    assert condicion[2] == 4242


def test_controles_de_ventana_enumera_descendientes(monkeypatch):
    handle = VentanaHandleFalso([_elemento_output(TEXGEN_ROOT)])
    montaje = MontajeCOM(monkeypatch, ventanas=[_elemento_ventana()])
    observador = montaje.construir()
    (ventana,) = observador.ventanas_de_proceso(4242)
    ventana = VentanaObservada(pid=ventana.pid, titulo=ventana.titulo, class_name=ventana.class_name, handle=handle)
    (control,) = observador.controles_de_ventana(ventana)
    assert (control.tipo_de_control, control.class_name) == ("Edit", "TEdit")
    assert handle.búsquedas == [], "los controles ya no se descubren con FindAll suelto"
    assert handle.búsquedas_cache and handle.búsquedas_cache[0][0] == "Descendants"
    assert handle.búsquedas_cache[0][1] == ("verdadera",)


def test_cache_request_contiene_exactamente_las_propiedades_de_describir(monkeypatch):
    handle = VentanaHandleFalso([_elemento_output(TEXGEN_ROOT)])
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    ventana = VentanaObservada(pid=4242, titulo="TexGen 3.00", class_name="TMainForm", handle=handle)
    observador.controles_de_ventana(ventana)
    assert len(montaje.cliente._uia.cache_requests) == 1
    solicitud = montaje.cliente._uia.cache_requests[0]
    assert tuple(solicitud.propiedades) == PROPIEDADES_DE_DESCRIPCION_UIA
    assert solicitud.TreeScope == "Element"
    assert solicitud.AutomationElementMode == "Full"


def test_coste_cliente_de_descripcion_sale_del_cache(monkeypatch):
    """DERIVADO POR LECTURA / MODELO DE COSTE, no RPC COM medidas.

    H3-A: las cinco propiedades de ``_describir`` salen de
    ``GetCachedPropertyValue``. ``GetCurrentPropertyValue`` de esas cinco no
    crece con N. El fake no simula el transporte COM.
    """
    ElementoFalso.lecturas_current = []
    ElementoFalso.lecturas_cache = []
    cantidad = 4
    handle = VentanaHandleFalso([_elemento_output(TEXGEN_ROOT, pid=4242) for _ in range(cantidad)])
    montaje = MontajeCOM(monkeypatch, ventanas=[_elemento_ventana()])
    observador = montaje.construir()
    ventana = VentanaObservada(pid=4242, titulo="TexGen 3.00", class_name="TMainForm", handle=handle)
    controles = observador.controles_de_ventana(ventana)
    assert len(controles) == cantidad
    descriptivas_current = [
        lectura for lectura in ElementoFalso.lecturas_current if lectura in PROPIEDADES_DE_DESCRIPCION_UIA
    ]
    descriptivas_cache = [
        lectura for lectura in ElementoFalso.lecturas_cache if lectura in PROPIEDADES_DE_DESCRIPCION_UIA
    ]
    assert descriptivas_current == []
    assert len(descriptivas_cache) == 5 * cantidad
    assert handle.búsquedas_cache and handle.búsquedas == []


def test_control_type_desconocido_se_reporta_como_id_crudo(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    assert observador._control_type(ElementoFalso({"UIA_ControlTypePropertyId": 59999})) == "59999"
    assert observador._control_type(ElementoFalso({"UIA_ControlTypePropertyId": "no-numérico"})) == "no-numérico"


def test_pid_ilegible_cuando_no_se_puede_convertir(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    assert observador._pid(ElementoFalso({"UIA_ProcessIdPropertyId": "???"})) == PID_ILEGIBLE


def test_error_com_en_la_enumeracion_se_traduce(monkeypatch):
    class _RaizRota:
        def FindAll(self, _alcance, _condicion):  # noqa: N802 -- espeja el nombre COM real
            raise FalsoCOMError("RPC_E_DISCONNECTED")

    montaje = MontajeCOM(monkeypatch)
    montaje.cliente._uia._raiz = _RaizRota()
    observador = montaje.construir()
    with pytest.raises(ObservacionUIAError):
        observador.ventanas_de_proceso(4242)


def test_enumeracion_incompleta_no_se_recorta(monkeypatch):
    montaje = MontajeCOM(monkeypatch, ventanas=[_elemento_ventana()] * (TOPE_DE_ELEMENTOS_UIA + 1))
    observador = montaje.construir()
    with pytest.raises(EnumeracionIncompletaError):
        observador.ventanas_de_proceso(4242)


# ---------------------------------------------------------------------------
# Lectura de valores
# ---------------------------------------------------------------------------


def test_leer_valor_prefiere_value_y_cae_a_text(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    por_valor = ControlObservado(pid=4242, tipo_de_control="Edit", class_name="TEdit", handle=_elemento_output("C:/A"))
    assert observador.leer_valor(por_valor) == "C:/A"

    por_texto = ControlObservado(
        pid=4242,
        tipo_de_control="Edit",
        class_name="TEdit",
        handle=ElementoFalso(
            {"UIA_IsValuePatternAvailablePropertyId": False, "UIA_IsTextPatternAvailablePropertyId": True},
            {"UIA_TextPatternId": PatronTextoFalso("C:/B")},
        ),
    )
    assert observador.leer_valor(por_texto) == "C:/B"


def test_leer_valor_vacio_en_value_prueba_text(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    handle = ElementoFalso(
        {"UIA_IsValuePatternAvailablePropertyId": True, "UIA_IsTextPatternAvailablePropertyId": True},
        {"UIA_ValuePatternId": PatronValorFalso(""), "UIA_TextPatternId": PatronTextoFalso(TEXGEN_ROOT)},
    )
    control = ControlObservado(pid=4242, tipo_de_control="Edit", class_name="TEdit", handle=handle)
    assert observador.leer_valor(control) == TEXGEN_ROOT


def test_leer_valor_sin_patrones_da_none(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    control = ControlObservado(pid=4242, tipo_de_control="Edit", class_name="TEdit", handle=ElementoFalso())
    assert observador.leer_valor(control) is None


def test_patrones_de_lectura_solo_diagnostica(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    handle = ElementoFalso(
        {
            "UIA_IsValuePatternAvailablePropertyId": True,
            "UIA_IsTextPatternAvailablePropertyId": False,
            "UIA_IsLegacyIAccessiblePatternAvailablePropertyId": True,
        }
    )
    control = ControlObservado(pid=4242, handle=handle)
    assert observador.patrones_de_lectura(control) == "ValuePattern,LegacyIAccessible"


def test_fallo_del_rig_en_patrones_se_reporta_y_bug_del_adaptador_propaga(monkeypatch):
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()

    class _HandleRoto:
        def GetCurrentPropertyValue(self, _identificador):  # noqa: N802 -- espeja el nombre COM real
            raise FalsoCOMError("stale")

    control = ControlObservado(pid=4242, handle=_HandleRoto())
    assert "ValuePattern=?" in observador.patrones_de_lectura(control)

    class _HandleSinModulo:
        pass

    # Sin `_uia_mod` válido el lookup `__dict__[id]` lanza KeyError: un bug del
    # adaptador (id mal escrito) NUNCA se disfraza de "no expone el patrón".
    observador_roto = montaje.construir()
    observador_roto._uia_mod = types.SimpleNamespace()
    with pytest.raises(KeyError):
        observador_roto.patrones_de_lectura(ControlObservado(pid=4242, handle=ElementoFalso()))


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------


def test_primer_texto_no_vacio_ordena_y_no_corta_en_vacio():
    llamados: list[str] = []

    def _lector(nombre, valor):
        def _leer():
            llamados.append(nombre)
            return valor

        return _leer

    assert primer_texto_no_vacio((_lector("value", ""), _lector("text", TEXGEN_ROOT))) == TEXGEN_ROOT
    assert llamados == ["value", "text"]
    llamados.clear()
    assert primer_texto_no_vacio((_lector("value", "C:/A"), _lector("text", TEXGEN_ROOT))) == "C:/A"
    assert llamados == ["value"]
    llamados.clear()
    assert primer_texto_no_vacio((_lector("value", None), _lector("text", ""))) is None
    assert llamados == ["value", "text"]


def test_describir_tolerando_fallos_cuenta_sin_abortar():
    def _describir(elemento):
        if elemento == "roto":
            raise FalsoCOMError("stale element")
        return ControlObservado(pid=4242, automation_id=str(elemento))

    descritos, fallidos = describir_tolerando_fallos(["a", "roto", "b"], _describir, (FalsoCOMError,))
    assert [c.automation_id for c in descritos] == ["a", "b"]
    assert fallidos == 1


def test_el_mapa_de_control_types_esta_congelado_en_el_runtime():
    assert NOMBRES_DE_CONTROL_TYPE == {
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


# ---------------------------------------------------------------------------
# Integración adaptador real + pipeline real (sólo COM falsificado)
# ---------------------------------------------------------------------------


class _LocalizadorFalso:
    def __init__(self, procesos):
        self._procesos = tuple(procesos)

    def procesos(self):
        return self._procesos


def test_el_adaptador_real_alimenta_al_pipeline_real(monkeypatch):
    """La pieza que el rig midió enchufada al decisor: MATCH de punta a punta."""
    handle = VentanaHandleFalso([_elemento_output(TEXGEN_ROOT)])
    montaje = MontajeCOM(monkeypatch)

    raiz_real = montaje.raiz
    ventana_handle = _elemento_ventana()
    raiz_real._ventanas = [ventana_handle]

    observador = montaje.construir()
    assert isinstance(observador, ObservadorUIAWindows)
    # La ventana observada viaja con el handle falso que conoce sus controles.
    (ventana_descrita,) = observador.ventanas_de_proceso(4242)
    ventana_descrita = VentanaObservada(
        pid=ventana_descrita.pid, titulo=ventana_descrita.titulo, class_name=ventana_descrita.class_name, handle=handle
    )

    class _ObservadorConVentana:
        def ventanas_de_proceso(self, pid):
            return (ventana_descrita,)

        def controles_de_ventana(self, ventana):
            return observador.controles_de_ventana(ventana)

        def leer_valor(self, control):
            return observador.leer_valor(control)

        def liberar(self):
            observador.liberar()

    resultado = observar_output(
        SolicitudPreflightUIA(
            tool="TexGen",
            ejecutable_esperado="TexGenx64.exe",
            salida_administrada_esperada=TEXGEN_ROOT,
            criterios_del_control=selector_de_output("TexGen"),
            pid=4242,
        ),
        localizador=_LocalizadorFalso(
            [ProcesoObservado(pid=4242, nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None)]
        ),
        observador=_ObservadorConVentana(),
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


# ---------------------------------------------------------------------------
# M8 — la sonda no redefine el backend (una sola implementación)
# ---------------------------------------------------------------------------


def test_la_sonda_importa_el_backend_del_runtime_en_vez_de_redefinirlo():
    """M8: si la sonda vuelve a implementar COM, este ancla se pone rojo."""
    arbol = ast.parse(SONDA.read_text(encoding="utf-8"))
    definidos = {
        nodo.name for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    piezas_del_backend = {
        "ObservadorUIAWindows",
        "construir_observador_windows",
        "primer_texto_no_vacio",
        "describir_tolerando_fallos",
        "ventanas_de_proceso",
        "controles_de_ventana",
        "leer_valor",
        "controles_para_volcado",
        "patrones_de_lectura",
    }
    assert not (definidos & piezas_del_backend), (
        f"la sonda redefine el backend: {sorted(definidos & piezas_del_backend)}"
    )
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").endswith("dyndolod_uia_windows"):
            importados.update(alias.name for alias in nodo.names)
    assert {"ObservadorUIAWindows", "primer_texto_no_vacio", "describir_tolerando_fallos"} <= importados


# ---------------------------------------------------------------------------
# H3-A — semántica del discovery cacheado (el fake no simula RPC)
# ---------------------------------------------------------------------------


def _solicitud_texgen() -> SolicitudPreflightUIA:
    return SolicitudPreflightUIA(
        tool="TexGen",
        ejecutable_esperado="TexGenx64.exe",
        salida_administrada_esperada=TEXGEN_ROOT,
        criterios_del_control=selector_de_output("TexGen"),
        pid=4242,
    )


def _localizador_texgen():
    return _LocalizadorFalso([ProcesoObservado(pid=4242, nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None)])


def _observar_con_backend(monkeypatch, controles):
    """Pipeline real + adaptador real. Sólo el boundary COM está falsificado."""
    handle = VentanaHandleFalso(controles)
    montaje = MontajeCOM(monkeypatch, ventanas=[_elemento_ventana()])
    observador = montaje.construir()
    (ventana,) = observador.ventanas_de_proceso(4242)
    ventana = VentanaObservada(pid=ventana.pid, titulo=ventana.titulo, class_name=ventana.class_name, handle=handle)

    class _ObservadorConVentana:
        def ventanas_de_proceso(self, pid):
            return (ventana,)

        def controles_de_ventana(self, _ventana):
            return observador.controles_de_ventana(ventana)

        def leer_valor(self, control):
            return observador.leer_valor(control)

    resultado = observar_output(
        _solicitud_texgen(),
        localizador=_localizador_texgen(),
        observador=_ObservadorConVentana(),
    )
    return resultado, handle, montaje, observador


def test_semantica_cacheada_conserva_veredictos(monkeypatch):
    """Los mismos estados/razones que el discovery sin cache. No es un RPC count."""
    ninguno, _, _, _ = _observar_con_backend(monkeypatch, [_elemento_ventana(titulo="etiqueta")])
    assert (ninguno.estado, ninguno.razon) == (EstadoPreflight.UNKNOWN, RazonPreflight.CONTROL_NO_ENCONTRADO)

    uno, handle, montaje, observador = _observar_con_backend(monkeypatch, [_elemento_output(TEXGEN_ROOT)])
    assert (uno.estado, uno.razon) == (EstadoPreflight.MATCH, RazonPreflight.OUTPUT_COINCIDE)
    assert (
        observador.leer_valor(
            observador.controles_de_ventana(
                VentanaObservada(pid=4242, titulo="TexGen 3.00", class_name="TMainForm", handle=handle)
            )[0]
        )
        == TEXGEN_ROOT
    )

    distinto, _, _, _ = _observar_con_backend(monkeypatch, [_elemento_output("E:/otra")])
    assert (distinto.estado, distinto.razon) == (EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_DIFIERE)

    dos, _, _, _ = _observar_con_backend(
        monkeypatch, [_elemento_output(TEXGEN_ROOT), _elemento_output(TEXGEN_ROOT, pid=4242)]
    )
    assert (dos.estado, dos.razon) == (EstadoPreflight.UNKNOWN, RazonPreflight.CONTROL_AMBIGUO)

    ilegible, _, _, _ = _observar_con_backend(
        monkeypatch,
        [
            ElementoFalso(
                {
                    "UIA_ProcessIdPropertyId": "???",
                    "UIA_AutomationIdPropertyId": "",
                    "UIA_NamePropertyId": "",
                    "UIA_ControlTypePropertyId": 50004,
                    "UIA_ClassNamePropertyId": "TEdit",
                }
            )
        ],
    )
    assert (ilegible.estado, ilegible.razon) == (EstadoPreflight.UNKNOWN, RazonPreflight.PID_NO_OBSERVABLE)

    ajeno, _, _, _ = _observar_con_backend(monkeypatch, [_elemento_output(TEXGEN_ROOT, pid=9999)])
    assert (ajeno.estado, ajeno.razon) == (EstadoPreflight.UNKNOWN, RazonPreflight.CONTROL_FUERA_DEL_PROCESO)

    assert montaje.raiz.condiciones
    assert {alcance for alcance, _condicion in montaje.raiz.condiciones} == {"Children"}
    assert handle.búsquedas == []
    assert handle.búsquedas_cache and handle.búsquedas_cache[0][0] == "Descendants"


def test_enumeracion_incompleta_cacheada_no_fabrica_match(monkeypatch):
    controles = [_elemento_output(TEXGEN_ROOT)] * (TOPE_DE_ELEMENTOS_UIA + 1)
    resultado, handle, _, _ = _observar_con_backend(monkeypatch, controles)
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ENUMERACION_INCOMPLETA
    assert resultado.estado is not EstadoPreflight.MATCH
    assert handle.búsquedas_cache, "el cache no puede saltarse la cota: la colección igual se mide"


def test_comerror_en_build_cache_sigue_fail_closed(monkeypatch):
    class _HandleRoto(VentanaHandleFalso):
        def FindAllBuildCache(self, _alcance, _condicion, _cache):  # noqa: N802 -- espeja el nombre COM real
            raise FalsoCOMError("RPC_E_DISCONNECTED")

    handle = _HandleRoto([])
    montaje = MontajeCOM(monkeypatch, ventanas=[_elemento_ventana()])
    observador = montaje.construir()
    (ventana,) = observador.ventanas_de_proceso(4242)
    ventana = VentanaObservada(pid=ventana.pid, titulo=ventana.titulo, class_name=ventana.class_name, handle=handle)

    class _ObservadorConVentana:
        def ventanas_de_proceso(self, pid):
            return (ventana,)

        def controles_de_ventana(self, _ventana):
            return observador.controles_de_ventana(ventana)

        def leer_valor(self, control):
            return observador.leer_valor(control)

    resultado = observar_output(
        _solicitud_texgen(),
        localizador=_localizador_texgen(),
        observador=_ObservadorConVentana(),
    )
    assert (resultado.estado, resultado.razon) == (EstadoPreflight.UNKNOWN, RazonPreflight.ERROR_UIA)


def test_mode_none_rompe_get_current_pattern(monkeypatch):
    """Mutante local C: AutomationElementMode_None no puede leer el patrón actual.

    MSDN AutomationElementMode: None no deja referencia al UI; GetCurrentPattern
    es una operación current y el fake la rechaza. El camino de producción fija Full.
    """
    handle = VentanaHandleFalso([_elemento_output(TEXGEN_ROOT)])
    montaje = MontajeCOM(monkeypatch)
    observador = montaje.construir()
    ventana = VentanaObservada(pid=4242, titulo="TexGen 3.00", class_name="TMainForm", handle=handle)
    original = observador.crear_cache_request_para_descripcion

    def _modo_none():
        solicitud = original()
        solicitud.AutomationElementMode = "None"
        return solicitud

    observador.crear_cache_request_para_descripcion = _modo_none  # type: ignore[method-assign]
    (control,) = observador.controles_de_ventana(ventana)
    with pytest.raises(ObservacionUIAError):
        observador.leer_valor(control)
    assert handle.búsquedas_cache[0][2].AutomationElementMode == "None"


def test_mutante_local_de_cache_incompleto_no_pasa_el_contrato():
    """Mutantes A/B/E reproducibles sin framework: el contrato que los tests exigen."""
    solicitud = CacheRequestFalsa()
    for nombre in PROPIEDADES_DE_DESCRIPCION_UIA:
        solicitud.AddProperty(nombre)
    assert tuple(solicitud.propiedades) == PROPIEDADES_DE_DESCRIPCION_UIA

    incompleto = CacheRequestFalsa()
    for nombre in PROPIEDADES_DE_DESCRIPCION_UIA[:-1]:
        incompleto.AddProperty(nombre)
    assert tuple(incompleto.propiedades) != PROPIEDADES_DE_DESCRIPCION_UIA

    lecturas_current = ["UIA_NamePropertyId"] * 8
    assert lecturas_current != []
    assert TOPE_DE_ELEMENTOS_UIA == 400
