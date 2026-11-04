"""PR D de ADR 0016 §7: el testigo de la fase 1 sobrevive al cierre de la GUI.

El testigo previo es lo único que permite **probar** que el Bashed Patch se construyó
durante esta corrida y no es un artefacto viejo que ya estaba ahí. Hasta el PR C vivía
lo que vivía el resultado en ``ReactiveStore`` —un diccionario en RAM
(``sky_claw/app/gui/state/reactive_store.py``), sin persistencia—, y en la superficie
del agente LLM ni siquiera podía viajar entre llamadas porque el schema sólo expone
``fase`` (A-4: aceptar un digest del modelo permitía fabricar frescura).

Este módulo fija las cuatro capas del contrato:

1. **El almacén** (``etapa6_testigos``): guarda el testigo en el estado por usuario de
   Sky-Claw (``SystemPaths.runtime_state_dir() / "etapa6_testigos.json"``, independiente
   del ``cwd`` igual que ADR 0011 P0.2). La clave identifica el **slot virtual** del
   perfil (``plugins_txt`` canónico + destino ``Data`` canónico), NO la capa ganadora del
   momento: en la primera corrida el parche no existe en ningún lado (el resolver cae a
   ``Data``) y cuando el operador construye aparece en ``overwrite`` o en ``mods/<mod>``;
   clavear por la ruta ganadora fallaría el 100 % de las primeras corridas. Un archivo
   ilegible o un enlace nunca se reescribe: ``leer`` degrada a ``None`` y ``guardar``
   levanta ``AlmacenDeTestigosIlegibleError``.
2. **La decisión** (``decidir_testigo_de_preparacion``, función pura): distingue preparar
   por primera vez, preparar sin cambios, preparar tras borrado y preparar **después de
   que el operador ya construyó** (donde sobrescribir el testigo compararía el parche
   contra sí mismo y daría ``SIN_CAMBIO`` sobre un build real).
3. **El servicio** (``WryeBashPipelineService``): partición total de
   ``EstadoDelTestigoDurable`` en la fase 1, precedencia payload > durable en la fase 2
   (sin sustituir un payload explícito malformado por estado de disco), preservación del
   testigo cuando la verificación falla (p. ej. ``NO_HABILITADO`` antes de tildar el
   plugin en MO2), y consumo tanto por ``durable`` como por ``payload`` al probar
   frescura para que una corrida futura no herede un build ya verificado.
4. **El cableado de extremo a extremo**: GUI reabierta en frío, agente LLM en dos
   instancias distintas de ``AsyncToolRegistry``, y ancla AST sobre todas las
   construcciones de ``WryeBashPipelineService`` en ``sky_claw/``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.gui.controllers.ritual_runner import resume_action_from_result
from sky_claw.config import Config
from sky_claw.local.tools.etapa6_testigos import (
    NOMBRE_DEL_ARCHIVO,
    AlmacenDeTestigosIlegibleError,
    AlmacenEnMemoria,
    AlmacenJSON,
    ClaveDeTestigo,
    TestigoDurable,
    almacen_por_defecto,
    clave_de_testigo,
)
from sky_claw.local.tools.wrye_bash_asistida import (
    DecisionDeTestigo,
    OrigenDelTestigo,
    TestigoPrevio,
    decidir_testigo_de_preparacion,
)
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from tests._symlink_guard import symlink_guard

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64


def _sha256_de(ruta: pathlib.Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


# =============================================================================
# 1. EL ALMACÉN — identidad de slot, atomicidad y degradación segura
# =============================================================================


def _clave(
    tmp_path: pathlib.Path,
    *,
    instancia: str = "MO2",
    perfil: str = "Default",
    juego: str = "Skyrim",
    nombre: str = BASHED_PATCH_NAME,
) -> ClaveDeTestigo:
    return clave_de_testigo(
        plugins_txt=tmp_path / instancia / "profiles" / perfil / "plugins.txt",
        destino_data=tmp_path / juego / "Data" / nombre,
    )


def test_el_almacen_devuelve_lo_que_guardo_con_su_marca_de_tiempo(tmp_path: pathlib.Path) -> None:
    almacen = AlmacenEnMemoria()
    guardado = almacen.guardar(_clave(tmp_path), TestigoPrevio(existia=True, sha256=SHA_A))

    leido = almacen.leer(_clave(tmp_path))
    assert leido is not None
    assert leido.existia is True
    assert leido.sha256 == SHA_A
    assert leido.observado_en == guardado.observado_en


def test_el_almacen_distingue_perfiles_instancias_y_juegos(tmp_path: pathlib.Path) -> None:
    """El testigo de una instancia/perfil/juego no puede verificar otro namespace."""
    almacen = AlmacenEnMemoria()
    almacen.guardar(
        _clave(tmp_path, instancia="ListaA", perfil="Default", juego="SSE"), TestigoPrevio(existia=True, sha256=SHA_A)
    )

    assert almacen.leer(_clave(tmp_path, instancia="ListaA", perfil="Survival", juego="SSE")) is None
    assert almacen.leer(_clave(tmp_path, instancia="ListaB", perfil="Default", juego="SSE")) is None
    assert almacen.leer(_clave(tmp_path, instancia="ListaA", perfil="Default", juego="AE")) is None


def test_la_clave_canoniza_rutas_y_es_inyectiva_con_separadores_raros(tmp_path: pathlib.Path) -> None:
    """``a/x/../b`` y ``a/b`` colisionan; dos pares distintos con ``|`` NO colisionan."""
    directa = clave_de_testigo(
        plugins_txt=tmp_path / "MO2" / "profiles" / "Default" / "plugins.txt",
        destino_data=tmp_path / "Skyrim" / "Data" / BASHED_PATCH_NAME,
    )
    torcida = clave_de_testigo(
        plugins_txt=tmp_path / "MO2" / "profiles" / "x" / ".." / "Default" / "plugins.txt",
        destino_data=tmp_path / "Skyrim" / "otra" / ".." / "Data" / BASHED_PATCH_NAME,
    )
    assert directa == torcida
    assert directa.como_texto() == torcida.como_texto()

    par_1 = ClaveDeTestigo(plugins_txt="a|b", destino_data="c")
    par_2 = ClaveDeTestigo(plugins_txt="a", destino_data="b|c")
    assert par_1.como_texto() != par_2.como_texto()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"existia": 1, "sha256": SHA_A, "observado_en": "2026-11-04T10:00:00Z"},
        {"existia": True, "sha256": None, "observado_en": "2026-11-04T10:00:00Z"},
        {"existia": False, "sha256": SHA_A, "observado_en": "2026-11-04T10:00:00Z"},
        {"existia": True, "sha256": "A" * 64, "observado_en": "2026-11-04T10:00:00Z"},
        {"existia": False, "sha256": None, "observado_en": ""},
    ],
)
def test_testigo_durable_rechaza_formas_que_el_dominio_no_admite(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TestigoDurable(**kwargs)  # type: ignore[arg-type]


def test_el_archivo_json_sobrevive_a_un_proceso_nuevo(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    AlmacenJSON(ruta).guardar(_clave(tmp_path), TestigoPrevio(existia=False, sha256=None))

    leido = AlmacenJSON(ruta).leer(_clave(tmp_path))
    assert leido is not None
    assert leido.existia is False and leido.sha256 is None


def test_el_archivo_json_no_deja_residuos_temporales(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    AlmacenJSON(ruta).guardar(_clave(tmp_path), TestigoPrevio(existia=True, sha256=SHA_A))

    assert [p.name for p in tmp_path.iterdir()] == [NOMBRE_DEL_ARCHIVO]


def test_el_archivo_json_corrupto_no_se_pisa_ni_al_leer_ni_al_guardar(tmp_path: pathlib.Path) -> None:
    """Ni lector ni escritor pisan un archivo corrupto: se preserva para diagnóstico."""
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    ruta.write_text("{esto no es json", encoding="utf-8")
    almacen = AlmacenJSON(ruta)

    assert almacen.leer(_clave(tmp_path)) is None
    with pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.guardar(_clave(tmp_path), TestigoPrevio(existia=False, sha256=None))
    with pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.olvidar(_clave(tmp_path))

    assert ruta.read_text(encoding="utf-8") == "{esto no es json"


@symlink_guard
def test_el_almacen_rechaza_un_enlace_en_la_ruta_del_archivo(tmp_path: pathlib.Path) -> None:
    destino_ajeno = tmp_path / "ajeno.json"
    destino_ajeno.write_text("{}", encoding="utf-8")
    enlace = tmp_path / NOMBRE_DEL_ARCHIVO
    enlace.symlink_to(destino_ajeno)
    almacen = AlmacenJSON(enlace)
    assert almacen.leer(_clave(tmp_path)) is None
    with pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.guardar(_clave(tmp_path), TestigoPrevio(existia=True, sha256=SHA_A))
    assert destino_ajeno.read_text(encoding="utf-8") == "{}"


@symlink_guard
def test_el_almacen_rechaza_un_enlace_en_el_directorio_padre(tmp_path: pathlib.Path) -> None:
    """``mkstemp`` + ``os.replace`` en un padre enlazado escribirían en el directorio ajeno."""
    dir_ajeno = tmp_path / "dir_ajeno"
    dir_ajeno.mkdir()
    padre_enlace = tmp_path / "state_enlace"
    padre_enlace.symlink_to(dir_ajeno, target_is_directory=True)
    almacen = AlmacenJSON(padre_enlace / NOMBRE_DEL_ARCHIVO)

    assert almacen.leer(_clave(tmp_path)) is None
    with pytest.raises(AlmacenDeTestigosIlegibleError):
        almacen.guardar(_clave(tmp_path), TestigoPrevio(existia=True, sha256=SHA_A))
    assert list(dir_ajeno.iterdir()) == []


def test_una_entrada_con_forma_invalida_se_descarta_sin_tumbar_las_demas(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    buena = _clave(tmp_path)
    mala = ClaveDeTestigo(plugins_txt="otra", destino_data="clave")
    ruta.write_text(
        json.dumps(
            {
                buena.como_texto(): {"existia": True, "sha256": SHA_A, "observado_en": "2026-01-01T00:00:00Z"},
                mala.como_texto(): {"existia": True, "sha256": None, "observado_en": "2026-01-01T00:00:00Z"},
            }
        ),
        encoding="utf-8",
    )

    assert AlmacenJSON(ruta).leer(buena) is not None
    assert AlmacenJSON(ruta).leer(mala) is None


def test_el_almacen_por_defecto_vive_en_runtime_state_dir_y_no_depende_del_cwd(
    estado_durable_real: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """ADR 0011 P0.2: dos procesos con ``cwd`` distintos ven el mismo testigo."""
    hogar = tmp_path / "dot_sky_claw"
    cwd_a = tmp_path / "cwd_a"
    cwd_b = tmp_path / "cwd_b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    monkeypatch.setattr(Config, "DEFAULT_CONFIG_DIR", hogar)

    clave = _clave(tmp_path)
    monkeypatch.chdir(cwd_a)
    almacen_por_defecto().guardar(clave, TestigoPrevio(existia=True, sha256=SHA_A))

    monkeypatch.chdir(cwd_b)
    leido = almacen_por_defecto().leer(clave)
    assert leido is not None and leido.sha256 == SHA_A
    assert (hogar / "state" / NOMBRE_DEL_ARCHIVO).is_file()
    assert not (cwd_a / ".skyclaw_backups").exists()


def test_olvidar_borra_solo_esa_clave(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    almacen = AlmacenJSON(ruta)
    almacen.guardar(_clave(tmp_path, perfil="Default"), TestigoPrevio(existia=True, sha256=SHA_A))
    almacen.guardar(_clave(tmp_path, perfil="Otro"), TestigoPrevio(existia=True, sha256=SHA_B))

    almacen.olvidar(_clave(tmp_path, perfil="Default"))

    assert almacen.leer(_clave(tmp_path, perfil="Default")) is None
    assert almacen.leer(_clave(tmp_path, perfil="Otro")) is not None


def _durable(sha256: str | None, *, existia: bool = True) -> TestigoDurable:
    return TestigoDurable(existia=existia, sha256=sha256, observado_en="2026-11-04T10:00:00Z")


# =============================================================================
# 2. LA DECISIÓN — pura, sin disco
# =============================================================================


def test_sin_testigo_durable_la_primera_preparacion_persiste_y_no_afirma_nada() -> None:
    decision = decidir_testigo_de_preparacion(observado_ahora=TestigoPrevio(existia=False, sha256=None), durable=None)
    assert decision == DecisionDeTestigo(
        testigo=TestigoPrevio(existia=False, sha256=None),
        persistir=True,
        construido_despues=False,
        origen=OrigenDelTestigo.NINGUNO,
    )


def test_si_el_artefacto_no_cambio_la_preparacion_refresca_su_testigo() -> None:
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=True, sha256=SHA_A), durable=_durable(SHA_A)
    )
    assert decision.persistir is True
    assert decision.construido_despues is False
    assert decision.testigo == TestigoPrevio(existia=True, sha256=SHA_A)


def test_el_artefacto_que_aparecio_despues_del_testigo_durable_se_conserva() -> None:
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=True, sha256=SHA_B), durable=_durable(None, existia=False)
    )
    assert decision.persistir is False
    assert decision.construido_despues is True
    assert decision.testigo == TestigoPrevio(existia=False, sha256=None)
    assert type(decision.testigo) is TestigoPrevio
    assert decision.origen is OrigenDelTestigo.DURABLE


def test_el_artefacto_que_cambio_de_contenido_tambien_conserva_el_testigo_viejo() -> None:
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=True, sha256=SHA_B), durable=_durable(SHA_A)
    )
    assert decision.persistir is False
    assert decision.construido_despues is True
    assert decision.testigo == TestigoPrevio(existia=True, sha256=SHA_A)


def test_un_artefacto_borrado_no_es_un_build_y_no_se_anuncia_como_tal() -> None:
    decision = decidir_testigo_de_preparacion(
        observado_ahora=TestigoPrevio(existia=False, sha256=None), durable=_durable(SHA_A)
    )
    assert decision.persistir is True
    assert decision.construido_despues is False
    assert decision.testigo == TestigoPrevio(existia=False, sha256=None)


def test_sin_observacion_actual_el_testigo_durable_sigue_siendo_el_vigente() -> None:
    decision = decidir_testigo_de_preparacion(observado_ahora=None, durable=_durable(SHA_A))
    assert decision.persistir is False
    assert decision.construido_despues is False
    assert decision.testigo == TestigoPrevio(existia=True, sha256=SHA_A)
    assert decision.origen is OrigenDelTestigo.DURABLE


def test_sin_observacion_ni_durable_la_preparacion_no_afirma_testigo() -> None:
    decision = decidir_testigo_de_preparacion(observado_ahora=None, durable=None)
    assert decision == DecisionDeTestigo(
        testigo=None, persistir=False, construido_despues=False, origen=OrigenDelTestigo.NINGUNO
    )


# =============================================================================
# 3. EL SERVICIO — salto de capa en el overlay, precedencia, fallos y consumo
# =============================================================================


def _resolver_real(game: pathlib.Path, mo2: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = mo2
    resolver.get_mo2_mods_path_best_effort.return_value = mo2 / "mods"
    resolver.get_active_profile.return_value = "Default"
    resolver.get_skyrim_path_raw.return_value = str(game)
    resolver.get_mo2_path_raw.return_value = str(mo2)
    return resolver


def _preflight_verde() -> MagicMock:
    reporte = MagicMock()
    reporte.blocks_mutations = False
    reporte.status.value = "green"
    reporte.to_dict.return_value = {}
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    return preflight


def _escenario(
    tmp_path: pathlib.Path,
    *,
    con_almacen: bool = True,
) -> tuple[WryeBashPipelineService, pathlib.Path, pathlib.Path, pathlib.Path, AlmacenEnMemoria | None]:
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    almacen = AlmacenEnMemoria() if con_almacen else None

    servicio = WryeBashPipelineService(
        path_resolver=_resolver_real(game, tmp_path),
        preflight=_preflight_verde(),
        testigo_store=almacen,
    )
    return servicio, game, perfil, overwrite, almacen


async def test_salto_de_capa_data_a_overwrite_prueba_frescura_sin_payload(tmp_path: pathlib.Path) -> None:
    """En la primera corrida la fase 1 resuelve ``Data`` (no existe en ningún lado) y el
    build del operador aterriza en ``overwrite``. Si la clave dependiera de la capa
    ganadora, la fase 2 jamás encontraría el testigo de la primera corrida."""
    servicio, game, _perfil, overwrite, _almacen = _escenario(tmp_path)

    prep = await servicio.preparar_etapa_asistida(profile="Default")
    assert prep["artefacto"] == str(game / "Data" / BASHED_PATCH_NAME)
    assert prep["testigo_durable"] == "guardado"
    assert prep["construido_despues_de_la_preparacion"] is False

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x00" * 60)

    veredicto = await servicio.verificar_etapa_asistida(profile="Default")
    assert veredicto["artefacto"] == str(overwrite / BASHED_PATCH_NAME)
    assert veredicto["success"] is True
    assert veredicto["testigo_origen"] == "durable"
    assert veredicto["frescura_probada"] is True


async def test_sin_almacen_la_fase_1_lo_declara_en_vez_de_fingir_persistencia(tmp_path: pathlib.Path) -> None:
    servicio, _game, _perfil, _overwrite, _almacen = _escenario(tmp_path, con_almacen=False)

    resultado = await servicio.preparar_etapa_asistida(profile="Default")

    assert resultado["pendiente"] is True
    assert resultado["testigo_durable"] == "no_disponible"
    assert resultado["construido_despues_de_la_preparacion"] is False


async def test_artefacto_sin_identidad_distingue_sin_observacion_y_conservado(tmp_path: pathlib.Path) -> None:
    """Un directorio en el lugar del ``.esp`` no tiene sha256: ni se miente ``guardado`` ni
    se borra un testigo durable que ya existía."""
    servicio, _game, _perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    # Caso 1: sin testigo previo en almacén y artefacto no regular (directorio).
    (overwrite / BASHED_PATCH_NAME).mkdir()
    sin_previo = await servicio.preparar_etapa_asistida(profile="Default")
    assert sin_previo["testigo_previo"] is None
    assert sin_previo["testigo_durable"] == "sin_observacion"

    # Caso 2: había un testigo durable válido y ahora el artefacto no se puede identificar.
    (overwrite / BASHED_PATCH_NAME).rmdir()
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"viejo")
    sha_viejo = _sha256_de(overwrite / BASHED_PATCH_NAME)
    assert (await servicio.preparar_etapa_asistida(profile="Default"))["testigo_durable"] == "guardado"

    (overwrite / BASHED_PATCH_NAME).unlink()
    (overwrite / BASHED_PATCH_NAME).mkdir()
    conservado = await servicio.preparar_etapa_asistida(profile="Default")
    assert conservado["testigo_durable"] == "conservado_sin_observacion"
    assert conservado["testigo_previo"] == {"existia": True, "sha256": sha_viejo}


async def test_fallo_del_almacen_en_fase_1_no_bloquea_al_operador_y_se_declara(tmp_path: pathlib.Path) -> None:
    ruta_corrupta = tmp_path / NOMBRE_DEL_ARCHIVO
    ruta_corrupta.write_text("{corrupto", encoding="utf-8")
    servicio, _game, _perfil, _overwrite, _ = _escenario(tmp_path, con_almacen=False)
    servicio_con_corrupto = WryeBashPipelineService(
        path_resolver=servicio._path_resolver,  # noqa: SLF001
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(ruta_corrupta),
    )

    resultado = await servicio_con_corrupto.preparar_etapa_asistida(profile="Default")

    assert resultado["pendiente"] is True
    assert resultado["testigo_previo"] == {"existia": False, "sha256": None}
    assert resultado["testigo_durable"] == "error_de_almacen"
    assert ruta_corrupta.read_text(encoding="utf-8") == "{corrupto"


async def test_reapertura_en_frio_de_la_gui_conserva_el_testigo_y_verifica_frescura(
    tmp_path: pathlib.Path,
) -> None:
    """Flujo real de GUI cerrada entre fases: preparar → cerrar → construir → preparar en
    frío → CTA ``resume_action_from_result`` → verificar ⇒ ``frescura_probada=True``."""
    ruta_store = tmp_path / NOMBRE_DEL_ARCHIVO
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()

    # Proceso 1: el operador pide preparar y cierra la GUI.
    svc_1 = WryeBashPipelineService(
        path_resolver=_resolver_real(game, tmp_path),
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(ruta_store),
    )
    await svc_1.preparar_etapa_asistida(profile="Default")

    # Con la GUI cerrada, el operador construye el Bashed Patch en Wrye Bash dentro de MO2.
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x01" * 60)

    # Proceso 2: reabre la GUI (ReactiveStore vacío), pulsa Etapa 6 y luego el botón de verificar.
    svc_2 = WryeBashPipelineService(
        path_resolver=_resolver_real(game, tmp_path),
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(ruta_store),
    )
    segunda_prep = await svc_2.preparar_etapa_asistida(profile="Default")
    assert segunda_prep["testigo_durable"] == "conservado_por_build"
    assert segunda_prep["construido_despues_de_la_preparacion"] is True
    assert segunda_prep["testigo_previo"] == {"existia": False, "sha256": None}

    accion = resume_action_from_result("wrye_bash", segunda_prep)
    assert accion is not None
    veredicto = await svc_2.ejecutar_fase_asistida(profile="Default", **accion["payload"])
    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True


async def test_el_testigo_del_payload_gana_sobre_el_durable_y_igual_lo_consume(tmp_path: pathlib.Path) -> None:
    """Dos invariantes juntos:
    1. El payload confirmado en pantalla gana sobre el almacén (acá el durable tiene el
       mismo sha256 del parche nuevo, así que si se usara el durable daría ``SIN_CAMBIO``).
    2. Cuando el veredicto prueba frescura por ``payload``, el testigo durable TAMBIÉN se
       consume: dejarlo vivo haría que la próxima corrida viera un testigo obsoleto."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    await servicio.preparar_etapa_asistida(profile="Default")
    parche = overwrite / BASHED_PATCH_NAME
    parche.write_bytes(b"TES4" + b"\x02" * 60)
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )
    almacen.guardar(clave, TestigoPrevio(existia=True, sha256=_sha256_de(parche)))

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default", testigo_previo={"existia": False, "sha256": None}
    )

    assert veredicto["success"] is True
    assert veredicto["testigo_origen"] == "payload"
    assert veredicto["frescura_probada"] is True
    assert almacen.leer(clave) is None, "el testigo durable quedó huérfano tras probar frescura por payload"


async def test_payload_explicito_malformado_no_usa_el_durable_ni_lo_consume(tmp_path: pathlib.Path) -> None:
    """Un payload explícito corrupto no se disfraza con el almacén ni borra la evidencia."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x03" * 60)

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default", testigo_previo={"existia": "sí", "sha256": 42}
    )

    assert veredicto["success"] is True
    assert veredicto["testigo_origen"] == "ninguno"
    assert veredicto["frescura_probada"] is False
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )
    assert almacen.leer(clave) is not None


async def test_una_verificacion_fallida_no_consume_el_testigo_durable(tmp_path: pathlib.Path) -> None:
    """Caso típico del operador: construye el parche, olvida tildarlo en MO2 (``NO_HABILITADO``),
    lo tilda y vuelve a verificar. Consumir en el fallo perdería la frescura."""
    servicio, _game, perfil, overwrite, _almacen = _escenario(tmp_path)
    perfil.joinpath("plugins.txt").write_text(f"{BASHED_PATCH_NAME}\n", encoding="utf-8")
    await servicio.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x04" * 60)

    fallido = await servicio.verificar_etapa_asistida(profile="Default")
    assert fallido["success"] is False
    assert fallido["motivo"] == "NO_HABILITADO"
    assert fallido["testigo_origen"] == "durable"

    # El operador habilita el plugin en MO2 y reintenta la fase 2.
    perfil.joinpath("plugins.txt").write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    reintento = await servicio.verificar_etapa_asistida(profile="Default")
    assert reintento["success"] is True
    assert reintento["testigo_origen"] == "durable"
    assert reintento["frescura_probada"] is True


async def test_un_fallo_al_consumir_el_testigo_no_tumba_un_veredicto_valido(tmp_path: pathlib.Path) -> None:
    """La limpieza del almacén no es parte de la validez del parche."""

    class _AlmacenQueFallaAlOlvidar(AlmacenEnMemoria):
        def olvidar(self, clave: ClaveDeTestigo) -> None:
            raise OSError("disco lleno justo al limpiar")

    servicio, _game, _perfil, overwrite, _ = _escenario(tmp_path, con_almacen=False)
    svc = WryeBashPipelineService(
        path_resolver=servicio._path_resolver,  # noqa: SLF001
        preflight=_preflight_verde(),
        testigo_store=_AlmacenQueFallaAlOlvidar(),
    )
    await svc.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x05" * 60)

    veredicto = await svc.verificar_etapa_asistida(profile="Default")
    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True


# =============================================================================
# 4. CABLEADO DE PRODUCCIÓN — agente LLM entre sesiones + ancla AST
# =============================================================================


async def test_la_tool_del_agente_prueba_frescura_entre_dos_instancias_del_registry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El LLM sigue sin poder enviar ``testigo_previo`` (A-4 intacto) y aun así la fase 2
    prueba frescura en otra instancia de ``AsyncToolRegistry`` gracias al almacén durable."""
    monkeypatch.setattr(WryeBashPipelineService, "_ensure_preflight", lambda self, **kwargs: None)
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Requiem"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    resolver = _resolver_real(game, tmp_path)

    reg_sesion_1 = AsyncToolRegistry(
        registry=None, mo2=None, sync_engine=None, mo2_profile="Requiem", path_resolver=resolver
    )
    prep = json.loads(await reg_sesion_1.execute("generate_bashed_patch", {"fase": "preparar"}))
    assert prep["pendiente"] is True
    assert prep["testigo_durable"] == "guardado"

    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x06" * 60)

    reg_sesion_2 = AsyncToolRegistry(
        registry=None, mo2=None, sync_engine=None, mo2_profile="Requiem", path_resolver=resolver
    )
    veredicto = json.loads(await reg_sesion_2.execute("generate_bashed_patch", {"fase": "verificar"}))
    assert veredicto["success"] is True
    assert veredicto["testigo_origen"] == "durable"
    assert veredicto["frescura_probada"] is True


_SITIOS_CONSTRUCCION_PRODUCCION = frozenset(
    {
        "sky_claw/app/orchestrator/orchestration_composition.py",
        "sky_claw/app/agent/tools/system_tools.py",
    }
)


def _es_llamada_al_servicio(nodo: ast.Call) -> bool:
    nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
    if nombre == "WryeBashPipelineService":
        return True
    if nombre == "partial" and nodo.args:
        primero = getattr(nodo.args[0], "id", None) or getattr(nodo.args[0], "attr", None)
        return primero == "WryeBashPipelineService"
    return False


def test_toda_construccion_de_produccion_pasa_un_almacen_no_nulo() -> None:
    """Enumera TODO ``sky_claw/`` por igualdad y rechaza ``testigo_store=None``.

    Hallado en auto-auditoría adversarial: exigir sólo ``"testigo_store" in claves`` dejaba
    pasar ``WryeBashPipelineService(..., testigo_store=None)`` en verde. Acá se congela el
    conjunto exacto de sitios productivos y se exige que el argumento no sea ``None``.
    """
    ofensores: list[str] = []
    sitios: set[str] = set()
    for py in sorted(RAIZ.joinpath("sky_claw").rglob("*.py")):
        rel = py.relative_to(RAIZ).as_posix()
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not _es_llamada_al_servicio(nodo):
                continue
            sitios.add(rel)
            kw_map = {kw.arg: kw.value for kw in nodo.keywords if kw.arg is not None}
            if "testigo_store" not in kw_map:
                ofensores.append(f"{rel}:{nodo.lineno} no pasa testigo_store")
            elif isinstance(kw_map["testigo_store"], ast.Constant) and kw_map["testigo_store"].value is None:
                ofensores.append(f"{rel}:{nodo.lineno} pasa testigo_store=None")

    assert sitios == _SITIOS_CONSTRUCCION_PRODUCCION, (
        f"cambió el conjunto de sitios que construyen WryeBashPipelineService: {sorted(sitios)}"
    )
    assert ofensores == [], "construcciones sin almacén durable:\n" + "\n".join(ofensores)


def test_orchestration_composition_cablea_un_almacen_json_vivo() -> None:
    """Oráculo conductual hermano del ancla AST: el servicio del daemon nace con AlmacenJSON."""
    from sky_claw.app.orchestrator.orchestration_composition import build_orchestration_composition

    comp = build_orchestration_composition(
        scraper=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(),
        path_validator=MagicMock(),
        event_bus=MagicMock(),
        profile_name="Default",
        hitl_guard=MagicMock(),
        loot_runner=None,
        require_vfs=False,
        patch_advisor_llm=None,
        grass_runtime_deps_provider=MagicMock(),
        plugin_limit_guard=MagicMock(),
        scan_asset_conflicts=MagicMock(),
        scan_asset_conflicts_json=MagicMock(),
    )
    assert isinstance(comp.wrye_bash_service._testigo_store, AlmacenJSON)  # noqa: SLF001
