"""Tests focales GP2-S3b-1: frontera del helper privilegiado + elevación UAC (componentes A y B).

Cobertura: PB-01..PB-07, mutantes M-CD1/M-CD2, ancla M-A1 (UAC != PPSC),
ancla de estados EARLY FSM, y anclas AST/estructurales del contrato CLI.
Los modelos puros corren en POSIX; la orquestación del launcher con seams
inyectados se verifica en Windows (donde existe la plataforma) con fakes que
jamás tocan UAC real.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import sys
import uuid
from typing import Any

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import GoldenProtectionPlanState
from sky_claw.local.runtime_vault.privileged_boundary import (
    HELPER_CLI_ALLOWED_FLAGS,
    PACKAGED_HELPER_IMAGE_NAME,
    PACKAGED_HELPER_PROVISIONING_STATUS,
    ElevatedHelperHandleError,
    ElevatedHelperProcessHandle,
    ElevationLaunchError,
    ElevationRejectedError,
    HelperArgumentError,
    HelperImageNotProvisionedError,
    HelperImageValidationError,
    PrivilegedBoundaryUnsupportedError,
    PrivilegedHelperLaunchRequest,
    build_helper_arguments,
    early_plan_state_for_launch_failure,
    launch_privileged_helper,
    resolve_packaged_helper_image,
    validate_helper_cli_arguments,
    validate_helper_image_candidate,
)

_VALID_UUID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_VALID_DIGEST = "a" * 64
_FORBIDDEN_PATH_FLAGS = (
    "--root",
    "--golden-path",
    "--manifest-path",
    "--tgr-path",
    "--operations-path",
    "--lock-path",
    "--helper-path",
)
_FORBIDDEN_EXEC_FLAGS = ("--script", "--command", "--acl", "--policy-json")


def _make_request(**overrides: Any) -> PrivilegedHelperLaunchRequest:
    kwargs: dict[str, Any] = {
        "operation_id": _VALID_UUID,
        "staging_digest": _VALID_DIGEST,
        "coordinator_pid": 4242,
        "coordinator_creation_time": 133_456_789_012_345_678,
    }
    kwargs.update(overrides)
    return PrivilegedHelperLaunchRequest(**kwargs)


# ============================================================================
# PB-01: gramática exacta de dos flags
# ============================================================================


class TestPb01HelperCliGrammar:
    def test_acepta_orden_canonico(self) -> None:
        args = validate_helper_cli_arguments(["--operation-id", _VALID_UUID, "--staging-digest", _VALID_DIGEST])
        assert args.operation_id_str == _VALID_UUID
        assert args.staging_digest == _VALID_DIGEST

    def test_acepta_orden_invertido_de_flags(self) -> None:
        args = validate_helper_cli_arguments(["--staging-digest", _VALID_DIGEST, "--operation-id", _VALID_UUID])
        assert args.operation_id_str == _VALID_UUID
        assert args.staging_digest == _VALID_DIGEST

    def test_conjunto_de_flags_es_exactamente_dos(self) -> None:
        assert frozenset({"--operation-id", "--staging-digest"}) == HELPER_CLI_ALLOWED_FLAGS

    def test_ancla_adr_12_1_identidad_del_coordinador_nunca_cruza_por_cli(self) -> None:
        # Ancla normativa (finding de review RECHAZADO por contradicción con
        # ADR 0010 §12.1): jamás existirán --coordinator-pid ni
        # --coordinator-creation-time. La identidad del coordinador se revalida
        # en el componente C (coordinator_identity) con PID + ProcessCreationTime
        # + imagen empaquetada a partir de evidencia ligada al staging_digest;
        # nunca se confía en datos staged sin verificar digest/binding.
        for forbidden_flag in ("--coordinator-pid", "--coordinator-creation-time"):
            with pytest.raises(HelperArgumentError):
                validate_helper_cli_arguments(
                    ["--operation-id", _VALID_UUID, "--staging-digest", _VALID_DIGEST, forbidden_flag, "4242"]
                )
        assert "--coordinator-pid" not in HELPER_CLI_ALLOWED_FLAGS
        assert "--coordinator-creation-time" not in HELPER_CLI_ALLOWED_FLAGS

    @pytest.mark.parametrize("flag", [*_FORBIDDEN_PATH_FLAGS, *_FORBIDDEN_EXEC_FLAGS])
    def test_pb_02_flag_prohibido_rechazado(self, flag: str) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments([flag, "C:\\cualquier\\cosa", "--staging-digest", _VALID_DIGEST])

    def test_pb_02_flag_desconocido_rechazado(self) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["--no-existo", "x", "--operation-id", _VALID_UUID])

    def test_forma_flag_igual_valor_rechazada(self) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments([f"--operation-id={_VALID_UUID}", "--staging-digest", _VALID_DIGEST, "extra"])

    def test_flag_duplicado_rechazado(self) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["--operation-id", _VALID_UUID, "--operation-id", _VALID_UUID])

    def test_valor_ausente_rechazado(self) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["--operation-id", "--staging-digest", _VALID_DIGEST, "x"])

    @pytest.mark.parametrize("count", [0, 1, 2, 3, 5, 6])
    def test_conteo_de_tokens_distinto_de_cuatro_rechazado(self, count: int) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["x"] * count)

    def test_argumento_posicional_rechazado(self) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["C:\\ruta", "x", "--operation-id", _VALID_UUID])


# ============================================================================
# PB-03 / PB-04: validación de UUID y digest
# ============================================================================


class TestPb03Pb04Validaciones:
    @pytest.mark.parametrize(
        "bad_uuid",
        [
            "no-es-un-uuid",
            "3F6B0BE2-1C2A-4D3E-8F4A-9B7C6D5E4F3A",  # mayúsculas: no canónico
            "{3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a}",  # llaves: no canónico
            "3f6b0be21c2a4d3e8f4a9b7c6d5e4f3a",  # sin guiones: no canónico
            "",
            "123",
        ],
    )
    def test_pb_03_operation_id_invalido_rechazado(self, bad_uuid: str) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["--operation-id", bad_uuid, "--staging-digest", _VALID_DIGEST])
        with pytest.raises(HelperArgumentError):
            _make_request(operation_id=bad_uuid)

    @pytest.mark.parametrize(
        "bad_digest",
        [
            "abc",  # corto
            "a" * 63,
            "a" * 65,
            "g" * 64,  # no hex
            "a" * 32 + " " + "a" * 33,
            "",
        ],
    )
    def test_pb_04_digest_invalido_rechazado(self, bad_digest: str) -> None:
        with pytest.raises(HelperArgumentError):
            validate_helper_cli_arguments(["--operation-id", _VALID_UUID, "--staging-digest", bad_digest])
        with pytest.raises(HelperArgumentError):
            _make_request(staging_digest=bad_digest)

    def test_digest_se_normaliza_a_minusculas(self) -> None:
        request = _make_request(staging_digest="A" * 64)
        assert request.staging_digest == "a" * 64


# ============================================================================
# Launch request: modelo inmutable sin paths mutables
# ============================================================================


class TestLaunchRequestModel:
    def test_campos_exactos_sin_paths_ni_ejecutable(self) -> None:
        fields = tuple(field.name for field in dataclasses.fields(PrivilegedHelperLaunchRequest))
        assert fields == ("operation_id", "staging_digest", "coordinator_pid", "coordinator_creation_time")

    def test_operation_id_se_normaliza_a_uuid(self) -> None:
        request = _make_request()
        assert isinstance(request.operation_id, uuid.UUID)
        assert request.operation_id_str == _VALID_UUID

    @pytest.mark.parametrize("bad_pid", [0, -1, True, 1.5, "42", 2**32])
    def test_coordinator_pid_fuera_de_rango_rechazado(self, bad_pid: object) -> None:
        with pytest.raises(HelperArgumentError):
            _make_request(coordinator_pid=bad_pid)

    @pytest.mark.parametrize("bad_time", [0, -5, False, 2.5, "133", 2**64])
    def test_coordinator_creation_time_fuera_de_rango_rechazado(self, bad_time: object) -> None:
        with pytest.raises(HelperArgumentError):
            _make_request(coordinator_creation_time=bad_time)


# ============================================================================
# Builder de command line: serialización única anclada
# ============================================================================


class TestArgumentBuilder:
    def test_serializacion_exacta_anclada(self) -> None:
        expected = f"--operation-id {_VALID_UUID} --staging-digest {_VALID_DIGEST}"
        assert build_helper_arguments(_make_request()) == expected

    def test_nunca_incluye_flags_adicionales(self) -> None:
        args = build_helper_arguments(_make_request())
        tokens = args.split(" ")
        assert len(tokens) == 4
        assert tokens[0] == "--operation-id"
        assert tokens[2] == "--staging-digest"

    def test_rechaza_objetos_que_no_son_request(self) -> None:
        with pytest.raises(HelperArgumentError):
            build_helper_arguments(object())  # type: ignore[arg-type]


# ============================================================================
# Imagen de helper: contrato de resolución (packaging UNRESOLVED)
# ============================================================================


class TestHelperImageContract:
    def test_estado_de_packaging_declarado_unresolved(self) -> None:
        assert PACKAGED_HELPER_PROVISIONING_STATUS == "UNRESOLVED"

    @pytest.mark.skipif(sys.platform != "win32", reason="La resolución productiva exige Windows")
    def test_resolver_productivo_falla_cerrado(self) -> None:
        with pytest.raises(HelperImageNotProvisionedError):
            resolve_packaged_helper_image()

    @pytest.mark.skipif(sys.platform == "win32", reason="En Windows el resolver exige plataforma antes de fallar")
    def test_resolver_productivo_en_posix_es_no_soportado_antes_de_packaging(self) -> None:
        with pytest.raises(PrivilegedBoundaryUnsupportedError):
            resolve_packaged_helper_image()

    @pytest.mark.parametrize(
        "bad_name",
        [
            "helper.exe",
            "sky-claw-vault-helper-beta.exe",
            "Sky-Claw-Vault-Helper.exe",  # case-sensitive anti-confusión
            "sky-claw-vault-helper.exe.exe",
        ],
    )
    def test_nombre_de_imagen_no_pineado_rechazado(self, bad_name: str) -> None:
        with pytest.raises(HelperImageValidationError):
            validate_helper_image_candidate(f"C:\\Sky-Claw\\{bad_name}")

    def test_ruta_relativa_rechazada(self) -> None:
        with pytest.raises(HelperImageValidationError):
            validate_helper_image_candidate(f"helpers\\{PACKAGED_HELPER_IMAGE_NAME}")

    def test_ruta_vacia_rechazada(self) -> None:
        with pytest.raises(HelperImageValidationError):
            validate_helper_image_candidate("   ")

    def test_forma_estructural_valida_en_posix(self) -> None:
        # Validación estructural (POSIX): nombre pineado + absoluta con volumen.
        if sys.platform == "win32":
            pytest.skip("Caso POSIX-estructural; en Windows se verifica causalmente")
        validated = validate_helper_image_candidate(f"C:\\Sky-Claw\\bin\\{PACKAGED_HELPER_IMAGE_NAME}")
        assert pathlib.PureWindowsPath(str(validated)).name == PACKAGED_HELPER_IMAGE_NAME


class TestCd2AnclasAst:
    """M-CD2: el boundary no puede aceptar ejecutable/path arbitrario como fuente de verdad."""

    PRODUCTION_MODULE = (
        pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "runtime_vault" / "privileged_boundary.py"
    )

    def _source(self) -> str:
        return self.PRODUCTION_MODULE.read_text(encoding="utf-8")

    def test_sin_fuentes_de_ejecutable_prohibidas_en_produccion(self) -> None:
        # AST sobre el CÓDIGO (no docstrings): la docstring normativa del módulo
        # nombra las fuentes prohibidas para prohibirlas; el código jamás las usa.
        import ast
        import tokenize

        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                head = node.body[0] if node.body else None
                if (
                    isinstance(head, ast.Expr)
                    and isinstance(head.value, ast.Constant)
                    and isinstance(head.value.value, str)
                ):
                    head.value.value = ""  # docstring no es código ejecutable
        code_strings = [
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not any("python -m" in s or "pythonw" in s for s in code_strings)
        names = {tok.string for tok in tokenize.generate_tokens(iter([source]).__next__)}
        assert "tempfile" not in names, "El código del boundary no puede usar tempfile como fuente de imagen"

    def test_launcher_no_acepta_ejecutable_ni_argumentos_por_parametro(self) -> None:
        params = tuple(inspect.signature(launch_privileged_helper).parameters)
        assert params == ("request", "image_resolver", "shell_runner")
        request_fields = tuple(field.name for field in dataclasses.fields(PrivilegedHelperLaunchRequest))
        assert "executable" not in request_fields
        assert "arguments" not in request_fields
        assert "path" not in request_fields


# ============================================================================
# PB-05 / PB-06 / PB-07 y ownership del handle del helper
# ============================================================================


@pytest.mark.skipif(sys.platform == "win32", reason="PB-05 verifica el resultado tipado en POSIX")
class TestPb05PosixUnsupported:
    def test_posix_launch_es_unsupported_sin_efectos_laterales(self) -> None:
        calls: list[str] = []

        def resolver() -> str:
            calls.append("resolver")
            return f"C:\\x\\{PACKAGED_HELPER_IMAGE_NAME}"

        def runner(*, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int:
            calls.append("runner")
            return 1234

        with pytest.raises(PrivilegedBoundaryUnsupportedError):
            launch_privileged_helper(_make_request(), image_resolver=resolver, shell_runner=runner)
        assert calls == [], "En POSIX no se toca ni el resolver ni el runner (cero efectos privilegiados)"


class TestElevatedHelperProcessHandleOwnership:
    def test_handle_invalido_rechazado(self) -> None:
        with pytest.raises(ElevatedHelperHandleError):
            ElevatedHelperProcessHandle(0, image_path=pathlib.Path("C:/x.exe"), operation_id=uuid.uuid4())

    def test_close_exactamente_una_vez_y_use_after_close(self) -> None:
        handle = ElevatedHelperProcessHandle(4242, image_path=pathlib.Path("C:/x.exe"), operation_id=uuid.uuid4())
        assert handle.raw_handle == 4242
        assert handle.close() is True
        assert handle.close() is False  # segunda vez: no-op reportado
        assert handle.closed is True
        with pytest.raises(ElevatedHelperHandleError):
            _ = handle.raw_handle

    def test_context_manager_cierra_una_vez(self) -> None:
        with ElevatedHelperProcessHandle(99, image_path=pathlib.Path("C:/x.exe"), operation_id=uuid.uuid4()) as handle:
            assert handle.closed is False
        assert handle.closed is True
        assert handle.close() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Orquestación del launcher con seams (plataforma Windows)")
class TestLauncherOrchestrationConSeams:
    """PB-06/PB-07 sobre el contrato del adaptador: fakes estructurales, NUNCA UAC real."""

    def _resolver(self) -> str:
        return r"C:\Windows\Temp\sky-claw-vault-helper.exe"

    def test_runner_recibe_imagen_validada_y_parametros_pineados(self, tmp_path: pathlib.Path) -> None:
        image = tmp_path / PACKAGED_HELPER_IMAGE_NAME
        image.write_bytes(b"MZ")
        recorded: dict[str, Any] = {}

        def runner(*, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int:
            recorded["lp_file"] = lp_file
            recorded["lp_parameters"] = lp_parameters
            recorded["lp_directory"] = lp_directory
            return 5150

        request = _make_request()
        with launch_privileged_helper(request, image_resolver=lambda: str(image), shell_runner=runner) as helper:
            assert str(helper.operation_id) == _VALID_UUID
        assert recorded["lp_file"] == str(image)
        assert recorded["lp_parameters"] == (f"--operation-id {_VALID_UUID} --staging-digest {_VALID_DIGEST}")
        assert recorded["lp_directory"] is None

    def test_pb_06_uac_cancel_propagado_tipado(self, tmp_path: pathlib.Path) -> None:
        # La imagen se valida ANTES de invocar al runner: una ruta inexistente
        # fallaría en validación, no en elevación. Se crea una imagen de helper
        # desechable real (nombre pineado) bajo tmp_path y el RECHAZO UAC lo
        # produce el runner en sí.
        image = tmp_path / PACKAGED_HELPER_IMAGE_NAME
        image.write_bytes(b"MZ")

        def runner(*, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int:
            raise ElevationRejectedError("cancelado")

        with pytest.raises(ElevationRejectedError):
            launch_privileged_helper(_make_request(), image_resolver=lambda: str(image), shell_runner=runner)

    def test_imagen_no_valida_nunca_llega_al_runner(self) -> None:
        calls: list[str] = []

        def runner(*, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int:
            calls.append("runner")
            return 1

        with pytest.raises(HelperImageValidationError):
            launch_privileged_helper(
                _make_request(),
                image_resolver=lambda: r"C:\evil\helper.exe",  # nombre no pineado
                shell_runner=runner,
            )
        assert calls == []


class TestEarlyFsmMapping:
    """PB-06: UAC cancel -> ELEVATION_REJECTED; el coordinador nunca fabrica estados privilegiados."""

    def test_early_fsm_contiene_exactamente_los_estados_normativos(self) -> None:
        assert set(GoldenProtectionPlanState) == {
            GoldenProtectionPlanState.PREPARING,
            GoldenProtectionPlanState.PREPARED,
            GoldenProtectionPlanState.AWAITING_ELEVATION,
            GoldenProtectionPlanState.CANCELLED,
            GoldenProtectionPlanState.ELEVATION_REJECTED,
        }

    def test_no_existen_estados_privilegiados_inventados(self) -> None:
        valores = {state.value for state in GoldenProtectionPlanState}
        for inventado in ("elevated_ok", "token_ready", "locked", "applying", "mutating", "committed"):
            assert inventado not in valores

    def test_pb_06_cancelacion_uac_mapea_elevation_rejected(self) -> None:
        assert early_plan_state_for_launch_failure(ElevationRejectedError("cancel")) is (
            GoldenProtectionPlanState.ELEVATION_REJECTED
        )

    def test_packaging_unresolved_mapea_cancelled(self) -> None:
        assert early_plan_state_for_launch_failure(HelperImageNotProvisionedError("unresolved")) is (
            GoldenProtectionPlanState.CANCELLED
        )

    def test_otros_fallos_de_lanzamiento_mapean_cancelled(self) -> None:
        assert early_plan_state_for_launch_failure(ElevationLaunchError("win32")) is (
            GoldenProtectionPlanState.CANCELLED
        )
