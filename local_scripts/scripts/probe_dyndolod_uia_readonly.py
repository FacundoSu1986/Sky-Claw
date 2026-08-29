#!/usr/bin/env python
"""Sonda READ-ONLY del árbol de UI Automation de TexGen/DynDOLOD (T5A).

**Para qué existe.** ``sky_claw/local/tools/dyndolod_uia_preflight.py`` sabe
decidir MATCH / MISMATCH / UNKNOWN, pero deliberadamente **no** trae un selector
del campo *Output*: nadie midió todavía el árbol UIA de estos binarios, y una
constante escrita de memoria sería una invención con apariencia de evidencia.
Esta sonda es cómo se consigue esa evidencia en un rig Windows real:

    python local_scripts/scripts/probe_dyndolod_uia_readonly.py \\
        --tool TexGen --exe "C:/Modding/DynDOLOD/TexGenx64.exe"

Imprime, saneada, la identidad del proceso, la de su ventana top-level y las
propiedades de cada control (``AutomationId``, ``Name``, ``ControlType``,
``ClassName``, y qué patrón de LECTURA expone). Con eso se decide qué
combinación de propiedades identifica al control de forma inequívoca — y recién
ahí se puede escribir el selector, con la medición al lado.

Si además se le pasan los criterios y la salida administrada, corre el preflight
completo con el observador real y muestra el veredicto:

    python local_scripts/scripts/probe_dyndolod_uia_readonly.py \\
        --tool TexGen --exe "C:/Modding/DynDOLOD/TexGenx64.exe" \\
        --automation-id edOutput --control-type Edit \\
        --expected-output "C:/Games/Skyrim Special Edition/Sky-Claw/DynDOLOD"

**Es de SOLO LECTURA y eso está anclado, no prometido.** Sólo conecta, enumera y
lee propiedades. No pulsa nada, no escribe presets, no cambia el Output, no
inyecta teclado ni mouse, no enfoca ventanas. El ancla por AST de
``tests/test_dyndolod_uia_preflight.py`` cubre este archivo igual que al módulo
productivo: no puede siquiera NOMBRAR una primitiva mutante de UIA/Win32, ni
usar despacho dinámico para llegar a una.

**Por qué el backend vive acá y no en el paquete.** Dos motivos.

1. *Dependencias.* ``comtypes`` (MIT, sin dependencias transitivas, upstream
   Enthought) es el binding COM correcto para esto, pero el repo no tiene hoy
   ninguna dependencia de UI Automation y **todavía no hay evidencia que
   justifique tomar una**: eso es precisamente lo que esta sonda va a medir.
   Acá el import es perezoso, así que el operador la instala en el rig
   (``pip install comtypes``) sin que el runtime de Sky-Claw dependa de ella.
2. *CI multiplataforma.* El paquete tiene que importarse en Ubuntu sin COM ni
   escritorio interactivo. Fuera del paquete, este archivo no puede romperlo:
   nada de ``sky_claw/`` lo importa, y un test lo congela.

**Estado de verificación, sin adornos:** el adaptador COM de abajo **no se
ejecutó nunca** — esta rama se desarrolló en Linux, sin TexGen/DynDOLOD y sin
Windows. Lo que sí está probado es la máquina de decisión del módulo productivo
(90 casos deterministas). Tratá la primera corrida de esta sonda como parte de
la medición, no como una herramienta ya validada.
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
    PID_ILEGIBLE,
    TOOLS_OBSERVABLES,
    TOPE_DE_ELEMENTOS_UIA,
    ControlObservado,
    CriteriosDeControl,
    EnumeracionIncompletaError,
    LocalizadorPsutil,
    ObservacionUIAError,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    VentanaObservada,
    exigir_enumeracion_completa,
    observar_output,
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
#: ESCRIBE el selector a partir de este volcado, así que un `List` que en
#: realidad es un `ListItem` produce un `--control-type` que apunta al control
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
        valor = valor.rstrip("\\/") if valor else valor
        # El guard es de FORMA, no de longitud: un perfil degenerado (`/`, `C:\`)
        # se redacta como prefijo y se comería cualquier separador del volcado.
        # Se exige que quede al menos un componente propio bajo una raíz.
        if valor and _ES_PERFIL_UTIL(valor):
            resultado = re.sub(
                re.escape(valor) + rf"(?=[{separadores}]|$)",
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


class ObservadorUIAWindows:
    """Adaptador READ-ONLY sobre UI Automation, vía ``comtypes``.

    Implementa los tres métodos de ``ObservadorUIA`` y ninguno más. Cada llamada
    COM de acá es una consulta: obtener la raíz, construir una condición,
    ``FindAll`` acotado, leer una propiedad, leer el valor de un patrón de
    lectura. No hay ninguna que modifique estado de la GUI.

    ``comtypes`` se importa dentro de ``__init__`` a propósito: importar este
    archivo en cualquier plataforma no debe fallar, y la ausencia del binding
    tiene que llegar como :class:`UIANoDisponibleError` —que el preflight
    traduce a ``UNKNOWN``— y no como un ``ImportError`` que reviente arriba.

    **Ciclo de vida de COM: una sola inicialización, sin cierre simétrico, y es
    deliberado.** ``CoInitialize()`` no lleva su ``CoUninitialize()`` porque esta
    sonda es un CLI de una corrida: el proceso termina y el sistema libera el
    apartamento. Cerrarlo a mano sería PEOR, no mejor — habría que soltar antes
    todas las referencias COM vivas (``self._uia``, los elementos que el volcado
    todavía sostiene), y un ``CoUninitialize()`` con referencias pendientes es
    exactamente cómo se consigue un crash en vez de una limpieza.

    La consecuencia, dicha para que nadie la descubra a los golpes: **no
    instancies esta clase muchas veces en un mismo intérprete** (un REPL, una
    sesión de diagnóstico larga). Si alguna vez hace falta, el arreglo no es
    agregar el cierre acá sino envolver el apartamento en un context manager que
    sea dueño de las referencias.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UIANoDisponibleError(f"UI Automation es Windows-only; esta plataforma es {sys.platform!r}")
        try:
            import comtypes  # noqa: PLC0415
            import comtypes.client  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover -- depende del rig
            raise UIANoDisponibleError(
                "falta el binding COM: instalá `comtypes` en el rig (pip install comtypes). "
                "Sky-Claw no lo declara como dependencia todavía: la decisión espera la evidencia "
                "que esta misma sonda produce."
            ) from exc
        try:
            comtypes.CoInitialize()
            self._uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia = comtypes.client.CreateObject(
                CLSID_CUIAUTOMATION,
                interface=self._uia_mod.IUIAutomation,
            )
        except Exception as exc:  # pragma: no cover -- depende del rig
            raise UIANoDisponibleError(f"no se pudo inicializar UI Automation: {exc}") from exc

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
            numero = int(crudo)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(crudo)
        return NOMBRES_DE_CONTROL_TYPE.get(numero, str(numero))

    def _pid(self, elemento: object) -> int:
        crudo = self._propiedad(elemento, "UIA_ProcessIdPropertyId")
        try:
            return int(crudo)  # type: ignore[arg-type]
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
        except Exception as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al enumerar ventanas del pid {pid}: {exc}") from exc

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]:
        """Descendientes de ESA ventana. Completos, o :class:`EnumeracionIncompletaError`."""
        try:
            encontrados = self._coleccion_de_controles(ventana)
            return tuple(self._describir(elemento) for elemento in self._elementos(encontrados))
        except EnumeracionIncompletaError:
            raise
        except Exception as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al enumerar controles de {ventana.titulo!r}: {exc}") from exc

    def controles_para_volcado(self, ventana: VentanaObservada) -> tuple[Sequence[ControlObservado], int]:
        """``(controles mostrados, total real)`` para el diagnóstico, nunca para decidir."""
        try:
            encontrados = self._coleccion_de_controles(ventana)
            elementos, total = self._elementos_truncados(encontrados)
            return tuple(self._describir(elemento) for elemento in elementos), total
        except Exception as exc:  # pragma: no cover -- depende del rig
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
            if self._propiedad(control.handle, "UIA_IsValuePatternAvailablePropertyId"):
                patron = control.handle.GetCurrentPattern(self._uia_mod.UIA_ValuePatternId)  # type: ignore[attr-defined]
                if patron:
                    valor = patron.QueryInterface(self._uia_mod.IUIAutomationValuePattern).CurrentValue
                    return None if valor is None else str(valor)
            if self._propiedad(control.handle, "UIA_IsTextPatternAvailablePropertyId"):
                patron = control.handle.GetCurrentPattern(self._uia_mod.UIA_TextPatternId)  # type: ignore[attr-defined]
                if patron:
                    rango = patron.QueryInterface(self._uia_mod.IUIAutomationTextPattern).DocumentRange
                    return str(rango.GetText(-1))
        except Exception as exc:  # pragma: no cover -- depende del rig
            raise ObservacionUIAError(f"fallo al leer el valor de {control.describir()}: {exc}") from exc
        return None

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
            except Exception:  # pragma: no cover -- depende del rig
                disponibles.append(f"{etiqueta}=?")
        return ",".join(disponibles) or "(ninguno)"


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
        controles, total = observador.controles_para_volcado(ventana)
        if total > len(controles):
            print(f"    TRUNCATED: {len(controles)} / {total} controles", file=salida)
            print(
                "    (el volcado se recorta para ser legible; el preflight, en cambio, responde "
                "UNKNOWN/ENUMERACION_INCOMPLETA ante un árbol que no entra entero)",
                file=salida,
            )
        else:
            print(f"    controles enumerados: {len(controles)} / {total}", file=salida)
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
        "[T5A] recordatorio: un MATCH dice que la GUI MUESTRA esa ruta hoy. "
        "No prueba dónde va a escribir una corrida futura (eso es T5-v2).",
        file=salida,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
