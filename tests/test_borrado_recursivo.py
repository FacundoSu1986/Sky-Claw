"""Ancla de familia: **todo** borrado recursivo del paquete es link-aware.

``shutil.rmtree`` es ciego a los junctions de Windows —su guard y su recorrido
interno usan ``os.path.islink()``/``DirEntry.is_dir()``, que no distinguen un
``IO_REPARSE_TAG_MOUNT_POINT`` de un directorio real— así que **atraviesa** el
reparse point y borra el contenido del árbol destino, sin excepción que lo
delate. El PR #404 arregló ese modo de falla en UN archivo
(``local/tools/_dir_rollback.py``); el censo posterior encontró **16 sitios de
borrado recursivo en 9 módulos**, 8 de ellos apuntando a árboles del usuario y
ninguno protegido en niveles anidados.

**El invariante, enunciado como propiedad del mecanismo:** *borrar un árbol
exige decidir, en cada nivel, si lo que se está por recorrer es un enlace — y
esa decisión vive en una sola primitiva.* Enunciado así cubre los 16 sitios de
una vez, en vez de ser una lista de llamadas a parchear.

**Enumera, no muestrea** (molde de ``tests/test_rollback_salida.py``): un
``shutil.rmtree`` nuevo en cualquier módulo rompe el guard de completitud hasta
que alguien lo clasifique, y la clasificación no puede ser prosa — quien dice
delegar tiene que importar la primitiva de verdad.

**Se afirma sobre el AST y no sobre un grep**, por el mismo motivo que
``test_links.py::test_la_deteccion_de_junctions_vive_en_un_solo_modulo``: el
docstring de ``app/security/links.py`` menciona ``shutil.rmtree`` para explicar
por qué la primitiva existe, así que un barrido por substring se marcaría en
rojo por un **comentario**. Nombrar el peligro no es cometerlo.
"""

from __future__ import annotations

import ast
import pathlib

_PAQUETE = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

#: Símbolos que borran un árbol entero. ``os.removedirs`` no se usa hoy en el
#: paquete; entra igual para que aparecer mañana rompa el ancla en vez de
#: colarse como una familia que nadie enumeró.
_BORRADORES_CRUDOS = {"rmtree", "removedirs"}

#: La primitiva y los wrappers que delegan en ella.
_BORRADORES_LINK_AWARE = {"rmtree_link_aware", "_rmtree_force"}


def _referencias(ruta: pathlib.Path, nombres: set[str]) -> bool:
    """True si *ruta* menciona alguno de *nombres* como CÓDIGO, no como prosa.

    Cubre las dos formas en que un borrador aparece: llamado
    (``shutil.rmtree(x)`` → ``Attribute``; ``_rmtree_force(x)`` → ``Name``) y
    pasado por referencia (``asyncio.to_thread(rmtree_link_aware, x)`` → ``Name``
    sin ``Call`` alrededor), que es como lo usan la mitad de los call sites.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    aliases = {
        alias.asname or alias.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.ImportFrom)
        for alias in nodo.names
        if alias.name in nombres
    }
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute) and nodo.attr in nombres:
            return True
        if isinstance(nodo, ast.Name) and nodo.id in nombres | aliases:
            return True
    return False


def test_el_censo_resuelve_aliases_de_importacion(tmp_path: pathlib.Path) -> None:
    """Un alias de ``rmtree`` sigue siendo un borrador crudo y rompe el ancla."""
    modulo = tmp_path / "borrador_alias.py"
    modulo.write_text(
        "from shutil import rmtree as remove_tree\n\ndef borrar(path):\n    remove_tree(path)\n",
        encoding="utf-8",
    )

    assert _referencias(modulo, _BORRADORES_CRUDOS)


def _modulos(nombres: set[str]) -> set[str]:
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix() for ruta in _PAQUETE.rglob("*.py") if _referencias(ruta, nombres)
    }


#: Módulo → con qué borra árboles.
#:
#: ``"link-aware"``  — importa ``app.security.links.rmtree_link_aware``.
#: ``"vía-wrapper"`` — usa ``_rmtree_force``, el wrapper que sólo agrega la
#:                     limpieza de read-only de Windows y delega en la primitiva.
#:                     Es delegación real, pero **indirecta**, y se nombra
#:                     aparte para que la tabla no afirme más de lo que hay.
#: ``"pendiente"``   — sigue con un borrado ciego, con el motivo NOMBRADO abajo.
#:                     No es un "ya lo miramos": es deuda declarada.
#:
#: ``app/security/links.py`` NO figura acá **a propósito**: no *usa* un borrador
#: de árboles, lo *implementa* (con ``os.scandir`` + recursión propia). Que la
#: primitiva no aparezca en su propia enumeración es la señal de que el barrido
#: mide uso y no definición.
MECANISMO_DE_BORRADO: dict[str, str] = {
    "sky_claw/app/db/snapshot_manager.py": "link-aware",
    "sky_claw/local/fomod/installer.py": "link-aware",
    "sky_claw/local/mo2/bridge_installer.py": "link-aware",
    "sky_claw/local/mo2/grass_profile.py": "vía-wrapper",
    "sky_claw/local/mo2/profile_sandbox.py": "vía-wrapper",
    "sky_claw/local/mo2/vfs.py": "link-aware",
    "sky_claw/local/tools/_dir_rollback.py": "link-aware",
    "sky_claw/local/tools/dyndolod_runner.py": "link-aware",
    "sky_claw/local/tools/rollback_reconciler.py": "link-aware",
    "sky_claw/local/tools/vramr_service.py": "link-aware",
}

#: Motivo, para los que NO delegan. Escribir el racional no cuenta como
#: arreglarlo (#318/#373): siguen contando como deuda abierta.
MOTIVO: dict[str, str] = {}


def test_ningun_modulo_borra_arboles_con_shutil_rmtree() -> None:
    """El ancla dura: ``shutil.rmtree`` no se usa en ninguna parte del paquete.

    Es la formulación más fuerte posible del invariante — no "casi todos
    delegan" sino "la llamada ciega no existe". Un módulo nuevo que la
    reintroduzca rompe acá, aunque su autor recuerde clasificarlo abajo.

    Se afirma por igualdad contra el conjunto vacío y no con un ``not in``: un
    ancla que sólo verifica presencia no detecta la copia nueva (mismo criterio
    que ``RITUAL_TOOL_MAP == {...}`` en ``tests/test_ritual_dispatch.py``).
    """
    assert _modulos(_BORRADORES_CRUDOS) == set()


def test_la_enumeracion_cubre_a_todo_el_que_borra_arboles() -> None:
    """Guard de completitud: un borrador nuevo obliga a declarar CÓMO borra.

    Sin esto, el módulo #10 hereda el silencio en vez de la primitiva — que es
    exactamente lo que pasó con los 8 módulos que quedaron atrás cuando #404
    arregló uno solo.
    """
    assert _modulos(_BORRADORES_CRUDOS | _BORRADORES_LINK_AWARE) == set(MECANISMO_DE_BORRADO)


def test_todo_borrador_pendiente_nombra_su_motivo() -> None:
    """ "Pendiente" sin motivo escrito es indistinguible de un olvido."""
    pendientes = {m for m, mecanismo in MECANISMO_DE_BORRADO.items() if mecanismo == "pendiente"}

    assert pendientes == set(MOTIVO)
    assert all(MOTIVO[m].strip() for m in pendientes)


def _importa(ruta: pathlib.Path, simbolo: str, *, desde: str | None = None) -> bool:
    """True si *ruta* importa *simbolo*, opcionalmente exigiendo el módulo origen.

    *desde* se deja libre para ``_rmtree_force`` a propósito: hay una sola
    definición (``local/mo2/vfs``) pero ``profile_sandbox`` la re-exporta, y
    atar el ancla a un módulo concreto la volvería frágil ante un re-export
    nuevo — justo la clase de acoplamiento que estas anclas existen para evitar.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    return any(
        isinstance(nodo, ast.ImportFrom)
        and (desde is None or nodo.module == desde)
        and any(alias.name == simbolo for alias in nodo.names)
        for nodo in ast.walk(arbol)
    )


def _modulos_que_importan(simbolo: str, *, desde: str | None = None) -> set[str]:
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if _importa(ruta, simbolo, desde=desde)
    }


def test_quien_dice_ser_link_aware_importa_la_primitiva() -> None:
    """La clasificación no es prosa: quien figura como ``"link-aware"`` tiene que
    importar ``rmtree_link_aware`` de verdad, y quien la importa tiene que estar
    clasificado así.

    Sin la doble dirección, un módulo podía quedar etiquetado como protegido sin
    tener el mecanismo — o adquirirlo sin que la tabla lo refleje, que es cómo
    una tabla de estado empieza a mentir.
    """
    declarados = {m for m, mecanismo in MECANISMO_DE_BORRADO.items() if mecanismo == "link-aware"}

    assert declarados == _modulos_que_importan("rmtree_link_aware", desde="sky_claw.app.security.links")


def test_quien_dice_delegar_por_wrapper_usa_el_wrapper() -> None:
    """Misma doble dirección para la delegación INDIRECTA.

    ``grass_profile`` y ``profile_sandbox`` no importan la primitiva: importan
    ``_rmtree_force``, el wrapper que sólo agrega la limpieza de read-only y
    delega. Es delegación real, pero se clasifica aparte para no afirmar que el
    módulo consulta la primitiva cuando en verdad depende de que su wrapper siga
    haciéndolo — si mañana ``_rmtree_force`` volviera a ``shutil.rmtree``, quien
    lo atrapa es el ancla dura de arriba, en el módulo del wrapper.
    """
    declarados = {m for m, mecanismo in MECANISMO_DE_BORRADO.items() if mecanismo == "vía-wrapper"}

    assert declarados == _modulos_que_importan("_rmtree_force")
