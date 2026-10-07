# ADR 0013 — Clean-room del generador nativo de parallax: política vigente, cuarentena y registro de permisos

**Fecha:** 2026-10-07
**Estado:** Aceptada por el operador (instrucción en sesión, 2026-10-07); entra en vigor al mergear a `main`.
**Contexto de origen:** `origin/main` `97dcc7a` (merge de REVAL-1, PR #696).
**Relacionados:** issue #676 (preview productivo y comparación con ParallaxR),
[`CLEAN_ROOM.md`](../../CLEAN_ROOM.md), `docs/design/research/2026-09-21-native-parallax-battle/`,
[registro B1](../audits/2026-10-07_b1_permisos_r_suite.md).

## 1. Contexto

- Sky-Claw (MIT) quiere un generador **propio** de height/parallax maps. ParallaxR es una
  herramienta cerrada de permisos restrictivos (registro B1: sin redistribución; modificación y uso
  de assets con permiso; invocación directa de sus helpers sin autorización, B6-L abierto).
- La política clean-room existía solo como **propuesta** (NP-R0): ni `AGENTS.md` ni ningún test la
  referenciaban. La afirmación "ninguna decisión de diseño se apoya en ParallaxR" era una frase.
- Un barrido por contenido de todos los archivos trackeados encontró material derivado de scripts y
  de binarios de herramientas cerradas en el plan P0 (v3 §2.2 y §2.4) y fragmentos de scripts en la
  evidencia P0 (§6.2), ambos anteriores a la política. El resto del árbol, incluido
  `native_parallax/` y sus docs de diseño, está limpio.
- El issue #676 prevé comparar el generador contra ParallaxR como **evaluación**; una política que
  lo prohibiera chocaría con la hoja de ruta del propio operador.

## 2. Decisión

1. `CLEAN_ROOM.md` pasa a ser política **vigente** en la raíz del repo, con el alcance y las reglas
   que allí se listan; la ruta de la propuesta queda como puntero.
2. El material anterior que contiene contenido derivado se **conserva y se pone en cuarentena**: se
   señaliza en el sitio, no se lee para trabajar en `native_parallax/` y su perímetro no crece (test
   ancla enumerativo). No se reescribe la historia de git.
3. Se distingue **evaluación** de **destilación**: comparar a ciegas contra la salida de una
   herramienta cerrada es válido como evaluación (sin retroalimentar el diseño ni commitear sus
   salidas); usar esa salida como etiqueta de entrenamiento, objetivo de ajuste o fuente de umbrales
   está prohibido.
4. Los permisos de las herramientas cerradas se registran con hash en `docs/audits/` y un test los
   acopla al contrato `MATERIAL_PIPELINE`: la evidencia legal y el código no se levantan por separado.
5. Los agentes reciben la regla en `AGENTS.md` y en un puntero local en
   `sky_claw/local/native_parallax/`.

## 3. Alternativas evaluadas

- **Solo documentar, sin test.** Descartada: `AGENTS.md` exige que toda regla traiga con qué se
  verifica; sin ancla, la política envejece como las demás.
- **Redactar o borrar el material del P0.** Descartada: documenta contratos de integración que el
  pipeline necesita, la historia de git lo conserva igual y reescribirla requiere confirmación
  explícita del operador.
- **Clean-room de dos equipos.** No alcanzable: el P0 registra la inspección de los paquetes por el
  operador. Se adopta procedencia verificable en su lugar.
- **No hacer nada.** Descartada: el reclamo de independencia quedaba sin sostén verificable.

## 4. Consecuencias

- `tests/test_clean_room_invariant.py` falla si aparece contenido derivado fuera del perímetro, si la
  zona limpia recibe una marca, si se quita un aviso de cuarentena, si la política o sus referencias
  se borran, o si el registro B1 y `B6_L` divergen.
- Sumar un documento que nombre ejecutables internos o hashes de artefactos exige decidir si es un
  contrato de integración (entra al perímetro de identidad) o contenido derivado (no entra).
- La cuarentena no deshace la exposición previa: la acota y la vuelve auditable.
- Todo PR futuro sobre `native_parallax/` hereda estas reglas; el revisor automático corre solo en PRs.

## 5. Non-goals

- No es asesoramiento legal ni decide si la reimplementación independiente es admisible: eso es del
  operador.
- No decide B6-L ni cambia `MaterialStepId.PARALLAXR`, `BENDR` ni `VRAMR` (siguen `BLOCKED`).
- No toca el código de investigación ni los resultados congelados de M0–M5.
- No re-verifica los permisos contra Nexus (egress bloqueado el 2026-10-07): ver el nivel de evidencia
  del registro B1.
