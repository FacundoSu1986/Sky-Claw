"""RIG Windows GP2-S4C: crash y recovery con PROCESOS REALES (§24/§37).

Cada escenario lanza el worker ``tests.s4c_crash_worker.py`` como PROCESO HIJO
real, lo deja morir con ``os._exit`` (nunca una excepción), y ejecuta la
recuperación en un PROCESO NUEVO que sólo ve evidencia durable en disco
(journal real con ``FlushFileBuffers`` verificado, lock real con
``CreateFileW`` share=0, árbol NTFS descartable).

DISCIPLINA DE ENTORNO (§27/§29):

- Todo vive bajo ``%TEMP%\\SkyClaw-S4C-RIG-<uuid>``; el worker aborta si la
  raíz huele a instalación real. NUNCA se toca el Golden del usuario.
- El controller NO se eleva (jamás toca UAC): los escenarios W02/W04 requieren
  poder restaurar nodos YA endurecidos (la Target DACL no concede WRITE_DAC al
  owner) y hacen un PROBE de elevación (``TokenElevation``); sin elevación se
  SKIPEAN con motivo explícito — jamás se declaran ejecutados.
- Este RIG demuestra CRASH DE PROCESO REAL (PROCESS_CRASH_REAL). NO demuestra
  POWER LOSS / REBOOT FÍSICO: eso exige el checkpoint manual separado.

La creación del journal usa ACL de usuario (namespace descartable del rig) para
que los escenarios no-elevados sean ejecutables; las escrituras posteriores
(MUTATING/MUTATED/transiciones y el rollback) pasan SIEMPRE por las primitivas
productivas reales y el drain de durabilidad real.
"""

from __future__ import annotations

import ctypes
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorized_plan import deserialize_authorized_plan
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockPhase,
    derive_golden_lock_path,
    deserialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.protection_journal import NodeWalState, parse_journal_bytes
from sky_claw.local.runtime_vault.protection_journal_store import derive_protection_journal_path
from sky_claw.local.runtime_vault.target_dacl import TargetDaclVerificationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKER_MODULE = "tests.s4c_crash_worker"

_OPERACION = "a4d1f0c2-6b3e-4a58-9d47-2e8f1c0b9a76"

_CODIGO_CRASH_BEFORE_SDSI = 0x51
_CODIGO_CRASH_AFTER_SDSI = 0x52
_CODIGO_CRASH_ALL_MUTATED = 0x53
_CODIGO_CRASH_RESTORED_1 = 0x54

_SKIP_POR_PLATAFORMA = pytest.mark.skipif(
    sys.platform != "win32", reason="RIG nativo Win32 (procesos reales + NTFS) sólo en Windows"
)


# ============================================================================
# Helpers del controller
# ============================================================================


def _rig_root() -> pathlib.Path:
    base = pathlib.Path(tempfile.gettempdir()) / f"SkyClaw-S4C-RIG-{uuid.uuid4().hex}"
    base.mkdir(parents=True)
    return base


def _resolver(rig: pathlib.Path) -> Any:
    return lambda: rig / "programdata"


def _limpiar(rig: pathlib.Path) -> None:
    # Sobre corridas NO elevadas un nodo endurecido puede resistir el borrado
    # (el owner pierde WRITE_DAC por diseño). Mismo comportamiento que el RIG
    # de S4-B: limpieza best-effort y nada más.
    shutil.rmtree(rig, ignore_errors=True)


def _correr_worker(rig: pathlib.Path, fase: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            _WORKER_MODULE,
            "--rig-root",
            str(rig),
            "--operation-id",
            _OPERACION,
            "--phase",
            fase,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _lanzar_worker(rig: pathlib.Path, fase: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            _WORKER_MODULE,
            "--rig-root",
            str(rig),
            "--operation-id",
            _OPERACION,
            "--phase",
            fase,
        ],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _matar_arbol(proc: subprocess.Popen[str]) -> None:
    """TerminateProcess REAL del árbol del worker (nunca del controller).

    En Windows ``.venv\\Scripts\\python.exe`` es un launcher que ejecuta el
    intérprete real como HIJO: matar sólo el launcher dejaría vivo al worker.
    ``taskkill /T /F`` mata el árbol completo.
    """
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
        )
    proc.wait(timeout=30)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _breadcrumbs(rig: pathlib.Path) -> list[str]:
    ruta = rig / "breadcrumbs.log"
    if not ruta.exists():
        return []
    return [linea.strip() for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip()]


def _eventos(rig: pathlib.Path) -> list[str]:
    return [linea.split(" ", 1)[1] for linea in _breadcrumbs(rig) if " " in linea]


def _pid_de_evento(rig: pathlib.Path, evento_exacto: str) -> int:
    """PID (según el propio breadcrumb) del proceso que emitió el evento."""
    for linea in _breadcrumbs(rig):
        pid, evento = linea.split(" ", 1)
        if evento == evento_exacto:
            return int(pid)
    raise AssertionError(f"no se encontró el evento '{evento_exacto}' en {_breadcrumbs(rig)}")


def _tomar_reporte(rig: pathlib.Path) -> dict[str, Any]:
    ruta = rig / "report.json"
    assert ruta.exists(), f"el worker de recovery no escribió report.json (breadcrumbs={_eventos(rig)})"
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    ruta.unlink()
    return datos


def _plan_en_disco(rig: pathlib.Path) -> Any:
    from sky_claw.local.runtime_vault.authorized_plan_store import derive_authorized_plan_path

    ruta = derive_authorized_plan_path(_OPERACION, programdata_resolver=_resolver(rig))
    return deserialize_authorized_plan(ruta.read_bytes())


def _journal_path(rig: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(derive_protection_journal_path(_OPERACION, programdata_resolver=_resolver(rig)))


def _journal_vivo(rig: pathlib.Path) -> Any:
    resultado = parse_journal_bytes(_journal_path(rig).read_bytes())
    assert resultado.journal is not None, resultado.detail
    return resultado.journal


def _estado_journal(rig: pathlib.Path) -> str:
    return _journal_vivo(rig).transaction_state.value


def _nodos_en_estado(rig: pathlib.Path, estado: NodeWalState) -> set[str]:
    return {registro.relative_path for registro in _journal_vivo(rig).nodes_in_state(estado)}


def _lock_path(rig: pathlib.Path) -> pathlib.Path:
    plan = _plan_en_disco(rig)
    return pathlib.Path(
        derive_golden_lock_path(plan.volume_serial_number, plan.root_file_id, programdata_resolver=_resolver(rig))
    )


def _estado_lock(rig: pathlib.Path) -> tuple[int, str]:
    metadata = deserialize_golden_lock_metadata(_lock_path(rig).read_bytes())
    return metadata.owner_pid, metadata.phase


def _es_elevado() -> bool:
    """Probe READ-ONLY de elevación del proceso (nunca toca UAC)."""
    if sys.platform != "win32":
        return False
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    class _TokenElevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return False
    try:
        elevacion = _TokenElevation()
        devuelto = wintypes.DWORD(0)
        ok = advapi32.GetTokenInformation(
            token,
            20,  # TokenElevation
            ctypes.byref(elevacion),
            ctypes.sizeof(elevacion),
            ctypes.byref(devuelto),
        )
        return bool(ok) and bool(elevacion.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def _skip_sin_elevacion(escenario: str) -> None:
    pytest.skip(
        f"{escenario}: restaurar un nodo YA endurecido exige un contexto elevado "
        "(la Target DACL reserve WRITE_DAC a Administrators/SYSTEM) — probe TokenElevation=False. "
        "CI windows-latest (elevado) lo ejecuta; corrida local no elevada NO cuenta como ejecución."
    )


def _verificar_sd_igual_a_pre(plan: Any, relative_path: str) -> None:
    """Verificación SEMÁNTICA real del nodo contra el PRE autorizado (§22)."""
    from sky_claw.local.runtime_vault.mutation_executor import derive_node_path
    from sky_claw.local.runtime_vault.target_dacl import (
        close_security_handle,
        open_node_security_handle,
        verify_restored_security_descriptor_by_handle,
    )

    nodo = plan.node_for(relative_path)
    assert nodo is not None
    handle = open_node_security_handle(derive_node_path(plan, relative_path))
    try:
        verify_restored_security_descriptor_by_handle(handle, nodo)
    finally:
        close_security_handle(handle)


def _sha_sd_live(plan: Any, relative_path: str) -> str:
    from sky_claw.local.runtime_vault.mutation_executor import derive_node_path
    from sky_claw.local.runtime_vault.target_dacl import (
        close_security_handle,
        open_node_security_handle,
        read_live_pre_sd_sha256_by_handle,
    )

    handle = open_node_security_handle(derive_node_path(plan, relative_path))
    try:
        return read_live_pre_sd_sha256_by_handle(handle)
    finally:
        close_security_handle(handle)


# ============================================================================
# Guardas del RIG
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestGuardasDelRig:
    def test_la_raiz_del_rig_es_siempre_temp_y_descartable(self) -> None:
        rig = _rig_root()
        try:
            assert str(rig).lower().startswith(tempfile.gettempdir().lower())
            for prohibido in ("skyrim", "steam", "mod organizer", "mo2", "program files", "programdata"):
                assert prohibido not in str(rig).lower()
        finally:
            _limpiar(rig)

    def test_el_worker_aborta_fuera_de_temp(self) -> None:
        """La barrera dura del worker: raíz fuera de %TEMP% => aborta sin tocar nada."""
        fuera = _REPO_ROOT / "SkyClaw-S4C-RIG-no-debe-crearse"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                _WORKER_MODULE,
                "--rig-root",
                str(fuera),
                "--operation-id",
                _OPERACION,
                "--phase",
                "recover",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode != 0
        assert not fuera.exists(), "el worker no debe crear nada fuera de %TEMP%"

    def test_el_worker_no_es_invocable_desde_codigo_productivo(self) -> None:
        """El worker es TEST-ONLY: ningún módulo de sky_claw/ lo referencia."""
        encontrados: list[str] = []
        for ruta in (_REPO_ROOT / "sky_claw").rglob("*.py"):
            if "s4c_crash_worker" in ruta.read_text(encoding="utf-8"):
                encontrados.append(str(ruta))
        assert encontrados == [], f"referencias productivas al worker de crash: {encontrados}"


# ============================================================================
# W01 — crash tras MUTATING durable, antes de SetSecurityInfo
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW01CrashAntesDeSetSecurityInfo:
    def test_w01_recovery_reconoce_nodo_no_mutado_y_cierra_rolled_back(self, tmp_path: pathlib.Path) -> None:
        rig = _rig_root()
        try:
            proc = _correr_worker(rig, "w01_crash_before_sdsi")
            assert proc.returncode == _CODIGO_CRASH_BEFORE_SDSI, proc.stderr
            eventos = _eventos(rig)
            assert any(e.startswith("pre-sdsi:Data/Skyrim.esm") for e in eventos), eventos
            assert not any(e.startswith("sdsi:") for e in eventos), (
                "el crash debe ocurrir ANTES del primer SetSecurityInfo"
            )
            # Evidencia durable DIRECTA desde el disco: MUTATING(K) sin MUTATED(K).
            assert _nodos_en_estado(rig, NodeWalState.MUTATING) == {"Data/Skyrim.esm"}
            assert _nodos_en_estado(rig, NodeWalState.MUTATED) == set()

            plan = _plan_en_disco(rig)
            _verificar_sd_igual_a_pre(plan, "Data/Skyrim.esm")  # nunca mutado: sigue en PRE

            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "rolled_back"
            assert reporte["nodes_restored"] == [], "un nodo sin SetSecurityInfo no debe re-escribirse"
            assert "Data/Skyrim.esm" in reporte["nodes_skipped"]
            assert reporte["stale_lock_takeover"] is True, "el dueño murió: takeover del lock huérfano"
            assert _estado_journal(rig) == "rolled_back"
            owner_pid, fase_lock = _estado_lock(rig)
            assert fase_lock == GoldenLockPhase.RELEASED.value
            assert owner_pid == _pid_de_evento(rig, "recover-done:rolled_back"), (
                "la metadata RELEASED la escribió el proceso de recovery"
            )
        finally:
            _limpiar(rig)


# ============================================================================
# W02 — EL ORÁCULO CENTRAL: SetSecurityInfo REAL y crash antes de MUTATED
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW02CrashTrasSetSecurityInfo:
    def test_w02_mutating_durable_con_sd_mutado_se_restaura_semanticamente(self, tmp_path: pathlib.Path) -> None:
        if not _es_elevado():
            _skip_sin_elevacion("W02")
        rig = _rig_root()
        try:
            proc = _correr_worker(rig, "w02_crash_after_sdsi")
            assert proc.returncode == _CODIGO_CRASH_AFTER_SDSI, proc.stderr
            eventos = _eventos(rig)
            assert any(e.startswith("sdsi:Data/Skyrim.esm") for e in eventos), (
                "el SetSecurityInfo REAL debe haber corrido antes del crash"
            )
            assert any(e.startswith("post-sdsi:Data/Skyrim.esm") for e in eventos), eventos
            assert not any(e.startswith("mutated:") for e in eventos), (
                "no puede existir MUTATED durable: ese es el oráculo del escenario"
            )
            assert _nodos_en_estado(rig, NodeWalState.MUTATING) == {"Data/Skyrim.esm"}
            assert _nodos_en_estado(rig, NodeWalState.MUTATED) == set()
            plan = _plan_en_disco(rig)
            assert _estado_journal(rig) == "applying"
            # CAUSALIDAD, mitad 1: tras el crash el SD vivo YA NO es el PRE
            # autorizado (SetSecurityInfo REAL aterrizó): la verificación
            # semántica debe fallar.
            with pytest.raises(TargetDaclVerificationError):
                _verificar_sd_igual_a_pre(plan, "Data/Skyrim.esm")
            sha_tras_crash = _sha_sd_live(plan, "Data/Skyrim.esm")

            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "rolled_back"
            assert reporte["nodes_restored"] == ["Data/Skyrim.esm"]
            assert reporte["stale_lock_takeover"] is True
            assert _estado_journal(rig) == "rolled_back"
            _, fase_lock = _estado_lock(rig)
            assert fase_lock == GoldenLockPhase.RELEASED.value

            # CAUSALIDAD, mitad 2: el recovery restauró el PRE autorizado y la
            # verificación semántica real pasa sobre el nodo vivo.
            _verificar_sd_igual_a_pre(plan, "Data/Skyrim.esm")
            sha_restaurado = _sha_sd_live(plan, "Data/Skyrim.esm")
            assert isinstance(sha_tras_crash, str) and isinstance(sha_restaurado, str)
        finally:
            _limpiar(rig)


# ============================================================================
# W03 — apply completo (todos MUTATED) y crash antes de la post-verificación
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW03TodosMutated:
    def test_w03_handoff_a_post_verificacion_sin_tocar_nada(self, tmp_path: pathlib.Path) -> None:
        rig = _rig_root()
        try:
            proc = _correr_worker(rig, "w03_crash_all_mutated")
            assert proc.returncode == _CODIGO_CRASH_ALL_MUTATED, proc.stderr
            eventos = _eventos(rig)
            assert any(e.startswith("apply-complete:applying") for e in eventos), eventos
            assert _nodos_en_estado(rig, NodeWalState.MUTATED) == {"Data/Skyrim.esm", "Data", "."}
            assert _nodos_en_estado(rig, NodeWalState.MUTATING) == set()
            bytes_antes = _journal_path(rig).read_bytes()

            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "post_verification_required", (
                "C5: el apply físico quedó completo; GP1/RV-2/NodeSet son de S4-D"
            )
            assert reporte["nodes_restored"] == [] and reporte["nodes_skipped"] == []
            assert reporte["observed_transaction_state"] == "applying"
            assert _journal_path(rig).read_bytes() == bytes_antes, (
                "S4-C no escribe VERIFYING_GP1 ni ningún registro nuevo en C5"
            )
            _, fase_lock = _estado_lock(rig)
            assert fase_lock == GoldenLockPhase.RELEASED.value
        finally:
            _limpiar(rig)


# ============================================================================
# W04 — DOBLE CRASH: A muta todo, B restaura el primer nodo y muere, C termina
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW04DobleCrash:
    def test_w04_process_c_reanuda_sin_memoria_de_process_b(self, tmp_path: pathlib.Path) -> None:
        if not _es_elevado():
            _skip_sin_elevacion("W04")
        rig = _rig_root()
        try:
            proc_a = _correr_worker(rig, "w03_crash_all_mutated")
            assert proc_a.returncode == _CODIGO_CRASH_ALL_MUTATED, proc_a.stderr

            proc_b = _correr_worker(rig, "recover_crash_restored1")
            assert proc_b.returncode == _CODIGO_CRASH_RESTORED_1, proc_b.stderr
            eventos_b = _eventos(rig)
            assert any(e.startswith("restore:") for e in eventos_b), eventos_b
            assert not any(e.startswith("recover-done:") for e in eventos_b), "B debe morir ANTES de cerrar el recovery"
            primer_restaurado = next(e.split(":", 1)[1] for e in eventos_b if e.startswith("restore:"))
            assert _estado_journal(rig) == "rolling_back", "el rollback de B quedó durable a medias"

            events_tras_b = list(eventos_b)
            proc_c = _correr_worker(rig, "recover")
            assert proc_c.returncode == 0, proc_c.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "rolled_back"
            assert primer_restaurado in reporte["nodes_skipped"], (
                "C no sabe qué restauró B: la sonda semántica debe reconocer el nodo ya restaurado"
            )
            assert primer_restaurado not in reporte["nodes_restored"], (
                "re-escribir un nodo ya restaurado delataría comparación raw de bytes"
            )
            assert _estado_journal(rig) == "rolled_back"
            _, fase_lock = _estado_lock(rig)
            assert fase_lock == GoldenLockPhase.RELEASED.value

            plan = _plan_en_disco(rig)
            for nodo in plan.nodes:
                _verificar_sd_igual_a_pre(plan, nodo.relative_path)
            assert events_tras_b, "B dejó evidencia causal"
        finally:
            _limpiar(rig)


# ============================================================================
# W05/W06 — lock huérfano: dueño vivo => BUSY; dueño muerto => takeover real
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW05W06LockHuerfano:
    def test_w05_w06_dueno_vivo_no_se_roba_dueno_muerto_permite_recovery(self) -> None:
        rig = _rig_root()
        worker_a: subprocess.Popen[str] | None = None
        try:
            worker_a = _lanzar_worker(rig, "w06_hold_lock")
            # Esperar a que A tenga lock + journal + MUTATING durable y duerma.
            limite = 60
            while "hold" not in _eventos(rig):
                if worker_a.poll() is not None:
                    out, err = worker_a.communicate()
                    pytest.fail(f"el worker A murió antes de hold: {out} {err}")
                if limite <= 0:
                    pytest.fail(f"A nunca llegó a hold: {_eventos(rig)}")
                limite -= 1
                time.sleep(0.5)
            assert worker_a.poll() is None, "A debe seguir VIVO con el lock tomado"
            pids_de_a = {int(linea.split(" ", 1)[0]) for linea in _breadcrumbs(rig)}
            assert pids_de_a, "A debe haber dejado evidencia durable (breadcrumbs)"

            # B: recovery con el dueño VIVO => BUSY, sin robar nada. La metadata
            # del lock NO puede leerse mientras A lo mantiene (share=0): el
            # propio resultado BUSY es la prueba de "dueño vivo identificado".
            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte_b = _tomar_reporte(rig)
            assert reporte_b["disposition"] == "lock_busy", reporte_b
            assert _estado_journal(rig) == "applying", "B no puede escribir con el dueño vivo"

            # Matar A con TerminateProcess REAL del ÁRBOL (nunca al controller).
            _matar_arbol(worker_a)

            # C: dueño muerto demostrado por PID+creation-time => takeover y cierre.
            proc_c = _correr_worker(rig, "recover")
            assert proc_c.returncode == 0, proc_c.stderr
            reporte_c = _tomar_reporte(rig)
            assert reporte_c["disposition"] == "rolled_back"
            assert reporte_c["stale_lock_takeover"] is True
            assert _estado_journal(rig) == "rolled_back"
            owner_pid, fase_final = _estado_lock(rig)
            assert fase_final == GoldenLockPhase.RELEASED.value
            assert owner_pid == _pid_de_evento(rig, "recover-done:rolled_back"), (
                "la metadata final la escribió el proceso de recovery C"
            )
            assert owner_pid not in pids_de_a, "el lock huérfano pasó a manos de C, no de A"
        finally:
            if worker_a is not None:
                _matar_arbol(worker_a)
            _limpiar(rig)


# ============================================================================
# W07 — sustitución física del nodo entre crash y recovery
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW07NodoSustituido:
    def test_w07_fileid_drift_es_indeterminate_y_cero_setsecurityinfo(self) -> None:
        rig = _rig_root()
        try:
            proc_a = _correr_worker(rig, "w01_crash_before_sdsi")
            assert proc_a.returncode == _CODIGO_CRASH_BEFORE_SDSI, proc_a.stderr

            plan = _plan_en_disco(rig)
            victima = rig / "golden" / "Data" / "Skyrim.esm"
            victima.unlink()
            victima.write_bytes(b"objeto-fisico-distinto")
            sha_antes = _sha_sd_live(plan, "Data/Skyrim.esm")

            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "indeterminate"
            assert reporte["nodes_restored"] == []
            assert reporte["operator_intervention_required"] is True
            assert "Data/Skyrim.esm" in reporte["nodes_pending"]
            # La transición declarada rolling_back -> indeterminate queda DURABLE:
            # la evidencia se preserva como estado explícito, no como applying.
            assert _estado_journal(rig) == "indeterminate"
            assert _sha_sd_live(plan, "Data/Skyrim.esm") == sha_antes, (
                "cero SetSecurityInfo sobre el objeto de reemplazo"
            )
            _, fase_lock = _estado_lock(rig)
            assert fase_lock != GoldenLockPhase.RELEASED.value, "INDETERMINATE retiene el lock para inspección"
        finally:
            _limpiar(rig)


# ============================================================================
# W08 — authorized_plan perdido antes del recovery
# ============================================================================


@_SKIP_POR_PLATAFORMA
class TestW08PlanPerdido:
    def test_w08_sin_plan_no_hay_rollback_automatico(self) -> None:
        rig = _rig_root()
        try:
            proc_a = _correr_worker(rig, "w01_crash_before_sdsi")
            assert proc_a.returncode == _CODIGO_CRASH_BEFORE_SDSI, proc_a.stderr

            from sky_claw.local.runtime_vault.authorized_plan_store import derive_authorized_plan_path

            plan_path = derive_authorized_plan_path(_OPERACION, programdata_resolver=_resolver(rig))
            plan_path.unlink()
            bytes_journal_antes = _journal_path(rig).read_bytes()

            proc_b = _correr_worker(rig, "recover")
            assert proc_b.returncode == 0, proc_b.stderr
            reporte = _tomar_reporte(rig)
            assert reporte["disposition"] == "indeterminate"
            assert reporte["nodes_restored"] == []
            assert reporte["operator_intervention_required"] is True
            assert _journal_path(rig).read_bytes() == bytes_journal_antes, (
                "sin PRE autoritativo no hay rollback ni registros nuevos (C4c)"
            )
        finally:
            _limpiar(rig)
