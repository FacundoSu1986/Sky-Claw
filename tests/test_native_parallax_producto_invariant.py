"""Ancla: el producto del generador nativo de parallax respeta el ADR 0014 (decisiones 2 y 3).

Qué congela, en lugar de muestrear:

- el CONJUNTO de módulos de producto de ``sky_claw/local/native_parallax/`` (todo lo que no es
  ``research/``) y que NINGUNO importa ``research`` (decisión 2: lo probado en research se reimplementa,
  no se importa; un test de equivalencia lo compara como oráculo). Un módulo nuevo rompe el censo hasta
  que se lo agregue acá, y el escáner de imports ya lo cubre por construcción;
- que ningún módulo de producto importe ``numpy`` ni ``PIL`` al cargarse (decisión 3: import perezoso,
  con error tipado si faltan);
- que el extra opcional ``native-parallax`` esté declarado con los MISMOS límites que ``dev`` y que
  ``numpy``/``pillow`` NO entren en ``dependencies`` ni en ``requirements.lock`` (el runtime base);
- que el lock propio del extra exista, fije ``numpy`` y ``pillow`` dentro de esos límites y que CI lo
  instale con ``--require-hashes`` y lo audite con ``pip-audit --strict``.

Límite: el escáner de imports ve ``import`` y ``from ... import`` (incluidos los relativos y los que
están dentro de funciones); un ``importlib.import_module("...research")`` con el nombre armado en
tiempo de ejecución no lo ve.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw" / "local" / "native_parallax"

MODULOS_DE_PRODUCTO_ESPERADOS = frozenset(
    {
        "__init__.py",
        "_extra.py",
        "bench/__init__.py",
        "bench/sintetico.py",
        "decode.py",
        "espectral.py",
        "integrate.py",
        "metrics.py",
    }
)
# Los únicos módulos donde se importa numpy DENTRO de una función (perezoso): ``_extra`` es el punto único para
# los que solo lo necesitan a él; ``decode`` necesita además Pillow y verifica las dos dependencias por su cuenta.
PUNTOS_DE_IMPORTACION_PEREZOSA_DE_NUMPY = frozenset({"_extra.py", "decode.py"})
# El único módulo de producto que calcula frecuencias espectrales: una segunda definición daría gradientes
# escalados por la resolución (bug real de NP-M0).
MODULOS_QUE_DEFINEN_FRECUENCIAS = frozenset({"espectral.py"})
# Los módulos que consumen la convención y la función de ``espectral`` que usa cada uno: el conjunto se detecta por
# AST (``test_los_consumidores_de_espectral_son_los_congelados``), así que un consumidor nuevo rompe el ancla hasta
# que se le escribe su receta acá.
CONSUMIDORES_DE_ESPECTRAL = {
    "bench/sintetico.py": "gradientes_espectrales",
    "integrate.py": "ejes_de_frecuencia",
    "metrics.py": "gradientes_espectrales",
}
FUNCIONES_DE_FRECUENCIA = ("fftfreq", "rfftfreq")
PESADOS = ("numpy", "PIL")
EXTRA = "native-parallax"
DEPENDENCIAS_DEL_EXTRA = frozenset({"numpy", "pillow"})
LOCK_DEL_EXTRA = "requirements-native-parallax.lock"


# ---------------------------------------------------------------------------
# Escáneres
# ---------------------------------------------------------------------------


def _modulos_de_producto(paquete: Path = PAQUETE) -> frozenset[str]:
    """Rutas relativas (posix) de los ``.py`` del paquete fuera de ``research/``."""
    return frozenset(
        ruta.relative_to(paquete).as_posix()
        for ruta in paquete.rglob("*.py")
        if "research" not in ruta.relative_to(paquete).parts and "__pycache__" not in ruta.parts
    )


def _nombres_importados(arbol: ast.AST) -> list[str]:
    """Nombres con punto de todo lo que importa el árbol, relativos incluidos (``.research``)."""
    nombres: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            base = "." * nodo.level + (nodo.module or "")
            nombres.append(base)
            # ``from . import research`` / ``from sky_claw.local.native_parallax import research``
            nombres.extend(f"{base}.{alias.name}" if base else alias.name for alias in nodo.names)
    return nombres


def _importa_research(codigo: str) -> bool:
    return any("research" in nombre.split(".") for nombre in _nombres_importados(ast.parse(codigo)))


def _es_type_checking(prueba: ast.expr) -> bool:
    """``TYPE_CHECKING`` o ``typing.TYPE_CHECKING`` como condición de un ``if``."""
    return (isinstance(prueba, ast.Name) and prueba.id == "TYPE_CHECKING") or (
        isinstance(prueba, ast.Attribute) and prueba.attr == "TYPE_CHECKING"
    )


def _importa_pesados_al_cargar(codigo: str) -> list[str]:
    """Imports de ``numpy``/``PIL`` que se EJECUTAN al cargar el módulo.

    Recorre las sentencias de nivel de módulo y entra en ``try``/``with``/``if``/``for``/``while``/``match`` y
    en el cuerpo de las clases (todo eso corre al cargar). Excluye el cuerpo de las funciones y el cuerpo
    de ``if TYPE_CHECKING:`` (su ``else`` sí corre).
    """
    encontrados: list[str] = []

    def visitar(sentencias: list[ast.stmt]) -> None:
        for nodo in sentencias:
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                nombres = _nombres_importados(ast.Module(body=[nodo], type_ignores=[]))
                encontrados.extend(n for n in nombres if n.split(".")[0] in PESADOS)
            elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            elif isinstance(nodo, ast.If) and _es_type_checking(nodo.test):
                visitar(nodo.orelse)
            else:
                for campo in ("body", "orelse", "finalbody"):
                    visitar(getattr(nodo, campo, []))
                for manejador in getattr(nodo, "handlers", []):
                    visitar(manejador.body)
                for caso in getattr(nodo, "cases", []):
                    visitar(caso.body)

    visitar(ast.parse(codigo).body)
    return encontrados


# ---------------------------------------------------------------------------
# Producto: sin research, import perezoso
# ---------------------------------------------------------------------------


def test_el_censo_de_modulos_de_producto_coincide_con_el_congelado() -> None:
    assert _modulos_de_producto() == MODULOS_DE_PRODUCTO_ESPERADOS


@pytest.mark.parametrize("modulo", sorted(MODULOS_DE_PRODUCTO_ESPERADOS))
def test_ningun_modulo_de_producto_importa_research(modulo: str) -> None:
    codigo = (PAQUETE / modulo).read_text(encoding="utf-8")

    assert not _importa_research(codigo), f"{modulo} importa research (ADR 0014, decisión 2)"


@pytest.mark.parametrize("modulo", sorted(MODULOS_DE_PRODUCTO_ESPERADOS))
def test_ningun_modulo_de_producto_importa_numpy_ni_pillow_al_cargarse(modulo: str) -> None:
    codigo = (PAQUETE / modulo).read_text(encoding="utf-8")

    assert _importa_pesados_al_cargar(codigo) == [], f"{modulo}: import no perezoso (ADR 0014, decisión 3)"


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("import sky_claw.local.native_parallax.research.metrics as m", True),
        ("from sky_claw.local.native_parallax.research import metrics", True),
        ("from sky_claw.local.native_parallax.research.metrics import f", True),
        ("from . import research", True),
        ("from .research import metrics", True),
        ("from .research.metrics import f", True),
        ("from sky_claw.local.native_parallax import research", True),
        ("def f():\n    from .research import x\n", True),
        ("import sky_claw.local.native_parallax.decode", False),
        ("from . import decode", False),
        ("from .decode import decodificar_normal", False),
        ("import researcher", False),
    ],
)
def test_el_escaner_de_research_ve_todas_las_formas_de_import(codigo: str, esperado: bool) -> None:
    assert _importa_research(codigo) is esperado


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("import numpy as np", ["numpy"]),
        ("from PIL import Image", ["PIL", "PIL.Image"]),
        ("import numpy.typing", ["numpy.typing"]),
        ("def f():\n    import numpy as np\n", []),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy as np\n", []),
        ("import json\nimport os", []),
        # imports condicionales o protegidos: corren al cargar el módulo (revisión de Codex en #756)
        ("try:\n    import numpy as np\nexcept ImportError:\n    np = None\n", ["numpy"]),
        ("try:\n    pass\nexcept ImportError:\n    import numpy as np\n", ["numpy"]),
        ("try:\n    pass\nfinally:\n    import numpy as np\n", ["numpy"]),
        ("import sys\nif sys.platform == 'win32':\n    from PIL import Image\n", ["PIL", "PIL.Image"]),
        ("import contextlib\nwith contextlib.suppress(ImportError):\n    import numpy\n", ["numpy"]),
        ("class A:\n    import numpy\n", ["numpy"]),
        ("if False:\n    pass\nelse:\n    import numpy\n", ["numpy"]),
        # el `else` de TYPE_CHECKING corre al cargar; el cuerpo no
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import numpy as np\n",
            ["numpy"],
        ),
        ("import typing\nif typing.TYPE_CHECKING:\n    import numpy as np\n", []),
        ("def f():\n    try:\n        import numpy\n    except ImportError:\n        pass\n", []),
        ("async def f():\n    import numpy\n", []),
    ],
)
def test_el_escaner_de_imports_pesados_distingue_modulo_de_funcion_y_type_checking(
    codigo: str, esperado: list[str]
) -> None:
    assert _importa_pesados_al_cargar(codigo) == esperado


# ---------------------------------------------------------------------------
# Puntos únicos: importación perezosa de numpy y convención de frecuencias (NP-I0)
# ---------------------------------------------------------------------------


def _importa_numpy_dentro_de_funciones(codigo: str) -> bool:
    """``True`` si alguna función (de cualquier profundidad) importa ``numpy``; el import de módulo no cuenta."""
    for nodo in ast.walk(ast.parse(codigo)):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for interno in ast.walk(nodo):
                if isinstance(interno, (ast.Import, ast.ImportFrom)):
                    nombres = _nombres_importados(ast.Module(body=[interno], type_ignores=[]))
                    if any(n.split(".")[0] == "numpy" for n in nombres):
                        return True
    return False


def _usa_funcion_de_frecuencia(codigo: str) -> bool:
    """``np.fft.fftfreq(...)``, ``fftfreq(...)`` o ``from numpy.fft import fftfreq`` (y ``rfftfreq``)."""
    for nodo in ast.walk(ast.parse(codigo)):
        if isinstance(nodo, ast.Attribute) and nodo.attr in FUNCIONES_DE_FRECUENCIA:
            return True
        if isinstance(nodo, ast.Name) and nodo.id in FUNCIONES_DE_FRECUENCIA:
            return True
        if isinstance(nodo, ast.ImportFrom) and any(a.name in FUNCIONES_DE_FRECUENCIA for a in nodo.names):
            return True
    return False


def _define_ejes_de_frecuencia(codigo: str) -> bool:
    return any(
        isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and "ejes_de_frecuencia" in nodo.name
        for nodo in ast.walk(ast.parse(codigo))
    )


def _modulos_que(predicado) -> frozenset[str]:  # type: ignore[no-untyped-def]
    return frozenset(
        modulo for modulo in _modulos_de_producto() if predicado((PAQUETE / modulo).read_text(encoding="utf-8"))
    )


def test_numpy_se_importa_dentro_de_funciones_solo_en_los_puntos_congelados() -> None:
    assert _modulos_que(_importa_numpy_dentro_de_funciones) == PUNTOS_DE_IMPORTACION_PEREZOSA_DE_NUMPY


def test_solo_un_modulo_de_producto_calcula_frecuencias_espectrales() -> None:
    assert _modulos_que(_usa_funcion_de_frecuencia) == MODULOS_QUE_DEFINEN_FRECUENCIAS


def test_solo_un_modulo_de_producto_define_los_ejes_de_frecuencia() -> None:
    assert _modulos_que(_define_ejes_de_frecuencia) == MODULOS_QUE_DEFINEN_FRECUENCIAS


ESPECTRAL = "sky_claw.local.native_parallax.espectral"


def _importa_de_espectral(codigo: str) -> bool:
    """``True`` si el código importa el módulo ``espectral`` por CUALQUIER forma de sentencia de importación.

    Absolutas (``from ...espectral import f``, ``from ...native_parallax import espectral``,
    ``import ...espectral``) y relativas. Las relativas no se resuelven contra el paquete del módulo escaneado:
    se detecta por el nombre, así que puede sobre-detectar (un ``from .x import espectral`` ajeno), y eso es
    deliberado. Sobre-detectar rompe el ancla y obliga a escribir la receta del consumidor; sub-detectar lo deja
    pasar sin que nadie lo vea. Límite: una importación dinámica (``importlib.import_module``) no es una sentencia
    de importación y este escáner no la ve.
    """
    paquete, nombre = ESPECTRAL.rsplit(".", 1)
    for nodo in ast.walk(ast.parse(codigo)):
        if isinstance(nodo, ast.Import):
            if any(alias.name == ESPECTRAL for alias in nodo.names):
                return True
        elif isinstance(nodo, ast.ImportFrom):
            modulo = nodo.module or ""
            importa_el_nombre = any(alias.name == nombre for alias in nodo.names)
            if nodo.level == 0:
                if modulo == ESPECTRAL or (modulo == paquete and importa_el_nombre):
                    return True
            elif modulo.rsplit(".", 1)[-1] == nombre or importa_el_nombre:
                return True
    return False


def test_los_consumidores_de_espectral_son_los_congelados() -> None:
    assert _modulos_que(_importa_de_espectral) == frozenset(CONSUMIDORES_DE_ESPECTRAL)


@pytest.mark.parametrize("modulo", sorted(CONSUMIDORES_DE_ESPECTRAL))
def test_los_consumidores_de_frecuencias_las_importan_de_espectral(modulo: str) -> None:
    arbol = ast.parse((PAQUETE / modulo).read_text(encoding="utf-8"))
    importados = {
        (nodo.module, alias.name)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.ImportFrom)
        for alias in nodo.names
    }

    assert (ESPECTRAL, CONSUMIDORES_DE_ESPECTRAL[modulo]) in importados


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("def f():\n    import numpy\n", True),
        ("def f():\n    import numpy as np\n", True),
        ("def f():\n    from numpy import fft\n", True),
        ("def f():\n    import numpy.typing\n", True),
        ("async def f():\n    import numpy\n", True),
        ("def f():\n    def g():\n        import numpy\n", True),
        ("class A:\n    def m(self):\n        import numpy\n", True),
        ("import numpy\n", False),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import numpy as np\n", False),
        ("def f():\n    import json\n", False),
        ("def f():\n    import numpyish\n", False),
    ],
)
def test_el_escaner_de_importacion_perezosa_de_numpy_distingue_funcion_y_modulo(codigo: str, esperado: bool) -> None:
    assert _importa_numpy_dentro_de_funciones(codigo) is esperado


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("import numpy as np\nx = np.fft.fftfreq(8)", True),
        ("import numpy as np\nx = np.fft.rfftfreq(8)", True),
        ("from numpy.fft import fftfreq\n", True),
        ("from numpy.fft import fftfreq as f\n", True),
        ("from numpy.fft import rfftfreq\n", True),
        ("x = fftfreq(8)", True),
        ("import scipy.fft as sf\nx = sf.fftfreq(8)", True),
        ("import numpy as np\nx = np.fft.fft2(a)", False),
        ("x = 2.0 * np.pi", False),
        ("def ejes_de_frecuencia(): pass", False),
    ],
)
def test_el_escaner_de_frecuencias_ve_todas_las_formas_de_llamada(codigo: str, esperado: bool) -> None:
    assert _usa_funcion_de_frecuencia(codigo) is esperado


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("from sky_claw.local.native_parallax.espectral import gradientes_espectrales\n", True),
        ("from sky_claw.local.native_parallax.espectral import gradientes_espectrales as g\n", True),
        ("from sky_claw.local.native_parallax import espectral\n", True),
        ("from sky_claw.local.native_parallax import espectral as e\n", True),
        ("from sky_claw.local.native_parallax import decode, espectral\n", True),
        ("import sky_claw.local.native_parallax.espectral\n", True),
        ("import sky_claw.local.native_parallax.espectral as e\n", True),
        ("from . import espectral\n", True),
        ("from .espectral import gradientes_espectrales\n", True),
        ("from .. import espectral\n", True),
        ("from ..espectral import gradientes_espectrales\n", True),
        ("def f():\n    from sky_claw.local.native_parallax import espectral\n", True),
        ("from sky_claw.local.native_parallax import decode\n", False),
        ("from sky_claw.local.native_parallax.integrate import integrar\n", False),
        ("from . import decode\n", False),
        ("from .decode import decodificar\n", False),
        ("import sky_claw.local.native_parallax.decode\n", False),
        ("from sky_claw.local.native_parallax.espectral_otro import algo\n", False),
        ("from otro_paquete import espectral\n", False),
        ("import numpy as np\n", False),
    ],
)
def test_el_escaner_de_consumidores_de_espectral_ve_todas_las_formas_de_importar(codigo: str, esperado: bool) -> None:
    assert _importa_de_espectral(codigo) is esperado


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("def ejes_de_frecuencia(): pass", True),
        ("def _ejes_de_frecuencia(): pass", True),
        ("async def mis_ejes_de_frecuencia_v2(): pass", True),
        ("class A:\n    def ejes_de_frecuencia(self): pass", True),
        ("def otra_cosa(): pass", False),
        ("from x import ejes_de_frecuencia", False),
    ],
)
def test_el_escaner_de_definiciones_de_ejes_ve_las_variantes_de_nombre(codigo: str, esperado: bool) -> None:
    assert _define_ejes_de_frecuencia(codigo) is esperado


def test_el_censo_de_modulos_de_producto_ignora_research_y_caches(tmp_path: Path) -> None:
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "metrics.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "decode.cpython-311.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "decode.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "otro.py").write_text("x = 1\n", encoding="utf-8")

    assert _modulos_de_producto(tmp_path) == {"decode.py", "sub/otro.py"}


# ---------------------------------------------------------------------------
# Dependencias: el extra existe y el runtime base no cambia
# ---------------------------------------------------------------------------


def _pyproject() -> dict[str, object]:
    return tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))


def _requisitos(lista: object) -> dict[str, Requirement]:
    assert isinstance(lista, list)
    return {canonicalize_name(Requirement(texto).name): Requirement(texto) for texto in lista}


def test_el_extra_native_parallax_declara_numpy_y_pillow_con_los_limites_de_dev() -> None:
    proyecto = _pyproject()["project"]
    assert isinstance(proyecto, dict)
    extras = proyecto["optional-dependencies"]
    assert isinstance(extras, dict)
    assert EXTRA in extras, "falta el extra native-parallax (ADR 0014, decisión 3)"

    del_extra = _requisitos(extras[EXTRA])
    de_dev = _requisitos(extras["dev"])

    assert frozenset(del_extra) == DEPENDENCIAS_DEL_EXTRA
    for nombre in DEPENDENCIAS_DEL_EXTRA:
        assert nombre in de_dev, f"dev ya no incluye {nombre}: los tests del producto lo necesitan"
        assert del_extra[nombre].specifier == de_dev[nombre].specifier, f"{nombre}: el extra y dev difieren"


def test_numpy_y_pillow_no_estan_en_las_dependencias_del_runtime_base() -> None:
    proyecto = _pyproject()["project"]
    assert isinstance(proyecto, dict)

    base = _requisitos(proyecto["dependencies"])

    assert DEPENDENCIAS_DEL_EXTRA.isdisjoint(base)


def _pines(texto: str) -> dict[str, str]:
    """``nombre==versión`` de un lock de ``uv pip compile`` (líneas sin sangría ni comentario)."""
    return {
        canonicalize_name(m.group(1)): m.group(2)
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", texto, flags=re.MULTILINE)
    }


def test_el_lock_del_runtime_base_no_fija_numpy_ni_pillow() -> None:
    pines = _pines((RAIZ / "requirements.lock").read_text(encoding="utf-8"))

    assert DEPENDENCIAS_DEL_EXTRA.isdisjoint(pines)


def test_el_lock_del_extra_fija_numpy_y_pillow_dentro_de_los_limites_con_hashes() -> None:
    texto = (RAIZ / LOCK_DEL_EXTRA).read_text(encoding="utf-8")
    proyecto = _pyproject()["project"]
    assert isinstance(proyecto, dict)
    extras = proyecto["optional-dependencies"]
    assert isinstance(extras, dict)
    limites = _requisitos(extras[EXTRA])

    pines = _pines(texto)

    assert "--extra native-parallax" in texto.splitlines()[1], "la cabecera debe declarar cómo se compiló"
    assert "--generate-hashes" in texto.splitlines()[1]
    for nombre in DEPENDENCIAS_DEL_EXTRA:
        assert nombre in pines, f"el lock del extra no fija {nombre}"
        assert SpecifierSet(str(limites[nombre].specifier)).contains(Version(pines[nombre]))
        bloque = texto.split(f"\n{nombre}=={pines[nombre]}", 1)[1].split("\n    # via", 1)[0]
        assert "--hash=sha256:" in bloque, f"{nombre} sin hashes en el lock del extra"


def test_el_lock_del_extra_coincide_con_el_del_runtime_base_salvo_el_extra() -> None:
    """Las dos locks no pueden discrepar sobre un paquete compartido: el extra SUMA, no cambia pines."""
    base = _pines((RAIZ / "requirements.lock").read_text(encoding="utf-8"))
    extra = _pines((RAIZ / LOCK_DEL_EXTRA).read_text(encoding="utf-8"))

    discrepancias = {n: (base[n], extra[n]) for n in base if n in extra and base[n] != extra[n]}
    faltantes = set(base) - set(extra)

    assert discrepancias == {}
    assert faltantes == set()


# ---------------------------------------------------------------------------
# CI: el lock del extra se instala con hashes y se audita
# ---------------------------------------------------------------------------


def test_ci_instala_el_lock_del_extra_con_hashes_y_lo_audita_en_modo_estricto() -> None:
    lineas = (RAIZ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()

    instala = [ln for ln in lineas if "--require-hashes" in ln and LOCK_DEL_EXTRA in ln]
    audita = [ln for ln in lineas if "pip-audit" in ln and "--strict" in ln and LOCK_DEL_EXTRA in ln]

    assert len(instala) == 1, "CI debe instalar el lock del extra con --require-hashes"
    assert len(audita) == 1, "CI debe auditar el lock del extra con pip-audit --strict"
