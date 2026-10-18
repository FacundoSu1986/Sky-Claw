"""Contratos de UI por build para la etapa 9 DESATENDIDA (#661 P2/P4).

**La pregunta que responde, entera.** *"Para ESTE binario (identificado por su
SHA-256) en ESTE modo de juego, ¿qué controles exactos puede tocar Sky-Claw,
con qué mecanismo, en qué orden y qué diálogos sabe reconocer?"* Si la respuesta
no está en :data:`CONTRATOS_DE_UI`, no hay modo desatendido: la corrida falla
cerrada con ``UNATTENDED_BUILD_UNSUPPORTED`` antes de spawnear.

**La clave es ``(tool, exe_sha256)`` y nada más débil.** Ni el nombre del
ejecutable ni la versión textual identifican un build: dos Alpha distintas
pueden compartir ``FileVersion`` y un ``DynDOLODx64.exe`` renombrado sigue
siendo cualquier cosa. El hash lo calcula el runner sobre el archivo que va a
lanzar, inmediatamente antes del spawn.

**Todo lo que hay acá es MEDIDO, no inferido.** La fuente es la evidencia de rig
P0-A/P0-B sobre Alpha-209 (``docs/validation/2026-10-02_p0_uia_alpha209/``):

* el campo Output es el único ``Edit``/``TEdit`` de la ventana ``TfrmMain`` (el
  mismo selector que usa el gate read-only, :data:`SELECTORES_DE_OUTPUT`);
* ``ValuePattern.SetValue`` sobre ese ``TEdit`` falla con ``COMError
  -2146233079`` 4/4 y ``LegacyIAccessiblePattern.SetValue`` escribe con readback
  exacto: el mecanismo del contrato es UNO y no hay fallback en runtime (P4);
* los botones de la ventana principal son ``TButton`` cuyo ``AutomationId`` es el
  HWND (por instancia): el selector usa tipo + clase + nombre, nunca el id;
* los diálogos son *task dialogs* ``#32770`` con la instrucción en el elemento
  ``MainInstruction`` y botones ``CCPushButton`` con ``CommandButton_N`` estable
  entre corridas: ahí el ``AutomationId`` SÍ entra al selector;
* ``Advanced >>>`` de DynDOLOD recarga el Output desde el preset
  (``ADVANCED_RELOADED_OUTPUT_FROM_PRESET = YES``): la acción previa obliga a
  reconvergir antes del Begin.

**Qué NO es este módulo.** No conoce COM, procesos ni el runner: son datos
inmutables y dos funciones puras. La decisión de qué hacer con una pantalla
observada vive en ``dyndolod_uia_driver``; el mecanismo COM, en
``dyndolod_uia_driver_windows``.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sky_claw.local.tools.dyndolod_uia_preflight import (
    SELECTORES_DE_OUTPUT,
    TOOLS_OBSERVABLES,
    CriteriosDeControl,
)


class ModoDeInteraccion(enum.Enum):
    """Cómo se opera la GUI de TexGen/DynDOLOD en ESTA corrida. Explícito siempre.

    ``ASISTIDO`` es el protocolo T5-v2 (gate read-only + confirmación humana en
    la PC); ``DESATENDIDO`` es el driver mutante acotado por contrato de build.
    No hay fallback entre los dos: un desatendido que no puede correr falla
    cerrado y el operador decide si lanza otra corrida asistida.
    """

    ASISTIDO = "asistido"
    DESATENDIDO = "desatendido"


def modo_de_interaccion_desde(valor: object) -> ModoDeInteraccion:
    """Parsea el modo desde la frontera (payload de tool, config). Fail-closed.

    Sólo acepta el enum o su valor exacto. Un alias (``"unattended"``), otra
    capitalización o un bool NO se interpretan: adivinar ``DESATENDIDO`` sería
    mutar una GUI que nadie pidió mutar.
    """
    if isinstance(valor, ModoDeInteraccion):
        return valor
    if isinstance(valor, str):
        for modo in ModoDeInteraccion:
            if modo.value == valor:
                return modo
    raise ValueError(
        f"modo de interacción desconocido: {valor!r}; valores admitidos: {[modo.value for modo in ModoDeInteraccion]}"
    )


class CodigoDesatendido(enum.Enum):
    """Por qué se cortó una corrida desatendida. Machine-readable y estable.

    El nombre del miembro es igual a su valor (en inglés, como los nombra #661)
    para que un log estructurado, un test y el operador por Telegram usen el
    mismo identificador sin traducción. Congelado por igualdad literal.
    """

    UNATTENDED_CAPABILITY_UNAVAILABLE = "UNATTENDED_CAPABILITY_UNAVAILABLE"
    UNATTENDED_BUILD_UNSUPPORTED = "UNATTENDED_BUILD_UNSUPPORTED"
    UNATTENDED_CONFIGURATION_NOT_APPROVED = "UNATTENDED_CONFIGURATION_NOT_APPROVED"
    PRESET_ABSENT = "PRESET_ABSENT"
    PRESET_MISMATCH = "PRESET_MISMATCH"
    PRESET_RESTORE_FAILED = "PRESET_RESTORE_FAILED"
    UNEXPECTED_MODAL = "UNEXPECTED_MODAL"
    KNOWN_MODAL_NOT_APPROVED = "KNOWN_MODAL_NOT_APPROVED"
    UIA_UNAVAILABLE = "UIA_UNAVAILABLE"
    UIA_CONTROL_MAP_MISMATCH = "UIA_CONTROL_MAP_MISMATCH"
    OUTPUT_WRITE_UNSUPPORTED = "OUTPUT_WRITE_UNSUPPORTED"
    OUTPUT_READBACK_MISMATCH = "OUTPUT_READBACK_MISMATCH"
    BEGIN_NOT_ENABLED = "BEGIN_NOT_ENABLED"
    BEGIN_OUTCOME_UNKNOWN = "BEGIN_OUTCOME_UNKNOWN"
    BEGIN_NOT_CONFIRMED = "BEGIN_NOT_CONFIRMED"
    PROCESS_DIED_PRE_BEGIN = "PROCESS_DIED_PRE_BEGIN"
    UNEXPECTED_DIALOG_DURING_GENERATION = "UNEXPECTED_DIALOG_DURING_GENERATION"
    TERMINAL_CONTROL_MISMATCH = "TERMINAL_CONTROL_MISMATCH"
    TERMINAL_OUTCOME_UNKNOWN = "TERMINAL_OUTCOME_UNKNOWN"
    UNATTENDED_CONTRACT_VIOLATION = "UNATTENDED_CONTRACT_VIOLATION"


class MecanismoDeEscritura(enum.Enum):
    """El ÚNICO mecanismo con el que un build acepta escribir su campo Output."""

    LEGACY_IACCESSIBLE = "LEGACY_IACCESSIBLE"
    VALUE_PATTERN = "VALUE_PATTERN"


class ContextoDeAccion(enum.Enum):
    """Dónde vive el control de una acción: la ventana contractual o un diálogo.

    Separa los dos universos que el árbol UIA aplana: un ``OK`` de la ventana
    principal (Begin de DynDOLOD) y un ``OK`` de un ``#32770`` no son el mismo
    control aunque compartan nombre.
    """

    VENTANA_PRINCIPAL = "VENTANA_PRINCIPAL"
    DIALOGO = "DIALOGO"


@dataclass(frozen=True, slots=True)
class AccionDeUI:
    """Una acción que el driver puede invocar EXACTAMENTE una vez por corrida."""

    id: str
    selector: CriteriosDeControl
    contexto: ContextoDeAccion

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("una acción de UI necesita un id no vacío")
        if self.selector.esta_vacio():
            raise ValueError(f"la acción {self.id!r} no puede tener un selector vacío")


@dataclass(frozen=True, slots=True)
class FirmaDeDialogo:
    """Identidad de un *task dialog*: instrucción EXACTA + botones requeridos.

    La instrucción es lo que desambigua dos diálogos con botones compartidos: el
    modal de arranque de TexGen y su diálogo terminal tienen ambos ``Exit
    TexGen``. Los botones requeridos son ``(nombre, AutomationId)``; botones de
    más (``Cerrar`` de la barra, scroll) no invalidan la firma, botones de menos
    sí.
    """

    instruccion: str
    botones: frozenset[tuple[str, str]]

    def __post_init__(self) -> None:
        if not self.instruccion.strip():
            raise ValueError("una firma de diálogo necesita la instrucción exacta")
        if not self.botones:
            raise ValueError("una firma de diálogo necesita al menos un botón requerido")

    def coincide(self, instruccion: str | None, botones: Iterable[tuple[str, str]]) -> bool:
        """Comparación exacta: sin substring, sin mayúsculas, sin trim."""
        return instruccion == self.instruccion and self.botones <= set(botones)


class PoliticaDeModal(enum.Enum):
    """Qué hace el driver ante un modal de arranque CONOCIDO.

    Hay una sola política y es deliberado: el rig registró el ``Ignore`` como
    ``interaction_source = HUMAN, automation_policy = NOT_AUTHORIZED``. El
    driver no lo decide: pregunta al operador por el canal remoto y sólo con su
    aprobación invoca el botón exacto, una vez.
    """

    APROBACION_DEL_OPERADOR = "APROBACION_DEL_OPERADOR"


@dataclass(frozen=True, slots=True)
class ModalConocido:
    """Un modal de arranque medido en rig, con su botón de continuar exacto."""

    id: str
    firma: FirmaDeDialogo
    accion_continuar: AccionDeUI
    politica: PoliticaDeModal
    descripcion: str

    def __post_init__(self) -> None:
        if self.accion_continuar.contexto is not ContextoDeAccion.DIALOGO:
            raise ValueError(f"el botón de continuar del modal {self.id!r} vive en el diálogo")
        par = (self.accion_continuar.selector.nombre, self.accion_continuar.selector.automation_id)
        if par not in self.firma.botones:
            raise ValueError(f"el botón de continuar del modal {self.id!r} no pertenece a su firma")


#: Marcas estructurales del *task dialog* ``#32770`` medidas en Alpha-209
#: (P0-A, ronda 2 de TexGen; P0-B en las dos herramientas). Las comparten las
#: dos herramientas porque es el diálogo del sistema, no un control VCL.
MARCAS_DEL_DIALOGO_DE_TAREA: Mapping[str, str] = MappingProxyType(
    {
        "tipo_de_control": "Window",
        "class_name": "#32770",
        "automation_id_instruccion": "MainInstruction",
        "class_name_boton": "CCPushButton",
    }
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ContratoDeUIHerramienta:
    """Todo lo que el driver puede hacer sobre UN build, y nada más.

    ``preset_relativo`` es la ruta (relativa al directorio del ejecutable) del
    preset que la herramienta carga al abrir y reescribe al iniciar; el driver
    lo verifica y lo restaura, nunca lo edita. ``seccion_preset`` y
    ``clave_preset_de_calidad`` dicen dónde leer el preset de calidad
    (``Preset=Medium``) que la corrida tiene que pedir explícitamente.
    """

    tool: str
    exe_sha256: str
    version: str
    modo_de_juego: str
    clase_ventana_principal: str
    selector_output: CriteriosDeControl
    mecanismo_output: MecanismoDeEscritura
    accion_previa: AccionDeUI | None
    accion_inicio: AccionDeUI
    dialogo_terminal: FirmaDeDialogo
    accion_terminal: AccionDeUI
    modales_conocidos: tuple[ModalConocido, ...]
    preset_relativo: tuple[str, ...]
    seccion_preset: str
    clave_preset_de_calidad: str | None
    evidencia: str

    def __post_init__(self) -> None:
        if self.tool not in TOOLS_OBSERVABLES:
            raise ValueError(f"tool fuera del contrato UIA: {self.tool!r}")
        if not _SHA256_HEX.match(self.exe_sha256):
            raise ValueError(f"exe_sha256 tiene que ser SHA-256 hex en minúsculas; llegó {self.exe_sha256!r}")
        if self.selector_output.esta_vacio():
            raise ValueError("el selector de Output no puede estar vacío")
        if self.accion_inicio.contexto is not ContextoDeAccion.VENTANA_PRINCIPAL:
            raise ValueError("el Begin vive en la ventana contractual, nunca en un diálogo")
        if self.accion_previa is not None and self.accion_previa.contexto is not ContextoDeAccion.VENTANA_PRINCIPAL:
            raise ValueError("la acción previa vive en la ventana contractual")
        if self.accion_terminal.contexto is not ContextoDeAccion.DIALOGO:
            raise ValueError("la acción terminal vive en el diálogo terminal")
        par_terminal = (self.accion_terminal.selector.nombre, self.accion_terminal.selector.automation_id)
        if par_terminal not in self.dialogo_terminal.botones:
            raise ValueError("la acción terminal tiene que ser un botón de la firma terminal")
        if not self.preset_relativo or any(not parte or parte in {".", ".."} for parte in self.preset_relativo):
            raise ValueError(f"preset_relativo inválido: {self.preset_relativo!r}")
        ids = [self.accion_inicio.id, self.accion_terminal.id]
        if self.accion_previa is not None:
            ids.append(self.accion_previa.id)
        ids.extend(modal.accion_continuar.id for modal in self.modales_conocidos)
        if len(ids) != len(set(ids)):
            raise ValueError(f"ids de acción repetidos en el contrato de {self.tool}: {ids}")

    def todas_las_acciones(self) -> tuple[AccionDeUI, ...]:
        """Las acciones invocables de este contrato, enumeradas (para el ledger y el helper)."""
        acciones = [self.accion_inicio, self.accion_terminal]
        if self.accion_previa is not None:
            acciones.append(self.accion_previa)
        acciones.extend(modal.accion_continuar for modal in self.modales_conocidos)
        return tuple(acciones)

    def accion_por_id(self, accion_id: str) -> AccionDeUI | None:
        """La acción con ese id, o ``None``: el helper nunca invoca algo fuera del contrato."""
        for accion in self.todas_las_acciones():
            if accion.id == accion_id:
                return accion
        return None


_EVIDENCIA_P0B = "docs/validation/2026-10-02_p0_uia_alpha209/p0b/final-report-p0b.md"

_BOTON_TAREA = MARCAS_DEL_DIALOGO_DE_TAREA["class_name_boton"]

_CONTRATO_TEXGEN_ALPHA209 = ContratoDeUIHerramienta(
    tool="TexGen",
    exe_sha256="0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62",
    version="3.0.0.209",
    modo_de_juego="SSE",
    clase_ventana_principal="TfrmMain",
    selector_output=SELECTORES_DE_OUTPUT["TexGen"],
    mecanismo_output=MecanismoDeEscritura.LEGACY_IACCESSIBLE,
    accion_previa=None,
    accion_inicio=AccionDeUI(
        id="texgen_start",
        selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="Start"),
        contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
    ),
    dialogo_terminal=FirmaDeDialogo(
        instruccion="Exit TexGen, zip and exit, check log or restart?",
        botones=frozenset(
            {
                ("Exit TexGen", "CommandButton_7"),
                ("Zip and Exit", "CommandButton_11"),
                ("Check log", "CommandButton_2"),
                ("Restart", "CommandButton_4"),
            }
        ),
    ),
    accion_terminal=AccionDeUI(
        id="texgen_exit",
        selector=CriteriosDeControl(
            tipo_de_control="Button",
            class_name=_BOTON_TAREA,
            nombre="Exit TexGen",
            automation_id="CommandButton_7",
        ),
        contexto=ContextoDeAccion.DIALOGO,
    ),
    modales_conocidos=(
        ModalConocido(
            id="texgen_stitched_lod_previo",
            firma=FirmaDeDialogo(
                instruccion=(
                    "Found stitched object LOD textures from earlier TexGen generation installed in game folder."
                ),
                botones=frozenset({("Ignore", "CommandButton_5"), ("Exit TexGen", "CommandButton_3")}),
            ),
            accion_continuar=AccionDeUI(
                id="texgen_modal_ignore",
                selector=CriteriosDeControl(
                    tipo_de_control="Button",
                    class_name=_BOTON_TAREA,
                    nombre="Ignore",
                    automation_id="CommandButton_5",
                ),
                contexto=ContextoDeAccion.DIALOGO,
            ),
            politica=PoliticaDeModal.APROBACION_DEL_OPERADOR,
            descripcion=(
                "TexGen encontró texturas stitched de una generación anterior instaladas en la carpeta del "
                "juego. Lo recomendado por DynDOLOD es desinstalar el output viejo antes de regenerar; "
                "'Ignore' continúa igual (dyndolod.info/Messages/TexGen-Output)."
            ),
        ),
    ),
    preset_relativo=("Edit Scripts", "DynDOLOD", "Presets", "DynDOLOD_SSE_TexGen.ini"),
    seccion_preset="TexGen",
    clave_preset_de_calidad=None,
    evidencia=_EVIDENCIA_P0B,
)

_CONTRATO_DYNDOLOD_ALPHA209 = ContratoDeUIHerramienta(
    tool="DynDOLOD",
    exe_sha256="b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0",
    version="3.0.0.209",
    modo_de_juego="SSE",
    clase_ventana_principal="TfrmMain",
    selector_output=SELECTORES_DE_OUTPUT["DynDOLOD"],
    mecanismo_output=MecanismoDeEscritura.LEGACY_IACCESSIBLE,
    accion_previa=AccionDeUI(
        id="dyndolod_advanced",
        selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="Advanced >>>"),
        contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
    ),
    accion_inicio=AccionDeUI(
        id="dyndolod_begin_ok",
        selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="OK"),
        contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
    ),
    dialogo_terminal=FirmaDeDialogo(
        instruccion="Save DynDOLOD plugins, save plugins and zip output, exit DynDOLOD without saving, check the log?",
        botones=frozenset(
            {
                ("Save and Exit", "CommandButton_6"),
                ("Save, Zip and Exit", "CommandButton_11"),
                ("Exit DynDOLOD", "CommandButton_7"),
                ("Check log", "CommandButton_2"),
            }
        ),
    ),
    accion_terminal=AccionDeUI(
        id="dyndolod_save_exit",
        selector=CriteriosDeControl(
            tipo_de_control="Button",
            class_name=_BOTON_TAREA,
            nombre="Save and Exit",
            automation_id="CommandButton_6",
        ),
        contexto=ContextoDeAccion.DIALOGO,
    ),
    modales_conocidos=(
        ModalConocido(
            id="dyndolod_dll_ng_ausente",
            firma=FirmaDeDialogo(
                instruccion="DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!",
                botones=frozenset({("Ignore", "CommandButton_5"), ("Exit DynDOLOD", "CommandButton_3")}),
            ),
            accion_continuar=AccionDeUI(
                id="dyndolod_modal_ignore",
                selector=CriteriosDeControl(
                    tipo_de_control="Button",
                    class_name=_BOTON_TAREA,
                    nombre="Ignore",
                    automation_id="CommandButton_5",
                ),
                contexto=ContextoDeAccion.DIALOGO,
            ),
            politica=PoliticaDeModal.APROBACION_DEL_OPERADOR,
            descripcion=(
                "DynDOLOD no encontró DynDOLOD.DLL de 'DynDOLOD DLL NG and Scripts'. Sin ese plugin SKSE el "
                "LOD dinámico no funciona en juego (SOP §2.9: requerido para 1.6.1170); 'Ignore' genera igual."
            ),
        ),
    ),
    preset_relativo=("Edit Scripts", "DynDOLOD", "Presets", "DynDOLOD_SSE_Default.ini"),
    seccion_preset="DynDOLOD",
    clave_preset_de_calidad="Preset",
    evidencia=_EVIDENCIA_P0B,
)

#: El registro completo de builds con contrato desatendido. Sólo builds
#: MEDIDOS en rig real entran acá; congelado por igualdad literal en
#: ``tests/test_dyndolod_uia_contrato.py``. Inmutable en runtime.
CONTRATOS_DE_UI: Mapping[tuple[str, str], ContratoDeUIHerramienta] = MappingProxyType(
    {
        (contrato.tool, contrato.exe_sha256): contrato
        for contrato in (_CONTRATO_TEXGEN_ALPHA209, _CONTRATO_DYNDOLOD_ALPHA209)
    }
)


def resolver_contrato(tool: str, exe_sha256: str, modo_de_juego: str) -> ContratoDeUIHerramienta | None:
    """El contrato del par ``(tool, sha256)`` en ese modo de juego, o ``None``.

    El hash se compara en minúsculas (``hashlib`` las emite así, Windows
    ``Get-FileHash`` en mayúsculas); cualquier otro desvío —hash malformado,
    tool ajena, modo no medido— es ``None``: fail-closed por omisión.
    """
    normalizado = exe_sha256.strip().lower()
    if not _SHA256_HEX.match(normalizado):
        return None
    contrato = CONTRATOS_DE_UI.get((tool, normalizado))
    if contrato is None or contrato.modo_de_juego != modo_de_juego:
        return None
    return contrato
