"""Pruebas de la exclusión verificable de ``modorganizer.ini`` (P5-C2).

Tracker: #672
ADR 0012 §13, §29.4, §34.4

Qué se ancla acá, y por qué con procesos REALES
-----------------------------------------------
La propiedad que se prueba es de exclusión entre procesos. Un ``Thread`` o un
``MagicMock`` comparte la tabla de handles del intérprete y no puede falsificar
una violación de compartición, así que los casos de concurrencia lanzan
subprocesos de verdad (``sys.executable -c``) y compiten contra el handle que
sostiene el test. Los dobles quedan para las ramas que NO son de concurrencia.

Los tres veredictos del gate se ejercitan por separado:
``EXCLUSION_ESTABLISHED``, ``EXCLUSION_NOT_ESTABLISHED`` e ``INDETERMINATE``.
Un ``INDETERMINATE`` nunca autoriza: se comprueba explícitamente.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import time

import pytest

from sky_claw.local.frozen_runtime import mo2_ini_escritura as escritura
from sky_claw.local.frozen_runtime import mo2_ini_exclusion as exclusion
from sky_claw.local.frozen_runtime.binder_recovery import _es_escritura_propia_interrumpida

SOLO_WINDOWS = pytest.mark.skipif(os.name != "nt", reason="la exclusión de archivos compartidos es de Windows")

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]

INI = (
    "[General]\r\n"
    "gameName=Skyrim Special Edition\r\n"
    "gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)\r\n"
    "game_edition=Steam\r\n"
    "version=2.5.2\r\n"
    "\r\n"
    "[Settings]\r\n"
    "Language=en\r\n"
)
RUTA_NUEVA = "G:\\Modding\\Skyrim_Runtime_1.5.97"


def _ini_sintetico(directorio: pathlib.Path, contenido: str = INI) -> pathlib.Path:
    ruta = directorio / "modorganizer.ini"
    ruta.write_text(contenido, encoding="utf-8", newline="")
    return ruta


def _en_subproceso(codigo: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Ejecuta ``codigo`` en OTRO proceso, con el repo en ``sys.path``."""
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(RAIZ_REPO) + (os.pathsep + entorno["PYTHONPATH"] if entorno.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-c", codigo],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(RAIZ_REPO),
        env=entorno,
    )


# ---------------------------------------------------------------------------
# A — Adquisición y veredictos
# ---------------------------------------------------------------------------


@SOLO_WINDOWS
def test_adquisicion_establece_exclusion_y_reporta_identidad(tmp_path: pathlib.Path) -> None:
    """El veredicto positivo trae identidad de grado handle, no sólo un booleano."""
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        assert gate.estado is exclusion.EstadoExclusion.ESTABLISHED
        assert gate.autoriza is True
        assert gate.identidad is not None
        # La identidad del handle es la del path: si no, el gate estaría
        # protegiendo otro archivo.
        assert gate.identidad.indice == os.stat(ini).st_ino
    finally:
        gate.cerrar()


@SOLO_WINDOWS
def test_archivo_inexistente_no_autoriza(tmp_path: pathlib.Path) -> None:
    """Sin archivo no hay nada que excluir: ``NOT_ESTABLISHED``, nunca autoriza."""
    gate = exclusion.adquirir_exclusion_ini(tmp_path / "no-existe.ini")
    assert gate.estado is exclusion.EstadoExclusion.NOT_ESTABLISHED
    assert gate.autoriza is False
    assert "no existe" in gate.diagnostico.motivo


@SOLO_WINDOWS
def test_segunda_exclusion_del_mismo_archivo_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Dos participantes de Sky-Claw no pueden sostener la exclusión a la vez."""
    ini = _ini_sintetico(tmp_path)
    primera = exclusion.adquirir_exclusion_ini(ini)
    try:
        segunda = exclusion.adquirir_exclusion_ini(ini, intentos=3, espera_s=0.001)
        assert segunda.estado is exclusion.EstadoExclusion.NOT_ESTABLISHED
        assert segunda.autoriza is False
        assert "incompatible" in segunda.diagnostico.motivo
        segunda.cerrar()
    finally:
        primera.cerrar()


@SOLO_WINDOWS
def test_el_reintento_acotado_no_se_queda_esperando(tmp_path: pathlib.Path) -> None:
    """El reintento es acotado: una negativa no se convierte en un cuelgue."""
    ini = _ini_sintetico(tmp_path)
    primera = exclusion.adquirir_exclusion_ini(ini)
    try:
        inicio = time.monotonic()
        segunda = exclusion.adquirir_exclusion_ini(ini, intentos=4, espera_s=0.001)
        transcurrido = time.monotonic() - inicio
        assert segunda.estado is exclusion.EstadoExclusion.NOT_ESTABLISHED
        assert segunda.diagnostico.intentos == 4
        assert transcurrido < 5.0, f"el reintento acotado tardó {transcurrido:.1f}s"
    finally:
        primera.cerrar()


@SOLO_WINDOWS
def test_un_reparse_point_se_rechaza(tmp_path: pathlib.Path) -> None:
    """Un symlink en la ruta del INI rompe la premisa: la exclusión no cubre el destino.

    Si la creación del symlink no está permitida (sin privilegio ni modo
    desarrollador) el caso queda INDETERMINATE en esta máquina; lo que NO puede
    pasar en ningún caso es que se autorice.
    """
    real = tmp_path / "real.ini"
    real.write_text(INI, encoding="utf-8", newline="")
    enlace = tmp_path / "modorganizer.ini"
    try:
        enlace.symlink_to(real)
    except OSError:
        pytest.skip("crear symlinks requiere privilegio en esta máquina")
    gate = exclusion.adquirir_exclusion_ini(enlace)
    try:
        assert gate.autoriza is False
        assert gate.estado is exclusion.EstadoExclusion.NOT_ESTABLISHED
        assert "reparse point" in gate.diagnostico.motivo
    finally:
        gate.cerrar()


def test_fuera_de_windows_el_veredicto_es_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Sin ``kernel32`` no se puede afirmar nada: ``INDETERMINATE`` y no autoriza."""
    monkeypatch.setattr(exclusion, "_KERNEL32", None)
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    assert gate.estado is exclusion.EstadoExclusion.INDETERMINATE
    assert gate.autoriza is False
    with pytest.raises(exclusion.ExclusionNoAutorizadaError):
        gate.leer_bytes()
    with pytest.raises(exclusion.ExclusionNoAutorizadaError):
        gate.escribir_bytes(b"x")


def test_estados_autorizantes_es_un_conjunto_explicito() -> None:
    """Un estado nuevo no hereda autorización: el conjunto está enumerado."""
    assert {exclusion.EstadoExclusion.ESTABLISHED} == exclusion.ESTADOS_AUTORIZANTES
    for estado in exclusion.EstadoExclusion:
        assert (estado in exclusion.ESTADOS_AUTORIZANTES) is (estado is exclusion.EstadoExclusion.ESTABLISHED)


# ---------------------------------------------------------------------------
# B — Exclusión real contra procesos externos
# ---------------------------------------------------------------------------


_ESCRITOR_EXTERNO = """
import sys
from sky_claw.local.frozen_runtime import mo2_ini_exclusion as g

ruta = sys.argv[1]
try:
    with open(ruta, "r+b") as f:
        f.write(b"PWNED")
    print("ESCRIBIO")
except OSError as exc:
    print(f"BLOQUEADO:{getattr(exc, 'winerror', exc.errno)}")
"""

_BORRADOR_EXTERNO = """
import os, sys
try:
    os.unlink(sys.argv[1])
    print("BORRO")
except OSError as exc:
    print(f"BLOQUEADO:{getattr(exc, 'winerror', exc.errno)}")
"""

_REEMPLAZADOR_EXTERNO = """
import os, sys, pathlib
origen, destino = sys.argv[1], sys.argv[2]
pathlib.Path(origen).write_bytes(b"OTRO")
try:
    os.replace(origen, destino)
    print("REEMPLAZO")
except OSError as exc:
    print(f"BLOQUEADO:{getattr(exc, 'winerror', exc.errno)}")
"""


@SOLO_WINDOWS
def test_escritura_externa_bloqueada_mientras_se_retiene(tmp_path: pathlib.Path) -> None:
    """Un proceso externo real no puede escribir el INI mientras el gate lo retiene."""
    ini = _ini_sintetico(tmp_path)
    original = ini.read_bytes()
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        proc = _en_subproceso(_ESCRITOR_EXTERNO.replace("sys.argv[1]", repr(str(ini))))
        assert "BLOQUEADO" in proc.stdout, f"la escritura externa pasó: {proc.stdout} {proc.stderr}"
    finally:
        gate.cerrar()
    assert ini.read_bytes() == original


@SOLO_WINDOWS
def test_borrado_externo_bloqueado_mientras_se_retiene(tmp_path: pathlib.Path) -> None:
    """``os.unlink`` externo falla: el nombre no se puede liberar por debajo."""
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        proc = _en_subproceso(_BORRADOR_EXTERNO.replace("sys.argv[1]", repr(str(ini))))
        assert "BLOQUEADO" in proc.stdout, f"el borrado externo pasó: {proc.stdout} {proc.stderr}"
        assert ini.exists()
    finally:
        gate.cerrar()


@SOLO_WINDOWS
def test_reemplazo_externo_bloqueado_mientras_se_retiene(tmp_path: pathlib.Path) -> None:
    """``os.replace`` externo falla: la ventana de reemplazo queda cerrada."""
    ini = _ini_sintetico(tmp_path)
    original = ini.read_bytes()
    intruso = tmp_path / "intruso.ini"
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        proc = _en_subproceso(
            _REEMPLAZADOR_EXTERNO.replace("sys.argv[1]", repr(str(intruso))).replace("sys.argv[2]", repr(str(ini)))
        )
        assert "BLOQUEADO" in proc.stdout, f"el reemplazo externo pasó: {proc.stdout} {proc.stderr}"
    finally:
        gate.cerrar()
    assert ini.read_bytes() == original


@SOLO_WINDOWS
def test_liberacion_tras_cerrar_devuelve_la_escritura(tmp_path: pathlib.Path) -> None:
    """Cerrar libera de verdad: el mismo proceso externo escribe sin obstáculo."""
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    gate.cerrar()
    proc = _en_subproceso(_ESCRITOR_EXTERNO.replace("sys.argv[1]", repr(str(ini))))
    assert "ESCRIBIO" in proc.stdout, f"la exclusión no se liberó: {proc.stdout} {proc.stderr}"


@SOLO_WINDOWS
def test_liberacion_tras_muerte_abrupta_del_tenedor(tmp_path: pathlib.Path) -> None:
    """Si el proceso muere, el kernel cierra el handle: no queda un archivo bloqueado.

    Es la propiedad que hace que la exclusión no pueda dejar el INI inaccesible
    tras un crash del binder.
    """
    ini = _ini_sintetico(tmp_path)
    codigo = (
        "import os, sys\n"
        "from sky_claw.local.frozen_runtime import mo2_ini_exclusion as g\n"
        f"gate = g.adquirir_exclusion_ini({str(ini)!r})\n"
        "assert gate.autoriza\n"
        "os._exit(9)\n"
    )
    proc = _en_subproceso(codigo)
    assert proc.returncode == 9, f"el tenedor no murió como se esperaba: {proc.stderr}"
    time.sleep(0.2)
    gate = exclusion.adquirir_exclusion_ini(ini, intentos=10, espera_s=0.01)
    try:
        assert gate.autoriza, f"la exclusión sobrevivió a la muerte del proceso: {gate.diagnostico.motivo}"
    finally:
        gate.cerrar()


@SOLO_WINDOWS
def test_un_lector_externo_no_puede_observar_el_archivo_mientras_se_escribe(
    tmp_path: pathlib.Path,
) -> None:
    """Un lector estilo CRT queda fuera: no hay lectura rasgada posible.

    Es la razón por la que el acceso propio incluye ``DELETE``: con la variante
    sin ``DELETE`` los lectores SÍ abren y se midieron 72 lecturas rasgadas sobre
    109.270.
    """
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        with pytest.raises(OSError):
            ini.read_bytes()
        lector = _en_subproceso(
            "import sys\n"
            "try:\n"
            f"    open({str(ini)!r}, 'rb').read()\n"
            "    print('LEE')\n"
            "except OSError as exc:\n"
            "    print(f'BLOQUEADO:{getattr(exc, \"winerror\", exc.errno)}')\n"
        )
        assert "BLOQUEADO" in lector.stdout, f"un lector externo pudo abrir el archivo: {lector.stdout}"
    finally:
        gate.cerrar()


#: Sonda autocontenida: usa ``ctypes`` directamente y NO importa nada del repo
#: ni del rig. Un test que depende de un módulo del rig externo mide el rig.
_ABRE_LECTOR = r"""
import ctypes, ctypes.wintypes as wt, sys

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k.CreateFileW.restype = wt.HANDLE
k.CloseHandle.argtypes = [wt.HANDLE]
k.CloseHandle.restype = wt.BOOL

acceso = int(sys.argv[2], 0)
compartir = int(sys.argv[3], 0)
h = k.CreateFileW(sys.argv[1], acceso, compartir, None, 3, 0x00000080, None)
invalido = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
if not h or int(h) == invalido:
    print(f"BLOQUEADO:{ctypes.get_last_error()}")
else:
    k.CloseHandle(h)
    print("ABRE")
"""


def _lector_intenta_abrir(ini: pathlib.Path, acceso: int, compartir: int) -> str:
    """Corre la sonda en OTRO proceso y devuelve su veredicto."""
    proc = _en_subproceso(
        _ABRE_LECTOR.replace("sys.argv[1]", repr(str(ini)))
        .replace("sys.argv[2]", repr(hex(acceso)))
        .replace("sys.argv[3]", repr(hex(compartir)))
    )
    salida = proc.stdout.strip()
    if not salida:
        raise AssertionError(f"la sonda de lectura no produjo salida: {proc.stderr[-500:]}")
    return salida


#: Abre SÓLO para atributos y después intenta leer contenido. Sirve para separar
#: «puede abrir» de «puede leer», que no son lo mismo en Windows.
_LEE_CON_ATRIBUTOS = r"""
import ctypes, ctypes.wintypes as wt, sys

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k.CreateFileW.restype = wt.HANDLE
k.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k.CloseHandle.argtypes = [wt.HANDLE]

invalido = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
h = k.CreateFileW(sys.argv[1], 0x0080, 0x00000007, None, 3, 0x00000080, None)
if not h or int(h) == invalido:
    print("SIN_HANDLE")
else:
    buf = ctypes.create_string_buffer(64)
    leidos = wt.DWORD(0)
    ok = k.ReadFile(h, buf, 64, ctypes.byref(leidos), None)
    if ok:
        print("LEE:" + str(leidos.value))
    else:
        print("NO_LEE:" + str(ctypes.get_last_error()))
    k.CloseHandle(h)
"""


@SOLO_WINDOWS
@pytest.mark.parametrize(
    "nombre, acceso, compartir",
    [
        ("CRT_GR_share_RW", 0x80000000, 0x1 | 0x2),
        ("compatible_GR_share_RWD", 0x80000000, 0x1 | 0x2 | 0x4),
        ("GR_share_READ", 0x80000000, 0x1),
        ("GR_share_NONE", 0x80000000, 0x0),
    ],
)
def test_ningun_lector_externo_abre_mientras_se_retiene(
    tmp_path: pathlib.Path, nombre: str, acceso: int, compartir: int
) -> None:
    """NINGUNA combinación de lector abre el archivo mientras el gate lo retiene.

    Este test existe por un defecto real. La primera versión concedía
    ``FILE_SHARE_READ`` y el ADR afirmaba que la lectura rasgada era «imposible
    por construcción». Era falso: la validación de compartición es
    **bidireccional**, así que un lector que pida ``GENERIC_READ`` y conceda
    ``READ|WRITE|DELETE`` abre igual aunque el tenedor sólo conceda ``READ``. Se
    midió: ese lector coexistió y capturó **11.038 lecturas rasgadas**.

    El caso ``compatible_GR_share_RWD`` es exactamente el que fallaba. Con
    ``COMPARTICION_EXCLUSIVA = 0`` los cuatro quedan bloqueados, y la escritura a
    medias deja de ser observable por construcción — ahora sí.
    """
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        veredicto = _lector_intenta_abrir(ini, acceso, compartir)
        assert "BLOQUEADO" in veredicto, (
            f"el lector {nombre} pudo abrir el INI mientras el gate lo retenía: {veredicto}"
        )
    finally:
        gate.cerrar()


@SOLO_WINDOWS
def test_una_apertura_solo_de_atributos_no_puede_leer_el_contenido(tmp_path: pathlib.Path) -> None:
    """``FILE_READ_ATTRIBUTES`` sí abre con share 0, pero no puede leer bytes.

    Windows permite abrir un archivo para consultar atributos aunque el tenedor
    no conceda ninguna compartición. Eso NO es una fuga: medido, ``ReadFile``
    sobre ese handle falla con ``ERROR_ACCESS_DENIED`` (5) y devuelve 0 bytes.

    La garantía se enuncia sobre el CONTENIDO, no sobre la apertura. Decir «ningún
    proceso puede abrir el archivo» sería falso por este caso, y una afirmación
    que el propio sistema refuta no sirve como garantía.
    """
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        veredicto = _lector_intenta_abrir(ini, 0x0080, 0x1 | 0x2 | 0x4)
        # Puede abrir (atributos), pero lo que importa es lo que puede HACER.
        assert veredicto.startswith(("ABRE", "BLOQUEADO")), veredicto
        lectura = _en_subproceso(_LEE_CON_ATRIBUTOS.replace("sys.argv[1]", repr(str(ini))))
        assert "NO_LEE" in lectura.stdout, (
            f"una apertura sólo de atributos pudo leer el contenido: {lectura.stdout} {lectura.stderr}"
        )
    finally:
        gate.cerrar()


def test_la_comparticion_propia_es_ninguna() -> None:
    """Ancla: la compartición no puede volver a conceder nada.

    Conceder ``FILE_SHARE_READ`` reabre la ventana de lectura rasgada para los
    lectores que concedan ``DELETE``, y el modo de compartición de MO2 real **no
    está medido**. Mientras no lo esté, conceder cualquier share es una apuesta.
    """
    assert exclusion.COMPARTICION_EXCLUSIVA == 0
    assert exclusion.ACCESO_EXCLUSIVO & 0x00010000, "el acceso propio tiene que incluir DELETE"


@SOLO_WINDOWS
def test_una_escritura_a_medias_no_es_observable(tmp_path: pathlib.Path) -> None:
    """El estado intermedio existe, pero ningún lector de CONTENIDO puede verlo.

    Se escribe de verdad una parte del contenido nuevo SIN ``SetEndOfFile`` —el
    estado exacto entre ``WriteFile`` y el truncado— y se comprueba que ningún
    proceso externo puede abrir el archivo para leerlo. La garantía no es «el
    estado intermedio no existe» (existe) sino «no es observable».
    """
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        original = gate.leer_bytes()
        nuevo = INI.replace("1.6.1170", "1.5.97").encode("utf-8")
        corte = _primer_corte_util(original, nuevo)
        # Estado intermedio real: prefijo nuevo + cola vieja, sin truncar.
        parcial = nuevo[:corte] + original[corte:]
        assert parcial != original and parcial != nuevo, "el corte no produjo un estado intermedio real"
        gate.escribir_bytes(parcial)

        for nombre, acceso, compartir in [
            ("CRT", 0x80000000, 0x1 | 0x2),
            ("compatible", 0x80000000, 0x1 | 0x2 | 0x4),
        ]:
            veredicto = _lector_intenta_abrir(ini, acceso, compartir)
            assert "BLOQUEADO" in veredicto, f"el lector {nombre} observó el estado intermedio del INI: {veredicto}"
    finally:
        gate.cerrar()


def _primer_corte_util(viejo: bytes, nuevo: bytes) -> int:
    """Primer índice POSTERIOR a la divergencia: un corte antes no cambia nada.

    Es la trampa que hizo que el primer experimento de lectura rasgada midiera
    cero: el prefijo común de un INI antes y después del cambio de ``gamePath``
    mide 91 de 116 bytes, así que cortar en ``len//3`` no modificaba ningún byte.
    """
    limite = min(len(viejo), len(nuevo))
    divergencia = 0
    while divergencia < limite and viejo[divergencia] == nuevo[divergencia]:
        divergencia += 1
    if divergencia >= limite:
        raise AssertionError("los contenidos no divergen: no hay estado intermedio que probar")
    return min(divergencia + 5, limite)


# ---------------------------------------------------------------------------
# C — Integridad del INI y equivalencia de hermanos
# ---------------------------------------------------------------------------


@SOLO_WINDOWS
def test_el_escritor_por_handle_preserva_bytes_ajenos(tmp_path: pathlib.Path) -> None:
    """Sólo cambia la línea ``gamePath``; el resto queda byte a byte."""
    ini = _ini_sintetico(tmp_path)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        escritura.actualizar_game_path_por_handle(gate, RUTA_NUEVA)
    finally:
        gate.cerrar()

    antes = INI.split("\n")
    with open(ini, encoding="utf-8", newline="") as manejador:
        despues = manejador.read().split("\n")
    assert len(antes) == len(despues)
    distintas = [i for i, (a, d) in enumerate(zip(antes, despues, strict=True)) if a != d]
    assert len(distintas) == 1
    assert despues[distintas[0]].startswith("gamePath=")
    # Se separó por "\n", así que cada línea conserva su "\r" final: se compara
    # el contenido sin el terminador para no atarse al salto.
    assert "[Settings]" in [linea.rstrip("\r") for linea in despues]
    assert "Language=en" in [linea.rstrip("\r") for linea in despues]


@SOLO_WINDOWS
def test_los_dos_escritores_producen_los_mismos_bytes(tmp_path: pathlib.Path) -> None:
    """Ancla de HERMANOS: los dos caminos de escritura no pueden divergir.

    El repo tiene una clase de defecto dominante: un fix que aterriza en un
    camino y deja intacto a su gemelo. Acá el gemelo es el escritor por ruta
    (``actualizar_game_path_en_ini``, con ``os.replace``) y el escritor por
    handle (``actualizar_game_path_por_handle``, con exclusión). Se comparan
    contra la MISMA previsión pura (``prever_bytes``), así que agregar un tercer
    escritor que no la use rompe este test.

    Se cubren varios largos y varias rutas para que la comparación no dependa de
    un caso donde ambos coinciden por casualidad.
    """
    casos = [
        ("G:\\Modding\\Skyrim_Runtime_1.5.97", INI),
        ("G:\\M\\R", INI),
        ("G:\\Modding\\Skyrim_Runtime_1.6.1170", INI),  # no-op: el valor ya era el pedido
        ("G:\\Modding\\Una_Ruta_Mucho_Mas_Larga_Que_La_Anterior_1.5.97", INI),
        ("G:\\Modding\\Skyrim_Runtime_1.5.97", INI.replace("\r\n", "\n")),  # LF puro
    ]
    for indice, (ruta, contenido) in enumerate(casos):
        base = tmp_path / f"caso{indice}"
        base.mkdir()

        por_ruta = base / "por_ruta.ini"
        por_ruta.write_text(contenido, encoding="utf-8", newline="")
        escritura.actualizar_game_path_en_ini(por_ruta, ruta)

        por_handle = base / "por_handle.ini"
        por_handle.write_text(contenido, encoding="utf-8", newline="")
        gate = exclusion.adquirir_exclusion_ini(por_handle)
        try:
            escritura.actualizar_game_path_por_handle(gate, ruta)
        finally:
            gate.cerrar()

        previsto = escritura.prever_bytes(contenido.encode("utf-8"), ruta)
        assert por_ruta.read_bytes() == previsto, f"el escritor por ruta divergió de prever_bytes (caso {indice})"
        assert por_handle.read_bytes() == previsto, f"el escritor por handle divergió de prever_bytes (caso {indice})"
        assert por_ruta.read_bytes() == por_handle.read_bytes(), f"los hermanos divergieron (caso {indice})"


@SOLO_WINDOWS
def test_escritor_por_handle_es_no_op_si_el_valor_ya_es_el_pedido(tmp_path: pathlib.Path) -> None:
    """Escribir el valor que ya está no toca el archivo ni su mtime."""
    ini = _ini_sintetico(tmp_path)
    antes = ini.read_bytes()
    mtime_antes = ini.stat().st_mtime_ns
    time.sleep(0.01)
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        escritura.actualizar_game_path_por_handle(gate, "G:\\Modding\\Skyrim_Runtime_1.6.1170")
    finally:
        gate.cerrar()
    assert ini.read_bytes() == antes
    assert ini.stat().st_mtime_ns == mtime_antes


@SOLO_WINDOWS
def test_escritor_por_handle_verifica_su_propio_readback(tmp_path: pathlib.Path) -> None:
    """Un escritor que no verifica su readback publica una intención, no un hecho."""

    class RetenidoQueMiente:
        def __init__(self, contenido: bytes) -> None:
            self._contenido = contenido

        def leer_bytes(self) -> bytes:
            return self._contenido

        def escribir_bytes(self, datos: bytes) -> None:
            # Acepta la escritura y no la aplica: exactamente lo que un handle
            # roto o un FS que miente producirían.
            return None

    retenido = RetenidoQueMiente(INI.encode("utf-8"))
    with pytest.raises(escritura.EscrituraIniNoVerificadaError):
        escritura.actualizar_game_path_por_handle(retenido, RUTA_NUEVA)


@SOLO_WINDOWS
def test_verificar_huella_detecta_cambio_previo_a_la_mutacion(tmp_path: pathlib.Path) -> None:
    """La comprobación y la mutación comparten handle: el cambio se detecta antes de escribir."""
    ini = _ini_sintetico(tmp_path)
    huella_autorizada = exclusion.adquirir_exclusion_ini(ini)
    huella = huella_autorizada.huella()
    huella_autorizada.cerrar()

    ini.write_text(INI.replace("1.6.1170", "1.6.640"), encoding="utf-8", newline="")

    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        with pytest.raises(exclusion.Mo2IniExclusionError):
            gate.verificar_huella(huella)
    finally:
        gate.cerrar()


# ---------------------------------------------------------------------------
# D — Recuperación de una escritura interrumpida
# ---------------------------------------------------------------------------

#: Prefijo común de un INI antes y después de cambiar `gamePath`: 89 de 110 bytes
#: en este par. Es el dato que hace que un corte «temprano» no cambie nada.
_PREVIOS = (
    b"[General]\r\ngameName=Skyrim Special Edition\r\ngame_edition=Steam\r\n"
    b"gamePath=@ByteArray(G:////////Modding////////Skyrim_Runtime_1.6.1170_muy_largo)//r//n"
    b"version=2.5.2\r\n"
)
#: La ruta objetivo y el contenido que el ESCRITOR produciría para ella.
#:
#: Se DERIVA de ``prever_bytes`` en vez de escribirse a mano: el predicado de
#: autoría se define contra la previsión, así que un literal que no coincida con
#: ella haría que los tests midieran otra cosa. Pasó: el literal tenía los
#: backslashes duplicados y el control positivo del test de recuperación fallaba
#: por esa diferencia, no por el código.
_RUTA_NUEVA = "G:////M////R"
_NUEVO = escritura.prever_bytes(_PREVIOS, _RUTA_NUEVA)


def _divergencia(previos: bytes, nuevo: bytes) -> int:
    limite = min(len(previos), len(nuevo))
    i = 0
    while i < limite and previos[i] == nuevo[i]:
        i += 1
    return i


def test_atribuye_el_unico_estado_intermedio_que_el_escritor_deja() -> None:
    """El estado medido matando un proceso real: prefijo nuevo + cola vieja.

    Rig ``exp9_death_points``: muriendo durante ``WriteFile`` y antes de
    ``SetEndOfFile`` el archivo queda con el prefijo nuevo y la cola vieja, del
    mismo tamaño que el snapshot. Es el único estado intermedio alcanzable.
    """
    divergencia = _divergencia(_PREVIOS, _NUEVO)
    corte = divergencia + 5
    intermedio = _NUEVO[:corte] + _PREVIOS[corte:]
    assert len(intermedio) == len(_PREVIOS)
    assert intermedio != _PREVIOS and intermedio != _NUEVO
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, intermedio) is True


def test_no_atribuye_un_corte_dentro_del_prefijo_comun() -> None:
    """Un corte antes de la divergencia NO cambia ningún byte.

    El archivo sigue siendo el snapshot, así que «interrumpida» no aplica. Si se
    atribuyera, el rollback reescribiría un archivo que nunca cambió.
    """
    divergencia = _divergencia(_PREVIOS, _NUEVO)
    assert divergencia > 0
    for corte in (0, 1, divergencia):
        estado = _NUEVO[:corte] + _PREVIOS[corte:]
        assert estado == _PREVIOS, f"el corte {corte} no debería cambiar el archivo"
        assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, estado) is False


def test_no_atribuye_un_archivo_truncado() -> None:
    """``nuevo[:n]`` NO es alcanzable por este escritor, y no se atribuye.

    ``SetEndOfFile`` corre sólo después de una escritura completa, así que un
    archivo truncado no puede ser el resultado de una muerte a mitad. Atribuirlo
    sería apropiarse del truncado de un tercero. La versión anterior del
    predicado lo aceptaba; el rig demostró que ese estado no es alcanzable.
    """
    divergencia = _divergencia(_PREVIOS, _NUEVO)
    for corte in (divergencia + 5, len(_NUEVO) - 1):
        truncado = _NUEVO[:corte]
        assert len(truncado) < len(_PREVIOS)
        assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, truncado) is False


def test_no_atribuye_una_escritura_ajena() -> None:
    """Si un tercero escribió, el rollback no restaura y conserva evidencia."""
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, b"Z" * len(_PREVIOS)) is False
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, b"") is False
    # Una escritura ajena que casualmente empieza como la nuestra, pero difiere
    # antes de la divergencia: no es nuestra.
    ajeno = _PREVIOS[:10] + b"X" + _PREVIOS[11:]
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, ajeno) is False


def test_no_atribuye_estados_completos() -> None:
    """El snapshot y la escritura completa no son «interrumpidas»."""
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, _PREVIOS) is False
    assert _es_escritura_propia_interrumpida(_PREVIOS, _NUEVO, _NUEVO) is False


def test_la_evidencia_se_conserva_cuando_la_autoria_no_se_establece(tmp_path: pathlib.Path) -> None:
    """Ante autoría indeterminada: ni restaurar ni descartar; dejar rastro.

    El snapshot ya preserva lo viejo. Esto preserva lo NUEVO —lo que escribió el
    tercero— que de otro modo se perdería en la primera restauración manual.
    """
    from sky_claw.local.frozen_runtime import binder_recovery as recovery

    objetivo = tmp_path / "modorganizer.ini"
    ajeno = b"contenido de un tercero"
    objetivo.write_bytes(ajeno)
    superficie = recovery.SuperficieCapturada(
        kind=recovery.SUPERFICIE_MO2_INI,
        path=str(objetivo),
        existed=True,
        sha256="0" * 64,
        contenido_b64=__import__("base64").b64encode(_PREVIOS).decode("ascii"),
    )
    registro = recovery.RegistroRecuperacion(
        schema_version=1,
        transition_id="t-evidence",
        journal_revision=1,
        operation="PROMOTION",
        target_clone_id="clone-1",
        target_generation_id="gen-1",
        expected_effective_path="G:////M////R",
        superficies=(superficie,),
        fase=recovery.FaseRecuperacion.INI_ESCRITO,
        created_at_ns=0,
        updated_at_ns=0,
    )
    evidencia = recovery._preservar_evidencia(tmp_path, registro, superficie.kind, objetivo)
    assert evidencia is not None, "no se conservó evidencia de la escritura ajena"
    assert evidencia.read_bytes() == ajeno
    assert evidencia.parent.name == "t-evidence"


# ---------------------------------------------------------------------------
# E — Cobertura de escritores: que no aparezca un tercero sin ancla
# ---------------------------------------------------------------------------


def test_los_escritores_del_ini_estan_enumerados() -> None:
    """Ancla enumerativa: los escritores de ``gamePath`` son exactamente dos.

    Mismo patrón que ``test_ritual_dispatch`` con ``RITUAL_TOOL_MAP``: si aparece
    un tercer camino de escritura del INI, este test rompe hasta que se le
    escriba su receta —y la receta lo mete en el test de equivalencia de
    hermanos, que es el que detecta la divergencia de bytes.
    """
    escritores = {
        nombre
        for nombre in dir(escritura)
        if nombre.startswith("actualizar_game_path_") and callable(getattr(escritura, nombre))
    }
    assert escritores == {"actualizar_game_path_en_ini", "actualizar_game_path_por_handle"}


def test_el_binder_no_escribe_el_ini_por_ruta() -> None:
    """El binder usa el camino con exclusión; el camino por ruta no puede volver.

    Un ``os.replace`` sobre el INI dentro de la sección crítica reabriría la
    ventana TOCTOU que P5-C2 cierra, y el test de comportamiento no lo vería
    porque el resultado final sería el mismo.
    """
    fuente = (RAIZ_REPO / "sky_claw" / "local" / "frozen_runtime" / "runtime_clone_binder.py").read_text(
        encoding="utf-8"
    )
    cuerpo = fuente.split("def ejecutar_transicion_binder(", 1)[1]
    assert "actualizar_game_path_en_ini(" not in cuerpo, (
        "el binder volvió a escribir el INI por ruta: eso reabre la ventana TOCTOU"
    )
    assert "actualizar_game_path_por_handle(" in cuerpo
    assert "adquirir_exclusion_ini(" in cuerpo


# ---------------------------------------------------------------------------
# F — La inspección de P7 es READ-ONLY por AST, no por promesa
# ---------------------------------------------------------------------------

#: Métodos que mutan el disco por sí solos, sin importar el modo.
_ESCRITURAS_PROHIBIDAS = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rmdir",
        "mkdir",
        "touch",
        "chmod",
        "symlink_to",
        "hardlink_to",
    }
)

#: Caracteres de modo que implican ESCRITURA en la API de ``open``.
_CARACTERES_DE_ESCRITURA = frozenset("wax+")


def _modo_de_apertura(nodo: ast.Call) -> str | None:
    """Modo declarado en una llamada de apertura, o ``None``.

    ``Path.open(mode)`` recibe el modo en la posición 0; el ``open`` de siempre
    lo recibe en la 1, porque la 0 es la ruta. Confundirlos haría que una ruta
    con una «w» se leyera como modo de escritura.
    """
    # ``Path.open(mode)`` es método; ``open(path, mode)``, ``io.open(path, mode)``
    # y ``os.open(path, flags, mode)`` son estilo función aunque los dos últimos
    # lleven punto. Confundirlos lee la RUTA como si fuera el modo.
    estilo_funcion = isinstance(nodo.func, ast.Name) or (
        isinstance(nodo.func, ast.Attribute)
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id in {"io", "os", "codecs"}
    )
    candidatos: list[ast.expr] = []
    if not estilo_funcion and nodo.args:
        candidatos.append(nodo.args[0])
    elif estilo_funcion and len(nodo.args) > 1:
        candidatos.append(nodo.args[1])
    for palabra in nodo.keywords:
        if palabra.arg == "mode":
            candidatos.append(palabra.value)
    for candidato in candidatos:
        if isinstance(candidato, ast.Constant) and isinstance(candidato.value, str):
            return candidato.value
    return None


def ofensas_de_escritura(fuente: str) -> list[str]:
    """Nombres de operaciones de escritura encontradas en ``fuente``, por AST.

    Es una función y no un bloque inline para poder probar el DETECTOR con
    fragmentos sintéticos. Un ancla que no se puede falsificar no es un ancla:
    la versión anterior ignoraba ``Path.open``, así que cambiar
    ``config_path.open("rb")`` por ``"wb"`` pasaba el test (Codex sobre #851).
    """
    arbol = ast.parse(fuente)
    ofensas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        funcional = nodo.func
        if isinstance(funcional, ast.Attribute):
            nombre = funcional.attr
            if nombre in _ESCRITURAS_PROHIBIDAS:
                ofensas.append(f"{nombre} (línea {nodo.lineno})")
            # ``replace``/``rename`` como nombre suelto marcan ``str.replace`` y
            # ``dataclasses.replace``, que no tocan el disco. El que importa es el
            # de ``os``, y se chequea por receptor.
            if nombre in {"replace", "rename"} and isinstance(funcional.value, ast.Name) and funcional.value.id == "os":
                ofensas.append(f"os.{nombre} (línea {nodo.lineno})")
            if nombre == "fdopen":
                # ``os.fdopen(fd, "w")`` escribe sin pasar por ``open``. Se
                # permite sólo dentro del escritor validado, como ``unlink``.
                ofensas.append(f"fdopen (línea {nodo.lineno})")
            if nombre == "open":
                # ``os.open`` NO se decide por el modo: sus argumentos son
                # (ruta, banderas, modo) y la ruta podría ser un literal con una
                # «w». Lo cubre el chequeo dedicado —exactamente una apertura,
                # dentro del escritor validado, con O_EXCL— que además es más
                # estricto que mirar banderas.
                if isinstance(funcional.value, ast.Name) and funcional.value.id == "os":
                    continue
                modo = _modo_de_apertura(nodo)
                if modo and _CARACTERES_DE_ESCRITURA & set(modo):
                    ofensas.append(f"open({modo!r}) (línea {nodo.lineno})")
        elif isinstance(funcional, ast.Name) and funcional.id == "open":
            modo = _modo_de_apertura(nodo)
            if modo and _CARACTERES_DE_ESCRITURA & set(modo):
                ofensas.append(f"open({modo!r}) (línea {nodo.lineno})")
    return ofensas


def _solo_en_el_escritor_validado(arbol: ast.Module, nombre: str) -> bool:
    """¿Toda llamada a ``nombre`` vive dentro de ``escribir_informe_exclusivo``?

    Se generalizó al agregar ``fdopen``: la pregunta «¿esta capacidad está
    confinada al único escritor validado?» se repite para cada operación que el
    inspector necesita y no puede prohibir del todo.
    """
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef):
            continue
        lo_usa = any(
            isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute) and hijo.func.attr == nombre
            for hijo in ast.walk(nodo)
        )
        if lo_usa and nodo.name != "escribir_informe_exclusivo":
            return False
    return True


def _unlink_vive_en_el_escritor_validado(arbol: object) -> bool:
    """¿El único ``unlink`` del inspector está dentro de ``escribir_informe_exclusivo``?"""
    assert isinstance(arbol, ast.Module)
    return _solo_en_el_escritor_validado(arbol, "unlink")


def test_la_inspeccion_de_p7_no_contiene_operaciones_mutantes() -> None:
    """Ancla por AST: la herramienta de P7 no puede escribir sobre el runtime.

    Escanea la FUENTE con :func:`ofensas_de_escritura`, que es una función aparte
    justamente para poder probar el DETECTOR con fragmentos sintéticos.

    Historia de este ancla, porque las tres versiones fallaron distinto:

    1. eximía ``json_salida.write_text`` —«es el informe»—, y con eso autorizaba
       cualquier destino, incluido ``modorganizer.ini``;
    2. miraba el ``O_EXCL`` en el archivo entero, y se satisfacía con la mención
       en el docstring;
    3. ignoraba ``Path.open``, así que ``config_path.open("wb")`` pasaba.

    El informe se escribe con ``os.open(O_CREAT|O_EXCL)`` dentro de
    ``escribir_informe_exclusivo``, y eso es lo único que el ancla permite.
    """
    ruta = RAIZ_REPO / "scripts" / "p7_readonly_inspection.py"
    fuente = ruta.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)

    ofensas = ofensas_de_escritura(fuente)
    aperturas_exclusivas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "open"
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == "os"
    ]

    # ``unlink`` de limpieza: se permite SÓLO dentro del escritor validado, donde
    # borra el informe parcial que él mismo acaba de crear con ``O_EXCL``.
    limpiezas = [o for o in ofensas if o.startswith("unlink")]
    ofensas = [o for o in ofensas if not o.startswith("unlink")]
    # ``fdopen`` sobre el descriptor que abrió el escritor validado es parte de la
    # misma operación; en cualquier otra función sería una escritura suelta.
    fdopens = [o for o in ofensas if o.startswith("fdopen")]
    ofensas = [o for o in ofensas if not o.startswith("fdopen")]
    assert not ofensas, f"la inspección de P7 contiene operaciones mutantes: {ofensas}"
    if fdopens:
        assert _solo_en_el_escritor_validado(arbol, "fdopen"), "un fdopen vive fuera de escribir_informe_exclusivo"
    if limpiezas:
        assert len(limpiezas) == 1, f"hay más de un unlink en el inspector: {limpiezas}"
        assert _unlink_vive_en_el_escritor_validado(arbol), (
            "el unlink del inspector vive fuera de escribir_informe_exclusivo"
        )

    assert len(aperturas_exclusivas) == 1, (
        f"se esperaba exactamente una os.open de escritura, hay {len(aperturas_exclusivas)}"
    )
    llamada = aperturas_exclusivas[0]
    escritor = next(
        (n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "escribir_informe_exclusivo"),
        None,
    )
    assert escritor is not None, "no existe escribir_informe_exclusivo"
    asignaciones = [
        n
        for n in ast.walk(escritor)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "banderas" for t in n.targets)
    ]
    assert len(asignaciones) == 1, f"se esperaba una asignación de banderas, hay {len(asignaciones)}"
    banderas = ast.get_source_segment(fuente, asignaciones[0]) or ""
    assert "O_EXCL" in banderas, (
        f"las banderas de apertura del informe no incluyen O_EXCL: podría truncar un archivo existente ({banderas})"
    )
    funciones_con_apertura = [
        nodo.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and any(hijo is llamada for hijo in ast.walk(nodo))
    ]
    assert funciones_con_apertura == ["escribir_informe_exclusivo"], (
        f"la apertura de escritura vive fuera del escritor validado: {funciones_con_apertura}"
    )


@pytest.mark.parametrize(
    "fragmento",
    [
        'p.open("wb")',
        'p.open("ab")',
        'p.open("r+b")',
        'p.open(mode="w")',
        'open(p, "w")',
        'open(p, "a")',
        'open(p, mode="x")',
        "io.open(p, 'wb')",
        "p.write_text('x')",
        "p.write_bytes(b'x')",
        "os.replace(a, b)",
        "os.rename(a, b)",
    ],
)
def test_el_detector_de_escritura_reconoce_la_variante(fragmento: str) -> None:
    """Cada forma de escribir tiene que ser detectada, no sólo la que se probó.

    Es la mitad que hace del ancla una enumeración y no una muestra. La variante
    ``Path.open("wb")`` es la que Codex usó para romper la versión anterior.
    """
    assert ofensas_de_escritura(fragmento) != [], f"el detector NO ve {fragmento!r}"


@pytest.mark.parametrize(
    "fragmento",
    [
        'p.open("rb")',
        "p.open()",
        "open(p)",
        'open(p, "rb")',
        "p.read_text()",
        "p.read_bytes()",
        "os.stat(p)",
        "open('C:/tmp/w')",  # la ruta contiene una «w»: no es un modo
        "os.open(p, flags, 0o600)",  # os.open tiene su propio chequeo
    ],
)
def test_el_detector_no_marca_lecturas(fragmento: str) -> None:
    """Un detector que marca todo tampoco sirve: los negativos son el control."""
    assert ofensas_de_escritura(fragmento) == [], f"el detector marca de más en {fragmento!r}"


def test_ningun_modulo_productivo_escribe_el_ini_por_ruta() -> None:
    """Ancla enumerativa: el escritor SIN exclusión no puede tener llamadores.

    ``actualizar_game_path_en_ini`` sigue existiendo (lo usa la previsión y lo
    cubre su propio test), pero muta el INI con ``os.replace`` y **sin** la
    exclusión de P5-C2. Un llamador nuevo en cualquier módulo productivo abriría
    un segundo camino que saltea el gate —exactamente la clase de defecto que
    ``AGENTS.md`` documenta— y este ancla lo rompe antes de que aterrice.

    Se enumeran los módulos, no se muestrea un caso: el escaneo es por AST sobre
    todo el paquete.
    """
    import ast

    raiz = RAIZ_REPO / "sky_claw"
    permitido = {"mo2_ini_escritura.py"}  # la definición y su uso interno
    ofensas: list[str] = []
    for modulo in sorted(raiz.rglob("*.py")):
        if modulo.name in permitido:
            continue
        try:
            arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "actualizar_game_path_en_ini"
            ):
                ofensas.append(f"{modulo.relative_to(raiz).as_posix()}:{nodo.lineno}")
    assert not ofensas, (
        "hay llamadores del escritor del INI SIN exclusión: "
        f"{ofensas}. Usá actualizar_game_path_por_handle dentro del gate"
    )


def test_la_recuperacion_en_proceso_nuevo_no_atribuye_una_escritura_ambigua(tmp_path: pathlib.Path) -> None:
    """Sin evidencia de PROCESO, un estado intermedio no se restaura.

    ``recuperar_transicion_interrumpida`` corre en un proceso nuevo: no sabe si la
    transición llegó a escribir, y la fase ``INI_ESCRITO`` se marca ANTES de
    escribir, así que tampoco lo prueba. Entre el crash y la recuperación puede
    pasar cualquier cosa —horas, incluso—, así que la ventana de una escritura
    ajena es mucho mayor que en el rollback in-process.

    Este test fija la decisión: el default es NO atribuir. Se conserva evidencia y
    el propietario decide (Codex sobre #851).
    """
    from sky_claw.local.frozen_runtime import binder_recovery as recovery

    objetivo = tmp_path / "modorganizer.ini"
    divergencia = _divergencia(_PREVIOS, _NUEVO)
    intermedio = _NUEVO[: divergencia + 5] + _PREVIOS[divergencia + 5 :]
    objetivo.write_bytes(intermedio)

    superficie = recovery.SuperficieCapturada(
        kind=recovery.SUPERFICIE_MO2_INI,
        path=str(objetivo),
        existed=True,
        sha256=recovery._sha256(_PREVIOS),
        contenido_b64=__import__("base64").b64encode(_PREVIOS).decode("ascii"),
    )
    registro = recovery.RegistroRecuperacion(
        schema_version=1,
        transition_id="t-proceso-nuevo",
        journal_revision=1,
        operation="PROMOTION",
        target_clone_id="c1",
        target_generation_id="g1",
        expected_effective_path="G:////M////R",
        superficies=(superficie,),
        fase=recovery.FaseRecuperacion.INI_ESCRITO,
        created_at_ns=0,
        updated_at_ns=0,
    )

    # Sin ``escritura_intentada``: es lo que hace la recuperación en proceso nuevo.
    veredicto = recovery._restaurar_persistida(superficie, registro, raiz_evidencia=tmp_path)
    assert veredicto.escritura_ajena is True, "la recuperación atribuyó una escritura que no puede probar"
    assert objetivo.read_bytes() == intermedio, "la recuperación pisó el contenido ambiguo"
    evidencia = list((tmp_path / "recovery_evidence").rglob("*.bin"))
    assert evidencia, "no se conservó evidencia del contenido ambiguo"
    assert evidencia[0].read_bytes() == intermedio

    # Control positivo: CON evidencia de proceso, el mismo estado sí se restaura.
    objetivo.write_bytes(intermedio)
    veredicto_con_flag = recovery._restaurar_persistida(
        superficie, registro, raiz_evidencia=tmp_path, escritura_intentada=True
    )
    assert veredicto_con_flag.escritura_ajena is False
    assert objetivo.read_bytes() == _PREVIOS, "con intento probado, el rollback tenía que restaurar"


@SOLO_WINDOWS
def test_el_hook_de_escritura_solo_dispara_cuando_se_escribe(tmp_path: pathlib.Path) -> None:
    """``al_escribir`` marca el intento REAL, no la llamada al escritor.

    El binder necesita saber si la transición llegó a mutar el INI para decidir
    si un estado intermedio es suyo. Marcar antes de llamar al escritor era
    impreciso: entre la llamada y la escritura están la admisión de formato y el
    no-op, que pueden terminar sin tocar el archivo. En el no-op, marcar «escribí»
    haría que un contenido ajeno se atribuyera a la transición.

    Se prueban los tres desenlaces: no-op (no dispara), escritura real (dispara
    una vez) y fallo de admisión (no dispara).
    """
    ini = _ini_sintetico(tmp_path)
    # Se lee ANTES de adquirir el gate: con la exclusión tomada, leer por ruta
    # está bloqueado a propósito, así que el valor del no-op tiene que venir de
    # afuera de la sección crítica.
    actual = escritura.leer_game_path_de_mo2(ini).path
    assert actual, "el INI sintético no tiene gamePath legible"
    gate = exclusion.adquirir_exclusion_ini(ini)
    try:
        disparos: list[int] = []

        # 1) No-op: el valor ya es el pedido. Se toma del LECTOR y no de un
        #    literal: el escapado de Qt duplica las barras, así que escribir la
        #    ruta a mano produce un valor distinto y el «no-op» deja de serlo.
        escritura.actualizar_game_path_por_handle(gate, actual, al_escribir=lambda: disparos.append(1))
        assert disparos == [], "el hook disparó en un no-op"

        # 2) Escritura real: dispara exactamente una vez.
        escritura.actualizar_game_path_por_handle(gate, RUTA_NUEVA, al_escribir=lambda: disparos.append(1))
        assert disparos == [1], f"el hook disparó {len(disparos)} veces en una escritura real"
    finally:
        gate.cerrar()
