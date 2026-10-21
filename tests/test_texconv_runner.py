"""NP-E0b: ejecutor del ``texconv`` fijado (``sky_claw/local/tools/texconv_runner.py``).

El borde de proceso (``run_capture``) se reemplaza por un ``texconv`` FALSO que lee la entrada que escribió el
ejecutor y escribe una salida BC4 armada con la disposición pública de Microsoft, de forma independiente del módulo.
Es un mock del proceso: prueba los argumentos, la verificación de la copia, la validación de la salida, el mapeo de
errores y la limpieza. NO prueba que el ``texconv`` real haga lo que el falso hace: eso lo cubre el smoke real de
``tests/test_texconv_real.py`` (saltado sin ``SKYCLAW_TEXCONV_EXE``). Ver ``sky_claw/local/tools/AGENTS.md``:
«No afirmar smoke real a partir de un mock».
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import io
import os
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax import export
from sky_claw.local.native_parallax.export import (
    AlturaNoExportableError,
    ExportError,
    TexconvNoCoincideError,
    TexconvNoFijadoError,
    TexconvPin,
    cuantizar_a_8_bits,
    generar_mips,
)
from sky_claw.local.tools import texconv_runner
from sky_claw.local.tools.texconv_runner import (
    ARGUMENTOS_BC4,
    EspacioDeTrabajoError,
    ResultadoDeExportacion,
    SalidaDeTexconvInvalidaError,
    TexconvCodigoDeSalidaError,
    TexconvNoEjecutadoError,
    TexconvRunnerError,
    TexconvTimeoutError,
    TimeoutInvalidoError,
    exportar_altura_bc4,
)

_ORIGEN = "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
_BINARIO = b"binario falso de texconv"
_BLOQUE = bytes([180, 180]) + b"\0" * 6


def _pin_del_binario_falso() -> TexconvPin:
    return TexconvPin("may2026", hashlib.sha256(_BINARIO).hexdigest(), _ORIGEN)


def _nivel_bytes(ancho: int, alto: int) -> int:
    return max(1, (ancho + 3) // 4) * max(1, (alto + 3) // 4) * 8


def _dds_bc4(
    ancho: int,
    alto: int,
    niveles: int,
    *,
    fourcc: bytes = b"BC4U",
    dx10: int | None = None,
    payload_extra: int = 0,
) -> bytes:
    """BC4 armado a mano con la disposición pública de Microsoft (independiente del módulo bajo prueba)."""
    tamanos = [_nivel_bytes(max(1, ancho >> i), max(1, alto >> i)) for i in range(niveles)]
    con_mips = niveles > 1
    banderas = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if con_mips else 0)
    caps = 0x1000 | ((0x8 | 0x400000) if con_mips else 0)
    formato_pixel = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    cabecera = struct.pack("<7I", 124, banderas, alto, ancho, tamanos[0], 0, niveles if con_mips else 0)
    cabecera += b"\0" * 44 + formato_pixel + struct.pack("<5I", caps, 0, 0, 0, 0)
    extension = struct.pack("<5I", dx10, 3, 0, 1, 0) if dx10 is not None else b""
    return b"DDS " + cabecera + extension + _BLOQUE * (sum(tamanos) // 8) + b"\0" * payload_extra


def _leer_entrada_l8(datos: bytes) -> tuple[int, int, int]:
    """(ancho, alto, niveles) de la entrada L8 que escribió el ejecutor; lectura independiente."""
    _, _, alto, ancho, _, _, mips = struct.unpack_from("<7I", datos, 4)
    return ancho, alto, max(1, mips)


class TexconvFalso:
    """Reemplazo de ``run_capture``: registra la llamada y escribe (o no) la salida que se le indique."""

    def __init__(
        self,
        *,
        codigo: int = 0,
        stdout: bytes = b"Microsoft (R) DirectX Texture Converter\n",
        stderr: bytes = b"",
        salida: Callable[[int, int, int], bytes] | None = None,
        escribir: bool = True,
        nombre_de_salida: str | None = None,
        extras: tuple[str, ...] = (),
        excepcion: BaseException | None = None,
    ) -> None:
        self.codigo, self.stdout, self.stderr = codigo, stdout, stderr
        self.salida = salida or (lambda ancho, alto, niveles: _dds_bc4(ancho, alto, niveles))
        self.escribir, self.nombre_de_salida, self.extras, self.excepcion = (
            escribir,
            nombre_de_salida,
            extras,
            excepcion,
        )
        self.llamadas: list[list[str]] = []
        self.plazo: float | None = None
        self.cwd: str | None = None
        self.entrada: bytes = b""
        self.binario_ejecutado: bytes = b""

    async def __call__(self, args: list[str], *, timeout: float, cwd: str | None = None) -> tuple[bytes, bytes, int]:
        self.llamadas.append(list(args))
        self.plazo, self.cwd = timeout, cwd
        if self.excepcion is not None:
            raise self.excepcion
        directorio_salida = Path(args[args.index("-o") + 1])
        entrada = Path(args[-1])
        self.entrada = entrada.read_bytes()
        self.binario_ejecutado = Path(args[0]).read_bytes()
        if self.escribir:
            ancho, alto, niveles = _leer_entrada_l8(self.entrada)
            destino = directorio_salida / (self.nombre_de_salida or f"{entrada.stem}.dds")
            destino.write_bytes(self.salida(ancho, alto, niveles))
            for extra in self.extras:
                (directorio_salida / extra).write_bytes(b"x")
        return self.stdout, self.stderr, self.codigo


@pytest.fixture
def texconv_falso_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "origen" / "texconv.exe"
    exe.parent.mkdir()
    exe.write_bytes(_BINARIO)
    return exe


@pytest.fixture
def zona(tmp_path: Path) -> Path:
    """Directorio temporal propio de cada test: tras una exportación debe quedar VACÍO."""
    carpeta = tmp_path / "zona"
    carpeta.mkdir()
    return carpeta


@pytest.fixture
def falso(monkeypatch: pytest.MonkeyPatch) -> TexconvFalso:
    instancia = TexconvFalso()
    monkeypatch.setattr(texconv_runner, "run_capture", instancia)
    return instancia


def _altura(forma: tuple[int, int] = (16, 8), semilla: int = 1) -> np.ndarray:
    return np.random.default_rng(semilla).random(forma)


async def _exportar(
    exe: Path, zona: Path, altura: np.ndarray | None = None, **kwargs: object
) -> ResultadoDeExportacion:
    return await exportar_altura_bc4(
        _altura() if altura is None else altura,
        texconv=exe,
        pin=_pin_del_binario_falso(),
        directorio_temporal=zona,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


async def test_exporta_y_devuelve_los_bytes_la_descripcion_y_la_huella(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    resultado = await _exportar(texconv_falso_exe, zona)

    esperado = _dds_bc4(8, 16, 5)
    assert resultado.datos == esperado
    assert (resultado.info.ancho, resultado.info.alto, resultado.info.niveles) == (8, 16, 5)
    assert resultado.info.formato == "BC4_UNORM"
    assert resultado.sha256 == hashlib.sha256(esperado).hexdigest()
    assert (resultado.texconv_version, resultado.texconv_sha256) == ("may2026", hashlib.sha256(_BINARIO).hexdigest())
    assert resultado.argumentos == ("-f", "BC4_UNORM", "-m", "0", "-y")


async def test_el_argv_es_una_lista_con_los_flags_verificados_y_todo_dentro_del_espacio_privado(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    await _exportar(texconv_falso_exe, zona)

    (argv,) = falso.llamadas
    assert argv[1:6] == ["-f", "BC4_UNORM", "-m", "0", "-y"]
    assert tuple(argv[1:6]) == ARGUMENTOS_BC4[:5]
    assert argv[6] == "-o" and len(argv) == 9
    espacio = Path(argv[0]).parent.parent
    assert espacio.parent == zona and falso.cwd == str(espacio)
    assert Path(argv[7]) == espacio / "salida" and Path(argv[8]) == espacio / "entrada.dds"
    assert all(isinstance(a, str) for a in argv)


async def test_ejecuta_una_copia_verificada_y_no_el_binario_original(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    await _exportar(texconv_falso_exe, zona)

    (argv,) = falso.llamadas
    assert Path(argv[0]) != texconv_falso_exe and Path(argv[0]).name == "texconv.exe"
    assert falso.binario_ejecutado == _BINARIO  # lo que se ejecuta tiene la huella del pin
    assert texconv_falso_exe.read_bytes() == _BINARIO  # el original no se toca


async def test_la_entrada_de_texconv_es_la_cadena_de_mips_cuantizada_de_la_altura(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    altura = _altura((16, 8), semilla=7)

    await _exportar(texconv_falso_exe, zona, altura)

    esperados = [cuantizar_a_8_bits(nivel).tobytes() for nivel in generar_mips(altura)]
    assert falso.entrada[128:] == b"".join(esperados)
    assert _leer_entrada_l8(falso.entrada) == (8, 16, 5)
    with Image.open(io.BytesIO(falso.entrada)) as imagen:  # lector independiente del nivel base
        assert imagen.size == (8, 16)
        imagen.load()
        assert np.array_equal(np.asarray(imagen), cuantizar_a_8_bits(generar_mips(altura)[0]))


async def test_las_rutas_del_argv_son_absolutas_aunque_el_directorio_temporal_sea_relativo(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(zona.parent)

    await _exportar(texconv_falso_exe, Path("zona"))

    (argv,) = falso.llamadas
    assert all(Path(argv[i]).is_absolute() for i in (0, 7, 8))
    assert list(zona.iterdir()) == []


async def test_el_plazo_pedido_llega_a_run_capture(texconv_falso_exe: Path, zona: Path, falso: TexconvFalso) -> None:
    await _exportar(texconv_falso_exe, zona, timeout_s=42.5)

    assert falso.plazo == 42.5


async def test_acepta_una_salida_con_extension_dx10_si_es_bc4_unorm(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    falso.salida = lambda ancho, alto, niveles: _dds_bc4(ancho, alto, niveles, fourcc=b"DX10", dx10=80)

    resultado = await _exportar(texconv_falso_exe, zona)

    assert resultado.info.con_extension_dx10 is True


async def test_el_espacio_privado_se_borra_tras_exportar(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    await _exportar(texconv_falso_exe, zona)

    assert list(zona.iterdir()) == []


# ---------------------------------------------------------------------------
# Fail-closed antes de ejecutar nada
# ---------------------------------------------------------------------------


async def test_un_binario_que_no_coincide_con_el_pin_no_se_ejecuta(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    texconv_falso_exe.write_bytes(_BINARIO + b"!")

    with pytest.raises(TexconvNoCoincideError):
        await _exportar(texconv_falso_exe, zona)

    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_se_verifica_la_copia_que_se_va_a_ejecutar_y_no_el_original(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    # El original tiene la huella correcta, pero lo que queda copiado difiere (alguien lo cambió en el camino):
    # verificar el original daría luz verde a un binario distinto del que se ejecuta.
    copiar_original = texconv_runner.shutil.copy2

    def _copia_alterada(origen: Path, destino: Path) -> Path:
        copiar_original(origen, destino)
        Path(destino).write_bytes(_BINARIO + b"!")
        return Path(destino)

    monkeypatch.setattr(texconv_runner.shutil, "copy2", _copia_alterada)

    with pytest.raises(TexconvNoCoincideError):
        await _exportar(texconv_falso_exe, zona)

    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_sin_pin_no_se_ejecuta_nada(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", None)

    with pytest.raises(TexconvNoFijadoError):
        await exportar_altura_bc4(_altura(), texconv=texconv_falso_exe, directorio_temporal=zona)

    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_usa_el_pin_vigente_cuando_no_se_pasa_uno(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", _pin_del_binario_falso())

    resultado = await exportar_altura_bc4(_altura(), texconv=texconv_falso_exe, directorio_temporal=zona)

    assert resultado.texconv_version == "may2026"


@pytest.mark.parametrize("quien", ["inexistente", "directorio"])
async def test_un_texconv_que_no_es_un_archivo_falla_cerrado(
    tmp_path: Path, zona: Path, falso: TexconvFalso, quien: str
) -> None:
    ruta = tmp_path / "no-esta.exe" if quien == "inexistente" else tmp_path

    with pytest.raises(TexconvNoCoincideError):
        await _exportar(ruta, zona)

    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_una_altura_invalida_se_rechaza_antes_de_crear_nada(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    for invalida in (np.full((8, 8), np.nan), np.zeros((8, 12)), np.zeros(8), np.full((8, 8), 2.0)):
        with pytest.raises(ExportError):
            await _exportar(texconv_falso_exe, zona, invalida)

    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_una_altura_con_nan_lanza_el_error_de_altura(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    with pytest.raises(AlturaNoExportableError):
        await _exportar(texconv_falso_exe, zona, np.full((8, 8), np.nan))


@pytest.mark.parametrize("plazo", [0, -1, float("nan"), float("inf"), True, "10", None])
async def test_un_plazo_invalido_se_rechaza(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, plazo: object
) -> None:
    with pytest.raises(TimeoutInvalidoError):
        await _exportar(texconv_falso_exe, zona, timeout_s=plazo)

    assert falso.llamadas == []


async def test_un_directorio_temporal_inexistente_es_un_error_de_espacio_de_trabajo(
    texconv_falso_exe: Path, tmp_path: Path, falso: TexconvFalso
) -> None:
    with pytest.raises(EspacioDeTrabajoError):
        await exportar_altura_bc4(
            _altura(),
            texconv=texconv_falso_exe,
            pin=_pin_del_binario_falso(),
            directorio_temporal=tmp_path / "no-existe",
        )

    assert falso.llamadas == []


# ---------------------------------------------------------------------------
# Fallos del proceso
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("excepcion", "esperado"),
    [
        (FileNotFoundError("no está"), TexconvNoEjecutadoError),
        (PermissionError("denegado"), TexconvNoEjecutadoError),
        (OSError("falló el lanzamiento"), TexconvNoEjecutadoError),
        (TimeoutError(), TexconvTimeoutError),
    ],
    ids=["no-encontrado", "sin-permiso", "oserror", "timeout"],
)
async def test_los_fallos_al_lanzar_o_por_plazo_se_traducen_y_limpian(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, excepcion: BaseException, esperado: type[Exception]
) -> None:
    falso.excepcion = excepcion

    with pytest.raises(esperado) as info:
        await _exportar(texconv_falso_exe, zona)

    assert isinstance(info.value, TexconvRunnerError) and isinstance(info.value, ExportError)
    assert list(zona.iterdir()) == []


async def test_un_codigo_de_salida_distinto_de_cero_falla_con_la_cola_de_la_salida(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    falso.codigo, falso.stdout, falso.stderr, falso.escribir = (
        1,
        b"x" * 5000 + b"ERROR: Unknown option",
        b"detalle",
        False,
    )

    with pytest.raises(TexconvCodigoDeSalidaError) as info:
        await _exportar(texconv_falso_exe, zona)

    mensaje = str(info.value)
    assert "1" in mensaje and "ERROR: Unknown option" in mensaje and "detalle" in mensaje
    assert len(mensaje) < 3000  # la salida se acota, no se vuelca entera
    assert list(zona.iterdir()) == []


@pytest.mark.parametrize("codigo", [1, 2, 255, -1, -1073741819])
async def test_cualquier_codigo_de_salida_distinto_de_cero_es_un_fallo(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, codigo: int
) -> None:
    # Un crash en Windows (acceso inválido, 0xC0000005) llega como entero negativo; ``> 0`` lo dejaría pasar.
    falso.codigo, falso.escribir = codigo, False

    with pytest.raises(TexconvCodigoDeSalidaError):
        await _exportar(texconv_falso_exe, zona)


async def test_un_codigo_de_salida_con_salida_ilegible_no_rompe_el_mensaje(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    falso.codigo, falso.stdout, falso.escribir = 3, b"\xff\xfe\x00invalido", False

    with pytest.raises(TexconvCodigoDeSalidaError):
        await _exportar(texconv_falso_exe, zona)


async def test_la_cancelacion_propaga_y_deja_el_directorio_limpio(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    falso.excepcion = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _exportar(texconv_falso_exe, zona)

    assert list(zona.iterdir()) == []


# ---------------------------------------------------------------------------
# Cancelación con hilos de trabajo en vuelo (hallazgo de Codex en #772)
# ---------------------------------------------------------------------------


class _HiloEnVuelo:
    """Bloquea una función que corre en un hilo hasta que el test la libere, y registra si ya terminó."""

    def __init__(self, original: object) -> None:
        self.original = original
        self.entro = threading.Event()
        self.liberar = threading.Event()
        self.termino = threading.Event()
        self.estado_al_borrar: list[bool] = []

    def envolver(self, *args: object, **kwargs: object) -> object:
        self.entro.set()
        assert self.liberar.wait(timeout=10), "el test no liberó al hilo"
        try:
            return self.original(*args, **kwargs)  # type: ignore[operator]
        finally:
            self.termino.set()


async def _cancelar_con_el_hilo_en_vuelo(
    texconv_falso_exe: Path, zona: Path, hilo: _HiloEnVuelo, monkeypatch: pytest.MonkeyPatch
) -> None:
    borrar_original = texconv_runner._borrar_espacio

    def _borrar_registrando(espacio: Path) -> None:
        hilo.estado_al_borrar.append(hilo.termino.is_set())
        borrar_original(espacio)

    monkeypatch.setattr(texconv_runner, "_borrar_espacio", _borrar_registrando)
    tarea = asyncio.create_task(_exportar(texconv_falso_exe, zona))
    assert await asyncio.to_thread(hilo.entro.wait, 10), "el hilo de trabajo nunca arrancó"

    tarea.cancel()
    asyncio.get_running_loop().call_later(0.1, hilo.liberar.set)  # el hilo sigue vivo un rato tras la cancelación

    with pytest.raises(asyncio.CancelledError):
        await tarea


async def test_cancelar_mientras_se_copia_espera_al_hilo_antes_de_borrar_el_espacio(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    hilo = _HiloEnVuelo(texconv_runner._copiar_y_verificar)
    monkeypatch.setattr(texconv_runner, "_copiar_y_verificar", hilo.envolver)

    await _cancelar_con_el_hilo_en_vuelo(texconv_falso_exe, zona, hilo, monkeypatch)

    # El borrado ocurrió DESPUÉS de que el hilo terminó: nada se borra bajo los pies de quien todavía escribe.
    assert hilo.estado_al_borrar == [True]
    assert falso.llamadas == []
    assert list(zona.iterdir()) == []


async def test_cancelar_mientras_se_lee_la_salida_espera_al_hilo_antes_de_borrar_el_espacio(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    hilo = _HiloEnVuelo(texconv_runner._leer_salida)
    monkeypatch.setattr(texconv_runner, "_leer_salida", hilo.envolver)

    await _cancelar_con_el_hilo_en_vuelo(texconv_falso_exe, zona, hilo, monkeypatch)

    assert hilo.estado_al_borrar == [True]
    assert list(zona.iterdir()) == []


async def test_el_espacio_de_trabajo_se_crea_sin_ceder_el_control_al_bucle(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Si se creara en un hilo, una cancelación a mitad de la espera dejaría el directorio creado y nunca asignado
    # (nadie lo borraría). Creado de forma síncrona no hay punto de cancelación entre crearlo y protegerlo.
    crear_original = texconv_runner._crear_espacio
    en_el_bucle: list[bool] = []

    def _crear_registrando(base: object) -> Path:
        try:
            asyncio.get_running_loop()
            en_el_bucle.append(True)
        except RuntimeError:
            en_el_bucle.append(False)
        return crear_original(base)  # type: ignore[arg-type]

    monkeypatch.setattr(texconv_runner, "_crear_espacio", _crear_registrando)

    await _exportar(texconv_falso_exe, zona)

    assert en_el_bucle == [True]


async def test_en_hilo_devuelve_el_valor_reenvia_los_argumentos_y_propaga_las_excepciones() -> None:
    assert await texconv_runner._en_hilo(lambda a, b: a + b, 2, 3) == 5
    assert await texconv_runner._en_hilo(lambda a, b=0: a + b, 2, b=5) == 7
    with pytest.raises(ZeroDivisionError):
        await texconv_runner._en_hilo(lambda: 1 / 0)


async def test_en_hilo_cancelado_espera_al_hilo_y_no_deja_excepciones_sin_recoger() -> None:
    liberar, termino = threading.Event(), threading.Event()

    def _trabajo() -> None:
        liberar.wait(timeout=10)
        termino.set()
        raise RuntimeError("falló después de la cancelación")

    reportes: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _bucle, contexto: reportes.append(contexto))
    tarea = asyncio.create_task(texconv_runner._en_hilo(_trabajo))
    await asyncio.sleep(0.05)
    tarea.cancel()
    asyncio.get_running_loop().call_later(0.05, liberar.set)

    with pytest.raises(asyncio.CancelledError):
        await tarea
    del tarea
    gc.collect()

    assert termino.is_set()  # la cancelación solo se propagó cuando el hilo ya había terminado
    assert reportes == []  # la excepción tardía del hilo se recogió: asyncio no avisa "never retrieved"


async def test_en_hilo_cancelado_no_espera_para_siempre_a_un_hilo_colgado(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Un hilo bloqueado (disco en red, archivo retenido) no puede colgar la cancelación: pasado el tope se sigue sin él.
    monkeypatch.setattr(texconv_runner, "_ESPERA_MAXIMA_DEL_HILO_S", 0.1)
    liberar = threading.Event()
    tarea = asyncio.create_task(texconv_runner._en_hilo(lambda: liberar.wait(timeout=30)))
    await asyncio.sleep(0.05)
    tarea.cancel()
    inicio = time.monotonic()

    with caplog.at_level("WARNING", logger=texconv_runner.logger.name), pytest.raises(asyncio.CancelledError):
        await tarea

    liberar.set()  # suelta al hilo: el test no deja trabajo colgado
    assert time.monotonic() - inicio < 10  # propagó la cancelación sin esperar los 30 s del hilo
    assert any("sigue en curso" in registro.getMessage() for registro in caplog.records)


def test_el_tope_de_espera_del_hilo_es_finito_y_acotado() -> None:
    # Sin tope, un hilo colgado cuelga la cancelación; con un tope enorme es lo mismo en la práctica.
    assert 0 < texconv_runner._ESPERA_MAXIMA_DEL_HILO_S <= 120


async def test_en_hilo_cancelado_no_registra_aviso_si_el_hilo_termina_a_tiempo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    liberar = threading.Event()
    tarea = asyncio.create_task(texconv_runner._en_hilo(lambda: liberar.wait(timeout=10)))
    await asyncio.sleep(0.05)
    tarea.cancel()
    asyncio.get_running_loop().call_later(0.05, liberar.set)

    with caplog.at_level("WARNING", logger=texconv_runner.logger.name), pytest.raises(asyncio.CancelledError):
        await tarea

    assert not [registro for registro in caplog.records if "sigue en curso" in registro.getMessage()]


# ---------------------------------------------------------------------------
# Validación de la salida (cada defecto, enumerado)
# ---------------------------------------------------------------------------


def _casos_de_salida_invalida() -> list[tuple[dict[str, object], str]]:
    return [
        ({"escribir": False}, "no-genera-archivo"),
        ({"nombre_de_salida": "otra-cosa.dds"}, "nombre-distinto"),
        ({"extras": ("sobrante.dds",)}, "archivo-extra"),
        ({"salida": lambda a, h, n: b""}, "vacio"),
        ({"salida": lambda a, h, n: b"no es un dds" * 20}, "no-es-dds"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, n, fourcc=b"BC5U")}, "formato-equivocado"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, n, fourcc=b"DX10", dx10=81)}, "bc4-con-signo"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, n - 1)}, "un-mip-de-menos"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, 1)}, "sin-mips"),
        ({"salida": lambda a, h, n: _dds_bc4(a * 2, h * 2, n + 1)}, "dimensiones-distintas"),
        ({"salida": lambda a, h, n: _dds_bc4(h, a, n)}, "dimensiones-transpuestas"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, n, payload_extra=8)}, "payload-largo"),
        ({"salida": lambda a, h, n: _dds_bc4(a, h, n)[:-8]}, "payload-corto"),
    ]


@pytest.mark.parametrize(
    ("configuracion", "_nombre"), _casos_de_salida_invalida(), ids=[nombre for _, nombre in _casos_de_salida_invalida()]
)
async def test_toda_salida_defectuosa_de_texconv_se_rechaza_y_se_limpia(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, configuracion: dict[str, object], _nombre: str
) -> None:
    for campo, valor in configuracion.items():
        setattr(falso, campo, valor)

    with pytest.raises(SalidaDeTexconvInvalidaError):
        await _exportar(texconv_falso_exe, zona)

    assert list(zona.iterdir()) == []


async def test_el_tope_de_tamano_es_el_exacto_de_la_cadena_mas_la_cabecera_maxima(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso
) -> None:
    falso.salida = lambda a, h, n: _dds_bc4(a, h, n, payload_extra=1000)

    with pytest.raises(SalidaDeTexconvInvalidaError, match="tope"):
        await _exportar(texconv_falso_exe, zona)


def test_leer_acotado_rechaza_un_archivo_que_creció_hasta_pasar_el_tope(tmp_path: Path) -> None:
    justo, de_mas = tmp_path / "justo.bin", tmp_path / "de_mas.bin"
    justo.write_bytes(b"a" * 10)
    de_mas.write_bytes(b"a" * 11)

    assert texconv_runner._leer_acotado(justo, 10) == b"a" * 10
    with pytest.raises(SalidaDeTexconvInvalidaError):
        texconv_runner._leer_acotado(de_mas, 10)


async def test_una_salida_desmesurada_se_rechaza_sin_leerla_entera(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    falso.salida = lambda a, h, n: _dds_bc4(a, h, n, payload_extra=1 << 20)
    leidos: list[int] = []
    original = texconv_runner._leer_acotado
    monkeypatch.setattr(
        texconv_runner, "_leer_acotado", lambda ruta, tope: (leidos.append(tope), original(ruta, tope))[1]
    )

    with pytest.raises(SalidaDeTexconvInvalidaError):
        await _exportar(texconv_falso_exe, zona)

    assert leidos == []  # el tamaño se compara antes de abrir el archivo


def _puede_crear_symlinks(carpeta: Path) -> bool:
    """Prueba real en vez de suponer por plataforma: en Windows con Modo desarrollador o como administrador sí se puede."""
    destino, enlace = carpeta / "destino-de-prueba", carpeta / "enlace-de-prueba"
    destino.write_bytes(b"x")
    try:
        os.symlink(destino, enlace)
    except (OSError, NotImplementedError):
        return False
    finally:
        enlace.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)
    return True


async def test_una_salida_que_es_un_symlink_se_rechaza(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _puede_crear_symlinks(tmp_path):
        pytest.skip("este sistema no permite crear symlinks (privilegios)")
    ajeno = tmp_path / "ajeno.dds"
    ajeno.write_bytes(_dds_bc4(8, 16, 5))

    class _ConEnlace(TexconvFalso):
        async def __call__(
            self, args: list[str], *, timeout: float, cwd: str | None = None
        ) -> tuple[bytes, bytes, int]:
            self.llamadas.append(list(args))
            os.symlink(ajeno, Path(args[args.index("-o") + 1]) / "entrada.dds")
            return b"", b"", 0

    monkeypatch.setattr(texconv_runner, "run_capture", _ConEnlace())

    with pytest.raises(SalidaDeTexconvInvalidaError):
        await _exportar(texconv_falso_exe, zona)

    assert ajeno.exists()  # el limpiador no sigue el enlace hacia afuera
    assert list(zona.iterdir()) == []


async def test_una_salida_que_es_un_directorio_se_rechaza(
    texconv_falso_exe: Path, zona: Path, falso: TexconvFalso, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ConDirectorio(TexconvFalso):
        async def __call__(
            self, args: list[str], *, timeout: float, cwd: str | None = None
        ) -> tuple[bytes, bytes, int]:
            self.llamadas.append(list(args))
            (Path(args[args.index("-o") + 1]) / "entrada.dds").mkdir()
            return b"", b"", 0

    monkeypatch.setattr(texconv_runner, "run_capture", _ConDirectorio())

    with pytest.raises(SalidaDeTexconvInvalidaError):
        await _exportar(texconv_falso_exe, zona)

    assert list(zona.iterdir()) == []


# ---------------------------------------------------------------------------
# Superficie pública
# ---------------------------------------------------------------------------


def test_los_errores_del_ejecutor_comparten_la_base_de_exportacion() -> None:
    for tipo in (
        EspacioDeTrabajoError,
        TexconvNoEjecutadoError,
        TexconvTimeoutError,
        TexconvCodigoDeSalidaError,
        SalidaDeTexconvInvalidaError,
        TimeoutInvalidoError,
    ):
        assert issubclass(tipo, TexconvRunnerError) and issubclass(tipo, ExportError)
    assert issubclass(TimeoutInvalidoError, ValueError)


def test_los_flags_fijos_son_exactamente_los_verificados_ejecutando_texconv_real() -> None:
    # Informe de la PC con Windows (2026-10-21): ``texconv -f BC4_UNORM -m 0 -y -o <dir> <archivo>`` terminó con
    # código 0 y produjo BC4 con la cadena completa de mips, idéntico byte a byte en dos corridas. Cualquier flag
    # extra necesita su propia verificación con el binario real: cambiar esta tupla rompe este test a propósito.
    assert ARGUMENTOS_BC4 == ("-f", "BC4_UNORM", "-m", "0", "-y", "-o")


def test_la_superficie_publica_del_ejecutor_esta_congelada() -> None:
    assert sorted(texconv_runner.__all__) == [
        "ARGUMENTOS_BC4",
        "EspacioDeTrabajoError",
        "ResultadoDeExportacion",
        "SalidaDeTexconvInvalidaError",
        "TIMEOUT_POR_DEFECTO_S",
        "TexconvCodigoDeSalidaError",
        "TexconvNoEjecutadoError",
        "TexconvRunnerError",
        "TexconvTimeoutError",
        "TimeoutInvalidoError",
        "exportar_altura_bc4",
    ]
