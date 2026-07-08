"""Informe final de vuelo (T-28, ADR 0002): productor puro, Markdown y composer.

La contraparte post-vuelo de :mod:`action_manifest` (T-26): al terminar un
Ritual mutante, el informe LEE la caja negra persistida — el
:class:`ActionManifest` en el journal — y la presenta en las cuatro secciones
de T-28 (qué cambió / por qué / quién ganó cada conflicto / cómo revertir).
No inventa datos. ``build_flight_report`` y el render Markdown son puros (sin
I/O) para testearse sin DB; ``compose_flight_report_from_journal`` es el único
punto que toca el journal y es el entry-point que consumirán GUI/CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.orchestrator.preview.manifest import (
    ActionManifest,
    FlightReport,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.db.journal import OperationJournal

__all__ = [
    "ActionManifest",
    "FlightReport",
    "build_flight_report",
    "compose_flight_report_from_journal",
    "render_flight_report_markdown",
]

#: Estado reportado cuando la transacción no existe en el journal.
_ESTADO_DESCONOCIDO = "desconocido"


def build_flight_report(
    *,
    manifest: ActionManifest | None,
    transaction_status: str,
    post_run_validation: dict[str, Any] | None = None,
) -> FlightReport:
    """Arma un :class:`FlightReport` desde el manifiesto de la caja negra.

    Puro (sin I/O): mapea las cuatro secciones de T-28 desde el
    ``ActionManifest`` — cambios (``files_touched`` / ``load_order_diff``),
    razones (``summary``), ganadores (``records_forwarded``) y rollback
    (``rollback_plan``) — sin re-derivar nada.

    Args:
        manifest: La caja negra del Ritual, o ``None`` si no quedó persistida —
            en ese caso el informe sale degradado con razón explícita, nunca
            vacío silencioso (criterio de T-28).
        transaction_status: Estado real de la transacción del journal
            (``"committed"`` / ``"rolled_back"`` / ``"pending"``).
        post_run_validation: Resultado del validador post-run (T-21) cuando
            exista; ``None`` mientras tanto.

    Returns:
        Un ``FlightReport`` listo para persistir y renderizar.
    """
    if manifest is None:
        return FlightReport(
            transaction_status=transaction_status,
            post_run_validation=post_run_validation,
            degraded=True,
            degraded_reason=(
                "sin manifiesto persistido: el Ritual corrió sin caja negra (T-26) o la emisión falló antes de ejecutar"
            ),
        )
    return FlightReport(
        ritual_id=manifest.ritual_id,
        tool=manifest.tool,
        tool_version=manifest.tool_version,
        transaction_status=transaction_status,
        files_touched=list(manifest.files_touched),
        load_order_diff=manifest.load_order_diff,
        summary=manifest.summary,
        conflicts_resolved=list(manifest.records_forwarded),
        rollback_plan=list(manifest.rollback_plan),
        post_run_validation=post_run_validation,
    )


async def compose_flight_report_from_journal(
    journal: OperationJournal,
    *,
    transaction_id: int,
) -> FlightReport:
    """Compone el informe leyendo la caja negra ya persistida en el journal.

    Localiza el op del manifiesto entre las operaciones de la transacción
    (metadata con ``ritual_id`` y que NO sea un informe previo — el propio
    ``FlightReport`` también viaja como metadata, discriminado por ``kind``),
    lo reconstruye con ``ActionManifest.model_validate`` y toma el estado REAL
    de la transacción. Sin manifiesto → informe degradado explícito.

    Args:
        journal: Journal abierto donde el Ritual persistió su caja negra.
        transaction_id: Transacción del Ritual a informar.

    Returns:
        El ``FlightReport`` del Ritual (degradado si falta la caja negra).
    """
    tx = await journal.get_transaction(transaction_id)
    transaction_status = tx.status.value if tx is not None else _ESTADO_DESCONOCIDO

    manifest: ActionManifest | None = None
    for entry in await journal.get_operations_by_transaction(transaction_id):
        metadata = entry.metadata
        if metadata and metadata.get("ritual_id") and metadata.get("kind") != "flight_report":
            manifest = ActionManifest.model_validate(metadata)
            break

    return build_flight_report(manifest=manifest, transaction_status=transaction_status)


def _seccion_cambios(report: FlightReport) -> list[str]:
    lines = ["## Qué cambió", ""]
    if report.files_touched:
        lines.append(f"Archivos tocados ({len(report.files_touched)}):")
        lines.extend(f"- `{path}`" for path in report.files_touched)
    else:
        lines.append("Sin archivos registrados.")
    diff = report.load_order_diff
    if diff is not None and diff.moves:
        lines.append("")
        lines.append("Orden de carga:")
        lines.extend(f"- `{m.plugin}`: posición {m.from_index} → {m.to_index}" for m in diff.moves)
    return lines


def _seccion_razones(report: FlightReport) -> list[str]:
    lines = ["## Por qué", ""]
    lines.append(report.summary or "Sin resumen registrado en el manifiesto.")
    if report.tool:
        version = f" {report.tool_version}" if report.tool_version else ""
        lines.append(f"Ejecutado por **{report.tool}{version}**.")
    return lines


def _seccion_ganadores(report: FlightReport) -> list[str]:
    lines = ["## Quién ganó cada conflicto", ""]
    if not report.conflicts_resolved:
        lines.append("Sin conflictos forwardeados en este Ritual.")
        return lines
    for pair in report.conflicts_resolved:
        detalle = ", ".join(filter(None, [pair.record_type, pair.form_id]))
        sufijo = f" ({detalle})" if detalle else ""
        perdedores = ", ".join(f"`{loser}`" for loser in pair.losers) or "—"
        lines.append(f"- **`{pair.winner}`** ganó sobre {perdedores}{sufijo}")
    return lines


def _seccion_rollback(report: FlightReport) -> list[str]:
    lines = ["## Cómo revertir", ""]
    if not report.rollback_plan:
        lines.append("Sin plan de rollback registrado (no hay snapshots que restaurar).")
        return lines
    lines.append("Restaurar cada archivo desde su snapshot:")
    lines.extend(
        f"- `{step.original_path}` ← `{step.snapshot_path}` (snapshot `{step.snapshot_id}`)"
        for step in report.rollback_plan
    )
    return lines


def _seccion_validacion(report: FlightReport) -> list[str]:
    lines = ["## Validación post-run", ""]
    if report.post_run_validation is None:
        lines.append("No disponible — validador post-run (T-21) pendiente.")
    else:
        lines.extend(f"- {clave}: {valor}" for clave, valor in report.post_run_validation.items())
    return lines


def render_flight_report_markdown(report: FlightReport) -> str:
    """Render Markdown legible del informe (aceptación de T-28).

    Siempre incluye las cuatro secciones y el slot de validación post-run; un
    informe degradado muestra su razón de forma prominente, nunca sale vacío.
    """
    titulo = report.ritual_id or "Ritual desconocido"
    lines = [
        f"# Informe final de vuelo — {titulo}",
        "",
        f"- Estado de la transacción: **{report.transaction_status}**",
        f"- Emitido: {report.created_at.isoformat()}",
    ]
    if report.degraded:
        lines.append("")
        lines.append(f"> ⚠️ **Informe degradado**: {report.degraded_reason or 'razón no registrada'}")
    for seccion in (
        _seccion_cambios(report),
        _seccion_razones(report),
        _seccion_ganadores(report),
        _seccion_rollback(report),
        _seccion_validacion(report),
    ):
        lines.append("")
        lines.extend(seccion)
    return "\n".join(lines) + "\n"
