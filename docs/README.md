# Documentación de Sky-Claw

> **Estado:** portal canónico.
>
> **Audiencia:** usuarios, operadores, desarrolladores y agentes.
>
> **Fuentes de navegación:** índices de este directorio y documentación raíz.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización DB/lifecycle:** 2026-08-16 sobre `main` `7156718`.

## Aprender y usar

- [Inicio rápido](../QUICKSTART.md)
- [Guías de usuario](user/README.md)
- [Pipeline de Skyrim](pipeline/skyrim_sop.md)
- [Glosario](glossary.md)

## Operar y recuperar

- [Runbook de deployment](../DEPLOYMENT.md)
- [Standalone y USVFS](operations/deployment_standalone_usvfs.md)
- [Operaciones](operations/README.md)
- [Política de seguridad](../SECURITY.md)
- [Validación en rig real](operations/real_rig_validation.md)

## Comprender y extender

- [Arquitectura](architecture/README.md)
- [Contribución](../CONTRIBUTING.md)
- [Referencia técnica](api/README.md)
- [Guías para agentes](agents/README.md)
- [Fuentes de verdad y resolución de drift](documentation/source_of_truth.md)

## Decisiones y evidencia histórica

- [ADRs](adr/README.md): decisiones aceptadas y sus consecuencias.
- [Auditorías](audits/README.md): evidencia fechada, no estado vivo.
- [Diseño](design/README.md): specs y planes de implementación, históricos o pendientes.
- [Estado OODA](pending_ooda_status.md): snapshot que debe reverificarse.

## Cómo resolver contradicciones

Aplicar el orden definido en
[source_of_truth.md](documentation/source_of_truth.md). Un documento que
contradiga código ejecutable o un ADR vigente debe corregirse o etiquetarse
como histórico; no se resuelve la contradicción copiando una tercera versión.

Para agentes de IA, una contradicción documental no autoriza a modificar
producción para ajustarla a un README, plan o auditoría. Primero debe trazarse el
caller actual, verificarse tests/ADR y registrar el caso como
`DOCUMENTATION_DRIFT` si la documentación quedó atrás.

La sincronización del 2026-08-16 cubre específicamente el contrato moderno de
`DatabaseLifecycleManager` y sus boundaries. No debe interpretarse como una
reverificación integral de todas las guías fechadas en julio.
