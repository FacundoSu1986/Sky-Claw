# Perfil FOMOD y frontera segura de resultados de tools

## Objetivo

Corregir dos defectos del PR #454 sin ampliar su alcance:

1. las operaciones FOMOD deben evaluar dependencias y registrar el mod en el
   perfil MO2 configurado para la sesión, no siempre en `Default`;
2. ningún resultado de tool con instrucciones maliciosas puede volver al
   contexto del LLM, incluso si no contiene delimitadores especiales.

No se modifica `docs/pending_ooda_status.md`: el PR no cierra T-03, T-05 ni
T-31 y su descripción ya declara el alcance FOMOD parcial.

## Diseño del perfil MO2

La aplicación resolverá una sola vez el nombre del perfil de la sesión con
esta precedencia:

1. argumento CLI `--profile` no vacío;
2. variable de entorno `MO2_PROFILE` no vacía;
3. fallback compatible `Default`.

El nombre se validará con `assert_safe_component` antes de construir rutas o
pasarlo a operaciones MO2. El mismo valor se inyectará en:

- `MO2PluginStateProvider`, para evaluar `fileDependency`;
- `AsyncToolRegistry`, para que `install_mod_from_archive` llame a
  `MO2Controller.add_mod_to_modlist(..., profile=...)`;
- `LLMRouter`, mediante la ruta `profiles/<perfil>` usada por su contexto.

El perfil queda fijo durante la sesión. Cambiarlo externamente en la interfaz
de MO2 requiere reiniciar Sky-Claw o lanzarlo otra vez con otro `--profile`.
Esta limitación será explícita en la ayuda CLI; no se inventará un parser de
`ModOrganizer.ini` que el repositorio no posee.

## Frontera de confianza de resultados de tools

El router tendrá una única función para preparar cualquier resultado antes de
guardarlo o reenviarlo al modelo. El orden será:

1. inspeccionar el texto crudo con `TextInspector`;
2. si existe un hallazgo `CRITICAL` o `HIGH`, descartar todo el contenido y
   devolver un resultado de error estable que indique que se bloqueó contenido
   externo no confiable;
3. si no se bloquea, aplicar `sanitize_for_prompt` como hasta ahora.

La misma función se usará en el camino estructurado `tool_result` y en el modo
Hermes. Bloquear el resultado completo evita intentar distinguir instrucciones
de descripciones legítimas mediante un escape o una lista parcial de campos.
El resultado crudo bloqueado no se persistirá en el historial.

## Errores y compatibilidad

- Un perfil inválido abortará el arranque antes de acceder al filesystem.
- La ausencia de `--profile` y `MO2_PROFILE` conserva `Default`.
- Las firmas públicas existentes mantendrán defaults compatibles donde sea
  necesario para fixtures y consumidores antiguos.
- El gate de resultados es fail-closed solo para severidades altas; hallazgos
  medios o bajos continúan por el saneamiento existente.
- La aprobación HITL de la instalación permanece intacta.

## Verificación TDD

Los tests se escribirán primero y se observarán fallar por la conducta actual:

- precedencia CLI, entorno y fallback del perfil;
- wiring de `app_context` hacia proveedor, registry y ruta del router;
- instalación que registra el mod en un perfil alternativo;
- resultado benigno conservado y saneado;
- resultado con frase de inyección bloqueado en el camino estructurado;
- el mismo bloqueo en modo Hermes, anclando que ambos hermanos usan la
  frontera común.

Después se ejecutarán las suites FOMOD/router afectadas, ambos gates Ruff,
`mypy` sobre los módulos tipados pertinentes y la suite completa disponible
antes del commit final y el push.
