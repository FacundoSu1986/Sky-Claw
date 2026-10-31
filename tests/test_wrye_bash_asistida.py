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
import os
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools.output_targets import CAPA_DATA_JUEGO, bashed_patch_en_overlay
from sky_claw.local.tools.wrye_bash_asistida import (
    _MENSAJES,
    PASOS_ASISTIDOS,
    PRECEDENCIA_DE_MOTIVOS,
    REGLAS_DEL_VEREDICTO,
    EstadoEnPlugins,
    HuellaDeArtefacto,
    MotivoAsistido,
    TestigoPrevio,
    _Observacion,
    estado_en_plugins_txt,
    huella_de_artefacto,
    preparacion_como_resultado,
    preparar_etapa_asistida,
    veredicto_como_resultado,
    veredicto_etapa_asistida,
)
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import DOMINIO_FISICO, DOMINIO_USVFS, WryeBashPipelineService
from sky_claw.local.validators.etapas_previas_lods import _plugins_habilitados
from tests._symlink_guard import crear_junction, junction_guard, puede_crear_symlinks

_SIN_SYMLINKS = pytest.mark.skipif(
    not puede_crear_symlinks(), reason="crear symlinks exige privilegios/modo desarrollador"
)

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_MODULO = _RAIZ / "sky_claw" / "local" / "tools" / "wrye_bash_asistida.py"


def _perfil_con_plugins(
    tmp_path: pathlib.Path, contenido: str, *, perfil: str = "Default"
) -> tuple[pathlib.Path, pathlib.Path]:
    """Crea ``<data>/profiles/<perfil>/plugins.txt`` y el Data del juego."""
    plugins = tmp_path / "profiles" / perfil / "plugins.txt"
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


@junction_guard
def test_huella_de_un_junction_no_se_sigue(tmp_path: pathlib.Path) -> None:
    """``os.path.islink`` es False para un junction de Windows: se mira el reparse tag.

    ``junction_guard`` y no un ``skip`` ad-hoc: crear un junction en Windows no exige
    privilegios, así que un fallo del helper significa "el helper se rompió", no "el
    entorno no soporta la feature". Un skip silencioso hacía que perder la cobertura del
    reparse point —justo en la única plataforma que puede ejercerla— se viera igual que
    tenerla (contrato documentado en ``tests/_symlink_guard.crear_junction``).
    """
    destino = tmp_path / "carpeta-ajena"
    destino.mkdir()
    enlace = tmp_path / BASHED_PATCH_NAME
    error = crear_junction(enlace, destino)
    assert error is None, error

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


def _servicio(
    game: pathlib.Path,
    mo2: pathlib.Path,
    *,
    guard: Any = None,
) -> WryeBashPipelineService:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = mo2
    return WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
        plugin_limit_guard=guard,
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


@pytest.mark.parametrize("fase", ["preparar", "verificar"])
async def test_las_dos_fases_rechazan_un_perfil_con_separadores(fase: str, tmp_path: pathlib.Path) -> None:
    """El perfil compone una ruta: ``..`` o separadores no pueden escapar del perfil."""
    _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)
    metodo = getattr(servicio, f"{fase}_etapa_asistida")

    resultado = await metodo(profile="../otro")

    assert resultado["success"] is False
    assert resultado["reason"] == "PerfilInvalido"


# ==============================================================================
# 7. Revisión adversarial del PR (ronda 2) — overlay, dominio y testigos
# ==============================================================================
#
# Los seis casos de esta sección vienen de revisores externos y cada uno se verificó
# contra el código antes de aceptarlo. Los tres que se rechazaron (precedencia de
# motivos) quedan anclados igual: un rechazo sin test es indistinguible de un olvido.


def _resolver_real(game: pathlib.Path, mo2: pathlib.Path, *, mods_dir: pathlib.Path | None = None) -> MagicMock:
    """Resolver con rutas REALES: los sensores del preflight se construyen de verdad."""
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = mo2
    resolver.get_mo2_mods_path_best_effort.return_value = mods_dir if mods_dir is not None else mo2 / "mods"
    resolver.get_active_profile.return_value = "Default"
    resolver.get_skyrim_path_raw.return_value = str(game)
    resolver.get_mo2_path_raw.return_value = str(mo2)
    return resolver


def _con_resolver_real(game: pathlib.Path, mo2: pathlib.Path) -> WryeBashPipelineService:
    """Servicio con resolver de rutas reales: ``mods/`` y ``overwrite/`` se resuelven de verdad."""
    return WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(game, mo2),
        preflight=_preflight_verde(),
    )


def _escribir_modlist(perfil: pathlib.Path, mods: list[str]) -> None:
    """``modlist.txt`` en el formato físico de MO2: prioridad DESCENDENTE."""
    lineas = [f"+{nombre}" for nombre in reversed(mods)] or ["# vacío"]
    perfil.joinpath("modlist.txt").write_text(
        "# This file was automatically generated by Mod Organizer.\n" + "\n".join(lineas) + "\n", encoding="utf-8"
    )


# --- C1: el preflight de la etapa asistida NO mide el Data físico -----------------


def test_el_preflight_asistido_no_cablea_el_sensor_de_visibilidad_fisica(tmp_path: pathlib.Path) -> None:
    """Un perfil que vive entero en ``mods/`` no tiene sus plugins en ``Data``.

    El sensor de visibilidad (U-01) mide el ``Data`` FÍSICO y da rojo sobre ese perfil
    sano. Su propio contrato dice que solo debe cablearse cuando el backend que ejecuta
    consume ese namespace —y el ejecutor de la etapa asistida es el operador dentro de
    MO2, bajo la USVFS—. Cableado acá, ``preparar_etapa_asistida`` devolvía
    ``PreflightBlocked`` por un problema que no existía.
    """
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text("*Skyrim.esm\n")
    _escribir_modlist(perfil, ["SoloEnMods"])
    (tmp_path / "mods" / "SoloEnMods").mkdir(parents=True)
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(game, tmp_path),
    )

    fisico = servicio._ensure_preflight(dominio=DOMINIO_FISICO)
    asistido = servicio._ensure_preflight(dominio=DOMINIO_USVFS)

    assert fisico is not None and asistido is not None
    assert fisico is not asistido, "los dos dominios no pueden compartir instancia: miden namespaces distintos"
    assert fisico._visibility_check is not None, "el dominio físico conserva U-01: el runner spawneado lee Data"
    assert asistido._visibility_check is None, "el dominio USVFS no puede medir el Data físico (rojo falso)"


@pytest.mark.asyncio
async def test_el_preflight_asistido_no_bloquea_un_perfil_que_solo_vive_en_mods(tmp_path: pathlib.Path) -> None:
    """La conducta, no el wiring: el perfil de arriba se prepara en vez de bloquearse."""
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(perfil, ["SoloEnMods"])
    (tmp_path / "mods" / "SoloEnMods").mkdir(parents=True)
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(game, tmp_path),
    )

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado.get("reason") != "PreflightBlocked", f"bloqueado por el sensor equivocado: {resultado}"
    assert resultado["pendiente"] is True


def test_el_dominio_desconocido_se_rechaza(tmp_path: pathlib.Path) -> None:
    """``dominio`` elige qué namespace se mide: un valor inventado no puede caer en el default."""
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(tmp_path / "Skyrim", tmp_path),
    )

    with pytest.raises(ValueError, match="dominio de datos desconocido"):
        servicio._ensure_preflight(dominio="overlay")


# --- C2: el artefacto del operador vive en la superposición, no en Data -----------


@pytest.mark.asyncio
async def test_preparar_encuentra_el_parche_en_el_overwrite(tmp_path: pathlib.Path) -> None:
    """El operador construye el patch dentro de MO2: la USVFS lo desvía a ``overwrite/``.

    Mirar solo el ``Data`` físico daba ``existia=False`` sobre un artefacto que el juego
    sí carga, y la verificación posterior lo declaraba ``ARTEFACTO_AUSENTE``.
    """
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(tmp_path / "profiles" / "Default", [])
    en_overwrite = tmp_path / "overwrite" / BASHED_PATCH_NAME
    _parche(en_overwrite, b"parche-construido-por-el-operador")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["success"] is False and resultado["pendiente"] is True
    assert resultado["testigo_previo"] == {
        "existia": True,
        "sha256": huella_de_artefacto(en_overwrite).sha256,
    }, "el testigo tiene que describir el artefacto del OPERADOR, no el Data físico"


@pytest.mark.asyncio
async def test_verificar_encuentra_el_parche_en_un_mod_de_salida(tmp_path: pathlib.Path) -> None:
    """Igual que arriba pero el patch quedó dentro de un mod habilitado del perfil."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(tmp_path / "profiles" / "Default", ["SalidaBashed"])
    _parche(tmp_path / "mods" / "SalidaBashed" / BASHED_PATCH_NAME)
    servicio = _con_resolver_real(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(profile="Default")

    assert veredicto["success"] is True, f"artefacto en un mod del perfil: {veredicto}"
    assert veredicto["motivo"] == MotivoAsistido.OK.value
    assert pathlib.Path(str(veredicto["artefacto"])).parent.parent == tmp_path / "mods"


@pytest.mark.asyncio
async def test_el_overlay_respeta_la_prioridad_del_perfil(tmp_path: pathlib.Path) -> None:
    """Dos mods proveen el patch: gana el de MAYOR prioridad (el último del modlist)."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(tmp_path / "profiles" / "Default", ["Baja", "Alta"])
    _parche(tmp_path / "mods" / "Baja" / BASHED_PATCH_NAME, b"copia-vieja-de-baja")
    ganador = _parche(tmp_path / "mods" / "Alta" / BASHED_PATCH_NAME, b"copia-que-carga-el-juego")
    servicio = _con_resolver_real(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(profile="Default")

    assert veredicto["success"] is True
    assert pathlib.Path(str(veredicto["artefacto"])) == ganador
    assert veredicto["sha256"] == huella_de_artefacto(ganador).sha256


@pytest.mark.asyncio
async def test_overwrite_gana_a_cualquier_mod(tmp_path: pathlib.Path) -> None:
    """``overwrite`` está por encima de todo mod en el overlay de MO2."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(tmp_path / "profiles" / "Default", ["SalidaBashed"])
    _parche(tmp_path / "mods" / "SalidaBashed" / BASHED_PATCH_NAME, b"copia-del-mod")
    en_overwrite = _parche(tmp_path / "overwrite" / BASHED_PATCH_NAME, b"copia-de-overwrite")
    servicio = _con_resolver_real(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(profile="Default")

    assert pathlib.Path(str(veredicto["artefacto"])) == en_overwrite


@pytest.mark.asyncio
async def test_un_modlist_ilegible_no_afirma_ausencia(tmp_path: pathlib.Path) -> None:
    """Sin poder enumerar las capas, "no está" es una afirmación sin evidencia."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("esto no es un modlist\n", encoding="utf-8")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.verificar_etapa_asistida(profile="Default")

    assert resultado["success"] is False
    assert resultado["reason"] == "CoberturaDeOverlayIncompleta", resultado
    assert "motivo" not in resultado or resultado["motivo"] != MotivoAsistido.ARTEFACTO_AUSENTE.value


# --- C3/C5: un artefacto sin identidad no es "no estaba" --------------------------


def test_un_artefacto_presente_sin_identidad_no_produce_testigo(tmp_path: pathlib.Path) -> None:
    """Un directorio (o un enlace) existe, pero no identifica contenido: no es un testigo."""
    directorio = tmp_path / BASHED_PATCH_NAME
    directorio.mkdir()

    huella = huella_de_artefacto(directorio)

    assert huella.existe is True and huella.sha256 is None
    assert TestigoPrevio.desde(huella) is None, "afirmar 'no estaba' sobre algo que está es un falso negativo"


def test_el_testigo_rechaza_la_combinacion_imposible() -> None:
    """``{existia: True, sha256: None}`` no significa nada y no puede construirse."""
    with pytest.raises(ValueError, match="identidad"):
        TestigoPrevio(existia=True, sha256=None)


@pytest.mark.asyncio
async def test_preparar_con_artefacto_no_regular_no_inventa_ausencia(tmp_path: pathlib.Path) -> None:
    """La fase 1 declara ``testigo_previo=None`` (no sé), no ``existia=False`` (no estaba)."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    (tmp_path / "Skyrim" / "Data" / BASHED_PATCH_NAME).mkdir()
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["testigo_previo"] is None


@pytest.mark.asyncio
async def test_un_testigo_imposible_no_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """Un cliente que devuelva ``{existia: True, sha256: None}`` no demuestra frescura.

    Ese testigo hacía ``sha256 != huella.sha256`` y declaraba frescura probada sin que la
    fase 1 hubiera identificado nunca el archivo: el veredicto salía de un dato que se
    contradecía a sí mismo.
    """
    artefacto, _ = _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _parche(artefacto, b"parche-de-esta-corrida")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default", testigo_previo={"existia": True, "sha256": None}
    )

    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is False


# --- C4: un byte inválido en cualquier parte vuelve ilegible el plugins.txt --------


def test_un_byte_invalido_ajeno_al_plugin_vuelve_ilegible_el_archivo(tmp_path: pathlib.Path) -> None:
    """El decodificado era por entrada y con ``replace``: una línea corrupta se colaba."""
    _, plugins = _perfil_con_plugins(tmp_path, "")
    plugins.write_bytes(f"# comentario roto \xff\n*Skyrim.esm\n*{BASHED_PATCH_NAME}\n".encode("latin-1"))

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.ILEGIBLE


def test_la_lectura_concuerda_con_el_consumidor_aguas_abajo_en_utf8_invalido(tmp_path: pathlib.Path) -> None:
    """Concordancia con ``_plugins_habilitados``: mismo archivo, misma conclusión.

    ``etapas_previas_lods`` (etapa 9) lee el mismo ``plugins.txt`` con ``utf-8-sig``
    estricto y devuelve ``None``. Si esta etapa respondiera ``HABILITADO``, la 6 diría
    que el patch está activo y la 9 se negaría a correr: dos verdades sobre el mismo
    archivo.
    """
    _, plugins = _perfil_con_plugins(tmp_path, "")
    plugins.write_bytes(b"*Skyrim.esm\n\xc3\x28\n*" + BASHED_PATCH_NAME.encode() + b"\n")

    assert _plugins_habilitados(plugins) is None
    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.ILEGIBLE


def test_el_bom_sigue_siendo_tolerado_despues_de_la_validacion_estricta(tmp_path: pathlib.Path) -> None:
    """La validación estricta no reintroduce la divergencia de F2: el BOM sigue aceptado."""
    _, plugins = _perfil_con_plugins(tmp_path, "")
    plugins.write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*" + BASHED_PATCH_NAME.encode() + b"\r\n")

    assert estado_en_plugins_txt(plugins, BASHED_PATCH_NAME) is EstadoEnPlugins.HABILITADO
    assert BASHED_PATCH_NAME.lower() in (_plugins_habilitados(plugins) or set())


# --- C6: el camino de excepción también adjunta el preflight ----------------------


@pytest.mark.asyncio
async def test_la_excepcion_inesperada_conserva_el_semaforo(tmp_path: pathlib.Path) -> None:
    """Era el ÚNICO retorno de la fase 1 sin ``_attach_preflight``."""
    _perfil_con_plugins(tmp_path, f"*{BASHED_PATCH_NAME}\n")
    amarillo = MagicMock()
    amarillo.blocks_mutations = False
    amarillo.status.value = "yellow"
    amarillo.to_dict.return_value = {"status": "yellow", "checks": []}
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=amarillo)
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(tmp_path / "Skyrim", tmp_path),
        preflight=preflight,
    )

    with patch("sky_claw.local.tools.wrye_bash_service.preparar_etapa_asistida", side_effect=RuntimeError("boom")):
        resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["reason"] == "ErrorInesperado"
    assert resultado["preflight"] == {"status": "yellow", "checks": []}


# --- C7 (rechazado): ILEGIBLE antes que NO_REGULAR es la opción conservadora ------


def test_un_artefacto_que_no_se_pudo_inspeccionar_no_afirma_que_sea_no_regular(tmp_path: pathlib.Path) -> None:
    """Rechazo razonado de reordenar la precedencia.

    Cuando el ``lstat`` falla no sabemos QUÉ es la ruta: la huella queda
    ``existe=True, es_regular=False, ilegible=True``. Decir "no es un archivo regular"
    sería afirmar una propiedad que no se observó; ``ARTEFACTO_ILEGIBLE`` ("no se pudo
    leer") es lo único que el dato sostiene. Para un directorio —el caso en que SÍ se
    observó la forma— ``ilegible`` queda en ``False`` y gana ``NO_REGULAR``.
    """
    sin_inspeccion = HuellaDeArtefacto(
        ruta=str(tmp_path / BASHED_PATCH_NAME),
        existe=True,
        es_enlace=False,
        es_regular=False,
        ilegible=True,
        bytes=0,
        sha256=None,
    )
    observado_como_directorio = HuellaDeArtefacto(
        ruta=str(tmp_path / BASHED_PATCH_NAME),
        existe=True,
        es_enlace=False,
        es_regular=False,
        ilegible=False,
        bytes=0,
        sha256=None,
    )

    def _motivo(huella: HuellaDeArtefacto) -> MotivoAsistido:
        observacion = _Observacion(huella=huella, estado_plugins=EstadoEnPlugins.HABILITADO, testigo=None)
        return next(motivo for motivo, regla in REGLAS_DEL_VEREDICTO if regla(observacion))

    assert _motivo(sin_inspeccion) is MotivoAsistido.ARTEFACTO_ILEGIBLE
    assert _motivo(observado_como_directorio) is MotivoAsistido.ARTEFACTO_NO_REGULAR


# --- Ronda 3 (CodeRabbit sobre `6db53c4`) ----------------------------------------


def test_el_mensaje_de_ausente_nombra_las_capas_que_se_miraron() -> None:
    """Si el veredicto buscó en tres capas, el mensaje no puede nombrar sólo una.

    Decir "no existe en el Data del juego" sobre una resolución que ya revisó
    ``overwrite`` y los mods habilitados manda al operador a mirar donde el sistema ya
    sabe que no está.
    """
    mensaje = _MENSAJES[MotivoAsistido.ARTEFACTO_AUSENTE]

    assert "overwrite" in mensaje
    assert "mods habilitados" in mensaje
    assert "Data del juego" in mensaje


@pytest.mark.asyncio
async def test_un_modlist_que_no_se_puede_estatuiar_no_es_un_perfil_sin_mods(tmp_path: pathlib.Path) -> None:
    """``is_file()`` también devuelve ``False`` para un directorio: no prueba ausencia.

    Un ``modlist.txt`` que es un directorio (o cuyo ``stat`` falla) no significa "el
    perfil no habilita mods": significa que no se pudo leer, y entonces el veredicto no
    puede afirmar que el artefacto no esté en ninguna capa.
    """
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    (tmp_path / "profiles" / "Default" / "modlist.txt").mkdir()
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    assert servicio._mods_habilitados("Default") is None
    resultado = await servicio.verificar_etapa_asistida(profile="Default")

    assert resultado["reason"] == "CoberturaDeOverlayIncompleta"


# --- Ronda 3b (PR-Agent sobre `6db53c4`): permisos y cobertura del overlay --------


def test_los_permisos_se_miden_donde_escribe_cada_ejecutor(tmp_path: pathlib.Path) -> None:
    """La VFS **redirige** la escritura: no hereda los permisos del destino físico.

    En una instalación de Steam el ``Data`` del juego suele estar bajo ``Program Files``
    y ser de sólo lectura —el escenario para el que existe MO2—. Sondearlo desde el
    dominio USVFS da un rojo crítico y bloquea un perfil sano; el destino real de esa
    escritura es el ``overwrite`` del perfil.
    """
    game, mo2 = tmp_path / "Skyrim", tmp_path / "mo2"
    (game / "Data").mkdir(parents=True)
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(game, mo2),
    )

    fisico = servicio._permission_targets(dominio=DOMINIO_FISICO)
    asistido = servicio._permission_targets(dominio=DOMINIO_USVFS)

    assert fisico == [game / "Data"]
    assert asistido == [mo2 / "overwrite"]
    assert game / "Data" not in asistido


@pytest.mark.asyncio
async def test_un_data_de_solo_lectura_no_bloquea_la_etapa_asistida(tmp_path: pathlib.Path) -> None:
    """Conducta: el ``Data`` deniega escritura y el perfil se prepara igual.

    El probe de permisos se sustituye por uno que deniega **sólo** las rutas bajo el
    ``Data`` físico, que es lo que hace un ``Data`` read-only de verdad (el sandbox de
    CI corre como root, así que un ``chmod`` no reproduciría el fallo).
    """
    from sky_claw.local.validators.write_permissions import WriteAccessIssue, WritePermissionsChecker

    game, mo2 = tmp_path / "Skyrim", tmp_path / "mo2"
    (game / "Data").mkdir(parents=True)
    perfil = mo2 / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _escribir_modlist(perfil, ["SoloEnMods"])
    (mo2 / "mods" / "SoloEnMods").mkdir(parents=True)
    (mo2 / "overwrite").mkdir(parents=True)
    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=_resolver_real(game, mo2),
    )

    def _probe_denegado_en_data(target: pathlib.Path) -> WriteAccessIssue | None:
        if pathlib.Path(target) == game / "Data":
            return WriteAccessIssue(path=str(target), kind="denied", severity="critical", remediation="read-only")
        return None

    with patch.object(WritePermissionsChecker, "_probe_write", side_effect=_probe_denegado_en_data):
        fisico = await servicio._ensure_preflight(dominio=DOMINIO_FISICO).run()
        asistido = await servicio._ensure_preflight(dominio=DOMINIO_USVFS).run()

    assert fisico.blocks_mutations is True, "el Data read-only sí bloquea al runner spawneado: escribe ahí de verdad"
    assert asistido.blocks_mutations is False, "el operador escribe en el overwrite: el Data read-only no lo afecta"


def test_un_acierto_en_overwrite_es_concluyente_ante_cobertura_parcial(tmp_path: pathlib.Path) -> None:
    """Aunque no se puedan enumerar los mods, ``overwrite`` gana a todo: el ganador es único."""
    game, mo2 = tmp_path / "game", tmp_path / "mo2"
    (game / "Data").mkdir(parents=True)
    (mo2 / "overwrite").mkdir(parents=True)
    _parche(mo2 / "overwrite" / BASHED_PATCH_NAME, b"copia-de-overwrite")

    encontrado = bashed_patch_en_overlay(game=game, mo2=mo2, mods_dir=None, mods_habilitados=None)

    assert encontrado is not None
    assert encontrado.capa == "overwrite"
    assert encontrado.cobertura_completa is True


# ==============================================================================
# 10. Revisión adversarial del PR (ronda 4) — asincronía, perfiles y mutantes
# ==============================================================================


async def test_preflight_se_construye_con_el_perfil_pedido(tmp_path: pathlib.Path) -> None:
    """A-2: el preflight debe evaluar el perfil solicitado por el llamador, no el activo."""
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    mo2.mkdir(parents=True)
    _perfil_con_plugins(mo2, f"*{BASHED_PATCH_NAME}\n", perfil="Pedido")

    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_skyrim_path_raw.return_value = str(game)
    resolver.get_mo2_instance_data_root.return_value = mo2
    resolver.get_mo2_mods_path_best_effort.return_value = mo2 / "mods"
    resolver.get_active_profile.return_value = "Activo"

    servicio = WryeBashPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
    )

    with patch("sky_claw.local.validators.preflight_sensors.build_mo2_profile_sources_resolver") as mock_sources:
        mock_sources.return_value = None
        await servicio.preparar_etapa_asistida(profile="Pedido")
        assert mock_sources.call_count == 1
        _, kwargs = mock_sources.call_args
        assert kwargs["profile"] == "Pedido", "debe usar el perfil pedido, no el activo ('Activo')"


async def test_un_modlist_con_permission_error_no_es_un_perfil_sin_mods(tmp_path: pathlib.Path) -> None:
    """A-3: si lstat eleva PermissionError/OSError, _mods_habilitados devuelve None, no ()."""
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)
    with patch.object(pathlib.Path, "lstat", side_effect=PermissionError(13, "denegado")):
        assert servicio._mods_habilitados("Default") is None


async def test_la_fase_1_aborta_si_el_guard_m04_falla(tmp_path: pathlib.Path) -> None:
    """A-3: si plugin_limit_guard indica que el límite de plugins falló, aborta preparación."""
    guard_mock = AsyncMock(return_value={"valid": False, "error": "M-04 límite excedido", "plugin_count": 256})
    servicio = _servicio(tmp_path / "Skyrim", tmp_path, guard=guard_mock)
    resultado = await servicio.preparar_etapa_asistida(profile="Default")
    assert resultado["success"] is False
    assert resultado["aborted_by"] == "plugin_limit_guard"
    assert resultado["plugin_count"] == 256
    assert "M-04" in resultado["error"]


def test_testigo_previo_rechaza_sha256_sintacticamente_invalido() -> None:
    """A-4: TestigoPrevio rechaza digests que no sean hex de 64 caracteres."""
    with pytest.raises(ValueError, match="digest hexadecimal"):
        TestigoPrevio(existia=True, sha256="no-es-un-digest")
    with pytest.raises(ValueError, match="digest hexadecimal"):
        TestigoPrevio(existia=True, sha256="abc")


async def test_testigo_fabricado_no_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """A-4: un testigo fabricado con digest no hex es descartado y declara frescura no probada."""
    _perfil_con_plugins(tmp_path, f"*Skyrim.esm\n*{BASHED_PATCH_NAME}\n")
    _parche(tmp_path / "overwrite" / BASHED_PATCH_NAME, b"contenido-nuevo")
    servicio = _servicio(tmp_path / "Skyrim", tmp_path)

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default",
        testigo_previo={"existia": True, "sha256": "no-es-un-digest"},
    )
    assert veredicto["success"] is True
    assert veredicto["motivo"] == MotivoAsistido.OK.value
    assert veredicto["frescura_probada"] is False, "testigo inválido no debe probar frescura"


def test_bashed_patch_en_overlay_saltea_capa_con_permission_error(tmp_path: pathlib.Path) -> None:
    """M-1: una capa donde lstat falla con PermissionError no eleva excepción; degrada cobertura."""
    game, mo2 = tmp_path / "game", tmp_path / "mo2"
    (game / "Data").mkdir(parents=True)
    _parche(game / "Data" / BASHED_PATCH_NAME, b"en-data")
    mods = mo2 / "mods"
    (mods / "ModBloqueado").mkdir(parents=True)
    _parche(mods / "ModBloqueado" / BASHED_PATCH_NAME, b"en-mod")

    original_lstat = os.lstat

    def _failing_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if "ModBloqueado" in str(path):
            raise PermissionError(13, "denegado")
        return original_lstat(path, *args, **kwargs)

    with patch("os.lstat", side_effect=_failing_lstat):
        resultado = bashed_patch_en_overlay(
            game=game,
            mo2=mo2,
            mods_dir=mods,
            mods_habilitados=("ModBloqueado",),
        )

    assert resultado is not None
    assert resultado.capa == CAPA_DATA_JUEGO
    assert resultado.cobertura_completa is False, "capa bloqueada degrada la cobertura"
