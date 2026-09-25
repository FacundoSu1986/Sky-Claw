"""Testigos de ejecución para runners mock de LOOT en tests (PR-2).

Un mock que simula "LOOT real corrió" DEBE declarar ``TESTIGO_FRESCO``: es
exactamente la evidencia que ``LOOTRunner`` adjunta cuando el proceso superó el
mutex ``LOOT.Shell.Instance`` y recreó ``LOOTDebugLog.txt`` (loot/loot 0.29.1
``src/gui/state/loot_state.cpp:105-106``). Un mock SIN testigo simula una
ejecución no atribuible (segunda instancia bloqueada, runner legacy sin data
root aislado) y el servicio la rechaza con ``EXECUTION_NOT_ATTRIBUTABLE``.

No existe un default "fresco" en ``LOOTResult``: ese default sería exactamente
el falso verde (rc 0 ⇒ éxito) que PR-2 cierra.
"""

from __future__ import annotations

from sky_claw.local.loot.execution_witness import LootExecutionWitness, LootExecutionWitnessState

TESTIGO_FRESCO = LootExecutionWitness(state=LootExecutionWitnessState.FRESH)
TESTIGO_INTACTO = LootExecutionWitness(state=LootExecutionWitnessState.SENTINEL_INTACT)
