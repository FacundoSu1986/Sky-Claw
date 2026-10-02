"""PROBE TEMPORAL S4-E0 / R2-§7 — semántica y resolución de st_ctime_ns en Windows.

Responde las preguntas que el review usa para proponer `st_ctime_ns` como fix:
  - ¿qué significa st_ctime en CPython/Windows?
  - ¿qué resolución efectiva tiene?
  - ¿detecta el race real (unlink+recreate sub-milisegundo)?
  - ¿es evidencia o autoridad?
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import uuid


def main() -> None:
    base = pathlib.Path(tempfile.gettempdir()) / f"CTIMEPROBE-{uuid.uuid4()}"
    base.mkdir(parents=True, exist_ok=False)
    out: dict[str, object] = {"plataforma": sys.platform, "python": sys.version.split()[0]}

    # 1. Resolución observada de st_ctime_ns creando archivos seguidos.
    muestras = []
    anterior = None
    for _ in range(200):
        p = base / f"c{uuid.uuid4()}"
        p.write_bytes(b"x")
        c = int(os.lstat(p).st_ctime_ns)
        muestras.append(c)
        if anterior is not None:
            pass
        anterior = c
        p.unlink()
    distintos = len(set(muestras))
    out["st_ctime_ns_distintos_entre_200_archivos_creados_y_borrados"] = distintos
    if len(muestras) > 1:
        deltas = [b - a for a, b in zip(muestras, muestras[1:], strict=False) if b > a]
        out["delta_minimo_observado_ns"] = min(deltas) if deltas else 0

    # 2. ¿st_ctime cambia tras unlink+recreate INMEDIATO (el race real)?
    victim = base / "victim.txt"
    victim.write_bytes(b"A" * 4096)
    pre = os.lstat(victim)
    c_pre = int(pre.st_ctime_ns)
    victim.unlink()
    victim.write_bytes(b"B" * 4096)
    c_post = int(os.lstat(victim).st_ctime_ns)
    out["race_inmediato_ctime_cambio"] = c_post != c_pre
    out["race_inmediato_st_ino_cambio"] = int(os.lstat(victim).st_ino) != int(pre.st_ino)

    # 3. ¿Y con un delay > 20 ms (fuera de la ventana del race)?
    time.sleep(0.05)
    pre2 = os.lstat(victim)
    c2_pre = int(pre2.st_ctime_ns)
    victim.unlink()
    victim.write_bytes(b"C" * 4096)
    c2_post = int(os.lstat(victim).st_ctime_ns)
    out["race_con_50ms_ctime_cambio"] = c2_post != c2_pre

    # 4. ¿st_ctime es CREATION time (no change time)? => escribir no lo cambia.
    c3_a = int(os.lstat(victim).st_ctime_ns)
    with open(victim, "r+b") as fh:  # noqa: PTH123
        fh.write(b"D" * 4096)
    c3_b = int(os.lstat(victim).st_ctime_ns)
    out["st_ctime_cambia_al_escribir_mismo_inode"] = c3_b != c3_a
    out["st_mtime_cambia_al_escribir_mismo_inode"] = int(os.lstat(victim).st_mtime_ns) != int(pre2.st_mtime_ns)
    out["interpretacion"] = (
        "st_ctime == creation time en Windows CPython: escribir NO lo cambia "
        "=> NO puede ser evidencia de mutación, sólo de creación."
        if not out["st_ctime_cambia_al_escribir_mismo_inode"]
        else "st_ctime parece change-time: distinguir con una relectura de la doc de CPython."
    )

    # 5. os.utime NO controla ctime en Windows (no hay API para fijarlo).
    p5 = base / "u.txt"
    p5.write_bytes(b"x")
    c5 = int(os.lstat(p5).st_ctime_ns)
    try:
        os.utime(p5, ns=(c5, 0))
        out["os_utime_puede_fijar_st_ctime"] = int(os.lstat(p5).st_ctime_ns) == c5
    except OSError as exc:
        out["os_utime_puede_fijar_st_ctime"] = f"OSError: {exc}"

    shutil.rmtree(base, ignore_errors=True)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
