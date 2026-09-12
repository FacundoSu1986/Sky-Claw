"""P2.3 fenced — el startup recovery no muta ACTIVE_TARGET externos sin autoridad.

Blocker P1 de P2.3: antes del fix, el recovery leía el `RegistroDeRootActivo`
y podía llegar a `backup.rename(destino)` protegiéndose sólo con
`dyndolod-pipeline`, sin demostrar que este proceso posee temporalmente el
lifetime ownership (`dyndolod-ownership-<clave>`) del `ResourceBinding`
correspondiente, ni que cada `external_work_root` sigue siendo un workspace
propio, válido y compatible (binding en disco).

Secuencia fenced requerida (orden congelado, nunca al revés):

```text
ResourceBinding actual
        ↓
dyndolod-workspace
        ↓
adquirir ownership temporal dyndolod-ownership-<clave>
        ↓
leer RegistroDeRootActivo
        ↓
validar cada root contra su binding real en disco
        ↓
dyndolod-pipeline
        ↓
reconcile_orphan_rollback_backups
        ↓
liberar pipeline → liberar ownership temporal
```

Fail-safe (nunca fail-open): ocupado → skip external recovery con warning y
startup que continúa; binding ajeno/corrupto/ausente → skip ese root sin mutar;
registro ausente → no-op; registro corrupto → no external recovery + warning;
pipeline ocupado → skip como ya hace hoy.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools import dyndolod_workspace as wsm
from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner
from sky_claw.local.tools.output_targets import derivar_layout_de_dyndolod
from sky_claw.local.tools.rollback_reconciler import (
    PRODUCTOR_DYNDOLOD,
    PRODUCTOR_LEGACY,
    construir_productores_de_move_aside,
    reconcile_orphan_rollback_backups,
)

_NONCE = "1753700000000000000"


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


def _recursos(tmp_path: pathlib.Path, sufijo: str = "") -> wsm.ResourceBinding:
    game = tmp_path / f"game{sufijo}"
    datos = tmp_path / f"mo2{sufijo}"
    mods = datos / "mods"
    for d in (game, datos, mods):
        d.mkdir(parents=True, exist_ok=True)
    return wsm.ResourceBinding.desde_paths(
        game_path=game,
        mo2_instance_data_root=datos,
        mo2_mods_path=mods,
    )


def _game_mo2(tmp_path: pathlib.Path, sufijo: str = "") -> tuple[pathlib.Path, SimpleNamespace]:
    game = tmp_path / f"game{sufijo}"
    mo2_root = tmp_path / f"mo2{sufijo}"
    (mo2_root / "mods").mkdir(parents=True, exist_ok=True)
    game.mkdir(parents=True, exist_ok=True)
    mo2 = SimpleNamespace(data_root=mo2_root, mods_dir=mo2_root / "mods")
    return game, mo2


def _escribir_binding_valido(
    root: pathlib.Path, recursos: wsm.ResourceBinding, binding_id: str = "11111111-2222-3333-4444-555555555555"
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / wsm.ARCHIVO_DE_BINDING).write_text(
        json.dumps(
            {
                "schema_version": wsm.SCHEMA_VERSION,
                "binding_id": binding_id,
                "resource_binding": {
                    "game_path": recursos.game_path,
                    "mo2_instance_data_root": recursos.mo2_instance_data_root,
                    "mo2_mods_path": recursos.mo2_mods_path,
                },
            }
        ),
        encoding="utf-8",
    )


def _coordinacion(tmp_path: pathlib.Path, nombre: str = "estado") -> wsm.Stage9Coordination:
    return wsm.construir_coordinacion_de_etapa9(base=tmp_path / nombre)


def _app_con_coordinacion(tmp_path: pathlib.Path, coordinacion: wsm.Stage9Coordination):
    from sky_claw.app_context import AppContext

    args = SimpleNamespace(db_path=tmp_path / "app.db")
    app = AppContext(args)
    app.stage9_coordination = coordinacion
    return app


def _sembrar_backup_externo(
    external: pathlib.Path, herramienta: str = "TexGen", contenido: bytes = b"BYTES-PREVIOS"
) -> tuple[pathlib.Path, pathlib.Path]:
    layout = derivar_layout_de_dyndolod(external_work_root=external)
    root = layout.texgen_root if herramienta == "TexGen" else layout.dyndolod_root
    backup = root.with_name(f"{root.name}.rollback-{_NONCE}")
    backup.mkdir(parents=True)
    (backup / "payload.dds").write_bytes(contenido)
    return root, backup


def _sembrar_backup_mod(mods: pathlib.Path, nombre: str, contenido: str = "previo") -> pathlib.Path:
    backup = mods / f"{nombre}.rollback-{_NONCE}"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "DynDOLOD.esp").write_text(contenido, encoding="utf-8")
    return backup


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path):
    mgr = DistributedLockManager(
        tmp_path / "locks.db",
        default_ttl=30.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    try:
        yield mgr
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# §15 — dos holders reales: el owner vivo bloquea, con el pipeline LIBRE
# ---------------------------------------------------------------------------


async def test_owner_vivo_bloquea_recovery_con_pipeline_libre(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    """A posee lifetime ownership de X (sin tomar el pipeline); B intenta recovery.

    Pipeline LIBRE a propósito: si B no restaura, el guard que lo impidió es el
    ownership, no el lock del ritual. B no restaura ningún ACTIVE_TARGET, no
    modifica mods asociados, reporta omitido/ocupado, backup byte-exacto y target
    ausente.
    """
    from sky_claw.app_context import AppContext

    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    _escribir_binding_valido(external, recursos, binding_id="binding-a")
    registro.registrar_activa(clave=recursos.clave(), root=external, binding_id="binding-a")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target, backup = _sembrar_backup_externo(external, "TexGen", b"GEN-VIEJA")
    mods_backup = _sembrar_backup_mod(mo2.mods_dir, DynDOLODRunner.TEXGEN_MOD_NAME, "MOD-PREVIO")
    mods_target = mo2.mods_dir / DynDOLODRunner.TEXGEN_MOD_NAME

    coord_a = _coordinacion(tmp_path)
    coord_b = _coordinacion(tmp_path)
    try:
        # A adquiere lifetime ownership REAL (P2.0), sin tocar el pipeline.
        owner_a = await coord_a.adquirir_ownership_vivo(recursos=recursos)
        try:
            # Pipeline demostrablemente LIBRE.
            manager_b = await coord_b.manager_del_ritual()
            assert await manager_b.get_lock_info(coord_b.RECURSO_DEL_RITUAL) is None

            # B inicia startup recovery con OTRA coordinación (otro proceso).
            app_b = AppContext(SimpleNamespace(db_path=tmp_path / "app_b.db"))
            app_b.stage9_coordination = coord_b
            ownership_b, roots_b, omitido = await app_b._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
            assert omitido is True, "con owner vivo ajeno el recovery debe reportarse omitido"
            assert ownership_b is None
            assert roots_b == ()

            # Producción filtra la familia dyndolod completa (roots + mods).
            productores_base = construir_productores_de_move_aside(
                mo2_root=mo2.data_root,
                mods_dir=mo2.mods_dir,
                game=game,
                external_work_roots=roots_b,
            )
            assert any(p.nombre == PRODUCTOR_DYNDOLOD for p in productores_base)
            productores = [p for p in productores_base if p.nombre != PRODUCTOR_DYNDOLOD]
            resultado = await reconcile_orphan_rollback_backups(
                productores=productores,
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord_b,
            )

            # Nada restaurado, nada creado, backups byte-exactos.
            assert not target.exists()
            assert backup.is_dir() and (backup / "payload.dds").read_bytes() == b"GEN-VIEJA"
            assert not mods_target.exists()
            assert mods_backup.is_dir()
            assert (mods_backup / "DynDOLOD.esp").read_text(encoding="utf-8") == "MOD-PREVIO"
            assert resultado.restaurados == ()
        finally:
            await owner_a.liberar()
    finally:
        await coord_a.close()
        await coord_b.close()


# ---------------------------------------------------------------------------
# §16 — holder muerto: tras expirar, el recovery sí restaura
# ---------------------------------------------------------------------------


async def test_holder_muerto_no_wedgea_recovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    """A adquiere y muere (sin renovar); tras expirar, B adquiere y restaura."""
    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    _escribir_binding_valido(external, recursos, binding_id="binding-a")
    registro.registrar_activa(clave=recursos.clave(), root=external, binding_id="binding-a")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target, backup = _sembrar_backup_externo(external, "DynDOLOD", b"BYTES-PREVIOS")

    coord_a = _coordinacion(tmp_path)
    coord_b = _coordinacion(tmp_path)
    try:
        # TTL corto sin renovación = muerte dura sin cleanup. La ventana de
        # retries de adquisición (≈1.5s) supera este TTL a propósito: B puede
        # adquirir en el primer intento tras expirar, lo que ya demuestra que
        # un holder muerto no wedgea. El bloqueo con holder VIVO se demuestra
        # en `test_owner_vivo_bloquea_recovery_con_pipeline_libre` con TTL 60s.
        muerto = await coord_a.adquirir_ownership_vivo(recursos=recursos, ttl=0.4, auto_renew=False)
        from sky_claw.app_context import AppContext

        app_b = AppContext(SimpleNamespace(db_path=tmp_path / "app_b.db"))
        app_b.stage9_coordination = coord_b

        await asyncio.sleep(0.7)  # la lease del muerto expira: nadie renovó.

        ownership_b, roots_b, omitido = await app_b._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert omitido is False
        assert ownership_b is not None
        assert roots_b == (external,)
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots_b
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord_b,
            )
        finally:
            await ownership_b.liberar()

        assert target.is_dir() and (target / "payload.dds").read_bytes() == b"BYTES-PREVIOS"
        assert not backup.exists()
        assert target in resultado.restaurados
        # El handle del muerto queda fenced y su liberar no toca la lease nueva.
        await muerto.liberar()
    finally:
        await coord_a.close()
        await coord_b.close()


# ---------------------------------------------------------------------------
# §17 — binding válido: el happy path sigue restaurando
# ---------------------------------------------------------------------------


async def test_binding_valido_restaura_con_autoridad_temporal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    _escribir_binding_valido(external, recursos)
    registro.registrar_activa(clave=recursos.clave(), root=external, binding_id="11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target, backup = _sembrar_backup_externo(external, "TexGen")

    coord = _coordinacion(tmp_path)
    try:
        from sky_claw.app_context import AppContext

        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, omitido = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert omitido is False
        assert ownership is not None
        assert roots == (external,)
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            await ownership.liberar()
        assert target.is_dir() and (target / "payload.dds").read_bytes() == b"BYTES-PREVIOS"
        assert not backup.exists()
        assert target in resultado.restaurados
    finally:
        await coord.close()


# ---------------------------------------------------------------------------
# §18 — binding ajeno: NO MUTATION y no alcanza backup.rename()
# ---------------------------------------------------------------------------


async def test_binding_ajeno_no_restaura(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    game, mo2 = _game_mo2(tmp_path)
    recursos_x = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    recursos_y = _recursos(tmp_path, sufijo="_ajeno")
    assert recursos_y.clave() != recursos_x.clave()

    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    _escribir_binding_valido(external, recursos_y, binding_id="99999999-0000-0000-0000-000000000000")
    registro.registrar_activa(
        clave=recursos_x.clave(), root=external, binding_id="99999999-0000-0000-0000-000000000000"
    )
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target, backup = _sembrar_backup_externo(external, "TexGen", b"AJENO-NO-TOCAR")

    from sky_claw.app_context import AppContext

    assert AppContext._roots_externos_para_recovery(game=game, mo2=mo2) == ()

    coord = _coordinacion(tmp_path)
    try:
        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, omitido = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        # Sin live owner, la autoridad se adquiere, pero el root ajeno se filtra.
        assert omitido is False
        assert roots == ()
        renames: list[tuple[pathlib.Path, pathlib.Path]] = []
        real_rename = pathlib.Path.rename

        def _rename_espia(self: pathlib.Path, destino: pathlib.Path) -> pathlib.Path:
            renames.append((self, destino))
            return real_rename(self, destino)

        monkeypatch.setattr(pathlib.Path, "rename", _rename_espia)
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            if ownership is not None:
                await ownership.liberar()
        assert not target.exists()
        assert backup.is_dir() and (backup / "payload.dds").read_bytes() == b"AJENO-NO-TOCAR"
        assert resultado.restaurados == ()
        assert all(backup != origen for origen, _ in renames), "el backup ajeno alcanzó backup.rename()"
    finally:
        await coord.close()


# ---------------------------------------------------------------------------
# §19 — binding corrupto / ausente: NO MUTATION, sin fabricar binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variante",
    ["json-roto", "schema-desconocido", "ausente", "vacio"],
)
async def test_binding_corrupto_o_ausente_no_muta(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_manager: DistributedLockManager,
    variante: str,
) -> None:
    from sky_claw.app_context import AppContext

    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    external.mkdir(parents=True, exist_ok=True)
    archivo = external / wsm.ARCHIVO_DE_BINDING
    if variante == "json-roto":
        archivo.write_text("{no es json", encoding="utf-8")
    elif variante == "schema-desconocido":
        archivo.write_text(
            json.dumps({"schema_version": 99, "binding_id": "x", "resource_binding": {}}),
            encoding="utf-8",
        )
    elif variante == "vacio":
        archivo.write_text("", encoding="utf-8")
    else:  # ausente: no se crea el archivo
        pass
    registro.registrar_activa(clave=recursos.clave(), root=external, binding_id="binding-a")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target, backup = _sembrar_backup_externo(external, "TexGen", b"UNICA-COPIA")
    # El backup vive bajo DynDOLOD/; el binding (o su ausencia) vive en la raíz.
    # Sembrar el backup no debe crear ni reparar el binding.
    antes = archivo.read_bytes() if archivo.exists() and variante != "ausente" else None

    assert AppContext._roots_externos_para_recovery(game=game, mo2=mo2) == ()

    coord = _coordinacion(tmp_path)
    try:
        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, _ = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert roots == ()
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            if ownership is not None:
                await ownership.liberar()
        assert not target.exists()
        assert backup.is_dir() and (backup / "payload.dds").read_bytes() == b"UNICA-COPIA"
        assert resultado.restaurados == ()
        if antes is None:
            assert not archivo.exists(), "el recovery fabricó un binding durante el barrido"
        else:
            assert archivo.read_bytes() == antes, "el recovery reescribió metadata corrupta"
    finally:
        await coord.close()


# ---------------------------------------------------------------------------
# §20 — transición pendiente A→B: validación propia por root, sin all-or-nothing
# ---------------------------------------------------------------------------


async def test_transicion_pendiente_ambos_validos_restauran(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    from sky_claw.app_context import AppContext

    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    viejo = tmp_path / "Work A"
    nuevo = tmp_path / "Work B"
    registro.registrar_activa(clave=recursos.clave(), root=viejo, binding_id="binding-a")
    registro.registrar_transicion(clave=recursos.clave(), hacia=nuevo, motivo="cambio")
    _escribir_binding_valido(viejo, recursos, binding_id="binding-a")
    _escribir_binding_valido(nuevo, recursos, binding_id="binding-b")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target_a, backup_a = _sembrar_backup_externo(viejo, "TexGen", b"PREVIO-EN-A")
    target_b, backup_b = _sembrar_backup_externo(nuevo, "DynDOLOD", b"PREVIO-EN-B")

    assert AppContext._roots_externos_para_recovery(game=game, mo2=mo2) == (nuevo, viejo)

    coord = _coordinacion(tmp_path)
    try:
        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, omitido = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert omitido is False
        assert set(roots) == {nuevo, viejo}
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            assert ownership is not None
            await ownership.liberar()
        assert (target_a / "payload.dds").read_bytes() == b"PREVIO-EN-A"
        assert (target_b / "payload.dds").read_bytes() == b"PREVIO-EN-B"
        assert not backup_a.exists() and not backup_b.exists()
        assert set(resultado.restaurados) == {target_a, target_b}
    finally:
        await coord.close()


async def test_transicion_pendiente_solo_el_valido_restaura(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    """A válido + B ajeno: A se recupera, B se omite (no all-or-nothing)."""
    from sky_claw.app_context import AppContext

    game, mo2 = _game_mo2(tmp_path)
    recursos_x = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    recursos_y = _recursos(tmp_path, sufijo="_ajeno")
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    viejo = tmp_path / "Work A"
    nuevo = tmp_path / "Work B"
    registro.registrar_activa(clave=recursos_x.clave(), root=viejo, binding_id="binding-a")
    registro.registrar_transicion(clave=recursos_x.clave(), hacia=nuevo, motivo="cambio")
    _escribir_binding_valido(viejo, recursos_x, binding_id="binding-a")
    _escribir_binding_valido(nuevo, recursos_y, binding_id="binding-ajeno")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    target_a, backup_a = _sembrar_backup_externo(viejo, "TexGen", b"BUENO-EN-A")
    target_b, backup_b = _sembrar_backup_externo(nuevo, "TexGen", b"MALO-EN-B")

    assert AppContext._roots_externos_para_recovery(game=game, mo2=mo2) == (viejo,)

    coord = _coordinacion(tmp_path)
    try:
        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, _ = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert roots == (viejo,)
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            assert ownership is not None
            await ownership.liberar()
        assert (target_a / "payload.dds").read_bytes() == b"BUENO-EN-A"
        assert not backup_a.exists()
        assert target_a in resultado.restaurados
        assert not target_b.exists()
        assert backup_b.is_dir() and (backup_b / "payload.dds").read_bytes() == b"MALO-EN-B"
    finally:
        await coord.close()


# ---------------------------------------------------------------------------
# §12 — root activo corrupto/ajeno fuera del árbol: outside intacto byte-exacto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variante", ["ausente", "ajeno", "corrupto"])
async def test_root_registrado_fuera_del_arbol_no_se_toca(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_manager: DistributedLockManager,
    variante: str,
) -> None:
    from sky_claw.app_context import AppContext

    game, mo2 = _game_mo2(tmp_path)
    recursos_x = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    ajeno = tmp_path / "SomeOtherTree"
    layout = derivar_layout_de_dyndolod(external_work_root=ajeno)
    backup = layout.texgen_root.with_name(f"{layout.texgen_root.name}.rollback-{_NONCE}")
    backup.mkdir(parents=True)
    (backup / "payload.dds").write_bytes(b"OUTSIDE-PREVIO")
    if variante == "ajeno":
        _escribir_binding_valido(ajeno, _recursos(tmp_path, sufijo="_otro"), binding_id="otro")
    elif variante == "corrupto":
        ajeno.mkdir(parents=True, exist_ok=True)
        (ajeno / wsm.ARCHIVO_DE_BINDING).write_text("{roto", encoding="utf-8")
    else:
        ajeno.mkdir(parents=True, exist_ok=True)
        # Sin binding a propósito.

    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    registro.registrar_activa(clave=recursos_x.clave(), root=ajeno, binding_id="binding-ajeno")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    assert AppContext._roots_externos_para_recovery(game=game, mo2=mo2) == ()

    coord = _coordinacion(tmp_path)
    try:
        app = AppContext(SimpleNamespace(db_path=tmp_path / "app.db"))
        app.stage9_coordination = coord
        ownership, roots, _ = await app._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
        assert roots == ()
        try:
            resultado = await reconcile_orphan_rollback_backups(
                productores=construir_productores_de_move_aside(
                    mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots
                ),
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord,
            )
        finally:
            if ownership is not None:
                await ownership.liberar()
        assert not layout.texgen_root.exists(), "se creó un target fuera del árbol administrado"
        assert backup.is_dir() and (backup / "payload.dds").read_bytes() == b"OUTSIDE-PREVIO"
        assert resultado.restaurados == ()
    finally:
        await coord.close()


# ---------------------------------------------------------------------------
# §13 — el legacy recovery-only sigue corriendo aunque dyndolod se omita
# ---------------------------------------------------------------------------


async def test_legacy_sigue_aunque_dyndolod_se_omita_por_ownership(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, lock_manager: DistributedLockManager
) -> None:
    game, mo2 = _game_mo2(tmp_path)
    recursos = wsm.ResourceBinding.desde_paths(
        game_path=game, mo2_instance_data_root=mo2.data_root, mo2_mods_path=mo2.mods_dir
    )
    registro = wsm.RegistroDeRootActivo(tmp_path / "estado" / "active_roots.json")
    external = tmp_path / "Work A"
    _escribir_binding_valido(external, recursos)
    registro.registrar_activa(clave=recursos.clave(), root=external, binding_id="binding-a")
    monkeypatch.setattr(wsm, "registro_de_roots_activos", lambda: registro)

    from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner as Runner
    from sky_claw.local.tools.output_targets import dyndolod_legacy_recovery_target

    raiz_legacy = dyndolod_legacy_recovery_target(game=game)
    assert raiz_legacy is not None
    legacy_target = raiz_legacy / Runner.TEXGEN_OUTPUT_NAME
    legacy_backup = legacy_target.with_name(f"{legacy_target.name}.rollback-{_NONCE}")
    legacy_backup.mkdir(parents=True)
    (legacy_backup / "lod.dds").write_bytes(b"LEGACY-PREVIO")

    coord_a = _coordinacion(tmp_path)
    coord_b = _coordinacion(tmp_path)
    try:
        owner_a = await coord_a.adquirir_ownership_vivo(recursos=recursos)
        try:
            from sky_claw.app_context import AppContext

            app_b = AppContext(SimpleNamespace(db_path=tmp_path / "app_b.db"))
            app_b.stage9_coordination = coord_b
            _, roots_b, omitido = await app_b._autoridad_temporal_para_recovery_externo(game=game, mo2=mo2)
            assert omitido is True
            productores_base = construir_productores_de_move_aside(
                mo2_root=mo2.data_root, mods_dir=mo2.mods_dir, game=game, external_work_roots=roots_b
            )
            # Sólo se salta dyndolod; el legacy queda.
            productores = [p for p in productores_base if p.nombre != PRODUCTOR_DYNDOLOD]
            assert any(p.nombre == PRODUCTOR_LEGACY for p in productores)
            resultado = await reconcile_orphan_rollback_backups(
                productores=productores,
                sandbox_root=None,
                lock_manager=lock_manager,
                coordinacion_etapa9=coord_b,
            )
            assert legacy_target.is_dir()
            assert (legacy_target / "lod.dds").read_bytes() == b"LEGACY-PREVIO"
            assert legacy_target in resultado.restaurados
        finally:
            await owner_a.liberar()
    finally:
        await coord_a.close()
        await coord_b.close()


# ---------------------------------------------------------------------------
# Orden de locks del startup recovery: workspace → ownership → pipeline
# ---------------------------------------------------------------------------


def test_orden_de_locks_del_startup_recovery_congelado() -> None:
    """El recovery fenced adquiere workspace, luego ownership, luego pipeline.

    Nunca pipeline antes que ownership (inversión/deadlock). Se ancla por
    posición en el AST de `app_context.py`: la autoridad temporal (workspace +
    ownership) precede al `reconcile_orphan_rollback_backups` (que adquiere el
    pipeline), y dentro de la autoridad el workspace precede al ownership.
    """
    import ast

    import sky_claw.app_context as app_context_mod

    fuente = pathlib.Path(app_context_mod.__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)

    autoridad = next(
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_autoridad_temporal_para_recovery_externo"
    )
    pos_workspace: int | None = None
    pos_ownership: int | None = None
    pos_pipeline_en_autoridad: int | None = None
    for nodo in ast.walk(autoridad):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            if nodo.func.attr == "sostener_workspace" and pos_workspace is None:
                pos_workspace = nodo.lineno
            if nodo.func.attr == "adquirir_ownership_vivo" and pos_ownership is None:
                pos_ownership = nodo.lineno
            # MP8: cualquier adquisición del pipeline dentro de la autoridad debe
            # ir DESPUÉS del ownership, nunca antes (inversión/deadlock).
            if (
                nodo.func.attr in ("sostener_ritual", "manager_del_ritual", "acquire_lock")
                and pos_pipeline_en_autoridad is None
            ):
                pos_pipeline_en_autoridad = nodo.lineno
        # `RECURSO_DEL_RITUAL` / `reconcile_...` como nombre también cuentan.
        if isinstance(nodo, ast.Name) and nodo.id in ("RECURSO_DEL_RITUAL",) and pos_pipeline_en_autoridad is None:
            pos_pipeline_en_autoridad = nodo.lineno
    assert pos_workspace is not None, "la autoridad temporal no sostiene dyndolod-workspace"
    assert pos_ownership is not None, "la autoridad temporal no adquiere dyndolod-ownership"
    assert pos_workspace < pos_ownership, "inversión: ownership antes que workspace"
    if pos_pipeline_en_autoridad is not None:
        assert pos_ownership < pos_pipeline_en_autoridad, (
            "inversión MP8: el pipeline se adquiere antes que el ownership dentro de la autoridad"
        )

    arranque = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_start_full_inner")
    posiciones: dict[str, int] = {}
    for nodo in ast.walk(arranque):
        if isinstance(nodo, ast.Call):
            nombre = None
            if isinstance(nodo.func, ast.Name):
                nombre = nodo.func.id
            elif isinstance(nodo.func, ast.Attribute):
                nombre = nodo.func.attr
            if nombre in ("_autoridad_temporal_para_recovery_externo", "reconcile_orphan_rollback_backups"):
                posiciones.setdefault(nombre, nodo.lineno)
    assert set(posiciones) == {
        "_autoridad_temporal_para_recovery_externo",
        "reconcile_orphan_rollback_backups",
    }, f"wiring del recovery fenced incompleto: {sorted(posiciones)}"
    assert posiciones["_autoridad_temporal_para_recovery_externo"] < posiciones["reconcile_orphan_rollback_backups"], (
        "inversión MP8: el pipeline (reconcile) se adquiere antes que el ownership"
    )
