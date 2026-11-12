"""P7 — frontera de autorización: el binder consume una aprobación que liga INI e instancia MO2.

Hallazgos que cierra este archivo:

* **A (INI aprobado).** ``SesionTransicionGoldenMaster.ejecutar()`` verifica la evidencia del INI y SÓLO
  DESPUÉS el binder lee el INI por su cuenta. Esa lectura era el ancla de toda su cadena
  (revalidación bajo lock → snapshot → huella bajo exclusión) y nunca se comparaba contra el INI que el
  propietario aprobó: un tercero que cambiaba el INI en la ventana llegaba a ``FINALIZED`` con una
  configuración distinta de la autorizada (Codex, discussion_r4543293593).
* **B (R-1, identidad de instancia MO2).** La instancia sólo se verificaba en el adaptador HITL
  (``approval_provenance``). El binder, único consumidor, aceptaba cualquier instancia activa.

Todos los escenarios usan el rig sintético: ninguna instalación real se toca y
``EMISION_PRODUCTIVA_HABILITADA`` sólo se abre dentro de cada test (``_habilitar_emision``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    cargar_ledger_aprobacion,
    derivar_autoridad_aprobacion,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.clones import cargar_runtime_clone_record
from sky_claw.local.frozen_runtime.effective_runtime.models import Mo2RegistryObservation, Mo2RegistryState
from sky_claw.local.frozen_runtime.mo2_authorization import (
    CLAVE_SEGMENTO,
    IDENTIDAD_AUTORIZADA,
    Mo2AuthorizationAusenteError,
    Mo2AuthorizationMalformadaError,
    Mo2AuthorizationMismatchError,
    calcular_token_mo2,
    clave_de_ruta,
    es_vinculo_obligatorio,
    segmento_de_procedencia,
    token_comprometido,
    verificar_vinculo,
)
from sky_claw.local.frozen_runtime.owner_consent import DecisionConsentimiento
from sky_claw.local.frozen_runtime.runtime_clone_binder import (
    BinderAutorizacionMo2Error,
    ejecutar_transicion_binder,
)
from sky_claw.local.frozen_runtime.storage import issuances_state_dir, recovery_state_dir
from tests._rig_p7_a5 import RigStockManual, aplicar_parche_identidad_pe, probes_con_registro_simulado
from tests.test_frozen_runtime_p5c1_mo2_clone_binder import _preparar_aprobacion
from tests.test_frozen_runtime_p7_a5_pr3_hitl_wiring import (
    SOLO_WINDOWS,
    _ExclusionPosix,
    _habilitar_emision,
    _identidad_telegram,
    _recolector,
    _sesion,
)

INSTANCIA_B = "Instancia B"
_NO_SON_LOS_APROBADOS = "no son los que el propietario aprobó"
_FALTA_EL_VINCULO = "falta mo2auth"


@pytest.fixture(autouse=True)
def _identidad_pe(monkeypatch: pytest.MonkeyPatch) -> None:
    aplicar_parche_identidad_pe(monkeypatch)


@pytest.fixture
def exclusion_de_test(monkeypatch: pytest.MonkeyPatch) -> str:
    """Exclusión real en Windows; doble POSIX fuera (mismo criterio que el test de wiring)."""
    import os

    if os.name == "nt":
        return "real"
    monkeypatch.setattr(
        binder_module,
        "adquirir_exclusion_ini",
        lambda ruta, **_kwargs: _ExclusionPosix(pathlib.Path(ruta)),
    )
    return "doble_posix"


# ---------------------------------------------------------------------------
# Utilidades de escenario
# ---------------------------------------------------------------------------


def _ini_con_argumentos(ini_path: pathlib.Path) -> bytes:
    """INI B: mismo ``gamePath`` y mismos binarios, pero con ARGUMENTOS en un ejecutable personalizado."""
    previo = ini_path.read_bytes()
    salto = b"\r\n" if b"\r\n" in previo else b"\n"
    marcador = b"2\\title=LOOT"
    assert marcador in previo
    return previo.replace(marcador, b"1\\arguments=-nointro" + salto + marcador, 1)


def _ini_con_clave_ajena(ini_path: pathlib.Path) -> bytes:
    """INI B': mismos ``gamePath``, binarios y argumentos; sólo una clave ajena de ``[Settings]`` (cambian los bytes)."""
    previo = ini_path.read_bytes()
    salto = b"\r\n" if b"\r\n" in previo else b"\n"
    return previo.rstrip(b"\r\n") + salto + b"notas=cambio-externo" + salto


def _mutar_ini_en_la_ventana(monkeypatch: pytest.MonkeyPatch, ini_path: pathlib.Path, nuevo: bytes) -> list[int]:
    """Cambia el INI justo ANTES de que el binder lo lea por primera vez (la ventana de Codex).

    ``_exigir_quiescencia(momento="preflight inicial")`` es lo último que hace el binder antes de leer el
    INI, y la sesión ya verificó su evidencia: es exactamente el hueco entre «el dueño aprobó» y «el binder
    mira».
    """
    original = binder_module._exigir_quiescencia
    llamadas: list[int] = []

    def _hook(*args: object, **kwargs: object) -> object:
        if not llamadas:
            ini_path.write_bytes(nuevo)
        llamadas.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(binder_module, "_exigir_quiescencia", _hook)
    return llamadas


def _superficies_no_ini(rig: RigStockManual) -> list[pathlib.Path]:
    return [
        rig.config_path,
        rig.manual_runtime,
        rig.managed_source,
        rig.stock_source,
        rig.stock_base,
        rig.storage / "versions",
    ]


def _huellas_no_ini(rig: RigStockManual) -> dict[str, str]:
    from tests._rig_p7_a5 import snapshot_sha256_recursivo

    return snapshot_sha256_recursivo(_superficies_no_ini(rig))


def _emitir(rig: RigStockManual, monkeypatch: pytest.MonkeyPatch):
    """Sesión sobre la instancia A con una aprobación EMITIDA (ISSUED) y la solicitud retenida."""
    sesion = _sesion(rig)
    _, entregar = _recolector()
    _habilitar_emision(monkeypatch)
    solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
    consentimiento = sesion.decidir_y_emitir(
        solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
    )
    assert consentimiento is not None
    return sesion, solicitud, consentimiento


def _consumir_sin_pending(rig: RigStockManual, monkeypatch: pytest.MonkeyPatch):
    """Aprobación CONSUMED y journal sin PENDING (fallo inyectado entre el burn y el PENDING)."""
    sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
    original = recovery.capturar_registro
    monkeypatch.setattr(
        recovery, "capturar_registro", lambda *a, **k: (_ for _ in ()).throw(recovery.RecuperacionError("fail"))
    )
    with pytest.raises(Exception):  # noqa: B017 — el fallo lo clasifica el binder
        sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)
    monkeypatch.setattr(recovery, "capturar_registro", original)
    trans_id = derivar_autoridad_aprobacion(rig.storage).tombstones[0].consumed_by_transition_id
    assert trans_id is not None
    return sesion, solicitud, consentimiento, trans_id


def _kwargs_binder(rig: RigStockManual, mo2_dir: pathlib.Path, probes: object) -> dict[str, object]:
    """Los mismos argumentos que la sesión le pasa al binder (política Stock + runtime saliente)."""
    return {
        "storage_root": rig.storage,
        "mo2_instance_dir": mo2_dir,
        "target_clone_id": rig.clone_id,
        "managed_source_root": rig.managed_source,
        "config_path": rig.config_path,
        "environ": rig.environ,
        "probes": probes,
        "transition_policy": BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        "outgoing_manual_runtime_root": rig.manual_runtime,
        "stock_source_root": rig.stock_source,
        "approved_stock_reference_digest_file": rig.approved_digest_file,
    }


def _crear_instancia_b(rig: RigStockManual) -> tuple[pathlib.Path, object]:
    """Instancia GLOBAL B (la «actual» de MO2 para sus probes) con el MISMO INI que A, byte a byte."""
    inst_b = rig.local_app_data / "ModOrganizer" / INSTANCIA_B
    inst_b.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rig.mo2_active_global / "modorganizer.ini", inst_b / "modorganizer.ini")
    probes_b = probes_con_registro_simulado(
        local_app_data=rig.local_app_data,
        registry_obs=Mo2RegistryObservation(state=Mo2RegistryState.OBSERVED, current_instance=INSTANCIA_B),
    )
    return inst_b, probes_b


def _estado_durable_sin_mutacion(rig: RigStockManual) -> None:
    """Nada se publicó: ni PENDING, ni registros de recuperación, ni FINALIZED."""
    snapshot = journal_module.cargar_journal_transicion(rig.storage)
    assert snapshot.disposition in (journal_module.JournalDisposition.ABSENT, journal_module.JournalDisposition.NONE)
    assert not list(recovery_state_dir(rig.storage).glob("*.json"))
    assert journal_module.derivar_historial_finalizados(rig.storage).registros == ()


# ===========================================================================
# A — el INI aprobado es el que el binder muta
# ===========================================================================


@SOLO_WINDOWS
class TestIniAprobadoEsElQueElBinderMuta:
    def test_a1_ini_modificado_entre_hitl_y_binder_se_rechaza_antes_del_burn(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """RED A1: aprobación sobre el INI A; otro proceso lo cambia a B antes de la 1ª lectura del binder."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_argumentos(ini)
        assert ini_b != ini.read_bytes()
        sin_ini_antes = _huellas_no_ini(rig)
        environ_antes = dict(rig.environ)
        _mutar_ini_en_la_ventana(monkeypatch, ini, ini_b)

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        # Ninguna superficie se tocó más allá del cambio del tercero, y NO hay FINALIZED.
        assert ini.read_bytes() == ini_b
        assert _huellas_no_ini(rig) == sin_ini_antes
        assert rig.environ == environ_antes
        _estado_durable_sin_mutacion(rig)
        # Divergencia detectable ANTES del burn: la aprobación sigue ISSUED y sin tombstone.
        ledger = cargar_ledger_aprobacion(rig.storage).ledger
        assert ledger is not None and ledger.state is ApprovalLifecycle.ISSUED
        assert derivar_autoridad_aprobacion(rig.storage).tombstones == ()

    def test_a3_el_vinculo_bajo_el_lock_no_depende_del_ancla_previa(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Defensa en profundidad: aunque se DEBILITE el eslabón previo, el vínculo bajo el lock frena.

        La revalidación bajo lock compara el INI contra la primera lectura del binder; un cambio entre ambas ya
        lo atrapa ella. Acá se simula que ese ancla se REEMPLAZA por el contenido nuevo (la clase de defecto
        que se está cerrando: validar contra otra lectura del mismo INI modificado): el vínculo con lo APROBADO
        tiene que seguir rechazando, y antes del snapshot y del PENDING.
        """
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_clave_ajena(ini)  # no toca gamePath ni ejecutables: el plan de migración no cambia
        sin_ini_antes = _huellas_no_ini(rig)
        original = binder_module._revalidar_bajo_lock

        def _ancla_reemplazada(**kwargs: object) -> object:
            ini.write_bytes(ini_b)  # el INI cambia DESPUÉS de la verificación previa al burn
            previo = kwargs["previo"]
            kwargs["previo"] = dataclasses.replace(previo, ini_sha256=recovery.huella(ini_b))  # type: ignore[type-var]
            return original(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(binder_module, "_revalidar_bajo_lock", _ancla_reemplazada)

        with pytest.raises(BinderAutorizacionMo2Error, match=r"\[bajo el lock\]"):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        assert ini.read_bytes() == ini_b  # sólo el cambio del tercero: el binder no escribió nada
        assert _huellas_no_ini(rig) == sin_ini_antes
        _estado_durable_sin_mutacion(rig)

    @staticmethod
    def _neutralizar_etapas_salvo(monkeypatch: pytest.MonkeyPatch, etapa_activa: str) -> None:
        """Deja activa SÓLO una etapa del vínculo: así cada eslabón se prueba sin que otro lo tape."""
        original = binder_module._exigir_vinculo_mo2

        def _filtrado(*args: object, **kwargs: object) -> None:
            if kwargs.get("etapa") == etapa_activa:
                original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(binder_module, "_exigir_vinculo_mo2", _filtrado)

    def _rechazo_sin_mutacion(
        self, rig: RigStockManual, ini: pathlib.Path, ini_b: bytes, sin_ini_antes: dict[str, str]
    ) -> None:
        """Post-rechazo en el punto de mutación: el cambio del tercero se PRESERVA y no hay FINALIZED."""
        assert ini.read_bytes() == ini_b  # el binder no escribió ni «restauró» sobre el cambio de un tercero
        assert _huellas_no_ini(rig) == sin_ini_antes
        assert journal_module.derivar_historial_finalizados(rig.storage).registros == ()

    def test_a4_el_vinculo_bajo_la_exclusion_frena_aunque_los_eslabones_previos_cedan(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """El PUNTO DE MUTACIÓN: con las etapas previas neutralizadas, lo único que separa un INI no aprobado
        de la escritura es la comparación sobre los bytes que retiene la exclusión."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_clave_ajena(ini)
        sin_ini_antes = _huellas_no_ini(rig)
        self._neutralizar_etapas_salvo(monkeypatch, "bajo la exclusión")
        _mutar_ini_en_la_ventana(monkeypatch, ini, ini_b)

        with pytest.raises(BinderAutorizacionMo2Error, match="bajo la exclusión"):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        self._rechazo_sin_mutacion(rig, ini, ini_b, sin_ini_antes)

    def test_a5_la_huella_bajo_la_exclusion_contra_el_snapshot_sigue_frenando(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Eslabón L1 (preexistente, antes sin test que lo discriminara): el INI cambia ENTRE el snapshot y la
        adquisición de la exclusión. Se neutraliza el vínculo bajo la exclusión para que no lo tape."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_clave_ajena(ini)
        sin_ini_antes = _huellas_no_ini(rig)
        self._neutralizar_etapas_salvo(monkeypatch, "ninguna")
        adquirir = binder_module.adquirir_exclusion_ini

        def _cambia_y_adquiere(ruta: pathlib.Path, **kwargs: object) -> object:
            ini.write_bytes(ini_b)  # después del snapshot, antes de que el handle retenga el INI
            return adquirir(ruta, **kwargs)

        monkeypatch.setattr(binder_module, "adquirir_exclusion_ini", _cambia_y_adquiere)

        with pytest.raises(binder_module.BinderVerificationError, match="entre el snapshot durable y la exclusión"):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        self._rechazo_sin_mutacion(rig, ini, ini_b, sin_ini_antes)

    def test_a6_el_snapshot_se_ancla_a_los_bytes_revalidados(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Eslabón L2 (preexistente): el INI cambia ENTRE la revalidación bajo lock y el snapshot durable.
        El vínculo bajo la exclusión sigue activo: si L2 faltara, lo atraparía OTRO control y la clase del error
        delataría que L2 dejó de actuar."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, solicitud, consentimiento = _emitir(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_clave_ajena(ini)
        sin_ini_antes = _huellas_no_ini(rig)
        capturar = recovery.capturar_registro

        def _cambia_y_captura(*args: object, **kwargs: object) -> object:
            ini.write_bytes(ini_b)
            return capturar(*args, **kwargs)

        monkeypatch.setattr(recovery, "capturar_registro", _cambia_y_captura)

        with pytest.raises(
            binder_module.BinderVerificationError, match="entre la revalidación bajo lock y el snapshot"
        ):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consentimiento)

        self._rechazo_sin_mutacion(rig, ini, ini_b, sin_ini_antes)

    def test_a2_reanudacion_directa_sobre_ini_distinto_se_rechaza_sin_la_sesion_original(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """RED A2: transición CONSUMED y reanudable; el INI cambia; el binder, SIN sesión, contrasta lo aprobado."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, _solicitud, _consentimiento, trans_id = _consumir_sin_pending(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini.write_bytes(_ini_con_argumentos(ini))
        sin_ini_antes = _huellas_no_ini(rig)
        aprobacion, _witness = sesion._aprobacion_consumida_durable(trans_id)

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, rig.mo2_active_global, rig.probes_globales_activos()),
                aprobacion_consumida=aprobacion,
                transition_id=trans_id,
            )

        assert _huellas_no_ini(rig) == sin_ini_antes
        _estado_durable_sin_mutacion(rig)

    def test_a2b_reanudar_no_rebaja_la_validacion_a_la_configuracion_observada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """RED A2b: ``reanudar()`` verifica la evidencia y, en la ventana siguiente, el INI cambia."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion, _solicitud, _consentimiento, trans_id = _consumir_sin_pending(rig, monkeypatch)
        ini = rig.mo2_active_global / "modorganizer.ini"
        ini_b = _ini_con_argumentos(ini)
        sin_ini_antes = _huellas_no_ini(rig)
        _mutar_ini_en_la_ventana(monkeypatch, ini, ini_b)

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            sesion.reanudar(transition_id=trans_id)

        assert ini.read_bytes() == ini_b
        assert _huellas_no_ini(rig) == sin_ini_antes
        _estado_durable_sin_mutacion(rig)


# ===========================================================================
# B — R-1: la aprobación liga la instancia MO2 que el binder consume
# ===========================================================================


@SOLO_WINDOWS
class TestAprobacionLigaLaInstanciaMo2:
    def test_b1_aprobacion_de_a_no_se_consume_sobre_b_via_binder_directo(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """RED B1: aprobación de la instancia A; B apunta al MISMO Clone y tiene el MISMO INI; binder directo."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        _sesion_a, solicitud, consentimiento = _emitir(rig, monkeypatch)
        inst_b, probes_b = _crear_instancia_b(rig)
        sin_ini_antes = _huellas_no_ini(rig)
        ini_b_antes = (inst_b / "modorganizer.ini").read_bytes()

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, inst_b, probes_b),
                propuesta=solicitud.propuesta,
                consentimiento=consentimiento,
                transition_id=solicitud.transicion_id,
            )

        # Rechazo ANTES de cualquier escritura y antes del burn.
        assert (inst_b / "modorganizer.ini").read_bytes() == ini_b_antes
        assert _huellas_no_ini(rig) == sin_ini_antes
        _estado_durable_sin_mutacion(rig)
        ledger = cargar_ledger_aprobacion(rig.storage).ledger
        assert ledger is not None and ledger.state is ApprovalLifecycle.ISSUED

    def test_b2_reanudacion_desde_b_se_rechaza_y_la_recuperacion_legitima_desde_a_sigue_viva(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """RED B2: CONSUMED para A, interrumpida antes del PENDING; se intenta reanudar desde B."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        sesion_a, _solicitud, _consentimiento, trans_id = _consumir_sin_pending(rig, monkeypatch)
        inst_b, probes_b = _crear_instancia_b(rig)
        aprobacion, _witness = sesion_a._aprobacion_consumida_durable(trans_id)
        sin_ini_antes = _huellas_no_ini(rig)

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, inst_b, probes_b),
                aprobacion_consumida=aprobacion,
                transition_id=trans_id,
            )

        assert _huellas_no_ini(rig) == sin_ini_antes
        _estado_durable_sin_mutacion(rig)
        # La capacidad legítima NO se perdió: reanudar desde A completa la transición.
        resultado = sesion_a.reanudar(transition_id=trans_id)
        assert resultado.binder is not None and resultado.binder.success is True
        assert (
            approval_module.derivar_autoridad_aprobacion(rig.storage).tombstones[0].consumed_by_transition_id
            == trans_id
        )


# ===========================================================================
# Contrato del vínculo (puro, corre en cualquier SO)
# ===========================================================================

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]
_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _token(**cambios: object) -> str:
    base: dict[str, object] = {
        "mo2_instance_dir": pathlib.Path("/mo2/instancia"),
        "mo2_instance_identity": IDENTIDAD_AUTORIZADA,
        "mo2_ini_sha256": _SHA_A,
        "transition_policy": BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
        "outgoing_manual_runtime_root": pathlib.Path("/runtime/manual"),
    }
    base.update(cambios)
    return calcular_token_mo2(**base)  # type: ignore[arg-type]


class TestContratoDelVinculo:
    @pytest.mark.parametrize(
        "cambio",
        [
            {"mo2_instance_dir": pathlib.Path("/mo2/otra")},
            {"mo2_instance_identity": "active_portable"},
            {"mo2_ini_sha256": _SHA_B},
            {"transition_policy": BinderTransitionPolicy.STRICT_PROVIDER_IDLE},
            {"outgoing_manual_runtime_root": pathlib.Path("/runtime/otro")},
            {"outgoing_manual_runtime_root": None},
        ],
        ids=["instancia", "identidad", "ini", "politica", "runtime-saliente", "sin-runtime-saliente"],
    )
    def test_31_cada_campo_del_vinculo_cambia_el_token(self, cambio: dict[str, object]) -> None:
        """ENUMERA los campos: ninguno puede cambiar sin que cambie lo que el propietario aprobó."""
        assert _token(**cambio) != _token()

    def test_32_el_token_es_determinista_y_normaliza_las_rutas(self) -> None:
        assert _token() == _token()
        assert _token(mo2_instance_dir=pathlib.Path("/mo2/instancia/")) == _token()
        assert _token(mo2_instance_dir=pathlib.Path("/mo2/x/../instancia")) == _token()

    @pytest.mark.parametrize("sha", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, None, 123])
    def test_33_un_sha_invalido_no_produce_un_token(self, sha: object) -> None:
        with pytest.raises(Mo2AuthorizationMalformadaError):
            _token(mo2_ini_sha256=sha)

    def test_34_gramatica_de_la_procedencia(self) -> None:
        tok = _token()
        seg = segmento_de_procedencia(tok)
        assert token_comprometido("golden-master:v1:inst=x:ini=y") is None
        assert token_comprometido(seg) == tok  # al principio
        assert token_comprometido(f"golden-master:v1:inst=x:{seg}:bind=abc") == tok  # en el medio
        assert token_comprometido(f"golden-master:v1:inst=x:{seg}") == tok  # al final

    @pytest.mark.parametrize(
        "procedencia",
        [
            f"{CLAVE_SEGMENTO}={'a' * 64}:{CLAVE_SEGMENTO}={'b' * 64}",  # dos segmentos: no se «elige» uno
            f"{CLAVE_SEGMENTO}={'a' * 63}",
            f"{CLAVE_SEGMENTO}={'a' * 65}",
            f"{CLAVE_SEGMENTO}={'A' * 64}",
            f"x{CLAVE_SEGMENTO}={'a' * 64}",  # pegado a otro segmento: no está delimitado
            f"{CLAVE_SEGMENTO}={'a' * 64}z",
            f"{CLAVE_SEGMENTO}=",
        ],
        ids=["duplicado", "corto", "largo", "mayusculas", "pegado-antes", "pegado-despues", "vacio"],
    )
    def test_35_una_procedencia_ambigua_o_mal_formada_falla_cerrado(self, procedencia: str) -> None:
        with pytest.raises(Mo2AuthorizationMalformadaError):
            token_comprometido(procedencia)

    def test_36_ningun_segmento_legible_puede_fabricar_un_token_ajeno(self) -> None:
        """Los segmentos legibles se escapan con ``quote``: ``=`` y ``:`` no sobreviven, así que una ruta no
        puede inyectar ``mo2auth=``. Se verifica sobre el escape REAL del adaptador."""
        from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import _segmento

        hostil = _segmento(f"C:\\x:{CLAVE_SEGMENTO}={'f' * 64}")
        assert CLAVE_SEGMENTO + "=" not in hostil

    def test_37_tabla_de_obligatoriedad_congelada_por_igualdad_literal(self) -> None:
        """(binding de solicitud presente, transición P7-A5) → ¿el vínculo es obligatorio?

        Cambiar una celda rompe este test: relajar la obligatoriedad es una decisión de contrato, no un detalle.
        """
        tabla = {
            (binding, p7): es_vinculo_obligatorio(solicitud_binding_presente=binding, transicion_p7_a5=p7)
            for binding in (False, True)
            for p7 in (False, True)
        }
        assert tabla == {(False, False): False, (False, True): True, (True, False): True, (True, True): True}

    def test_38_semantica_de_verificar_vinculo(self) -> None:
        tok = _token()
        assert verificar_vinculo(token_aprobado=tok, token_observado=tok, obligatorio=True, etapa="t") is True
        assert verificar_vinculo(token_aprobado=None, token_observado=tok, obligatorio=False, etapa="t") is False
        with pytest.raises(Mo2AuthorizationAusenteError):
            verificar_vinculo(token_aprobado=None, token_observado=tok, obligatorio=True, etapa="t")
        # Un token PRESENTE se compara siempre, aunque la operación no lo exigiera.
        with pytest.raises(Mo2AuthorizationMismatchError):
            verificar_vinculo(
                token_aprobado=_token(mo2_ini_sha256=_SHA_B), token_observado=tok, obligatorio=False, etapa="t"
            )

    def test_39_las_tres_normalizaciones_de_ruta_del_subsistema_coinciden(self) -> None:
        """El vínculo compara rutas calculadas en TRES lugares (adaptador, binder y este módulo): si divergieran,
        una aprobación legítima se rechazaría o, peor, una ajena coincidiría."""
        from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import _clave

        entradas = ["/a/b", "/a/b/", "/a/./b", "/a/x/../b", "relativa/ruta", "/A/B", "//a//b"]
        for entrada in entradas:
            assert clave_de_ruta(entrada) == _clave(entrada) == binder_module._norm(entrada), entrada
        assert clave_de_ruta(None) is None and _clave(None) is None and binder_module._norm(None) is None

    def test_40_la_unica_via_de_emision_con_binding_justifica_tolerar_el_legacy_sin_vinculo(self) -> None:
        """La ausencia del vínculo sólo se tolera sin binding y sin P7-A5. Eso es defendible porque las
        aprobaciones sin binding sólo salen de fixtures: ``emitir_aprobacion`` siempre levanta y el emisor
        privado lo invoca ÚNICAMENTE ``owner_consent.emitir_aprobacion_autorizada`` (que exige el binding).
        ENUMERA los llamadores en ``sky_claw/``: uno nuevo rompe el ancla hasta que se decide su receta."""
        llamadores: set[tuple[str, str]] = set()
        for ruta in sorted((RAIZ_REPO / "sky_claw").rglob("*.py")):
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
            for funcion in ast.walk(arbol):
                if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for nodo in ast.walk(funcion):
                    if (
                        isinstance(nodo, ast.Call)
                        and isinstance(nodo.func, ast.Attribute)
                        and nodo.func.attr == "_emitir_aprobacion_bajo_lock"
                    ):
                        llamadores.add((ruta.name, funcion.name))
        assert llamadores == {("owner_consent.py", "emitir_aprobacion_autorizada")}

    def test_41_el_binder_exige_el_vinculo_antes_del_burn_bajo_el_lock_y_bajo_la_exclusion(self) -> None:
        """ANCLA por AST: ``_exigir_vinculo_mo2`` se llama exactamente en TRES etapas.

        1. ANTES de ``consumir_aprobacion`` y fuera del lock (no se gasta una aprobación que no liga);
        2. dentro del ``with handle_lock``, antes del snapshot y del ``PENDING``;
        3. dentro del lock, DESPUÉS de adquirir la exclusión del INI y ANTES de escribirlo: es el último control
           y compara los bytes que el handle retenido va a mutar.
        """
        fuente = (RAIZ_REPO / "sky_claw" / "local" / "frozen_runtime" / "runtime_clone_binder.py").read_text(
            encoding="utf-8"
        )
        funcion = next(
            n
            for n in ast.walk(ast.parse(fuente))
            if isinstance(n, ast.FunctionDef) and n.name == "ejecutar_transicion_binder"
        )
        seccion = next(
            n
            for n in ast.walk(funcion)
            if isinstance(n, ast.With)
            and any(isinstance(i.context_expr, ast.Name) and i.context_expr.id == "handle_lock" for i in n.items)
        )

        def _llamadas(nodo: ast.AST, nombre: str) -> list[ast.Call]:
            return [
                n
                for n in ast.walk(nodo)
                if isinstance(n, ast.Call)
                and (
                    (isinstance(n.func, ast.Name) and n.func.id == nombre)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
                )
            ]

        def _etapa(llamada: ast.Call) -> str:
            kw = next(k for k in llamada.keywords if k.arg == "etapa")
            assert isinstance(kw.value, ast.Constant)
            return str(kw.value.value)

        todas = _llamadas(funcion, "_exigir_vinculo_mo2")
        assert sorted(_etapa(c) for c in todas) == ["antes del burn", "bajo el lock", "bajo la exclusión"]
        por_etapa = {_etapa(c): c for c in todas}
        previa, bajo_lock, bajo_exclusion = (
            por_etapa["antes del burn"],
            por_etapa["bajo el lock"],
            por_etapa["bajo la exclusión"],
        )
        burn = min(c.lineno for c in _llamadas(funcion, "consumir_aprobacion"))
        assert previa.lineno < burn, "el vínculo debe verificarse ANTES de gastar la aprobación"
        dentro_del_lock = {id(c) for c in _llamadas(seccion, "_exigir_vinculo_mo2")}
        assert id(previa) not in dentro_del_lock
        assert id(bajo_lock) in dentro_del_lock and id(bajo_exclusion) in dentro_del_lock
        for posterior in ("capturar_registro", "_crear_pendiente_locked", "_start_next_locked"):
            assert bajo_lock.lineno < min(c.lineno for c in _llamadas(seccion, posterior)), posterior
        adquisicion = min(c.lineno for c in _llamadas(seccion, "adquirir_exclusion_ini"))
        escrituras = [c.lineno for nombre in ("actualizar_game_path_por_handle",) for c in _llamadas(seccion, nombre)]
        assert adquisicion < bajo_exclusion.lineno < min(escrituras), (
            "el último control va entre la exclusión y la escritura"
        )

    def test_42_el_adaptador_compromete_en_la_procedencia_lo_que_el_binder_recomputa(
        self, tmp_path: pathlib.Path
    ) -> None:
        """La procedencia REAL que arma el adaptador trae el token que el binder calcularía desde su observación."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        evidencia = _sesion(rig).derivar_evidencia()
        esperado = calcular_token_mo2(
            mo2_instance_dir=rig.mo2_active_global,
            mo2_instance_identity=IDENTIDAD_AUTORIZADA,
            mo2_ini_sha256=hashlib.sha256((rig.mo2_active_global / "modorganizer.ini").read_bytes()).hexdigest(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
        )
        assert token_comprometido(evidencia.approval_provenance) == esperado


# ===========================================================================
# B3 — manipulación, replay y compatibilidad (E2E)
# ===========================================================================


@SOLO_WINDOWS
class TestManipulacionReplayYCompatibilidad:
    def test_b3_1_si_la_instancia_aprobada_deja_de_ser_la_global_activa_el_binder_no_la_muta(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """Identidad global observada distinta: MO2 pasa a tener a B como instancia actual (HKCU) y se intenta
        consumir la aprobación de A sobre A. La exigencia «instancia GLOBAL ACTIVA» la impone el preflight del
        binder (``Mo2InstanceIdentityObservation.is_active``): el vínculo agrega QUÉ instancia, no CUÁL es activa."""
        from sky_claw.local.frozen_runtime.runtime_clone_binder import BinderPreflightFailedError

        rig = RigStockManual(tmp_path, steam_activo=True)
        _sesion_a, solicitud, consentimiento = _emitir(rig, monkeypatch)
        _inst_b, probes_b = _crear_instancia_b(rig)  # el registro ahora marca a B como actual
        ini_antes = (rig.mo2_active_global / "modorganizer.ini").read_bytes()

        with pytest.raises(BinderPreflightFailedError, match="mo2_instance"):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, rig.mo2_active_global, probes_b),
                propuesta=solicitud.propuesta,
                consentimiento=consentimiento,
                transition_id=solicitud.transicion_id,
            )

        assert (rig.mo2_active_global / "modorganizer.ini").read_bytes() == ini_antes
        _estado_durable_sin_mutacion(rig)

    def test_b3_2_politica_distinta_se_rechaza(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """La aprobación emitida bajo una política no se reutiliza bajo otra (mismo INI, misma instancia)."""
        rig = RigStockManual(tmp_path, steam_activo=False)
        sesion = _sesion(rig, policy=BinderTransitionPolicy.STRICT_PROVIDER_IDLE)
        _, entregar = _recolector()
        _habilitar_emision(monkeypatch)
        solicitud = sesion.preparar(identidad=_identidad_telegram(), entregar=entregar)
        consentimiento = sesion.decidir_y_emitir(
            solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=_identidad_telegram()
        )
        assert consentimiento is not None

        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, rig.mo2_active_global, rig.probes_globales_activos()),  # política STOCK
                propuesta=solicitud.propuesta,
                consentimiento=consentimiento,
                transition_id=solicitud.transicion_id,
            )
        _estado_durable_sin_mutacion(rig)

    def test_b3_3_witness_manipulado_falla_cerrado_antes_del_burn(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """El vínculo sale del witness DURABLE: reescribir su procedencia rompe el digest y no se consume."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        _sesion_a, solicitud, consentimiento = _emitir(rig, monkeypatch)
        archivos = sorted(issuances_state_dir(rig.storage).glob("*.json"))
        assert len(archivos) == 1
        payload = json.loads(archivos[0].read_text(encoding="utf-8"))
        original = payload["scope_payload"]["approval_provenance"]
        token = token_comprometido(original)
        assert token is not None
        payload["scope_payload"]["approval_provenance"] = original.replace(token, "0" * 64)
        archivos[0].write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(BinderAutorizacionMo2Error):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, rig.mo2_active_global, rig.probes_globales_activos()),
                propuesta=solicitud.propuesta,
                consentimiento=consentimiento,
                transition_id=solicitud.transicion_id,
            )
        ledger = cargar_ledger_aprobacion(rig.storage).ledger
        assert ledger is not None and ledger.state is ApprovalLifecycle.ISSUED
        _estado_durable_sin_mutacion(rig)

    def test_b3_4_una_procedencia_reescrita_por_el_llamador_no_sustituye_a_la_del_witness(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exclusion_de_test: str
    ) -> None:
        """El llamador fabrica un scope con el token de OTRA instancia y su digest recomputado: el witness
        durable no liga ese digest, así que no hay autoridad que consumir (y nada se gasta)."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        _sesion_a, solicitud, consentimiento = _emitir(rig, monkeypatch)
        inst_b, probes_b = _crear_instancia_b(rig)
        token_b = calcular_token_mo2(
            mo2_instance_dir=inst_b,
            mo2_instance_identity=IDENTIDAD_AUTORIZADA,
            mo2_ini_sha256=hashlib.sha256((inst_b / "modorganizer.ini").read_bytes()).hexdigest(),
            transition_policy=BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            outgoing_manual_runtime_root=rig.manual_runtime,
        )
        original = solicitud.propuesta.scope
        procedencia_falsa = original.approval_provenance.replace(
            token_comprometido(original.approval_provenance) or "", token_b
        )
        borrador = dataclasses.replace(original, approval_provenance=procedencia_falsa)
        scope_falso = dataclasses.replace(
            borrador, approval_scope_digest=approval_module.calcular_approval_scope_digest(borrador)
        )
        propuesta_falsa = dataclasses.replace(solicitud.propuesta, scope=scope_falso)
        consentimiento_falso = dataclasses.replace(
            consentimiento,
            approval_scope_digest=scope_falso.approval_scope_digest,
            approval_provenance=procedencia_falsa,
        )

        with pytest.raises(BinderAutorizacionMo2Error, match="no liga el digest"):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, inst_b, probes_b),
                propuesta=propuesta_falsa,
                consentimiento=consentimiento_falso,
                transition_id=solicitud.transicion_id,
            )
        ledger = cargar_ledger_aprobacion(rig.storage).ledger
        assert ledger is not None and ledger.state is ApprovalLifecycle.ISSUED
        _estado_durable_sin_mutacion(rig)

    def test_b3_5_una_aprobacion_p7_a5_sin_vinculo_no_se_consume(
        self, tmp_path: pathlib.Path, exclusion_de_test: str
    ) -> None:
        """Compatibilidad: una aprobación de política Stock SIN ``mo2auth`` (fixture del núcleo, sin binding)
        no evita el chequeo obligatorio de la operación P7-A5."""
        rig = RigStockManual(tmp_path, steam_activo=True)
        clone = cargar_runtime_clone_record(rig.storage, rig.clone_id)
        assert clone is not None
        propuesta, consentimiento = _preparar_aprobacion(
            rig.storage,
            target_generation_id=rig.generation_id,
            target_clone_id=rig.clone_id,
            runtime_setup_id=clone.verified_runtime_setup_id or "setup-skse-clean",
        )

        with pytest.raises(BinderAutorizacionMo2Error, match=_FALTA_EL_VINCULO):
            ejecutar_transicion_binder(
                **_kwargs_binder(rig, rig.mo2_active_global, rig.probes_globales_activos()),
                propuesta=propuesta,
                consentimiento=consentimiento,
            )
        ledger = cargar_ledger_aprobacion(rig.storage).ledger
        assert ledger is not None and ledger.state is ApprovalLifecycle.ISSUED
        _estado_durable_sin_mutacion(rig)


class TestExigirVinculoEnElBinder:
    """``_exigir_vinculo_mo2`` aislado (corre en cualquier SO): lo que decide por cada etapa."""

    @staticmethod
    def _vinculo(token: str | None, *, obligatorio: bool):
        return binder_module._VinculoMo2Aprobado(token=token, obligatorio=obligatorio, approval_seq=1)

    @staticmethod
    def _exigir(vinculo: object, **cambios: object) -> None:
        argumentos: dict[str, object] = {
            "mo2_dir": pathlib.Path("/mo2/instancia"),
            "identidad": IDENTIDAD_AUTORIZADA,
            "ini_sha256": _SHA_A,
            "transition_policy": BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION,
            "outgoing_manual_runtime_root": pathlib.Path("/runtime/manual"),
            "etapa": "prueba",
        }
        argumentos.update(cambios)
        binder_module._exigir_vinculo_mo2(vinculo, **argumentos)  # type: ignore[arg-type]

    def test_b3_6_un_token_presente_se_verifica_aunque_la_operacion_sea_legacy(self) -> None:
        """Legacy (no obligatorio): la AUSENCIA se tolera, un token PRESENTE y ajeno no."""
        self._exigir(self._vinculo(None, obligatorio=False))  # tolerado
        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            self._exigir(self._vinculo(_token(mo2_ini_sha256=_SHA_B), obligatorio=False))

    @pytest.mark.parametrize(
        "cambio",
        [
            {"mo2_dir": pathlib.Path("/mo2/otra")},
            {"identidad": "active_portable"},
            {"ini_sha256": _SHA_B},
            {"transition_policy": BinderTransitionPolicy.STRICT_PROVIDER_IDLE},
            {"outgoing_manual_runtime_root": None},
        ],
        ids=["instancia", "identidad", "ini", "politica", "runtime-saliente"],
    )
    def test_b3_7_cada_dimension_observada_distinta_se_rechaza_en_el_binder(self, cambio: dict[str, object]) -> None:
        """ENUMERA las cinco dimensiones del vínculo en el binder (la identidad también: el preflight la impone
        hoy, pero el compromiso de la aprobación no depende de que el preflight se mantenga igual)."""
        vinculo = self._vinculo(_token(), obligatorio=True)
        self._exigir(vinculo)  # lo aprobado coincide
        with pytest.raises(BinderAutorizacionMo2Error, match=_NO_SON_LOS_APROBADOS):
            self._exigir(vinculo, **cambio)

    def test_b3_8_obligatorio_y_ausente_se_rechaza(self) -> None:
        with pytest.raises(BinderAutorizacionMo2Error, match=_FALTA_EL_VINCULO):
            self._exigir(self._vinculo(None, obligatorio=True))
