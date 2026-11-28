"""Fence durable por RECURSO del precache de grass (P1, invariantes I1/I6/I7).

Un ritual puede dejar un ``SkyrimSE.exe`` vivo. El fence es la protección persistente que impide
que OTRO ritual toque los mismos recursos mientras eso no se haya demostrado cerrado.

Modelo (mínimo, sin supervisor ni coordinador genérico):

- **Un marcador por recurso mutable**, no por par ``game_path``+``cache``: dos rituales que comparten
  *cualquiera* de los recursos colisionan en el marcador de ESE recurso. Recursos: la instalación del
  juego (``game_path``), el directorio físico de caché (``overwrite_grass_dir``) y la raíz MO2
  (``<overwrite>/../..``), que es donde viven el perfil temporal y el mod de configuración del ritual.
- **Adquisición exclusiva**: creación con ``os.open(O_CREAT | O_EXCL)``, no ``os.replace``. Una
  escritura atómica NO es una adquisición: si el archivo ya existe, ``O_EXCL`` falla y se lee el
  marcador para decidir (propio ⇒ actualización; ajeno ⇒ bloqueo; ilegible ⇒ bloqueo).
- **Adquisición de varios recursos**: orden lexicográfico por *resource id* para que dos rituales no
  se bloqueen mutuamente en orden invertido; si un paso falla, se compensan **sólo los propios**
  (``run_id`` coincidente). Un fallo de compensación deja el marcador: conservador.
- **Consulta de solo lectura** (``bloqueo_por_fence_scope``): no borra, no modifica, no renueva.
- **Liberación** (``liberar_fence_scope``): sólo con autorización explícita del dueño de los leases y
  sólo marcadores cuyo ``run_id`` persistido coincide; cada ``unlink`` se **verifica** y un fallo no
  se reporta como liberado.

Este módulo NO es un supervisor de procesos ni un administrador de leases: sólo persiste, lee
conservador y borra lo propio.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Literal

import psutil

from sky_claw.local.tools.grass_cache_safety import _escribir_json_atomico, _validar_raiz_fisica

logger = logging.getLogger(__name__)

#: Subdirectorio del estado durable donde viven los marcadores.
FENCE_DIR = "fence"

EstadoProceso = Literal["vivo", "terminado", "indeterminado"]

#: Tipos de recurso protegido (el sufijo del resource id).
RECURSO_JUEGO = "game"
RECURSO_CACHE = "cache"
RECURSO_MO2 = "mo2"


class FenceCorruptoError(Exception):
    """El marcador existe pero no se puede interpretar: se trata como bloqueo (fail-closed)."""


class FenceAdquisicionError(Exception):
    """No se pudo adquirir el fence completo (ya compensado lo propio)."""


@dataclass(frozen=True, slots=True)
class FenceProceso:
    """Marcador de una corrida sobre UN recurso."""

    run_id: str
    intento: int
    lanzamiento: str
    pid: int | None
    create_time: float | None
    modo: str
    game_path: str
    overwrite_grass_dir: str
    creado: str
    recurso: str = ""

    def a_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class FenceScope:
    """Recursos físicos que un ritual puede mutar (I1)."""

    game_path: pathlib.Path
    overwrite_grass_dir: pathlib.Path
    mo2_root: pathlib.Path | None = None

    @classmethod
    def desde_config(cls, *, game_path: pathlib.Path, overwrite_grass_dir: pathlib.Path) -> FenceScope:
        """Scope derivado de los recursos REALES del ritual.

        El perfil temporal y el mod de configuración viven bajo la raíz MO2, que no está en el DTO:
        se deriva de ``<mo2>/overwrite/Grass`` (dos niveles arriba) sólo cuando la estructura es la
        esperada. Si no lo es, el scope cubre juego + caché y la ausencia del recurso MO2 se declara
        (no se inventa una ruta).
        """
        mo2_root: pathlib.Path | None = None
        if overwrite_grass_dir.parent.name.lower() == "overwrite":
            mo2_root = overwrite_grass_dir.parent.parent
        return cls(game_path=game_path, overwrite_grass_dir=overwrite_grass_dir, mo2_root=mo2_root)

    def recursos(self) -> tuple[tuple[str, str], ...]:
        """``(resource_id, valor_físico)`` ordenados — el orden es el de adquisición."""
        pares: list[tuple[str, str]] = [
            (f"{RECURSO_JUEGO}:{_huella(self.game_path)}", str(self.game_path.resolve()).lower()),
            (f"{RECURSO_CACHE}:{_huella(self.overwrite_grass_dir)}", str(self.overwrite_grass_dir.resolve()).lower()),
        ]
        if self.mo2_root is not None:
            pares.append((f"{RECURSO_MO2}:{_huella(self.mo2_root)}", str(self.mo2_root.resolve()).lower()))
        return tuple(sorted(pares))


@dataclass(frozen=True, slots=True)
class Adquisicion:
    """Resultado de adquirir el fence de un scope."""

    ok: bool
    bloqueo: str | None
    rutas: tuple[pathlib.Path, ...]
    recursos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Liberacion:
    """Resultado de intentar liberar el fence propio."""

    autorizada: bool
    liberados: tuple[str, ...]
    conservados: tuple[str, ...]
    fallos: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.autorizada and not self.fallos and not self.conservados


def _huella(ruta: pathlib.Path) -> str:
    import hashlib

    return hashlib.sha256(str(ruta.resolve()).lower().encode("utf-8")).hexdigest()[:24]


def ruta_de_recurso(raiz_estado: pathlib.Path, resource_id: str) -> pathlib.Path:
    """Ruta del marcador de UN recurso (identidad física estable)."""
    tipo, _, huella = resource_id.partition(":")
    return raiz_estado / FENCE_DIR / f"fence_{tipo}_{huella}.json"


def ruta_fence(
    *, raiz_estado: pathlib.Path, game_path: pathlib.Path, overwrite_grass_dir: pathlib.Path
) -> pathlib.Path:
    """Ruta del marcador del CACHÉ (compatibilidad con llamadas de una sola ruta)."""
    return ruta_de_recurso(raiz_estado, f"{RECURSO_CACHE}:{_huella(overwrite_grass_dir)}")


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def _payload(fence: FenceProceso) -> bytes:
    return json.dumps(fence.a_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")


def leer_fence(ruta: pathlib.Path) -> FenceProceso | None:
    """Lee el marcador. ``None`` = no existe. Ilegible ⇒ :class:`FenceCorruptoError`.

    Un enlace colgante NO es ausencia: si ``lstat``/``read`` fallan por cualquier motivo distinto de
    «no existe», se trata como marcador ilegible.
    """
    try:
        if ruta.is_symlink():
            raise FenceCorruptoError(f"El fence {ruta} es un enlace: no se interpreta como marcador")
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return None
    except FenceCorruptoError:
        raise
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
            recurso=str(carga.get("recurso", "")),
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise FenceCorruptoError(f"Fence ilegible en {ruta}: {exc}") from exc


def _crear_exclusivo(ruta: pathlib.Path, fence: FenceProceso) -> None:
    """Crea el marcador SÓLO si no existe (``O_CREAT | O_EXCL``), con contención física validada."""
    _validar_raiz_fisica(ruta.parent, que="del fence de grass")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(ruta), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, _payload(fence))
        os.fsync(fd)
    finally:
        os.close(fd)


def _actualizar_propio(ruta: pathlib.Path, fence: FenceProceso) -> bool:
    """Reemplaza un marcador PROPIO de forma atómica, verificando el persistido antes y después."""
    try:
        actual = leer_fence(ruta)
    except FenceCorruptoError:
        return False  # ilegible: no se toca (conservador)
    if actual is None or actual.run_id != fence.run_id:
        return False
    _escribir_json_atomico(ruta, fence.a_dict())
    try:
        tras = leer_fence(ruta)
    except FenceCorruptoError:
        return False
    return tras is not None and tras.run_id == fence.run_id and tras.intento == fence.intento


# ---------------------------------------------------------------------------
# Adquisición / consulta / liberación
# ---------------------------------------------------------------------------


def adquirir_fence(
    raiz_estado: pathlib.Path,
    scope: FenceScope,
    *,
    run_id: str,
    intento: int,
    lanzamiento: str,
    pid: int | None,
    create_time: float | None,
    modo: str,
    creado: str,
) -> Adquisicion:
    """Adquiere el fence de TODOS los recursos del scope, o no adquiere ninguno nuevo.

    Orden lexicográfico por *resource id*. Cada recurso: creación exclusiva; si ya existe, sólo se
    actualiza cuando el ``run_id`` persistido es el propio (marcador propio). Cualquier marcador
    ajeno o ilegible ⇒ bloqueo con compensación de lo propio y sin tocar lo ajeno.

    Si el estado de una adquisición parcial es indeterminado (fallo de compensación), los marcadores
    se **conservan**: es la opción segura.
    """
    creados: list[tuple[pathlib.Path, str]] = []
    for resource_id, valor in scope.recursos():
        ruta = ruta_de_recurso(raiz_estado, resource_id)
        fence = FenceProceso(
            run_id=run_id,
            intento=intento,
            lanzamiento=lanzamiento,
            pid=pid,
            create_time=create_time,
            modo=modo,
            game_path=str(scope.game_path),
            overwrite_grass_dir=str(scope.overwrite_grass_dir),
            creado=creado,
            recurso=resource_id,
        )
        try:
            _crear_exclusivo(ruta, fence)
            creados.append((ruta, resource_id))
            continue
        except FileExistsError:
            pass
        except OSError as exc:
            _compensar(creados, run_id)
            return Adquisicion(
                ok=False,
                bloqueo=f"No se pudo persistir el fence de {resource_id} ({valor}): {exc}",
                rutas=tuple(r for r, _ in creados),
                recursos=tuple(i for _, i in creados),
            )
        try:
            existente = leer_fence(ruta)
        except FenceCorruptoError as exc:
            _compensar(creados, run_id)
            return Adquisicion(
                ok=False,
                bloqueo=(
                    f"El fence del recurso {resource_id} ({valor}) es ILEGIBLE ({exc}). Se conserva "
                    "como bloqueo: verificá manualmente que no queden procesos del precache, "
                    "lanzamientos pendientes ni recursos del ritual anterior en uso."
                ),
                rutas=tuple(r for r, _ in creados),
                recursos=tuple(i for _, i in creados),
            )
        if existente is None:  # carrera: desapareció entre O_EXCL y la lectura
            try:
                _crear_exclusivo(ruta, fence)
                creados.append((ruta, resource_id))
                continue
            except OSError as exc:
                _compensar(creados, run_id)
                return Adquisicion(
                    ok=False,
                    bloqueo=f"No se pudo crear el fence de {resource_id} tras una carrera: {exc}",
                    rutas=tuple(r for r, _ in creados),
                    recursos=tuple(i for _, i in creados),
                )
        if existente.run_id != run_id:
            _compensar(creados, run_id)
            return Adquisicion(
                ok=False,
                bloqueo=(
                    f"Hay un ritual de grass previo SIN cierre demostrado sobre el recurso "
                    f"{resource_id} ({valor}): run_id={existente.run_id}, intento={existente.intento}, "
                    f"lanzamiento={existente.lanzamiento}, pid={existente.pid}. No se inicia otro "
                    "ritual sobre recursos compartidos. Recuperación: cerrá el juego y verificá que no "
                    "quede ningún SkyrimSE.exe del precache, ningún lanzamiento pendiente ni recursos "
                    "del ritual anterior en uso antes de retirar el marcador."
                ),
                rutas=tuple(r for r, _ in creados),
                recursos=tuple(i for _, i in creados),
            )
        try:
            actualizada = _actualizar_propio(ruta, fence)
        except OSError as exc:
            # C1: el marcador conservador anterior se conserva; no se compensa lo ajeno.
            _compensar(creados, run_id)
            return Adquisicion(
                ok=False,
                bloqueo=(
                    f"No se pudo actualizar el fence propio del recurso {resource_id} ({valor}): {exc}. "
                    "Se conserva el marcador anterior."
                ),
                rutas=tuple(r for r, _ in creados),
                recursos=tuple(i for _, i in creados),
            )
        if not actualizada:
            _compensar(creados, run_id)
            return Adquisicion(
                ok=False,
                bloqueo=(
                    f"No se pudo actualizar el fence propio del recurso {resource_id}: el marcador "
                    "persistido no coincide con esta corrida."
                ),
                rutas=tuple(r for r, _ in creados),
                recursos=tuple(i for _, i in creados),
            )
    return Adquisicion(
        ok=True,
        bloqueo=None,
        rutas=tuple(ruta_de_recurso(raiz_estado, i) for i, _ in scope.recursos()),
        recursos=tuple(i for i, _ in scope.recursos()),
    )


def _compensar(creados: list[tuple[pathlib.Path, str]], run_id: str) -> None:
    """Borra SÓLO los marcadores creados por esta corrida. Un fallo se conserva (conservador)."""
    for ruta, resource_id in reversed(creados):
        try:
            actual = leer_fence(ruta)
        except FenceCorruptoError:
            logger.warning("Compensación: %s ilegible, se conserva.", ruta)
            continue
        if actual is not None and actual.run_id != run_id:
            logger.warning("Compensación: %s es de otra corrida, no se toca.", ruta)
            continue
        with contextlib.suppress(OSError):
            ruta.unlink()
            logger.info("Compensación: se retiró el marcador propio de %s.", resource_id)


def bloqueo_por_fence_scope(raiz_estado: pathlib.Path, scope: FenceScope) -> str | None:
    """Bloqueo si ALGÚN recurso del scope tiene marcador; ``None`` si se puede seguir.

    **Solo lectura**: no borra, no modifica, no renueva. Un marcador previo bloquea aunque su PID
    registrado esté muerto o reciclado: la desaparición de ese PID no demuestra el cierre integral.
    """
    for resource_id, valor in scope.recursos():
        ruta = ruta_de_recurso(raiz_estado, resource_id)
        try:
            fence = leer_fence(ruta)
        except FenceCorruptoError as exc:
            return (
                f"Hay un fence ILEGIBLE en {ruta} ({exc}). Se trata como bloqueo: verificá a mano que "
                "no queden procesos SkyrimSE.exe del precache, lanzamientos pendientes ni recursos "
                f"del ritual anterior en uso; recién entonces retirá {ruta.name}."
            )
        if fence is None:
            continue
        estado = estado_del_proceso(fence)
        return (
            f"Hay un ritual de grass previo SIN cierre demostrado sobre el recurso {resource_id} "
            f"({valor}): run_id={fence.run_id}, intento={fence.intento}, "
            f"lanzamiento={fence.lanzamiento}, pid={fence.pid}, estado_del_pid={estado}. No se inicia "
            "otro ritual sobre recursos compartidos. Recuperación: cerrá el juego, verificá que no "
            "quede ningún SkyrimSE.exe del precache, ningún lanzamiento pendiente y ningún recurso del "
            "ritual anterior en uso, y recién entonces retirá el marcador. La muerte del PID "
            "registrado NO alcanza como prueba."
        )
    return None


def liberar_fence_scope(
    raiz_estado: pathlib.Path,
    scope: FenceScope,
    *,
    run_id: str,
    autorizado: bool,
) -> Liberacion:
    """Libera los marcadores PROPIOS del scope, sólo con autorización del dueño de los leases.

    - ``autorizado=False`` ⇒ no se borra nada (se conserva la protección).
    - Marcador con otro ``run_id`` ⇒ se conserva (nunca se borra lo ajeno).
    - Cada ``unlink`` se verifica con una relectura: un fallo no se reporta como liberado.
    """
    liberados: list[str] = []
    conservados: list[str] = []
    fallos: list[str] = []
    for resource_id, _valor in scope.recursos():
        ruta = ruta_de_recurso(raiz_estado, resource_id)
        try:
            fence = leer_fence(ruta)
        except FenceCorruptoError:
            conservados.append(f"{resource_id}: ilegible")
            continue
        if fence is None:
            continue
        if fence.run_id != run_id:
            conservados.append(f"{resource_id}: pertenece a {fence.run_id}")
            continue
        if not autorizado:
            conservados.append(f"{resource_id}: sin autorización de cierre")
            continue
        try:
            ruta.unlink()
        except OSError as exc:
            fallos.append(f"{resource_id}: {exc}")
            continue
        with contextlib.suppress(OSError):
            if ruta.exists():
                fallos.append(f"{resource_id}: sigue existiendo tras el borrado")
            else:
                liberados.append(resource_id)
    if fallos or conservados:
        logger.warning(
            "Fence conservado parcialmente (liberados=%s, conservados=%s, fallos=%s)",
            liberados,
            conservados,
            fallos,
        )
    return Liberacion(
        autorizada=autorizado,
        liberados=tuple(liberados),
        conservados=tuple(conservados),
        fallos=tuple(fallos),
    )


# ---------------------------------------------------------------------------
# Estado del proceso registrado
# ---------------------------------------------------------------------------


def estado_del_proceso(fence: FenceProceso) -> EstadoProceso:
    """Clasifica el proceso del marcador. ``indeterminado`` si no se puede afirmar nada.

    **No** se usa para liberar: sólo para el mensaje de recuperación y el diagnóstico.
    """
    if fence.pid is None:
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


__all__ = [
    "FENCE_DIR",
    "RECURSO_CACHE",
    "RECURSO_JUEGO",
    "RECURSO_MO2",
    "Adquisicion",
    "EstadoProceso",
    "FenceAdquisicionError",
    "FenceCorruptoError",
    "FenceProceso",
    "FenceScope",
    "Liberacion",
    "adquirir_fence",
    "bloqueo_por_fence_scope",
    "estado_del_proceso",
    "leer_fence",
    "liberar_fence_scope",
    "ruta_de_recurso",
    "ruta_fence",
]
