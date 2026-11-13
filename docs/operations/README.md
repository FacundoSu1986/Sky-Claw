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
- [Smoke de la etapa 6 asistida en el rig Windows](etapa6_smoke_rig.md) — procedimiento y
  resultado de la corrida de Wrye Bash asistido (MO2 + USVFS); el informe completo queda fuera
  del repo.
- [Alta de un build nuevo de TexGen/DynDOLOD en el contrato de UI](dyndolod_alta_de_build_uia.md) —
  procedimiento con el script de medición de **solo lectura**; probado solo con una GUI simulada, nunca
  contra la GUI real de un build.
- [Prueba del generador nativo de parallax en un rig real](native_parallax_prueba_en_rig.md)
- [Parallax nativo (beta): estado y cómo ajustar la profundidad](native_parallax_beta.md)
- [Runbook P7: provisionar un Golden Master](p7_golden_master_runbook.md) — preparado, **no
  ejecutado**; cada mutación exige aprobación explícita del propietario.
- [Deployment y preflight](../../DEPLOYMENT.md)

## Regla operativa

Separar siempre:

- verificado por lectura;
- verificado por tests o CI;
- verificado en un MO2/USVFS real;
- pendiente de verificación.

Un artifact de CI que arranca no demuestra firma, instalador, VFS, rollback ni
ausencia de procesos nietos.
