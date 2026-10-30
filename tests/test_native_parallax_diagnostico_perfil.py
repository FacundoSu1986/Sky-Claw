"""NP-A0g: ``diagnostico_perfil.py`` es LA pieza que contesta «¿qué gana, en el perfil de MO2, la entrada y la salida de un mod?».

Antes vivía en ``cli.py`` (subcomandos ``perfil`` y ``generar``). Las superficies que no son la línea de comandos (el agente LLM y el
dispatcher de la GUI) no pueden importar ``cli.py`` (arrastra el lote, el exportador y la CLI), y si cada una resolviera el perfil por
su cuenta contestarían distinto (el hermano que dice AGENTS.md). Qué se verifica: la API pública sobre un disco real (sin doble de
MO2), que lo que no se puede afirmar falla cerrado con ``PerfilError`` y que la clase queda anclada por AST: quién llama al resolutor
de ganadores y quién consume el diagnóstico.

El orden del ``modlist.txt`` es el de MO2: la primera línea habilitada es la de MAYOR prioridad.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError, dataclass

import pytest

from sky_claw.local.native_parallax import diagnostico_perfil, provider
from sky_claw.local.native_parallax.diagnostico_perfil import (
    ALCANCE_DEL_DIAGNOSTICO,
    MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
    MAXIMO_DE_HALLAZGOS_EN_EL_RESULTADO,
    TIPOS_QUE_IMPIDEN_MEDIR,
    Hallazgo,
    InformeDelPerfil,
    PerfilError,
    cargar_perfil,
    descubrir_normales_del_mod,
    diagnosticar_perfil,
    diagnostico_como_resultado,
    hallazgos_del_perfil,
    linea_de_hallazgo,
)
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA

_CARPETA = "textures/architecture/whiterun"
_N1 = f"{_CARPETA}/muro01_n.dds"
_N2 = f"{_CARPETA}/muro02_n.dds"
_P1 = f"{_CARPETA}/muro01_p.dds"
_MOD = "Texturas"
_CONTROL = "SkyClaw-Control-Visual"
_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent


@dataclass
class Mundo:
    """Una instancia MO2 mínima en disco. Las normales son archivos cualesquiera: el diagnóstico no las decodifica."""

    raiz: pathlib.Path

    @property
    def mods(self) -> pathlib.Path:
        return self.raiz / "mods"

    @property
    def data(self) -> pathlib.Path:
        return self.raiz / "data"

    def archivo(self, mod: str, ruta: str) -> None:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"contenido de " + mod.encode())

    def modlist(self, *lineas: str, perfil: str = "Default") -> None:
        """``lineas`` en el orden del archivo de MO2: la primera es la de MAYOR prioridad."""
        ruta = self.data / "profiles" / perfil / "modlist.txt"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# generado\n" + "\n".join(lineas) + "\n", encoding="utf-8")

    def diagnosticar(self, mod: str = _MOD, perfil: str = "Default") -> InformeDelPerfil:
        return diagnosticar_perfil(mods_dir=self.mods, data_root=self.data, perfil=perfil, mod=mod)


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> Mundo:
    m = Mundo(tmp_path)
    m.archivo(_MOD, _N1)
    m.archivo(_MOD, _N2)
    m.archivo(
        NOMBRE_DEL_MOD_DE_SALIDA, "textures/.vacio"
    )  # la carpeta existe (antes de la primera corrida hay una vacía)
    m.data.mkdir()
    return m


# ---------------------------------------------------------------------------
# La API pública sobre un disco real
# ---------------------------------------------------------------------------


def test_un_perfil_sin_tapados_devuelve_el_informe_con_su_resumen_y_sin_hallazgos(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    informe = mundo.diagnosticar()

    assert informe == InformeDelPerfil(perfil="Default", mod=_MOD, n_habilitados=2, n_texturas=2, hallazgos=())


def test_el_informe_es_inmutable_y_sus_hallazgos_son_una_tupla(mundo: Mundo) -> None:
    """Las superficies lo serializan y lo recortan: no debe poder cambiarse por debajo."""
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.archivo(_CONTROL, _P1)

    informe = mundo.diagnosticar()

    assert isinstance(informe.hallazgos, tuple)
    with pytest.raises(FrozenInstanceError):
        informe.mod = "otro"  # type: ignore[misc]


def test_h12_el_control_por_encima_de_la_salida_la_tapa(mundo: Mundo) -> None:
    mundo.archivo(_CONTROL, _P1)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    informe = mundo.diagnosticar()

    assert [(h.tipo, h.ruta) for h in informe.hallazgos] == [("SALIDA TAPADA", _P1)]
    assert _CONTROL in informe.hallazgos[0].detalle


def test_si_se_invierte_el_orden_del_modlist_la_salida_gana_y_no_hay_hallazgo(mundo: Mundo) -> None:
    mundo.archivo(_CONTROL, _P1)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_CONTROL}", f"+{_MOD}")

    assert mundo.diagnosticar().hallazgos == ()


def test_otro_mod_por_encima_tapa_la_entrada(mundo: Mundo) -> None:
    mundo.archivo("Retexture", _N2)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")

    informe = mundo.diagnosticar()

    assert [(h.tipo, h.ruta) for h in informe.hallazgos] == [("ENTRADA TAPADA", _N2)]


def test_una_normal_oculta_con_mohidden_esta_fuera_del_overlay(mundo: Mundo) -> None:
    mundo.archivo(_MOD, "textures/oculta.mohidden/zz_n.dds")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    informe = mundo.diagnosticar()

    assert [h.tipo for h in informe.hallazgos] == ["ENTRADA FUERA DEL OVERLAY"]


def test_el_mod_de_salida_sin_habilitar_es_un_hallazgo_y_no_un_error(mundo: Mundo) -> None:
    mundo.modlist(f"+{_MOD}")

    assert [h.tipo for h in mundo.diagnosticar().hallazgos] == ["SALIDA NO HABILITADA"]


def test_el_perfil_se_elige_por_nombre(mundo: Mundo) -> None:
    mundo.archivo(_CONTROL, _P1)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}", perfil="Pruebas")

    assert mundo.diagnosticar().hallazgos == ()
    assert [h.tipo for h in mundo.diagnosticar(perfil="Pruebas").hallazgos] == ["SALIDA TAPADA"]


def test_el_nombre_del_mod_se_busca_sin_distinguir_mayusculas_y_devuelve_el_del_modlist(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    assert mundo.diagnosticar(mod=_MOD.upper()).mod == _MOD


def test_los_tipos_que_impiden_medir_son_los_de_la_entrada_y_ninguno_de_la_salida() -> None:
    assert set(TIPOS_QUE_IMPIDEN_MEDIR) == {"ENTRADA TAPADA", "ENTRADA FUERA DEL OVERLAY"}


def test_la_linea_de_un_hallazgo_es_la_que_imprime_la_cli() -> None:
    assert linea_de_hallazgo(Hallazgo("ENTRADA TAPADA", "textures/a_n.dds", "la gana 'X'")) == (
        "  [ENTRADA TAPADA] textures/a_n.dds: la gana 'X'"
    )


# ---------------------------------------------------------------------------
# Falla cerrado: lo que no se puede afirmar es un PerfilError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caso", "mensaje"),
    [
        pytest.param("sin_modlist", "modlist", id="sin_modlist"),
        pytest.param("modlist_invalido", "inválida", id="modlist_invalido"),
        pytest.param("mod_no_habilitado", "no está habilitado", id="mod_no_habilitado"),
        pytest.param("mod_es_la_salida", "mod de salida", id="mod_es_la_salida"),
        pytest.param("mod_sin_carpeta", "Fantasma", id="mod_sin_carpeta"),
        pytest.param("sin_normales", "_n.dds", id="sin_normales"),
        pytest.param("mods_inexistente", "mods_dir", id="mods_inexistente"),
    ],
)
def test_lo_que_no_se_puede_afirmar_falla_cerrado_con_perfil_error(mundo: Mundo, caso: str, mensaje: str) -> None:
    mod = _MOD
    mods_dir = mundo.mods
    if caso == "modlist_invalido":
        mundo.modlist("esto no es una línea válida")
    elif caso == "mod_no_habilitado":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}")
    elif caso == "mod_es_la_salida":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
        mod = NOMBRE_DEL_MOD_DE_SALIDA
    elif caso == "mod_sin_carpeta":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Fantasma", f"+{_MOD}")
    elif caso == "sin_normales":
        mundo.archivo("Vacio", f"{_CARPETA}/muro01_d.dds")
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Vacio")
        mod = "Vacio"
    elif caso == "mods_inexistente":
        mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
        mods_dir = mundo.raiz / "no_existe"

    with pytest.raises(PerfilError, match=mensaje):
        diagnosticar_perfil(mods_dir=mods_dir, data_root=mundo.data, perfil="Default", mod=mod)


@pytest.mark.parametrize(
    "perfil",
    [
        pytest.param("", id="vacio"),
        pytest.param(".", id="punto"),
        pytest.param("..", id="punto_punto"),
        pytest.param("../Otro", id="sale_hacia_arriba"),
        pytest.param("Default/../../Otro", id="sale_pasando_por_un_perfil"),
        pytest.param("a/b", id="separador_posix"),
        pytest.param("a\\b", id="separador_windows"),
        pytest.param("Def\x00ault", id="nul"),
    ],
)
def test_el_perfil_tiene_que_ser_una_sola_carpeta_de_profiles(mundo: Mundo, perfil: str) -> None:
    """Un nombre que se sale de ``profiles`` leería el ``modlist.txt`` de OTRA carpeta y lo daría por el del perfil: falla cerrado.

    Hay un modlist válido FUERA de ``profiles`` (en ``<data>/Otro``): sin la validación, ``../Otro`` lo lee y el diagnóstico «funciona».
    """
    afuera = mundo.data / "Otro"
    afuera.mkdir(parents=True, exist_ok=True)
    (afuera / "modlist.txt").write_text(f"+{NOMBRE_DEL_MOD_DE_SALIDA}\n+{_MOD}\n", encoding="utf-8")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    with pytest.raises(PerfilError, match="perfil"):
        mundo.diagnosticar(perfil=perfil)


def test_un_nombre_de_perfil_con_puntos_o_espacios_adentro_es_valido(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}", perfil="Mi perfil v1.2")

    assert mundo.diagnosticar(perfil="Mi perfil v1.2").perfil == "Mi perfil v1.2"


def test_una_textura_ilegible_falla_cerrado_en_vez_de_darse_por_no_tapada(
    mundo: Mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mundo.archivo("Retexture", _N2)  # la ilegible además está tapada: un diagnóstico que la ignorara diría «limpio»
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")
    real = provider.huella_acotada

    def huella(ruta: pathlib.Path, tamano: int) -> str:
        if ruta.name == "muro02_n.dds" and ruta.parts[-len(_N2.split("/")) - 1] == _MOD:
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta, tamano)

    monkeypatch.setattr(provider, "huella_acotada", huella)

    with pytest.raises(PerfilError, match="incompleta"):
        mundo.diagnosticar()


# ---------------------------------------------------------------------------
# La enumeración completa
# ---------------------------------------------------------------------------


def test_descubrir_normales_del_mod_devuelve_las_normales_y_el_texto_de_lo_omitido(tmp_path: pathlib.Path) -> None:
    (tmp_path / "textures").mkdir()
    for nombre in ("a_n.dds", "b_n.dds"):
        (tmp_path / "textures" / nombre).write_bytes(b"x")
    (tmp_path / "textures" / "b_p.dds").write_bytes(b"p")  # un *_p.dds autoral al lado: se omite, y se dice

    normales, omitidas = descubrir_normales_del_mod(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a_n.dds"]
    assert len(omitidas) == 1 and "textures/b_n.dds" in omitidas[0]


def test_por_defecto_lo_ilegible_se_omite_y_se_dice_y_solo_con_exigir_completa_falla(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quien no actúa sobre lo que NO figura (la enumeración de ``generar`` sin retirar) conserva lo demás; quien declara «nada
    tapado» o retira salidas pide ``exigir_completa``."""
    (tmp_path / "textures").mkdir()
    for nombre in ("a_n.dds", "b_n.dds"):
        (tmp_path / "textures" / nombre).write_bytes(b"x")
    real = provider.huella_acotada

    def huella(ruta: pathlib.Path, tamano: int) -> str:
        if ruta.name == "b_n.dds":
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta, tamano)

    monkeypatch.setattr(provider, "huella_acotada", huella)

    normales, omitidas = descubrir_normales_del_mod(tmp_path)

    assert [n.ruta_de_origen for n in normales] == ["textures/a_n.dds"]
    assert len(omitidas) == 1 and "textures/b_n.dds" in omitidas[0] and "bloqueado" in omitidas[0]
    with pytest.raises(PerfilError, match="incompleta.*b_n.dds"):
        descubrir_normales_del_mod(tmp_path, exigir_completa=True)


def test_descubrir_normales_del_mod_usa_el_tope_que_se_le_pide(tmp_path: pathlib.Path) -> None:
    (tmp_path / "textures").mkdir()
    for i in range(3):
        (tmp_path / "textures" / f"{i}_n.dds").write_bytes(b"x")

    with pytest.raises(PerfilError):
        descubrir_normales_del_mod(tmp_path, maximo_de_archivos=2)
    normales, _ = descubrir_normales_del_mod(tmp_path, maximo_de_archivos=3)
    assert len(normales) == 3


def test_el_tope_por_defecto_es_el_de_la_herramienta_experimental() -> None:
    assert MAXIMO_DE_ARCHIVOS_DE_ENTRADA == 10_000


# ---------------------------------------------------------------------------
# El resultado de tool (lo que ven la GUI y el agente LLM): success/message + campos estructurados, ACOTADO
# ---------------------------------------------------------------------------


def _resultado(mundo: Mundo, **kw: object) -> dict[str, object]:
    return diagnostico_como_resultado(
        mods_dir=mundo.mods, data_root=mundo.data, **{"perfil": "Default", "mod": _MOD, **kw}
    )  # type: ignore[arg-type]


def test_el_resultado_sin_tapados_es_exitoso_con_mensaje_vacio_y_dice_su_alcance(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    r = _resultado(mundo)

    assert r == {
        "success": True,
        "message": "",
        "perfil": "Default",
        "mod": _MOD,
        "n_habilitados": 2,
        "n_texturas": 2,
        "n_hallazgos": 0,
        "hallazgos": [],
        "truncado": False,
        "impide_medir": False,
        "alcance": ALCANCE_DEL_DIAGNOSTICO,
    }
    assert "estructural" in ALCANCE_DEL_DIAGNOSTICO.lower()


def test_el_resultado_con_la_salida_tapada_lista_el_hallazgo_y_no_impide_medir(mundo: Mundo) -> None:
    mundo.archivo(_CONTROL, _P1)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    r = _resultado(mundo)

    assert r["success"] is True and r["message"] == ""
    assert r["n_hallazgos"] == 1 and r["impide_medir"] is False
    assert [(h["tipo"], h["ruta"]) for h in r["hallazgos"]] == [("SALIDA TAPADA", _P1)]  # type: ignore[index, union-attr]


def test_el_resultado_con_la_entrada_tapada_marca_que_impide_medir(mundo: Mundo) -> None:
    mundo.archivo(_CONTROL, _N1)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    r = _resultado(mundo)

    assert r["impide_medir"] is True
    assert {h["tipo"] for h in r["hallazgos"]} == {"ENTRADA TAPADA"}  # type: ignore[union-attr]


def test_el_resultado_recorta_los_hallazgos_pero_cuenta_todos(mundo: Mundo) -> None:
    """Una salida acotada: el agente LLM la lee en su contexto y la GUI la muestra; el total real no se pierde."""
    for i in range(5):
        mundo.archivo(_MOD, f"{_CARPETA}/extra{i}_n.dds")
        mundo.archivo(_CONTROL, f"{_CARPETA}/extra{i}_p.dds")
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    r = _resultado(mundo, maximo_de_hallazgos=3)

    assert r["n_hallazgos"] == 5 and len(r["hallazgos"]) == 3 and r["truncado"] is True  # type: ignore[arg-type]
    assert MAXIMO_DE_HALLAZGOS_EN_EL_RESULTADO > 0


@pytest.mark.parametrize("tope", [0, -5])
def test_un_tope_de_cero_o_negativo_no_lista_hallazgos_pero_sigue_contandolos(mundo: Mundo, tope: int) -> None:
    mundo.archivo(_CONTROL, _P1)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    r = _resultado(mundo, maximo_de_hallazgos=tope)

    assert r["hallazgos"] == [] and r["n_hallazgos"] == 1 and r["truncado"] is True


def test_el_resultado_de_lo_que_no_se_puede_afirmar_es_un_fallo_con_mensaje_y_sin_traceback(mundo: Mundo) -> None:
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}")

    r = _resultado(mundo)

    assert r["success"] is False
    assert isinstance(r["message"], str) and r["message"]
    assert "hallazgos" not in r


def test_el_resultado_no_se_traga_lo_que_no_es_un_error_del_perfil(
    mundo: Mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solo ``PerfilError`` es un resultado: un bug (o un OSError inesperado) sube a la capa de errores de la superficie."""

    def explota(**_: object) -> InformeDelPerfil:
        raise RuntimeError("bug")

    monkeypatch.setattr(diagnostico_perfil, "diagnosticar_perfil", explota)

    with pytest.raises(RuntimeError):
        _resultado(mundo)


# ---------------------------------------------------------------------------
# Anclas que ENUMERAN: quién llama a qué
# ---------------------------------------------------------------------------


def _llamadores_de(nombre: str) -> set[str]:
    """Módulos de ``sky_claw/`` que llaman (por nombre simple o atributo) a *nombre*."""
    encontrados: set[str] = set()
    for archivo in (_RAIZ_DEL_REPO / "sky_claw").rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                f = nodo.func
                llamado = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if llamado == nombre:
                    encontrados.add(archivo.relative_to(_RAIZ_DEL_REPO).as_posix())
    return encontrados


def _usuarios_de(nombre: str) -> set[str]:
    """Módulos de ``sky_claw/`` que USAN *nombre*: lo llaman o lo pasan como callable (``asyncio.to_thread(nombre, …)``)."""
    encontrados: set[str] = set()
    for archivo in (_RAIZ_DEL_REPO / "sky_claw").rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if (isinstance(nodo, ast.Name) and nodo.id == nombre and isinstance(nodo.ctx, ast.Load)) or (
                isinstance(nodo, ast.Attribute) and nodo.attr == nombre
            ):
                encontrados.add(archivo.relative_to(_RAIZ_DEL_REPO).as_posix())
    return encontrados


_MODULO = "sky_claw/local/native_parallax/diagnostico_perfil.py"
_CLI = "sky_claw/local/native_parallax/cli.py"
_ELEGIBILIDAD = "sky_claw/local/native_parallax/elegibilidad.py"


def test_el_resolutor_de_ganadores_se_llama_solo_desde_el_diagnostico() -> None:
    """Un consumidor que resolviera ``indexar_ganadores`` por su cuenta contestaría distinto: el resolutor es de esta pieza."""
    assert _llamadores_de("indexar_ganadores") == {_MODULO}


def test_los_consumidores_del_diagnostico_estan_congelados() -> None:
    """Agregar una superficie rompe esto hasta que se la declare acá (y se cablee junto a las demás)."""
    assert _llamadores_de("diagnosticar_perfil") == {_CLI, _MODULO}  # el módulo, vía ``diagnostico_como_resultado``
    # NP-A0j: la elegibilidad resuelve el perfil con la MISMA pieza (cargar_perfil), no con la suya.
    assert _llamadores_de("cargar_perfil") == {_MODULO, _CLI, _ELEGIBILIDAD}
    assert _llamadores_de("hallazgos_del_perfil") == {_MODULO, _CLI, _ELEGIBILIDAD}


def test_las_dos_superficies_del_resultado_de_tool_estan_congeladas() -> None:
    """NP-A0h: el resultado de tool lo sirven la GUI (dispatcher) y el agente LLM, y NINGUNA otra. Una tercera superficie rompe esto
    hasta que se la declare acá junto a las otras dos (el hermano que dice AGENTS.md)."""
    assert _usuarios_de("diagnostico_como_resultado") == {
        "sky_claw/app/orchestrator/tool_strategies/diagnosticar_perfil_parallax.py",
        "sky_claw/app/agent/tools/parallax_tools.py",
    }


def test_el_diagnostico_compone_las_mismas_piezas_que_usa_generar() -> None:
    """``diagnosticar_perfil`` (que usan ``perfil`` y las superficies) y ``generar`` comparten resolución y veredicto."""
    arbol = ast.parse((_RAIZ_DEL_REPO / _MODULO).read_text(encoding="utf-8"))
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "diagnosticar_perfil")
    llamadas = {n.func.id for n in ast.walk(funcion) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    assert {"cargar_perfil", "hallazgos_del_perfil", "descubrir_normales_del_mod"} <= llamadas


def test_el_modulo_es_de_solo_lectura_y_no_conoce_el_lote_ni_el_exportador() -> None:
    texto = (_RAIZ_DEL_REPO / _MODULO).read_text(encoding="utf-8")
    nombres = {n.id for n in ast.walk(ast.parse(texto)) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(ast.parse(texto)) if isinstance(n, ast.Attribute)
    }

    assert not nombres & {
        "procesar_lote",
        "exportar_altura_bc4",
        "crear_exportador_texconv",
        "open",
        "write_bytes",
        "write_text",
    }


def test_el_modulo_no_arrastra_la_cli() -> None:
    """Las superficies lo importan: no puede traer ``cli.py`` (lote, exportador, argparse)."""
    texto = (_RAIZ_DEL_REPO / _MODULO).read_text(encoding="utf-8")
    importados = {n.module or "" for n in ast.walk(ast.parse(texto)) if isinstance(n, ast.ImportFrom)}

    assert not any(m.endswith(".cli") or m.endswith(".lote") or m.endswith(".export") for m in importados)


def test_cargar_perfil_y_hallazgos_son_publicos_en_el_modulo() -> None:
    assert callable(cargar_perfil) and callable(hallazgos_del_perfil)
    assert diagnostico_perfil.__name__.endswith("diagnostico_perfil")
