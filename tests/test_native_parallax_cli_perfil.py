"""NP-A0e: el subcomando ``perfil`` de la CLI del generador nativo de parallax (diagnóstico de SOLO lectura).

Qué se verifica: que el subcomando use el resolutor de ganadores de MO2 (``sky_claw/local/mo2/ganadores.py``) para decir,
por cada textura de un mod, quién gana la ruta de entrada (``*_n.dds``) y quién gana la de salida (``*_p.dds``): el caso
H12 de #792 (un mod de control por encima del mod de salida tapa nuestra salida) y su gemelo (otro mod tapa la textura
que se mediría). No genera nada, no necesita ``texconv`` y no deja ningún archivo. El orden del ``modlist.txt`` es el de
MO2: la primera línea habilitada es la de MAYOR prioridad.
"""

from __future__ import annotations

import io
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from sky_claw.local.native_parallax import cli, provider
from sky_claw.local.native_parallax.cli import (
    CODIGO_CON_TAPADOS,
    CODIGO_DE_USO,
    CODIGO_OK,
    construir_parser,
    main,
)
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA

_CARPETA = "textures/architecture/whiterun"
_ENTRADA = f"{_CARPETA}/muro01_n.dds"
_SALIDA = f"{_CARPETA}/muro01_p.dds"
_MOD = "Texturas"
_CONTROL = "SkyClaw-Control-Visual"


@dataclass
class Mundo:
    """Una instancia MO2 mínima en disco: ``mods/``, ``data/`` (con ``overwrite``) y el ``modlist.txt`` de un perfil."""

    raiz: pathlib.Path

    @property
    def mods(self) -> pathlib.Path:
        return self.raiz / "mods"

    @property
    def data(self) -> pathlib.Path:
        return self.raiz / "data"

    def archivo(self, mod: str, ruta: str) -> pathlib.Path:
        destino = self.mods / mod / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"contenido de " + mod.encode() + b" en " + ruta.encode())
        return destino

    def mod_vacio(self, mod: str) -> None:
        """Un mod que existe en disco sin archivos (el mod de salida antes de la primera corrida)."""
        (self.mods / mod).mkdir(parents=True, exist_ok=True)

    def overwrite(self, ruta: str) -> None:
        destino = self.data / "overwrite" / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"overwrite")

    def modlist(self, *lineas: str, perfil: str = "Default") -> pathlib.Path:
        """``lineas`` en el orden del archivo de MO2: la primera es la de MAYOR prioridad."""
        ruta = self.data / "profiles" / perfil / "modlist.txt"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# generado\n" + "\n".join(lineas) + "\n", encoding="utf-8")
        return ruta


@pytest.fixture
def mundo(tmp_path: pathlib.Path) -> Mundo:
    m = Mundo(tmp_path)
    m.mods.mkdir()
    m.data.mkdir()
    return m


def _perfil(mundo: Mundo, *extra: str, mod: str = _MOD, entorno: dict[str, str] | None = None) -> tuple[int, str]:
    def no_debe_pedir_texconv(_ruta: pathlib.Path) -> Any:  # pragma: no cover - solo corre si el comando lo pide
        raise AssertionError("el diagnóstico de perfil no debe pedir texconv")

    salida = io.StringIO()
    codigo = main(
        [
            "perfil",
            "--mod",
            mod,
            "--mods",
            str(mundo.mods),
            "--data-root",
            str(mundo.data),
            *extra,
        ],
        entorno={} if entorno is None else entorno,
        fabrica_de_exportador=no_debe_pedir_texconv,
        base_de_estado=mundo.raiz / "estado",
        salida=salida,
    )
    return codigo, salida.getvalue()


# ---------------------------------------------------------------------------
# El caso sano y los dos tapados
# ---------------------------------------------------------------------------


def test_sin_nadie_que_tape_el_diagnostico_sale_en_cero_y_no_marca_nada(mundo: Mundo) -> None:
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_OK
    assert "TAPADA" not in texto
    assert "1 textura" in texto
    assert "Sin texturas tapadas" in texto


def test_h12_un_mod_de_control_por_encima_de_la_salida_tapa_la_salida(mundo: Mundo) -> None:
    """#792 H12: el control (línea 1 = mayor prioridad) está por ENCIMA del mod de salida y gana la ruta ``_p``."""
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_CONTROL, _SALIDA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_CON_TAPADOS
    assert "SALIDA TAPADA" in texto
    assert _SALIDA in texto
    assert _CONTROL in texto
    assert "1 hallazgo." in texto
    assert "no prueba lo que se ve en el juego" in texto
    assert "Sin texturas tapadas" not in texto


def test_si_la_salida_esta_por_encima_del_control_la_salida_gana_y_no_se_marca(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_CONTROL, _SALIDA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_CONTROL}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_OK
    assert "TAPADA" not in texto


def test_overwrite_gana_a_todo_mod_y_tapa_la_salida(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.overwrite(_SALIDA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_CON_TAPADOS
    assert "SALIDA TAPADA" in texto
    assert "overwrite" in texto


def test_otro_mod_por_encima_tapa_la_textura_de_entrada_que_se_mediria(mundo: Mundo) -> None:
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo("Retexture", _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_CON_TAPADOS
    assert "ENTRADA TAPADA" in texto
    assert _ENTRADA in texto
    assert "Retexture" in texto


def test_una_textura_oculta_con_mohidden_no_esta_en_el_overlay_y_se_dice(mundo: Mundo) -> None:
    """El proveedor ve el archivo en disco; MO2 no lo entrega si su carpeta es ``*.mohidden``: se marca, no se calla."""
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, "textures/oculta.mohidden/muro_n.dds")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_CON_TAPADOS
    assert "ENTRADA TAPADA" not in texto  # no la tapa nadie: no está
    assert "FUERA DEL OVERLAY" in texto


def test_una_version_previa_de_la_salida_en_una_capa_inferior_no_es_un_tapado(mundo: Mundo) -> None:
    """Quien provee ``_p`` pero está por DEBAJO del mod de salida no la tapa: la salida lo reemplazaría al generarse."""
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo("Base", _SALIDA)
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}", "+Base")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_OK
    assert "TAPADA" not in texto


def test_el_mod_de_salida_no_habilitado_se_marca_porque_su_salida_no_se_cargaria(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_CON_TAPADOS
    assert "SALIDA NO HABILITADA" in texto
    assert NOMBRE_DEL_MOD_DE_SALIDA in texto


def test_el_orden_de_prioridad_es_el_de_mo2_la_primera_linea_gana(mundo: Mundo) -> None:
    """Invertir el modlist invierte el veredicto: ancla del sentido de las prioridades."""
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_CONTROL, _SALIDA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{_MOD}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_CONTROL}")

    codigo, _texto = _perfil(mundo)

    assert codigo == CODIGO_OK


# ---------------------------------------------------------------------------
# Falla cerrado: lo que no se puede afirmar es un error de uso, no un «sin problemas»
# ---------------------------------------------------------------------------


def test_un_mod_que_no_esta_habilitado_en_el_perfil_es_un_error_de_uso(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert _MOD in texto


def test_revisar_el_propio_mod_de_salida_es_un_error_de_uso(mundo: Mundo) -> None:
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}")

    codigo, texto = _perfil(mundo, mod=NOMBRE_DEL_MOD_DE_SALIDA)

    assert codigo == CODIGO_DE_USO
    assert "mod de salida" in texto


def test_un_modlist_inexistente_es_un_error_de_uso_y_no_un_perfil_vacio(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "modlist" in texto


def test_un_modlist_con_una_linea_invalida_es_un_error_de_uso(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    ruta = mundo.modlist(f"+{_MOD}")
    ruta.write_text("esto no es una linea valida\n", encoding="utf-8")

    codigo, _texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO


def test_el_mod_de_salida_habilitado_pero_sin_carpeta_falla_cerrado_y_se_nombra(mundo: Mundo) -> None:
    """Antes de la primera corrida el modlist puede listar la salida sin que exista: no es un «sin tapados»."""
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert NOMBRE_DEL_MOD_DE_SALIDA in texto


def test_una_textura_ilegible_hace_fallar_el_diagnostico_en_vez_de_darlo_por_limpio(
    mundo: Mundo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo que no se pudo leer no se puede dar por «no tapado»: sin verlo no se sabe qué gana (revisión de Codex en #809).

    La textura ilegible está además tapada por otro mod, así que el diagnóstico completo marcaría un tapado; si la omisión se
    descartara, el resultado sería «Sin texturas tapadas» con código 0.
    """
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo("Retexture", _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Retexture", f"+{_MOD}")
    real = provider.huella_acotada

    def huella(ruta: pathlib.Path, tamano: int) -> str:
        if ruta.parts[-len(_ENTRADA.split("/")) - 1] == _MOD:
            raise PermissionError("bloqueado por el antivirus")
        return real(ruta, tamano)

    monkeypatch.setattr(provider, "huella_acotada", huella)

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "incompleta" in texto
    assert "Sin texturas tapadas" not in texto


def test_un_mod_habilitado_sin_carpeta_es_un_error_de_uso(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", "+Fantasma", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "Fantasma" in texto


def test_un_mods_inexistente_es_un_error_de_uso_que_nombra_mods_dir(mundo: Mundo, tmp_path: pathlib.Path) -> None:
    """El perfil sí se lee (el modlist existe): lo que falla es la validación de ``mods_dir`` del resolutor."""
    mundo.archivo(_MOD, _ENTRADA)
    mundo.modlist(f"+{_MOD}")
    salida = io.StringIO()

    codigo = main(
        ["perfil", "--mod", _MOD, "--mods", str(tmp_path / "no_existe"), "--data-root", str(mundo.data)],
        entorno={},
        salida=salida,
    )

    assert codigo == CODIGO_DE_USO
    assert "mods_dir" in salida.getvalue()


def test_un_data_root_inexistente_es_un_error_de_uso_porque_no_hay_modlist(
    mundo: Mundo, tmp_path: pathlib.Path
) -> None:
    """El perfil se lee de ``--data-root``, así que un root inexistente falla ahí, antes de llegar al resolutor."""
    mundo.archivo(_MOD, _ENTRADA)
    salida = io.StringIO()

    codigo = main(
        ["perfil", "--mod", _MOD, "--mods", str(mundo.mods), "--data-root", str(tmp_path / "no_existe")],
        entorno={},
        salida=salida,
    )

    assert codigo == CODIGO_DE_USO
    assert "modlist" in salida.getvalue()


def test_un_mod_sin_texturas_es_un_error_de_uso_y_no_un_sin_problemas(mundo: Mundo) -> None:
    (mundo.mods / _MOD).mkdir()
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, _texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO


def test_un_mod_con_textures_pero_sin_ninguna_normal_es_un_error_de_uso_como_en_generar(mundo: Mundo) -> None:
    """Hermano de ``generar``, que rechaza «ningún *_n.dds procesable»: revisar cero texturas no es «sin texturas tapadas»."""
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, f"{_CARPETA}/muro01_d.dds")  # tiene textures/, pero ningún *_n.dds
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    codigo, texto = _perfil(mundo)

    assert codigo == CODIGO_DE_USO
    assert "_n.dds" in texto
    assert "Sin texturas tapadas" not in texto


def test_la_ayuda_del_subcomando_documenta_los_codigos_de_salida() -> None:
    ayuda = _subcomandos()["perfil"].format_help()

    for codigo in ("0", "2", "4"):
        assert f"{codigo} " in ayuda
    assert "tapad" in ayuda


def test_el_perfil_se_elige_con_el_argumento_perfil(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_CONTROL, _SALIDA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")  # Default: sano
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}", perfil="Pruebas")  # Pruebas: tapado

    sano, _ = _perfil(mundo)
    tapado, texto = _perfil(mundo, "--perfil", "Pruebas")

    assert sano == CODIGO_OK
    assert tapado == CODIGO_CON_TAPADOS
    assert "Pruebas" in texto


# ---------------------------------------------------------------------------
# Solo lectura y sin texconv
# ---------------------------------------------------------------------------


def _foto(raiz: pathlib.Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None) for p in sorted(raiz.rglob("*"))}


def test_el_diagnostico_no_escribe_nada_ni_pide_texconv_ni_crea_la_base_de_estado(mundo: Mundo) -> None:
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_CONTROL, _SALIDA)
    mundo.archivo(NOMBRE_DEL_MOD_DE_SALIDA, _SALIDA)
    mundo.modlist(f"+{_CONTROL}", f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")
    antes = _foto(mundo.raiz)

    codigo, _texto = _perfil(mundo, entorno={})  # sin variable de texconv: tendría que fallar si lo pidiera

    assert codigo == CODIGO_CON_TAPADOS
    assert _foto(mundo.raiz) == antes  # ni un archivo nuevo (informe, estado, lock) ni uno modificado
    assert not (mundo.raiz / "estado").exists()


# ---------------------------------------------------------------------------
# Anclas que ENUMERAN (el hermano que nadie tocó)
# ---------------------------------------------------------------------------


def _subcomandos() -> dict[str, Any]:
    parser = construir_parser()
    for accion in parser._actions:  # noqa: SLF001 - no hay API pública para listar subcomandos
        if hasattr(accion, "choices") and isinstance(accion.choices, dict) and "generar" in accion.choices:
            return dict(accion.choices)
    raise AssertionError("el parser no tiene subcomandos")


def test_los_subcomandos_estan_congelados_y_cada_uno_declara_si_pide_texconv() -> None:
    """Un subcomando nuevo rompe este test hasta que declare si necesita ``texconv``: ``main`` solo lo exige a los que
    escriben o exportan. Si ``perfil`` o ``elegibilidad`` (diagnósticos de solo lectura) empezaran a escribir, tendrían que moverse de grupo."""
    subcomandos = _subcomandos()
    pide_texconv = {nombre for nombre, p in subcomandos.items() if "--texconv" in p._option_string_actions}  # noqa: SLF001

    assert set(subcomandos) == {"autoprueba", "elegibilidad", "generar", "perfil"}
    assert pide_texconv == {"autoprueba", "generar"}


def test_perfil_no_nombra_el_lote_ni_el_exportador_para_no_entrar_al_censo_de_escritores() -> None:
    """``procesar_lote`` y ``exportar_altura_bc4`` tienen un censo de llamadores (``cli.py`` es el único): el diagnóstico
    no debe sumar ninguna llamada nueva a ellos."""
    import ast

    arbol = ast.parse(pathlib.Path(cli.__file__).read_text(encoding="utf-8"))
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "_perfil")
    nombres = {n.id for n in ast.walk(funcion) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(funcion) if isinstance(n, ast.Attribute)
    }

    assert not nombres & {"procesar_lote", "exportar_altura_bc4", "crear_exportador_texconv"}


def _lineas(texto: str) -> Sequence[str]:
    return texto.splitlines()


def test_la_salida_resume_cuantas_texturas_se_revisaron(mundo: Mundo) -> None:
    mundo.mod_vacio(NOMBRE_DEL_MOD_DE_SALIDA)
    mundo.archivo(_MOD, _ENTRADA)
    mundo.archivo(_MOD, f"{_CARPETA}/muro02_n.dds")
    mundo.modlist(f"+{NOMBRE_DEL_MOD_DE_SALIDA}", f"+{_MOD}")

    _codigo, texto = _perfil(mundo)

    assert any("2 texturas" in linea for linea in _lineas(texto))
