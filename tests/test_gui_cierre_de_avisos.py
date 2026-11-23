"""Issue #996: la × de los avisos de la GUI queda adentro de su panel, se ve y sigue cerrando.

En el rig, el aviso del ritual llevó la × fuera del recuadro con un mensaje largo (una ruta y una
tupla COM sin espacios), así que el operador no lo podía cerrar. Reproducido en Chromium sobre los
paneles reales: con 564 px de viewport el panel ocupaba de x=22 a x=542 y la × quedaba en x=842.

La propiedad del mecanismo es esta: *en una fila flex, la × solo queda adentro si TODO ítem
hermano puede encogerse por debajo de su palabra más larga, y ella no se encoge*. Un ítem que
crece (``flex:1``) sin ``min-width:0`` fija su ancho mínimo en la palabra más larga. Sin
``overflow-wrap:anywhere`` una ruta sin espacios es una sola palabra. En el aviso había DOS ítems
así: el texto y el ``detalle`` de la acción de resume, que puede traer ``texgen_mod_path``.

Los tests enumeran la familia de botones de cierre por AST en TODO el paquete GUI y la congelan
por igualdad. Así, una × nueva o un panel nuevo rompe el ancla hasta que recibe la receta.
La comprobación del layout real (bounding boxes en Chromium) no corre en CI: el paquete no tiene
un navegador en la suite. Estos tests fijan las declaraciones de las que depende ese layout.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from nicegui import background_tasks, core

import sky_claw.app.gui.views.forge_dashboard as fd
from sky_claw.app.gui.controllers import ritual_runner as rr
from sky_claw.app.gui.state.reactive_store import get_store, reset_store_for_tests

_GUI = Path(__file__).resolve().parent.parent / "sky_claw" / "app" / "gui"
_FORGE = _GUI / "views" / "forge_dashboard.py"
_STYLES = (_GUI / "styles.css").read_text(encoding="utf-8")

#: Grafías del glifo de cierre que puede emitir un ``ui.html``/``ui.label``.
_GLIFOS_DE_CIERRE = frozenset({"&times;", "×", "&#215;", "&#xd7;", "&#xD7;"})

#: Familia COMPLETA de botones de cierre de la GUI: (archivo, función:variable) → nombre accesible.
#: Igualdad exacta: sumar una × en cualquier módulo de la GUI rompe el ancla hasta que se decida acá.
_FAMILIA_DE_CIERRE: dict[tuple[str, str], str] = {
    ("views/forge_dashboard.py", "_ritual_feedback_panel:x"): "Cerrar aviso del ritual",
    ("views/forge_dashboard.py", "_ritual_preflight_panel:x"): "Cerrar semáforo de preflight",
    ("views/forge_dashboard.py", "_parallax_diagnostico_panel:x"): "Cerrar informe de parallax",
}

#: Fondos sobre los que se dibuja la ×: el tramo CLARO del gradiente del aviso (el peor caso para
#: un glifo claro) y la tarjeta de parallax. El de preflight y parallax es la página detrás del panel.
_FONDOS_DE_LA_X = {"aviso": "#1e160e", "parallax": "#0e0c09", "pagina": "#0b0e14"}

#: AAA (7:1). El gris de antes (#8a8270) daba 4.68:1, pero con 15 px y una caja de 12×15 casi no se
#: veía. El umbral pide un contraste que se note también en un glifo fino.
_CONTRASTE_MINIMO = 7.0

#: WCAG 2.5.8 (tamaño mínimo del objetivo): 24×24 px CSS.
_OBJETIVO_MINIMO_PX = 24


# ── utilidades ───────────────────────────────────────────────────────────────


def _padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    return {hijo: nodo for nodo in ast.walk(arbol) for hijo in ast.iter_child_nodes(nodo)}


def _funcion_contenedora(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> ast.FunctionDef | None:
    actual: ast.AST | None = nodo
    while actual is not None and not isinstance(actual, ast.FunctionDef):
        actual = padres.get(actual)
    return actual


def _es_ui(nodo: ast.AST, metodo: str) -> bool:
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == "ui"
        and nodo.func.attr == metodo
    )


def _cadena(expresion: ast.expr) -> tuple[ast.expr, list[tuple[str, ast.Call]]]:
    """Desarma ``ui.element(...).props(...).classes(...)`` → (raíz, [(método, llamada), ...])."""
    eslabones: list[tuple[str, ast.Call]] = []
    actual: ast.expr = expresion
    while isinstance(actual, ast.Call) and isinstance(actual.func, ast.Attribute):
        eslabones.append((actual.func.attr, actual))
        actual = actual.func.value
    return actual, eslabones


def _texto(nodo: ast.expr) -> str:
    """Texto literal de un ``str`` o de un f-string; cada valor interpolado queda como ``{}``."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in nodo.values)
    raise AssertionError(f"se esperaba un literal de texto: {ast.unparse(nodo)}")


def _declaraciones(bloque: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for declaracion in bloque.split(";"):
        propiedad, separador, valor = declaracion.partition(":")
        if separador:
            props[propiedad.strip().lower()] = valor.strip()
    return props


def _crece(declaraciones: dict[str, str]) -> bool:
    """``flex:<n>`` o ``flex:<n> …`` con ``n > 0``; ``none``/``auto`` y la ausencia no crecen."""
    primero = declaraciones.get("flex", "").split()[:1]
    return bool(primero) and re.fullmatch(r"\d+(?:\.\d+)?", primero[0]) is not None and float(primero[0]) > 0


def _censo_de_cierres() -> dict[tuple[str, str], ast.FunctionDef]:
    """Todo ``ui.html``/``ui.label`` con un glifo de cierre en la GUI → el botón que lo envuelve.

    La identidad es ``función:variable`` del ``with <variable>:`` que lo contiene. Un glifo fuera
    de un ``with <nombre>:`` queda como ``<sin variable>`` y rompe el ancla: no hay receta que verificar.
    """
    censo: dict[tuple[str, str], ast.FunctionDef] = {}
    for archivo in sorted(_GUI.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        padres = _padres(arbol)
        for nodo in ast.walk(arbol):
            if not (_es_ui(nodo, "html") or _es_ui(nodo, "label")) or not nodo.args:
                continue
            primero = nodo.args[0]
            if not (isinstance(primero, ast.Constant) and str(primero.value).strip() in _GLIFOS_DE_CIERRE):
                continue
            envoltura = padres.get(padres.get(nodo))
            variable = "<sin variable>"
            if isinstance(envoltura, ast.With) and isinstance(envoltura.items[0].context_expr, ast.Name):
                variable = envoltura.items[0].context_expr.id
            funcion = _funcion_contenedora(nodo, padres)
            assert funcion is not None, f"glifo de cierre a nivel de módulo en {archivo}"
            censo[(archivo.relative_to(_GUI).as_posix(), f"{funcion.name}:{variable}")] = funcion
    return censo


def _boton(funcion: ast.FunctionDef, variable: str) -> list[tuple[str, ast.Call]]:
    asignaciones = [
        nodo.value
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Assign)
        and len(nodo.targets) == 1
        and isinstance(nodo.targets[0], ast.Name)
        and nodo.targets[0].id == variable
    ]
    assert len(asignaciones) == 1, f"{funcion.name}:{variable} debe asignarse una sola vez"
    raiz, eslabones = _cadena(asignaciones[0])
    elemento = [llamada for metodo, llamada in eslabones if metodo == "element"]
    assert isinstance(raiz, ast.Name) and raiz.id == "ui", f"{funcion.name}:{variable} no es un ui.element"
    assert elemento and ast.literal_eval(elemento[0].args[0]) == "button", (
        f"{funcion.name}:{variable} no es un <button>"
    )
    return eslabones


def _fila_de_la_x(funcion: ast.FunctionDef, variable: str) -> dict[str, str]:
    """Declaraciones de la fila flex que contiene a la × (el ``with ui.element("div").style(...)``).

    Si el estilo es una variable, se resuelve por su asignación en la misma función.
    """
    padres = _padres(funcion)
    asignacion = next(
        nodo
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Assign) and isinstance(nodo.targets[0], ast.Name) and nodo.targets[0].id == variable
    )
    actual: ast.AST | None = asignacion
    while actual is not None:
        actual = padres.get(actual)
        if not isinstance(actual, ast.With):
            continue
        _, eslabones = _cadena(actual.items[0].context_expr)
        estilos = [llamada.args[0] for metodo, llamada in eslabones if metodo == "style"]
        if not estilos:
            continue
        estilo = estilos[0]
        if isinstance(estilo, ast.Name):
            estilo = next(
                nodo.value
                for nodo in ast.walk(funcion)
                if isinstance(nodo, ast.Assign)
                and isinstance(nodo.targets[0], ast.Name)
                and nodo.targets[0].id == estilo.id
            )
        partes = [estilo] if not isinstance(estilo, ast.BinOp) else [estilo.left, estilo.right]
        return _declaraciones("".join(_texto(parte) for parte in partes))
    raise AssertionError(f"{funcion.name}:{variable} no está dentro de una fila con estilo")


def _bloques_de_estilo(funcion: ast.FunctionDef) -> list[str]:
    """Todo bloque de estilo literal de la función: los ``.style("…")`` y los ``style="…"`` del HTML."""
    bloques: list[str] = []
    padres = _padres(funcion)
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Constant) and isinstance(padres.get(nodo), ast.JoinedStr):
            continue  # se lee entero desde su JoinedStr
        if not (isinstance(nodo, ast.JoinedStr) or (isinstance(nodo, ast.Constant) and isinstance(nodo.value, str))):
            continue
        texto = _texto(nodo)
        atributos = re.findall(r'style="([^"]*)"', texto)
        bloques.extend(atributos if atributos else [texto])
    return bloques


def _regla_css(selector: str) -> dict[str, str]:
    """Declaraciones de la ÚNICA regla independiente de styles.css cuyo selector es ``selector``."""
    patron = rf"(?m)^[ \t]*{re.escape(selector)}[ \t]*\{{"
    reglas = [
        _STYLES[coincidencia.end() : _STYLES.index("}", coincidencia.end())]
        for coincidencia in re.finditer(patron, _STYLES)
    ]
    assert len(reglas) == 1, f"selector independiente ausente o duplicado en styles.css: {selector}"
    return _declaraciones(re.sub(r"/\*.*?\*/", "", reglas[0], flags=re.S))


def _reglas_que_nombran(selector: str) -> list[dict[str, str]]:
    """Reglas de styles.css cuya lista de selectores incluye ``selector`` (``a, b { … }``)."""
    sin_comentarios = re.sub(r"/\*.*?\*/", "", _STYLES, flags=re.S)
    return [
        _declaraciones(cuerpo)
        for selectores, cuerpo in re.findall(r"([^{}]+)\{([^{}]*)\}", sin_comentarios)
        if selector in {s.strip() for s in selectores.split(",")}
    ]


def _resolver_color(valor: str) -> str:
    """``var(--x)`` → su valor en ``:root``; un hex literal se devuelve tal cual."""
    variable = re.fullmatch(r"var\((--[\w-]+)\)", valor.strip())
    if variable:
        raiz = _STYLES[_STYLES.index(":root {") : _STYLES.index("}", _STYLES.index(":root {"))]
        hallado = re.search(rf"{re.escape(variable.group(1))}\s*:\s*([^;]+);", raiz)
        assert hallado, f"variable de color sin definir en :root: {variable.group(1)}"
        valor = hallado.group(1)
    valor = valor.strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", valor), f"el color de la × debe ser un hex opaco: {valor!r}"
    return valor


def _contraste(primero: str, segundo: str) -> float:
    def luminancia(hexa: str) -> float:
        canales = [int(hexa[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lineal = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canales]
        return 0.2126 * lineal[0] + 0.7152 * lineal[1] + 0.0722 * lineal[2]

    claro, oscuro = sorted((luminancia(primero), luminancia(segundo)), reverse=True)
    return (claro + 0.05) / (oscuro + 0.05)


# ── censo: la familia de cierres está congelada ──────────────────────────────


def test_la_familia_de_botones_de_cierre_esta_congelada() -> None:
    """Toda × de la GUI se enumera; una nueva rompe el ancla hasta que recibe la receta."""
    assert set(_censo_de_cierres()) == set(_FAMILIA_DE_CIERRE)


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
def test_la_x_usa_la_receta_compartida_sin_estilo_inline(clave: tuple[str, str]) -> None:
    """La × lleva ``sc-dismiss`` y ningún ``.style()``: un estilo inline le gana a la clase y la
    volvería a dejar chica, tenue o encogible, sin que la regla de styles.css lo muestre."""
    funcion = _censo_de_cierres()[clave]
    eslabones = _boton(funcion, clave[1].split(":")[1])
    clases = {
        clase
        for metodo, llamada in eslabones
        if metodo == "classes"
        for arg in llamada.args
        for clase in ast.literal_eval(arg).split()
    }
    assert "sc-dismiss" in clases, f"{clave[1]}: falta la clase sc-dismiss"
    assert "sc-btn" not in clases, f"{clave[1]}: la × es icónica, no un botón de la familia C2"
    assert not [metodo for metodo, _ in eslabones if metodo == "style"], f"{clave[1]}: estilo inline en la ×"
    assert not any(llamada.keywords for _, llamada in eslabones), f"{clave[1]}: keywords no verificables por AST"


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
def test_la_x_tiene_nombre_accesible(clave: tuple[str, str]) -> None:
    """Un botón solo-ícono necesita ``aria-label``: el lector de pantalla no puede leer «×» como «cerrar»."""
    funcion = _censo_de_cierres()[clave]
    props = " ".join(
        ast.literal_eval(arg)
        for metodo, llamada in _boton(funcion, clave[1].split(":")[1])
        if metodo == "props"
        for arg in llamada.args
    )
    assert re.findall(r'aria-label="([^"]+)"', props) == [_FAMILIA_DE_CIERRE[clave]]


# ── contención: ningún hermano empuja la × fuera de la fila ───────────────────


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
def test_la_fila_de_la_x_parte_las_palabras_largas(clave: tuple[str, str]) -> None:
    """La fila declara ``overflow-wrap:anywhere``. Se hereda, así que cubre a TODO hermano, de hoy o
    futuro: el texto, el detalle de la acción de resume y el contenido de la tarjeta de preflight.
    Con eso, una ruta sin espacios deja de fijar el ancho mínimo de su ítem."""
    funcion = _censo_de_cierres()[clave]
    fila = _fila_de_la_x(funcion, clave[1].split(":")[1])
    assert fila.get("display") == "flex", f"{clave[1]}: la × debería vivir en una fila flex"
    assert fila.get("overflow-wrap") == "anywhere", f"{clave[1]}: la fila no parte palabras largas"


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
def test_todo_item_que_crece_puede_encogerse(clave: tuple[str, str]) -> None:
    """Todo bloque con ``flex`` que crece declara ``min-width:0``: si no, su mínimo es la palabra más
    larga y empuja a los hermanos. Se enumeran TODOS los bloques de estilo de la función, no solo el
    del texto: en el aviso, el detalle de la acción de resume también es un ítem de la fila."""
    funcion = _censo_de_cierres()[clave]
    que_crecen = [bloque for bloque in _bloques_de_estilo(funcion) if _crece(_declaraciones(bloque))]
    assert que_crecen, f"{clave[1]}: sin ítems que crezcan, el ancla no está mirando nada"
    sin_min_width = [b for b in que_crecen if _declaraciones(b).get("min-width") != "0"]
    assert not sin_min_width, f"{clave[1]}: ítems que crecen sin min-width:0: {sin_min_width}"


def test_el_detalle_de_la_accion_de_resume_comparte_la_fila_sin_empujar() -> None:
    """Hermano dentro del aviso: el detalle puede traer ``texgen_mod_path``. Reproducido en Chromium,
    con una ruta larga llevaba la × a x=1617 px. El ``ui.html`` que lo envuelve es el ítem de la fila,
    así que es él quien declara que crece y que puede encogerse."""
    funcion = _censo_de_cierres()[("views/forge_dashboard.py", "_ritual_feedback_panel:x")]
    del_detalle = [
        _declaraciones(_texto(nodo.args[0]))
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "style"
        and _es_ui(nodo.func.value, "html")
        and "detalle" in ast.unparse(nodo.func.value)
    ]
    assert len(del_detalle) == 1, "el ui.html del detalle debe llevar su estilo de ítem de la fila"
    assert _crece(del_detalle[0]), "el detalle debe repartir el ancho con el texto, no fijarlo"
    assert del_detalle[0].get("min-width") == "0"


# ── visibilidad: receta .sc-dismiss en styles.css ─────────────────────────────


def test_receta_sc_dismiss_no_se_encoge_y_tiene_objetivo_comodo() -> None:
    regla = _regla_css(".sc-dismiss")
    assert regla.get("flex-shrink") == "0", "la × no puede encogerse dentro de la fila"
    assert regla.get("cursor") == "pointer"
    for eje in ("width", "height"):
        medida = re.fullmatch(r"(\d+(?:\.\d+)?)px", regla.get(eje, ""))
        assert medida and float(medida.group(1)) >= _OBJETIVO_MINIMO_PX, f"{eje} de la × menor a 24 px"


def test_receta_sc_dismiss_contrasta_en_reposo() -> None:
    color = _resolver_color(_regla_css(".sc-dismiss")["color"])
    for nombre, fondo in _FONDOS_DE_LA_X.items():
        contraste = _contraste(color, fondo)
        assert contraste >= _CONTRASTE_MINIMO, f"× {color} sobre {nombre} {fondo}: {contraste:.2f}:1"


@pytest.mark.parametrize("estado", [":hover", ":focus-visible"])
def test_receta_sc_dismiss_marca_hover_y_foco(estado: str) -> None:
    """Hover y foco de teclado iluminan el glifo y le dan un fondo translúcido. El outline dorado lo
    sigue poniendo la regla global ``:focus-visible``; esto solo suma el fondo."""
    reposo = _resolver_color(_regla_css(".sc-dismiss")["color"])
    reglas = _reglas_que_nombran(f".sc-dismiss{estado}")
    assert len(reglas) == 1, f"falta (o está duplicada) la regla .sc-dismiss{estado}"
    regla = reglas[0]
    assert regla.get("background", "transparent") not in {"none", "transparent"}
    activo = _resolver_color(regla["color"])
    assert activo != reposo, "el estado activo debe cambiar el color del glifo"
    assert all(_contraste(activo, fondo) >= _CONTRASTE_MINIMO for fondo in _FONDOS_DE_LA_X.values())


# ── comportamiento: la × sigue cerrando, también si el panel ya no está ───────

#: Cómo se llena y se lee el slot de cada panel de la familia.
_SLOTS: dict[str, tuple[Any, Any]] = {
    "_ritual_feedback_panel": (
        lambda s: s.set(rr.STORE_KEY_RITUAL_FEEDBACK, {"text": "x" * 700, "type": "negative"}),
        lambda s: s.get(rr.STORE_KEY_RITUAL_FEEDBACK),
    ),
    "_ritual_preflight_panel": (
        lambda s: s.set(rr.STORE_KEY_RITUAL_PREFLIGHT, {"status": "red", "checks": []}),
        lambda s: s.get(rr.STORE_KEY_RITUAL_PREFLIGHT),
    ),
    "_parallax_diagnostico_panel": (
        lambda s: s.set(
            rr.STORE_KEY_DIAGNOSTICO_PARALLAX,
            {"por_pestana": {"t": {"mod": "M", "resultado": {"success": True}}}},
        ),
        lambda s: rr.diagnostico_parallax_de(s, "t"),
    ),
}


def test_los_slots_cubren_a_toda_la_familia() -> None:
    assert set(_SLOTS) == {identidad.split(":")[0] for _, identidad in _FAMILIA_DE_CIERRE}


def _cerrar_de(funcion: ast.FunctionDef, variable: str) -> ast.Lambda:
    manejadores = [
        nodo.args[1]
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "on"
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == variable
        and len(nodo.args) == 2
        and ast.literal_eval(nodo.args[0]) == "click"
        and isinstance(nodo.args[1], ast.Lambda)
    ]
    assert len(manejadores) == 1, f"{funcion.name}:{variable} debe tener un solo handler de click"
    return manejadores[0]


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
async def test_la_x_cierra_el_panel_y_tolera_que_ya_no_este(
    clave: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ejecuta el handler REAL de la × (el lambda del módulo) con el ``refreshable`` REAL y sin ningún
    panel montado, como cuando el operador hace doble clic o el panel ya se re-renderizó vacío.

    El ``refresh`` de NiceGUI corre en una task de fondo. Se esperan las tasks que creó el clic para
    que una excepción ahí haga fallar el test y no quede solo en el log.
    """
    funcion = _censo_de_cierres()[clave]
    nombre_del_panel = funcion.name
    llenar, leer = _SLOTS[nombre_del_panel]
    reset_store_for_tests()
    store = get_store()
    llenar(store)
    assert leer(store), "el slot del panel debía arrancar lleno"
    monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
    entorno = {**vars(fd), "current_tab_id": lambda: "t"}
    cerrar = eval(compile(ast.Expression(_cerrar_de(funcion, clave[1].split(":")[1])), "<x>", "eval"), entorno)  # noqa: S307 (lambda del propio módulo)

    try:
        for _ in range(2):  # el segundo clic encuentra el panel ya cerrado
            antes = set(background_tasks.running_tasks)
            cerrar()
            await asyncio.gather(*(set(background_tasks.running_tasks) - antes))
            assert not leer(store), f"{clave[1]}: la × no vació el slot del panel"
        assert not getattr(fd, nombre_del_panel).targets, "no debería quedar ningún panel montado"
    finally:
        reset_store_for_tests()


# ── la × tampoco sale por ARRIBA: el contenedor que crece hacia arriba está acotado ──


def _llamadas_a(nombre: str) -> list[tuple[ast.Call, dict[ast.AST, ast.AST]]]:
    arbol = ast.parse(_FORGE.read_text(encoding="utf-8"))
    padres = _padres(arbol)
    return [
        (nodo, padres)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre
    ]


def _estilo_de_with(nodo: ast.With) -> tuple[dict[str, str], set[str]] | None:
    _, eslabones = _cadena(nodo.items[0].context_expr)
    estilos = [llamada.args[0] for metodo, llamada in eslabones if metodo == "style" and llamada.args]
    if not estilos or not isinstance(estilos[0], (ast.Constant, ast.JoinedStr)):
        return None
    clases = {
        clase
        for metodo, llamada in eslabones
        if metodo == "classes"
        for arg in llamada.args
        for clase in ast.literal_eval(arg).split()
    }
    return _declaraciones(_texto(estilos[0])), clases


def _ancla_fija(clave: tuple[str, str]) -> tuple[dict[str, str], set[str]]:
    """El contenedor ``position:fixed`` que posiciona la × en pantalla: la fila propia del panel o,
    si no es fija, el ``with`` más cercano que lo envuelve donde se lo llama."""
    funcion = _censo_de_cierres()[clave]
    fila = _fila_de_la_x(funcion, clave[1].split(":")[1])
    if fila.get("position") == "fixed":
        return fila, set()
    llamadas = _llamadas_a(funcion.name)
    assert len(llamadas) == 1, f"{funcion.name} debe montarse en un solo lugar"
    nodo, padres = llamadas[0]
    actual: ast.AST | None = nodo
    while actual is not None:
        actual = padres.get(actual)
        if (
            isinstance(actual, ast.With)
            and (estilo := _estilo_de_with(actual))
            and estilo[0].get("position") == "fixed"
        ):
            return estilo
    raise AssertionError(f"{funcion.name} no está dentro de ningún contenedor position:fixed")


@pytest.mark.parametrize("clave", sorted(_FAMILIA_DE_CIERRE), ids=lambda c: c[1])
def test_la_x_no_sale_por_arriba_del_viewport(clave: tuple[str, str]) -> None:
    """La × está en la esquina SUPERIOR del panel. Un contenedor anclado arriba (``top``) la deja
    siempre a la vista. Uno anclado abajo (``bottom``) crece hacia arriba. Al partir las palabras
    largas, el aviso crece en alto, y en Chromium, con 360×640, la × quedaba en y=-172. Por eso el
    contenedor anclado abajo tiene que estar acotado al viewport y scrollear."""
    estilo, clases = _ancla_fija(clave)
    if "top" in estilo:
        assert "bottom" not in estilo, f"{clave[1]}: anclado arriba y abajo a la vez"
        return
    assert "bottom" in estilo, f"{clave[1]}: el contenedor fijo no declara ni top ni bottom"
    assert "100vh" in estilo.get("max-height", ""), f"{clave[1]}: la pila que crece hacia arriba no tiene tope"
    assert estilo.get("overflow-y") in {"auto", "scroll"}, f"{clave[1]}: la pila acotada no scrollea"
    assert "sc-scroll" in clases, f"{clave[1]}: la pila scrolleable usa la scrollbar del tema"


def test_la_x_queda_pegada_arriba_mientras_se_scrollea_el_aviso() -> None:
    """Si un aviso es más alto que la pila, el operador scrollea para leerlo. ``position:sticky`` deja
    la × en el borde superior de la pila mientras el aviso siga a la vista."""
    regla = _regla_css(".sc-dismiss")
    assert regla.get("position") == "sticky"
    assert regla.get("top") == "0"
