"""Helper de subproceso para el test de contención cross-process de T-31.

Cada rol corre en SU PROPIO proceso, con SU PROPIA instancia de
``DistributedLockManager`` sobre el MISMO archivo SQLite. La exclusión entre
procesos la da la fila ``resource_locks``, no el estado en memoria.

Roles:
- ``A``: adquiere el lock de instalación de ``install_dir`` y lo sostiene
  mientras sincroniza con el orquestador vía archivos sentinel.
- ``B``: corre ``ToolsInstaller.ensure_loot`` contra el mismo ``install_dir``.
  Espera que A tenga el lock y verifica que QUEDA BLOQUEADO hasta que A libera;
  recién entonces ve ``already_existed=True``, sin descarga ni HITL.

Sincronización por archivos (portable, sin IPC nativo):
    ``<sentinel>/a_listo``       → A tiene el lock (escrito por A)
    ``<sentinel>/b_en_acquire``  → B está entrando al gate del acquire de
                                   ``ensure_loot`` (escrito por B inmediatamente
                                   antes de llamar). Es el HANDSHAKE de contención:
                                   A espera este sentinel para saber que B está a
                                   punto de contener, no "arrancó después".
    ``<sentinel>/a_libera``      → el orquestador dice a A y B que la barrera
                                   terminó (A sostiene; B intenta).
    ``<sentinel>/b_resultado``   → JSON con el resultado de B
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import sys
import time

# Permitir importar sky_claw desde la raíz del repo cuando este helper corre
# como script independiente (el orquestador pytest lo lanza con cwd = repo root).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sky_claw.app.db.locks import DistributedLockManager, LockReleaseError  # noqa: E402
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway  # noqa: E402
from sky_claw.app.security.path_validator import PathValidator  # noqa: E402
from sky_claw.local.tools_installer import ToolsInstaller, _install_lock_resource_id  # noqa: E402


def _esperar_sentinel(sentinel: pathlib.Path, timeout: float = 20.0) -> bool:
    """Espera (polling) a que el archivo sentinel exista. Devuelve True si apareció."""
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if sentinel.exists():
            return True
        time.sleep(0.02)
    return False


def _rol_a(args: argparse.Namespace) -> int:
    """Adquiere el lock y lo sostiene hasta que el orquestador le diga que B espera."""
    install_dir = pathlib.Path(args.install_dir)
    dlm = DistributedLockManager(
        args.db,
        default_ttl=600.0,
        max_retries=1,
        backoff_base=0.01,
        backoff_max=0.01,
    )
    asyncio.run(_rol_a_async(dlm, install_dir, pathlib.Path(args.sentinel_dir)))
    return 0


async def _rol_a_async(
    dlm: DistributedLockManager,
    install_dir: pathlib.Path,
    sentinel_dir: pathlib.Path,
) -> None:
    # H-2: la clave canónica se importa del instalador, no se replica a mano
    # — si la normalización cambiara, A y B usarían filas DISTINTAS y el
    # test fallaría por contrato sin decir por qué.
    #
    # Se calcula ANTES del try: si `resolve()` fallara adentro, el `finally`
    # leeria un local sin asignar y el NameError reemplazaria a la excepcion
    # original (y `suppress(LockReleaseError)` no lo cubre).
    resource_id = _install_lock_resource_id(install_dir)
    await dlm.initialize()
    adquirido = False
    try:
        await dlm.acquire_lock(resource_id, "tools-installer", ttl=600.0)
        adquirido = True
        # A tiene el lock: avisa al orquestador.
        (sentinel_dir / "a_listo").write_text("1", encoding="utf-8")
        # Espera a que el orquestador libere a B para intentar el acquire.
        if not _esperar_sentinel(sentinel_dir / "a_libera", timeout=20.0):
            print("A: timeout esperando a_libera", file=sys.stderr)
            return
        # HANDSHAKE (fix T-1/H-1): A sostiene el lock hasta que B confirme —desde
        # el gate del acquire (b_en_acquire)— que está listo para contener. Sin
        # esto, la contención dependía de que B llegara al acquire dentro de los
        # 0.5 s de hold de A: un B preemptado por el scheduler >0.5 s llegaba con
        # el lock libre y el test fallaba espurio (o pasaba sin demostrar nada).
        # Con el handshake, el intervalo entre el write de B y su llamada al
        # acquire es de microsegundos, no de 0.5 s.
        if not _esperar_sentinel(sentinel_dir / "b_en_acquire", timeout=20.0):
            print("A: timeout esperando b_en_acquire", file=sys.stderr)
            return
        # B está en el acquire (bloqueado): sostiene lo necesario para que el
        # primer intento de B fracase de forma provable (t_seg >= 0.2) antes de
        # liberar en el finally.
        await asyncio.sleep(0.3)
    finally:
        # Solo se libera lo que se llego a adquirir: si `acquire_lock` fallo
        # (A corre con max_retries=1), liberar igual disparaba un
        # LockReleaseError que `suppress` se tragaba, y A salia con rc=0 sin
        # escribir sentinel — el orquestador se quedaba esperando `a_listo`
        # hasta el timeout de 30 s sin ninguna causa declarada.
        if adquirido:
            # release_lock lanza LockReleaseError (no LockAcquisitionError) si el
            # DELETE de la fila falla o no la encuentra — espejo de locks.py:421.
            with contextlib.suppress(LockReleaseError):
                await dlm.release_lock(resource_id, "tools-installer")
        await dlm.close()


def _rol_b(args: argparse.Namespace) -> int:
    """Corre ``ensure_loot`` y verifica que esperó el lock de A."""
    install_dir = pathlib.Path(args.install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    # Pre-crear loot.exe: cuando B gane el lock debe ver already_existed=True
    # SIN red ni HITL. La "instalación" de A es simulada por este archivo.
    (install_dir / "loot.exe").write_text("fake", encoding="utf-8")

    dlm = DistributedLockManager(
        args.db,
        default_ttl=600.0,
        # Espera total suficiente para cubrir los ~0.5 s que A sostiene + margen.
        max_retries=60,
        backoff_base=0.02,
        backoff_max=0.1,
    )
    sentinel_dir = pathlib.Path(args.sentinel_dir)
    try:
        asyncio.run(_rol_b_async(dlm, install_dir, sentinel_dir))
    except BaseException as exc:
        import traceback

        # B murió: sin stderr capturado (DEVNULL) el orquestador no vería la causa.
        (sentinel_dir / "b_error").write_text(
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        return 1
    return 0


async def _rol_b_async(
    dlm: DistributedLockManager,
    install_dir: pathlib.Path,
    sentinel_dir: pathlib.Path,
) -> None:
    from sky_claw.app.security.hitl import Decision

    await dlm.initialize()
    hitl_calls: list[str] = []

    class _Guard:
        """HITLGuard mínimo que registra si se pidió aprobación (no debe pasar)."""

        async def request_approval(
            self,
            *,
            request_id: str,
            reason: str,
            url: str,
            detail: str,
            category: str,
        ) -> Decision:  # pragma: no cover — garantiza que el test falle si llega acá
            hitl_calls.append(request_id)
            return Decision.APPROVED

    validator = PathValidator(roots=[install_dir, pathlib.Path(os.environ.get("TEMP", ".")) / "sky_claw"])
    installer = ToolsInstaller(
        hitl=_Guard(),  # type: ignore[arg-type]
        gateway=NetworkGateway(EgressPolicy(block_private_ips=False)),
        path_validator=validator,
        lock_manager=dlm,
        install_ttl=600.0,
    )

    # ESPERA CRÍTICA: B no llama a ensure_loot hasta que A haya recibido
    # `a_libera`. Sin esto, B puede llegar al acquire DESPUÉS de que A ya
    # liberó y el test mediría "bloqueado" cuando B simplemente arrancó después
    # — que es justo lo que la barrera existe para descartar. Al esperar
    # `a_libera` en B, ambos despiertan juntos y A sigue sosteniendo el lock
    # cuando B entra al acquire.
    if not _esperar_sentinel(sentinel_dir / "a_libera", timeout=20.0):
        raise RuntimeError("B: timeout esperando a_libera del orquestador")

    # HANDSHAKE (fix T-1/H-1): B confirma que está a punto de llamar al acquire.
    # El intervalo entre este write y el primer intento de acquire es de
    # microsegundos — A solo libera DESPUÉS de ver este sentinel, así que la
    # contención queda demostrada de forma provable, sin depender de que B llegue
    # dentro de un hold arbitrario de 0.5 s.
    (sentinel_dir / "b_en_acquire").write_text("1", encoding="utf-8")

    session = object()  # ensure_loot no usa session si loot.exe ya existe
    inicio = time.monotonic()
    # El intento de instalación: si el lock de A no lo bloqueara, esto terminaría
    # en <0.5 s (no hay red ni HITL). Con el lock correcto, queda bloqueado hasta
    # que A libera.
    resultado = await installer.ensure_loot(install_dir, session)  # type: ignore[arg-type]
    fin = time.monotonic()

    # B estaba ejecutando ensure_loot: avisa al orquestador que YA FUE LLAMADO.
    # (Esto se escribe después para que el orquestador pueda medir el bloqueo;
    # el assert del bloqueo real lo hace el resultado por tiempo.)
    payload = {
        "already_existed": resultado.already_existed,
        "hitl_calls": hitl_calls,
        "t_seg": round(fin - inicio, 3),
    }
    (sentinel_dir / "b_resultado").write_text(json.dumps(payload), encoding="utf-8")
    await dlm.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("A", "B"), required=True)
    parser.add_argument("--db", required=True, type=str)
    parser.add_argument("--install-dir", required=True, type=str)
    parser.add_argument("--sentinel-dir", required=True, type=str)
    args = parser.parse_args()

    sentinel_dir = pathlib.Path(args.sentinel_dir)
    sentinel_dir.mkdir(parents=True, exist_ok=True)

    if args.role == "A":
        return _rol_a(args)
    return _rol_b(args)


if __name__ == "__main__":
    raise SystemExit(main())
