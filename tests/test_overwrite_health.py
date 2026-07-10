"""Tests del sensor de overwrite sucio (T-30·3 de TECHNICAL_REVIEW_TASKS.md).

El ``overwrite`` de MO2 es el destino compartido de la salida de las
herramientas (bashed patch, DynDOLOD, Synthesis, BodySlide…). Residuos previos
hacen inatribuible el diff del próximo Ritual y contaminan el clon del sandbox
(T-27). El sensor es read-only y best-effort: reporta lo que hay, nunca borra.
Suciedad = AMARILLO (nunca rojo): un Bashed Patch recién generado es un estado
legítimo a mitad de flujo — MO2 mismo solo advierte.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

from sky_claw.local.validators.overwrite_health import (
    OverwriteHealthChecker,
    OverwriteScan,
    overwrite_preflight_check,
)
from sky_claw.local.validators.preflight import PreflightStatus


def _check(overwrite_dir: pathlib.Path) -> OverwriteScan:
    return OverwriteHealthChecker(overwrite_dir=overwrite_dir).check()


# ---------------------------------------------------------------------------
# OverwriteHealthChecker (escaneo)
# ---------------------------------------------------------------------------


class TestEscaneo:
    def test_overwrite_vacio_es_scan_vacio(self, tmp_path: pathlib.Path) -> None:
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()

        scan = _check(overwrite)

        assert scan == OverwriteScan(files=(), plugins=())

    def test_overwrite_inexistente_es_scan_vacio(self, tmp_path: pathlib.Path) -> None:
        """MO2 crea el overwrite on demand: que no exista no es suciedad."""
        scan = _check(tmp_path / "no-existe")

        assert scan == OverwriteScan(files=(), plugins=())

    def test_dirs_vacios_no_cuentan_como_suciedad(self, tmp_path: pathlib.Path) -> None:
        overwrite = tmp_path / "overwrite"
        (overwrite / "SKSE" / "Plugins").mkdir(parents=True)

        scan = _check(overwrite)

        assert scan.files == ()

    def test_archivos_anidados_con_rutas_relativas(self, tmp_path: pathlib.Path) -> None:
        overwrite = tmp_path / "overwrite"
        (overwrite / "SKSE").mkdir(parents=True)
        (overwrite / "SKSE" / "skse64.log").write_text("log", encoding="utf-8")
        (overwrite / "suelto.txt").write_text("x", encoding="utf-8")

        scan = _check(overwrite)

        assert set(scan.files) == {"SKSE/skse64.log", "suelto.txt"}
        assert scan.plugins == ()

    def test_plugins_se_destacan_del_resto(self, tmp_path: pathlib.Path) -> None:
        """Un plugin en el overwrite entra al load order con máxima precedencia
        sin estar gestionado como mod: se reporta aparte."""
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()
        (overwrite / "Bashed Patch, 0.esp").write_bytes(b"TES4")
        (overwrite / "readme.txt").write_text("x", encoding="utf-8")

        scan = _check(overwrite)

        assert "Bashed Patch, 0.esp" in scan.plugins
        assert set(scan.files) == {"Bashed Patch, 0.esp", "readme.txt"}

    def test_oserror_al_escanear_no_explota(self, tmp_path: pathlib.Path) -> None:
        """Sensor best-effort: un overwrite ilegible degrada a scan vacío, no a
        excepción (el preflight nunca debe caerse por un sensor)."""
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()
        (overwrite / "residuo.txt").write_text("x", encoding="utf-8")

        with patch.object(pathlib.Path, "rglob", side_effect=OSError("denegado")):
            scan = _check(overwrite)

        assert scan == OverwriteScan(files=(), plugins=())


# ---------------------------------------------------------------------------
# overwrite_preflight_check (puente al semáforo)
# ---------------------------------------------------------------------------


class TestPuenteAlSemaforo:
    def test_limpio_es_verde(self) -> None:
        check = overwrite_preflight_check(OverwriteScan(files=(), plugins=()))

        assert check.name == "overwrite"
        assert check.status is PreflightStatus.GREEN
        assert "limpio" in check.summary.lower()

    def test_residuos_es_amarillo_nunca_rojo(self, tmp_path: pathlib.Path) -> None:
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()
        (overwrite / "Bashed Patch, 0.esp").write_bytes(b"TES4")
        (overwrite / "log.txt").write_text("x", encoding="utf-8")

        check = overwrite_preflight_check(_check(overwrite))

        assert check.status is PreflightStatus.YELLOW
        assert "2" in check.summary  # conteo de archivos
        assert "1" in check.summary  # conteo de plugins

    def test_details_incluyen_rutas_y_remediacion(self) -> None:
        scan = OverwriteScan(files=("SKSE/skse64.log", "suelto.txt"), plugins=())

        check = overwrite_preflight_check(scan)

        assert "SKSE/skse64.log" in check.details
        assert any("mod" in d.lower() for d in check.details)  # remediación accionable

    def test_details_se_capan_ante_muchos_archivos(self) -> None:
        """Un overwrite con cientos de archivos (BodySlide) no debe inflar el
        reporte: se listan los primeros y se resume el resto."""
        scan = OverwriteScan(files=tuple(f"meshes/{i:03}.nif" for i in range(25)), plugins=())

        check = overwrite_preflight_check(scan)

        listadas = [d for d in check.details if d.startswith("meshes/")]
        assert len(listadas) == 10
        assert any("15 más" in d for d in check.details)
