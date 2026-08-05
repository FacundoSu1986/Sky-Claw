"""Contención cross-process real del lock de instalación (T-31).

Dos procesos con instancias independientes de ``DistributedLockManager`` sobre
la MISMA SQLite. El primero (A) adquiere el lock y espera en una barrera antes
de mutar; el segundo (B) intenta ``ensure_loot`` y debe quedar BLOQUEADO hasta
la liberación — recién entonces devuelve ``already_existed=True``, sin segunda
descarga ni segundo HITL.

Sin la barrera este test pasaría aunque B simplemente haya arrancado después,
que no demuestra nada. La barrera es un HANDSHAKE de confirmación: A escribe
``a_listo`` cuando tiene el lock; el orquestador suelta ``a_libera``; B —tras
verlo— escribe ``b_en_acquire`` inmediatamente antes de llamar al acquire, y A
solo libera DESPUÉS de ver ese sentinel. Con el intervalo write→acquire en
microsegundos, la contención de B queda demostrada de forma provable (no
estadística). El resultado de B incluye ``t_seg``: con el lock funcionando es
>= al hold comprobable de A (0.3 s); sin lock sería ~0.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

import pytest

_HELPER = pathlib.Path(__file__).parent / "helpers" / "installer_lock_proc.py"
_PYTHON = sys.executable


def _esperar_sentinel(sentinel: pathlib.Path, timeout: float = 30.0) -> bool:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if sentinel.exists():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sentinel_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "sentinels"
    d.mkdir()
    return d


@pytest.fixture
def install_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "modding" / "LOOT"


def _lanzar_rol(role: str, db: pathlib.Path, install_dir: pathlib.Path, sentinels: pathlib.Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            _PYTHON,
            str(_HELPER),
            "--role",
            role,
            "--db",
            str(db),
            "--install-dir",
            str(install_dir),
            "--sentinel-dir",
            str(sentinels),
        ],
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        # DEVNULL: el helper comunica resultados vía sentinel files, no stdout/stderr.
        # Con PIPE los pipes quedan sin leer y el repo convierte el ResourceWarning
        # resultante en error (ver conftest.py — PytestUnraisableExceptionWarning).
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_segundo_llamador_queda_bloqueado_y_ve_already_existed(
    tmp_path: pathlib.Path,
    sentinel_dir: pathlib.Path,
    install_dir: pathlib.Path,
) -> None:
    """El hermano B espera el lock de A y recién entonces ve already_existed=True."""
    db = tmp_path / "locks.db"

    proc_a = _lanzar_rol("A", db, install_dir, sentinel_dir)
    try:
        # A tiene el lock y está esperando a que le digamos que B ya espera.
        assert _esperar_sentinel(sentinel_dir / "a_listo"), "A no adquirió el lock en el tiempo esperado"

        # Lanzar B. La barrera se suelta acá: A empieza a esperar la confirmación
        # de B (b_en_acquire) y B —tras ver a_libera— confirma que está a punto
        # de llamar al acquire. A sostiene el lock hasta VER esa confirmación:
        # la contención de B queda demostrada de forma provable, no estadística.
        proc_b = _lanzar_rol("B", db, install_dir, sentinel_dir)
        try:
            (sentinel_dir / "a_libera").write_text("1", encoding="utf-8")
            assert _esperar_sentinel(sentinel_dir / "b_en_acquire"), "B no confirmó su entrada al acquire"

            # Esperar el resultado de B.
            assert _esperar_sentinel(sentinel_dir / "b_resultado"), "B no reportó resultado"
            payload = json.loads((sentinel_dir / "b_resultado").read_text(encoding="utf-8"))

            assert payload["already_existed"] is True
            assert payload["hitl_calls"] == [], "B no debe pedir HITL si el tool ya está instalado"
            assert payload["t_seg"] >= 0.2, (
                f"B terminó en {payload['t_seg']}s: no estuvo bloqueado por el lock de A "
                "(debería haber esperado al menos lo que A sostuvo)"
            )

            rc_b = proc_b.wait(timeout=30)
            assert rc_b == 0, f"B falló con rc={rc_b}"
        finally:
            if proc_b.poll() is None:
                proc_b.kill()
                proc_b.wait()
    finally:
        # A no debe quedar colgado: si el sentinel no llegó, A ya salió (timeout).
        if (sentinel_dir / "a_libera").exists():
            proc_a.wait(timeout=30)
        elif proc_a.poll() is None:
            proc_a.kill()
            proc_a.wait()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "El invariante es semantica de Windows: `os.path.normcase` solo baja "
        "mayusculas y convierte `/`->`\\\\` ahi. En POSIX es la identidad, las "
        "rutas son case-sensitive y `\\\\` es un caracter valido de nombre, asi "
        "que `C:\\\\Tools\\\\LOOT` y `c:/tools/loot` son dos archivos DISTINTOS y "
        "exigir la misma clave seria incorrecto."
    ),
)
def test_misma_clave_para_grafias_distintas(tmp_path: pathlib.Path) -> None:
    """C:\\Tools\\LOOT y c:\\tools\\loot toman el MISMO lock (casefold + normcase).

    Sin normalizar, dos grafías del mismo directorio toman locks distintos sobre
    el mismo recurso y el lock no protege nada.
    """
    from sky_claw.local.tools_installer import _install_lock_resource_id

    a = pathlib.Path("C:/Tools/LOOT")
    b = pathlib.Path("c:\\tools\\loot")

    assert _install_lock_resource_id(a) == _install_lock_resource_id(b)
    assert _install_lock_resource_id(a).startswith("tools-install:")
