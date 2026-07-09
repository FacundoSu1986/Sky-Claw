# Política de Seguridad / Security Policy

> **English summary:** please report vulnerabilities privately via **GitHub → Security →
> Advisories → "Report a vulnerability"**. Do not disclose exploit details in public
> issues. Only the latest 0.2.x release receives security fixes.

## Versiones soportadas

Solo la última release publicada recibe correcciones de seguridad.

| Versión | Soportada |
| ------- | --------- |
| 0.2.x (última release) | ✅ |
| < 0.2 | ❌ |

## Cómo reportar una vulnerabilidad

- Usá el reporte privado de GitHub: **Security → Advisories → "Report a vulnerability"**.
  (Si la opción no aparece, el mantenedor debe habilitarla en *Settings → Code security →
  Private vulnerability reporting*.)
- **No publiques detalles de explotación en issues públicos.**
- Incluí: versión afectada, pasos para reproducir e impacto estimado.

**Expectativas:** acuse de recibo en ~72 h y evaluación best-effort (proyecto de un solo
mantenedor). Si el reporte se confirma, el fix sale en la siguiente release, con crédito
al reporter salvo que pida lo contrario.

## Alcance

Sky-Claw es una aplicación local de escritorio que orquesta herramientas de modding
(MO2, SSEEdit, LOOT, DynDOLOD…).

**En alcance** — todo lo que comprometa la máquina del usuario o sus secretos:

- Escapes del sandbox de rutas (`PathValidator`).
- Inyección de comandos hacia los ejecutables orquestados.
- Fuga de API keys o de otros secretos gestionados por la app.
- Prompt injection que derive en acciones destructivas del agente LLM.

**Fuera de alcance** — vulnerabilidades propias de los ejecutables de terceros que
Sky-Claw invoca (SSEEdit, LOOT, DynDOLOD, MO2, etc.): reportalas a sus proyectos.

## Medidas existentes

- CI con **Bandit** (SAST) y **pip-audit --strict** sobre `requirements.lock` con hashes
  enforced; `npm audit` para el gateway de Telegram.
- Releases firmadas con **cosign** + **SBOM** publicado.
- Secretos vía keyring del sistema (DPAPI en Windows); directorios de estado con DACLs
  restrictivas (`.sky_claw/`).
- Sandboxing de rutas con `PathValidator`
  (`sky_claw/antigravity/security/path_validator.py`) relativo a `SystemPaths`.
- Guardrails del agente LLM: sanitización de historial, detección de prompt injection y
  de PII.
