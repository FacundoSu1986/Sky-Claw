"""GUI simulada de TexGen/DynDOLOD Alpha-209 para los tests del driver desatendido.

Reproduce la ESTRUCTURA medida en rig (P0-A/P0-B,
``docs/validation/2026-10-02_p0_uia_alpha209/``): el ``#32770`` anidado en
``TfrmMain`` con ``MainInstruction`` y botones ``CCPushButton``, el ``TEdit``
único de Output, los ``TButton`` ``Start``/``Advanced >>>``/``OK`` y la recarga
del Output desde el preset al entrar en Advanced. Implementa a la vez el
``BackendDeConduccion`` (lo que el helper ve y toca) y el
``LocalizadorDeProcesos`` (la identidad del pid).

No es una sonda: no sabe nada de COM. Registra cada escritura e invocación para
que los tests puedan exigir "cero mutaciones" o "exactamente una".
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Sequence

from sky_claw.local.tools.dyndolod_uia_contrato import (
    CONTRATOS_DE_UI,
    ContratoDeUIHerramienta,
    MecanismoDeEscritura,
)
from sky_claw.local.tools.dyndolod_uia_driver import ElementoUI, InstantaneaUI, VentanaUI
from sky_claw.local.tools.dyndolod_uia_preflight import ProcesoObservado

SHA_TEXGEN = "0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62"
SHA_DYNDOLOD = "b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0"
CONTRATO_TEXGEN: ContratoDeUIHerramienta = CONTRATOS_DE_UI[("TexGen", SHA_TEXGEN)]
CONTRATO_DYNDOLOD: ContratoDeUIHerramienta = CONTRATOS_DE_UI[("DynDOLOD", SHA_DYNDOLOD)]

EXE_TEXGEN = r"C:\Modding\DynDOLOD\TexGenx64.exe"
EXE_DYNDOLOD = r"C:\Modding\DynDOLOD\DynDOLODx64.exe"
OUTPUT_ADMINISTRADO = r"E:\SkyClawWork\DynDOLOD\TexGen"
OUTPUT_RANCIO = r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\TexGen\\"


def elemento(tipo: str, clase: str, nombre: str = "", automation_id: str = "", habilitado: bool = True) -> ElementoUI:
    return ElementoUI(
        tipo_de_control=tipo,
        class_name=clase,
        nombre=nombre,
        automation_id=automation_id,
        habilitado=habilitado,
    )


def _cromo_de_ventana() -> list[ElementoUI]:
    """Lo que toda ventana del rig arrastra y NO es parte del contrato."""
    return [
        elemento("Edit", "TMemo", automation_id="722020"),
        elemento("Pane", "TPanel", automation_id="5113500"),
        elemento("MenuBar", "", "Sistema", "MenuBar"),
        elemento("Button", "", "Minimizar"),
        elemento("Button", "", "Maximizar"),
        elemento("Button", "", "Cerrar"),
    ]


def elementos_de_dialogo(instruccion: str, botones: Sequence[tuple[str, str]], tool: str) -> list[ElementoUI]:
    return [
        elemento("Window", "#32770", tool),
        elemento("Image", "Element", "MainInstructionIcon", "MainIcon"),
        elemento("Text", "Element", instruccion, "MainInstruction"),
        *[elemento("Button", "CCPushButton", nombre, aid) for nombre, aid in botones],
        elemento("Button", "", "Cerrar"),
    ]


def ventana_principal(elementos: list[ElementoUI], titulo: str = "TexGen 3.0 Alpha-209 x64") -> VentanaUI:
    return VentanaUI(titulo=titulo, class_name="TfrmMain", elementos=tuple([*elementos, *_cromo_de_ventana()]))


def output_edit(habilitado: bool = True) -> ElementoUI:
    return elemento("Edit", "TEdit", automation_id="6293158", habilitado=habilitado)


def boton(nombre: str, habilitado: bool = True) -> ElementoUI:
    return elemento("Button", "TButton", nombre, automation_id="3016382", habilitado=habilitado)


class GuiSimulada:
    """Máquina de estados de la GUI. Thread-safe: el ejecutor en proceso usa hilos."""

    def __init__(
        self,
        contrato: ContratoDeUIHerramienta,
        *,
        pid: int = 4242,
        exe: str | None = None,
        ruta_observada: str | None = None,
        modal_inicial: bool = False,
        output_inicial: str = OUTPUT_RANCIO,
        output_tras_advanced: str | None = None,
        raiz_output: pathlib.Path | None = None,
        polls_de_generacion: int = 2,
        inicio_habilitado: bool = True,
        hresult_escritura: str = "S_OK",
        escritura_no_persiste: bool = False,
        hresult_invoke: dict[str, str] | None = None,
        excepcion_en_invoke: set[str] | None = None,
        dialogo_en_generacion: tuple[str, tuple[tuple[str, str], ...]] | None = None,
        sale_sin_dialogo: bool = False,
        no_sale_tras_terminal: bool = False,
        inicio_sin_progreso: bool = False,
        modal_reaparece: bool = False,
        terminal_ambiguo: bool = False,
        ruta_log: pathlib.Path | None = None,
        ruta_preset: pathlib.Path | None = None,
    ) -> None:
        self.contrato = contrato
        self.pid = pid
        self.exe = exe if exe is not None else (EXE_TEXGEN if contrato.tool == "TexGen" else EXE_DYNDOLOD)
        self.ruta_observada = ruta_observada if ruta_observada is not None else self.exe
        self.estado = "modal" if modal_inicial else "asistente"
        self.valor_output = output_inicial
        self.output_tras_advanced = output_tras_advanced
        self.raiz_output = raiz_output
        self.polls_de_generacion = polls_de_generacion
        self.inicio_habilitado = inicio_habilitado
        self.hresult_escritura = hresult_escritura
        self.escritura_no_persiste = escritura_no_persiste
        self.hresult_invoke = hresult_invoke or {}
        self.excepcion_en_invoke = excepcion_en_invoke or set()
        self.dialogo_en_generacion = dialogo_en_generacion
        self.sale_sin_dialogo = sale_sin_dialogo
        self.no_sale_tras_terminal = no_sale_tras_terminal
        self.inicio_sin_progreso = inicio_sin_progreso
        self.modal_reaparece = modal_reaparece
        self.terminal_ambiguo = terminal_ambiguo
        self.ruta_log = ruta_log
        self.ruta_preset = ruta_preset
        self.vivo = True
        self.escrituras: list[tuple[MecanismoDeEscritura, str]] = []
        self.invocaciones: list[str] = []
        #: Cuántas instantáneas se sirvieron (F2: cuántas lecturas costó un paso).
        self.lecturas = 0
        self._polls = 0
        self._guion: list[tuple[VentanaUI, ...]] = []
        self._repetir_ultima = False
        self._lock = threading.Lock()

    # -- estado observable ------------------------------------------------------

    def _ventanas(self) -> tuple[VentanaUI, ...]:
        contrato = self.contrato
        if self.estado == "modal":
            modal = contrato.modales_conocidos[0]
            return (
                ventana_principal(
                    elementos_de_dialogo(modal.firma.instruccion, sorted(modal.firma.botones), contrato.tool)
                ),
            )
        if self.estado == "asistente":
            if contrato.accion_previa is not None:
                return (ventana_principal([output_edit(), boton("Advanced >>>"), boton("Help")]),)
            return (ventana_principal([output_edit(), boton("Start", self.inicio_habilitado), boton("Help")]),)
        if self.estado == "avanzado":
            return (
                ventana_principal(
                    [output_edit(), boton("OK", self.inicio_habilitado), boton("Cancel"), boton("<<< Basic")]
                ),
            )
        if self.estado == "generando":
            return (ventana_principal([]),)
        if self.estado == "dialogo_inesperado" and self.dialogo_en_generacion is not None:
            instruccion, botones = self.dialogo_en_generacion
            return (ventana_principal(elementos_de_dialogo(instruccion, botones, contrato.tool)),)
        if self.estado == "terminal":
            elementos = elementos_de_dialogo(
                contrato.dialogo_terminal.instruccion, sorted(contrato.dialogo_terminal.botones), contrato.tool
            )
            if self.terminal_ambiguo:
                nombre = contrato.accion_terminal.selector.nombre or ""
                elementos.append(
                    elemento("Button", "CCPushButton", nombre, contrato.accion_terminal.selector.automation_id or "")
                )
            return (ventana_principal(elementos),)
        return ()

    def _avanzar_generacion(self) -> None:
        if self.estado != "generando":
            return
        self._polls += 1
        if self._polls < self.polls_de_generacion:
            return
        if self.sale_sin_dialogo:
            self.vivo = False
            self.estado = "salido"
        elif self.dialogo_en_generacion is not None:
            self.estado = "dialogo_inesperado"
        else:
            self.estado = "terminal"

    # -- guion de instantáneas (huecos de observación de F2) --------------------

    def armar_guion(
        self,
        instantaneas: Sequence[tuple[VentanaUI, ...]],
        *,
        repetir_ultima: bool = False,
    ) -> None:
        """Sirve estas instantáneas, en orden, ANTES de la real.

        Modela el hueco de observación medido en el rig Alpha-209 (F2, #773): la
        aprobación del modal ya ocurrió y el árbol UIA todavía no muestra el
        ``#32770`` porque la ventana está repintando. ``repetir_ultima`` deja el
        hueco abierto indefinidamente (el caso "nunca cierra").
        """
        with self._lock:
            self._guion = list(instantaneas)
            self._repetir_ultima = repetir_ultima

    def _siguiente_guion(self) -> tuple[VentanaUI, ...] | None:
        """La próxima instantánea forzada, o ``None`` si el guion se agotó."""
        if not self._guion:
            return None
        if self._repetir_ultima and len(self._guion) == 1:
            return self._guion[0]
        return self._guion.pop(0)

    # -- LocalizadorDeProcesos --------------------------------------------------

    def procesos(self) -> Sequence[ProcesoObservado]:
        with self._lock:
            if not self.vivo:
                return ()
            return (
                ProcesoObservado(
                    pid=self.pid, nombre_ejecutable=self.exe.rsplit("\\", 1)[-1], ruta_ejecutable=self.ruta_observada
                ),
            )

    # -- BackendDeConduccion ----------------------------------------------------

    def instantanea(self, pid: int) -> InstantaneaUI:
        with self._lock:
            self._avanzar_generacion()
            self.lecturas += 1
            forzadas = self._siguiente_guion()
            ventanas = forzadas if forzadas is not None else (self._ventanas() if self.vivo and pid == self.pid else ())
            return InstantaneaUI(
                pid=pid,
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
                                referencia=e.nombre or e.class_name,
                            )
                            for e in ventana.elementos
                        ),
                    )
                    for ventana in ventanas
                ),
            )

    def leer_output(self, elemento_ui: ElementoUI) -> str | None:
        with self._lock:
            return self.valor_output

    def escribir_output(self, elemento_ui: ElementoUI, mecanismo: MecanismoDeEscritura, valor: str) -> str:
        with self._lock:
            self.escrituras.append((mecanismo, valor))
            if mecanismo is not MecanismoDeEscritura.LEGACY_IACCESSIBLE:
                # Alpha-209: ValuePattern.SetValue → COMError (medido 4/4).
                return "COMError -0x7feceaf7"
            if self.hresult_escritura != "S_OK":
                return self.hresult_escritura
            if not self.escritura_no_persiste:
                self.valor_output = valor
            return "S_OK"

    def invocar(self, elemento_ui: ElementoUI) -> str:
        with self._lock:
            nombre = elemento_ui.nombre
            self.invocaciones.append(nombre)
            if nombre in self.excepcion_en_invoke:
                raise RuntimeError(f"crash simulado al invocar {nombre}")
            hresult = self.hresult_invoke.get(nombre, "S_OK")
            if hresult != "S_OK":
                return hresult
            if nombre == "Ignore" and self.estado == "modal":
                self.estado = "modal" if self.modal_reaparece else "asistente"
            elif nombre == "Advanced >>>" and self.estado == "asistente":
                self.estado = "avanzado"
                if self.output_tras_advanced is not None:
                    self.valor_output = self.output_tras_advanced
            elif nombre in {"Start", "OK"} and self.estado in {"asistente", "avanzado"}:
                self.estado = "generando"
                if self.raiz_output is not None and not self.inicio_sin_progreso:
                    self.raiz_output.mkdir(parents=True, exist_ok=True)
                    (self.raiz_output / "progreso.dds").write_bytes(b"x")
                if self.ruta_log is not None and not self.inicio_sin_progreso:
                    self.ruta_log.parent.mkdir(parents=True, exist_ok=True)
                    with self.ruta_log.open("ab") as log:
                        log.write(b"[00:01] generando\n")
                if self.ruta_preset is not None and self.ruta_preset.exists():
                    # P0-B: la herramienta reescribe su preset al iniciar (OutputPath).
                    contenido = self.ruta_preset.read_bytes()
                    self.ruta_preset.write_bytes(contenido.replace(b"PR2 Work", b"Escrito Por La Herramienta"))
            elif (
                self.estado == "terminal"
                and nombre == self.contrato.accion_terminal.selector.nombre
                and not self.no_sale_tras_terminal
            ):
                self.vivo = False
                self.estado = "salido"
            return "S_OK"

    def liberar(self) -> None:
        return None

    # -- conveniencia -----------------------------------------------------------

    def proceso_vivo(self) -> bool:
        with self._lock:
            return self.vivo
