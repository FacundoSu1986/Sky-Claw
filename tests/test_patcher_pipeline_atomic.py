"""§2.1 reporte de consistencia — escritura atómica de la config de patchers.

``PatcherPipeline.to_json`` escribía con ``open(w)`` + ``json.dump`` directo: un
crash a mitad del dump dejaba el JSON truncado y el próximo ``from_json`` (o el
``__init__`` que carga la config) fallaba con un pipeline corrupto. Ahora usa
tmp+``os.replace`` atómico (mismo patrón que ``vfs._write_modlist_atomic``).
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.local.tools.patcher_pipeline import PatcherDefinition, PatcherPipeline


def _pipeline_con_patchers() -> PatcherPipeline:
    p = PatcherPipeline()
    p.add_patcher(PatcherDefinition(patcher_id="LeveledLists", order=0))
    p.add_patcher(PatcherDefinition(patcher_id="Armor", order=1, config={"foo": "bar"}))
    return p


def test_to_json_escribe_config_valida(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "sub" / "pipeline.json"  # el dir padre no existe aún

    _pipeline_con_patchers().to_json(destino)

    assert destino.is_file()
    data = json.loads(destino.read_text(encoding="utf-8"))
    ids = {pat["patcher_id"] for pat in data["patchers"]}
    assert ids == {"LeveledLists", "Armor"}
    # Round-trip: from_json reconstruye el mismo set.
    recargado = PatcherPipeline.from_json(destino)
    assert len(recargado) == 2


def test_to_json_no_deja_tmp_huerfano(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "pipeline.json"

    _pipeline_con_patchers().to_json(destino)

    # El único archivo del dir es el destino final — ningún .tmp residual.
    assert [f.name for f in tmp_path.iterdir()] == ["pipeline.json"]


def test_crash_a_mitad_del_dump_preserva_el_config_previo(tmp_path: pathlib.Path) -> None:
    """La atomicidad real: si el dump revienta, el JSON previo queda intacto y
    NO aparece un archivo truncado ni un tmp huérfano."""
    destino = tmp_path / "pipeline.json"
    # Config previa válida en disco.
    PatcherPipeline().to_json(destino)  # pipeline vacío pero válido
    bytes_previos = destino.read_bytes()

    pipeline = _pipeline_con_patchers()
    # json.dump revienta a mitad de escritura (disco lleno / I/O error).
    with (
        patch("sky_claw.local.tools.patcher_pipeline.json.dump", side_effect=OSError("disco lleno")),
        pytest.raises(OSError, match="disco lleno"),
    ):
        pipeline.to_json(destino)

    # El destino sigue siendo el config previo, byte a byte (nunca se tocó).
    assert destino.read_bytes() == bytes_previos
    # Sin .tmp huérfano en el dir.
    assert sorted(f.name for f in tmp_path.iterdir()) == ["pipeline.json"]


def test_to_json_sobreescribe_atomicamente(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "pipeline.json"
    PatcherPipeline().to_json(destino)  # v1: vacío

    _pipeline_con_patchers().to_json(destino)  # v2: 2 patchers

    assert len(PatcherPipeline.from_json(destino)) == 2
