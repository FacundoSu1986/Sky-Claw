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

import os
import pathlib
import subprocess
import sys
import time

import pytest

from sky_claw.local.frozen_runtime import mo2_ini_escritura as escritura
from sky_claw.local.frozen_runtime import mo2_ini_exclusion as exclusion
from sky_claw.local.frozen_runtime.binder_recovery import _es_prefijo_rasgado

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
    109.270. Bloquear al lector convierte la lectura rasgada de improbable en
    imposible por construcción.
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
# D — Recuperación de escritura rasgada
# ---------------------------------------------------------------------------


def test_deteccion_de_prefijo_rasgado_truncado() -> None:
    """``nuevo[:n]`` es un estado intermedio de la escritura por handle."""
    previos = b"AAAAAAAAAA"
    nuevo = b"BBBBBBBBBBBBBB"
    assert _es_prefijo_rasgado(previos, nuevo, nuevo[:5]) is True
    assert _es_prefijo_rasgado(previos, nuevo, nuevo[:1]) is True


def test_deteccion_de_prefijo_rasgado_con_cola_vieja() -> None:
    """Si ``SetEndOfFile`` no llegó a correr, sobrevive la cola del contenido viejo."""
    previos = b"AAAAAAAAAA"
    nuevo = b"BBBBBBBBBBBBBB"
    intermedio = nuevo[:4] + previos[4:]
    assert len(intermedio) == len(previos)
    assert _es_prefijo_rasgado(previos, nuevo, intermedio) is True


def test_no_confunde_escritura_ajena_con_prefijo_rasgado() -> None:
    """Una escritura de un tercero NO se clasifica como propia.

    Es la mitad que impide que el rollback pise lo que escribió otro. Si esta
    función devolviera ``True`` para contenido arbitrario, el discriminador de
    ``binder_recovery`` perdería su razón de ser.
    """
    previos = b"AAAAAAAAAA"
    nuevo = b"BBBBBBBBBBBBBB"
    assert _es_prefijo_rasgado(previos, nuevo, b"ZZZZZZZZZZ") is False
    assert _es_prefijo_rasgado(previos, nuevo, b"") is False
    assert _es_prefijo_rasgado(previos, nuevo, nuevo) is False  # escritura completa, no rasgada


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

_ESCRITURAS_PROHIBIDAS = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rmdir",
        "mkdir",
        "touch",
        "rename",
        "replace",
        "chmod",
        "symlink_to",
        "hardlink_to",
    }
)


def test_la_inspeccion_de_p7_no_contiene_operaciones_mutantes() -> None:
    """Ancla por AST: la herramienta de P7 no puede escribir sobre el runtime.

    Mismo patrón que el ancla del paquete ``effective_runtime``: se escanea la
    FUENTE, no se confía en que nadie agregue un ``write_text`` «sólo para el
    informe». La única escritura permitida es la del informe JSON, que el
    llamador pide explícitamente con ``--json-salida`` y que se verifica aparte.
    """
    import ast

    ruta = RAIZ_REPO / "scripts" / "p7_readonly_inspection.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    ofensas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        funcional = nodo.func
        if isinstance(funcional, ast.Attribute):
            nombre = funcional.attr
            if nombre in _ESCRITURAS_PROHIBIDAS:
                # ``json_salida.write_text`` es el informe, no el runtime: se
                # permite sólo si el receptor es exactamente ese parámetro.
                receptor = funcional.value
                if nombre == "write_text" and isinstance(receptor, ast.Attribute) and receptor.attr == "json_salida":
                    continue
                ofensas.append(f"{nombre} (línea {nodo.lineno})")
        elif isinstance(funcional, ast.Name) and funcional.id in {"open"}:
            modos = [a for a in nodo.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any(("w" in m or "a" in m or "+" in m) for m in modos):
                ofensas.append(f"open con modo de escritura (línea {nodo.lineno})")

    assert not ofensas, f"la inspección de P7 contiene operaciones mutantes: {ofensas}"
