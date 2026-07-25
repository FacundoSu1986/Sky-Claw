# Validación en un rig real

> **Audiencia:** operadores y responsables de aceptación.
>
> **Estado:** Parcial; existe evidencia histórica de canary brokerizado, pero
> la cobertura no incluye todos los runners ni todos los escenarios.
>
> **Fuentes canónicas:** `sky_claw/local/mo2/vfs_broker.py`,
> `sky_claw/local/mo2/vfs_worker.py`,
> `sky_claw/local/mo2/vfs_attestation.py`, `DEPLOYMENT.md` y ADR 0007.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Objetivo

Probar que una tool ve el VFS del perfil correcto, produce el artefacto
esperado y cierra su árbol de procesos. `scan_mods_dir=True`, un exit code cero
o un subprocess mockeado no prueban USVFS.

## Preparación

- Windows y MO2 reales.
- Perfil descartable con un mod habilitado que aporte un archivo ausente del
  `Data` físico.
- Baseline y backup externos.
- Ejecutable Sky-Claw congelado y bridge instalado.
- Logs de Sky-Claw y MO2 disponibles.

## Secuencia de aceptación

1. Iniciar MO2 y confirmar que carga `Sky-Claw VFS Bridge`.
2. Ejecutar `vfs-health` con instancia, Skyrim y perfil explícitos.
3. Confirmar que worker y nieto ven el mismo canary y hash.
4. Probar perfil incorrecto y exigir fallo cerrado antes de la tool.
5. Ejecutar una acción representativa y verificar stdout, stderr, exit code y
   artefacto dentro del perfil.
6. Forzar timeout y cancelación; comprobar `worker_exit`, kill-tree y reap.
7. Forzar fallo posterior a escritura; verificar rollback byte a byte.
8. Confirmar journal, lock liberado y ausencia de procesos remanentes.

## Cobertura pendiente

La evidencia de un canary no generaliza automáticamente a LOOT, xEdit,
DynDOLOD, Synthesis, Wrye Bash, Pandora o procesos iniciados por otras rutas.
Cada runner prometido por una release necesita su propia matriz de aceptación.

Registrar versión de MO2, USVFS, Skyrim, tool, perfil, SHA de Sky-Claw y
resultado de cada escenario.
