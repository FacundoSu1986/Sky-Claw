"""Perfil aislado: un proceso con ``SKYCLAW_ISOLATED`` no toca el keyring ni descubre la máquina real.

Por qué existe (R0, rig real, 2026-11-16/17): un ``AppContext`` armado con un ``HOME`` aislado igual leyó el token de
Telegram del operador del keyring GLOBAL de Windows y ejecutó long-polling, y el zero-config buscó Skyrim en la máquina
real, lo persistió en el ``config.toml`` aislado y lo admitió como raíz del sandbox. Aislar ``HOME`` no alcanza: el
keyring, el registro de Windows y las bibliotecas de Steam son del USUARIO, no del perfil.

La propiedad: *el aislamiento solo protege si TODOS los accesos participan*. Por eso no hay un parche por llamada:

* **keyring**: todas las llamadas del producto pasan por el backend global de ``keyring``. Este módulo instala UN backend
  que **falla** (``keyring.backends.fail``) y así cubre por construcción cada sitio, incluidos los que se agreguen. Los
  sitios ya tratan ese estado como «no configurado» (es el de un Linux headless sin backend).
* **descubrimiento de la máquina real** (``AutoDetector`` y ``EnvironmentScanner``): consultan ``perfil_aislado()`` y
  devuelven «no encontrado». Una ruta que el perfil fijó en su config sigue valiendo: se apaga el descubrimiento, no la
  configuración.

Se instala el backend que FALLA y no el nulo (``keyring.backends.null``) a propósito: con el nulo ``set_password``
«funciona» sin guardar nada y ``Config._load_from_keyring`` creería haber migrado el secreto al keyring y lo BORRARÍA del
TOML. Con el que falla el secreto se queda donde estaba (contrato fail-closed de AGENTS.md, «Secretos en
``Config.save()``»).

El aislamiento es irreversible dentro del proceso (el backend no se «des-instala»): un perfil aislado no vuelve a ver el
keyring real por apagar la variable después. Anclas: ``tests/test_aislamiento_perfil.py``.

Este módulo lo importa ``sky_claw/__init__.py`` en CADA arranque: no importa nada fuera de la librería estándar (``keyring``
se importa dentro de ``aplicar_aislamiento`` y solo si el interruptor está activo), para que los workers livianos no
arrastren ``config``/``keyring``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Variable de entorno que activa el perfil aislado. La escriben el runbook del rig y los arneses: no se renombra a la
#: ligera (``tests/test_aislamiento_perfil.py`` congela el nombre).
ENV_PERFIL_AISLADO = "SKYCLAW_ISOLATED"

#: Lo que APAGA el interruptor. Cualquier otro valor no vacío lo ENCIENDE: es un interruptor de seguridad y un typo
#: («ture») no puede dejar al proceso tocando la máquina real (fail-closed).
_VALORES_QUE_LO_APAGAN = frozenset({"", "0", "false", "no", "off", "n", "f"})


def perfil_aislado() -> bool:
    """¿Este proceso corre con el perfil aislado? Sin la variable (o con un valor «apagado») es ``False``."""
    valor = os.environ.get(ENV_PERFIL_AISLADO)
    if valor is None:
        return False
    return valor.strip().casefold() not in _VALORES_QUE_LO_APAGAN


def aplicar_aislamiento() -> bool:
    """Instala el backend de keyring que falla si el perfil es aislado. Devuelve si el aislamiento quedó activo.

    Idempotente y barato: sin el interruptor no importa ``keyring`` ni toca nada. Con el interruptor reinstala el backend
    en cada llamada (a propósito: reemplaza cualquier backend que alguien haya fijado antes).
    """
    if not perfil_aislado():
        return False
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        # Sin la librería no hay keyring que aislar (un worker mínimo): nada que hacer.
        return False
    # `keyring` no tipa el constructor de sus backends (stub incompleto): mypy lo ve como llamada sin tipar. Es el único
    # silenciado de este módulo y no afecta lo que se instala (el contrato es `KeyringBackend`).
    keyring.set_keyring(fail.Keyring())  # type: ignore[no-untyped-call]
    logger.info("Perfil aislado (%s): el keyring del usuario queda fuera de este proceso.", ENV_PERFIL_AISLADO)
    return True
