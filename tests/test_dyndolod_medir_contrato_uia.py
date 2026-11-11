"""H-8: no había herramienta para medir un build nuevo de TexGen/DynDOLOD y darlo de alta.

Cualquier actualización (o un rig VR) cae en ``UNATTENDED_BUILD_UNSUPPORTED`` y el único camino era un rig manual sin
procedimiento. ``scripts/dyndolod_medir_contrato_uia.py`` mide la UI de un proceso VIVO por el puerto de LECTURA
(``ObservadorUIA``) y diagnostica qué partes del contrato congelado siguen valiendo en ese build.

Lo que se ancla acá, sin rig (la GUI es simulada: se DERIVA del propio contrato congelado):

- la identidad del proceso es estricta (ruta exacta, una sola instancia) y nunca se amplía el alcance a otros pids;
- una enumeración incompleta o un backend ausente cortan: jamás un recorte silencioso;
- el diagnóstico, corrido sobre la GUI simulada de CADA contrato congelado, da ``compatible`` (el «candidato igual al
  contrato congelado» del plan, a nivel de estructura), y cada mutación de la GUI cambia el veredicto;
- «compatible» exige haber observado TODO: sin las instantáneas del diálogo terminal y de los modales el veredicto es
  ``incompleto``, nunca un «sí» por omisión.

**No prueba** que la GUI real de Alpha-209 produzca ese mismo candidato: eso es el smoke del rig.
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
)
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
        self.llamadas: list[str] = []

    def ventanas_de_proceso(self, pid: int) -> list[VentanaObservada]:
        self.llamadas.append("ventanas_de_proceso")
        return list(self.ventanas)

    def controles_de_ventana(self, ventana: VentanaObservada) -> list[ControlObservado]:
        self.llamadas.append("controles_de_ventana")
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


def _dialogo(contrato_firma, *, handle: str, pid: int = PID) -> tuple[VentanaObservada, list[ControlObservado]]:
    clase_boton = MARCAS_DEL_DIALOGO_DE_TAREA["class_name_boton"]
    controles = [
        ControlObservado(pid=pid, nombre=contrato_firma.instruccion, tipo_de_control="Text", class_name="Static"),
        *(
            ControlObservado(
                pid=pid, nombre=nombre, automation_id=aid, tipo_de_control="Button", class_name=clase_boton
            )
            for nombre, aid in sorted(contrato_firma.botones)
        ),
    ]
    return VentanaObservada(pid=pid, titulo="dialogo", class_name="TfrmTask", handle=handle), controles


def _gui_simulada(contrato: ContratoDeUIHerramienta, *, pid: int = PID, con_dialogos: bool = True):
    """La GUI de ``contrato`` DERIVADA del contrato: ventana principal, y (opcional) diálogo terminal y modales."""
    principal = VentanaObservada(
        pid=pid, titulo=contrato.tool, class_name=contrato.clase_ventana_principal, handle="w0"
    )
    controles_principal = [
        _ctl(contrato.selector_output, pid=pid),
        # El control que COLISIONA con Output por tipo: sin él la unicidad del selector no se estaría probando.
        ControlObservado(pid=pid, tipo_de_control="Edit", class_name="TMemo", nombre="log"),
        _ctl(contrato.accion_inicio.selector, pid=pid, tipo="Button", clase="TButton"),
    ]
    if contrato.accion_previa is not None:
        controles_principal.append(_ctl(contrato.accion_previa.selector, pid=pid, tipo="Button", clase="TButton"))
    ventanas, controles = [principal], {"w0": controles_principal}
    if con_dialogos:
        firmas = [contrato.dialogo_terminal, *(m.firma for m in contrato.modales_conocidos)]
        for i, firma in enumerate(firmas, start=1):
            ventana, ctl = _dialogo(firma, handle=f"w{i}", pid=pid)
            ventanas.append(ventana)
            controles[f"w{i}"] = ctl
    return ObservadorFalso(ventanas, controles)


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


# ---------------------------------------------------------------------------
# 1. La medición
# ---------------------------------------------------------------------------


def test_la_medicion_registra_el_build_y_la_ui_del_proceso(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")

    medicion = _medir(exe, _gui_simulada(contrato))

    assert (medicion.tool, medicion.modo_de_juego, medicion.etiqueta) == ("DynDOLOD", "SSE", "reposo")
    assert medicion.exe_sha256 == SHA_DEL_EXE, "el hash sale del ARCHIVO, no de lo que diga el operador"
    clases = {v.class_name for v in medicion.ventanas}
    assert contrato.clase_ventana_principal in clases
    principal = next(v for v in medicion.ventanas if v.class_name == contrato.clase_ventana_principal)
    assert any(c.class_name == "TMemo" for c in principal.controles)


def test_la_medicion_es_determinista_aunque_uia_devuelva_en_otro_orden(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    base = _gui_simulada(contrato)
    barajada = _gui_simulada(contrato)
    random.Random(7).shuffle(barajada.ventanas)
    for clave in barajada.controles:
        random.Random(9).shuffle(barajada.controles[clave])

    assert _medir(exe, base) == _medir(exe, barajada), "el orden de UIA no es parte de lo que se mide"


def test_nunca_se_amplia_el_alcance_a_ventanas_ni_controles_de_otro_pid(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    observador = _gui_simulada(contrato)
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
    base = _gui_simulada(contrato)

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


def _diagnosticar(tmp_path, contrato, observador, *, mutar=None):
    exe = _con_preset(tmp_path, contrato)
    medicion = _medir(exe, observador, tool=contrato.tool, localizador=_localizador(exe))
    if mutar is not None:
        mutar(exe)
    return medir.diagnosticar(medicion, contrato, directorio_del_exe=exe.parent)


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: f"{c.tool}-{c.version}")
def test_la_gui_simulada_de_cada_contrato_congelado_da_compatible(tmp_path, contrato) -> None:
    diagnostico = _diagnosticar(tmp_path, contrato, _gui_simulada(contrato))

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
def test_sin_las_instantaneas_de_los_dialogos_el_veredicto_es_incompleto_nunca_compatible(tmp_path, contrato) -> None:
    diagnostico = _diagnosticar(tmp_path, contrato, _gui_simulada(contrato, con_dialogos=False))

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


MUTACIONES = {
    "boton_de_inicio_renombrado": (
        "accion_inicio",
        lambda o, c: _renombrar_inicio(o, c),
    ),
    "boton_de_inicio_duplicado": (
        "accion_inicio",
        lambda o, c: o.controles["w0"].append(
            ControlObservado(
                pid=PID,
                nombre=c.accion_inicio.selector.nombre or "",
                automation_id=c.accion_inicio.selector.automation_id or "",
                tipo_de_control=c.accion_inicio.selector.tipo_de_control or "Button",
                class_name=c.accion_inicio.selector.class_name or "TButton",
            )
        ),
    ),
    "campo_output_ausente": (
        "selector_output",
        lambda o, c: o.controles["w0"].remove(
            next(
                x for x in o.controles["w0"] if x.class_name != "TMemo" and x.nombre != c.accion_inicio.selector.nombre
            )
        ),
    ),
    "clase_de_ventana_distinta": (
        "ventana_principal",
        lambda o, c: o.ventanas.__setitem__(
            0, VentanaObservada(pid=PID, titulo="x", class_name="TfrmOtra", handle="w0")
        ),
    ),
}


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
@pytest.mark.parametrize("mutacion", sorted(MUTACIONES))
def test_cada_cambio_de_la_gui_hace_que_el_contrato_difiera_en_el_chequeo_que_corresponde(
    tmp_path, contrato, mutacion
) -> None:
    chequeo, mutar_gui = MUTACIONES[mutacion]
    observador = _gui_simulada(contrato)
    mutar_gui(observador, contrato)

    diagnostico = _diagnosticar(tmp_path, contrato, observador)

    assert diagnostico.veredicto is medir.Veredicto.DIFIERE, diagnostico.resumen()
    estados = {c.nombre: c.estado for c in diagnostico.chequeos}
    assert estados[chequeo] is medir.Estado.DIFIERE


@pytest.mark.parametrize("contrato", CONTRATOS, ids=lambda c: c.tool)
def test_un_preset_ausente_en_el_build_nuevo_difiere(tmp_path, contrato) -> None:
    def borrar_preset(exe: pathlib.Path) -> None:
        exe.parent.joinpath(*contrato.preset_relativo).unlink()

    diagnostico = _diagnosticar(tmp_path, contrato, _gui_simulada(contrato), mutar=borrar_preset)

    assert diagnostico.veredicto is medir.Veredicto.DIFIERE
    assert {c.nombre: c.estado for c in diagnostico.chequeos}["preset"] is medir.Estado.DIFIERE


def test_un_dialogo_con_los_botones_pero_otra_instruccion_no_coincide_con_el_terminal(tmp_path) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    observador = _gui_simulada(contrato)
    terminal = next(
        i
        for i, ctl in observador.controles.items()
        if any(x.nombre == contrato.dialogo_terminal.instruccion for x in ctl)
    )
    observador.controles[terminal] = [
        ControlObservado(pid=c.pid, nombre="Otro texto", tipo_de_control=c.tipo_de_control, class_name=c.class_name)
        if c.nombre == contrato.dialogo_terminal.instruccion
        else c
        for c in observador.controles[terminal]
    ]

    diagnostico = _diagnosticar(tmp_path, contrato, observador)

    estados = {c.nombre: c.estado for c in diagnostico.chequeos}
    assert estados["dialogo_terminal"] is not medir.Estado.COINCIDE


# ---------------------------------------------------------------------------
# 3. Fusionar instantáneas de distintos momentos
# ---------------------------------------------------------------------------


def test_fusionar_une_las_ventanas_de_los_momentos_y_exige_el_mismo_build(tmp_path, exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    gui = _gui_simulada(contrato)
    reposo = _medir(exe, ObservadorFalso(gui.ventanas[:1], {"w0": gui.controles["w0"]}), etiqueta="reposo")
    terminal = _medir(
        exe,
        ObservadorFalso(gui.ventanas[1:2], {"w1": gui.controles["w1"]}),
        etiqueta="dialogo-terminal",
    )

    fusion = medir.fusionar([reposo, terminal])

    assert len(fusion.ventanas) == 2
    assert fusion.exe_sha256 == SHA_DEL_EXE

    otro_build = dataclasses.replace(reposo, exe_sha256="0" * 64, etiqueta="x")
    with pytest.raises(medir.MedicionError, match="distinto build"):
        medir.fusionar([reposo, otro_build])


# ---------------------------------------------------------------------------
# 4. El CLI
# ---------------------------------------------------------------------------


def test_el_cli_diagnostica_desde_archivos_sin_necesitar_uia(tmp_path, capsys) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    exe = _con_preset(tmp_path, contrato)
    medicion = _medir(exe, _gui_simulada(contrato), localizador=_localizador(exe), tool=contrato.tool)
    archivo = tmp_path / "med.json"
    archivo.write_text(medir.a_json(medicion), encoding="utf-8")

    codigo = medir.main(["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe), str(archivo)])

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "compatible" in salida.lower()


def test_exigir_compatible_devuelve_un_codigo_distinto_si_el_veredicto_no_lo_es(tmp_path, capsys) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    exe = _con_preset(tmp_path, contrato)
    medicion = _medir(
        exe, _gui_simulada(contrato, con_dialogos=False), localizador=_localizador(exe), tool=contrato.tool
    )
    archivo = tmp_path / "med.json"
    archivo.write_text(medir.a_json(medicion), encoding="utf-8")

    argv = ["diagnosticar", "--contra-sha", contrato.exe_sha256, "--exe", str(exe), str(archivo)]

    assert medir.main(argv) == 0
    assert medir.main([*argv, "--exigir-compatible"]) == 3


def _cli_con_gui_simulada(monkeypatch: pytest.MonkeyPatch, exe: pathlib.Path) -> None:
    """El CLI con una GUI y un proceso simulados: la medición TIENE éxito, así que lo que se prueba es lo demás."""
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    monkeypatch.setattr(medir, "_observador_real", lambda: _gui_simulada(contrato))
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

    assert codigo == 2
    assert not prohibida.exists(), "el informe nunca se escribe dentro del directorio de la herramienta"


def test_el_cli_medir_en_una_plataforma_sin_uia_falla_cerrado_y_sin_archivo(tmp_path, exe, capsys) -> None:
    salida = tmp_path / "informes" / "med.json"
    salida.parent.mkdir()

    codigo = medir.main(["medir", "--tool", "DynDOLOD", "--exe", str(exe), "--modo", "SSE", "--salida", str(salida)])

    assert codigo == 2
    assert not salida.exists()


def test_el_json_de_una_medicion_es_estable_y_se_puede_releer(exe) -> None:
    contrato = next(c for c in CONTRATOS if c.tool == "DynDOLOD")
    medicion = _medir(exe, _gui_simulada(contrato))

    texto = medir.a_json(medicion)

    assert medir.de_json(texto) == medicion
    crudo = json.loads(texto)
    assert crudo["esquema"] == medir.ESQUEMA
    assert crudo["medicion"]["exe_sha256"] == SHA_DEL_EXE
