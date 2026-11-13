"""H-8: no había herramienta para medir un build nuevo de TexGen/DynDOLOD y darlo de alta.

Cualquier actualización (o un rig VR) cae en ``UNATTENDED_BUILD_UNSUPPORTED`` y el único camino era un rig manual sin
procedimiento. ``scripts/dyndolod_medir_contrato_uia.py`` mide la UI de un proceso VIVO por el puerto de LECTURA
(``ObservadorUIA``) y diagnostica qué partes del contrato congelado siguen valiendo en ese build.

Lo que se ancla acá, sin rig (la GUI es simulada: se DERIVA del propio contrato congelado, una captura por momento y con
a lo sumo un diálogo a la vez, como en una corrida real):

- la identidad del proceso es estricta (ruta exacta, una sola instancia) y nunca se amplía el alcance a otros pids;
- una enumeración incompleta o un backend ausente cortan: jamás un recorte silencioso;
- **cada captura se clasifica como lo hace el driver de producción**: el test de PARIDAD compara ambas sobre casos
  enumerados (review Codex del PR #907: el diagnóstico era más laxo que el driver);
- el diagnóstico, corrido sobre la GUI simulada de CADA contrato congelado, da ``compatible``, y cada mutación de la GUI
  cambia el veredicto;
- «compatible» exige haber observado TODO: sin las capturas del diálogo terminal y de los modales el veredicto es
  ``incompleto``, nunca un «sí» por omisión.

**No prueba** que la GUI real de Alpha-209 produzca ese mismo informe: eso es el smoke del rig.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import random

import pytest

from scripts import dyndolod_medir_contrato_uia as medir
from sky_claw.local.tools.dyndolod_uia_contrato import (
    CONTRATOS_DE_UI,
    MARCAS_DEL_DIALOGO_DE_TAREA,
    ContratoDeUIHerramienta,
    FirmaDeDialogo,
)
from sky_claw.local.tools.dyndolod_uia_driver import ElementoUI, InstantaneaUI, VentanaUI, clasificar_pantalla
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ControlObservado,
    EnumeracionIncompletaError,
    ProcesoObservado,
    UIANoDisponibleError,
    VentanaObservada,
)

PID = 4242
EXE_BYTES = b"build-de-prueba"
SHA_DEL_EXE = hashlib.sha256(EXE_BYTES).hexdigest()
CONTRATOS = sorted(CONTRATOS_DE_UI.values(), key=lambda c: (c.tool, c.exe_sha256))
CLASE_BOTON = MARCAS_DEL_DIALOGO_DE_TAREA["class_name_boton"]


# ---------------------------------------------------------------------------
# Dobles: el puerto de lectura y el sensor de procesos
# ---------------------------------------------------------------------------


class LocalizadorFalso:
    def __init__(self, *procesos: ProcesoObservado) -> None:
        self._procesos = procesos

    def procesos(self) -> tuple[ProcesoObservado, ...]:
        return self._procesos


class ObservadorFalso:
    """Un ``ObservadorUIA`` sobre ventanas y controles en memoria. SOLO los dos métodos de lectura que usa el script."""

    def __init__(self, ventanas: list[VentanaObservada], controles: dict[str, list[ControlObservado]]) -> None:
        self.ventanas = ventanas
        self.controles = controles

    def ventanas_de_proceso(self, pid: int) -> list[VentanaObservada]:
        return list(self.ventanas)

    def controles_de_ventana(self, ventana: VentanaObservada) -> list[ControlObservado]:
        return list(self.controles[str(ventana.handle)])

    def leer_valor(self, control: ControlObservado) -> str | None:  # pragma: no cover - el script no lo usa
        raise AssertionError("la medición no necesita leer valores")


def _ctl(selector, *, pid: int = PID, tipo: str = "Edit", clase: str = "TEdit") -> ControlObservado:
    """Un control que ``selector`` reconoce, con defaults para lo que el selector no dice."""
    return ControlObservado(
        pid=pid,
        automation_id=selector.automation_id or "",
        nombre=selector.nombre or "",
        tipo_de_control=selector.tipo_de_control or tipo,
        class_name=selector.class_name or clase,
    )


def _controles_de_la_ventana_principal(contrato: ContratoDeUIHerramienta) -> list[ControlObservado]:
    controles = [
        _ctl(contrato.selector_output),
        # El control que COLISIONA con Output por tipo: sin él la unicidad del selector no se estaría probando.
        ControlObservado(pid=PID, tipo_de_control="Edit", class_name="TMemo", nombre="log"),
        _ctl(contrato.accion_inicio.selector, tipo="Button", clase="TButton"),
    ]
    if contrato.accion_previa is not None:
        controles.append(_ctl(contrato.accion_previa.selector, tipo="Button", clase="TButton"))
    return controles


def _controles_del_dialogo(
    firma: FirmaDeDialogo, *, anidado: bool, clase_boton: str = CLASE_BOTON, instrucciones: int = 1
) -> list[ControlObservado]:
    controles: list[ControlObservado] = []
    if anidado:  # la marca ``#32770`` como DESCENDIENTE de la ventana principal, no como ventana propia
        controles.append(
            ControlObservado(pid=PID, tipo_de_control="Window", class_name="#32770", nombre="dialogo anidado")
        )
    controles.extend(
        ControlObservado(
            pid=PID,
            automation_id=MARCAS_DEL_DIALOGO_DE_TAREA["automation_id_instruccion"],
            nombre=firma.instruccion,
            tipo_de_control="Text",
            class_name="Static",
        )
        for _ in range(instrucciones)
    )
    controles.extend(
        ControlObservado(pid=PID, nombre=nombre, automation_id=aid, tipo_de_control="Button", class_name=clase_boton)
        for nombre, aid in sorted(firma.botones)
    )
    return controles


def _captura(
    contrato: ContratoDeUIHerramienta,
    firma: FirmaDeDialogo | None = None,
    *,
    anidado: bool = False,
    titulo: str | None = None,
    clase_boton: str = CLASE_BOTON,
    instrucciones: int = 1,
) -> ObservadorFalso:
    """UNA captura de la GUI: la ventana principal y, opcionalmente, UN diálogo (de nivel superior o anidado)."""
    principal = VentanaObservada(
        pid=PID, titulo=titulo or contrato.tool, class_name=contrato.clase_ventana_principal, handle="w0"
    )
    controles = {"w0": _controles_de_la_ventana_principal(contrato)}
    ventanas = [principal]
    if firma is not None:
        dialogo = _controles_del_dialogo(firma, anidado=anidado, clase_boton=clase_boton, instrucciones=instrucciones)
        if anidado:
            controles["w0"].extend(dialogo)
        else:
            ventanas.append(VentanaObservada(pid=PID, titulo="dialogo", class_name="#32770", handle="w1"))
            controles["w1"] = dialogo
    return ObservadorFalso(ventanas, controles)


def _capturas(contrato: ContratoDeUIHerramienta, *, con_dialogos: bool = True, anidado: bool = False):
    """Las capturas de una corrida: reposo, y (opcional) el diálogo terminal y cada modal conocido, UNO por captura."""
    capturas = [("reposo", _captura(contrato))]
    if con_dialogos:
        capturas.append(("dialogo-terminal", _captura(contrato, contrato.dialogo_terminal, anidado=anidado)))
        capturas.extend(
            (f"modal-{m.id}", _captura(contrato, m.firma, anidado=anidado)) for m in contrato.modales_conocidos
        )
    return capturas


@pytest.fixture
def exe(tmp_path: pathlib.Path) -> pathlib.Path:
    ruta = tmp_path / "DynDOLOD" / "DynDOLODx64.exe"
    ruta.parent.mkdir()
    ruta.write_bytes(EXE_BYTES)
    return ruta


def _localizador(exe: pathlib.Path, *, pid: int = PID) -> LocalizadorFalso:
    return LocalizadorFalso(ProcesoObservado(pid=pid, nombre_ejecutable=exe.name, ruta_ejecutable=str(exe)))


def _medir(exe, observador, *, localizador=None, tool="DynDOLOD", etiqueta="reposo"):
    return medir.medir(
        exe=exe,
        tool=tool,
        modo_de_juego="SSE",
        etiqueta=etiqueta,
        observador=observador,
        localizador=localizador or _localizador(exe),
    )


def _medir_todas(exe, contrato, capturas) -> list[medir.Medicion]:
    return [
        _medir(exe, observador, tool=contrato.tool, etiqueta=etiqueta, localizador=_localizador(exe))
        for etiqueta, observador in capturas
    ]


# ---------------------------------------------------------------------------
# 1. La medición
# ---------------------------------------------------------------------------


def test_la_medicion_registra_el_build_y_la_ui_del_proceso(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")

    medicion = _medir(exe, _captura(contrato))

    assert (medicion.tool, medicion.modo_de_juego, medicion.etiqueta) == ("DynDOLOD", "SSE", "reposo")
    assert medicion.exe_sha256 == SHA_DEL_EXE, "el hash sale del ARCHIVO, no de lo que diga el operador"
    clases = {v.class_name for v in medicion.ventanas}
    assert contrato.clase_ventana_principal in clases
    principal = next(v for v in medicion.ventanas if v.class_name == contrato.clase_ventana_principal)
    assert any(c.class_name == "TMemo" for c in principal.controles)


def test_la_medicion_es_determinista_aunque_uia_devuelva_en_otro_orden(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    base = _captura(contrato, contrato.dialogo_terminal)
    barajada = _captura(contrato, contrato.dialogo_terminal)
    random.Random(7).shuffle(barajada.ventanas)
    for clave in barajada.controles:
        random.Random(9).shuffle(barajada.controles[clave])

    assert _medir(exe, base) == _medir(exe, barajada), "el orden de UIA no es parte de lo que se mide"


def test_nunca_se_amplia_el_alcance_a_ventanas_ni_controles_de_otro_pid(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    observador = _captura(contrato)
    intruso = VentanaObservada(pid=999, titulo="otra app", class_name="Intrusa", handle="x")
    observador.ventanas.append(intruso)
    observador.controles["x"] = [ControlObservado(pid=999, nombre="ajeno")]
    observador.controles["w0"].append(ControlObservado(pid=999, nombre="control-de-otro-pid", class_name="Cuco"))

    medicion = _medir(exe, observador)

    assert "Intrusa" not in {v.class_name for v in medicion.ventanas}
    assert all(c.nombre != "control-de-otro-pid" for v in medicion.ventanas for c in v.controles)


def test_sin_el_proceso_la_medicion_falla_cerrada(exe) -> None:
    with pytest.raises(medir.MedicionError, match="no hay un proceso"):
        _medir(exe, ObservadorFalso([], {}), localizador=LocalizadorFalso())


def test_el_mismo_nombre_en_otra_ruta_no_es_el_build(exe, tmp_path) -> None:
    otra = tmp_path / "otro" / exe.name
    localizador = LocalizadorFalso(ProcesoObservado(pid=PID, nombre_ejecutable=exe.name, ruta_ejecutable=str(otra)))

    with pytest.raises(medir.MedicionError, match="no hay un proceso"):
        _medir(exe, ObservadorFalso([], {}), localizador=localizador)


def test_un_proceso_con_ruta_ilegible_no_se_adivina_por_nombre(exe) -> None:
    localizador = LocalizadorFalso(ProcesoObservado(pid=PID, nombre_ejecutable=exe.name, ruta_ejecutable=None))

    with pytest.raises(medir.MedicionError, match="no hay un proceso"):
        _medir(exe, ObservadorFalso([], {}), localizador=localizador)


def test_dos_instancias_del_mismo_ejecutable_son_ambiguas(exe) -> None:
    localizador = LocalizadorFalso(
        ProcesoObservado(pid=PID, nombre_ejecutable=exe.name, ruta_ejecutable=str(exe)),
        ProcesoObservado(pid=PID + 1, nombre_ejecutable=exe.name, ruta_ejecutable=str(exe)),
    )

    with pytest.raises(medir.MedicionError, match="más de una instancia"):
        _medir(exe, ObservadorFalso([], {}), localizador=localizador)


def test_una_enumeracion_incompleta_no_deja_una_medicion_parcial(exe) -> None:
    class Truncado(ObservadorFalso):
        def controles_de_ventana(self, ventana):
            raise EnumeracionIncompletaError("el árbol no entró entero")

    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    base = _captura(contrato)

    with pytest.raises(medir.MedicionError, match="incompleta"):
        _medir(exe, Truncado(base.ventanas, base.controles))


def test_sin_backend_de_uia_la_medicion_lo_dice_y_no_inventa(exe) -> None:
    class SinBackend(ObservadorFalso):
        def ventanas_de_proceso(self, pid):
            raise UIANoDisponibleError("no hay UIA en esta plataforma")

    with pytest.raises(medir.MedicionError, match="no hay UIA"):
        _medir(exe, SinBackend([], {}))


# ---------------------------------------------------------------------------
# 2. El diagnóstico contra el contrato congelado (enumerado: cada contrato)
# ---------------------------------------------------------------------------


def _con_preset(tmp_path: pathlib.Path, contrato: ContratoDeUIHerramienta) -> pathlib.Path:
    """Un directorio de herramienta donde el preset del contrato SÍ existe."""
    base = tmp_path / f"herramienta-{contrato.tool}"
    preset = base.joinpath(*contrato.preset_relativo)
    preset.parent.mkdir(parents=True)
    preset.write_text("[DynDOLOD]\nPreset=Medium\n", encoding="utf-8")
    exe = base / f"{contrato.tool}x64.exe"
    exe.write_bytes(EXE_BYTES)
    return exe


def _diagnosticar(tmp_path, contrato, capturas, *, mutar=None):
    exe = _con_preset(tmp_path, contrato)
    mediciones = _medir_todas(exe, contrato, capturas)
    if mutar is not None:
        mutar(exe)
    return medir.diagnosticar(mediciones, contrato, directorio_del_exe=exe.parent)


@pytest.mark.parametrize("anidado", [False, True], ids=["dialogos_de_nivel_superior", "dialogos_anidados"])
@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: f"{c.tool}-{c.version}")
def test_la_gui_simulada_de_cada_contrato_congelado_da_compatible(tmp_path, contrato, anidado) -> None:
    diagnostico = _diagnosticar(tmp_path, contrato, _capturas(contrato, anidado=anidado))

    assert diagnostico.veredicto is medir.Veredicto.COMPATIBLE, diagnostico.resumen()
    assert all(c.estado is medir.Estado.COINCIDE for c in diagnostico.chequeos)
    nombres = {c.nombre for c in diagnostico.chequeos}
    assert {"ventana_principal", "selector_output", "accion_inicio", "dialogo_terminal", "preset"} <= nombres
    assert sum(n.startswith("modal:") for n in nombres) == len(contrato.modales_conocidos)


def test_el_ancla_cubre_todos_los_contratos_congelados_y_hay_al_menos_dos() -> None:
    """Si alguien agrega un contrato, el parametrizado de arriba lo cubre solo: no hay lista que mantener."""
    assert len(CONTRATOS) == len(CONTRATOS_DE_UI) >= 2
    assert {c.tool for c in CONTRATOS} == {"TexGen", "DynDOLOD"}


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_sin_las_capturas_de_los_dialogos_el_veredicto_es_incompleto_nunca_compatible(tmp_path, contrato) -> None:
    diagnostico = _diagnosticar(tmp_path, contrato, _capturas(contrato, con_dialogos=False))

    assert diagnostico.veredicto is medir.Veredicto.INCOMPLETO
    estados = {c.nombre: c.estado for c in diagnostico.chequeos}
    assert estados["dialogo_terminal"] is medir.Estado.NO_OBSERVADO
    assert estados["ventana_principal"] is medir.Estado.COINCIDE


def _renombrar_inicio(observador: ObservadorFalso, contrato) -> None:
    for i, c in enumerate(observador.controles["w0"]):
        if c.nombre == contrato.accion_inicio.selector.nombre:
            observador.controles["w0"][i] = ControlObservado(
                pid=c.pid, nombre="Begin", tipo_de_control=c.tipo_de_control, class_name=c.class_name
            )


def _quitar_output(observador: ObservadorFalso, contrato) -> None:
    observador.controles["w0"].pop(0)  # el primer control de la ventana principal es el Output


def _otra_clase_de_ventana(observador: ObservadorFalso, contrato) -> None:
    observador.ventanas[0] = VentanaObservada(pid=PID, titulo="x", class_name="TfrmOtra", handle="w0")


def _duplicar_inicio(observador: ObservadorFalso, contrato) -> None:
    observador.controles["w0"].append(_ctl(contrato.accion_inicio.selector, tipo="Button", clase="TButton"))


MUTACIONES = {
    "boton_de_inicio_renombrado": ("accion_inicio", _renombrar_inicio),
    "boton_de_inicio_duplicado": ("accion_inicio", _duplicar_inicio),
    "campo_output_ausente": ("selector_output", _quitar_output),
    "clase_de_ventana_distinta": ("ventana_principal", _otra_clase_de_ventana),
}


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
@pytest.mark.parametrize("mutacion", sorted(MUTACIONES))
def test_cada_cambio_de_la_gui_hace_que_el_contrato_difiera_en_el_chequeo_que_corresponde(
    tmp_path, contrato, mutacion
) -> None:
    chequeo, mutar_gui = MUTACIONES[mutacion]
    capturas = _capturas(contrato)
    mutar_gui(capturas[0][1], contrato)  # la captura de reposo

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is medir.Veredicto.DIFIERE, diagnostico.resumen()
    estados = {c.nombre: c.estado for c in diagnostico.chequeos}
    assert estados[chequeo] is medir.Estado.DIFIERE


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_preset_ausente_en_el_build_nuevo_difiere(tmp_path, contrato) -> None:
    def borrar_preset(exe: pathlib.Path) -> None:
        exe.parent.joinpath(*contrato.preset_relativo).unlink()

    diagnostico = _diagnosticar(tmp_path, contrato, _capturas(contrato), mutar=borrar_preset)

    assert diagnostico.veredicto is medir.Veredicto.DIFIERE
    assert {c.nombre: c.estado for c in diagnostico.chequeos}["preset"] is medir.Estado.DIFIERE


# --- Los tres hallazgos de Codex en el PR #907, como regresiones -----------------------------------------------------


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_cambio_solo_de_titulo_entre_capturas_no_es_una_ventana_principal_duplicada(tmp_path, contrato) -> None:
    """Hallazgo 2: el diagnóstico contaba las versiones sucesivas de la ventana como simultáneas y daba ``difiere``."""
    capturas = _capturas(contrato)
    capturas.insert(1, ("reposo-con-otro-titulo", _captura(contrato, titulo=f"{contrato.tool} - trabajando…")))

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is medir.Veredicto.COMPATIBLE, diagnostico.resumen()


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_dialogo_anidado_en_la_ventana_principal_se_reconoce(tmp_path, contrato) -> None:
    """Hallazgo 3: excluir la ventana principal dejaba ``no_observado`` un diálogo que el driver sí reconoce."""
    diagnostico = _diagnosticar(tmp_path, contrato, _capturas(contrato, anidado=True))

    estados = {c.nombre: c.estado for c in diagnostico.chequeos}
    assert estados["dialogo_terminal"] is medir.Estado.COINCIDE, diagnostico.resumen()
    assert all(estados[f"modal:{m.id}"] is medir.Estado.COINCIDE for m in contrato.modales_conocidos)


@pytest.mark.parametrize("anidado", [False, True], ids=["nivel_superior", "anidado"])
@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_boton_de_otra_clase_no_se_reconoce_como_el_dialogo_y_nunca_da_compatible(
    tmp_path, contrato, anidado
) -> None:
    """Hallazgo 1a: el driver exige botones ``CCPushButton``; con otra clase no puede ni reconocer ni operar el diálogo."""
    capturas = _capturas(contrato, anidado=anidado, con_dialogos=False)
    capturas.append(
        ("dialogo-terminal", _captura(contrato, contrato.dialogo_terminal, anidado=anidado, clase_boton="TButton"))
    )

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is not medir.Veredicto.COMPATIBLE, diagnostico.resumen()
    assert {c.nombre: c.estado for c in diagnostico.chequeos}["dialogo_terminal"] is not medir.Estado.COINCIDE


@pytest.mark.parametrize("instrucciones", [0, 2], ids=["sin_instruccion", "instruccion_duplicada"])
@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_el_dialogo_sin_exactamente_un_maininstruction_no_se_reconoce(tmp_path, contrato, instrucciones) -> None:
    """Hallazgo 1b: el driver exige UN solo ``MainInstruction``; buscar la instrucción en cualquier control era más laxo."""
    capturas = _capturas(contrato, con_dialogos=False)
    capturas.append(("dialogo-terminal", _captura(contrato, contrato.dialogo_terminal, instrucciones=instrucciones)))

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is not medir.Veredicto.COMPATIBLE
    assert {c.nombre: c.estado for c in diagnostico.chequeos}["dialogo_terminal"] is not medir.Estado.COINCIDE


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_una_accion_del_dialogo_duplicada_difiere_porque_el_driver_la_rechaza(tmp_path, contrato) -> None:
    """Hallazgo 1c: el driver rechaza (``AMBIGUA``) un diálogo cuya acción resuelve a más de un elemento."""
    capturas = _capturas(contrato, con_dialogos=False)
    duplicada = _captura(contrato, contrato.dialogo_terminal)
    duplicada.controles["w1"].append(_ctl(contrato.accion_terminal.selector, tipo="Button", clase=CLASE_BOTON))
    capturas.append(("dialogo-terminal", duplicada))

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is medir.Veredicto.DIFIERE, diagnostico.resumen()
    assert {c.nombre: c.estado for c in diagnostico.chequeos}["dialogo_terminal"] is medir.Estado.DIFIERE


def test_dos_dialogos_en_una_misma_captura_no_cuentan_como_evidencia(tmp_path) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    a, b = _captura(contrato, contrato.dialogo_terminal), _captura(contrato, contrato.modales_conocidos[0].firma)
    juntas = ObservadorFalso(
        [*a.ventanas, dataclasses.replace(b.ventanas[1], handle="w2")],
        {**a.controles, "w2": b.controles["w1"]},
    )

    diagnostico = _diagnosticar(tmp_path, contrato, [("reposo", _captura(contrato)), ("dos-dialogos", juntas)])

    assert diagnostico.veredicto is not medir.Veredicto.COMPATIBLE
    assert any(
        c.nombre == "captura:dos-dialogos" and c.estado is medir.Estado.NO_OBSERVADO for c in diagnostico.chequeos
    )


# --- La paridad con el driver de producción --------------------------------------------------------------------------


def _como_instantanea(medicion: medir.Medicion) -> InstantaneaUI:
    return InstantaneaUI(
        pid=PID,
        ventanas=tuple(
            VentanaUI(
                titulo=v.titulo,
                class_name=v.class_name,
                elementos=tuple(
                    ElementoUI(
                        tipo_de_control=c.tipo_de_control,
                        class_name=c.class_name,
                        nombre=c.nombre,
                        automation_id=c.automation_id,
                        habilitado=True,
                    )
                    for c in v.controles
                ),
            )
            for v in medicion.ventanas
        ),
    )


def _escenarios(contrato: ContratoDeUIHerramienta):
    """Capturas enumeradas: las normales y cada patología que el driver distingue."""
    terminal = contrato.dialogo_terminal
    yield "reposo", _captura(contrato)
    yield "reposo-otro-titulo", _captura(contrato, titulo="otro titulo")
    for anidado in (False, True):
        s = "anidado" if anidado else "nivel_superior"
        yield f"terminal-{s}", _captura(contrato, terminal, anidado=anidado)
        for modal in contrato.modales_conocidos:
            yield f"modal-{modal.id}-{s}", _captura(contrato, modal.firma, anidado=anidado)
        yield f"boton-otra-clase-{s}", _captura(contrato, terminal, anidado=anidado, clase_boton="TButton")
        yield f"instruccion-duplicada-{s}", _captura(contrato, terminal, anidado=anidado, instrucciones=2)
        yield f"sin-instruccion-{s}", _captura(contrato, terminal, anidado=anidado, instrucciones=0)
        desconocido = FirmaDeDialogo(instruccion="una instruccion que ningun contrato conoce", botones=terminal.botones)
        yield f"desconocido-{s}", _captura(contrato, desconocido, anidado=anidado)
        duplicada = _captura(contrato, terminal, anidado=anidado)
        duplicada.controles["w0" if anidado else "w1"].append(
            _ctl(contrato.accion_terminal.selector, tipo="Button", clase=CLASE_BOTON)
        )
        yield f"accion-terminal-duplicada-{s}", duplicada
    yield "sin-ventanas", ObservadorFalso([], {})
    dos = _captura(contrato)
    dos.ventanas.append(
        VentanaObservada(pid=PID, titulo="otra", class_name=contrato.clase_ventana_principal, handle="w9")
    )
    dos.controles["w9"] = []
    yield "dos-ventanas-principales", dos
    sin_output = _captura(contrato)
    sin_output.controles["w0"] = [c for c in sin_output.controles["w0"] if c.class_name == "TMemo"]
    yield "sin-output", sin_output
    dos_outputs = _captura(contrato)
    dos_outputs.controles["w0"].append(_ctl(contrato.selector_output))
    yield "dos-outputs", dos_outputs
    inicio_duplicado = _captura(contrato)
    inicio_duplicado.controles["w0"].append(_ctl(contrato.accion_inicio.selector, tipo="Button", clase="TButton"))
    yield "inicio-duplicado", inicio_duplicado
    a, b = _captura(contrato, terminal), _captura(contrato, terminal)
    yield (
        "dos-dialogos-simultaneos",
        ObservadorFalso(
            [*a.ventanas, dataclasses.replace(b.ventanas[1], handle="w2")], {**a.controles, "w2": b.controles["w1"]}
        ),
    )


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_la_clasificacion_de_cada_captura_coincide_con_la_del_driver_de_produccion(exe, contrato) -> None:
    """El script no puede importar al driver (es la superficie que actúa), así que su lógica está duplicada: ESTO la ata."""
    vistos: set[str] = set()
    for nombre, observador in _escenarios(contrato):
        medicion = _medir(exe, observador, tool=contrato.tool, etiqueta=nombre, localizador=_localizador(exe))

        mia = medir.clasificar_captura(contrato, medicion)
        driver = clasificar_pantalla(contrato, _como_instantanea(medicion))

        assert mia.tipo == driver.tipo.value, (nombre, mia, driver)
        vistos.add(mia.tipo)
    assert vistos >= {
        medir.ASISTENTE,
        medir.CARGANDO,
        medir.MODAL_CONOCIDO,
        medir.DIALOGO_TERMINAL,
        medir.DIALOGO_DESCONOCIDO,
        medir.AMBIGUA,
        medir.SIN_VENTANA,
    }, f"los escenarios no cubren todos los tipos de pantalla: {sorted(vistos)}"


# ---------------------------------------------------------------------------
# 3. Capturas de un mismo build
# ---------------------------------------------------------------------------


def test_las_capturas_de_otro_build_no_se_mezclan(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    reposo = _medir(exe, _captura(contrato))
    otro_build = dataclasses.replace(reposo, exe_sha256="0" * 64, etiqueta="x")

    assert medir.exigir_mismo_build([reposo, reposo]) == reposo
    with pytest.raises(medir.MedicionError, match="distinto build"):
        medir.exigir_mismo_build([reposo, otro_build])
    with pytest.raises(medir.MedicionError, match="no hay mediciones"):
        medir.exigir_mismo_build([])


# ---------------------------------------------------------------------------
# 4. El CLI
# ---------------------------------------------------------------------------


def _informes(tmp_path, exe, contrato, capturas) -> list[str]:
    rutas = []
    for i, medicion in enumerate(_medir_todas(exe, contrato, capturas)):
        archivo = tmp_path / f"med{i}.json"
        archivo.write_text(medir.a_json(medicion), encoding="utf-8")
        rutas.append(str(archivo))
    return rutas


def test_el_cli_diagnostica_desde_archivos_sin_necesitar_uia(tmp_path, capsys) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    exe = _con_preset(tmp_path, contrato)
    informes = _informes(tmp_path, exe, contrato, _capturas(contrato))

    codigo = medir.main(["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe), *informes])

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "compatible" in salida.lower()


def test_exigir_compatible_devuelve_un_codigo_distinto_si_el_veredicto_no_lo_es(tmp_path) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    exe = _con_preset(tmp_path, contrato)
    informes = _informes(tmp_path, exe, contrato, _capturas(contrato, con_dialogos=False))

    argv = ["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe), *informes]

    assert medir.main(argv) == 0
    assert medir.main([*argv, "--exigir-compatible"]) == medir.SALIDA_NO_COMPATIBLE


def _cli_con_gui_simulada(monkeypatch: pytest.MonkeyPatch, exe: pathlib.Path) -> None:
    """El CLI con una GUI y un proceso simulados: la medición TIENE éxito, así que lo que se prueba es lo demás."""
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    monkeypatch.setattr(medir, "_observador_real", lambda: _captura(contrato))
    monkeypatch.setattr(medir, "LocalizadorPsutil", lambda: _localizador(exe))


def test_el_cli_medir_escribe_el_informe_fuera_del_directorio_de_la_herramienta(tmp_path, exe, monkeypatch) -> None:
    """Control positivo: sin él, el test de abajo pasaría también si la medición fallara por cualquier otro motivo."""
    _cli_con_gui_simulada(monkeypatch, exe)
    salida = tmp_path / "informes" / "med.json"
    salida.parent.mkdir()

    codigo = medir.main(["medir", "--tool", "DynDOLOD", "--exe", str(exe), "--modo", "SSE", "--salida", str(salida)])

    assert codigo == 0
    assert medir.de_json(salida.read_text(encoding="utf-8")).exe_sha256 == SHA_DEL_EXE


def test_el_cli_no_escribe_dentro_del_directorio_de_la_herramienta(exe, monkeypatch) -> None:
    _cli_con_gui_simulada(monkeypatch, exe)
    prohibida = exe.parent / "informe.json"

    codigo = medir.main(["medir", "--tool", "DynDOLOD", "--exe", str(exe), "--modo", "SSE", "--salida", str(prohibida)])

    assert codigo == medir.SALIDA_ERROR
    assert not prohibida.exists(), "el informe nunca se escribe dentro del directorio de la herramienta"


def test_el_cli_medir_sin_el_proceso_falla_cerrado_y_sin_archivo(tmp_path, exe) -> None:
    """Sin el proceso (y fuera de Windows, sin UIA) la medición se niega: no deja un informe a medias."""
    salida = tmp_path / "informes" / "med.json"
    salida.parent.mkdir()

    codigo = medir.main(["medir", "--tool", "DynDOLOD", "--exe", str(exe), "--modo", "SSE", "--salida", str(salida)])

    assert codigo == medir.SALIDA_ERROR
    assert not salida.exists()


def test_el_json_de_una_medicion_es_estable_y_se_puede_releer(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    medicion = _medir(exe, _captura(contrato))

    texto = medir.a_json(medicion)

    assert medir.de_json(texto) == medicion
    crudo = json.loads(texto)
    assert crudo["esquema"] == medir.ESQUEMA
    assert crudo["medicion"]["exe_sha256"] == SHA_DEL_EXE


# --- Revisión de CodeRabbit (#907) ------------------------------------------------------------------------------------


@pytest.mark.parametrize("anidado", [False, True], ids=["nivel_superior", "anidado"])
@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_dialogo_desconocido_capturado_impide_el_compatible_aunque_esten_todos_los_conocidos(
    tmp_path, contrato, anidado
) -> None:
    """Con TODAS las capturas conocidas más un diálogo sin firma, el driver no sabría qué hacer con ese diálogo.

    Antes la captura desconocida solo aparecía en el diagnóstico si faltaba algún diálogo conocido; con el set completo el
    veredicto salía ``compatible`` y la captura desaparecía.
    """
    desconocido = FirmaDeDialogo(
        instruccion="Un diálogo que ningún contrato conoce", botones=frozenset({("Aceptar", "")})
    )
    capturas = [
        *_capturas(contrato, anidado=anidado),
        ("dialogo-desconocido", _captura(contrato, desconocido, anidado=anidado)),
    ]

    diagnostico = _diagnosticar(tmp_path, contrato, capturas)

    assert diagnostico.veredicto is not medir.Veredicto.COMPATIBLE, diagnostico.resumen()
    assert any(
        c.nombre == "captura:dialogo-desconocido" and c.estado is medir.Estado.NO_OBSERVADO
        for c in diagnostico.chequeos
    ), diagnostico.resumen()


def _diagnosticar_por_cli(exe_medido: pathlib.Path, exe_del_diagnostico: pathlib.Path, tmp_path, contrato) -> int:
    informes = _informes(tmp_path, exe_medido, contrato, _capturas(contrato))
    argv = ["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe_del_diagnostico), *informes]
    return medir.main(argv)


def test_el_diagnostico_se_niega_si_el_exe_indicado_no_es_el_ejecutable_medido(tmp_path, capsys) -> None:
    """El preset se busca junto a ``--exe``: si ese ``--exe`` es OTRO build, su preset avalaría un build que no es."""
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    medido = _con_preset(tmp_path / "medido", contrato)
    otro = _con_preset(tmp_path / "otro", contrato)
    otro.write_bytes(b"otro-build-distinto")  # tiene el preset, pero no es el ejecutable que se midió

    assert _diagnosticar_por_cli(medido, medido, tmp_path, contrato) == 0, "control positivo: el mismo exe se acepta"
    capsys.readouterr()
    codigo = _diagnosticar_por_cli(medido, otro, tmp_path, contrato)

    assert codigo == medir.SALIDA_ERROR
    assert "no es el ejecutable medido" in capsys.readouterr().err


def test_el_diagnostico_se_niega_si_el_exe_fue_reemplazado_en_la_misma_ruta(tmp_path, capsys) -> None:
    """Una actualización que pisa el ejecutable en el mismo lugar: el hash ya no es el de la medición."""
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    exe_ = _con_preset(tmp_path, contrato)
    informes = _informes(tmp_path, exe_, contrato, _capturas(contrato))
    exe_.write_bytes(b"build-actualizado-sobre-la-misma-ruta")

    codigo = medir.main(["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe_), *informes])

    assert codigo == medir.SALIDA_ERROR
    assert "no es el ejecutable medido" in capsys.readouterr().err


def test_medir_con_la_carpeta_de_salida_inexistente_falla_antes_de_medir(tmp_path, exe, monkeypatch, capsys) -> None:
    """Medir es lo caro (una GUI abierta y preparada); perder la medición al escribir sería lo peor."""
    medido: list[bool] = []
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    monkeypatch.setattr(medir, "_observador_real", lambda: medido.append(True) or _captura(contrato))
    monkeypatch.setattr(medir, "LocalizadorPsutil", lambda: _localizador(exe))
    salida = tmp_path / "no-existe" / "med.json"

    codigo = medir.main(["medir", "--tool", "DynDOLOD", "--exe", str(exe), "--modo", "SSE", "--salida", str(salida)])

    assert codigo == medir.SALIDA_ERROR
    assert medido == [], "no se mide si el informe no se va a poder guardar"
    assert "carpeta" in capsys.readouterr().err and not salida.parent.exists()


def _informe_con_sha(exe_, sha: object) -> str:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    crudo = json.loads(medir.a_json(_medir(exe_, _captura(contrato))))
    crudo["medicion"]["exe_sha256"] = sha
    return json.dumps(crudo)


@pytest.mark.parametrize(
    "invalido",
    [None, 12345, "", "abc", "g" * 64, "a" * 63, "a" * 65, ["a" * 64]],
    ids=["null", "entero", "vacio", "corto", "no_hex", "63", "65", "lista"],
)
def test_un_informe_sin_el_hash_valido_del_build_se_rechaza_al_cargarlo(exe, invalido) -> None:
    """Sin identidad de build válida, ``exigir_mismo_build`` aceptaría informes de cualquier build (todos ``None``)."""
    with pytest.raises(medir.MedicionError, match="exe_sha256"):
        medir.de_json(_informe_con_sha(exe, invalido))


def test_un_informe_con_el_hash_en_mayusculas_se_normaliza(exe) -> None:
    assert medir.de_json(_informe_con_sha(exe, SHA_DEL_EXE.upper())).exe_sha256 == SHA_DEL_EXE


def test_el_cli_no_diagnostica_informes_sin_hash_de_build(tmp_path, exe, capsys) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    informes = []
    for i in range(2):
        archivo = tmp_path / f"sin-hash{i}.json"
        archivo.write_text(_informe_con_sha(exe, None), encoding="utf-8")
        informes.append(str(archivo))

    codigo = medir.main(["diagnosticar", "--contra-sha", contrato.exe_sha256, *informes])

    assert codigo == medir.SALIDA_ERROR
    assert "exe_sha256" in capsys.readouterr().err
