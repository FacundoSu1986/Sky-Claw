"""PR-2b1 (ADR 0019 §2.3/§2.4): raíz administrada, timeout de la corrida y sombra del ``overwrite``.

Tres propiedades del mecanismo, cada una con su ancla:

- *el timeout de la corrida es UNO y lo comparten los dos backends*: el servicio recibe
  ``run_timeout_seconds`` (900 por defecto, reemplaza los 300 rígidos de standalone) y lo
  pasa a TODA ``SynthesisConfig`` que construye. Se enumeran los dos backends, no se muestrea.
- *la salida administrada solo sirve si gana en el overlay*: en MO2 el ``overwrite`` le gana a
  todo mod, así que un ``<Grupo>.esp`` (o un split ``<Grupo>_N.esp``) viejo ahí taparía la
  salida nueva de ``Sky-Claw Synthesis Output``. El preflight brokered lo detecta y falla
  cerrado ANTES de la Fase 1, sin abrir transacción ni despachar al broker.
- *standalone no cambia*: sigue escribiendo en el ``overwrite`` (o en el clon del sandbox), así
  que ahí no hay sombra que buscar.
"""

from __future__ import annotations

import math
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.mo2 import brokered_synthesis
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from tests.test_brokered_synthesis import (
    _construir_tes4,
    _escenario_mo2,
    _PreparadorFalso,
    _resolver_para_servicio,
    _servicio_brokered,
)

_CHEQUEO_SOMBRA = "synthesis_overwrite_shadow"


def _servicio(rutas: dict[str, pathlib.Path], backend: str, **kw: Any) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=_resolver_para_servicio(rutas),
        event_bus=MagicMock(),
        synthesis_settings_path=rutas["settings"],
        vfs_broker=MagicMock(),
        instance_id="portable-main",
        synthesis_backend=backend,
        **kw,
    )


# ---------------------------------------------------------------------------
# Timeout de la corrida
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["standalone", "brokered"])
def test_el_timeout_de_la_corrida_llega_al_runner_de_los_dos_backends(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    por_defecto = _servicio(_escenario_mo2(tmp_path / "def"), backend)
    assert por_defecto._ensure_synthesis_runner()._config.timeout_seconds == 900
    propio = _servicio(_escenario_mo2(tmp_path / "propio"), backend, run_timeout_seconds=1800)
    assert propio._ensure_synthesis_runner()._config.timeout_seconds == 1800


@pytest.mark.parametrize("invalido", [0, -1, True, 1.5, math.inf, "900", None])
def test_un_timeout_invalido_falla_al_construir(tmp_path: pathlib.Path, invalido: Any) -> None:
    with pytest.raises(ValueError, match="run_timeout_seconds"):
        _servicio(_escenario_mo2(tmp_path), "standalone", run_timeout_seconds=invalido)


# ---------------------------------------------------------------------------
# Sombra del overwrite sobre la salida administrada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sombra", ["Synthesis.esp", "synthesis_2.ESP", "Synthesis_12.esp"])
async def test_un_output_viejo_en_overwrite_bloquea_el_brokered_antes_de_la_fase1(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sombra: str
) -> None:
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    overwrite = rutas["mo2"] / "overwrite"
    overwrite.mkdir()
    (overwrite / sombra).write_bytes(_construir_tes4(["Skyrim.esm"]))
    broker = MagicMock(submit=AsyncMock())
    journal = AsyncMock()
    preparador = _PreparadorFalso(rutas["mo2"] / "fase1")

    out = await _servicio_brokered(rutas, broker, preparador, journal=journal).execute_pipeline(
        patcher_ids=["PatcherA"]
    )

    assert out["success"] is False
    assert out["reason"] == "PreflightBlocked"
    chequeo = next(c for c in out["preflight"]["checks"] if c["name"] == _CHEQUEO_SOMBRA)
    assert chequeo["status"] == "red"
    assert sombra in chequeo["details"]
    # Nada se mutó: sin Fase 1, sin transacción, sin despacho, y el archivo del operador intacto.
    assert preparador.llamadas == []
    journal.begin_transaction.assert_not_awaited()
    broker.submit.assert_not_awaited()
    assert (overwrite / sombra).is_file()


def test_otros_plugins_en_overwrite_no_son_sombra(tmp_path: pathlib.Path) -> None:
    """Solo cuentan los outputs de los grupos seleccionados; ``SynthesisExtra.esp`` no es un split.

    (Un semáforo verde no viaja en el resultado de ``execute_pipeline`` —contrato de
    ``_attach_preflight``—, así que el verde se lee del constructor del chequeo.)
    """
    rutas = _escenario_mo2(tmp_path)
    overwrite = rutas["mo2"] / "overwrite"
    overwrite.mkdir()
    for nombre in ("Otro.esp", "SynthesisExtra.esp", "Synthesis_1.esp", "Synthesis.esp.bak"):
        (overwrite / nombre).write_bytes(b"x")
    chequeo = _servicio(rutas, "brokered")._build_overwrite_shadow_check(("Synthesis",))
    assert chequeo is not None
    assert chequeo.status.value == "green"


def test_overwrite_ausente_no_es_sombra(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    chequeo = _servicio(rutas, "brokered")._build_overwrite_shadow_check(("Synthesis",))
    assert chequeo is not None
    assert chequeo.status.value == "green"


def test_la_salida_del_sandbox_no_busca_sombra(tmp_path: pathlib.Path) -> None:
    """Con override (clon del sandbox, hasta PR-2b3) la salida no es la administrada."""
    rutas = _escenario_mo2(tmp_path)
    svc = _servicio(rutas, "brokered", output_path=rutas["output"])
    assert svc._build_overwrite_shadow_check(("Synthesis",)) is None


async def test_un_overwrite_ilegible_o_enlazado_falla_cerrado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin poder enumerar el ``overwrite`` la ausencia de sombra no está probada: rojo."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    (rutas["mo2"] / "overwrite").write_text("no soy una carpeta", encoding="utf-8")
    preparador = _PreparadorFalso(rutas["mo2"] / "fase1")

    out = await _servicio_brokered(rutas, MagicMock(submit=AsyncMock()), preparador).execute_pipeline(
        patcher_ids=["PatcherA"]
    )

    assert out["reason"] == "PreflightBlocked"
    chequeo = next(c for c in out["preflight"]["checks"] if c["name"] == _CHEQUEO_SOMBRA)
    assert chequeo["status"] == "red"
    assert preparador.llamadas == []


def test_standalone_no_busca_sombra(tmp_path: pathlib.Path) -> None:
    """Standalone escribe EN el ``overwrite`` (o en el clon): un output ahí es el suyo, no una sombra."""
    rutas = _escenario_mo2(tmp_path)
    svc = _servicio(rutas, "standalone")
    assert svc._build_overwrite_shadow_check(("Synthesis",)) is None


# ---------------------------------------------------------------------------
# Hermano del lado de la LECTURA: el guard de etapas previas de los LODs
# ---------------------------------------------------------------------------


def test_las_carpetas_de_salida_cubren_todo_destino_posible(tmp_path: pathlib.Path) -> None:
    """Igualdad, no muestreo: toda carpeta que ``synthesis_output_target`` puede devolver está
    en la lista que el guard de la etapa 9 escanea en busca de splits.

    Si un destino nuevo se agrega al resolver y no a la lista, un split escrito ahí no se exige
    habilitado antes de DynDOLOD (falso verde). Se recorren todas las ramas del resolver.
    """
    from sky_claw.local.tools.output_targets import (
        carpetas_de_salida_de_synthesis,
        synthesis_output_target,
    )

    # Las dos ramas del mods_dir: declarado, y sin resolver (el resolver cae a <datos>/mods).
    for nombre, mods in (("declarado", tmp_path / "mods_declarado"), ("sin_resolver", None)):
        mo2 = tmp_path / nombre / "mo2"
        destinos: set[pathlib.Path] = set()
        for con_overwrite in (False, True):
            if con_overwrite:
                (mo2 / "overwrite").mkdir(parents=True)
            for administrada in (False, True):
                destino = synthesis_output_target(mo2=mo2, override=None, mods_dir=mods, administrada=administrada)
                assert destino is not None
                destinos.add(destino)
        assert set(carpetas_de_salida_de_synthesis(data_root=mo2, mods_dir=mods)) == destinos, nombre


def test_dyndolod_descubre_los_splits_de_la_raiz_administrada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un ``Synthesis_2.esp`` en ``Sky-Claw Synthesis Output`` es un plugin que la etapa 9 exige."""
    import sky_claw.local.validators.etapas_previas_lods as etapas
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    rutas = _escenario_mo2(tmp_path)
    (rutas["output"] / "Synthesis_2.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
    capturado: dict[str, Any] = {}
    descubridor_real = etapas.descubrir_plugins_de_synthesis

    def _descubrir(**kw: Any) -> tuple[str, ...]:
        capturado.update(kw)
        return ("Synthesis.esp",)

    monkeypatch.setattr(etapas, "descubrir_plugins_de_synthesis", _descubrir)
    monkeypatch.setattr(etapas, "verificar_etapas_previas_de_lods", lambda **_kw: None)
    svc = object.__new__(DynDOLODPipelineService)
    resolver = _resolver_para_servicio(rutas)
    resolver.get_mo2_instance_data_root_estricto.return_value = rutas["mo2"]
    svc._path_resolver = resolver  # type: ignore[attr-defined]
    svc._mo2_profile = "Default"  # type: ignore[attr-defined]
    monkeypatch.setattr(svc, "_plugins_txt_del_perfil_activo", lambda: rutas["plugins"], raising=False)

    svc.verificar_etapas_previas()

    assert rutas["output"] in list(capturado["carpetas_de_salida"])
    # Con el descubridor real, el split de la raíz administrada aparece.
    plugins = descubridor_real(
        settings_path=rutas["settings"],
        profile_identifier="Default",
        carpetas_de_salida=capturado["carpetas_de_salida"],
    )
    assert "Synthesis_2.esp" in plugins


# ---------------------------------------------------------------------------
# Sondeo de permisos sobre un destino que todavía no existe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["standalone", "brokered"])
def test_el_sondeo_de_permisos_sube_al_ancestro_existente_si_el_destino_no_existe(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """``WritePermissionsChecker`` saltea rutas inexistentes: pasarle un destino que la corrida
    todavía no creó era un verde sin sondeo. Se sondea el ancestro existente más cercano, que
    es donde la corrida va a tener que crear la carpeta. Los dos backends (standalone sin
    ``overwrite`` cae en ``mods/Synthesis Output``; brokered en la raíz administrada)."""
    from unittest.mock import patch

    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    rutas["output"].rmdir()  # la raíz administrada todavía no existe (primera corrida)
    svc = _servicio(rutas, backend)
    checker = MagicMock()
    with patch("sky_claw.local.validators.write_permissions.WritePermissionsChecker", checker):
        preflight = svc._ensure_preflight()
        assert preflight is not None
        preflight._permissions_check()  # type: ignore[misc]
    assert checker.call_args.kwargs["targets"] == [rutas["mods"]]


def test_el_sondeo_de_permisos_usa_el_destino_si_existe(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import patch

    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    svc = _servicio(rutas, "brokered")
    checker = MagicMock()
    with patch("sky_claw.local.validators.write_permissions.WritePermissionsChecker", checker):
        preflight = svc._ensure_preflight()
        assert preflight is not None
        preflight._permissions_check()  # type: ignore[misc]
    assert checker.call_args.kwargs["targets"] == [rutas["output"]]
