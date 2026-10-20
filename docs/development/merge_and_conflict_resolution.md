# Integración de código: merge, rebase y resolución de conflictos

> **Audiencia:** desarrolladores y agentes que integran ramas en `main`.
>
> **Estado:** norma de proceso vigente. Los hechos de §2 están medidos, con fecha y SHA.
>
> **Fuentes canónicas:** la configuración de protección de `main` en GitHub (medida en §2),
> `.github/workflows/ci.yml`, [`AGENTS.md`](../../AGENTS.md) y
> [source_of_truth.md](../documentation/source_of_truth.md).
>
> **Última verificación:** 2026-10-20 sobre `origin/main` `97e57d7d`.

## 1. Objetivo y alcance

Este documento fija **cómo se integra una rama y cómo se resuelve un conflicto de git** en
Sky-Claw: qué estrategia de merge se usa en cada caso, cómo se clasifica un conflicto, qué
evidencia cierra cada clase, y qué se registra en el PR.

**Principio rector.** Un conflicto no está resuelto cuando el árbol compila: está resuelto
cuando podés nombrar, por cada hunk, qué invariante protegía cada lado y con qué evidencia
lo verificaste **en el árbol mergeado**. El árbol mergeado es el único que se prueba y el
único que llega a `main`; verificar cada lado por separado es verificar un árbol que nunca
va a existir.

**Fuera de alcance, y con nombre explícito:**

- **Conflictos de mods** — records de ESP que se pisan, assets que se sobrescriben. Eso es
  dominio del producto (`sky_claw/local/xedit/conflict_analyzer.py`,
  `sky_claw/app/orchestrator/asset_conflict_scan.py`) y no tiene nada que ver con lo de acá.
  En este documento, "conflicto" **siempre** significa conflicto de git.
- El ciclo completo de un PR (apertura, gates, revisión por bots, post-merge): ver
  [`AGENTS.md`](../../AGENTS.md) y [CONTRIBUTING.md](../../CONTRIBUTING.md) §5.
- Nombres de rama y estrategia de branching: [CONTRIBUTING.md](../../CONTRIBUTING.md) §5.1.

## 2. Hechos medidos

Sin esto, las reglas de §4 son opiniones. Cada fila se reproduce con el comando que la
acompaña.

| Hecho | Valor observado (2026-10-20) | Consecuencia para la integración |
|---|---|---|
| `allow_merge_commit` | `true` | el botón de merge puede crear un commit de merge y arrastrar historia roja a `main` |
| `allow_rebase_merge` | `true` | puede reescribir los SHAs que ya revisaron los bots |
| `allow_squash_merge` | `true` | es la única que la política admite para el PR de una rama |
| `required_status_checks.strict` | `false` | `main` puede avanzar y el PR mergearse igual: **la puesta al día no la exige GitHub, la tiene que exigir el proceso** |
| `required_approving_review_count` | `0` | el gate son los bots y el CI; no hay aprobación humana que ataje un conflicto mal resuelto |
| `delete_branch_on_merge` | `true` | al mergear desaparece la rama remota; el worktree local queda sobre un SHA sin ref |
| `core.autocrlf` | `true`, y `.gitattributes` fija **sólo** `*.py` | 164/164 `.md` están `i/lf w/crlf`: consistente **por configuración local**, no por contrato |

```bash
gh api repos/FacundoSu1986/Sky-Claw \
  --jq '{squash:.allow_squash_merge,merge:.allow_merge_commit,rebase:.allow_rebase_merge,delete:.delete_branch_on_merge}'
gh api repos/FacundoSu1986/Sky-Claw/branches/main/protection \
  --jq '{contexts:.required_status_checks.contexts,strict:.required_status_checks.strict,approvals:.required_pull_request_reviews.required_approving_review_count}'
git ls-files --eol '*.md' | awk '{print $1" "$2}' | sort | uniq -c   # 164 i/lf w/crlf
```

Dos de estos hechos son los que más pesan y no son obvios:

1. **`strict: false`.** GitHub no exige que el PR esté al día con `main`. El resultado es que
   el árbol que corre el CI (`merge(PR, main@cuando-corrió)`) y el árbol que se mergea
   (`merge(PR, main@ahora)`) pueden ser distintos, y nadie probó el segundo. La regla R4
   existe sólo por esto.
2. **Sin aprobación humana.** Un conflicto resuelto tomando un lado entero no lo ataja nadie
   salvo que quede escrito en el PR. De ahí R13.

### 2.1 Deuda conocida: los `.md` no tienen contrato de fin de línea

`.gitattributes` fija `*.py text eol=lf` y nada más. Para markdown, el fin de línea depende
de la configuración local de cada clon. Hoy el repo es consistente (`i/lf` en los 164), pero
eso lo sostiene `core.autocrlf=true` en las máquinas que lo editaron, no el repo.

**No está aplicado**: agregar `*.md text eol=lf` a `.gitattributes` cerraría la clase
entera. Queda como cambio atómico propio, con su verificación:

```bash
git ls-files --eol '*.md' | grep -v '^i/lf' | head    # debe salir vacío después del cambio
```

Hasta entonces, un diff de markdown que toca todas las líneas es `MECANICO` (§3), no una
edición.

## 3. Clases de conflicto

Toda resolución empieza por acá. La clase decide **quién tiene autoridad** y **qué evidencia
cierra** el conflicto; sin clase, no se toca el archivo (R5).

| Clase | Señal | Autoridad | Evidencia de cierre |
|---|---|---|---|
| `MECANICO` | el diff es de forma: fin de línea, reindentación, orden de imports, un artefacto generado | el generador, nunca la mano | `git diff -w --ignore-blank-lines` vacío contra `origin/main` |
| `SEMANTICO` | los dos lados cambiaron la misma lógica | ninguna por defecto: se decide hunk por hunk | un test que cubre el hunk, verde en el árbol mergeado |
| `DE_CONTRATO` | un lado cambia la firma, el tipo de retorno, la excepción o la semántica de entrada de una primitiva; el otro agrega o cambia un consumidor | el contrato | inventario de call sites del árbol mergeado + el consumidor nuevo ejerciendo la entrada degenerada |
| `NO_CLASIFICADO` | no encaja en ninguna de las tres | nadie | no se resuelve: se detiene y se escala (R14) |

### 3.1 El conflicto que git no reporta

La clase peligrosa no es la que deja marcadores. Es la que **mergea limpio** y deja el
defecto adentro. Tres mecanismos, los tres vistos en este repo:

1. **Contrato cambiado de un lado, consumidor nuevo del otro.** Son archivos distintos: git
   no dice nada. Es el caso de `spearman`, que pasó de tolerar `NaN` (devolviendo basura) a
   lanzar `ValueError`: un PR que cambiara el contrato y otro que agregara un consumidor
   mergean sin un solo marcador, y el consumidor nuevo hereda un contrato que nunca vio.
2. **El hermano que nadie tocó.** Un PR arregla el camino de la GUI y el otro arregla el del
   agente LLM; cada uno toca archivos distintos, mergean limpios, y el recurso queda con dos
   comportamientos. Es la clase de defecto dominante del repo, ejecutada por merge en vez de
   por descuido.
3. **Artefacto generado regenerado de un solo lado.** `uv.lock`, `requirements.lock`, `dist/`:
   el lado que no regeneró "gana" el merge y el artefacto queda inconsistente con su fuente.

Por eso R7: **un merge limpio no es evidencia de un merge correcto.** Cuando el otro lado
tocó una primitiva que este PR consume, el inventario de call sites se corre igual, haya
conflicto o no.

## 4. Reglas normativas

Cada regla declara cómo se verifica. Una regla sin verificación envejece como el resto del
corpus normativo del repo: se cumple mientras alguien se acuerde.

**Sincronización**

### R1 — La puesta al día se hace con `merge`, nunca con `rebase`

Rebase reescribe los SHAs que los bots ya revisaron y el PR pierde la trazabilidad de qué
árbol se revisó. Además el rebase interactivo está descartado en esta máquina.

**Verificación:** `git log --format='%P' -1 HEAD` muestra dos padres en el commit de
sincronización, y `git reflog` no tiene entradas de `rebase`.

### R2 — El merge de sincronización es `--no-ff` aunque git pudiera hacer fast-forward

Un fast-forward no deja rastro de que hubo integración: después no se puede saber contra qué
base se probó.

**Verificación:** `git log --merges origin/main..HEAD` no sale vacío.

### R3 — `main` recibe **squash**: el PR no aporta su historia roja

El ciclo TDD deja un commit `test(...)` deliberadamente rojo. La evidencia queda en el PR;
`main` no hereda un commit que rompe `git bisect`.

**Verificación:** `git log --format='%P' -1 origin/main` devuelve **un solo** padre. Dos
padres significan merge commit.

### R4 — Antes de mergear, `origin/main` es ancestro del head del PR

Compensa `strict: false`. Sin esta regla el merge ocurre sobre una base que nunca se probó
junta, y el verde del CI es histórico.

**Verificación:** `git merge-base --is-ancestor origin/main <head>` sale con exit 0.
Si sale 1, se sincroniza con R1 y se vuelve a correr el CI.

**Clasificación**

### R5 — Todo conflicto se clasifica antes de resolverse

Sin clase asignada no se toca el archivo. La clase es lo que decide si `--ours` es una
decisión o un accidente.

**Verificación:** la tabla de §6 tiene una clase por archivo conflictuado.

### R6 — Prohibido `--ours` / `--theirs` sobre un archivo entero, salvo `MECANICO`

Es "arreglar un hermano y no al otro" ejecutado con un comando: el invariante del lado
descartado desaparece sin que nadie lo nombre.

**Verificación:** un archivo con clase `SEMANTICO` o `DE_CONTRATO` no puede declarar
`lado autoritativo = ours|theirs` en la tabla de §6; tiene hunks, y los hunks se nombran.

### R7 — Un merge limpio no es evidencia de un merge correcto

Si el otro lado tocó una primitiva que este PR consume, o si este PR arregla un camino que
tiene hermano, el inventario se corre igual, haya conflicto o no (§3.1).

**Verificación:** `git diff --name-only $(git merge-base origin/main HEAD) origin/main`
muestra los archivos que trajo el otro lado; si alguno define o consume la primitiva tocada,
el inventario de R10 se corre antes de mergear.

**Resolución**

### R8 — `MECANICO` se regenera o se normaliza; no se edita a mano

Editar a mano un artefacto generado o "arreglar" un diff de fin de línea deja el archivo
fuera de sincronía con su fuente.

**Verificación:** `git diff -w --ignore-blank-lines origin/main -- <archivo>` vacío. Para
artefactos, el generador se corrió sobre el árbol mergeado y esa salida es la commiteada.

### R9 — `SEMANTICO`: cada hunk resuelto cita el test que lo cubre

Si el test no existe, se escribe **antes** de cerrar el conflicto. TDD también aplica a la
resolución: un hunk sin test es una hipótesis.

**Verificación:** el test citado corre verde en el árbol mergeado, no en cada lado.

### R10 — `DE_CONTRATO`: el inventario de call sites se hace sobre el árbol mergeado

Auditar cada lado por separado no ve al consumidor que llegó del otro lado, que es
exactamente el que va a romperse.

**Verificación:** `grep -rn "<primitiva>(" sky_claw/ tests/` corrido sobre el merge, y el
consumidor nuevo ejerciendo la entrada degenerada (no sólo la feliz).

### R11 — Los artefactos generados no se resuelven a mano

`uv.lock`, `requirements.lock` y `dist/` se toman del lado de `main` y se regeneran. Si el
generador no reproduce `main`, se detiene: significa que el otro lado cambió una fuente que
este lado no declaró.

**Verificación:** `git diff origin/main -- uv.lock requirements.lock` vacío, salvo los
cambios de dependencia declarados en `pyproject.toml` en este mismo PR.

### R12 — Un `uv.lock` que cambió sólo por `uv sync` no se commitea

`uv sync` reescribe el lock con dependencias que faltaban del lock committeado. No es parte
del cambio.

**Verificación:** `git diff --cached --name-only` no incluye `uv.lock` cuando
`pyproject.toml` no cambió.

**Registro y cierre**

### R13 — Todo merge con conflictos registra la tabla de §6 en el PR

Es el único control que queda cuando no hay aprobación humana (§2). Sin tabla, el merge no
está listo.

**Verificación:** la tabla está en el cuerpo del PR, con una fila por archivo conflictuado y
la evidencia de cada una.

### R14 — Un conflicto que no se puede clasificar detiene la sesión

Se documenta como `NO_CLASIFICADO` y se escala. No se resuelve "para seguir": un conflicto
resuelto sin entenderlo es el que después aparece en `main` sin dueño. Es la misma regla que
el gate `UNKNOWN != COMPATIBLE` de [ADR 0012](../adr/0012-frozen-runtime.md).

**Verificación:** el archivo queda con marcadores o sin tocar (`git status` lo muestra en
conflicto) y el PR queda en Draft con la fila `NO_CLASIFICADO` en la tabla.

### R15 — Si el conflicto toca una invariante con ancla, el ancla corre sobre el árbol mergeado

Las anclas del repo enumeran familias por introspección o AST. Un merge puede dejar el
código "correcto" y el ancla roja — o peor, el ancla verde sobre un árbol que ya no es el que
se mergea.

**Verificación:** el test ancla nombrado en la tabla de §6 corre verde desde el worktree del
árbol mergeado.

## 5. Procedimiento

### 5.1 Pre-flight sin mover refs

Antes de tocar el worktree, se calcula el merge sintético. No escribe nada y dice si hay
conflicto y cuáles.

```bash
git fetch origin --prune
BASE=$(git merge-base origin/main HEAD)
git merge-tree --write-tree --name-only HEAD origin/main    # exit 1 + lista si hay conflicto
```

Si hay conflicto y querés el árbol completo para correr tests sin ensuciar el worktree:

```bash
TREE=$(git merge-tree --write-tree HEAD origin/main)        # primera línea = tree OID
COMMIT=$(git commit-tree "$TREE" -p HEAD -p origin/main -m "merge sintetico")
git worktree add --detach /tmp/skyclaw-merge-preflight "$COMMIT"
# correr los tests acá; si el árbol sintético coincide con el merge real, la evidencia aplica
```

Si el `tree` del merge real coincide con el sintético, la evidencia local aplica al merge de
verdad. Si no coincide, la evidencia **no** aplica: hay que repetirla sobre el árbol real.

### 5.2 Resolver

1. Clasificar cada archivo (R5). Si alguno queda sin clase → R14, se corta.
2. `MECANICO`: regenerar o normalizar (R8). No abrir el editor.
3. `SEMANTICO`: resolver hunk por hunk, escribiendo el test que cubre cada uno (R9).
4. `DE_CONTRATO`: resolver el hunk y después auditar **todos** los call sites del árbol
   mergeado (R10), incluidos los que llegaron del otro lado.
5. Artefactos: `git checkout origin/main -- uv.lock requirements.lock` y regenerar (R11).

### 5.3 Verificar sobre el árbol mergeado

```bash
git diff --stat HEAD                    # qué quedó staged del merge
ruff check sky_claw/ scripts/ tests/
ruff format --check sky_claw/ scripts/ tests/
python -m pytest tests/ -q -k "<subsistema>"     # primero el área, después la suite
```

Un verde por lado no reemplaza a este paso. El árbol que se prueba acá es el que se mergea.

### 5.4 Cerrar

1. Completar la tabla de §6 en el cuerpo del PR (R13).
2. Gate de merge: `MERGEABLE=YES`, `BEHIND_MAIN=0` (R4), `UNRESOLVED_THREADS=0`,
   `FAILED_CHECKS=0` sobre los required checks.
3. `gh pr merge <N> --squash --match-head-commit <sha-validado>` (R3).
4. Verificar que fue squash: `git log --format='%P' -1 origin/main` → un padre.

## 6. Tabla de registro

Va en el cuerpo del PR, una fila por archivo conflictuado. Es lo que R13 exige y lo que R6
verifica.

| Archivo | Clase | Lado autoritativo | Evidencia |
|---|---|---|---|
| `sky_claw/local/native_parallax/research/metrics.py` | `DE_CONTRATO` | el contrato nuevo de `spearman` **y** el flag `finite` que `compute_all` ya exponía | `pytest tests/test_native_parallax_spearman.py -q` verde en el merge, con el inventario de `spearman(` auditado |
| `uv.lock` | `MECANICO` | `origin/main` + `uv sync --extra dev` | `git diff origin/main -- uv.lock` vacío |
| `docs/README.md` | `MECANICO` | ambos (líneas distintas, sin solapamiento) | `git diff -w` muestra sólo las dos líneas agregadas |

## 7. Fallos y recuperación

**`--ours` y `--theirs` están invertidos respecto de lo que uno espera.** En un **merge**,
`--ours` es tu rama (HEAD) y `--theirs` es lo que estás trayendo. En un **rebase** es al
revés: `--ours` es la base sobre la que rebaseás. "Elegí el mío" elige el ajeno. Es una razón
más para R1.

**Conflicto fantasma por fin de línea.** Síntoma: el archivo entero en conflicto y
`git diff -w` vacío. Causa: `core.autocrlf` distinto entre los dos lados. Acción: no
resolver a mano; es `MECANICO` (R8) y se normaliza.

**El merge quedó a medias.** `git merge --abort` restaura el estado previo. Si ya se
commiteó, `git reset --hard <sha-previo>` — **desde el worktree propio**, nunca desde el
principal ([`AGENTS.md`](../../AGENTS.md), "Una sesión, un worktree").

**La rama remota desapareció al mergear.** `delete_branch_on_merge: true` la borra; el
worktree local queda sobre un SHA sin ref. Se retira el worktree o se escribe la ref a mano
en esta máquina, donde las refs anidadas no se escriben solas.

**El conflicto toca `.github/workflows/**` o `pyproject.toml`.** La resolución la valida un
run de CI sobre el merge, no un razonamiento: un workflow que parsea bien no es un workflow
que corre.

## 8. Verificación

El estándar tiene ancla propia: `tests/test_merge_conflict_standard.py` congela el conjunto
de reglas por igualdad, exige que cada una declare su verificación, y resuelve los enlaces
relativos y las rutas citadas. Borrar una regla o dejarla sin verificación rompe el test.

```bash
python -m pytest tests/test_merge_conflict_standard.py -q
```

Ese test **no** verifica que las reglas se cumplan en un merge concreto — eso lo hace la
tabla de §6 en cada PR. Declarar lo que un test no cubre es parte del contrato de este repo.

## 9. Referencias

- [`AGENTS.md`](../../AGENTS.md) — invariantes del repo, worktree por sesión, la regla del hermano.
- [CONTRIBUTING.md](../../CONTRIBUTING.md) §5 — ramas, PR, revisión.
- [source_of_truth.md](../documentation/source_of_truth.md) — qué fuente manda ante drift.
- [standards.md](../documentation/standards.md) — metadatos y reglas de precisión documental.
- [ADR 0012](../adr/0012-frozen-runtime.md) — `UNKNOWN != COMPATIBLE`, origen de R14.
- [.github/PULL_REQUEST_TEMPLATE.md](../../.github/PULL_REQUEST_TEMPLATE.md) — checklist de apertura.
