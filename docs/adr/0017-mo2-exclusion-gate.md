# ADR 0017 — Exclusión de mutación de `modorganizer.ini` (P5-C2)

**Fecha:** 2026-11-01
**Estado:** Propuesta (PR Draft). Alcance: `SYNTHETIC_ONLY` en lo productivo.
**Tracker:** #672
**Base:** `origin/main` `9fbf0b2f` (P5-C1 mergeado en `ac19cb28`, CI `41249009598` verde).
**Rama:** `feat/frozen-runtime-mo2-exclusion-gate`
**Complementa:** ADR 0012 §13/§29.4/§34.4 y ADR 0015.

## 1. Contexto

P5-C1 dejó declarado, con honestidad, `EXCLUSION_WITH_MO2 = NOT_ESTABLISHED`. El
binder comprueba quiescencia (no hay `ModOrganizer.exe` vivo), revalida el INI
bajo el lock y escribe con temporal + `os.replace`. Entre la última revalidación
y el `os.replace` queda una ventana que ningún lock de Sky-Claw cierra, porque
MO2 no participa del lock.

Este ADR resuelve esa ventana con evidencia medida, o la declara bloqueada.

## 2. La pregunta, formulada de forma que se pueda falsificar

El requisito no es «impedir que el usuario abra MO2» —eso exigiría inyección,
hooks o bloqueo del sistema, y está prohibido por el brief—. Es más estrecho y
más fuerte:

> Mientras la transición muta el INI, **ningún otro proceso puede escribir,
> truncar, borrar ni renombrar** ese archivo.

Que otro proceso lo **lea** no abre la ventana: una lectura no cambia los bytes
que el binder va a reemplazar. Confundir ambas cosas es lo que hace que la
mayoría de las «soluciones» de este problema sean peores que el problema.

## 3. Evidencia experimental

Rig aislado en `E:/Skyclaw_Rig_MO2_Exclusion`, Windows 10 19045, con **procesos
externos reales** (subprocesos del SO, no hilos ni mocks). Cada celda se mide
sobre un directorio y un handle nuevos: la primera versión del rig medía varias
sondas sobre el mismo archivo y una sonda que borraba el archivo contaminaba las
siguientes, haciendo parecer que el `dwShareMode` causaba fallas que eran
artefacto de orden.

### 3.1 Matriz de `dwShareMode`

`CREATE`/`OPEN_EXISTING`, acceso del tenedor `GENERIC_READ|DELETE` salvo donde se
indica. «Bloquea» = el proceso externo recibe error.

| Compartición propia | lectura externa | escritura externa | `MoveFileEx` externo | `ReplaceFileW` externo | `DeleteFile` externo | `os.replace` propio |
|---|---|---|---|---|---|---|
| `0` | bloquea | bloquea | bloquea | bloquea | bloquea | bloquea |
| `READ` | permite | bloquea | bloquea | bloquea | bloquea | **bloquea** |
| `READ\|WRITE` | permite | **permite** | bloquea | bloquea | bloquea | bloquea |
| `READ\|DELETE` | permite | bloquea | bloquea | **permite** | **permite** | bloquea |
| `READ\|WRITE\|DELETE` | permite | **permite** | bloquea | **permite** | **permite** | bloquea |

Dos hechos que la tabla deja establecidos y que el brief pedía no asumir:

1. **`FILE_SHARE_DELETE` propio habilita el reemplazo ajeno.** Concederlo no
   «amplía compatibilidad»: reabre exactamente la ventana que se quiere cerrar.
2. **`os.replace` es incompatible con cualquier exclusión útil.** Con
   `FILE_SHARE_READ` —la única configuración que bloquea escritura, borrado y
   reemplazo externos— el reemplazo por ruta del propio binder falla con
   `ERROR_ACCESS_DENIED` (5). No es un detalle de implementación: es la razón por
   la que el protocolo de escritura tiene que cambiar.

### 3.2 El reemplazo atómico bajo exclusión es imposible

Se probaron las tres formas de instalar contenido nuevo mientras se sostiene el
handle:

| Mecanismo | Resultado medido |
|---|---|
| `os.replace` / `MoveFileExW(REPLACE_EXISTING)` | `ERROR_ACCESS_DENIED` (5) |
| `ReplaceFileW` | `ERROR_SHARING_VIOLATION` (32) |
| `SetFileInformationByHandle(FileRenameInfoEx)` con `POSIX_SEMANTICS` sobre el destino retenido | `ERROR_SHARING_VIOLATION` (32) |

`FILE_RENAME_FLAG_POSIX_SEMANTICS` **no** salta el chequeo de compartición del
destino. Es un resultado negativo importante: descarta la variante «un solo paso
atómico» que habría sido la solución ideal.

Sí funciona `FileRenameInfoEx` **sobre el propio handle** (renombrar el archivo
retenido): la identidad de archivo sobrevive al rename (`file_id` antes ==
después). Eso habilita un protocolo de dos pasos, que se midió y se descartó (§3.3).

### 3.3 El rename-away abre una carrera peor que la que cierra

Protocolo «rename-away + install»: renombrar `ini → ini.bak` por el handle,
después `tmp → ini`. Deja el **nombre** libre entre los dos renombres.

Bajo un creador externo en bucle (`CREATE_NEW` sobre `modorganizer.ini`), 40
transiciones:

| Protocolo | Victorias del agresor | Transiciones fallidas |
|---|---|---|
| rename-away + install | **40 / 40** | **27 / 40** (`FALLA_INSTALL(32)`) |
| escritura in-place por el handle | **0 / 123.910** | 0 / 40 |

El creador no sólo gana el nombre: hace **fallar la instalación con el original
ya movido**. Un protocolo que convierte una escritura ajena en una transición
rota a mitad de vuelo es peor que la ventana que venía a cerrar.

### 3.4 La escritura in-place se puede observar rasgada

Con acceso `GENERIC_READ|GENERIC_WRITE` (sin `DELETE`), los lectores externos
siguen abriendo el archivo. Midiendo 600 escrituras in-place de ~130 B con un
lector estilo CRT en bucle:

| Lecturas completas | Lecturas rasgadas |
|---|---|
| 109.270 | **72** |

72 sobre 109.270 es 0,066 %: suficiente para descartar «no pasa», insuficiente
para llamarlo seguro. Un MO2 que arrancara en esa ventana leería un INI
truncado. Por eso la configuración final **sí** incluye `DELETE`: bloquea a los
lectores y convierte la lectura rasgada de improbable en imposible por
construcción.

### 3.5 Configuración final y su validación

```text
dwDesiredAccess = GENERIC_READ | GENERIC_WRITE | DELETE
dwShareMode     = 0            (FILE_SHARE_NONE)
```

> **Corrección (auditoría del PR #851).** La primera versión usaba
> `FILE_SHARE_READ` y afirmaba que, con eso, la lectura rasgada era «imposible
> por construcción». **Era falso.** Ver §3.7: un lector compatible abre igual y
> observa la escritura a medias. La compartición es `0` desde esta corrección.

Medido con el handle retenido:

| Sonda | Resultado |
|---|---|
| escritura externa (share RWD) | `ERROR_SHARING_VIOLATION` (32) |
| escritura externa (share RW) | `ERROR_SHARING_VIOLATION` (32) |
| apertura externa con sólo `DELETE` | `ERROR_SHARING_VIOLATION` (32) |
| `DeleteFile` externo | `ERROR_SHARING_VIOLATION` (32) |
| `ReplaceFileW` externo | `ERROR_SHARING_VIOLATION` (32) |
| escritura por un alias **hardlink** del mismo archivo | `ERROR_SHARING_VIOLATION` (32) |
| lectura estilo CRT (`open()` de Python) | bloqueada |
| lectura **compatible** (`GR` + `share RWD`) | **bloqueada** (con `FILE_SHARE_READ` abría) |
| apertura sólo de atributos (`FILE_READ_ATTRIBUTES`) | abre, pero `ReadFile` da `ACCESS_DENIED` (5) y 0 bytes |
| `os.stat` por ruta | funciona (`GetFileAttributesExW`, no abre handle) |
| segunda exclusión del mismo archivo | `EXCLUSION_NOT_ESTABLISHED` |
| liberación tras muerte abrupta del tenedor | liberada (el kernel cierra el handle) |

La exclusión es sobre el **objeto de archivo**, no sobre el nombre: un hardlink
del mismo archivo también queda bloqueado, porque el chequeo de compartición es
por objeto.

### 3.7 La lectura rasgada SÍ era observable con `FILE_SHARE_READ`

La validación de compartición es **bidireccional**: el acceso pedido tiene que
estar contenido en el share existente *y* el acceso existente en el share pedido.
Con `share = READ` del lado del tenedor, un lector que pida `GENERIC_READ` y
conceda `READ|WRITE|DELETE` satisface las dos direcciones y **abre**.

Medido con la ventana real de la escritura ensanchada (`WriteFile` sin
`SetEndOfFile`, con el corte **después** del primer byte divergente):

| Configuración del tenedor | Lector compatible | Lector CRT | Lecturas rasgadas |
|---|---|---|---|
| `GR\|GW\|DEL` + `share READ` | **abre** | bloqueado | **11.038** |
| `GR\|GW\|DEL` + `share 0` | bloqueado | bloqueado | **0** (no puede abrir) |
| `GR\|GW` + `share READ` | abre | abre | 11.739 |

Un corte **antes** de la divergencia no cambia ningún byte: el prefijo común de
un INI antes y después del cambio de `gamePath` mide 89 de 110 bytes. La primera
medición cortaba en `len//3 = 38` —dentro del prefijo— y por eso daba cero
rasgadas. Era un artefacto del corte, no una propiedad del protocolo.

### 3.8 Costo de `FILE_SHARE_NONE`, medido

| Costo | Medición |
|---|---|
| adquisición frente a un lector compatible | 195/200 con 1 intento; **200/200 con 40 reintentos de 5 ms** |
| `os.stat` por ruta mientras se retiene | funciona |
| lectura por ruta (Python) | falla (ya fallaba con `READ`) |
| escritura propia por el handle | funciona |
| liberación al cerrar | correcta |

El único costo real es una negativa transitoria de adquisición, que el reintento
acotado ya existente absorbe. Por eso se adopta.

### 3.6 La adquisición puede fallar, y el reintento lo resuelve

Un lector que tenga el archivo abierto en el instante del intento hace fallar la
apertura exclusiva (su compartición no concede `DELETE`). Medido: **291 de 300**
adquisiciones fallan sin reintento; con 40 reintentos y 5 ms, **0 de 300**.

El reintento es acotado a propósito: esperar indefinidamente convierte una
negativa en un cuelgue, y un cuelgue en la sección crítica no es fail-closed, es
fail-stuck.

## 4. Decisión

1. Se implementa `mo2_ini_exclusion`, un componente pequeño y reutilizable que
   toma la exclusión con la configuración de §3.5 y expone tres veredictos:
   `EXCLUSION_ESTABLISHED`, `EXCLUSION_NOT_ESTABLISHED`, `INDETERMINATE`. Sólo el
   primero autoriza mutar, y el conjunto está enumerado para que un estado nuevo
   no herede autorización.
2. El binder escribe el INI **por el handle retenido**
   (`mo2_ini_escritura.actualizar_game_path_por_handle`), no por ruta. La
   comprobación de identidad y la mutación usan el **mismo handle**, así que no
   queda ventana entre comprobar y mutar.
3. La huella contra la que se compara es la del **snapshot durable**, no la de
   otra lectura por ruta: es el estado al que el rollback volvería.
4. Se mantiene la quiescencia de procesos como defensa en profundidad. La
   exclusión es la garantía; la quiescencia es el diagnóstico temprano.
5. La activación productiva sigue deshabilitada. Este ADR no la habilita.

## 5. Consecuencias

### 5.1 La escritura deja de ser atómica, y eso obliga a tocar la recuperación

`os.replace` publicaba el archivo nuevo de una vez. Escribir por handle deja un
estado intermedio si el proceso muere entre `WriteFile` y `SetEndOfFile`.

Ese estado intermedio **rompía el discriminador de escritura ajena** de
`binder_recovery`. El discriminador estaba calibrado para un reemplazo atómico,
donde el archivo es o el viejo o el nuevo; un archivo a medio escribir no es
ninguno de los dos, así que se clasificaba como «lo escribió un tercero» y el
rollback **se negaba a restaurarlo**, dejando el INI truncado. Lo detectó el test
de escritura parcial, no una revisión.

Se corrige reconociendo el estado intermedio de la escritura por handle.

**El predicado se calibró matando procesos reales**, no construyendo bytes a mano
(rig `exp9_death_points`). Matando el escritor en cada punto, los desenlaces
observados son exactamente tres:

| Muerte | Estado en disco |
|---|---|
| antes de `WriteFile` | idéntico al snapshot |
| durante `WriteFile` | `nuevo[:k] + previos[k:]`, mismo tamaño que el snapshot |
| tras `SetEndOfFile` | completo (`nuevo`) |

`FlushFileBuffers` no cambia lo observable: sólo la durabilidad, que sigue en
`POWER_LOSS_GUARANTEE = NOT_CLAIMED`.

Por eso se acepta **un solo** estado intermedio, con dos condiciones que lo hacen
atribuible: mismo tamaño que el snapshot (el truncado no ocurrió) y corte
**estrictamente posterior** al primer byte divergente. Un corte anterior a la
divergencia no modifica ningún byte —el archivo seguiría siendo el snapshot— y un
estado que sólo coincide en el prefijo común es indistinguible de un truncado
ajeno.

La versión anterior aceptaba además `nuevo[:n]` (archivo truncado). Se quitó:
`SetEndOfFile` corre sólo después de una escritura completa, así que ese estado
**no es alcanzable** por este escritor y atribuírselo sería apropiarse del
truncado de un tercero.

**Cuando la autoría no se establece, no se elige: se conserva evidencia y se pide
intervención del propietario.** El rollback copia los bytes actuales a
`<storage>/recovery_evidence/<transition_id>/<superficie>-<sha256>.bin` antes de
abstenerse, así que ni lo viejo (el snapshot) ni lo nuevo (la escritura ajena) se
pierden.

### 5.2 Mientras dura la transición, el INI no es legible por NADIE

Con `dwShareMode = 0` ningún proceso externo abre el archivo para leer contenido:
ni `open()` del CRT, ni un lector que conceda `READ|WRITE|DELETE`. La única
apertura que Windows sigue permitiendo es la de **atributos**
(`FILE_READ_ATTRIBUTES`), y medido: sobre ese handle `ReadFile` falla con
`ACCESS_DENIED` (5) y devuelve 0 bytes.

La garantía se enuncia sobre el **contenido**, no sobre la apertura. Decir «ningún
proceso puede abrir el archivo» sería falso por ese caso, y una afirmación que el
propio sistema refuta no sirve como garantía.

Consecuencia operativa: el binder lee y escribe por el handle y libera **antes**
de los readbacks por ruta. El código existente sigue funcionando fuera de la
sección crítica.

Costo asumido: si MO2 arranca justo en la ventana, no puede leer su INI y falla
de forma ruidosa. Es preferible a leer un INI a medias en silencio —que es lo que
pasaba con `FILE_SHARE_READ` y motivó la corrección de §3.7—.

### 5.3 Un lector concurrente puede impedir la adquisición

Fail-closed, con reintento acotado. No hay corrupción posible; hay una negativa
con diagnóstico.

## 6. Alternativas descartadas, con el motivo medido

| Alternativa | Por qué no |
|---|---|
| `dwShareMode = 0` | Excluye todo, pero también la lectura: deja el INI inaccesible durante la transición sin ganar nada sobre §3.5. |
| `FILE_SHARE_READ\|FILE_SHARE_DELETE` | Habilita `ReplaceFileW` y `DeleteFile` externos. No excluye. |
| `FILE_SHARE_READ\|FILE_SHARE_WRITE` | Habilita la escritura externa. No excluye. |
| Rename-away + install | 40/40 victorias del agresor, 27/40 transiciones rotas. |
| `LockFileEx` por rangos | Bloquea escritura in-place, pero no `DeleteFile` ni el reemplazo: no cierra la ventana. |
| Mutex/protocolo propio de Sky-Claw | Coordina sólo a los participantes de Sky-Claw. MO2 no participa. |
| Inyección de DLL / hooks / ACL masivas / UAC | Prohibidos por el brief, y desproporcionados. |

## 7. Qué NO afirma este ADR

- `POWER_LOSS_GUARANTEE = NOT_CLAIMED`. Se hace `FlushFileBuffers` sobre el
  handle; no hay `fsync` de directorio ni WAL.
- `QT_QSETTINGS_EQUIVALENCE = NOT_CLAIMED`. Sin cambios respecto de P5-C1.
- **No** se probó contra una instalación real de MO2. El modo de compartición con
  el que Qt abre `modorganizer.ini` está **sin medir**; lo medido es el patrón
  `share = READ|WRITE`, que es el del CRT de Python y el documentado para
  `QFSFileEngine`. Confirmarlo contra MO2 real es requisito de P7.
- **No** se probó el caso de reparse point: crear symlinks en esta máquina
  requiere privilegio que la sesión no tiene (`ERROR_PRIVILEGE_NOT_HELD`, 1314).
  El gate lo **rechaza** y el test queda en `skip`; el comportamiento real de un
  INI enlazado queda `INDETERMINATE`, y por lo tanto nunca autoriza.
- No se promete que Steam permita arrancar una versión downgrade. Eso es P7.

## 8. Requisitos que esta decisión impone a P7

1. Medir el modo de compartición real con el que MO2 abre su INI, y confirmar
   que la transición no le impide arrancar cuando la ventana está cerrada.
2. Confirmar que un lector bloqueado durante la ventana produce un error
   visible de MO2 y no una configuración por defecto persistida.
3. Ejecutar la matriz P7-A..P7-G de `scripts/p7_readonly_inspection.py`.
