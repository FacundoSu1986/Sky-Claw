# NGIO — autoinstalador: preparación y plan de prueba en rig descartable (Decisión 4)

> **Fecha:** 2026-11-25 · **Estado:** **NO EJECUTADO** — preparación y verificación de lectura.
> **Alcance autorizado:** preparar y probar `ToolsInstaller.ensure_ngio()` **sólo** en un rig
> descartable con independencia física demostrada. **No** incluye ejecutar Skyrim ni iniciar un
> precache real. **No** se corrige el instalador dentro de P1: los defectos quedan registrados para P3.

## 1. Por qué no se ejecutó

| Requisito de la autorización | Estado real |
|---|---|
| Rig descartable con **independencia física demostrada** | **No existe.** Este entorno tiene una única instalación de Skyrim/MO2 (la de producción). Crear un rig exige un volumen y una instalación de juego separados; no puedo fabricarlo sin tocar la instalación productiva, que está prohibido |
| **Aprobaciones HITL individuales** para descarga e instalación | El canal HITL es del producto (GUI/Telegram) y requiere un operador humano. Yo no puedo aprobar en nombre del operador |
| **Credenciales de Nexus** por el mecanismo seguro | `ensure_ngio` exige `NexusDownloader` (Address Library y Grass Cache Helper NG sólo existen en Nexus). La API key se configura en la app; no está disponible para un agente y **no debe** pasar por el chat ni por el repositorio |
| No ejecutar Skyrim / no precachear | Se respetó |

Por eso la Decisión 4 queda **preparada**, con todo lo verificable sin instalar, y bloqueada en el
punto exacto donde hace falta un humano y una máquina separada.

## 2. Qué descargaría hoy Sky-Claw (verificado 2026-11-24/25)

`ToolsInstaller.ensure_ngio()` (`sky_claw/local/tools_installer.py:2567-2650`) instala **tres**
componentes como mods de MO2 (no toca `modlist.txt`; la activación la hace el caller):

| Componente | Fuente | Selección | Versión que descargaría HOY |
|---|---|---|---|
| `No Grass In Objects NG` | GitHub `DwemerEngineer/No-Grass-In-Objects-NG`, `releases/latest` (`:266`, `:2616`) | `_select_ngio_asset` (`:1241-1265`): AE → asset con `ngio-ae`; SE → sin `ngio-ae` y sin `vr` | **1.0.13** (`published_at 2024-04-30`): `NGIO-AE-1.0.13-1.6.640.zip` (AE), `NGIO-NG_1.0.13.1.7z` (SE) |
| `Address Library for SKSE Plugins` | Nexus **32444** | `version-1-5-*.bin` (SE) / `version-1-6-*.bin` (AE) | la del archivo principal del mod |
| `Grass Cache Helper NG` | Nexus **101095** | **sólo si la edición es AE** (`:2637-2649`) | archivo principal (1.0.3 al 2026-11-24) |

Sentinels de idempotencia: `*.dll` (NGIO y GCH) y `version-1-*.bin` (Address Library). Activación:
`app/agent/tools/external_tools.py:69-108` agrega `+Nombre` al `modlist.txt` del perfil en uso y
devuelve `activacion ∈ {ok|parcial|fallo|no_disponible|vacio}`; `success` de la tool es exactamente
`activacion == "ok"`.

### 2.1 Mecanismo por el que instala una versión vieja

`/releases/latest` **excluye prereleases**. Las releases posteriores a 1.0.13 (`1.0.17`, `1.0.17b3`)
están marcadas `prerelease: true`, así que «latest» queda anclado en **1.0.13** (abril 2024) mientras
`main` del repo está en **2.3.0** y la página de Nexus 42161 ya publica **2.3.0**. El instalador no
tiene pin ni comparación de versiones: instala lo que diga «latest».

**No se actualiza a 2.3.0 en P1** (instrucción explícita del propietario): el contrato de P1 se ancla
en **1.0.13**, que es la versión cuya fuente se inspeccionó. Cambiar de versión exige inspeccionar la
nueva, y eso es P3.

## 3. Grass Cache Helper NG: lo que hay que saber antes de generar

- **No es obligatorio según su autor**: *«Since NGIO is now updated and supports post-AE and VR
  versions as well, you don't strictly need this mod unless you want Seasonal Grass cache support.»*
  → la regla «AE ⇒ GCH» es una **decisión de producto de Sky-Claw**, no un requisito upstream.
- **Su función principal es de RUNTIME**: engancha la carga para que el juego lea `*.cgid` en vez de
  `*.gid`. En **generación** sólo ajusta INIs: *«Automatically sets bAllowCreateGrass,
  bAllowLoadGrass, bGenerateGrassDataFiles settings in the game ini by detecting if you are
  generating precache or not.»*
- **Ambos pueden estar activos a la vez**: NGIO-NG carga `Data/SKSE/Plugins/GrassCacheHelperNG.dll`
  con `LoadLibraryW` y, si está, **no instala su propio hook** de carga de nombre de archivo. El autor
  lo confirma para generación: *«You can use this mod or not alongside it.»*
- **NO VERIFICADO:** que GCH activo durante la generación **cambie la salida** (contenido/validez de
  los `.cgid`), fuerce otro modo de NGIO o evite el crash-loop. Es ausencia de evidencia, no prueba de
  inocuidad. **No se asume compatibilidad**: se registra que Sky-Claw los deja activos a los dos y que
  no hay fuente que lo prohíba.
- **Mutación fuera del perfil clonado:** GCH escribe settings en los INI del juego bajo
  `Documents/My Games/...`, que **no** forman parte del clon de perfil que Sky-Claw crea. Es una
  mutación de configuración del usuario que el ritual no controla ni revierte.

## 4. Defectos del instalador detectados (para P3, NO corregidos en P1)

| # | Defecto | Evidencia | Impacto |
|---|---|---|---|
| I-1 | Instala **1.0.13** porque `/releases/latest` excluye prereleases; `main` y Nexus están en 2.3.0 | `_NGIO_RELEASES_URL` (`:266`) + API de GitHub | versión ~28 meses atrás del desarrollo actual |
| I-2 | **No hay gate de versión del runtime**: el asset AE es `NGIO-AE-1.0.13-1.6.640.zip` y se elige por edición, no por la versión instalada (1.6.640 vs 1.6.1170) | `_select_ngio_asset` | un DLL para otro build puede no cargar |
| I-3 | **No hay gate de SKSE** en `ensure_ngio` | `ensure_ngio` no lo consulta | instala plugins SKSE sin verificar el prerequisito |
| I-4 | La exigencia **AE ⇒ GCH** no está respaldada por upstream | §3 | dependencia extra obligatoria por decisión propia |
| I-5 | GCH **reescribe INIs del juego** fuera del perfil clonado | §3 | mutación de config del usuario fuera del control del ritual y del respaldo |
| I-6 | Re-correr el instalador con una versión nueva **no actualiza** si el sentinel `*.dll` ya existe | `_ensure_github_mod` + sentinel | el operador cree que actualizó y no ocurrió |

## 5. Rig de prueba: precondiciones exactas (para ejecutar cuando haya humano + máquina)

1. `SKYCLAW_ISOLATED=1` **obligatorio** en todo proceso que construya `AppContext`/`Config` fuera de
   producción (el keyring pasa a un backend que **falla** y los descubridores dejan de ver la máquina).
2. Instancia MO2 **portable nueva** en un volumen **distinto** del de producción (p. ej.
   `E:\Skyclaw_Rig_NGIO\`), con su `ModOrganizer.ini` propio y `base_directory` explícito.
3. **Independencia física** demostrada, no lógica: `Path.resolve()` de las dos instancias sin prefijo
   común; sin junctions/symlinks en la cadena (`links.exigir_contencion_fisica`); `git status` del repo
   productivo sin cambios; `runtime_state_dir` aislado; **inventario y respaldo del estado inicial**
   del rig antes de tocar nada.
4. Perfil MO2 de prueba propio (`plugins.txt`/`modlist.txt`) — no el perfil de producción.
5. Verificación previa: runtime detectado (`detect_skyrim_edition`), SKSE presente, Address Library
   por edición, rutas efectivas de `mods/` y `overwrite/` bajo la instancia de prueba.
6. Credenciales: API key de Nexus configurada **por el mecanismo existente** (nunca por chat, log ni
   repositorio).
7. HITL: cada descarga e instalación se aprueba individualmente desde el canal del producto.

## 6. Plan de prueba (T1–T10)

| # | Caso | Evidencia exigida | Requiere |
|---|---|---|---|
| T1 | descarga + extracción SE | `mods/No Grass In Objects NG/SKSE/Plugins/*.dll`, `meta.ini`, asset sin `ngio-ae` | HITL (egress) |
| T2 | descarga + extracción AE | asset `NGIO-AE-*`, **GCH pedido**, `GrassCacheHelperNG.dll` | HITL + API key |
| T3 | Address Library por edición | `version-1-6-*.bin` en AE / `version-1-5-*.bin` en SE | API key |
| T4 | detección de edición | `edition` coincide con el `.exe` del rig | — |
| T5 | activación | `+Nombre` en el `modlist.txt` del **perfil de prueba**, `activacion == "ok"` | — |
| T6 | idempotencia | segunda corrida: `already_existed`, **sin** nueva descarga ni HITL nuevo | — |
| T7 | cancelación a mitad | lo instalado queda; sin archivos parciales con nombre definitivo; lock liberado; el próximo run pide sólo lo faltante | HITL (cortar) |
| T8 | denegación HITL | no se descarga nada; `activacion == "fallo"`; `success=False` con mensaje | HITL (denegar) |
| T9 | sin `SKYRIM_PATH`/`mo2_root` | `success=False` con motivo, sin egress | — |
| T10 | perfil ≠ clon de grass | los tres mods activos en el perfil de origen; el clon los hereda; `modlist.txt` del clon intacto | — |

## 7. Lo que NO se hizo

- No se instaló NGIO ni GCH manualmente ni con el autoinstalador.
- No se descargó ni extrajo ningún asset (sólo se leyó su código publicado y la API pública).
- No se ejecutó Skyrim ni se generó caché.
- No se modificó `tools_installer.py` ni `external_tools.py`.
- No se creó ninguna instancia MO2: no hay rig donde crearla sin tocar producción.
