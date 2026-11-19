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

La propiedad que hace valer la igualdad: **la extracción es estructural, no por patrón**. Se
recorre cada línea del bloque (sub-lista del semáforo, filas de la tabla, viñetas de estados,
párrafo de reapertura, líneas de la tarjeta) y una línea que no tiene la forma esperada rompe el
ancla en vez de quedar fuera de la captura. Buscar solo lo conocido (los tres emojis, el formato
de hoy) deja pasar en verde justamente lo nuevo, que es lo que el ancla tiene que atajar. Los
hechos de CI se leen parseando el YAML/TOML, no buscando texto: un comentario no cuenta.

Lo que estas anclas **no** cubren: que un agente clasifique bien un hallazgo, que un cierre
concreto respete el presupuesto o que emita la tarjeta. Eso lo revisa el propietario sobre el
cuerpo del PR; no hay forma de verificarlo desde un test sin acceso a él. Un test verde sobre
este archivo no dice nada sobre el próximo cierre, y el documento lo declara en su §9 (también
se ancla).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import yaml

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

#: Únicas causas de reapertura de un cierre (forma de `AGENTS.md`).
REAPERTURA: tuple[str, ...] = (
    "regresión reproducida",
    "defecto P0/P1 demostrado",
    "incumplimiento del contrato original",
    "nueva orden explícita del propietario",
)

#: Las mismas causas como las escribe el documento, con sus aclaraciones. Se congelan enteras:
#: descartar las aclaraciones antes de comparar dejaría pasar una causa nueva escrita entre
#: paréntesis. Sin las aclaraciones, tienen que dar exactamente `REAPERTURA`.
REAPERTURA_EN_EL_DOCUMENTO: tuple[str, ...] = (
    "regresión reproducida",
    "defecto P0/P1 demostrado (vulnerabilidad o fallo de integridad)",
    "incumplimiento del contrato original (un criterio original descubierto sin cumplir)",
    "nueva orden explícita del propietario",
)

#: Lo que precede a `(1)` en el párrafo de reapertura del documento.
PREAMBULO_DE_REAPERTURA = "Una tarea cerrada solo se reabre por"

#: Método de verificación de hallazgos ajenos, en orden.
METODO = "CLAIM → EXTRACT → DOUBT → RECONCILE"

#: Campos de la tarjeta obligatoria de cierre (§8 del documento), en orden de aparición.
CAMPOS_DE_LA_TARJETA: tuple[str, ...] = (
    "### Semáforo de Cierre:",
    "- **Estado:**",
    "- **Recomendación al propietario:**",
    "- **SHA / Versión verificada:**",
    "- **Honestidad de verificación:**",
    "- *Ejecutado:*",
    "- *Leído en código:*",
    "- *No verificado:*",
    "- **Hallazgos clasificados:**",
    f"- {ROJO} Bloqueantes (P0/P1):",
    f"- {AMARILLO} Diferidos / Limitaciones (P2):",
    f"- {VERDE} Descartados (P3 / ruido de bots):",
    "- **Por qué corresponde detenerse (o seguir) ahora:**",
)

#: Módulos que §3 del documento cita como zona exenta de `mypy`, en orden. Se comparan por
#: igualdad contra lo que §3 cita y cada uno contra `pyproject.toml`: la política no puede
#: nombrar una zona exenta que la configuración no exime.
MODULOS_EXENTOS_DE_MYPY: tuple[str, ...] = (
    "sky_claw.app.gui.*",
    "sky_claw.app.web.*",
    "sky_claw.local.tools.*",
    "sky_claw.local.tools_installer",
)

#: Nombre (o parte del nombre) del job agregador de pytest que §5 cita como falso rojo conocido.
JOB_AGREGADOR = "Tests (Pytest)"

#: Prefijos de ruta que los documentos citan y que tienen que existir en el árbol.
PREFIJOS_DE_RUTA = ("sky_claw/", "tests/", "docs/", "scripts/", ".github/")

_ENLACE_MARKDOWN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ENTRE_COMILLAS_INVERTIDAS = re.compile(r"`([^`\n]+)`")
_ENCABEZADO_DE_SECCION = re.compile(r"^## ", re.MULTILINE)

#: `AGENTS.md`, cada sub-viñeta del semáforo: «- **🔴 ROJO (Recomendación ALTA — ...)».
#: Genérico a propósito (cualquier emoji y nombre): un color nuevo se parsea y rompe la igualdad.
_SEMAFORO_EN_LISTA = re.compile(r"^- \*\*(\S+)[ \t]+(\S+)[ \t]*\(Recomendación[ \t]+(\S+)")

#: Separador de una tabla Markdown: «| --- | --- |».
_SEPARADOR_DE_TABLA = re.compile(r"^\|[\s:|-]+\|$")

#: Documento, §7: «- **`DONE`** (`🟢 VERDE`): ...».
_ESTADO_CON_COLOR = re.compile(r"^- \*\*`([A-Z_]+)`\*\*\s*\(([^)]*)\)")

#: Etiqueta de campo de la tarjeta de §8: el encabezado, «- **Campo:**», «- *Subcampo:*» o
#: «- 🔴 Hallazgo:». Toda línea de la tarjeta tiene que empezar con una de estas formas.
_ETIQUETA_DE_LA_TARJETA = re.compile(
    r"^[ \t]*(###[ \t]+[^:\n]+:|-[ \t]+\*\*[^*\n]+:\*\*|-[ \t]+\*[^*\n]+:\*|-[ \t]+[\U0001f534\U0001f7e1\U0001f7e2][ \t]+[^:\n]+:)"
)


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
    """Cadena COMPLETA `a > b > ... > z` de la prioridad, partida en sus criterios.

    Se toma desde `seguridad >` hasta el fin de la línea o del código en línea, y no hasta el
    último criterio conocido: así un criterio agregado al final ensancha la cadena y rompe la
    igualdad con `PRIORIDAD` en vez de quedar fuera de la captura.
    """
    cadenas = re.findall(r"seguridad\s*>[^`\n]*", texto)
    assert len(cadenas) == 1, f"se esperaba una sola cadena de prioridad, hay {len(cadenas)}"
    return tuple(parte.strip() for parte in cadenas[0].rstrip(" .").split(">"))


def _reapertura_en_agents(seccion: str) -> tuple[str, ...]:
    """Causas de reapertura de `AGENTS.md`: «Solo por a, b, c o d.» partido en sus causas."""
    linea = next((linea for linea in seccion.splitlines() if "**Reapertura:**" in linea), "")
    assert linea, "AGENTS.md ya no tiene la línea «**Reapertura:**»"
    cuerpo = linea.split("**Reapertura:**", 1)[1].strip().rstrip(".")
    cuerpo = re.sub(r"^Solo por\s+", "", cuerpo)
    return tuple(causa.strip() for causa in re.split(r",\s*|\s+o\s+", cuerpo))


def _reapertura_en_documento(seccion: str) -> tuple[str, tuple[str, ...]]:
    """(preámbulo, causas) del párrafo «Reapertura:» del documento, hasta el fin de §7.

    Se parte por los marcadores `(N)` y cada causa conserva sus aclaraciones: todo el texto del
    párrafo queda en el preámbulo o en alguna causa, así que un agregado en cualquier lugar —con
    o sin numerar, dentro o fuera de paréntesis— cambia la tupla y rompe la igualdad.
    """
    plano = re.sub(r"\s+", " ", seccion.replace("**", ""))
    assert "Reapertura:" in plano, "el documento ya no tiene el párrafo «Reapertura:»"
    partes = re.split(r"\(\d\)", plano[plano.index("Reapertura:") + len("Reapertura:") :])
    causas = tuple(re.sub(r"(?:,| o)?\.?$", "", parte.strip()).strip() for parte in partes[1:])
    return partes[0].strip(), causas


def _semaforo_en_agents(seccion: str) -> tuple[tuple[str, str, str], ...]:
    """(emoji, nombre, recomendación) de CADA sub-viñeta del «Semáforo obligatorio».

    Se toman todas las líneas indentadas que siguen a la viñeta del semáforo hasta la próxima
    viñeta de primer nivel; una que no tenga la forma esperada rompe el ancla.
    """
    lineas = seccion.splitlines()
    inicio = next((i for i, linea in enumerate(lineas) if "**Semáforo obligatorio" in linea), None)
    assert inicio is not None, "AGENTS.md ya no tiene la viñeta «Semáforo obligatorio»"
    filas = []
    for linea in lineas[inicio + 1 :]:
        if not linea.startswith((" ", "\t")):
            break
        if not linea.strip():
            continue
        coincidencia = _SEMAFORO_EN_LISTA.match(linea.strip())
        assert coincidencia, f"sub-viñeta del semáforo sin la forma esperada: {ascii(linea.strip())}"
        filas.append((coincidencia.group(1), coincidencia.group(2), coincidencia.group(3)))
    return tuple(filas)


def _semaforo_en_documento() -> tuple[tuple[str, str, str, str], ...]:
    """(emoji, nombre, recomendación, severidad) de CADA fila de la tabla de §3."""
    filas_de_tabla = [linea.strip() for linea in _seccion_del_documento(3).splitlines() if linea.startswith("|")]
    assert len(filas_de_tabla) >= 2, "§3 ya no tiene la tabla del semáforo"
    filas = []
    for fila in filas_de_tabla[1:]:  # la primera es el encabezado
        if _SEPARADOR_DE_TABLA.match(fila):
            continue
        celdas = [celda.replace("**", "").strip() for celda in fila.strip("|").split("|")]
        color = celdas[0].split()
        assert len(celdas) >= 3 and len(color) == 2, f"fila del semáforo sin la forma esperada: {ascii(fila)}"
        filas.append((color[0], color[1], celdas[1].split()[0], celdas[2].split()[0]))
    return tuple(filas)


def _estados_del_documento() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """(estado, colores) de CADA viñeta de primer nivel de §7; una sin la forma esperada rompe el ancla."""
    estados = []
    for linea in _seccion_del_documento(7).splitlines():
        if not linea.startswith("- "):
            continue
        coincidencia = _ESTADO_CON_COLOR.match(linea)
        assert coincidencia, f"viñeta de §7 sin la forma «- **`ESTADO`** (color)»: {ascii(linea)}"
        estados.append((coincidencia.group(1), tuple(re.findall(r"ROJO|AMARILLO|VERDE", coincidencia.group(2)))))
    return tuple(estados)


def _etiquetas_de_la_tarjeta(bloque: str) -> tuple[str, ...]:
    """La etiqueta de CADA línea de la tarjeta, en orden y con los espacios normalizados.

    Una línea que no empieza con una etiqueta reconocible rompe el ancla: un campo nuevo escrito
    en otro formato no puede quedar fuera de la comparación.
    """
    etiquetas = []
    for linea in bloque.splitlines():
        if not linea.strip():
            continue
        coincidencia = _ETIQUETA_DE_LA_TARJETA.match(linea)
        assert coincidencia, f"línea de la tarjeta sin etiqueta reconocible: {ascii(linea)}"
        etiquetas.append(re.sub(r"\s+", " ", coincidencia.group(1)))
    return tuple(etiquetas)


def _nombres_de_jobs(ruta: Path) -> list[str]:
    """Nombre de cada job del workflow, leído del YAML parseado (un comentario no cuenta)."""
    datos = yaml.safe_load(_leer(ruta))
    return [str(job.get("name", clave)) for clave, job in datos["jobs"].items()]


def _valores_escalares(nodo: object) -> Iterator[str]:
    """Todos los valores escalares de un YAML parseado, como texto (sin comentarios)."""
    if isinstance(nodo, dict):
        for valor in nodo.values():
            yield from _valores_escalares(valor)
    elif isinstance(nodo, list):
        for valor in nodo:
            yield from _valores_escalares(valor)
    elif nodo is not None:
        yield str(nodo)


def _overrides_de_mypy() -> list[tuple[tuple[str, ...], object]]:
    """(módulos, ignore_errors) de cada `[[tool.mypy.overrides]]` de `pyproject.toml`."""
    datos = tomllib.loads(_leer(PYPROJECT))
    resultado: list[tuple[tuple[str, ...], object]] = []
    for override in datos["tool"]["mypy"]["overrides"]:
        modulos = override.get("module", [])
        resultado.append(((modulos,) if isinstance(modulos, str) else tuple(modulos), override.get("ignore_errors")))
    return resultado


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
    en_agents = _semaforo_en_agents(_seccion_de_agents())
    assert en_agents == SEMAFORO, f"semáforo de AGENTS.md cambió: {ascii(en_agents)}"

    en_politica = _semaforo_en_documento()
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

    en_documento = tuple(estado for estado, _ in _estados_del_documento())
    assert en_documento == ESTADOS_FINALES, f"documento: {en_documento}"


def test_cada_estado_cierra_con_el_color_que_le_corresponde() -> None:
    """`BLOCKED` es rojo y `DONE` es verde: el estado técnico y lo que el propietario lee no divergen."""
    por_estado = dict(_estados_del_documento())

    assert por_estado == COLOR_POR_ESTADO, f"estado→color cambió: {por_estado}"


def test_la_prioridad_esta_congelada_en_las_dos_copias() -> None:
    """El orden de prioridad es lo que desempata; no puede reordenarse en silencio."""
    assert _prioridad_de(_seccion_de_agents()) == PRIORIDAD
    assert _prioridad_de(_leer(POLITICA)) == PRIORIDAD


def test_la_reapertura_esta_congelada_en_las_dos_copias() -> None:
    """Un cierre solo se reabre por las causas declaradas, y ninguna otra se agrega sola."""
    en_agents = _reapertura_en_agents(_seccion_de_agents())
    preambulo, en_documento = _reapertura_en_documento(_seccion_del_documento(7))

    assert en_agents == REAPERTURA, f"causas de reapertura de AGENTS.md: {en_agents}"
    assert preambulo == PREAMBULO_DE_REAPERTURA, f"preámbulo de reapertura del documento: {preambulo}"
    assert en_documento == REAPERTURA_EN_EL_DOCUMENTO, f"causas de reapertura del documento: {en_documento}"

    # Las dos copias dicen lo mismo: sin las aclaraciones, las causas del documento son las de AGENTS.md.
    nucleo = tuple(re.sub(r"\s*\([^)]*\)", "", causa) for causa in REAPERTURA_EN_EL_DOCUMENTO)
    assert nucleo == REAPERTURA, f"el documento y AGENTS.md divergen en las causas de reapertura: {nucleo}"


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

    # Igualdad de la secuencia completa: un campo duplicado, agregado, ausente o reordenado rompe el ancla.
    en_la_tarjeta = _etiquetas_de_la_tarjeta(bloque.group(1))
    assert en_la_tarjeta == CAMPOS_DE_LA_TARJETA, f"campos de la tarjeta: {ascii(en_la_tarjeta)}"


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
    # Se parsea el YAML: `Tests (Pytest)` también aparece en comentarios de ci.yml, que no son un job.
    assert any(JOB_AGREGADOR in nombre for nombre in _nombres_de_jobs(CI)), (
        f"ci.yml ya no tiene un job llamado `{JOB_AGREGADOR}` (el falso rojo que §5 cita)"
    )
    assert any(":free" in valor for valor in _valores_escalares(yaml.safe_load(_leer(QODO)))), (
        "qodo-merge-adversarial.yml ya no configura un modelo `:free` (el falso rojo que §5 cita)"
    )

    # Lo que §3 cita como zona exenta de mypy es exactamente lo congelado, ni más ni menos.
    fila_p1 = next((linea for linea in _seccion_del_documento(3).splitlines() if "P1 — Bloqueante" in linea), "")
    citados = tuple(re.findall(r"`(sky_claw\.[^`]+)`", fila_p1))
    assert citados == MODULOS_EXENTOS_DE_MYPY, f"§3 cita como exentos de mypy: {citados}"

    # No alcanza con que el nombre esté escrito: el override que lo lista tiene que seguir
    # ignorando errores (`ignore_errors = true`), que es lo que hace que la zona esté exenta.
    overrides = _overrides_de_mypy()
    for modulo in MODULOS_EXENTOS_DE_MYPY:
        veredictos = [ignora for modulos, ignora in overrides if modulo in modulos]
        assert veredictos, f"pyproject.toml ya no lista {modulo} en ningún override de mypy (§3)"
        assert all(ignora is True for ignora in veredictos), (
            f"{modulo} dejó de estar exento de mypy: sus overrides dan ignore_errors={veredictos} (§3)"
        )


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
