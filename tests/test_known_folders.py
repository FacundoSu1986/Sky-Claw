"""Known Folders de Windows — primitiva de identidad de carpetas de usuario.

Por qué existe: la admisión de `external_work_root` (ADR 0011 §2.7) prohíbe
`Documents`, `Desktop` y `Downloads`, y el ADR congela CÓMO se resuelven: por
**identificador** con la API de Known Folders, tomando la ruta efectiva vigente.
Las dos formas fáciles están prohibidas por escrito y las dos fallan en el mismo
escenario real —una carpeta redirigida—:

* `%USERPROFILE%\\Documents` no refleja la redirección (el usuario mueve
  `Documents` a `D:\\Users\\...\\Docs` y la env var sigue apuntando al perfil);
* buscar el substring `"Documents"` / `"OneDrive"` en la ruta confunde identidad
  con nombre: `E:\\Mis Documentos de Trabajo` no es la Known Folder, y
  `D:\\Users\\x\\Docs` sí lo es.

Estos tests enumeran el conjunto cerrado v1 en vez de muestrearlo, y anclan por
código fuente que las dos formas prohibidas no reaparezcan.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.app.security import known_folders

MODULO = pathlib.Path(known_folders.__file__)


def test_el_conjunto_prohibido_v1_es_exactamente_el_del_adr() -> None:
    """Igualdad literal: agregar una carpeta exige enmienda del ADR 0011 §2.7.

    El conjunto es CERRADO para v1. Un `KNOWN_FOLDERS_PROHIBIDOS` que crezca sin
    su justificación de riesgo rompe acá primero.
    """
    assert known_folders.KNOWN_FOLDERS_PROHIBIDOS == ("Documents", "Desktop", "Downloads")


@pytest.mark.parametrize("nombre", ["Documents", "Desktop", "Downloads"])
def test_cada_carpeta_prohibida_tiene_su_identificador_oficial(nombre: str) -> None:
    """Cada nombre del conjunto resuelve por GUID, no por convención de ruta."""
    guid = known_folders.IDENTIFICADORES[nombre]
    assert guid.startswith("{") and guid.endswith("}")
    assert len(guid) == 38  # {8-4-4-4-12}


@pytest.mark.parametrize(
    ("nombre", "efectiva"),
    [
        ("Documents", r"C:\Users\facha\Documents"),
        ("Desktop", r"C:\Users\facha\Desktop"),
        ("Downloads", r"C:\Users\facha\Downloads"),
        # El caso que mata a `%USERPROFILE%` y al substring: redirigida a OTRO
        # volumen y con OTRO nombre.
        ("Documents", r"D:\Users\facha\Docs"),
        # Y la redirección a la carpeta de sincronización.
        ("Documents", r"C:\Users\facha\OneDrive\Documentos"),
    ],
)
def test_resolver_devuelve_la_ruta_efectiva_que_reporta_la_api(
    nombre: str, efectiva: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La primitiva devuelve lo que dice la API para ese identificador."""
    guid_esperado = known_folders.IDENTIFICADORES[nombre]
    vistos: list[str] = []

    def _falso(guid: str) -> str | None:
        vistos.append(guid)
        return efectiva if guid == guid_esperado else None

    monkeypatch.setattr(known_folders, "_resolver_por_api", _falso)

    assert known_folders.resolver_known_folder(nombre) == pathlib.PureWindowsPath(efectiva)
    assert vistos == [guid_esperado], "se resolvió por identificador, no por nombre de carpeta"


def test_una_carpeta_desconocida_es_un_error_de_programacion() -> None:
    """No hay resolución "best-effort" de nombres arbitrarios: el conjunto es cerrado."""
    with pytest.raises(KeyError):
        known_folders.resolver_known_folder("Music")


def test_prohibidos_enumera_las_tres_carpetas_con_su_ruta_efectiva(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rutas = {
        known_folders.IDENTIFICADORES["Documents"]: r"D:\Users\facha\Docs",
        known_folders.IDENTIFICADORES["Desktop"]: r"C:\Users\facha\Escritorio",
        known_folders.IDENTIFICADORES["Downloads"]: r"E:\Descargas",
    }
    monkeypatch.setattr(known_folders, "_resolver_por_api", rutas.get)

    prohibidos = known_folders.known_folders_prohibidos()

    assert [nombre for nombre, _ in prohibidos] == ["Documents", "Desktop", "Downloads"]
    assert [str(ruta) for _, ruta in prohibidos] == [
        r"D:\Users\facha\Docs",
        r"C:\Users\facha\Escritorio",
        r"E:\Descargas",
    ]


def test_una_carpeta_que_la_api_no_resuelve_no_inventa_una_ruta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin respuesta de la API la carpeta NO entra a la lista.

    Fail-closed acá sería inventar una ruta derivada del perfil — exactamente lo
    prohibido. La contención la aporta el resto de la admisión (solapamiento con
    game/MO2/TEMP, drive root, UNC), no una adivinanza.
    """
    monkeypatch.setattr(known_folders, "_resolver_por_api", lambda guid: None)

    assert known_folders.known_folders_prohibidos() == ()
    assert known_folders.resolver_known_folder("Documents") is None


def test_no_deriva_del_perfil_del_usuario(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mover `%USERPROFILE%` no mueve la Known Folder: la autoridad es la API."""
    monkeypatch.setenv("USERPROFILE", r"Z:\perfil\movido")
    monkeypatch.setenv("HOME", r"Z:\perfil\movido")
    monkeypatch.setattr(
        known_folders,
        "_resolver_por_api",
        lambda guid: r"D:\Users\facha\Docs" if guid == known_folders.IDENTIFICADORES["Documents"] else None,
    )

    assert known_folders.resolver_known_folder("Documents") == pathlib.PureWindowsPath(r"D:\Users\facha\Docs")


def test_en_windows_una_carpeta_sin_respuesta_queda_indeterminada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "No hay carpetas" y "no contestaron" son veredictos distintos.

    En Windows la carpeta EXISTE: que la API no conteste no significa que este
    root no sea `Documents`, significa que no lo podemos saber. Reportarlo como
    conjunto vacío era indistinguible de Linux, y admitía justo el root que
    había que rechazar.
    """
    monkeypatch.setattr(known_folders, "plataforma_resuelve_known_folders", lambda: True)
    solo_desktop = {known_folders.IDENTIFICADORES["Desktop"]: r"C:\Users\facha\Desktop"}
    monkeypatch.setattr(known_folders, "_resolver_por_api", solo_desktop.get)

    inspeccion = known_folders.inspeccionar_known_folders_prohibidas()

    assert [nombre for nombre, _ in inspeccion.rutas] == ["Desktop"]
    assert inspeccion.indeterminadas == ("Documents", "Downloads")


def test_fuera_de_windows_no_hay_indeterminadas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin Known Folders no falta evidencia: no existe la carpeta a comparar.

    La contención en esas plataformas la dan las otras reglas de admisión, y
    marcar las tres como indeterminadas volvería IMPOSIBLE configurar un
    `external_work_root` en Linux — fail-closed convertido en fail-siempre.
    """
    monkeypatch.setattr(known_folders, "plataforma_resuelve_known_folders", lambda: False)
    monkeypatch.setattr(known_folders, "_resolver_por_api", lambda guid: None)

    inspeccion = known_folders.inspeccionar_known_folders_prohibidas()

    assert inspeccion.rutas == ()
    assert inspeccion.indeterminadas == ()


def test_la_plataforma_se_decide_por_sys_platform_y_no_por_la_respuesta() -> None:
    """El seam de plataforma no se infiere de que la API haya contestado."""
    import sys

    assert known_folders.plataforma_resuelve_known_folders() is (sys.platform == "win32")


def test_ancla_de_fuente_sin_userprofile_ni_substrings() -> None:
    """Ancla por código: las dos formas prohibidas por el ADR no pueden reaparecer.

    Un futuro "arreglo rápido" que agregue `os.environ["USERPROFILE"]` o un
    `"Documents" in str(path)` rompe acá antes de llegar a producción — que es la
    única razón por la que esta regla no envejece como prosa.
    """
    fuente = MODULO.read_text(encoding="utf-8")
    # Se mira sólo el CÓDIGO: el docstring del módulo nombra a propósito lo que
    # está prohibido, y prohibir la palabra sería prohibir explicarla.
    codigo = "\n".join(linea for linea in fuente.splitlines() if not linea.lstrip().startswith("#"))
    _, _, tras_docstring = codigo.partition('"""')
    _, _, cuerpo = tras_docstring.partition('"""')

    assert "USERPROFILE" not in cuerpo
    assert "expanduser" not in cuerpo
    assert "Path.home" not in cuerpo


def test_ancla_de_fuente_sin_comparacion_por_nombre_de_carpeta() -> None:
    """Ancla el PATRÓN prohibido, no el literal que se nos ocurrió hoy.

    `assert '"OneDrive"' not in fuente` sólo ataja la palabra que ya conocíamos:
    `"Documentos" in str(ruta)`, `ruta.name == "Desktop"` o
    `"onedrive" in ruta.as_posix().casefold()` son el MISMO error —identidad por
    nombre— y pasaban limpios. Se prohíbe la forma: ninguna comparación
    (`in`/`==`) del módulo puede tener un literal de texto de un lado.

    La tabla `IDENTIFICADORES` y la tupla `KNOWN_FOLDERS_PROHIBIDOS` quedan
    fuera: ahí los nombres son CLAVES de un mapa a GUID, no un criterio de
    identidad de rutas.
    """
    import ast

    arbol = ast.parse(MODULO.read_text(encoding="utf-8"), filename=str(MODULO))
    tablas = {
        nodo
        for nodo in arbol.body
        if isinstance(nodo, ast.AnnAssign)
        and isinstance(nodo.target, ast.Name)
        and nodo.target.id in {"IDENTIFICADORES", "KNOWN_FOLDERS_PROHIBIDOS"}
    }
    excluidos = {id(n) for tabla in tablas for n in ast.walk(tabla)}

    def _es_deteccion_de_plataforma(nodo: ast.Compare) -> bool:
        """`sys.platform == "win32"` compara la PLATAFORMA, no una ruta.

        Es la única comparación contra texto legítima del módulo, y se exime por
        lo que compara —un atributo concreto de `sys`— y no por el literal del
        otro lado, que es lo que volvería a abrir la puerta.
        """
        return any(
            isinstance(o, ast.Attribute)
            and o.attr == "platform"
            and isinstance(o.value, ast.Name)
            and o.value.id == "sys"
            for o in (nodo.left, *nodo.comparators)
        )

    ofensores: list[str] = []
    for nodo in ast.walk(arbol):
        if id(nodo) in excluidos or not isinstance(nodo, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn, ast.Eq, ast.NotEq)) for op in nodo.ops):
            continue
        if _es_deteccion_de_plataforma(nodo):
            continue
        operandos = [nodo.left, *nodo.comparators]
        if any(isinstance(o, ast.Constant) and isinstance(o.value, str) for o in operandos):
            ofensores.append(ast.unparse(nodo))

    assert not ofensores, f"identidad por nombre de carpeta reintroducida: {ofensores}"


def test_en_esta_plataforma_la_api_no_inventa_rutas() -> None:
    """Sin Windows no hay Known Folders: la primitiva lo dice, no lo simula.

    La honestidad importa: un fallback POSIX "equivalente" (``~/Documents``)
    sería el `%USERPROFILE%` prohibido con otro nombre, y haría que la suite
    verde en Linux no dijera nada sobre Windows. El contrato de identificador →
    ruta efectiva se ejerce con la seam (`_resolver_por_api`) en los tests de
    arriba; acá sólo se ancla que la rama real no adivina.
    """
    import sys

    if sys.platform != "win32":
        assert known_folders._resolver_por_api(known_folders.IDENTIFICADORES["Documents"]) is None
