"""IniEditor — edición byte-fiel de INIs de Skyrim/NGIO (PR-1 del plan grass cache).

Pieza fundacional del Stage 8 (No Grass In Objects) del SOP: escribe
``GrassControl.ini`` (sintaxis plana ``Key = Value`` de NGIO-NG, sin
secciones), ``Skyrim.ini``/``SkyrimPrefs.ini`` (secciones clásicas
``[Grass]``) y ``SSEDisplayTweaks.ini``.

Disciplina heredada de ``profile_sandbox`` (ADR 0002): **byte-fidelidad**.
El archivo no se re-serializa (``configparser`` normalizaría espaciado,
mayúsculas y comentarios): se opera línea a línea y solo se toca la línea
editada — BOM UTF-8, estilo de EOL (CRLF/LF), comentarios y espaciado del
resto quedan byte-idénticos. La decodificación usa ``surrogateescape`` para
que bytes no-UTF-8 (INIs ANSI del juego) sobrevivan el roundtrip intactos.

Semántica INI de Windows: clave y sección matchean case-insensitive, pero al
reemplazar se preserva el spelling del archivo. Las líneas comentadas
(``;``/``#``) jamás matchean.

Escrituras: atómicas (tmp → ``os.replace``, mismo patrón que
``_write_modlist_atomic`` de ``vfs.py``); antes de cada mutación real se
persiste ``<archivo>.bak`` con los bytes previos (red de seguridad local — el
rollback primario del ritual grass es descartar el clon/mod de config entero).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from dataclasses import dataclass

_BOM = b"\xef\xbb\xbf"
_COMMENT_PREFIXES = (";", "#")
#: EOL para archivos nuevos o sin EOL detectable (convención Windows/Skyrim).
_DEFAULT_EOL = "\r\n"
#: Separador para claves nuevas cuando el scope no tiene precedente que imitar:
#: NGIO-NG escribe "Key = Value" (plano); los INIs del juego usan "key=value".
_DEFAULT_SEP_FLAT = " = "
_DEFAULT_SEP_SECTION = "="


@dataclass(frozen=True, slots=True)
class IniEditResult:
    """Resultado de una escritura del editor.

    Attributes:
        changed: ``True`` si el archivo fue modificado (``False`` = idempotente).
        previous_value: Valor previo de la clave (``None`` si no existía).
        backup_path: Ruta del ``.bak`` con los bytes previos a esta escritura,
            o ``None`` si no hubo escritura o el archivo no existía.
    """

    changed: bool
    previous_value: str | None
    backup_path: pathlib.Path | None


@dataclass(frozen=True, slots=True)
class _Line:
    """Una línea física: cuerpo sin EOL + su EOL original (``""`` si no tiene)."""

    body: str
    eol: str


@dataclass(frozen=True, slots=True)
class _KeyHit:
    """Match de una clave: índice de línea + descomposición para reemplazo."""

    index: int
    #: Todo lo previo al valor: "<key con su espaciado>=<whitespace>" — se
    #: preserva verbatim al reemplazar (spelling y espaciado originales).
    prefix: str
    value: str


class IniEditor:
    """Editor byte-fiel de archivos INI (con secciones o sintaxis plana NGIO)."""

    async def get(self, path: pathlib.Path, key: str, section: str | None = None) -> str | None:
        """Lee el valor de *key* (en ``[section]``, o en el área plana si es ``None``).

        Nunca muta el archivo. Devuelve el valor sin whitespace exterior, o
        ``None`` si la clave no existe. Lanza ``FileNotFoundError`` si el
        archivo no existe (fail-closed: un path mal resuelto no es "sin valor").
        """
        return await asyncio.to_thread(self._get_sync, path, key, section)

    async def set(
        self,
        path: pathlib.Path,
        key: str,
        value: str,
        section: str | None = None,
    ) -> IniEditResult:
        """Escribe ``key = value`` preservando byte-fidelidad del resto del archivo.

        - Clave existente: reemplaza solo el valor (spelling y espaciado intactos).
        - Clave faltante: la agrega al final de su sección (o del área plana),
          imitando el estilo de separador del scope.
        - Sección faltante: la crea al final del archivo.
        - Archivo inexistente: lo crea (sin BOM, CRLF).
        - Mismo valor: no reescribe ni genera backup (``changed=False``).

        *value* se escribe verbatim (API de strings: ``"20272.0000"`` no se
        normaliza).
        """
        return await asyncio.to_thread(self._set_sync, path, key, value, section)

    # ------------------------------------------------------------------
    # Núcleo síncrono (corre en to_thread: I/O de archivos chicos)
    # ------------------------------------------------------------------

    def _get_sync(self, path: pathlib.Path, key: str, section: str | None) -> str | None:
        _bom, lines = _read_lines(path.read_bytes())
        hit = _find_key(lines, key, section)
        return hit.value if hit is not None else None

    def _set_sync(
        self,
        path: pathlib.Path,
        key: str,
        value: str,
        section: str | None,
    ) -> IniEditResult:
        try:
            original = path.read_bytes()
        except FileNotFoundError:
            original = None

        bom, lines = _read_lines(original or b"")
        hit = _find_key(lines, key, section)

        if hit is not None:
            if hit.value == value:
                return IniEditResult(changed=False, previous_value=hit.value, backup_path=None)
            lines[hit.index] = _Line(body=f"{hit.prefix}{value}", eol=lines[hit.index].eol)
            previous = hit.value
        else:
            _insert_key(lines, key, value, section)
            previous = None

        backup_path: pathlib.Path | None = None
        if original is not None:
            backup_path = path.with_name(path.name + ".bak")
            _write_atomic(backup_path, original)

        _write_atomic(path, _serialize(bom, lines))
        return IniEditResult(changed=True, previous_value=previous, backup_path=backup_path)


# ---------------------------------------------------------------------------
# Parsing y serialización (funciones puras)
# ---------------------------------------------------------------------------


def _read_lines(data: bytes) -> tuple[bool, list[_Line]]:
    """Descompone *data* en (tiene_bom, líneas) con roundtrip byte-exacto.

    Se separa manualmente en ``\\r\\n``/``\\n``/``\\r`` (no ``splitlines()``:
    partiría en NEL/U+2028 y rompería la fidelidad de INIs con bytes exóticos).
    """
    has_bom = data.startswith(_BOM)
    if has_bom:
        data = data[len(_BOM) :]
    text = data.decode("utf-8", errors="surrogateescape")

    lines: list[_Line] = []
    body_start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\r":
            eol = "\r\n" if text[i + 1 : i + 2] == "\n" else "\r"
            lines.append(_Line(body=text[body_start:i], eol=eol))
            i += len(eol)
            body_start = i
        elif ch == "\n":
            lines.append(_Line(body=text[body_start:i], eol="\n"))
            i += 1
            body_start = i
        else:
            i += 1
    if body_start < len(text):
        lines.append(_Line(body=text[body_start:], eol=""))
    return has_bom, lines


def _serialize(bom: bool, lines: list[_Line]) -> bytes:
    text = "".join(f"{line.body}{line.eol}" for line in lines)
    data = text.encode("utf-8", errors="surrogateescape")
    return _BOM + data if bom else data


def _section_of(body: str) -> str | None:
    """Nombre de sección si *body* es un header ``[Nombre]``, si no ``None``."""
    stripped = body.strip()
    if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2:
        return stripped[1:-1].strip()
    return None


def _split_key_line(body: str) -> tuple[str, str, str] | None:
    """Descompone una línea ``clave=valor`` en (prefix, clave_stripped, valor_stripped).

    ``prefix`` = clave con su espaciado + ``=`` + whitespace previo al valor —
    exactamente lo que se preserva al reemplazar. Comentarios y líneas sin
    ``=`` devuelven ``None``.
    """
    stripped = body.strip()
    if not stripped or stripped.startswith(_COMMENT_PREFIXES) or "=" not in body:
        return None
    raw_key, raw_value = body.split("=", 1)
    key = raw_key.strip()
    if not key:
        return None
    lead_ws = raw_value[: len(raw_value) - len(raw_value.lstrip())]
    return f"{raw_key}={lead_ws}", key, raw_value.strip()


def _find_key(lines: list[_Line], key: str, section: str | None) -> _KeyHit | None:
    target = section.lower() if section is not None else None
    current: str | None = None
    wanted = key.lower()
    for index, line in enumerate(lines):
        header = _section_of(line.body)
        if header is not None:
            current = header.lower()
            continue
        if current != target:
            continue
        parsed = _split_key_line(line.body)
        if parsed is not None and parsed[1].lower() == wanted:
            prefix, _, value = parsed
            return _KeyHit(index=index, prefix=prefix, value=value)
    return None


def _detect_eol(lines: list[_Line]) -> str:
    for line in lines:
        if line.eol:
            return line.eol
    return _DEFAULT_EOL


def _scope_bounds(lines: list[_Line], section: str | None) -> tuple[int, int] | None:
    """(inicio, fin) de las líneas del scope (sin el header), o ``None`` si no existe.

    El scope plano (``section=None``) va del inicio del archivo al primer
    header; una sección va de su header al siguiente header o EOF.
    """
    if section is None:
        end = next((i for i, line in enumerate(lines) if _section_of(line.body) is not None), len(lines))
        return 0, end

    target = section.lower()
    start: int | None = None
    for i, line in enumerate(lines):
        header = _section_of(line.body)
        if header is None:
            continue
        if start is not None:
            return start, i
        if header.lower() == target:
            start = i + 1
    return (start, len(lines)) if start is not None else None


def _scope_separator(lines: list[_Line], start: int, end: int, section: str | None) -> str:
    """Separador a imitar para claves nuevas: el de la última clave del scope."""
    for i in range(end - 1, start - 1, -1):
        parsed = _split_key_line(lines[i].body)
        if parsed is not None:
            raw_key, _rest = lines[i].body.split("=", 1)
            trail_ws = raw_key[len(raw_key.rstrip()) :]
            lead_ws = _rest[: len(_rest) - len(_rest.lstrip())]
            return f"{trail_ws}={lead_ws}"
    return _DEFAULT_SEP_FLAT if section is None else _DEFAULT_SEP_SECTION


def _insert_key(lines: list[_Line], key: str, value: str, section: str | None) -> None:
    """Inserta ``key<sep>value`` al final de su scope (creando la sección si falta)."""
    eol = _detect_eol(lines)
    bounds = _scope_bounds(lines, section)

    if bounds is None:
        # Sección inexistente: se crea al final del archivo.
        if lines and not lines[-1].eol:
            lines[-1] = _Line(body=lines[-1].body, eol=eol)
        assert section is not None  # bounds solo es None para secciones con nombre
        lines.append(_Line(body=f"[{section}]", eol=eol))
        lines.append(_Line(body=f"{key}{_DEFAULT_SEP_SECTION}{value}", eol=eol))
        return

    start, end = bounds
    sep = _scope_separator(lines, start, end, section)
    # Retroceder sobre líneas en blanco: la clave nueva queda pegada al último
    # contenido real de la sección, no tras el hueco que la separa de la próxima.
    insert_at = end
    while insert_at > start and not lines[insert_at - 1].body.strip():
        insert_at -= 1
    if insert_at > 0 and not lines[insert_at - 1].eol:
        lines[insert_at - 1] = _Line(body=lines[insert_at - 1].body, eol=eol)
    lines.insert(insert_at, _Line(body=f"{key}{sep}{value}", eol=eol))


# ---------------------------------------------------------------------------
# Escritura atómica
# ---------------------------------------------------------------------------


def _write_atomic(path: pathlib.Path, data: bytes) -> None:
    """Escritura tmp → ``os.replace`` (mismo patrón que ``_write_modlist_atomic``).

    Un lector externo (MO2, el juego, un watcher) nunca ve un archivo parcial.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - solo si os.replace falló
            tmp.unlink(missing_ok=True)


__all__ = ["IniEditResult", "IniEditor"]
