"""T5-v2.1 — wiring productivo: censo de constructores y puertos satisfechos.

El modo directo del runner (``readiness=None``) NO corre el protocolo. Eso es
correcto para tests/rig y peligroso en producción, así que la garantía no es un
default sino un CENSO: se enumeran todos los sitios que construyen el runner en
``sky_claw/**`` **y en ``docs/validation/**``** (harnesses de rig comprometidos,
callers ejecutables reales — F2 de #590) y se exige que cada uno cablee el modo
de readiness. Un sitio nuevo —o uno que la olvide— rompe el test en vez de
salir a producción sin gate.

Es el mismo instrumento que el repo ya usa para el servicio
(``tests/test_dyndolod_workspace.py::test_censo_de_constructores_del_servicio_dyndolod``):
enumerar, no muestrear.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from sky_claw.local.tools.dyndolod_uia_ejecutor import (
    FLAG_HELPER_UIA,
    EjecutorGateEnProceso,
    EjecutorGatePorHelper,
    comando_del_helper,
)
from sky_claw.local.tools.dyndolod_uia_gate import ConfirmadorDeConfiguracion
from sky_claw.local.tools.dyndolod_uia_preflight import (
    LocalizadorDeProcesos,
    LocalizadorPsutil,
)
from sky_claw.local.tools.dyndolod_uia_windows import ObservadorUIAWindows, construir_observador_windows

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Sitios que construyen el runner. ``dyndolod_service.py`` es el productivo; el
#: harness del rig real PR-580 está COMPROMETIDO en ``docs/validation`` y es un
#: caller ejecutable de verdad — F2 (#590) midió que dejarlo fuera del censo
#: permitió que quedara roto (``DynDOLODRunner(cfg)`` sin ``readiness=``,
#: ``TypeError`` al correrlo) durante rondas enteras. El ejemplo del docstring
#: de ``dyndolod_runner`` no cuenta: es texto, no un ``ast.Call``.
CONSTRUCTORES_DEL_RUNNER: frozenset[str] = frozenset(
    {
        "sky_claw/local/tools/dyndolod_service.py",
        "docs/validation/2026-09-13_pr580_real_rig/run_phase.py",
    }
)

#: Árboles de fuentes Python EJECUTABLES comprometidos que el censo enumera.
#: ``docs/validation`` entra por F2 (#590): un harness de rig es código que se
#: corre contra la máquina real, no documentación narrativa — el censo no hace
#: grep sobre prosa, parsea ASTs de archivos ``.py`` y exige ``readiness=`` en
#: cada ``ast.Call`` a ``DynDOLODRunner``. ``tests/`` queda afuera a propósito:
#: sus constructores son dobles de prueba, no callers comprometidos.
CENSO_DE_CARPETAS_DEL_RUNNER: tuple[str, ...] = ("sky_claw", "docs/validation")

#: Sitios que construyen el SERVICIO. El preview es plan-only y declara el
#: opt-out explícito; el composition root cablea la capacidad productiva. El
#: censo exige ``readiness=`` en los dos: un ``None`` silencioso en un servicio
#: nuevo no puede dejar la etapa 9 sin gate.
CONSTRUCTORES_DEL_SERVICIO: frozenset[str] = frozenset(
    {
        "sky_claw/app/orchestrator/orchestration_composition.py",
        "sky_claw/app/orchestrator/preview/chain_preview_service.py",
    }
)

#: Archivos que participan del wiring de T5-v2.1.
COMPOSITION = RAIZ / "sky_claw" / "app" / "orchestrator" / "orchestration_composition.py"
ADAPTER = RAIZ / "sky_claw" / "app" / "orchestrator" / "dyndolod_readiness_hitl.py"
SERVICE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py"
RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"
GATE = RAIZ / "sky_claw" / "app" / "orchestrator" / "dyndolod_readiness_hitl.py"
