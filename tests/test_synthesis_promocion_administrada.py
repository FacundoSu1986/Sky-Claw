"""PR-2b2 (ADR 0019 §2.3): promoción de la salida administrada con ``DirectoryRollback``.

La raíz administrada (``<mods>/Sky-Claw Synthesis Output``) se protege con un move-aside O(1), no
con el snapshot copy-based. Propiedades del mecanismo, cada una con su caso:

- *los grupos no seleccionados se preservan*: la raíz se regenera con todo lo que NO es salida de
  un grupo de esta corrida (copiado del backup) más lo que produce el CLI.
- *un fallo devuelve la raíz byte a byte* y la transacción queda ``rolled_back``.
- *la terminalidad desconocida veta el rollback*: el backup y el parcial quedan, la TX pendiente.
- *la sombra se vuelve a mirar antes de confirmar*: un ``<Grupo>.esp`` que aparezca en
  ``overwrite`` durante la corrida aborta y restaura (carrera del preflight, triage de #1022).
- *el residuo tiene dueño*: ``construir_productores_de_move_aside`` declara el productor
  ``synthesis`` con el destino exacto y el lock del ritual.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.mo2 import brokered_synthesis
from sky_claw.local.mo2.vfs_broker import VfsTeardownError
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob, VfsJobResult
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import execute_worker_manifest
from sky_claw.local.tools.output_targets import SYNTHESIS_MANAGED_MOD_NAME
from sky_claw.local.tools.rollback_reconciler import (
    PRODUCTORES_CABLEADOS,
    SUFIJO_MOVE_ASIDE,
    construir_productores_de_move_aside,
)
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from tests.test_brokered_synthesis import (
    _construir_tes4,
    _escenario_mo2,
    _PreparadorFalso,
    _probe_ok,
    _resolver_para_servicio,
)

_ESP_VALIDO = _construir_tes4(["Skyrim.esm", "MiModVirtual.esp"])
_SALIDA_OK = b"[PatcherA] Starting run.\n[PatcherA] Run successful.\nExported patch to final destination: out\n"


def _residuos(raiz: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in raiz.parent.iterdir() if SUFIJO_MOVE_ASIDE.search(p.name))


class _BrokerConCli:
    """Broker que ejecuta el worker REAL con un CLI falso (mismo arnés que el rerun brokered)."""

    def __init__(self, rutas: dict[str, pathlib.Path], cli: Any) -> None:
        self._rutas = rutas
        self._cli = cli

    async def submit(self, job: VfsJob, **kwargs: Any) -> VfsJobResult:
        manifest = VfsWorkerManifest(
            protocol_version=VFS_PROTOCOL_VERSION,
            job=job,
            challenge=kwargs["challenge"],
            mo2_root=self._rutas["mo2"],
            mods_dir=self._rutas["mods"],
            virtual_data_dir=self._rutas["data_virtual"],
            descriptor_path=self._rutas["mo2"] / "descriptor.json",
        )
        with patch("sky_claw.local.tools.synthesis_runner.run_capture", self._cli):
            return await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)


async def _correr(
    tmp_path: pathlib.Path, rutas: dict[str, pathlib.Path], broker: Any
) -> tuple[dict[str, Any], AsyncMock, SynthesisPipelineService]:
    lock_mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snaps = tmp_path / "snapshots"
    snaps.mkdir(exist_ok=True)
    snap_mgr = FileSnapshotManager(snapshot_dir=snaps)
    await snap_mgr.initialize()
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=21)
    event_bus = CoreEventBus()
    await event_bus.start()
    try:
        svc = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,
            path_resolver=_resolver_para_servicio(rutas),
            event_bus=event_bus,
            synthesis_settings_path=rutas["settings"],
            vfs_broker=broker,
            instance_id="portable-main",
            synthesis_backend="brokered",
            preparador_fase1=_PreparadorFalso(rutas["mo2"] / "fase1"),
        )
        out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
    finally:
        await event_bus.stop()
        await lock_mgr.close()
    return out, journal, svc


def _sembrar_raiz_previa(rutas: dict[str, pathlib.Path]) -> dict[str, bytes]:
    raiz = rutas["output"]
    previos = {
        "Synthesis.esp": _construir_tes4(["Skyrim.esm"]) + b"previo",
        "Synthesis_2.esp": _construir_tes4(["Skyrim.esm"]) + b"split-previo",
        "OtroGrupo.esp": _construir_tes4(["Skyrim.esm"]) + b"otro",
        "meta.ini": b"[General]\n",
    }
    for nombre, contenido in previos.items():
        (raiz / nombre).write_bytes(contenido)
    return previos


@pytest.fixture(autouse=True)
def _hold_levantado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)


async def test_exito_regenera_la_raiz_y_preserva_los_grupos_no_seleccionados(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    previos = _sembrar_raiz_previa(rutas)
    nuevo = _construir_tes4(["Skyrim.esm", "MiModVirtual.esp"])

    async def _cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
        # El CLI arranca sin las salidas de los grupos de esta corrida (frescura §2.5.5).
        assert not (rutas["output"] / "Synthesis.esp").exists()
        assert not (rutas["output"] / "Synthesis_2.esp").exists()
        (rutas["output"] / "Synthesis.esp").write_bytes(nuevo)
        return _SALIDA_OK, b"", 0

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerConCli(rutas, _cli))

    assert out["success"] is True, out
    raiz = rutas["output"]
    assert (raiz / "Synthesis.esp").read_bytes() == nuevo
    # El split viejo del grupo regenerado NO sobrevive (sería un split stale).
    assert not (raiz / "Synthesis_2.esp").exists()
    # Lo que no es salida de un grupo seleccionado se preserva byte a byte.
    assert (raiz / "OtroGrupo.esp").read_bytes() == previos["OtroGrupo.esp"]
    assert (raiz / "meta.ini").read_bytes() == previos["meta.ini"]
    assert _residuos(raiz) == []
    journal.commit_transaction.assert_awaited_once_with(21)
    journal.mark_transaction_rolled_back.assert_not_awaited()


async def test_primer_run_crea_la_raiz_administrada(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    rutas["output"].rmdir()

    async def _cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
        (rutas["output"] / "Synthesis.esp").write_bytes(_ESP_VALIDO)
        return _SALIDA_OK, b"", 0

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerConCli(rutas, _cli))

    assert out["success"] is True, out
    assert (rutas["output"] / "Synthesis.esp").is_file()
    assert _residuos(rutas["output"]) == []
    journal.commit_transaction.assert_awaited_once_with(21)


async def test_un_fallo_devuelve_la_raiz_byte_a_byte(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    previos = _sembrar_raiz_previa(rutas)

    async def _cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
        (rutas["output"] / "Synthesis.esp").write_bytes(b"parcial")
        return b"Overall error: boom\n", b"", 1

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerConCli(rutas, _cli))

    assert out["success"] is False
    raiz = rutas["output"]
    assert {p.name: p.read_bytes() for p in raiz.iterdir()} == previos
    assert _residuos(raiz) == []
    journal.mark_transaction_rolled_back.assert_awaited_once_with(21)
    journal.commit_transaction.assert_not_awaited()


async def test_la_terminalidad_desconocida_conserva_backup_y_parcial(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    previos = _sembrar_raiz_previa(rutas)

    class _BrokerHuerfano:
        async def submit(self, _job: VfsJob, **_kw: Any) -> VfsJobResult:
            (rutas["output"] / "Synthesis.esp").write_bytes(b"parcial")
            raise VfsTeardownError("teardown fallido: el worker puede seguir vivo")

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerHuerfano())

    assert out["success"] is False
    raiz = rutas["output"]
    # El parcial queda donde el worker lo dejó y el backup es la única copia del estado previo.
    assert (raiz / "Synthesis.esp").read_bytes() == b"parcial"
    residuos = _residuos(raiz)
    assert len(residuos) == 1
    assert {p.name: p.read_bytes() for p in residuos[0].iterdir()} == previos
    journal.mark_transaction_rolled_back.assert_not_awaited()
    journal.commit_transaction.assert_not_awaited()


async def test_una_sombra_que_aparece_durante_la_corrida_aborta_y_restaura(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    previos = _sembrar_raiz_previa(rutas)
    overwrite = rutas["mo2"] / "overwrite"

    async def _cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
        (rutas["output"] / "Synthesis.esp").write_bytes(_ESP_VALIDO)
        overwrite.mkdir(exist_ok=True)
        (overwrite / "Synthesis.esp").write_bytes(b"de otra herramienta")
        return _SALIDA_OK, b"", 0

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerConCli(rutas, _cli))

    assert out["success"] is False
    assert "overwrite" in out["message"]
    raiz = rutas["output"]
    assert {p.name: p.read_bytes() for p in raiz.iterdir()} == previos
    assert _residuos(raiz) == []
    # El archivo del operador no se toca.
    assert (overwrite / "Synthesis.esp").read_bytes() == b"de otra herramienta"
    journal.mark_transaction_rolled_back.assert_awaited_once_with(21)
    journal.commit_transaction.assert_not_awaited()


def test_el_reconciliador_declara_el_productor_de_la_raiz_administrada(tmp_path: pathlib.Path) -> None:
    mo2 = tmp_path / "mo2"
    mods = tmp_path / "mods_declarado"

    for kwargs, esperado in (
        ({"mo2_root": mo2}, mo2 / "mods" / SYNTHESIS_MANAGED_MOD_NAME),
        ({"mo2_root": mo2, "mods_dir": mods}, mods / SYNTHESIS_MANAGED_MOD_NAME),
    ):
        productores = construir_productores_de_move_aside(**kwargs)
        synthesis = next(p for p in productores if p.nombre == "synthesis")
        assert synthesis.destinos == (esperado,)
        # El mismo lock que toma el servicio (RESOURCE_ID): mientras corre, el residuo no se toca.
        assert synthesis.lock_resource_id == SynthesisPipelineService.RESOURCE_ID
        # Una raíz confirmada nunca queda vacía (lleva al menos un <Grupo>.esp).
        assert synthesis.destinos_nunca_vacios == (esperado,)
    assert "synthesis" in PRODUCTORES_CABLEADOS


async def test_una_finalizacion_fallida_deja_la_tx_pendiente(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El run confirmó pero el backup no se pudo descartar: la salida nueva está aplicada y el estado
    previo sigue en disco. Ni ``committed`` (el move-aside no cerró) ni ``rolled_back`` (no se revirtió
    nada): la TX queda pendiente y el resultado no es un éxito."""
    from sky_claw.local.tools import _dir_rollback

    rutas = _escenario_mo2(tmp_path)
    _sembrar_raiz_previa(rutas)

    async def _descarte_fallido(self: Any, *, permitir_restore: bool) -> bool:
        return False

    monkeypatch.setattr(_dir_rollback.DirectoryRollback, "_discard_backup", _descarte_fallido)

    async def _cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
        (rutas["output"] / "Synthesis.esp").write_bytes(_ESP_VALIDO)
        return _SALIDA_OK, b"", 0

    out, journal, _ = await _correr(tmp_path, rutas, _BrokerConCli(rutas, _cli))

    assert out["success"] is False
    assert "pendiente" in out["message"]
    assert (rutas["output"] / "Synthesis.esp").read_bytes() == _ESP_VALIDO
    assert len(_residuos(rutas["output"])) == 1
    journal.commit_transaction.assert_not_awaited()
    journal.mark_transaction_rolled_back.assert_not_awaited()
