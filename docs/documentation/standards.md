# Estándares de documentación

> **Audiencia:** autores y reviewers de documentación.
>
> **Estado:** norma documental vigente.
>
> **Fuente canónica:** este documento.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Metadatos mínimos

Toda página nueva debe declarar:

- audiencia;
- estado: `Implementado`, `Parcial`, `Aspiracional` o `Histórico`;
- fuentes canónicas;
- fecha y SHA de última verificación cuando contenga hechos runtime.

## Tipos de contenido

- **Tutorial:** conduce a un primer resultado seguro.
- **How-to:** resuelve una tarea concreta.
- **Referencia:** enumera interfaces verificadas, sin narrativa aspiracional.
- **Explicación:** conecta componentes y decisiones.

Una página puede enlazar otro tipo, pero no debe mezclar estado vigente con
planes sin etiquetarlos.

## Reglas de precisión

1. Usar rutas y símbolos; evitar números de línea como enlaces permanentes.
2. No copiar schemas, flags o excepciones sin contrastar el archivo canónico.
3. No llamar “probado” a código leído, tests mockeados o CI de packaging.
4. Marcar explícitamente smokes reales pendientes.
5. No inventar APIs para completar un tutorial.
6. Separar política vigente de una recomendación futura.
7. Mantener una sola fuente para reglas de alta deriva, como el DAG de modding.

## Estilo

- Español para documentación de usuario y desarrollo.
- Símbolos, nombres de archivo, comandos y claves se conservan literalmente.
- Frases directas, pasos reversibles y condiciones de parada visibles.
- Diagramas Mermaid pequeños, con componentes y flechas verificables.
- Ejemplos sin secretos, rutas personales ni outputs que no se hayan validado.

## Cambios documentales

Un PR documental debe pasar:

- `git diff --check`;
- revisión de enlaces relativos;
- comprobación de que los símbolos citados existen;
- comparación de referencias estáticas con el código;
- vista renderizada de tablas y Mermaid.

Los gates de Python pueden permanecer verdes sin detectar una falsedad
documental; la revisión de verdad es obligatoria y separada.
