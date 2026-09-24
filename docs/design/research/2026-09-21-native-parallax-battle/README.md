# Native Parallax Generator — Battle Mode (A vs B → Synthesis C)

> **Sesión:** 2026-09-21 (UTC) · Rama `arena/01a0c5fd-sky-claw`.
> **Objetivo:** diseñar (solo investigación y diseño, cero código productivo) un generador
> nativo de height/parallax maps para Skyrim SE/AE, **clean-room** respecto a ParallaxR.
> **Entregables:** dos arquitecturas independientes, crítica cruzada, síntesis C, roadmap.

## Archivos

| Archivo | Contenido |
|---|---|
| `10_ecosystem_facts.md` | Base de hechos verificados del ecosistema parallax 2026 (formatos, canales, PGPatcher, CS, ENB, TruePBR, normal maps, DDS, IA). Cita a la que se refieren los tres documentos. |
| `20_architect_a_determinista.md` | ARQUITECTO A — pipeline clásico/determinista (CV, Fourier, Poisson, sin IA). Secciones A–U. |
| `30_architect_b_hibrida.md` | ARQUITECTO B — híbrido determinista + IA opcional (MODE 0/1/2). Secciones A–U. |
| `40_critica_cruzada.md` | A critica B, B critica A. Cada cargo con evidencia o experimento. |
| `50_sintesis_c.md` | SYNTHESIS C — componentes que sobreviven, matriz de decisión, roadmap por PRs. |
| `CLEAN_ROOM.md` | Política clean-room propuesta (qué NO se acepta en el repo, qué sí). |

## Método de evidencia

Estados epistémicos por afirmación, consistentes con `2026-09-19-pre-lod-material-p0-evidence.md`:

- **[VERIFICADO]** — leído en fuente primaria viva durante esta sesión (con enlace en `10_ecosystem_facts.md`).
- **[INFERENCIA]** — derivado de otra evidencia verificada; razonamiento explícito, no observado.
- **[HIPÓTESIS]** — proposición falsable; tiene experimento asignado (EXP-001..EXP-012).
- **[NO VERIFICADO]** — sin fuente primaria consultada; se declara como desconocido.

Regla del brief: si una idea solo se justifica con "ParallaxR lo hace así" → descartada.
Ninguna decisión de diseño de los documentos 20/30/50 se apoya en detalles internos de
ParallaxR. El doc P0 del repo se cita únicamente para contratos de integración Sky-Claw
(versiones PGPatcher, artefactos de output), nunca para algoritmos.

## Lectura recomendada

1. `10_ecosystem_facts.md` (el "qué es parallax en 2026" ya no se discute dos veces).
2. `20_architect_a_determinista.md` y `30_architect_b_hibrida.md` (independientes entre sí).
3. `40_critica_cruzada.md`.
4. `50_sintesis_c.md` (incluye decisión y roadmap).
