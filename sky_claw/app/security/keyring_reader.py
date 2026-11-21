"""Lectura de credenciales del llavero con diagnóstico seguro del backend."""

from __future__ import annotations

from logging import Logger

import keyring
import keyring.errors


def leer_secreto_de_keyring(name: str, *, logger: Logger) -> str | None:
    """Devuelve el secreto o None y clasifica el fallo sin volcar la excepción.

    Algunos backends de Windows propagan errores nativos sin envolverlos en
    KeyringError. El llamador puede continuar con su fallback incluso entonces,
    y el mensaje de la excepción puede contener datos sensibles.
    """
    try:
        return keyring.get_password("sky_claw", name)
    except keyring.errors.NoKeyringError:
        logger.warning("keyring: no hay backend disponible; %r se trata como no configurado", name)
    except (keyring.errors.KeyringError, OSError) as exc:
        logger.warning(
            "keyring: el backend falló (%s) al leer %r; se trata como no configurado", type(exc).__name__, name
        )
    except Exception as exc:  # noqa: BLE001 — último recurso, distinguible por nivel ERROR (ver docstring)
        logger.error(
            "keyring: error inesperado del backend (%s) al leer %r; se trata como no configurado",
            type(exc).__name__,
            name,
        )
    return None
