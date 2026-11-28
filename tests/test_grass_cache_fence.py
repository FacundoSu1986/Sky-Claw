"""Tests del fence por recurso (I1/I6/I7) — Fase F."""

from __future__ import annotations

import json
import os
import pathlib

import psutil
import pytest

from sky_claw.local.tools.grass_cache_fence import (
    FenceCorruptoError,
    FenceProceso,
    FenceScope,
    adquirir_fence,
    bloqueo_por_fence_scope,
    leer_fence,
    liberar_fence_scope,
    ruta_de_recurso,
)


def _scope(tmp_path: pathlib.Path, *, juego: str = "game", cache: str = "overwrite/Grass") -> FenceScope:
    raiz = tmp_path / "entorno"
    return FenceScope.desde_config(
        game_path=raiz / juego,
        overwrite_grass_dir=raiz / cache,
    )


def _adquirir(tmp_path: pathlib.Path, scope: FenceScope, run_id: str = "run-1", intento: int = 0):
    return adquirir_fence(
        tmp_path / "estado",
        scope,
        run_id=run_id,
        intento=intento,
        lanzamiento="solicitado",
        pid=None,
        create_time=None,
        modo="new",
        creado="2026-11-26T00:00:00",
    )


def test_scope_cubre_juego_cache_y_mo2(tmp_path: pathlib.Path) -> None:
    """I1: el scope protege los tres recursos reales (incluida la raíz MO2 del perfil temporal)."""
    scope = _scope(tmp_path)

    recursos = [rid for rid, _ in scope.recursos()]

    assert any(r.startswith("game:") for r in recursos)
    assert any(r.startswith("cache:") for r in recursos)
    assert any(r.startswith("mo2:") for r in recursos), "el perfil temporal/mod de config comparten raíz MO2"


def test_adquisicion_exclusiva_no_sobrescribe_marcador_ajeno(tmp_path: pathlib.Path) -> None:
    """I6: si ya existe un marcador de otra corrida, se bloquea y no se sobrescribe."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    segunda = _adquirir(tmp_path, scope, run_id="run-2")

    assert segunda.ok is False
    # `segunda.recursos` está vacío cuando la adquisición se bloquea: el id del recurso sale del
    # SCOPE, no del resultado fallido.
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    ruta = ruta_de_recurso(tmp_path / "estado", cache)
    assert leer_fence(ruta) is not None and leer_fence(ruta).run_id == "run-1"


def test_adquisicion_parcial_compensa_solo_lo_propio(tmp_path: pathlib.Path) -> None:
    """C1: comparte un recurso (cache) con otra corrida pero no el juego; se compensa lo propio."""
    comun = _scope(tmp_path, juego="game-a", cache="overwrite/Grass")
    assert _adquirir(tmp_path, comun, run_id="run-1").ok

    otra = _scope(tmp_path, juego="game-b", cache="overwrite/Grass")
    resultado = _adquirir(tmp_path, otra, run_id="run-2")

    assert resultado.ok is False
    # El recurso exclusivo de la corrida nueva (game-b) se compensó; el ajeno (cache) sigue intacto.
    game_b = [r for r in otra.recursos() if r[0].startswith("game:")][0][0]
    assert not ruta_de_recurso(tmp_path / "estado", game_b).exists()
    cache = [r for r in otra.recursos() if r[0].startswith("cache:")][0][0]
    persistido = leer_fence(ruta_de_recurso(tmp_path / "estado", cache))
    assert persistido is not None and persistido.run_id == "run-1"


def test_consulta_es_solo_lectura(tmp_path: pathlib.Path) -> None:
    """I7: la consulta bloquea y NO borra el marcador, aunque el PID esté muerto."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok
    ruta = ruta_de_recurso(tmp_path / "estado", [r for r in scope.recursos() if r[0].startswith("cache:")][0][0])

    bloqueo = bloqueo_por_fence_scope(tmp_path / "estado", scope)

    assert bloqueo is not None
    assert ruta.exists(), "la consulta no libera"


def test_marcador_corrupto_bloquea(tmp_path: pathlib.Path) -> None:
    """I6: marcador ilegible ⇒ bloqueo, nunca «no existe»."""
    scope = _scope(tmp_path)
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    ruta = ruta_de_recurso(tmp_path / "estado", cache)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"{no es json")

    assert bloqueo_por_fence_scope(tmp_path / "estado", scope) is not None


def test_enlace_colgante_no_es_ausencia(tmp_path: pathlib.Path) -> None:
    """I6: un enlace (aunque su destino no exista) no equivale a «sin marcador»."""
    scope = _scope(tmp_path)
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    ruta = ruta_de_recurso(tmp_path / "estado", cache)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    destino = tmp_path / "inexistente.json"
    try:
        os.symlink(destino, ruta)
    except (OSError, NotImplementedError):
        pytest.skip("el sistema no permite crear enlaces sin privilegios")

    assert bloqueo_por_fence_scope(tmp_path / "estado", scope) is not None


def test_liberacion_requiere_autorizacion_explicita(tmp_path: pathlib.Path) -> None:
    """I5: sin autorización del dueño de los leases no se borra nada."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    liberacion = liberar_fence_scope(tmp_path / "estado", scope, run_id="run-1", autorizado=False)

    assert liberacion.ok is False
    assert liberacion.conservados


def test_liberacion_no_borra_marcador_ajeno(tmp_path: pathlib.Path) -> None:
    """I6: con run_id distinto no se elimina el marcador."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    liberacion = liberar_fence_scope(tmp_path / "estado", scope, run_id="run-2", autorizado=True)

    assert liberacion.ok is False
    assert any("pertenece a run-1" in c for c in liberacion.conservados)


def test_liberacion_propia_verifica_el_borrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """I6: un unlink que falla no se reporta como liberado."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    def _falla(self: pathlib.Path, missing_ok: bool = False) -> None:
        raise OSError(13, "acceso denegado")

    monkeypatch.setattr(pathlib.Path, "unlink", _falla)

    liberacion = liberar_fence_scope(tmp_path / "estado", scope, run_id="run-1", autorizado=True)

    assert liberacion.ok is False
    assert liberacion.fallos


def test_liberacion_propia_libera_todos_los_recursos(tmp_path: pathlib.Path) -> None:
    """Cierre demostrado: se liberan los tres marcadores propios y la consulta deja de bloquear."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    liberacion = liberar_fence_scope(tmp_path / "estado", scope, run_id="run-1", autorizado=True)

    assert liberacion.ok is True
    assert len(liberacion.liberados) == len(scope.recursos())
    assert bloqueo_por_fence_scope(tmp_path / "estado", scope) is None


def test_actualizacion_conserva_marcador_anterior_si_falla(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: un fallo de actualización conserva el marcador conservador anterior."""
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok

    def _falla(*args: object, **kwargs: object) -> None:
        raise OSError(28, "sin espacio")

    monkeypatch.setattr("sky_claw.local.tools.grass_cache_fence._escribir_json_atomico", _falla)

    resultado = _adquirir(tmp_path, scope, run_id="run-1", intento=1)

    assert resultado.ok is False
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    persistido = leer_fence(ruta_de_recurso(tmp_path / "estado", cache))
    assert persistido is not None and persistido.run_id == "run-1" and persistido.intento == 0


def test_pid_vivo_se_reporta_pero_no_libera(tmp_path: pathlib.Path) -> None:
    """I7: el estado del PID es diagnóstico; no habilita liberación."""
    scope = _scope(tmp_path)
    ruta = ruta_de_recurso(tmp_path / "estado", [r for r in scope.recursos() if r[0].startswith("cache:")][0][0])
    ruta.parent.mkdir(parents=True, exist_ok=True)
    marcador = json.dumps(
        {
            "run_id": "run-1",
            "intento": 0,
            "lanzamiento": "atribuido",
            "pid": os.getpid(),
            "create_time": psutil.Process().create_time(),
            "modo": "new",
            "game_path": "g",
            "overwrite_grass_dir": "c",
            "creado": "2026-11-26T00:00:00",
            "recurso": "cache:x",
        }
    )
    ruta.write_bytes(marcador.encode("utf-8"))

    assert bloqueo_por_fence_scope(tmp_path / "estado", scope) is not None
    assert ruta.exists()


# ---------------------------------------------------------------------------
# Brechas de validación C3/C4 exigidas por el propietario de Fase F
# ---------------------------------------------------------------------------


def test_mismo_skyrim_con_distintos_caches_bloquea_por_el_recurso_juego(tmp_path: pathlib.Path) -> None:
    """C3/C4: mismo Skyrim con cachés distintos ⇒ la exclusión por ``game`` es efectiva.

    Las raíces MO2 también difieren, así que el ÚNICO recurso físico compartido es el juego. Si el
    fence no cubriera ``game``, la segunda adquisición pasaría y dos rituales usarían el mismo
    Skyrim a la vez.
    """
    primero = FenceScope.desde_config(
        game_path=tmp_path / "game",
        overwrite_grass_dir=tmp_path / "mo2-a" / "overwrite" / "Grass",
    )
    segundo_scope = FenceScope.desde_config(
        game_path=tmp_path / "game",
        overwrite_grass_dir=tmp_path / "mo2-b" / "overwrite" / "Grass",
    )
    comunes = {rid for rid, _ in primero.recursos()} & {rid for rid, _ in segundo_scope.recursos()}
    assert comunes and all(rid.startswith("game:") for rid in comunes), "sólo el juego es compartido"

    assert _adquirir(tmp_path, primero, run_id="run-1").ok

    segunda = _adquirir(tmp_path, segundo_scope, run_id="run-2")

    assert segunda.ok is False, "el juego compartido impide el segundo ritual"
    marcador = leer_fence(ruta_de_recurso(tmp_path / "estado", next(iter(comunes))))
    assert marcador is not None and marcador.run_id == "run-1", "el marcador ajeno no se sobrescribe"


def test_recursos_fisicos_independientes_no_producen_bloqueo_falso(tmp_path: pathlib.Path) -> None:
    """C4: dos scopes sin NINGÚN recurso físico en común no producen un falso bloqueo."""
    primero = FenceScope.desde_config(
        game_path=tmp_path / "game-a",
        overwrite_grass_dir=tmp_path / "mo2-a" / "overwrite" / "Grass",
    )
    segundo_scope = FenceScope.desde_config(
        game_path=tmp_path / "game-b",
        overwrite_grass_dir=tmp_path / "mo2-b" / "overwrite" / "Grass",
    )
    ids_a = {rid for rid, _ in primero.recursos()}
    ids_b = {rid for rid, _ in segundo_scope.recursos()}
    assert not ids_a & ids_b, "el caso exige recursos completamente independientes"

    assert _adquirir(tmp_path, primero, run_id="run-1").ok
    assert _adquirir(tmp_path, segundo_scope, run_id="run-2").ok, "no hay bloqueo falso"

    estado = tmp_path / "estado"
    assert bloqueo_por_fence_scope(estado, primero) is not None
    assert bloqueo_por_fence_scope(estado, segundo_scope) is not None
    for rid in ids_a:
        marcador = leer_fence(ruta_de_recurso(estado, rid))
        assert marcador is not None and marcador.run_id == "run-1"
    for rid in ids_b:
        marcador = leer_fence(ruta_de_recurso(estado, rid))
        assert marcador is not None and marcador.run_id == "run-2"


def test_recursos_mo2_compartidos_bloquean_aunque_juego_y_cache_difieran(tmp_path: pathlib.Path) -> None:
    """C3/C4: el perfil temporal y el mod de config comparten raíz MO2 y NO están en el DTO.

    Juego y caché son distintos: el ÚNICO recurso común es ``mo2``. Omitirlo por no venir en la
    configuración autorizaría dos rituales sobre el mismo clon y el mismo mod de configuración.
    """
    primero = FenceScope.desde_config(
        game_path=tmp_path / "game-a",
        overwrite_grass_dir=tmp_path / "mo2" / "overwrite" / "Grass-A",
    )
    segundo_scope = FenceScope.desde_config(
        game_path=tmp_path / "game-b",
        overwrite_grass_dir=tmp_path / "mo2" / "overwrite" / "Grass-B",
    )
    comunes = {rid for rid, _ in primero.recursos()} & {rid for rid, _ in segundo_scope.recursos()}
    assert comunes and all(rid.startswith("mo2:") for rid in comunes), "sólo MO2 es compartido"

    assert _adquirir(tmp_path, primero, run_id="run-1").ok

    segunda = _adquirir(tmp_path, segundo_scope, run_id="run-2")

    assert segunda.ok is False, "la raíz MO2 compartida impide el segundo ritual"
    marcador = leer_fence(ruta_de_recurso(tmp_path / "estado", next(iter(comunes))))
    assert marcador is not None and marcador.run_id == "run-1"


def test_marcador_sustituido_antes_de_liberar_no_se_elimina(tmp_path: pathlib.Path) -> None:
    """C2/I6: se RELEE el marcador persistido; si otra corrida lo sustituyó, se conserva.

    El dueño legítimo pide liberar con ``run_id='run-1'``, pero en disco ya hay un marcador de
    ``run-2``. Liberar por la identidad en memoria borraría el fence ajeno (C2 exige releer).
    """
    scope = _scope(tmp_path)
    assert _adquirir(tmp_path, scope, run_id="run-1").ok
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    ruta = ruta_de_recurso(tmp_path / "estado", cache)
    ajeno = FenceProceso(
        run_id="run-2",
        intento=0,
        lanzamiento="atribuido",
        pid=None,
        create_time=None,
        modo="new",
        game_path="g",
        overwrite_grass_dir="c",
        creado="2026-11-27T00:00:00",
        recurso=cache,
    )
    ruta.write_bytes(json.dumps(ajeno.a_dict(), ensure_ascii=False).encode("utf-8"))

    liberacion = liberar_fence_scope(tmp_path / "estado", scope, run_id="run-1", autorizado=True)

    assert liberacion.ok is False
    assert any("pertenece a run-2" in c for c in liberacion.conservados)
    persistido = leer_fence(ruta)
    assert persistido is not None and persistido.run_id == "run-2", "no se elimina el marcador ajeno"


def test_primitiva_enlace_colgante_es_bloqueo_sin_crear_symlink(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I6/C1: complemento DETERMINISTA del test de enlace colgante (SKIPPED en este host).

    ``test_enlace_colgante_no_es_ausencia`` no puede crear symlinks sin privilegios y queda
    SKIPPED; NO se contabiliza como PASS. Acá se fuerza la MISMA primitiva (``Path.is_symlink`` →
    ``FenceCorruptoError`` → bloqueo conservador) sin elevar privilegios ni tocar la seguridad
    del host.
    """
    scope = _scope(tmp_path)
    cache = [r for r in scope.recursos() if r[0].startswith("cache:")][0][0]
    ruta = ruta_de_recurso(tmp_path / "estado", cache)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"{}")
    real_is_symlink = pathlib.Path.is_symlink

    def _es_enlace(candidato: pathlib.Path) -> bool:
        return True if candidato == ruta else real_is_symlink(candidato)

    monkeypatch.setattr(pathlib.Path, "is_symlink", _es_enlace)

    with pytest.raises(FenceCorruptoError, match="es un enlace"):
        leer_fence(ruta)
    assert bloqueo_por_fence_scope(tmp_path / "estado", scope) is not None
