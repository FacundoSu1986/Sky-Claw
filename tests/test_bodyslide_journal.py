"""BodySlide PR 2b — journal durable de la config y recuperación fail-closed.

Brief: ``docs/design/specs/2026-11-23-bodyslide-pr2-concepts-brief.md`` (D5, §13 P-4 y C1).

Propiedad que se ancla: *la configuración del usuario sólo se reescribe cuando el
journal prueba que el contenido actual es nuestro y que nadie más puede estar
escribiéndolo*. Todo lo que no se puede probar es HOLD y se preserva.

Los casos de crash corren en un PROCESO REAL que muere con ``os._exit`` en un punto
exacto: lo que se verifica es el disco que deja, no una excepción de Python.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

import psutil
import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools import bodyslide_journal as bj
from sky_claw.local.tools.bodyslide_journal import (
    CLAVES_DE_ARCHIVO,
    CLAVES_DEL_JOURNAL,
    TRANSICIONES,
    AlmacenDeJournal,
    EstadoDelJournal,
    IdentidadDeEjecucion,
    IdentidadDeProceso,
    JournalOcupadoError,
    ModificacionConcurrenteError,
    PrecondicionDelJournalError,
    TransicionNoAutorizadaError,
    Veredicto,
    identidad_de_instancia,
    identidad_de_maquina,
    iniciar_transaccion,
    recuperar_journal,
)
from sky_claw.local.tools.output_targets import BODYSLIDE_MESHES_RESOURCE_ID
from sky_claw.local.tools.rollback_reconciler import (
    PRODUCTORES_CABLEADOS,
    construir_productores_de_move_aside,
    reconcile_bodyslide_config_journals,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]

ORIGINAL = b"<Config><TargetGame>4</TargetGame></Config>"
SEMBRADO = b"<Config><TargetGame>4</TargetGame><GameDataPath>C:/juego/Data</GameDataPath></Config>"
APP_ORIGINAL = b"<BodySlideConfig><SelectedPreset>Viejo</SelectedPreset></BodySlideConfig>"
APP_DE_LA_CORRIDA = b"<BodySlideConfig><SelectedPreset>Nuevo</SelectedPreset></BodySlideConfig>"
MAQUINA = "maquina-de-prueba"


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _sin_endurecer(_ruta: pathlib.Path) -> None:
    """Los tests de lógica no pagan ``icacls`` por directorio; el endurecimiento real se ancla aparte."""


@pytest.fixture
def exe_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Carpeta descartable de BodySlide: Config.xml y BodySlide.xml presentes, BuildSelection.xml ausente."""
    directorio = tmp_path / "BodySlide"
    directorio.mkdir()
    (directorio / "Config.xml").write_bytes(ORIGINAL)
    (directorio / "BodySlide.xml").write_bytes(APP_ORIGINAL)
    return directorio


@pytest.fixture
def almacen(tmp_path: pathlib.Path) -> AlmacenDeJournal:
    return AlmacenDeJournal(tmp_path / "state" / "bodyslide_journal", endurecer=_sin_endurecer)


@pytest.fixture(scope="module")
def duenio_muerto() -> IdentidadDeProceso:
    """Identidad (pid + creación) de un proceso real que ya terminó."""
    proceso = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])  # noqa: S603 - argv fijo
    creado = psutil.Process(proceso.pid).create_time()
    proceso.wait(timeout=30)
    return IdentidadDeProceso(pid=proceso.pid, creado=creado)


def _identidad(exe_dir: pathlib.Path, maquina: str = MAQUINA) -> IdentidadDeEjecucion:
    return IdentidadDeEjecucion(maquina=maquina, instancia=identidad_de_instancia(exe_dir))


def _iniciar(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio: IdentidadDeProceso | None = None
) -> bj.TransaccionDeConfig:
    return iniciar_transaccion(
        almacen=almacen,
        identidad=_identidad(exe_dir),
        exe_dir=exe_dir,
        bytes_sembrados=SEMBRADO,
        duenio=duenio,
    )


_POR_DEFECTO = object()


def _recuperar(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    *,
    identidad: Any = _POR_DEFECTO,
) -> bj.InformeDeRecuperacion:
    actual = _identidad(exe_dir) if identidad is _POR_DEFECTO else identidad
    return recuperar_journal(
        almacen.ruta_de(identidad_de_instancia(exe_dir)),
        almacen=almacen,
        identidad_actual=actual,
        exe_dir=exe_dir,
    )


def _documento_en_disco(ruta: pathlib.Path) -> dict[str, Any]:
    datos: dict[str, Any] = json.loads(ruta.read_text(encoding="utf-8"))
    return datos


# ---------------------------------------------------------------------------
# Anclas: vocabulario, transiciones y schema congelados por igualdad literal
# ---------------------------------------------------------------------------


def test_estados_del_journal_congelados() -> None:
    assert {e.value for e in EstadoDelJournal} == {
        "PREPARADO",
        "SIEMBRA_INDETERMINADA",
        "SEMBRADO",
        "LANZADO",
        "TERMINAL",
        "RESTAURACION_FALLIDA",
        "RESTAURADO",
    }


def test_transiciones_congeladas_por_igualdad_literal() -> None:
    """LANZADO sólo sale a TERMINAL: sin terminalidad registrada no hay restauración posible."""
    e = EstadoDelJournal
    assert dict(TRANSICIONES) == {
        e.PREPARADO: frozenset({e.SEMBRADO, e.SIEMBRA_INDETERMINADA, e.RESTAURADO, e.RESTAURACION_FALLIDA}),
        e.SIEMBRA_INDETERMINADA: frozenset({e.SEMBRADO, e.RESTAURADO, e.RESTAURACION_FALLIDA}),
        e.SEMBRADO: frozenset({e.LANZADO, e.RESTAURADO, e.RESTAURACION_FALLIDA}),
        e.LANZADO: frozenset({e.TERMINAL}),
        e.TERMINAL: frozenset({e.RESTAURADO, e.RESTAURACION_FALLIDA}),
        e.RESTAURACION_FALLIDA: frozenset({e.RESTAURADO, e.RESTAURACION_FALLIDA}),
        e.RESTAURADO: frozenset(),
    }


def test_claves_del_schema_congeladas() -> None:
    assert (
        frozenset(
            {
                "schema_version",
                "run_id",
                "generacion",
                "estado",
                "maquina",
                "instancia",
                "directorio",
                "directorio_volumen",
                "directorio_indice",
                "duenio_pid",
                "duenio_creado",
                "arranque_del_sistema",
                "archivos",
                "integridad",
            }
        )
        == CLAVES_DEL_JOURNAL
    )
    assert frozenset({"nombre", "original_b64", "original_sha256", "esperados_sha256"}) == CLAVES_DE_ARCHIVO


def test_veredictos_congelados() -> None:
    assert {v.value for v in Veredicto} == {
        "RESTAURADO",
        "SIN_CAMBIOS",
        "SIN_JOURNAL",
        "CONCURRENT_MODIFICATION",
        "RESTORE_FAILED",
        "HOLD_TERMINALITY_UNKNOWN",
        "HOLD_MUTACION_EN_VUELO",
        "HOLD_JOURNAL_CORRUPTO",
        "HOLD_IDENTIDAD",
        "HOLD_RUTA_NO_CONFIABLE",
        "HOLD_CUARENTENA",
    }


def test_los_veredictos_compartidos_usan_el_nombre_del_vocabulario_del_servicio() -> None:
    """Los tres estados que ya existen en ``EstadoBodySlide`` no se renombran acá (2c los traduce 1:1)."""
    from sky_claw.local.tools.bodyslide_service import EstadoBodySlide

    for nombre in ("CONCURRENT_MODIFICATION", "RESTORE_FAILED", "HOLD_TERMINALITY_UNKNOWN"):
        assert Veredicto(nombre).value == EstadoBodySlide(nombre).value


# ---------------------------------------------------------------------------
# Journal: durable ANTES de mutar, exclusivo y con transiciones explícitas
# ---------------------------------------------------------------------------


def test_el_journal_es_durable_antes_de_la_primera_mutacion(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    # Arrange / Act
    tx = _iniciar(almacen, exe_dir)
    # Assert: el journal ya está en disco y la config todavía no se tocó.
    documento = _documento_en_disco(tx.ruta)
    assert documento["estado"] == "PREPARADO"
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    archivos = {a["nombre"]: a for a in documento["archivos"]}
    assert set(archivos) == {"Config.xml", "BodySlide.xml", "BuildSelection.xml"}
    assert base64.b64decode(archivos["Config.xml"]["original_b64"]) == ORIGINAL
    assert archivos["Config.xml"]["original_sha256"] == _sha(ORIGINAL)
    assert archivos["Config.xml"]["esperados_sha256"] == [_sha(SEMBRADO)]
    assert archivos["BodySlide.xml"]["esperados_sha256"] == []
    assert archivos["BuildSelection.xml"]["original_sha256"] is None
    assert archivos["BuildSelection.xml"]["original_b64"] is None
    assert documento["maquina"] == MAQUINA
    assert documento["instancia"] == identidad_de_instancia(exe_dir)
    assert documento["duenio_pid"] == os.getpid()
    assert len(documento["run_id"]) == 32
    assert len(documento["generacion"]) == 32
    assert tx.ruta.parent == almacen.directorio


def test_segunda_transaccion_sobre_la_misma_instancia_se_rechaza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path
) -> None:
    # Arrange
    primera = _iniciar(almacen, exe_dir)
    antes = primera.ruta.read_bytes()
    # Act / Assert: O_EXCL — el journal pendiente no se pisa.
    with pytest.raises(JournalOcupadoError):
        _iniciar(almacen, exe_dir)
    assert primera.ruta.read_bytes() == antes


def test_sin_identidad_de_maquina_no_se_inicia(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    with pytest.raises(PrecondicionDelJournalError):
        iniciar_transaccion(
            almacen=almacen,
            identidad=IdentidadDeEjecucion(maquina="", instancia=identidad_de_instancia(exe_dir)),
            exe_dir=exe_dir,
            bytes_sembrados=SEMBRADO,
        )
    assert almacen.listar() == []


def test_con_el_perfil_aislado_no_se_lee_la_identidad_de_la_maquina(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYCLAW_ISOLATED", "1")
    assert identidad_de_maquina() is None


def test_la_instancia_ajena_a_la_carpeta_no_se_inicia(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    otra = tmp_path / "Otra"
    otra.mkdir()
    with pytest.raises(PrecondicionDelJournalError):
        iniciar_transaccion(
            almacen=almacen,
            identidad=_identidad(otra),
            exe_dir=exe_dir,
            bytes_sembrados=SEMBRADO,
        )


def test_config_enlazado_no_se_journalea(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    afuera = exe_dir.parent / "afuera.xml"
    afuera.write_bytes(ORIGINAL)
    (exe_dir / "Config.xml").unlink()
    try:
        (exe_dir / "Config.xml").symlink_to(afuera)
    except OSError:
        pytest.skip("sin privilegio para crear enlaces simbólicos")
    with pytest.raises(PrecondicionDelJournalError):
        _iniciar(almacen, exe_dir)
    assert almacen.listar() == []


def test_transicion_no_autorizada_falla_cerrada(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    # Arrange
    tx = _iniciar(almacen, exe_dir)
    antes = tx.ruta.read_bytes()
    # Act / Assert: no se puede declarar LANZADO sin haber sembrado.
    with pytest.raises(TransicionNoAutorizadaError):
        tx.marcar_lanzado()
    assert tx.ruta.read_bytes() == antes


def test_lanzado_se_persiste_antes_de_volver(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    tx = _iniciar(almacen, exe_dir)
    tx.sembrar()
    tx.marcar_lanzado()
    assert _documento_en_disco(tx.ruta)["estado"] == "LANZADO"
    assert tx.estado is EstadoDelJournal.LANZADO


def test_sembrar_con_config_alterado_no_escribe(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    # Arrange
    tx = _iniciar(almacen, exe_dir)
    tercero = b"<Config><TargetGame>4</TargetGame><Usuario/></Config>"
    (exe_dir / "Config.xml").write_bytes(tercero)
    # Act / Assert
    with pytest.raises(ModificacionConcurrenteError):
        tx.sembrar()
    assert (exe_dir / "Config.xml").read_bytes() == tercero
    assert _documento_en_disco(tx.ruta)["estado"] == "PREPARADO"


def test_journal_reemplazado_por_otro_aborta_la_transaccion(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    # Arrange: alguien reescribe el journal (otra generación) por debajo de la transacción viva.
    tx = _iniciar(almacen, exe_dir)
    ajeno = _documento_en_disco(tx.ruta)
    ajeno["generacion"] = "f" * 32
    tx.ruta.write_bytes(bj._serializar(bj._con_integridad(ajeno)))
    # Act / Assert
    with pytest.raises(bj.JournalError):
        tx.sembrar()
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL


def test_endurece_el_directorio_una_vez_al_crearlo(tmp_path: pathlib.Path, exe_dir: pathlib.Path) -> None:
    endurecidos: list[pathlib.Path] = []
    almacen = AlmacenDeJournal(tmp_path / "state" / "bodyslide_journal", endurecer=endurecidos.append)
    _iniciar(almacen, exe_dir)
    otra = tmp_path / "Otra"
    otra.mkdir()
    (otra / "Config.xml").write_bytes(ORIGINAL)
    _iniciar(almacen, otra)
    assert endurecidos == [almacen.directorio]


def test_el_endurecimiento_por_defecto_es_restrict_to_owner() -> None:
    from sky_claw.app.security.file_permissions import restrict_to_owner

    assert AlmacenDeJournal()._endurecer is restrict_to_owner


def test_el_almacen_por_defecto_vive_en_el_estado_durable() -> None:
    from sky_claw.config import SystemPaths

    assert AlmacenDeJournal().directorio == SystemPaths.runtime_state_dir() / "bodyslide_journal"


# ---------------------------------------------------------------------------
# Journal corrupto, truncado, adulterado o enlazado: HOLD, nunca restaurar
# ---------------------------------------------------------------------------


def _adulterar(caso: str, documento: dict[str, Any]) -> bytes:
    if caso == "truncado":
        return bj._serializar(bj._con_integridad(documento))[:40]
    if caso == "vacio":
        return b""
    if caso == "integridad":
        documento["arranque_del_sistema"] = 1.0
        return json.dumps(documento).encode()
    if caso == "campo_de_mas":
        documento["extra"] = 1
    elif caso == "campo_faltante":
        del documento["run_id"]
    elif caso == "schema_booleano":
        documento["schema_version"] = True
    elif caso == "estado_desconocido":
        documento["estado"] = "PUBLICADO"
    elif caso == "archivo_ajeno":
        documento["archivos"][0]["nombre"] = "..\\..\\Windows\\win.ini"
    elif caso == "campo_de_mas_en_archivo":
        documento["archivos"][0]["extra"] = 1
    elif caso == "instancia_distinta_al_nombre":
        documento["instancia"] = "0" * 32
    elif caso == "base64_que_no_coincide":
        documento["archivos"][0]["original_b64"] = base64.b64encode(b"otra cosa").decode()
    elif caso == "pid_cero":
        # pid <= 0 se lee como "dueño muerto": aceptarlo autorizaría una restauración.
        documento["duenio_pid"] = 0
    return bj._serializar(bj._con_integridad(documento))


CASOS_DE_CORRUPCION = [
    "pid_cero",
    "truncado",
    "vacio",
    "integridad",
    "campo_de_mas",
    "campo_faltante",
    "schema_booleano",
    "estado_desconocido",
    "archivo_ajeno",
    "campo_de_mas_en_archivo",
    "instancia_distinta_al_nombre",
    "base64_que_no_coincide",
]


@pytest.mark.parametrize("caso", CASOS_DE_CORRUPCION)
def test_journal_corrupto_es_hold_y_se_preserva(
    caso: str, almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange: una corrida muerta en SEMBRADO cuyo journal luego se daña.
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    danado = _adulterar(caso, _documento_en_disco(tx.ruta))
    tx.ruta.write_bytes(danado)
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert: no se escribe la config y el journal queda como evidencia.
    assert informe.veredicto is Veredicto.HOLD_JOURNAL_CORRUPTO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    assert tx.ruta.read_bytes() == danado


def test_journal_enlazado_es_corrupto(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    real = tx.ruta.with_name("real.json")
    tx.ruta.rename(real)
    try:
        tx.ruta.symlink_to(real)
    except OSError:
        pytest.skip("sin privilegio para crear enlaces simbólicos")
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.HOLD_JOURNAL_CORRUPTO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_journal_con_un_segundo_enlace_duro_es_corrupto(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, tmp_path: pathlib.Path
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    try:
        os.link(tx.ruta, tmp_path / "copia_dura.json")
    except OSError:
        pytest.skip("el filesystem no admite enlaces duros")
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.HOLD_JOURNAL_CORRUPTO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


# ---------------------------------------------------------------------------
# Recuperación condicional (dueño muerto real; sin proceso de crash)
# ---------------------------------------------------------------------------


def test_original_intacto_no_se_sobrescribe(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    config = exe_dir / "Config.xml"
    marca = config.stat().st_mtime_ns
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert
    assert informe.veredicto is Veredicto.SIN_CAMBIOS
    assert config.read_bytes() == ORIGINAL
    assert config.stat().st_mtime_ns == marca
    assert not tx.ruta.exists()


def test_igual_al_sembrado_se_restaura(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.RESTAURADO
    assert informe.restaurados == ("Config.xml",)
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert not tx.ruta.exists()


def test_tercer_contenido_es_concurrent_modification(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    tercero = b"<Config><TargetGame>4</TargetGame><Operador/></Config>"
    (exe_dir / "Config.xml").write_bytes(tercero)
    journal = tx.ruta.read_bytes()
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.CONCURRENT_MODIFICATION
    assert (exe_dir / "Config.xml").read_bytes() == tercero
    assert tx.ruta.read_bytes() == journal


def test_un_archivo_ajeno_frena_la_restauracion_de_todos(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Se clasifica TODO antes de escribir nada: no hay restauraciones a medias."""
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    (exe_dir / "BodySlide.xml").write_bytes(b"<BodySlideConfig><Operador/></BodySlideConfig>")
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.CONCURRENT_MODIFICATION
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_lanzado_sin_terminalidad_es_hold_aunque_cambie_el_arranque(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: el journal dice LANZADO y la máquina "se reinició" (boot distinto).
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    tx.marcar_lanzado()
    monkeypatch.setattr(psutil, "boot_time", lambda: time.time())
    journal = tx.ruta.read_bytes()
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert: el boot por sí solo no autoriza (P-4).
    assert informe.veredicto is Veredicto.HOLD_TERMINALITY_UNKNOWN
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    assert tx.ruta.read_bytes() == journal


def test_duenio_vivo_es_hold_mutacion_en_vuelo(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    tx = _iniciar(almacen, exe_dir)  # dueño: este proceso, vivo
    tx.sembrar()
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.HOLD_MUTACION_EN_VUELO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_vivacidad_indeterminada_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    from sky_claw.local.frozen_runtime.root_lock import OwnerLiveness

    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    informe = recuperar_journal(
        tx.ruta,
        almacen=almacen,
        identidad_actual=_identidad(exe_dir),
        exe_dir=exe_dir,
        vivacidad=lambda _pid, _creado: OwnerLiveness.INDETERMINATE,
    )
    assert informe.veredicto is Veredicto.HOLD_TERMINALITY_UNKNOWN
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_identidad_de_maquina_incompatible_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    informe = _recuperar(almacen, exe_dir, identidad=_identidad(exe_dir, maquina="otra-maquina"))
    assert informe.veredicto is Veredicto.HOLD_IDENTIDAD
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    assert tx.ruta.exists()


def test_identidad_desconocida_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    informe = _recuperar(almacen, exe_dir, identidad=None)
    assert informe.veredicto is Veredicto.HOLD_IDENTIDAD
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_instancia_incompatible_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, tmp_path: pathlib.Path
) -> None:
    # Arrange: el journal es de exe_dir; quien recupera tiene configurada otra carpeta.
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    otra = tmp_path / "OtraInstalacion"
    otra.mkdir()
    (otra / "Config.xml").write_bytes(SEMBRADO)
    # Act
    informe = recuperar_journal(tx.ruta, almacen=almacen, identidad_actual=_identidad(otra), exe_dir=otra)
    # Assert: ninguna de las dos carpetas se toca.
    assert informe.veredicto is Veredicto.HOLD_IDENTIDAD
    assert (otra / "Config.xml").read_bytes() == SEMBRADO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_carpeta_reemplazada_en_la_misma_ruta_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Misma ruta, otra carpeta física: la identidad física no coincide y no se escribe."""
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    instancia_registrada = _documento_en_disco(tx.ruta)["instancia"]
    exe_dir.rename(exe_dir.with_name("BodySlide.viejo"))
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_bytes(SEMBRADO)
    informe = recuperar_journal(
        tx.ruta,
        almacen=almacen,
        identidad_actual=IdentidadDeEjecucion(maquina=MAQUINA, instancia=instancia_registrada),
        exe_dir=exe_dir,
    )
    assert informe.veredicto is Veredicto.HOLD_RUTA_NO_CONFIABLE
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_ruta_del_journal_adulterada_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, tmp_path: pathlib.Path
) -> None:
    # Arrange: integridad recalculada, pero el directorio apunta a otro lado.
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    victima = tmp_path / "Victima"
    victima.mkdir()
    (victima / "Config.xml").write_bytes(SEMBRADO)
    documento = _documento_en_disco(tx.ruta)
    documento["directorio"] = str(victima)
    tx.ruta.write_bytes(bj._serializar(bj._con_integridad(documento)))
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert
    assert informe.veredicto is Veredicto.HOLD_RUTA_NO_CONFIABLE
    assert (victima / "Config.xml").read_bytes() == SEMBRADO
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_cuarentena_activa_es_hold(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    almacen.marcador_de_cuarentena.write_text("{}", encoding="utf-8")
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.HOLD_CUARENTENA
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_restauracion_fallida_preserva_journal_y_permite_reintento(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    original = bj._reemplazar_atomico

    def _disco_lleno(destino: pathlib.Path, blob: bytes) -> None:
        if destino.name == "Config.xml":
            raise OSError(28, "No space left on device")
        original(destino, blob)

    monkeypatch.setattr(bj, "_reemplazar_atomico", _disco_lleno)
    # Act 1: falla la escritura de la restauración.
    primera = _recuperar(almacen, exe_dir)
    # Assert 1: journal durable en RESTAURACION_FALLIDA, config intacta en lo sembrado.
    assert primera.veredicto is Veredicto.RESTORE_FAILED
    assert _documento_en_disco(tx.ruta)["estado"] == "RESTAURACION_FALLIDA"
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    # Act 2: reintento controlado con el disco sano.
    monkeypatch.setattr(bj, "_reemplazar_atomico", original)
    segunda = _recuperar(almacen, exe_dir)
    # Assert 2
    assert segunda.veredicto is Veredicto.RESTAURADO
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert not tx.ruta.exists()


def test_doble_recuperacion_es_idempotente(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    assert _recuperar(almacen, exe_dir).veredicto is Veredicto.RESTAURADO
    marca = (exe_dir / "Config.xml").stat().st_mtime_ns
    segunda = _recuperar(almacen, exe_dir)
    assert segunda.veredicto is Veredicto.SIN_JOURNAL
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert (exe_dir / "Config.xml").stat().st_mtime_ns == marca


def test_restaurado_sin_retirar_el_journal_se_cierra_sin_escribir(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Crash entre el RESTAURADO durable y el ``unlink`` del journal: el siguiente arranque sólo lo cierra."""
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    documento = _documento_en_disco(tx.ruta)
    (exe_dir / "Config.xml").write_bytes(ORIGINAL)
    documento["estado"] = "RESTAURADO"
    tx.ruta.write_bytes(bj._serializar(bj._con_integridad(documento)))
    marca = (exe_dir / "Config.xml").stat().st_mtime_ns
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.SIN_CAMBIOS
    assert not tx.ruta.exists()
    assert (exe_dir / "Config.xml").stat().st_mtime_ns == marca


def test_terminal_restaura_lo_que_la_corrida_escribio(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange: BodySlide escribió BodySlide.xml y creó BuildSelection.xml; la terminalidad se registró.
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    tx.marcar_lanzado()
    (exe_dir / "BodySlide.xml").write_bytes(APP_DE_LA_CORRIDA)
    (exe_dir / "BuildSelection.xml").write_bytes(b"<BuildSelection/>")
    tx.marcar_terminal()
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert
    assert informe.veredicto is Veredicto.RESTAURADO
    assert set(informe.restaurados) == {"Config.xml", "BodySlide.xml", "BuildSelection.xml"}
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert (exe_dir / "BodySlide.xml").read_bytes() == APP_ORIGINAL
    assert not (exe_dir / "BuildSelection.xml").exists()


def test_original_ausente_se_restaura_retirando_lo_sembrado(
    almacen: AlmacenDeJournal, tmp_path: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    exe_dir = tmp_path / "SinConfig"
    exe_dir.mkdir()
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.RESTAURADO
    assert not (exe_dir / "Config.xml").exists()


def test_temporal_huerfano_propio_se_retira_y_lo_ajeno_no(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    huerfano = exe_dir / f"Config.xml.{'a' * 32}.tmp"
    huerfano.write_bytes(SEMBRADO[:10])
    ajeno = exe_dir / "Config.xml.bak"
    ajeno.write_bytes(b"del usuario")
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.SIN_CAMBIOS
    assert not huerfano.exists()
    assert ajeno.read_bytes() == b"del usuario"
    assert not tx.ruta.exists()


def test_cerrar_retira_los_temporales_huerfanos_del_propio_journal(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Un crash a mitad de una transición deja ``.<journal>.<hex>.tmp``; al cerrar se retira, lo ajeno no."""
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    huerfano = almacen.directorio / f".{tx.ruta.name}.{'b' * 32}.tmp"
    huerfano.write_bytes(b"{")
    ajeno = almacen.directorio / "notas-del-operador.txt"
    ajeno.write_bytes(b"no tocar")
    assert _recuperar(almacen, exe_dir).veredicto is Veredicto.RESTAURADO
    assert not huerfano.exists()
    assert ajeno.read_bytes() == b"no tocar"


def test_recuperar_sin_journal_no_toca_nada(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.SIN_JOURNAL
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL


# ---------------------------------------------------------------------------
# Camino vivo (lo que 2c cablea): restaurar y abortar desde la misma transacción
# ---------------------------------------------------------------------------


def test_restaurar_en_vivo_desde_sembrado(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    tx = _iniciar(almacen, exe_dir)
    tx.sembrar()
    informe = tx.restaurar()
    assert informe.veredicto is Veredicto.RESTAURADO
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert not tx.ruta.exists()


def test_restaurar_en_vivo_desde_lanzado_no_esta_autorizado(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    tx = _iniciar(almacen, exe_dir)
    tx.sembrar()
    tx.marcar_lanzado()
    with pytest.raises(TransicionNoAutorizadaError):
        tx.restaurar()
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO


def test_abortar_sin_mutar_cierra_el_journal(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    tx = _iniciar(almacen, exe_dir)
    informe = tx.abortar_sin_mutar()
    assert informe.veredicto is Veredicto.SIN_CAMBIOS
    assert not tx.ruta.exists()
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL


# ---------------------------------------------------------------------------
# Crash en un proceso REAL: el disco que queda decide
# ---------------------------------------------------------------------------

_GUION_DE_CRASH = """
import os, pathlib, sys
sys.path.insert(0, {raiz!r})
from sky_claw.local.tools import bodyslide_journal as bj

exe_dir = pathlib.Path(sys.argv[1])
almacen = bj.AlmacenDeJournal(pathlib.Path(sys.argv[2]), endurecer=lambda _p: None)
punto = sys.argv[3]
sembrado = bytes.fromhex(sys.argv[4])

def morir(*_a, **_k):
    os._exit(77)

if punto == "tras_temporal":
    bj.TransaccionDeConfig._tras_escribir_temporal = morir
if punto == "tras_reemplazo":
    bj.TransaccionDeConfig._tras_reemplazar = morir
identidad = bj.IdentidadDeEjecucion(maquina={maquina!r}, instancia=bj.identidad_de_instancia(exe_dir))
tx = bj.iniciar_transaccion(almacen=almacen, identidad=identidad, exe_dir=exe_dir, bytes_sembrados=sembrado)
if punto == "preparado":
    os._exit(77)
tx.sembrar()
if punto == "sembrado":
    os._exit(77)
tx.marcar_lanzado()
if punto == "lanzado":
    os._exit(77)
(exe_dir / "BodySlide.xml").write_bytes({app_de_la_corrida!r})
tx.marcar_terminal()
if punto == "terminal":
    os._exit(77)
os._exit(0)
"""


def _entorno_hijo() -> dict[str, str]:
    entorno = dict(os.environ)
    entorno["PYTHONPATH"] = str(RAIZ)
    return entorno


def _crash(tmp_path: pathlib.Path, exe_dir: pathlib.Path, almacen: AlmacenDeJournal, punto: str) -> None:
    guion = tmp_path / f"crash_{punto}.py"
    guion.write_text(
        _GUION_DE_CRASH.format(raiz=str(RAIZ), maquina=MAQUINA, app_de_la_corrida=APP_DE_LA_CORRIDA),
        encoding="utf-8",
    )
    completado = subprocess.run(  # noqa: S603 - argv fijo, sin shell
        [sys.executable, str(guion), str(exe_dir), str(almacen.directorio), punto, SEMBRADO.hex()],
        capture_output=True,
        timeout=120,
        env=_entorno_hijo(),
        check=False,
    )
    assert completado.returncode == 77, completado.stderr.decode(errors="replace")


# (punto de crash, estado en disco tras el crash, Config.xml tras el crash, veredicto, Config.xml final)
MATRIZ_DE_CRASH = [
    ("preparado", "PREPARADO", ORIGINAL, Veredicto.SIN_CAMBIOS, ORIGINAL),
    ("tras_temporal", "PREPARADO", ORIGINAL, Veredicto.SIN_CAMBIOS, ORIGINAL),
    ("tras_reemplazo", "PREPARADO", SEMBRADO, Veredicto.RESTAURADO, ORIGINAL),
    ("sembrado", "SEMBRADO", SEMBRADO, Veredicto.RESTAURADO, ORIGINAL),
    ("lanzado", "LANZADO", SEMBRADO, Veredicto.HOLD_TERMINALITY_UNKNOWN, SEMBRADO),
    ("terminal", "TERMINAL", SEMBRADO, Veredicto.RESTAURADO, ORIGINAL),
]


@pytest.mark.parametrize(("punto", "estado", "config_tras_crash", "veredicto", "config_final"), MATRIZ_DE_CRASH)
def test_crash_real_y_recuperacion(
    punto: str,
    estado: str,
    config_tras_crash: bytes,
    veredicto: Veredicto,
    config_final: bytes,
    tmp_path: pathlib.Path,
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
) -> None:
    # Arrange: el proceso muere de verdad en el punto indicado.
    _crash(tmp_path, exe_dir, almacen, punto)
    ruta = almacen.ruta_de(identidad_de_instancia(exe_dir))
    # Assert del disco que dejó el crash (antes de recuperar).
    assert _documento_en_disco(ruta)["estado"] == estado
    assert (exe_dir / "Config.xml").read_bytes() == config_tras_crash
    huerfanos = sorted(p.name for p in exe_dir.glob("Config.xml.*.tmp"))
    assert len(huerfanos) == (1 if punto == "tras_temporal" else 0)
    # Act
    informe = _recuperar(almacen, exe_dir)
    # Assert
    assert informe.veredicto is veredicto
    assert (exe_dir / "Config.xml").read_bytes() == config_final
    if veredicto is Veredicto.HOLD_TERMINALITY_UNKNOWN:
        assert _documento_en_disco(ruta)["estado"] == "LANZADO"
    else:
        assert not ruta.exists()
        assert list(exe_dir.glob("Config.xml.*.tmp")) == []
    if punto == "terminal":
        assert (exe_dir / "BodySlide.xml").read_bytes() == APP_ORIGINAL


def test_crash_tras_sembrado_con_alteracion_concurrente_preserva_todo(
    tmp_path: pathlib.Path, almacen: AlmacenDeJournal, exe_dir: pathlib.Path
) -> None:
    _crash(tmp_path, exe_dir, almacen, "sembrado")
    tercero = b"<Config><TargetGame>4</TargetGame><EditadoAMano/></Config>"
    (exe_dir / "Config.xml").write_bytes(tercero)
    informe = _recuperar(almacen, exe_dir)
    assert informe.veredicto is Veredicto.CONCURRENT_MODIFICATION
    assert (exe_dir / "Config.xml").read_bytes() == tercero
    assert _documento_en_disco(almacen.ruta_de(identidad_de_instancia(exe_dir)))["estado"] == "SEMBRADO"


# ---------------------------------------------------------------------------
# Dos procesos independientes
# ---------------------------------------------------------------------------

_GUION_DE_INICIO = """
import json, pathlib, sys, time
sys.path.insert(0, {raiz!r})
from sky_claw.local.tools import bodyslide_journal as bj

exe_dir = pathlib.Path(sys.argv[1])
almacen = bj.AlmacenDeJournal(pathlib.Path(sys.argv[2]), endurecer=lambda _p: None)
salida = pathlib.Path(sys.argv[3])
arranque = float(sys.argv[4])
identidad = bj.IdentidadDeEjecucion(maquina={maquina!r}, instancia=bj.identidad_de_instancia(exe_dir))
while time.time() < arranque:
    time.sleep(0.001)
try:
    bj.iniciar_transaccion(almacen=almacen, identidad=identidad, exe_dir=exe_dir, bytes_sembrados=b"x")
    resultado = "gano"
except bj.JournalOcupadoError:
    resultado = "ocupado"
salida.write_text(json.dumps(resultado), encoding="utf-8")
"""


def test_dos_procesos_compiten_por_iniciar_y_gana_uno(
    tmp_path: pathlib.Path, almacen: AlmacenDeJournal, exe_dir: pathlib.Path
) -> None:
    guion = tmp_path / "iniciar.py"
    guion.write_text(_GUION_DE_INICIO.format(raiz=str(RAIZ), maquina=MAQUINA), encoding="utf-8")
    arranque = time.time() + 2.0
    salidas = [tmp_path / f"inicio_{i}.json" for i in (1, 2)]
    procesos = [
        subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
            [sys.executable, str(guion), str(exe_dir), str(almacen.directorio), str(salida), str(arranque)],
            env=_entorno_hijo(),
        )
        for salida in salidas
    ]
    for proceso in procesos:
        assert proceso.wait(timeout=120) == 0
    resultados = sorted(json.loads(s.read_text(encoding="utf-8")) for s in salidas)
    assert resultados == ["gano", "ocupado"]
    assert len(almacen.listar()) == 1


_GUION_DE_RECUPERACION = """
import asyncio, json, pathlib, sys, time
sys.path.insert(0, {raiz!r})
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools import bodyslide_journal as bj
from sky_claw.local.tools.rollback_reconciler import reconcile_bodyslide_config_journals

exe_dir = pathlib.Path(sys.argv[1])
almacen = bj.AlmacenDeJournal(pathlib.Path(sys.argv[2]), endurecer=lambda _p: None)
salida = pathlib.Path(sys.argv[3])
arranque = float(sys.argv[4])
db = pathlib.Path(sys.argv[5])


async def main():
    manager = DistributedLockManager(db, default_ttl=30.0, max_retries=1, backoff_base=0.01, backoff_max=0.02)
    await manager.initialize()
    identidad = bj.IdentidadDeEjecucion(maquina={maquina!r}, instancia=bj.identidad_de_instancia(exe_dir))
    while time.time() < arranque:
        time.sleep(0.001)
    try:
        resultado = await reconcile_bodyslide_config_journals(
            almacen=almacen, exe_dir=exe_dir, identidad_actual=identidad, lock_manager=manager
        )
    finally:
        await manager.close()
    veredictos = [i.veredicto.value for i in resultado.informes]
    salida.write_text(json.dumps({{"omitido": resultado.omitido_por_lock, "veredictos": veredictos}}), encoding="utf-8")


asyncio.run(main())
"""


async def _inicializar_base_de_locks(db: pathlib.Path) -> None:
    manager = DistributedLockManager(db, default_ttl=30.0)
    await manager.initialize()
    await manager.close()


def test_dos_procesos_recuperan_a_la_vez_sin_doble_escritura(
    tmp_path: pathlib.Path, almacen: AlmacenDeJournal, exe_dir: pathlib.Path
) -> None:
    # Arrange: una corrida muerta en SEMBRADO; dos arranques compiten por recuperarla.
    _crash(tmp_path, exe_dir, almacen, "sembrado")
    guion = tmp_path / "recuperar.py"
    guion.write_text(_GUION_DE_RECUPERACION.format(raiz=str(RAIZ), maquina=MAQUINA), encoding="utf-8")
    db = tmp_path / "locks.db"
    # La base de locks ya existe en WAL antes de que compitan, como en un arranque real con
    # estado previo. Si los dos hijos la convirtieran a la vez, el perdedor recibiría
    # SQLITE_BUSY sin esperar: SQLite saltea el busy handler en la promoción de lock (ver
    # ``DatabaseLifecycleManager._enter_wal_mode``). Esa carrera es de
    # ``DistributedLockManager`` standalone, no de la reconciliación que mide este test, y
    # tumbó ``main`` en Windows (run 45677510174).
    asyncio.run(_inicializar_base_de_locks(db))
    arranque = time.time() + 3.0
    salidas = [tmp_path / f"recuperacion_{i}.json" for i in (1, 2)]
    procesos = [
        subprocess.Popen(  # noqa: S603 - argv fijo, sin shell
            [sys.executable, str(guion), str(exe_dir), str(almacen.directorio), str(s), str(arranque), str(db)],
            env=_entorno_hijo(),
            stderr=subprocess.PIPE,
        )
        for s in salidas
    ]
    try:
        errores = [proceso.communicate(timeout=180)[1] for proceso in procesos]
    finally:
        # Un hijo que sobrevive al test lo hace fallar al SIGUIENTE (ResourceWarning de Popen).
        for proceso in procesos:
            if proceso.poll() is None:
                proceso.kill()
                proceso.communicate()
    for proceso, error in zip(procesos, errores, strict=True):
        assert proceso.returncode == 0, error.decode(errors="replace")
    # Act / Assert: exactamente una restauración; el otro se omitió o ya no encontró journal.
    resultados = [json.loads(s.read_text(encoding="utf-8")) for s in salidas]
    veredictos = sorted(v for r in resultados for v in r["veredictos"])
    assert veredictos.count("RESTAURADO") == 1
    assert set(veredictos) <= {"RESTAURADO", "SIN_JOURNAL"}
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert almacen.listar() == []


# ---------------------------------------------------------------------------
# Integración con el reconciliador existente
# ---------------------------------------------------------------------------


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> Any:
    manager = DistributedLockManager(
        tmp_path / "locks.db", default_ttl=5.0, max_retries=1, backoff_base=0.01, backoff_max=0.02
    )
    await manager.initialize()
    yield manager
    await manager.close()


async def test_el_consumidor_se_omite_con_el_recurso_tomado(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, lock_manager: Any
) -> None:
    # Arrange: una corrida (otra instancia) tiene el recurso bodyslide-meshes.
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    assert await lock_manager.acquire_lock(BODYSLIDE_MESHES_RESOURCE_ID, "bodyslide-tool", ttl=30.0)
    # Act
    resultado = await reconcile_bodyslide_config_journals(
        almacen=almacen, exe_dir=exe_dir, identidad_actual=_identidad(exe_dir), lock_manager=lock_manager
    )
    # Assert
    assert resultado.omitido_por_lock is True
    assert resultado.informes == ()
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    assert tx.ruta.exists()


async def test_el_consumidor_restaura_con_el_recurso_libre(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, lock_manager: Any
) -> None:
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    resultado = await reconcile_bodyslide_config_journals(
        almacen=almacen, exe_dir=exe_dir, identidad_actual=_identidad(exe_dir), lock_manager=lock_manager
    )
    assert resultado.omitido_por_lock is False
    assert [i.veredicto for i in resultado.informes] == [Veredicto.RESTAURADO]
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert await lock_manager.get_lock_info(BODYSLIDE_MESHES_RESOURCE_ID) is None


async def test_el_consumidor_reporta_journals_de_otra_instancia_sin_tocarlos(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    lock_manager: Any,
    tmp_path: pathlib.Path,
) -> None:
    otra = tmp_path / "OtraInstalacion"
    otra.mkdir()
    (otra / "Config.xml").write_bytes(ORIGINAL)
    tx = _iniciar(almacen, otra, duenio=duenio_muerto)
    tx.sembrar()
    resultado = await reconcile_bodyslide_config_journals(
        almacen=almacen, exe_dir=exe_dir, identidad_actual=_identidad(exe_dir), lock_manager=lock_manager
    )
    assert [i.veredicto for i in resultado.informes] == [Veredicto.HOLD_IDENTIDAD]
    assert (otra / "Config.xml").read_bytes() == SEMBRADO
    assert tx.ruta.exists()


def test_bodyslide_config_no_es_un_productor_de_move_aside(tmp_path: pathlib.Path) -> None:
    """El consumidor del journal no se confunde con los backups ``.rollback-*`` de BodySlide."""
    assert "bodyslide-config" not in PRODUCTORES_CABLEADOS
    nombres = {p.nombre for p in construir_productores_de_move_aside(mo2_root=tmp_path, game=tmp_path)}
    assert "bodyslide-config" not in nombres


def _llamadores_en_produccion(nombre: str) -> set[str]:
    encontrados: set[str] = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                llamado = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if llamado == nombre:
                    encontrados.add(archivo.relative_to(RAIZ).as_posix())
    return encontrados


def test_los_call_sites_productivos_del_journal_estan_congelados() -> None:
    """PR 2c-1 cableó el journal: el runner abre la transacción y el arranque corre el consumidor.

    Un camino nuevo que abra transacciones o recupere journals rompe esto hasta que se
    decida (y se pruebe) su participación. La producción sigue apagada por la barrera.
    """
    assert _llamadores_en_produccion("reconcile_bodyslide_config_journals") == {"sky_claw/app_context.py"}
    assert _llamadores_en_produccion("iniciar_transaccion") == {"sky_claw/local/tools/bodyslide_runner.py"}
    assert _llamadores_en_produccion("recuperar_todos") == {"sky_claw/local/tools/rollback_reconciler.py"}
    # PR 2c-2b (opción (a)): solo el operador registra la terminalidad de un LANZADO, y solo desde su superficie.
    assert _llamadores_en_produccion("registrar_terminalidad_por_operador") == {
        "sky_claw/local/tools/bodyslide_operador.py"
    }


def test_los_importadores_del_journal_estan_congelados() -> None:
    importadores: set[str] = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        relativo = archivo.relative_to(RAIZ).as_posix()
        if relativo == "sky_claw/local/tools/bodyslide_journal.py":
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            # PR 2c-2b: también ``from sky_claw.local.tools import bodyslide_journal as bj``. Sin esa
            # forma el censo no veía al módulo del operador, que importa el journal así.
            if (
                isinstance(nodo, ast.ImportFrom)
                and (
                    (nodo.module or "").endswith("bodyslide_journal")
                    or any(a.name == "bodyslide_journal" for a in nodo.names)
                )
                or isinstance(nodo, ast.Import)
                and any(a.name.endswith("bodyslide_journal") for a in nodo.names)
            ):
                importadores.add(relativo)
    assert importadores == {
        "sky_claw/local/tools/rollback_reconciler.py",
        "sky_claw/local/tools/bodyslide_runner.py",
        "sky_claw/app_context.py",
        # PR 2c-2b: la superficie del operador (``--mode bodyslide-recovery``).
        "sky_claw/local/tools/bodyslide_operador.py",
        "sky_claw/__main__.py",
    }


def test_la_barrera_productiva_sigue_en_false() -> None:
    from sky_claw.local.tools import bodyslide_service

    assert bodyslide_service._CONTENCION_BODYSLIDE_VERIFICADA is False


def test_el_journal_no_deja_estado_fuera_de_su_directorio(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Fuera de la config sembrada, todo lo que escribe el journal cae bajo su directorio."""
    antes = {p.name for p in exe_dir.iterdir()}
    tx = _iniciar(almacen, exe_dir, duenio=duenio_muerto)
    tx.sembrar()
    tx.marcar_lanzado()
    tx.marcar_terminal()
    assert {p.name for p in exe_dir.iterdir()} == antes
    assert {p.name for p in almacen.directorio.iterdir()} == {tx.ruta.name}
