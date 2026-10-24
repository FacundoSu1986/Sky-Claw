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

- Tests: `tests/test_frozen_runtime_p5a_effective_runtime_oracle.py` (36 casos),
  incluidos los diez escenarios de FASE C, el ancla de enum por igualdad
  literal, el ancla AST read-only y el ancla "sin fuente para la Managed Source".
- Mutación: 12/12 mutantes muertos (`M1`–`M12`: byte array truncado, sección
  equivocada, cota hex, MATCH sin compatibilidad, reparse, Managed Source, clone
  equivocado, persistido sobre vivo, ini stale, divergencia tolerada, Desired
  fantasma, versión ausente).
- Regresiones: `frozen_runtime or path_resolution or output_targets` ⇒
  1114 passed, 3 skipped, 6 failed — **los mismos 6 en `origin/main`** (fallos de
  entorno por symlink/privilegio, incluido el flake conocido de
  `test_f06_inspect_applies_anti_reparse_admission_boundary`). Cero regresiones.
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
