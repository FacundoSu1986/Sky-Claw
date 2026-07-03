"""Tests de la sección Descargas del Forge (última pantalla placeholder).

Cubre la fuente de datos real del "Registro de la Puerta": lectura del
``task_log`` del registry (hasta ahora write-only) con nombre de mod resuelto.
"""

from __future__ import annotations

import pytest

from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.gui.views.forge_dashboard import (
    GREEN,
    RED,
    _hitl_modal_visible,
    _task_log_row_html,
)


class TestGetTaskLog:
    @pytest.mark.asyncio
    async def test_devuelve_filas_recientes_primero(self, async_registry: AsyncModRegistry) -> None:
        await async_registry.log_tasks_batch(
            [
                (None, "sync", "ok", "primera"),
                (None, "download_mod", "failed", "segunda"),
            ]
        )
        rows = await async_registry.get_task_log()
        assert len(rows) == 2
        # Orden descendente por inserción (log_id): lo más nuevo primero.
        assert rows[0]["detail"] == "segunda"
        assert rows[0]["action"] == "download_mod"
        assert rows[0]["status"] == "failed"
        assert rows[1]["detail"] == "primera"
        assert "created_at" in rows[0]

    @pytest.mark.asyncio
    async def test_resuelve_nombre_de_mod_cuando_hay_mod_id(self, async_registry: AsyncModRegistry) -> None:
        mod_id = await async_registry.upsert_mod(nexus_id=4242, name="SkyUI")
        await async_registry.log_tasks_batch([(mod_id, "install_mod", "registered", "v5.2")])
        rows = await async_registry.get_task_log()
        assert rows[0]["mod_name"] == "SkyUI"

    @pytest.mark.asyncio
    async def test_mod_name_none_sin_mod_id(self, async_registry: AsyncModRegistry) -> None:
        await async_registry.log_tasks_batch([(None, "sync", "ok", "x")])
        rows = await async_registry.get_task_log()
        assert rows[0]["mod_name"] is None

    @pytest.mark.asyncio
    async def test_respeta_el_limite(self, async_registry: AsyncModRegistry) -> None:
        await async_registry.log_tasks_batch([(None, "sync", "ok", f"fila {i}") for i in range(10)])
        rows = await async_registry.get_task_log(limit=3)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_vacio_devuelve_lista_vacia(self, async_registry: AsyncModRegistry) -> None:
        assert await async_registry.get_task_log() == []

    @pytest.mark.asyncio
    async def test_limite_no_positivo_no_vuelca_todo(self, async_registry: AsyncModRegistry) -> None:
        """En SQLite ``LIMIT -1`` significa "sin límite"; el límite debe clampearse (review Copilot #224)."""
        await async_registry.log_tasks_batch([(None, "sync", "ok", f"fila {i}") for i in range(5)])
        assert await async_registry.get_task_log(limit=-1) == []
        assert await async_registry.get_task_log(limit=0) == []


class TestHitlModalVisible:
    """El modal global se suprime en Descargas (review Codex #224).

    El overlay full-screen taparía la Puerta de Aprobación inline — que es la
    UI más rica: muestra la URL de la descarga, dato que el modal no tiene.
    """

    def test_se_suprime_en_downloads(self) -> None:
        assert _hitl_modal_visible({"request_id": "r1"}, "Downloads") is False

    def test_visible_en_cualquier_otra_seccion(self) -> None:
        assert _hitl_modal_visible({"request_id": "r1"}, "Dashboard") is True
        assert _hitl_modal_visible({"request_id": "r1"}, "Mods") is True

    def test_sin_solicitud_pendiente_nunca_se_muestra(self) -> None:
        assert _hitl_modal_visible(None, "Dashboard") is False
        assert _hitl_modal_visible({}, "Downloads") is False


class TestLoadDownloadsHistory:
    """Disparador de carga del historial (suscripción, no render — review Copilot #224).

    Cargar dentro de ``main_page`` repetía la query en cada refresh de la página
    y duplicaba la carga al entrar (render→set→refresh→render). El trigger vive
    en los suscriptores del store: navegar a Descargas o mover la puerta HITL.
    """

    @pytest.mark.asyncio
    async def test_puebla_el_store_desde_el_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from sky_claw.antigravity.gui import sky_claw_gui as gui
        from sky_claw.antigravity.gui import task_tracking
        from sky_claw.antigravity.gui.state import get_store, reset_store_for_tests

        reset_store_for_tests()
        filas = [{"action": "sync", "status": "ok", "detail": "x", "mod_name": None, "created_at": "hoy"}]

        class _StubRegistry:
            async def get_task_log(self, limit: int = 50) -> list[dict[str, object]]:
                assert limit == 50
                return filas

        runtime = SimpleNamespace(app_context=SimpleNamespace(registry=_StubRegistry()))
        monkeypatch.setattr(gui, "get_runtime_context", lambda: runtime)

        gui._load_downloads_history()
        await asyncio.gather(*list(task_tracking._BACKGROUND_TASKS))

        assert get_store().get("downloads_history") == filas

    @pytest.mark.asyncio
    async def test_sin_registry_no_rompe_ni_toca_el_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from sky_claw.antigravity.gui import sky_claw_gui as gui
        from sky_claw.antigravity.gui.state import get_store, reset_store_for_tests

        reset_store_for_tests()
        runtime = SimpleNamespace(app_context=SimpleNamespace(registry=None))
        monkeypatch.setattr(gui, "get_runtime_context", lambda: runtime)

        gui._load_downloads_history()  # no debe lanzar ni crear tasks

        assert get_store().get("downloads_history") is None


class TestTaskLogRowHtml:
    """Seam puro de la fila del Registro de la Puerta (sin contexto NiceGUI)."""

    def test_fila_completa_muestra_accion_mod_y_fecha(self) -> None:
        html = _task_log_row_html(
            {
                "action": "install_mod",
                "status": "registered",
                "detail": "v5.2",
                "mod_name": "SkyUI",
                "created_at": "2026-07-03 10:00:00",
            }
        )
        assert "install_mod" in html
        assert "SkyUI" in html
        assert "2026-07-03 10:00:00" in html
        # El nombre de mod desplaza al detail como sujeto de la fila.
        assert "v5.2" not in html

    def test_sin_mod_name_cae_al_detail(self) -> None:
        html = _task_log_row_html(
            {"action": "sync", "status": "ok", "detail": "sincronización completa", "mod_name": None}
        )
        assert "sincronización completa" in html

    def test_color_por_status_y_neutro_para_desconocidos(self) -> None:
        assert GREEN in _task_log_row_html({"status": "OK"})  # case-insensitive
        assert RED in _task_log_row_html({"status": "failed"})
        neutro = _task_log_row_html({"status": "algo_raro"})
        assert GREEN not in neutro
        assert RED not in neutro

    def test_escapa_contenido(self) -> None:
        html = _task_log_row_html({"action": "<script>x</script>", "detail": "a & b", "status": "ok"})
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "a &amp; b" in html
