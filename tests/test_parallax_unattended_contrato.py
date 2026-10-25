"""NP-UA-4: el contrato de la capa desatendida se verifica, no se confía.

Qué se verifica y con qué:

- **El paquete cumple las cuatro reglas de ``AGENTS.md``**: no importa al generador, no importa
  numpy ni Pillow, no escribe en disco, y su ``__all__`` está completo. Corre en CI con cada
  PR porque es parte de la suite, no un chequeo manual que alguien se acuerda de correr.
- **El detector DETECTA.** Cada regla tiene su caso con una fuente sintética que la viola. Un
  verificador que siempre devuelve vacío pasaría el primer test sin verificar nada; estos son
  los que lo distinguen.
- **No hay falso positivo con lo permitido**: leer un archivo, importar de la biblioteca
  estándar y los imports relativos internos no son hallazgo.
- **El escaneo no es vacío.** Si la ruta del paquete estuviera mal, ``verificar`` devolvería
  cero hallazgos y el test pasaría sin haber leído un solo archivo. Se exige un mínimo de
  archivos vistos y que aparezcan los cuatro módulos por nombre.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.parallax_unattended.contrato import (
    ReglaDeContrato,
    revisar_fuente,
    verificar,
    verificar_api,
)

_PAQUETE = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "parallax_unattended"


# ---------------------------------------------------------------------------
# El paquete real
# ---------------------------------------------------------------------------


def test_verificar_paquete_completo_incluye_modulos() -> None:
    """Sin esto, una ruta mal escrita haría pasar el contrato sobre cero archivos."""

    vistos = {ruta.name for ruta in _PAQUETE.glob("*.py")}

    assert {"__init__.py", "auditor.py", "composicion.py", "contrato.py", "veredicto.py"} <= vistos


def test_verificar_paquete_real_cumple_todas_las_reglas() -> None:
    hallazgos = verificar(_PAQUETE)

    assert hallazgos == (), "\n".join(f"{h.archivo}:{h.linea} {h.regla} — {h.detalle}" for h in hallazgos)


def test_verificar_api_paquete_real_exporta_simbolos_completos() -> None:
    hallazgos = verificar_api()

    assert hallazgos == (), "\n".join(f"{h.regla} — {h.detalle}" for h in hallazgos)


# ---------------------------------------------------------------------------
# El detector detecta (si no, el test de arriba no prueba nada)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fuente", "regla"),
    [
        ("import numpy as np", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        ("from numpy.linalg import norm", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        ("from PIL import Image", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        ("import PIL.Image", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        ("from sky_claw.local.native_parallax import lote", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from sky_claw.local.native_parallax.lote import InformeDelLote", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from sky_claw.local import native_parallax", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from ..native_parallax import lote", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from ..native_parallax.lote import InformeDelLote", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from .. import native_parallax", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from ...local.native_parallax import lote", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from ...local import native_parallax", ReglaDeContrato.IMPORTA_GENERADOR),
        ("from ....sky_claw.local.native_parallax import lote", ReglaDeContrato.IMPORTA_GENERADOR),
        ('open(ruta, "w")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('open(ruta, mode="a")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('open(ruta, "r+")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.open("w")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.open(mode="a")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('io.open(ruta, "w")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("os.open(ruta, os.O_RDWR)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("os.remove(ruta)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("os.makedirs(ruta)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("os.symlink(origen, destino)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("os.link(origen, destino)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("shutil.copy(origen, destino)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("shutil.rmtree(ruta)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.write_text("x")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.write_bytes(b"x")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("ruta.unlink()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("ruta.mkdir(parents=True)", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("tempfile.mkdtemp()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("tempfile.mkstemp()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("tempfile.NamedTemporaryFile()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("tempfile.TemporaryFile()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("tempfile.TemporaryDirectory()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("NamedTemporaryFile()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("mkdtemp()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("TemporaryDirectory()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('os.write(fd, b"x")', ReglaDeContrato.ESCRIBE_EN_DISCO),
    ],
)
def test_revisar_fuente_violacion_sintetica_reporta_regla_esperada(fuente: str, regla: ReglaDeContrato) -> None:
    hallazgos = revisar_fuente(fuente, "sintetico.py")

    assert [h.regla for h in hallazgos] == [regla]


@pytest.mark.parametrize(
    "fuente",
    [
        'open(ruta, "rb")',
        "open(ruta)",
        "ruta.read_text(encoding='utf-8')",
        "ruta.is_file()",
        "import ast",
        "from __future__ import annotations",
        "from .auditor import Hallazgo",
        "from .veredicto import dictar",
        "from sky_claw.local.parallax_unattended import auditar",
        "os.open(ruta, os.O_RDONLY)",
    ],
)
def test_revisar_fuente_codigo_permitido_devuelve_cero_hallazgos(fuente: str) -> None:
    assert revisar_fuente(fuente, "sintetico.py") == ()


def test_revisar_fuente_sintaxis_invalida_reporta_error_sin_excepcion() -> None:
    hallazgos = revisar_fuente("def (: pass", "roto.py")

    assert [h.regla for h in hallazgos] == [ReglaDeContrato.SINTAXIS_INVALIDA]


def test_revisar_fuente_subpaquete_anidado_resuelve_import_relativo_prohibido() -> None:
    fuente = "from ... import native_parallax"
    hallazgos = revisar_fuente(fuente, "sub/anidado.py")

    assert [h.regla for h in hallazgos] == [ReglaDeContrato.IMPORTA_GENERADOR]
    assert hallazgos[0].detalle == "importa 'sky_claw.local.native_parallax'"


def test_verificar_archivo_en_subdirectorio_detecta_violacion_en_disco(tmp_path: pathlib.Path) -> None:
    (tmp_path / "limpio.py").write_text("import ast\n", encoding="utf-8", newline="\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "sucio.py").write_text("import numpy\n", encoding="utf-8", newline="\n")

    hallazgos = verificar(tmp_path)

    assert [(h.archivo, h.regla) for h in hallazgos] == [("sub/sucio.py", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL)]


def test_verificar_api_simbolo_publico_no_exportado_reporta_api_incompleta(monkeypatch: pytest.MonkeyPatch) -> None:
    import sky_claw.local.parallax_unattended as paquete

    monkeypatch.setattr(paquete, "SIMBOLO_FANTASMA", 42, raising=False)
    hallazgos = verificar_api()

    assert any(h.regla is ReglaDeContrato.API_INCOMPLETA and "SIMBOLO_FANTASMA" in h.detalle for h in hallazgos)


def test_revisar_fuente_violaciones_multiples_reporta_lineas_y_reglas_exactas() -> None:
    fuente = 'import ast\nimport numpy\n\ndef f():\n    open("x", "w")\n'
    hallazgos = revisar_fuente(fuente, "sintetico.py")

    assert [(h.linea, h.regla) for h in hallazgos] == [
        (2, ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        (5, ReglaDeContrato.ESCRIBE_EN_DISCO),
    ]


def test_escribir_en_un_buffer_en_memoria_no_es_escribir_en_disco() -> None:
    """``os.write`` es disco; ``.write`` de un buffer en memoria no: sin esta frontera el contrato sería ruido."""

    assert revisar_fuente('buffer = io.StringIO()\nbuffer.write("x")\n', "sintetico.py") == ()


@pytest.mark.parametrize("fuente", ["f()()\n", "tabla[0](x)\n", "(lambda: 1)()\n"])
def test_llamadas_cuyo_callee_no_es_nombre_ni_atributo_no_se_confunden_con_escrituras(fuente: str) -> None:
    """El callee que no es ``Name`` ni ``Attribute`` (otra llamada, un subíndice) no puede lanzar ni contar como disco."""

    assert revisar_fuente(fuente, "sintetico.py") == ()
