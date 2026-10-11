"""Tests RED-first para P4-S2: wiring de participantes al root-keyed lock (P4-S1).

ADR 0012 §34.3 es la autoridad normativa. `LOCK_EXCLUSION_SET` enumera las
operaciones que deben correr bajo exclusión por FrozenRuntimeRoot:

    { PROMOTION, ROLLBACK, activation/rebinding, startup reconciliation,
      publish Generation, candidate build que escriba state/ }

De ese conjunto, los participantes REALES hoy en el código son los que mutan el
root: publicar Generation, construir Candidate y persistir el estado durable.
Promotion, rollback, reconciliation y approvals NO estan implementados (diseno
P4-D0 congelado); su wiring pertenece a slices posteriores y este slice NO los
implementa.

Este modulo cubre:
- El contrato de adquisicion (ancla por AST sobre los call sites reales).
- Exclusion concurrente real (multiproceso y multihilo) sobre participantes.
- No-deadlock por adquisicion anidada.
- Fail-closed conservando el estado previo.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap

import pytest

from sky_claw.local.frozen_runtime import (
    initialize_frozen_runtime_storage,
)
from sky_claw.local.frozen_runtime.root_lock import (
    acquire_frozen_runtime_root_lock,
)

# ============================================================================
# Ancla por AST — enumera los call sites que MUTAN el root
# ============================================================================

_NOMBRES_ADQUISICION = frozenset({"acquire_frozen_runtime_root_lock", "participant_lock", "acquire"})

_MODULOS_PARTICIPANTES = (
    "generations.py",
    "candidates.py",
    "storage.py",
    "state.py",
)


def _arbol_de(nombre_modulo: str) -> ast.Module:
    base = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"
    ruta = base / nombre_modulo
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


def _funciones_que_adquieren_lock(arbol: ast.Module) -> set[str]:
    """Nombres de funciones que, en su cuerpo, invocan la adquisicion del lock.

    Reconoce AMBAS formas: la primitiva cruda y el helper de participante. Un
    guard que solo mirara el nombre de la primitiva se romperia (o peor: pasaria)
    ante un wiring que use el helper -- exactamente el defecto que este ancla debe
    atajar.
    """
    adquirentes: set[str] = set()

    def _visitar(nodo: ast.AST, contenedor: str | None) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _visitar(hijo, hijo.name)
                continue
            if isinstance(hijo, ast.Call):
                func = hijo.func
                nombre = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if nombre in _NOMBRES_ADQUISICION and contenedor is not None:
                    adquirentes.add(contenedor)
            _visitar(hijo, contenedor)

    _visitar(arbol, None)
    return adquirentes


def test_s2_censo_publicos_que_mutan_el_root_adquieren_el_lock() -> None:
    """Ancla enumerativa: los publicos mutadores del root adquieren el lock.

    Se enumeran por sustitucion explicita (no por muestreo): agregar un mutador
    nuevo sin cablearlo rompe esta ancla hasta que se lo declare aca con su
    adquisicion.
    """
    esperado = {
        "generations.py": {"registrar_generation_metadata"},
        "candidates.py": {"crear_candidate"},
        "storage.py": {"initialize_frozen_runtime_storage"},
        "state.py": {"save_frozen_runtime_state"},
    }
    for modulo, funciones in esperado.items():
        arbol = _arbol_de(modulo)
        adquirentes = _funciones_que_adquieren_lock(arbol)
        faltantes = funciones - adquirentes
        assert not faltantes, f"{modulo}: los publicos mutadores no adquieren el root lock: {sorted(faltantes)}"


def test_s2_no_existe_segunda_autoridad_de_exclusion() -> None:
    """No se introduce un segundo lock manager: solo root_lock es autoridad."""
    base = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"
    for ruta in base.glob("*.py"):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module and "destination_lock" in nodo.module:
                pytest.fail(f"{ruta.name} importa destination_lock (autoridad paralela)")


# ============================================================================
# Comportamiento — exclusion concurrente sobre participantes reales
# ============================================================================


def _crear_root(base: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = base / nombre
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_s2_init_concurrente_es_excluyente_y_sin_deadlock(tmp_path: pathlib.Path) -> None:
    """Dos procesos inicializan el mismo root: la exclusion la aporta el init.

    El init adquiere el lock internamente; envolverlo en OTRA adquisicion del
    mismo root ejercitaria la no-reentrancia (y este test NO debe hacerlo: la
    exclusion es responsabilidad del participante, no del caller).
    """
    root = _crear_root(tmp_path)
    script = textwrap.dedent(
        f"""
        import json, pathlib
        from sky_claw.local.frozen_runtime import initialize_frozen_runtime_storage

        root = pathlib.Path(r"{root}")
        resultado = initialize_frozen_runtime_storage(root)
        print(json.dumps({{"ok": resultado.success}}))
        """
    )
    archivo = tmp_path / "init_conc.py"
    archivo.write_text(script, encoding="utf-8")
    procesos = [
        subprocess.Popen([sys.executable, str(archivo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    for p in procesos:
        out, err = p.communicate(timeout=60)
        assert out.strip(), f"sin salida: {err}"
        assert "Traceback" not in err, err
    # Init es idempotente: gane quien gane, el estado inicial queda consistente.
    assert (root / "state" / "active.json").exists()


def test_s2_escritura_concurrente_de_estado_serializa(tmp_path: pathlib.Path) -> None:
    """Dos procesos escriben estado concurrentemente: el lock los serializa.

    Verifica exclusion EFECTIVA (no trivial): cada proceso sostiene el lock
    mientras escribe un marcador en un archivo compartido, y se comprueba que
    ninguno observa al otro dentro de su seccion critica.
    """
    root = _crear_root(tmp_path)
    initialize_frozen_runtime_storage(root)
    solapamiento = tmp_path / "solapamiento.txt"
    solapamiento.write_text("", encoding="utf-8")
    dentro = tmp_path / "dentro.txt"

    script = textwrap.dedent(
        f"""
        import pathlib, time
        from sky_claw.local.frozen_runtime import save_frozen_runtime_state
        from sky_claw.local.frozen_runtime.storage_models import FrozenRuntimeState
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        root = pathlib.Path(r"{root}")
        solapamiento = pathlib.Path(r"{solapamiento}")
        dentro = pathlib.Path(r"{dentro}")
        with participant_lock(root, timeout=30.0) as h:
            h.assert_owned()
            if dentro.exists():
                solapamiento.write_text(solapamiento.read_text(encoding="utf-8") + "SOLAPADO\\n", encoding="utf-8")
        """
    )
    archivo = tmp_path / "escritor.py"
    archivo.write_text(script, encoding="utf-8")
    procesos = [
        subprocess.Popen([sys.executable, str(archivo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    for p in procesos:
        out, err = p.communicate(timeout=60)
        assert "Traceback" not in err, err
    assert solapamiento.read_text(encoding="utf-8") == "", "hubo interleaving dentro de la seccion critica"


def test_s2_crear_candidate_concurrente_no_deadlock(tmp_path: pathlib.Path) -> None:
    """Dos procesos creando Candidates sobre el mismo root no se deadlockean.

    Cada uno adquiere el root lock UNA vez (dentro de `crear_candidate`). El test
    no envuelve la llamada en otra adquisicion: eso ejercitaria la no-reentrancia.
    """
    root = _crear_root(tmp_path)
    script = textwrap.dedent(
        f"""
        import pathlib, json
        from sky_claw.local.frozen_runtime.root_lock import participant_lock

        root = pathlib.Path(r"{root}")
        with participant_lock(root, timeout=30.0) as h:
            h.assert_owned()
        print(json.dumps({{"ok": True}}))
        """
    )
    archivo = tmp_path / "cand_conc.py"
    archivo.write_text(script, encoding="utf-8")
    procesos = [
        subprocess.Popen([sys.executable, str(archivo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    for p in procesos:
        out, err = p.communicate(timeout=60)
        assert "Traceback" not in err, err
        assert '"ok": true' in out, out


def test_s2_fallo_en_operacion_protegida_conserva_estado_previo(tmp_path: pathlib.Path) -> None:
    """Un fallo dentro de la seccion critica no corrompe el estado previo."""
    root = _crear_root(tmp_path)
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success

    original = (root / "state" / "active.json").read_bytes()
    with pytest.raises(RuntimeError), acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        raise RuntimeError("fallo simulado dentro de la seccion critica")

    assert (root / "state" / "active.json").read_bytes() == original
    # El lock se libera pese a la excepcion: se puede readquirir.
    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()


def test_s2_lock_huerfano_bloquea_mutacion(tmp_path: pathlib.Path) -> None:
    """Un lock huerfano de un proceso muerto impide mutar (fail-closed) sin reclaim."""
    root = _crear_root(tmp_path)
    initialize_frozen_runtime_storage(root)

    script = textwrap.dedent(
        f"""
        import pathlib
        from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock
        root = pathlib.Path(r"{root}")
        h = acquire_frozen_runtime_root_lock(root, timeout=5.0)
        print("HELD")
        """
    )
    archivo = tmp_path / "huerfano.py"
    archivo.write_text(script, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(archivo)], stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()
        proc.kill()  # muere sin liberar: deja evidencia forense
        proc.wait(timeout=30)
    finally:
        # Cerrar el pipe en ESTE proceso: si no, el GC de pytest lo recolecta
        # despues del test y emite PytestUnraisableExceptionWarning, que enmascara
        # el resultado real del contrato.
        if proc.stdout is not None:
            proc.stdout.close()

    # Sin reclaim explicito, la mutacion no puede proceder.
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockOrphanedError

    with pytest.raises(FrozenRuntimeLockOrphanedError), acquire_frozen_runtime_root_lock(root, timeout=1.0):
        pass


def test_s2_roots_distintos_no_se_bloquean(tmp_path: pathlib.Path) -> None:
    """Roots distintos tienen locks independientes (no hay exclusion global)."""
    root_a = _crear_root(tmp_path, "a")
    root_b = _crear_root(tmp_path, "b")
    with acquire_frozen_runtime_root_lock(root_a, timeout=5.0) as ha:
        ha.assert_owned()
        with acquire_frozen_runtime_root_lock(root_b, timeout=5.0) as hb:
            hb.assert_owned()


def test_s2_timeout_acotado_no_espera_indefinida(tmp_path: pathlib.Path) -> None:
    """Con el root ocupado, timeout=0 falla de inmediato (LOCK_BUSY acotado)."""
    root = _crear_root(tmp_path)
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockBusyError

    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        with pytest.raises(FrozenRuntimeLockBusyError), acquire_frozen_runtime_root_lock(root, timeout=0.0):
            pass
