# Etapa 9 DESATENDIDA y operable de forma remota (TexGen + DynDOLOD)

> **Estado:** `IMPLEMENTADO COMO CANDIDATO — RIG REAL PENDIENTE`. Implementa los
> slices P1 (parcial: modales de arranque), P2–P9 de
> [#661](https://github.com/FacundoSu1986/Sky-Claw/issues/661) sobre la evidencia
> de rig **P0_PASS** (`docs/validation/2026-10-02_p0_uia_alpha209/`), más la
> superficie remota (Telegram / agente LLM) que la etapa 9 no tenía.
> **Lo que NO prueba este documento ni su PR:** que la cadena corra contra los
> binarios reales. El backend COM mutante replica las llamadas que la sonda P0-B
> ejecutó (`LegacyIAccessiblePattern.SetValue` + `InvokePattern.Invoke`), pero
> el código productivo **no corrió en Windows**: el criterio de DONE de #661
> (TexGen/DynDOLOD standalone unattended PASS, brokered PASS, evidencia
> commiteada) sigue abierto. Ver §9.

## 1. Problema

Hasta este cambio la etapa 9 era **ASISTIDA** por construcción: después del
spawn, un humano frente a la PC tenía que

1. pulsar `Ignore` en el modal de arranque (cuando aparece),
2. corregir el campo Output si el preset lo pre-cargó rancio,
3. confirmar la configuración en el modal HITL de la GUI,
4. pulsar `Start` (TexGen) o `Advanced >>>` → `OK` (DynDOLOD),
5. cerrar la herramienta con `Exit TexGen` / `Save and Exit`.

Además, la etapa 9 **no era alcanzable desde la superficie remota**: el registro
de tools del agente LLM (`AsyncToolRegistry`, la ruta de Telegram y `/api/chat`)
no tenía ninguna tool de DynDOLOD, y el round de tools del router corta a los
120 s (`DEFAULT_TOOL_ROUND_TIMEOUT`), menos que cualquier corrida real.

## 2. Decisión

Se agrega un modo **DESATENDIDO** explícito por corrida, que convive con el
**ASISTIDO** (que no cambia y sigue siendo el default de la GUI):

```text
ModoDeInteraccion.ASISTIDO     → protocolo de readiness T5-v2 (sin cambios)
ModoDeInteraccion.DESATENDIDO  → driver UIA mutante acotado por contrato de build
```

No hay fallback entre modos: un DESATENDIDO que no puede correr falla cerrado con
un código machine-readable (`CodigoDesatendido`); el operador puede lanzar otra
corrida ASISTIDA explícitamente.

El humano **sigue en el loop, pero de forma remota**: el significado del HITL
asistido ("terminé de configurar la GUI") se reemplaza formalmente por dos
aprobaciones remotas, ambas por Telegram y nunca auto-aprobables:

| HITL | Cuándo | Qué aprueba el operador |
|---|---|---|
| configuración desatendida | antes de spawnear nada | la configuración EXACTA (huella de los presets en disco) con la que correrá cada herramienta, el build verificado y el Output administrado |
| modal de arranque conocido | mid-run, si aparece | pulsar `Ignore` en un modal cuyo fingerprint fue medido en rig (P0-A/P0-B) |

Un modal desconocido, un diálogo inesperado durante la generación o cualquier
drift de controles falla cerrado: el proceso se termina y el aviso llega por
Telegram con el fingerprint observado, para diagnosticar sin ir a la PC.

## 3. Contrato de build (P2/P4)

`sky_claw/local/tools/dyndolod_uia_contrato.py`. Clave `(tool, exe_sha256)`,
nunca nombre de exe ni versión textual. Un registro congelado por igualdad
literal contiene sólo los builds medidos:

| Tool | SHA-256 | Versión | Modo |
|---|---|---|---|
| TexGen | `0939bc8f…7a62` | 3.0.0.209 | SSE |
| DynDOLOD | `b67625eb…3bd0` | 3.0.0.209 | SSE |

Cada contrato fija: clase de la ventana principal (`TfrmMain`), selector del
Output (`Edit`/`TEdit`, único), **un** mecanismo de escritura
(`LEGACY_IACCESSIBLE` para Alpha-209: `ValuePattern.SetValue` falla con
`COMError -2146233079` 4/4), la acción previa (DynDOLOD: `Advanced >>>`, que
recarga el Output del preset y obliga a reconvergir), la acción de inicio
(`Start` / `OK` estructural `TButton`, nunca un `OK` de `#32770`), el diálogo
terminal (instrucción exacta + botones) y su acción (`Exit TexGen`
`CommandButton_7` / `Save and Exit` `CommandButton_6`; nunca las variantes Zip ni
`Exit DynDOLOD`), y los modales de arranque conocidos.

Un mecanismo que falla es `OUTPUT_WRITE_UNSUPPORTED`; no se prueba otro.

## 4. Driver (P3, P5–P7)

Superficie mutante **separada** de la read-only (que no cambia):

| Módulo | Rol |
|---|---|
| `dyndolod_uia_driver.py` | modelo de instantánea, clasificación de pantalla, pasos puros del helper, conductor async exactly-once |
| `dyndolod_uia_driver_windows.py` | backend COM mutante (único archivo del paquete autorizado a nombrar `SetValue`/`Invoke`; ancla AST) |
| `dyndolod_uia_driver_helper.py` | worker descartable (`--skyclaw-uia-driver-helper`) |
| `dyndolod_uia_driver_ejecutor.py` | ejecutor por helper con deadline duro, kill/reap y Job Object |

Cada paso corre en un proceso descartable (`observar`, `escribir_output`,
`invocar`) con timeout duro; la decisión vive en el padre y el padre
re-clasifica la instantánea que devuelve el helper (no confía en su veredicto).
Máquina de estados:

```text
LAUNCHED → (modal conocido → HITL remoto → Ignore exactly-once)*
→ CONFIGURATION_VISIBLE → Output convergido (write + readback canónico)
→ [DynDOLOD: Advanced exactly-once → reconvergencia]
→ BEGIN_AUTHORIZED (helper re-valida pid/exe/ventana/Output/enabled en el mismo paso)
→ BEGIN_SENT (exactly-once) → BEGIN confirmado (vivo + crecimiento del root o del log)
→ GENERATING (cero mutaciones UI; sólo observación)
→ TERMINAL_READY (diálogo terminal contractual)
→ TERMINAL_ACTION_SENT (exactly-once) → PROCESS_EXITED → post-check EXISTENTE
```

Exactly-once: el ledger del padre marca la acción como intentada ANTES de
spawnear el helper que la invoca. Si el helper muere, se cuelga o el COM
devuelve error después de pedir la acción, el desenlace es
`BEGIN_OUTCOME_UNKNOWN`/`TERMINAL_OUTCOME_UNKNOWN` y no se reintenta.

El post-check existente (exit code + artefacto + log de la corrida + born-empty)
sigue siendo la autoridad final; el driver no lo reemplaza ni lo relaja.

## 5. Configuración y presets (P5/P8)

La configuración (preset de calidad, worldspaces, parámetros de TexGen) la toma
cada herramienta de su preset en `<exe>\Edit Scripts\DynDOLOD\Presets\`.
**Sky-Claw no los edita.** El driver:

1. calcula la huella normalizada de cada preset (SHA-256 sin las líneas
   `OutputPath=`, que es lo único que Sky-Claw corrige por UIA);
2. exige `Preset=<preset pedido>` en el preset de DynDOLOD (`PRESET_MISMATCH`);
3. pide la aprobación remota mostrando esa configuración;
4. re-verifica la huella aprobada inmediatamente antes de cada spawn;
5. guarda un snapshot DURABLE (bytes + SHA-256, o `ABSENT`) bajo
   `SystemPaths.runtime_state_dir()/dyndolod_presets` antes del spawn y lo
   restaura byte-exacto al terminar (P0-B midió que las dos herramientas
   reescriben su preset al iniciar); un residuo de una corrida que murió se
   reconcilia al comienzo de la próxima corrida, bajo el mismo lock de etapa 9.
   `PRESET_RESTORE_FAILED` corta la corrida: no se declara éxito con el estado
   del usuario sin restaurar.

**Límite declarado:** la lista de worldspaces y el preset de calidad NO se leen
de la GUI por UIA (P0 no midió esos controles). Su autoridad es la huella del
archivo aprobada por el operador + el post-check. `WORLDSPACE_MISMATCH` no
existe en esta versión por ese motivo.

## 6. Superficie remota (Telegram / LLM)

* Tools nuevas del agente: `generate_lods` (lanza la etapa 9 como **trabajo en
  segundo plano** y responde enseguida) y `lods_status` (estado del trabajo).
* El trabajo delega al **mismo** `DynDOLODPipelineService` que usa la GUI —el
  que el `SupervisorAgent` construye en el composition root—, publicado en el
  `AppContext` por el bootloader: mismo lock de etapa 9, misma coordinación,
  mismo workspace con su lease, mismas capacidades. No hay segundo constructor.
* Al terminar, el resultado (éxito o código de fallo) llega al operador por la
  superficie de avisos del `HITLGuard` (Telegram).
* La tool del dispatcher (`generate_lods` de la GUI) acepta la misma clave
  `interaction_mode`, así que las dos superficies pueden pedir cualquiera de los
  dos modos (ancla de paridad de claves).
* Requiere la app de escritorio (modo GUI) en ejecución: en `--mode telegram`
  no hay `SupervisorAgent` y la tool responde `success=false` con esa razón.
* **Guard de etapas previas (SOP §5 regla 2).** Antes de lanzar, el gestor pide
  al servicio la evidencia de las etapas 6–8 (`verificar_etapas_previas`:
  `Bashed Patch, 0.esp` y `Synthesis.esp` habilitados en el perfil activo, un
  `*.cgid` en `overwrite/Grass` o en un mod habilitado). Si falta algo, el ÚNICO
  override es una aprobación del operador por el canal remoto (misma categoría
  no auto-aprobable); la tool no tiene un parámetro para saltearlo. Sin canal,
  se corta antes de lanzar. Prueba que los artefactos existen y están activos,
  no que se regeneraron después del último cambio de load order. El camino de la
  GUI (`GenerateLodsStrategy`) conserva su deuda documentada: cablearlo cambia
  el comportamiento para los usuarios actuales y es decisión del owner.

## 7. Errores estables

`CodigoDesatendido` (valores en inglés, estables, congelados por igualdad
literal): `UNATTENDED_CAPABILITY_UNAVAILABLE`, `UNATTENDED_BUILD_UNSUPPORTED`,
`UNATTENDED_CONFIGURATION_NOT_APPROVED`, `PRESET_ABSENT`, `PRESET_MISMATCH`,
`PRESET_RESTORE_FAILED`, `UNEXPECTED_MODAL`, `KNOWN_MODAL_NOT_APPROVED`,
`UIA_UNAVAILABLE`, `UIA_CONTROL_MAP_MISMATCH`, `OUTPUT_WRITE_UNSUPPORTED`,
`OUTPUT_READBACK_MISMATCH`, `BEGIN_NOT_ENABLED`, `BEGIN_OUTCOME_UNKNOWN`,
`BEGIN_NOT_CONFIRMED`, `PROCESS_DIED_PRE_BEGIN`,
`UNEXPECTED_DIALOG_DURING_GENERATION`, `TERMINAL_CONTROL_MISMATCH`,
`TERMINAL_OUTCOME_UNKNOWN`, `UNATTENDED_CONTRACT_VIOLATION`. El timeout global
sigue siendo `DynDOLODTimeoutError` (no se duplica como `GENERATION_TIMEOUT`).

## 8. Qué no cambia

`external_work_root`, born-empty, ownership, spawn strategy, dominio de
visibilidad, broker MO2/USVFS, verificación de plugins esperados, handoff de
TexGen, packaging, rollback, post-check, el protocolo ASISTIDO y su HITL.

## 9. Pendiente para declarar DONE (#661)

1. Rig real Alpha-209 **standalone**: TexGen y DynDOLOD desatendidos con preset
   rancio, decoy vacío, Output corregido, begin/terminal exactly-once, post-check
   y packaging PASS, preset restaurado por SHA, procesos residuales = [].
2. Rig **brokered** MO2/USVFS (después del standalone).
3. Evidencia commiteada bajo `docs/validation/` y la fila del inventario
   actualizada con los builds realmente soportados.
4. Superficie GUI del modo desatendido (botón/selector en el ritual): fuera de
   este cambio; hoy se pide por Telegram/`/api/chat` o por el payload del
   dispatcher.
5. Verificación en Windows del backend COM y del helper: hasta ahora sólo
   lectura de código, tests con GUI simulada y mypy con `--platform win32`.
