"""Parser del output de ``dump_record_detail.pas`` (Fase 1 AI-assisted).

El script Pascal emite líneas pipe-delimited vía ``AddMessage`` (mismo
protocolo que ``list_all_conflicts.pas`` — '|' es inválido en filenames de
Windows, a diferencia de ','/':'):

    DUMP_BEGIN|<FormID>|<EditorID>|<RecordType>
    VERSION|<FormID>|<Plugin>|<0/1 es_ganador>
    ELEMENT|<FormID>|<Plugin>|<ElementPath>|<Valor>
    DUMP_END|<FormID>

Tolerante por diseño: xEdit intercala ruido (timestamps, "Processing: …") y
un crash a mitad de dump deja bloques sin cerrar — las líneas no-protocolo se
ignoran y un bloque sin ``DUMP_END`` se descarta (mejor sin contexto que con
contexto a medias). El advisor trabaja con ``dump=None`` en ese caso.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def normalize_form_id(form_id: str) -> str:
    """Normaliza un FormID a hex lowercase sin separadores (``00012eb7``)."""
    return form_id.strip().lower().replace(":", "").replace("0x", "")


@dataclass(frozen=True, slots=True)
class RecordVersion:
    """Una versión del record (el master o un override) con sus elementos.

    Attributes:
        plugin: Plugin que aporta esta versión.
        is_winner: True si es el winning override por load order.
        elements: Pares ``(path, valor)`` en el orden emitido por el script.
    """

    plugin: str
    is_winner: bool
    elements: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RecordDump:
    """Dump completo de un record conflictivo (todas sus versiones).

    Attributes:
        form_id: FormID normalizado (hex lowercase).
        editor_id: EditorID del record (puede ser vacío).
        record_type: Firma del record (``NPC_``, ``QUST``…).
        versions: Una entrada por plugin que toca el record.
    """

    form_id: str
    editor_id: str
    record_type: str
    versions: tuple[RecordVersion, ...] = field(default_factory=tuple)

    def differing_elements(self) -> list[tuple[str, dict[str, str]]]:
        """Elementos cuyos valores difieren entre versiones.

        Es el insumo del prompt compacto: en vez de mandar el record entero
        al LLM (5-20 KB), se mandan solo los subrecords en disputa.

        Returns:
            Lista de ``(path, {plugin: valor})`` — solo paths con ≥2 valores
            distintos entre las versiones que los definen.
        """
        by_path: dict[str, dict[str, str]] = {}
        for version in self.versions:
            for path, value in version.elements:
                by_path.setdefault(path, {})[version.plugin] = value
        differing: list[tuple[str, dict[str, str]]] = []
        for path, values in by_path.items():
            if len(set(values.values())) > 1:
                differing.append((path, values))
        return differing


def parse_dump_output(stdout: str) -> list[RecordDump]:
    """Parsea el stdout de ``dump_record_detail.pas`` a :class:`RecordDump`.

    Las líneas que no matchean el protocolo se ignoran (ruido de xEdit). Un
    bloque sin ``DUMP_END`` se descarta con warning — fail-closed: el advisor
    prefiere trabajar sin dump antes que con uno truncado.
    """
    dumps: list[RecordDump] = []

    current_form: str | None = None
    current_editor = ""
    current_type = ""
    # plugin -> (is_winner, elementos acumulados)
    current_versions: list[tuple[str, bool, list[tuple[str, str]]]] = []

    def _reset() -> None:
        nonlocal current_form, current_editor, current_type, current_versions
        current_form = None
        current_editor = ""
        current_type = ""
        current_versions = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        # xEdit prefija sus mensajes con "[HH:MM] " — quitarlo si está.
        if line.startswith("[") and "] " in line:
            line = line.split("] ", 1)[1]

        if line.startswith("DUMP_BEGIN|"):
            if current_form is not None:
                logger.warning("Dump de %s sin DUMP_END: se descarta (bloque truncado).", current_form)
            _reset()
            parts = line.split("|")
            if len(parts) < 4:
                logger.warning("DUMP_BEGIN malformado, se ignora: %r", raw_line)
                continue
            current_form = normalize_form_id(parts[1])
            current_editor = parts[2]
            current_type = parts[3]

        elif line.startswith("VERSION|") and current_form is not None:
            parts = line.split("|")
            if len(parts) < 4 or normalize_form_id(parts[1]) != current_form:
                logger.warning("VERSION malformada o fuera de bloque, se ignora: %r", raw_line)
                continue
            current_versions.append((parts[2], parts[3] == "1", []))

        elif line.startswith("ELEMENT|") and current_form is not None:
            # El valor puede contener '|' (paths de texturas, sentences de
            # diálogo): split acotado a los 4 primeros separadores.
            parts = line.split("|", 4)
            if len(parts) < 5 or normalize_form_id(parts[1]) != current_form:
                logger.warning("ELEMENT malformado o fuera de bloque, se ignora: %r", raw_line)
                continue
            plugin, path, value = parts[2], parts[3], parts[4]
            for version_plugin, _winner, elements in current_versions:
                if version_plugin == plugin:
                    elements.append((path, value))
                    break
            else:
                logger.warning("ELEMENT de un plugin sin VERSION previa, se ignora: %r", raw_line)

        elif line.startswith("DUMP_END|") and current_form is not None:
            parts = line.split("|")
            if len(parts) < 2 or normalize_form_id(parts[1]) != current_form:
                logger.warning("DUMP_END de otro form_id, se descarta el bloque abierto: %r", raw_line)
                _reset()
                continue
            dumps.append(
                RecordDump(
                    form_id=current_form,
                    editor_id=current_editor,
                    record_type=current_type,
                    versions=tuple(
                        RecordVersion(plugin=plugin, is_winner=winner, elements=tuple(elements))
                        for plugin, winner, elements in current_versions
                    ),
                )
            )
            _reset()

    if current_form is not None:
        logger.warning("Dump de %s sin DUMP_END al final del output: se descarta.", current_form)

    return dumps


__all__ = ["RecordDump", "RecordVersion", "normalize_form_id", "parse_dump_output"]
