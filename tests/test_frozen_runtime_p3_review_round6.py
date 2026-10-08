"""P3 ronda 6 — P3-W (solapamiento origen/destino), P3-X (timestamps obligatorios) y P3-Y (provider metadata estricto).

Findings revalidados contra `6a1e9cca`:

* P3-W (`copying.py`): `copiar_arbol_independiente()` es una primitive
  publica/reutilizable y no impone la frontera ``source ∩ destination = ∅``.
  Un caller directo con ``origen = contenedor = Managed Source`` y
  ``destino = <source>/candidates/X/payload`` pasa los guards actuales
  (``destino ∈ contenedor``) y ejecuta ``mkdir``/``open("xb")`` DENTRO de la
  Managed Source: ``MANAGED_SOURCE_WRITES=NO`` violado en la propia primitive.

* P3-X (`candidates.py`): el loader usaba ``_entero_json_opcional(...,
  defecto=0)`` para ``created_at_ns``/``updated_at_ns``; un schema-v1
  truncado se reconstruia como completo. Ademas
  ``source_provider``/``source_appid`` aceptaban ausencia via default (``""``).

* P3-Y (`candidates.py`): ``buildid = {}`` se normalizaba como ``None`` ("no
  observado"), silenciando contradicciones PRE/POST que
  ``_buildid_contradictoire`` debio haber rechazado, y
  ``manifest_parse_error`` ni siquiera se reconstruia: el round-trip perdia
  evidencia persistida.

RED-first: cada test se vio FALLAR contra ``6a1e9cca`` por la vulnerabilidad
especifica que denuncia (``P3_W_PRE_FIX_RED`` / ``P3_X_PRE_FIX_RED`` /
``P3_Y_PRE_FIX_RED`` en el informe de cierre).
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime.candidates import (
    CANDIDATE_SCHEMA_VERSION,
    _evidencia_desde_dict,
    candidate_metadata_path,
    descubrir_candidates,
    leer_metadata_candidate,
    serializar_metadata_candidate,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError, CandidateCorruptMetadataError
from sky_claw.local.frozen_runtime.membership import construir_evidencia_membership
from sky_claw.local.frozen_runtime.models import ManagedSourceProvider, ProviderMetadataObservation
from sky_claw.local.frozen_runtime.storage_models import (
    CandidateMetadata,
    CandidateSourceEvidence,
    CandidateState,
    GenerationVerificationState,
)
from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401

# Header obligatorio de schema-v1, escrito A MANO a proposito: si el serializer
# o el loader cambian su conjunto de campos sin actualizar este literal, el
# test de congelacion de P3-X falla (divergencia silenciosa).
_HEADER_SCHEMA_V1 = frozenset(
    {
        "schema_version",
        "candidate_id",
        "state",
        "created_at_ns",
        "updated_at_ns",
        "source_provider",
        "source_appid",
        "pre_source_evidence",
        "candidate_evidence",
        "post_source_evidence",
        "failure_reason",
    }
)


# ── helpers compartidos ────────────────────────────────────────────────────


def _ready_y_meta(rig):  # noqa: ANN001, ANN202, F811
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID
    return root, resultado.candidate_id, candidate_metadata_path(root, resultado.candidate_id or "")


def _leer_json(ruta: pathlib.Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir_json(ruta: pathlib.Path, datos: dict) -> None:
    ruta.write_text(json.dumps(datos, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# P3-W · copiar_arbol_independiente rechaza solapamiento origen/destino
# ══════════════════════════════════════════════════════════════════════════


def _fuente_p3w(tmp_path: pathlib.Path) -> pathlib.Path:
    """Managed Source sintetica: Data/ (con hijo) + SkyrimSE.exe."""
    source = tmp_path / "steamapps" / "common" / "Skyrim Special Edition"
    (source / "Data").mkdir(parents=True)
    (source / "SkyrimSE.exe").write_bytes(b"exe")
    (source / "Data" / "Skyrim.esm").write_bytes(b"esm")
    return source


_ARCHIVOS_P3W = (
    FileIdentity(rel_path="SkyrimSE.exe", size=3, digest="a" * 64),
    FileIdentity(rel_path="Data/Skyrim.esm", size=3, digest="b" * 64),
)


def _snapshot_arbol(raiz: pathlib.Path) -> dict[str, bytes | None]:
    """Membresia + bytes del arbol: archivos con su contenido, dirs con None."""
    salida: dict[str, bytes | None] = {}
    for entrada in sorted(raiz.rglob("*")):
        rel = entrada.relative_to(raiz).as_posix()
        salida[rel] = entrada.read_bytes() if entrada.is_file() else None
    return salida


def test_p3w_un_destino_dentro_del_origen_se_rechaza_antes_de_mutar(tmp_path) -> None:  # noqa: ANN001
    """P3-W RED: la primitive no puede escribir dentro de su propio source.

    El caller hostil arma el namespace DENTRO de la Managed Source (el
    escenario exacto del finding: origen = contenedor = Managed Source) y
    llama a la primitive PUBLICA directamente, saltandose la admision de
    storage de ``crear_candidate``. Pre-fix la copia tenia exito y mutaba el
    source; post-fix debe rechazar ANTES del primer mkdir.
    """
    source = _fuente_p3w(tmp_path)
    (source / "candidates" / "cand_p3w").mkdir(parents=True)
    destino = source / "candidates" / "cand_p3w" / "payload"
    antes = _snapshot_arbol(source)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(source, destino, _ARCHIVOS_P3W, ("Data",), contenedor=source)

    assert _snapshot_arbol(source) == antes, "el Managed Source fue mutado (MANAGED_SOURCE_WRITES=NO)"
    assert not destino.exists(), "el payload no debe existir dentro de la Managed Source"


def test_p3w_origen_igual_al_destino_se_rechaza(tmp_path) -> None:  # noqa: ANN001
    """P3-W: source == destination es un self-copy y se rechaza."""
    source = _fuente_p3w(tmp_path)
    antes = _snapshot_arbol(source)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(source, source, _ARCHIVOS_P3W, ("Data",), contenedor=source)

    assert _snapshot_arbol(source) == antes


def test_p3w_un_origen_dentro_del_destino_tambien_se_rechaza(tmp_path) -> None:  # noqa: ANN001
    """P3-W: el solapamiento INVERSO tambien se rechaza (decision documentada).

    Si destino contiene al origen, cualquier escritura en el destino opera
    sobre el ancestro del source mientras el source se esta leyendo: es
    self-copy con contaminacion del namespace de origen, no una copia
    independiente. La propiedad que se defiende es
    ``source tree ∩ destination tree = ∅`` en AMBAS direcciones.
    """
    source = _fuente_p3w(tmp_path)
    destino = source.parent  # common/ CONTIENE a Skyrim Special Edition/
    antes = _snapshot_arbol(source)
    antes_destino = _snapshot_arbol(destino)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(source, destino, _ARCHIVOS_P3W, ("Data",), contenedor=destino)

    assert _snapshot_arbol(source) == antes
    assert _snapshot_arbol(destino) == antes_destino


def test_p3w_copia_legitima_entre_arboles_disjuntos_pasa(tmp_path) -> None:  # noqa: ANN001
    """P3-W control: el guard no puede volver rechazo general a la copia valida."""
    source = _fuente_p3w(tmp_path)
    raiz = tmp_path / "frozen-runtime"
    (raiz / "candidates" / "cand_legit").mkdir(parents=True)
    destino = raiz / "candidates" / "cand_legit" / "payload"

    copiados = copiar_arbol_independiente(source, destino, _ARCHIVOS_P3W, ("Data",), contenedor=raiz)

    assert copiados == 2
    assert (destino / "SkyrimSE.exe").read_bytes() == b"exe"
    assert (destino / "Data" / "Skyrim.esm").read_bytes() == b"esm"
    assert (destino / "Data").is_dir()


# ══════════════════════════════════════════════════════════════════════════
# P3-X · created_at_ns/updated_at_ns obligatorios + header schema-v1
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("campo", ["created_at_ns", "updated_at_ns"])
def test_p3x_timestamp_ausente_es_corrupcion(rig, campo: str) -> None:  # noqa: F811
    """P3-X RED: la ausencia ya no se reconstruye como 0 y pasa por completo."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos.pop(campo)
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN
    assert descubrir_candidates(root).records[0].state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("created_at_ns", -1),
        ("updated_at_ns", -1),
        ("created_at_ns", None),
        ("updated_at_ns", None),
        ("created_at_ns", 1.5),
        ("updated_at_ns", "123"),
    ],
)
def test_p3x_timestamp_invalido_es_corrupcion(rig, campo: str, valor) -> None:  # noqa: F811, ANN001
    """P3-X: negativo/null/float/string no son timestamps persistidos validos.

    null/float/string ya los rechazaba el entero estricto de P3-S (controles);
    el negativo era el caso fail-open de este finding.
    """
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos[campo] = valor
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize("campo", ["created_at_ns", "updated_at_ns"])
def test_p3x_timestamp_bool_es_corrupcion(rig, campo: str) -> None:  # noqa: F811
    """P3-X control: bool es subclase de int y nunca cuenta como timestamp."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos[campo] = True
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


def test_p3x_timestamps_validos_se_aceptan(rig) -> None:  # noqa: F811
    """P3-X control: el rechazo no se lleva puestos timestamps legitimos."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos["created_at_ns"] = 0
    datos["updated_at_ns"] = 1_700_000_000_000_000_000
    _escribir_json(meta, datos)

    cargada = leer_metadata_candidate(meta)
    assert cargada.created_at_ns == 0
    assert cargada.updated_at_ns == 1_700_000_000_000_000_000
    assert verificar_candidate(root, cid).state is GenerationVerificationState.VALID


def test_p3x_el_header_requerido_de_schema_v1_esta_congelado(rig) -> None:  # noqa: F811
    """P3-X: serializer y loader comparten el MISMO header obligatorio.

    Primero se exige que CADA campo ausente sea corrupcion tipada (la
    vulnerabilidad concreta: los defaults dispersos reconstruian metadata
    truncada como completa); despues se congela la declaracion del loader
    contra el literal escrito a mano, para que ninguna de las dos caras
    pueda divergir en silencio.
    """
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    assert frozenset(datos) == _HEADER_SCHEMA_V1, "el serializer dejo de emitir el header congelado"

    for campo in sorted(_HEADER_SCHEMA_V1):
        mutado = dict(datos)
        mutado.pop(campo)
        _escribir_json(meta, mutado)
        with pytest.raises(CandidateCorruptMetadataError):
            leer_metadata_candidate(meta)
    _escribir_json(meta, datos)

    declarado = getattr(candidates_module, "CAMPOS_OBLIGATORIOS_SCHEMA_V1", None)
    assert declarado == _HEADER_SCHEMA_V1, "el loader no congela el mismo header que el serializer"


# ══════════════════════════════════════════════════════════════════════════
# P3-Y · provider metadata: string-or-none estricto + round-trip completo
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("buildid", {}),
        ("buildid", 123),
        ("buildid", ["1234567"]),
        ("state_flags", []),
        ("update_result", False),
        ("install_dir", {}),
        ("manifest_parse_error", 5),
    ],
)
def test_p3y_campo_string_malformado_es_corrupcion(rig, campo: str, valor) -> None:  # noqa: F811, ANN001
    """P3-Y RED: un tipo equivocado NO se normaliza a None (fallo silencioso)."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos["pre_source_evidence"]["provider_metadata"][campo] = valor
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN
    assert descubrir_candidates(root).records[0].state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize("campo", ["buildid", "state_flags", "update_result", "install_dir", "manifest_parse_error"])
def test_p3y_null_sigue_siendo_legitimo(rig, campo: str) -> None:  # noqa: F811
    """P3-Y control: ``null`` sigue significando "el manifest no lo expone"."""
    _root, _cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos["pre_source_evidence"]["provider_metadata"][campo] = None
    _escribir_json(meta, datos)

    evidencia = leer_metadata_candidate(meta).pre_source_evidence
    assert evidencia is not None
    assert getattr(evidencia.provider_metadata, campo) is None


@pytest.mark.parametrize("campo", ["buildid", "state_flags", "update_result", "install_dir", "manifest_parse_error"])
def test_p3y_strings_reales_siguen_aceptados(rig, campo: str) -> None:  # noqa: F811
    """P3-Y control: el endurecimiento no rompe la deserializacion legitima.

    El valor se pone en PRE Y POST a la vez: cambiar solo el PRE de un campo
    que la triada compara (``buildid``) produjera una contradiccion LEGITIMA y
    el veredicto correcto seria INVALID, no VALID.
    """
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    for posicion in ("pre_source_evidence", "post_source_evidence"):
        datos[posicion]["provider_metadata"][campo] = f"valor-{campo}"
    _escribir_json(meta, datos)

    evidencia = leer_metadata_candidate(meta).pre_source_evidence
    assert evidencia is not None
    assert getattr(evidencia.provider_metadata, campo) == f"valor-{campo}"
    assert verificar_candidate(root, cid).state is GenerationVerificationState.VALID


def test_p3y_un_buildid_pre_malformado_no_puede_silenciar_la_contradiccion(rig) -> None:  # noqa: F811
    """P3-Y RED (ataque): ``{}`` en PRE no puede pasar por "no observado".

    Pre-fix ``{}`` se normalizaba a ``None``; ``_buildid_contradictoire`` veia
    ``None`` = "el manifest no lo expone" y NO producia veredicto, asi que la
    contradiccion contra el POST con buildid legitimo quedaba silenciada y el
    Candidate re-verificaba VALID. Post-fix la corrupcion se detecta al
    deserializar, ANTES de la triada: VALID es imposible.
    """
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos["pre_source_evidence"]["provider_metadata"]["buildid"] = {}
    # El POST conserva el buildid legitimo del manifest ("1234567").
    assert datos["post_source_evidence"]["provider_metadata"]["buildid"] == "1234567"
    _escribir_json(meta, datos)

    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN
    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)


@pytest.mark.parametrize("campo", ["provider", "appid"])
def test_p3y_provider_metadata_contradictorio_es_corrupcion(rig, campo: str) -> None:  # noqa: F811
    """P3-Y: el loader no puede IGNORAR valores persistidos que contradicen la evidencia.

    P1 y la observacion del Candidate construyen provider/appid IGUALES a los
    de la evidencia (coherencia por construccion); un JSON que los contradice
    es tampering/corrupcion. Pre-fix el loader ni siquiera los leia: usaba los
    del nivel superior e ignoraba los del bloque.
    """
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    bloque = datos["pre_source_evidence"]["provider_metadata"]
    assert bloque[campo] == datos["pre_source_evidence"][campo], "el fixture debe partir coherente"
    bloque[campo] = "999999"
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize("campo", ["provider", "appid"])
def test_p3y_provider_metadata_ausente_es_corrupcion(rig, campo: str) -> None:  # noqa: F811
    """P3-Y: el serializer SIEMPRE emite provider/appid; ausencia = corrupcion."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    datos["pre_source_evidence"]["provider_metadata"].pop(campo)
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


def _provider_metadata_llena() -> ProviderMetadataObservation:
    """Observacion con TODOS los campos que el serializer persiste poblados."""
    return ProviderMetadataObservation(
        provider=ManagedSourceProvider.STEAM,
        appid="489830",
        buildid="7654321",
        state_flags="4",
        bytes_to_download=10,
        bytes_downloaded=5,
        update_result="0",
        install_dir="Skyrim Special Edition",
        manifest_readable=False,
        manifest_parse_error="boom",
    )


def _campos_persistidos(pm: ProviderMetadataObservation) -> tuple[object, ...]:
    """Los 10 campos de la familia serializada (sin manifest_path ni observed)."""
    return (
        pm.provider,
        pm.appid,
        pm.buildid,
        pm.state_flags,
        pm.bytes_to_download,
        pm.bytes_downloaded,
        pm.update_result,
        pm.install_dir,
        pm.manifest_readable,
        pm.manifest_parse_error,
    )


def test_p3y_la_familia_serializada_sobrevive_al_round_trip_por_disco(rig) -> None:  # noqa: F811
    """P3-Y: los 10 campos persistidos sobreviven serialize -> disco -> loader.

    ``manifest_path`` NO pertenece al JSON persistido (no se serializa), asi
    que no se exige; ``manifest_parse_error`` SI, y pre-fix se perdia.
    """
    _root, _cid, meta = _ready_y_meta(rig)
    metadata = leer_metadata_candidate(meta)
    assert metadata.pre_source_evidence is not None
    pm_llena = _provider_metadata_llena()
    metadata_llena = replace(
        metadata, pre_source_evidence=replace(metadata.pre_source_evidence, provider_metadata=pm_llena)
    )

    _escribir_json(meta, serializar_metadata_candidate(metadata_llena))
    recargada = leer_metadata_candidate(meta).pre_source_evidence
    assert recargada is not None
    assert _campos_persistidos(recargada.provider_metadata) == _campos_persistidos(pm_llena)


def test_p3y_el_evidencia_desde_dict_es_simetrico_con_el_serializer() -> None:
    """P3-Y: simetria directa serializer/decoder sin pasar por disco."""
    critico = (FileIdentity(rel_path="SkyrimSE.exe", size=1, digest="a" * 64),)
    pm_llena = _provider_metadata_llena()
    evidencia = CandidateSourceEvidence(
        provider="steam",
        appid="489830",
        game_key="skyrimse",
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        tree_digest=TreeDigest(digest="b" * 64, files=1, bytes=1),
        directory_membership=construir_evidencia_membership(("Data",)),
        critical_files=critico,
        provider_metadata=pm_llena,
        observed_at_ns=1,
        files=critico,
    )
    serializada = serializar_metadata_candidate(
        CandidateMetadata(
            schema_version=CANDIDATE_SCHEMA_VERSION,
            candidate_id="cand_" + "0" * 32,
            state=CandidateState.BUILDING,
            created_at_ns=1,
            updated_at_ns=1,
            source_provider="steam",
            source_appid="489830",
            pre_source_evidence=evidencia,
            candidate_evidence=None,
            post_source_evidence=None,
            failure_reason=None,
        )
    )
    recargada = _evidencia_desde_dict(serializada["pre_source_evidence"], etiqueta="round-trip")
    assert _campos_persistidos(recargada.provider_metadata) == _campos_persistidos(pm_llena)
