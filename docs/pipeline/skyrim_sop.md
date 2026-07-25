# Skyrim Modding Pipeline — Manual de Operaciones (SOP)

> **Audiencia:** Usuarios avanzados, modders y desarrolladores que necesitan comprender la lógica de dominio y el orden de operaciones del pipeline de modding que Sky-Claw orquesta.
> **Nota Técnica:** Este documento extrae el conocimiento de dominio del [AGENTS.md operativo](../../sky_claw/local/AGENTS.md). Para reglas de edición de código y directivas de agentes IA, consultar ese archivo directamente.

---

## 1. Arquitectura del Pipeline

El proceso de modding no es una colección de scripts aislados; es un **Grafo Dirigido Acíclico (DAG)** estricto. Cada etapa consume el estado estabilizado por la etapa anterior. Saltar, reordenar o re-ejecutar una etapa intermedia invalida todos los artefactos subsiguientes, resultando en corrupción de guardadas, texturas faltantes o crashes silenciosos.

### 1.1 Orden Cronológico de Ejecución

| Etapa | Herramienta | Propósito Principal | Artefactos de Salida |
| :--- | :--- | :--- | :--- |
| **1** | **xEdit (QuickAutoClean)** | Sanitizar archivos maestros (Update.esm, DLCs). Eliminar ITMs y reparar UDRs. | Plugins maestros limpios. |
| **2** | **Cathedral Assets Optimizer** | Optimización de assets por mod individual. Compresión de texturas, generación de mipmaps, empaquetado `.bsa`. | Texturas comprimidas, `.bsa` generados. |
| **3** | **BodySlide + Outfit Studio** | Construcción de bases morfológicas y conformado de armaduras al preset corporal. | Meshes 3D (`.nif`), armaduras adaptadas. |
| **4** | **Pandora Behaviour Engine** | Compilación de comportamientos de IA, animaciones y esqueletos al formato del motor gráfico. | Grafos de comportamiento inyectados. |
| **5** | **LOOT** | **Estabilización del Load Order.** Verificación de dependencias maestras y reporte de conflictos. | `loadorder.txt` y `plugins.txt` actualizados. |
| **6** | **Wrye Bash** | Consolidación de Listas de Niveles (Leveled Lists) en un parche unificado. | `Bashed Patch, 0.esp`. |
| **7** | **Synthesis** | Generación de parches dinámicos y mutadores algorithmicos (clima, IA, luces). | `Synthesis.esp`. |
| **8** | **No Grass In Objects** | Precacheo de métricas espaciales para prevenir que el pasto atraviese caminos y ruinas. | Caché de pasto (`\Grass\`). |
| **9** | **TexGen ➔ DynDOLOD 3** | Generación paramétrica de LODs (Level of Detail) dinámicos para el horizonte geográfico. | Paquetes de LOD, plugins `.esp`/`.esm` espaciales. |

### 1.2 Matriz de Dependencias Críticas

- **LOOT (Etapa 5) es el prerrequisito universal:** Ningún generador de parches (Wrye Bash, Synthesis) ni generador de LODs (DynDOLOD) puede ejecutarse antes de que LOOT haya estabilizado el orden. Un parche construido sobre un orden inestable es basura.
- **TexGen/DynDOLOD (Etapa 9) es el nodo sumidero:** Debe ejecutarse **ABSOLUTAMENTE AL FINAL**. Requiere que Wrye Bash, Synthesis y No Grass In Objects hayan completado sus tareas. Si DynDOLOD corre antes, los LODs generados ignorarán los parches dinámicos, causando "pop-in" y referencias faltantes.
- **No Grass In Objects (Etapa 8) precede a DynDOLOD:** Revertir este orden causa que el pasto se renderice incorrectamente sobre geometría de LOD.

---

## 2. Protocolo de Resolución de Conflictos

El motor de Skyrim no resuelve todas las colisiones por simple sobreescritura en disco. Existen tres capas conceptuales de resolución que Sky-Claw respeta.

### Capa 1: La Regla del Uno (Bases de Datos de Plugins)
Si dos mods (`.esp` / `.esm` / `.esl`) alteran el mismo registro (ej. la salud de un NPC), el mod que carga **físicamente al final** en el load order anula permanentemente los cambios del anterior. No hay fusión aditiva en esta capa sin un parcheador externo.

### Capa 2: Gestión Sistemática de Registros (Parches)
Para escapar de la "Regla del Uno", se utilizan dos herramientas que **no deben superponerse**:

| Clase de Conflicto | Herramienta | Comportamiento |
| :--- | :--- | :--- |
| **Leveled Lists** (Inventarios inyectados en NPCs/Contenedores) | **Wrye Bash** | Fusión aditiva. El motor **SUMA** las entradas en lugar de sobrescribirlas. |
| **Sobreescrituras Masivas** (IA, condiciones lógicas, clima) | **Synthesis** | Mutadores paramétricos en tiempo real unifican todas las anulaciones en un solo plugin. |

> **Regla de Oro:** Nunca delegar Leveled Lists a Synthesis si Wrye Bash ya las fusionó. Nunca delegar lógica de IA/Clima a Wrye Bash. La separación es canónica.
> **Advertencia de Wrye Bash:** Jamás utilizar Wrye Bash para fusionar efectos mágicos, parámetros acústicos o costes de hechizos. Hacerlo multiplicará los costos de maná en overhauls de magia. **Solo Leveled Lists.**

### Capa 3: Gestión de Assets Físicos (Archivos Sueltos y BSAs)
Los conflictos gráficos (ej. dos texturas para el mismo ladrillo) se resuelven **EXCLUSIVAMENTE** manipulando la jerarquía de prioridad en el panel izquierdo del Mod Organizer 2 (VFS). 
- En `modlist.txt`, el mod listado **AL FINAL** (más abajo en el panel) tiene la **mayor prioridad** de archivos sueltos (se lee de abajo hacia arriba).
- No existe fusión a nivel de registros para assets.
- **Recomendación:** Usar Cathedral Assets Optimizer (CAO) para comprimir archivos sueltos en `.bsa`. Esto previene lecturas de disco innecesarias. Cargas con archivos sueltos sin comprimir son un defecto de rendimiento.

---

## 3. Restricciones y Anomalías por Herramienta

Documentación de los modos de fallo conocidos y reglas operativas específicas.

### 3.1 xEdit / QuickAutoClean (QAC)
- **Regla:** Limpiar EXACTAMENTE UN archivo por ejecución. La limpieza por lotes causa contaminación cruzada de NavMesh.
- **Anomalía Crítica (Dawnguard.esm):** Requiere **DOS** pasadas automáticas de QAC, seguidas de limpieza manual de tres celdas específicas (`CELL 00016BCF`, `CELL 0001FA4C`, `CELL 0006C3B6`). Una sola pasada se considera un defecto.

### 3.2 LOOT
- **Error "Something went wrong!":** El archivo `plugins.txt` está marcado como Solo Lectura. Quitar el atributo y reintentar.
- **Error "Cero mods detectados":** La ruta VFS del juego está construida sobre symlinks. LOOT no puede resolver mods a través de un VFS con symlinks. Reconfigurar el VFS para usar rutas directas.

### 3.3 Wrye Bash
- **Error "FILE NOT FOUND":** Un archivo maestro fue movido o eliminado entre ejecuciones. **Solución:** Re-ejecutar LOOT para refrescar la lista de maestros, luego reconstruir el parche.
- **CTD / "Unrecognized version" en Skyrim ≥ 1.6.1130:** Wrye Bash no puede procesar nativamente plugins con Header 1.71. **Solución:** Inyectar el mod **BEES** (Backported Extended ESL Support) antes de reconstruir.

### 3.4 Synthesis
- **Entorno:** Debe instalarse en un directorio virgen fuera de MO2 y del juego (ej. `C:\Tools\Synthesis`). Requiere el **.NET SDK** (x64), no solo el Runtime.
- **Error "DotNet SDK Not Detected":** Colisión con vestigios x86 (32-bit) de dotnet. Desinstalar el runtime x86 con la herramienta oficial e instalar el SDK x64.
- **Fallo "Max Masters Exceeded":** Skyrim rechaza cualquier `.esp` con más de 254 maestros. En cargas con ~1000+ mods, es **OBLIGATORIO** habilitar la directiva `Split Files if Max Masters Exceeded` (Auto-Split) en Synthesis.

### 3.5 DynDOLOD 3
- **Error "Resources SE version information not found":** Jerarquía de DLL incorrecta. La carpeta de DynDOLOD DLL NG debe ubicarse **DEBAJO** del directorio oficial Resources SE en la jerarquía de prioridad.
- **Crash por desbordamiento de punteros:** Configurar `Temporary=1` en `DynDOLOD_SSE.ini` para liberar límites de referencias en el límite del motor.

### 3.6 No Grass In Objects (Precache)
- **Fallo "Zero-bounds":** Carpetas de salida vacías. Un mod de terceros contiene registros con límites nulos `(0,0,0)`. Purgar la dependencia de mesh rota vía Creation Kit antes de reintentar.
- **Tolerancia Térmica:** Limitar temporalmente la resolución a 800x400 y desactivar ENB/Shaders durante el precacheo para tolerar los escaneos reiterados de celdas sin colgar la GPU.

---

## 4. Tabla de Referência Rápida de Fallos Críticos

| Síntoma | Causa Raíz | Solución Obligatoria |
| :--- | :--- | :--- |
| Pasto atraviesa caminos tras DynDOLOD | Precache de pasto corrió DESPUÉS de DynDOLOD | Re-ejecutar pipeline: Precache PRIMERO, DynDOLOD ÚLTIMO |
| Costs de maná multiplicados | Wrye Bash fusionó efectos mágicos | Reconstruir parche con Leveled Lists ONLY |
| Crash de Skyrim por >254 maestros | Synthesis excedió el límite | Habilitar Auto-Split en Synthesis |
| LOOT no detecta mods | VFS usa symlinks | Reconfigurar VFS sin symlinks |
| Synthesis no detecta .NET SDK | Conflicto con dotnet x86 | Desinstalar x86, instalar SDK x64 |
| CTD en Wrye Bash (Header 1.71) | Incompatibilidad nativa | Instalar mod BEES antes de correr Wrye Bash |
