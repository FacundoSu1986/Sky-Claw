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
    HALLAZGOS_DE_FALLA,
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
            {"ruta_de_salida": ruta, "estado": estado, "efecto": efecto, "veredicto": "AUTO_ACCEPT"} for ruta in rutas
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
    # Modo de 6 valores interpolados + 0 y 255 (cuando extremo_alto <= extremo_bajo).
    assert _valores_del_bloque(_bloque(0, 255, [0, 1, 2, 3, 4, 5, 6, 7] * 2)) == (0, 255, 51, 102, 153, 204, 0, 255) * 2
    assert (
        _valores_del_bloque(_bloque(128, 128, [0, 1, 2, 3, 4, 5, 6, 7] * 2))
        == (
            128,
            128,
            128,
            128,
            128,
            128,
            0,
            255,
        )
        * 2
    )


def test_el_muestreo_coprimo_no_sufre_aliasing_en_grillas_periodicas_de_2048() -> None:
    """C-3: en una 2048² (512×512 bloques) con juntas planas cada 8 bloques (32 px), el paso coprimo mide el relieve."""

    bloques_lado = 512
    bloque_plano = _bloque(128, 128, 0)
    bloque_relieve = _bloque(255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
    fila_junta = bloque_plano * bloques_lado
    fila_ladrillo = b"".join(bloque_plano if col % 8 == 0 else bloque_relieve for col in range(bloques_lado))
    payload = b"".join(fila_junta if fila % 8 == 0 else fila_ladrillo for fila in range(bloques_lado))
    dds = _dds(2048, 2048, 1, payload)

    sd = _sd_bc4(dds, _leer_cabecera(dds))

    assert sd > 50.0 / 255.0


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
    dx10_bc3 = struct.pack("<5I", 77, 3, 0, 1, 0)  # DXGI_FORMAT_BC3_UNORM (77), 2D simple
    base_dx10 = _dds(16, 16, 1, b"", fourcc=b"DX10")
    _escribir(tmp_path, "textures/a_p.dds", base_dx10 + dx10_bc3 + _payload(16, 255, 0, 0))

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
    auditoria = auditar({"version_del_lote": "np-a0a.v1", "conteos": {}}, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert not auditoria.ok


@pytest.mark.parametrize(
    "informe",
    [
        None,
        [],
        "texto",
        {"archivos": []},
        {"version_del_lote": "unknown-v99", "archivos": []},
    ],
)
def test_un_informe_no_mapeo_o_con_version_desconocida_no_lanza_y_es_hallazgo(
    tmp_path: pathlib.Path, informe: object
) -> None:
    auditoria = auditar(informe, tmp_path)  # type: ignore[arg-type]

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert not auditoria.ok


# ---------------------------------------------------------------------------
# El auditor no puede aprobar por no haber mirado nada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "informe",
    [
        {"version_del_lote": "np-a0a.v1", "archivos": []},
        {"version_del_lote": "np-a0a.v1", "archivos": ["basura"]},
        {"version_del_lote": "np-a0a.v1", "archivos": [None, 3]},
        {"version_del_lote": "np-a0a.v1", "archivos": [{"estado": "ESCRITA", "efecto": "PUBLICADA"}]},
        {
            "version_del_lote": "np-a0a.v1",
            "archivos": [{"estado": "EN_REVISION", "efecto": None, "veredicto": "REVIEW"}],
        },
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
        "version_del_lote": "np-a0a.v1",
        "archivos": [
            "basura",
            {"ruta_de_salida": "textures/a_p.dds", "estado": "ESCRITA", "efecto": "PUBLICADA"},
        ],
    }

    auditoria = auditar(informe, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.INFORME_INVALIDO,)
    assert auditoria.verificados == ()


def test_una_entrada_escrita_sin_ruta_corta_la_auditoria(tmp_path: pathlib.Path) -> None:
    """Un artefacto que se declara escrito y no dice dónde no se puede auditar ni ignorar."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    informe = {
        "version_del_lote": "np-a0a.v1",
        "archivos": [
            {"ruta_de_salida": "textures/a_p.dds", "estado": "ESCRITA", "efecto": "PUBLICADA"},
            {"estado": "ESCRITA", "efecto": "PUBLICADA"},
        ],
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
    """Entre ``is_file()`` y ``open()`` otro proceso puede llevarse o bloquear el archivo."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    def bloqueado(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        raise PermissionError("bloqueado por otro proceso")

    monkeypatch.setattr(pathlib.Path, "open", bloqueado)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.NO_LEGIBLE,)
    assert not auditoria.ok


def test_error_en_sd_bc4_no_tumba_la_auditoria(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la decodificación de bloques falla en un archivo, no debe abortar la auditoría del lote."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    _escribir(tmp_path, "textures/b_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    def decodificacion_rota(datos: bytes, cabecera: object) -> float:
        raise modulo_auditor._DdsInvalidoError("bloque corrupto simulado")

    monkeypatch.setattr(modulo_auditor, "_sd_bc4", decodificacion_rota)

    auditoria = auditar(_informe(["textures/a_p.dds", "textures/b_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.NO_LEGIBLE,)
    assert [h.codigo for h in auditoria.hallazgos] == [CodigoDeHallazgo.NO_LEGIBLE, CodigoDeHallazgo.NO_LEGIBLE]
    assert not auditoria.ok
    assert "no se pudo decodificar BC4" in auditoria.hallazgos[0].detalle


def test_un_artefacto_demasiado_grande_no_se_lee(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    monkeypatch.setattr(modulo_auditor, "MAXIMO_DE_BYTES", 100)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.TAMANO_EXCESIVO,)


def test_ati1_y_dx10_bc4_unorm_se_verifican(tmp_path: pathlib.Path) -> None:
    """El validador del generador acepta ``BC4U``, ``ATI1`` y ``DX10`` con ``DXGI_FORMAT_BC4_UNORM`` (80)."""

    payload = _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
    _escribir(tmp_path, "textures/ati1_p.dds", _dds(16, 16, 1, payload, fourcc=b"ATI1"))
    dx10_ext = struct.pack("<5I", 80, 3, 0, 1, 0)
    base_dx10 = _dds(16, 16, 1, b"", fourcc=b"DX10")
    _escribir(tmp_path, "textures/dx10_p.dds", base_dx10 + dx10_ext + payload)

    auditoria = auditar(_informe(["textures/ati1_p.dds", "textures/dx10_p.dds"]), tmp_path)

    assert auditoria.ok
    assert auditoria.verificados == ("textures/ati1_p.dds", "textures/dx10_p.dds")


def test_mips_cero_o_profundidad_volumen_son_dimensiones_invalidas(tmp_path: pathlib.Path) -> None:
    payload = _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
    _escribir(tmp_path, "textures/mips0_p.dds", _dds(16, 16, 0, payload))
    volumen = bytearray(_dds(16, 16, 1, payload))
    struct.pack_into("<I", volumen, 24, 4)  # profundidad = 4
    _escribir(tmp_path, "textures/vol_p.dds", bytes(volumen))

    auditoria = auditar(_informe(["textures/mips0_p.dds", "textures/vol_p.dds"]), tmp_path)

    assert [h.codigo for h in auditoria.hallazgos] == [
        CodigoDeHallazgo.DIMENSIONES_INVALIDAS,
        CodigoDeHallazgo.DIMENSIONES_INVALIDAS,
    ]


def test_cadena_completa_de_mips_y_bordes_de_dimensiones(tmp_path: pathlib.Path) -> None:
    """Mata mutantes en ``_bytes_de_bc4`` (niveles 16→8→4→2→1 = 23 bloques) y en ``LADO_MINIMO``/``LADO_MAXIMO``."""

    bloque = _bloque(255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
    # 16x16 con 5 mips: 16 + 4 + 1 + 1 + 1 = 23 bloques
    _escribir(tmp_path, "textures/mips5_p.dds", _dds(16, 16, 5, bloque * 23))
    # 16x16 con 6 mips (supera bit_length = 5): dimensiones inválidas
    _escribir(tmp_path, "textures/mips6_p.dds", _dds(16, 16, 6, bloque * 24))
    # 4x4 (LADO_MINIMO exacto): válido
    _escribir(tmp_path, "textures/min_p.dds", _dds(4, 4, 1, bloque))
    # 8192x4 (LADO_MAXIMO exacto): 2048x1 bloques = 2048 bloques
    _escribir(tmp_path, "textures/max_p.dds", _dds(8192, 4, 1, bloque * 2048))
    # 2x4 (< LADO_MINIMO) y 16384x4 (> LADO_MAXIMO): dimensiones inválidas
    _escribir(tmp_path, "textures/chica_p.dds", _dds(2, 4, 1, bloque))
    _escribir(tmp_path, "textures/grande_p.dds", _dds(16384, 4, 1, bloque * 4096))

    auditoria = auditar(
        _informe(
            [
                "textures/mips5_p.dds",
                "textures/mips6_p.dds",
                "textures/min_p.dds",
                "textures/max_p.dds",
                "textures/chica_p.dds",
                "textures/grande_p.dds",
            ]
        ),
        tmp_path,
    )

    assert auditoria.verificados == ("textures/mips5_p.dds", "textures/min_p.dds", "textures/max_p.dds")
    assert [(h.ruta, h.codigo) for h in auditoria.hallazgos] == [
        ("textures/mips6_p.dds", CodigoDeHallazgo.DIMENSIONES_INVALIDAS),
        ("textures/chica_p.dds", CodigoDeHallazgo.DIMENSIONES_INVALIDAS),
        ("textures/grande_p.dds", CodigoDeHallazgo.DIMENSIONES_INVALIDAS),
    ]


def test_dx10_no_2d_y_cabeceras_sin_banderas_obligatorias_se_rechazan(tmp_path: pathlib.Path) -> None:
    payload = _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
    base_dx10 = _dds(16, 16, 1, b"", fourcc=b"DX10")
    # DX10 con formato distinto de 80 pero array_size=6 (no es 2D simple): DIMENSIONES_INVALIDAS
    _escribir(tmp_path, "textures/dx10_array_p.dds", base_dx10 + struct.pack("<5I", 77, 3, 0, 6, 0) + payload)
    # Cabecera sin DDPF_FOURCC
    sin_fourcc = bytearray(_dds(16, 16, 1, payload))
    struct.pack_into("<I", sin_fourcc, 80, 0x0)
    _escribir(tmp_path, "textures/sin_fourcc_p.dds", bytes(sin_fourcc))
    # Cabecera sin DDSD_HEIGHT | DDSD_WIDTH
    sin_wh = bytearray(_dds(16, 16, 1, payload))
    struct.pack_into("<I", sin_wh, 8, 0x1001)
    _escribir(tmp_path, "textures/sin_wh_p.dds", bytes(sin_wh))

    auditoria = auditar(
        _informe(["textures/dx10_array_p.dds", "textures/sin_fourcc_p.dds", "textures/sin_wh_p.dds"]),
        tmp_path,
    )

    assert [(h.ruta, h.codigo) for h in auditoria.hallazgos] == [
        ("textures/dx10_array_p.dds", CodigoDeHallazgo.DIMENSIONES_INVALIDAS),
        ("textures/sin_fourcc_p.dds", CodigoDeHallazgo.NO_ES_DDS),
        ("textures/sin_wh_p.dds", CodigoDeHallazgo.NO_ES_DDS),
    ]


def test_colapso_frente_a_amplitud_declarada_es_altura_plana(tmp_path: pathlib.Path) -> None:
    """ADR 0014 (#797): si ``amplitud.desviacion`` era 0.20 (51/255) y el BC4 cayó a ~10/255, es ``ALTURA_PLANA``."""

    # Extremos 140 y 115 con índices 0 y 1 alternados dan sd = 12.5/255 (> SD_MINIMA = 8/255),
    # pero mucho menor que el 50 % de la amplitud declarada (0.20 -> 25.5/255).
    bloque_suave = _bloque(140, 115, [0, 1] * 8)
    _escribir(tmp_path, "textures/colapsada_p.dds", _dds(16, 16, 1, bloque_suave * 16))
    informe = {
        "version_del_lote": "np-a0a.v1",
        "archivos": [
            {
                "ruta_de_salida": "textures/colapsada_p.dds",
                "estado": "ESCRITA",
                "efecto": "PUBLICADA",
                "veredicto": "AUTO_ACCEPT",
                "amplitud": {"desviacion": 0.20},
            }
        ],
    }

    auditoria = auditar(informe, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.ALTURA_PLANA,)
    assert "cayó frente a la amplitud declarada" in auditoria.hallazgos[0].detalle


def test_amplitud_declarada_por_debajo_del_minimo_da_mensaje_exacto(tmp_path: pathlib.Path) -> None:
    """Si el relieve en disco mide sd >= 8/255 pero la amplitud previa ya era plana, el mensaje no dice 'cayó'."""

    bloque_suave = _bloque(140, 115, [0, 1] * 8)
    _escribir(tmp_path, "textures/plana_de_origen_p.dds", _dds(16, 16, 1, bloque_suave * 16))
    informe = {
        "version_del_lote": "np-a0a.v1",
        "archivos": [
            {
                "ruta_de_salida": "textures/plana_de_origen_p.dds",
                "estado": "ESCRITA",
                "efecto": "PUBLICADA",
                "veredicto": "AUTO_ACCEPT",
                "amplitud": {"desviacion": 0.01},  # 2.55/255 < SD_MINIMA
            }
        ],
    }

    auditoria = auditar(informe, tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.ALTURA_PLANA,)
    assert "amplitud declarada = 2.55/255 por debajo de 8/255" in auditoria.hallazgos[0].detalle


def test_otras_rutas_omitidas_por_cache_se_auditan_desde_disco(tmp_path: pathlib.Path) -> None:
    """C-4: una salida previa conservada en ``otras_rutas`` con ``OMITIDA`` también se relee en disco."""

    _escribir(tmp_path, "textures/nueva_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    _escribir(tmp_path, "textures/previa_p.dds", _dds(16, 16, 1, _payload(16, 128, 128, 0)))
    informe = {
        "version_del_lote": "np-a0a.v1",
        "archivos": [
            {
                "ruta_de_salida": "textures/nueva_p.dds",
                "estado": "ESCRITA",
                "efecto": "PUBLICADA",
                "veredicto": "AUTO_ACCEPT",
            }
        ],
        "otras_rutas": [{"ruta": "textures/previa_p.dds", "efecto": "OMITIDA", "detalle": "caché"}],
    }

    auditoria = auditar(informe, tmp_path)

    assert auditoria.verificados == ("textures/nueva_p.dds",)
    assert [(h.ruta, h.codigo) for h in auditoria.hallazgos] == [
        ("textures/previa_p.dds", CodigoDeHallazgo.ALTURA_PLANA)
    ]


def _informe_con_retirada(ruta_retirada: str) -> dict[str, object]:
    informe = _informe(["textures/nueva_p.dds"])
    informe["otras_rutas"] = [{"ruta": ruta_retirada, "efecto": "RETIRADA", "detalle": "ya no se desea"}]
    return informe


def test_una_ruta_declarada_retirada_que_sigue_en_disco_es_hallazgo_duro(tmp_path: pathlib.Path) -> None:
    """Una ``_p.dds`` obsoleta que el juego carga con el log en verde es el falso verde que esta capa existe para cerrar."""

    _escribir(tmp_path, "textures/nueva_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    _escribir(tmp_path, "textures/vieja_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    auditoria = auditar(_informe_con_retirada("textures/vieja_p.dds"), tmp_path)

    assert [(h.ruta, h.codigo) for h in auditoria.hallazgos] == [
        ("textures/vieja_p.dds", CodigoDeHallazgo.RETIRADA_PRESENTE)
    ]
    assert auditoria.verificados == ("textures/nueva_p.dds",)
    assert CodigoDeHallazgo.RETIRADA_PRESENTE in HALLAZGOS_DE_FALLA


def test_una_ruta_declarada_retirada_y_ausente_no_es_hallazgo(tmp_path: pathlib.Path) -> None:
    _escribir(tmp_path, "textures/nueva_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))

    auditoria = auditar(_informe_con_retirada("textures/vieja_p.dds"), tmp_path)

    assert auditoria.hallazgos == ()
    assert auditoria.verificados == ("textures/nueva_p.dds",)


def test_una_retirada_presente_se_audita_aunque_no_quede_ningun_artefacto_escrito(tmp_path: pathlib.Path) -> None:
    """Todo en revisión + una salida anterior retirada que sigue en disco: el chequeo de ausencia no puede saltearse."""

    _escribir(tmp_path, "textures/vieja_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    informe = _informe(["textures/nueva_p.dds"], estado="EN_REVISION", efecto=None)
    informe["archivos"][0]["veredicto"] = "REVIEW"  # type: ignore[index]
    informe["otras_rutas"] = [{"ruta": "textures/vieja_p.dds", "efecto": "RETIRADA", "detalle": "x"}]

    auditoria = auditar(informe, tmp_path)

    assert CodigoDeHallazgo.RETIRADA_PRESENTE in codigos(auditoria.hallazgos)
    assert CodigoDeHallazgo.SIN_ARTEFACTOS_AUDITABLES in codigos(auditoria.hallazgos)

    from sky_claw.local.parallax_unattended.composicion import combinar
    from sky_claw.local.parallax_unattended.veredicto import VeredictoDelLote, dictar

    informe["encoder"] = {"texconv_version": "may2026", "texconv_sha256": "a" * 64}
    informe["omitidas_al_enumerar"] = []
    informe["conteos"] = {"EN_REVISION": 1, "ESCRITA": 0, "NO_ESCRITA": 0, "NO_PROCESADA": 0}
    assert combinar(dictar(informe), auditoria).veredicto is VeredictoDelLote.FAIL


def test_una_retirada_que_sigue_en_disco_degrada_el_veredicto_final_a_fail(tmp_path: pathlib.Path) -> None:
    """De punta a punta: informe + disco → ``combinar`` → código de salida distinto de 0."""

    from sky_claw.local.parallax_unattended.composicion import combinar
    from sky_claw.local.parallax_unattended.veredicto import VeredictoDelLote, codigo_de_salida, dictar

    _escribir(tmp_path, "textures/nueva_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    _escribir(tmp_path, "textures/vieja_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    informe = _informe_con_retirada("textures/vieja_p.dds")
    informe["encoder"] = {"texconv_version": "may2026", "texconv_sha256": "a" * 64}

    final = combinar(dictar(informe), auditar(informe, tmp_path))

    assert final.veredicto in (VeredictoDelLote.FAIL, VeredictoDelLote.INCONSISTENTE)
    assert codigo_de_salida(final) != 0


@pytest.mark.parametrize(
    "ruta_ntfs_invalida",
    [
        "textures\\a_p.dds",
        "textures/CON.dds",
        "textures/nul_p.dds.",
        "textures/a_p.dds ",
        "textures//a_p.dds",
    ],
)
def test_rutas_con_nombres_reservados_o_sintaxis_ntfs_invalida_se_rechazan(
    tmp_path: pathlib.Path, ruta_ntfs_invalida: str
) -> None:
    auditoria = auditar(_informe([ruta_ntfs_invalida]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,)


def test_enlace_dentro_de_la_raiz_se_rechaza_sin_atravesarlo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-2: un symlink o junction dentro de la salida se rechaza como ``RUTA_FUERA_DE_LA_SALIDA``."""

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    real = modulo_auditor.link_kind_and_identity_or_raise_with_retry

    def simular_junction(ruta: pathlib.Path) -> tuple[str | None, object]:
        if ruta.name == "textures":
            return "junction", ruta.lstat()
        return real(ruta)

    monkeypatch.setattr(modulo_auditor, "link_kind_and_identity_or_raise_with_retry", simular_junction)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,)


def test_un_symlink_real_dentro_de_la_raiz_se_rechaza(tmp_path: pathlib.Path) -> None:
    """El mock de junction no prueba el enlace de verdad: acá hay un symlink en disco (se omite si el SO no deja crearlo)."""

    afuera = tmp_path / "afuera"
    _escribir(afuera, "a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    salida = tmp_path / "salida"
    salida.mkdir()
    try:
        (salida / "textures").symlink_to(afuera, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("este SO no permite crear symlinks sin privilegios")

    auditoria = auditar(_informe(["textures/a_p.dds"]), salida)

    assert codigos(auditoria.hallazgos) == (CodigoDeHallazgo.RUTA_FUERA_DE_LA_SALIDA,)


def test_un_error_transitorio_al_inspeccionar_un_enlace_se_reintenta(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermano de ``output_writer``: un antivirus que suelta el archivo a los pocos ms no debe dar un falso NO_LEGIBLE."""

    from sky_claw.app.security import links

    _escribir(tmp_path, "textures/a_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    real = links.link_kind_and_identity_or_raise
    fallos = {"pendientes": 1}

    def inestable(ruta: pathlib.Path) -> tuple[str | None, object]:
        if fallos["pendientes"] > 0:
            fallos["pendientes"] -= 1
            raise OSError("sharing violation transitoria")
        return real(ruta)

    monkeypatch.setattr(links, "link_kind_and_identity_or_raise", inestable)
    monkeypatch.setattr(links.time, "sleep", lambda _s: None)

    auditoria = auditar(_informe(["textures/a_p.dds"]), tmp_path)

    assert auditoria.hallazgos == ()
    assert fallos["pendientes"] == 0


def test_el_auditor_solo_inspecciona_enlaces_con_la_variante_con_reintento() -> None:
    """Ancla por AST: ninguna llamada ni importación de la variante SIN reintento (el hermano que se olvida)."""

    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(modulo_auditor))
    usos = {
        nodo.id
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Name) and nodo.id.startswith("link_kind_and_identity")
    }
    importados = {
        alias.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.ImportFrom)
        for alias in nodo.names
        if alias.name.startswith("link_kind_and_identity")
    }

    assert usos == {"link_kind_and_identity_or_raise_with_retry"}
    assert importados == {"link_kind_and_identity_or_raise_with_retry"}


def test_un_error_de_resolucion_o_is_file_no_aborta_el_resto_del_lote(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: ``resolve()`` con bucle de enlaces o ``is_file()`` con ``OSError`` se traduce a hallazgo por archivo."""

    _escribir(tmp_path, "textures/sana_p.dds", _dds(16, 16, 1, _payload(16, 255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)))
    original_is_file = pathlib.Path.is_file

    def is_file_con_fallo(self: pathlib.Path) -> bool:
        if self.name == "rota_p.dds":
            raise RuntimeError("Symlink loop from 'rota_p.dds'")
        return original_is_file(self)

    monkeypatch.setattr(pathlib.Path, "is_file", is_file_con_fallo)

    auditoria = auditar(_informe(["textures/rota_p.dds", "textures/sana_p.dds"]), tmp_path)

    assert [h.codigo for h in auditoria.hallazgos] == [CodigoDeHallazgo.NO_LEGIBLE]
    assert auditoria.verificados == ("textures/sana_p.dds",)
