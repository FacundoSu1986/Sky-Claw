"""NP-UA-1: el veredicto de un lote se decide por ``veredicto``, nunca por ``estado``.

Qué se verifica y con qué:

- **El caso que el código de salida del generador no ve.** Ocho texturas con
  ``estado: ESCRITA``, ``efecto: PUBLICADA`` y ``veredicto: REVIEW`` son exactamente la forma
  del informe real del rig (issue #792). El contador del generador dice ``EN_REVISION = 0``;
  este módulo dice ``REVIEW`` y devuelve un código distinto de 0. El test afirma **las dos
  cosas a la vez** para que quede escrito que no se contradicen: son dos preguntas distintas.
- **``conteos`` no es fuente de verdad.** Se recalcula desde ``archivos`` y una divergencia es
  ``INCONSISTENTE``, no un ``PASS``.
- **La precedencia está enumerada.** Inconsistente gana sobre fallo, y fallo sobre revisión,
  porque un informe que se contradice no certifica ni siquiera el fallo.
- **Cada motivo pertenece a exactamente un grupo.** Un motivo nuevo rompe la enumeración
  hasta que se decida si es de inconsistencia, de falla o de revisión.
- **Los códigos de salida son distintos y sólo ``PASS`` es 0.**

Límite declarado: este módulo no abre archivos. Que el artefacto exista y tenga relieve lo
verifica ``test_parallax_unattended_auditor.py``.
"""

from __future__ import annotations

import pytest

from sky_claw.local.native_parallax.gates import Veredicto as VeredictoDelGenerador
from sky_claw.local.native_parallax.lote import VERSION_DEL_LOTE, EstadoDeArchivo
from sky_claw.local.native_parallax.output_writer import EfectoDeSalida
from sky_claw.local.parallax_unattended.veredicto import (
    CODIGOS_DE_SALIDA,
    EFECTOS_CONOCIDOS,
    EFECTOS_FALLIDOS,
    EFECTOS_QUE_DEJAN_ARCHIVO,
    ESTADOS_CONOCIDOS,
    MOTIVOS_DE_FALLA,
    MOTIVOS_DE_INCONSISTENCIA,
    MOTIVOS_DE_REVISION,
    VEREDICTOS_CONOCIDOS,
    VERSIONES_DEL_LOTE_CONOCIDAS,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
    dictar,
)

_HUELLA = "d" * 64
_ENCODER = {"texconv_version": "may2026", "texconv_sha256": _HUELLA}


def _archivo(
    ruta: str = "textures/a_p.dds",
    *,
    estado: str = "ESCRITA",
    efecto: str | None = "PUBLICADA",
    veredicto: str | None = "AUTO_ACCEPT",
) -> dict[str, object]:
    return {
        "ruta_de_origen": "textures/a_n.dds",
        "ruta_de_salida": ruta,
        "estado": estado,
        "efecto": efecto,
        "veredicto": veredicto,
        "motivos": [],
        "metricas": None,
        "detalle": "",
    }


def _conteos(archivos: list[dict[str, object]]) -> dict[str, int]:
    cuenta = {estado: 0 for estado in sorted(ESTADOS_CONOCIDOS)}
    for archivo in archivos:
        cuenta[str(archivo["estado"])] += 1
    return cuenta


def _informe(
    archivos: list[dict[str, object]],
    *,
    otras_rutas: list[dict[str, object]] | None = None,
    conteos: dict[str, int] | None = None,
    encoder: dict[str, str] | None = _ENCODER,
    omitidas: list[str] | None = None,
) -> dict[str, object]:
    return {
        "version_del_lote": "np-a0a.v1",
        "parametros": {},
        "encoder": encoder,
        "conteos": _conteos(archivos) if conteos is None else conteos,
        "archivos": archivos,
        "otras_rutas": [] if otras_rutas is None else otras_rutas,
        "omitidas_al_enumerar": [] if omitidas is None else omitidas,
    }


# ---------------------------------------------------------------------------
# El defecto que este módulo existe para cerrar
# ---------------------------------------------------------------------------


def test_ocho_texturas_en_revision_escritas_no_son_pass() -> None:
    """La forma exacta del informe real: todo escrito, todo en revisión, contador en cero."""

    archivos = [_archivo(f"textures/t{i}_p.dds", veredicto="REVIEW") for i in range(8)]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.REVIEW
    assert decision.conteos["EN_REVISION"] == 0, "el contador del generador cuenta estado, no veredicto"
    assert decision.conteos["ESCRITA"] == 8
    assert len(decision.rutas) == 8
    assert codigo_de_salida(decision) != 0


def test_una_sola_en_revision_alcanza_para_no_ser_pass() -> None:
    archivos = [_archivo("textures/a_p.dds"), _archivo("textures/b_p.dds", veredicto="REVIEW")]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.REVIEW
    assert decision.rutas == ("textures/b_p.dds",)


def test_lote_limpio_es_pass() -> None:
    decision = dictar(_informe([_archivo("textures/a_p.dds"), _archivo("textures/b_p.dds")]))

    assert decision.veredicto is VeredictoDelLote.PASS
    assert decision.motivos == ()
    assert codigo_de_salida(decision) == 0


# ---------------------------------------------------------------------------
# conteos no es fuente de verdad
# ---------------------------------------------------------------------------


def test_conteos_que_no_cuadran_son_inconsistentes() -> None:
    archivos = [_archivo("textures/a_p.dds")]
    mentira = {"EN_REVISION": 0, "ESCRITA": 7, "NO_ESCRITA": 0, "NO_PROCESADA": 0}
    decision = dictar(_informe(archivos, conteos=mentira))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.CONTEO_DIVERGENTE in decision.motivos
    assert decision.conteos["ESCRITA"] == 1, "los conteos devueltos son los recalculados"


def test_un_estado_nuevo_en_el_informe_rompe_el_conteo() -> None:
    archivos = [_archivo("textures/a_p.dds")]
    con_estado_extra = {"EN_REVISION": 0, "ESCRITA": 1, "NO_ESCRITA": 0, "NO_PROCESADA": 0, "ARCHIVADA": 3}
    decision = dictar(_informe(archivos, conteos=con_estado_extra))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.CONTEO_DIVERGENTE in decision.motivos


# ---------------------------------------------------------------------------
# Precedencia
# ---------------------------------------------------------------------------


def test_inconsistente_gana_sobre_falla() -> None:
    """Si el informe se contradice, ni siquiera el fallo es certificable."""

    archivos = [_archivo("textures/a_p.dds", estado="NO_ESCRITA", efecto=None, veredicto=None)]
    mentira = {"EN_REVISION": 0, "ESCRITA": 0, "NO_ESCRITA": 0, "NO_PROCESADA": 0}
    decision = dictar(_informe(archivos, conteos=mentira))

    assert MotivoDelLote.ARCHIVO_NO_ESCRITO in decision.motivos
    assert MotivoDelLote.CONTEO_DIVERGENTE in decision.motivos
    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE


def test_falla_gana_sobre_revision() -> None:
    archivos = [
        _archivo("textures/a_p.dds", veredicto="REVIEW"),
        _archivo("textures/b_p.dds", estado="NO_ESCRITA", efecto=None, veredicto=None),
    ]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.FAIL
    assert decision.rutas == ("textures/a_p.dds", "textures/b_p.dds")


# ---------------------------------------------------------------------------
# Trabajo perdido
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["NO_PROCESADA", "NO_ESCRITA"])
def test_un_archivo_perdido_es_falla(estado: str) -> None:
    archivos = [_archivo("textures/a_p.dds", estado=estado, efecto=None, veredicto=None)]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.FAIL
    assert decision.motivos


def test_lote_vacio_es_falla() -> None:
    decision = dictar(_informe([]))

    assert decision.veredicto is VeredictoDelLote.FAIL
    assert MotivoDelLote.LOTE_VACIO in decision.motivos


# ---------------------------------------------------------------------------
# Codificador no registrado
# ---------------------------------------------------------------------------


def test_artefacto_publicado_sin_registro_del_codificador_es_inconsistente() -> None:
    decision = dictar(_informe([_archivo("textures/a_p.dds")], encoder=None))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.ENCODER_NO_REGISTRADO in decision.motivos


def test_sin_artefacto_el_codificador_no_hace_falta() -> None:
    """Nada se escribió: no hay artefacto que atribuir a un binario."""

    archivos = [_archivo("textures/a_p.dds", estado="EN_REVISION", efecto=None, veredicto="REVIEW")]
    decision = dictar(_informe(archivos, encoder=None))

    assert decision.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.ENCODER_NO_REGISTRADO not in decision.motivos


def test_encoder_ausente_con_otras_rutas_omitida_marca_inconsistente() -> None:
    """Si no hay archivos nuevos pero otras_rutas declara un artefacto omitido por caché, el encoder se exige."""

    archivos = [_archivo("textures/a_p.dds", estado="EN_REVISION", efecto=None, veredicto="REVIEW")]
    otras_rutas = [{"ruta": "textures/previa_p.dds", "efecto": "OMITIDA", "detalle": "caché"}]
    decision = dictar(_informe(archivos, otras_rutas=otras_rutas, encoder=None))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.ENCODER_NO_REGISTRADO in decision.motivos


def test_en_revision_con_veredicto_auto_accept_es_incoherente() -> None:
    """El estado dice que el gate la retuvo y el veredicto dice que se auto-aceptó: una de las dos miente."""

    archivos = [_archivo("textures/a_p.dds", estado="EN_REVISION", efecto=None, veredicto="AUTO_ACCEPT")]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.VEREDICTO_INCOHERENTE in decision.motivos


def test_veredicto_pass_en_archivo_se_rechaza_como_forma_invalida() -> None:
    """El generador emite ``AUTO_ACCEPT``, nunca ``PASS``, en ``archivos[i].veredicto``."""

    archivos = [_archivo("textures/a_p.dds", veredicto="PASS")]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


# ---------------------------------------------------------------------------
# Forma y versión del contrato
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [None, "", 123, "unknown-v99"])
def test_version_del_lote_ausente_o_desconocida_es_forma_invalida(version: object) -> None:
    informe = _informe([_archivo("textures/a_p.dds")])
    if version is None:
        del informe["version_del_lote"]
    else:
        informe["version_del_lote"] = version

    decision = dictar(informe)

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


@pytest.mark.parametrize("no_mapeo", [None, [], "texto", 42])
def test_informe_que_no_es_mapping_no_lanza_y_es_forma_invalida(no_mapeo: object) -> None:
    decision = dictar(no_mapeo)  # type: ignore[arg-type]

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


def test_archivo_escrito_sin_veredicto_es_inconsistente() -> None:
    decision = dictar(_informe([_archivo("textures/a_p.dds", veredicto=None)]))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.ARCHIVO_SIN_VEREDICTO in decision.motivos


def test_estado_escrito_con_efecto_que_no_deja_archivo_es_inconsistente() -> None:
    """``ESCRITA`` + ``RETIRADA`` se contradicen: o está, o se retiró."""

    decision = dictar(_informe([_archivo("textures/a_p.dds", efecto="RETIRADA")]))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.FORMA_INVALIDA in decision.motivos


@pytest.mark.parametrize(
    ("estado", "efecto", "veredicto"),
    [
        ("EN_REVISION", "PUBLICADA", "REVIEW"),
        ("EN_REVISION", "RETIRADA", "REVIEW"),
        ("NO_PROCESADA", "PUBLICADA", None),
        ("NO_PROCESADA", "FALLIDA", None),
        ("NO_ESCRITA", "PUBLICADA", "AUTO_ACCEPT"),
        ("NO_ESCRITA", "REEMPLAZADA", "AUTO_ACCEPT"),
        ("NO_ESCRITA", "OMITIDA", "AUTO_ACCEPT"),
        ("ESCRITA", "INVENTADO", "AUTO_ACCEPT"),
    ],
)
def test_par_estado_efecto_contradictorio_o_desconocido_es_inconsistente(
    estado: str, efecto: str, veredicto: str | None
) -> None:
    decision = dictar(_informe([_archivo("textures/a_p.dds", estado=estado, efecto=efecto, veredicto=veredicto)]))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.FORMA_INVALIDA in decision.motivos


def test_rutas_de_salida_duplicadas_sin_distinguir_mayusculas_son_forma_invalida() -> None:
    archivos = [_archivo("textures/Stone_p.dds"), _archivo("textures/stone_p.dds")]
    decision = dictar(_informe(archivos))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


@pytest.mark.parametrize(
    "encoder_invalido",
    [
        {"texconv_version": "may2026", "texconv_sha256": "d" * 63},
        {"texconv_version": "may2026", "texconv_sha256": "d" * 65},
        {"texconv_version": "may2026", "texconv_sha256": "D" * 64},
        {"texconv_version": "may2026", "texconv_sha256": "z" * 64},
        {"texconv_version": "may2026", "texconv_sha256": ("d" * 64) + "\n"},
        {"texconv_version": "", "texconv_sha256": _HUELLA},
        {"texconv_version": "   ", "texconv_sha256": _HUELLA},
        {"texconv_sha256": _HUELLA},
    ],
)
def test_encoder_con_huella_o_version_invalida_no_cuenta_como_registrado(encoder_invalido: dict[str, str]) -> None:
    decision = dictar(_informe([_archivo("textures/a_p.dds")], encoder=encoder_invalido))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.ENCODER_NO_REGISTRADO in decision.motivos


@pytest.mark.parametrize("efecto_fallido", ["CONFLICTO", "FALLIDA"])
def test_otras_rutas_con_conflicto_o_fallida_degradan_a_fail(efecto_fallido: str) -> None:
    informe = _informe([_archivo("textures/a_p.dds")])
    informe["otras_rutas"] = [{"ruta": "textures/vieja_p.dds", "efecto": efecto_fallido, "detalle": "fallo"}]

    decision = dictar(informe)

    assert decision.veredicto is VeredictoDelLote.FAIL
    assert MotivoDelLote.ARCHIVO_NO_ESCRITO in decision.motivos
    assert "textures/vieja_p.dds" in decision.rutas


@pytest.mark.parametrize(
    "otras_invalidas",
    [
        "no-es-lista",
        ["no-es-objeto"],
        [{"ruta": "textures/A_p.dds", "efecto": "OMITIDA"}],
        [{"ruta": "textures/vieja_p.dds", "efecto": "PUBLICADA"}],
        [{"ruta": "textures/vieja_p.dds", "efecto": "DESCONOCIDO"}],
    ],
)
def test_otras_rutas_con_forma_o_colision_invalida_es_inconsistente(otras_invalidas: object) -> None:
    informe = _informe([_archivo("textures/a_p.dds")])
    informe["otras_rutas"] = otras_invalidas

    decision = dictar(informe)

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


@pytest.mark.parametrize(
    "amplitud_invalida",
    [
        "no-es-objeto",
        {"desviacion": None},
        {"desviacion": True},
        {"desviacion": -0.01},
        {"desviacion": 1.01},
        {"desviacion": float("nan")},
    ],
)
def test_amplitud_con_desviacion_invalida_es_forma_invalida(amplitud_invalida: object) -> None:
    entrada = _archivo("textures/a_p.dds")
    entrada["amplitud"] = amplitud_invalida

    decision = dictar(_informe([entrada]))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


def test_estado_desconocido_es_forma_invalida() -> None:
    """El vocabulario de estados es cerrado: uno nuevo se rechaza antes de mirar los conteos."""

    decision = dictar(_informe([_archivo("textures/a_p.dds", estado="ARCHIVADA")], conteos={}))

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)


def test_informe_sin_archivos_es_forma_invalida() -> None:
    decision = dictar({"conteos": {}, "encoder": None})

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.FORMA_INVALIDA in decision.motivos


# ---------------------------------------------------------------------------
# Anclas enumerativas
# ---------------------------------------------------------------------------


def test_todo_motivo_pertenece_a_exactamente_un_grupo() -> None:
    """Un motivo nuevo sin grupo deja el veredicto sin precedencia definida."""

    for motivo in MotivoDelLote:
        grupos = sum(
            1 for grupo in (MOTIVOS_DE_INCONSISTENCIA, MOTIVOS_DE_FALLA, MOTIVOS_DE_REVISION) if motivo in grupo
        )
        assert grupos == 1, f"{motivo} está en {grupos} grupos"


def test_los_estados_conocidos_son_los_cuatro_del_generador() -> None:
    assert {"ESCRITA", "EN_REVISION", "NO_PROCESADA", "NO_ESCRITA"} == ESTADOS_CONOCIDOS
    assert {e.name for e in EstadoDeArchivo} == ESTADOS_CONOCIDOS


def test_los_veredictos_conocidos_son_los_del_generador() -> None:
    """Ancla contra drift: el paquete no importa ``native_parallax``, pero el test congela que coincidan."""

    assert {"AUTO_ACCEPT", "REVIEW"} == VEREDICTOS_CONOCIDOS
    assert {v.name for v in VeredictoDelGenerador} == VEREDICTOS_CONOCIDOS


def test_las_versiones_y_efectos_conocidos_coinciden_con_el_generador() -> None:
    assert {"np-a0a.v1"} == VERSIONES_DEL_LOTE_CONOCIDAS
    assert VERSION_DEL_LOTE in VERSIONES_DEL_LOTE_CONOCIDAS
    assert {e.name for e in EfectoDeSalida} == EFECTOS_CONOCIDOS
    assert {"PUBLICADA", "REEMPLAZADA", "OMITIDA"} == EFECTOS_QUE_DEJAN_ARCHIVO
    assert {"CONFLICTO", "FALLIDA"} == EFECTOS_FALLIDOS
    assert {"RETIRADA", "PRESERVADA", "SOLTADA"} == (EFECTOS_CONOCIDOS - EFECTOS_QUE_DEJAN_ARCHIVO - EFECTOS_FALLIDOS)


@pytest.mark.parametrize("veredicto", list(VeredictoDelLote))
def test_solo_pass_devuelve_cero(veredicto: VeredictoDelLote) -> None:
    if veredicto is VeredictoDelLote.PASS:
        assert CODIGOS_DE_SALIDA[veredicto] == 0
    else:
        assert CODIGOS_DE_SALIDA[veredicto] != 0


def test_no_hay_dos_veredictos_con_el_mismo_codigo() -> None:
    codigos = list(CODIGOS_DE_SALIDA.values())

    assert len(codigos) == len(set(codigos))
    assert codigos.count(0) == 1


def test_los_motivos_no_se_repiten() -> None:
    archivos = [_archivo(f"textures/t{i}_p.dds", veredicto="REVIEW") for i in range(5)]
    decision = dictar(_informe(archivos))

    assert len(decision.motivos) == len(set(decision.motivos))


# ---------------------------------------------------------------------------
# Censo de lo omitido al enumerar: un lote que dejó texturas afuera no es PASS
# ---------------------------------------------------------------------------


def _archivo_escrito_ok() -> dict[str, object]:
    return {
        "ruta_de_salida": "textures/a_p.dds",
        "estado": "ESCRITA",
        "efecto": "PUBLICADA",
        "veredicto": "AUTO_ACCEPT",
    }


def test_un_lote_con_texturas_omitidas_al_enumerar_es_review_y_no_pass() -> None:
    """Sin el censo, 1 textura procesada de 12 daba el mismo PASS que el lote completo."""

    decision = dictar(_informe([_archivo_escrito_ok()], omitidas=["textures/b_n.dds: no se pudo inspeccionar"]))

    assert decision.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.RUTAS_OMITIDAS in decision.motivos
    assert MotivoDelLote.RUTAS_OMITIDAS in MOTIVOS_DE_REVISION


def test_un_informe_sin_censo_de_omitidas_es_inconsistente() -> None:
    """Un informe del contrato viejo (sin el campo) no puede probar que no se omitió nada: falla cerrado."""

    informe = _informe([_archivo_escrito_ok()])
    del informe["omitidas_al_enumerar"]

    decision = dictar(informe)

    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.FORMA_INVALIDA in decision.motivos


@pytest.mark.parametrize("basura", [None, "x", 3, {"a": 1}, [1], [None], ["ok", 2]])
def test_un_censo_de_omitidas_con_forma_invalida_es_inconsistente(basura: object) -> None:
    informe = _informe([_archivo_escrito_ok()])
    informe["omitidas_al_enumerar"] = basura

    assert dictar(informe).veredicto is VeredictoDelLote.INCONSISTENTE
