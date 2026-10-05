# Texto preparado para #592 — finding 3 (a publicar/tildar cuando haya permiso de issues)

**Estado actual:** BLOQUEADO por permisos. El token de la GitHub App `arena-ai-coding-agent` tiene
`issues: read` (no write), por lo que tanto `PATCH /issues/592`, `POST /issues/592/comments` (REST) como
`addComment` (GraphQL) devuelven `403 Resource not accessible by integration` — incluso después de reconectar GitHub.

## Opción 1 — tildar las 3 casillas del finding 3 y pegar la nota (recomendada)

Sección a editar en el body de #592: **`## 3. El packaging mide link-aware y copia link-following — CONFIRMADO (P2)` → `### Aceptación`**.

Casillas a pasar de `- [ ]` a `- [x]`:

```
- [x] Test con junction/symlink real **dentro** del staging (usar `tests/_symlink_guard.crear_junction`) que hoy falle.
- [x] Escaneo fail-closed de la fuente con la familia link-aware existente, o copia link-aware propia.
- [x] Que el presupuesto ENOSPC y la copia midan/copien el mismo conjunto.
```

Y agregar debajo la nota (ya redactada):

```markdown
> **RESUELTO por PR #686** — merge commit `7684c92a4205a2d53aa47972be126282e093cecf` (PR HEAD `7374d609`).
> Qué lo cerró:
>
> - pre-scan fail-closed de descendientes (`exigir_arbol_copiable_sin_reparse` en `app/security/links.py`) **antes** de `rmtree`/`mkdir`/`copytree`;
> - rechazo de symlink, junction y reparse tag no clasificado: un enlace descendiente ya no llega a la copia;
> - `inventory == copyable` sobre árboles admitidos (el presupuesto link-aware y la copia ya operan sobre el mismo conjunto);
> - lifecycle del scan resistente a cancelación: cancel #1/#2 esperan scan terminal antes de propagar, y el worker mutante no arranca;
> - retry acotado de inspecciones transitorias (`_scandir_con_reintento` + `link_kind_and_identity_or_raise_with_retry`, locks AV/indexer) y tests directos del primitivo (`TestExigirArbolCopiableSinReparse`, `tests/test_links.py`).
>
> **Límite que sigue vigente:** el pre-scan **no se declara race-proof** frente a un actor externo que reemplace una entrada entre el scan y `copytree`; la copia con validación por entrada/handles queda como follow-up independiente.
> Este sub-hallazgo queda cerrado; **#592 permanece abierto** por los demás hallazgos (1, 2, 4–9; el finding 2 —borrado previo a ENOSPC— sigue abierto).
```

El body completo ya editado (sólo cambia el finding 3) está en `issue592-body-editado-finding3.md`:
si tenés permiso de issues, `gh issue edit 592 --body-file <ese archivo>` reproduce el cambio exacto.

## Opción 2 — comentario (alternativa, si no querés tocar el body)

Comentario corto:

```markdown
### Finding 3 — RESUELTO por PR #686 ✅

Merge commit `7684c92a4205a2d53aa47972be126282e093cecf` (PR HEAD `7374d609`).

- pre-scan fail-closed de descendientes (`exigir_arbol_copiable_sin_reparse`) antes de `rmtree`/`mkdir`/`copytree`;
- rechazo de symlink/junction/reparse no clasificado (un enlace descendiente ya no llega a la copia);
- `inventory == copyable` sobre árboles admitidos;
- lifecycle del scan resistente a cancelación + retry acotado de inspecciones transitorias + tests directos del primitivo.

**Límite vigente:** el pre-scan **no es race-proof** frente a un actor externo entre scan y `copytree` (follow-up).
**#592 permanece abierto** por los demás hallazgos (1, 2, 4–9).

Evidencia de tracking: PR #688 (`docs(dyndolod): close R2 bookkeeping after #686`) alinea
`docs/pending_ooda_status.md` y `runner-defects-plan.md` — R1 = FIXED/MERGED · R2 = FIXED/MERGED · R3 = OPEN/REPRODUCED.
```

## Opción 3 — habilitar el permiso y que lo haga el agente

Requiere que la App de Arena declare **Issues: Read and write** (y se re-apruebe la instalación), o bien
proveer un PAT con scope de issues. Con eso, el agente ejecuta el `PATCH` y el `POST` automáticamente.
Reconectar la misma App **no** alcanza: su permiso instalado sigue siendo `issues: read` (verificado).
