"""P7-A5 PR 3 — cableado de autorización HITL para la transición Golden Master.

Tracker: #867 (histórico: #672) · ADR 0012 §35.2, §36.6, §37.2, §37.4, §39.3-§39.4, §46, §47
ADR 0015 §12, §13

Qué congela este archivo
------------------------
Hasta P7-A5 PR 2 las piezas existían **sueltas y sin cablear**:

```text
approval.proponer_aprobacion        (FASE A: propuesta)          sin llamador productivo
owner_consent.presentar/responder   (FASE B/B': HITL)            HITL_PRODUCTIVE = NOT_WIRED
owner_consent.emitir_aprobacion_autorizada (FASE C: ISSUE)       cerrada por interruptor
runtime_clone_binder.ejecutar_...   (CONSUME → BIND → FINALIZE)  sin emisor que lo alimente
```

El cableado que este archivo prueba es la cadena completa —**evidencia derivada de
observación real → propuesta → presentación al propietario → decisión → emisión →
consumo → binder → verificación → finalización**— y, con la misma fuerza, las
**puertas que impiden que exista un atajo**:

```text
G1  el scope NO se arma con strings del llamador: sale de verificadores read-only
G2  la presentación la acuña el registro HITL, no el cableado
G3  la emisión sigue CERRADA (EMISION_PRODUCTIVA_HABILITADA = False) y el cableado
    no la abre: los tests que emiten lo hacen por monkeypatch EXPLÍCITO del
    interruptor, así que abrirlo es un cambio de una línea con consumidor ya probado
G4  el consentimiento es single-use, de una sola sesión y de un solo root
G5  si la evidencia cambia entre la presentación y la ejecución, no se emite y no
    se ejecuta: la aprobación es de un estado del mundo, no de una intención
G6  no se autoriza una segunda transición mientras una está en curso o reanudable
G7  EMISION_PRODUCTIVA_HABILITADA = False se preserva: el cableado no define el
    interruptor ni consume aprobaciones por su cuenta
```

``HITL_PRODUCTIVE`` cambia de ``NOT_WIRED`` a ``WIRED_BUT_GATED`` en este PR: existe
un consumidor autorizado de extremo a extremo y la emisión sigue cerrada hasta que
el dueño la abra en un PR propio.
"""

from __future__ import annotations

import ast
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import time
import tomllib
import urllib.parse

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import mo2_ini_exclusion, storage
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    cargar_ledger_aprobacion,
    derivar_autoridad_aprobacion,
    leer_binding_solicitud,
)
from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
    AbandonoCorruptoError,
    AbandonoTransicionInvalidoError,
    EstadoDurable,
    EvidenciaTransicion,
    EvidenciaTransicionError,
    ReanudacionRequiereDuenoError,
    RegistroAbandonoTransicion,
    SesionFueraDeOrdenError,
    SesionTransicionGoldenMaster,
    TransicionEnCursoError,
    TransicionYaActivaError,
    _calcular_digest_abandono,
    _cargar_abandonos,
    _registros_de_clones,
    abandonar_transicion_consumida,
    cargar_registro_abandono,
    derivar_evidencia_transicion,
    diagnosticar_estado_durable,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    cargar_runtime_clone_record,
    registrar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    Mo2RegistryObservation,
    Mo2RegistryState,
    ObservationState,
)
from sky_claw.local.frozen_runtime.errors import (
    ApprovalCasError,
    ApprovalHitolNoDisponibleError,
    ApprovalMaxIssuedError,
    ConsentimientoDuplicadoError,
    FrozenRuntimeLockBusyError,
    FrozenRuntimeStorageError,
    IdentidadPropietarioNoVerificadaError,
    SesionConsentimientoNoCoincideError,
    SuperficieSinIdentidadVerificadaError,
)
from sky_claw.local.frozen_runtime.owner_consent import (
    MECANISMO_PRESENCIA_LOCAL,
    DecisionConsentimiento,
    SuperficieConsentimiento,
    _acuñar_identidad,
    render_valor_scope,
    verificar_identidad_telegram,
)
from sky_claw.local.frozen_runtime.provisioning import (
    verificar_runtime_setup,
)
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key
from sky_claw.local.frozen_runtime.storage import (
    approvals_state_dir,
    issuances_state_dir,
    recovery_state_dir,
    transition_journal_path,
)
from tests._rig_p7_a5 import (
    RigStockManual,
    aplicar_parche_identidad_pe,
    crear_config_toml,
    crear_mo2_global_sintetica,
    escribir_ini_mo2,
    probes_con_registro_simulado,
    snapshot_sha256_recursivo,
)

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
MODULO_CABLEADO = RAIZ_REPO / "sky_claw" / "local" / "frozen_runtime" / "autorizacion_hitl_transicion.py"
OPERADOR = 424242
#: Los cuatro tests de extremo a extremo mutan ``modorganizer.ini`` de verdad y por eso
#: corren con la exclusión de Win32. En POSIX la exclusión real no existe, así que se
#: omiten... salvo que se pida explícitamente el doble POSIX de ``exclusion_de_test``.
#: El opt-in hace REPRODUCIBLE la afirmación de la documentación:
#:
#:     SKY_CLAW_PR3_E2E_POSIX=1 python -m pytest tests/test_frozen_runtime_p7_a5_pr3_hitl_wiring.py
E2E_POSIX_PEDIDO = os.environ.get("SKY_CLAW_PR3_E2E_POSIX") == "1"
SOLO_WINDOWS = pytest.mark.skipif(
    os.name != "nt" and not E2E_POSIX_PEDIDO,
    reason=(
        "la mutación de modorganizer.ini exige la exclusión de archivos compartidos de Win32 "
        "(FILE_SHARE_NONE); fuera de Windows el binder falla cerrado a propósito "
        "(para correrlos con el doble POSIX: SKY_CLAW_PR3_E2E_POSIX=1)"
    ),
)


# ---------------------------------------------------------------------------
# Fixtures y dobles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _identidad_pe(monkeypatch: pytest.MonkeyPatch) -> None:
    aplicar_parche_identidad_pe(monkeypatch)


class _ExclusionPosix:
    """Doble POSIX del handle de exclusión de Win32, SÓLO para verificar el cableado acá.

    En Windows CI corre la exclusión REAL: este doble se aplica únicamente cuando
    ``os.name != "nt"``, y hay un test que lo afirma. Sin él, ninguna prueba de
    extremo a extremo podría correr fuera de Windows y el cableado se diseñaría a
    ciegas (que es exactamente el defecto que este PR cierra).

    Lo que el doble NO promete: exclusión real entre procesos. Fuera de Windows el
    binder falla cerrado por diseño; el doble no cambia esa política, sólo permite
    recorrer el camino feliz en un entorno de test.
    """

    def __init__(self, ruta: pathlib.Path) -> None:
        self.ruta = pathlib.Path(ruta)
        self._abierta = True
        self.diagnostico = mo2_ini_exclusion.DiagnosticoExclusion(
            estado=mo2_ini_exclusion.EstadoExclusion.ESTABLISHED,
            ruta=self.ruta,
            motivo="doble POSIX de test: la exclusión de Win32 no existe fuera de Windows",
            intentos=0,
        )

    @property
    def autoriza(self) -> bool:
        return self._abierta

    @property
    def estado(self) -> mo2_ini_exclusion.EstadoExclusion:
        return self.diagnostico.estado

    def leer_bytes(self) -> bytes:
        if not self._abierta:
            raise mo2_ini_exclusion.Mo2IniExclusionError("exclusión cerrada")
        return self.ruta.read_bytes()

    def huella(self) -> str:
        return hashlib.sha256(self.leer_bytes()).hexdigest()

    def verificar_huella(self, esperada: str) -> None:
        if self.huella() != esperada:
            raise mo2_ini_exclusion.Mo2IniExclusionError("huella divergente (doble POSIX)")

    def escribir_bytes(self, datos: bytes, *, al_escribir: object = None) -> None:
        if not self._abierta:
            raise mo2_ini_exclusion.Mo2IniExclusionError("exclusión cerrada")
        if callable(al_escribir):
            al_escribir()
        self.ruta.write_bytes(datos)

    def cerrar(self) -> None:
        self._abierta = False


@pytest.fixture
def exclusion_de_test(monkeypatch: pytest.MonkeyPatch) -> str:
    """``real`` en Windows, ``doble_posix`` fuera: el valor lo afirma un test."""
    if os.name == "nt":
        return "real"
    monkeypatch.setattr(
        binder_module,
        "adquirir_exclusion_ini",
        lambda ruta, **_kwargs: _ExclusionPosix(pathlib.Path(ruta)),
    )
    return "doble_posix"


def _identidad_telegram(*, operador: int = OPERADOR) -> object:
    """Identidad que produce el verificador REAL a partir de un update real de Telegram."""
    return verificar_identidad_telegram(
        {"from": {"id": operador}, "chat": {"id": operador, "type": "private"}},
        operador_configurado=OPERADOR,
    )


def _recolector() -> tuple[list[object], object]:
    entregadas: list[object] = []

    def _entregar(presentacion: object) -> None:
        entregadas.append(presentacion)

    return entregadas, _entregar


def _habilitar_emision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Abre el interruptor SÓLO durante el test: es la simulación del PR de activación.

    El valor en el código fuente sigue siendo ``False``; el test que lo ancla lo
    verifica leyendo el archivo, no el atributo mutado.
    """
    monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)


def _sesion(
    rig: RigStockManual, *, policy: BinderTransitionPolicy = BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
):
    return SesionTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_active_global,
        clone_id=rig.clone_id,
        transition_policy=policy,
        managed_source_root=rig.managed_source,
        outgoing_manual_runtime_root=rig.manual_runtime,
        stock_source_root=rig.stock_source,
        approved_stock_reference_digest_file=rig.approved_digest_file,
        config_path=rig.config_path,
        environ=rig.environ,
        probes=rig.probes_globales_activos(),
    )


def _superficies_del_rig(rig: RigStockManual) -> list[pathlib.Path]:
    return [
        rig.mo2_active_global / "modorganizer.ini",
        rig.config_path,
        rig.manual_runtime,
        rig.managed_source,
        rig.stock_source,
        rig.stock_base,
        rig.storage / "versions",
    ]


def _huellas(rig: RigStockManual) -> dict[str, str]:
    return snapshot_sha256_recursivo(_superficies_del_rig(rig))


def _sin_evidencia_durable(storage: pathlib.Path) -> bool:
    return not list(issuances_state_dir(storage).iterdir()) and not list(approvals_state_dir(storage).iterdir())


# ===========================================================================
# CONTRATO G1 — la evidencia sale de verificadores, no del llamador
# ===========================================================================


class TestContratoG1EvidenciaDerivada:
    def test_01_evidencia_stock_independiente_con_steam_activo(self, tmp_path: pathlib.Path) -> None:
        """El scope se deriva de observación verificada; ningún campo lo aporta el llamador."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        evidencia = derivar_evidencia_transicion(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=rig.config_path,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )

        # Destino: identidad de la Generation y del Clone, no una ruta del llamador.
        assert evidencia.clone_id == rig.clone_id
        assert evidencia.generation_id == rig.generation_id
        registro = cargar_runtime_clone_record(rig.storage, rig.clone_id)
        assert registro is not None
        assert evidencia.runtime_setup_id == registro.verified_runtime_setup_id
        assert evidencia.clone_root == pathlib.Path(registro.root_path)
        assert evidencia.clone_lifecycle is RuntimeCloneLifecycle.PROVISIONED

        # Evidencia content-bound: recomputada desde el árbol observado.
        setup_observado = verificar_runtime_setup(evidencia.clone_root)
        assert setup_observado.setup_id == evidencia.runtime_setup_id
        assert re.fullmatch(r"[0-9a-f]{64}", evidencia.clone_evidence)
        assert re.fullmatch(r"[0-9a-f]{64}", evidencia.runtime_setup_evidence)
        assert re.fullmatch(r"[0-9a-f]{64}", evidencia.compatibility_evidence_id)
        assert evidencia.runtime_setup_evidence != evidencia.compatibility_evidence_id

        # Origen: no había activación de Sky-Claw, así que el bootstrap es el íntegramente vacío.
        assert evidencia.source_activation.generation_id is None
        assert evidencia.source_activation.clone_id is None
        assert evidencia.source_activation.runtime_setup_id is None
        assert evidencia.source_activation.effective_runtime_evidence is None

        # Procedencia A1 aprobada y política efectiva, ambas verificadas (no supuestas).
        assert evidencia.stock_reference_digest == rig.approved_digest
        assert evidencia.transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION

        # La procedencia es determinista y liga instancia, INI, política, Stock y destino.
        assert evidencia.approval_provenance.startswith("golden-master:v1:")
        repeated = derivar_evidencia_transicion(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=rig.config_path,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )
        assert repeated.approval_provenance == evidencia.approval_provenance
        assert repeated.clone_evidence == evidencia.clone_evidence

    def test_02_candidate_id_es_determinista_y_content_bound(self, tmp_path: pathlib.Path) -> None:
        """PROMOTION exige candidate_id: se deriva del contenido, no de un contador."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        primera = derivar_evidencia_transicion(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=rig.config_path,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )
        segunda = derivar_evidencia_transicion(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=rig.config_path,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )
        assert primera.candidate_id == segunda.candidate_id
        assert re.fullmatch(r"cand_[0-9a-f]{32}", primera.candidate_id)

    def test_03_preflight_bloqueado_no_produce_evidencia(self, tmp_path: pathlib.Path) -> None:
        """Steam ACTIVE bajo la política histórica: no hay evidencia que presentar."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        with pytest.raises(EvidenciaTransicionError, match="preflight"):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
                managed_source_root=rig.managed_source,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )
        assert _sin_evidencia_durable(rig.storage)

    def test_04_politica_stock_sin_ancla_a1_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """Sin ancla A1 no se deriva evidencia bajo ``STOCK_INDEPENDENT_TRANSITION``.

        El gate que lo hace cumplir HOY es el preflight (``stock_reference_unapproved``);
        el cableado repite la comprobación después de ``READY`` para que un preflight
        futuro que deje de exigirla no convierta este camino en un bypass silencioso.
        """
        rig = RigStockManual(tmp_path, steam_activo=True)

        with pytest.raises(EvidenciaTransicionError, match="stock_reference_unapproved"):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )

    def test_05_setup_del_clone_alterado_no_produce_evidencia(self, tmp_path: pathlib.Path) -> None:
        """El setup aprobado es el observado: un SKSE distinto invalida la evidencia."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        (rig.clone_root / "skse64_loader.exe").write_bytes(b"loader-ALTERADO")

        with pytest.raises(EvidenciaTransicionError, match="setup"):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )

    def test_06_clone_no_provisionado_no_produce_evidencia(self, tmp_path: pathlib.Path) -> None:
        """Un Clone que no está ``PROVISIONED`` no es destino de una transición autorizable."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        registro = cargar_runtime_clone_record(rig.storage, rig.clone_id)
        assert registro is not None
        # El registro es no-clobber: el caso se arma con un Clone NUEVO, no pisando el del rig.
        copia = pathlib.Path(shutil.copytree(rig.clone_root, tmp_path / "FrozenClones" / "clone-en-provisioning"))
        registrar_runtime_clone_record(
            rig.storage,
            dataclasses.replace(
                registro,
                clone_id="clone-en-provisioning",
                lifecycle=RuntimeCloneLifecycle.PROVISIONING,
                verified_runtime_setup_id=None,
                root_path=str(copia),
            ),
            managed_source_root=rig.managed_source,
        )

        with pytest.raises(EvidenciaTransicionError):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id="clone-en-provisioning",
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )

    def test_07_instancia_mo2_no_activa_no_produce_evidencia(self, tmp_path: pathlib.Path) -> None:
        """La instancia se verifica por registro: otra instancia activa no se admite."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        probes_ajenas = probes_con_registro_simulado(
            local_app_data=rig.local_app_data,
            registry_obs=Mo2RegistryObservation(
                state=Mo2RegistryState.OBSERVED,
                current_instance="Otra Instancia",
            ),
        )

        with pytest.raises(EvidenciaTransicionError):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=probes_ajenas,
            )

    def test_08_generation_ausente_no_produce_evidencia(self, tmp_path: pathlib.Path) -> None:
        """Un Clone cuya Generation perdió su metadata no autoriza nada (linaje no verificable)."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        metadata = rig.storage / "state" / "generations" / f"{rig.generation_id}.json"
        assert metadata.is_file()
        metadata.unlink()

        with pytest.raises(EvidenciaTransicionError):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )

    def test_09_maquina_de_estados_de_la_sesion(self, tmp_path: pathlib.Path) -> None:
        """No se ejecuta lo que no se autorizó: la sesión no es un contenedor de strings."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        entregadas, entregar = _recolector()

        with pytest.raises(SesionFueraDeOrdenError):
            sesion.ejecutar(solicitud=None, consentimiento=None)  # type: ignore[arg-type]

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        assert len(entregadas) == 1
        with pytest.raises(SesionFueraDeOrdenError):
            sesion.ejecutar(solicitud=solicitud, consentimiento=None)  # type: ignore[arg-type]

        with pytest.raises((ApprovalHitolNoDisponibleError, EvidenciaTransicionError)):
            sesion.decidir_y_emitir(
                solicitud=solicitud,
                decision=DecisionConsentimiento.APROBAR,
                identidad=_identidad_telegram(),
            )
        # Nada durable se escribió: sigue sin haber witnesses ni tombstones.
        assert _sin_evidencia_durable(rig.storage)

    # ===========================================================================
    # CONTRATO G2/G3 — presentación real, emisión cerrada por defecto
    # ===========================================================================

    def test_36_registro_de_clone_ilegible_no_permite_declarar_bootstrap_vacio(self, tmp_path: pathlib.Path) -> None:
        """Un registro de Clone ilegible es fail-closed, no "no hay Clones".

        ``_registros_de_clones`` alimenta la prueba de que el runtime saliente NO es
        una superficie administrada. Si el registro relevante está corrupto y se
        ignora, esa prueba desaparece en silencio y el bootstrap vacío se declara
        sobre un estado que el dueño tiene que reconciliar.
        """
        rig = RigStockManual(tmp_path, steam_activo=True)
        clones = rig.storage / "state" / "clones"
        (clones / "clone-ilegible.json").write_text("{no es json", encoding="utf-8")

        # El preflight ya bloquea con ``clone_record_corrupt``; el chequeo propio del
        # cableado (defensa en profundidad) es el que impide que un registro ilegible se
        # descarte en silencio si el preflight algún día deja de mirarlo.
        with pytest.raises(EvidenciaTransicionError) as capturado:
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )
        assert "clone_record_corrupt" in str(capturado.value)

        # Y el helper que sostiene la afirmación «no es una superficie administrada» falla
        # cerrado por sí mismo, sin depender de que el preflight lo haya mirado.
        with pytest.raises(EvidenciaTransicionError, match="no se puede leer"):
            _registros_de_clones(rig.storage)

    def test_37a_config_path_none_rechaza_antes_de_presentar_y_no_deja_aprobacion_issued(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Solución al finding Codex discussion_r4543251718:

        Sin config_path, derivar_evidencia_transicion / sesion.preparar() rechaza de inmediato
        con EvidenciaTransicionError antes de presentar o emitir una aprobación, impidiendo
        que quede una aprobación ISSUED colgada en el ledger para una transición condenada.
        """
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = SesionTransicionGoldenMaster(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=None,
            environ=rig.environ,
            probes=rig.probes_globales_activos(),
        )
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        with pytest.raises(EvidenciaTransicionError, match="se exige config_path"):
            sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)

        # Ninguna aprobación fue emitida ni quedó registrada en el ledger
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.witnesses) == 0
        ledger_res = cargar_ledger_aprobacion(rig.storage)
        assert ledger_res.ledger is None or ledger_res.ledger.state != ApprovalLifecycle.ISSUED

    def test_37b_config_invalida_ausente_o_incoherente_se_rechaza_antes_de_presentar(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Objetivo A: configuraciones ausentes, ilegibles o incoherentes se rechazan en derivación/preparación."""
        rig = RigStockManual(tmp_path, steam_activo=True)

        def _derivar(cfg_p: pathlib.Path | None) -> EvidenciaTransicion:
            return derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=cfg_p,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )

        # 1. Config ausente (None)
        with pytest.raises(EvidenciaTransicionError, match="se exige config_path"):
            _derivar(None)

        # 2. Config inexistente (archivo no existe)
        with pytest.raises(EvidenciaTransicionError, match="no existe o no es legible"):
            _derivar(tmp_path / "inexistente_config.toml")

        # 3. Config ilegible (corrupta)
        cfg_corrupto = tmp_path / "corrupto_config.toml"
        cfg_corrupto.write_text("skyrim_path = [esto no es toml valido", encoding="utf-8")
        with pytest.raises(EvidenciaTransicionError, match="no existe o no es legible"):
            _derivar(cfg_corrupto)

        # 4. Config incoherente (skyrim_path divergente que no coincide con el preflight)
        cfg_incoherente = tmp_path / "incoherente_config.toml"
        crear_config_toml(cfg_incoherente, skyrim_path=str(tmp_path / "RutaInventada"))
        with pytest.raises(EvidenciaTransicionError, match="preflight de integración no autorizó la operación"):
            _derivar(cfg_incoherente)

        # 5. Config válida: conserva el flujo existente
        ev = _derivar(rig.config_path)
        assert ev is not None
        assert ev.config_path == rig.config_path


class TestContratoG2G3PresentacionYEmision:
    def test_10_la_presentacion_muestra_el_scope_derivado_y_la_procedencia(self, tmp_path: pathlib.Path) -> None:
        """La presentación es la del registro HITL y contiene los 14 campos del scope."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        entregadas, entregar = _recolector()

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)

        presentacion = entregadas[0]
        assert presentacion.solicitud_id == solicitud.presentacion.solicitud_id
        assert presentacion.approval_seq == 1
        texto = presentacion.texto
        for campo in ("generation_id", "clone_id", "clone_evidence", "runtime_setup_id", "approval_provenance"):
            assert campo in texto, campo
        # La presentación rinde cada valor por ``render_valor_scope`` (json.dumps), así que
        # en Windows los ``\`` del path salen escapados: se compara el valor RENDIDO, que es
        # el que el propietario ve, no el crudo.
        assert render_valor_scope(solicitud.evidencia.approval_provenance) in texto
        # La procedencia sigue siendo legible: decodificada se lee el path de la instancia.
        procedencia_visible = urllib.parse.unquote(solicitud.evidencia.approval_provenance).lower()
        assert os.path.normcase(str(rig.mo2_active_global)).lower() in procedencia_visible
        # Presentar no emite: la propuesta es un valor en memoria sin autoridad.
        assert _sin_evidencia_durable(rig.storage)

    def test_11_emision_cerrada_falla_cerrado_y_no_muta_nada(self, tmp_path: pathlib.Path) -> None:
        """``EMISION_PRODUCTIVA_HABILITADA = False``: el cableado no emite ni toca superficies."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        antes = _huellas(rig)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        with pytest.raises(ApprovalHitolNoDisponibleError, match="emisión productiva sigue cerrada"):
            sesion.decidir_y_emitir(
                solicitud=solicitud,
                decision=DecisionConsentimiento.APROBAR,
                identidad=_identidad_telegram(),
            )

        assert _sin_evidencia_durable(rig.storage)
        assert _huellas(rig) == antes
        ledger = cargar_ledger_aprobacion(rig.storage)
        assert ledger.ledger is None or ledger.ledger.state is not ApprovalLifecycle.ISSUED

    def test_12_cadena_autorizada_emite_witness_ligado_a_la_solicitud(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con el interruptor abierto: ISSUE con binding de la solicitud que el dueño vio."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad_telegram(),
        )

        assert consentimiento is not None
        assert consentimiento.approval_scope_digest == solicitud.propuesta.scope.approval_scope_digest
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.witnesses) == 1
        witness = autoridad.witnesses[0]
        assert witness.approval_seq == 1
        assert witness.approval_scope_digest == solicitud.propuesta.scope.approval_scope_digest
        assert witness.solicitud_binding is not None
        binding = leer_binding_solicitud(rig.storage, witness.approval_seq)
        assert binding.solicitud_id == solicitud.presentacion.solicitud_id
        assert binding.owner_subject == str(OPERADOR)
        # Emitir NO consume: no hay tombstone y el journal sigue sin transición.
        assert autoridad.tombstones == ()
        assert cargar_ledger_aprobacion(rig.storage).ledger.state is ApprovalLifecycle.ISSUED
        assert journal_module.cargar_journal_transicion(rig.storage).journal is None

    @pytest.mark.parametrize("decision", [DecisionConsentimiento.RECHAZAR, DecisionConsentimiento.CANCELAR])
    def test_13_decision_negativa_no_emite(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, decision: DecisionConsentimiento
    ) -> None:
        """Rechazar o cancelar no produce consentimiento ni escribe autoridad."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        assert sesion.decidir_y_emitir(solicitud=solicitud, decision=decision, identidad=_identidad_telegram()) is None
        assert _sin_evidencia_durable(rig.storage)

    def test_14_una_solicitud_resuelta_no_se_reabre(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una decisión terminal no se repite: el consentimiento es single-use."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        sesion.decidir_y_emitir(
            solicitud=solicitud,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad_telegram(),
        )
        with pytest.raises((ConsentimientoDuplicadoError, SesionFueraDeOrdenError)):
            sesion.decidir_y_emitir(
                solicitud=solicitud,
                decision=DecisionConsentimiento.APROBAR,
                identidad=_identidad_telegram(),
            )
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 1

    def test_15_otra_sesion_de_propietario_no_puede_decidir(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un tercero con otra identidad no puede convertir la solicitud en aprobación."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        ajeno = verificar_identidad_telegram(
            {"from": {"id": 999111}, "chat": {"id": 999111, "type": "private"}},
            operador_configurado=999111,
        )
        with pytest.raises(SesionConsentimientoNoCoincideError):
            sesion.decidir_y_emitir(
                solicitud=solicitud,
                decision=DecisionConsentimiento.APROBAR,
                identidad=ajeno,
            )
        assert _sin_evidencia_durable(rig.storage)

    def test_16_evidencia_cambiada_tras_la_presentacion_no_emite(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Se aprueba un estado del mundo: si el subtree cambia, la aprobación no aplica."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        (rig.clone_root / "inyectado_tras_presentar.bin").write_bytes(b"drift")

        with pytest.raises(EvidenciaTransicionError):
            sesion.decidir_y_emitir(
                solicitud=solicitud,
                decision=DecisionConsentimiento.APROBAR,
                identidad=_identidad_telegram(),
            )
        assert _sin_evidencia_durable(rig.storage)

    def test_17_dos_aprobaciones_concurrentes_no_producen_dos_witnesses(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrencia: el CAS de la caché y ``MAX_ISSUED = 1`` dejan pasar una sola emisión."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        _habilitar_emision(monkeypatch)
        solicitudes = []
        for _ in range(2):
            sesion = _sesion(rig)
            _, entregar = _recolector()
            solicitudes.append((sesion, sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)))

        def _emitir(item: tuple[SesionTransicionGoldenMaster, object]) -> str:
            sesion, solicitud = item
            try:
                sesion.decidir_y_emitir(
                    solicitud=solicitud,
                    decision=DecisionConsentimiento.APROBAR,
                    identidad=_identidad_telegram(),
                )
            except (ApprovalCasError, ApprovalMaxIssuedError, FrozenRuntimeLockBusyError) as exc:
                # Las tres son fail-closed: dos emisiones no pueden coexistir ni por
                # CAS, ni por MAX_ISSUED=1, ni por el lock del root.
                return f"rechazada:{type(exc).__name__}"
            return "emitida"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            resultados = sorted(pool.map(_emitir, solicitudes))

        assert resultados.count("emitida") == 1, resultados
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 1


# ===========================================================================
# CONTRATO G7 / anclas enumerativas — el cableado no agrega puertas paralelas
# ===========================================================================


class TestAnclasEnumerativas:
    def test_18_el_interruptor_sigue_cerrado_y_es_unico(self) -> None:
        """Censo por texto sobre el FUENTE, no sobre el atributo que el test mutó."""
        patron = re.compile(r"^EMISION_PRODUCTIVA_HABILITADA\s*=", re.MULTILINE)
        definiciones: list[str] = []
        for ruta in sorted((RAIZ_REPO / "sky_claw").rglob("*.py")):
            if "__pycache__" in ruta.parts:
                continue
            if patron.search(ruta.read_text(encoding="utf-8")):
                definiciones.append(ruta.relative_to(RAIZ_REPO).as_posix())
        assert definiciones == ["sky_claw/local/frozen_runtime/approval.py"]

    def test_19_el_cableado_no_consume_ni_emite_por_su_cuenta(self) -> None:
        """El cableado usa la superficie de emisión, no una copia; y no consume aprobaciones."""
        fuente = MODULO_CABLEADO.read_text(encoding="utf-8")
        assert "consumir_aprobacion(" not in fuente, (
            "el cableado debe delegar el consumo en el binder: consumir acá sería un segundo consumidor "
            "y el censo de P4-S6 quedaría desactualizado en silencio"
        )
        assert "_emitir_aprobacion_bajo_lock(" not in fuente
        assert "ConsentimientoAprobacion(" not in fuente, (
            "el único módulo que construye consentimientos es owner_consent (censo P4-S6)"
        )
        assert "emitir_aprobacion_autorizada(" in fuente, (
            "la emisión tiene que pasar por la superficie productiva, que es la que falla cerrada"
        )

    def test_20_el_cableado_no_usa_la_superficie_hitl_de_tools(self) -> None:
        """``app.security.hitl`` aprueba TOOLS, no transiciones: no es un atajo válido."""
        arbol = ast.parse(MODULO_CABLEADO.read_text(encoding="utf-8"))
        modulos: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                modulos.update(alias.name for alias in nodo.names)
            elif isinstance(nodo, ast.ImportFrom) and nodo.module and not nodo.level:
                modulos.add(nodo.module)
        assert not {m for m in modulos if m.startswith("sky_claw.app.security.hitl")}, modulos
        assert not {m for m in modulos if m.startswith("sky_claw.app.comms")}, modulos

    def test_21_el_doble_posix_no_se_activa_en_windows(self, exclusion_de_test: str) -> None:
        """El doble de exclusión es de POSIX: en Windows CI corre la exclusión real."""
        if os.name == "nt":
            assert exclusion_de_test == "real"
        else:
            assert exclusion_de_test == "doble_posix"
            assert binder_module.adquirir_exclusion_ini is not mo2_ini_exclusion.adquirir_exclusion_ini


# ===========================================================================
# CONTRATO G6 — diagnóstico durable: una transición a la vez
# ===========================================================================


class TestContratoG6DiagnosticoDurable:
    def test_22_storage_nuevo_esta_limpio(self, tmp_path: pathlib.Path) -> None:
        rig = RigStockManual(tmp_path, steam_activo=False)
        diagnostico = diagnosticar_estado_durable(rig.storage)
        assert diagnostico.estado is EstadoDurable.LIMPIO
        assert diagnostico.transicion_en_curso is None
        assert diagnostico.transicion_reanudable is None

    def test_23_journal_pendiente_ajeno_bloquea_la_solicitud(self, tmp_path: pathlib.Path) -> None:
        """Un PENDING de otra transición no se pisa: primero se resuelve, después se autoriza."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        journal_path = transition_journal_path(rig.storage)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            '{"schema_version": 2, "state": "PENDING_PROMOTION", "journal_revision": 1}',
            encoding="utf-8",
        )
        diagnostico = diagnosticar_estado_durable(rig.storage)
        assert diagnostico.estado is not EstadoDurable.LIMPIO

        sesion = _sesion(rig, policy=BinderTransitionPolicy.STRICT_PROVIDER_IDLE)
        _, entregar = _recolector()
        with pytest.raises((TransicionEnCursoError, EvidenciaTransicionError)):
            sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        assert not list(issuances_state_dir(rig.storage).iterdir())


# ===========================================================================
# CONTRATO G7 — política histórica preservada
# ===========================================================================


class TestContratoG7PoliticaHistorica:
    def test_24_la_sesion_no_degrada_ni_asciende_la_politica(self, tmp_path: pathlib.Path) -> None:
        """STRICT_PROVIDER_IDLE es el default y el cableado no lo cambia por su cuenta."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        escribir_ini_mo2(
            rig.mo2_active_global / "modorganizer.ini",
            game_path=rig.managed_source,
            base_directory=rig.mo2_base_data,
        )
        crear_config_toml(rig.config_path, skyrim_path=str(rig.managed_source))
        rig.environ["SKYRIM_PATH"] = str(rig.managed_source)
        sesion = SesionTransicionGoldenMaster(
            storage_root=rig.storage,
            mo2_instance_dir=rig.mo2_active_global,
            clone_id=rig.clone_id,
            managed_source_root=rig.managed_source,
            config_path=rig.config_path,
            environ={"SKYRIM_PATH": str(rig.managed_source)},
            probes=rig.probes_globales_activos(),
        )
        _, entregar = _recolector()
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        assert solicitud.evidencia.transition_policy is BinderTransitionPolicy.STRICT_PROVIDER_IDLE
        assert solicitud.evidencia.stock_reference_digest is None

    def test_25_stock_independiente_no_relaja_la_independencia_fisica(self, tmp_path: pathlib.Path) -> None:
        """Bajo ``STOCK_INDEPENDENT_TRANSITION`` un hardlink en el Clone sigue bloqueando."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        original = rig.clone_root / "Data" / "Skyrim.esm"
        enlace = rig.clone_root / "Data" / "Skyrim_esm_enlazado.esm"
        try:
            os.link(original, enlace)
        except (OSError, NotImplementedError):
            pytest.skip("el sistema de archivos no soporta hardlinks")

        with pytest.raises(EvidenciaTransicionError):
            derivar_evidencia_transicion(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_active_global,
                clone_id=rig.clone_id,
                transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
                managed_source_root=rig.managed_source,
                outgoing_manual_runtime_root=rig.manual_runtime,
                stock_source_root=rig.stock_source,
                approved_stock_reference_digest_file=rig.approved_digest_file,
                config_path=rig.config_path,
                environ=rig.environ,
                probes=rig.probes_globales_activos(),
            )


# ===========================================================================
# Extremo a extremo (mutación real del INI: Windows)
# ===========================================================================


@SOLO_WINDOWS
class TestExtremoAExtremo:
    def test_30_transicion_golden_master_autorizada_completa_la_cadena(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Solicitud → presentación → decisión → emisión → consumo → binder → verificación."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        antes = _huellas(rig)
        sesion = _sesion(rig)
        entregadas, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud,
            decision=DecisionConsentimiento.APROBAR,
            identidad=_identidad_telegram(),
        )
        assert consentimiento is not None
        resultado = sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        assert resultado.transicion_id
        assert resultado.binder is not None and resultado.binder.success is True
        assert resultado.binder.target_clone_root == rig.clone_root.resolve()

        # Superficies: INI y config apuntan al Clone.
        evidencia_ini = leer_game_path_de_mo2(rig.mo2_active_global / "modorganizer.ini")
        assert evidencia_ini.state is ObservationState.OBSERVED
        assert pathlib.Path(evidencia_ini.path or "").resolve() == rig.clone_root.resolve()
        config = tomllib.loads(rig.config_path.read_text(encoding="utf-8"))
        assert os.path.normcase(config["skyrim_path"]) == os.path.normcase(str(rig.clone_root))

        # Autoridad durable: aprobación CONSUMIDA por ESTA transición, una sola vez.
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.witnesses) == 1
        assert len(autoridad.tombstones) == 1
        tombstone = autoridad.tombstones[0]
        assert tombstone.terminal_state.value == "CONSUMED"
        assert tombstone.consumed_by_transition_id == resultado.transicion_id
        ledger = cargar_ledger_aprobacion(rig.storage)
        assert ledger.ledger is not None and ledger.ledger.state is ApprovalLifecycle.CONSUMED

        # Journal FINALIZED y registro inmutable de la transición.
        assert (
            journal_module.cargar_journal_transicion(rig.storage).disposition is journal_module.JournalDisposition.NONE
        )
        historial = journal_module.derivar_historial_finalizados(rig.storage)
        assert historial.latest is not None
        assert historial.latest.transition_id == resultado.transicion_id
        assert historial.latest.target_clone_id == rig.clone_id

        # Nada de Steam, Stock, Generation ni del runtime manual cambió.
        despues = _huellas(rig)
        for superficie in (
            rig.managed_source,
            rig.stock_source,
            rig.stock_base,
            rig.manual_runtime,
            rig.storage / "versions",
        ):
            for clave, huella in antes.items():
                if clave.startswith(str(superficie)):
                    assert despues[clave] == huella, f"se mutó una superficie prohibida: {clave}"
        assert exclusion_de_test in {"real", "doble_posix"}

    def test_31_una_segunda_solicitud_tras_activar_no_reautoria(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Activado el runtime, la misma operación no vuelve a pedir aprobación."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None
        sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        sesion_nueva = _sesion(rig)
        _, entregar_nuevo = _recolector()
        with pytest.raises(TransicionYaActivaError):
            sesion_nueva.preparar(identidad=_identidad_telegram(), entregar=entregar_nuevo)
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 1

    def test_32_recuperacion_de_aprobacion_consumida_antes_del_pending(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Fallo entre el burn y el PENDING: la MISMA aprobación consumida completa la transición."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        antes_ini = (rig.mo2_active_global / "modorganizer.ini").read_bytes()
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro

        def _falla_una_vez(*args: object, **kwargs: object) -> object:
            raise recovery.RecuperacionError("fallo inyectado entre el burn y el PENDING")

        monkeypatch.setattr(recovery, "capturar_registro", _falla_una_vez)
        with pytest.raises(Exception):  # noqa: B017 — el tipo lo fija el binder, acá se mide el estado durable
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        # Estado durable: aprobación QUEMADA, journal sin transición, INI intacto.
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.tombstones) == 1
        assert autoridad.tombstones[0].terminal_state.value == "CONSUMED"
        assert journal_module.cargar_journal_transicion(rig.storage).journal is None
        assert (rig.mo2_active_global / "modorganizer.ini").read_bytes() == antes_ini

        diagnostico = diagnosticar_estado_durable(rig.storage)
        assert diagnostico.estado is EstadoDurable.REANUDABLE
        assert diagnostico.transicion_reanudable == autoridad.tombstones[0].consumed_by_transition_id

        # Una aprobación NUEVA sería una segunda autorización de lo mismo: se rechaza.
        _, entregar_nuevo = _recolector()
        with pytest.raises(TransicionEnCursoError):
            _sesion(rig).preparar(identidad=_identidad_telegram(), entregar=entregar_nuevo)

        reanudado = _sesion(rig).reanudar(transition_id=diagnostico.transicion_reanudable or "")
        assert reanudado.binder is not None and reanudado.binder.success is True
        # La MISMA aprobación (seq 1) completó: no se emitió una segunda.
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 1
        assert journal_module.derivar_historial_finalizados(rig.storage).latest is not None
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

    def test_33_fallo_despues_del_pending_no_se_reanuda_y_exige_al_dueno(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Fallo con PENDING publicado: el burn-first gastó la aprobación y decide el dueño."""
        from sky_claw.local.frozen_runtime import mo2_ini_escritura

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        def _falla_escribir(*args: object, **kwargs: object) -> bytes:
            raise mo2_ini_escritura.Mo2IniEscrituraError("fallo inyectado en la escritura del INI")

        monkeypatch.setattr(binder_module, "actualizar_game_path_por_handle", _falla_escribir)
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        diagnostico = diagnosticar_estado_durable(rig.storage)
        assert diagnostico.estado is not EstadoDurable.LIMPIO
        assert diagnostico.journal_disposition is journal_module.JournalDisposition.PENDING_PROMOTION

        # Ni una aprobación nueva ni una reanudación automática: la decisión es del dueño.
        _, entregar_nuevo = _recolector()
        with pytest.raises(TransicionEnCursoError):
            _sesion(rig).preparar(identidad=_identidad_telegram(), entregar=entregar_nuevo)
        with pytest.raises(ReanudacionRequiereDuenoError):
            _sesion(rig).reanudar(transition_id=resultado_transicion(rig))
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 1

    def test_34_reanudar_no_aplica_una_aprobacion_de_otra_instancia(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Una aprobación consumida para la instancia A no se reanuda desde la instancia B.

        El scope de 14 campos liga el runtime destino, no la instancia de MO2 (hallazgo
        F-05): la instancia viaja en ``approval_provenance``. Si la reanudación no la
        re-verificara, un consentimiento gastado en A se aplicaría al INI de B, y el
        momento elegido —una interrupción— es justo cuando nadie está mirando.
        """
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion_a = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion_a.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion_a.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro

        def _falla_una_vez(*args: object, **kwargs: object) -> object:
            raise recovery.RecuperacionError("fallo inyectado entre el burn y el PENDING")

        monkeypatch.setattr(recovery, "capturar_registro", _falla_una_vez)
        with pytest.raises(Exception):  # noqa: B017 — el tipo lo fija el binder, acá se mide el estado
            sesion_a.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        diagnostico = diagnosticar_estado_durable(rig.storage)
        assert diagnostico.estado is EstadoDurable.REANUDABLE
        transition_id = diagnostico.transicion_reanudable or ""
        assert transition_id

        # La instancia GLOBAL activa pasa a ser otra: mismo Clone destino, otro INI y otro storage local.
        local_app_data_b = tmp_path / "LocalAppData_B"
        instancia_b = crear_mo2_global_sintetica(
            local_app_data_b, instance_name="Skyrim Special Edition", game_path=rig.manual_runtime
        )
        config_b = crear_config_toml(tmp_path / "config_b.toml", skyrim_path=str(rig.manual_runtime))
        ini_b_antes = (instancia_b / "modorganizer.ini").read_bytes()
        sesion_b = SesionTransicionGoldenMaster(
            storage_root=rig.storage,
            mo2_instance_dir=instancia_b,
            clone_id=rig.clone_id,
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            managed_source_root=rig.managed_source,
            outgoing_manual_runtime_root=rig.manual_runtime,
            stock_source_root=rig.stock_source,
            approved_stock_reference_digest_file=rig.approved_digest_file,
            config_path=config_b,
            environ={"SKYRIM_PATH": str(rig.manual_runtime), "LOCALAPPDATA": str(local_app_data_b)},
            probes=probes_con_registro_simulado(
                local_app_data=local_app_data_b,
                registry_obs=Mo2RegistryObservation(
                    state=Mo2RegistryState.OBSERVED, current_instance="Skyrim Special Edition"
                ),
            ),
        )
        with pytest.raises(EvidenciaTransicionError, match="campos distintos"):
            sesion_b.reanudar(transition_id=transition_id)

        # El rechazo ocurre ANTES de mutar: instancia B, journal, recuperación y autoridad intactos.
        assert (instancia_b / "modorganizer.ini").read_bytes() == ini_b_antes
        assert journal_module.cargar_journal_transicion(rig.storage).journal is None
        assert not list(recovery_state_dir(rig.storage).glob("*.json"))
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.tombstones) == 1
        assert autoridad.tombstones[0].terminal_state.value == "CONSUMED"
        assert diagnostico_estado_actual(rig) is EstadoDurable.REANUDABLE

        # Y la instancia aprobada sigue pudiendo completar: el rechazo no dañó el estado.
        reanudado = _sesion(rig).reanudar(transition_id=transition_id)
        assert reanudado.binder is not None and reanudado.binder.success is True
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

    def test_35_reanudar_con_pending_es_solo_diagnostico(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Con un ``PENDING`` publicado, ``reanudar`` informa y NO restaura nada.

        ``binder_recovery.recuperar_transicion_interrumpida`` reescribe superficies y
        retira registros: es una operación con efectos. Hacerla dentro de un método que
        después lanza una excepción deja al operador sin saber qué cambió. La
        recuperación queda como paso EXPLÍCITO del dueño; acá se afirma que la
        reanudación es de sólo lectura.
        """
        from sky_claw.local.frozen_runtime import binder_recovery as recovery
        from sky_claw.local.frozen_runtime import mo2_ini_escritura

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        def _falla_escribir(*args: object, **kwargs: object) -> bytes:
            raise mo2_ini_escritura.Mo2IniEscrituraError("fallo inyectado en la escritura del INI")

        monkeypatch.setattr(binder_module, "actualizar_game_path_por_handle", _falla_escribir)
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.undo()

        ini_tras_fallo = (rig.mo2_active_global / "modorganizer.ini").read_bytes()
        transition_id = resultado_transicion(rig)
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.PENDIENTE_RECUPERABLE
        # El binder YA dejó su propio registro de recuperación al fallar: la referencia es ése,
        # no «ningún registro»; lo que se mide es que reanudar no agregue ni cambie ninguno.
        registros_antes = sorted(ruta.name for ruta in recovery_state_dir(rig.storage).glob("*.json"))

        with pytest.raises(ReanudacionRequiereDuenoError, match="PENDING"):
            _sesion(rig).reanudar(transition_id=transition_id)

        # Nada se restauró ni se registró: el estado durable es el mismo que dejó el fallo.
        assert (rig.mo2_active_global / "modorganizer.ini").read_bytes() == ini_tras_fallo
        assert sorted(ruta.name for ruta in recovery_state_dir(rig.storage).glob("*.json")) == registros_antes
        assert (
            journal_module.cargar_journal_transicion(rig.storage).disposition
            is journal_module.JournalDisposition.PENDING_PROMOTION
        )
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.PENDIENTE_RECUPERABLE
        # Y la recuperación sigue siendo posible, pero como operación EXPLÍCITA del dueño.
        recuperacion = recovery.recuperar_transicion_interrumpida(rig.storage, transition_id=transition_id)
        assert recuperacion.estado is not None

    def test_38_reproduccion_consumed_sin_pending_evidencia_cambiada_deadlock(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Reproducción del finding Codex discussion_r4543251726 (Objetivo B):

        1. Aprobación ISSUED -> CONSUMED.
        2. Fallo inyectado antes de PENDING (sin journal PENDING ni recovery).
        3. Cambio de evidencia del entorno.
        4. reanudar() es rechazada porque la evidencia ya no coincide con lo aprobado.
        5. sesion.preparar() está permanentemente bloqueada (DEADLOCK) porque
           diagnosticar_estado_durable() clasifica el estado como REANUDABLE.
        """
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro

        def _falla_antes_de_pending(*args: object, **kwargs: object) -> object:
            raise recovery.RecuperacionError("fallo inyectado entre el burn y el PENDING")

        monkeypatch.setattr(recovery, "capturar_registro", _falla_antes_de_pending)
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        # Verificar estado tras el fallo: CONSUMED, sin PENDING, sin recovery
        autoridad = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad.tombstones) == 1
        tombstone = autoridad.tombstones[0]
        assert tombstone.terminal_state.value == "CONSUMED"
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None
        assert journal_module.cargar_journal_transicion(rig.storage).journal is None
        assert not list(recovery_state_dir(rig.storage).glob("*.json"))

        # Cambio de evidencia del entorno: mutamos modorganizer.ini (cambia sha256 preservando CRLF)
        ini_path = rig.mo2_active_global / "modorganizer.ini"
        ini_path.write_bytes(ini_path.read_bytes() + b"\r\n# mutacion de evidencia\r\n")

        # 4. reanudar() es rechazada porque la evidencia no coincide
        with pytest.raises(
            EvidenciaTransicionError, match="la evidencia re-derivada no es la que el propietario aprobó"
        ):
            sesion.reanudar(transition_id=trans_id)

        # 5. una sesion nueva intentar preparar() está bloqueada en DEADLOCK por TransicionEnCursoError
        _, entregar_2 = _recolector()
        with pytest.raises(TransicionEnCursoError, match="se reanuda con la MISMA aprobación"):
            _sesion(rig).preparar(identidad=_identidad_telegram(), entregar=entregar_2)

    def test_38b_abandono_autorizado_resuelve_deadlock_y_permite_nueva_autorizacion(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Solución autorizada al finding Codex discussion_r4543251726 (Objetivo B):

        Tras el deadlock de test_38, el propietario ejecuta un abandono explícito y autorizado:
        - La aprobación consumida permanece CONSUMED (nunca vuelve a ISSUED).
        - El tombstone CONSUMED permanece intacto.
        - Se crea un registro durable e inmutable de abandono con todos los invariantes ligados.
        - diagnosticar_estado_durable() pasa a LIMPIO.
        - Una nueva sesión puede preparar (seq 2), emitir y ejecutar a éxito la transición.
        """
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro

        def _falla_antes_de_pending(*args: object, **kwargs: object) -> object:
            raise recovery.RecuperacionError("fallo inyectado entre el burn y el PENDING")

        monkeypatch.setattr(recovery, "capturar_registro", _falla_antes_de_pending)
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        autoridad = derivar_autoridad_aprobacion(rig.storage)
        tombstone = autoridad.tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        # Modificación de evidencia para detonar deadlock. El separador sale del propio INI (CRLF en
        # Windows, LF en POSIX): mezclar clases de salto de línea hace que el binder falle cerrado por otra causa.
        ini_path = rig.mo2_active_global / "modorganizer.ini"
        ini_previo = ini_path.read_bytes()
        salto = b"\r\n" if b"\r\n" in ini_previo else b"\n"
        ini_path.write_bytes(ini_previo + salto + b"# mutacion de evidencia" + salto)

        # Comprobar que reanudar falla y preparar está bloqueada
        with pytest.raises(EvidenciaTransicionError):
            sesion.reanudar(transition_id=trans_id)
        _, entregar_bloqueada = _recolector()
        with pytest.raises(TransicionEnCursoError):
            _sesion(rig).preparar(identidad=_identidad_telegram(), entregar=entregar_bloqueada)

        # OPERACIÓN DE ABANDONO AUTORIZADO
        motivo = "evidencia del entorno cambió tras interrupción pre-pending; transición abandonada legítimamente"
        registro = sesion.abandonar(
            transition_id=trans_id,
            identidad=_identidad_telegram(),
            motivo=motivo,
        )

        # Invariantes del registro de abandono
        assert isinstance(registro, RegistroAbandonoTransicion)
        assert registro.schema_version == 1
        assert registro.transition_id == trans_id
        assert registro.approval_id == tombstone.approval_id
        assert registro.approval_seq == int(tombstone.approval_seq)
        assert registro.scope_digest == tombstone.approval_scope_digest
        assert registro.propietario_sujeto == str(OPERADOR)
        assert registro.propietario_superficie == "telegram"
        assert registro.propietario_mecanismo == "telegram_from_id"
        assert registro.motivo == motivo
        assert bool(registro.record_digest)

        # Invariantes de persistencia y no regresión
        autoridad_post = derivar_autoridad_aprobacion(rig.storage)
        assert len(autoridad_post.tombstones) == 1
        assert autoridad_post.tombstones[0].terminal_state.value == "CONSUMED"
        ledger_res = cargar_ledger_aprobacion(rig.storage)
        assert ledger_res.ledger is not None
        assert ledger_res.ledger.state.value == "CONSUMED"

        # Diagnóstico reconoce la resolución
        diag = diagnosticar_estado_durable(rig.storage)
        assert diag.estado is EstadoDurable.LIMPIO

        # Nueva preparación es posible, sin reutilizar consentimiento previo
        sesion_2 = _sesion(rig)
        entregadas_2, entregar_2 = _recolector()
        solicitud_2 = sesion_2.preparar(identidad=_identidad_telegram(), entregar=entregar_2)
        assert solicitud_2.presentacion.approval_seq == 2
        assert len(entregadas_2) == 1

        consentimiento_2 = sesion_2.decidir_y_emitir(
            solicitud=solicitud_2, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento_2 is not None

        # Ejecución a éxito
        resultado_2 = sesion_2.ejecutar(solicitud=solicitud_2, consentimiento=consentimiento_2)
        assert resultado_2.binder is not None and resultado_2.binder.success is True
        assert resultado_2.aprobacion_seq == 2
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO
        assert len(derivar_autoridad_aprobacion(rig.storage).witnesses) == 2

    def test_39_adversarial_abandono_duplicado_es_idempotente(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: reintentar o duplicar abandono es idempotente y resistente a replay."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        # Primer abandono
        reg1 = sesion.abandonar(
            transition_id=trans_id,
            identidad=_identidad_telegram(),
            motivo="primer abandono",
        )
        # Segundo abandono (duplicado/replay)
        reg2 = sesion.abandonar(
            transition_id=trans_id,
            identidad=_identidad_telegram(),
            motivo="primer abandono",
        )
        assert reg1 == reg2
        assert reg2.transition_id == trans_id

        # Solo existe un archivo en el directorio
        archivos = list(storage.abandoned_transitions_state_dir(rig.storage).glob("*.json"))
        assert len(archivos) == 1

    def test_40_adversarial_abandono_rechaza_otro_root(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: el registro de abandono no es transferible a otro root (expected_root_id mismatch)."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="abandono")

        ruta_reg = storage.abandoned_transition_record_path(rig.storage, trans_id)
        assert ruta_reg.is_file()

        # Al cargarlo con expected_root_id de otro root, falla cerrado
        with pytest.raises(AbandonoTransicionInvalidoError, match="no coincide con el esperado"):
            cargar_registro_abandono(ruta_reg, expected_root_id="otro_root_id_falso")

    def test_41_adversarial_abandono_rechaza_otro_transition_id(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: no se puede abandonar un transition_id que no tiene tombstone CONSUMED."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)

        with pytest.raises(AbandonoTransicionInvalidoError, match="no se encontró ninguna aprobación consumida"):
            sesion.abandonar(
                transition_id="9b2f6c1e-3d4a-4e5f-8a6b-7c8d9e0f1a2b",  # UUID v4 canónico, sin tombstone
                identidad=_identidad_telegram(),
                motivo="intento invalido",
            )

        with pytest.raises(AbandonoTransicionInvalidoError, match="no es un UUID válido"):
            sesion.abandonar(
                transition_id="trans_inexistente_12345",
                identidad=_identidad_telegram(),
                motivo="intento invalido",
            )

        with pytest.raises(AbandonoTransicionInvalidoError, match="transition_id debe ser una cadena no vacía"):
            sesion.abandonar(
                transition_id="   ",
                identidad=_identidad_telegram(),
                motivo="intento invalido",
            )

        with pytest.raises(AbandonoTransicionInvalidoError, match="se exige un motivo no vacío"):
            sesion.abandonar(
                transition_id="9b2f6c1e-3d4a-4e5f-8a6b-7c8d9e0f1a2b",
                identidad=_identidad_telegram(),
                motivo="",
            )

    def test_42_adversarial_abandono_rechaza_identidad_no_verificada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: la decisión exige autorización verificada del propietario; rechaza GUI o identidades falsas."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        # 1. Identidad fabricada sin sello (intento de bypass)
        class _IdentidadFalsa:
            superficie = SuperficieConsentimiento.TELEGRAM
            sujeto = str(OPERADOR)
            sesion = str(OPERADOR)
            mecanismo = "telegram_from_id"

        with pytest.raises(IdentidadPropietarioNoVerificadaError):
            sesion.abandonar(
                transition_id=trans_id,
                identidad=_IdentidadFalsa(),  # type: ignore[arg-type]
                motivo="intento falso",
            )

        # 2. Identidad con superficie GUI (mecanismo presencia local, no verificado)
        identidad_gui = _acuñar_identidad(
            superficie=SuperficieConsentimiento.GUI,
            sujeto="local_user",
            sesion="desktop_session",
            mecanismo=MECANISMO_PRESENCIA_LOCAL,
            verificado_en_ns=time.time_ns(),
        )
        with pytest.raises((IdentidadPropietarioNoVerificadaError, SuperficieSinIdentidadVerificadaError)):
            sesion.abandonar(
                transition_id=trans_id,
                identidad=identidad_gui,
                motivo="intento gui",
            )

    def test_43_adversarial_abandono_falla_si_aprobacion_o_tombstone_corrupto(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: si el registro de abandono o el tombstone está corrupto, diagnosticar_estado_durable y cargar fallan cerrado."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="abandono")

        ruta_reg = storage.abandoned_transition_record_path(rig.storage, trans_id)
        assert ruta_reg.is_file()

        # 1. Corrupción de contenido JSON
        ruta_reg.write_text("ESTO NO ES JSON VALIDO", encoding="utf-8")
        with pytest.raises(AbandonoCorruptoError, match="no es JSON válido"):
            cargar_registro_abandono(ruta_reg)
        diag = diagnosticar_estado_durable(rig.storage)
        assert diag.estado is EstadoDurable.REQUIERE_DUENO

        # 2. Corrupción de digest
        data = {
            "schema_version": 1,
            "root_id": canonical_root_key(rig.storage)[0],
            "transition_id": trans_id,
            "approval_id": tombstone.approval_id,
            "approval_seq": int(tombstone.approval_seq),
            "scope_digest": tombstone.approval_scope_digest,
            "propietario_sujeto": str(OPERADOR),
            "propietario_superficie": "telegram",
            "propietario_sesion": str(OPERADOR),
            "propietario_mecanismo": "telegram_from_id",
            "motivo": "abandono",
            "abandonado_en_ns": 123456,
            "record_digest": "digest_falsificado_0000000000000000000000000000000000000000000000000000000000000000",
        }
        ruta_reg.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(AbandonoCorruptoError, match="está corrupto"):
            cargar_registro_abandono(ruta_reg)

        # 3. Schema no soportado
        data["record_digest"] = "val"
        data["schema_version"] = 999
        ruta_reg.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(AbandonoCorruptoError, match="versión de schema no soportada"):
            cargar_registro_abandono(ruta_reg)

    def test_44_adversarial_abandono_rechaza_si_pending_en_journal(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: rechazar abandono si existe PENDING en journal (debe recuperarse/reconciliarse primero)."""
        from sky_claw.local.frozen_runtime import mo2_ini_escritura

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        # Inyectar fallo después de publicar PENDING
        def _falla_escribir(*args: object, **kwargs: object) -> bytes:
            raise mo2_ini_escritura.Mo2IniEscrituraError("fallo al mutar ini")

        monkeypatch.setattr(binder_module, "actualizar_game_path_por_handle", _falla_escribir)
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.undo()

        trans_id = resultado_transicion(rig)
        assert (
            journal_module.cargar_journal_transicion(rig.storage).disposition
            is not journal_module.JournalDisposition.NONE
        )

        with pytest.raises(AbandonoTransicionInvalidoError, match="el journal declara un PENDING"):
            abandonar_transicion_consumida(
                rig.storage,
                transition_id=trans_id,
                identidad=_identidad_telegram(),
                motivo="intento indebido de abandono con PENDING",
            )

    def test_45_adversarial_abandono_rechaza_si_recovery_existente(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: rechazar abandono si existen registros de recuperación no resueltos."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        # Simular presencia de un registro de recuperación no resuelto
        rec_dir = recovery_state_dir(rig.storage)
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / f"{trans_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transition_id": trans_id,
                    "operation": "PROMOTION",
                    "fase": "CAPTURED",
                    "superficies": [],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(AbandonoTransicionInvalidoError, match="registros de recuperación"):
            abandonar_transicion_consumida(
                rig.storage,
                transition_id=trans_id,
                identidad=_identidad_telegram(),
                motivo="intento indebido con recovery pendiente",
            )

    def test_46_adversarial_abandono_rechaza_si_transicion_finalized(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: rechazar abandono si la transición ya completó exitosamente (FINALIZED)."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        res = sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        assert res.binder is not None and res.binder.success is True
        trans_id = res.transicion_id

        # La transición completó y está en finalizados
        historial = journal_module.derivar_historial_finalizados(rig.storage)
        assert any(r.transition_id == trans_id for r in historial.registros)

        with pytest.raises(AbandonoTransicionInvalidoError, match="ya está FINALIZED"):
            abandonar_transicion_consumida(
                rig.storage,
                transition_id=trans_id,
                identidad=_identidad_telegram(),
                motivo="intento indebido sobre transicion finalizada",
            )

    def test_47_adversarial_abandono_maneja_fallo_de_persistencia(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Adversarial: si la escritura atómica del registro de abandono falla, falla cerrado sin corromper el estado."""
        from sky_claw.local.frozen_runtime import binder_recovery as recovery

        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion = _sesion(rig)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)

        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        original = recovery.capturar_registro
        monkeypatch.setattr(
            recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
        )
        with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
        monkeypatch.setattr(recovery, "capturar_registro", original)

        tombstone = derivar_autoridad_aprobacion(rig.storage).tombstones[0]
        trans_id = tombstone.consumed_by_transition_id
        assert trans_id is not None

        from sky_claw.local.frozen_runtime import autorizacion_hitl_transicion as hitl_module

        # Simular fallo de IO en write_json_atomic
        def _falla_write(*args: object, **kwargs: object) -> None:
            raise OSError("fallo de disco inyectado en write_json_atomic")

        monkeypatch.setattr(hitl_module, "write_json_atomic", _falla_write)

        with pytest.raises(OSError, match="fallo de disco inyectado"):
            sesion.abandonar(
                transition_id=trans_id,
                identidad=_identidad_telegram(),
                motivo="abandono con fallo de io",
            )
        monkeypatch.undo()

        # No se dejó ningún registro corrupto
        ruta_reg = storage.abandoned_transition_record_path(rig.storage, trans_id)
        assert not ruta_reg.exists()


def diagnostico_estado_actual(rig: RigStockManual) -> EstadoDurable:
    """Estado durable del root del rig (alias legible para los tests de reanudación)."""
    return diagnosticar_estado_durable(rig.storage).estado


def resultado_transicion(rig: RigStockManual) -> str:
    """transition_id del PENDING vigente (sólo para el caso negativo del test 33)."""
    snapshot = journal_module.cargar_journal_transicion(rig.storage)
    assert snapshot.journal is not None
    return snapshot.journal.transition_id


# ===========================================================================
# ENDURECIMIENTO DEL ABANDONO (revisión adversarial del hotfix)
# ===========================================================================

_UUID_A = "9b2f6c1e-3d4a-4e5f-8a6b-7c8d9e0f1a2b"
_UUID_B = "0f1e2d3c-4b5a-4968-8776-655443322110"


def _payload_abandono_valido(**cambios: object) -> dict[str, object]:
    """Registro de abandono v1 bien formado y con el digest RECOMPUTADO tras aplicar ``cambios``."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "root_id": "root-de-prueba",
        "transition_id": _UUID_A,
        "approval_id": "approval-1",
        "approval_seq": 1,
        "scope_digest": "a" * 64,
        "propietario_sujeto": str(OPERADOR),
        "propietario_superficie": "telegram",
        "propietario_sesion": str(OPERADOR),
        "propietario_mecanismo": "telegram_from_id",
        "motivo": "motivo de prueba",
        "abandonado_en_ns": 123456,
    }
    payload.update(cambios)
    payload["record_digest"] = _calcular_digest_abandono(payload)
    return payload


def _escribir_registro_abandono(ruta: pathlib.Path, payload: dict[str, object]) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


class TestAbandonoStoreYParserEnPosix:
    """Contratos del store y del parser que NO necesitan el binder: corren también fuera de Windows."""

    @pytest.mark.parametrize(
        "transition_id",
        [
            "../../escape",
            "..\\escape",
            "a/b",
            "C:evil",
            "",
            "   ",
            "trans_valida",
            _UUID_A.upper(),  # no canónico (minúsculas)
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # UUID v1
            _UUID_A + "\n",  # `$` de una regex sin fullmatch lo dejaría pasar
        ],
    )
    def test_48_la_ruta_del_registro_solo_acepta_uuid_v4_canonico(
        self, tmp_path: pathlib.Path, transition_id: str
    ) -> None:
        with pytest.raises(FrozenRuntimeStorageError):
            storage.abandoned_transition_record_path(tmp_path, transition_id)

    def test_48b_un_uuid_v4_canonico_queda_contenido_en_el_store(self, tmp_path: pathlib.Path) -> None:
        ruta = storage.abandoned_transition_record_path(tmp_path, _UUID_A)
        assert ruta.parent == storage.abandoned_transitions_state_dir(tmp_path)
        assert ruta.name == f"{_UUID_A}.json"

    @pytest.mark.parametrize("transition_id", ["../../escape", "a/b", "trans_valida", ""])
    def test_48c_abandonar_rechaza_ids_no_canonicos_antes_de_tocar_el_disco(
        self, tmp_path: pathlib.Path, transition_id: str
    ) -> None:
        """La validación va ANTES del lock y de cualquier lectura: ni siquiera hace falta un storage."""
        antes = sorted(p.name for p in tmp_path.rglob("*"))
        with pytest.raises(AbandonoTransicionInvalidoError):
            abandonar_transicion_consumida(
                tmp_path, transition_id=transition_id, identidad=_identidad_telegram(), motivo="intento"
            )
        assert sorted(p.name for p in tmp_path.rglob("*")) == antes

    def test_49_el_store_no_se_crea_a_traves_de_un_enlace(self, tmp_path: pathlib.Path) -> None:
        """Hermano de ``_exigir_candidates_state_dir`` (Codex sobre #682): un enlace NO muta fuera del root."""
        raiz = tmp_path / "root"
        externo = tmp_path / "externo"
        (raiz / "state").mkdir(parents=True)
        externo.mkdir()
        try:
            (raiz / "state" / "abandoned_transitions").symlink_to(externo, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("el SO no permite crear symlinks sin privilegios")
        with pytest.raises(FrozenRuntimeStorageError):
            storage.asegurar_directorio_abandonos(raiz)
        assert list(externo.iterdir()) == []

    def test_49b_state_redirigido_no_hace_mkdir_en_el_destino_externo(self, tmp_path: pathlib.Path) -> None:
        raiz = tmp_path / "root"
        externo = tmp_path / "externo"
        raiz.mkdir()
        externo.mkdir()
        try:
            (raiz / "state").symlink_to(externo, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("el SO no permite crear symlinks sin privilegios")
        with pytest.raises(FrozenRuntimeStorageError):
            storage.asegurar_directorio_abandonos(raiz)
        assert list(externo.iterdir()) == []

    @pytest.mark.parametrize(
        ("cambios", "motivo_del_rechazo"),
        [
            ({"approval_seq": "1"}, "entero positivo"),  # str() / int() habrían coercionado
            ({"approval_seq": True}, "entero positivo"),  # bool es int en Python
            ({"approval_seq": 0}, "entero positivo"),
            ({"abandonado_en_ns": -5}, "entero positivo"),
            ({"motivo": "   "}, "string no vacío"),
            ({"root_id": 12345}, "string no vacío"),
            ({"propietario_sujeto": None}, "string no vacío"),
            ({"transition_id": "../../escape"}, "UUID"),
            ({"schema_version": True}, "versión de schema"),
        ],
    )
    def test_50_un_registro_con_tipos_invalidos_es_corrupto_aunque_su_digest_cierre(
        self, tmp_path: pathlib.Path, cambios: dict[str, object], motivo_del_rechazo: str
    ) -> None:
        """El digest está RECOMPUTADO: lo que falla es el tipo, no el sello. Y sale como error del dominio."""
        ruta = tmp_path / "registro.json"
        _escribir_registro_abandono(ruta, _payload_abandono_valido(**cambios))
        with pytest.raises(AbandonoCorruptoError, match=motivo_del_rechazo):
            cargar_registro_abandono(ruta)

    def test_50b_el_esquema_es_cerrado(self, tmp_path: pathlib.Path) -> None:
        ruta = tmp_path / "registro.json"
        _escribir_registro_abandono(ruta, _payload_abandono_valido(campo_extra="x"))
        with pytest.raises(AbandonoCorruptoError, match="esquema cerrado"):
            cargar_registro_abandono(ruta)

        faltante = _payload_abandono_valido()
        del faltante["motivo"]
        _escribir_registro_abandono(ruta, faltante)
        with pytest.raises(AbandonoCorruptoError, match="esquema cerrado"):
            cargar_registro_abandono(ruta)

    def test_50c_un_registro_valido_round_trip(self, tmp_path: pathlib.Path) -> None:
        ruta = tmp_path / "registro.json"
        _escribir_registro_abandono(ruta, _payload_abandono_valido())
        registro = cargar_registro_abandono(ruta, expected_root_id="root-de-prueba")
        assert registro.transition_id == _UUID_A
        assert registro.approval_seq == 1

    def test_51_el_diagnostico_falla_cerrado_ante_un_registro_con_nombre_ajeno(self, tmp_path: pathlib.Path) -> None:
        """Un registro válido COPIADO bajo el nombre de otra transición no la resuelve."""
        raiz = tmp_path / "root"
        (raiz / "state").mkdir(parents=True)
        root_id = canonical_root_key(raiz)[0]
        ruta_ajena = storage.abandoned_transition_record_path(raiz, _UUID_B)
        _escribir_registro_abandono(ruta_ajena, _payload_abandono_valido(root_id=root_id, transition_id=_UUID_A))
        with pytest.raises(AbandonoCorruptoError, match="no coinciden"):
            _cargar_abandonos(raiz)

    def test_52_ancla_la_unica_via_de_reanudacion_pasa_por_el_diagnostico(self) -> None:
        """ENUMERA (no muestrea) quién llama al binder con ``aprobacion_consumida=``.

        El abandono sólo lo honra ``diagnosticar_estado_durable``, así que toda vía que reanude una
        aprobación consumida SIN pasar por ``SesionTransicionGoldenMaster.reanudar`` podría reanudar una
        transición ya abandonada. Un llamador nuevo rompe esta ancla hasta que se le decida su receta.
        """
        llamadores: set[tuple[str, str]] = set()
        for ruta in sorted((RAIZ_REPO / "sky_claw").rglob("*.py")):
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
            for funcion in ast.walk(arbol):
                if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for nodo in ast.walk(funcion):
                    if isinstance(nodo, ast.Call) and any(k.arg == "aprobacion_consumida" for k in nodo.keywords):
                        llamadores.add((ruta.name, funcion.name))
        assert llamadores == {("autorizacion_hitl_transicion.py", "reanudar")}


def _dejar_consumida_sin_pending(
    rig: RigStockManual, monkeypatch: pytest.MonkeyPatch
) -> tuple[SesionTransicionGoldenMaster, str]:
    """Aprobación CONSUMED y journal sin PENDING (el fallo se inyecta entre el burn y el PENDING)."""
    from sky_claw.local.frozen_runtime import binder_recovery as recovery

    sesion = _sesion(rig)
    _, entregar = _recolector()
    _habilitar_emision(monkeypatch)
    solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
    consentimiento = sesion.decidir_y_emitir(
        solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
    )
    assert consentimiento is not None
    original = recovery.capturar_registro
    monkeypatch.setattr(
        recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
    )
    with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
        sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
    monkeypatch.setattr(recovery, "capturar_registro", original)
    trans_id = derivar_autoridad_aprobacion(rig.storage).tombstones[0].consumed_by_transition_id
    assert trans_id is not None
    return sesion, trans_id


@SOLO_WINDOWS
class TestAbandonoEndurecidoExtremoAExtremo:
    def test_53_solo_el_dueno_que_autorizo_la_aprobacion_puede_abandonar(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Hermano de #373: una identidad VERIFICADA pero ajena a la aprobación no es el dueño."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, trans_id = _dejar_consumida_sin_pending(rig, monkeypatch)

        otro_operador = OPERADOR + 1
        identidad_ajena = verificar_identidad_telegram(
            {"from": {"id": otro_operador}, "chat": {"id": otro_operador, "type": "private"}},
            operador_configurado=otro_operador,
        )
        with pytest.raises(AbandonoTransicionInvalidoError, match="no es la del dueño"):
            sesion.abandonar(transition_id=trans_id, identidad=identidad_ajena, motivo="no soy el dueño")

        assert not storage.abandoned_transition_record_path(rig.storage, trans_id).exists()
        assert diagnosticar_estado_durable(rig.storage).estado is not EstadoDurable.LIMPIO

        # El dueño verdadero sí puede: el rechazo anterior no dejó estado que lo bloquee.
        registro = sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="dueño")
        assert registro.propietario_sujeto == str(OPERADOR)
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

    def test_54_ids_con_path_traversal_no_escapan_del_store(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, _ = _dejar_consumida_sin_pending(rig, monkeypatch)
        fuera = tmp_path / "evil.json"
        for malicioso in ("../../evil", "..\\..\\evil", f"../{_UUID_A}", "a/b"):
            with pytest.raises(AbandonoTransicionInvalidoError):
                sesion.abandonar(transition_id=malicioso, identidad=_identidad_telegram(), motivo="intento")
        assert not fuera.exists()
        assert not list(storage.abandoned_transitions_state_dir(rig.storage).glob("*"))

    def test_55_un_registro_presente_con_otro_id_interno_no_se_sobrescribe(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """El registro es INMUTABLE: si el archivo ya está y no es de esta transición, falla cerrado."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, trans_id = _dejar_consumida_sin_pending(rig, monkeypatch)
        ruta = storage.abandoned_transition_record_path(rig.storage, trans_id)
        root_id = canonical_root_key(rig.storage)[0]
        _escribir_registro_abandono(ruta, _payload_abandono_valido(root_id=root_id, transition_id=_UUID_B))
        antes = ruta.read_bytes()

        with pytest.raises(AbandonoCorruptoError, match="no coinciden"):
            sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="intento de pisar")

        assert ruta.read_bytes() == antes
        assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.REQUIERE_DUENO

    def test_56_el_replay_idempotente_conserva_el_registro_original(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Un reintento con OTRO motivo devuelve el registro original: la evidencia no se reescribe."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, trans_id = _dejar_consumida_sin_pending(rig, monkeypatch)
        primero = sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="motivo original")
        ruta = storage.abandoned_transition_record_path(rig.storage, trans_id)
        bytes_originales = ruta.read_bytes()

        segundo = sesion.abandonar(transition_id=trans_id, identidad=_identidad_telegram(), motivo="otro motivo")

        assert segundo == primero
        assert segundo.motivo == "motivo original"
        assert ruta.read_bytes() == bytes_originales
