"""PROBE TEMPORAL S4-E0 / R2 — NO ES PRODUCCIÓN NI TEST DEL REPO.

Mide si la primitiva path-based de `inventory_tree` (RV-1) puede producir un
"seal" falso cuando un atacante reemplaza el archivo por otro de MISMO tamaño y
con el mtime RESTAURADO, dentro de la ventana PRE -> READ -> POST.

Dos modos, ambos sin sleeps diseñados para hacer pasar nada:

  MODO A (causal, determinista): usa el seam de hooks que el propio módulo
  expone (`_HOOK_TRAS_LA_LECTURA`) para hacer el unlink/recreate exactamente
  en el punto de la ventana que importa. No hay carrera: la mutación ocurre
  cuando el módulo ya leyó los bytes y va a hacer el POST.

  MODO B (observacional, N iteraciones): el atacante corre en otro thread y
  hace un bucle cerrado de unlink/recreate sobre el mismo pathname mientras
  `inventory_tree` camina el árbol. Mide si NTFS REUTILIZA el FileId.

Se registra la evidencia física (st_dev, st_ino, st_mtime_ns, st_ctime_ns) y el
veredicto exacto de `inventory_tree`.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sky_claw.local.runtime_vault import inventory as inv  # noqa: E402
from sky_claw.local.runtime_vault.inventory import inventory_tree  # noqa: E402

CONTENT_A = b"A" * 4096
CONTENT_B = b"B" * 4096  # MISMO tamaño, contenido distinto


def _evidencia(p: pathlib.Path) -> dict[str, object]:
    st = os.lstat(p)
    return {
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_size": int(st.st_size),
        "st_mtime_ns": int(st.st_mtime_ns),
        "st_ctime_ns": int(getattr(st, "st_ctime_ns", -1)),
    }


def _volumen() -> str:
    root = pathlib.Path(tempfile.gettempdir()).resolve()
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-Command", "(Get-Volume -DriveLetter C).FileSystem"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        fs = out.stdout.strip()
    except Exception:  # noqa: BLE001
        fs = "DESCONOCIDO"
    return f"root={root} fs_C={fs}"


# ---------------------------------------------------------------- MODO A
def modo_a(iteraciones: int = 50) -> dict[str, object]:
    """Mutación determinista en la ventana POST-lectura."""
    detecciones: list[str] = []
    aceptaciones_falsas: list[dict[str, object]] = []
    ino_reutilizado = 0
    ino_cambiado = 0
    mensajes: dict[str, int] = {}
    ctime_cambia = 0
    mtime_exacto = 0

    for i in range(iteraciones):
        base = pathlib.Path(tempfile.gettempdir()) / f"RV1PROBE-A-{uuid.uuid4()}"
        base.mkdir(parents=True, exist_ok=False)
        victima = base / "victim.txt"
        victima.write_bytes(CONTENT_A)
        pre = _evidencia(victima)
        if int(pre["st_ino"]) == 0:
            print("ABORTA: st_ino == 0 en esta plataforma; el gate de identidad no puede discriminiar")

        estado = {"reemplazado": False}

        def hook(ruta: pathlib.Path, _pre=pre, _estado=estado) -> None:
            nonlocal ctime_cambia, mtime_exacto
            # Exactamente la ventana: el módulo ya leyó CONTENT_A y va a lstat.
            try:
                ruta.unlink()
                ruta.write_bytes(CONTENT_B)
                st = os.lstat(ruta)
                # Restaurar mtime para neutralizar el gate de evidencia.
                os.utime(ruta, ns=(int(_pre["st_ctime_ns"]), int(_pre["st_mtime_ns"])))
                _estado["reemplazado"] = True
                _estado["post"] = _evidencia(ruta)
                if _estado["post"]["st_mtime_ns"] == _pre["st_mtime_ns"]:
                    mtime_exacto += 1
                if _estado["post"]["st_ctime_ns"] != _pre["st_ctime_ns"]:
                    ctime_cambia += 1
            except OSError as exc:  # pragma: no cover - diagnóstico
                _estado["error"] = repr(exc)

        inv._HOOK_TRAS_LA_LECTURA = hook
        try:
            resultado = inventory_tree(base)
            # ACEPTADO. Esto es un sello falso si el digest es el de CONTENT_B.
            import hashlib  # noqa: PLC0415

            identidades = {f.rel_path: f.digest for f in resultado}
            digest = identidades.get("victim.txt")
            digest_b = hashlib.sha256(CONTENT_B).hexdigest()
            digest_a = hashlib.sha256(CONTENT_A).hexdigest()
            post = _estado.get("post") or {}
            ino_post = int(post.get("st_ino", -1))
            if ino_post == int(pre["st_ino"]):
                ino_reutilizado += 1
            elif ino_post > 0:
                ino_cambiado += 1
            if digest == digest_b and digest != digest_a:
                aceptaciones_falsas.append({"iter": i, "pre": pre, "post": post})
            detecciones.append("ACEPTADO")
        except Exception as exc:  # noqa: BLE001
            detecciones.append(type(exc).__name__)
            post = estado.get("post") or {}
            ino_post = int(post.get("st_ino", -1))
            if ino_post == int(pre["st_ino"]):
                ino_reutilizado += 1
            elif ino_post > 0:
                ino_cambiado += 1
            # Clasificar QUÉ gate observó el reemplazo.
            mensajes.setdefault(f"{type(exc).__name__}: {exc}", 0)
            mensajes[f"{type(exc).__name__}: {exc}"] += 1
        finally:
            inv._HOOK_TRAS_LA_LECTURA = None
            shutil.rmtree(base, ignore_errors=True)

    return {
        "modo": "A_causal_determinista",
        "iteraciones": iteraciones,
        "veredictos": sorted(set(detecciones)),
        "conteo_por_veredicto": {v: detecciones.count(v) for v in sorted(set(detecciones))},
        "st_ino_REUTILIZADO": ino_reutilizado,
        "st_ino_CAMBIADO": ino_cambiado,
        "SELLOS_FALSOS_ACEPTADOS": len(aceptaciones_falsas),
        "muestra_de_sellos_falsos": aceptaciones_falsas[:3],
        "mensajes_de_rechazo": dict(list(mensajes.items())[:8]),
        "st_ctime_ns_cambio_observado": ctime_cambia,
        "mtime_restaurado_exacto": mtime_exacto,
    }


# ---------------------------------------------------------------- MODO B
def modo_b(iteraciones: int = 400) -> dict[str, object]:
    """Carrera real: atacante en otro thread, sin sleeps."""
    detecciones: dict[str, int] = {}
    reutilizaciones = 0
    intentos = 0

    for i in range(iteraciones):
        base = pathlib.Path(tempfile.gettempdir()) / f"RV1PROBE-B-{uuid.uuid4()}"
        base.mkdir(parents=True, exist_ok=False)
        victima = base / "victim.txt"
        victima.write_bytes(CONTENT_A)
        pre_ino = int(os.lstat(victima).st_ino)

        stop = threading.Event()

        def atacante() -> None:
            nonlocal reutilizaciones, intentos
            while not stop.is_set():
                intentos += 1
                try:
                    os.unlink(victima)
                    with open(victima, "wb") as fh:  # noqa: PTH123
                        fh.write(CONTENT_B)
                except OSError:
                    pass
                try:
                    if int(os.lstat(victima).st_ino) == pre_ino:
                        reutilizaciones += 1
                except OSError:
                    pass

        t = threading.Thread(target=atacante, daemon=True)
        t.start()
        try:
            inventory_tree(base)
            veredicto = "ACEPTADO"
        except Exception as exc:  # noqa: BLE001
            veredicto = type(exc).__name__
        finally:
            stop.set()
            t.join(timeout=2)
            shutil.rmtree(base, ignore_errors=True)
        detecciones[veredicto] = detecciones.get(veredicto, 0) + 1

    return {
        "modo": "B_observacional_carrera",
        "iteraciones": iteraciones,
        "veredictos": detecciones,
        "intentos_de_reemplazo": intentos,
        "FILEID_REUTILIZADO_contados_por_el_atacante": reutilizaciones,
    }


# ---------------------------------------------------------------- MAIN
def main() -> None:
    print("=" * 78)
    print("PROBE R2 / RV-1 — inventario path-based vs unlink+recreate en NTFS")
    print(_volumen())
    print("=" * 78)

    t0 = time.perf_counter()
    a = modo_a(int(os.environ.get("PROBE_A_ITER", "50")))
    print(json.dumps(a, indent=2, default=str))
    print(f"[modo A] {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    b = modo_b(int(os.environ.get("PROBE_B_ITER", "400")))
    print(json.dumps(b, indent=2, default=str))
    print(f"[modo B] {time.perf_counter() - t0:.1f}s")

    print("=" * 78)
    if a["SELLOS_FALSOS_ACEPTADOS"] > 0:
        print("VEREDICTO: R2_NTFS_REPRO = CONFIRMED")
    elif b.get("FILEID_REUTILIZADO_contados_por_el_atacante", 0) > 0:
        print("VEREDICTO: FILEID_REUTILIZADO_OBSERVADO (el sello no se rompió en esta corrida)")
    else:
        print("VEREDICTO: R2_NTFS_REPRO = NOT_OBSERVED")
    print("=" * 78)


if __name__ == "__main__":
    main()
