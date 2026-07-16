"""Tests del prompt builder del advisor de IA (Fase 1).

Anclan el contrato del prompt: compacto (solo el diff, acotado), honesto
(declara cuando no hay dump) y con el SYSTEM_PROMPT exigiendo JSON puro del
esquema de PatchRecommendation.
"""

from __future__ import annotations

from sky_claw.local.ai.conflict_prompt import (
    MAX_DIFFERING_ELEMENTS,
    MAX_VALUE_CHARS,
    SYSTEM_PROMPT,
    build_prompt,
)
from sky_claw.local.ai.recommendation import ForumReference
from sky_claw.local.xedit.conflict_analyzer import RecordConflict
from sky_claw.local.xedit.record_dump_parser import RecordDump, RecordVersion


def _conflicto() -> RecordConflict:
    return RecordConflict(
        form_id="00012EB7",
        editor_id="BanditThief",
        record_type="NPC_",
        winner="Requiem.esp",
        losers=["Ordinator.esp", "RSChildren.esp"],
        severity="critical",
    )


def _dump() -> RecordDump:
    return RecordDump(
        form_id="00012eb7",
        editor_id="BanditThief",
        record_type="NPC_",
        versions=(
            RecordVersion(
                plugin="Requiem.esp",
                is_winner=True,
                elements=(("SNAM - Factions", "BanditFaction"), ("CNAM - Class", "BanditClass")),
            ),
            RecordVersion(
                plugin="Ordinator.esp",
                is_winner=False,
                elements=(("SNAM - Factions", "BanditFaction, ThievesGuild"), ("CNAM - Class", "BanditClass")),
            ),
        ),
    )


def test_system_prompt_exige_json_puro_y_esquema() -> None:
    assert "SINGLE JSON object" in SYSTEM_PROMPT
    assert '"severity"' in SYSTEM_PROMPT
    assert "manual_only" in SYSTEM_PROMPT
    # Advisory puro: el system prompt lo dice explícitamente.
    assert "never mutate" in SYSTEM_PROMPT.lower()


def test_prompt_contiene_los_datos_del_conflicto() -> None:
    prompt = build_prompt(_conflicto(), _dump())

    assert "NPC_ 00012EB7" in prompt
    assert "BanditThief" in prompt
    assert "WINNER (by load order): Requiem.esp" in prompt
    assert "Ordinator.esp" in prompt and "RSChildren.esp" in prompt


def test_prompt_con_dump_lista_solo_los_subrecords_en_disputa() -> None:
    prompt = build_prompt(_conflicto(), _dump())

    assert "DIFFERING SUBRECORDS (1 total):" in prompt
    assert "SNAM - Factions" in prompt
    # CNAM no difiere entre versiones: no debe viajar al LLM.
    assert "CNAM - Class" not in prompt


def test_prompt_sin_dump_lo_declara() -> None:
    prompt = build_prompt(_conflicto(), dump=None)

    assert "NO RECORD DUMP AVAILABLE" in prompt
    assert "DIFFERING SUBRECORDS" not in prompt


def test_prompt_acota_los_elementos_en_disputa() -> None:
    """Un QUST patológico no debe reventar el presupuesto de tokens."""
    total = MAX_DIFFERING_ELEMENTS + 15
    versions = (
        RecordVersion(plugin="A.esp", is_winner=True, elements=tuple((f"EL{i}", "a") for i in range(total))),
        RecordVersion(plugin="B.esp", is_winner=False, elements=tuple((f"EL{i}", "b") for i in range(total))),
    )
    dump = RecordDump(form_id="00012eb7", editor_id="Q", record_type="QUST", versions=versions)

    prompt = build_prompt(_conflicto(), dump)

    assert f"({total} total)" in prompt
    assert f"…{total - MAX_DIFFERING_ELEMENTS} more differing subrecords omitted" in prompt
    # Las líneas de elementos emitidas son exactamente el tope.
    assert sum(1 for line in prompt.splitlines() if line.startswith("- EL")) == MAX_DIFFERING_ELEMENTS


def test_prompt_trunca_valores_gigantes() -> None:
    """Un VMAD/CTDA enorme viaja truncado, no entero."""
    gigante = "x" * (MAX_VALUE_CHARS * 3)
    dump = RecordDump(
        form_id="00012eb7",
        editor_id="Q",
        record_type="QUST",
        versions=(
            RecordVersion(plugin="A.esp", is_winner=True, elements=(("VMAD", gigante),)),
            RecordVersion(plugin="B.esp", is_winner=False, elements=(("VMAD", "corto"),)),
        ),
    )

    prompt = build_prompt(_conflicto(), dump)

    assert gigante not in prompt
    assert "x" * (MAX_VALUE_CHARS - 1) + "…" in prompt


def test_prompt_incluye_referencias_de_foros_cuando_hay() -> None:
    referencias = (
        ForumReference(
            title="Requiem + Ordinator patch thread",
            url="https://example.org/hilo",
            source="reddit",
            snippet="Forward the factions from Ordinator.",
        ),
    )

    prompt = build_prompt(_conflicto(), _dump(), forum_references=referencias)

    assert "KNOWN COMMUNITY SOLUTIONS:" in prompt
    assert "[reddit] Requiem + Ordinator patch thread" in prompt
    assert "Forward the factions" in prompt


def test_prompt_sin_referencias_no_menciona_foros() -> None:
    prompt = build_prompt(_conflicto(), _dump())

    assert "KNOWN COMMUNITY SOLUTIONS" not in prompt


def test_prompt_termina_con_la_pregunta_y_el_recordatorio_de_esquema() -> None:
    prompt = build_prompt(_conflicto(), _dump())

    assert prompt.rstrip().endswith("Answer with the JSON schema only.")
