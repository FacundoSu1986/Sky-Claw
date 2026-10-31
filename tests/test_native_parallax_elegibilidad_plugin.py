"""#834 ítem 3: la elegibilidad mira el INI del plugin de relieve que el overlay del perfil entrega.

Antes, ``SI`` solo miraba la forma de la malla: con ``bAutoEnableParallax = false`` en el INI (el valor con que se distribuye) el plugin no
enciende relieve en NINGUNA forma, y el informe igual decía ``SI``. Ahora:

- un INI legible y APAGADO convierte un ``SI`` en ``NO`` (y nada más: ``NO`` y ``NO_VERIFICADO`` no cambian);
- un INI ausente o que no se puede interpretar NO cambia ningún veredicto, pero se avisa: lo no observado no se da por apagado ni por activo;
- el INI que cuenta es el que GANA en el overlay, como con las mallas;
- la DLL solo se comprueba por presencia y solo genera un aviso.

Los estados del INI se enumeran (``AjusteDelIni``): un estado nuevo rompe el ancla hasta que se decida de qué lado cae.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from sky_claw.local.native_parallax import elegibilidad
from sky_claw.local.native_parallax.cli import CODIGO_OK, main
from sky_claw.local.native_parallax.elegibilidad import EfectoPosible, elegibilidad_como_resultado
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA
from sky_claw.local.native_parallax.plugin_relieve import (
    MAXIMO_DEL_INI,
    AjusteDelIni,
    EstadoDelPlugin,
    interpretar_ini,
)
from tests.test_native_parallax_elegibilidad import (  # noqa: F401  (``mundo`` es un fixture)
    _MALLAS,
    _MOD,
    _P1,
    Mundo,
    _nif,
    mundo,
)

_RUTA_INI = "SKSE/Plugins/AutoParallax.ini"
_RUTA_DLL = "SKSE/Plugins/AutoParallax.dll"


def _ini(valor: str) -> bytes:
    return f"[Experimental]\nbAutoEnableParallax = {valor}\n".encode()


def _escribir_ini(mundo: Mundo, datos: bytes, *, mod: str = _MALLAS) -> None:  # noqa: F811
    """El INI TAL CUAL: ``Mundo.archivo`` le agrega el nombre del mod al contenido, y un INI con basura al final no es el que se quiere probar."""
    mundo.malla(datos, mod=mod, ruta=_RUTA_INI)


# ---------------------------------------------------------------------------
# El defecto que este módulo existe para cerrar
# ---------------------------------------------------------------------------


def test_un_ini_apagado_en_el_overlay_no_deja_pasar_un_si(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    assert (
        mundo.por_ruta(mundo.evaluar())[_P1].efecto is EfectoPosible.SI
    )  # el control: sin INI, la forma elegible da SI
    _escribir_ini(mundo, _ini("false"))

    textura = mundo.por_ruta(mundo.evaluar())[_P1]

    assert textura.efecto is EfectoPosible.NO
    assert "bAutoEnableParallax" in textura.razon and repr(_MALLAS) in textura.razon  # nombra qué mod entrega el INI
    assert textura.mallas == () and textura.n_mallas == 0


def test_un_ini_activo_deja_el_si_como_estaba(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    _escribir_ini(mundo, _ini("true"))

    informe = mundo.evaluar()

    assert mundo.por_ruta(informe)[_P1].efecto is EfectoPosible.SI
    assert informe.plugin.ini is AjusteDelIni.ACTIVO and informe.plugin.capa_del_ini == _MALLAS


# ---------------------------------------------------------------------------
# Qué cuenta como activo, apagado o sin determinar (se enumera, no se muestrea)
# ---------------------------------------------------------------------------

_CASOS_DEL_INI = [
    # --- legibles
    (b"[Experimental]\nbAutoEnableParallax = true\n", AjusteDelIni.ACTIVO),
    (b"[Experimental]\nbAutoEnableParallax = TRUE\n", AjusteDelIni.ACTIVO),
    (b"[Experimental]\nbAutoEnableParallax=1\n", AjusteDelIni.ACTIVO),
    (b"[experimental]\nBAUTOENABLEPARALLAX = True\n", AjusteDelIni.ACTIVO),  # sin distinguir mayúsculas
    (b"\xef\xbb\xbf[Experimental]\nbAutoEnableParallax = true\n", AjusteDelIni.ACTIVO),  # con BOM
    (b"; comentario\n[Experimental]\n# otro\nbAutoEnableParallax = false\n", AjusteDelIni.APAGADO),
    (b"[Experimental]\nbAutoEnableParallax = false\n", AjusteDelIni.APAGADO),
    (b"[Experimental]\nbAutoEnableParallax = 0\n", AjusteDelIni.APAGADO),
    (b"[Otra]\nx = 1\n[Experimental]\nbAutoEnableParallax = false\n", AjusteDelIni.APAGADO),
    # --- no se puede afirmar: nunca se adivina
    (b"", AjusteDelIni.NO_DETERMINADO),
    (b"[Experimental]\n", AjusteDelIni.NO_DETERMINADO),  # sin la clave
    (b"[Otra]\nbAutoEnableParallax = false\n", AjusteDelIni.NO_DETERMINADO),  # la clave en otra sección
    (b"bAutoEnableParallax = false\n", AjusteDelIni.NO_DETERMINADO),  # sin sección
    (b"[Experimental]\nbAutoEnableParallax = yes\n", AjusteDelIni.NO_DETERMINADO),  # no es un booleano claro
    (
        b"[Experimental]\nbAutoEnableParallax = false ; x\n",
        AjusteDelIni.NO_DETERMINADO,
    ),  # comentario en línea: no se supone cómo lo lee
    (b"[Experimental]\nbAutoEnableParallax =\n", AjusteDelIni.NO_DETERMINADO),
    (
        b"[Experimental]\nbAutoEnableParallax = true\nbAutoEnableParallax = false\n",
        AjusteDelIni.NO_DETERMINADO,
    ),  # clave repetida
    (
        b"[Experimental]\nbAutoEnableParallax = true\n[Experimental]\nx = 1\n",
        AjusteDelIni.NO_DETERMINADO,
    ),  # sección repetida
    (b"[Experimental]\nbAutoEnableParallax = false\nMallas", AjusteDelIni.NO_DETERMINADO),  # línea sin «=»
    (b"\xff\xfe\x00\x01", AjusteDelIni.NO_DETERMINADO),  # no es UTF-8
    (
        b"[Experimental]\nbAutoEnableParallax = false\n" + b"#" * MAXIMO_DEL_INI,
        AjusteDelIni.NO_DETERMINADO,
    ),  # demasiado grande
]


@pytest.mark.parametrize(("datos", "esperado"), _CASOS_DEL_INI, ids=range(len(_CASOS_DEL_INI)))
def test_interpretar_ini_enumerado(datos: bytes, esperado: AjusteDelIni) -> None:
    ajuste, detalle = interpretar_ini(datos)

    assert ajuste is esperado
    assert bool(detalle) is (esperado is AjusteDelIni.NO_DETERMINADO)  # el motivo viaja solo cuando no se pudo afirmar


def test_los_casos_del_ini_cubren_los_tres_estados_que_salen_de_un_archivo() -> None:
    assert {esperado for _datos, esperado in _CASOS_DEL_INI} == set(AjusteDelIni) - {
        AjusteDelIni.AUSENTE
    }  # AUSENTE = no hay archivo


# Ancla por igualdad literal de la familia: un estado nuevo obliga a decidir qué hace con un ``SI``.
_QUE_HACE_CADA_ESTADO_CON_UN_SI = {
    AjusteDelIni.ACTIVO: EfectoPosible.SI,
    AjusteDelIni.APAGADO: EfectoPosible.NO,
    AjusteDelIni.AUSENTE: EfectoPosible.SI,  # se avisa, no se cambia: lo no observado no se da por apagado
    AjusteDelIni.NO_DETERMINADO: EfectoPosible.SI,
}


def test_cada_estado_del_ini_tiene_su_decision_sobre_un_si() -> None:
    assert set(_QUE_HACE_CADA_ESTADO_CON_UN_SI) == set(AjusteDelIni)
    for ajuste, efecto in _QUE_HACE_CADA_ESTADO_CON_UN_SI.items():
        estado = EstadoDelPlugin(ajuste, "Mod", True, "")
        assert estado.apaga_el_relieve is (efecto is EfectoPosible.NO), ajuste


@pytest.mark.parametrize(("datos", "esperado"), _CASOS_DEL_INI, ids=range(len(_CASOS_DEL_INI)))
def test_de_punta_a_punta_solo_un_ini_legible_y_apagado_baja_un_si(
    mundo: Mundo,  # noqa: F811
    datos: bytes,
    esperado: AjusteDelIni,
) -> None:
    mundo.malla(_nif())
    _escribir_ini(mundo, datos)

    informe = mundo.evaluar()

    assert informe.plugin.ini is esperado
    efecto = EfectoPosible.NO if esperado is AjusteDelIni.APAGADO else EfectoPosible.SI
    assert mundo.por_ruta(informe)[_P1].efecto is efecto


def test_sin_ningun_ini_el_si_no_cambia_y_se_avisa(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())

    informe = mundo.evaluar()

    assert mundo.por_ruta(informe)[_P1].efecto is EfectoPosible.SI
    assert informe.plugin.ini is AjusteDelIni.AUSENTE and informe.plugin.capa_del_ini is None
    assert any("AutoParallax.ini" in aviso for aviso in informe.plugin.avisos())


# ---------------------------------------------------------------------------
# Un INI apagado solo baja un SI: lo demás no se toca
# ---------------------------------------------------------------------------


def test_un_ini_apagado_no_cambia_un_no_ni_un_no_verificado(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif(tipo_de_shader=3))  # relieve de fábrica: NO por su propia razón
    _escribir_ini(mundo, _ini("false"))
    con_no = mundo.por_ruta(mundo.evaluar())[_P1]
    assert (
        con_no.efecto is EfectoPosible.NO
        and "relieve de fábrica" in con_no.razon
        and "bAutoEnableParallax" not in con_no.razon
    )

    sin_mallas = mundo.mods / _MALLAS / "meshes"
    for archivo in sin_mallas.rglob("*.nif"):
        archivo.unlink()
    sin_referencias = mundo.por_ruta(mundo.evaluar())[_P1]
    assert (
        sin_referencias.efecto is EfectoPosible.NO_VERIFICADO
    )  # sigue sin saberse si una malla dentro de un .bsa la usaría


# ---------------------------------------------------------------------------
# Quién gana
# ---------------------------------------------------------------------------


def test_el_ini_que_cuenta_es_el_del_mod_de_mayor_prioridad(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    (mundo.mods / "Ajustes").mkdir()
    # ``modlist.txt``: la primera línea es la de MAYOR prioridad.
    mundo.modlist("+Ajustes", f"+{_MALLAS}", f"+{_MOD}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}")
    _escribir_ini(mundo, _ini("true"), mod=_MALLAS)
    _escribir_ini(mundo, _ini("false"), mod="Ajustes")

    gana_el_apagado = mundo.evaluar()

    assert gana_el_apagado.plugin.ini is AjusteDelIni.APAGADO and gana_el_apagado.plugin.capa_del_ini == "Ajustes"
    assert mundo.por_ruta(gana_el_apagado)[_P1].efecto is EfectoPosible.NO

    _escribir_ini(mundo, _ini("true"), mod="Ajustes")
    _escribir_ini(mundo, _ini("false"), mod=_MALLAS)

    gana_el_activo = mundo.evaluar()

    assert gana_el_activo.plugin.ini is AjusteDelIni.ACTIVO
    assert mundo.por_ruta(gana_el_activo)[_P1].efecto is EfectoPosible.SI


# ---------------------------------------------------------------------------
# La DLL: presencia y aviso, nunca veredicto
# ---------------------------------------------------------------------------


def test_la_dll_suelta_en_el_perfil_se_detecta_y_su_ausencia_solo_avisa(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    _escribir_ini(mundo, _ini("true"))
    sin_dll = mundo.evaluar()
    assert sin_dll.plugin.dll_presente is False
    assert any("AutoParallax.dll" in aviso for aviso in sin_dll.plugin.avisos())
    assert (
        mundo.por_ruta(sin_dll)[_P1].efecto is EfectoPosible.SI
    )  # puede estar instalada fuera de MO2: no se cambia el veredicto

    mundo.malla(b"MZ", ruta=_RUTA_DLL)
    con_dll = mundo.evaluar()

    assert con_dll.plugin.dll_presente is True
    assert con_dll.plugin.avisos() == ()  # INI activo y DLL a la vista: nada que avisar


# ---------------------------------------------------------------------------
# Si SKSE/Plugins no se puede resolver, la evaluación no se pierde
# ---------------------------------------------------------------------------


def test_si_skse_plugins_no_se_puede_resolver_el_ajuste_queda_sin_determinar_y_la_evaluacion_sigue(
    mundo: Mundo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mundo.malla(_nif())
    _escribir_ini(mundo, _ini("false"))
    monkeypatch.setattr(elegibilidad, "_MAXIMO_DE_ARCHIVOS_EN_PLUGINS", 0)  # un tope inválido: el resolutor lo rechaza

    informe = mundo.evaluar()

    assert informe.plugin.ini is AjusteDelIni.NO_DETERMINADO and informe.plugin.detalle
    assert mundo.por_ruta(informe)[_P1].efecto is EfectoPosible.SI  # no sabemos: no se baja un SI por lo que no se vio


# ---------------------------------------------------------------------------
# Las superficies: una función, un resultado
# ---------------------------------------------------------------------------


def test_el_resultado_de_tool_trae_el_estado_del_plugin_y_sus_avisos(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    _escribir_ini(mundo, _ini("false"))

    resultado = elegibilidad_como_resultado(mods_dir=mundo.mods, data_root=mundo.data, perfil="Default", mod=_MOD)

    assert resultado["success"] is True
    assert resultado["plugin"]["ini"] == "APAGADO" and resultado["plugin"]["capa_del_ini"] == _MALLAS
    assert resultado["plugin"]["dll_presente"] is False
    assert any("apagado" in aviso for aviso in resultado["plugin"]["avisos"])
    assert resultado["conteo"] == {"SI": 0, "NO": 1, "NO_VERIFICADO": 0}


def test_la_cli_imprime_los_avisos_del_plugin(mundo: Mundo) -> None:  # noqa: F811
    mundo.malla(_nif())
    _escribir_ini(mundo, _ini("false"))
    salida = io.StringIO()

    codigo = main(
        ["elegibilidad", "--mod", _MOD, "--mods", str(mundo.mods), "--data-root", str(mundo.data)],
        entorno={},
        fabrica_de_exportador=lambda _ruta: pytest.fail("la elegibilidad no debe pedir texconv"),
        base_de_estado=pathlib.Path(mundo.raiz) / "estado",
        salida=salida,
    )

    texto = salida.getvalue()
    assert codigo == CODIGO_OK
    assert f"[NO] {_P1}" in texto and "bAutoEnableParallax apagado" in texto
    assert "Aviso: el INI del plugin" in texto
