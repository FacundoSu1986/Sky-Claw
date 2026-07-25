# Glosario

> **Audiencia:** todos los lectores.
>
> **Estado:** referencia de terminología, no de contratos runtime.
>
> **Fuentes canónicas:** código, ADR y documentación enlazada por término.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

| Término | Significado en Sky-Claw |
|---|---|
| Agente LLM | `LLMRouter` y su loop de herramientas; no implica autonomía irrestricta. |
| Caja negra de vuelo | Manifests, journal, preview, aprobación e informe para explicar mutaciones. |
| CoreEventBus | Bus asíncrono del core/orquestador con lifecycle y DLQ. |
| EventBus de GUI | Adaptador que entrega eventos al loop de NiceGUI. |
| HITL | Decisión humana explícita en una frontera concreta; no está presente en toda ruta del agente. |
| Lock-only | Política de la ruta LLM: serializa mutaciones sin un gate HITL propio. |
| MO2 | Mod Organizer 2. |
| Overwrite | Área compartida de MO2 para outputs; en ciertos contratos VFS se representa como nombre de mod validado. |
| Perfil | Árbol `profiles/<nombre>` con archivos como `plugins.txt` y `modlist.txt`. |
| Preview | Ejecución reversible que produce evidencia antes de aprobar una mutación. |
| Ritual | Operación de alto nivel del orquestador, normalmente compuesta por service, strategy y middleware. |
| Runner | Wrapper de bajo nivel de un ejecutable externo. |
| Service | Lógica de dominio que coordina runner, validación, locks y resultado. |
| Strategy | Unidad registrada en `OrchestrationToolDispatcher`. |
| ToolResult | Vista común normalizada de un resultado crudo de tool. |
| USVFS | Virtual filesystem de MO2 usado mediante broker/plugin/worker. |
| VFS canary | Archivo elegible usado para probar que el worker ve el perfil virtual correcto. |
