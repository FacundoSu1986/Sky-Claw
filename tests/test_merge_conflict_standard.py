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
import subprocess
from pathlib import Path

import pytest

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
    índice (el namespace residual de PR #446; ver `test_docs_superpowers_namespace.py`,
    que prohíbe nombrar esa ruta en cualquier archivo trackeado, docstrings incluidos).
    Se compara sobre destinos reales de
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


# ---------------------------------------------------------------------------
# Estructura: el contenido de las secciones, no sólo la presencia de palabras.
#
# Las anclas de arriba dejaban sobrevivir mutantes que vaciaban la tabla de clases,
# la columna "Evidencia de cierre" o §6 entera. Estas enumeran filas y celdas.
# ---------------------------------------------------------------------------

#: Secciones de primer nivel, congeladas por igualdad (número y título).
SECCIONES: tuple[str, ...] = (
    "1. Objetivo y alcance",
    "2. Hechos medidos",
    "3. Clases de conflicto",
    "4. Reglas normativas",
    "5. Procedimiento",
    "6. Tabla de registro",
    "7. Fallos y recuperación",
    "8. Verificación",
    "9. Referencias",
)

_ENCABEZADO_NUMERADO = re.compile(r"^## (\d+\. .+?)\s*$", re.MULTILINE)


def _seccion(numero: int) -> str:
    """Cuerpo de la sección `## <numero>. ...`, acotado a la próxima sección."""
    texto = _texto()
    inicio = re.search(rf"^## {numero}\. .*$", texto, re.MULTILINE)
    assert inicio is not None, f"falta la sección {numero} del estándar"
    siguiente = _ENCABEZADO_DE_SECCION.search(texto, inicio.end())
    return texto[inicio.end() : siguiente.start() if siguiente else len(texto)]


def _filas_de_tabla(cuerpo: str) -> list[list[str]]:
    """Filas de datos (sin encabezado ni separador) de la primera tabla del cuerpo."""
    filas: list[list[str]] = []
    for linea in cuerpo.splitlines():
        linea = linea.strip()
        if not linea.startswith("|"):
            if filas:
                break
            continue
        celdas = [celda.strip() for celda in linea.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", celda) for celda in celdas):
            continue
        filas.append(celdas)
    return filas[1:]  # la primera fila es el encabezado


def test_las_secciones_del_estandar_estan_congeladas() -> None:
    """Borrar §6 (o cualquier sección) entera no puede pasar en silencio."""
    assert tuple(_ENCABEZADO_NUMERADO.findall(_texto())) == SECCIONES


def test_la_tabla_de_clases_tiene_una_fila_completa_por_clase() -> None:
    """Cada clase: una fila, con señal, autoridad y evidencia de cierre no vacías."""
    filas = _filas_de_tabla(_seccion(3))
    por_clase = {fila[0].strip("`"): fila for fila in filas}

    assert tuple(por_clase) == CLASES, f"las filas de §3 no son las clases congeladas: {tuple(por_clase)}"
    for clase, fila in por_clase.items():
        assert len(fila) == 4, f"{clase}: la fila no tiene 4 celdas"
        # `nadie` (autoridad de NO_CLASIFICADO) es la celda más corta legítima: 5.
        assert all(len(celda) >= 5 for celda in fila[1:]), (
            f"{clase}: señal, autoridad o evidencia de cierre vacías o triviales: {fila[1:]}"
        )


def test_la_tabla_de_registro_existe_y_respeta_r6() -> None:
    """§6 es lo único que R5/R6/R13 verifican: tiene que existir y ser coherente."""
    cuerpo = _seccion(6)
    assert "| Archivo | Clase | Lado autoritativo | Evidencia |" in cuerpo
    filas = _filas_de_tabla(cuerpo)
    assert filas, "§6 no tiene ninguna fila de ejemplo: R13 no tiene contra qué compararse"

    for archivo, clase, lado, evidencia in (fila[:4] for fila in filas):
        assert clase.strip("`") in CLASES, f"{archivo}: clase desconocida {clase!r}"
        assert archivo and lado and evidencia, f"{archivo}: celdas vacías"
        if clase.strip("`") in ("SEMANTICO", "DE_CONTRATO"):
            assert not re.search(r"\b(ours|theirs)\b", lado), (
                f"{archivo}: R6 prohíbe lado autoritativo ours/theirs para {clase}"
            )


def test_toda_receta_que_crea_un_worktree_vive_bajo_la_convencion() -> None:
    """Hermano de `test_los_worktrees_no_principales_viven_bajo_la_convencion`.

    El ancla del repo audita el registro real; esta audita las **recetas** del
    documento, que son las que producen un worktree fuera de convención. Se
    enumeran todos los `git worktree add` de los bloques de código.
    """
    texto = _texto()
    altas = [
        linea.strip()
        for bloque in re.findall(r"```bash\n(.*?)```", texto, flags=re.DOTALL)
        for linea in bloque.splitlines()
        if "git worktree add" in linea
    ]

    assert altas, "el estándar ya no documenta ninguna receta con worktree"
    fuera = [linea for linea in altas if ".worktrees/" not in linea]
    assert not fuera, f"recetas que crean un worktree fuera de <repo>/.worktrees/: {fuera}"


# ---------------------------------------------------------------------------
# Comportamiento: las recetas de git se EJECUTAN en repos aislados.
#
# El defecto original de este documento era que sus verificaciones no podían fallar
# (R7) o no distinguían lo que decían distinguir (R3). Un `in texto` no lo atrapa;
# correr la receta contra un grafo real, sí.
# ---------------------------------------------------------------------------

_GIT_BASE = (
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@t",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    resultado = subprocess.run(  # noqa: S603 - argv fijo, repo temporal
        [*_GIT_BASE, *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if check and resultado.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {resultado.stderr}")
    return resultado


def _commit(repo: Path, archivo: str, contenido: str, mensaje: str) -> str:
    (repo / archivo).write_text(contenido, encoding="utf-8", newline="\n")
    _git(repo, "add", archivo)
    _git(repo, "commit", "-m", mensaje)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _repo_con_rama(tmp_path: Path) -> Path:
    """`main` con a.py; la rama `pr` sale de ahí. Aún no hay divergencia."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.py", "x = 1\n", "base")
    _git(repo, "branch", "pr")
    return repo


def _version_de_git() -> tuple[int, ...]:
    salida = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout  # noqa: S607
    return tuple(int(parte) for parte in re.search(r"(\d+)\.(\d+)", salida).groups())  # type: ignore[union-attr]


requiere_merge_tree = pytest.mark.skipif(
    _version_de_git() < (2, 38), reason="`git merge-tree --write-tree` requiere git >= 2.38"
)


def test_r7_la_base_previa_ve_lo_que_trajo_main_y_la_receta_vieja_no(tmp_path: Path) -> None:
    """Reproduce la tautología: tras sincronizar, `merge-base origin/main HEAD` == main."""
    repo = _repo_con_rama(tmp_path)
    _commit(repo, "b.py", "y = 1\n", "main toca b.py")  # lo que trae el otro lado
    _git(repo, "checkout", "-q", "pr")
    _commit(repo, "c.py", "z = 1\n", "el PR agrega c.py")

    base = _git(repo, "merge-base", "main", "HEAD").stdout.strip()  # §5.1, ANTES de sincronizar
    _git(repo, "merge", "--no-ff", "-m", "sync", "main")  # R1 + R2

    # Receta vieja: vacía siempre. Se deja ejecutada para que no vuelva por accidente.
    vieja = _git(repo, "merge-base", "main", "HEAD").stdout.strip()
    assert _git(repo, "diff", "--name-only", vieja, "main").stdout.split() == []

    # Receta vigente: con la BASE guardada, y con la recuperación desde el commit de sync.
    assert _git(repo, "diff", "--name-only", base, "main").stdout.split() == ["b.py"]
    recuperada = _git(repo, "merge-base", "HEAD^1", "HEAD^2").stdout.strip()
    assert _git(repo, "diff", "--name-only", recuperada, "HEAD^2").stdout.split() == ["b.py"]

    texto = _texto()
    assert 'git diff --name-only "$BASE" origin/main' in texto
    assert 'git diff --name-only "$(git merge-base HEAD^1 HEAD^2)" HEAD^2' in texto


@requiere_merge_tree
def test_preflight_merge_tree_limpio_se_materializa_y_con_conflicto_no(tmp_path: Path) -> None:
    """B4: con conflicto, `$(...)` capturaría varias líneas y `commit-tree` muere."""
    repo = _repo_con_rama(tmp_path)
    _commit(repo, "b.py", "y = 1\n", "main toca b.py")
    _git(repo, "checkout", "-q", "pr")
    _commit(repo, "c.py", "z = 1\n", "el PR agrega c.py")

    limpio = _git(repo, "merge-tree", "--write-tree", "HEAD", "main")
    assert limpio.returncode == 0
    tree = limpio.stdout.strip()
    assert "\n" not in tree, "con exit 0 la salida es sólo el tree OID"
    commit = _git(repo, "commit-tree", tree, "-p", "HEAD", "-p", "main", "-m", "merge sintetico").stdout.strip()
    assert _git(repo, "rev-parse", f"{commit}^{{tree}}").stdout.strip() == tree

    _git(repo, "checkout", "-q", "main")
    _commit(repo, "a.py", "x = 2\n", "main cambia a.py")
    _git(repo, "checkout", "-q", "pr")
    _commit(repo, "a.py", "x = 3\n", "el PR cambia a.py")

    conflicto = _git(repo, "merge-tree", "--write-tree", "HEAD", "main", check=False)
    assert conflicto.returncode == 1
    assert len(conflicto.stdout.strip().splitlines()) > 1, "la salida con conflicto no es un OID solo"
    roto = _git(repo, "commit-tree", conflicto.stdout.strip(), "-m", "x", check=False)
    assert roto.returncode != 0, "la captura cruda del viejo snippet tiene que seguir fallando"

    assert "if TREE=$(git merge-tree --write-tree HEAD origin/main); then" in _texto()


def _es_squash(repo: Path, ref: str, numero: int) -> bool:
    """R3 tal como lo documenta el estándar: un padre y asunto terminado en `(#N)`."""
    padres, asunto = _git(repo, "log", "--format=%P%x09%s", "-1", ref).stdout.rstrip("\n").split("\t", 1)
    return len(padres.split()) == 1 and asunto.endswith(f"(#{numero})")


def test_r3_distingue_squash_de_rebase_merge_y_de_merge_commit(tmp_path: Path) -> None:
    """B6: un padre solo acepta un rebase merge como squash."""
    repo = _repo_con_rama(tmp_path)
    _git(repo, "checkout", "-q", "pr")
    primero = _commit(repo, "f1.py", "1\n", "feat: uno")
    segundo = _commit(repo, "f2.py", "2\n", "feat: dos")

    _git(repo, "checkout", "-q", "-b", "como-squash", "main")
    _git(repo, "merge", "--squash", "pr")
    _git(repo, "commit", "-m", "feat: uno y dos (#770)")
    assert _es_squash(repo, "como-squash", 770)

    _git(repo, "checkout", "-q", "-b", "como-rebase", "main")
    _git(repo, "cherry-pick", primero, segundo)
    assert not _es_squash(repo, "como-rebase", 770), "un rebase merge no es un squash (un padre, sin sufijo)"

    _git(repo, "checkout", "-q", "-b", "como-merge", "main")
    _git(repo, "merge", "--no-ff", "-m", "Merge pull request #770", "pr")
    assert not _es_squash(repo, "como-merge", 770)

    assert "git log --format='%P%x09%s' -1 origin/main" in _texto()
