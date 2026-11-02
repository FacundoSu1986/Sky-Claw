"""Origen de los procesos que el arranque del intérprete crea ANTES de que el worker pueda verificarse.

``windows_onefile_census`` cuenta CUÁNTOS procesos nacen; este módulo responde DESDE QUÉ CÓDIGO. Instala
un audit hook (PEP 578) y va importando, en orden, los módulos que recorre el ejecutable congelado antes
de llegar a ``worker_main``. Cada creación de proceso observada queda con la pila Python que la provocó y
con la FASE de import en curso, así que se puede decir «este ``cmd.exe`` nació en el import de X».

Es Python de desarrollo, no el ejecutable congelado: sirve para localizar el código responsable, no para
contar lo que hace el bundle (eso lo mide el censo con IOCP sobre el ``.exe`` real). No ejecuta nada de la
aplicación: solo importa módulos.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import sys
import traceback
from collections.abc import Sequence
from typing import Any

#: Eventos de auditoría que crean (o intentan crear) un proceso.
EVENTOS_DE_PROCESO = frozenset(
    {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile"}
)

#: Fases por defecto: lo que recorre el ejecutable congelado hasta ``worker_main``.
FASES_POR_DEFECTO = (
    "sky_claw",
    "sky_claw.compat",
    "sky_claw.__main__",
    "sky_claw.local.mo2.vfs_worker",
)

_PROPIO = pathlib.Path(__file__).name


def _marcos(limite: int = 60) -> list[str]:
    """Pila Python actual, sin los marcos de este módulo, ``archivo:línea:función``."""
    pila = traceback.extract_stack(limit=limite)
    return [f"{marco.filename}:{marco.lineno}:{marco.name}" for marco in pila if _PROPIO not in marco.filename]


def instalar(registro: list[dict[str, Any]], fase: dict[str, str]) -> None:
    """Instala el hook: cada creación de proceso se anota en ``registro`` con la fase vigente."""

    def hook(evento: str, argumentos: tuple[Any, ...]) -> None:
        if evento in EVENTOS_DE_PROCESO:
            registro.append(
                {
                    "fase": fase["actual"],
                    "evento": evento,
                    # `subprocess.Popen` entrega (executable, args, cwd, env): se recorta para no volcar el entorno.
                    "argumentos": repr(argumentos[:2])[:300],
                    "pila": _marcos()[-14:],
                }
            )

    sys.addaudithook(hook)


def medir(fases: Sequence[str]) -> list[dict[str, Any]]:
    registro: list[dict[str, Any]] = []
    fase = {"actual": "(antes de importar)"}
    instalar(registro, fase)
    for modulo in fases:
        fase["actual"] = f"import {modulo}"
        importlib.import_module(modulo)
    fase["actual"] = "(después de importar)"
    return registro


def resumir(registro: Sequence[dict[str, Any]]) -> str:
    if not registro:
        return "ningún import creó procesos"
    lineas = [f"{len(registro)} creaciones de proceso durante los imports:"]
    for evento in registro:
        ultimo = next((m for m in reversed(evento["pila"]) if "site-packages" not in m and "importlib" not in m), "?")
        lineas.append(f"  [{evento['fase']}] {evento['evento']} {evento['argumentos']}  <- {ultimo}")
    return "\n".join(lineas)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="windows_spawn_origin", description=__doc__)
    parser.add_argument("--fase", action="append", default=[], help="módulo a importar (repetible, en orden)")
    parser.add_argument("--json", type=pathlib.Path, help="ruta del JSON de resultados")
    parser.add_argument("--resumir", type=pathlib.Path, help="imprime el resumen de un JSON ya generado y termina")
    args = parser.parse_args(argv)
    if args.resumir is not None:
        print(resumir(json.loads(args.resumir.read_text(encoding="utf-8"))))  # noqa: T201 - CLI
        return 0
    fases = tuple(args.fase) or FASES_POR_DEFECTO
    registro = medir(fases)
    print(resumir(registro))  # noqa: T201 - CLI
    if args.json:
        args.json.write_text(json.dumps(registro, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
