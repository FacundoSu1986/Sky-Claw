"""Ancla de la política clean-room del generador nativo de parallax (ADR 0013).

Por qué existe: ``CLEAN_ROOM.md`` nació como propuesta (NP-R0) y nada la hacía cumplir, mientras
el plan P0 ya contenía citas de scripts y strings de binarios de herramientas cerradas. «Ninguna
decisión de diseño se apoya en ParallaxR» era una frase del README, no una propiedad verificada
(``AGENTS.md``: escribir el racional no cuenta como verificarlo).

Enumera en vez de muestrear (ver «La regla que más se viola» en ``AGENTS.md``): recorre **todos**
los archivos trackeados por git y busca marcas por *contenido*, así que un archivo nuevo con
material de una herramienta cerrada rompe el test aunque nadie lo haya listado antes.

Dos familias de marcas, de la más grave a la más benigna:

- **contenido** (derivado de leer scripts o de inspeccionar binarios): citas de línea de scripts
  y etiquetas de evidencia T2. Solo puede vivir en el P0 v3, dentro de las secciones señalizadas.
- **identidad** (nombres de ejecutables internos y hashes de artefactos): aparecen en contratos
  de integración y en la evidencia; nunca en la zona limpia.

La **zona limpia** (``native_parallax/`` y sus docs de diseño) no puede tener ninguna marca.

Deliberadamente NO se barre la palabra «ParallaxR»: el nombre del producto es un contrato de
integración legítimo (entrypoint, markers, step id) y decirlo en un docstring clean-room no es
contaminación. El ancla protege el *contenido derivado*, no cada mención léxica.

Este archivo se excluye a sí mismo del barrido (sus propias expresiones regulares contienen las
marcas); ``test_el_propio_test_existe_donde_se_lo_excluye`` impide que esa exclusión quede huérfana.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
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
LIMITE_DE_BYTES = 2 * 1024 * 1024

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

# El contenido derivado vive SOLO acá (secciones señalizadas; ver test de banners).
PERIMETRO_DE_CONTENIDO = frozenset({P0_V3})
# La identidad se admite en contratos de integración y evidencia. Un archivo nuevo entra a esta lista
# solo si es un contrato de integración; si contiene contenido derivado, va a cuarentena, no acá.
PERIMETRO_DE_IDENTIDAD = frozenset({P0_V3, P0_EVIDENCIA, SPEC_ASISTIDO, TEST_ASISTIDO})
ZONA_LIMPIA = (
    "sky_claw/local/native_parallax/",
    "docs/design/research/native-parallax/",
    "docs/design/research/2026-09-21-native-parallax-battle/",
)

ENCABEZADOS_DE_LA_POLITICA = [
    "## Principio",
    "## PROHIBIDO aceptar en el repo (código, issues, PRs, docs, conversaciones)",
    "## SÍ se permite estudiar y usar",
    "## Regla de decisión",
    "## Trazabilidad",
    "## Perímetro de cuarentena",
    "## Exposición previa (reconocida)",
    "## Si se detecta contaminación",
    "## Permisos de las herramientas cerradas (B1)",
    "## Anclas",
]
POLITICA_B1_VIGENTE = ["MANUAL_ONLY", "NO_VENDOR", "NO_BUNDLE", "NO_REDISTRIBUTE"]


@cache
def _archivos_trackeados() -> tuple[str, ...]:
    if shutil.which("git") is None or not (RAIZ / ".git").exists():
        pytest.skip("distribución sin .git: el ancla enumera con `git ls-files`")
    salida = subprocess.run(["git", "ls-files", "-z"], cwd=RAIZ, check=True, capture_output=True).stdout
    return tuple(ruta for ruta in salida.decode("utf-8").split("\0") if ruta)


def _leer(ruta: str) -> str | None:
    """Texto UTF-8 del archivo, o ``None`` si es binario, demasiado grande o ilegible."""
    destino = RAIZ / ruta
    try:
        if destino.stat().st_size > LIMITE_DE_BYTES:
            return None
        datos = destino.read_bytes()
        if b"\0" in datos[:8192]:
            return None
        return datos.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@cache
def _escanear() -> dict[str, dict[str, int]]:
    """``{ruta: {marca: cantidad}}`` de TODOS los archivos trackeados (salvo este test)."""
    marcas = {**MARCAS_DE_CONTENIDO, **MARCAS_DE_IDENTIDAD}
    hallazgos: dict[str, dict[str, int]] = {}
    for ruta in _archivos_trackeados():
        if ruta == ESTE_ARCHIVO:
            continue
        texto = _leer(ruta)
        if texto is None:
            continue
        presentes = {nombre: n for nombre, rx in marcas.items() if (n := len(rx.findall(texto)))}
        if presentes:
            hallazgos[ruta] = presentes
    return hallazgos


def _seccion(texto: str, encabezado: str) -> str:
    """Cuerpo de la sección ``encabezado`` (hasta el próximo ``## ``)."""
    lineas = texto.splitlines()
    inicio = next((i for i, linea in enumerate(lineas) if linea.strip() == encabezado), None)
    assert inicio is not None, f"falta la sección {encabezado!r}"
    fin = next((i for i in range(inicio + 1, len(lineas)) if lineas[i].startswith("## ")), len(lineas))
    return "\n".join(lineas[inicio + 1 : fin])


def _lineas_bajo(texto: str, prefijo: str, cuantas: int = 3) -> str:
    """Las ``cuantas`` líneas que siguen al encabezado que empieza con ``prefijo``."""
    lineas = texto.splitlines()
    indice = next((i for i, linea in enumerate(lineas) if linea.startswith(prefijo)), None)
    assert indice is not None, f"no encuentro el encabezado que empieza con {prefijo!r}"
    return "\n".join(lineas[indice + 1 : indice + 1 + cuantas])


def _texto_requerido(ruta: str) -> str:
    texto = _leer(ruta)
    assert texto is not None, f"falta o es ilegible: {ruta}"
    return texto


# --------------------------------------------------------------------------- perímetros


def test_el_propio_test_existe_donde_se_lo_excluye() -> None:
    """Si se renombra este archivo, la exclusión del barrido queda huérfana y el test se delata."""
    assert Path(__file__).resolve() == (RAIZ / ESTE_ARCHIVO).resolve()


def test_el_perimetro_de_contenido_no_crece() -> None:
    con_contenido = {ruta for ruta, marcas in _escanear().items() if set(marcas) & set(MARCAS_DE_CONTENIDO)}
    assert con_contenido == PERIMETRO_DE_CONTENIDO, (
        "Contenido derivado de scripts o binarios de herramientas cerradas fuera del perímetro de cuarentena.\n"
        f"  fuera de perímetro: {sorted(con_contenido - PERIMETRO_DE_CONTENIDO)}\n"
        f"  perímetro sin marcas (¿se limpió? actualizá la constante): {sorted(PERIMETRO_DE_CONTENIDO - con_contenido)}\n"
        "No lo agregues a la lista: ese material no entra al repo (CLEAN_ROOM.md)."
    )


def test_el_perimetro_de_identidad_no_crece() -> None:
    con_identidad = {ruta for ruta, marcas in _escanear().items() if set(marcas) & set(MARCAS_DE_IDENTIDAD)}
    assert con_identidad == PERIMETRO_DE_IDENTIDAD, (
        "Nombres de ejecutables internos o hashes de artefactos fuera de los contratos de integración.\n"
        f"  fuera de perímetro: {sorted(con_identidad - PERIMETRO_DE_IDENTIDAD)}\n"
        f"  perímetro sin marcas (¿se limpió? actualizá la constante): {sorted(PERIMETRO_DE_IDENTIDAD - con_identidad)}\n"
        "Si el archivo nuevo es un contrato de integración, sumalo con su justificación; si contiene "
        "contenido derivado de un script o binario, va a cuarentena, no a esta lista."
    )


def test_la_zona_limpia_no_tiene_ni_una_marca() -> None:
    sucias = {ruta: marcas for ruta, marcas in _escanear().items() if ruta.startswith(ZONA_LIMPIA)}
    assert sucias == {}, f"la zona limpia no puede contener marcas de herramientas cerradas: {sucias}"
    # No vacuidad: si se renombra un directorio de la zona, el test no puede pasar en falso.
    for prefijo in ZONA_LIMPIA:
        assert any(ruta.startswith(prefijo) for ruta in _archivos_trackeados()), f"zona limpia sin archivos: {prefijo}"


def test_las_secciones_en_cuarentena_estan_senalizadas() -> None:
    p0 = _texto_requerido(P0_V3)
    assert p0.count(BANNER) == 3, "el P0 v3 debe llevar el aviso arriba y en sus secciones §2.2 y §2.4"
    assert BANNER in "\n".join(p0.splitlines()[:20]), "el aviso global debe estar en las primeras líneas del P0 v3"
    for prefijo in ("### 2.2 ", "### 2.4 "):
        assert BANNER in _lineas_bajo(p0, prefijo), f"falta el aviso bajo el encabezado {prefijo!r}"

    evidencia = _texto_requerido(P0_EVIDENCIA)
    assert evidencia.count(BANNER) == 1, "la evidencia P0 debe llevar el aviso solo en su §6.2"
    assert BANNER in _lineas_bajo(evidencia, "### 6.2 ")


# --------------------------------------------------------------------------- política vigente


def test_la_politica_vigente_esta_en_la_raiz() -> None:
    politica = _texto_requerido("CLEAN_ROOM.md")
    assert "**Estado:** VIGENTE" in politica
    encabezados = [linea for linea in politica.splitlines() if linea.startswith("## ")]
    assert encabezados == ENCABEZADOS_DE_LA_POLITICA


def test_la_politica_distingue_evaluacion_ciega_de_destilacion() -> None:
    """El issue #676 prevé comparar contra ParallaxR: eso es evaluación; entrenar con su salida no."""
    politica = _texto_requerido("CLEAN_ROOM.md")
    prohibido = _seccion(politica, ENCABEZADOS_DE_LA_POLITICA[1])
    permitido = _seccion(politica, ENCABEZADOS_DE_LA_POLITICA[2])
    assert "etiqueta de entrenamiento" in prohibido
    assert "evaluación" in permitido
    assert "#676" in permitido


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


def test_el_registro_b1_coincide_con_el_contrato_de_materiales() -> None:
    """Evidencia legal y contrato son hermanos: levantar uno sin el otro rompe el test."""
    from sky_claw.local.tools.material_contract import MATERIAL_PIPELINE, MaterialBlocker, MaterialStepId

    registro = _registro_b1()
    paso_de = {
        "parallaxr": MaterialStepId.PARALLAXR,
        "bendr": MaterialStepId.BENDR,
        "vramr": MaterialStepId.VRAMR,
    }
    assert set(registro["herramientas"]) == set(paso_de)
    for herramienta, datos in registro["herramientas"].items():
        abierta = datos["invocacion_directa_de_helpers"] == "ABIERTA"
        bloqueada = MaterialBlocker.B6_L in MATERIAL_PIPELINE[paso_de[herramienta]].blockers
        assert abierta == bloqueada, (
            f"{herramienta}: el registro B1 dice abierta={abierta} pero el contrato tiene B6_L={bloqueada}"
        )
    assert registro["politica_vigente"] == POLITICA_B1_VIGENTE
