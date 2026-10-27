"""NP-A0j (parte 2): ``EFECTO_POSIBLE`` por textura — ¿hay alguna malla del perfil donde el plugin de relieve use lo que el generador publica?

El generador publica ``x_p.dds`` para cada ``x_n.dds`` de un mod. En la prueba en el rig, 3 de 4 texturas estaban en mallas con relieve de
fábrica, que Auto Parallax apaga: la salida existía y no se veía. Este módulo lo dice de antemano, con la misma resolución del perfil que
el resto del diagnóstico (``cargar_perfil``: quién gana cada ruta) y la regla del plugin (``assets/auto_parallax_regla.py``).

Qué se verifica, sobre un disco real (``tmp_path``) con NIFs sintéticos (``tests/_nif_sintetico.py``):

- ``SI`` solo con una forma elegible que, siguiendo la búsqueda del plugin (altura → difuso → normal), terminaría usando esa salida;
- ``NO`` solo si TODAS las formas que la referencian quedan fuera por una razón que se nombra, y nada de lo leído quedó incompleto;
- ``NO_VERIFICADO`` en todo lo demás: ninguna malla suelta la referencia (podría estar en un ``.bsa``, que no se lee), la búsqueda depende de
  un archivo que no se puede comprobar, o alguna malla no se pudo leer por completo. Lo no observado no se da por ausente;
- que la malla que cuenta es la que GANA en el overlay (una de menor prioridad tapada por otro mod no existe para el juego).
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from dataclasses import dataclass

import pytest

from sky_claw.local.native_parallax import elegibilidad, provider
from sky_claw.local.native_parallax.diagnostico_perfil import PerfilError
from sky_claw.local.native_parallax.elegibilidad import (
    ALCANCE_DE_LA_ELEGIBILIDAD,
    MAXIMO_DE_MALLAS_POR_TEXTURA,
    EfectoPosible,
    ElegibilidadDeTextura,
    InformeDeElegibilidad,
    evaluar_elegibilidad,
)
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from tests._nif_sintetico import (
    Bloque,
    bloque_alfa,
    bloque_forma_bs,
    bloque_juego_de_texturas,
    bloque_nodo,
    bloque_shader_de_efecto,
    bloque_shader_de_iluminacion,
    construir_nif,
)

_CARPETA = "textures/architecture/whiterun"
_N1 = f"{_CARPETA}/muro01_n.dds"
_P1 = f"{_CARPETA}/muro01_p.dds"
_N2 = f"{_CARPETA}/muro02_n.dds"
_P2 = f"{_CARPETA}/muro02_p.dds"
_D1 = "textures\\architecture\\whiterun\\muro01.dds"
_NN1 = "textures\\architecture\\whiterun\\muro01_n.dds"
_D2 = "textures\\architecture\\whiterun\\muro02.dds"
_NN2 = "textures\\architecture\\whiterun\\muro02_n.dds"
_MOD = "Texturas"
_MALLAS = "Mallas"
_RUTA_MALLA = "meshes/architecture/whiterun/muro.nif"
_VC = 1 << 5


def _nif(
    *,
    difuso: str = _D1,
    normal: str = _NN1,
    altura: str = "",
    tipo_de_shader: int = 0,
    flags_1: int = 0,
    flags_2: int = _VC,
    alfa: bool = False,
    efecto: bool = False,
) -> bytes:
    bloques = [bloque_nodo(hijos=(1,))]
    if efecto:
        bloques += [bloque_forma_bs(nombre=-1, shader=2), bloque_shader_de_efecto()]
    else:
        bloques += [
            bloque_forma_bs(nombre=-1, shader=2, alfa=4 if alfa else -1),
            bloque_shader_de_iluminacion(
                tipo_de_shader=tipo_de_shader, flags_1=flags_1, flags_2=flags_2, juego_de_texturas=3
            ),
            bloque_juego_de_texturas((difuso, normal, "", altura)),
        ]
        if alfa:
            bloques.append(bloque_alfa(flags=0, umbral=0))
    return construir_nif(bloques)


@dataclass
class Mundo:
    raiz: pathlib.Path

    @property
    def mods(self) -> pathlib.Path:
        return self.raiz / "mods"

    @property
    def data(self) -> pathlib.Path:
        return self.raiz / "data"

    def archivo(self, mod: str, ruta: str, contenido: bytes = b"x") -> None:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido + mod.encode())

    def malla(self, contenido: bytes, *, mod: str = _MALLAS, ruta: str = _RUTA_MALLA) -> None:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido)

    def modlist(self, *lineas: str, perfil: str = "Default") -> None:
        """``lineas`` en el orden de MO2: la primera es la de MAYOR prioridad."""
        ruta = self.data / "profiles" / perfil / "modlist.txt"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# generado\n" + "\n".join(lineas) + "\n", encoding="utf-8")

    def evaluar(self, **kw: object) -> InformeDeElegibilidad:
        return evaluar_elegibilidad(mods_dir=self.mods, data_root=self.data, perfil="Default", mod=_MOD, **kw)  # type: ignore[arg-type]

    def por_ruta(self, informe: InformeDeElegibilidad) -> dict[str, ElegibilidadDeTextura]:
        return {t.ruta_de_salida: t for t in informe.texturas}


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> Mundo:
    m = Mundo(tmp_path)
    m.archivo(_MOD, _N1)
    m.archivo(NOMBRE_DEL_MOD_DE_SALIDA, "textures/.vacio")
    (m.mods / _MALLAS).mkdir(parents=True)  # el mod de mallas está habilitado aunque todavía no tenga ninguna
    m.data.mkdir()
    m.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MALLAS}", f"+{_MOD}")
    return m


# --- SI ------------------------------------------------------------------------------------------------------------------


def test_una_forma_elegible_que_usaria_la_salida_da_si_y_nombra_la_malla(mundo: Mundo) -> None:
    mundo.malla(_nif())

    informe = mundo.evaluar()

    (textura,) = informe.texturas
    assert textura.ruta_de_salida == _P1
    assert textura.efecto is EfectoPosible.SI
    assert textura.mallas == (f"{_RUTA_MALLA} [{_MALLAS}]",)


def test_el_informe_resume_el_perfil_el_mod_y_la_lectura(mundo: Mundo) -> None:
    mundo.malla(_nif())

    informe = mundo.evaluar()

    assert informe.perfil == "Default"
    assert informe.mod == _MOD
    assert informe.n_habilitados == 3
    assert informe.n_mallas_leidas == 1
    assert informe.mallas_no_leidas == ()
    assert informe.alcance == ALCANCE_DE_LA_ELEGIBILIDAD


def test_las_mayusculas_y_las_barras_de_la_ruta_del_nif_no_cambian_el_resultado(mundo: Mundo) -> None:
    mundo.malla(
        _nif(
            difuso="Textures\\Architecture\\Whiterun\\Muro01.dds",
            normal="Textures\\Architecture\\Whiterun\\Muro01_n.dds",
        )
    )

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.SI


def test_una_sola_forma_elegible_alcanza_aunque_otras_formas_de_otras_mallas_no_lo_sean(mundo: Mundo) -> None:
    mundo.malla(_nif(tipo_de_shader=3), ruta="meshes/a.nif")
    mundo.malla(_nif(alfa=True), ruta="meshes/b.nif")
    mundo.malla(_nif(), ruta="meshes/c.nif")

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.SI
    assert textura.mallas == (f"meshes/c.nif [{_MALLAS}]",)


def test_el_si_concuerda_el_numero_de_mallas_en_singular_y_en_plural(mundo: Mundo) -> None:
    mundo.malla(_nif(), ruta="meshes/a.nif")
    (uno,) = mundo.evaluar().texturas
    mundo.malla(_nif(), ruta="meshes/b.nif")
    (dos,) = mundo.evaluar().texturas

    assert uno.razon.startswith("1 malla donde")
    assert uno.n_mallas == 1
    assert dos.razon.startswith("2 mallas donde")
    assert dos.n_mallas == 2


def test_las_mallas_nombradas_estan_acotadas_pero_el_conteo_es_el_real(mundo: Mundo) -> None:
    for i in range(MAXIMO_DE_MALLAS_POR_TEXTURA + 3):
        mundo.malla(_nif(), ruta=f"meshes/m{i:02d}.nif")

    (textura,) = mundo.evaluar().texturas

    assert len(textura.mallas) == MAXIMO_DE_MALLAS_POR_TEXTURA
    assert textura.n_mallas == MAXIMO_DE_MALLAS_POR_TEXTURA + 3


def test_dos_texturas_del_mismo_mod_se_evaluan_cada_una_por_su_cuenta(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _N2)
    mundo.malla(_nif())  # solo muro01 tiene malla elegible
    mundo.malla(_nif(difuso=_D2, normal=_NN2, tipo_de_shader=3), ruta="meshes/otra.nif")

    efectos = {ruta: t.efecto for ruta, t in mundo.por_ruta(mundo.evaluar()).items()}

    assert efectos == {_P1: EfectoPosible.SI, _P2: EfectoPosible.NO}


# --- NO: todas las formas que la referencian quedan fuera ---------------------------------------------------------------


def test_una_malla_con_relieve_de_fabrica_da_no_y_lo_explica(mundo: Mundo) -> None:
    mundo.malla(_nif(tipo_de_shader=3))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO
    assert "relieve de fábrica" in textura.razon
    assert textura.mallas == ()
    assert textura.n_mallas == 0
    assert "otra textura" not in textura.razon
    assert textura.razon.startswith("ninguna de las 1 forma que la referencian")


@pytest.mark.parametrize(
    ("kw", "texto"),
    [
        ({"alfa": True}, "transparencia"),
        ({"flags_2": 0}, "colores de vértice"),
        ({"flags_1": 1 << 23}, "Projected_UV"),
        ({"flags_1": 1 << 1}, "Skinned"),
        ({"flags_2": _VC | (1 << 2)}, "LOD_Objects"),
        ({"flags_2": _VC | (1 << 29)}, "Tree_Anim"),
        ({"flags_2": _VC | (1 << 10)}, "Vertex_Lighting"),
        ({"tipo_de_shader": 1}, "otro tipo de shader"),
    ],
)
def test_cada_exclusion_da_no_y_nombra_su_motivo(mundo: Mundo, kw: dict[str, object], texto: str) -> None:
    mundo.malla(_nif(**kw))  # type: ignore[arg-type]

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO
    assert texto in textura.razon


def test_una_forma_con_shader_de_efecto_no_aporta_rutas_de_textura_y_no_cuenta_como_referencia(mundo: Mundo) -> None:
    mundo.malla(_nif(efecto=True))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO
    assert ".bsa" in textura.razon


def test_el_no_cuenta_cuantas_formas_hay_de_cada_motivo(mundo: Mundo) -> None:
    mundo.malla(_nif(tipo_de_shader=3), ruta="meshes/a.nif")
    mundo.malla(_nif(tipo_de_shader=3), ruta="meshes/b.nif")
    mundo.malla(_nif(alfa=True), ruta="meshes/c.nif")

    (textura,) = mundo.evaluar().texturas

    assert textura.razon.startswith("ninguna de las 3 formas que la referencian")
    assert "relieve de fábrica (el plugin lo apaga): 2" in textura.razon
    assert "con transparencia (NiAlphaProperty): 1" in textura.razon


# --- NO_VERIFICADO: lo que no se puede afirmar -----------------------------------------------------------------------------


def test_sin_ninguna_malla_suelta_que_la_referencie_es_no_verificado_porque_podria_estar_en_un_bsa(
    mundo: Mundo,
) -> None:
    mundo.malla(_nif(difuso="textures\\otra\\cosa.dds", normal="textures\\otra\\cosa_n.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO
    assert ".bsa" in textura.razon


def test_sin_ninguna_malla_en_el_perfil_todo_es_no_verificado(mundo: Mundo) -> None:
    informe = mundo.evaluar()

    assert informe.n_mallas_leidas == 0
    assert [t.efecto for t in informe.texturas] == [EfectoPosible.NO_VERIFICADO]


def test_una_ranura_de_altura_que_no_esta_suelta_hace_no_verificable_la_busqueda(mundo: Mundo) -> None:
    mundo.malla(_nif(altura="textures\\architecture\\whiterun\\alto.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO
    assert "no se puede comprobar" in textura.razon


def test_un_difuso_con_otro_nombre_cuyo_p_no_esta_suelto_hace_no_verificable_la_busqueda(mundo: Mundo) -> None:
    mundo.malla(_nif(difuso="textures\\architecture\\whiterun\\otro.dds", normal=_NN1))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO


def test_una_ruta_cuyo_resultado_depende_de_las_mayusculas_es_no_verificada(mundo: Mundo) -> None:
    mundo.malla(_nif(difuso="", normal="textures\\architecture\\whiterun\\muro01_N.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO


def test_la_ambiguedad_de_un_candidato_posterior_no_invalida_un_candidato_firme_que_ya_decide(mundo: Mundo) -> None:
    """El difuso deriva a la salida de forma inequívoca y el plugin devuelve en cuanto encuentra: la normal ambigua que viene después no cuenta."""
    mundo.malla(_nif(difuso=_D1, normal="textures\\architecture\\whiterun\\muro01_N.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.SI


def test_una_malla_ilegible_baja_un_no_a_no_verificado_y_se_nombra(mundo: Mundo) -> None:
    mundo.malla(_nif(tipo_de_shader=3), ruta="meshes/a.nif")
    mundo.malla(b"esto no es un nif", ruta="meshes/rota.nif")

    informe = mundo.evaluar()

    (textura,) = informe.texturas
    assert textura.efecto is EfectoPosible.NO_VERIFICADO
    assert "no se pudieron leer" in textura.razon
    assert informe.mallas_no_leidas == (f"meshes/rota.nif [{_MALLAS}]",)
    assert informe.n_mallas_leidas == 1


def test_una_malla_ilegible_no_cambia_un_si(mundo: Mundo) -> None:
    mundo.malla(_nif(), ruta="meshes/a.nif")
    mundo.malla(b"roto", ruta="meshes/rota.nif")

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.SI


def test_una_malla_con_geometria_que_el_lector_no_sabe_abrir_cuenta_como_no_leida_por_completo(mundo: Mundo) -> None:
    mundo.malla(_nif(tipo_de_shader=3), ruta="meshes/a.nif")
    mundo.malla(construir_nif([Bloque("NiParticleSystem", b"\x00" * 16)]), ruta="meshes/particulas.nif")

    informe = mundo.evaluar()

    assert informe.texturas[0].efecto is EfectoPosible.NO_VERIFICADO
    assert informe.mallas_no_leidas == (f"meshes/particulas.nif [{_MALLAS}]",)


# --- Precedencia de la búsqueda --------------------------------------------------------------------------------------------


def test_si_la_ranura_de_altura_existe_suelta_el_plugin_usa_esa_y_no_la_nuestra(mundo: Mundo) -> None:
    mundo.archivo(_MOD, f"{_CARPETA}/alto.dds")
    mundo.malla(_nif(altura="textures\\architecture\\whiterun\\alto.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO
    assert "el plugin usaría otra textura antes que esta: 1" in textura.razon


def test_si_el_p_derivado_del_difuso_existe_suelto_el_plugin_usa_ese(mundo: Mundo) -> None:
    mundo.archivo(_MOD, f"{_CARPETA}/otro_p.dds")
    mundo.malla(_nif(difuso="textures\\architecture\\whiterun\\otro.dds", normal=_NN1))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO
    assert "otra textura" in textura.razon


# --- Qué malla cuenta --------------------------------------------------------------------------------------------------------


def test_cuenta_la_malla_que_gana_en_el_overlay_y_no_la_que_otro_mod_tapa(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Parche", f"+{_MALLAS}", f"+{_MOD}")
    mundo.malla(_nif(), mod=_MALLAS)  # elegible, pero el parche la reemplaza por una con relieve de fábrica
    mundo.malla(_nif(tipo_de_shader=3), mod="Parche")

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO


def test_una_malla_de_un_mod_deshabilitado_no_existe_para_el_juego(mundo: Mundo) -> None:
    mundo.malla(_nif(), mod="Apagado")

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO


def test_el_reconocimiento_de_mallas_es_solo_de_archivos_nif(mundo: Mundo) -> None:
    mundo.malla(_nif(), ruta="meshes/a.txt")

    informe = mundo.evaluar()

    assert informe.n_mallas_leidas == 0


# --- Falla cerrado ---------------------------------------------------------------------------------------------------------------


def test_una_ruta_de_textura_del_nif_que_se_sale_del_arbol_no_rompe_y_deja_la_busqueda_sin_verificar(
    mundo: Mundo,
) -> None:
    mundo.malla(_nif(altura="textures\\..\\fuera.dds"))

    (textura,) = mundo.evaluar().texturas

    assert textura.efecto is EfectoPosible.NO_VERIFICADO


def test_una_normal_del_mod_que_no_se_pudo_leer_hace_fallar_la_evaluacion_en_vez_de_omitirla(
    mundo: Mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo que no se pudo leer no se puede dar por «sin efecto»: la enumeración incompleta falla cerrado, como en el diagnóstico."""
    real = provider.huella_acotada

    def huella(ruta: pathlib.Path, tamano: int) -> str:
        if ruta.name == "muro01_n.dds":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta, tamano)

    monkeypatch.setattr(provider, "huella_acotada", huella)

    with pytest.raises(PerfilError, match="incompleta"):
        mundo.evaluar()


def test_el_mod_ausente_del_perfil_falla_con_perfil_error(mundo: Mundo) -> None:
    with pytest.raises(PerfilError, match="no está habilitado"):
        evaluar_elegibilidad(mods_dir=mundo.mods, data_root=mundo.data, perfil="Default", mod="Fantasma")


def test_mas_mallas_que_el_tope_falla_en_vez_de_evaluar_a_medias(mundo: Mundo) -> None:
    mundo.malla(_nif(), ruta="meshes/a.nif")
    mundo.malla(_nif(), ruta="meshes/b.nif")

    with pytest.raises(PerfilError, match="mallas"):
        mundo.evaluar(maximo_de_mallas=1)


def test_un_mod_sin_normales_falla_igual_que_el_diagnostico(mundo: Mundo) -> None:
    (mundo.mods / _MOD / _N1).unlink()

    with pytest.raises(PerfilError, match="ningún"):
        mundo.evaluar()


# --- Forma del informe y anclas --------------------------------------------------------------------------------------------------


def test_el_informe_y_cada_textura_son_inmutables(mundo: Mundo) -> None:
    mundo.malla(_nif())

    informe = mundo.evaluar()

    with pytest.raises(dataclasses.FrozenInstanceError):
        informe.mod = "otro"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        informe.texturas[0].efecto = EfectoPosible.NO  # type: ignore[misc]
    assert isinstance(informe.texturas, tuple)


def test_el_vocabulario_de_efectos_esta_cerrado() -> None:
    assert {e.name: e.value for e in EfectoPosible} == {"SI": "SI", "NO": "NO", "NO_VERIFICADO": "NO_VERIFICADO"}


def test_el_alcance_declara_lo_que_el_nif_no_muestra() -> None:
    for frase in ("Static", "bAutoEnableParallax", ".bsa", "no prueba"):
        assert frase in ALCANCE_DE_LA_ELEGIBILIDAD


def test_el_modulo_no_lee_ni_escribe_archivos_ni_arrastra_la_cli() -> None:
    texto = pathlib.Path(elegibilidad.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(texto)
    nombres = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)
    }
    importados = {n.module or "" for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}

    assert not nombres & {
        "open",
        "write_bytes",
        "write_text",
        "read_bytes",
        "scandir",
        "iterdir",
        "walk",
        "glob",
        "rglob",
    }
    assert not any(m.endswith((".cli", ".lote", ".export")) for m in importados)


def _llamadores_de(nombre: str) -> set[str]:
    """Módulos de ``sky_claw/`` que llaman (por nombre simple o atributo) a *nombre*."""
    raiz = pathlib.Path(__file__).resolve().parent.parent
    encontrados: set[str] = set()
    for archivo in (raiz / "sky_claw").rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.Call):
                f = nodo.func
                llamado = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if llamado == nombre:
                    encontrados.add(archivo.relative_to(raiz).as_posix())
    return encontrados


def test_los_consumidores_de_la_elegibilidad_estan_congelados() -> None:
    """Hoy la sirve SOLO la línea de comandos. GUI y agente LLM se cablean juntos (AGENTS.md: dos superficies, un recurso): una
    tercera superficie, o una sola de las dos, rompe esto hasta que se la declare acá junto a las demás."""
    assert _llamadores_de("evaluar_elegibilidad") == {
        "sky_claw/local/native_parallax/cli.py",
    }


def test_el_indice_de_mallas_lo_arma_solo_la_pieza_que_ya_es_dueña_del_resolutor() -> None:
    assert _llamadores_de("cargar_mallas_del_perfil") == {"sky_claw/local/native_parallax/elegibilidad.py"}
    assert _llamadores_de("indexar_ganadores") == {"sky_claw/local/native_parallax/diagnostico_perfil.py"}


def test_la_lectura_de_archivos_de_nif_vive_solo_en_el_lector_y_la_usa_solo_la_elegibilidad() -> None:
    assert _llamadores_de("leer_materiales_de_archivo") == {"sky_claw/local/native_parallax/elegibilidad.py"}
