"""F4 — ciclo de vida del apartamento COM del backend Windows (T5-v2).

**Qué prueba este archivo.** Que :class:`ObservadorUIAWindows` equilibra
``CoInitialize`` con exactamente un ``CoUninitialize`` en el mismo hilo lógico
de ejecución, en todo camino: liberación explícita (``liberar``), liberación
idempotente (llamada duplicada no desequilibra) y construcción abortada
(fallo tras inicializar → se desinicializa antes de propagar).

**Cómo.** ``comtypes`` se sustituye entero por un módulo falso inyectado en
``sys.modules``: el adaptador lo importa perezoso dentro de ``__init__`` y
sus métodos, así que el falso no necesita COM real ni un rig. La plataforma
también se falsifica (``sys.platform = "win32"``), porque la guardia del
adaptador rechaza construirse fuera de Windows aunque el binding exista.

**Por qué existe la prueba.** Microsoft exige un ``CoUninitialize`` por cada
``CoInitialize`` exitoso —incluido el ``S_FALSE`` de reiniciar un hilo del
pool ya inicializado—; el diseño anterior documentaba la omisión como
deliberada apoyándose en el reaprovechamiento del hilo del pool, que es
exactamente el caso en el que el desbalance crece. El gate llama a
``liberar`` en su ``finally``; estos tests fijan el otro extremo del
contrato.
"""

from __future__ import annotations

import sys
import types

import pytest

from sky_claw.local.tools import dyndolod_uia_windows as backend
from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError


def _comtypes_falso(*, falla_create_object: bool = False) -> types.ModuleType:
    """Un ``comtypes`` de módulo, con contadores de ciclo de vida observables.

    Implementa sólo lo que el adaptador toca: ``COMError``, ``CoInitialize``,
    ``CoUninitialize`` y el submódulo ``client`` con ``GetModule`` y
    ``CreateObject``. Los contadores viven como atributos del módulo falso
    para que el test los lea sin closures frágiles.
    """
    comtypes = types.ModuleType("comtypes")

    class COMError(Exception):
        """El gemelo falso de ``comtypes.COMError``."""

    comtypes.COMError = COMError  # type: ignore[attr-defined]
    comtypes.inicializaciones = 0  # type: ignore[attr-defined]
    comtypes.uninicializaciones = 0  # type: ignore[attr-defined]

    def _init() -> None:
        comtypes.inicializaciones += 1  # type: ignore[attr-defined]

    def _uninit() -> None:
        comtypes.uninicializaciones += 1  # type: ignore[attr-defined]

    comtypes.CoInitialize = _init  # type: ignore[attr-defined]
    comtypes.CoUninitialize = _uninit  # type: ignore[attr-defined]

    client = types.ModuleType("comtypes.client")
    uia_mod = types.SimpleNamespace(IUIAutomation=object())
    client.GetModule = lambda _nombre: uia_mod  # type: ignore[attr-defined]

    def _create_object(_clsid: str, interface: object) -> object:
        if falla_create_object:
            raise COMError("CreateObject falló (simulado)")
        return object()

    client.CreateObject = _create_object  # type: ignore[attr-defined]
    comtypes.client = client  # type: ignore[attr-defined]
    return comtypes


def _con_comtypes_falso(monkeypatch: pytest.MonkeyPatch, comtypes: types.ModuleType) -> None:
    """Instala el binding falso y la plataforma win32 que el adaptador exige."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", comtypes.client)  # type: ignore[attr-defined]


def test_liberar_equilibra_coinitialize_y_suelta_las_referencias(monkeypatch):
    comtypes = _comtypes_falso()
    _con_comtypes_falso(monkeypatch, comtypes)

    observador = backend.ObservadorUIAWindows()
    assert comtypes.inicializaciones == 1  # type: ignore[attr-defined]
    assert comtypes.uninicializaciones == 0  # type: ignore[attr-defined]

    observador.liberar()

    # Las referencias COM se sueltan ANTES del CoUninitialize (MSDN: liberar
    # después de cerrar el apartamento es el crash que el diseño evita).
    assert observador._uia is None
    assert observador._uia_mod is None
    assert comtypes.uninicializaciones == 1  # type: ignore[attr-defined]


def test_liberar_es_idempotente(monkeypatch):
    comtypes = _comtypes_falso()
    _con_comtypes_falso(monkeypatch, comtypes)
    observador = backend.ObservadorUIAWindows()

    observador.liberar()
    observador.liberar()

    assert comtypes.inicializaciones == 1  # type: ignore[attr-defined]
    assert comtypes.uninicializaciones == 1  # type: ignore[attr-defined]


def test_una_construccion_abortada_tras_coinitialize_lo_equilibra(monkeypatch):
    """Si ``CreateObject`` falla, el apartamento inicializado no queda suelto."""
    comtypes = _comtypes_falso(falla_create_object=True)
    _con_comtypes_falso(monkeypatch, comtypes)

    with pytest.raises(UIANoDisponibleError):
        backend.ObservadorUIAWindows()

    assert comtypes.inicializaciones == 1  # type: ignore[attr-defined]
    assert comtypes.uninicializaciones == 1  # type: ignore[attr-defined]
