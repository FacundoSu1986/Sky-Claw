"""Tests del contrato de arranque del precache NGIO (P0 SAFE CACHE, F-01).

El hecho que estos tests fijan: NGIO-NG borra ``*.cgid``/``*.dgid``/``*.fail``
cuando ``PrecacheGrass.txt`` no contiene progreso. Por eso el arranque tiene
tres modos con precondiciones verificables y **ninguno** destruye por defecto.

Los escenarios son sintéticos (``tmp_path``): no hay rig Windows, ni MO2, ni
NGIO. Lo que se verifica es el contrato de Sky-Claw sobre el sistema de archivos
—qué se lee, qué se copia, qué se verifica y cuándo se niega a arrancar—, no la
semántica real del binario de NGIO, que sigue NO VERIFICADA (ver el informe).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from sky_claw.local.tools.grass_cache_safety import (
    APROBACION_REBUILD,
    GrassCacheSafetyError,
    aplicar_plan,
    archivar_progreso,
    huella_del_cache,
    inventariar_cache,
    neutralizar_flag,
    planificar_arranque,
    preparar_arranque,
    recuperar_progreso,
    respaldar_cache,
    ruta_del_flag,
)


@pytest.fixture
def juego(tmp_path: pathlib.Path) -> pathlib.Path:
    """Directorio del juego con el exe (el flag vive ahí)."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    return game


@pytest.fixture
def grass(tmp_path: pathlib.Path) -> pathlib.Path:
    """``overwrite/Grass`` con un caché de dos celdas y un ``.fail``."""
    raiz = tmp_path / "overwrite" / "Grass"
    _escribir(raiz / "Tamriel.cgid", b"celda-tamriel")
    _escribir(raiz / "Solstheim.cgid", b"celda-solstheim")
    _escribir(raiz / "Tamriel.dgid", b"dgid")
    _escribir(raiz / "celda_rota.fail", b"fallo")
    return raiz


@pytest.fixture
def estado(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "estado"


def _escribir(ruta: pathlib.Path, datos: bytes) -> pathlib.Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(datos)
    return ruta


def _inventario(grass: pathlib.Path, juego: pathlib.Path):
    return inventariar_cache(grass_dir=grass, flag_path=ruta_del_flag(juego))


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------


def test_inventario_cuenta_extensiones_de_ngio(grass: pathlib.Path, juego: pathlib.Path) -> None:
    inv = _inventario(grass, juego)

    assert (inv.cgid_count, inv.dgid_count, inv.fail_count) == (2, 1, 1)
    assert inv.archivos_cache == 4
    assert inv.cgid_bytes == len(b"celda-tamriel") + len(b"celda-solstheim")
    assert inv.tiene_cache is True
    assert inv.flag_presente is False
    assert inv.flag_progreso is None


def test_inventario_de_carpeta_ausente_es_cache_vacio(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # NGIO crea Grass/ al escribir el primer .cgid: ausente no es error.
    inv = inventariar_cache(grass_dir=tmp_path / "no_existe" / "Grass", flag_path=ruta_del_flag(juego))

    assert inv.tiene_cache is False
    assert inv.archivos_cache == 0


def test_inventario_trata_cualquier_contenido_como_progreso(grass: pathlib.Path, juego: pathlib.Path) -> None:
    # No se interpreta el formato del progreso: un formato desconocido PROTEGE
    # (impide new/rebuild sin decisión), que es el lado seguro del error.
    ruta_del_flag(juego).write_text("formato-que-no-conozco\n", encoding="utf-8")

    inv = _inventario(grass, juego)

    assert inv.flag_presente is True
    assert inv.flag_progreso == "formato-que-no-conozco\n"


def test_inventario_flag_en_blanco_no_es_progreso(grass: pathlib.Path, juego: pathlib.Path) -> None:
    ruta_del_flag(juego).write_text("  \n\t\n", encoding="utf-8")

    inv = _inventario(grass, juego)

    assert inv.flag_presente is True
    assert inv.flag_progreso is None


def test_huella_cambia_si_cambia_el_conjunto(grass: pathlib.Path, juego: pathlib.Path) -> None:
    antes = _inventario(grass, juego).huella
    (grass / "nueva.cgid").write_bytes(b"otra")

    assert _inventario(grass, juego).huella != antes
    # El orden de iteración del SO no puede cambiar la huella.
    assert huella_del_cache([("a", 1), ("b", 2)]) == huella_del_cache([("b", 2), ("a", 1)])


def test_inventario_rechaza_enlaces_en_el_cache(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # Copiar a través de un junction llevaría bytes de fuera del rig y el
    # borrado de NGIO alcanzaría el destino: fail-closed.
    enlace = grass / "enlazado.cgid"
    try:
        enlace.symlink_to(tmp_path / "afuera.cgid")
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        _inventario(grass, juego)

    assert excinfo.value.codigo == "cache_con_enlaces"


# ---------------------------------------------------------------------------
# Decisión: NEW / RESUME / REBUILD
# ---------------------------------------------------------------------------


def test_new_con_cache_vacio_escribe_flag_vacio(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    vacio = tmp_path / "overwrite" / "Grass"

    plan = preparar_arranque(modo="new", game_path=juego, grass_dir=vacio)

    assert plan.accion == "escribir_vacio"
    assert plan.respaldo is None
    aplicar_plan(plan, ruta_del_flag(juego))
    assert ruta_del_flag(juego).read_text(encoding="utf-8") == ""


def test_new_con_cache_existente_se_niega(grass: pathlib.Path, juego: pathlib.Path) -> None:
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="new", game_path=juego, grass_dir=grass)

    assert excinfo.value.codigo == "cache_existente_sin_decision"
    assert not ruta_del_flag(juego).exists(), "negarse no puede escribir el flag"


def test_new_no_trunca_un_flag_con_progreso(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    progreso = "Tamriel\n-2,4\n"
    ruta_del_flag(juego).write_text(progreso, encoding="utf-8")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="new", game_path=juego, grass_dir=tmp_path / "overwrite" / "Grass")

    assert excinfo.value.codigo == "flag_con_progreso_sin_decision"
    assert ruta_del_flag(juego).read_text(encoding="utf-8") == progreso


def test_resume_conserva_el_contenido_exacto_del_flag(grass: pathlib.Path, juego: pathlib.Path) -> None:
    # Byte a byte: cualquier normalización (recorte, fin de línea, BOM)
    # convertiría una reanudación correcta en un borrado de caché.
    progreso = "  Tamriel\r\n-2,4  \n"
    ruta_del_flag(juego).write_text(progreso, encoding="utf-8", newline="")

    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass)
    aplicar_plan(plan, ruta_del_flag(juego))

    assert plan.accion == "conservar"
    assert ruta_del_flag(juego).read_bytes() == progreso.encode("utf-8")


def test_resume_sin_progreso_se_niega(grass: pathlib.Path, juego: pathlib.Path) -> None:
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="resume", game_path=juego, grass_dir=grass)

    assert excinfo.value.codigo == "resume_sin_progreso"


def test_rebuild_sin_aprobacion_se_niega(grass: pathlib.Path, juego: pathlib.Path) -> None:
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="rebuild", game_path=juego, grass_dir=grass)

    assert excinfo.value.codigo == "rebuild_sin_aprobacion"
    assert not (grass.parent / "estado").exists(), "sin aprobación no se escribe nada"


def test_rebuild_sin_raiz_de_estado_se_niega(grass: pathlib.Path, juego: pathlib.Path) -> None:
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(
            modo="rebuild",
            game_path=juego,
            grass_dir=grass,
            aprobacion=APROBACION_REBUILD,
        )

    assert excinfo.value.codigo == "rebuild_sin_respaldo"


def test_rebuild_con_aprobacion_respalda_y_planifica(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    plan = preparar_arranque(
        modo="rebuild",
        game_path=juego,
        grass_dir=grass,
        aprobacion=APROBACION_REBUILD,
        raiz_estado=estado,
    )

    assert plan.accion == "escribir_vacio"
    assert plan.respaldo is not None
    assert plan.respaldo.cubre(plan.inventario)
    assert plan.respaldo.archivos == 4
    # Los cuatro artefactos están copiados, incluido el .fail (diagnóstico).
    for nombre in ("Tamriel.cgid", "Solstheim.cgid", "Tamriel.dgid", "celda_rota.fail"):
        assert (plan.respaldo.destino / nombre).is_file()


def test_rebuild_archiva_el_progreso_antes_de_pisarlo(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    progreso = "Tamriel\n-2,4\n"
    ruta_del_flag(juego).write_text(progreso, encoding="utf-8")

    plan = preparar_arranque(
        modo="rebuild",
        game_path=juego,
        grass_dir=grass,
        aprobacion=APROBACION_REBUILD,
        raiz_estado=estado,
    )
    aplicar_plan(plan, ruta_del_flag(juego))

    assert ruta_del_flag(juego).read_text(encoding="utf-8") == ""
    assert recuperar_progreso(estado, plan.inventario) == progreso


# ---------------------------------------------------------------------------
# Respaldo: verificación real y reuso
# ---------------------------------------------------------------------------


def test_respaldo_de_otro_conjunto_no_habilita_rebuild(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # El plan es puro: un respaldo de OTRO conjunto de archivos no habilita el
    # borrado, aunque exista y verifique contra su propio inventario.
    inv = _inventario(grass, juego)
    respaldo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)
    (grass / "aparece_despues.cgid").write_bytes(b"nueva")
    inv_nuevo = _inventario(grass, juego)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        planificar_arranque(
            modo="rebuild",
            inventario=inv_nuevo,
            aprobacion=APROBACION_REBUILD,
            respaldo=respaldo,
        )

    assert excinfo.value.codigo == "respaldo_no_cubre_el_cache"
    assert respaldo.cubre(inv) is True
    assert respaldo.cubre(inv_nuevo) is False


def test_preparar_arranque_respalda_el_estado_actual_si_el_cache_cambio(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # Si el caché creció desde el respaldo anterior, REBUILD toma uno NUEVO del
    # estado actual antes de destruirlo: nunca se apoya en un respaldo viejo.
    estado = tmp_path / "estado"
    preparar_arranque(
        modo="rebuild", game_path=juego, grass_dir=grass, aprobacion=APROBACION_REBUILD, raiz_estado=estado
    )
    (grass / "aparece_despues.cgid").write_bytes(b"nueva")

    plan = preparar_arranque(
        modo="rebuild", game_path=juego, grass_dir=grass, aprobacion=APROBACION_REBUILD, raiz_estado=estado
    )

    assert plan.respaldo is not None
    assert plan.respaldo.cubre(plan.inventario) is True
    assert (plan.respaldo.destino / "aparece_despues.cgid").is_file()


def test_respaldo_verifica_releyendo_el_destino(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Si la copia no coincide con el origen, el respaldo NO vale: es la única
    # diferencia entre "hay una carpeta" y "hay un respaldo verificado".
    inv = _inventario(grass, juego)
    import sky_claw.local.tools.grass_cache_safety as safety

    real = safety._copiar_con_hash

    def _copia_truncada(origen: pathlib.Path, destino: pathlib.Path) -> tuple[int, str]:
        destino.write_bytes(b"x")  # copia incompleta, hash mentiroso
        return real(origen, destino)[0], "0" * 64

    monkeypatch.setattr(safety, "_copiar_con_hash", _copia_truncada)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)

    assert excinfo.value.codigo == "respaldo_no_verificado"


def test_respaldo_es_idempotente_y_no_recopia(tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path) -> None:
    inv = _inventario(grass, juego)
    uno = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)
    marca = uno.manifiesto.stat().st_mtime_ns

    dos = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)

    assert dos.destino == uno.destino
    assert dos.manifiesto.stat().st_mtime_ns == marca, "un respaldo verificado se reusa, no se recopia"


def test_manifiesto_tamperizado_no_se_reusa_como_verificado(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # Un manifiesto con la huella cambiada no acredita nada: el respaldo se
    # rehace desde el origen en vez de confiar en la carpeta existente.
    inv = _inventario(grass, juego)
    respaldo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)
    carga = json.loads(respaldo.manifiesto.read_text(encoding="utf-8"))
    carga["huella"] = "0" * 64
    respaldo.manifiesto.write_text(json.dumps(carga), encoding="utf-8")

    nuevo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inv)

    assert nuevo.cubre(inv) is True
    releido = json.loads(nuevo.manifiesto.read_text(encoding="utf-8"))
    assert releido["huella"] == inv.huella, "el manifiesto reusado sería el tamperizado"
    # El respaldo inválido se conserva apartado, no se pisa en silencio.
    invalidos = list(tmp_path.glob("respaldo.invalido-*"))
    assert invalidos, "un respaldo que no verifica se aparta con nombre explícito"


# ---------------------------------------------------------------------------
# Evidencia de progreso (reanudación)
# ---------------------------------------------------------------------------


def test_progreso_archivado_se_restaura_si_la_huella_coincide(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    inv = _inventario(grass, juego)
    archivar_progreso(estado, inv, "Tamriel\n-2,4\n")

    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)

    assert plan.accion == "restaurar"
    aplicar_plan(plan, ruta_del_flag(juego))
    assert ruta_del_flag(juego).read_text(encoding="utf-8") == "Tamriel\n-2,4\n"


def test_progreso_archivado_de_otro_cache_no_se_restaura(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # Reanudar con el progreso de otro conjunto haría que NGIO saltee celdas
    # que nunca se escribieron: mejor negarse que producir un caché incompleto.
    inv_ajeno = _inventario(grass, juego)
    archivar_progreso(estado, inv_ajeno, "Tamriel\n0,0\n")
    (grass / "aparece_despues.cgid").write_bytes(b"nueva")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)

    assert excinfo.value.codigo == "resume_sin_progreso"
    assert recuperar_progreso(estado, _inventario(grass, juego)) is None


def test_neutralizar_flag_archiva_y_borra(grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path) -> None:
    progreso = "Tamriel\n-2,4\n"
    ruta_del_flag(juego).write_text(progreso, encoding="utf-8")
    inv = _inventario(grass, juego)

    neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=estado)

    assert not ruta_del_flag(juego).exists(), "un flag residual pondría el juego en modo precache"
    assert recuperar_progreso(estado, inv) == progreso


def test_neutralizar_sin_estado_igual_borra_el_flag(juego: pathlib.Path, grass: pathlib.Path) -> None:
    ruta_del_flag(juego).write_text("Tamriel\n", encoding="utf-8")

    neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=None)

    assert not ruta_del_flag(juego).exists()


def test_planificar_es_puro_y_no_toca_disco(grass: pathlib.Path, juego: pathlib.Path) -> None:
    inv = _inventario(grass, juego)
    antes = sorted(p.name for p in grass.iterdir())

    with pytest.raises(GrassCacheSafetyError):
        planificar_arranque(modo="new", inventario=inv)

    assert sorted(p.name for p in grass.iterdir()) == antes
    assert not ruta_del_flag(juego).exists()
