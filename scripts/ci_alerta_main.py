"""Abre (o comenta) el issue «CI de main roto» cuando una corrida de ``main`` termina en rojo.

Lo ejecuta el job ``alerta-main`` de ``ci.yml`` solo fuera de un PR y sobre ``main`` (push, corrida nocturna o
``workflow_dispatch``). Desde que los PRs corren solo py3.11, una regresión exclusiva de py3.12 aparece recién ahí: sin
esta alerta sería una cruz roja en un commit que nadie mira, y los PRs siguientes se mergearían sobre un ``main`` roto.

Un solo issue abierto por vez: la primera falla lo crea y cada falla siguiente lo comenta. Se cierra a mano cuando
``main`` vuelve a verde. Solo usa la biblioteca estándar y el ``gh`` del runner: corre con el ``python3`` del sistema, sin
instalar el proyecto. Si no puede avisar, falla (el job queda en rojo): un aviso perdido en silencio es lo que se evita.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

TITULO = "CI de main roto"
#: Lo que el workflow le pasa al script por ``env`` (además de ``GH_TOKEN`` y ``GH_REPO``, que lee ``gh``).
VARIABLES_REQUERIDAS = ("EVENTO", "COMMIT", "URL_DE_LA_CORRIDA")

Ejecutor = Callable[[Sequence[str]], str]


def _gh(argumentos: Sequence[str]) -> str:
    """Corre ``gh`` sin shell y devuelve su salida; un código distinto de 0 lanza ``CalledProcessError``."""
    resultado = subprocess.run(  # noqa: S603 -- argumentos fijos del propio script, sin shell
        ["gh", *argumentos],  # noqa: S607 -- el gh del runner de GitHub
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return resultado.stdout


def cuerpo_del_aviso(evento: str, commit: str, url_de_la_corrida: str) -> str:
    return (
        f"La corrida {url_de_la_corrida} (evento `{evento}`, commit `{commit}`) de `main` terminó en rojo.\n\n"
        "Desde que los PRs corren la suite solo en py3.11, una regresión exclusiva de py3.12 aparece recién acá. "
        "Revisar el job que falló antes de mergear otro PR; con `main` de nuevo en verde, cerrar este issue a mano."
    )


def issue_abierto(ejecutar: Ejecutor) -> int | None:
    """El número del issue de la alerta que está abierto (el más viejo si hubiera varios), o ``None``.

    La búsqueda de GitHub es por palabras: se filtra por título EXACTO. Una respuesta que no se entiende es un error, no
    «no hay issue»: concluir lo segundo abriría un issue nuevo en cada corrida.
    """
    salida = ejecutar(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--search",
            f'"{TITULO}" in:title',
            "--json",
            "number,title",
            "--limit",
            "100",
        ]
    )
    try:
        issues = json.loads(salida)
    except ValueError as exc:
        raise ValueError(f"gh issue list devolvió algo que no es JSON: {salida[:200]!r}") from exc
    if not isinstance(issues, list) or not all(
        isinstance(i, dict) and isinstance(i.get("number"), int) and isinstance(i.get("title"), str) for i in issues
    ):
        raise ValueError(f"gh issue list devolvió una forma inesperada: {salida[:200]!r}")
    numeros = sorted(i["number"] for i in issues if i["title"] == TITULO)
    return numeros[0] if numeros else None


def alertar(evento: str, commit: str, url_de_la_corrida: str, ejecutar: Ejecutor = _gh) -> str:
    """Crea el issue o comenta el abierto; devuelve qué hizo (``creado`` o ``comentado #N``)."""
    cuerpo = cuerpo_del_aviso(evento, commit, url_de_la_corrida)
    numero = issue_abierto(ejecutar)
    if numero is None:
        ejecutar(["issue", "create", "--title", TITULO, "--body", cuerpo])
        return "creado"
    ejecutar(["issue", "comment", str(numero), "--body", cuerpo])
    return f"comentado #{numero}"


def main(entorno: Mapping[str, str] | None = None, ejecutar: Ejecutor = _gh) -> int:
    entorno = os.environ if entorno is None else entorno
    faltan = [nombre for nombre in VARIABLES_REQUERIDAS if not entorno.get(nombre)]
    if faltan:
        print(f"ERROR: faltan variables de entorno del workflow: {faltan}", file=sys.stderr)
        return 2
    print(
        f"Alerta de main roto: issue {alertar(entorno['EVENTO'], entorno['COMMIT'], entorno['URL_DE_LA_CORRIDA'], ejecutar)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
