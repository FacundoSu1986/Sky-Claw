# Endurecimiento posterior a la revisión de crash logging

## Contexto

La rama `codex/crash-logging-async-safe` ya implementa el pipeline centralizado
de logging mediante cola no bloqueante y listener dedicado. Una revisión
posterior mezcló limitaciones vigentes con observaciones correspondientes al
estado anterior de la rama.

Este cambio de seguimiento incorpora únicamente las observaciones todavía
válidas. No modifica el comportamiento del runtime de logging, la semántica de
cancelación, los contratos VFS ni los resultados de herramientas externas.

## Alcance

### Documentación de saturación y estado degradado

La documentación operativa declarará explícitamente que:

- la cola principal tiene capacidad para 8192 registros;
- `WARNING` e inferiores pueden perderse cuando la cola está llena;
- el buffer de emergencia conserva como máximo 256 registros `ERROR/CRITICAL`
  y, una vez lleno, también descarta los más antiguos;
- no existe escritura síncrona de último recurso desde el hilo asyncio;
- una futura recuperación del listener, health endpoint o métrica debe
  ejecutarse fuera del event loop.

No se añadirá un fallback síncrono porque contradiría el requisito async-safe.
Tampoco se implementarán en este cambio métricas nuevas ni un supervisor del
listener.

### Estado de release y firma

La documentación de despliegue y release reflejará el estado comprobado:

- existen releases y tags hasta `v0.2.4`;
- el workflow de release genera un ejecutable, un SBOM y una firma Cosign
  keyless en forma de bundle;
- Cosign no equivale a Authenticode: el workflow no incluye un paso de
  Authenticode ni `signtool`, pero esta tarea no inspeccionó la firma PE del
  artefacto histórico;
- los cambios de esta rama continúan en `[Unreleased]` hasta que se publique una
  release nueva;
- esta tarea no verificó criptográficamente el bundle ni hizo cold boot del
  artefacto concreto; identidad de publisher y SmartScreen permanecen sin
  verificación independiente.

### Frontera estricta de mypy

Los módulos siguientes pasarán a una sección `strict = true` e
`ignore_errors = false`:

- `sky_claw._logging_runtime`;
- `sky_claw.logging_config`;
- `sky_claw.__main__`.

Se corregirán exclusivamente los siete diagnósticos observados en la prueba
estricta inicial:

- tipado del import opcional de OpenTelemetry;
- cinco comentarios `type: ignore` que dejan de ser necesarios;
- anotación de la tupla de señales en `__main__`.

Las correcciones serán sólo de tipos y no alterarán decisiones funcionales.
`AppContext` y el resto de los overrides amplios quedan fuera de alcance.

## Verificación

La aceptación exige:

1. `mypy --strict` efectivo sobre los tres módulos objetivo.
2. `ruff check sky_claw/ tests/`.
3. `ruff format --check sky_claw/ tests/`.
4. Tests focales de logging, bootstrap, GUI, VFS, LOOT y xEdit.
5. Suite completa con un `--basetemp` externo.
6. `git diff --check`.

Los smokes con MO2/USVFS, SSEEdit, LOOT y el ejecutable PyInstaller real
seguirán declarados como no verificados si no se ejecutan con esas instalaciones.

## Entrega

Después de verificar, todos los cambios de la rama se commitearán, publicarán y
se propondrán mediante un pull request contra `main`. El PR distinguirá tests
automatizados, smokes locales y superficies no verificadas en runtime.
