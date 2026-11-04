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
        {"existia": False, "sha256": None, "observado_en": "2026-11-04T10:00:00Z", "generacion": ""},
        {"existia": False, "sha256": None, "observado_en": "2026-11-04T10:00:00Z", "generacion": "A" * 32},
        {"existia": False, "sha256": None, "observado_en": "2026-11-04T10:00:00Z", "generacion": "a" * 31},
        {"existia": False, "sha256": None, "observado_en": "2026-11-04T10:00:00Z", "generacion": 123},
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

    nombres = {p.name for p in tmp_path.iterdir()}
    assert NOMBRE_DEL_ARCHIVO in nombres
    assert not any(n.endswith(".tmp") for n in nombres), f"quedaron temporales: {nombres}"


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="fsync sobre descriptor de directorio es contrato POSIX",
)
def test_guardar_sincroniza_tambien_el_directorio_padre_en_posix(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeRabbit: en POSIX ``os.replace`` necesita ``fsync`` del directorio contenedor tras
    el rename para que la entrada nueva sea durable ante corte eléctrico."""
    import os

    sincronizados: list[tuple[int, int]] = []
    fsync_real = os.fsync

    def _espia_fsync(fd: int) -> None:
        st = os.fstat(fd)
        sincronizados.append((st.st_dev, st.st_ino))
        fsync_real(fd)

    monkeypatch.setattr(os, "fsync", _espia_fsync)
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    AlmacenJSON(ruta).guardar(_clave(tmp_path), TestigoPrevio(existia=True, sha256=SHA_A))

    st_padre = tmp_path.stat()
    assert (st_padre.st_dev, st_padre.st_ino) in sincronizados, (
        "guardar sincronizó el temporal pero no el directorio padre tras os.replace"
    )


def test_dos_instancias_concurrentes_no_pierden_el_testigo_del_otro_perfil(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: ``orchestration_composition`` y ``system_tools`` usan instancias DISTINTAS de
    ``AlmacenJSON`` sobre el mismo archivo. Se aísla la capa intraproceso (incluso con rutas
    equivalentes ``sub/..``) desactivando el lock del SO (cubierto aparte por dos procesos):
    si el lock fuera de instancia en vez de por ruta canónica, ambas entrarían juntas a la
    barrera con ``{}`` y el segundo ``os.replace`` borraría al primer perfil."""
    import concurrent.futures
    import threading

    import sky_claw.local.tools.etapa6_testigos as mod_testigos

    (tmp_path / "sub").mkdir()
    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    ruta_equivalente = tmp_path / "sub" / ".." / NOMBRE_DEL_ARCHIVO
    instancia_gui = AlmacenJSON(ruta)
    instancia_llm = AlmacenJSON(ruta_equivalente)

    monkeypatch.setattr(mod_testigos, "_intentar_lock_so", lambda _fd: True)
    monkeypatch.setattr(mod_testigos, "_liberar_lock_so", lambda _fd: None)

    barrera = threading.Barrier(2, timeout=0.15)
    cargar_real = AlmacenJSON._cargar_objeto

    def _cargar_sincronizado(self: AlmacenJSON, r: pathlib.Path) -> dict[str, object]:
        datos = cargar_real(self, r)
        try:
            barrera.wait()
        except threading.BrokenBarrierError:
            barrera.reset()
        return datos

    monkeypatch.setattr(AlmacenJSON, "_cargar_objeto", _cargar_sincronizado)

    clave_a = _clave(tmp_path, perfil="Default")
    clave_b = _clave(tmp_path, perfil="Survival")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(instancia_gui.guardar, clave_a, TestigoPrevio(existia=True, sha256=SHA_A))
        f2 = pool.submit(instancia_llm.guardar, clave_b, TestigoPrevio(existia=True, sha256=SHA_B))
        f1.result(timeout=5.0)
        f2.result(timeout=5.0)

    lector = AlmacenJSON(ruta)
    leido_a = lector.leer(clave_a)
    leido_b = lector.leer(clave_b)
    assert leido_a is not None and leido_a.sha256 == SHA_A
    assert leido_b is not None and leido_b.sha256 == SHA_B


def test_dos_procesos_concurrentes_serializan_escrituras_sobre_el_mismo_archivo(tmp_path: pathlib.Path) -> None:
    """Exclusión cross-process real y determinista con dos intérpretes Python separados."""
    import subprocess
    import sys

    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    script = (
        "import pathlib, sys, time; "
        "from sky_claw.local.tools.etapa6_testigos import AlmacenJSON, clave_de_testigo; "
        "from sky_claw.local.tools.wrye_bash_asistida import TestigoPrevio; "
        "ruta = pathlib.Path(sys.argv[1]); perfil = sys.argv[2]; letra = sys.argv[3]; "
        "otro = sys.argv[4]; "
        "(ruta.parent / f'.ready_{perfil}').write_text('ok'); "
        "deadline = time.monotonic() + 5.0; "
        "exec('while not (ruta.parent / f\".ready_{otro}\").exists() and time.monotonic() < deadline: time.sleep(0.002)'); "
        "almacen = AlmacenJSON(ruta); "
        "[almacen.guardar("
        "clave_de_testigo(plugins_txt=ruta.parent / f'{perfil}_{i}' / 'plugins.txt', destino_data=ruta.parent / 'Data' / 'p.esp'), "
        "TestigoPrevio(existia=True, sha256=letra * 64)"
        ") for i in range(25)]"
    )
    p1 = subprocess.Popen([sys.executable, "-c", script, str(ruta), "GUI", "a", "LLM"], cwd=RAIZ)
    p2 = subprocess.Popen([sys.executable, "-c", script, str(ruta), "LLM", "b", "GUI"], cwd=RAIZ)
    assert p1.wait(timeout=15) == 0
    assert p2.wait(timeout=15) == 0

    lector = AlmacenJSON(ruta)
    for i in range(25):
        ca = clave_de_testigo(
            plugins_txt=tmp_path / f"GUI_{i}" / "plugins.txt", destino_data=tmp_path / "Data" / "p.esp"
        )
        cb = clave_de_testigo(
            plugins_txt=tmp_path / f"LLM_{i}" / "plugins.txt", destino_data=tmp_path / "Data" / "p.esp"
        )
        assert lector.leer(ca) is not None, f"se perdió GUI_{i} por carrera entre procesos"
        assert lector.leer(cb) is not None, f"se perdió LLM_{i} por carrera entre procesos"


def test_los_warnings_del_almacen_respetan_pipeline_stage_numerico_y_tx_id_none(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex P1 / SOP §5 regla 5: ``pipeline_stage`` es el entero ``6`` (acoplado al servicio)
    y lleva ``tx_id=None`` para no desaparecer de las consultas estructuradas de la etapa 6."""
    import logging

    import sky_claw.local.tools.etapa6_testigos as mod_testigos
    import sky_claw.local.tools.wrye_bash_service as mod_servicio

    assert mod_testigos._ETAPA_WRYE_BASH == mod_servicio._ETAPA_WRYE_BASH == 6  # noqa: SLF001

    # Warning 1: archivo JSON corrupto.
    ruta_corrupta = tmp_path / "corrupto.json"
    ruta_corrupta.write_text("{roto", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.etapa6_testigos"):
        assert AlmacenJSON(ruta_corrupta).leer(_clave(tmp_path)) is None

    # Warning 2: entrada individual inválida dentro de un objeto JSON válido.
    ruta_invalida = tmp_path / "invalida.json"
    mala = _clave(tmp_path, perfil="Malo")
    ruta_invalida.write_text(
        json.dumps({mala.como_texto(): {"existia": True, "sha256": None, "observado_en": "2026-11-04T00:00:00Z"}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.etapa6_testigos"):
        assert AlmacenJSON(ruta_invalida).leer(mala) is None

    registros = [r for r in caplog.records if r.name == "sky_claw.local.tools.etapa6_testigos"]
    assert len(registros) == 2
    for rec in registros:
        etapa = getattr(rec, "pipeline_stage", None)
        assert etapa == 6 and type(etapa) is int
        assert getattr(rec, "tx_id", "ausente") is None


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
def test_el_almacen_rechaza_un_enlace_en_la_ruta_del_archivo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    # Simula también Windows (donde ``os.O_NOFOLLOW`` no existe = 0) para que la defensa de
    # ``_rechazar_enlaces`` sobre el archivo y el lockfile muerda sin depender de POSIX.
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
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
    mala_gen = ClaveDeTestigo(plugins_txt="tercera", destino_data="clave")
    ruta.write_text(
        json.dumps(
            {
                buena.como_texto(): {
                    "existia": True,
                    "sha256": SHA_A,
                    "observado_en": "2026-01-01T00:00:00Z",
                    "generacion": "1" * 32,
                },
                mala.como_texto(): {
                    "existia": True,
                    "sha256": None,
                    "observado_en": "2026-01-01T00:00:00Z",
                    "generacion": "2" * 32,
                },
                mala_gen.como_texto(): {
                    "existia": False,
                    "sha256": None,
                    "observado_en": "2026-01-01T00:00:00Z",
                    "generacion": "GENERACION_INVALIDA",
                },
            }
        ),
        encoding="utf-8",
    )

    assert AlmacenJSON(ruta).leer(buena) is not None
    assert AlmacenJSON(ruta).leer(mala) is None
    assert AlmacenJSON(ruta).leer(mala_gen) is None


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
    guardado = await servicio.preparar_etapa_asistida(profile="Default")
    assert guardado["testigo_durable"] == "guardado"
    gen_original = guardado["testigo_previo"]["generacion"]
    assert isinstance(gen_original, str) and len(gen_original) == 32

    (overwrite / BASHED_PATCH_NAME).unlink()
    (overwrite / BASHED_PATCH_NAME).mkdir()
    conservado = await servicio.preparar_etapa_asistida(profile="Default")
    assert conservado["testigo_durable"] == "conservado_sin_observacion"
    assert conservado["testigo_previo"] == {"existia": True, "sha256": sha_viejo, "generacion": gen_original}


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


async def test_fallo_del_almacen_en_fase_1_con_artefacto_sin_identidad_declara_error_de_almacen(
    tmp_path: pathlib.Path,
) -> None:
    """Si el almacén JSON está corrupto, la fase 1 declara ``error_de_almacen`` incluso cuando
    el artefacto tampoco tiene identidad (directorio): no puede enmascararse como ``sin_observacion``."""
    ruta_corrupta = tmp_path / NOMBRE_DEL_ARCHIVO
    ruta_corrupta.write_text("{corrupto", encoding="utf-8")
    servicio, _game, _perfil, overwrite, _ = _escenario(tmp_path, con_almacen=False)
    (overwrite / BASHED_PATCH_NAME).mkdir()
    svc = WryeBashPipelineService(
        path_resolver=servicio._path_resolver,  # noqa: SLF001
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(ruta_corrupta),
    )

    resultado = await svc.preparar_etapa_asistida(profile="Default")

    assert resultado["pendiente"] is True
    assert resultado["testigo_previo"] is None
    assert resultado["testigo_durable"] == "error_de_almacen"
    assert ruta_corrupta.read_text(encoding="utf-8") == "{corrupto"


async def test_reapertura_en_frio_de_la_gui_conserva_el_testigo_y_verifica_frescura(
    tmp_path: pathlib.Path,
) -> None:
    """Flujo real de GUI cerrada entre fases: preparar → cerrar → construir → preparar en
    frío → CTA ``resume_action_from_result`` → verificar ⇒ ``frescura_probada=True`` y
    consumo limpio de esa generación."""
    ruta_store = tmp_path / NOMBRE_DEL_ARCHIVO
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    perfil.joinpath("plugins.txt").write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )

    # Proceso 1: el operador pide preparar y cierra la GUI.
    svc_1 = WryeBashPipelineService(
        path_resolver=_resolver_real(game, tmp_path),
        preflight=_preflight_verde(),
        testigo_store=AlmacenJSON(ruta_store),
    )
    primera_prep = await svc_1.preparar_etapa_asistida(profile="Default")
    gen_a = primera_prep["testigo_previo"]["generacion"]

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
    assert segunda_prep["testigo_previo"] == {"existia": False, "sha256": None, "generacion": gen_a}

    accion = resume_action_from_result("wrye_bash", segunda_prep)
    assert accion is not None
    veredicto = await svc_2.ejecutar_fase_asistida(profile="Default", **accion["payload"])
    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True
    assert AlmacenJSON(ruta_store).leer(clave) is None


async def test_verificar_por_payload_consume_el_testigo_durable_de_su_misma_generacion(
    tmp_path: pathlib.Path,
) -> None:
    """Cuando la verificación por ``payload`` corresponde a la preparación vigente, el
    testigo durable de esa misma generación se consume para no dejar evidencia huérfana."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    prep = await servicio.preparar_etapa_asistida(profile="Default")
    parche = overwrite / BASHED_PATCH_NAME
    parche.write_bytes(b"TES4" + b"\x02" * 60)
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )

    veredicto = await servicio.verificar_etapa_asistida(
        profile="Default",
        testigo_previo=prep["testigo_previo"],
    )

    assert veredicto["success"] is True
    assert veredicto["testigo_origen"] == "payload"
    assert veredicto["frescura_probada"] is True
    assert almacen.leer(clave) is None, "la misma generación debía consumirse al probar frescura"


async def test_payload_viejo_no_borra_el_testigo_durable_de_una_preparacion_posterior(
    tmp_path: pathlib.Path,
) -> None:
    """Review #5770094299 (P1): repetir la verificación A con su payload viejo (con o sin
    ``generacion``) NO puede borrar el testigo durable creado por una preparación B posterior.

    Secuencia AAA determinista:
    1. Preparar A cuando el Bashed Patch no existe.
    2. Crear el parche v1 y verificar A con su payload (consume A).
    3. Preparar B sobre el parche v1 existente, guardando ``sha_v1`` bajo una generación nueva.
    4. Repetir la verificación A con su payload antiguo (y también con dict crudo sin generación):
       ``payload > durable`` rige para evaluar la llamada explícita, pero NO borra el registro de B.
    5. Verificar que B conserva su testigo: sin nuevo build devuelve ``SIN_CAMBIO`` (fail-closed)
       y tras construir v2 prueba ``frescura_probada=True`` y recién ahí consume B."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )

    # 1. Preparar A sin parche.
    prep_a = await servicio.preparar_etapa_asistida(profile="Default")
    payload_a = prep_a["testigo_previo"]

    # 2. Construir v1 y verificar A con su payload -> consume A.
    parche = overwrite / BASHED_PATCH_NAME
    parche.write_bytes(b"TES4_V1_" + b"\x11" * 56)
    sha_v1 = _sha256_de(parche)
    ver_a1 = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=payload_a)
    assert ver_a1["success"] is True and ver_a1["frescura_probada"] is True
    assert almacen.leer(clave) is None

    # 3. Preparar B sobre el parche v1 actual -> almacena sha_v1 con nueva generación.
    prep_b = await servicio.preparar_etapa_asistida(profile="Default")
    assert prep_b["testigo_durable"] == "guardado"
    durable_b = almacen.leer(clave)
    assert durable_b is not None and durable_b.sha256 == sha_v1
    assert durable_b.generacion != payload_a["generacion"]

    # 4. Replay con payload viejo de A (con generación vieja y también sin generación).
    ver_replay_con_gen = await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=payload_a)
    assert ver_replay_con_gen["testigo_origen"] == "payload"
    ver_replay_sin_gen = await servicio.verificar_etapa_asistida(
        profile="Default",
        testigo_previo={"existia": False, "sha256": None},
    )
    assert ver_replay_sin_gen["testigo_origen"] == "payload"

    # 5. El testigo durable de B permanece intacto y protege el ciclo B.
    assert almacen.leer(clave) == durable_b
    sin_rebuild = await servicio.verificar_etapa_asistida(profile="Default")
    assert sin_rebuild["success"] is False
    assert sin_rebuild["motivo"] == "SIN_CAMBIO"

    parche.write_bytes(b"TES4_V2_" + b"\x22" * 56)
    con_rebuild = await servicio.verificar_etapa_asistida(profile="Default")
    assert con_rebuild["success"] is True
    assert con_rebuild["testigo_origen"] == "durable"
    assert con_rebuild["frescura_probada"] is True
    assert almacen.leer(clave) is None


async def test_payload_viejo_no_borra_nueva_generacion_aunque_coincida_la_huella_previa(
    tmp_path: pathlib.Path,
) -> None:
    """Mata el mutante que solo compara ``(existia, sha256)`` sin mirar ``generacion``:
    A y B observaron ambos ausencia (``existia=False, sha256=None``), pero son generaciones
    distintas y el replay de A no puede consumir el registro de B."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )
    parche = overwrite / BASHED_PATCH_NAME

    prep_a = await servicio.preparar_etapa_asistida(profile="Default")
    payload_a = prep_a["testigo_previo"]
    parche.write_bytes(b"TES4_V1_" + b"\x31" * 56)
    assert (await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=payload_a))[
        "frescura_probada"
    ] is True
    assert almacen.leer(clave) is None

    # El operador borra v1 y arranca una nueva preparación B: misma observación (ausente), otra generación.
    parche.unlink()
    prep_b = await servicio.preparar_etapa_asistida(profile="Default")
    durable_b = almacen.leer(clave)
    assert durable_b is not None and durable_b.existia is False
    assert prep_b["testigo_previo"]["generacion"] != payload_a["generacion"]

    # El operador construye v2; antes de que B verifique, llega un replay con payload_a.
    parche.write_bytes(b"TES4_V2_" + b"\x32" * 56)
    await servicio.verificar_etapa_asistida(profile="Default", testigo_previo=payload_a)

    # El testigo de B NO fue consumido por el replay de A: B verifica sin payload y prueba frescura.
    assert almacen.leer(clave) == durable_b
    ver_b = await servicio.verificar_etapa_asistida(profile="Default")
    assert ver_b["success"] is True
    assert ver_b["testigo_origen"] == "durable"
    assert ver_b["frescura_probada"] is True


async def test_multi_pestana_gui_cta_obsoleto_no_borra_preparacion_posterior(
    tmp_path: pathlib.Path,
) -> None:
    """Superficie GUI real (PATCH 0010): la pestaña 1 conserva en RAM el CTA derivado de A
    por ``resume_action_from_result``; si B ya preparó otro ciclo, accionar el CTA viejo de la
    pestaña 1 no borra el testigo durable de B."""
    servicio, game, perfil, overwrite, almacen = _escenario(tmp_path)
    assert almacen is not None
    clave = clave_de_testigo(
        plugins_txt=perfil / "plugins.txt",
        destino_data=game / "Data" / BASHED_PATCH_NAME,
    )
    parche = overwrite / BASHED_PATCH_NAME

    prep_pestana_1 = await servicio.preparar_etapa_asistida(profile="Default")
    accion_pestana_1 = resume_action_from_result("wrye_bash", prep_pestana_1)
    assert accion_pestana_1 is not None

    parche.write_bytes(b"TES4_V1_" + b"\x41" * 56)
    # Otra superficie (p. ej. agente LLM) verifica A y arranca preparación B.
    assert (await servicio.verificar_etapa_asistida(profile="Default"))["frescura_probada"] is True
    await servicio.preparar_etapa_asistida(profile="Default")
    durable_b = almacen.leer(clave)
    assert durable_b is not None

    # El usuario hace clic en el botón obsoleto que quedó abierto en la pestaña 1.
    await servicio.ejecutar_fase_asistida(profile="Default", **accion_pestana_1["payload"])
    assert almacen.leer(clave) == durable_b


def test_olvidar_si_coincide_en_origen_durable_no_borra_si_otra_preparacion_avanza_en_paralelo(
    tmp_path: pathlib.Path,
) -> None:
    """En ``origen == 'durable'``, si entre ``leer`` y ``olvidar_si_coincide`` otra preparación
    ya reemplazó el slot con una generación nueva, el consumo condicional respeta la nueva."""
    from sky_claw.local.tools.wrye_bash_service import _verificar_con_almacen

    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    destino_data = game / "Data" / BASHED_PATCH_NAME
    plugins_txt = tmp_path / "profiles" / "Default" / "plugins.txt"
    plugins_txt.parent.mkdir(parents=True)
    plugins_txt.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    parche = tmp_path / "overwrite" / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True)
    parche.write_bytes(b"TES4_V1_" + b"\x51" * 56)

    clave = clave_de_testigo(plugins_txt=plugins_txt, destino_data=destino_data)
    almacen = AlmacenJSON(ruta)
    almacen.guardar(clave, TestigoPrevio(existia=False, sha256=None))

    leer_real = almacen.leer
    reemplazado: list[TestigoDurable] = []

    def _leer_y_avanzar(c: ClaveDeTestigo) -> TestigoDurable | None:
        leido = leer_real(c)
        nuevo = AlmacenJSON(ruta).guardar(c, TestigoPrevio(existia=True, sha256=_sha256_de(parche)))
        reemplazado.append(nuevo)
        return leido

    almacen.leer = _leer_y_avanzar  # type: ignore[method-assign]
    veredicto, origen = _verificar_con_almacen(
        artefacto=parche,
        plugins_txt=plugins_txt,
        destino_data=destino_data,
        testigo_previo=None,
        almacen=almacen,
    )
    assert veredicto.frescura_probada is True
    assert origen is OrigenDelTestigo.DURABLE
    assert AlmacenJSON(ruta).leer(clave) == reemplazado[0]


@pytest.mark.parametrize(
    ("crudo", "es_valido"),
    [
        ({"existia": False, "sha256": None}, True),
        ({"existia": True, "sha256": SHA_A}, True),
        ({"existia": False, "sha256": None, "generacion": "0123456789abcdef" * 2}, True),
        ({"existia": True, "sha256": SHA_A, "generacion": "f" * 32}, True),
        ({"existia": False}, False),
        ({"existia": False, "sha256": None, "generacion": None}, False),
        ({"existia": False, "sha256": None, "generacion": ""}, False),
        ({"existia": False, "sha256": None, "generacion": "A" * 32}, False),
        ({"existia": False, "sha256": None, "generacion": "a" * 31}, False),
        ({"existia": False, "sha256": None, "generacion": 42}, False),
        ({"existia": False, "sha256": None, "generation": "a" * 32}, False),
        ({"existia": True, "sha256": None}, False),
        ({"existia": False, "sha256": SHA_A}, False),
    ],
)
def test_gui_y_servicio_comparten_el_mismo_parser_de_testigo_de_payload(
    crudo: dict[str, object],
    es_valido: bool,
) -> None:
    """S-10 / AGENTS.md (hermanos): ``ritual_runner._testigo_utilizable`` y
    ``wrye_bash_service._testigo_desde_resultado`` aceptan y rechazan exactamente las mismas formas."""
    from sky_claw.app.gui.controllers.ritual_runner import _testigo_utilizable
    from sky_claw.local.tools.wrye_bash_service import _testigo_desde_resultado

    desde_gui = _testigo_utilizable(crudo)
    desde_svc = _testigo_desde_resultado(crudo)
    assert (desde_gui is not None) is es_valido
    assert (desde_svc is not None) is es_valido
    if es_valido:
        assert desde_gui == crudo


@pytest.mark.parametrize(
    "fabrica_almacen",
    [
        pytest.param(lambda _p: AlmacenEnMemoria(), id="memoria"),
        pytest.param(lambda p: AlmacenJSON(p / NOMBRE_DEL_ARCHIVO), id="json"),
    ],
)
def test_dos_preparaciones_concurrentes_desde_almacen_vacio_detectan_aparicion_intermedia(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fabrica_almacen: object,
) -> None:
    """Review #5770094299 (P2, variante almacén vacío + paridad memoria/JSON): si ``f1`` entra con
    almacén vacío y sin parche, y mientras decide aparece ``v1`` y entra ``f2``, ``f2`` debe
    esperar a que ``f1`` cierre su transacción y entonces detectar ``construido_desde_testigo_durable=True``
    (estado ``CONSERVADO``) en **ambos** hermanos (``AlmacenEnMemoria`` y ``AlmacenJSON``)."""
    import concurrent.futures
    import threading

    import sky_claw.local.tools.etapa6_testigos as mod_testigos
    import sky_claw.local.tools.wrye_bash_service as mod_servicio
    from sky_claw.local.tools.wrye_bash_asistida import decidir_testigo_de_preparacion as decidir_real
    from sky_claw.local.tools.wrye_bash_service import _preparar_con_almacen

    almacen = fabrica_almacen(tmp_path)  # type: ignore[operator]
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    destino_data = game / "Data" / BASHED_PATCH_NAME
    plugins_txt = tmp_path / "profiles" / "Default" / "plugins.txt"
    plugins_txt.parent.mkdir(parents=True)
    plugins_txt.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    parche = tmp_path / "overwrite" / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True)

    clave = clave_de_testigo(plugins_txt=plugins_txt, destino_data=destino_data)
    en_decision_1 = threading.Event()
    competidor_termino = threading.Event()
    primera_vez = True
    lock_flag = threading.Lock()

    def _decidir_espiado(*, observado_ahora: TestigoPrevio | None, durable: object) -> DecisionDeTestigo:
        nonlocal primera_vez
        decision = decidir_real(observado_ahora=observado_ahora, durable=durable)  # type: ignore[arg-type]
        es_primera = False
        with lock_flag:
            if primera_vez:
                primera_vez = False
                es_primera = True
        if es_primera:
            en_decision_1.set()
            competidor_termino.wait(timeout=0.15)
        return decision

    monkeypatch.setattr(mod_servicio, "decidir_testigo_de_preparacion", _decidir_espiado, raising=False)
    monkeypatch.setattr(mod_testigos, "decidir_testigo_de_preparacion", _decidir_espiado, raising=False)

    def _correr_f2() -> tuple[object, object, bool]:
        try:
            return _preparar_con_almacen(
                artefacto=parche,
                plugins_txt=plugins_txt,
                destino_data=destino_data,
                almacen=almacen,
            )
        finally:
            competidor_termino.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            _preparar_con_almacen,
            artefacto=destino_data,
            plugins_txt=plugins_txt,
            destino_data=destino_data,
            almacen=almacen,
        )
        assert en_decision_1.wait(timeout=5.0)
        parche.write_bytes(b"TES4_V1_" + b"\x65" * 56)
        f2 = pool.submit(_correr_f2)
        f1.result(timeout=5.0)
        prep_2, estado_2, construido_2 = f2.result(timeout=5.0)

    final = almacen.leer(clave)
    assert construido_2 is True
    assert getattr(estado_2, "value", None) == "conservado_por_build"
    assert getattr(prep_2, "testigo_previo", None) == TestigoPrevio(existia=False, sha256=None)
    assert final is not None and final.existia is False


@pytest.mark.parametrize(
    "fabrica_almacen",
    [
        pytest.param(lambda _p: AlmacenEnMemoria(), id="memoria"),
        pytest.param(lambda p: AlmacenJSON(p / NOMBRE_DEL_ARCHIVO), id="json"),
    ],
)
def test_refresco_idempotente_conserva_generacion_pero_nuevo_ciclo_rota_generacion(
    tmp_path: pathlib.Path,
    fabrica_almacen: object,
) -> None:
    """Paridad ``AlmacenEnMemoria`` / ``AlmacenJSON``: refrescar el mismo testigo sin cambios
    conserva la época (``generacion``) para no dejar testigos zombis si el cliente usa el primer
    payload; consumir o cambiar de huella base rota ``generacion``."""
    almacen = fabrica_almacen(tmp_path)  # type: ignore[operator]
    clave = clave_de_testigo(
        plugins_txt=tmp_path / "Default" / "plugins.txt",
        destino_data=tmp_path / "Data" / BASHED_PATCH_NAME,
    )

    r1 = almacen.sincronizar_preparacion(
        clave,
        lambda: TestigoPrevio(existia=False, sha256=None),
        observado_en="2026-11-04T10:00:00Z",
    )
    r2 = almacen.sincronizar_preparacion(
        clave,
        lambda: TestigoPrevio(existia=False, sha256=None),
        observado_en="2026-11-04T10:05:00Z",
    )
    assert r1.durable is not None and r2.durable is not None
    assert r2.durable.generacion == r1.durable.generacion
    assert r2.durable.observado_en == "2026-11-04T10:05:00Z"

    # Cuando el parche aparece sin haberse verificado aún, CONSERVADO también preserva la época.
    r3 = almacen.sincronizar_preparacion(
        clave,
        lambda: TestigoPrevio(existia=True, sha256="a" * 64),
        observado_en="2026-11-04T10:10:00Z",
    )
    assert r3.decision.construido_despues is True
    assert r3.durable == r2.durable

    # Consumir con la generación de r1 vacía el slot; la siguiente preparación rota generación.
    assert (
        almacen.olvidar_si_coincide(
            clave,
            testigo=TestigoPrevio(existia=False, sha256=None),
            generacion=r1.durable.generacion,
        )
        is True
    )
    assert almacen.leer(clave) is None

    r4 = almacen.sincronizar_preparacion(
        clave,
        lambda: TestigoPrevio(existia=False, sha256=None),
        observado_en="2026-11-04T10:15:00Z",
    )
    assert r4.durable is not None
    assert r4.durable.generacion != r1.durable.generacion


def test_dos_preparaciones_concurrentes_del_mismo_slot_son_atomicas_y_no_invierten_decisiones(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #5770094299 (P2): ``observar → leer → decidir → guardar`` sobre el mismo slot es
    atómico. Una preparación que ya decidió sobre ``v0`` no puede quedar sin lock antes de
    persistir, dejando que otra preparación posterior escriba ``existia=False`` y luego sea
    pisada en orden inverso por el ``v0`` viejo."""
    import concurrent.futures
    import threading

    import sky_claw.local.tools.etapa6_testigos as mod_testigos
    import sky_claw.local.tools.wrye_bash_service as mod_servicio
    from sky_claw.local.tools.wrye_bash_asistida import decidir_testigo_de_preparacion as decidir_real
    from sky_claw.local.tools.wrye_bash_service import _preparar_con_almacen

    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    destino_data = game / "Data" / BASHED_PATCH_NAME
    plugins_txt = tmp_path / "profiles" / "Default" / "plugins.txt"
    plugins_txt.parent.mkdir(parents=True)
    plugins_txt.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    parche = tmp_path / "overwrite" / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True)
    parche.write_bytes(b"TES4_V0_" + b"\x60" * 56)
    sha_v0 = _sha256_de(parche)

    clave = clave_de_testigo(plugins_txt=plugins_txt, destino_data=destino_data)
    AlmacenJSON(ruta).guardar(clave, TestigoPrevio(existia=True, sha256=sha_v0))

    en_decision_1 = threading.Event()
    competidor_termino = threading.Event()
    primera_vez = True
    lock_flag = threading.Lock()

    def _decidir_espiado(*, observado_ahora: TestigoPrevio | None, durable: object) -> DecisionDeTestigo:
        nonlocal primera_vez
        decision = decidir_real(observado_ahora=observado_ahora, durable=durable)  # type: ignore[arg-type]
        es_primera = False
        with lock_flag:
            if primera_vez:
                primera_vez = False
                es_primera = True
        if es_primera:
            en_decision_1.set()
            # Sin atomicidad de transacción, f2 entra sin bloqueo, persiste existia=False y
            # despierta a f1 en <5 ms para que pise existia=False con sha_v0. Con transacción
            # atómica, f2 queda bloqueada esperando el lock hasta que este timeout vence.
            competidor_termino.wait(timeout=0.15)
        return decision

    monkeypatch.setattr(mod_servicio, "decidir_testigo_de_preparacion", _decidir_espiado, raising=False)
    monkeypatch.setattr(mod_testigos, "decidir_testigo_de_preparacion", _decidir_espiado, raising=False)

    def _correr_f2() -> tuple[object, object, bool]:
        try:
            return _preparar_con_almacen(
                artefacto=destino_data,
                plugins_txt=plugins_txt,
                destino_data=destino_data,
                almacen=AlmacenJSON(ruta),
            )
        finally:
            competidor_termino.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            _preparar_con_almacen,
            artefacto=parche,
            plugins_txt=plugins_txt,
            destino_data=destino_data,
            almacen=AlmacenJSON(ruta),
        )
        assert en_decision_1.wait(timeout=5.0)
        parche.unlink()
        f2 = pool.submit(_correr_f2)
        f1.result(timeout=5.0)
        prep_2, estado_2, _ = f2.result(timeout=5.0)

    final = AlmacenJSON(ruta).leer(clave)
    assert getattr(estado_2, "value", None) == "guardado"
    assert getattr(prep_2, "testigo_previo", None) == TestigoPrevio(existia=False, sha256=None)
    assert final is not None and final.existia is False and final.sha256 is None


def test_preparar_y_verificar_concurrentes_del_mismo_slot_no_resucitan_un_testigo_consumido(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #5770094299 (P2): ``_preparar_con_almacen`` y ``_verificar_con_almacen`` no pueden
    intercalarse entre ``leer`` y ``guardar`` dejando resucitado un testigo ya consumido (que
    daría ``frescura_probada=True`` falso en una corrida posterior sin nuevo build)."""
    import concurrent.futures
    import threading

    import sky_claw.local.tools.etapa6_testigos as mod_testigos
    import sky_claw.local.tools.wrye_bash_service as mod_servicio
    from sky_claw.local.tools.wrye_bash_asistida import decidir_testigo_de_preparacion as decidir_real
    from sky_claw.local.tools.wrye_bash_service import _preparar_con_almacen, _verificar_con_almacen

    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    destino_data = game / "Data" / BASHED_PATCH_NAME
    plugins_txt = tmp_path / "profiles" / "Default" / "plugins.txt"
    plugins_txt.parent.mkdir(parents=True)
    plugins_txt.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    parche = tmp_path / "overwrite" / BASHED_PATCH_NAME
    parche.parent.mkdir(parents=True)

    clave = clave_de_testigo(plugins_txt=plugins_txt, destino_data=destino_data)
    AlmacenJSON(ruta).guardar(clave, TestigoPrevio(existia=False, sha256=None))

    en_decision = threading.Event()
    verificacion_termino = threading.Event()

    def _decidir_pausado(*, observado_ahora: TestigoPrevio | None, durable: object) -> DecisionDeTestigo:
        decision = decidir_real(observado_ahora=observado_ahora, durable=durable)  # type: ignore[arg-type]
        en_decision.set()
        verificacion_termino.wait(timeout=0.15)
        return decision

    monkeypatch.setattr(mod_servicio, "decidir_testigo_de_preparacion", _decidir_pausado, raising=False)
    monkeypatch.setattr(mod_testigos, "decidir_testigo_de_preparacion", _decidir_pausado, raising=False)

    def _correr_verificar() -> tuple[object, object]:
        try:
            return _verificar_con_almacen(
                artefacto=parche,
                plugins_txt=plugins_txt,
                destino_data=destino_data,
                testigo_previo=None,
                almacen=AlmacenJSON(ruta),
            )
        finally:
            verificacion_termino.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_prep = pool.submit(
            _preparar_con_almacen,
            artefacto=destino_data,
            plugins_txt=plugins_txt,
            destino_data=destino_data,
            almacen=AlmacenJSON(ruta),
        )
        assert en_decision.wait(timeout=5.0)
        parche.write_bytes(b"TES4_V1_" + b"\x70" * 56)
        fut_ver = pool.submit(_correr_verificar)
        fut_prep.result(timeout=5.0)
        ver_1, _ = fut_ver.result(timeout=5.0)

    assert getattr(ver_1, "frescura_probada", None) is True
    assert AlmacenJSON(ruta).leer(clave) is None

    # Una verificación posterior SIN nuevo build ya NO puede ver un testigo zombi ni probar frescura.
    ver_2, _ = _verificar_con_almacen(
        artefacto=parche,
        plugins_txt=plugins_txt,
        destino_data=destino_data,
        testigo_previo=None,
        almacen=AlmacenJSON(ruta),
    )
    assert ver_2.frescura_probada is False


def test_dos_procesos_concurrentes_en_el_mismo_slot_no_corrompen_el_almacen(tmp_path: pathlib.Path) -> None:
    """Exclusión cross-process sobre el MISMO slot usando ``sincronizar_preparacion`` y
    ``olvidar_si_coincide`` simultáneamente desde dos procesos Python independientes."""
    import subprocess
    import sys

    ruta = tmp_path / NOMBRE_DEL_ARCHIVO
    script = (
        "import pathlib, sys, time; "
        "from sky_claw.local.tools.etapa6_testigos import AlmacenJSON, clave_de_testigo; "
        "from sky_claw.local.tools.wrye_bash_asistida import TestigoPrevio; "
        "ruta = pathlib.Path(sys.argv[1]); rol = sys.argv[2]; otro = sys.argv[3]; "
        "clave = clave_de_testigo(plugins_txt=ruta.parent / 'Default' / 'plugins.txt', "
        "destino_data=ruta.parent / 'Data' / 'p.esp'); "
        "(ruta.parent / f'.ready_slot_{rol}').write_text('ok'); "
        "deadline = time.monotonic() + 5.0; "
        "exec('while not (ruta.parent / f\".ready_slot_{otro}\").exists() and time.monotonic() < deadline: time.sleep(0.002)'); "
        "almacen = AlmacenJSON(ruta); "
        "exec("
        "'for i in range(25):\\n"
        '  res = almacen.sincronizar_preparacion(clave, lambda i=i: TestigoPrevio(existia=True, sha256=(f"{i:02x}" * 32)))\\n'
        '  if rol == "CONSUMIDOR" and res.durable is not None:\\n'
        "    almacen.olvidar_si_coincide(clave, testigo=res.durable.como_testigo_previo(), generacion=res.durable.generacion)\\n'"
        ")"
    )
    p1 = subprocess.Popen([sys.executable, "-c", script, str(ruta), "PRODUCTOR", "CONSUMIDOR"], cwd=RAIZ)
    p2 = subprocess.Popen([sys.executable, "-c", script, str(ruta), "CONSUMIDOR", "PRODUCTOR"], cwd=RAIZ)
    try:
        rc1 = p1.wait(timeout=15)
        rc2 = p2.wait(timeout=15)
    finally:
        for proc in (p1, p2):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    assert rc1 == 0 and rc2 == 0

    # El archivo resultante debe seguir siendo JSON íntegro (nunca truncado ni corrupto) y legible.
    crudo = json.loads(ruta.read_text(encoding="utf-8"))
    assert isinstance(crudo, dict)
    clave = clave_de_testigo(
        plugins_txt=tmp_path / "Default" / "plugins.txt",
        destino_data=tmp_path / "Data" / "p.esp",
    )
    leido = AlmacenJSON(ruta).leer(clave)
    assert leido is None or len(leido.generacion) == 32


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


async def test_un_fallo_al_consumir_el_testigo_no_tumba_un_veredicto_valido(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La limpieza del almacén no es parte de la validez del parche, y su fallo registra stage 6."""
    import logging

    class _AlmacenQueFallaAlOlvidar(AlmacenEnMemoria):
        def olvidar_si_coincide(
            self,
            clave: ClaveDeTestigo,
            *,
            testigo: TestigoPrevio,
            generacion: str | None = None,
        ) -> bool:
            raise OSError("disco lleno justo al limpiar")

    servicio, _game, _perfil, overwrite, _ = _escenario(tmp_path, con_almacen=False)
    svc = WryeBashPipelineService(
        path_resolver=servicio._path_resolver,  # noqa: SLF001
        preflight=_preflight_verde(),
        testigo_store=_AlmacenQueFallaAlOlvidar(),
    )
    await svc.preparar_etapa_asistida(profile="Default")
    (overwrite / BASHED_PATCH_NAME).write_bytes(b"TES4" + b"\x05" * 60)

    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.wrye_bash_service"):
        veredicto = await svc.verificar_etapa_asistida(profile="Default")
    assert veredicto["success"] is True
    assert veredicto["frescura_probada"] is True
    registros = [r for r in caplog.records if "limpiar el testigo durable" in r.getMessage()]
    assert len(registros) == 1
    assert getattr(registros[0], "pipeline_stage", None) == 6
    assert getattr(registros[0], "tx_id", "ausente") is None


def test_produccion_solo_muta_el_almacen_con_operaciones_atomicas_y_condicionales() -> None:
    """Ancla AST (sin acoplamiento a números de línea): ningún caller de producción fuera de
    ``etapa6_testigos.py`` puede invocar ``.guardar(`` ni ``.olvidar(`` incondicional sobre el
    almacén; ``wrye_bash_service.py`` sólo muta por ``sincronizar_preparacion`` y
    ``olvidar_si_coincide``."""
    almacen_rel = "sky_claw/local/tools/etapa6_testigos.py"
    servicio_rel = "sky_claw/local/tools/wrye_bash_service.py"
    prohibidas: list[str] = []
    mutaciones_en_servicio: dict[str, set[str]] = {}

    for py in sorted(RAIZ.joinpath("sky_claw").rglob("*.py")):
        rel = py.relative_to(RAIZ).as_posix()
        if rel == almacen_rel:
            continue
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for cont in ast.walk(arbol):
            if not isinstance(cont, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for nodo in ast.walk(cont):
                if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
                    continue
                metodo = nodo.func.attr
                receptor = getattr(nodo.func.value, "id", None) or getattr(nodo.func.value, "attr", None)
                if receptor in {"almacen", "_testigo_store", "testigo_store"} and metodo in {"guardar", "olvidar"}:
                    prohibidas.append(f"{rel}:{cont.name}:{nodo.lineno} llama .{metodo}()")
                if rel == servicio_rel and metodo in {
                    "sincronizar_preparacion",
                    "olvidar_si_coincide",
                    "guardar",
                    "olvidar",
                }:
                    mutaciones_en_servicio.setdefault(cont.name, set()).add(metodo)

    assert prohibidas == [], (
        "producción volvió a usar primitivas no atómicas/incondicionales del almacén:\n" + "\n".join(prohibidas)
    )
    assert mutaciones_en_servicio == {
        "_preparar_con_almacen": {"sincronizar_preparacion"},
        "_verificar_con_almacen": {"olvidar_si_coincide"},
    }


def test_gui_y_servicio_delegan_en_parsear_testigo_de_payload() -> None:
    """Ancla AST contra S-10: ni ``ritual_runner._testigo_utilizable`` ni
    ``wrye_bash_service._testigo_desde_resultado`` pueden reimplementar la validación por
    separado; ambos deben delegar en ``parsear_testigo_de_payload``."""
    objetivos = {
        "sky_claw/app/gui/controllers/ritual_runner.py": "_testigo_utilizable",
        "sky_claw/local/tools/wrye_bash_service.py": "_testigo_desde_resultado",
    }
    for rel, nombre_fn in objetivos.items():
        arbol = ast.parse((RAIZ / rel).read_text(encoding="utf-8"))
        fn = next(
            (n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == nombre_fn),
            None,
        )
        assert fn is not None, f"desapareció {nombre_fn} en {rel}"
        llamadas = {
            nodo.func.id for nodo in ast.walk(fn) if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
        }
        assert "parsear_testigo_de_payload" in llamadas, (
            f"{rel}:{nombre_fn} dejó de delegar en parsear_testigo_de_payload (riesgo S-10)"
        )


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
