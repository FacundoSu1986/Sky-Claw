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
    # Cuenta los ``.nif``/``.tri`` del target para decidir si BodySlide construyó
    # de verdad (su exit code es 0 aunque no construya nada). Mide link-aware
    # porque su contraparte que borra es ``DirectoryRollback``: si el conteo
    # entrara a un árbol ajeno por un junction, mallas de otro mod validarían el
    # build y el rollback descartaría el backup dando por buena una salida que no
    # existe.
    "sky_claw/local/tools/bodyslide_runner.py": "link-aware",
    "sky_claw/local/assets/asset_scanner.py": "sin-contraparte-que-borre",
    "sky_claw/local/tools/grass_cache_runner.py": "sin-contraparte-que-borre",
}


#: Lo que ofrece la primitiva para recorrer un árbol sin entrar a lo ajeno.
_MEDIDORES_LINK_AWARE = {"tamano_de_arbol_propio", "iter_archivos_propios"}


#: Formas de recorrer un árbol entero. Cualquiera de ellas junto a una lectura
#: de ``st_size`` es una medición cruda.
#:
#: Están las cinco y no sólo ``rglob``/``walk`` porque el censo vale lo que vale
#: su cobertura: ``Path.glob("**/*")`` recorre igual y el atributo es otro, y una
#: recursión propia se arma con ``scandir`` o ``iterdir``. Un medidor escrito con
#: cualquiera de esas formas no entraba al censo NI importaba la primitiva, así
#: que el guard de completitud se quedaba verde y el medidor sin declarar — el
#: mismo agujero que este archivo existe para cerrar.
_RECORRIDOS_CRUDOS = {"rglob", "glob", "walk", "scandir", "iterdir"}


def _es_recorrido_crudo(nodo: ast.expr) -> bool:
    """True si *nodo* es una llamada a un recorrido de árbol de la stdlib."""
    if not isinstance(nodo, ast.Call):
        return False
    llamado = nodo.func
    if isinstance(llamado, ast.Attribute):
        return llamado.attr in _RECORRIDOS_CRUDOS
    return isinstance(llamado, ast.Name) and llamado.id in _RECORRIDOS_CRUDOS


def _lee_st_size(nodo: ast.AST) -> bool:
    return any(isinstance(hijo, ast.Attribute) and hijo.attr == "st_size" for hijo in ast.walk(nodo))


def _mide_crudo(ruta: pathlib.Path) -> bool:
    """True si *ruta* lee ``st_size`` **recorriendo** un árbol sin política de enlaces.

    Exige que las dos cosas estén **ligadas**, no que coexistan en el archivo:
    el ``st_size`` tiene que leerse dentro del cuerpo de un ``for`` —o de una
    comprensión— que itera sobre un recorrido crudo. La versión anterior pedía
    sólo que ambos nombres aparecieran en el módulo, y eso confunde dos cosas
    distintas: un ``glob`` para encontrar archivos y un ``st_size`` sobre uno
    solo, en funciones que ni se tocan, no es medir un árbol. Medido: con la
    regla laxa el censo pasaba de 2 a 7 módulos, y cinco eran falsos positivos.

    Cubre las dos formas reales, porque el repo tiene una de cada:
    ``sum(f.stat().st_size for f in d.rglob("*"))`` (comprensión) y
    ``for fp in d.rglob("*"): ts += fp.stat().st_size`` (bucle).

    Y mira ``Attribute`` **y** ``Name`` en el recorrido: ``from os import walk``
    seguido de ``walk(...)`` es un ``Name`` pelado.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.For)
            and _es_recorrido_crudo(nodo.iter)
            and any(_lee_st_size(sentencia) for sentencia in nodo.body)
        ):
            return True
        if (
            isinstance(nodo, ast.GeneratorExp | ast.ListComp | ast.SetComp | ast.DictComp)
            and any(_es_recorrido_crudo(generador.iter) for generador in nodo.generators)
            and _lee_st_size(nodo)
        ):
            return True
    return False


def _mide_con_primitiva(ruta: pathlib.Path) -> bool:
    return any(_importa(ruta, simbolo, desde="sky_claw.app.security.links") for simbolo in _MEDIDORES_LINK_AWARE)


def test_la_enumeracion_cubre_a_todo_el_que_mide_arboles() -> None:
    """Un medidor nuevo obliga a declarar con qué política de enlaces mide.

    El censo de BORRADO existía y el de MEDICIÓN no, así que los medidores
    quedaban fuera de toda enumeración: se los encontraba muestreando, y
    muestrear deja siempre uno afuera.
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

    Pedir sólo el import es insuficiente y se verifica: con un medidor
    revertido al recorrido crudo, el módulo SIGUE importando la primitiva para
    los otros, así que el ancla se quedaría verde afirmando "este módulo mide
    link-aware" cuando lo cierto sería "mide link-aware *en algunos lados*".
    Con la conjunción, revertir cualquiera de ellos lo pone rojo.
    """
    declarados = {m for m, mecanismo in MECANISMO_DE_MEDICION.items() if mecanismo == "link-aware"}
    reales = {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if _mide_con_primitiva(ruta) and not _mide_crudo(ruta)
    }

    assert declarados == reales


# ---------------------------------------------------------------------------
# La política de `limpiar_readonly`: quién necesita pedirlo, y quién no
# ---------------------------------------------------------------------------

#: Módulo → si sus invocaciones de ``rmtree_link_aware`` necesitan
#: ``limpiar_readonly=True``.
#:
#: ``"requiere"``    — el árbol que borra puede traer ``FILE_ATTRIBUTE_READONLY``
#:                     de Windows: salida de una herramienta externa, contenido
#:                     extraído de un archive, o una copia que propaga el modo
#:                     de la fuente. Sin el flag el borrado falla con
#:                     ``OSError`` — silencioso si está dentro de un
#:                     ``suppress(OSError)``, o abortando a mitad de un loop si
#:                     no.
#: ``"no-requiere"`` — el motivo NOMBRADO abajo, no argumentado nomás: escribir
#:                     el racional de por qué se excluye un sitio no cuenta
#:                     como verificarlo (AGENTS.md, #318/#373).
#:
#: Nace del PR #419: arregló ``dyndolod_runner.py`` y ``fomod/installer.py``,
#: dejó ``vramr_service.py`` con el mismo patrón sin el flag —lo agarró un
#: revisor, no un ancla— y este censo, al construirse, encontró DOS hermanos
#: más que nadie había señalado: ``snapshot_manager.py`` (dos sitios) y
#: ``rollback_reconciler.py`` snapshotean/clonan con ``shutil.copy2``
#: (directo o vía ``copytree(copy_function=...)``), que propaga el modo de la
#: fuente.
POLITICA_DE_LIMPIAR_READONLY: dict[str, str] = {
    "sky_claw/app/db/snapshot_manager.py": "requiere",
    "sky_claw/local/fomod/installer.py": "requiere",
    "sky_claw/local/mo2/bridge_installer.py": "no-requiere",
    "sky_claw/local/mo2/vfs.py": "requiere",
    "sky_claw/local/tools/dyndolod_runner.py": "requiere",
    "sky_claw/local/tools/rollback_reconciler.py": "requiere",
    "sky_claw/local/tools/vramr_service.py": "requiere",
}

#: Motivo, para los que NO lo requieren.
MOTIVO_DE_NO_REQUIERE: dict[str, str] = {
    "sky_claw/local/mo2/bridge_installer.py": (
        "staging/backup salen de copiar self._bundle -del propio repo, no de "
        "una herramienta externa- y _harden_tree los endurece con "
        "restrict_to_owner: en Windows eso es `icacls /grant:r usuario:(F)`, "
        "que otorga Control Total al owner y nunca toca "
        "FILE_ATTRIBUTE_READONLY. No hay modo por el que el borrado, hecho por "
        "el mismo proceso, se tope con un archivo read-only."
    ),
}


def _invocaciones_de_rmtree_link_aware(ruta: pathlib.Path) -> list[ast.Call]:
    """Toda invocación de ``rmtree_link_aware``: directa o pasada por referencia
    a ``asyncio.to_thread`` (la otra forma real que usa el paquete).

    Para la forma indirecta, ``limpiar_readonly`` no puede vivir en un ``Call``
    propio a ``rmtree_link_aware`` —no hay ninguno, se lo pasa por nombre—, así
    que el nodo relevante es el ``Call`` a ``to_thread``: ahí es donde el
    keyword aparece de verdad.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    invocaciones: list[ast.Call] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        objetivo = nodo.func
        if isinstance(objetivo, ast.Attribute):
            nombre = objetivo.attr
        elif isinstance(objetivo, ast.Name):
            nombre = objetivo.id
        else:
            continue
        es_directa = nombre == "rmtree_link_aware"
        es_por_referencia = (
            nombre == "to_thread"
            and bool(nodo.args)
            and isinstance(nodo.args[0], ast.Name)
            and nodo.args[0].id == "rmtree_link_aware"
        )
        if es_directa or es_por_referencia:
            invocaciones.append(nodo)
    return invocaciones


def _pide_limpiar_readonly(invocacion: ast.Call) -> bool:
    return any(
        kw.arg == "limpiar_readonly" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in invocacion.keywords
    )


def _modulos_que_invocan_rmtree_link_aware() -> set[str]:
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if _invocaciones_de_rmtree_link_aware(ruta)
    }


def test_la_enumeracion_cubre_a_todo_el_que_invoca_rmtree_link_aware() -> None:
    """Un caller nuevo de la primitiva obliga a declarar si necesita el flag.

    Guard de completitud, mismo molde que ``MECANISMO_DE_BORRADO``: sin esto,
    el próximo módulo que llame a ``rmtree_link_aware`` sobre un árbol ajeno
    hereda el silencio de ``vramr_service.py`` en vez de la pregunta.
    """
    assert _modulos_que_invocan_rmtree_link_aware() == set(POLITICA_DE_LIMPIAR_READONLY)


def test_todo_sitio_eximido_de_limpiar_readonly_nombra_su_motivo() -> None:
    """ "No-requiere" sin motivo escrito es indistinguible de un olvido."""
    eximidos = {m for m, politica in POLITICA_DE_LIMPIAR_READONLY.items() if politica == "no-requiere"}

    assert eximidos == set(MOTIVO_DE_NO_REQUIERE)
    assert all(MOTIVO_DE_NO_REQUIERE[m].strip() for m in eximidos)


def test_todo_sitio_que_requiere_el_flag_lo_pide_en_cada_invocacion() -> None:
    """No alcanza con que ALGUNA invocación lo pida: tiene que ser todas.

    Un módulo con dos llamadas —una con el flag y otra sin— pasaría un chequeo
    que sólo pregunte "¿alguna la pide?" y dejaría la segunda como el próximo
    huérfano silencioso.
    """
    requeridos = {m for m, politica in POLITICA_DE_LIMPIAR_READONLY.items() if politica == "requiere"}

    for modulo in requeridos:
        invocaciones = _invocaciones_de_rmtree_link_aware(_PAQUETE.parent / modulo)
        assert invocaciones, modulo
        assert all(_pide_limpiar_readonly(invocacion) for invocacion in invocaciones), (
            f"{modulo} está clasificado 'requiere' pero tiene una invocación sin limpiar_readonly=True"
        )


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

    @symlink_guard
    def test_una_raiz_que_es_un_enlace_mide_cero_y_borra_solo_el_enlace(self, tmp_path: pathlib.Path) -> None:
        """El caso donde medir y borrar toman caminos DISTINTOS y aun asi coinciden.

        La medicion sale temprano por ``tipo is not None``; el borrado si quita
        el reparse point. Que el numero sea el mismo —cero— no es trivial: es la
        unica rama donde una de las dos hace trabajo y la otra no.
        """
        ajeno = tmp_path / "ajeno"
        ajeno.mkdir()
        (ajeno / "grande.bin").write_bytes(b"x" * 10_000)

        raiz = tmp_path / "enlace"
        raiz.symlink_to(ajeno, target_is_directory=True)

        medido, archivos = tamano_de_arbol_propio(raiz)
        borrado = rmtree_link_aware(raiz)

        assert (medido, archivos) == (0, 0)
        assert borrado == medido
        assert not raiz.is_symlink()
        assert (ajeno / "grande.bin").read_bytes() == b"x" * 10_000

    @symlink_guard
    def test_un_enlace_roto_como_raiz_mide_cero_y_se_borra(self, tmp_path: pathlib.Path) -> None:
        """Ejerce el motivo por el que la primitiva usa ``path_present``, no ``exists()``.

        Un enlace colgante NO pasa ``exists()`` —que sigue al destino— asi que
        con ese guard se tomaria por "no hay nada aca" y el enlace quedaria sin
        borrar. Su docstring lo declara; hasta ahora nadie lo anclaba.
        """
        raiz = tmp_path / "colgante"
        raiz.symlink_to(tmp_path / "no-esta")

        assert not raiz.exists()
        assert tamano_de_arbol_propio(raiz) == (0, 0)
        assert rmtree_link_aware(raiz) == 0
        assert not raiz.is_symlink()
