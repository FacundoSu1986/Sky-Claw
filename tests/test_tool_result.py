"""Tests del contrato de resultado compartido de los tools (deuda #5 de CLAUDE.md).

`normalize_tool_result` es la ÚNICA pieza que conoce las claves legacy bajo las
que cada servicio reportaba errores (`logs`, `errors`, `error`, `stderr`,
`details`, `reason`). Las shapes de estos tests están copiadas de los returns
reales de cada servicio, para que el normalizador quede anclado a lo que existe.
"""

from __future__ import annotations

from sky_claw.local.tools.tool_result import normalize_tool_result


# ── Contrato canónico: message manda ────────────────────────────────────────────
def test_message_canonico_tiene_prioridad_sobre_legacy() -> None:
    out = normalize_tool_result(
        {"success": False, "message": "detalle canónico", "logs": "viejo", "error": "más viejo"}
    )
    assert out["success"] is False
    assert out["message"] == "detalle canónico"


def test_exito_por_success_booleano() -> None:
    out = normalize_tool_result({"success": True})
    assert out["success"] is True


def test_exito_por_status_string_sin_success() -> None:
    out = normalize_tool_result({"status": "success"})
    assert out["success"] is True


def test_success_booleano_manda_sobre_status() -> None:
    # Si ambos están presentes y discrepan, el booleano explícito gana.
    out = normalize_tool_result({"status": "success", "success": False, "logs": "x"})
    assert out["success"] is False


# ── Shapes legacy reales (una por servicio) ─────────────────────────────────────
def test_shape_loot_error_bajo_logs() -> None:
    # loot_service.sort_load_order — lock/LOOTNotFound/timeout.
    raw = {"status": "error", "success": False, "logs": "Could not acquire load-order lock 'load-order': busy"}
    assert normalize_tool_result(raw)["message"].startswith("Could not acquire load-order lock")


def test_shape_loot_error_con_errors_lista_y_logs_vacio() -> None:
    # loot_service: fallo del runner con errors lista y logs="" (stdout vacío).
    raw = {
        "status": "error",
        "success": False,
        "return_code": 1,
        "errors": ["cyclic rules", "missing master"],
        "logs": "",
    }
    assert normalize_tool_result(raw)["message"] == "cyclic rules; missing master"


def test_shape_pandora_error_bajo_stderr() -> None:
    # pandora_service: fallo del runner (stderr del subproceso).
    raw = {"status": "error", "success": False, "return_code": 2, "stdout": "", "stderr": "Pandora crashed"}
    assert normalize_tool_result(raw)["message"] == "Pandora crashed"


def test_shape_dyndolod_error_bajo_errors_lista() -> None:
    raw = {"success": False, "errors": ["Lock acquisition failed: busy"], "duration_seconds": 1.2}
    assert normalize_tool_result(raw)["message"] == "Lock acquisition failed: busy"


def test_shape_xedit_patch_error_bajo_error() -> None:
    # xedit_service._error_dict (execute_patch, retornos tempranos).
    raw = {"success": False, "xedit_exit_code": -1, "warnings": [], "error": "XEDIT_PATH no configurado"}
    assert normalize_tool_result(raw)["message"] == "XEDIT_PATH no configurado"


def test_shape_quick_auto_clean_error_bajo_logs() -> None:
    raw = {"status": "error", "success": False, "logs": "SKYRIM_PATH no está configurado."}
    assert normalize_tool_result(raw)["message"] == "SKYRIM_PATH no está configurado."


def test_shape_con_details_tiene_prioridad_legacy_maxima() -> None:
    raw = {"success": False, "details": "detalle fino", "error": "genérico", "logs": "crudo"}
    assert normalize_tool_result(raw)["message"] == "detalle fino"


def test_shape_solo_reason() -> None:
    raw = {"status": "error", "reason": "Boom"}
    out = normalize_tool_result(raw)
    assert out["success"] is False
    assert out["message"] == "Boom"


# ── Bordes ──────────────────────────────────────────────────────────────────────
def test_dict_vacio_cae_a_error_desconocido() -> None:
    out = normalize_tool_result({})
    assert out["success"] is False
    assert out["message"] == "error desconocido"


def test_exito_sin_message_devuelve_cadena_vacia() -> None:
    # En éxito el consumidor arma su propio copy; no hay que inventar detalle.
    assert normalize_tool_result({"success": True})["message"] == ""


def test_dry_run_preview_con_message_canonico() -> None:
    # dyndolod/xedit dry_run devuelven status="dry_run_preview" + message con el
    # summary del change_set (no es un éxito de ejecución ni un fallo opaco).
    raw = {"status": "dry_run_preview", "message": "Would generate LODs — DynDOLOD not run.", "change_set": {}}
    out = normalize_tool_result(raw)
    assert out["success"] is False  # no es un éxito de ejecución
    assert out["message"] == "Would generate LODs — DynDOLOD not run."


def test_return_code_y_warnings_se_propagan() -> None:
    raw = {"success": False, "return_code": 3, "warnings": ["w1"], "logs": "x"}
    out = normalize_tool_result(raw)
    assert out["return_code"] == 3
    assert out["warnings"] == ["w1"]


def test_valores_no_string_se_convierten() -> None:
    # Robustez: un error no-string no debe romper el toast.
    raw = {"success": False, "error": ValueError("kaput")}
    assert "kaput" in normalize_tool_result(raw)["message"]


# ── Review Copilot #222: success estricto y message vacío en éxito ──────────────
def test_success_no_booleano_no_cuenta_como_exito() -> None:
    # Un "False" serializado como string es truthy: solo un bool real vale como
    # señal de éxito; si no lo es, decide el status.
    out = normalize_tool_result({"status": "error", "success": "False", "logs": "x"})
    assert out["success"] is False


def test_success_entero_cae_al_status() -> None:
    out = normalize_tool_result({"status": "success", "success": 1})
    assert out["success"] is True  # decide status, no el truthy no-bool


def test_exito_con_message_vacio_explicito_no_cae_a_legacy() -> None:
    # Un éxito con message="" canónico no debe mostrar el stderr de warnings.
    out = normalize_tool_result({"success": True, "message": "", "stderr": "warnings del runner"})
    assert out["message"] == ""


def test_fallo_con_message_vacio_si_cae_a_legacy() -> None:
    # Asimetría deliberada: en FALLO, un message vacío no debe ocultar el
    # detalle legacy disponible (mejor un stderr real que nada).
    out = normalize_tool_result({"success": False, "message": "", "stderr": "crash real"})
    assert out["message"] == "crash real"
