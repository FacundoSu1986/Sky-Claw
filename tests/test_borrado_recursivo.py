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

from sky_claw.app.security.links import rmtree_link_aware, tamano_de_arbol_propio
from tests._symlink_guard import crear_junction, is_junction_real, junction_guard, symlink_guard

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


# ---------------------------------------------------------------------------
# La otra mitad del invariante: MEDIR un árbol usa la misma política que BORRARLO
# ---------------------------------------------------------------------------

#: Módulo → con qué política mide el tamaño de un árbol.
#:
#: ``"link-aware"``               — usa ``tamano_de_arbol_propio`` o el retorno
#:                                  de ``rmtree_link_aware``: no entra a un
#:                                  enlace, así que mide lo mismo que borraría.
#: ``"sin-contraparte-que-borre"`` — mide con un recorrido crudo, pero el módulo
#:                                  NO borra árboles, así que no hay ningún
#:                                  número que pueda divergir de un efecto.
#:
#: Existe porque arreglar el borrado ABRIÓ esta divergencia en vez de cerrarla.
#: Antes, ``rglob`` y ``shutil.rmtree`` eran igual de ciegos al junction: los dos
#: entraban, así que la cuenta y el efecto coincidían —mal, pero coincidían—.
#: Al volver link-aware sólo el borrado, el medidor quedó contando bytes que el
#: borrado ya no se lleva, y quien resta ese número de un presupuesto cree haber
#: liberado espacio que sigue ocupado.
#:
#: Enumera módulos que MIDEN, no que borran: son dos familias distintas y un
#: módulo puede estar en una sola.
MECANISMO_DE_MEDICION: dict[str, str] = {
    "sky_claw/app/db/snapshot_manager.py": "link-aware",
    "sky_claw/local/assets/asset_scanner.py": "sin-contraparte-que-borre",
}


#: Lo que ofrece la primitiva para recorrer un árbol sin entrar a lo ajeno.
_MEDIDORES_LINK_AWARE = {"tamano_de_arbol_propio", "iter_archivos_propios"}


def _mide_crudo(ruta: pathlib.Path) -> bool:
    """True si *ruta* suma ``st_size`` a lo largo de un recorrido recursivo CRUDO.

    Detecta el patrón por AST y no por substring: hace falta que en el mismo
    módulo convivan un recorrido recursivo (``rglob``/``walk``) y una lectura de
    ``st_size``. Un ``stat().st_size`` sobre UN archivo puntual no es medir un
    árbol y no debe entrar al censo. Que sea AST también hace que un comentario
    que *nombre* ``rglob`` para explicar por qué no se usa no cuente como uso.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    atributos = {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute)}
    return bool({"rglob", "walk"} & atributos) and "st_size" in atributos


def _mide_con_primitiva(ruta: pathlib.Path) -> bool:
    return any(_importa(ruta, simbolo, desde="sky_claw.app.security.links") for simbolo in _MEDIDORES_LINK_AWARE)


def test_la_enumeracion_cubre_a_todo_el_que_mide_arboles() -> None:
    """Un medidor nuevo obliga a declarar con qué política de enlaces mide.

    Es el guard que le faltaba a esta familia: el censo de BORRADO estaba
    completo y el de MEDICIÓN no existía, así que los cuatro medidores de
    ``snapshot_manager`` quedaron fuera de toda enumeración. Los encontré
    grepeando un patrón, conté tres, y el cuarto (``_collect_stats``) apareció
    recién al leer el archivo entero — muestrear en vez de enumerar, el error
    que estas anclas existen para atajar.
    """
    medidores = {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if _mide_crudo(ruta) or _mide_con_primitiva(ruta)
    }

    assert medidores == set(MECANISMO_DE_MEDICION)


def test_quien_se_exime_por_no_borrar_efectivamente_no_borra() -> None:
    """La exención se VERIFICA contra el censo de borrado, no se argumenta.

    ``asset_scanner`` mide con un ``rglob`` crudo y está bien que lo haga: un
    asset dentro de una carpeta junctioneada sigue siendo del mod, y saltearlo
    dejaría ciega a la detección de conflictos. Su ``st_size`` es metadata por
    archivo de un inventario — no se resta de ningún presupuesto ni decide qué
    se borra.

    Pero eso es un argumento, y ``AGENTS.md`` es explícito en que escribir el
    racional de por qué se excluye una rama **no cuenta como verificarla**
    (#318, #373). Así que la exención se ata a un hecho comprobable: el módulo
    no puede figurar en :data:`MECANISMO_DE_BORRADO`, que es el censo COMPLETO
    de quien borra árboles. Si mañana alguien le agrega un borrado, la exención
    deja de valer y este test se pone rojo hasta que se decida de nuevo — en vez
    de quedar amparada por un comentario que envejeció.
    """
    eximidos = {m for m, mecanismo in MECANISMO_DE_MEDICION.items() if mecanismo == "sin-contraparte-que-borre"}

    assert eximidos and not (eximidos & set(MECANISMO_DE_BORRADO))


def test_quien_dice_medir_link_aware_no_conserva_ningun_recorrido_crudo() -> None:
    """``"link-aware"`` exige importar la primitiva **y no medir crudo en ningún lado**.

    La primera versión de este test sólo pedía el import, y eso resultó
    insuficiente de una forma que verifiqué: revertí ``_calc_dir_size_and_remove``
    al ``rglob`` original y **el ancla siguió verde**, porque el módulo seguía
    importando la primitiva para sus otros medidores. La clasificación afirmaba
    "este módulo mide link-aware" cuando lo cierto era "este módulo mide
    link-aware *en algunos lados*".

    Es la misma debilidad que este archivo le reprocha a los demás —afirmar
    menos de lo que se dice verificar— dentro del ancla escrita para evitarla.
    Con la conjunción, revertir cualquiera de los cuatro medidores lo pone rojo.
    """
    declarados = {m for m, mecanismo in MECANISMO_DE_MEDICION.items() if mecanismo == "link-aware"}
    reales = {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if _mide_con_primitiva(ruta) and not _mide_crudo(ruta)
    }

    assert declarados == reales


class TestMedirYBorrarNoPuedenDivergir:
    """El invariante conductual: ``tamano_de_arbol_propio`` == lo que borra.

    Los censos de arriba verifican que cada módulo declare su mecanismo; esto
    verifica que los dos mecanismos **coincidan**. Sin esta mitad, medir y
    borrar podrían seguir caminos distintos y ambos estar "clasificados".
    """

    def test_en_un_arbol_sin_enlaces_medir_y_borrar_coinciden(self, tmp_path: pathlib.Path) -> None:
        raiz = tmp_path / "arbol"
        (raiz / "sub" / "hondo").mkdir(parents=True)
        (raiz / "a.bin").write_bytes(b"x" * 100)
        (raiz / "sub" / "b.bin").write_bytes(b"y" * 250)
        (raiz / "sub" / "hondo" / "c.bin").write_bytes(b"z" * 7)

        medido, archivos = tamano_de_arbol_propio(raiz)
        borrado = rmtree_link_aware(raiz)

        assert (medido, archivos) == (357, 3)
        assert borrado == medido

    @symlink_guard
    def test_un_symlink_no_aporta_bytes_ni_a_la_cuenta_ni_al_borrado(self, tmp_path: pathlib.Path) -> None:
        """El destino queda entero, y NINGUNO de los dos caminos lo cuenta."""
        ajeno = tmp_path / "ajeno"
        ajeno.mkdir()
        (ajeno / "grande.bin").write_bytes(b"x" * 10_000)

        raiz = tmp_path / "arbol"
        raiz.mkdir()
        (raiz / "propio.bin").write_bytes(b"y" * 30)
        (raiz / "enlace").symlink_to(ajeno, target_is_directory=True)

        medido, archivos = tamano_de_arbol_propio(raiz)
        borrado = rmtree_link_aware(raiz)

        assert (medido, archivos) == (30, 1)
        assert borrado == medido
        assert (ajeno / "grande.bin").read_bytes() == b"x" * 10_000

    @junction_guard
    def test_un_junction_tampoco_aporta_bytes(self, tmp_path: pathlib.Path) -> None:
        """El caso que ``rglob`` SÍ contaba de más, y por el que existe la primitiva.

        Es la mitad Windows del invariante: ``Path.rglob`` frena en un symlink
        —decide con ``is_dir(follow_symlinks=False)``— pero **entra** a un
        junction, porque ``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink para
        ``os.path.islink``. Medir con ``rglob`` y borrar link-aware divergía
        exactamente acá, y en ningún otro lado: el caso de symlink de arriba ya
        coincidía antes del fix.
        """
        ajeno = tmp_path / "ajeno"
        ajeno.mkdir()
        (ajeno / "grande.bin").write_bytes(b"x" * 10_000)

        raiz = tmp_path / "arbol"
        raiz.mkdir()
        (raiz / "propio.bin").write_bytes(b"y" * 30)
        assert crear_junction(raiz / "enlace", ajeno) is None
        assert is_junction_real(raiz / "enlace")

        # La medición ingenua que este módulo reemplaza: entra al junction.
        rglob_ingenuo = sum(f.stat().st_size for f in raiz.rglob("*") if f.is_file())
        medido, archivos = tamano_de_arbol_propio(raiz)
        borrado = rmtree_link_aware(raiz)

        assert rglob_ingenuo == 10_030, "el rglob crudo tiene que contar los bytes ajenos"
        assert (medido, archivos) == (30, 1)
        assert borrado == medido
        assert (ajeno / "grande.bin").read_bytes() == b"x" * 10_000

    def test_una_ruta_ausente_mide_cero_y_borra_cero(self, tmp_path: pathlib.Path) -> None:
        assert tamano_de_arbol_propio(tmp_path / "no-esta") == (0, 0)
        assert rmtree_link_aware(tmp_path / "no-esta") == 0
