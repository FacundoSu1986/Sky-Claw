"""NP-A0f: ``generar`` con perfil de MO2 (``--data-root``, ``--mod``, ``--perfil``) usa el MISMO diagnóstico que ``perfil``.

Qué se verifica: con perfil, ``generar`` no mide una textura que otro mod tapa (o que el overlay no entrega), la deja en
``omitidas_al_enumerar`` del informe, y AVISA antes de escribir si la salida va a quedar tapada o sin habilitar; pero no
bloquea por eso, porque en la primera corrida el mod de salida todavía no está ordenado en MO2 (hay que generarlo para poder
ubicarlo). Sin los argumentos de perfil nada cambia. Lo que no se puede afirmar falla cerrado, y no se combina con
``--retirar-no-incluidas`` (lo omitido por el perfil NO es «ausente»). El orden del ``modlist.txt`` es el de MO2: la primera línea
habilitada es la de MAYOR prioridad.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

from sky_claw.local.native_parallax import cli
from sky_claw.local.native_parallax.cli import CODIGO_DE_USO, CODIGO_OK, ENTORNO_TEXCONV, main
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from tests.test_native_parallax_cli import _escribir_normal, _fabrica

_CARPETA = "textures/architecture/whiterun"
_MOD = "Texturas"
_CONTROL = "SkyClaw-Control-Visual"
_N1 = f"{_CARPETA}/muro01_n.dds"
_N2 = f"{_CARPETA}/muro02_n.dds"
_P1 = f"{_CARPETA}/muro01_p.dds"


@dataclass
class Mundo:
    """Una instancia MO2 mínima en disco con un mod de texturas de dos normales reales."""

    raiz: pathlib.Path

    @property
    def mods(self) -> pathlib.Path:
        return self.raiz / "mods"

    @property
    def data(self) -> pathlib.Path:
        return self.raiz / "data"

    @property
    def informe(self) -> pathlib.Path:
        return self.raiz / "informe.json"

    @property
    def salida(self) -> pathlib.Path:
        return self.mods / NOMBRE_DEL_MOD_DE_SALIDA

    def modlist(self, *lineas: str, perfil: str = "Default") -> None:
        """``lineas`` en el orden del archivo de MO2: la primera es la de MAYOR prioridad."""
        ruta = self.data / "profiles" / perfil / "modlist.txt"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# generado\n" + "\n".join(lineas) + "\n", encoding="utf-8")

    def otro_mod(self, mod: str, ruta: str) -> None:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"contenido de " + mod.encode())


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> Mundo:
    m = Mundo(tmp_path)
    _escribir_normal(m.mods / _MOD / _N1)
    _escribir_normal(m.mods / _MOD / _N2, fase=1.0)
    m.data.mkdir()
    return m


def _generar(mundo: Mundo, *extra: str, carpeta: pathlib.Path | None = None, **kwargs: Any) -> tuple[int, str]:
    salida = io.StringIO()
    codigo = main(
        [
            "generar",
            "--carpeta",
            str(carpeta if carpeta is not None else mundo.mods / _MOD),
            "--mods",
            str(mundo.mods),
            "--informe",
            str(mundo.informe),
            "--incluir-revision",
            *extra,
        ],
        entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
        fabrica_de_exportador=kwargs.get("fabrica", _fabrica()),
        base_de_estado=mundo.raiz / "estado",
        salida=salida,
    )
    return codigo, salida.getvalue()


def _con_perfil(mundo: Mundo, *extra: str, **kwargs: Any) -> tuple[int, str]:
    return _generar(mundo, "--data-root", str(mundo.data), "--mod", _MOD, *extra, **kwargs)


def _informe(mundo: Mundo) -> dict[str, Any]:
    return json.loads(mundo.informe.read_text(encoding="utf-8"))


def _exigir_aviso_antes_de_procesar(texto: str, aviso: str) -> None:
    """El aviso sale ANTES de que el lote empiece: antes de su primera línea de progreso (``[1/N]``) y del informe, que son
    lo primero que imprime ``procesar_lote`` al trabajar la primera textura y lo último que escribe la corrida."""
    assert aviso in texto
    assert texto.index(aviso) < texto.index("texturas por procesar") < texto.index("[1/")
    assert texto.index(aviso) < texto.index("Informe:")


def _escritas(mundo: Mundo) -> list[str]:
    carpeta = mundo.salida / _CARPETA
    return sorted(p.name for p in carpeta.iterdir()) if carpeta.exists() else []


# ---------------------------------------------------------------------------
# Sin tapados: nada cambia
# ---------------------------------------------------------------------------


def test_sin_los_argumentos_de_perfil_generar_se_comporta_como_antes(mundo: Mundo) -> None:
    codigo, texto = _generar(mundo)

    assert codigo == CODIGO_OK
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]
    assert "TAPADA" not in texto


def test_con_perfil_y_sin_nadie_que_tape_procesa_lo_mismo_y_no_avisa(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]
    assert "TAPADA" not in texto and "FUERA DEL OVERLAY" not in texto
    assert _informe(mundo)["omitidas_al_enumerar"] == []


# ---------------------------------------------------------------------------
# La entrada tapada no se mide
# ---------------------------------------------------------------------------


def test_una_normal_tapada_por_otro_mod_no_se_mide_ni_se_escribe_y_queda_en_el_informe(mundo: Mundo) -> None:
    mundo.otro_mod("Retexture", _N2)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK
    assert _escritas(mundo) == ["muro01_p.dds"]  # la tapada no se mide
    informe = _informe(mundo)
    assert [a["ruta_de_salida"] for a in informe["archivos"]] == [_P1]
    assert any(_N2 in o and "Retexture" in o for o in informe["omitidas_al_enumerar"])
    assert "ENTRADA TAPADA" in texto and "1 texturas por procesar" in texto


def test_una_normal_oculta_con_mohidden_no_se_mide(mundo: Mundo) -> None:
    _escribir_normal(mundo.mods / _MOD / "textures/oculta.mohidden/zz_n.dds")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]
    assert "FUERA DEL OVERLAY" in texto
    assert any("zz_n.dds" in o for o in _informe(mundo)["omitidas_al_enumerar"])


def test_si_todas_las_normales_estan_tapadas_es_un_error_de_uso_y_no_se_escribe_nada(mundo: Mundo) -> None:
    mundo.otro_mod("Retexture", _N1)
    mundo.otro_mod("Retexture", _N2)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "tapad" in texto
    assert _escritas(mundo) == []
    assert not mundo.informe.exists()


# ---------------------------------------------------------------------------
# La salida tapada avisa ANTES de escribir, pero no bloquea
# ---------------------------------------------------------------------------


def test_h12_si_el_control_tapa_la_salida_avisa_antes_de_escribir_y_aun_asi_escribe(mundo: Mundo) -> None:
    mundo.otro_mod(_CONTROL, _P1)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK  # el aviso no cambia el resultado del lote
    assert "SALIDA TAPADA" in texto and _CONTROL in texto
    _exigir_aviso_antes_de_procesar(texto, "SALIDA TAPADA")
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]


def test_overwrite_por_encima_de_la_salida_la_tapa_y_avisa_antes_de_escribir(mundo: Mundo) -> None:
    """``overwrite`` gana a todo mod: aunque la salida sea la primera línea del modlist, ``overwrite`` la tapa."""
    destino = mundo.data / "overwrite" / _P1
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"overwrite")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK
    assert "SALIDA TAPADA" in texto and "overwrite" in texto
    _exigir_aviso_antes_de_procesar(texto, "SALIDA TAPADA")
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]


def test_la_primera_corrida_con_el_mod_de_salida_sin_habilitar_avisa_y_no_bloquea(mundo: Mundo) -> None:
    """Hay que generar el mod para poder ordenarlo en MO2: bloquear acá sería un círculo."""
    mundo.modlist(f"+{_MOD}")

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_OK
    _exigir_aviso_antes_de_procesar(texto, "SALIDA NO HABILITADA")
    assert _escritas(mundo) == ["muro01_p.dds", "muro02_p.dds"]


# ---------------------------------------------------------------------------
# Falla cerrado
# ---------------------------------------------------------------------------


def test_con_perfil_no_se_combina_con_retirar_porque_lo_omitido_no_es_ausente(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo, "--retirar-no-incluidas")

    assert codigo == CODIGO_DE_USO
    assert "retirar" in texto
    assert _escritas(mundo) == []


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(("--data-root", "X"), id="data_root_sin_mod"),
        pytest.param(("--mod", _MOD), id="mod_sin_data_root"),
        pytest.param(("--perfil", "Otro"), id="perfil_sin_data_root_ni_mod"),
    ],
)
def test_los_argumentos_de_perfil_van_todos_juntos_o_ninguno(mundo: Mundo, extra: tuple[str, ...]) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    argumentos = tuple(str(mundo.data) if a == "X" else a for a in extra)

    codigo, texto = _generar(mundo, *argumentos)

    assert codigo == CODIGO_DE_USO
    assert "--data-root" in texto and "--mod" in texto
    assert _escritas(mundo) == []


def test_la_carpeta_tiene_que_ser_la_del_mod_indicado(mundo: Mundo, tmp_path: pathlib.Path) -> None:
    """Si ``--carpeta`` no es ``<mods>/<mod>``, «lo gana otro mod» compara contra una carpeta que no es esa."""
    ajena = tmp_path / "ajena"
    _escribir_normal(ajena / _N1)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)

    codigo, texto = _con_perfil(mundo, carpeta=ajena)

    assert codigo == CODIGO_DE_USO
    assert "--carpeta" in texto
    assert _escritas(mundo) == []


@pytest.mark.parametrize("estado", ["sin_modlist", "mod_no_habilitado", "mod_es_la_salida"])
def test_un_perfil_que_no_se_puede_resolver_falla_cerrado_sin_escribir(mundo: Mundo, estado: str) -> None:
    if estado == "mod_no_habilitado":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}")
        mundo.salida.mkdir(parents=True)
    elif estado == "mod_es_la_salida":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
        mundo.salida.mkdir(parents=True)

    if estado == "mod_es_la_salida":
        salida = io.StringIO()
        codigo = main(
            [
                "generar",
                "--carpeta",
                str(mundo.salida),
                "--mods",
                str(mundo.mods),
                "--informe",
                str(mundo.informe),
                "--data-root",
                str(mundo.data),
                "--mod",
                NOMBRE_DEL_MOD_DE_SALIDA,
            ],
            entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
            fabrica_de_exportador=_fabrica(),
            base_de_estado=mundo.raiz / "estado",
            salida=salida,
        )
        assert codigo == CODIGO_DE_USO
    else:
        codigo, _texto = _con_perfil(mundo)
        assert codigo == CODIGO_DE_USO

    assert _escritas(mundo) == []
    assert not mundo.informe.exists()


def test_una_textura_ilegible_con_perfil_falla_cerrado_en_vez_de_darla_por_no_tapada(
    mundo: Mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.native_parallax import provider

    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.salida.mkdir(parents=True)
    real = provider.huella_acotada

    def huella(ruta: pathlib.Path, tamano: int) -> str:
        if ruta.name == "muro02_n.dds":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta, tamano)

    monkeypatch.setattr(provider, "huella_acotada", huella)

    codigo, texto = _con_perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "incompleta" in texto
    assert _escritas(mundo) == []


# ---------------------------------------------------------------------------
# Anclas que ENUMERAN: perfil y generar contestan «quién gana» con la MISMA pieza
# ---------------------------------------------------------------------------


def _funciones_de_cli() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    arbol = ast.parse(pathlib.Path(cli.__file__).read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _nombres_llamados(funcion: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(funcion) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} | {
        n.func.attr for n in ast.walk(funcion) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _alcanzables_desde(nombre_de_funcion: str) -> set[str]:
    """Nombres llamados, directa o transitivamente, a través de las funciones definidas en ``cli.py``."""
    funciones = _funciones_de_cli()
    vistos: set[str] = set()
    pendientes = [nombre_de_funcion]
    llamados: set[str] = set()
    while pendientes:
        actual = pendientes.pop()
        if actual in vistos or actual not in funciones:
            continue
        vistos.add(actual)
        directos = _nombres_llamados(funciones[actual])
        llamados |= directos
        pendientes.extend(directos & funciones.keys())
    return llamados


@pytest.mark.parametrize("consumidor", ["_perfil", "_generar"])
def test_los_dos_consumidores_del_perfil_usan_las_mismas_piezas_compartidas(consumidor: str) -> None:
    """Un consumidor que resolviera el perfil o el veredicto por su cuenta contestaría distinto (el hermano)."""
    llamadas = _alcanzables_desde(consumidor)

    assert {"_cargar_perfil", "_hallazgos_del_perfil"} <= llamadas


def test_la_resolucion_del_perfil_no_esta_repetida_fuera_de_la_pieza_compartida() -> None:
    """``read_enabled_mods`` e ``indexar_ganadores`` se llaman SOLO desde ``_cargar_perfil``."""
    for nombre, funcion in _funciones_de_cli().items():
        if nombre == "_cargar_perfil":
            continue
        assert not _nombres_llamados(funcion) & {"read_enabled_mods", "indexar_ganadores"}, nombre
