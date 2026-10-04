"""Parser mínimo y fail-closed del formato VDF/ACF de Valve.

Alcance: lo necesario para ``libraryfolders.vdf`` y ``appmanifest_<appid>.acf``.
Políticas explícitas:

- sin ``eval``, sin shell, sin regex sobre la gramática (tokenizer de comillas);
- clave duplicada en el mismo scope ⇒ :class:`DuplicateKeyVdfError`;
- entrada malformada o codificación ilegible ⇒ :class:`MalformedVdfError`;
- campos desconocidos ⇒ tolerados (se preservan en el dict resultante);
- codificación ⇒ UTF-8 explícito, fail-closed ante bytes inválidos;
- escapes ⇒ sólo ``\\"`` y ``\\\\``; cualquier otro escape falla cerrado.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.frozen_runtime.errors import DuplicateKeyVdfError, MalformedVdfError


def _tokenize(text: str, *, source_label: str) -> list[str]:
    """Convierte el texto VDF en una secuencia de tokens (strings y llaves)."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "{":
            tokens.append("{")
            i += 1
            continue
        if ch == "}":
            tokens.append("}")
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            closed = False
            while i < n:
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise MalformedVdfError(f"{source_label}: escape al final de la entrada")
                    siguiente = text[i + 1]
                    if siguiente in ('"', "\\"):
                        buf.append(siguiente)
                        i += 2
                        continue
                    raise MalformedVdfError(f"{source_label}: escape desconocido '\\{siguiente}' en posición {i}")
                if c == '"':
                    i += 1
                    closed = True
                    break
                buf.append(c)
                i += 1
            if not closed:
                raise MalformedVdfError(f"{source_label}: comilla sin cerrar")
            tokens.append("".join(buf))
            continue
        raise MalformedVdfError(f"{source_label}: carácter inesperado {ch!r} en posición {i}")
    return tokens


def parse_vdf_text(text: str, *, source_label: str) -> dict[str, object]:
    """Parsea un documento VDF/ACF completo.

    El scope raíz no lleva llaves de cierre: el documento entero es un scope.
    ``object`` es ``str`` (valor escalar) o ``dict[str, object]`` (scope anidado).
    """
    tokens = _tokenize(text, source_label=source_label)
    pos = 0

    def parse_scope() -> dict[str, object]:
        nonlocal pos
        scope: dict[str, object] = {}
        while True:
            if pos >= len(tokens):
                return scope
            token = tokens[pos]
            if token == "}":
                pos += 1
                return scope
            if token == "{":
                raise MalformedVdfError(f"{source_label}: '{{' inesperado sin clave previa")
            key = token
            pos += 1
            if pos >= len(tokens):
                raise MalformedVdfError(f"{source_label}: la clave '{key}' quedó sin valor")
            value_token = tokens[pos]
            pos += 1
            if value_token == "{":
                if key in scope:
                    raise DuplicateKeyVdfError(
                        f"{source_label}: clave duplicada '{key}' en el mismo scope (fail-closed)"
                    )
                scope[key] = parse_scope()
            else:
                if key in scope:
                    raise DuplicateKeyVdfError(
                        f"{source_label}: clave duplicada '{key}' en el mismo scope (fail-closed)"
                    )
                scope[key] = value_token

    result = parse_scope()
    if pos != len(tokens):
        raise MalformedVdfError(f"{source_label}: token residual tras el scope raíz")
    return result


def parse_vdf_file(path: pathlib.Path, *, source_label: str | None = None) -> dict[str, object]:
    """Lee y parsea un archivo VDF/ACF con codificación UTF-8 explícita."""
    label = source_label or str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedVdfError(f"{label}: contenido no UTF-8: {exc}") from exc
    except OSError as exc:
        raise MalformedVdfError(f"{label}: no se pudo leer: {exc}") from exc
    return parse_vdf_text(text, source_label=label)
