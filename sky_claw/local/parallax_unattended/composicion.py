"""Composición del veredicto del informe con el post-check de los artefactos.

Por qué existe este módulo
--------------------------
``dictar`` mira el informe; ``auditar`` mira el disco. Por separado dejan pasar un lote que
sale ``PASS`` con artefactos planos: el informe dice que todo pasó y el archivo no tiene
relieve. Si nadie los cruza, la regla se inventa en el lugar donde se necesita —el runner— y
ahí es donde se pierde. Un lote no está bien porque cada mitad esté bien por su lado.

Regla única, y por qué es asimétrica
------------------------------------
- ``INCONSISTENTE`` y ``FAIL`` **no se rescatan**, pero sí incorporan los motivos y las rutas del
  post-check para que el operador vea el cuadro completo y no sólo la primera capa que habló.
- Los hallazgos de relieve o formatos no medibles (``ALTURA_PLANA``, ``no_verificados``) degradan
  a ``REVIEW``, **nunca a ``FAIL``**: un mapa plano es indistinguible de una superficie
  genuinamente lisa mirando sólo el archivo.
- Los hallazgos duros de disco (``NO_EXISTE``, ``TAMANO_INCOHERENTE``, ``NO_ES_DDS``, etc.) sobre
  un archivo que el informe declaró ``ESCRITA`` degradan a ``FAIL`` (``ARTEFACTO_INVALIDO``): que
  el archivo falte o esté truncado no es una duda de relieve, es trabajo perdido.
"""

from __future__ import annotations

from .auditor import HALLAZGOS_DE_FALLA, HALLAZGOS_DE_REVISION, InformeDeAuditoria
from .veredicto import (
    MOTIVOS_DE_FALLA,
    MOTIVOS_DE_INCONSISTENCIA,
    DecisionDelLote,
    MotivoDelLote,
    VeredictoDelLote,
)


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

    if not auditoria.hallazgos and not auditoria.no_verificados:
        return decision

    motivos = list(decision.motivos)
    if (
        any(h.codigo in HALLAZGOS_DE_FALLA for h in auditoria.hallazgos)
        and MotivoDelLote.ARTEFACTO_INVALIDO not in motivos
    ):
        motivos.append(MotivoDelLote.ARTEFACTO_INVALIDO)
    if (
        any(h.codigo in HALLAZGOS_DE_REVISION for h in auditoria.hallazgos) or auditoria.no_verificados
    ) and MotivoDelLote.ARTEFACTO_NO_CONFIABLE not in motivos:
        motivos.append(MotivoDelLote.ARTEFACTO_NO_CONFIABLE)

    rutas = list(decision.rutas)
    for ruta in _rutas_dudosas(auditoria):
        if ruta not in rutas:
            rutas.append(ruta)

    if decision.veredicto is VeredictoDelLote.INCONSISTENTE or any(m in MOTIVOS_DE_INCONSISTENCIA for m in motivos):
        veredicto = VeredictoDelLote.INCONSISTENTE
    elif decision.veredicto is VeredictoDelLote.FAIL or any(m in MOTIVOS_DE_FALLA for m in motivos):
        veredicto = VeredictoDelLote.FAIL
    else:
        veredicto = VeredictoDelLote.REVIEW

    resumen = _detalle(auditoria)
    detalle = f"{decision.detalle}; {resumen}" if decision.detalle else resumen

    return DecisionDelLote(
        veredicto=veredicto,
        motivos=tuple(motivos),
        rutas=tuple(rutas),
        conteos=decision.conteos,
        detalle=detalle,
    )
