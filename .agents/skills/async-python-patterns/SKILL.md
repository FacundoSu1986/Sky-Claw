---
name: async-python-patterns
description: Dominar Python asyncio, programación concurrente y patrones async/await para aplicaciones de alto rendimiento. Usar al construir APIs async, sistemas concurrentes o aplicaciones I/O-bound que requieran operaciones no bloqueantes.
metadata:
  version: 1.1.0
  last_updated: 2026-04-23
  compatibility:
    - Python 3.11+
    - asyncio
---

# Patrones Async en Python

Guía completa para implementar aplicaciones Python asíncronas usando asyncio, patrones de programación concurrente y async/await para construir sistemas de alto rendimiento y no bloqueantes.

## Usar esta skill cuando

- Construyas APIs web async (FastAPI, aiohttp, Sanic).
- Implementes operaciones de I/O concurrentes (base de datos, archivos, red).
- Creas web scrapers con requests concurrentes.
- Desarrolles aplicaciones en tiempo real (servidores WebSocket, sistemas de chat).
- Proceses múltiples tareas independientes simultáneamente.
- Construyas microservicios con comunicación async.
- Optimices cargas de trabajo I/O-bound.
- Implementes tareas en segundo plano y colas async.

## No usar esta skill cuando

- La carga de trabajo sea CPU-bound con I/O mínimo.
- Un script síncrono simple sea suficiente.
- El entorno de ejecución no soporte asyncio / event loop.

## Instrucciones

- Clarificar características de la carga de trabajo (I/O vs CPU), objetivos y restricciones de runtime.
- Elegir patrones de concurrencia (tasks, gather, queues, pools) con reglas de cancelación.
- Agregar timeouts, backpressure y manejo de errores estructurado.
- Incluir guía de testing y debugging para rutinas async.
- Si se requieren ejemplos detallados, consultar `resources/implementation-playbook.md`.

Consulta `resources/implementation-playbook.md` para patrones detallados y ejemplos.

## Recursos

- `resources/implementation-playbook.md` para patrones detallados y ejemplos.
