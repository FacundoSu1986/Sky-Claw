"""El contrato de esta capa, ejecutable. Las reglas de ``AGENTS.md`` dejan de ser prosa.

Qué se verifica, y por qué cada cosa
------------------------------------
1. **No importa al generador** (``sky_claw.local.native_parallax``). Es la regla que hace que
   esta capa no colisione con quien es dueño de ese módulo: si empezara a importar sus
   dataclasses, un refactor ajeno redefiniría en silencio qué significa "listo".
2. **No importa numpy ni Pillow.** Son un extra opcional del generador: el runtime base y el
   ejecutable congelado no los tienen, así que un import acá rompe donde tiene que correr.
3. **No escribe nada.** Ni archivos, ni logs, ni caché. Una capa que decide no puede además
   mutar el estado que está decidiendo.
4. **La API pública está completa.** Cada nombre de ``__all__`` existe, es público y se puede
   importar.

Se ejecuta de dos maneras, con el mismo código:

- ``python -m sky_claw.local.parallax_unattended.contrato`` (hook de pre-commit y a mano)
- desde ``tests/test_parallax_unattended_contrato.py``, que además prueba que el detector
  **detecta**: un verificador que siempre devuelve vacío pasaría este chequeo sin verificar
  nada.

Límite declarado: es análisis estático. Un ``getattr(Path, "write_" + "text")`` o un import
dinámico no se ven. Cubre lo que un humano escribe por descuido, no a alguien que lo evita a
propósito.
"""

from __future__ import annotations

import ast
import enum
import importlib
import pathlib
import sys
import types
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

#: Ruta de paquetes de esta capa para resolver imports relativos de cualquier profundidad.
PAQUETE_PROPIO: Final[tuple[str, ...]] = ("sky_claw", "local", "parallax_unattended")

#: Paquete que esta capa NO puede importar: tiene otro dueño.
MODULO_PROHIBIDO: Final[str] = "sky_claw.local.native_parallax"

#: Dependencias que sólo existen en el extra opcional del generador.
DEPENDENCIAS_PROHIBIDAS: Final[tuple[str, ...]] = ("numpy", "PIL")

#: Métodos de ``pathlib``/``os``/``shutil`` que mutan el disco.
METODOS_QUE_ESCRIBEN: Final[frozenset[str]] = frozenset(
    {
        "chmod",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "hardlink_to",
        "link_to",
        "makedirs",
        "mkdir",
        "move",
        "remove",
        "removedirs",
        "rename",
        "replace",
        "rmdir",
        "rmtree",
        "symlink_to",
        "touch",
        "truncate",
        "unlink",
        "utime",
        "write_bytes",
        "write_text",
    }
)

#: Caracteres de modo de ``open`` que implican escritura.
_MODOS_DE_ESCRITURA: Final[frozenset[str]] = frozenset("wax+")


class ReglaDeContrato(enum.StrEnum):
    """Qué regla se rompió. Vocabulario cerrado."""

    IMPORTA_GENERADOR = "importa_generador"
    IMPORTA_DEPENDENCIA_OPCIONAL = "importa_dependencia_opcional"
    ESCRIBE_EN_DISCO = "escribe_en_disco"
    API_INCOMPLETA = "api_incompleta"
    SINTAXIS_INVALIDA = "sintaxis_invalida"


@dataclass(frozen=True, slots=True)
class Hallazgo:
    archivo: str
    linea: int
    regla: ReglaDeContrato
    detalle: str


def _nombre_importado(nodo: ast.Import | ast.ImportFrom) -> Iterable[tuple[int, str]]:
    if isinstance(nodo, ast.Import):
        for alias in nodo.names:
            yield nodo.lineno, alias.name
        return
    modulo = nodo.module or ""
    if nodo.level:
        subida = max(0, len(PAQUETE_PROPIO) - nodo.level + 1)
        base_relativa = ".".join(PAQUETE_PROPIO[:subida])
        modulo = f"{base_relativa}.{modulo}".strip(".") if modulo else base_relativa
    if modulo:
        if _es_prohibido(modulo) is not None:
            yield nodo.lineno, modulo
            return
        for alias in nodo.names:
            yield nodo.lineno, f"{modulo}.{alias.name}"
    else:
        for alias in nodo.names:
            yield nodo.lineno, alias.name


def _es_prohibido(nombre: str) -> ReglaDeContrato | None:
    if nombre == MODULO_PROHIBIDO or nombre.startswith(MODULO_PROHIBIDO + "."):
        return ReglaDeContrato.IMPORTA_GENERADOR
    for dependencia in DEPENDENCIAS_PROHIBIDAS:
        if nombre == dependencia or nombre.startswith(dependencia + "."):
            return ReglaDeContrato.IMPORTA_DEPENDENCIA_OPCIONAL
    return None


def _modo_escribe(nodo: ast.Call, *, indices_posicionales: Sequence[int] = (1,)) -> bool:
    """Detecta modos con ``w``, ``a``, ``x`` o ``+`` en ``open`` / ``Path.open``."""

    candidatos: list[ast.expr] = [nodo.args[i] for i in indices_posicionales if i < len(nodo.args)]
    candidatos += [palabra.value for palabra in nodo.keywords if palabra.arg == "mode"]
    for candidato in candidatos:
        if (
            isinstance(candidato, ast.Constant)
            and isinstance(candidato.value, str)
            and any(caracter in _MODOS_DE_ESCRITURA for caracter in candidato.value)
        ):
            return True
    return False


def _revisar_llamada(nodo: ast.Call) -> bool:
    funcion = nodo.func
    if isinstance(funcion, ast.Name) and funcion.id == "open":
        return _modo_escribe(nodo, indices_posicionales=(1,))
    if isinstance(funcion, ast.Attribute):
        if funcion.attr == "open":
            if isinstance(funcion.value, ast.Name) and funcion.value.id == "os":
                return True
            if isinstance(funcion.value, ast.Name) and funcion.value.id in ("io", "codecs", "builtins"):
                return _modo_escribe(nodo, indices_posicionales=(1,))
            return _modo_escribe(nodo, indices_posicionales=(0, 1))
        return funcion.attr in METODOS_QUE_ESCRIBEN
    return False


def revisar_fuente(fuente: str, archivo: str = "<memoria>") -> tuple[Hallazgo, ...]:
    """Revisa un archivo fuente y devuelve sus violaciones. Puro: no toca el disco."""

    try:
        arbol = ast.parse(fuente, filename=archivo)
    except SyntaxError as exc:
        return (Hallazgo(archivo, exc.lineno or 0, ReglaDeContrato.SINTAXIS_INVALIDA, str(exc.msg)),)

    hallazgos: list[Hallazgo] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Import, ast.ImportFrom)):
            for linea, nombre in _nombre_importado(nodo):
                regla = _es_prohibido(nombre)
                if regla is not None:
                    hallazgos.append(Hallazgo(archivo, linea, regla, f"importa {nombre!r}"))
        elif isinstance(nodo, ast.Call) and _revisar_llamada(nodo):
            nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else "open"
            hallazgos.append(Hallazgo(archivo, nodo.lineno, ReglaDeContrato.ESCRIBE_EN_DISCO, f"llama a {nombre}()"))
    return tuple(sorted(hallazgos, key=lambda h: (h.archivo, h.linea)))


def verificar(raiz: pathlib.Path) -> tuple[Hallazgo, ...]:
    """Revisa todos los ``*.py`` del paquete. No importa el paquete: sólo lo lee."""

    hallazgos: list[Hallazgo] = []
    for ruta in sorted(raiz.rglob("*.py")):
        fuente = ruta.read_text(encoding="utf-8")
        hallazgos.extend(revisar_fuente(fuente, ruta.relative_to(raiz).as_posix()))
    return tuple(hallazgos)


def verificar_api() -> tuple[Hallazgo, ...]:
    """Cada nombre de ``__all__`` existe, es público y ningún símbolo público queda fuera."""

    paquete = importlib.import_module(__package__ or "sky_claw.local.parallax_unattended")
    exportados: Sequence[str] = tuple(getattr(paquete, "__all__", ()))
    hallazgos: list[Hallazgo] = []

    if not exportados:
        return (Hallazgo("__init__.py", 0, ReglaDeContrato.API_INCOMPLETA, "el paquete no declara __all__"),)
    conjunto_exportados = set(exportados)
    if len(conjunto_exportados) != len(exportados):
        hallazgos.append(Hallazgo("__init__.py", 0, ReglaDeContrato.API_INCOMPLETA, "__all__ tiene nombres repetidos"))
    for nombre in exportados:
        if nombre.startswith("_"):
            hallazgos.append(Hallazgo("__init__.py", 0, ReglaDeContrato.API_INCOMPLETA, f"{nombre!r} es privado"))
        if not hasattr(paquete, nombre):
            hallazgos.append(Hallazgo("__init__.py", 0, ReglaDeContrato.API_INCOMPLETA, f"{nombre!r} no existe"))
    for nombre, valor in sorted(vars(paquete).items()):
        if nombre.startswith("_") or nombre == "annotations" or isinstance(valor, types.ModuleType):
            continue
        if nombre not in conjunto_exportados:
            hallazgos.append(
                Hallazgo("__init__.py", 0, ReglaDeContrato.API_INCOMPLETA, f"{nombre!r} es público y falta en __all__")
            )
    return tuple(hallazgos)


def main() -> int:
    """0 si el contrato se cumple. Cualquier hallazgo sale 1, con el detalle en stderr.

    Escribe con ``sys.stderr.write`` y no con ``print`` porque el repo tiene un guard de
    pre-commit que prohíbe ``print(`` en ``sky_claw/``. La herramienta cumple el guard que la
    va a correr.
    """

    raiz = pathlib.Path(__file__).parent
    hallazgos = verificar(raiz) + verificar_api()
    if not hallazgos:
        sys.stderr.write(f"contrato OK: {len(list(raiz.rglob('*.py')))} archivos, API completa\n")
        return 0
    for hallazgo in hallazgos:
        sys.stderr.write(f"{hallazgo.archivo}:{hallazgo.linea}: {hallazgo.regla} — {hallazgo.detalle}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
