# CLEAN_ROOM.md — Política clean-room del Native Parallax Generator

> **Estado:** VIGENTE desde 2026-10-07 (decisión del operador; [ADR 0013](docs/adr/0013-clean-room-native-parallax.md)).
> Antes era la propuesta NP-R0 de `docs/design/research/2026-09-21-native-parallax-battle/`.
>
> **Alcance:** toda ruta trackeada cuyo path contenga `native-parallax` o `native_parallax` (el paquete
> `sky_claw/local/native_parallax/`, sus tests y sus docs de diseño, investigación y validación) y cualquier
> productor futuro de height/parallax maps.
>
> **Audiencia:** operador, desarrolladores y agentes.

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
- Usar la **salida** de una herramienta cerrada como etiqueta de entrenamiento, ground truth,
  objetivo de ajuste o fuente de umbrales y constantes (destilación): ata la calidad propia a la
  ajena y hereda su criterio sin poder auditarlo.
- Entrenar o afinar pesos que se vayan a distribuir con texturas de terceros con derechos
  reservados (Skyrim, mods de Nexus) sin una revisión de derechos explícita.
- Pegar material en cuarentena (ver abajo) en prompts, issues o PRs de trabajo sobre el
  algoritmo, las heurísticas o las exclusiones.

## SÍ se permite estudiar y usar

- El **formato** que Skyrim/ENB/CS esperan (slots, canales, naming `_p`, env-mask alfa,
  BC4/BC3/BC7), documentado públicamente.
- Documentación pública: Community Shaders (GitHub/docs), PGPatcher wiki (GPL-3, pública),
  ENB docs/artículos, Nexus solo para comportamiento documentado, papers (Frankot-Chellappa,
  Poisson/Simchony, Agrawal 2006, Quéau survey, MatSynth, DA3/CHORD).
- Implementaciones open-source con licencia compatible (DirectXTex MIT, Compressonator MIT,
  nvtt MIT, bcdec, Pillow), invocadas o importadas según su licencia.
- Datos CC0/CC-BY (MatSynth, AmbientCG, PolyHaven) para benchmarks y entrenamiento propio
  (CC-BY exige atribución).
- **Comparar a ciegas** la salida de una herramienta cerrada con la nuestra, con el mismo input,
  como **evaluación** (es lo que prevé el issue #676), bajo tres condiciones: los permisos del
  autor lo toleran (registro B1); la comparación no retroalimenta el diseño (no se afinan
  umbrales ni constantes hasta igualar su salida); y sus salidas no se commitean (solo hashes y
  métricas).

## Regla de decisión

Si una decisión de diseño solo puede justificarse con "ParallaxR lo hace así" → se descarta.
Toda decisión debe poder citar una fuente pública o un experimento propio.

## Trazabilidad

- Los docs de diseño citan fuente + fecha + estado epistémico (VERIFIED / SUPPORTED_BY_…
  / INFERRED / HYPOTHESIS / NO VERIFIED / REAL_RIG_REQUIRED, taxonomía del P0 del repo).
- Los artefactos de terceros nunca se commitean: solo hashes y citas cortas (convención ya
  establecida por el P0).
- Cada heurística, umbral o regla de exclusión del generador cita su fuente pública o el
  experimento propio que la fijó.

## Perímetro de cuarentena

Material **anterior** a esta política que contiene contenido derivado de leer scripts o de
inspeccionar binarios de herramientas cerradas. Se conserva porque documenta contratos de
integración (orden de stages, markers, forma de invocación) que el pipeline necesita; **no es
fuente** de algoritmos, heurísticas, listas de exclusión, presets ni constantes del generador nativo.

| Sección | Qué contiene |
|---|---|
| [`docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md`](docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md) §2.2 (filas de ParallaxR/BENDr) y §2.4 | citas de scripts, strings de binarios (T2) y entradas de listas de exclusión |
| [`docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md`](docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md) §6.2 | fragmentos de scripts |

1. Quien trabaje en `native_parallax/` (humano o agente) **no lee** esas secciones. Los contratos de
   integración que necesite ya están expuestos sin ese contenido: `sky_claw/local/tools/parallaxr_assisted.py`,
   `sky_claw/local/tools/material_contract.py` y el registro de permisos B1.
2. El perímetro **no crece**: ningún archivo nuevo puede contener citas de scripts, strings de
   binarios ni entradas de listas de exclusión de esas herramientas; lo hace cumplir
   `tests/test_clean_room_invariant.py`.
3. El contenido en cuarentena no se copia, no se parafrasea ni se "reimplementa de memoria" en el
   código o en los docs del generador.

## Exposición previa (reconocida)

El P0 registra la inspección de paquetes de ParallaxR, BENDr y VRAMr por parte del operador (niveles
T1 y T2 del propio P0). Por eso un clean-room de dos equipos —uno que mira y otro que implementa— no
es alcanzable en este repo. El estándar vigente es **procedencia verificable**: cada decisión cita
fuente pública o experimento propio (ver "Trazabilidad"), el contenido derivado está acotado y
señalizado (ver "Perímetro de cuarentena") y nada de él entra al algoritmo, a las heurísticas ni a
las exclusiones.

## Si se detecta contaminación

1. **Parar**: no seguir construyendo sobre la decisión afectada.
2. **No propagar**: no copiar el material a otros archivos, issues ni PRs.
3. **Registrar**: nota fechada en el PR o issue (qué decisión, qué material, cómo se detectó).
4. **Re-derivar** la decisión desde una fuente pública o un experimento propio.
5. La historia de git **no se reescribe** sin confirmación explícita del operador.

## Permisos de las herramientas cerradas (B1)

Registro con hash: [`docs/audits/2026-10-07_b1_permisos_r_suite.md`](docs/audits/2026-10-07_b1_permisos_r_suite.md).
Política vigente hasta evidencia contraria: `MANUAL_ONLY`, `NO_VENDOR`, `NO_BUNDLE`,
`NO_REDISTRIBUTE`. La invocación directa de helpers (B6-L) sigue **abierta**: sin permiso explícito
del autor no se invocan. El registro describe qué se puede hacer con los archivos de esas
herramientas; no es asesoramiento legal ni decide la reimplementación independiente.

## Anclas

`tests/test_clean_room_invariant.py` recorre **todos** los archivos trackeados, sin descartar ninguno por
tamaño, por codificación ni por ser binario, y verifica:

- *el contenido en cuarentena no sale de su sección*: las marcas de contenido (citas de scripts,
  etiquetas de evidencia T2) solo valen dentro de las secciones de la tabla de arriba, y ninguna línea de
  esas secciones puede reaparecer en otro lugar (huellas normalizadas: re-cortarla, cambiar las
  mayúsculas o incrustarla en otro texto no la esconde, y un tramo de al menos 79 caracteres tampoco),
  así que un fragmento de script copiado sin etiquetas también rompe el test. Los contratos de
  integración (`PERIMETRO_DE_IDENTIDAD`) quedan exentos solo de este chequeo de tramos: las copias de
  línea completa siguen prohibidas también ahí;
- *la identidad no se propaga*: nombres de ejecutables internos y hashes de artefactos solo en contratos
  de integración y evidencia;
- *la zona limpia no tiene ni una marca*: toda ruta que nombre el generador nativo (por patrón, no por
  lista), con sus binarios congelados por enumeración y sin ejecutables en el repo (por cabecera, no solo
  por extensión);
- *las secciones en cuarentena están señalizadas* con el aviso `CUARENTENA CLEAN-ROOM`;
- *la política no se borra en silencio*: encabezados y cláusulas congelados por frase; `AGENTS.md` y el
  puntero local la referencian;
- *la evidencia legal y el contrato coinciden*: el registro B1 y `B6_L` de `MATERIAL_PIPELINE`, con
  vocabulario cerrado de estados (solo `AUTORIZADA` levanta el bloqueo).

**Límite declarado.** El ancla no puede detectar contenido derivado que no esté en este repo: un script
reescrito con otras palabras, salidas de la herramienta convertidas a otro formato y copias parciales de
menos de 79 caracteres (según dónde caigan) pasan. Eso depende de la revisión humana y del procedimiento
de «Si se detecta contaminación». Las cláusulas se congelan por frase, no por significado.
