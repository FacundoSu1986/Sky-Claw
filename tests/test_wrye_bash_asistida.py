"""Etapa 6 asistida — contrato de preparación y veredicto del Bashed Patch.

Por qué existe: ``wrye_bash_runner.py`` no puede construir el parche (``Mopy/bash/barg.py``
no expone build headless, verificado contra el parser oficial) y por eso
``WryeBashPipelineService.execute_pipeline`` falla cerrado **después** de tomar los locks
``bashed-patch`` + ``load-order``, emitir el ``ActionManifest`` y abrir una TX: cada
invocación escribe caja negra por un ritual que no puede correr. La decisión (ADR 0016)
es modelar la etapa 6 como **asistida**: el operador construye el parche en la GUI de
Wrye Bash lanzado desde MO2, y Sky-Claw **prepara** (instrucciones + huella previa) y
**verifica** (artefacto + ``plugins.txt``), sin spawnear nunca.

Tres propiedades que este archivo ancla, y por qué cada una tiene test:

1. **El veredicto no se deriva de ningún exit code.** No hay subproceso que consultar:
   se deriva de evidencia en disco (artefacto regular, no enlace, no vacío, habilitado
   en ``plugins.txt``). Es el mismo defecto que ``test_contrato_veredicto_de_exito.py``
   persigue —éxito decidido por algo que no reporta el resultado del trabajo—, acá con
   la variante "no hay código que consultar".
2. **Lo que no se pudo leer no es "está".** ``plugins.txt`` ilegible o artefacto
   ilegible nunca producen verde: la etapa 9 (``etapas_previas_lods``) consume esta
   evidencia para decidir si lanza DynDOLOD, y un falso verde ahí encadena stages.
3. **La etapa asistida NO spawnea.** El módulo no importa ni llama
   ``subprocess``/``asyncio.create_subprocess_exec``. Eso lo mantiene fuera del censo
   de ``LANZADORES_ESPERADOS`` (``test_contrato_argumentos_cli.py``), donde el runner
   de Wrye Bash figura **excluido a propósito** por el ancla ``spawn_mock.assert_not_called``.
   Si este módulo empezara a spawnear, la etapa 6 volvería a la ambigüedad headless que
   ADR 0016 cierra.

**Límite declarado.** La detección de enlaces es local y por ``lstat`` (como
``artifact_digest``): no promete una garantía TOCTOU-fuerte contra una carrera que
reemplace el archivo entre la inspección y la apertura. Y la frescura solo se PRUEBA
cuando el llamador trae la ``huella_previa``; sin ella el veredicto es honesto
(``frescura_probada=False``) en vez de inventar un cambio por ``mtime`` — comparar
``mtime`` es exactamente lo que el contrato de LOOT rechazó (PR-2, ``CHANGED`` vs
``NO_CHANGE`` por estado observable, no por marca de tiempo).
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools.wrye_bash_asistida import (
    _MENSAJES,
    PASOS_ASISTIDOS,
    PRECEDENCIA_DE_MOTIVOS,
    REGLAS_DEL_VEREDICTO,
    EstadoEnPlugins,
    MotivoAsistido,
    TestigoPrevio,
    estado_en_plugins_txt,
    huella_de_artefacto,
    preparacion_como_resultado,
    preparar_etapa_asistida,
    veredicto_como_resultado,
    veredicto_etapa_asistida,
)
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from sky_claw.local.validators.etapas_previas_lods import _plugins_habilitados
from tests._symlink_guard import crear_junction, puede_crear_symlinks

_SIN_SYMLINKS = pytest.mark.skipif(
    not puede_crear_symlinks(), reason="crear symlinks exige privilegios/modo desarrollador"
)

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_MODULO = _RAIZ / "sky_claw" / "local" / "tools" / "wrye_bash_asistida.py"


def _perfil_con_plugins(tmp_path: pathlib.Path, contenido: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Crea ``<data>/profiles/Default/plugins.txt`` y el Data del juego."""
    plugins = tmp_path / "profiles" / "Default" / "plugins.txt"
    plugins.parent.mkdir(parents=True)
    plugins.write_text(contenido, encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    return game / "Data" / BASHED_PATCH_NAME, plugins


def _parche(artefacto: pathlib.Path, contenido: bytes = b"TES4" + b"\x00" * 60) -> pathlib.Path:
    artefacto.parent.mkdir(parents=True, exist_ok=True)
    artefacto.write_bytes(contenido)
    return artefacto


# ==============================================================================
# 1. Huella del artefacto — identidad por contenido, nunca por mtime
# ==============================================================================


def test_huella_de_artefacto_inexistente_no_inventa_bytes(tmp_path: pathlib.Path) -> None:
    huella = huella_de_artefacto(tmp_path / "no-existe.esp")

    assert huella.existe is False
    assert huella.sha256 is None
    assert huella.bytes == 0
    assert huella.es_regular is False


def test_huella_hashea_todo_el_contenido_y_no_una_muestra(tmp_path: pathlib.Path) -> None:
    """El digest cubre el archivo entero, incluso cruzando el chunk de lectura."""
    grande = _parche(tmp_path / BASHED_PATCH_NAME, b"A" * (3 * 1024 * 1024))
    otro = _parche(tmp_path / "otro.esp", b"A" * (3 * 1024 * 1024 - 1) + b"B")

    assert huella_de_artefacto(grande).sha256 != huella_de_artefacto(otro).sha256
    assert huella_de_artefacto(grande).bytes == 3 * 1024 * 1024


def test_huella_de_un_directorio_no_es_un_artefacto(tmp_path: pathlib.Path) -> None:
    carpeta = tmp_path / BASHED_PATCH_NAME
    carpeta.mkdir()

    huella = huella_de_artefacto(carpeta)

    assert huella.existe is True
    assert huella.es_regular is False
    assert huella.sha256 is None


@_SIN_SYMLINKS
def test_huella_de_un_symlink_no_se_sigue(tmp_path: pathlib.Path) -> None:
    """Un enlace a un parche ajeno no puede validar nuestra corrida."""
    ajeno = _parche(tmp_path / "ajeno.esp")
    enlace = tmp_path / BASHED_PATCH_NAME
    enlace.symlink_to(ajeno)

    huella = huella_de_artefacto(enlace)

    assert huella.existe is True
    assert huella.es_enlace is True
    assert huella.es_regular is False
    assert huella.sha256 is None


def test_huella_de_un_junction_no_se_sigue(tmp_path: pathlib.Path) -> None:
    """``os.path.islink`` es False para un junction de Windows: se mira el reparse tag."""
    destino = tmp_path / "carpeta-ajena"
    destino.mkdir()
    enlace = tmp_path / BASHED_PATCH_NAME
    error = crear_junction(enlace, destino)
    if error is not None:
        pytest.skip(f"no se pudo crear el junction: {error}")

    huella = huella_de_artefacto(enlace)

    assert huella.existe is True
    assert huella.es_enlace is True
    assert huella.es_regular is False


# ==============================================================================
# 2. Estado en plugins.txt — el artefacto tiene que estar ACTIVO
# ==============================================================================


def test_estado_en_plugins_enumera_los_tres_casos(tmp_path: pathlib.Path) -> None:
    _, plugins = _perfil_con_plugins(
        tmp_path,
        f"*Skyrim.esm\n{BASHED_PATCH_NAME}\n*Update.esm\n",
    )

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.DESHABILITADO
    assert estado_en_plugins_txt(plugins, "Skyrim.esm") is EstadoEnPlugins.HABILITADO
    assert estado_en_plugins_txt(plugins, "Ausente.esp") is EstadoEnPlugins.AUSENTE


def test_estado_en_plugins_ignora_crlf_comentarios_y_espacios(tmp_path: pathlib.Path) -> None:
    """Misma semántica que libloadorder (``loot.outcome.parse_load_order_bytes``)."""
    _, plugins = _perfil_con_plugins(
        tmp_path,
        f"# comentario\r\n\r\n*Skyrim.esm\r\n*{BASHED_PATCH_NAME}\r\n",
    )

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.HABILITADO


def test_estado_en_plugins_es_case_insensitive(tmp_path: pathlib.Path) -> None:
    _, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME.upper()}\n")

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.HABILITADO


def test_una_entrada_activa_y_otra_inactiva_cuenta_como_habilitada(tmp_path: pathlib.Path) -> None:
    """El caso ``+X``/``-X`` duplicado de #835: si ALGUNA está activa, el motor la carga."""
    _, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n{BASHED_PATCH_NAME}\n")

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.HABILITADO


@pytest.mark.parametrize(
    "crudo",
    [
        pytest.param(b"\xef\xbb\xbf*" + BASHED_PATCH_NAME.encode() + b"\n", id="bom-utf8"),
        pytest.param(b"*" + BASHED_PATCH_NAME.encode() + b"\r\n", id="crlf"),
        pytest.param(b"# comentario\n\n*" + BASHED_PATCH_NAME.encode() + b"\n", id="comentarios"),
        pytest.param(b"Skyrim.esm\n*" + BASHED_PATCH_NAME.encode() + b"\n", id="tras-otro-master"),
        pytest.param(b"\xef\xbb\xbf*" + BASHED_PATCH_NAME.encode() + b"\r\n", id="bom-mas-crlf"),
    ],
)
def test_el_lector_concuerda_con_el_consumidor_de_la_etapa_9(tmp_path: pathlib.Path, crudo: bytes) -> None:
    """Mismo archivo, misma pregunta, misma respuesta que `etapas_previas_lods`.

    El hermano es el oráculo: es quien habilita DynDOLOD. Si mi lector dice AUSENTE
    donde el suyo dice habilitado, el operador recibe un rojo con el diagnóstico
    equivocado —y lo habilita sin que el mensaje cambie—. El BOM es el caso medido:
    `utf-8-sig` lo descarta, un lector de bytes ingenuo deja el nombre pegado al BOM.
    """
    _, plugins = _perfil_con_plugins(tmp_path, "")
    plugins.write_bytes(crudo)

    mio = estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.HABILITADO
    hermano = BASHED_PATCH_NAME.lower() in (_plugins_habilitados(plugins) or set())

    assert mio is True
    assert mio is hermano


def test_plugins_inexistente_es_ausente_y_no_ilegible(tmp_path: pathlib.Path) -> None:
    assert estado_en_plugins_txt(tmp_path / "no.txt", BASHED_PATCH_NAME) is EstadoEnPlugins.AUSENTE


def test_plugins_ilegible_no_se_confunde_con_ausente(tmp_path: pathlib.Path) -> None:
    """'No lo pude leer' nunca es 'está': fail-closed (misma regla que `etapas_previas_lods`).

    Con un directorio en vez de ``chmod 000``: ese truco es un no-op como root y no
    existe en Windows, que es la única plataforma de entrega.
    """
    plugins = tmp_path / "plugins.txt"
    plugins.mkdir()

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.ILEGIBLE


# ==============================================================================
# 3. Veredicto — matriz de motivos, precedencia congelada, frescura honesta
# ==============================================================================


def test_veredicto_ok_cuando_el_operador_construyo_y_habilito(tmp_path: pathlib.Path) -> None:
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is True
    assert veredicto.motivo is MotivoAsistido.OK


def test_veredicto_exige_artefacto_existente(tmp_path: pathlib.Path) -> None:
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.ARTEFACTO_AUSENTE


def test_veredicto_rechaza_artefacto_vacio(tmp_path: pathlib.Path) -> None:
    """Un placeholder de 0 bytes no es un Bashed Patch."""
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto, b"")

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.ARTEFACTO_VACIO


def test_veredicto_exige_habilitado_en_plugins(tmp_path: pathlib.Path) -> None:
    """Construido pero deshabilitado: la etapa 9 no lo vería. No es éxito."""
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"{BASHED_PATCH_NAME}\n")
    _parche(artefacto)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.NO_HABILITADO


def test_veredicto_con_plugins_ausente_no_esta_habilitado(tmp_path: pathlib.Path) -> None:
    """Renombrado: sin ``plugins.txt`` el estado es AUSENTE, no ILEGIBLE. El ilegible
    de verdad se prueba abajo con un directorio (ver ``test_veredicto_plugins_ilegible``)."""
    artefacto = _parche(tmp_path / BASHED_PATCH_NAME)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=tmp_path / "no-existe.txt")

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.NO_HABILITADO
    assert veredicto.estado_plugins is EstadoEnPlugins.AUSENTE


def test_veredicto_plugins_ilegible(tmp_path: pathlib.Path) -> None:
    """Un directorio con el nombre ``plugins.txt`` no se puede leer: ILEGIBLE, no AUSENTE.

    Cross-platform y seguro como root (``read_bytes`` sobre un directorio lanza
    ``IsADirectoryError``, subclase de ``OSError``): el truco del ``chmod 000`` no
    sirve —es un no-op para root y no existe en Windows—.
    """
    artefacto = _parche(tmp_path / BASHED_PATCH_NAME)
    plugins = tmp_path / "plugins.txt"
    plugins.mkdir()

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.PLUGINS_ILEGIBLE
    assert veredicto.estado_plugins is EstadoEnPlugins.ILEGIBLE


def test_veredicto_artefacto_no_regular(tmp_path: pathlib.Path) -> None:
    """El artefacto existe pero es un directorio: NO_REGULAR, nunca OK."""
    artefacto = tmp_path / BASHED_PATCH_NAME
    artefacto.mkdir()
    _, plugins = _perfil_con_plugins(tmp_path / "b", f"*{BASHED_PATCH_NAME}\n")

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.ARTEFACTO_NO_REGULAR


def test_veredicto_artefacto_ilegible(tmp_path: pathlib.Path) -> None:
    """``lstat`` funciona, la LECTURA falla: ILEGIBLE con ``sha256=None`` (fail-closed).

    Es la única rama que no se puede provocar de forma portable sin fabricar el
    ``OSError`` (un ``chmod`` no niega lectura a root ni existe en Windows), así que se
    parchea ``Path.open`` — se prueba el MAPEO del error, que es lo que decide el
    veredicto, no el mecanismo del filesystem.
    """
    artefacto = _parche(tmp_path / BASHED_PATCH_NAME)
    _, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")

    with patch("pathlib.Path.open", side_effect=PermissionError("denegado")):
        veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.ARTEFACTO_ILEGIBLE
    assert veredicto.huella.sha256 is None
    assert veredicto.huella.ilegible is True


def test_primer_parche_ausencia_previa_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """El caso que más importa: no existía en la fase 1 y aparece después.

    Con un ``sha256 | None`` esto era indistinguible de "no traje testigo", así que la
    creación desde cero —el escenario normal de la primera corrida— era el único donde
    la frescura no se podía demostrar.
    """
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    testigo = TestigoPrevio.desde(huella_de_artefacto(artefacto))
    assert testigo.existia is False

    _parche(artefacto)
    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins, testigo=testigo)

    assert veredicto.success is True
    assert veredicto.frescura_probada is True


def test_sin_testigo_el_veredicto_declara_que_la_frescura_no_se_probo(
    tmp_path: pathlib.Path,
) -> None:
    """Sin testigo previo no se puede afirmar que ESTA corrida lo generó."""
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is True
    assert veredicto.frescura_probada is False


def test_con_testigo_y_huella_distinta_la_frescura_queda_probada(tmp_path: pathlib.Path) -> None:
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)
    testigo = TestigoPrevio(existia=True, sha256="0" * 64)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins, testigo=testigo)

    assert veredicto.success is True
    assert veredicto.frescura_probada is True


def test_con_testigo_identico_el_veredicto_es_sin_cambio(tmp_path: pathlib.Path) -> None:
    """El operador no reconstruyó nada: el archivo es el de antes → no hay éxito nuevo."""
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)
    testigo = TestigoPrevio.desde(huella_de_artefacto(artefacto))

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins, testigo=testigo)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.SIN_CAMBIO


@_SIN_SYMLINKS
def test_un_enlace_no_aprueba_la_etapa_aunque_el_destino_sea_valido(tmp_path: pathlib.Path) -> None:
    ajeno = _parche(tmp_path / "ajeno.esp")
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    artefacto.symlink_to(ajeno)

    veredicto = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)

    assert veredicto.success is False
    assert veredicto.motivo is MotivoAsistido.ARTEFACTO_ENLACE


def test_el_vocabulario_de_motivos_esta_congelado() -> None:
    """Un motivo nuevo sin clasificar rompe el ancla a propósito (enumerar, no muestrear)."""
    assert {m.value for m in MotivoAsistido} == {
        "OK",
        "ESPERANDO_OPERADOR",
        "ARTEFACTO_AUSENTE",
        "ARTEFACTO_ENLACE",
        "ARTEFACTO_ILEGIBLE",
        "ARTEFACTO_NO_REGULAR",
        "ARTEFACTO_VACIO",
        "PLUGINS_ILEGIBLE",
        "NO_HABILITADO",
        "SIN_CAMBIO",
    }


def test_cada_regla_tiene_mensaje_y_no_sobra_ninguno() -> None:
    """El mapa de mensajes es TOTAL sobre las reglas: un motivo nuevo sin mensaje
    explotaría con KeyError dentro de `veredicto_como_resultado`, en producción."""
    reglas = {motivo for motivo, _ in REGLAS_DEL_VEREDICTO}

    assert set(_MENSAJES) == set(MotivoAsistido)
    assert reglas.issubset(set(_MENSAJES))


@pytest.mark.parametrize("motivo", list(MotivoAsistido))
def test_veredicto_como_resultado_no_explota_con_ningun_motivo(motivo: MotivoAsistido) -> None:
    """Enumera los 10 motivos: la primera versión solo ejercitaba 8 y dos de las
    ramas de fallo no tenían test que las disparara."""
    from sky_claw.local.tools.wrye_bash_asistida import HuellaDeArtefacto, VeredictoAsistido

    veredicto = VeredictoAsistido(
        success=motivo is MotivoAsistido.OK,
        motivo=motivo,
        artefacto="x",
        huella=HuellaDeArtefacto("x", False, False, False, False, 0, None),
        estado_plugins=EstadoEnPlugins.AUSENTE,
        frescura_probada=False,
    )

    resultado = veredicto_como_resultado(veredicto)

    assert resultado["motivo"] == motivo.value
    if motivo is MotivoAsistido.OK:
        assert resultado["message"] == ""
    else:
        assert resultado["message"]


def test_la_precedencia_de_motivos_esta_congelada() -> None:
    """La PRIMERA causa aplicable gana; el orden es contrato, no detalle de implementación."""
    assert PRECEDENCIA_DE_MOTIVOS == (
        MotivoAsistido.ARTEFACTO_AUSENTE,
        MotivoAsistido.ARTEFACTO_ENLACE,
        MotivoAsistido.ARTEFACTO_ILEGIBLE,
        MotivoAsistido.ARTEFACTO_NO_REGULAR,
        MotivoAsistido.ARTEFACTO_VACIO,
        MotivoAsistido.PLUGINS_ILEGIBLE,
        MotivoAsistido.NO_HABILITADO,
        MotivoAsistido.SIN_CAMBIO,
    )


def test_la_precedencia_se_aplica_cuando_coexisten_dos_defectos(tmp_path: pathlib.Path) -> None:
    """Enumeración de los pares que coexisten: gana siempre el primero de la tabla."""
    # ausente gana sobre plugins ausente (existencia primero)
    artefacto, _ = _perfil_con_plugins(tmp_path, "")
    ausente = veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=tmp_path / "no.txt")
    assert ausente.motivo is MotivoAsistido.ARTEFACTO_AUSENTE

    # vacío gana sobre no-habilitado
    artefacto2, plugins2 = _perfil_con_plugins(tmp_path / "b", f"{BASHED_PATCH_NAME}\n")
    _parche(artefacto2, b"")
    assert veredicto_etapa_asistida(artefacto=artefacto2, plugins_txt=plugins2).motivo is MotivoAsistido.ARTEFACTO_VACIO

    # no-habilitado gana sobre sin-cambio
    artefacto3, plugins3 = _perfil_con_plugins(tmp_path / "c", f"{BASHED_PATCH_NAME}\n")
    _parche(artefacto3)
    testigo = TestigoPrevio.desde(huella_de_artefacto(artefacto3))
    veredicto3 = veredicto_etapa_asistida(artefacto=artefacto3, plugins_txt=plugins3, testigo=testigo)
    assert veredicto3.motivo is MotivoAsistido.NO_HABILITADO


# ==============================================================================
# 4. Contrato canónico de tool-result (AGENTS.md: success + message)
# ==============================================================================


def test_la_preparacion_nunca_es_un_exito(tmp_path: pathlib.Path) -> None:
    """Preparar no construye nada: declarar éxito sería el falso verde que este módulo cierra."""
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")

    preparacion = preparar_etapa_asistida(artefacto=artefacto, plugins_txt=plugins)
    resultado = preparacion_como_resultado(preparacion)

    assert resultado["success"] is False
    assert resultado["pendiente"] is True
    assert resultado["motivo"] == MotivoAsistido.ESPERANDO_OPERADOR.value
    assert BASHED_PATCH_NAME in resultado["message"]
    assert resultado["instrucciones"] == list(PASOS_ASISTIDOS)


def test_el_veredicto_exitoso_tiene_message_vacio(tmp_path: pathlib.Path) -> None:
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)

    resultado = veredicto_como_resultado(veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins))

    assert resultado["success"] is True
    assert resultado["message"] == ""
    assert resultado["motivo"] == "OK"
    assert resultado["sha256"]


def test_el_veredicto_fallido_nombra_la_causa_en_espanol(tmp_path: pathlib.Path) -> None:
    artefacto, plugins = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")

    resultado = veredicto_como_resultado(veredicto_etapa_asistida(artefacto=artefacto, plugins_txt=plugins))

    assert resultado["success"] is False
    assert "Bashed Patch" in resultado["message"]
    assert resultado["motivo"] == "ARTEFACTO_AUSENTE"


# ==============================================================================
# 5. Anclas del módulo
# ==============================================================================


#: Superficie COMPLETA de spawn de CPython. No es una muestra: si alguien escribe
#: ``asyncio.create_subprocess_exec`` —justo lo que el ADR nombra— y el set no la tiene,
#: el ancla pasa en verde sobre código que sí spawnearía. Ese fue el defecto de la primera
#: versión de este test (revisión adversarial 2026-10-30): el set no incluía
#: ``create_subprocess_exec`` y el detector no veía la llamada que la prosa prometía.
_SPAWN_PROHIBIDO = frozenset(
    {
        # asyncio
        "create_subprocess_exec",
        "create_subprocess_shell",
        # subprocess
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        # os
        "system",
        "popen",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "startfile",
    }
)


def _spawns_en_codigo(codigo: str) -> list[str]:
    """Detector del ancla: nombres de spawn importados o invocados en *codigo*."""
    hallados: list[str] = []
    for nodo in ast.walk(ast.parse(codigo)):
        if isinstance(nodo, ast.Import):
            # ``import os`` es legítimo (el módulo lo usa para ``lstat``); lo que no se
            # tolera es traer el MÓDULO de subprocesos.
            hallados.extend(n.name for n in nodo.names if n.name.split(".")[0] == "subprocess")
        elif isinstance(nodo, ast.ImportFrom):
            origen = (nodo.module or "").split(".")[0]
            if origen == "subprocess":
                hallados.append(nodo.module or "")
            elif origen == "os":
                # ``from os import system, spawnv, execv...`` sí es spawn.
                hallados.extend(n.name for n in nodo.names if n.name in _SPAWN_PROHIBIDO)
        elif isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if nombre in _SPAWN_PROHIBIDO:
                hallados.append(nombre)
    return hallados


def test_el_modulo_asistido_no_spawnea_procesos() -> None:
    """La etapa asistida no ejecuta nada: por eso no entra al censo de lanzadores.

    Si este ancla se rompe, `LANZADORES_ESPERADOS` (test_contrato_argumentos_cli.py)
    queda incompleto y la etapa 6 reabre la ambigüedad headless que ADR 0016 cierra.
    """
    hallados = _spawns_en_codigo(_MODULO.read_text(encoding="utf-8"))

    assert not hallados, f"el módulo asistido no puede spawnear ni importar subprocess: {hallados}"


@pytest.mark.parametrize(
    "codigo",
    [
        "import asyncio\nasync def f(e):\n    return await asyncio.create_subprocess_exec(e)",
        "import asyncio\nasync def f(e):\n    return await asyncio.create_subprocess_shell(e)",
        "import os\nos.system('wrye')",
        "import os\nos.spawnv(0, 'wrye', [])",
        "import os\nos.execv('wrye', [])",
        "import subprocess\nsubprocess.Popen(['wrye'])",
        "from subprocess import run\nrun(['wrye'])",
        "import subprocess\nsubprocess.check_output(['wrye'])",
    ],
)
def test_el_detector_del_ancla_ve_las_formas_reales_de_spawn(codigo: str) -> None:
    """CONTROL POSITIVO: sin esto, vaciar ``_SPAWN_PROHIBIDO`` dejaría el ancla en verde.

    Un ancla que solo se prueba sobre el código que debe pasar no distingue "no hay
    spawn" de "no lo estoy mirando". Cada caso acá es una forma que el módulo podría
    usar mañana; el detector tiene que verlas todas.
    """
    assert _spawns_en_codigo(codigo), f"el detector no vio el spawn en: {codigo!r}"


def test_los_pasos_nombran_la_herramienta_el_artefacto_y_mo2() -> None:
    """Las instrucciones son para un operador: tienen que decir QUÉ abrir y DÓNDE."""
    texto = " ".join(PASOS_ASISTIDOS)

    assert "Wrye Bash" in texto
    assert BASHED_PATCH_NAME in texto
    assert "MO2" in texto


# ==============================================================================
# 6. Servicio — preparar y verificar detrás de la resolución de rutas
# ==============================================================================


def _preflight_verde() -> MagicMock:
    """Preflight inyectado y verde: estos tests no construyen sensores reales."""
    reporte = MagicMock()
    reporte.blocks_mutations = False
    reporte.status.value = "green"
    reporte.to_dict.return_value = {}
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    return preflight


def _servicio(game: pathlib.Path, mo2: pathlib.Path) -> WryeBashPipelineService:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = mo2
    return WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
        preflight=_preflight_verde(),
    )


@pytest.mark.asyncio
async def test_preparar_devuelve_instrucciones_y_huella_previa(tmp_path: pathlib.Path) -> None:
    artefacto, _ = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto, b"parche-previo")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["success"] is False
    assert resultado["pendiente"] is True
    assert resultado["testigo_previo"] == {
        "existia": True,
        "sha256": huella_de_artefacto(artefacto).sha256,
    }
    assert resultado["pendiente_no_ratificado"] is True
    assert resultado["instrucciones"] == list(PASOS_ASISTIDOS)


@pytest.mark.asyncio
async def test_preparar_corrige_la_huella_y_verificar_la_usa(tmp_path: pathlib.Path) -> None:
    """Ciclo completo: preparar → el operador reconstruye → verificar prueba la frescura."""
    artefacto, _ = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto, b"parche-viejo")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    preparacion = await servicio.preparar_etapa_asistida(profile="Default")
    _parche(artefacto, b"parche-nuevo-de-la-corrida")
    veredicto = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=preparacion["testigo_previo"])

    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True


@pytest.mark.asyncio
async def test_primera_corrida_via_servicio_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """Sin Bashed Patch previo: preparar deja constancia de la ausencia y la
    aparición posterior prueba que esta corrida lo escribió."""
    artefacto, _ = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    preparacion = await servicio.preparar_etapa_asistida(profile="Default")
    assert preparacion["testigo_previo"] == {"existia": False, "sha256": None}

    _parche(artefacto)
    veredicto = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=preparacion["testigo_previo"])

    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True


@pytest.mark.asyncio
async def test_un_testigo_previo_malformado_se_descarta(tmp_path: pathlib.Path) -> None:
    """Fail-closed: un testigo inventado daría un SIN_CAMBIO falso; mejor sin testigo."""
    artefacto, _ = _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    _parche(artefacto)
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default", testigo_previo={"existia": "sí", "sha256": 42}
    )

    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is False


@pytest.mark.asyncio
async def test_verificar_sin_artefacto_no_es_exito(tmp_path: pathlib.Path) -> None:
    _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.verificar_etapa_asistida(profile="Default")

    assert resultado["success"] is False
    assert resultado["motivo"] == "ARTEFACTO_AUSENTE"


@pytest.mark.asyncio
async def test_preparar_bloquea_si_el_preflight_esta_en_rojo(tmp_path: pathlib.Path) -> None:
    """No se manda al operador a un paso manual que el preflight ya sabe que va a fallar."""
    _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    reporte = MagicMock()
    reporte.blocks_mutations = True
    reporte.status.value = "red"
    chequeo = MagicMock()
    chequeo.status.value = "red"
    chequeo.summary = "Data sin permisos de escritura"
    reporte.checks = [chequeo]
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)

    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=MagicMock(),
        preflight=preflight,
    )

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["success"] is False
    assert resultado["reason"] == "PreflightBlocked"
    assert "Data sin permisos" in resultado["message"]


@pytest.mark.asyncio
async def test_preparar_sin_data_resoluble_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Sin ruta del juego no hay artefacto que observar: dict honesto, no excepción."""
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = None
    resolver.get_mo2_instance_data_root.return_value = None
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
    )

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert isinstance(resultado, dict)
    assert resultado["success"] is False
    assert resultado["reason"] == "SinRutasResolubles"


@pytest.mark.asyncio
async def test_preparar_rechaza_un_perfil_con_separadores(tmp_path: pathlib.Path) -> None:
    """El perfil compone una ruta: ``..`` o separadores no pueden escapar del perfil."""
    _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.preparar_etapa_asistida(profile="../otro")

    assert resultado["success"] is False
    assert resultado["reason"] == "PerfilInvalido"
