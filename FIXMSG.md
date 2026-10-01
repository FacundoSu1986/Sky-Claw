fix(runtime-vault): recovery monotónico y límites de error de GP2-S4D

Corrige los defectos válidos que la revisión adversarial encontró sobre S4-D.
Dos eran P1.

P1 — recovery desde VERIFYING_NODE_SET
--------------------------------------
La cadena de fases usaba `if estado_durable is not FASE: entrar(FASE)` por
fase. Desde VERIFYING_NODE_SET eso pedía `enter(VERIFYING_RV2)`, una arista que
el FSM de §19.2 no admite: el resultado era INDETERMINATE con CERO gates
ejecutados, es decir un Golden sin verificar y sin rollback. El defecto era
invisible porque todos los tests arrancaban desde APPLYING, así que el tramo de
reanuación —que existe justamente para cuando el proceso anterior muere— nunca
se ejercitaba.

Ahora el avance es un recorrido por índice sobre el orden normativo declarado
una vez: sólo se entra a la fase N+1 cuando el estado durable es exactamente la
N, y eso hace la monotonía estructural en vez de memorizada. Nuevo archivo
`test_runtime_vault_s4d_resume_monotonico.py` enumera los SEIS estados de entrada
y comprueba la secuencia exacta de gates de cada uno: "llega a COMMITTED" no
distingue una reanudación correcta de una que re-ejecuta de más o se salta uno.

P1 — el RIG de "tres reinicios" no probaba reinicios
----------------------------------------------------
`_esperar_breadcrumb` miraba todo el historial del log, así que en el segundo y
tercer ciclo encontraba el breadcrumb del primer worker y mataba al proceso
nuevo antes de que llegara al punto. El test pasaba sin ejercer ni un crash real
en esos ciclos.

El arreglo no es filtrar por PID: en Windows `sys.executable` es el launcher del
venv que crea el intérprete real como proceso HIJO, así que el `pid` del
controller no es el `os.getpid()` del worker. Se usa un NONCE DE CORRIDA que el
controller inyecta con `--ciclo` y que el worker antepone a cada breadcrumb. Un
breadcrumb de una corrida anterior no puede satisfacer la espera de otra. El
test ahora exige PIDs y ciclos distintos por ciclo y comprueba el breadcrumb de
cada uno.

Lo mismo aplica al worker de semilla: recorre la ruta de fases desde el estado
durable real y entra SÓLO en las posteriores a la alcanzada, así que relanzarlo
sobre la misma operación es idempotente en vez de pedir aristas hacia atrás.

Límites de error
----------------
- **NodeSet**: `observar_node_set` capturaba sólo `NativeEvidenceError`, que NO
  es la clase base de `InventoryLinkError` (reparse: caso I) ni de
  `DuplicateFileIdError` (hardlink: caso M). Ambas se escapaban al caller en vez
  de volverse veredicto FAIL, y como el caller clasifica por tipo no caían en
  la rama de INDETERMINATE. Ahora los errores de dominio enumerados explícitamente
  producen veredicto; un gate que no puede observar es un gate que no pasa.
- **Backup writer**: `write_secured_file_create_once_at` levanta
  `TrustedNamespaceError` y sus subtipos directamente (sus `error_factory` por
  defecto son los del namespace), así que escapaban del contrato
  `GoldenBackupError`. El adaptador productivo los traduce encadenando la
  causa y conservando el destino en el mensaje.
- **Replay de COMMITTED**: `_normalizar_commit_ya_durable` capturaba sólo
  `GoldenLockIoError`; un `OSError` de `release()` escapaba al caller con un
  COMMITTED durable ya escrito, y el caller podía leer la excepción como "el
  commit se perdió". Ahora captura `(GoldenLockIoError, OSError)`: el commit no
  se deshace, lo pendiente es la normalización del lock, y el reporte lo dice.

Contrato de enteros del manifiesto
---------------------------------
El validador del backup exigía `> 0` mientras el plan autoritativo acepta `>= 0`
(`volume_serial_number` en `[0, 2**64-1]`, `root_file_id` en `[0, 2**128-1]`,
`files`/`bytes` no negativos). Un Golden sin archivos —`files == 0`— producía un
INDETERMINATE sobre evidencia válida. Se replica el contrato upstream, sin tocar
upstream. Negativos, booleanos y fuera-de-rango se siguen rechazando.

Trazabilidad e inteligencia de la documentación
----------------------------------------------
- `_evaluar` recibía el gate implícito y armaba `gate="observacion"`, con lo que
  `verdict_for("rv2")` devolvía `None` sobre un reporte que sí contenía el
  veredicto de RV-2. Ahora el nombre del gate es un parámetro y se VALIDA: un
  port que devuelva el veredicto del gate equivocado es un error de contrato,
  no algo que se acepta en silencio.
- El docstring afirmaba que el digest del backup quedaba escrito DENTRO del
  registro COMMITTED. No es así, y el esquema del journal tiene sus tres claves
  congeladas por un ancla heredado. Se corrigió la documentación en vez de
  ampliar el esquema: el binding se recupera del disco (ruta derivada + binding
  por `operation_id` y `authorized_plan_digest` + create-once), y por eso
  `_normalizar_commit_ya_durable` relee el backup antes de liberar el lock.
- `__all__` declaraba `FinalizationRollbackPort`, símbolo que no existía ni se
  importaba. Un `from ... import *` fallaba. Se quitó y se añadió un ancla que
  recorre TODO `__all__` con `getattr`, más otro sobre el árbol de excepciones
  del store de backup.
- `RollbackNotifier` renombrado el default a `_solo_aviso_de_rollback`, y
  `FinalizationForensicReport` expone `rollback_executed` SIEMPRE en `False`
  para que `ROLLBACK_REQUIRED` no se pueda leer como "ya restaurado".

Nada de esto amplía el alcance del slice: no toca S4-C, ni el motor de rollback,
ni el namespace de confianza, ni el esquema del journal.