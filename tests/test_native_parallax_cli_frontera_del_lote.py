"""#834 ítem 2: la frontera del lote ante un fallo del candado o del disco durante la escritura.

Antes, un ``LockLeaseLostError`` (o cualquier ``OSError`` que el escritor no absorbe) escapaba de ``main`` como traceback: sin
informe y con lo publicado sin confirmar. Ahora el lote lo declara: cada textura que quedó a medias es ``SIN_CONFIRMAR``, el
informe trae el motivo en ``interrupcion`` y el código de salida no es 0.

Además el informe se escribe de forma atómica y, si no se pudo escribir, NINGUNO de los dos comandos que lo producen
(``generar`` y ``autoprueba``) sale con 0: sin informe el resultado de la corrida no es auditable.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
from typing import Any

import pytest

from sky_claw.app.db.locks import LockError, LockLeaseLostError
from sky_claw.local.native_parallax import cli, output_writer
from sky_claw.local.native_parallax.cli import CODIGO_CON_FALLOS, CODIGO_OK, ENTORNO_TEXCONV, main
from tests.test_native_parallax_cli import (  # noqa: F401  (``mundo`` es un fixture)
    _fabrica,
    _generar,
    _informe_de_muestra,
    mundo,
)

pytest.importorskip("numpy")


def _perder_el_candado_al_escribir(
    monkeypatch: pytest.MonkeyPatch, excepcion: BaseException, *, desde_la_llamada: int
) -> None:
    """Hace que ``_asegurar_lock`` de la sesión real lance *excepcion* a partir de la llamada *desde_la_llamada*."""
    real = output_writer.SesionDeSalida._asegurar_lock
    llamadas = {"n": 0}

    async def se_pierde(self: Any) -> None:
        llamadas["n"] += 1
        if llamadas["n"] >= desde_la_llamada:
            raise excepcion
        await real(self)

    monkeypatch.setattr(output_writer.SesionDeSalida, "_asegurar_lock", se_pierde)


@pytest.mark.parametrize(
    "excepcion",
    [LockLeaseLostError("lease perdida (simulada)"), LockError("candado (simulado)"), OSError("disco (simulado)")],
    ids=lambda e: type(e).__name__,
)
def test_un_corte_de_la_escritura_no_revienta_la_cli_y_deja_un_informe_que_lo_declara(
    mundo: pathlib.Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    excepcion: BaseException,
) -> None:
    _perder_el_candado_al_escribir(monkeypatch, excepcion, desde_la_llamada=3)

    codigo, texto = _generar(mundo, "--incluir-revision")

    assert codigo == CODIGO_CON_FALLOS
    assert "Traceback" not in texto and "ESCRITURA INTERRUMPIDA" in texto
    informe = json.loads((mundo / "informe.json").read_text(encoding="utf-8"))
    assert informe["interrupcion"].startswith(f"{type(excepcion).__name__}: ")
    assert informe["conteos"]["SIN_CONFIRMAR"] == 2
    assert {a["estado"] for a in informe["archivos"]} == {"SIN_CONFIRMAR"}


def test_un_informe_que_no_se_puede_reemplazar_no_toca_el_anterior_ni_deja_temporales(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destino = tmp_path / "informe.json"
    destino.write_text('{"anterior": true}\n', encoding="utf-8")

    def falla(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("el destino está abierto (simulado)")

    monkeypatch.setattr(cli.os, "replace", falla)
    salida = io.StringIO()

    escrito = cli._escribir_informe(_informe_de_muestra(), [], str(destino), salida)

    assert escrito is False
    assert json.loads(destino.read_text(encoding="utf-8")) == {"anterior": True}  # ni truncado ni a medias
    assert sorted(p.name for p in tmp_path.iterdir()) == ["informe.json"]
    assert salida.getvalue().startswith(f"No se pudo escribir el informe en {destino}:")


def test_un_informe_escrito_reemplaza_al_anterior_y_no_deja_temporales(tmp_path: pathlib.Path) -> None:
    destino = tmp_path / "informe.json"
    destino.write_text('{"anterior": true}\n', encoding="utf-8")

    escrito = cli._escribir_informe(_informe_de_muestra(), [], str(destino), io.StringIO())

    assert escrito is True
    assert "version_del_lote" in json.loads(destino.read_text(encoding="utf-8"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["informe.json"]


def _sin_poder_escribir_el_informe(monkeypatch: pytest.MonkeyPatch, informe: pathlib.Path) -> None:
    """Hace fallar SOLO el reemplazo atómico del informe. ``cli.os`` es el módulo ``os`` entero: parchear ``replace`` sin discriminar
    también rompería las escrituras atómicas del escritor de la salida y el lote saldría con 1 por la razón equivocada."""
    real = cli.os.replace

    def falla(origen: Any, destino: Any, *args: Any, **kwargs: Any) -> None:
        if pathlib.Path(destino) == informe:
            raise PermissionError("no se puede escribir el informe (simulado)")
        real(origen, destino, *args, **kwargs)

    monkeypatch.setattr(cli.os, "replace", falla)


def test_generar_no_sale_con_cero_si_el_informe_no_se_pudo_escribir(
    mundo: pathlib.Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _generar(mundo, "--incluir-revision")[0] == CODIGO_OK  # el control: sin el fallo, este mismo lote sale con 0
    _sin_poder_escribir_el_informe(monkeypatch, mundo / "informe.json")

    codigo, texto = _generar(mundo, "--incluir-revision")

    assert codigo == CODIGO_CON_FALLOS and "No se pudo escribir el informe" in texto
    # El lote en sí salió bien: la causa del 1 es SOLO el informe (no una escritura rota ni una interrupción).
    assert "ESCRITURA INTERRUMPIDA" not in texto and "2 escrita" in texto


def test_la_autoprueba_no_sale_con_cero_si_el_informe_no_se_pudo_escribir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def correr() -> tuple[int, str]:
        salida = io.StringIO()
        codigo = main(
            ["autoprueba", "--informe", str(tmp_path / "i.json")],
            entorno={ENTORNO_TEXCONV: "C:/texconv.exe"},
            fabrica_de_exportador=_fabrica(),
            base_de_estado=tmp_path / "estado",
            salida=salida,
        )
        return codigo, salida.getvalue()

    assert correr()[0] == CODIGO_OK
    _sin_poder_escribir_el_informe(monkeypatch, tmp_path / "i.json")

    codigo, texto = correr()

    assert codigo == CODIGO_CON_FALLOS and "el informe no se pudo escribir" in texto
    assert (
        "AUTOPRUEBA FALLÓ" in texto and "la textura sintética no quedó escrita" not in texto
    )  # el lote en sí estuvo bien


def test_ningun_comando_descarta_si_el_informe_quedo_escrito() -> None:
    """Ancla por AST: ``_informar`` y ``_escribir_informe`` devuelven si el informe quedó; una llamada que tira ese valor (un comando
    nuevo, o uno de los dos de hoy) volvería a salir con 0 sin informe. Se enumeran TODAS las llamadas, no las dos conocidas."""
    arbol = ast.parse(pathlib.Path(cli.__file__).read_text(encoding="utf-8"))
    nombres = {"_informar", "_escribir_informe"}
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id in nombres
    ]
    descartadas = [
        nodo.lineno
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Expr)
        and isinstance(nodo.value, ast.Call)
        and isinstance(nodo.value.func, ast.Name)
        and nodo.value.func.id in nombres
    ]
    assert len(llamadas) == 3  # _generar y _autoprueba llaman a _informar; _informar llama a _escribir_informe
    assert descartadas == [], f"se descarta el resultado de _informar/_escribir_informe en las líneas {descartadas}"
