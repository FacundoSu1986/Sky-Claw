"""Composición del veredicto del informe con el post-check de los artefactos.

Por qué existe este módulo
--------------------------
``dictar`` mira el informe; ``auditar`` mira el disco. Por separado dejan pasar un lote que
sale ``PASS`` con artefactos planos: el informe dice que todo pasó y el archivo no tiene
relieve. Si nadie los cruza, la regla se inventa en el lugar donde se necesita —el runner— y
ahí es donde se pierde. Un lote no está bien porque cada mitad esté bien por su lado.

Regla única, y por qué es asimétrica
------------------------------------
- ``INCONSISTENTE`` y ``FAIL`` **no se rescatan**. Si el informe se contradice o se perdió
  trabajo, que el disco se vea bien no cambia el veredicto.
- Cualquier hallazgo o artefacto no verificado degrada a ``REVIEW``, **nunca a ``FAIL``**. Un
  mapa plano es indistinguible de una superficie genuinamente lisa mirando sólo el archivo: la
  diferencia entre "el generador falló" y "esta pared no tiene relieve" no está en los bytes.
  Declarar ``FAIL`` ahí sería inventar evidencia, y en desatendido eso cuesta tanto como el
  falso verde opuesto: un runner que grita lobo se termina ignorando.
"""

from __future__ import annotations

from .auditor import InformeDeAuditoria
from .veredicto import DecisionDelLote, MotivoDelLote, VeredictoDelLote

#: Veredictos que el post-check no puede mejorar: ya son peores que ``REVIEW``.
_IRRESCATABLES = frozenset({VeredictoDelLote.INCONSISTENTE, VeredictoDelLote.FAIL})


def _rutas_dudosas(auditoria: InformeDeAuditoria) -> tuple[str, ...]:
    """Rutas que el post-check no pudo aprobar, con hallazgo o sin verificar."""

    rutas: list[str] = []
    for ruta in [h.ruta for h in auditoria.hallazgos] + list(auditoria.no_verificados):
        if ruta and ruta not in rutas:
            rutas.append(ruta)
    return tuple(rutas)


def _detalle(auditoria: InformeDeAuditoria) -> str:
    return (
        f"{len(auditoria.verificados)} artefactos verificados, "
        f"{len(auditoria.hallazgos)} con hallazgo, "
        f"{len(auditoria.no_verificados)} sin verificar"
    )


def combinar(decision: DecisionDelLote, auditoria: InformeDeAuditoria) -> DecisionDelLote:
    """Veredicto final de la corrida: el del informe, degradado por lo que el disco diga."""

    if decision.veredicto in _IRRESCATABLES:
        return decision
    if not auditoria.hallazgos and not auditoria.no_verificados:
        return decision

    motivos = list(decision.motivos)
    if MotivoDelLote.ARTEFACTO_NO_CONFIABLE not in motivos:
        motivos.append(MotivoDelLote.ARTEFACTO_NO_CONFIABLE)

    rutas = list(decision.rutas)
    for ruta in _rutas_dudosas(auditoria):
        if ruta not in rutas:
            rutas.append(ruta)

    return DecisionDelLote(
        veredicto=VeredictoDelLote.REVIEW,
        motivos=tuple(motivos),
        rutas=tuple(rutas),
        conteos=decision.conteos,
        detalle=_detalle(auditoria),
    )
