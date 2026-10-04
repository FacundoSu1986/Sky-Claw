"""Sonda P0-B-PROBE — cadena de acción UIA completa sobre TexGen/DynDOLOD Alpha-209.

AUTORIZACIÓN: P0-B-PROBE solamente. P0-B-PRODUCT-PIPELINE bloqueado (defectos del
runner OPEN). No usa DynDOLODRunner, packaging, DirectoryRollback ni broker.

Reglas implementadas:
- exactly-once: ledger; una acción con invoke_attempted=True jamás se re-invoca.
- sin fallback: writer = LegacyIAccessiblePattern (medido en P0-A); NUNCA intenta
  ValuePattern en la cadena de escritura.
- modales: se capturan y comparan fingerprints; sólo el HUMANO pulsa Ignore.
  Modal inesperado durante generación → capture + FAIL_CLOSED (terminate del
  proceso propio) + STOP.
- readback exacto obligatorio antes de Begin; mismatch → STOP.
- completion: sólo el diálogo terminal contractual (Exit TexGen exacto, no
  "Zip and Exit"; Save & Exit exacto, no "Save, Zip and Exit").
- métricas pasivas #654; -RealTimeLog NO se usa.

Uso:
    python p0b_probe.py --tool texgen   --evidence <dir> --output-root <dir> --modal-timeout 600 --generation-timeout 2400
    python p0b_probe.py --tool dynodlod --evidence <dir> --output-root <dir> ...
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

from p0a_probe import (
    EXE_DYNDOLOD,
    EXE_TEXGEN,
    PRESET_DYNDOLOD,
    PRESET_TEXGEN,
    RIG_DATA,
    RIG_INI,
    RIG_PLUGINS,
    UiAdapter,
    estado_archivos,
    pids_de_imagen,
    sanitizar,
    sha256_de,
    verificar_root_temporal,
)

BUILD_SHA = {
    "texgen": "0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62",
    "dynodlod": "b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0",
}

#: Fingerprints de los modales iniciales medidos en P0-A (rondas con MATCH).
EXPECTED_STARTUP_MODAL = {
    "texgen": {
        "name": "TexGen",
        "instruction": "Found stitched object LOD textures from earlier TexGen generation installed in game folder.",
        "buttons": [("Ignore", "CommandButton_5"), ("Exit TexGen", "CommandButton_3")],
    },
    "dynodlod": {
        "name": "DynDOLOD",
        "instruction": "DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!",
        "buttons": [("Ignore", "CommandButton_5"), ("Exit DynDOLOD", "CommandButton_3")],
    },
}

TERMINAL_BUTTON = {
    "texgen": "Exit TexGen",
    "dynodlod": "Save & Exit",  # también "Save and Exit"; NUNCA variantes con Zip
}

#: Aceptación del diálogo terminal por herramienta (detección, no identidad final).
TERMINAL_NAMES = {
    "texgen": {"Exit TexGen"},
    "dynodlod": {"Save & Exit", "Save and Exit"},
}


def log(msg: str) -> None:
    """Imprime a la consola del host sin morir por caracteres no representables.

    El texto de los diálogos del provider trae flechas (U+2192) y la consola de
    Windows puede ser cp1252: sin este sanitize el probe moría con
    UnicodeEncodeError al registrar el modal, perdiendo toda la corrida.
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(msg.encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)


class Ledger:
    """Exactly-once: una acción invocada jamás se re-invoca, aun si el resultado es ambiguo."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, **kwargs: object) -> dict:
        self.entries.append(kwargs)
        log(f"LEDGER: {json.dumps({k: v for k, v in kwargs.items() if k != 'control_fingerprint'}, default=str)}")
        return kwargs

    def ya_invocada(self, action_id: str) -> bool:
        return any(e.get("action_id") == action_id and e.get("invoke_attempted") for e in self.entries)


class FalloP0Error(Exception):
    """Excepción tipada del rig: el estado P0 viaja con la razón."""

    def __init__(self, estado: str, detalle: str) -> None:
        super().__init__(f"{estado}: {detalle}")
        self.estado = estado
        self.detalle = detalle


def fingerprint_de_ventana(adapter: UiAdapter, el: object, controles: list[dict]) -> dict:
    """Fingerprint canónico de un diálogo.

    `name` sale del propio diálogo (el control #32770), NO del título de la
    ventana que lo contiene: en Alpha-209 el #32770 es un control anidado de
    TfrmMain y usar el título de la ventana hacía que todo modal pareciera
    MISMATCH contra su propia referencia.
    """
    dialogo = controles[0] if controles and controles[0].get("class_name") == "#32770" else None
    instruction = next((c["name"] for c in controles if c.get("control_type") == "Text" and c.get("name")), None)
    return {
        "class_name": (dialogo or {}).get("class_name") or adapter._txt(el, "UIA_ClassNamePropertyId"),
        "name": (dialogo or {}).get("name") if dialogo else adapter._txt(el, "UIA_NamePropertyId"),
        "hwnd": adapter.prop(el, "UIA_NativeWindowHandlePropertyId"),
        "instruccion": instruction,
        "botones": [
            {"name": c.get("name"), "automation_id": c.get("automation_id")}
            for c in controles
            if c.get("control_type") == "Button" and c.get("name") and "class_name" in c
        ][:6],
    }


def modal_coincide(obs: dict, esperado: dict) -> bool:
    botones = [(b.get("name"), b.get("automation_id")) for b in (obs.get("botones") or [])]
    faltan = [b for b in esperado["buttons"] if b not in botones]
    return obs.get("name") == esperado["name"] and not faltan


def evaluar_confirmacion_de_begin(señales: dict) -> bool:
    """CR-2: Begin confirmado = liveness AND progreso observable.

    `ui_change_inferred` es DERIVADO de (output_growth OR log_growth): no es
    una señal independiente y NO participa del veredicto.
    """
    progreso = bool(señales.get("output_growth") or señales.get("log_growth"))
    return bool(señales.get("process_alive")) and progreso


def escanear_logs(fuentes: list[pathlib.Path], markers: list[str], fase: str) -> dict:
    """CR-1: escaneo de logs con CANDIDATOS y resultados tipados.

    Lee SIEMPRE desde disco (sin caché), así que puede re-ejecutarse después
    del exit del proceso. Jamás traga un OSError sin evidencia:
      NOT_FOUND     el archivo no existe
      READ_FAILED   existe pero la lectura falló (error persistido)
      NO_MARKERS    se leyó y no contiene ninguno de los markers
      MARKERS_FOUND se leyó y contiene al menos un marker
    """
    out: dict[str, object] = {
        "fase": fase,
        "encontrados": {},
        "candidatos": [],
        "using_output_path": None,
        "using_data_path": None,
    }
    for ruta in fuentes:
        candidato: dict[str, object] = {
            "path": str(ruta),
            "exists": ruta.exists(),
            "size": None,
            "mtime": None,
            "read_attempted": False,
            "read_ok": False,
            "error": None,
            "markers_encontrados": [],
        }
        out["candidatos"].append(candidato)
        if not ruta.exists():
            candidato["error"] = "NOT_FOUND"
            continue
        try:
            st = ruta.stat()
            candidato["size"] = st.st_size
            candidato["mtime"] = time.strftime("%H:%M:%S", time.localtime(st.st_mtime))
        except OSError as e:
            candidato["error"] = f"STAT_FAILED: {e!r}"
        candidato["read_attempted"] = True
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            candidato["error"] = f"READ_FAILED: {e!r}"
            continue
        candidato["read_ok"] = True
        for m in markers:
            if m in texto and m not in out["encontrados"]:
                out["encontrados"][m] = str(ruta)
                candidato["markers_encontrados"].append(m)
        candidato["error"] = "MARKERS_FOUND" if candidato["markers_encontrados"] else "NO_MARKERS"
        for linea in texto.splitlines():
            if out["using_output_path"] is None and linea.strip().startswith("Using Output Path:"):
                out["using_output_path"] = linea.strip()
            if out["using_data_path"] is None and " Data Path: " in linea and linea.strip().startswith("Using"):
                out["using_data_path"] = linea.strip()
    return out


def escribir_stop(evidencia: pathlib.Path, tool: str, estado: str, detalle: str) -> pathlib.Path:
    """CR-9: el STOP se persiste SANITIZADO — el detalle puede contener paths
    personales (readback de un Output con username, fingerprint de modal)."""
    destino = evidencia / f"{tool}_STOP.txt"
    destino.write_text(sanitizar(f"{estado}\n{detalle}\n"), encoding="utf-8")
    return destino


class RunnerP0B:
    def __init__(self, tool: str, args: argparse.Namespace) -> None:
        self.tool = tool
        self.args = args
        self.exe = EXE_TEXGEN if tool == "texgen" else EXE_DYNDOLOD
        self.preset = PRESET_TEXGEN if tool == "texgen" else PRESET_DYNDOLOD
        self.ledger = Ledger()
        self.evi = pathlib.Path(args.evidence)
        self.evi.mkdir(parents=True, exist_ok=True)
        self.adapter = UiAdapter()
        self.inicio = time.monotonic()
        self.marcas: dict[str, float] = {}
        self.modal_encontrados: list[dict] = []
        self.proceso: subprocess.Popen | None = None

    def marca(self, nombre: str) -> None:
        self.marcas[nombre] = time.monotonic() - self.inicio

    # -- infra ---------------------------------------------------------------

    def ventanas_pid(self) -> list[tuple[object, list[dict]]]:
        """(ventana, controles) top-level del PID, enumeración FRESCA cada llamada."""
        out = []
        for w in self.adapter.ventanas_de_proceso(self.proceso.pid):
            try:
                controles, _total, _ileg = self.adapter.controles_de_ventana(w)
            except (self.adapter._ct.COMError, OSError, ValueError, AttributeError):
                # la enumeración de una ventana recién aparecida puede fallar
                # transitoriamente; la ventana se relee en el próximo ciclo.
                controles = []
            out.append((w, controles))
        return out

    def ventana_con_tedit(self) -> tuple[object, list[dict], dict] | None:
        for w, controles in self.ventanas_pid():
            tedit = [c for c in controles if c.get("class_name") == "TEdit" and c.get("control_type") == "Edit"]
            if len(tedit) == 1:
                return w, controles, tedit[0]
        return None

    def elemento_por_runtime_id(self, ventana: object, rid: list[int]) -> object | None:
        for sub in _descendientes(self.adapter, ventana):
            try:
                if [int(x) for x in sub.GetRuntimeId()] == rid:
                    return sub
            except self.adapter._ct.COMError:
                continue
        return None

    def hwnd_de(self, ventana: object) -> int | None:
        try:
            return self.adapter.prop(ventana, "UIA_NativeWindowHandlePropertyId")
        except self.adapter._ct.COMError:
            return None

    def controles_por_hwnd(self, hwnd: int | None) -> list[dict]:
        """Enumeración FRESCA matched por HWND (la identidad de objeto COM no sobrevive re-enumeraciones)."""
        for w, controles in self.ventanas_pid():
            if hwnd is not None and self.hwnd_de(w) == hwnd:
                return controles
        return []

    def botones_por_nombre(self, hwnd: int | None, nombre: str) -> list[dict]:
        return [
            c for c in self.controles_por_hwnd(hwnd) if c.get("control_type") == "Button" and c.get("name") == nombre
        ]

    def invocar_una_vez(self, action_id: str, ventana: object, control: dict, postcondition: str) -> dict:
        if self.ledger.ya_invocada(action_id):
            raise FalloP0Error("P0_BLOCKED_BY_ACTION_AMBIGUITY", f"{action_id} ya estaba invocada: no se re-invoca")
        el = self.elemento_por_runtime_id(ventana, control["runtime_id"])
        if el is None:
            raise FalloP0Error(
                "P0_BLOCKED_BY_UNSTABLE_IDENTITY", f"{action_id}: el control desapareció antes del Invoke"
            )
        try:
            inv = self.adapter._patron(el, "UIA_InvokePatternId", self.adapter._mod.IUIAutomationInvokePattern)
        except (self.adapter._ct.COMError, ValueError, OSError, AttributeError):
            inv = None
        if inv is None:
            raise FalloP0Error("P0_BLOCKED_BY_UNAUTHORIZED_MECHANISM", f"{action_id}: InvokePattern no disponible")
        self.marca(f"{action_id}_before")
        try:
            inv.Invoke()
            resultado = "S_OK"
        except self.adapter._ct.COMError as e:
            resultado = f"COMError {hex(e.hresult)}"
        except (ValueError, OSError) as e:
            resultado = f"COM_POINTER {e!r}"
        entry = self.ledger.add(
            action_id=action_id,
            tool=self.tool,
            build_sha256=BUILD_SHA[self.tool],
            pid=self.proceso.pid,
            control_fingerprint={
                k: control.get(k)
                for k in (
                    "control_type",
                    "class_name",
                    "automation_id",
                    "name",
                    "native_hwnd",
                    "runtime_id",
                    "is_enabled",
                )
            },
            timestamp_before=time.strftime("%Y-%m-%dT%H:%M:%S"),
            invoke_attempted=True,
            invoke_result=resultado,
            postcondition=postcondition,
        )
        if resultado != "S_OK":
            raise FalloP0Error("P0_BLOCKED_BY_ACTION_AMBIGUITY", f"{action_id}: Invoke devolvió {resultado}")
        return entry

    def capturar_modal(self, w: object, controles: list[dict], fase: str) -> dict:
        fp = fingerprint_de_ventana(self.adapter, w, controles)
        fp["phase"] = fase
        fp["captured_at"] = time.strftime("%H:%M:%S")
        self.modal_encontrados.append(fp)
        log(
            f"MODAL [{fase}] class={fp['class_name']} name={fp['name']!r} instr={fp['instruccion']!r} "
            f"botones={[b['name'] for b in fp['botones']]}"
        )
        return fp

    def esperar_modal_inicial(self) -> None:
        """§6: modal inicial conocido → humano. MISMATCH o modal extraño → STOP.

        En Alpha-209 el diálogo #32770 aparece como CONTROL interno de TfrmMain
        (medido en P0-A), no como ventana top-level separada: se detecta por
        control y su fingerprint se arma desde los controles del diálogo.
        """
        esperado = EXPECTED_STARTUP_MODAL[self.tool]
        aviso_hecho = False
        deadline = time.time() + self.args.modal_timeout
        while time.time() < deadline:
            if self.proceso.poll() is not None:
                raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", "el proceso terminó antes de mostrar el wizard")
            for w, controles in self.ventanas_pid():
                # ventana contractual: contiene el TEdit
                if any(c.get("class_name") == "TEdit" for c in controles):
                    return
                # diálogo anidado dentro de la top-level (o top-level #32770 pura)
                if controles and controles[0].get("class_name") == "#32770":
                    fp = self.capturar_modal(w, controles, "startup")
                    if not modal_coincide(fp, esperado):
                        raise FalloP0Error(
                            "P0_BLOCKED_BY_ENVIRONMENT",
                            f"modal inicial MISMATCH: {fp['instruccion']!r} botones={fp['botones']}",
                        )
                    if not aviso_hecho:
                        log(
                            "HUMAN_ACTION_NEEDED: modal inicial MATCH — el HUMANO puede pulsar Ignore "
                            "(interaction_source=HUMAN, automation_policy=NOT_AUTHORIZED)"
                        )
                        aviso_hecho = True
            time.sleep(2.0)
        raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", "timeout esperando resolución humana del modal inicial")

    def escritura_output(self, output_root: str) -> dict:
        """§7/§4: LegacyIAccessible.SetValue + readback exacto. Sin fallback."""
        hallazgo = self.ventana_con_tedit()
        if hallazgo is None:
            raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", "ventana contractual con TEdit único no encontrada")
        w, controles, tedit = hallazgo
        el = self.elemento_por_runtime_id(w, tedit["runtime_id"])
        if el is None:
            raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", "TEdit stale antes de SetValue")
        original = self.adapter.lectura_output(el)
        escritura = self.adapter.set_value(el, "LegacyIAccessible", output_root)
        if escritura.get("hresult") != "S_OK":
            raise FalloP0Error("P0_BLOCKED_BY_UNAUTHORIZED_MECHANISM", f"Legacy SetValue falló: {escritura}")
        readback = self.adapter.lectura_output(el)
        leido = readback.get("value_pattern_value") or readback.get("legacy_value")
        if leido != output_root:
            raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", f"readback {leido!r} != expected {output_root!r} → STOP")
        log(f"OUTPUT_SET_OK: readback exacto = {leido}")
        return {"original": original, "escritura": escritura, "readback": leido}

    def metricas_de_archivos(self) -> dict:
        out = {}
        for nombre in ("stdout", "stderr"):
            p = self.evi / f"{self.tool}_{nombre}.txt"
            try:
                out[f"{nombre}_bytes"] = p.stat().st_size
            except OSError:
                out[f"{nombre}_bytes"] = None
        return out

    def archivos_de_log_crecidos(self) -> list[str]:
        """Logs del tool dir modificados desde el launch (corroboration §23)."""
        crecidos = []
        raiz = pathlib.Path(self.exe).parent
        candidatos = [
            raiz / "Logs",
            raiz / "Edit Scripts" / "DynDOLOD" / "Logs",
            raiz / "Edit Scripts" / "TexGen" / "Logs",
            raiz / "Edit Scripts" / "DynDOLOD",
        ]
        for base in candidatos:
            if not base.is_dir():
                continue
            for f in base.rglob("*"):
                if f.is_file():
                    try:
                        if f.stat().st_mtime >= self.t_launch - 5:
                            crecidos.append(str(f))
                    except OSError:
                        pass
        return crecidos

    # -- corrida -------------------------------------------------------------

    def correr(self) -> dict:
        tool = self.tool
        registro: dict[str, object] = {
            "tool": tool,
            "mode": "P0_B_PROBE",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        output_root = pathlib.Path(self.args.output_root)
        info_root = verificar_root_temporal(output_root)
        if not info_root.get("born_empty") or not info_root.get("sin_reparse_en_cadena"):
            raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", f"root temporal inválido: {info_root}")
        registro["output_root"] = info_root

        sha = (sha256_de(self.exe) or "").lower()
        if sha != BUILD_SHA[tool]:
            raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", f"BUILD_DRIFT: sha {sha} != {BUILD_SHA[tool]}")
        registro["exe_sha256"] = sha
        registro["preset_antes"] = estado_archivos([self.preset])

        residuos_previos = pids_de_imagen(pathlib.Path(self.exe).name)
        if residuos_previos:
            raise FalloP0Error("P0_BLOCKED_BY_ENVIRONMENT", f"procesos residuales previos: {residuos_previos}")

        argv = [
            self.exe,
            "-sse",
            f"-o:{output_root}\\",
            f"-d:{RIG_DATA}\\",
            f"-m:{RIG_INI}\\",
            f"-p:{RIG_PLUGINS}",
            f"-t:{self.args.temp_dir}\\",
        ]
        registro["argv"] = argv
        # Los handles quedan abiertos durante TODA la corrida (los hereda el
        # proceso) y se cierran deliberadamente en _cierre, no aquí.
        stdout_f = open(self.evi / f"{tool}_stdout.txt", "wb")  # noqa: SIM115
        stderr_f = open(self.evi / f"{tool}_stderr.txt", "wb")  # noqa: SIM115
        self.proceso = subprocess.Popen(argv, cwd=str(pathlib.Path(self.exe).parent), stdout=stdout_f, stderr=stderr_f)
        self.t_launch = time.time()
        self.marca("launch")
        log(f"SPAWN pid={self.proceso.pid} argv={argv}")

        try:
            self.esperar_modal_inicial()
            self.marca("wizard_disponible")
            log("MODAL_TEXGEN_REVALIDADO fingerprint=MATCH" if tool == "texgen" else "MODAL_DYNDOLOD_MATCH")

            # §7 — configuración del Output (Legacy, readback exacto)
            self.config_output = self.escritura_output(str(output_root))
            self.marca("output_set")

            if tool == "texgen":
                registro.update(self.cadena_texgen(output_root))
            else:
                registro.update(self.cadena_dyndolod(output_root))
        finally:
            self._cierre(registro, stdout_f, stderr_f)
        return registro

    # -- TexGen ---------------------------------------------------------------

    def cadena_texgen(self, output_root: pathlib.Path) -> dict:
        registro: dict[str, object] = {}
        hallazgo = self.ventana_con_tedit()
        if hallazgo is None:
            raise FalloP0Error(
                "P0_BLOCKED_BY_UNSTABLE_IDENTITY", "ventana contractual con TEdit único no encontrada antes de Start"
            )
        w, controles, _tedit = hallazgo
        starts = [c for c in controles if c.get("control_type") == "Button" and c.get("name") == "Start"]
        if len(starts) != 1 or not starts[0].get("is_enabled"):
            raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", f"Start candidatos={len(starts)}")
        registro["start_fingerprint"] = {
            k: starts[0].get(k)
            for k in ("control_type", "class_name", "automation_id", "name", "native_hwnd", "runtime_id", "is_enabled")
        }
        self.invocar_una_vez("texgen_start", w, starts[0], "postcondition: confirmar señales de Begin")
        self.marca("begin_invoke")

        # §9 — confirmación con ≥2 señales independientes
        confirmado = self.confirmar_begin(output_root)
        registro["begin_signals"] = confirmado
        if not confirmado["confirmado"]:
            raise FalloP0Error(
                "P0_BLOCKED_BY_ACTION_AMBIGUITY", "BEGIN_NOT_CONFIRMED: sin señales de inicio de generación"
            )

        # §10/§11 — observación hasta el diálogo terminal
        terminal = self.observar_generacion(output_root, "TexGen completed successfully")
        registro["generation"] = terminal["generation"]
        if terminal.get("estado") != "TERMINAL_DIALOG":
            raise FalloP0Error("P0_BLOCKED_BY_COMPLETION_AMBIGUITY", f"sin diálogo terminal: {terminal.get('estado')}")
        registro["completion_fingerprint"] = terminal["fingerprint"]
        registro["log_markers"] = terminal.get("log_markers")

        # §12 — Exit TexGen exacto (NUNCA Zip and Exit; su presencia como opción es normal)
        w_t, _c_t = terminal["ventana"]
        hwnd_t = self.hwnd_de(w_t)
        exits = self.botones_por_nombre(hwnd_t, "Exit TexGen")
        zips = [
            c.get("name")
            for c in self.controles_por_hwnd(hwnd_t)
            if c.get("control_type") == "Button" and c.get("name") and "zip" in c["name"].lower()
        ]
        registro["zip_variants_presentes"] = zips
        if len(exits) != 1:
            raise FalloP0Error(
                "P0_BLOCKED_BY_TERMINAL_ACTION",
                f"'Exit TexGen' exacto candidatos={len(exits)}; botones terminal={zips}",
            )
        registro["exit_fingerprint"] = {
            k: exits[0].get(k)
            for k in ("control_type", "class_name", "automation_id", "name", "native_hwnd", "runtime_id")
        }
        self.invocar_una_vez("texgen_exit", w_t, exits[0], "postcondition: process exit")
        self.marca("terminal_invoke")

        try:
            self.proceso.wait(timeout=120)
        except subprocess.TimeoutExpired as e:
            raise FalloP0Error("P0_BLOCKED_BY_TERMINAL_ACTION", "el proceso no salió tras Exit TexGen") from e
        self.marca("process_exit")
        registro["exit_code"] = self.proceso.returncode
        return registro

    # -- DynDOLOD ---------------------------------------------------------------

    def cadena_dyndolod(self, output_root: pathlib.Path) -> dict:
        registro: dict[str, object] = {}
        hallazgo = self.ventana_con_tedit()
        if hallazgo is None:
            raise FalloP0Error(
                "P0_BLOCKED_BY_UNSTABLE_IDENTITY", "ventana contractual con TEdit único no encontrada antes de Advanced"
            )
        w, controles, _tedit = hallazgo
        advanced = [c for c in controles if c.get("control_type") == "Button" and c.get("name") == "Advanced >>>"]
        if len(advanced) != 1 or not advanced[0].get("is_enabled"):
            raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", f"Advanced >>> candidatos={len(advanced)}")
        registro["advanced_fingerprint"] = {
            k: advanced[0].get(k)
            for k in ("control_type", "class_name", "automation_id", "name", "native_hwnd", "runtime_id")
        }
        output_antes = self.config_output["readback"]
        self.invocar_una_vez("dyndolod_advanced", w, advanced[0], "postcondition: rediscover + re-read Output")
        self.marca("advanced_invoke")
        time.sleep(3.0)

        # §14 — ¿Advanced recargó Output desde el preset?
        hallazgo2 = self.ventana_con_tedit()
        if hallazgo2 is None:
            raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", "sin TEdit tras Advanced")
        w2, controles2, tedit2 = hallazgo2
        el2 = self.elemento_por_runtime_id(w2, tedit2["runtime_id"])
        if el2 is None:
            raise FalloP0Error(
                "P0_BLOCKED_BY_UNSTABLE_IDENTITY", "TEdit stale tras Advanced (la ventana se reconstruyó)"
            )
        rb2 = self.adapter.lectura_output(el2)
        output_despues = rb2.get("value_pattern_value") or rb2.get("legacy_value")
        if output_despues == output_antes:
            registro["advanced_reloaded_output_from_preset"] = "NO"
        else:
            registro["advanced_reloaded_output_from_preset"] = "YES"
            registro["output_antes_de_advanced"] = output_antes
            registro["output_despues_de_advanced"] = output_despues
            log(f"ADVANCED_RELOADED_OUTPUT_FROM_PRESET=YES ({output_despues!r}) → reconvergencia con Legacy")
            self.config_output = self.escritura_output(str(output_root))
            registro["reconvergencia"] = self.config_output["readback"]
            hallazgo2 = self.ventana_con_tedit()
            if hallazgo2 is None:
                raise FalloP0Error("P0_BLOCKED_BY_UNSTABLE_IDENTITY", "sin TEdit tras la reconvergencia del Output")
            w2, controles2, tedit2 = hallazgo2
            w, controles = w2, controles2

        # §16 — Begin real: OK estructural de la ventana contractual (NUNCA #32770)
        oks = [
            c
            for c in controles2
            if c.get("control_type") == "Button"
            and c.get("name") == "OK"
            and c.get("is_enabled")
            and c.get("class_name") == "TButton"
        ]
        if len(oks) != 1:
            nombres = [c.get("name") for c in controles if c.get("control_type") == "Button"]
            raise FalloP0Error(
                "P0_BLOCKED_BY_UNSTABLE_IDENTITY", f"OK estructural candidatos={len(oks)}; botones={nombres}"
            )
        registro["begin_fingerprint"] = {
            k: oks[0].get(k)
            for k in ("control_type", "class_name", "automation_id", "name", "native_hwnd", "runtime_id", "is_enabled")
        }
        self.invocar_una_vez("dyndolod_begin_ok", w2, oks[0], "postcondition: confirmar señales de Begin")
        self.marca("begin_invoke")

        confirmado = self.confirmar_begin(output_root)
        registro["begin_signals"] = confirmado
        if not confirmado["confirmado"]:
            raise FalloP0Error("P0_BLOCKED_BY_ACTION_AMBIGUITY", "BEGIN_NOT_CONFIRMED")

        terminal = self.observar_generacion(output_root, "DynDOLOD plugins generated successfully")
        registro["generation"] = terminal["generation"]
        if terminal.get("estado") != "TERMINAL_DIALOG":
            raise FalloP0Error("P0_BLOCKED_BY_COMPLETION_AMBIGUITY", f"sin diálogo terminal: {terminal.get('estado')}")
        registro["completion_fingerprint"] = terminal["fingerprint"]
        registro["log_markers"] = terminal.get("log_markers")

        # §18/§19 — Save & Exit exacto (variantes Zip como opción son normales; no se usan)
        w_t, _c_t = terminal["ventana"]
        hwnd_t = self.hwnd_de(w_t)
        candidatos = self.botones_por_nombre(hwnd_t, "Save & Exit") or self.botones_por_nombre(hwnd_t, "Save and Exit")
        todos_los_botones_terminal = [
            c.get("name")
            for c in self.controles_por_hwnd(hwnd_t)
            if c.get("control_type") == "Button" and c.get("name")
        ]
        registro["botones_terminal"] = todos_los_botones_terminal
        if len(candidatos) != 1:
            raise FalloP0Error(
                "P0_BLOCKED_BY_TERMINAL_ACTION",
                f"'Save & Exit' exacto candidatos={len(candidatos)}; botones terminal={todos_los_botones_terminal}",
            )
        registro["save_exit_fingerprint"] = {
            k: candidatos[0].get(k)
            for k in ("control_type", "class_name", "automation_id", "name", "native_hwnd", "runtime_id")
        }
        self.invocar_una_vez("dyndolod_save_exit", w_t, candidatos[0], "postcondition: process exit + guardado")
        self.marca("terminal_invoke")
        try:
            self.proceso.wait(timeout=180)
        except subprocess.TimeoutExpired as e:
            raise FalloP0Error("P0_BLOCKED_BY_TERMINAL_ACTION", "el proceso no salió tras Save & Exit") from e
        self.marca("process_exit")
        registro["exit_code"] = self.proceso.returncode
        return registro

    # -- señales de Begin / observación -------------------------------------

    def conteo_output(self, root: pathlib.Path) -> int:
        n = 0
        for _actual, _dirs, files in os.walk(root):
            n += len(files)
        return n

    def confirmar_begin(self, output_root: pathlib.Path) -> dict:
        """§9 (CR-2): Begin confirmado = liveness + una señal observable de progreso.

        Contrato de ESTA sonda — las señales REALES son:
          process_alive   el proceso sigue vivo (liveness)
          output_growth   crecieron archivos del root gestionado (progreso)
          log_growth      creció la evidencia de log (progreso)

        `ui_change` es DERIVADO de (output_growth OR log_growth): NO es una
        señal independiente y NO entra al contador — se conserva renombrado
        como `ui_change_inferred` sólo como estado descriptivo.
        """
        base_out = self.conteo_output(output_root)
        base_err = self.metricas_de_archivos().get("stderr_bytes") or 0
        señales: dict[str, object] = {
            "process_alive": False,
            "output_growth": False,
            "log_growth": False,
            "ui_change_inferred": False,
        }
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proceso.poll() is None:
                señales["process_alive"] = True
            if self.conteo_output(output_root) > base_out:
                señales["output_growth"] = True
            err_now = self.metricas_de_archivos().get("stderr_bytes") or 0
            if err_now > base_err + 4096:
                señales["log_growth"] = True
            progreso = bool(señales["output_growth"] or señales["log_growth"])
            if progreso:
                señales["ui_change_inferred"] = True  # DERIVADO, no independiente
                break
            time.sleep(3.0)
        progreso = bool(señales["output_growth"] or señales["log_growth"])
        señales["progress_signal"] = progreso
        señales["confirmado"] = evaluar_confirmacion_de_begin(señales)
        log(f"BEGIN_SIGNALS: {señales}")
        return señales

    def _dialogos(self, ventana_controles: list[tuple[object, list[dict]]]) -> list[tuple[object, list[dict], dict]]:
        """Diálogos anidados o top-level, con su fingerprint.

        Alpha-209 expone el #32770 como CONTROL interno de TfrmMain (medido en
        P0-A), no siempre como ventana top-level propia: se buscan ambos.
        """
        encontrados = []
        for w, controles in ventana_controles:
            if controles and controles[0].get("class_name") == "#32770":
                encontrados.append((w, controles, self.capturar_modal(w, controles, "dialogo")))
        return encontrados

    def observar_generacion(self, output_root: pathlib.Path, marker: str) -> dict:
        """§10/§17: NO UI mutation; modales fail-closed; terminal contractual."""
        esperado_startup = EXPECTED_STARTUP_MODAL[self.tool]
        generation: dict[str, object] = {"polls": 0, "modal_durante_generacion": []}
        ultimo_out = 0
        deadline = time.time() + self.args.generation_timeout
        while time.time() < deadline:
            generation["polls"] = int(generation["polls"]) + 1
            vivo = self.proceso.poll() is None
            out_n = self.conteo_output(output_root)
            if out_n != ultimo_out:
                log(f"GEN: output_files={out_n} alive={vivo} t={time.monotonic() - self.inicio:.0f}s")
                ultimo_out = out_n
            ventanas = self.ventanas_pid()
            for w, controles, fp in self._dialogos(ventanas):
                hay_tedit = any(c.get("class_name") == "TEdit" for c in controles)
                if hay_tedit:
                    continue
                nombres = [c.get("name") for c in controles if c.get("control_type") == "Button" and c.get("name")]
                # el wizard de TexGen mantiene su TEdit detrás del diálogo: la
                # señal de contrato es el botón terminal, no la ausencia de TEdit
                if any(
                    c.get("name") in TERMINAL_NAMES[self.tool] for c in controles if c.get("control_type") == "Button"
                ):
                    self.marca("terminal_dialog")
                    log(f"TERMINAL_DIALOG detectado: {nombres}")
                    return {
                        "estado": "TERMINAL_DIALOG",
                        "ventana": (w, controles),
                        "fingerprint": fp,
                        "controles_terminal": controles,
                        "generation": generation,
                        "log_markers": self.buscar_markers([marker], "terminal_dialog"),
                    }
                if modal_coincide(fp, esperado_startup):
                    log("HUMAN_ACTION_NEEDED (generation): modal conocido reapareció — el HUMANO decide")
                    fp["human_interaction"] = "requested"
                    time.sleep(5.0)
                    continue
                if any(b["name"] in TERMINAL_NAMES["texgen"] for b in fp["botones"]) and any(
                    "zip" in (n or "").lower() for n in nombres
                ):
                    continue  # diálogo de progreso/verificación, no terminal aún
                # modal desconocido durante generación → fail-closed
                fp["blocking"] = True
                generation["modal_durante_generacion"].append(fp)
                raise FalloP0Error("P0_BLOCKED", f"MODAL_DESCONOCIDO_DURANTE_GENERACION: {fp['instruccion']!r}")
            if not vivo:
                self.marca("process_exit_sin_dialogo")
                markers = self.buscar_markers([marker], "process_exit_sin_dialogo")
                return {
                    "estado": "PROCESS_EXIT",
                    "generation": generation,
                    "log_markers": markers,
                    "exit_code": self.proceso.returncode,
                }
            time.sleep(3.0)
        raise FalloP0Error("P0_BLOCKED_BY_COMPLETION_AMBIGUITY", "generation_timeout sin estado terminal")

    def buscar_markers(self, markers: list[str], fase: str) -> dict:
        """§23/§27: corroboración por logs del tool dir + stderr, con CANDIDATOS.

        Delega en `escanear_logs` (función pura, testeable sin COM ni proceso).
        """
        fuentes: list[str] = list(self.archivos_de_log_crecidos())
        fuentes.append(str(self.evi / f"{self.tool}_stderr.txt"))
        return escanear_logs([pathlib.Path(f) for f in fuentes[:12]], markers, fase)

    # -- cierre ---------------------------------------------------------------

    def _cierre(self, registro: dict, stdout_f, stderr_f) -> None:
        try:
            if self.proceso.poll() is None:
                # fail-closed: el probe no deja generación huérfana
                log("CLEANUP: proceso vivo al cerrar el probe → terminate (propio, no ajeno)")
                self.proceso.terminate()
                try:
                    self.proceso.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proceso.kill()
            registro["ledger"] = self.ledger.entries
            registro["modales"] = self.modal_encontrados
            registro["marcas_segundos"] = {k: round(v, 1) for k, v in self.marcas.items()}
            registro["metricas"] = self.metricas_de_archivos()
            registro["preset_final"] = estado_archivos([self.preset])
            time.sleep(1.5)
            registro["residual_processes"] = [
                {"pid": p, "exe": e} for p, e in pids_de_imagen(pathlib.Path(self.exe).name)
            ]
            # CR-1 — relectura de logs DESPUÉS de que el proceso terminó: los
            # buffers de log del tool se vacían al salir; re-leer aquí captura
            # los markers finales que la observación en vivo pudo perder.
            registro["log_corroboration_post_exit"] = self.buscar_markers(
                ["completed successfully", "plugins generated successfully", "User says"],
                "post_exit",
            )
            registro["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            destino = self.evi / f"{self.tool}_p0b.json"
            destino.write_text(
                json.dumps(sanitizar(registro), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
            log(f"EVIDENCIA: {destino}")
        finally:
            stdout_f.close()
            stderr_f.close()
            self.adapter.liberar()


def _descendientes(adapter: UiAdapter, ventana: object) -> list[object]:
    try:
        cond = adapter._uia.CreateTrueCondition()
        col = ventana.FindAll(adapter._mod.TreeScope_Descendants, cond)
        return [col.GetElement(i) for i in range(col.Length)]
    except adapter._ct.COMError:
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", choices=["texgen", "dynodlod"], required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--modal-timeout", type=int, default=600)
    parser.add_argument("--generation-timeout", type=int, default=2400)
    args = parser.parse_args()

    try:
        registro = RunnerP0B(args.tool, args).correr()
    except FalloP0Error as e:
        log(f"STOP {e.estado}: {e.detalle}")
        # CR-9: STOP sanitizado (ver `escribir_stop`)
        escribir_stop(pathlib.Path(args.evidence), args.tool, e.estado, e.detalle)
        return 2
    log(
        f"CHAIN_OK: {args.tool} — exit_code={registro.get('exit_code')} "
        f"ledger={len(registro.get('ledger', []))} modales={len(registro.get('modales', []))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
