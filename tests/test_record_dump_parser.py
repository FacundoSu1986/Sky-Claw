"""Tests del parser de ``dump_record_detail.pas`` (Fase 1 AI-assisted).

El protocolo pipe-delimited está documentado en ambos lados
(``record_dump_parser.py`` ↔ ``dump_record_detail.pas``) — estos tests anclan
el lado Python: tolerancia al ruido de xEdit, descarte de bloques truncados y
el cálculo de elementos en disputa que alimenta el prompt del advisor.
"""

from __future__ import annotations

from sky_claw.local.xedit.record_dump_parser import (
    RecordDump,
    RecordVersion,
    normalize_form_id,
    parse_dump_output,
)

DUMP_COMPLETO = """\
DUMP_BEGIN|00012EB7|BanditThief|NPC_
VERSION|00012EB7|Skyrim.esm|0
ELEMENT|00012EB7|Skyrim.esm|SNAM - Factions|BanditFaction
ELEMENT|00012EB7|Skyrim.esm|CNAM - Class|BanditClass
VERSION|00012EB7|Requiem.esp|1
ELEMENT|00012EB7|Requiem.esp|SNAM - Factions|BanditFaction, ThievesGuild
ELEMENT|00012EB7|Requiem.esp|CNAM - Class|BanditClass
DUMP_END|00012EB7
"""


def test_parsea_un_dump_completo() -> None:
    dumps = parse_dump_output(DUMP_COMPLETO)

    assert len(dumps) == 1
    dump = dumps[0]
    assert dump.form_id == "00012eb7"  # normalizado lowercase
    assert dump.editor_id == "BanditThief"
    assert dump.record_type == "NPC_"
    assert len(dump.versions) == 2
    assert dump.versions[0] == RecordVersion(
        plugin="Skyrim.esm",
        is_winner=False,
        elements=(("SNAM - Factions", "BanditFaction"), ("CNAM - Class", "BanditClass")),
    )
    assert dump.versions[1].is_winner is True


def test_ignora_ruido_de_xedit_entre_lineas() -> None:
    """xEdit intercala timestamps y mensajes de progreso — no son protocolo."""
    con_ruido = "[00:01] Processing: Skyrim.esm\nBackground loader finished\n" + DUMP_COMPLETO + "[00:05] Done.\n"

    dumps = parse_dump_output(con_ruido)

    assert len(dumps) == 1
    assert dumps[0].form_id == "00012eb7"


def test_quita_el_prefijo_de_timestamp_de_las_lineas_de_protocolo() -> None:
    """AddMessage puede salir prefijado con [HH:MM] según la build de xEdit."""
    prefijado = "\n".join(f"[00:02] {line}" for line in DUMP_COMPLETO.splitlines())

    dumps = parse_dump_output(prefijado)

    assert len(dumps) == 1
    assert dumps[0].versions[1].plugin == "Requiem.esp"


def test_bloque_sin_dump_end_se_descarta() -> None:
    """Fail-closed: mejor sin contexto que con un dump a medias (crash de xEdit)."""
    truncado = "\n".join(DUMP_COMPLETO.splitlines()[:-1])  # sin DUMP_END

    assert parse_dump_output(truncado) == []


def test_bloque_truncado_no_contamina_al_siguiente() -> None:
    truncado_mas_completo = "DUMP_BEGIN|0000AAAA|Roto|QUST\nVERSION|0000AAAA|A.esp|1\n" + DUMP_COMPLETO

    dumps = parse_dump_output(truncado_mas_completo)

    assert [d.form_id for d in dumps] == ["00012eb7"]


def test_valor_con_pipes_no_rompe_el_parseo() -> None:
    """El valor es el último campo: los '|' extra pertenecen al valor."""
    salida = (
        "DUMP_BEGIN|0000BBBB|Dial|INFO\n"
        "VERSION|0000BBBB|Mod.esp|1\n"
        "ELEMENT|0000BBBB|Mod.esp|NAM1 - Response|Te dije: cuidado | o algo así\n"
        "DUMP_END|0000BBBB\n"
    )

    dumps = parse_dump_output(salida)

    assert dumps[0].versions[0].elements == (("NAM1 - Response", "Te dije: cuidado | o algo así"),)


def test_lineas_malformadas_se_ignoran_sin_tumbar_el_parser() -> None:
    salida = (
        "DUMP_BEGIN|0000CCCC\n"  # sin editor_id ni tipo → se ignora el BEGIN
        "ELEMENT|0000CCCC|Mod.esp|X\n" + DUMP_COMPLETO  # fuera de bloque → se ignora
    )

    dumps = parse_dump_output(salida)

    assert [d.form_id for d in dumps] == ["00012eb7"]


def test_output_vacio_devuelve_lista_vacia() -> None:
    assert parse_dump_output("") == []


def test_differing_elements_solo_reporta_paths_en_disputa() -> None:
    dump = parse_dump_output(DUMP_COMPLETO)[0]

    en_disputa = dump.differing_elements()

    assert len(en_disputa) == 1
    path, valores = en_disputa[0]
    assert path == "SNAM - Factions"
    assert valores == {
        "Skyrim.esm": "BanditFaction",
        "Requiem.esp": "BanditFaction, ThievesGuild",
    }


def test_differing_elements_sin_versiones_es_vacio() -> None:
    dump = RecordDump(form_id="00000001", editor_id="X", record_type="NPC_", versions=())

    assert dump.differing_elements() == []


def test_normalize_form_id_acepta_variantes() -> None:
    assert normalize_form_id("00012EB7") == "00012eb7"
    assert normalize_form_id("0x00012eb7") == "00012eb7"
    assert normalize_form_id("  00:012EB7 ") == "00012eb7"
