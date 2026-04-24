---
name: python-patterns
description: Referencia de patrones de diseño en Python idiomático (GoF + Pythonic). Usar para refactorizar código, resolver problemas arquitectónicos complejos, o diseñar nuevos módulos siguiendo principios SOLID. No usar para scripts simples que no requieran abstracción arquitectónica.
---

# Python Patterns (Referencia de Arquitectura)

Referencia de patrones de diseño clásicos en versión idiomática de Python, basada en el repositorio local `e:\Pruba antigravity\python-patterns`.

## Cuándo Usar

- Al refactorizar código que necesite abstracción (Factory, Strategy, Observer).
- Frente a problemas de creación dinámica de objetos → consultar `creational/`.
- Para notificación de eventos → consultar `behavioral/observer.py`.
- Al encapsular algoritmos intercambiables → consultar `behavioral/strategy.py`.
- Cuando se necesite validar que el código sigue principios SOLID.

## Cuándo NO Usar

- Para scripts simples de un solo uso.
- Cuando la complejidad del patrón superaría la del problema.
- Para prototipado rápido donde la velocidad es prioritaria sobre la arquitectura.

## Instrucciones

### 1. Consultar Patrones
Al enfrentar un problema arquitectónico, consultar ejemplos dentro de:
```
e:\Pruba antigravity\python-patterns\patterns\
├── behavioral/    # Strategy, Observer, Command, State, etc.
├── creational/    # Factory, Singleton, Builder, Prototype, etc.
└── structural/    # Adapter, Decorator, Facade, Proxy, etc.
```

### 2. Principios de Implementación
- Todo código refactorizado debe seguir **principios SOLID**.
- Usar **anotaciones de tipos estáticos** (typing) en todas las funciones.
- Preferir **composición sobre herencia**.
- Aplicar **diseño limpio** (Clean Code) junto con los patrones.

### 3. Patrones Frecuentes en Sky-Claw

| Problema | Patrón | Archivo de Referencia |
|----------|--------|----------------------|
| Crear instancias de diferentes LLMs | Factory | `creational/factory.py` |
| Notificar cambios de estado del daemon | Observer | `behavioral/observer.py` |
| Intercambiar estrategias de scraping | Strategy | `behavioral/strategy.py` |
| Encadenar validaciones AST | Chain of Responsibility | `behavioral/chain_of_responsibility.py` |
| Gestionar estado de sesiones | State | `behavioral/state.py` |
