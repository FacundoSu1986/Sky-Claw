"""Ancla del estándar de integración y resolución de conflictos de git.

Por qué existe: `docs/development/merge_and_conflict_resolution.md` es un corpus
normativo (reglas R1..R15) y el modo de falla documentado de este repo es que una
regla normativa sin gate envejece en silencio. Estas anclas cubren las tres formas
de degradación que sí son mecánicamente detectables:

1. una regla que **desaparece** del documento — el conjunto se congela por igualdad
   literal, no por pertenencia (mismo criterio que `RITUAL_TOOL_MAP == {...}`);
2. una regla que queda **sin declarar cómo se verifica** — es el mecanismo por el
   que el resto del corpus normativo del repo no se cumple;
3. una referencia que **deja de existir**: enlaces relativos, rutas citadas y el
   índice que hace descubrible al documento.

Lo que estas anclas **no** cubren: que las reglas se cumplan en un merge concreto.
Eso lo hace la tabla de registro de §6 en cada PR, y no hay forma de verificarlo
desde un test sin acceso al cuerpo del PR. Declararlo es parte del contrato: un
test verde sobre este archivo no dice nada sobre el próximo conflicto.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ESTANDAR = RAIZ / "docs" / "development" / "merge_and_conflict_resolution.md"

#: Reglas normativas del estándar. Congeladas por igualdad: agregar una regla sin
#: actualizar esta tupla rompe el test (fricción deliberada: obliga a decidir si la
#: regla nueva tiene verificación), y borrar una la rompe también.
REGLAS: tuple[str, ...] = tuple(f"R{numero}" for numero in range(1, 16))

#: Clases de conflicto de §3. Una clase nueva sin entrada en la tabla deja el
#: documento sin autoridad definida para ese caso.
CLASES: tuple[str, ...] = ("MECANICO", "SEMANTICO", "DE_CONTRATO", "NO_CLASIFICADO")

#: Prefijos de ruta que el documento cita y que tienen que existir en el árbol.
PREFIJOS_DE_RUTA = ("sky_claw/", "tests/", "docs/", "scripts/", ".github/")

_ENCABEZADO_DE_REGLA = re.compile(r"^### (R\d+)\s+[—–-]\s+", re.MULTILINE)
_ENCABEZADO_DE_SECCION = re.compile(r"^## ", re.MULTILINE)
_ENLACE_MARKDOWN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ENTRE_COMILLAS_INVERTIDAS = re.compile(r"`([^`\n]+)`")


def _texto() -> str:
    """Contenido del estándar con fin de línea normalizado.

    El repo corre con `core.autocrlf=true` y `.gitattributes` no fija `*.md`, así
    que el archivo llega con CRLF en Windows y LF en CI. Sin normalizar, cualquier
    patrón que cruce líneas matchea en un runner y no en el otro.
    """
    return ESTANDAR.read_text(encoding="utf-8").replace("\r\n", "\n")


def _bloques_de_reglas() -> dict[str, str]:
    """Cuerpo de cada regla, acotado al próximo encabezado de regla o de sección."""
    texto = _texto()
    encabezados = list(_ENCABEZADO_DE_REGLA.finditer(texto))
    secciones = [coincidencia.start() for coincidencia in _ENCABEZADO_DE_SECCION.finditer(texto)]
    bloques: dict[str, str] = {}
    for indice, encabezado in enumerate(encabezados):
        limites = [len(texto)]
        if indice + 1 < len(encabezados):
            limites.append(encabezados[indice + 1].start())
        limites.extend(posicion for posicion in secciones if posicion > encabezado.end())
        bloques[encabezado.group(1)] = texto[encabezado.end() : min(limites)]
    return bloques


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


def test_el_estandar_declara_los_metadatos_minimos() -> None:
    """`docs/documentation/standards.md` exige audiencia, estado, fuentes y SHA."""
    assert ESTANDAR.is_file(), f"falta el estándar en {ESTANDAR.relative_to(RAIZ).as_posix()}"
    texto = _texto()

    for metadato in ("**Audiencia:**", "**Estado:**", "**Fuentes canónicas:**", "**Última verificación:**"):
        assert metadato in texto, f"el estándar no declara {metadato}"


def test_las_reglas_normativas_estan_congeladas() -> None:
    """Igualdad literal del conjunto: una regla no se borra ni se agrega en silencio."""
    documentadas = tuple(_bloques_de_reglas())

    assert documentadas == REGLAS, (
        "El conjunto de reglas del estándar cambió. Si la regla es nueva, agregala a "
        "REGLAS y declarale su `**Verificación:**`; si la borraste a propósito, actualizá "
        f"REGLAS en {Path(__file__).name}. Documentadas: {documentadas}"
    )


def test_cada_regla_declara_como_se_verifica() -> None:
    """Una regla sin verificación es la forma en que este corpus envejece.

    No alcanza con que exista la palabra: se exige contenido después del marcador,
    porque `**Verificación:**` seguido de nada cumple la forma y no el fondo.
    """
    sin_verificacion = []
    for regla, bloque in _bloques_de_reglas().items():
        marcador = "**Verificación:**"
        if marcador not in bloque:
            sin_verificacion.append(regla)
            continue
        contenido = bloque.split(marcador, 1)[1].strip()
        if len(contenido) < 30:
            sin_verificacion.append(regla)

    assert not sin_verificacion, f"reglas sin verificación declarada: {sin_verificacion}"


def test_las_clases_de_conflicto_estan_congeladas() -> None:
    """Cada clase declara autoridad y evidencia; una clase nueva rompe el ancla."""
    texto = _texto()

    assert "| Clase | Señal | Autoridad | Evidencia de cierre |" in texto, (
        "desapareció la tabla de clases de §3, que es la que asigna autoridad"
    )
    for clase in CLASES:
        assert f"`{clase}`" in texto, f"la clase {clase} ya no está declarada en el estándar"


def test_el_estandar_no_se_confunde_con_los_conflictos_de_mods() -> None:
    """El repo ya usa "conflicto" para records de ESP y assets que se pisan.

    Sin la desambiguación explícita, un lector (o un agente) puede aplicar este
    estándar de proceso a un conflicto de mods, o al revés. El módulo de dominio
    se nombra para que la frontera sea verificable y no una intención.
    """
    texto = _texto()

    assert "conflict_analyzer.py" in texto
    assert "asset_conflict_scan.py" in texto
    assert "conflicto de git" in texto


def test_los_enlaces_relativos_del_estandar_resuelven() -> None:
    """Automatiza el chequeo que `standards.md` pide a mano para un PR documental."""
    rotos = []
    for destino in _destinos_de_enlaces(_texto()):
        if destino.startswith(("http://", "https://", "mailto:", "#")):
            continue
        ruta = (ESTANDAR.parent / destino.split("#", 1)[0]).resolve()
        if not ruta.exists():
            rotos.append(destino)

    assert not rotos, f"enlaces relativos rotos en el estándar: {sorted(rotos)}"


def test_las_rutas_citadas_existen() -> None:
    """Una ruta inventada en un documento normativo es una instrucción falsa."""
    inexistentes = sorted(ruta for ruta in _rutas_citadas(_texto()) if not (RAIZ / ruta).exists())

    assert not inexistentes, f"el estándar cita rutas que no existen: {inexistentes}"


def test_el_estandar_esta_indexado() -> None:
    """Un documento que nadie encuentra no es una norma.

    El repo ya tuvo el caso opuesto: contenido movido que quedó huérfano del
    índice (`docs/superpowers`, PR #446). Se compara sobre destinos reales de
    enlace, no sobre subcadenas: nombrar la ruta en prosa no la indexa.
    """
    indice_raiz = _destinos_de_enlaces((RAIZ / "docs" / "README.md").read_text(encoding="utf-8"))
    indice_desarrollo = _destinos_de_enlaces((RAIZ / "docs" / "development" / "README.md").read_text(encoding="utf-8"))
    contribucion = _destinos_de_enlaces((RAIZ / "CONTRIBUTING.md").read_text(encoding="utf-8"))

    assert "development/README.md" in indice_raiz, "docs/README.md no indexa docs/development/"
    assert "merge_and_conflict_resolution.md" in indice_desarrollo, "el índice de development no lista el estándar"
    # Igualdad del destino, no subcadena: `...md.bak` contiene "merge_and_conflict_resolution.md"
    # y pasaba igual. Lo cazó un mutante, no la lectura (ver `test_docs_superpowers_namespace.py`,
    # mismo defecto señalado en la revisión de PR #459).
    assert "docs/development/merge_and_conflict_resolution.md" in contribucion, (
        "CONTRIBUTING.md no enlaza el estándar: quien abre un PR no lo encuentra"
    )
