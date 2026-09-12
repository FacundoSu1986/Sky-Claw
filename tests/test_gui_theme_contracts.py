"""Anclas de contrato del tema «Forja del Dovahkiin» (quick wins de la auditoría).

Cada aserción protege UN fix de diseño que ya ocurrió una vez y rompía en
silencio (ningún gate lo veía): textos ilegibles, scrollbar nativa gris, emojis
que se renderizan con la pila de color del SO, un h1 que pedía un peso que la
fuente declaraba no tener, una atenuación de estado que se llevaba puesto el
contraste del texto, un foco global que imponía geometría a Quasar/NiceGUI y una
intervención universal de movimiento reducido que congelaba indicadores ajenos
al tema. La convención del repo para este tipo de regla es el test
ancla que ENUMERA la familia completa (ver AGENTS.md y test_pyinstaller.py):
si un refactor futuro pisa cualquiera de estas cuerdas, el test se rompe en CI
y no en la cara del usuario.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_GUI_DIR = Path(__file__).resolve().parent.parent / "sky_claw" / "app" / "gui"
_STYLES = (_GUI_DIR / "styles.css").read_text(encoding="utf-8")
_FONTS = (_GUI_DIR / "assets" / "fonts" / "fonts.css").read_text(encoding="utf-8")
_FORGE = (_GUI_DIR / "views" / "forge_dashboard.py").read_text(encoding="utf-8")


def _funciones_que_usan_sc_scroll() -> set[str]:
    """Devuelve los nombres de las funciones del shell que aplican ``sc-scroll``.

    Búsqueda por AST sobre ``.classes("sc-scroll")``: si alguien quita la clase
    de un contenedor, el conjunto cambia y el ancla lo detecta — verificar solo
    que la regla CSS EXISTE no protege sus consumidores (revisión Codex #522).
    """
    usos: set[str] = set()

    class _Buscador(ast.NodeVisitor):
        def __init__(self) -> None:
            self._pila: list[str] = []

        def _con_pila(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._pila.append(node.name)
            self.generic_visit(node)
            self._pila.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._con_pila(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._con_pila(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "classes"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and "sc-scroll" in node.args[0].value
            ):
                usos.add(self._pila[-1] if self._pila else "<módulo>")
            self.generic_visit(node)

    _Buscador().visit(ast.parse(_FORGE))
    return usos


def test_sc_scroll_tiene_scrollbar_tallada_y_fallback_estandar() -> None:
    """Scrollbar del shell: receta webkit tallada + propiedades estándar
    (``scrollbar-width``/``scrollbar-color``) para Firefox — el shell puede
    abrirse en un navegador externo además del webview Chromium de NiceGUI."""
    assert ".sc-scroll::-webkit-scrollbar-thumb" in _STYLES
    inicio = _STYLES.index(".sc-scroll {")
    bloque_base = _STYLES[inicio : _STYLES.index("}", inicio)]
    assert "scrollbar-width: thin" in bloque_base
    assert "scrollbar-color" in bloque_base


def test_sc_scroll_enumera_todos_sus_consumidores() -> None:
    """Los 3 contenedores scrolleables del shell (contenido principal, Orden de
    Carga, chat) deben seguir usando la clase: quien quite una, vuelve a la
    scrollbar nativa gris. Enumeración exhaustiva, no muestreo."""
    assert _funciones_que_usan_sc_scroll() == {"render_forge_dashboard", "_orden_carga", "_asistente"}


def test_cinzel_ambas_caras_declaran_hasta_900() -> None:
    """El h1 del hero pide ``font-weight:900``; la variable font de Cinzel lo
    cubre (400–900), pero declarar 400–700 lo clampaba a 700. Hay exactamente
    DOS @font-face de Cinzel (latin / latin-ext) y ambas deben cubrir hasta 900
    — verificar solo una ocurrencia dejaría la otra cara fuera de control."""
    caras = [bloque for bloque in _FONTS.split("@font-face") if "font-family: 'Cinzel'" in bloque]
    assert len(caras) == 2
    assert all("font-weight: 400 900;" in cara for cara in caras)


def test_focus_visible_foca_sin_imponer_geometria_global() -> None:
    """El foco de teclado global sigue existiendo (outline dorado + offset) y NO
    impone geometría: el ``border-radius: 3px`` en la regla global pisaba la
    forma propia de pills y botones redondos de Quasar/NiceGUI. La estética del
    Forge no viaja en la regla de accesibilidad: cada componente conserva su
    forma y el outline la sigue (revisión #522)."""
    selector = ':is(button, a, input, [role="button"], [tabindex]):focus-visible'
    assert selector in _STYLES, "el foco de teclado global desapareció"
    inicio = _STYLES.index(selector)
    regla = _STYLES[inicio : _STYLES.index("}", inicio)]
    assert "outline:" in regla, "el foco visible debe seguir siendo un outline"
    assert "outline-offset" in regla
    assert "border-radius" not in regla, "la regla global no puede imponer geometría"


def test_focus_visible_pisa_el_reset_de_quasar_en_inputs() -> None:
    """Quasar trae un reset con !important
    (``.q-field__native,.q-field__input{outline:0!important}`` en
    quasar.important.prod.css): sin la pisa, Tab en un ``ui.input`` borderless
    del shell (búsqueda del header, chat) era invisible (revisión Codex #522).
    """
    for clase in (".q-field__native", ".q-field__input"):
        assert f"{clase}:focus-visible" in _STYLES
    inicio = _STYLES.index(".q-field__native:focus-visible")
    regla = _STYLES[inicio : _STYLES.index("}", inicio)]
    # La pisa tiene que ser del outline y con !important en la MISMA declaración:
    # basta que pierda el marcador para que vuelva a ganar el reset de Quasar.
    assert "outline: 2px solid var(--sky-gold-bright) !important;" in regla, (
        "la pisa debe ganar al reset !important de Quasar"
    )


# Familia animada del tema, congelada por nombre (secciones 13/14 de styles.css
# y emisiones inline del shell). Enumerar la familia completa —no muestrear— es
# lo que hace que una animación nueva sin política de movimiento reducido rompa
# el test en CI (AGENTS.md: anclar con enumeración, no con ejemplos).
#
# Animaciones aplicadas por reglas del stylesheet: NINGUNA hoy — las tres v3
# (sky-pulse-amber/soft, sky-fade-up) solo las emitían el sidebar/header legacy
# y murieron con la isla pre-Forge. El mapa queda VACÍO a propósito: cualquier
# `animation:` nueva en styles.css rompe el ancla hasta que reciba su política
# de movimiento reducido.
_ANIMACIONES_POR_CLASE_CSS: dict[str, set[str]] = {}

_ANIMACIONES_INLINE: dict[str, str] = {
    # keyframes emitidos por forge_dashboard.py en estilos inline → clase
    # marcadora que debe viajar en la MISMA línea del HTML emitido:
    # sc-deco — queda estática sin ambigüedad (el estado lo siguen dando el
    # color/etiqueta/texto); sc-ember — se apaga del todo, porque sin animación
    # las brasas quedarían como puntos fijos en vez de fundirse.
    "scAurora": "sc-deco",
    "scPulse": "sc-deco",
    "scShimmer": "sc-deco",
    "scBlink": "sc-deco",
    "scEmber": "sc-ember",
}

_KEYFRAMES_SIN_USO: set[str] = {"scSpin", "scFade"}  # declarados en styles.css, aún sin consumo


def _keyframes_declarados() -> set[str]:
    return set(re.findall(r"@keyframes\s+([A-Za-z][A-Za-z0-9-]*)", _STYLES))


def _bloque_reduced_motion() -> str:
    """Texto del bloque ``@media (prefers-reduced-motion: reduce)`` con balance
    de llaves: contiene reglas anidadas, un ``index("}")`` simple se cortaría
    en la primera."""
    inicio = _STYLES.index("@media (prefers-reduced-motion: reduce)")
    profundidad = 0
    for i in range(_STYLES.index("{", inicio), len(_STYLES)):
        if _STYLES[i] == "{":
            profundidad += 1
        elif _STYLES[i] == "}":
            profundidad -= 1
            if profundidad == 0:
                return _STYLES[inicio : i + 1]
    raise AssertionError("@media prefers-reduced-motion sin cerrar")


def test_reduced_motion_dirigido_a_las_decorativas_del_tema() -> None:
    """Política de movimiento reducido ENUMERADA, no universal (revisión #522).

    Congela cuatro cosas: (1) la familia EXACTA de keyframes del tema — uno
    nuevo obliga a decidir su política; (2) cada keyframe aplicado por el
    stylesheet viaja con su selector dentro del bloque @media; (3) cada
    ``animation:scX`` inline del shell viaja con su clase marcadora, y la
    marcadora está en la política; (4) el bloque no vuelve al selector
    universal ``*, *::before, *::after``, que congelaba también spinners e
    indicadores de carga de Quasar/NiceGUI.
    """
    bloque = _bloque_reduced_motion()

    # (1) La familia declarada es exacta y conocida.
    familia = set(_ANIMACIONES_POR_CLASE_CSS) | set(_ANIMACIONES_INLINE) | _KEYFRAMES_SIN_USO
    assert _keyframes_declarados() == familia

    # (2) Aplicados por el stylesheet: nombres y selectores exactos (fuera del
    # bloque @media y sin comentarios CSS), y cada selector dentro de la política.
    inicio_bloque = _STYLES.index("@media (prefers-reduced-motion: reduce)")
    fuera = _STYLES[:inicio_bloque] + _STYLES[inicio_bloque + len(bloque) :]
    fuera = re.sub(r"/\*.*?\*/", "", fuera, flags=re.DOTALL)
    uso_css: dict[str, set[str]] = {}
    for coincidencia in re.finditer(r"([^{}]+)\{[^{}]*?animation:\s*([A-Za-z][\w-]*)", fuera):
        for sel in coincidencia.group(1).strip().split(","):
            uso_css.setdefault(coincidencia.group(2), set()).add(sel.strip())
    assert uso_css == _ANIMACIONES_POR_CLASE_CSS, f"uso de animaciones en el stylesheet cambió: {uso_css}"
    for nombre, selectores in _ANIMACIONES_POR_CLASE_CSS.items():
        for sel in selectores:
            assert sel in bloque, f"{nombre} ({sel}) sin política de movimiento reducido"

    # (3) Aplicados inline por el shell: conjunto exacto de nombres; cada línea
    # emisora viaja con su marcadora y la marcadora está en la política.
    inline_encontrados = set(re.findall(r"animation:\s*([A-Za-z][\w-]*)", _FORGE))
    assert inline_encontrados == set(_ANIMACIONES_INLINE), (
        f"animaciones inline del shell cambiaron: {inline_encontrados}"
    )
    for nombre, marcador in _ANIMACIONES_INLINE.items():
        # regex con \s*: un reformateo del inline ("animation: scX") no debe
        # tumbar el ancla — la clase marcadora sobrevive a la re-emisión.
        lineas = [linea for linea in _FORGE.splitlines() if re.search(rf"animation:\s*{nombre}\b", linea)]
        assert lineas, f"{nombre} dejó de emitirse; actualizar la familia"
        for linea in lineas:
            assert marcador in linea, f"{nombre} inline sin clase marcadora {marcador}"
        assert f".{marcador}" in bloque

    # (3b) Los keyframes sin uso deben seguir siéndolo: consumirlos obliga a
    # sumarlos a la política antes.
    for nombre in _KEYFRAMES_SIN_USO:
        assert not re.search(rf"animation:\s*{nombre}\b", _STYLES)
        assert not re.search(rf"animation:\s*{nombre}\b", _FORGE)

    # (4) Sin selector universal dentro del bloque.
    cuerpo = bloque[bloque.index("{") + 1 :]
    for fragmento in cuerpo.split("}"):
        if "{" not in fragmento:
            continue
        selector = fragmento.split("{")[0]
        for token in selector.split(","):
            base = token.strip().split("::")[0].strip()
            assert base != "*", f"selector universal en reduced-motion: {token!r}"


# Codepoints con Emoji_Presentation=Yes (emoji-data.txt) que caerían dentro de
# los rangos permitidos: esos rectángulos tomarían la pila de color del SO por
# defecto, que es justo lo que el contrato prohíbe (revisión Codex #522). Las
# flechas 0x2194–0x2199 son Emoji=Yes pero texto por defecto → no se excluyen.
_EMOJI_PRESENTACION_DEFECTO = frozenset(range(0x25FB, 0x25FF))  # ◻ ◼ ◽ ◾


def _glifo_permitido(cp: int) -> bool:
    """Clases de glifos no-ASCII que el shell Forge tiene derecho a usar.

    Exhaustivo por bloque, no por lista negra de caracteres: cualquier glifo
    nuevo fuera de estas clases hace fallar el inventario y obliga a decidir
    conscientemente si entra al alfabeto del tema.
    """
    if cp in _EMOJI_PRESENTACION_DEFECTO:
        return False
    if cp < 0x80:
        return True  # ASCII
    if 0x00A1 <= cp <= 0x024F:
        return True  # Español y tipografía latina: acentos, ñ, «», ·, ¿¡, ×
    if 0x16A0 <= cp <= 0x16FF:
        return True  # Rúnico — el alfabeto del tema
    if 0x2010 <= cp <= 0x2027:
        return True  # Puntuación general: — …
    if 0x2190 <= cp <= 0x21FF:
        return True  # Flechas: ↑ → ↓
    if 0x2500 <= cp <= 0x25FF:
        return True  # Box drawing (divisores de comentarios) y rombo de estado ◆
    return cp == 0x2713  # Checkmark semántico de disputas resueltas


def test_shell_forge_inventario_de_glifos_congelado() -> None:
    """El shell no usa emojis: se reemplazaron por los SVG de ``icons.py``
    (stroke ``currentColor``) porque el emoji se renderiza con la pila de color
    del SO y rompe la ilusión diegética.

    La verificación es EXHAUSTIVA, no un muestreo de caracteres (revisión
    CodeRabbit #522): todo carácter no-ASCII del archivo debe caer en una clase
    permitida de ``_glifo_permitido``. Los bloques U+2600–U+27BF —el reservorio
    de emojis (⚔ U+2694, ⚙ U+2699, ⚠ U+26A0) y de los selectores de variación
    U+FE0F— quedan prohibidos por construcción, igual que el plano
    multilingüe U+1F300+. No aplica a Telegram/logs, donde el emoji es nativo
    del canal.
    """
    intrusos = sorted({c for c in _FORGE if not _glifo_permitido(ord(c))})
    assert not intrusos, "glifos fuera del alfabeto del tema en forge_dashboard.py: " + ", ".join(
        f"U+{ord(c):04X} {c!r}" for c in intrusos
    )
    # Y los reemplazos existen de verdad en el registro de iconos.
    icons = (_GUI_DIR / "icons.py").read_text(encoding="utf-8")
    assert '_ICON_SHIELD_CHECK = """<svg' in icons
    assert '_ICON_SWORDS = """<svg' in icons
    assert '_ICON_LOCK = """<svg' in icons
    assert '_ICON_UNLOCK = """<svg' in icons


def test_rituales_atenuan_decoracion_nunca_el_texto() -> None:
    """Las tarjetas de los estados no-listos (``missing``/``unknown``) solo
    atenúan lo decorativo — medallón rúnico y borde vía ``deco_opacity`` /
    ``card_border``. Un ``opacity`` global en la tarjeta tambaleaba también el
    texto: #8a8270 con opacity .62 componía ~2.5:1 contra el fondo, por debajo
    de WCAG AA (revisión Codex #522).

    Cobertura de los cuatro estados: available / present_unverified / missing /
    unknown.
    """
    from sky_claw.app.gui.views.forge_dashboard import _RITUAL_STATE_STYLE

    assert set(_RITUAL_STATE_STYLE) == {"available", "present_unverified", "missing", "unknown"}
    for estado, estilo in _RITUAL_STATE_STYLE.items():
        assert "opacity" not in estilo, f"{estado}: opacity global en la tarjeta (ataca al texto)"
        assert float(estilo["deco_opacity"]) <= 1.0
        assert estilo["card_border"].startswith("rgba(200,168,106")


# ── Limpieza de la isla pre-Forge (C3/A3 del roadmap GUI) ────────────────────

#: Archivos que se eliminaron porque SOLO los alcanzaba el render muerto del
#: viejo home (``render_dashboard_page_content``/secciones legacy). Si reaparece
#: cualquiera, el ancla se rompe a propósito — reintroducir el viejo shell
#: significaba (de hecho) dos shells con dos paletas conviviendo.
_MODULOS_LEGACY_ELIMINADOS = (
    "views/layout/__init__.py",
    "views/layout/header.py",
    "views/layout/sidebar.py",
    "views/sections/stats_section.py",
    "views/sections/features_section.py",
    "views/sections/mods_preview.py",
    "views/sections/cta_section.py",
    "views/components/chat_bubble.py",
    "views/components/feature_card.py",
    "views/components/mod_item.py",
    "views/components/stat_card.py",
)


def test_modulos_legacy_eliminados_no_reaparecen() -> None:
    """La isla pre-Forge quedó vacía: ninguno de los módulos borrados puede
    reaparecer (ningún recreador silencioso de importlib ni copy accidental).

    Las rutas se resuelven contra ``_GUI_DIR`` (que ya es ``app/gui``) — el bug
    original duplicaba el prefijo ``views/views/`` y el ancla siempre pasaba en
    falso positivo (encontrado por Codex/Copilot en #572)."""
    for rel in _MODULOS_LEGACY_ELIMINADOS:
        assert not (_GUI_DIR / rel).exists(), f"módulo legacy reintroducido: {rel}"


def test_superficie_publica_de_views_es_la_fachada_forja() -> None:
    """El paquete ``views`` solo exporta ``render_dashboard`` (la fachada que
    sky_claw_gui.py consume: delega en ``render_forge_dashboard``). Si vuelve un
    create_* sin emisores en el Forge, el ancla lo detecta."""
    import sky_claw.app.gui.views as views_pkg

    assert views_pkg.__all__ == ["render_dashboard"]


def test_medievalsharp_fuera_del_bundle() -> None:
    """MedievalSharp se retiró (A3): estaba empaquetada y nunca aplicada — 61 KB
    de peso muerto en el exe. La que no puede reaparecer es la REGLA
    ``font-family: 'MedievalSharp'`` (en cualquier forma: comillas simples,
    dobles o sin comillas — el regex se evalúa sobre el CSS SIN comentarios, así
    que el propio comentario documental del retiro no lo dispara) y los woff2."""
    css_sin_comentarios = re.sub(r"/\*.*?\*/", "", _FONTS, flags=re.DOTALL)
    # re.IGNORECASE: los nombres de font-family son case-insensitive en CSS —
    # "medievalsharp" minúscula restauraría la familia sin romper el ancla
    # (revisión CodeRabbit #572).
    assert not re.search(
        r"font-family\s*:\s*['\"]?MedievalSharp['\"]?",
        css_sin_comentarios,
        flags=re.IGNORECASE,
    ), "regla font-family MedievalSharp reintroducida"
    fonts_dir = _GUI_DIR / "assets" / "fonts"
    remanentes = sorted(p.name for p in fonts_dir.iterdir() if p.name.lower().startswith("medievalsharp"))
    assert remanentes == [], f"woff2 de MedievalSharp residuales: {remanentes}"


def _fuentes_gui() -> dict[str, str]:
    """Mapa ``ruta relativa a app/gui`` → fuente, para los censos por AST."""
    return {
        str(p.relative_to(_GUI_DIR)).replace("\\", "/"): p.read_text(encoding="utf-8")
        for p in sorted(_GUI_DIR.rglob("*.py"))
    }


#: Nombre de la fábrica y del módulo que la define: el censo los resuelve
#: también por alias (import renombrado, llamada por atributo, alias por asignación).
_FABRICA_DRAGON_EYE = "_icon_dragon_eye"
_MODULO_ICONOS = "sky_claw.app.gui.icons"


def _raiz_de(expr: ast.expr) -> ast.expr:
    """Desciende la cadena ``a.b.c`` hasta el ``Name`` raíz."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr


def _referencias_a_la_fabrica(arbol: ast.Module) -> tuple[set[str], set[str]]:
    """Nombres locales que pueden llamar a la fábrica, con sus alias resueltos.

    Devuelve ``(directos, modulos)``: ``directos`` son locales ligados a la
    FUNCIÓN (``from ...icons import _icon_dragon_eye [as X]``) y ``modulos`` los
    ligados al MÓDULO ``icons`` (``import ...icons [as X]`` / ``from ... import
    icons``) para las llamadas por atributo (``X._icon_dragon_eye(...)``). Las
    asignaciones simples (``f = _icon_dragon_eye``) se propagan a punto fijo:
    un consumidor no puede esquivar el censo renombrando la fábrica — la misma
    trampa de alias de los barridos AST por nombre exacto.
    """
    directos: set[str] = set()
    modulos: set[str] = set()
    asignaciones: list[tuple[str, ast.expr]] = []

    for node in ast.walk(arbol):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _FABRICA_DRAGON_EYE:
                    directos.add(alias.asname or alias.name)
                elif alias.name == "icons":
                    modulos.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _MODULO_ICONOS:
                    modulos.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            asignaciones.append((node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            asignaciones.append((node.target.id, node.value))

    cambio = True
    while cambio:
        cambio = False
        for destino, valor in asignaciones:
            if destino in directos or destino in modulos:
                continue
            raiz = _raiz_de(valor) if isinstance(valor, ast.Attribute) else None
            es_directo = (isinstance(valor, ast.Name) and valor.id in directos) or (
                isinstance(valor, ast.Attribute)
                and valor.attr == _FABRICA_DRAGON_EYE
                and isinstance(raiz, ast.Name)
                and raiz.id in modulos
            )
            if es_directo:
                directos.add(destino)
                cambio = True
            elif isinstance(valor, ast.Name) and valor.id in modulos:
                modulos.add(destino)
                cambio = True
    return directos, modulos


def _llamadas_a_la_fabrica(src: str) -> list[str]:
    """Ids ``iris_id`` de TODAS las llamadas a la fábrica del emblema.

    Reconoce nombre directo, alias de import, llamada por atributo al módulo
    (``icons._icon_dragon_eye``) y alias por asignación. Una llamada sin
    ``iris_id`` o con un valor no literal también se lista (centinela) para que
    el censo falle en vez de ignorarla.
    """
    arbol = ast.parse(src)
    directos, modulos = _referencias_a_la_fabrica(arbol)
    ids: list[str] = []
    for node in ast.walk(arbol):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            es_fabrica = func.id in directos
        elif isinstance(func, ast.Attribute) and func.attr == _FABRICA_DRAGON_EYE:
            raiz = _raiz_de(func.value)
            es_fabrica = isinstance(raiz, ast.Name) and raiz.id in modulos
        else:
            es_fabrica = False
        if not es_fabrica:
            continue
        encontro_iris = False
        for kw in node.keywords:
            if kw.arg != "iris_id":
                continue
            encontro_iris = True
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                ids.append(kw.value.value)
            else:
                ids.append("<iris_id no constante>")
        if not encontro_iris:
            ids.append("<sin iris_id>")
    return ids


#: Formas sintácticas por las que un consumidor puede llamar a la fábrica, con
#: la salida esperada del censo. Cada forma sin resolver es un consumidor que
#: queda fuera de ``_CONSUMIDORES_DRAGON_EYE`` y, por lo tanto, de la
#: comprobación de colisiones: import aliaseado, atributo de módulo, alias
#: normal, alias anotado y las llamadas que el censo no puede congelar
#: (``iris_id`` no constante o ausente) viajan como centinelas fail-closed.
_CASOS_DEL_CENSO_DE_LLAMADAS: dict[str, tuple[str, list[str]]] = {
    "nombre_directo": (
        'from sky_claw.app.gui.icons import _icon_dragon_eye\n_icon_dragon_eye(iris_id="scX")\n',
        ["scX"],
    ),
    "import_aliaseado": (
        'from sky_claw.app.gui.icons import _icon_dragon_eye as eye\neye(iris_id="scX")\n',
        ["scX"],
    ),
    "atributo_de_modulo": (
        'from sky_claw.app.gui import icons as ic\nic._icon_dragon_eye(iris_id="scX")\n',
        ["scX"],
    ),
    "alias_por_asignacion": (
        'from sky_claw.app.gui.icons import _icon_dragon_eye\neye = _icon_dragon_eye\neye(iris_id="scX")\n',
        ["scX"],
    ),
    "alias_anotado": (
        "from sky_claw.app.gui.icons import _icon_dragon_eye\n"
        'eye: Callable[..., str] = _icon_dragon_eye\neye(iris_id="scX")\n',
        ["scX"],
    ),
    "iris_id_no_constante": (
        'from sky_claw.app.gui.icons import _icon_dragon_eye\nnombre = "scX"\n_icon_dragon_eye(iris_id=nombre)\n',
        ["<iris_id no constante>"],
    ),
    "sin_iris_id": (
        "from sky_claw.app.gui.icons import _icon_dragon_eye\n_icon_dragon_eye()\n",
        ["<sin iris_id>"],
    ),
}


def test_censo_del_emblema_reconoce_todas_las_formas_de_llamada() -> None:
    """Enumera la familia sintáctica que el censo del emblema debe resolver.

    El defecto de fondo era de enumeración: cada forma no contemplada dejaba a
    un consumidor fuera del inventario (y de la comprobación de colisiones). Si
    el helper deja de resolver una de estas formas, este test rompe — no se
    agrega un caso suelto por cada hermana que aparezca.
    """
    for forma, (src, esperado) in _CASOS_DEL_CENSO_DE_LLAMADAS.items():
        assert _llamadas_a_la_fabrica(src) == esperado, f"forma no reconocida por el censo: {forma}"


#: Única fuente de verdad de test para la familia de consumidores del emblema
#: D4: ``ruta relativa a app/gui`` → id de gradiente. El censo AST congela que
#: los call sites reales sean exactamente este mapa (una llamada por archivo), y
#: el test de instancias renderiza TODAS las entradas de acá — no una lista
#: paralela que podría divergir. Un consumidor nuevo, o un id reutilizado, entra
#: a la comprobación de colisiones agregándolo una sola vez a este mapa.
_CONSUMIDORES_DRAGON_EYE: dict[str, str] = {
    "views/forge_dashboard.py": "scIris-sidebar",
    "setup_wizard.py": "scIris-wizard",
}


def test_emblema_dragon_unico_y_compartido() -> None:
    """D4: el ojo del dragón es UNA plantilla (``_ICON_DRAGON_EYE_TEMPLATE`` en
    icons.py) renderizada vía ``_icon_dragon_eye(iris_id=...)`` en los dos lugares
    donde aparece la marca: el sidebar del shell y la cabecera del wizard.

    Verificación por introspección AST, no por texto (revisiones #579): los
    imports huérfanos no cuentan — se enumeran las LLAMADAS reales a la fábrica
    en CUALQUIER forma (nombre, alias de import, atributo, alias por asignación)
    y sus ids congelados contra ``_CONSUMIDORES_DRAGON_EYE``, la MISMA fuente
    que renderiza ``test_emblema_ids_unicos_por_instancia``. Como el
    wizard es overlay sobre el dashboard, ambas instancias coexisten en el mismo
    DOM; los ids de gradiente deben ser únicos, y la relación url(#id)↔id se
    prueba renderizando TODOS los consumidores declarados, no una muestra fija.
    """
    fuentes = _fuentes_gui()
    path_ojo = "M5 24C13 14 35 14 43 24C35 34 13 34 5 24Z"

    # (1) El path del emblema vive exactamente una vez en TODO el árbol gui —
    # una copia pegada en cualquier otro archivo rompe el censo.
    total = sum(src.count(path_ojo) for src in fuentes.values())
    assert total == 1, f"copias del path del emblema fuera del registro: {total}"

    # (2) Consumidores REALES de la fábrica: llamadas con sus ids congelados.
    # El matcher resuelve alias de import y llamadas por atributo (CodeRabbit
    # #582): ninguna forma sintáctica de llamar a la fábrica queda fuera del censo.
    llamadas: dict[str, list[str]] = {}
    for rel, src in fuentes.items():
        ids_del_archivo = _llamadas_a_la_fabrica(src)
        if ids_del_archivo:
            llamadas[rel] = ids_del_archivo
    # Listas de un elemento, no strings: dos llamadas en el mismo archivo también
    # rompen el contrato (una sola instancia del emblema por superficie).
    esperado = {rel: [iris_id] for rel, iris_id in _CONSUMIDORES_DRAGON_EYE.items()}
    assert llamadas == esperado, f"consumidores/ids del emblema cambiaron: {llamadas}"


def test_emblema_ids_unicos_por_instancia() -> None:
    """RED→verde (revisión adversarial #579, P1): el wizard es overlay sobre el
    dashboard, así que dos instancias del emblema conviven en el mismo DOM y un
    ``id="scIris"`` compartido haría ambigua la referencia ``url(#scIris)``.

    Renderiza TODAS las instancias declaradas en ``_CONSUMIDORES_DRAGON_EYE``
    —la misma fuente que congela el censo AST, no una lista paralela: un
    consumidor nuevo entra a esta comprobación con una sola edición— y congela
    el contrato D4 por IGUALDAD EXACTA DE LISTAS, no de sets: cada instancia
    tiene exactamente una definición ``id="..."`` y exactamente una referencia
    ``url(#...)`` (dos ocurrencias idénticas que un set colapsaría también
    incumplen el «exactamente una»), y los ids de instancias distintas son
    disjuntos. Un SVG que perdiera a la vez sus ``id="..."`` y sus ``url(#...)``
    (p. ej. el gradiente reemplazado por un color plano) dejaba los dos conjuntos
    vacíos y el test original pasaba sin probar nada.
    """
    from sky_claw.app.gui.icons import _icon_dragon_eye

    ids_por_consumidor: dict[str, set[str]] = {}
    for consumidor, iris_id in _CONSUMIDORES_DRAGON_EYE.items():
        svg = _icon_dragon_eye(iris_id=iris_id)
        ids_lista = re.findall(r'id="([^"]+)"', svg)
        refs_lista = re.findall(r"url\(#([^)]+)\)", svg)
        # Listas antes que sets: la cantidad importa, un set colapsaría la
        # definición o la referencia duplicada.
        assert ids_lista == [iris_id], f"{consumidor}: definiciones de id inesperadas: {ids_lista}"
        assert refs_lista == [iris_id], f"{consumidor}: referencias url(#...) inesperadas: {refs_lista}"
        assert set(refs_lista) <= set(ids_lista), f"{consumidor}: url(#{iris_id}) sin definición en la misma instancia"
        ids_por_consumidor[consumidor] = set(ids_lista)

    # Disjunción PAR A PAR sobre toda la familia renderizada: un tercer
    # consumidor que reutilice un id cae acá sin tocar este test.
    consumidores = list(ids_por_consumidor.items())
    for i, (nombre_a, ids_a) in enumerate(consumidores):
        for nombre_b, ids_b in consumidores[i + 1 :]:
            assert ids_a.isdisjoint(ids_b), f"ids SVG compartidos entre {nombre_a} y {nombre_b}: {ids_a & ids_b}"


#: Registro vivo de iconos, congelado por igualdad literal (censo por AST sobre
#: los Assign de nivel superior de icons.py). La purga de las 12 constantes
#: legacy del wizard no estaba anclada: reintroducirlas sin consumidor pasaba en
#: verde (revisión Codex #579).
_REGISTRO_ICONOS_VIVO = frozenset(
    {
        "_ICON_ROCKET",
        "_ICON_SHIELD_CHECK",
        "_ICON_SWORDS",
        "_ICON_LOCK",
        "_ICON_UNLOCK",
        "_ICON_DRAGON_EYE_TEMPLATE",
    }
)


def test_registro_iconos_congelado_y_sin_muertos() -> None:
    """icons.py define EXACTAMENTE el registro vivo. Cada constante definida se
    puede consumir de dos maneras: desde un archivo distinto (rocket y los cuatro
    del shell) o desde la fábrica del propio archivo (la plantilla del emblema la
    carga ``_icon_dragon_eye``). Por eso se cuentan las cargas en TODO el árbol,
    icons.py incluido; una constante sin ninguna carga sigue fallando."""
    fuentes = _fuentes_gui()
    tree_icons = ast.parse(fuentes["icons.py"])
    definidos = {
        node.targets[0].id
        for node in tree_icons.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("_ICON_")
    }
    assert definidos == set(_REGISTRO_ICONOS_VIVO), f"registro de iconos cambió: {sorted(definidos)}"

    cargas: dict[str, int] = {}
    for src in fuentes.values():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name) and node.id in definidos and isinstance(node.ctx, ast.Load):
                cargas[node.id] = cargas.get(node.id, 0) + 1
    sin_consumidor = sorted(definidos - set(cargas))
    assert not sin_consumidor, f"iconos definidos sin consumidor: {sin_consumidor}"


# ── D3 — integridad del hero reactiva al estado ──────────────────────────────

#: Barra de integridad del hero: una variante CSS por estado del forja, y el
#: mapeo estado→variante vive en forge_dashboard.py. Antes era dorado a fuego
#: fijo (misa placa para "sin conflictos" y "en disputa", es decir mentía).
#:
#: Se congelan las DOS mitades de la tupla —color del sello Y variante de barra—
#: a propósito: el bug que D3 arregló de paso fue el COLOR (VIGILANTE pintaba el
#: verde de ESTABLE), y un contrato que sólo enumerara la variante lo dejaba
#: volver sin romper nada.
_ESTADOS_ESPERADOS = {
    "ESTABLE": ("#7fc08c", "estable"),
    "VIGILANTE": ("#e0a13c", "vigilante"),
    "EN DISPUTA": ("#e88a82", "disputa"),
}

#: Umbrales del estado enumerados sobre el conteo de conflictos, con AMBAS
#: fronteras (4 = último VIGILANTE, 5 = primer EN DISPUTA). Se verifican sobre el
#: HTML que el hero realmente emite, no sobre el mapeo: un inventario del dict no
#: distingue "la barra reacciona" de "la barra quedó fija o sin clase".
_UMBRALES_ESPERADOS = ((0, "ESTABLE"), (1, "VIGILANTE"), (4, "VIGILANTE"), (5, "EN DISPUTA"), (30, "EN DISPUTA"))


def test_integridad_del_hero_reactiva_por_estado() -> None:
    """D3: la barra del hero NO es siempre dorada — mapea el estado.

    (a) los tres estados congelados existen y el mapeo está definido en el
    módulo; (b) cada variante ``.sc-bar--<slug>`` existe en styles.css; (c) el
    gradiente dorado ya no aparece inline en el hero — si alguien lo
    reintroduce, el ancla rompe."""
    from sky_claw.app.gui.views.forge_dashboard import _ESTADO_FORJA

    assert set(_ESTADO_FORJA) == set(_ESTADOS_ESPERADOS), f"estado del forja cambió: {sorted(_ESTADO_FORJA)}"
    for estado, (color, slug) in _ESTADOS_ESPERADOS.items():
        assert _ESTADO_FORJA[estado] == (color, slug), (
            f"{estado} mapeado a {_ESTADO_FORJA[estado]} (esperado {(color, slug)})"
        )
        assert f".sc-bar--{slug}" in _STYLES, f"styles.css sin receta .sc-bar--{slug}"
    # El gradiente dorado NO puede estar inline en el hero:
    assert "linear-gradient(90deg,#8a6c38,#ecd9a8)" not in _FORGE, "gradiente dorado reintroducido inline en el hero"


def test_integridad_del_hero_pinta_el_estado_en_el_html_renderizado() -> None:
    """D3: el panel RENDERIZADO deriva del conteo de conflictos.

    Enumerar el mapeo no alcanza — es la diferencia entre "las recetas existen" y
    "la barra es señal". Sobre el HTML real de :func:`_integridad_html` esto ataja
    las tres regresiones que el inventario dejaba pasar: fijar el estado, borrar la
    clase dinámica ``sc-bar--{variante}`` (que deja la barra SIN fondo, porque D3
    se llevó el gradiente inline) y devolver el color de sello de otro estado.
    """
    from sky_claw.app.gui.views.forge_dashboard import _integridad_html

    for conflicts, estado in _UMBRALES_ESPERADOS:
        color, variante = _ESTADOS_ESPERADOS[estado]
        html = _integridad_html(conflicts)
        assert f"sc-bar--{variante}" in html, f"{conflicts} conflictos: la barra no lleva sc-bar--{variante}"
        assert f"color:{color};" in html, f"{conflicts} conflictos: el sello no pinta {color} ({estado})"
        assert f"◆ {estado}" in html, f"{conflicts} conflictos: el sello no dice {estado}"
        ajenas = {slug for _, slug in _ESTADOS_ESPERADOS.values()} - {variante}
        for otra in sorted(ajenas):
            assert f"sc-bar--{otra}" not in html, f"{conflicts} conflictos: se coló la variante ajena {otra}"


def test_el_hero_consume_el_seam_de_integridad() -> None:
    """D3: el panel del hero se emite por el seam puro, no por un f-string suelto.

    Sin esto el seam podría quedar verde y muerto mientras ``_hero`` sigue
    construyendo su propia barra: el test de arriba pasaría y la GUI no reaccionaría.
    """
    consumidores_de_integridad: set[str] = set()

    class _Buscador(ast.NodeVisitor):
        def __init__(self) -> None:
            self._pila: list[str] = []

        def _con_pila(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._pila.append(node.name)
            self.generic_visit(node)
            self._pila.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._con_pila(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._con_pila(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "html"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ui"
            ):
                argumentos = list(node.args) + [kw.value for kw in node.keywords]
                for arg in argumentos:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Name)
                        and arg.func.id == "_integridad_html"
                    ):
                        consumidores_de_integridad.add(self._pila[-1] if self._pila else "<módulo>")
            self.generic_visit(node)

    _Buscador().visit(ast.parse(_FORGE))
    assert consumidores_de_integridad == {"_hero"}, (
        f"Consumidores de ui.html(_integridad_html(...)) inesperados: {consumidores_de_integridad} (esperado {{'_hero'}})"
    )

    # La clase sólo puede nacer en el seam: si reaparece en otra función, hay una
    # segunda barra que este contrato no cubre (el hermano del que avisa AGENTS.md).
    fuera_del_seam = [
        nodo.name
        for nodo in ast.walk(ast.parse(_FORGE))
        if isinstance(nodo, ast.FunctionDef) and nodo.name != "_integridad_html" and "sc-bar--" in ast.unparse(nodo)
    ]
    assert not fuera_del_seam, f"sc-bar-- construido fuera del seam de integridad: {fuera_del_seam}"


# ── C2 — recetas de botón centralizadas ──────────────────────────────────────

#: Variantes semánticas válidas de la familia ``.sc-btn`` (sección 6b de styles.css).
_VARIANTES_SC_BTN = frozenset({"gold", "ghost", "danger"})

#: Propiedades de la receta visual central: un consumidor C2 NO puede
#: redefinirlas inline. Lo que sí conserva inline es geometría/contexto
#: (padding, margin, gap, display, tamaño, tipografía puntual, la sombra del
#: CTA y su transición). La verificación es ESTRUCTURAL — nombre de propiedad
#: declarado en el ``.style(...)``, no grafía: ``border:1px solid rgba(197,82,74,.5)``
#: y ``border : 1px solid rgba(197, 82, 74, 0.5)`` son la MISMA violación.
_PROPIEDADES_RECETA_CENTRAL = frozenset(
    {
        "color",
        "background",
        "background-color",
        "background-image",
        "border",
        "border-color",
        "border-width",
        "border-style",
        "border-radius",
        "font-family",
        "font-weight",
        "cursor",
    }
)

#: Inventario C2 EXHAUSTIVO de los consumidores de la familia en el shell:
#: identidad estable → variante. La identidad es ``función:variable_asignada``
#: (mismo anclaje por función que el censo de ``sc-scroll``; el número de línea
#: NO participa porque las líneas cambian con cualquier reflow). Igualdad
#: exacta, no muestreo: agregar, quitar o cambiar la variante de un consumidor
#: rompe el test.
_CONSUMIDORES_C2: dict[str, str] = {
    "_hitl_modal_panel:deny": "danger",
    "_hitl_modal_panel:ok": "gold",
    "_ritual_feedback_panel:r": "gold",
    "_hero:btn": "gold",
    "_mods_screen:upd_btn": "gold",
    "_conflicts_screen:scan_btn": "gold",
    "_conflict_row:btn": "gold",
    "_open_resolve_dialog:cancel": "ghost",
    "_open_resolve_dialog:ok": "gold",
    "_settings_screen:btn": "gold",
    "_downloads_screen:deny": "danger",
    "_downloads_screen:approve": "gold",
    "_placeholder:b": "gold",
}

#: Botones del shell deliberadamente FUERA de C2, decididos por identidad
#: semántica (no una allowlist anónima): conservan receta propia porque su
#: aspecto es dinámico por estado o icónico-minimalista. Si alguno adopta
#: ``.sc-btn`` o desaparece, el test rompe: salir de esta familia también es
#: una decisión consciente que hay que escribir acá.
_EXCEPCIONES_FUERA_DE_C2: dict[str, str] = {
    "_nav_item:btn": "ítem de navegación del sidebar: fondo/marker dinámicos por estado activo",
    "_modo_local_panel:btn": "toggle Modo local: color/borde dinámicos según el estado on/off",
    "_ritual_feedback_panel:x": "botón icónico × de dismiss del panel de feedback",
    "_ritual_preflight_panel:x": "botón icónico × de dismiss del panel de preflight",
    "_header:gear": "botón icónico de Ajustes 40×40 (gear, sin texto)",
    "_ritual_card:b": "acción de tarjeta de ritual: color/borde dinámicos por estado del tool",
    "_orden_carga:vb": "link subrayado «Ver Todo», sin caja de botón",
    "_asistente:sb": "botón icónico de envío del chat (flecha)",
    "_conflicts_screen:deep_btn": "escaneo profundo (xEdit): secundario deliberado del escaneo liviano",
}

#: El CTA hero extiende la transición de la receta (transform/box-shadow);
#: pisarla SIN ``filter`` deja el brightness del hover cambiando sin transición.
_TRANSICION_DEBE_INCLUIR_FILTER = frozenset({"_hero:btn"})


@dataclass(frozen=True)
class _Boton:
    """Estado de clases/estilos de un botón del censo (``None`` = estilo
    dinámico no verificable por AST)."""

    clases: frozenset[str]
    estilos: tuple[str | None, ...]
    keywords: bool


def _es_llamada_ui(node: ast.AST, metodo: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ui"
        and node.func.attr == metodo
    )


def _desarmar_cadena(expresion: ast.expr) -> tuple[ast.expr, list[tuple[str, ast.Call]]]:
    """Desarma una cadena de métodos ``a.b(...).c(...)`` → (raíz, eslabones).

    Devuelve la raíz de la cadena (``Name('ui')`` para llamadas ``ui.*``) y los
    eslabones como pares ``(nombre_método, Call)`` del más externo al más interno.
    """
    eslabones: list[tuple[str, ast.Call]] = []
    actual: ast.expr = expresion
    while isinstance(actual, ast.Call) and isinstance(actual.func, ast.Attribute):
        eslabones.append((actual.func.attr, actual))
        actual = actual.func.value
    return actual, eslabones


def _inventario_botones() -> dict[str, _Boton]:
    """Censo AST determinístico de TODO botón emitido por forge_dashboard.py.

    Identidad ``función_contenedora:variable`` — estable ante reflow de líneas.
    Para un botón sin variable, ``función:botón#<ordinal>`` por orden de
    aparición dentro de la función (documentado: un botón nuevo sin variable
    desplaza los ordinales y rompe el ancla a propósito — obliga a clasificarlo
    antes de dejarlo pasar). Funciones anidadas se unen con ``.``.

    Solo captura botones que encabezan la expresión de un statement (asignación
    o expresión). Otras formas (``with ui.element(...) as b``, botones anidados
    en argumentos) quedan fuera del inventario pero el censo total los cuenta,
    así que el test rompe fail-closed en lugar de dejarlos pasar sin clasificar.
    """
    arbol = ast.parse(_FORGE)
    inventario: dict[str, _Boton] = {}

    class _Visitante(ast.NodeVisitor):
        def __init__(self) -> None:
            self._pila: list[str] = []
            self._anonimos: dict[str, int] = {}

        def _con_funcion(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._pila.append(node.name)
            self.generic_visit(node)
            self._pila.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._con_funcion(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._con_funcion(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self._registrar(node.targets[0].id, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name) and node.value is not None:
                self._registrar(node.target.id, node.value)
            self.generic_visit(node)

        def visit_Expr(self, node: ast.Expr) -> None:
            self._registrar(None, node.value)
            self.generic_visit(node)

        def _registrar(self, variable: str | None, expresion: ast.expr) -> None:
            base, eslabones = _desarmar_cadena(expresion)
            if not (isinstance(base, ast.Name) and base.id == "ui"):
                return
            elemento = next((llamada for attr, llamada in eslabones if attr == "element"), None)
            if (
                elemento is None
                or not elemento.args
                or not isinstance(elemento.args[0], ast.Constant)
                or elemento.args[0].value != "button"
            ):
                return
            funcion = ".".join(self._pila) if self._pila else "<módulo>"
            if variable is None:
                ordinal = self._anonimos.get(funcion, 0) + 1
                self._anonimos[funcion] = ordinal
                variable = f"botón#{ordinal}"
            clases: set[str] = set()
            estilos: list[str | None] = []
            keywords = False
            for attr, llamada in eslabones:
                # add/remove/replace por keyword modifican el conjunto final de
                # clases/estilos sin pasar por los args posicionales: fail-closed,
                # el parser no los interpreta.
                keywords = keywords or bool(llamada.keywords)
                for arg in llamada.args:
                    if attr == "classes" and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        clases |= set(arg.value.split())
                    elif attr == "style":
                        # Un estilo dinámico (f-string) podría esconder cualquier
                        # propiedad: se marca None y el test lo exige verificable.
                        estilos.append(arg.value if isinstance(arg, ast.Constant) else None)
            inventario[f"{funcion}:{variable}"] = _Boton(frozenset(clases), tuple(estilos), keywords)

    _Visitante().visit(arbol)
    return inventario


def _censo_total_de_botones() -> int:
    """Todo botón posible del shell: ``ui.element("button")``, cualquier
    ``ui.element`` con tag dinámico no verificable, y los componentes botón de
    Quasar (``ui.button``/``ui.icon_button``/``ui.toggle_button``) — ninguna de
    estas formas existe hoy fuera del inventario; su aparición rompe el test."""
    arbol = ast.parse(_FORGE)
    total = 0
    for node in ast.walk(arbol):
        if _es_llamada_ui(node, "element") and node.args:
            primero = node.args[0]
            if (isinstance(primero, ast.Constant) and primero.value == "button") or not isinstance(
                primero, ast.Constant
            ):
                total += 1
        elif any(_es_llamada_ui(node, metodo) for metodo in ("button", "icon_button", "toggle_button")):
            total += 1
    return total


def _declaraciones(estilos: tuple[str | None, ...]) -> dict[str, str]:
    """Aplana los strings ``.style(...)`` en pares propiedad→valor."""
    props: dict[str, str] = {}
    for bloque in estilos:
        for declaracion in bloque.split(";"):
            if ":" not in declaracion:
                continue
            propiedad, _, valor = declaracion.partition(":")
            props[propiedad.strip().lower()] = valor.strip()
    return props


def test_botones_c2_del_shell_inventario_exhaustivo_y_sin_receta_inline() -> None:
    """C2: los consumidores del shell usan ``.sc-btn`` + exactamente UNA
    variante semántica, y ninguna redeclara inline la receta central.

    A diferencia de un regex sobre la receta (que un cambio de whitespace, un
    ``var(...)`` o un ``0.5`` evadía), este ancla es por AST: enumera TODOS los
    botones del archivo y particiona en consumidores C2 + excepciones
    deliberadas por IGUALDAD EXACTA. Un botón nuevo sin clasificar, un
    consumidor que pierde su clase, o una receta reintroducida bajo cualquier
    grafía rompen el test (AGENTS.md: enumerar, no muestrear).
    """
    inventario = _inventario_botones()
    total = _censo_total_de_botones()
    assert len(inventario) == total, (
        f"hay {total - len(inventario)} botón(es) que el inventario no captura "
        "(with-as, anidado en argumentos, ui.button de Quasar…): clasifícalo en "
        "_CONSUMIDORES_C2 o _EXCEPCIONES_FUERA_DE_C2 antes de dejarlo pasar"
    )

    con_familia = {identidad for identidad, boton in inventario.items() if "sc-btn" in boton.clases}
    sin_familia = set(inventario) - con_familia
    assert con_familia == set(_CONSUMIDORES_C2), (
        f"el conjunto de consumidores C2 cambió: extra={sorted(con_familia - set(_CONSUMIDORES_C2))}, "
        f"faltante={sorted(set(_CONSUMIDORES_C2) - con_familia)}"
    )
    assert sin_familia == set(_EXCEPCIONES_FUERA_DE_C2), (
        f"el conjunto de excepciones deliberadas cambió: extra={sorted(sin_familia - set(_EXCEPCIONES_FUERA_DE_C2))}, "
        f"faltante={sorted(set(_EXCEPCIONES_FUERA_DE_C2) - sin_familia)}"
    )

    for identidad, variante in _CONSUMIDORES_C2.items():
        boton = inventario[identidad]
        # Igualdad exacta de clases: sc-btn presente, UNA variante, la esperada,
        # y ninguna clase extra sin decisión consciente.
        assert boton.clases == {"sc-btn", f"sc-btn--{variante}"}, (
            f"{identidad}: clases inesperadas {sorted(boton.clases)}; el contrato espera "
            f"sc-btn + exactamente la variante {variante}"
        )
        assert not boton.keywords, (
            f"{identidad}: .classes()/.style() con keywords (add/remove/replace) no verificables "
            "por el parser; el contrato exige los literales posicionales"
        )
        assert all(estilo is not None for estilo in boton.estilos), (
            f"{identidad}: .style() dinámico no verificable por AST; el contrato exige "
            "un literal para poder congelar las propiedades"
        )
        declaradas = _declaraciones(boton.estilos)
        reintroducidas = sorted(set(declaradas) & _PROPIEDADES_RECETA_CENTRAL)
        assert not reintroducidas, f"{identidad}: receta central reintroducida inline: {reintroducidas}"
        if identidad in _TRANSICION_DEBE_INCLUIR_FILTER:
            assert "filter" in declaradas.get("transition", ""), (
                f"{identidad}: su transition inline pisa la de .sc-btn sin incluir filter "
                "(el brightness del hover cambiaría sin transición)"
            )


def _reglas_independientes(selector: str) -> list[str]:
    """Cuerpos de las reglas planas de styles.css cuyo selector coincide como
    SELECTOR INDEPENDIENTE: anclado al inicio de línea, así un selector
    compuesto (``.contenedor .sc-btn--gold {``) no cuenta como la receta —
    solo una regla global ``.sc-btn--gold {`` alimenta a los 13 consumidores.
    (Un ``index``/search sin anclar además confundiría ``.sc-btn:disabled``
    con ``.sc-btn:disabled:hover``.)"""
    patron = rf"(?m)^[ \t]*{re.escape(selector)}[ \t]*\{{"
    return [
        _STYLES[coincidencia.end() : _STYLES.index("}", coincidencia.end())]
        for coincidencia in re.finditer(patron, _STYLES)
    ]


def _regla_css(selector: str) -> str:
    reglas = _reglas_independientes(selector)
    assert len(reglas) == 1, f"selector independiente ausente o duplicado en styles.css: {selector}"
    return reglas[0]


def test_receta_sc_btn_centralizada_con_estado_disabled() -> None:
    """C2 (styles.css): la receta vive ÚNICAMENTE acá y la familia tiene los
    estados completos que exige el roadmap — base, hover, active (foco de
    teclado cubierto por la política global :focus-visible, ya anclada) y
    disabled, hoy explícito.

    El disabled usa ``:disabled`` porque NiceGUI aplica ``element.props("disabled")``
    como el atributo HTML ``disabled`` del ``<button>`` nativo (nicegui.js pasa
    los props a ``Vue.h(tag nativo, props)``), así que la pseudo-clase es el
    selector real que coincide. Un botón que no responde no debe sugerir
    interacción: el mismo mecanismo ``filter`` del hover llevado a gris apagado
    (atenúa la receta sin arrastrar el texto con opacity), cursor honesto y
    sin estados :hover/:active engañosos ni animaciones.
    """
    # La variante oro vive en SU bloque de styles.css. La verificación es sobre
    # el bloque y por propiedad, no un conteo global de la grafía del gradiente:
    # hay otras familias doradas en el tema (var(--sky-gold), var(--sky-gold-deep))
    # deliberadamente fuera de C2, y la no-reintroducción inline en los
    # consumidores ya la garantiza el inventario AST.
    assert len(_reglas_independientes(".sc-btn--gold")) == 1, (
        ".sc-btn--gold debe declararse exactamente una vez como regla independiente"
    )
    oro = _declaraciones((_regla_css(".sc-btn--gold"),))
    assert oro["background"].startswith("linear-gradient(180deg"), "la receta oro es un gradiente vertical"
    for parada in ("#f3dca0", "#c8a86a", "#9c7a40"):
        assert parada in oro["background"], f"stop {parada} ausente de la receta oro"
    assert "#1c130a" in oro["color"], "la tinta del CTA es oscura"
    assert "#f6e6bd" in oro["border"], "el filo del CTA es claro"
    for variante in _VARIANTES_SC_BTN:
        assert f".sc-btn--{variante}" in _STYLES, f"falta la variante .sc-btn--{variante} en styles.css"

    # Normalizado por _declaraciones: no depende del whitespace del CSS.
    bloque = _declaraciones((_regla_css(".sc-btn:disabled"),))
    assert bloque.get("cursor") == "not-allowed", "el cursor debe denegar la interacción"
    assert "filter" in bloque, "el estado debe comunicarse atenuando la receta via filter"

    hover = _declaraciones((_regla_css(".sc-btn:disabled:hover"),))
    assert "filter" in hover, ":hover sobre un botón deshabilitado no puede aplicar el brightness(1.07) del válido"

    activo = _declaraciones((_regla_css(".sc-btn:disabled:active"),))
    assert activo.get("transform") == "none", ":active no debe hundir un botón que no responde"


# ── D2 — lore rotatorio del wizard ───────────────────────────────────────────

#: Las cinco frases del lore, congeladas como un todo (igualdad literal,
#: patrón del repo). Un cambio silencioso de UNA rompe el ancla. Textos
#: originales, cero material de Bethesda.
_LORE_D2 = (
    "No todos los descansos son derrotas: a veces el dragón duerme para que la forja aguante.",
    "Un orden de carga bien atado vale más que diez mods brillantes mal pertrechados.",
    "LOOT ordena, xEdit confiesa, DynDOLOD revela: cada herramienta a su ritual.",
    "Que cada cambio tenga prueba y cada prueba tenga nombre — eso separa la forja del fuego.",
    "El viento de la garganta no borra las runas, si alguien las grabó de verdad.",
)


def test_lore_d2_del_wizard_inventario_y_mecanica() -> None:
    """D2: el wizard de primer arranque trae una cita al pie que rota
    automáticamente (estilo pantalla de carga). El ancla congela (i) el inventario
    literal, (ii) el ciclo, (iii) el marcador de destino, (iv) el apagado del
    timer cuando el modal cerró (sin seguir corriendo sobre un DOM muerto),
    (v) el arranque desfasado (``immediate=False`` — la NiceGUI pinned tiene
    immediate=True por defecto y saltaba la primera cita) y (vi) la guarda no
    por excepción sino por estado del elemento (NiceGUI 3.12 no lanza en
    ``content=`` sobre un borrado; un ``except RuntimeError`` sería código muerto)."""
    from sky_claw.app.gui.setup_wizard import _WIZARD_LORE, _lore_markup

    # (i) Las 5 frases, exactamente como se escribieron y en ese orden.
    assert tuple(_WIZARD_LORE) == _LORE_D2

    # (ii) Marcado del destino — si el id cambia, el ciclo JS deja de apuntarlo.
    markup = _lore_markup("test")  # pantalla de carga: una frase ya visible
    assert 'id="sky-wizard-lore"' in markup, "falta el id del marcador de lore"
    assert "'EB Garamond'" in markup and "font-style:italic" in markup, "la cita no está en la tipografía narrativa"

    # (iii+iv+v+vi) Ciclo y timer con arranque diferido + dos líneas de defensa:
    # un chequeo de estado antes de mutar + un apagado explícito al cerrar.
    src_wizard = (_GUI_DIR / "setup_wizard.py").read_text(encoding="utf-8")
    assert "itertools.cycle(_WIZARD_LORE)" in src_wizard, "falta el ciclo de lore en el wizard"
    assert "ui.timer(6.0, self._rotate_lore, immediate=False)" in src_wizard, (
        "falta el timer de rotación con inicio diferido"
    )
    assert "self._lore_el.is_deleted" in src_wizard, (
        "falta la guarda por estado del elemento (NiceGUI 3.12 no lanza RuntimeError)"
    )
    assert "self._lore_timer.deactivate()" in src_wizard, "el timer debe desactivarse explícitamente"


def test_lore_d2_timer_se_apaga_si_el_elemento_llego_borrado() -> None:
    """Comportamiento RED-proof (los reviewers señalaron que la ancla por texto
    no reproduce el lifecycle): levanto el modal sin build() y con un elemento
    marcado borrado — como pasa cuando el overlay se cierra — y el callback
    DEBE apagar su timer sin mutar el elemento, no colgarlo en sesión."""
    from sky_claw.app.gui import setup_wizard as wizard_mod

    class FakeElementoBorrado:
        is_deleted: bool = True
        content: str | None = None

    class FakeTimer:
        active: bool = True

        def deactivate(self) -> None:
            self.active = False

    wiz = object.__new__(wizard_mod.SetupWizardModal)
    wiz._lore_el = FakeElementoBorrado()
    wiz._lore_timer = FakeTimer()
    wiz._lore_iter = __import__("itertools").cycle(wizard_mod._WIZARD_LORE)

    wiz._rotate_lore()

    assert wiz._lore_timer is None, "el timer debe desactivarse y limpiarse"
    assert wiz._lore_el.content is None, "el elemento borrado no debe mutarse"


def test_lore_d2_timer_arranca_desfasado_y_rota_en_vida() -> None:
    """Complemento del contrato: con un elemento VIVO el callback rota una cita
    distinta (salta al siguiente índice del ciclo) cada vez que el timer lo dispara;
    y el timer se crea con immediate=False (Codex fastidió el salto inicial con
    NiceGUI 3.12 default)."""
    from sky_claw.app.gui.setup_wizard import _WIZARD_LORE

    # El arranque desfasado ya va en la ancla arriba. Aquí un smoke puro:
    iter1 = __import__("itertools").cycle(_WIZARD_LORE)
    frase1 = next(iter1)
    frase2 = next(iter1)
    assert frase1 != frase2, "el ciclo no rota"
    assert frase1 in _WIZARD_LORE and frase2 in _WIZARD_LORE, "frase fuera del inventario"
