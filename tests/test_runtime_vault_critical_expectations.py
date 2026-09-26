"""Serialización canónica y digest de critical_expectations (ADR 0010 §11.4 / RVO-10).

Congela la única serialización admisible de la lista confirmada de expectativas
críticas que el receipt liga con `critical_expectations_digest`:

- cada entrada tiene exactamente `rel_path`, `expected_digest` y `expected_size`;
- `rel_path` usa la normalización anti-traversal de `CriticalFileExpectation` y
  los paths normalizados duplicados se rechazan;
- las entradas se ordenan por orden lexicográfico de los bytes UTF-8 de `rel_path`;
- `expected_digest` en minúsculas; `expected_size` se serializa SIEMPRE
  (entero JSON no negativo, no boolean, o `null`);
- JSON array UTF-8 sin BOM ni newline final, `ensure_ascii=False`, campos de
  objeto ordenados lexicográficamente, separadores compactos `(',', ':')`;
- la lista vacía se serializa exactamente como `[]` y su digest es
  `SHA-256(UTF-8("[]"))`.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from sky_claw.local.runtime_vault.critical_expectations import (
    CriticalExpectationsDigestError,
    canonical_critical_expectations_bytes,
    critical_expectations_digest,
)
from sky_claw.local.runtime_vault.models import CriticalFileExpectation

_D_A = "a" * 64
_D_B = "b" * 64


def _e(rel_path: str, digest: str = _D_A, size: int | None = None) -> CriticalFileExpectation:
    return CriticalFileExpectation(rel_path=rel_path, expected_digest=digest, expected_size=size)


class TestListaVacia:
    def test_lista_vacia_se_serializa_exactamente_como_llaves_abiertas(self) -> None:
        assert canonical_critical_expectations_bytes(()) == b"[]"

    def test_digest_de_lista_vacia_es_sha256_de_llaves_abiertas(self) -> None:
        esperado = hashlib.sha256(b"[]").hexdigest()
        assert critical_expectations_digest(()) == esperado

    def test_lista_vacia_no_se_sustituye_por_null_ni_se_omite(self) -> None:
        assert critical_expectations_digest(()) != critical_expectations_digest((_e("a.esm"),))


class TestSerializacionCanonica:
    def test_bytes_sin_bom_ni_newline_final(self) -> None:
        raw = canonical_critical_expectations_bytes((_e("a.esm"),))
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert not raw.endswith(b"\n")
        assert not raw.endswith(b"\r")

    def test_bytes_son_utf8_y_ensure_ascii_falso(self) -> None:
        raw = canonical_critical_expectations_bytes((_e("Data/\u00f1and\u00fa.esm"),))
        assert "\u00f1".encode("utf-8") in raw
        assert b"\\u00f1" not in raw
        raw.decode("utf-8")

    def test_separadores_compactos_sin_espacios(self) -> None:
        raw = canonical_critical_expectations_bytes((_e("a.esm", size=7),))
        texto = raw.decode("utf-8")
        assert ", " not in texto
        assert ": " not in texto

    def test_campos_de_objeto_ordenados_lexicograficamente(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("a.esm", size=7),)).decode("utf-8")
        pos_dig = texto.index('"expected_digest"')
        pos_size = texto.index('"expected_size"')
        pos_rel = texto.index('"rel_path"')
        assert pos_dig < pos_size < pos_rel

    def test_entrada_tiene_exactamente_tres_campos(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("a.esm", size=1),)).decode("utf-8")
        entrada = json.loads(texto)[0]
        assert set(entrada) == {"rel_path", "expected_digest", "expected_size"}

    def test_digest_de_entrada_va_en_minusculas(self) -> None:
        # El modelo ya normaliza a minúsculas; el digest de lista debe reflejarlo.
        esperado = critical_expectations_digest(
            (CriticalFileExpectation(rel_path="a.esm", expected_digest=_D_A.upper()),)
        )
        assert _D_A.upper() not in canonical_critical_expectations_bytes(
            (CriticalFileExpectation(rel_path="a.esm", expected_digest=_D_A.upper()),)
        ).decode("utf-8")
        assert esperado == critical_expectations_digest((_e("a.esm"),))


class TestOrdenYUnicidad:
    def test_entradas_ordenadas_por_bytes_utf8_de_rel_path(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("Data/b.esm"), _e("Data/a.esm"), _e("Data/A.esm"))).decode(
            "utf-8"
        )
        rutas = [e["rel_path"] for e in json.loads(texto)]
        assert rutas == sorted(rutas, key=lambda r: r.encode("utf-8"))

    def test_digest_invariante_al_orden_de_las_entradas(self) -> None:
        a = critical_expectations_digest((_e("x.esm", _D_A, 1), _e("y.esm", _D_B, 2)))
        b = critical_expectations_digest((_e("y.esm", _D_B, 2), _e("x.esm", _D_A, 1)))
        assert a == b

    def test_digest_cambia_si_cambia_el_conjunto(self) -> None:
        base = critical_expectations_digest((_e("x.esm"),))
        distinto = critical_expectations_digest((_e("y.esm"),))
        assert base != distinto

    def test_duplicados_normalizados_se_rechazan(self) -> None:
        with pytest.raises(CriticalExpectationsDigestError):
            canonical_critical_expectations_bytes((_e("Data/a.esm"), _e("Data\\a.esm")))

    def test_digest_de_duplicados_tambien_falla_cerrado(self) -> None:
        with pytest.raises(CriticalExpectationsDigestError):
            critical_expectations_digest((_e("a.esm"), _e("./a.esm")))


class TestExpectedSize:
    def test_expected_size_siempre_se_serializa(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("a.esm"),)).decode("utf-8")
        assert '"expected_size":null' in texto

    def test_expected_size_null_se_serializa_como_null(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("a.esm", size=None),)).decode("utf-8")
        assert '"expected_size":null' in texto

    def test_expected_size_entero_no_negativo(self) -> None:
        texto = canonical_critical_expectations_bytes((_e("a.esm", size=0),)).decode("utf-8")
        assert '"expected_size":0' in texto

    def test_expected_size_booleano_se_rechaza(self) -> None:
        con_bool = CriticalFileExpectation(rel_path="a.esm", expected_digest=_D_A, expected_size=True)
        with pytest.raises(CriticalExpectationsDigestError):
            canonical_critical_expectations_bytes((con_bool,))

    def test_expected_size_float_se_rechaza(self) -> None:
        con_float = CriticalFileExpectation(rel_path="a.esm", expected_digest=_D_A, expected_size=1.5)  # type: ignore[arg-type]
        with pytest.raises(CriticalExpectationsDigestError):
            canonical_critical_expectations_bytes((con_float,))

    def test_expected_size_negativo_lo_rechaza_el_modelo(self) -> None:
        with pytest.raises(ValueError):
            CriticalFileExpectation(rel_path="a.esm", expected_digest=_D_A, expected_size=-1)


class TestEntradaDeTipos:
    def test_lista_de_diccionarios_se_rechaza(self) -> None:
        with pytest.raises(CriticalExpectationsDigestError):
            canonical_critical_expectations_bytes(  # type: ignore[arg-type]
                ({"rel_path": "a.esm", "expected_digest": _D_A, "expected_size": None},)
            )

    def test_elemento_no_modelo_se_rechaza(self) -> None:
        with pytest.raises(CriticalExpectationsDigestError):
            canonical_critical_expectations_bytes(("a.esm",))  # type: ignore[arg-type]


class TestContratoDeFunciones:
    def test_digest_es_sha256_hex_de_64(self) -> None:
        digest = critical_expectations_digest((_e("a.esm", size=3),))
        assert len(digest) == 64
        assert digest == digest.lower()
        assert all(c in "0123456789abcdef" for c in digest)

    def test_digest_es_sha256_de_los_bytes_canonicos(self) -> None:
        esperados = (
            CriticalFileExpectation(rel_path="Data/a.esm", expected_digest=_D_A, expected_size=10),
            CriticalFileExpectation(rel_path="Data/b.esm", expected_digest=_D_B),
        )
        assert (
            critical_expectations_digest(esperados)
            == hashlib.sha256(canonical_critical_expectations_bytes(esperados)).hexdigest()
        )
