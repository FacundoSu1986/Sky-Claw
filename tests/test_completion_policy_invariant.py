"""Ancla de la política de cierre y Semáforo Anti-Mejora Infinita.

Por qué existe: la política vive en dos lugares (`AGENTS.md`, que es la fuente canónica, y
`docs/agents/completion_policy.md`, que la desarrolla) y el modo de falla documentado de este
repo es que una regla normativa sin gate envejece en silencio. Estas anclas cubren las formas
de degradación que sí son mecánicamente detectables:

1. un color, un estado final, un criterio de prioridad, una condición de reapertura o un
   campo de la tarjeta de cierre que **desaparece o cambia** — el conjunto se congela por
   igualdad literal, no por pertenencia (mismo criterio que `RITUAL_TOOL_MAP == {...}`);
2. las dos copias que **divergen**: ambas se comparan contra las mismas constantes de abajo;
3. una referencia que **deja de existir**: enlaces relativos, rutas citadas, el índice que
   hace descubrible al documento y los hechos de CI que el documento da por ciertos (el job
   `Tests (Pytest)`, el modelo `:free` de Qodo, la zona exenta de `mypy`).

Lo que estas anclas **no** cubren: que un agente clasifique bien un hallazgo, que un cierre
concreto respete el presupuesto o que emita la tarjeta. Eso lo revisa el propietario sobre el
cuerpo del PR; no hay forma de verificarlo desde un test sin acceso a él. Un test verde sobre
este archivo no dice nada sobre el próximo cierre, y el documento lo declara en su §9 (también
se ancla).
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
AGENTS = RAIZ / "AGENTS.md"
POLITICA = RAIZ / "docs" / "agents" / "completion_policy.md"
INDICE = RAIZ / "docs" / "agents" / "README.md"
CI = RAIZ / ".github" / "workflows" / "ci.yml"
QODO = RAIZ / ".github" / "workflows" / "qodo-merge-adversarial.yml"
PYPROJECT = RAIZ / "pyproject.toml"

TITULO_DE_SECCION = "## Política de cierre y Semáforo Anti-Mejora Infinita"
RUTA_DE_LA_POLITICA = "docs/agents/completion_policy.md"

ROJO = "\U0001f534"
AMARILLO = "\U0001f7e1"
VERDE = "\U0001f7e2"

#: Semáforo de `AGENTS.md`: (emoji, nombre, recomendación). Congelado por igualdad. Se escribe
#: con escapes Unicode para que ni la fuente ni los mensajes de fallo dependan de la
#: codificación de la consola (en Windows puede ser cp1252).
SEMAFORO: tuple[tuple[str, str, str], ...] = (
    (ROJO, "ROJO", "ALTA"),
    (AMARILLO, "AMARILLO", "MEDIA"),
    (VERDE, "VERDE", "BAJA"),
)

#: Semáforo del documento: (emoji, nombre, recomendación, severidad). El rojo ocupa dos filas
#: (P0 y P1): la severidad es lo que distingue «contener ya» de «corregir antes de aprobar».
SEMAFORO_DETALLADO: tuple[tuple[str, str, str, str], ...] = (
    (ROJO, "ROJO", "ALTA", "P0"),
    (ROJO, "ROJO", "ALTA", "P1"),
    (AMARILLO, "AMARILLO", "MEDIA", "P2"),
    (VERDE, "VERDE", "BAJA", "P3"),
)

#: Estados finales únicos. Un estado nuevo, o uno borrado, rompe el ancla: obliga a decidir
#: si el cierre puede terminar en un estado que la política no contempla.
ESTADOS_FINALES: tuple[str, ...] = ("DONE", "DONE_WITH_LIMITATIONS", "BLOCKED", "OUT_OF_SCOPE")

#: Color(es) con que cada estado puede cerrar. Es lo que une el estado técnico con el color
#: que el propietario lee: un `BLOCKED` en verde sería un cierre que miente.
COLOR_POR_ESTADO: dict[str, tuple[str, ...]] = {
    "DONE": ("VERDE",),
    "DONE_WITH_LIMITATIONS": ("AMARILLO", "VERDE"),
    "BLOCKED": ("ROJO",),
    "OUT_OF_SCOPE": ("AMARILLO", "VERDE"),
}

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

#: Campos de la tarjeta obligatoria de cierre (§8 del documento), en orden de aparición.
CAMPOS_DE_LA_TARJETA: tuple[str, ...] = (
    "### Semáforo de Cierre:",
    "- **Estado:**",
    "- **Recomendación al propietario:**",
    "- **SHA / Versión verificada:**",
    "- **Honestidad de verificación:**",
    "*Ejecutado:*",
    "*Leído en código:*",
    "*No verificado:*",
    "- **Hallazgos clasificados:**",
    "- **Por qué corresponde detenerse (o seguir) ahora:**",
)

#: Prefijos de ruta que los documentos citan y que tienen que existir en el árbol.
PREFIJOS_DE_RUTA = ("sky_claw/", "tests/", "docs/", "scripts/", ".github/")

_ENLACE_MARKDOWN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ENTRE_COMILLAS_INVERTIDAS = re.compile(r"`([^`\n]+)`")
_ENCABEZADO_DE_SECCION = re.compile(r"^## ", re.MULTILINE)
_EMOJI = r"(\U0001f534|\U0001f7e1|\U0001f7e2)"

#: `AGENTS.md`: «🔴 ROJO (Recomendación ALTA — ...)».
_SEMAFORO_EN_LISTA = re.compile(_EMOJI + r"\s+(ROJO|AMARILLO|VERDE)\s*\(Recomendación\s+(ALTA|MEDIA|BAJA)\b")

#: Documento: fila de tabla «| **🔴 ROJO** | **ALTA (Frenar)** | **P0 — Crítico** | ...».
_SEMAFORO_EN_TABLA = re.compile(
    r"^\|\s*\*{0,2}"
    + _EMOJI
    + r"\s+(ROJO|AMARILLO|VERDE)\*{0,2}\s*\|\s*\*{0,2}(ALTA|MEDIA|BAJA)\b[^|]*\|\s*\*{0,2}(P\d)\b",
    re.MULTILINE,
)

#: Documento, §7: «- **`DONE`** (`🟢 VERDE`): ...».
_ESTADO_CON_COLOR = re.compile(r"^- \*\*`([A-Z_]+)`\*\*\s*\(([^)]*)\)", re.MULTILINE)


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


def _seccion_del_documento(numero: int) -> str:
    """Cuerpo de la sección `## N. ...` del documento, acotado al próximo `## `."""
    texto = _leer(POLITICA)
    encabezado = re.search(rf"^## {numero}\. .*$", texto, flags=re.MULTILINE)
    assert encabezado is not None, f"el documento ya no tiene la sección §{numero}"
    cierre = _ENCABEZADO_DE_SECCION.search(texto, encabezado.end())
    return texto[encabezado.end() : cierre.start() if cierre else len(texto)]


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


def _prioridad_de(texto: str) -> tuple[str, ...]:
    """Cadena `a > b > c` de la prioridad, partida en sus criterios."""
    # Se une el texto antes de buscar, para que reajustar el ancho de un párrafo (o partir un
    # bloque `>` en varias líneas) no rompa el ancla.
    plano = re.sub(r"\s*\n\s*>?\s*", " ", texto)
    coincidencias = re.findall(r"seguridad\s*>.*?rendimiento", plano)
    assert len(coincidencias) == 1, f"se esperaba una sola cadena de prioridad, hay {len(coincidencias)}"
    return tuple(parte.strip() for parte in coincidencias[0].split(">"))


def _estados_de(linea: str) -> tuple[str, ...]:
    """Estados finales (identificadores en mayúsculas entre comillas invertidas)."""
    return tuple(re.findall(r"`([A-Z][A-Z_]+)`", linea))


def test_agents_md_declara_la_seccion_y_apunta_al_documento() -> None:
    """La sección existe, una sola vez, y enlaza al documento que la desarrolla."""
    seccion = _seccion_de_agents()

    assert f"({RUTA_DE_LA_POLITICA})" in seccion, "la sección no enlaza a la política desarrollada"
    assert (RAIZ / RUTA_DE_LA_POLITICA).is_file(), f"falta {RUTA_DE_LA_POLITICA}"
    assert "Antes de implementar y antes de declarar una tarea cerrada" in seccion, (
        "la sección ya no obliga a leer la política antes de implementar y antes de cerrar"
    )


def test_el_semaforo_esta_congelado_en_las_dos_copias() -> None:
    """Rojo/amarillo/verde con su recomendación (y severidad): igualdad literal."""
    en_agents = tuple((m.group(1), m.group(2), m.group(3)) for m in _SEMAFORO_EN_LISTA.finditer(_seccion_de_agents()))
    assert en_agents == SEMAFORO, f"semáforo de AGENTS.md cambió: {ascii(en_agents)}"

    en_politica = tuple(
        (m.group(1), m.group(2), m.group(3), m.group(4)) for m in _SEMAFORO_EN_TABLA.finditer(_leer(POLITICA))
    )
    assert en_politica == SEMAFORO_DETALLADO, f"semáforo del documento cambió: {ascii(en_politica)}"

    # Las dos copias coinciden: quitar la severidad y colapsar el rojo de dos filas da el de AGENTS.md.
    colapsado = tuple(dict.fromkeys((e, n, r) for e, n, r, _ in en_politica))
    assert colapsado == en_agents, "AGENTS.md y el documento dejaron de coincidir en el semáforo"


def test_las_consecuencias_de_cada_color_siguen_declaradas() -> None:
    """El color solo sirve si fija qué hacer: el rojo bloquea `DONE`, el verde no commitea."""
    seccion = _seccion_de_agents()

    assert "Prohibido declarar `DONE`" in seccion, "el ROJO ya no prohíbe declarar DONE"
    assert "NO commitear" in seccion, "el VERDE ya no prohíbe commitear"
    assert "sin abrir otro PR ni ampliar el actual sin autorización" in seccion, (
        "el AMARILLO ya no prohíbe ampliar el PR sin autorización"
    )

    politica = _leer(POLITICA)
    assert "Prohibido declarar `DONE`" in politica, "el documento: el ROJO ya no prohíbe declarar DONE"
    assert "No generar commits nuevos" in politica, "el documento: el VERDE ya no prohíbe commitear"
    assert "No implementar en este PR" in politica, "el documento: el AMARILLO ya no prohíbe ampliar el PR"


def test_los_estados_finales_estan_congelados_en_las_dos_copias() -> None:
    """Cuatro estados, ni uno más ni uno menos, y los mismos en las dos copias."""
    linea = next(
        (linea for linea in _seccion_de_agents().splitlines() if "Estados finales" in linea),
        "",
    )
    assert _estados_de(linea) == ESTADOS_FINALES, f"AGENTS.md: {_estados_de(linea)}"

    en_documento = tuple(m.group(1) for m in _ESTADO_CON_COLOR.finditer(_seccion_del_documento(7)))
    assert en_documento == ESTADOS_FINALES, f"documento: {en_documento}"


def test_cada_estado_cierra_con_el_color_que_le_corresponde() -> None:
    """`BLOCKED` es rojo y `DONE` es verde: el estado técnico y lo que el propietario lee no divergen."""
    por_estado = {
        m.group(1): tuple(re.findall(r"ROJO|AMARILLO|VERDE", m.group(2)))
        for m in _ESTADO_CON_COLOR.finditer(_seccion_del_documento(7))
    }

    assert por_estado == COLOR_POR_ESTADO, f"estado→color cambió: {por_estado}"


def test_la_prioridad_esta_congelada_en_las_dos_copias() -> None:
    """El orden de prioridad es lo que desempata; no puede reordenarse en silencio."""
    assert _prioridad_de(_seccion_de_agents()) == PRIORIDAD
    assert _prioridad_de(_leer(POLITICA)) == PRIORIDAD


def test_la_reapertura_esta_congelada_en_las_dos_copias() -> None:
    """Un cierre solo se reabre por las causas declaradas, y ninguna otra se agrega sola."""
    seccion = _seccion_de_agents().lower()
    # Sin el énfasis, para que un `**...**` no parta la frase buscada.
    bloque = _seccion_del_documento(7).replace("**", "")
    inicio = bloque.index("Reapertura:")
    parrafo = bloque[inicio:].lower()

    for causa in (c.lower() for c in REAPERTURA):
        assert causa in seccion, f"AGENTS.md ya no declara la causa de reapertura: {causa}"
        assert causa in parrafo, f"el documento ya no declara la causa de reapertura: {causa}"

    # El documento las numera (1)..(N); una causa extra rompe el ancla.
    numeradas = re.findall(r"\((\d)\)", parrafo)
    assert numeradas == [str(i) for i in range(1, len(REAPERTURA) + 1)], f"causas numeradas: {numeradas}"


def test_el_metodo_y_el_presupuesto_siguen_declarados() -> None:
    """Verificar antes de tocar y detenerse tras una ronda: lo que evita la mejora infinita."""
    seccion = _seccion_de_agents()
    politica = _leer(POLITICA)

    assert METODO in seccion, "AGENTS.md ya no exige CLAIM → EXTRACT → DOUBT → RECONCILE"
    assert METODO in politica, "el documento ya no exige CLAIM → EXTRACT → DOUBT → RECONCILE"
    assert "1 revisión adversarial acotada al cambio + 1 ronda de corrección y revalidación" in seccion
    assert "detener y escalar" in seccion.lower()
    assert "una (1)** revisión adversarial" in politica, "el documento ya no fija el presupuesto de revisión"
    assert "una (1) sola ronda" in politica, "el documento ya no fija el presupuesto de corrección"


def test_la_tarjeta_de_cierre_conserva_sus_campos_en_orden() -> None:
    """La tarjeta es lo que el propietario lee sin leer código: un campo menos es una verificación menos."""
    seccion = _seccion_del_documento(8)
    bloque = re.search(r"```markdown\n(.*?)```", seccion, flags=re.DOTALL)
    assert bloque is not None, "la tarjeta de §8 ya no está en un bloque ```markdown"

    posiciones = []
    for campo in CAMPOS_DE_LA_TARJETA:
        assert campo in bloque.group(1), f"la tarjeta ya no tiene el campo {ascii(campo)}"
        posiciones.append(bloque.group(1).index(campo))
    assert posiciones == sorted(posiciones), "los campos de la tarjeta cambiaron de orden"


def test_el_documento_declara_los_metadatos_minimos() -> None:
    """`docs/documentation/standards.md` exige audiencia, estado, fuentes y SHA."""
    assert POLITICA.is_file(), f"falta {RUTA_DE_LA_POLITICA}"
    texto = _leer(POLITICA)

    for metadato in ("**Audiencia:**", "**Estado:**", "**Fuentes canónicas:**", "**Última verificación:**"):
        assert metadato in texto, f"el documento no declara {metadato}"


def test_el_documento_declara_lo_que_el_repo_no_verifica() -> None:
    """Declarar el límite es parte del contrato; sin esto un verde sobre el ancla engaña."""
    texto = _leer(POLITICA)

    assert "## 9. Qué verifica el repo y qué no" in texto
    assert "**No verificado:**" in texto


def test_los_hechos_de_ci_que_el_documento_da_por_ciertos_siguen_existiendo() -> None:
    """§5 y §3 nombran cosas de CI y de `mypy`; si se renombran, la política enseña un falso rojo que ya no existe."""
    assert "Tests (Pytest)" in _leer(CI), "ci.yml ya no tiene el job agregador `Tests (Pytest)` que §5 cita"
    assert ":free" in _leer(QODO), "qodo-merge-adversarial.yml ya no usa un modelo `:free` (falso rojo de §5)"

    pyproject = _leer(PYPROJECT)
    for modulo in ("sky_claw.app.gui.*", "sky_claw.local.tools_installer"):
        assert f'"{modulo}"' in pyproject, f"pyproject.toml ya no lista {modulo} en las zonas exentas de mypy (§3)"


def test_los_enlaces_y_las_rutas_citadas_existen() -> None:
    """Una referencia rota hace que la política apunte a un documento que ya no está."""
    rotas: list[str] = []
    for ruta, texto in ((POLITICA, _leer(POLITICA)), (AGENTS, _seccion_de_agents())):
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
