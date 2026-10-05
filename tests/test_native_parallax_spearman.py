"""PR-MATH-B — Spearman con empates: contrato de la primitiva canónica.

Contexto (finding confirmado): ``metrics.spearman`` y ``run_exp_m2.spearman``
rankeaban con un doble ``argsort``, que asigna rangos ORDINALES distintos a
valores iguales. Spearman estándar asigna el rango MEDIO del grupo empatado.

Este archivo congela:

- la primitiva de rangos (``metrics.average_ranks``), con los casos de §6/§19;
- el caso de valor conocido verificable a mano (rho = 5/6);
- que el comportamiento SIN empates no cambia;
- el contrato de entrada degenerada (constante ⇒ no evaluable; NaN ⇒ error);
- que M2 delega en la canónica en vez de reimplementarla;
- la semántica NaN de los consumidores de M3 (bootstrap, orientación, selección);
- el diagnóstico de coherencia de M4 (serializado con ``allow_nan=False``).

Los valores esperados se verificaron de forma independiente (fórmula
``(count(<x) + count(<=x) + 1)/2`` y Pearson escrito a mano), sin usar la
implementación bajo test.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from sky_claw.local.native_parallax.research import metrics
from sky_claw.local.native_parallax.research import run_exp_m2 as m2
from sky_claw.local.native_parallax.research import run_exp_m3 as m3
from sky_claw.local.native_parallax.research.run_exp_m2 import spearman as m2_spearman
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    TRUST_PROXY_NAMES,
    build_cohort_a_summary,
    select_proxy_on_calibration,
    spearman_ci,
    trust_gate_passes,
)

# ---------------------------------------------------------------- A. rangos medios


def test_average_ranks_sin_empates() -> None:
    np.testing.assert_allclose(metrics.average_ranks([10.0, 20.0, 30.0]), [1.0, 2.0, 3.0])


def test_average_ranks_un_par_empatado_comparte_rango_medio() -> None:
    np.testing.assert_allclose(metrics.average_ranks([10.0, 10.0, 20.0]), [1.5, 1.5, 3.0])


def test_average_ranks_dos_grupos_empatados() -> None:
    np.testing.assert_allclose(metrics.average_ranks([1.0, 1.0, 2.0, 2.0, 3.0]), [1.5, 1.5, 3.5, 3.5, 5.0])


def test_average_ranks_todos_empatados() -> None:
    np.testing.assert_allclose(metrics.average_ranks([5.0, 5.0, 5.0]), [2.0, 2.0, 2.0])


def test_average_ranks_negativos_y_desordenados() -> None:
    np.testing.assert_allclose(metrics.average_ranks([3.0, -1.0, 3.0, -5.0]), [3.5, 2.0, 3.5, 1.0])


def test_average_ranks_2d_preserva_forma_y_aplana_consistente() -> None:
    a = np.array([[3.0, 1.0], [1.0, 2.0]])
    r = metrics.average_ranks(a)
    assert r.shape == a.shape
    np.testing.assert_allclose(r, [[4.0, 1.5], [1.5, 3.0]])
    # el aplanado tiene que ser el mismo que el de la entrada aplanada (consistencia 1D/2D)
    np.testing.assert_allclose(r.ravel(), metrics.average_ranks(a.ravel()))


def test_average_ranks_contra_referencia_ingenua() -> None:
    """Referencia O(n²) independiente: rango = (count(<x) + count(<=x) + 1) / 2.

    No depende del orden incidental de ``argsort``: barre todas las entradas para
    cada valor, así que cualquier orden inestable que rompa empates se detecta.
    """
    rng = np.random.default_rng(20261005)
    for _ in range(50):
        n = int(rng.integers(1, 40))
        vals = rng.integers(0, 6, size=n).astype(np.float64)  # empates garantizados
        esperado = np.array([(sum(1 for v in vals if v < x) + sum(1 for v in vals if v <= x) + 1) / 2.0 for x in vals])
        np.testing.assert_allclose(metrics.average_ranks(vals), esperado)


def test_average_ranks_rechaza_no_finitos() -> None:
    """§21: sin orden arbitrario de NaN — la entrada no finita es un error del caller."""
    with pytest.raises(ValueError, match="no finit"):
        metrics.average_ranks([1.0, float("nan"), 2.0])
    with pytest.raises(ValueError, match="no finit"):
        metrics.average_ranks([1.0, float("inf"), 2.0])


# ---------------------------------------------------------------- B. valor conocido


def test_rho_conocido_con_empates() -> None:
    """Caso verificable a mano: x=[1,1,2,3], y=[1,2,2,3] ⇒ rho = 5/6.

    rx = [1.5, 1.5, 3, 4]; ry = [1, 2.5, 2.5, 4]; cov = 3.75; sd = 4.5 ⇒ 3.75/4.5.
    El baseline ordinal devolvía ≈ 1.0 sobre este mismo input (doble argsort).
    """
    x = [1.0, 1.0, 2.0, 3.0]
    y = [1.0, 2.0, 2.0, 3.0]
    np.testing.assert_allclose(metrics.average_ranks(x), [1.5, 1.5, 3.0, 4.0])
    np.testing.assert_allclose(metrics.average_ranks(y), [1.0, 2.5, 2.5, 4.0])
    assert metrics.spearman(x, y) == pytest.approx(5.0 / 6.0, abs=1e-12)
    assert metrics.spearman(x, y) != pytest.approx(1.0, abs=1e-3)


def test_empates_no_inflan_la_correlacion() -> None:
    """Un bloque plano no puede fabricar monotonía: si x es constante, rho no existe."""
    assert math.isnan(metrics.spearman([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]))


# ---------------------------------------------------------------- C. sin empates no cambia


def test_sin_empates_monotono_estricto() -> None:
    assert metrics.spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert metrics.spearman([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_sin_empates_identico_al_ordinal_historico() -> None:
    """Regresión: con valores continuos (sin empates) el resultado es el de siempre."""
    rng = np.random.default_rng(11)
    for _ in range(20):
        n = int(rng.integers(4, 30))
        x = rng.normal(size=n)
        y = rng.normal(size=n)
        rx = np.argsort(np.argsort(x))
        ry = np.argsort(np.argsort(y))
        esperado = float(np.corrcoef(rx, ry)[0, 1])
        assert metrics.spearman(x, y) == pytest.approx(esperado, abs=1e-12)


# ---------------------------------------------------------------- D. entrada degenerada


def test_constante_no_hereda_la_convencion_plana_de_pearson() -> None:
    """§10: ``pearson`` define campo plano ≡ 1.0/0.0; Spearman NO hereda esa convención.

    Sin orden no hay correlación de rangos que reportar: fabricar un 1.0
    informativo donde la varianza de rangos es cero es un falso positivo.
    """
    assert metrics.pearson(np.ones(4), np.ones(4)) == 1.0  # convención de pearson, intacta
    assert math.isnan(metrics.spearman(np.ones(4), np.ones(4)))
    assert math.isnan(metrics.spearman(np.ones(4), np.arange(4.0)))


def test_n_menor_a_dos_es_no_evaluable() -> None:
    assert math.isnan(metrics.spearman([1.0], [2.0]))


def test_n_cero_es_no_evaluable() -> None:
    """Frontera n=0 (Regression Oracle PR #685): sin pares no hay correlación."""
    assert math.isnan(metrics.spearman([], []))


def test_n_dos_con_empates_es_no_evaluable() -> None:
    """Frontera n=2 (Regression Oracle PR #685): pasa la guarda de tamaño, pero un
    empate deja la varianza de rangos en cero — y eso es NOT_EVALUABLE, no 0.0/1.0."""
    assert math.isnan(metrics.spearman([1.0, 1.0], [2.0, 3.0]))
    assert math.isnan(metrics.spearman([1.0, 2.0], [3.0, 3.0]))


def test_tamanos_desalineados_es_error_del_caller() -> None:
    with pytest.raises(ValueError, match="tama"):
        metrics.spearman([1.0, 2.0], [1.0, 2.0, 3.0])


# ---------------------------------------------------------------- E. una sola implementación


def test_ranking_vive_solo_en_average_ranks() -> None:
    """Ancla §5: el doble argsort (ranking ordinal) no puede reaparecer en ningún módulo.

    Enumera el paquete entero en vez de muestrear: un archivo nuevo con la
    implementación vieja rompe el ancla, aunque nadie lo recuerde.
    """
    patron = re.compile(r"np\.argsort\(\s*np\.argsort")
    # Revisión adversarial PR #685: ``glob("*.py")`` enumeraba UN directorio mientras
    # el docstring prometía "el paquete entero". ``rglob`` desde la raíz del paquete
    # cubre subpaquetes futuros y el propio ``__init__.py``; sin esto, un
    # ``research/submod/x.py`` reintroducía el doble argsort sin romper el ancla.
    raiz = Path(metrics.__file__).resolve().parent.parent
    ofensores = sorted(
        str(p.relative_to(raiz)) for p in raiz.rglob("*.py") if patron.search(p.read_text(encoding="utf-8"))
    )
    assert ofensores == []


def test_m2_spearman_delega_en_la_canonica() -> None:
    """M2 conserva su API pero no reimplementa el algoritmo (wrapper delgado)."""
    x = [1.0, 1.0, 2.0, 3.0]
    y = [1.0, 2.0, 2.0, 3.0]
    assert m2_spearman(x, y) == pytest.approx(metrics.spearman(x, y))
    assert m2_spearman(x, y) == pytest.approx(5.0 / 6.0, abs=1e-12)
    assert m2_spearman(x, y) != pytest.approx(1.0, abs=1e-3)


def test_m2_spearman_conserva_sus_guardas_historicas() -> None:
    assert math.isnan(m2_spearman([1.0, 2.0], [1.0, 2.0]))  # n < 3 histórico
    assert m2_spearman([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


def test_m2_spearman_no_es_una_reimplementacion() -> None:
    """Ancla estructural: el CUERPO de M2 delega en la canónica (el docstring no cuenta).

    Se descarta el docstring a propósito: un comentario que dice "misma definición
    que metrics.py" no es una delegación.
    """
    fn = ast.parse(inspect.getsource(m2.spearman)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    cuerpo = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) else fn.body
    texto = "\n".join(ast.unparse(nodo) for nodo in cuerpo)
    assert "metrics.spearman" in texto or "np_m0_metrics.spearman" in texto, (
        "run_exp_m2.spearman debe delegar en la primitiva canónica, no reimplementarla"
    )


# ---------------------------------------------------------------- F. M3 bootstrap §11


def test_bootstrap_cuenta_replicas_degeneradas() -> None:
    """Réplicas constantes son posibles aunque el dataset no lo sea (§11).

    x casi constante ⇒ ~10% de réplicas bootstrap son planas. Deben omitirse
    del percentil y quedar CONTADAS, no desaparecer en silencio.
    """
    x = [1.0] * 8 + [2.0, 3.0]
    y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    ci = spearman_ci(x, y)
    assert ci["n_boot_evaluable"] + ci["n_boot_degenerate"] == 1000.0
    assert ci["n_boot_degenerate"] > 0
    assert ci["n_boot_evaluable"] > 0
    assert ci["ci_status"] == "PARTIAL"
    assert np.isfinite(ci["ci_low"]) and np.isfinite(ci["ci_high"])
    assert ci["ci_low"] <= ci["spearman"] <= ci["ci_high"]


def test_bootstrap_sin_degeneradas_es_evaluable() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    y = [2.0, 4.0, 6.1, 7.9, 10.2, 11.8]
    ci = spearman_ci(x, y)
    assert ci["n_boot_degenerate"] == 0.0
    assert ci["ci_status"] == "EVALUABLE"


def test_bootstrap_totalmente_degenerado_no_produce_ci() -> None:
    """Nunca un percentil calculado sobre cero réplicas válidas."""
    x = [1.0] * 10
    y = [float(i) for i in range(10)]
    ci = spearman_ci(x, y)
    assert ci["ci_status"] == "NOT_EVALUABLE"
    assert ci["n_boot_evaluable"] == 0.0
    assert math.isnan(ci["ci_low"]) and math.isnan(ci["ci_high"])


def test_bootstrap_determinista_con_las_claves_nuevas() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    y = [2.0, 4.0, 6.1, 7.9, 10.2, 11.8]
    assert spearman_ci(x, y) == spearman_ci(x, y)  # seed fija ⇒ determinista


# ---------------------------------------------------------------- G. M3 orientación §13


def _summary_rows(*, cal_rmse: list[float], held_rmse: list[float]) -> tuple[dict[str, Any], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    feats: dict[str, dict[str, Any]] = {}
    for i, rmse in enumerate(cal_rmse):
        asset = f"cal{i}"
        rows.append(
            {
                "asset": asset,
                "family": "f_cal",
                "split": "CALIBRATION",
                "aligned_rmse": rmse,
                "correlation": 0.9,
                "variance_ratio": 0.9,
                "catastrophic": False,
            }
        )
        feats[asset] = {"nz_p01": float(i) + 1.0}
    for i, rmse in enumerate(held_rmse):
        asset = f"held{i}"
        rows.append(
            {
                "asset": asset,
                "family": "f_held",
                "split": "HELD_OUT",
                "aligned_rmse": rmse,
                "correlation": 0.9,
                "variance_ratio": 0.9,
                "catastrophic": False,
            }
        )
        feats[asset] = {"nz_p01": float(i) + 1.0}
    return {"rows": rows, "features": feats, "dataset_invalid": []}, feats


def test_orientacion_no_evaluable_no_cae_silenciosamente_en_menos_uno(monkeypatch: pytest.MonkeyPatch) -> None:
    """§13: ``nan > 0`` es falso; sin guarda, rho_cal=NaN ⇒ orientation=-1 fabricada.

    La guarda debe fallar CERRADO: sin orientación no hay trust held-out, y el
    gate §31 no puede pasar.
    """
    monkeypatch.setattr(m3, "select_proxy_on_calibration", lambda rows, feats: "nz_p01")
    ev, _ = _summary_rows(cal_rmse=[0.5] * 6, held_rmse=[0.1 * i for i in range(6)])  # rmse cal constante
    summary = build_cohort_a_summary(ev, sufficient=True)

    assert summary["calibration"]["status"] == "CALIBRATION_SPEARMAN_NOT_EVALUABLE"
    assert not np.isfinite(summary["calibration"]["spearman"])
    assert summary["heldout_trust"] is None
    assert trust_gate_passes(summary) is False


def test_orientacion_evaluable_sigue_orientando(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: con rho_cal negativo la orientación sigue siendo -1 (sin cambios)."""
    monkeypatch.setattr(m3, "select_proxy_on_calibration", lambda rows, feats: "nz_p01")
    ev, _ = _summary_rows(cal_rmse=[0.1 * i for i in range(6)], held_rmse=[0.1 * i for i in range(6)])
    summary = build_cohort_a_summary(ev, sufficient=True)
    assert summary["calibration"]["spearman"] == pytest.approx(1.0)
    assert summary["calibration"]["orientation"] == 1.0
    assert summary["heldout_trust"] is not None


# ---------------------------------------------------------------- H. selección de proxy §14


def test_proxy_seleccion_no_elige_un_proxy_no_evaluable() -> None:
    """§14: rmse constante en calibración ⇒ todos los Spearman NaN ⇒ sin selección."""
    rows = [
        {"asset": f"a{i}", "family": "f", "split": "CALIBRATION", "aligned_rmse": 0.5, "catastrophic": False}
        for i in range(4)
    ]
    feats = {r["asset"]: {name: float(i) for name in TRUST_PROXY_NAMES} for i, r in enumerate(rows)}
    assert select_proxy_on_calibration(rows, feats) is None


def test_sigma_eff_ignora_candidatos_no_evaluables(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2-D: el orden de ``sorted`` no puede elegir un candidato con rho no evaluable.

    ``cand_const`` devuelve un σ constante ⇒ varianza de rangos cero ⇒ rho NaN. Con
    el ranking ordinal viejo ese candidato obtenía rho ≈ +1 (los tres valores iguales
    recibían rangos 0,1,2) y podía ganar por orden de tabla. Ahora queda fuera.
    """
    monkeypatch.setattr(
        m2,
        "SIGMA_EFF_CANDIDATES",
        {"cand_const": lambda _n, _f: 1.0, "cand_var": lambda _n, f: f["sig"]},
    )
    monkeypatch.setattr(m2, "split_of", lambda _family: "CALIBRATION")
    characs = {
        f"A{i}": {
            "_normal": None,
            "features": {"sig": 1.0 / (i + 1)},
            "raw_eval": {"aligned_rmse": 0.1 * (i + 1), "gradient_rmse": 0.2},
        }
        for i in range(3)
    }
    assets = {a: SimpleNamespace(family="f") for a in characs}
    sel = m2.select_sigma_eff(characs, assets)

    por_candidato = {t["candidate"]: t["spearman"] for t in sel["table"] if t["target"] == "aligned_rmse"}
    assert not np.isfinite(por_candidato["cand_const"])
    assert np.isfinite(por_candidato["cand_var"])
    assert sel["winner"] == "cand_var"
    assert sel["status"] == "EVALUABLE"


def test_sigma_eff_sin_candidatos_evaluables_es_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """MATH-B.1: sin NINGÚN candidato evaluable no hay ganador — no hay fallback.

    El fallback histórico a ``nz_p01`` no era fail-closed: elegía un σ_eff sin
    respaldo estadístico y las policies M2-E/F corrían igual, con lo que un corpus
    donde la selección no es medible producía resultados presentables como si lo
    fuera. El estado correcto es explícito y sin ganador.
    """
    monkeypatch.setattr(m2, "SIGMA_EFF_CANDIDATES", {"cand_a": lambda _n, _f: 1.0, "cand_b": lambda _n, _f: 2.0})
    monkeypatch.setattr(m2, "split_of", lambda _family: "CALIBRATION")
    characs = {
        "A1": {"_normal": None, "features": {}, "raw_eval": {"aligned_rmse": 0.1, "gradient_rmse": 0.2}},
        "A2": {"_normal": None, "features": {}, "raw_eval": {"aligned_rmse": 0.3, "gradient_rmse": 0.4}},
    }
    assets = {a: SimpleNamespace(family="f") for a in characs}
    sel = m2.select_sigma_eff(characs, assets)

    assert sel["winner"] is None
    assert sel["status"] == "NO_EVALUABLE_SIGMA_CANDIDATE"
    assert sel["per_asset"] is None
    assert all(not np.isfinite(t["spearman"]) for t in sel["table"])


def test_sigma_eff_sigue_eligiendo_el_mejor_cuando_hay_evaluables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        m2,
        "SIGMA_EFF_CANDIDATES",
        {"bueno": lambda _n, f: f["sig_baja"], "malo": lambda _n, f: f["sig_sube"]},
    )
    monkeypatch.setattr(m2, "split_of", lambda _family: "CALIBRATION")
    characs = {
        f"A{i}": {
            "_normal": None,
            "features": {"sig_baja": 1.0 / (i + 1), "sig_sube": float(i + 1)},
            "raw_eval": {"aligned_rmse": 0.1 * (i + 1), "gradient_rmse": 0.2},
        }
        for i in range(5)
    }
    assets = {a: SimpleNamespace(family="f") for a in characs}
    sel = m2.select_sigma_eff(characs, assets)
    assert sel["winner"] == "bueno"
    assert sel["status"] == "EVALUABLE"


# ------------------------------------------- I. M2: sin winner no hay ejecución de policies


def _specs_y_characs() -> tuple[list[Any], dict[str, dict[str, Any]]]:
    specs = [SimpleNamespace(asset_id="A1", family="stone")]
    characs = {
        "A1": {
            "_normal": None,
            "features": {"sig": 1.0},
            "raw_eval": {"affine_scale": 1.0, "oracle_best_sign": 1.0, "aligned_rmse": 0.1, "gradient_rmse": 0.2},
        }
    }
    return specs, characs


def test_m2_sin_winner_no_ejecuta_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ancla §6 end-to-end: NO_WINNER ⇒ NO_TRANSFER_POLICY_EXECUTION.

    Encadena la selección REAL (calibración de 2 assets ⇒ todos los rho NaN) con la
    ejecución. Si la selección no es evaluable, ninguna policy dependiente de σ_eff
    (SOFT_TIKHONOV / FLOOR_CLAMP, transfer M2-E y sweep M2-F) puede correr: se hace
    explotar ``run_policy`` para que cualquier ejecución sea un fallo, no un dato.
    """

    def _no_debe_correr(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("run_policy ejecutado sin σ_eff elegido válidamente")

    monkeypatch.setattr(m2, "SIGMA_EFF_CANDIDATES", {"cand_a": lambda _n, _f: 1.0, "cand_b": lambda _n, _f: 2.0})
    monkeypatch.setattr(m2, "split_of", lambda _family: "CALIBRATION")
    monkeypatch.setattr(m2, "run_policy", _no_debe_correr)
    monkeypatch.setattr(m2, "load_asset", lambda _spec, _res: SimpleNamespace())

    characs = {
        "A1": {"_normal": None, "features": {}, "raw_eval": {"aligned_rmse": 0.1, "gradient_rmse": 0.2}},
        "A2": {"_normal": None, "features": {}, "raw_eval": {"aligned_rmse": 0.3, "gradient_rmse": 0.4}},
    }
    assets = {a: SimpleNamespace(family="f") for a in characs}
    specs = [SimpleNamespace(asset_id=a, family="f") for a in characs]

    sel = m2.select_sigma_eff(characs, assets)
    assert sel["winner"] is None
    filas = m2.evaluate_sigma_policies(specs, characs, sel, 64)

    assert filas == []
    assert sel["per_asset"] is None


def test_m2_con_winner_si_ejecuta_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control del ancla anterior: con winner válido el transfer/sweep SÍ corre.

    Sin este control, ``evaluate_sigma_policies`` podría devolver siempre [] y el
    test de arriba pasaría por la razón equivocada.
    """
    llamadas: list[tuple[Any, ...]] = []

    def _registrar(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        llamadas.append(args)
        return {"policy": args[3]}

    monkeypatch.setattr(m2, "run_policy", _registrar)
    monkeypatch.setattr(m2, "load_asset", lambda _spec, _res: SimpleNamespace())
    monkeypatch.setattr(m2, "split_of", lambda _family: "CALIBRATION")
    monkeypatch.setattr(m2, "SIGMA_EFF_CANDIDATES", {"cand": lambda _n, _f: 1.0})
    specs, characs = _specs_y_characs()
    sel = {"winner": "cand", "status": "EVALUABLE", "table": [], "per_asset": None}

    filas = m2.evaluate_sigma_policies(specs, characs, sel, 64)

    # transfer (2 policies) + sweep M2-F (2 policies × len(K_SWEEP)) sobre 1 asset CALIBRATION
    assert len(llamadas) == 2 + 2 * len(m2.K_SWEEP)
    assert len(filas) == len(llamadas)
    assert sel["per_asset"] == {"A1": 1.0}


def test_m2_main_escribe_el_estado_no_evaluable_y_conserva_raw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§3/§9: el artefacto declara el estado y NO inventa filas de policy.

    ``winner: null`` + ``status`` es JSON válido (no depende de un sanitizer) y las
    filas RAW ya calculadas sobreviven como evidencia.
    """

    def _solo_raw(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        if args[3] != "RAW":
            raise AssertionError("policy dependiente de σ_eff ejecutada sin winner")
        return {
            "aligned_rmse": 0.1,
            "gradient_rmse": 0.2,
            "variance_ratio": 0.9,
            "catastrophic": False,
            "affine_scale": 1.0,
            "oracle_best_sign": 1.0,
        }

    monkeypatch.setattr(
        m2, "load_manifest", lambda _p: [SimpleNamespace(asset_id="A1", family="f", declared_convention="DIRECTX")]
    )
    monkeypatch.setattr(m2, "load_asset", lambda _s, _r: SimpleNamespace(normal=None))
    monkeypatch.setattr(
        m2, "characterize_asset", lambda _m: {"features": {}, "raw_eval": {}, "oracle_agreement_deg": 10.0}
    )
    monkeypatch.setattr(m2, "run_policy", _solo_raw)
    monkeypatch.setattr(m2, "SIGMA_EFF_CANDIDATES", {"cand_a": lambda _n, _f: 1.0, "cand_b": lambda _n, _f: 2.0})
    monkeypatch.setattr(m2, "split_of", lambda _f: "CALIBRATION")
    monkeypatch.setattr(
        sys, "argv", ["run_exp_m2", "--manifest", str(tmp_path / "m.json"), "--out", str(tmp_path / "out")]
    )

    m2.main()

    datos = json.loads((tmp_path / "out" / "rows.json").read_text(encoding="utf-8"))
    assert datos["sigma_selection"]["winner"] is None
    assert datos["sigma_selection"]["status"] == "NO_EVALUABLE_SIGMA_CANDIDATE"
    assert len(datos["rows"]) == 1  # sólo la fila RAW del único asset: ninguna policy σ_eff
    assert (tmp_path / "out" / "characs.json").is_file()


# ---------------------------------------------------------------- I. M4 diagnóstico §16


def test_coherence_diagnostic_reporta_no_evaluable_sin_romper_json() -> None:
    """M4 serializa con ``allow_nan=False``: un NaN en el diagnóstico aborta la corrida.

    El campo está documentado como NO trust proxy ni criterio de exclusión, así que
    un resultado no evaluable debe reportarse explícito, no tumbar el reporte.
    """
    from sky_claw.local.native_parallax.research.run_exp_m4 import coherence_diagnostic

    rows = [
        {"asset": f"a{i}", "coherence_agreement_deg": 12.0, "delta_rmse": 0.05} for i in range(5)
    ]  # delta_rmse constante ⇒ Spearman no evaluable
    diag = coherence_diagnostic(rows)
    assert diag["spearman_deg_vs_delta_rmse"] is None
    assert diag["status"] == "COHERENCE_SPEARMAN_NOT_EVALUABLE"
    assert "NO es trust proxy" in diag["note"]
    json.dumps(diag, allow_nan=False)  # no debe explotar

    variado = [{"asset": f"b{i}", "coherence_agreement_deg": 10.0 + i, "delta_rmse": 0.02 * i} for i in range(5)]
    diag2 = coherence_diagnostic(variado)
    assert diag2["spearman_deg_vs_delta_rmse"] == pytest.approx(1.0)
    assert diag2["status"] == "COHERENCE_SPEARMAN_EVALUABLE"
    json.dumps(diag2, allow_nan=False)
