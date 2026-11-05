"""Issue #866: vigencia contextual del testigo durable, simetría M-04 e I/O del almacén.

Cierra los hallazgos de la auditoría adversarial de los tramos #862/#864/#865 que
**no** nacieron en #865:

- **H-1** — el testigo durable no tenía vigencia. Si ``preparar`` guardaba ``S0`` con el
  load order ``LO1``, el operador construía ``S1`` sin verificar y después LOOT pasaba a
  ``LO2``, tanto ``preparar`` (``conservado_por_build``) como ``verificar`` seguían
  comparando contra ``S0`` y declaraban ``frescura_probada=True`` sobre un parche
  anterior a ``LO2``. La vigencia es una **función determinista del contexto de entrada
  del perfil** (``huella_contexto``), no del reloj (ver no-objetivos de la issue).
- **H-2** — ``AsyncToolRegistry`` construía el servicio sin ``PluginLimitGuard`` y pasaba
  ``validate_limit=False``: con >254 plugins la GUI abortaba por M-04 y el agente LLM no.
- **H-4 / L-2 / L-3** — el reemplazo atómico del almacén no reintentaba una violación de
  uso compartido transitoria en Windows, dejaba escapar ``OSError`` crudos y nunca
  barría los ``.tmp`` huérfanos de un proceso muerto.

Cada bloque nombra la mutación (M1..M10 de la issue) que lo pone en rojo.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import stat
import sys
from unittest.mock import patch

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.local.tools import etapa6_testigos
from sky_claw.local.tools.etapa6_testigos import (
    AlmacenDeTestigosIlegibleError,
    AlmacenEnMemoria,
    AlmacenJSON,
    TestigoDurable,
    clave_de_testigo,
)
from sky_claw.local.tools.wrye_bash_asistida import (
    TestigoPrevio,
    decidir_testigo_de_preparacion,
    huella_de_contexto,
)
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from tests.test_etapa6_testigo_durable import _escenario, _preflight_verde, _resolver_real

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64
LO1 = ("*Skyrim.esm", "*ModA.esp", "*ModB.esp")
LO2 = ("*Skyrim.esm", "*ModB.esp", "*ModA.esp", "*ModC.esp")


def _escribir(ruta: pathlib.Path, lineas: tuple[str, ...]) -> None:
    ruta.write_text("".join(f"{linea}\n" for linea in lineas), encoding="utf-8")


# =============================================================================
# 1. La huella del contexto de entrada (función pura sobre el perfil)
# =============================================================================


def test_la_huella_excluye_la_entrada_propia_del_bashed_patch(tmp_path: pathlib.Path) -> None:
    """Activar el parche recién construido (paso 5 de ``PASOS_ASISTIDOS``) agrega
    ``*Bashed Patch, 0.esp`` a ``plugins.txt``: no puede cambiar la huella, o el build
    legítimo invalidaría su propio testigo. Se mide con la entrada activa, inactiva, en
    otra posición y con otra capitalización."""
    plugins = tmp_path / "plugins.txt"
    _escribir(plugins, LO1)
    base = huella_de_contexto(plugins)
    assert base is not None

    variantes = (
        (*LO1, f"*{BASHED_PATCH_NAME}"),
        (*LO1, BASHED_PATCH_NAME),
        (f"*{BASHED_PATCH_NAME}", *LO1),
        (*LO1, f"*{BASHED_PATCH_NAME.upper()}"),
    )
    for lineas in variantes:
        _escribir(plugins, lineas)
        assert huella_de_contexto(plugins) == base, lineas


def test_la_huella_cambia_ante_cualquier_cambio_real_de_plugins(tmp_path: pathlib.Path) -> None:
    """M1: ignorar el reorden / alta / cambio de activación de otros plugins."""
    plugins = tmp_path / "plugins.txt"
    huellas: set[str | None] = set()
    for lineas in (
        LO1,
        LO2,
        ("*Skyrim.esm", "*ModB.esp", "*ModA.esp"),  # sólo reorden
        ("*Skyrim.esm", "ModA.esp", "*ModB.esp"),  # ModA desactivado
        (*LO1, "*ModD.esp"),  # alta
    ):
        _escribir(plugins, lineas)
        huellas.add(huella_de_contexto(plugins))
    assert None not in huellas
    assert len(huellas) == 5, "cada variante real del load order debe tener su propia huella"


def test_la_huella_cambia_con_el_modlist_hermano(tmp_path: pathlib.Path) -> None:
    """M2: omitir ``modlist.txt`` (la prioridad de los mods) del digest."""
    plugins = tmp_path / "plugins.txt"
    modlist = tmp_path / "modlist.txt"
    _escribir(plugins, LO1)
    _escribir(modlist, ("+ModB", "+ModA"))
    antes = huella_de_contexto(plugins)
    _escribir(modlist, ("+ModA", "+ModB"))
    reordenado = huella_de_contexto(plugins)
    _escribir(modlist, ("+ModB", "-ModA"))
    deshabilitado = huella_de_contexto(plugins)
    assert len({antes, reordenado, deshabilitado}) == 3


def test_la_huella_ignora_el_fin_de_linea_y_es_determinista(tmp_path: pathlib.Path) -> None:
    plugins = tmp_path / "plugins.txt"
    modlist = tmp_path / "modlist.txt"
    plugins.write_bytes(b"*Skyrim.esm\r\n*ModA.esp\r\n")
    modlist.write_bytes(b"+ModA\r\n")
    crlf = huella_de_contexto(plugins)
    plugins.write_bytes(b"*Skyrim.esm\n*ModA.esp\n")
    modlist.write_bytes(b"+ModA\n")
    assert huella_de_contexto(plugins) == crlf
    assert huella_de_contexto(plugins) == huella_de_contexto(plugins)


def test_la_huella_es_none_si_el_plugins_txt_no_se_puede_leer(tmp_path: pathlib.Path) -> None:
    """Ilegible ⇒ ``None`` ("no sé"), nunca un digest de "vacío": un fallo de lectura no
    puede colapsar en "el contexto no cambió"."""
    plugins = tmp_path / "plugins.txt"
    plugins.mkdir()
    assert huella_de_contexto(plugins) is None


def test_la_huella_distingue_ausente_de_ilegible(tmp_path: pathlib.Path) -> None:
    """Un perfil recién creado sin ``plugins.txt`` es una observación válida, no un fallo."""
    assert huella_de_contexto(tmp_path / "plugins.txt") is not None


# =============================================================================
# 2. El registro durable lleva la huella (y lee los registros de PR D)
# =============================================================================


def test_el_registro_durable_persiste_y_relee_la_huella(tmp_path: pathlib.Path) -> None:
    durable = TestigoDurable(existia=False, sha256=None, observado_en="2026-11-05T00:00:00Z", huella_contexto=SHA_A)
    assert TestigoDurable.desde_dict(durable.como_dict()) == durable
    assert durable.como_dict()["huella_contexto"] == SHA_A


def test_un_registro_de_pr_d_sin_huella_se_lee_como_contexto_desconocido() -> None:
    """Compatibilidad: los registros ya escritos por #864 (4 claves) no se descartan, pero
    tampoco pueden afirmar un contexto."""
    legado = {"existia": False, "sha256": None, "observado_en": "2026-11-05T00:00:00Z", "generacion": "c" * 32}
    leido = TestigoDurable.desde_dict(legado)
    assert leido is not None
    assert leido.huella_contexto is None


@pytest.mark.parametrize("invalida", ["", "xyz", "A" * 64, "a" * 63, 7])
def test_una_huella_malformada_descarta_el_registro(invalida: object) -> None:
    crudo = {
        "existia": False,
        "sha256": None,
        "observado_en": "2026-11-05T00:00:00Z",
        "generacion": "c" * 32,
        "huella_contexto": invalida,
    }
    assert TestigoDurable.desde_dict(crudo) is None


# =============================================================================
# 3. La decisión pura
# =============================================================================


def test_decision_construido_con_el_mismo_contexto_conserva_el_testigo_previo() -> None:
    durable = TestigoPrevio(existia=False, sha256=None)
    ahora = TestigoPrevio(existia=True, sha256=SHA_A)
    decision = decidir_testigo_de_preparacion(
        observado_ahora=ahora,
        durable=durable,
        verificar_contexto=True,
        contexto_ahora=SHA_A,
        contexto_durable=SHA_A,
    )
    assert decision.construido_despues is True
    assert decision.persistir is False
    assert decision.testigo == durable


@pytest.mark.parametrize("contexto_durable", [SHA_B, None])
def test_decision_construido_con_otro_contexto_adopta_lo_observado(contexto_durable: str | None) -> None:
    """M1/M3 en la capa pura: el testigo anterior no describe una corrida de otro contexto."""
    ahora = TestigoPrevio(existia=True, sha256=SHA_A)
    decision = decidir_testigo_de_preparacion(
        observado_ahora=ahora,
        durable=TestigoPrevio(existia=False, sha256=None),
        verificar_contexto=True,
        contexto_ahora=SHA_A,
        contexto_durable=contexto_durable,
    )
    assert decision.construido_despues is False
    assert decision.persistir is True
    assert decision.testigo == ahora


def test_decision_con_contexto_no_observable_conserva_el_durable_sin_tocarlo() -> None:
    durable = TestigoPrevio(existia=False, sha256=None)
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=True, sha256=SHA_A),
        durable=durable,
        verificar_contexto=True,
        contexto_ahora=None,
        contexto_durable=SHA_A,
    )
    assert decision.persistir is False
    assert decision.construido_despues is False
    assert decision.testigo == durable


def test_decision_sin_verificar_contexto_conserva_el_comportamiento_de_pr_d() -> None:
    """Los llamadores puros (y el almacén invocado sin ``contexto``) no cambian."""
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=True, sha256=SHA_A),
        durable=TestigoPrevio(existia=False, sha256=None),
    )
    assert decision.construido_despues is True


# =============================================================================
# 4. El servicio de extremo a extremo (M1, M2, M3 de la issue)
# =============================================================================


def _hacer_servicio_json(
    tmp_path: pathlib.Path,
) -> tuple[WryeBashPipelineService, pathlib.Path, pathlib.Path, pathlib.Path]:
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    servicio = WryeBashPipelineService(
        path_resolver=_resolver_real(game, tmp_path),
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(tmp_path / "state" / "etapa6_testigos.json"),
    )
    return servicio, game, perfil, overwrite


async def test_m1_loot_entre_el_build_y_la_preparacion_invalida_el_testigo(tmp_path: pathlib.Path) -> None:
    """``preparar(LO1) → build S1 → LOOT(LO2) → preparar(LO2)``: el testigo viejo NO se
    conserva (no se afirma un build) y la época nueva adopta lo observado."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")
    assert p1["testigo_durable"] == "guardado"

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))

    p2 = await servicio.preparar_etapa_asistida(profile="Default")
    assert p2["testigo_durable"] == "guardado"
    assert p2["construido_despues_de_la_preparacion"] is False
    assert p2["testigo_previo"]["existia"] is True
    assert p2["testigo_previo"]["generacion"] != p1["testigo_previo"]["generacion"]

    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["frescura_probada"] is False
    assert v["motivo"] == "SIN_CAMBIO"


async def test_m1_verificar_directo_tras_loot_no_certifica_frescura(tmp_path: pathlib.Path) -> None:
    """El operador llama directo a ``verificar`` sin un segundo ``preparar``."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    await servicio.preparar_etapa_asistida(profile="Default")

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))

    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["success"] is True
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "cambio"


async def test_m1_el_payload_de_la_gui_tampoco_certifica_tras_loot(tmp_path: pathlib.Path) -> None:
    """La GUI viaja con ``testigo_previo`` (+``generacion``): el registro durable de esa
    misma época gobierna el contexto."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))

    v = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=p1["testigo_previo"])
    assert v["testigo_origen"] == "payload"
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "cambio"


async def test_m2_un_cambio_en_el_modlist_invalida_el_testigo(tmp_path: pathlib.Path) -> None:
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    _escribir(perfil / "modlist.txt", ("+ModB", "+ModA"))
    await servicio.preparar_etapa_asistida(profile="Default")

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "modlist.txt", ("+ModA", "+ModB"))

    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "cambio"


async def test_m3_build_y_activacion_legitimos_conservan_el_testigo(tmp_path: pathlib.Path) -> None:
    """Camino feliz: el operador construye y tilda el parche (``plugins.txt`` gana
    ``*Bashed Patch, 0.esp``) y reabre la GUI en frío. La huella NO cambia."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    _escribir(perfil / "modlist.txt", ("+ModB", "+ModA"))
    p1 = await servicio.preparar_etapa_asistida(profile="Default")

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO1, f"*{BASHED_PATCH_NAME}"))

    p2 = await servicio.preparar_etapa_asistida(profile="Default")
    assert p2["testigo_durable"] == "conservado_por_build"
    assert p2["testigo_previo"] == {"existia": False, "sha256": None, "generacion": p1["testigo_previo"]["generacion"]}

    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["success"] is True
    assert v["frescura_probada"] is True
    assert v["testigo_contexto"] == "coincide"


async def test_loot_antes_del_build_no_invalida_la_epoca(tmp_path: pathlib.Path) -> None:
    """LOOT corre **antes** de construir: el artefacto sigue ausente (misma identidad), así
    que la época se conserva y sólo se actualiza su huella. El build posterior se prueba."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")

    _escribir(perfil / "plugins.txt", LO2)
    p2 = await servicio.preparar_etapa_asistida(profile="Default")
    assert p2["testigo_previo"]["generacion"] == p1["testigo_previo"]["generacion"]

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO2" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))
    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["frescura_probada"] is True
    assert v["testigo_contexto"] == "coincide"


async def test_un_registro_de_pr_d_sin_huella_no_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """Un registro escrito por #864 (sin ``huella_contexto``) no puede afirmar que el
    contexto no cambió: la verificación lo declara ``no_verificable``."""
    servicio, game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", (*LO1, f"*{BASHED_PATCH_NAME}"))
    ruta = tmp_path / "state" / "etapa6_testigos.json"
    clave = clave_de_testigo(plugins_txt=perfil / "plugins.txt", destino_data=game / "Data" / BASHED_PATCH_NAME)
    ruta.parent.mkdir(parents=True)
    ruta.write_text(
        json.dumps(
            {
                clave.como_texto(): {
                    "existia": False,
                    "sha256": None,
                    "observado_en": "2026-11-04T00:00:00Z",
                    "generacion": "d" * 32,
                }
            }
        ),
        encoding="utf-8",
    )
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x01" * 60)

    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["success"] is True
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "no_verificable"


async def test_sin_registro_durable_el_contexto_no_aplica(tmp_path: pathlib.Path) -> None:
    """Sin almacén (o payload sin ``generacion``) no hay registro que gobierne: el campo lo
    declara en vez de omitirse."""
    servicio, _game, perfil, overwrite, _almacen = _escenario(tmp_path, con_almacen=False)
    _escribir(perfil / "plugins.txt", LO1)
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x01" * 60)
    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["testigo_contexto"] == "no_aplica"


async def test_el_almacen_en_memoria_gobierna_el_contexto_igual_que_el_json(tmp_path: pathlib.Path) -> None:
    """Ambas implementaciones del Protocol comparten ``_resolver_preparacion_durable``."""
    servicio, _game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert isinstance(almacen, AlmacenEnMemoria)
    _escribir(perfil / "plugins.txt", LO1)
    await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x01" * 60)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))
    v = await servicio.verificar_etapa_asistida(profile="Default")
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "cambio"


async def test_un_payload_de_una_epoca_reemplazada_no_prueba_frescura(tmp_path: pathlib.Path) -> None:
    """Review Codex P2 (#870): la GUI conserva el payload de la época A; el operador construye,
    LOOT cambia el load order y OTRA preparación reemplaza la época por B. El registro de A ya no
    existe, así que no hay huella con qué validar el contexto: el testigo del payload no puede
    atestiguar nada (antes: ``success=True, frescura_probada=True, testigo_contexto=no_aplica``)."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))
    p2 = await servicio.preparar_etapa_asistida(profile="Default")
    assert p2["testigo_previo"]["generacion"] != p1["testigo_previo"]["generacion"]

    v = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=p1["testigo_previo"])
    assert v["success"] is True
    assert v["testigo_origen"] == "payload"
    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "no_verificable"


async def test_un_payload_ya_consumido_no_vuelve_a_probar_frescura(tmp_path: pathlib.Path) -> None:
    """Repetir la verificación con el mismo payload tras el consumo: la época ya no existe."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO1, f"*{BASHED_PATCH_NAME}"))

    primera = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=p1["testigo_previo"])
    assert primera["frescura_probada"] is True
    assert primera["testigo_contexto"] == "coincide"

    repetida = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=p1["testigo_previo"])
    assert repetida["success"] is True
    assert repetida["frescura_probada"] is False
    assert repetida["testigo_contexto"] == "no_verificable"


async def test_si_el_almacen_no_se_puede_leer_el_payload_con_generacion_falla_cerrado(
    tmp_path: pathlib.Path,
) -> None:
    """Review pr-agent (#870): un ``OSError`` al leer el registro de la época no puede apagar en
    silencio la validación de contexto (fail-open)."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", LO1)
    p1 = await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4_LO1" + b"\x01" * 56)
    _escribir(perfil / "plugins.txt", (*LO2, f"*{BASHED_PATCH_NAME}"))

    with patch.object(servicio._testigo_store, "leer", side_effect=OSError("almacén no disponible")):  # noqa: SLF001
        v = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=p1["testigo_previo"])

    assert v["frescura_probada"] is False
    assert v["testigo_contexto"] == "no_verificable"


async def test_un_payload_sin_generacion_sigue_sin_registro_que_lo_gobierne(tmp_path: pathlib.Path) -> None:
    """Contraste que acota el cambio: sin ``generacion`` el payload jamás provino de un registro
    persistido (``no_disponible`` / ``error_de_almacen``), así que sigue siendo ``no_aplica``."""
    servicio, _game, perfil, overwrite = _hacer_servicio_json(tmp_path)
    _escribir(perfil / "plugins.txt", (*LO1, f"*{BASHED_PATCH_NAME}"))
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x01" * 60)
    v = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo={"existia": False, "sha256": None})
    assert v["frescura_probada"] is True
    assert v["testigo_contexto"] == "no_aplica"


# =============================================================================
# 5. H-2: el guard M-04 en la superficie del agente LLM (M4)
# =============================================================================


async def test_m4_la_tool_del_llm_aborta_por_m04_con_mas_de_254_plugins(tmp_path: pathlib.Path) -> None:
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    _escribir(perfil / "plugins.txt", tuple(f"*Mod_{i:03d}.esp" for i in range(256)))
    _escribir(perfil / "modlist.txt", ("+ModA",))

    resolver = _resolver_real(game, tmp_path)
    resolver.resolve_modlist_path.return_value = perfil / "modlist.txt"

    with patch.object(WryeBashPipelineService, "_ensure_preflight", return_value=None):
        registro = AsyncToolRegistry(
            registry=None, mo2=None, sync_engine=None, mo2_profile="Default", path_resolver=resolver
        )
        resultado = json.loads(await registro.execute("generate_bashed_patch", {"fase": "preparar"}))

    assert resultado.get("aborted_by") == "plugin_limit_guard"
    assert resultado.get("plugin_count") == 256


def test_m4_ancla_ast_la_tool_del_llm_cablea_el_guard_y_valida_el_limite() -> None:
    """Hermano por AST: ni ``plugin_limit_guard`` ausente ni ``validate_limit=False``."""
    ruta = RAIZ / "sky_claw/app/agent/tools/system_tools.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    funcion = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "generate_bashed_patch"
    )
    llamadas = [n for n in ast.walk(funcion) if isinstance(n, ast.Call)]

    construcciones = [c for c in llamadas if getattr(c.func, "id", None) == "WryeBashPipelineService"]
    assert len(construcciones) == 1
    kw = {k.arg: k.value for k in construcciones[0].keywords if k.arg}
    assert "plugin_limit_guard" in kw
    assert not (isinstance(kw["plugin_limit_guard"], ast.Constant) and kw["plugin_limit_guard"].value is None)

    delegadas = [c for c in llamadas if getattr(c.func, "attr", None) == "ejecutar_fase_asistida"]
    assert len(delegadas) == 1
    kw = {k.arg: k.value for k in delegadas[0].keywords if k.arg}
    assert "validate_limit" in kw
    assert not (isinstance(kw["validate_limit"], ast.Constant) and kw["validate_limit"].value is False)
    assert "M-04 se valida a nivel Supervisor" not in ruta.read_text(encoding="utf-8")


def test_m4_todo_sitio_productivo_del_servicio_pasa_el_guard_m04() -> None:
    """Enumera, no muestrea: TODA construcción de ``WryeBashPipelineService`` en ``sky_claw/``
    cablea ``plugin_limit_guard``. #866 arregló el hermano que faltaba (``system_tools``); un
    cuarto sitio futuro sin guard rompe este ancla en vez de repetir la asimetría."""
    sitios: dict[str, ast.Call] = {}
    for py in sorted(RAIZ.joinpath("sky_claw").rglob("*.py")):
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "WryeBashPipelineService":
                sitios[f"{py.relative_to(RAIZ).as_posix()}:{nodo.lineno}"] = nodo

    modulos = {clave.split(":")[0] for clave in sitios}
    assert modulos == {
        "sky_claw/app/agent/tools/system_tools.py",
        "sky_claw/app/orchestrator/orchestration_composition.py",
    }, f"cambió el conjunto de sitios productivos: {sorted(modulos)}"

    sin_guard = [
        clave
        for clave, llamada in sitios.items()
        if "plugin_limit_guard" not in {k.arg for k in llamada.keywords}
        or any(
            k.arg == "plugin_limit_guard" and isinstance(k.value, ast.Constant) and k.value.value is None
            for k in llamada.keywords
        )
    ]
    assert sin_guard == [], f"construcciones del servicio sin guard M-04: {sin_guard}"


# =============================================================================
# 6. H-4 / L-3 / L-2: el reemplazo atómico del almacén
# =============================================================================


def _clave_de(tmp_path: pathlib.Path):  # noqa: ANN202
    return clave_de_testigo(
        plugins_txt=tmp_path / "profiles" / "Default" / "plugins.txt",
        destino_data=tmp_path / "Skyrim" / "Data" / BASHED_PATCH_NAME,
    )


def _error_de_uso_compartido(winerror: int) -> PermissionError:
    err = PermissionError(13, "sharing violation")
    err.winerror = winerror  # type: ignore[attr-defined]
    return err


@pytest.fixture
def sin_espera(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Registra los backoffs sin dormir."""
    pausas: list[float] = []
    monkeypatch.setattr(etapa6_testigos.time, "sleep", pausas.append)
    return pausas


@pytest.mark.parametrize("winerror", [5, 32])
def test_m7_una_violacion_transitoria_de_uso_compartido_se_reintenta_en_windows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sin_espera: list[float], winerror: int
) -> None:
    monkeypatch.setattr(etapa6_testigos, "_es_windows", lambda: True)
    almacen = AlmacenJSON(tmp_path / "state" / "etapa6_testigos.json")
    llamadas = 0
    original = os.replace

    def intermitente(origen: str, destino: str) -> None:
        nonlocal llamadas
        llamadas += 1
        if llamadas <= 2:
            raise _error_de_uso_compartido(winerror)
        original(origen, destino)

    with patch("os.replace", side_effect=intermitente):
        durable = almacen.guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))

    assert llamadas == 3
    assert almacen.leer(_clave_de(tmp_path)) == durable
    assert len(sin_espera) == 2
    assert sin_espera == sorted(sin_espera), "el backoff no puede decrecer"


def test_m8_no_se_reintenta_fuera_de_windows_ni_un_error_no_transitorio(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sin_espera: list[float]
) -> None:
    almacen = AlmacenJSON(tmp_path / "state" / "etapa6_testigos.json")
    # POSIX: un PermissionError no es transitorio.
    monkeypatch.setattr(etapa6_testigos, "_es_windows", lambda: False)
    llamadas = 0

    def siempre(_o: str, _d: str) -> None:
        nonlocal llamadas
        llamadas += 1
        raise _error_de_uso_compartido(32)

    with patch("os.replace", side_effect=siempre), pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))
    assert llamadas == 1

    # Windows pero con un winerror que no es de uso compartido (p. ej. 3: ruta no encontrada).
    monkeypatch.setattr(etapa6_testigos, "_es_windows", lambda: True)
    llamadas = 0

    def permanente(_o: str, _d: str) -> None:
        nonlocal llamadas
        llamadas += 1
        raise _error_de_uso_compartido(3)

    with patch("os.replace", side_effect=permanente), pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))
    assert llamadas == 1
    assert sin_espera == []


def test_m9_agotados_los_reintentos_se_levanta_el_error_del_almacen_y_no_queda_temporal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sin_espera: list[float]
) -> None:
    monkeypatch.setattr(etapa6_testigos, "_es_windows", lambda: True)
    ruta = tmp_path / "state" / "etapa6_testigos.json"
    almacen = AlmacenJSON(ruta)

    with (
        patch("os.replace", side_effect=_error_de_uso_compartido(32)),
        pytest.raises(AlmacenDeTestigosIlegibleError) as info,
    ):
        almacen.guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))

    assert isinstance(info.value.__cause__, PermissionError)
    assert len(sin_espera) >= 1, "se agotó un presupuesto de reintentos, no un único intento"
    assert [p.name for p in ruta.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_m9_un_fallo_al_crear_el_directorio_es_un_error_del_almacen(tmp_path: pathlib.Path) -> None:
    """L-3: el ``mkdir`` de ``_escribir_atomico`` también se envuelve.

    Se invoca el método directamente: por ``guardar`` el ``mkdir`` de ``_seccion_critica`` falla
    antes y enmascara al de ``_escribir_atomico`` (mutación M9b: este test sobrevivía).
    """
    ruta = tmp_path / "state" / "etapa6_testigos.json"
    almacen = AlmacenJSON(ruta)
    with (
        patch.object(pathlib.Path, "mkdir", side_effect=PermissionError(13, "denegado")),
        pytest.raises(AlmacenDeTestigosIlegibleError) as info,
    ):
        almacen._escribir_atomico(ruta, {})  # noqa: SLF001
    assert isinstance(info.value.__cause__, PermissionError)


def test_m10_un_temporal_huerfano_de_un_proceso_muerto_se_barre_bajo_el_lock(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / "state" / "etapa6_testigos.json"
    ruta.parent.mkdir(parents=True)
    huerfano = ruta.parent / ".etapa6_testigos.crash_previo.tmp"
    huerfano.write_text("parcial", encoding="utf-8")
    ajeno = ruta.parent / ".otro_archivo.tmp"
    ajeno.write_text("no es mío", encoding="utf-8")

    AlmacenJSON(ruta).guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))

    assert not huerfano.exists()
    assert ajeno.exists(), "sólo se barren los temporales de ESTE almacén"
    assert ruta.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requiere symlinks sin privilegios")
def test_m10_el_barrido_no_sigue_enlaces_ni_toca_no_regulares(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / "state" / "etapa6_testigos.json"
    ruta.parent.mkdir(parents=True)
    objetivo = tmp_path / "ajeno.txt"
    objetivo.write_text("no tocar", encoding="utf-8")
    enlace = ruta.parent / ".etapa6_testigos.enlace.tmp"
    enlace.symlink_to(objetivo)
    directorio = ruta.parent / ".etapa6_testigos.dir.tmp"
    directorio.mkdir()

    AlmacenJSON(ruta).guardar(_clave_de(tmp_path), TestigoPrevio(existia=False, sha256=None))

    assert objetivo.read_text(encoding="utf-8") == "no tocar"
    assert stat.S_ISLNK(os.lstat(enlace).st_mode), "el enlace se deja como está"
    assert directorio.is_dir()
