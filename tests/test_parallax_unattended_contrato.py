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


def test_el_escaneo_ve_todos_los_modulos() -> None:
    """Sin esto, una ruta mal escrita haría pasar el contrato sobre cero archivos."""

    vistos = {ruta.name for ruta in _PAQUETE.glob("*.py")}

    assert {"__init__.py", "auditor.py", "composicion.py", "contrato.py", "veredicto.py"} <= vistos


def test_el_paquete_cumple_su_contrato() -> None:
    hallazgos = verificar(_PAQUETE)

    assert hallazgos == (), "\n".join(f"{h.archivo}:{h.linea} {h.regla} — {h.detalle}" for h in hallazgos)


def test_la_api_publica_esta_completa() -> None:
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
        ('open(ruta, "w")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('open(ruta, mode="a")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('open(ruta, "r+")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.write_text("x")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ('ruta.write_bytes(b"x")', ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("ruta.unlink()", ReglaDeContrato.ESCRIBE_EN_DISCO),
        ("ruta.mkdir(parents=True)", ReglaDeContrato.ESCRIBE_EN_DISCO),
    ],
)
def test_cada_regla_tiene_su_caso(fuente: str, regla: ReglaDeContrato) -> None:
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
    ],
)
def test_lo_permitido_no_es_hallazgo(fuente: str) -> None:
    assert revisar_fuente(fuente, "sintetico.py") == ()


def test_una_sintaxis_invalida_se_reporta_y_no_rompe() -> None:
    hallazgos = revisar_fuente("def (: pass", "roto.py")

    assert [h.regla for h in hallazgos] == [ReglaDeContrato.SINTAXIS_INVALIDA]


def test_el_archivo_que_viola_se_detecta_desde_el_disco(tmp_path: pathlib.Path) -> None:
    (tmp_path / "limpio.py").write_text("import ast\n", encoding="utf-8", newline="\n")
    (tmp_path / "sucio.py").write_text("import numpy\n", encoding="utf-8", newline="\n")

    hallazgos = verificar(tmp_path)

    assert [(h.archivo, h.regla) for h in hallazgos] == [("sucio.py", ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL)]


def test_el_detector_reporta_la_linea_exacta() -> None:
    fuente = 'import ast\nimport numpy\n\ndef f():\n    open("x", "w")\n'
    hallazgos = revisar_fuente(fuente, "sintetico.py")

    assert [(h.linea, h.regla) for h in hallazgos] == [
        (2, ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL),
        (5, ReglaDeContrato.ESCRIBE_EN_DISCO),
    ]
