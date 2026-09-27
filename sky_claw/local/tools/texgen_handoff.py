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

Cuando el gate aprueba, el veredicto porta un :class:`TexGenHandoffApproval`:
el ESTADO exacto aprobado, que el runner threada hasta el spawn y que el spawn
revalida completo antes de abrir el proceso (cierre de la ventana TOCTOU
prueba→spawn). :class:`HandoffDriftError` es el corte fail-closed de esa
revalidación.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Literal

from sky_claw.local.tools.artifact_digest import TreeDigest


class HandoffDriftError(Exception):
    """El estado aprobado por el gate cambió antes del spawn: el spawn se bloquea.

    Es el corte TOCTOU del binding gate→spawn: el approval certifica el estado
    bajo el que se autorizó, y ``spawn`` lo REVALIDA en su boundary. Si algo
    divergió —perfil, identidad del artifact, efectividad o evidencia runtime—
    no hay proceso que arranque sobre un estado que nadie aprobó.
    """


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
class TexGenHandoffApproval:
    """El ESTADO EXACTO que el gate aprobó, ligado estructuradamente al spawn.

    El binding cierra la ventana TOCTOU ``verify_texgen_handoff() PASS →
    drift → spawn()``: sin este token, ``spawn()`` reconstruía un challenge
    NUEVO que certificaba el estado actual —que ya no es el aprobado— y hasta
    podía elegir un canary de otro mod. El token se entrega por parámetro
    explícito (nunca estado mutable implícito en la strategy) y ``spawn()`` lo
    REVALIDA completo, fail-closed, antes de abrir cualquier proceso/sesión.

    Mínimo ligado (además de la ruta del árbol sobre la que se midió):

    * ``profile`` — el perfil dueño del artifact (expected profile);
    * ``profile_fingerprint`` — el estado del perfil (modlist/plugins) bajo el
      que se aprobó;
    * ``artifact`` — digest/files/bytes del árbol autorizado;
    * ``mod_name`` — el mod fuente (``TexGen Output``);
    * ``canary_*`` — la identidad del canary que atestiguó el mapping runtime.

    Los campos son ``None`` cuando el backend no tiene esa noción (standalone
    no modela fingerprint ni canary); ``spawn()`` revalida lo que su dominio
    posee y exige coherencia total con lo que el token SÍ afirma.
    """

    mod_name: str
    artifact_root: pathlib.Path
    artifact: TreeDigest
    data_dir: pathlib.Path
    profile: str | None = None
    profile_fingerprint: str | None = None
    canary_relative_path: pathlib.PurePosixPath | None = None
    canary_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TexGenHandoffResult:
    """Veredicto estructurado del gate de handoff. Fallo cerrado por diseño.

    ``verified=False`` con ``pending_action`` es la ÚNICA forma de decir "el
    artifact está listo y espera una acción humana": es lo que autoriza al
    servicio a PRESERVAR el mod y ofrecer resume. Todo lo demás —incluido lo que
    no se pudo determinar— es un bloqueo duro (``pending_action=None``).

    ``verified=True`` implica ``approval`` presente (invariante del
    constructor): no existe "aprobado sin estado ligado". El runner threada ese
    token hasta :func:`DynDOLODSpawnStrategy.spawn`, que lo revalida.
    """

    verified: bool
    reason: str
    pending_action: HandoffPendingAction | None = None
    approval: TexGenHandoffApproval | None = None

    def __post_init__(self) -> None:
        if self.verified != (self.approval is not None):
            raise ValueError(
                "TexGenHandoffResult exige approval si y sólo si verified=True: "
                "el spawn se liga al estado aprobado, no a un booleano"
            )

    @classmethod
    def aprobado(cls, resumen: str, *, approval: TexGenHandoffApproval) -> TexGenHandoffResult:
        return cls(verified=True, reason=resumen, approval=approval)

    @classmethod
    def bloqueado(
        cls,
        razon: str,
        *,
        pending_action: HandoffPendingAction | None = None,
    ) -> TexGenHandoffResult:
        return cls(verified=False, reason=razon, pending_action=pending_action)


__all__ = [
    "HandoffDriftError",
    "HandoffPendingAction",
    "TexGenHandoffApproval",
    "TexGenHandoffRequest",
    "TexGenHandoffResult",
    "TreeDigest",
]
