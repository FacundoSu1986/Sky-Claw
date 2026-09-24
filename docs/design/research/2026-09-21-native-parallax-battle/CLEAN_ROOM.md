# CLEAN_ROOM.md — Política clean-room del Native Parallax Generator (propuesta)

> Estado: propuesta (NP-R0). Si el proyecto se aprueba, este archivo se promueve a la raíz
> del repo y se referencia desde AGENTS.md.

## Principio

Sky-Claw diseña su generador de height/parallax maps exclusivamente a partir de:
requisitos del formato final, matemáticas públicas, especificaciones gráficas (DirectX/DDS),
comportamiento documentado de Skyrim, documentación pública de Community Shaders / ENB /
PGPatcher / TruePBR, papers académicos, y experimentación propia.

## PROHIBIDO aceptar en el repo (código, issues, PRs, docs, conversaciones)

- Código fuente, scripts (BAT/PowerShell), EXE/DLL, o helpers de ParallaxR (o de cualquier
  herramienta cerrada de la categoría).
- Strings extraídas de binarios de terceros; decompilación; ingeniería inversa; capturas de
  implementación decompilada.
- Constantes, parámetros, presets, bases de exclusiones, tablas, configuraciones o
  secuencias internas copiadas de herramientas cerradas.
- Contribuciones derivadas de cualquiera de los materiales anteriores ("tainted
  contributions"): si un aporte cita conocimiento interno no público, se rechaza completo.
- Extraer strings de binarios de modelos de IA de terceros para inferir comportamiento.

## SÍ se permite estudiar y usar

- El **formato** que Skyrim/ENB/CS esperan (slots, canales, naming `_p`, env-mask alfa,
  BC4/BC3/BC7), documentado públicamente.
- Documentación pública: Community Shaders (GitHub/docs), PGPatcher wiki (GPL-3, pública),
  ENB docs/artículos, Nexus solo para comportamiento documentado, papers (Frankot-Chellappa,
  Poisson/Simchony, Agrawal 2006, Quéau survey, MatSynth, DA3/CHORD).
- Implementaciones open-source con licencia compatible (DirectXTex MIT, Compressonator MIT,
  nvtt MIT, bcdec, Pillow), invocadas o importadas según su licencia.
- Datos CC0/CC-BY (MatSynth, AmbientCG, PolyHaven) para benchmarks.

## Regla de decisión

Si una decisión de diseño solo puede justificarse con "ParallaxR lo hace así" → se descarta.
Toda decisión debe poder citar una fuente pública o un experimento propio.

## Trazabilidad

- Los docs de diseño citan fuente + fecha + estado epistémico (VERIFIED / SUPPORTED_BY_…
  / INFERRED / HYPOTHESIS / NO VERIFIED / REAL_RIG_REQUIRED, taxonomía del P0 del repo).
- Los artefactos de terceros nunca se commitean: solo hashes y citas cortas (convención ya
  establecida por el P0).
