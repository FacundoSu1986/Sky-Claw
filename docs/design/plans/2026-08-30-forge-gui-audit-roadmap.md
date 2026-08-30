# Roadmap post-auditoría GUI — Forja del Dovahkiin

**Fecha de baseline:** 2026-08-30  
**Alcance:** shell Forge / GUI NiceGUI.  
**Propósito:** conservar los pendientes reales de la auditoría visual sin mezclar defectos comprobados, decisiones de limpieza y propuestas artísticas.

> Este documento es un plan de trabajo. Antes de implementar cada ítem se debe
> revalidar el árbol actual y leer `AGENTS.md` y el `AGENTS.md` aplicable al
> subárbol. Las propiedades que cubran familias de superficies deben anclarse
> mediante enumeración/introspección, no mediante muestras manuales.

## 1. Baseline: PR #522

PR: `#522` — `fix(gui): quick wins visuales del tema Forja del Dovahkiin`  
HEAD verificado: `1701e94179d2ee38d7536b38058e9cfd120f9d23`  
Estado al registrar este plan: **READY FOR SQUASH MERGE**, todavía no afirmado como integrado en `main`.

El PR cubre los cinco quick wins de la auditoría:

1. scrollbar temática `.sc-scroll`, incluyendo fallback estándar y WebKit;
2. Cinzel hasta peso 900 en las dos caras declaradas;
3. foco visible por teclado y política dirigida de `prefers-reduced-motion`;
4. contraste de textos/rituales sin atenuar globalmente la tarjeta;
5. sustitución de iconos emoji del shell por SVG diegéticos y contrato de glifos.

Cierre de revisión verificado sobre ese HEAD:

- CI principal y los dos workflows Qodo: `success`;
- siete review threads: resueltos;
- PR: mergeable/clean;
- `#523` permanece fuera de alcance.

### Sincronización OODA después del merge

No editar `docs/pending_ooda_status.md` como si #522 ya estuviera en `main` antes del merge.
Después del squash merge, revalidar y actualizar como mínimo:

- **T-22**: pasar de `Abierto` a **Parcial** si el único cierre demostrado por #522 es `prefers-reduced-motion`; mantener pendiente cualquier trabajo de transiciones que siga existiendo.
- **T-24**: pasar de `Abierto` a **Parcial** si #522 demuestra foco visible pero no cierra exhaustivamente labels/formularios.

No declarar ninguno `Cerrado` sin enumerar la superficie completa correspondiente.

## 2. Prioridad inmediata: integridad antes que arte

### P0 — #523: ownership multi-tab de `STORE_KEY_RITUAL_FEEDBACK`

**Estado:** abierto; bug preexistente descubierto durante la revisión de #522.  
**Regla de alcance:** PR separado; no reabrir #522 para corregirlo.

Objetivo: impedir que una pestaña pueda consumir/limpiar feedback perteneciente a otra. La solución debe tratar feedback, resultado ritual y acción de reanudación como estado con ownership coherente, con pruebas de dos pestañas y comportamiento fail-closed.

Criterio de cierre mínimo:

- ninguna pestaña puede cerrar/consumir feedback ajeno;
- dismiss/resume respetan el mismo owner;
- tests cubren explícitamente dos owners y enumeran todos los caminos que leen/limpian ese estado.

## 3. Deuda estructural verificada o candidata a verificación

### C2 — Centralizar clases de botón

**Estado:** pendiente estructural.  
**Intención:** extraer estilos repetidos de CTA/acciones del shell a clases semánticas como `.sc-btn-gold` y `.sc-btn-ghost` en `styles.css`.

La auditoría detectó repetición del patrón visual dorado en múltiples superficies (hero y distintos paneles/modales). Antes de editar, el PR debe inventariar por introspección/búsqueda todas las recetas equivalentes y congelar el conjunto para no migrar solo una muestra.

Objetivos:

- una única receta de base/hover/focus/disabled por variante;
- evitar que cada panel redefina gradiente, borde y foco inline;
- conservar semántica y callbacks; cambio puramente de presentación.

### A3 — MedievalSharp: decidir uso o eliminación

**Estado:** investigación previa obligatoria, no afirmar todavía “fuente muerta”.

El árbol contiene assets WOFF2 de MedievalSharp. Antes de eliminarlos hay que demostrar dos propiedades distintas:

1. **reachability CSS/runtime:** ninguna regla o superficie productiva los usa;
2. **packaging:** confirmar si PyInstaller los incluye efectivamente en el ejecutable/distribución y medir el impacto real.

Decisión posterior:

- si existe un rol visual justificado, asignarlo explícitamente (p. ej. títulos/hero) y anclarlo;
- si no existe reachability y el asset se empaqueta, eliminarlo junto con su declaración/referencia;
- no mantener una fuente solo “por si acaso”.

### C3 — Layout/sections legacy

**Estado:** candidato a limpieza; **código muerto no confirmado todavía**.

La auditoría señala `views/layout/header.py`, `views/layout/sidebar.py` y `sections/*` como superficies legacy asociadas a `render_dashboard_page_content`, mientras el dashboard actual usa Forge. Sin embargo, antes de borrar hay que probar reachability completa porque símbolos legacy todavía pueden estar exportados/importados.

Criterio de cierre:

- enumerar imports y callers productivos de `render_dashboard_page_content` y del paquete legacy;
- si el conjunto productivo es vacío, eliminar/deprecar en PR de limpieza atómico;
- si existe un consumidor, migrarlo primero;
- agregar un ancla que impida reintroducir accidentalmente dos shells/paletas paralelos.

### Responsive baseline

**Estado:** pendiente.

Se verificaron recetas rígidas en el Forge, entre ellas una grilla de cuatro columnas y hero con tipografía de 52 px. No formular el problema como “el CSS no tiene media queries”: sí existen reglas responsive/de accesibilidad en el proyecto; la deuda es que estas superficies principales no tienen una adaptación de ventana pequeña demostrada.

Criterio de diseño:

- definir breakpoints por comportamiento, no por dispositivo concreto;
- stats: 4 → 2 → 1 columnas según ancho utilizable;
- hero: `clamp()` o escala equivalente sin desbordes;
- validar navegación, chat, Orden de Carga y modales en viewport reducido;
- preservar foco visible y reduced-motion del #522.

## 4. Propuestas artísticas

Estas tareas son mejoras de producto, no defectos técnicos demostrados. Mantenerlas separadas de correcciones de integridad.

### D3 — Integridad reactiva del hero

La barra de integridad debe usar el estado real como señal visual, por ejemplo:

- oro: estable;
- brasa: vigilancia/estado degradado no terminal;
- carmesí: disputa/error.

La fuente de verdad debe ser el estado reactivo existente; no duplicar lógica de diagnóstico dentro de la vista. Color nunca debe ser la única señal: conservar texto/glifo/estado accesible.

### D4 — Emblema del ojo de dragón en el wizard

Reutilizar el emblema SVG diegético ya disponible en la identidad visual en lugar de un engranaje genérico, si la auditoría de assets confirma que el SVG reutilizable es el mismo recurso y no una copia divergente.

### D6 — Rombo `◆` como glifo de estado

Aplicar el rombo textual como vocabulario visual coherente en badges de disputas y registro de la Puerta. Mantener la política de glifos del #522: evitar codepoints con presentación emoji por defecto y no depender del glifo/color como única señal semántica.

### D2 — Lore en el wizard

Agregar una cita/lore rotatorio inspirado en las pantallas de carga de Skyrim durante el primer arranque.

Requisitos:

- contenido local y determinista, sin red;
- no bloquear ni ralentizar el wizard;
- separar copy/lore de la lógica de setup;
- revisar procedencia/licencia del texto: no copiar extensamente texto protegido de Bethesda; preferir texto original compatible con el tono del proyecto.

### D1 — Brújula Skyrim en el header

Replantear el HUD GPU/CPU como barra-compás con rombos/marcadores, manteniendo la telemetría legible.

Antes de implementarlo:

- prototipo visual aislado;
- definir qué dato representa cada marcador;
- no convertir telemetría funcional en decoración ambigua;
- validar ventana pequeña y reduced-motion.

### D5 — Sonido diegético

**Estado:** propuesta con seam por verificar.

No asumir que `playSkyrimSound` está actualmente cableado: la búsqueda del árbol usada para este baseline no devolvió evidencia suficiente de ese símbolo. El PR debe primero verificar si existe un seam de audio reutilizable.

Si no existe, el feature incluye diseñarlo. En ambos casos:

- asset con licencia/procedencia compatible;
- toggle de silencio persistente en Ajustes;
- default conservador;
- cero reproducción automática molesta durante arranque/errores repetitivos;
- ausencia del asset o fallo de audio nunca debe afectar el flujo funcional.

## 5. Mascotas Skyrim

**Estado:** experimento visual posterior; no mezclar con correcciones de GUI.

Orden sugerido: comenzar por un dragoncito SVG inline animado por CSS como indicador complementario de estado. Luego comparar variantes (orco, argoniana, elfa) en trabajo de arte separado.

Guardarraíles:

- el store reactivo es la fuente de verdad; la mascota no inventa estado;
- `prefers-reduced-motion` debe congelar/desactivar la animación decorativa;
- el indicador funcional sigue existiendo sin mascota;
- assets originales o con licencia compatible;
- variantes de arte se comparan sin introducir varias implementaciones productivas paralelas.

## 6. Secuencia recomendada

Orden por riesgo/deuda antes que ornamentación:

1. squash merge de #522 y sincronización parcial de T-22/T-24;
2. #523 — ownership multi-tab;
3. auditoría/limpieza C3 y decisión A3, preferiblemente como PRs atómicos separados;
4. C2 — centralización de botones;
5. responsive baseline;
6. D3 — integridad reactiva;
7. D4 — emblema del wizard;
8. D6 — glifo de estado;
9. D2 — lore;
10. D1 — brújula/HUD;
11. D5 — sonido, una vez verificado/diseñado el seam;
12. mascotas.

La secuencia puede cambiar por dependencias descubiertas, pero **#523 y la deuda estructural deben preceder a añadir nuevas superficies decorativas**.

## 7. Reglas de ejecución por PR

Para cada ítem:

1. revalidar el hallazgo contra `main` actualizado;
2. una rama + un PR atómico;
3. declarar la propiedad del mecanismo que se quiere preservar;
4. enumerar todas las superficies hermanas afectadas;
5. agregar test/ancla cuando la propiedad sea verificable automáticamente;
6. ejecutar Ruff check + format, tests GUI focalizados y los gates aplicables;
7. hacer smoke visual separado cuando el claim sea de render/comportamiento real;
8. no expandir alcance para arreglar hallazgos preexistentes no relacionados: registrarlos y tratarlos en PR aparte.
