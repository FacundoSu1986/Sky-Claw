"""Matriz adversarial P3: Candidate creation + verification.

Synthetic rig, sin Skyrim real (§49): una Managed Source de Steam falsa con
``SkyrimSE.exe`` y un arbol de modding. Los ataques se ejecutan sobre el payload
del Candidate DESPUES de la copia y ANTES del gate, que es donde un Candidate
podria colarse a READY.

Los IDs (C01..C20) mapean la matriz del handoff.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import time

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime import copying as copying_module
from sky_claw.local.frozen_runtime import observation as observation_module
from sky_claw.local.frozen_runtime.candidates import (
    candidate_dir,
    candidate_metadata_path,
    candidates_dir,
    candidates_state_dir,
    crear_candidate,
    descubrir_candidates,
    leer_metadata_candidate,
    payload_dir,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.models import ManagedSource, ManagedSourceProvider
from sky_claw.local.frozen_runtime.storage import initialize_frozen_runtime_storage
from sky_claw.local.frozen_runtime.storage_models import CandidateState, GenerationVerificationState
from tests._symlink_guard import crear_junction, junction_guard

MANIFEST_IDLE = (
    '"AppState"\n'
    "{\n"
    '\t"appid"\t\t"489830"\n'
    '\t"name"\t\t"Skyrim Special Edition"\n'
    '\t"StateFlags"\t\t"4"\n'
    '\t"buildid"\t\t"1234567"\n'
    '\t"BytesToDownload"\t\t"0"\n'
    '\t"BytesDownloaded"\t\t"0"\n'
    '\t"UpdateResult"\t\t"0"\n'
    '\t"installdir"\t\t"Skyrim Special Edition"\n'
    "}\n"
)


def _sin_op(_segundos: float) -> None:
    """Dormir no es necesario: los tests son deterministas, no temporizados."""
    return


def _escribir_managed_source(steamapps: pathlib.Path) -> pathlib.Path:
    common = steamapps / "common" / "Skyrim Special Edition"
    (common / "Data" / "Meshes" / "Characters").mkdir(parents=True)
    (common / "Data" / "Textures").mkdir(parents=True)
    (common / "Data" / "EmptyFolder").mkdir(parents=True)
    (common / "SkyrimSE.exe").write_bytes(b"fake-skyrimse-payload")
    (common / "Data" / "Meshes" / "Characters" / "a.nif").write_bytes(b"nif-payload")
    (common / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    (steamapps / "appmanifest_489830.acf").write_text(MANIFEST_IDLE, encoding="utf-8")
    return common


def _source(steamapps: pathlib.Path) -> ManagedSource:
    common = steamapps / "common" / "Skyrim Special Edition"
    return ManagedSource(
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        appid="489830",
        root=common,
        library_steamapps=steamapps,
        library_root=steamapps.parent,
    )


@pytest.fixture
def parche_identidad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identidad PE falsa y estable (el exe sintetico no es un PE real).

    Se parchea en LOS DOS modulos que importan la primitive: P1 la usa a traves
    de ``observation`` y P3 a traves de ``membership`` (que la importa por
    nombre). Parchear solo uno dejaria al Candidate observando un PE sintetico
    ilegible.
    """
    from sky_claw.local.frozen_runtime import membership as membership_module
    from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

    def identidad(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version="1.6.1170.0",
            observed_exe_path=str(pathlib.Path(root) / "SkyrimSE.exe"),
            observed_at_ns=time.time_ns(),
        )

    monkeypatch.setattr(observation_module, "observe_runtime_identity_from_root", identidad)
    monkeypatch.setattr(membership_module, "observe_runtime_identity_from_root", identidad)


@pytest.fixture
def rig(tmp_path: pathlib.Path, parche_identidad: None) -> tuple[ManagedSource, pathlib.Path]:
    steamapps = tmp_path / "library" / "steamapps"
    steamapps.mkdir(parents=True)
    _escribir_managed_source(steamapps)
    root = tmp_path / "frozen-runtime"
    assert initialize_frozen_runtime_storage(root).success
    return _source(steamapps), root


def _crear(source: ManagedSource, root: pathlib.Path, cid: str = "cand_" + "a" * 32):
    return crear_candidate(source, root, quiet_window_seconds=0.0, sleep=_sin_op, id_factory=lambda: cid)


# ── Camino feliz ──────────────────────────────────────────────────────────


def test_c01_un_candidate_recien_creado_empieza_building(rig, monkeypatch) -> None:
    """C01: BUILDING se persiste ANTES de copiar; nunca READY por defecto."""
    source, root = rig
    estados: list[CandidateState] = []
    original = candidates_module._persistir_metadata

    def registrando(raiz: pathlib.Path, metadata) -> None:
        estados.append(metadata.state)
        original(raiz, metadata)

    monkeypatch.setattr(candidates_module, "_persistir_metadata", registrando)
    resultado = _crear(source, root)

    assert estados[0] is CandidateState.BUILDING
    assert estados[-1] is CandidateState.READY
    assert resultado.metadata is not None
    assert resultado.metadata.state is CandidateState.READY


def test_c02_copia_sintetica_exitosa(rig) -> None:
    """C02: la copia crea un arbol real e independiente."""
    source, root = rig
    resultado = _crear(source, root)

    assert resultado.state is GenerationVerificationState.VALID, resultado.message
    payload = payload_dir(candidate_dir(root, resultado.candidate_id or ""))
    assert (payload / "SkyrimSE.exe").read_bytes() == b"fake-skyrimse-payload"
    assert (payload / "Data" / "Skyrim.esm").read_bytes() == b"esm-payload"


def test_c03_los_directorios_vacios_se_preservan(rig) -> None:
    """C03: los directorios vacios son parte de la identidad P3."""
    source, root = rig
    resultado = _crear(source, root)
    payload = payload_dir(candidate_dir(root, resultado.candidate_id or ""))

    assert (payload / "Data" / "EmptyFolder").is_dir()
    assert list((payload / "Data" / "EmptyFolder").iterdir()) == []
    assert resultado.pre_source_evidence is not None
    assert "Data/EmptyFolder" in resultado.pre_source_evidence.directory_membership.directories
    assert (
        resultado.pre_source_evidence.directory_membership == resultado.post_source_evidence.directory_membership  # type: ignore[union-attr]
    )


def test_c04_pre_candidate_post_iguales_llevan_a_ready(rig) -> None:
    """C04: solo A==A==A puede terminar en READY."""
    source, root = rig
    resultado = _crear(source, root)

    assert resultado.state is GenerationVerificationState.VALID, resultado.message
    pre, cand, post = resultado.pre_source_evidence, resultado.candidate_evidence, resultado.post_source_evidence
    assert pre is not None and cand is not None and post is not None
    assert pre.tree_digest == cand.tree_digest == post.tree_digest
    assert pre.directory_membership == cand.directory_membership == post.directory_membership
    assert pre.runtime_identity == cand.runtime_identity == post.runtime_identity


# ── Ataques a la autoridad (SFR-15) ───────────────────────────────────────


def _payload_de(resultado, root: pathlib.Path) -> pathlib.Path:
    return payload_dir(candidate_dir(root, resultado.candidate_id or ""))


def test_c05_source_muta_durante_la_copia_no_llega_a_ready(rig, monkeypatch) -> None:
    """C08/C35: PRE=A, la fuente cambia a B durante la copia => NUNCA READY."""
    source, root = rig
    original = candidates_module.copiar_arbol_independiente

    def copia_con_mutacion(origen, destino, files, directories):
        # La fuente muta DESPUES de que PRE se sello y antes de terminar la copia.
        (pathlib.Path(origen) / "Data" / "Skyrim.esm").write_bytes(b"esm-MUTADO-POR-STEAM")
        return original(origen, destino, files, directories)

    monkeypatch.setattr(candidates_module, "copiar_arbol_independiente", copia_con_mutacion)
    resultado = _crear(source, root)

    assert resultado.state is not GenerationVerificationState.VALID
    assert resultado.metadata is not None
    assert resultado.metadata.state is CandidateState.INVALID


def test_c09_la_membership_vacia_de_la_fuente_cambia_no_llega_a_ready(rig, monkeypatch) -> None:
    """C09/C37: test central de ``P3_DIRECTORY_MEMBERSHIP``.

    ``Data/EmptyFolder/`` desaparece de la FUENTE entre el PRE y el POST, con
    TODOS los archivos intactos. El ``TreeDigest`` de archivos puede seguir
    cuadrando; la membership de directorios no. Si este test pasara con READY,
    el blocker del handoff estaria abierto.
    """
    source, root = rig
    original = candidates_module.copiar_arbol_independiente

    def copia_con_perdida_de_vacio(origen, destino, files, directories):
        # El directorio vacio desaparece de la fuente DESPUES del PRE.
        (pathlib.Path(origen) / "Data" / "EmptyFolder").rmdir()
        return original(origen, destino, files, directories)

    monkeypatch.setattr(candidates_module, "copiar_arbol_independiente", copia_con_perdida_de_vacio)
    resultado = _crear(source, root)

    assert resultado.state is GenerationVerificationState.INVALID
    assert "DirectoryMembership" in resultado.message
    assert resultado.metadata is not None
    assert resultado.metadata.state is CandidateState.INVALID


def test_c06_candidate_mutado_no_llega_a_ready(rig) -> None:
    """C10: mutar un archivo del Candidate invalida la triada."""
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID

    payload = _payload_de(resultado, root)
    (payload / "Data" / "Skyrim.esm").write_bytes(b"esm-tampered")

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is GenerationVerificationState.INVALID
    assert "TreeDigest" in verificacion.message


def test_c07_candidate_sin_mismo_directorio_vacio_no_llega_a_ready(rig) -> None:
    """C11: perder el directorio vacio invalida aunque el digest de archivos coincida."""
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID

    payload = _payload_de(resultado, root)
    (payload / "Data" / "EmptyFolder").rmdir()

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is GenerationVerificationState.INVALID
    assert "DirectoryMembership" in verificacion.message


def test_c12_hardlink_inyectado_no_llega_a_ready(rig, tmp_path) -> None:
    """C12/C40: hardlink con bytes IDENTICOS viola la independencia fisica.

    El ``TreeDigest`` no lo ve: el contenido es el mismo. Lo ve ``st_nlink``.
    """
    source, root = rig
    resultado = _crear(source, root)
    payload = _payload_de(resultado, root)

    objetivo = payload / "Data" / "Skyrim.esm"
    bytes_originales = objetivo.read_bytes()
    objetivo.unlink()
    os.link(source.root / "Data" / "Skyrim.esm", objetivo)
    assert objetivo.read_bytes() == bytes_originales

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is GenerationVerificationState.INVALID


@junction_guard
def test_c13_reparse_inyectado_no_llega_a_ready(rig, tmp_path) -> None:
    """C13/C41: un junction en el payload no se sigue y no habilita READY."""

    source, root = rig
    resultado = _crear(source, root)
    payload = _payload_de(resultado, root)

    fuera = tmp_path / "fuera-del-root"
    fuera.mkdir()
    (fuera / "inyectado.txt").write_bytes(b"inyectado")

    victimizado = payload / "Data" / "Texturas-inyectadas"
    # Se crea la victima con contenido (no se puede `rmdir` un dir con archivos),
    # luego se reemplaza por un junction: es el ataque real sobre un directorio.
    victimizado.mkdir()
    (victimizado / "antes.txt").write_bytes(b"antes")
    shutil.rmtree(victimizado)
    if (motivo := crear_junction(victimizado, fuera)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is not GenerationVerificationState.VALID


def test_c16_el_candidate_no_puede_autorizarse_a_si_mismo(rig, tmp_path) -> None:
    """C16/C42: el test DIRECTO de SFR-15.

    Se toma un Candidate REALMENTE creado y verificado (internamente coherente:
    tree, identidad y membership consistentes consigo mismo) y luego se reescribe
    su metadata persistida para que ``pre_source_evidence`` describa un arbol
    DISTINTO del que contiene el payload. El Candidate sigue siendo coherente
    consigo mismo y con su propio ``candidate_evidence``/``post``; lo unico que
    no cuadra es contra la Managed Source. Debe ser INVALID: la expectativa la
    impone el PRE, no el Candidate.
    """
    import json

    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID

    ruta_meta = candidate_metadata_path(root, resultado.candidate_id or "")
    datos = json.loads(ruta_meta.read_text(encoding="utf-8"))
    # Alteramos SOLO la expectativa de PRE: un digest de arbol que el payload
    # jamas pudo producir.
    datos["pre_source_evidence"]["tree_digest"]["digest"] = "f" * 64
    ruta_meta.write_text(json.dumps(datos, indent=2), encoding="utf-8")

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is GenerationVerificationState.INVALID
    assert "TreeDigest" in verificacion.message


def test_c14_la_evidencia_sobrevive_a_un_restart(rig) -> None:
    """C14: tras 'reiniciar', la metadata persistida demuestra PRE/Candidate/POST."""
    source, root = rig
    resultado = _crear(source, root, cid="cand_" + "f" * 32)
    assert resultado.state is GenerationVerificationState.VALID

    # Simulamos el reinicio leyendo SOLO desde disco, sin memoria de la corrida.
    releida = leer_metadata_candidate(candidate_metadata_path(root, "cand_" + "f" * 32))
    assert releida.state is CandidateState.READY
    assert releida.pre_source_evidence is not None
    assert releida.candidate_evidence is not None
    assert releida.post_source_evidence is not None
    assert releida.pre_source_evidence.observed_at_ns != releida.post_source_evidence.observed_at_ns
    assert releida.pre_source_evidence.directory_membership.digest == (
        releida.post_source_evidence.directory_membership.digest
    )


def test_c15_la_metadata_corrupta_falla_cerrado(rig) -> None:
    """C15: metadata truncada/corrupta => UNKNOWN, nunca READY."""
    source, root = rig
    resultado = _crear(source, root)
    ruta_meta = candidate_metadata_path(root, resultado.candidate_id or "")
    ruta_meta.write_text('{"schema_version": 1, "candidate_id": ', encoding="utf-8")

    verificacion = verificar_candidate(root, resultado.candidate_id or "")
    assert verificacion.state is GenerationVerificationState.UNKNOWN

    inventario = descubrir_candidates(root)
    assert len(inventario.records) == 1
    assert inventario.records[0].state is GenerationVerificationState.UNKNOWN


def test_c18_building_tras_un_crash_nunca_es_ready(rig, monkeypatch) -> None:
    """C18/C31: un crash a mitad de copia deja BUILDING, y BUILDING != READY."""
    source, root = rig

    class _CrashSimuladoError(RuntimeError):
        pass

    def copia_que_revienta(origen, destino, files, directories):
        raise _CrashSimuladoError("proceso muerto a mitad de la copia")

    monkeypatch.setattr(candidates_module, "copiar_arbol_independiente", copia_que_revienta)
    # El crash NO es una excepcion de negocio: se propaga, como un proceso dying.
    with pytest.raises(_CrashSimuladoError):
        _crear(source, root)

    # Lo que quedo en disco es BUILDING, y re-leerlo no lo convierte en READY.
    registros = descubrir_candidates(root).records
    assert len(registros) == 1
    assert registros[0].metadata is not None
    assert registros[0].metadata.state is CandidateState.BUILDING
    assert verificar_candidate(root, registros[0].candidate_id or "").state is GenerationVerificationState.INVALID


def test_c17_una_copia_fallida_nunca_queda_ready(rig, monkeypatch) -> None:
    """C17: un fallo de copia deja INVALID con su motivo, sin borrar contenido."""
    source, root = rig

    def copia_que_falla(origen, destino, files, directories):
        raise candidates_module.CandidateCopyError("disco lleno (simulado)")

    monkeypatch.setattr(candidates_module, "copiar_arbol_independiente", copia_que_falla)
    resultado = _crear(source, root)

    assert resultado.state is GenerationVerificationState.INVALID
    assert resultado.metadata is not None
    assert resultado.metadata.state is CandidateState.INVALID
    assert "copia fallo" in (resultado.metadata.failure_reason or "")


def test_un_archivo_ajeno_no_hunde_el_descubrimiento(rig) -> None:
    """CodeRabbit #2: un `*.json` con nombre no conforme se REGISTRA, no lanza.

    Un archivo suelto en ``state/candidates/`` no puede abortar la funcion entera:
    eso esconderia TODOS los Candidates reales detras de un `notes.json`.
    """
    source, root = rig
    resultado = crear_candidate(source, root, quiet_window_seconds=0.0, sleep=lambda _s: None)
    assert resultado.state is GenerationVerificationState.VALID

    candidates_state_dir(root).mkdir(parents=True, exist_ok=True)
    (candidates_state_dir(root) / "notes.json").write_text("{}", encoding="utf-8")

    inventario = descubrir_candidates(root)
    estados = {r.candidate_id: r.state for r in inventario.records}
    # El inventario clasifica, no verifica: el READY persistido es UNKNOWN hasta
    # que corra la verificacion fresca, y el nombre no confiable tambien.
    assert estados[resultado.candidate_id] is GenerationVerificationState.UNKNOWN
    assert estados[None] is GenerationVerificationState.UNKNOWN


def test_codex_el_inventario_no_reporta_ready_como_valid(rig) -> None:
    """Codex #904 / P1: el inventario NO es verificacion fresca.

    Tras borrar el payload de un Candidate READY, `descubrir_candidates` no puede
    seguir diciendo VALID sólo por la metadata persistida: un caller lo tomaria
    como promovible. Se reporta UNKNOWN hasta que corra la verificacion fresca.
    """
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID

    # El payload desaparece: la metadata sigue diciendo READY.
    shutil.rmtree(payload_dir(candidate_dir(root, resultado.candidate_id or "")))

    registros = descubrir_candidates(root).records
    assert len(registros) == 1
    assert registros[0].state is GenerationVerificationState.UNKNOWN
    # Y la verificacion fresca sí lo declara INVALID.
    assert verificar_candidate(root, resultado.candidate_id or "").state is (GenerationVerificationState.INVALID)


def test_codex_la_metadata_esta_atada_a_su_nombre_de_archivo(rig) -> None:
    """Codex #434 / P2: la metadata de B no puede hacerse pasar por A.

    Sin este binding, copiar el JSON de B sobre el de A (misma fuente: caso
    normal) haria que `verificar_candidate(A)` devolviera VALID con la identidad
    de B.
    """
    source, root = rig
    a = _crear(source, root, cid="cand_" + "1" * 32)
    b = _crear(source, root, cid="cand_" + "2" * 32)
    assert a.state is b.state is GenerationVerificationState.VALID

    shutil.copyfile(
        candidate_metadata_path(root, "cand_" + "2" * 32),
        candidate_metadata_path(root, "cand_" + "1" * 32),
    )

    veredicto = verificar_candidate(root, "cand_" + "1" * 32)
    assert veredicto.state is GenerationVerificationState.UNKNOWN
    assert "identidad ambigua" in veredicto.message


def test_codex_la_procedencia_se_compara_en_la_triada(rig) -> None:
    """Codex #469 / P2: un POST re-bound a otro appid no puede re-verificar VALID."""
    import json

    source, root = rig
    resultado = _crear(source, root)
    ruta_meta = candidate_metadata_path(root, resultado.candidate_id or "")
    datos = json.loads(ruta_meta.read_text(encoding="utf-8"))
    datos["post_source_evidence"]["appid"] = "999999"
    datos["post_source_evidence"]["provider_metadata"]["appid"] = "999999"
    ruta_meta.write_text(json.dumps(datos, indent=2), encoding="utf-8")

    veredicto = verificar_candidate(root, resultado.candidate_id or "")
    assert veredicto.state is GenerationVerificationState.INVALID
    assert "procedencia" in veredicto.message


def test_codex_la_ventana_de_estabilizacion_conserva_el_default_de_p1() -> None:
    """Codex #643 / P1: el default productivo es el de P1, no cero."""
    import inspect

    from sky_claw.local.frozen_runtime.stabilization import DEFAULT_QUIET_WINDOW_SECONDS

    default = inspect.signature(crear_candidate).parameters["quiet_window_seconds"].default
    assert default == DEFAULT_QUIET_WINDOW_SECONDS
    assert default > 0.0


def test_codex_un_root_dentro_de_la_fuente_es_rechazado(rig) -> None:
    """Codex #665 / P1: el storage no puede estar dentro de la Managed Source.

    Persistir BUILDING y copiar el payload dentro del arbol que Steam administra
    violaria MANAGED_SOURCE_WRITES=NO. La admision de storage acepta
    `managed_source_root` OPCIONAL, asi que un root elegido sin esa referencia
    debe re-admitirse contra la fuente CONCRETA antes de tocar nada.
    """
    source, root = rig
    dentro = source.root / "skyclaw-storage-candidate"

    resultado = crear_candidate(source, root=dentro, quiet_window_seconds=0.0, sleep=lambda _s: None)

    assert resultado.state is GenerationVerificationState.INDETERMINATE
    assert "admisible" in resultado.message
    # Lo esencial: NADA se escribio dentro de la Managed Source.
    assert not dentro.exists()


def test_codex_un_root_que_contiene_la_fuente_es_rechazado(rig, tmp_path) -> None:
    """Codex #665 (el otro sentido): el storage no puede CONTENER la fuente."""
    source, root = rig
    contenedor = source.root.parent  # contiene a la Managed Source

    resultado = crear_candidate(source, root=contenedor, quiet_window_seconds=0.0, sleep=lambda _s: None)

    assert resultado.state is GenerationVerificationState.INDETERMINATE
    assert "admisible" in resultado.message
    assert not (contenedor / "candidates").exists()


@junction_guard
def test_c19_un_junction_no_puede_redirigir_la_escritura(rig, tmp_path) -> None:
    """C19: ``candidates/<id>`` como junction => NO se escribe fuera del root."""
    source, root = rig
    fuera = tmp_path / "fuera-del-frozen-runtime-root"
    fuera.mkdir()

    objetivo = candidate_dir(root, "cand_" + "9" * 32)
    candidates_dir(root).mkdir(parents=True, exist_ok=True)
    if (motivo := crear_junction(objetivo, fuera)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    resultado = _crear(source, root, cid="cand_" + "9" * 32)
    assert resultado.state is not GenerationVerificationState.VALID
    # Nada se escribio FUERA del FrozenRuntimeRoot.
    assert list(fuera.iterdir()) == []


def test_c20_el_contrato_de_copia_soporta_otros_volumenes(rig) -> None:
    """C20: la copia es cross-volume por construccion (no asume mismo volumen).

    Se afirma sobre el MECANISMO: la copia transfiere contenido con
    ``open()``/``copyfileobj``, sin ``os.link``, ``os.replace`` ni reflink, que son
    exactamente las operaciones que fallan entre volumenes distintos. El
    destino cross-volume real se ejercita en el rig de Windows (§55).
    """
    import ast

    fuente = pathlib.Path(copying_module.__file__ or "")
    arbol = ast.parse(fuente.read_text(encoding="utf-8"))
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }
    # Ninguna primitive que asuma mismo volumen / objeto compartido.
    assert "link" not in llamadas
    assert "replace" not in llamadas
    assert "rename" not in llamadas
    assert {"copyfileobj"} <= llamadas
