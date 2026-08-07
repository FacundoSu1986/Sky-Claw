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
import json
import pathlib
import tomllib

import pytest

from sky_claw.config import Config
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
# El lock (`_CONFIG_WRITE_LOCK`) da UNA garantía: el ciclo leer-modificar-escribir
# de cada escritor corre de punta a punta sin que el otro empiece en el medio.
# Eso alcanza para que el archivo NUNCA quede corrupto ni con una escritura a
# medio hacer. Lo que el lock NO puede dar es "el escritor que iba último no
# pise lo que el otro acaba de guardar": eso depende de si ESE escritor tenía
# una vista actualizada del archivo al momento de empezar a escribir.
#
# `persistir_campo` (GUI) siempre relee el archivo antes de escribir — dentro
# del lock, así que ve el estado más reciente posible. `guardar_config` sobre
# un `Config` (agente LLM) escribe el `_data` en MEMORIA de un objeto que
# `AppContext` construye UNA vez y mantiene vivo toda la sesión: si ese objeto
# nunca releyó el campo que el otro camino acaba de escribir, su próximo
# `save()` lo pisa igual, lock o no lock — es el modelo de "el objeto entero
# es la fuente de verdad" de `Config`, no algo que un lock de escritura
# resuelva. Esto es preexistente a este PR (todo `.save()` de `Config` en el
# árbol comparte el mismo modelo) y una solución completa (merge por campo en
# vez de reemplazo del objeto entero) es un cambio de arquitectura aparte —
# los dos tests de abajo documentan la frontera exacta en vez de afirmar más
# de lo que el lock realmente garantiza.
async def test_lock_evita_la_escritura_entrelazada_y_corrupta(tmp_path: pathlib.Path) -> None:
    """Garantía real del lock: el archivo nunca queda a medio escribir.

    Sin el lock, dos ``to_thread`` concurrentes sobre el mismo ``config.toml``
    pueden intercalar sus escrituras (dos ``os.replace`` compitiendo) — acá se
    fuerza el solapamiento con un ``sleep`` dentro de la ventana del lock (en
    vez de confiar en el scheduler) para que el test sea determinístico, y se
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


async def test_limite_conocido_un_config_vivo_pisa_lo_que_el_otro_camino_guardo(
    tmp_path: pathlib.Path,
) -> None:
    """El caso que el lock NO resuelve, documentado a propósito: si el objeto
    `Config` de larga vida del agente ya existía ANTES de que la GUI guardara
    `ngio_mods`, el `save()` del agente —posterior, y sin corrupción, el lock
    funcionó— reemplaza el archivo entero con su propio `_data`, que nunca vio
    ese campo. Se pierde igual que antes de este PR: el lock serializa el
    ACCESO, no fusiona el CONTENIDO. Este test existe para que ese límite no se
    reintroduzca como "ya arreglado" sin que alguien lo note — si algún día se
    agrega merge-on-save, este test debe empezar a fallar y hay que
    actualizarlo, no borrarlo en silencio.
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
    # El archivo sigue siendo TOML válido y completo (no corrupto) — pero el
    # campo que la GUI escribió desapareció: el `_data` del agente no lo tenía.
    assert "ngio_mods" not in en_disco
    assert en_disco["community_shaders_mods"] == ["Community Shaders"]
    assert en_disco["mo2_root"] == "D:/MO2"


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


def _importadores_crudos() -> dict[str, set[str]]:
    encontrados: dict[str, set[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        if archivo.name == "local_config.py":
            continue  # el dueño del formato
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        nombres = {
            alias.name
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.module == _MODULO
            for alias in nodo.names
            if alias.name in _CRUDOS
        }
        if nombres:
            encontrados[archivo.relative_to(RAIZ).as_posix()] = nombres
    return encontrados


def test_ancla_de_importadores_del_save_json_crudo() -> None:
    """Igualdad literal, no `issubset`: un importador nuevo rompe hasta que se
    decida si va por `persistir_campo`/`guardar_config` o es otra exención.
    """
    assert _importadores_crudos() == IMPORTADORES_PERMITIDOS


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
