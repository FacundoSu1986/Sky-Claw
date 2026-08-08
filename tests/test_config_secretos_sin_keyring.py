"""`Config.save()` fail-closed en las DOS direcciones cuando no hay keyring.

Por qué existe: `save()` saca los secretos de ``_data``, los manda al keyring y
**nunca** los escribe en el TOML — decisión deliberada para no dejar plaintext en
disco. Pero el `pop` es incondicional: si el keyring no está disponible (sin
backend, sesión sin desbloquear, servicio caído), el secreto que el usuario tenía
en el archivo se borraba en el mismo `save()`, sin haberse guardado en ningún
lado. Perder la key es tan malo como filtrarla, y no hay backup ni escritura
atómica para recuperarla.

El contrato correcto tiene dos mitades y las dos se prueban acá:

* un secreto que YA estaba en el archivo y no se pudo mover al keyring
  **sobrevive** en el archivo (no se pierde lo que el usuario ya tenía);
* un secreto NUEVO, que sólo existe en memoria, **no** se escribe en plaintext
  (no se degrada la política de secretos por un fallo de keyring).
"""

from __future__ import annotations

import pathlib
import tomllib

import keyring
import keyring.errors
import pytest

from sky_claw.config import Config


@pytest.fixture
def _keyring_caido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyring completamente inaccesible: ni lee ni escribe."""

    def _explota(*_a: object, **_k: object) -> None:
        raise keyring.errors.KeyringError("sin backend disponible")

    monkeypatch.setattr(keyring, "get_password", _explota)
    monkeypatch.setattr(keyring, "set_password", _explota)


def test_secreto_del_archivo_sobrevive_si_el_keyring_no_esta(tmp_path: pathlib.Path, _keyring_caido: None) -> None:
    """Sin keyring, `save()` no puede borrar el único ejemplar del secreto."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "clave-del-usuario"\nmo2_root = "D:/MO2"\n',
        encoding="utf-8",
    )

    cfg = Config(config_path)
    cfg._data["community_shaders_mods"] = ["Community Shaders"]
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["nexus_api_key"] == "clave-del-usuario"
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["mo2_root"] == "D:/MO2"


def test_secreto_nuevo_en_memoria_no_se_escribe_en_plaintext(tmp_path: pathlib.Path, _keyring_caido: None) -> None:
    """La otra mitad: un fallo de keyring no habilita plaintext nuevo en disco."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")

    cfg = Config(config_path)
    cfg._data["nexus_api_key"] = "clave-recien-tipeada"
    cfg.save()

    crudo = config_path.read_text(encoding="utf-8")
    assert "clave-recien-tipeada" not in crudo
    assert "nexus_api_key" not in tomllib.loads(crudo)


def test_con_keyring_disponible_el_secreto_se_scrubea_del_toml(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El camino feliz no cambia: si el keyring acepta el secreto, sale del TOML.

    Y no vuelve en un `save()` posterior: una vez que el keyring lo tiene, el
    archivo deja de ser el único ejemplar y preservarlo sería reintroducir el
    plaintext que la migración acaba de sacar.
    """
    almacen: dict[str, str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))
    monkeypatch.setattr(keyring, "set_password", lambda _s, k, v: almacen.__setitem__(k, v))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "clave-del-usuario"\n',
        encoding="utf-8",
    )

    cfg = Config(config_path)
    cfg.save()
    cfg.save()  # segunda pasada: el plaintext no debe reaparecer

    crudo = config_path.read_text(encoding="utf-8")
    assert "clave-del-usuario" not in crudo
    assert almacen["nexus_api_key"] == "clave-del-usuario"


def test_secreto_escrito_en_disco_por_otra_superficie_sobrevive_si_el_keyring_falla(
    tmp_path: pathlib.Path, _keyring_caido: None
) -> None:
    """Variante F1 (review CodeRabbit): el plaintext NO lo escribió ESTE objeto —
    apareció en el archivo DESPUÉS de su construcción (otra superficie o la mano
    del usuario en el TOML). Con el keyring caído, el ``pop`` incondicional del
    scrub lo sacaba de ``save_data`` y ``_secretos_solo_en_archivo`` no tenía
    entrada para restaurarlo (sólo se puebla al construir): el ÚNICO ejemplar
    del secreto se destruía en el mismo save que decía proteger secretos. La
    fuente de restauración es lo que HAY en el archivo en el momento del save,
    con fallback al registro de la construcción.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")

    cfg = Config(config_path)
    # Otra superficie (o el usuario a mano) escribe el secreto al archivo.
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "clave-escrita-por-otro"\n',
        encoding="utf-8",
    )

    cfg._data["community_shaders_mods"] = ["Community Shaders"]
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["nexus_api_key"] == "clave-escrita-por-otro"
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["llm_provider"] == "anthropic"
