"""RIG Windows cross-process de GP2-S4D: PROCESOS REALES, no simulaciones.

Por qué este archivo existe aparte de los tests unitarios
--------------------------------------------------------
Un crash de proceso y una excepción de Python no son lo mismo. La excepción
hace unwind, ejecuta ``finally``, cierra handles, libera el lock del kernel y
deja el proceso vivo: el "recovery" que se observa después es una simulación que
además tiene la ventaja de saber cosas que el proceso real no sabría. Lo que
S4-D promete —que un proceso NUEVO, sin memoria, decide bien a partir de
bytes— sólo se demuestra matando al proceso con ``taskkill /T /F`` y dejando que
otro lo retome.

Cobertura de la matriz de §24
-----------------------------
======  =====================================================================
W-D01   S4-D completo -> COMMITTED -> lock RELEASED
W-D02   crash tras ``VERIFYING_GP1`` durable, antes de observar GP1
W-D03   crash tras un GP1 observado (reanudación en el mismo punto)
W-D04   crash en ``ARCHIVING_BACKUP`` antes de que exista el backup
W-D05   backup durable + crash antes de COMMITTED -> revalida y comitea
W-D06   COMMITTED durable + lock huérfano -> normaliza sin re-mutar
W-D07   handle abierto -> el probe REAL de quiescence falla -> NO COMMITTED
W-D08   drift de contenido antes del RV-2 final -> NO COMMITTED
W-D09   sustitución de FileId con el MISMO contenido -> NO COMMITTED
======  =====================================================================

W-D09 es el caso más interesante y por eso está: sustituir un archivo por otro con el
mismo contenido deja el TreeDigest INTACTO, así que el RV-2 pasa. Sólo la
comparación por identidad física (FileId) lo detecta. Un S4-D que verificara
"paths y contenido" comitearía un Golden donde un archivo fue sustituido.

Guardas duras
-------------
* La raíz es SIEMPRE un árbol descartable nuevo bajo ``%TEMP%``, con UUID.
* Si el nombre de la ruta contiene algo que huela a Steam / Skyrim / Mods /
  MO2, el RIG ABORTA antes de tocar nada. Es una defensa contra que alguien
  corra este archivo con un argumento equivocado.
* El Golden del usuario real no se abre, se lee ni se endurece: nunca.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorized_plan_store import load_durable_authorized_plan
from sky_claw.local.runtime_vault.golden_backup_archive import (
    archive_golden_backup,
    deserialize_golden_backup_archive,
    load_durable_golden_backup,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockPhase,
    derive_golden_lock_path,
    deserialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState, parse_journal_bytes
from sky_claw.local.runtime_vault.protection_journal_store import derive_protection_journal_path

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKER_MODULE = "tests.s4d_crash_worker"

_OPERACION = "b7e2d140-9c5a-4f38-8e21-6d3a0c9f1e57"

_SKIP_POR_PLATAFORMA = pytest.mark.skipif(
    sys.platform != "win32",
    reason="RIG nativo Win32 (procesos reales + NTFS) sólo en Windows",
)

#: Fragmentos que nunca pueden aparecer en la ruta del RIG. Si aparecen, ABORT:
#: un RIG que por un argumento equivocado escribiera sobre el Golden real del
#: usuario sería peor que no tener RIG.
_FRAGMENTOS_PROHIBIDOS: tuple[str, ...] = (
    "steam",
    "skyrim special edition",
    "mod organizer 2",
    "mo2",
    "s4d-rig-prohibido",
    "\\mods\\",
)

#: Nombres de archivo que el RIG crea dentro del Golden. Deliberadamente
#: genéricos: el RIG prueba transacciones, no una instalación de Skyrim.
_CONTENIDO_ESM = b"Skyrim.esm\ncontenido sintetico del RIG\n"
_CONTENIDO_ESM_ALT = b"Skyrim.esm\ncontenido sintetico del RIG\n"
_CONTENIDO_SCRIPTS = b"; script sintetico del RIG\n"


# ============================================================================
# Guardas y helpers del controller
# ============================================================================


def _verificar_ruta_segura(ruta: pathlib.Path) -> None:
    """ABORTA si la ruta del RIG huele a una instalación real."""
    texto = str(ruta).lower()
    for fragmento in _FRAGMENTOS_PROHIBIDOS:
        if fragmento in texto:
            raise AssertionError(
                f"RIG ABORTADO: la ruta '{ruta}' contiene '{fragmento}', que puede ser una instalación real. "
                "Este RIG sólo puede correr sobre un árbol descartable nuevo bajo %TEMP%"
            )


def _rig_root() -> pathlib.Path:
    base = pathlib.Path(tempfile.gettempdir()) / f"SkyClaw-S4D-RIG-{uuid.uuid4().hex}"
    _verificar_ruta_segura(base)
    base.mkdir(parents=True)
    return base


def _resolver(rig: pathlib.Path) -> Any:
    return lambda: rig / "programdata"


def _limpiar(rig: pathlib.Path) -> None:
    shutil.rmtree(rig, ignore_errors=True)


def _crear_golden(rig: pathlib.Path) -> pathlib.Path:
    """Árbol descartable con la forma que el pipeline espera de un Golden."""
    golden = rig / "golden"
    (golden / "Data" / "Scripts").mkdir(parents=True)
    (golden / "Skyrim.esm").write_bytes(_CONTENIDO_ESM)
    (golden / "Data" / "Scripts" / "carga.esp").write_bytes(_CONTENIDO_SCRIPTS)
    return golden


def _argv(rig: pathlib.Path, fase: str, extra: tuple[str, ...] = ()) -> list[str]:
    return [
        sys.executable,
        "-m",
        _WORKER_MODULE,
        "--rig-root",
        str(rig),
        "--operation-id",
        _OPERACION,
        "--phase",
        fase,
        *extra,
    ]


def _correr(
    rig: pathlib.Path, fase: str, timeout: int = 180, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _argv(rig, fase, extra),
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _lanzar(rig: pathlib.Path, fase: str, extra: tuple[str, ...] = ()) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _argv(rig, fase, extra),
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _matar_arbol(proc: subprocess.Popen[str]) -> None:
    """``TerminateProcess`` REAL del árbol del worker (nunca del controller).

    En Windows ``.venv\\Scripts\\python.exe`` es un launcher que ejecuta el
    intérprete real como HIJO: matar sólo el launcher dejaría vivo al worker.
    ``taskkill /T /F`` mata el árbol completo — que es lo que un corte de
    energía le hace a un proceso en general: sin ``finally``, sin ``atexit``, sin
    flush implícito.
    """
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False, text=True)
    proc.wait(timeout=30)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _esperar_y_matar(rig: pathlib.Path, destino: str, *, publicar_backup: bool = False) -> None:
    """Siembra un estado durable y mata al worker MIENTRAS está vivo.

    La espera por el breadcrumb es lo que hace que el test sea causal y no una
    carrera: matar antes de que el proceso escriba probaría el estado durable
    ANTERIOR, que es otra escenario. Y matar en vez de dejar que el worker salga
    solo es lo que lo convierte en un crash real y no en un ``os._exit``.
    """
    extra = ["--destino", destino]
    if publicar_backup:
        extra.append("--publicar-backup")
    proc = _lanzar(rig, "semilla", tuple(extra))
    try:
        _esperar_breadcrumb(rig, "vivo:esperando_taskkill", proc=proc)
        assert _proceso_vivo(proc), "el worker se salió solo: no hay crash que demostrar"
        _matar_arbol(proc)
    finally:
        if proc.poll() is None:
            _matar_arbol(proc)
    assert f"semilla:{destino}" in _eventos(rig)


def _esperar_breadcrumb(rig: pathlib.Path, evento: str, *, proc: subprocess.Popen[str], timeout: float = 60.0) -> None:
    """Espera a que el breadcrumb exista, o que el proceso muera (y falla)."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if evento in _eventos(rig):
            return
        if proc.poll() is not None:
            pytest.fail(
                f"el worker terminó con código {proc.returncode} antes de emitir '{evento}'; "
                f"stderr={proc.stderr.read() if proc.stderr else ''}"
            )
        time.sleep(0.05)
    pytest.fail(f"el worker nunca emitió el breadcrumb '{evento}' (eventos vistos: {_eventos(rig)})")


def _proceso_vivo(proc: subprocess.Popen[str]) -> bool:
    return proc.poll() is None


def _breadcrumbs(rig: pathlib.Path) -> list[str]:
    ruta = rig / "breadcrumbs.log"
    if not ruta.exists():
        return []
    return [linea.strip() for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip()]


def _eventos(rig: pathlib.Path) -> list[str]:
    return [linea.split(" ", 1)[1] for linea in _breadcrumbs(rig) if " " in linea]


def _journal_path(rig: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(str(derive_protection_journal_path(_OPERACION, programdata_resolver=_resolver(rig))))


def _estado_durable(rig: pathlib.Path) -> ProtectionTransactionState:
    """Estado del journal LEÍDO DE DISCO por el controller.

    El controller no comparte memoria con el worker: lee el archivo protegido,
    igual que haría un recovery. Un ``success=True`` del worker no prueba nada
    sobre lo que quedó durable.
    """
    resultado = parse_journal_bytes(_journal_path(rig).read_bytes())
    assert resultado.journal is not None, f"journal no interpretable: {resultado.detail}"
    return resultado.journal.transaction_state


def _lock_path(rig: pathlib.Path) -> pathlib.Path:
    plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
    return pathlib.Path(
        str(
            derive_golden_lock_path(
                plan.volume_serial_number,
                plan.root_file_id,
                programdata_resolver=_resolver(rig),
            )
        )
    )


def _fase_del_lock(rig: pathlib.Path) -> str:
    return deserialize_golden_lock_metadata(_lock_path(rig).read_bytes()).phase


def _backup_path(rig: pathlib.Path) -> pathlib.Path:
    plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
    from sky_claw.local.runtime_vault.golden_backup_archive import derive_golden_backup_path

    return pathlib.Path(str(derive_golden_backup_path(plan.plan, programdata_resolver=_resolver(rig))))


def _preparar(rig: pathlib.Path) -> subprocess.CompletedProcess[str]:
    _crear_golden(rig)
    proc = _correr(rig, "preparar")
    assert proc.returncode == 0, f"preparación falló: {proc.stdout}\n{proc.stderr}"
    assert _estado_durable(rig) is ProtectionTransactionState.APPLYING
    return proc


def _desenlace(stdout: str) -> tuple[str, bool, str]:
    partes = stdout.strip().splitlines()[-1].split("|")
    assert len(partes) == 3, f"salida inesperada del worker: {stdout!r}"
    return partes[0], partes[1] == "True", partes[2]


# ============================================================================
# W-D01: el camino feliz completo
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d01_s4d_completo_termina_en_committed_y_libera_el_lock() -> None:
    """W-D01: todos MUTATED -> S4-D -> COMMITTED -> lock RELEASED."""
    rig = _rig_root()
    try:
        _preparar(rig)
        proc = _correr(rig, "completa")
        assert proc.returncode == 0, f"el worker no comiteó: {proc.stdout}\n{proc.stderr}"

        desenlace, liberado, digest = _desenlace(proc.stdout)
        assert desenlace == "committed"
        assert liberado is True
        assert digest != "-"

        # Todo lo que se afirma, leído de disco y no del stdout del worker.
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
        assert _fase_del_lock(rig) == GoldenLockPhase.RELEASED.value

        # Y el backup es AUTO-CERTIFICADO: ata con el plan durable sin que
        # ningún proceso en memoria participe del juicio.
        plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
        backup = load_durable_golden_backup(plan.plan, programdata_resolver=_resolver(rig))
        assert backup.archive_digest == digest
        assert backup.archive.binds_to(plan.plan)
        # Y trae la evidencia que GP3 va a necesitar.
        assert backup.archive.node_count == plan.plan.node_count
        assert all(nodo.pre_sd_bytes_b64 for nodo in backup.archive.nodes)
    finally:
        _limpiar(rig)


# ============================================================================
# W-D02 / W-D03: reanudación con proceso NUEVO
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d02_crash_antes_de_gp1_el_proceso_b_retoma() -> None:
    """W-D02: ``VERIFYING_GP1`` durable y muerte ANTES de observar GP1.

    El proceso B tiene que observar GP1 él mismo. Si el journal dijera
    "GP1_OK" el test no necesitaría un proceso nuevo, y eso es precisamente lo
    que no existe.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "verifying_gp1")

        assert _estado_durable(rig) is ProtectionTransactionState.VERIFYING_GP1

        # El proceso B arranca SIN memoria del A.
        proc_b = _correr(rig, "completa")
        assert proc_b.returncode == 0, f"el proceso B no comiteó: {proc_b.stdout}\n{proc_b.stderr}"
        desenlace, liberado, _ = _desenlace(proc_b.stdout)
        assert desenlace == "committed"
        assert liberado is True
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED

        # Y B ejecutó TODOS los gates: re-observó, no(약)recordó.
        gates = [evento for evento in _eventos(rig) if evento.startswith("gate:")]
        assert gates == ["gate:gp1", "gate:rv2", "gate:node_set", "gate:quiescence", "gate:rv2", "gate:rv2"]
    finally:
        _limpiar(rig)


@_SKIP_POR_PLATAFORMA
def test_w_d03_crash_tras_un_gp1_observado_no_hay_bandera_que_recordar() -> None:
    """W-D03: el journal sólo dice la FASE, nunca "GP1 ya pasó".

    Es el escenario que separa un flag de completitud de la re-observación. Si
    existiera un ``GP1_COMPLETED`` durable, este estado sería indistinguible del
    de W-D02 y el test no probaría nada.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "verifying_rv2")

        assert _estado_durable(rig) is ProtectionTransactionState.VERIFYING_RV2
        # Y el journal NO tiene ningún registro de verificación completada.
        crudo = _journal_path(rig).read_text(encoding="utf-8")
        for bandera in ("gp1_ok", "gp1_completed", "verified", "rv2_ok", "node_set_ok"):
            assert bandera not in crudo

        proc_b = _correr(rig, "completa")
        assert proc_b.returncode == 0, f"el proceso B no comiteó: {proc_b.stdout}\n{proc_b.stderr}"
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
    finally:
        _limpiar(rig)


# ============================================================================
# W-D04 / W-D05: el archivo
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d04_crash_en_archiving_antes_del_backup_recovery_seguro() -> None:
    """W-D04: ``ARCHIVING_BACKUP`` durable sin backup -> B archiva y comitea."""
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup")

        assert _estado_durable(rig) is ProtectionTransactionState.ARCHIVING_BACKUP
        assert not _backup_path(rig).exists(), "no puede haber backup si se murió antes de escribirlo"

        proc_b = _correr(rig, "completa")
        assert proc_b.returncode == 0, f"el proceso B no comiteó: {proc_b.stdout}\n{proc_b.stderr}"
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
        assert _backup_path(rig).exists()
        assert _fase_del_lock(rig) == GoldenLockPhase.RELEASED.value
    finally:
        _limpiar(rig)


@_SKIP_POR_PLATAFORMA
def test_w_d05_backup_durable_tras_crash_b_revalida_sin_sobrescribir() -> None:
    """W-D05: backup durable + crash antes de COMMITTED -> revalidar y comitear.

    El backup NO se vuelve a publicar: se re-lee y se acepta por equivalencia
    exacta de bytes. Su digest antes y después del crash tiene que ser el
    mismo, y eso es lo que prueba que no se sobrescribió evidencia.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup", publicar_backup=True)

        assert _estado_durable(rig) is ProtectionTransactionState.ARCHIVING_BACKUP
        assert _backup_path(rig).exists()
        bytes_del_backup = _backup_path(rig).read_bytes()
        digest_antes = _sha_de(bytes_del_backup)

        proc_b = _correr(rig, "completa")
        assert proc_b.returncode == 0, f"el proceso B no comiteó: {proc_b.stdout}\n{proc_b.stderr}"
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED

        # Mismos bytes: create-once respetado, evidencia intacta.
        assert _backup_path(rig).read_bytes() == bytes_del_backup
        assert _sha_de(_backup_path(rig).read_bytes()) == digest_antes
        assert _fase_del_lock(rig) == GoldenLockPhase.RELEASED.value

        # Y el journal de B tiene una sola transición ARCHIVING_BACKUP -> COMMITTED:
        crudo = _journal_path(rig).read_text(encoding="utf-8")
        assert crudo.count('"state":"committed"') == 1
    finally:
        _limpiar(rig)


@_SKIP_POR_PLATAFORMA
def test_w_d05b_backup_corrupto_no_se_sobrescribe_ni_habilita_commit() -> None:
    """W-D05b: un backup truncado tras el crash es INDETERMINATE, no COMMITTED."""
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup", publicar_backup=True)
        assert _estado_durable(rig) is ProtectionTransactionState.ARCHIVING_BACKUP

        corrupto = _backup_path(rig).read_bytes()
        _backup_path(rig).write_bytes(corrupto[: len(corrupto) // 2])

        proc_b = _correr(rig, "completa")
        desenlace, _, _ = _desenlace(proc_b.stdout)
        assert desenlace == "indeterminate"
        assert _estado_durable(rig) is not ProtectionTransactionState.COMMITTED
        # Y la evidencia corrupta NO fue reparada ni sustituida en silencio.
        assert _backup_path(rig).read_bytes() == corrupto[: len(corrupto) // 2]
        assert _fase_del_lock(rig) != GoldenLockPhase.RELEASED.value
    finally:
        _limpiar(rig)


def _sha_de(datos: bytes) -> str:
    import hashlib

    return hashlib.sha256(datos).hexdigest()


# ============================================================================
# W-D06: COMMITTED durable + lock huérfano
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d06_committed_durable_con_lock_huerfano_se_normaliza_sin_remutar() -> None:
    """W-D06: el final crítico — journal COMMITTED, lock sin ``phase=RELEASED``.

    El proceso B tiene que normalizar el lock SIN re-ejecutar apply, SIN
    rollback y SIN re-verificar. La prueba de que no re-mutó es que el archivo
    del backup no cambió de bytes y que ningún gate volvió a correr.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup", publicar_backup=True)

        # Se completa el commit a mano, como si el proceso hubiera muerto
        # ENTRE la escritura del journal y la del lock. Es la fila P de §20.
        from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal

        plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
        journal = open_protection_journal(_OPERACION, plan, programdata_resolver=_resolver(rig))
        escritura = archive_golden_backup(plan, programdata_resolver=_resolver(rig), writer=_WriterDelRIG())
        journal.commit_finalized(archive=escritura.durable, plan=plan)
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
        # El lock quedó huérfano, sin RELEASED.
        assert _fase_del_lock(rig) != GoldenLockPhase.RELEASED.value
        bytes_del_backup = _backup_path(rig).read_bytes()

        proc_b = _correr(rig, "completa")
        desenlace, liberado, _ = _desenlace(proc_b.stdout)
        assert desenlace == "already_committed"
        assert liberado is True

        # Final de la transacción: lock normalizado, evidencia intacta, y NINGÚN
        # gate re-ejecutado (un replay que re-verifica está desperdiciando I/O
        # y, peor, podría observar drift y reabrir una transacción ya cerrada).
        assert _fase_del_lock(rig) == GoldenLockPhase.RELEASED.value
        assert _backup_path(rig).read_bytes() == bytes_del_backup
        assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
        assert not any("reejecucion" in evento for evento in _eventos(rig))
    finally:
        _limpiar(rig)


class _WriterDelRIG:
    """Create-once real con ``O_EXCL`` + flush, para el commit manual del W-D06."""

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        descriptor = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_BINARY)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@_SKIP_POR_PLATAFORMA
def test_w_d06b_lock_de_otra_operacion_no_se_toca_tras_un_committed() -> None:
    """W-D06b: COMMITTED de X con lock de Y -> INDETERMINATE y el lock intacto.

    Es la intersección de §15 con el caso S: tomar ese lock sería operar sobre
    el Golden de otra transacción.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup", publicar_backup=True)

        plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
        from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal

        journal = open_protection_journal(_OPERACION, plan, programdata_resolver=_resolver(rig))
        escritura = archive_golden_backup(plan, programdata_resolver=_resolver(rig), writer=_WriterDelRIG())
        journal.commit_finalized(archive=escritura.durable, plan=plan)

        # El lock pasa a ser de OTRA operación.
        from sky_claw.local.runtime_vault.golden_mutation_lock import (
            GoldenLockMetadata,
            derive_golden_lock_key,
            serialize_golden_lock_metadata,
        )

        ruta = _lock_path(rig)
        bytes_previos = ruta.read_bytes()
        ruta.write_bytes(
            serialize_golden_lock_metadata(
                GoldenLockMetadata(
                    lock_key=derive_golden_lock_key(plan.volume_serial_number, plan.root_file_id),
                    operation_id="c0ffee00-1111-4222-8333-444455556666",
                    owner_pid=0x7FFFFFFF,
                    owner_process_creation_time=140_000_000_000_000_000,
                    session_id=1,
                    created_at=1_758_499_100,
                    phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                )
            )
        )

        proc_b = _correr(rig, "completa")
        desenlace, _, _ = _desenlace(proc_b.stdout)
        assert desenlace == "indeterminate"
        # El lock ajeno quedó tal cual: ni tomado ni liberado.
        assert ruta.read_bytes() != bytes_previos
        metadata = deserialize_golden_lock_metadata(ruta.read_bytes())
        assert metadata.operation_id == "c0ffee00-1111-4222-8333-444455556666"
        assert metadata.phase == GoldenLockPhase.AUTHORIZATION_BOUNDARY.value
    finally:
        _limpiar(rig)


# ============================================================================
# W-D07: quiescence real con handle bloqueante
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d07_un_handle_abierto_hace_fallar_la_quiescence_final() -> None:
    """W-D07: el probe REAL de quiescence falla con un handle abierto.

    Este es el único escenario del RIG que usa la primitiva de quiescence de
    verdad, porque es la única cuyo fallo se puede provocar sin endurecer el
    Golden. Con un handle ``dwShareMode=0`` sobre un nodo, el probe recibe
    ``ERROR_SHARING_VIOLATION`` tras sus 5 intentos con backoff.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        proc = _correr(rig, "quiescence_bloqueada")
        desenlace, liberado, _ = _desenlace(proc.stdout)

        assert desenlace == "rollback_required", f"un Golden tomado no puede comitear: {proc.stdout}\n{proc.stderr}"
        assert liberado is False, "el lock NO se libera con un Golden bloqueado"
        assert _estado_durable(rig) is ProtectionTransactionState.ROLLBACK_REQUIRED
        assert not _backup_path(rig).exists(), "sin el gate de quiescence no se archiva"
        assert _fase_del_lock(rig) != GoldenLockPhase.RELEASED.value
        assert "handle_bloqueante" in " ".join(_eventos(rig))
    finally:
        _limpiar(rig)


# ============================================================================
# W-D08 / W-D09: drift de contenido y de identidad física
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_w_d08_drift_de_contenido_antes_del_rv2_final_no_comitea() -> None:
    """W-D08: un byte cambia en el Golden -> NO COMMITTED.

    El drift ocurre con el journal aún en APPLYING, así que el RV-2 final lo ve
    en la primera observación. Es el caso F de la matriz adversarial: contenido
    mutado entre el apply y la post-verificación.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        (rig / "golden" / "Skyrim.esm").write_bytes(_CONTENIDO_ESM_ALT + b"mutacion del adversario\n")

        proc = _correr(rig, "completa")
        desenlace, _, _ = _desenlace(proc.stdout)
        assert desenlace == "rollback_required"
        assert _estado_durable(rig) is ProtectionTransactionState.ROLLBACK_REQUIRED
        assert not _backup_path(rig).exists()
        assert _fase_del_lock(rig) != GoldenLockPhase.RELEASED.value
    finally:
        _limpiar(rig)


@_SKIP_POR_PLATAFORMA
def test_w_d09_sustitucion_de_fileid_con_el_mismo_contenido_no_comitea() -> None:
    """W-D09: mismo contenido, objeto físico DISTINTO -> NO COMMITTED.

    Es el caso que separa "comparar paths y contenido" de "comparar identidad".
    El archivo se borra y se reescribe con bytes IDÉNTICOS: el TreeDigest no
    cambia, así que el RV-2 tiene que pasar. Lo único que lo detecta es el
    FileId, y por eso este test existe: si el NodeSet comparara sólo paths,
    comitearía un Golden donde un archivo fue sustituido.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        objetivo = rig / "golden" / "Data" / "Scripts" / "carga.esp"
        contenido = objetivo.read_bytes()
        plan_antes = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
        file_id_antes = {n.relative_path: n.file_id for n in plan_antes.plan.nodes}
        digest_antes = plan_antes.plan.tree_digest

        # Sustitución: mismo path, mismo contenido, objeto nuevo.
        objetivo.unlink()
        objetivo.write_bytes(contenido)

        # El contenido es idéntico, así que el digest del árbol NO cambió…
        from sky_claw.local.runtime_vault.inventory import inventory_tree
        from sky_claw.local.runtime_vault.verification import tree_digest_from_files

        assert tree_digest_from_files(inventory_tree(rig / "golden")) == digest_antes, (
            "el test requiere que el contenido NO cambie: si el digest difiere, el RV-2 lo detectaría "
            "y no estaríamos probando la identidad física"
        )
        # …pero la identidad física de ese nodo sí.
        from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence

        file_id_despues = {e.backup.relative_path: e.backup.file_id for e in probe_node_evidence(str(rig / "golden"))}
        assert file_id_despues["Data/Scripts/carga.esp"] != file_id_antes["Data/Scripts/carga.esp"], (
            "la sustitución no cambió el FileId: el escenario no probaría nada en este volumen"
        )

        proc = _correr(rig, "completa")
        desenlace, _, _ = _desenlace(proc.stdout)
        assert desenlace == "rollback_required", (
            f"una sustitución con el mismo contenido debe fallar el NodeSet por identidad física: {proc.stdout}"
        )
        assert _estado_durable(rig) is ProtectionTransactionState.ROLLBACK_REQUIRED
        assert not _backup_path(rig).exists()
    finally:
        _limpiar(rig)


# ============================================================================
# Reinicio MÚLTIPLE en la misma fase (fila V de §30)
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_v_reiniciar_tres_veces_en_la_misma_fase_no_altera_el_desenlace() -> None:
    """V: el recovery es idempotente aunque se reinicie muchas veces.

    Se siembra la misma fase tres veces y se corre el recovery tres veces. La
    primera termina en COMMITTED; las otras dos son replays terminales. Si el
    recoveryDepends de "que sea la primera vez" o de cuántas veces se lo llamó,
    esto lo rompería.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        for _ in range(3):
            _esperar_y_matar(rig, "verifying_rv2")
            assert _estado_durable(rig) is ProtectionTransactionState.VERIFYING_RV2

        for intento in range(3):
            proc = _correr(rig, "completa")
            if intento == 0:
                assert proc.returncode == 0, f"el recovery no comiteó: {proc.stdout}\n{proc.stderr}"
                assert _estado_durable(rig) is ProtectionTransactionState.COMMITTED
            else:
                desenlace, _, _ = _desenlace(proc.stdout)
                assert desenlace == "already_committed"

        assert _fase_del_lock(rig) == GoldenLockPhase.RELEASED.value
    finally:
        _limpiar(rig)


# ============================================================================
# El backup sobre disco, re-leído por el controller (independiente del worker)
# ============================================================================


@_SKIP_POR_PLATAFORMA
def test_el_backup_producido_es_leyble_y_verificable_por_un_tercero() -> None:
    """El backup se relee SIN el worker: su validez no depende de quién lo escribió.

    Es la diferencia entre evidencia y un mensaje. El controller no comparte
    proceso, memoria ni handles con el worker: abre el archivo, lo parsea con
    esquema cerrado y comprueba los bindings contra el plan durable.
    """
    rig = _rig_root()
    try:
        _preparar(rig)
        proc = _correr(rig, "completa")
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

        crudo = _backup_path(rig).read_bytes()
        archivo = deserialize_golden_backup_archive(crudo)
        plan = load_durable_authorized_plan(_OPERACION, programdata_resolver=_resolver(rig))
        assert archivo.binds_to(plan.plan)
        assert archivo.archive_digest == _sha_de(crudo)
        # PRE SD por nodo: la evidencia de restauración que GP3 necesitará.
        for nodo in archivo.nodes:
            assert nodo.pre_sd_bytes_b64
            assert nodo.pre_sd_sha256
            assert nodo.pre_dacl_protected_flag is not None
        # Y un esquema futuro no se interpreta como autoridad actual.
        import json

        cuerpo = json.loads(crudo.decode("utf-8"))
        cuerpo["schema_version"] = "gp2-golden-backup-v99"
        with pytest.raises(Exception, match="schema_version"):
            deserialize_golden_backup_archive(json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    finally:
        _limpiar(rig)


@_SKIP_POR_PLATAFORMA
def test_un_backup_de_otra_operacion_no_se_sobrescribe() -> None:
    """Ninguna operación puede pisar la evidencia de otra (fila M de §20)."""
    rig = _rig_root()
    try:
        _preparar(rig)
        _esperar_y_matar(rig, "archiving_backup", publicar_backup=True)

        destino = _backup_path(rig)
        evidencia_ajena = b'{"schema_version":"gp2-golden-backup-v1","operation_id":"ajena"}'
        destino.write_bytes(evidencia_ajena)

        proc_b = _correr(rig, "completa")
        desenlace, _, _ = _desenlace(proc_b.stdout)
        assert desenlace == "indeterminate"
        assert destino.read_bytes() == evidencia_ajena
    finally:
        _limpiar(rig)
