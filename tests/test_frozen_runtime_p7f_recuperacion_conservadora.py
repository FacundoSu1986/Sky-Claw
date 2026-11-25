"""P7-F Incremento 1 — la recuperación sólo restaura lo que puede PROBAR que escribió.

Informe: ``P7F_INDEPENDENT_RECOVERY_DESIGN`` (Alternativa B, contrato conservador).
ADR 0012 §36.9/§36.10 (nunca revertir automáticamente sin evidencia).

La propiedad del mecanismo que estos tests anclan:

    *una superficie persistida se restaura sólo si su contenido actual es
    idéntico, byte a byte, a la huella que el ESCRITOR de esta transición
    publicó; en ``config`` esa huella además tiene que diferir del previo sólo
    en ``skyrim_path``. Sin huella no hay autoría. La decisión es de CONJUNTO:
    se clasifica todo antes de escribir, y si una superficie no es atribuible
    no se restaura ninguna.*

La decisión de conjunto NO es una transacción atómica: la restauración son
escrituras independientes. Lo que se garantiza es que no se empieza a escribir
sobre una clasificación que ya dice «hay algo ajeno», y que una anomalía a mitad
de camino (fallo, carrera) detiene las escrituras restantes y queda declarada.

Defectos que cerraba esto (reproducidos en el laboratorio P7-F):

- ``config.toml``: un comentario, espacios o una sustitución equivalente se
  atribuían a la transición porque el TOML parseado era igual, y se perdían.
- ``active.json``: un ``updated_at_ns`` ajeno con los mismos IDs se pisaba.
- en frío, el INI quedaba en el Clone y config/active volvían al previo
  (estado partido).

Las pruebas de reinicio usan un SUBPROCESO que muere con ``os._exit``: un
``except`` en el mismo proceso no demuestra nada sobre la recuperación en frío.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from unittest.mock import patch

import pytest

from sky_claw.config import lock_de_archivo_config
from sky_claw.local import local_config
from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder
from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime.journal import JournalDisposition, cargar_journal_transicion
from sky_claw.local.frozen_runtime.mo2_ini_escritura import Mo2IniEscrituraError, actualizar_game_path_en_ini
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.storage import active_state_path, initialize_frozen_runtime_storage
from tests.test_frozen_runtime_p5c1_binder_integrity import (
    _correr_hasta_crashear,
    _crear_config,
    _crear_mo2_instance_sintetica,
    _ejecutar,
    _montar_rig,
    _probes_quiescentes,
    _Rig,
)

_REPO = pathlib.Path(__file__).resolve().parent.parent
_TID = "55555555-5555-4555-8555-555555555555"
_GEN = "1.6.1170__06854f02338c"
_COMENTARIO_AJENO = b"\n# escritura ajena del escenario\n"


@pytest.fixture(autouse=True)
def _mock_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
        lambda exe: "1.6.1170.0",
    )


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _norm(ruta: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(ruta)))


def _notas(exc: BaseException) -> str:
    return " ".join(getattr(exc, "__notes__", []))


# ---------------------------------------------------------------------------
# 1. Los escritores informan la huella de lo PUBLICADO, no de una relectura
# ---------------------------------------------------------------------------


def test_config_save_devuelve_la_huella_publicada_y_no_un_readback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un tercero que escribe justo después del ``os.replace`` NO entra en la huella.

    Es el contraejemplo del readback: si la huella se calculara releyendo el
    destino, la escritura ajena quedaría atribuida a este ``save()``.
    """
    config = _crear_config(tmp_path / "config.toml", skyrim_path=r"C:\Steam\Skyrim")
    replace_real = os.replace
    publicado: dict[str, bytes] = {}

    def _replace_y_tercero_escribe(origen: str, destino: os.PathLike[str] | str) -> None:
        replace_real(origen, destino)
        publicado["bytes"] = pathlib.Path(destino).read_bytes()
        pathlib.Path(destino).write_bytes(publicado["bytes"] + _COMENTARIO_AJENO)

    monkeypatch.setattr(os, "replace", _replace_y_tercero_escribe)
    huella = local_config.persistir_campo(config, "skyrim_path", r"E:\Clones\Clone1")
    monkeypatch.setattr(os, "replace", replace_real)

    assert huella == _sha(publicado["bytes"]), "la huella no es la de los bytes que publicó el escritor"
    assert huella != _sha(config.read_bytes()), "la huella incluyó la escritura del tercero (readback)"


def test_save_json_legacy_y_guardar_config_devuelven_la_huella_publicada(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "sky_claw_config.json"
    cfg = local_config.LocalConfig(skyrim_path=r"C:\Steam\Skyrim")
    assert local_config.save(cfg, destino) == _sha(destino.read_bytes())
    cfg.skyrim_path = r"E:\Clones\Clone1"
    assert local_config.guardar_config(cfg, destino) == _sha(destino.read_bytes())
    assert local_config.persistir_campo(destino, "skyrim_path", r"F:\Otro") == _sha(destino.read_bytes())


def test_guardar_config_sin_huella_del_escritor_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Un ``save()`` que no informa huella NO se convierte en prueba de autoría."""

    class _ConfigSinHuella:
        def save(self) -> None:
            return None

    assert local_config.guardar_config(_ConfigSinHuella(), tmp_path / "x.toml") is None


def test_write_json_atomic_devuelve_la_huella_publicada(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "estado.json"
    huella = state_module.write_json_atomic(destino, {"b": 2, "a": 1})
    assert huella == _sha(destino.read_bytes())


def test_el_cas_avisa_la_huella_publicada_antes_del_readback(tmp_path: pathlib.Path) -> None:
    """``al_publicar`` recibe lo publicado aunque un tercero escriba antes del readback."""
    storage = tmp_path / "Storage"
    assert initialize_frozen_runtime_storage(storage).success
    active = active_state_path(storage)
    esperado = state_module.observar_estado_previo(active)
    escribir_real = state_module.write_json_atomic
    recibidas: list[str] = []

    def _escribir_y_tercero(path: pathlib.Path, payload: dict[str, object]) -> str:
        huella = escribir_real(path, payload)
        if pathlib.Path(path).name == "active.json":
            pathlib.Path(path).write_bytes(pathlib.Path(path).read_bytes() + b" ")
        return huella

    payload: dict[str, object] = {
        "schema_version": 2,
        "desired_generation_id": _GEN,
        "desired_clone_id": "clone-1",
        "updated_at_ns": 7,
    }
    with patch.object(state_module, "write_json_atomic", side_effect=_escribir_y_tercero):
        despues = state_module.escribir_estado_activo_cas(
            active, payload, esperado=esperado, al_publicar=recibidas.append
        )

    assert len(recibidas) == 1
    assert recibidas[0] != _sha(active.read_bytes()), "la huella avisada incluye la escritura del tercero"
    assert despues.sha256 == _sha(active.read_bytes()), "el valor devuelto es lo releído, no lo publicado"


# ---------------------------------------------------------------------------
# 2. Ancla enumerante: cada superficie persistida tiene su receta
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Superficies:
    storage: pathlib.Path
    ini: pathlib.Path
    config: pathlib.Path
    active: pathlib.Path
    destino: str

    def ruta(self, kind: str) -> pathlib.Path:
        return {
            recovery.SUPERFICIE_MO2_INI: self.ini,
            recovery.SUPERFICIE_CONFIG: self.config,
            recovery.SUPERFICIE_ACTIVE: self.active,
        }[kind]


def _montar_superficies(tmp_path: pathlib.Path, *, config_json: bool = False) -> _Superficies:
    storage = tmp_path / "Storage"
    assert initialize_frozen_runtime_storage(storage).success
    origen = str(tmp_path / "Steam")
    mo2 = _crear_mo2_instance_sintetica(tmp_path / "MO2", game_path=origen)
    if config_json:
        config = tmp_path / "sky_claw_config.json"
        local_config.save(local_config.LocalConfig(skyrim_path=origen), config)
    else:
        config = _crear_config(tmp_path / "config.toml", skyrim_path=origen)
    return _Superficies(storage, mo2 / "modorganizer.ini", config, active_state_path(storage), str(tmp_path / "Clone"))


def _capturar(s: _Superficies) -> recovery.RegistroRecuperacion:
    registro = recovery.capturar_registro(
        storage_root=s.storage,
        transition_id=_TID,
        journal_revision=0,
        operation="PROMOTION",
        target_clone_id="clone-1",
        target_generation_id=_GEN,
        expected_effective_path=s.destino,
        mo2_ini_path=s.ini,
        config_path=s.config,
        environ_skyrim_path=None,
    )
    return dataclasses.replace(registro, fase=recovery.FaseRecuperacion.DESEADO_ACTUALIZADO)


def _escribir_ini_propio(s: _Superficies) -> dict[str, object]:
    actualizar_game_path_en_ini(s.ini, s.destino)
    return {"escritura_intentada": True, "bytes_tras_escritura_ini": s.ini.read_bytes()}


def _escribir_config_propia(s: _Superficies) -> dict[str, object]:
    huella = local_config.persistir_campo(s.config, "skyrim_path", s.destino)
    assert isinstance(huella, str)
    return {"huellas_posteriores": {recovery.SUPERFICIE_CONFIG: huella}}


def _escribir_active_propio(s: _Superficies) -> dict[str, object]:
    huella = state_module.write_json_atomic(
        s.active,
        {"schema_version": 2, "desired_generation_id": _GEN, "desired_clone_id": "clone-1", "updated_at_ns": 11},
    )
    return {"huellas_posteriores": {recovery.SUPERFICIE_ACTIVE: huella}}


def _ajeno_ini(ruta: pathlib.Path) -> None:
    ruta.write_bytes(ruta.read_bytes() + b"; agregado por un tercero\r\n")


def _ajeno_config(ruta: pathlib.Path) -> None:
    ruta.write_bytes(ruta.read_bytes() + _COMENTARIO_AJENO)


def _ajeno_active(ruta: pathlib.Path) -> None:
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    datos["updated_at_ns"] = int(datos["updated_at_ns"]) + 1  # mismos IDs, otro contenido
    ruta.write_text(json.dumps(datos, indent=2, sort_keys=True), encoding="utf-8")


#: Receta por superficie: (escritura propia → evidencia en proceso, escritura ajena mínima).
_RECETAS: dict[str, tuple[Callable[[_Superficies], dict[str, object]], Callable[[pathlib.Path], None]]] = {
    recovery.SUPERFICIE_MO2_INI: (_escribir_ini_propio, _ajeno_ini),
    recovery.SUPERFICIE_CONFIG: (_escribir_config_propia, _ajeno_config),
    recovery.SUPERFICIE_ACTIVE: (_escribir_active_propio, _ajeno_active),
}


def test_toda_superficie_persistida_tiene_receta() -> None:
    """Igualdad literal: una superficie nueva rompe acá hasta que se le escribe su receta."""
    assert set(_RECETAS) == set(recovery._KINDS_PERSISTIDAS)


def _restaurar(
    s: _Superficies, registro: recovery.RegistroRecuperacion, kinds: frozenset[str], evidencia: dict[str, object]
) -> tuple[recovery.VerificacionSuperficie, ...]:
    with participant_lock(s.storage, timeout=10.0) as handle:
        handle.assert_owned()
        return recovery.restaurar_registro_locked(
            handle,
            s.storage,
            registro,
            None,
            solo_superficies=kinds,
            **evidencia,  # type: ignore[arg-type]
        )


def _veredicto(
    verificaciones: tuple[recovery.VerificacionSuperficie, ...], kind: str
) -> recovery.VerificacionSuperficie:
    return next(v for v in verificaciones if v.kind == kind)


def _evidencias(storage: pathlib.Path) -> dict[str, bytes]:
    raiz = storage / "recovery_evidence"
    return {p.name: p.read_bytes() for p in sorted(raiz.rglob("*.bin"))} if raiz.exists() else {}


@pytest.mark.parametrize("kind", sorted(_RECETAS))
def test_propia_con_huella_se_restaura(tmp_path: pathlib.Path, kind: str) -> None:
    s = _montar_superficies(tmp_path)
    previos = s.ruta(kind).read_bytes()
    registro = _capturar(s)
    evidencia = _RECETAS[kind][0](s)
    assert s.ruta(kind).read_bytes() != previos

    v = _veredicto(_restaurar(s, registro, frozenset({kind}), evidencia), kind)

    assert v.ok and v.restaurada and not v.escritura_ajena
    assert s.ruta(kind).read_bytes() == previos


@pytest.mark.parametrize("kind", sorted(_RECETAS))
def test_propia_mas_una_escritura_ajena_se_preserva(tmp_path: pathlib.Path, kind: str) -> None:
    s = _montar_superficies(tmp_path)
    registro = _capturar(s)
    evidencia = _RECETAS[kind][0](s)
    _RECETAS[kind][1](s.ruta(kind))
    actuales = s.ruta(kind).read_bytes()

    v = _veredicto(_restaurar(s, registro, frozenset({kind}), evidencia), kind)

    assert v.ok and v.escritura_ajena and not v.restaurada
    assert s.ruta(kind).read_bytes() == actuales, "la recuperación pisó la escritura ajena"
    assert actuales in _evidencias(s.storage).values(), "no se conservó evidencia de lo ajeno"


@pytest.mark.parametrize("kind", sorted(_RECETAS))
def test_sin_evidencia_de_proceso_no_se_atribuye(tmp_path: pathlib.Path, kind: str) -> None:
    """Es la recuperación en frío: no hay huella en memoria y nada divergente se pisa."""
    s = _montar_superficies(tmp_path)
    registro = _capturar(s)
    _RECETAS[kind][0](s)
    actuales = s.ruta(kind).read_bytes()

    v = _veredicto(_restaurar(s, registro, frozenset({kind}), {}), kind)

    assert v.ok and v.escritura_ajena and not v.restaurada
    assert s.ruta(kind).read_bytes() == actuales


def test_config_json_legacy_propia_se_restaura_y_ajena_se_preserva(tmp_path: pathlib.Path) -> None:
    s = _montar_superficies(tmp_path / "propia", config_json=True)
    previos = s.config.read_bytes()
    registro = _capturar(s)
    evidencia = _escribir_config_propia(s)
    v = _veredicto(_restaurar(s, registro, frozenset({recovery.SUPERFICIE_CONFIG}), evidencia), "config")
    assert v.restaurada and s.config.read_bytes() == previos

    s2 = _montar_superficies(tmp_path / "ajena", config_json=True)
    registro2 = _capturar(s2)
    evidencia2 = _escribir_config_propia(s2)
    s2.config.write_bytes(s2.config.read_bytes() + b"\n")
    actuales = s2.config.read_bytes()
    v2 = _veredicto(_restaurar(s2, registro2, frozenset({recovery.SUPERFICIE_CONFIG}), evidencia2), "config")
    assert v2.escritura_ajena and s2.config.read_bytes() == actuales


def test_huella_de_config_que_arrastra_un_cambio_ajeno_no_autoriza_restaurar(tmp_path: pathlib.Path) -> None:
    """Merge-on-save: lo que la GUI guardó ANTES de nuestra escritura viaja DENTRO de nuestros bytes.

    La huella coincide (nadie escribió después), pero restaurar el previo
    borraría ``language = "en"``. La guarda semántica es la que lo impide; la
    huella sola —el diseño A1— lo habría restaurado.
    """
    s = _montar_superficies(tmp_path)
    registro = _capturar(s)
    s.config.write_text(s.config.read_text(encoding="utf-8").replace('language = "es"', 'language = "en"'), "utf-8")
    evidencia = _escribir_config_propia(s)
    actuales = s.config.read_bytes()

    v = _veredicto(_restaurar(s, registro, frozenset({recovery.SUPERFICIE_CONFIG}), evidencia), "config")

    assert v.escritura_ajena and not v.restaurada
    assert s.config.read_bytes() == actuales
    assert tomllib.loads(actuales.decode("utf-8"))["language"] == "en"


# ---------------------------------------------------------------------------
# 3. Decisión de conjunto, anomalías a mitad de camino, lock del archivo
# ---------------------------------------------------------------------------


def test_una_superficie_ajena_impide_restaurar_las_demas(tmp_path: pathlib.Path) -> None:
    s = _montar_superficies(tmp_path)
    ini_previo = s.ini.read_bytes()
    registro = _capturar(s)
    evidencia: dict[str, object] = dict(_escribir_ini_propio(s))
    huellas = {
        **_escribir_config_propia(s)["huellas_posteriores"],  # type: ignore[dict-item]
        **_escribir_active_propio(s)["huellas_posteriores"],  # type: ignore[dict-item]
    }
    evidencia["huellas_posteriores"] = huellas
    _ajeno_active(s.active)
    bytes_tras = {k: s.ruta(k).read_bytes() for k in _RECETAS}

    verificaciones = _restaurar(s, registro, frozenset(_RECETAS), evidencia)

    assert {k: s.ruta(k).read_bytes() for k in _RECETAS} == bytes_tras, "se restauró algo pese a la ajena"
    assert s.ini.read_bytes() != ini_previo
    assert _veredicto(verificaciones, "active_json").escritura_ajena
    for kind in ("mo2_ini", "config"):
        v = _veredicto(verificaciones, kind)
        assert v.ok and not v.restaurada and not v.escritura_ajena, v


@pytest.mark.parametrize("falla", ["config", "active_json"])
def test_un_fallo_a_mitad_de_la_restauracion_detiene_las_escrituras_restantes(
    tmp_path: pathlib.Path, falla: str
) -> None:
    s = _montar_superficies(tmp_path)
    previos = {k: s.ruta(k).read_bytes() for k in _RECETAS}
    registro = _capturar(s)
    evidencia: dict[str, object] = dict(_escribir_ini_propio(s))
    evidencia["huellas_posteriores"] = {
        **_escribir_config_propia(s)["huellas_posteriores"],  # type: ignore[dict-item]
        **_escribir_active_propio(s)["huellas_posteriores"],  # type: ignore[dict-item]
    }
    escribir_real = recovery._escribir_bytes_atomico

    def _escribir_o_fallar(ruta: pathlib.Path, datos: bytes) -> None:
        if _norm(ruta) == _norm(s.ruta(falla)):
            raise OSError("disco lleno simulado")
        escribir_real(ruta, datos)

    with patch.object(recovery, "_escribir_bytes_atomico", side_effect=_escribir_o_fallar):
        verificaciones = _restaurar(s, registro, frozenset(_RECETAS), evidencia)

    orden = list(recovery._KINDS_PERSISTIDAS)
    posicion = orden.index(falla)
    for kind in orden[:posicion]:
        assert _veredicto(verificaciones, kind).restaurada
        assert s.ruta(kind).read_bytes() == previos[kind]
    assert not _veredicto(verificaciones, falla).ok
    for kind in orden[posicion + 1 :]:
        v = _veredicto(verificaciones, kind)
        assert not v.restaurada, f"{kind} se restauró después de un fallo"
        assert s.ruta(kind).read_bytes() != previos[kind]
    with pytest.raises(recovery.RecuperacionError):
        recovery.exigir_restauracion_completa(verificaciones)


def test_una_escritura_ajena_posterior_a_la_clasificacion_se_preserva(tmp_path: pathlib.Path) -> None:
    """Carrera: el tercero escribe DESPUÉS de clasificar y ANTES de restaurar esa superficie."""
    s = _montar_superficies(tmp_path)
    previos = {k: s.ruta(k).read_bytes() for k in _RECETAS}
    registro = _capturar(s)
    evidencia: dict[str, object] = dict(_escribir_ini_propio(s))
    evidencia["huellas_posteriores"] = {
        **_escribir_config_propia(s)["huellas_posteriores"],  # type: ignore[dict-item]
        **_escribir_active_propio(s)["huellas_posteriores"],  # type: ignore[dict-item]
    }
    restaurar_real = recovery._restaurar_persistida

    def _tercero_y_restaurar(superficie: recovery.SuperficieCapturada, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if superficie.kind == recovery.SUPERFICIE_CONFIG:
            _ajeno_config(s.config)
        return restaurar_real(superficie, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(recovery, "_restaurar_persistida", side_effect=_tercero_y_restaurar):
        verificaciones = _restaurar(s, registro, frozenset(_RECETAS), evidencia)

    assert _veredicto(verificaciones, "mo2_ini").restaurada
    config_v = _veredicto(verificaciones, "config")
    assert config_v.escritura_ajena and not config_v.restaurada
    assert s.config.read_bytes().endswith(_COMENTARIO_AJENO), "la carrera pisó la escritura ajena"
    active_v = _veredicto(verificaciones, "active_json")
    assert not active_v.restaurada, "se siguió restaurando después de detectar la carrera"
    assert s.active.read_bytes() != previos["active_json"]


def test_la_restauracion_de_config_participa_del_lock_del_archivo(tmp_path: pathlib.Path) -> None:
    """Un ``Config.save()`` del mismo proceso no puede intercalarse con la restauración."""
    s = _montar_superficies(tmp_path)
    previos = s.config.read_bytes()
    registro = _capturar(s)
    evidencia = _escribir_config_propia(s)
    publicado = s.config.read_bytes()
    resultado: dict[str, object] = {}
    lock = lock_de_archivo_config(s.config)

    lock.acquire()
    try:
        hilo = threading.Thread(
            target=lambda: resultado.update(
                v=_restaurar(s, registro, frozenset({recovery.SUPERFICIE_CONFIG}), evidencia)
            ),
            daemon=True,
        )
        hilo.start()
        hilo.join(timeout=0.5)
        assert hilo.is_alive(), "la restauración de config no esperó al lock del archivo"
        assert s.config.read_bytes() == publicado
    finally:
        lock.release()
    hilo.join(timeout=10)
    assert not hilo.is_alive()
    assert s.config.read_bytes() == previos


def test_la_recuperacion_repetida_no_muta_nada(tmp_path: pathlib.Path) -> None:
    s = _montar_superficies(tmp_path)
    registro = _capturar(s)
    for kind in _RECETAS:
        _RECETAS[kind][0](s)
    bytes_tras = {k: s.ruta(k).read_bytes() for k in _RECETAS}

    primeras = _restaurar(s, registro, frozenset(_RECETAS), {})
    evidencia_1 = _evidencias(s.storage)
    for _ in range(2):
        siguientes = _restaurar(s, registro, frozenset(_RECETAS), {})
        assert siguientes == primeras
        assert {k: s.ruta(k).read_bytes() for k in _RECETAS} == bytes_tras
        assert _evidencias(s.storage) == evidencia_1


# ---------------------------------------------------------------------------
# 4. Rollback EN PROCESO del binder: sólo con la huella de su propio escritor
# ---------------------------------------------------------------------------


def _bytes_superficies(rig: _Rig) -> dict[str, bytes]:
    return {"ini": rig.ini_bytes(), "config": rig.config_bytes(), "active": rig.active_bytes()}


def _fallo_post_con(tercero: Callable[[], None]) -> Callable[..., None]:
    def _efecto(**_: object) -> None:
        tercero()
        raise RuntimeError("POST VERIFY revienta")

    return _efecto


def _sustitucion_atomica_equivalente(rig: _Rig) -> None:
    texto = rig.config.read_text(encoding="utf-8").replace(" = ", "   =   ")
    tmp = rig.config.with_name("config.toml.ajeno")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, rig.config)


@pytest.mark.parametrize(
    "variante",
    ["comentario", "sustitucion_atomica_equivalente"],
)
def test_en_proceso_no_se_pisa_una_escritura_ajena_equivalente_en_config(tmp_path: pathlib.Path, variante: str) -> None:
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)

    def _tercero() -> None:
        if variante == "comentario":
            _ajeno_config(rig.config)
        else:
            _sustitucion_atomica_equivalente(rig)

    with (
        patch.object(binder, "assess_effective_runtime", side_effect=_fallo_post_con(_tercero)),
        pytest.raises(RuntimeError, match="POST VERIFY revienta") as excinfo,
    ):
        _ejecutar(rig)

    ajenos = rig.config_bytes()
    assert ajenos != antes["config"]
    if variante == "comentario":
        assert ajenos.endswith(_COMENTARIO_AJENO), "el rollback pisó el comentario ajeno"
    else:
        assert b"   =   " in ajenos, "el rollback pisó la sustitución ajena"
    # Decisión de conjunto: como config no es atribuible, no se restauró nada.
    assert rig.ini_bytes() != antes["ini"]
    assert rig.active_bytes() != antes["active"]
    assert "config" in _notas(excinfo.value), "el rollback incompleto no quedó declarado en la excepción"


def test_en_proceso_active_con_mismos_ids_y_otro_contenido_se_preserva(tmp_path: pathlib.Path) -> None:
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)
    active = active_state_path(rig.storage)

    with (
        patch.object(binder, "assess_effective_runtime", side_effect=_fallo_post_con(lambda: _ajeno_active(active))),
        pytest.raises(RuntimeError),
    ):
        _ejecutar(rig)

    assert rig.active_bytes() != antes["active"], "el rollback pisó el active.json ajeno con los mismos IDs"
    assert json.loads(rig.active_bytes())["desired_clone_id"] == rig.clone_id


def test_en_proceso_una_escritura_ajena_tras_persistir_config_no_se_atribuye(tmp_path: pathlib.Path) -> None:
    """La huella viene del escritor: lo que un tercero escribe DESPUÉS no es nuestro."""
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)
    real = binder.persistir_campo

    def _persistir_y_tercero(path: pathlib.Path, campo: str, valor: object) -> str | None:
        huella = real(path, campo, valor)
        _ajeno_config(path)
        return huella

    with (
        patch.object(binder, "persistir_campo", side_effect=_persistir_y_tercero),
        patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        _ejecutar(rig)

    assert rig.config_bytes().endswith(_COMENTARIO_AJENO)
    assert rig.ini_bytes() != antes["ini"]


def test_en_proceso_una_escritura_ajena_tras_el_cas_y_antes_del_readback_no_se_atribuye(
    tmp_path: pathlib.Path,
) -> None:
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)
    escribir_real = state_module.write_json_atomic

    def _escribir_y_tercero(path: pathlib.Path, payload: dict[str, object]) -> str:
        huella = escribir_real(path, payload)
        if pathlib.Path(path).name == "active.json":
            pathlib.Path(path).write_bytes(pathlib.Path(path).read_bytes() + b" ")
        return huella

    with (
        patch.object(state_module, "write_json_atomic", side_effect=_escribir_y_tercero),
        patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        _ejecutar(rig)

    assert rig.active_bytes().endswith(b" "), "el rollback pisó la escritura ajena posterior al CAS"
    assert rig.active_bytes() != antes["active"]


def test_en_proceso_el_merge_ajeno_dentro_de_nuestra_escritura_no_se_restaura(tmp_path: pathlib.Path) -> None:
    rig = _montar_rig(tmp_path)
    real = binder.persistir_campo

    def _gui_guarda_antes(path: pathlib.Path, campo: str, valor: object) -> str | None:
        path.write_text(path.read_text(encoding="utf-8").replace('language = "es"', 'language = "en"'), "utf-8")
        return real(path, campo, valor)

    with (
        patch.object(binder, "persistir_campo", side_effect=_gui_guarda_antes),
        patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        _ejecutar(rig)

    persistido = tomllib.loads(rig.config.read_text(encoding="utf-8"))
    assert persistido["language"] == "en", "el rollback borró lo que la GUI guardó dentro del merge"


@pytest.mark.parametrize("superficie", ["config", "active"])
def test_en_proceso_sin_huella_del_escritor_se_preserva(tmp_path: pathlib.Path, superficie: str) -> None:
    """Evidencia incompleta: el escritor no informó qué publicó ⇒ no hay autoría."""
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)
    if superficie == "config":
        real_cfg = binder.persistir_campo

        def _sin_huella_cfg(path: pathlib.Path, campo: str, valor: object) -> None:
            real_cfg(path, campo, valor)

        parche = patch.object(binder, "persistir_campo", side_effect=_sin_huella_cfg)
    else:
        real_cas = binder._escribir_estado_activo_cas_locked

        def _sin_huella_cas(path, payload, *, esperado, al_publicar=None):  # type: ignore[no-untyped-def]
            return real_cas(path, payload, esperado=esperado)

        parche = patch.object(binder, "_escribir_estado_activo_cas_locked", side_effect=_sin_huella_cas)

    with (
        parche,
        patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        _ejecutar(rig)

    despues = _bytes_superficies(rig)
    assert despues[superficie] != antes[superficie], f"{superficie} se restauró sin huella del escritor"
    assert despues["ini"] != antes["ini"], "decisión de conjunto: el INI no debía restaurarse"


def test_en_proceso_un_fallo_de_escritura_del_ini_sin_huella_no_se_atribuye_por_forma(tmp_path: pathlib.Path) -> None:
    """H10: si el escritor del INI falla y no se pudo leer bajo la exclusión, la autoría es DESCONOCIDA.

    Antes, el camino de excepción conservaba ``escritura_intentada=True`` sin
    huella y la recuperación atribuía por la FORMA del contenido, con la
    exclusión ya liberada (cualquiera podía haber escrito).
    """
    rig = _montar_rig(tmp_path)
    antes = rig.ini_bytes()
    real = binder.actualizar_game_path_por_handle

    def _escribe_y_falla(retenido, valor, **kwargs):  # type: ignore[no-untyped-def]
        real(retenido, valor, **kwargs)
        raise Mo2IniEscrituraError("fallo simulado DESPUÉS de escribir")

    with (
        patch.object(binder, "actualizar_game_path_por_handle", side_effect=_escribe_y_falla),
        patch.object(binder, "_bytes_bajo_exclusion", return_value=None),
        pytest.raises(binder.BinderEscrituraIniError),
    ):
        _ejecutar(rig)

    assert rig.ini_bytes() != antes, "se atribuyó el INI por forma sin huella tomada bajo la exclusión"


@pytest.mark.parametrize("falla", ["config", "active"])
def test_en_proceso_un_fallo_en_la_segunda_o_tercera_restauracion_es_critico_y_luego_conservador(
    tmp_path: pathlib.Path, falla: str
) -> None:
    rig = _montar_rig(tmp_path)
    antes = _bytes_superficies(rig)
    objetivo = rig.config if falla == "config" else active_state_path(rig.storage)
    escribir_real = recovery._escribir_bytes_atomico

    def _escribir_o_fallar(ruta: pathlib.Path, datos: bytes) -> None:
        if _norm(ruta) == _norm(objetivo):
            raise OSError("disco lleno simulado")
        escribir_real(ruta, datos)

    with (
        patch.object(recovery, "_escribir_bytes_atomico", side_effect=_escribir_o_fallar),
        patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("boom")),
        pytest.raises(binder.BinderRollbackFailedError),
    ):
        _ejecutar(rig)

    assert rig.ini_bytes() == antes["ini"], "la primera restauración no ocurrió"
    if falla == "config":
        assert rig.config_bytes() != antes["config"]
        assert rig.active_bytes() != antes["active"], "se siguió restaurando después del fallo"
    else:
        assert rig.config_bytes() == antes["config"]
        assert rig.active_bytes() != antes["active"]
    assert recovery.listar_registros(rig.storage), "el registro durable desapareció"

    # Recuperación en frío posterior: conservadora e idempotente.
    tras_rollback = _bytes_superficies(rig)
    estados = {recovery.recuperar_transicion_interrumpida(rig.storage).estado for _ in range(2)}
    assert estados == {recovery.EstadoRecuperacion.RESTAURACION_INCOMPLETA_REQUIERE_DUENO}
    assert _bytes_superficies(rig) == tras_rollback


def test_el_mensaje_de_telegram_incluye_la_nota_del_rollback_incompleto() -> None:
    """El dueño lee la excepción en Telegram: la nota del rollback incompleto tiene que llegar."""
    from sky_claw.app.comms.golden_master_hitl import _texto_de_error

    exc = binder.BinderVerificationError("POST VERIFY no autoriza")
    assert _texto_de_error(exc) == "POST VERIFY no autoriza"
    exc.add_note("rollback INCOMPLETO de la transición t: ['config'] quedaron sin restaurar")
    texto = _texto_de_error(exc)
    assert texto.startswith("POST VERIFY no autoriza")
    assert "rollback INCOMPLETO" in texto and "['config']" in texto


# ---------------------------------------------------------------------------
# 5. Recuperación en FRÍO tras matar el proceso en cada frontera
# ---------------------------------------------------------------------------


def _correr_y_morir(rig_dir: pathlib.Path, frontera: str) -> None:
    """Cuerpo del subproceso. Las fronteras del módulo P5-C1 se delegan tal cual."""
    if frontera in ("ini", "config", "entorno", "active"):
        _correr_hasta_crashear(rig_dir, frontera)
        return
    with patch(
        "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
        lambda exe: "1.6.1170.0",
    ):
        rig = _montar_rig(rig_dir)
        active = active_state_path(rig.storage)
        manifiesto = {
            "storage": str(rig.storage),
            "ini": str(rig.ini),
            "config": str(rig.config),
            "active": str(active),
            "managed_src": str(rig.managed_src),
            "sha_ini": _sha(rig.ini.read_bytes()),
            "sha_config": _sha(rig.config.read_bytes()),
            "sha_active": _sha(active.read_bytes()),
            "transition_id": _TID,
        }
        (rig_dir / "antes.json").write_text(json.dumps(manifiesto), encoding="utf-8")
        pila = contextlib.ExitStack()
        if frontera == "entorno_marcado":
            real_fase = recovery.marcar_fase_locked

            def _fase_y_morir(handle, root, *, transition_id, fase):  # type: ignore[no-untyped-def]
                resultado = real_fase(handle, root, transition_id=transition_id, fase=fase)
                if fase is recovery.FaseRecuperacion.ENTORNO_ACTUALIZADO:
                    os._exit(9)
                return resultado

            pila.enter_context(patch.object(recovery, "marcar_fase_locked", side_effect=_fase_y_morir))
        elif frontera == "rollback":
            pila.enter_context(patch.object(binder, "assess_effective_runtime", side_effect=RuntimeError("POST")))
            escribir_real = recovery._escribir_bytes_atomico
            superficies = {_norm(rig.ini), _norm(rig.config), _norm(active)}

            def _restaurar_una_y_morir(ruta: pathlib.Path, datos: bytes) -> None:
                escribir_real(ruta, datos)
                if _norm(ruta) in superficies:
                    os._exit(9)

            pila.enter_context(patch.object(recovery, "_escribir_bytes_atomico", side_effect=_restaurar_una_y_morir))
        else:
            raise AssertionError(f"frontera desconocida {frontera!r}")
        with pila:
            binder.ejecutar_transicion_binder(
                storage_root=rig.storage,
                mo2_instance_dir=rig.mo2_dir,
                target_clone_id=rig.clone_id,
                propuesta=rig.propuesta,
                consentimiento=rig.consentimiento,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ={"SKYRIM_PATH": str(rig.managed_src)},
                probes=_probes_quiescentes(),
                transition_id=_TID,
            )
        os._exit(0)


#: Frontera → (fase durable que debe quedar, superficies que el crash dejó divergentes).
_FRONTERAS: dict[str, tuple[recovery.FaseRecuperacion, frozenset[str]]] = {
    "ini": (recovery.FaseRecuperacion.INI_ESCRITO, frozenset({"ini"})),
    "config": (recovery.FaseRecuperacion.CONFIG_ESCRITA, frozenset({"ini", "config"})),
    # IP-4 específico: muere apenas se marcó ENTORNO_ACTUALIZADO.
    "entorno_marcado": (recovery.FaseRecuperacion.ENTORNO_ACTUALIZADO, frozenset({"ini", "config"})),
    "entorno": (recovery.FaseRecuperacion.ENTORNO_ACTUALIZADO, frozenset({"ini", "config"})),
    "active": (recovery.FaseRecuperacion.DESEADO_ACTUALIZADO, frozenset({"ini", "config", "active"})),
    # Interrupción DURANTE el rollback en proceso: el INI ya volvió, el resto no.
    "rollback": (recovery.FaseRecuperacion.DESEADO_ACTUALIZADO, frozenset({"config", "active"})),
}


@pytest.mark.parametrize("frontera", list(_FRONTERAS))
def test_recuperacion_en_frio_no_sobrescribe_ninguna_superficie_divergente(
    tmp_path: pathlib.Path, frontera: str
) -> None:
    rig_dir = tmp_path / "rig"
    rig_dir.mkdir()
    script = tmp_path / "runner.py"
    script.write_text(
        "import os, sys, pathlib\n"
        "REPO = os.environ['P7F_REPO']\n"
        "sys.path.insert(0, REPO)\n"
        "from tests import test_frozen_runtime_p7f_recuperacion_conservadora as fx\n"
        f"fx._correr_y_morir(pathlib.Path(os.environ['P7F_RIG']), {frontera!r})\n",
        encoding="utf-8",
    )
    entorno_proc = dict(os.environ, P7F_REPO=str(_REPO), P7F_RIG=str(rig_dir), PYTHONPATH=str(_REPO))
    completado = subprocess.run(
        [sys.executable, str(script)], env=entorno_proc, capture_output=True, timeout=300, check=False
    )
    assert completado.returncode == 9, (
        f"el subproceso no murió en {frontera}: rc={completado.returncode} stderr={completado.stderr[-2000:]!r}"
    )

    manifiesto = json.loads((rig_dir / "antes.json").read_text(encoding="utf-8"))
    storage = pathlib.Path(manifiesto["storage"])
    rutas = {k: pathlib.Path(manifiesto[k]) for k in ("ini", "config", "active")}
    tras_crash = {k: ruta.read_bytes() for k, ruta in rutas.items()}
    fase_esperada, divergentes_esperadas = _FRONTERAS[frontera]

    # La fase durable que el kill dejó y su efecto sobre el disco quedan acreditados.
    registro = recovery.cargar_registro(storage, manifiesto["transition_id"])
    assert registro is not None and registro.fase is fase_esperada
    divergentes = {k for k in rutas if _sha(tras_crash[k]) != manifiesto[f"sha_{k}"]}
    assert divergentes == divergentes_esperadas

    entorno_nuevo: dict[str, str] = {}
    resultados = []
    for _ in range(3):
        resultados.append(
            recovery.recuperar_transicion_interrumpida(
                storage, transition_id=manifiesto["transition_id"], environ=entorno_nuevo
            )
        )
        assert {k: ruta.read_bytes() for k, ruta in rutas.items()} == tras_crash, "la recuperación escribió"
        if len(resultados) == 1:
            evidencia_1 = _evidencias(storage)

    assert _evidencias(storage) == evidencia_1, "la repetición alteró la evidencia"
    assert len({r.estado for r in resultados}) == 1
    resultado = resultados[0]
    assert resultado.estado is recovery.EstadoRecuperacion.RESTAURACION_INCOMPLETA_REQUIERE_DUENO
    assert resultado.requiere_dueno is True
    assert not any(v.restaurada for v in resultado.verificaciones)
    kind_de = {"ini": "mo2_ini", "config": "config", "active": "active_json"}
    for k in divergentes:
        assert _veredicto(resultado.verificaciones, kind_de[k]).escritura_ajena
        assert tras_crash[k] in evidencia_1.values(), f"no se conservó evidencia de {k}"
    # Lo que ya está como en el snapshot (p. ej. el INI que el rollback interrumpido
    # alcanzó a restaurar) no queda marcado como pendiente para el dueño.
    for k in set(rutas) - divergentes:
        v = _veredicto(resultado.verificaciones, kind_de[k])
        assert not v.requiere_dueno and not v.escritura_ajena, (k, v.detail)
    assert entorno_nuevo == {}, "la superficie derivada se restauró sin restaurar su fuente"
    assert cargar_journal_transicion(storage).disposition is JournalDisposition.PENDING_PROMOTION
