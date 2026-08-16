# Política de Seguridad / Security Policy

> **Audiencia:** usuarios, investigadores de seguridad y maintainers.
> **Fuente canónica:** controles del runtime, workflows y releases publicadas.
>
> **English summary:** please report vulnerabilities privately via **GitHub → Security →
> Advisories → "Report a vulnerability"**. Do not disclose exploit details in public
> issues. The latest published release is `v0.2.4`; only the latest published `0.2.x`
> release will receive security fixes. The release artifacts exist, but this page
> does not independently verify their cryptographic validity, Authenticode status or
> cold-boot behavior.
>
> **Estado:** controles de runtime verificados leyendo código; `v0.2.4` existe como
> release pública, mientras que la verificación criptográfica del bundle, la firma
> Authenticode y el cold boot del ejecutable permanecen fuera de esta verificación.
> **Última verificación:** 2026-08-16 sobre `origin/main` `32abf6f77b758c622dd71f20607589c53effeaf1`.

## Versiones soportadas

Solo la última release publicada recibe correcciones de seguridad.

| Versión | Soportada |
| ------- | --------- |
| 0.2.x (última publicada: `v0.2.4`) | ✅ |
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
- El workflow construye un artefacto PyInstaller en CI y la release `v0.2.4`
  publicó el ejecutable, el SBOM SPDX y el bundle Cosign. La existencia de esos
  artefactos no equivale a una verificación criptográfica independiente, firma
  Authenticode ni cold boot; no deben anunciarse como garantías hasta validarlos
  específicamente.
- Secretos vía el backend de keyring del sistema; directorios de estado con
  DACLs restrictivas (`.sky_claw/`) donde el control está cableado.
- Sandboxing de rutas con `PathValidator`
  (`sky_claw/app/security/path_validator.py`) relativo a `SystemPaths`.
- Guardrails del agente LLM: sanitización de historial, detección de prompt injection y
  de PII.

Los límites implementados y parciales se detallan en
[security_boundaries.md](docs/architecture/security_boundaries.md). El estado
de empaquetado y release vive en [DEPLOYMENT.md](DEPLOYMENT.md) y
[release.md](docs/operations/release.md).
