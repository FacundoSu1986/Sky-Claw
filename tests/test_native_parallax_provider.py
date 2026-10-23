"""NP-A0b: el proveedor de normales de carpeta plana (``provider.py``) descubre los ``*_n.dds`` aptos, solo leyendo.

Qué se verifica (cada propiedad con su test; las familias se enumeran, no se muestrean):

- descubre las normales bajo ``<raíz>/textures`` con su ruta de origen, su ruta de salida ya validada, su tamaño y la huella
  de su contenido, ordenadas de forma determinista y compatibles con lo que consume el lote (``lote.NormalAProcesar``);
- cada motivo de omisión del vocabulario cerrado tiene su caso, y lo no observado (carpeta o archivo que no se pudo ver)
  NUNCA se presenta como una enumeración completa: quien retira salidas por «ya no figura» la necesita completa (#783);
- nunca sigue enlaces (symlink ni junction), ni dentro del árbol ni en ``textures``; los ancestros de la raíz son del
  operador y no se rechazan;
- la lectura está acotada por el tamaño con el que se validó el archivo, un archivo que falla no aborta el descubrimiento y un
  exceso de entradas se rechaza en vez de truncarse;
- es de solo lectura y no decide política: lo fijan anclas por AST sobre sus imports, sus llamadas al disco y sus escrituras.

Nunca se fabrica en disco un nombre que Windows no admite (``:``, ``<``, ``|``…): en NTFS un ``:`` crea un flujo de datos
alternativo de otro archivo y el árbol queda distinto al que el test cree (lo mostró el CI de Windows en #783). Para probar
cómo reacciona el proveedor ante una ruta inválida se deja el nombre en disco normal y se sustituye el validador.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import pathlib
from typing import Any

import pytest

from sky_claw.local.native_parallax import provider
from sky_claw.local.native_parallax.lote import NormalAProcesar
from sky_claw.local.native_parallax.output import (
    OrigenDeEntrada,
    RutaDeSalidaInvalidaError,
    clave_de_ruta,
)
from sky_claw.local.native_parallax.provider import (
    MAXIMO_DE_ARCHIVOS_POR_DEFECTO,
    MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO,
    DemasiadosArchivosError,
    Descubrimiento,
    LimiteInvalidoError,
    MotivoDeOmision,
    NormalDescubierta,
    Omitida,
    ProveedorError,
    RaizInvalidaError,
    descubrir_normales,
)
from tests._symlink_guard import crear_junction

M = MotivoDeOmision


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


def _tocar(ruta: pathlib.Path, contenido: bytes = b"x") -> pathlib.Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    return ruta


@pytest.fixture
def raiz(tmp_path: pathlib.Path) -> pathlib.Path:
    """La raíz de una carpeta (o de un mod): todavía vacía, con su carpeta ``textures``."""
    carpeta = tmp_path / "mod"
    (carpeta / "textures").mkdir(parents=True)
    return carpeta


def _enlazar(tipo: str, destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Crea *enlace* → *destino* como symlink o como junction (que ``is_symlink()`` no ve y solo existe en Windows)."""
    if tipo == "symlink":
        try:
            os.symlink(destino, enlace, target_is_directory=destino.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("este sistema no permite crear symlinks")
        return
    if os.name != "nt":
        pytest.skip("los junctions solo existen en Windows")
    # Sin skip en Windows: crear un junction no pide privilegios, así que un fallo es el helper roto, no el entorno.
    if (motivo := crear_junction(enlace, destino)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")


def _origenes(d: Descubrimiento) -> list[str]:
    return [n.ruta_de_origen for n in d.normales]


def _omitidas(d: Descubrimiento) -> list[tuple[str, MotivoDeOmision]]:
    return [(o.ruta_de_origen, o.motivo) for o in d.omitidas]


def _foto(carpeta: pathlib.Path) -> dict[str, tuple[str, int, int, int]]:
    """Lo que un test puede comparar para decir «no se tocó nada»: tipo, tamaño, fecha y modo de cada entrada."""
    foto: dict[str, tuple[str, int, int, int]] = {}
    for ruta in sorted(carpeta.rglob("*")):
        info = ruta.lstat()
        tipo = "d" if ruta.is_dir() else "l" if ruta.is_symlink() else "f"
        foto[ruta.relative_to(carpeta).as_posix()] = (tipo, info.st_size, info.st_mtime_ns, info.st_mode)
    return foto


def _distingue_mayusculas(carpeta: pathlib.Path) -> bool:
    _tocar(carpeta / "sonda_a")
    _tocar(carpeta / "SONDA_A")
    return len({p.name for p in carpeta.iterdir() if p.name.casefold() == "sonda_a"}) == 2


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


def test_descubre_las_normales_con_su_ruta_de_salida_tamano_y_huella(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "muros" / "alfa_n.dds", b"ALFA")
    _tocar(raiz / "textures" / "suelo" / "piedra" / "beta_n.dds", b"BETA!")

    d = descubrir_normales(raiz)

    assert d.normales == (
        NormalDescubierta(
            ruta_de_origen="textures/muros/alfa_n.dds",
            ruta_de_salida="textures/muros/alfa_p.dds",
            ruta_absoluta=raiz / "textures" / "muros" / "alfa_n.dds",
            tamano=4,
            origen=OrigenDeEntrada("normal", hashlib.sha256(b"ALFA").hexdigest()),
        ),
        NormalDescubierta(
            ruta_de_origen="textures/suelo/piedra/beta_n.dds",
            ruta_de_salida="textures/suelo/piedra/beta_p.dds",
            ruta_absoluta=raiz / "textures" / "suelo" / "piedra" / "beta_n.dds",
            tamano=5,
            origen=OrigenDeEntrada("normal", hashlib.sha256(b"BETA!").hexdigest()),
        ),
    )
    assert d.omitidas == () and d.completo is True


def test_lo_que_no_es_una_normal_no_se_descubre_ni_se_cuenta_como_omision(raiz: pathlib.Path) -> None:
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "alfa_n.dds")
    _tocar(carpeta / "alfa.dds")  # difusa
    _tocar(carpeta / "alfa_msn.dds")  # normal de espacio de modelo
    _tocar(carpeta / "alfa_n.png")
    _tocar(carpeta / "leeme.txt")
    _tocar(carpeta / "ZETA_N.DDS")  # el sufijo no distingue mayúsculas, como Windows

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds", "textures/a/ZETA_N.DDS"]
    assert [n.ruta_de_salida for n in d.normales] == ["textures/a/alfa_p.dds", "textures/a/ZETA_p.dds"]
    assert d.omitidas == ()


def test_la_carpeta_textures_se_busca_sin_distinguir_mayusculas_y_conserva_su_nombre_en_disco(
    tmp_path: pathlib.Path,
) -> None:
    _tocar(tmp_path / "mod" / "Textures" / "a" / "alfa_n.dds")

    d = descubrir_normales(tmp_path / "mod")

    assert _origenes(d) == ["Textures/a/alfa_n.dds"]
    assert d.normales[0].ruta_de_salida == "Textures/a/alfa_p.dds"


def test_una_raiz_relativa_se_vuelve_absoluta_sin_resolver_enlaces(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    monkeypatch.chdir(raiz.parent)

    d = descubrir_normales(pathlib.Path("mod"))

    assert d.normales[0].ruta_absoluta.is_absolute()
    assert d.normales[0].ruta_absoluta == raiz / "textures" / "a" / "alfa_n.dds"


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
def test_los_ancestros_de_la_raiz_los_elige_el_operador_y_no_se_rechazan(tmp_path: pathlib.Path, tipo: str) -> None:
    """MO2 puede vivir bajo un junction (otro disco): lo ajeno es lo de ADENTRO del árbol, no el camino hasta él."""
    real = tmp_path / "real"
    _tocar(real / "mod" / "textures" / "a" / "alfa_n.dds")
    _enlazar(tipo, real, tmp_path / "padre")

    d = descubrir_normales(tmp_path / "padre" / "mod")

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert d.normales[0].ruta_absoluta == tmp_path / "padre" / "mod" / "textures" / "a" / "alfa_n.dds"


# ---------------------------------------------------------------------------
# Motivos de omisión (un caso por miembro del vocabulario cerrado)
# ---------------------------------------------------------------------------


def test_ya_tiene_parallax_una_normal_con_su_p_al_lado_no_se_pisa(raiz: pathlib.Path) -> None:
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "alfa_n.dds")
    _tocar(carpeta / "alfa_p.dds", b"AUTORAL")  # puede ser autoral
    _tocar(carpeta / "beta_n.dds")
    _tocar(carpeta / "BETA_P.DDS")  # el hermano tampoco distingue mayúsculas
    _tocar(carpeta / "gama_n.dds")
    _tocar(carpeta / "otra" / "gama_p.dds")  # en OTRA carpeta no cuenta

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/gama_n.dds"]
    assert _omitidas(d) == [
        ("textures/a/alfa_n.dds", M.YA_TIENE_PARALLAX),
        ("textures/a/beta_n.dds", M.YA_TIENE_PARALLAX),
    ]
    assert "ya hay un" in d.omitidas[0].detalle
    assert d.completo is True  # es una decisión de la política, no algo que no se pudo ver


@pytest.mark.parametrize("tipo", ["carpeta", "symlink"])
def test_ya_tiene_parallax_cualquier_entrada_llamada_p_cuenta_aunque_no_sea_un_archivo_regular(
    tmp_path: pathlib.Path, raiz: pathlib.Path, tipo: str
) -> None:
    """Fail-closed por diseño: una carpeta o un enlace llamado ``x_p.dds`` puede ser el autoral de alguien (p. ej. un enlace a
    su copia), y escribir la salida en esa ruta lo taparía; la normal se omite y la omisión lo dice. Es lo que ya hacía la
    enumeración por carpeta (todos los nombres), ahora global. Se fija para que nadie lo «corrija» a favor de procesar
    (Reviewer Guide, #784)."""
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "x_n.dds")
    if tipo == "carpeta":
        (carpeta / "x_p.dds").mkdir()
    else:
        _tocar(tmp_path / "fuera" / "copia.dds")
        _enlazar("symlink", tmp_path / "fuera" / "copia.dds", carpeta / "x_p.dds")
    _tocar(carpeta / "y_n.dds")  # sin nada llamado y_p.dds: se procesa

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/y_n.dds"]
    esperadas = [("textures/a/x_n.dds", M.YA_TIENE_PARALLAX)]
    if tipo == "symlink":
        esperadas.append(("textures/a/x_p.dds", M.ENLACE))  # el enlace en sí también se informa
    assert _omitidas(d) == esperadas
    assert d.completo is True


def test_ya_tiene_parallax_una_p_en_otra_carpeta_que_solo_difiere_en_mayusculas_tambien_cuenta(
    raiz: pathlib.Path,
) -> None:
    """La salida no distingue mayúsculas (``clave_de_ruta``): ``Muros/x_n.dds`` y ``muros/x_p.dds`` son el MISMO destino en
    Windows y en Skyrim. Mirar solo la carpeta física dejaría pisar el autoral en un sistema que sí distingue mayúsculas
    (Codex, #784). Se prueba en las dos direcciones: el orden en que se recorren las carpetas no puede decidir el resultado."""
    muros = raiz / "textures" / "Muros"
    if not _distingue_mayusculas(raiz / "textures"):
        pytest.skip("este sistema no distingue mayúsculas en los nombres")
    (raiz / "textures" / "sonda_a").unlink()
    (raiz / "textures" / "SONDA_A").unlink()
    _tocar(muros / "x_n.dds")
    _tocar(raiz / "textures" / "muros" / "x_p.dds", b"AUTORAL")  # la p está en la carpeta que se escribe distinto
    _tocar(raiz / "textures" / "muros" / "y_n.dds")
    _tocar(muros / "Y_P.DDS")  # y la inversa, y con el nombre del archivo también en otras mayúsculas
    _tocar(muros / "z_n.dds")  # sin ninguna p en ninguna de las dos: se procesa

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/Muros/z_n.dds"]
    assert _omitidas(d) == [
        ("textures/Muros/x_n.dds", M.YA_TIENE_PARALLAX),
        ("textures/muros/y_n.dds", M.YA_TIENE_PARALLAX),
    ]
    assert d.completo is True


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
def test_enlace_un_enlace_a_un_archivo_o_a_una_carpeta_se_omite_y_no_se_sigue(
    tmp_path: pathlib.Path, raiz: pathlib.Path, tipo: str
) -> None:
    fuera = tmp_path / "fuera"
    _tocar(fuera / "escondida_n.dds")
    _tocar(fuera / "otra_n.dds")
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _enlazar(tipo, fuera, raiz / "textures" / "a" / "carpeta_enlazada")
    if tipo == "symlink":
        _enlazar(tipo, fuera / "otra_n.dds", raiz / "textures" / "a" / "enlace_n.dds")

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    esperadas = [("textures/a/carpeta_enlazada", M.ENLACE)]
    if tipo == "symlink":
        esperadas.append(("textures/a/enlace_n.dds", M.ENLACE))
    assert _omitidas(d) == esperadas
    assert tipo in d.omitidas[0].detalle
    assert d.completo is True  # un enlace se omite A PROPÓSITO: no es algo que no se pudo ver


def test_archivo_vacio_se_omite(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "a" / "vacia_n.dds", b"")
    _tocar(raiz / "textures" / "a" / "llena_n.dds", b"x")

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/llena_n.dds"]
    assert _omitidas(d) == [("textures/a/vacia_n.dds", M.ARCHIVO_VACIO)]
    assert "vacío" in d.omitidas[0].detalle
    assert d.completo is True


def test_demasiado_grande_se_omite_y_justo_el_tope_pasa(raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, "TAMANO_MAXIMO_DEL_ARCHIVO", 8)
    _tocar(raiz / "textures" / "a" / "justo_n.dds", b"x" * 8)
    _tocar(raiz / "textures" / "a" / "grande_n.dds", b"x" * 9)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/justo_n.dds"]
    assert d.normales[0].tamano == 8
    assert _omitidas(d) == [("textures/a/grande_n.dds", M.DEMASIADO_GRANDE)]
    assert "9" in d.omitidas[0].detalle and "8" in d.omitidas[0].detalle
    assert d.completo is True


def test_ruta_invalida_el_detalle_trae_el_error_del_validador(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Qué nombre es inadmisible lo decide ``output.validar_ruta_de_salida``: acá se verifica la REACCIÓN."""
    _tocar(raiz / "textures" / "a" / "buena_n.dds")
    _tocar(raiz / "textures" / "a" / "mala_n.dds")
    real = provider.validar_ruta_de_salida

    def validar(ruta: str) -> str:
        if "mala" in ruta:
            raise RutaDeSalidaInvalidaError("caracter no admitido")
        return real(ruta)

    monkeypatch.setattr(provider, "validar_ruta_de_salida", validar)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/buena_n.dds"]
    assert d.omitidas == (
        Omitida("textures/a/mala_n.dds", M.RUTA_INVALIDA, "la ruta de salida no es admisible: caracter no admitido"),
    )
    assert d.completo is True


def test_ruta_invalida_con_el_validador_real_un_nombre_no_ascii_se_omite(raiz: pathlib.Path) -> None:
    try:
        _tocar(raiz / "textures" / "a" / "piedra_ñ_n.dds")
    except (OSError, UnicodeError):
        pytest.skip("este sistema no permite nombres no ASCII")
    _tocar(raiz / "textures" / "a" / "piedra_n.dds")

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/piedra_n.dds"]
    assert _omitidas(d) == [("textures/a/piedra_ñ_n.dds", M.RUTA_INVALIDA)]


def test_ilegible_un_archivo_que_no_se_puede_inspeccionar_no_aborta_y_deja_la_enumeracion_incompleta(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _tocar(raiz / "textures" / "a" / "beta_n.dds")
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        if ruta.name == "beta_n.dds":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert _omitidas(d) == [("textures/a/beta_n.dds", M.ILEGIBLE)]
    assert "no se pudo inspeccionar" in d.omitidas[0].detalle and "bloqueado por el antivirus" in d.omitidas[0].detalle
    assert d.completo is False


def test_una_entrada_que_desaparece_entre_el_listado_y_la_inspeccion_ya_no_esta_y_no_cuenta_como_no_observada(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una entrada borrada a mitad del recorrido está AUSENTE (se la observó ausente): no deja la enumeración incompleta."""
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _tocar(raiz / "textures" / "a" / "beta_n.dds")
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        return (None, None) if ruta.name == "beta_n.dds" else real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert d.omitidas == () and d.completo is True


def test_ilegible_un_archivo_que_no_se_puede_leer_no_aborta_y_deja_la_enumeracion_incompleta(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds", b"ALFA")
    _tocar(raiz / "textures" / "a" / "beta_n.dds", b"BETA")
    real = provider._huella_acotada

    def huella(ruta: pathlib.Path, tamano_esperado: int) -> str:
        if ruta.name == "beta_n.dds":
            raise PermissionError("acceso denegado")
        return real(ruta, tamano_esperado)

    monkeypatch.setattr(provider, "_huella_acotada", huella)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert _omitidas(d) == [("textures/a/beta_n.dds", M.ILEGIBLE)]
    assert "no se pudo leer" in d.omitidas[0].detalle and "acceso denegado" in d.omitidas[0].detalle
    assert d.completo is False


@pytest.mark.skipif(os.name == "nt", reason="un FIFO no se crea en Windows")
def test_ilegible_algo_que_se_llama_como_una_normal_pero_no_es_un_archivo_regular(raiz: pathlib.Path) -> None:
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "alfa_n.dds")
    os.mkfifo(carpeta / "fifo_n.dds")

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert _omitidas(d) == [("textures/a/fifo_n.dds", M.ILEGIBLE)]
    assert "no es un archivo regular" in d.omitidas[0].detalle
    assert d.completo is False


def test_carpeta_ilegible_se_omite_con_su_motivo_y_deja_la_enumeracion_incompleta(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _tocar(raiz / "textures" / "b" / "beta_n.dds")
    real = os.scandir

    def scandir(ruta: Any = ".") -> Any:
        if pathlib.Path(ruta).name == "b":
            raise PermissionError("acceso denegado")
        return real(ruta)

    monkeypatch.setattr(provider.os, "scandir", scandir)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert _omitidas(d) == [("textures/b", M.CARPETA_ILEGIBLE)]
    assert "no se pudo leer la carpeta" in d.omitidas[0].detalle and "acceso denegado" in d.omitidas[0].detalle
    assert d.completo is False


@pytest.mark.parametrize("cambio", ["ahora_es_un_enlace", "es_otra_carpeta"])
def test_carpeta_ilegible_una_carpeta_que_cambia_mientras_se_abre_no_se_recorre(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch, cambio: str
) -> None:
    """Se revalida DESPUÉS de abrirla (como ``iter_archivos_propios``): una carpeta reemplazada por un enlace entre su
    ``lstat`` y su ``scandir`` haría entrar al árbol de afuera."""
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _tocar(raiz / "textures" / "b" / "beta_n.dds")
    real = provider.link_kind_and_identity_or_raise
    veces: dict[str, int] = {}

    def inspeccionar(ruta: pathlib.Path) -> Any:
        tipo, info = real(ruta)
        veces[ruta.name] = veces.get(ruta.name, 0) + 1
        if ruta.name != "b" or veces["b"] < 2 or info is None:
            return tipo, info
        if cambio == "ahora_es_un_enlace":
            return "symlink", info
        mutada = list(info)
        mutada[1] = info.st_ino + 1  # otra identidad: la carpeta que se abrió no es la que se inspeccionó
        return None, os.stat_result(tuple(mutada))

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/alfa_n.dds"]
    assert _omitidas(d) == [("textures/b", M.CARPETA_ILEGIBLE)]
    assert "cambió mientras se abría" in d.omitidas[0].detalle
    assert d.completo is False


def test_duplicada_por_mayusculas_se_omiten_las_dos(raiz: pathlib.Path) -> None:
    carpeta = raiz / "textures" / "a"
    if not _distingue_mayusculas(carpeta):
        pytest.skip("este sistema no distingue mayúsculas en los nombres")
    (carpeta / "sonda_a").unlink()
    (carpeta / "SONDA_A").unlink()
    _tocar(carpeta / "Alfa_n.dds")
    _tocar(carpeta / "alfa_n.dds")
    _tocar(carpeta / "beta_n.dds")
    _tocar(raiz / "textures" / "Muros" / "x_n.dds")
    _tocar(raiz / "textures" / "muros" / "x_n.dds")  # la CARPETA también cambia la clave

    d = descubrir_normales(raiz)

    assert _origenes(d) == ["textures/a/beta_n.dds"]
    assert _omitidas(d) == [
        ("textures/a/Alfa_n.dds", M.DUPLICADA_POR_MAYUSCULAS),
        ("textures/a/alfa_n.dds", M.DUPLICADA_POR_MAYUSCULAS),
        ("textures/Muros/x_n.dds", M.DUPLICADA_POR_MAYUSCULAS),
        ("textures/muros/x_n.dds", M.DUPLICADA_POR_MAYUSCULAS),
    ]
    assert "textures/a/alfa_p.dds" in d.omitidas[0].detalle
    assert d.completo is True  # fail-closed por ambigüedad, no por no haber podido ver


def test_cada_motivo_del_vocabulario_cerrado_tiene_un_caso_en_este_archivo() -> None:
    """Si alguien agrega un motivo a ``MotivoDeOmision`` sin un caso, este test se lo recuerda."""
    casos = {
        M.YA_TIENE_PARALLAX: "test_ya_tiene_parallax_una_normal_con_su_p_al_lado_no_se_pisa",
        M.ENLACE: "test_enlace_un_enlace_a_un_archivo_o_a_una_carpeta_se_omite_y_no_se_sigue",
        M.RUTA_INVALIDA: "test_ruta_invalida_el_detalle_trae_el_error_del_validador",
        M.ARCHIVO_VACIO: "test_archivo_vacio_se_omite",
        M.DEMASIADO_GRANDE: "test_demasiado_grande_se_omite_y_justo_el_tope_pasa",
        M.ILEGIBLE: "test_ilegible_un_archivo_que_no_se_puede_leer_no_aborta_y_deja_la_enumeracion_incompleta",
        M.CARPETA_ILEGIBLE: "test_carpeta_ilegible_se_omite_con_su_motivo_y_deja_la_enumeracion_incompleta",
        M.DUPLICADA_POR_MAYUSCULAS: "test_duplicada_por_mayusculas_se_omiten_las_dos",
    }

    assert set(casos) == set(MotivoDeOmision)
    assert all(nombre in globals() for nombre in casos.values())


def test_el_vocabulario_de_motivos_y_su_clasificacion_estan_congelados_por_igualdad_literal() -> None:
    """Un motivo nuevo obliga a decidir si deja lo no observado (``completo=False``) o es una decisión deliberada."""
    assert {m.name for m in MotivoDeOmision} == {
        "YA_TIENE_PARALLAX",
        "ENLACE",
        "RUTA_INVALIDA",
        "ARCHIVO_VACIO",
        "DEMASIADO_GRANDE",
        "ILEGIBLE",
        "CARPETA_ILEGIBLE",
        "DUPLICADA_POR_MAYUSCULAS",
    }
    assert frozenset({M.ILEGIBLE, M.CARPETA_ILEGIBLE}) == MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO
    assert set(MotivoDeOmision) - MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO == {
        M.YA_TIENE_PARALLAX,
        M.ENLACE,
        M.RUTA_INVALIDA,
        M.ARCHIVO_VACIO,
        M.DEMASIADO_GRANDE,
        M.DUPLICADA_POR_MAYUSCULAS,
    }


@pytest.mark.parametrize("motivo", sorted(MotivoDeOmision, key=lambda m: m.name))
def test_completo_depende_solo_de_la_clasificacion_del_motivo(motivo: MotivoDeOmision) -> None:
    """``completo`` se deriva de la clasificación y no de una lista aparte: no pueden separarse."""
    omitidas = (Omitida("textures/a_n.dds", motivo, ""),)

    assert provider._es_completo(omitidas) is (motivo not in MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO)
    assert provider._es_completo(()) is True


# ---------------------------------------------------------------------------
# La raíz y la carpeta textures
# ---------------------------------------------------------------------------


def test_una_raiz_que_no_es_una_ruta_se_rechaza(raiz: pathlib.Path) -> None:
    with pytest.raises(RaizInvalidaError, match="debe ser una ruta"):
        descubrir_normales(str(raiz))  # type: ignore[arg-type]  # el test existe para pasar el tipo equivocado


def test_una_raiz_que_no_existe_o_no_es_una_carpeta_se_rechaza(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RaizInvalidaError, match="no existe"):
        descubrir_normales(tmp_path / "no_existe")
    archivo = _tocar(tmp_path / "archivo")
    with pytest.raises(RaizInvalidaError, match="no es una carpeta"):
        descubrir_normales(archivo)


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
def test_una_raiz_que_es_un_enlace_se_rechaza(tmp_path: pathlib.Path, raiz: pathlib.Path, tipo: str) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _enlazar(tipo, raiz, tmp_path / "enlace_a_la_raiz")

    with pytest.raises(RaizInvalidaError, match=f"es un enlace \\({tipo}\\)"):
        descubrir_normales(tmp_path / "enlace_a_la_raiz")


def test_una_raiz_sin_textures_se_rechaza(tmp_path: pathlib.Path) -> None:
    (tmp_path / "mod").mkdir()

    with pytest.raises(RaizInvalidaError, match="debe tener una subcarpeta 'textures'"):
        descubrir_normales(tmp_path / "mod")


def test_una_raiz_que_no_se_puede_listar_se_rechaza(raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.scandir

    def scandir(ruta: Any = ".") -> Any:
        if pathlib.Path(ruta) == raiz:
            raise PermissionError("acceso denegado")
        return real(ruta)

    monkeypatch.setattr(provider.os, "scandir", scandir)

    with pytest.raises(RaizInvalidaError, match="no se pudo leer la carpeta.*acceso denegado"):
        descubrir_normales(raiz)


def test_una_raiz_que_no_se_puede_inspeccionar_se_rechaza(raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def inspeccionar(ruta: pathlib.Path) -> Any:
        raise PermissionError("bloqueado por el antivirus")

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(RaizInvalidaError, match="no se pudo inspeccionar.*bloqueado por el antivirus"):
        descubrir_normales(raiz)


def test_una_textures_que_no_se_puede_inspeccionar_se_rechaza(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        if ruta.name == "textures":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(RaizInvalidaError, match="no se pudo inspeccionar.*textures.*bloqueado por el antivirus"):
        descubrir_normales(raiz)


def test_una_textures_que_desaparece_mientras_se_abre_la_raiz_se_rechaza(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = provider.link_kind_and_identity_or_raise

    def inspeccionar(ruta: pathlib.Path) -> Any:
        return (None, None) if ruta.name == "textures" else real(ruta)

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(RaizInvalidaError, match="'textures' desapareció mientras se abría la raíz"):
        descubrir_normales(raiz)


@pytest.mark.parametrize("cambio", ["ahora_es_un_enlace", "es_otra_carpeta"])
def test_una_raiz_que_cambia_mientras_se_abre_se_rechaza(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch, cambio: str
) -> None:
    """La raíz se revalida DESPUÉS de abrirla, igual que cada carpeta que se recorre (Codex, #784): reemplazada por un
    enlace entre su ``lstat`` y su ``scandir``, el escaneo seguiría el reemplazo y la identidad de ``textures`` se
    tomaría del árbol de afuera."""
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    real = provider.link_kind_and_identity_or_raise
    veces = {"raiz": 0}

    def inspeccionar(ruta: pathlib.Path) -> Any:
        tipo, info = real(ruta)
        if ruta != raiz or info is None:
            return tipo, info
        veces["raiz"] += 1
        if veces["raiz"] < 2:  # la primera inspección es la previa a abrirla; la segunda, la de después
            return tipo, info
        if cambio == "ahora_es_un_enlace":
            return "symlink", info
        mutada = list(info)
        mutada[1] = info.st_ino + 1
        return None, os.stat_result(tuple(mutada))

    monkeypatch.setattr(provider, "link_kind_and_identity_or_raise", inspeccionar)

    with pytest.raises(RaizInvalidaError, match="no se pudo leer la carpeta.*cambió mientras se abría"):
        descubrir_normales(raiz)

    assert veces["raiz"] == 2


def test_textures_que_es_un_archivo_se_rechaza(tmp_path: pathlib.Path) -> None:
    _tocar(tmp_path / "mod" / "textures")

    with pytest.raises(RaizInvalidaError, match="'textures' no es una carpeta"):
        descubrir_normales(tmp_path / "mod")


@pytest.mark.parametrize("tipo", ["symlink", "junction"])
def test_textures_que_es_un_enlace_se_rechaza(tmp_path: pathlib.Path, tipo: str) -> None:
    """En Windows ``DirEntry.is_dir(follow_symlinks=False)`` acepta un junction: ``textures`` se inspecciona como lo de adentro."""
    _tocar(tmp_path / "fuera" / "a" / "alfa_n.dds")
    (tmp_path / "mod").mkdir()
    _enlazar(tipo, tmp_path / "fuera", tmp_path / "mod" / "textures")

    with pytest.raises(RaizInvalidaError, match=f"'textures' es un enlace \\({tipo}\\)"):
        descubrir_normales(tmp_path / "mod")


def test_dos_carpetas_textures_que_solo_difieren_en_mayusculas_son_ambiguas(
    raiz: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    if not _distingue_mayusculas(tmp_path):
        pytest.skip("este sistema no distingue mayúsculas en los nombres")
    (raiz / "Textures").mkdir()

    with pytest.raises(RaizInvalidaError, match="ambigua"):
        descubrir_normales(raiz)


def test_una_raiz_invalida_no_toca_nada(tmp_path: pathlib.Path) -> None:
    (tmp_path / "mod").mkdir()
    antes = _foto(tmp_path)

    with pytest.raises(RaizInvalidaError):
        descubrir_normales(tmp_path / "mod")

    assert _foto(tmp_path) == antes


# ---------------------------------------------------------------------------
# El límite de entradas y sus parámetros
# ---------------------------------------------------------------------------


def test_un_exceso_de_normales_se_rechaza_y_justo_el_limite_pasa(raiz: pathlib.Path) -> None:
    for nombre in ("a", "b"):
        _tocar(raiz / "textures" / f"{nombre}_n.dds")

    assert len(descubrir_normales(raiz, maximo_de_archivos=2).normales) == 2

    _tocar(raiz / "textures" / "c_n.dds")
    with pytest.raises(DemasiadosArchivosError, match="más de 2 entradas"):
        descubrir_normales(raiz, maximo_de_archivos=2)


def test_el_limite_minimo_de_uno_es_valido(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "a_n.dds")

    assert len(descubrir_normales(raiz, maximo_de_archivos=1).normales) == 1

    _tocar(raiz / "textures" / "b_n.dds")
    with pytest.raises(DemasiadosArchivosError, match="más de 1 entradas"):
        descubrir_normales(raiz, maximo_de_archivos=1)


def test_las_omisiones_tambien_cuentan_para_el_limite_porque_son_memoria(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "a_n.dds")
    _tocar(raiz / "textures" / "b_n.dds")
    _tocar(raiz / "textures" / "c_n.dds", b"")  # vacía: una omisión
    assert len(descubrir_normales(raiz, maximo_de_archivos=3).omitidas) == 1

    _tocar(raiz / "textures" / "d_n.dds", b"")
    with pytest.raises(DemasiadosArchivosError):
        descubrir_normales(raiz, maximo_de_archivos=3)


@pytest.mark.parametrize("limite", [0, -1, True, 1.5, "10", None])
def test_un_limite_fuera_de_dominio_se_rechaza_antes_de_mirar_el_disco(raiz: pathlib.Path, limite: Any) -> None:
    with pytest.raises(LimiteInvalidoError, match="maximo_de_archivos"):
        descubrir_normales(raiz, maximo_de_archivos=limite)


def test_el_limite_por_defecto_es_de_cien_mil() -> None:
    assert MAXIMO_DE_ARCHIVOS_POR_DEFECTO == 100_000


def test_los_errores_del_proveedor_tienen_una_raiz_comun_y_los_de_valor_son_value_error() -> None:
    assert issubclass(RaizInvalidaError, ProveedorError) and issubclass(RaizInvalidaError, ValueError)
    assert issubclass(DemasiadosArchivosError, ProveedorError)
    assert issubclass(LimiteInvalidoError, ProveedorError) and issubclass(LimiteInvalidoError, ValueError)


# ---------------------------------------------------------------------------
# Determinismo y orden
# ---------------------------------------------------------------------------


def _lo_observable(d: Descubrimiento) -> Any:
    """Todo menos la raíz absoluta, que cambia entre árboles equivalentes."""
    return (
        [(n.ruta_de_origen, n.ruta_de_salida, n.tamano, n.origen) for n in d.normales],
        d.omitidas,
        d.completo,
    )


def _armar(raiz: pathlib.Path, orden: list[str]) -> None:
    for nombre in orden:
        if nombre.endswith("_n.dds"):
            _tocar(raiz / "textures" / "a" / nombre, nombre.encode())
    _tocar(raiz / "textures" / "a" / "viz_n.dds", b"")  # una omisión
    _tocar(raiz / "textures" / "a" / "w_n.dds")
    _tocar(raiz / "textures" / "a" / "w_p.dds")  # otra


def test_el_mismo_arbol_da_la_misma_salida_sin_depender_del_orden_de_creacion(tmp_path: pathlib.Path) -> None:
    nombres = ["B_n.dds", "a_n.dds", "C_n.dds", "b2_n.dds"]
    uno, otro = tmp_path / "uno", tmp_path / "otro"
    _armar(uno, nombres)
    _armar(otro, list(reversed(nombres)))

    d_uno, d_otro = descubrir_normales(uno), descubrir_normales(otro)

    assert _lo_observable(d_uno) == _lo_observable(d_otro)
    assert descubrir_normales(uno) == d_uno  # y dos corridas sobre el mismo árbol son idénticas


def test_las_normales_y_las_omisiones_van_ordenadas_por_clave_de_ruta_no_por_el_orden_del_sistema(
    raiz: pathlib.Path,
) -> None:
    for nombre in ("B_n.dds", "a_n.dds", "C_n.dds", "b2_n.dds"):
        _tocar(raiz / "textures" / "a" / nombre)
    _tocar(raiz / "textures" / "a" / "Z_n.dds", b"")
    _tocar(raiz / "textures" / "a" / "e_n.dds", b"")

    d = descubrir_normales(raiz)

    claves = [clave_de_ruta(n.ruta_de_salida) for n in d.normales]
    assert (
        claves
        == sorted(claves)
        == [
            "textures/a/a_p.dds",
            "textures/a/b2_p.dds",
            "textures/a/b_p.dds",
            "textures/a/c_p.dds",
        ]
    )
    assert [o.ruta_de_origen for o in d.omitidas] == ["textures/a/e_n.dds", "textures/a/Z_n.dds"]


# ---------------------------------------------------------------------------
# La huella: acotada, y solo de lo que sobrevive
# ---------------------------------------------------------------------------


@pytest.fixture
def bloque_chico(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, "_BLOQUE", 3)


@pytest.mark.parametrize("tamano", [1, 2, 3, 4, 6, 7, 8])
def test_la_huella_es_el_sha256_del_contenido_con_bloques_de_cualquier_tamano(
    tmp_path: pathlib.Path, bloque_chico: None, tamano: int
) -> None:
    archivo = _tocar(tmp_path / "a.bin", bytes(range(tamano)))

    assert provider._huella_acotada(archivo, tamano) == hashlib.sha256(bytes(range(tamano))).hexdigest()


def test_un_archivo_que_creció_despues_de_medirlo_se_rechaza(tmp_path: pathlib.Path, bloque_chico: None) -> None:
    archivo = _tocar(tmp_path / "a.bin", b"x" * 20)

    with pytest.raises(OSError, match="creció mientras se leía"):
        provider._huella_acotada(archivo, 5)  # se midió en 5 bytes


def test_un_archivo_que_se_achico_despues_de_medirlo_se_rechaza(tmp_path: pathlib.Path, bloque_chico: None) -> None:
    archivo = _tocar(tmp_path / "a.bin", b"x" * 3)

    with pytest.raises(OSError, match="cambió mientras se leía.*3.*5"):
        provider._huella_acotada(archivo, 5)


def test_la_huella_no_lee_mas_de_un_byte_pasado_lo_que_midio_aunque_el_origen_no_termine_nunca(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La propiedad es de memoria y de tiempo: un origen que crece sin parar no se lee más allá de ``tamaño + 1`` bytes."""
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

    archivo = _tocar(tmp_path / "a.bin", b"12345678")
    monkeypatch.setattr(provider, "open", lambda *_a, **_k: OrigenInfinito(), raising=False)

    with pytest.raises(OSError, match="creció mientras se leía"):
        provider._huella_acotada(archivo, 8)

    assert pedidos == [5, 4] and sum(pedidos) == 8 + 1


def test_la_huella_se_calcula_solo_de_lo_que_sobrevive_y_en_orden(
    raiz: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashear un archivo que se va a omitir es leerlo (hasta 1 GiB) para nada."""
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "b_n.dds", b"B")
    _tocar(carpeta / "a_n.dds", b"A")
    _tocar(carpeta / "c_n.dds", b"C")
    _tocar(carpeta / "c_p.dds")  # se omite: ya tiene parallax
    _tocar(carpeta / "vacia_n.dds", b"")  # se omite
    leidos: list[str] = []
    real = provider._huella_acotada

    def huella(ruta: pathlib.Path, tamano_esperado: int) -> str:
        leidos.append(ruta.name)
        return real(ruta, tamano_esperado)

    monkeypatch.setattr(provider, "_huella_acotada", huella)

    d = descubrir_normales(raiz)

    assert leidos == ["a_n.dds", "b_n.dds"] == [pathlib.Path(n.ruta_de_origen).name for n in d.normales]


def test_el_tamano_y_la_huella_describen_los_mismos_bytes(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds", b"contenido de prueba")

    (normal,) = descubrir_normales(raiz).normales

    assert normal.tamano == len(b"contenido de prueba")
    assert normal.origen == OrigenDeEntrada("normal", hashlib.sha256(b"contenido de prueba").hexdigest())


# ---------------------------------------------------------------------------
# Solo lectura
# ---------------------------------------------------------------------------


def test_descubrir_no_crea_mueve_ni_borra_nada(tmp_path: pathlib.Path, raiz: pathlib.Path) -> None:
    carpeta = raiz / "textures" / "a"
    _tocar(carpeta / "alfa_n.dds", b"ALFA")
    _tocar(carpeta / "beta_n.dds", b"")
    _tocar(carpeta / "gama_n.dds")
    _tocar(carpeta / "gama_p.dds")
    _tocar(carpeta / "b" / "delta_n.dds", b"DELTA")
    antes = _foto(tmp_path)

    descubrir_normales(raiz)

    assert _foto(tmp_path) == antes


# ---------------------------------------------------------------------------
# Compatibilidad con el lote
# ---------------------------------------------------------------------------


def test_una_normal_descubierta_cumple_el_protocolo_que_consume_el_lote(raiz: pathlib.Path) -> None:
    """``lote.NormalAProcesar`` pide tres propiedades de solo lectura; ``NormalDescubierta`` las tiene, con esos tipos."""
    pedidas = {nombre for nombre in vars(NormalAProcesar) if not nombre.startswith("_")}
    campos = {c.name: c for c in dataclasses.fields(NormalDescubierta)}
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")

    (normal,) = descubrir_normales(raiz).normales

    assert pedidas == {"ruta_de_origen", "ruta_de_salida", "ruta_absoluta"}
    assert pedidas <= set(campos)
    assert isinstance(normal.ruta_de_origen, str) and isinstance(normal.ruta_de_salida, str)
    assert isinstance(normal.ruta_absoluta, pathlib.Path) and normal.ruta_absoluta.is_absolute()


def test_los_valores_del_proveedor_son_inmutables_y_con_slots(raiz: pathlib.Path) -> None:
    _tocar(raiz / "textures" / "a" / "alfa_n.dds")
    _tocar(raiz / "textures" / "a" / "vacia_n.dds", b"")
    d = descubrir_normales(raiz)

    # Un campo REAL de cada valor: en 3.11 un frozen con slots lanza ``TypeError`` (no ``FrozenInstanceError``) al escribir
    # un atributo que no existe.
    for valor, campo in ((d, "completo"), (d.normales[0], "tamano"), (d.omitidas[0], "detalle")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(valor, campo, None)
        assert not hasattr(valor, "__dict__")


def test_el_all_del_proveedor_esta_congelado() -> None:
    assert set(provider.__all__) == {
        "MAXIMO_DE_ARCHIVOS_POR_DEFECTO",
        "MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO",
        "DemasiadosArchivosError",
        "Descubrimiento",
        "LimiteInvalidoError",
        "MotivoDeOmision",
        "NormalDescubierta",
        "Omitida",
        "ProveedorError",
        "RaizInvalidaError",
        "descubrir_normales",
    }


# ---------------------------------------------------------------------------
# Anclas por AST: no decide política y es de solo lectura
# ---------------------------------------------------------------------------

_FUENTE = pathlib.Path(provider.__file__)


def _arbol() -> ast.Module:
    return ast.parse(_FUENTE.read_text(encoding="utf-8"))


def _modulos_importados() -> set[str]:
    modulos: set[str] = set()
    for nodo in ast.walk(_arbol()):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module is not None:
            modulos.add(nodo.module)
    return modulos


def test_el_proveedor_solo_importa_la_biblioteca_estandar_el_modulo_puro_de_salida_y_los_enlaces() -> None:
    """No sabe de ``texconv``, del gate, del escritor ni de MO2, y no trae dependencias nuevas (ni numpy ni Pillow)."""
    assert _modulos_importados() == {
        "__future__",
        "dataclasses",
        "enum",
        "hashlib",
        "os",
        "pathlib",
        "stat",
        "typing",
        "sky_claw.app.security.links",
        "sky_claw.local.native_parallax.output",
    }


def test_las_unicas_llamadas_al_disco_son_las_de_lectura_y_estan_enumeradas() -> None:
    """Un uso nuevo de ``os`` (o de ``open``) rompe el ancla a propósito: decidí si sigue siendo de solo lectura."""
    usos_de_os = {
        nodo.attr
        for nodo in ast.walk(_arbol())
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "os"
    }
    assert usos_de_os == {"scandir", "path", "stat_result", "DirEntry"}

    aperturas = [
        nodo
        for nodo in ast.walk(_arbol())
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "open"
    ]
    assert len(aperturas) == 1
    modo = aperturas[0].args[1]
    assert isinstance(modo, ast.Constant) and modo.value == "rb"


def test_ninguna_operacion_de_escritura_aparece_en_el_codigo() -> None:
    prohibidas = {
        "write",
        "write_bytes",
        "write_text",
        "unlink",
        "mkdir",
        "makedirs",
        "rmdir",
        "rename",
        "replace",
        "touch",
        "remove",
        "symlink",
        "chmod",
        "utime",
        "truncate",
        "rmtree",
        "copy",
        "copyfile",
        "move",
    }
    usadas = {nodo.attr for nodo in ast.walk(_arbol()) if isinstance(nodo, ast.Attribute)}

    assert not usadas & prohibidas
