"""Gate «Main en verde»: un PR no se mergea mientras la última corrida terminada de CI sobre ``main`` esté en rojo.

La alerta de ``main`` roto (``scripts/ci_alerta_main.py``) abre un issue, pero un issue no frena a nadie. Este script es
el check ``🚦 Main en verde`` de ``.github/workflows/main-en-verde.yml`` y tiene dos subcomandos:

- ``evaluar`` (en cada PR contra ``main``): mira la última corrida TERMINADA de ``ci.yml`` sobre ``main`` (push,
  nocturna o a pedido) y falla si alguno de los checks obligatorios terminó en rojo. Una corrida sin veredicto (cancelada,
  incompleta) se saltea y decide la anterior; si ninguna tiene veredicto, falla cerrado: no saber no es «está en verde».
  El PR que arregla ``main`` pasa con la etiqueta ``ci:repara-main``, leída EN VIVO (al re-correr un job, el payload del
  evento es el original y no ve una etiqueta puesta después).
- ``reevaluar`` (al terminar cada corrida de CI sobre ``main``): vuelve a correr el job del gate en cada PR abierto. Sin
  esto, un PR que pasó el gate a las 10 seguiría en verde a las 12 aunque ``main`` se hubiera roto a las 11. Si no
  puede (GitHub rechaza el rerun a los 30 días de la corrida inicial o tras 50 reruns), pasa el PR a BORRADOR, que no se
  puede mergear, y le comenta por qué; marcarlo listo (``ready_for_review``) corre el gate de cero. Los borradores no
  se re-evalúan.

Solo usa la biblioteca estándar y el ``gh`` del runner: corre con el ``python3`` del sistema, sin instalar el proyecto.
Una respuesta de ``gh`` que no se entiende es un error, nunca «no hay nada en rojo».
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

WORKFLOW_DE_CI = "ci.yml"
WORKFLOW_DEL_GATE = "main-en-verde.yml"
NOMBRE_DEL_GATE = "🚦 Main en verde"
ETIQUETA_DE_SALIDA = "ci:repara-main"
#: Los checks que exige la protección de ``main`` (los jobs agregadores de ``ci.yml``). El build y el POSIX informativo
#: no están: su rojo lo avisa la alerta, pero no frena merges.
CHECKS_OBLIGATORIOS = ("🔍 Lint (Ruff)", "🏷️ Type Check (Mypy)", "🧪 Tests (Pytest)", "🛡️ Security Scan")
#: Las corridas que hablan del estado de ``main``: todo disparador de ``ci.yml`` salvo el PR.
EVENTOS_DE_MAIN = frozenset({"push", "schedule", "workflow_dispatch"})
#: Una conclusión de job que es un rojo de verdad (``cancelled``/``skipped``/``neutral`` no dicen nada de ``main``).
CONCLUSIONES_ROJAS = frozenset({"failure", "timed_out", "startup_failure"})
#: Cuántas corridas de ``main`` se miran hacia atrás buscando una con veredicto.
LIMITE_DE_CORRIDAS = 30
#: Cuánto se espera a que termine un gate en curso antes de re-correrlo (30 × 10 s = 5 min, dentro del timeout del job).
INTENTOS_DE_ESPERA = 30
PAUSA_DE_ESPERA = 10.0

Ejecutor = Callable[[Sequence[str]], str]
Espera = Callable[[float], None]


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


def _json(ejecutar: Ejecutor, argumentos: Sequence[str]) -> Any:
    salida = ejecutar(argumentos)
    try:
        return json.loads(salida)
    except ValueError as exc:
        raise ValueError(f"gh {' '.join(argumentos[:2])} devolvió algo que no es JSON: {salida[:200]!r}") from exc


def _lista_de_dicts(datos: Any, claves: Mapping[str, type], comando: str) -> list[dict[str, Any]]:
    """Valida que ``datos`` sea una lista de dicts con esas claves y tipos; si no, es un error (no «lista vacía»)."""
    if not isinstance(datos, list) or not all(
        isinstance(d, dict) and all(isinstance(d.get(clave), tipo) for clave, tipo in claves.items()) for d in datos
    ):
        raise ValueError(f"gh {comando} devolvió una forma inesperada: {str(datos)[:200]!r}")
    return datos


def _jobs_de(ejecutar: Ejecutor, corrida: int) -> list[dict[str, Any]]:
    datos = _json(ejecutar, ["run", "view", str(corrida), "--json", "jobs"])
    jobs = datos.get("jobs") if isinstance(datos, dict) else None
    return _lista_de_dicts(jobs, {"name": str}, "run view")


@dataclass(frozen=True)
class Veredicto:
    verde: bool
    mensaje: str


def _estado_de_main(ejecutar: Ejecutor) -> Veredicto:
    corridas = _lista_de_dicts(
        _json(
            ejecutar,
            [
                "run",
                "list",
                "--workflow",
                WORKFLOW_DE_CI,
                "--branch",
                "main",
                "--status",
                "completed",
                "--json",
                "databaseId,event,headSha,url",
                "--limit",
                str(LIMITE_DE_CORRIDAS),
            ],
        ),
        {"databaseId": int, "event": str, "headSha": str, "url": str},
        "run list",
    )
    for corrida in corridas:
        # ``--branch main`` también trae un PR desde un fork cuya rama se llama main: solo cuentan las corridas de main.
        if corrida["event"] not in EVENTOS_DE_MAIN:
            continue
        conclusiones: dict[str, set[Any]] = {nombre: set() for nombre in CHECKS_OBLIGATORIOS}
        for job in _jobs_de(ejecutar, corrida["databaseId"]):
            if job["name"] in conclusiones:
                conclusiones[job["name"]].add(job.get("conclusion"))
        origen = f"la corrida {corrida['url']} ({corrida['event']}, {corrida['headSha'][:8]})"
        rojos = [nombre for nombre, vistas in conclusiones.items() if vistas & CONCLUSIONES_ROJAS]
        if rojos:
            return Veredicto(False, f"main en rojo: en {origen} fallaron {', '.join(rojos)}.")
        if all(vistas == {"success"} for vistas in conclusiones.values()):
            return Veredicto(
                True, f"main en verde: {origen} tiene los {len(CHECKS_OBLIGATORIOS)} checks obligatorios en verde."
            )
        # Cancelada, salteada o incompleta: no dice si main está roto. Decide la anterior.
    return Veredicto(
        False,
        f"ninguna de las últimas {LIMITE_DE_CORRIDAS} corridas terminadas de CI sobre main tiene veredicto: no se puede "
        "afirmar que main esté en verde.",
    )


def evaluar(pr: int, ejecutar: Ejecutor = _gh) -> Veredicto:
    """El veredicto del gate para el PR ``pr``: el estado de ``main``, salvo que el PR lleve la etiqueta de salida."""
    datos = _json(ejecutar, ["pr", "view", str(pr), "--json", "labels"])
    etiquetas = _lista_de_dicts(datos.get("labels") if isinstance(datos, dict) else None, {"name": str}, "pr view")
    estado = _estado_de_main(ejecutar)
    if any(etiqueta["name"] == ETIQUETA_DE_SALIDA for etiqueta in etiquetas):
        return Veredicto(
            True, f"PR con la etiqueta `{ETIQUETA_DE_SALIDA}`: pasa aunque main no esté en verde. {estado.mensaje}"
        )
    if estado.verde:
        return estado
    return Veredicto(
        False,
        f"{estado.mensaje} Arreglar main antes de mergear otro PR; el PR que lo arregla lleva la etiqueta "
        f"`{ETIQUETA_DE_SALIDA}`.",
    )


@dataclass
class Presupuesto:
    """Pausas que le quedan a TODA la reevaluación: esperas por PR en serie podrían sumar más que el timeout del job, y
    GitHub lo mataría antes de llegar a los PRs que faltan (que conservarían su veredicto viejo)."""

    pausas: int = INTENTOS_DE_ESPERA


def _esperar_a_que_termine(corrida: int, ejecutar: Ejecutor, esperar: Espera, presupuesto: Presupuesto) -> None:
    """Espera a que la corrida termine; si se agota el presupuesto es un error (no se puede saltear: ver ``_reevaluar_pr``)."""
    while presupuesto.pausas > 0:
        presupuesto.pausas -= 1
        esperar(PAUSA_DE_ESPERA)
        datos = _json(ejecutar, ["run", "view", str(corrida), "--json", "status"])
        if not isinstance(datos, dict) or not isinstance(datos.get("status"), str):
            raise ValueError(f"gh run view devolvió una forma inesperada: {str(datos)[:200]!r}")
        if datos["status"] == "completed":
            return
    raise ValueError(
        f"la corrida {corrida} del gate no terminó dentro de los {INTENTOS_DE_ESPERA * PAUSA_DE_ESPERA:.0f} s de espera "
        "de la reevaluación"
    )


def _reevaluar_pr(
    numero: int, commit: str, rama: str, ejecutar: Ejecutor, esperar: Espera, presupuesto: Presupuesto
) -> str:
    """Vuelve a correr el job del gate de ese PR; devuelve qué hizo. Un problema real lanza ``ValueError``."""
    corridas = _lista_de_dicts(
        _json(
            ejecutar,
            [
                "run",
                "list",
                "--workflow",
                WORKFLOW_DEL_GATE,
                "--commit",
                commit,
                # Dos PRs pueden apuntar al mismo commit desde ramas distintas: solo por commit, uno se llevaría la
                # corrida del otro y el segundo conservaría su veredicto viejo.
                "--branch",
                rama,
                "--event",
                "pull_request",
                "--json",
                "databaseId,status,event",
                "--limit",
                "20",
            ],
        ),
        {"databaseId": int, "status": str, "event": str},
        "run list",
    )
    del_pr = [corrida for corrida in corridas if corrida["event"] == "pull_request"]
    if not del_pr:
        return "sin corrida del gate (su check falta y eso ya bloquea)"
    corrida = del_pr[0]
    if corrida["status"] != "completed":
        # Un gate en curso pudo haber leído las corridas de main ANTES de que terminara la que disparó esta
        # reevaluación: saltearlo dejaría su veredicto viejo fijo hasta el próximo evento del PR. Se espera y se re-corre.
        _esperar_a_que_termine(corrida["databaseId"], ejecutar, esperar, presupuesto)
    jobs = [job for job in _jobs_de(ejecutar, corrida["databaseId"]) if job["name"] == NOMBRE_DEL_GATE]
    if len(jobs) != 1 or not isinstance(jobs[0].get("databaseId"), int):
        raise ValueError(f"la corrida {corrida['databaseId']} no tiene exactamente un job «{NOMBRE_DEL_GATE}»")
    ejecutar(["run", "rerun", str(corrida["databaseId"]), "--job", str(jobs[0]["databaseId"])])
    return f"re-evaluado (corrida {corrida['databaseId']})"


def reevaluar(ejecutar: Ejecutor = _gh, esperar: Espera = time.sleep) -> int:
    """Re-corre el gate de cada PR abierto contra ``main``. Devuelve 1 si alguno no se pudo re-evaluar."""
    prs = _lista_de_dicts(
        _json(
            ejecutar,
            [
                "pr",
                "list",
                "--base",
                "main",
                "--state",
                "open",
                "--json",
                "number,headRefOid,headRefName,isDraft",
                "--limit",
                "200",
            ],
        ),
        {"number": int, "headRefOid": str, "headRefName": str, "isDraft": bool},
        "pr list",
    )
    sin_proteccion = 0
    presupuesto = Presupuesto()
    for pr in prs:
        if pr["isDraft"]:
            # Un borrador no se puede mergear; al marcarlo listo (`ready_for_review`) el gate corre de cero. Saltearlo
            # además no gasta reruns de su corrida.
            print(f"PR #{pr['number']}: borrador, no se re-evalúa")
            continue
        try:
            resultado = _reevaluar_pr(pr["number"], pr["headRefOid"], pr["headRefName"], ejecutar, esperar, presupuesto)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            # Su check quedaría fijo en el último veredicto, quizá verde con main roto (p. ej. GitHub rechaza el rerun a
            # los 30 días de la corrida inicial o tras 50 reruns). Seguir con los demás y bloquear éste.
            # El mensaje de GitHub (p. ej. el del límite de reruns) viene en el stderr de gh, no en el texto de la excepción.
            motivo = (getattr(exc, "stderr", None) or str(exc)).strip()
            print(f"PR #{pr['number']}: no se pudo re-evaluar: {motivo}", file=sys.stderr)
            if not _pasar_a_borrador(pr["number"], motivo, ejecutar):
                sin_proteccion += 1
            continue
        print(f"PR #{pr['number']}: {resultado}")
    # Rojo solo si algún PR quedó SIN protección (ni re-evaluado ni en borrador): ése es el que se puede mergear mal.
    return 1 if sin_proteccion else 0


def _pasar_a_borrador(numero: int, motivo: str, ejecutar: Ejecutor) -> bool:
    """Pasa el PR a borrador (no se puede mergear) y le explica por qué. Devuelve si quedó protegido."""
    try:
        ejecutar(["pr", "ready", str(numero), "--undo"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: PR #{numero}: tampoco se pudo pasar a borrador; queda SIN protección: {exc}", file=sys.stderr)
        return False
    try:
        ejecutar(["pr", "comment", str(numero), "--body", _aviso_de_borrador(motivo)])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # El borrador ya lo protege; el comentario solo explica.
        print(f"AVISO: PR #{numero}: quedó en borrador pero no se pudo comentar: {exc}", file=sys.stderr)
    print(f"PR #{numero}: pasado a borrador")
    return True


def _aviso_de_borrador(motivo: str) -> str:
    return (
        f"El gate «{NOMBRE_DEL_GATE}» de este PR no se pudo re-evaluar después de una corrida de CI sobre `main`, así "
        f"que su check podría estar viejo. Lo pasé a borrador para que no se mergee con ese veredicto.\n\n"
        f"Motivo: `{motivo[:300]}`\n\n"
        "Para seguir, marcá el PR como listo (**Ready for review**): el gate corre de cero con el estado actual de "
        "`main`. GitHub no deja re-correr una corrida pasados 30 días de la inicial ni más de 50 veces."
    )


def main(argumentos: Sequence[str], entorno: Mapping[str, str] | None = None, ejecutar: Ejecutor = _gh) -> int:
    entorno = os.environ if entorno is None else entorno
    subcomando = argumentos[0] if argumentos else ""
    if subcomando == "reevaluar":
        return reevaluar(ejecutar)
    if subcomando == "evaluar":
        pr = entorno.get("PR", "")
        if not pr.isdigit():
            print(f"ERROR: falta el número de PR en la variable PR (valor: {pr!r})", file=sys.stderr)
            return 2
        veredicto = evaluar(int(pr), ejecutar)
        print(veredicto.mensaje, file=sys.stdout if veredicto.verde else sys.stderr)
        return 0 if veredicto.verde else 1
    print("uso: ci_main_en_verde.py {evaluar|reevaluar}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
