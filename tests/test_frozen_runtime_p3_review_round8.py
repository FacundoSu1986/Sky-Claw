"""P3 ronda 8 — P3-AA: la contención del destino no se ejecutaba para el NIVEL RAIZ.

Finding de Codex sobre el HEAD final de la ronda 7 (`9a1eff1d`), revalidado con
probe propio antes de clasificarlo.

`_materializar_directorio_del_destino` revalidaba la contención **dentro** del loop
que recorre los componentes del padre:

```python
actual = pathlib.Path(raiz_destino)
for componente in rel_dir.parts:
    _exigir_contencion_destino(contenedor, actual)
    ...
```

Para un archivo anidado (`Data/a.bin`) el padre es `Data` y el loop itera: el
guard corre. Para un archivo en la **raíz del payload** (`SkyrimSE.exe`,
`second.bin`) el padre es `"."` y `PurePosixPath(".").parts == ()`, así que el
loop hace **CERO iteraciones** y `_exigir_contencion_destino` **nunca se llama**.

No es una "ventana residual entre la verificación y la mutación" (eso lo cierra
el lock cross-process, que es P4): es **verificación ausente en un nivel**. La
propiedad que P3-T/P3-P afirman es que la contención se revalida ANTES DE CADA
mutación; acá no se revalidaba para ninguna mutación en la raíz.

Evidencia medida contra `9a1eff1d` con el MISMO ataque en los dos niveles:

| Relpath | Resultado |
|---|---|
| `Data/a.bin` (anidado) | `CandidateCopyError`, sin escape — guard OK |
| `SkyrimSE.exe` (raíz) | copia **OK** + bytes escritos FUERA del `FrozenRuntimeRoot` |

RED-first: estos tests se escribieron y se vieron FALLAR contra `9a1eff1d`
(`P3_AA_PRE_FIX_RED`).
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime import copying as copying_module
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError
from sky_claw.local.runtime_vault.models import FileIdentity
from tests._symlink_guard import crear_junction, junction_guard


def _armar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    """Origen con DOS archivos en la raíz + contenedor + destino externo."""
    raiz_origen = tmp_path / "source"
    raiz_origen.mkdir()
    (raiz_origen / "SkyrimSE.exe").write_bytes(b"EXE-REAL")
    (raiz_origen / "second.bin").write_bytes(b"SEGUNDO-REAL")

    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    destino = contenedor / "candidates" / "cand_x" / "payload"

    externo = tmp_path / "externo"
    externo.mkdir()
    return raiz_origen, contenedor, destino, externo


def _archivos_raiz() -> tuple[FileIdentity, ...]:
    """Los dos archivos viven en la RAIZ del payload: su padre es ``.``."""
    return (
        FileIdentity(rel_path="SkyrimSE.exe", size=8, digest="a" * 64),
        FileIdentity(rel_path="second.bin", size=12, digest="b" * 64),
    )


# ── P3-AA · el nivel raiz del payload no puede quedar sin revalidar ────────


@junction_guard
def test_p3aa_el_archivo_en_la_raiz_del_payload_no_escribe_fuera(tmp_path, monkeypatch) -> None:
    """RED: el payload reemplazado por junction no puede desviar un archivo de raíz.

    El swap ocurre DESPUES de copiar el primer archivo y ANTES de materializar el
    padre del segundo -- la misma ventana determinista que usa el test de P3-P,
    pero con los dos archivos en la RAIZ, que es el nivel donde el guard no corría.

    Pre-fix el segundo archivo se escribia en el destino externo y la copia
    terminaba SIN error; post-fix la revalidación de la raíz del payload lo corta.
    """
    raiz_origen, contenedor, destino, externo = _armar(tmp_path)

    original = copying_module.copiar_archivo
    estado = {"cambiado": False}

    def copiar_con_swap(origen, dest):  # noqa: ANN001, ANN202
        resultado = original(origen, dest)
        if not estado["cambiado"]:
            estado["cambiado"] = True
            # El payload era legítimo; ahora lo reemplaza un junction al externo.
            shutil.rmtree(destino)
            if (motivo := crear_junction(destino, externo)) is not None:
                pytest.skip(f"no se pudo crear junction: {motivo}")
        return resultado

    monkeypatch.setattr(copying_module, "copiar_archivo", copiar_con_swap)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, _archivos_raiz(), (), contenedor=contenedor)

    assert estado["cambiado"] is True, "el test no alcanzo la ventana que dice cubrir"
    assert list(externo.iterdir()) == [], "se escribio contenido FUERA del FrozenRuntimeRoot"


@junction_guard
def test_p3aa_la_materializacion_sin_componentes_igual_revalida(tmp_path) -> None:
    """RED (invariante estructural): cero componentes NO puede significar cero guards.

    Se llama a la primitive de materializacion directamente con `"."` como rel_dir
    -- que es exactamente lo que recibe para un archivo en la raíz del payload --
    sobre un destino que es un junction. La revalidación tiene que ocurrir aunque
    el loop no tenga nada que recorrer.

    Este es el ancla que impide la regresión de forma más general que el caso
    concreto: cualquier caller futuro que materialice la raíz (``parts`` vacío)
    queda cubierto por el mismo guard.
    """
    _raiz_origen, contenedor, destino, externo = _armar(tmp_path)
    destino.mkdir(parents=True)
    shutil.rmtree(destino)
    if (motivo := crear_junction(destino, externo)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    with pytest.raises(CandidateCopyError):
        copying_module._materializar_directorio_del_destino(
            contenedor, destino, pathlib.PurePosixPath(".")
        )

    assert list(externo.iterdir()) == [], "la materializacion muto el destino externo"


# ── Controles: el endurecimiento no rompe la copia legítima ───────────────


def test_p3aa_un_archivo_legitimo_en_la_raiz_sigue_copiandose(tmp_path) -> None:
    """Control: el rechazo no puede llevarse puestos los archivos de raíz sanos.

    ``SkyrimSE.exe`` vive en la raíz de toda Managed Source real: si este test
    falla, el fix volvió inutilizable el caso más común de la copia.
    """
    raiz_origen, contenedor, destino, _externo = _armar(tmp_path)

    copiados = copiar_arbol_independiente(raiz_origen, destino, _archivos_raiz(), (), contenedor=contenedor)

    assert copiados == 2
    assert (destino / "SkyrimSE.exe").read_bytes() == b"EXE-REAL"
    assert (destino / "second.bin").read_bytes() == b"SEGUNDO-REAL"


@junction_guard
def test_p3aa_el_anidado_sigue_detectandose(tmp_path, monkeypatch) -> None:
    """Control de NO-REGRESIÓN de P3-P/P3-T: el nivel anidado sigue cortando.

    El hermano que ya funcionaba no puede debilitarse al agregar el guard de la
    raíz. Misma ventana, archivos anidados.
    """
    raiz_origen = tmp_path / "source"
    (raiz_origen / "Data").mkdir(parents=True)
    (raiz_origen / "Data" / "a.bin").write_bytes(b"AAA")
    (raiz_origen / "Data" / "b.bin").write_bytes(b"BBB")

    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    destino = contenedor / "candidates" / "cand_x" / "payload"
    externo = tmp_path / "externo"
    externo.mkdir()

    archivos = (
        FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),
        FileIdentity(rel_path="Data/b.bin", size=3, digest="b" * 64),
    )

    original = copying_module.copiar_archivo
    estado = {"cambiado": False}

    def copiar_con_swap(origen, dest):  # noqa: ANN001, ANN202
        resultado = original(origen, dest)
        if not estado["cambiado"]:
            estado["cambiado"] = True
            shutil.rmtree(pathlib.Path(dest).parent)
            if (motivo := crear_junction(pathlib.Path(dest).parent, externo)) is not None:
                pytest.skip(f"no se pudo crear junction: {motivo}")
        return resultado

    monkeypatch.setattr(copying_module, "copiar_archivo", copiar_con_swap)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, archivos, ("Data",), contenedor=contenedor)

    assert list(externo.iterdir()) == [], "se escribio contenido FUERA del FrozenRuntimeRoot"
