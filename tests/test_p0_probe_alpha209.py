"""Tests de las sondas P0 (investigación #661) — propiedades de seguridad y contrato.

Las sondas viven en `docs/validation/2026-10-02_p0_uia_alpha209/probe/` y NO son
runtime productivo. Estos tests anclan exactamente las propiedades que el modo
P0 prometió y que un futuro cambio del probe podría romper en silencio:

- redacción (sanitización) de paths personales, recursiva;
- serialización de la evidencia (ledger/fingerprints) a JSON;
- estabilidad del fingerprint de modales (botones de scroll no lo rompen);
- ledger exactly-once: una acción intentada no se re-invoca;
- P0-A: prohibición de primitivas mutantes (WM_SETTEXT/BM_CLICK/mouse/teclado);
- P0-B: TODO Invoke vive dentro de `invocar_una_vez`, la familia de acciones
  está enumerada por igualdad literal, y los modales iniciales sólo se
  resuelven con intervención HUMANA (`automation_policy=NOT_AUTHORIZED`).

No requieren Windows ni COM: importan los módulos y ejercitan sus funciones
puras o escanean su fuente. La fuente se lee como texto/AST, igual que las
anclas del repo para superficies sensibles.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
PROBE_DIR = RAIZ / "docs" / "validation" / "2026-10-02_p0_uia_alpha209" / "probe"
P0A_SRC = PROBE_DIR / "p0a_probe.py"
P0B_SRC = PROBE_DIR / "p0b_probe.py"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import p0a_probe  # noqa: E402
import p0b_probe  # noqa: E402

# ---------------------------------------------------------------------------
# serialización / redacción
# ---------------------------------------------------------------------------


def test_sanitizar_redacta_username_en_strings(monkeypatch):
    """El username local no debe llegar jamás a la evidencia publicada."""
    monkeypatch.setattr(p0a_probe, "_usuario", "OperadorX")
    assert p0a_probe.sanitizar("C:\\Users\\OperadorX\\Temp\\x") == "<USER>\\Temp\\x"
    assert p0a_probe.sanitizar("\\OperadorX\\algo") == "\\<USER>\\algo"


def test_sanitizar_es_recursivo_y_no_toca_no_strings(monkeypatch):
    monkeypatch.setattr(p0a_probe, "_usuario", "OperadorX")
    dato = {
        "path": "C:\\Users\\OperadorX\\a",
        "n": 7,
        "lista": ["\\OperadorX\\b", 3.5, None],
        "anidado": {"otro": "C:\\Users\\OperadorX\\c"},
    }
    saneado = p0a_probe.sanitizar(dato)
    assert saneado["path"] == "<USER>\\a"
    assert saneado["lista"][0] == "\\<USER>\\b"
    assert saneado["anidado"]["otro"] == "<USER>\\c"
    assert saneado["n"] == 7
    assert saneado["lista"][1] == 3.5
    assert saneado["lista"][2] is None


def test_sanitizar_sin_username_configurado_no_rompe(monkeypatch):
    monkeypatch.setattr(p0a_probe, "_usuario", "")
    assert p0a_probe.sanitizar("C:\\Users\\alguien\\x") == "C:\\Users\\alguien\\x"


def test_evidencia_serializa_a_json_estable():
    """Un registro de ledger con Path/bytes debe serializar con default=str."""
    entrada = {
        "action_id": "texgen_start",
        "pid": 1234,
        "fingerprint": {"runtime_id": [42, 1050446], "name": "Start"},
        "t": pathlib.Path("C:\\x"),
        "raw": b"\x00\x01",
    }
    texto = json.dumps(p0a_probe.sanitizar(entrada), default=str, sort_keys=True)
    vuelta = json.loads(texto)
    assert vuelta["action_id"] == "texgen_start"
    assert vuelta["fingerprint"]["runtime_id"] == [42, 1050446]
    assert isinstance(vuelta["t"], str) and isinstance(vuelta["raw"], str)


# ---------------------------------------------------------------------------
# fingerprints de modales
# ---------------------------------------------------------------------------


def _modal_observado(name: str, instruccion: str, botones: list[tuple[str, str]]) -> dict:
    """Formato del fingerprint (P0): botones como dicts con automation_id."""
    return {
        "class_name": "#32770",
        "name": name,
        "instruccion": instruccion,
        "botones": [{"name": n, "automation_id": a} for n, a in botones],
    }


def _modal_esperado(name: str, botones: list[tuple[str, str]]) -> dict:
    """Formato del contrato esperado (P0-B): pares (name, automation_id)."""
    return {"name": name, "buttons": botones}


def test_fingerprint_de_modal_es_estable_entre_llamadas():
    entrada = {
        "class_name": "#32770",
        "name": "TexGen",
        "instruccion": "Found stitched object LOD textures",
        "texto_visible": ["Found stitched object LOD textures", "mas texto"],
        "botones": [
            {"name": "Ignore", "automation_id": "CommandButton_5"},
            {"name": "Exit TexGen", "automation_id": "CommandButton_3"},
        ],
    }
    uno = p0a_probe.fingerprint_de_modal(entrada)
    dos = p0a_probe.fingerprint_de_modal(entrada)
    assert uno == dos
    assert p0a_probe.modales_equivalentes(uno, dos)


def test_botones_de_scroll_no_rompen_la_equivalencia_del_modal():
    """El diálogo real trae botones de scroll; la identidad son sus botones funcionales."""
    esperado = _modal_esperado(
        "TexGen",
        [("Ignore", "CommandButton_5"), ("Exit TexGen", "CommandButton_3")],
    )
    observado = _modal_observado(
        "TexGen",
        "Found stitched object LOD textures",
        [
            ("Ignore", "CommandButton_5"),
            ("Exit TexGen", "CommandButton_3"),
            ("Cerrar", ""),
            ("Línea arriba", "UpButton"),
            ("Re Pág", "UpPageButton"),
        ],
    )
    assert p0b_probe.modal_coincide(observado, esperado)


def test_modal_distinto_no_es_equivalente():
    esperado = _modal_esperado(
        "TexGen",
        [("Ignore", "CommandButton_5"), ("Exit TexGen", "CommandButton_3")],
    )
    otro = _modal_observado("Otra cosa", "Otro mensaje", [("OK", "CommandButton_1")])
    assert not p0b_probe.modal_coincide(otro, esperado)
    # mismo nombre pero sin los botones contractuales tampoco alcanza
    parcial = _modal_observado("TexGen", "Found stitched object LOD textures", [("Exit TexGen", "CommandButton_3")])
    assert not p0b_probe.modal_coincide(parcial, esperado)


# ---------------------------------------------------------------------------
# ledger exactly-once
# ---------------------------------------------------------------------------


def test_ledger_una_accion_intentada_no_se_reinvoca():
    ledger = p0b_probe.Ledger()
    assert not ledger.ya_invocada("texgen_start")
    ledger.add(action_id="texgen_start", invoke_attempted=True, invoke_result="S_OK")
    assert ledger.ya_invocada("texgen_start")
    assert not ledger.ya_invocada("texgen_exit")


def test_ledger_no_cuenta_intentos_no_realizados():
    """invoke_attempted=False (o ausente) no bloquea: sólo lo intentado congela."""
    ledger = p0b_probe.Ledger()
    ledger.add(action_id="texgen_start", invoke_attempted=False, invoke_result="N/A")
    assert not ledger.ya_invocada("texgen_start")


def test_fallo_p0_conserva_el_estado_tipado():
    fallo = p0b_probe.FalloP0Error("P0_BLOCKED_BY_ACTION_AMBIGUITY", "ambigüedad")
    assert fallo.estado == "P0_BLOCKED_BY_ACTION_AMBIGUITY"
    assert "ambigüedad" in str(fallo)


# ---------------------------------------------------------------------------
# gates de fuente: P0-A sin mutaciones prohibidas / P0-B sólo Invoke con ledger
# ---------------------------------------------------------------------------

#: Primitivas mutantes que P0 prohíbe en AMBAS sondas (input sintético/Win32).
_PRIMITIVAS_PROHIBIDAS = (
    "WM_SETTEXT",
    "BM_CLICK",
    "SendMessage",
    "PostMessage",
    "mouse_event",
    "SendInput",
    "keybd_event",
    "SetCursorPos",
    "pyautogui",
    "SetFocus",
)


def _fuente(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _tokens_de_codigo(p: pathlib.Path) -> list[str]:
    """Tokens del CÓDIGO (atributos, nombres y constantes string no-docstring).

    Escanear el texto crudo da falsos positivos: los docstrings y comentarios de
    las sondas NOMBRAN las primitivas prohibidas justamente para documentar que
    no se usan. El contrato es sobre el código, así que el ancla lee el AST.
    """
    arbol = ast.parse(_fuente(p))
    docstrings: set[int] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cuerpo = getattr(nodo, "body", [])
            primero = cuerpo[0] if cuerpo else None
            if (
                isinstance(primero, ast.Expr)
                and isinstance(primero.value, ast.Constant)
                and isinstance(primero.value.value, str)
            ):
                docstrings.add(id(primero.value))
    tokens: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            tokens.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            tokens.append(nodo.id)
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docstrings:
            tokens.append(nodo.value)
    return tokens
    return tokens


def test_p0a_no_usa_primitivas_mutantes_prohibidas():
    tokens = _tokens_de_codigo(P0A_SRC)
    for prohibida in _PRIMITIVAS_PROHIBIDAS:
        assert prohibida not in tokens, f"P0-A no puede usar {prohibida} en código"
    arbol = ast.parse(_fuente(P0A_SRC))
    invokes = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "Invoke"
    ]
    assert not invokes, "P0-A no invoca acciones"


def test_p0b_no_usa_primitivas_mutantes_prohibidas():
    tokens = _tokens_de_codigo(P0B_SRC)
    for prohibida in _PRIMITIVAS_PROHIBIDAS:
        assert prohibida not in tokens, f"P0-B no puede usar {prohibida} en código"


def test_p0b_todo_invoke_vive_en_invocar_una_vez():
    """Cada `.Invoke(` de P0-B debe estar dentro de `invocar_una_vez` (ledger)."""
    arbol = ast.parse(_fuente(P0B_SRC))
    padres: dict[ast.AST, ast.AST] = {}
    for padre in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(padre):
            padres[hijo] = padre

    def funcion_contenedora(nodo: ast.AST) -> str | None:
        actual: ast.AST | None = nodo
        while actual is not None:
            if isinstance(actual, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return actual.name
            actual = padres.get(actual)
        return None

    invokes = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "Invoke"
    ]
    assert invokes, "la sonda de acción debe tener exactamente los Invoke de la cadena"
    for nodo in invokes:
        assert funcion_contenedora(nodo) == "invocar_una_vez", (
            "un Invoke fuera de invocar_una_vez esquiva el ledger exactly-once"
        )


def test_p0b_familia_de_acciones_congelada_por_igualdad():
    """Enumera, no muestrea: una acción nueva rompe el ancla hasta agregarla acá."""
    arbol = ast.parse(_fuente(P0B_SRC))
    acciones: list[str] = []
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "invocar_una_vez"
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            acciones.append(nodo.args[0].value)
    assert sorted(acciones) == sorted(
        [
            "texgen_start",
            "texgen_exit",
            "dyndolod_advanced",
            "dyndolod_begin_ok",
            "dyndolod_save_exit",
        ]
    )


def test_p0b_ledger_bloquea_antes_de_invocar():
    """El gate real: si la acción ya fue intentada, FalloP0 en vez de re-invocar."""
    texto = _fuente(P0B_SRC)
    assert "if self.ledger.ya_invocada(action_id):" in texto
    assert "P0_BLOCKED_BY_ACTION_AMBIGUITY" in texto


def test_p0b_modales_iniciales_no_son_automatizables():
    """`Ignore` sólo puede venir de HUMAN; la sonda registra la política explícita."""
    texto = _fuente(P0B_SRC)
    assert "interaction_source=HUMAN" in texto
    assert "automation_policy=NOT_AUTHORIZED" in texto
    # la sonda jamás decide por sí misma el botón de un modal: no existe una
    # invocación cuyo action_id sea un botón de modal.
    assert 'invocar_una_vez("Ignore' not in texto
    assert "invocar_una_vez('Ignore" not in texto


def test_p0a_writer_sin_fallback():
    """La escritura de P0-A/P0-B es por mecanismo explícito, sin fallback automático.

    Ancla precisa: dentro de `set_value` (P0-A) ninguna llamada a `_patron`
    vive en una rama `except` — un fallback "ValuePattern falla → probar
    Legacy" tendría que aparecer exactamente ahí, y no existe.
    """
    arbol = ast.parse(_fuente(P0A_SRC))
    funcion = next(nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "set_value")
    en_except: list[ast.Call] = []
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.ExceptHandler):
            for interno in ast.walk(nodo):
                if (
                    isinstance(interno, ast.Call)
                    and isinstance(interno.func, ast.Attribute)
                    and interno.func.attr == "_patron"
                ):
                    en_except.append(interno)
    assert not en_except, "set_value no puede reintentar el otro patrón desde un except"
    assert "MECANISMO_DESCONOCIDO" in _fuente(P0A_SRC)


def test_p0b_build_sha_congelado():
    """El probe de acción sólo corre contra los hashes medidos (BUILD_DRIFT gate)."""
    assert set(p0b_probe.BUILD_SHA) == {"texgen", "dynodlod"}
    texto = _fuente(P0B_SRC)
    assert "BUILD_DRIFT" in texto


# ---------------------------------------------------------------------------
# CR-9 — STOP sanitizado (ancla del path REAL de escritura)
# ---------------------------------------------------------------------------


def test_cr9_stop_se_escribe_sanitizado(tmp_path, monkeypatch):
    """El archivo de STOP no puede filtrar el username, ni siquiera si llega
    dentro del detalle del error (readback de Output, fingerprint, etc.)."""
    monkeypatch.setattr(p0a_probe, "_usuario", "OperadorX")
    destino = p0b_probe.escribir_stop(
        tmp_path,
        "texgen",
        "P0_BLOCKED_BY_ENVIRONMENT",
        "readback C:\\Users\\OperadorX\\Temp\\SkyClaw-P0B\\TexGen != expected",
    )
    contenido = destino.read_text(encoding="utf-8")
    assert "OperadorX" not in contenido
    assert "<USER>" in contenido
    assert "P0_BLOCKED_BY_ENVIRONMENT" in contenido


def test_cr9_main_usa_escribir_stop():
    """Ancla de fuente: `main()` no vuelve a escribir el STOP a mano."""
    texto = _fuente(P0B_SRC)
    assert "escribir_stop(pathlib.Path(args.evidence)" in texto
    assert 'f"{args.tool}_STOP.txt"' not in texto


# ---------------------------------------------------------------------------
# CR-2 — Begin: `ui_change` derivado NO cuenta como señal independiente
# ---------------------------------------------------------------------------


def test_cr2_ui_change_derivado_solo_no_confirma_begin():
    """Sin ninguna señal real de progreso, el derivado no puede confirmar."""
    assert not p0b_probe.evaluar_confirmacion_de_begin(
        {"process_alive": True, "output_growth": False, "log_growth": False, "ui_change_inferred": True}
    )


def test_cr2_begin_confirmado_requiere_liveness_y_progreso():
    assert p0b_probe.evaluar_confirmacion_de_begin({"process_alive": True, "output_growth": True})
    assert p0b_probe.evaluar_confirmacion_de_begin({"process_alive": True, "log_growth": True})
    # sin liveness no hay confirmación aunque haya crecimiento
    assert not p0b_probe.evaluar_confirmacion_de_begin({"process_alive": False, "output_growth": True})
    # sin progreso no hay confirmación aunque el proceso viva
    assert not p0b_probe.evaluar_confirmacion_de_begin({"process_alive": True})


def test_cr2_confirmar_begin_no_incluye_ui_change_como_senal():
    """Ancla de fuente: el veredicto sale del helper puro, y el derivado se
    llama `ui_change_inferred` (nombre que delata su naturaleza)."""
    texto = _fuente(P0B_SRC)
    assert "evaluar_confirmacion_de_begin(señales)" in texto
    assert '"ui_change_inferred"' in texto


# ---------------------------------------------------------------------------
# CR-3 — journal pre-mutación y restore desde finally
# ---------------------------------------------------------------------------


def test_cr3_journal_persiste_antes_de_mutar(tmp_path, monkeypatch):
    """El journal RESTORE_PENDING queda durable con original + intended,
    sanitizado, ANTES de que la mutación ocurra."""
    monkeypatch.setattr(p0a_probe, "_usuario", "OperadorX")
    ruta = p0a_probe.journal_de_restore(
        tmp_path,
        "texgen",
        pid=4321,
        exe_sha="0939bc8f" * 8,
        fingerprint={"runtime_id": [42, 1], "control_type": "Edit", "name": "Output"},
        original_value="C:\\Users\\OperadorX\\viejo",
        intended_value="%TEMP%\\SkyClaw-P0\\OutputTest",
        estado="RESTORE_PENDING",
    )
    dato = json.loads(ruta.read_text(encoding="utf-8"))
    assert dato["state"] == "RESTORE_PENDING"
    assert dato["intended_temporary_value"].endswith("OutputTest")
    assert "OperadorX" not in ruta.read_text(encoding="utf-8")
    assert dato["tool"] == "texgen" and dato["pid"] == 4321


def test_cr3_restore_corre_desde_finally_con_journal(tmp_path):
    """Ancla AST: el try de mutación tiene un `finally` que (a) restaura vía
    `set_value` y (b) re-persiste el journal (RESTORED/RESTORE_FAILED)."""
    arbol = ast.parse(_fuente(P0A_SRC))
    candidatos = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Try) or not nodo.finalbody:
            continue
        body_txt = ast.dump(ast.Module(body=nodo.body, type_ignores=[]))
        final_txt = ast.dump(ast.Module(body=nodo.finalbody, type_ignores=[]))
        restaura_en_final = "set_value" in final_txt
        journal_en_final = "journal_de_restore" in final_txt
        muta_en_body = "root_test" in body_txt or "str(root_test)" in body_txt
        if restaura_en_final or journal_en_final:
            candidatos.append((muta_en_body, restaura_en_final, journal_en_final))
    assert any(muta and restaura and journal for muta, restaura, journal in candidatos), (
        "debe existir un try de mutación cuyo finally restaure y persista el journal"
    )


def test_cr3_restore_failed_queda_durable(tmp_path, monkeypatch):
    """Si el restore falla, el journal persiste RESTORE_FAILED + errores."""
    monkeypatch.setattr(p0a_probe, "_usuario", "OperadorX")
    ruta = p0a_probe.journal_de_restore(
        tmp_path,
        "texgen",
        pid=99,
        exe_sha="0939bc8f" * 8,
        fingerprint={"runtime_id": [42, 2], "name": "Output"},
        original_value="orig",
        intended_value="tmp",
        estado="RESTORE_FAILED",
        errores={"mutation_error": "COMError temporal"},
    )
    dato = json.loads(ruta.read_text(encoding="utf-8"))
    assert dato["state"] == "RESTORE_FAILED"
    assert dato["errores"]["mutation_error"] == "COMError temporal"


# ---------------------------------------------------------------------------
# CR-1 — escáner de logs: candidatos, errores y relectura post-exit
# ---------------------------------------------------------------------------


def test_cr1_escaner_registra_candidatos_y_errores(tmp_path):
    existe_con_marker = tmp_path / "log_ok.txt"
    existe_con_marker.write_text("hola\nTexGen completed successfully\n", encoding="utf-8")
    existe_sin_marker = tmp_path / "log_vacio.txt"
    existe_sin_marker.write_text("nada util\n", encoding="utf-8")
    inexistente = tmp_path / "no_existe.txt"
    directorio = tmp_path / "es_directorio"
    directorio.mkdir()

    salida = p0b_probe.escanear_logs(
        [inexistente, directorio, existe_sin_marker, existe_con_marker],
        ["TexGen completed successfully"],
        "test",
    )
    por_path = {c["path"]: c for c in salida["candidatos"]}
    assert por_path[str(inexistente)]["error"] == "NOT_FOUND"
    assert por_path[str(directorio)]["read_attempted"] is True
    assert str(por_path[str(directorio)]["error"]).startswith("READ_FAILED")
    assert por_path[str(existe_sin_marker)]["error"] == "NO_MARKERS"
    assert por_path[str(existe_con_marker)]["error"] == "MARKERS_FOUND"
    assert salida["encontrados"] == {"TexGen completed successfully": str(existe_con_marker)}


def test_cr1_escaner_relee_despues_del_exit(tmp_path):
    """El escáner lee SIEMPRE desde disco: una segunda pasada (post-exit)
    ve el contenido que se agregó después de la primera."""
    log = tmp_path / "late.log"
    log.write_text("inicio\n", encoding="utf-8")
    primera = p0b_probe.escanear_logs([log], ["completed successfully"], "en_vivo")
    assert primera["candidatos"][0]["error"] == "NO_MARKERS"
    assert not primera["encontrados"]
    # el proceso terminó y volcó su buffer al log
    log.write_text("inicio\nTexGen completed successfully\n", encoding="utf-8")
    segunda = p0b_probe.escanear_logs([log], ["completed successfully"], "post_exit")
    assert segunda["candidatos"][0]["error"] == "MARKERS_FOUND"
    assert segunda["fase"] == "post_exit"


def test_cr1_cierre_tiene_relectura_post_exit():
    texto = _fuente(P0B_SRC)
    assert "log_corroboration_post_exit" in texto
    assert '"post_exit"' in texto
