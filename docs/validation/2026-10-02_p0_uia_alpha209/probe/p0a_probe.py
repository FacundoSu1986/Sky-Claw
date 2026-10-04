"""Sonda P0-A — diagnóstico real Windows UIA sobre TexGen/DynDOLOD Alpha-209.

Script INDEPENDIENTE: no importa nada de sky_claw. Read-only salvo los dos
experimentos de SetValue explícitos (ValuePattern y LegacyIAccessible) sobre el
campo Output, con readback y restauración por el MISMO mecanismo.

Prohibiciones implementadas: ningún Invoke, ningún BM_CLICK/WM_SETTEXT, ningún
mouse/teclado sintético, no interacción con modales (sólo se capturan).

Uso:
    python p0a_probe.py --tool texgen   --round 1 --evidence <dir>
    python p0a_probe.py --tool dynodlod --round 2 --evidence <dir>
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

EXE_TEXGEN = r"C:\Modding\DynDOLOD RigTest\TexGenx64.exe"
EXE_DYNDOLOD = r"C:\Modding\DynDOLOD RigTest\DynDOLODx64.exe"
RIG_DATA = r"C:\Modding\DynDOLOD RigTest\_rig_test\data"
RIG_INI = r"C:\Modding\DynDOLOD RigTest\_rig_test\ini"
RIG_PLUGINS = r"C:\Modding\DynDOLOD RigTest\_rig_test\plugins.txt"
PRESETS_DIR = r"C:\Modding\DynDOLOD RigTest\Edit Scripts\DynDOLOD\Presets"
PRESET_TEXGEN = PRESETS_DIR + r"\DynDOLOD_SSE_TexGen.ini"
PRESET_DYNDOLOD = PRESETS_DIR + r"\DynDOLOD_SSE_Default.ini"

VENTANA_WAIT_SEG = 90
CAP_CONTROLES = 600
UIA_E_ELEMENTNOTAVAILABLE = 0x80040201

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_usuario = os.environ.get("USERNAME", "")


def sanitizar(valor: object) -> object:
    """Sanear paths personales antes de persistir evidencia."""
    if isinstance(valor, str):
        texto = valor.replace("C:\\Users\\" + _usuario, "<USER>") if _usuario else valor
        texto = texto.replace("\\" + _usuario + "\\", "\\<USER>\\") if _usuario else texto
        return texto
    if isinstance(valor, dict):
        return {k: sanitizar(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [sanitizar(v) for v in valor]
    return valor


def sha256_de(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for bloque in iter(lambda: fh.read(65536), b""):
                h.update(bloque)
        return h.hexdigest()
    except OSError:
        return None


def estado_archivos(paths: list[str]) -> dict:
    out = {}
    for p in paths:
        existe = os.path.exists(p)
        entry: dict[str, object] = {"exists": existe}
        if existe:
            try:
                st = os.stat(p)
                entry["size"] = st.st_size
                entry["sha256"] = sha256_de(p)
            except OSError as e:
                entry["stat_error"] = repr(e)
        out[p] = entry
    return out


def outputpath_de_preset(path: str) -> str | None:
    """Lee OutputPath del preset SIN modificarlo (sólo regex, read-only)."""
    try:
        texto = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^OutputPath\s*=\s*(.+)$", texto, re.MULTILINE)
    return m.group(1).strip() if m else None


def verificar_root_temporal(root: pathlib.Path) -> dict:
    """§5: canonical, existencia, born-empty, sin symlink/junction/reparse (raíz y ancestros)."""
    root.mkdir(parents=True, exist_ok=True)
    info: dict[str, object] = {"path": str(root)}
    info["canonical"] = str(root.resolve())
    info["exists"] = root.exists()
    try:
        info["born_empty"] = next(root.iterdir(), None) is None
    except OSError as e:
        info["born_empty_error"] = repr(e)
    reparse_chain = []
    actual = root
    while True:
        try:
            st = os.lstat(actual)
            attrs = getattr(st, "st_file_attributes", 0)
            tag = getattr(st, "st_reparse_tag", 0)
            reparse_chain.append(
                {
                    "path": str(actual),
                    "is_reparse": bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT),
                    "reparse_tag": hex(tag) if tag else None,
                }
            )
        except OSError as e:
            reparse_chain.append({"path": str(actual), "lstat_error": repr(e)})
        parent = actual.parent
        if parent == actual:
            break
        actual = parent
    info["ancestros_reparse"] = reparse_chain
    info["sin_reparse_en_cadena"] = not any(e.get("is_reparse") for e in reparse_chain)
    return info


class UiAdapter:
    """Adaptador COM de una sola hebra, espejo de los idiomas del backend read-only."""

    def __init__(self) -> None:
        import comtypes
        import comtypes.client

        self._ct = comtypes
        # Los fallos que ESTE adaptador puede tener sin ser un bug suyo.
        # Se define UNA vez en __init__ (antes de cualquier polling que lo
        # consulte): un `except` que evaluara este atributo sin haberlo
        # definido lanzaría AttributeError DESDE el handler y abortaría el
        # polling loop, que es justo lo que la sonda debe sobrevivir.
        # Espejo del `ObservadorUIAWindows._errores_del_rig` del backend
        # productivo (misma enumeración: COMError + OSError).
        self._errores_del_rig: tuple[type[BaseException], ...] = (comtypes.COMError, OSError)
        comtypes.CoInitialize()
        try:
            self._mod = comtypes.client.GetModule("UIAutomationCore.dll")
            self._uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                interface=self._mod.IUIAutomation,
            )
        except (comtypes.COMError, OSError, AttributeError):
            # Fallo en la inicialización COM: ownership del apartamento
            comtypes.CoUninitialize()
            raise
        self.stale_count = 0
        self.otros_errores = 0

    def liberar(self) -> None:
        self._uia = None
        self._mod = None
        self._ct.CoUninitialize()

    def prop(self, el: object, nombre: str) -> object:
        return el.GetCurrentPropertyValue(self._mod.__dict__[nombre])

    def _txt(self, el: object, nombre: str) -> str:
        v = self.prop(el, nombre)
        return "" if v is None else str(v)

    def ventanas_de_proceso(self, pid: int) -> list[object]:
        raiz = self._uia.GetRootElement()
        cond = self._uia.CreatePropertyCondition(self._mod.UIA_ProcessIdPropertyId, pid)
        col = raiz.FindAll(self._mod.TreeScope_Children, cond)
        return [col.GetElement(i) for i in range(col.Length)]

    def describir_ventana(self, el: object) -> dict:
        hwnd = self.prop(el, "UIA_NativeWindowHandlePropertyId")
        d = {
            "pid": self.prop(el, "UIA_ProcessIdPropertyId"),
            "hwnd": hwnd,
            "name": self._txt(el, "UIA_NamePropertyId"),
            "class_name": self._txt(el, "UIA_ClassNamePropertyId"),
            "control_type": self._control_type(el),
            "automation_id": self._txt(el, "UIA_AutomationIdPropertyId"),
            "framework_id": self._txt(el, "UIA_FrameworkIdPropertyId"),
            "is_enabled": self.prop(el, "UIA_IsEnabledPropertyId"),
            "is_offscreen": self.prop(el, "UIA_IsOffscreenPropertyId"),
        }
        d["owner_hwnd"] = _owner_hwnd(hwnd) if hwnd else None
        wp = self._patron(el, "UIA_WindowPatternId", self._mod.IUIAutomationWindowPattern)
        if wp is not None:
            try:
                d["window_is_modal"] = bool(wp.CurrentIsModal)
                d["window_can_maximize"] = bool(wp.CurrentCanMaximize)
            except self._ct.COMError as e:
                d["window_pattern_error"] = _comerr(e)
        else:
            d["window_is_modal"] = None
        return d

    def _control_type(self, el: object) -> str:
        crudo = self.prop(el, "UIA_ControlTypePropertyId")
        nombres = {
            50000: "Button",
            50001: "Calendar",
            50002: "CheckBox",
            50003: "ComboBox",
            50004: "Edit",
            50005: "Hyperlink",
            50006: "Image",
            50007: "ListItem",
            50008: "List",
            50009: "Menu",
            50010: "MenuBar",
            50011: "MenuItem",
            50012: "ProgressBar",
            50013: "RadioButton",
            50014: "ScrollBar",
            50015: "Slider",
            50016: "Spinner",
            50017: "StatusBar",
            50018: "Tab",
            50019: "TabItem",
            50020: "Text",
            50021: "Thumb",
            50022: "TitleBar",
            50023: "ToolBar",
            50024: "ToolTip",
            50025: "Tree",
            50026: "TreeItem",
            50027: "Custom",
            50028: "Group",
            50029: "Pane",
            50030: "Document",
            50031: "SplitButton",
            50032: "Window",
            50033: "Pane2Fallback",
        }
        try:
            n = int(crudo)
        except (TypeError, ValueError):
            return str(crudo)
        return nombres.get(n, str(n))

    def _patron(self, el: object, nombre_id: str, interfaz: object) -> object | None:
        crudo = None
        try:
            crudo = el.GetCurrentPattern(self._mod.__dict__[nombre_id])
        except self._ct.COMError:
            return None
        except (ValueError, OSError):
            # comtypes puede lanzar "NULL COM pointer access" al hacer
            # QueryInterface sobre un puntero vacío (patrón no soportado por el
            # provider): se trata como patrón ausente, no como error de rig.
            return None
        if crudo is None:
            return None
        try:
            return crudo.QueryInterface(interfaz)
        except (self._ct.COMError, ValueError, OSError):
            return None

    def controles_de_ventana(self, el: object) -> tuple[list[dict], int, int]:
        """(controles descritos [capados], total, ilegibles). Volcado con cap §11."""
        cond = self._uia.CreateTrueCondition()
        cache = self._uia.CreateCacheRequest()
        for nombre in (
            "UIA_ProcessIdPropertyId",
            "UIA_AutomationIdPropertyId",
            "UIA_NamePropertyId",
            "UIA_ControlTypePropertyId",
            "UIA_ClassNamePropertyId",
        ):
            cache.AddProperty(self._mod.__dict__[nombre])
        cache.TreeScope = self._mod.TreeScope_Element
        cache.AutomationElementMode = self._mod.AutomationElementMode_Full
        col = el.FindAllBuildCache(self._mod.TreeScope_Descendants, cond, cache)
        total = col.Length
        n = min(total, CAP_CONTROLES)
        descritos: list[dict] = []
        ilegibles = 0
        for i in range(n):
            try:
                sub = col.GetElement(i)
                descritos.append(
                    {
                        "runtime_id": [int(x) for x in sub.GetRuntimeId()],
                        "control_type": self._control_type(sub),
                        "class_name": self._txt(sub, "UIA_ClassNamePropertyId"),
                        "automation_id": self._txt(sub, "UIA_AutomationIdPropertyId"),
                        "name": self._txt(sub, "UIA_NamePropertyId"),
                        "native_hwnd": self.prop(sub, "UIA_NativeWindowHandlePropertyId"),
                        "is_enabled": self.prop(sub, "UIA_IsEnabledPropertyId"),
                        "is_offscreen": self.prop(sub, "UIA_IsOffscreenPropertyId"),
                        "is_keyboard_focusable": self.prop(sub, "UIA_IsKeyboardFocusablePropertyId"),
                        "patterns": self._patterns_de(sub),
                    }
                )
            except self._ct.COMError as e:
                ilegibles += 1
                if e.hresult == UIA_E_ELEMENTNOTAVAILABLE:
                    self.stale_count += 1
                else:
                    self.otros_errores += 1
        return descritos, total, ilegibles

    def _patterns_de(self, el: object) -> list[str]:
        nombres = {
            "UIA_InvokePatternId": "Invoke",
            "UIA_ValuePatternId": "Value",
            "UIA_TextPatternId": "Text",
            "UIA_LegacyIAccessiblePatternId": "LegacyIAccessible",
            "UIA_SelectionPatternId": "Selection",
            "UIA_SelectionItemPatternId": "SelectionItem",
            "UIA_TogglePatternId": "Toggle",
            "UIA_ExpandCollapsePatternId": "ExpandCollapse",
            "UIA_WindowPatternId": "Window",
            "UIA_RangeValuePatternId": "RangeValue",
        }
        disponibles = []
        for pid_id, nombre in nombres.items():
            try:
                if el.GetCurrentPattern(self._mod.__dict__[pid_id]) is not None:
                    disponibles.append(nombre)
            except self._ct.COMError:
                pass
        return disponibles

    def lectura_output(self, el: object) -> dict:
        """Lectura por los tres patrones de lectura; sin mutación."""
        out: dict[str, object] = {}
        try:
            vp = self._patron(el, "UIA_ValuePatternId", self._mod.IUIAutomationValuePattern)
            if vp is not None:
                try:
                    out["value_pattern_value"] = vp.CurrentValue
                    out["value_is_readonly"] = bool(vp.CurrentIsReadOnly)
                except self._ct.COMError as e:
                    out["value_pattern_error"] = _comerr(e)
            else:
                out["value_pattern"] = None
            lg = self._patron(el, "UIA_LegacyIAccessiblePatternId", self._mod.IUIAutomationLegacyIAccessiblePattern)
            if lg is not None:
                try:
                    out["legacy_value"] = lg.CurrentValue
                except self._ct.COMError as e:
                    out["legacy_error"] = _comerr(e)
            else:
                out["legacy_pattern"] = None
            tp = self._patron(el, "UIA_TextPatternId", self._mod.IUIAutomationTextPattern)
            if tp is not None:
                try:
                    doc = tp.DocumentRange
                    out["text_pattern_text"] = doc.GetText(512)
                except (self._ct.COMError, ValueError, OSError) as e:
                    out["text_pattern_error"] = repr(e)
            else:
                out["text_pattern"] = None
        except (self._ct.COMError, ValueError, OSError) as e:
            out["lectura_error"] = repr(e)
        return out

    def set_value(self, el: object, mecanismo: str, valor: str) -> dict:
        """Experimento §13/§14. Un único mecanismo; registra HRESULT exacto."""
        resultado: dict[str, object] = {"mechanism": mecanismo}
        try:
            if mecanismo == "ValuePattern":
                vp = self._patron(el, "UIA_ValuePatternId", self._mod.IUIAutomationValuePattern)
                if vp is None:
                    resultado["error"] = "PATRON_NO_DISPONIBLE"
                    return resultado
                vp.SetValue(valor)
            elif mecanismo == "LegacyIAccessible":
                lg = self._patron(el, "UIA_LegacyIAccessiblePatternId", self._mod.IUIAutomationLegacyIAccessiblePattern)
                if lg is None:
                    resultado["error"] = "PATRON_NO_DISPONIBLE"
                    return resultado
                lg.SetValue(valor)
            else:
                resultado["error"] = "MECANISMO_DESCONOCIDO"
                return resultado
            resultado["hresult"] = "S_OK"
        except self._ct.COMError as e:
            resultado["hresult"] = hex(e.hresult)
            resultado["exception_type"] = type(e).__name__
            resultado["exception_repr"] = repr(e)
        return resultado


def journal_de_restore(
    evi: pathlib.Path,
    tool: str,
    *,
    pid: int,
    exe_sha: str,
    fingerprint: dict,
    original_value: object,
    intended_value: str,
    estado: str,
    errores: dict | None = None,
) -> pathlib.Path:
    """CR-3: journal durable del estado del Output ANTES de cualquier mutación.

    Se persiste ANTES de mutar (RESTORE_PENDING) y se re-persiste al final
    (RESTORED / RESTORE_FAILED): si el probe muere a mitad de la mutación, el
    journal queda en disco con el valor original y el intended, de modo que el
    wizard mutado es recuperable y la pérdida no es silenciosa.
    """
    registro = {
        "tool": tool,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pid": pid,
        "exe_sha256": exe_sha,
        "control_fingerprint": {
            k: fingerprint.get(k)
            for k in (
                "runtime_id",
                "control_type",
                "class_name",
                "automation_id",
                "name",
                "native_hwnd",
            )
        },
        "original_output_value": original_value,
        "intended_temporary_value": intended_value,
        "state": estado,
    }
    if errores:
        registro["errores"] = errores
    destino = evi / f"{tool}_output_restore_journal.json"
    destino.write_text(
        json.dumps(sanitizar(registro), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return destino


def _comerr(e: Exception) -> dict:
    return {
        "hresult": hex(getattr(e, "hresult", -1)),
        "text": str(getattr(e, "text", "")),
        "repr": repr(e),
    }


def _owner_hwnd(hwnd: int) -> int | None:
    try:
        import ctypes.wintypes  # noqa: F401 -- registra los tipos

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gw_owner = 4
        owner = user32.GetWindow(ctypes.wintypes.HWND(hwnd), gw_owner)
        return owner or None
    except (OSError, AttributeError, ImportError):
        # user32/wintypes pueden no estar disponibles fuera de Windows;
        # el owner es evidencia opcional, nunca un gate.
        return None


def fingerprint_de_modal(m: dict) -> dict:
    """Fingerprint canonical de un modal §16: nombre, clase y botones por AutomationId."""
    return {
        "class_name": m.get("class_name") or "#32770",
        "name": m.get("name"),
        "main_instruction": (m.get("texto_visible") or [""])[0] if m.get("texto_visible") else None,
        "botones": [{"name": b.get("name"), "automation_id": b.get("automation_id")} for b in (m.get("botones") or [])],
    }


def modales_equivalentes(a: dict, b: dict) -> bool:
    """Igualdad inequívoca: clase + instrucción principal + botones (nombre y AutomationId)."""
    return a.get("name") == b.get("name") and a.get("botones") == b.get("botones")


def pids_de_imagen(nombre: str) -> list[tuple[int, str]]:
    """PIDs vivos de una imagen (ps no intrusivo). Devuelve (pid, exe_path)."""
    out = []
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if p.info["name"] and p.info["name"].lower() == nombre.lower():
                    out.append((p.info["pid"], p.info["exe"] or ""))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {nombre}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for linea in r.stdout.splitlines():
            partes = [x.strip('"') for x in linea.split('","')]
            if len(partes) >= 2 and partes[0].lower() == nombre.lower():
                out.append((int(partes[1]), ""))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", choices=["texgen", "dynodlod"], required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--modal-wait-secs", type=int, default=300, help="espera máxima a resolución humana de un modal §16"
    )
    args = parser.parse_args()

    exe = EXE_TEXGEN if args.tool == "texgen" else EXE_DYNDOLOD
    imagen = pathlib.Path(exe).name
    preset = PRESET_TEXGEN if args.tool == "texgen" else PRESET_DYNDOLOD

    sesion = uuid.uuid4().hex[:8]
    base = pathlib.Path(os.environ["TEMP"]) / f"SkyClaw-P0-{sesion}"
    root_out = base / ("TexGen" if args.tool == "texgen" else "DynDOLOD")
    root_test = base / "OutputTest"
    root_temp = base / "temp"

    evi = pathlib.Path(args.evidence)
    evi.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out_path = evi / f"{args.tool}_round{args.round}_{stamp}.json"

    registro: dict[str, object] = {
        "tool": args.tool,
        "exe": exe,
        "round": args.round,
        "session": sesion,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "temp_roots": {},
        "preset_hashes": {},
        "steps": [],
    }

    def paso(etapa: str, datos: dict) -> None:
        registro["steps"].append({"etapa": etapa, "t": time.time(), **datos})

    # -- §5: raíces temporales born-empty -------------------------------------
    registro["temp_roots"]["output_root"] = verificar_root_temporal(root_out)
    registro["temp_roots"]["temp_root"] = verificar_root_temporal(root_temp)
    paso("temp_roots", {"output_test_will_be": str(root_test)})

    # -- estado previo de procesos --------------------------------------------
    registro["pre_existing_processes"] = [{"pid": p, "exe": e} for p, e in pids_de_imagen(imagen)]

    # -- §9: hashes de preset ANTES -------------------------------------------
    registro["preset_hashes"]["antes_de_launch"] = estado_archivos([preset, PRESET_TEXGEN, PRESET_DYNDOLOD])
    registro["preset_outputpath_antes"] = {
        "texgen_preset": outputpath_de_preset(PRESET_TEXGEN),
        "dyndolod_preset": outputpath_de_preset(PRESET_DYNDOLOD),
    }

    argv = [
        exe,
        "-sse",
        f"-o:{root_out}\\",
        f"-d:{RIG_DATA}\\",
        f"-m:{RIG_INI}\\",
        f"-p:{RIG_PLUGINS}",
        f"-t:{root_temp}\\",
    ]
    registro["argv"] = argv

    # -- spawn sin shell ------------------------------------------------------
    proc = subprocess.Popen(argv, cwd=str(pathlib.Path(exe).parent))
    registro["pid"] = proc.pid
    paso("spawn", {"pid": proc.pid})

    adaptador = UiAdapter()
    ventanas_finales: list[dict] = []
    modales_capturados: list[dict] = []
    output_seleccion: dict[str, object] = {}
    experimentos: dict[str, object] = {}

    try:
        # -- §10: esperar ventana top-level del PID ---------------------------
        # Dos fases: (1) primera ventana, (2) ventana contractual (con TEdit).
        # El formulario "TexGen Options" / wizard de DynDOLOD aparece segundos
        # después del TfrmMain inicial.
        deadline = time.time() + VENTANA_WAIT_SEG
        ventanas: list[object] = []
        while time.time() < deadline:
            try:
                ventanas = adaptador.ventanas_de_proceso(proc.pid)
            except adaptador._errores_del_rig + (ValueError, AttributeError):
                # enumeración transitoria mientras el proceso arranca
                ventanas = []
            if ventanas:
                break
            if proc.poll() is not None:
                break
            time.sleep(1.0)
        registro["window_wait_seconds"] = round(VENTANA_WAIT_SEG - (deadline - time.time()), 1)

        # fase 2: ventana contractual con TEdit (polling acotado).
        # §16: si aparece un modal (#32770) se CAPTURA, no se interactúa, y se
        # espera la intervención humana (el agente no pulsa Ignore).
        if ventanas and proc.poll() is None:
            modales_vistos: dict[int, dict] = {}
            contractual_deadline = time.time() + 45 + args.modal_wait_secs
            corte_espera_modal = time.time() + args.modal_wait_secs
            while time.time() < contractual_deadline:
                hay_tedit = False
                try:
                    ventanas = adaptador.ventanas_de_proceso(proc.pid)
                    for w in ventanas:
                        conts, _total, _ileg = adaptador.controles_de_ventana(w)
                        for c in conts:
                            if c.get("class_name") == "TEdit":
                                hay_tedit = True
                            elif c.get("class_name") == "#32770":
                                hwnd_m = c.get("native_hwnd")
                                if hwnd_m and hwnd_m not in modales_vistos:
                                    modales_vistos[hwnd_m] = {
                                        "captured_at": time.strftime("%H:%M:%S"),
                                        "hwnd": hwnd_m,
                                        "name": c.get("name"),
                                        "class_name": "#32770",
                                        "botones": [
                                            {
                                                "name": b.get("name"),
                                                "class_name": b.get("class_name"),
                                                "automation_id": b.get("automation_id"),
                                                "is_enabled": b.get("is_enabled"),
                                            }
                                            for b in conts
                                            if b.get("control_type") == "Button"
                                        ],
                                        "texto_visible": sorted(
                                            {
                                                x["name"]
                                                for x in conts
                                                if x.get("name")
                                                and x.get("control_type") in ("Text", "Pane2Fallback", "Custom")
                                                and len(x.get("name", "")) > 3
                                            }
                                        )[:12],
                                    }
                                    print(f"MODAL_CAPTURADO (no interactuar): {c.get('name')}")
                except adaptador._errores_del_rig + (ValueError, AttributeError):
                    # la enumeración durante un modal puede fallar; se reintenta
                    pass
                registro["modales_durante_espera"] = list(modales_vistos.values())
                if hay_tedit:
                    break
                if modales_vistos and time.time() > corte_espera_modal:
                    registro["error"] = "MODAL_BLOQUEANTE_SIN_RESOLUCION_HUMANA"
                    break
                time.sleep(1.5)
            registro["contractual_window_wait_seconds"] = round(
                45 + args.modal_wait_secs - (contractual_deadline - time.time()), 1
            )

        if not ventanas:
            registro["error"] = "NO_WINDOW_OBSERVED"
            registro["exit_code"] = proc.poll()
        else:
            # -- §9: hashes tras launch ---------------------------------------
            registro["preset_hashes"]["despues_de_launch"] = estado_archivos([preset, PRESET_TEXGEN, PRESET_DYNDOLOD])

            # §16: cualquier ventana adicional (no principal) se captura, no se toca
            for w in ventanas:
                d = adaptador.describir_ventana(w)
                d["controles"], d["controles_total"], d["controles_ilegibles"] = adaptador.controles_de_ventana(w)
                ventanas_finales.append(d)

            # ventana contractual: la que contiene TEdits (Output)
            con_tedit = [w for w in ventanas_finales if any(c.get("class_name") == "TEdit" for c in w["controles"])]
            registro["ventanas_con_tedit"] = [
                {"hwnd": w["hwnd"], "name": w["name"], "class_name": w["class_name"]} for w in con_tedit
            ]

            # modales: ventanas distintas de la contractual con owner en el proceso
            if len(ventanas_finales) > 1:
                principal_hwnd = con_tedit[0]["hwnd"] if con_tedit else None
                for w in ventanas_finales:
                    if w["hwnd"] != principal_hwnd:
                        modales_capturados.append(
                            {
                                k: w[k]
                                for k in (
                                    "pid",
                                    "hwnd",
                                    "name",
                                    "class_name",
                                    "control_type",
                                    "automation_id",
                                    "window_is_modal",
                                    "owner_hwnd",
                                    "is_enabled",
                                )
                            }
                        )

            # -- §12: selector del Output TEdit --------------------------------
            candidatos: list[dict] = []
            if con_tedit:
                for c in con_tedit[0]["controles"]:
                    if c.get("class_name") == "TEdit" and c.get("control_type") == "Edit":
                        candidatos.append(c)
            output_seleccion = {
                "candidatos": len(candidatos),
                "estado": "OK" if len(candidatos) == 1 else "OUTPUT_SELECTOR_AMBIGUOUS",
                "candidato": candidatos[0] if len(candidatos) == 1 else None,
            }
            paso("output_selector", {k: v for k, v in output_seleccion.items() if k != "candidato"})

            if len(candidatos) == 1:
                # re-descubrimiento anclado a la ventana CONTRACTUAL (la que
                # contiene el TEdit), no a ventanas[0]
                el = None
                hwnd_contratual = con_tedit[0].get("hwnd") if con_tedit else None
                ventana_fuente = None
                for w in ventanas:
                    try:
                        if adaptador.prop(w, "UIA_NativeWindowHandlePropertyId") == hwnd_contratual:
                            ventana_fuente = w
                            break
                    except adaptador._ct.COMError:
                        continue
                if ventana_fuente is not None:
                    for sub in _todos_los_descendientes(adaptador, ventana_fuente):
                        try:
                            rid = [int(x) for x in sub.GetRuntimeId()]
                        except adaptador._ct.COMError:
                            continue
                        if rid == candidatos[0]["runtime_id"]:
                            el = sub
                            break

                if el is None:
                    output_seleccion["estado"] = "OUTPUT_SELECTOR_STALE_EN_REDESCUBRIMIENTO"
                    registro["stale_observed"] = True
                else:
                    original = adaptador.lectura_output(el)
                    paso("output_lectura_original", original)
                    orig_val = original.get("value_pattern_value")
                    if orig_val is None:
                        orig_val = original.get("legacy_value") or ""

                    # -- CR-3: journal durable ANTES de cualquier mutación -----
                    sha_exe = sha256_de(exe) or "desconocido"
                    journal = journal_de_restore(
                        evi,
                        args.tool,
                        pid=proc.pid,
                        exe_sha=sha_exe,
                        fingerprint=candidatos[0],
                        original_value=orig_val,
                        intended_value=str(root_test),
                        estado="RESTORE_PENDING",
                    )
                    paso("restore_journal_persistido", {"archivo": str(journal), "estado": "RESTORE_PENDING"})

                    errores_experimento: dict[str, object] = {}
                    try:
                        # -- §13: experimento ValuePattern ---------------------
                        vp = adaptador.set_value(el, "ValuePattern", str(root_test))
                        rb = adaptador.lectura_output(el)
                        vp["readback"] = {
                            "value_pattern_value": rb.get("value_pattern_value"),
                            "legacy_value": rb.get("legacy_value"),
                        }
                        vp["changed"] = vp.get("hresult") == "S_OK" and rb.get("value_pattern_value") == str(root_test)
                        experimentos["value_pattern"] = vp

                        # -- §14: experimento LegacyIAccessible (independiente) ----
                        lg = adaptador.set_value(el, "LegacyIAccessible", str(root_test))
                        rb3 = adaptador.lectura_output(el)
                        lg["readback"] = {
                            "value_pattern_value": rb3.get("value_pattern_value"),
                            "legacy_value": rb3.get("legacy_value"),
                        }
                        lg["changed"] = lg.get("hresult") == "S_OK" and (
                            rb3.get("value_pattern_value") == str(root_test)
                            or rb3.get("legacy_value") == str(root_test)
                        )
                        experimentos["legacy_iaccessible"] = lg
                    except adaptador._errores_del_rig + (ValueError, AttributeError) as exc_mut:
                        # fallo DURANTE la mutación: se registra, y el finally de
                        # abajo intenta el restore igual — el wizard no queda mutado.
                        errores_experimento["mutation_error"] = repr(exc_mut)
                        print(f"ERROR_DURANTE_MUTACION: {exc_mut!r}")
                    finally:
                        # -- restore crash-safe: corre SIEMPRE, incluso si la
                        # mutación o el probe murieron a mitad. Sin fallback
                        # dinámico: el restore usa el mecanismo MEDIDO de este
                        # build (LegacyIAccessible) — el único que escribe en
                        # estos binarios; si falla, RESTORE_FAILED queda durable.
                        restaurar = adaptador.set_value(el, "LegacyIAccessible", str(orig_val))
                        rb2 = adaptador.lectura_output(el)
                        restaurar["readback"] = {
                            "value_pattern_value": rb2.get("value_pattern_value"),
                            "legacy_value": rb2.get("legacy_value"),
                        }
                        restaurado_ok = (
                            rb2.get("value_pattern_value") == orig_val or rb2.get("legacy_value") == orig_val
                        )
                        restaurar["restored_ok"] = restaurado_ok
                        experimentos["restore"] = restaurar
                        # CR-3: el estado final del journal se persiste SIEMPRE
                        journal_de_restore(
                            evi,
                            args.tool,
                            pid=proc.pid,
                            exe_sha=sha_exe,
                            fingerprint=candidatos[0],
                            original_value=orig_val,
                            intended_value=str(root_test),
                            estado="RESTORED" if restaurado_ok else "RESTORE_FAILED",
                            errores=errores_experimento or None,
                        )
                        if not restaurado_ok:
                            registro["restore_failed"] = True
                            print("OUTPUT_RESTORE_FAILED — wizard mutado, journal persistido")

                    registro["preset_hashes"]["despues_de_mutaciones"] = estado_archivos(
                        [preset, PRESET_TEXGEN, PRESET_DYNDOLOD]
                    )

            # -- §15: controles de acción — SOLO inventario --------------------
            nombres_objetivo = (
                {"Start", "Exit TexGen"}
                if args.tool == "texgen"
                else {"Advanced", "OK", "Begin", "Save and Exit", "Exit DynDOLOD"}
            )
            acciones = []
            if con_tedit:
                for c in con_tedit[0]["controles"]:
                    if c.get("control_type") == "Button" and (c.get("name") in nombres_objetivo):
                        acciones.append(
                            {
                                k: c.get(k)
                                for k in (
                                    "control_type",
                                    "class_name",
                                    "automation_id",
                                    "name",
                                    "native_hwnd",
                                    "runtime_id",
                                    "is_enabled",
                                    "patterns",
                                )
                            }
                        )
            registro["action_controls_inventory"] = acciones
            # botones totales (para no depender de nombres esperados)
            registro["todos_los_botones"] = [
                {
                    "name": c.get("name"),
                    "class_name": c.get("class_name"),
                    "automation_id": c.get("automation_id"),
                    "is_enabled": c.get("is_enabled"),
                }
                for c in (con_tedit[0]["controles"] if con_tedit else [])
                if c.get("control_type") == "Button"
            ]

            # -- §17: texto visible completo (modal/warnings fingerprints) -----
            registro["texto_visible"] = sorted(
                {
                    c["name"]
                    for c in (con_tedit[0]["controles"] if con_tedit else [])
                    if c.get("name") and c.get("control_type") in ("Text", "Group")
                }
            )
    finally:
        registro["modales_capturados"] = modales_capturados
        registro.setdefault("modales_durante_espera", [])
        registro["ventanas"] = ventanas_finales
        registro["output_seleccion"] = output_seleccion
        registro["experimentos"] = experimentos
        registro["stale_observations"] = {
            "uia_elementnotavailable_count": adaptador.stale_count,
            "otros_com_errors_count": adaptador.otros_errores,
            "stale_observed": adaptador.stale_count > 0,
        }
        # -- §29: cleanup — terminar SOLO el proceso propio -------------------
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired) as e:
                with contextlib.suppress(OSError):
                    proc.kill()
                registro["terminate_error"] = repr(e)
        time.sleep(1.0)
        registro["residual_processes"] = [{"pid": p, "exe": e} for p, e in pids_de_imagen(imagen)]
        registro["preset_hashes"]["final"] = estado_archivos([preset, PRESET_TEXGEN, PRESET_DYNDOLOD])
        registro["exit_code"] = proc.poll()
        registro["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        adaptador.liberar()

    out_path.write_text(json.dumps(sanitizar(registro), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"EVIDENCIA: {out_path}")
    if registro.get("restore_failed"):
        print("OUTPUT_RESTORE_FAILED — STOP")
        return 2
    if registro.get("error"):
        return 1
    return 0


def _todos_los_descendientes(adaptador: UiAdapter, ventana: object) -> list[object]:
    """Re-descubrimiento desde la ventana (§19, diagnóstico)."""
    try:
        cond = adaptador._uia.CreateTrueCondition()
        col = ventana.FindAll(adaptador._mod.TreeScope_Descendants, cond)
        return [col.GetElement(i) for i in range(col.Length)]
    except adaptador._ct.COMError:
        return []


if __name__ == "__main__":
    sys.exit(main())
