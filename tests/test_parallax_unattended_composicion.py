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

import pathlib

import pytest

from sky_claw.local.parallax_unattended.auditor import (
    HALLAZGOS_DE_FALLA,
    HALLAZGOS_DE_INCONSISTENCIA,
    HALLAZGOS_DE_REVISION,
    CodigoDeHallazgo,
    Hallazgo,
    InformeDeAuditoria,
    auditar,
)
from sky_claw.local.parallax_unattended.composicion import combinar
from sky_claw.local.parallax_unattended.veredicto import (
    EFECTOS_CONOCIDOS,
    ESTADOS_CONOCIDOS,
    DecisionDelLote,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
    dictar,
)

_CONTEOS = {"EN_REVISION": 0, "ESCRITA": 1, "NO_ESCRITA": 0, "NO_PROCESADA": 0, "SIN_CONFIRMAR": 0}


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
# Evidencia dura (FAIL) vs inconsistencia (INCONSISTENTE) vs juicio de relieve (REVIEW)
# ---------------------------------------------------------------------------


def test_todo_codigo_de_hallazgo_pertenece_a_exactamente_una_severidad() -> None:
    """Un código de hallazgo nuevo rompe el test hasta que se clasifique en inconsistencia, falla o revisión."""

    assert not (HALLAZGOS_DE_FALLA & HALLAZGOS_DE_REVISION)
    assert not (HALLAZGOS_DE_FALLA & HALLAZGOS_DE_INCONSISTENCIA)
    assert not (HALLAZGOS_DE_REVISION & HALLAZGOS_DE_INCONSISTENCIA)
    assert set(CodigoDeHallazgo) == (HALLAZGOS_DE_FALLA | HALLAZGOS_DE_REVISION | HALLAZGOS_DE_INCONSISTENCIA)


@pytest.mark.parametrize("codigo", sorted(HALLAZGOS_DE_REVISION))
def test_un_hallazgo_de_relieve_degrada_a_review_y_nunca_a_fail(codigo: CodigoDeHallazgo) -> None:
    """Un mapa plano es indistinguible de una superficie lisa mirando sólo los bytes: pide revisión."""

    final = combinar(
        _decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", codigo, ""),))
    )

    assert final.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in final.motivos


@pytest.mark.parametrize("codigo", sorted(HALLAZGOS_DE_FALLA))
def test_un_hallazgo_duro_de_disco_degrada_a_fail(codigo: CodigoDeHallazgo) -> None:
    """Un archivo ausente, truncado o corrupto cuando el informe declaró ``ESCRITA`` es fallo duro, no revisión."""

    final = combinar(
        _decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(Hallazgo("textures/a_p.dds", codigo, ""),))
    )

    assert final.veredicto is VeredictoDelLote.FAIL
    assert MotivoDelLote.ARTEFACTO_INVALIDO in final.motivos


@pytest.mark.parametrize("codigo", sorted(HALLAZGOS_DE_INCONSISTENCIA))
def test_un_hallazgo_de_inconsistencia_degrada_a_inconsistente(codigo: CodigoDeHallazgo) -> None:
    final = combinar(_decision(VeredictoDelLote.PASS), _auditoria(hallazgos=(Hallazgo("", codigo, ""),)))

    assert final.veredicto is VeredictoDelLote.INCONSISTENTE
    assert MotivoDelLote.FORMA_INVALIDA in final.motivos


def test_lote_en_revision_sin_escritas_no_se_degrada_a_fail_por_cero_artefactos() -> None:
    """C-2: el default ``escribir_revision=False`` deja ``ESCRITA=0`` y ``auditar`` emite ``SIN_ARTEFACTOS_AUDITABLES``.

    El veredicto final DEBE seguir siendo ``REVIEW`` (código 3), no degradar a ``FAIL`` (código 1).
    """
    decision_en_revision = DecisionDelLote(
        veredicto=VeredictoDelLote.REVIEW,
        motivos=(MotivoDelLote.ARCHIVO_EN_REVISION,),
        rutas=("textures/t0_p.dds",),
        conteos={"EN_REVISION": 8, "ESCRITA": 0, "NO_ESCRITA": 0, "NO_PROCESADA": 0, "SIN_CONFIRMAR": 0},
    )
    auditoria_sin_artefactos = InformeDeAuditoria(
        verificados=(),
        hallazgos=(Hallazgo("", CodigoDeHallazgo.SIN_ARTEFACTOS_AUDITABLES, "sin artefactos"),),
        no_verificados=(),
    )

    final = combinar(decision_en_revision, auditoria_sin_artefactos)

    assert final.veredicto is VeredictoDelLote.REVIEW
    assert final.motivos == (MotivoDelLote.ARCHIVO_EN_REVISION,)
    assert final.rutas == ("textures/t0_p.dds",)


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


# ---------------------------------------------------------------------------
# Ancla: ``dictar`` y ``auditar`` tienen que coincidir en la matriz (estado, efecto)
# ---------------------------------------------------------------------------

#: La matriz legal, congelada por igualdad literal. Cada módulo la valida por su cuenta (el informe puede llegar a
#: cualquiera de los dos primero): si una mitad cambia sola, el desacuerdo hace pasar un informe que la otra rechazaría.
_COMBINACIONES_LEGALES = frozenset(
    {
        ("EN_REVISION", None),
        ("ESCRITA", "OMITIDA"),
        ("ESCRITA", "PUBLICADA"),
        ("ESCRITA", "REEMPLAZADA"),
        ("NO_ESCRITA", None),
        ("NO_ESCRITA", "CONFLICTO"),
        ("NO_ESCRITA", "FALLIDA"),
        ("NO_ESCRITA", "PRESERVADA"),
        ("NO_ESCRITA", "RETIRADA"),
        ("NO_ESCRITA", "SOLTADA"),
        ("NO_PROCESADA", None),
        ("SIN_CONFIRMAR", None),
    }
)


def _informe_de_una_combinacion(estado: str, efecto: str | None) -> dict[str, object]:
    veredicto = {"ESCRITA": "AUTO_ACCEPT", "EN_REVISION": "REVIEW"}.get(estado)
    return {
        "version_del_lote": "np-a0a.v1",
        "parametros": {},
        "encoder": {"texconv_version": "v", "texconv_sha256": "a" * 64},
        "conteos": {e: int(e == estado) for e in sorted(ESTADOS_CONOCIDOS)},
        "archivos": [
            {"ruta_de_salida": "textures/a_p.dds", "estado": estado, "efecto": efecto, "veredicto": veredicto}
        ],
        "otras_rutas": [],
        "omitidas_al_enumerar": [],
        "interrupcion": "LockLeaseLostError: lease perdida" if estado == "SIN_CONFIRMAR" else None,
    }


@pytest.mark.parametrize("efecto", [None, *sorted(EFECTOS_CONOCIDOS)])
@pytest.mark.parametrize("estado", sorted(ESTADOS_CONOCIDOS))
def test_dictar_y_auditar_coinciden_en_la_matriz_estado_efecto(
    estado: str, efecto: str | None, tmp_path: pathlib.Path
) -> None:
    informe = _informe_de_una_combinacion(estado, efecto)
    legal = (estado, efecto) in _COMBINACIONES_LEGALES

    forma_invalida_en_dictar = MotivoDelLote.FORMA_INVALIDA in dictar(informe).motivos
    forma_invalida_en_auditar = any(
        h.codigo is CodigoDeHallazgo.INFORME_INVALIDO for h in auditar(informe, tmp_path).hallazgos
    )

    assert forma_invalida_en_dictar is not legal
    assert forma_invalida_en_auditar is not legal
