"""#838 — la identidad de un mod en ``modlist.txt`` **no distingue mayúsculas**.

Decisión (la del issue #838, opción «sí»): ``X`` y ``x`` son el MISMO mod para la
primitiva. MO2 resuelve ``mods/<Nombre>`` contra el sistema de archivos de Windows,
que no distingue mayúsculas, y ``ganadores._exigir_enabled`` ya lo asume: rechaza el
perfil que trae el mismo nombre dos veces comparando con ``lower()``. Con la
comparación exacta que tenían los mutadores, un perfil con ``+x`` y un
``add_mod_to_modlist("X")`` terminaba con ``+x`` **y** ``+X`` — un perfil que el
diagnóstico de overlay rechaza —, y ni siquiera avisaba: el WARNING de #835 contaba
sólo coincidencias exactas.

El límite de la decisión, también fijado acá: se compara con ``lower()`` y **no** con
``casefold()``. ``casefold`` colapsa ``Straße``/``Strasse``, que NTFS **no** unifica;
tratarlas como el mismo mod fabricaría un «ya presente» que no existe (el mismo
límite que ``tests/test_mo2_ganadores.py`` congela del lado de ``ganadores``).

Este archivo **enumera los caminos** de la primitiva en vez de fijar un caso suelto:
las dos ramas de ``add_mod_to_modlist`` (por defecto y ``highest_priority``), sus dos
alias (``add_mod_to_modlist_highest_priority`` y ``priority="highest"``) y los otros
dos mutadores del modlist (``remove_mod_from_modlist`` y ``toggle_mod_in_modlist``),
que identifican el mismo recurso con el mismo nombre y no pueden quedar con otra
semántica. Al final hay dos anclas por enumeración: qué mutadores comparan por clave
—y cuáles tocan ``modlist.txt``— y quiénes son los callers de producción, para que un
camino nuevo rompa el test hasta que se le escriba su receta.

Los cuatro callers de producción (``external_tools`` vía ``_activar_en_modlist``,
``system_tools`` para FOMOD, ``ritual_runner`` para la GUI y ``grass_profile`` con
``highest_priority=True``) no se tocan: el cambio vive en la primitiva, y por eso la
tabla de arriba cubre las ramas que cada uno usa.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.ganadores import indexar_ganadores
from sky_claw.local.mo2.vfs import MO2Controller, _clave_de_nombre_de_mod
from sky_claw.local.mo2.vfs_attestation import read_enabled_mods

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent
_VFS = pathlib.Path("sky_claw/local/mo2/vfs.py")


# ── Infraestructura ─────────────────────────────────────────────────────────────


def _controlador(raiz: pathlib.Path) -> MO2Controller:
    return MO2Controller(raiz, path_validator=PathValidator(roots=[raiz]))


def _perfil(raiz: pathlib.Path) -> pathlib.Path:
    """Crea ``profiles/Default`` y devuelve la ruta del ``modlist.txt``."""
    profile_dir = raiz / "profiles" / "Default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir / "modlist.txt"


def _escribir(modlist: pathlib.Path, lineas: list[str], *, crlf: bool = False) -> bytes:
    """Escribe el modlist SIN BOM y devuelve los bytes escritos (para comparar intactos)."""
    crudo = ("\r\n" if crlf else "\n").join(lineas)
    if lineas:
        crudo += "\r\n" if crlf else "\n"
    contenido = crudo.encode("utf-8")
    modlist.write_bytes(contenido)
    return contenido


def _lineas(modlist: pathlib.Path) -> list[str]:
    return [ln.strip() for ln in modlist.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]


# ── La clave de comparación: decisión, no detalle ───────────────────────────────


def test_clave_de_nombre_de_mod_ignora_mayusculas_y_no_colapsa_lo_que_ntfs_distingue() -> None:
    """``lower()`` sí, ``casefold()`` no: el límite es el de NTFS, no el de Unicode."""
    assert _clave_de_nombre_de_mod("X") == _clave_de_nombre_de_mod("x")
    assert _clave_de_nombre_de_mod("Community Shaders") == _clave_de_nombre_de_mod("community shaders")
    # `casefold` unificaría estas dos; NTFS las distingue, así que son dos mods.
    assert _clave_de_nombre_de_mod("Straße") != _clave_de_nombre_de_mod("Strasse")


# ── Camino por defecto de `add_mod_to_modlist` ──────────────────────────────────


@pytest.mark.parametrize(
    ("antes", "mod_pedido", "despues"),
    [
        # `+x` ya está habilitado: `X` ES el mismo mod. No-op silencioso.
        (["+x", "+Otra"], "X", ["+x", "+Otra"]),
        # `+X` con el caller en minúsculas: mismo caso, al revés.
        (["+X"], "x", ["+X"]),
        # `-x` deshabilitado: se habilita EN SU LUGAR y conserva la grafía del archivo.
        (["+A", "-x", "+B"], "X", ["+A", "+x", "+B"]),
        # `-X` deshabilitado y caller en minúsculas: ídem.
        (["-X"], "x", ["+X"]),
        # `+x` manda sobre `-X`: se conserva la habilitada y se descarta la deshabilitada.
        (["+x", "-X"], "X", ["+x"]),
        (["-X", "+x"], "X", ["+x"]),
        # No estaba en ninguna grafía: se agrega al final con la grafía del caller.
        (["+Otra"], "X", ["+Otra", "+X"]),
        ([], "X", ["+X"]),
    ],
)
async def test_add_mod_to_modlist_reconoce_cualquier_grafia(
    tmp_path: pathlib.Path, antes: list[str], mod_pedido: str, despues: list[str]
) -> None:
    """La rama por defecto compara «ya presente» y clasifica ``+``/``-`` por clave."""
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    original = _escribir(modlist, antes, crlf=True)

    await controlador.add_mod_to_modlist(mod_pedido)

    if despues == antes:
        # Sin nada que hacer, el archivo ni se reescribe (la reescritura normaliza
        # a LF y agrega BOM: comparar bytes es la prueba de que no se tocó).
        assert modlist.read_bytes() == original
    else:
        assert _lineas(modlist) == despues


async def test_add_mod_to_modlist_avisa_de_duplicados_entre_grafias(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``+x`` y ``+X`` (sólo sale de editar a mano) son el duplicado que el WARNING debe contar.

    El aviso de #835 sólo contaba coincidencias exactas: con ``+x,+X`` no decía nada.
    Ahora cuenta las dos, nombra las grafías encontradas y no repara el archivo.
    """
    import logging

    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    original = _escribir(modlist, ["+x", "+Otra", "+X"], crlf=True)

    with caplog.at_level(logging.WARNING, logger="sky_claw.local.mo2.vfs"):
        await controlador.add_mod_to_modlist("X")

    assert modlist.read_bytes() == original
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(avisos) == 1
    mensaje = avisos[0].getMessage()
    assert "'X'" in mensaje
    assert "2 veces" in mensaje
    # Las dos grafías, para que el operador encuentre las líneas en MO2.
    assert "+x" in mensaje and "+X" in mensaje


async def test_add_mod_to_modlist_con_una_sola_habilitada_en_otra_grafia_no_avisa(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Control: ``+x`` sola y un pedido de ``X`` es «ya está», no un duplicado."""
    import logging

    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, ["+x"])

    with caplog.at_level(logging.WARNING, logger="sky_claw.local.mo2.vfs"):
        await controlador.add_mod_to_modlist("X")

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_add_mod_to_modlist_no_confunde_strasse_con_strasse(tmp_path: pathlib.Path) -> None:
    """``casefold`` las unificaría; NTFS no: con ``+Straße`` en el perfil, ``Strasse`` se agrega."""
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, ["+Straße"])

    await controlador.add_mod_to_modlist("Strasse")

    assert _lineas(modlist) == ["+Straße", "+Strasse"]


# ── Rama `highest_priority=True` (grass_profile) y sus alias ────────────────────


@pytest.mark.parametrize("via", ["kwarg", "alias", "priority"])
@pytest.mark.parametrize(
    ("antes", "despues"),
    [
        # Ya habilitado en otra grafía: se MUEVE al tope (no se duplica) y conserva
        # la grafía escrita por el operador.
        (["# header", "+Otra", "+x"], ["# header", "+x", "+Otra"]),
        # Deshabilitado en otra grafía: sube habilitado, una sola entrada.
        (["+Otra", "-x"], ["+x", "+Otra"]),
        # Ausente: se inserta con la grafía del caller.
        (["+Otra"], ["+X", "+Otra"]),
    ],
)
async def test_add_mod_to_modlist_highest_priority_deduplica_entre_grafias(
    tmp_path: pathlib.Path, antes: list[str], despues: list[str], via: str
) -> None:
    """Las tres formas de pedir máxima prioridad pasan por la misma rama."""
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, antes)

    if via == "kwarg":
        await controlador.add_mod_to_modlist("X", highest_priority=True)
    elif via == "alias":
        await controlador.add_mod_to_modlist_highest_priority("X")
    else:
        await controlador.add_mod_to_modlist("X", priority="highest")

    assert _lineas(modlist) == despues


# ── Los otros dos mutadores del modlist ─────────────────────────────────────────


async def test_remove_mod_from_modlist_quita_cualquier_grafia(tmp_path: pathlib.Path) -> None:
    """Desinstalar ``X`` tiene que quitar ``+x``: si no, add lo sigue viendo «presente»."""
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, ["+A", "-x", "+B", "+X"])

    await controlador.remove_mod_from_modlist("X")

    assert _lineas(modlist) == ["+A", "+B"]


async def test_remove_mod_from_modlist_no_toca_otro_mod(tmp_path: pathlib.Path) -> None:
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    original = _escribir(modlist, ["+Otra", "-Straße"], crlf=True)

    await controlador.remove_mod_from_modlist("Strasse")

    assert modlist.read_bytes() == original


@pytest.mark.parametrize(
    ("antes", "enable", "despues"),
    [
        # Deshabilitar `x` pedido como `X`: cambia el signo y conserva la grafía.
        (["+A", "+x"], False, ["+A", "-x"]),
        (["+A", "-x"], True, ["+A", "+x"]),
        # Ya estaba en el estado pedido: no se reescribe (bytes intactos aparte).
        (["+A", "+X"], True, ["+A", "+X"]),
        (["+A", "-X"], False, ["+A", "-X"]),
    ],
)
async def test_toggle_mod_in_modlist_reconoce_cualquier_grafia(
    tmp_path: pathlib.Path, antes: list[str], enable: bool, despues: list[str]
) -> None:
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, antes)

    await controlador.toggle_mod_in_modlist("X", enable=enable)

    assert _lineas(modlist) == despues


async def test_toggle_mod_in_modlist_no_escribe_si_no_hay_entrada(tmp_path: pathlib.Path) -> None:
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    original = _escribir(modlist, ["+Otra"], crlf=True)

    await controlador.toggle_mod_in_modlist("X", enable=False)

    assert modlist.read_bytes() == original


# ── La consecuencia del issue, de punta a punta ─────────────────────────────────


@pytest.mark.parametrize("mod_pedido", ["X", "x", "X"])
async def test_ningun_camino_deja_el_duplicado_que_ganadores_rechaza(tmp_path: pathlib.Path, mod_pedido: str) -> None:
    """Con ``+x`` en el perfil, agregar ``X`` no puede terminar en un perfil irresoluble.

    Es exactamente la consecuencia que el issue describe: ``read_enabled_mods``
    devolvía ``("x", "X")`` y ``ganadores._exigir_enabled`` rechazaba el perfil
    entero («el modlist repite el mod»). Acá se mide sobre ``indexar_ganadores``,
    que es el consumidor real, con el mod en disco.
    """
    controlador = _controlador(tmp_path)
    modlist = _perfil(tmp_path)
    _escribir(modlist, ["+x"])
    mod_root = tmp_path / "mods" / "x" / "textures"
    mod_root.mkdir(parents=True)
    (mod_root / "muro01_n.dds").write_bytes(b"x")

    await controlador.add_mod_to_modlist(mod_pedido)

    habilitados = read_enabled_mods(modlist)
    assert [nombre.lower() for nombre in habilitados] == ["x"]
    # No levanta `GanadoresError`: el nombre no está repetido bajo la identidad de Windows.
    indice = indexar_ganadores(mods_dir=tmp_path / "mods", data_root=tmp_path, enabled=habilitados)
    ganador = indice.ganador("textures/muro01_n.dds")
    assert ganador is not None and ganador.capa == "x"


# ── Anclas por enumeración: mutadores y callers ─────────────────────────────────

#: Método de ``MO2Controller`` → cómo identifica el mod contra el ``modlist.txt``.
#: ``clave`` = compara con :func:`_clave_de_nombre_de_mod`; ``delega`` = es el alias
#: de ``add_mod_to_modlist`` y no lee el archivo.
#:
#: Congelado por igualdad literal: un mutador nuevo (o uno que vuelva a comparar de
#: forma exacta) rompe el test hasta que se lo clasifique. El conjunto de mutadores
#: también está congelado en ``tests/test_mo2_modlist_priority.py``
#: (``test_la_api_publica_de_mo2controller_esta_clasificada``), que congela la API
#: pública entera de ``MO2Controller``.
COMPARACION_POR_MUTADOR = {
    "add_mod_to_modlist": "clave",
    "add_mod_to_modlist_highest_priority": "delega",
    "remove_mod_from_modlist": "clave",
    "toggle_mod_in_modlist": "clave",
}

#: Callers de producción de la primitiva (no hay que cablear nada nuevo: el cambio
#: vive en ella). Igualdad literal: una superficie nueva rompe el ancla.
#: - ``external_tools``: NGIO y Community Shaders por el agente LLM;
#: - ``system_tools``: FOMOD instalado por el agente LLM;
#: - ``ritual_runner``: Community Shaders por la GUI;
#: - ``grass_profile``: el mod de config del clon, con ``highest_priority=True``;
#: - ``vfs``: el alias ``add_mod_to_modlist_highest_priority``.
CALLERS_DE_LA_PRIMITIVA = frozenset(
    {
        "sky_claw/app/agent/tools/external_tools.py",
        "sky_claw/app/agent/tools/system_tools.py",
        "sky_claw/app/gui/controllers/ritual_runner.py",
        "sky_claw/local/mo2/grass_profile.py",
        "sky_claw/local/mo2/vfs.py",
    }
)


def _metodos_de_mo2controller() -> dict[str, ast.AsyncFunctionDef]:
    arbol = ast.parse((_RAIZ_DEL_REPO / _VFS).read_text(encoding="utf-8"))
    clase = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "MO2Controller")
    return {n.name: n for n in clase.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _nombres_cargados(nodo: ast.AST) -> set[str]:
    """Identificadores y atributos *leídos* en el cuerpo (llamadas incluidas)."""
    nombres: set[str] = set()
    for hijo in ast.walk(nodo):
        if isinstance(hijo, ast.Name) and isinstance(hijo.ctx, ast.Load):
            nombres.add(hijo.id)
        elif isinstance(hijo, ast.Attribute):
            nombres.add(hijo.attr)
    return nombres


def _literales(nodo: ast.AST) -> set[str]:
    return {hijo.value for hijo in ast.walk(nodo) if isinstance(hijo, ast.Constant) and isinstance(hijo.value, str)}


def test_todo_mutador_del_modlist_compara_con_la_misma_clave() -> None:
    """La identidad del mod es UNA: si un mutador compara de otra forma, la clase vuelve."""
    metodos = _metodos_de_mo2controller()
    for nombre, forma in COMPARACION_POR_MUTADOR.items():
        assert nombre in metodos, f"{nombre} desapareció de MO2Controller: actualizá este ancla"
        cargados = _nombres_cargados(metodos[nombre])
        if forma == "clave":
            assert "_clave_de_nombre_de_mod" in cargados, (
                f"{nombre} no compara los nombres con _clave_de_nombre_de_mod: "
                "volvería a tratar `X` y `x` como mods distintos"
            )
        else:
            assert "_clave_de_nombre_de_mod" not in cargados
            assert "add_mod_to_modlist" in cargados


def test_los_metodos_que_leen_el_modlist_estan_enumerados() -> None:
    """Un método nuevo que parsee ``modlist.txt`` rompe el ancla hasta que se lo clasifique.

    ``add_mod_to_modlist_highest_priority`` no está: delega en ``add_mod_to_modlist`` y
    no toca el archivo. ``read_modlist`` lo lee pero no identifica ningún mod, así que no
    tiene clave que unificar.
    """
    metodos = _metodos_de_mo2controller()
    leen_el_modlist = {nombre for nombre, nodo in metodos.items() if "modlist.txt" in _literales(nodo)}

    assert leen_el_modlist == {
        "read_modlist",
        "add_mod_to_modlist",
        "remove_mod_from_modlist",
        "toggle_mod_in_modlist",
    }


def test_los_callers_de_la_primitiva_estan_enumerados() -> None:
    """El cambio alcanza a todos los callers; una superficie nueva rompe el ancla."""
    llamadores = {
        ruta.relative_to(_RAIZ_DEL_REPO).as_posix()
        for ruta in (_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")
        if any(
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr in {"add_mod_to_modlist", "add_mod_to_modlist_highest_priority"}
            for nodo in ast.walk(ast.parse(ruta.read_text(encoding="utf-8")))
        )
    }

    assert llamadores == CALLERS_DE_LA_PRIMITIVA
