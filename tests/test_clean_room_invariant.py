"""Ancla de la política clean-room del generador nativo de parallax (ADR 0013).

Por qué existe: ``CLEAN_ROOM.md`` nació como propuesta (NP-R0) y nada la hacía cumplir, mientras
el plan P0 ya contenía citas de scripts y strings de binarios de herramientas cerradas. «Ninguna
decisión de diseño se apoya en ParallaxR» era una frase del README, no una propiedad verificada
(``AGENTS.md``: escribir el racional no cuenta como verificarlo).

Enumera en vez de muestrear (ver «La regla que más se viola» en ``AGENTS.md``): recorre **todos**
los archivos trackeados por git y no descarta ninguno por tamaño, por codificación ni por ser
binario. Un archivo nuevo con material de una herramienta cerrada rompe el test aunque nadie lo
haya listado antes.

Qué verifica, de la propiedad más fuerte a la más débil:

1. **Contenido en cuarentena.** Las marcas de contenido (citas de scripts, etiquetas de evidencia T2)
   solo pueden aparecer *dentro* de las secciones señalizadas (``SECCIONES_EN_CUARENTENA``). Ninguna línea
   de esas secciones (de al menos ``LARGO_MINIMO_DE_HUELLA`` caracteres, normalizada) puede reaparecer
   fuera de ellas, ni re-cortada ni incrustada en otro texto, y ningún tramo de al menos
   ``2·LARGO_DEL_TROZO−1`` caracteres puede aparecer fuera de los contratos de integración (huellas): así
   también se detecta un fragmento de script copiado SIN ninguna etiqueta.
2. **Identidad.** Nombres de ejecutables internos y hashes de artefactos solo en contratos de
   integración y evidencia (``PERIMETRO_DE_IDENTIDAD``).
3. **Zona limpia** (toda ruta trackeada que nombre el generador nativo, definida por patrón): ni una
   marca, ni una huella, y sus binarios congelados por enumeración.
4. **Ejecutables.** Ningún PE/ELF/Mach-O trackeado, por cabecera y no solo por extensión.
5. **Política y evidencia.** Las cláusulas de ``CLEAN_ROOM.md`` están congeladas, los avisos presentes
   y el registro B1 coincide con el contrato de materiales.

LÍMITES (declarados, no ocultos): el ancla NO puede detectar contenido derivado que no esté en este
repo. Un script de la herramienta reescrito con otras palabras, salidas suyas convertidas a otro formato
y copias parciales de menos de ``2·LARGO_DEL_TROZO−1`` caracteres (según dónde caigan) pasan; eso
depende de la revisión humana y del procedimiento de ``CLEAN_ROOM.md``. Las cláusulas se congelan por
frase, no por significado.

Deliberadamente NO se barre la palabra «ParallaxR»: el nombre del producto es un contrato de
integración legítimo (entrypoint, markers, step id) y decirlo en un docstring clean-room no es
contaminación. El ancla protege el *contenido derivado*, no cada mención léxica.

Este archivo se excluye a sí mismo del barrido (sus propias expresiones regulares contienen las
marcas); ``test_el_propio_test_existe_donde_se_lo_excluye`` impide que esa exclusión quede huérfana.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[1]
ESTE_ARCHIVO = "tests/test_clean_room_invariant.py"

P0_V3 = "docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md"
P0_EVIDENCIA = "docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md"
SPEC_ASISTIDO = "docs/design/specs/2026-09-21-parallaxr-assisted-external-mode.md"
TEST_ASISTIDO = "tests/test_parallaxr_assisted.py"
ADR = "docs/adr/0013-clean-room-native-parallax.md"
PUNTERO_LOCAL = "sky_claw/local/native_parallax/AGENTS.md"
STUB_VIEJO = "docs/design/research/2026-09-21-native-parallax-battle/CLEAN_ROOM.md"
REGISTRO_B1_MD = "docs/audits/2026-10-07_b1_permisos_r_suite.md"
REGISTRO_B1_JSON = "docs/audits/data/2026-10-07_b1_permisos_r_suite.json"

BANNER = "CUARENTENA CLEAN-ROOM"

# Secciones en cuarentena: (archivo, prefijo del encabezado). El rango de cada una va desde su
# encabezado hasta el próximo encabezado de nivel igual o menor, sin contar los bloques cercados.
SECCIONES_EN_CUARENTENA: tuple[tuple[str, str], ...] = (
    (P0_V3, "### 2.2 "),
    (P0_V3, "### 2.4 "),
    (P0_EVIDENCIA, "### 6.2 "),
)
# Además del aviso de cada sección, el P0 v3 lleva uno global arriba (el contenido en cuarentena
# es solo una parte del documento y quien lo abre debe saberlo antes de llegar a ella).
ARCHIVOS_CON_AVISO_GLOBAL = frozenset({P0_V3})
LARGO_MINIMO_DE_HUELLA = 30
# Copias PARCIALES: cada huella se parte en trozos alineados de este largo. Toda subcadena de una huella de
# al menos 2·k−1 (= 79) caracteres contiene algún trozo completo, así que esa copia no se escapa; una
# de entre k y 2·k−2 se detecta según dónde caiga. Con k = 30 aparecían falsos positivos en scripts propios.
LARGO_DEL_TROZO = 40
SEPARADOR_DE_TABLA = re.compile(r"[|\-: ]+")

# Contenido derivado de leer scripts o inspeccionar binarios de herramientas cerradas.
MARCAS_DE_CONTENIDO = {
    "cita_de_script": re.compile(r"\b(?:PxR|BENDr) \d{1,3}\b"),
    "etiqueta_t2": re.compile(r"T2 — (?:strings de|`Exclusions)"),
}
# Identidad de artefactos internos: nombres de ejecutables y prefijos de hash.
MARCAS_DE_IDENTIDAD = {
    "nombre_de_helper": re.compile(
        r"\b(?:ExtractBSA|MakeUnpack|LooseCopy|ParallaxRFilter|OutputQC|HeightMap|BENDrFilter|BENDr|Exclusions)"
        r"\.(?:exe|mod)\b"
    ),
    "hash_de_artefacto": re.compile(
        r"\b(?:bbc2035e|ca9105de|99b8aeef|f9184150|f345d1b0|1e1ac9a5|a6ae25d7|4a7fabc4|72d9e191|bd6f7887)"
    ),
}
# La identidad se admite en contratos de integración y evidencia. Un archivo nuevo entra a esta lista
# solo si es un contrato de integración; si contiene contenido derivado, va a cuarentena, no acá.
PERIMETRO_DE_IDENTIDAD = frozenset({P0_V3, P0_EVIDENCIA, SPEC_ASISTIDO, TEST_ASISTIDO})

# La zona limpia se define por PATRÓN y no por lista: toda ruta trackeada que nombre el generador
# nativo queda adentro, también los directorios que se creen mañana.
PATRON_DE_ZONA_LIMPIA = re.compile(r"native[-_]parallax", re.IGNORECASE)
RUTAS_CONOCIDAS_DE_LA_ZONA = (
    "sky_claw/local/native_parallax/__init__.py",
    "docs/design/research/native-parallax/np-m0-results.md",
    "docs/design/research/2026-09-21-native-parallax-battle/README.md",
    "tests/test_native_parallax_math_spike.py",
)
# Binarios admitidos en la zona limpia, congelados por enumeración: cada uno es una imagen propia de
# NP-M0 (verdad terreno sintética y su reconstrucción). Una imagen nueva exige sumarla acá a
# sabiendas; es el momento de confirmar que no es salida de una herramienta cerrada.
BINARIOS_ADMITIDOS_EN_LA_ZONA = frozenset(
    {
        "docs/design/research/native-parallax/assets/N02_gt.png",
        "docs/design/research/native-parallax/assets/N02_rec.png",
        "docs/design/research/native-parallax/assets/S08_err.png",
        "docs/design/research/native-parallax/assets/S08_gt.png",
        "docs/design/research/native-parallax/assets/S08_rec.png",
    }
)
EXTENSIONES_DE_EJECUTABLE = frozenset({".exe", ".dll", ".msi", ".so", ".dylib", ".pyd"})
# Congelado vacío: el repo no versiona ningún binario ejecutable. Agregar uno exige justificarlo acá.
EJECUTABLES_ADMITIDOS: frozenset[str] = frozenset()
CABECERAS_MACHO = (
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
)

# Cláusulas de CLEAN_ROOM.md congeladas por sección (frase, no significado). Las claves, en orden,
# son los encabezados de la política: agregar una sección sin sus cláusulas rompe el test.
CLAUSULAS_DE_LA_POLITICA: dict[str, tuple[str, ...]] = {
    "## Principio": (
        "exclusivamente a partir de",
        "matemáticas públicas",
        "papers académicos",
        "experimentación propia",
    ),
    "## PROHIBIDO aceptar en el repo (código, issues, PRs, docs, conversaciones)": (
        "scripts (BAT/PowerShell)",
        "EXE/DLL",
        "Strings extraídas de binarios de terceros",
        "decompilación",
        "bases de exclusiones",
        "tainted contributions",
        "etiqueta de entrenamiento",
        "destilación",
        "texturas de terceros con derechos",
        "material en cuarentena",
    ),
    "## SÍ se permite estudiar y usar": (
        "documentado públicamente",
        "CC0/CC-BY",
        "Comparar a ciegas",
        "evaluación",
        "#676",
        "registro B1",
        "no retroalimenta el diseño",
        "solo hashes y métricas",
    ),
    "## Regla de decisión": (
        "ParallaxR lo hace así",
        "se descarta",
        "fuente pública o un experimento propio",
    ),
    "## Trazabilidad": (
        "estado epistémico",
        "nunca se commitean",
        "cita su fuente pública o el experimento propio",
    ),
    "## Perímetro de cuarentena": (
        "no es fuente",
        "no lee",
        "no crece",
        "no se copia, no se parafrasea",
    ),
    "## Exposición previa (reconocida)": (
        "dos equipos",
        "no es alcanzable",
        "procedencia verificable",
    ),
    "## Si se detecta contaminación": (
        "Parar",
        "No propagar",
        "Registrar",
        "Re-derivar",
        "no se reescribe",
    ),
    "## Permisos de las herramientas cerradas (B1)": (
        "MANUAL_ONLY",
        "NO_VENDOR",
        "NO_BUNDLE",
        "NO_REDISTRIBUTE",
        "B6-L",
        "no es asesoramiento legal",
    ),
    "## Anclas": (
        ESTE_ARCHIVO,
        "huellas",
        "exentos solo de este chequeo de tramos",
        "siguen prohibidas también ahí",
        "no puede detectar",
    ),
}
ENCABEZADOS_DE_LA_POLITICA = list(CLAUSULAS_DE_LA_POLITICA)

POLITICA_B1_VIGENTE = ["MANUAL_ONLY", "NO_VENDOR", "NO_BUNDLE", "NO_REDISTRIBUTE"]
# Vocabulario cerrado de `invocacion_directa_de_helpers` en el registro B1. Solo AUTORIZADA (permiso
# escrito y fechado del autor) puede levantar el bloqueo B6_L del contrato; todo lo demás, incluido
# un valor que no se reconoce, deja el bloqueo en pie (falla cerrado).
ESTADOS_DE_PERMISO = frozenset({"ABIERTA", "DENEGADA", "AUTORIZADA"})
ESTADO_QUE_LEVANTA_EL_BLOQUEO = "AUTORIZADA"


# --------------------------------------------------------------------------- lectura y barrido


def _accion_sin_git(entorno: Mapping[str, str]) -> str:
    """``"fallar"`` en CI (un ancla que se saltea sola ahí es un ancla apagada); ``"saltar"`` fuera."""
    return "fallar" if entorno.get("GITHUB_ACTIONS") == "true" else "saltar"


@cache
def _archivos_trackeados() -> tuple[str, ...]:
    if shutil.which("git") is None or not (RAIZ / ".git").exists():
        motivo = "distribución sin .git: el ancla enumera con `git ls-files`"
        if _accion_sin_git(os.environ) == "fallar":
            pytest.fail(f"{motivo}; en CI no puede saltearse", pytrace=False)
        pytest.skip(motivo)
    salida = subprocess.run(["git", "ls-files", "-z"], cwd=RAIZ, check=True, capture_output=True).stdout
    return tuple(ruta for ruta in salida.decode("utf-8").split("\0") if ruta)


def _leer(ruta: str) -> str | None:
    """Texto UTF-8 estricto de un documento que el test parsea, o ``None`` si falta o no decodifica."""
    try:
        return (RAIZ / ruta).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _texto_requerido(ruta: str) -> str:
    texto = _leer(ruta)
    assert texto is not None, f"falta o es ilegible: {ruta}"
    return texto


def _lineas(ruta: str) -> list[str]:
    return _texto_requerido(ruta).splitlines()


def _vista_de_barrido(datos: bytes) -> str:
    """Texto sobre el que se buscan marcas, para CUALQUIER contenido (nunca se descarta un archivo).

    Con BOM UTF-16 (o UTF-32 LE, que empieza igual) se decodifica como tal: el BOM suelto pegado a la
    primera palabra le quitaría el límite de palabra a una marca que abra el archivo. Sin BOM pero con
    NUL se descartan los NUL y se decodifica latin-1, de modo que un UTF-16 sin BOM o un binario exponen
    su texto ASCII. Sin NUL: UTF-8 con reemplazo, así un cp1252 sigue mostrando sus marcas ASCII.
    """
    if datos[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return datos.decode("utf-16", errors="replace").replace("\0", "")
    if b"\0" in datos:
        return datos.replace(b"\0", b"").decode("latin-1")
    return datos.decode("utf-8", errors="replace")


def _es_ejecutable(datos: bytes) -> bool:
    """PE, ELF o Mach-O por su cabecera: un ``.exe`` renombrado a ``.bin`` sigue siéndolo."""
    if datos[:4] == b"\x7fELF" or datos[:4] in CABECERAS_MACHO:
        return True
    return datos[:2] == b"MZ" and b"PE\0\0" in datos[:65536]


@dataclass(frozen=True, slots=True)
class _Archivo:
    ruta: str
    texto: str  # vista de barrido: siempre hay texto, aunque el archivo sea binario
    es_binario: bool  # NUL en los primeros 8 KiB: no se puede revisar como texto
    es_ejecutable: bool


@cache
def _barrido() -> tuple[_Archivo, ...]:
    """TODOS los archivos trackeados presentes en el árbol de trabajo, salvo este test."""
    archivos: list[_Archivo] = []
    for ruta in _archivos_trackeados():
        if ruta == ESTE_ARCHIVO:
            continue
        try:
            datos = (RAIZ / ruta).read_bytes()
        except FileNotFoundError:
            continue  # trackeado pero borrado en el árbol de trabajo: no hay contenido que revisar
        except OSError as error:
            raise AssertionError(f"archivo trackeado ilegible: {ruta} ({error})") from error
        archivos.append(_Archivo(ruta, _vista_de_barrido(datos), b"\0" in datos[:8192], _es_ejecutable(datos)))
    return tuple(archivos)


def _textos() -> list[tuple[str, str]]:
    return [(archivo.ruta, archivo.texto) for archivo in _barrido()]


def _en_zona_limpia(ruta: str) -> bool:
    return PATRON_DE_ZONA_LIMPIA.search(ruta) is not None


# --------------------------------------------------------------------------- secciones y huellas

_ENCABEZADO_MD = re.compile(r"^(#{1,6}) ")
_CERCO_MD = re.compile(r"^(`{3,}|~{3,})(.*)$")


def _cerco_de(linea: str) -> tuple[str, str] | None:
    """``(marca, resto)`` si la línea es un cerco de bloque de código (tres o más ` o ~), si no ``None``.

    La sangría no cuenta: un bloque cercado dentro de un ítem de lista lleva más de tres espacios y sus
    ``# comentarios`` tampoco son encabezados.
    """
    coincide = _CERCO_MD.match(linea.lstrip())
    return (coincide.group(1), coincide.group(2)) if coincide else None


def _rango_de_seccion(lineas: Sequence[str], prefijo: str) -> tuple[int, int]:
    """``[inicio, fin)`` (índices base 0) de la sección cuyo encabezado empieza con ``prefijo``.

    Termina en el próximo encabezado de nivel igual o menor. Las líneas dentro de un bloque cercado
    nunca son encabezados: el ``# comentario`` de un script no corta la sección (en el P0 v3 eso
    recortaba el §2.4 y dejaba su cola, que es contenido en cuarentena, sin rango). Un cerco cierra solo
    si es del mismo tipo, igual o más largo y no lleva texto (CommonMark): un ```` ```bash ```` anidado
    dentro de un bloque de cuatro comillas no lo cierra.
    """
    cercado: str | None = None
    inicio: int | None = None
    nivel = 0
    for indice, linea in enumerate(lineas):
        cerco = _cerco_de(linea)
        if cerco is not None:
            marca, resto = cerco
            if cercado is None:
                if not (marca[0] == "`" and "`" in resto):  # el texto de un cerco de ` no lleva `
                    cercado = marca
                    continue
            elif marca[0] == cercado[0] and len(marca) >= len(cercado) and not resto.strip():
                cercado = None
                continue
        if cercado is not None:
            continue
        encabezado = _ENCABEZADO_MD.match(linea)
        if encabezado is None:
            continue
        if inicio is None:
            if linea.startswith(prefijo):
                inicio, nivel = indice, len(encabezado.group(1))
        elif len(encabezado.group(1)) <= nivel:
            return inicio, indice
    assert inicio is not None, f"no encuentro el encabezado que empieza con {prefijo!r}"
    return inicio, len(lineas)


def _cuerpo_de_seccion(texto: str, prefijo: str) -> str:
    lineas = texto.splitlines()
    inicio, fin = _rango_de_seccion(lineas, prefijo)
    return "\n".join(lineas[inicio + 1 : fin])


@cache
def _rangos_en_cuarentena() -> dict[str, tuple[tuple[int, int], ...]]:
    """``{archivo: ((inicio, fin), ...)}`` de las secciones en cuarentena, con su encabezado incluido."""
    rangos: dict[str, list[tuple[int, int]]] = {}
    for ruta, prefijo in SECCIONES_EN_CUARENTENA:
        rangos.setdefault(ruta, []).append(_rango_de_seccion(_lineas(ruta), prefijo))
    return {ruta: tuple(rs) for ruta, rs in rangos.items()}


def _huella(linea: str) -> str:
    """Línea normalizada (espacios y mayúsculas) para comparar copias literales."""
    return " ".join(linea.lstrip(chr(0xFEFF)).split()).casefold()


def _huellas_de_seccion(lineas: Iterable[str]) -> dict[str, int]:
    """``{huella: índice}`` de las líneas de CUERPO de una sección (sin su aviso, separadores ni líneas cortas).

    El índice es la posición de la primera aparición dentro de ``lineas``: sirve para decir QUÉ línea en
    cuarentena se copió sin volver a imprimir su contenido.
    """
    huellas: dict[str, int] = {}
    en_aviso = False
    for indice, linea in enumerate(lineas):
        if BANNER in linea:
            en_aviso = True
            continue
        if en_aviso and linea.lstrip().startswith(">"):
            continue
        en_aviso = False
        huella = _huella(linea)
        if len(huella) >= LARGO_MINIMO_DE_HUELLA and not SEPARADOR_DE_TABLA.fullmatch(huella):
            huellas.setdefault(huella, indice)
    return huellas


def _huellas_por_seccion() -> dict[tuple[str, str], dict[str, int]]:
    huellas = {}
    for ruta, prefijo in SECCIONES_EN_CUARENTENA:
        lineas = _lineas(ruta)
        inicio, fin = _rango_de_seccion(lineas, prefijo)
        huellas[(ruta, prefijo)] = _huellas_de_seccion(lineas[inicio + 1 : fin])
    return huellas


def _origen_de_las_huellas() -> dict[str, str]:
    """``{huella: "archivo sección línea N"}``: dice qué línea en cuarentena se copió sin repetir su texto."""
    origen: dict[str, str] = {}
    for (ruta, prefijo), huellas in _huellas_por_seccion().items():
        inicio, _ = _rango_de_seccion(_lineas(ruta), prefijo)
        for huella, indice in huellas.items():
            origen.setdefault(huella, f"{ruta} {prefijo.strip()} línea {inicio + 2 + indice}")
    return origen


def _dentro_de(rangos: Iterable[tuple[int, int]], numero: int) -> bool:
    return any(inicio <= numero < fin for inicio, fin in rangos)


def _marcas_fuera_de_rango(
    textos: Iterable[tuple[str, str]],
    marcas: Mapping[str, re.Pattern[str]],
    rangos: Mapping[str, Sequence[tuple[int, int]]],
) -> dict[str, dict[str, list[int]]]:
    """``{ruta: {marca: [n_linea, ...]}}`` de las marcas que aparecen FUERA de los rangos permitidos."""
    hallazgos: dict[str, dict[str, list[int]]] = {}
    for ruta, texto in textos:
        activas = {nombre: rx for nombre, rx in marcas.items() if rx.search(texto)}
        if not activas:
            continue
        permitidos = rangos.get(ruta, ())
        for numero, linea in enumerate(texto.splitlines()):
            if _dentro_de(permitidos, numero):
                continue
            for nombre, rx in activas.items():
                if rx.search(linea):
                    hallazgos.setdefault(ruta, {}).setdefault(nombre, []).append(numero + 1)
    return hallazgos


def _trozos_de(huella: str) -> list[str]:
    """Trozos alineados de ``LARGO_DEL_TROZO`` que cubren la huella (ella misma si es más corta)."""
    if len(huella) <= LARGO_DEL_TROZO:
        return [huella]
    posiciones = range(0, len(huella) - LARGO_DEL_TROZO + 1, LARGO_DEL_TROZO)
    return sorted({huella[i : i + LARGO_DEL_TROZO] for i in posiciones} | {huella[-LARGO_DEL_TROZO:]})


def _origen_de_los_trozos() -> dict[str, str]:
    """``{trozo: procedencia}`` de todas las huellas, para detectar copias parciales."""
    return {
        trozo: procedencia for huella, procedencia in _origen_de_las_huellas().items() for trozo in _trozos_de(huella)
    }


def _texto_fuera_de(texto: str, permitidos: Sequence[tuple[int, int]]) -> str:
    """El texto sin las líneas que caen dentro de los rangos permitidos."""
    if not permitidos:
        return texto
    lineas = texto.splitlines()
    return "\n".join(linea for numero, linea in enumerate(lineas) if not _dentro_de(permitidos, numero))


def _huellas_copiadas(
    textos: Iterable[tuple[str, str]],
    origen: Mapping[str, str],
    rangos: Mapping[str, Sequence[tuple[int, int]]],
) -> dict[str, list[str]]:
    """``{ruta: [procedencia, ...]}`` de las huellas (o trozos) que aparecen FUERA de los rangos permitidos.

    Se busca cada una como subcadena del texto normalizado COMPLETO, no línea por línea: volver a cortar
    la línea entre palabras, cambiar la indentación o las mayúsculas no la esconde, y que esté incrustada
    en un texto más largo tampoco. La salida nombra de dónde sale lo copiado, nunca repite su contenido.
    """
    copias: dict[str, list[str]] = {}
    for ruta, texto in textos:
        visible = _huella(_texto_fuera_de(texto, rangos.get(ruta, ())))
        halladas = [procedencia for huella, procedencia in origen.items() if huella in visible]
        if halladas:
            copias[ruta] = halladas
    return copias


# --------------------------------------------------------------------------- perímetros


def test_el_propio_test_existe_donde_se_lo_excluye() -> None:
    """Si se renombra este archivo, la exclusión del barrido queda huérfana y el test se delata."""
    assert Path(__file__).resolve() == (RAIZ / ESTE_ARCHIVO).resolve()


def test_el_barrido_cubre_todos_los_archivos_trackeados() -> None:
    """Enumera, no muestrea: ningún archivo trackeado se descarta por tamaño, codificación o formato."""
    esperados = {ruta for ruta in _archivos_trackeados() if ruta != ESTE_ARCHIVO and (RAIZ / ruta).exists()}
    assert {archivo.ruta for archivo in _barrido()} == esperados
    assert len(esperados) > 100, "el barrido casi no vio archivos: ¿git ls-files falló en silencio?"


def test_las_marcas_de_contenido_solo_viven_dentro_de_las_secciones_en_cuarentena() -> None:
    fuera = _marcas_fuera_de_rango(_textos(), MARCAS_DE_CONTENIDO, _rangos_en_cuarentena())
    assert fuera == {}, (
        "Contenido derivado de scripts o binarios de herramientas cerradas fuera de las secciones en "
        f"cuarentena (SECCIONES_EN_CUARENTENA): {fuera}\n"
        "No agregues la sección a la lista para que pase: ese material no entra al repo (CLEAN_ROOM.md)."
    )
    # No vacuidad: si una expresión regular se rompe, el test no puede pasar en falso.
    todas = _marcas_fuera_de_rango(_textos(), MARCAS_DE_CONTENIDO, {})
    assert todas, "ninguna marca de contenido encontrada ni dentro de la cuarentena: ¿expresiones rotas?"
    assert set(todas) <= set(_rangos_en_cuarentena()), "hay marcas en archivos que no declaran sección en cuarentena"


def test_ninguna_linea_en_cuarentena_se_copia_fuera_de_su_seccion() -> None:
    """Detecta un fragmento de script pegado SIN etiquetas: las citas y T2 no alcanzan para eso."""
    por_seccion = _huellas_por_seccion()
    for (ruta, prefijo), huellas in por_seccion.items():
        assert huellas, f"la sección {prefijo!r} de {ruta} no aporta huellas: ¿se limpió? sacala de la cuarentena"
    copias = _huellas_copiadas(_textos(), _origen_de_las_huellas(), _rangos_en_cuarentena())
    assert copias == {}, (
        f"texto de una sección en cuarentena (archivo copiado: [procedencias]) fuera de ella: {copias}\n"
        "El contenido en cuarentena no se copia ni se parafrasea (CLEAN_ROOM.md, «Perímetro de cuarentena»)."
    )


def test_ningun_tramo_de_la_cuarentena_se_copia_fuera_de_los_contratos_de_integracion() -> None:
    """Copias PARCIALES: un tramo de al menos ``2·LARGO_DEL_TROZO−1`` caracteres de una línea en cuarentena.

    Los contratos de integración (``PERIMETRO_DE_IDENTIDAD``, incluidos los propios P0) restatan por diseño
    formas de invocación, así que quedan fuera de esta comprobación; sus copias de línea completa siguen
    prohibidas por el test anterior.
    """
    candidatos = [(ruta, texto) for ruta, texto in _textos() if ruta not in PERIMETRO_DE_IDENTIDAD]
    assert candidatos, "no quedó ningún archivo por revisar: ¿el perímetro de identidad se tragó todo?"
    copias = _huellas_copiadas(candidatos, _origen_de_los_trozos(), {})
    assert copias == {}, (
        f"tramo de una sección en cuarentena (archivo copiado: [procedencias]) fuera de ella: {copias}\n"
        "Si es un contrato de integración legítimo, sumalo a PERIMETRO_DE_IDENTIDAD con su justificación; "
        "si no, reescribilo con una fuente pública o un experimento propio (CLEAN_ROOM.md)."
    )


def test_la_identidad_de_los_artefactos_solo_vive_en_contratos_de_integracion() -> None:
    con_identidad = {ruta for ruta, texto in _textos() if any(rx.search(texto) for rx in MARCAS_DE_IDENTIDAD.values())}
    assert con_identidad == PERIMETRO_DE_IDENTIDAD, (
        "Nombres de ejecutables internos o hashes de artefactos fuera de los contratos de integración.\n"
        f"  fuera de perímetro: {sorted(con_identidad - PERIMETRO_DE_IDENTIDAD)}\n"
        f"  perímetro sin marcas (¿se limpió? actualizá la constante): {sorted(PERIMETRO_DE_IDENTIDAD - con_identidad)}\n"
        "Si el archivo nuevo es un contrato de integración, sumalo con su justificación; si contiene "
        "contenido derivado de un script o binario, va a cuarentena, no a esta lista."
    )


# --------------------------------------------------------------------------- zona limpia


def test_la_zona_limpia_se_define_por_patron() -> None:
    dentro = (
        "sky_claw/local/native_parallax/solver.py",
        "docs/design/research/native-parallax/np-m0-results.md",
        "docs/design/research/2027-01-15-native-parallax-nuevo/notas.md",
        "docs/validation/native-parallax-math-revalidation-20261006/README.md",
        "tests/test_native_parallax_nuevo.py",
        "docs/adr/0013-clean-room-native-parallax.md",
    )
    fuera = (
        "sky_claw/local/tools/parallaxr_assisted.py",
        "tests/test_parallaxr_assisted.py",
        "docs/design/specs/2026-09-21-parallaxr-assisted-external-mode.md",
        "docs/audits/2026-10-07_b1_permisos_r_suite.md",
        "CLEAN_ROOM.md",
    )
    assert all(_en_zona_limpia(ruta) for ruta in dentro)
    assert not any(_en_zona_limpia(ruta) for ruta in fuera)


def test_la_zona_limpia_no_tiene_ni_una_marca() -> None:
    en_zona = [(ruta, texto) for ruta, texto in _textos() if _en_zona_limpia(ruta)]
    # No vacuidad: si se renombra un directorio de la zona, el test no puede pasar en falso.
    rutas = {ruta for ruta, _ in en_zona}
    for conocida in RUTAS_CONOCIDAS_DE_LA_ZONA:
        assert conocida in rutas, f"la zona limpia perdió una ruta conocida: {conocida}"
    marcas = {**MARCAS_DE_CONTENIDO, **MARCAS_DE_IDENTIDAD}
    sucias = _marcas_fuera_de_rango(en_zona, marcas, {})
    assert sucias == {}, f"la zona limpia no puede contener marcas de herramientas cerradas: {sucias}"


def test_los_binarios_de_la_zona_limpia_estan_congelados() -> None:
    """Un binario no se puede revisar como texto: en la zona entra solo si se lo admite a sabiendas."""
    binarios = {archivo.ruta for archivo in _barrido() if _en_zona_limpia(archivo.ruta) and archivo.es_binario}
    assert binarios == BINARIOS_ADMITIDOS_EN_LA_ZONA, (
        "Binarios en la zona limpia que no están admitidos (o admitidos que ya no existen).\n"
        f"  sin admitir: {sorted(binarios - BINARIOS_ADMITIDOS_EN_LA_ZONA)}\n"
        f"  admitidos ausentes: {sorted(BINARIOS_ADMITIDOS_EN_LA_ZONA - binarios)}\n"
        "Sumarlo a BINARIOS_ADMITIDOS_EN_LA_ZONA implica confirmar que NO es salida de una herramienta cerrada."
    )


def test_no_hay_ejecutables_trackeados() -> None:
    por_extension = {ruta for ruta in _archivos_trackeados() if Path(ruta).suffix.lower() in EXTENSIONES_DE_EJECUTABLE}
    por_cabecera = {archivo.ruta for archivo in _barrido() if archivo.es_ejecutable}
    assert por_extension | por_cabecera == EJECUTABLES_ADMITIDOS, (
        "La política prohíbe aceptar EXE/DLL de herramientas cerradas y el repo no versiona ejecutables.\n"
        f"  por extensión: {sorted(por_extension)}\n  por cabecera (PE/ELF/Mach-O): {sorted(por_cabecera)}"
    )


# --------------------------------------------------------------------------- casos sintéticos de los helpers


def test_es_ejecutable_reconoce_la_cabecera_y_no_la_extension() -> None:
    pe = b"MZ" + b"\0" * 58 + b"\x80\0\0\0" + b"\0" * 60 + b"PE\0\0" + b"\0" * 32
    assert _es_ejecutable(pe)
    assert _es_ejecutable(b"\x7fELF\x02\x01\x01" + b"\0" * 32)
    assert _es_ejecutable(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    assert not _es_ejecutable(b"MZ es un texto que empieza con esas letras")
    assert not _es_ejecutable(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    assert not _es_ejecutable(b"")


def test_la_vista_de_barrido_no_descarta_ningun_formato() -> None:
    cita = MARCAS_DE_CONTENIDO["cita_de_script"]
    helper = MARCAS_DE_IDENTIDAD["nombre_de_helper"]
    casos = {
        "utf8": b"PxR 123",
        "utf8_con_bom": "PxR 123".encode("utf-8-sig"),
        "utf16_con_bom": "PxR 123".encode("utf-16"),
        "utf16_be_con_bom": b"\xfe\xff" + "PxR 123".encode("utf-16-be"),
        "utf16_le_sin_bom": "PxR 123".encode("utf-16-le"),
        "utf16_be_sin_bom": "PxR 123".encode("utf-16-be"),
        "utf32_con_bom": "PxR 123".encode("utf-32"),
        "binario_con_nul": b"\x00\x01PxR 123\x00\x02",
        "cp1252_con_acentos": "acción PxR 123".encode("cp1252"),
        "mas_de_dos_mib": b"x" * (2 * 1024 * 1024 + 1) + b"\nPxR 123\n",
    }
    for nombre, datos in casos.items():
        assert cita.search(_vista_de_barrido(datos)), f"no se ve la marca en: {nombre}"
    assert helper.search(_vista_de_barrido("acción: ExtractBSA.exe".encode("cp1252")))
    assert helper.search(_vista_de_barrido("ExtractBSA.exe".encode("utf-16"))) is not None
    assert _vista_de_barrido(b"") == ""


def test_el_rango_de_seccion_ignora_los_encabezados_dentro_de_bloques_cercados() -> None:
    lineas = [
        "## Uno",  # 0
        "### 1.1 Interna",  # 1
        "cuerpo",  # 2
        "```bash",  # 3
        "# un comentario de script NO es un encabezado",  # 4
        "## ni esto",  # 5
        "```",  # 6
        "cola de la sección",  # 7
        "### 1.2 Hermana",  # 8
        "texto",  # 9
        "## Dos",  # 10
        "~~~",  # 11
        "### 2.1 Dentro de un cerco, no cuenta",  # 12
        "~~~",  # 13
        "### 2.2 Última",  # 14
        "fin",  # 15
    ]
    assert _rango_de_seccion(lineas, "### 1.1 ") == (1, 8)
    assert _rango_de_seccion(lineas, "## Uno") == (0, 10)
    assert _rango_de_seccion(lineas, "### 2.2 ") == (14, len(lineas))
    with pytest.raises(AssertionError, match="no encuentro el encabezado"):
        _rango_de_seccion(lineas, "### 2.1 ")  # solo existe dentro de un bloque cercado


def test_el_rango_de_seccion_respeta_el_largo_y_el_tipo_del_cerco() -> None:
    """Reglas de CommonMark que importan acá: un cerco cierra solo si es del mismo tipo, igual o más largo y sin texto."""
    lineas = [
        "### 1 Sección",  # 0
        "````markdown",  # 1: abre con cuatro comillas
        "```bash",  # 2: un cerco más corto NO cierra el bloque
        "# comentario dentro del bloque anidado",  # 3
        "```",  # 4: tampoco cierra
        "## encabezado falso dentro del bloque",  # 5
        "````",  # 6: cierra (mismo tipo y largo)
        "texto después del bloque",  # 7
        "~~~",  # 8: abre un cerco de tildes
        "```",  # 9: otro tipo de cerco: no cierra
        "# comentario",  # 10
        "~~~ con texto",  # 11: un cierre no lleva texto: no cierra
        "## sigue dentro del bloque",  # 12
        "~~~~",  # 13: cierra (mismo tipo, más largo)
        "```con ``` comillas en el texto",  # 14: comillas en el texto de info: no es un cerco
        "## Siguiente",  # 15
    ]
    assert _rango_de_seccion(lineas, "### 1 ") == (0, 15)


def test_las_huellas_omiten_avisos_separadores_y_lineas_cortas() -> None:
    lineas = [
        f"> **{BANNER}** — texto del aviso que es largo y no debe ser huella",
        "> segunda línea del mismo aviso, también larga y tampoco huella",
        "| a | b |",
        "|---|---|",
        "linea corta",
        "   Una   LÍNEA   de cuerpo bastante larga con espacios raros   ",
        "> una cita real del cuerpo que es larga y sí cuenta como huella",
    ]
    # El índice es el de la primera aparición dentro de la lista recibida (aviso y líneas cortas cuentan).
    assert _huellas_de_seccion(lineas) == {
        "una línea de cuerpo bastante larga con espacios raros": 5,
        "> una cita real del cuerpo que es larga y sí cuenta como huella": 6,
    }


def test_las_copias_se_detectan_aunque_cambie_el_formato_y_se_permiten_dentro_del_rango() -> None:
    linea = "una línea de cuerpo bastante larga con espacios raros"
    origen = {linea: "origen.md línea 2"}
    textos = [
        ("origen.md", "intro\nUNA línea de cuerpo bastante larga con espacios raros\nfin"),  # dentro del rango
        ("otro.md", "hola\n\tuna  LÍNEA de cuerpo bastante larga con espacios raros  \n"),  # copia con otra forma
        ("recortada.md", "una línea de cuerpo bastante\nlarga con espacios raros\n"),  # la línea cortada en otro lugar
        (
            "incrustada.md",
            "como dice, una línea de cuerpo bastante larga con espacios raros, y sigue\n",
        ),  # dentro de otra
        ("con_bom.md", chr(0xFEFF) + "una línea de cuerpo bastante larga con espacios raros\r\n"),  # BOM y CRLF
        ("limpio.md", "una línea de cuerpo\nbastante distinta\n"),
    ]
    copiadas = {"otro.md", "recortada.md", "incrustada.md", "con_bom.md"}
    con_rango = _huellas_copiadas(textos, origen, {"origen.md": ((1, 2),)})
    assert set(con_rango) == copiadas
    assert all(procedencias == ["origen.md línea 2"] for procedencias in con_rango.values())
    assert set(_huellas_copiadas(textos, origen, {})) == copiadas | {"origen.md"}


def test_los_trozos_cubren_toda_copia_de_al_menos_el_doble_menos_uno() -> None:
    """Enumera, no muestrea: TODA subcadena de ``2·k−1`` caracteres o más contiene algún trozo completo."""
    huella = " ".join(f"palabra{n:02d}" for n in range(18))  # 17·9 + 9 = 161 caracteres, sin repeticiones
    trozos = _trozos_de(huella)
    minimo = 2 * LARGO_DEL_TROZO - 1
    assert len(huella) > 2 * minimo
    for inicio in range(len(huella) - minimo + 1):
        for fin in range(inicio + minimo, len(huella) + 1):
            tramo = huella[inicio:fin]
            assert any(trozo in tramo for trozo in trozos), f"se escapa el tramo [{inicio}:{fin}]"
    assert _trozos_de("corta pero con más de treinta caracteres") == ["corta pero con más de treinta caracteres"]


def test_una_copia_parcial_se_detecta_por_trozos_y_no_por_linea_completa() -> None:
    huella = " ".join(f"palabra{n:02d}" for n in range(18))
    parcial = huella[13:113]  # 100 caracteres, desde una posición no alineada con los trozos
    textos = [("nuevo.md", f"intro\n{parcial}\nfin\n"), ("limpio.md", "nada que ver\n")]
    por_linea = _huellas_copiadas(textos, {huella: "origen línea 1"}, {})
    por_trozos = _huellas_copiadas(textos, {trozo: "origen línea 1" for trozo in _trozos_de(huella)}, {})
    assert por_linea == {}
    assert por_trozos == {"nuevo.md": ["origen línea 1"]}


def test_las_marcas_se_permiten_solo_dentro_del_rango() -> None:
    textos = [("p0.md", "a\nPxR 12 dentro\nb\nPxR 99 fuera\n"), ("x.md", "BENDr 7\n")]
    marcas = {"cita": MARCAS_DE_CONTENIDO["cita_de_script"]}
    assert _marcas_fuera_de_rango(textos, marcas, {"p0.md": ((1, 3),)}) == {
        "p0.md": {"cita": [4]},
        "x.md": {"cita": [1]},
    }


def test_sin_git_el_ancla_falla_en_ci_y_se_saltea_fuera() -> None:
    assert _accion_sin_git({"GITHUB_ACTIONS": "true"}) == "fallar"
    assert _accion_sin_git({"GITHUB_ACTIONS": "false"}) == "saltar"
    assert _accion_sin_git({}) == "saltar"


# --------------------------------------------------------------------------- avisos


def test_las_secciones_en_cuarentena_estan_senaladas() -> None:
    por_archivo: dict[str, list[str]] = {}
    for ruta, prefijo in SECCIONES_EN_CUARENTENA:
        por_archivo.setdefault(ruta, []).append(prefijo)
    for ruta, prefijos in por_archivo.items():
        texto = _texto_requerido(ruta)
        lineas = texto.splitlines()
        esperados = len(prefijos) + (1 if ruta in ARCHIVOS_CON_AVISO_GLOBAL else 0)
        assert texto.count(BANNER) == esperados, f"{ruta}: el aviso debe estar en cada sección en cuarentena y no más"
        if ruta in ARCHIVOS_CON_AVISO_GLOBAL:
            assert BANNER in "\n".join(lineas[:20]), f"{ruta}: el aviso global debe estar en las primeras líneas"
        for prefijo in prefijos:
            inicio, _ = _rango_de_seccion(lineas, prefijo)
            assert BANNER in "\n".join(lineas[inicio + 1 : inicio + 4]), f"{ruta}: falta el aviso bajo {prefijo!r}"


# --------------------------------------------------------------------------- política vigente


def test_la_politica_vigente_esta_en_la_raiz() -> None:
    politica = _texto_requerido("CLEAN_ROOM.md")
    assert "**Estado:** VIGENTE" in politica
    encabezados = [linea for linea in politica.splitlines() if linea.startswith("## ")]
    assert encabezados == ENCABEZADOS_DE_LA_POLITICA


def test_la_politica_conserva_cada_una_de_sus_clausulas() -> None:
    """No alcanza con los encabezados: borrar el contenido de una sección dejaría la política vacía."""
    politica = _texto_requerido("CLEAN_ROOM.md")
    faltantes: dict[str, list[str]] = {}
    for encabezado, clausulas in CLAUSULAS_DE_LA_POLITICA.items():
        assert clausulas, f"la sección {encabezado!r} no tiene cláusulas congeladas"
        cuerpo = " ".join(_cuerpo_de_seccion(politica, encabezado).split())
        ausentes = [clausula for clausula in clausulas if " ".join(clausula.split()) not in cuerpo]
        if ausentes:
            faltantes[encabezado] = ausentes
    assert faltantes == {}, f"cláusulas de CLEAN_ROOM.md que ya no están en su sección: {faltantes}"


def test_la_politica_distingue_evaluacion_ciega_de_destilacion() -> None:
    """El issue #676 prevé comparar contra ParallaxR: eso es evaluación; entrenar con su salida no."""
    politica = _texto_requerido("CLEAN_ROOM.md")
    prohibido = _cuerpo_de_seccion(politica, ENCABEZADOS_DE_LA_POLITICA[1])
    permitido = _cuerpo_de_seccion(politica, ENCABEZADOS_DE_LA_POLITICA[2])
    assert "etiqueta de entrenamiento" in prohibido
    assert "evaluación" in permitido
    assert "#676" in permitido


def test_la_politica_declara_su_alcance_por_patron_y_su_perimetro() -> None:
    politica = _texto_requerido("CLEAN_ROOM.md")
    cabecera = " ".join(" ".join(politica.splitlines()[:10]).split())
    assert "`native-parallax`" in cabecera
    assert "`native_parallax`" in cabecera
    perimetro = _cuerpo_de_seccion(politica, "## Perímetro de cuarentena")
    for ruta, prefijo in SECCIONES_EN_CUARENTENA:
        assert ruta in perimetro, f"la tabla del perímetro no nombra {ruta}"
        assert f"§{prefijo.split()[1]}" in perimetro, f"la tabla del perímetro no nombra la sección {prefijo!r}"


def test_la_ruta_vieja_de_la_propuesta_es_un_puntero_y_no_una_segunda_copia() -> None:
    viejo = _texto_requerido(STUB_VIEJO)
    assert "../../../../CLEAN_ROOM.md" in viejo
    assert len(viejo.splitlines()) < 15, "la ruta vieja debe ser un puntero, no una copia que derive"


def test_agents_md_referencia_la_politica_y_su_ancla() -> None:
    agents = _texto_requerido("AGENTS.md")
    assert "[CLEAN_ROOM.md](CLEAN_ROOM.md)" in agents
    assert ESTE_ARCHIVO in agents
    assert ADR in agents


def test_el_puntero_local_remite_a_la_politica() -> None:
    puntero = _texto_requerido(PUNTERO_LOCAL)
    assert "CLEAN_ROOM.md" in puntero
    assert "no leas" in puntero.lower()


def test_el_adr_existe_y_esta_indexado() -> None:
    assert _leer(ADR) is not None, f"falta {ADR}"
    assert f"({Path(ADR).name})" in _texto_requerido("docs/adr/README.md")


# --------------------------------------------------------------------------- B1


def _registro_b1() -> dict[str, Any]:
    return json.loads(_texto_requerido(REGISTRO_B1_JSON))


def _hash_canonico(registro: dict[str, Any]) -> str:
    """sha256 del JSON canónico del registro SIN su propio campo ``sha256``."""
    carga = {clave: valor for clave, valor in registro.items() if clave != "sha256"}
    canonico = json.dumps(carga, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _bloqueos_del_contrato() -> dict[str, bool]:
    """``{paso: ¿lleva B6_L?}`` de TODOS los pasos de ``MATERIAL_PIPELINE`` (sin tabla escrita a mano)."""
    from sky_claw.local.tools.material_contract import MATERIAL_PIPELINE, MaterialBlocker

    return {paso.value: MaterialBlocker.B6_L in spec.blockers for paso, spec in MATERIAL_PIPELINE.items()}


def _incoherencias_b1(estados: Mapping[str, str], bloqueos: Mapping[str, bool]) -> list[str]:
    """Incoherencias entre el registro B1 y el contrato (vacía = coherente). Falla cerrado.

    ``B6_L`` debe estar puesto salvo que el registro diga ``AUTORIZADA``; un estado fuera del vocabulario
    (un typo, una negativa que se tomara por permiso) nunca levanta el bloqueo.
    """
    problemas: list[str] = []
    for herramienta, estado in estados.items():
        if estado not in ESTADOS_DE_PERMISO:
            problemas.append(f"{herramienta}: estado {estado!r} fuera del vocabulario {sorted(ESTADOS_DE_PERMISO)}")
        elif herramienta not in bloqueos:
            problemas.append(f"{herramienta}: el registro la lista pero no es un paso de MATERIAL_PIPELINE")
        elif bloqueos[herramienta] != (estado != ESTADO_QUE_LEVANTA_EL_BLOQUEO):
            problemas.append(
                f"{herramienta}: el registro dice {estado} pero el contrato tiene B6_L={bloqueos[herramienta]}"
            )
    problemas.extend(
        f"{paso}: el contrato la bloquea con B6_L y el registro B1 no la cubre"
        for paso, bloqueado in bloqueos.items()
        if bloqueado and paso not in estados
    )
    return problemas


def test_el_registro_b1_conserva_su_hash() -> None:
    registro = _registro_b1()
    assert registro["sha256"] == _hash_canonico(registro), "el registro B1 se editó sin recalcular su hash"


def test_el_registro_b1_declara_su_nivel_de_evidencia() -> None:
    fuente = _registro_b1()["fuente_primaria"]
    assert fuente["nivel_de_evidencia"] == "SUPPORTED_BY_REPO_EVIDENCE"
    assert fuente["consultado_en_origen"] == "2026-09-19"
    assert fuente["reverificacion_en_vivo"].startswith("NO_REALIZADA")
    md = _texto_requerido(REGISTRO_B1_MD)
    assert "B6-L" in md
    assert "ABIERTO" in md
    assert _registro_b1()["sha256"] in md, "el hash que muestra el registro B1 no coincide con el de sus datos"
    for estado in sorted(ESTADOS_DE_PERMISO):
        assert f"`{estado}`" in md, f"el registro B1 no documenta el estado {estado}"


def test_el_registro_b1_coincide_con_el_contrato_de_materiales() -> None:
    """Evidencia legal y contrato son hermanos: levantar uno sin el otro rompe el test."""
    registro = _registro_b1()
    estados = {
        herramienta: datos["invocacion_directa_de_helpers"] for herramienta, datos in registro["herramientas"].items()
    }
    assert estados, "el registro B1 no lista ninguna herramienta"
    assert _incoherencias_b1(estados, _bloqueos_del_contrato()) == []
    assert registro["politica_vigente"] == POLITICA_B1_VIGENTE


CASOS_DE_COHERENCIA_B1 = {
    "abierta_y_bloqueada": ({"x": "ABIERTA"}, {"x": True}, []),
    "denegada_y_bloqueada": ({"x": "DENEGADA"}, {"x": True}, []),
    "autorizada_y_levantada": ({"x": "AUTORIZADA"}, {"x": False}, []),
    "paso_sin_b6l_y_sin_registro": ({"x": "ABIERTA"}, {"x": True, "libre": False}, []),
    "denegada_con_el_bloqueo_levantado": ({"x": "DENEGADA"}, {"x": False}, ["x"]),
    "typo_con_el_bloqueo_levantado": ({"x": "ABIERTO "}, {"x": False}, ["x"]),
    "typo_con_el_bloqueo_puesto": ({"x": "ABIERTO "}, {"x": True}, ["x"]),
    "autorizada_pero_el_contrato_sigue_bloqueando": ({"x": "AUTORIZADA"}, {"x": True}, ["x"]),
    "abierta_pero_el_contrato_no_bloquea": ({"x": "ABIERTA"}, {"x": False}, ["x"]),
    "cuarto_paso_con_b6l_sin_registro": ({"x": "ABIERTA"}, {"x": True, "nuevo": True}, ["nuevo"]),
    "registro_de_algo_que_no_es_paso": ({"x": "ABIERTA", "fantasma": "ABIERTA"}, {"x": True}, ["fantasma"]),
}


@pytest.mark.parametrize("estados, bloqueos, culpables", CASOS_DE_COHERENCIA_B1.values(), ids=CASOS_DE_COHERENCIA_B1)
def test_la_coherencia_b1_enumerada_por_caso(
    estados: dict[str, str], bloqueos: dict[str, bool], culpables: list[str]
) -> None:
    problemas = _incoherencias_b1(estados, bloqueos)
    assert [problema.split(":")[0] for problema in problemas] == culpables, problemas
