"""Contrato del veredicto del precache de grass (P1) — política pura.

Cubre F-02 (falso éxito), F-12 (diagnóstico) y las decisiones del propietario D1/D2: `success`
(``exito_operacional``) exige finalización operacional legítima **y** trabajo de esta corrida; la
certificación agrega ausencia de fallos conocidos y worldspaces con artefactos; el alcance distingue
trabajo de la corrida / integridad observable / completitud global.
"""

from __future__ import annotations

import pytest

from sky_claw.local.tools.grass_cache_verdict import (
    CODIGOS_DE_RAZON,
    CacheObservado,
    EvidenciaVeredicto,
    evaluar_veredicto,
    invalidar_certificacion,
)


def _cache(**over: object) -> CacheObservado:
    base: dict[str, object] = {
        "cgid_count": 10,
        "cgid_bytes": 1024,
        "dgid_count": 0,
        "fail_count": 0,
        "archivos": 10,
    }
    base.update(over)
    return CacheObservado(**base)  # type: ignore[arg-type]


def _ev(**over: object) -> EvidenciaVeredicto:
    """Evidencia por defecto: NEW impecable, todo lo del caché es de esta corrida."""
    base: dict[str, object] = {
        "outcome": "completed",
        "modo": "new",
        "terminalidad": "terminado",
        "limpieza_limpia": True,
        "inventario_final": _cache(),
        "archivos_heredados": 0,
        "archivos_nuevos": 10,
        "fallos_finales": {},
        "fallos_previos": {},
        "worldspaces_solicitados": ("Tamriel",),
        "worldspaces_con_artefactos": ("Tamriel",),
        "progreso_observado": True,
    }
    base.update(over)
    return EvidenciaVeredicto(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Camino feliz y falso éxito (F-02)
# ---------------------------------------------------------------------------


def test_evidencia_completa_certifica_corrida_completa() -> None:
    v = evaluar_veredicto(_ev())

    assert v.verdict == "completed_verified"
    assert v.certificado is True
    assert v.exito_operacional is True
    assert v.razones == ()
    assert v.alcance_certificado == "corrida_completa"


def test_cache_preexistente_sin_procedencia_no_da_exito_ni_certifica() -> None:
    """El defecto de F-02: un REBUILD que no borró nada no puede declarar éxito."""
    v = evaluar_veredicto(_ev(modo="rebuild", archivos_heredados=10, archivos_nuevos=0))

    assert v.verdict == "completed_unverified"
    assert v.exito_operacional is False
    assert v.certificado is False
    assert "archivos_heredados_sin_procedencia" in v.razones
    assert v.alcance_certificado == "ninguno"


def test_new_con_heredados_es_contradiccion_y_no_certifica() -> None:
    """NEW exige caché vacío (P0): si hay heredados, la premisa no se cumplió."""
    v = evaluar_veredicto(_ev(archivos_heredados=3, archivos_nuevos=7))

    assert v.exito_operacional is False
    assert v.verdict == "completed_unverified"


def test_rebuild_que_si_regenero_certifica() -> None:
    v = evaluar_veredicto(_ev(modo="rebuild", archivos_heredados=0, archivos_nuevos=40))

    assert v.verdict == "completed_verified"
    assert v.alcance_certificado == "corrida_completa"


def test_cache_vacio_es_fallo_aunque_el_flag_haya_desaparecido() -> None:
    v = evaluar_veredicto(_ev(inventario_final=_cache(cgid_bytes=0)))

    assert v.verdict == "failed"
    assert v.exito_operacional is False
    assert "cache_vacio" in v.razones


def test_sin_cgid_es_fallo() -> None:
    v = evaluar_veredicto(_ev(inventario_final=_cache(cgid_count=0, archivos=0, cgid_bytes=0)))

    assert v.verdict == "failed"


# ---------------------------------------------------------------------------
# `.fail`: contador de intentos por celda (C4)
# ---------------------------------------------------------------------------


def test_fail_atribuible_bloquea_certificacion_pero_no_el_exito_operacional() -> None:
    v = evaluar_veredicto(_ev(fallos_finales={"Tamrielx0001y0000.fail": b"1\n"}))

    assert v.verdict == "completed_unverified"
    assert v.exito_operacional is True  # la corrida trabajó; el caché tiene una celda sin completar
    assert v.certificado is False
    assert v.integridad_observable == "con_fallos_conocidos"
    assert "fallos_de_celda_atribuibles" in v.razones


def test_fail_historico_no_se_atribuye_y_tampoco_certifica() -> None:
    previo = {"Tamrielx0001y0000.fail": b"2\n"}
    v = evaluar_veredicto(_ev(fallos_previos=previo, fallos_finales=dict(previo)))

    assert "fallos_de_celda_historicos" in v.razones
    assert "fallos_de_celda_atribuibles" not in v.razones
    assert v.certificado is False


def test_fail_con_contador_incrementado_es_de_esta_corrida() -> None:
    v = evaluar_veredicto(
        _ev(
            fallos_previos={"Tamrielx0001y0000.fail": b"1\n"},
            fallos_finales={"Tamrielx0001y0000.fail": b"2\n"},
        )
    )

    assert "fallos_de_celda_atribuibles" in v.razones
    assert "fallos_de_celda_historicos" not in v.razones


def test_fail_borrado_cuenta_como_avance_en_resume() -> None:
    """NGIO borra el `.fail` cuando la celda completa: es evidencia POSITIVA de avance."""
    v = evaluar_veredicto(
        _ev(
            modo="resume",
            archivos_heredados=10,
            archivos_nuevos=0,
            progreso_observado=False,
            fallos_previos={"Tamrielx0001y0000.fail": b"1\n"},
            fallos_finales={},
        )
    )

    assert v.verdict == "completed_verified"
    assert v.alcance_certificado == "trabajo_de_la_corrida"


# ---------------------------------------------------------------------------
# RESUME y alcance certificado (D2 / RD-12)
# ---------------------------------------------------------------------------


def test_resume_con_progreso_certifica_con_alcance_limitado() -> None:
    v = evaluar_veredicto(_ev(modo="resume", archivos_heredados=30_000, archivos_nuevos=0, progreso_observado=True))

    assert v.verdict == "completed_verified"
    assert v.certificado is True
    assert v.alcance_certificado == "trabajo_de_la_corrida"
    assert v.completitud_global == "no_verificada"


def test_resume_sin_evidencia_de_avance_no_certifica_ni_da_exito() -> None:
    v = evaluar_veredicto(_ev(modo="resume", archivos_heredados=30_000, archivos_nuevos=0, progreso_observado=False))

    assert v.verdict == "completed_unverified"
    assert v.exito_operacional is False
    assert v.alcance_certificado == "ninguno"
    assert "sin_evidencia_de_esta_corrida" in v.razones


# ---------------------------------------------------------------------------
# Worldspaces
# ---------------------------------------------------------------------------


def test_worldspace_solicitado_sin_artefactos_impide_certificar() -> None:
    v = evaluar_veredicto(
        _ev(
            worldspaces_solicitados=("Tamriel", "DLC2SolstheimWorld"),
            worldspaces_con_artefactos=("Tamriel",),
        )
    )

    assert v.verdict == "completed_unverified"
    assert "worldspaces_sin_artefactos" in v.razones


def test_el_prefijo_del_worldspace_se_compara_sin_mayusculas() -> None:
    v = evaluar_veredicto(
        _ev(
            worldspaces_solicitados=("Tamriel",),
            worldspaces_con_artefactos=("tamriel",),
        )
    )

    assert v.verdict == "completed_verified"


# ---------------------------------------------------------------------------
# Terminalidad y evidencia ilegible (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminalidad", ["vivo", "indeterminado"])
def test_terminalidad_no_verificada_es_unknown(terminalidad: str) -> None:
    v = evaluar_veredicto(_ev(terminalidad=terminalidad))

    assert v.verdict == "unknown"
    assert v.certificado is False
    assert v.exito_operacional is False
    assert "terminalidad_no_verificada" in v.razones


def test_evidencia_ilegible_es_unknown() -> None:
    v = evaluar_veredicto(_ev(evidencia_ilegible=True, inventario_final=None))

    assert v.verdict == "unknown"
    assert "cache_ilegible" in v.razones


# ---------------------------------------------------------------------------
# Limpieza del flag: dos fases (RD-11)
# ---------------------------------------------------------------------------


def test_limpieza_incompleta_no_certifica_ni_da_exito() -> None:
    v = evaluar_veredicto(_ev(limpieza_limpia=False))

    assert v.verdict == "completed_unverified"
    assert v.exito_operacional is False
    assert "limpieza_incompleta" in v.razones


def test_fase_uno_sin_limpieza_conocida_puede_certificar_provisionalmente() -> None:
    """El provisional nunca se publica: `run()` lo recalcula (RD-11)."""
    v = evaluar_veredicto(_ev(limpieza_limpia=None))

    assert v.verdict == "completed_verified"
    assert "limpieza_incompleta" not in v.razones


def test_la_re_evaluacion_con_limpieza_fallida_degrada_el_provisional() -> None:
    """RD-11: una sola política. `run()` reevalúa la MISMA evidencia con la limpieza real."""
    provisional = evaluar_veredicto(_ev(limpieza_limpia=None))
    final = evaluar_veredicto(_ev(limpieza_limpia=False))

    assert provisional.certificado is True
    assert final.verdict == "completed_unverified"
    assert final.certificado is False
    assert final.exito_operacional is False
    assert "limpieza_incompleta" in final.razones


def test_la_limpieza_completa_no_altera_un_veredicto_ya_certificado() -> None:
    assert evaluar_veredicto(_ev(limpieza_limpia=True)) == evaluar_veredicto(_ev())


# ---------------------------------------------------------------------------
# Matriz de terminación
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "verdict"),
    [
        ("stalled", "partial"),
        ("timeout", "partial"),
        ("max_restarts", "partial"),
        ("disk_full", "partial"),
        ("spawn_failed", "failed"),
        ("flag_sin_progreso", "failed"),
        ("flag_alternativo_presente", "failed"),
        ("cache_cambio_despues_del_plan", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_matriz_de_terminacion(outcome: str, verdict: str) -> None:
    v = evaluar_veredicto(_ev(outcome=outcome, progreso_observado=False))

    assert v.verdict == verdict
    assert v.certificado is False
    assert v.exito_operacional is False


def test_parcial_sin_cache_es_fallo() -> None:
    v = evaluar_veredicto(_ev(outcome="timeout", inventario_final=_cache(cgid_count=0, cgid_bytes=0, archivos=0)))

    assert v.verdict == "failed"


# ---------------------------------------------------------------------------
# Anclas de contrato
# ---------------------------------------------------------------------------


def test_vocabulario_de_razones_congelado() -> None:
    assert set(CODIGOS_DE_RAZON) == {
        "cache_ilegible",
        "terminalidad_no_verificada",
        "cache_vacio",
        "limpieza_incompleta",
        "archivos_heredados_sin_procedencia",
        "sin_evidencia_de_esta_corrida",
        "fallos_de_celda_atribuibles",
        "fallos_de_celda_historicos",
        "worldspaces_sin_artefactos",
        "fallos_de_celda_no_evaluables",
        "baseline_ilegible",
        "operacion_invalidada",
        "outcome_no_completado",
        "sin_certificacion",
        "cancelado",
    }


def test_toda_razon_emitida_pertenece_al_vocabulario() -> None:
    casos = [
        _ev(),
        _ev(modo="rebuild", archivos_heredados=10),
        _ev(fallos_finales={"x.fail": b"1\n"}),
        _ev(inventario_final=_cache(cgid_bytes=0)),
        _ev(limpieza_limpia=False),
        _ev(terminalidad="vivo"),
        _ev(evidencia_ilegible=True, inventario_final=None),
        _ev(worldspaces_solicitados=("A", "B"), worldspaces_con_artefactos=("A",)),
        _ev(outcome="stalled"),
        _ev(outcome="cancelled"),
        _ev(outcome="spawn_failed"),
    ]
    for ev in casos:
        for razon in evaluar_veredicto(ev).razones:
            assert razon in CODIGOS_DE_RAZON, razon


def test_el_veredicto_es_determinista() -> None:
    ev = _ev(modo="resume", progreso_observado=True)

    assert evaluar_veredicto(ev) == evaluar_veredicto(ev)


def test_no_se_publica_certificacion_sin_alcance() -> None:
    """Invariante: certificado => alcance != ninguno y trabajo demostrado."""
    for ev in (
        _ev(),
        _ev(modo="resume", progreso_observado=True),
        _ev(modo="rebuild", archivos_heredados=0, archivos_nuevos=5),
    ):
        v = evaluar_veredicto(ev)
        assert v.certificado is True
        assert v.alcance_certificado != "ninguno"
        assert v.trabajo_de_la_corrida == "demostrado"


def test_completitud_global_nunca_se_certifica_en_p1() -> None:
    assert evaluar_veredicto(_ev()).completitud_global == "no_verificada"
    assert evaluar_veredicto(_ev(modo="resume", progreso_observado=True)).completitud_global == "no_verificada"


# ---------------------------------------------------------------------------
# Revisión independiente: H1 (defensa en la política), H4 y H6
# ---------------------------------------------------------------------------


def test_h1_fail_contado_pero_no_inventariado_no_certifica() -> None:
    """H1: si el inventario cuenta `.fail` que no se pudieron evaluar, no hay certificación.

    Es la defensa en profundidad de la política: aunque el runner pierda el detalle, el conteo del
    inventario delata que hay un `.fail` sobreviviente no evaluable.
    """
    v = evaluar_veredicto(_ev(inventario_final=_cache(fail_count=1), fallos_finales={}))

    assert v.verdict == "completed_unverified"
    assert v.certificado is False
    assert "fallos_de_celda_no_evaluables" in v.razones
    assert v.integridad_observable == "con_fallos_conocidos"


def test_h1_fail_no_evaluable_no_atribuye_de_mas() -> None:
    """El `.fail` ilegible no se cuenta como histórico ni como atribuible: es no evaluable."""
    v = evaluar_veredicto(_ev(inventario_final=_cache(fail_count=2), fallos_finales={"a.fail": b"1\n"}))

    assert "fallos_de_celda_no_evaluables" in v.razones
    assert "fallos_de_celda_atribuibles" in v.razones  # el que sí se pudo leer es nuevo


def test_h4_baseline_ilegible_no_certifica() -> None:
    """H4: sin baseline verificable no se puede afirmar procedencia."""
    v = evaluar_veredicto(_ev(baseline_ilegible=True))

    assert v.verdict == "unknown"
    assert v.certificado is False
    assert v.exito_operacional is False
    assert "baseline_ilegible" in v.razones


def test_h6_invalidar_certificacion_degrada_y_agrega_razon() -> None:
    """H6: la validez de la OPERACIÓN puede invalidar una certificación del runner."""
    veredicto = evaluar_veredicto(_ev())
    assert veredicto.certificado is True

    final = invalidar_certificacion(veredicto, razon="operacion_invalidada")

    assert final.verdict == "completed_unverified"
    assert final.certificado is False
    assert final.alcance_certificado == "ninguno"
    assert final.razones == ("operacion_invalidada",)
    # La evidencia observada NO se falsea: el trabajo de la corrida sigue demostrado.
    assert final.trabajo_de_la_corrida == "demostrado"


def test_h6_invalidar_no_mejora_un_veredicto_ya_no_certificado() -> None:
    base = evaluar_veredicto(_ev(limpieza_limpia=False))

    assert invalidar_certificacion(base, razon="operacion_invalidada") == base
