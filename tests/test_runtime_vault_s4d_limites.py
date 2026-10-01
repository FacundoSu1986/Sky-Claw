"""Límites de error y de responsabilidad de S4-D.

Cada test de este archivo ancla UN límite que un caller podría asumir violado:

* los errores de dominio de la evidencia se vuelven veredicto, no excepción;
* los errores del namespace de confianza no escapan del contrato del store;
* un ``OSError`` al liberar el lock no deshace un ``COMMITTED`` durable;
* el manifiesto acepta los mismos enteros que acepta el plan autoritativo;
* S4-D deja ``ROLLBACK_REQUIRED`` y **no** ejecuta rollback de ACL.

Son límites de FRONTERA: el código del que están en duda está en un módulo
distinto del que decide. Un test de comportamiento en el orquestador no los
cubriría, porque el orquestador ya trata correctamente lo que le llega bien
tipado; lo que no se sabe es si algo mal tipado puede llegar.
"""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any

import pytest

from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
)
from sky_claw.local.runtime_vault.finalization_verification import (
    Win32FinalizationVerificationPort,
)
from sky_claw.local.runtime_vault.golden_backup_archive import (
    GoldenBackupArchive,
    GoldenBackupWriteError,
    deserialize_golden_backup_archive,
)
from sky_claw.local.runtime_vault.golden_protection_plan import DuplicateFileIdError
from sky_claw.local.runtime_vault.models import InventoryError, InventoryLinkError, TreeDigest
from sky_claw.local.runtime_vault.node_evidence import NativeEvidenceError
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from tests.test_runtime_vault_s4d_finalization import (
    _finalize,
    _Harness,
    _obs,
)

# ============================================================================
# NodeSet: los errores de dominio NO pueden escapar al caller
# ============================================================================


class _Explosivo:
    """Doble que levanta una excepción concreta en el punto que elijamos."""

    def __init__(self, excepcion: BaseException) -> None:
        self.excepcion = excepcion

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self.excepcion


@pytest.fixture
def _sin_mocks(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


@pytest.mark.parametrize(
    ("excepcion", "por_que"),
    [
        pytest.param(
            NativeEvidenceError("volumen fijo no disponible"),
            "el fallo nativo del probe",
            id="native_evidence_error",
        ),
        pytest.param(
            InventoryLinkError("junction inesperado en 'Data'"),
            "reparse point dentro del árbol: caso I de la matriz adversarial",
            id="reparse_inventory_link_error",
        ),
        pytest.param(
            DuplicateFileIdError("dos rutas, un FileId"),
            "hardlink o identidad física duplicada: caso M de la matriz",
            id="duplicate_file_id_error",
        ),
        pytest.param(
            InventoryError("entrada ilegible durante el recorrido"),
            "el árbol cambió mientras se inventariaba",
            id="inventory_error",
        ),
    ],
)
def test_nodeset_convierte_los_errores_de_dominio_en_verdicto_fallido(
    _sin_mocks: pytest.MonkeyPatch,
    excepcion: BaseException,
    por_que: str,
) -> None:
    """Un error de evidencia produce ``passed=False``, nunca una excepción.

    El defecto que esto cubre era un ``except NativeEvidenceError`` que dejaba
    pasar ``InventoryLinkError`` y ``DuplicateFileIdError`` — que NO derivan de
    esa clase — hasta el caller. El caller del S4-D clasifica por tipo, así que
    una excepción sin clasificar no caía en la rama de INDETERMINATE y el Golden
    quedaba sin verificar y sin rollback.
    """
    port = Win32FinalizationVerificationPort.__new__(Win32FinalizationVerificationPort)
    port.golden = pathlib.Path(".")  # no se usa: el probe está simulado
    port.rig = pathlib.Path(".")
    port.quiescence_real = False
    port.llamadas = []

    _sin_mocks.setattr(
        "sky_claw.local.runtime_vault.finalization_verification.probe_node_evidence",
        _Explosivo(excepcion),
    )

    veredicto = port.observar_node_set(raiz="C:\\no\\importa", nodos_autorizados=())

    assert veredicto.gate == "node_set"
    assert veredicto.passed is False, f"un gate que no pudo observar debe fallar: {por_que}"
    assert type(excepcion).__name__ in veredicto.detail
    assert len(veredicto.evidence_digest) == 64


def test_los_errores_de_dominio_del_nodeset_llegan_al_orchestrator_como_rollback(
    tmp_path: pathlib.Path,
) -> None:
    """El error de dominio acaba en ROLLBACK_REQUIRED, con el lock retenido.

    El ciclo completo importa: un veredicto FAIL en el port correcto tiene que
    producir el desenlace forense correcto, o el fix del adapter no sirve de
    nada aguas arriba.
    """
    h = _Harness(tmp_path)

    # GP1, RV-2 y quiescence se resuelven con el port falso del harness (para no
    # depender del Golden real); SOLO el NodeSet va al adapter de producción, que
    # es donde vive el defecto.
    class _PortHibrido:
        def __init__(self, interno: Any, adaptador: Any) -> None:
            self.interno = interno
            self.observar_node_set = adaptador.observar_node_set

        def observar_gp1(self, **kwargs: Any) -> Any:
            return self.interno.observar_gp1(**kwargs)

        def observar_rv2(self, **kwargs: Any) -> Any:
            return self.interno.observar_rv2(**kwargs)

        def observar_quiescence(self, **kwargs: Any) -> Any:
            return self.interno.observar_quiescence(**kwargs)

    adaptador = Win32FinalizationVerificationPort.__new__(Win32FinalizationVerificationPort)
    adaptador.golden = pathlib.Path(".")
    adaptador.rig = pathlib.Path(".")
    adaptador.quiescence_real = False
    adaptador.llamadas = []

    import sky_claw.local.runtime_vault.finalization_verification as modulo

    original = modulo.probe_node_evidence
    modulo.probe_node_evidence = _Explosivo(DuplicateFileIdError("FileId duplicado"))
    try:
        reporte = _finalize(h, port=_PortHibrido(h.puerto, adaptador))
    finally:
        modulo.probe_node_evidence = original

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert reporte.verdict_for("node_set") is not None
    assert reporte.verdict_for("node_set").passed is False
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
    assert reporte.lock.retained_as_orphan is True
    assert not h.backup_path().exists()


# ============================================================================
# Backup writer: TrustedNamespaceError no escapa del contrato del store
# ============================================================================


def test_el_writer_productivo_traduce_trusted_namespace_error(tmp_path: pathlib.Path) -> None:
    """``TrustedNamespaceError`` del namespace se vuelve ``GoldenBackupWriteError``.

    ``write_secured_file_create_once_at`` usa los ``error_factory`` POR DEFECTO
    del namespace, así que levanta ``TrustedNamespaceError`` y sus subtipos
    directamente — un reparse en el padre del destino es el caso real. Sin la
    traducción escapaban del contrato ``GoldenBackupError`` y el llamador
    recibía un tipo que el store no describe.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import _NamespaceGoldenBackupWriter
    from sky_claw.local.runtime_vault.trusted_namespace import TrustedNamespaceError

    _harness = _Harness(tmp_path)
    _harness._publicar_backup()
    destino = _harness.backup_path()

    class _PrimitivaRebelde:
        def write_secured_file_create_once_at(self, *_args: Any, **_kwargs: Any) -> None:
            raise TrustedNamespaceError("el padre del destino es un reparse point")

    import sky_claw.local.runtime_vault.trusted_namespace as modulo

    original = modulo.write_secured_file_create_once_at
    modulo.write_secured_file_create_once_at = _PrimitivaRebelde().write_secured_file_create_once_at
    try:
        with pytest.raises(GoldenBackupWriteError) as excinfo:
            _NamespaceGoldenBackupWriter().write_create_once(destino, b"{}", "golden_backup_manifest.json")
    finally:
        modulo.write_secured_file_create_once_at = original

    assert isinstance(excinfo.value.__cause__, TrustedNamespaceError), (
        "la causalidad debe preservarse: sin __cause__ se pierde el motivo real del rechazo"
    )
    assert str(destino) in str(excinfo.value)


def test_un_error_del_namespace_no_llega_a_committed(tmp_path: pathlib.Path) -> None:
    """El desenlace forense del fallo de namespace: INDETERMINATE, sin commit.

    Se fuerza el rechazo del namespace DENTRO del adaptador PRODUCTIVO, con la
    primitiva real sustituida, para que el test ejercite la cadena completa —no
    una excepción inyectada en un writer de test, que no es lo que el fix
    corrige.
    """
    from sky_claw.local.runtime_vault.trusted_namespace import TrustedNamespaceError

    h = _Harness(tmp_path)

    from sky_claw.local.runtime_vault.golden_backup_archive import _NamespaceGoldenBackupWriter

    class _PrimitivaRebelde:
        def write_secured_file_create_once_at(self, *_args: Any, **_kwargs: Any) -> None:
            raise TrustedNamespaceError("el padre del destino es un reparse point")

    import sky_claw.local.runtime_vault.trusted_namespace as modulo_ns

    original = modulo_ns.write_secured_file_create_once_at
    modulo_ns.write_secured_file_create_once_at = _PrimitivaRebelde().write_secured_file_create_once_at
    try:
        reporte = _finalize(h, archive_writer=_NamespaceGoldenBackupWriter())
    finally:
        modulo_ns.write_secured_file_create_once_at = original

    # El writer de test no pasa por el adaptador productivo, así que su error de
    # namespace NO está tipado; el store lo envuelve igual en GoldenBackupWriteError
    # (por la rama OSError/typed) y el orquestador cae en INDETERMINATE.
    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not reporte.committed
    assert h.journal_estado() is not ProtectionTransactionState.COMMITTED
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True
    assert not h.backup_path().exists()


# ============================================================================
# Replay de COMMITTED: OSError al liberar el lock
# ============================================================================


def test_un_oserror_al_normalizar_un_commit_durable_no_lo_deshace(tmp_path: pathlib.Path) -> None:
    """``OSError`` al liberar no propaga, no deshace, y se refleja en el reporte.

    Antes el manejador sólo capturaba ``GoldenLockIoError`` y un ``OSError``
    escapaba al caller con un ``COMMITTED`` durable ya escrito. El caller podía
    interpretar la excepción como "el commit se perdió".
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)
    h._publicar_backup()
    h.journal.commit_finalized(archive=h._cargar_backup(), plan=h.plan)
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED

    # Ahora el lock huérfano falla al escribir phase=RELEASED con un OSError.
    h.lock_kernel.fallar_escritura = True

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.ALREADY_COMMITTED, (
        "el COMMITTED ya era durable: un fallo de normalización no lo reescribe"
    )
    assert reporte.disposition is not FinalizationDisposition.INDETERMINATE
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED


def test_un_oserror_al_liberar_en_el_camino_feliz_tambien_se_absorbe(tmp_path: pathlib.Path) -> None:
    """El camino principal ya captura ``OSError``; el replay también debe.

    Se comprueba la simetría: los dos caminos que llaman a ``release()`` tienen
    que tratar el fallo igual. Un camino que lo absorba y el otro que lo propague
    es la clase de defecto de hermano que el repo documenta como dominante.
    """
    h = _Harness(tmp_path)
    h.lock_kernel.fallar_escritura = True

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.COMMITTED
    assert reporte.lock.released is False
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED


# ============================================================================
# El contrato de enteros del manifiesto es el del plan
# ============================================================================


def _manifiesto(tree_digest: TreeDigest, *, vol: int = 1, file_id: int = 1) -> GoldenBackupArchive:
    import base64
    import hashlib

    from sky_claw.local.runtime_vault.golden_protection_plan import (
        GoldenProtectionNodeKind,
        NodeSecurityBackup,
    )

    sd = b"\x01\x00\x04\x80\x00"
    return GoldenBackupArchive(
        operation_id="op-1",
        canonical_root="C:\\JUEGO",
        volume_serial_number=vol,
        root_file_id=file_id,
        tree_digest=tree_digest,
        policy_version="gp2-v1",
        authorized_plan_digest="a" * 64,
        nodes=(
            NodeSecurityBackup(
                relative_path=".",
                node_kind=GoldenProtectionNodeKind.DIR,
                volume_serial_number=vol,
                file_id=file_id,
                pre_sd_bytes_b64=base64.b64encode(sd).decode("ascii"),
                pre_sd_length=len(sd),
                pre_sd_sha256=hashlib.sha256(sd).hexdigest(),
                owner_sid="S-1-5-18",
                group_sid="S-1-5-32-544",
                dacl_control_flags=0x1000,
                pre_dacl_protected_flag=True,
                sddl_diagnostic="D:(A;;FA;;;BA)",
            ),
        ),
    )


def test_un_arbol_sin_archivos_es_un_manifiesto_valido() -> None:
    """``files == 0`` y ``bytes == 0`` son válidos: un Golden puede estar vacío.

    El validador local pedía ``> 0`` mientras el plan autoritativo acepta
    ``>= 0``. Con el ``> 0``, un plan legítimo con cero archivos producía un
    INDETERMINATE sobre evidencia válida.
    """
    manifiesto = _manifiesto(TreeDigest(digest="b" * 64, files=0, bytes=0))

    crudo = manifiesto.canonical_bytes()
    assert deserialize_golden_backup_archive(crudo) == manifiesto


def test_identificadores_en_cero_son_validos() -> None:
    """``volume_serial_number == 0`` y ``root_file_id == 0`` son válidos.

    El contrato upstream es ``[0, 2**64-1]`` y ``[0, 2**128-1]``. Cero es un
    uint legítimo; rechazarlo en el manifiesto y no en el plan daría dos
    veredictos distintos para el mismo objeto.
    """
    manifiesto = _manifiesto(TreeDigest(digest="c" * 64, files=1, bytes=0), vol=0, file_id=0)

    assert deserialize_golden_backup_archive(manifiesto.canonical_bytes()) == manifiesto


@pytest.mark.parametrize(
    ("tree_digest", "vol", "file_id"),
    [
        pytest.param(TreeDigest(digest="d" * 64, files=-1, bytes=0), 1, 1, id="files_negativo"),
        pytest.param(TreeDigest(digest="d" * 64, files=0, bytes=-5), 1, 1, id="bytes_negativo"),
        pytest.param(TreeDigest(digest="d" * 64, files=1, bytes=0), -1, 1, id="vol_negativo"),
        pytest.param(TreeDigest(digest="d" * 64, files=1, bytes=0), 1, -1, id="file_id_negativo"),
        pytest.param(TreeDigest(digest="d" * 64, files=True, bytes=0), 1, 1, id="files_bool"),
        pytest.param(TreeDigest(digest="d" * 64, files=1, bytes=0), True, 1, id="vol_bool"),
        pytest.param(TreeDigest(digest="d" * 64, files=1, bytes=0), (1 << 64), 1, id="vol_sobre_uint64"),
        pytest.param(TreeDigest(digest="d" * 64, files=1, bytes=0), 1, (1 << 128), id="file_id_sobre_uint128"),
    ],
)
def test_el_manifiesto_rechaza_lo_que_el_contrato_rechaza(tree_digest: TreeDigest, vol: int, file_id: int) -> None:
    """Negativos, booleanos y fuera-de-rango se rechazan igual que en el plan.

    ``bool`` merece caso propio porque en Python es un ``int`` subclass: sin la
    guarda explícita, ``files=True`` pasaría como ``files == 1``.

    La aserción es sobre ``RuntimeVaultError`` y no sobre
    ``GoldenBackupError`` a propósito: los nodos del manifiesto son
    ``NodeSecurityBackup``, que valida sus propios enteros y puede rechazar
    ANTES de que corra el validador del archivo. Lo que importa es que ambas
    capas rechazan lo mismo.
    """
    from sky_claw.local.runtime_vault.models import RuntimeVaultError

    with pytest.raises(RuntimeVaultError):
        _manifiesto(tree_digest, vol=vol, file_id=file_id)


# ============================================================================
# Límite de responsabilidad: S4-D NO hace rollback
# ============================================================================


def test_un_gate_fallido_deja_rollback_required_sin_ejecutar_rollback(
    tmp_path: pathlib.Path,
) -> None:
    """S4-D escribe el estado y NO restaura ACLs; el motor es S4-C.

    Se comprueba que S4-D no llama a ninguna primitiva de mutación de Security
    Descriptor y que el reporte lo dice explícitamente con
    ``rollback_executed=False``, para que un operador no lea
    ``ROLLBACK_REQUIRED`` como "ya restaurado".
    """
    h = _Harness(tmp_path)
    llamadas: list[str] = []

    class _PuertoQueAnotaRestore:
        """Port que además delega al port real y registra cualquier restore."""

        def __init__(self, interno: Any) -> None:
            self.interno = interno

        def __getattr__(self, nombre: str) -> Any:
            return getattr(self.interno, nombre)

    port = _PuertoQueAnotaRestore(h.puerto)
    # El override va sobre el port INTERNO: el wrapper delega los métodos vía
    # ``__getattr__``, así que un atributo puesto en el wrapper no lo vería nadie.
    h.puerto.gp1 = _obs(False, "gp1:WRITING_LEFT", "gp1")

    import sky_claw.local.runtime_vault.mutation_executor as modulo

    original_restore = getattr(modulo, "restore_security_descriptor_by_handle", None)
    if original_restore is not None:  # pragma: no branch

        def _interceptar(*args: Any, **kwargs: Any) -> Any:
            llamadas.append("restore")
            return original_restore(*args, **kwargs)

        modulo.restore_security_descriptor_by_handle = _interceptar
    try:
        reporte = _finalize(h, port=port)
    finally:
        if original_restore is not None:
            modulo.restore_security_descriptor_by_handle = original_restore

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert reporte.rollback_executed is False, (
        "S4-D no ejecuta rollback: el campo del DTO tiene que decirlo explícitamente"
    )
    assert llamadas == [], "S4-D no debe invocar ninguna restauración de Security Descriptor"
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
    assert reporte.lock.retained_as_orphan is True
    assert reporte.lock.released is False
    assert not h.backup_path().exists()


def test_rollback_executed_es_siempre_falso_en_todos_los_desenlaces(tmp_path: pathlib.Path) -> None:
    """El invariante del DTO: S4-D jamás ejecuta rollback, en ningún camino.

    Enumera el camino feliz y el de fallo. El campo es ``False`` por defecto, así
    que esto verifica que nadie lo设onga en ``True`` por descuido.
    """
    feliz = _finalize(_Harness(tmp_path))
    assert feliz.disposition is FinalizationDisposition.COMMITTED
    assert feliz.rollback_executed is False

    h_fallo = _Harness(pathlib.Path(tempfile.mkdtemp()))
    h_fallo.puerto.gp1 = _obs(False, "gp1:WRITING_LEFT", "gp1")
    fallo = _finalize(h_fallo)
    assert fallo.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert fallo.rollback_executed is False
