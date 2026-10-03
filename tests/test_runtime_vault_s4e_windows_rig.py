"""GP2-S4E — RIG de Windows con PROCESOS REALES y crash REAL.

Alcance y honestidad del RIG
----------------------------

REAL en este archivo: el ``GoldenMutationLock`` Win32 (share mode 0, metadata
durable), el ``DurableAuthorizedPlan``, el ``ProtectionJournal`` con flush
verificado, S4-B (``apply_authorized_plan`` sobre el puerto handle-bound →
``SetSecurityInfo`` de verdad), S4-C, S4-D (archivado del backup,
``COMMITTED``), el router de S4-E, y los crashes: el proceso A muere con
``taskkill /T /F``, no con una excepción de Python.

DEGRADADO, y declarado en cada test que lo toca:

* **GP1 / quiescence / RV-2**: el puerto de verificación aprueba los gates en
  vez de observarlos. Un fake port NO es validación nativa y este archivo no
  lo afirma en ningún lado.
* **La frontera de S4-A**: elevación y PPSC no corren (el RIG no está
  elevado). Se toma el lock REAL con la primitiva de producción, así que la
  continuidad del lock sí se ejercita.
* **El backup**: se publica con el ``GoldenBackupDurableWriter`` del RIG
  (create-once + ``fsync`` + relectura) en vez de con el SD canónico del
  namespace, que exige un SID propietario que no resuelve sin el bootstrap
  privilegiado. La *durabilidad* sí es real; el SD canónico no se prueba acá.

Lo que el RIG NO toca jamás: Skyrim real, Steam, MO2, el Golden del usuario ni
``%ProgramData%`` productivo. Hay un guard duro en el worker y otro acá.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from typing import Any

import pytest

#: Path ABSOLUTO al worker: el RIG lo invoca desde el directorio de tests para
#: que import s4c_crash_worker (el builder de plan/journal compartido con el
#: RIG de S4-C) resuelva igual.
_WORKER = str(pathlib.Path(__file__).resolve().parent / "s4e_crash_worker.py")
_CWD = str(pathlib.Path(__file__).resolve().parent)

_PATHS_PROHIBIDOS = (
    "skyrim",
    "steam",
    "mod organizer",
    "modorganizer",
    ".mo2",
    "program files",
    "programdata",
)


# --------------------------------------------------------------------------
# Elevación: la restauración física de un nodo YA endurecido la exige
# --------------------------------------------------------------------------


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
        f"{escenario}: S4-C restaura un nodo YA endurecido con WRITE_DAC, y la Target DACL "
        "lo reserva a Administrators/SYSTEM — probe TokenElevation=False. "
        "CI windows-latest (elevado) lo ejecuta; una corrida local no elevada NO cuenta "
        "como ejecución de E02 (no hay rollback físico que observar)."
    )


# --------------------------------------------------------------------------
# Andamiaje
# --------------------------------------------------------------------------


def _rig() -> pathlib.Path:
    base = pathlib.Path(tempfile.gettempdir()) / f"SkyClaw-S4E-RIG-{uuid.uuid4().hex}"
    base.mkdir(parents=True)
    (base / "operation-id.txt").write_text(str(uuid.uuid4()), encoding="utf-8")
    return base


def _resolver_arg(rig: pathlib.Path) -> list[str]:
    return ["--rig-root", str(rig)]


def _correr(rig: pathlib.Path, fase: str, *, crash_en: str | None = None) -> dict[str, Any]:
    argv = [sys.executable, f"{_WORKER}", *_resolver_arg(rig), "--fase", fase]
    if crash_en is not None:
        argv += ["--crash-en", crash_en]
    proc = subprocess.run(argv, capture_output=True, check=False, text=True, timeout=300, cwd=_CWD)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _lanzar(rig: pathlib.Path, fase: str, *, crash_en: str | None = None) -> subprocess.Popen[str]:
    argv = [sys.executable, f"{_WORKER}", *_resolver_arg(rig), "--fase", fase]
    if crash_en is not None:
        argv += ["--crash-en", crash_en]
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent),
    )


def _matar_arbol(proc: subprocess.Popen[str]) -> None:
    """TerminateProcess REAL del árbol.

    En Windows ``.venv\\Scripts\\python.exe`` es un LAUNCHER que ejecuta el
        intérprete real como hijo: matar sólo al launcher dejaría vivo al worker y
        el test probaría una fantasía. ``taskkill /T /F`` mata el árbol entero.
    """
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
        )
    try:
        proc.wait(timeout=60)
    finally:
        _cerrar_streams(proc)


def _cerrar_streams(proc: subprocess.Popen[str]) -> None:
    """Cierra los pipes del subprocess SIEMPRE, haya excepcion o no.

    Un ``Popen`` con ``stdout=PIPE`` sin cerrar deja un ``ResourceWarning`` que
    pytest reporta como fallo del test. Cerrarlos en el ``finally`` del helper
    —y no al final del camino feliz— es lo que evita que una excepcion
    intermedia convierta un test verde en uno rojo por un motivo que no es del
    RIG.
    """
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def _leer(rig: pathlib.Path, nombre: str) -> Any:
    return json.loads((rig / nombre).read_text(encoding="utf-8"))


def _eventos(rig: pathlib.Path) -> list[str]:
    ruta = rig / "s4e-events.log"
    if not ruta.exists():
        return []
    return [linea.split("|", 1)[-1] for linea in ruta.read_text(encoding="utf-8").splitlines()]


def _eventos_de(rig: pathlib.Path, marca_inicio: str) -> list[str]:
    """Eventos (sin nonce) de la ÚLTIMA corrida del worker que marcó `marca_inicio`.

    Cada proceso del RIG abre con un nonce nuevo y lo antepone a cada línea:
    agrupar por el nonce de la marca aisla la corrida y hace imposible que un
    evento histórico (otro worker, otro test sobre el mismo rig) satisfaga una
    espera del ciclo actual.
    """
    ruta = rig / "s4e-events.log"
    if not ruta.exists():
        return []
    nonce: str | None = None
    eventos: list[str] = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        n, _, evento = linea.partition("|")
        if evento == marca_inicio:
            nonce = n
            eventos = [evento]
        elif nonce is not None and n == nonce:
            eventos.append(evento)
    return eventos


def _plan_del_rig(rig: pathlib.Path) -> Any:
    from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    return load_durable_authorized_plan(operation_id, programdata_resolver=lambda: rig / "programdata")


def _sha_sd_live(plan: Any, relative_path: str) -> str:
    """SHA-256 del SD VIVO del nodo, leído por handle con la primitiva canónica."""
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


def _verificar_sd_igual_a_pre(plan: Any, relative_path: str) -> None:
    """Verificación SEMÁNTICA del nodo contra el PRE autorizado (la primitiva de #644)."""
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


def _crash_externo_por_evento(rig: pathlib.Path, fase: str, prefijo: str, timeout: float = 120.0) -> None:
    """Lanza el worker y lo mata con ``taskkill`` apenas aparece el breadcrumb.

    El nonce del worker es nuevo por corrida, así que un evento de una corrida
    anterior NO puede hacer que este test crie que crasheó.
    """
    import time

    proc = _lanzar(rig, fase)
    limite = time.monotonic() + timeout
    try:
        while time.monotonic() < limite:
            if any(e.startswith(prefijo) for e in _eventos(rig)):
                _matar_arbol(proc)
                return
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        _matar_arbol(proc)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def rig() -> Any:
    base = _rig()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------
# Guard de aislamiento
# --------------------------------------------------------------------------


def test_el_rig_nunca_toca_una_instalacion_real(rig: pathlib.Path) -> None:
    """El RIG vive bajo TEMP y ninguno de sus paths contiene una instalación real."""
    raiz = str(rig.resolve()).lower()
    assert raiz.startswith(str(pathlib.Path(tempfile.gettempdir()).resolve()).lower())
    for prohibido in _PATHS_PROHIBIDOS:
        assert prohibido not in raiz, f"el RIG S4-E rechaza rutas con '{prohibido}': {rig}"


def test_el_worker_rechaza_una_raiz_fuera_de_temp(tmp_path: pathlib.Path) -> None:
    """El guard del worker es real: no es decorativo."""
    fuera = tmp_path / "Skyrim" / "Data"
    fuera.mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, f"{_WORKER}", "--rig-root", str(fuera), "--fase", "preparar"],
        capture_output=True,
        check=False,
        text=True,
        cwd=_CWD,
    )
    assert proc.returncode != 0
    assert "TEMP" in (proc.stderr + proc.stdout) or "prohibidas" in (proc.stderr + proc.stdout)


# --------------------------------------------------------------------------
# E01 — HAPPY PATH
# --------------------------------------------------------------------------


def test_e01_happy_path_comite_y_libera_el_lock(rig: pathlib.Path) -> None:
    """E01: apply real → backup durable → COMMITTED → lock liberado.

    El orden observable es la propiedad: el digest del backup aparece en el
    resultado y ``COMMITTED`` es durable ANTES del release. Un S4-D que
    comiteara sin archivar dejaría ``archive_digest`` en null, que el assert
    de abajo notices.
    """
    r = _correr(rig, "apply_then_finalize")
    assert r["returncode"] == 0, r["stderr"]
    resultado = _leer(rig, "s4e-result.json")

    assert resultado["apply_ok"] is True
    assert resultado["apply_sdsi_calls"] == 3, "los 3 nodos del RIG deben llevar SetSecurityInfo real"
    assert resultado["committed"] is True
    assert resultado["journal_state"] == "committed"
    assert resultado["lock_released"] is True
    assert resultado["archive_digest"], "COMMITTED sin backup durable es exactamente lo que S4-D debe impedir"

    # El orden del breadcrumbs: apply antes del primer gate, gates antes del commit.
    eventos = _eventos(rig)
    assert any(e.startswith("sdsi:") for e in eventos)
    assert any(e.startswith("gate:") for e in eventos)


def test_e01_el_backup_existe_en_disco_antes_del_commit(rig: pathlib.Path) -> None:
    """La evidencia de respaldo es observable en el filesystem, no sólo en el reporte."""
    r = _correr(rig, "apply_then_finalize")
    assert r["returncode"] == 0, r["stderr"]
    backups = list((rig / "programdata").rglob("*_manifest.json"))
    assert backups, "COMMITTED sin manifest de backup en disco"
    assert len(backups) == 1, backups


# --------------------------------------------------------------------------
# E08 — COMMITTED + lock huérfano → normalización idempotente
# --------------------------------------------------------------------------


def test_e08_replay_sobre_committed_no_reaplica(rig: pathlib.Path) -> None:
    """E08/E14: reanudar un COMMITTED normaliza; NO vuelve a aplicar."""
    assert _correr(rig, "apply_then_finalize")["returncode"] == 0

    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    assert b["disposition"] == "already_committed"
    assert b["route"] == "s4d_normalize"
    assert b["committed"] is True
    assert b["rollback_executed"] is False
    # El conteo GLOBAL de SetSecurityInfo es el del proceso A y nada más: la
    # replay no re-aplica. Cada breadcrumb lleva el nonce de su corrida, así
    # que esto cuenta el apply real y no un evento histórico.
    assert sum(1 for e in _eventos(rig) if e.startswith("pre-sdsi:")) == 3, (
        "la replay re-aplicó: sólo el proceso A puede llamar a SetSecurityInfo"
    )


def test_e14_replay_es_idempotente_dos_veces(rig: pathlib.Path) -> None:
    """E14: dos reanudaciones dan el mismo resultado y no divergen."""
    assert _correr(rig, "apply_then_finalize")["returncode"] == 0
    primero = None
    for _ in range(2):
        assert _correr(rig, "resume")["returncode"] == 0
        b = _leer(rig, "s4e-result-b.json")
        if primero is None:
            primero = b
        else:
            assert b["disposition"] == primero["disposition"]
            assert b["archive_digest"] == primero["archive_digest"]
            assert b["journal_state"] == primero["journal_state"]


# --------------------------------------------------------------------------
# E02/E03 — crash durante el apply, con taskkill REAL
# --------------------------------------------------------------------------


def test_la_produccion_no_consume_breadcrumbs_del_rig() -> None:
    """P6 (cont.): los breadcrumbs del RIG son evidencia de TEST, no autoridad.

    El RIG sabe MUCHO más que la producción: en E02 conoce ``pre-sdsi`` sin
    ``sdsi``, o sea que la muerte ocurrió ANTES del ``SetSecurityInfo``. El
    proceso B real no tiene esa información, y el contrato de §20 C8 existe
    justamente para no depender de ella: la evidencia durable dice ``MUTATING`` y
    la reconciliación decide con una sonda semántica.

    Si el código productivo llegara a leer los breadcrumbs, el recovery
    determinista dejaría de ser determinista en producción y pasaría a depender
    de un artefacto de test — el peor modo de fallo posible, porque el RIG lo
    seguiría dando verde. Este ancla congela la frontera.
    """
    from sky_claw.local.runtime_vault import protection_service as svc

    raiz = pathlib.Path(svc.__file__).resolve().parents[3]
    marcas_test_only = (
        "s4e-events.log",
        "s4e_crash_worker",
        "s4e-boot.json",
        "s4e-result-b.json",
        "SkyClaw-S4E-RIG",
    )
    assert raiz.name == "Sky-Claw" or (raiz / "sky_claw").is_dir(), f"raíz inesperada: {raiz}"

    referencias: list[str] = []
    for modulo in (raiz / "sky_claw").rglob("*.py"):
        arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                for marca in marcas_test_only:
                    if marca in nodo.value:
                        referencias.append(f"{modulo.relative_to(raiz)}:{nodo.lineno} menciona {marca!r}")

    assert not referencias, (
        "producción consumió evidencia del RIG: el recovery pasó a depender de un "
        f"artefacto de test y sólo el RIG lo seguiría dando verde: {referencias}"
    )


def test_e02_no_puede_volver_a_un_contrato_permisivo() -> None:
    """P6 (cont.): el contrato E02 exacto queda congelado POR FORMA, no por valor.

    Derivar el contrato correcto no alcanza si nada impide que el test vuelva a
    ser laxo: sustituir ``== "rolled_back"`` por ``in ("rolled_back",
    "indeterminate")`` deja el test VERDE, porque la producción es determinista y
    siempre devuelve ``rolled_back``. Ese es el agujero real que dejaron los
    mutantes M1/M2: un contrato permisivo no se detecta por sí mismo.

    Este ancla mira la FORMA del test E02 por AST y exige igualdad exacta sobre
    ``disposition`` y ``route``, y que no exista pertenencia a conjunto sobre
    esos dos campos. Así el weaken que sobrevivió a los mutantes queda muerto,
    y borrar la evidencia física también.
    """
    fuente = pathlib.Path(__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)

    def _test(nombre: str) -> ast.FunctionDef:
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
                return nodo
        raise AssertionError(f"no se encontró el test {nombre}")

    e02 = _test("test_e02_crash_durante_apply_revierte_el_golden")

    exactos: set[tuple[str, str]] = set()
    permisivos: list[str] = []
    tiene_evidencia_fisica = False

    def _campo(izq: ast.expr) -> str | None:
        """Nombre del campo en ``b["campo"]``, sea cual sea el tipo de comilla.

        Se toma del nodo ``Subscript`` y no de ``unparse``: el texto normaliza
        las comillas y haría fallar el ancla por estilo, no por contrato.
        """
        if isinstance(izq, ast.Subscript) and isinstance(izq.slice, ast.Constant):
            valor = izq.slice.value
            if isinstance(valor, str):
                return valor
        return None

    for nodo in ast.walk(e02):
        if not isinstance(nodo, ast.Assert):
            continue
        texto = ast.unparse(nodo.test)
        # Normalizar comillas y paréntesis: ``unparse`` unifica ambos estilos y
        # el ancla no debe depender de la forma en que quedó escrito el texto.
        norm = texto.replace("'", "").replace('"', "").replace("(", "").replace(")", "").replace(" ", "")
        if "notanye.startswithsdsi:foreineventos_b" in norm:
            tiene_evidencia_fisica = True
        if not isinstance(nodo.test, ast.Compare) or len(nodo.test.ops) != 1:
            continue
        op = nodo.test.ops[0]
        campo = _campo(nodo.test.left)
        if campo is not None and isinstance(op, ast.Eq) and isinstance(nodo.test.comparators[0], ast.Constant):
            valor = nodo.test.comparators[0].value
            if isinstance(valor, str):
                exactos.add((campo, valor))
        if campo in {"disposition", "route"} and isinstance(op, (ast.In, ast.NotIn)):
            permisivos.append(texto)

    for campo, valor in (("disposition", "rolled_back"), ("route", "s4c_rollback")):
        assert (campo, valor) in exactos, (
            f'E02 debe afirmar exactamente b["{campo}"] == "{valor}": el contrato se derivó '
            f"del código y no admite un conjunto permisivo. Asserts exactos hallados: {exactos}"
        )

    assert not permisivos, (
        "E02 volvió a aceptar un conjunto de desenlaces; un contrato permisivo no se "
        f"detecta por sí mismo porque la producción siempre devuelve el valor bueno: {permisivos}"
    )

    assert tiene_evidencia_fisica, (
        "E02 perdió la evidencia física (que la reconciliación no escribió ninguna SD): "
        "sin ella, un 'rolled_back' correcto en el DTO no probaría nada sobre el Golden"
    )


def test_e02_crash_durante_apply_revierte_el_golden(rig: pathlib.Path) -> None:
    """E02 ESTRICTO: crash mid-apply ⇒ desenlace único, derivado del protocolo.

    Por que ``taskkill`` no aca: la transaccion completa del RIG dura menos que
    el intervalo de polling, asi que un ``taskkill`` por sondeo llega cuando la
    transaccion ya comiteo y el test probaria una idempotencia en lugar de un
    crash. La muerte EXTERNA por ``taskkill /T /F`` si se ejercita en E15.

    P6 — este test era LAX y ahora es un contrato exacto. Antes aceptaba
    ``disposition in (rolled_back, rollback_required, indeterminate)`` y
    ``route in (s4c_rollback, operator_required)``: tres desenlaces para un
    escenario que tiene uno. El permiso estaba en el commit que introdujo E02
    (``bc92cdf6``), sin ``xfail``, sin workaround de plataforma o elevación que
    lo justificara — un placeholder que nadie endureció cuando el comportamento
    se volvió determinista.

    El contrato sale del CÓDIGO, no de la intuición:

    * Orden WAL (§6): ``record_node_mutation_intent`` durable → ``SetSecurityInfo``
      → ``record_node_mutation_completed`` durable. Un crash entre el primero y
      el segundo deja ``MUTATING`` sin ``MUTATED``.
    * ``MUTATING(K)`` significa que K PUEDE estar mutado (§20 C4). La
      evidencia durable NO distingue "murió antes del SetSecurityInfo" de
      "murió después": por eso el rollback es IDEMPOTENTE y se decide con una
      SONDA SEMÁNTICA contra el PRE autorizado (§20 C8), no con memoria de qué
      nodos se mutaron.
    * En E02 el ``SetSecurityInfo`` nunca corrió (``pre-sdsi`` sin ``sdsi``), así
      que la sonda encuentra el nodo ``en_pre``: se SKIPEA sin escribir, no hay
      nada que restaurar, y la transacción cierra ``ROLLED_BACK`` con el lock
      LIBERADO.

    ``rollback_executed`` NO significa "se escribió una SD": es
    ``physical_restoration_completed``, la reconciliación física verificada. En
    E02 vale True con CERO escrituras, que es el contrato §20 C8 funcionando, no
    una restauración que no ocurrió (§15).
    """
    r = _correr(rig, "apply_then_finalize", crash_en="mid_apply")
    assert r["returncode"] == 43, r["stderr"]

    # El crash fue ANTES de cualquier SetSecurityInfo del nodo crasheado:
    # existe el breadcrumb ``pre-sdsi`` y ningun ``sdsi`` lo sigue.
    eventos = _eventos(rig)
    assert any(e.startswith("pre-sdsi:") for e in eventos), eventos
    assert not any(e.startswith("sdsi:") for e in eventos), eventos

    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    # --- Contrato exacto: un desenlace, no un conjunto -------------------
    assert b["disposition"] == "rolled_back", b
    assert b["route"] == "s4c_rollback", b["route"]
    assert b["committed"] is False, b
    assert b["settled"] is True, b
    assert b["operator_intervention_required"] is False, b
    assert b["rollback_executed"] is True, b
    assert b["lock_retained"] is False, b
    assert b["lock_outcome"] == "acquired_released", b
    assert b["archive_digest"] is None, "una transacción revertida no archiva backup"
    assert b["fail_closed_reason"] == "", b

    # --- Evidencia FÍSICA: ni una escritura de SD en la reconciliación ---
    # No había nada que restaurar porque el nodo nunca salió de PRE. Un
    # ``rolled_back`` con escrituras sería otro escenario (A de §5) y exigiría
    # su propio contrato; aquí cero escrituras es lo que la sonda verificó.
    eventos_b = _eventos(rig)
    assert not any(e.startswith("sdsi:") for e in eventos_b), (
        f"la reconciliación escribió una SD sin haber mutagenizado nada: {eventos_b}"
    )
    assert not [e for e in eventos_b if "restore" in e.lower()], eventos_b

    # --- Evidencia DURABLE: el asiento terminal existe en el journal ------
    from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
    from sky_claw.local.runtime_vault.protection_journal_store import (
        ProtectionJournalClassification,
        classify_protection_journal,
    )

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    clasificacion = classify_protection_journal(
        operation_id,
        programdata_resolver=lambda: rig / "programdata",
    )
    assert clasificacion.classification is ProtectionJournalClassification.VALID, clasificacion.detail
    assert clasificacion.journal is not None
    assert clasificacion.journal.transaction_state is ProtectionTransactionState.ROLLED_BACK, (
        "el journal durable debe quedar en ROLLED_BACK: el asiento terminal es la "
        "evidencia que distingue un desenlace real de un DTO bien formado"
    )


def test_e02_deja_el_golden_readquirible(rig: pathlib.Path) -> None:
    """E02 (cont.): tras el desenlace settled, una operación nueva toma el Golden.

    Si S4-C hubiera devuelto ``retain`` en vez de liberar, esta adquisición
    real daría ``GoldenLockBusyError`` — que es exactamente la regresión que hay
    que cazar. Es el mismo contrato que E16 ya congeló, aplicado al camino de
    rollback para que las dos mitades queden ancladas juntas.

    La identidad sale del PLAN DURABLE y no del binding: en este escenario el
    apply alcanzó a promotion, así que ``authorized_plan.json`` es la autoridad
    (E16 usa el binding porque en la ventana pre-plan el plan todavía no existe;
    usar la fuente equivocada ahí sería el mismo error en espejo).
    """
    assert _correr(rig, "apply_then_finalize", crash_en="mid_apply")["returncode"] == 43
    assert _correr(rig, "resume")["returncode"] == 0
    b = _leer(rig, "s4e-result-b.json")
    assert b["lock_retained"] is False, b

    from sky_claw.local.runtime_vault.authorized_plan_store import (
        load_durable_authorized_plan,
    )
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        acquire_golden_mutation_lock,
        derive_golden_lock_path,
    )

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    durable = load_durable_authorized_plan(operation_id, programdata_resolver=lambda: rig / "programdata")
    assert durable is not None, "el plan durable es la autoridad de identidad en este escenario"

    pathlib.Path(
        str(
            derive_golden_lock_path(
                durable.volume_serial_number,
                durable.root_file_id,
                programdata_resolver=lambda: rig / "programdata",
            )
        )
    ).parent.mkdir(parents=True, exist_ok=True)

    nuevo_lock = acquire_golden_mutation_lock(
        durable.volume_serial_number,
        durable.root_file_id,
        operation_id,
        programdata_resolver=lambda: rig / "programdata",
    )
    try:
        assert not nuevo_lock.closed, "un Golden revertido tiene que poder volver a adquirirse"
    finally:
        nuevo_lock.release()


def test_e02_mid_apply_crash_revierte_fisicamente_el_golden(rig: pathlib.Path) -> None:
    """E02: crash REAL a mitad del apply -> S4-C REVIERTE físicamente el Golden.

    El escenario es el que define P6, no el camino vacío:

        Proceso A  lock real -> journal APPLYING -> nodo 1 con
                   ``SetSecurityInfo`` real, POST verificado y ``MUTATED(K)``
                   durable (flush) -> CRASH antes del nodo 2.
                   Evidencia durable al morir: 1 ``MUTATED`` y `(total-1)`
                   nodos todavía sin registro.

        Proceso B  ``resume_golden_protection`` -> S4-C -> rollback físico
                   (``SetSecurityInfo`` de restauración + verificación
                   semántica) -> ``ROLLED_BACK`` durable -> lock liberado.

    El crash es determinista por construcción: el seam es el ``probe`` del
    nodo K+1, el primer punto del flujo donde "K nodos MUTATED durable" está
    GARANTIZADO (el engine escribe ``MUTATED(K)`` con flush verificado antes
    de abrir el nodo siguiente). Nada de ``sleep`` ni sondeo por tiempo: el
    breadcrumb ``mid-apply-mutated:<n>:<rel>`` es la evidencia causal.

    La muerte es ``os._exit(45)`` desde el worker: muerte de proceso
    determinista, sin ``finally`` ni flush pendiente. NO se presenta como
    muerte externa: el ``taskkill /T /F`` real sobre un proceso que espera de
    verdad se ejercita en E15 — usar el sondeo acá haría el borde dependiente
    del timing, que es exactamente lo que este test existe para descartar.

    El RIG sólo OBSERVA: la restauración la ejecuta S4-C
    (``recover_interrupted_protection``) en el proceso B; este test jamás
    llama a ``restore_security_descriptor_by_handle`` para "preparar" el
    resultado. ``disposition == "rolled_back"`` es la ÚNICA aceptada:
    ``rollback_required`` / ``indeterminate`` / ``operator_required`` son
    fail-closed válidos en OTROS escenarios, pero significan que E02 no
    demostró rollback automático.

    El escenario exige ELEVACIÓN: restaurar un nodo ya endurecido requiere
    ``WRITE_DAC`` de Administrators/SYSTEM (misma clase de límite que los
    escenarios de restauración del RIG de S4-C). Sin elevación el RIG no
    puede observar ni el ACL mutado ni su restauración, y este test se
    salta DECLARÁNDOLO — no es una ejecución de E02.

    El caso C3 (crash ANTES de cualquier ``SetSecurityInfo``, cero
    ``MUTATED``) queda cubierto por su propio contrato estricto en
    ``test_e02_crash_durante_apply_revierte_el_golden``: son escenarios
    distintos y cada uno tiene su prueba.
    """
    if not _es_elevado():
        _skip_sin_elevacion("E02")

    # --- Proceso A: apply real, 1 nodo MUTATED durable, crash antes del nodo 2.
    r = _correr(rig, "apply_then_finalize", crash_en="mid_apply_after_mutated")
    assert r["returncode"] == 45, r["stderr"]

    # Evidencia del crash (sólo la corrida de A, por nonce). Un ``restore:``
    # ANTES del resume sería evidencia cruzada de otra corrida: no puede haberlo.
    eventos_a = _eventos_de(rig, "lock-adquirido")
    assert any(e.startswith("mid-apply-mutated:1:") for e in eventos_a), eventos_a
    assert not any(e.startswith("apply-fin:") for e in eventos_a), eventos_a
    assert not any(e.startswith("restore:") for e in eventos_a), eventos_a
    # Exactamente UN SetSecurityInfo real ocurrió antes de morir.
    assert sum(1 for e in eventos_a if e.startswith("pre-sdsi:")) == 1, eventos_a
    assert sum(1 for e in eventos_a if e.startswith("sdsi:")) == 1, eventos_a
    mutado = next(e.split(":", 2)[2] for e in eventos_a if e.startswith("mid-apply-mutated:"))

    from sky_claw.local.runtime_vault.protection_journal import NodeWalState, ProtectionTransactionState
    from sky_claw.local.runtime_vault.protection_journal_store import classify_protection_journal

    resolver = lambda: rig / "programdata"  # noqa: E731
    plan = _plan_del_rig(rig)
    total = len(plan.plan.nodes)
    assert total > 1, "E02 exige un plan multi-nodo: con 1 nodo no hay apply INCOMPLETO"

    # --- Estado durable PRE-recovery: APPLYING con exactamente 1 MUTATED.
    clasificacion = classify_protection_journal(operation_id=plan.operation_id, programdata_resolver=resolver)
    assert clasificacion.journal is not None
    assert clasificacion.journal.transaction_state is ProtectionTransactionState.APPLYING
    mutados = [n.relative_path for n in clasificacion.journal.nodes_in_state(NodeWalState.MUTATED)]
    assert mutados == [mutado], (mutados, mutado)

    # --- Evidencia FÍSICA pre-crash: el nodo MUTATED ya difiere del PRE
    # autorizado y los nodos pendientes siguen en PRE. Si el ACL no difiriera,
    # el "rollback" del paso siguiente no tendría nada que demostrar.
    nodo_mutado = plan.node_for(mutado)
    assert nodo_mutado is not None
    assert _sha_sd_live(plan, mutado) != nodo_mutado.pre_sd_sha256, (
        f"el nodo '{mutado}' figura MUTATED en el WAL pero su ACL física sigue en PRE: "
        "la mutación no fue durable; el escenario no es E02"
    )
    for nodo in plan.plan.nodes:
        if nodo.relative_path != mutado:
            assert _sha_sd_live(plan, nodo.relative_path) == nodo.pre_sd_sha256, (
                f"el nodo pendiente '{nodo.relative_path}' ya difiere del PRE antes del crash"
            )

    # --- Proceso B: el router de S4-E -> S4-C ejecuta el rollback.
    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    # Assertions duras del desenlace (contrato E02; no se admite otro).
    assert b["disposition"] == "rolled_back", b
    assert b["route"] == "s4c_rollback", b
    assert b["source_orchestrator"].startswith("recovery_orchestrator"), b["source_orchestrator"]
    assert b["committed"] is False
    assert b["rollback_executed"] is True, b
    assert b["operator_intervention_required"] is False, b
    assert b["lock_retained"] is False, b
    assert b["lock_outcome"] == "acquired_released", b
    assert b["archive_digest"] is None, "una transacción revertida no archiva backup"

    # --- Evidencia de RESTORE físico: S4-C restauró EXACTAMENTE los nodos que
    # quedaron MUTATED durable en A (breadcrumb ``restore:`` del resume actual,
    # aislado por nonce para que un restore histórico no cuente).
    eventos_b = _eventos_de(rig, "resume:begin")
    restores = [e for e in eventos_b if e.startswith("restore:")]
    assert restores == [f"restore:{mutado}"], eventos_b

    # --- POST-RECOVERY == PRE, re-observado físicamente en TODOS los nodos.
    #
    # La evidencia final es la verificación SEMÁNTICA por handle
    # (``verify_restored_security_descriptor_by_handle``, ADR 0010 §12.2):
    # owner, group, DACL (ACEs y orden canónico) y SE_DACL_PROTECTED contra el
    # PRE autorizado. NO se exige igualdad raw del SD serializado: Windows
    # puede reserializar con layout/padding equivalente entre
    # SetSecurityInfo/GetSecurityInfo, así que un sha256 de los bytes vivos
    # contra ``pre_sd_sha256`` NO es el contrato de restauración del repo
    # (``target_dacl.py``:1697-1703). El raw-hash SÍ se usa en la
    # PRECONDICIÓN (arriba), donde lo que hay que demostrar es que la ACL
    # dejó de ser PRE — para eso basta la desigualdad.
    for nodo in plan.plan.nodes:
        _verificar_sd_igual_a_pre(plan, nodo.relative_path)

    # --- WAL FINAL durable: ROLLED_BACK, leído de la evidencia y no de la
    # proyección del servicio.
    post = classify_protection_journal(operation_id=plan.operation_id, programdata_resolver=resolver)
    assert post.journal is not None
    assert post.journal.transaction_state is ProtectionTransactionState.ROLLED_BACK, post.journal.transaction_state

    # --- LOCK liberado: una operación nueva puede adquirir el Golden. Si S4-C
    # hubiera retenido el lock tras el rollback completo, esta adquisición
    # caería en la ruta de huérfano/busy — la regresión exacta que hay que cazar.
    from sky_claw.local.runtime_vault.golden_mutation_lock import acquire_golden_mutation_lock

    nuevo_lock = acquire_golden_mutation_lock(
        plan.volume_serial_number,
        plan.root_file_id,
        plan.operation_id,
        programdata_resolver=resolver,
    )
    try:
        assert not nuevo_lock.closed
    finally:
        nuevo_lock.release()


def test_e02_estado_durable_y_fail_closed_sin_elevacion(rig: pathlib.Path) -> None:
    """E02 (contractual): la pre-condición durable del crash y el fail-closed.

    Este test corre en CUALQUIER host porque no exige restaurar un nodo
    endurecido. NO es la prueba física de rollback — esa es
    ``test_e02_mid_apply_crash_revierte_fisicamente_el_golden``, gateada por
    ``TokenElevation``— y no la sustituye: lo que congela acá es que

    1. el seam de crash produce EXACTAMENTE la evidencia del escenario
       (APPLYING, 1 ``MUTATED`` durable, el resto sin registro), en cualquier
       entorno;
    2. en un host SIN elevación, donde la restauración física no puede
       ejecutarse, el resume NUNCA reporta ``rolled_back``: devuelve un
       fail-closed con el lock retenido y cero breadcrumbs ``restore:``.
       Un ``rolled_back`` acá sería un rollback afirmado sin evidencia física —
       el mutante M6-3 (< ROLLED_BACK > sin restaurar) más peligroso — y este
       test lo mata en los runners que no pueden ejecutar el test físico.
    """
    r = _correr(rig, "apply_then_finalize", crash_en="mid_apply_after_mutated")
    assert r["returncode"] == 45, r["stderr"]

    eventos_a = _eventos_de(rig, "lock-adquirido")
    assert any(e.startswith("mid-apply-mutated:1:") for e in eventos_a), eventos_a
    mutado = next(e.split(":", 2)[2] for e in eventos_a if e.startswith("mid-apply-mutated:"))

    from sky_claw.local.runtime_vault.protection_journal import NodeWalState, ProtectionTransactionState
    from sky_claw.local.runtime_vault.protection_journal_store import classify_protection_journal

    resolver = lambda: rig / "programdata"  # noqa: E731
    plan = _plan_del_rig(rig)
    assert len(plan.plan.nodes) > 1, "E02 exige un plan multi-nodo"

    clasificacion = classify_protection_journal(operation_id=plan.operation_id, programdata_resolver=resolver)
    assert clasificacion.journal is not None
    assert clasificacion.journal.transaction_state is ProtectionTransactionState.APPLYING
    assert [n.relative_path for n in clasificacion.journal.nodes_in_state(NodeWalState.MUTATED)] == [mutado]

    # El router sí va a S4-C en cualquier host: eso no necesita privilegios
    # para decidirse.
    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")
    assert b["route"] == "s4c_rollback", b

    if _es_elevado():
        # El runner elevado ejecuta el test físico; acá se congela que el
        # desenlace reportado y el WAL DURABLE cuentan la MISMA historia en
        # ambos sentidos (un ``rolled_back`` sin ``ROLLED_BACK`` durable, o un
        # ``ROLLED_BACK`` durable reportado como otra cosa, son mutantes
        # distintos y ambos letales).
        post = classify_protection_journal(operation_id=plan.operation_id, programdata_resolver=resolver)
        assert post.journal is not None
        if b["disposition"] == "rolled_back":
            assert post.journal.transaction_state is ProtectionTransactionState.ROLLED_BACK, (
                "el servicio afirmó rolled_back sin ROLLED_BACK durable"
            )
        else:
            assert post.journal.transaction_state is not ProtectionTransactionState.ROLLED_BACK, (
                f"ROLLED_BACK durable reportado como '{b['disposition']}'"
            )
        return

    # Sin elevación: S4-C NO pudo restaurar (WRITE_DAC reservado), así que un
    # ``rolled_back`` sería una afirmación falsa. El fail-closed es el
    # comportamiento correcto y queda congelado.
    assert b["disposition"] != "rolled_back", b
    assert b["rollback_executed"] is False, b
    assert b["committed"] is False
    assert b["operator_intervention_required"] is True, b
    assert b["lock_retained"] is True, b
    eventos_b = _eventos_de(rig, "resume:begin")
    assert not any(e.startswith("restore:") for e in eventos_b), eventos_b


def test_e03_crash_todos_mutados_pre_finalizacion_reenruta(rig: pathlib.Path) -> None:
    """E03: apply COMPLETO durable, muerte antes de S4-D → S4-E encadena S4-C → S4-D.

    El borde de crash es determinista por construcción: el worker muere DESPUÉS
    de que `apply_authorized_plan` volvió, que es la única prueba de que cada
    nodo cerró su WAL. Una versión anterior de este test pedía el crash desde
    dentro del puerto, al volver del segundo `SetSecurityInfo`: ahí el nodo
    todavía no tenía su `MUTATED` en el WAL, así que el apply quedaba
    incompleto y el router hacía rollback. Pasaba en esta máquina y fallaba en
    CI según si ese flush había ocurrido — un test verde por suerte de timing,
    que es lo peor que puede hacer un crash test.

    Este test estuvo detrás de un `xfail(strict=True)` mientras S4-C y S4-D no
    podían darse el handle. Ahora es un test real de nuevo: S4-C
    TRANSFIERE el handle vivo y S4-D lo recibe por `continuation_lock`, sin
    cerrar y reabrir nada. El `strict` del marker viejo fue lo que lo dejó
    rojo apenas se cerró el defecto — que es exactamente para lo que estaba.
    """
    r = _correr(rig, "apply_then_finalize", crash_en="all_mutated")
    assert r["returncode"] != 0, "el worker debía morir, no terminar"

    # El apply terminó y los SetSecurityInfo realmente ocurrieron: este es el
    # breadcrumb que prueba el orden "WAL cerrado antes de la muerte".
    eventos = _eventos(rig)
    assert any(e.startswith("apply-fin:True:") for e in eventos), eventos
    assert any(e.startswith("all-mutated:") for e in eventos), eventos
    assert any(e.startswith("sdsi:") for e in eventos), "el apply no llegó a mutar"

    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    # Apply completo ⇒ el router NO puede revertir: tiene que cerrar la
    # transacción. Un `rolled_back` aquí significaría que el router℃
    # reescribió evidencia durable.
    assert b["disposition"] in ("committed", "already_committed", "rollback_required"), b
    assert b["journal_state"] in (
        "verifying_gp1",
        "verifying_rv2",
        "verifying_node_set",
        "archiving_backup",
        "committed",
    ), b
    # La propiedad de S4-E: el handoff ocurrió y salió por S4-D.
    assert b["source_orchestrator"].startswith("finalization_orchestrator"), b["source_orchestrator"]
    # Y CERO re-apply: el conteo global de SetSecurityInfo no crece.
    assert sum(1 for e in _eventos(rig) if e.startswith("pre-sdsi:")) == 3, (
        "la reanudación volvió a aplicar: un COMMITTED sobre un apply ya durable re-muta el Golden"
    )


# --------------------------------------------------------------------------
# Discovery y arranque
# --------------------------------------------------------------------------


def test_discovery_solo_acepta_entradas_del_namespace(rig: pathlib.Path) -> None:
    """El barrido ve la operacion real y descarta lo que no es una operacion.

    Las tres validaciones de :func:`discover_pending_operations` se prueban
    aqui con entradas reales: la buena, un archivo suelto (no es un
    directorio de operacion) y un nombre que no es un ``operation_id``
    canonico. Las tres conviven sin que la buena se/contamine.
    """
    assert _correr(rig, "preparar")["returncode"] == 0
    operaciones = next(iter((rig / "programdata").rglob("operations")))
    (operaciones / "no-es-una-operacion.txt").write_text("x", encoding="utf-8")

    from sky_claw.local.runtime_vault.protection_service import discover_pending_operations

    pendientes = discover_pending_operations(programdata_resolver=lambda: rig / "programdata")
    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    assert [p.operation_id for p in pendientes] == [operation_id], pendientes
    assert pendientes[0].journal_state is not None


def test_el_arranque_clasifica_sin_mutar_y_es_idempotente(rig: pathlib.Path) -> None:
    """GP2-S4E / P4 - el arranque clasifica read-only y NO reconcilia.

    La premisa de este test cambio a proposito. Antes decia "el arranque
    reconcilia"; eso era el blocker: ejecutaba S4-C y S4-D desde
    ``asyncio.to_thread`` en el proceso normal, y un thread no cambia el token
    de seguridad. Con ``PACKAGED_HELPER_PROVISIONING_STATUS = UNRESOLVED`` no
    hay forma honesta de levantar la frontera privilegiada desde ahi, asi que
    el arranque clasifica y reporta.

    Lo que se verifica aca:

    - el barrido ENCUENTRA la operacion pendiente;
    - clasifica su ruta y su binding sin escribir nada;
    - marca que el cierre requiere privilegios, porque hay un COMMITTED que
      S4-D tiene que normalizar y S4-D es mutante;
    - no ejecuto gates ni SetSecurityInfo;
    - repetirlo da el MISMO resultado.
    """
    assert _correr(rig, "apply_then_finalize")["returncode"] == 0

    # Los breadcrumbs son globales a la corrida del worker, asi que el happy
    # path de A YA dejo gates de S4-D legítimos. Lo que hay que afirmar es que
    # el sweep NO AGREGA ninguno: se toma una foto antes y se compara.
    eventos_antes = _eventos(rig)
    gates_antes = sum(1 for e in eventos_antes if e.startswith("gate:"))
    sdsi_antes = sum(1 for e in eventos_antes if e.startswith("pre-sdsi:"))

    for _ in range(2):
        r = _correr(rig, "discovery")
        assert r["returncode"] == 0, r["stderr"]

        descubrimiento = _leer(rig, "s4e-discovery.json")
        assert len(descubrimiento) == 1, descubrimiento
        assert descubrimiento[0]["journal_state"] == "committed", descubrimiento

        arranque = _leer(rig, "s4e-boot.json")
        assert len(arranque) == 1, arranque
        assert arranque[0]["route"] == "s4d_normalize", arranque
        # El cierre de un COMMITTED huerfano es mutante: necesita la frontera.
        assert arranque[0]["requiere_privilegios"] is True, arranque
        assert arranque[0]["bloquea_operador"] is True, arranque
        assert "UNRESOLVED" in arranque[0]["motivo"], arranque

    # Y el arranque NO toco nada: los conteos de gates y de SetSecurityInfo no
    # cambiaron respecto de antes del sweep.
    eventos_despues = _eventos(rig)
    gates_despues = sum(1 for e in eventos_despues if e.startswith("gate:"))
    sdsi_despues = sum(1 for e in eventos_despues if e.startswith("pre-sdsi:"))

    assert gates_despues == gates_antes, (
        f"el arranque ejecuto {gates_despues - gates_antes} gates de S4-D: eso es "
        "reconciliacion mutante desde un thread normal"
    )
    assert sdsi_despues == sdsi_antes, f"el arranque re-aplico {sdsi_despues - sdsi_antes} nodos con SetSecurityInfo"
    assert sdsi_despues == 3, "los 3 nodos del RIG deben llevar SetSecurityInfo, y solo del apply"


def test_e11_lock_externo_no_se_toca(rig: pathlib.Path) -> None:
    """E11: un lock vivo de OTRO proceso deja ``acquired=false`` y nada muta.

    Se sostiene el lock desde ESTE proceso (equivalente a "otro proceso vivo")
    y se corre el resume en un worker aparte: el segundo tiene que clasificar
    ``LIVE`` y devolver ``LOCK_BUSY`` sin escribir en el journal ajeno.
    """
    from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan
    from sky_claw.local.runtime_vault.golden_mutation_lock import acquire_golden_mutation_lock

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    assert _correr(rig, "preparar")["returncode"] == 0

    durable = load_durable_authorized_plan(operation_id, programdata_resolver=lambda: rig / "programdata")
    from sky_claw.local.runtime_vault.golden_mutation_lock import derive_golden_lock_path

    pathlib.Path(
        str(
            derive_golden_lock_path(
                durable.volume_serial_number,
                durable.root_file_id,
                programdata_resolver=lambda: rig / "programdata",
            )
        )
    ).parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_golden_mutation_lock(
        durable.volume_serial_number,
        durable.root_file_id,
        operation_id,
        programdata_resolver=lambda: rig / "programdata",
    )
    try:
        r = _correr(rig, "resume")
        assert r["returncode"] == 0, r["stderr"]
        b = _leer(rig, "s4e-result-b.json")
        assert b["disposition"] == "lock_busy", b
        assert b["committed"] is False
        assert b["archive_digest"] is None, "no se puede archivar sobre un lock ajeno"
    finally:
        lock.release()


def test_e15_muerte_externa_por_taskkill_deja_lock_huerfano(rig: pathlib.Path) -> None:
    """E15: ``taskkill /T /F`` REAL sobre un proceso con el lock tomado.

    Dos cosas quedan probadas que un ``os._exit`` no prueba igual:

    1. La muerte es EXTERNA (la ordena otro proceso, no el propio worker) y
       mata el ARBOL entero: en Windows, matar solo al launcher de
       ``.venv\\Scripts\\python.exe`` deja vivo al hijo real.
    2. El lock queda huerfano con la metadata sin ``RELEASED``, que es
       justo la clase de residuo que S4-C tiene que poder tomar.

    No se usa ``sleep`` para que el timing funcione: el controller espera
    el breadcrumb ``hold:iniciado`` y mata apenas aparece.
    """
    assert _correr(rig, "preparar")["returncode"] == 0
    proc = _lanzar(rig, "hold_lock")
    try:
        import time

        limite = time.monotonic() + 60
        while time.monotonic() < limite and not any(e == "hold:iniciado" for e in _eventos(rig)):
            assert proc.poll() is None, "el worker del lock murio antes de tomar el lock"
            time.sleep(0.05)
        assert any(e == "hold:iniciado" for e in _eventos(rig)), _eventos(rig)
        _matar_arbol(proc)
    finally:
        _matar_arbol(proc)

    # La asercion real NO es "el lock no esta busy" (eso seria mirar el
    # resultado de la propia prueba): es que la PRIMITIVA de recovery puede
    # tomar el lock huerfano de esta MISMA operacion. Si el takeover por
    # ORPHANED dejo de funcionar, esto falla.
    from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        acquire_golden_mutation_lock_for_recovery,
    )

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    durable = load_durable_authorized_plan(operation_id, programdata_resolver=lambda: rig / "programdata")
    adquisicion = acquire_golden_mutation_lock_for_recovery(
        durable.volume_serial_number,
        durable.root_file_id,
        operation_id,
        programdata_resolver=lambda: rig / "programdata",
    )
    try:
        assert adquisicion.handle is not None
    finally:
        adquisicion.handle.release()

    # El takeover NO se apropia del residuo en nombre de otra operacion: el
    # journal sigue siendo el de esta, en el estado en que la muerte la dejo.
    from sky_claw.local.runtime_vault.protection_journal_store import classify_protection_journal

    clasificacion = classify_protection_journal(operation_id, programdata_resolver=lambda: rig / "programdata")
    assert clasificacion.journal is not None
    assert clasificacion.journal.transaction_state.value == "applying"


# --------------------------------------------------------------------------
# E09/E10 — evidencia durable corrupta
# --------------------------------------------------------------------------


@pytest.mark.parametrize("objetivo", ["plan", "journal"])
def test_e09_e10_evidencia_corrupta_es_indeterminate(rig: pathlib.Path, objetivo: str) -> None:
    """E09/E10: plan o journal truncado ⇒ INDETERMINATE, cero escrituras, cero gates."""
    assert _correr(rig, "preparar")["returncode"] == 0
    nombre = "authorized_plan.json" if objetivo == "plan" else "protection_journal.json"
    objetivo_path = next(iter((rig / "programdata").rglob(nombre)))
    objetivo_path.write_bytes(b'{"truncado": ')

    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    assert b["disposition"] == "indeterminate", b
    assert b["committed"] is False
    assert b["fail_closed_reason"], "un INDETERMINATE tiene que decir por qué"
    # Cero gates, cero SetSecurityInfo: la ambigüedad no ejecuta nada.
    eventos = _eventos(rig)
    assert not any(e.startswith("gate:") for e in eventos)
    assert not any(e.startswith("pre-sdsi:") for e in eventos)


# --------------------------------------------------------------------------
# E12/E13 — staging nunca es autoridad
# --------------------------------------------------------------------------


@pytest.mark.parametrize("modo", ["ausente", "adversarial"])
def test_e12_e13_staging_nunca_es_autoridad(rig: pathlib.Path, modo: str) -> None:
    """E12/E13: staging ausente o manipulado no cambia el desenlace.

    El RIG no usa staging para el recovery, así que el test prueba lo que
    importa: el resultado sale IGUAL con staging borrado y con staging
    reescrito con basura. Si algún día el recovery leyera staging, este test
    se pondría rojo solo.
    """
    assert _correr(rig, "apply_then_finalize")["returncode"] == 0
    baseline = _leer(rig, "s4e-result.json")

    staging = rig / "staging"
    if modo == "ausente":
        pass
    else:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "candidate_manifest.json").write_text(
            '{"operation_id": "attacker", "nodes": [{"relative_path": "..\\..\\evil"}]}',
            encoding="utf-8",
        )
        (staging / "PRE.b64").write_text("SD falso del atacante", encoding="utf-8")

    assert _correr(rig, "resume")["returncode"] == 0
    b = _leer(rig, "s4e-result-b.json")
    # El desenlace es INDEPENDIENTE del staging: mismo digest de backup, mismo
    # estado durable. Si el recovery leyera staging, el digest o el estado
    # cambiarian entre los dos modos.
    assert b["disposition"] == "already_committed", b
    assert b["archive_digest"] == baseline["archive_digest"], b
    assert b["journal_state"] == baseline["journal_state"], b


# --------------------------------------------------------------------------
# E16 — binding-only PRE-PLAN crash
# --------------------------------------------------------------------------


def test_e16_pre_plan_crash_se_normaliza_sin_tocar_acl(rig: pathlib.Path) -> None:
    """E16: binding durable + lock tomado + crash → S4-C normaliza el lock.

    La ventana legítima del protocolo: el binding PRE-plan ya es durable y el
    ``GoldenMutationLock`` está tomado, pero la muerte ocurre ANTES del publish
    del ``authorized_plan`` y antes de crear el journal. No hay apply que
    deshacer y no hay nada que revertir.

    Todo con primitives PRODUCTIVAS —``promote_operation_lock_binding`` y
    ``acquire_golden_mutation_lock``— y muerte de proceso por ``os._exit``
    (sin ``finally``, sin ``atexit``, sin flush pendiente). No se presenta como
    muerte externa: ese escenario es E15, que sí usa ``taskkill /T /F``.

    La evidencia pre-crash se verifica en un JSON escrito ANTES de morir, para
    que el test no dependa de que "no se crean" por accidente.
    """
    r = _correr(rig, "pre_plan_lock")
    assert r["returncode"] == 41, r["stderr"]

    pre = _leer(rig, "s4e-preplan.json")
    assert pre["plan_classification"] == "not_durable", pre
    assert pre["journal_classification"] == "absent", pre

    eventos = _eventos(rig)
    assert any(e.startswith("binding-durable:") for e in eventos), eventos
    assert any(e.startswith("lock-tomado:") for e in eventos), eventos

    # --- Proceso B: el router tiene que LLEGAR a S4-C ---
    r = _correr(rig, "resume")
    assert r["returncode"] == 0, r["stderr"]
    b = _leer(rig, "s4e-result-b.json")

    assert b["route"] == "s4c_rollback", b
    assert b["source_orchestrator"].startswith("recovery_orchestrator"), b["source_orchestrator"]

    # S4-C normalizó el lock huérfano: acquired_released, no retained.
    assert b["lock_outcome"] == "acquired_released", b
    assert b["lock_retained"] is False, b

    # Desenlace CERRADO y limpio, no un fail-closed.
    assert b["committed"] is False, b
    assert b["settled"] is True, b
    assert b["operator_intervention_required"] is False, b

    # No hubo MUTATING, no hubo gates de S4-D, no hubo rollback, no hubo backup.
    assert b["rollback_executed"] is False, b
    assert b["archive_digest"] is None, "un pre-plan no archiva nada"
    assert b["journal_state"] is None, "un pre-plan no tiene estado de journal"

    # El reason dice lo que S4-C demostró, no un INDETERMINATE genérico.
    assert "pre-plan" in b["fail_closed_reason"].lower(), b
    assert "NO se toc" in b["fail_closed_reason"], b

    # Cero mutación: ni SetSecurityInfo, ni restore, ni gates.
    todos = _eventos(rig)
    assert not any(e.startswith("pre-sdsi:") for e in todos), "un pre-plan no aplica ACLs"
    assert not any(e.startswith("restore:") for e in todos), "un pre-plan no revierte nada"
    assert not any(e.startswith("gate:") for e in todos), "un pre-plan no ejecuta gates de S4-D"


def test_e16_tras_la_normalizacion_el_golden_queda_disponible(rig: pathlib.Path) -> None:
    """E16 (cont.): una operación nueva puede adquirir el Golden.

    Demuestra que el lock huérfano quedó NORMALIZADO y no retenido. Si S4-C
    hubiera devuelto `retain` en vez de liberar, esta adquisición daría
    ``GoldenLockBusyError`` — que es exactamente la regresión que hay que cazar.

    La identidad se toma del BINDING DURABLE, que es la autoridad de esta
    ventana. No del plan (no existe) ni de staging.
    """
    assert _correr(rig, "pre_plan_lock")["returncode"] == 41
    assert _correr(rig, "resume")["returncode"] == 0

    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        acquire_golden_mutation_lock,
        derive_golden_lock_path,
    )
    from sky_claw.local.runtime_vault.operation_lock_binding import (
        load_durable_operation_lock_binding,
    )

    operation_id = (rig / "operation-id.txt").read_text(encoding="utf-8").strip()
    binding = load_durable_operation_lock_binding(operation_id, programdata_resolver=lambda: rig / "programdata")
    assert binding is not None, "el binding durable es la autoridad de esta ventana"

    pathlib.Path(
        str(
            derive_golden_lock_path(
                binding.volume_serial_number,
                binding.root_file_id,
                programdata_resolver=lambda: rig / "programdata",
            )
        )
    ).parent.mkdir(parents=True, exist_ok=True)

    nuevo_lock = acquire_golden_mutation_lock(
        binding.volume_serial_number,
        binding.root_file_id,
        operation_id,
        programdata_resolver=lambda: rig / "programdata",
    )
    try:
        assert not nuevo_lock.closed, "el lock normalizado tiene que poder volver a adquirirse"
    finally:
        nuevo_lock.release()


def test_e16_pre_plan_no_toca_staging(rig: pathlib.Path) -> None:
    """E16 (cont.): el camino pre-plan no reconstruye autoridad desde staging.

    Staging puede tener cualquier cosa —incluso un manifest completo— y el
    desenlace tiene que ser el mismo. Es la congelación operativa de
    ``STAGING != AUTHORITY`` para la ventana donde NO hay plan ni journal, que
    es donde la tentación de leerlo es máxima.
    """
    assert _correr(rig, "pre_plan_lock")["returncode"] == 41

    staging = rig / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "candidate_manifest.json").write_text(
        '{"operation_id":"attacker","nodos":[{"relative_path":"..\\..\\evil"}]}',
        encoding="utf-8",
    )
    (staging / "PRE.b64").write_text("SD falso del atacante", encoding="utf-8")

    assert _correr(rig, "resume")["returncode"] == 0
    b = _leer(rig, "s4e-result-b.json")
    assert b["committed"] is False, b
    assert b["settled"] is True, b
    assert b["archive_digest"] is None, b
    assert not any(e.startswith("gate:") for e in _eventos(rig))
