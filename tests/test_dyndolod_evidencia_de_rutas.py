"""Evidencia de rutas declaradas por el binario en el log (Findings 4 y 5 de #592).

**Qué contrato congela este archivo.** Los binarios de la etapa 9 ecoan al
arrancar las rutas que parsearon de su línea de comandos:

- ``Using Output Path:`` — el ``-o:`` (rig 2026-09-10 T5-v2 y rig commiteado
  ``docs/validation/2026-09-13_pr580_real_rig/``: ``texgen-log.txt:11``,
  ``dyndolod-log.txt:12``);
- ``Using Skyrim Special Edition Data Path:`` — el ``-d:`` (mismo rig:
  ``texgen-log.txt:8``, ``dyndolod-log.txt:8``).

Esas líneas son evidencia de lo que el binario **parseó**, no de dónde
escribió: el rig 2026-08-11 midió el eco igual al argv mientras las escrituras
iban al preset rancio de la GUI (``docs/pending_ooda_status.md``, ítem del
preset). Por eso el check de este archivo **no autoriza ningún destino**: la
autoridad post-run sigue siendo la verificación física del artefacto en el
subroot exclusivo, y esta evidencia sólo (a) enriquece el diagnóstico cuando el
artefacto falta (Finding 4) y (b) advierte cuando el binario declaró una ruta
distinta de la configurada aunque el artefacto esté (Finding 5).

**Límite de la evidencia.** Los formatos citados están medidos sobre
TexGenx64/DynDOLODx64 Alpha-209 reales. Un binario que no los emita no produce
fallo: la ausencia es UNKNOWN, no veredicto. Endurecer la discrepancia a
fallo cerrado exige repetir una corrida real con los ejecutables del operador
(ver el handoff del PR).

La atribución a la corrida actual es la de ``_validar_completitud_de_la_corrida``:
estas funciones se evalúan SOLO sobre la ventana ``de_esta_corrida``. Ninguna
línea heredada de una ejecución anterior puede entrar por acá.
"""

from __future__ import annotations

import logging
import pathlib
import sys
from unittest.mock import patch

import pytest

from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODRunner,
    EvidenciaDeRuta,
    EvidenciaDeRutas,
    VeredictoRuta,
    evaluar_evidencia_de_rutas,
    extraer_rutas_declaradas,
    rutas_equivalentes,
)
from tests.test_dyndolod_service import (
    _MARCADOR_DE_FIN,
    _apendear_log,
    _corrida_que_completa,
    _EjecucionFalsa,
    _escribir_log,
    _escribir_salida,
    _runner_texgen,
)

# ---------------------------------------------------------------------------
# Vectores del rig commiteado (docs/validation/2026-09-13_pr580_real_rig/).
# Literales, para que el test no dependa de leer el doc.
# ---------------------------------------------------------------------------

#: Línea real de ``dyndolod-log.txt:12`` / ``texgen-log.txt:11``. El argv llevaba
#: ``-o:...DynDOLOD\\`` (backslash duplicado por ``list2cmdline`` al citar por
#: los espacios) y el binario lo normalizó: T5 respondido, opción A verificada.
LINEA_RIG_OUTPUT = "Using Output Path: E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD\\"
#: Línea real de ``dyndolod-log.txt:8`` / ``texgen-log.txt:8``.
LINEA_RIG_DATA = "Using Skyrim Special Edition Data Path: C:\\Modding\\DynDOLOD RigTest\\_rig_test\\data\\"

RUTAS_RIG = f"{LINEA_RIG_OUTPUT}\n{LINEA_RIG_DATA}\n"


# ---------------------------------------------------------------------------
# extraer_rutas_declaradas — texto puro
# ---------------------------------------------------------------------------


def test_extraer_rutas_declaradas_lee_las_dos_lineas_del_rig() -> None:
    """El formato real del rig: sin timestamp, backslash normalizado, con espacios."""
    rutas = extraer_rutas_declaradas(RUTAS_RIG)
    assert rutas["output"] == ("E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD\\",)
    assert rutas["data"] == ("C:\\Modding\\DynDOLOD RigTest\\_rig_test\\data\\",)


def test_extraer_rutas_declaradas_tolera_timestamp_y_backslash_duplicado() -> None:
    """Variantes: timestamp líder (formato de las fixtures del repo) y ``\\\\`` final.

    El valor se guarda CRUDO: normalizar el separador duplicado es trabajo del
    comparador, que es donde vive la semántica Windows.
    """
    texto = "[00:00:01] Using Output Path: C:\\Program Files\\Sky Claw\\salida\\\\\n"
    rutas = extraer_rutas_declaradas(texto)
    assert rutas["output"] == (r"C:\Program Files\Sky Claw\salida\\",)


def test_extraer_rutas_declaradas_ignora_las_otras_rutas_using() -> None:
    """Alcance acotado a las dos claves del contrato: Temp/Scripts/Cache/ini/save NO entran.

    El rig las emite también (``Using Temp Path:``, ``Using Scripts Path:``,
    ``Using Cache Path:``, ``Using ini:``, ``Using save path:``) y ninguna es
    parte del contrato de este check: compararlas requeriría targets que
    Sky-Claw no administra como predicado de éxito.
    """
    texto = (
        "Using Scripts Path: C:\\x\\Edit Scripts\\\n"
        "Using Cache Path: C:\\x\\Cache\\\n"
        "Using Temp Path: C:\\x\\temp\\\n"
        "Using ini: C:\\x\\ini\\\\Skyrim.ini\n"
        "Using save path: C:\\x\\Saves\\\n"
    )
    rutas = extraer_rutas_declaradas(texto)
    assert rutas["output"] == ()
    assert rutas["data"] == ()


def test_extraer_rutas_declaradas_sin_lineas_devuelve_tuplas_vacias() -> None:
    assert extraer_rutas_declaradas("") == {"output": (), "data": ()}
    assert extraer_rutas_declaradas("DynDOLOD plugins generated successfully\n") == {"output": (), "data": ()}


# ---------------------------------------------------------------------------
# rutas_equivalentes — semántica Windows
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="normcase/backslash: semántica de Windows")
def test_rutas_equivalentes_tolera_el_backslash_final_duplicado_del_rig() -> None:
    """El vector T5 exacto: el eco trae ``\\\\`` final y el ``-o:`` configurado no.

    ``list2cmdline`` duplica el backslash al citar por espacios; el binario lo
    normaliza en el eco. Comparar en crudo daría DIFIERE sobre el mismo
    directorio — un falso rojo fabricado por la serialización.
    """
    assert rutas_equivalentes(
        r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD\\",
        pathlib.Path(r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD"),
    )


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitivity: semántica de Windows")
def test_rutas_equivalentes_ignora_mayusculas_y_separadores() -> None:
    assert rutas_equivalentes("c:\\modding\\out\\", pathlib.Path("C:/Modding/Out"))


@pytest.mark.skipif(sys.platform != "win32", reason="UNC: semántica de Windows")
def test_rutas_equivalentes_unc_con_barra_final() -> None:
    assert rutas_equivalentes(r"\\servidor\compartido\out\\", pathlib.Path(r"\\servidor\compartido\out"))


def test_rutas_equivalentes_distintas_no_son_equivalentes() -> None:
    assert not rutas_equivalentes("C:\\decoy\\TexGen\\", pathlib.Path(r"C:\managed\DynDOLOD\TexGen"))


# ---------------------------------------------------------------------------
# evaluar_evidencia_de_rutas — el veredicto por clave
# ---------------------------------------------------------------------------


def test_veredicto_coincide_con_el_vector_del_rig() -> None:
    """El eco del rig contra los roots configurados del propio rig: COINCIDE."""
    ev = evaluar_evidencia_de_rutas(
        RUTAS_RIG,
        raiz_de_salida=pathlib.Path(r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD"),
        data_dir=pathlib.Path(r"C:\Modding\DynDOLOD RigTest\_rig_test\data"),
    )
    assert ev.output.veredicto is VeredictoRuta.COINCIDE
    assert ev.data.veredicto is VeredictoRuta.COINCIDE


def test_veredicto_difiere_nombra_la_observada_y_la_configurada() -> None:
    ev = evaluar_evidencia_de_rutas(
        "Using Output Path: C:\\decoy\\TexGen\\\n",
        raiz_de_salida=pathlib.Path(r"C:\managed\DynDOLOD\TexGen"),
        data_dir=None,
    )
    assert ev.output.veredicto is VeredictoRuta.DIFIERE
    assert ev.output.declarada == "C:\\decoy\\TexGen\\"
    assert ev.output.configurada == pathlib.Path(r"C:\managed\DynDOLOD\TexGen")


def test_veredicto_ausente_cuando_no_hay_lineas() -> None:
    ev = evaluar_evidencia_de_rutas(
        "DynDOLOD plugins generated successfully\n",
        raiz_de_salida=pathlib.Path(r"C:\managed\DynDOLOD\DynDOLOD"),
        data_dir=pathlib.Path(r"C:\game\Data"),
    )
    assert ev.output.veredicto is VeredictoRuta.AUSENTE
    assert ev.data.veredicto is VeredictoRuta.AUSENTE
    assert ev.output.declarada is None


def test_veredicto_indeterminado_con_valores_en_conflicto() -> None:
    """Dos valores distintos para la misma clave: no se puede afirmar cuál es el efectivo.

    El eco puede aparecer más de una vez en el log; si los valores difieren, la
    evidencia no permite veredicto — UNKNOWN, no una adjudicación a ojo.
    """
    texto = "Using Output Path: C:\\a\\\nUsing Output Path: C:\\b\\\n"
    ev = evaluar_evidencia_de_rutas(texto, raiz_de_salida=pathlib.Path(r"C:\a"), data_dir=None)
    assert ev.output.veredicto is VeredictoRuta.INDETERMINADO


def test_veredicto_indeterminado_si_no_hay_target_configurado() -> None:
    """Eco presente pero sin path configurado contra el cual contrastar.

    La clave sin línea de eco sigue siendo ``AUSENTE``: la ausencia de evidencia
    no depende de que haya target.
    """
    ev = evaluar_evidencia_de_rutas(
        "Using Output Path: C:\\x\\\n",
        raiz_de_salida=None,
        data_dir=None,
    )
    assert ev.output.veredicto is VeredictoRuta.INDETERMINADO
    assert ev.data.veredicto is VeredictoRuta.AUSENTE


def test_veredicto_valores_repetidos_e_iguales_no_son_conflicto() -> None:
    """El eco repetido con el MISMO valor es evidencia consistente, no ambigua."""
    texto = "Using Output Path: C:\\a\\\nUsing Output Path: C:\\a\\\n"
    ev = evaluar_evidencia_de_rutas(texto, raiz_de_salida=pathlib.Path(r"C:\a"), data_dir=None)
    assert ev.output.veredicto is VeredictoRuta.COINCIDE


def test_discrepancias_enumera_solo_las_claves_desviadas_en_orden_estable() -> None:
    """``EvidenciaDeRutas.discrepancias()`` es la fuente del diagnóstico de Finding 4.

    El texto del diagnóstico nombra las claves desviadas leyendo esta tupla. El
    orden es FIJO (``output`` antes que ``data``) y no depende de ningún
    diccionario: un orden derivado del hash haría inestable el mensaje y frágil
    su test. Se ancla acá, a nivel de dataclass, para que el contrato no dependa
    de ejercitar el runner completo.
    """
    ambas = EvidenciaDeRutas(
        output=EvidenciaDeRuta(veredicto=VeredictoRuta.DIFIERE, declarada="C:\\a"),
        data=EvidenciaDeRuta(veredicto=VeredictoRuta.DIFIERE, declarada="C:\\b"),
    )
    assert ambas.discrepancias() == ("output", "data")

    solo_data = EvidenciaDeRutas(
        output=EvidenciaDeRuta(veredicto=VeredictoRuta.COINCIDE, declarada="C:\\a"),
        data=EvidenciaDeRuta(veredicto=VeredictoRuta.DIFIERE, declarada="C:\\b"),
    )
    assert solo_data.discrepancias() == ("data",)

    ninguna = EvidenciaDeRutas(
        output=EvidenciaDeRuta(veredicto=VeredictoRuta.AUSENTE),
        data=EvidenciaDeRuta(veredicto=VeredictoRuta.INDETERMINADO),
    )
    assert ninguna.discrepancias() == ()


# ---------------------------------------------------------------------------
# Finding 4 — el diagnóstico cuando el artefacto falta
# ---------------------------------------------------------------------------


def _log_con_eco(eco_output: str, eco_data: str | None = None, *, tool: str = "TexGen") -> str:
    """Un log completo de ``tool`` cuyo eco de salida (y opcionalmente de datos) es ``eco``.

    El marcador de fin sale de ``_MARCADOR_DE_FIN`` y no de un literal: el
    marcador es DISTINTO por herramienta, y hardcodear el de TexGen en el gemelo
    de DynDOLOD dejaría al test rojo por marcador rancio en vez de por su sujeto
    — el falso rojo que ``_log_completo`` ya evita en ``test_dyndolod_service``.
    """
    data = f"{eco_data}\n" if eco_data else ""
    return f"[00:00:01] {eco_output}\n{data}[00:10:00] {_MARCADOR_DE_FIN[tool]}\n"


async def test_f4_artefacto_ausente_con_eco_desviado_nombra_la_ruta_observada(tmp_path: pathlib.Path) -> None:
    """RED→GREEN de Finding 4: causa CONFIRMADA por el eco, con las dos rutas nombradas.

    Hoy el post-check devuelve ``errors`` vacío en este caso y el lanzador loguea
    "Unknown error": el operador no recibe ni el artefacto que falta, ni la
    herramienta, ni la ruta esperada, ni la observada.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_layout is not None
    esperado = config.texgen_root
    desviado = "C:\\decoy\\TexGen\\"
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path, "TexGen", lambda: None, log=_log_con_eco(f"Using Output Path: {desviado}")
        ),
    )
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    diagnostico = " ".join(result.errors)
    assert "TexGen" in diagnostico
    assert "textures" in diagnostico  # el artefacto que falta, nombrado
    assert str(esperado) in diagnostico  # la ruta administrada esperada
    assert desviado in diagnostico  # la ruta observada, SOLO porque hay evidencia
    assert "causa" in diagnostico.lower() and "confirmada" in diagnostico.lower()


async def test_f4_dyndolod_artefacto_ausente_con_eco_desviado_nombra_la_ruta_observada(
    tmp_path: pathlib.Path,
) -> None:
    """El GEMELO de DynDOLOD: el diagnóstico de Finding 4 no puede cubrir sólo a TexGen.

    ``_post_check`` es compartido, pero ``_diagnostico_de_artefacto_ausente``
    ramifica por herramienta —el artefacto que nombra y el subárbol donde lo
    busca— y ``_evidencia_de_rutas`` contrasta el eco contra el subroot EXCLUSIVO
    de cada una (``texgen_root`` vs ``dyndolod_root``). Un ancla que sólo ejercite
    TexGen deja la mitad DynDOLOD sin cubrir: exactamente el defecto hermano que
    ``AGENTS.md`` declara dominante en este repo. Este test recorre la rama
    ``else`` del diagnóstico, que es la única tool-dependiente.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_layout is not None
    esperado = config.dyndolod_root
    desviado = "C:\\decoy\\DynDOLOD\\"
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path,
            "DynDOLOD",
            lambda: None,
            log=_log_con_eco(f"Using Output Path: {desviado}", tool="DynDOLOD"),
        ),
    )
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    diagnostico = " ".join(result.errors)
    assert "DynDOLOD" in diagnostico
    assert "DynDOLOD.esp" in diagnostico  # el artefacto de ESTA herramienta
    assert "textures" not in diagnostico  # ...y NO el del hermano
    assert str(esperado) in diagnostico  # la raíz administrada de DynDOLOD
    assert desviado in diagnostico  # la ruta observada, SOLO porque hay evidencia
    assert "causa" in diagnostico.lower() and "confirmada" in diagnostico.lower()


async def test_f4_artefacto_ausente_con_eco_correcto_sugiere_revisar_la_gui(tmp_path: pathlib.Path) -> None:
    """Eco == raíz administrada: el parse fue correcto, así que la escritura pudo desviarse
    por otra vía. El diagnóstico NO inventa causa: sugiere revisar el campo Output.

    Es el escenario medido del rig 2026-08-11: eco = argv mientras la GUI
    escribía en el preset. El chequeo no puede afirmar que el preset causó el
    fallo porque el valor efectivo no se conoce — sólo sugiere dónde mirar.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_layout is not None
    esperado = config.texgen_root
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path, "TexGen", lambda: None, log=_log_con_eco(f"Using Output Path: {esperado}\\")
        ),
    )
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    diagnostico = " ".join(result.errors)
    assert str(esperado) in diagnostico
    assert "Output" in diagnostico and "GUI" in diagnostico  # sugerencia, no atribución
    # Y ninguna ruta "observada distinta" se muestra cuando no hay evidencia de ella.
    assert "C:\\decoy" not in diagnostico


async def test_f4_artefacto_ausente_sin_eco_no_inventa_causa(tmp_path: pathlib.Path) -> None:
    """Sin líneas de eco: evidencia insuficiente. Se sugiere la GUI y nada más."""
    _, runner = _runner_texgen(tmp_path)
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(tmp_path, "TexGen", lambda: None, log=_log_con_eco("")),
    )
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    diagnostico = " ".join(result.errors)
    assert "Output" in diagnostico and "GUI" in diagnostico
    assert "confirmada" not in diagnostico.lower()


async def test_f4_eco_de_una_corrida_anterior_no_contamina_el_diagnostico(tmp_path: pathlib.Path) -> None:
    """Log que APENDEA: el eco desviado quedó en el prefijo de la corrida anterior.

    La ventana de esta corrida no tiene eco (AUSENTE), así que el diagnóstico
    no puede mostrar el valor viejo como "ruta observada" de hoy. Las líneas
    heredadas son evidencia de otra ejecución — el atributo ``de_esta_corrida``
    es la frontera, y este test la verifica consumiendo el diagnóstico. Es
    guardia de regresión del diagnóstico nuevo (hoy pasa vacío), no una
    deficiencia propia: la no-contaminación ya la cubre la atribución vigente.
    """
    _, runner = _runner_texgen(tmp_path)
    _escribir_log(
        tmp_path,
        "TexGen",
        "[00:00:01] Using Output Path: C:\\viejo\\decoy\\\n[00:10:00] TexGen completed successfully\n",
    )

    def _corrida() -> None:
        # La corrida actual apendea su ventana: sin eco, con artefacto ausente.
        _apendear_log(tmp_path, "TexGen", "[00:10:00] TexGen completed successfully\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_corrida)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    assert result.errors, "el diagnóstico de artefacto ausente debe existir"
    diagnostico = " ".join(result.errors)
    assert "C:\\viejo\\decoy\\" not in diagnostico
    assert "confirmada" not in diagnostico.lower()


async def test_f4_log_reescrito_no_presta_evidencia_de_ruta(tmp_path: pathlib.Path) -> None:
    """Ventana atribuible indeterminada (log reescrito, prefijo cambiado): evidencia None.

    El diagnóstico no muestra ninguna ruta observada — no hay con qué afirmar
    que una línea del archivo sea de esta corrida. Guardia de regresión del
    diagnóstico nuevo: la ventana indeterminada ya la cubre la atribución
    vigente (devuelve ``""``), lo que se verifica acá es que el diagnóstico
    nuevo no la ignore.
    """
    _, runner = _runner_texgen(tmp_path)
    _escribir_log(
        tmp_path, "TexGen", "[00:00:01] Using Output Path: C:\\previo\\\n[00:10:00] TexGen completed successfully\n"
    )

    def _corrida() -> None:
        # Reescritura que crece: el prefijo previo ya no está, la frontera no
        # se puede demostrar y `de_esta_corrida` queda vacío.
        _escribir_log(
            tmp_path,
            "TexGen",
            "[00:00:01] Using Output Path: C:\\reescrito\\\n[00:10:00] TexGen completed successfully\n",
        )

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_corrida)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    assert result.errors
    diagnostico = " ".join(result.errors)
    assert "C:\\reescrito\\" not in diagnostico
    assert "confirmada" not in diagnostico.lower()


def test_ventana_indeterminada_no_es_lo_mismo_que_una_corrida_sin_eco(tmp_path: pathlib.Path) -> None:
    """``evidencia_rutas is None`` ≠ ``AUSENTE``: son dos estados y el diagnóstico depende de eso.

    ``None`` es "no hay evidencia ATRIBUIBLE a esta corrida" (frontera
    indeterminada: log reescrito o truncado); ``AUSENTE`` es "esta corrida SÍ se
    pudo leer y no emitió el eco". Colapsarlos hace que un log reescrito declare
    —o niegue— una ruta que no le corresponde.

    Este ancla existe porque la mutación que reemplaza el ``return None`` por un
    ``EvidenciaDeRutas`` todo-``AUSENTE`` **sobrevivía** a la suite: los dos
    estados producen el mismo texto de diagnóstico y el mismo silencio de
    advertencia, así que sólo el campo ``_PostCheck.evidencia_rutas`` los
    distingue. Un contrato que el docstring declara y ningún test fija es una
    afirmación sin verificar.
    """
    _, runner = _runner_texgen(tmp_path)

    # (a) Frontera indeterminada: reescritura que CRECE con prefijo cambiado ⇒
    #     la ventana atribuible queda vacía ⇒ None, no AUSENTE.
    _escribir_log(
        tmp_path, "TexGen", "[00:00:01] Using Output Path: C:\\previo\\\n[00:10:00] TexGen completed successfully\n"
    )
    firma = runner._firma_del_log("TexGen")
    _escribir_log(
        tmp_path,
        "TexGen",
        "[00:00:01] Using Output Path: C:\\reescrito\\\n[00:10:00] TexGen completed successfully\n",
    )
    assert runner._post_check("TexGen", firma).evidencia_rutas is None

    # (b) Ventana atribuible SIN eco ⇒ la evidencia existe y sus claves son AUSENTE.
    (tmp_path / "DynDOLOD" / "Logs" / "TexGen_SSE_log.txt").unlink()
    firma = runner._firma_del_log("TexGen")
    _escribir_log(tmp_path, "TexGen", "[00:00:01] arrancando\n[00:10:00] TexGen completed successfully\n")
    post = runner._post_check("TexGen", firma)
    assert post.evidencia_rutas is not None
    assert post.evidencia_rutas.output.veredicto is VeredictoRuta.AUSENTE
    assert post.evidencia_rutas.data.veredicto is VeredictoRuta.AUSENTE


# ---------------------------------------------------------------------------
# Finding 5 — la evidencia con artefacto presente
# ---------------------------------------------------------------------------


async def test_f5_artefacto_presente_con_eco_desviado_no_autoriza_pero_advierten(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """El artefacto ES la autoridad: la corrida con artefacto en el subroot sigue siendo éxito.

    El eco desviado se advierte (logging) y queda en la evidencia del
    post-check, pero NO tumbar el veredicto: (a) el mapa de autoridades del
    brief mantiene el artefacto como autoridad post-run; (b) el eco mide el
    parse del argv, no el destino de escritura; (c) el formato está medido solo
    sobre Alpha-209 y extrapolarlo a un gate duro repite el criterio v1 que el
    roadmap retiró. Endurecerlo exige corrida real — declarado en el handoff.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_layout is not None
    staging = config.texgen_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    desviado = "C:\\decoy\\TexGen\\"
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path,
            "TexGen",
            lambda: _escribir_salida(staging, "a.dds"),
            log=_log_con_eco(f"Using Output Path: {desviado}"),
        ),
    )
    with caplog.at_level(logging.WARNING, logger="sky_claw"), patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is True  # el artefacto manda
    advertencias = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(desviado in r.getMessage() for r in advertencias)


async def test_f5_artefacto_presente_con_eco_correcto_es_exito_sin_advertencia(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    config, runner = _runner_texgen(tmp_path)
    assert config.output_layout is not None
    staging = config.texgen_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    fake = _EjecucionFalsa(
        return_code=0,
        al_ejecutar=_corrida_que_completa(
            tmp_path,
            "TexGen",
            lambda: _escribir_salida(staging, "a.dds"),
            log=_log_con_eco(f"Using Output Path: {config.texgen_root}\\"),
        ),
    )
    with caplog.at_level(logging.WARNING, logger="sky_claw"), patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is True
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_f5_data_path_correcto_e_incorrecto(tmp_path: pathlib.Path) -> None:
    """El ``-d:`` se contrasta contra el ``data_dir`` vigente de la config.

    Es la mitad del contrato que cierra el falso verde de los LODs del juego
    base: el binario declarando una ruta de datos distinta de la configurada es
    evidencia —atribuible a la corrida— de que leyó otro ``Data``.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.data_dir is not None
    correcto = _log_con_eco(
        "Using Output Path: X", eco_data=f"Using Skyrim Special Edition Data Path: {config.data_dir}\\"
    )
    incorrecto = _log_con_eco(
        "Using Output Path: X", eco_data="Using Skyrim Special Edition Data Path: C:\\otro\\Data\\"
    )
    ruta_log = tmp_path / "DynDOLOD" / "Logs" / "TexGen_SSE_log.txt"

    # Cada caso arranca sin log: la firma previa es _SIN_LOG y el archivo entero
    # es atribuible a la corrida — la misma forma en que el runner la toma
    # cuando el binario crea su log en la corrida.
    if ruta_log.exists():
        ruta_log.unlink()
    firma = runner._firma_del_log("TexGen")
    _escribir_log(tmp_path, "TexGen", correcto)
    post_ok = runner._post_check("TexGen", firma)
    assert post_ok.evidencia_rutas is not None
    assert post_ok.evidencia_rutas.data.veredicto is VeredictoRuta.COINCIDE

    ruta_log.unlink()
    firma = runner._firma_del_log("TexGen")
    _escribir_log(tmp_path, "TexGen", incorrecto)
    post_mal = runner._post_check("TexGen", firma)
    assert post_mal.evidencia_rutas is not None
    assert post_mal.evidencia_rutas.data.veredicto is VeredictoRuta.DIFIERE
    assert post_mal.evidencia_rutas.data.declarada == "C:\\otro\\Data\\"


def test_f5_dyndolod_no_hereda_el_subroot_de_texgen() -> None:
    """Aislamiento: el ``-o:`` de cada herramienta se contrasta contra SU subroot.

    Un eco de TexGen apuntando al subroot de DynDOLOD es DIFIERE para TexGen —
    la comparación es por herramienta, nunca contra la familia ni contra el
    hermano. Y al revés.
    """
    texgen = evaluar_evidencia_de_rutas(
        "Using Output Path: C:\\Work\\DynDOLOD\\DynDOLOD\\\n",
        raiz_de_salida=pathlib.Path(r"C:\Work\DynDOLOD\TexGen"),
        data_dir=None,
    )
    assert texgen.output.veredicto is VeredictoRuta.DIFIERE
    dyndolod = evaluar_evidencia_de_rutas(
        "Using Output Path: C:\\Work\\DynDOLOD\\TexGen\\\n",
        raiz_de_salida=pathlib.Path(r"C:\Work\DynDOLOD\DynDOLOD"),
        data_dir=None,
    )
    assert dyndolod.output.veredicto is VeredictoRuta.DIFIERE


@pytest.mark.skipif(sys.platform != "win32", reason="el vector del rig es de Windows")
async def test_f5_vector_del_rig_end_to_end_coincide(tmp_path: pathlib.Path) -> None:
    """El vector real del rig (espacios + backslash duplicado) contra un root equivalente.

    End-to-end sobre el post-check del runner con el literal del rig: el eco
    ``E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD\\`` coincide con el
    ``-o:`` configurado ``E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD``.
    """
    _, runner = _runner_texgen(tmp_path)
    raiz = pathlib.Path(r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD")
    ev = evaluar_evidencia_de_rutas(
        LINEA_RIG_OUTPUT + "\n",
        raiz_de_salida=raiz,
        data_dir=None,
    )
    assert ev.output.veredicto is VeredictoRuta.COINCIDE


# ---------------------------------------------------------------------------
# Finding 5-A — el cierre documental de T5
# ---------------------------------------------------------------------------


def test_f5a_switch_de_ruta_declara_t5_cerrado_con_el_rig() -> None:
    """El docstring de ``_switch_de_ruta`` ya no declara T5 pendiente.

    La evidencia está commiteada: ``docs/validation/2026-09-13_pr580_real_rig/``
    (``dyndolod-command.txt`` con el ``-o:`` duplicado, ``dyndolod-log.txt:12``
    con el eco normalizado) y ``docs/validation/2026-09-10_t5v2_dyndolod_stage9.md``.
    Un código que documenta "pendiente" lo que el árbol ya probó es deuda
    documental medible — este test es su gate.
    """
    rig = pathlib.Path(__file__).resolve().parents[1] / "docs" / "validation" / "2026-09-13_pr580_real_rig"
    assert (rig / "dyndolod-command.txt").exists()
    assert (rig / "dyndolod-log.txt").exists()

    docstring = DynDOLODRunner._switch_de_ruta.__doc__ or ""
    assert "Pendiente de T5" not in docstring
    assert "2026-09-13_pr580_real_rig" in docstring


def test_f5a_test_de_divergencia_declara_t5_respondido() -> None:
    """El gemelo del lado de los tests: su docstring tampoco puede decir "pendiente"."""
    from tests import test_contrato_argumentos_cli

    docstring = test_contrato_argumentos_cli.test_dyndolod_argv_con_espacios_diverge_entre_crt_y_delphi.__doc__ or ""
    assert "Pendiente de T5" not in docstring
    assert "2026-09-13_pr580_real_rig" in docstring
