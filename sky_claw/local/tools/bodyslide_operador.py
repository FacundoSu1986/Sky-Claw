"""Superficie de operador de BodySlide: ``--mode bodyslide-recovery`` (PR 2c-2b).

Las dos salidas MANUALES del journal de BodySlide —liberar la cuarentena del recurso
(2c-1) y registrar la terminalidad de un ``LANZADO`` (2c-2b, opción (a) del brief
§13.6)— no tenían llamador: un HOLD solo se resolvía editando archivos de estado a mano.
El propietario eligió (2026-11-25) darles una superficie CLI hermana de
``--mode vfs-quarantine``.

Propiedad: *el operador ve lo mismo que el reconciliador y solo cambia algo con evidencia
escrita*. ``status`` no escribe; ``release-quarantine`` y ``confirm-terminal`` exigen
evidencia, quedan en el log como CRITICAL y fallan cerradas, sin tocar el estado, ante
cualquier prueba que falte. Ninguna restaura la config: eso lo hace el reconciliador en
el próximo arranque, con sus propias pruebas.

Corre con Sky-Claw cerrado: este módulo no construye ``AppContext`` (lo ancla
``tests/test_bodyslide_operador.py``). Arrancar el daemon tomaría el recurso que el
operador está liberando.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import pathlib
from typing import Final

from sky_claw.local.tools import bodyslide_journal as bj

logger = logging.getLogger(__name__)


class AccionDelOperador(enum.StrEnum):
    """Acciones de ``--bodyslide-recovery-action``. Congeladas por igualdad en el test."""

    STATUS = "status"
    RELEASE_QUARANTINE = "release-quarantine"
    CONFIRM_TERMINAL = "confirm-terminal"


#: Los ``choices`` del parser salen de acá: una sola lista para la CLI y el módulo.
ACCIONES_DEL_OPERADOR: Final[tuple[str, ...]] = tuple(accion.value for accion in AccionDelOperador)

#: Estado que se muestra para un journal que no se pudo leer o validar.
ESTADO_CORRUPTO: Final[str] = "CORRUPTO"


class AccionDelOperadorRechazadaError(Exception):
    """Falta una prueba: no se cambió nada. El mensaje es para el operador, no un traceback."""


@dataclasses.dataclass(frozen=True, slots=True)
class JournalVisto:
    """Un journal tal como lo ve el operador."""

    archivo: str
    estado: str
    run_id: str | None
    de_esta_instalacion: bool


@dataclasses.dataclass(frozen=True, slots=True)
class InformeDelOperador:
    """Lo que el operador lee después de una acción."""

    accion: AccionDelOperador
    cuarentena_activa: bool
    journals: tuple[JournalVisto, ...]
    cambio: bool
    detalle: str


def _instancia_de(exe_dir: pathlib.Path | None) -> str | None:
    if exe_dir is None:
        return None
    try:
        return bj.identidad_de_instancia(exe_dir)
    except (OSError, bj.JournalError):
        return None


def _journals_vistos(almacen: bj.AlmacenDeJournal, instancia: str | None) -> tuple[JournalVisto, ...]:
    vistos = []
    for ruta in almacen.listar():
        propio = instancia is not None and ruta.name == f"{instancia}.json"
        try:
            documento = almacen.leer(ruta)
        except (bj.JournalCorruptoError, OSError):
            vistos.append(JournalVisto(ruta.name, ESTADO_CORRUPTO, None, propio))
            continue
        vistos.append(JournalVisto(ruta.name, documento.estado.value, documento.run_id, propio))
    return tuple(vistos)


def _exigir_evidencia(evidencia: str | None) -> str:
    if evidencia is None or not evidencia.strip():
        raise AccionDelOperadorRechazadaError(
            "esta acción exige evidencia escrita del operador (--bodyslide-evidence): qué verificaste"
        )
    return evidencia.strip()


def _confirmar_terminal(almacen: bj.AlmacenDeJournal, exe_dir: pathlib.Path | None, evidencia: str) -> str:
    if exe_dir is None:
        raise AccionDelOperadorRechazadaError(
            "no hay carpeta de BodySlide configurada (bodyslide_exe): sin ella no se puede probar la instalación"
        )
    identidad = bj.identidad_actual(exe_dir)
    if identidad is None:
        raise AccionDelOperadorRechazadaError(
            "no se pudo establecer la identidad de la máquina o de la instalación de BodySlide "
            "(con SKYCLAW_ISOLATED=1 no se lee): sin ella no se registra nada"
        )
    ruta = almacen.ruta_de(identidad.instancia)
    try:
        documento = bj.registrar_terminalidad_por_operador(
            ruta, almacen=almacen, identidad_actual=identidad, exe_dir=exe_dir, evidencia=evidencia
        )
    except FileNotFoundError as exc:
        raise AccionDelOperadorRechazadaError("no hay journal de esta instalación de BodySlide") from exc
    except bj.JournalCorruptoError as exc:
        raise AccionDelOperadorRechazadaError(f"el journal no es válido (corrupto): {exc}") from exc
    except (bj.JournalError, OSError, ValueError) as exc:
        raise AccionDelOperadorRechazadaError(str(exc)) from exc
    return (
        f"Corrida {documento.run_id}: LANZADO → TERMINAL. La config se restaura en el próximo arranque de "
        "Sky-Claw, si los archivos siguen siendo los que dejó la corrida."
    )


def ejecutar_accion_del_operador(
    accion: AccionDelOperador,
    *,
    almacen: bj.AlmacenDeJournal,
    exe_dir: pathlib.Path | None,
    evidencia: str | None,
) -> InformeDelOperador:
    """Ejecuta una acción del operador y devuelve lo que quedó.

    Raises:
        AccionDelOperadorRechazadaError: falta una prueba; no se cambió nada.
    """
    cambio = False
    detalle = ""
    if accion is AccionDelOperador.RELEASE_QUARANTINE:
        texto = _exigir_evidencia(evidencia)
        cambio = bj.liberar_cuarentena(almacen, evidencia=texto)
        detalle = (
            "Cuarentena de bodyslide-meshes liberada."
            if cambio
            else "El recurso bodyslide-meshes no estaba en cuarentena; no se cambió nada."
        )
    elif accion is AccionDelOperador.CONFIRM_TERMINAL:
        detalle = _confirmar_terminal(almacen, exe_dir, _exigir_evidencia(evidencia))
        cambio = True
    return InformeDelOperador(
        accion=accion,
        cuarentena_activa=bj.cuarentena_activa(almacen),
        journals=_journals_vistos(almacen, _instancia_de(exe_dir)),
        cambio=cambio,
        detalle=detalle,
    )


__all__ = [
    "ACCIONES_DEL_OPERADOR",
    "ESTADO_CORRUPTO",
    "AccionDelOperador",
    "AccionDelOperadorRechazadaError",
    "InformeDelOperador",
    "JournalVisto",
    "ejecutar_accion_del_operador",
]
