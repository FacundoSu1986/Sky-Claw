# Smoke de LOOT desatendido en el rig Windows (R0–R8)

> **Audiencia:** operador del rig (Windows + MO2 + LOOT) y revisor del cierre de LOOT.
>
> **Estado:** preparado, **no ejecutado**. Ninguna afirmación de esta guía está
> verificada en un rig real. Los tests automatizados la acompañan; **no** la sustituyen.
>
> **Fuentes canónicas:**
> - `sky_claw/local/tools/loot_service.py`: veredicto, lock, journal y rollback.
> - `sky_claw/local/loot/outcome.py`: matriz A–H.
> - `sky_claw/local/loot/debug_log.py`: firmas fatales del log.
> - `sky_claw/local/mo2/brokered_loot.py`: job brokered con `expected_plugins`.
> - [`recovery.md`](recovery.md): cuarentena y recuperación manual.
>
> **Última verificación (por lectura):** 2026-11-24 sobre la rama del cierre de LOOT (base `79c583a`).

## Qué valida y qué no

Valida, contra el binario oficial de LOOT 0.29.x y una instancia MO2 con USVFS, que el sort
desatendido:

- produce el veredicto tipado correcto;
- deja el `plugins.txt`/`loadorder.txt` del perfil como corresponde (aplicado, intacto o restaurado);
- deja el journal en el estado verdadero (`COMMITTED`, `ROLLED_BACK` o `PENDING`).

**No** valida el refresco del masterlist. Queda para PR-B: la política decidida es que Sky-Claw provea el
masterlist con `updateMasterlist = false`. Mientras tanto, LOOT descarga el masterlist en cada
sort y **necesita red**. Sin red, la corrida termina en `timeout` (R5).

## Preparación (una vez)

1. MO2 portable de prueba, con un perfil que tenga al menos 5 mods con plugins, en un orden
   distinto al que LOOT produciría.
2. LOOT **standalone oficial** 0.29.x instalado fuera de MO2. El LOOT interno de MO2 se rechaza (guard F8).
3. Bridge de Sky-Claw instalado (`--mode install-vfs-bridge`) y MO2 abierto.
4. Respaldos, antes de cada escenario:
   - copiar `profiles/<perfil>/plugins.txt` y `loadorder.txt`;
   - anotar el SHA-256 de ambos;
   - anotar el estado de `.skyclaw_backups/journal.db`.
5. Data root de LOOT de Sky-Claw: `%USERPROFILE%\.sky_claw\state\loot\<instancia>\<perfil>`.
   Anotar si existe antes de R0.

Cada escenario se corre por las **dos** superficies, porque las dos comparten el servicio y lo
que se valida es la paridad:

- **GUI:** Ritual LOOT. Pide aprobación HITL; en «Modo local» se auto-aprueba.
- **Agente:** `run_loot_sort` por Telegram o `/api/chat`. Solo toma el lock, sin HITL.

Por cada corrida, registrar:
- la respuesta completa (`success`, `message`, `outcome`, `failure_reason`, `rolled_back`,
  `rollback_vetoed`, `snapshots`, `verification`, `loot_log`);
- los SHA-256 posteriores de los dos archivos;
- el estado de la última transacción del journal.

## Escenarios

| # | Preparación | Resultado esperado | Verificación física |
|---|---|---|---|
| R0 | Borrar el data root de LOOT de Sky-Claw | `changed` o `no_change`, sin el diálogo «First-Time Tips» | En `settings.toml` del data root: `lastVersion` = versión del `LOOT.exe` instalado, `useNoSortingChangesDialog = false`, `enableLootUpdateCheck = false` |
| R1 | Orden desordenado | `outcome = changed`, `verification.execution_witness = fresh`, `active_plugins_preserved = true` | El orden cambió; la activación canónica es idéntica (la misma semántica de `read_active_plugins_state`: un master base o de Creation Club puede perder su `*` sin cambiar de estado); journal `COMMITTED` |
| R2 | Repetir R1 de inmediato | `outcome = no_change`, `success = true`, `message = ""` | SHA-256 idénticos al final de R1; journal `COMMITTED` |
| R3 | GUI de LOOT abierta (mutex `LOOT.Shell.Instance`) | `failure_reason = execution_not_attributable`, testigo `sentinel_intact` | SHA-256 idénticos a la preparación |
| R4 | Fallo deliberado de sort (regla cíclica en el userlist del data root) | `fail`: `process_error` con `loot_log.fatales` citando `Failed to sort plugins`, o `timeout` con esa línea en `outcome_detail` | Archivos restaurados byte a byte; journal `ROLLED_BACK`. Anotar cuál de las dos salidas ocurrió |
| R5 | Sin red (adaptador deshabilitado) | `failure_reason = timeout`; si LOOT registró el error de red, `loot_log` lo cita | Archivos restaurados; journal `ROLLED_BACK` |
| R6 | Un mod habilitado cuyo plugin **no** se proyecta en el Data virtual (por ejemplo, el `.esp` movido fuera de la carpeta del mod) | `failure_reason = precondition_failed`, con el nombre del plugin en `message`; **LOOT no se lanzó** (no hay `LOOTDebugLog.txt` nuevo) | SHA-256 idénticos a la preparación. Si se llegara a lanzar: `active_plugins_changed` y archivos restaurados |
| R7 | Cambiar el perfil activo de MO2 entre la aprobación y el sort | Rechazo de la atestación VFS (perfil distinto), sin lanzar LOOT | SHA-256 idénticos |
| R8 | Cerrar MO2 a mitad de un sort largo | `failure_reason = timeout` o de protocolo, `rollback_vetoed = true`, `snapshots` no vacío | Archivos **sin** restaurar (el veto es intencional), journal `PENDING`, instancia en cuarentena. Recuperación con [`recovery.md`](recovery.md) y restauración manual desde los `snapshots` informados |

## Cierre

- Toda fila sin evidencia queda como «no ejecutada». No se infiere de otra fila ni de los tests.
- Lo esperado se deriva del código y de la evidencia upstream citada en los módulos. Una discrepancia
  en el rig es un hallazgo: se registra con su evidencia y **no** se corrige el contrato a ojo.
- La evidencia del rig vive fuera del repositorio (informe del operador). El inventario
  ([`pending_ooda_status.md`](../pending_ooda_status.md), fila «Contrato de veredicto de éxito»)
  remite a esta guía.
