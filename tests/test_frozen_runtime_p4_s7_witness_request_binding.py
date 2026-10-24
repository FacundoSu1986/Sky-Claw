"""P4-S7 — binding durable de la solicitud HITL en el witness v3 (ADR 0012 §47).

Cierra ``OWNER_REQUEST_ID_IS_DURABLE = NO`` (§46.5). Hasta P4-S6 el
``solicitud_id`` viajaba dentro de ``approval_provenance``, que es un string
**opaco que pone el llamador**: se persistía y el digest lo cubría, pero nada
verificaba que lo contuviera. Después de un reinicio no se podía responder «qué
solicitud HITL se aprobó» sin consultar un registro que vive en memoria y muere
con el proceso.

La propiedad que estos tests congelan:

    REQUEST_PRESENTED == REQUEST_APPROVED == REQUEST_PERSISTED

y la condición que la hace no trivial: **copiar el ``solicitud_id`` que trae el
consentimiento no alcanza**. Un ``ConsentimientoAprobacion`` es un dataclass
público; si el witness se limitara a copiar su campo, el campo seguiría siendo
una aserción del llamador mudada un nivel más abajo — el mismo defecto que P4-S4
cerró para ``root_id`` y P4-S5 para ``digest_canonico``.

Por eso el binding durable no es un string: es un objeto acuñado por un productor
privado (``approval._acuñar_binding_solicitud``), con sello comparado por
identidad, y la puerta durable lo **verifica campo por campo** contra el scope
propuesto, el root físico y el propio consentimiento antes del ``CREATE``. El
lector recomputa su digest: un campo que sólo se escribe es decoración.

**Lo que este slice NO hace** (declarado, no insinuado): no hay firma ni HMAC, y
guardar el ``solicitud_id`` **no** es autenticación criptográfica del propietario
(§46.5 sigue vigente). El modelo de amenaza no cambia: el código arbitrario en el
proceso y la escritura arbitraria sobre el filesystem no quedan resueltos acá.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import owner_consent
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    ApprovalOperation,
    ConsentimientoAprobacion,
    _emitir_aprobacion_bajo_lock,
    cargar_ledger_aprobacion,
    proponer_aprobacion,
)
from sky_claw.local.frozen_runtime.errors import (
    ApprovalCasError,
    ApprovalConsentimientoError,
    ApprovalHitolNoDisponibleError,
    ApprovalIdentityError,
    ApprovalSchemaError,
    ConsentimientoDuplicadoError,
    SolicitudConsentimientoDesconocidaError,
)
from sky_claw.local.frozen_runtime.journal import SourceActivation
from sky_claw.local.frozen_runtime.owner_consent import (
    MECANISMO_TELEGRAM,
    SUPERFICIES_DE_CONSENTIMIENTO,
    DecisionConsentimiento,
    RegistroConsentimientos,
    SuperficieConsentimiento,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import (
    initialize_frozen_runtime_storage,
    issuances_state_dir,
)

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]

GEN_ORIGEN = "1.6.1170.0__a1b2c3d4e5f6"
GEN_OBJETIVO = "1.6.640.0__0f1e2d3c4b5a"
OPERADOR = 424242

#: Los diez campos que el ``binding_digest`` compromete, en la forma canónica.
CAMPOS_BINDING = (
    "solicitud_id",
    "root_id",
    "approval_id",
    "approval_seq",
    "approval_scope_digest",
    "surface",
    "session_id",
    "owner_subject",
    "mecanismo",
    "decided_at_ns",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(tmp_path: pathlib.Path, nombre: str = "root") -> pathlib.Path:
    root = tmp_path / nombre
    resultado = initialize_frozen_runtime_storage(root)
    assert resultado.success, resultado.admission.message
    return root


def _fuente(**overrides: object) -> SourceActivation:
    base: dict[str, object] = {
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
        "approval_provenance": "telegram:req-1",
    }
    base.update(overrides)
    return base


def _propuesta(root: pathlib.Path, **overrides: object):
    return proponer_aprobacion(root, **_kwargs(**overrides))  # type: ignore[arg-type]


def _identidad(*, sesion: str = "chat-424242", sujeto: str = str(OPERADOR)):
    """Identidad ACUÑADA por el productor privado del módulo de consentimiento."""
    return owner_consent._acuñar_identidad(
        superficie=SuperficieConsentimiento.TELEGRAM,
        sujeto=sujeto,
        sesion=sesion,
        mecanismo=MECANISMO_TELEGRAM,
        verificado_en_ns=1_700_000_000_000_000_000,
    )


def _presentar(root: pathlib.Path, propuesta, registro: RegistroConsentimientos, **overrides: object):
    datos: dict[str, object] = {"identidad": _identidad()} if "identidad" not in overrides else {}
    datos.update(overrides)
    return registro.presentar(root, propuesta, **datos)  # type: ignore[arg-type]


def _emitir_verificado(root: pathlib.Path, propuesta, consentimiento) -> None:
    with participant_lock(root, timeout=30.0) as handle:
        handle.assert_owned()
        _emitir_aprobacion_bajo_lock(handle, root, propuesta, consentimiento)


def _witness_crudo(root: pathlib.Path, seq: int) -> dict[str, object]:
    ruta = issuances_state_dir(root) / f"{seq}.json"
    return json.loads(ruta.read_text(encoding="utf-8"))


def _binding_crudo(root: pathlib.Path, seq: int) -> dict[str, object]:
    datos = _witness_crudo(root, seq)
    assert "solicitud_binding" in datos, (
        f"el witness no persiste el binding de la solicitud: REQUEST_PERSISTED no existe (claves: {sorted(datos)})"
    )
    binding = datos["solicitud_binding"]
    assert isinstance(binding, dict), f"solicitud_binding debe ser un objeto JSON, no {type(binding).__name__}"
    return binding


def _payload_binding(binding: dict[str, object]) -> dict[str, object]:
    return {campo: binding[campo] for campo in CAMPOS_BINDING}


def _digest_binding(binding: dict[str, object]) -> str:
    """Recomputa el digest del binding con la MISMA función canónica del repo.

    Es lo que hace un lector independiente después de un reinicio: no consulta el
    registro en memoria ni confía en el digest declarado.
    """
    return journal_module.digest_canonico(_payload_binding(binding))


def _aprobar(root: pathlib.Path, propuesta, registro: RegistroConsentimientos, **overrides: object):
    presentacion = _presentar(root, propuesta, registro, **overrides)
    consentimiento = registro.responder(
        presentacion.solicitud_id,
        decision=DecisionConsentimiento.APROBAR,
        identidad=overrides.get("identidad") or _identidad(),
    )
    assert consentimiento is not None
    return presentacion, consentimiento


def _modulos_bajo(directorio: pathlib.Path) -> list[pathlib.Path]:
    return sorted(ruta for ruta in directorio.rglob("*.py") if "__pycache__" not in ruta.parts)


def _llamadas_a(nombre: str) -> set[tuple[str, str]]:
    """Dónde se invoca ``nombre`` en ``sky_claw/``: ``(archivo, función contenedora)``.

    Se resuelve por AST sobre ``Call`` y no por substring: el nombre aparece en
    docstrings y prosa, y un censo que cuente texto cuenta comentarios.
    """
    encontradas: set[tuple[str, str]] = set()
    for ruta in _modulos_bajo(RAIZ_REPO / "sky_claw"):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        contenedor: dict[int, str] = {}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for hijo in ast.walk(nodo):
                    contenedor.setdefault(id(hijo), nodo.name)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre:
                encontradas.add((ruta.relative_to(RAIZ_REPO).as_posix(), contenedor.get(id(nodo), "<module>")))
    return encontradas


# ---------------------------------------------------------------------------
# 1. La propiedad: lo presentado es lo aprobado y es lo persistido
# ---------------------------------------------------------------------------


def test_la_solicitud_presentada_es_la_que_se_persiste(tmp_path: pathlib.Path) -> None:
    """``REQUEST_PRESENTED == REQUEST_APPROVED == REQUEST_PERSISTED``.

    El id que el propietario vio en la presentación es el que viaja en el
    consentimiento y el que queda en el witness. Se comprueba en los tres
    extremos, no en dos: comparar sólo presentación contra consentimiento dejaría
    el artefacto durable sin verificar, que es exactamente el diferimiento que
    este slice cierra.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion, consentimiento = _aprobar(root, propuesta, registro)

    assert consentimiento.solicitud_id == presentacion.solicitud_id
    _emitir_verificado(root, propuesta, consentimiento)

    binding = _binding_crudo(root, propuesta.scope.approval_seq)
    assert binding["solicitud_id"] == presentacion.solicitud_id


def test_el_witness_liga_la_solicitud_con_la_aprobacion_emitida(tmp_path: pathlib.Path) -> None:
    """El binding no flota: se ata al ``approval_id``/``seq``/digest/root emitidos."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion, consentimiento = _aprobar(root, propuesta, registro)
    _emitir_verificado(root, propuesta, consentimiento)

    witness = _witness_crudo(root, propuesta.scope.approval_seq)
    binding = _binding_crudo(root, propuesta.scope.approval_seq)
    assert binding["root_id"] == witness["root_id"]
    assert binding["approval_id"] == witness["approval_id"]
    assert binding["approval_seq"] == witness["approval_seq"]
    assert binding["approval_scope_digest"] == witness["approval_scope_digest"]
    assert binding["surface"] == "telegram"
    assert binding["session_id"] == presentacion.sesion
    assert binding["owner_subject"] == presentacion.propietario
    assert binding["mecanismo"] == MECANISMO_TELEGRAM
    assert binding["decided_at_ns"] == consentimiento.decided_at_ns


def test_el_binding_persistido_recomputa_su_digest(tmp_path: pathlib.Path) -> None:
    """Un lector independiente recomputa el digest sin el registro en memoria.

    Es la mitad que hace que el campo no sea decoración: el artefacto trae su
    propio digest y el lector lo recalcula. Si el binding se escribiera sin
    digest, o el digest no cubriera los campos, este test no tendría qué
    comparar.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)
    _emitir_verificado(root, propuesta, consentimiento)

    binding = _binding_crudo(root, propuesta.scope.approval_seq)
    assert set(binding) == set(CAMPOS_BINDING) | {"binding_digest"}
    assert binding["binding_digest"] == _digest_binding(binding)


# ---------------------------------------------------------------------------
# 2. Sustitución e invención: copiar el string no alcanza
# ---------------------------------------------------------------------------


def test_sustituir_la_solicitud_en_el_consentimiento_no_emite(tmp_path: pathlib.Path) -> None:
    """Cambiar el ``solicitud_id`` del consentimiento no autoriza otra solicitud.

    El digest del scope no cambia —es el mismo scope—, así que sin binding
    durable esta sustitución es invisible: el witness persistiría un id que
    ningún registro emitió.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    sustituido = dataclasses.replace(consentimiento, solicitud_id=str(uuid.uuid4()))
    with pytest.raises(ApprovalConsentimientoError, match="solicitud"):
        _emitir_verificado(root, propuesta, sustituido)
    assert not list(issuances_state_dir(root).iterdir())


def test_un_consentimiento_ligado_sin_binding_no_emite(tmp_path: pathlib.Path) -> None:
    """El defecto que este slice cierra, en su forma más corta.

    Un consentimiento con los seis campos de propietario completos y un
    ``decided_at_ns`` válido pasaba el núcleo y emitía: el ``solicitud_id`` era
    un string que el llamador ponía y nadie contrastaba. Ahora la puerta exige el
    binding acuñado.
    """
    root = _root(tmp_path)
    propuesta = _propuesta(root)
    copia_del_string = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="telegram",
        root_id=approval_module._root_id(root),
        solicitud_id=str(uuid.uuid4()),
        session_id=str(OPERADOR),
        owner_subject=str(OPERADOR),
        mecanismo=MECANISMO_TELEGRAM,
        decided_at_ns=propuesta.proposed_at_ns + 1,
    )
    with pytest.raises(ApprovalConsentimientoError, match="binding"):
        _emitir_verificado(root, propuesta, copia_del_string)
    assert not list(issuances_state_dir(root).iterdir())


def test_un_binding_acuñado_a_mano_no_emite(tmp_path: pathlib.Path) -> None:
    """Sin el sello, los campos del binding son una aserción del llamador.

    Es la misma falsificación más corta que permite Python para la identidad
    (``object.__new__`` + ``object.__setattr__``): todos los campos correctos,
    ningún sello. Tiene que rechazarse por procedencia, no por contenido.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    falsificado = object.__new__(approval_module.BindingSolicitud)
    for campo in CAMPOS_BINDING:
        object.__setattr__(falsificado, campo, getattr(consentimiento.binding, campo))
    object.__setattr__(falsificado, "binding_digest", consentimiento.binding.binding_digest)

    with pytest.raises(ApprovalConsentimientoError, match="sello|acuñad"):
        _emitir_verificado(root, propuesta, dataclasses.replace(consentimiento, binding=falsificado))
    assert not list(issuances_state_dir(root).iterdir())


def test_el_binding_no_se_construye_con_el_constructor_publico() -> None:
    """``BindingSolicitud(...)`` no produce un objeto utilizable.

    Mismo cierre que ``IdentidadPropietario``: si el constructor público
    funcionara, «binding acuñado por el registro» sería una afirmación sobre
    strings del llamador.
    """
    with pytest.raises(ApprovalConsentimientoError):
        approval_module.BindingSolicitud(
            solicitud_id=str(uuid.uuid4()),
            root_id="r" * 16,
            approval_id=str(uuid.uuid4()),
            approval_seq=1,
            approval_scope_digest="a" * 64,
            surface="telegram",
            session_id=str(OPERADOR),
            owner_subject=str(OPERADOR),
            mecanismo=MECANISMO_TELEGRAM,
            decided_at_ns=1,
            binding_digest="b" * 64,
        )


def test_un_solicitud_id_inventado_no_resuelve_en_el_registro() -> None:
    """El id tiene que existir en el registro: no se inventa un consentimiento."""
    registro = RegistroConsentimientos()
    with pytest.raises(SolicitudConsentimientoDesconocidaError):
        registro.responder(
            str(uuid.uuid4()),
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(),
        )


# ---------------------------------------------------------------------------
# 3. Decisiones terminales: rechazada, cancelada, consumida
# ---------------------------------------------------------------------------


def test_una_solicitud_rechazada_no_emite(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    assert (
        registro.responder(
            presentacion.solicitud_id,
            decision=DecisionConsentimiento.RECHAZAR,
            identidad=_identidad(),
        )
        is None
    )
    assert not list(issuances_state_dir(root).iterdir())


def test_una_solicitud_cancelada_no_emite(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion = _presentar(root, propuesta, registro)
    registro.cancelar(presentacion.solicitud_id, motivo="apagado ordenado")
    with pytest.raises(ConsentimientoDuplicadoError):
        registro.responder(
            presentacion.solicitud_id,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(),
        )
    assert not list(issuances_state_dir(root).iterdir())


def test_una_solicitud_consumida_no_emite_de_nuevo(tmp_path: pathlib.Path) -> None:
    """Single-use: la misma solicitud no produce un segundo consentimiento."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion, consentimiento = _aprobar(root, propuesta, registro)
    _emitir_verificado(root, propuesta, consentimiento)

    with pytest.raises(ConsentimientoDuplicadoError):
        registro.responder(
            presentacion.solicitud_id,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad(),
        )
    assert len(list(issuances_state_dir(root).iterdir())) == 1


# ---------------------------------------------------------------------------
# 4. Lectura durable: sin el registro en memoria
# ---------------------------------------------------------------------------


def test_el_witness_v3_se_lee_sin_el_registro_en_memoria(tmp_path: pathlib.Path) -> None:
    """Reinicio: registro nuevo, proceso nuevo, la solicitud sigue siendo legible.

    El registro de propuestas murió con el proceso anterior. Si el único lugar
    donde constaba qué solicitud se aprobó fuera ese registro, la respuesta se
    habría perdido: por eso el binding vive en el witness.
    """
    root = _root(tmp_path)
    registro_anterior = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion, consentimiento = _aprobar(root, propuesta, registro_anterior)
    _emitir_verificado(root, propuesta, consentimiento)
    registro_anterior.descartar_todo()

    binding = approval_module.leer_binding_solicitud(root, propuesta.scope.approval_seq)
    assert binding.solicitud_id == presentacion.solicitud_id
    assert binding.approval_id == propuesta.scope.approval_id
    assert binding.approval_seq == propuesta.scope.approval_seq
    assert binding.approval_scope_digest == propuesta.scope.approval_scope_digest


def test_el_witness_de_una_fixture_no_inventa_un_binding(tmp_path: pathlib.Path) -> None:
    """Sin superficie HITL no hay solicitud: el artefacto lo dice, no lo simula.

    Los tests del núcleo durable (P4-S4/P4-S5) emiten con fixtures de tres
    cadenas. Ese camino no tiene solicitud, y el witness lo declara con ``null``
    en vez de fabricar un id: ``UNKNOWN != COMPATIBLE``.
    """
    root = _root(tmp_path)
    propuesta = _propuesta(root)
    fixture = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="fixture",
    )
    _emitir_verificado(root, propuesta, fixture)

    datos = _witness_crudo(root, propuesta.scope.approval_seq)
    assert datos["solicitud_binding"] is None
    with pytest.raises(ApprovalIdentityError, match="solicitud"):
        approval_module.leer_binding_solicitud(root, propuesta.scope.approval_seq)


# ---------------------------------------------------------------------------
# 5. Parser v3: esquema cerrado y coherencia de identidades
# ---------------------------------------------------------------------------


def _emitido(root: pathlib.Path) -> tuple[object, dict[str, object]]:
    """Emite por el camino real y devuelve ``(propuesta, witness_crudo)``."""
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)
    _emitir_verificado(root, propuesta, consentimiento)
    return propuesta, _witness_crudo(root, propuesta.scope.approval_seq)


def _reescribir(root: pathlib.Path, seq: int, datos: dict[str, object]) -> None:
    (issuances_state_dir(root) / f"{seq}.json").write_text(json.dumps(datos), encoding="utf-8")


def test_el_parser_v3_rechaza_un_campo_faltante(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    del datos["solicitud_binding"]["owner_subject"]  # type: ignore[index]
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError, match="solicitud_binding"):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_parser_v3_rechaza_un_campo_sobrante(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    datos["solicitud_binding"]["campo_de_mas"] = "x"  # type: ignore[index]
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError, match="solicitud_binding"):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_parser_v3_rechaza_un_tipo_incorrecto(tmp_path: pathlib.Path) -> None:
    """``approval_seq`` como string, con el digest recomputado: el tipo se valida aparte."""
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    binding = datos["solicitud_binding"]
    binding["approval_seq"] = str(binding["approval_seq"])  # type: ignore[index]
    binding["binding_digest"] = _digest_binding(binding)  # type: ignore[arg-type]
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError, match="approval_seq"):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_parser_v3_rechaza_identidades_contradictorias(tmp_path: pathlib.Path) -> None:
    """Un binding bien formado y con digest válido que apunta a OTRA aprobación.

    El digest recomputado no alcanza: el binding tiene que coincidir con la
    identidad del witness que lo contiene. Es la comprobación que separa «el
    archivo es coherente consigo mismo» de «el archivo describe esta emisión».
    """
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    binding = datos["solicitud_binding"]
    binding["approval_id"] = str(uuid.uuid4())  # type: ignore[index]
    binding["binding_digest"] = _digest_binding(binding)  # type: ignore[arg-type]
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalIdentityError, match="solicitud_binding"):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_parser_v3_rechaza_un_binding_adulterado(tmp_path: pathlib.Path) -> None:
    """Cambiar el id sin recomputar el digest: el lector lo detecta."""
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    datos["solicitud_binding"]["solicitud_id"] = str(uuid.uuid4())  # type: ignore[index]
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalIdentityError, match="solicitud_binding"):
        approval_module.derivar_autoridad_aprobacion(root)


# ---------------------------------------------------------------------------
# 5 bis. Capas de la puerta que la comparación campo por campo no cubre
# ---------------------------------------------------------------------------


def test_un_binding_de_otro_root_no_emite(tmp_path: pathlib.Path) -> None:
    """El root del BINDING se contrasta, no sólo el del consentimiento.

    ``consentimiento.root_id`` sigue apuntando al root correcto y el chequeo de
    root de P4-S6 pasa; lo único que delata la sustitución es el ``root_id`` que
    el registro estampó dentro del binding. Sin esta comparación, un binding
    acuñado para otro root viajaría dentro de un consentimiento legítimo.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    de_otro_root = approval_module._acuñar_binding_solicitud(
        solicitud_id=consentimiento.binding.solicitud_id,
        root_id="otro-root-fisico",
        approval_id=consentimiento.binding.approval_id,
        approval_seq=consentimiento.binding.approval_seq,
        approval_scope_digest=consentimiento.binding.approval_scope_digest,
        surface=consentimiento.binding.surface,
        session_id=consentimiento.binding.session_id,
        owner_subject=consentimiento.binding.owner_subject,
        mecanismo=consentimiento.binding.mecanismo,
        decided_at_ns=consentimiento.binding.decided_at_ns,
    )
    with pytest.raises(ApprovalConsentimientoError, match="root_id"):
        _emitir_verificado(root, propuesta, dataclasses.replace(consentimiento, binding=de_otro_root))
    assert not list(issuances_state_dir(root).iterdir())


def test_un_binding_con_digest_falso_no_emite(tmp_path: pathlib.Path) -> None:
    """El digest del binding se RECOMPUTA en la puerta, no se copia.

    Se mutan sólo los 64 caracteres del digest: los diez campos siguen
    coincidiendo con el consentimiento y el scope, así que la comparación campo
    por campo no lo ve. Sin la recomputación se emitiría un witness cuyo binding
    el propio lector rechazaría después: la emisión ocurriría y el artefacto
    quedaría ilegible.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    object.__setattr__(consentimiento.binding, "binding_digest", "0" * 64)
    with pytest.raises(ApprovalConsentimientoError, match="digest"):
        _emitir_verificado(root, propuesta, consentimiento)
    assert not list(issuances_state_dir(root).iterdir())


def test_el_parser_v3_rechaza_un_witness_sin_la_clave_del_binding(tmp_path: pathlib.Path) -> None:
    """La clave tiene que EXISTIR. Ausente es otro esquema; ``null`` es «no hubo».

    Confundir las dos cosas es lo que permitiría que un artefacto v2 se colara
    como v3 quitándole la clave en vez de declarando ``null``.
    """
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    datos.pop("solicitud_binding")
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError, match="solicitud_binding"):
        approval_module.derivar_autoridad_aprobacion(root)


# ---------------------------------------------------------------------------
# 6. Root ajeno: falla ANTES del CREATE
# ---------------------------------------------------------------------------


def test_el_root_ajeno_falla_antes_del_create(tmp_path: pathlib.Path) -> None:
    """Un consentimiento del root A no autoriza nada en el root B.

    La propuesta del root B es el MISMO scope —mismo ``approval_id``, misma
    ``seq``, mismo digest— a propósito: si fuera otra propuesta, el rechazo
    vendría del digest y el test no aislaría la atadura al root físico. La
    comprobación va antes del ``CREATE`` del witness: después del witness llega
    tarde, porque el witness ES la emisión (§39.3 C3).
    """
    root_a = _root(tmp_path, "root-a")
    root_b = _root(tmp_path, "root-b")
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root_a)
    _, consentimiento = _aprobar(root_a, propuesta, registro)

    mismo_scope_en_b = approval_module.PropuestaAprobacion(
        scope=propuesta.scope,
        ledger_revision_at_proposal=propuesta.ledger_revision_at_proposal,
        proposed_at_ns=propuesta.proposed_at_ns,
    )
    with pytest.raises(ApprovalIdentityError):
        _emitir_verificado(root_b, mismo_scope_en_b, consentimiento)
    assert not list(issuances_state_dir(root_b).iterdir())
    assert not list(issuances_state_dir(root_a).iterdir())


# ---------------------------------------------------------------------------
# 7. Fallos de escritura y crash posterior al witness
# ---------------------------------------------------------------------------


def test_una_escritura_fallida_no_fabrica_una_aprobacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Burn-first: si el ``CREATE`` del witness falla, no queda aprobación."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    def _falla(*_args: object, **_kwargs: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(approval_module, "_crear_json_inmutable", _falla)
    with pytest.raises(OSError):
        _emitir_verificado(root, propuesta, consentimiento)

    assert not list(issuances_state_dir(root).iterdir())
    ledger = cargar_ledger_aprobacion(root)
    assert ledger.ledger is None or ledger.ledger.state is not ApprovalLifecycle.ISSUED


def test_un_crash_tras_el_witness_conserva_el_binding(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El witness es la autoridad: si el CAS falla, el binding no se pierde."""
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    presentacion, consentimiento = _aprobar(root, propuesta, registro)

    def _cas_roto(*_args: object, **_kwargs: object) -> None:
        raise ApprovalCasError("CAS perdido tras el witness")

    monkeypatch.setattr(approval_module, "_escribir_ledger_cas", _cas_roto)
    _emitir_verificado(root, propuesta, consentimiento)

    binding = _binding_crudo(root, propuesta.scope.approval_seq)
    assert binding["solicitud_id"] == presentacion.solicitud_id
    ledger = cargar_ledger_aprobacion(root)
    assert ledger.ledger is not None
    assert ledger.ledger.state is ApprovalLifecycle.ISSUED


# ---------------------------------------------------------------------------
# 8. Concurrencia real entre procesos
# ---------------------------------------------------------------------------


def test_la_carrera_entre_procesos_conserva_single_use_y_no_clobber(tmp_path: pathlib.Path) -> None:
    """Dos procesos emiten a la vez: UN witness, y es el de la solicitud que ganó.

    Cada proceso acuña su propia solicitud (su propio ``solicitud_id``). El
    perdedor no puede publicar un segundo witness ni reescribir el del ganador, y
    el binding persistido tiene que ser el del ganador — no el del perdedor.
    """
    root = _root(tmp_path)
    guion = RAIZ_REPO / "tests" / "_p4_s7_child.py"
    assert guion.is_file(), "falta el hijo autónomo de concurrencia"

    resultados = []
    for _ in range(2):
        proceso = subprocess.run(
            [sys.executable, str(guion), str(root)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env={
                **os.environ,
                # El hijo corre como script: ``sys.path[0]`` es ``tests/``, así que
                # el worktree tiene que entrar por PYTHONPATH o importaría el
                # editable-install del repo principal (o nada).
                "PYTHONPATH": str(RAIZ_REPO),
                "SKYCLAW_P4_S7_WORKTREE": str(RAIZ_REPO),
            },
        )
        assert proceso.returncode == 0, proceso.stderr[-2000:]
        resultados.append(json.loads(proceso.stdout.strip().splitlines()[-1]))

    exitos = [item for item in resultados if item.get("ok")]
    fallos = [item for item in resultados if not item.get("ok")]
    assert len(exitos) == 1, f"la emisión no es exclusiva: {resultados}"
    assert len(fallos) == 1, f"los dos procesos emitieron: {resultados}"

    archivos = sorted(issuances_state_dir(root).iterdir())
    assert len(archivos) == 1, f"no-clobber violado: {[ruta.name for ruta in archivos]}"
    binding = _binding_crudo(root, 1)
    assert binding["solicitud_id"] == exitos[0]["solicitud_id"]


# ---------------------------------------------------------------------------
# 9. Legado: v1/v2 no se declaran v3
# ---------------------------------------------------------------------------


def test_un_witness_v2_no_se_declara_v3(tmp_path: pathlib.Path) -> None:
    """``LEGACY_WITNESS_POLICY = FAIL_CLOSED``: no se migra ni se reinterpreta.

    El cuerpo se deja COMPLETO y coherente —con su ``solicitud_binding`` y su
    digest válido— para que la ÚNICA razón de rechazo sea la versión declarada.
    Si el test quitara además el campo nuevo, lo rechazaría el esquema cerrado y
    la comprobación de versión quedaría sin aislar: un lector que aceptara v2
    «porque los campos comunes coinciden» pasaría inadvertido, que es exactamente
    ``UNKNOWN != COMPATIBLE``.
    """
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    datos["schema_version"] = 2
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]

    antes = (issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").read_bytes()  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError, match="schema_version"):
        approval_module.derivar_autoridad_aprobacion(root)
    despues = (issuances_state_dir(root) / f"{propuesta.scope.approval_seq}.json").read_bytes()  # type: ignore[attr-defined]
    assert antes == despues, "el lector modificó un artefacto legado: AUTO_DELETE_OF_state = NO"


def test_un_witness_v2_sin_el_campo_nuevo_tampoco_se_acepta(tmp_path: pathlib.Path) -> None:
    """La forma REAL de un artefacto v2: no tiene ``solicitud_binding``.

    Se rechaza por el esquema cerrado, con independencia de la versión. Las dos
    rutas de rechazo importan: un v2 completo cae por versión, un v2 auténtico cae
    por campo faltante, y ninguna de las dos fabrica un binding sintético.
    """
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    datos["schema_version"] = 2
    datos.pop("solicitud_binding")
    _reescribir(root, propuesta.scope.approval_seq, datos)  # type: ignore[attr-defined]
    with pytest.raises(ApprovalSchemaError):
        approval_module.derivar_autoridad_aprobacion(root)


def test_el_witness_emitido_declara_esquema_v3(tmp_path: pathlib.Path) -> None:
    """El artefacto, no la constante: lo que se persiste es lo que se audita."""
    root = _root(tmp_path)
    propuesta, datos = _emitido(root)
    assert datos["schema_version"] == 3
    assert "solicitud_binding" in datos


# ---------------------------------------------------------------------------
# 10. Anclas enumerativas
# ---------------------------------------------------------------------------


def test_solo_el_consentimiento_y_el_lector_acunan_bindings() -> None:
    """Los DOS acuñadores legítimos, enumerados; un tercero rompe el ancla.

    ``_acuñar_binding_solicitud`` es el único productor de la capacidad, y tiene
    exactamente dos llamadores con propósitos distintos:

    - ``owner_consent.responder`` — AUTORIDAD: acuña desde la presentación que el
      registro tiene abierta. Es el único que puede producir un binding para una
      emisión nueva.
    - ``approval._parsear_binding_solicitud`` — RECONSTRUCCIÓN: re-acuña desde un
      artefacto durable que ya pasó esquema cerrado, digest recomputado y
      coherencia con el witness. No puede producir un binding arbitrario: los
      campos salen del archivo y están comprometidos por su digest.

    Un tercer llamador —una superficie que fabricara un binding para una solicitud
    que nunca presentó— rompe el test. Se resuelve por AST sobre ``Call`` y no por
    substring: el nombre aparece en docstrings y prosa.
    """
    assert _llamadas_a("_acuñar_binding_solicitud") == {
        ("sky_claw/local/frozen_runtime/owner_consent.py", "responder"),
        ("sky_claw/local/frozen_runtime/approval.py", "_parsear_binding_solicitud"),
    }
    # El constructor público no se usa en ninguna parte, ni siquiera acá.
    assert _llamadas_a("BindingSolicitud") == set()


def test_el_camino_autorizado_siempre_persiste_binding(tmp_path: pathlib.Path) -> None:
    """La entrada productiva exige binding: no hay emisión autorizada sin solicitud.

    ``emitir_aprobacion_autorizada`` ya rechazaba un consentimiento sin los seis
    campos; ahora el séptimo —el binding acuñado— es obligatorio. Se comprueba por
    comportamiento (no emite y no deja witness), no afirmando el valor de un flag.
    """
    root = _root(tmp_path)
    propuesta = _propuesta(root)
    completo_sin_binding = ConsentimientoAprobacion(
        approval_scope_digest=propuesta.scope.approval_scope_digest,
        approval_provenance=propuesta.scope.approval_provenance,
        surface="telegram",
        root_id=approval_module._root_id(root),
        solicitud_id=str(uuid.uuid4()),
        session_id=str(OPERADOR),
        owner_subject=str(OPERADOR),
        mecanismo=MECANISMO_TELEGRAM,
        decided_at_ns=propuesta.proposed_at_ns + 1,
    )
    with pytest.raises(ApprovalHitolNoDisponibleError, match="binding"):
        owner_consent.emitir_aprobacion_autorizada(root, propuesta, completo_sin_binding)
    assert not list(issuances_state_dir(root).iterdir())


def test_el_binding_no_habilita_la_emision_productiva(tmp_path: pathlib.Path) -> None:
    """FASE E: el binding es evidencia, no una llave de promoción.

    Un consentimiento COMPLETO, con binding acuñado por el camino real, sigue sin
    emitir por la entrada productiva mientras no exista consumidor. El test no
    afirma el valor de ``EMISION_PRODUCTIVA_HABILITADA``: prueba que la puerta
    sigue cerrada por conducta.
    """
    root = _root(tmp_path)
    registro = RegistroConsentimientos()
    propuesta = _propuesta(root)
    _, consentimiento = _aprobar(root, propuesta, registro)

    with pytest.raises(ApprovalHitolNoDisponibleError):
        owner_consent.emitir_aprobacion_autorizada(root, propuesta, consentimiento)
    assert not list(issuances_state_dir(root).iterdir())


def test_el_censo_de_superficies_no_crecio(tmp_path: pathlib.Path) -> None:
    """Ninguna superficie nueva puede aprobar sin declarar su mecanismo."""
    censo = {
        superficie.value: mecanismo.verifica_identidad
        for superficie, mecanismo in SUPERFICIES_DE_CONSENTIMIENTO.items()
    }
    assert censo == {"telegram": True, "gui": False}
