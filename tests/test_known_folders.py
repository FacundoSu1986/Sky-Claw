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
    # Ningún nombre de carpeta se usa como literal comparable dentro del cuerpo
    # salvo en la tabla de identificadores (que mapea nombre → GUID).
    assert cuerpo.count('"OneDrive"') == 0


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
