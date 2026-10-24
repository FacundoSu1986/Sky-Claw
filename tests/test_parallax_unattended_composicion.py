"""NP-UA-3: el veredicto final cruza el informe con el disco; ninguna mitad alcanza sola.

Qué se verifica y con qué:

- **Un lote ``PASS`` con un artefacto plano no es ``PASS``.** Es el agujero que queda si cada
  módulo se mira por separado: el informe dice que todo pasó y el archivo no tiene relieve.
- **La degradación es a ``REVIEW``, nunca a ``FAIL``.** Un mapa plano es indistinguible de una
  superficie genuinamente lisa mirando sólo el archivo, así que declarar ``FAIL`` sería
  inventar evidencia. Se enumera sobre todos los códigos de hallazgo para que un código nuevo
  rompa el test hasta que se decida qué significa.
- **``FAIL`` e ``INCONSISTENTE`` no se rescatan**: que el disco se vea bien no arregla un
  informe que se contradice ni el trabajo perdido.
- **Las rutas y los motivos del informe se conservan**, y se suman los del post-check: el
  operador tiene que ver todo junto, no la última capa que habló.

Límite declarado: acá se construyen las dos entradas a mano. Que ``auditar`` produzca esos
hallazgos desde archivos reales lo verifica ``test_parallax_unattended_auditor.py``.
"""

from __future__ import annotations

import pytest

from sky_claw.local.parallax_unattended.auditor import (
    HALLAZGOS_DE_FALLA,
    HALLAZGOS_DE_REVISION,
    CodigoDeHallazgo,
    Hallazgo,
    InformeDeAuditoria,
)
from sky_claw.local.parallax_unattended.composicion import combinar
from sky_claw.local.parallax_unattended.veredicto import (
    DecisionDelLote,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
)

_CONTEOS = {"EN_REVISION": 0, "ESCRITA": 1, "NO_ESCRITA": 0, "NO_PROCESADA": 0}


def _decision(veredicto: VeredictoDelLote, *motivos: MotivoDelLote, rutas: tuple[str, ...] = ()) -> DecisionDelLote:
    return DecisionDelLote(veredicto=veredicto, motivos=motivos, rutas=rutas, conteos=_CONTEOS)


def _auditoria(
    *,
    verificados: tuple[str, ...] = ("textures/a_p.dds",),
    hallazgos: tuple[Hallazgo, ...] = (),
    no_verificados: tuple[str, ...] = (),
) -> InformeDeAuditoria:
    return InformeDeAuditoria(verificados=verificados, hallazgos=hallazgos, no_verificados=no_verificados)


# ---------------------------------------------------------------------------
# El agujero que cierra
# ---------------------------------------------------------------------------


def test_un_pass_del_informe_no_sobrevive_a_un_artefacto_plano() -> None:
    plano = Hallazgo("textures/a_p.dds", CodigoDeHallazgo.ALTURA_PLANA, "sd = 1.10/255")

    final = combinar(_decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(plano,)))

    assert final.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in final.motivos
    assert "textures/a_p.dds" in final.rutas
    assert codigo_de_salida(final) != 0


def test_un_pass_limpio_sigue_siendo_pass() -> None:
    final = combinar(_decision(VeredictoDelLote.PASS), _auditoria())

    assert final.veredicto is VeredictoDelLote.PASS
    assert codigo_de_salida(final) == 0


def test_un_artefacto_no_verificado_tambien_degrada() -> None:
    """No poder medir no es aprobar: el formato desconocido deja el lote en revisión."""

    final = combinar(_decision(VeredictoDelLote.PASS), _auditoria(verificados=(), no_verificados=("textures/a_p.dds",)))

    assert final.veredicto is VeredictoDelLote.REVIEW
    assert final.rutas == ("textures/a_p.dds",)


# ---------------------------------------------------------------------------
# Lo que no se rescata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("veredicto", [VeredictoDelLote.FAIL, VeredictoDelLote.INCONSISTENTE])
def test_lo_que_ya_esta_mal_no_se_rescata_y_suma_los_hallazgos_del_disco(veredicto: VeredictoDelLote) -> None:
    original = _decision(veredicto, MotivoDelLote.ARCHIVO_NO_ESCRITO, rutas=("textures/perdida_p.dds",))

    assert combinar(original, _auditoria()) is original
    con_disco = combinar(
        original, _auditoria(hallazgos=(Hallazgo("textures/plana_p.dds", CodigoDeHallazgo.ALTURA_PLANA, ""),))
    )
    assert con_disco.veredicto is veredicto
    assert MotivoDelLote.ARCHIVO_NO_ESCRITO in con_disco.motivos
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in con_disco.motivos
    assert con_disco.rutas == ("textures/perdida_p.dds", "textures/plana_p.dds")


# ---------------------------------------------------------------------------
# Evidencia dura (FAIL) vs juicio de relieve (REVIEW)
# ---------------------------------------------------------------------------


def test_todo_codigo_de_hallazgo_pertenece_a_exactamente_una_severidad() -> None:
    """Un código de hallazgo nuevo rompe el test hasta que se clasifique en falla o revisión."""

    assert not (HALLAZGOS_DE_FALLA & HALLAZGOS_DE_REVISION)
    assert set(CodigoDeHallazgo) == (HALLAZGOS_DE_FALLA | HALLAZGOS_DE_REVISION)


@pytest.mark.parametrize("codigo", sorted(HALLAZGOS_DE_REVISION))
def test_un_hallazgo_de_relieve_degrada_a_review_y_nunca_a_fail(codigo: CodigoDeHallazgo) -> None:
    """Un mapa plano es indistinguible de una superficie lisa mirando sólo los bytes: pide revisión."""

    final = combinar(
        _decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", codigo, ""),))
    )

    assert final.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in final.motivos


@pytest.mark.parametrize(
    "codigo",
    [
        CodigoDeHallazgo.INFORME_INVALIDO,
        CodigoDeHallazgo.SIN_ARTEFACTOS_AUDITABLES,
        CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,
        CodigoDeHallazgo.NO_EXISTE,
        CodigoDeHallazgo.NO_LEGIBLE,
        CodigoDeHallazgo.TAMANO_EXCESIVO,
        CodigoDeHallazgo.NO_ES_DDS,
        CodigoDeHallazgo.DIMENSIONES_INVALIDAS,
        CodigoDeHallazgo.TAMANO_INCOHERENTE,
    ],
)
def test_un_hallazgo_duro_de_disco_degrada_a_fail(codigo: CodigoDeHallazgo) -> None:
    """Un archivo ausente, truncado o corrupto cuando el informe declaró ``ESCRITA`` es fallo duro, no revisión."""

    final = combinar(
        _decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", codigo, ""),))
    )

    assert final.veredicto is VeredictoDelLote.FAIL
    assert MotivoDelLote.ARTEFACTO_INVALIDO in final.motivos


# ---------------------------------------------------------------------------
# Lo que se conserva
# ---------------------------------------------------------------------------


def test_los_motivos_y_rutas_del_informe_se_conservan() -> None:
    original = _decision(
        VeredictoDelLote.REVIEW,
        MotivoDelLote.ARCHIVO_EN_REVISION,
        rutas=("textures/b_p.dds",),
    )

    final = combinar(original, _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", CodigoDeHallazgo.ALTURA_PLANA, ""),)))

    assert MotivoDelLote.ARCHIVO_EN_REVISION in final.motivos
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in final.motivos
    assert final.rutas == ("textures/b_p.dds", "textures/a_p.dds")
    assert final.conteos == original.conteos


def test_el_motivo_no_se_duplica() -> None:
    ya_marcada = _decision(VeredictoDelLote.REVIEW, MotivoDelLote.ARTEFACTO_NO_CONFIABLE)

    final = combinar(
        ya_marcada, _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", CodigoDeHallazgo.ALTURA_PLANA, ""),))
    )

    assert final.motivos.count(MotivoDelLote.ARTEFACTO_NO_CONFIABLE) == 1


def test_el_detalle_dice_que_se_miro() -> None:
    final = combinar(
        _decision(VeredictoDelLote.PASS),
        _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", CodigoDeHallazgo.ALTURA_PLANA, ""),), no_verificados=("b",)),
    )

    assert "1 artefactos verificados" in final.detalle
    assert "1 con hallazgo" in final.detalle
    assert "1 sin verificar" in final.detalle
