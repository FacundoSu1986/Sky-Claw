# Runtime Vault — auditoría de lanzamiento MO2 desde Stock Game

> **Fecha:** 2026-08-22  
> **Estado:** evidencia histórica fechada; no fuente viva por sí sola.  
> **Ámbito:** comportamiento real de MO2 + Skyrim SE 1.6.1170 + Steam durante dos lanzamientos desde un Stock/Golden externo.  
> **Modo de la auditoría original:** read-only/forense después de los lanzamientos.  
> **Objetivo:** preservar evidencia de rig que sirva de referencia para Runtime Vault sin versionar logs crudos ni payloads de Creation Club.

## 1. Resumen ejecutivo

La auditoría reconstruyó dos ejecuciones realizadas mediante Mod Organizer 2 (MO2) con el game path configurado hacia un Stock Game externo:

```text
G:\Modding\Skyrim_Stock_1.6.1170
```

Los dos ejecutables se originaron físicamente desde ese árbol, no desde la instalación administrada por Steam:

- **RUN_A:** `SkyrimSELauncher.exe` desde el Stock bajo USVFS de MO2; después se inició `SkyrimSE.exe` y el cliente Steam fue invocado para AppID `489830`.
- **RUN_B:** `SkyrimSE.exe` desde el Stock directamente bajo USVFS de MO2, con Steam ya en memoria.

El resultado más importante para Runtime Vault es doble:

1. MO2 puede ejecutar un runtime de Skyrim ubicado fuera del directorio administrado por Steam.
2. El Stock/Golden no debe considerarse un runtime operativo inmutable: la prueba observó escrituras reales dentro de `Data`, además de escrituras de estado redirigidas por MO2.

## 2. Entorno observado

```text
Skyrim runtime:          1.6.1170.0
Steam AppID:             489830
Stock Game:              G:\Modding\Skyrim_Stock_1.6.1170
Steam Game:              G:\SteamLibrary\steamapps\common\Skyrim Special Edition
MO2 base:                G:\Modding\MO2\SkyrimSE
MO2 profile:             Default
MO2 game path:           G:\Modding\Skyrim_Stock_1.6.1170
Manifest:                G:\SteamLibrary\steamapps\appmanifest_489830.acf
Manifest ReadOnly:       True
Manifest buildid:        13189953
Manifest StateFlags:     518
```

La ruta del archivo de configuración de MO2 se sanitiza en este documento como:

```text
%LOCALAPPDATA%\ModOrganizer\Skyrim Special Edition\ModOrganizer.ini
```

## 3. Provenance de los lanzamientos

### RUN_A — Launcher desde Stock

La configuración activa de MO2 apuntaba a:

```text
Binary:            G:/Modding/Skyrim_Stock_1.6.1170/SkyrimSELauncher.exe
Working Directory: G:/Modding/Skyrim_Stock_1.6.1170
```

La evidencia forense de USVFS/procesos indicó que `SkyrimSELauncher.exe` fue iniciado desde ese path y derivó en la ejecución de `SkyrimSE.exe`. Steam fue invocado mediante AppID `489830` (`steam://run/489830//`) como parte de la sesión.

**Clasificación:** `DIRECT_EVIDENCE` en la auditoría original.

### RUN_B — SkyrimSE.exe desde Stock

La configuración activa de MO2 apuntaba a:

```text
Binary:            G:/Modding/Skyrim_Stock_1.6.1170/SkyrimSE.exe
Working Directory: G:/Modding/Skyrim_Stock_1.6.1170
```

La evidencia forense indicó inicio directo de `SkyrimSE.exe` desde el Stock bajo USVFS de MO2.

**Clasificación:** `DIRECT_EVIDENCE` en la auditoría original.

## 4. Intervención de Steam

Durante la ventana auditada:

```text
STEAM_PROCESS_PRESENT=YES
STEAM_PARENT_OF_GAME=NO
STEAM_UPDATE_ACTIVITY=BLOCKED
MANIFEST_READONLY=True
```

Se observaron cambios de estado de AppID `489830` y carga del overlay sobre `SkyrimSE.exe`. También se observaron intentos de actualización que terminaron en `Disk write failure` mientras el `appmanifest_489830.acf` permanecía con atributo ReadOnly.

Extracto mínimo preservado del reporte original:

```text
AppID 489830 state changed : Update Required,Fully Installed,Update Queued,App Running,
...
Disk write failure
```

Esto demuestra el comportamiento observado en esta corrida; no constituye una garantía universal de que ReadOnly pueda bloquear cualquier comportamiento futuro de Steam.

## 5. Escrituras de usuario y VFS de MO2

La auditoría separó las escrituras fuera del Stock de las escrituras físicas dentro de él.

### Estado de perfil / usuario

Se observaron escrituras normales en el perfil `Default` de MO2, entre ellas `skyrim.ini`, `skyrimprefs.ini`, `archives.txt`, `loadorder.txt`, `plugins.txt`, `lockedorder.txt` e `initweaks.ini`.

También se creó el directorio de saves del usuario al iniciar el motor.

### MO2 overwrite — Creation Club / Anniversary Edition

MO2 capturó en:

```text
G:\Modding\MO2\SkyrimSE\overwrite
```

un total de:

```text
140 archivos
4,682,905,181 bytes (~4.36 GiB)
```

correspondientes al payload observado de **Creation Club / Skyrim Anniversary Edition**.

Estos ~4.36 GiB **no se clasifican como basura ni como corrupción por sí mismos**. Son evidencia de contenido de Creation Club/Anniversary Edition capturado/redirigido mediante el VFS de MO2 durante la ejecución.

## 6. Integridad estructural del Stock

La estructura del Stock seguía coincidiendo con el baseline canónico:

```text
STOCK_FILE_COUNT=16207
EXPECTED_FILE_COUNT=16207
STOCK_TOTAL_BYTES=18212317443
EXPECTED_TOTAL_BYTES=18212317443
STOCK_REPARSE_POINTS=0
```

El archivo `SKYCLAW_STOCK_GAME_INFO.txt`, creado después de la verificación inicial, fue excluido del conteo canónico.

### Hashes críticos

| Archivo | SHA-256 observado | Match canónico |
|---|---|---|
| `SkyrimSE.exe` | `C434208894F07F604B852F29B8EDC3A58C4DE63DE783373733E72B2B73F33BE9` | Sí |
| `SkyrimSELauncher.exe` | `CE2A1B3F9727C8B53FFB8E0BCCD2BBED2BD8BEC47B0AB8552C2850CAA05E68B1` | Sí |
| `bink2w64.dll` | `653247E35DB8D6453E83A008C805A877FD2D56A1D844282F9065CE2F34388FEC` | Sí |

## 7. Comparación completa Stock ↔ Steam

La comparación SHA-256 completa produjo:

```text
FILES_HASHED=16207
MISSING_IN_STOCK=0
EXTRA_IN_STOCK=0
SIZE_MISMATCH=0
HASH_MISMATCH=2
```

Por tanto:

```text
HASH_IDENTICAL=16205/16207
```

Los dos mismatches de contenido fueron:

```text
Data/ccbgssse037-curios.bsa
Data/ccbgssse037-curios.esl
```

Ambos conservaban el mismo tamaño que su referencia, pero su SHA-256 era distinto después de la ejecución.

La auditoría los clasificó como `CONTENT_CHANGE` producido durante la actividad in-engine de Creation Club/Bethesda.

Otros archivos de Creation Club mostraron cambios de timestamps/metadata pero conservaron hashes idénticos a la referencia Steam. `Debug.log` también fue escrito/tocado por el Launcher pero terminó con contenido/hash idéntico al archivo de referencia.

### Corrección editorial del veredicto original

El reporte original incluyó el rótulo:

```text
STOCK_CONTENT_INTEGRITY=METADATA_TOUCHED_CONTENT_UNCHANGED
```

Ese rótulo no describe correctamente el árbol completo, porque la misma auditoría demostró dos `HASH_MISMATCH` reales. Para esta evidencia histórica se adopta la clasificación coherente con los datos:

```text
STOCK_LAUNCH_AUDIT_FOUND_STOCK_WRITES
```

No se afirma que el Stock permaneciera byte-for-byte intacto.

## 8. Timeline resumido

| Hora aprox. | Evidencia | Evento |
|---|---|---|
| 11:30 | MO2/USVFS | RUN_A inicia `SkyrimSELauncher.exe` desde el Stock. |
| 11:30 | Steam | Steam se inicia/invoca para AppID 489830. |
| 11:32 | MO2/USVFS | RUN_B inicia `SkyrimSE.exe` desde el Stock. |
| 11:32 | Steam | AppID 489830 pasa a estado `App Running`; overlay engancha `SkyrimSE.exe`. |
| 11:32–11:35 | MO2 overwrite / Stock | Actividad de Creation Club; 140 archivos quedan en `overwrite`. |
| 11:35 | Stock `Data` | `ccbgssse037-curios.bsa/.esl` cambian de contenido. |
| 11:36 | MO2 profile | Se actualizan preferencias/archives al cerrar Skyrim. |
| 12:37 | Steam content log | Intento de actualización termina en `Disk write failure`. |

Los timestamps se conservan únicamente como reconstrucción de esta corrida; no son parte de una identidad persistente de Runtime Vault.

## 9. Implicaciones para Runtime Vault

| Propiedad | Resultado | Alcance de la evidencia |
|---|---|---|
| `MO2_EXTERNAL_RUNTIME` | `PROVEN` | MO2 ejecutó `SkyrimSE.exe` físicamente desde el Stock externo. |
| `MO2_EXTERNAL_LAUNCHER` | `PROVEN` | MO2 ejecutó `SkyrimSELauncher.exe` físicamente desde el Stock externo. |
| `STEAM_AUTH_EXTERNAL_RUNTIME` | `PROVEN` para esta corrida | Steam reconoció AppID 489830/overlay mientras el ejecutable estaba fuera del directorio Steam. |
| `UPDATE_GUARD_BLOCKED_STEAM_WRITE` | `OBSERVED` | Los intentos registrados terminaron en `Disk write failure` con el manifest ReadOnly. |
| `GOLDEN_EXECUTION_CAN_CAUSE_WRITES` | `PROVEN` | Dos archivos de `Data` terminaron con hashes diferentes. |
| `GOLDEN_SHOULD_NOT_BE_USED_AS_RUNTIME` | `SUPPORTED` | Consecuencia arquitectónica respaldada por esta prueba; no una garantía universal. |
| `GOLDEN_RUNTIME_SEPARATION_REQUIRED` | `SUPPORTED` | Refuerza el diseño Golden Master → copia Runtime independiente. |

Esta auditoría no implementa ni valida por sí sola RV-1/RV-2. Es evidencia de rig para orientar y posteriormente validar esos contratos.

## 10. Evidencia fuente y política de conservación

Los logs crudos de Steam/MO2, eventos de Windows y payloads de Creation Club **no se versionan en Git** con este informe.

La fuente entregada para construir esta referencia fue el transcript de la auditoría forense:

```text
SOURCE_ARTIFACT=Pasted text(20260822-155459).txt
SOURCE_SIZE=53048 bytes
SOURCE_SHA256=cd2bd4e266eff4f1384daae334f80171df3b35537972b07b88d07559e669fc80
```

El transcript contiene los comandos ejecutados, salidas relevantes y el reporte final de la auditoría.

Los hashes SHA-256 de los **logs crudos locales individuales** no estaban disponibles para esta operación de documentación y, por tanto, no se inventan ni se consignan. Si se preservan en una futura captura de evidencia, deben registrarse separadamente como provenance sin subir necesariamente los logs completos al repositorio.

## 11. Límites

- Una corrida física no prueba comportamiento universal de todas las versiones de MO2, Steam o Skyrim.
- `ReadOnly` en el manifest fue efectivo en los intentos observados, no se presenta como garantía contractual de Valve.
- Los 140 archivos de Creation Club observados en `overwrite` se describen como payload de Anniversary Edition/Creation Club, no como corrupción.
- El Stock usado en esta prueba debe volver a verificarse/restaurarse antes de volver a declararlo Golden Master byte-for-byte.
- Este documento es evidencia histórica. Antes de convertir una conclusión en comportamiento productivo de Sky-Claw, debe revisarse código, tests, ADRs y estado vigente del proyecto.

## 12. Veredicto de la prueba

```text
MO2_EXTERNAL_STOCK_LAUNCH_CONFIRMED
STOCK_LAUNCH_AUDIT_FOUND_STOCK_WRITES
```

La prueba confirma que MO2 puede lanzar un runtime externo y, al mismo tiempo, demuestra que ejecutar directamente el Golden/Stock permite escrituras observables. Runtime Vault debe preservar la separación entre una referencia verificada y una copia operativa destinada a ejecución.