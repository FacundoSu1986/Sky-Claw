"""Anclas del reparto de la suite en shards (``scripts/ci_particion.py``).

El riesgo de repartir una suite no es que un shard falle: es que un test **deje de correr sin que nadie lo note**.
Por eso las propiedades se prueban enumeradas (varios N, cada modo de falla del verificador) y el plugin se ejerce
de verdad en un subproceso sobre un árbol mínimo, no solo la función pura.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from scripts.ci_particion import Shard, huella_de_suite, parsear_shard, repartir, verificar

from scripts import ci_particion

RAIZ = Path(__file__).resolve().parents[1]

#: Cantidades de shards sobre las que se enumeran las propiedades (1 es el caso degenerado: sin reparto).
TOTALES = range(1, 10)

PESOS_SINTETICOS: dict[str, dict[str, int]] = {
    "uniforme": {f"tests/test_{i:02d}.py": 5 for i in range(23)},
    "sesgado": {"tests/test_pesado.py": 400, **{f"tests/test_{i:02d}.py": 3 for i in range(31)}},
    "con_empates": {f"tests/test_{i:02d}.py": (i % 4) + 1 for i in range(17)},
    "menos_archivos_que_shards": {"tests/test_a.py": 7, "tests/test_b.py": 2},
    "un_solo_archivo": {"tests/test_unico.py": 11},
}


def _pesos_reales() -> dict[str, int]:
    """Pesos heterogéneos reales: el tamaño en bytes de cada archivo de test del repo."""
    archivos = sorted(RAIZ.glob("tests/**/test_*.py"))
    assert len(archivos) > 100, "el árbol de tests real debería tener cientos de archivos"
    return {a.relative_to(RAIZ).as_posix(): a.stat().st_size for a in archivos}


# ---------------------------------------------------------------------------
# parsear_shard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("1/1", Shard(1, 1)), ("2/4", Shard(2, 4)), ("4/4", Shard(4, 4)), ("10/12", Shard(10, 12))],
)
def test_parsear_shard_acepta_la_forma_indice_barra_total(texto: str, esperado: Shard) -> None:
    assert parsear_shard(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["", "0/4", "5/4", "1/0", "a/b", "1/4/2", "1 / 4", " 1/4", "1/4 ", "-1/4", "1.5/4", "1", "/4", "1/"],
)
def test_parsear_shard_rechaza_todo_lo_que_no_sea_una_forma_valida(texto: str) -> None:
    with pytest.raises(ValueError, match="SKYCLAW_SHARD"):
        parsear_shard(texto)


# ---------------------------------------------------------------------------
# repartir: propiedades enumeradas
# ---------------------------------------------------------------------------


def _cargas(pesos: Mapping[str, int], grupos: tuple[frozenset[str], ...]) -> list[int]:
    return [sum(pesos[n] for n in g) for g in grupos]


@pytest.mark.parametrize("total", TOTALES)
@pytest.mark.parametrize("nombre", sorted(PESOS_SINTETICOS))
def test_repartir_es_una_particion_exacta_en_los_casos_sinteticos(nombre: str, total: int) -> None:
    pesos = PESOS_SINTETICOS[nombre]
    grupos = repartir(pesos, total)

    assert len(grupos) == total
    union: set[str] = set()
    for grupo in grupos:
        assert not (union & grupo), "un archivo no puede estar en dos shards"
        union |= grupo
    assert union == set(pesos), "ningún archivo puede quedar sin shard"


@pytest.mark.parametrize("total", TOTALES)
def test_repartir_es_una_particion_exacta_sobre_el_arbol_de_tests_real(total: int) -> None:
    pesos = _pesos_reales()
    grupos = repartir(pesos, total)

    todos = [n for g in grupos for n in sorted(g)]
    assert sorted(todos) == sorted(pesos), "la unión debe ser exactamente el árbol real, sin repetidos ni faltantes"
    assert len(todos) == len(set(todos))


@pytest.mark.parametrize("total", TOTALES)
@pytest.mark.parametrize("nombre", sorted(PESOS_SINTETICOS))
def test_repartir_no_se_aleja_del_promedio_mas_que_el_archivo_mas_pesado(nombre: str, total: int) -> None:
    """Cota del reparto voraz: la carga máxima no supera el promedio más el peso del archivo más grande."""
    pesos = PESOS_SINTETICOS[nombre]
    cargas = _cargas(pesos, repartir(pesos, total))

    assert max(cargas) <= sum(pesos.values()) / total + max(pesos.values())


@pytest.mark.parametrize("total", TOTALES)
def test_repartir_cumple_la_cota_de_balance_sobre_el_arbol_real(total: int) -> None:
    pesos = _pesos_reales()
    cargas = _cargas(pesos, repartir(pesos, total))

    assert max(cargas) <= sum(pesos.values()) / total + max(pesos.values())


@pytest.mark.parametrize("total", TOTALES)
def test_repartir_no_depende_del_orden_en_que_se_enumeran_los_archivos(total: int) -> None:
    """Dos shards que colectan en distinto orden tienen que llegar a la misma asignación."""
    pesos = _pesos_reales()
    esperado = repartir(pesos, total)

    for semilla in range(5):
        items = list(pesos.items())
        random.Random(semilla).shuffle(items)
        assert repartir(dict(items), total) == esperado


def test_repartir_con_un_solo_shard_los_deja_todos_juntos() -> None:
    pesos = PESOS_SINTETICOS["con_empates"]
    assert repartir(pesos, 1) == (frozenset(pesos),)


def test_repartir_literal_con_empates_pone_primero_lo_pesado_y_desempata_por_nombre_e_indice() -> None:
    pesos = {"d": 4, "a": 3, "c": 2, "b": 1, "e": 1}

    assert repartir(pesos, 2) == (frozenset({"d", "b", "e"}), frozenset({"a", "c"}))


@pytest.mark.parametrize("total", [0, -1])
def test_repartir_rechaza_una_cantidad_de_shards_no_positiva(total: int) -> None:
    with pytest.raises(ValueError, match="total"):
        repartir({"a": 1}, total)


@pytest.mark.parametrize("peso", [0, -3])
def test_repartir_rechaza_pesos_no_positivos(peso: int) -> None:
    with pytest.raises(ValueError, match="peso"):
        repartir({"a": peso}, 2)


# ---------------------------------------------------------------------------
# verificar: cada modo de falla, enumerado
# ---------------------------------------------------------------------------

PESOS_DEL_VERIFICADOR = {"tests/test_a.py": 4, "tests/test_b.py": 3, "tests/test_c.py": 2, "tests/test_d.py": 1}
TOTAL_DEL_VERIFICADOR = 2


def _manifiestos_validos() -> list[dict[str, Any]]:
    grupos = repartir(PESOS_DEL_VERIFICADOR, TOTAL_DEL_VERIFICADOR)
    return [
        ci_particion.construir_manifiesto(
            Shard(i + 1, TOTAL_DEL_VERIFICADOR),
            PESOS_DEL_VERIFICADOR,
            grupos[i],
            {n: 0.5 for n in grupos[i]},
        )
        for i in range(TOTAL_DEL_VERIFICADOR)
    ]


def _escribir(directorio: Path, manifiestos: list[dict[str, Any]]) -> Path:
    for i, manifiesto in enumerate(manifiestos):
        carpeta = directorio / f"test-shard-py3.11-{i + 1}"
        carpeta.mkdir(parents=True)
        (carpeta / ci_particion.NOMBRE_DEL_MANIFIESTO).write_text(json.dumps(manifiesto), encoding="utf-8")
    return directorio


def test_verificar_acepta_un_conjunto_de_manifiestos_completo_y_consistente(tmp_path: Path) -> None:
    errores = verificar(_escribir(tmp_path, _manifiestos_validos()), TOTAL_DEL_VERIFICADOR)

    assert errores == []


def _sin_el_segundo_shard(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return m[:1]


def _con_un_shard_de_mas(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*m, {**m[0], "shard": 3}]


def _con_indice_repetido(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "shard": 1}]


def _con_total_de_shards_distinto(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "total_shards": 3}]


def _con_huella_distinta(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "huella": "0" * 64}]


def _con_total_colectado_distinto(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "total_colectado": m[1]["total_colectado"] + 1}]


def _con_un_test_sin_correr(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Un shard 'pierde' un archivo: la suma de lo seleccionado ya no alcanza el total."""
    perdido = sorted(m[1]["archivos"])[0]
    archivos = {k: v for k, v in m[1]["archivos"].items() if k != perdido}
    return [m[0], {**m[1], "archivos": archivos, "seleccionados": sum(archivos.values())}]


def _con_un_archivo_en_dos_shards(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repetido = sorted(m[0]["archivos"])[0]
    peso = m[0]["archivos"][repetido]
    archivos = {**m[1]["archivos"], repetido: peso}
    return [m[0], {**m[1], "archivos": archivos, "seleccionados": sum(archivos.values())}]


def _con_seleccionados_que_no_suman_sus_archivos(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "seleccionados": m[1]["seleccionados"] + 1}]


def _con_version_desconocida(m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m[0], {**m[1], "version": 99}]


@pytest.mark.parametrize(
    ("mutador", "fragmento_del_error"),
    [
        (_sin_el_segundo_shard, "esperaban"),
        (_con_un_shard_de_mas, "esperaban"),
        (_con_indice_repetido, "índices"),
        (_con_total_de_shards_distinto, "total_shards"),
        (_con_huella_distinta, "misma suite"),
        (_con_total_colectado_distinto, "mismo total"),
        (_con_un_test_sin_correr, "suma"),
        (_con_un_archivo_en_dos_shards, "más de un shard"),
        (_con_seleccionados_que_no_suman_sus_archivos, "declara"),
        (_con_version_desconocida, "versión"),
    ],
)
def test_verificar_rechaza_cada_modo_de_falla(mutador: Any, fragmento_del_error: str, tmp_path: Path) -> None:
    errores = verificar(_escribir(tmp_path, mutador(_manifiestos_validos())), TOTAL_DEL_VERIFICADOR)

    assert errores, "el verificador dejó pasar un conjunto de manifiestos roto"
    assert any(fragmento_del_error in e for e in errores), errores


def test_verificar_falla_si_el_directorio_no_tiene_ningun_manifiesto(tmp_path: Path) -> None:
    errores = verificar(tmp_path, TOTAL_DEL_VERIFICADOR)

    assert any("esperaban" in e for e in errores)


def test_verificar_falla_con_un_manifiesto_que_no_es_json(tmp_path: Path) -> None:
    carpeta = tmp_path / "test-shard-py3.11-1"
    carpeta.mkdir()
    (carpeta / ci_particion.NOMBRE_DEL_MANIFIESTO).write_text("{no es json", encoding="utf-8")

    errores = verificar(tmp_path, 1)

    assert any(ci_particion.NOMBRE_DEL_MANIFIESTO in e for e in errores)


def test_el_manifiesto_tiene_un_nombre_propio_que_el_producto_no_genera() -> None:
    """Los tests (y el producto) escriben ``manifest.json`` por todos lados, p. ej. ``.skyclaw-output/manifest.json``:
    un nombre genérico en una búsqueda recursiva recoge archivos ajenos. Lo descubrió el ensayo local de los 4 shards."""
    assert ci_particion.NOMBRE_DEL_MANIFIESTO == "shard-manifest.json"


def test_verificar_ignora_cualquier_manifest_json_ajeno_en_cualquier_nivel(tmp_path: Path) -> None:
    directorio = _escribir(tmp_path, _manifiestos_validos())
    for ajeno in (
        directorio / "manifest.json",
        directorio / "test-shard-py3.11-1" / "manifest.json",
        directorio
        / "test-shard-py3.11-1"
        / "tmp"
        / "test_x0"
        / "mods"
        / "Salida"
        / ".skyclaw-output"
        / "manifest.json",
    ):
        ajeno.parent.mkdir(parents=True, exist_ok=True)
        ajeno.write_text("esto no es JSON ni un manifiesto de shard", encoding="utf-8")

    assert verificar(directorio, TOTAL_DEL_VERIFICADOR) == []


def test_verificar_solo_busca_un_manifiesto_por_artefacto_a_un_nivel(tmp_path: Path) -> None:
    """``download-artifact`` deja cada artefacto en su carpeta directamente bajo el destino: un manifiesto más hondo no es de
    un shard (es de un árbol temporal de tests) y no puede contar."""
    for i, manifiesto in enumerate(_manifiestos_validos()):
        hondo = tmp_path / f"test-shard-py3.11-{i + 1}" / "anidado"
        hondo.mkdir(parents=True)
        (hondo / ci_particion.NOMBRE_DEL_MANIFIESTO).write_text(json.dumps(manifiesto), encoding="utf-8")

    errores = verificar(tmp_path, TOTAL_DEL_VERIFICADOR)

    assert any("esperaban" in e for e in errores)


def test_la_huella_depende_del_contenido_de_la_suite_y_no_del_orden() -> None:
    a = huella_de_suite({"x": 1, "y": 2})

    assert a == huella_de_suite({"y": 2, "x": 1})
    assert a != huella_de_suite({"x": 1, "y": 3})
    assert a != huella_de_suite({"x": 1, "y": 2, "z": 1})


def test_main_verificar_devuelve_0_si_todo_cuadra_y_1_si_no(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bueno = _escribir(tmp_path / "bueno", _manifiestos_validos())
    roto = _escribir(tmp_path / "roto", _con_huella_distinta(_manifiestos_validos()))

    assert ci_particion.main(["verificar", str(bueno), "--total", str(TOTAL_DEL_VERIFICADOR)]) == 0
    assert "OK" in capsys.readouterr().out

    assert ci_particion.main(["verificar", str(roto), "--total", str(TOTAL_DEL_VERIFICADOR)]) == 1
    assert "misma suite" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# El plugin de pytest, ejercido de verdad en un subproceso sobre un árbol mínimo
# ---------------------------------------------------------------------------

ARBOL_MINIMO = {
    "test_a.py": "def test_1():\n    pass\n\ndef test_2():\n    pass\n\ndef test_3():\n    pass\n",
    "test_b.py": "def test_1():\n    pass\n",
    "test_c.py": "def test_1():\n    pass\n\ndef test_2():\n    pass\n",
    "test_d.py": "import pytest\n\n@pytest.mark.parametrize('n', range(4))\ndef test_p(n):\n    assert n >= 0\n",
    "test_e.py": "def test_1():\n    pass\n",
}
TOTAL_DEL_ARBOL_MINIMO = 11


def _arbol_minimo(destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    # ``tmp_path`` cae dentro del repo (``--basetemp=.pytest-tmp``): sin su propio ini, el subproceso heredaría el
    # ``pyproject.toml`` del repo (y sus ``addopts``). Un ini vacío fija la raíz acá y aísla la prueba.
    (destino / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for nombre, contenido in ARBOL_MINIMO.items():
        (destino / nombre).write_text(contenido, encoding="utf-8")
    return destino


def _correr_pytest(
    carpeta: Path,
    *args: str,
    shard: str | None = None,
    manifiesto: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SKYCLAW_SHARD")}
    env["PYTHONPATH"] = str(RAIZ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # solo el plugin de partición: la prueba no depende de otros plugins
    if shard is not None:
        env["SKYCLAW_SHARD"] = shard
    if manifiesto is not None:
        env["SKYCLAW_SHARD_MANIFEST"] = str(manifiesto)
    return subprocess.run(  # noqa: S603 -- ejecuta el pytest del propio intérprete sobre un árbol temporal
        [sys.executable, "-m", "pytest", "-p", "scripts.ci_particion", "-p", "no:cacheprovider", *args],
        cwd=carpeta,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _nodeids(salida: str) -> list[str]:
    return [linea.strip() for linea in salida.splitlines() if "::" in linea and not linea.startswith(" ")]


def test_sin_la_variable_el_plugin_no_toca_la_coleccion(tmp_path: Path) -> None:
    carpeta = _arbol_minimo(tmp_path / "suite")

    resultado = _correr_pytest(carpeta, "--collect-only", "-q")

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert len(_nodeids(resultado.stdout)) == TOTAL_DEL_ARBOL_MINIMO


@pytest.mark.parametrize("total", [1, 2, 3, 5])
def test_los_shards_juntos_colectan_exactamente_la_suite_sin_repetir_ni_perder(total: int, tmp_path: Path) -> None:
    carpeta = _arbol_minimo(tmp_path / "suite")
    completa = _nodeids(_correr_pytest(carpeta, "--collect-only", "-q").stdout)

    por_shard: list[list[str]] = []
    for indice in range(1, total + 1):
        resultado = _correr_pytest(carpeta, "--collect-only", "-q", shard=f"{indice}/{total}")
        assert resultado.returncode in (0, 5), (
            resultado.stdout + resultado.stderr
        )  # 5 = shard sin tests (más shards que archivos)
        por_shard.append(_nodeids(resultado.stdout))

    juntos = [n for ids in por_shard for n in ids]
    assert sorted(juntos) == sorted(completa), "la unión de los shards no es la suite"
    assert len(juntos) == len(set(juntos)), "un test quedó en dos shards"


def test_un_archivo_nunca_se_parte_entre_shards(tmp_path: Path) -> None:
    carpeta = _arbol_minimo(tmp_path / "suite")
    archivos_por_shard = []
    for indice in (1, 2):
        ids = _nodeids(_correr_pytest(carpeta, "--collect-only", "-q", shard=f"{indice}/2").stdout)
        archivos_por_shard.append({n.split("::", 1)[0] for n in ids})

    assert not (archivos_por_shard[0] & archivos_por_shard[1])


def test_el_reparto_de_cada_shard_es_el_mismo_en_dos_corridas(tmp_path: Path) -> None:
    carpeta = _arbol_minimo(tmp_path / "suite")

    primera = _nodeids(_correr_pytest(carpeta, "--collect-only", "-q", shard="2/3").stdout)
    segunda = _nodeids(_correr_pytest(carpeta, "--collect-only", "-q", shard="2/3").stdout)

    assert primera == segunda


@pytest.mark.parametrize("valor", ["0/2", "3/2", "x", "1/0", "1/2/3", " 1/2"])
def test_un_valor_invalido_de_shard_aborta_la_sesion_en_vez_de_correr_todo(valor: str, tmp_path: Path) -> None:
    """Fail-closed: un SKYCLAW_SHARD mal escrito NO puede degradar en silencio a «correr la suite entera»."""
    carpeta = _arbol_minimo(tmp_path / "suite")

    resultado = _correr_pytest(carpeta, "--collect-only", "-q", shard=valor)

    assert resultado.returncode != 0
    assert "SKYCLAW_SHARD" in resultado.stdout + resultado.stderr


def test_una_variable_vacia_equivale_a_no_definirla(tmp_path: Path) -> None:
    """La única forma de «apagar» el reparto es no definir la variable (o dejarla vacía), nunca un valor inválido."""
    carpeta = _arbol_minimo(tmp_path / "suite")

    resultado = _correr_pytest(carpeta, "--collect-only", "-q", shard="")

    assert resultado.returncode == 0
    assert len(_nodeids(resultado.stdout)) == TOTAL_DEL_ARBOL_MINIMO


def test_el_manifiesto_describe_lo_que_el_shard_selecciono_y_alimenta_al_verificador(tmp_path: Path) -> None:
    carpeta = _arbol_minimo(tmp_path / "suite")
    total = 2
    raiz_manifiestos = tmp_path / "manifiestos"

    for indice in range(1, total + 1):
        manifiesto = raiz_manifiestos / f"test-shard-py3.11-{indice}" / ci_particion.NOMBRE_DEL_MANIFIESTO
        resultado = _correr_pytest(carpeta, "-q", shard=f"{indice}/{total}", manifiesto=manifiesto)
        assert resultado.returncode == 0, resultado.stdout + resultado.stderr
        contenido = json.loads(manifiesto.read_text(encoding="utf-8"))
        assert contenido["shard"] == indice
        assert contenido["total_shards"] == total
        assert contenido["total_colectado"] == TOTAL_DEL_ARBOL_MINIMO
        assert contenido["seleccionados"] == sum(contenido["archivos"].values())
        assert set(contenido["duraciones_s"]) == set(contenido["archivos"]), "una duración por archivo propio"
        assert all(d >= 0 for d in contenido["duraciones_s"].values())

    assert verificar(raiz_manifiestos, total) == []


def test_ejecutar_el_modulo_como_script_verifica_los_manifiestos(tmp_path: Path) -> None:
    """``python -m scripts.ci_particion verificar`` es lo que corre el workflow: el punto de entrada tiene que andar."""
    directorio = _escribir(tmp_path / "m", _manifiestos_validos())
    env = {**os.environ, "PYTHONPATH": str(RAIZ)}

    resultado = subprocess.run(  # noqa: S603 -- ejecuta el propio módulo del repo con el intérprete actual
        [
            sys.executable,
            "-m",
            "scripts.ci_particion",
            "verificar",
            str(directorio),
            "--total",
            str(TOTAL_DEL_VERIFICADOR),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "OK" in resultado.stdout
