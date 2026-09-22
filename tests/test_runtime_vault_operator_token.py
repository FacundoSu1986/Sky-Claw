"""Tests focales GP2-S3b-1: identidad del coordinador (C) + token del operador (D/E).

Cobertura: OTS-01..OTS-08 con adaptador fake (fases, conteo de cierres y fallos),
mutantes M-DBG (sin SeDebugPrivilege ni privilegios TCB), M-O2 (cross-account
sin proveedor -> REFUSE), M-O3 (sin TOKEN_IMPERSONATE en derechos), y anclas
AST/estructurales sobre los módulos de producción.

Incluye una clase causal Win32 (skipif) que usa el proceso propio y duplicados
reales para verificar TokenType, TOKEN_DUPLICATE efectivo y cierre exacto de
handles. NUNCA habilita privilegios ni eleva; CreateProcessWithTokenW queda
declarado como verificación GP2-T22 PARTIAL (S5+).
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import pathlib
import sys
import tokenize
from typing import Any

import pytest

from sky_claw.local.runtime_vault.coordinator_identity import (
    IMAGE_AUTHENTICITY,
    CoordinatorIdentityBindingError,
    CoordinatorIdentityDisposition,
    CoordinatorIdentityModelError,
    CoordinatorIdentityVerdict,
    CoordinatorProcessIdentity,
    ProcessIdentityProbe,
    classify_coordinator_identity,
    require_bound_coordinator_identity,
)
from sky_claw.local.runtime_vault.operator_token import (
    OPERATOR_TOKEN_REQUIRED_RIGHTS,
    TOKEN_ASSIGN_PRIMARY,
    TOKEN_DUPLICATE,
    TOKEN_QUERY,
    OpenCoordinatorProcessError,
    OpenCoordinatorTokenError,
    OperatorIdentityEvidenceError,
    OperatorPrimaryToken,
    OperatorTokenOwnershipError,
    OperatorTokenStrategy,
    OperatorTokenTypeError,
    OtsElevationCase,
    TokenDuplicationError,
    acquire_operator_primary_token_from_coordinator,
    resolve_operator_token_strategy,
)
from sky_claw.local.runtime_vault.privileged_boundary import PlanAuthorizationError

_VALID_SID_PREFIX = "S-1-5-21-"
_FAKE_HANDLES = (101, 202, 303)


class _FakeOperatorTokenAdapter:
    """Adaptador determinista: handles falsos, fallas scriptables, conteo exacto de cierres."""

    def __init__(self) -> None:
        self.closed: list[int] = []
        self.duplicate_requests: list[int] = []
        self.fail_open_process = False
        self.fail_open_token = False
        self.fail_duplicate = False
        self.fail_user_sid = False
        self.token_type_result = 1  # TokenPrimary

    def open_process(self, desired_access: int, pid: int) -> int:
        if self.fail_open_process:
            raise OpenCoordinatorProcessError("proceso no abrible")
        return _FAKE_HANDLES[0]

    def open_process_token(self, process_handle: int, desired_access: int) -> int:
        if self.fail_open_token:
            raise OpenCoordinatorTokenError("token no abrible")
        return _FAKE_HANDLES[1]

    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int:
        self.duplicate_requests.append(desired_access)
        if self.fail_duplicate:
            raise TokenDuplicationError("dup falló")
        return _FAKE_HANDLES[2]

    def get_token_type(self, token_handle: int) -> int:
        return self.token_type_result

    def read_token_user_sid(self, token_handle: int) -> str:
        if self.fail_user_sid:
            raise OperatorIdentityEvidenceError("sid ilegible")
        return f"{_VALID_SID_PREFIX}1001-1002-1003-1001"

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


def _acquire_with_fake(**scripting: Any) -> tuple[OperatorPrimaryToken, _FakeOperatorTokenAdapter]:
    adapter = _FakeOperatorTokenAdapter()
    for key, value in scripting.items():
        setattr(adapter, key, value)
    token = acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
    return token, adapter


# ============================================================================
# OTS-01: flujo feliz y contrato de derechos (M-O3 anclado por parámetro)
# ============================================================================


class TestOts01FlujoFeliz:
    def test_operador_token_evidence_y_raii(self) -> None:
        token, adapter = _acquire_with_fake()
        evidence = token.evidence
        assert evidence.operator_sid.startswith(_VALID_SID_PREFIX)
        assert evidence.token_type == "primary"
        assert evidence.acquired_via == "same_account_coordinator_extraction"
        assert adapter.duplicate_requests == [OPERATOR_TOKEN_REQUIRED_RIGHTS]
        # Cierres inmediatos de los handles intermedios (process, token fuente).
        assert adapter.closed == [_FAKE_HANDLES[1], _FAKE_HANDLES[0]]
        assert token.close() is True
        assert adapter.closed == [_FAKE_HANDLES[1], _FAKE_HANDLES[0], _FAKE_HANDLES[2]]

    def test_m_o3_derechos_requeridos_son_exactamente_query_duplicate_assign(self) -> None:
        assert TOKEN_QUERY == 0x0008
        assert TOKEN_DUPLICATE == 0x0002
        assert TOKEN_ASSIGN_PRIMARY == 0x0001
        assert OPERATOR_TOKEN_REQUIRED_RIGHTS == TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY
        assert OPERATOR_TOKEN_REQUIRED_RIGHTS & 0x0010 == 0  # TOKEN_IMPERSONATE jamás solicitado


class TestMdbgAnclasAst:
    """M-DBG: NUNCA SeDebugPrivilege ni privilegios TCB, en NINGÚN módulo de producción."""

    PRODUCTION_DIR = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "runtime_vault"

    def _module_sources(self) -> dict[str, str]:
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(self.PRODUCTION_DIR.glob("*.py"))}

    def test_nunguna_fuente_menciona_privilegios_prohibidos(self) -> None:
        forbidden = ("SeDebugPrivilege", "SeTcbPrivilege", "SeImpersonatePrivilege")
        for name, source in self._module_sources().items():
            for literal in forbidden:
                assert literal not in source, f"{name} menciona literal prohibido: {literal}"

    def test_ninguna_api_de_ajuste_de_privilegios_en_el_codigo(self) -> None:
        # NUNCA se ajustan privilegios de token en el código de ESTE slice.
        forbidden_apis = ("AdjustTokenPrivileges", "LookupPrivilegeValueW", "NtAdjustPrivilegesToken")
        slice_modules = ("operator_token.py", "authorization_context.py", "ppsc.py", "privileged_boundary.py")
        for name in slice_modules:
            source = self._module_sources()[name]
            for token_info in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
                if token_info.type == tokenize.NAME:
                    assert token_info.string not in forbidden_apis, f"{name} usa API prohibida {token_info.string}"

    def test_sin_wts_en_la_adquisicion_v1(self) -> None:
        source = self._module_sources()["operator_token.py"]
        names = {
            tok.string
            for tok in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
            if tok.type == tokenize.NAME
        }
        assert "WTSQueryUserToken" not in names
        assert "CreateProcessWithTokenW" not in names


# ============================================================================
# OTS-02: DuplicateTokenEx como TokenPrimary con derechos requeridos (fake)
# ============================================================================


class TestOts02Duplicado:
    def test_dup_falla_propaga_y_cierra_todas_las_rams(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.fail_duplicate = True
        with pytest.raises(TokenDuplicationError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        # process + token fuente cerrados; sin handle duplicado que limpiar.
        assert sorted(adapter.closed) == sorted(_FAKE_HANDLES[:2])


# ============================================================================
# OTS-03: token no primario -> rechazo estricto, handle cerrado
# ============================================================================


class TestOts03TokenNoPrimario:
    def test_token_impersonation_rechazado_y_cerrado_exactamente_una_vez(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.token_type_result = 2  # TokenImpersonation
        with pytest.raises(OperatorTokenTypeError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        # Los tres handles abiertos hasta el fallo quedan cerrados, exactamente una vez.
        assert sorted(adapter.closed) == sorted(_FAKE_HANDLES)

    def test_token_type_desconocido_rechazado(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.token_type_result = 99
        with pytest.raises(OperatorTokenTypeError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        assert sorted(adapter.closed) == sorted(_FAKE_HANDLES)


# ============================================================================
# OTS-04: fallas de apertura => fail-closed + cierre parcial exacto
# ============================================================================


class TestOts04FallasApertura:
    def test_open_process_falla_sin_nada_que_cerrar(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.fail_open_process = True
        with pytest.raises(OpenCoordinatorProcessError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        assert adapter.closed == []
        assert isinstance(OpenCoordinatorProcessError("x"), PlanAuthorizationError)

    def test_open_token_falla_cierra_solo_process(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.fail_open_token = True
        with pytest.raises(OpenCoordinatorTokenError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        assert adapter.closed == [_FAKE_HANDLES[0]]

    def test_sid_ilegible_rechaza_y_cierra_todo(self) -> None:
        adapter = _FakeOperatorTokenAdapter()
        adapter.fail_user_sid = True
        with pytest.raises(OperatorIdentityEvidenceError):
            acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
        assert sorted(adapter.closed) == sorted(_FAKE_HANDLES)


# ============================================================================
# OTS-05/OTS-08: ownership del handle (mint-proof, close-once, use-after-close)
# ============================================================================


class TestOts05TipoEstricto:
    def test_operador_primary_token_creado_protegido(self) -> None:
        token, _ = _acquire_with_fake()
        with pytest.raises(OperatorTokenOwnershipError):
            OperatorPrimaryToken(123, token.evidence, closer=lambda h: None)
        with pytest.raises(OperatorTokenOwnershipError):
            OperatorPrimaryToken(0, token.evidence, closer=lambda h: None, _proof=object())
        token.close()

    def test_cierre_exactamente_una_vez_archivado_por_flag(self) -> None:
        closes: list[int] = []
        token, _ = _acquire_with_fake()
        token_handle = token.raw_handle
        # Reemplazamos el closer para contar (el adapter ya hizo sus cierres internos).
        object.__setattr__(token, "_closer", closes.append)
        assert token.close() is True
        assert token.close() is False
        assert closes == [token_handle]

    def test_use_after_close_rechazado(self) -> None:
        token, _ = _acquire_with_fake()
        evidence = token.evidence
        token.close()
        assert token.closed is True
        with pytest.raises(OperatorTokenOwnershipError):
            _ = token.raw_handle
        # La identidad ya extraída queda como evidencia (no reabre el handle).
        assert evidence.operator_sid.startswith(_VALID_SID_PREFIX)


# ============================================================================
# OTS (C): identidad del coordinador — PID + creation-time + imagen
# ============================================================================


class _FakeProbeProvider:
    def __init__(self, probe_result: ProcessIdentityProbe) -> None:
        self._probe_result = probe_result
        self.calls = 0

    def probe(self, pid: int) -> ProcessIdentityProbe:
        self.calls += 1
        return self._probe_result


def _expected_identity(**overrides: Any) -> CoordinatorProcessIdentity:
    kwargs: dict[str, Any] = {
        "pid": 4242,
        "creation_time": 133_456_789_012_345_678,
        "image_path": "C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    }
    kwargs.update(overrides)
    return CoordinatorProcessIdentity(**kwargs)


class TestCoordinatorIdentity:
    def test_campos_exactos_de_identidad(self) -> None:
        fields = tuple(field.name for field in dataclasses.fields(CoordinatorProcessIdentity))
        assert fields == ("pid", "creation_time", "image_path")

    def test_ots05_imagen_no_empaquetada_es_field_obligatorio(self) -> None:
        with pytest.raises(CoordinatorIdentityModelError):
            CoordinatorProcessIdentity(pid=1, creation_time=1, image_path="")
        assert IMAGE_AUTHENTICITY == "PATH_BINDING"

    @pytest.mark.parametrize(
        "bad_path",
        ["\\\\server\\share\\x.exe", "x.exe", "C:x.exe", "/tmp/x"],
    )
    def test_imagen_fuera_de_contrato_estructural_rechazada(self, bad_path: str) -> None:
        with pytest.raises(CoordinatorIdentityModelError):
            _expected_identity(image_path=bad_path)

    def test_imagen_temp_aceptada_por_contrato_declarado(self) -> None:
        # No existe lista blanqueada normativa de directorios en el ADR v1:
        # el contrato exige ruta absoluta local (PATH_BINDING); las carpetas
        # candidatas concretas se endurecen con el contrato de packaging (S4+).
        assert _expected_identity(image_path="C:\\Windows\\Temp\\x.exe").image_path.endswith("x.exe")

    def test_bind_exitoso(self) -> None:
        expected = _expected_identity()
        probe = ProcessIdentityProbe(
            pid=expected.pid,
            is_openable=True,
            creation_time=expected.creation_time,
            image_path=expected.image_path,
        )
        verdict = classify_coordinator_identity(expected, probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.BOUND
        assert verdict.bound is True
        required = require_bound_coordinator_identity(expected, _FakeProbeProvider(probe))
        assert required == expected

    def test_ots04_pid_mismatch(self) -> None:
        probe = ProcessIdentityProbe(
            pid=7777,
            is_openable=True,
            creation_time=133_456_789_012_345_678,
            image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_PID_MISMATCH
        with pytest.raises(CoordinatorIdentityBindingError):
            require_bound_coordinator_identity(_expected_identity(), _FakeProbeProvider(probe))

    def test_ots05_imagen_mismatch(self) -> None:
        probe = ProcessIdentityProbe(
            pid=4242,
            is_openable=True,
            creation_time=133_456_789_012_345_678,
            image_path="C:\\Windows\\System32\\cmd.exe",
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_IMAGE_MISMATCH

    def test_imagen_comparacion_case_insensitive_y_normalizada(self) -> None:
        expected = _expected_identity()
        probe = ProcessIdentityProbe(
            pid=expected.pid,
            is_openable=True,
            creation_time=expected.creation_time,
            image_path="c:\\program files\\sky-claw\\SKY-CLAW.EXE",
        )
        assert classify_coordinator_identity(expected, probe).disposition is CoordinatorIdentityDisposition.BOUND

    def test_pid_muerto_o_no_abrible_refuse(self) -> None:
        probe = ProcessIdentityProbe(
            pid=4242,
            is_openable=False,
            creation_time=None,
            image_path=None,
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_PROCESS_MISSING

    def test_creation_time_mismatch_refuse(self) -> None:
        probe = ProcessIdentityProbe(
            pid=4242,
            is_openable=True,
            creation_time=111,
            image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_CREATION_TIME_MISMATCH

    def test_verdict_estructuralmente_excluyente(self) -> None:
        with pytest.raises(CoordinatorIdentityModelError):
            CoordinatorIdentityVerdict(
                disposition=CoordinatorIdentityDisposition.BOUND,
                reason="contradicción",
            )
        with pytest.raises(CoordinatorIdentityModelError):
            CoordinatorIdentityVerdict(
                disposition=CoordinatorIdentityDisposition.REFUSE_IMAGE_MISMATCH,
                bound_identity=_expected_identity(),
            )

    def test_m_pid_sin_canal_doble_no_hay_bind(self) -> None:
        # Sin creation_time del probe: jamás BOUND, jamás default.
        probe = ProcessIdentityProbe(
            pid=4242,
            is_openable=True,
            creation_time=None,
            image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_CREATION_TIME_UNREADABLE
        assert not verdict.bound

    def test_imagen_ilegible_refuse(self) -> None:
        probe = ProcessIdentityProbe(
            pid=4242,
            is_openable=True,
            creation_time=133_456_789_012_345_678,
            image_path=None,
        )
        verdict = classify_coordinator_identity(_expected_identity(), probe)
        assert verdict.disposition is CoordinatorIdentityDisposition.REFUSE_IMAGE_UNREADABLE


# ============================================================================
# Estrategia OTS: cross-account sin proveedor legítimo => REFUSE (M-O2)
# ============================================================================


class TestOtsStrategy:
    def test_m_o2_cross_account_sin_proveedor_refuse(self) -> None:
        assert (
            resolve_operator_token_strategy(OtsElevationCase.CROSS_ACCOUNT, None)
            is OperatorTokenStrategy.REFUSE_TO_PLAN
        )

    def test_same_account_siempre_extraccion(self) -> None:
        assert (
            resolve_operator_token_strategy(OtsElevationCase.SAME_ACCOUNT, None)
            is OperatorTokenStrategy.SAME_ACCOUNT_EXTRACTION
        )

    def test_cross_account_con_proveedor_declara_canal_v2(self) -> None:
        class _Provider:
            def acquire_interactive_operator_token(self) -> OperatorPrimaryToken:  # pragma: no cover
                raise NotImplementedError

        assert (
            resolve_operator_token_strategy(OtsElevationCase.CROSS_ACCOUNT, _Provider())
            is OperatorTokenStrategy.SERVICE_WTS_PROVIDER
        )

    def test_caso_invalido_rechazado(self) -> None:
        from sky_claw.local.runtime_vault.operator_token import OperatorTokenError

        with pytest.raises(OperatorTokenError):
            resolve_operator_token_strategy("cross", None)  # type: ignore[arg-type]

    def test_ancla_sin_parametros_de_bypass(self) -> None:
        params = inspect.signature(acquire_operator_primary_token_from_coordinator).parameters
        assert "case" not in params and "bypass" not in params
        # La adquisición v1 no expone canal WTS: se resuelve solo para el coordinador.
        assert set(params) == {"coordinator_pid", "adapter"}


# ============================================================================
# Causal Win32: proceso propio, duplicados reales y vida del handle
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas causales de tokens Win32")
class TestWin32CausalOperatorToken:
    """OTS-01/02/03/08 causales con el propio proceso. Sin privilegios habilitados."""

    def test_adquirir_desde_proceso_propio(self) -> None:
        import ctypes
        import ctypes.wintypes  # noqa: F401

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        token = acquire_operator_primary_token_from_coordinator(os.getpid())
        evidence = token.evidence
        assert evidence.operator_sid.startswith("S-1-")
        assert evidence.token_type == "primary"

        # TOKEN_DUPLICATE real: un segundo DuplicateTokenEx sobre el handle adquirido funciona.
        handle = token.raw_handle
        duplicated = ctypes.wintypes.HANDLE()
        ok = advapi32.DuplicateTokenEx(
            ctypes.c_void_p(handle),
            0x0008,  # TOKEN_QUERY
            None,
            2,  # SecurityImpersonation
            2,  # TokenImpersonation
            ctypes.byref(duplicated),
        )
        assert ok, "TOKEN_DUPLICATE no efectivo en el handle adquirido"
        kernel32.CloseHandle(ctypes.c_void_p(duplicated.value))
        assert token.close() is True
        # Tras el cierre, el handle ya no es utilizable (GetTokenInformation falla tipado).
        length = ctypes.c_ulong(0)
        ok = advapi32.GetTokenInformation(
            ctypes.c_void_p(handle),
            8,
            None,
            0,
            ctypes.byref(length),  # TokenType
        )
        assert not ok, "El handle cerrado siguió siendo utilizable: ownership roto"

    def test_sin_credencial_interactiva_el_caso_cross_account_es_declarado(self) -> None:
        # GP2-T22 PARTIAL: CreateProcessWithTokenW y el enrolamiento cross-account se
        # verifican en S5+ con helper real y credenciales de prueba; este slice ancla
        # que ningún camino v1 habilita ese flujo silenciosamente.
        assert resolve_operator_token_strategy(OtsElevationCase.CROSS_ACCOUNT, None) is (
            OperatorTokenStrategy.REFUSE_TO_PLAN
        )
