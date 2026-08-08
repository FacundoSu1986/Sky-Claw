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
    fuente de restauración es lo que HAY en el archivo en el momento del save
    (la lectura fresca del merge), no el snapshot de construcción.
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


def test_un_keyring_con_valor_mas_nuevo_no_se_pisa_con_el_plaintext_viejo_del_archivo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 de qodo: el keyring ya tiene un valor MÁS NUEVO que el plaintext
    obsoleto del TOML (en la construcción, ``_load_from_keyring`` resuelve la
    memoria al valor del keyring). Un save no relacionado NO puede tomar el
    plaintext viejo de la lectura fresca y pasarlo a ``set_password`` —eso
    sobrescribía la credencial nueva con el valor obsoleto del archivo antes
    de scrubbear el TOML, rompiendo autenticación y destruyendo la única
    credencial vigente. La fuente del keyring es el valor del OBJETO
    (resuelto contra el keyring al construir); el plaintext del archivo rige
    solo cuando el objeto no conoce el secreto.
    """
    almacen: dict[str, str] = {"nexus_api_key": "credential-nueva-del-keyring"}
    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))
    monkeypatch.setattr(keyring, "set_password", lambda _s, k, v: almacen.__setitem__(k, v))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "credential-vieja-del-archivo"\n',
        encoding="utf-8",
    )

    cfg = Config(config_path)
    # En memoria gana el valor del keyring; el plaintext queda en disco porque
    # los valores difieren (no hay migración que scrubbear en el init).
    assert cfg._data["nexus_api_key"] == "credential-nueva-del-keyring"

    cfg._data["community_shaders_mods"] = ["Community Shaders"]
    cfg.save()

    # El keyring conserva la credencial vigente — NO la vieja del archivo.
    assert almacen["nexus_api_key"] == "credential-nueva-del-keyring"
    crudo = config_path.read_text(encoding="utf-8")
    assert "credential-vieja-del-archivo" not in crudo
    assert "credential-nueva-del-keyring" not in crudo
    assert tomllib.loads(crudo)["community_shaders_mods"] == ["Community Shaders"]


def test_migracion_de_secreto_en_constructor_no_reaplica_el_snapshot_completo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El scrub de construcción preserva campos actualizados tras la lectura inicial."""
    almacen: dict[str, str] = {}
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nmo2_root = "D:/MO2-antiguo"\nnexus_api_key = "clave-a-migrar"\n',
        encoding="utf-8",
    )
    intercalado = False

    def _leer(_servicio: str, clave: str) -> str | None:
        nonlocal intercalado
        if clave == "nexus_api_key" and not intercalado:
            intercalado = True
            config_path.write_text(
                'llm_provider = "anthropic"\nmo2_root = "D:/MO2-actualizado"\nnexus_api_key = "clave-a-migrar"\n',
                encoding="utf-8",
            )
        return almacen.get(clave)

    monkeypatch.setattr(keyring, "get_password", _leer)
    monkeypatch.setattr(keyring, "set_password", lambda _s, k, v: almacen.__setitem__(k, v))

    Config(config_path)

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["mo2_root"] == "D:/MO2-actualizado"
    assert "nexus_api_key" not in en_disco
    assert almacen["nexus_api_key"] == "clave-a-migrar"


def test_hidratacion_del_keyring_no_se_confunde_con_una_escritura_explicita(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrar un secreto B no puede reescribir una versión obsoleta de A."""
    almacen = {
        "llm_api_key": "llm-v1-leida",
        "ws_auth_token": "token-ws-existente",
    }
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "nexus-a-migrar"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))

    def _guardar(_servicio: str, clave: str, valor: str) -> None:
        almacen[clave] = valor
        if clave == "nexus_api_key":
            # Otra superficie rota A después de que este constructor leyó v1.
            almacen["llm_api_key"] = "llm-v2-rotada"

    monkeypatch.setattr(keyring, "set_password", _guardar)

    Config(config_path)

    assert almacen["llm_api_key"] == "llm-v2-rotada"
    assert almacen["nexus_api_key"] == "nexus-a-migrar"
    assert "nexus_api_key" not in tomllib.loads(config_path.read_text(encoding="utf-8"))


def test_un_config_long_lived_no_pisa_un_keyring_actualizado_despues_de_construirse(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin intención explícita, el keyring vivo prevalece sobre la memoria vieja."""
    almacen: dict[str, str] = {"nexus_api_key": "clave-al-construir"}
    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))
    monkeypatch.setattr(keyring, "set_password", lambda _s, k, v: almacen.__setitem__(k, v))
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)

    almacen["nexus_api_key"] = "clave-actualizada"
    cfg._data["community_shaders_mods"] = ["Community Shaders"]
    cfg.save()

    assert almacen["nexus_api_key"] == "clave-actualizada"
    assert "nexus_api_key" not in tomllib.loads(config_path.read_text(encoding="utf-8"))


def test_keyring_vivo_prevalece_sobre_plaintext_fresco_si_no_hubo_escritura_explicita(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El plaintext fresco sólo se migra cuando el keyring todavía está vacío."""
    almacen: dict[str, str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))
    monkeypatch.setattr(keyring, "set_password", lambda _s, k, v: almacen.__setitem__(k, v))
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)

    almacen["nexus_api_key"] = "clave-viva-del-keyring"
    config_path.write_text(
        'llm_provider = "anthropic"\nnexus_api_key = "plaintext-obsoleto"\n',
        encoding="utf-8",
    )
    cfg._data["community_shaders_mods"] = ["Community Shaders"]
    cfg.save()

    assert almacen["nexus_api_key"] == "clave-viva-del-keyring"
    crudo = config_path.read_text(encoding="utf-8")
    assert "plaintext-obsoleto" not in crudo
    assert "clave-viva-del-keyring" not in crudo


def test_secreto_nuevo_fallido_queda_pendiente_y_se_reintenta(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un fallo conserva la intención para el próximo save sin bajar plaintext."""
    almacen: dict[str, str] = {}
    keyring_caido = True

    monkeypatch.setattr(keyring, "get_password", lambda _s, k: almacen.get(k))

    def _guardar(_servicio: str, clave: str, valor: str) -> None:
        if keyring_caido:
            raise keyring.errors.KeyringError("fallo transitorio")
        almacen[clave] = valor

    monkeypatch.setattr(keyring, "set_password", _guardar)
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)
    cfg._data["nexus_api_key"] = "clave-nueva"

    cfg.save()
    assert "clave-nueva" not in config_path.read_text(encoding="utf-8")

    keyring_caido = False
    cfg.save()

    assert almacen["nexus_api_key"] == "clave-nueva"
    assert "nexus_api_key" not in tomllib.loads(config_path.read_text(encoding="utf-8"))
