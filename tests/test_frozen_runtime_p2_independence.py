"""Tests de P2 — independencia física Generation ↔ Managed Source (PI*, SFR-18).

Cubre aliasing, contención, symlink/junction/reparse y hardlinks. En Windows
los enlaces/junctions se crean con las primitives del sistema (sin privilegios)
y se saltan explícitamente cuando la plataforma no los soporta — la ausencia
de prueba jamás se convierte en PASS.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from sky_claw.local.frozen_runtime import (
    IndependenceState,
    verify_generation_independence,
    verify_generation_physical_integrity,
)


def _arbol_generacion(base: pathlib.Path, *, contenido: bytes = b"contenido-independiente") -> pathlib.Path:
    (base / "Data").mkdir(parents=True)
    (base / "SkyrimSE.exe").write_bytes(contenido)
    (base / "Data" / "Skyrim.esm").write_bytes(b"esm")
    return base


def _arbol_fuente(base: pathlib.Path) -> pathlib.Path:
    (base / "Data").mkdir(parents=True)
    (base / "SkyrimSE.exe").write_bytes(b"exe-fuente")
    (base / "Data" / "Skyrim.esm").write_bytes(b"esm-fuente")
    return base


def _enlace_directorio(destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Crea un enlace de directorio: junction en Windows, symlink en POSIX."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)], check=True, capture_output=True)
    else:
        enlace.symlink_to(destino, target_is_directory=True)


def _hardlink(objetivo: pathlib.Path, enlace: pathlib.Path) -> None:
    try:
        os.link(objetivo, enlace)
    except (OSError, NotImplementedError) as exc:  # filesystem sin hardlinks
        pytest.skip(f"hardlinks no disponibles en este filesystem: {exc}")


class TestIndependenciaFisica:
    def test_pi01_mismo_root_rechazado(self, tmp_path: pathlib.Path) -> None:
        arbol = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_independence(arbol, arbol)
        assert resultado.state is IndependenceState.VIOLATED

    def test_pi02_generation_dentro_de_fuente_rechazado(self, tmp_path: pathlib.Path) -> None:
        fuente = _arbol_fuente(tmp_path / "fuente")
        gen = _arbol_generacion(fuente / "gen_adentro")
        assert verify_generation_independence(gen, fuente).state is IndependenceState.VIOLATED

    def test_pi02b_fuente_dentro_de_generation_rechazado(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        fuente = _arbol_fuente(gen / "fuente_adentro")
        assert verify_generation_independence(gen, fuente).state is IndependenceState.VIOLATED

    @pytest.mark.skipif(os.name == "nt", reason="symlink requiere dev mode en Windows; PI04 cubre junction")
    def test_pi03_symlink_alias_rechazado(self, tmp_path: pathlib.Path) -> None:
        real = _arbol_generacion(tmp_path / "gen_real")
        alias = tmp_path / "gen_alias"
        alias.symlink_to(real, target_is_directory=True)
        fuente = _arbol_fuente(tmp_path / "fuente")
        resultado = verify_generation_independence(alias, fuente)
        assert resultado.state is IndependenceState.VIOLATED

    @pytest.mark.skipif(os.name != "nt", reason="junction es Windows-only")
    def test_pi04_junction_alias_rechazado(self, tmp_path: pathlib.Path) -> None:
        real = _arbol_generacion(tmp_path / "gen_real")
        alias = tmp_path / "gen_alias"
        _enlace_directorio(real, alias)
        fuente = _arbol_fuente(tmp_path / "fuente")
        resultado = verify_generation_independence(alias, fuente)
        assert resultado.state is IndependenceState.VIOLATED

    def test_pi05_reparse_dentro_de_generation_rechazado(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        externo = tmp_path / "externo"
        externo.mkdir()
        _enlace_directorio(externo, gen / "Data" / "enlace")
        fuente = _arbol_fuente(tmp_path / "fuente")
        resultado = verify_generation_independence(gen, fuente)
        assert resultado.state is IndependenceState.VIOLATED
        assert "reparse" in resultado.message or "enlace" in resultado.message

    def test_pi06_hardlink_compartido_rechazado(self, tmp_path: pathlib.Path) -> None:
        fuente = _arbol_fuente(tmp_path / "fuente")
        gen = tmp_path / "gen"
        (gen / "Data").mkdir(parents=True)
        # MISMO objeto físico por hardlink: distinto path NO implica independencia
        _hardlink(fuente / "SkyrimSE.exe", gen / "SkyrimSE.exe")
        (gen / "Data" / "Skyrim.esm").write_bytes(b"esm")
        resultado = verify_generation_independence(gen, fuente)
        assert resultado.state is IndependenceState.VIOLATED
        assert resultado.shared_objects, "debe reportar el objeto compartido con evidencia"
        evidencia = resultado.shared_objects[0]
        assert evidencia.volume_serial > 0
        assert evidencia.file_index > 0
        assert evidencia.rel_path_generation == "SkyrimSE.exe"

    def test_pi07_copia_independiente_pasa(self, tmp_path: pathlib.Path) -> None:
        fuente = _arbol_fuente(tmp_path / "fuente")
        gen = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_independence(gen, fuente)
        assert resultado.state is IndependenceState.INDEPENDENT
        assert not resultado.shared_objects

    def test_pi08_fuente_ausente_indeterminate(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_independence(gen, tmp_path / "no_existe")
        assert resultado.state is IndependenceState.INDETERMINATE

    def test_pi09_reparse_en_fuente_indeterminate(self, tmp_path: pathlib.Path) -> None:
        fuente = _arbol_fuente(tmp_path / "fuente")
        externo = tmp_path / "externo"
        externo.mkdir()
        _enlace_directorio(externo, fuente / "Data" / "enlace")
        gen = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_independence(gen, fuente)
        assert resultado.state is IndependenceState.INDETERMINATE
        assert "reparse" in resultado.message or "enlace" in resultado.message


class TestIntegridadFisicaPropia:
    """SFR-18 on-demand de la Generation, sin requerir la Managed Source (P2-B2)."""

    def test_pi10_generation_independiente_pasa(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        assert verify_generation_physical_integrity(gen).state is IndependenceState.INDEPENDENT

    def test_pi13_copia_normal_nlink_uno(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_physical_integrity(gen)
        assert resultado.state is IndependenceState.INDEPENDENT
        assert os.stat(gen / "SkyrimSE.exe").st_nlink == 1

    def test_pi14_sin_managed_source_la_integridad_se_demuestra(self, tmp_path: pathlib.Path) -> None:
        # La primitive no recibe ni requiere una Managed Source: rollback futuro
        # no puede depender de Steam.
        gen = _arbol_generacion(tmp_path / "gen")
        resultado = verify_generation_physical_integrity(gen)
        assert resultado.state is IndependenceState.INDEPENDENT

    def test_pi10b_reparse_dentro_violacion(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        externo = tmp_path / "externo"
        externo.mkdir()
        _enlace_directorio(externo, gen / "Data" / "enlace")
        resultado = verify_generation_physical_integrity(gen)
        assert resultado.state is IndependenceState.VIOLATED

    def test_pi10c_hardlink_dentro_violacion_sin_fuente(self, tmp_path: pathlib.Path) -> None:
        gen = _arbol_generacion(tmp_path / "gen")
        objetivo = gen / "Data" / "Skyrim.esm"
        bytes_originales = objetivo.read_bytes()
        externo = tmp_path / "externo.esm"
        externo.write_bytes(bytes_originales)
        objetivo.unlink()
        _hardlink(externo, objetivo)
        resultado = verify_generation_physical_integrity(gen)
        assert resultado.state is IndependenceState.VIOLATED
        assert "multi-link" in resultado.message or "hardlink" in resultado.message

    def test_pi10d_generation_ausente_indeterminate(self, tmp_path: pathlib.Path) -> None:
        resultado = verify_generation_physical_integrity(tmp_path / "no_existe")
        assert resultado.state is IndependenceState.INDETERMINATE
