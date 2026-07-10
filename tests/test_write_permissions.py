"""Tests del sensor de permisos de escritura (T-30·4 de TECHNICAL_REVIEW_TASKS.md).

El clásico "Skyrim/MO2 bajo Program Files sin admin": el Ritual muere a mitad de
escritura. El sensor lo detecta ANTES de tocar nada con un **write-probe real**
(crear + borrar un archivo temporal único) — `os.access(W_OK)` es inútil en
Windows (ignora ACLs). Un permiso denegado en una ruta que el Ritual va a
escribir es **crítico/rojo** (el fallo es seguro, a diferencia del overwrite
sucio que solo advierte).
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from sky_claw.local.validators.preflight import PreflightStatus
from sky_claw.local.validators.write_permissions import (
    WriteAccessReport,
    WritePermissionsChecker,
    permissions_preflight_check,
)

_root_guard = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root escribe aunque el modo sea 0o555 (típico en contenedores de CI)",
)
_posix_guard = pytest.mark.skipif(sys.platform == "win32", reason="chmod 0o555 no aplica en Windows")


def _check(*targets: pathlib.Path) -> WriteAccessReport:
    return WritePermissionsChecker(targets=list(targets)).check()


# ---------------------------------------------------------------------------
# WritePermissionsChecker (probe)
# ---------------------------------------------------------------------------


class TestProbe:
    def test_dir_escribible_es_verde_y_no_deja_residuo(self, tmp_path: pathlib.Path) -> None:
        report = _check(tmp_path)

        assert report.issues == ()
        assert str(tmp_path) in report.probed
        # El probe se limpió: la carpeta queda sin archivos .skyclaw_probe_*.
        assert list(tmp_path.iterdir()) == []

    def test_dir_inexistente_se_saltea(self, tmp_path: pathlib.Path) -> None:
        report = _check(tmp_path / "no-existe")

        assert report.probed == ()
        assert report.issues == ()

    def test_target_que_es_archivo_se_saltea(self, tmp_path: pathlib.Path) -> None:
        archivo = tmp_path / "f.txt"
        archivo.write_text("x", encoding="utf-8")

        report = _check(archivo)

        assert report.probed == ()

    def test_permission_error_mapea_critical(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Independiente de plataforma: un PermissionError al abrir el probe es
        denegación de escritura → crítico."""
        destino = tmp_path / "d"
        destino.mkdir()

        def _boom(self: pathlib.Path, *a: object, **k: object) -> None:
            raise PermissionError("denegado")

        monkeypatch.setattr(pathlib.Path, "open", _boom)
        report = _check(destino)

        assert len(report.issues) == 1
        assert report.issues[0].kind == "denied"
        assert report.issues[0].severity == "critical"

    def test_otro_oserror_mapea_warning(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        destino = tmp_path / "d"
        destino.mkdir()

        def _boom(self: pathlib.Path, *a: object, **k: object) -> None:
            raise OSError("disco lleno")

        monkeypatch.setattr(pathlib.Path, "open", _boom)
        report = _check(destino)

        assert report.issues[0].kind == "error"
        assert report.issues[0].severity == "warning"

    def test_unlink_fallido_es_probe_residue(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si el probe se escribió pero no se pudo borrar, se avisa (warning) para
        que el operador limpie el residuo."""
        destino = tmp_path / "d"
        destino.mkdir()
        real_unlink = pathlib.Path.unlink

        def _boom(self: pathlib.Path, *a: object, **k: object) -> None:
            raise OSError("bloqueado")

        monkeypatch.setattr(pathlib.Path, "unlink", _boom)
        report = _check(destino)

        assert report.issues[0].kind == "probe_residue"
        assert report.issues[0].severity == "warning"
        # Limpieza real del residuo (unlink estaba parcheado durante el check).
        monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
        for residuo in destino.iterdir():
            residuo.unlink()

    @_posix_guard
    @_root_guard
    def test_dir_sin_permiso_de_escritura_es_critical(self, tmp_path: pathlib.Path) -> None:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            report = _check(readonly)
            assert any(i.severity == "critical" for i in report.issues)
        finally:
            readonly.chmod(0o755)


# ---------------------------------------------------------------------------
# permissions_preflight_check (puente al semáforo)
# ---------------------------------------------------------------------------


class TestPuenteAlSemaforo:
    def test_sin_issues_es_verde(self, tmp_path: pathlib.Path) -> None:
        check = permissions_preflight_check(_check(tmp_path))

        assert check.name == "write_permissions"
        assert check.status is PreflightStatus.GREEN
        assert "1" in check.summary  # una ruta verificada

    def test_critical_fuerza_rojo(self) -> None:
        from sky_claw.local.validators.write_permissions import WriteAccessIssue

        report = WriteAccessReport(
            probed=("C:/Program Files/Skyrim/Data",),
            issues=(
                WriteAccessIssue(
                    path="C:/Program Files/Skyrim/Data",
                    kind="denied",
                    severity="critical",
                    remediation="corré fuera de Program Files",
                ),
            ),
        )

        check = permissions_preflight_check(report)

        assert check.status is PreflightStatus.RED
        assert any("Program Files" in d for d in check.details)

    def test_solo_warnings_es_amarillo(self) -> None:
        from sky_claw.local.validators.write_permissions import WriteAccessIssue

        report = WriteAccessReport(
            probed=("/mnt/net/mods",),
            issues=(
                WriteAccessIssue(path="/mnt/net/mods", kind="error", severity="warning", remediation="revisá la ruta"),
            ),
        )

        check = permissions_preflight_check(report)

        assert check.status is PreflightStatus.YELLOW
