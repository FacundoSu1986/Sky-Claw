# ADR 0015 — Oráculo de Effective Runtime de solo lectura (P5-A)

**Fecha:** 2026-10-24
**Estado:** Propuesta (PR Draft, sin fusión). Aceptación pendiente del operador.
**Contexto de origen:** slice P5-A del tracker #672, blocker
`P5_EFFECTIVE_RUNTIME_ORACLE` (ADR 0012 Q12/§34.5) y el brief "GOLDEN MASTER
P5-A". Base `70a14bac` (`origin/main`), rama `p5a-effective-runtime-oracle`.

## 1. Contexto

El ADR 0012 ya decidió **quién consume** la observación del Effective Runtime:
`journal.reconciliar_journal(observador_effective=...)` recibe una
`ObservacionEffective` (`generation_id`/`clone_id`/`effective_path`), y el
veredicto de reconciliación depende de que ese callback exista y sea honesto.

Lo que no existía era el **productor**. Cada llamador tenía que inventar el
callback, y el brief prohíbe explícitamente aceptar «una cadena de ruta declarada
por el llamador» como prueba. El censo de §29.4 encontró **seis superficies que
determinan la ruta** (2 persistidas, 4 derivadas) y **3 consumidores con cache en
memoria**, y la pregunta Q12 («¿cómo demuestra Sky-Claw qué ruta ejecutan
MO2/SKSE?») quedó abierta como gate de P5.

Además, la evidencia empírica disponible es una máquina real donde:

- MO2 2.5.2 (instancia `G:/Modding/MO2/SkyrimSE`) persiste
  `gamePath=@ByteArray(G:\\Modding\\Skyrim_Runtime_1.6.1170)`;
- Steam tiene la instalación administrada en `G:/SteamLibrary/steamapps/common/
  Skyrim Special Edition` (buildid `13189953`);
- Sky-Claw tiene `skyrim_path = "G:\SteamLibrary\steamapps\common\Skyrim Special
  Edition"` persistido en `~/.sky_claw/config.toml`.

Es decir: **las herramientas de Sky-Claw apuntan a un árbol distinto del que
ejecuta el juego.** Un oráculo que sólo leyera el config de Sky-Claw habría
declarado como runtime efectivo la instalación de Steam.

## 2. Decisiones

### 2.1 El oráculo es un productor local read-only; el bridge no se consulta

El plugin del bridge de MO2 sólo acepta `health`/`launch_worker`/`cancel` (ADR
0007) y no expone el game path. **No se añade ninguna operación al bridge**, ni
mutante ni de lectura. La observación se hace con dos vías que el sistema
operativo ya expone sin cooperación del proceso observado:

1. **filesystem**: `modorganizer.ini` de la instancia MO2 (persistida) y el
   TOML/entorno de Sky-Claw (configurada);
2. **tabla de procesos**: la imagen de un `SkyrimSE.exe`/`skse64_loader.exe`
   vivo es la única prueba de qué se está ejecutando *ahora*.

El paquete entero queda anclado como read-only **por AST**
(`tests/…p5a…py::test_el_paquete_no_contiene_operaciones_mutantes`): cualquier
`open(...)`, `write_text`, `mkdir`, `unlink`, `shutil.*`, etc. en
`sky_claw/local/frozen_runtime/effective_runtime/` rompe el ancla. Es el patrón
"escaneo de fuente del anti-patrón" que ya funciona en el repo.

### 2.2 `gamePath` se lee de `[General]`, no de `[Settings]`

El primer lector de este slice buscaba `gamePath` en `[Settings]` y devolvía
`ABSENT` sobre una instancia perfectamente configurada. **El defecto lo detectó
la sonda contra el rig real, no la suite**: los fixtures y el lector compartían
la misma suposición, así que la suite quedaba verde sobre un ini que MO2 nunca
escribe. Es la forma más pura del defecto "un test que hereda la premisa del
código no puede falsificarlo".

MO2 2.5.2 persiste `gamePath` en `[General]`, junto a `gameName`,
`selected_profile`, `game_edition` y `version`. `[Settings]` empieza recién en la
línea 89 del ini real. El lector:

- lee `[General]`;
- si `gamePath` aparece en **otra** sección, devuelve `ABSENT` con un `detail`
  que nombra la sección — un "no sé" diagnosticable en lugar de leer una clave
  homónima de un lugar donde MO2 no la escribe.

El valor va serializado como `@ByteArray` de Qt y se decodifica con la semántica
de `iniUnescapedByteArray`: `\` + letra de la tabla de escapes, `\x` consume
**hasta dos** dígitos hexadecimales, cualquier otra cosa es literal. La cota de
dos dígitos está congelada por el caso `\x1234` (sin cota, el acumulador pasa de
255 y `bytearray.append` explota).

### 2.3 Lo observado gana a lo persistido; la duda jamás se resuelve a favor del camino feliz

`assess_effective_runtime` resuelve la ruta efectiva en este orden:

1. **procesos vivos del juego** → ruta PROBADA por el SO (gana sobre todo);
2. **dos raíces de juego vivas** → `AMBIGUOUS_LIVE_GAME` ⇒ `UNKNOWN`;
3. **tabla de procesos ilegible** ⇒ `PROCESS_TABLE_UNREADABLE` ⇒ `UNKNOWN`. Un
   `AccessDenied` al leer la tabla **no** es "no hay juego corriendo";
4. **MO2 vivo arrancado antes de la última escritura del ini** ⇒
   `MO2_IN_MEMORY_MAY_DIVERGE` ⇒ `UNKNOWN`: MO2 puede retener en memoria un
   `gamePath` distinto del persistido;
5. **`gamePath` persistido** → ruta CONFIGURADA (no probada);
6. nada ⇒ `NO_EFFECTIVE_EVIDENCE` ⇒ `UNKNOWN`.

Ese orden implementa la distinción que el brief exige separar: ruta
**configurada/persistida**, ruta **retenida en memoria**, y ejecutable
**seleccionado** (que puede contradecir al `gamePath`: si
`[customExecutables] N\binary` apunta a otro árbol, el que manda es el binario).

### 2.4 La cadena de rechazos es decidible SIN el Desired

Sobre la ruta efectiva, en orden: reparse point → Managed Source → Generation →
divergencia de las superficies de Sky-Claw → Desired ausente → ruta inexistente
→ Desired distinto → identidad ilegible → compatibilidad.

Los primeros cinco rechazos son **decidibles sin autoridad durable del Desired**
(una junction no es un Clone; la Managed Source nunca es un Clone; una Generation
nunca es un Clone; las tools de Sky-Claw apuntando a otro árbol es un defecto
medible). Ponerlos después de "no hay Desired" haría que el oráculo devolviera
`UNKNOWN` —que no distingue "no sé" de "está mal"— ante un defecto que sí puede
demostrar.

### 2.5 La divergencia config↔efectivo es `MISMATCH`, no una nota al pie

Si `skyrim_path` persistido o `SKYRIM_PATH` del entorno apuntan a un árbol
distinto del runtime efectivo, el veredicto es `MISMATCH` con
`CONFIG_DIVERGES_FROM_EFFECTIVE`. Es la forma medible del patrón "dos
superficies, un recurso" de `AGENTS.md`: con las tools resolviendo al runtime
saliente, LOOT, xEdit, DynDOLOD y el grass escriben en el árbol equivocado
(§29.4 ya lo describía como "escrituras aterrizando en el runtime saliente").

### 2.6 Sin autoridad durable del Desired Clone, no hay `MATCH`

`desired_runtime_from_storage` sólo puede leer `active.json` v1
(`desired_active_generation`). **El store de `RuntimeCloneRecord` no existe** (el
modelo con `root_path`, `source_generation_id` y `runtime_setup_id` del ADR 0012
es diseño, no código), así que `clone_root` queda en `None` y el veredicto de
producción es `UNKNOWN` con `NO_DESIRED_CLONE`.

Esto es un **hallazgo de FASE D, no una carencia del oráculo**: fabricar el
Desired desde un parámetro del llamador sería aceptar una ruta declarada como
prueba — exactamente lo que el brief prohíbe. Es también la razón por la que el
oráculo se declara **PARTIAL**, no IMPLEMENTED.

### 2.7 `UNKNOWN` nunca autoriza, y es una propiedad del TIPO

`EffectiveRuntimeAssessment.authorizes_activation` es la única expresión del
paquete que devuelve `True`, y exige `MATCH` **y** `RUNTIME_COMPATIBILITY ==
COMPATIBLE` (`UNKNOWN != COMPATIBLE`, ADR 0012 §13). No hay un `if` en el
llamador que lo garantice — el defecto que este repo cerró cuatro veces.

### 2.8 El gate de compatibilidad reusa la autoridad existente

`assess_runtime_compatibility` delega el veredicto en
`scanner.find_skse_installation` y sólo lo envuelve para (a) distinguir
`INCOMPATIBLE` de `UNKNOWN` —que el original colapsa en `None`— y (b) explicar
el motivo con las versiones observadas. Con versión del ejecutable inafirmable,
el resultado es `UNKNOWN` **sin llegar a llamar** a `find_skse_installation`,
porque con `game_version=""` esa función degrada a "loader + algún DLL" y
devolvería `COMPATIBLE` sin haber verificado el build.

## 3. Alcance declarado (lo que este slice NO hace)

- No implementa el store de `RuntimeCloneRecord` ni el `RuntimeSetupManifest`.
- No implementa el **binding** (mover las 6 superficies + invalidar los 3 caches
  de §29.4). El oráculo **observa**; no escribe.
- No consulta ni extiende el bridge de MO2.
- No consume aprobaciones ni toca PROMOTION/ROLLBACK: `EMISION_PRODUCTIVA_
  HABILITADA = False`, `HITL_PRODUCTIVE = NOT_WIRED`, `PROMOTION_PRODUCTIVA =
  DISABLED`, `ROLLBACK_PRODUCTIVA = DISABLED` siguen intactos.
- No corre contra instalaciones reales: la sonda del rig lee y nada más; los
  tests construyen instancias MO2 y roots de juego sintéticos bajo `tmp_path`.

## 4. Veredicto medido en el rig real (2026-10-24)

Lectura read-only de la máquina de desarrollo:

```json
{
  "verdict": "mismatch",
  "authorizes_activation": false,
  "reasons": ["config_diverges_from_effective"],
  "effective_path": "g:\\modding\\skyrim_runtime_1.6.1170",
  "effective_source": "mo2_game_path",
  "compatibility": "unknown",
  "desired_clone_root": null
}
```

Interpretación: el juego que MO2 lanzaría corre desde un runtime externo
downgradeado (1.6.1170), **no** desde la Managed Source — y las herramientas de
Sky-Claw apuntan a la instalación de Steam. No hay Desired Clone registrado, así
que ni siquiera se evalúa igualdad contra un Clone. `UNKNOWN` como *clase de
veredicto* habría ocultado la divergencia; `MISMATCH` la nombra.

## 5. Consecuencias

- `reconciliar_journal` puede recibir un `observador_effective` real a partir de
  este slice; cablearlo queda para la slice siguiente.
- La próxima acción mínima para habilitar `MATCH` en producción es el store
  durable de `RuntimeCloneRecord` (con `root_path`, `source_generation_id`,
  `runtime_setup_id` y la admisión física de §29.3), que es además prerrequisito
  del `ApprovalScope` (campos `clone_evidence` y `runtime_setup_evidence` exigen
  digerir **contenido**, no rutas).
- Una simulación verde **no** demuestra que el juego arranca desde el Clone
  desde MO2: eso es P7 (rig real), y se separa explícitamente de esta evidencia.

## 6. Verificación

- Tests: `tests/test_frozen_runtime_p5a_effective_runtime_oracle.py` (61 casos),
  incluidos los diez escenarios de FASE C, el ancla de enum por igualdad
  literal, el ancla AST read-only, el ancla "sin fuente para la Managed Source",
  el diferencial de fidelidad Qt (§9) y el contrato de cableado (§11).
- Mutación: 12/12 mutantes muertos (`M1`–`M12`: byte array truncado, sección
  equivocada, cota hex, MATCH sin compatibilidad, reparse, Managed Source, clone
  equivocado, persistido sobre vivo, ini stale, divergencia tolerada, Desired
  fantasma, versión ausente).
- Regresiones (`-k "frozen_runtime or path_resolution or output_targets"`):
  1139 passed, 3 skipped, 7 failed. Seis de los siete fallan **idéntico** en
  `origin/main` (`df9bb79b`): son symlink/junction sin privilegio en esta
  máquina. El séptimo (`test_s04_archivo_eliminado_unstable`) pasa aislado en
  los dos worktrees y `test_frozen_runtime_p1.py` no tiene ninguna arista de
  importación hacia este paquete: interferencia de corrida pesada, no
  regresión. Cero regresiones.
## 7. Auditoría adversarial del HEAD (2026-10-24, misma sesión)

Cuatro intentos de falsificar el MATCH con evidencia incompleta. Los cuatro
confirmaron un defecto real del HEAD `06aad479`; los cuatro corregidos con
sonda RED por la razón exacta, fix mínimo, GREEN y mutante que reintroduce el
defecto y muere **por su propia sonda**:

- **B1 — tabla de procesos parcialmente ilegible con un juego observado.** Con
  `indeterminate=True` y un `SkyrimSE.exe` legible en el Clone A, el oráculo
  devolvía MATCH: una raíz observada no permite DESCARTAR otra cuyo pid no se
  pudo leer. Ahora: UNKNOWN con `OTHER_ROOT_NOT_RULED_OUT`. El test anterior de
  tabla ilegible usaba cero procesos; el nuevo usa uno.
- **B2 — instancias MO2 mixtas.** `all(started_at < mtime_ini)` sólo
  disparaba UNKNOWN si TODAS las instancias MO2 eran anteriores al ini; con
  una vieja y una nueva, la mezcla pasaba y el ini persistido se daba por
  vigente aunque la vieja pudiera retener otro gamePath. Ahora `any(...)`
  (y no se identifica de forma fiable qué instancia posee ESTE ini).
- **B3 — selección del ejecutable no probada.** Con `gamePath` → Clone A,
  Desired → A y un `skse64_loader.exe` registrado desde el Clone B, el oráculo
  devolvía MATCH presuponiendo la selección. Ahora: UNKNOWN con
  `EXECUTABLE_SELECTION_UNPROVEN`. Sólo cuentan los basenames que lanzan el
  juego (`SkyrimSE.exe`/`skse64_loader.exe`/`skse64.exe`): LOOT y xEdit son
  herramientas externas legítimas y no disparan nada. No es MISMATCH —no se
  inventa una selección— y no aplica cuando un proceso vivo ya probó la ruta.
- **B4 — `OSError` en la inspección de reparse.** El recorrido colapsaba el
  error de inspección en "no hay reparse" (`except OSError: return None`) y
  fabricaba una inspección exitosa sobre una duda: con todo lo demás apuntando
  al Desired, daba MATCH. Ahora el contrato es tri-estado (hay / no hay / no
  se pudo determinar) y el tercer caso produce UNKNOWN con
  `REPARSE_INSPECTION_FAILED`.

Hallazgo adicional de la corrección: el helper nuevo usaba `str.replace` y el
ancla AST read-only del paquete lo rechazó (colisiona con `Path.replace`) —
el ancla hizo su trabajo sobre código recién escrito; se eliminó la
transformación en vez de debilitar el ancla.

Verificación de la ronda: 44 tests verdes (7 nuevos + `test_4f` del hallazgo
Qodo), ruff/mypy limpios, 5/5 mutantes muertos por su propia sonda,
regresiones sin cambios respecto de `origin/main`. Ready for Review habilita
Codex/Qodo/CodeRabbit; sus hallazgos se adjudican aparte. Ready for Review
NO autoriza el merge.

## 8. Hallazgos del Revisor Adversarial Qodo (sobre `7754545a`)

5 hallazgos, adjudicados con evidencia en el PR: **1 CONFIRMED + FIXED** —
`assess_runtime_compatibility` indexaba `dlls[0]` sin guard en la rama donde
el campo `skse_dll` ya usaba ternario; con una identidad cuyo `game_version`
matchea un DLL sin build codificado (los dos filtros divergen: el scanner
excluye auxiliares por nombre, este módulo exige build por regex) era un
`IndexError` que tumbaba al oráculo. Fix `4a4eea63`: `dlls` vacío ⇒ UNKNOWN.
Inalcanzable con la identidad real del PE (numérica); la puerta pública
acepta identidad inyectada y el contrato del gate es NUNCA lanzar.
**4 FALSE_POSITIVE** con premisa falsada por medición o prueba conductual:
`except Exception` no captura KeyboardInterrupt (derivan de `BaseException`);
`ntpath.basename` sí separa `/` (`altsep`); `link_kind_or_raise` no propaga
`FileNotFoundError` (un componente inexistente corta como `"none"`, lo que
`test_5` afirma conductualmente); `AE` y `SE` comparten la misma tupla de
loaders en `_SKSE_LOADERS_BY_EDITION`, así que el AE hardcodeado es
conductualmente idéntico y está documentado.

## 9. Fidelidad del desescape `@ByteArray` contra Qt (gate final, 2026-10-24)

El decodificador se escribió "según lo que parece razonable" y quedó con una
suposición: **`\x` consume hasta dos dígitos hex**. No era una lectura de Qt.

**Medición.** Se descargó `qtbase` 5.15 (`src/corelib/io/qsettings.cpp`,
md5 `b2500ed4da48b951935ceee001568413`) y se leyó el autómata:

- `QSettingsPrivate::iniUnescapedStringList` (líneas 728-900), `StHexEscape`
  en 854: acumula dígitos hex **sucesivos, sin tope**, en un `char16_t`
  (`escapeVal <<= 4`), hasta el primer carácter que no sea hex.
- `StOctEscape` (873): lo mismo para `\0`-`\7`.
- `iniEscapedString` (617): el escritor emite `\x` + `QByteArray::number(ch,16)`
  —1 o 2 dígitos, sin cero a la izquierda— y activa `escapeNextIfDigit` para
  que el lector ávido no se coma un dígito hex que venga después. Escribe `\0`
  para el byte NUL.
- `stringToVariant` (477): `s.midRef(11, size-12).toLatin1()` — angosta el
  `QString` acumulado, sustituyendo por `?` todo lo que no entra en Latin-1.

**Experimento diferencial** (`.tmp_p5a_probe/diferencial_qt_hex.py`, puerto
fiel del autómata contra el módulo real, corpus exhaustivo de `\x` con 1, 2, 3
y 4 dígitos = 69.919 casos): **69.646 divergencias** antes del fix, en 6 clases.

| Caso | Qt | Antes | Clase |
|---|---|---|---|
| `\x0d9` | `D9` | `0D` + `'9'` | valor ≤ 0xFF mal decodificado |
| `\x1234` | `?` (pierde) | `12` + `"34"` | ruta inventada |
| `\0` | NUL | `'0'` | octal ignorado |
| `\101` | `A` | `101` | octal ignorado |
| `C:\z` | `C:` | `C:\z` | escape desconocido conservado |
| `C:\` | `C:` | `C:\` | barra final conservada |

**Fix.** `_desescapar_bytes` reproduce el autómata: hex y octal ávidos, wrap de
16 bits emulado (`& 0xFFFF`, observable en `\x10041` → `A`), escapes
desconocidos y barra final descartados. Cuando el acumulador pasa de `0xFF`,
Qt angostaría con pérdida: se lanza `QtByteArrayIrrecuperable` y el
`gamePath` queda **`UNREADABLE`**, nunca una ruta inventada.

**Post-fix:** divergencia **cero** sobre los mismos 69.919 casos, con 69.121
negativas legítimas (exactamente los casos donde Qt pierde). El
`modorganizer.ini` real de esta máquina sigue decodificando a
`G:\Modding\Skyrim_Runtime_1.6.1170`.

**Invariante de compatibilidad.** `test_ida_y_vuelta_de_los_256_bytes_que_escribe_qt`
implementa el escritor de Qt y exige ida y vuelta exacta para los 256 bytes.
Es la prueba de que hacer ávido al lector **no** rompe los ini reales: para
contenido escrito por Qt el tope viejo y el consumo ávido coinciden, porque el
escritor nunca emite tres dígitos y escapa el dígito que siga a un escape.

## 10. La carpeta del lanzador, no su descendencia (gate final)

`_lanzadores_del_juego_fuera_de` usaba `_es_igual_o_hijo`, que acepta cualquier
descendiente. Dos síntomas del mismo defecto:

- **Demasiado permisivo:** un `skse64_loader.exe` registrado en
  `<efectiva>/tools` contaba como "mismo árbol" y habilitaba MATCH. Pero SKSE
  resuelve el juego relativo a SU directorio: si MO2 lanza ese loader, el
  runtime ejecutado es `tools`. "Dentro de" no es "en la raíz" ⇒ ahora UNKNOWN
  con `EXECUTABLE_SELECTION_UNPROVEN`.
- **Demasiado estricto:** un binario sin componente de directorio
  (`skse64_loader.exe` a secas, la forma en que MO2 registra un ejecutable del
  propio juego) se normalizaba con `abspath` contra el CWD del proceso —que no
  es la instancia— y toda instancia normal caía en UNKNOWN por un artefacto.
  Ahora "sin carpeta" significa "en la raíz".

Un binario relativo **con** directorio no se puede resolver sin la instancia:
no prueba nada y cuenta como fuera (falla cerrado).

El caso del **proceso vivo** en subdirectorio (`raiz_de_la_imagen` sube un
nivel) sigue fuera de alcance declarado, pero ahora está probado como rechazo
seguro y no sólo documentado: la raíz reportada es el subdirectorio, que no es
el Desired ⇒ MISMATCH y `authorizes_activation is False`
(`test_o8_loader_vivo_en_subdirectorio_no_puede_dar_match`).

## 11. Deuda de cableado de P5 (declarada, no disimulada)

Censo verificado en el gate final: `assess_effective_runtime` **no tiene
llamadores productivos**. El consumidor existe
(`journal.reconciliar_journal(observador_effective=...)`) pero nadie lo cablea
todavía, y `desired_runtime_from_storage` devuelve `clone_root=None` por falta
del store de `RuntimeCloneRecord`. En producción, por lo tanto, el veredicto es
`UNKNOWN` por construcción.

El riesgo no es la ausencia —es deuda declarada para P5-B— sino el llamador
**futuro** que omita `config_skyrim_path`: apagaría
`CONFIG_DIVERGES_FROM_EFFECTIVE` sin que nada lo delate, y el split-brain
medido en este rig (`skyrim_path` → Steam, `gamePath` → runtime independiente)
quedaría invisible con un veredicto potencialmente MATCH.

Dos barreras, ninguna ficticia:

1. **Contrato por AST** (`test_o6_ningun_llamador_productivo_omite_config_skyrim_path`):
   toda llamada a `assess_effective_runtime` bajo `sky_claw/` o `scripts/` debe
   pasar `config_skyrim_path`. El predicado se prueba sobre fuente sintética
   (porque hoy la lista de llamadas está vacía y un bucle sobre cero casos no
   verifica nada) y se falsificó plantando un llamador que lo omite: el test
   falla nombrando archivo y línea. También se ancla que el parámetro siga
   existiendo y sea keyword-only.
2. **La omisión se declara.** `ABSENT` a secas no distinguía "medí y no hay" de
   "nunca pregunté"; la observación `CONFIG_SKYRIM_PATH` ahora lleva un
   `detail` explícito cuando la superficie no se suministró.

La misma vara para el resto de la superficie: `decode_qt_scalar` (lectura de
`[Settings] base_directory`, que es texto plano y no debe decodificarse) se
exporta y se prueba pero **tampoco tiene consumidor productivo** en este
slice. Se declara acá en vez de presentarse como cableada; su consumidor
natural es el mismo P5-B que el de `config_skyrim_path`.


## 12. P5-B: Autoridad durable de RuntimeCloneRecord y resolución auténtica de Desired Runtime

**Fecha:** 2026-10-25
**Estado:** Implementado (P5-B, Tracker #672).

### 12.1 Ubicación durable y schema versionado

1. **Ubicación canónica:** Cada registro de clone reside de forma durable en
   `<storage_root>/state/clones/<clone_id>.json`. No se crea automáticamente
   ninguna carpeta raíz `clones/` en el storage root: las rutas físicas de
   ejecución de los clones son externas (gestionadas por MO2 o por el operador) y
   se validan estrictamente mediante admisión.
2. **Modelo de datos:** `RuntimeCloneRecord` incluye `schema_version` (v1),
   `clone_id`, `source_generation_id`, `lifecycle` (`RuntimeCloneLifecycle`),
   `intended_runtime_setup_id`, `verified_runtime_setup_id`, `root_path`,
   `admitted_role="runtime_clone"` y `created_at_ns`.
3. **Persistencia atómica y no-clobber:** La escritura utiliza `write_json_atomic`
   bajo el lock de exclusión cross-process (`participant_lock` sobre `FrozenRuntimeRoot`),
   impidiendo colisiones y corrupción ante abortos o carreras. Un registro preexistente
   con distinta identidad o procedencia se rechaza con `RuntimeCloneCollisionError`.
4. **active.json v2:** Se extiende el schema de `state/active.json` a versión 2
   incorporando el par canónico `desired_generation_id` y `desired_clone_id`.

### 12.2 Admisión física e invariantes de seguridad (`admitir_clone_root_path`)

- **Ruta absoluta:** Requiere rutas normalizadas y absolutas.
- **Sin reparse points:** Rechaza terminantemente enlaces simbólicos, junctions y
  reparse points NTFS en la raíz del clone.
- **Aislamiento de Managed Source:** Rechaza cualquier ruta contenida dentro de
  un árbol administrado por Steam (`steamapps/common`).
- **Aislamiento de Storage Root:** Prohíbe que el clone se ubique dentro de
  `versions/` (SFR-19: una Generation nunca es runtime ejecutable), `candidates/`
  o `state/`.

### 12.3 FSM de Lifecycle y verificación de setups

- Máquina de estados estricta: `CREATED` → `PROVISIONING` → `PROVISIONED` → `INVALID`.
- Invariante de setup: La transición o registro en `PROVISIONED` exige de forma
  invariable que `verified_runtime_setup_id` no sea nulo y coincida con
  `intended_runtime_setup_id`.

### 12.4 Integración auténtica con el Oráculo

`desired_runtime_from_storage(root)` resuelve de forma auténtica el `DesiredRuntime`:
- Lee `state/active.json`. Si `desired_clone_id` está ausente o corrupto, devuelve
  `clone_root=None`.
- Si `desired_clone_id` está presente, carga `state/clones/<clone_id>.json`.
- Valida que `source_generation_id` coincida con la Generation deseada, que el rol sea
  `runtime_clone`, que el ciclo de vida sea exactamente `PROVISIONED`, y que
  `admitir_clone_root_path` apruebe la raíz física.
- Si cualquier verificación falla, opera en modo **fail-closed** (`clone_root=None`),
  garantizando que el oráculo mantenga el veredicto `UNKNOWN` (`NO_DESIRED_CLONE`) y
  deniegue la activación.
- Si todas las pruebas son satisfactorias y coinciden con la evidencia observada del
  juego y de MO2, el oráculo puede emitir un veredicto `MATCH` con autoridad auténtica.

### 12.5 Desacoplamiento del consumidor del Journal

El callback `observador_effective` de `journal.reconciliar_journal` requiere un
adaptador específico hacia `ObservacionEffective` (`generation_id`, `clone_id`,
`effective_path`). Dado que `reconciliar_journal` pertenece a la maquinaria de
transición/promoción de candidatos (P4/P6) y no debe activar operaciones mutantes
sin una auditoría completa de su contrato, dicho cableado se mantiene diferido
y documentado para el slice de integración correspondiente.

### 12.6 Hallazgos de la Auditoría Adversarial y Blindaje P5-B

Tras una revisión adversarial independiente sobre la autoridad durable, se
identificaron y corrigieron cuatro fronteras críticas:

1. **Frontera de autoridad del clon (Anti-falsificación):**
   - Ni `registrar_runtime_clone_record` ni `desired_runtime_from_storage`
     aceptan identificadores de Generation inexistentes; se valida la presencia
     auténtica de `state/generations/<generation_id>.json`.
   - Una mera declaración `lifecycle: PROVISIONED` en JSON no confiere autoridad
     por sí misma: `desired_runtime_from_storage` ejecuta `verificar_runtime_clone`
     de forma física antes de resolver `clone_root`. Ante cualquier discrepancia,
     opera en fail-closed (`clone_root=None`), previniendo un falso `MATCH`.

2. **Integridad de contenido en archivos críticos:**
   - La comprobación superficial de tamaño (`st_size`) era insuficiente para
     detectar reemplazos maliciosos o binarios corrompidos con idéntico byte-count.
   - `verificar_runtime_clone` realiza el cálculo del digest SHA-256 en bloques de
     64 KiB para todos los `critical_files`, contrastando contra `crit.digest`.

3. **Contención física estricta:**
   - `admitir_clone_root_path` prohíbe que el clone root coincida con el
     `storage_root`, sea su ancestro, o se solape en cualquier dirección con la
     raíz administrada (`managed_source_root`).

4. **Distinción inquebrantable entre corrupción y ausencia:**
   - Errores de schema o corrupción de JSON (`active.json` o registros de clone)
     propagan excepciones tipadas (`StateCorruptError`, `StateSchemaError`,
     `RuntimeCloneCorruptError`, `RuntimeCloneSchemaError`) y nunca se
     degradan a un estado ausente limpio (`NO_DESIRED_CLONE`).

5. **Vinculación estricta entre filename y clone_id:**
   - Se prohíbe que el payload JSON redefina la identidad fijada por el namespace
     durable. `_leer_record_de_ruta` valida obligatoriamente que
     `record.clone_id == ruta.stem`; ante divergencia, lanza `RuntimeCloneCorruptError`.

6. **Reparse points colgantes y coherencia del escritor:**
   - `cargar_runtime_clone_record` elimina el guard ingenuo con `Path.exists()`, el
     cual interpretaba symlinks o junctions colgantes como ausencia (`None`). La
     inspección delega en `link_kind_or_raise` fallando cerrado con
     `RuntimeCloneCorruptError` ante cualquier enlace roto o redirigido.
   - `registrar_runtime_clone_record` valida exhaustivamente el contrato tipado
     (`schema_version`, identificadores de setup sin traversal, `created_at_ns` no
     negativo y consistencia de serialización) antes de adquirir locks o escribir en
     disco, asegurando que el escritor jamás publique un registro que su propio
     lector rechazaría.
