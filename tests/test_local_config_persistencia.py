"""Persistencia de config compartida por las dos superficies (GUI y agente LLM).

Por qué existe: el autoinstalador de Community Shaders persiste los nombres de
los mods para que el orquestador los active en `modlist.txt` (contrato NGIO), y
cada superficie lo hacía a su manera:

* la GUI abría el path con `local_config.load` (JSON) — pero
  `AppContext.config_path` es el TOML canónico (`~/.sky_claw/config.toml`), así
  que el parseo fallaba, `load` devolvía defaults con TODO en `None`, y el
  `save` posterior reescribía el archivo entero: la config del usuario
  (mo2_root, skyrim_path, provider, modelos, chat id) desaparecía sin error;
* el agente escribía en `Config._data` y llamaba `local_config.save`, que hace
  `asdict()` y sólo acepta dataclases: con el `Config` real que inyecta
  `AppContext` levantaba `TypeError` y el campo nunca llegaba al disco.

Los dos formatos reescriben el archivo completo sin backup ni escritura
atómica, así que "leer mal" y "escribir" es destrucción de datos silenciosa.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): el round-trip se ejerce sobre **las dos clases reales** de config
que existen en el árbol, y un ancla por AST congela quién puede importar los
`load`/`save` crudos del formato JSON legacy.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import pathlib
import threading
import tomllib

import pytest

from sky_claw.config import Config, _DatosConfig
from sky_claw.local.local_config import (
    LocalConfig,
    abrir_config_para_persistir,
    escribir_campo,
    guardar_config,
    guardar_config_bloqueante,
    persistir_campo,
    persistir_campo_bloqueante,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

_TOML_DE_USUARIO = """\
llm_provider = "anthropic"
anthropic_model = "claude-opus-5"
mo2_root = "D:/Modding/MO2"
skyrim_path = "D:/Steam/steamapps/common/Skyrim Special Edition"
install_dir = "D:/Modding/Tools"
loot_exe = "D:/Modding/Tools/LOOT/LOOT.exe"
"""


# ── Round-trip por formato ──────────────────────────────────────────────────────
def test_persistir_campo_sobre_toml_conserva_la_config_del_usuario(tmp_path: pathlib.Path) -> None:
    """El path canónico es TOML: persistir un campo NO puede volverlo JSON de defaults."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_TOML_DE_USUARIO, encoding="utf-8")

    persistir_campo(config_path, "community_shaders_mods", ["Address Library", "Community Shaders"])

    datos = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert datos["community_shaders_mods"] == ["Address Library", "Community Shaders"]
    # Lo que había antes sigue ahí: éste es el punto del test, no el campo nuevo.
    assert datos["mo2_root"] == "D:/Modding/MO2"
    assert datos["skyrim_path"] == "D:/Steam/steamapps/common/Skyrim Special Edition"
    assert datos["install_dir"] == "D:/Modding/Tools"
    assert datos["loot_exe"] == "D:/Modding/Tools/LOOT/LOOT.exe"
    assert datos["llm_provider"] == "anthropic"
    assert datos["anthropic_model"] == "claude-opus-5"


def test_persistir_campo_sobre_json_legacy_conserva_la_config(tmp_path: pathlib.Path) -> None:
    """El JSON legacy (`sky_claw_config.json`) sigue soportado por su extensión."""
    config_path = tmp_path / "sky_claw_config.json"
    config_path.write_text(
        json.dumps({"mo2_root": "D:/Modding/MO2", "skyrim_path": "D:/Skyrim"}),
        encoding="utf-8",
    )

    persistir_campo(config_path, "ngio_mods", ["NGIO-NG"])

    datos = json.loads(config_path.read_text(encoding="utf-8"))
    assert datos["ngio_mods"] == ["NGIO-NG"]
    assert datos["mo2_root"] == "D:/Modding/MO2"
    assert datos["skyrim_path"] == "D:/Skyrim"


def test_persistir_campo_crea_el_archivo_si_no_existe(tmp_path: pathlib.Path) -> None:
    """Primer arranque: sin archivo previo no hay nada que perder, se crea."""
    config_path = tmp_path / "config.toml"

    persistir_campo(config_path, "community_shaders_mods", ["Community Shaders"])

    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["Community Shaders"]


# ── Fail-closed: no se escribe sobre lo que no se pudo leer ─────────────────────
@pytest.mark.parametrize(
    ("nombre", "contenido"),
    [
        ("config.toml", "esto no es TOML = = ["),
        ("sky_claw_config.json", "{esto no es JSON"),
    ],
)
def test_abrir_config_para_persistir_falla_cerrado_si_no_parsea(
    tmp_path: pathlib.Path, nombre: str, contenido: str
) -> None:
    """Un archivo existente que no parsea aborta ANTES de devolver un objeto.

    Sin esto, el objeto con defaults viaja hasta el `save` y reescribe el
    archivo entero: exactamente la destrucción que este módulo evita. Que el
    archivo quede intacto es la mitad importante de la aserción.
    """
    config_path = tmp_path / nombre
    config_path.write_text(contenido, encoding="utf-8")

    with pytest.raises((tomllib.TOMLDecodeError, json.JSONDecodeError)):
        abrir_config_para_persistir(config_path)

    assert config_path.read_text(encoding="utf-8") == contenido


def test_persistir_campo_no_toca_el_archivo_ilegible(tmp_path: pathlib.Path) -> None:
    """El ciclo completo hereda el fail-closed del lector."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[[[roto", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        persistir_campo(config_path, "community_shaders_mods", ["X"])

    assert config_path.read_text(encoding="utf-8") == "[[[roto"


# ── Archivo vacío: "sin datos todavía" no es lo mismo que "ilegible" ────────────
def test_persistir_campo_sobre_toml_de_cero_bytes_no_es_error(tmp_path: pathlib.Path) -> None:
    """Un documento TOML vacío es válido (`tomllib.loads("") == {}`) — no hay
    fail-closed que disparar. Un `config.toml` de 0 bytes (arranque interrumpido
    ANTES de esta escritura atómica, o un `touch` manual) es "sin datos todavía",
    no "corrupto", y `Config()` ya lo trata como defaults.
    """
    config_path = tmp_path / "config.toml"
    config_path.touch()

    persistir_campo(config_path, "mo2_root", "D:/MO2")

    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["mo2_root"] == "D:/MO2"


def test_persistir_campo_sobre_json_de_cero_bytes_no_es_error(tmp_path: pathlib.Path) -> None:
    """La asimetría real: a diferencia de TOML, ``json.loads("")`` SÍ lanza.

    El `load()` legacy tolera esto (atrapa `JSONDecodeError` y cae a defaults);
    `abrir_config_para_persistir` debe preservar esa tolerancia para el archivo
    vacío específicamente — es distinto de "el archivo tiene basura adentro",
    que sigue siendo fail-closed (ver `test_abrir_config_para_persistir_falla_cerrado_si_no_parsea`).
    """
    config_path = tmp_path / "sky_claw_config.json"
    config_path.touch()

    persistir_campo(config_path, "ngio_mods", ["NGIO-NG"])

    assert json.loads(config_path.read_text(encoding="utf-8"))["ngio_mods"] == ["NGIO-NG"]


# ── Escritura atómica: un fallo a mitad de camino no puede truncar el archivo ───
def test_guardar_config_no_trunca_el_archivo_si_la_escritura_falla(tmp_path: pathlib.Path, monkeypatch) -> None:
    """`Config.save()`/`local_config.save()` escribían con ``open(path, "wb")``,
    que trunca ANTES de escribir el contenido nuevo: un fallo a mitad de la
    serialización (disco lleno, `OSError` transitorio en Windows, el proceso
    matado) dejaba el archivo en 0 bytes — la config anterior, completa y
    válida, desaparecía por un fallo que no tenía nada que ver con ella. La
    escritura debe pasar por un temporal + `os.replace`, que en POSIX y en NTFS
    es atómico: o el archivo viejo entero, o el nuevo entero, nunca una mezcla.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nmo2_root = "D:/MO2"\n', encoding="utf-8")
    cfg = Config(config_path)
    cfg._data["mo2_root"] = "D:/Otro"

    import tomli_w

    def _dump_que_explota(*_a: object, **_k: object) -> None:
        raise OSError("disco lleno (simulado)")

    monkeypatch.setattr(tomli_w, "dump", _dump_que_explota)

    with pytest.raises(OSError):
        cfg.save()

    # El archivo original sigue COMPLETO — no truncado, no a medio escribir.
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["mo2_root"] == "D:/MO2"


def test_guardar_config_no_deja_temporales_huerfanos_tras_un_save_exitoso(tmp_path: pathlib.Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)
    cfg._data["mo2_root"] = "D:/MO2"

    cfg.save()

    residuos = [p for p in tmp_path.iterdir() if p != config_path]
    assert residuos == []


# ── Carrera GUI ↔ agente LLM: los dos escritores del mismo config.toml ──────────
#
# El lock (`_obtener_lock_de_escritura`) da UNA garantía: el ciclo
# leer-modificar-escribir de cada escritor corre de punta a punta sin que el
# otro empiece en el medio. Eso alcanza para que el archivo NUNCA quede
# corrupto ni con una escritura a medio hacer.
#
# La garantía de CONTENIDO (que el escritor con objeto desactualizado no pise
# lo que el otro camino acaba de guardar) NO la da el lock: la da
# `Config.save()` desde F1, que hace merge-on-save con diff contra un baseline
# tomado al construir el objeto (ver `sky_claw/config.py`): cada `save()` relee
# el disco bajo el threading.Lock de save, aplica SOLO los campos que ESTE
# objeto cambió, y recién después scrubea secretos y escribe atómicamente. Un
# `Config` long-lived ya no puede borrar campos que nunca vio — la frontera que
# antes documentaba `test_limite_conocido_...` ahora es garantía, y los tests
# de abajo la anclan sobre campos que SÍ vienen de `_load_defaults()` (un test
# sobre campos sin default pasaría incluso con la unión ingenua).
async def test_lock_evita_la_escritura_entrelazada_y_corrupta(tmp_path: pathlib.Path) -> None:
    """Garantía real del lock: el archivo nunca queda a medio escribir.

    Sin el lock, dos ``to_thread`` concurrentes sobre el mismo ``config.toml``
    pueden intercalar sus escrituras (dos ``os.replace`` compitiendo) — acá se
    ordena el arranque con un ``sleep(0)`` antes de que el agente tome el lock,
    y se
    verifica que el resultado final SIEMPRE es TOML válido con exactamente una
    de las dos escrituras completas.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nmo2_root = "D:/MO2"\n', encoding="utf-8")
    cfg_del_agente = Config(config_path)
    escribir_campo(cfg_del_agente, "community_shaders_mods", ["Community Shaders"])

    orden: list[str] = []

    async def _gui() -> None:
        orden.append("gui-start")
        await persistir_campo_bloqueante(config_path, "ngio_mods", ["NGIO-NG"])
        orden.append("gui-end")

    async def _agente() -> None:
        await asyncio.sleep(0)  # cede el loop: deja que la GUI tome el lock primero
        orden.append("agente-start")
        await guardar_config_bloqueante(cfg_del_agente, config_path)
        orden.append("agente-end")

    await asyncio.gather(_gui(), _agente())

    # Serializado de punta a punta: nunca "gui-start, agente-start, agente-end, gui-end"
    # intercalado a nivel de ESCRITURA — cada `-end` sigue inmediatamente a su `-start`
    # del mismo lado, o al `-end` del otro.
    assert orden == ["gui-start", "agente-start", "gui-end", "agente-end"]
    # Nunca corrupto: siempre parsea, pase lo que pase con los campos.
    tomllib.loads(config_path.read_text(encoding="utf-8"))


async def test_el_escritor_con_lectura_fresca_no_pierde_lo_que_el_otro_ya_guardo(
    tmp_path: pathlib.Path,
) -> None:
    """El caso que el lock SÍ resuelve del todo: cuando el segundo escritor relee
    antes de escribir (``persistir_campo``, la GUI), no importa el orden — ve lo
    que el primero ya guardó y lo conserva.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nmo2_root = "D:/MO2"\n', encoding="utf-8")
    cfg_del_agente = Config(config_path)
    escribir_campo(cfg_del_agente, "community_shaders_mods", ["Community Shaders"])

    async def _agente_primero() -> None:
        await guardar_config_bloqueante(cfg_del_agente, config_path)

    async def _gui_segunda_con_lectura_fresca() -> None:
        await asyncio.sleep(0)
        await persistir_campo_bloqueante(config_path, "ngio_mods", ["NGIO-NG"])

    await asyncio.gather(_agente_primero(), _gui_segunda_con_lectura_fresca())

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["ngio_mods"] == ["NGIO-NG"]
    assert en_disco["mo2_root"] == "D:/MO2"


async def test_el_merge_on_save_conserva_lo_que_el_otro_camino_guardo(
    tmp_path: pathlib.Path,
) -> None:
    """F1 cerró el límite que este test documentaba al revés: si el objeto
    `Config` de larga vida del agente ya existía ANTES de que la GUI guardara
    `ngio_mods`, el `save()` del agente —posterior— releía el disco bajo el
    lock de save y aplicaba SOLO lo que ESTE objeto cambió (diff contra el
    baseline tomado al construirlo). El campo que la GUI escribió sobrevive.

    El test se conserva (invertido) en vez de borrarse: es la historia del
    límite y la ancla de que no regrese. Ver el comentario de la sección.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nmo2_root = "D:/MO2"\n', encoding="utf-8")
    # El objeto del agente se construye ANTES de que exista `ngio_mods` en disco.
    cfg_del_agente = Config(config_path)
    escribir_campo(cfg_del_agente, "community_shaders_mods", ["Community Shaders"])

    async def _gui_primera() -> None:
        await persistir_campo_bloqueante(config_path, "ngio_mods", ["NGIO-NG"])

    async def _agente_segundo_con_objeto_desactualizado() -> None:
        await asyncio.sleep(0)
        await guardar_config_bloqueante(cfg_del_agente, config_path)

    await asyncio.gather(_gui_primera(), _agente_segundo_con_objeto_desactualizado())

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    # El archivo es TOML válido y completo, y el campo que la GUI escribió
    # SOBREVIVE al save del objeto desactualizado — eso es F1.
    assert en_disco["ngio_mods"] == ["NGIO-NG"]
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["mo2_root"] == "D:/MO2"


@pytest.mark.parametrize(
    ("campo", "valor_fresco"),
    [
        ("mo2_root", "D:/Otro/MO2"),
        ("telegram_chat_id", "-1001234567890"),
        ("llm_provider", "openai"),
        ("anthropic_model", "claude-sonnet-5"),
        ("install_dir", "D:/Otro/Tools"),
        ("first_run", False),
    ],
)
async def test_un_config_vivo_no_pisa_campos_frescos_que_vienen_de_defaults(
    tmp_path: pathlib.Path, campo: str, valor_fresco: object
) -> None:
    """Cierre de CLASE de F1, no de la instancia.

    El objeto del agente se construye cuando el campo todavía NO está en disco:
    su `_data` queda con el DEFAULT (o el valor de arranque) del campo. La GUI
    lo escribe después con lectura fresca, y el agente salva otro campo con el
    objeto desactualizado. El campo fresco tiene que sobrevivir.

    Se parametriza sobre campos que SÍ están en `_load_defaults()`: un test
    sobre `community_shaders_mods`/`ngio_mods` (sin default) pasaría incluso
    con la unión ingenua de "releer disco + aplicar `_data` encima", porque
    esos campos no tienen entrada default que los pise — es exactamente la
    trampa que señaló la review de diseño.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\nmo2_root = "D:/MO2"\nxedit_exe = "D:/Tools/xEdit/SSEEdit.exe"\n',
        encoding="utf-8",
    )
    cfg_del_agente = Config(config_path)

    await persistir_campo_bloqueante(config_path, campo, valor_fresco)

    # El agente toca OTRO campo y salva con el objeto que no vio el fresco.
    escribir_campo(cfg_del_agente, "loot_exe", "D:/Tools/LOOT/LOOT.exe")
    cfg_del_agente.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco[campo] == valor_fresco, "el objeto desactualizado pisó con su default un campo fresco"
    assert en_disco["loot_exe"] == "D:/Tools/LOOT/LOOT.exe"
    # Invariante para TODOS los casos: un campo que nadie tocó permanece.
    assert en_disco["xedit_exe"] == "D:/Tools/xEdit/SSEEdit.exe"


def test_save_con_archivo_corrupto_aborta_sin_escribir(tmp_path: pathlib.Path) -> None:
    """Contrato fail-closed declarado del merge-on-save (review CodeRabbit): si
    el archivo se corrompió DESPUÉS de la construcción del objeto, ``save()``
    aborta con el error del parser y NO escribe — no se mergea sobre lo que no
    se puede leer. El archivo queda exactamente como estaba."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)
    escribir_campo(cfg, "loot_exe", "D:/Tools/LOOT/LOOT.exe")
    config_path.write_text("esto no es TOML = = [", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        cfg.save()

    assert config_path.read_text(encoding="utf-8") == "esto no es TOML = = ["


def test_intercalacion_gui_lee_agente_escribe_gui_reemplaza_no_pierde_nada(
    tmp_path: pathlib.Path,
) -> None:
    """El interleaving que pidió verificar la review de CodeRabbit: la GUI LEE
    el archivo (construye su propio ``Config``), el agente persiste su diff
    DIRECTO —``cfg.save()``, sin pasar por el lock asyncio de los wrappers— y
    recién entonces la GUI reemplaza. Nada se pierde: la escritura de la GUI
    TAMBIÉN es un merge-on-save con lectura fresca, y todas las mutaciones del
    archivo (las de ``persistir_campo`` y las de cualquier ``.save()`` directo)
    corren bajo el MISMO ``threading.Lock`` por path dentro de
    ``Config.save()``: el coordinador del archivo vive en el mecanismo, no en
    ninguno de los dos locks por separado.

    El orden se fuerza de forma explícita (lectura GUI → save del agente →
    reemplazo GUI) en vez de dormir threads: la propiedad debe valer para
    cualquier orden, así que se ancla el peor orden conocido.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")

    cfg_agente = Config(config_path)
    escribir_campo(cfg_agente, "community_shaders_mods", ["Community Shaders"])

    # t1: la GUI lee el archivo (construye su propio Config, lectura fresca).
    cfg_gui = abrir_config_para_persistir(config_path)

    # t2: el agente persiste directo, sin el lock asyncio de los wrappers.
    cfg_agente.save()

    # t3: la GUI escribe su campo y persiste con el objeto construido en t1.
    escribir_campo(cfg_gui, "ngio_mods", ["NGIO-NG"])
    guardar_config(cfg_gui, config_path)

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["ngio_mods"] == ["NGIO-NG"]
    assert en_disco["llm_provider"] == "anthropic"


def test_mutacion_anidada_sin_reasignacion_no_pisa_un_valor_fresco(
    tmp_path: pathlib.Path,
) -> None:
    """La intención persistible exige pasar por un mutador de ``_DatosConfig``.

    Un valor mutable devuelto por ``Config`` no puede registrar por sí solo qué
    parte cambió ni reconciliarla con una versión fresca escrita por otro
    objeto. Tratar su diferencia contra el baseline como una escritura completa
    perdería los elementos que este objeto nunca vio.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('community_shaders_mods = ["A"]\n', encoding="utf-8")
    cfg_desactualizado = Config(config_path)

    cfg_fresco = Config(config_path)
    escribir_campo(cfg_fresco, "community_shaders_mods", ["A", "B"])
    cfg_fresco.save()

    cfg_desactualizado.community_shaders_mods.append("C")
    escribir_campo(cfg_desactualizado, "loot_exe", "D:/Tools/LOOT/LOOT.exe")
    cfg_desactualizado.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["community_shaders_mods"] == ["A", "B"]
    assert en_disco["loot_exe"] == "D:/Tools/LOOT/LOOT.exe"


def test_save_refresca_la_redaccion_desde_el_estado_fusionado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El contexto de logs debe reflejar el chat id que realmente quedó en disco."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'llm_provider = "anthropic"\ntelegram_chat_id = "chat-antiguo"\n',
        encoding="utf-8",
    )
    cfg = Config(config_path)
    config_path.write_text(
        'llm_provider = "anthropic"\ntelegram_chat_id = "chat-actualizado"\n',
        encoding="utf-8",
    )
    contextos: list[dict[str, str]] = []

    from sky_claw import logging_config

    monkeypatch.setattr(logging_config, "update_logging_redaction_context", lambda **kw: contextos.append(kw))

    cfg._data["loot_exe"] = "D:/LOOT"
    cfg.save()

    assert contextos[-1]["telegram_chat_id"] == "chat-actualizado"


async def test_una_asignacion_explicita_del_valor_del_baseline_se_persiste_igual(
    tmp_path: pathlib.Path,
) -> None:
    """Reasignar EXPLÍCITAMENTE el valor que ya estaba en el baseline no es
    "no tocado": es una declaración de intención (``setup_tools`` que
    re-descubre el mismo exe path que el objeto long-lived ya tenía). La
    comparación de valores sola no distingue ambas cosas, omitía la escritura
    y el merge conservaba lo que otra superficie escribió encima — el valor
    re-descubierto se perdía en silencio (review de qodo, P2). El tracking de
    claves dirty cierra la detección.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nloot_exe = "D:/LOOT"\n', encoding="utf-8")
    cfg = Config(config_path)  # baseline: loot_exe = "D:/LOOT"

    # Otra superficie cambia el campo en el archivo.
    await persistir_campo_bloqueante(config_path, "loot_exe", "D:/Otro")

    # El agente reasigna explícitamente el valor original (re-descubrimiento).
    escribir_campo(cfg, "loot_exe", "D:/LOOT")
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["loot_exe"] == "D:/LOOT"
    assert en_disco["llm_provider"] == "anthropic"


def test_reasignacion_explicita_repetida_tras_un_save_se_persiste(tmp_path: pathlib.Path) -> None:
    """Cada asignación expresa una intención nueva, incluso tras actualizar el baseline."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\nloot_exe = "D:/LOOT"\n', encoding="utf-8")
    cfg = Config(config_path)

    cfg._data["loot_exe"] = "D:/LOOT"
    cfg.save()

    persistir_campo(config_path, "loot_exe", "D:/Otro")
    cfg._data["loot_exe"] = "D:/LOOT"
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["loot_exe"] == "D:/LOOT"


def test_borrado_explicito_usa_tombstone_aunque_la_clave_no_estuviera_en_el_baseline(
    tmp_path: pathlib.Path,
) -> None:
    """Agregar y borrar localmente una clave debe vencer una adición externa anterior."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)

    persistir_campo(config_path, "campo_nuevo", "valor-externo")
    cfg._data["campo_nuevo"] = "valor-temporal"
    del cfg._data["campo_nuevo"]
    cfg.save()

    assert "campo_nuevo" not in tomllib.loads(config_path.read_text(encoding="utf-8"))


def test_datos_config_enumera_mutadores_y_preserva_generaciones_en_copias() -> None:
    """Todo mutador público del dict participa del contrato de generaciones."""
    datos = _DatosConfig({"para_del": 1, "para_pop": 2})
    generaciones: list[int] = []

    datos["setitem"] = 1
    generaciones.append(datos.generaciones["setitem"])
    datos.update({"update": 2})
    generaciones.append(datos.generaciones["update"])
    datos.setdefault("setdefault", 3)
    generaciones.append(datos.generaciones["setdefault"])
    datos |= {"ior": 4}
    generaciones.append(datos.generaciones["ior"])
    del datos["para_del"]
    generaciones.append(datos.generaciones["para_del"])
    datos.pop("para_pop")
    generaciones.append(datos.generaciones["para_pop"])
    datos["para_popitem"] = 5
    datos.popitem()
    generaciones.append(datos.generaciones["para_popitem"])

    snapshot = datos.snapshot()
    copia = datos.copy()
    copia_profunda = copy.deepcopy(datos)
    datos["posterior"] = 6

    assert generaciones == sorted(generaciones)
    assert len(generaciones) == len(set(generaciones))
    assert snapshot.generaciones == copia.generaciones == copia_profunda.generaciones
    assert "posterior" not in snapshot

    generaciones_antes_de_clear = {clave: datos.generaciones[clave] for clave in datos}
    datos.clear()
    assert not datos
    assert all(
        datos.generaciones[clave] > generacion_anterior
        for clave, generacion_anterior in generaciones_antes_de_clear.items()
    )


def test_snapshot_tolera_una_mutacion_concurrente_de_data(tmp_path: pathlib.Path) -> None:
    """El snapshot debe ser coherente aunque otro thread escriba durante la copia."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)
    copia_iniciada = threading.Event()
    mutacion_terminada = threading.Event()

    class _ValorQueAbreLaCarrera:
        def __init__(self) -> None:
            self._primera_copia = True

        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            if self._primera_copia:
                self._primera_copia = False
                copia_iniciada.set()
                assert not mutacion_terminada.wait(timeout=0.05)
            return "copiado"

    cfg._data["campo_para_snapshot"] = _ValorQueAbreLaCarrera()

    def _mutar_durante_la_copia() -> None:
        assert copia_iniciada.wait(timeout=1)
        cfg._data["campo_concurrente"] = "valor-concurrente"
        mutacion_terminada.set()

    escritor = threading.Thread(target=_mutar_durante_la_copia)
    escritor.start()
    cfg.save()
    escritor.join(timeout=1)
    assert not escritor.is_alive()

    # La escritura concurrente puede quedar fuera de este snapshot, pero debe
    # conservar su generación pendiente para el save siguiente.
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["campo_para_snapshot"] == "copiado"
    assert en_disco["campo_concurrente"] == "valor-concurrente"


def test_mutacion_concurrente_durante_el_save_no_se_da_por_persistida(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La ventana exacta del P1 de qodo: mientras ``save()`` corre (en un
    thread del pool vía ``guardar_config_bloqueante``), otra invocación
    concurrente puede mutar el ``local_cfg`` compartido. Si el baseline se
    deep-copiara del ``_data`` VIVO después de calcular el diff, esa mutación
    entraría al baseline pero no al diff guardado → los saves siguientes la
    tratan como ya persistida y el valor se pierde en silencio. El snapshot de
    ``_data`` va ANTES del diff y ese mismo snapshot es el nuevo baseline.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    cfg = Config(config_path)

    import tomli_w

    dump_real = tomli_w.dump

    def _dump_que_simula_un_escritor_concurrente(save_data: object, f: object) -> None:
        # Otra superficie muta el objeto compartido mientras el save está
        # entre el diff y el refresh del baseline.
        cfg._data["loot_exe"] = "D:/Tools/LOOT-concurrente.exe"
        return dump_real(save_data, f)  # type: ignore[arg-type]

    monkeypatch.setattr(tomli_w, "dump", _dump_que_simula_un_escritor_concurrente)

    escribir_campo(cfg, "community_shaders_mods", ["Community Shaders"])
    cfg.save()

    # La mutación concurrente no pudo formar parte del diff de ESE save — pero
    # no puede quedar tratada como ya persistida: el próximo save la persiste.
    cfg.save()

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["loot_exe"] == "D:/Tools/LOOT-concurrente.exe"
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]


# ── Ancla: las DOS clases reales de config, no una muestra ──────────────────────
def test_escribir_y_guardar_soportan_las_dos_clases_reales(tmp_path: pathlib.Path) -> None:
    """`Config` (TOML, `_data`) y `LocalConfig` (JSON, dataclass) son las dos
    formas que circulan por el árbol: `AppContext` inyecta la primera en el
    registry del agente, y la segunda es el formato legacy que todavía migra
    `AppContext._migrate_legacy_json`. Un tercer tipo de config rompe acá antes
    de romper en producción.
    """
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(_TOML_DE_USUARIO, encoding="utf-8")
    json_path = tmp_path / "sky_claw_config.json"
    json_path.write_text("{}", encoding="utf-8")

    configs = {
        toml_path: Config(toml_path),
        json_path: LocalConfig(),
    }
    for path, config in configs.items():
        escribir_campo(config, "community_shaders_mods", ["A", "B"])
        guardar_config(config, path)

    assert tomllib.loads(toml_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["A", "B"]
    assert json.loads(json_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["A", "B"]


def test_la_autodeteccion_zero_config_llega_al_disco(tmp_path: pathlib.Path) -> None:
    """Reproduce la secuencia de `AppContext._start_full_inner` sobre un `Config`.

    Ahí se escriben `mo2_root`/`skyrim_path`/`loot_exe`/`xedit_exe` detectados y
    se llama `local_cfg.save()` bajo `if config_changed`. Con `setattr` crudo el
    `save` volcaba `_data` —que nunca los recibió— y el arranque siguiente
    re-escaneaba discos como si no hubiera detectado nada: el síntoma es lento y
    silencioso, nunca un error.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    config = Config(config_path)

    escribir_campo(config, "mo2_root", "D:/Modding/MO2")
    escribir_campo(config, "skyrim_path", "D:/Skyrim")
    escribir_campo(config, "loot_exe", "D:/Tools/LOOT/LOOT.exe")
    escribir_campo(config, "xedit_exe", "D:/Tools/xEdit/SSEEdit.exe")
    guardar_config(config, config_path)

    en_disco = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert en_disco["mo2_root"] == "D:/Modding/MO2"
    assert en_disco["skyrim_path"] == "D:/Skyrim"
    assert en_disco["loot_exe"] == "D:/Tools/LOOT/LOOT.exe"
    assert en_disco["xedit_exe"] == "D:/Tools/xEdit/SSEEdit.exe"


def test_guardar_config_rechaza_un_objeto_que_no_sabe_persistirse() -> None:
    """Nunca degradar a un `save` que no corresponde al formato: abortar."""

    class _ConfigAjena:
        pass

    with pytest.raises(TypeError, match="no sabe persistirse"):
        guardar_config(_ConfigAjena(), pathlib.Path("config.toml"))


# ── Ancla por AST: quién puede tocar el `save`/`load` crudos del JSON legacy ────
#: `save` escribe JSON pase lo que pase y `load` devuelve defaults en silencio
#: cuando el archivo no es JSON: juntos son la receta exacta que borró la config.
#: Fuera de `local_config.py` sólo se permiten los usos listados acá.
IMPORTADORES_PERMITIDOS = {
    # Migración legacy: el archivo *sí* es JSON en ese camino (`sky_claw_config.json`).
    "sky_claw/app_context.py": {"load"},
}

_MODULO = "sky_claw.local.local_config"
_CRUDOS = {"load", "save"}


def _nombres_crudos_de(arbol: ast.AST) -> set[str]:
    """Nombres crudos (``load``/``save``) importados de ``local_config``.

    Detecta la forma ABSOLUTA (``from sky_claw.local.local_config import save``)
    y la RELATIVA (``from .local_config import save``,
    ``from ..local.local_config import load``). La relativa es la natural para
    un hermano de ``sky_claw/local/`` — donde vive ``tools_installer.py`` — y
    si el detector solo matcheara el string absoluto del módulo, esa copia-pega
    se saltea el ancla sin romper nada. El escaneo corre solo sobre ``sky_claw/``,
    donde ``local_config`` es único: un homónimo en otro paquete no es riesgo.
    """
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom):
            continue
        if nodo.level == 0:
            if nodo.module != _MODULO:
                continue
        else:
            modulo = nodo.module or ""
            if not (modulo == "local_config" or modulo.endswith(".local_config")):
                continue
        nombres.update(alias.name for alias in nodo.names if alias.name in _CRUDOS)
    return nombres


def _importadores_crudos() -> dict[str, set[str]]:
    encontrados: dict[str, set[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        if archivo.name == "local_config.py":
            continue  # el dueño del formato
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        nombres = _nombres_crudos_de(arbol)
        if nombres:
            encontrados[archivo.relative_to(RAIZ).as_posix()] = nombres
    return encontrados


def test_ancla_de_importadores_del_save_json_crudo() -> None:
    """Igualdad literal, no `issubset`: un importador nuevo rompe hasta que se
    decida si va por `persistir_campo`/`guardar_config` o es otra exención.
    """
    assert _importadores_crudos() == IMPORTADORES_PERMITIDOS


def test_ancla_detecta_import_relativo_del_save_crudo() -> None:
    """La forma relativa es la que alcanza natural un módulo hermano de
    ``sky_claw/local/`` (donde vive ``tools_installer.py``): si el detector
    solo matcheara el string absoluto del módulo, ``from .local_config import
    save`` se saltea la igualdad literal de arriba sin romper nada — y el
    ancla existe para enumerar, no para muestrear (AGENTS.md).
    """
    fuente_absoluta = "from sky_claw.local.local_config import save\n"
    fuente_rel_un_nivel = "from .local_config import save\n"
    fuente_rel_dos_niveles = "from ..local.local_config import load, save\n"
    fuente_inocua = "from .otro_modulo import save\n"

    assert _nombres_crudos_de(ast.parse(fuente_absoluta)) == {"save"}
    assert _nombres_crudos_de(ast.parse(fuente_rel_un_nivel)) == {"save"}
    assert _nombres_crudos_de(ast.parse(fuente_rel_dos_niveles)) == {"load", "save"}
    assert _nombres_crudos_de(ast.parse(fuente_inocua)) == set()


# ── Ancla por AST: nadie escribe campos con `setattr` crudo sobre la config ─────
#: `Config.__getattr__` sólo lee de `_data`, así que `local_cfg.campo = valor`
#: crea un atributo de instancia que se lee bien EN LA MISMA SESIÓN y que ningún
#: `save()` mira jamás: el valor se pierde al reiniciar y nadie ve un error. Es
#: silencioso justamente donde más duele (autodetección zero-config, paths de
#: tools recién instalados), así que se congela en cero.
#:
#: El ancla se ancla al nombre convencional `local_cfg`, que es el que usan las
#: dos superficies y `app_context`: apunta a la copia-pega del patrón existente,
#: que es como se propagó. No pretende cubrir un alias arbitrario.
ASIGNACIONES_CRUDAS_PERMITIDAS: dict[str, int] = {}

_NOMBRE_CONFIG = "local_cfg"


def _asignaciones_crudas() -> dict[str, int]:
    encontradas: dict[str, int] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        objetivos = [
            destino
            for nodo in ast.walk(arbol)
            for destino in (
                nodo.targets
                if isinstance(nodo, ast.Assign)
                else [nodo.target]
                if isinstance(nodo, (ast.AugAssign, ast.AnnAssign))
                else []
            )
            if isinstance(destino, ast.Attribute)
            and isinstance(destino.value, ast.Name)
            and destino.value.id == _NOMBRE_CONFIG
        ]
        if objetivos:
            encontradas[archivo.relative_to(RAIZ).as_posix()] = len(objetivos)
    return encontradas


def test_ancla_de_asignaciones_crudas_sobre_la_config() -> None:
    """Congela la CANTIDAD por módulo, no sólo el nombre: sumar una asignación
    más en un módulo ya listado es el punto de extensión más probable, y con un
    set de nombres pasaría sin romper nada.
    """
    assert _asignaciones_crudas() == ASIGNACIONES_CRUDAS_PERMITIDAS


# ── Ancla por AST: quién serializa TOML de config ──────────────────────────────
# F1 mueve el merge-on-save DENTRO de `Config.save()`: la garantía "todo
# escritor de Config participa" es una propiedad del mecanismo (AGENTS.md), y
# su vía de escape es que alguien vuelva a serializar el TOML por fuera —
# un `tomli_w.dump` nuevo que reescriba el archivo entero sin el merge. Se
# congela el conjunto de módulos que lo llaman, en vez de muestrear.
ESCRITORES_TOML_PERMITIDOS = {
    # El dueño del formato TOML de config; el merge vive en su `save()`.
    "sky_claw/config.py",
}


def _usa_tomli_w_dump(arbol: ast.AST) -> bool:
    """True si el módulo llama ``tomli_w.dump``/``tomli_w.dumps`` en CUALQUIERA
    de sus formas: atributo sobre el módulo (``import tomli_w`` o ``import
    tomli_w as alias``) y nombre importado directo (``from tomli_w import
    dump``, con o sin alias). ``dumps`` cuenta aunque devuelva str en vez de
    escribir el fd: quien serializa el TOML de config por fuera de
    ``Config.save()`` esquiva el merge-on-save igual (review de qodo sobre el
    detector original, que solo matcheaba ``Name("tomli_w").dump``).
    """
    _primitivas = {"dump", "dumps"}
    nombres_modulo: set[str] = set()
    nombres_dump: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name == "tomli_w":
                    nombres_modulo.add(alias.asname or alias.name)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module == "tomli_w":
            for alias in nodo.names:
                if alias.name in _primitivas:
                    nombres_dump.add(alias.asname or alias.name)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _primitivas
            and isinstance(func.value, ast.Name)
            and func.value.id in nombres_modulo
        ):
            return True
        if isinstance(func, ast.Name) and func.id in nombres_dump:
            return True
    return False


def _escritores_de_toml() -> set[str]:
    """Módulos bajo ``sky_claw/`` que llaman ``tomli_w.dump``, la primitiva que
    serializa config TOML, en cualquiera de las formas que detecta
    :func:`_usa_tomli_w_dump`."""
    encontrados: set[str] = set()
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        if _usa_tomli_w_dump(arbol):
            encontrados.add(archivo.relative_to(RAIZ).as_posix())
    return encontrados


def test_ancla_de_escritores_de_toml_de_config() -> None:
    """Igualdad literal: un serializador TOML nuevo rompe hasta que se decida
    si participa del merge de `Config.save()` o es otra exención deliberada.
    """
    assert _escritores_de_toml() == ESCRITORES_TOML_PERMITIDOS


def test_ancla_detecta_todas_las_formas_de_llamar_tomli_w_dump() -> None:
    """El detector no se evade con aliases ni from-imports: la garantía del
    merge vive en `Config.save()`, así que cualquier serializador que la
    esquive tiene que romper el ancla primero (review de CodeRabbit).
    """
    forma_plain = "import tomli_w\n\ntomli_w.dump({}, f)\n"
    forma_alias = "import tomli_w as escritor\n\nescritor.dump({}, f)\n"
    forma_from = "from tomli_w import dump\n\ndump({}, f)\n"
    forma_from_alias = "from tomli_w import dump as volcar\n\nvolcar({}, f)\n"
    forma_dumps = "import tomli_w\n\ncontenido = tomli_w.dumps({})\n"
    forma_dumps_from = "from tomli_w import dumps\n\ncontenido = dumps({})\n"
    forma_inocua = "import tomli_w\n\ntomli_w.loads(x)\n"
    forma_modulo_distinto = "import otro_modulo as tw\n\ntw.dump({}, f)\n"

    assert _usa_tomli_w_dump(ast.parse(forma_plain)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_alias)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_from)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_from_alias)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_dumps)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_dumps_from)) is True
    assert _usa_tomli_w_dump(ast.parse(forma_inocua)) is False
    assert _usa_tomli_w_dump(ast.parse(forma_modulo_distinto)) is False
