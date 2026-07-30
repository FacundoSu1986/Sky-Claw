# OODA — inventario vivo de pendientes

> **Audiencia:** maintainers, reviewers y agentes.
>
> **Estado:** Parcial.
>
> **Fuentes canónicas:** código y tests del árbol actual; ADRs aceptados para
> decisiones; este documento únicamente consolida su estado.
>
> **Última verificación:** 2026-07-30 sobre `origin/main` `8d1050b`.

La narrativa fechada, las refutaciones y la secuencia completa de decisiones se
preservan en el [historial OODA de julio de
2026](audits/2026-07_historial_ooda.md). Antes de iniciar un ítem, volver a
confirmarlo contra código y tests.

## Estado consolidado

| Ítem | Estado | Cerrado en | Qué falta | Verificado por |
|---|---|---|---|---|
| T-10 | Parcial | `local/tools/` | Retirar BLE001 incrementalmente del resto del árbol | `pyproject.toml` |
| T-11 | Parcial | `local/xedit/` | Retirar BLE001 incrementalmente del resto del árbol | `pyproject.toml` |
| T-12 | Parcial | `local/fomod/`, `local/loot/` | Migrar los módulos aún exentos a mypy estricto | `pyproject.toml` |
| T-16c | Cerrado | #288, #306, #323 | — | tests de preflight por ritual |
| T-22 | Abierto | — | Transiciones y `prefers-reduced-motion` | humano |
| T-23 | Abierto | — | Virtualizar listas grandes de mods | humano |
| T-24 | Abierto | — | Labels y foco visible en formularios | humano |
| T-25 | Parcial | — | Cerrar T-27 y después ejecutar la matriz E2E con Skyrim, MO2 y herramientas reales | `TECHNICAL_REVIEW_TASKS.md:247-249`; humano |
| T-26 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_action_manifest` |
| T-27 | Parcial | ADR 0005 para Synthesis | Diseñar redirección de salida para Pandora, DynDOLOD y Wrye Bash | código y ADR 0005 |
| T-28 | Cerrado | #309, #318 y cierres posteriores | — | productores de `persist_flight_report` |
| T-29 | Cerrado | Oleada 7 | — | historial Git y tests |
| T-30 | Cerrado | Oleada 7 | — | historial Git y tests |
| U-01 | Cerrado | #381, #388 y documentación operativa | — | `test_output_targets.py` |
| U-02 | Cerrado | #360 | — | tests de Job Object |
| U-03 | Cerrado | #356 | — | tests de reconciliación de precache |
| U-04 | Parcial | #397 y #399 | BodySlide y el modo `Data` de Pandora | `test_rollback_salida.py` |
| U-05 | Cerrado | #354 | — | `test_vramr_service.py` |
| U-06 | Parcial | #375 para DynDOLOD | Post-check de Wrye Bash y BodySlide; criterio seguro para QuickAutoClean | tests de cada runner |
| U-07 | Cerrado | #355 | — | tests de Job Object de DynDOLOD |
| U-08 | Cerrado | #378 y reconciliador de arranque | — | `test_rollback_reconciler.py` |
| U-09 | Cerrado | #376 | — | tests del journal de grass |
| U-10 | Cerrado | #357 | — | tests de timeouts dedicados |
| U-11 | Cerrado | #359 | — | tests de `_process.run_capture` |
| U-12 | Cerrado | #358 | — | tests de limpieza del script xEdit |
| F1 | Cerrado | #328 | — | auditoría de resiliencia #319 |
| F2 | Cerrado | #320 y #322 | — | auditoría de resiliencia #319 |
| F3 | Cerrado | #321 | — | auditoría de resiliencia #319 |
| F4 | Cerrado | #342 | — | auditoría de resiliencia #319 |
| F5 | Cerrado | #332 | — | auditoría de resiliencia #319 |
| F6 | Cerrado | #331 | — | auditoría de resiliencia #319 |
| F7 | Cerrado | #343 | — | auditoría de resiliencia #319 |
| F8 | Cerrado | #343 | — | auditoría de resiliencia #319 |
| F9 | Abierto | — | Reducir el composition root de `SupervisorAgent` incrementalmente | auditoría de resiliencia #319 |
| F8 USVFS | Parcial | ADR 0007 y smoke `vfs-health` | Migrar runners restantes y completar smokes reales de cancelación, perfil y rollback | historial OODA, addendum F8 USVFS |
| Detección de enlaces | Cerrado | #404 y #405 | — | `test_links.py` |
| Borrado recursivo | Cerrado | #405 | — | `test_borrado_recursivo.py` |
| Smoke real de QuickAutoClean | Bloqueado (rig humano) | — | Ejecutar SSEEdit QuickAutoClean en una instalación real | humano |
| Smokes reales restantes | Bloqueado (rig humano) | — | NGIO, scripts `.pas`, GUI/FOMOD y Telegram end-to-end | humano |
| Residuos OODA de bajo valor | Abierto | — | Solo retomar agrupados si cambia su relación esfuerzo/impacto | historial OODA §3 |
| Residuos de crash logging | Abierto | #383 cerró F1/F2 reales | Cinco deudas sin víctima productiva actual | historial OODA, addendum #372 |

## 2.3 Zero-Trust — 2 ítems residuales

Persisten dos trabajos no urgentes heredados del inventario Zero-Trust:

1. migrar las lecturas centralizadas de `PathResolutionService` desde variables
   de entorno a `config.toml`;
2. consolidar secretos bajo una API única de `CredentialVault` con backend de
   keyring. El vault ya está conectado al `LLMRouter`, pero esa consolidación no
   existe todavía.

La escritura de una variable de entorno tras instalar una herramienta no
contradice por sí sola el primer punto: la deuda pendiente es la fuente
centralizada de lectura.

## Decide — recomendación de próximo frente

El frente de cierres mecánicos verificables íntegramente por un agente está
agotado. Lo pendiente se divide en tres clases:

1. **prueba humana en rig real:** T-25, QuickAutoClean y los demás smokes de
   herramientas;
2. **decisión de dominio antes de implementar:** rollback fino de Pandora
   `Data`, salida parametrizada de BodySlide y redirección de T-27;
3. **deuda incremental no bloqueante:** T-10/T-11/T-12, F9 y los residuales de
   bajo valor.

El próximo PR debería partir de evidencia nueva del rig o de una decisión de
dominio explícita. No conviene abrir otra caza general de hallazgos sobre este
snapshot.
