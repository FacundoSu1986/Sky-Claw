"""NP-A0a: el lote de carpeta plana (``lote.py``) arma la tubería completa sobre el escritor de la salida.

Qué se verifica (cada propiedad con su test; las familias se enumeran, no se muestrean):

- la tubería lee → integra → normaliza → mide → decide → exporta → escribe, y la procedencia que viaja al manifiesto es la
  de lo que de verdad corrió (huella de la normal, parámetros, encoder que devolvió el exportador, entorno);
- el gate es fail-closed: sin umbrales todo es ``REVIEW`` y NO se escribe salvo pedido explícito (``escribir_revision``);
- un archivo que falla no frena al lote, y cada estado del vocabulario cerrado tiene su caso;
- los parámetros viajan IGUALES a la integración y a las métricas (el cableado que fijara los bordes en una sola
  llamada mezclaría dos dominios en una misma medición);
- una segunda corrida idéntica es caché vigente; las salidas de corridas anteriores se conservan salvo pedido de retirarlas.

El exportador es un doble: el ``texconv`` real solo corre en Windows (su smoke vive en ``tests/test_texconv_real.py``).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import pathlib
import platform
import sys
import tempfile
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sky_claw.local.native_parallax import lote, provider
from sky_claw.local.native_parallax.decode import ArchivoNormalInvalidoError, ConvencionNormal, NormalDecoder
from sky_claw.local.native_parallax.export import ExportError, generar_mips
from sky_claw.local.native_parallax.gates import Decision, MetricasDeCalidad, Motivo, Veredicto
from sky_claw.local.native_parallax.integrate import Bordes, ModoNormalizacion
from sky_claw.local.native_parallax.lote import (
    EXPORTADOR_ID,
    EstadoDeArchivo,
    InformeDelLote,
    LoteInvalidoError,
    ParametrosDelLote,
    ParametrosDelLoteInvalidosError,
    ResultadoDeArchivo,
    ResultadoDeRuta,
    procesar_lote,
)
from sky_claw.local.native_parallax.metrics import Amplitud, amplitud
from sky_claw.local.native_parallax.output import (
    NOMBRE_DEL_MOD_DE_SALIDA,
    NivelDeReproducibilidad,
    clave_de_cache,
    leer_manifiesto,
)
from sky_claw.local.native_parallax.output_writer import (
    CoordinacionDeSalida,
    EfectoDeSalida,
    SalidaNoEscribibleError,
    construir_coordinacion_de_salida,
)

np = pytest.importorskip("numpy")
imagen = pytest.importorskip("PIL.Image")

_H_TEXCONV = hashlib.sha256(b"texconv-de-prueba").hexdigest()
_ARGUMENTOS = ("-f", "BC4_UNORM", "-m", "0", "-y", "-o")
_AUTOACEPTAR = Decision(Veredicto.AUTO_ACCEPT, (), ())


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Normal:
    """Una entrada del lote (lo que devolverá ``provider.py``: ruta de origen, de salida y archivo)."""

    ruta_de_origen: str
    ruta_de_salida: str
    ruta_absoluta: pathlib.Path


@dataclasses.dataclass(frozen=True)
class _ResultadoDelExportador:
    datos: bytes
    texconv_version: str = "may2026"
    texconv_sha256: str = _H_TEXCONV
    argumentos: tuple[str, ...] = _ARGUMENTOS


class _Exportador:
    """Doble del ``texconv``: valida las dimensiones como el real y devuelve bytes que dependen de la altura."""

    def __init__(self) -> None:
        self.alturas: list[Any] = []

    async def __call__(self, altura: Any) -> _ResultadoDelExportador:
        self.alturas.append(altura)
        generar_mips(altura)  # lanza DimensionesNoExportablesError con lados que no son potencia de 2
        return _ResultadoDelExportador(datos=b"BC4:" + hashlib.sha256(altura.tobytes()).digest())


def _normal_sintetica(n: int, fase: float) -> Any:
    """Normal DirectX de una superficie periódica suave, codificada en 8 bits como la guarda un ``_n.dds``."""
    y, x = np.mgrid[0:n, 0:n] / n
    pendiente_x = 0.5 * np.cos(2 * np.pi * x + fase) * np.cos(2 * np.pi * y)
    pendiente_y = -0.5 * np.sin(2 * np.pi * x + fase) * np.sin(2 * np.pi * y)
    vector = np.stack((-pendiente_x, -pendiente_y, np.ones_like(x)), axis=-1)
    vector /= np.linalg.norm(vector, axis=-1, keepdims=True)
    return np.round((vector * 0.5 + 0.5) * 255).astype(np.uint8)


def _escribir_normal(ruta: pathlib.Path, *, n: int = 64, fase: float = 0.0) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    imagen.fromarray(_normal_sintetica(n, fase), mode="RGB").save(ruta, format="PNG")


class Escenario:
    def __init__(self, base: pathlib.Path) -> None:
        self.base = base
        self.entrada = base / "entrada"
        self.entrada.mkdir()
        (base / "mods").mkdir()
        self.raiz = base / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
        self.coordinacion: CoordinacionDeSalida = construir_coordinacion_de_salida(base=base / "estado")
        self.exportador = _Exportador()

    def normal(self, nombre: str, *, n: int = 64, fase: float = 0.0) -> _Normal:
        carpeta = "textures/architecture/whiterun"
        ruta = self.entrada / f"{nombre}_n.png"
        _escribir_normal(ruta, n=n, fase=fase)
        return _Normal(f"{carpeta}/{nombre}_n.dds", f"{carpeta}/{nombre}_p.dds", ruta)

    def disco(self, ruta: str) -> pathlib.Path:
        return self.raiz.joinpath(*ruta.split("/"))

    def manifiesto(self) -> Any:
        archivo = self.raiz / ".skyclaw-output" / "manifest.json"
        return leer_manifiesto(archivo.read_bytes())

    async def lote(self, normales: list[_Normal], **kwargs: Any) -> InformeDelLote:
        kwargs.setdefault("parametros", ParametrosDelLote(escribir_revision=True))
        return await procesar_lote(
            normales,
            raiz_de_salida=self.raiz,
            coordinacion=self.coordinacion,
            exportador=kwargs.pop("exportador", self.exportador),
            **kwargs,
        )

    async def cerrar(self) -> None:
        await self.coordinacion.close()


@pytest.fixture
async def escenario(tmp_path: pathlib.Path) -> AsyncIterator[Escenario]:
    e = Escenario(tmp_path)
    yield e
    await e.cerrar()


def _estados(informe: InformeDelLote) -> dict[str, EstadoDeArchivo]:
    return {a.ruta_de_salida: a.estado for a in informe.archivos}


# ---------------------------------------------------------------------------
# Camino feliz y procedencia
# ---------------------------------------------------------------------------


async def test_el_lote_publica_las_salidas_y_las_describe_en_el_informe(escenario: Escenario) -> None:
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)

    informe = await escenario.lote([a, b])

    assert _estados(informe) == {a.ruta_de_salida: EstadoDeArchivo.ESCRITA, b.ruta_de_salida: EstadoDeArchivo.ESCRITA}
    assert [r.efecto for r in informe.archivos] == [EfectoDeSalida.PUBLICADA, EfectoDeSalida.PUBLICADA]
    assert informe.completo
    for r in informe.archivos:
        assert r.veredicto is Veredicto.REVIEW and Motivo.SIN_UMBRALES.name in r.motivos
        assert r.metricas is not None and r.metricas.no_finitos == 0
        assert escenario.disco(r.ruta_de_salida).read_bytes().startswith(b"BC4:")
    assert {e.ruta for e in escenario.manifiesto().entradas} == {a.ruta_de_salida, b.ruta_de_salida}


@pytest.mark.parametrize(
    "parametros",
    [
        ParametrosDelLote(escribir_revision=True),
        ParametrosDelLote(
            normalizacion=ModoNormalizacion.PERCENTILES, percentil_bajo=5.0, percentil_alto=95.0, escribir_revision=True
        ),
    ],
    ids=["min_max", "percentiles"],
)
async def test_el_informe_declara_la_amplitud_de_la_altura_que_se_exporto(
    escenario: Escenario, parametros: ParametrosDelLote
) -> None:
    # NP-V0 (TL): que un mapa casi plano se vea en el informe antes de llegar al juego. Describe, no decide: sin umbrales,
    # con los percentiles fijos de la descripción (no los de la normalización) y sobre el MISMO arreglo que se exportó.
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)

    informe = await escenario.lote([a, b], parametros=parametros)

    assert lote.PERCENTILES_DE_LA_AMPLITUD == (2.0, 98.0)
    for resultado, exportada in zip(informe.archivos, escenario.exportador.alturas, strict=True):
        assert resultado.amplitud == amplitud(exportada, percentil_bajo=2.0, percentil_alto=98.0)
    assert [a["amplitud"]["percentil_bajo"] for a in informe.a_diccionario()["archivos"]] == [2.0, 2.0]


async def test_la_amplitud_se_declara_aunque_la_revision_no_se_escriba(escenario: Escenario) -> None:
    informe = await escenario.lote([escenario.normal("muro01")], parametros=ParametrosDelLote())

    (resultado,) = informe.archivos
    assert resultado.estado is EstadoDeArchivo.EN_REVISION
    assert resultado.amplitud is not None and resultado.amplitud.desviacion > 0.0


async def test_la_procedencia_del_manifiesto_describe_lo_que_de_verdad_corrio(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")
    huella_de_la_normal = hashlib.sha256(normal.ruta_absoluta.read_bytes()).hexdigest()
    parametros = ParametrosDelLote(bordes=Bordes.NEUMANN, escala_y=-1.0, escribir_revision=True)

    await escenario.lote([normal], parametros=parametros)

    entrada = escenario.manifiesto().entradas[0]
    procedencia = entrada.procedencia
    assert procedencia.algoritmo_id == "normal_fft_neumann_v1"
    assert [(o.rol, o.sha256) for o in procedencia.origenes] == [("normal", huella_de_la_normal)]
    assert json.loads(procedencia.parametros) == parametros.a_parametros_de_procedencia()
    assert json.loads(procedencia.parametros)["bordes"] == "neumann"
    assert json.loads(procedencia.parametros)["escala_y"] == -1.0
    assert procedencia.encoder.exportador_id == EXPORTADOR_ID
    assert (procedencia.encoder.texconv_version, procedencia.encoder.texconv_sha256) == ("may2026", _H_TEXCONV)
    assert procedencia.encoder.argumentos == _ARGUMENTOS
    assert procedencia.nivel is NivelDeReproducibilidad.DETERMINISTA_MISMO_ENTORNO
    nombres = [nombre for nombre, _ in procedencia.entorno]
    assert nombres == sorted(nombres) and {"numpy", "pillow", "plataforma", "python"} <= set(nombres)
    assert entrada.clave_de_cache == clave_de_cache(procedencia)


async def test_el_exportador_recibe_la_altura_normalizada_a_0_1(escenario: Escenario) -> None:
    await escenario.lote([escenario.normal("muro01")])

    (altura,) = escenario.exportador.alturas
    assert altura.shape == (64, 64) and float(altura.min()) == 0.0 and float(altura.max()) == 1.0


async def test_los_parametros_que_cambian_los_bytes_cambian_la_clave_y_los_que_no_la_dejan(
    escenario: Escenario,
) -> None:
    normal = escenario.normal("muro01")
    await escenario.lote([normal])
    clave_base = escenario.manifiesto().entradas[0].clave_de_cache

    # ``escribir_revision`` y ``retirar_no_incluidas`` son política, no entran a los bytes: misma clave, caché vigente.
    politica = ParametrosDelLote(escribir_revision=True, retirar_no_incluidas=True)
    informe = await escenario.lote([normal], parametros=politica)
    assert informe.archivos[0].efecto is EfectoDeSalida.OMITIDA
    assert escenario.manifiesto().entradas[0].clave_de_cache == clave_base

    # ``escala_y`` sí cambia la altura: otra clave, reconstrucción.
    otra = ParametrosDelLote(escala_y=-1.0, escribir_revision=True)
    informe = await escenario.lote([normal], parametros=otra)
    assert informe.archivos[0].efecto is EfectoDeSalida.REEMPLAZADA
    assert escenario.manifiesto().entradas[0].clave_de_cache != clave_base


async def test_una_segunda_corrida_identica_es_cache_vigente_y_no_reescribe(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")
    await escenario.lote([normal])
    destino = escenario.disco(normal.ruta_de_salida)
    antes = destino.stat().st_mtime_ns

    informe = await escenario.lote([normal])

    assert informe.archivos[0].estado is EstadoDeArchivo.ESCRITA
    assert informe.archivos[0].efecto is EfectoDeSalida.OMITIDA
    assert destino.stat().st_mtime_ns == antes


# ---------------------------------------------------------------------------
# El gate: fail-closed
# ---------------------------------------------------------------------------


async def test_sin_umbrales_todo_es_revision_y_no_se_escribe_nada_sin_pedirlo(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")

    informe = await escenario.lote([normal], parametros=ParametrosDelLote())

    (resultado,) = informe.archivos
    assert resultado.estado is EstadoDeArchivo.EN_REVISION
    assert resultado.veredicto is Veredicto.REVIEW and resultado.efecto is None
    assert Motivo.SIN_UMBRALES.name in resultado.motivos
    assert resultado.metricas is not None  # las métricas se miden igual: son los datos para calibrar los umbrales
    assert escenario.exportador.alturas == []  # ni se exporta lo que no se va a escribir
    assert not escenario.raiz.exists()  # y no se abre la sesión ni se crea el mod de salida
    assert informe.completo  # una revisión es una decisión del gate, no un fallo
    assert informe.contar()[EstadoDeArchivo.EN_REVISION] == 1


async def test_un_auto_accept_se_escribe_sin_pedir_nada(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")

    informe = await escenario.lote([normal], parametros=ParametrosDelLote(), decisor=lambda _metricas: _AUTOACEPTAR)

    assert informe.archivos[0].estado is EstadoDeArchivo.ESCRITA
    assert informe.archivos[0].veredicto is Veredicto.AUTO_ACCEPT and informe.archivos[0].motivos == ()
    assert escenario.disco(normal.ruta_de_salida).is_file()


# ---------------------------------------------------------------------------
# Un archivo que falla no frena al lote; cada estado del vocabulario tiene su caso
# ---------------------------------------------------------------------------


async def test_un_archivo_ilegible_no_frena_a_los_demas(escenario: Escenario) -> None:
    buena = escenario.normal("muro01")
    rota = escenario.normal("muro02")
    rota.ruta_absoluta.write_bytes(b"esto no es una imagen")

    informe = await escenario.lote([rota, buena])

    assert _estados(informe) == {
        rota.ruta_de_salida: EstadoDeArchivo.NO_PROCESADA,
        buena.ruta_de_salida: EstadoDeArchivo.ESCRITA,
    }
    assert "no se pudo leer" in informe.archivos[0].detalle
    assert not informe.completo


async def test_una_textura_plana_no_tiene_relieve_que_exportar(escenario: Escenario) -> None:
    plana = escenario.normal("muro01")
    imagen.fromarray(np.full((64, 64, 3), (128, 128, 255), dtype=np.uint8), mode="RGB").save(plana.ruta_absoluta)

    informe = await escenario.lote([plana])

    assert informe.archivos[0].estado is EstadoDeArchivo.NO_PROCESADA
    assert "rango" in informe.archivos[0].detalle
    assert escenario.exportador.alturas == []


async def test_un_lado_que_no_es_potencia_de_2_falla_su_ruta_con_el_motivo_del_exportador(escenario: Escenario) -> None:
    rara = escenario.normal("muro01", n=48)
    buena = escenario.normal("muro02")

    informe = await escenario.lote([rara, buena])

    assert _estados(informe) == {
        rara.ruta_de_salida: EstadoDeArchivo.NO_PROCESADA,
        buena.ruta_de_salida: EstadoDeArchivo.ESCRITA,
    }
    assert "potencia" in informe.archivos[0].detalle.lower() or "dimension" in informe.archivos[0].detalle.lower()


async def test_un_exportador_que_falla_falla_esa_ruta_y_el_lote_sigue(escenario: Escenario) -> None:
    llamadas = 0

    async def exportador(altura: Any) -> _ResultadoDelExportador:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise ExportError("texconv terminó con código 1")
        return await escenario.exportador(altura)

    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)

    informe = await escenario.lote([a, b], exportador=exportador)

    assert _estados(informe) == {
        a.ruta_de_salida: EstadoDeArchivo.NO_PROCESADA,
        b.ruta_de_salida: EstadoDeArchivo.ESCRITA,
    }
    assert "código 1" in informe.archivos[0].detalle


async def test_un_archivo_ajeno_en_la_ruta_de_salida_no_se_pisa(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")
    ajeno = escenario.disco(normal.ruta_de_salida)
    ajeno.parent.mkdir(parents=True)
    ajeno.write_bytes(b"AUTORAL")

    informe = await escenario.lote([normal])

    assert informe.archivos[0].estado is EstadoDeArchivo.NO_ESCRITA
    assert informe.archivos[0].efecto is EfectoDeSalida.PRESERVADA
    assert ajeno.read_bytes() == b"AUTORAL"
    assert not informe.completo  # una salida que el escritor no pudo dejar es un fallo del lote


def test_cada_estado_del_vocabulario_cerrado_tiene_un_caso_en_este_archivo() -> None:
    """Si alguien agrega un estado a ``EstadoDeArchivo`` sin un caso, este test se lo recuerda."""
    cubiertos = {
        EstadoDeArchivo.ESCRITA,  # test_el_lote_publica_las_salidas_y_las_describe_en_el_informe
        EstadoDeArchivo.EN_REVISION,  # test_sin_umbrales_todo_es_revision_y_no_se_escribe_nada_sin_pedirlo
        EstadoDeArchivo.NO_PROCESADA,  # test_un_archivo_ilegible_no_frena_a_los_demas
        EstadoDeArchivo.NO_ESCRITA,  # test_un_archivo_ajeno_en_la_ruta_de_salida_no_se_pisa
    }
    assert set(EstadoDeArchivo) == cubiertos


# ---------------------------------------------------------------------------
# Los parámetros viajan IGUALES a la integración y a las métricas
# ---------------------------------------------------------------------------


async def test_bordes_y_escalas_son_los_mismos_en_la_integracion_y_en_cada_metrica(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    vistos: dict[str, dict[str, Any]] = {}

    def espiar(nombre: str) -> None:
        original = getattr(lote, nombre)

        def envoltura(*args: Any, **kwargs: Any) -> Any:
            vistos[nombre] = {k: v for k, v in kwargs.items() if k in ("bordes", "escala_x", "escala_y")}
            return original(*args, **kwargs)

        monkeypatch.setattr(lote, nombre, envoltura)

    for nombre in ("integrar", "acuerdo_normal", "residuo_de_integrabilidad"):
        espiar(nombre)
    parametros = ParametrosDelLote(bordes=Bordes.NEUMANN, escala_x=2.0, escala_y=-1.0, escribir_revision=True)

    await escenario.lote([escenario.normal("muro01")], parametros=parametros)

    esperado = {"bordes": Bordes.NEUMANN, "escala_x": 2.0, "escala_y": -1.0}
    assert vistos == {
        "integrar": esperado,
        "acuerdo_normal": esperado,
        "residuo_de_integrabilidad": esperado,
    }


async def test_la_convencion_de_la_fuente_llega_al_decodificador_y_a_la_procedencia(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    convenciones: list[ConvencionNormal] = []
    decodificar = NormalDecoder.decode

    def espiar(self: NormalDecoder, path: Any) -> Any:
        convenciones.append(self.source_convention)
        return decodificar(self, path)

    monkeypatch.setattr(NormalDecoder, "decode", espiar)
    parametros = ParametrosDelLote(convencion=ConvencionNormal.OPENGL, escribir_revision=True)

    await escenario.lote([escenario.normal("muro01")], parametros=parametros)

    assert convenciones == [ConvencionNormal.OPENGL]
    assert json.loads(escenario.manifiesto().entradas[0].procedencia.parametros)["convencion"] == "opengl"


async def test_los_percentiles_llegan_a_la_normalizacion(escenario: Escenario) -> None:
    parametros = ParametrosDelLote(
        normalizacion=ModoNormalizacion.PERCENTILES, percentil_bajo=5.0, percentil_alto=95.0, escribir_revision=True
    )

    await escenario.lote([escenario.normal("muro01")], parametros=parametros)

    entrada = escenario.manifiesto().entradas[0]
    cargados = json.loads(entrada.procedencia.parametros)
    assert (cargados["normalizacion"], cargados["percentil_bajo"], cargados["percentil_alto"]) == (
        "percentiles",
        5.0,
        95.0,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "normalizacion": ModoNormalizacion.PERCENTILES
        },  # sin percentiles: no hay valor por defecto (ADR 0014 §4 y §6)
        {"percentil_bajo": 5.0, "percentil_alto": 95.0},  # MIN_MAX no usa percentiles
        {"normalizacion": ModoNormalizacion.PERCENTILES, "percentil_bajo": 90.0, "percentil_alto": 10.0},
        {"escala_x": 0.0},
        {"escala_y": float("nan")},
        {"bordes": "periodicos"},
        {"convencion": "directx"},
        {"escribir_revision": 1},
    ],
)
def test_los_parametros_invalidos_se_rechazan_al_construirlos(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ParametrosDelLoteInvalidosError):
        ParametrosDelLote(**kwargs)


# ---------------------------------------------------------------------------
# Corridas anteriores: se conservan salvo pedido
# ---------------------------------------------------------------------------


async def test_las_salidas_de_corridas_anteriores_se_conservan_por_defecto(escenario: Escenario) -> None:
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)
    await escenario.lote([a])

    informe = await escenario.lote([b])

    assert informe.conservadas == (a.ruta_de_salida,) and informe.retiradas == ()
    assert escenario.disco(a.ruta_de_salida).is_file() and escenario.disco(b.ruta_de_salida).is_file()
    assert {e.ruta for e in escenario.manifiesto().entradas} == {a.ruta_de_salida, b.ruta_de_salida}


async def test_retirar_no_incluidas_borra_solo_lo_nuestro_que_ya_no_se_pide(escenario: Escenario) -> None:
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)
    await escenario.lote([a])

    informe = await escenario.lote([b], parametros=ParametrosDelLote(escribir_revision=True, retirar_no_incluidas=True))

    assert informe.retiradas == (a.ruta_de_salida,) and informe.conservadas == ()
    assert not escenario.disco(a.ruta_de_salida).exists() and escenario.disco(b.ruta_de_salida).is_file()
    assert {e.ruta for e in escenario.manifiesto().entradas} == {b.ruta_de_salida}


async def test_una_revision_no_escrita_no_borra_la_salida_anterior_de_esa_textura(escenario: Escenario) -> None:
    normal = escenario.normal("muro01")
    await escenario.lote([normal])
    escenario.exportador.alturas.clear()

    informe = await escenario.lote(
        [normal], parametros=ParametrosDelLote(escala_y=-1.0)
    )  # otra clave, REVIEW, sin escribir

    assert informe.archivos[0].estado is EstadoDeArchivo.EN_REVISION
    assert informe.otras_rutas == ()  # sin nada que escribir no se abre la sesión: el manifiesto ni se mira
    assert escenario.disco(normal.ruta_de_salida).is_file()
    assert [e.ruta for e in escenario.manifiesto().entradas] == [normal.ruta_de_salida]
    assert escenario.exportador.alturas == []


# ---------------------------------------------------------------------------
# Contrato de entrada
# ---------------------------------------------------------------------------


async def test_dos_entradas_con_la_misma_ruta_de_salida_se_rechazan_antes_de_tocar_nada(escenario: Escenario) -> None:
    a = escenario.normal("muro01")
    b = dataclasses.replace(
        escenario.normal("muro02"), ruta_de_salida=a.ruta_de_salida.upper().replace("TEXTURES", "textures")
    )

    with pytest.raises(LoteInvalidoError, match="misma ruta de salida"):
        await escenario.lote([a, b])

    assert escenario.exportador.alturas == [] and not escenario.raiz.exists()


async def test_un_lote_vacio_no_abre_la_salida(escenario: Escenario) -> None:
    informe = await escenario.lote([])

    assert informe.archivos == () and informe.completo
    assert not escenario.raiz.exists()


async def test_la_huella_del_origen_es_la_de_los_bytes_que_se_decodificaron(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal = escenario.normal("muro01")
    decodificar = NormalDecoder.decode

    def decodificar_y_que_un_tercero_lo_cambie(self: NormalDecoder, path: Any) -> Any:
        resultado = decodificar(self, path)
        pathlib.Path(path).write_bytes(pathlib.Path(path).read_bytes() + b"\0")  # un tercero lo cambia
        return resultado

    monkeypatch.setattr(NormalDecoder, "decode", decodificar_y_que_un_tercero_lo_cambie)

    informe = await escenario.lote([normal])

    assert informe.archivos[0].estado is EstadoDeArchivo.NO_PROCESADA
    assert "cambió" in informe.archivos[0].detalle
    assert escenario.exportador.alturas == []


async def test_un_origen_que_no_es_un_archivo_regular_no_se_procesa(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    normal = escenario.normal("muro01")
    carpeta = dataclasses.replace(normal, ruta_absoluta=tmp_path)  # un directorio

    informe = await escenario.lote([carpeta])

    assert informe.archivos[0].estado is EstadoDeArchivo.NO_PROCESADA
    assert "archivo regular" in informe.archivos[0].detalle


async def test_el_directorio_de_trabajo_no_deja_restos_ni_siquiera_si_algo_falla(
    escenario: Escenario, tmp_path: pathlib.Path
) -> None:
    trabajo = tmp_path / "trabajo"
    trabajo.mkdir()
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)

    await escenario.lote([a, b], directorio_de_trabajo=trabajo)
    assert list(trabajo.iterdir()) == []

    async def explota(_altura: Any) -> _ResultadoDelExportador:
        raise RuntimeError("bug del exportador, no un fallo de texconv")

    with pytest.raises(RuntimeError, match="bug del exportador"):
        await escenario.lote([escenario.normal("muro03", fase=2.0)], exportador=explota, directorio_de_trabajo=trabajo)
    assert list(trabajo.iterdir()) == []


# ---------------------------------------------------------------------------
# El informe
# ---------------------------------------------------------------------------


async def test_el_informe_es_json_estable_y_no_filtra_rutas_absolutas(escenario: Escenario) -> None:
    informe = await escenario.lote([escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)])

    texto = json.dumps(informe.a_diccionario(), sort_keys=True)

    assert json.loads(texto) == informe.a_diccionario()
    assert str(escenario.base) not in texto
    cargado = json.loads(texto)
    assert cargado["conteos"] == {"ESCRITA": 2, "EN_REVISION": 0, "NO_PROCESADA": 0, "NO_ESCRITA": 0}
    assert cargado["archivos"][0]["metricas"]["acuerdo_p95_grados"] >= 0.0
    assert cargado["parametros"]["bordes"] == "periodicos"


def test_un_error_de_lectura_del_decodificador_es_un_error_tipado_del_modulo() -> None:
    """El lote atrapa los errores tipados de cada etapa (no ``Exception``): el vocabulario se enumera acá."""
    atrapados = set(lote.ERRORES_POR_ARCHIVO)
    assert ArchivoNormalInvalidoError.__mro__[1] in atrapados or any(
        issubclass(ArchivoNormalInvalidoError, e) for e in atrapados
    )
    codigo = pathlib.Path(lote.__file__).read_text(encoding="utf-8")
    for nodo in ast.walk(ast.parse(codigo)):
        if isinstance(nodo, ast.ExceptHandler):
            assert nodo.type is not None, "un except desnudo escondería los bugs"
            nombres = {n.id for n in ast.walk(nodo.type) if isinstance(n, ast.Name)}
            assert "Exception" not in nombres and "BaseException" not in nombres


# ---------------------------------------------------------------------------
# Segunda pasada (mutación): contratos congelados por igualdad literal
# ---------------------------------------------------------------------------


def test_los_parametros_de_procedencia_estan_congelados_por_igualdad_literal() -> None:
    """Son la parte de la clave de caché que pone el lote: cambiar un nombre o un valor por defecto invalida las cachés."""
    assert ParametrosDelLote().a_parametros_de_procedencia() == {
        "version_del_lote": "np-a0a.v1",
        "convencion": "directx",
        "bordes": "periodicos",
        "escala_x": 1.0,
        "escala_y": 1.0,
        "normalizacion": "min_max",
        "percentil_bajo": None,
        "percentil_alto": None,
    }
    completo = ParametrosDelLote(
        convencion=ConvencionNormal.OPENGL,
        bordes=Bordes.NEUMANN,
        escala_x=2,
        escala_y=-0.5,
        normalizacion=ModoNormalizacion.PERCENTILES,
        percentil_bajo=1,
        percentil_alto=99,
        escribir_revision=True,
        retirar_no_incluidas=True,
    )
    assert completo.a_diccionario() == {
        "version_del_lote": "np-a0a.v1",
        "convencion": "opengl",
        "bordes": "neumann",
        "escala_x": 2.0,
        "escala_y": -0.5,
        "normalizacion": "percentiles",
        "percentil_bajo": 1.0,
        "percentil_alto": 99.0,
        "escribir_revision": True,
        "retirar_no_incluidas": True,
    }
    assert all(
        type(completo.a_parametros_de_procedencia()[k]) is float for k in ("escala_x", "escala_y", "percentil_bajo")
    )


def test_los_valores_del_lote_son_inmutables_y_con_slots() -> None:
    informe = _informe_a_mano()
    for valor in (ParametrosDelLote(), informe, informe.archivos[0], informe.otras_rutas[0]):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(valor, dataclasses.fields(valor)[0].name, None)
        assert not hasattr(valor, "__dict__")


@pytest.mark.parametrize(
    ("kwargs", "texto"),
    [
        ({"escala_x": 0.0}, "escala_x"),
        ({"escala_y": 0.0}, "escala_y"),
        ({"percentil_bajo": 5.0}, "MIN_MAX no usa percentiles"),  # uno solo de los dos, con MIN_MAX
        ({"percentil_alto": 95.0}, "MIN_MAX no usa percentiles"),
        ({"normalizacion": ModoNormalizacion.PERCENTILES, "percentil_bajo": 5.0}, "percentil"),
    ],
)
def test_el_error_de_un_parametro_nombra_cual_es(kwargs: dict[str, Any], texto: str) -> None:
    with pytest.raises(ParametrosDelLoteInvalidosError, match=texto):
        ParametrosDelLote(**kwargs)


def _informe_a_mano() -> InformeDelLote:
    metricas = MetricasDeCalidad(
        acuerdo_p95_grados=3.5, signo=1, residuo_de_integrabilidad=0.25, razon_de_costura=1.5, no_finitos=0
    )
    escrita = ResultadoDeArchivo(
        "textures/a/x_n.dds",
        "textures/a/x_p.dds",
        EstadoDeArchivo.ESCRITA,
        EfectoDeSalida.PUBLICADA,
        Veredicto.REVIEW,
        ("SIN_UMBRALES",),
        metricas,
        "",
        Amplitud(percentil_bajo=2.0, valor_bajo=0.25, percentil_alto=98.0, valor_alto=0.75, desviacion=0.125),
    )
    fallida = ResultadoDeArchivo(
        "textures/a/y_n.dds",
        "textures/a/y_p.dds",
        EstadoDeArchivo.NO_PROCESADA,
        None,
        None,
        (),
        None,
        "ArchivoNormalInvalidoError: roto",
    )
    otras = (
        ResultadoDeRuta("textures/a/z_p.dds", EfectoDeSalida.OMITIDA, ""),
        ResultadoDeRuta("textures/a/w_p.dds", EfectoDeSalida.RETIRADA, "se retiró"),
    )
    return InformeDelLote(ParametrosDelLote(escribir_revision=True), (escrita, fallida), otras, ("may2026", _H_TEXCONV))


def test_el_informe_en_json_esta_congelado_por_igualdad_literal() -> None:
    assert _informe_a_mano().a_diccionario() == {
        "version_del_lote": "np-a0a.v1",
        "parametros": {
            "version_del_lote": "np-a0a.v1",
            "convencion": "directx",
            "bordes": "periodicos",
            "escala_x": 1.0,
            "escala_y": 1.0,
            "normalizacion": "min_max",
            "percentil_bajo": None,
            "percentil_alto": None,
            "escribir_revision": True,
            "retirar_no_incluidas": False,
        },
        "encoder": {"texconv_version": "may2026", "texconv_sha256": _H_TEXCONV},
        "conteos": {"ESCRITA": 1, "EN_REVISION": 0, "NO_PROCESADA": 1, "NO_ESCRITA": 0},
        "archivos": [
            {
                "ruta_de_origen": "textures/a/x_n.dds",
                "ruta_de_salida": "textures/a/x_p.dds",
                "estado": "ESCRITA",
                "efecto": "PUBLICADA",
                "veredicto": "REVIEW",
                "motivos": ["SIN_UMBRALES"],
                "metricas": {
                    "acuerdo_p95_grados": 3.5,
                    "signo": 1,
                    "residuo_de_integrabilidad": 0.25,
                    "razon_de_costura": 1.5,
                    "no_finitos": 0,
                },
                "amplitud": {
                    "percentil_bajo": 2.0,
                    "valor_bajo": 0.25,
                    "percentil_alto": 98.0,
                    "valor_alto": 0.75,
                    "desviacion": 0.125,
                },
                "detalle": "",
            },
            {
                "ruta_de_origen": "textures/a/y_n.dds",
                "ruta_de_salida": "textures/a/y_p.dds",
                "estado": "NO_PROCESADA",
                "efecto": None,
                "veredicto": None,
                "motivos": [],
                "metricas": None,
                "amplitud": None,
                "detalle": "ArchivoNormalInvalidoError: roto",
            },
        ],
        "otras_rutas": [
            {"ruta": "textures/a/z_p.dds", "efecto": "OMITIDA", "detalle": ""},
            {"ruta": "textures/a/w_p.dds", "efecto": "RETIRADA", "detalle": "se retiró"},
        ],
    }


def test_un_informe_sin_encoder_y_con_metricas_no_finitas_sigue_siendo_json_valido() -> None:
    metricas = MetricasDeCalidad(
        acuerdo_p95_grados=float("inf"),
        signo=-1,
        residuo_de_integrabilidad=float("nan"),
        razon_de_costura=1.0,
        no_finitos=3,
    )
    archivo = ResultadoDeArchivo("a", "b", EstadoDeArchivo.EN_REVISION, None, Veredicto.REVIEW, ("X",), metricas, "d")
    informe = InformeDelLote(ParametrosDelLote(), (archivo,), (), None)

    cargado = json.loads(json.dumps(informe.a_diccionario(), allow_nan=False))  # allow_nan=False rechaza NaN e inf

    assert cargado["encoder"] is None
    assert cargado["archivos"][0]["metricas"] == {
        "acuerdo_p95_grados": None,
        "signo": -1,
        "residuo_de_integrabilidad": None,
        "razon_de_costura": 1.0,
        "no_finitos": 3,
    }


def test_completo_conservadas_y_retiradas_se_derivan_de_los_estados_y_los_efectos() -> None:
    def archivo(estado: EstadoDeArchivo) -> ResultadoDeArchivo:
        return ResultadoDeArchivo("o", "s", estado, None, None, (), None, "")

    def informe(*estados: EstadoDeArchivo, otras: tuple[ResultadoDeRuta, ...] = ()) -> InformeDelLote:
        return InformeDelLote(ParametrosDelLote(), tuple(archivo(e) for e in estados), otras, None)

    assert informe().completo
    assert informe(EstadoDeArchivo.ESCRITA, EstadoDeArchivo.EN_REVISION).completo
    assert not informe(EstadoDeArchivo.NO_PROCESADA).completo
    assert not informe(EstadoDeArchivo.ESCRITA, EstadoDeArchivo.NO_ESCRITA).completo
    otras = (
        ResultadoDeRuta("b", EfectoDeSalida.OMITIDA, ""),
        ResultadoDeRuta("a", EfectoDeSalida.OMITIDA, ""),
        ResultadoDeRuta("c", EfectoDeSalida.RETIRADA, ""),
        ResultadoDeRuta("d", EfectoDeSalida.PRESERVADA, ""),
    )
    resumen = informe(EstadoDeArchivo.ESCRITA, otras=otras)
    assert resumen.conservadas == ("a", "b") and resumen.retiradas == ("c",)
    assert resumen.contar() == {
        EstadoDeArchivo.ESCRITA: 1,
        EstadoDeArchivo.EN_REVISION: 0,
        EstadoDeArchivo.NO_PROCESADA: 0,
        EstadoDeArchivo.NO_ESCRITA: 0,
    }


# ---------------------------------------------------------------------------
# El origen: huella acotada, antes y después de decodificar
# ---------------------------------------------------------------------------


@pytest.fixture
def tope_chico(monkeypatch: pytest.MonkeyPatch) -> int:
    """Un tope de 8 bytes y bloques de 3: los bordes del tope y los bloques se ejercen con archivos de unos bytes.

    El tope lo valida el lote; los bloques de lectura son de la lectura acotada, que es del proveedor (``provider.huella_acotada``)."""
    monkeypatch.setattr(lote, "TAMANO_MAXIMO_DEL_ARCHIVO", 8)
    monkeypatch.setattr(provider, "_BLOQUE", 3)
    return 8


@pytest.mark.parametrize("tamano", [1, 7, 8])
def test_la_huella_acotada_es_el_sha256_del_contenido_con_bloques_de_cualquier_tamano(
    tmp_path: pathlib.Path, tope_chico: int, tamano: int
) -> None:
    archivo = tmp_path / "a.bin"
    archivo.write_bytes(bytes(range(tamano)))

    assert lote._huella_acotada(archivo) == hashlib.sha256(bytes(range(tamano))).hexdigest()


@pytest.mark.parametrize(("tamano", "texto"), [(0, "entre 1 y 8 bytes, no 0"), (9, "entre 1 y 8 bytes, no 9")])
def test_la_huella_acotada_rechaza_lo_vacio_y_lo_que_pasa_el_tope_con_el_motivo(
    tmp_path: pathlib.Path, tope_chico: int, tamano: int, texto: str
) -> None:
    archivo = tmp_path / "a.bin"
    archivo.write_bytes(b"x" * tamano)

    with pytest.raises(lote.OrigenInvalidoError, match=texto):
        lote._huella_acotada(archivo)


def test_la_huella_acotada_no_lee_mas_que_el_tope_aunque_el_archivo_crezca(
    tmp_path: pathlib.Path, tope_chico: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    archivo = tmp_path / "a.bin"
    archivo.write_bytes(b"x" * 20)
    real = os.lstat

    def lstat_que_miente(ruta: Any) -> Any:
        info = real(ruta)
        return os.stat_result((info.st_mode, 0, 0, 0, 0, 0, 5, 0, 0, 0))  # dice 5 bytes: el archivo creció después

    monkeypatch.setattr(lote.os, "lstat", lstat_que_miente)

    with pytest.raises(lote.OrigenInvalidoError, match="creció mientras se leía"):
        lote._huella_acotada(archivo)


def test_la_huella_acotada_detecta_un_origen_que_se_achico_despues_de_medirlo(
    tmp_path: pathlib.Path, tope_chico: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antes el lote solo cortaba lo que CRECÍA; un origen que pierde bytes entre ``lstat`` y la lectura es igual de
    sospechoso (otro proceso lo está reescribiendo) y la lectura acotada del proveedor lo rechaza."""
    archivo = tmp_path / "a.bin"
    archivo.write_bytes(b"x" * 3)
    real = os.lstat

    def lstat_que_miente(ruta: Any) -> Any:
        info = real(ruta)
        return os.stat_result((info.st_mode, 0, 0, 0, 0, 0, 7, 0, 0, 0))  # dice 7 bytes: el archivo se achicó después

    monkeypatch.setattr(lote.os, "lstat", lstat_que_miente)

    with pytest.raises(lote.OrigenInvalidoError, match="cambió mientras se leía"):
        lote._huella_acotada(archivo)


def test_la_huella_acotada_no_deja_pasar_un_origen_que_no_se_puede_inspeccionar(tmp_path: pathlib.Path) -> None:
    with pytest.raises(lote.OrigenInvalidoError, match="no se pudo inspeccionar el origen"):
        lote._huella_acotada(tmp_path / "no_existe.bin")


def test_sin_la_version_de_una_dependencia_el_entorno_no_se_inventa(monkeypatch: pytest.MonkeyPatch) -> None:
    def sin_pillow(nombre: str) -> str:
        raise importlib.metadata.PackageNotFoundError(nombre)

    monkeypatch.setattr(lote.importlib.metadata, "version", sin_pillow)

    with pytest.raises(lote.LoteError, match="versión de una dependencia"):
        lote._entorno()


def test_el_entorno_tiene_las_versiones_instaladas_ordenadas_por_nombre() -> None:
    entorno = dict(lote._entorno())

    assert list(entorno) == ["numpy", "pillow", "plataforma", "python"]
    assert entorno["numpy"] == importlib.metadata.version("numpy")
    assert entorno["pillow"] == importlib.metadata.version("pillow")
    assert entorno["plataforma"] == sys.platform and entorno["python"] == platform.python_version()


# ---------------------------------------------------------------------------
# Progreso, directorio de trabajo y revisión
# ---------------------------------------------------------------------------


async def test_el_progreso_informa_posicion_total_y_ruta_de_origen_antes_de_cada_textura(escenario: Escenario) -> None:
    a, b = escenario.normal("muro01"), escenario.normal("muro02", fase=1.0)
    vistos: list[tuple[int, int, str]] = []

    await escenario.lote([a, b], progreso=lambda i, n, ruta: vistos.append((i, n, ruta)))

    assert vistos == [(1, 2, a.ruta_de_origen), (2, 2, b.ruta_de_origen)]


async def test_el_detalle_de_una_revision_no_escrita_dice_que_hay_que_pedirla(escenario: Escenario) -> None:
    informe = await escenario.lote([escenario.normal("muro01")], parametros=ParametrosDelLote())

    assert informe.archivos[0].detalle == ("el gate la mandó a revisión y no se pidió escribir las salidas en revisión")


async def test_el_directorio_de_trabajo_se_crea_con_prefijo_dentro_del_pedido_y_borrarlo_no_enmascara_el_resultado(
    escenario: Escenario, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pedidos: list[tuple[str, Any]] = []
    mkdtemp = tempfile.mkdtemp
    borrados: list[pathlib.Path] = []

    def espiar_mkdtemp(*, prefix: str, dir: Any) -> str:
        pedidos.append((prefix, dir))
        return mkdtemp(prefix=prefix, dir=dir)

    def borrar_y_fallar(ruta: pathlib.Path) -> int:
        borrados.append(ruta)
        raise PermissionError("no se puede borrar el directorio de trabajo")

    monkeypatch.setattr(lote.tempfile, "mkdtemp", espiar_mkdtemp)
    monkeypatch.setattr(lote, "rmtree_link_aware", borrar_y_fallar)
    base = tmp_path / "base"
    base.mkdir()

    with caplog.at_level("WARNING", logger=lote.logger.name):
        informe = await escenario.lote([escenario.normal("muro01")], directorio_de_trabajo=base)

    assert pedidos == [("skyclaw-parallax-", base)]
    assert [r.parent for r in borrados] == [base]  # el que borra es el directorio que se creó dentro del pedido
    assert informe.archivos[0].estado is EstadoDeArchivo.ESCRITA  # y no poder borrarlo no cambia el resultado
    assert "no se pudo borrar por completo el directorio de trabajo" in caplog.text


async def test_sin_parametros_el_lote_usa_los_de_la_referencia_y_no_escribe_revisiones(escenario: Escenario) -> None:
    informe = await procesar_lote(
        [escenario.normal("muro01")],
        raiz_de_salida=escenario.raiz,
        coordinacion=escenario.coordinacion,
        exportador=escenario.exportador,
    )

    assert informe.parametros == ParametrosDelLote()
    assert informe.archivos[0].estado is EstadoDeArchivo.EN_REVISION


def test_la_huella_acotada_pide_como_maximo_un_byte_mas_que_lo_que_midio_aunque_el_origen_no_termine_nunca(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La propiedad es de memoria y de tiempo: un origen que crece sin parar no se lee más allá del tamaño con el que se
    validó (lo que midió ``lstat``) más un byte. Es MÁS estricta que el tope de 1 GiB con el que el lote leía antes: la lectura
    acotada es ahora la del proveedor, que se acota por el tamaño de CADA archivo."""
    monkeypatch.setattr(lote, "TAMANO_MAXIMO_DEL_ARCHIVO", 8)
    monkeypatch.setattr(provider, "_BLOQUE", 5)
    pedidos: list[int] = []

    class OrigenInfinito:
        def __enter__(self) -> OrigenInfinito:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, cuanto: int) -> bytes:
            pedidos.append(cuanto)
            return b"x" * cuanto

    archivo = tmp_path / "a.bin"
    archivo.write_bytes(b"12345")  # lo que ve ``lstat``: 5 bytes, dentro del tope
    monkeypatch.setattr(provider, "open", lambda *_a, **_k: OrigenInfinito(), raising=False)

    with pytest.raises(lote.OrigenInvalidoError, match="creció mientras se leía"):
        lote._huella_acotada(archivo)

    assert pedidos == [5, 1] and sum(pedidos) == 5 + 1


# ---------------------------------------------------------------------------
# Revisión de #783: validar antes de gastar y no abandonar el hilo que escribe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raiz", ["sin_carpeta_madre", "otro_nombre", "relativa"])
async def test_una_raiz_inadmisible_se_rechaza_antes_de_procesar_ninguna_textura(
    escenario: Escenario, raiz: str
) -> None:
    """Un 4K tarda minutos y varios GiB: una configuración mala (``--mods`` que no existe) se dice antes, no después."""
    malas = {
        "sin_carpeta_madre": escenario.base / "no_existe" / NOMBRE_DEL_MOD_DE_SALIDA,
        "otro_nombre": escenario.base / "mods" / "otro_mod",
        "relativa": pathlib.Path("mods") / NOMBRE_DEL_MOD_DE_SALIDA,
    }
    normal = escenario.normal("muro01")

    with pytest.raises(SalidaNoEscribibleError):
        await procesar_lote(
            [normal],
            raiz_de_salida=malas[raiz],
            coordinacion=escenario.coordinacion,
            exportador=escenario.exportador,
            parametros=ParametrosDelLote(escribir_revision=True),
        )

    assert escenario.exportador.alturas == []
    assert not (escenario.base / "no_existe").exists()


async def test_cancelar_mientras_se_escribe_la_salida_intermedia_espera_al_hilo_antes_de_borrar_el_trabajo(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asyncio.to_thread`` no detiene el hilo al cancelarse: sin esperarlo, el borrado correría contra el archivo abierto
    (en Windows falla y el resto queda en el temporal)."""
    entro, liberar, termino = threading.Event(), threading.Event(), threading.Event()
    escribir_de_verdad = pathlib.Path.write_bytes

    def escribir(self: pathlib.Path, datos: Any, *args: Any, **kwargs: Any) -> int:
        if not self.name.endswith("_p.dds"):
            return escribir_de_verdad(self, datos, *args, **kwargs)
        entro.set()
        liberar.wait(10)
        resultado = escribir_de_verdad(self, datos, *args, **kwargs)
        termino.set()
        return resultado

    visto_al_borrar: list[bool] = []
    borrar_de_verdad = lote._borrar_trabajo

    def borrar(trabajo: pathlib.Path) -> None:
        visto_al_borrar.append(termino.is_set())
        borrar_de_verdad(trabajo)

    monkeypatch.setattr(pathlib.Path, "write_bytes", escribir)
    monkeypatch.setattr(lote, "_borrar_trabajo", borrar)
    normal = escenario.normal("muro01")
    tarea = asyncio.ensure_future(escenario.lote([normal]))
    try:
        assert await asyncio.to_thread(entro.wait, 10)
        tarea.cancel()
        asyncio.get_running_loop().call_later(0.2, liberar.set)
        with pytest.raises(asyncio.CancelledError):
            await tarea
    finally:
        liberar.set()

    assert visto_al_borrar == [True]
