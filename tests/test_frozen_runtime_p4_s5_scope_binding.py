"""P4-S5 — binding verificable ``ApprovalScope`` → transición.

Cierra el defecto P2 documentado en P4-S4: el journal comparaba un digest durable
contra un digest **aportado por el llamador**, sin demostrar que los campos de la
transición correspondían al scope autorizado. La reproducción ejecutable del
defecto está en el docstring de
``test_una_transicion_adulterada_no_consume_la_aprobacion``.

La propiedad que estos tests congelan:

    NINGUNA TRANSICIÓN PUEDE APROVECHAR UNA APROBACIÓN VÁLIDA PARA REALIZAR UNA
    OPERACIÓN O UTILIZAR ARTEFACTOS DISTINTOS DE LOS AUTORIZADOS.

Se verifica en dos niveles, y ambos se ejercitan acá:

1. **Antes del consumo irreversible** (``consumir_aprobacion``): el tombstone no
   se escribe si la transición declarada no liga el scope durable. Es el nivel que
   importa: el consumo es burn-first, así que un chequeo posterior llega tarde.
2. **Al crear el PENDING** (``_exigir_precondiciones_comunes``): la evidencia de
   consumo lleva la proyección DURABLE —leída del witness y verificada contra su
   payload—, no la del llamador, de modo que la comparación no es tautológica.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalOperation,
    ConsentimientoAprobacion,
    consumir_aprobacion,
    payload_scope_sin_digest,
    proponer_aprobacion,
    verificar_consumo_aprobacion,
)
from sky_claw.local.frozen_runtime.errors import (
    ApprovalIdentityError,
    ApprovalSchemaError,
    ScopeBindingError,
)
from sky_claw.local.frozen_runtime.journal import SourceActivation
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import (
    approval_ledger_path,
    approvals_state_dir,
    initialize_frozen_runtime_storage,
    issuances_state_dir,
)

WORKTREE = pathlib.Path(__file__).resolve().parents[1]
ENTORNO_HIJO = {
    **os.environ,
    "PYTHONPATH": str(WORKTREE),
    "SKYCLAW_P4_S5_WORKTREE": str(WORKTREE),
}
_HIJO = pathlib.Path(__file__).resolve().parent / "_p4_s5_child.py"

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
GEN_AJENA = "1.6.353.0__deadbeefcafe"
T1 = "8f4e1a90-5ac1-4b6d-9c21-0d2b8f4e1a90"
T2 = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = tmp_path / nombre
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success, resultado.admission.message
    return root


def _fuente(**overrides: object) -> SourceActivation:
    base = {
        "generation_id": GEN_ORIGEN,
        "clone_id": "clone-origen",
        "runtime_setup_id": "setup-origen",
        "effective_runtime_evidence": "b" * 64,
    }
    base.update(overrides)
    return SourceActivation(**base)  # type: ignore[arg-type]


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation": ApprovalOperation.PROMOTION,
        "source_activation": _fuente(),
        "generation_id": GEN_OBJETIVO,
        "clone_id": "clone-objetivo",
        "clone_evidence": "d" * 64,
        "runtime_setup_id": "setup-objetivo",
        "runtime_setup_evidence": "e" * 64,
        "compatibility_evidence_id": "f" * 64,
        "candidate_id": "cand-1",
        "approval_provenance": "ui:req-1",
    }
    base.update(overrides)
    return base


def _emitir_fixture(root: pathlib.Path, **overrides: object):
    """Emite por el núcleo durable con una fixture explícita de consentimiento.

    ``emitir_aprobacion`` falla cerrado a propósito (no hay superficie HITL): la
    única vía de emitir en un test es el núcleo bajo lock, con consentimiento
    declarado como fixture. El scope emitido es real y durable.
    """
    propuesta = proponer_aprobacion(root, **_kwargs(**overrides))  # type: ignore[arg-type]
    consentimiento = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture",
    )
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        approval_module._emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)
    return propuesta


def _transicion(propuesta, transition_id: str = T1, **overrides: object):
    """Transición LEGÍTIMA: proyecta exactamente el scope aprobado."""
    base: dict[str, object] = {
        "transition_id": transition_id,
        "operation": journal_module.JournalOperation.PROMOTION,
        "source_activation": propuesta.scope.source_activation,
        "source_activation_digest": propuesta.scope.source_activation_digest,
        "target_generation_id": propuesta.scope.generation_id,
        "target_clone_id": propuesta.scope.clone_id,
        "target_runtime_setup_id": propuesta.scope.runtime_setup_id,
        "candidate_id": propuesta.scope.candidate_id,
        "approval_scope_digest": propuesta.scope.approval_scope_digest,
        "evidence_digests": journal_module.EvidenceDigests(
            clone_evidence=propuesta.scope.clone_evidence,
            runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
            compatibility_evidence=propuesta.scope.compatibility_evidence_id,
        ),
        "expected_effective_path": f"E:/rt/versions/{propuesta.scope.generation_id}",
        "created_at_ns": 1_700_000_000_000_000_000,
    }
    base.update(overrides)
    return journal_module.PropuestaTransicion(**base)  # type: ignore[arg-type]


def _consumir(root: pathlib.Path, propuesta, transicion, transition_id: str = T1, **overrides: object):
    datos: dict[str, object] = {
        "approval_id": propuesta.scope.approval_id,
        "approval_seq": propuesta.scope.approval_seq,
        "approval_scope_digest": propuesta.scope.approval_scope_digest,
        "transition_id": transition_id,
        "transicion": transicion,
    }
    datos.update(overrides)
    return consumir_aprobacion(root, **datos)  # type: ignore[arg-type]


def _instantanea_state(root: pathlib.Path) -> dict[str, bytes]:
    """Bytes de todo ``state/`` menos el lock, que es estado del mutex.

    Indexado por ruta relativa: dos artefactos en directorios distintos pueden
    compartir basename y colapsarían en el dict.
    """
    return {
        ruta.relative_to(root).as_posix(): ruta.read_bytes()
        for ruta in root.joinpath("state").rglob("*")
        if ruta.is_file() and ruta.name != "frozen-runtime.lock"
    }


def _lanzar(modo: str, root: pathlib.Path, args: dict) -> dict:
    proceso = subprocess.run(  # noqa: S603
        [sys.executable, str(_HIJO), modo, str(root), json.dumps(args)],
        capture_output=True,
        timeout=180,
        env=ENTORNO_HIJO,
        check=False,
    )
    salida = proceso.stdout.decode("utf-8", "replace").strip().splitlines()
    assert salida, f"el hijo no escribió nada; stderr={proceso.stderr.decode('utf-8', 'replace')[-2000:]}"
    return json.loads(salida[-1])


# ---------------------------------------------------------------------------
# ADULTERACIÓN CAMPO POR CAMPO — el corazón del slice
# ---------------------------------------------------------------------------

#: Cada entrada adultera EXACTAMENTE un campo de la transición legítima, dejando
#: todo lo demás (incluido el ``approval_scope_digest`` copiado del scope) igual.
#: Es la forma del ataque real: el llamador conserva el digest válido y cambia
#: aquello que el digest debería haber ligado.
ADULTERACIONES: dict[str, dict[str, object]] = {
    "operation": {
        # ROLLBACK coherente consigo mismo (sin candidate): difiere del scope
        # PROMOTION en la operación Y en el candidate. Ambas diferencias las
        # declara la operación, así que las dos tienen que aparecer.
        "operation": journal_module.JournalOperation.ROLLBACK,
        "candidate_id": None,
    },
    "source_activation_digest": {"source_activation_digest": "1" * 64},
    "generation_id": {"target_generation_id": GEN_AJENA},
    "clone_id": {"target_clone_id": "clone-ADULTERADO"},
    "runtime_setup_id": {"target_runtime_setup_id": "setup-ADULTERADO"},
    "candidate_id": {"candidate_id": "cand-ADULTERADO"},
    "clone_evidence": {
        "evidence_digests": journal_module.EvidenceDigests(
            clone_evidence="9" * 64,
            runtime_setup_evidence="e" * 64,
            compatibility_evidence="f" * 64,
        )
    },
    "runtime_setup_evidence": {
        "evidence_digests": journal_module.EvidenceDigests(
            clone_evidence="d" * 64,
            runtime_setup_evidence="8" * 64,
            compatibility_evidence="f" * 64,
        )
    },
    "compatibility_evidence_id": {
        "evidence_digests": journal_module.EvidenceDigests(
            clone_evidence="d" * 64,
            runtime_setup_evidence="e" * 64,
            compatibility_evidence="7" * 64,
        )
    },
}


@pytest.mark.parametrize("campo", sorted(ADULTERACIONES))
def test_una_transicion_adulterada_no_consume_la_aprobacion(tmp_path: pathlib.Path, campo: str) -> None:
    """Ningún campo del scope puede sustituirse conservando el digest.

    Antes de P4-S5 los nueve casos pasaban: el journal comparaba
    ``aprobacion.scope_digest == propuesta.approval_scope_digest`` y **ambos los
    aportaba el llamador**, así que copiar el digest válido en una transición con
    otros artefactos bastaba para escribir un PENDING que el bind habría aplicado
    sobre material no aprobado.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    adulterada = _transicion(propuesta, **ADULTERACIONES[campo])

    with pytest.raises(ScopeBindingError, match=campo):
        _consumir(root, propuesta, adulterada)

    # El rechazo ocurre ANTES del write-ahead: no hay evidencia terminal.
    assert list(approvals_state_dir(root).iterdir()) == []


def test_el_digest_copiado_no_autoriza_artefactos_distintos(tmp_path: pathlib.Path) -> None:
    """Reproducción literal del defecto P2, ahora rechazada.

    Aprobación real para ``1.6.640.0`` / ``clone-objetivo`` / evidencia ``d…``.
    La transición cambia TODO el material —generación, clone, setup y las tres
    evidencias— y conserva únicamente el ``approval_scope_digest``. Es el ataque
    que el digest-que-aporta-el-llamador no podía ver.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    adulterada = _transicion(
        propuesta,
        target_generation_id=GEN_AJENA,
        target_clone_id="clone-ADULTERADO",
        target_runtime_setup_id="setup-ADULTERADO",
        evidence_digests=journal_module.EvidenceDigests(
            clone_evidence="9" * 64,
            runtime_setup_evidence="8" * 64,
            compatibility_evidence="7" * 64,
        ),
    )
    assert adulterada.approval_scope_digest == propuesta.scope.approval_scope_digest

    with pytest.raises(ScopeBindingError) as excinfo:
        _consumir(root, propuesta, adulterada)
    mensaje = str(excinfo.value)
    for campo in (
        "generation_id",
        "clone_id",
        "runtime_setup_id",
        "clone_evidence",
        "runtime_setup_evidence",
        "compatibility_evidence_id",
    ):
        assert campo in mensaje, f"la discrepancia en {campo} no se reportó: {mensaje}"


def test_el_rechazo_por_binding_no_escribe_nada(tmp_path: pathlib.Path) -> None:
    """Ausencia de escrituras ante rechazo: ni tombstone, ni witness nuevo, ni CAS.

    Se comparan los BYTES de ``state/`` antes y después. Un rechazo que deja la
    caché del ledger avanzada —o un tombstone huérfano— convertiría la aprobación
    en terminal sin transición, que es el modo de falla que el burn-first existe
    para evitar.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    antes = _instantanea_state(root)

    with pytest.raises(ScopeBindingError):
        _consumir(root, propuesta, _transicion(propuesta, target_generation_id=GEN_AJENA))

    assert _instantanea_state(root) == antes


# ---------------------------------------------------------------------------
# El digest durable deja de ser una afirmación: se recomputa al leer
# ---------------------------------------------------------------------------


def test_el_witness_se_verifica_contra_su_payload(tmp_path: pathlib.Path) -> None:
    """Un witness con payload manipulado y digest intacto se rechaza al leer.

    ``root_id`` ya se verificaba al leer (P4-S4). El scope hace lo mismo: el
    witness guarda los 13 campos canónicos y el digest se **recomputa** desde
    ellos. Un campo durable que sólo se escribe es decoración.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    payload["scope_payload"]["clone_evidence"] = "9" * 64
    ruta.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ApprovalIdentityError, match="scope_payload"):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_witness_persiste_los_trece_campos_canonicos(tmp_path: pathlib.Path) -> None:
    """El witness durable lleva el scope completo, no sólo su digest."""
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    payload = json.loads(ruta.read_text(encoding="utf-8"))

    assert payload["scope_payload"] == payload_scope_sin_digest(propuesta.scope)


def test_el_scope_payload_tiene_esquema_cerrado(tmp_path: pathlib.Path) -> None:
    """Un payload con un campo de MÁS se rechaza, no se ignora.

    Un esquema abierto dejaría entrar campos que el digest no compromete: el
    ``approval_scope_digest`` se computa sobre las 13 claves canónicas, así que
    una clave extra sería material aprobado que nadie aprobó. El campo agregado
    acá no altera el digest (no está en el payload canónico), así que sólo el
    chequeo de forma lo atrapa.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    ruta = issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json"
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    payload["scope_payload"]["campo_de_mas"] = "x"
    ruta.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ApprovalSchemaError, match="scope_payload"):
        approval_module.derivar_autoridad_aprobacion(root)


# ---------------------------------------------------------------------------
# Replay / root ajeno
# ---------------------------------------------------------------------------


def test_el_replay_no_puede_reutilizar_la_aprobacion(tmp_path: pathlib.Path) -> None:
    """Single-use (§35.2): consumida para T1, no habilita T2 — ni con binding válido."""
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)

    with pytest.raises(ApprovalIdentityError, match="CONSUMED|consumida|otra transición"):
        _consumir(root, propuesta, _transicion(propuesta, T2), T2)


def test_el_reintento_idempotente_tambien_verifica_el_binding(tmp_path: pathlib.Path) -> None:
    """El reintento con la MISMA transición es idempotente; con otro material, no.

    El chequeo corre antes del branch de estado: si estuviera después, un
    reintento adulterado se colaría por la rama «ya consumido».
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    legitima = _transicion(propuesta, T1)
    primero = _consumir(root, propuesta, legitima, T1)
    segundo = _consumir(root, propuesta, legitima, T1)
    assert segundo.consumed_by_transition_id == primero.consumed_by_transition_id

    with pytest.raises(ScopeBindingError):
        _consumir(root, propuesta, _transicion(propuesta, T1, target_generation_id=GEN_AJENA), T1)


def _trasplantar_evidencia(origen: pathlib.Path, destino: pathlib.Path) -> None:
    """Copia los TRES artefactos de la autoridad de un root a otro.

    Es el escenario que el cross-check de ``root_id`` existe para atajar: no un
    archivo suelto, sino el ``state/`` completo de la autoridad —ledger, cadena
    de witnesses y tombstones— movido a otro FrozenRuntimeRoot. Todo es
    internamente coherente, así que ninguna validación de forma lo distingue.
    """
    shutil.copytree(issuances_state_dir(origen), issuances_state_dir(destino), dirs_exist_ok=True)
    shutil.copytree(approvals_state_dir(origen), approvals_state_dir(destino), dirs_exist_ok=True)
    shutil.copy2(approval_ledger_path(origen), approval_ledger_path(destino))


def test_la_evidencia_de_otro_root_no_autoriza(tmp_path: pathlib.Path) -> None:
    """Un scope emitido en otro root no liga nada acá (§40.4).

    El ``root_id`` se verifica AL LEER el witness (P4-S4) y el binding corre
    después, así que el rechazo tiene que ser de root y no una discrepancia de
    artefactos: confundirlos mandaría al operador a buscar un cambio de material
    donde hubo un trasplante de estado.
    """
    origen = _root(tmp_path, "origen")
    destino = _root(tmp_path, "destino")
    propuesta = _emitir_fixture(origen)
    _trasplantar_evidencia(origen, destino)

    from sky_claw.local.frozen_runtime.errors import ApprovalRootAjenoError

    with pytest.raises(ApprovalRootAjenoError, match="root_id"):
        _consumir(destino, propuesta, _transicion(propuesta, T1), T1)
    assert list(approvals_state_dir(destino).iterdir()) == []


# ---------------------------------------------------------------------------
# Concurrencia real (procesos)
# ---------------------------------------------------------------------------


def test_dos_consumos_concurrentes_dejan_una_sola_evidencia(tmp_path: pathlib.Path) -> None:
    """Dos procesos compiten: la legítima gana, la adulterada no escribe nada.

    Se corre en procesos separados porque el root lock es cross-process: dos
    hilos del mismo proceso no ejercitarían la garantía que se quiere probar.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    comun = {
        "approval_id": propuesta.scope.approval_id,
        "approval_seq": propuesta.scope.approval_seq,
        "digest": propuesta.scope.approval_scope_digest,
        "transition_id": T1,
        "lock_timeout": 60.0,
        "scope": payload_scope_sin_digest(propuesta.scope),
    }
    legitima = _lanzar("consumir", root, {**comun, "override": {}})
    adulterada = _lanzar("consumir", root, {**comun, "override": {"target_generation_id": GEN_AJENA}})

    assert legitima["ok"] is True, legitima
    assert adulterada["ok"] is False, adulterada
    assert adulterada["error_type"] == "ScopeBindingError", adulterada

    tombstones = list(approvals_state_dir(root).iterdir())
    assert len(tombstones) == 1
    terminal = json.loads(tombstones[0].read_text(encoding="utf-8"))
    assert terminal["consumed_by_transition_id"] == T1


# ---------------------------------------------------------------------------
# El journal: la evidencia de consumo lleva la proyección DURABLE
# ---------------------------------------------------------------------------


def test_el_journal_rechaza_un_pending_que_no_liga_el_scope(tmp_path: pathlib.Path) -> None:
    """Segundo nivel: aun con un consumo válido, el PENDING tiene que ligar.

    La evidencia que devuelve ``verificar_consumo_aprobacion`` lleva la
    proyección leída del witness. Si el journal comparara sólo digests, una
    transición adulterada con el digest correcto pasaría igual.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)
    evidencia = verificar_consumo_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        transition_id=T1,
        transicion=_transicion(propuesta, T1),
    )
    with pytest.raises(ScopeBindingError, match="generation_id"):
        journal_module._crear_pendiente_bajo_lock(
            root, _transicion(propuesta, T1, target_generation_id=GEN_AJENA), evidencia
        )


def test_la_evidencia_de_consumo_proyecta_el_scope_durable(tmp_path: pathlib.Path) -> None:
    """La proyección de la evidencia sale del witness, no del llamador."""
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)
    evidencia = verificar_consumo_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        transition_id=T1,
        transicion=_transicion(propuesta, T1),
    )
    assert evidencia.proyeccion == approval_module.proyectar_scope(propuesta.scope)


def test_el_pending_persiste_el_candidate_aprobado(tmp_path: pathlib.Path) -> None:
    """El candidate forma parte del scope y queda en el documento durable.

    Un campo que sólo vive en memoria no se puede auditar después: el registro
    FINALIZED es la evidencia de qué se aplicó.
    """
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)
    evidencia = verificar_consumo_aprobacion(
        root,
        approval_id=propuesta.scope.approval_id,
        approval_seq=propuesta.scope.approval_seq,
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        transition_id=T1,
        transicion=_transicion(propuesta, T1),
    )
    doc = journal_module._crear_pendiente_bajo_lock(root, _transicion(propuesta, T1), evidencia)
    assert doc.candidate_id == "cand-1"

    releido = journal_module.cargar_journal_transicion(root)
    assert releido.journal is not None
    assert releido.journal.candidate_id == "cand-1"


# ---------------------------------------------------------------------------
# Ancla que ENUMERA (no muestrea)
# ---------------------------------------------------------------------------


def test_ancla_la_proyeccion_cubre_todo_campo_del_scope() -> None:
    """Congela la correspondencia scope ↔ transición.

    No muestrea un campo: enumera los 13 del scope canónico y exige que cada uno
    esté ligado o explícitamente excluido. Agregar un campo al ``ApprovalScope``
    rompe este test hasta que se decida —y se cablee— cómo se liga.
    """
    del_scope = set(payload_scope_sin_digest(_scope_de_referencia()))
    ligados = set(journal_module.CAMPOS_DEL_SCOPE_LIGADOS)
    excluidos = set(journal_module.CAMPOS_DEL_SCOPE_SIN_TRANSICION)

    assert ligados | excluidos == del_scope, (
        f"campos del scope sin decisión de ligadura: {sorted(del_scope - ligados - excluidos)}"
    )
    assert not (ligados & excluidos), sorted(ligados & excluidos)
    # La proyección no puede tener campos de más: cada uno tiene que venir de un
    # campo del scope, o la comparación incluiría algo que el scope no compromete.
    assert set(journal_module.CAMPOS_DEL_SCOPE_LIGADOS.values()) == set(
        journal_module.ProyeccionScope.__dataclass_fields__
    ), "la proyección y el mapa de ligadura divergieron"
    # El esquema CERRADO del witness durable es otra lista literal de los mismos
    # 13 campos. Si diverge del payload canónico, todo witness nuevo se vuelve
    # ilegible al releerlo — y ese fallo aparecería recién en producción.
    assert del_scope == approval_module._CAMPOS_SCOPE_PAYLOAD, (
        "el esquema del witness divergió de payload_scope_sin_digest: "
        f"{sorted(approval_module._CAMPOS_SCOPE_PAYLOAD ^ del_scope)}"
    )


def test_ancla_las_exclusiones_estan_verificadas_no_justificadas() -> None:
    """Cada exclusión tiene un chequeo REAL que la cubre, y acá se ejerce.

    ``source_activation`` no está en la proyección porque la liga su digest — y
    eso se verifica: cambiar el contenido dejando el digest viejo se rechaza.
    ``approval_provenance`` la cruza el consumo contra el ledger.
    """
    assert frozenset({"source_activation", "approval_provenance"}) == journal_module.CAMPOS_DEL_SCOPE_SIN_TRANSICION

    scope = _scope_de_referencia()
    # El digest liga el CONTENIDO del source, no su forma: si cambia el contenido
    # y se conserva el digest, el scope deja de ser válido.
    adulterado = approval_module.ApprovalScope(
        **{
            **{campo: getattr(scope, campo) for campo in scope.__dataclass_fields__},
            "source_activation": SourceActivation(
                generation_id=GEN_ORIGEN,
                clone_id="clone-ORIGEN-ADULTERADO",
                runtime_setup_id="setup-origen",
                effective_runtime_evidence="b" * 64,
            ),
        }
    )
    with pytest.raises(ApprovalIdentityError, match="source_activation_digest"):
        approval_module.validar_scope(adulterado)


def _scope_de_referencia():
    """Scope válido en memoria, sin tocar disco (para las anclas de censo)."""
    return approval_module.construir_scope(
        operation=ApprovalOperation.PROMOTION,
        source_activation=_fuente(),
        generation_id=GEN_OBJETIVO,
        clone_id="clone-objetivo",
        clone_evidence="d" * 64,
        runtime_setup_id="setup-objetivo",
        runtime_setup_evidence="e" * 64,
        compatibility_evidence_id="f" * 64,
        candidate_id="cand-1",
        approval_id=T1,
        approval_seq=1,
        approval_provenance="ui:req-1",
    )


# ---------------------------------------------------------------------------
# El slice NO habilita nada
# ---------------------------------------------------------------------------


def test_el_binding_no_habilita_la_activacion_productiva(tmp_path: pathlib.Path) -> None:
    """P4-S5 liga, no activa: PROMOTION/ROLLBACK productivos siguen fallando cerrado."""
    from sky_claw.local.frozen_runtime.errors import (
        ApprovalHitolNoDisponibleError,
        JournalDependenciaNoDisponibleError,
    )

    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)

    with pytest.raises(JournalDependenciaNoDisponibleError):
        journal_module.iniciar_transicion_pendiente(root, _transicion(propuesta, T1))
    with pytest.raises(JournalDependenciaNoDisponibleError):
        journal_module.iniciar_transicion_rollback(root, _transicion(propuesta, T1))
    with pytest.raises(ApprovalHitolNoDisponibleError):
        approval_module.emitir_aprobacion(root, proponer_aprobacion(root, **_kwargs()), None)


def test_el_slice_no_toca_el_runtime_efectivo(tmp_path: pathlib.Path) -> None:
    """Todo lo que escribe P4-S5 vive bajo ``state/``."""
    root = _root(tmp_path)
    propuesta = _emitir_fixture(root)
    _consumir(root, propuesta, _transicion(propuesta, T1), T1)
    archivos = sorted(ruta.relative_to(root).as_posix() for ruta in root.rglob("*") if ruta.is_file())
    assert all(ruta.startswith("state/") for ruta in archivos), archivos
