"""Tests causales del candidate manifest productivo (P2 de S4-E closure)."""

import hashlib
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.authorized_plan_store import (
    CANDIDATE_MANIFEST_FILE_NAME,
    CandidateManifestPublishError,
    derive_candidate_manifest_path,
    publish_candidate_manifest,
    read_candidate_manifest_bytes,
)
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
    FinalizationForensicReport,
    FinalizationLockOutcome,
    FinalizationPhase,
)
from sky_claw.local.runtime_vault.planning_orchestrator import GP2PlanningDisposition
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState

_OP = "3f2b1c8e-9a4d-4f5e-8b7a-1c2d3e4f5a6b"


# ===========================================================================
# Fakes locales. Viven acá y no en un modulo compartido porque `tests/` no
# esta en sys.path: un `import` entre modulos de test seria fragil.
# ===========================================================================


class _JournalFalso:
    """Contrato real de ``DurableProtectionJournal`` para el ``finally`` de S4-E."""

    def __init__(self) -> None:
        self._cerrado = False

    @property
    def is_closed(self) -> bool:
        return self._cerrado

    def close(self) -> None:
        self._cerrado = True


class _HandleFalso:
    def __init__(self) -> None:
        self.closed = False
        self.eventos: list[str] = []

    def release(self) -> bool:
        if self.closed:
            return False
        self.closed = True
        self.eventos.append("release")
        return True

    def retain_for_inspection(self) -> bool:
        if self.closed:
            return False
        self.closed = True
        self.eventos.append("retain")
        return True


class _TokenFalso:
    def close(self) -> bool:
        return True


class _SesionFalsa:
    def __init__(self) -> None:
        self.lock = _HandleFalso()
        self.operator_token = _TokenFalso()
        self.closed = False

    def close(self) -> bool:
        self.closed = True
        self.lock.release()
        self.operator_token.close()
        return True


def _planning_exitoso(*args: Any, **kwargs: Any) -> Any:
    """Planning que devuelve un plan sellado con bytes canónicos no vacíos."""

    class _PlanSellado:
        candidate_manifest_bytes = b'{"operation_id":"fake","nodos":[]}'
        staging_digest = "0" * 64

    return SimpleNamespace(
        disposition=GP2PlanningDisposition.PREPARED,
        success=True,
        message="",
        sealed_plan=_PlanSellado(),
    )


def _autorizacion_falsa() -> Any:
    return svc.ProtectionAuthorizationInputs(
        launch_request=object(),
        expected_coordinator=object(),
        coordinator_probe_provider=object(),
        elevation_case=object(),
        ppsc_provider=object(),
        ppsc_payload=object(),
        volume_serial_number=1,
        root_file_id=2,
    )


class _ReporteApplyFalso:
    def __init__(self, *, apply_error: str | None = None, rollback_error: str | None = None) -> None:
        self.operation_id = _OP
        self.transaction_state = ProtectionTransactionState.APPLYING
        self.apply_error = apply_error
        self.rollback_error = rollback_error
        self.rolled_back_nodes: tuple[str, ...] = ()
        self.setsecurityinfo_calls = 3
        self.outcomes: tuple[Any, ...] = ()


def _reporte_finalizacion(
    disposition: FinalizationDisposition = FinalizationDisposition.COMMITTED,
) -> Any:
    comiteado = disposition is FinalizationDisposition.COMMITTED
    return FinalizationForensicReport(
        operation_id=_OP,
        disposition=disposition,
        phase_reached=FinalizationPhase.COMMITTED if comiteado else FinalizationPhase.ENTRY,
        verdicts=(),
        lock=FinalizationLockOutcome(
            acquired=True,
            released=comiteado,
            retained_as_orphan=not comiteado,
            operation_id=_OP,
        ),
        journal_state=ProtectionTransactionState.COMMITTED if comiteado else ProtectionTransactionState.INDETERMINATE,
        archive_digest="a" * 64 if comiteado else None,
        authorized_plan_digest="b" * 64,
        fail_closed_reason="" if comiteado else "evidencia ambigua",
    )


def test_candidate_manifest_replay_identico_es_idempotente(tmp_path: pathlib.Path) -> None:
    """Replay con los MISMOS bytes → éxito idempotente, sin reescribir.

    Es lo que evita que un crash entre la publicación y la autorización
    convierta la operación en un bloqueo permanente: el retry con la misma
    `operation_id` y el mismo plan republica los mismos bytes y sigue.

    El arbitraje NO es ``dest.exists()``: es el create-once atómico. Si
    ganamos, se escribe; si perdemos, se relee y se compara. Este test no
    puede distinguir esos dos caminos por sí solo —lo que congela el arbitraje
    es `test_el_arbitro_es_la_publicacion_atomica`— pero sí congela el
    resultado observable.
    """
    payload = b'{"operation_id":"' + _OP.encode() + b'","nodos":[]}'
    first = publish_candidate_manifest(_OP, payload, programdata_resolver=_resolver(tmp_path))
    segundo = publish_candidate_manifest(_OP, payload, programdata_resolver=_resolver(tmp_path))

    assert first == segundo
    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == payload


def test_candidate_manifest_replay_distinto_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Replay con bytes DIFERENTES → conflicto fail-closed, y NO se sustituye.

    Sustituir un manifest ya publicado dejaría que una segunda planificación se
    aprobara sobre el nombre de la primera. El contenido original tiene que
    quedar intacto.
    """
    publish_candidate_manifest(_OP, b"manifest-A", programdata_resolver=_resolver(tmp_path))

    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(_OP, b"manifest-B", programdata_resolver=_resolver(tmp_path))

    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == b"manifest-A"


def test_el_arbitro_es_la_publicacion_atomica(tmp_path: pathlib.Path) -> None:
    """El writer NO consulta ``dest.exists()`` antes de publicar.

    Congela el arbitraje por comportamiento observable, no por lectura de
    fuente: se pasa un writer cuyo destino aparece ENTRE la derivación y la
    publicación, y se verifica que el resultado es el de la reconciliación
    (bytes ajenos → conflicto), no el de un fast-path que hubiera devuelto
    éxito sin mirar.
    """
    from sky_claw.local.runtime_vault.staging_writer import StagingCreateOnceWriter

    intruso = b"escrito-por-otro-proceso"
    dest_preparado = derive_candidate_manifest_path(_OP, programdata_resolver=_resolver(tmp_path))
    dest_preparado.parent.mkdir(parents=True, exist_ok=True)
    dest_preparado.write_bytes(intruso)

    writer = StagingCreateOnceWriter()
    with pytest.raises(FileExistsError):
        writer.write_create_once(dest_preparado, b"mio", CANDIDATE_MANIFEST_FILE_NAME)

    # El fast-path `dest.exists()` habría raising FileExistsError IGUAL, así
    # que este test separa la otra mitad: el arbitraje de la reconciliación.
    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(_OP, b"mio", programdata_resolver=_resolver(tmp_path))
    assert dest_preparado.read_bytes() == intruso


def test_el_replay_idempotente_no_depende_de_un_fast_path_de_existencia(
    tmp_path: pathlib.Path,
) -> None:
    """Con un writer stub que SIEMPRE pierde, el replay idéntico igual prospera.

    Si la idempotencia dependiera de ``dest.exists()``, un writer que nunca
    escribe (y por lo tanto nunca "gana") haría fallar el replay. Con la
    reconciliación por re-lectura, el resultado depende sólo de los bytes.
    """
    from sky_claw.local.runtime_vault.authorized_plan_store import _default_staging_writer  # noqa: F401

    payload = b"estables"
    publish_candidate_manifest(_OP, payload, programdata_resolver=_resolver(tmp_path))

    class _WriterQueSiemprePierde:
        def write_create_once(self, dest, data, name):  # noqa: ANN001, ANN201, ARG002
            raise FileExistsError(dest)

    # Idempotente: el segundo叫ayi con los mismos bytes reconcilia.
    publish_candidate_manifest(
        _OP, payload, programdata_resolver=_resolver(tmp_path), staging_writer=_WriterQueSiemprePierde()
    )
    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == payload

    # Y con bytes distintos falla cerrado, también sin ganar nunca.
    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(
            _OP, b"otro", programdata_resolver=_resolver(tmp_path), staging_writer=_WriterQueSiemprePierde()
        )


def _resolver(tmp_path: pathlib.Path) -> Any:
    return lambda: tmp_path / "programdata"


# --------------------------------------------------------------------------
# La primitive
# --------------------------------------------------------------------------


def test_publica_y_s4a_relee_los_mismos_bytes(tmp_path: pathlib.Path) -> None:
    """M-CM1/M-CM2: lo publicado es exactamente lo que S4-A relee.

    Este es el contrato que hace que la promoción sea real: S4-A no recibe el
    plan en memoria, RELee los bytes de staging y los revalida contra el digest
    del plan. Si la publicación escribiera otra cosa, o otra cosa después, el
    digest no coincidiría y la promoción fallaría cerrada.
    """
    payload = b'{"operation_id":"' + _OP.encode() + b'","nodos":[]}'
    dest = publish_candidate_manifest(_OP, payload, programdata_resolver=_resolver(tmp_path))

    assert dest.name == CANDIDATE_MANIFEST_FILE_NAME
    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == payload
    # El digest que S4-A compara es el de los bytes PUBLICADOS, no el de otra cosa.
    assert (
        hashlib.sha256(payload).hexdigest()
        == hashlib.sha256(read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path))).hexdigest()
    )


def test_es_create_once_no_sustituye(tmp_path: pathlib.Path) -> None:
    """Un replay con contenido DISTINTO falla cerrado en vez de pisar evidencia."""
    publish_candidate_manifest(_OP, b"original", programdata_resolver=_resolver(tmp_path))

    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(_OP, b"otro", programdata_resolver=_resolver(tmp_path))

    # Y el contenido original sigue intacto: no se sustituyó.
    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == b"original"


def test_rechaza_bytes_vacios(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(_OP, b"", programdata_resolver=_resolver(tmp_path))


def test_la_ruta_se_deriva_internamente(tmp_path: pathlib.Path) -> None:
    """La API no acepta un path: la derivación es la MISMA que usa la lectura.

    Que publicar y leer compartan derivación es lo que impide que se diverjan:
    si el caller pudiera elegir dónde se escribe, la lectura podría ir a otro
    sitio y la promoción fallaría sin que nadie entienda por qué.
    """
    dest = publish_candidate_manifest(_OP, b"x", programdata_resolver=_resolver(tmp_path))
    assert dest == derive_candidate_manifest_path(_OP, programdata_resolver=_resolver(tmp_path))
    assert dest.parent.name == _OP
    assert dest.parent.parent.name == "staging"


def test_rechaza_reparse_en_la_ruta_de_staging(tmp_path: pathlib.Path) -> None:
    """M-CM3: un enlace en la ruta de staging falla cerrado sin publicar.

    Staging es UNTRUSTED, pero "untrusted" no significa "cualquiera puede
    redirigir la escritura": un enlace en `staging/<op>` mandaría el manifest
    fuera del namespace.

    **La ruta se DERIVA, no se supone.** Una versión anterior de este test
    construía el enlace a mano en `<programdata>/runtime_vault/staging/<op>`,
    pero la ruta real lleva un segmento `Sky-Claw/` más:
    `<programdata>/Sky-Claw/runtime_vault/staging/<op>`. El enlace quedaba en
    un directorio que la primitive nunca mira. Localmente eso no se notó porque
    crear el symlink exige privilegio y el test se saltaba ANTES de validar
    nada — o sea, el anchor llevaba un tiempo sin ejercitarse. En el runner de
    CI el symlink sí se crea, y el fallo se-manifestó como "DID NOT RAISE".
    """
    fuera = tmp_path / "fuera-del-namespace"
    fuera.mkdir()
    padre_real = derive_candidate_manifest_path(_OP, programdata_resolver=_resolver(tmp_path)).parent
    padre_real.mkdir(parents=True)

    try:
        padre_real.rmdir()
        padre_real.symlink_to(fuera, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - sin privilegio de symlink
        pytest.skip(f"la plataforma no permite crear el enlace de prueba: {exc}")

    with pytest.raises(CandidateManifestPublishError):
        publish_candidate_manifest(_OP, b"x", programdata_resolver=_resolver(tmp_path))

    assert list(fuera.iterdir()) == [], "no se publicó nada fuera del namespace"


def test_el_candidate_de_otra_operacion_no_sirve(tmp_path: pathlib.Path) -> None:
    """M-CM4: cada operación lee SUS bytes del SU staging.

    Publicar el manifest de A dentro del directorio de B sería la vía para que
    S4-A de B leyera el plan de A. La derivación por `operation_id` lo
    impide: cada una tiene su directorio y su archivo.
    """
    otra = "11111111-2222-3333-4444-555555555555"
    publish_candidate_manifest(_OP, b"manifesto-de-A", programdata_resolver=_resolver(tmp_path))
    publish_candidate_manifest(otra, b"manifesto-de-B", programdata_resolver=_resolver(tmp_path))

    assert read_candidate_manifest_bytes(_OP, programdata_resolver=_resolver(tmp_path)) == b"manifesto-de-A"
    assert read_candidate_manifest_bytes(otra, programdata_resolver=_resolver(tmp_path)) == b"manifesto-de-B"


# --------------------------------------------------------------------------
# El cableado: S4-E publica ANTES de abrir la frontera
# --------------------------------------------------------------------------


def test_s4e_publica_antes_de_abrir_la_frontera(monkeypatch: pytest.MonkeyPatch) -> None:
    """M-CM1 a nivel de servicio: sin la publicación, el fresh path no avanza.

    Antes de P2, `promote_durable_authorized_plan` fallaba con
    `CandidateManifestUnavailableError` en el primer `protect_golden_root`
    productivo. El orden importa: publicar antes de la frontera evita abrirla
    para una promoción que va a fallar.
    """
    orden: list[str] = []

    def _fake_publish(operation_id: str, manifest_bytes: bytes, **kwargs: Any) -> None:
        orden.append("publish")
        assert manifest_bytes, "se publican bytes no vacíos"

    def _fake_auth(**kwargs: Any) -> Any:
        orden.append("auth")
        return object(), _SesionFalsa()

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "publish_candidate_manifest", _fake_publish)
    monkeypatch.setattr(svc, "establish_privileged_authorization", _fake_auth)
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: orden.append("promote") or object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: _JournalFalso())
    monkeypatch.setattr(svc, "apply_authorized_plan", lambda **k: _ReporteApplyFalso())
    monkeypatch.setattr(svc, "build_default_verification_port", lambda: object())
    monkeypatch.setattr(svc, "finalize_protection_transaction", lambda **k: _reporte_finalizacion())

    svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OP,
        authorization=_autorizacion_falsa(),
    )

    assert orden == ["publish", "auth", "promote"], orden


def test_si_la_publicacion_falla_no_se_abre_la_frontera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: sin manifest durable, NO se abre la frontera privileged.

    Abrir la frontera (y con ella tomar el GoldenMutationLock) para una
    promoción que va a fallar deja un lock tomado y ningún plan detrás.
    """
    abierta: list[str] = []

    def _publica_y_falla(*args: Any, **kwargs: Any) -> None:
        raise CandidateManifestPublishError("disco lleno")

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "publish_candidate_manifest", _publica_y_falla)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: abierta.append("auth"))

    resultado = svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OP,
        authorization=_autorizacion_falsa(),
    )

    assert abierta == [], "se abrió la frontera sin manifest durable"
    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.committed is False
    assert resultado.fail_closed_reason
