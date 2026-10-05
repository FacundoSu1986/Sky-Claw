"""P3 ronda 4 — P3-T: contención en la MATERIALIZACION de directorios.

Finding de Codex sobre `c420d287` (`copying.py:291`): el guard de P3-P corre por
ARCHIVO, despues de que TODO el arbol de directorios se materializo. Un prefix ya
creado (`payload/Data`) que se reemplace por un junction hace que
`mkdir(parents=True)` cree los niveles siguientes fuera del `FrozenRuntimeRoot`.

Es especialmente grave para los directorios VACIOS: P3 los sella y los conserva a
proposito, asi que pueden no tener ningun archivo posterior que dispare el guard
de P3-P y el escape queda invisible.

RED-first: cada test se escribio y se vio fallar contra `c420d287`.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError
from tests._symlink_guard import crear_junction, junction_guard


def _armar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    raiz_origen = tmp_path / "source"
    raiz_origen.mkdir(parents=True)
    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    destino = contenedor / "candidates" / "cand_x" / "payload"
    externo = tmp_path / "externo"
    externo.mkdir()
    return raiz_origen, contenedor, destino, externo


def _swap_tras_crear(nombre: str, externo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Reemplaza por un junction el directorio `nombre` JUSTO despues de crearlo.

    Es la unica forma determinista de alcanzar la ventana: el prefix era legitimo
    cuando se creo, y deja de serlo antes de que el loop cree el nivel siguiente.
    """
    original_mkdir = pathlib.Path.mkdir
    estado = {"cambiado": False}

    def mkdir_con_swap(self: pathlib.Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        resultado = original_mkdir(self, *args, **kwargs)
        if not estado["cambiado"] and self.name == nombre:
            estado["cambiado"] = True
            shutil.rmtree(self)
            if (motivo := crear_junction(self, externo)) is not None:
                pytest.skip(f"no se pudo crear junction: {motivo}")
        return resultado

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir_con_swap)
    return estado


# ── P3-T · un directorio VACIO sellado no puede crearse fuera del root ─────


@junction_guard
def test_p3t_swap_de_prefijo_nido_no_crea_el_directorio_vacio_afuera(tmp_path, monkeypatch) -> None:
    """`payload/Data` -> junction: `Data/EmptyFolder` NO puede aparecer afuera.

    No hay ningun archivo en la copia, asi que el guard de P3-P (por archivo) no
    tiene oportunidad de correr: este es exactamente el caso que P3-T denuncia.
    """
    raiz_origen, contenedor, destino, externo = _armar(tmp_path)
    estado = _swap_tras_crear("Data", externo, monkeypatch)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, (), ("Data", "Data/EmptyFolder"), contenedor=contenedor)

    assert estado["cambiado"] is True, "el test no alcanzo la ventana que dice cubrir"
    assert not (externo / "EmptyFolder").exists(), "se creo un directorio FUERA del FrozenRuntimeRoot"
    assert list(externo.iterdir()) == [], "hubo mutacion en el destino externo"


# ── P3-T · lo mismo para un nivel mas profundo ─────────────────────────────


@junction_guard
def test_p3t_swap_de_prefijo_profundo_no_crea_niveles_afuera(tmp_path, monkeypatch) -> None:
    """Un prefix profundo redirigido tampoco puede materializar sus descendientes."""
    raiz_origen, contenedor, destino, externo = _armar(tmp_path)
    estado = _swap_tras_crear("Meshes", externo, monkeypatch)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(
            raiz_origen,
            destino,
            (),
            ("Data", "Data/Meshes", "Data/Meshes/Weapons"),
            contenedor=contenedor,
        )

    assert estado["cambiado"] is True, "el test no alcanzo la ventana que dice cubrir"
    assert list(externo.iterdir()) == [], "hubo mutacion en el destino externo"


# ── P3-T · control: el camino feliz sigue creando los directorios ──────────


def test_p3t_un_arbol_legitimo_sigue_materializandose(tmp_path) -> None:
    """El guard no puede volverse un rechazo general de directorios anidados."""
    raiz_origen, contenedor, destino, _externo = _armar(tmp_path)

    copiados = copiar_arbol_independiente(
        raiz_origen, destino, (), ("Data", "Data/EmptyFolder", "Data/Meshes"), contenedor=contenedor
    )

    assert copiados == 0
    assert (destino / "Data" / "EmptyFolder").is_dir()
    assert (destino / "Data" / "Meshes").is_dir()
