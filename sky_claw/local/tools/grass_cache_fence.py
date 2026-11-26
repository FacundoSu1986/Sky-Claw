"""Fence durable del precache de grass (P1, F1).

Una corrida que no puede **demostrar** que su ``SkyrimSE.exe`` terminó deja el caché en un estado
incierto. Hasta la ronda anterior eso se reportaba, pero **nada impedía** que otro ritual arrancara y
escribiera el mismo ``overwrite/Grass`` mientras el proceso anterior seguía vivo. Este módulo es el
fence persistente y acotado que cierra ese hueco.

Propiedades del mecanismo:

- **Se crea ANTES de pedir el spawn.** Si Sky-Claw muere entre el marcador y el kill, el próximo
  arranque encuentra incertidumbre en lugar de silencio.
- **Identidad física**: la ruta del marcador se deriva del ``game_path`` y del ``overwrite_grass_dir``
  ya resueltos (``Path.resolve()``), así dos instalaciones distintas no comparten fence.
- **Escritura atómica** con el escritor que ya usa P0 (``_escribir_json_atomico``: temporal + replace
  en el mismo directorio).
- **Marcador ilegible = BLOQUEO**, nunca «no hay marcador»: un fence corrupto no puede abrir el
  pipeline.
- **Se borra sólo cuando se demuestra que el proceso ya no puede seguir activo** (PID ausente o
  reciclado por otro proceso con igual ``create_time`` comparado).

No crea un supervisor nuevo ni retiene locks: es un archivo de estado, consultado por el servicio
antes de cualquier mutación del ritual.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Literal

import psutil

from sky_claw.local.tools.grass_cache_safety import (
    _escribir_json_atomico,
    _validar_raiz_fisica,
    huella_del_cache,
)

logger = logging.getLogger(__name__)

#: Subdirectorio del estado durable donde viven los marcadores.
FENCE_DIR = "fence"

EstadoProceso = Literal["vivo", "terminado", "indeterminado"]


class FenceCorruptoError(Exception):
    """El marcador existe pero no se puede interpretar: se trata como bloqueo (fail-closed)."""


@dataclass(frozen=True, slots=True)
class FenceProceso:
    """Marcador de una corrida que puede haber dejado un proceso vivo."""

    run_id: str
    intento: int
    lanzamiento: str
    pid: int | None
    create_time: float | None
    modo: str
    game_path: str
    overwrite_grass_dir: str
    creado: str

    def a_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def ruta_fence(
    *, raiz_estado: pathlib.Path, game_path: pathlib.Path, overwrite_grass_dir: pathlib.Path
) -> pathlib.Path:
    """Ruta del marcador para ESTA instalación y ESTE caché (identidad física)."""
    identidad = huella_del_cache(
        [
            ("game", 0),
            ("cache", 0),
            (str(game_path.resolve()).lower(), 0),
            (str(overwrite_grass_dir.resolve()).lower(), 0),
        ]
    )
    return raiz_estado / FENCE_DIR / f"fence_{identidad[:24]}.json"


def escribir_fence(ruta: pathlib.Path, fence: FenceProceso) -> None:
    """Escribe el marcador de forma atómica, validando la cadena física de la raíz.

    La raíz de estado se valida con la primitiva de P0 (``_validar_raiz_fisica``: sin enlaces ni
    junctions). Un fallo acá **no** se silencia: quien llama decide (el runner lo registra y sigue,
    porque negarse a lanzar por no poder escribir el fence sería peor que lanzar sin él).
    """
    _validar_raiz_fisica(ruta.parent, que="del fence de grass")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    _escribir_json_atomico(ruta, fence.a_dict())


def leer_fence(ruta: pathlib.Path) -> FenceProceso | None:
    """Lee el marcador. ``None`` = no existe. Marcador ilegible ⇒ :class:`FenceCorruptoError`."""
    try:
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FenceCorruptoError(f"No se pudo leer el fence {ruta}: {exc}") from exc
    try:
        carga = json.loads(crudo.decode("utf-8"))
        return FenceProceso(
            run_id=str(carga["run_id"]),
            intento=int(carga["intento"]),
            lanzamiento=str(carga["lanzamiento"]),
            pid=int(carga["pid"]) if carga.get("pid") is not None else None,
            create_time=float(carga["create_time"]) if carga.get("create_time") is not None else None,
            modo=str(carga["modo"]),
            game_path=str(carga["game_path"]),
            overwrite_grass_dir=str(carga["overwrite_grass_dir"]),
            creado=str(carga["creado"]),
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise FenceCorruptoError(f"Fence ilegible en {ruta}: {exc}") from exc


def estado_del_proceso(fence: FenceProceso) -> EstadoProceso:
    """Clasifica el proceso del marcador. ``indeterminado`` si no se puede afirmar nada."""
    if fence.pid is None:
        # Sin PID: hay lanzamiento solicitado y no se obtuvo identidad ⇒ no se puede afirmar nada.
        return "indeterminado"
    try:
        if not psutil.pid_exists(fence.pid):
            return "terminado"
        proc = psutil.Process(fence.pid)
        if fence.create_time is not None and proc.create_time() != fence.create_time:
            return "terminado"  # PID reciclado por otro proceso
    except psutil.NoSuchProcess:
        return "terminado"
    except psutil.Error:
        return "indeterminado"
    return "vivo"


def borrar_fence(ruta: pathlib.Path) -> None:
    """Borra el marcador (sólo cuando la terminalidad quedó demostrada). Best-effort."""
    try:
        ruta.unlink(missing_ok=True)
    except OSError:
        logger.warning("No se pudo borrar el fence %s", ruta, exc_info=True)


def bloqueo_por_fence(ruta: pathlib.Path) -> str | None:
    """Mensaje de bloqueo si el fence impide operar; ``None`` si se puede seguir.

    **Solo lectura.** Esta consulta no borra, no modifica, no renueva ni sobrescribe ningún marcador:
    la liberación es una decisión aparte, con prueba de propiedad y de terminalidad integral.

    - No existe ⇒ se puede seguir.
    - Existe de una corrida anterior ⇒ **bloqueo**, aunque su PID registrado esté muerto o reciclado:
      la desaparición de ESE PID no demuestra que hayan terminado todos los procesos relevantes del
      ritual (pudo relanzarse, o quedar un hijo con otra identidad).
    - Ilegible/corrupto ⇒ **bloqueo** (no ausencia).
    - Vivo o indeterminado ⇒ **bloqueo**.
    """
    try:
        fence = leer_fence(ruta)
    except FenceCorruptoError as exc:
        return (
            f"Hay un fence ILEGIBLE en {ruta} ({exc}). Un marcador que no se puede interpretar se "
            "trata como bloqueo. Antes de liberarlo, verificá a mano que no queden procesos "
            "SkyrimSE.exe del precache, lanzamientos pendientes ni recursos del ritual anterior en "
            f"uso; recién entonces retirá {ruta.name}."
        )
    if fence is None:
        return None
    estado = estado_del_proceso(fence)
    return (
        f"Hay un ritual de grass previo SIN cierre demostrado (run_id={fence.run_id}, "
        f"intento={fence.intento}, lanzamiento={fence.lanzamiento}, pid={fence.pid}, "
        f"estado_del_pid={estado}). No se inicia otro ritual sobre el mismo caché. Recuperación: "
        "cerrá el juego, verificá que no quede ningún SkyrimSE.exe del precache, ningún lanzamiento "
        "pendiente y ningún recurso del ritual anterior en uso (clon/perfil), y recién entonces "
        f"retirá el marcador {ruta}. La muerte del PID registrado NO alcanza como prueba: el ritual "
        "pudo relanzar el juego con otra identidad."
    )


def puede_liberar_fence(
    fence: FenceProceso,
    *,
    run_id: str,
    game_path: str,
    overwrite_grass_dir: str,
    terminalidad_integral: bool,
    locks_propios: bool,
    lanzamiento_pendiente: bool,
) -> bool:
    """¿Esta corrida puede borrar ESTE marcador? Todas las condiciones, en conjunto.

    - ``run_id`` del marcador == el de esta corrida (nunca el de otra ejecución).
    - Ámbito físico protegido idéntico (misma instalación y mismo caché).
    - Terminalidad **integral** demostrada (no «el PID murió»).
    - Propiedad vigente de los locks del ritual.
    - Ningún intento de lanzamiento pendiente.
    """
    return (
        fence.run_id == run_id
        and fence.game_path == game_path
        and fence.overwrite_grass_dir == overwrite_grass_dir
        and terminalidad_integral
        and locks_propios
        and not lanzamiento_pendiente
    )


def liberar_fence_propio(ruta: pathlib.Path, fence: FenceProceso, **condiciones: object) -> bool:
    """Borra el marcador **sólo** si :func:`puede_liberar_fence` lo autoriza.

    Un runner que nunca consiguió crear su fence no tiene ``run_id`` coincidente y por lo tanto no
    puede borrar uno preexistente. Si no se puede demostrar el cierre seguro, el marcador se conserva.
    """
    if not puede_liberar_fence(fence, **condiciones):  # type: ignore[arg-type]
        logger.warning(
            "Fence %s conservado: no se demostró propiedad y cierre seguro (run_id=%s).",
            ruta,
            getattr(fence, "run_id", "?"),
        )
        return False
    borrar_fence(ruta)
    return True


__all__ = [
    "FENCE_DIR",
    "FenceCorruptoError",
    "FenceProceso",
    "EstadoProceso",
    "bloqueo_por_fence",
    "borrar_fence",
    "escribir_fence",
    "estado_del_proceso",
    "leer_fence",
    "ruta_fence",
]
