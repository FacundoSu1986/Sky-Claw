# Bootstrap OODA para agentes

> **Audiencia:** agentes autónomos bajo supervisión.
>
> **Estado:** procedimiento vigente.
>
> **Fuentes canónicas:** `AGENTS.md`,
> `docs/documentation/source_of_truth.md` y Git.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Observar

1. Resolver el repositorio y la rama activos.
2. Leer el `AGENTS.md` raíz y cada `AGENTS.md` aplicable al archivo.
3. Inspeccionar status, remotos, SHA, base y alcance del diff.
4. Localizar entrypoint, caller, contrato y tests antes de concluir.
5. Separar código vigente de ADR, auditorías, backlog e históricos.

## Orientar

- Definir si la tarea es lectura, documentación, cambio o validación.
- Identificar fronteras: async, DB, filesystem, red, HITL y subprocess.
- Resolver la fuente canónica con
  [source of truth](../documentation/source_of_truth.md).
- Declarar hechos no verificados, especialmente rig real y releases.

## Decidir

Elegir el cambio mínimo que satisface el alcance. No crear APIs o políticas
para completar una explicación. Una documentación solicitada no autoriza
cambios de lógica.

## Actuar y verificar

- Preservar cambios del usuario.
- Aplicar convenciones del subárbol.
- Ejecutar verificaciones proporcionales.
- Comparar el diff final con el alcance autorizado.
- Reportar por separado lectura, ejecución y pendiente.

No enlazar instrucciones por números de línea permanentes: usar rutas y
símbolos.
