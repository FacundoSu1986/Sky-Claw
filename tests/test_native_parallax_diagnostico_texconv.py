"""NP-A0c: diagnóstico previo del ``texconv.exe`` configurado (ADR 0014).

Qué ancla, enumerando y no muestreando:

- el vocabulario de ``EstadoDeTexconv`` es EXACTAMENTE el congelado y CADA estado tiene un escenario con mensaje no
  vacío: agregar un estado sin caso rompe este archivo;
- los cuatro fallos que ``export.verificar_texconv`` mete en un mismo tipo (no existe, no es un archivo, no se pudo
  leer, huella distinta) se distinguen acá, más el quinto (no hay versión fijada);
- la huella NO se recalcula acá: el módulo delega en ``export.verificar_texconv`` y no importa ``hashlib``, ni lanza
  procesos, ni usa la red (por AST). El censo de ``tests/test_texconv_invariant.py`` lo tiene registrado;
- el argumento gana sobre la variable de entorno (igual que ``cli._resolver_texconv``), solo un valor VACÍO cuenta como
  no configurado, y el nombre de la variable es el mismo que lee la CLI. Una tabla ENUMERA entradas y exige que el
  diagnóstico y la CLI elijan la misma ruta: un pre-chequeo que normaliza lo que el exportador no normaliza declararía
  CORRECTA una configuración que el exportador rechaza;
- un ``OSError`` inesperado al mirar el archivo (p. ej. ``ELOOP``) llega al mensaje con el texto del sistema.

Límite: nada de esto ejecuta ``texconv``. Que el binario fijado exporte de verdad se mide en Windows con
``tests/test_texconv_real.py``; el diagnóstico solo mira el archivo y delega la huella.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from sky_claw.local.native_parallax import cli, diagnostico_texconv, export
from sky_claw.local.native_parallax.diagnostico_texconv import (
    ENTORNO_TEXCONV,
    DiagnosticoDeTexconv,
    EstadoDeTexconv,
    diagnosticar_texconv,
)
from sky_claw.local.native_parallax.export import TexconvNoCoincideError, TexconvPin

RAIZ = Path(__file__).resolve().parents[1]
MODULO = "sky_claw/local/native_parallax/diagnostico_texconv.py"
ORIGEN = "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
VERSION = "may2026"
CONTENIDO = b"MZ esto no es el texconv fijado: es un archivo de prueba\n"
SHA_DEL_ARCHIVO_DE_PRUEBA = hashlib.sha256(CONTENIDO).hexdigest()
SHA_DE_OTRO_BINARIO = "0" * 64

#: Lo único que el módulo puede importar. Una dependencia nueva (``hashlib``, ``subprocess``, ``socket``, ``numpy``,
#: ``PIL``) rompe este ancla hasta que se decida: el diagnóstico no calcula huellas ni ejecuta nada.
IMPORTS_PERMITIDOS = {"__future__", "collections", "dataclasses", "enum", "os", "pathlib", "stat", "typing", "sky_claw"}
LLAMADAS_DE_PROCESO = {
    "Popen",
    "call",
    "check_call",
    "check_output",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "popen",
    "run",
    "run_capture",
    "spawn_detached",
    "system",
}


def _pin(sha256: str = SHA_DEL_ARCHIVO_DE_PRUEBA, version: str = VERSION) -> TexconvPin:
    return TexconvPin(version=version, sha256=sha256, origen=ORIGEN)


def _archivo(tmp_path: Path, nombre: str = "texconv.exe") -> Path:
    ruta = tmp_path / nombre
    ruta.write_bytes(CONTENIDO)
    return ruta


def _delegado_que_no_puede_leer(ruta: object, *, pin: object = None) -> object:
    """El contrato de ``export.verificar_texconv`` cuando el sistema operativo no deja leer: ``raise ... from OSError``."""
    raise TexconvNoCoincideError(f"no se pudo leer texconv: {ruta}") from OSError("acceso denegado")


def _un_diagnostico_por_estado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[EstadoDeTexconv, DiagnosticoDeTexconv]:
    """Un diagnóstico por CADA estado del vocabulario: es el ancla de que vocabulario y mensajes están cubiertos."""
    bueno = _archivo(tmp_path)
    carpeta = tmp_path / "DirectXTex"
    carpeta.mkdir()
    ausente = tmp_path / "no-existe" / "texconv.exe"
    with monkeypatch.context() as contexto:
        contexto.setattr(export, "verificar_texconv", _delegado_que_no_puede_leer)
        sin_lectura = diagnosticar_texconv(bueno, entorno={}, pin=_pin())
    with monkeypatch.context() as contexto:
        contexto.setattr(export, "TEXCONV_PIN_VIGENTE", None)
        sin_version = diagnosticar_texconv(bueno, entorno={})
    return {
        EstadoDeTexconv.CORRECTO: diagnosticar_texconv(bueno, entorno={}, pin=_pin()),
        EstadoDeTexconv.NO_CONFIGURADO: diagnosticar_texconv(None, entorno={}, pin=_pin()),
        EstadoDeTexconv.NO_ENCONTRADO: diagnosticar_texconv(ausente, entorno={}, pin=_pin()),
        EstadoDeTexconv.NO_ES_UN_ARCHIVO: diagnosticar_texconv(carpeta, entorno={}, pin=_pin()),
        EstadoDeTexconv.NO_SE_PUDO_LEER: sin_lectura,
        EstadoDeTexconv.HUELLA_DISTINTA: diagnosticar_texconv(bueno, entorno={}, pin=_pin(SHA_DE_OTRO_BINARIO)),
        EstadoDeTexconv.SIN_VERSION_FIJADA: sin_version,
    }


# ---------------------------------------------------------------------------
# Vocabulario cerrado y contrato del dato
# ---------------------------------------------------------------------------


def test_el_vocabulario_de_estados_es_exactamente_el_congelado() -> None:
    assert {estado.name for estado in EstadoDeTexconv} == {
        "CORRECTO",
        "NO_CONFIGURADO",
        "NO_ENCONTRADO",
        "NO_ES_UN_ARCHIVO",
        "NO_SE_PUDO_LEER",
        "HUELLA_DISTINTA",
        "SIN_VERSION_FIJADA",
    }


def test_la_superficie_publica_es_exactamente_la_congelada() -> None:
    assert set(diagnostico_texconv.__all__) == {
        "ENTORNO_TEXCONV",
        "DiagnosticoDeTexconv",
        "EstadoDeTexconv",
        "OrigenDeLaRuta",
        "diagnosticar_texconv",
    }


def test_cada_estado_del_vocabulario_tiene_escenario_y_mensaje_no_vacio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnosticos = _un_diagnostico_por_estado(tmp_path, monkeypatch)

    assert set(diagnosticos) == set(EstadoDeTexconv)
    for estado, diagnostico in diagnosticos.items():
        assert diagnostico.estado is estado
        assert diagnostico.mensaje.strip(), f"{estado.name} sin mensaje"


def test_la_version_esperada_es_la_del_pin_en_todos_los_estados_menos_sin_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for estado, diagnostico in _un_diagnostico_por_estado(tmp_path, monkeypatch).items():
        esperada = None if estado is EstadoDeTexconv.SIN_VERSION_FIJADA else VERSION

        assert diagnostico.version_esperada == esperada, estado.name


def test_no_configurado_no_tiene_ruta_ni_origen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diagnostico = _un_diagnostico_por_estado(tmp_path, monkeypatch)[EstadoDeTexconv.NO_CONFIGURADO]

    assert diagnostico.ruta is None
    assert diagnostico.origen_de_la_ruta is None


@pytest.mark.parametrize(
    "estado",
    [estado for estado in EstadoDeTexconv if estado is not EstadoDeTexconv.NO_CONFIGURADO],
)
def test_todo_estado_configurado_devuelve_la_ruta_y_de_donde_salio(
    estado: EstadoDeTexconv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnostico = _un_diagnostico_por_estado(tmp_path, monkeypatch)[estado]

    assert diagnostico.ruta is not None
    assert diagnostico.origen_de_la_ruta in {"argumento", "entorno"}


def test_el_diagnostico_es_inmutable(tmp_path: Path) -> None:
    diagnostico = diagnosticar_texconv(_archivo(tmp_path), entorno={}, pin=_pin())

    with pytest.raises(dataclasses.FrozenInstanceError):
        diagnostico.estado = EstadoDeTexconv.NO_CONFIGURADO


def test_un_diagnostico_con_mensaje_en_blanco_no_se_puede_construir() -> None:
    with pytest.raises(ValueError):
        DiagnosticoDeTexconv(
            estado=EstadoDeTexconv.CORRECTO,
            ruta="C:/tools/texconv.exe",
            origen_de_la_ruta="argumento",
            version_esperada=VERSION,
            mensaje="   ",
        )


def test_una_ruta_sin_origen_no_se_puede_construir() -> None:
    with pytest.raises(ValueError):
        DiagnosticoDeTexconv(
            estado=EstadoDeTexconv.NO_ENCONTRADO,
            ruta="C:/tools/texconv.exe",
            origen_de_la_ruta=None,
            version_esperada=VERSION,
            mensaje="no existe",
        )


# ---------------------------------------------------------------------------
# De dónde sale la ruta
# ---------------------------------------------------------------------------


def test_sin_argumento_ni_variable_el_estado_es_no_configurado() -> None:
    diagnostico = diagnosticar_texconv(None, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_CONFIGURADO
    assert diagnostico.ruta is None
    assert diagnostico.origen_de_la_ruta is None


def test_el_mensaje_de_no_configurado_dice_las_dos_formas_de_configurarlo() -> None:
    mensaje = diagnosticar_texconv(None, entorno={}, pin=_pin()).mensaje

    assert "--texconv" in mensaje
    assert ENTORNO_TEXCONV in mensaje


@pytest.mark.parametrize(
    ("ruta", "entorno"),
    [
        (None, {}),
        ("", {}),
        (None, {ENTORNO_TEXCONV: ""}),
        ("", {ENTORNO_TEXCONV: ""}),
    ],
)
def test_solo_un_valor_vacio_cuenta_como_no_configurado(ruta: str | None, entorno: dict[str, str]) -> None:
    diagnostico = diagnosticar_texconv(ruta, entorno=entorno, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_CONFIGURADO
    assert diagnostico.ruta is None
    assert diagnostico.origen_de_la_ruta is None


#: Entradas que la CLI y el diagnóstico tienen que resolver IGUAL. ``None`` en la salida de la CLI es «falta el texconv».
#: Incluye los blancos: el pre-chequeo no puede normalizar (recortar) lo que ``cli._resolver_texconv`` pasa tal cual.
_ENTRADAS_DE_RESOLUCION = [
    (None, {}),
    ("", {}),
    ("   ", {}),
    ("\t", {}),
    ("  C:/tools/texconv.exe", {}),
    ("C:/tools/texconv.exe  ", {}),
    ("C:/tools/texconv.exe", {}),
    (None, {ENTORNO_TEXCONV: ""}),
    (None, {ENTORNO_TEXCONV: "   "}),
    (None, {ENTORNO_TEXCONV: "  C:/tools/texconv.exe"}),
    (None, {ENTORNO_TEXCONV: "C:/tools/texconv.exe"}),
    ("", {ENTORNO_TEXCONV: "C:/tools/texconv.exe"}),
    ("   ", {ENTORNO_TEXCONV: "C:/tools/texconv.exe"}),
    ("A/texconv.exe", {ENTORNO_TEXCONV: "B/texconv.exe"}),
    ("  ", {ENTORNO_TEXCONV: "  "}),
]


@pytest.mark.parametrize(("argumento", "entorno"), _ENTRADAS_DE_RESOLUCION)
def test_el_diagnostico_y_la_cli_eligen_la_misma_ruta(argumento: str | None, entorno: dict[str, str]) -> None:
    """ENUMERA las entradas: dos resoluciones distintas serían dos verdades (la CLI rechazaría lo que el diagnóstico dio
    por bueno)."""
    try:
        de_la_cli: str | None = str(cli._resolver_texconv(argumento, entorno))
    except cli.UsoIncorrectoError:
        de_la_cli = None

    diagnostico = diagnosticar_texconv(argumento, entorno=entorno, pin=_pin())

    assert diagnostico.ruta == de_la_cli
    assert (diagnostico.estado is EstadoDeTexconv.NO_CONFIGURADO) == (de_la_cli is None)


def test_una_ruta_con_espacios_al_principio_no_se_da_por_correcta(tmp_path: Path) -> None:
    """La CLI le pasa al exportador la ruta SIN recortar: si el diagnóstico la recortara diría «correcto» para una
    configuración que el exportador rechaza, que es justo lo que un pre-chequeo no puede hacer."""
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv(f"  {archivo}", entorno={}, pin=_pin())

    assert diagnostico.estado is not EstadoDeTexconv.CORRECTO
    assert diagnostico.ruta == f"  {archivo}"


def test_el_argumento_gana_sobre_la_variable_de_entorno(tmp_path: Path) -> None:
    archivo = _archivo(tmp_path)
    otro = tmp_path / "otra-carpeta" / "texconv.exe"

    diagnostico = diagnosticar_texconv(archivo, entorno={ENTORNO_TEXCONV: str(otro)}, pin=_pin(SHA_DE_OTRO_BINARIO))

    assert diagnostico.ruta == str(archivo)
    assert diagnostico.origen_de_la_ruta == "argumento"
    assert diagnostico.estado is EstadoDeTexconv.HUELLA_DISTINTA


def test_la_variable_de_entorno_se_usa_cuando_no_hay_argumento(tmp_path: Path) -> None:
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv(None, entorno={ENTORNO_TEXCONV: str(archivo)}, pin=_pin())

    assert diagnostico.ruta == str(archivo)
    assert diagnostico.origen_de_la_ruta == "entorno"
    assert diagnostico.estado is EstadoDeTexconv.CORRECTO


def test_un_argumento_vacio_deja_paso_a_la_variable_de_entorno(tmp_path: Path) -> None:
    """Igual que ``cli._resolver_texconv`` (``argumento or entorno.get(...)``): solo el vacío no es una ruta."""
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv("", entorno={ENTORNO_TEXCONV: str(archivo)}, pin=_pin())

    assert diagnostico.ruta == str(archivo)
    assert diagnostico.origen_de_la_ruta == "entorno"


def test_un_argumento_de_solo_espacios_se_usa_y_no_deja_paso_a_la_variable(tmp_path: Path) -> None:
    """Es truthy: la CLI lo usaría como ruta (y fallaría). El diagnóstico dice lo mismo en vez de «arreglarlo»."""
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv("   ", entorno={ENTORNO_TEXCONV: str(archivo)}, pin=_pin())

    assert diagnostico.origen_de_la_ruta == "argumento"
    assert diagnostico.estado is not EstadoDeTexconv.CORRECTO


def test_otras_variables_del_entorno_no_se_confunden_con_la_del_texconv(tmp_path: Path) -> None:
    archivo = _archivo(tmp_path)
    entorno = {"PATH": "x", f"{ENTORNO_TEXCONV}_OTRA": "y", "SKYCLAW_TEXCONV": "z", ENTORNO_TEXCONV: str(archivo)}

    diagnostico = diagnosticar_texconv(None, entorno=entorno, pin=_pin())

    assert diagnostico.ruta == str(archivo)


def test_la_variable_de_entorno_es_la_misma_que_lee_la_cli() -> None:
    """El diagnóstico explica la variable que la CLI lee: dos nombres distintos serían dos verdades."""
    assert ENTORNO_TEXCONV == cli.ENTORNO_TEXCONV


# ---------------------------------------------------------------------------
# El pin: explícito, vigente o ninguno
# ---------------------------------------------------------------------------


def test_correcto_cuando_la_huella_es_la_del_pin(tmp_path: Path) -> None:
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv(archivo, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.CORRECTO
    assert diagnostico.version_esperada == VERSION
    assert str(archivo) in diagnostico.mensaje
    assert VERSION in diagnostico.mensaje


def test_el_pin_explicito_gana_sobre_el_vigente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", _pin(SHA_DE_OTRO_BINARIO, version="otra-version"))

    diagnostico = diagnosticar_texconv(_archivo(tmp_path), entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.CORRECTO
    assert diagnostico.version_esperada == VERSION


def test_usa_el_pin_vigente_cuando_no_se_pasa_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El vigente se lee en la llamada, no se copia al importar: si se fija otra versión, el diagnóstico la ve."""
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", _pin(version="vigente-de-prueba"))

    diagnostico = diagnosticar_texconv(_archivo(tmp_path), entorno={})

    assert diagnostico.estado is EstadoDeTexconv.CORRECTO
    assert diagnostico.version_esperada == "vigente-de-prueba"


def test_sin_version_fijada_lo_dice_y_no_inventa_una_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", None)

    diagnostico = diagnosticar_texconv(_archivo(tmp_path), entorno={})

    assert diagnostico.estado is EstadoDeTexconv.SIN_VERSION_FIJADA
    assert diagnostico.version_esperada is None
    assert "fijad" in diagnostico.mensaje.lower()
    assert VERSION not in diagnostico.mensaje


# ---------------------------------------------------------------------------
# Los cuatro fallos de ``TexconvNoCoincideError``, separados
# ---------------------------------------------------------------------------


def test_una_ruta_que_no_existe_es_no_encontrado(tmp_path: Path) -> None:
    ausente = tmp_path / "no-existe" / "texconv.exe"

    diagnostico = diagnosticar_texconv(ausente, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_ENCONTRADO
    assert str(ausente) in diagnostico.mensaje


def test_una_carpeta_es_no_es_un_archivo(tmp_path: Path) -> None:
    carpeta = tmp_path / "DirectXTex"
    carpeta.mkdir()

    diagnostico = diagnosticar_texconv(carpeta, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_ES_UN_ARCHIVO
    assert str(carpeta) in diagnostico.mensaje


def test_un_nombre_que_el_sistema_no_puede_interpretar_es_no_encontrado(tmp_path: Path) -> None:
    """Un nombre con un byte NUL no nombra ningún archivo: el diagnóstico no puede explotar con la ruta del operador."""
    diagnostico = diagnosticar_texconv(f"{tmp_path}/tex\0conv.exe", entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_ENCONTRADO


def test_un_oserror_al_leer_es_no_se_pudo_leer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No hay forma portable de fabricar un archivo ilegible (en Windows ``chmod`` no quita la lectura): se simula el
    contrato del delegado, que documenta ``__cause__`` como ``OSError``."""
    monkeypatch.setattr(export, "verificar_texconv", _delegado_que_no_puede_leer)
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv(archivo, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_SE_PUDO_LEER
    assert str(archivo) in diagnostico.mensaje
    assert "acceso denegado" in diagnostico.mensaje


def test_un_oserror_inesperado_al_mirar_el_archivo_llega_al_mensaje(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un bucle de enlaces (``ELOOP``) no lo ve ``verificar_texconv`` (``is_file`` lo toma por «no es un archivo» y no
    deja causa): el ``stat`` del diagnóstico es el único que lo ve, y su texto no puede perderse en «sin motivo».

    No se arma un bucle real: crear enlaces exige un privilegio que la CI de Windows y varias máquinas no tienen."""
    bucle = tmp_path / "bucle" / "texconv.exe"
    stat_original = Path.stat

    def stat_con_bucle(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self == bucle:
            raise OSError(errno.ELOOP, "Demasiados niveles de enlaces simbólicos", str(self))
        return stat_original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_con_bucle)

    diagnostico = diagnosticar_texconv(bucle, entorno={}, pin=_pin())

    assert diagnostico.estado is EstadoDeTexconv.NO_SE_PUDO_LEER
    assert "Demasiados niveles de enlaces simbólicos" in diagnostico.mensaje
    assert "no dio un motivo" not in diagnostico.mensaje


def test_huella_distinta_dice_la_version_y_la_huella_esperadas(tmp_path: Path) -> None:
    archivo = _archivo(tmp_path)

    diagnostico = diagnosticar_texconv(archivo, entorno={}, pin=_pin(SHA_DE_OTRO_BINARIO))

    assert diagnostico.estado is EstadoDeTexconv.HUELLA_DISTINTA
    assert str(archivo) in diagnostico.mensaje
    assert VERSION in diagnostico.mensaje
    assert SHA_DE_OTRO_BINARIO in diagnostico.mensaje


# ---------------------------------------------------------------------------
# Pureza: delega la huella y no ejecuta nada
# ---------------------------------------------------------------------------


def _arbol_del_modulo() -> ast.Module:
    return ast.parse((RAIZ / MODULO).read_text(encoding="utf-8"))


def _nombre_de_la_llamada(nodo: ast.Call) -> str | None:
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    return None


def test_el_modulo_solo_importa_la_stdlib_y_export() -> None:
    importados: set[str] = set()
    for nodo in ast.walk(_arbol_del_modulo()):
        if isinstance(nodo, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0:
            importados.add((nodo.module or "").split(".")[0])

    assert importados == IMPORTS_PERMITIDOS


def test_el_modulo_no_lanza_procesos() -> None:
    llamadas = {_nombre_de_la_llamada(nodo) for nodo in ast.walk(_arbol_del_modulo()) if isinstance(nodo, ast.Call)}

    assert llamadas & LLAMADAS_DE_PROCESO == set()


def test_el_modulo_delega_la_huella_en_export() -> None:
    llamadas = {
        (nodo.func.value.id, nodo.func.attr)
        for nodo in ast.walk(_arbol_del_modulo())
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and isinstance(nodo.func.value, ast.Name)
    }

    assert ("export", "verificar_texconv") in llamadas
