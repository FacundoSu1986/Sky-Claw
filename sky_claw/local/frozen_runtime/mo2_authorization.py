"""Vínculo verificable entre una aprobación y la instancia/INI de MO2 que el binder consume (P7).

Por qué existe
--------------
El adaptador HITL derivaba la evidencia del INI y de la instancia MO2 y la comprometía en
``approval_provenance`` (campo 14 del ``ApprovalScope``, cubierto por el digest que el propietario aprueba
y persistido en el witness). Pero el binder —único consumidor— no leía nada de eso: tomaba su PROPIA primera
lectura del INI como ancla de toda su cadena (revalidación bajo lock → snapshot → huella bajo exclusión) y
aceptaba cualquier instancia GLOBAL activa (la que el registro de MO2 marcara como actual en ese momento): el
preflight exige que la instancia sea la global activa, pero nada ataba la aprobación a CUÁL. La verificación vivía en el adaptador, y un llamador directo del binder
(o un tercero que cambiaba el INI en la ventana entre «el dueño aprobó» y «el binder mira») la saltaba.

Contrato
--------
``approval_provenance`` lleva UN segmento ``mo2auth=<sha256>`` que compromete, con un digest canónico:

* la instancia de MO2 (ruta canónica) y su identidad verificada (``active_global``);
* el SHA-256 de los bytes completos de ``modorganizer.ini``;
* la política de transición y el runtime saliente declarados.

El binder lo lee del WITNESS DURABLE (no de lo que aporta el llamador), recomputa el digest desde SU propia
observación y exige igualdad, antes de quemar la aprobación y de nuevo bajo el ``participant_lock``. Como
``approval_provenance`` ya entra en el digest del scope, el segmento queda comprometido por la aprobación del
propietario sin un campo nuevo, sin versión nueva del esquema y sin migrar aprobaciones.

Compatibilidad (decisión explícita, ver :func:`es_vinculo_obligatorio`)
-----------------------------------------------------------------------
Un segmento PRESENTE se verifica siempre. Un segmento AUSENTE sólo se tolera cuando la aprobación no puede
ser del camino productivo ni del contrato P7-A5: sin ``solicitud_binding`` (sólo las fixtures del núcleo
durable lo omiten: ``emitir_aprobacion`` está bloqueada y ``emitir_aprobacion_autorizada`` exige el binding
completo) y para una transición que no migra ejecutables. Los witness v1/v2 ni siquiera se leen (se rechazan
fail-closed), así que no hay un witness «anterior» que pueda evitar el chequeo.

No hace E/S y no importa el binder ni el adaptador HITL: ambos lo consumen sin ciclo.
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Final

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
from sky_claw.local.frozen_runtime.journal import digest_canonico

#: Versión del contenido que compromete el digest del segmento.
MO2_AUTH_SCHEMA_VERSION: Final = 1

#: Clave del segmento dentro de ``approval_provenance``.
CLAVE_SEGMENTO: Final = "mo2auth"

_MARCADOR: Final = f"{CLAVE_SEGMENTO}="
_SEGMENTO_RE: Final = re.compile(rf"(?:^|:){CLAVE_SEGMENTO}=([0-9a-f]{{64}})(?=:|$)")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")

#: La identidad de instancia que el adaptador HITL exige para presentar una aprobación.
IDENTIDAD_AUTORIZADA: Final = "active_global"


class Mo2AuthorizationError(FrozenRuntimeError):
    """Base de los rechazos del vínculo aprobación ↔ instancia/INI de MO2. Todos son fail-closed."""


class Mo2AuthorizationMismatchError(Mo2AuthorizationError):
    """La instancia, la identidad, el INI, la política o el runtime observados no son los aprobados."""


class Mo2AuthorizationAusenteError(Mo2AuthorizationError):
    """El vínculo es obligatorio para esta aprobación y su procedencia no lo trae."""


class Mo2AuthorizationMalformadaError(Mo2AuthorizationError):
    """El segmento está duplicado, mal formado o los datos para calcularlo no son válidos."""


def clave_de_ruta(ruta: str | os.PathLike[str] | None) -> str | None:
    """Ruta canónica comparable. Misma normalización que el binder (``_norm``) y el adaptador (``_clave``)."""
    if ruta is None:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


def calcular_token_mo2(
    *,
    mo2_instance_dir: str | os.PathLike[str],
    mo2_instance_identity: object,
    mo2_ini_sha256: str,
    transition_policy: object,
    outgoing_manual_runtime_root: str | os.PathLike[str] | None,
) -> str:
    """Digest canónico (SHA-256 hex) de lo que la aprobación liga sobre la instancia y el INI.

    ``mo2_ini_sha256`` es el SHA-256 hexadecimal de los bytes COMPLETOS del INI (no un digest envuelto): es
    lo que el binder también obtiene de ``binder_recovery.huella`` sobre los bytes que lee. Los enums se
    aceptan por su ``.value`` para que ningún llamador tenga que importar sus tipos.
    """
    if not isinstance(mo2_ini_sha256, str) or _SHA256_RE.fullmatch(mo2_ini_sha256) is None:
        raise Mo2AuthorizationMalformadaError("mo2_ini_sha256 debe ser un SHA-256 hexadecimal en minúsculas")
    identidad = getattr(mo2_instance_identity, "value", mo2_instance_identity)
    politica = getattr(transition_policy, "value", transition_policy)
    if not isinstance(identidad, str) or not identidad:
        raise Mo2AuthorizationMalformadaError("la identidad de la instancia MO2 debe ser un texto no vacío")
    if not isinstance(politica, str) or not politica:
        raise Mo2AuthorizationMalformadaError("la política de transición debe ser un texto no vacío")
    return digest_canonico(
        {
            "schema_version": MO2_AUTH_SCHEMA_VERSION,
            "kind": "mo2-authorization",
            "mo2_instance_dir": clave_de_ruta(pathlib.Path(mo2_instance_dir)),
            "mo2_instance_identity": identidad,
            "mo2_ini_sha256": mo2_ini_sha256,
            "transition_policy": politica,
            "outgoing_manual_runtime_root": clave_de_ruta(outgoing_manual_runtime_root),
        }
    )


def segmento_de_procedencia(token: str) -> str:
    """Segmento que el adaptador agrega a ``approval_provenance`` (``mo2auth=<sha256>``)."""
    if not isinstance(token, str) or _SHA256_RE.fullmatch(token) is None:
        raise Mo2AuthorizationMalformadaError("el token MO2 debe ser un SHA-256 hexadecimal en minúsculas")
    return f"{_MARCADOR}{token}"


def token_comprometido(approval_provenance: str) -> str | None:
    """Token comprometido en una procedencia, ``None`` si no lo trae, y error si la forma es ambigua.

    Exige EXACTAMENTE cero o un segmento. Los segmentos legibles de la procedencia se escapan con
    ``urllib.parse.quote`` (``=`` y ``:`` quedan como ``%3D``/``%3A``), así que ninguna ruta ni identificador
    puede fabricar un ``mo2auth=`` ajeno; un segundo marcador sólo puede ser una procedencia manipulada y no
    se «elige» uno de los dos.
    """
    if not isinstance(approval_provenance, str):
        raise Mo2AuthorizationMalformadaError("approval_provenance debe ser un texto")
    ocurrencias = approval_provenance.count(_MARCADOR)
    if ocurrencias == 0:
        return None
    if ocurrencias > 1:
        raise Mo2AuthorizationMalformadaError(
            f"la procedencia trae {ocurrencias} segmentos {CLAVE_SEGMENTO}: forma ambigua (fail-closed)"
        )
    coincidencia = _SEGMENTO_RE.search(approval_provenance)
    if coincidencia is None:
        raise Mo2AuthorizationMalformadaError(
            f"el segmento {CLAVE_SEGMENTO} de la procedencia no es un SHA-256 delimitado (fail-closed)"
        )
    return coincidencia.group(1)


def es_vinculo_obligatorio(*, solicitud_binding_presente: bool, transicion_p7_a5: bool) -> bool:
    """¿La procedencia DEBE traer el vínculo? Tabla cerrada, anclada por igualdad literal en los tests.

    * Aprobación con ``solicitud_binding`` ⇒ la emitió la superficie HITL (único camino productivo): obligatorio.
    * Transición que migra ejecutables (política Stock o runtime saliente, el contrato P7-A5) ⇒ obligatorio,
      con o sin binding: es la operación que muta ``[customExecutables]`` y no admite una aprobación sin vínculo.
    * Resto (fixture del núcleo sin binding y transición legacy de ``gamePath``) ⇒ se tolera la ausencia.
    """
    return solicitud_binding_presente or transicion_p7_a5


def verificar_vinculo(*, token_aprobado: str | None, token_observado: str, obligatorio: bool, etapa: str) -> bool:
    """Compara lo aprobado contra lo observado por el consumidor.

    Devuelve ``True`` si el vínculo se verificó y ``False`` si la aprobación es legacy y no lo trae (sólo
    cuando no es obligatorio). Un token PRESENTE se compara siempre, aunque no fuera obligatorio.
    """
    if token_aprobado is None:
        if obligatorio:
            raise Mo2AuthorizationAusenteError(
                f"[{etapa}] la aprobación no liga la instancia ni el INI de MO2 (falta {CLAVE_SEGMENTO}) y para "
                "esta operación el vínculo es obligatorio: no se consume una autorización que no describe "
                "qué instancia ni qué INI aprobó el propietario"
            )
        return False
    if token_aprobado != token_observado:
        raise Mo2AuthorizationMismatchError(
            f"[{etapa}] la instancia o el INI de MO2 observados no son los que el propietario aprobó "
            "(instancia, identidad, bytes del INI, política o runtime saliente distintos): no se ejecuta una "
            "transición distinta de la autorizada"
        )
    return True


__all__ = [
    "CLAVE_SEGMENTO",
    "IDENTIDAD_AUTORIZADA",
    "MO2_AUTH_SCHEMA_VERSION",
    "Mo2AuthorizationAusenteError",
    "Mo2AuthorizationError",
    "Mo2AuthorizationMalformadaError",
    "Mo2AuthorizationMismatchError",
    "calcular_token_mo2",
    "clave_de_ruta",
    "es_vinculo_obligatorio",
    "segmento_de_procedencia",
    "token_comprometido",
    "verificar_vinculo",
]
