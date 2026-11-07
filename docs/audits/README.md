# Auditorías

> **Estado:** evidencia histórica fechada; no fuente viva por sí sola.
>
> **Audiencia:** reviewers, maintainers y agentes.
>
> **Fuente canónica del estado vigente:**
> [`../pending_ooda_status.md`](../pending_ooda_status.md). Los informes
> fechados de este directorio son evidencia histórica.
>
> **Última verificación del índice:** 2026-07-25 sobre `origin/main` `c6ab35e`.
> Esa fecha es el **baseline contra el que se re-verificaron las auditorías
> previas**, no la fecha de la entrada más reciente: cada informe lleva la suya
> en el nombre del archivo y vale contra el commit que declara en su cabecera.

Las auditorías preservan escenario, evidencia y hallazgos al momento de su
ejecución. Un hallazgo puede haber sido corregido después. Antes de actuar:

1. refrescar `origin/main`;
2. localizar el caller productivo;
3. revisar PRs y tests posteriores;
4. trasladar sólo el estado vigente a arquitectura, referencia u operaciones.

Archivos actuales:

- `2026-11-02_fence_de_supervision_del_broker.md` (fence `supervised` del broker MO2/USVFS; dictamen `SUPERVISION_FENCE_SECURITY_HOLD`: la identidad del proceso Python efectivo en onefile no está probada)
- `2026-11-03_identidad_del_job_onefile.md` (identidad del Job y contención del worker PyInstaller onefile, P2: el worker efectivo se verifica en el Job nombrado antes de hablar con el broker; medición exacta del arranque del `.exe` congelado; dictamen `ONEFILE_CONTAINMENT_READY_FOR_REVIEW` con la evidencia de Windows registrada: **para revisión adversarial, no para merge** ni para levantar la barrera de #854)
- `2026-10-07_b1_permisos_r_suite.md` (+ `data/` con el registro y su hash)
- `2026-10-07_native_parallax_math_architecture_audit.md` (histórica: genera hipótesis; la adjudicación vigente de H1–H5 está en el PR #700; ver la cabecera del documento)
- `2026-08-22_runtime_vault_mo2_stock_launch_audit.md`
- `2026-08-04_wrye_bash_headless_snippet.md`
- `2026-07-18_orchestrator_resilience_audit.md`
- `2026-07-18_zero_trust_network_gateway_audit.md`
- `2026-07_historial_ooda.md`
- `auditoria_03_pipeline_consolidada.md`
- `auditoria_prs_300-304_verificacion.md`