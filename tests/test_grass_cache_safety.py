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

import sky_claw.local.tools.grass_cache_safety as gcs_mod
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
    verificar_respaldo_para_borrado,
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
    assert inv.flag_progreso == b"formato-que-no-conozco\n"


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

    plan = preparar_arranque(modo="new", game_path=juego, grass_dir=vacio, ack_usvfs_no_verificado=True)

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
    # El ack de USVFS ya está dado: lo que falta acá es dónde dejar el respaldo.
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(
            modo="rebuild",
            game_path=juego,
            grass_dir=grass,
            aprobacion=APROBACION_REBUILD,
            ack_usvfs_no_verificado=True,
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
        ack_usvfs_no_verificado=True,
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
        ack_usvfs_no_verificado=True,
    )
    aplicar_plan(plan, ruta_del_flag(juego))

    assert ruta_del_flag(juego).read_text(encoding="utf-8") == ""
    assert recuperar_progreso(estado, plan.inventario, grass_dir=grass) == progreso.encode("utf-8")


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
            ack_usvfs_no_verificado=True,
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
        modo="rebuild",
        game_path=juego,
        grass_dir=grass,
        aprobacion=APROBACION_REBUILD,
        raiz_estado=estado,
        ack_usvfs_no_verificado=True,
    )
    (grass / "aparece_despues.cgid").write_bytes(b"nueva")

    plan = preparar_arranque(
        modo="rebuild",
        game_path=juego,
        grass_dir=grass,
        aprobacion=APROBACION_REBUILD,
        raiz_estado=estado,
        ack_usvfs_no_verificado=True,
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
    archivar_progreso(estado, inv, b"Tamriel\n-2,4\n", grass_dir=grass)

    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)

    assert plan.accion == "restaurar"
    aplicar_plan(plan, ruta_del_flag(juego))
    assert ruta_del_flag(juego).read_bytes() == b"Tamriel\n-2,4\n"


def test_progreso_archivado_de_otro_cache_no_se_restaura(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # Reanudar con el progreso de otro conjunto haría que NGIO saltee celdas
    # que nunca se escribieron: mejor negarse que producir un caché incompleto.
    inv_ajeno = _inventario(grass, juego)
    archivar_progreso(estado, inv_ajeno, b"Tamriel\n0,0\n", grass_dir=grass)
    (grass / "aparece_despues.cgid").write_bytes(b"nueva")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)

    assert excinfo.value.codigo == "resume_sin_progreso"
    assert recuperar_progreso(estado, _inventario(grass, juego), grass_dir=grass) is None


def test_neutralizar_flag_archiva_y_borra(grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path) -> None:
    progreso = "Tamriel\n-2,4\n"
    ruta_del_flag(juego).write_text(progreso, encoding="utf-8")
    inv = _inventario(grass, juego)

    resultado = neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=estado)

    assert resultado.borrado is True and resultado.limpio is True
    assert resultado.archivado is True
    assert resultado.errores == ()
    assert not ruta_del_flag(juego).exists(), "un flag residual pondría el juego en modo precache"
    assert recuperar_progreso(estado, inv, grass_dir=grass) == progreso.encode("utf-8")


def test_neutralizar_sin_estado_igual_borra_el_flag(juego: pathlib.Path, grass: pathlib.Path) -> None:
    # El borrado manda (el juego no puede quedar en modo precache), pero sin
    # raíz de estado no hay dónde archivar: la pérdida se DECLARA.
    ruta_del_flag(juego).write_text("Tamriel\n", encoding="utf-8")

    resultado = neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=None)

    assert resultado.borrado is True
    assert not ruta_del_flag(juego).exists()
    assert resultado.progreso_perdido is True
    assert resultado.limpio is False
    assert any("reanudar" in error for error in resultado.errores)


def test_planificar_es_puro_y_no_toca_disco(grass: pathlib.Path, juego: pathlib.Path) -> None:
    inv = _inventario(grass, juego)
    antes = sorted(p.name for p in grass.iterdir())

    with pytest.raises(GrassCacheSafetyError):
        planificar_arranque(modo="new", inventario=inv)

    assert sorted(p.name for p in grass.iterdir()) == antes
    assert not ruta_del_flag(juego).exists()


# ---------------------------------------------------------------------------
# BLOQUEANTE 1 — el respaldo se reutilizaba comparando sólo metadatos
# ---------------------------------------------------------------------------


def test_respaldo_no_se_reutiliza_si_el_origen_cambio_con_el_mismo_tamano(
    tmp_path: pathlib.Path, juego: pathlib.Path
) -> None:
    # Arrange: respaldo de AAAA; después el origen pasa a BBBB conservando los
    # cuatro bytes (mismo nombre, mismo tamaño, contenido distinto).
    grass = tmp_path / "overwrite" / "Grass"
    archivo = _escribir(grass / "Tamriel.cgid", b"AAAA")
    primero = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=_inventario(grass, juego))
    archivo.write_bytes(b"BBBB")
    inventario_nuevo = _inventario(grass, juego)
    # La huella barata (nombre+tamaño) NO distingue los dos contenidos: por eso
    # reutilizar por huella era exactamente el defecto.
    assert inventario_nuevo.huella == primero.huella

    # Act
    segundo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inventario_nuevo)

    # Assert: el respaldo vigente tiene el contenido ACTUAL, no el histórico...
    assert (segundo.destino / "Tamriel.cgid").read_bytes() == b"BBBB"
    assert segundo.huella_contenido != primero.huella_contenido
    # ...y el respaldo viejo se conserva apartado, no se pisa ni se borra.
    invalidos = list(tmp_path.glob("respaldo.invalido-*"))
    assert invalidos, "el respaldo que ya no verifica se aparta con nombre explícito"
    assert (invalidos[0] / "Tamriel.cgid").read_bytes() == b"AAAA"


def test_respaldo_alterado_en_el_destino_no_se_reutiliza(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # Arrange: respaldo correcto de AAAA; alguien altera la copia a XXXX con el
    # mismo tamaño. El manifiesto histórico sigue diciendo AAAA.
    grass = tmp_path / "overwrite" / "Grass"
    _escribir(grass / "Tamriel.cgid", b"AAAA")
    inventario = _inventario(grass, juego)
    primero = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inventario)
    (primero.destino / "Tamriel.cgid").write_bytes(b"XXXX")

    # Act
    segundo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=inventario)

    # Assert: la copia se rehace desde el origen; XXXX no sobrevive como respaldo.
    assert (segundo.destino / "Tamriel.cgid").read_bytes() == b"AAAA"
    invalidos = list(tmp_path.glob("respaldo.invalido-*"))
    assert invalidos and (invalidos[0] / "Tamriel.cgid").read_bytes() == b"XXXX"


def test_verificacion_previa_al_borrado_rechaza_cache_cambiado(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # Arrange: respaldo verificado y después el caché cambia (mismo tamaño).
    grass = tmp_path / "overwrite" / "Grass"
    archivo = _escribir(grass / "Tamriel.cgid", b"AAAA")
    respaldo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=_inventario(grass, juego))
    archivo.write_bytes(b"ZZZZ")

    # Act / Assert: el borrado NO se autoriza.
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        verificar_respaldo_para_borrado(grass_dir=grass, respaldo=respaldo)

    assert excinfo.value.codigo == "respaldo_desactualizado"


def test_verificacion_previa_al_borrado_rechaza_respaldo_alterado(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    grass = tmp_path / "overwrite" / "Grass"
    _escribir(grass / "Tamriel.cgid", b"AAAA")
    respaldo = respaldar_cache(grass_dir=grass, destino=tmp_path / "respaldo", inventario=_inventario(grass, juego))
    (respaldo.destino / "Tamriel.cgid").write_bytes(b"XXXX")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        verificar_respaldo_para_borrado(grass_dir=grass, respaldo=respaldo)

    assert excinfo.value.codigo == "respaldo_alterado"


# ---------------------------------------------------------------------------
# BLOQUEANTE 2 — la raíz del caché podía ser un enlace a un árbol externo
# ---------------------------------------------------------------------------


def test_cache_raiz_enlazada_se_rechaza(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # Arrange: overwrite/Grass es un symlink a un directorio FUERA del rig.
    externo = tmp_path / "externo"
    _escribir(externo / "Tamriel.cgid", b"ajeno")
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    enlace = overwrite / "Grass"
    try:
        enlace.symlink_to(externo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    # Act / Assert: ni inventariar ni respaldar atraviesan la redirección.
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        inventariar_cache(grass_dir=enlace, flag_path=ruta_del_flag(juego))
    assert excinfo.value.codigo == "cache_raiz_enlazada"

    with pytest.raises(GrassCacheSafetyError) as excinfo_respaldo:
        respaldar_cache(grass_dir=enlace, destino=tmp_path / "respaldo", inventario=_inventario(externo, juego))
    assert excinfo_respaldo.value.codigo == "cache_raiz_enlazada"
    assert not (tmp_path / "respaldo").exists()


def test_componente_intermedio_enlazado_se_rechaza(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # El enlace no está en la raíz del caché ni en un archivo: está en un
    # ANCESTRO. Es el caso que un chequeo del path final no ve.
    real = tmp_path / "real"
    (real / "Grass").mkdir(parents=True)
    _escribir(real / "Grass" / "Tamriel.cgid", b"externo")
    mo2 = tmp_path / "mo2"
    try:
        mo2.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        inventariar_cache(grass_dir=mo2 / "Grass", flag_path=ruta_del_flag(juego))

    assert excinfo.value.codigo == "cache_raiz_enlazada"


# ---------------------------------------------------------------------------
# CORRECCIÓN 3 — el progreso se decodificaba con errors="replace"
# ---------------------------------------------------------------------------


def test_flag_con_bytes_no_utf8_se_preserva_byte_a_byte(
    tmp_path: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # Arrange: bytes que NO son UTF-8 válido. Decodificar con "replace" los
    # cambiaría (U+FFFD) y la reanudación de NGIO dejaría de coincidir.
    grass = tmp_path / "overwrite" / "Grass"
    crudo = b"\xff\xfeTamriel\x00\x80\r\n-2,4\r\n"
    ruta_del_flag(juego).write_bytes(crudo)
    inventario = _inventario(grass, juego)
    assert inventario.flag_progreso == crudo

    # Act
    archivar_progreso(estado, inventario, inventario.flag_progreso or b"", grass_dir=grass)
    recuperado = recuperar_progreso(estado, inventario, grass_dir=grass)

    # Assert
    assert recuperado == crudo


def test_resume_restaura_bytes_no_utf8_sin_recodificar(
    tmp_path: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    grass = tmp_path / "overwrite" / "Grass"
    crudo = b"\xff\xfe\n"
    inventario = _inventario(grass, juego)
    archivar_progreso(estado, inventario, crudo, grass_dir=grass)

    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)
    aplicar_plan(plan, ruta_del_flag(juego))

    assert ruta_del_flag(juego).read_bytes() == crudo


def test_progreso_archivado_corrupto_no_se_restaura(
    tmp_path: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # Un archivo de progreso manipulado no se restaura "a medias": el sha256
    # archivado tiene que sobrevivir el round-trip.
    import base64

    grass = tmp_path / "overwrite" / "Grass"
    inventario = _inventario(grass, juego)
    ruta = archivar_progreso(estado, inventario, b"Tamriel\n", grass_dir=grass)
    carga = json.loads(ruta.read_text(encoding="utf-8"))
    carga["contenido_b64"] = base64.b64encode(b"otra-cosa\n").decode("ascii")
    ruta.write_text(json.dumps(carga), encoding="utf-8")

    assert recuperar_progreso(estado, inventario, grass_dir=grass) is None
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)
    assert excinfo.value.codigo == "resume_sin_progreso"


# ---------------------------------------------------------------------------
# CORRECCIÓN 4 — la limpieza del flag no puede mentir
# ---------------------------------------------------------------------------


def test_neutralizacion_reporta_fallo_de_borrado(
    tmp_path: pathlib.Path, juego: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: el borrado falla (archivo en uso / ACL), que es el caso real de
    # Windows con el juego o un antivirus reteniendo el archivo.
    ruta_del_flag(juego).write_bytes(b"Tamriel\n")
    grass = tmp_path / "overwrite" / "Grass"
    real_unlink = pathlib.Path.unlink

    def _unlink_que_falla(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self.name == "PrecacheGrass.txt":
            raise PermissionError("el archivo está en uso")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_que_falla)

    # Act
    resultado = neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=None)

    # Assert: NO se declara limpio, y el error queda trazado con su causa.
    assert resultado.borrado is False
    assert resultado.limpio is False
    assert any("No se pudo eliminar" in error for error in resultado.errores)
    assert ruta_del_flag(juego).exists()


def test_neutralizacion_no_borra_un_flag_que_es_un_enlace(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # Un "flag" que en realidad es un enlace no es el de NGIO: se informa y no
    # se borra (podría ser un archivo del usuario apuntado desde otro lado).
    externo = tmp_path / "ajeno.txt"
    externo.write_bytes(b"no tocar")
    enlace = ruta_del_flag(juego)
    try:
        enlace.symlink_to(externo)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    resultado = neutralizar_flag(flag_path=enlace, grass_dir=tmp_path / "overwrite" / "Grass", raiz_estado=None)

    assert resultado.borrado is False
    assert any("enlace" in error for error in resultado.errores)
    assert externo.read_bytes() == b"no tocar"


# ---------------------------------------------------------------------------
# COBERTURA USVFS — HOLD explícito, no silencio
# ---------------------------------------------------------------------------


def test_rebuild_sin_ack_de_usvfs_queda_en_hold(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # Arrange: el respaldo cubriría el caché físico, pero no el overlay de mods.
    estado = tmp_path / "estado"

    # Act / Assert: HOLD por defecto, y sin tocar el disco (no se respalda nada).
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(
            modo="rebuild",
            game_path=juego,
            grass_dir=grass,
            aprobacion=APROBACION_REBUILD,
            raiz_estado=estado,
        )

    assert excinfo.value.codigo == "usvfs_no_demostrado"
    assert "USVFS" in str(excinfo.value)
    assert not estado.exists(), "un HOLD no debe escribir nada"
    assert grass.is_dir()


def test_rebuild_con_ack_explicito_deja_de_estar_en_hold(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    plan = preparar_arranque(
        modo="rebuild",
        game_path=juego,
        grass_dir=grass,
        aprobacion=APROBACION_REBUILD,
        raiz_estado=tmp_path / "estado",
        ack_usvfs_no_verificado=True,
    )

    assert plan.modo == "rebuild"
    assert plan.a_dict()["cobertura_usvfs"] == "no_verificada"


# ---------------------------------------------------------------------------
# SEGUNDA REVISIÓN (b11860f) — B1…B6
# ---------------------------------------------------------------------------


def test_new_sin_ack_de_usvfs_queda_en_hold(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # B5: NEW también escribe el flag vacío, y el borrado de NGIO alcanza el
    # overlay de los mods —que este inventario NO ve—. Sin reconocimiento, HOLD.
    vacio = tmp_path / "overwrite" / "Grass"

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="new", game_path=juego, grass_dir=vacio)

    assert excinfo.value.codigo == "usvfs_no_demostrado"
    assert "USVFS" in str(excinfo.value)
    assert not ruta_del_flag(juego).exists(), "un HOLD no escribe el flag"


def test_aplicar_plan_no_escribe_a_traves_de_un_temporal_enlazado(tmp_path: pathlib.Path, juego: pathlib.Path) -> None:
    # B2: el temporal tenía nombre previsible; un enlace ahí redirigía la
    # escritura fuera del directorio del juego.
    victima = tmp_path / "victima.txt"
    victima.write_bytes(b"contenido ajeno que no se toca")
    trampa = ruta_del_flag(juego).with_name("PrecacheGrass.txt.tmp")
    try:
        trampa.symlink_to(victima)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")
    plan = preparar_arranque(
        modo="new",
        game_path=juego,
        grass_dir=tmp_path / "overwrite" / "Grass",
        ack_usvfs_no_verificado=True,
    )

    aplicar_plan(plan, ruta_del_flag(juego))

    assert victima.read_bytes() == b"contenido ajeno que no se toca"
    assert ruta_del_flag(juego).read_bytes() == b""


def test_aplicar_plan_rechaza_un_temporal_ya_existente(
    tmp_path: pathlib.Path, juego: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B2: crear el temporal tiene que ser EXCLUSIVO. Con el nombre fijado a
    # mano, un archivo/enlace preexistente no puede ser abierto ni escrito.
    victima = tmp_path / "victima.txt"
    victima.write_bytes(b"intacto")
    fijo = ruta_del_flag(juego).with_name(".PrecacheGrass.txt.fijo.tmp")
    try:
        fijo.symlink_to(victima)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")
    monkeypatch.setattr(gcs_mod, "_ruta_temporal", lambda flag: fijo)
    plan = preparar_arranque(
        modo="new",
        game_path=juego,
        grass_dir=tmp_path / "overwrite" / "Grass",
        ack_usvfs_no_verificado=True,
    )

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        aplicar_plan(plan, ruta_del_flag(juego))

    assert excinfo.value.codigo == "flag_temporal_inseguro"
    assert victima.read_bytes() == b"intacto"
    assert not ruta_del_flag(juego).exists()


def test_aplicar_plan_verifica_que_la_sustitucion_ocurrio(
    tmp_path: pathlib.Path, juego: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B2: si el rename se pierde (o alguien repone otra cosa), el flag NO quedó
    # escrito: hay que decirlo, no asumir éxito.
    plan = preparar_arranque(
        modo="new",
        game_path=juego,
        grass_dir=tmp_path / "overwrite" / "Grass",
        ack_usvfs_no_verificado=True,
    )
    monkeypatch.setattr(gcs_mod.os, "replace", lambda src, dst: None)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        aplicar_plan(plan, ruta_del_flag(juego))

    assert excinfo.value.codigo == "flag_no_verificado"
    assert not ruta_del_flag(juego).exists()


def test_neutralizacion_con_clasificacion_fallida_no_borra(
    tmp_path: pathlib.Path, juego: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B3: "no pude clasificar" no es "es un archivo normal". Sin saber qué es,
    # no se borra.
    ruta_del_flag(juego).write_bytes(b"Tamriel\n-2,4\n")

    def _clasificar_falla(ruta: pathlib.Path) -> str | None:
        raise PermissionError("bloqueado por el indexador")

    monkeypatch.setattr(gcs_mod, "link_kind_or_raise", _clasificar_falla)

    resultado = neutralizar_flag(
        flag_path=ruta_del_flag(juego), grass_dir=tmp_path / "overwrite" / "Grass", raiz_estado=None
    )

    assert resultado.borrado is False
    assert resultado.limpio is False
    assert any("clasificar" in error for error in resultado.errores)
    assert ruta_del_flag(juego).exists()


def test_si_falla_el_archivado_no_se_declara_limpio(
    tmp_path: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B4: el flag se borra igual (el juego del usuario no puede quedar en modo
    # precache) pero la única evidencia de reanudación se perdió: eso es una
    # limpieza INCOMPLETA, no un éxito.
    grass = tmp_path / "overwrite" / "Grass"
    _escribir(grass / "Tamriel.cgid", b"celda")
    ruta_del_flag(juego).write_bytes(b"Tamriel\n-2,4\n")

    def _archivar_falla(*args: object, **kwargs: object) -> pathlib.Path:
        raise OSError("no queda espacio en el disco")

    monkeypatch.setattr(gcs_mod, "archivar_progreso", _archivar_falla)

    resultado = neutralizar_flag(flag_path=ruta_del_flag(juego), grass_dir=grass, raiz_estado=estado)

    assert resultado.borrado is True
    assert resultado.progreso_perdido is True
    assert resultado.limpio is False
    assert any("archivar" in error.lower() for error in resultado.errores)
    assert not ruta_del_flag(juego).exists()


def test_rebuild_no_pisa_el_progreso_si_no_puede_archivarlo(
    grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B4 (auditoría del suppress(OSError)): REBUILD pisa el flag con progreso.
    # Si ese progreso no se puede archivar, se niega en vez de perderlo.
    progreso = b"Tamriel\n-2,4\n"
    ruta_del_flag(juego).write_bytes(progreso)

    def _archivar_falla(*args: object, **kwargs: object) -> pathlib.Path:
        raise OSError("no queda espacio en el disco")

    monkeypatch.setattr(gcs_mod, "archivar_progreso", _archivar_falla)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(
            modo="rebuild",
            game_path=juego,
            grass_dir=grass,
            aprobacion=APROBACION_REBUILD,
            raiz_estado=estado,
            ack_usvfs_no_verificado=True,
        )

    assert excinfo.value.codigo == "progreso_no_archivado"
    assert ruta_del_flag(juego).read_bytes() == progreso, "el flag no se pisó"


def test_no_se_restaura_progreso_de_un_cache_con_mismo_tamano_y_otro_contenido(
    tmp_path: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # B6: la huella de (nombre, tamaño) no distingue contenido. Si el .cgid
    # cambió conservando el tamaño, ese progreso es de OTRO caché: reanudar con
    # él haría que NGIO saltara celdas que nunca se escribieron.
    grass = tmp_path / "overwrite" / "Grass"
    _escribir(grass / "Tamriel.cgid", b"celda-tamriel")
    ruta_del_flag(juego).write_bytes(b"Tamriel\n-2,4\n")
    inventario = _inventario(grass, juego)
    archivar_progreso(estado, inventario, b"Tamriel\n-2,4\n", grass_dir=grass)
    ruta_del_flag(juego).unlink()
    _escribir(grass / "Tamriel.cgid", b"celda-TAMRIEL")
    assert _inventario(grass, juego).huella == inventario.huella, "mismo nombre y tamaño"

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(modo="resume", game_path=juego, grass_dir=grass, raiz_estado=estado)

    assert excinfo.value.codigo == "progreso_de_otro_cache"
    assert not ruta_del_flag(juego).exists(), "no se restauró nada"


# ---------------------------------------------------------------------------
# TERCERA REVISIÓN (73ec59e) — B7, B8
# ---------------------------------------------------------------------------


def test_archivado_no_sobrescribe_victima_de_temporal_enlazado(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # B7: el temporal del archivo de progreso era `<huella>.json.tmp`, nombre
    # derivable del estado del caché. Un enlace ahí hacía que el JSON de
    # evidencia se escribiera SOBRE un archivo ajeno.
    victima = tmp_path / "victima.txt"
    victima.write_bytes(b"contenido ajeno")
    inventario = _inventario(grass, juego)
    trampa = estado / "progreso" / f"{inventario.huella}.json.tmp"
    trampa.parent.mkdir(parents=True)
    try:
        trampa.symlink_to(victima)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    archivar_progreso(estado, inventario, b"Tamriel\n-2,4\n", grass_dir=grass)

    assert victima.read_bytes() == b"contenido ajeno"
    evidencia = estado / "progreso" / f"{inventario.huella}.json"
    assert evidencia.is_file(), "la evidencia se archiva igual, con un temporal propio"


def test_no_quedan_temporales_de_nombre_predecible_en_el_modulo() -> None:
    # B7 (respaldo): `.manifiesto.json.tmp` era el mismo patrón. La ancla mira
    # el código fuente: cualquier escritura de metadatos tiene que pasar por el
    # temporal exclusivo, no por un nombre que otro pueda ocupar.
    fuente = pathlib.Path(gcs_mod.__file__).read_text(encoding="utf-8")

    assert ".json.tmp" not in fuente
    assert ".manifiesto.json.tmp" not in fuente


def test_raiz_de_estado_enlazada_se_rechaza(tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path) -> None:
    # B7: la raíz de estado también es una ruta de escritura: si es un enlace,
    # la evidencia se archiva fuera del árbol de Sky-Claw.
    real = tmp_path / "estado_real"
    real.mkdir()
    enlace = tmp_path / "estado"
    try:
        enlace.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")
    inventario = _inventario(grass, juego)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        archivar_progreso(enlace, inventario, b"Tamriel\n", grass_dir=grass)

    assert excinfo.value.codigo == "estado_raiz_enlazada"
    assert not list(real.iterdir()), "no se escribió nada a través del enlace"


def test_resume_rechaza_flag_vaciado_despues_del_preflight(grass: pathlib.Path, juego: pathlib.Path) -> None:
    # B8: entre el preflight y el arranque, el flag pudo vaciarse (otro proceso,
    # el propio usuario). RESUME no puede asumir que sigue como lo dejó.
    ruta_del_flag(juego).write_bytes(b"Tamriel\n-2,4\n")
    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass)
    assert plan.accion == "conservar"
    ruta_del_flag(juego).write_bytes(b"")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        aplicar_plan(plan, ruta_del_flag(juego))

    assert excinfo.value.codigo == "flag_cambio_despues_del_preflight"
    assert ruta_del_flag(juego).read_bytes() == b"", "no se toca lo que cambió otro"


def test_resume_rechaza_flag_borrado_despues_del_preflight(grass: pathlib.Path, juego: pathlib.Path) -> None:
    ruta_del_flag(juego).write_bytes(b"Tamriel\n-2,4\n")
    plan = preparar_arranque(modo="resume", game_path=juego, grass_dir=grass)
    ruta_del_flag(juego).unlink()

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        aplicar_plan(plan, ruta_del_flag(juego))

    assert excinfo.value.codigo == "flag_cambio_despues_del_preflight"
    assert not ruta_del_flag(juego).exists()


# ---------------------------------------------------------------------------
# CUARTA REVISIÓN — B10: la cadena física del destino, no sólo la raíz
# ---------------------------------------------------------------------------


def test_archivado_rechaza_subdirectorio_de_progreso_enlazado(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # B10: validar `raiz_estado` no alcanza: `<estado>/progreso` es OTRO
    # componente de la ruta de escritura, y un enlace ahí saca el JSON de
    # evidencia del árbol administrado.
    externo = tmp_path / "externo"
    externo.mkdir()
    estado.mkdir()
    try:
        (estado / "progreso").symlink_to(externo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")
    inventario = _inventario(grass, juego)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        archivar_progreso(estado, inventario, b"Tamriel\n-2,4\n", grass_dir=grass)

    assert excinfo.value.codigo == "progreso_raiz_enlazada"
    assert not list(externo.iterdir()), "no se escribe fuera de la raíz administrada"


def test_recuperar_rechaza_subdirectorio_de_progreso_enlazado(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path, estado: pathlib.Path
) -> None:
    # El mismo enlace, del lado de la lectura: no se acepta evidencia que viva
    # fuera del árbol administrado.
    externo = tmp_path / "externo"
    externo.mkdir()
    estado.mkdir()
    inventario = _inventario(grass, juego)
    ajeno = externo / f"{inventario.huella}.json"
    ajeno.write_text(
        json.dumps(
            {
                "version": 1,
                "huella": inventario.huella,
                "huella_contenido": "x",
                "contenido_b64": "VGFtcmllbAo=",
                "contenido_sha256": "0" * 64,
                "contenido_bytes": 8,
            }
        ),
        encoding="utf-8",
    )
    try:
        (estado / "progreso").symlink_to(externo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        recuperar_progreso(estado, inventario, grass_dir=grass)

    assert excinfo.value.codigo == "progreso_raiz_enlazada"


def test_archivado_rechaza_un_ancestro_intermedio_enlazado(
    tmp_path: pathlib.Path, grass: pathlib.Path, juego: pathlib.Path
) -> None:
    # El enlace no está ni en la raíz del estado ni en `progreso`: está en un
    # ANCESTRO de la raíz. La validación cubre la cadena completa.
    externo = tmp_path / "externo"
    externo.mkdir()
    try:
        (tmp_path / "enlazado").symlink_to(externo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear el enlace (permisos).")
    inventario = _inventario(grass, juego)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        archivar_progreso(tmp_path / "enlazado" / "estado", inventario, b"Tamriel\n", grass_dir=grass)

    assert excinfo.value.codigo == "estado_raiz_enlazada"
    assert not list(externo.iterdir())
