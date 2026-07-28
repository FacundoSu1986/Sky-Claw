# Operaciones

> **Audiencia:** operadores, responsables de release y soporte.
>
> **Estado:** Parcial; el runtime dispone de controles operativos, pero no hay
> una release GA firmada.
>
> **Fuentes canónicas:** `DEPLOYMENT.md`, `sky_claw/logging_config.py`,
> `sky_claw/app_context.py` y `.github/workflows/`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Runbooks

- [Standalone y USVFS](deployment_standalone_usvfs.md) — **leer antes del primer
  ritual**: Sky-Claw no hereda la VFS de MO2, y esa invariante decide qué ven y
  dónde escriben las herramientas.
- [Observabilidad](observability.md)
- [Recuperación](recovery.md)
- [Release](release.md)
- [Validación en rig real](real_rig_validation.md)
- [Deployment y preflight](../../DEPLOYMENT.md)

## Regla operativa

Separar siempre:

- verificado por lectura;
- verificado por tests o CI;
- verificado en un MO2/USVFS real;
- pendiente de verificación.

Un artifact de CI que arranca no demuestra firma, instalador, VFS, rollback ni
ausencia de procesos nietos.
