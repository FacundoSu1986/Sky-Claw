from __future__ import annotations

from sky_claw.app_context import AppContext


async def _run_security(ctx: AppContext, command_str: str | None) -> None:
    """Ejecuta operaciones de auditoría Purple Team desde la CLI."""
    if not command_str:
        print(
            "Uso: python -m sky_claw --mode security 'scan <path>' o 'approve <path>'"
        )
        return

    parts = command_str.split(maxsplit=1)
    action = parts[0].lower()
    path_str = parts[1] if len(parts) > 1 else "."

    from sky_claw.security.metacognitive_logic import audit_resource
    from sky_claw.security.governance import GovernanceManager

    if action == "scan":
        print(f"🛡️ Iniciando auditoría Purple Team para: {path_str}...")
        result = await audit_resource(path_str)

        # Formatear salida similar al agente
        confidence = result.get("confidence", 0.0)
        print(f"\nResultados de Auditoría (Confianza: {confidence:.2f}):")
        print(f"Decisión: {result.get('summary', {}).get('is_safe', False)}")

        for find in result.get("findings", []):
            severity = find.get("severity", "LOW")
            print(
                f"[{severity}] {find.get('message')} ({find.get('file')}:{find.get('line')})"
            )

        if result.get("summary", {}).get("is_safe"):
            print("\n✅ El recurso es seguro según las políticas de Abril 2026.")
        else:
            print(
                "\n🔴 SE HAN DETECTADO RIESGOS CRÍTICOS. Se recomienda revisión manual."
            )

    elif action == "approve":
        GovernanceManager.get_instance().approve_file(path_str)
        print(f"✅ Archivo '{path_str}' añadido a la whitelist local.")
    else:
        print(f"Acción de seguridad desconocida: {action}")
