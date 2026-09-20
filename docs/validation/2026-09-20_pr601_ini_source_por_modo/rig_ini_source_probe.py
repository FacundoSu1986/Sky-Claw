"""rig_ini_source_probe.py — rig mínimo de la fuente de ``-m:`` por game mode (#601).

No es código productivo ni un test de la suite: es el instrumento que produce la
evidencia auditable del contrato que valida ``DynDOLODConfig``. La pregunta que
contesta es UNA:

    con la carpeta ``<dir>`` declarada en ``-m:``, ¿qué archivo abre el binario
    para cada game mode, y qué pasa si el archivo que busca no está?

Lo que **no** hace: no pulsa Start, no genera LOD, no toca el juego, no escribe
fuera de su directorio de trabajo. Cada corrida lanza el binario con un ``-d:``/
``-o:``/``-t:``/``-p:`` sintéticos, espera la línea de evidencia y mata el árbol
del proceso (el binario es una app GUI: no hay salida a stdout y su sesión queda
abierta hasta que se la cierre).

Escenarios (uno por modo × contenido de la carpeta declarada):

    A  sse     + ``-m:`` con ``Skyrim.ini`` (+``SkyrimPrefs.ini``)
    B  tes5vr  + la MISMA carpeta (``Skyrim.ini``) — ¿VR usa el nombre de SSE?
    C  tes5vr  + ``-m:`` con SOLO ``SkyrimVR.ini``  — ¿el nombre "obvio" de VR?
    D  sse     + ``-m:`` con SOLO ``Skyrim.ini``    — ¿alcanza sin ``SkyrimPrefs.ini``?

Criterio de PASS por escenario: el veredicto sale de las líneas del log del
binario (``Using ini:`` / ``Fatal: Could not find ini`` / ``can not be found``),
comparadas con separadores normalizados (el binario ecoa el separador duplicado
cuando el valor de ``-m:`` termina en ``\\``, y eso no cambia qué archivo abre).

Uso:

    python rig_ini_source_probe.py --exe <DynDOLODx64.exe> [--work <dir>]
                                   [--timeout 30] [--transcript <archivo>]

Salida: transcript legible por humano (stdout y, si se pide, archivo) + códigos de
salida: 0 sólo si TODOS los escenarios dieron el veredicto esperado; 2 si el
entorno no alcanza (exe ausente, no arranca).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import time

#: Log que el binario escribe por modo, relativo a ``<exe.parent>/Logs``.
#: El ``..._log.txt`` se vacía/append al cerrarse la sesión; el ``..._Debug_log.txt``
#: se escribe mientras corre. Se leen los DOS: el de debug da la marca en vivo y el
#: normal es el artefacto final de la sesión cerrada.
LOG_POR_MODO: dict[str, tuple[str, str]] = {
    "sse": ("DynDOLOD_SSE_log.txt", "DynDOLOD_SSE_Debug_log.txt"),
    "tes5vr": ("DynDOLOD_TES5VR_log.txt", "DynDOLOD_TES5VR_Debug_log.txt"),
}

#: Contenido mínimo de una INI de juego: el binario la abre como TMemIniFile, no
#: necesita secciones para la etapa que este rig mide.
INI_MINIMA = "; probe #601\n[General]\n"


class Escenario:
    """Un caso: modo + carpeta declarada + qué líneas del log lo prueban."""

    def __init__(
        self,
        nombre: str,
        switch: str,
        ini_rel: str,
        archivos_ini: tuple[str, ...],
        *,
        espera_ini: str | None,
        exige: tuple[str, ...] = (),
        prohibe_dir_declarada: bool = False,
    ) -> None:
        self.nombre = nombre
        self.switch = switch
        self.ini_rel = pathlib.Path(ini_rel)
        self.archivos_ini = archivos_ini
        #: Nombre del archivo que el escenario espera ver abierto DENTRO de la
        #: carpeta declarada (``None`` = no se espera que la use).
        self.espera_ini = espera_ini
        self.exige = exige
        #: El binario imprime la ruta que VA A INTENTAR aunque después falle. Este
        #: flag exige que ninguna línea ``Using ini:`` viva en la carpeta declarada
        #: (el caso "la carpeta declarada no sirve y el binario busca otra cosa").
        self.prohibe_dir_declarada = prohibe_dir_declarada


ESCENARIOS: tuple[Escenario, ...] = (
    Escenario(
        nombre="A__sse__carpeta-con-Skyrim.ini",
        switch="-sse",
        ini_rel="Skyrim/ini",
        archivos_ini=("Skyrim.ini", "SkyrimPrefs.ini"),
        espera_ini="Skyrim.ini",
    ),
    Escenario(
        nombre="B__tes5vr__misma-carpeta-con-Skyrim.ini",
        switch="-tes5vr",
        ini_rel="Skyrim/ini",
        archivos_ini=("Skyrim.ini", "SkyrimPrefs.ini"),
        espera_ini="Skyrim.ini",
    ),
    Escenario(
        nombre="C__tes5vr__carpeta-con-solo-SkyrimVR.ini",
        switch="-tes5vr",
        ini_rel="SkyrimVR/ini",
        archivos_ini=("SkyrimVR.ini",),
        espera_ini=None,
        exige=("Fatal: Could not find ini", "Skyrim.ini can not be found", "Skyrim VR (TES5VR)"),
        prohibe_dir_declarada=True,
    ),
    Escenario(
        nombre="D__sse__carpeta-con-solo-Skyrim.ini",
        switch="-sse",
        ini_rel="solo_ini/ini",
        archivos_ini=("Skyrim.ini",),
        espera_ini="Skyrim.ini",
    ),
)


def _normalizar(texto: str) -> str:
    """Colapsa los separadores duplicados que el binario ecoa (no cambia el archivo)."""
    while "\\\\" in texto:
        texto = texto.replace("\\\\", "\\")
    return texto


def _redactar(texto: str) -> str:
    """Reemplaza el home del usuario por ``%USERPROFILE%`` en la evidencia."""
    candidatos = {str(pathlib.Path.home()), os.environ.get("USERPROFILE", "")}
    for home in sorted((c for c in candidatos if c), key=len, reverse=True):
        texto = texto.replace(home, "%USERPROFILE%")
    return texto


def _sha256(ruta: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as fh:
        for bloque in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _sanear_archivo(ruta: pathlib.Path) -> None:
    """Reemplaza el home del operador en una COPIA que se va a commitear.

    Se opera sobre bytes decodificados sin partir líneas, así que los ``\\r\\n``
    del log del binario quedan intactos (el artefacto es evidencia byte a byte).
    """
    original = ruta.read_bytes().decode("utf-8", errors="replace")
    saneado = _redactar(original)
    if saneado != original:
        ruta.write_bytes(saneado.encode("utf-8"))


def _preparar_escenario(work: pathlib.Path, esc: Escenario) -> dict[str, pathlib.Path]:
    """Crea el árbol sintético del escenario y devuelve las rutas de sus insumos."""
    base = work / esc.nombre
    if base.exists():
        shutil.rmtree(base)
    data = base / "data"
    temp = base / "temp"
    out = base / "out"
    ini_dir = base / esc.ini_rel
    for carpeta in (data, temp, out, ini_dir):
        carpeta.mkdir(parents=True, exist_ok=True)
    # Un .esm vacío alcanza: el rig mide la fuente de -m:, no el contenido del juego.
    (data / "Skyrim.esm").write_bytes(b"")
    plugins = base / "plugins.txt"
    plugins.write_text("Skyrim.esm\n", encoding="utf-8")
    for nombre in esc.archivos_ini:
        (ini_dir / nombre).write_text(INI_MINIMA, encoding="utf-8")
    return {"base": base, "data": data, "temp": temp, "out": out, "ini_dir": ini_dir, "plugins": plugins}


def _argv(esc: Escenario, insumos: dict[str, pathlib.Path]) -> list[str]:
    """Vector mínimo que el binario entiende (mismo formato que emite Sky-Claw).

    Directorios con ``\\`` final y SIN comillas: son ELEMENTOS de argv, no una
    línea de comandos (ver ``DynDOLODRunner._switch_de_ruta``).
    """
    return [
        esc.switch,
        f"-o:{insumos['out']}\\",
        f"-d:{insumos['data']}\\",
        f"-m:{insumos['ini_dir']}\\",
        f"-p:{insumos['plugins']}",
        f"-t:{insumos['temp']}\\",
    ]


def _leer_logs(rutas: tuple[pathlib.Path, ...]) -> str:
    """Une los logs sin duplicar líneas (el debug repite las del log normal).

    Se lee primero el log normal de la sesión y después el de debug; una línea que
    ya apareció exacta no se repite. Las sesiones viejas del mismo archivo sí se
    conservan (cada escenario borra los logs antes de arrancar, para atribuirlos).
    """
    lineas: list[str] = []
    vistas: set[str] = set()
    for ruta in rutas:
        if not ruta.is_file():
            continue
        for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines():
            if linea not in vistas:
                vistas.add(linea)
                lineas.append(linea)
    return "\n".join(lineas)


def _ventanas_del_pid(pid: int) -> list[int]:
    """Handles de las ventanas de nivel superior del proceso (sólo Windows)."""
    if sys.platform != "win32":
        return []
    import ctypes  # noqa: PLC0415 — dependencia de plataforma, local al helper
    from ctypes import wintypes  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        propietario = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(propietario))
        if propietario.value == pid:
            handles.append(hwnd)
        return True

    user32.EnumWindows(_callback, 0)
    return handles


def _cerrar_suave(pid: int) -> int:
    """``WM_CLOSE`` a las ventanas del proceso: el binario VUELCA su log al cerrar.

    Es lo que hace auditable la evidencia: ``taskkill /F`` mata sin vaciar el log
    (medido en la primera corrida de este rig: los cuatro escenarios quedaron sin
    líneas, con el log borrado al empezar). Devuelve cuántos ``PostMessageW``
    devolvieron distinto de cero.
    """
    if sys.platform != "win32":
        return 0
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    wm_close = 0x0010
    enviadas = 0
    for hwnd in _ventanas_del_pid(pid):
        if user32.PostMessageW(wintypes.HWND(hwnd), wm_close, 0, 0):
            enviadas += 1
    return enviadas


def _matar_arbol(pid: int) -> None:
    """Cierre FORZADO, sólo como fallback del cierre suave.

    POSIX: el hijo se lanza con ``start_new_session=True``, así que SU grupo de
    procesos es el suyo y ``killpg`` no puede alcanzar al rig ni al shell que lo
    lanzó. El ``getpgid(pid) == pid`` es la verificación de esa precondición: si
    por lo que fuera el hijo no lidera su grupo, se mata SÓLO ese pid (nunca el
    grupo heredado).
    """
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603, S607 — taskkill es un builtin de Windows
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
            timeout=30,
        )
    else:
        import contextlib  # noqa: PLC0415 — sólo POSIX
        import signal  # noqa: PLC0415 — sólo POSIX

        with contextlib.suppress(OSError):
            grupo = os.getpgid(pid)
            if grupo == pid:
                os.killpg(grupo, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)


def _esperar_marca(rutas: tuple[pathlib.Path, ...], marcas: tuple[str, ...], *, timeout: float) -> str:
    """Espera a que alguno de los logs contenga alguna marca; devuelve el texto.

    El ``..._Debug_log.txt`` se escribe mientras el proceso corre, así que es la
    fuente en vivo; el ``..._log.txt`` aparece al cerrar la sesión. Si el timeout
    vence sin marca, se devuelve lo que haya y el veredicto lo decide el caller.
    """
    limite = time.monotonic() + timeout
    while True:
        texto = _leer_logs(rutas)
        if any(marca in texto for marca in marcas):
            return texto
        if time.monotonic() >= limite:
            return texto
        time.sleep(0.5)


def _lineas_relevantes(texto: str) -> list[str]:
    """Líneas del log que deciden el veredicto, en el orden en que aparecieron."""
    claves = ("Using ini:", "Fatal: Could not find ini", "can not be found", "starting session")
    return [linea for linea in texto.splitlines() if any(clave in linea for clave in claves)]


def correr_escenario(
    esc: Escenario,
    *,
    exe: pathlib.Path,
    work: pathlib.Path,
    timeout: float,
    gracia: float,
) -> tuple[bool, list[str]]:
    """Ejecuta un escenario y devuelve ``(pasó, líneas del transcript)``."""
    insumos = _preparar_escenario(work, esc)
    rutas_log = tuple(exe.parent / "Logs" / nombre for nombre in LOG_POR_MODO[esc.switch.lstrip("-")])
    for ruta in rutas_log:
        if ruta.is_file():
            ruta.unlink()  # atribución: lo que se lea es SÓLO de esta corrida
    argv = _argv(esc, insumos)
    salida = [
        f"=== {esc.nombre} ===",
        f"argv              : {_redactar(subprocess.list2cmdline([str(exe), *argv]))}",
        f"carpeta -m:       : {_redactar(str(insumos['ini_dir']))}",
        f"archivos en -m:   : {', '.join(esc.archivos_ini)}",
    ]
    if not exe.is_file():
        return False, [*salida, f"ERROR: no existe el ejecutable {exe}"]
    proc = subprocess.Popen(  # noqa: S603 — el exe y el argv son de este rig
        [str(exe), *argv],
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # POSIX: sesión propia para que el kill de fallback no alcance al rig
        # (ver `_matar_arbol`). En Windows el cierre es por WM_CLOSE/taskkill.
        start_new_session=sys.platform != "win32",
    )
    texto = ""
    cerrado = False
    try:
        texto = _esperar_marca(rutas_log, ("Using ini:", "Fatal: Could not find ini"), timeout=timeout)
        # Gracia: deja que la sesión avance (loader, diálogo de error) antes de cerrar.
        time.sleep(gracia)
        enviadas = _cerrar_suave(proc.pid)
        salida.append(f"cierre suave      : {enviadas} WM_CLOSE enviados")
        try:
            proc.wait(timeout=20)
            cerrado = True
        except subprocess.TimeoutExpired:
            salida.append("ADVERTENCIA: no cerró con WM_CLOSE; se fuerza el árbol")
    finally:
        if not cerrado:
            _matar_arbol(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                salida.append("ADVERTENCIA: el proceso no cerró ni tras taskkill")
    # El log normal se vacía al cerrar la sesión: se releen los DOS después del cierre.
    texto = _leer_logs(rutas_log)
    # Copia cruda por escenario: el log del binario se pisa en cada corrida, así que
    # sin esta copia el artefacto de A/B se perdería al correr C/D. La copia se
    # SANEA (home → %USERPROFILE%, mismo criterio que el transcript) porque se
    # commitea: el sha256 que publica el transcript es el del artefacto saneado.
    copias: list[str] = []
    for ruta in rutas_log:
        if ruta.is_file():
            # ``log-crudo`` y no ``logs``: el .gitignore del repo excluye cualquier
            # carpeta llamada ``logs/`` y la evidencia tiene que poder commitearse.
            destino = insumos["base"] / "log-crudo" / ruta.name
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ruta, destino)
            _sanear_archivo(destino)
            copias.append(f"{_redactar(str(destino))} (sha256 {_sha256(destino)})")
    lineas = _lineas_relevantes(texto)
    salida.append(f"logs              : {', '.join(_redactar(str(r)) for r in rutas_log)}")
    if copias:
        salida.append("copias crudas     :")
        salida.extend(f"  {copia}" for copia in copias)
    salida.append("líneas relevantes :")
    if lineas:
        salida.extend(f"  {_redactar(linea)}" for linea in lineas)
    else:
        salida.append("  (ninguna)")

    normalizado = _normalizar(texto)
    fallas: list[str] = []
    lineas_ini = [linea for linea in normalizado.splitlines() if "Using ini:" in linea]
    if esc.espera_ini is not None:
        esperado = _normalizar(f"{insumos['ini_dir']}\\{esc.espera_ini}")
        if not any(esperado in linea for linea in lineas_ini):
            fallas.append(f"no se abrió {esperado!r}; líneas 'Using ini:' vistas: {lineas_ini}")
    if esc.prohibe_dir_declarada:
        prefijo = _normalizar(str(insumos["ini_dir"]))
        usadas = [linea for linea in lineas_ini if prefijo in linea]
        if usadas:
            fallas.append(f"usó la carpeta declarada cuando no debía: {usadas}")
        if lineas_ini and not all(linea.rstrip().endswith("\\Skyrim.ini") for linea in lineas_ini):
            fallas.append(f"alguna línea 'Using ini:' no termina en Skyrim.ini: {lineas_ini}")
    for exigido in esc.exige:
        if exigido not in normalizado:
            fallas.append(f"falta la línea exigida {exigido!r}")
    salida.append(f"veredicto         : {'PASS' if not fallas else 'FAIL'}")
    salida.extend(f"  - {falla}" for falla in fallas)
    return (not fallas), salida


def main() -> int:
    parser = argparse.ArgumentParser(description="Rig mínimo de la fuente de -m: por game mode (#601).")
    parser.add_argument(
        "--exe", required=True, type=pathlib.Path, help="Ruta a DynDOLODx64.exe (Alpha-209 en la evidencia)."
    )
    parser.add_argument("--work", type=pathlib.Path, default=pathlib.Path.cwd() / "_rig601_work")
    parser.add_argument("--timeout", type=float, default=30.0, help="Segundos máximos por escenario.")
    parser.add_argument(
        "--gracia",
        type=float,
        default=3.0,
        help="Segundos entre la marca y el cierre suave (deja avanzar la sesión).",
    )
    parser.add_argument("--transcript", type=pathlib.Path, default=None, help="Archivo donde copiar el transcript.")
    args = parser.parse_args()

    exe = args.exe.resolve()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    lineas: list[str] = [
        "rig_ini_source_probe.py — evidencia de la fuente de -m: por game mode (#601)",
        f"fecha local        : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"plataforma         : {sys.platform} / Python {sys.version.split()[0]}",
        f"ejecutable         : {_redactar(str(exe))}",
        f"sha256 ejecutable  : {_sha256(exe) if exe.is_file() else '<ausente>'}",
        f"work dir           : {_redactar(str(work))}",
        "",
    ]
    resultados: list[tuple[str, bool]] = []
    for esc in ESCENARIOS:
        paso, bloque = correr_escenario(esc, exe=exe, work=work, timeout=args.timeout, gracia=args.gracia)
        lineas.extend(bloque)
        lineas.append("")
        resultados.append((esc.nombre, paso))

    lineas.append("=== resumen ===")
    lineas.extend(f"{'PASS' if paso else 'FAIL'}  {nombre}" for nombre, paso in resultados)
    transcript = "\n".join(lineas) + "\n"
    # Estabiliza el transcript: el work dir es efímero de cada máquina; los
    # artefactos crudos de cada escenario viajan en `artifacts/` (copia del rig).
    transcript = transcript.replace(_redactar(str(work)), "<RIG_WORK>")
    print(transcript, end="")
    if args.transcript is not None:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(transcript, encoding="utf-8")
    return 0 if all(paso for _, paso in resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
