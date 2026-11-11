"""Mide la UI de un build de TexGen/DynDOLOD en ejecución y diagnostica qué parte del contrato congelado sigue valiendo.

**Para qué (H-8 de la auditoría de la etapa 9).** El contrato de UI (``dyndolod_uia_contrato.CONTRATOS_DE_UI``) solo
tiene builds MEDIDOS en un rig real (hoy, Alpha-209 SSE). Cualquier actualización de la herramienta, o un rig VR, cae
en ``UNATTENDED_BUILD_UNSUPPORTED`` y no había con qué medir el build nuevo ni un procedimiento para darlo de alta.
Este script es esa herramienta. El procedimiento completo está en ``docs/operations/dyndolod_alta_de_build_uia.md``.

**Es de SOLO LECTURA, y eso es el contrato, no una promesa.** Observa por el puerto ``ObservadorUIA`` —sus métodos
son de lectura— y jamás importa al driver mutante (``dyndolod_uia_driver*``). No enfoca ventanas, no pulsa botones, no
escribe en los controles ni en el directorio de la herramienta. Lo único que escribe es el informe que el operador
pide con ``--salida``, y se rechaza si cae dentro del directorio del ejecutable. Un ancla por AST lo congela
(``tests/test_dyndolod_uia_driver_superficie.py``).

**Qué produce.** Dos cosas distintas y separadas a propósito:

* una *medición*: HECHOS de la UI del proceso (ventanas y controles con tipo, clase, nombre y ``AutomationId``) más el
  SHA-256 del ejecutable, calculado del archivo y no declarado por el operador;
* un *diagnóstico* contra un contrato congelado: por cada pieza del contrato, si en el build medido ``COINCIDE``,
  ``DIFIERE`` o está ``NO_OBSERVADO``. **«Compatible» exige haberlo observado todo**: sin las instantáneas del diálogo
  terminal y de los modales el veredicto es ``incompleto``, nunca un «sí» por omisión.

**Lo que NO hace, y por qué.** No emite un contrato listo para pegar. Hay campos que ningún dato medido define: el
mecanismo de escritura del Output, la política de cada modal (¿quién aprueba continuar?) y la evidencia. Esos los decide
una persona con el informe del rig en la mano, en un PR que además actualiza el congelamiento por igualdad literal de
``tests/test_dyndolod_uia_contrato.py``. Medir no autoriza nada.

**Límites que conviene saber.**

* Se probó con una GUI SIMULADA derivada del propio contrato congelado. Que la GUI real de Alpha-209 produzca el mismo
  resultado es el smoke del rig, y no está medido.
* El texto de un diálogo se busca como el nombre de algún control o como el título de la ventana; es un criterio de
  este script, no algo que se haya medido en Windows real.
* Un diálogo que no coincide queda ``NO_OBSERVADO`` con lo que se vio, no ``DIFIERE``: no se puede distinguir «cambió»
  de «no se capturó ese momento».
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import ntpath
import pathlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

from sky_claw.local.tools.dyndolod_uia_contrato import CONTRATOS_DE_UI, ContratoDeUIHerramienta
from sky_claw.local.tools.dyndolod_uia_preflight import (
    TOOLS_OBSERVABLES,
    ControlObservado,
    CriteriosDeControl,
    EnumeracionIncompletaError,
    LocalizadorDeProcesos,
    LocalizadorPsutil,
    ObservacionUIAError,
    ObservadorNoDisponible,
    ObservadorUIA,
    ProcesoObservado,
)
from sky_claw.local.tools.dyndolod_uia_windows import construir_observador_windows

#: Versión del esquema del informe. Un informe de otra versión no se interpreta: se rechaza.
ESQUEMA = 1

#: Código de salida cuando se pidió ``--exigir-compatible`` y el veredicto no lo es.
SALIDA_NO_COMPATIBLE = 3
SALIDA_ERROR = 2


class MedicionError(RuntimeError):
    """La medición o el diagnóstico no se pueden hacer con garantías. Nunca devuelve un resultado a medias."""


# ---------------------------------------------------------------------------
# Tipos de la medición (serializables)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class ControlMedido:
    """Un control, con las cuatro propiedades que el contrato sabe usar como criterio.

    ``order=True`` porque las ventanas se ordenan por su contenido (tupla de controles): sin orden, dos ventanas con la
    misma clase y título harían fallar la comparación en lugar de salir en un orden determinista.
    """

    tipo_de_control: str
    class_name: str
    nombre: str
    automation_id: str


@dataclass(frozen=True, slots=True)
class VentanaMedida:
    titulo: str
    class_name: str
    controles: tuple[ControlMedido, ...]


@dataclass(frozen=True, slots=True)
class Medicion:
    """Hechos de UN momento de la UI de UN build. ``etiqueta`` dice qué momento era (reposo, diálogo terminal…)."""

    tool: str
    exe_sha256: str
    modo_de_juego: str
    etiqueta: str
    ventanas: tuple[VentanaMedida, ...]


class Estado(enum.Enum):
    COINCIDE = "coincide"
    DIFIERE = "difiere"
    NO_OBSERVADO = "no_observado"


class Veredicto(enum.Enum):
    COMPATIBLE = "compatible"
    DIFIERE = "difiere"
    INCOMPLETO = "incompleto"


@dataclass(frozen=True, slots=True)
class Chequeo:
    nombre: str
    estado: Estado
    detalle: str


@dataclass(frozen=True, slots=True)
class Diagnostico:
    veredicto: Veredicto
    chequeos: tuple[Chequeo, ...]

    def resumen(self) -> str:
        lineas = [f"veredicto: {self.veredicto.value}"]
        lineas.extend(f"  [{c.estado.value}] {c.nombre}: {c.detalle}" for c in self.chequeos)
        return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Medición
# ---------------------------------------------------------------------------


def _sha256_del_archivo(ruta: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with ruta.open("rb") as archivo:
            for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
                digest.update(bloque)
    except OSError as exc:
        raise MedicionError(f"no se pudo leer el ejecutable {ruta}: {exc}") from exc
    return digest.hexdigest()


def _normalizada(ruta: str) -> str:
    """Clave de comparación de rutas Windows: separadores y mayúsculas no distinguen."""
    return ntpath.normcase(ntpath.normpath(ruta))


def _proceso_del_ejecutable(localizador: LocalizadorDeProcesos, exe: pathlib.Path) -> ProcesoObservado:
    """El ÚNICO proceso vivo cuyo ejecutable es exactamente ``exe``; si no hay uno solo, no se mide.

    La identidad es por RUTA completa. Un proceso con la ruta ilegible se descarta: adivinarlo por nombre de binario
    mediría la UI de otra instalación y el informe diría que es la de este build.
    """
    aceptables = {_normalizada(str(exe)), _normalizada(str(exe.resolve()))}
    try:
        procesos = localizador.procesos()
    except ObservacionUIAError as exc:
        raise MedicionError(f"no se pudo enumerar los procesos: {exc}") from exc
    candidatos = [
        p
        for p in procesos
        if p.pid > 0 and p.ruta_ejecutable is not None and _normalizada(p.ruta_ejecutable) in aceptables
    ]
    if not candidatos:
        raise MedicionError(
            f"no hay un proceso en ejecución con esa ruta exacta: {exe}. Abrí la herramienta desde ese ejecutable."
        )
    if len(candidatos) > 1:
        pids = ", ".join(str(p.pid) for p in candidatos)
        raise MedicionError(f"hay más de una instancia de {exe} (pids {pids}): cerrá las que sobran y repetí")
    return candidatos[0]


def _medir_control(control: ControlObservado) -> ControlMedido:
    return ControlMedido(
        tipo_de_control=control.tipo_de_control,
        class_name=control.class_name,
        nombre=control.nombre,
        automation_id=control.automation_id,
    )


def medir(
    *,
    exe: pathlib.Path,
    tool: str,
    modo_de_juego: str,
    etiqueta: str,
    observador: ObservadorUIA,
    localizador: LocalizadorDeProcesos,
) -> Medicion:
    """Mide la UI del proceso de ``exe`` y devuelve hechos ordenados de forma determinista.

    Solo se llaman los dos métodos de lectura del puerto que enumeran ventanas y controles. Se revalida el ``pid`` de
    cada ventana y de cada control contra el proceso resuelto: un adaptador que devolviera de más no puede ampliar el
    alcance de lo que el informe dice sobre ESTE build.
    """
    if tool not in TOOLS_OBSERVABLES:
        raise MedicionError(f"tool fuera del contrato UIA: {tool!r} (válidas: {sorted(TOOLS_OBSERVABLES)})")
    sha = _sha256_del_archivo(exe)
    proceso = _proceso_del_ejecutable(localizador, exe)
    try:
        ventanas_crudas = [v for v in observador.ventanas_de_proceso(proceso.pid) if v.pid == proceso.pid]
        medidas = []
        for ventana in ventanas_crudas:
            controles = [c for c in observador.controles_de_ventana(ventana) if c.pid == proceso.pid]
            medidas.append(
                VentanaMedida(
                    titulo=ventana.titulo,
                    class_name=ventana.class_name,
                    controles=tuple(
                        sorted(
                            (_medir_control(c) for c in controles),
                            key=lambda m: (m.tipo_de_control, m.class_name, m.nombre, m.automation_id),
                        )
                    ),
                )
            )
    except EnumeracionIncompletaError as exc:
        raise MedicionError(f"enumeración incompleta (no se informa una medición parcial): {exc}") from exc
    except ObservacionUIAError as exc:
        raise MedicionError(f"no se pudo observar la UI: {exc}") from exc
    ordenadas = sorted(medidas, key=lambda v: (v.class_name, v.titulo, v.controles))
    return Medicion(
        tool=tool, exe_sha256=sha, modo_de_juego=modo_de_juego, etiqueta=etiqueta, ventanas=tuple(ordenadas)
    )


def fusionar(mediciones: Sequence[Medicion]) -> Medicion:
    """Une las ventanas de varios momentos (reposo, modal de arranque, diálogo terminal) de UN mismo build."""
    if not mediciones:
        raise MedicionError("no hay mediciones que fusionar")
    base = mediciones[0]
    for otra in mediciones[1:]:
        if (otra.tool, otra.exe_sha256, otra.modo_de_juego) != (base.tool, base.exe_sha256, base.modo_de_juego):
            raise MedicionError(
                "las mediciones son de un distinto build: "
                f"{(base.tool, base.exe_sha256[:12], base.modo_de_juego)} vs "
                f"{(otra.tool, otra.exe_sha256[:12], otra.modo_de_juego)}"
            )
    vistas: list[VentanaMedida] = []
    for medicion in mediciones:
        for ventana in medicion.ventanas:
            if ventana not in vistas:
                vistas.append(ventana)
    ordenadas = sorted(vistas, key=lambda v: (v.class_name, v.titulo, v.controles))
    return Medicion(
        tool=base.tool,
        exe_sha256=base.exe_sha256,
        modo_de_juego=base.modo_de_juego,
        etiqueta="+".join(m.etiqueta for m in mediciones),
        ventanas=tuple(ordenadas),
    )


# ---------------------------------------------------------------------------
# Diagnóstico contra un contrato congelado
# ---------------------------------------------------------------------------


def _como_observado(control: ControlMedido) -> ControlObservado:
    # ``pid`` no participa en ``coincide``: el selector del contrato mira solo las cuatro propiedades.
    return ControlObservado(
        pid=0,
        automation_id=control.automation_id,
        nombre=control.nombre,
        tipo_de_control=control.tipo_de_control,
        class_name=control.class_name,
    )


def _cuantos_resuelven(selector: CriteriosDeControl, ventana: VentanaMedida) -> int:
    return sum(1 for c in ventana.controles if selector.coincide(_como_observado(c)))


def _chequeo_de_selector(nombre: str, selector: CriteriosDeControl, principal: VentanaMedida | None) -> Chequeo:
    if principal is None:
        return Chequeo(nombre, Estado.NO_OBSERVADO, "sin ventana principal contra la que resolver el selector")
    n = _cuantos_resuelven(selector, principal)
    if n == 1:
        return Chequeo(nombre, Estado.COINCIDE, f"el selector resuelve a un único control ({selector.describir()})")
    if n == 0:
        return Chequeo(nombre, Estado.DIFIERE, f"el selector no resuelve a ningún control ({selector.describir()})")
    return Chequeo(nombre, Estado.DIFIERE, f"el selector es ambiguo: resuelve a {n} controles ({selector.describir()})")


def _botones(ventana: VentanaMedida) -> set[tuple[str, str]]:
    return {(c.nombre, c.automation_id) for c in ventana.controles if c.tipo_de_control == "Button"}


def _tiene_el_texto(ventana: VentanaMedida, texto: str) -> bool:
    return ventana.titulo == texto or any(c.nombre == texto for c in ventana.controles)


def _chequeo_de_dialogo(
    nombre: str, instruccion: str, botones: frozenset[tuple[str, str]], otras: Iterable[VentanaMedida]
) -> Chequeo:
    vistas = list(otras)
    for ventana in vistas:
        if _tiene_el_texto(ventana, instruccion) and botones <= _botones(ventana):
            return Chequeo(nombre, Estado.COINCIDE, f"hay un diálogo con la instrucción {instruccion!r} y sus botones")
    if not vistas:
        return Chequeo(nombre, Estado.NO_OBSERVADO, "no hay otra ventana medida: falta la instantánea de este momento")
    clases = sorted({f"{v.class_name}/{v.titulo!r}" for v in vistas})
    return Chequeo(
        nombre,
        Estado.NO_OBSERVADO,
        f"ninguna de las otras ventanas medidas coincide ({', '.join(clases)}): puede ser otro momento o un cambio",
    )


def diagnosticar(
    medicion: Medicion, contrato: ContratoDeUIHerramienta, *, directorio_del_exe: pathlib.Path | None
) -> Diagnostico:
    """Para cada pieza de ``contrato``: ¿vale en el build medido?

    El veredicto es ``compatible`` SOLO si todas coinciden. Que algo no se haya observado no se rellena con un «sí».
    """
    chequeos: list[Chequeo] = []
    candidatas = [v for v in medicion.ventanas if v.class_name == contrato.clase_ventana_principal]
    principal: VentanaMedida | None = None
    if len(candidatas) == 1:
        principal = candidatas[0]
        chequeos.append(
            Chequeo(
                "ventana_principal", Estado.COINCIDE, f"hay una ventana de clase {contrato.clase_ventana_principal!r}"
            )
        )
    elif not candidatas:
        vistas = sorted({v.class_name for v in medicion.ventanas}) or ["(ninguna)"]
        chequeos.append(
            Chequeo(
                "ventana_principal",
                Estado.DIFIERE,
                f"ninguna ventana de clase {contrato.clase_ventana_principal!r}; clases vistas: {', '.join(vistas)}",
            )
        )
    else:
        chequeos.append(
            Chequeo(
                "ventana_principal",
                Estado.DIFIERE,
                f"hay {len(candidatas)} ventanas de clase {contrato.clase_ventana_principal!r}: no se sabe cuál es",
            )
        )
    chequeos.append(_chequeo_de_selector("selector_output", contrato.selector_output, principal))
    chequeos.append(_chequeo_de_selector("accion_inicio", contrato.accion_inicio.selector, principal))
    if contrato.accion_previa is not None:
        chequeos.append(_chequeo_de_selector("accion_previa", contrato.accion_previa.selector, principal))

    otras = [v for v in medicion.ventanas if v is not principal]
    terminal = contrato.dialogo_terminal
    chequeos.append(_chequeo_de_dialogo("dialogo_terminal", terminal.instruccion, terminal.botones, otras))
    for modal in contrato.modales_conocidos:
        chequeos.append(_chequeo_de_dialogo(f"modal:{modal.id}", modal.firma.instruccion, modal.firma.botones, otras))

    if directorio_del_exe is None:
        chequeos.append(Chequeo("preset", Estado.NO_OBSERVADO, "no se indicó el directorio del ejecutable (--exe)"))
    else:
        preset = directorio_del_exe.joinpath(*contrato.preset_relativo)
        if preset.is_file():
            chequeos.append(Chequeo("preset", Estado.COINCIDE, f"existe {'/'.join(contrato.preset_relativo)}"))
        else:
            chequeos.append(Chequeo("preset", Estado.DIFIERE, f"no existe {'/'.join(contrato.preset_relativo)}"))

    estados = {c.estado for c in chequeos}
    if Estado.DIFIERE in estados:
        veredicto = Veredicto.DIFIERE
    elif Estado.NO_OBSERVADO in estados:
        veredicto = Veredicto.INCOMPLETO
    else:
        veredicto = Veredicto.COMPATIBLE
    return Diagnostico(veredicto=veredicto, chequeos=tuple(chequeos))


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------


def a_json(medicion: Medicion) -> str:
    """El informe, estable (claves ordenadas) y versionado."""
    return json.dumps({"esquema": ESQUEMA, "medicion": asdict(medicion)}, ensure_ascii=False, indent=2, sort_keys=True)


def de_json(texto: str) -> Medicion:
    try:
        crudo = json.loads(texto)
        if crudo["esquema"] != ESQUEMA:
            raise MedicionError(f"informe de esquema {crudo['esquema']!r}; este script entiende el {ESQUEMA}")
        m = crudo["medicion"]
        return Medicion(
            tool=m["tool"],
            exe_sha256=m["exe_sha256"],
            modo_de_juego=m["modo_de_juego"],
            etiqueta=m["etiqueta"],
            ventanas=tuple(
                VentanaMedida(
                    titulo=v["titulo"],
                    class_name=v["class_name"],
                    controles=tuple(ControlMedido(**c) for c in v["controles"]),
                )
                for v in m["ventanas"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MedicionError(f"el informe no tiene la forma esperada: {exc!r}") from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _observador_real() -> ObservadorUIA:
    """El backend de Windows, o uno que falla cerrado diciendo por qué (fuera de Windows no hay UIA)."""
    try:
        return construir_observador_windows()
    except ObservacionUIAError as exc:
        return ObservadorNoDisponible(f"no hay backend de UI Automation en esta máquina: {exc}")


def _salida_valida(salida: pathlib.Path, exe: pathlib.Path) -> None:
    """El informe nunca se escribe dentro del directorio de la herramienta: es lo único que este script escribe."""
    if salida.resolve().is_relative_to(exe.resolve().parent):
        raise MedicionError(
            f"--salida cae dentro del directorio de la herramienta ({exe.resolve().parent}): elegí otro lugar"
        )


def _contrato_de_referencia(tool: str, sha: str) -> ContratoDeUIHerramienta:
    contrato = CONTRATOS_DE_UI.get((tool, sha.strip().lower()))
    if contrato is None:
        disponibles = sorted(s for t, s in CONTRATOS_DE_UI if t == tool)
        raise MedicionError(f"no hay un contrato congelado de {tool} con sha256 {sha!r}; hay: {', '.join(disponibles)}")
    return contrato


def _cmd_medir(args: argparse.Namespace) -> int:
    exe = pathlib.Path(args.exe)
    salida = pathlib.Path(args.salida) if args.salida else None
    if salida is not None:
        _salida_valida(salida, exe)
    medicion = medir(
        exe=exe,
        tool=args.tool,
        modo_de_juego=args.modo,
        etiqueta=args.etiqueta,
        observador=_observador_real(),
        localizador=LocalizadorPsutil(),
    )
    texto = a_json(medicion)
    if salida is None:
        print(texto)
    else:
        salida.write_text(texto + "\n", encoding="utf-8")
        print(f"medición ({args.etiqueta}) guardada en {salida}", file=sys.stderr)
    return 0


def _cmd_diagnosticar(args: argparse.Namespace) -> int:
    mediciones = [de_json(pathlib.Path(ruta).read_text(encoding="utf-8")) for ruta in args.informes]
    medicion = fusionar(mediciones)
    contrato = _contrato_de_referencia(medicion.tool, args.contra_sha)
    directorio = pathlib.Path(args.exe).parent if args.exe else None
    diagnostico = diagnosticar(medicion, contrato, directorio_del_exe=directorio)
    print(f"build medido: {medicion.tool} sha256={medicion.exe_sha256} modo={medicion.modo_de_juego}")
    print(f"contrato de referencia: {contrato.tool} {contrato.version} sha256={contrato.exe_sha256}")
    print(diagnostico.resumen())
    if args.exigir_compatible and diagnostico.veredicto is not Veredicto.COMPATIBLE:
        return SALIDA_NO_COMPATIBLE
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyndolod_medir_contrato_uia",
        description="Mide (solo lectura) la UI de un build de TexGen/DynDOLOD y la compara con el contrato congelado.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    medir_p = sub.add_parser(
        "medir", help="mide la UI del proceso en ejecución (requiere Windows y la herramienta abierta)"
    )
    medir_p.add_argument("--tool", required=True, choices=sorted(TOOLS_OBSERVABLES))
    medir_p.add_argument("--exe", required=True, help="ruta EXACTA del ejecutable en ejecución")
    medir_p.add_argument("--modo", required=True, help="modo de juego del build medido (SSE, TES5VR…)")
    medir_p.add_argument(
        "--etiqueta", default="reposo", help="qué momento se mide (reposo, modal-de-arranque, dialogo-terminal)"
    )
    medir_p.add_argument(
        "--salida", help="archivo del informe JSON (por defecto, stdout); nunca dentro del directorio de la herramienta"
    )
    diag_p = sub.add_parser(
        "diagnosticar", help="compara informes ya medidos con un contrato congelado (no necesita UIA)"
    )
    diag_p.add_argument("--contra-sha", required=True, help="sha256 del build CONGELADO con el que se compara")
    diag_p.add_argument("--exe", help="ejecutable del build medido: su directorio se usa para comprobar el preset")
    diag_p.add_argument(
        "--exigir-compatible",
        action="store_true",
        help=f"sale con {SALIDA_NO_COMPATIBLE} si el veredicto no es compatible",
    )
    diag_p.add_argument(
        "informes", nargs="+", help="informes JSON de las mediciones (reposo, modales, diálogo terminal)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.comando == "medir":
            return _cmd_medir(args)
        return _cmd_diagnosticar(args)
    except MedicionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":  # pragma: no cover - entrada de línea de comandos
    sys.exit(main())
