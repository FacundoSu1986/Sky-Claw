#!/usr/bin/env python
"""Sonda READ-ONLY del árbol de UI Automation de TexGen/DynDOLOD (T5A).

**Para qué existe.** ``sky_claw/local/tools/dyndolod_uia_preflight.py`` sabe
 decidir MATCH / MISMATCH / UNKNOWN y la medición del selector ya ocurrió (rig
 T5A, 2026-08-29: único ``Edit``/``TEdit`` con ``ValuePattern`` en las dos
 herramientas, ``AutomationId`` inestable). Esta sonda queda como herramienta
 de DIAGNÓSTICO sobre un rig Windows real: vuelca el árbol cuando aparezca una
 duda nueva (un build nuevo de las herramientas, un árbol ambiguo), y corre el
 preflight completo con el observador real:

    python local_scripts/scripts/probe_dyndolod_uia_readonly.py \\
        --tool TexGen --exe "C:/Modding/DynDOLOD/TexGenx64.exe"

    python local_scripts/scripts/probe_dyndolod_uia_readonly.py \\
        --tool TexGen --exe "C:/Modding/DynDOLOD/TexGenx64.exe" \\
        --control-type Edit \\
        --expected-output "C:/Games/Skyrim Special Edition/Sky-Claw/DynDOLOD"

**El adaptador COM NO vive acá: vive en el runtime.** Desde T5-v2 el backend es
``sky_claw/local/tools/dyndolod_uia_windows.py`` y este archivo lo IMPORTA — es
la misma implementación que midió el rig y la que el gate de producción usa,
así que no hay una copia de diagnóstico divergiendo de la productiva. Lo que
sí queda acá es lo que el paquete no quiere: el volcado saneado, el CLI y la
política de impresión para pegar evidencia en un PR.

**Es de SOLO LECTURA y eso está anclado, no prometido.** Sólo conecta, enumera y
 lee propiedades. No pulsa nada, no escribe presets, no cambia el Output, no
 inyecta teclado ni mouse, no enfoca ventanas. El ancla por AST de
 ``tests/test_dyndolod_uia_preflight.py`` cubre este archivo igual que al módulo
 productivo: no puede siquiera NOMBRAR una primitiva mutante de UIA/Win32, ni
 usar despacho dinámico para llegar a una.

**Estado de verificación:** el adaptador COM corrió en el rig T5A
 (2026-08-29, TexGen/DynDOLOD Alpha-209) y es la pieza que hoy alimenta el
 gate. La máquina de decisión tiene además su suite determinista multiplataforma.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from collections.abc import Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sky_claw.local.tools.dyndolod_uia_preflight import (  # noqa: E402
    TOOLS_OBSERVABLES,
    CriteriosDeControl,
    LocalizadorPsutil,
    ObservacionUIAError,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    observar_output,
)
from sky_claw.local.tools.dyndolod_uia_windows import (  # noqa: E402
    NOMBRES_DE_CONTROL_TYPE,
    ObservadorUIAWindows,
    describir_tolerando_fallos,
    primer_texto_no_vacio,
)

# Re-exportaciones deliberadas: tests y operadores históricos las leen desde
# ESTE módulo (es la superficie documentada de la sonda desde T5A), aunque la
# implementación viva en el runtime. Mantenerlas acá cuesta una línea y evita
# que existan dos nombres públicos para la misma pieza. `__all__` es lo que las
# hace re-exportaciones reconocibles (F401) y verbatim lo que el runtime expone.
__all__ = [
    "NOMBRES_DE_CONTROL_TYPE",
    "ObservadorUIAWindows",
    "describir_tolerando_fallos",
    "primer_texto_no_vacio",
]

# El adaptador COM (CLSID, mapa de ControlType, lectores de patrón, la clase
# `ObservadorUIAWindows` entera y sus helpers puros) NO se redefine acá: desde
# T5-v2 vive en `sky_claw/local/tools/dyndolod_uia_windows.py` y esta sonda la
# importa — la misma pieza que el gate de producción usa, sin una copia de
# diagnóstico divergente. El ancla por AST que lo exige es
# `test_la_sonda_no_redefine_el_backend_del_runtime`.


def _ES_PERFIL_UTIL(valor: str) -> bool:  # noqa: N802 -- se lee como constante en el punto de uso
    """``True`` si el perfil designa un directorio propio y no una raíz pelada.

    `` `/` ``, `` `\\` `` o `` `C:\\` `` como ``USERPROFILE``/``HOME`` no
    identifican a nadie, y redactarlos como prefijo destrozaría el volcado.
    """
    nucleo = valor.strip("\\/")
    return bool(nucleo) and ("\\" in nucleo or "/" in nucleo)


def _sanear(texto: str) -> str:
    """Redacta el perfil del usuario antes de imprimir, SIN romper el resto.

    Los títulos de ventana y los valores de las cajas de texto llevan rutas
    completas; este volcado se pega en un PR, así que redactar no es cosmético.

    **Pero sobre-redactar rompe justo aquello para lo que existe el volcado.**
    Reemplazar por substring convertía —con `USERNAME=Admin`— `Administración`
    en `<USERNAME>istración` y `badminton` en `b<USERNAME>ton`: el árbol que hay
    que LEER para elegir el selector T5A quedaba ilegible, y podía inducir
    criterios equivocados. Por eso se exige frontera:

    * ``USERPROFILE``/``HOME`` son rutas: se redactan como prefijo, sólo cuando
      lo que sigue es un separador o el fin de la cadena;
    * ``USERNAME`` es un nombre suelto: se redacta sólo como COMPONENTE completo
      de ruta, rodeado de separadores o extremos.

    La comparación ignora mayúsculas porque las rutas de Windows tampoco las
    distinguen: no redactar por diferencia de caso sería una fuga. El costo
    aceptado es que un ``USERNAME`` suelto en prosa (un título como "Admin
    tools") no se redacta — ahí no es una ruta, y romper el volcado por ese caso
    sale más caro que el dato.
    """
    separadores = r"\\/"
    resultado = texto
    for variable in ("USERPROFILE", "HOME"):
        valor = os.environ.get(variable)
        # Se normaliza el separador FINAL antes de armar el patrón: con
        # `USERPROFILE=C:\Users\op\` el lookahead exigía otro separador después
        # del valor —el suyo ya estaba adentro— así que una ruta que continúa no
        # matcheaba y el perfil salía crudo. Hallazgo de review (Qodo).
        # Whitespace Y separadores finales: `rstrip("\\/")` no se lleva un
        # espacio, y con `USERPROFILE=C:\Users\op ` el patrón exigía ese espacio
        # textual y no matcheaba una ruta que continúa. Hallazgo de review (Qodo).
        valor = re.sub(r"[\s\\/]+$", "", valor) if valor else valor
        # El guard es de FORMA, no de longitud: un perfil degenerado (`/`, `C:\`)
        # se redacta como prefijo y se comería cualquier separador del volcado.
        # Se exige que quede al menos un componente propio bajo una raíz.
        if valor and _ES_PERFIL_UTIL(valor):
            # El patrón acepta CUALQUIERA de los dos separadores en cada
            # posición, en vez de exigir los del valor de la variable. Win32
            # acepta los dos y el volcado mezcla: en Windows `USERPROFILE` viene
            # con `\`, mientras que el uso documentado de la sonda es
            # `--exe "C:/Modding/…"`. Con el patrón literal esa combinación no
            # matcheaba y la ruta del perfil salía entera. Se normaliza el
            # PATRÓN, nunca el texto: el volcado tiene que mostrar los
            # separadores que UIA reportó, no los que nos resulten cómodos.
            # Hallazgo de review (Qodo).
            patron = f"[{separadores}]".join(re.escape(parte) for parte in re.split(r"[\\/]", valor))
            resultado = re.sub(
                # Whitespace también CIERRA el perfil: un título de ventana suele
                # pegar la ruta al nombre de la app (`C:\\Users\\op - TexGen 3.00`)
                # y con sólo separador-o-fin eso no matcheaba, así que el volcado
                # imprimía el usuario entero. Hallazgo de review (Qodo).
                patron + rf"(?=[{separadores}]|\s|$)",
                f"<{variable}>",
                resultado,
                flags=re.IGNORECASE,
            )
    usuario = os.environ.get("USERNAME")
    # SIN umbral de longitud: el `len(usuario) > 2` era herencia de cuando el
    # reemplazo era por substring, donde un usuario corto ensuciaba cualquier
    # palabra. El regex de abajo exige que el usuario sea un COMPONENTE completo
    # de ruta, así que ya no hay nada de qué protegerse — y el umbral dejaba sin
    # redactar al operador que se llama `jd`. Hallazgo de review (Qodo).
    if usuario:
        resultado = re.sub(
            rf"(?<![^{separadores}]){re.escape(usuario)}(?![^{separadores}])",
            "<USERNAME>",
            resultado,
            flags=re.IGNORECASE,
        )
    return resultado


def _volcar(observador: ObservadorUIAWindows, pid: int, salida) -> None:
    """Imprime el subárbol de cada ventana top-level del pid, saneado ENTERO.

    Todo campo de TEXTO pasa por `_sanear`, no sólo los que obviamente llevan
    una ruta. Una app que derive su ``AutomationId`` o su ``ClassName`` de una
    ruta filtraría ahí el perfil del operador — y este volcado está hecho para
    pegarse en un PR. Que TexGen/DynDOLOD lo hagan no está verificado (se sabrá
    en un rig real), pero redactar cuesta cero. Los pids son numéricos y no
    llevan nada que redactar. Hallazgo de review (Qodo).
    """
    ventanas = observador.ventanas_de_proceso(pid)
    print(f"  ventanas top-level: {len(ventanas)}", file=salida)
    for ventana in ventanas:
        print(
            f"  ventana titulo={_sanear(ventana.titulo)!r} clase={_sanear(ventana.class_name)!r} pid={ventana.pid}",
            file=salida,
        )
        controles, total, ilegibles = observador.controles_para_volcado(ventana)
        # Dos condiciones DISTINTAS, dos líneas distintas. El árbol recortado por
        # la cota y el control que no se pudo leer se veían igual —`N / M`— y el
        # operador habría leído "se truncó" ante un elemento stale.
        if total > len(controles) + ilegibles:
            print(f"    TRUNCATED: {len(controles) + ilegibles} / {total} controles", file=salida)
            print(
                "    (el volcado se recorta para ser legible; el preflight, en cambio, responde "
                "UNKNOWN/ENUMERACION_INCOMPLETA ante un árbol que no entra entero)",
                file=salida,
            )
        else:
            print(f"    controles enumerados: {len(controles)} / {total}", file=salida)
        if ilegibles:
            print(
                f"    ILEGIBLES: {ilegibles} control(es) fallaron al leerse y se omiten "
                "(stale durante la enumeración); los demás siguen abajo",
                file=salida,
            )
        for control in controles:
            patrones = observador.patrones_de_lectura(control)
            print(
                f"    - automation_id={_sanear(control.automation_id)!r} nombre={_sanear(control.nombre)!r} "
                f"tipo={_sanear(control.tipo_de_control)!r} clase={_sanear(control.class_name)!r} "
                f"pid={control.pid} patrones={patrones}",
                file=salida,
            )


def _analizar_argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        description="Sonda READ-ONLY del árbol UIA de TexGen/DynDOLOD (T5A). No modifica nada.",
    )
    analizador.add_argument("--tool", required=True, choices=sorted(TOOLS_OBSERVABLES))
    analizador.add_argument("--exe", required=True, help="ruta o nombre del ejecutable esperado")
    analizador.add_argument("--pid", type=int, default=None, help="atar la observación a este pid")
    analizador.add_argument("--expected-output", default=None, help="salida administrada esperada")
    analizador.add_argument("--automation-id", default=None)
    analizador.add_argument("--name", dest="nombre", default=None)
    analizador.add_argument("--control-type", dest="tipo", default=None)
    analizador.add_argument(
        "--class-name",
        dest="clase",
        default=None,
        help="clase de ventana Win32 (en Delphi, el nombre de clase: TEdit, TMemo…)",
    )
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    argumentos = _analizar_argumentos(argv)
    salida = sys.stdout

    # Saneado como el resto: una instalación bajo el perfil del operador
    # lleva su nombre de usuario en la ruta, y este volcado se pega en un PR.
    print(f"[T5A] sonda READ-ONLY — tool={argumentos.tool} exe={_sanear(argumentos.exe)}", file=salida)
    localizador = LocalizadorPsutil()
    esperado = pathlib.PurePath(argumentos.exe.replace("\\", "/")).name.lower()
    try:
        procesos = localizador.procesos()
    except ObservacionUIAError as exc:
        # Un proceso que muere a mitad de la enumeración hace que psutil falle, y
        # `LocalizadorPsutil` lo traduce a ObservacionUIAError. Sin este borde,
        # el CLI escupía un traceback justo donde el resto responde con un
        # diagnóstico y un código de salida — y un traceback en el rig es
        # exactamente lo que no se puede pegar en un PR como evidencia.
        print(f"[T5A] ERROR_UIA: no se pudo enumerar procesos: {_sanear(str(exc))}", file=salida)
        return 4
    candidatos = [p for p in procesos if p.nombre_ejecutable.lower() == esperado]
    print(f"[T5A] procesos con ese binario: {len(candidatos)}", file=salida)
    for proceso in candidatos:
        print(f"  pid={proceso.pid} exe={_sanear(proceso.ruta_ejecutable or proceso.nombre_ejecutable)}", file=salida)
    if not candidatos:
        print("[T5A] no hay nada que observar: abrí la herramienta y volvé a correr la sonda.", file=salida)
        return 2

    try:
        observador = ObservadorUIAWindows()
    except UIANoDisponibleError as exc:
        print(f"[T5A] UIA_UNAVAILABLE: {_sanear(str(exc))}", file=salida)
        return 3

    for proceso in candidatos:
        if argumentos.pid is not None and proceso.pid != argumentos.pid:
            continue
        print(f"[T5A] volcado del pid {proceso.pid}", file=salida)
        try:
            _volcar(observador, proceso.pid, salida)
        except ObservacionUIAError as exc:
            # Saneado como cualquier otro campo: estos mensajes llevan
            # `ventana.titulo` y `control.describir()` adentro, así que un fallo
            # COM filtraba por el borde de error lo que el volcado redacta en el
            # camino feliz. Hallazgo de review (Qodo).
            print(f"  ERROR_UIA: {_sanear(str(exc))}", file=salida)

    criterios = CriteriosDeControl(
        automation_id=argumentos.automation_id,
        nombre=argumentos.nombre,
        tipo_de_control=argumentos.tipo,
        class_name=argumentos.clase,
    )
    if argumentos.expected_output is None or criterios.esta_vacio():
        print(
            "[T5A] sin --expected-output y criterios de control no se corre la comparación: "
            "el selector se escribe DESPUÉS de leer el volcado de arriba, no antes.",
            file=salida,
        )
        return 0

    resultado = observar_output(
        SolicitudPreflightUIA(
            tool=argumentos.tool,
            ejecutable_esperado=argumentos.exe,
            salida_administrada_esperada=argumentos.expected_output,
            criterios_del_control=criterios,
            pid=argumentos.pid,
        ),
        localizador=localizador,
        observador=observador,
    )
    print(f"[T5A] {resultado.estado.value} ({resultado.razon.value}): {_sanear(resultado.detalle)}", file=salida)
    print(f"       observado={_sanear(str(resultado.valor_observado))!r}", file=salida)
    print(f"       esperado ={_sanear(resultado.valor_esperado)!r}", file=salida)
    for linea in resultado.evidencia:
        print(f"       evidencia: {_sanear(linea)}", file=salida)
    print(
        "[T5A] recordatorio: un MATCH dice que la GUI MUESTRA esa ruta hoy. No es un comprobante "
        "de escritura física — el destino real lo certifica el post-check de artefactos (T5-v2 "
        "usa este veredicto sólo como gate previo, fail-closed).",
        file=salida,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
