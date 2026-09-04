# Fronteras de seguridad

> **Estado:** controles implementados y límites parciales.
>
> **Audiencia:** desarrolladores, operadores y agentes.
>
> **Fuentes canónicas:** `sky_claw/app/security/`, `sky_claw/config.py`
> y callers productivos.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización parcial xEdit LLM:** 2026-09-04 sobre `main` `10354c2b` (#548);
> limitada a la frontera read-only de `run_xedit_script`. No reverifica el resto.

## Entradas y controles

| Frontera | Control principal |
|---|---|
| Paths | `PathValidator`, `assert_safe_component` y validators específicos |
| Egress | `NetworkGateway.authorize()` / `request()` cuando está inyectado |
| Secrets | keyring del SO; vault cifrado para el flujo que lo cablea |
| LLM | schemas Pydantic, sanitización y guardrails |
| Tools | allowlist por sesión, locks y middleware de ruta |
| WebSocket | tokens y comparaciones seguras según flujo |
| MO2 | profile explícito, broker loopback y attestation/canary |

## HITL

HITL es una propiedad de un camino concreto:

- descargas externas y handlers que reciben `HITLGuard`;
- middleware de rituales destructivos;
- promoción post-run del sandbox.

La capa de tools del LLM no tiene HITL general: su política base es lock-only,
pero un handler puede recibir un gate explícito, como ocurre con
`download_mod`. No documentar “todo es HITL” ni “todo es autónomo”.

## xEdit del agente LLM: read-only con provenance (#548)

La tool `run_xedit_script` que expone el agente LLM es **read-only**. Su frontera
vigente (verificada en `sky_claw/app/agent/tools/xedit_readonly_tool.py`,
`xedit_policy.py` y `schemas.py`):

- **capacidad read-only**: sólo diagnósticos xEdit bundleados, no scripts arbitrarios
  ni mutantes;
- **schema y runtime comparten la misma política de nombres**
  (`XEDIT_AGENT_ALLOWED_SCRIPT_NAMES` / `XEditAnalysisParams`): el schema anuncia
  exactamente lo que la ejecución acepta;
- **fallo cerrado ante nombres desconocidos**, tanto en la validación del schema como
  en el handler;
- **staging canónico obligatorio** antes de ejecutar (`ensure_scripts_staged()`); sin
  provenance verificable el handler queda bloqueado;
- **parsing por protocolo** para cada script.

Esto **no** significa que todas las rutas internas de xEdit sean read-only: los
rituales/orquestador ejecutan xEdit mutante por otra frontera
(`XEditPipelineService`, con lock, journal y rollback), con contratos distintos.
Ancla: `tests/test_xedit_agent_readonly_policy.py`.

## Secrets

La configuración de runtime de proveedores/Nexus/Telegram se obtiene
principalmente de keyring. `CredentialVault` es un almacén separado y sólo
participa donde el caller lo inyecta. Nunca documentar un secreto real ni
recomendar guardarlo en TOML plano.

## Garantías parciales

Un control definido pero no inyectado en el caller productivo no protege ese
camino. Las auditorías de seguridad son evidencia fechada; esta página debe
actualizarse desde código y tests cuando sus hallazgos cambien.
