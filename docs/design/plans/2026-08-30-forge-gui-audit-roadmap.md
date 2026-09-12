# Roadmap post-auditoría GUI — Forja del Dovahkiin

**Fecha de baseline:** 2026-08-30  
**Alcance:** shell Forge / GUI NiceGUI.  
**Propósito:** conservar los pendientes reales de la auditoría visual sin mezclar defectos comprobados, decisiones de limpieza y propuestas artísticas.

> Este documento es un plan de trabajo. Antes de implementar cada ítem se debe
> revalidar el árbol actual y leer `AGENTS.md` y el `AGENTS.md` aplicable al
> subárbol. Las propiedades que cubran familias de superficies deben anclarse
> mediante enumeración/introspección, no mediante muestras manuales.

## 1. Baseline: PR #522 (Merged)

PR: `#522` — `fix(gui): quick wins visuales del tema Forja del Dovahkiin`  
Squash commit en `main`: `89d81ad2a86821c55b19cdd83b339aa914e4288e`  
PR HEAD previo (trazabilidad): `1701e94179d2ee38d7536b38058e9cfd120f9d23`  
Estado actual: **MERGED** en `main`.

El PR cubrió los cinco quick wins de la auditoría:

1. scrollbar temática `.sc-scroll`, incluyendo fallback estándar y WebKit;
2. Cinzel hasta peso 900 en las dos caras declaradas;
3. foco visible por teclado y política dirigida de `prefers-reduced-motion`;
4. contraste de textos/rituales sin atenuar globalmente la tarjeta;
5. sustitución de iconos emoji del shell por SVG diegéticos y contrato de glifos.

Cierre de revisión verificado sobre ese PR:

- CI principal y workflows Qodo: `success`;
- anclas de contrato incorporadas en `tests/test_gui_theme_contracts.py`;
- review threads resueltos;
- `#523` permanece fuera de alcance y se gestiona como seguimiento independiente.

### Sincronización OODA tras el merge de #522

Tras la integración de #522 en `main`, `docs/pending_ooda_status.md` fue reconciliado con los cierres parciales demostrados:

- **T-22**: actualizado a **Parcial** — #522 cubrió la política de `prefers-reduced-motion` sobre animaciones decorativas; continúa pendiente la revisión/cierre del resto del contrato de transiciones.
- **T-24**: actualizado a **Parcial** — #522 cubrió foco visible de teclado (incluyendo inputs Quasar); continúa pendiente el inventario exhaustivo de labels y formularios accesibles.

## 2. Prioridad inmediata: integridad antes que arte

### P0 — #523: ownership multi-tab de estado efímero de rituales (feedback y preflight)

**Estado:** abierto; defecto de concurrencia preexistente a #522 (detectado durante su revisión y ampliado en la auditoría de #525).  
**Regla de alcance:** PR separado; no mezclar con cambios cosméticos.

**Diagnóstico de frontera y superficies hermanas:**
El proceso NiceGUI comparte un único `ReactiveStore` entre todas las pestañas abiertas. Hoy existen dos superficies hermanas afectadas por la misma clase de defecto («dos superficies, un recurso»):

1. `STORE_KEY_RITUAL_FEEDBACK`: `_ritual_feedback_panel()` en `forge_dashboard.py` consume y limpia globalmente la clave al reanudar o cerrar el toast, lo que borra el feedback de otra pestaña antes de que su dueño lo vea.
2. `STORE_KEY_RITUAL_PREFLIGHT`: `_ritual_preflight_panel()` en `forge_dashboard.py` lee y descarta globalmente el reporte de preflight (`store.set(STORE_KEY_RITUAL_PREFLIGHT, None)`), mientras que `run_ritual` y `run_ritual_install` en `ritual_runner.py` limpian y publican preflight sobre esa misma clave global. Una pestaña puede ocultar o sobreescribir el semáforo/reporte de preflight rojo de otra pestaña.

**Invariante / Propiedad del mecanismo:**
> Todo estado efímero de ritual que pueda ser publicado, mostrado, reanudado o descartado desde una pestaña debe conservar ownership explícito; una pestaña nunca puede consumir, reemplazar o limpiar el estado perteneciente a otra.

**Frontera de ownership obligatoria:**
- `STORE_KEY_RITUAL_FEEDBACK` (toasts de feedback de rituales);
- `STORE_KEY_RITUAL_PREFLIGHT` (panel/semáforo dismissible de preflight);
- `STORE_KEY_RITUAL_LAST_RESULT` y acción de reanudación (`resolve_ritual_resume_action` / `clear_ritual_result_owned`) bajo un modelo de ownership coherente.

**Criterios de cierre de #523:**
- feedback con scoping por pestaña/owner;
- preflight con scoping por pestaña/owner;
- resultado ritual y acción de reanudación con ownership coherente;
- dismiss de feedback y preflight acotado estrictamente a su owner;
- tests de carrera multi-pestaña que simulen explícitamente dos owners concurrentes;
- ancla por AST/introspección que enumere todos los productores, consumidores y limpiadores de la familia en `ReactiveStore` (evitando muestreos parciales).

## 3. Deuda estructural verificada o candidata a verificación

### C2 — Centralizar clases de botón

**Estado:** **RESUELTO** en #577 — familia `.sc-btn` (base + `--gold` /
`--ghost` / `--danger`) en la sección 6b de `styles.css`, consumida por los 13
call-sites del shell. Unificadas las dos recetas divergentes de "Denegar"
(HITL modal vs Puerta de Descargas). Estados completos: base, hover, active y
disabled (`.sc-btn:disabled` con cursor honesto y gris apagado vía `filter`,
el mecanismo del hover; NiceGUI aplica `props("disabled")` como atributo
`disabled` del `<button>` nativo); el foco de teclado sigue cubierto por la
política global `:focus-visible`. La transición inline del CTA hero extiende
la de la receta sin pisar su `filter`.

Ancla por AST en
`test_gui_theme_contracts.py::test_botones_c2_del_shell_inventario_exhaustivo_y_sin_receta_inline`:
enumera TODOS los botones de `forge_dashboard.py` y particiona por igualdad
exacta en 13 consumidores C2 (identidad `función:variable` → variante) + 9
excepciones deliberadas fuera de la familia (nav del sidebar, toggles de
estado, botones icónicos, link «Ver Todo», acción de tarjeta de ritual,
escaneo profundo); un botón nuevo sin clasificar, un consumidor que pierde la
clase o cambia de variante, o una receta central reintroducida inline bajo
cualquier grafía (verificación por nombre de propiedad, no por string) rompen
el test. Complemento CSS en
`test_receta_sc_btn_centralizada_con_estado_disabled` (receta única + estados).

### A3 — MedievalSharp: alcance y decisión de adopción vs. eliminación

**Estado:** **RESUELTO (eliminación)** en #572 — la familia quedó fuera de
`fonts.css` y los dos woff2 fuera del bundle y del árbol; ancla en
`tests/test_gui_theme_contracts.py::test_medievalsharp_fuera_del_bundle`
(regex sobre CSS sin comentarios: cubre comillas simples, dobles y
declaración sin comillas).
Si en el futuro se le quisiera dar rol (p. ej. títulos diegéticos), adoptarla
exige reintroducir el asset más la regla, y el ancla aplica presión consciente.

### C3 — Layout/sections legacy: depuración con exclusión de componentes activos

**Estado:** **RESUELTO** en #572 — isla pre-Forge eliminada (layout entero,
secciones del viejo home, componentes solo consumidos por ellas), isla de
`dashboard_page.py` reducida a la fachada, `chat_preview.py` recortado al seam
vivo `_try_send_with_rollback`, `components/buttons.py` retenido por la cadena
viva de `preview_manifest_panel`. Anclas: los 11 archivos eliminados están
congelados por nombre y la superficie pública de `views` queda en
`{"render_dashboard"}`.

La familia `sections/*` no puede tratarse en bloque como código muerto: el dashboard Forge actual importa activamente `create_preflight_panel` desde `.sections` (`forge_dashboard.py:37`) y lo invoca en la línea 880 para renderizar reportes de preflight de rituales. Tratar todo `sections/*` como legacy mezclaría componentes productivos vivos con las viejas secciones de inicio.

Criterio de cierre:

- inventariar los módulos individualmente:
  - **superficies legacy:** `views/layout/header.py`, `views/layout/sidebar.py` y las secciones del viejo home (`cta_section.py`, `features_section.py`, `mods_preview.py`, `stats_section.py`) asociadas a `render_dashboard_page_content` / `dashboard_page.py`;
  - **componentes activos:** `preflight_panel.py` debe ser explícitamente retenido y/o reubicado (p. ej. a `views/components/`) antes de cualquier eliminación del namespace legacy;
- demostrar reachability de cada export antes de eliminar;
- si las superficies legacy no tienen callers productivos tras la separación, eliminarlas en un PR de limpieza atómico;
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

**Estado:** **RESUELTO** en #581 — la barra de integridad ya no es dorada fija:
tres variantes CSS (`.sc-bar--estable` oro / `.sc-bar--vigilante` brasa /
`.sc-bar--disputa` carmesí) mapeadas desde `_ESTADO_FORJA`
(`forge_dashboard.py`) según el conteo de conflictos real. El sello ◆ también
gana color por estado (VIGILANTE deja de compartir el verde de ESTABLE:
brasa/carmesí). El texto/cifra permanecen como señal accesible, el color jamás
es la única pista. Ancla: `test_integridad_del_hero_reactiva_por_estado`.

**Seguimiento (revisión de #581).** El ancla original *inventariaba* el mapeo —
qué estados existen, qué variante le toca a cada uno, qué recetas CSS hay — pero
no tocaba el HTML que el hero emite. Tres regresiones pasaban verdes y se
verificaron por mutación: fijar `estado` a un literal, borrar la clase dinámica
`sc-bar--{variante}` (que deja la barra **sin fondo**, porque D3 se llevó el
gradiente inline) y devolver el color de sello de otro estado — justo el bug que
D3 arregló de paso. El panel pasa a emitirse por un seam puro
(`_integridad_html`, como `_vitals_html`/`_hud_html`) y el contrato se verifica
sobre el HTML real, enumerando los umbrales con ambas fronteras (4/5). Anclas:
`test_integridad_del_hero_pinta_el_estado_en_el_html_renderizado` y
`test_el_hero_consume_el_seam_de_integridad`.

### D4 — Emblema del ojo de dragón en el wizard

**Estado:** **RESUELTO** en #579 — el wizard de primer arranque muestra el
emblema de la marca en el medallón (antes un engranaje genérico). Un solo
recurso: `_ICON_DRAGON_EYE_TEMPLATE` en `icons.py`, compartido por sidebar y
wizard. Como el wizard es overlay sobre el dashboard y ambas instancias
coexisten en el DOM, los ids del gradiente se parametrizan por superficie
(`_icon_dragon_eye(iris_id="scIris-sidebar" | "scIris-wizard")`) — un id
duplicado haría ambigua la referencia `url(#)` (revisión adversarial del PR).
Anclas: censos AST del emblema (`test_emblema_dragon_unico_y_compartido`,
`test_emblema_ids_unicos_por_instancia`) y censo congelado del registro
(`test_registro_iconos_congelado_y_sin_muertos`).

### D6 — Rombo `◆` como glifo de estado

Aplicar el rombo textual como vocabulario visual coherente en badges de disputas y registro de la Puerta. Mantener la política de glifos del #522: evitar codepoints con presentación emoji por defecto y no depender del glifo/color como única señal semántica.

### D2 — Lore en el wizard

**Estado:** **RESUELTO** en #584 — cita rotatoria al pie del wizard de primer
arranque (`_WIZARD_LORE`, 5 frases originales inventariadas en el módulo), con
ciclo `itertools` + `ui.timer(6s)`, apagado del timer al cerrar el modal
(defensa ante RuntimeError del DOM borrado). Cero red, cero bloqueo. Ancla:
`test_lore_d2_del_wizard_inventario_y_mecanica`. Copy original, nada de texto
de Bethesda.

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

**Estado:** seam existente / implementación real de audio pendiente.

`gui_helpers._load_css()` ya define `window.playSkyrimSound = window.playSkyrimSound || function (_type) {};` (`sky_claw/app/gui/gui_helpers.py:48`) como seam global centralizado (no-op silencioso para evitar `ReferenceError` en consola mientras no existan assets empaquetados). Distintos componentes ya lo invocan en hover y click: `views/components/buttons.py:51` (`playSkyrimSound('click')`), `views/components/feature_card.py:46` (`playSkyrimSound('hover')`) y `views/components/stat_card.py:54` (`playSkyrimSound('hover')`).

Por tanto, **no hace falta diseñar ni descubrir otro seam**: la interfaz ya está cableada. El alcance de la tarea queda acotado a implementar la reproducción real de sonido detrás del seam existente.

Guardarraíles y requisitos:

- asset de audio con licencia/procedencia compatible y limpia (sin redistribuir audio propietario de Bethesda);
- implementación técnica de audio detrás del seam existente `window.playSkyrimSound`;
- toggle de silencio persistente en Ajustes / Config;
- default conservador (volumen moderado / sin estridencias);
- cero reproducción automática molesta o repetitiva durante el arranque o ante ráfagas de errores;
- la ausencia del asset o cualquier fallo en el subsistema de audio nunca debe afectar el flujo funcional ni propagar excepciones no capturadas.

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

Orden por riesgo/deuda antes que ornamentación (asumiendo #522 ya integrado en `main` y T-22/T-24 reconciliados parcialmente):

1. **#523** — ownership multi-tab de estado efímero (feedback, preflight, result/resume);
2. **Auditoría/limpieza C3 y decisión A3**, preferiblemente como PRs atómicos separados;
3. **C2** — centralización de clases de botones;
4. **Responsive baseline**;
5. **D3** — integridad reactiva del hero;
6. **D4** — emblema del wizard;
7. **D6** — glifo de estado;
8. **D2** — lore en el wizard;
9. **D1** — brújula/HUD;
10. **D5** — sonido diegético tras el seam existente;
11. **Mascotas**.

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
