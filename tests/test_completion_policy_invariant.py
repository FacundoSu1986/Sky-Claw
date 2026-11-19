"""Ancla de la política de cierre y Semáforo Anti-Mejora Infinita.

Por qué existe: la política vive en dos lugares (`AGENTS.md`, que es la fuente canónica, y
`docs/agents/completion_policy.md`, que la desarrolla) y el modo de falla documentado de este
repo es que una regla normativa sin gate envejece en silencio. Estas anclas cubren las formas
de degradación que sí son mecánicamente detectables:

1. un color, un estado final, un criterio de prioridad o una condición de reapertura que
   **desaparece o cambia** — el conjunto se congela por igualdad literal, no por pertenencia
   (mismo criterio que `RITUAL_TOOL_MAP == {...}`);
2. las dos copias que **divergen**: ambas se comparan contra las mismas constantes de abajo;
3. una referencia que **deja de existir**: enlaces relativos, rutas citadas y el índice que
   hace descubrible al documento.

Lo que estas anclas **no** cubren: que un agente clasifique bien un hallazgo o que un cierre
concreto respete el presupuesto. Eso lo revisa el propietario sobre el cuerpo del PR; no hay
forma de verificarlo desde un test sin acceso a él. Un test verde sobre este archivo no dice
nada sobre el próximo cierre, y el documento lo declara en su §8 (también se ancla).
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
AGENTS = RAIZ / "AGENTS.md"
POLITICA = RAIZ / "docs" / "agents" / "completion_policy.md"
INDICE = RAIZ / "docs" / "agents" / "README.md"

TITULO_DE_SECCION = "## Política de cierre y Semáforo Anti-Mejora Infinita"
RUTA_DE_LA_POLITICA = "docs/agents/completion_policy.md"

#: Semáforo: (emoji, nombre, recomendación). Congelado por igualdad. Se escribe con escapes
#: Unicode para que ni la fuente ni los mensajes de fallo dependan de la codificación de la
#: consola (en Windows puede ser cp1252).
SEMAFORO: tuple[tuple[str, str, str], ...] = (
    ("\U0001f534", "ROJO", "ALTA"),
    ("\U0001f7e1", "AMARILLO", "MEDIA"),
    ("\U0001f7e2", "VERDE", "BAJA"),
)

#: Estados finales únicos. Un estado nuevo, o uno borrado, rompe el ancla: obliga a decidir
#: si el cierre puede terminar en un estado que la política no contempla.
ESTADOS_FINALES: tuple[str, ...] = ("DONE", "DONE_WITH_LIMITATIONS", "BLOCKED", "OUT_OF_SCOPE")

#: Orden de prioridad, de mayor a menor. Se compara tal cual aparece en los dos documentos.
PRIORIDAD: tuple[str, ...] = (
    "seguridad",
    "integridad de datos",
    "correctitud (incluidos hermanos del mismo cambio y fail-closed)",
    "tests/anclas AST",
    "mantenibilidad",
    "rendimiento",
)

#: Únicas causas de reapertura de un cierre.
REAPERTURA: tuple[str, ...] = (
    "regresión reproducida",
    "defecto P0/P1 demostrado",
    "incumplimiento del contrato original",
    "nueva orden explícita del propietario",
)

#: Método de verificación de hallazgos ajenos, en orden.
METODO = "CLAIM → EXTRACT → DOUBT → RECONCILE"

#: Prefijos de ruta que los documentos citan y que tienen que existir en el árbol.
PREFIJOS_DE_RUTA = ("sky_claw/", "tests/", "docs/", "scripts/", ".github/")

_ENLACE_MARKDOWN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ENTRE_COMILLAS_INVERTIDAS = re.compile(r"`([^`\n]+)`")
_ENCABEZADO_DE_SECCION = re.compile(r"^## ", re.MULTILINE)


def _leer(ruta: Path) -> str:
    """Contenido con fin de línea normalizado.

    El repo corre con `core.autocrlf=true` y `.gitattributes` no fija `*.md`, así que el
    archivo llega con CRLF en Windows y LF en CI. Sin normalizar, cualquier patrón que cruce
    líneas matchea en un runner y no en el otro.
    """
    return ruta.read_text(encoding="utf-8").replace("\r\n", "\n")


def _seccion_de_agents() -> str:
    """Cuerpo de la sección de `AGENTS.md`, acotado al próximo encabezado `## `."""
    texto = _leer(AGENTS)
    assert texto.count(TITULO_DE_SECCION) == 1, (
        f"AGENTS.md debe tener exactamente una sección {ascii(TITULO_DE_SECCION)}; "
        f"tiene {texto.count(TITULO_DE_SECCION)}"
    )
    inicio = texto.index(TITULO_DE_SECCION) + len(TITULO_DE_SECCION)
    cierre = _ENCABEZADO_DE_SECCION.search(texto, inicio)
    return texto[inicio : cierre.start() if cierre else len(texto)]


def _destinos_de_enlaces(texto: str) -> list[str]:
    """Destinos de `[texto](destino)`, sin los bloques de código."""
    sin_bloques = re.sub(r"```.*?```", "", texto, flags=re.DOTALL)
    return [destino.strip() for destino in _ENLACE_MARKDOWN.findall(sin_bloques)]


def _rutas_citadas(texto: str) -> set[str]:
    """Rutas del repo mencionadas entre comillas invertidas."""
    rutas: set[str] = set()
    for token in _ENTRE_COMILLAS_INVERTIDAS.findall(texto):
        candidata = token.split("::", 1)[0].strip()
        if not candidata.startswith(PREFIJOS_DE_RUTA):
            continue
        if "*" in candidata or candidata.endswith("(") or " " in candidata:
            continue
        rutas.add(candidata)
    return rutas


_EMOJI = r"(\U0001f534|\U0001f7e1|\U0001f7e2)"

#: `AGENTS.md`: «🔴 ROJO (Recomendación ALTA — ...)».
_SEMAFORO_EN_LISTA = re.compile(_EMOJI + r"\s+(ROJO|AMARILLO|VERDE)\s*\(Recomendación\s+(ALTA|MEDIA|BAJA)\b")

#: Documento: fila de tabla «| 🔴 ROJO | ALTA — ... |».
_SEMAFORO_EN_TABLA = re.compile(
    r"^\|\s*" + _EMOJI + r"\s+(ROJO|AMARILLO|VERDE)\s*\|\s*(ALTA|MEDIA|BAJA)\b", re.MULTILINE
)


def _semaforo_de(texto: str, patron: re.Pattern[str]) -> tuple[tuple[str, str, str], ...]:
    """(emoji, nombre, recomendación) en el orden en que aparecen en el texto."""
    return tuple((m.group(1), m.group(2), m.group(3)) for m in patron.finditer(texto))


def _prioridad_de(texto: str) -> tuple[str, ...]:
    """Cadena `a > b > c` de la prioridad, partida en sus criterios."""
    # El documento la cita en un bloque `>` que puede partirse en varias líneas: se une
    # antes de buscar, para que reajustar el ancho del párrafo no rompa el ancla.
    plano = re.sub(r"\s*\n\s*>?\s*", " ", texto)
    coincidencias = re.findall(r"seguridad\s*>.*?rendimiento", plano)
    assert len(coincidencias) == 1, f"se esperaba una sola cadena de prioridad, hay {len(coincidencias)}"
    return tuple(parte.strip() for parte in coincidencias[0].split(">"))


def _estados_de(linea_o_texto: str) -> tuple[str, ...]:
    """Estados finales (identificadores en mayúsculas entre comillas invertidas)."""
    return tuple(re.findall(r"`([A-Z][A-Z_]+)`", linea_o_texto))


def test_agents_md_declara_la_seccion_y_apunta_al_documento() -> None:
    """La sección existe, una sola vez, y enlaza al documento que la desarrolla."""
    seccion = _seccion_de_agents()

    assert f"({RUTA_DE_LA_POLITICA})" in seccion, "la sección no enlaza a la política desarrollada"
    assert (RAIZ / RUTA_DE_LA_POLITICA).is_file(), f"falta {RUTA_DE_LA_POLITICA}"
    assert "Antes de implementar y antes de declarar una tarea cerrada" in seccion, (
        "la sección ya no obliga a leer la política antes de implementar y antes de cerrar"
    )


def test_el_semaforo_esta_congelado_en_las_dos_copias() -> None:
    """Rojo/amarillo/verde con su recomendación: igualdad literal, en `AGENTS.md` y en el doc."""
    en_agents = _semaforo_de(_seccion_de_agents(), _SEMAFORO_EN_LISTA)
    en_politica = _semaforo_de(_leer(POLITICA), _SEMAFORO_EN_TABLA)

    assert en_agents == SEMAFORO, f"semáforo de AGENTS.md cambió: {ascii(en_agents)}"
    assert en_politica == SEMAFORO, f"semáforo del documento cambió: {ascii(en_politica)}"


def test_las_consecuencias_de_cada_color_siguen_declaradas() -> None:
    """El color solo sirve si fija qué hacer: el rojo bloquea `DONE`, el verde no commitea."""
    seccion = _seccion_de_agents()

    assert "Prohibido declarar `DONE`" in seccion, "el ROJO ya no prohíbe declarar DONE"
    assert "NO commitear" in seccion, "el VERDE ya no prohíbe commitear"
    assert "sin abrir otro PR ni ampliar el actual sin autorización" in seccion, (
        "el AMARILLO ya no prohíbe ampliar el PR sin autorización"
    )
    politica = _leer(POLITICA)
    assert "Prohibido declarar `DONE`" in politica
    assert "no commitear" in politica.lower()


def test_los_estados_finales_estan_congelados_en_las_dos_copias() -> None:
    """Cuatro estados, ni uno más ni uno menos, y los mismos en las dos copias."""
    linea = next(
        (linea for linea in _seccion_de_agents().splitlines() if "Estados finales" in linea),
        "",
    )
    assert _estados_de(linea) == ESTADOS_FINALES, f"AGENTS.md: {_estados_de(linea)}"

    politica = _leer(POLITICA)
    inicio = politica.index("## 6. Estados finales")
    cierre = _ENCABEZADO_DE_SECCION.search(politica, inicio + 3)
    bloque = politica[inicio : cierre.start() if cierre else len(politica)]
    filas = [fila for fila in bloque.splitlines() if fila.startswith("| `")]
    en_tabla = tuple(re.findall(r"^\| `([A-Z_]+)`", "\n".join(filas), flags=re.MULTILINE))
    assert en_tabla == ESTADOS_FINALES, f"documento: {en_tabla}"


def test_la_prioridad_esta_congelada_en_las_dos_copias() -> None:
    """El orden de prioridad es lo que desempata; no puede reordenarse en silencio."""
    assert _prioridad_de(_seccion_de_agents()) == PRIORIDAD
    assert _prioridad_de(_leer(POLITICA)) == PRIORIDAD


def test_la_reapertura_esta_congelada_en_las_dos_copias() -> None:
    """Un cierre solo se reabre por las causas declaradas, y ninguna otra se agrega sola."""
    seccion = _seccion_de_agents().lower()
    # Sin el énfasis, para que `**P0/P1 demostrado**` no parta la frase buscada.
    politica = _leer(POLITICA).replace("**", "").lower()

    for causa in (c.lower() for c in REAPERTURA):
        assert causa in seccion, f"AGENTS.md ya no declara la causa de reapertura: {causa}"
        assert causa in politica, f"el documento ya no declara la causa de reapertura: {causa}"

    # El documento las enumera como lista numerada; una causa extra rompe el ancla.
    inicio = politica.index("## 7. reapertura")
    cierre = _ENCABEZADO_DE_SECCION.search(politica, inicio + 3)
    bloque = politica[inicio : cierre.start() if cierre else len(politica)]
    items = re.findall(r"^\d+\.\s", bloque, flags=re.MULTILINE)
    assert len(items) == len(REAPERTURA), f"el documento enumera {len(items)} causas, se esperaban {len(REAPERTURA)}"


def test_el_metodo_y_el_presupuesto_siguen_declarados() -> None:
    """Verificar antes de tocar y detenerse tras una ronda: lo que evita la mejora infinita."""
    seccion = _seccion_de_agents()
    politica = _leer(POLITICA)

    assert METODO in seccion, "AGENTS.md ya no exige CLAIM → EXTRACT → DOUBT → RECONCILE"
    assert METODO in politica, "el documento ya no exige CLAIM → EXTRACT → DOUBT → RECONCILE"
    assert "1 revisión adversarial acotada al cambio + 1 ronda de corrección y revalidación" in seccion
    assert "detener y escalar" in seccion.lower()


def test_el_documento_declara_los_metadatos_minimos() -> None:
    """`docs/documentation/standards.md` exige audiencia, estado, fuentes y SHA."""
    assert POLITICA.is_file(), f"falta {RUTA_DE_LA_POLITICA}"
    texto = _leer(POLITICA)

    for metadato in ("**Audiencia:**", "**Estado:**", "**Fuentes canónicas:**", "**Última verificación:**"):
        assert metadato in texto, f"el documento no declara {metadato}"


def test_el_documento_declara_lo_que_el_repo_no_verifica() -> None:
    """Declarar el límite es parte del contrato; sin esto un verde sobre el ancla engaña."""
    texto = _leer(POLITICA)

    assert "## 8. Qué verifica el repo y qué no" in texto
    assert "**No verificado:**" in texto


def test_los_enlaces_y_las_rutas_citadas_existen() -> None:
    """Una referencia rota hace que la política apunte a un documento que ya no está."""
    rotas: list[str] = []
    for ruta in (POLITICA, AGENTS):
        texto = _leer(ruta) if ruta == POLITICA else _seccion_de_agents()
        for destino in _destinos_de_enlaces(texto):
            if destino.startswith(("http://", "https://", "mailto:", "#")):
                continue
            objetivo = (ruta.parent / destino.split("#", 1)[0]).resolve()
            if not objetivo.exists():
                rotas.append(f"{ruta.name}: {destino}")
        for cita in sorted(_rutas_citadas(texto)):
            if not (RAIZ / cita).exists():
                rotas.append(f"{ruta.name}: `{cita}`")

    assert not rotas, f"referencias que no existen: {rotas}"


def test_el_documento_es_descubrible_desde_el_indice_de_agentes() -> None:
    """Sin el índice, solo lo encuentra quien ya sabe que existe."""
    destinos = _destinos_de_enlaces(_leer(INDICE))

    assert "completion_policy.md" in destinos, "docs/agents/README.md no enlaza a completion_policy.md"
