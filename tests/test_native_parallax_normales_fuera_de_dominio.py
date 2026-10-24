"""Normales fuera del dominio de una altura (``nz <= 0`` o casi): no pueden decidir la altura de TODA la textura.

Hallazgo del rig (2026-10-24, ``wrcitywall01_n.dds`` de Skyland AIO, BC7 2048²): 7 píxeles de 4,2 millones traen
``nz < 0`` (BC7 cuantiza un borde muy inclinado por debajo del horizonte). La inversa ``p = -nx / nz`` con el piso
numérico ``1e-6`` les da un gradiente ~1e6, y Frankot–Chellappa reparte ese gradiente como un dipolo sobre toda la
textura: la altura integrada era una mancha de baja frecuencia sin rastro de las piedras (acuerdo p95 44.8°,
correlación con el difuso −0.015) y con min-max quedaba casi plana (los extremos del dipolo se comían el rango).

La propiedad, enunciada sobre el mecanismo: **un píxel cuyo gradiente no está acotado tiene que pesar lo que vale su
``nz``, no lo que vale su inversa**. La integración del producto (``PONDERACION_DEL_PRODUCTO``) pondera por ``nz``, así
que el aporte de cada píxel queda acotado por su ``nx``, y fuera del dominio (``nz`` bajo el piso) pide pendiente nula
con el peso medio (relleno suave; ver ``integrate.py``).

Cómo se ancla, enumerando y no muestreando:

- cada formato de entrada que el decodificador ACEPTA (la familia se congela contra las listas cerradas de
  ``decode.py``: un formato nuevo rompe el test hasta que se le escribe su receta) × cada normalización, por el camino
  de producción (``lote._medir``), con un bloque fuera del dominio;
- cada clase de píxel fuera del dominio (negativo, cero, bajo el piso, diminuto sobre el piso) en el integrador;
- cada llamador de producto de ``integrar`` pasa la ponderación del producto (por AST: un llamador nuevo rompe el ancla).
"""

from __future__ import annotations

import ast
import pathlib
import struct
from collections.abc import Callable

import pytest

np = pytest.importorskip("numpy")
imagen = pytest.importorskip("PIL.Image")

from sky_claw.local.native_parallax import decode, integrate  # noqa: E402
from sky_claw.local.native_parallax.espectral import gradientes_espectrales  # noqa: E402
from sky_claw.local.native_parallax.gates import decidir  # noqa: E402
from sky_claw.local.native_parallax.integrate import (  # noqa: E402
    ALGORITMO_PONDERADO_NEUMANN,
    ALGORITMO_PONDERADO_PERIODICO,
    Bordes,
    EntradaInvalidaError,
    ModoNormalizacion,
    Ponderacion,
    SinConvergenciaError,
    integrar,
)
from sky_claw.local.native_parallax.lote import ParametrosDelLote, _medir  # noqa: E402
from sky_claw.local.native_parallax.metrics import acuerdo_normal  # noqa: E402
from tests.test_native_parallax_decode import _bloque_bc5, _bloque_bc7_modo6, _dds  # noqa: E402
from tests.test_native_parallax_integrate import _normal_del_coseno  # noqa: E402

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_PAQUETE = _RAIZ / "sky_claw" / "local" / "native_parallax"

_LADO = 64
_BLOQUE = 4


# ---------------------------------------------------------------------------
# La verdad: una altura suave periódica y su normal por bloques de 4x4
# ---------------------------------------------------------------------------


def _altura_verdadera(lado: int = _LADO) -> np.ndarray:
    y, x = np.mgrid[0:lado, 0:lado] / lado
    return 0.08 * np.sin(2 * np.pi * x) * np.sin(2 * np.pi * y) + 0.03 * np.cos(2 * np.pi * 2 * x)


def _normal_de_altura(altura: np.ndarray, sx: float = 1.0, sy: float = 1.0) -> np.ndarray:
    """Normal ``normalize(-sx·p, -sy·q, 1)`` con la derivada espectral del producto: integrable exacta con esas escalas."""
    p, q = gradientes_espectrales(np, altura)
    vector = np.stack((-sx * p, -sy * q, np.ones_like(p)), axis=-1)
    return vector / np.linalg.norm(vector, axis=-1, keepdims=True)


def _rgb_por_bloque() -> np.ndarray:
    """(16, 16, 3) uint8: la normal del centro de cada bloque; el bloque (5, 9) queda fuera del dominio (B < 128)."""
    normal = _normal_de_altura(_altura_verdadera())
    centros = normal[_BLOQUE // 2 :: _BLOQUE, _BLOQUE // 2 :: _BLOQUE]
    rgb = np.round((centros * 0.5 + 0.5) * 255).astype(np.uint8)
    rgb[5, 9] = (230, 128, 100)  # nz = 100/255·2 − 1 < 0: borde cuantizado por debajo del horizonte
    return rgb


def _por_pixel(por_bloque: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(por_bloque, _BLOQUE, axis=0), _BLOQUE, axis=1)


def _bloques(por_bloque: np.ndarray, codificar: Callable[[tuple[int, ...]], bytes]) -> bytes:
    return b"".join(codificar(tuple(int(c) for c in por_bloque[f, c])) for f in range(16) for c in range(16))


# ---------------------------------------------------------------------------
# Una receta por formato ACEPTADO (la familia se congela contra decode.py)
# ---------------------------------------------------------------------------


def _png_rgb(ruta: pathlib.Path) -> None:
    imagen.fromarray(_por_pixel(_rgb_por_bloque()), mode="RGB").save(ruta, format="PNG")


def _png_rgba(ruta: pathlib.Path) -> None:
    rgb = _por_pixel(_rgb_por_bloque())
    alfa = np.full(rgb.shape[:2] + (1,), 40, dtype=np.uint8)  # el alfa de un _n de Skyrim es otra señal
    imagen.fromarray(np.concatenate((rgb, alfa), axis=-1), mode="RGBA").save(ruta, format="PNG")


def _dds_clasico_rgba(ruta: pathlib.Path) -> None:
    """Códec ``dds_rgb``: Pillow escribe un DDS sin comprimir de cabecera clásica."""
    rgb = _por_pixel(_rgb_por_bloque())
    alfa = np.full(rgb.shape[:2] + (1,), 40, dtype=np.uint8)
    imagen.fromarray(np.concatenate((rgb, alfa), axis=-1), mode="RGBA").save(ruta, format="DDS")


def _dds_dx10_rgba(ruta: pathlib.Path) -> None:
    """Códec ``raw``: DXGI_FORMAT_R8G8B8A8_UNORM (28) con cabecera DX10."""
    rgb = _por_pixel(_rgb_por_bloque())
    rgba = np.concatenate((rgb, np.full(rgb.shape[:2] + (1,), 40, dtype=np.uint8)), axis=-1)
    ruta.write_bytes(_dds(_LADO, _LADO, rgba.tobytes(), fourcc=b"DX10", dxgi=28))


def _dds_dxt1(ruta: pathlib.Path) -> None:
    def bloque(rgb: tuple[int, ...]) -> bytes:
        r, g, b = rgb
        color = ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)
        return struct.pack("<HHI", color, color, 0)

    ruta.write_bytes(_dds(_LADO, _LADO, _bloques(_rgb_por_bloque(), bloque), fourcc=b"DXT1"))


def _dds_bc5(ruta: pathlib.Path) -> None:
    por_bloque = _rgb_por_bloque()
    por_bloque[5, 9] = (255, 255, 0)  # BC5 no tiene B: fuera del disco unidad, Z se fija en 0 (``clamped_pixels``)
    ruta.write_bytes(_dds(_LADO, _LADO, _bloques(por_bloque, lambda c: _bloque_bc5(c[0], c[1])), fourcc=b"ATI2"))


def _dds_bc7(ruta: pathlib.Path) -> None:
    # Modo 6 sólido: cada canal sale ``(c << 1) | 1``; el alfa lleva otra señal, como el _n real (0–105).
    payload = _bloques(_rgb_por_bloque(), lambda c: _bloque_bc7_modo6(c[0] >> 1, c[1] >> 1, c[2] >> 1, a=20))
    ruta.write_bytes(_dds(_LADO, _LADO, payload, fourcc=b"DX10", dxgi=98))


# Formatos de bloques: uno por cada elemento de ``decode._FORMATOS_DDS_DE_BLOQUES_VERIFICADOS``.
_RECETAS_DE_BLOQUES: dict[str, Callable[[pathlib.Path], None]] = {"DXT1": _dds_dxt1, "BC5": _dds_bc5, "BC7": _dds_bc7}
# DDS sin comprimir: uno por cada códec de ``decode._CODECS_DDS_SIN_COMPRIMIR``.
_RECETAS_SIN_COMPRIMIR: dict[str, Callable[[pathlib.Path], None]] = {
    "dds_rgb": _dds_clasico_rgba,
    "raw": _dds_dx10_rgba,
}
# PNG: uno por cada modo de ``decode._MODOS_ADMITIDOS``.
_RECETAS_PNG: dict[str, Callable[[pathlib.Path], None]] = {"RGB": _png_rgb, "RGBA": _png_rgba}

_RECETAS: dict[str, Callable[[pathlib.Path], None]] = {
    **{f"DDS_{k}": v for k, v in _RECETAS_DE_BLOQUES.items()},
    **{f"DDS_{k}": v for k, v in _RECETAS_SIN_COMPRIMIR.items()},
    **{f"PNG_{k}": v for k, v in _RECETAS_PNG.items()},
}


def test_hay_una_receta_por_cada_formato_que_el_decodificador_acepta() -> None:
    # Si decode.py acepta un formato nuevo, este test rompe hasta que se le escribe su receta (y entra abajo).
    assert set(_RECETAS_DE_BLOQUES) == decode._FORMATOS_DDS_DE_BLOQUES_VERIFICADOS
    assert set(_RECETAS_SIN_COMPRIMIR) == decode._CODECS_DDS_SIN_COMPRIMIR
    assert set(_RECETAS_PNG) == set(decode._MODOS_ADMITIDOS)
    assert decode._CONTENEDORES_ADMITIDOS == ("PNG", "DDS")


def _parametros(modo: ModoNormalizacion) -> ParametrosDelLote:
    if modo is ModoNormalizacion.PERCENTILES:
        return ParametrosDelLote(normalizacion=modo, percentil_bajo=2.0, percentil_alto=98.0)
    return ParametrosDelLote(normalizacion=modo)


def _correlacion(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.corrcoef(np.asarray(a, dtype=np.float64).ravel(), b.ravel())[0, 1])


@pytest.mark.parametrize("modo", list(ModoNormalizacion), ids=lambda m: m.value)
@pytest.mark.parametrize("formato", sorted(_RECETAS))
def test_un_bloque_fuera_del_dominio_no_borra_la_altura_en_ningun_formato_aceptado(
    tmp_path: pathlib.Path, formato: str, modo: ModoNormalizacion
) -> None:
    ruta = tmp_path / "muro_n.dds"
    _RECETAS[formato](ruta)
    decodificada = decode.NormalDecoder().decode(ruta)
    # La receta de verdad deja un bloque fuera del dominio (si no, el test no prueba nada).
    assert float(decodificada.normal[..., 2].min()) <= 0.0

    medida = _medir(ruta, _parametros(modo), decidir)

    # Medido antes del arreglo: correlación ≈ 0 (el dipolo del bloque se comía la textura) y p95 de ~40-80°.
    # Con la integración ponderada: correlación > 0.99 y p95 de 4-9° (la escalera de bloques de 4x4 es lo que queda).
    assert _correlacion(medida.altura, _altura_verdadera()) > 0.95
    assert medida.metricas.acuerdo_p95_grados < 15.0
    assert medida.algoritmo == ALGORITMO_PONDERADO_PERIODICO
    # Con min-max los extremos del dipolo dejaban todo en un hilo alrededor de 0.5 (el _p "plano" del rig).
    assert float(np.percentile(medida.altura, 98) - np.percentile(medida.altura, 2)) > 0.5


# ---------------------------------------------------------------------------
# El integrador ponderado
# ---------------------------------------------------------------------------


def _normal_con_un_pixel(nz: float) -> np.ndarray:
    normal = _normal_de_altura(_altura_verdadera()).copy()
    normal[20, 40] = (0.6, 0.0, nz)
    normal[20, 40] /= np.linalg.norm(normal[20, 40])
    return normal


@pytest.mark.parametrize(
    "nz",
    [-0.2, 0.0, 1e-9, 1e-5],
    ids=["negativo", "cero", "bajo-el-piso", "diminuto-sobre-el-piso"],
)
@pytest.mark.parametrize("bordes", list(Bordes), ids=lambda b: b.value)
def test_un_solo_pixel_fuera_del_dominio_no_decide_la_altura(nz: float, bordes: Bordes) -> None:
    normal = _normal_con_un_pixel(nz)
    verdad = _altura_verdadera()

    uniforme = integrar(normal, bordes=bordes)
    ponderada = integrar(normal, bordes=bordes, ponderacion=Ponderacion.NZ)

    # El defecto, reproducido: con la inversa sin acotar, UN píxel decide la altura de la textura entera.
    assert _correlacion(uniforme.altura, verdad) < 0.5
    assert _correlacion(ponderada.altura, verdad) > 0.99
    assert acuerdo_normal(normal, ponderada.altura, bordes=bordes).p95_grados < 2.0


@pytest.mark.parametrize("bordes", list(Bordes), ids=lambda b: b.value)
def test_en_un_campo_integrable_la_ponderada_coincide_con_la_uniforme(bordes: Bordes) -> None:
    # Si el campo ES un gradiente, cualquier ponderación positiva tiene el mismo mínimo: la ponderada no inventa nada.
    # Integrable según el borde: periódico sobre el toro; con Neumann, un coseno de derivada normal nula.
    normal = _normal_de_altura(_altura_verdadera()) if bordes is Bordes.PERIODICOS else _normal_del_coseno(64, 96, 3, 2)
    uniforme = integrar(normal, bordes=bordes)
    ponderada = integrar(normal, bordes=bordes, ponderacion=Ponderacion.NZ)

    escala = float(np.abs(uniforme.altura).max())
    assert float(np.abs(ponderada.altura - uniforme.altura).max()) < 1e-5 * escala
    assert abs(float(ponderada.altura.mean())) < 1e-12
    assert ponderada.iteraciones > 0
    assert uniforme.iteraciones == 0


@pytest.mark.parametrize("escalas", [(1.0, 1.0), (2.0, 0.5), (-1.0, 1.0), (1.0, -1.0)])
def test_la_ponderada_respeta_las_escalas_como_la_uniforme(escalas: tuple[float, float]) -> None:
    sx, sy = escalas
    normal = _normal_de_altura(_altura_verdadera(), sx, sy)
    uniforme = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=sx, escala_y=sy)
    ponderada = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=sx, escala_y=sy, ponderacion=Ponderacion.NZ)

    escala = float(np.abs(uniforme.altura).max())
    assert float(np.abs(ponderada.altura - uniforme.altura).max()) < 1e-5 * escala
    verdad = _altura_verdadera()
    assert float(np.abs(ponderada.altura - (verdad - verdad.mean())).max()) < 1e-5 * escala


def test_los_nombres_de_los_algoritmos_ponderados_estan_congelados_y_se_informan() -> None:
    assert ALGORITMO_PONDERADO_PERIODICO == "normal_wls_nz_periodic_v1"
    assert ALGORITMO_PONDERADO_NEUMANN == "normal_wls_nz_neumann_v1"
    normal = _normal_de_altura(_altura_verdadera())
    vistos = {b: integrar(normal, bordes=b, ponderacion=Ponderacion.NZ).algoritmo for b in Bordes}
    assert vistos == {Bordes.PERIODICOS: ALGORITMO_PONDERADO_PERIODICO, Bordes.NEUMANN: ALGORITMO_PONDERADO_NEUMANN}
    assert {p.value for p in Ponderacion} == {"uniforme", "nz"}


def test_la_ponderacion_por_defecto_es_la_uniforme_del_oraculo() -> None:
    # La uniforme sigue siendo Frankot–Chellappa (el oráculo de research); el producto pide la ponderada explícitamente.
    normal = _normal_de_altura(_altura_verdadera())
    assert integrar(normal, bordes=Bordes.PERIODICOS).algoritmo == integrate.ALGORITMO_PERIODICO


def test_si_ningun_pixel_esta_en_el_dominio_la_altura_es_plana() -> None:
    normal = np.zeros((8, 8, 3))
    normal[..., 0] = 1.0  # todo de canto: ningún píxel describe una altura
    resultado = integrar(normal, bordes=Bordes.PERIODICOS, ponderacion=Ponderacion.NZ)
    assert not np.any(resultado.altura)
    assert resultado.nz_bajo_piso == 64


def test_si_no_converge_falla_tipado_y_no_devuelve_una_altura_a_medias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrate, "MAX_ITERACIONES", 1)
    with pytest.raises(SinConvergenciaError, match="iteraciones"):
        integrar(_normal_con_un_pixel(-0.2), bordes=Bordes.PERIODICOS, ponderacion=Ponderacion.NZ)
    assert issubclass(SinConvergenciaError, integrate.IntegracionError)


@pytest.mark.parametrize("malo", ["nz", None, 1])
def test_una_ponderacion_invalida_se_rechaza(malo: object) -> None:
    with pytest.raises(EntradaInvalidaError, match="ponderacion"):
        integrar(_normal_de_altura(_altura_verdadera()), bordes=Bordes.PERIODICOS, ponderacion=malo)  # type: ignore[arg-type]


def test_la_tolerancia_y_el_tope_de_iteraciones_estan_congelados() -> None:
    # Guardas NUMÉRICAS, no umbrales de calidad: medidas sobre wrcitywall01 (2048², BC7 real), ver integrate.py.
    assert integrate.TOLERANCIA_RELATIVA == 1e-8
    assert integrate.MAX_ITERACIONES == 500


# ---------------------------------------------------------------------------
# Todos los llamadores de producto integran con la ponderación del producto
# ---------------------------------------------------------------------------

# (módulo relativo al paquete, función que llama a ``integrar``): lo que mide el gate y lo que se publica tienen que
# salir del MISMO integrador, o los umbrales derivados en ``experiments/medicion.py`` medirían otro algoritmo.
_LLAMADORES_DE_INTEGRAR = frozenset({("lote.py", "_medir"), ("experiments/medicion.py", "medir_textura")})


def _llamadas_a_integrar() -> dict[tuple[str, str], list[ast.Call]]:
    encontradas: dict[tuple[str, str], list[ast.Call]] = {}
    for archivo in sorted(_PAQUETE.rglob("*.py")):
        relativo = archivo.relative_to(_PAQUETE).as_posix()
        if relativo.startswith("research/") or relativo == "integrate.py":
            continue
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for nodo in ast.walk(funcion):
                if isinstance(nodo, ast.Call) and (
                    (isinstance(nodo.func, ast.Name) and nodo.func.id == "integrar")
                    or (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "integrar")
                ):
                    encontradas.setdefault((relativo, funcion.name), []).append(nodo)
    return encontradas


def test_todo_llamador_de_producto_integra_con_la_ponderacion_del_producto() -> None:
    llamadas = _llamadas_a_integrar()
    assert set(llamadas) == _LLAMADORES_DE_INTEGRAR
    for sitio, nodos in llamadas.items():
        for nodo in nodos:
            argumento = next((k.value for k in nodo.keywords if k.arg == "ponderacion"), None)
            assert argumento is not None, f"{sitio} llama a integrar sin ponderacion= (la uniforme es solo el oráculo)"
            assert ast.unparse(argumento) == "PONDERACION_DEL_PRODUCTO", sitio
