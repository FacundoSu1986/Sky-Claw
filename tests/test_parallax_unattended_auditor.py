"""NP-UA-2: el post-check relee el artefacto desde disco; "declarado" no es "presente".

Qué se verifica y con qué:

- **La decodificación BC4 es la de la especificación, no una aproximación.** Se construyen
  bloques a mano con extremos conocidos y se afirma el valor exacto de los 8 niveles
  interpolados (``218, 182, 145, 109, 72, 36``). Sin esta ancla, los tests de "plano" y "con
  relieve" pasarían con un decodificador que devuelve cualquier cosa.
- **Un mapa plano es hallazgo.** Con extremos iguales todos los píxeles valen lo mismo y la sd
  es 0: es el caso de los cuatro mapas inertes del lote real (sd 1,1 / 1,3 / 3,2 / 3,5 sobre
  255), mientras los sanos del mismo lote dan 20,2–47,0.
- **Un mapa con relieve se verifica.** El mismo formato, con los 8 niveles usados.
- **Una ruta que sale de la raíz se rechaza** antes de abrir nada: absoluta, con unidad, con
  ``..`` o con ``:``. En desatendido, escribir fuera del mod es el daño irreversible.
- **Un payload truncado es hallazgo.** Se recalcula la cadena de mips completa de BC4 y se
  compara byte a byte: el tamaño declarado por la cabecera no se usa como verdad.
- **Un formato que no se puede medir no se aprueba.** Queda en ``no_verificados`` y el informe
  no es ``ok``: "no lo pude medir" y "está bien" no son el mismo estado.

Límite declarado: la sd se estima sobre bloques muestreados. Los tests usan texturas de 16x16,
donde se decodifican todos los bloques, así que acá la estimación es exacta; en una 2048² es
una muestra.
"""

from __future__ import annotations

import pathlib
import struct
from collections.abc import Sequence

import pytest

from sky_claw.local.parallax_unattended import auditor as modulo_auditor
from sky_claw.local.parallax_unattended.auditor import (
    SD_MINIMA,
    CodigoDeHallazgo,
    _leer_cabecera,
    _sd_bc4,
    _valores_del_bloque,
    auditar,
    codigos,
)

_NIVELES_DE_255_A_0 = (255, 0, 218, 182, 145, 109, 72, 36)


def _bloque(extremo_alto: int, extremo_bajo: int, indices: Sequence[int] | int = 0) -> bytes:
    """Arma un bloque BC4 de 8 bytes con los índices pedidos (3 bits por píxel)."""

    patron = [indices] * 16 if isinstance(indices, int) else list(indices)
    assert len(patron) == 16
    bits = 0
    for posicion, indice in enumerate(patron):
        bits |= (indice & 0x7) << (3 * posicion)
    return bytes([extremo_alto, extremo_bajo]) + bits.to_bytes(6, "little")


def _payload(lado: int, extremo_alto: int, extremo_bajo: int, indices: Sequence[int] | int) -> bytes:
    bloques_por_lado = max(1, lado // 4)
    return _bloque(extremo_alto, extremo_bajo, indices) * (bloques_por_lado * bloques_por_lado)


def _dds(ancho: int, alto: int, mips: int, payload: bytes, fourcc: bytes = b"BC4U") -> bytes:
    cabecera = bytearray(128)
    cabecera[0:4] = b"DDS "
    struct.pack_into("<I", cabecera, 4, 124)
    struct.pack_into("<I", cabecera, 8, 0x1007)
    struct.pack_into("<I", cabecera, 12, alto)
    struct.pack_into("<I", cabecera, 16, ancho)
    struct.pack_into("<I", cabecera, 20, len(payload))
    struct.pack_into("<I", cabecera, 24, 1)
    struct.pack_into("<I", cabecera, 28, mips)
    struct.pack_into("<I", cabecera, 76, 32)
    struct.pack_into("<I", cabecera, 80, 0x4)
    cabecera[84:88] = fourcc
    struct.pack_into("<I", cabecera, 108, 0x401008)
    return bytes(cabecera) + payload


def _informe(rutas: Sequence[str], *, estado: str = "ESCRITA", efecto: str | None = "PUBLICADA") -> dict[str, object]:
    return {
        "version_del_lote": "np-a0a.v1",
        "conteos": {estado: len(rutas)},
        "archivos": [
            {"ruta_de_salida": ruta, "estado": estado, "efecto": efecto, "veredicto": "PASS"} for ruta in rutas
        ],
    }


def _escribir(raiz: pathlib.Path, ruta: str, datos: bytes) -> None:
    destino = raiz / ruta
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)


# ---------------------------------------------------------------------------
# El decodificador, anclado contra la especificación
# ---------------------------------------------------------------------------


def test_el_decodificador_devuelve_los_ocho_niveles_de_la_especificacion() -> None:
    assert _valores_del_bloque(_bloque(255, 0, 0)) == (255,) * 16
    assert _valores_del_bloque(_bloque(255, 0, 1)) == (0,) * 16
    assert _valores_del_bloque(_bloque(255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)) == _NIVELES_DE_255_A_0 * 2


def test_la_sd_del_plano_es_cero_y_la_del_relieve_supera_el_umbral() -> None:
    plano = _dds(16, 16, 1, _payload(16, 128, 128, 0))
    relieve = _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2))

    assert _sd_bc4(plano, _leer_cabecera(plano)) == pytest.approx(0.0, abs=1e-9)
    assert _sd_bc4(relieve, _leer_cabecera(relieve)) > SD_MINIMA * 6


# ---------------------------------------------------------------------------
# Artefacto
# ---------------------------------------------------------------------------


def test_un_mapa_plano_es_hallazgo(tmp_path: pathlib.Path) -> None:
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 128, 128, 0)))

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert not auditoria.ok
    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.ALTURA_PLANA,)
    assert auditoria.verificados == ()


def test_un_mapa_con_relieve_se_verifica(tmp_path: pathlib.Path) -> None:
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert auditoria.ok
    assert auditoria.verificados == ("textures/a_p.dds",)
    assert auditoria.hallazgos == ()


def test_un_archivo_omitido_por_cache_se_audita_igual(tmp_path: pathlib.Path) -> None:
    """``OMITIDA`` deja el archivo de la corrida anterior: se audita, no se saltea."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 128, 128, 0)))

    auditoria = auditar(_informe(["textures/a_p.dds"], efecto="OMITIDA"), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.ALTURA_PLANA,)


def test_un_payload_truncado_es_hallazgo(tmp_path: pathlib.Path) -> None:
    completo = _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2))
    _escribir(tmp_path, "textures/a_p.dds", completo[:-8])

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.TAMANO_INCOHERENTE,)


def test_un_archivo_que_no_es_dds_es_hallazgo(tmp_path: pathlib.Path) -> None:
    _escribir(tmp_path, "textures/a_p.dds", b"esto no es un dds" * 16)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.NO_ES_DDS,)


def test_un_formato_que_no_se_puede_medir_no_se_aprueba(tmp_path: pathlib.Path) -> None:
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, 0), fourcc=b"DX10"))

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert not auditoria.ok
    assert auditoria.hallazgos == ()
    assert auditoria.no_verificados == ("textures/a_p.dds",)


def test_un_archivo_declarado_y_ausente_es_hallazgo(tmp_path: pathlib.Path) -> None:
    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.NO_EXISTE,)


# ---------------------------------------------------------------------------
# La raíz de salida es un límite, no una sugerencia
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta",
    [
        "../fuera_p.dds",
        "textures/../../fuera_p.dds",
        "C:/Windows/fuera_p.dds",
        "/etc/fuera_p.dds",
        "textures/a_p.dds:flujo",
    ],
)
def test_una_ruta_que_sale_de_la_raiz_se_rechaza(tmp_path: pathlib.Path, ruta: str) -> None:
    """El límite es la PROPIEDAD (nada fuera de la raíz), no una de las capas que la sostienen.

    ``..``, la unidad y lo absoluto quedan rechazados por la contención de todos modos; el test
    ancla el resultado. Quitar sólo una capa no lo mata porque son redundantes a propósito
    (mutante M4 documentado); quitar el límite entero sí lo mata.
    """
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    auditoria = auditar(_informe([ruta]), tmp_path)

    assert not auditoria.ok
    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,)


def test_un_informe_sin_archivos_es_hallazgo(tmp_path: pathlib.Path) -> None:
    auditoria = auditar({"conteos": {}}, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert not auditoria.ok


# ---------------------------------------------------------------------------
# El auditor no puede aprobar por no haber mirado nada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "informe",
    [
        {"archivos": []},
        {"archivos": ["basura"]},
        {"archivos": [None, 3]},
        {"archivos": [{"estado": "ESCRITA", "efecto": "PUBLICADA"}]},
        {"archivos": [{"estado": "EN_REVISION", "efecto": None, "veredicto": "REVIEW"}]},
    ],
)
def test_cero_artefactos_auditados_nunca_es_ok(tmp_path: pathlib.Path, informe: dict[str, object]) -> None:
    """Un ``ok`` sin nada verificado sería el mismo falso verde que esta capa cierra."""

    auditoria = auditar(informe, tmp_path)

    assert not auditoria.ok
    assert auditoria.verificados == ()


def test_una_entrada_que_no_es_objeto_corta_la_auditoria(tmp_path: pathlib.Path) -> None:
    """Saltear la entrada rota dejaría el resto auditado y el lote aprobado."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    informe = {
        "archivos": [
            "basura",
            {"ruta_de_salida": "textures/a_p.dds", "estado": "ESCRITA", "efecto": "PUBLICADA"},
        ]
    }

    auditoria = auditar(informe, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert auditoria.verificados == ()


def test_una_entrada_escrita_sin_ruta_corta_la_auditoria(tmp_path: pathlib.Path) -> None:
    """Un artefacto que se declara escrito y no dice dónde no se puede auditar ni ignorar."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    informe = {
        "archivos": [
            {"ruta_de_salida": "textures/a_p.dds", "estado": "ESCRITA", "efecto": "PUBLICADA"},
            {"estado": "ESCRITA", "efecto": "PUBLICADA"},
        ]
    }

    auditoria = auditar(informe, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert auditoria.verificados == ()


# ---------------------------------------------------------------------------
# Lo que el formato permite no se rechaza
# ---------------------------------------------------------------------------


def test_una_textura_que_no_es_potencia_de_dos_se_acepta(tmp_path: pathlib.Path) -> None:
    """``--escala-x 0.75`` produce 1536: DDS y BC4 lo aceptan y el formato no pide POT."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(12, 12, 1, _payload(12, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert auditoria.ok
    assert auditoria.verificados == ("textures/a_p.dds",)


def test_un_archivo_ilegible_no_tumba_la_auditoria(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entre ``is_file()`` y ``read_bytes()`` otro proceso puede llevarse el archivo."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    def bloqueado(self: pathlib.Path) -> bytes:
        raise PermissionError("bloqueado por otro proceso")

    monkeypatch.setattr(pathlib.Path, "read_bytes", bloqueado)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.NO_LEGIBLE,)
    assert not auditoria.ok


def test_un_artefacto_demasiado_grande_no_se_lee(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    monkeypatch.setattr(modulo_auditor, "MAXIMO_DE_BYTES", 100)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.TAMANO_EXCESIVO,)
