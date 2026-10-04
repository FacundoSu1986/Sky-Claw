"""Ancla de la convención de worktrees para agentes.

Por qué existe:
Este repo se trabaja desde varias sesiones a la vez. Compartir el worktree
principal no falla de forma visible: la sesión siguiente hace `checkout` de otra
rama y la anterior commitea sobre la que no tocaba. Ya pasó acá: un ``HEAD`` movido
bajo los pies, con dos commits colocados en la rama equivocada y un directorio de
trabajo untracked borrado de un plumazo.

La propiedad que restaura: *toda sesión trabaja en su propio worktree bajo
``<repo>/.worktrees/<nombre>``, y el worktree principal queda reservado para
integrar*. Este archivo enumera esa familia y falla si alguien se la saltea, para
que el cumplimiento no dependa de que cada agente lea y recuerde la instrucción.

Cada ancla y qué cierra:

- ``test_los_worktrees_no_principales_viven_bajo_la_convencion``: lee el registro
  real de git. Es la que convierte la convención en puerta y no en sugerencia.
- ``test_la_convencion_esta_documentada_en_agents_md``: cierra el caso de que la
  regla se borre en silencio — si desaparece el texto, el test falla.
- ``test_el_ignorar_cubre_el_directorio_de_worktrees``: impide que un worktree de
  agente llegue a un PR.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
AGENTS_MD = RAIZ / "AGENTS.md"
GITIGNORE = RAIZ / ".gitignore"

NOMBRE_CONVENCION = ".worktrees"
NOMBRE_ANCLA = "test_worktree_convention_invariant"
TITULO_SECCION = "## Una sesión, un worktree"


def _normalizar(ruta: str | Path) -> str:
    """Comparable entre plataformas: resuelve symlinks y normaliza mayúsculas en Windows."""
    return os.path.normcase(os.path.realpath(str(ruta)))


def listar_worktrees() -> list[str]:
    """Rutas registradas en `git worktree list`. Lista vacía si git no está disponible."""
    try:
        resultado = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(RAIZ),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if resultado.returncode != 0:
        return []
    return [
        linea[len("worktree ") :].strip() for linea in resultado.stdout.splitlines() if linea.startswith("worktree ")
    ]


def test_los_worktrees_no_principales_viven_bajo_la_convencion() -> None:
    """Enumera el registro real de git y falla si algún worktree se sale de `<repo>/.worktrees/`."""
    rutas = listar_worktrees()
    if not rutas:
        return  # git no disponible: nada que auditar (repo empaquetado, CI mínimo)

    # El worktree principal es el único cuyo `.git` es un DIRECTORIO. En un worktree
    # enlazado `.git` es un archivo que apunta al principal. Por eso este test da el
    # mismo veredicto corrido desde el principal o desde un worktree de agente.
    primarios = [r for r in rutas if (Path(r) / ".git").is_dir()]
    assert len(primarios) <= 1, (
        f"Se esperaba como máximo un worktree principal (`.git` como directorio) y hay {len(primarios)}: {primarios}"
    )
    if not primarios:
        return

    raiz_principal = Path(primarios[0]).resolve()
    prefijo_convencion = _normalizar(raiz_principal / NOMBRE_CONVENCION) + os.sep

    fuera = [
        ruta
        for ruta in rutas
        if Path(ruta).resolve() != raiz_principal and not _normalizar(ruta).startswith(prefijo_convencion)
    ]

    assert not fuera, (
        "Hay worktrees fuera de la convención `<repo>/.worktrees/`: cada sesión necesita "
        "el suyo y el principal queda para integrar. Remediar sin perder trabajo:\n"
        f"  git worktree move <ruta> {raiz_principal / NOMBRE_CONVENCION}/<nombre>\n"
        "  git worktree prune   # si el directorio ya no existe\n"
        f"Fuera de convención ({len(fuera)}):\n  " + "\n  ".join(fuera)
    )


def test_la_convencion_esta_documentada_en_agents_md() -> None:
    """La regla no se borra en silencio: si el texto o su ancla desaparecen, esto falla."""
    contenido = AGENTS_MD.read_text(encoding="utf-8")

    assert TITULO_SECCION in contenido, (
        f"AGENTS.md ya no contiene la sección `{TITULO_SECCION}`. La convención de "
        "worktrees es la que evita que las sesiones se pisen entre sí: si se quita de "
        "la guía, el ancla que la verifica debe quitarse también, en el mismo cambio."
    )
    assert NOMBRE_CONVENCION in contenido, (
        f"AGENTS.md documenta la convención pero no nombra `{NOMBRE_CONVENCION}`; sin la ruta "
        "exacta un agente no puede saber dónde crear su worktree."
    )
    assert NOMBRE_ANCLA in contenido, (
        f"AGENTS.md no referencia el ancla `{NOMBRE_ANCLA}`: la regla y el gate que la "
        "verifica tienen que quedar enlazados, o la regla envejece."
    )


def test_el_ignorar_cubre_el_directorio_de_worktrees() -> None:
    """Ningún worktree de agente puede entrar a un PR."""
    reglas = [
        linea.strip()
        for linea in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if linea.strip() and not linea.strip().startswith("#")
    ]

    assert any(regla.rstrip("/") == NOMBRE_CONVENCION for regla in reglas), (
        f".gitignore no cubre `{NOMBRE_CONVENCION}/`. Sin esa regla, el checkout de un "
        "worktree de agente aparece como untracked en `git status` y puede terminar "
        "commiteado en un PR por accidente."
    )
