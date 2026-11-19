"""Sonda v3: QSettings 6.7.x contra el INI retenido con FILE_SHARE_NONE.

Cada escenario corre en un PROCESO NUEVO (Qt cachea QConfFile por proceso: medir
desde el proceso que autoró el archivo mediría el caché, no el disco).

Uso: python probe_qsettings.py <python_con_sky_claw> <run_dir>
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time

from harness_validacion import HarnessError, exigir_codigo, exigir_resultado, leer_json

PROBES = pathlib.Path(__file__).resolve().parent
_HIJOS: list[dict] = []
#: Se completan en :func:`configurar`; así el módulo se puede importar desde los
#: tests sin depender de ``sys.argv`` ni de PyQt6.
PY: str = ""
RUN: pathlib.Path = pathlib.Path()
INSTANCIA: pathlib.Path = pathlib.Path("instance")
INI: pathlib.Path = INSTANCIA / "modorganizer.ini"

CAMPOS = ("title", "binary", "arguments", "workingDirectory")

EJECUTABLES = [
    {
        "title": "SKSE",
        "binary": "G:/Lab_Compat/FrozenClones/clone-lab-01/skse64_loader.exe",
        "arguments": '-p "Golden Master"',
        "workingDirectory": "G:/Lab_Compat/FrozenClones/clone-lab-01",
        "steamAppID": "",
        "hide": "false",
        "ownicon": "true",
        "toolbar": "false",
    },
    {
        "title": "SSEEdit",
        "binary": "G:/Lab_Compat/Tools/SSEEdit/SSEEdit.exe",
        "arguments": "-IKnowWhatImDoing -quickautoclean",
        "workingDirectory": "G:/Lab_Compat/Tools/SSEEdit",
        "steamAppID": "",
        "hide": "false",
        "ownicon": "true",
        "toolbar": "false",
    },
    {
        "title": "Pandora",
        "binary": "G:/Lab_Compat/Tools/Pandora/Pandora.exe",
        "arguments": "",
        "workingDirectory": "G:/Lab_Compat/Tools/Pandora",
        "steamAppID": "",
        "hide": "false",
        "ownicon": "true",
        "toolbar": "false",
    },
]

GAMEPATH = None  # los define ``autorar_ini`` con PyQt6 ya importado
PERFIL = None


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def autorar_ini() -> None:
    from PyQt6.QtCore import QByteArray, QSettings

    gamepath = QByteArray(b"G:\\\\Lab_Compat\\\\Skyrim_Runtime_1.6.1170")
    perfil = QByteArray(b"Golden Master")
    INSTANCIA.mkdir(parents=True, exist_ok=True)
    INI.unlink(missing_ok=True)
    qs = QSettings(str(INI), QSettings.Format.IniFormat)
    qs.setValue("gameName", "Skyrim Special Edition")
    qs.setValue("selected_profile", perfil)
    qs.setValue("gamePath", gamepath)
    qs.setValue("game_edition", "Steam")
    qs.setValue("version", "2.5.2")
    qs.setValue("first_start", "false")
    qs.setValue("iniFiles/Skyrim.ini", "G:/Lab_Compat/Skyrim.ini")
    qs.beginWriteArray("customExecutables", len(EJECUTABLES))
    # Índice CERO: ``setArrayIndex`` es 0-based y escribe ``1\`` en el archivo.
    # Con ``start=1`` se escribían ``2\..4\`` con ``size=3``: el primer elemento
    # salía vacío y el último se perdía. Lo encontró Codex sobre #965 y está
    # anotado en el Anexo B del informe.
    for indice, exe in enumerate(EJECUTABLES, start=0):
        qs.setArrayIndex(indice)
        for campo, valor in exe.items():
            qs.setValue(campo, valor)
    qs.endArray()
    qs.setValue("Settings/language", "en")
    qs.setValue("recentDirectories/0", "\\\\NAS-SINTETICO\\share\\Data")
    qs.sync()
    del qs


def texto() -> str:
    return INI.read_text(encoding="utf-8", errors="replace")


class Tenedor:
    def __init__(self, etiqueta: str) -> None:
        self.etiqueta = etiqueta
        self.ready = RUN / f"holder-{etiqueta}.ready"
        self.release = RUN / f"holder-{etiqueta}.release"
        self.released = RUN / f"holder-{etiqueta}.released"
        for ruta in (self.ready, self.release, self.released):
            ruta.unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [PY, str(PROBES / "holder.py"), str(INI), str(self.ready), str(self.release), str(self.released)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def esperar_ready(self, limite_s: float = 60.0) -> dict[str, object]:
        limite = time.monotonic() + limite_s
        while time.monotonic() < limite:
            if self.ready.exists():
                return json.loads(self.ready.read_text(encoding="utf-8"))
            if self.proc.poll() is not None:
                break
            time.sleep(0.01)
        raise RuntimeError(f"el tenedor no quedó listo: rc={self.proc.poll()}")

    def liberar(self, limite_s: float = 60.0) -> None:
        """Libera el handle y CONFIRMA que la liberación fue efectiva.

        No alcanza con escribir ``release``: hay que verificar que el tenedor
        terminó con 0, que dejó su constancia, y que la exclusión ya se puede
        volver a tomar. Sin eso, «liberado» sería una suposición.
        """
        self.release.write_text("go", encoding="utf-8")
        limite = time.monotonic() + limite_s
        while time.monotonic() < limite and not self.released.exists():
            time.sleep(0.01)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise HarnessError(f"tenedor {self.etiqueta}: no terminó al liberarlo") from None
        exigir_codigo(self.proc, f"tenedor {self.etiqueta}", "no liberó el handle")
        if not self.released.exists():
            raise HarnessError(f"tenedor {self.etiqueta}: no dejó constancia de la liberación")
        estado = run_probe("acquirer.py", str(INI))
        exigir_resultado(f"liberacion/{self.etiqueta}", estado.get("autoriza") is True, estado)


def hijo(modo: str, etiqueta: str, *extra: str, esperar_ready: float = 60.0) -> dict[str, object]:
    salida = RUN / f"{etiqueta}.json"
    salida.unlink(missing_ok=True)
    ready = RUN / f"{etiqueta}.ready"
    release = RUN / f"{etiqueta}.release"
    args = [str(sys.executable), str(PROBES / "qsettings_child.py"), str(INI), modo, str(salida)]
    if extra or modo in {"deferred", "dtor", "hold"}:
        args += [str(ready), str(release)]
    args += list(extra)
    # Un rerun en el MISMO run_dir heredaría los archivos de coordinación de la
    # corrida anterior: ``esperar_archivo`` volvería al instante y el hijo no
    # esperaría el checkpoint. Se limpian antes de lanzarlo (el tenedor ya lo
    # hace con los suyos).
    ready.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    h = {"proc": proc, "salida": salida, "ready": ready, "release": release, "stderr": proc.stderr}
    _HIJOS.append(h)
    return h


def _matar_hijos() -> None:
    """Limpieza: un escenario que falla no deja clientes vivos."""
    for h in _HIJOS:
        proc = h["proc"]
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    _HIJOS.clear()


def esperar_archivo(path: pathlib.Path, limite_s: float = 60.0) -> bool:
    limite = time.monotonic() + limite_s
    while time.monotonic() < limite:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def exigir_checkpoint(h: dict, etiqueta: str) -> None:
    """Falla si el cliente no llegó a su checkpoint.

    Sin esto, un cliente muerto o colgado dejaría que el escenario libere la
    exclusión y registre un resultado que no mide lo que dice medir.
    """
    if esperar_archivo(h["ready"]):
        return
    proc = h["proc"]
    proc.kill()
    _, err = proc.communicate(timeout=10)
    raise RuntimeError(f"el cliente {etiqueta} no llegó a su checkpoint: {err}")


def exigir_tendedor(diagnostico: dict[str, object], etiqueta: str) -> None:
    if not diagnostico.get("autoriza"):
        raise RuntimeError(f"el tenedor {etiqueta} no tomó la exclusión: {diagnostico}")


def exigir_exito(h: dict, etiqueta: str) -> dict[str, object]:
    """Espera al cliente, exige ``rc == 0`` y devuelve su JSON.

    Un ``AccessError`` de QSettings es un VALOR esperado del escenario; lo que no
    se acepta es que el proceso haya muerto, que no haya escrito su JSON o que el
    JSON venga truncado.
    """
    proc = h["proc"]
    try:
        _, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=10)
        raise HarnessError(f"{etiqueta}: el cliente no terminó en 120 s") from None
    exigir_codigo(proc, f"cliente {etiqueta}", (err or "").strip()[:300])
    return leer_json(h["salida"], f"cliente {etiqueta}")


def run_probe(name: str, *args: str) -> dict[str, object]:
    proc = subprocess.run([PY, str(PROBES / name), *args], capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise HarnessError(f"{name}: rc={proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    for linea in reversed((proc.stdout or "").splitlines()):
        if linea.strip().startswith("{"):
            return json.loads(linea)
    raise HarnessError(f"{name}: sin salida JSON: {(proc.stdout or '').strip()[:300]}")


def semantica_esperada() -> list[dict[str, object]]:
    return [{campo: exe[campo] for campo in CAMPOS} for exe in EJECUTABLES]


def _qt_version() -> tuple[str, str]:
    """``(runtime, build)`` de Qt, o ``("no-disponible", …)`` si no hay PyQt6.

    Los tests del repo importan este módulo sin PyQt6; la medición real lo exige
    y falla más adelante, con un error claro, en ``autorar_ini``.
    """
    try:
        from PyQt6.QtCore import QT_VERSION_STR, qVersion
    except ModuleNotFoundError:
        return "no-disponible", "no-disponible"
    return qVersion(), QT_VERSION_STR


def medir() -> dict[str, object]:
    runtime_qt, build_qt = _qt_version()
    RUN.mkdir(parents=True, exist_ok=True)
    resultados: dict[str, object] = {"qt_runtime": runtime_qt, "qt_build": build_qt, "ini": str(INI)}

    autorar_ini()
    shutil.copyfile(INI, RUN / "ini_canonico.ini")
    sha0 = sha256(INI)
    resultados["K0_ini_canonico"] = {
        "sha": sha0,
        "tamano": INI.stat().st_size,
        "texto": texto(),
        "n_ejecutables": len(EJECUTABLES),
    }

    # A) lectura con el lock retenido, en proceso nuevo
    tenedor = Tenedor("A")
    resultados["A_tenedor"] = tenedor.esperar_ready()
    exigir_tendedor(resultados["A_tenedor"], "A")
    h = hijo("read", "A")
    datos_a = exigir_exito(h, "A")
    resultados["A_lectura_con_lock"] = datos_a
    tenedor.liberar()
    resultados["A_archivo_intacto"] = sha256(INI) == sha0
    exigir_resultado(
        "A/lectura",
        datos_a.get("status_tras_leer") == "AccessError" and datos_a.get("n_claves") == 0,
        datos_a.get("status_tras_leer"),
    )
    exigir_resultado("A/intacto", resultados["A_archivo_intacto"] is True, "el INI cambió con el lock")

    # B) escritura con el lock retenido, en proceso nuevo
    autorar_ini()
    sha0 = sha256(INI)
    tenedor = Tenedor("B")
    resultados["B_tenedor"] = tenedor.esperar_ready()
    exigir_tendedor(resultados["B_tenedor"], "B")
    h = hijo("write", "B")
    datos_b = exigir_exito(h, "B")
    resultados["B_escritura_con_lock"] = datos_b
    tenedor.liberar()
    resultados["B_archivo_intacto"] = sha256(INI) == sha0
    exigir_resultado("B/escritura", datos_b.get("status_tras_sync") == "AccessError", datos_b.get("status_tras_sync"))
    exigir_resultado("B/intacto", resultados["B_archivo_intacto"] is True, "el INI cambió con el lock")

    # C) sync diferido: el proceso sobrevive al lock y sincroniza después
    autorar_ini()
    sha0 = sha256(INI)
    tenedor = Tenedor("C")
    resultados["C_tenedor"] = tenedor.esperar_ready()
    exigir_tendedor(resultados["C_tenedor"], "C")
    h = hijo("deferred", "C")
    exigir_checkpoint(h, "C")
    tenedor.liberar()
    h["release"].write_text("go", encoding="utf-8")  # sin esto el hijo espera su timeout
    deferido = exigir_exito(h, "C")
    resultados["C_sync_diferido"] = deferido
    resultados["C_ready_observado"] = True
    hc2 = hijo("read", "C2")
    fresco = exigir_exito(hc2, "C2")
    resultados["C_lectura_fresca"] = fresco
    resultados["C_semantica"] = {
        "conserva_ejecutables": fresco.get("ejecutables") == semantica_esperada(),
        "gamePath": fresco.get("gamePath"),
        "n_claves": fresco.get("n_claves"),
        "gameName": fresco.get("gameName"),
        "ejecutables": fresco.get("ejecutables"),
    }
    exigir_resultado(
        "C/sync_con_lock", deferido.get("status_sync_con_lock") == "AccessError", deferido.get("status_sync_con_lock")
    )
    exigir_resultado(
        "C/preservacion", resultados["C_semantica"]["conserva_ejecutables"] is True,
        resultados["C_semantica"].get("ejecutables"),
    )

    # D) destructor que sincroniza después de la liberación
    autorar_ini()
    sha0 = sha256(INI)
    tenedor = Tenedor("D")
    resultados["D_tenedor"] = tenedor.esperar_ready()
    exigir_tendedor(resultados["D_tenedor"], "D")
    h = hijo("dtor", "D")
    exigir_checkpoint(h, "D")
    tenedor.liberar()
    h["release"].write_text("go", encoding="utf-8")  # idem: el destructor corre al salir
    exigir_exito(h, "D")
    resultados["D_ready_observado"] = True
    # releer la semántica en un proceso nuevo
    h2 = hijo("read", "D2")
    resultados["D_semantica_tras_destructor"] = exigir_exito(h2, "D2")
    resultados["D_sha_cambio"] = sha256(INI) != sha0
    resultados["D_conserva_ejecutables"] = (
        resultados["D_semantica_tras_destructor"].get("ejecutables") == semantica_esperada()
    )
    exigir_resultado("D/sha", resultados["D_sha_cambio"] is True, "el destructor no escribió")
    exigir_resultado(
        "D/preservacion", resultados["D_conserva_ejecutables"] is True,
        resultados["D_semantica_tras_destructor"].get("ejecutables"),
    )

    # E) objeto QSettings vivo: ¿retiene el archivo?
    autorar_ini()
    h = hijo("hold", "E")
    exigir_checkpoint(h, "E")
    estado_e = run_probe("acquirer.py", str(INI))
    resultados["E_adquisicion_con_objeto_vivo"] = estado_e
    exigir_resultado("E/adquisicion", estado_e.get("autoriza") is True, estado_e)
    h["release"].write_text("go", encoding="utf-8")
    exigir_exito(h, "E")

    # F) efecto de una reescritura sobre comentarios/orden
    resultados["F_reescritura"] = {
        "secciones": [ln.strip() for ln in texto().splitlines() if ln.strip().startswith("[")],
        "conserva_comentarios": ";" in texto(),
    }
    return resultados


def configurar(argv: list[str]) -> None:
    """Fija los globales desde argv. Fuera de ``main`` el módulo es importable."""
    global PY, RUN, INSTANCIA, INI
    if len(argv) < 3:
        raise SystemExit("uso: probe_qsettings.py <python_con_sky_claw> <run_dir>")
    PY = argv[1]
    RUN = pathlib.Path(argv[2]).resolve()
    INSTANCIA = RUN / "instance"
    INI = INSTANCIA / "modorganizer.ini"


#: Escenarios que ``main`` exige antes de publicar evidencia. No alcanza con que
#: ``medir`` no haya levantado: el resultado tiene que estar COMPLETO.
_ESCENARIOS_OBLIGATORIOS = (
    "A_lectura_con_lock",
    "B_escritura_con_lock",
    "C_semantica",
    "D_semantica_tras_destructor",
    "E_adquisicion_con_objeto_vivo",
)


def main() -> int:
    configurar(sys.argv)
    try:
        resultado = medir()
    finally:
        _matar_hijos()
    faltan = [clave for clave in _ESCENARIOS_OBLIGATORIOS if clave not in resultado]
    if faltan:
        raise HarnessError(f"resultado incompleto: faltan {faltan}")
    destino = RUN / "qsettings.json"
    destino.write_text(json.dumps(resultado, indent=2, ensure_ascii=True), encoding="utf-8")
    # Sólo se llega acá si TODOS los escenarios dieron lo esperado: cualquier
    # desvío levanta y el proceso termina sin publicar ok.
    print(json.dumps({"ok": True, "destino": str(destino)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
