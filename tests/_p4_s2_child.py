"""Script hijo para los tests de concurrencia real de P4-S2.

Vive en su PROPIO modulo (no como string embebido en el test) por una razon
concreta: el test debe poder demostrar que el hijo ejecuta la funcion de
PARTICIPANTE real (`crear_candidate`, `save_frozen_runtime_state`,
`initialize_frozen_runtime_storage`) y no una reimplementacion del lock. Un
script en texto dentro del test es facil de degradar a un `participant_lock()`
pelado sin que nadie lo note — que es exactamente el defecto F01/F02 de la
auditoria.

Se invoca como ``python _p4_s2_child.py <modo> <root> <json-args>`` y escribe
UNA linea JSON a stdout con el resultado tipado del participante.

Aislamiento de imports: el test exporta ``PYTHONPATH`` apuntando al worktree
P4-S2 y ``SKYCLAW_P4_S2_WORKTREE`` para que este proceso cargue el codigo del
worktree y no el editable-install del repo principal.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time


def _patch_identidad() -> None:
    """Identidad PE sintetica y estable: el exe del rig no es un PE real.

    Se parchea en LOS DOS modulos que importan la primitiva (mismo criterio que
    ``tests/_p3_rig.py``): ``observation`` y ``membership``.
    """
    from sky_claw.local.frozen_runtime import membership as membership_module
    from sky_claw.local.frozen_runtime import observation as observation_module
    from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

    def identidad(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version="1.6.1170.0",
            observed_exe_path=str(pathlib.Path(root) / "SkyrimSE.exe"),
            observed_at_ns=time.time_ns(),
        )

    observation_module.observe_runtime_identity_from_root = identidad
    membership_module.observe_runtime_identity_from_root = identidad


def _construir_source(steamapps: pathlib.Path):
    from sky_claw.local.frozen_runtime.models import ManagedSource, ManagedSourceProvider

    common = steamapps / "common" / "Skyrim Special Edition"
    return ManagedSource(
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        appid="489830",
        root=common,
        library_steamapps=steamapps,
        library_root=steamapps.parent,
    )


def _modo_candidate(root: pathlib.Path, args: dict) -> dict:
    """Ejecuta `crear_candidate` REAL sobre el root compartido."""
    from sky_claw.local.frozen_runtime.candidates import crear_candidate

    _patch_identidad()
    source = _construir_source(pathlib.Path(args["steamapps"]))
    cid = args["candidate_id"]

    def fabrica() -> str:
        return cid

    resultado = crear_candidate(
        source,
        root,
        quiet_window_seconds=0.0,
        sleep=lambda _s: None,
        id_factory=fabrica,
    )
    return {
        "candidate_id": resultado.candidate_id,
        "state": resultado.state.value,
        "message": resultado.message,
        "pid": __import__("os").getpid(),
    }


def _modo_estado(root: pathlib.Path, args: dict) -> dict:
    """Ejecuta `save_frozen_runtime_state` REAL bajo contencion controlada.

    El participante adquiere el lock internamente (NO se envuelve aca: envolverlo
    seria la reentrada artificial que la auditoria senala en F02). El marcador y
    la bandera de "estoy dentro" se escriben DENTRO de la llamada real: se
    intercepta `os.replace`, que es el COMMIT de la escritura atomica y por lo
    tanto corre con el lock ya tomado por el participante. Asi el test observa el
    interior de la seccion critica del PARTICIPANTE, no una seccion critica
    inventada por el test.

    Handshake determinista (reemplaza los `sleep` arbitrarios): dentro de la
    seccion critica el hijo

    1. crea ``dentro`` (prueba de que REALMENTE tiene el lock), y
    2. si ``mantener_hasta`` esta definido, ESPERA activamente a que el test cree
       ese archivo antes de commitear.

    El test espera a ver ``dentro`` y solo ENTONCES lanza al competidor: la
    exclusion se mide sobre un lock efectivamente retenido, no sobre una carrera
    de arranque de procesos (que era el defecto del diseno previo: A podia perder
    el lock frente a B por orden de scheduling y el test lo reportaba como fallo).
    """
    import os

    from sky_claw.local.frozen_runtime import state as state_module
    from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState

    solapamiento = pathlib.Path(args["solapamiento"])
    dentro = pathlib.Path(args["dentro"])
    mantener_hasta = args.get("mantener_hasta")
    manter = pathlib.Path(mantener_hasta) if mantener_hasta else None
    retener = float(args.get("retener_segundos", 0.0))

    original_replace = os.replace

    def _envuelto(origen, destino, *resto, **kw):
        # Estamos DENTRO de la seccion critica: el lock ya lo tomo el participante.
        if args.get("marcar_solapamiento", True):
            solapamiento.write_text(
                solapamiento.read_text(encoding="utf-8") + f"SOLAPADO:{os.getpid()}\n",
                encoding="utf-8",
            )
        dentro.write_text(str(os.getpid()), encoding="utf-8")
        if manter is not None:
            # Espera ACTIVA acotada: el test libera creando `manter` una vez que
            # comprobo que el competidor fallo cerrado.
            limite = time.monotonic() + 60.0
            while not manter.exists() and time.monotonic() < limite:
                time.sleep(0.02)
        if retener:
            time.sleep(retener)
        return original_replace(origen, destino, *resto, **kw)

    # `state.py` y el modulo `os` son el MISMO objeto global: parchear `os.replace`
    # alcanza al participante, que lo resuelve por atributo en tiempo de llamada.
    state_module.os.replace = _envuelto

    active = root / "state" / "active.json"
    estado = FrozenRuntimeState(
        schema_version=1,
        desired_active_generation=args.get("desired"),
        updated_at_ns=int(args.get("updated_at_ns", 0)),
    )
    save = state_module.save_frozen_runtime_state
    save(active, estado)
    return {"ok": True, "pid": os.getpid(), "escrito": str(active)}


def _modo_init(root: pathlib.Path, args: dict) -> dict:
    """Ejecuta `initialize_frozen_runtime_storage` REAL."""
    from sky_claw.local.frozen_runtime import initialize_frozen_runtime_storage

    resultado = initialize_frozen_runtime_storage(root)
    return {
        "success": bool(resultado.success),
        "admission_state": resultado.admission.state.value,
        "created_dirs": [str(p) for p in resultado.created_dirs],
        "pid": __import__("os").getpid(),
    }


_MODOS = {"candidate": _modo_candidate, "estado": _modo_estado, "init": _modo_init}


def main() -> int:
    modo = sys.argv[1]
    root = pathlib.Path(sys.argv[2])
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    resultado = _MODOS[modo](root, args)
    sys.stdout.write(json.dumps(resultado) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
