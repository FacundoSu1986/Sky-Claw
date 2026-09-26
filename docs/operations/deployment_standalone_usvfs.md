# Sky-Claw standalone y la USVFS de MO2: dos backends, dos dominios de verdad

> **Audiencia:** operadores que instalan y corren Sky-Claw contra una instancia
> de Mod Organizer 2.
>
> **Estado:** Implementado. Describe cómo se lanzan las herramientas hoy, no una
> configuración a elegir.
>
> **Fuentes canónicas:** `sky_claw/local/tools/output_targets.py`,
> `sky_claw/local/tools/_process.py`, `sky_claw/local/mo2/vfs.py`,
> `sky_claw/local/mo2/brokered_loot.py`,
> `sky_claw/local/mo2/brokered_dyndolod.py`,
> `sky_claw/local/mo2/mod_effectivity.py`,
> `sky_claw/local/validators/vfs_visibility.py`,
> `sky_claw/local/validators/texgen_visibility.py`.
>
> **Última verificación:** 2026-09-26 (bifurcación standalone/brokered, PR-586D)
> sobre `origin/main` `b985404`.

## La bifurcación, en una frase

Sky-Claw tiene DOS backends de lanzamiento y cada uno vive en un namespace
distinto:

- **Backend standalone (histórico):** las herramientas del pipeline (LOOT,
  xEdit, Wrye Bash, Pandora, BodySlide, Synthesis, DynDOLOD/TexGen) se spawnean
  **directo**, con `asyncio.create_subprocess_exec` a través de
  `sky_claw/local/tools/_process.py`. **Ninguna hereda la USVFS de MO2**: leen y
  escriben en el filesystem **físico**, no en el árbol virtual que ves en la
  interfaz de MO2. El único proceso que Sky-Claw lanza a través del proxy
  `ModOrganizer.exe` es **el juego** (`sky_claw/local/mo2/vfs.py`).
- **Backend brokered MO2/USVFS (PR-586):** TexGen/DynDOLOD pueden lanzarse como
  jobs del broker (`sky_claw/local/mo2/vfs_broker.py` + bridge
  `skyclaw_bridge`), y entonces el proceso y sus descendientes **sí** ven el
  overlay virtual del perfil activo. La atestación (worker + proceso nieto)
  prueba esa visibilidad antes de ejecutar nada.

Todo lo demás de esta página describe el backend standalone y sigue siendo
válido para él. La única zona donde los dos backends se cruzan es **el gate de
TexGen → DynDOLOD**, que desde PR-586D es *mode-aware*: la pregunta —¿DynDOLOD
va a ver exactamente el TexGen Output autorizado?— se responde **en el dominio
donde la verdad existe** para cada backend:

```text
standalone → visibilidad byte-exact en el Data físico
brokered   → perfil MO2 + mod habilitado/efectivo + evidencia runtime USVFS
```

Los dos gates fallan cerrado. La bifurcación NO elimina la invariante: cambia
la forma de demostrarla.

## Por qué te importa: el modo de falla silencioso

La USVFS de MO2 es un árbol virtual. Existe **solo dentro de los procesos que
MO2 lanza**. Un proceso externo que abra `<juego>/Data` ve el `Data` real del
disco.

Si tu instancia de MO2 está en USVFS estándar (el default) y el árbol de mods
**no está materializado a disco**, entonces:

- los tools leen el `Data` del juego base, **sin un solo mod**;
- LOOT ordena una lista casi vacía, Wrye Bash arma un Bashed Patch sin
  contenido, DynDOLOD genera LODs del juego base;
- **cada paso termina con éxito.** No hay error: las herramientas hicieron bien
  su trabajo sobre el árbol que les tocó ver.

Es una falla silenciosa: el pipeline reporta verde de punta a punta y el
resultado no sirve.

## Qué hace Sky-Claw para detectarlo

Antes de cada ritual mutante, el preflight corre un **sensor de visibilidad de
mods** (`sky_claw/local/validators/vfs_visibility.py`). Compara los plugins que
el perfil activa contra los que son visibles en el `Data` que el tool va a leer.

El criterio es **deliberadamente conservador**: corta en ROJO solo ante el caso
inequívoco — el perfil activa N plugins de mods y **ninguno** es visible. Eso
distingue "la USVFS no se heredó" de una materialización parcial (algunos mods
desplegados y otros no), que pasa en verde a propósito: frenar un setup que hoy
funciona sería peor que dejar pasar un caso ambiguo.

**Consecuencia operativa:** un preflight en verde te dice que *algo* del perfil
es visible, **no** que todo lo esté. Con una materialización parcial, el sensor
no te va a avisar.

### El caso de TexGen → DynDOLOD tiene además su propio gate

El sensor de arriba mide el modlist antes del ritual. La etapa 9 tiene una
segunda frontera, **dentro** de la corrida, y es de otra naturaleza: lo que
DynDOLOD necesita ver no es un mod que ya estaba, sino la salida que TexGen
**acaba de generar en esta misma corrida**. Empaquetarla en
`<mo2>/mods/TexGen Output` es entrega, no despliegue.

Ese gate es **mode-aware** desde PR-586D y demuestra el handoff en el dominio
del backend que va a consumir el artifact:

- **Standalone:** DynDOLOD se lanza directo contra el `-d:<Data>` físico, así
  que el mod le es invisible salvo que lo hayas materializado. Antes de
  spawnear, Sky-Claw recorre el staging de TexGen **completo** y exige que cada
  archivo aparezca bajo `<Data>/textures` con los **mismos bytes** — no le
  alcanza con que exista ni con que tenga el mismo tamaño, porque un despliegue
  de una corrida anterior cumple las dos cosas
  (`sky_claw/local/validators/texgen_visibility.py`).
- **Brokered:** DynDOLOD se lanza bajo la USVFS del perfil activo, donde el
  Data físico vacío NO es un fallo. El gate exige entonces perfil idéntico al
  del job, `+TexGen Output` habilitado en ese perfil, identidad del artifact,
  **efectividad byte-exact** sobre el overlay completo (sin mod de mayor
  prioridad ni `overwrite` entregando bytes distintos) y una **evidencia
  runtime USVFS** (probe `health` atestiguado por worker + nieto sobre un
  canary del propio mod) (`sky_claw/local/mo2/brokered_dyndolod.py`,
  `sky_claw/local/mo2/mod_effectivity.py`). El reparto de la evidencia es
  exactamente éste: los **1.448 archivos** del artifact se prueban con
  **identidad + efectividad exhaustivas host-side** (recorrido completo, byte a
  byte, contra el overlay), y la **prueba runtime USVFS** cubre **canary(s)
  representativos** del propio mod — un archivo representativo no certifica
  1.448 rutas, y el recorrido host-side no prueba que el mapping se aplique.
  Ninguna de las dos evidencias reemplaza a la otra.

  Y el pase del gate queda **ligado al spawn** (cierre de la ventana TOCTOU):
  el estado exacto aprobado —perfil + fingerprint, identidad del artifact,
  enablement, efectividad y canary— se revalida completo en el boundary del
  spawn de DynDOLOD; si algo cambió entre la prueba y el spawn, DynDOLOD no
  arranca.

En los dos modos, si el gate no puede demostrarlo, **DynDOLOD no se lanza** y
la etapa sale en rojo antes de gastar los 30+ minutos. Acá el criterio NO es el
conservador del sensor de modlist: ahí una materialización parcial pasa en verde
porque el universo medido es heterogéneo y frenar un setup que funciona sería
peor. Este árbol, en cambio, salió entero de una sola corrida, así que "parcial"
no es ambiguo — o desplegaste esta salida o no. **Sky-Claw no materializa nada
por su cuenta:** no copia a `Data`, no edita el modlist y no lanza
`ModOrganizer.exe`. Sólo se niega a seguir sin evidencia.

### Cuando ese gate corta, la salida de TexGen te queda esperando

El corte por visibilidad no es un fallo de herramienta: TexGen corrió bien y su
mod quedó empaquetado y validado. Por eso la etapa **preserva**
`<mo2>/mods/TexGen Output` en vez de revertirlo, aunque el resto de la corrida sí
revierta. Es deliberado y acotado:

| Destino | Qué pasa tras un corte por visibilidad |
|---|---|
| `<mo2>/mods/TexGen Output` | **se conserva** con la salida de ESTA corrida — es lo que tenés que desplegar |
| `<raíz administrada>/textures` (staging crudo) | **revierte**, como siempre: que nazca vacío es la precondición de que su contenido sea el de la corrida |
| `<mo2>/mods/DynDOLOD Output` | revierte — DynDOLOD no llegó a correr |

La transacción queda **PENDIENTE**, no marcada como revertida, y el registro
nombra el directorio preservado: hay una mutación viva en disco y el journal lo
dice. `rolled_back` en el resultado es `False` por la misma razón.

El ciclo completo, entonces (backend standalone):

```text
TexGen corre  →  TexGen Output empaquetado  →  gate de visibilidad FALLA
                                                      ↓
                       resultado: success=False, needs_deployment=True,
                                  texgen_mod_path=<mo2>/mods/TexGen Output
                                                      ↓
                          MATERIALIZÁS ese árbol en el Data del juego
                                                      ↓
                    volvés a correr la etapa con TexGen DESACTIVADO
                                                      ↓
             se verifica el mod preservado contra el Data, byte a byte
                                                      ↓
                                DynDOLOD arranca
```

En el backend brokered el ciclo es análogo y la acción humana cambia: el primer
corte —`TexGen Output` existe pero todavía no está habilitado en el perfil—
llega con `needs_deployment=True` y `handoff_action="profile_enablement"`. Lo
que hacés es **habilitar `+TexGen Output` en MO2** (Sky-Claw jamás edita
`modlist.txt`) y reanudar con TexGen desactivado; el gate reevalúa perfil,
enablement, efectividad y la evidencia runtime USVFS antes de lanzar DynDOLOD.

Correr la continuación **sin** TexGen es lo correcto y no un atajo: la autoridad
es el artefacto que ya se generó y desplegaste (o habilitaste), no una
regeneración que podría producir bytes distintos. Esa continuación tiene su
propio gate — si el mod empaquetado existe, DynDOLOD no se lanza hasta que el
dominio del backend lo demuestre byte a byte; que el archivo *exista* con el
tamaño correcto no alcanza. Y si nunca empaquetaste un `TexGen Output` con
Sky-Claw, el uso de siempre —DynDOLOD solo, porque tus texturas ya están— sigue
funcionando igual.

## Qué tenés que hacer

**Materializá a disco el árbol de mods del perfil activo**, en el `Data` del
juego, antes de correr rituales. Cómo hacerlo depende de tu instalación de MO2 y
queda fuera de esta página: lo que Sky-Claw necesita es el resultado — que los
plugins y assets del perfil estén **físicamente** bajo el `Data` que las
herramientas van a abrir.

Lanzar Sky-Claw *desde* MO2 para heredar la USVFS **no es un modo soportado**.

### Cómo verificarlo vos mismo

Abrí `<juego>/Data` con el explorador de archivos, fuera de MO2, y buscá un
plugin que sepas que viene de un mod (no de un DLC oficial). Si no está, tus
herramientas tampoco lo van a ver.

Los `.esm` base y los archivos `cc*` de Creation Club **no sirven como prueba**:
viven en `Data` con o sin VFS.

## Dónde escribe cada herramienta

Corolario de la misma invariante: hay exactamente **dos** formas de que la salida
de una herramienta llegue al `overwrite` de MO2.

- **(a) Redirección USVFS** — el tool escribe en `Data/x` y la VFS lo desvía a
  `overwrite/x`. **Exige heredar la VFS**, así que para Sky-Claw es
  **inalcanzable**.
- **(b) Ruta explícita** — al tool se le pasa la ruta de salida en su línea de
  comandos y escribe ahí **físicamente**. Funciona standalone y es perfectamente
  válido.

De ahí la regla: **el `overwrite` solo se alcanza por (b), nunca por (a)**. Quien
no lleva su ruta de salida en el comando escribe relativo a su directorio de
trabajo.

<!-- markdownlint-disable MD013 -->

| Herramienta | Dónde aterriza su salida |
|---|---|
| **Wrye Bash** | `<juego>/Data/Bashed Patch, 0.esp` — sin ruta en el comando, `cwd` = juego |
| **Pandora** | `<juego resuelto>/Pandora_Output` — ruta absoluta explícita mediante `--output` |
| **BodySlide** | `<juego>/<output_path>` — el `-o` es **relativo** al `cwd`, que es el juego |
| **Synthesis** | Ruta explícita (caso (b)): el `overwrite` de MO2 si existe, si no `<mo2>/mods/Synthesis Output` |
| **DynDOLOD / TexGen** | Subroots EXCLUSIVOS por herramienta bajo el `external_work_root` (`<external>/DynDOLOD/TexGen` y `<external>/DynDOLOD/DynDOLOD`) mediante `-o:`; DynDOLOD usa `DynDOLOD_Output` (o su root con `DynDOLOD.esp`) y TexGen usa `textures`; los mods empaquetados van a `<mo2>/mods/`. Cada subroot pertenece a una sola herramienta y la familia nunca se empaqueta entera: el born-empty + ownership dan la atribución física |
| **LOOT** | No produce artefacto nuevo: reordena el `plugins.txt` / `loadorder.txt` del perfil |
| **xEdit (QuickAutoClean)** | Reescribe el plugin **in-place**, sobre su propia ruta de entrada |

Para BodySlide, que el destino sea una lista o un parámetro es **ambigüedad de
la herramienta** (versión, configuración), no del modo de lanzamiento.

<!-- markdownlint-enable MD013 -->

La fuente única de estas rutas es `sky_claw/local/tools/output_targets.py`; esta
tabla es su versión legible, no una segunda definición.

### Consecuencia: qué se puede revertir y qué no

El destino de salida define qué se puede deshacer cuando un ritual falla:

- **DynDOLOD / TexGen** — los destinos son directorios propios, y se protegen con
  un move-aside que se restaura ante fallo: los dos mods empaquetados bajo
  `<mo2>/mods` y —desde PR-2— el **root completo de cada herramienta** bajo el
  `external_work_root` (`<external>/DynDOLOD/{TexGen,DynDOLOD}`). Ese root no se
  aparta para poder revertirlo: se aparta para que nazca **vacío** (born-empty),
  y así lo que quede adentro después sea exactamente lo que esta corrida generó.
  Sin eso, un archivo de una corrida anterior sobrevivía en el árbol y terminaba
  dentro del mod: el antiguo gate de frescura sólo probaba que *algo* cambió,
  nunca que TODO el árbol fuera de esta corrida, y PR-3 lo retiró al volverse
  redundante con el born-empty. La cobertura del move-aside sigue siendo
  **parcial** a propósito: el directorio del ejecutable (donde el binario escribe
  su log y su INI) y el temporal quedan afuera.
- **Wrye Bash** — el destino es **un archivo** con nombre canónico, así que se
  puede snapshotear antes de correr y restaurar si el run falla.
  *Estado: implementado en el PR #395; hasta que ese PR esté mergeado, tratá a
  Wrye Bash como el caso de abajo.*
- **Pandora** — escribe en el directorio dedicado
  `<juego resuelto>/Pandora_Output`. `DirectoryRollback` mueve aparte el árbol
  previo y lo restaura ante fallo; el reconciliador de arranque vigente opera
  sobre ese destino exacto bajo el lock `behavior-graphs`.
- **BodySlide** — escribe en directorios compartidos. **No hay rollback
  automático:** un fallo a mitad de camino deja la salida parcial en disco.
  Revertir automáticamente un directorio compartido sería más destructivo que
  el fallo que intenta reparar.

Si el proceso muere de forma dura (corte de luz, cierre forzado) durante un
ritual, mirá [Recuperación](recovery.md). Para Pandora, el reconciliador actual
reconoce los backups de su destino exacto. Sigue pendiente un smoke en un rig
real que confirme que Pandora respeta `--output` y determine la forma exacta de
sus artefactos; los tests no sustituyen esa evidencia.

## La única excepción: el broker VFS

Sky-Claw tiene **un** camino que sí corre bajo la USVFS: el broker
(`sky_claw/local/mo2/brokered_loot.py`), que despacha trabajos a un worker
lanzado dentro del entorno de MO2.

Su cobertura productiva es acotada y conviene no sobreestimarla: el worker acepta
exactamente **dos tipos de trabajo**, `health` (atestación) y `loot_sort`
(`sky_claw/local/mo2/vfs_worker.py`). Todo lo demás del pipeline corre standalone,
con las implicancias de arriba.

## Ver también

- [Recuperación](recovery.md) — qué hacer ante un startup parcial, una
  cancelación o un ritual fallido.
- [MO2/USVFS y subprocesos](../architecture/mo2_vfs_subprocesses.md) — la vista
  de arquitectura del mismo mecanismo.
- [Pipeline de Skyrim](../pipeline/skyrim_sop.md) — el orden de stages y las
  reglas por herramienta.
