"""Contrato del gate de handoff TexGen → DynDOLOD: tipos compartidos.

Viven acá —y no en el runner— para que los adaptadores de backend
(``mo2/brokered_dyndolod.py``) puedan construir veredictos sin importar el
runner completo, y para que la pregunta del gate tenga UNA sola forma en todos
los dominios: *¿DynDOLOD va a ver exactamente el TexGen Output autorizado?*

Cada pieza del veredicto prueba una propiedad distinta y ninguna se solapa:

* la **identidad del artifact** (digest/files/bytes) prueba qué bytes pertenecen
  al TexGen Output autorizado;
* la **visibilidad demostrada** por el backend prueba que el consumidor los verá
  en SU namespace (``Data`` físico standalone; overlay MO2/USVFS brokered);
* un **canary runtime** prueba que un proceso dentro del VFS ve un mapping
  esperado — un archivo representativo, nunca el artifact completo.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Literal

from sky_claw.local.tools.artifact_digest import TreeDigest

#: Acción humana que completa el handoff cuando el corte es ACCIONABLE. Es la
#: semántica generalizada de ``DynDOLODPipelineResult.needs_deployment``: el
#: artifact está listo y certificable, y lo único que falta es que el operador
#: haga el último paso de entrega — materializarlo en el ``Data`` físico
#: (standalone) o habilitar el mod en el perfil MO2 (brokered). Cualquier otro
#: corte (drift, identidad dudosa, bridge caído) NO lleva acción pendiente:
#: es un fallo, no una espera.
HandoffPendingAction = Literal["physical_deployment", "profile_enablement"]


@dataclass(frozen=True, slots=True)
class TexGenHandoffRequest:
    """Qué debe demostrarse antes de autorizar el spawn de DynDOLOD.

    ``staging`` es el árbol ``textures`` AUTORIZADO —el staging de esta corrida
    (``run_texgen=True``) o el artifact preservado (resume)— y su contenido es
    la identidad que DynDOLOD tiene que ver, byte a byte. ``authorized`` es la
    autoridad durable del artifact (digest/files/bytes del handoff que lo
    certifica) cuando existe; ``None`` significa "sin handoff durable previo" y
    la identidad se apoya entonces en el propio staging de la corrida
    (born-empty) o en la verificación de resume.
    """

    mod_name: str
    staging: pathlib.Path
    data_dir: pathlib.Path
    expected_profile: str | None
    authorized: TreeDigest | None = None


@dataclass(frozen=True, slots=True)
class TexGenHandoffResult:
    """Veredicto estructurado del gate de handoff. Fallo cerrado por diseño.

    ``verified=False`` con ``pending_action`` es la ÚNICA forma de decir "el
    artifact está listo y espera una acción humana": es lo que autoriza al
    servicio a PRESERVAR el mod y ofrecer resume. Todo lo demás —incluido lo que
    no se pudo determinar— es un bloqueo duro (``pending_action=None``).
    """

    verified: bool
    reason: str
    pending_action: HandoffPendingAction | None = None

    @classmethod
    def aprobado(cls, resumen: str) -> TexGenHandoffResult:
        return cls(verified=True, reason=resumen)

    @classmethod
    def bloqueado(
        cls,
        razon: str,
        *,
        pending_action: HandoffPendingAction | None = None,
    ) -> TexGenHandoffResult:
        return cls(verified=False, reason=razon, pending_action=pending_action)


__all__ = [
    "HandoffPendingAction",
    "TexGenHandoffRequest",
    "TexGenHandoffResult",
    "TreeDigest",
]
