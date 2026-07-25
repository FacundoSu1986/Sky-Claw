"""Tests de cableado del checkpoint WAL periódico y atexit.

Valida que ``init_all()`` registra (o no) el handler de ``atexit``
según el valor de ``DatabaseLifecycleConfig.enable_auto_checkpoint``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Ruta temporal para la base de datos de prueba."""
    return tmp_path / "wiring_test.db"


# ---------------------------------------------------------------------------
# Rama habilitada (default): atexit.register SÍ se invoca
# ---------------------------------------------------------------------------


async def test_init_all_registra_atexit_cuando_auto_checkpoint_habilitado(
    db_path: Path,
) -> None:
    """Cuando enable_auto_checkpoint=True (default), init_all() debe
    registrar el handler de atexit para el checkpoint sincrónico."""
    config = DatabaseLifecycleConfig(enable_signal_handlers=False)
    manager = DatabaseLifecycleManager(db_paths=[db_path], config=config)

    with patch("atexit.register") as mock_register:
        await manager.init_all()
        mock_register.assert_called_once_with(manager._sync_shutdown)

    await manager.shutdown_all()


# ---------------------------------------------------------------------------
# Rama deshabilitada: atexit.register NO se invoca
# ---------------------------------------------------------------------------


async def test_init_all_no_registra_atexit_cuando_auto_checkpoint_deshabilitado(
    db_path: Path,
) -> None:
    """Cuando enable_auto_checkpoint=False, init_all() NO debe
    registrar ningún handler de atexit."""
    config = DatabaseLifecycleConfig(
        enable_auto_checkpoint=False,
        enable_signal_handlers=False,
    )
    manager = DatabaseLifecycleManager(db_paths=[db_path], config=config)

    with patch("atexit.register") as mock_register:
        await manager.init_all()
        mock_register.assert_not_called()

    await manager.shutdown_all()
