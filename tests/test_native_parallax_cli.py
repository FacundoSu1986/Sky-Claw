"""NP-A0a: la línea de comandos del lote (``cli.py``) es un borde delgado sobre ``lote.procesar_lote``.

Qué se verifica: el adaptador de la enumeración de la carpeta (la hace ``provider.py``; acá se verifica lo que el operador
ve: no sigue enlaces, no pisa lo autoral, no deja pasar rutas inadmisibles ni duplicados, y qué errores son de uso), el mapeo de argumentos a ``ParametrosDelLote``, los códigos de salida, la política del gate
(sin ``--incluir-revision`` no se escribe), que ``autoprueba`` falla cuando la salida no es un BC4 válido, y que el
exportador real se ata al ``texconv`` verificado. El ``texconv`` real solo corre en Windows (``tests/test_texconv_real.py``):
acá el exportador es un doble.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
from typing import Any

import pytest

from sky_claw.local.native_parallax import cli, provider
from sky_claw.local.native_parallax.cli import (
    CODIGO_CON_FALLOS,
    CODIGO_DE_USO,
    CODIGO_INTERRUMPIDO,
    CODIGO_OK,
    CODIGO_SALIDA_OCUPADA,
    ENTORNO_TEXCONV,
    construir_parser,
    crear_exportador_texconv,
    descubrir_normales_de_carpeta,
    main,
)
from sky_claw.local.native_parallax.diagnostico_texconv import DiagnosticoDeTexconv, EstadoDeTexconv
from sky_claw.local.native_parallax.export import TexconvNoCoincideError, generar_mips
from sky_claw.local.native_parallax.integrate import Bordes
from sky_claw.local.native_parallax.lote import EstadoDeArchivo, InformeDelLote, ParametrosDelLote
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA, RutaDeSalidaInvalidaError
from sky_claw.local.native_parallax.output_writer import SalidaOcupadaError
from tests.test_native_parallax_export import _dds as _dds_bc4

np = pytest.importorskip("numpy")
imagen = pytest.importorskip("PIL.Image")

_H_TEXCONV = hashlib.sha256(b"texconv-de-prueba").hexdigest()
_ARGUMENTOS = ("-f", "BC4_UNORM", "-m", "0", "-y", "-o")
_CARPETA = "textures/architecture/whiterun"


class _Resultado:
    def __init__(self, datos: bytes) -> None:
        self.datos = datos
        self.texconv_version = "may2026"
        self.texconv_sha256 = _H_TEXCONV
        self.argumentos = _ARGUMENTOS


async def _exportador_falso(altura: Any) -> _Resultado:
    generar_mips(altura)  # lanza con lados que no son potencia de 2, como el real
    lado = altura.shape[0]
    return _Resultado(_dds_bc4(lado, lado, lado.bit_length()))


async def _exportador_que_devuelve_basura(altura: Any) -> _Resultado:
    return _Resultado(b"esto no es un DDS")


def _fabrica(exportador: Any = _exportador_falso) -> Any:
    return lambda _ruta: exportador


def _escribir_normal(ruta: pathlib.Path, n: int = 64, fase: float = 0.0) -> None:
    """Una normal de superficie periódica suave. Se guarda como PNG aunque el nombre diga ``.dds``: el decodificador
    mira el contenido, no la extensión (la enumeración mira el nombre)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    y, x = np.mgrid[0:n, 0:n] / n
    vector = np.stack(
        (
            -0.5 * np.cos(2 * np.pi * x + fase) * np.cos(2 * np.pi * y),
            0.5 * np.sin(2 * np.pi * x + fase) * np.sin(2 * np.pi * y),
            np.ones_like(x),
        ),
        axis=-1,
    )
    vector /= np.linalg.norm(vector, axis=-1, keepdims=True)
    imagen.fromarray(np.round((vector * 0.5 + 0.5) * 255).astype(np.uint8), mode="RGB").save(ruta, format="PNG")


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Una carpeta de entrada con dos normales y una carpeta ``mods`` vacía."""
    entrada = tmp_path / "entrada"
    _escribir_normal(entrada / _CARPETA / "muro01_n.dds")
    _escribir_normal(entrada / _CARPETA / "muro02_n.dds", fase=1.0)
    (tmp_path / "mods").mkdir()
    return tmp_path


def _correr(
    mundo: pathlib.Path, *argv: str, fabrica: Any = None, entorno: dict[str, str] | None = None
) -> tuple[int, str]:
    salida = io.StringIO()
    codigo = main(
        [*argv],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"} if entorno is None else entorno,
        fabrica_de_exportador=fabrica if fabrica is not None else _fabrica(),
        base_de_estado=mundo / "estado",
        salida=salida,
    )
    return codigo, salida.getvalue()


def _generar(mundo: pathlib.Path, *extra: str, **kwargs: Any) -> tuple[int, str]:
    informe = str(mundo / "informe.json")
    return _correr(
        mundo,
        "generar",
        "--carpeta",
        str(mundo / "entrada"),
        "--mods",
        str(mundo / "mods"),
        "--informe",
        informe,
        *extra,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Enumeración de la carpeta (adaptador de provider.py)
# ---------------------------------------------------------------------------


def _tocar(ruta: pathlib.Path) -> pathlib.Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"x")
    return ruta


def test_la_enumeracion_devuelve_las_normales_ordenadas_con_su_ruta_de_salida(tmp_path: pathlib.Path) -> None:
    _tocar(tmp_path / "textures" / "b" / "zeta_N.DDS")  # el sufijo no distingue mayúsculas, como Windows
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "alfa.dds")  # difusa: no es una normal
    _tocar(tmp_path / "textures" / "a" / "alfa_msn.dds")  # normal de espacio de modelo: tampoco
    _tocar(tmp_path / "textures" / "a" / "leeme.txt")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [(n.ruta_de_origen, n.ruta_de_salida) for n in normales] == [
        ("textures/a/alfa_n.dds", "textures/a/alfa_p.dds"),
        ("textures/b/zeta_N.DDS", "textures/b/zeta_p.dds"),
    ]
    assert omitidas == []


def test_el_adaptador_entrega_lo_que_descubre_el_proveedor_con_su_huella(tmp_path: pathlib.Path) -> None:
    """La CLI no tiene su propia idea de «una normal»: devuelve las ``NormalDescubierta`` del proveedor, con el tamaño y la
    huella del contenido que éste calculó."""
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")

    normales, _ = descubrir_normales_de_carpeta(tmp_path)

    assert isinstance(normales[0], provider.NormalDescubierta)
    assert normales[0].tamano == 1
    assert normales[0].origen.sha256 == hashlib.sha256(b"x").hexdigest()


def test_una_normal_con_su_parallax_al_lado_no_se_pisa(tmp_path: pathlib.Path) -> None:
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "alfa_p.dds")  # autoral
    _tocar(tmp_path / "textures" / "a" / "beta_n.dds")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/beta_n.dds"]
    assert len(omitidas) == 1 and "alfa_n.dds" in omitidas[0] and "autoral" in omitidas[0]


def test_la_enumeracion_no_sigue_enlaces_ni_de_archivos_ni_de_carpetas(tmp_path: pathlib.Path) -> None:
    real = _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    fuera = tmp_path.parent / f"{tmp_path.name}_fuera"
    _tocar(fuera / "oculta_n.dds")
    try:
        os.symlink(real, tmp_path / "textures" / "a" / "enlace_n.dds")
        os.symlink(fuera, tmp_path / "textures" / "carpeta_enlazada", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear symlinks")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/alfa_n.dds"]
    assert sorted(o.split(":")[0] for o in omitidas) == ["textures/a/enlace_n.dds", "textures/carpeta_enlazada"]
    assert all("enlace" in o for o in omitidas)


def test_una_carpeta_de_entrada_enlazada_se_rechaza(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real"
    _tocar(real / "textures" / "a" / "alfa_n.dds")
    try:
        os.symlink(real, tmp_path / "enlace", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear symlinks")

    with pytest.raises(cli.UsoIncorrectoError, match="enlace"):
        descubrir_normales_de_carpeta(tmp_path / "enlace")


@pytest.mark.parametrize("preparar", ["sin_textures", "no_existe"])
def test_una_carpeta_sin_textures_o_inexistente_se_rechaza(tmp_path: pathlib.Path, preparar: str) -> None:
    carpeta = tmp_path / "x"
    if preparar == "sin_textures":
        carpeta.mkdir()

    with pytest.raises(cli.UsoIncorrectoError):
        descubrir_normales_de_carpeta(carpeta)


def test_una_ruta_de_salida_inadmisible_se_omite_y_se_dice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Qué nombre es inadmisible lo decide ``output.validar_ruta_de_salida`` (con sus propios tests); acá se verifica la
    REACCIÓN de la enumeración. No se fabrica un nombre malo en disco: en Windows un ``:`` en el nombre no crea un archivo
    con ese nombre sino un flujo de datos alternativo de otro (el CI de Windows lo mostró)."""
    _tocar(tmp_path / "textures" / "a" / "buena_n.dds")
    _tocar(tmp_path / "textures" / "a" / "mala_n.dds")
    real = provider.validar_ruta_de_salida

    def validar(ruta: str) -> str:
        if "mala" in ruta:
            raise RutaDeSalidaInvalidaError("caracter no admitido")
        return real(ruta)

    monkeypatch.setattr(provider, "validar_ruta_de_salida", validar)

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/buena_n.dds"]
    assert omitidas == ["textures/a/mala_n.dds: la ruta de salida no es admisible (caracter no admitido)"]


def test_dos_normales_que_dan_la_misma_salida_se_omiten_las_dos(tmp_path: pathlib.Path) -> None:
    _tocar(tmp_path / "textures" / "a" / "Alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "beta_n.dds")
    if len({p.name for p in (tmp_path / "textures" / "a").iterdir()}) < 3:
        pytest.skip("este sistema no distingue mayúsculas en los nombres")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/beta_n.dds"]
    # Diferencia con la enumeración provisoria (una línea por PAR): el proveedor informa una omisión por ARCHIVO, y el
    # informe nombra las dos rutas de entrada en vez de una salida.
    assert len(omitidas) == 2 and all("misma salida" in o for o in omitidas)
    assert sorted(o.split(":")[0] for o in omitidas) == ["textures/a/Alfa_n.dds", "textures/a/alfa_n.dds"]


# ---------------------------------------------------------------------------
# generar
# ---------------------------------------------------------------------------


def test_sin_pedirlo_no_se_escribe_nada_y_el_informe_dice_por_que(mundo: pathlib.Path) -> None:
    codigo, texto = _generar(mundo)

    assert codigo == CODIGO_OK
    assert "--incluir-revision" in texto and "2 en_revision" in texto
    assert not (mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA).exists()
    informe = json.loads((mundo / "informe.json").read_text(encoding="utf-8"))
    assert informe["conteos"]["EN_REVISION"] == 2
    assert all(a["metricas"] is not None for a in informe["archivos"])  # los datos para calibrar se miden igual


def test_con_incluir_revision_se_escriben_las_salidas_y_se_informan(mundo: pathlib.Path) -> None:
    codigo, texto = _generar(mundo, "--incluir-revision")

    assert codigo == CODIGO_OK
    salida = mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    assert sorted(p.name for p in (salida / _CARPETA).iterdir()) == ["muro01_p.dds", "muro02_p.dds"]
    assert "2 escrita" in texto and "Informe:" in texto
    assert json.loads((mundo / "informe.json").read_text(encoding="utf-8"))["conteos"]["ESCRITA"] == 2


def test_los_argumentos_llegan_a_los_parametros_del_lote(mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict[str, Any] = {}

    async def falso(normales: Any, **kwargs: Any) -> InformeDelLote:
        capturado.update(kwargs)
        return InformeDelLote(parametros=kwargs["parametros"], archivos=(), otras_rutas=(), encoder=None)

    monkeypatch.setattr(cli, "procesar_lote", falso)

    codigo, _ = _generar(
        mundo,
        "--convencion",
        "opengl",
        "--bordes",
        "neumann",
        "--escala-x",
        "2",
        "--escala-y",
        "-1",
        "--normalizacion",
        "percentiles",
        "--percentil-bajo",
        "5",
        "--percentil-alto",
        "95",
        "--incluir-revision",
        "--retirar-no-incluidas",
    )

    assert codigo == CODIGO_OK
    parametros = capturado["parametros"]
    assert isinstance(parametros, ParametrosDelLote)
    assert parametros.a_diccionario() == {
        "version_del_lote": "np-a0a.v1",
        "convencion": "opengl",
        "bordes": "neumann",
        "escala_x": 2.0,
        "escala_y": -1.0,
        "normalizacion": "percentiles",
        "percentil_bajo": 5.0,
        "percentil_alto": 95.0,
        "escribir_revision": True,
        "retirar_no_incluidas": True,
    }
    assert capturado["raiz_de_salida"] == (mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA).absolute()


def test_una_textura_que_falla_da_codigo_1_y_las_demas_se_escriben(mundo: pathlib.Path) -> None:
    (mundo / "entrada" / _CARPETA / "muro02_n.dds").write_bytes(b"roto")

    codigo, texto = _generar(mundo, "--incluir-revision")

    assert codigo == CODIGO_CON_FALLOS
    assert "NO_PROCESADA" in texto
    assert (mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA / _CARPETA / "muro01_p.dds").is_file()


def test_sin_texconv_no_se_arranca_y_se_dice_donde_ponerlo(mundo: pathlib.Path) -> None:
    llamadas: list[pathlib.Path] = []

    def fabrica(ruta: pathlib.Path) -> Any:
        llamadas.append(ruta)
        return _exportador_falso

    codigo, texto = _generar(mundo, entorno={}, fabrica=fabrica)

    assert codigo == CODIGO_DE_USO and ENTORNO_TEXCONV in texto and "--texconv" in texto
    assert llamadas == []


def test_el_texconv_sale_del_argumento_y_si_no_de_la_variable_de_entorno(mundo: pathlib.Path) -> None:
    vistas: list[pathlib.Path] = []

    def fabrica(ruta: pathlib.Path) -> Any:
        vistas.append(ruta)
        return _exportador_falso

    _generar(mundo, fabrica=fabrica, entorno={ENTORNO_TEXCONV: "C:/de_el_entorno.exe"})
    _generar(
        mundo, "--texconv", "C:/del_argumento.exe", fabrica=fabrica, entorno={ENTORNO_TEXCONV: "C:/de_el_entorno.exe"}
    )

    assert vistas == [pathlib.Path("C:/de_el_entorno.exe"), pathlib.Path("C:/del_argumento.exe")]


def test_un_texconv_que_no_coincide_con_el_pin_es_un_error_de_uso_antes_de_procesar(mundo: pathlib.Path) -> None:
    def fabrica(_ruta: pathlib.Path) -> Any:
        raise TexconvNoCoincideError("la huella de texconv.exe no coincide con la del pin may2026")

    codigo, texto = _generar(mundo, fabrica=fabrica)

    assert codigo == CODIGO_DE_USO and "no coincide" in texto
    assert not (mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA).exists()


def test_una_carpeta_sin_normales_es_un_error_de_uso(mundo: pathlib.Path) -> None:
    vacia = mundo / "vacia" / "textures"
    vacia.mkdir(parents=True)

    codigo, texto = _correr(
        mundo,
        "generar",
        "--carpeta",
        str(vacia.parent),
        "--mods",
        str(mundo / "mods"),
        "--informe",
        str(mundo / "i.json"),
    )

    assert codigo == CODIGO_DE_USO and "no encontré ningún" in texto


@pytest.mark.parametrize(
    ("excepcion", "codigo_esperado"),
    [(SalidaOcupadaError("otra instancia"), CODIGO_SALIDA_OCUPADA), (KeyboardInterrupt(), CODIGO_INTERRUMPIDO)],
)
def test_los_codigos_de_salida_de_una_salida_ocupada_y_de_una_interrupcion(
    mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, excepcion: BaseException, codigo_esperado: int
) -> None:
    async def falso(*_args: Any, **_kwargs: Any) -> InformeDelLote:
        raise excepcion

    monkeypatch.setattr(cli, "procesar_lote", falso)

    codigo, texto = _generar(mundo)

    assert codigo == codigo_esperado and texto.strip()


def test_el_parser_exige_lo_que_no_tiene_valor_por_defecto() -> None:
    with pytest.raises(SystemExit) as salida:
        construir_parser().parse_args(["generar"])
    assert salida.value.code == 2
    with pytest.raises(SystemExit):
        construir_parser().parse_args([])
    argumentos = construir_parser().parse_args(["generar", "--carpeta", "a", "--mods", "b"])
    assert (argumentos.bordes, argumentos.convencion, argumentos.normalizacion) == ("periodicos", "directx", "min_max")
    assert argumentos.incluir_revision is False and argumentos.retirar_no_incluidas is False


# ---------------------------------------------------------------------------
# Revisión de #783: validar antes de gastar, no tomar lo no observado por ausente, y el informe fuera de la salida
# ---------------------------------------------------------------------------


def _exportador_que_cuenta(llamadas: list[Any]) -> Any:
    async def exportar(altura: Any) -> _Resultado:
        llamadas.append(altura.shape)
        return await _exportador_falso(altura)

    return exportar


def test_un_mods_que_no_existe_se_rechaza_antes_de_procesar_ninguna_textura(mundo: pathlib.Path) -> None:
    llamadas: list[Any] = []

    codigo, texto = _correr(
        mundo,
        "generar",
        "--carpeta",
        str(mundo / "entrada"),
        "--mods",
        str(mundo / "no_existe"),
        "--informe",
        str(mundo / "i.json"),
        fabrica=_fabrica(_exportador_que_cuenta(llamadas)),
    )

    assert codigo == CODIGO_DE_USO and "debe existir" in texto
    assert llamadas == []
    assert not (mundo / "no_existe").exists()


def test_un_textures_que_es_un_junction_se_rechaza_aunque_is_dir_lo_vea_como_carpeta(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En Windows ``DirEntry.is_dir(follow_symlinks=False)`` es verdadero para un junction: se mira con el helper que lo
    reconoce, igual que a todo lo que está debajo."""
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        tipo, identidad = real(ruta)
        return ("junction", identidad) if ruta.name == "textures" else (tipo, identidad)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(cli.UsoIncorrectoError, match=r"'textures' es un enlace \(junction\)"):
        descubrir_normales_de_carpeta(tmp_path)


def test_un_textures_que_no_se_puede_inspeccionar_es_un_error_de_uso(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        if ruta.name == "textures":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(cli.UsoIncorrectoError, match="no se pudo inspeccionar .*textures.*bloqueado por el antivirus"):
        descubrir_normales_de_carpeta(tmp_path)


@pytest.mark.parametrize("fallo", ["carpeta_ilegible", "archivo_sin_inspeccionar"])
def test_exigir_completa_aborta_si_algo_no_se_pudo_observar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fallo: str
) -> None:
    """Sin verlo no se sabe si está: retirar salidas por «no figura» sería tomar lo no observado por ausente."""
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "b" / "beta_n.dds")
    if fallo == "carpeta_ilegible":
        _con_scandir_que_falla_en(monkeypatch, "b")
    else:
        real = provider.link_kind_and_identity_or_raise

        def inspeccionar(ruta: pathlib.Path) -> Any:
            if ruta.name == "beta_n.dds":
                raise PermissionError("bloqueado por el antivirus")
            return real(ruta)

        monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(cli.UsoIncorrectoError, match="enumeración quedó incompleta.*acceso denegado|bloqueado"):
        descubrir_normales_de_carpeta(tmp_path, exigir_completa=True)


def test_exigir_completa_nombra_todo_lo_que_no_se_pudo_ver_separado_por_punto_y_coma(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El operador tiene que saber QUÉ carpetas no se vieron, todas, para arreglarlas antes de pedir que se retire algo."""
    for nombre in ("a", "b", "c"):
        _tocar(tmp_path / "textures" / nombre / f"{nombre}_n.dds")
    real = os.scandir

    def scandir(ruta: Any = ".") -> Any:
        if pathlib.Path(ruta).name in {"b", "c"}:
            raise PermissionError("acceso denegado")
        return real(ruta)

    monkeypatch.setattr(cli.os, "scandir", scandir)

    with pytest.raises(cli.UsoIncorrectoError) as error:
        descubrir_normales_de_carpeta(tmp_path, exigir_completa=True)

    assert (
        "textures/b: no se pudo leer la carpeta (acceso denegado); textures/c: no se pudo leer la carpeta (acceso denegado)"
        in str(error.value)
    )
    assert "textures/a" not in str(error.value)  # lo que sí se vio no se acusa


def test_si_textures_cambia_despues_de_enumerar_el_informe_no_se_da_por_seguro(tmp_path: pathlib.Path) -> None:
    """``--informe`` se compara contra las texturas de entrada con la MISMA resolución del descubrimiento; si ésta ya no
    puede resolverlas (la entrada cambió desde que se enumeró), se corta en vez de suponer que el informe es seguro."""
    (tmp_path / "entrada").mkdir()  # sin ``textures``: lo que vio el descubrimiento ya no está

    with pytest.raises(cli.UsoIncorrectoError, match="subcarpeta 'textures'"):
        cli._exigir_informe_fuera_de_la_salida(
            str(tmp_path / "informe.json"), tmp_path / "mods" / NOMBRE_DEL_MOD_DE_SALIDA, tmp_path / "entrada"
        )


def test_exigir_completa_no_aborta_por_omisiones_deliberadas(tmp_path: pathlib.Path) -> None:
    """Un ``*_p.dds`` autoral al lado es una decisión de la política, no algo que no se pudo ver."""
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "alfa_p.dds")
    _tocar(tmp_path / "textures" / "a" / "beta_n.dds")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path, exigir_completa=True)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/beta_n.dds"]
    assert len(omitidas) == 1 and "alfa_n.dds" in omitidas[0]


def test_con_retirar_una_enumeracion_incompleta_aborta_y_no_borra_las_salidas_anteriores(
    mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _escribir_normal(mundo / "entrada" / "textures" / "otra" / "extra_n.dds", fase=2.0)
    codigo, _ = _generar(mundo, "--incluir-revision")
    raiz = mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    previas = sorted(raiz.rglob("*_p.dds"))
    assert codigo == CODIGO_OK and len(previas) == 3
    real = os.scandir

    def scandir(ruta: Any = ".") -> Any:
        if pathlib.Path(ruta) == mundo / "entrada" / _CARPETA:
            raise PermissionError("acceso denegado")
        return real(ruta)

    monkeypatch.setattr(cli.os, "scandir", scandir)

    codigo, texto = _generar(mundo, "--incluir-revision", "--retirar-no-incluidas")

    assert codigo == CODIGO_DE_USO and "incompleta" in texto
    assert all(p.is_file() for p in previas)


@pytest.mark.parametrize(
    "donde", ["manifiesto", "salida_generada", "ruta_nueva_dentro_del_mod", "por_un_enlace", "origen"]
)
def test_el_informe_no_puede_escribirse_dentro_de_la_salida_gestionada_ni_sobre_una_textura_de_origen(
    mundo: pathlib.Path, donde: str
) -> None:
    """Escribirlo después de cerrar la sesión saltearía el lock, la procedencia y la recuperación."""
    assert _generar(mundo, "--incluir-revision")[0] == CODIGO_OK
    raiz = mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    if donde == "por_un_enlace":
        enlace = mundo / "enlace_al_mod"
        try:
            os.symlink(raiz, enlace, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("este sistema no permite crear symlinks")
    destino = {
        "manifiesto": raiz / ".skyclaw-output" / "manifest.json",
        "salida_generada": raiz / _CARPETA / "muro01_p.dds",
        "ruta_nueva_dentro_del_mod": raiz / "informe.json",
        "por_un_enlace": mundo / "enlace_al_mod" / ".skyclaw-output" / "manifest.json",
        "origen": mundo / "entrada" / _CARPETA / "muro01_n.dds",
    }[donde]
    antes = destino.read_bytes() if destino.exists() else None
    llamadas: list[Any] = []

    codigo, texto = _generar(
        mundo, "--incluir-revision", "--informe", str(destino), fabrica=_fabrica(_exportador_que_cuenta(llamadas))
    )

    assert codigo == CODIGO_DE_USO and "--informe" in texto
    assert llamadas == []
    assert (destino.read_bytes() if destino.exists() else None) == antes


@pytest.mark.parametrize("donde", ["textura_omitida_por_tener_un_p_autoral", "archivo_nuevo_en_textures"])
def test_el_informe_no_puede_apuntar_dentro_de_las_texturas_de_entrada_aunque_esa_textura_se_omita(
    mundo: pathlib.Path, donde: str
) -> None:
    """Una normal con un ``*_p.dds`` autoral al lado se omite de la lista a procesar, pero sigue siendo del operador."""
    carpeta = mundo / "entrada" / _CARPETA
    _tocar(carpeta / "autoral_n.dds")
    _tocar(carpeta / "autoral_p.dds")
    destino = {
        "textura_omitida_por_tener_un_p_autoral": carpeta / "autoral_p.dds",
        "archivo_nuevo_en_textures": mundo / "entrada" / "textures" / "informe.json",
    }[donde]
    antes = destino.read_bytes() if destino.exists() else None
    llamadas: list[Any] = []

    codigo, texto = _generar(mundo, "--informe", str(destino), fabrica=_fabrica(_exportador_que_cuenta(llamadas)))

    assert codigo == CODIGO_DE_USO and "--informe" in texto
    assert llamadas == []
    assert (destino.read_bytes() if destino.exists() else None) == antes


def test_con_retirar_una_carpeta_sin_normales_sigue_siendo_un_error_de_uso_y_no_retira_nada(
    mundo: pathlib.Path,
) -> None:
    """Una ``--carpeta`` equivocada (vacía) con ``--retirar-no-incluidas`` retiraría TODO lo generado antes: se frena."""
    assert _generar(mundo, "--incluir-revision")[0] == CODIGO_OK
    raiz = mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    previas = sorted(raiz.rglob("*_p.dds"))
    assert len(previas) == 2
    vacia = mundo / "vacia" / "textures"
    vacia.mkdir(parents=True)

    codigo, texto = _correr(
        mundo,
        "generar",
        "--carpeta",
        str(vacia.parent),
        "--mods",
        str(mundo / "mods"),
        "--informe",
        str(mundo / "i.json"),
        "--retirar-no-incluidas",
    )

    assert codigo == CODIGO_DE_USO and "no encontré ningún" in texto
    assert all(p.is_file() for p in previas)


def test_un_informe_junto_al_mod_pero_fuera_de_el_sigue_funcionando(mundo: pathlib.Path) -> None:
    codigo, _ = _generar(mundo, "--incluir-revision", "--informe", str(mundo / "mods" / "informe_parallax.json"))

    assert codigo == CODIGO_OK
    assert (mundo / "mods" / "informe_parallax.json").is_file()


# ---------------------------------------------------------------------------
# autoprueba
# ---------------------------------------------------------------------------


def test_la_autoprueba_corre_la_tuberia_completa_y_verifica_el_bc4(tmp_path: pathlib.Path) -> None:
    salida = io.StringIO()

    codigo = main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(),
        base_de_estado=tmp_path / "estado",
        salida=salida,
    )

    assert codigo == CODIGO_OK and "AUTOPRUEBA OK" in salida.getvalue()
    assert json.loads((tmp_path / "i.json").read_text(encoding="utf-8"))["conteos"]["ESCRITA"] == 1


def test_la_autoprueba_falla_si_la_salida_no_es_un_bc4_valido(tmp_path: pathlib.Path) -> None:
    salida = io.StringIO()

    codigo = main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(_exportador_que_devuelve_basura),
        base_de_estado=tmp_path / "estado",
        salida=salida,
    )

    assert codigo == CODIGO_CON_FALLOS and "AUTOPRUEBA FALLÓ" in salida.getvalue()
    assert "BC4" in salida.getvalue()


def test_sin_base_de_estado_la_cli_coordina_en_la_base_durable_por_usuario_y_no_en_el_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El lock cross-process solo excluye a la app y a la CLI si las dos lo toman en la MISMA base (la durable)."""
    from sky_claw.config import SystemPaths

    estado = tmp_path / "estado_durable"
    otro_cwd = tmp_path / "otro_cwd"
    otro_cwd.mkdir()
    monkeypatch.setattr(SystemPaths, "runtime_state_dir", classmethod(lambda cls: estado))
    monkeypatch.chdir(otro_cwd)

    codigo = main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(),
        salida=io.StringIO(),
    )

    assert codigo == CODIGO_OK
    assert (estado / "native_parallax_output").is_dir()
    assert list(otro_cwd.iterdir()) == []


def test_la_autoprueba_borra_su_carpeta_temporal_sin_atravesar_enlaces(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Es el hermano del directorio de trabajo del lote: ``TemporaryDirectory`` borra con ``shutil.rmtree``, que en
    Windows atraviesa un junction. Que el árbol sea propio no habilita la excepción."""
    creadas: list[pathlib.Path] = []
    mkdtemp_real = cli.tempfile.mkdtemp

    def mkdtemp(*args: Any, **kwargs: Any) -> str:
        ruta = mkdtemp_real(*args, **kwargs)
        if pathlib.Path(ruta).name.startswith("skyclaw-autoprueba-"):
            creadas.append(pathlib.Path(ruta))
        return ruta

    borradas: list[pathlib.Path] = []
    borrar_real = cli.rmtree_link_aware

    def borrar(ruta: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        borradas.append(pathlib.Path(ruta))
        borrar_real(ruta, *args, **kwargs)

    monkeypatch.setattr(cli.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(cli, "rmtree_link_aware", borrar)

    codigo = main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(),
        base_de_estado=tmp_path / "estado",
        salida=io.StringIO(),
    )

    assert codigo == CODIGO_OK
    assert len(creadas) == 1 and borradas == creadas and not creadas[0].exists()


@pytest.mark.parametrize(
    ("exportador", "codigo_esperado", "titulo"),
    [
        (_exportador_falso, CODIGO_OK, "AUTOPRUEBA OK"),
        (_exportador_que_devuelve_basura, CODIGO_CON_FALLOS, "AUTOPRUEBA FALLÓ"),
    ],
)
def test_un_fallo_al_borrar_la_carpeta_temporal_no_tapa_el_resultado_de_la_autoprueba(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    exportador: Any,
    codigo_esperado: int,
    titulo: str,
) -> None:
    """En Windows un antivirus puede tener abierto un archivo recién escrito: el operador tiene que ver el resultado
    de la prueba, no un traceback."""

    def borrar(_ruta: pathlib.Path, *_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("el antivirus lo tiene abierto")

    monkeypatch.setattr(cli, "rmtree_link_aware", borrar)
    salida = io.StringIO()

    codigo = main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(exportador),
        base_de_estado=tmp_path / "estado",
        salida=salida,
    )

    texto = salida.getvalue()
    assert codigo == codigo_esperado and titulo in texto
    assert "Aviso: no se pudo borrar la carpeta temporal" in texto and "el antivirus lo tiene abierto" in texto


def test_el_png_de_la_autoprueba_es_un_png_valido_con_los_pixeles_pedidos(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / "prueba.png"

    cli._escribir_normal_sintetica(ruta, 16)

    with imagen.open(ruta) as leida:
        assert (leida.format, leida.mode, leida.size) == ("PNG", "RGB", (16, 16))
        pixeles = np.asarray(leida)
    assert pixeles[..., 2].min() > 128  # la componente z de una normal que mira hacia arriba


# ---------------------------------------------------------------------------
# El exportador real se ata al texconv verificado
# ---------------------------------------------------------------------------


def test_un_texconv_con_otra_huella_no_crea_el_exportador(tmp_path: pathlib.Path) -> None:
    falso = tmp_path / "texconv.exe"
    falso.write_bytes(b"no soy el texconv oficial")

    with pytest.raises(cli.UsoIncorrectoError, match="su huella SHA-256 es otra"):
        crear_exportador_texconv(falso)


def _diagnostico(estado: EstadoDeTexconv) -> DiagnosticoDeTexconv:
    configurado = estado is not EstadoDeTexconv.NO_CONFIGURADO
    return DiagnosticoDeTexconv(
        estado=estado,
        ruta="C:/tools/texconv.exe" if configurado else None,
        origen_de_la_ruta="argumento" if configurado else None,
        version_esperada="may2026",
        mensaje=f"mensaje propio de {estado.name}",
    )


@pytest.mark.parametrize("estado", [e for e in EstadoDeTexconv if e is not EstadoDeTexconv.CORRECTO])
def test_todo_estado_que_no_es_correcto_corta_el_exportador_con_el_mensaje_del_diagnostico(
    estado: EstadoDeTexconv, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENUMERA el vocabulario del diagnóstico: un estado nuevo queda cableado solo (todo lo que no es CORRECTO corta), y
    el operador lee la frase del diagnóstico y no una propia de la CLI."""
    monkeypatch.setattr(cli, "diagnosticar_texconv", lambda ruta, **_kw: _diagnostico(estado))

    with pytest.raises(cli.UsoIncorrectoError) as excepcion:
        crear_exportador_texconv(tmp_path / "texconv.exe")

    assert str(excepcion.value) == f"mensaje propio de {estado.name}"


def test_la_cli_diagnostica_el_binario_que_resolvio_y_no_lee_el_entorno_del_proceso(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Se le pasa la ruta ya resuelta y un entorno vacío: la variable la lee UNA vez, al resolver, no otra vez adentro."""
    pedidos: list[tuple[Any, Any]] = []

    def diagnostico(ruta: Any, **kwargs: Any) -> DiagnosticoDeTexconv:
        pedidos.append((ruta, kwargs.get("entorno")))
        return _diagnostico(EstadoDeTexconv.CORRECTO)

    monkeypatch.setattr(cli, "diagnosticar_texconv", diagnostico)

    crear_exportador_texconv(tmp_path / "texconv.exe")

    assert pedidos == [(tmp_path / "texconv.exe", {})]


def test_con_el_exportador_real_un_texconv_equivocado_se_explica_antes_de_tocar_la_salida(
    mundo: pathlib.Path,
) -> None:
    falso = mundo / "texconv.exe"
    falso.write_bytes(b"no soy el texconv oficial")

    codigo, texto = _generar(mundo, "--texconv", str(falso), fabrica=crear_exportador_texconv)

    assert codigo == CODIGO_DE_USO
    assert texto.startswith("Error: El archivo ")
    assert "su huella SHA-256 es otra" in texto
    assert not (mundo / "mods" / NOMBRE_DEL_MOD_DE_SALIDA).exists()


async def test_el_exportador_real_llama_al_ejecutor_fijado_con_el_binario_verificado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools import texconv_runner

    verificados: list[pathlib.Path] = []
    llamadas: list[tuple[Any, pathlib.Path]] = []
    binario = tmp_path / "texconv.exe"

    async def ejecutor(altura: Any, *, texconv: pathlib.Path, **kwargs: Any) -> str:
        llamadas.append((altura, texconv))
        return "resultado del ejecutor"

    def diagnostico(ruta: pathlib.Path, **_kwargs: Any) -> DiagnosticoDeTexconv:
        verificados.append(ruta)
        return _diagnostico(EstadoDeTexconv.CORRECTO)

    monkeypatch.setattr(cli, "diagnosticar_texconv", diagnostico)
    monkeypatch.setattr(texconv_runner, "exportar_altura_bc4", ejecutor)

    exportador = crear_exportador_texconv(binario)
    resultado = await exportador("una altura")  # type: ignore[arg-type]

    assert verificados == [binario] and resultado == "resultado del ejecutor"
    assert llamadas == [("una altura", binario)]


def test_los_valores_por_defecto_son_los_de_la_referencia_sintetica() -> None:
    parametros = ParametrosDelLote()
    assert parametros.bordes is Bordes.PERIODICOS and parametros.escala_x == parametros.escala_y == 1.0
    assert parametros.escribir_revision is False and parametros.retirar_no_incluidas is False
    assert EstadoDeArchivo.EN_REVISION in set(EstadoDeArchivo)


# ---------------------------------------------------------------------------
# Segunda pasada (mutación)
# ---------------------------------------------------------------------------


def _con_scandir_que_falla_en(monkeypatch: pytest.MonkeyPatch, nombre: str) -> None:
    real = os.scandir

    def scandir(ruta: Any = ".") -> Any:
        if pathlib.Path(ruta).name == nombre:
            raise PermissionError("acceso denegado")
        return real(ruta)

    monkeypatch.setattr(cli.os, "scandir", scandir)


def test_una_carpeta_de_entrada_ilegible_es_un_error_de_uso(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _con_scandir_que_falla_en(monkeypatch, tmp_path.name)

    with pytest.raises(cli.UsoIncorrectoError, match="no se pudo leer la carpeta"):
        descubrir_normales_de_carpeta(tmp_path)


def test_una_subcarpeta_ilegible_se_omite_y_se_dice_sin_frenar_las_demas(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "b" / "beta_n.dds")
    _con_scandir_que_falla_en(monkeypatch, "b")

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/alfa_n.dds"]
    assert omitidas == ["textures/b: no se pudo leer la carpeta (acceso denegado)"]


def test_un_archivo_que_no_se_puede_inspeccionar_se_omite_y_se_dice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(tmp_path / "textures" / "a" / "alfa_n.dds")
    _tocar(tmp_path / "textures" / "a" / "beta_n.dds")
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        if ruta.name == "beta_n.dds":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    normales, omitidas = descubrir_normales_de_carpeta(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a/alfa_n.dds"]
    assert omitidas == ["textures/a/beta_n.dds: no se pudo inspeccionar (bloqueado por el antivirus)"]


@pytest.mark.parametrize(
    ("como", "mensaje"),
    [("archivo", "'textures' no es una carpeta"), ("enlace", "'textures' es un enlace")],
)
def test_textures_que_no_es_una_carpeta_real_no_cuenta(tmp_path: pathlib.Path, como: str, mensaje: str) -> None:
    if como == "archivo":
        _tocar(tmp_path / "textures")
    else:
        real = tmp_path.parent / f"{tmp_path.name}_real"
        _tocar(real / "a" / "alfa_n.dds")
        try:
            os.symlink(real, tmp_path / "textures", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("este sistema no permite crear symlinks")

    # Diferencia con la enumeración provisoria (que decía «debe tener una subcarpeta 'textures'» en ambos casos): el
    # proveedor distingue el caso, y el operador sabe qué arreglar.
    with pytest.raises(cli.UsoIncorrectoError, match=mensaje):
        descubrir_normales_de_carpeta(tmp_path)


def test_mas_archivos_que_el_tope_se_rechazan_y_justo_el_tope_pasa(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_MAXIMO_DE_ARCHIVOS_DE_ENTRADA", 2)
    for nombre in ("a", "b"):
        _tocar(tmp_path / "textures" / f"{nombre}_n.dds")

    normales, _ = descubrir_normales_de_carpeta(tmp_path)
    assert len(normales) == 2

    _tocar(tmp_path / "textures" / "c_n.dds")
    with pytest.raises(cli.UsoIncorrectoError, match="más de 2 entradas relevantes"):
        descubrir_normales_de_carpeta(tmp_path)


# --- presentación ------------------------------------------------------------------------------------------------


def _informe_de_muestra(*, escribir_revision: bool = False) -> InformeDelLote:
    from sky_claw.local.native_parallax.gates import MetricasDeCalidad, Veredicto
    from sky_claw.local.native_parallax.lote import ResultadoDeArchivo, ResultadoDeRuta
    from sky_claw.local.native_parallax.output_writer import EfectoDeSalida

    metricas = MetricasDeCalidad(
        acuerdo_p95_grados=3.5, signo=1, residuo_de_integrabilidad=0.25, razon_de_costura=1.5, no_finitos=0
    )
    archivos = (
        ResultadoDeArchivo(
            "o1",
            "s1",
            EstadoDeArchivo.ESCRITA,
            EfectoDeSalida.PUBLICADA,
            Veredicto.REVIEW,
            ("SIN_UMBRALES", "OTRO"),
            metricas,
            "listo",
        ),
        ResultadoDeArchivo(
            "o2", "s2", EstadoDeArchivo.EN_REVISION, None, Veredicto.REVIEW, ("SIN_UMBRALES",), metricas, "no escrita"
        ),
        ResultadoDeArchivo(
            "o3", "s3", EstadoDeArchivo.NO_PROCESADA, None, None, (), None, "ArchivoNormalInvalidoError: roto"
        ),
        ResultadoDeArchivo(
            "o4", "s4", EstadoDeArchivo.ESCRITA, EfectoDeSalida.OMITIDA, Veredicto.AUTO_ACCEPT, (), metricas, ""
        ),
    )
    otras = (ResultadoDeRuta("anterior_p.dds", EfectoDeSalida.OMITIDA, "caché vigente"),)
    return InformeDelLote(ParametrosDelLote(escribir_revision=escribir_revision), archivos, otras, None)


def test_cada_textura_se_muestra_con_su_estado_efecto_metricas_gate_y_detalle() -> None:
    informe = _informe_de_muestra()

    lineas = [cli._linea_de_archivo(a) for a in informe.archivos]

    assert lineas[0] == (
        "  [ESCRITA → PUBLICADA] s1\n"
        "      acuerdo p95 3.50°, signo +1, residuo 0.2500, costura 1.500\n"
        "      gate: REVIEW (SIN_UMBRALES, OTRO)\n"
        "      listo"
    )
    # una revisión no escrita no repite el detalle (el resumen ya explica qué hacer)
    assert lineas[1] == (
        "  [EN_REVISION] s2\n      acuerdo p95 3.50°, signo +1, residuo 0.2500, costura 1.500\n      gate: REVIEW (SIN_UMBRALES)"
    )
    assert lineas[2] == "  [NO_PROCESADA] s3\n      ArchivoNormalInvalidoError: roto"  # sin métricas ni gate
    assert lineas[3] == (
        "  [ESCRITA → OMITIDA] s4\n      acuerdo p95 3.50°, signo +1, residuo 0.2500, costura 1.500\n      gate: AUTO_ACCEPT"
    )


def test_el_resumen_lista_todo_cuenta_y_sugiere_incluir_la_revision_solo_si_corresponde() -> None:
    salida = io.StringIO()

    cli._resumen(_informe_de_muestra(), ["textures/x: es un enlace"], salida)

    texto = salida.getvalue()
    assert texto.startswith("\n  [ESCRITA → PUBLICADA] s1")
    assert "  [OMITIDA] anterior_p.dds (corrida anterior) caché vigente\n" in texto
    assert "  [OMITIDA] textures/x: es un enlace\n" in texto
    assert "\nResumen: 2 escrita, 1 en_revision, 1 no_procesada, 0 no_escrita, 1 omitidas al enumerar.\n" in texto
    assert "--incluir-revision" in texto

    pedida = io.StringIO()
    cli._resumen(_informe_de_muestra(escribir_revision=True), [], pedida)
    assert "--incluir-revision" not in pedida.getvalue()  # ya se pidió

    sin_revision = io.StringIO()
    informe = _informe_de_muestra()
    cli._resumen(InformeDelLote(informe.parametros, informe.archivos[:1], (), None), [], sin_revision)
    assert "--incluir-revision" not in sin_revision.getvalue()  # no hay nada en revisión


def test_el_progreso_muestra_posicion_total_y_ruta() -> None:
    salida = io.StringIO()

    cli._progreso(salida)(2, 5, "textures/a/x_n.dds")

    assert salida.getvalue() == "[2/5] textures/a/x_n.dds\n"


def test_el_informe_json_queda_legible_ordenado_y_con_acentos(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "carpeta nueva" / "anidada" / "informe.json"
    salida = io.StringIO()

    cli._escribir_informe(_informe_de_muestra(), ["textures/x_n.dds: ilegible"], str(destino), salida)

    texto = destino.read_text(encoding="utf-8")
    assert texto.endswith("}\n") and not texto.endswith("\n\n")
    assert texto.startswith('{\n  "archivos": [\n    {\n      "amplitud": null,\n      "detalle": "listo"')  # ordenadas
    assert json.loads(texto)["otras_rutas"][0]["detalle"] == "caché vigente"
    # El censo de lo omitido viaja en el informe: sin él, un lote con texturas afuera es indistinguible de uno completo.
    assert json.loads(texto)["omitidas_al_enumerar"] == ["textures/x_n.dds: ilegible"]
    assert "caché vigente" in texto  # ensure_ascii=False: los acentos no se escapan
    assert salida.getvalue() == f"Informe: {destino.resolve()}\n"


def test_si_el_informe_no_se_puede_escribir_se_avisa_y_no_se_pierde_el_resultado(tmp_path: pathlib.Path) -> None:
    salida = io.StringIO()

    cli._escribir_informe(_informe_de_muestra(), [], str(tmp_path), salida)  # un directorio, no un archivo

    assert salida.getvalue().startswith(f"No se pudo escribir el informe en {tmp_path}:")


def test_generar_anuncia_cuantas_texturas_procesa_y_hacia_donde_va(mundo: pathlib.Path) -> None:
    _, texto = _generar(mundo)

    assert texto.splitlines()[0] == f"2 texturas por procesar; salida: {mundo / 'mods' / NOMBRE_DEL_MOD_DE_SALIDA}"
    assert (
        "[1/2] textures/architecture/whiterun/muro01_n.dds\n[2/2] textures/architecture/whiterun/muro02_n.dds" in texto
    )


# --- autoprueba: cada fallo tiene su mensaje ---------------------------------------------------------------------


async def _salida_de_autoprueba(tmp_path: pathlib.Path, datos: bytes) -> tuple[pathlib.Path, str, InformeDelLote]:
    """Corre el lote sobre una normal sintética con un exportador que devuelve *datos*; devuelve raíz, ruta e informe."""
    from sky_claw.local.native_parallax.lote import procesar_lote
    from sky_claw.local.native_parallax.output_writer import construir_coordinacion_de_salida

    origen = tmp_path / "entrada" / "prueba_n.png"
    origen.parent.mkdir(parents=True)
    cli._escribir_normal_sintetica(origen, 128)
    (tmp_path / "mods").mkdir()
    raiz = tmp_path / "mods" / NOMBRE_DEL_MOD_DE_SALIDA
    ruta = "textures/skyclaw_autoprueba/prueba_p.dds"
    coordinacion = construir_coordinacion_de_salida(base=tmp_path / "estado")

    async def exportador(_altura: Any) -> _Resultado:
        return _Resultado(datos)

    try:
        informe = await procesar_lote(
            [cli.NormalDeCarpeta("textures/skyclaw_autoprueba/prueba_n.dds", ruta, origen)],
            raiz_de_salida=raiz,
            coordinacion=coordinacion,
            exportador=exportador,
            parametros=ParametrosDelLote(escribir_revision=True),
        )
    finally:
        await coordinacion.close()
    return raiz, ruta, informe


async def test_la_verificacion_de_la_autoprueba_acepta_un_bc4_del_tamano_pedido_con_su_manifiesto(
    tmp_path: pathlib.Path,
) -> None:
    raiz, ruta, informe = await _salida_de_autoprueba(tmp_path, _dds_bc4(128, 128, 8))

    assert cli._verificar_autoprueba(raiz, ruta, informe) == []


async def test_la_verificacion_de_la_autoprueba_dice_cada_cosa_que_falla(tmp_path: pathlib.Path) -> None:
    raiz, ruta, informe = await _salida_de_autoprueba(
        tmp_path, _dds_bc4(64, 64, 7)
    )  # un BC4 válido pero del tamaño equivocado

    assert cli._verificar_autoprueba(raiz, ruta, informe) == ["el BC4 tiene 64x64 y se esperaba 128x128"]

    manifiesto = raiz / ".skyclaw-output" / "manifest.json"
    guardado = manifiesto.read_bytes()
    manifiesto.write_bytes(b"{}")  # ilegible como manifiesto
    assert cli._verificar_autoprueba(raiz, ruta, informe)[-1].startswith("el manifiesto no se pudo leer:")
    manifiesto.unlink()
    assert cli._verificar_autoprueba(raiz, ruta, informe)[-1].startswith("el manifiesto no se pudo leer:")
    manifiesto.write_bytes(guardado)
    otra = (
        raiz / "textures" / "otra" / "cosa_p.dds"
    )  # un BC4 del tamaño pedido en una ruta que el manifiesto no describe
    otra.parent.mkdir(parents=True)
    otra.write_bytes(_dds_bc4(128, 128, 8))
    assert cli._verificar_autoprueba(raiz, "textures/otra/cosa_p.dds", informe) == [
        "el manifiesto no describe exactamente la salida de la prueba"
    ]

    raiz.joinpath(*ruta.split("/")).write_bytes(b"basura")  # ya no es un BC4
    assert cli._verificar_autoprueba(raiz, ruta, informe)[0].startswith("la salida no es un BC4 válido:")
    assert len(cli._verificar_autoprueba(raiz, ruta, informe)) == 1  # y no sigue con lo que depende de que lo sea

    incompleto = InformeDelLote(informe.parametros, (), (), None)
    assert cli._verificar_autoprueba(raiz, ruta, incompleto) == ["la textura sintética no quedó escrita (ver arriba)"]


def test_la_autoprueba_anuncia_lo_que_hace_y_su_resultado(tmp_path: pathlib.Path) -> None:
    salida = io.StringIO()
    main(
        ["autoprueba", "--informe", str(tmp_path / "i.json")],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(_exportador_que_devuelve_basura),
        base_de_estado=tmp_path / "estado",
        salida=salida,
    )

    texto = salida.getvalue()
    assert "Autoprueba sobre una textura sintética de 128x128 (mod temporal).\n" in texto
    assert "[1/1] textures/skyclaw_autoprueba/prueba_n.dds" in texto
    assert "textures/skyclaw_autoprueba/prueba_p.dds" in texto
    assert "\nAUTOPRUEBA FALLÓ:\n  - " in texto


# --- main: valores por defecto y mensajes --------------------------------------------------------------------------


def test_main_sin_argumentos_con_nombre_usa_el_entorno_la_salida_estandar_y_el_exportador_real(
    mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pedidos: list[pathlib.Path] = []

    def exportador_real(ruta: pathlib.Path) -> Any:
        pedidos.append(ruta)
        return _exportador_falso

    monkeypatch.setattr(cli, "crear_exportador_texconv", exportador_real)
    monkeypatch.setenv(ENTORNO_TEXCONV, "C:/del_entorno_real.exe")
    monkeypatch.setattr(
        cli,
        "construir_coordinacion_de_salida",
        lambda base=None: construir_coordinacion_de_salida(base=mundo / "estado"),
    )
    from sky_claw.local.native_parallax.output_writer import construir_coordinacion_de_salida

    codigo = main(
        [
            "generar",
            "--carpeta",
            str(mundo / "entrada"),
            "--mods",
            str(mundo / "mods"),
            "--informe",
            str(mundo / "i.json"),
        ]
    )

    assert codigo == CODIGO_OK
    assert pedidos == [pathlib.Path("C:/del_entorno_real.exe")]
    assert "2 texturas por procesar" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("excepcion", "fragmento"),
    [
        (SalidaOcupadaError("otra instancia"), "La salida está en uso por otra instancia: otra instancia"),
        (KeyboardInterrupt(), "Interrumpido. Si había un lote a medias, la próxima corrida lo recupera sola."),
    ],
)
def test_los_mensajes_de_una_salida_ocupada_y_de_una_interrupcion_dicen_que_pasa(
    mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, excepcion: BaseException, fragmento: str
) -> None:
    async def falso(*_args: Any, **_kwargs: Any) -> InformeDelLote:
        raise excepcion

    monkeypatch.setattr(cli, "procesar_lote", falso)

    _, texto = _generar(mundo)

    assert texto.strip().endswith(fragmento)


def test_un_error_de_uso_se_muestra_sin_traceback_con_el_prefijo_error(mundo: pathlib.Path) -> None:
    _, texto = _generar(mundo, entorno={})

    assert texto.startswith("Error: No me dijeron dónde está el texconv")
    assert "--texconv" in texto and ENTORNO_TEXCONV in texto


def test_el_png_de_la_autoprueba_tiene_el_encabezado_y_los_crc_correctos(tmp_path: pathlib.Path) -> None:
    import struct
    import zlib

    datos = cli._png_rgb(bytes(range(12)) * 4, 4, 4)

    assert datos[:8] == b"\x89PNG\r\n\x1a\n"
    posicion, trozos = 8, []
    while posicion < len(datos):
        largo = struct.unpack(">I", datos[posicion : posicion + 4])[0]
        cuerpo = datos[posicion + 4 : posicion + 8 + largo]
        assert struct.unpack(">I", datos[posicion + 8 + largo : posicion + 12 + largo])[0] == zlib.crc32(cuerpo)
        trozos.append((cuerpo[:4], cuerpo[4:]))
        posicion += 12 + largo
    assert [etiqueta for etiqueta, _ in trozos] == [b"IHDR", b"IDAT", b"IEND"]
    assert trozos[0][1] == struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)  # 8 bits, RGB, sin entrelazado
    assert zlib.decompress(trozos[1][1]) == b"".join(
        b"\x00" + (bytes(range(12)) * 4)[f * 12 : (f + 1) * 12] for f in range(4)
    )


def test_si_la_integracion_no_converge_la_textura_queda_no_procesada_y_el_lote_sigue(
    mundo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``SinConvergenciaError`` es un ``IntegracionError``: falla UNA ruta (``ERRORES_POR_ARCHIVO``), no la corrida entera.
    from sky_claw.local.native_parallax import integrate

    monkeypatch.setattr(integrate, "MAX_ITERACIONES", 0)

    codigo, texto = _generar(mundo, "--incluir-revision")

    assert codigo == CODIGO_CON_FALLOS
    assert texto.count("NO_PROCESADA") >= 2 and "SinConvergenciaError" in texto
    informe = json.loads((mundo / "informe.json").read_text(encoding="utf-8"))
    assert informe["conteos"]["NO_PROCESADA"] == 2


# ---------------------------------------------------------------------------
# La salida no depende del codepage de la consola (rig 2026-10-24: «→» en cp1252 tiraba UnicodeEncodeError)
# ---------------------------------------------------------------------------

# Codificaciones ESTRICTAS que la consola de Windows o una redirección pueden imponer: la ANSI (cp1252), las OEM
# (cp437, cp850) y la más pobre (ascii). Cada una tiene caracteres de los mensajes que no puede representar.
_CODIFICACIONES_ESTRICTAS = ("cp1252", "cp437", "cp850", "ascii")


def _comando(nombre: str, mundo: pathlib.Path) -> list[str]:
    informe = str(mundo / "informe.json")
    if nombre == "autoprueba":
        return ["autoprueba", "--informe", informe]
    carpetas = ["--carpeta", str(mundo / "entrada"), "--mods", str(mundo / "mods")]
    return ["generar", *carpetas, "--informe", informe, "--incluir-revision"]


def _correr_con(mundo: pathlib.Path, argv: list[str], salida: Any) -> int:
    return main(
        argv,
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=_fabrica(),
        base_de_estado=mundo / "estado",
        salida=salida,
    )


@pytest.mark.parametrize("codificacion", _CODIFICACIONES_ESTRICTAS)
@pytest.mark.parametrize("comando", ["generar", "autoprueba"])
def test_ningun_comando_revienta_por_el_codepage_y_el_informe_se_escribe(
    mundo: pathlib.Path, comando: str, codificacion: str
) -> None:
    crudo = io.BytesIO()
    salida = io.TextIOWrapper(crudo, encoding=codificacion, errors="strict", newline="")

    codigo = _correr_con(mundo, _comando(comando, mundo), salida)

    salida.flush()
    texto = crudo.getvalue().decode(codificacion)
    assert codigo == CODIGO_OK
    assert "Resumen:" in texto and "Informe:" in texto
    # Ninguna de las cuatro representa «→»: se escapa visible, no se pierde ni se inventa.
    assert "\\u2192" in texto
    assert json.loads((mundo / "informe.json").read_text(encoding="utf-8"))["conteos"]["ESCRITA"] >= 1


class _ConsolaQueSeCae(io.StringIO):
    """Una consola que deja de aceptar texto: en el resumen (después de escribir la salida) o desde el principio."""

    def __init__(self, desde: str) -> None:
        super().__init__()
        self._desde = desde
        self._caida = False

    def write(self, texto: str) -> int:
        if self._desde in texto or self._caida:
            self._caida = True
            raise OSError(22, "la consola se cerró")
        return super().write(texto)


# Dónde se cae la consola: en el resumen (la corrida ya escribió la salida) o en la primera línea que se imprime.
_CAIDAS_DE_LA_CONSOLA = {"en-el-resumen": "Resumen:", "desde-el-principio": ""}


@pytest.mark.parametrize("caida", sorted(_CAIDAS_DE_LA_CONSOLA))
@pytest.mark.parametrize("comando", ["generar", "autoprueba"])
def test_una_consola_que_se_cae_no_aborta_la_corrida_ni_se_lleva_el_informe(
    mundo: pathlib.Path, comando: str, caida: str
) -> None:
    # Imprimir es lo único que depende de la consola: el informe (la evidencia de la corrida) y el código de salida, no.
    codigo = _correr_con(mundo, _comando(comando, mundo), _ConsolaQueSeCae(_CAIDAS_DE_LA_CONSOLA[caida]))

    assert codigo == CODIGO_OK
    assert json.loads((mundo / "informe.json").read_text(encoding="utf-8"))["conteos"]["ESCRITA"] >= 1


def test_un_error_que_no_es_de_la_consola_no_se_esconde(mundo: pathlib.Path) -> None:
    # La salida tolera la consola, no los bugs: un error que no es de E/S sigue subiendo.
    class _Rota(io.StringIO):
        def write(self, texto: str) -> int:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        _correr_con(mundo, _comando("generar", mundo), _Rota())


def test_la_salida_sin_codepage_es_un_flujo_de_texto_de_solo_escritura() -> None:
    crudo = io.BytesIO()
    salida = cli._sin_codepage(io.TextIOWrapper(crudo, encoding="cp1252", errors="strict", newline=""))

    assert salida.writable() and not salida.readable()
    with pytest.raises(io.UnsupportedOperation):
        salida.read()
    salida.write("a → b\n")
    salida.flush()
    assert crudo.getvalue() == b"a \\u2192 b\n"


@pytest.mark.parametrize("escribir_revision", [False, True])
def test_el_resumen_avisa_que_un_mapa_sin_contenido_no_se_escribe_ni_con_incluir_revision(
    escribir_revision: bool,
) -> None:
    from sky_claw.local.native_parallax.gates import Veredicto
    from sky_claw.local.native_parallax.lote import MOTIVO_SIN_CONTENIDO, ResultadoDeArchivo

    plana = ResultadoDeArchivo(
        "o1", "s1", EstadoDeArchivo.EN_REVISION, None, Veredicto.REVIEW, (MOTIVO_SIN_CONTENIDO,), None, "sin contenido"
    )
    informe = InformeDelLote(ParametrosDelLote(escribir_revision=escribir_revision), (plana,), (), None)
    salida = io.StringIO()

    cli._resumen(informe, [], salida)

    texto = salida.getvalue()
    assert "1 sin contenido" in texto and "ni con --incluir-revision" in texto
