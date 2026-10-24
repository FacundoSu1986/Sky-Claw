"""Evidencia de procesos VIVOS: la ruta retenida en memoria (P5-A).

El brief separa explícitamente "ruta configurada" de "ruta retenida en memoria
por procesos activos", y el ADR 0012 (§29.4, clase 3) ya había identificado que
los consumidores con cache en memoria son un problema real. Para un oráculo hay
una consecuencia directa: **un valor persistido no prueba qué se está
ejecutando**. La única prueba de eso es la imagen del proceso que el sistema
operativo tiene cargada.

Este módulo es read-only por construcción: enumera la tabla de procesos y lee la
ruta de la imagen. No abre handles de escritura, no inyecta, no termina nada.

``indeterminate`` no es un detalle: un ``AccessDenied`` al leer la tabla no es
"no hay ningún juego corriendo". Colapsarlos haría que el oráculo habilitara una
activación sobre un runtime en uso.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Iterable
from typing import Any, cast

from sky_claw.local.frozen_runtime.effective_runtime.models import (
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
)

#: Nombres de ejecutable del juego, en minúsculas (Windows no distingue caja).
_NOMBRES_DE_JUEGO: frozenset[str] = frozenset({"skyrimse.exe", "skyrim.exe", "skyrimvr.exe"})
#: Loaders de SKSE por edición: los tres son binarios DISTINTOS, pero para el
#: oráculo todos significan "alguien lanzó el juego vía SKSE desde algún root".
#: ``skse64.exe`` no es un binario canónico de la distribución de SKSE, pero
#: el oráculo lo audita como lanzador registrado (Oracle #1 de la auditoría):
#: si algún setup lo usa como nombre de loader, el proceso vivo debe clasificarse
#: o la evidencia de la raíz real se pierde.
_NOMBRES_DE_LOADER: frozenset[str] = frozenset(
    {"skse64_loader.exe", "skse_loader.exe", "sksevr_loader.exe", "skse64.exe"}
)
_NOMBRE_DE_MO2 = "modorganizer.exe"


def _clasificar(nombre: str) -> LiveProcessKind | None:
    """Traduce un nombre de imagen a la clase que el oráculo entiende."""
    minusculas = nombre.lower()
    if minusculas in _NOMBRES_DE_JUEGO:
        return LiveProcessKind.GAME
    if minusculas in _NOMBRES_DE_LOADER:
        return LiveProcessKind.SKSE_LOADER
    if minusculas == _NOMBRE_DE_MO2:
        return LiveProcessKind.MO2
    return None


def _foto_desde_iterador(procesos: Iterable[Any]) -> LiveProcessSnapshot:
    """Construye la foto desde objetos con ``.info`` (psutil o un doble de test)."""
    evidencias: list[LiveProcessEvidence] = []
    indeterminado = False
    motivos: list[str] = []

    for proceso in procesos:
        try:
            info = proceso.info
        except Exception:  # noqa: BLE001 - psutil lanza tipos propios, no OSError
            indeterminado = True
            motivos.append("un proceso desapareció durante el barrido")
            continue

        nombre = info.get("name") or ""
        kind = _clasificar(str(nombre))
        if kind is None:
            continue

        ruta_imagen = info.get("exe")
        if not ruta_imagen:
            # El proceso EXISTE y su nombre lo delata, pero no sabemos desde
            # dónde corre: es exactamente "no sé", no "no hay".
            indeterminado = True
            motivos.append(f"{nombre} vivo sin ruta de imagen legible (pid {info.get('pid')})")
            continue

        creado = info.get("create_time") or 0
        evidencias.append(
            LiveProcessEvidence(
                kind=kind,
                pid=int(info.get("pid") or 0),
                image_path=str(ruta_imagen),
                started_at_ns=int(float(creado) * 1_000_000_000),
            )
        )

    return LiveProcessSnapshot(
        processes=tuple(evidencias),
        indeterminate=indeterminado,
        detail="; ".join(motivos),
    )


def probe_live_processes(
    *,
    process_iter: Callable[[], Iterable[Any]] | None = None,
) -> LiveProcessSnapshot:
    """Foto de los procesos relevantes (juego, loader de SKSE, MO2).

    Args:
        process_iter: inyectable; por defecto ``psutil.process_iter`` con los
            atributos que hacen falta. Un fallo global de psutil degrada a
            ``indeterminate=True`` — nunca a "no hay procesos".

    Returns:
        ``LiveProcessSnapshot`` con las evidencias y la bandera de indeterminación.
    """
    if process_iter is None:
        try:
            import psutil
        except ImportError:
            return LiveProcessSnapshot(
                processes=(),
                indeterminate=True,
                detail="psutil no está disponible: la tabla de procesos no es observable",
            )

        def _iterador() -> Iterable[Any]:
            return cast(Iterable[Any], psutil.process_iter(["pid", "name", "exe", "create_time"]))

        process_iter = _iterador

    try:
        return _foto_desde_iterador(process_iter())
    except Exception as exc:  # noqa: BLE001 - psutil.Error no hereda de OSError
        return LiveProcessSnapshot(
            processes=(),
            indeterminate=True,
            detail=f"la tabla de procesos no se pudo leer: {exc}",
        )


def raiz_de_la_imagen(image_path: str) -> pathlib.Path | None:
    """Raíz del runtime a partir de la imagen observada, o ``None``.

    Se sube **un** nivel desde el ejecutable y nada más: ``skse64_loader.exe`` y
    ``SkyrimSE.exe`` viven en la raíz del juego, y ascender más convertiría
    ``.../Skyrim Special Edition/Data/tools/x.exe`` en el directorio padre
    equivocado. Un loader lanzado desde un subdirectorio no es un caso que este
    slice afirme cubrir.
    """
    if not image_path:
        return None
    return pathlib.Path(image_path).parent
