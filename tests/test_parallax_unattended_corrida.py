"""#802: el punto de entrada de la corrida desatendida compone la secuencia COMPLETA.

La capa ``parallax_unattended`` se escribió para que la tubería corra sin operador, pero la secuencia
``dictar -> auditar -> combinar -> codigo_de_salida`` solo existía documentada. Un integrador que la rearmara
a mano podía saltearse ``combinar`` y dejar un lote ``PASS`` con artefactos planos (el defecto que la capa existe para
cerrar). ``decidir_corrida`` y ``python -m sky_claw.local.parallax_unattended`` son el ÚNICO punto de entrada.

Qué se verifica y con qué:

- **La secuencia completa corre de punta a punta** con archivos reales en ``tmp_path``: un ``PASS`` del informe con un
  artefacto plano en disco se degrada a ``REVIEW`` (eso solo ocurre si ``combinar`` participa).
- **Un informe ilegible es ``INCONSISTENTE``, nunca una excepción ni un ``PASS``** (fail-closed): ausente, no UTF-8,
  JSON roto, que no es un objeto, con constantes no estándar o con claves repetidas.
- **No escribe nada** (regla 3 de la capa): la lectura va en el borde y el árbol queda byte a byte igual.
- **El código de salida es el del veredicto** y se enumera sobre ``VeredictoDelLote``: un veredicto nuevo rompe el test
  hasta que se decida su código. ``0`` es solo ``PASS``; el uso incorrecto NO se confunde con ``INCONSISTENTE``.
- **Anclas por AST**: el orden ``dictar -> auditar -> combinar`` queda congelado, y nadie fuera de la capa recompone la
  secuencia a mano (el censo de consumidores se congela por igualdad literal).

Límite declarado: esto NO cablea la capa a la GUI ni al agente LLM (decisión aparte, ver ``AGENTS.md`` de la capa).
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import struct
import subprocess
import sys
from collections.abc import Iterator, Sequence

import pytest

from sky_claw.local.parallax_unattended import (
    CODIGOS_DE_SALIDA,
    DecisionDelLote,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
    decidir_corrida,
)
from sky_claw.local.parallax_unattended import corrida as modulo_corrida

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent
_PAQUETE = _RAIZ_DEL_REPO / "sky_claw" / "local" / "parallax_unattended"
_RUTA = "textures/a_p.dds"


# ---------------------------------------------------------------------------
# Fixtures a mano (mismo formato que ``test_parallax_unattended_auditor.py``; no se importa de ahí para no acoplar
# dos archivos de test por sus helpers privados)
# ---------------------------------------------------------------------------


def _bloque(extremo_alto: int, extremo_bajo: int, indices: Sequence[int] | int = 0) -> bytes:
    patron = [indices] * 16 if isinstance(indices, int) else list(indices)
    bits = 0
    for posicion, indice in enumerate(patron):
        bits |= (indice & 0x7) << (3 * posicion)
    return bytes([extremo_alto, extremo_bajo]) + bits.to_bytes(6, "little")


def _dds_16x16(extremo_alto: int, extremo_bajo: int, indices: Sequence[int] | int) -> bytes:
    payload = _bloque(extremo_alto, extremo_bajo, indices) * 16
    cabecera = bytearray(128)
    cabecera[0:4] = b"DDS "
    struct.pack_into("<I", cabecera, 4, 124)
    struct.pack_into("<I", cabecera, 8, 0x1007)
    struct.pack_into("<I", cabecera, 12, 16)
    struct.pack_into("<I", cabecera, 16, 16)
    struct.pack_into("<I", cabecera, 20, len(payload))
    struct.pack_into("<I", cabecera, 24, 1)
    struct.pack_into("<I", cabecera, 28, 1)
    struct.pack_into("<I", cabecera, 76, 32)
    struct.pack_into("<I", cabecera, 80, 0x4)
    cabecera[84:88] = b"BC4U"
    struct.pack_into("<I", cabecera, 108, 0x401008)
    return bytes(cabecera) + payload


_RELIEVE = _dds_16x16(255, 0, [0, 1, 2, 3, 4, 5, 6, 7] * 2)
_PLANO = _dds_16x16(128, 128, 0)


def _informe_pass(ruta: str = _RUTA) -> dict[str, object]:
    """Un informe sin nada en duda: el veredicto del informe solo es ``PASS``."""

    return {
        "version_del_lote": "np-a0a.v1",
        "conteos": {"EN_REVISION": 0, "ESCRITA": 1, "NO_ESCRITA": 0, "NO_PROCESADA": 0, "SIN_CONFIRMAR": 0},
        "archivos": [{"ruta_de_salida": ruta, "estado": "ESCRITA", "efecto": "PUBLICADA", "veredicto": "AUTO_ACCEPT"}],
        "otras_rutas": [],
        "omitidas_al_enumerar": [],
        "interrupcion": None,
        "encoder": {"texconv_version": "test", "texconv_sha256": "a" * 64},
    }


class _Corrida:
    """Un informe y una raíz de salida en carpetas hermanas (el generador prohíbe el informe DENTRO de la salida)."""

    def __init__(self, base: pathlib.Path) -> None:
        self.raiz = base / "salida"
        self.informe = base / "informe_parallax.json"
        self.raiz.mkdir()

    def escribir_informe(self, informe: object) -> None:
        self.informe.write_text(json.dumps(informe), encoding="utf-8")

    def escribir_artefacto(self, ruta: str, datos: bytes) -> None:
        destino = self.raiz / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(datos)


@pytest.fixture
def corrida(tmp_path: pathlib.Path) -> _Corrida:
    return _Corrida(tmp_path)


def _arbol(base: pathlib.Path) -> dict[str, bytes | None]:
    """Foto del árbol: ruta relativa -> contenido (``None`` para carpetas). Detecta cualquier escritura."""

    return {
        str(ruta.relative_to(base)): (ruta.read_bytes() if ruta.is_file() else None) for ruta in sorted(base.rglob("*"))
    }


# ---------------------------------------------------------------------------
# La secuencia completa, de punta a punta
# ---------------------------------------------------------------------------


def test_un_lote_limpio_con_relieve_en_disco_es_pass_y_sale_0(corrida: _Corrida) -> None:
    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _RELIEVE)

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.PASS
    assert decision.motivos == ()
    assert codigo_de_salida(decision) == 0


def test_un_pass_del_informe_con_artefacto_plano_en_disco_se_degrada_a_review(corrida: _Corrida) -> None:
    """EL agujero que cierra el punto de entrada: sin ``combinar`` este lote saldría ``PASS``."""

    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _PLANO)

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.REVIEW
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE in decision.motivos
    assert _RUTA in decision.rutas
    assert codigo_de_salida(decision) != 0


def test_un_artefacto_declarado_y_ausente_en_disco_no_es_pass(corrida: _Corrida) -> None:
    # Arrange: el informe dice ESCRITA/PUBLICADA, pero el archivo no está en la raíz de salida
    corrida.escribir_informe(_informe_pass())

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is not VeredictoDelLote.PASS
    assert codigo_de_salida(decision) != 0


def test_un_informe_que_ya_dice_fail_no_se_rescata_porque_el_disco_se_vea_bien(corrida: _Corrida) -> None:
    # Arrange
    informe = _informe_pass()
    informe["archivos"] = [
        {"ruta_de_salida": _RUTA, "estado": "NO_ESCRITA", "efecto": "FALLIDA", "veredicto": "AUTO_ACCEPT"}
    ]
    informe["conteos"] = {"EN_REVISION": 0, "ESCRITA": 0, "NO_ESCRITA": 1, "NO_PROCESADA": 0, "SIN_CONFIRMAR": 0}
    corrida.escribir_informe(informe)
    corrida.escribir_artefacto(_RUTA, _RELIEVE)

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.FAIL
    assert codigo_de_salida(decision) != 0


def test_decidir_corrida_no_escribe_nada(corrida: _Corrida, tmp_path: pathlib.Path) -> None:
    """Regla 3 de la capa: solo lectura y un valor de retorno. El árbol queda idéntico, byte a byte."""

    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _PLANO)
    antes = _arbol(tmp_path)

    # Act
    decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert _arbol(tmp_path) == antes


# ---------------------------------------------------------------------------
# Un informe ilegible es INCONSISTENTE (fail-closed): nunca excepción, nunca PASS
# ---------------------------------------------------------------------------

_CASOS_ILEGIBLES = [
    pytest.param(None, id="ausente"),
    pytest.param(b"", id="vacio"),
    pytest.param(b"\xff\xfe\x00no es utf-8", id="no_utf8"),
    pytest.param(b"{no es json", id="json_roto"),
    pytest.param(b"[]", id="no_es_un_objeto_lista"),
    pytest.param(b'"texto"', id="no_es_un_objeto_texto"),
    pytest.param(b"null", id="no_es_un_objeto_null"),
    pytest.param(b"[" * 100_000, id="anidamiento_absurdo"),
]


@pytest.mark.parametrize("contenido", _CASOS_ILEGIBLES)
def test_un_informe_ilegible_es_inconsistente_con_forma_invalida(corrida: _Corrida, contenido: bytes | None) -> None:
    # Arrange
    if contenido is not None:
        corrida.informe.write_bytes(contenido)

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)
    assert decision.detalle != ""
    assert codigo_de_salida(decision) == CODIGOS_DE_SALIDA[VeredictoDelLote.INCONSISTENTE]


_ANOMALIAS_QUE_JSON_TOLERA = [
    pytest.param(
        b'{"interrupcion": "corte de disco", %CUERPO_SIN_LLAVE%',
        id="clave_repetida_la_ultima_ganaria",
    ),
    pytest.param(b'%CUERPO_SIN_LLAVE_FINAL%, "metrica": NaN}', id="constante_nan"),
    pytest.param(b'%CUERPO_SIN_LLAVE_FINAL%, "metrica": Infinity}', id="constante_infinity"),
    pytest.param(b'%CUERPO_SIN_LLAVE_FINAL%, "metrica": -Infinity}', id="constante_menos_infinity"),
    pytest.param(b"\xef\xbb\xbf%CUERPO%", id="con_bom"),
]


@pytest.mark.parametrize("plantilla", _ANOMALIAS_QUE_JSON_TOLERA)
def test_una_anomalia_que_json_tolera_pero_el_generador_no_emite_es_inconsistente(
    corrida: _Corrida, plantilla: bytes
) -> None:
    """Claves repetidas, ``NaN``/``Infinity`` y BOM: ``json`` los acepta; esta capa NO (fail-closed, regla 5)."""

    # Arrange: sobre un lote que sin la anomalía sería PASS, con relieve real en disco
    cuerpo = json.dumps(_informe_pass()).encode("utf-8")
    contenido = (
        plantilla.replace(b"%CUERPO_SIN_LLAVE_FINAL%", cuerpo[:-1])
        .replace(b"%CUERPO_SIN_LLAVE%", cuerpo[1:])
        .replace(b"%CUERPO%", cuerpo)
    )
    corrida.informe.write_bytes(contenido)
    corrida.escribir_artefacto(_RUTA, _RELIEVE)

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE
    assert decision.motivos == (MotivoDelLote.FORMA_INVALIDA,)
    assert codigo_de_salida(decision) != 0


def test_el_mismo_lote_sin_la_anomalia_es_pass(corrida: _Corrida) -> None:
    """Contracara: sin ella el lote del caso anterior SÍ es ``PASS``, así que lo que lo tumba es la anomalía."""

    corrida.informe.write_bytes(json.dumps(_informe_pass()).encode("utf-8"))
    corrida.escribir_artefacto(_RUTA, _RELIEVE)

    decision = decidir_corrida(corrida.informe, corrida.raiz)

    assert decision.veredicto is VeredictoDelLote.PASS


def test_un_directorio_en_lugar_del_informe_es_inconsistente(corrida: _Corrida) -> None:
    # Arrange
    corrida.informe.mkdir()

    # Act
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    # Assert
    assert decision.veredicto is VeredictoDelLote.INCONSISTENTE


def test_el_detalle_de_un_informe_ausente_nombra_la_ruta(corrida: _Corrida) -> None:
    decision = decidir_corrida(corrida.informe, corrida.raiz)

    assert str(corrida.informe) in decision.detalle


def test_un_informe_ilegible_tampoco_escribe_nada(corrida: _Corrida, tmp_path: pathlib.Path) -> None:
    corrida.informe.write_bytes(b"{no es json")
    antes = _arbol(tmp_path)

    decidir_corrida(corrida.informe, corrida.raiz)

    assert _arbol(tmp_path) == antes


# ---------------------------------------------------------------------------
# La línea de comandos
# ---------------------------------------------------------------------------


def _correr(argv: Sequence[str]) -> tuple[int, str, str]:
    salida, error = io.StringIO(), io.StringIO()
    codigo = modulo_corrida.main(argv, salida=salida, error=error)
    return codigo, salida.getvalue(), error.getvalue()


def test_main_de_un_lote_limpio_imprime_pass_y_devuelve_0(corrida: _Corrida) -> None:
    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _RELIEVE)

    # Act
    codigo, salida, error = _correr([str(corrida.informe), str(corrida.raiz)])

    # Assert
    assert codigo == 0
    assert salida.splitlines()[0] == "veredicto: pass"
    assert error == ""


def test_main_de_un_lote_en_revision_imprime_motivos_y_rutas(corrida: _Corrida) -> None:
    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _PLANO)

    # Act
    codigo, salida, _ = _correr([str(corrida.informe), str(corrida.raiz)])

    # Assert
    assert codigo == CODIGOS_DE_SALIDA[VeredictoDelLote.REVIEW]
    assert salida.splitlines()[0] == "veredicto: review"
    assert MotivoDelLote.ARTEFACTO_NO_CONFIABLE.value in salida
    assert _RUTA in salida


@pytest.mark.parametrize("veredicto", list(VeredictoDelLote))
def test_main_sale_con_el_codigo_de_su_veredicto_y_solo_pass_sale_0(
    monkeypatch: pytest.MonkeyPatch, veredicto: VeredictoDelLote
) -> None:
    """Enumera TODOS los veredictos: uno nuevo sin código en ``CODIGOS_DE_SALIDA`` rompe acá, no en producción."""

    # Arrange
    decision = DecisionDelLote(veredicto=veredicto, motivos=(), rutas=(), conteos={})
    monkeypatch.setattr(modulo_corrida, "decidir_corrida", lambda _informe, _raiz: decision)

    # Act
    codigo, salida, _ = _correr(["informe.json", "salida"])

    # Assert
    assert codigo == CODIGOS_DE_SALIDA[veredicto]
    assert (codigo == 0) is (veredicto is VeredictoDelLote.PASS)
    assert salida.splitlines()[0] == f"veredicto: {veredicto.value}"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="sin_argumentos"),
        pytest.param(["solo_el_informe.json"], id="falta_la_raiz"),
        pytest.param(["a.json", "salida", "sobra"], id="argumento_de_mas"),
        pytest.param(["a.json", "salida", "--no-existe"], id="opcion_desconocida"),
    ],
)
def test_main_con_uso_incorrecto_no_se_confunde_con_ningun_veredicto(argv: list[str]) -> None:
    """argparse sale con 2 por defecto, que es ``INCONSISTENTE``: quien llama no podría distinguir un error suyo."""

    # Act
    codigo, salida, error = _correr(argv)

    # Assert
    assert codigo == modulo_corrida.CODIGO_DE_USO_INCORRECTO
    assert codigo not in set(CODIGOS_DE_SALIDA.values())
    assert salida == ""
    assert "uso:" in error.lower() or "usage:" in error.lower()


def test_main_escapa_caracteres_de_control_de_un_informe_no_confiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rutas y detalle vienen de un archivo no confiable: una secuencia ANSI no debe llegar a la terminal del operador."""

    # Arrange
    decision = DecisionDelLote(
        veredicto=VeredictoDelLote.FAIL,
        motivos=(MotivoDelLote.ARCHIVO_NO_ESCRITO,),
        rutas=("textures/\x1b[2Jborrada_p.dds",),
        conteos={},
        detalle="linea1\nveredicto: pass",
    )
    monkeypatch.setattr(modulo_corrida, "decidir_corrida", lambda _informe, _raiz: decision)

    # Act
    _, salida, _ = _correr(["informe.json", "salida"])

    # Assert
    assert "\x1b" not in salida
    assert "veredicto: pass" not in salida.splitlines()
    assert salida.splitlines()[0] == "veredicto: fail"


def test_main_con_una_salida_que_no_codifica_el_texto_conserva_el_codigo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ``UnicodeEncodeError`` al imprimir (consola cp1252/ascii) no puede convertir el veredicto en un traceback."""

    # Arrange
    decision = DecisionDelLote(
        veredicto=VeredictoDelLote.REVIEW,
        motivos=(MotivoDelLote.ARCHIVO_EN_REVISION,),
        rutas=("textures/ñandú_p.dds",),
        conteos={},
    )
    monkeypatch.setattr(modulo_corrida, "decidir_corrida", lambda _informe, _raiz: decision)
    bytes_de_salida = io.BytesIO()
    salida = io.TextIOWrapper(bytes_de_salida, encoding="ascii", errors="strict")

    # Act
    codigo = modulo_corrida.main(["informe.json", "salida"], salida=salida, error=io.StringIO())
    salida.flush()

    # Assert
    assert codigo == CODIGOS_DE_SALIDA[VeredictoDelLote.REVIEW]
    assert b"veredicto: review" in bytes_de_salida.getvalue()


def test_python_dash_m_corre_la_secuencia_y_sale_con_el_codigo_del_veredicto(
    corrida: _Corrida, tmp_path: pathlib.Path
) -> None:
    """El segundo entregable de #802: ``python -m sky_claw.local.parallax_unattended <informe> <raiz>``."""

    # Arrange
    corrida.escribir_informe(_informe_pass())
    corrida.escribir_artefacto(_RUTA, _PLANO)

    # Act
    resultado = subprocess.run(  # noqa: S603 - argv fijo y rutas de tmp_path, sin shell
        [sys.executable, "-m", "sky_claw.local.parallax_unattended", str(corrida.informe), str(corrida.raiz)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )

    # Assert
    assert resultado.returncode == CODIGOS_DE_SALIDA[VeredictoDelLote.REVIEW], resultado.stderr
    assert resultado.stdout.splitlines()[0] == "veredicto: review"


# ---------------------------------------------------------------------------
# Anclas por AST: enumeran, no muestrean
# ---------------------------------------------------------------------------


def _llamadas_en_orden(funcion: ast.FunctionDef) -> list[str]:
    nombres: list[tuple[int, int, str]] = []
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                nombres.append((nodo.lineno, nodo.col_offset, nodo.func.id))
            elif isinstance(nodo.func, ast.Attribute):
                nombres.append((nodo.lineno, nodo.col_offset, nodo.func.attr))
    return [nombre for _, _, nombre in sorted(nombres)]


def _funcion(nombre: str) -> ast.FunctionDef:
    arbol = ast.parse((_PAQUETE / "corrida.py").read_text(encoding="utf-8"))
    for nodo in arbol.body:
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"corrida.py no define {nombre}")


def test_decidir_corrida_compone_dictar_auditar_combinar_una_vez_y_en_ese_orden() -> None:
    """El orden no es intercambiable: saltearse ``combinar`` deja un lote PASS con artefactos planos."""

    llamadas = [n for n in _llamadas_en_orden(_funcion("decidir_corrida")) if n in {"dictar", "auditar", "combinar"}]

    assert llamadas == ["dictar", "auditar", "combinar"]


def test_main_traduce_el_veredicto_solo_con_codigo_de_salida() -> None:
    llamadas = _llamadas_en_orden(_funcion("main"))

    assert "decidir_corrida" in llamadas
    assert "codigo_de_salida" in llamadas
    assert llamadas.index("decidir_corrida") < llamadas.index("codigo_de_salida")


def _modulos_que_importan_la_capa() -> Iterator[tuple[str, set[str]]]:
    """(ruta relativa, nombres importados de la capa) de cada módulo de ``sky_claw/`` FUERA de la capa."""

    for ruta in sorted((_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")):
        if _PAQUETE in ruta.parents:
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        nombres: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and "parallax_unattended" in (nodo.module or ""):
                nombres.update(alias.name for alias in nodo.names)
            elif isinstance(nodo, ast.ImportFrom):
                nombres.update(alias.name for alias in nodo.names if alias.name == "parallax_unattended")
            elif isinstance(nodo, ast.Import):
                nombres.update(alias.name for alias in nodo.names if "parallax_unattended" in alias.name)
        if nombres:
            yield str(ruta.relative_to(_RAIZ_DEL_REPO)).replace("\\", "/"), nombres


def test_hoy_ningun_modulo_fuera_de_la_capa_la_consume_y_el_censo_esta_congelado() -> None:
    """Agregar una superficie (GUI, agente LLM) rompe esto a propósito: hay que enumerarla Y cablearla igual que a la otra."""

    consumidores = {ruta for ruta, _ in _modulos_que_importan_la_capa()}

    assert consumidores == set()


def test_nadie_fuera_de_la_capa_recompone_la_secuencia_a_mano() -> None:
    """La propiedad del mecanismo: la única forma de decidir una corrida es ``decidir_corrida``."""

    recompuestos = {
        ruta: sorted(nombres & {"dictar", "auditar", "combinar"})
        for ruta, nombres in _modulos_que_importan_la_capa()
        if nombres & {"dictar", "auditar", "combinar"}
    }

    assert recompuestos == {}
